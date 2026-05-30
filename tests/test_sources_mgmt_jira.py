"""Tests for digest.sources.mgmt_jira."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from digest.config import AtlassianConfig, MgmtSummaryConfig
from digest.sources.mgmt_jira import (
    _extract_text,
    _parse_dt,
    _parse_dt_optional,
    fetch_sprint,
    fetch_team_tickets,
)


def make_atlassian_config() -> AtlassianConfig:
    return AtlassianConfig(
        url="https://example.atlassian.net",
        email="u@e.com",
        api_token="tok",
        jira_projects=[],
        confluence_spaces=[],
    )


def make_mgmt_cfg(**kwargs) -> MgmtSummaryConfig:
    defaults = dict(jira_jql="project = TEAM", jira_board_id=1)
    defaults.update(kwargs)
    return MgmtSummaryConfig(**defaults)


AUTH = "Basic xxx"
SINCE = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 5, 31, 0, 0, 0, tzinfo=timezone.utc)


def _sprint_resp(values, is_last=True):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {"values": values, "isLast": is_last}
    return resp


def _search_resp(issues, is_last=True, next_page_token=None):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    body = {"issues": issues, "isLast": is_last}
    if next_page_token:
        body["nextPageToken"] = next_page_token
    resp.json.return_value = body
    return resp


def _make_issue(key="TEAM-1", summary="Do the thing", status_name="In Progress",
                status_key="indeterminate", assignee_id="a1", assignee_name="Alice",
                reporter_id="r1", issue_type="Story", description=None):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {
                "name": status_name,
                "statusCategory": {"key": status_key},
            },
            "assignee": {"accountId": assignee_id, "displayName": assignee_name},
            "reporter": {"accountId": reporter_id, "displayName": "Reporter"},
            "issuetype": {"name": issue_type},
            "description": description,
            "updated": "2026-05-10T09:00:00Z",
            "created": "2026-05-01T08:00:00Z",
        },
    }


# ---------------------------------------------------------------------------
# fetch_sprint
# ---------------------------------------------------------------------------

def test_fetch_sprint_by_name():
    sprint = {"id": 42, "name": "Sprint 7", "startDate": "2026-05-01T00:00:00Z", "endDate": "2026-05-14T00:00:00Z"}
    mock_get = MagicMock(return_value=_sprint_resp([sprint]))
    with patch("digest.sources.mgmt_jira.requests.get", mock_get):
        sid, start, end, name = fetch_sprint(make_atlassian_config(), AUTH, 1, "Sprint 7")
    assert sid == 42
    assert name == "Sprint 7"
    assert start == datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 14, 0, 0, 0, tzinfo=timezone.utc)


def test_fetch_sprint_case_insensitive():
    sprint = {"id": 10, "name": "Sprint Alpha", "startDate": "2026-05-01T00:00:00Z", "endDate": "2026-05-14T00:00:00Z"}
    mock_get = MagicMock(return_value=_sprint_resp([sprint]))
    with patch("digest.sources.mgmt_jira.requests.get", mock_get):
        sid, _, _, name = fetch_sprint(make_atlassian_config(), AUTH, 1, "sprint alpha")
    assert sid == 10


def test_fetch_sprint_paginated():
    page1_resp = _sprint_resp([{"id": 1, "name": "Sprint 1"}], is_last=False)
    page2_resp = _sprint_resp([
        {"id": 99, "name": "Sprint 99", "startDate": "2026-05-01T00:00:00Z", "endDate": "2026-05-14T00:00:00Z"}
    ], is_last=True)
    mock_get = MagicMock(side_effect=[page1_resp, page2_resp])
    with patch("digest.sources.mgmt_jira.requests.get", mock_get):
        sid, _, _, _ = fetch_sprint(make_atlassian_config(), AUTH, 1, "Sprint 99")
    assert sid == 99
    assert mock_get.call_count == 2


def test_fetch_sprint_not_found():
    mock_get = MagicMock(return_value=_sprint_resp([{"id": 1, "name": "Sprint X"}]))
    with patch("digest.sources.mgmt_jira.requests.get", mock_get):
        with pytest.raises(ValueError, match="Sprint 'Missing' not found"):
            fetch_sprint(make_atlassian_config(), AUTH, 1, "Missing")


def test_fetch_sprint_current_delegates_to_active():
    sprint = {"id": 7, "name": "Active Sprint", "startDate": "2026-05-01T00:00:00Z", "endDate": "2026-05-14T00:00:00Z"}
    mock_get = MagicMock(return_value=_sprint_resp([sprint]))
    with patch("digest.sources.mgmt_jira.requests.get", mock_get):
        sid, _, _, name = fetch_sprint(make_atlassian_config(), AUTH, 1, "current")
    assert sid == 7
    assert name == "Active Sprint"
    called_params = mock_get.call_args[1]["params"]
    assert called_params.get("state") == "active"


def test_fetch_sprint_current_no_active_sprint():
    mock_get = MagicMock(return_value=_sprint_resp([]))
    with patch("digest.sources.mgmt_jira.requests.get", mock_get):
        with pytest.raises(ValueError, match="No active sprint"):
            fetch_sprint(make_atlassian_config(), AUTH, 1, "current")


# ---------------------------------------------------------------------------
# fetch_team_tickets
# ---------------------------------------------------------------------------

def test_fetch_team_tickets_basic():
    issues = [
        _make_issue("TEAM-1", status_name="Done", status_key="done"),
        _make_issue("TEAM-2", status_name="In Progress", status_key="indeterminate"),
        _make_issue("TEAM-3", status_name="To Do", status_key="new"),
    ]
    mock_post = MagicMock(return_value=_search_resp(issues))
    with patch("digest.sources.mgmt_jira.requests.post", mock_post):
        items, account_ids = fetch_team_tickets(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL)

    assert len(items) == 3
    kinds = [i.kind for i in items]
    assert "ticket_done" in kinds
    assert "ticket_wip" in kinds
    assert "ticket_todo" in kinds
    assert items[0].metadata["status"] == "Done"
    assert items[0].metadata["assignee"] == "Alice"
    assert "a1" in account_ids
    assert "r1" in account_ids


def test_fetch_team_tickets_sprint_id_in_jql():
    mock_post = MagicMock(return_value=_search_resp([]))
    with patch("digest.sources.mgmt_jira.requests.post", mock_post):
        fetch_team_tickets(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, sprint_id=42)
    body = mock_post.call_args[1]["json"]
    assert "sprint = 42" in body["jql"]
    assert "updated >=" not in body["jql"]


def test_fetch_team_tickets_no_sprint_id_uses_date_range():
    mock_post = MagicMock(return_value=_search_resp([]))
    with patch("digest.sources.mgmt_jira.requests.post", mock_post):
        fetch_team_tickets(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL)
    body = mock_post.call_args[1]["json"]
    assert "updated >=" in body["jql"]
    assert "sprint" not in body["jql"]


def test_fetch_team_tickets_ignore_user():
    issues = [
        _make_issue("TEAM-1", assignee_name="Bot User"),
        _make_issue("TEAM-2", assignee_name="Alice"),
    ]
    mock_post = MagicMock(return_value=_search_resp(issues))
    cfg = make_mgmt_cfg(ignore_users=["Bot User"])
    with patch("digest.sources.mgmt_jira.requests.post", mock_post):
        items, _ = fetch_team_tickets(make_atlassian_config(), AUTH, cfg, SINCE, UNTIL)
    assert len(items) == 1
    assert items[0].title.startswith("TEAM-2")


def test_fetch_team_tickets_ignore_issue_type():
    issues = [
        _make_issue("TEAM-1", issue_type="Sub-task"),
        _make_issue("TEAM-2", issue_type="Story"),
    ]
    mock_post = MagicMock(return_value=_search_resp(issues))
    cfg = make_mgmt_cfg(ignore_issue_types=["sub-task"])
    with patch("digest.sources.mgmt_jira.requests.post", mock_post):
        items, _ = fetch_team_tickets(make_atlassian_config(), AUTH, cfg, SINCE, UNTIL)
    assert len(items) == 1
    assert items[0].title.startswith("TEAM-2")


def test_fetch_team_tickets_adf_description_extracted_and_truncated():
    long_text = "x" * 400
    adf = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": long_text}]}],
    }
    issues = [_make_issue("TEAM-1", description=adf)]
    mock_post = MagicMock(return_value=_search_resp(issues))
    with patch("digest.sources.mgmt_jira.requests.post", mock_post):
        items, _ = fetch_team_tickets(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL)
    assert len(items) == 1
    assert "x" in items[0].content
    assert len(items[0].content) < 450


def test_fetch_team_tickets_pagination():
    page1_issues = [_make_issue("TEAM-1")]
    page2_issues = [_make_issue("TEAM-2")]
    mock_post = MagicMock(side_effect=[
        _search_resp(page1_issues, is_last=False, next_page_token="tok-2"),
        _search_resp(page2_issues, is_last=True),
    ])
    with patch("digest.sources.mgmt_jira.requests.post", mock_post):
        items, _ = fetch_team_tickets(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL)
    assert len(items) == 2
    assert mock_post.call_count == 2
    second_body = mock_post.call_args_list[1][1]["json"]
    assert second_body.get("nextPageToken") == "tok-2"


# ---------------------------------------------------------------------------
# _parse_dt / _parse_dt_optional
# ---------------------------------------------------------------------------

def test_parse_dt_with_z_suffix():
    dt = _parse_dt("2026-05-10T09:00:00Z")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026 and dt.month == 5 and dt.day == 10


def test_parse_dt_without_z():
    dt = _parse_dt("2026-05-10T09:00:00+02:00")
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 7


def test_parse_dt_optional_none_returns_now():
    before = datetime.now(timezone.utc)
    result = _parse_dt_optional(None)
    after = datetime.now(timezone.utc)
    assert before <= result <= after


def test_parse_dt_optional_empty_string_returns_now():
    before = datetime.now(timezone.utc)
    result = _parse_dt_optional("")
    after = datetime.now(timezone.utc)
    assert before <= result <= after


def test_parse_dt_optional_with_value():
    result = _parse_dt_optional("2026-05-01T00:00:00Z")
    assert result.year == 2026 and result.month == 5


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------

def test_extract_text_plain_string():
    assert _extract_text("hello world") == "hello world"


def test_extract_text_adf_nested():
    node = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "World"},
                ],
            }
        ],
    }
    result = _extract_text(node)
    assert "Hello" in result
    assert "World" in result


def test_extract_text_list():
    nodes = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
    result = _extract_text(nodes)
    assert "A" in result and "B" in result


def test_extract_text_empty():
    assert _extract_text({}) == ""
    assert _extract_text([]) == ""
