import copy
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from digest.config import AtlassianConfig
from digest.sources.jira import (
    fetch, _extract_text, _display_name, _parse_dt, _append_extra, _has_mention,
    _merge_field_changes, _net_field_change, _jql_search,
)


def make_config():
    return AtlassianConfig(
        url="https://example.atlassian.net",
        email="u@e.com", api_token="tok",
        jira_projects=["PROJ"], confluence_spaces=[],
    )


SINCE = datetime(2026, 4, 9, 7, 0, 0, tzinfo=timezone.utc)

CURRENT_USER = {"accountId": "user-123", "displayName": "Chris"}

ISSUE_BASE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "Fix the bug",
        "status": {"name": "In Progress"},
        "reporter": {"displayName": "Anna"},
        "assignee": {"displayName": "Chris"},
        "updated": "2026-04-09T08:00:00Z",
        "created": "2026-04-09T07:00:00Z",
        "description": None,
        "comment": {"comments": []},
    },
    "changelog": {"histories": []},
}


def _mock_responses(watched_issues, user=CURRENT_USER, new_ticket_issues=None):
    """Return (mock_get, mock_post) routing GET calls to /myself vs. per-issue /changelog."""
    if new_ticket_issues is None:
        new_ticket_issues = []

    # Extract changelog histories from the issue fixtures so they can be served
    # by the per-issue GET /changelog endpoint mock.
    changelogs = {
        issue["key"]: issue.get("changelog", {}).get("histories", [])
        for issue in watched_issues
    }

    def _get_side_effect(url, *args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if "/myself" in url:
            resp.json.return_value = user
        elif "/changelog" in url:
            key = url.split("/issue/")[1].split("/changelog")[0]
            resp.json.return_value = {"values": changelogs.get(key, [])}
        else:
            resp.json.return_value = {}
        return resp

    mock_get = MagicMock(side_effect=_get_side_effect)

    def _post_side_effect(*args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        jql = kwargs.get("json", {}).get("jql", "")
        resp.json.return_value = {"issues": new_ticket_issues if "created >=" in jql else watched_issues}
        return resp

    mock_post = MagicMock(side_effect=_post_side_effect)
    return mock_get, mock_post


def test_plain_comment_produces_comment_kind():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [{
        "id": "101",
        "body": "Looks good to me",
        "author": {"displayName": "Marco"},
        "updated": "2026-04-09T08:30:00Z",
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    comments = [i for i in items if i.kind == "comment"]
    assert len(comments) == 1
    assert comments[0].author == "Marco"
    assert comments[0].title == "PROJ-1: Fix the bug"


def test_mention_in_comment_produces_mention_kind():
    mention_body = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": "user-123", "text": "@Chris"}},
            {"type": "text", "text": " please review"},
        ]}],
    }
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [{
        "id": "102",
        "body": mention_body,
        "author": {"displayName": "Anna"},
        "updated": "2026-04-09T08:30:00Z",
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    mentions = [i for i in items if i.kind == "mention"]
    assert len(mentions) == 1
    assert mentions[0].author == "Anna"
    assert mentions[0].metadata["mention_author"] == "Anna"


def test_field_change_in_changelog_produces_field_change_kind():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:15:00Z",
        "author": {"displayName": "Bob"},
        "items": [{"field": "status", "fromString": "Open", "toString": "In Progress"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    assert "status" in changes[0].content
    assert "Open" in changes[0].content
    assert "In Progress" in changes[0].content


def test_multiple_field_changes_aggregated_into_one_item():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [
        {
            "created": "2026-04-09T08:10:00Z",
            "author": {"displayName": "Bob"},
            "items": [{"field": "status", "fromString": "Open", "toString": "In Progress"}],
        },
        {
            "created": "2026-04-09T08:20:00Z",
            "author": {"displayName": "Bob"},
            "items": [{"field": "assignee", "fromString": "unknown", "toString": "Anna"}],
        },
    ]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    assert "status" in changes[0].content
    assert "assignee" in changes[0].content


def test_field_changes_by_different_authors_credit_both():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [
        {
            "created": "2026-04-09T08:10:00Z",
            "author": {"displayName": "Bob"},
            "items": [{"field": "status", "fromString": "Open", "toString": "In Progress"}],
        },
        {
            "created": "2026-04-09T08:20:00Z",
            "author": {"displayName": "Anna"},
            "items": [{"field": "assignee", "fromString": "unknown", "toString": "Anna"}],
        },
    ]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    assert changes[0].author == "Anna, Bob"


def test_multi_value_field_swap_pairs_removal_and_addition_per_history():
    # Jira logs a fixVersions swap as two items in the same history: a removal
    # (toString empty) and an addition (fromString empty). Without pairing them
    # per-history, a naive across-history merge can end up chaining the removal
    # fragment as the "final" value instead of what was actually added.
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:10:00Z",
        "author": {"displayName": "Bob"},
        "items": [
            {"field": "Fix Version", "fromString": "PI 3", "toString": None},
            {"field": "Fix Version", "fromString": None, "toString": "PI 1"},
        ],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    assert changes[0].content == "Fix Version: PI 3 → PI 1"


def test_multi_value_field_reverted_across_histories_is_dropped():
    # EGOV-595: Fix Version was swapped PI 3 -> PI 1 -> PI 3 (back to the original).
    # The two swaps must chain to a net no-op and be dropped entirely, not report
    # a stray "PI 3 -> —" from an unpaired removal fragment.
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [
        {
            "created": "2026-04-09T08:10:00Z",
            "author": {"displayName": "Automation for Jira"},
            "items": [
                {"field": "Fix Version", "fromString": "PI 3", "toString": None},
                {"field": "Fix Version", "fromString": None, "toString": "PI 1"},
            ],
        },
        {
            "created": "2026-04-09T08:20:00Z",
            "author": {"displayName": "Automation for Jira"},
            "items": [
                {"field": "Fix Version", "fromString": None, "toString": "PI 3"},
                {"field": "Fix Version", "fromString": "PI 1", "toString": None},
            ],
        },
    ]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    assert not any(i.kind == "field_change" for i in items)


def test_description_change_produces_description_change_kind():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["description"] = "Updated description text"
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:10:00Z",
        "author": {"displayName": "Anna"},
        "items": [{"field": "description", "fromString": "old", "toString": "new"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    desc_items = [i for i in items if i.kind == "description_change"]
    assert len(desc_items) == 1


def test_description_with_mention_produces_mention_kind():
    mention_desc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": "user-123", "text": "@Chris"}},
        ]}],
    }
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["description"] = mention_desc
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:10:00Z",
        "author": {"displayName": "Anna"},
        "items": [{"field": "description", "fromString": "old", "toString": "new"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    mentions = [i for i in items if i.kind == "mention"]
    assert len(mentions) == 1


def test_deduplication_mention_suppresses_comment_and_field_change():
    mention_body = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": "user-123", "text": "@Chris"}},
        ]}],
    }
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [
        {
            "id": "100",
            "body": mention_body,
            "author": {"displayName": "Anna"},
            "updated": "2026-04-09T08:30:00Z",
        },
        {
            "id": "101",
            "body": "Plain comment",
            "author": {"displayName": "Marco"},
            "updated": "2026-04-09T08:35:00Z",
        },
    ]
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:15:00Z",
        "author": {"displayName": "Bob"},
        "items": [{"field": "status", "fromString": "Open", "toString": "Done"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    watched_items = [i for i in items if i.source == "jira" and i.title == "PROJ-1: Fix the bug"]
    assert all(i.kind == "mention" for i in watched_items)


def test_deduplication_comment_suppresses_field_change():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [{
        "id": "101",
        "body": "Plain comment no mention",
        "author": {"displayName": "Marco"},
        "updated": "2026-04-09T08:30:00Z",
    }]
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:15:00Z",
        "author": {"displayName": "Bob"},
        "items": [{"field": "status", "fromString": "Open", "toString": "Done"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    watched = [i for i in items if i.source == "jira" and i.title == "PROJ-1: Fix the bug"]
    assert all(i.kind == "comment" for i in watched)
    assert not any(i.kind == "field_change" for i in watched)


def test_multiple_comments_on_same_ticket_merge_into_one_item():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [
        {"id": "101", "body": "First question", "author": {"displayName": "Marco"}, "updated": "2026-04-09T08:10:00Z"},
        {"id": "102", "body": "Second question", "author": {"displayName": "Anna"}, "updated": "2026-04-09T08:20:00Z"},
    ]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    comments = [i for i in items if i.kind == "comment"]
    assert len(comments) == 1
    assert "First question" in comments[0].content
    assert "Second question" in comments[0].content
    assert comments[0].author == "Anna, Marco"  # every distinct commenter, not just the latest


def test_comment_and_description_change_merge_into_one_item():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["description"] = "Updated description text"
    issue["fields"]["comment"]["comments"] = [
        {"id": "101", "body": "A plain comment", "author": {"displayName": "Marco"}, "updated": "2026-04-09T08:10:00Z"},
    ]
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:20:00Z",
        "author": {"displayName": "Anna"},
        "items": [{"field": "description", "fromString": "old", "toString": "new"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    comments = [i for i in items if i.kind == "comment"]
    assert len(comments) == 1
    assert "A plain comment" in comments[0].content
    assert "Updated description text" in comments[0].content


def test_multiple_mentions_on_same_ticket_merge_with_mention_authors():
    def _mention_body():
        return {
            "type": "doc", "content": [{"type": "paragraph", "content": [
                {"type": "mention", "attrs": {"id": "user-123", "text": "@Chris"}},
                {"type": "text", "text": " please check"},
            ]}],
        }
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [
        {"id": "101", "body": _mention_body(), "author": {"displayName": "Marco"}, "updated": "2026-04-09T08:10:00Z"},
        {"id": "102", "body": _mention_body(), "author": {"displayName": "Anna"}, "updated": "2026-04-09T08:20:00Z"},
    ]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    mentions = [i for i in items if i.kind == "mention"]
    assert len(mentions) == 1
    assert mentions[0].metadata["mention_authors"] == ["Marco", "Anna"]
    assert mentions[0].author == "Anna, Marco"  # every distinct mentioner, not just the latest


def test_comment_before_since_excluded():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [{
        "id": "101",
        "body": "Old comment",
        "author": {"displayName": "Marco"},
        "updated": "2026-04-09T06:00:00Z",  # before SINCE
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    assert not any(i.kind == "comment" for i in items)


def test_field_change_before_since_excluded():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T06:00:00Z",  # before SINCE
        "author": {"displayName": "Bob"},
        "items": [{"field": "status", "fromString": "Open", "toString": "Done"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    assert not any(i.kind == "field_change" for i in items)


def test_empty_projects_returns_nothing():
    cfg = make_config()
    cfg.jira_projects = []
    mock_get, _ = _mock_responses([], user=CURRENT_USER)
    with patch("digest.sources.jira.requests.get", mock_get):
        items = fetch(cfg, "Basic xxx", SINCE)
    assert items == []


def test_invalid_project_key_raises():
    cfg = make_config()
    cfg.jira_projects = ["invalid key"]
    mock_get, _ = _mock_responses([], user=CURRENT_USER)
    with patch("digest.sources.jira.requests.get", mock_get):
        with pytest.raises(ValueError, match="Invalid Jira project key"):
            fetch(cfg, "Basic xxx", SINCE)


def test_jql_extra_appended():
    cfg = make_config()
    cfg.jira_jql_extra = '"Team[Team]" = abc'
    mock_get, mock_post = _mock_responses([])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        fetch(cfg, "Basic xxx", SINCE)
    calls = [call.kwargs["json"]["jql"] for call in mock_post.call_args_list]
    assert all('"Team[Team]" = abc' in jql for jql in calls)


def test_jql_uses_watcher_query():
    mock_get, mock_post = _mock_responses([])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        fetch(make_config(), "Basic xxx", SINCE)
    watched_calls = [
        call.kwargs["json"]["jql"]
        for call in mock_post.call_args_list
        if "watcher" in call.kwargs["json"]["jql"]
    ]
    assert len(watched_calls) >= 1


def test_changelog_fetched_per_issue():
    issue = copy.deepcopy(ISSUE_BASE)
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        fetch(make_config(), "Basic xxx", SINCE)
    get_urls = [call.args[0] for call in mock_get.call_args_list]
    assert any("/changelog" in url for url in get_urls)


# --- _merge_field_changes ---

def _fc(field, from_val, to_val, ts_hour):
    return {"field": field, "from": from_val, "to": to_val, "author": "X",
            "ts": datetime(2026, 4, 9, ts_hour, 0, 0, tzinfo=timezone.utc)}


def test_merge_chains_repeated_field():
    changes = [_fc("status", "Backlog", "To Do", 8), _fc("status", "To Do", "In Progress", 9)]
    result = _merge_field_changes(changes)
    assert len(result) == 1
    assert result[0]["from"] == "Backlog"
    assert result[0]["to"] == "In Progress"


def test_merge_drops_net_zero_change():
    changes = [_fc("labels", "—", "extern", 8), _fc("labels", "extern", "—", 9)]
    assert _merge_field_changes(changes) == []


def test_merge_preserves_independent_fields():
    changes = [_fc("status", "Open", "Done", 8), _fc("assignee", "—", "Anna", 8)]
    result = _merge_field_changes(changes)
    assert len(result) == 2


def test_merge_sorts_by_timestamp():
    # out-of-order input — status change at hour 9 must chain after hour 8
    changes = [_fc("status", "To Do", "In Progress", 9), _fc("status", "Backlog", "To Do", 8)]
    result = _merge_field_changes(changes)
    assert result[0]["from"] == "Backlog"
    assert result[0]["to"] == "In Progress"


# --- _net_field_change ---

def test_net_field_change_single_item_passthrough():
    changes = [{"field": "status", "fromString": "Open", "toString": "In Progress"}]
    assert _net_field_change(changes) == ("Open", "In Progress")


def test_net_field_change_pairs_removal_and_addition():
    changes = [
        {"field": "Fix Version", "fromString": "PI 3", "toString": None},
        {"field": "Fix Version", "fromString": None, "toString": "PI 1"},
    ]
    assert _net_field_change(changes) == ("PI 3", "PI 1")


def test_net_field_change_removal_only():
    changes = [{"field": "Fix Version", "fromString": "PI 3", "toString": None}]
    assert _net_field_change(changes) == ("PI 3", None)


def test_net_field_change_addition_only():
    changes = [{"field": "Fix Version", "fromString": None, "toString": "PI 4"}]
    assert _net_field_change(changes) == (None, "PI 4")


# --- unit helpers ---

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


def test_display_name_null_value_returns_unknown():
    assert _display_name({"displayName": None, "accountId": "abc"}) == "unknown"
    assert _display_name(None) == "unknown"


def test_parse_dt_non_utc_offset():
    result = _parse_dt("2026-04-09T08:30:00+05:30")
    assert result.hour == 3
    assert result.tzinfo == timezone.utc


def test_has_mention_detects_account_id():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"id": "abc-123", "text": "@User"}},
        ]}
    ]}
    assert _has_mention(adf, "abc-123") is True
    assert _has_mention(adf, "other-id") is False


def test_has_mention_returns_false_for_empty_account_id():
    adf = {"type": "mention", "attrs": {"id": "", "text": "@User"}}
    assert _has_mention(adf, "") is False


def test_jql_extra_inserted_before_order_by():
    result = _append_extra(
        'project in (PROJ) AND created >= "2026-01-01" ORDER BY created DESC',
        '"Team[Team]" = abc',
    )
    assert result.index('"Team[Team]"') < result.upper().index("ORDER BY")


def test_rank_field_change_ignored():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:00:00Z",
        "author": {"displayName": "Anna"},
        "items": [{"field": "Rank", "fromString": None, "toString": "Ranked higher"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    assert not any(i.kind == "field_change" for i in items)


def test_duedate_field_change_formatted_as_date_only():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:00:00Z",
        "author": {"displayName": "Anna"},
        "items": [{"field": "duedate", "fromString": None, "toString": "2026-09-27 00:00:00.0"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    assert changes[0].metadata["changes"][0]["to"] == "2026-09-27"


def _make_enrichment_mocks(issue, summaries):
    """Build (mock_get, mock_post) for enrichment tests, routing summary lookups via summaries dict."""
    changelogs = {issue["key"]: issue.get("changelog", {}).get("histories", [])}

    def _get_side_effect(url, *args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.ok = True
        if "/myself" in url:
            resp.json.return_value = CURRENT_USER
        elif "/changelog" in url:
            key = url.split("/issue/")[1].split("/changelog")[0]
            resp.json.return_value = {"values": changelogs.get(key, [])}
        else:
            for key, summary in summaries.items():
                if f"/issue/{key}" in url:
                    resp.json.return_value = {"fields": {"summary": summary}}
                    return resp
            resp.json.return_value = {}
        return resp

    def _post_side_effect(*args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        jql = kwargs.get("json", {}).get("jql", "")
        resp.json.return_value = {"issues": [] if "created >=" in jql else [issue]}
        return resp

    return MagicMock(side_effect=_get_side_effect), MagicMock(side_effect=_post_side_effect)


def test_link_field_change_includes_ticket_summary():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:00:00Z",
        "author": {"displayName": "Anna"},
        "items": [{"field": "Link", "fromString": None, "toString": "This work item blocks EGOV-648"}],
    }]
    mock_get, mock_post = _make_enrichment_mocks(issue, {"EGOV-648": "Fix login bug"})
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    assert "EGOV-648 (Fix login bug)" in changes[0].content


def test_issue_parent_association_field_change_includes_summary():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:00:00Z",
        "author": {"displayName": "Anna"},
        "items": [{"field": "IssueParentAssociation", "fromString": "EGOV-58", "toString": "EGOV-9"}],
    }]
    mock_get, mock_post = _make_enrichment_mocks(issue, {"EGOV-58": "Old parent", "EGOV-9": "New parent"})
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    assert "EGOV-58 (Old parent)" in changes[0].content
    assert "EGOV-9 (New parent)" in changes[0].content


def _remote_link_issue(quoted_title):
    issue = copy.deepcopy(ISSUE_BASE)
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:00:00Z",
        "author": {"displayName": "Anna"},
        "items": [{
            "field": "RemoteWorkItemLink", "fromString": None,
            "toString": f'This work item links to "{quoted_title}"',
        }],
    }]
    return issue


def _remote_link_mocks(issue, remote_link_objects):
    def _get_side_effect(url, *args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.ok = True
        if "/myself" in url:
            resp.json.return_value = CURRENT_USER
        elif "/changelog" in url:
            resp.json.return_value = {"values": issue["changelog"]["histories"]}
        elif "/remotelink" in url:
            resp.json.return_value = remote_link_objects
        else:
            resp.json.return_value = {}
        return resp

    def _post_side_effect(*args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        jql = kwargs.get("json", {}).get("jql", "")
        resp.json.return_value = {"issues": [] if "created >=" in jql else [issue]}
        return resp

    return MagicMock(side_effect=_get_side_effect), MagicMock(side_effect=_post_side_effect)


def test_remote_work_item_link_field_change_is_hyperlinked():
    issue = _remote_link_issue("Migrationskonzept (System Confluence)")
    mock_get, mock_post = _remote_link_mocks(issue, [
        {"object": {"title": "Migrationskonzept", "url": "https://confluence.example.com/migrationskonzept"}},
    ])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    to_value = changes[0].metadata["changes"][0]["to"]
    assert to_value == '"[<a href="https://confluence.example.com/migrationskonzept">Migrationskonzept</a>]"'


def test_remote_work_item_link_matches_exact_quoted_title():
    issue = _remote_link_issue("Page (Confluence)")
    mock_get, mock_post = _remote_link_mocks(issue, [
        {"object": {"title": "Page (Confluence)", "url": "https://confluence.example.com/page"}},
    ])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    to_value = changes[0].metadata["changes"][0]["to"]
    assert to_value == '"[<a href="https://confluence.example.com/page">Page</a>]"'


def test_remote_work_item_link_without_matching_remote_link_left_plain():
    issue = _remote_link_issue("Page (Confluence)")
    mock_get, mock_post = _remote_link_mocks(issue, [])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    changes = [i for i in items if i.kind == "field_change"]
    to_value = changes[0].metadata["changes"][0]["to"]
    assert to_value == 'This work item links to "Page (Confluence)"'
    assert "<a" not in to_value


# --- status → Done "unblocks" detection ---

def _status_change_history(from_status="In Progress", to_status="Done"):
    return [{
        "created": "2026-04-09T08:15:00Z",
        "author": {"displayName": "Bob"},
        "items": [{"field": "status", "fromString": from_status, "toString": to_status}],
    }]


def _status_change_item(items):
    changes = [i for i in items if i.kind == "field_change"]
    assert len(changes) == 1
    return next(c for c in changes[0].metadata["changes"] if c["field"] == "status")


def test_status_change_to_done_adds_unblocks_metadata():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["status"] = {"name": "Done", "statusCategory": {"key": "done"}}
    issue["fields"]["issuelinks"] = [{
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "outwardIssue": {
            "key": "PROJ-2",
            "fields": {"summary": "Deploy the fix", "status": {"statusCategory": {"key": "indeterminate"}}},
        },
    }]
    issue["changelog"]["histories"] = _status_change_history()
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    status_change = _status_change_item(items)
    assert status_change["unblocks"] == [{
        "key": "PROJ-2", "title": "Deploy the fix", "url": "https://example.atlassian.net/browse/PROJ-2",
    }]


def test_multiple_unblocks_entries_collected():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["status"] = {"name": "Done", "statusCategory": {"key": "done"}}
    issue["fields"]["issuelinks"] = [
        {
            "type": {"outward": "blocks"},
            "outwardIssue": {"key": "PROJ-2", "fields": {"summary": "A", "status": {"statusCategory": {"key": "new"}}}},
        },
        {
            "type": {"outward": "has to be done before"},
            "outwardIssue": {"key": "PROJ-3", "fields": {"summary": "B", "status": {"statusCategory": {"key": "indeterminate"}}}},
        },
    ]
    issue["changelog"]["histories"] = _status_change_history()
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    status_change = _status_change_item(items)
    assert {u["key"] for u in status_change["unblocks"]} == {"PROJ-2", "PROJ-3"}


def test_unblocks_excludes_already_done_blocked_ticket():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["status"] = {"name": "Done", "statusCategory": {"key": "done"}}
    issue["fields"]["issuelinks"] = [{
        "type": {"outward": "blocks"},
        "outwardIssue": {"key": "PROJ-2", "fields": {"summary": "Already finished", "status": {"statusCategory": {"key": "done"}}}},
    }]
    issue["changelog"]["histories"] = _status_change_history()
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    assert "unblocks" not in _status_change_item(items)


def test_non_blocking_link_type_ignored():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["status"] = {"name": "Done", "statusCategory": {"key": "done"}}
    issue["fields"]["issuelinks"] = [{
        "type": {"outward": "relates to"},
        "outwardIssue": {"key": "PROJ-2", "fields": {"summary": "Unrelated", "status": {"statusCategory": {"key": "new"}}}},
    }]
    issue["changelog"]["histories"] = _status_change_history()
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    assert "unblocks" not in _status_change_item(items)


def test_status_change_not_to_done_no_unblocks():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["status"] = {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}
    issue["fields"]["issuelinks"] = [{
        "type": {"outward": "blocks"},
        "outwardIssue": {"key": "PROJ-2", "fields": {"summary": "X", "status": {"statusCategory": {"key": "new"}}}},
    }]
    issue["changelog"]["histories"] = _status_change_history(from_status="Open", to_status="In Progress")
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    assert "unblocks" not in _status_change_item(items)


def test_jql_search_paginates_via_next_page_token():
    """A previous version of _jql_search fetched only the first 50 results and
    warned instead of paginating, silently dropping the rest -- mgmt_jira.py's
    _paginate_jql already did this correctly for the same endpoint via
    nextPageToken/isLast, so this must follow the same pagination."""
    page1 = {"issues": [{"key": f"PROJ-{i}"} for i in range(50)], "isLast": False, "nextPageToken": "tok1"}
    page2 = {"issues": [{"key": "PROJ-50"}], "isLast": True}

    def fake_post(url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = page2 if (json or {}).get("nextPageToken") == "tok1" else page1
        return resp

    with patch("digest.sources.jira.requests.post", side_effect=fake_post):
        issues = _jql_search(make_config(), "Basic xxx", "project = PROJ")

    assert len(issues) == 51
