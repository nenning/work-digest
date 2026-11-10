import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from digest.config import LLMConfig, RetryConfig, SpecMonitorConfig
from digest.spec_monitor import runner
from digest.spec_monitor.analysis import AnalysisError, SpecSynthesis
from digest.spec_monitor.confluence_monitor import ConfluenceMonitor
from digest.spec_monitor.discovery import Discovery
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import (
    Classification, DeltaChange, ParsedSources, SourceKind, SourceRef, SourceResult, SpecInfo,
)


def _spec(title, status="Refined"):
    return SpecInfo(page_id=title, title=title, url=f"https://x/{title}", status=status, body_storage="<p/>")


def _config(tmp_path):
    return SimpleNamespace(
        spec_monitor=SpecMonitorConfig(parent_page_id="1", selector_type="label", selector_value="spec", retry=RetryConfig(1, 0)),
        atlassian=MagicMock(), llm=LLMConfig(provider="openai", api_key="k", models=["m"], llm_timeout=30), language="de", data_dir=tmp_path, gitlab=None,
    )


class FakeMonitor:
    def __init__(self, kind, fn):
        self.kind, self.fn = kind, fn

    def handles(self, ref):
        return ref.kind == self.kind

    def resolve(self, ref, spec):
        return self.fn(ref, spec)


def _changed(ref, spec):
    return SourceResult(ref=ref, classification=Classification.CHANGED, diff_text="- a\n+ b")


@pytest.fixture
def patched(mocker):
    mocker.patch.object(runner, "parse_sources")
    mocker.patch.object(runner.analysis, "summarize_source_diff", return_value="Zusammenfassung")
    mocker.patch.object(runner.analysis, "synthesize_spec", return_value=SpecSynthesis(
        change_request="CR", delta_changes=[DeltaChange(kind="changed", affected_part="3.1")], open_points=[], effort=None))
    mocker.patch.object(runner.analysis, "build_completeness_warnings", return_value=["warn"])
    return runner


def _run(config, specs, refs, monitors, only=None):
    runner.discover_specs.return_value = Discovery(specs=specs, excluded=[("9", "Excluded Page")])
    runner.parse_sources.return_value = ParsedSources(found=True, refs=refs)
    return runner.run_spec_monitor(config, only=only, client=MagicMock(), monitors=monitors, progress=lambda _m: None)


@pytest.fixture(autouse=True)
def _discover(mocker):
    mocker.patch.object(runner, "discover_specs")


def test_splits_analysed_and_listed_only(tmp_path, patched):
    cfg = _config(tmp_path)
    ref = SourceRef(name="A", kind=SourceKind.CONFLUENCE)
    report = _run(cfg, [_spec("S1", "refined"), _spec("S2", "In Ausarbeitung"), _spec("S3", "Angenommen")], [ref],
                  [FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    assert [a.spec.title for a in report.analysed] == ["S1", "S3"]
    assert [s.title for s in report.listed_only] == ["S2"]
    assert report.excluded_pages == [("9", "Excluded Page")]
    assert report.analysed[0].change_request == "CR"
    assert report.analysed[0].sources[0].diff_summary == "Zusammenfassung"
    assert report.analysed[0].completeness_warnings == ["warn"]


def test_failing_monitor_becomes_error_row_not_failure(tmp_path, patched):
    def boom(ref, spec):
        raise RuntimeError("kaputt")
    refs = [SourceRef(name="A", kind=SourceKind.CONFLUENCE), SourceRef(name="B", kind=SourceKind.ECH)]
    report = _run(_config(tmp_path), [_spec("S1")], refs, [
        FakeMonitor(SourceKind.CONFLUENCE, boom), FakeMonitor(SourceKind.ECH, _changed)])
    results = report.analysed[0].sources
    assert results[0].classification == Classification.ERROR and "kaputt" in results[0].errors[0]
    assert results[1].classification == Classification.CHANGED


def test_excluded_external_and_parse_issue_rows(tmp_path, patched):
    refs = [
        SourceRef(name="Notizen", kind=SourceKind.CONFLUENCE, excluded_reason="working notes"),
        SourceRef(name="Web", kind=SourceKind.EXTERNAL, note="v1.4"),
        SourceRef(name="Pair", kind=SourceKind.CONFLUENCE, parse_issue="could not pair links and versions"),
    ]
    report = _run(_config(tmp_path), [_spec("S1")], refs, [])
    kinds = [r.classification for r in report.analysed[0].sources]
    assert kinds == [Classification.EXCLUDED, Classification.NOT_MONITORED, Classification.ERROR]
    assert report.analysed[0].change_request is None


def test_llm_failure_marks_analysis_incomplete_but_keeps_sources(tmp_path, patched):
    runner.analysis.synthesize_spec.side_effect = AnalysisError("kein JSON")
    report = _run(_config(tmp_path), [_spec("S1")], [SourceRef(name="A", kind=SourceKind.CONFLUENCE)],
                  [FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    a = report.analysed[0]
    assert a.analysis_incomplete and a.change_request is None
    assert a.sources[0].classification == Classification.CHANGED
    assert "kein JSON" in a.errors[0].message


def test_summary_failure_is_reported_per_source(tmp_path, patched):
    runner.analysis.summarize_source_diff.side_effect = AnalysisError("timeout")
    report = _run(_config(tmp_path), [_spec("S1")], [SourceRef(name="A", kind=SourceKind.CONFLUENCE)],
                  [FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    assert report.analysed[0].analysis_incomplete
    assert any("S1: A" == e.scope for e in report.analysed[0].errors)


def test_null_summary_makes_source_cosmetic_and_skips_change_request(tmp_path, patched):
    runner.analysis.summarize_source_diff.return_value = ""
    report = _run(_config(tmp_path), [_spec("S1")], [SourceRef(name="A", kind=SourceKind.CONFLUENCE)],
                  [FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    a = report.analysed[0]
    assert a.sources[0].classification == Classification.COSMETIC_ONLY
    assert a.sources[0].diff_text == ""
    assert not a.has_changes
    runner.analysis.synthesize_spec.assert_not_called()


def test_null_summary_only_demotes_that_source(tmp_path, patched):
    runner.analysis.summarize_source_diff.side_effect = lambda r, *a: "" if r.ref.name == "A" else "echt"
    refs = [SourceRef(name=n, kind=SourceKind.CONFLUENCE) for n in "AB"]
    a = _run(_config(tmp_path), [_spec("S1")], refs, [FakeMonitor(SourceKind.CONFLUENCE, _changed)]).analysed[0]
    assert [s.classification for s in a.sources] == [Classification.COSMETIC_ONLY, Classification.CHANGED]
    assert a.has_changes
    runner.analysis.synthesize_spec.assert_called_once()


def test_unchanged_sources_skip_llm(tmp_path, patched):
    unchanged = lambda ref, spec: SourceResult(ref=ref, classification=Classification.UNCHANGED)
    report = _run(_config(tmp_path), [_spec("S1")], [SourceRef(name="A", kind=SourceKind.CONFLUENCE)],
                  [FakeMonitor(SourceKind.CONFLUENCE, unchanged)])
    runner.analysis.synthesize_spec.assert_not_called()
    assert not report.analysed[0].has_changes


def test_spec_level_crash_is_isolated(tmp_path, patched):
    def crash(*a, **k):
        raise ValueError("unexpected")
    runner.parse_sources.side_effect = crash
    runner.discover_specs.return_value = Discovery(specs=[_spec("S1"), _spec("S2")])
    report = runner.run_spec_monitor(_config(tmp_path), client=MagicMock(), monitors=[], progress=lambda _m: None)
    assert len(report.analysed) == 2
    assert all(a.analysis_incomplete and a.errors for a in report.analysed)


def test_discovery_failure_is_fatal(tmp_path):
    runner.discover_specs.side_effect = RequestFailed("u", 3, "HTTP 503")
    with pytest.raises(runner.SpecMonitorFatal, match="could not be discovered"):
        runner.run_spec_monitor(_config(tmp_path), client=MagicMock(), monitors=[], progress=lambda _m: None)


def test_unknown_only_spec_is_fatal(tmp_path):
    runner.discover_specs.side_effect = ValueError("no spec matches 'x'")
    with pytest.raises(runner.SpecMonitorFatal):
        runner.run_spec_monitor(_config(tmp_path), only="x", client=MagicMock(), monitors=[], progress=lambda _m: None)


def test_missing_config_block_is_fatal(tmp_path):
    cfg = _config(tmp_path)
    cfg.spec_monitor = None
    with pytest.raises(runner.SpecMonitorFatal):
        runner.run_spec_monitor(cfg, client=MagicMock(), monitors=[])


def test_no_sources_found_uses_llm_fallback_sentence(tmp_path, patched, mocker):
    mocker.patch.object(runner, "find_sources_sentence", return_value="Quellen: Seite X v35")
    extracted = [SourceRef(name="X", kind=SourceKind.CONFLUENCE, low_confidence=True)]
    mocker.patch.object(runner.analysis, "extract_sources_with_llm", return_value=extracted)
    runner.discover_specs.return_value = Discovery(specs=[_spec("S1")])
    runner.parse_sources.return_value = ParsedSources(found=False)
    report = runner.run_spec_monitor(_config(tmp_path), client=MagicMock(), progress=lambda _m: None,
                                     monitors=[FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    assert report.analysed[0].sources[0].ref.low_confidence
    args = runner.analysis.build_completeness_warnings.call_args[0]
    assert any("LLM" in i for i in args[2])


def test_analyse_all_states_flag_analyses_every_spec(tmp_path, patched):
    cfg = _config(tmp_path)
    cfg.spec_monitor.analyse_all_states = True
    ref = SourceRef(name="A", kind=SourceKind.CONFLUENCE)
    report = _run(cfg, [_spec("S1", "In Ausarbeitung"), _spec("S2", "Abgelöst")], [ref],
                  [FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    assert [a.spec.title for a in report.analysed] == ["S1", "S2"]
    assert report.listed_only == []


def test_build_monitors_passes_the_derived_vision_config_to_confluence_monitor(tmp_path, mocker):
    cfg = _config(tmp_path)
    mocker.patch.object(runner, "ConfluenceMonitor")
    cache = runner.SpecCache(tmp_path / "spec_cache")
    runner._build_monitors(cfg, cache, MagicMock())
    kwargs = runner.ConfluenceMonitor.call_args.kwargs
    assert kwargs["vision"].model == (cfg.llm.heavy_models or cfg.llm.models)[0]
    assert kwargs["vision"].api_key == cfg.llm.api_key
    assert kwargs["language"] == "de" and kwargs["cache"] is cache


@pytest.mark.parametrize("dry_run", [False, True])
def test_build_monitors_passes_dry_run_to_url_monitor(tmp_path, mocker, dry_run):
    cfg = _config(tmp_path)
    mocker.patch.object(runner, "ConfluenceMonitor")
    monitors = runner._build_monitors(cfg, runner.SpecCache(tmp_path / "spec_cache"), MagicMock(), dry_run)
    url = next(m for m in monitors if type(m).__name__ == "UrlMonitor")
    assert url.dry_run is dry_run


def test_build_monitors_without_vision_or_gitlab(tmp_path, mocker):
    cfg = _config(tmp_path)
    cfg.llm = SimpleNamespace(provider="anthropic", api_key="k", models=["m1"], heavy_models=[], vision_model=None, endpoint=None, llm_timeout=30)
    monitors = runner._build_monitors(cfg, MagicMock(), MagicMock())
    assert [type(m).__name__ for m in monitors] == ["ConfluenceMonitor", "EchMonitor", "I14yMonitor", "UrlMonitor"]


# ---- concurrency ----

class FakeConfluence(ConfluenceMonitor):
    """Parallel-safe monitor (isinstance ConfluenceMonitor), without a client."""
    def __init__(self, fn):
        self.fn = fn

    def handles(self, ref):
        return ref.kind == SourceKind.CONFLUENCE

    def resolve(self, ref, spec):
        return self.fn(ref, spec)


class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.order = []

    def enter(self, name):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.order.append(name)

    def leave(self):
        with self.lock:
            self.active -= 1


def _unchanged(ref, spec):
    return SourceResult(ref=ref, classification=Classification.UNCHANGED)


def test_results_keep_source_order_with_out_of_order_completion(tmp_path, patched):
    done_c = threading.Event()

    def fn(ref, spec):
        if ref.name == "A":
            assert done_c.wait(5)
        if ref.name == "C":
            done_c.set()
        return SourceResult(ref=ref, classification=Classification.UNCHANGED, warnings=[ref.name])

    refs = [SourceRef(name=n, kind=SourceKind.CONFLUENCE) for n in "ABC"]
    cfg = _config(tmp_path)
    cfg.spec_monitor.workers = 3
    report = _run(cfg, [_spec("S1")], refs, [FakeConfluence(fn)])
    assert [r.ref.name for r in report.analysed[0].sources] == ["A", "B", "C"]


def test_confluence_monitor_runs_in_parallel(tmp_path, patched):
    barrier = threading.Barrier(2, timeout=5)

    def fn(ref, spec):
        barrier.wait()
        return _unchanged(ref, spec)

    refs = [SourceRef(name=n, kind=SourceKind.CONFLUENCE) for n in "AB"]
    report = _run(_config(tmp_path), [_spec("S1")], refs, [FakeConfluence(fn)])
    assert all(r.classification == Classification.UNCHANGED for r in report.analysed[0].sources)


def test_non_confluence_monitors_never_overlap(tmp_path, patched):
    tracker = Tracker()

    def make(kind):
        def fn(ref, spec):
            tracker.enter(ref.name)
            time.sleep(0.02)
            tracker.leave()
            return _unchanged(ref, spec)
        return FakeMonitor(kind, fn)

    refs = [SourceRef(name=f"E{i}", kind=SourceKind.ECH) for i in range(6)]
    cfg = _config(tmp_path)
    cfg.spec_monitor.workers = 6
    report = _run(cfg, [_spec("S1")], refs, [make(SourceKind.ECH)])
    assert tracker.max_active == 1
    assert [r.ref.name for r in report.analysed[0].sources] == [f"E{i}" for i in range(6)]


def test_different_non_confluence_monitors_may_overlap(tmp_path, patched):
    barrier = threading.Barrier(2, timeout=5)

    def fn(ref, spec):
        barrier.wait()
        return _unchanged(ref, spec)

    refs = [SourceRef(name="E", kind=SourceKind.ECH), SourceRef(name="U", kind=SourceKind.URL)]
    report = _run(_config(tmp_path), [_spec("S1")], refs,
                  [FakeMonitor(SourceKind.ECH, fn), FakeMonitor(SourceKind.URL, fn)])
    assert all(r.classification == Classification.UNCHANGED for r in report.analysed[0].sources)


def test_exceptions_stay_isolated_in_parallel(tmp_path, patched):
    def boom(ref, spec):
        raise RuntimeError(f"kaputt {ref.name}")

    refs = [SourceRef(name="A", kind=SourceKind.CONFLUENCE), SourceRef(name="B", kind=SourceKind.ECH),
            SourceRef(name="C", kind=SourceKind.CONFLUENCE)]
    report = _run(_config(tmp_path), [_spec("S1")], refs, [
        FakeConfluence(lambda r, s: boom(r, s) if r.name == "A" else _unchanged(r, s)),
        FakeMonitor(SourceKind.ECH, boom)])
    kinds = [r.classification for r in report.analysed[0].sources]
    assert kinds == [Classification.ERROR, Classification.ERROR, Classification.UNCHANGED]
    assert "kaputt B" in report.analysed[0].sources[1].errors[0]


def test_workers_one_is_strictly_sequential(tmp_path, patched, mocker):
    pool_cls = mocker.patch.object(runner, "ThreadPoolExecutor", side_effect=AssertionError("no pool with workers=1"))
    tracker = Tracker()

    def fn(ref, spec):
        tracker.enter(ref.name)
        time.sleep(0.01)
        tracker.leave()
        return _changed(ref, spec)

    refs = [SourceRef(name=n, kind=SourceKind.CONFLUENCE) for n in "ABC"]
    cfg = _config(tmp_path)
    cfg.spec_monitor.workers = 1
    report = _run(cfg, [_spec("S1")], refs, [FakeConfluence(fn)])
    pool_cls.assert_not_called()
    assert tracker.max_active == 1 and tracker.order == ["A", "B", "C"]
    assert [r.diff_summary for r in report.analysed[0].sources] == ["Zusammenfassung"] * 3


def test_diff_summaries_run_in_pool_with_isolated_errors(tmp_path, patched):
    barrier = threading.Barrier(2, timeout=5)

    def summarise(result, llm, language, cache):
        if result.ref.name == "C":
            raise AnalysisError("timeout")
        barrier.wait()  # A and B must be summarised concurrently
        return f"sum {result.ref.name}"

    runner.analysis.summarize_source_diff.side_effect = summarise
    refs = [SourceRef(name=n, kind=SourceKind.CONFLUENCE) for n in "ABC"]
    report = _run(_config(tmp_path), [_spec("S1")], refs, [FakeConfluence(_changed)])
    a = report.analysed[0]
    assert [r.diff_summary for r in a.sources] == ["sum A", "sum B", None]
    assert a.analysis_incomplete and [e.scope for e in a.errors] == ["S1: C"]
    assert "diff summary unavailable: timeout" in a.sources[2].warnings


def test_spec_llm_with_configured_timeout_is_used_for_every_analysis_call(tmp_path, patched, mocker):
    mocker.patch.object(runner, "find_sources_sentence", return_value="Quellen: X")
    extract = mocker.patch.object(runner.analysis, "extract_sources_with_llm",
                                  return_value=[SourceRef(name="X", kind=SourceKind.CONFLUENCE)])
    cfg = _config(tmp_path)
    cfg.spec_monitor.llm_timeout = 77
    runner.discover_specs.return_value = Discovery(specs=[_spec("S1")])
    runner.parse_sources.return_value = ParsedSources(found=False)
    runner.run_spec_monitor(cfg, client=MagicMock(), progress=lambda _m: None,
                            monitors=[FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    llms = [extract.call_args[0][1], runner.analysis.summarize_source_diff.call_args[0][1],
            runner.analysis.synthesize_spec.call_args[0][2]]
    for llm in llms:
        assert isinstance(llm, LLMConfig) and llm.llm_timeout == 77
        assert llm.models == ["m"] and llm.api_key == "k"
    assert cfg.llm.llm_timeout == 30


def test_summaries_use_summary_models_and_everything_else_the_heavy_ones(tmp_path, patched, mocker):
    mocker.patch.object(runner, "find_sources_sentence", return_value="Quellen: X")
    extract = mocker.patch.object(runner.analysis, "extract_sources_with_llm",
                                  return_value=[SourceRef(name="X", kind=SourceKind.CONFLUENCE)])
    cfg = _config(tmp_path)
    cfg.llm = LLMConfig(provider="openai", api_key="k", models=["small"], llm_timeout=30,
                        heavy_models=["big"])
    runner.discover_specs.return_value = Discovery(specs=[_spec("S1")])
    runner.parse_sources.return_value = ParsedSources(found=False)
    runner.run_spec_monitor(cfg, client=MagicMock(), progress=lambda _m: None,
                            monitors=[FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    summary_llm = runner.analysis.summarize_source_diff.call_args[0][1]
    assert summary_llm.models == ["small"]
    for llm in (extract.call_args[0][1], runner.analysis.synthesize_spec.call_args[0][2]):
        assert llm.models == ["big"]


def test_summary_models_fall_back_to_standard_models_when_unset(tmp_path, patched):
    cfg = _config(tmp_path)
    _run(cfg, [_spec("S1")], [SourceRef(name="A", kind=SourceKind.CONFLUENCE)],
         [FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    assert runner.analysis.summarize_source_diff.call_args[0][1].models == ["m"]
    assert runner.analysis.synthesize_spec.call_args[0][2].models == ["m"]


def _cut_changed(ref, spec):
    return SourceResult(ref=ref, classification=Classification.CHANGED, diff_text="+ " + "x" * 20000)


def test_cosmetic_verdict_keeps_source_with_diagram_changes(tmp_path, patched):
    runner.analysis.summarize_source_diff.return_value = ""

    def with_diagram(ref, spec):
        return SourceResult(ref=ref, classification=Classification.CHANGED, diff_text="- a\n+ b",
                            diagram_notes=["diagram 'x': Node added: Q"])
    report = _run(_config(tmp_path), [_spec("S1")], [SourceRef(name="A", kind=SourceKind.CONFLUENCE)],
                  [FakeMonitor(SourceKind.CONFLUENCE, with_diagram)])
    assert report.analysed[0].sources[0].classification == Classification.CHANGED


def test_synthesis_warnings_reach_the_report(tmp_path, patched):
    runner.analysis.synthesize_spec.return_value = SpecSynthesis(
        change_request="CR", delta_changes=[], warnings=["the model returned no delta changes"])
    report = _run(_config(tmp_path), [_spec("S1")], [SourceRef(name="A", kind=SourceKind.CONFLUENCE)],
                  [FakeMonitor(SourceKind.CONFLUENCE, _changed)])
    a = report.analysed[0]
    assert "the model returned no delta changes" in a.completeness_warnings
