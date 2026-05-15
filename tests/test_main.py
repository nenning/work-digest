"""Tests for digest.main (orchestrator)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from digest.main import parse_since


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

def _make_config(tmp_path: Path):
    """Return a minimal Config-like mock."""
    cfg = MagicMock()
    cfg.data_dir = tmp_path
    cfg.m365.tenant_id = "test-tenant"
    cfg.atlassian.url = "https://example.atlassian.net"
    cfg.atlassian.email = "user@example.com"
    cfg.atlassian.api_token = "token"
    cfg.atlassian.jira_projects = []
    cfg.atlassian.confluence_spaces = []
    cfg.llm.provider = "openai"
    cfg.llm.api_key = "key"
    cfg.llm.model = "gpt-4o-mini"
    cfg.llm.endpoint = None
    cfg.email.subject_prefix = "[Digest]"
    return cfg


# ---------------------------------------------------------------------------
# test_main_nothing_new
# ---------------------------------------------------------------------------

def test_main_nothing_new(tmp_path, capsys, monkeypatch):
    """When all sources return [], main() prints 'Nothing new' and returns."""
    monkeypatch.setattr(sys, "argv", ["main.py"])

    with (
        patch("digest.main.load_config", return_value=_make_config(tmp_path)),
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

def test_main_dry_run(tmp_path, monkeypatch):
    """With --dry-run and one item, send_digest is called with dry_run=True."""
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
        patch("digest.main.load_config", return_value=_make_config(tmp_path)),
        patch("digest.main.load_state", return_value={}),
        patch("digest.main.get_auth_header", return_value="Basic xxx"),
        patch("digest.main.get_token", return_value="tok"),
        patch("digest.main.jira.fetch", return_value=[fake_source_item]),
        patch("digest.main.summarize_items", return_value=[fake_summarized]),
        patch("digest.main.get_recipient", return_value="user@example.com"),
        patch("digest.main.send_digest", mock_send),
    ):
        from digest.main import main
        main()

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs.get("dry_run") is True


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
# get_recipient error handling
# ---------------------------------------------------------------------------

def test_get_recipient_raises_when_fields_missing():
    import requests as req_mod
    from digest.main import get_recipient
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {}  # neither mail nor userPrincipalName
    with patch("digest.main.requests.get", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="recipient"):
            get_recipient("tok")


def test_get_recipient_raises_on_network_error():
    import requests as req_mod
    from digest.main import get_recipient
    with patch("digest.main.requests.get", side_effect=req_mod.ConnectionError("timeout")):
        with pytest.raises(RuntimeError, match="Graph"):
            get_recipient("tok")
