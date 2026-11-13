from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from digest.config import AtlassianConfig, RetryConfig
from digest.spec_monitor import confluence_client as cc
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.confluence_client import ConfluenceClient, pick_version_at
from digest.spec_monitor.confluence_monitor import ConfluenceMonitor
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import (
    Classification, SourceKind, SourceRef, SpecInfo, VersionPoint,
)

from digest.spec_monitor.textdiff import extract_page_links as real_extract_page_links

LOCAL_TZ = datetime.now().astimezone().tzinfo


class FakeResp:
    def __init__(self, status=200, data=None, headers=None, content=b""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._data = data
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._data


def make_client(tmp_path, router):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return router(method, url, kwargs)

    atl = AtlassianConfig(url="https://x.atlassian.net", email="a@b", api_token="t")
    client = ConfluenceClient(atl, "Basic abc", RetryConfig(attempts=1), SpecCache(tmp_path))
    return client, calls, fake


@pytest.fixture
def patch_http(mocker):
    def _patch(fake):
        mocker.patch.object(cc, "request_with_retry", side_effect=fake)
    return _patch


def test_list_versions_paginates_and_sorts(tmp_path, patch_http):
    def router(method, url, kwargs):
        if "cursor=2" in url:
            return FakeResp(data={"results": [{"number": 1, "when": "2026-01-01T10:00:00.000Z"}], "_links": {}})
        return FakeResp(data={
            "results": [{"number": 3, "when": "2026-03-01T10:00:00.000Z"}, {"number": 2, "when": "2026-02-01T10:00:00.000Z"}],
            "_links": {"next": "/rest/api/content/5/version?limit=200&cursor=2"},
        })

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    versions = client.list_versions("5")
    assert [v.version for v in versions] == ["1", "2", "3"]
    assert versions[0].when is not None and versions[0].when.tzinfo is not None
    assert calls[0][1] == "https://x.atlassian.net/wiki/rest/api/content/5/version"
    assert calls[0][2]["params"]["limit"] == 200
    assert calls[1][1] == "https://x.atlassian.net/wiki/rest/api/content/5/version?limit=200&cursor=2"
    assert calls[0][2]["headers"]["Authorization"] == "Basic abc"


def test_get_body_second_call_hits_cache(tmp_path, patch_http):
    def router(method, url, kwargs):
        return FakeResp(data={"body": {"storage": {"value": "<p>hi</p>"}}})

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    assert client.get_body("5", 7) == "<p>hi</p>"
    assert client.get_body("5", 7) == "<p>hi</p>"
    assert len(calls) == 1
    assert calls[0][2]["params"] == {"status": "historical", "version": 7, "expand": "body.storage"}


def test_get_body_falls_back_to_live_for_current_version(tmp_path, patch_http):
    def router(method, url, kwargs):
        if kwargs["params"].get("status") == "historical":
            return FakeResp(404)
        return FakeResp(data={"version": {"number": 7}, "body": {"storage": {"value": "<p>live</p>"}}})

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    assert client.get_body("5", 7) == "<p>live</p>"


def test_get_page_expands_ancestors(tmp_path, patch_http):
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(data={"id": "5"}))
    patch_http(fake)
    client.get_page("5")
    assert calls[0][2]["params"]["expand"] == "version,history,body.storage,ancestors"
    client.get_page("5", with_body=False)
    assert calls[1][2]["params"]["expand"] == "version,history,ancestors"


def test_get_page_404_raises_lookup_error(tmp_path, patch_http):
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(404))
    patch_http(fake)
    with pytest.raises(LookupError):
        client.get_page("5")


def test_get_page_403_raises_request_failed(tmp_path, patch_http):
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(403))
    patch_http(fake)
    with pytest.raises(RequestFailed) as exc:
        client.get_page("5")
    assert "403" in str(exc.value)


def test_search_descendants_paginates_and_builds_cql(tmp_path, patch_http):
    def router(method, url, kwargs):
        if "start=1" in url:
            return FakeResp(data={"results": [{"id": "2", "title": "B", "version": {"number": 4}}], "_links": {}})
        return FakeResp(data={
            "results": [{"id": "1", "title": "A", "version": {"number": 3}}],
            "_links": {"next": "/rest/api/content/search?cql=x&start=1"},
        })

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    out = client.search_descendants("668", label="spec")
    assert out == [{"id": "1", "title": "A", "version": 3, "when": None}, {"id": "2", "title": "B", "version": 4, "when": None}]
    cql = calls[0][2]["params"]["cql"]
    assert "ancestor=668" in cql and "type=page" in cql and 'label = "spec"' in cql
    assert calls[1][1] == "https://x.atlassian.net/wiki/rest/api/content/search?cql=x&start=1"


def test_find_page_id_uses_title_and_space(tmp_path, patch_http):
    def router(method, url, kwargs):
        return FakeResp(data={"results": [{"id": "42", "title": "Utility"}]})

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    assert client.find_page_id('Util"ity', "RMEG") == "42"
    cql = calls[0][2]["params"]["cql"]
    assert 'title = "Util\\"ity"' in cql and 'space = "RMEG"' in cql


def test_find_page_id_none_when_missing(tmp_path, patch_http):
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(data={"results": []}))
    patch_http(fake)
    assert client.find_page_id("Nope") is None


def test_download_attachment_follows_redirect_without_auth(tmp_path, patch_http):
    def router(method, url, kwargs):
        if url.endswith("/child/attachment"):
            return FakeResp(data={"results": [{"id": "att9"}]})
        if "/att9/download" in url:
            return FakeResp(302, headers={"Location": "https://media.example/file?token=z"})
        return FakeResp(200, content=b"<mxfile/>")

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    assert client.download_attachment("5", "diag", 3) == b"<mxfile/>"
    assert calls[0][2]["params"] == {"filename": "diag"}
    dl = calls[1]
    assert dl[1].endswith("/content/5/child/attachment/att9/download")
    assert dl[2]["params"] == {"version": 3}
    assert dl[2]["allow_redirects"] is False
    assert dl[2]["headers"]["Authorization"] == "Basic abc"
    last = calls[2]
    assert last[1] == "https://media.example/file?token=z"
    assert "Authorization" not in (last[2].get("headers") or {})


def test_download_attachment_unknown_file_returns_none(tmp_path, patch_http):
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(data={"results": []}))
    patch_http(fake)
    assert client.download_attachment("5", "nope", 1) is None


def _vp(n, y, m, d, h=12):
    return VersionPoint(version=str(n), when=datetime(y, m, d, h, 0, tzinfo=LOCAL_TZ))


def test_pick_version_at_boundaries():
    vs = [_vp(1, 2026, 1, 10), _vp(2, 2026, 2, 10, 23), _vp(3, 2026, 3, 10)]
    assert pick_version_at(vs, date(2026, 1, 9)) is None
    assert pick_version_at(vs, date(2026, 1, 10)).version == "1"
    assert pick_version_at(vs, date(2026, 2, 10)).version == "2"
    assert pick_version_at(vs, date(2026, 2, 11)).version == "2"
    assert pick_version_at(vs, date(2027, 1, 1)).version == "3"


# ---- monitor ----

PAGE_NEW = "<p>new</p>"


def make_monitor(versions=None, new_ver=5, bodies=None, page_id="100", attachments=None, find_id="100"):
    client = MagicMock(spec=ConfluenceClient)
    client.page_url.side_effect = lambda pid, version=None: f"https://x/p/{pid}" + (f"?v={version}" if version else "")
    client.find_page_id.return_value = find_id
    client.get_page.return_value = {
        "id": page_id,
        "version": {"number": new_ver, "when": "2026-11-01T10:00:00.000Z"},
    }
    client.list_versions.return_value = versions if versions is not None else [
        _vp(n, 2026, 10, n) for n in range(1, new_ver + 1)
    ]
    bodies = bodies or {}
    client.get_body.side_effect = lambda pid, v: bodies.get(v, f"<p>body{v}</p>")
    attachments = attachments or {}
    client.download_attachment.side_effect = lambda pid, name, v: attachments.get((name, v))
    return ConfluenceMonitor(client), client


def spec(stand=None, created=None, body=""):
    return SpecInfo(page_id="1", title="Spec", url="https://x/s", status="Refined",
                    stand_date=stand, created=created, body_storage=body)


def ref(**kw):
    base = dict(name="Src", kind=SourceKind.CONFLUENCE, title="Src", page_id="100")
    base.update(kw)
    return SourceRef(**base)


@pytest.fixture
def text_mocks(mocker):
    m = {
        "to_text": mocker.patch("digest.spec_monitor.textdiff.storage_to_text", side_effect=lambda s: s),
        "diff": mocker.patch("digest.spec_monitor.textdiff.diff_text", side_effect=lambda a, b: "" if a == b else f"- {a}\n+ {b}"),
        "cosmetic": mocker.patch("digest.spec_monitor.textdiff.is_cosmetic", return_value=False),
        "added": mocker.patch("digest.spec_monitor.textdiff.added_links", return_value=[]),
        "links": mocker.patch("digest.spec_monitor.textdiff.extract_page_links", return_value=[]),
        "revs": mocker.patch("digest.spec_monitor.textdiff.drawio_revisions", return_value={}),
        "diagram": mocker.patch("digest.spec_monitor.drawio.diff_diagrams"),
    }
    return m


def test_handles_only_confluence():
    mon, _ = make_monitor()
    assert mon.handles(ref())
    assert not mon.handles(ref(kind=SourceKind.ECH))


def test_stated_version_changed(text_mocks):
    mon, client = make_monitor(new_ver=5, bodies={3: "OLD", 5: "NEW"})
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.CHANGED
    assert res.old.version == "3" and not res.old.inferred
    assert res.new.version == "5"
    assert res.old_text == "OLD" and res.new_text == "NEW"
    assert "- OLD" in res.diff_text


def test_same_version_unchanged_no_body_fetch(text_mocks):
    mon, client = make_monitor(new_ver=5)
    res = mon.resolve(ref(baseline_version="5"), spec())
    assert res.classification == Classification.UNCHANGED
    assert res.old.version == res.new.version == "5"
    client.get_body.assert_not_called()


def test_cosmetic_only(text_mocks):
    text_mocks["cosmetic"].return_value = True
    mon, client = make_monitor(new_ver=5, bodies={3: "a", 5: "a."})
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.COSMETIC_ONLY


def test_baseline_date_inferred(text_mocks):
    mon, client = make_monitor(new_ver=5, versions=[_vp(n, 2026, 10, n * 2) for n in range(1, 6)])
    res = mon.resolve(ref(baseline_date=date(2026, 10, 5)), spec(stand=date(2026, 1, 1)))
    assert res.old.version == "2"
    assert res.old.inferred is True
    assert res.old.inferred_reason == "from date 2026-10-05"
    assert res.classification == Classification.CHANGED


def test_fallback_to_stand_date_then_created(text_mocks):
    versions = [_vp(n, 2026, 10, n * 2) for n in range(1, 6)]
    mon, _ = make_monitor(new_ver=5, versions=versions)
    res = mon.resolve(ref(), spec(stand=date(2026, 10, 7), created=datetime(2026, 1, 1)))
    assert res.old.version == "3"
    assert "Stand" in res.old.inferred_reason and "2026-10-07" in res.old.inferred_reason
    res = mon.resolve(ref(), spec(created=datetime(2026, 10, 9, 8, 0)))
    assert res.old.version == "4"
    assert "creat" in res.old.inferred_reason and "2026-10-09" in res.old.inferred_reason


def test_no_baseline_at_all_is_error(text_mocks):
    mon, _ = make_monitor()
    res = mon.resolve(ref(), spec())
    assert res.classification == Classification.ERROR
    assert res.errors


def test_stated_version_missing_in_history(text_mocks):
    mon, _ = make_monitor(new_ver=5, versions=[_vp(n, 2026, 10, n) for n in (1, 2, 5)])
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.ERROR
    assert any("3" in e and "history" in e for e in res.errors)


def test_page_not_found_by_title_is_error(text_mocks):
    mon, client = make_monitor(find_id=None)
    res = mon.resolve(ref(page_id=None, title="Ghost"), spec())
    assert res.classification == Classification.ERROR
    assert any("Ghost" in e for e in res.errors)


def test_lookup_error_is_error_row(text_mocks):
    mon, client = make_monitor()
    client.get_page.side_effect = LookupError("gone")
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert res.classification == Classification.ERROR
    assert any("gone" in e for e in res.errors)


def test_request_failed_keeps_attempts(text_mocks):
    mon, client = make_monitor()
    client.get_page.side_effect = RequestFailed("u", 3, "HTTP 503")
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert res.classification == Classification.ERROR
    assert res.attempts == 3
    assert any("503" in e for e in res.errors)


def test_unexpected_exception_never_raises(text_mocks):
    mon, client = make_monitor()
    client.get_page.side_effect = RuntimeError("boom")
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert res.classification == Classification.ERROR
    assert any("boom" in e for e in res.errors)


def test_page_id_resolved_by_title(text_mocks):
    mon, client = make_monitor(find_id="777")
    mon.resolve(ref(page_id=None, title="T", space_key="RMEG", baseline_version="5"), spec())
    client.find_page_id.assert_called_once_with("T", "RMEG")
    client.get_page.assert_called_once()
    assert client.get_page.call_args[0][0] == "777"


def test_added_and_other_links(text_mocks):
    text_mocks["added"].return_value = ["In Table", "Fresh Page"]
    text_mocks["links"].side_effect = lambda html: {
        "SPECBODY": ["In Table"],
        "<p>body5</p>": ["In Table", "Fresh Page", "Old Extra"],
    }.get(html, [])
    mon, client = make_monitor(new_ver=5)
    res = mon.resolve(ref(baseline_version="3"), spec(body="SPECBODY"))
    assert res.added_links == ["Fresh Page"]
    assert res.other_links == ["Old Extra"]


def link(title):
    return f'<ac:link><ri:page ri:content-title="{title}" /></ac:link>'


QUELLEN_BODY = (
    f"<p>Intro {link('Outside Table')}</p>"
    "<h2>Quellen</h2>"
    f"<table><tr><td>{link('In Table')}</td></tr></table>"
    "<h2>Weiteres</h2>"
    f"<table><tr><td>{link('Other Table')}</td></tr></table>"
)


def spec_with_ancestors(body="", titles=("Detailspezifikationen",)):
    s = spec(body=body)
    s.ancestor_ids = ["900"]
    s.ancestor_titles = list(titles)
    return s


def test_ancestor_links_never_reported(text_mocks):
    text_mocks["added"].return_value = ["detailspezifikationen", "Fresh Page"]
    text_mocks["links"].side_effect = lambda html: (
        ["Detailspezifikationen", "Fresh Page", "Old Extra", "Parent Root"] if html == "<p>body5</p>" else []
    )
    mon, _ = make_monitor(new_ver=5)
    res = mon.resolve(ref(baseline_version="3"), spec_with_ancestors(titles=("Detailspezifikationen", "parent root")))
    assert res.added_links == ["Fresh Page"]
    assert res.other_links == ["Old Extra"]


def test_known_set_limited_to_quellen_table(mocker, text_mocks):
    text_mocks["links"].side_effect = real_extract_page_links
    text_mocks["added"].return_value = ["In Table", "Outside Table", "Other Table"]
    mon, _ = make_monitor(new_ver=5, bodies={3: "old", 5: link("In Table") + link("Outside Table") + link("Other Table")})
    res = mon.resolve(ref(baseline_version="3"), spec(body=QUELLEN_BODY))
    assert res.added_links == ["Outside Table", "Other Table"]
    assert res.other_links == []


def test_known_set_falls_back_to_whole_body_without_quellen_table(mocker, text_mocks):
    text_mocks["links"].side_effect = real_extract_page_links
    body = f"<p>{link('In Body')}</p><h2>Quellen</h2><p>kein Tabelle</p>"
    text_mocks["added"].return_value = ["In Body", "Fresh"]
    mon, _ = make_monitor(new_ver=5, bodies={3: "old", 5: link("In Body") + link("Fresh")})
    res = mon.resolve(ref(baseline_version="3"), spec(body=body))
    assert res.added_links == ["Fresh"]


def test_quellen_heading_match_is_case_insensitive_and_table_must_follow_it(mocker, text_mocks):
    text_mocks["links"].side_effect = real_extract_page_links
    body = (f"<table><tr><td>{link('Before')}</td></tr></table>"
            "<h3><strong>Verwendete QUELLEN</strong></h3><p>text</p>"
            f"<table><tr><td>{link('After')}</td></tr></table>")
    text_mocks["added"].return_value = ["Before", "After"]
    mon, _ = make_monitor(new_ver=5, bodies={3: "old", 5: "new"})
    res = mon.resolve(ref(baseline_version="3"), spec(body=body))
    assert res.added_links == ["Before"]


def test_drawio_revision_diff(text_mocks):
    text_mocks["revs"].side_effect = lambda html: {"<p>body3</p>": {"arch": 1}, "<p>body5</p>": {"arch": 2}}[html]
    diag = MagicMock(changes=["added node 'Queue'"], layout_only=False, warnings=["more than one page"])
    text_mocks["diagram"].return_value = diag
    mon, client = make_monitor(new_ver=5, attachments={("arch", 1): b"<old/>", ("arch", 2): b"<new/>"})
    res = mon.resolve(ref(baseline_version="3"), spec())
    text_mocks["diagram"].assert_called_once_with("<old/>", "<new/>")
    assert any("arch" in n and "added node 'Queue'" in n for n in res.diagram_notes)
    assert "more than one page" in res.warnings


def test_drawio_only_change_is_changed_even_if_text_cosmetic(text_mocks):
    text_mocks["cosmetic"].return_value = True
    text_mocks["revs"].side_effect = lambda html: {"<p>body3</p>": {"arch": 1}, "<p>body5</p>": {"arch": 2}}[html]
    text_mocks["diagram"].return_value = MagicMock(changes=["removed node 'A'"], layout_only=False, warnings=[])
    mon, _ = make_monitor(new_ver=5, attachments={("arch", 1): b"o", ("arch", 2): b"n"})
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.CHANGED


def test_drawio_layout_only_stays_cosmetic(text_mocks):
    text_mocks["cosmetic"].return_value = True
    text_mocks["revs"].side_effect = lambda html: {"<p>body3</p>": {"arch": 1}, "<p>body5</p>": {"arch": 2}}[html]
    text_mocks["diagram"].return_value = MagicMock(changes=[], layout_only=True, warnings=[])
    mon, _ = make_monitor(new_ver=5, attachments={("arch", 1): b"o", ("arch", 2): b"n"})
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.COSMETIC_ONLY
    assert res.diagram_notes == []


def test_drawio_undiffable_notes_not_compared(text_mocks):
    text_mocks["revs"].side_effect = lambda html: {"<p>body3</p>": {"arch": 1}, "<p>body5</p>": {"arch": 2}}[html]
    text_mocks["diagram"].side_effect = ValueError("garbage")
    mon, _ = make_monitor(new_ver=5, attachments={("arch", 1): b"o", ("arch", 2): b"n"})
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.CHANGED
    assert any("diagram changed, not compared" in n and "arch" in n for n in res.diagram_notes)


def test_drawio_attachment_missing_or_failing_notes_not_compared(text_mocks):
    text_mocks["revs"].side_effect = lambda html: {"<p>body3</p>": {"arch": 1}, "<p>body5</p>": {"arch": 2}}[html]
    mon, client = make_monitor(new_ver=5, attachments={})
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert any("diagram changed, not compared" in n for n in res.diagram_notes)
    client.download_attachment.side_effect = RequestFailed("u", 2, "timeout")
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.CHANGED
    assert any("diagram changed, not compared" in n for n in res.diagram_notes)


def test_new_and_removed_diagrams_are_noted(text_mocks):
    text_mocks["revs"].side_effect = lambda html: {"<p>body3</p>": {"gone": 1}, "<p>body5</p>": {"fresh": 1}}[html]
    mon, _ = make_monitor(new_ver=5)
    res = mon.resolve(ref(baseline_version="3"), spec())
    joined = " ".join(res.diagram_notes)
    assert "fresh" in joined and "gone" in joined
    assert res.classification == Classification.CHANGED


# ---- per-version stepping ----

def test_gap_of_five_versions_gives_five_steps(text_mocks):
    mon, client = make_monitor(new_ver=6)
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert res.classification == Classification.CHANGED
    assert [s.version for s in res.steps] == ["2", "3", "4", "5", "6"]
    assert res.steps[0].when == datetime(2026, 10, 2, 12, 0, tzinfo=LOCAL_TZ)
    assert res.steps[4].when == datetime(2026, 10, 6, 12, 0, tzinfo=LOCAL_TZ)
    assert res.steps[0].diff_text == "- <p>body1</p>\n+ <p>body2</p>"
    assert res.steps[3].diff_text == "- <p>body4</p>\n+ <p>body5</p>"
    assert res.diff_text == "- <p>body1</p>\n+ <p>body6</p>"
    assert res.warnings == []


def test_cosmetic_step_is_skipped(text_mocks):
    text_mocks["cosmetic"].side_effect = lambda d: d == "- <p>body3</p>\n+ <p>body4</p>"
    mon, _ = make_monitor(new_ver=6)
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert [s.version for s in res.steps] == ["2", "3", "5", "6"]
    assert res.steps[2].diff_text == "- <p>body4</p>\n+ <p>body5</p>"


def test_step_cap_boundary_allows_twelve_versions(text_mocks):
    mon, _ = make_monitor(new_ver=13)
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert len(res.steps) == 12
    assert res.warnings == []


def test_cap_exceeded_keeps_net_diff_and_warns(text_mocks):
    mon, client = make_monitor(new_ver=14)
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert res.classification == Classification.CHANGED
    assert res.steps == []
    assert "history of 13 versions not itemised (cap)" in res.warnings
    assert "- <p>body1</p>" in res.diff_text
    assert [c.args[1] for c in client.get_body.call_args_list] == [1, 14]


@pytest.mark.parametrize("exc", [LookupError("gone"), RequestFailed("u", 3, "timeout")])
def test_unreadable_intermediate_warns_without_steps(text_mocks, exc):
    mon, client = make_monitor(new_ver=6)
    base = client.get_body.side_effect

    def body(pid, v):
        if v == 4:
            raise exc
        return base(pid, v)

    client.get_body.side_effect = body
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert res.classification == Classification.CHANGED
    assert res.steps == []
    assert "history of 5 versions not itemised (version 4 unreadable)" in res.warnings
    assert "- <p>body1</p>" in res.diff_text


def test_single_version_gap_has_no_steps(text_mocks):
    mon, _ = make_monitor(new_ver=5)
    res = mon.resolve(ref(baseline_version="4"), spec())
    assert res.classification == Classification.CHANGED
    assert res.steps == [] and res.warnings == []


def test_cosmetic_net_result_has_no_steps(text_mocks):
    text_mocks["cosmetic"].return_value = True
    mon, _ = make_monitor(new_ver=6)
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert res.classification == Classification.COSMETIC_ONLY
    assert res.steps == []


def test_missing_version_dates_do_not_break_steps(text_mocks):
    mon, client = make_monitor(new_ver=3, versions=[VersionPoint(version=str(n)) for n in (1, 2, 3)])
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert [s.version for s in res.steps] == ["2", "3"]
    assert res.steps[0].when is None


# ---- pagination guard ----

def _ids_page(ids, nxt=None):
    data = {"results": [{"id": str(i), "title": "t", "version": {"number": 1}} for i in ids], "_links": {}}
    if nxt:
        data["_links"]["next"] = nxt
    return FakeResp(data=data)


def test_paginate_normal_multi_page_run(tmp_path, patch_http):
    def router(method, url, kwargs):
        if "cursor=c2" in url:
            return _ids_page([3, 4], "/rest/api/content/search?cursor=c3")
        if "cursor=c3" in url:
            return _ids_page([5])
        return _ids_page([1, 2], "/rest/api/content/search?cursor=c2")

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    assert [r["id"] for r in client.search_descendants("1")] == ["1", "2", "3", "4", "5"]
    assert len(calls) == 3


def test_paginate_aborts_on_repeating_cursor(tmp_path, patch_http):
    counter = {"n": 0}

    def router(method, url, kwargs):
        counter["n"] += 1
        if "cursor=loop" in url:
            return _ids_page([100 + counter["n"]], "/rest/api/content/search?cursor=loop")
        return _ids_page([1], "/rest/api/content/search?cursor=loop")

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    with pytest.raises(RequestFailed, match="pagination did not advance"):
        client.search_descendants("1")
    assert len(calls) == 2


def test_paginate_aborts_when_page_has_no_new_ids(tmp_path, patch_http):
    def router(method, url, kwargs):
        if "cursor=c2" in url:
            return _ids_page([1, 2], "/rest/api/content/search?cursor=c3")
        return _ids_page([1, 2], "/rest/api/content/search?cursor=c2")

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    with pytest.raises(RequestFailed, match="pagination did not advance"):
        client.search_descendants("1")
    assert len(calls) == 2


def test_paginate_aborts_on_empty_page_with_next_link(tmp_path, patch_http):
    def router(method, url, kwargs):
        if "cursor=c2" in url:
            return _ids_page([], "/rest/api/content/search?cursor=c3")
        return _ids_page([1], "/rest/api/content/search?cursor=c2")

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    with pytest.raises(RequestFailed, match="pagination did not advance"):
        client.search_descendants("1")


def test_paginate_empty_result_without_next_is_fine(tmp_path, patch_http):
    client, calls, fake = make_client(tmp_path, lambda m, u, k: _ids_page([]))
    patch_http(fake)
    assert client.search_descendants("1") == []


def test_paginate_safety_cap(tmp_path, patch_http):
    def router(method, url, kwargs):
        n = len(calls) + 1
        return _ids_page([n], f"/rest/api/content/search?cursor=c{n + 1}")

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    with pytest.raises(RequestFailed, match="pagination did not advance"):
        client.search_descendants("1")
    assert len(calls) == cc._MAX_PAGES


def test_list_versions_without_id_field_uses_number(tmp_path, patch_http):
    def router(method, url, kwargs):
        if "cursor=2" in url:
            return FakeResp(data={"results": [{"number": 1}], "_links": {}})
        return FakeResp(data={"results": [{"number": 2}], "_links": {"next": "/rest/api/content/5/version?cursor=2"}})

    client, calls, fake = make_client(tmp_path, router)
    patch_http(fake)
    assert [v.version for v in client.list_versions("5")] == ["1", "2"]


def test_get_body_without_storage_raises_no_storage_body(tmp_path, patch_http):
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(data={"body": {}}))
    patch_http(fake)
    with pytest.raises(cc.NoStorageBody):
        client.get_body("5", 7)


# ---- bodyless content ----

def test_database_type_with_changed_versions_is_changed_with_warning(text_mocks):
    mon, client = make_monitor(new_ver=5)
    client.get_page.return_value["type"] = "database"
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.CHANGED
    assert res.warnings == ["content type database has no storage body; versions 3 -> 5 not compared"]
    assert not res.diff_text
    client.get_body.assert_not_called()


def test_database_type_with_equal_versions_is_unchanged(text_mocks):
    mon, client = make_monitor(new_ver=5)
    client.get_page.return_value["type"] = "database"
    res = mon.resolve(ref(baseline_version="5"), spec())
    assert res.classification == Classification.UNCHANGED
    assert res.warnings == []


def test_missing_storage_body_is_changed_not_cosmetic(text_mocks):
    text_mocks["cosmetic"].return_value = True
    mon, client = make_monitor(new_ver=5)
    client.get_body.side_effect = cc.NoStorageBody("no body")
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.CHANGED
    assert res.warnings == ["content type page has no storage body; versions 3 -> 5 not compared"]


def test_missing_storage_body_on_old_version_only(text_mocks):
    mon, client = make_monitor(new_ver=5)
    client.get_body.side_effect = lambda pid, v: (_ for _ in ()).throw(cc.NoStorageBody("x")) if v == 3 else "<p>n</p>"
    res = mon.resolve(ref(baseline_version="3"), spec())
    assert res.classification == Classification.CHANGED
    assert "versions 3 -> 5 not compared" in res.warnings[0]


def test_missing_storage_body_equal_versions_unchanged(text_mocks):
    mon, client = make_monitor(new_ver=5)
    client.get_body.side_effect = cc.NoStorageBody("no body")
    res = mon.resolve(ref(baseline_version="5"), spec())
    assert res.classification == Classification.UNCHANGED



def _http_timeouts(mocker, tmp_path, router, timeout_seconds=77):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return router(method, url, kwargs)

    mocker.patch("digest.spec_monitor.http.requests.request", side_effect=fake)
    atl = AtlassianConfig(url="https://x.atlassian.net", email="a@b", api_token="t")
    retry = RetryConfig(attempts=1, backoff_seconds=0, timeout_seconds=timeout_seconds)
    return ConfluenceClient(atl, "Basic abc", retry, SpecCache(tmp_path)), calls


def test_api_calls_use_retry_timeout(tmp_path, mocker):
    client, calls = _http_timeouts(mocker, tmp_path, lambda m, u, k: FakeResp(data={"results": [], "_links": {}}))
    client.list_versions("5")
    client.find_page_id("T")
    assert calls and all(c[2]["timeout"] == 77 for c in calls)


def test_downloads_use_double_retry_timeout(tmp_path, mocker):
    def router(method, url, kwargs):
        if url.endswith("/child/attachment"):
            return FakeResp(data={"results": [{"id": "att9"}]})
        if "/att9/download" in url:
            return FakeResp(302, headers={"Location": "https://media.example/file?token=z"})
        return FakeResp(200, content=b"<mxfile/>")

    client, calls = _http_timeouts(mocker, tmp_path, router)
    assert client.download_attachment("5", "diag", 3) == b"<mxfile/>"
    assert [c[2]["timeout"] for c in calls] == [77, 154, 154]


def test_find_page_id_ambiguous_title_without_space_raises(tmp_path, patch_http):
    results = [
        {"id": "1", "title": "Batch Processing", "space": {"key": "AAA"}},
        {"id": "2", "title": "Batch Processing", "space": {"key": "BBB"}},
    ]
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(data={"results": results}))
    patch_http(fake)
    with pytest.raises(LookupError, match="ambiguous") as exc:
        client.find_page_id("Batch Processing")
    assert "AAA" in str(exc.value) and "BBB" in str(exc.value)


def test_find_page_id_prefers_exact_title_without_space(tmp_path, patch_http):
    results = [
        {"id": "1", "title": "Batch Processing Old"},
        {"id": "2", "title": "batch processing"},
    ]
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(data={"results": results}))
    patch_http(fake)
    assert client.find_page_id("Batch Processing") == "2"


def test_find_page_id_with_space_takes_first_match(tmp_path, patch_http):
    results = [{"id": "7", "title": "Batch Processing"}, {"id": "8", "title": "Batch Processing"}]
    client, calls, fake = make_client(tmp_path, lambda m, u, k: FakeResp(data={"results": results}))
    patch_http(fake)
    assert client.find_page_id("Batch Processing", "AAA") == "7"


def test_list_versions_failure_in_step_stage_warns(text_mocks):
    mon, client = make_monitor(new_ver=6)
    versions = client.list_versions.return_value
    client.list_versions.side_effect = [versions, RequestFailed("u", 3, "timeout")]
    res = mon.resolve(ref(baseline_version="1"), spec())
    assert [s.version for s in res.steps] == ["2", "3", "4", "5", "6"]
    assert res.steps[0].when is None
    assert any("version dates of the history are unavailable" in w for w in res.warnings)
