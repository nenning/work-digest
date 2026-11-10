"""Monitor for external web sources (kind URL): OpenAPI docs, portal API JSON, HTML pages.

These sources have no page history. The monitor therefore extracts a current
*version* where it can and compares it with the version stated in the spec;
without a usable stated version it compares against a stored baseline snapshot
(``<data_dir>/url_baselines/``). Baselines are persistent state, not cache: they
are kept apart from the disposable spec cache, never created or advanced on a
dry run, and advanced to the new snapshot once a non-dry run has reported a
CHANGED result (so a one-off change is not reported forever; cosmetic-only
changes leave the baseline alone). Content is only diffed against such a
snapshot, never against the stated version.

Version sources, in order: OpenAPI/Swagger ``info.version`` (also probing
``<base>/v3/api-docs`` for swagger-ui URLs), top-level ``version`` of a JSON
object (developer.apis.post.ch portal API), first dotted version in an HTML
``<title>``. HTML pages are compared via title + h1-h3 + file links only
(navigation noise and render timestamps are ignored); everything else via a
hash of the normalised body text. The fingerprint always covers the full content; only the
stored/diffed text copy is capped at MAX_TEXT_CHARS (with a warning), so a change beyond the cap
is still detected.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from digest.config import RetryConfig
from digest.spec_monitor.cache import SpecCache, hash_key
from digest.spec_monitor.ech import version_key
from digest.spec_monitor.http import RequestFailed, request_with_retry
from digest.spec_monitor.models import (
    Classification,
    SourceKind,
    SourceRef,
    SourceResult,
    SpecInfo,
    VersionPoint,
)
from digest.spec_monitor.textdiff import diff_text, is_cosmetic

log = logging.getLogger(__name__)

BASELINE_NAMESPACE = "url-snapshot"
MAX_TEXT_CHARS = 25 * 1024 * 1024   # safety ceiling only; real documents are stored and diffed in full
_USER_AGENT = "Mozilla/5.0 (compatible; spec-monitor)"
_PORTAL_RE = re.compile(r"^(https?://developer\.apis\.post\.ch)/api/apis/([^/?#]+)(?:/pages/([^/?#]+)/content)?/?(?:[?#].*)?$", re.I)
_DOTTED_RE = re.compile(r"\d+(?:\.\d+)+")
_FILE_EXT_RE = re.compile(r"\.(?:pdf|zip|xsd|wsdl|xlsx?|docx|json|yaml|xml)$", re.I)
_HTMLISH_RE = re.compile(r"<\s*(?:!doctype|html|head|title|body|h[1-6]|a|div|p)\b", re.I)
_TEXT_CT = ("text", "json", "xml", "yaml", "html")
_NO_HISTORY = "content not compared (no history available)"
_HTML_LIMIT = "HTML page: only title, headings and file links are compared; changes in the body text are not detected"


def normalise_version(raw: Optional[str]) -> Optional[str]:
    """First version-looking token of a stated/found version, 'V1X8' -> '1.8', 'v1.4.0' -> '1.4.0'."""
    if not raw:
        return None
    text = re.sub(r"(?<=\d)[xX](?=\d)", ".", str(raw).strip())
    text = re.sub(r"(?<![\w.])[vV](?=\d)", "", text)
    m = re.search(r"(?<![\w.])\d+(?:\.\d+)*(?!\w)", text)  # not inside words such as commit shas
    return m.group(0) if m else None


def versions_equal(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = normalise_version(a), normalise_version(b)
    return na is not None and nb is not None and version_key(na) == version_key(nb)


class _Failure(Exception):
    def __init__(self, message: str, attempts: int = 1):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class _Snap:
    """What was observed at the URL right now."""
    version: Optional[str]
    text: Optional[str]          # None for binary content; capped at MAX_TEXT_CHARS
    fingerprint: str             # of the FULL content
    truncated: bool = False      # text is a capped copy of the full content
    notes: Optional[List[str]] = None   # observations to surface as warnings


def _make_snap(version: Optional[str], full_text: str, notes: Optional[List[str]] = None) -> "_Snap":
    truncated = len(full_text) > MAX_TEXT_CHARS
    return _Snap(
        version=version, text=full_text[:MAX_TEXT_CHARS], fingerprint=hash_key(full_text),
        truncated=truncated, notes=list(notes or []),
    )


def _normalise_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _canonical_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)


class _PageExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.headings: List[str] = []
        self.links: List[Tuple[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._heading: Optional[List[str]] = None
        self._heading_tag = ""
        self._anchor: Optional[Tuple[str, List[str]]] = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3"):
            self._heading, self._heading_tag = [], tag
        elif tag == "a":
            href = dict(attrs).get("href")
            self._anchor = (href, []) if href else None

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == self._heading_tag and self._heading is not None:
            text = re.sub(r"\s+", " ", "".join(self._heading)).strip()
            if text:
                self.headings.append(f"{tag.upper()}: {text}")
            self._heading, self._heading_tag = None, ""
        elif tag == "a" and self._anchor is not None:
            href, parts = self._anchor
            self._anchor = None
            if _is_file_link(href):
                text = re.sub(r"\s+", " ", "".join(parts)).strip()
                self.links.append((text, urljoin(self.base_url, href)))

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._heading is not None:
            self._heading.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)


def _is_file_link(href: str) -> bool:
    path = urlparse(href).path
    return bool(_FILE_EXT_RE.search(path)) or "/asset/" in href.lower() or "download" in href.lower()


def _html_snapshot(body: str, url: str) -> _Snap:
    parser = _PageExtractor(url)
    try:
        parser.feed(body)
        parser.close()
    except Exception as exc:
        raise _Failure(f"HTML could not be parsed: {type(exc).__name__}: {exc}")
    title = re.sub(r"\s+", " ", parser.title).strip()
    lines = ([f"Title: {title}"] if title else []) + parser.headings
    seen = set()
    for text, href in parser.links:
        line = f"Link: {text} -> {href}" if text else f"Link: {href}"
        if line not in seen:
            seen.add(line)
            lines.append(line)
    if not lines:  # page without recognisable structure: whole-body text as in the generic case
        return _plain_snapshot(re.sub(r"<[^>]*>", " ", body))
    text = _normalise_text("\n".join(lines))
    m = _DOTTED_RE.search(title)
    return _make_snap(m.group(0) if m else None, text, notes=[_HTML_LIMIT])


def _plain_snapshot(body: str) -> _Snap:
    return _make_snap(None, _normalise_text(body))


def _json_snapshot(data) -> _Snap:
    version = None
    if isinstance(data, dict):
        info = data.get("info")
        if (data.get("openapi") or data.get("swagger")) and isinstance(info, dict) and info.get("version") is not None:
            version = str(info["version"])
        elif isinstance(data.get("version"), str) and data["version"].strip():
            version = data["version"].strip()
    return _make_snap(version, _canonical_json(data))


def _openapi_info(data) -> Optional[str]:
    if isinstance(data, dict) and (data.get("openapi") or data.get("swagger")):
        info = data.get("info")
        if isinstance(info, dict) and info.get("version") is not None:
            return str(info["version"])
    return None


class UrlMonitor:
    def __init__(
        self, retry: RetryConfig, cache: SpecCache, today: Optional[date] = None,
        baseline_dir: Optional[Path] = None, dry_run: bool = False, defer_baselines: bool = False,
    ):
        self.retry = retry
        self.cache = cache
        self.baselines = SpecCache(baseline_dir or (cache.root.parent / "url_baselines"))
        self.dry_run = dry_run
        self._today = today
        # Baselines advance past a change the moment they are saved; a caller that reports the
        # change by mail defers them until the mail is out, so a failed send is reported again.
        self.defer_baselines = defer_baselines
        self._pending: dict = {}

    def commit_baselines(self) -> List[str]:
        """Write the deferred baselines; returns a message per snapshot that could not be saved."""
        failed = []
        for key, data in list(self._pending.items()):
            if self.baselines.put_json(BASELINE_NAMESPACE, key, data):
                del self._pending[key]
            else:
                failed.append(f"baseline snapshot for {data.get('url', key)} could not be saved")
        return failed

    def handles(self, ref: SourceRef) -> bool:
        return ref.kind == SourceKind.URL

    def resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        try:
            return self._resolve(ref)
        except RequestFailed as exc:
            return self._error(ref, f"URL not reachable: {exc.last_error}", exc.attempts)
        except _Failure as exc:
            return self._error(ref, str(exc), exc.attempts)
        except Exception as exc:  # monitors never raise
            log.exception("URL monitor failed for %s", ref.name)
            return self._error(ref, f"unexpected error: {type(exc).__name__}: {exc}", 1)

    @staticmethod
    def _error(ref: SourceRef, message: str, attempts: int) -> SourceResult:
        return SourceResult(ref=ref, classification=Classification.ERROR, errors=[message], attempts=attempts)

    def _get(self, url: str, accept: Optional[str] = None):
        headers = {"User-Agent": _USER_AGENT}
        if accept:
            headers["Accept"] = accept
        return request_with_retry("GET", url, retry=self.retry, headers=headers)

    @staticmethod
    def _body(resp) -> str:
        return resp.content.decode("utf-8", errors="replace")

    def _require_ok(self, resp, url: str):
        if not resp.ok:
            raise _Failure(f"HTTP {resp.status_code} at {url}")

    def _observe(self, url: str) -> _Snap:
        portal = _PORTAL_RE.match(url)
        if portal:
            return self._observe_portal(url, portal)
        if "swagger-ui" in urlparse(url).path:
            probe_notes: List[str] = []
            snap = self._probe_swagger(url, probe_notes)
            if snap:
                return snap
        else:
            probe_notes = []
        resp = self._get(url, "text/html,application/json;q=0.9,*/*;q=0.8")
        self._require_ok(resp, url)
        snap = self._snapshot_from_response(resp, url)
        snap.notes = (snap.notes or []) + probe_notes
        return snap

    def _probe_swagger(self, url: str, notes: List[str]) -> Optional[_Snap]:
        parsed = urlparse(url)
        idx = parsed.path.find("swagger-ui")
        base_path = parsed.path[: parsed.path.rfind("/", 0, idx) + 1]
        api_url = f"{parsed.scheme}://{parsed.netloc}{base_path}v3/api-docs"
        fallback = "falling back to the HTML page (no machine-readable version)"
        try:
            resp = self._get(api_url, "application/json")
            if not resp.ok:
                notes.append(f"swagger-ui: {api_url} not usable (HTTP {resp.status_code}); {fallback}")
                return None
            data = json.loads(self._body(resp))
        except RequestFailed as exc:
            notes.append(f"swagger-ui: {api_url} not reachable ({exc.last_error}); {fallback}")
            return None
        except ValueError:
            notes.append(f"swagger-ui: {api_url} is not valid JSON; {fallback}")
            return None
        if not _openapi_info(data):
            notes.append(f"swagger-ui: {api_url} is not an OpenAPI document with info.version; {fallback}")
            return None
        return _json_snapshot(data)

    def _observe_portal(self, url: str, match) -> _Snap:
        host, api_id, page_id = match.group(1), match.group(2), match.group(3)
        api_url = f"{host}/api/apis/{api_id}"
        resp = self._get(api_url, "application/json")
        self._require_ok(resp, api_url)
        try:
            data = json.loads(self._body(resp))
        except ValueError:
            raise _Failure(f"response of {api_url} is not valid JSON")
        snap = _json_snapshot(data)
        if page_id:
            page = self._get(url, "text/plain")
            if page.ok:
                return _make_snap(snap.version, _normalise_text(self._body(page)))
            raise _Failure(f"HTTP {page.status_code} at {url} (page content not readable)")
        return snap

    def _snapshot_from_response(self, resp, url: str) -> _Snap:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and not any(t in ctype for t in _TEXT_CT):
            return _Snap(version=None, text=None, fingerprint=hash_key(resp.content.hex()))
        body = self._body(resp)
        stripped = body.lstrip()
        if "json" in ctype:
            try:
                return _json_snapshot(json.loads(body))
            except ValueError:
                raise _Failure(f"response of {url} is declared as JSON but could not be parsed")
        if stripped[:1] in ("{", "["):
            try:
                return _json_snapshot(json.loads(body))
            except ValueError:
                pass
        if "html" in ctype or _HTMLISH_RE.search(stripped[:2000]):
            return _html_snapshot(body, url)
        return _plain_snapshot(body)

    def _resolve(self, ref: SourceRef) -> SourceResult:
        if not ref.url:
            return self._error(ref, "source has no URL", 1)
        snap = self._observe(ref.url)

        warnings: List[str] = list(snap.notes or [])
        if snap.truncated:
            warnings.append(
                f"content larger than {MAX_TEXT_CHARS // 1024} KB: the fingerprint covers all of it, "
                f"but only the first {MAX_TEXT_CHARS // 1024} KB are stored and diffed"
            )
        if ref.section:
            warnings.append(f"check {ref.section}")
        if ref.note:
            warnings.append(f"note: {ref.note}")

        if ref.baseline_version:
            if not normalise_version(ref.baseline_version):
                warnings.append(
                    f"stated baseline '{ref.baseline_version}' is not a version; compared against local snapshot instead"
                )
            elif snap.version is None:
                warnings.append(
                    f"stated baseline {ref.baseline_version} could not be checked: no version found on the page; "
                    "compared against local snapshot instead"
                )
            else:
                stated = VersionPoint(version=ref.baseline_version)
                found = VersionPoint(version=snap.version, url=ref.url)
                if versions_equal(ref.baseline_version, snap.version):
                    return SourceResult(ref=ref, classification=Classification.UNCHANGED, old=stated, new=found, warnings=warnings)
                warnings.append(_NO_HISTORY)
                return SourceResult(ref=ref, classification=Classification.CHANGED, old=stated, new=found, warnings=warnings)

        return self._compare_snapshot(ref, snap, warnings)

    def _label(self, snap: _Snap) -> str:
        return snap.version or f"content hash {snap.fingerprint[:8]}"

    def _save_baseline(self, ref: SourceRef, snap: _Snap, today: str, warnings: List[str]) -> None:
        data = {"version": snap.version, "text": snap.text, "fingerprint": snap.fingerprint, "observed": today,
                "truncated": snap.truncated}
        if self.defer_baselines:
            self._pending[hash_key(ref.url)] = {**data, "url": ref.url}
            return
        stored = self.baselines.put_json(BASELINE_NAMESPACE, hash_key(ref.url), data)
        if not stored:
            warnings.append("baseline snapshot could not be saved; next run will not compare against it")

    def _compare_snapshot(self, ref: SourceRef, snap: _Snap, warnings: List[str]) -> SourceResult:
        today = (self._today or date.today()).isoformat()
        current = VersionPoint(version=self._label(snap), url=ref.url)
        stored = self.baselines.get_json(BASELINE_NAMESPACE, hash_key(ref.url))
        baseline_file = self.baselines._path(BASELINE_NAMESPACE, hash_key(ref.url), ".json")
        if (not isinstance(stored, dict) or "fingerprint" not in stored) and baseline_file.exists():
            # Re-establishing silently would discard whatever changed since that snapshot.
            return self._error(
                ref,
                f"baseline snapshot {baseline_file} exists but is unreadable or corrupt; not overwritten, "
                "so changes since it was taken cannot be reported. Delete the file to start a new baseline.",
                1,
            )
        if not isinstance(stored, dict) or "fingerprint" not in stored:
            if self.dry_run:
                warnings.append(
                    f"no baseline yet ({self._label(snap)}); not recorded on a dry run"
                )
            else:
                self._save_baseline(ref, snap, today, warnings)
                warnings.append(
                    f"baseline established today ({self._label(snap)}); future runs compare against this snapshot"
                )
            return SourceResult(ref=ref, classification=Classification.NOT_MONITORED, new=current, warnings=warnings)

        old_version = stored.get("version")
        observed = stored.get("observed") or "unknown date"
        old_label = old_version or f"content hash {str(stored['fingerprint'])[:8]}"
        old_point = VersionPoint(
            version=old_label, url=ref.url, inferred=True,
            inferred_reason=f"snapshot of {observed} (local baseline)",
        )
        version_changed = bool(old_version and snap.version and not versions_equal(old_version, snap.version))
        if stored["fingerprint"] == snap.fingerprint and not version_changed:
            return SourceResult(ref=ref, classification=Classification.UNCHANGED, old=old_point, new=current, warnings=warnings)

        old_text, new_text = stored.get("text"), snap.text
        diff = None
        if isinstance(old_text, str) and new_text is not None:
            diff = diff_text(old_text, new_text)
        elif new_text is None:
            warnings.append("binary content: changed, but no text diff available")
        classification = Classification.CHANGED
        capped = snap.truncated or bool(stored.get("truncated"))
        if capped and not (diff or "").strip():
            warnings.append("the change lies beyond the stored first part of the content; no diff available")
        elif capped:
            warnings.append("content is capped, so a cosmetic-looking diff may hide a real change further down; reported as changed")
        elif not version_changed and diff is not None and is_cosmetic(diff):
            classification = Classification.COSMETIC_ONLY
        if classification == Classification.CHANGED and not self.dry_run:
            self._save_baseline(ref, snap, today, warnings)
        return SourceResult(
            ref=ref, classification=classification, old=old_point, new=current,
            old_text=old_text if isinstance(old_text, str) else None, new_text=new_text, diff_text=diff,
            warnings=warnings,
        )
