"""Orchestration of a spec-monitor run (discovery -> sources -> monitors -> analysis -> RunReport)."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from digest.auth.atlassian import get_auth_header
from digest.config import Config, effective_vision_llm, spec_llm
from digest.spec_monitor import analysis
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.confluence_client import ConfluenceClient
from digest.spec_monitor.confluence_monitor import ConfluenceMonitor
from digest.spec_monitor.discovery import discover_specs
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import (
    Classification, ReportError, RunReport, SourceKind, SourceRef, SourceResult, SpecAnalysis, SpecInfo,
)
from digest.spec_monitor.parser import find_sources_sentence, parse_sources

log = logging.getLogger(__name__)


class SpecMonitorFatal(RuntimeError):
    """The run cannot produce a useful report (invalid config, specs cannot be discovered)."""


def _build_monitors(config: Config, cache: SpecCache, client: ConfluenceClient, dry_run: bool = False,
                    defer_baselines: bool = False) -> list:
    from digest.spec_monitor.ech import EchMonitor
    from digest.spec_monitor.i14y import I14yMonitor
    from digest.spec_monitor.url_monitor import UrlMonitor

    retry = config.spec_monitor.retry
    monitors = [
        ConfluenceMonitor(client, vision=effective_vision_llm(config), language=config.language, cache=cache),
        EchMonitor(retry, cache), I14yMonitor(retry, cache), UrlMonitor(retry, cache, dry_run=dry_run, defer_baselines=defer_baselines),
    ]
    if config.gitlab:
        from digest.spec_monitor.gitlab import GitLabMonitor
        monitors.append(GitLabMonitor(config.gitlab, retry, cache))
    return monitors


def _monitor_locks(monitors: list) -> Dict[int, threading.Lock]:
    """One lock per monitor instance except the Confluence monitor: the others keep unsynchronised state
    (probe caches, snapshots), so their lookups are serialised even when sources are resolved in parallel."""
    return {id(m): threading.Lock() for m in monitors if not isinstance(m, ConfluenceMonitor)}


def _map(pool: Optional[ThreadPoolExecutor], fn: Callable, items: list) -> list:
    """Results in input order; strictly sequential without a pool."""
    if pool is None:
        return [fn(item) for item in items]
    return list(pool.map(fn, items))


def _resolve_ref(ref: SourceRef, spec: SpecInfo, monitors: list,
                 locks: Optional[Dict[int, threading.Lock]] = None) -> SourceResult:
    if ref.excluded_reason:
        return SourceResult(ref=ref, classification=Classification.EXCLUDED, warnings=[ref.excluded_reason])
    if ref.parse_issue:
        return SourceResult(ref=ref, classification=Classification.ERROR, errors=[ref.parse_issue])
    if ref.kind == SourceKind.EXTERNAL:
        reason = "external source without a readable version: check manually"
        if ref.note:
            reason += f" ({ref.note})"
        return SourceResult(ref=ref, classification=Classification.NOT_MONITORED, warnings=[reason])
    for monitor in monitors:
        if monitor.handles(ref):
            lock = (locks or {}).get(id(monitor))
            try:
                if lock is None:
                    return monitor.resolve(ref, spec)
                with lock:
                    return monitor.resolve(ref, spec)
            except Exception as exc:
                log.exception("monitor %s failed for %s", type(monitor).__name__, ref.name)
                return SourceResult(ref=ref, classification=Classification.ERROR,
                                    errors=[f"{type(exc).__name__}: {exc}"])
    return SourceResult(ref=ref, classification=Classification.NOT_MONITORED,
                        warnings=[f"no monitor for source kind {ref.kind.value}"])


def _analyse_spec(
    spec: SpecInfo, config: Config, cache: SpecCache, monitors: list, progress: Callable[[str], None],
    pool: Optional[ThreadPoolExecutor] = None, locks: Optional[Dict[int, threading.Lock]] = None,
) -> SpecAnalysis:
    sm = config.spec_monitor
    llm = spec_llm(config)
    summary_llm = spec_llm(config, "summary")
    out = SpecAnalysis(spec=spec)

    parsed = parse_sources(
        spec.body_storage, stand_date=spec.stand_date,
        excluded_markers=sm.excluded_source_markers, gitlab=config.gitlab,
        ancestor_titles=spec.ancestor_titles, ancestor_ids=spec.ancestor_ids,
    )
    refs = list(parsed.refs)
    issues = list(parsed.issues)
    out.gap_notes = list(parsed.gap_notes)

    if not parsed.found:
        sentence = find_sources_sentence(spec.body_storage)
        if sentence:
            try:
                refs = analysis.extract_sources_with_llm(sentence, llm, config.language)
                issues.append("sources taken from a free-text sentence (LLM extraction, lower confidence)")
            except AnalysisError as exc:
                out.errors.append(ReportError(
                    scope=spec.title, message=f"source extraction failed: {exc}",
                    impact="no sources could be checked for this spec"))
        if not refs:
            issues.append("no sources found on the page")

    progress(f"  {spec.title}: {len(refs)} source(s)")
    out.sources.extend(_map(pool, lambda ref: _resolve_ref(ref, spec, monitors, locks), refs))

    def summarise(result: SourceResult):
        try:
            return analysis.summarize_source_diff(result, summary_llm, config.language, cache), None
        except AnalysisError as exc:
            return None, exc

    demoted: List[str] = []
    synthesis_notes: List[str] = []
    to_summarise = [r for r in out.sources if r.classification == Classification.CHANGED and r.diff_text]
    for result, (summary, exc) in zip(to_summarise, _map(pool, summarise, to_summarise)):
        if exc is None:
            result.diff_summary = summary
            if not summary and not result.diagram_notes:
                # the model judged the diff cosmetic: same treatment as ConfluenceMonitor's own cosmetic check
                result.classification = Classification.COSMETIC_ONLY
                result.diff_text = ""
                demoted.append(result.ref.name)
            continue
        out.analysis_incomplete = True
        result.warnings.append(f"diff summary unavailable: {exc}")
        out.errors.append(ReportError(
            scope=f"{spec.title}: {result.ref.name}", message=f"diff summary failed: {exc}",
            impact="the raw change is detected but not summarised"))

    if any(r.classification == Classification.CHANGED for r in out.sources):
        try:
            synthesis = analysis.synthesize_spec(spec, out.sources, llm, config.language, cache)
            out.change_request = synthesis.change_request
            out.delta_changes = synthesis.delta_changes
            out.open_points = synthesis.open_points
            out.effort = synthesis.effort
            synthesis_notes = list(synthesis.warnings)
        except AnalysisError as exc:
            out.analysis_incomplete = True
            out.errors.append(ReportError(
                scope=spec.title, message=f"change request / delta spec generation failed: {exc}",
                impact="source facts are shown, but no change request or delta spec was generated"))

    out.completeness_warnings = analysis.build_completeness_warnings(out.gap_notes, out.sources, issues)
    out.completeness_warnings.extend(
        f"Quelle {n}: Änderung erkannt, vom Modell aber als kosmetisch eingestuft und nicht gemeldet, bitte prüfen"
        for n in demoted
    )
    out.completeness_warnings.extend(w for w in synthesis_notes if w not in out.completeness_warnings)
    return out


def run_spec_monitor(
    config: Config,
    *,
    only: Optional[str] = None,
    command_line: str = "",
    progress: Callable[[str], None] = print,
    client: Optional[ConfluenceClient] = None,
    monitors: Optional[list] = None,
    cache_dir: Optional[Path] = None,
    dry_run: bool = False,
    defer_baselines: bool = False,
) -> RunReport:
    sm = config.spec_monitor
    if sm is None:
        raise SpecMonitorFatal("No spec_monitor block configured in config.yaml")

    cache = SpecCache(cache_dir or (config.data_dir / "spec_cache"))
    if client is None:
        client = ConfluenceClient(config.atlassian, get_auth_header(config.atlassian), sm.retry, cache)
    if monitors is None:
        monitors = _build_monitors(config, cache, client, dry_run, defer_baselines)

    try:
        discovery = discover_specs(client, sm, only)
    except (RequestFailed, LookupError, ValueError) as exc:
        raise SpecMonitorFatal(f"Specs could not be discovered below page {sm.parent_page_id}: {exc}") from exc

    report = RunReport(
        generated_at=datetime.now().astimezone(),
        excluded_pages=list(discovery.excluded),
        errors=[
            ReportError(scope=title, message=f"page could not be read: {reason}",
                        impact="this page was not checked; it may be a spec that is now unmonitored")
            for _pid, title, reason in discovery.unreadable
        ] + list(discovery.problems),
        command_line=command_line,
        language=config.language,
    )
    progress(f"{len(discovery.specs)} spec(s) found, {len(discovery.excluded)} page(s) excluded")

    locks = _monitor_locks(monitors)
    pool = ThreadPoolExecutor(max_workers=sm.workers) if sm.workers > 1 else None
    try:
        for spec in discovery.specs:
            if not sm.is_analysed(spec.status):
                report.listed_only.append(spec)
                continue
            try:
                report.analysed.append(_analyse_spec(spec, config, cache, monitors, progress, pool, locks))
            except Exception as exc:
                log.exception("analysis of %s failed", spec.title)
                report.analysed.append(SpecAnalysis(
                    spec=spec, analysis_incomplete=True,
                    errors=[ReportError(scope=spec.title, message=f"{type(exc).__name__}: {exc}",
                                        impact="this spec was not analysed")],
                ))
    finally:
        if pool is not None:
            pool.shutdown()

    if defer_baselines:
        committers = [m.commit_baselines for m in monitors if hasattr(m, "commit_baselines")]
        report.commit_baselines = lambda: [msg for commit in committers for msg in commit()]
    report.cache_hits = cache.hits
    report.cache_misses = cache.misses
    report.cache_corrupt = cache.corrupt
    report.cache_write_errors = cache.write_errors
    report.cache_write_error = cache.first_write_error or ""
    return report
