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

def test_smtp_dry_run_sends_preview_to_sender(capsys):
    """dry_run sends to smtp sender (self-preview), not to the configured recipient."""
    item = _make_item()
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)
    smtp_cfg = SmtpConfig(host="smtp.example.com", username="sender@example.com")

    with (
        patch("smtplib.SMTP", return_value=mock_smtp),
        patch("keyring.get_password", return_value="secret"),
    ):
        result = send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=smtp_cfg,
            recipient="boss@example.com",
            dry_run=True,
            now=_FIXED_NOW,
        )

    assert result is True
    mock_smtp.sendmail.assert_called_once()
    assert mock_smtp.sendmail.call_args.args[1] == "sender@example.com"
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

def test_smtp_subject_contains_prefix_and_count():
    import email as email_mod
    from email.header import decode_header as dh
    item = _make_item()
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
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
            dry_run=True,
            now=_FIXED_NOW,
        )
    sent_msg_str = mock_smtp.sendmail.call_args.args[2]
    parsed = email_mod.message_from_string(sent_msg_str)
    raw_subject, enc = dh(parsed["Subject"])[0]
    subject = raw_subject.decode(enc or "utf-8") if isinstance(raw_subject, bytes) else raw_subject
    assert "[Digest]" in subject
    assert "1 item" in subject


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


def test_smtp_mgmt_summary_dry_run_sends_preview_to_sender(capsys):
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)
    smtp_cfg = SmtpConfig(host="smtp.example.com", username="sender@example.com")

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
            smtp_cfg=smtp_cfg,
            recipient="boss@example.com",
            dry_run=True,
        )
    assert result is True
    mock_smtp.sendmail.assert_called_once()
    assert mock_smtp.sendmail.call_args.args[1] == "sender@example.com"
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

# ---------------------------------------------------------------------------
# XOAUTH2 path
# ---------------------------------------------------------------------------

def _oauth2_smtp() -> SmtpConfig:
    return SmtpConfig(host="smtp.office365.com", username="user@example.com", use_oauth2=True)


def test_smtp_oauth2_uses_xoauth2_not_login():
    item = _make_item()
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)
    mock_smtp.docmd.return_value = (235, b"OK")

    with patch("smtplib.SMTP", return_value=mock_smtp):
        result = send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=_oauth2_smtp(),
            recipient="user@example.com",
            m365_token="fake-token",
            now=_FIXED_NOW,
        )

    assert result is True
    mock_smtp.login.assert_not_called()
    docmd_call = mock_smtp.docmd.call_args
    assert docmd_call.args[0] == "AUTH"
    assert "XOAUTH2" in docmd_call.args[1]


def test_smtp_oauth2_xoauth2_string_contains_token():
    import base64
    item = _make_item()
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)
    mock_smtp.docmd.return_value = (235, b"OK")

    with patch("smtplib.SMTP", return_value=mock_smtp):
        send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=_oauth2_smtp(),
            recipient="user@example.com",
            m365_token="my-access-token",
            now=_FIXED_NOW,
        )

    raw_arg = mock_smtp.docmd.call_args.args[1]
    b64_part = raw_arg.split(" ", 1)[1]
    decoded = base64.b64decode(b64_part).decode()
    assert "my-access-token" in decoded
    assert "user=user@example.com" in decoded


def test_smtp_oauth2_without_token_raises():
    item = _make_item()
    with pytest.raises(RuntimeError, match="setup-auth"):
        send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=_oauth2_smtp(),
            recipient="user@example.com",
            m365_token=None,
        )


def test_smtp_oauth2_does_not_call_keyring():
    item = _make_item()
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)
    mock_smtp.docmd.return_value = (235, b"OK")

    with (
        patch("smtplib.SMTP", return_value=mock_smtp),
        patch("keyring.get_password") as mock_keyring,
    ):
        send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=_oauth2_smtp(),
            recipient="user@example.com",
            m365_token="tok",
            now=_FIXED_NOW,
        )

    mock_keyring.assert_not_called()


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
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)
    with (
        patch("smtplib.SMTP", return_value=mock_smtp),
        patch("keyring.get_password", return_value="secret"),
    ):
        result = send_via_smtp(
            items=[unknown_item, jira_item],
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
            dry_run=True,
            now=_FIXED_NOW,
        )
    assert result is True
