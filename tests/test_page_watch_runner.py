from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from digest.config import LLMConfig, PageWatchConfig, RetryConfig
from digest.spec_monitor import page_watch
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.confluence_client import NoStorageBody
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import (
    Classification, DeltaChange, PageStatus, SourceResult, VersionPoint, WatchSynthesis,
)
from digest.spec_monitor.runner import SpecMonitorFatal

TODAY = date(2026, 11, 13)


def _iso(day, hour=10):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour).astimezone().isoformat()


def _vp(number, day):
    return VersionPoint(version=str(number), when=datetime.combine(day, datetime.min.time()).replace(hour=10).astimezone())


def _config(tmp_path, **over):
    pw = PageWatchConfig(parent_page_id="1", retry=RetryConfig(1, 0), workers=over.pop("workers", 1), **over)
    return SimpleNamespace(
        page_watch=pw, atlassian=MagicMock(), language="de", data_dir=tmp_path,
        llm=LLMConfig(provider="openai", api_key="k", models=["m"], llm_timeout=30), spec_monitor=None,
    )


class FakeClient:
    def __init__(self, pages, versions=None, parent_when=None):
        self.pages = pages
        self.versions = versions or {}
        self.parent_when = parent_when or _iso(date(2026, 1, 1))
        self.bodies = {}
        self.fail_search = None

    def get_page(self, pid, with_body=True):
        if pid == "1":
            return {"title": "Coremodul", "version": {"when": self.parent_when}, "ancestors": []}
        return {"title": pid, "ancestors": [{"id": "1", "title": "Coremodul"}, {"id": "50", "title": "Gruppe"}]}

    def search_descendants(self, parent, label=None):
        if self.fail_search:
            raise self.fail_search
        self.label = label
        return self.pages

    def page_url(self, pid, version=None):
        return f"https://x/{pid}"

    def list_versions(self, pid):
        if isinstance(self.versions.get(pid), Exception):
            raise self.versions[pid]
        return self.versions.get(pid, [_vp(1, date(2026, 1, 1))])

    def get_body(self, pid, version):
        body = self.bodies.get(pid, "<p>Neuer Inhalt</p>")
        if isinstance(body, Exception):
            raise body
        return body


class FakeMonitor:
    def __init__(self, fn=None):
        self.fn = fn or (lambda ref, spec: SourceResult(ref=ref, classification=Classification.CHANGED,
                                                        diff_text="- a\n+ b",
                                                        old=_vp(1, date(2026, 10, 1)), new=_vp(2, date(2026, 11, 5))))
        self.refs = []

    def resolve(self, ref, spec):
        self.refs.append((ref, spec))
        return self.fn(ref, spec)


@pytest.fixture(autouse=True)
def llm(mocker):
    mocker.patch.object(page_watch.analysis, "summarize_source_diff", return_value="Zusammenfassung")
    return mocker.patch.object(page_watch.watch_analysis, "synthesize_watch", return_value=WatchSynthesis(
        overview="Gesamt", change_request="CR", delta_changes=[DeltaChange(kind="changed", affected_part="3", page="A")]))


def _run(config, client, monitor=None, **kw):
    return page_watch.run_page_watch(config, client=client, monitor=monitor or FakeMonitor(), today=TODAY,
                                     progress=lambda _m: None, **kw)


def _entry(pid, when_day):
    return {"id": pid, "title": pid, "version": 3, "when": _iso(when_day)}


def test_baseline_day_and_window_start():
    assert page_watch.baseline_day(14, TODAY) == date(2026, 10, 30)
    assert page_watch.baseline_day(1, TODAY) == date(2026, 11, 12)


def test_window_start_is_first_covered_day(tmp_path):
    report = _run(_config(tmp_path), FakeClient([]))
    assert report.window_days == 14 and report.window_start == date(2026, 10, 31)


def test_days_override_beats_config(tmp_path):
    report = _run(_config(tmp_path), FakeClient([]), days=3)
    assert report.window_days == 3 and report.window_start == date(2026, 11, 11)


def test_pages_not_modified_since_baseline_are_unchanged_without_further_calls(tmp_path):
    client = FakeClient([_entry("A", date(2026, 10, 30))])
    monitor = FakeMonitor()
    report = _run(_config(tmp_path), client, monitor)
    assert [p.status for p in report.pages] == [PageStatus.UNCHANGED, PageStatus.UNCHANGED]
    assert monitor.refs == []


def test_changed_page_is_resolved_against_baseline_day_and_summarised(tmp_path, llm):
    client = FakeClient([_entry("A", date(2026, 11, 5))])
    monitor = FakeMonitor()
    client.versions["A"] = [_vp(1, date(2026, 10, 1)), _vp(2, date(2026, 11, 5))]
    report = _run(_config(tmp_path), client, monitor)
    page = next(p for p in report.pages if p.page_id == "A")
    assert page.status == PageStatus.CHANGED and page.summary == "Zusammenfassung"
    assert page.path == ["Coremodul", "Gruppe"]
    ref, spec = monitor.refs[0]
    assert ref.baseline_date == date(2026, 10, 30) and ref.page_id == "A" and spec.page_id == "A"
    assert page_watch.analysis.summarize_source_diff.call_args.kwargs["page_mode"] is True
    assert report.synthesis.overview == "Gesamt" and not report.errors and not report.analysis_incomplete
    assert [p.page_id for p in llm.call_args.args[0]] == ["A"]


def test_parent_page_is_included_and_listed_first(tmp_path):
    client = FakeClient([_entry("B", date(2026, 10, 1)), _entry("A", date(2026, 10, 1))],
                        parent_when=_iso(date(2026, 11, 8)))
    client.versions["1"] = [_vp(1, date(2026, 10, 1)), _vp(2, date(2026, 11, 8))]
    report = _run(_config(tmp_path), client)
    assert [p.page_id for p in report.pages] == ["1", "A", "B"]
    assert report.pages[0].title == "Coremodul" and report.pages[0].status == PageStatus.CHANGED


def test_parent_can_be_excluded(tmp_path):
    report = _run(_config(tmp_path, include_parent=False), FakeClient([_entry("A", date(2026, 10, 1))]))
    assert [p.page_id for p in report.pages] == ["A"]


def test_nothing_to_watch_is_a_reported_problem(tmp_path):
    report = _run(_config(tmp_path, include_parent=False), FakeClient([]))
    assert any("no page to watch" in e.message for e in report.errors)


def test_label_is_passed_to_the_search(tmp_path):
    client = FakeClient([])
    _run(_config(tmp_path, label="core"), client)
    assert client.label == "core"


def test_new_page_is_reported_with_whole_text_not_as_error(tmp_path, llm):
    client = FakeClient([_entry("N", date(2026, 11, 9))])
    client.versions["N"] = [_vp(1, date(2026, 11, 9))]
    client.bodies["N"] = "<p>Anforderung CM-FR-09</p>"
    monitor = FakeMonitor()
    report = _run(_config(tmp_path), client, monitor)
    page = next(p for p in report.pages if p.page_id == "N")
    assert page.status == PageStatus.NEW and not page.errors
    assert page.result.old is None and page.result.new.version == "1"
    assert "+ Anforderung CM-FR-09" in page.result.diff_text
    assert monitor.refs == []
    assert page in llm.call_args.args[0]


def test_new_page_without_storage_body_is_new_with_warning(tmp_path):
    client = FakeClient([_entry("N", date(2026, 11, 9))])
    client.versions["N"] = [_vp(1, date(2026, 11, 9))]
    client.bodies["N"] = NoStorageBody("no body")
    page = next(p for p in _run(_config(tmp_path), client).pages if p.page_id == "N")
    assert page.status == PageStatus.NEW and any("no storage body" in w for w in page.warnings)


def test_cosmetic_result_is_not_summarised_or_synthesised(tmp_path, llm):
    client = FakeClient([_entry("A", date(2026, 11, 5))])
    monitor = FakeMonitor(lambda ref, spec: SourceResult(ref=ref, classification=Classification.COSMETIC_ONLY))
    report = _run(_config(tmp_path), client, monitor)
    assert next(p for p in report.pages if p.page_id == "A").status == PageStatus.COSMETIC_ONLY
    page_watch.analysis.summarize_source_diff.assert_not_called()
    llm.assert_not_called()
    assert report.synthesis is None


def test_empty_model_summary_demotes_to_cosmetic_with_warning(tmp_path):
    page_watch.analysis.summarize_source_diff.return_value = ""
    client = FakeClient([_entry("A", date(2026, 11, 5))])
    page = next(p for p in _run(_config(tmp_path), client).pages if p.page_id == "A")
    assert page.status == PageStatus.COSMETIC_ONLY and any("kosmetisch" in w for w in page.warnings)


def test_failing_page_is_an_error_row_and_others_continue(tmp_path):
    client = FakeClient([_entry("A", date(2026, 11, 5)), _entry("B", date(2026, 11, 5))])

    def fn(ref, spec):
        if ref.page_id == "A":
            raise RuntimeError("kaputt")
        return SourceResult(ref=ref, classification=Classification.CHANGED, diff_text="+ x",
                            old=_vp(1, date(2026, 10, 1)), new=_vp(2, date(2026, 11, 5)))
    report = _run(_config(tmp_path), client, FakeMonitor(fn))
    by_id = {p.page_id: p for p in report.pages}
    assert by_id["A"].status == PageStatus.ERROR and by_id["B"].status == PageStatus.CHANGED
    assert any(e.scope == "A" and "kaputt" in e.message for e in report.errors)
    assert report.synthesis is not None


def test_monitor_error_result_becomes_error_row(tmp_path):
    client = FakeClient([_entry("A", date(2026, 11, 5))])
    monitor = FakeMonitor(lambda ref, spec: SourceResult(ref=ref, classification=Classification.ERROR,
                                                         errors=["HTTP 403"]))
    report = _run(_config(tmp_path), client, monitor)
    assert any(e.scope == "A" and "HTTP 403" in e.message for e in report.errors)


def test_request_failure_while_listing_versions_is_an_error_row(tmp_path):
    client = FakeClient([_entry("A", date(2026, 11, 5))])
    client.versions["A"] = RequestFailed("https://x", 3, "timeout")
    report = _run(_config(tmp_path), client)
    assert any(e.scope == "A" and "timeout" in e.message and "3 attempt" in e.message for e in report.errors)


def test_versions_without_dates_are_an_error_not_a_new_page(tmp_path):
    client = FakeClient([_entry("A", date(2026, 11, 5))])
    client.versions["A"] = [VersionPoint(version="1", when=None)]
    page = next(p for p in _run(_config(tmp_path), client).pages if p.page_id == "A")
    assert page.status == PageStatus.ERROR


def test_missing_when_in_search_result_falls_back_to_resolving(tmp_path):
    client = FakeClient([{"id": "A", "title": "A", "version": 2, "when": None}])
    client.versions["A"] = [_vp(1, date(2026, 10, 1))]
    monitor = FakeMonitor(lambda ref, spec: SourceResult(ref=ref, classification=Classification.UNCHANGED))
    report = _run(_config(tmp_path), client, monitor)
    assert len(monitor.refs) == 1 and report.synthesis is None


def test_discovery_failure_is_fatal(tmp_path):
    client = FakeClient([])
    client.fail_search = RequestFailed("https://x", 3, "HTTP 503")
    with pytest.raises(SpecMonitorFatal, match="HTTP 503"):
        _run(_config(tmp_path), client)


def test_unknown_parent_is_fatal(tmp_path):
    client = FakeClient([])
    client.get_page = MagicMock(side_effect=LookupError("page 1 not found (HTTP 404)"))
    with pytest.raises(SpecMonitorFatal, match="404"):
        _run(_config(tmp_path), client)


def test_missing_block_and_bad_window_are_fatal(tmp_path):
    cfg = _config(tmp_path)
    cfg.page_watch = None
    with pytest.raises(SpecMonitorFatal, match="page_watch"):
        _run(cfg, FakeClient([]))
    with pytest.raises(SpecMonitorFatal, match="at least 1"):
        _run(_config(tmp_path), FakeClient([]), days=0)


def test_synthesis_failure_marks_incomplete_but_keeps_pages(tmp_path, llm):
    llm.side_effect = AnalysisError("kein JSON")
    client = FakeClient([_entry("A", date(2026, 11, 5))])
    report = _run(_config(tmp_path), client)
    assert report.analysis_incomplete and report.synthesis is None
    assert any("kein JSON" in e.message for e in report.errors)
    assert next(p for p in report.pages if p.page_id == "A").status == PageStatus.CHANGED


def test_summary_failure_is_reported_and_diff_still_goes_to_synthesis(tmp_path, llm):
    page_watch.analysis.summarize_source_diff.side_effect = AnalysisError("timeout")
    client = FakeClient([_entry("A", date(2026, 11, 5))])
    report = _run(_config(tmp_path), client)
    page = next(p for p in report.pages if p.page_id == "A")
    assert page.status == PageStatus.CHANGED and page.summary == "" and page.summary_failed == "timeout"
    assert report.analysis_incomplete and any("diff summary failed" in e.message for e in report.errors)
    assert page in llm.call_args.args[0]


def test_workers_run_pages_in_parallel_and_keep_order(tmp_path):
    entries = [_entry(c, date(2026, 10, 1)) for c in "DCBA"]
    report = _run(_config(tmp_path, workers=4), FakeClient(entries))
    assert [p.page_id for p in report.pages] == ["1", "A", "B", "C", "D"]
