"""eCH standard monitor: old vs new *approved* version of an eCH standard.

Old = the version stated in the row, else the latest approved version at the
baseline date (row date -> spec Stand date -> spec creation date, inferred and
labelled). New = latest approved version today. The standard's content itself is
not diffed (PDFs); the result links both versions.
"""
import html as html_lib
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from digest.config import RetryConfig
from digest.spec_monitor.cache import SpecCache
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

APPROVED_STATUSES = {"genehmigt", "abgelöst"}   # a superseded version was approved once
CACHE_TTL_SECONDS = 6 * 3600
_USER_AGENT = {"User-Agent": "Mozilla/5.0 (spec-monitor)"}
_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
    "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}


class MonitorFailure(Exception):
    """A readable reason a source could not be resolved (becomes an ERROR row)."""

    def __init__(self, message: str, attempts: int = 1):
        super().__init__(message)
        self.attempts = attempts


def version_key(version: str) -> Tuple[int, ...]:
    nums = [int(n) for n in re.findall(r"\d+", version or "")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def ttl_get(cache: SpecCache, namespace: str, key: str, ttl: int = CACHE_TTL_SECONDS):
    entry = cache.get_json(namespace, key)
    if not isinstance(entry, dict) or "d" not in entry or not isinstance(entry.get("t"), (int, float)):
        return None
    if time.time() - entry["t"] > ttl:
        return None
    return entry["d"]


def ttl_put(cache: SpecCache, namespace: str, key: str, data) -> None:
    cache.put_json(namespace, key, {"t": time.time(), "d": data})


def baseline_day(ref: SourceRef, spec: SpecInfo) -> Optional[Tuple[date, str]]:
    if ref.baseline_date:
        return ref.baseline_date, f"from date {ref.baseline_date.isoformat()}"
    if spec.stand_date:
        return spec.stand_date, f"from Stand date {spec.stand_date.isoformat()} of the spec"
    if spec.created:
        d = spec.created.date()
        return d, f"from creation date {d.isoformat()} of the spec"
    return None


def resolve_pair(
    ref: SourceRef,
    spec: SpecInfo,
    approved: List[VersionPoint],
    listed: List[VersionPoint],
    today: date,
) -> Tuple[VersionPoint, VersionPoint]:
    """Pick (old, new) from approved versions (all carry `when` or None); raises MonitorFailure."""
    live = [v for v in approved if v.when is None or v.when.date() <= today]
    if not live:
        raise MonitorFailure("no approved version found")
    new = max(live, key=lambda v: version_key(v.version))

    if ref.baseline_version:
        wanted = version_key(ref.baseline_version)
        pool = approved + [v for v in listed if v.version not in {a.version for a in approved}]
        match = next((v for v in pool if version_key(v.version) == wanted), None)
        if match is None:
            known = ", ".join(v.version for v in listed) or "none"
            raise MonitorFailure(f"stated version {ref.baseline_version} is not listed (known versions: {known})")
        return match, new

    base = baseline_day(ref, spec)
    if base is None:
        raise MonitorFailure("no baseline version or date available (row, Stand date and creation date missing)")
    day, reason = base
    at_day = [v for v in approved if v.when is not None and v.when.date() <= day]
    if not at_day:
        raise MonitorFailure(f"no approved version existed on {day.isoformat()}")
    old = max(at_day, key=lambda v: version_key(v.version))
    return (
        VersionPoint(
            version=old.version, when=old.when, url=old.url, status=old.status,
            inferred=True, inferred_reason=reason,
        ),
        new,
    )


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]*(?:>|$)", " ", fragment))).strip()


def _parse_dt(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_german_date(text: str) -> Optional[datetime]:
    m = re.search(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})", text)
    if m and m.group(2).lower() in _MONTHS:
        return datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), 12)
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12)
        except ValueError:
            return None
    return None


def _field(page: str, label: str) -> Optional[str]:
    m = re.search(r'field__label">\s*' + re.escape(label) + r"\s*</div>(.*?)(?=field__label|\Z)", page, re.S)
    return m.group(1) if m else None


def _field_date(page: str, label: str) -> Optional[datetime]:
    segment = _field(page, label)
    if segment is None:
        return None
    m = re.search(r'<time[^>]*datetime="([^"]+)"', segment)
    if m:
        parsed = _parse_dt(m.group(1))
        if parsed:
            return parsed
    return _parse_german_date(_strip_tags(segment))


def parse_version_page(page: str) -> dict:
    """Status and approval date of one eCH version page.

    A superseded version shows the supersession decision as Beschlussdatum and
    its own approval as "Genehmigt am", so the latter wins when present.
    """
    status_segment = _field(page, "Status")
    when = _field_date(page, "Genehmigt am") or _field_date(page, "Beschlussdatum")
    return {
        "status": _strip_tags(status_segment) if status_segment is not None else None,
        "when": when.isoformat() if when else None,
    }


@dataclass
class _Listed:
    version: str
    url: str
    status: Optional[str]


def _ech_number(ref: SourceRef) -> Optional[str]:
    for text in (ref.url, ref.name, ref.title, ref.version_text, ref.note, ref.used_for):
        m = re.search(r"ech[-\s_]?(\d{4})", text or "", re.I)
        if m:
            return m.group(1)
    return None


class EchMonitor:
    def __init__(
        self,
        retry: RetryConfig,
        cache: SpecCache,
        base_url: str = "https://www.ech.ch",
        *,
        today: Optional[Callable[[], date]] = None,
    ):
        self.retry = retry
        self.cache = cache
        self.base_url = base_url.rstrip("/")
        self._today = today or date.today

    def handles(self, ref: SourceRef) -> bool:
        return ref.kind == SourceKind.ECH

    def resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        try:
            return self._resolve(ref, spec)
        except RequestFailed as exc:
            return self._error(ref, f"eCH website not reachable: {exc.last_error}", exc.attempts)
        except MonitorFailure as exc:
            return self._error(ref, str(exc), exc.attempts)
        except Exception as exc:  # monitors never raise
            log.exception("eCH monitor failed for %s", ref.name)
            return self._error(ref, f"unexpected error: {type(exc).__name__}: {exc}", 1)

    @staticmethod
    def _error(ref: SourceRef, message: str, attempts: int) -> SourceResult:
        return SourceResult(ref=ref, classification=Classification.ERROR, errors=[message], attempts=attempts)

    def _fetch(self, url: str):
        return request_with_retry("GET", url, retry=self.retry, headers=_USER_AGENT)

    def _list_versions(self, number: str, notes: Optional[List[str]] = None) -> List[_Listed]:
        list_url = f"{self.base_url}/de/ech/ech-{number}"
        resp = self._fetch(list_url)
        if not resp.ok:
            raise MonitorFailure(f"eCH-{number} version list not readable (HTTP {resp.status_code}) at {list_url}")
        page = resp.content.decode("utf-8", errors="replace")
        found: dict = {}
        skipped: List[str] = []
        pattern = r'<a\b[^>]*?href="([^"]*?/ech-%s/([0-9][0-9.]*))"[^>]*>(.*?)</a>' % number
        for m in re.finditer(pattern, page, re.S):
            href, version, inner = m.group(1), m.group(2).rstrip("."), _strip_tags(m.group(3))
            if version in found:
                continue
            if not inner.startswith(version):
                skipped.append(f"{version} ('{inner[:40]}')")
                continue
            rest = inner[len(version):].strip()
            sm = re.match(r"([^\W\d_][\w\- ]*?)(?=\s+\d|\s*$)", rest)
            url = href if href.startswith("http") else f"{self.base_url}{href}"
            found[version] = _Listed(version, url, sm.group(1).strip() if sm else None)
        if skipped and notes is not None:
            notes.append(
                f"{len(skipped)} version link(s) on the eCH-{number} list ignored because the link text does not "
                f"start with the version number (a newer version may be missing): {', '.join(skipped[:5])}"
            )
        if not found:
            raise MonitorFailure(f"no versions found on {list_url} (page layout changed?)")
        return list(found.values())

    def _version_info(self, number: str, item: _Listed) -> dict:
        key = f"ech-{number}-{item.version}"
        cached = ttl_get(self.cache, "ech", key)
        if cached is not None:
            return cached
        resp = self._fetch(item.url)
        if not resp.ok:
            raise MonitorFailure(f"eCH-{number} version {item.version} not readable (HTTP {resp.status_code}) at {item.url}")
        info = parse_version_page(resp.content.decode("utf-8", errors="replace"))
        if info.get("status") is not None:   # a missing status is a layout problem: never cache it
            ttl_put(self.cache, "ech", key, info)
        return info

    def _resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        number = _ech_number(ref)
        if not number:
            return SourceResult(
                ref=ref,
                classification=Classification.NOT_MONITORED,
                warnings=["eCH standard number not recognisable in the source row - check manually"],
            )

        notes: List[str] = []
        listed = self._list_versions(number, notes)
        approved: List[VersionPoint] = []
        known: List[VersionPoint] = []
        for item in listed:
            status = item.status
            info = None
            if status is None or status.casefold() in APPROVED_STATUSES:
                info = self._version_info(number, item)
                if info.get("status") is None and status is None:
                    notes.append(
                        f"version {item.version}: no status found on its page (page layout changed?); "
                        "treated as not approved"
                    )
                status = info.get("status") or status
            when = _parse_dt(info["when"]) if info and info.get("when") else None
            point = VersionPoint(version=item.version, when=when, url=item.url, status=status)
            known.append(point)
            if status and status.casefold() in APPROVED_STATUSES:
                approved.append(point)

        try:
            old, new = resolve_pair(ref, spec, approved, known, self._today())
        except MonitorFailure as exc:
            if notes:
                raise MonitorFailure(f"{exc} [{'; '.join(notes)}]", exc.attempts) from exc
            raise

        warnings: List[str] = list(notes)
        if not ref.baseline_version:
            undated = [v.version for v in approved if v.when is None]
            if undated:
                warnings.append(
                    f"approved version(s) {', '.join(undated)} have no parseable approval date and are ignored "
                    "when inferring the baseline version"
                )
        if ref.section:
            warnings.append(f"check {ref.section}")
        old_key, new_key = version_key(old.version), version_key(new.version)
        if old_key == new_key:
            classification = Classification.UNCHANGED
        elif old_key > new_key:
            classification = Classification.UNCHANGED
            warnings.append(f"stated version {old.version} is newer than the latest approved version {new.version}")
        else:
            classification = Classification.CHANGED
            warnings.append("content of the standard is not compared - see the linked versions")
        return SourceResult(ref=ref, classification=classification, old=old, new=new, warnings=warnings)
