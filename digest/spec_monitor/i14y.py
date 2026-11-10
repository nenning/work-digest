"""i14y code list monitor.

The public i14y API (GET <api>/api/public/v1/search?query=...) returns every
published version of a concept with `version`, `validFrom` and
`registrationStatus`, so code lists are monitored like eCH standards: old = the
stated version (else the latest released version at the baseline date), new =
the latest released version today. The list contents are not diffed.

A source is NOT_MONITORED (with a reason) when no usable search term can be
derived from the row, the code list is not found, or the name matches several
code lists equally well.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional

from digest.config import RetryConfig
from digest.spec_monitor.cache import SpecCache, hash_key
from digest.spec_monitor.ech import (
    MonitorFailure,
    resolve_pair,
    ttl_get,
    ttl_put,
    version_key,
)
from digest.spec_monitor.http import RequestFailed, request_with_retry
from digest.spec_monitor.models import (
    Classification,
    SourceKind,
    SourceRef,
    SourceResult,
    SpecInfo,
    VersionPoint,
)

log = logging.getLogger(__name__)

RELEASED_STATUSES = {"standard", "preferredstandard", "superseded"}
_STOPWORDS = {"i14y", "codeliste", "codelisten", "code", "liste", "der", "die", "das", "des", "von", "und", "version", "im", "in", "zur", "für"}
_USER_AGENT = {"User-Agent": "Mozilla/5.0 (spec-monitor)", "Accept": "application/json"}


def _search_term(ref: SourceRef) -> str:
    text = re.sub(r"\bv?\d+(?:\.\d+)+\b", " ", ref.name or "", flags=re.I)
    words = re.findall(r"[^\W\d_]\w*", text)
    return " ".join(w for w in words if w.lower() not in _STOPWORDS)


def _local_naive(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        return parsed
    try:
        from zoneinfo import ZoneInfo

        return parsed.astimezone(ZoneInfo("Europe/Zurich")).replace(tzinfo=None)
    except Exception:
        return parsed.astimezone(timezone(timedelta(hours=1))).replace(tzinfo=None)


def _title(entry: dict) -> str:
    title = entry.get("title")
    if isinstance(title, dict):
        return title.get("de") or title.get("en") or next(iter(title.values()), "") or ""
    return title or ""


def _overlap(term: str, title: str) -> int:
    score = 0
    title_words = [w.lower() for w in re.findall(r"\w+", title)]
    for word in re.findall(r"\w+", term.lower()):
        if len(word) >= 4 and any(word.startswith(t) or t.startswith(word) for t in title_words if len(t) >= 4):
            score += 1
    return score


class I14yMonitor:
    def __init__(
        self,
        retry: RetryConfig,
        cache: SpecCache,
        api_url: str = "https://api.i14y.admin.ch",
        ui_url: str = "https://www.i14y.admin.ch",
        *,
        today: Optional[Callable[[], date]] = None,
    ):
        self.retry = retry
        self.cache = cache
        self.api_url = api_url.rstrip("/")
        self.ui_url = ui_url.rstrip("/")
        self._today = today or date.today

    def handles(self, ref: SourceRef) -> bool:
        return ref.kind == SourceKind.I14Y

    def resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        try:
            return self._resolve(ref, spec)
        except RequestFailed as exc:
            return self._error(ref, f"i14y API not reachable: {exc.last_error}", exc.attempts)
        except MonitorFailure as exc:
            return self._error(ref, str(exc), exc.attempts)
        except Exception as exc:  # monitors never raise
            log.exception("i14y monitor failed for %s", ref.name)
            return self._error(ref, f"unexpected error: {type(exc).__name__}: {exc}", 1)

    @staticmethod
    def _error(ref: SourceRef, message: str, attempts: int) -> SourceResult:
        return SourceResult(ref=ref, classification=Classification.ERROR, errors=[message], attempts=attempts)

    @staticmethod
    def _not_monitored(ref: SourceRef, reason: str) -> SourceResult:
        return SourceResult(ref=ref, classification=Classification.NOT_MONITORED, warnings=[reason])

    def _search(self, term: str, notes: Optional[List[str]] = None) -> List[dict]:
        key = hash_key("i14y-search", term.lower())
        cached = ttl_get(self.cache, "i14y", key)
        if cached is not None:
            return cached
        url = f"{self.api_url}/api/public/v1/search"
        resp = request_with_retry(
            "GET", url, retry=self.retry, headers=_USER_AGENT, params={"query": term}
        )
        if not resp.ok:
            raise MonitorFailure(f"i14y search for '{term}' failed (HTTP {resp.status_code})")
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise MonitorFailure(f"i14y search for '{term}' returned an unexpected payload")
        total = next(
            (payload[k] for k in ("totalCount", "total", "totalItems", "totalResults")
             if isinstance(payload.get(k), int) and not isinstance(payload.get(k), bool)),
            None,
        )
        incomplete = total is not None and total > len(data)
        if incomplete and notes is not None:
            notes.append(
                f"i14y search for '{term}' returned {len(data)} of {total} hits (paged); "
                "the code list or a newer version may be missing from the result"
            )
        compact = [
            {
                "id": e.get("id"),
                "identifier": e.get("identifier"),
                "title": _title(e),
                "version": e.get("version"),
                "status": e.get("registrationStatus"),
                "valid_from": e.get("validFrom"),
            }
            for e in data
            if isinstance(e, dict) and e.get("type") == "Concept" and e.get("conceptType") == "CodeList" and e.get("version")
        ]
        if not incomplete:   # never cache a partial result as if it were complete
            ttl_put(self.cache, "i14y", key, compact)
        return compact

    def _resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        term = _search_term(ref)
        if not term:
            return self._not_monitored(ref, f"no code list name recognisable in '{ref.name}' - check manually")

        notes: List[str] = []
        entries = self._search(term, notes)
        groups: dict = {}
        for e in entries:
            groups.setdefault(e.get("identifier"), []).append(e)
        if not groups:
            return self._not_monitored(ref, " ".join([f"code list '{term}' not found on i14y - check manually"] + notes))

        if len(groups) == 1:
            chosen = next(iter(groups.values()))
            if _overlap(term, chosen[0]["title"]) == 0:
                notes.append(
                    f"only one i14y hit for '{term}' and its title '{chosen[0]['title']}' shares no word with it; "
                    "verify that it is the right code list"
                )
        else:
            scored = sorted(groups.values(), key=lambda g: _overlap(term, g[0]["title"]), reverse=True)
            best, second = _overlap(term, scored[0][0]["title"]), _overlap(term, scored[1][0]["title"])
            if best == 0 or best == second:
                names = ", ".join(sorted({g[0]["title"] or str(g[0]["identifier"]) for g in groups.values()}))
                return self._not_monitored(ref, " ".join([f"ambiguous: '{term}' matches several i14y code lists ({names}) - check manually"] + notes))
            chosen = scored[0]

        listed: List[VersionPoint] = []
        approved: List[VersionPoint] = []
        for e in chosen:
            point = VersionPoint(
                version=str(e["version"]),
                when=_local_naive(e.get("valid_from") or ""),
                url=f"{self.ui_url}/de/concepts/{e['id']}/description" if e.get("id") else None,
                status=e.get("status"),
            )
            listed.append(point)
            if (e.get("status") or "").casefold() in RELEASED_STATUSES:
                approved.append(point)

        try:
            old, new = resolve_pair(ref, spec, approved, listed, self._today())
        except MonitorFailure as exc:
            if notes:
                raise MonitorFailure(f"{exc} [{'; '.join(notes)}]", exc.attempts) from exc
            raise

        warnings: List[str] = list(notes)
        if ref.section:
            warnings.append(f"check {ref.section}")
        old_key, new_key = version_key(old.version), version_key(new.version)
        if old_key == new_key:
            classification = Classification.UNCHANGED
        elif old_key > new_key:
            classification = Classification.UNCHANGED
            warnings.append(f"stated version {old.version} is newer than the latest released version {new.version}")
        else:
            classification = Classification.CHANGED
            warnings.append("code list entries are not compared - see the linked versions")
        return SourceResult(ref=ref, classification=classification, old=old, new=new, warnings=warnings)
