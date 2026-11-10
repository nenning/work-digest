import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from digest import usage
from digest.config import LLMConfig, RetryConfig, VisionLLMConfig
from digest.spec_monitor import analysis, cli, http, vision
from digest.spec_monitor.cache import SpecCache


@pytest.fixture(autouse=True)
def clean():
    usage.stop()
    yield
    usage.stop()


def test_everything_is_a_noop_until_started():
    with usage.llm_call("p", "m"):
        usage.note_tokens(1, 2)
    usage.record_http("http://x/y", attempts=1, status=200, failed=False, seconds=0.1)
    usage.record_cache("ns", True)
    usage.mark_unusable("p", "m")
    assert usage.current() is None


def test_llm_call_counts_calls_failures_and_tokens():
    stats = usage.start()
    with usage.llm_call("vision", "opus"):
        usage.note_tokens(100, 20)
    with pytest.raises(ValueError):
        with usage.llm_call("vision", "opus"):
            raise ValueError("boom")
    with usage.llm_call("change_request", "sonnet"):
        usage.note_tokens(None, None)          # endpoint without usage data
    row = stats.llm[("vision", "opus")]
    assert (row.calls, row.failed, row.tokens_in, row.tokens_out, row.with_usage) == (2, 1, 100, 20, 1)
    other = stats.llm[("change_request", "sonnet")]
    assert (other.calls, other.with_usage, other.tokens_in) == (1, 0, 0)


def test_note_tokens_outside_a_call_is_ignored():
    stats = usage.start()
    usage.note_tokens(5, 5)
    assert not stats.llm


def test_http_recording_through_request_with_retry(monkeypatch):
    stats = usage.start()
    queue = [requests.ConnectionError("x"), SimpleNamespace(status_code=200, headers={}),
             SimpleNamespace(status_code=404, headers={}),
             requests.Timeout("a"), requests.Timeout("b")]

    def fake_request(*args, **kwargs):
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http.requests, "request", fake_request)
    retry = RetryConfig(attempts=2, backoff_seconds=0)
    http.request_with_retry("GET", "https://a.example/p", retry=retry, sleep=lambda s: None)   # retried once, then 200
    http.request_with_retry("GET", "https://a.example/q", retry=retry, sleep=lambda s: None)   # 404, no retry
    with pytest.raises(http.RequestFailed):
        http.request_with_retry("GET", "https://b.example/r", retry=retry, sleep=lambda s: None)
    a, b = stats.http["a.example"], stats.http["b.example"]
    assert (a.requests, a.attempts, a.retried, a.failed, a.http_errors) == (2, 3, 1, 0, 1)
    assert (b.requests, b.attempts, b.retried, b.failed) == (1, 2, 1, 1)


def test_cache_hits_and_misses_per_namespace(tmp_path):
    stats = usage.start()
    cache = SpecCache(tmp_path)
    assert cache.get_json("vision", "k") is None
    cache.put_json("vision", "k", {"a": 1})
    assert cache.get_json("vision", "k") == {"a": 1}
    assert cache.get_text("summary", "z") is None
    assert stats.cache["vision"] == [1, 1] and stats.cache["summary"] == [0, 1]
    assert (cache.hits, cache.misses) == (1, 2)


def _llm(provider="anthropic"):
    return LLMConfig(provider=provider, api_key="k", models=["m1", "m2"], llm_timeout=5)


def test_analysis_attributes_calls_tokens_and_unusable_replies_to_a_purpose(mocker):
    stats = usage.start()
    replies = iter(["not json", json.dumps({"ok": 1}), ])

    def fake(prompt, config, model, timeout, max_tokens=500):
        usage.note_tokens(10, 3)
        return next(replies)

    mocker.patch("digest.spec_monitor.analysis.summarizer._call_anthropic", side_effect=fake)
    result, _ = analysis._ask_json("p", _llm(), 5, 100, lambda d: d, "change_request")
    assert result == {"ok": 1}
    row = stats.llm[("change_request", "m1")]
    assert (row.calls, row.unusable, row.tokens_in, row.tokens_out) == (2, 1, 20, 6)


def test_low_level_clients_report_token_usage(mocker):
    stats = usage.start()
    anth = MagicMock()
    anth.messages.create.return_value = MagicMock(content=[MagicMock(text="t")], usage=SimpleNamespace(input_tokens=7, output_tokens=2))
    mocker.patch("digest.summarizer.anthropic.Anthropic", return_value=anth)
    with usage.llm_call("p", "a"):
        analysis.summarizer._call_anthropic("x", _llm(), "a", 5)
    oai = MagicMock()
    oai.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="t"))], usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1))
    mocker.patch("digest.summarizer.openai.OpenAI", return_value=oai)
    with usage.llm_call("p", "o"):
        analysis.summarizer._call_openai("x", _llm("openai"), "o", 5)
    assert (stats.llm[("p", "a")].tokens_in, stats.llm[("p", "a")].tokens_out) == (7, 2)
    assert (stats.llm[("p", "o")].tokens_in, stats.llm[("p", "o")].tokens_out) == (4, 1)


def test_vision_calls_are_recorded_with_their_model(mocker):
    stats = usage.start()
    client = MagicMock()
    client.messages.create.return_value = MagicMock(
        content=[MagicMock(text=json.dumps({"summary": "s", "changes": [], "differences": False, "confidence": "high"}))],
        usage=SimpleNamespace(input_tokens=900, output_tokens=40))
    mocker.patch("digest.spec_monitor.vision.anthropic.Anthropic", return_value=client)
    vcfg = VisionLLMConfig(provider="anthropic", api_key="k", model="claude-opus-5-5")
    vision.compare_images(b"\x89PNG-o", b"\x89PNG-n", diagram_name="d", context="", vision=vcfg, language="en")
    row = stats.llm[("vision", "claude-opus-5-5")]
    assert (row.calls, row.failed, row.tokens_in, row.tokens_out) == (1, 0, 900, 40)


def test_report_lists_purposes_models_hosts_and_cache():
    stats = usage.start()
    with usage.llm_call("vision", "claude-opus-5-5"):
        usage.note_tokens(1500, 80)
    with usage.llm_call("source_summary", "claude-haiku-5-5"):
        usage.note_tokens(200, 30)
    usage.mark_unusable("source_summary", "claude-haiku-5-5")
    usage.record_http("https://x.atlassian.net/wiki/rest", attempts=2, status=200, failed=False, seconds=0.5)
    usage.record_cache("vision", True)
    usage.record_cache("vision", False)
    text = usage.format_report(stats)
    assert "Run statistics" in text
    assert "LLM calls: 2, 1 unusable reply(ies); tokens in 1,700 / out 110" in text
    assert "vision" in text and "claude-opus-5-5" in text and "source_summary" in text
    assert "per model: claude-haiku-5-5 1 call(s), 200 in / 30 out; claude-opus-5-5 1 call(s), 1,500 in / 80 out" in text
    assert "HTTP requests: 1 (1 retried)" in text and "x.atlassian.net" in text
    assert "Cache: 1 hit(s), 1 miss(es)" in text and "vision 1/2" in text


def test_report_without_activity_and_without_token_info():
    stats = usage.start()
    assert "LLM calls: 0" in usage.format_report(stats) and "HTTP requests: 0" in usage.format_report(stats)
    with usage.llm_call("p", "m"):
        pass
    assert "tokens not reported" in usage.format_report(stats)


def test_dispatch_prints_statistics_even_when_the_run_fails(mocker, capsys):
    def boom(args, config, cache_file, command_line):
        with usage.llm_call("vision", "m"):
            pass
        raise RuntimeError("run failed")

    mocker.patch.object(cli, "_dispatch", side_effect=boom)
    with pytest.raises(RuntimeError):
        cli.dispatch(SimpleNamespace(), SimpleNamespace(), None, "cmd")
    out = capsys.readouterr().out
    assert "Run statistics" in out and "vision" in out
    assert usage.current() is None


def test_html_report_lists_the_same_tables_escaped():
    stats = usage.start()
    with usage.llm_call("vision", "claude-opus-5-5"):
        usage.note_tokens(1500, 80)
    with usage.llm_call("source_summary", "<haiku>"):
        usage.note_tokens(200, 30)
    usage.record_http("https://x.atlassian.net/wiki/rest", attempts=2, status=404, failed=False, seconds=0.5)
    usage.record_cache("vision", True)
    usage.record_cache("vision", False)
    out = usage.format_html(stats)
    assert 'id="run-statistics"' in out and "claude-opus-5-5" in out and "&lt;haiku&gt;" in out and "<haiku>" not in out
    assert "tokens in 1,700 / out 110" in out and "Per model" in out
    assert "x.atlassian.net" in out and "1 retried" in out and "1 HTTP error status" in out
    assert "1 hit(s), 1 miss(es)" in out


def test_html_report_without_activity():
    out = usage.format_html(usage.start())
    assert "No LLM calls." in out and "No HTTP requests." in out and "Cache" not in out


def test_dry_run_preview_contains_statistics_above_the_footer(tmp_path, mocker):
    from datetime import datetime
    from digest.spec_monitor.models import RunReport
    mocker.patch.object(cli, "run_spec_monitor", return_value=RunReport(generated_at=datetime(2026, 11, 10, 12).astimezone()))
    mocker.patch.object(cli, "render_mail_html", return_value='<html><div class="footer">f</div></html>')
    mocker.patch.object(cli, "build_subject", return_value="S")
    config = SimpleNamespace(spec_monitor=SimpleNamespace(recipient=None, publish_parent_page_id=None),
                             mgmt_summary=SimpleNamespace(recipient=None), email=SimpleNamespace(recipient=None, subject_prefix="[D]"),
                             data_dir=tmp_path)
    usage.start()
    cli.run_cli(SimpleNamespace(spec=None, publish_page=False, dry_run=True), config, tmp_path / "t", "cmd")
    page = (tmp_path / "spec_cache" / "preview.html").read_text(encoding="utf-8")
    assert page.index("Run statistics") < page.index('<div class="footer">')


def test_mail_body_has_no_statistics_without_active_collection():
    assert cli._with_statistics("<html>x</html>") == "<html>x</html>"
