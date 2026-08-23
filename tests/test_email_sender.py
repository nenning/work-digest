"""Tests for digest.email_sender."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from digest.config import EmailConfig, SmtpConfig
from digest.email_sender import (
    TEMPLATES_DIR, _render_html, _safe_url, _build_sections,
    send_via_smtp, send_mgmt_summary_via_smtp,
)
from digest.models import MgmtSection, SourceItem, SummarizedItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    source: str = "jira",
    priority: str = "info",
    title: str = "Test ticket",
    url: str = "https://example.com/1",
    summary: str = "Something happened.",
    kind: str = "comment",
    project: str | None = None,
) -> SummarizedItem:
    return SummarizedItem(
        source=source,
        kind=kind,
        title=title,
        url=url,
        summary=summary,
        author="Alice",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        priority=priority,
        project=project,
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


def test_smtp_connection_has_timeout():
    """Without an explicit timeout, smtplib.SMTP defaults to no timeout at all --
    a hung connection (bad host, silent firewall drop) blocks forever."""
    item = _make_item()
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)
    smtp_cfg = SmtpConfig(host="smtp.example.com", username="sender@example.com")

    with (
        patch("smtplib.SMTP", return_value=mock_smtp) as mock_smtp_class,
        patch("keyring.get_password", return_value="secret"),
    ):
        send_via_smtp(
            items=[item],
            config=_default_config(),
            smtp_cfg=smtp_cfg,
            recipient="boss@example.com",
            dry_run=True,
            now=_FIXED_NOW,
        )

    _, kwargs = mock_smtp_class.call_args
    assert kwargs.get("timeout") is not None


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


def _make_mgmt_section(name="Project One") -> MgmtSection:
    return MgmtSection(
        name=name,
        label="2026-05-01 - 2026-05-31",
        narrative="Team did great work.",
        jira_items=[_make_source_item()],
        confluence_items=[],
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
            sections=[_make_mgmt_section()],
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
            sections=[_make_mgmt_section()],
            subject="[Team Summary]",
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
        )
    assert result is True
    mock_smtp.sendmail.assert_called_once()


def test_smtp_mgmt_summary_multiple_sections_all_rendered():
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)

    with (
        patch("smtplib.SMTP", return_value=mock_smtp),
        patch("keyring.get_password", return_value="secret"),
    ):
        result = send_mgmt_summary_via_smtp(
            sections=[_make_mgmt_section("Project One"), _make_mgmt_section("Project Two")],
            subject="[Team Summary]",
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
        )
    assert result is True
    html_body = mock_smtp.sendmail.call_args.args[2]
    assert "Project One" in html_body
    assert "Project Two" in html_body


# ---------------------------------------------------------------------------
# Test 5: action_needed CSS class appears in rendered HTML (via _render_html)
# ---------------------------------------------------------------------------

def test_action_needed_class_in_html():
    item = _make_item(priority="action_needed")
    general = {"updates": [item]}  # kind="comment" maps to "updates" group
    html = _render_html([], general, "Test subject")
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

# ---------------------------------------------------------------------------
# _build_sections: per-project grouping
# ---------------------------------------------------------------------------

def test_build_sections_groups_items_by_project_and_kind():
    items = [
        _make_item(project="Project One", kind="comment"),
        _make_item(project="Project One", kind="mention"),
        _make_item(project="Project Two", kind="comment"),
    ]
    projects, general = _build_sections(items)
    assert general == {}
    by_name = {p["name"]: p["groups"] for p in projects}
    assert set(by_name) == {"Project One", "Project Two"}
    assert len(by_name["Project One"]["jira_comments"]) == 1
    assert len(by_name["Project One"]["jira_mentions"]) == 1
    assert len(by_name["Project Two"]["jira_comments"]) == 1


def test_build_sections_items_without_project_go_to_general():
    items = [
        _make_item(source="teams", kind="channel_message", project=None),
        _make_item(source="jira", kind="comment", project="Project One"),
    ]
    projects, general = _build_sections(items)
    assert len(projects) == 1
    assert projects[0]["name"] == "Project One"
    assert "updates" in general
    assert len(general["updates"]) == 1


def test_build_sections_respects_project_order():
    items = [
        _make_item(project="Zebra", kind="comment"),
        _make_item(project="Alpha", kind="comment"),
    ]
    projects, _ = _build_sections(items, project_order=["Alpha", "Zebra"])
    assert [p["name"] for p in projects] == ["Alpha", "Zebra"]


def test_build_sections_unlisted_project_appended_after_ordered_ones():
    items = [
        _make_item(project="Alpha", kind="comment"),
        _make_item(project="Surprise", kind="comment"),
    ]
    projects, _ = _build_sections(items, project_order=["Alpha"])
    assert [p["name"] for p in projects] == ["Alpha", "Surprise"]


# ---------------------------------------------------------------------------
# _render_html: project header visibility
# ---------------------------------------------------------------------------

def test_render_html_shows_project_headers_when_multiple_projects():
    items = [
        _make_item(project="Project One", kind="comment"),
        _make_item(project="Project Two", kind="comment"),
    ]
    projects, general = _build_sections(items)
    html = _render_html(projects, general, "Test subject")
    assert "Project One" in html
    assert "Project Two" in html
    assert "project-header" in html


def test_render_html_hides_project_header_for_single_project():
    items = [_make_item(project="Only Project", kind="comment")]
    projects, general = _build_sections(items)
    html = _render_html(projects, general, "Test subject")
    assert '<h2>Only Project</h2>' not in html


def test_render_html_general_section_rendered_without_project_header():
    items = [_make_item(source="teams", kind="channel_message", project=None)]
    projects, general = _build_sections(items)
    html = _render_html(projects, general, "Test subject")
    assert '<div class="project-header">' not in html


# ---------------------------------------------------------------------------
# send_via_smtp: project_order threaded through to the rendered email
# ---------------------------------------------------------------------------

def test_smtp_send_orders_projects_per_project_order():
    import email as email_mod
    items = [
        _make_item(project="Zebra", kind="comment", title="Z ticket"),
        _make_item(project="Alpha", kind="comment", title="A ticket"),
    ]
    mock_smtp = MagicMock()
    mock_smtp.__enter__ = lambda s: s
    mock_smtp.__exit__ = MagicMock(return_value=False)

    with (
        patch("smtplib.SMTP", return_value=mock_smtp),
        patch("keyring.get_password", return_value="secret"),
    ):
        send_via_smtp(
            items=items,
            config=_default_config(),
            smtp_cfg=_default_smtp(),
            recipient="user@example.com",
            now=_FIXED_NOW,
            project_order=["Alpha", "Zebra"],
        )

    # The rendered HTML contains non-ASCII group icons, so smtplib base64-encodes
    # the body -- decode it rather than substring-matching the raw MIME payload.
    raw_msg = mock_smtp.sendmail.call_args.args[2]
    parsed = email_mod.message_from_string(raw_msg)
    html_body = parsed.get_payload()[0].get_payload(decode=True).decode("utf-8")
    assert html_body.index("Alpha") < html_body.index("Zebra")


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
