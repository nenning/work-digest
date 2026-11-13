from types import SimpleNamespace

import pytest

from digest.config import LLMConfig, _load_page_watch, effective_vision_llm, spec_llm


def test_defaults():
    cfg = _load_page_watch({"parent_page_id": 41484320})
    assert cfg.parent_page_id == "41484320"
    assert cfg.window_days == 14
    assert cfg.include_parent is True
    assert cfg.label is None and cfg.recipient is None and cfg.publish_parent_page_id is None
    assert cfg.workers == 4 and cfg.llm_timeout == 240
    assert cfg.retry.timeout_seconds == 60


def test_absent_block_is_none():
    assert _load_page_watch(None) is None
    assert _load_page_watch({}) is None


def test_values_are_read():
    cfg = _load_page_watch({
        "parent_page_id": "1", "window_days": 7, "label": "core", "include_parent": False,
        "recipient": "a@b.c", "workers": 2, "llm_timeout": 100,
        "retry": {"attempts": 5, "backoff_seconds": 1, "timeout_seconds": 30},
        "confluence_page": {"parent_page_id": 99},
    })
    assert (cfg.window_days, cfg.label, cfg.include_parent, cfg.recipient) == (7, "core", False, "a@b.c")
    assert (cfg.workers, cfg.llm_timeout, cfg.retry.attempts, cfg.publish_parent_page_id) == (2, 100, 5, "99")


def test_parent_is_required():
    with pytest.raises(ValueError, match="parent_page_id"):
        _load_page_watch({"window_days": 7})


@pytest.mark.parametrize("value", [0, -3, "x", 1.5, True, None])
def test_window_days_must_be_positive_integer(value):
    with pytest.raises(ValueError, match="window_days"):
        _load_page_watch({"parent_page_id": "1", "window_days": value})


def test_non_positive_workers_and_timeouts_rejected():
    with pytest.raises(ValueError, match="workers"):
        _load_page_watch({"parent_page_id": "1", "workers": 0})
    with pytest.raises(ValueError, match="llm_timeout"):
        _load_page_watch({"parent_page_id": "1", "llm_timeout": 0})
    with pytest.raises(ValueError, match="timeout_seconds"):
        _load_page_watch({"parent_page_id": "1", "retry": {"timeout_seconds": 0}})


def _config(spec_monitor=None, page_watch=None):
    return SimpleNamespace(
        llm=LLMConfig(provider="openai", api_key="k", models=["m"], llm_timeout=30),
        spec_monitor=spec_monitor, page_watch=page_watch,
    )


def test_spec_llm_uses_page_watch_timeout_without_spec_monitor():
    pw = _load_page_watch({"parent_page_id": "1", "llm_timeout": 111})
    assert spec_llm(_config(page_watch=pw)).llm_timeout == 111
    assert spec_llm(_config(page_watch=pw), "summary").llm_timeout == 111
    assert effective_vision_llm(_config(page_watch=pw)).timeout == 111


def test_spec_llm_prefers_spec_monitor_and_falls_back_to_llm_timeout():
    sm = SimpleNamespace(llm_timeout=222)
    pw = _load_page_watch({"parent_page_id": "1", "llm_timeout": 111})
    assert spec_llm(_config(spec_monitor=sm, page_watch=pw)).llm_timeout == 222
    assert spec_llm(_config()).llm_timeout == 30
