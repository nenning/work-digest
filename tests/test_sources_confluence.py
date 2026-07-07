import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from digest.config import AtlassianConfig
from digest.sources.confluence import fetch, _validate_space_keys, _compute_diff, _storage_to_text

SINCE = datetime(2026, 4, 9, 7, 0, 0, tzinfo=timezone.utc)

PAGE = {
    "id": "page-123",
    "title": "Sprint 14 Retro",
    "_links": {"webui": "/spaces/ENG/pages/123"},
    "history": {"createdBy": {"displayName": "Anna"}, "createdDate": "2026-04-09T08:00:00Z"},
    "version": {"number": 3, "by": {"displayName": "Anna"}, "when": "2026-04-09T08:00:00Z"},
}

CURR_BODY = {"body": {"storage": {"value": "<p>New section about deployment pipeline.</p>"}}}
PREV_BODY = {"body": {"storage": {"value": "<p>Old draft content here.</p>"}}}


def make_config():
    return AtlassianConfig(
        url="https://example.atlassian.net",
        email="u@e.com", api_token="tok",
        jira_projects=[], confluence_spaces=["ENG"],
    )


def make_mock(json_data):
    m = MagicMock()
    m.json.return_value = json_data
    m.raise_for_status = lambda: None
    return m


def test_fetch_page_updates():
    responses = [
        make_mock({"accountId": "user-abc"}),                          # /wiki/rest/api/user/current
        make_mock({"results": []}),                                    # mentions CQL
        make_mock({"results": [PAGE]}),                                # page updates CQL
        make_mock({"version": {"when": "2026-04-09T06:00:00Z"}}),     # baseline metadata (v2, before SINCE)
        make_mock(CURR_BODY),                                          # _fetch_page_diff: current body
        make_mock(PREV_BODY),                                          # _fetch_page_diff: baseline body
    ]
    with patch("digest.sources.confluence.requests.get", side_effect=responses):
        items = fetch(make_config(), "Basic xxx", SINCE)

    page_updates = [i for i in items if i.kind == "page_update"]
    assert len(page_updates) == 1
    assert "Sprint 14 Retro" in page_updates[0].title
    # Title should be clean — no "Updated:" prefix
    assert not page_updates[0].title.startswith("Updated:")


def test_cosmetic_only_page_skipped():
    """Pages with only trivial changes are excluded from results."""
    responses = [
        make_mock({"accountId": "user-abc"}),
        make_mock({"results": []}),
        make_mock({"results": [PAGE]}),
        make_mock({"version": {"when": "2026-04-09T06:00:00Z"}}),            # baseline metadata (v2, before SINCE)
        make_mock({"body": {"storage": {"value": "<p>Same content</p>"}}}),  # current body
        make_mock({"body": {"storage": {"value": "<p>Same content</p>"}}}),  # baseline body (identical)
    ]
    with patch("digest.sources.confluence.requests.get", side_effect=responses):
        items = fetch(make_config(), "Basic xxx", SINCE)

    page_updates = [i for i in items if i.kind == "page_update"]
    assert len(page_updates) == 0


def test_no_spaces_skips_page_updates():
    cfg = make_config()
    cfg.confluence_spaces = []
    responses = [
        make_mock({"accountId": "user-abc"}),
        make_mock({"results": []}),
    ]
    with patch("digest.sources.confluence.requests.get", side_effect=responses):
        items = fetch(cfg, "Basic xxx", SINCE)
    assert all(i.kind != "page_update" for i in items)


def test_fetch_mentions():
    mention = {
        "title": "API Guidelines",
        "_links": {"webui": "/spaces/ENG/pages/456"},
        "history": {"createdBy": {"displayName": "Bob"}, "createdDate": "2026-04-09T08:00:00Z"},
    }
    responses = [
        make_mock({"accountId": "user-abc"}),
        make_mock({"results": [mention]}),    # mentions
        make_mock({"results": []}),           # page updates
    ]
    with patch("digest.sources.confluence.requests.get", side_effect=responses):
        items = fetch(make_config(), "Basic xxx", SINCE)

    mentions = [i for i in items if i.kind == "mention"]
    assert len(mentions) == 1
    assert "API Guidelines" in mentions[0].title
    assert not mentions[0].title.startswith("Mentioned in:")
    assert mentions[0].author == "Bob"


def test_multiple_mentions_on_same_page_merge():
    mention1 = {
        "title": "API Guidelines",
        "type": "comment",
        "id": "c1",
        "_links": {"webui": "/spaces/ENG/pages/456?focusedCommentId=1#comment-1"},
        "history": {"createdBy": {"displayName": "Bob"}, "createdDate": "2026-04-09T08:00:00Z"},
    }
    mention2 = {
        "title": "API Guidelines",
        "type": "comment",
        "id": "c2",
        "_links": {"webui": "/spaces/ENG/pages/456?focusedCommentId=2#comment-2"},
        "history": {"createdBy": {"displayName": "Anna"}, "createdDate": "2026-04-09T09:00:00Z"},
    }
    responses = [
        make_mock({"accountId": "user-abc"}),                                          # user id
        make_mock({"results": [mention1, mention2]}),                                  # mentions CQL
        make_mock({"body": {"storage": {"value": "<p>Please fix ASAP</p>"}}}),          # mention1 comment body
        make_mock({"body": {"storage": {"value": "<p>Also check this</p>"}}}),          # mention2 comment body
        make_mock({"results": []}),                                                    # page updates CQL
    ]
    with patch("digest.sources.confluence.requests.get", side_effect=responses):
        items = fetch(make_config(), "Basic xxx", SINCE)

    merged = [i for i in items if i.kind == "page"]
    assert len(merged) == 1
    assert merged[0].url == "https://example.atlassian.net/wiki/spaces/ENG/pages/456"
    assert "Bob" in merged[0].content
    assert "Anna" in merged[0].content
    assert "Please fix ASAP" in merged[0].content
    assert "Also check this" in merged[0].content


def test_invalid_space_key_raises():
    with pytest.raises(ValueError, match="Invalid Confluence space key"):
        _validate_space_keys(["lowercase"])


def test_valid_space_keys_pass():
    _validate_space_keys(["ENG", "DOC2", "A1"])  # no exception


def test_page_updates_paginates_via_cursor_link():
    """Confluence's CQL search paginates via an opaque `_links.next` cursor, not a
    client-incremented `start` (see mgmt_confluence._search_all for the live-API
    evidence) -- a page of results followed by `_links.next` must not be treated
    as the end, or results past the first 50 are silently dropped."""
    base = "https://example.atlassian.net/wiki"
    batch1 = [dict(PAGE, id=f"page-{i}", title=f"Page {i}", _links={"webui": f"/spaces/ENG/pages/{i}"}) for i in range(50)]
    batch2 = [dict(PAGE, id="page-50", title="Page 50", _links={"webui": "/spaces/ENG/pages/50"})]

    responses = [
        make_mock({"accountId": "user-abc"}),                                          # /wiki/rest/api/user/current
        make_mock({"results": []}),                                                    # mentions CQL
        make_mock({                                                                    # page updates CQL, page 1
            "results": batch1,
            "_links": {"base": base, "next": "/rest/api/content/search?next=true&cursor=abc"},
        }),
        make_mock({"results": batch2}),                                                # page updates CQL, page 2 (last)
    ]
    with patch("digest.sources.confluence.requests.get", side_effect=responses), \
         patch("digest.sources.confluence._fetch_page_diff", return_value="Added: something significant"):
        items = fetch(make_config(), "Basic xxx", SINCE)

    assert len(items) == 51


def test_compute_diff_returns_none_for_identical():
    assert _compute_diff("same line", "same line") is None


def test_compute_diff_returns_diff_for_changes():
    result = _compute_diff("old content here", "new content added here significantly")
    assert result is not None


def test_storage_to_text_strips_tags():
    result = _storage_to_text("<p>Hello <strong>world</strong></p>")
    assert "Hello" in result
    assert "world" in result
    assert "<" not in result


def test_storage_to_text_status_macro_uses_title_only():
    """Status macros must render as the title only, not "GreenCore" or "RedLEGACY"."""
    green_core = (
        '<ac:structured-macro ac:name="status" ac:schema-version="1">'
        '<ac:parameter ac:name="colour">Green</ac:parameter>'
        '<ac:parameter ac:name="title">Core</ac:parameter>'
        '</ac:structured-macro>'
    )
    red_legacy = (
        '<ac:structured-macro ac:name="status" ac:schema-version="1">'
        '<ac:parameter ac:name="colour">Red</ac:parameter>'
        '<ac:parameter ac:name="title">Legacy</ac:parameter>'
        '</ac:structured-macro>'
    )
    result_core = _storage_to_text(green_core)
    assert result_core == "Core"
    assert "Green" not in result_core

    result_legacy = _storage_to_text(red_legacy)
    assert result_legacy == "Legacy"
    assert "Red" not in result_legacy


def test_storage_to_text_table_keeps_rows_grouped():
    """Table cells must stay grouped by row so a value keeps its column context
    instead of collapsing into an undifferentiated flat list of header + values."""
    table_html = (
        "<table><tbody>"
        "<tr><th>Stream</th><th>Status</th><th>Risks</th></tr>"
        "<tr><td>Portalplattform High Code</td><td>On Track</td><td>None</td></tr>"
        "<tr><td>Low Code Plattform</td><td>At Risk</td><td>Dependency on Team X</td></tr>"
        "</tbody></table>"
    )
    result = _storage_to_text(table_html)
    lines = result.splitlines()
    assert "Stream | Status | Risks" in lines
    assert "Portalplattform High Code | On Track | None" in lines
    assert "Low Code Plattform | At Risk | Dependency on Team X" in lines


def test_storage_to_text_table_cell_with_nested_tags():
    table_html = "<table><tbody><tr><td><p>Alice</p></td><td>Backend <strong>lead</strong></td></tr></tbody></table>"
    result = _storage_to_text(table_html)
    assert result == "Alice | Backend lead"


def test_compute_diff_keeps_short_table_row_with_long_row_context():
    """A row's cells are diffed together, so a short status value survives the
    trivial-change filter as long as its row carries enough content overall."""
    old_table = "<table><tbody></tbody></table>"
    new_table = (
        "<table><tbody>"
        "<tr><td>Portalplattform High Code</td><td>On Track</td><td>None</td></tr>"
        "</tbody></table>"
    )
    diff = _compute_diff(_storage_to_text(old_table), _storage_to_text(new_table))
    assert diff is not None
    assert "On Track" in diff
    assert "Portalplattform High Code" in diff
