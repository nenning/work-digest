"""Tests for digest.main (orchestrator)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from digest.main import parse_since
from digest.config import (
    AtlassianConfig,
    Config,
    EmailConfig,
    LLMConfig,
    M365Config,
    MgmtSummaryConfig,
    ProjectConfig,
    ProjectJiraConfig,
    ProjectMgmtSummaryConfig,
    ScheduleConfig,
    SmtpConfig,
)
from digest.models import SourceItem


# ---------------------------------------------------------------------------
# parse_since helpers
# ---------------------------------------------------------------------------

def test_parse_since_hours():
    result = parse_since("2h")
    now = datetime.now(timezone.utc)
    diff = (now - result).total_seconds()
    assert 7190 <= diff <= 7210, f"Expected ~7200s, got {diff}"


def test_parse_since_iso():
    result = parse_since("2026-04-09T08:00:00")
    assert result.year == 2026
    assert result.hour == 8
    assert result.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Shared helpers for main() integration tests
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path, smtp: bool = False):
    """Return a minimal Config-like mock."""
    cfg = MagicMock()
    cfg.data_dir = tmp_path
    cfg.m365.tenant_id = "test-tenant"
    cfg.atlassian.url = "https://example.atlassian.net"
    cfg.atlassian.email = "user@example.com"
    cfg.atlassian.api_token = "token"
    cfg.atlassian.projects = []
    cfg.llm.provider = "openai"
    cfg.llm.api_key = "key"
    cfg.llm.model = "gpt-4o-mini"
    cfg.llm.endpoint = None
    cfg.email.subject_prefix = "[Digest]"
    cfg.email.recipient = "user@example.com"
    cfg.smtp = SmtpConfig(host="smtp.example.com", username="user@example.com") if smtp else None
    return cfg


# ---------------------------------------------------------------------------
# _run_mgmt_summary integration tests
# ---------------------------------------------------------------------------

def _make_mgmt_config(tmp_path: Path, projects, mgmt_summary=None) -> Config:
    """Real (non-mock) Config -- _run_mgmt_summary exercises real dataclass code
    (AtlassianConfig.for_project, resolve_project_mgmt_config's dataclasses.replace),
    which a MagicMock can't stand in for."""
    return Config(
        atlassian=AtlassianConfig(
            url="https://example.atlassian.net",
            email="user@example.com",
            api_token="token",
            projects=projects,
        ),
        m365=M365Config(enabled=False),
        llm=LLMConfig(provider="openai", api_key="key", models=["gpt-4o-mini"]),
        schedule=ScheduleConfig(times=["08:00"]),
        email=EmailConfig(recipient="user@example.com"),
        data_dir=tmp_path,
        mgmt_summary=mgmt_summary or MgmtSummaryConfig(),
        smtp=SmtpConfig(host="smtp.example.com", username="user@example.com"),
    )


def _make_ticket(key="PROJ-1") -> SourceItem:
    return SourceItem(
        source="jira", kind="ticket_done", title=key, url=f"https://example.atlassian.net/browse/{key}",
        content="done", author="Alice", timestamp=datetime.now(timezone.utc),
    )


def test_mgmt_summary_no_project_configured_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--mgmt-summary", "--since", "7d"])
    project = ProjectConfig(name="P1", jira=ProjectJiraConfig(project="PROJ"))  # no mgmt_summary block
    config = _make_mgmt_config(tmp_path, [project])

    with patch("digest.main.load_config", return_value=config):
        from digest.main import main
        with pytest.raises(RuntimeError, match="mgmt_summary block"):
            main()


def test_mgmt_summary_sprint_missing_board_id_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--mgmt-summary", "--sprint", "Sprint 1"])
    project = ProjectConfig(
        name="P1", jira=ProjectJiraConfig(project="PROJ"),
        mgmt_summary=ProjectMgmtSummaryConfig(),  # no jira_board_id
    )
    config = _make_mgmt_config(tmp_path, [project])

    with (
        patch("digest.main.load_config", return_value=config),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
    ):
        from digest.main import main
        with pytest.raises(ValueError, match="jira_board_id"):
            main()


def test_mgmt_summary_single_project_sends_one_section(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--mgmt-summary", "--since", "7d"])
    project = ProjectConfig(
        name="Project One", jira=ProjectJiraConfig(project="PROJ"),
        mgmt_summary=ProjectMgmtSummaryConfig(),
    )
    config = _make_mgmt_config(tmp_path, [project])

    with (
        patch("digest.main.load_config", return_value=config),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.fetch_team_tickets", return_value=([_make_ticket()], {"a1"})),
        patch("digest.main.fetch_team_pages", return_value=[]),
        patch("digest.main.synthesize_mgmt_summary", return_value="Great progress."),
        patch("digest.main.send_mgmt_summary_via_smtp") as mock_send,
    ):
        from digest.main import main
        main()

    assert mock_send.call_count == 1
    sections = mock_send.call_args.args[0]
    assert len(sections) == 1
    assert sections[0].name == "Project One"
    assert sections[0].narrative == "Great progress."


def test_mgmt_summary_multiple_projects_each_own_section(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--mgmt-summary", "--since", "7d"])
    p1 = ProjectConfig(name="Project One", jira=ProjectJiraConfig(project="ONE"),
                        mgmt_summary=ProjectMgmtSummaryConfig())
    p2 = ProjectConfig(name="Project Two", jira=ProjectJiraConfig(project="TWO"),
                        mgmt_summary=ProjectMgmtSummaryConfig())
    config = _make_mgmt_config(tmp_path, [p1, p2])

    with (
        patch("digest.main.load_config", return_value=config),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.fetch_team_tickets", return_value=([_make_ticket()], {"a1"})),
        patch("digest.main.fetch_team_pages", return_value=[]),
        patch("digest.main.synthesize_mgmt_summary", return_value="Progress."),
        patch("digest.main.send_mgmt_summary_via_smtp") as mock_send,
    ):
        from digest.main import main
        main()

    sections = mock_send.call_args.args[0]
    assert [s.name for s in sections] == ["Project One", "Project Two"]


def test_mgmt_summary_project_with_no_data_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--mgmt-summary", "--since", "7d"])
    project = ProjectConfig(
        name="Empty", jira=ProjectJiraConfig(project="EMPTY"),
        mgmt_summary=ProjectMgmtSummaryConfig(),
    )
    config = _make_mgmt_config(tmp_path, [project])

    with (
        patch("digest.main.load_config", return_value=config),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.fetch_team_tickets", return_value=([], set())),
        patch("digest.main.fetch_team_pages", return_value=[]),
        patch("digest.main.send_mgmt_summary_via_smtp") as mock_send,
    ):
        from digest.main import main
        main()

    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# test_main_nothing_new
# ---------------------------------------------------------------------------

def test_main_nothing_new(tmp_path, capsys, monkeypatch):
    """When all sources return [], main() prints 'Nothing new' and returns."""
    monkeypatch.setattr(sys, "argv", ["main.py"])

    with (
        patch("digest.main.load_config", return_value=_make_config(tmp_path, smtp=True)),
        patch("digest.main.load_state", return_value={}),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.get_token", return_value="tok"),
        patch("digest.main.jira.fetch", return_value=[]),
        patch("digest.main.confluence.fetch", return_value=[]),
        patch("digest.main.teams.fetch", return_value=[]),
        patch("digest.main.outlook.fetch", return_value=[]),
    ):
        from digest.main import main
        main()

    captured = capsys.readouterr()
    assert "Nothing new" in captured.out


# ---------------------------------------------------------------------------
# test_main_dry_run
# ---------------------------------------------------------------------------

def test_smtp_oauth2_token_passed_to_send(tmp_path, monkeypatch):
    """When smtp.use_oauth2=True, main() acquires M365 token and passes it to send_via_smtp."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--dry-run", "--source", "jira"])

    from digest.config import SmtpConfig
    from digest.models import SourceItem, SummarizedItem
    ts = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)
    cfg = _make_config(tmp_path, smtp=True)
    cfg.smtp = SmtpConfig(host="smtp.office365.com", username="u@example.com", use_oauth2=True)

    fake_item = SourceItem(source="jira", kind="comment", title="T", url="https://x.com",
                           content="x", author="A", timestamp=ts)
    fake_sum = SummarizedItem(source="jira", kind="comment", title="T", url="https://x.com",
                              summary="s", author="A", timestamp=ts)
    mock_send = MagicMock(return_value=True)

    with (
        patch("digest.main.load_config", return_value=cfg),
        patch("digest.main.load_state", return_value={}),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.get_token", return_value="oauth-token"),
        patch("digest.main.jira.fetch", return_value=[fake_item]),
        patch("digest.main.summarize_items", return_value=[fake_sum]),
        patch("digest.main.send_via_smtp", mock_send),
    ):
        from digest.main import main
        main()

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs.get("m365_token") == "oauth-token"


def test_main_dry_run_smtp(tmp_path, monkeypatch):
    """With --dry-run and smtp config, send_via_smtp is called with dry_run=True."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--dry-run", "--source", "jira"])

    ts = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)

    from digest.models import SourceItem, SummarizedItem

    fake_source_item = SourceItem(
        source="jira",
        kind="assignment",
        title="PROJ-1: Fix something",
        url="https://example.atlassian.net/browse/PROJ-1",
        content="Fix something important",
        author="Alice",
        timestamp=ts,
    )
    fake_summarized = SummarizedItem(
        source="jira",
        kind="assignment",
        title="PROJ-1: Fix something",
        url="https://example.atlassian.net/browse/PROJ-1",
        summary="A ticket was assigned.",
        author="Alice",
        timestamp=ts,
    )

    mock_send = MagicMock(return_value=True)

    with (
        patch("digest.main.load_config", return_value=_make_config(tmp_path, smtp=True)),
        patch("digest.main.load_state", return_value={}),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.get_token", return_value="tok"),
        patch("digest.main.jira.fetch", return_value=[fake_source_item]),
        patch("digest.main.summarize_items", return_value=[fake_summarized]),
        patch("digest.main.send_via_smtp", mock_send),
    ):
        from digest.main import main
        main()

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs.get("dry_run") is True


def test_main_dry_run_com(tmp_path, monkeypatch):
    """With --dry-run and no smtp config on Windows, send_via_com is called."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--dry-run", "--source", "jira"])

    ts = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)
    from digest.models import SourceItem, SummarizedItem

    fake_source_item = SourceItem(
        source="jira", kind="assignment", title="PROJ-1",
        url="https://example.atlassian.net/browse/PROJ-1",
        content="x", author="Alice", timestamp=ts,
    )
    fake_summarized = SummarizedItem(
        source="jira", kind="assignment", title="PROJ-1",
        url="https://example.atlassian.net/browse/PROJ-1",
        summary="done", author="Alice", timestamp=ts,
    )

    mock_send = MagicMock(return_value=True)

    with (
        patch("digest.main.load_config", return_value=_make_config(tmp_path, smtp=False)),
        patch("digest.main.load_state", return_value={}),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.get_token", return_value="tok"),
        patch("digest.main.jira.fetch", return_value=[fake_source_item]),
        patch("digest.main.summarize_items", return_value=[fake_summarized]),
        patch("digest.main.send_via_com", mock_send),
        patch("digest.main.sys.platform", "win32"),
    ):
        from digest.main import main
        main()

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs.get("dry_run") is True


def test_main_non_windows_no_smtp_raises(tmp_path, monkeypatch):
    """On non-Windows without smtp config, main() raises a clear error."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--dry-run", "--source", "jira"])

    ts = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)
    from digest.models import SourceItem, SummarizedItem

    fake_source_item = SourceItem(
        source="jira", kind="assignment", title="PROJ-1",
        url="https://example.atlassian.net/browse/PROJ-1",
        content="x", author="Alice", timestamp=ts,
    )
    fake_summarized = SummarizedItem(
        source="jira", kind="assignment", title="PROJ-1",
        url="https://example.atlassian.net/browse/PROJ-1",
        summary="done", author="Alice", timestamp=ts,
    )

    with (
        patch("digest.main.load_config", return_value=_make_config(tmp_path, smtp=False)),
        patch("digest.main.load_state", return_value={}),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.get_token", return_value="tok"),
        patch("digest.main.jira.fetch", return_value=[fake_source_item]),
        patch("digest.main.summarize_items", return_value=[fake_summarized]),
        patch("digest.main.sys.platform", "linux"),
        pytest.raises(RuntimeError, match="smtp"),
    ):
        from digest.main import main
        main()


# ---------------------------------------------------------------------------
# test_setup_auth_calls_get_token
# ---------------------------------------------------------------------------

def test_setup_auth_calls_get_token(tmp_path, capsys, monkeypatch):
    """--setup-auth calls get_token and prints a success message."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--setup-auth"])

    mock_get_token = MagicMock(return_value="tok")

    with (
        patch("digest.main.load_config", return_value=_make_config(tmp_path)),
        patch("digest.main.get_token", mock_get_token),
    ):
        from digest.main import main
        main()

    mock_get_token.assert_called_once()
    captured = capsys.readouterr()
    assert "success" in captured.out.lower()


# ---------------------------------------------------------------------------
# parse_since validation
# ---------------------------------------------------------------------------

def test_parse_since_invalid_hours_raises():
    with pytest.raises(ValueError, match="2h"):
        parse_since("h")  # int("") raises


def test_parse_since_zero_hours_raises():
    with pytest.raises(ValueError, match="positive"):
        parse_since("0h")


def test_parse_since_negative_hours_raises():
    with pytest.raises(ValueError, match="positive"):
        parse_since("-1h")


# ---------------------------------------------------------------------------
# --setup-smtp-auth
# ---------------------------------------------------------------------------

def test_setup_smtp_auth_stores_password(tmp_path, capsys, monkeypatch):
    """--setup-smtp-auth prompts for password and stores it in keyring."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--setup-smtp-auth"])

    cfg = _make_config(tmp_path, smtp=True)

    with (
        patch("digest.main.load_config", return_value=cfg),
        patch("digest.main.getpass.getpass", return_value="my-secret"),
        patch("digest.main.keyring.set_password") as mock_set,
    ):
        from digest.main import main
        main()

    mock_set.assert_called_once_with("digest-smtp", "user@example.com", "my-secret")
    assert "success" in capsys.readouterr().out.lower()


def test_setup_smtp_auth_requires_smtp_config(tmp_path, monkeypatch):
    """--setup-smtp-auth fails clearly when no smtp block is configured."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--setup-smtp-auth"])

    with (
        patch("digest.main.load_config", return_value=_make_config(tmp_path, smtp=False)),
        pytest.raises(RuntimeError, match="smtp"),
    ):
        from digest.main import main
        main()
