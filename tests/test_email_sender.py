"""Tests for digest.email_sender."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from digest.config import EmailConfig
from digest.email_sender import (
    TEMPLATES_DIR, _render_html, _safe_url, send_digest
)
from digest.models import SummarizedItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    source: str = "jira",
    priority: str = "info",
    title: str = "Test ticket",
    url: str = "https://example.com/1",
    summary: str = "Something happened.",
) -> SummarizedItem:
    return SummarizedItem(
        source=source,
        kind="comment",
        title=title,
        url=url,
        summary=summary,
        author="Alice",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        priority=priority,
    )


def _default_config() -> EmailConfig:
    return EmailConfig(subject_prefix="[Digest]")


_FIXED_NOW = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test 1: returns False when no items
# ---------------------------------------------------------------------------

def test_returns_false_when_no_items():
    result = send_digest(
        items=[],
        config=_default_config(),
        m365_token="tok",
        recipient="user@example.com",
    )
    assert result is False


# ---------------------------------------------------------------------------
# Test 2: dry_run does not call Graph API; prints "DRY RUN" to stdout
# ---------------------------------------------------------------------------

def test_dry_run_does_not_call_graph(capsys):
    item = _make_item()
    with patch("digest.email_sender.requests.post") as mock_post:
        result = send_digest(
            items=[item],
            config=_default_config(),
            m365_token="tok",
            recipient="user@example.com",
            dry_run=True,
            now=_FIXED_NOW,
        )

    assert result is True
    mock_post.assert_not_called()

    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out


# ---------------------------------------------------------------------------
# Test 3: sends via Graph API with correct URL and Authorization header
# ---------------------------------------------------------------------------

def test_sends_via_graph_api():
    item = _make_item()
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None

    with patch("digest.email_sender.requests.post", return_value=mock_resp) as mock_post:
        result = send_digest(
            items=[item],
            config=_default_config(),
            m365_token="my-token",
            recipient="user@example.com",
            dry_run=False,
            now=_FIXED_NOW,
        )

    assert result is True
    mock_post.assert_called_once()

    call_kwargs = mock_post.call_args

    # Positional arg 0 is the URL
    url = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("url", "")
    assert "sendMail" in url

    # Headers contain Authorization
    headers = call_kwargs.kwargs.get("headers", {})
    assert "Authorization" in headers
    assert "my-token" in headers["Authorization"]


# ---------------------------------------------------------------------------
# Test 4: subject contains prefix and correct item count
# ---------------------------------------------------------------------------

def test_subject_contains_prefix_and_count(capsys):
    item = _make_item()
    send_digest(
        items=[item],
        config=_default_config(),
        m365_token="tok",
        recipient="user@example.com",
        dry_run=True,
        now=_FIXED_NOW,
    )

    captured = capsys.readouterr()
    assert "[Digest]" in captured.out
    assert "1 item" in captured.out


# ---------------------------------------------------------------------------
# Test 5: action_needed CSS class appears in rendered HTML (via _render_html)
# ---------------------------------------------------------------------------

def test_action_needed_class_in_html():
    item = _make_item(priority="action_needed")
    sections = {"updates": [item]}  # kind="comment" maps to "updates" group
    html = _render_html(sections, "Test subject")
    assert "action_needed" in html


# ---------------------------------------------------------------------------
# Test 6: safe_url blocks javascript: scheme
# ---------------------------------------------------------------------------

def test_safe_url_blocks_javascript():
    assert _safe_url("javascript:alert(1)") == "#"


def test_safe_url_allows_https():
    url = "https://example.atlassian.net/browse/PROJ-1"
    assert _safe_url(url) == url


def test_safe_url_allows_http():
    assert _safe_url("http://intranet.local/page") == "http://intranet.local/page"


def test_safe_url_blocks_data_uri():
    assert _safe_url("data:text/html,<script>evil()</script>") == "#"


# ---------------------------------------------------------------------------
# Test 7: Graph API error is caught, returns False
# ---------------------------------------------------------------------------

def test_graph_error_returns_false():
    item = _make_item()
    with patch("digest.email_sender.requests.post") as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError("timeout")
        result = send_digest(
            items=[item],
            config=_default_config(),
            m365_token="tok",
            recipient="user@example.com",
            now=_FIXED_NOW,
        )
    assert result is False


# ---------------------------------------------------------------------------
# Test 8: items with unknown source are excluded from sections
# ---------------------------------------------------------------------------

def test_unknown_source_excluded():
    unknown_item = _make_item(source="slack")  # not in SOURCE_ORDER
    jira_item = _make_item(source="jira")
    result = send_digest(
        items=[unknown_item, jira_item],
        config=_default_config(),
        m365_token="tok",
        recipient="user@example.com",
        dry_run=True,
        now=_FIXED_NOW,
    )
    # slack item dropped, jira item present → still sends
    assert result is True
