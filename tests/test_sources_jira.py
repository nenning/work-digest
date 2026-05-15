import copy
import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from digest.config import AtlassianConfig
from digest.sources.jira import fetch, _extract_text, _display_name, _parse_dt, _append_extra


def make_config():
    return AtlassianConfig(
        url="https://example.atlassian.net",
        email="u@e.com", api_token="tok",
        jira_projects=["PROJ"], confluence_spaces=[],
    )


ISSUE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "Fix the bug",
        "status": {"name": "In Progress"},
        "reporter": {"displayName": "Anna"},
        "assignee": {"displayName": "Chris"},
        "updated": "2026-04-09T08:00:00Z",
        "created": "2026-04-09T07:00:00Z",
        "comment": {"comments": [
            {
                "id": "101",
                "body": "Looks good to me",
                "author": {"displayName": "Marco"},
                "updated": "2026-04-09T08:30:00Z",
            }
        ]},
    },
}

SINCE = datetime(2026, 4, 9, 7, 0, 0, tzinfo=timezone.utc)


def test_fetch_assigned():
    with patch("digest.sources.jira.requests.post") as mock_get:
        mock_get.return_value.json.return_value = {"issues": [ISSUE]}
        mock_get.return_value.raise_for_status = lambda: None
        items = fetch(make_config(), "Basic xxx", SINCE)

    assigned = [i for i in items if i.kind == "assignment"]
    assert len(assigned) == 1
    assert assigned[0].title == "PROJ-1: Fix the bug"
    assert assigned[0].url == "https://example.atlassian.net/browse/PROJ-1"


def test_fetch_comments():
    with patch("digest.sources.jira.requests.post") as mock_get:
        mock_get.return_value.json.return_value = {"issues": [ISSUE]}
        mock_get.return_value.raise_for_status = lambda: None
        items = fetch(make_config(), "Basic xxx", SINCE)

    comments = [i for i in items if i.kind == "comment"]
    assert any(i.author == "Marco" for i in comments)


def test_extract_text_plain_string():
    assert _extract_text("hello world") == "hello world"


def test_extract_text_adf():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ]}
    ]}
    assert "Hello" in _extract_text(adf)


def test_empty_projects_returns_nothing():
    cfg = make_config()
    cfg.jira_projects = []
    items = fetch(cfg, "Basic xxx", SINCE)
    assert items == []


def test_display_name_null_value_returns_unknown():
    assert _display_name({"displayName": None, "accountId": "abc"}) == "unknown"
    assert _display_name(None) == "unknown"


def test_parse_dt_non_utc_offset():
    # 08:30+05:30 = 03:00 UTC
    result = _parse_dt("2026-04-09T08:30:00+05:30")
    assert result.hour == 3
    assert result.tzinfo == timezone.utc


def test_comment_before_since_is_excluded():
    issue_with_old_comment = copy.deepcopy(ISSUE)
    issue_with_old_comment["fields"]["comment"]["comments"][0]["updated"] = "2026-04-09T06:00:00Z"
    with patch("digest.sources.jira.requests.post") as mock_get:
        mock_get.return_value.json.return_value = {"issues": [issue_with_old_comment]}
        mock_get.return_value.raise_for_status = lambda: None
        items = fetch(make_config(), "Basic xxx", SINCE)
    comments = [i for i in items if i.kind == "comment"]
    assert len(comments) == 0


def test_invalid_project_key_raises():
    cfg = make_config()
    cfg.jira_projects = ["invalid key"]
    with pytest.raises(ValueError, match="Invalid Jira project key"):
        fetch(cfg, "Basic xxx", SINCE)


def test_jql_extra_appended_to_assigned():
    cfg = make_config()
    cfg.jira_jql_extra = '"Team[Team]" = abc'
    with patch("digest.sources.jira.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"issues": [ISSUE]}
        mock_post.return_value.raise_for_status = lambda: None
        fetch(cfg, "Basic xxx", SINCE)
    calls = [call.kwargs["json"]["jql"] for call in mock_post.call_args_list]
    assert all('"Team[Team]" = abc' in jql for jql in calls)


def test_jql_extra_inserted_before_order_by():
    result = _append_extra(
        'project in (PROJ) AND created >= "2026-01-01" ORDER BY created DESC',
        '"Team[Team]" = abc',
    )
    assert result.index('"Team[Team]"') < result.upper().index("ORDER BY")
