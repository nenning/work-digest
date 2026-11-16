import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from digest.config import GitLabConfig, GitLabRepoConfig, LLMConfig, PageWatchConfig, PageWatchTraceConfig, RetryConfig
from digest.spec_monitor import page_trace
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.models import (
    DeltaChange, PageChange, PageStatus, PageTrace, PageWatchReport, TraceCandidate, TraceSourceError, WatchSynthesis,
)
from digest.spec_monitor.trace_code import CodeSearch

TODAY = date(2026, 11, 16)
LLM = LLMConfig(provider="openai", api_key="k", models=["m"], llm_timeout=30)


def _config(tmp_path, workers=1):
    repo = GitLabRepoConfig(name="app", project="g/app")
    pw = PageWatchConfig(parent_page_id="1", retry=RetryConfig(1, 0), workers=workers,
                         trace=PageWatchTraceConfig(jira_projects=["PSSA"], lookback_days=90, repositories=["app"]))
    return SimpleNamespace(page_watch=pw, atlassian=MagicMock(), language="de", data_dir=tmp_path, llm=LLM,
                           gitlab=GitLabConfig(url="https://gl", api_token="t", repositories=[repo]), spec_monitor=None)


def _change(title="A", labels=()):
    return PageChange(page_id="2", title=title, url="u", status=PageStatus.CHANGED, summary="Neue Regel",
                      labels=None if labels is None else list(labels))


def _cand(kind, ref, own=False):
    return TraceCandidate(kind=kind, ref=ref, label=f"label {ref}", url=f"https://x/{ref}", own=own)


def _ask(mocker, terms=("requestInProgress",), refs=()):
    def fake(prompt, llm, timeout, max_tokens, validate, purpose="other"):
        data = {"terms": list(terms)} if purpose == "trace_terms" else {"references": list(refs)}
        return validate(data), json.dumps(data)
    return mocker.patch.object(page_trace.analysis, "_ask_json", side_effect=fake)


@pytest.fixture
def sources(mocker):
    return SimpleNamespace(
        jira=mocker.patch.object(page_trace, "search_issues", return_value=[]),
        code=mocker.patch.object(page_trace, "search_code", return_value=CodeSearch(searched=["app: branches"])),
    )


def _trace(tmp_path, change=None, deltas=(), window_end=None):
    return page_trace.trace_page(change or _change(), list(deltas), config=_config(tmp_path), llm=LLM,
                                 cache=SpecCache(tmp_path / "c"), auth_header="h", today=TODAY, window_end=window_end)


def test_frozen_page_makes_no_calls(tmp_path, mocker, sources):
    ask = _ask(mocker)
    t = _trace(tmp_path, _change(labels=["Frozen"]))
    assert t.status == "FROZEN"
    ask.assert_not_called()
    sources.jira.assert_not_called()
    sources.code.assert_not_called()


def test_extract_terms_caps_and_dedupes(tmp_path, mocker):
    raw = ["Alpha", "alpha", " beta  gamma ", ""] + [f"t{i}" for i in range(8)]
    _ask(mocker, terms=raw)
    terms = page_trace.extract_terms(_change(), [], LLM, "de", SpecCache(tmp_path / "c"))
    assert len(terms) == 8 and len({t.casefold() for t in terms}) == 8
    assert terms[:2] == ["Alpha", "beta gamma"]


def test_extract_terms_is_cached(tmp_path, mocker):
    ask = _ask(mocker, terms=["a"])
    cache = SpecCache(tmp_path / "c")
    page_trace.extract_terms(_change(), [], LLM, "de", cache)
    page_trace.extract_terms(_change(), [], LLM, "de", cache)
    assert ask.call_count == 1


def test_empty_terms_is_incomplete_not_none(tmp_path, mocker, sources):
    _ask(mocker, terms=[" ", ""])
    t = _trace(tmp_path)
    assert t.status == "INCOMPLETE"
    assert "no search terms could be derived" in t.problems
    sources.jira.assert_not_called()
    sources.code.assert_not_called()


def test_found_with_three_references_max(tmp_path, mocker, sources):
    cands = [_cand("code", "f.py"), _cand("jira", "PSSA-1"), _cand("mr", "!4"), _cand("branch", "b"),
             _cand("jira", "PSSA-2")]
    sources.jira.return_value = cands
    verdicts = ["related", "in_progress", "in_progress", "done_likely", "related"]
    _ask(mocker, refs=[{"ref": c.ref, "verdict": v, "reason": "r"} for c, v in zip(cands, verdicts)])
    t = _trace(tmp_path)
    assert t.status == "FOUND"
    assert [r.ref for r in t.references] == ["PSSA-1", "!4", "b"]


def test_invented_reference_is_dropped_with_problem(tmp_path, mocker, sources):
    sources.jira.return_value = [_cand("jira", "PSSA-1")]
    _ask(mocker, refs=[{"ref": "PSSA-999", "verdict": "in_progress", "reason": "r"},
                       {"ref": "PSSA-1", "verdict": "in_progress", "reason": "r"}])
    t = _trace(tmp_path)
    assert [r.ref for r in t.references] == ["PSSA-1"]
    assert "judge returned unknown reference PSSA-999" in t.problems
    assert t.status == "INCOMPLETE"


def test_own_ticket_is_related_and_not_a_finding(tmp_path, mocker, sources):
    sources.jira.return_value = [_cand("jira", "PSSA-7", own=True)]
    _ask(mocker, refs=[{"ref": "PSSA-7", "verdict": "in_progress", "reason": "r"}])
    t = _trace(tmp_path)
    assert t.status == "NONE"
    assert len(t.references) == 1 and t.references[0].own and t.references[0].verdict == "related"


def test_no_candidates_and_no_problems_is_none_and_names_what_was_searched(tmp_path, mocker, sources):
    ask = _ask(mocker)
    t = _trace(tmp_path, window_end=date(2026, 11, 15))
    assert t.status == "NONE" and t.references == []
    assert ask.call_count == 1
    assert any("PSSA" in s and "90 days" in s and "2026-11-15" in s for s in t.searched)
    assert "app: branches" in t.searched
    args = sources.jira.call_args.args
    assert args[3] == ["requestInProgress"] and args[4] == ["PSSA"] and args[5] == date(2026, 8, 17)


def test_failed_source_makes_incomplete_even_when_other_source_finds_nothing(tmp_path, mocker, sources):
    sources.jira.side_effect = TraceSourceError("Jira search: HTTP 500")
    _ask(mocker)
    t = _trace(tmp_path)
    assert t.status == "INCOMPLETE"
    assert any("Jira search: HTTP 500" in p for p in t.problems)


def test_failed_source_keeps_found_references(tmp_path, mocker, sources):
    sources.jira.return_value = [_cand("jira", "PSSA-1")]
    sources.code.return_value = CodeSearch(problems=["GitLab app: branch search failed (HTTP 403)"])
    _ask(mocker, refs=[{"ref": "PSSA-1", "verdict": "in_progress", "reason": "r"}])
    t = _trace(tmp_path)
    assert t.status == "INCOMPLETE" and [r.ref for r in t.references] == ["PSSA-1"]
    assert any("403" in p for p in t.problems)


def test_unreadable_labels_is_a_problem(tmp_path, mocker, sources):
    _ask(mocker)
    t = _trace(tmp_path, _change(labels=None))
    assert t.status == "INCOMPLETE" and "labels unreadable, freeze state unknown" in t.problems


def test_term_llm_failure_is_incomplete(tmp_path, mocker, sources):
    mocker.patch.object(page_trace.analysis, "_ask_json", side_effect=AnalysisError("boom"))
    t = _trace(tmp_path)
    assert t.status == "INCOMPLETE" and any("boom" in p for p in t.problems)


def test_judge_failure_is_incomplete(tmp_path, mocker, sources):
    sources.jira.return_value = [_cand("jira", "PSSA-1")]

    def fake(prompt, llm, timeout, max_tokens, validate, purpose="other"):
        if purpose == "trace_terms":
            return validate({"terms": ["x"]}), "{}"
        raise AnalysisError("judge down")

    mocker.patch.object(page_trace.analysis, "_ask_json", side_effect=fake)
    t = _trace(tmp_path)
    assert t.status == "INCOMPLETE" and any("judge down" in p for p in t.problems)


def _report(titles=("A", "B"), deltas=()):
    pages = [_change(t) for t in titles]
    pages.append(PageChange(page_id="9", title="Cosmetic", url="u", status=PageStatus.COSMETIC_ONLY))
    return PageWatchReport(generated_at=None, pages=pages,
                           synthesis=WatchSynthesis(overview="o", delta_changes=list(deltas)))


def test_unexpected_exception_is_contained_per_page(tmp_path, mocker):
    def boom(change, *a, **k):
        if change.title == "A":
            raise RuntimeError("kaputt")
        return PageTrace("NONE")

    mocker.patch.object(page_trace, "trace_page", side_effect=boom)
    report = _report()
    page_trace.run_trace(_config(tmp_path), report, auth_header="h", today=TODAY, progress=lambda m: None)
    a, b, c = report.pages
    assert a.trace.status == "INCOMPLETE" and "kaputt" in a.trace.problems[0]
    assert b.trace.status == "NONE"
    assert c.trace is None
    assert [e.scope for e in report.errors] == ["A"]


def test_run_trace_passes_only_the_pages_deltas_and_reports_problems(tmp_path, mocker):
    seen = {}

    def fake(change, deltas, **k):
        seen[change.title] = [d.change for d in deltas]
        return PageTrace("INCOMPLETE", problems=["Jira search: HTTP 500"])

    mocker.patch.object(page_trace, "trace_page", side_effect=fake)
    deltas = [DeltaChange(kind="changed", affected_part="1", change="x", page="A"),
              DeltaChange(kind="new", affected_part="2", change="y", page="B")]
    report = _report(deltas=deltas)
    page_trace.run_trace(_config(tmp_path), report, auth_header="h", today=TODAY, progress=lambda m: None)
    assert seen == {"A": ["x"], "B": ["y"]}
    assert len(report.errors) == 2
    assert report.errors[0].impact.startswith("the check for existing Jira")
    assert "absence of traces is not established" in report.errors[0].impact


def test_run_trace_without_synthesis_uses_no_deltas(tmp_path, mocker):
    seen = []
    mocker.patch.object(page_trace, "trace_page", side_effect=lambda c, d, **k: seen.append(d) or PageTrace("NONE"))
    report = _report()
    report.synthesis = None
    page_trace.run_trace(_config(tmp_path), report, auth_header="h", today=TODAY, progress=lambda m: None)
    assert seen == [[], []]
