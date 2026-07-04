"""Tests for the LLM summarizer module."""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from markupsafe import Markup

from digest.config import LLMConfig
from digest.models import SourceItem
from digest.summarizer import _build_prompt, summarize_items, _format_field_change


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    source: str = "jira",
    kind: str = "comment",
    content: str = "A" * 200,
    priority: str = "info",
) -> SourceItem:
    return SourceItem(
        source=source,
        kind=kind,
        title="Test title",
        url="https://example.com/1",
        content=content,
        author="Anna",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        priority=priority,
    )


def _openai_config() -> LLMConfig:
    return LLMConfig(provider="openai", api_key="sk-test", models=["gpt-4o"])


def _anthropic_config() -> LLMConfig:
    return LLMConfig(provider="anthropic", api_key="ant-test", models=["claude-3-5-sonnet-20241022"])


# ---------------------------------------------------------------------------
# Test 1: openai client returns summary + priority
# ---------------------------------------------------------------------------

def test_summarize_openai():
    item = _make_item(source="jira", content="A" * 200)
    config = _openai_config()

    llm_reply = json.dumps({"summary": "Anna commented that the fix works.", "priority": "fyi"})

    mock_message = MagicMock()
    mock_message.content = llm_reply
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client

        results = summarize_items([item], config)

    assert len(results) == 1
    assert results[0].summary == "Anna commented that the fix works."
    # Non-outlook items preserve original priority regardless of what the LLM returns.
    assert results[0].priority == "info"


# ---------------------------------------------------------------------------
# Test 2: outlook prompt includes priority instruction
# ---------------------------------------------------------------------------

def test_outlook_prompt_includes_priority_instruction():
    item = _make_item(source="outlook", kind="email", content="A" * 200)
    prompt = _build_prompt(item)

    assert "action_needed" in prompt
    assert "meeting_invite" in prompt
    assert "fyi" in prompt
    assert "info" in prompt


# ---------------------------------------------------------------------------
# Test 3: non-outlook prompt excludes priority instruction
# ---------------------------------------------------------------------------

def test_non_outlook_prompt_excludes_priority_instruction():
    item = _make_item(source="jira", kind="comment", content="A" * 200)
    prompt = _build_prompt(item)

    assert "action_needed" not in prompt


# ---------------------------------------------------------------------------
# Test 4: short content prompt says verbatim
# ---------------------------------------------------------------------------

def test_short_content_prompt_says_verbatim():
    item = _make_item(content="Short text")  # < 100 chars
    assert len(item.content) < 100
    prompt = _build_prompt(item)

    assert "verbatim" in prompt


# ---------------------------------------------------------------------------
# Test 5: long content prompt says summarize
# ---------------------------------------------------------------------------

def test_long_content_prompt_says_summarize():
    item = _make_item(content="A" * 200)  # >= 100 chars
    assert len(item.content) >= 100
    prompt = _build_prompt(item)

    assert "summary" in prompt.lower() or "summarize" in prompt.lower()


# ---------------------------------------------------------------------------
# Test 5b: Jira single mention prompt unchanged
# ---------------------------------------------------------------------------

def test_jira_single_mention_prompt_unchanged():
    item = SourceItem(
        source="jira", kind="mention",
        title="PROJ-1: Fix bug", url="https://example.com/1",
        content="please check this",
        author="Anna",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        metadata={"mention_author": "Anna"},
    )
    prompt = _build_prompt(item)
    assert "Erwähnt von: Anna" in prompt


# ---------------------------------------------------------------------------
# Test 5c: Jira merged mention prompt lists all authors
# ---------------------------------------------------------------------------

def test_jira_merged_mention_prompt_lists_all_authors():
    item = SourceItem(
        source="jira", kind="mention",
        title="PROJ-1: Fix bug", url="https://example.com/1",
        content="[Erwähnt von Marco] first\n\n[Erwähnt von Anna] second",
        author="Anna",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        metadata={"mention_authors": ["Marco", "Anna"]},
    )
    prompt = _build_prompt(item)
    assert "Marco" in prompt
    assert "Anna" in prompt
    assert "insgesamt" in prompt


# ---------------------------------------------------------------------------
# Test 6: malformed JSON falls back to raw text
# ---------------------------------------------------------------------------

def test_malformed_json_falls_back():
    item = _make_item(source="jira", content="A" * 200)
    config = _openai_config()

    raw_text = "not json"

    mock_message = MagicMock()
    mock_message.content = raw_text
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client

        results = summarize_items([item], config)

    assert results[0].summary == "not json"
    assert results[0].priority == item.priority


def test_json_shaped_response_with_stray_quote_is_salvaged():
    item = _make_item(source="confluence", kind="mention", content="A" * 200)
    config = _openai_config()

    raw_text = (
        '{ "summary": "Christian hät dich erwähnt und fragt zur Bestätigung: '
        '„D.h. ich lass den Text so. Korrekt?"" }'
    )

    mock_message = MagicMock()
    mock_message.content = raw_text
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client

        results = summarize_items([item], config)

    assert "{" not in results[0].summary
    assert "\"summary\"" not in results[0].summary
    assert results[0].summary == (
        "Christian hät dich erwähnt und fragt zur Bestätigung: "
        "„D.h. ich lass den Text so. Korrekt?\""
    )
    assert results[0].priority == item.priority


# ---------------------------------------------------------------------------
# Test 7: anthropic client returns correct summary
# ---------------------------------------------------------------------------

def test_anthropic_client():
    item = _make_item(source="teams", content="A" * 200)
    config = _anthropic_config()

    llm_reply = json.dumps({"summary": "A ticket was created for the new feature.", "priority": "info"})

    mock_text_block = MagicMock()
    mock_text_block.text = llm_reply
    mock_response = MagicMock()
    mock_response.content = [mock_text_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("digest.summarizer.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = mock_client

        results = summarize_items([item], config)

    assert len(results) == 1
    assert results[0].summary == "A ticket was created for the new feature."
    assert results[0].priority == "info"


# ---------------------------------------------------------------------------
# Test 8: invalid outlook priority falls back to original
# ---------------------------------------------------------------------------

def test_invalid_outlook_priority_falls_back():
    item = _make_item(source="outlook", kind="email", content="A" * 200, priority="info")
    config = _openai_config()

    llm_reply = json.dumps({"summary": "Budget email.", "priority": "definitely_not_valid"})
    mock_message = MagicMock()
    mock_message.content = llm_reply
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        results = summarize_items([item], config)

    assert results[0].priority == "info"


# ---------------------------------------------------------------------------
# Test 9: LLM API error is caught; item gets error summary, run continues
# ---------------------------------------------------------------------------

def test_llm_error_caught_and_continues():
    import threading
    items = [
        _make_item(source="jira", content="A" * 200),
        _make_item(source="teams", content="B" * 200),
    ]
    config = _openai_config()

    call_lock = threading.Lock()
    call_count = 0

    def flaky_create(**kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            count = call_count
        if count == 1:
            raise RuntimeError("rate limit hit")
        m = MagicMock()
        m.choices[0].message.content = json.dumps({"summary": "Second item summary."})
        return m

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = flaky_create

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        results = summarize_items(items, config)

    assert len(results) == 2
    summaries = [r.summary for r in results]
    assert any("llm error" in s.lower() for s in summaries)
    assert any(s == "Second item summary." for s in summaries)


# ---------------------------------------------------------------------------
# Test 10: unknown provider raises ValueError
# ---------------------------------------------------------------------------

def test_unknown_provider_raises():
    item = _make_item()
    config = LLMConfig(provider="unknown_llm", api_key="x", models=["m"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        summarize_items([item], config)


# ---------------------------------------------------------------------------
# Test 11: new_ticket with description calls LLM, summary starts with "Neu."
# ---------------------------------------------------------------------------

def test_new_ticket_with_description_calls_llm():
    from datetime import datetime, timezone
    item = SourceItem(
        source="jira", kind="new_ticket",
        title="PROJ-1: Fix login bug",
        url="https://example.com/browse/PROJ-1",
        content="",
        author="Bob",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        metadata={"assignee": "Alice", "description": "Users cannot log in when using SSO. The error occurs after the redirect."},
    )
    config = _openai_config()

    llm_reply = json.dumps({"summary": "Users cannot log in via SSO due to a redirect error."})
    mock_message = MagicMock()
    mock_message.content = llm_reply
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        results = summarize_items([item], config)

    assert len(results) == 1
    assert results[0].summary.startswith("Neu.")
    assert "SSO" in results[0].summary
    mock_client.chat.completions.create.assert_called_once()


# ---------------------------------------------------------------------------
# Test 12: new_ticket without description skips LLM, summary is "Neu."
# ---------------------------------------------------------------------------

def test_new_ticket_without_description_skips_llm():
    from datetime import datetime, timezone
    item = SourceItem(
        source="jira", kind="new_ticket",
        title="PROJ-2: Empty description",
        url="https://example.com/browse/PROJ-2",
        content="",
        author="Bob",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        metadata={"assignee": "Alice", "description": ""},
    )
    config = _openai_config()
    mock_client = MagicMock()

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        results = summarize_items([item], config)

    assert len(results) == 1
    assert results[0].summary == "Neu."
    mock_client.chat.completions.create.assert_not_called()


# ---------------------------------------------------------------------------
# Test 12b: free-text language override picks German labels and is sent
# verbatim to the LLM
# ---------------------------------------------------------------------------

def test_new_ticket_free_text_language_override_picks_german_labels():
    item = SourceItem(
        source="jira", kind="new_ticket",
        title="PROJ-4: Empty description",
        url="https://example.com/browse/PROJ-4",
        content="",
        author="Bob",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        metadata={"assignee": "Alice", "description": ""},
    )
    config = _openai_config()
    mock_client = MagicMock()

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        results = summarize_items([item], config, language="Deutsch (Schweiz)")

    assert results[0].summary == "Neu."


def test_comment_free_text_language_override_used_verbatim_in_prompt():
    item = _make_item(content="A" * 200)
    config = _openai_config()

    llm_reply = json.dumps({"summary": "A comment was made."})
    mock_message = MagicMock()
    mock_message.content = llm_reply
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        summarize_items([item], config, language="English (US)")

    prompt = mock_client.chat.completions.create.call_args[1]["messages"][0]["content"]
    assert "English (US)" in prompt


# ---------------------------------------------------------------------------
# Test 13: assignment items are ignored entirely
# ---------------------------------------------------------------------------

def test_assignment_items_are_ignored():
    items = [
        _make_item(source="jira", kind="assignment", content="Ticket PROJ-3. Status: In Progress."),
        _make_item(source="jira", kind="comment", content="A" * 200),
    ]
    config = _openai_config()

    llm_reply = json.dumps({"summary": "A comment was made."})
    mock_message = MagicMock()
    mock_message.content = llm_reply
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("digest.summarizer.openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        results = summarize_items(items, config)

    assert len(results) == 1
    assert results[0].kind == "comment"


# ---------------------------------------------------------------------------
# Test 14: field_change unblocks note rendered as safe HTML link
# ---------------------------------------------------------------------------

def _field_change_item(changes: list[dict]) -> SourceItem:
    return SourceItem(
        source="jira", kind="field_change", title="PROJ-1: Fix", url="https://example.com/1",
        content="status: In Progress → Done", author="Bob",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        metadata={"changes": changes},
    )


def test_format_field_change_without_unblocks_uses_plain_content():
    item = _field_change_item([{"field": "status", "from": "In Progress", "to": "Done"}])
    result = _format_field_change(item)
    assert result.summary == item.content


def test_format_field_change_renders_unblocks_as_link():
    item = _field_change_item([{
        "field": "status", "from": "In Progress", "to": "Done",
        "unblocks": [{"key": "PROJ-2", "title": "Deploy", "url": "https://example.com/browse/PROJ-2"}],
    }])
    result = _format_field_change(item)
    assert '<a href="https://example.com/browse/PROJ-2">PROJ-2 - Deploy</a>' in result.summary
    assert "status: In Progress → Done (unblocks [" in result.summary


def test_format_field_change_multiple_unblocks_comma_separated():
    item = _field_change_item([{
        "field": "status", "from": "In Progress", "to": "Done",
        "unblocks": [
            {"key": "PROJ-2", "title": "Deploy", "url": "https://example.com/browse/PROJ-2"},
            {"key": "PROJ-3", "title": "Notify", "url": "https://example.com/browse/PROJ-3"},
        ],
    }])
    result = _format_field_change(item)
    assert "[<a href=\"https://example.com/browse/PROJ-2\">PROJ-2 - Deploy</a>], [<a href=\"https://example.com/browse/PROJ-3\">PROJ-3 - Notify</a>]" in result.summary


def test_format_field_change_escapes_html_in_unblocked_title():
    item = _field_change_item([{
        "field": "status", "from": "In Progress", "to": "Done",
        "unblocks": [{"key": "PROJ-2", "title": "<script>alert(1)</script>", "url": "https://example.com/browse/PROJ-2"}],
    }])
    result = _format_field_change(item)
    assert "<script>" not in result.summary
    assert "&lt;script&gt;" in result.summary


def test_format_field_change_renders_remote_link_as_markup():
    item = _field_change_item([{
        "field": "RemoteWorkItemLink", "from": "—",
        "to": Markup('"[<a href="https://confluence.example.com/page">Migrationskonzept</a>]"'),
    }])
    result = _format_field_change(item)
    assert '"[<a href="https://confluence.example.com/page">Migrationskonzept</a>]"' in result.summary


def test_format_field_change_escapes_other_fields_when_rebuilt():
    item = _field_change_item([
        {"field": "assignee", "from": "—", "to": "Anna & Bob"},
        {"field": "status", "from": "In Progress", "to": "Done",
         "unblocks": [{"key": "PROJ-2", "title": "Deploy", "url": "https://example.com/browse/PROJ-2"}]},
    ])
    result = _format_field_change(item)
    assert "Anna &amp; Bob" in result.summary
