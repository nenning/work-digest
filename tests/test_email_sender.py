"""Tests for digest.email_sender."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from digest.config import EmailConfig, SmtpConfig
from digest.email_sender import (
    TEMPLATES_DIR, _render_html, _safe_url,
    send_via_smtp, send_mgmt_summary_via_smtp,
)
from digest.models import SourceItem, SummarizedItem


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


def _default_smtp() -> SmtpConfig:
    return SmtpConfig(host="smtp.example.com", username="user@example.com")


_FIXED_NOW = datetime(2026, 4, 9, 14, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# send_via_smtp: returns False when no items
# ---------------------------------------------------------------------------

def test_smtp_returns_false_when_no_items():
    result = send_via_smtp(
        items=[],
        config=_default_config(),
        smtp_cfg=_default_smtp(),
        recipient="user@example.com",
    )
    assert result is False


# ---------------------------------------------------------------------------
# send_via_smtp: dry_run prints "DRY RUN", does not connect to SMTP
# ---------------------------------------------------------------------------

def test_smtp_dry_run_does_not_connect(capsys):
    item = _make_item()
    with patch("smtplib.SMTP") as mock_smtp_cls:
        result = send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
            dry_run=True,
            now=_FIXED_NOW,
        )
    assert result is True
    mock_smtp_cls.assert_not_called()
    assert "DRY RUN" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# send_via_smtp: connects, authenticates, and sends
# ---------------------------------------------------------------------------

def test_smtp_sends_email():
    item = _make_item()
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)

    with (
        patch("smtplib.SMTP", return_value=mock_smtp),
        patch("keyring.get_password", return_value="secret"),
    ):
        result = send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
            now=_FIXED_NOW,
        )

    assert result is True
    mock_smtp.starttls.assert_called_once()
    mock_smtp.login.assert_called_once_with("user@example.com", "secret")
    mock_smtp.sendmail.assert_called_once()


# ---------------------------------------------------------------------------
# send_via_smtp: missing keyring password raises RuntimeError
# ---------------------------------------------------------------------------

def test_smtp_missing_password_raises():
    item = _make_item()
    with patch("keyring.get_password", return_value=None):
        with pytest.raises(RuntimeError, match="setup-smtp-auth"):
            send_via_smtp(
                items=[item],
                config=_default_config(),
                smtp_cfg=_default_smtp(),
                recipient="user@example.com",
            )


# ---------------------------------------------------------------------------
# send_via_smtp: uses sender from smtp_cfg when set
# ---------------------------------------------------------------------------

def test_smtp_uses_configured_sender():
    item = _make_item()
    smtp = SmtpConfig(host="smtp.example.com", username="user@example.com", sender="noreply@example.com")
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)

    with (
        patch("smtplib.SMTP", return_value=mock_smtp),
        patch("keyring.get_password", return_value="secret"),
    ):
        send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=smtp,
            recipient="user@example.com",
            now=_FIXED_NOW,
        )

    sendmail_call = mock_smtp.sendmail.call_args
    assert sendmail_call.args[0] == "noreply@example.com"


# ---------------------------------------------------------------------------
# send_via_smtp: subject contains prefix and item count
# ---------------------------------------------------------------------------

def test_smtp_subject_contains_prefix_and_count(capsys):
    item = _make_item()
    send_via_smtp(
        items=[item],
        config=_default_config(),
        smtp_cfg=_default_smtp(),
        recipient="user@example.com",
        dry_run=True,
        now=_FIXED_NOW,
    )
    out = capsys.readouterr().out
    assert "[Digest]" in out
    assert "1 item" in out


# ---------------------------------------------------------------------------
# send_mgmt_summary_via_smtp: dry_run prints narrative
# ---------------------------------------------------------------------------

def _make_source_item(kind: str = "ticket_done") -> SourceItem:
    return SourceItem(
        source="jira",
        kind=kind,
        title="PROJ-1",
        url="https://example.com/PROJ-1",
        content="done",
        author="Alice",
        timestamp=_FIXED_NOW,
    )


def test_smtp_mgmt_summary_dry_run(capsys):
    with patch("smtplib.SMTP") as mock_smtp_cls:
        result = send_mgmt_summary_via_smtp(
            narrative="Team did great work.",
            jira_items=[_make_source_item()],
            confluence_items=[],
            subject="[Team Summary]",
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
            dry_run=True,
        )
    assert result is True
    mock_smtp_cls.assert_not_called()
    assert "Team did great work." in capsys.readouterr().out


def test_smtp_mgmt_summary_sends():
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)

    with (
        patch("smtplib.SMTP", return_value=mock_smtp),
        patch("keyring.get_password", return_value="secret"),
    ):
        result = send_mgmt_summary_via_smtp(
            narrative="Team did great work.",
            jira_items=[_make_source_item()],
            confluence_items=[],
            subject="[Team Summary]",
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
        )
    assert result is True
    mock_smtp.sendmail.assert_called_once()


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
# SMTP error is caught, returns False
# ---------------------------------------------------------------------------

def test_smtp_error_returns_false():
    import smtplib
    item = _make_item()
    with (
        patch("smtplib.SMTP", side_effect=smtplib.SMTPException("connect failed")),
        patch("keyring.get_password", return_value="secret"),
    ):
        result = send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
            now=_FIXED_NOW,
        )
    assert result is False


# ---------------------------------------------------------------------------
# items with unknown source are excluded from sections
# ---------------------------------------------------------------------------

def test_unknown_source_excluded():
    unknown_item = _make_item(source="slack")
    jira_item = _make_item(source="jira")
    result = send_via_smtp(
        items=[unknown_item, jira_item],
        config=_default_config(),
        smtp_cfg=_default_smtp(),
        recipient="user@example.com",
        dry_run=True,
        now=_FIXED_NOW,
    )
    assert result is True
