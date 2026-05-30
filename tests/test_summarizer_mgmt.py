"""Tests for synthesize_mgmt_summary() in digest.summarizer."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from digest.config import LLMConfig
from digest.models import SourceItem
from digest.summarizer import synthesize_mgmt_summary


def _openai_config(fallback_models=None) -> LLMConfig:
    return LLMConfig(
        provider="openai",
        api_key="sk-test",
        models=["gpt-4o"],
        fallback_models=fallback_models or [],
    )


def _make_jira_item(kind="ticket_done", title="TEAM-1: Do the thing",
                    status="Done", assignee="Alice") -> SourceItem:
    return SourceItem(
        source="jira",
        kind=kind,
        title=title,
        url="https://example.atlassian.net/browse/TEAM-1",
        content=f"Status: {status}. Assignee: {assignee}.",
        author=assignee,
        timestamp=datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc),
        metadata={"status": status, "status_category": "done", "assignee": assignee},
    )


def _make_confluence_item(title="Team Notes") -> SourceItem:
    return SourceItem(
        source="confluence",
        kind="page_update",
        title=title,
        url="https://example.atlassian.net/wiki/pages/1",
        content="Page updated by Alice.",
        author="Alice",
        timestamp=datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc),
    )


def _mock_openai_client(reply="Generated narrative."):
    mock_msg = MagicMock()
    mock_msg.content = reply
    mock_choice = MagicMock()
    mock_choice.message = mock_msg
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp
    return mock_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_synthesize_basic():
    mock_client = _mock_openai_client("The team shipped the feature.")
    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = synthesize_mgmt_summary(
            [_make_jira_item()],
            [_make_confluence_item()],
            _openai_config(),
            label="Sprint 7",
        )
    assert result == "The team shipped the feature."
    mock_client.chat.completions.create.assert_called_once()


def test_synthesize_prompt_contains_section_headers():
    mock_client = _mock_openai_client("ok")
    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        synthesize_mgmt_summary(
            [
                _make_jira_item(kind="ticket_done", title="TEAM-1: Done thing"),
                _make_jira_item(kind="ticket_wip", title="TEAM-2: WIP thing", status="In Progress"),
                _make_jira_item(kind="ticket_todo", title="TEAM-3: Todo thing", status="To Do"),
            ],
            [],
            _openai_config(),
            label="Sprint 7",
        )
    prompt = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"]
    assert "DONE" in prompt
    assert "IN PROGRESS" in prompt
    assert "TODO" in prompt


def test_synthesize_short_flag_prompt():
    mock_client = _mock_openai_client("- Item one\n- Item two")
    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        synthesize_mgmt_summary(
            [_make_jira_item()],
            [],
            _openai_config(),
            label="Sprint 7",
            short=True,
        )
    prompt = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"]
    assert "bullet" in prompt.lower() or "5" in prompt
    assert "paragraph" not in prompt.lower()


def test_synthesize_assume_done_flag():
    mock_client = _mock_openai_client("All done.")
    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        synthesize_mgmt_summary(
            [_make_jira_item(kind="ticket_wip")],
            [],
            _openai_config(),
            label="Sprint 7",
            assume_done=True,
        )
    prompt = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"]
    assert "completed" in prompt.lower() or "accomplished" in prompt.lower()


def test_synthesize_empty_inputs_still_calls_llm():
    mock_client = _mock_openai_client("Nothing to report.")
    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = synthesize_mgmt_summary([], [], _openai_config(), label="Sprint 7")
    assert result == "Nothing to report."
    mock_client.chat.completions.create.assert_called_once()


def test_synthesize_fallback_model_used_when_primary_fails():
    mock_client = _mock_openai_client("Fallback result.")
    call_count = 0

    def _create(**kwargs):
        nonlocal call_count
        call_count += 1
        if kwargs["model"] == "gpt-4o":
            raise RuntimeError("primary failed")
        return mock_client.chat.completions.create.return_value

    mock_client.chat.completions.create.side_effect = _create

    cfg = LLMConfig(provider="openai", api_key="sk-test", models=["gpt-4o"], fallback_models=["gpt-3.5-turbo"])
    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = synthesize_mgmt_summary([], [], cfg, label="Sprint 7")
    assert result == "Fallback result."
    assert call_count == 2


def test_synthesize_all_models_fail_raises():
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("boom")

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        with pytest.raises(RuntimeError, match="All LLM models failed"):
            synthesize_mgmt_summary([], [], _openai_config(), label="Sprint 7")


def test_synthesize_language_english():
    mock_client = _mock_openai_client("Summary in English.")
    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        synthesize_mgmt_summary([], [], _openai_config(), label="Sprint 7", language="en")
    prompt = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"]
    assert "English" in prompt
    assert "German" not in prompt and "Deutsch" not in prompt
