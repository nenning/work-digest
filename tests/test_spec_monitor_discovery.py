from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from digest.config import SpecMonitorConfig
from digest.spec_monitor import discovery
from digest.spec_monitor.confluence_client import ConfluenceClient
from digest.spec_monitor.discovery import discover_specs
from digest.spec_monitor.models import PageProperties


def details_macro(macro_id):
    return (
        '<ac:structured-macro ac:name="details" ac:schema-version="1">'
        f'<ac:parameter ac:name="id">{macro_id}</ac:parameter>'
        '<ac:rich-text-body><table><tr><th>Status</th><td>x</td></tr></table></ac:rich-text-body>'
        '</ac:structured-macro>'
    )


PAGES = {
    "1": {"title": "API Customer", "body": details_macro("DETAILSPEC"), "created": "2026-09-01T08:00:00.000Z"},
    "2": {"title": "Utility Core", "body": details_macro("DETAILSPEC") + "<p>second</p>", "created": "2026-09-02T08:00:00.000Z"},
    "3": {"title": "Regeln", "body": "<p>no macro</p>", "created": "2026-09-03T08:00:00.000Z"},
    "4": {"title": "Other macro", "body": details_macro("OTHER"), "created": "2026-09-03T08:00:00.000Z"},
}

PROPS = {
    "API Customer": PageProperties(status="Refined", jira=["PROJ-1"], stand_date=date(2026, 10, 27)),
    "Utility Core": PageProperties(status="In Ausarbeitung"),
}


@pytest.fixture
def client(mocker):
    c = MagicMock(spec=ConfluenceClient)
    c.search_descendants.side_effect = lambda parent, label=None: [
        {"id": pid, "title": p["title"], "version": 1} for pid, p in PAGES.items()
        if label is None or pid in ("1", "2")
    ]
    c.get_page.side_effect = lambda pid: {
        "id": pid,
        "title": PAGES[pid]["title"],
        "body": {"storage": {"value": PAGES[pid]["body"]}},
        "history": {"createdDate": PAGES[pid]["created"]},
    }
    c.page_url.side_effect = lambda pid, version=None: f"https://x/p/{pid}"

    def by_body(body):
        for page in PAGES.values():
            if page["body"] == body and page["title"] in PROPS:
                return PROPS[page["title"]]
        return PageProperties()

    mocker.patch.object(discovery, "_parse_properties", side_effect=lambda body: by_body(body))
    return c


def cfg(**kw):
    base = dict(parent_page_id="668", selector_type="page_properties_id", selector_value="DETAILSPEC")
    base.update(kw)
    return SpecMonitorConfig(**base)


def test_page_properties_selector_keeps_matching_macro_only(client):
    d = discover_specs(client, cfg())
    assert [s.page_id for s in d.specs] == ["1", "2"]
    assert d.excluded == [("3", "Regeln"), ("4", "Other macro")]
    client.search_descendants.assert_called_once_with("668", None)


def test_specs_carry_properties_and_url(client):
    d = discover_specs(client, cfg())
    s = d.specs[0]
    assert s.title == "API Customer" and s.status == "Refined"
    assert s.jira == ["PROJ-1"] and s.stand_date == date(2026, 10, 27)
    assert s.url == "https://x/p/1"
    assert isinstance(s.created, datetime) and s.created.year == 2026


def test_specs_carry_all_ancestor_levels(client):
    orig = client.get_page.side_effect

    def with_ancestors(pid):
        page = orig(pid)
        page["ancestors"] = [
            {"id": 11, "title": "Home"},
            {"id": 668, "title": "Detailspezifikationen"},
        ]
        return page

    client.get_page.side_effect = with_ancestors
    d = discover_specs(client, cfg())
    for s in d.specs:
        assert s.ancestor_ids == ["11", "668"]
        assert s.ancestor_titles == ["Home", "Detailspezifikationen"]


def test_missing_ancestors_default_empty(client):
    d = discover_specs(client, cfg())
    assert d.specs[0].ancestor_ids == [] and d.specs[0].ancestor_titles == []


def test_body_only_kept_for_analysed_states(client):
    d = discover_specs(client, cfg())
    assert d.specs[0].body_storage != ""
    assert d.specs[1].status == "In Ausarbeitung" and d.specs[1].body_storage == ""


def test_body_kept_for_every_state_with_analyse_all_states(client):
    d = discover_specs(client, cfg(analyse_all_states=True))
    assert d.specs[1].status == "In Ausarbeitung" and d.specs[1].body_storage != ""


def test_label_selector_uses_label_search_and_excludes_rest(client):
    d = discover_specs(client, cfg(selector_type="label", selector_value="detailspec"))
    calls = [c.args for c in client.search_descendants.call_args_list]
    assert ("668", "detailspec") in calls
    assert [s.page_id for s in d.specs] == ["1", "2"]
    assert d.excluded == [("3", "Regeln"), ("4", "Other macro")]


def test_unreadable_candidate_is_reported_as_error_not_excluded(client):
    orig = client.get_page.side_effect

    def flaky(pid):
        if pid == "3":
            raise LookupError("gone")
        return orig(pid)

    client.get_page.side_effect = flaky
    d = discover_specs(client, cfg())
    assert [s.page_id for s in d.specs] == ["1", "2"]
    assert d.unreadable == [("3", "Regeln", "gone")]
    assert all(pid != "3" for pid, _ in d.excluded)


def test_search_failure_propagates(client):
    from digest.spec_monitor.http import RequestFailed
    client.search_descendants.side_effect = RequestFailed("u", 3, "HTTP 503")
    with pytest.raises(RequestFailed):
        discover_specs(client, cfg())


def test_only_by_title_case_insensitive_and_by_id(client):
    assert [s.page_id for s in discover_specs(client, cfg(), only="api customer").specs] == ["1"]
    assert [s.page_id for s in discover_specs(client, cfg(), only="2").specs] == ["2"]


def test_only_not_among_specs_raises(client):
    with pytest.raises(ValueError):
        discover_specs(client, cfg(), only="Regeln")
    with pytest.raises(ValueError):
        discover_specs(client, cfg(), only="Nonexistent")


def test_unknown_selector_type_raises(client):
    with pytest.raises(ValueError):
        discover_specs(client, cfg(selector_type="bogus"))


def test_only_accepts_confluence_url(client):
    url = "https://x.atlassian.net/wiki/spaces/S/pages/2/Some+Title"
    assert [s.page_id for s in discover_specs(client, cfg(), only=url).specs] == ["2"]
    legacy = "https://x.atlassian.net/wiki/pages/viewpage.action?pageId=1"
    assert [s.page_id for s in discover_specs(client, cfg(), only=legacy).specs] == ["1"]


def test_only_unresolvable_url_raises_not_among_specs(client):
    with pytest.raises(ValueError, match="not among the discovered specs"):
        discover_specs(client, cfg(), only="https://x.atlassian.net/wiki/x/AbCd")


def test_only_fetches_bodies_of_matching_pages_only(client):
    discover_specs(client, cfg(), only="api customer")
    assert [c.args[0] for c in client.get_page.call_args_list] == ["1"]
    client.get_page.reset_mock()
    discover_specs(client, cfg(), only="2")
    assert [c.args[0] for c in client.get_page.call_args_list] == ["2"]


def test_only_without_candidate_match_fetches_nothing(client):
    with pytest.raises(ValueError, match="not among the discovered specs"):
        discover_specs(client, cfg(), only="Nonexistent")
    client.get_page.assert_not_called()


def test_only_transient_failure_of_requested_page_is_not_hidden_by_others(client):
    from digest.spec_monitor.http import RequestFailed
    orig = client.get_page.side_effect

    def flaky(pid):
        if pid == "2":
            raise RequestFailed("u", 3, "HTTP 503")
        return orig(pid)

    client.get_page.side_effect = flaky
    assert [s.page_id for s in discover_specs(client, cfg(), only="1").specs] == ["1"]


def test_label_selector_does_not_fetch_unlabelled_pages(client):
    discover_specs(client, cfg(selector_type="label", selector_value="detailspec"))
    assert sorted(c.args[0] for c in client.get_page.call_args_list) == ["1", "2"]


def test_bodies_are_fetched_in_parallel_and_keep_order(client):
    import threading
    import time
    barrier = threading.Barrier(2, timeout=5)
    orig = client.get_page.side_effect

    def rendezvous(pid):
        if pid in ("1", "2"):
            barrier.wait()
        if pid == "1":
            time.sleep(0.05)
        return orig(pid)

    client.get_page.side_effect = rendezvous
    d = discover_specs(client, cfg())
    assert [s.page_id for s in d.specs] == ["1", "2"]
    assert d.excluded == [("3", "Regeln"), ("4", "Other macro")]


def test_parallel_fetch_isolates_errors_and_keeps_order(client):
    orig = client.get_page.side_effect

    def flaky(pid):
        if pid in ("1", "3"):
            raise LookupError("gone")
        return orig(pid)

    client.get_page.side_effect = flaky
    d = discover_specs(client, cfg())
    assert [s.page_id for s in d.specs] == ["2"]
    assert d.unreadable == [("1", "API Customer", "gone"), ("3", "Regeln", "gone")]
    assert d.excluded == [("4", "Other macro")]


def test_readable_statuses_are_no_problem(client):
    assert discover_specs(client, cfg()).problems == []


def test_unparseable_properties_report_unknown_status(client, mocker):
    mocker.patch.object(discovery, "_parse_properties", return_value=PageProperties())
    d = discover_specs(client, cfg())
    scopes = {p.scope for p in d.problems}
    assert {"API Customer", "Utility Core"} <= scopes
    assert all("unknown" in p.message for p in d.problems)


def test_zero_specs_is_a_reported_problem(client):
    client.search_descendants.side_effect = lambda parent, label=None: []
    d = discover_specs(client, cfg())
    assert d.specs == []
    assert len(d.problems) == 1 and "no spec found" in d.problems[0].message
