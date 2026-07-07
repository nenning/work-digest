"""Tests for digest.sources.mgmt_confluence."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from digest.config import AtlassianConfig, MgmtSummaryConfig
from digest.sources.mgmt_confluence import fetch_team_pages


def make_atlassian_config() -> AtlassianConfig:
    return AtlassianConfig(
        url="https://example.atlassian.net",
        email="u@e.com",
        api_token="tok",
        jira_projects=[],
        confluence_spaces=[],
    )


def make_mgmt_cfg(**kwargs) -> MgmtSummaryConfig:
    defaults = dict(jira_jql="project = TEAM")
    defaults.update(kwargs)
    return MgmtSummaryConfig(**defaults)


AUTH = "Basic xxx"
SINCE = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 5, 31, 0, 0, 0, tzinfo=timezone.utc)
TEAM_IDS = {"a1", "a2"}

_NEW_PAGE_BODY = "<p>This is a brand new page with substantial content describing the rollout plan.</p>"


def _make_page(page_id="1", title="My Page", webui="/pages/1",
               author_id="a1", author_name="Alice", when="2026-05-10T09:00:00Z",
               version_number=1):
    return {
        "id": page_id,
        "title": title,
        "_links": {"webui": webui},
        "version": {
            "number": version_number,
            "by": {"accountId": author_id, "displayName": author_name},
            "when": when,
        },
    }


def _search_resp(results):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {"results": results}
    return resp


def _version_resp(author_id, author_name, when):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {
        "version": {"by": {"accountId": author_id, "displayName": author_name}, "when": when}
    }
    return resp


def _body_resp(html):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {"body": {"storage": {"value": html}}}
    return resp


def _default_body_get(pages_or_page):
    """Generic fake_get: search returns given page(s); any body.storage fetch
    returns substantive new-page content (used when no history-walk is expected)."""
    results = pages_or_page if isinstance(pages_or_page, list) else [pages_or_page]

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            return _search_resp(results)
        if params.get("expand") == "body.storage":
            return _body_resp(_NEW_PAGE_BODY)
        raise AssertionError(f"unexpected call: {url} {params}")

    return fake_get


# ---------------------------------------------------------------------------
# fetch_team_pages
# ---------------------------------------------------------------------------

def test_fetch_team_pages_basic():
    pages = [
        _make_page("1", "Page One", "/pages/1", "a1", "Alice", "2026-05-10T09:00:00Z"),
        _make_page("2", "Page Two", "/pages/2", "a2", "Bob", "2026-05-12T10:00:00Z"),
    ]

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=_default_body_get(pages)):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    items.sort(key=lambda i: i.title)
    assert len(items) == 2
    assert items[0].kind == "page_update"
    assert items[0].title == "Page One"
    assert items[0].author == "Alice"
    assert "https://example.atlassian.net/wiki/pages/1" in items[0].url
    assert items[0].timestamp == datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
    assert items[1].title == "Page Two"


def test_fetch_team_pages_new_page_diffs_against_empty_baseline():
    """A page created within the window (version 1) has no history to diff against,
    so the whole body should show up as newly added content."""
    page = _make_page("1", "New Page", "/pages/1", version_number=1)

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=_default_body_get(page)):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert len(items) == 1
    assert "rollout plan" in items[0].content
    assert items[0].content.startswith("Added:")


def test_fetch_team_pages_finds_overwritten_team_edit():
    """A team member's edit that was later overwritten by a non-team member in the
    same window must still surface -- the bug being fixed: filtering on
    lastModifier alone would silently drop this edit."""
    page = _make_page("1", "Shared Page", "/pages/1", author_id="outsider",
                       author_name="Outsider", when="2026-05-15T12:00:00Z", version_number=2)

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            return _search_resp([page])
        if url.endswith("/content/1"):
            expand = params.get("expand")
            status = params.get("status")
            version = params.get("version")
            if expand == "version" and status == "historical" and version == 1:
                return _version_resp("a1", "Alice", "2026-05-10T09:00:00Z")
            if expand == "body.storage" and status == "historical" and version == 2:
                return _body_resp("<p>Current content after the outsider's later edit landed here.</p>")
            if expand == "body.storage" and status == "historical" and version == 1:
                return _body_resp("<p>Original content before Alice made her substantive edit.</p>")
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert len(items) == 1
    assert items[0].author == "Alice"
    assert items[0].timestamp == datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)


def test_fetch_team_pages_credits_all_team_authors_in_window():
    """Two different team members editing the same page within the window must both
    be credited -- previously only whichever one the history-walk hit first surfaced."""
    page = _make_page("1", "Shared Page", "/pages/1", author_id="a2", author_name="Bob",
                       when="2026-05-20T12:00:00Z", version_number=3)

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            return _search_resp([page])
        if url.endswith("/content/1"):
            expand = params.get("expand")
            status = params.get("status")
            version = params.get("version")
            if expand == "version" and status == "historical" and version == 2:
                return _version_resp("a1", "Alice", "2026-05-12T09:00:00Z")
            if expand == "version" and status == "historical" and version == 1:
                return _version_resp("a1", "Alice", "2026-04-01T09:00:00Z")  # baseline, before SINCE
            if expand == "body.storage" and status == "historical" and version == 3:
                return _body_resp(_NEW_PAGE_BODY)
            if expand == "body.storage" and status == "historical" and version == 1:
                return _body_resp("<p>Original baseline content before either edit.</p>")
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, {"a1", "a2"})

    assert len(items) == 1
    assert items[0].author == "Alice, Bob"
    assert items[0].timestamp == datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_fetch_team_pages_diff_pinned_to_cql_verified_version_not_live_body():
    """The diff must reflect the version the CQL search confirmed is within the
    window (lastModified <= until), not whatever is live on the page right now --
    otherwise a report for a past window (e.g. --sprint or --to) would pick up
    edits made after `until` but before the report happened to run."""
    page = _make_page("1", "Page", "/pages/1", version_number=2)

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            return _search_resp([page])
        if url.endswith("/content/1"):
            expand = params.get("expand")
            status = params.get("status")
            version = params.get("version")
            if expand == "version" and status == "historical" and version == 1:
                return _version_resp("outsider", "Outsider", "2026-04-01T09:00:00Z")  # before SINCE -> baseline
            if expand == "body.storage" and status == "historical" and version == 1:
                return _body_resp("<p>Old baseline text before the window.</p>")
            if expand == "body.storage" and status == "historical" and version == 2:
                return _body_resp(_NEW_PAGE_BODY)
            if expand == "body.storage" and (status is None or version is None):
                raise AssertionError("must not fetch the live/unpinned body")
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert len(items) == 1
    assert "rollout plan" in items[0].content


def test_fetch_team_pages_no_team_edit_within_window_excluded():
    """Contributor matched historically, but that edit predates `since` -- the page
    should not be reported as team activity for this period."""
    page = _make_page("1", "Old Contribution", "/pages/1", author_id="outsider",
                       author_name="Outsider", when="2026-05-15T12:00:00Z", version_number=2)

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            return _search_resp([page])
        if url.endswith("/content/1") and params.get("version") == 1:
            return _version_resp("a1", "Alice", "2026-04-01T09:00:00Z")  # before SINCE
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert items == []


def test_fetch_team_pages_paginates_beyond_first_batch():
    """Confluence's CQL search paginates via an opaque `_links.next` cursor, not a
    client-incremented `start` -- following `start` manually re-fetches page 1
    forever (the actual bug this guards against). Only `_links.next` may be used
    to reach subsequent pages."""
    batch1 = [_make_page(str(i), f"Page {i}") for i in range(50)]
    batch2 = [_make_page("50", "Page 50")]
    base = "https://example.atlassian.net/wiki"
    next_url = f"{base}/rest/api/content/search?next=true&cursor=abc"

    def fake_get(url, headers=None, params=None, timeout=None):
        if url == f"{base}/rest/api/content/search":
            resp = _search_resp(batch1)
            resp.json.return_value["_links"] = {"base": base, "next": "/rest/api/content/search?next=true&cursor=abc"}
            return resp
        if url == next_url:
            return _search_resp(batch2)  # no _links.next -> this is the last page
        if params and params.get("expand") == "body.storage":
            return _body_resp(_NEW_PAGE_BODY)
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert len(items) == 51


def test_fetch_team_pages_stops_at_page_safety_cap_instead_of_looping_forever():
    """Regression test for the actual production bug: if the search response ever
    echoes the same `_links.next` cursor forever (as this Confluence instance did
    when `start` was used instead of the cursor), we must bail out via the page
    cap rather than looping until the process is killed."""
    from digest.sources.mgmt_confluence import _MAX_SEARCH_PAGES

    base = "https://example.atlassian.net/wiki"
    same_next = "/rest/api/content/search?next=true&cursor=stuck"
    call_count = {"n": 0}

    page = _make_page("1", "Loops Forever", author_id="outsider", author_name="Outsider")

    def fake_get(url, headers=None, params=None, timeout=None):
        call_count["n"] += 1
        resp = _search_resp([page])
        resp.json.return_value["_links"] = {"base": base, "next": same_next}
        return resp

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert call_count["n"] == _MAX_SEARCH_PAGES
    assert items == []  # non-team author, resolved without further HTTP calls


def test_fetch_team_pages_empty_team_ids_no_http_call():
    mock_get = MagicMock()
    with patch("digest.sources.mgmt_confluence.requests.get", mock_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, set())
    assert items == []
    mock_get.assert_not_called()


def test_fetch_team_pages_ignore_users_excludes_edit():
    """ignore_users must also apply to Confluence contributions, not just Jira
    tickets -- an ignored bot/tester's page edits shouldn't surface here even if
    their accountId is in team_account_ids."""
    page = _make_page("1", "Bot Edited Page", "/pages/1", author_id="bot1", author_name="Bot User")
    cfg = make_mgmt_cfg(ignore_users=["Bot User"])

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=_default_body_get(page)):
        items = fetch_team_pages(make_atlassian_config(), AUTH, cfg, SINCE, UNTIL, {"bot1", "a2"})

    assert items == []


def test_fetch_team_pages_missing_author_defaults_to_unknown():
    page = _make_page("1", "Orphan Page", "/pages/1", author_id="a1", author_name=None)
    page["version"]["by"] = {"accountId": "a1"}  # no displayName

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=_default_body_get(page)):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)
    assert len(items) == 1
    assert items[0].author == "unknown"


def test_fetch_team_pages_no_results():
    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            return _search_resp([])
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)
    assert items == []


def test_fetch_team_pages_scopes_cql_to_configured_spaces():
    """Without a space restriction, 'contributor in (...)' scans the whole instance
    and is slow enough to look like a hang on a wide --since range. It must be
    scoped the same way the personal digest's confluence.py scopes its search."""
    config = make_atlassian_config()
    config.confluence_spaces = ["ENG", "DOC"]
    captured_cql = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            captured_cql["cql"] = params["cql"]
            return _search_resp([])
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        fetch_team_pages(config, AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert 'space = "ENG"' in captured_cql["cql"]
    assert 'space = "DOC"' in captured_cql["cql"]


def test_fetch_team_pages_no_space_restriction_when_unconfigured():
    captured_cql = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            captured_cql["cql"] = params["cql"]
            return _search_resp([])
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert "space" not in captured_cql["cql"]


def test_fetch_team_pages_inaccessible_page_skipped_others_kept():
    """A page we lose access to between search and detail-fetch (403) must not
    take down the other pages resolved in the same batch."""
    import requests as requests_module

    forbidden = _make_page("1", "Restricted Page", "/pages/1", version_number=1)
    ok = _make_page("2", "Open Page", "/pages/2", version_number=1)

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            return _search_resp([forbidden, ok])
        if url.endswith("/content/1") and params.get("expand") == "body.storage":
            resp = MagicMock()
            resp.raise_for_status.side_effect = requests_module.exceptions.HTTPError("403 Forbidden")
            return resp
        if url.endswith("/content/2") and params.get("expand") == "body.storage":
            return _body_resp(_NEW_PAGE_BODY)
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert len(items) == 1
    assert items[0].title == "Open Page"


def test_fetch_team_pages_cosmetic_only_change_skipped():
    """A page whose diff is purely cosmetic (short lines) should be dropped, same as
    the personal digest's page-update handling."""
    page = _make_page("1", "Typo Fix Page", "/pages/1", version_number=1)

    def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/content/search"):
            return _search_resp([page])
        if params.get("expand") == "body.storage":
            return _body_resp("<p>Hi</p>")  # trivially short -> no significant diff vs ""
        raise AssertionError(f"unexpected call: {url} {params}")

    with patch("digest.sources.mgmt_confluence.requests.get", side_effect=fake_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert items == []
