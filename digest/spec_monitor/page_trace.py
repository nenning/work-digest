"""--page-watch trace pass: is a changed page's content already a CR / in progress? Search terms from the
change, candidates from Jira / source code, a judge that may only pick from the candidates."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Callable, List, Optional, Tuple

from digest import summarizer
from digest.config import Config, LLMConfig, spec_llm
from digest.spec_monitor import analysis
from digest.spec_monitor.analysis import AnalysisError, _DATA_NOTE, _quote
from digest.spec_monitor.cache import SpecCache, hash_key
from digest.spec_monitor.models import (
    DeltaChange, PageChange, PageStatus, PageTrace, PageWatchReport, ReportError, TraceCandidate, TraceReference,
    TraceSourceError,
)
from digest.spec_monitor.page_watch import is_frozen
from digest.spec_monitor.trace_code import search_code
from digest.spec_monitor.trace_jira import search_issues

log = logging.getLogger(__name__)

_PROMPT_VERSION = "1"
_MAX_TERMS = 8
_MAX_REFERENCES = 3
_MAX_TOKENS = 1500
_DIFF_FALLBACK_CHARS = 4000
_VERDICTS = ("in_progress", "done_likely", "related")
_KINDS = ("jira", "mr", "branch", "code")
_INCOMPLETE_IMPACT = ("the check for existing Jira tickets / code is incomplete for this page; "
                      "absence of traces is not established")


def _change_block(change: PageChange, deltas: List[DeltaChange]) -> str:
    lines = [f"Page: {change.title}"]
    if change.summary:
        lines.append(f"Change summary: {change.summary}")
    for d in deltas:
        ids = f" [{', '.join(d.ids)}]" if d.ids else ""
        lines.append(f"- {d.kind}{ids} {d.affected_part}: {d.change}")
    if not change.summary and not deltas and change.result is not None and change.result.diff_text:
        lines.append("Diff (- removed, + added):\n" + change.result.diff_text[:_DIFF_FALLBACK_CHARS])
    return "\n".join(lines)


def _terms_prompt(change: PageChange, deltas: List[DeltaChange]) -> str:
    return (
        "Derive search terms from the specification change below. The terms are used to look for existing Jira "
        "tickets and source code that already deal with this change.\n"
        f"Give at most {_MAX_TERMS} terms: requirement / ticket / decision IDs verbatim (e.g. CM-FR-03), domain "
        "nouns, identifier-style names in code spelling (e.g. requestInProgress), German and English variants of "
        "the key concepts. No generic words (e.g. 'change', 'page', 'specification', 'Anforderung').\n"
        f"{_DATA_NOTE}\n\n"
        + _quote("page change", _change_block(change, deltas))
        + '\n\nRespond with JSON only: {"terms": ["...", "..."]}'
    )


def _clean_terms(raw) -> List[str]:
    seen, out = set(), []
    for t in raw:
        term = " ".join(str(t).split())
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)
    return out[:_MAX_TERMS]


def _parse_terms(data) -> List[str]:
    if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
        raise ValueError("missing terms list")
    return _clean_terms(data["terms"])


def _cached_json(cache: SpecCache, namespace: str, key: str, prompt: str, llm: LLMConfig, parse: Callable,
                 purpose: str):
    cached = cache.get_text(namespace, key)
    if cached is not None:
        try:
            return parse(json.loads(cached))
        except (ValueError, TypeError, KeyError):
            pass
    value, raw = analysis._ask_json(prompt, llm, llm.llm_timeout, _MAX_TOKENS, parse, purpose)
    cache.put_text(namespace, key, raw)
    return value


def extract_terms(change: PageChange, deltas: List[DeltaChange], llm: LLMConfig, language: str,
                  cache: SpecCache) -> List[str]:
    prompt = _terms_prompt(change, deltas)
    key = hash_key(_PROMPT_VERSION, analysis._model_key(llm), prompt)
    return _cached_json(cache, "llm_trace_terms", key, prompt, llm, _parse_terms, "trace_terms")


def _judge_prompt(change: PageChange, deltas: List[DeltaChange], candidates: List[TraceCandidate],
                  language: str) -> str:
    lang = summarizer._language_name(language)
    rows = []
    for c in candidates:
        extra = ", ".join(x for x in (c.status, c.detail) if x)
        own = " [own delta ticket created by this tool for an earlier report]" if c.own else ""
        rows.append(f"- kind={c.kind} ref={c.ref} label={c.label}" + (f" ({extra})" if extra else "") + own)
    return (
        "Below is a specification change and a list of candidates found by a text search in Jira and in the "
        "source code. Decide which candidates are traces that this change is already being handled (a ticket / "
        "merge request / branch for it) or already implemented, or are merely related.\n"
        f"Choose at most {_MAX_REFERENCES} candidates. Never invent references: only choose refs from the list, "
        "copied exactly. Answer with an empty list when no candidate is relevant. These are traces, not proof; "
        "keep the reasons hedged.\n"
        'Verdicts: "in_progress" (ticket / MR / branch for exactly this change), "done_likely" (looks implemented '
        'already), "related" (same area, not clearly this change).\n'
        f"Each reason: one short line in {lang}.\n{_DATA_NOTE}\n\n"
        + _quote("page change", _change_block(change, deltas))
        + "\n\n" + _quote("candidates", "\n".join(rows))
        + '\n\nRespond with JSON only: {"references": [{"ref": "...", "verdict": "in_progress|done_likely|related", '
        '"reason": "..."}]}'
    )


def _parse_judge(data) -> List[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("references"), list):
        raise ValueError("missing references list")
    out = []
    for r in data["references"]:
        if not isinstance(r, dict) or not str(r.get("ref", "")).strip():
            raise ValueError("reference without ref")
        verdict = str(r.get("verdict", "")).strip()
        out.append({"ref": str(r["ref"]).strip(), "verdict": verdict if verdict in _VERDICTS else "related",
                    "reason": " ".join(str(r.get("reason", "") or "").split())})
    return out


def judge(change: PageChange, deltas: List[DeltaChange], candidates: List[TraceCandidate], llm: LLMConfig,
          language: str, cache: SpecCache) -> Tuple[List[TraceReference], List[str]]:
    prompt = _judge_prompt(change, deltas, candidates, language)
    key = hash_key(_PROMPT_VERSION, analysis._model_key(llm), prompt)
    chosen = _cached_json(cache, "llm_trace_judge", key, prompt, llm, _parse_judge, "trace_judge")
    by_ref = {}
    for c in candidates:
        by_ref.setdefault(c.ref, c)
    refs, problems, seen = [], [], set()
    for r in chosen:
        cand = by_ref.get(r["ref"])
        if cand is None:
            problems.append(f"judge returned unknown reference {r['ref']}")
            continue
        if cand.ref in seen:
            continue
        seen.add(cand.ref)
        refs.append(TraceReference(
            kind=cand.kind, ref=cand.ref, label=cand.label, url=cand.url,
            verdict="related" if cand.own else r["verdict"], reason=r["reason"], own=cand.own))
    refs.sort(key=lambda x: (_VERDICTS.index(x.verdict), _KINDS.index(x.kind) if x.kind in _KINDS else len(_KINDS)))
    return refs[:_MAX_REFERENCES], problems


def trace_page(change: PageChange, deltas: List[DeltaChange], *, config: Config, llm: LLMConfig, cache: SpecCache,
               auth_header: str, today: date, window_end: Optional[date]) -> PageTrace:
    pw = config.page_watch
    if is_frozen(change, pw.freeze_label):
        return PageTrace("FROZEN")
    trace = PageTrace("INCOMPLETE")
    if change.labels is None:
        trace.problems.append("labels unreadable, freeze state unknown")
    try:
        terms = extract_terms(change, deltas, llm, config.language, cache)
    except AnalysisError as exc:
        trace.problems.append(f"search terms could not be derived: {exc}")
        return trace
    if not terms:
        trace.problems.append("no search terms could be derived")
        return trace

    tcfg = pw.trace
    end = window_end or today
    candidates: List[TraceCandidate] = []
    try:
        candidates.extend(search_issues(config.atlassian, auth_header, pw.retry, terms, tcfg.jira_projects,
                                        end - timedelta(days=tcfg.lookback_days)))
        trace.searched.append(f"Jira {', '.join(tcfg.jira_projects)}: updated within {tcfg.lookback_days} days "
                              f"before {end.isoformat()}")
    except TraceSourceError as exc:
        trace.problems.append(str(exc))

    names = {n.casefold() for n in tcfg.repositories}
    repos = [r for r in (config.gitlab.repositories if config.gitlab else []) if r.name.casefold() in names]
    if repos:
        code = search_code(config.gitlab, repos, pw.retry, terms)
        candidates.extend(code.candidates)
        trace.searched.extend(code.searched)
        trace.problems.extend(code.problems)
    else:
        trace.searched.append("source code: no repository configured")

    if candidates:
        try:
            trace.references, problems = judge(change, deltas, candidates, llm, config.language, cache)
            trace.problems.extend(problems)
        except AnalysisError as exc:
            trace.problems.append(f"judging the candidates failed: {exc}")
    if trace.problems:
        trace.status = "INCOMPLETE"
    elif any(not r.own for r in trace.references):
        trace.status = "FOUND"
    else:
        trace.status = "NONE"
    return trace


def run_trace(config: Config, report: PageWatchReport, *, auth_header: str, today: date,
              progress: Callable[[str], None] = print, cache: Optional[SpecCache] = None) -> None:
    pw = config.page_watch
    cache = cache or SpecCache(config.data_dir / "spec_cache")
    llm = spec_llm(config, "summary")
    deltas = report.synthesis.delta_changes if report.synthesis else []
    pages = report.with_status(PageStatus.CHANGED, PageStatus.NEW)
    progress(f"trace check for {len(pages)} page(s)")

    def one(pc: PageChange) -> None:
        try:
            pc.trace = trace_page(pc, [d for d in deltas if d.page == pc.title], config=config, llm=llm, cache=cache,
                                  auth_header=auth_header, today=today, window_end=report.window_end)
        except Exception as exc:  # noqa: BLE001 - one page must never fail the run
            log.exception("trace failed for %s", pc.title)
            pc.trace = PageTrace("INCOMPLETE", problems=[f"unexpected error {type(exc).__name__}: {exc}"])

    with ThreadPoolExecutor(max_workers=max(1, pw.workers)) as pool:
        list(pool.map(one, pages))
    for pc in pages:
        for problem in pc.trace.problems:
            report.errors.append(ReportError(scope=pc.title, message=problem, impact=_INCOMPLETE_IMPACT))
