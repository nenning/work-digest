"""Tests for digest.sources.mgmt_confluence."""
import warnings
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

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


def make_mgmt_cfg() -> MgmtSummaryConfig:
    return MgmtSummaryConfig(jira_jql="project = TEAM")


AUTH = "Basic xxx"
SINCE = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 5, 31, 0, 0, 0, tzinfo=timezone.utc)
TEAM_IDS = {"a1", "a2"}


def _make_page(page_id="1", title="My Page", webui="/pages/1",
               author_name="Alice", when="2026-05-10T09:00:00Z"):
    return {
        "id": page_id,
        "title": title,
        "_links": {"webui": webui},
        "version": {
            "by": {"displayName": author_name},
            "when": when,
        },
    }


def _search_resp(results, total=None):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {
        "results": results,
        "totalSize": total if total is not None else len(results),
    }
    return resp


# ---------------------------------------------------------------------------
# fetch_team_pages
# ---------------------------------------------------------------------------

def test_fetch_team_pages_basic():
    pages = [
        _make_page("1", "Page One", "/pages/1", "Alice", "2026-05-10T09:00:00Z"),
        _make_page("2", "Page Two", "/pages/2", "Bob", "2026-05-12T10:00:00Z"),
    ]
    mock_get = MagicMock(return_value=_search_resp(pages))
    with patch("digest.sources.mgmt_confluence.requests.get", mock_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)

    assert len(items) == 2
    assert items[0].kind == "page_update"
    assert items[0].title == "Page One"
    assert items[0].author == "Alice"
    assert "https://example.atlassian.net/wiki/pages/1" in items[0].url
    assert items[0].timestamp == datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
    assert items[1].title == "Page Two"


def test_fetch_team_pages_empty_team_ids_no_http_call():
    mock_get = MagicMock()
    with patch("digest.sources.mgmt_confluence.requests.get", mock_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, set())
    assert items == []
    mock_get.assert_not_called()


def test_fetch_team_pages_truncation_warning():
    results = [_make_page(str(i), f"Page {i}") for i in range(50)]
    mock_get = MagicMock(return_value=_search_resp(results, total=80))
    with patch("digest.sources.mgmt_confluence.requests.get", mock_get):
        with pytest.warns(RuntimeWarning, match=r"50\+"):
            fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)


def test_fetch_team_pages_missing_author_defaults_to_unknown():
    page = {
        "id": "1",
        "title": "Orphan Page",
        "_links": {"webui": "/pages/1"},
        "version": {"when": "2026-05-10T09:00:00Z"},
    }
    mock_get = MagicMock(return_value=_search_resp([page]))
    with patch("digest.sources.mgmt_confluence.requests.get", mock_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)
    assert len(items) == 1
    assert items[0].author == "unknown"


def test_fetch_team_pages_no_results():
    mock_get = MagicMock(return_value=_search_resp([]))
    with patch("digest.sources.mgmt_confluence.requests.get", mock_get):
        items = fetch_team_pages(make_atlassian_config(), AUTH, make_mgmt_cfg(), SINCE, UNTIL, TEAM_IDS)
    assert items == []
