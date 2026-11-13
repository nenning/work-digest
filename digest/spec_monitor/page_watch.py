"""--page-watch: what changed on a Confluence page tree in the last N days (no baseline, no state)."""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from digest.auth.atlassian import get_auth_header
from digest.config import Config, effective_vision_llm, spec_llm
from digest.spec_monitor import analysis, textdiff, watch_analysis
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.confluence_client import ConfluenceClient, NoStorageBody, parse_when, pick_version_at
from digest.spec_monitor.confluence_monitor import ConfluenceMonitor
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import (
    Classification, PageChange, PageStatus, PageWatchReport, ReportError, SourceKind, SourceRef, SourceResult,
    SpecInfo, VersionPoint,
)
from digest.spec_monitor.runner import SpecMonitorFatal

log = logging.getLogger(__name__)

_CLASSIFICATION_TO_STATUS = {
    Classification.CHANGED: PageStatus.CHANGED,
    Classification.COSMETIC_ONLY: PageStatus.COSMETIC_ONLY,
    Classification.UNCHANGED: PageStatus.UNCHANGED,
    Classification.ERROR: PageStatus.ERROR,
}


def baseline_day(window_days: int, today: Optional[date] = None) -> date:
    """Last day before the window: a page's old state is its last version at or before the end of this day."""
    return (today or date.today()) - timedelta(days=window_days)


def window_label(start: date, today: date, window_days: int) -> str:
    return f"{start.isoformat()} to {today.isoformat()} ({window_days} days, today included)"


def _end_of_day(day: date) -> datetime:
    return datetime.combine(day, time.max).astimezone()


class _PageWatch:
    def __init__(self, config: Config, client: ConfluenceClient, monitor, cache: SpecCache, parent_title: str,
                 parent_id: str, bday: date, progress: Callable[[str], None]):
        self.config = config
        self.client = client
        self.monitor = monitor
        self.cache = cache
        self.parent_title = parent_title
        self.parent_id = parent_id
        self.bday = bday
        self.progress = progress
        self.llm_summary = spec_llm(config, "summary")

    def process(self, entry: dict) -> PageChange:
        pid, title = entry["id"], entry["title"]
        change = PageChange(page_id=pid, title=title, url=self.client.page_url(pid), status=PageStatus.UNCHANGED)
        try:
            when = parse_when(entry.get("when"))
            if when is not None and when <= _end_of_day(self.bday):
                return change
            self._locate(change)
            versions = self.client.list_versions(pid)
            if versions and any(v.when is None for v in versions):
                raise LookupError("version dates are missing in the page history; the window cannot be applied")
            if versions and pick_version_at(versions, self.bday) is None:
                self._new_page(change, versions[-1])
            else:
                self._changed_page(change, entry)
        except RequestFailed as exc:
            self._fail(change, f"{exc.last_error} (after {exc.attempts} attempt(s))")
        except LookupError as exc:
            self._fail(change, str(exc))
        except Exception as exc:  # noqa: BLE001 - one page must never fail the run
            log.exception("page watch failed for %s", title)
            self._fail(change, f"unexpected error {type(exc).__name__}: {exc}")
        return change

    @staticmethod
    def _fail(change: PageChange, message: str) -> None:
        change.status = PageStatus.ERROR
        change.errors.append(message)

    def _locate(self, change: PageChange) -> None:
        try:
            page = self.client.get_page(change.page_id, with_body=False)
        except (RequestFailed, LookupError) as exc:
            change.warnings.append(f"location in the page tree unavailable ({exc})")
            return
        titles, ids = [], []
        for a in page.get("ancestors") or []:
            ids.append(str(a.get("id")))
            titles.append(a.get("title") or "")
        if self.parent_id in ids:
            titles = titles[ids.index(self.parent_id) + 1:]
            change.path = [self.parent_title] + titles
        else:
            change.path = titles

    def _new_page(self, change: PageChange, newest: VersionPoint) -> None:
        pid = change.page_id
        ref = SourceRef(name=change.title, kind=SourceKind.CONFLUENCE, title=change.title, page_id=pid,
                        url=change.url)
        result = SourceResult(ref=ref, classification=Classification.CHANGED, old=None, new=newest)
        try:
            text = textdiff.storage_to_text(self.client.get_body(pid, int(newest.version)))
            result.new_text = text
            result.diff_text = textdiff.diff_text("", text)
        except NoStorageBody:
            result.warnings.append("content type has no storage body; the new page's content is not compared")
        change.status = PageStatus.NEW
        change.result = result
        change.warnings.extend(result.warnings)
        self._summarise(change)

    def _changed_page(self, change: PageChange, entry: dict) -> None:
        ref = SourceRef(name=change.title, kind=SourceKind.CONFLUENCE, title=change.title, page_id=change.page_id,
                        url=change.url, baseline_date=self.bday)
        spec = SpecInfo(page_id=change.page_id, title=change.title, url=change.url, status="",
                        ancestor_titles=list(change.path))
        result = self.monitor.resolve(ref, spec)
        change.result = result
        change.status = _CLASSIFICATION_TO_STATUS.get(result.classification, PageStatus.ERROR)
        change.warnings.extend(result.warnings)
        if result.classification == Classification.ERROR:
            change.errors.extend(result.errors or ["error"])
            return
        if change.status == PageStatus.CHANGED:
            self._summarise(change)

    def _summarise(self, change: PageChange) -> None:
        result = change.result
        if not result.diff_text and not result.diagram_notes:
            return
        try:
            summary = analysis.summarize_source_diff(result, self.llm_summary, self.config.language, self.cache,
                                                     page_mode=True)
        except AnalysisError as exc:
            change.warnings.append(f"diff summary unavailable: {exc}")
            change.summary_failed = str(exc)
            return
        change.summary = summary
        if not summary and not result.diagram_notes:
            change.status = PageStatus.COSMETIC_ONLY
            result.classification = Classification.COSMETIC_ONLY
            result.diff_text = ""
            change.warnings.append(
                "Änderung erkannt, vom Modell aber als kosmetisch eingestuft und nicht gemeldet, bitte prüfen")


def run_page_watch(
    config: Config,
    *,
    days: Optional[int] = None,
    command_line: str = "",
    progress: Callable[[str], None] = print,
    client: Optional[ConfluenceClient] = None,
    monitor=None,
    cache_dir: Optional[Path] = None,
    today: Optional[date] = None,
) -> PageWatchReport:
    pw = config.page_watch
    if pw is None:
        raise SpecMonitorFatal("No page_watch block configured in config.yaml")
    window_days = days if days is not None else pw.window_days
    if window_days < 1:
        raise SpecMonitorFatal(f"the window must be at least 1 day, got {window_days}")
    today = today or date.today()
    bday = baseline_day(window_days, today)

    cache = SpecCache(cache_dir or (config.data_dir / "spec_cache"))
    if client is None:
        client = ConfluenceClient(config.atlassian, get_auth_header(config.atlassian), pw.retry, cache)
    if monitor is None:
        monitor = ConfluenceMonitor(client, vision=effective_vision_llm(config), language=config.language, cache=cache)

    try:
        parent = client.get_page(pw.parent_page_id, with_body=False)
        descendants = client.search_descendants(pw.parent_page_id, pw.label)
    except (RequestFailed, LookupError) as exc:
        reason = exc.last_error if isinstance(exc, RequestFailed) else str(exc)
        raise SpecMonitorFatal(f"The page tree below page {pw.parent_page_id} could not be read: {reason}") from exc

    parent_title = parent.get("title") or pw.parent_page_id
    entries: List[dict] = []
    if pw.include_parent:
        entries.append({"id": str(pw.parent_page_id), "title": parent_title,
                        "when": (parent.get("version") or {}).get("when")})
    entries.extend(sorted((e for e in descendants if e["id"] != str(pw.parent_page_id)), key=lambda e: e["title"]))

    report = PageWatchReport(
        generated_at=datetime.now().astimezone(), parent_title=parent_title,
        parent_url=client.page_url(str(pw.parent_page_id)), window_days=window_days,
        window_start=bday + timedelta(days=1), command_line=command_line, language=config.language,
    )
    if not entries:
        report.errors.append(ReportError(
            scope=f"page {pw.parent_page_id}", message="no page to watch (no descendants found and the parent is excluded)",
            impact="nothing was checked; 'no changes' is meaningless -- check parent_page_id, label and include_parent"))
    progress(f"{len(entries)} page(s) to check, window {window_label(report.window_start, today, window_days)}")

    watch = _PageWatch(config, client, monitor, cache, parent_title, str(pw.parent_page_id), bday, progress)
    if pw.workers > 1 and len(entries) > 1:
        with ThreadPoolExecutor(max_workers=pw.workers) as pool:
            report.pages = list(pool.map(watch.process, entries))
    else:
        report.pages = [watch.process(e) for e in entries]

    for page in report.pages:
        for message in page.errors:
            report.errors.append(ReportError(
                scope=page.title, message=message,
                impact="this page was not (fully) checked; changes on it may be missing from the report"))
        if page.summary_failed:
            report.analysis_incomplete = True
            report.errors.append(ReportError(
                scope=page.title, message=f"diff summary failed: {page.summary_failed}",
                impact="the change is detected but not summarised; the overall analysis uses the raw diff"))

    changed = report.with_status(PageStatus.CHANGED, PageStatus.NEW)
    progress(f"{len(changed)} changed or new page(s)")
    if changed:
        try:
            report.synthesis = watch_analysis.synthesize_watch(
                changed, window_label(report.window_start, today, window_days), parent_title,
                spec_llm(config, "heavy"), config.language, cache)
        except AnalysisError as exc:
            report.analysis_incomplete = True
            report.errors.append(ReportError(
                scope=parent_title, message=f"overall summary / change request / delta spec generation failed: {exc}",
                impact="the changes per page are shown, but no overall summary, change request, delta spec or effort"))

    report.cache_hits = cache.hits
    report.cache_misses = cache.misses
    report.cache_corrupt = cache.corrupt
    report.cache_write_errors = cache.write_errors
    report.cache_write_error = cache.first_write_error or ""
    return report
