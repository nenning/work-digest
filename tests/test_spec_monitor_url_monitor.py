import json
from datetime import date

import pytest
import requests

from digest.config import RetryConfig
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.models import Classification, SourceKind, SourceRef, SpecInfo
from digest.spec_monitor.url_monitor import UrlMonitor, normalise_version, versions_equal

TODAY = date(2026, 11, 10)

ZEFIX_UI = "https://www.zefix.admin.ch/ZefixPublicREST/swagger-ui/index.html"
ZEFIX_API = "https://www.zefix.admin.ch/ZefixPublicREST/v3/api-docs"
POST_API = "https://developer.apis.post.ch/api/apis/42"
POST_PAGE = "https://developer.apis.post.ch/api/apis/42/pages/7/content"
BURWEB = "https://statistik.admin.ch/de/asset/30325086"
NOVERSION = "https://statistik.admin.ch/de/asset/11007268"


class FakeResp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}
        self.content = text.encode("utf-8") if isinstance(text, str) else text
        self.text = text if isinstance(text, str) else ""


def _openapi(version="2.7.2.4", title="Zefix REST", paths=("/v1/company",)):
    return json.dumps({
        "openapi": "3.0.1",
        "info": {"title": title, "version": version},
        "paths": {p: {"get": {}} for p in paths},
    })


def _html(title, headings=(), links=(), noise=""):
    body = "".join(f"<h2>{h}</h2>" for h in headings)
    body += "".join(f'<p><a href="{href}">{text}</a></p>' for text, href in links)
    return (
        f"<html><head><title>{title}</title><script>var x='<a href=\"/asset/9\">no</a>';</script></head>"
        f'<body><nav><a href="/de/home">Home</a><a href="/de/kontakt">Kontakt</a> {noise}</nav>'
        f"<main><h1>{title}</h1>{body}</main></body></html>"
    )


@pytest.fixture
def web(mocker):
    class Web:
        routes = {}
        calls = []

    w = Web()
    w.routes = {}
    w.calls = []

    def fake(method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        w.calls.append((url, headers))
        assert kwargs.get("timeout")
        assert headers.get("User-Agent") == "Mozilla/5.0 (compatible; spec-monitor)"
        if url not in w.routes:
            return FakeResp(404, "nope")
        value = w.routes[url]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(headers)
        if isinstance(value, tuple):
            return FakeResp(*value)
        return FakeResp(200, value)

    mocker.patch("digest.spec_monitor.http.requests.request", side_effect=fake)
    return w


@pytest.fixture
def monitor(tmp_path):
    return UrlMonitor(
        RetryConfig(attempts=2, backoff_seconds=0), SpecCache(tmp_path / "spec_cache"),
        baseline_dir=tmp_path / "url_baselines", today=TODAY,
    )


def _ref(url, **kw):
    kw.setdefault("name", "Ext")
    kw.setdefault("kind", SourceKind.URL)
    return SourceRef(url=url, **kw)


def _spec():
    return SpecInfo(page_id="1", title="Spec", url="https://x/1", status="Refined")


def test_handles_only_url(monitor):
    assert monitor.handles(_ref(ZEFIX_UI))
    assert not monitor.handles(SourceRef(name="x", kind=SourceKind.ECH))


@pytest.mark.parametrize("raw,expected", [
    ("V1X8", "1.8"), ("v1.4.0", "1.4.0"), ("1.8", "1.8"), ("Version 2.7.2.4 (Stand 2026)", "2.7.2.4"),
    ("86", "86"), ("abc123", None), ("", None), (None, None),
])
def test_normalise_version(raw, expected):
    assert normalise_version(raw) == expected


def test_versions_equal_ignores_trailing_zeros():
    assert versions_equal("1.8", "1.8.0")
    assert versions_equal("V1X8", "1.8.0")
    assert not versions_equal("1.8", "1.8.1")


def test_swagger_ui_probes_api_docs_and_matches_baseline(monitor, web):
    web.routes[ZEFIX_API] = _openapi("2.7.2.4")
    res = monitor.resolve(_ref(ZEFIX_UI, baseline_version="2.7.2.4"), _spec())
    assert res.classification == Classification.UNCHANGED
    assert res.new.version == "2.7.2.4"
    assert [c[0] for c in web.calls] == [ZEFIX_API]


def test_swagger_version_differs_is_changed_with_warning(monitor, web):
    web.routes[ZEFIX_API] = _openapi("2.7.2.4")
    res = monitor.resolve(_ref(ZEFIX_UI, baseline_version="2.6.0"), _spec())
    assert res.classification == Classification.CHANGED
    assert res.old.version == "2.6.0"
    assert res.new.version == "2.7.2.4"
    assert res.new.url == ZEFIX_UI
    assert "content not compared (no history available)" in res.warnings


def test_swagger_probe_404_falls_back_to_page(monitor, web):
    web.routes[ZEFIX_UI] = _html("Swagger UI 5.1.0")
    res = monitor.resolve(_ref(ZEFIX_UI, baseline_version="5.1"), _spec())
    assert res.classification == Classification.UNCHANGED
    assert [c[0] for c in web.calls] == [ZEFIX_API, ZEFIX_UI]


def test_direct_openapi_json_body(monitor, web):
    url = "https://api.example.ch/openapi.json"
    web.routes[url] = (200, _openapi("1.2.0"), {"Content-Type": "application/json"})
    res = monitor.resolve(_ref(url, baseline_version="v1.2"), _spec())
    assert res.classification == Classification.UNCHANGED


def test_portal_api_version_from_json(monitor, web):
    web.routes[POST_API] = json.dumps({"version": "1.4.0", "name": "Adress API", "updated_at": 1700000000})
    res = monitor.resolve(_ref(POST_API, baseline_version="v1.4.0"), _spec())
    assert res.classification == Classification.UNCHANGED
    assert web.calls[0][1].get("Accept") == "application/json"


def test_portal_api_version_differs(monitor, web):
    web.routes[POST_API] = json.dumps({"version": "1.4.0", "name": "Adress API"})
    res = monitor.resolve(_ref(POST_API, baseline_version="1.3.2"), _spec())
    assert res.classification == Classification.CHANGED
    assert res.new.version == "1.4.0"


def test_portal_pages_url_uses_json_for_version_and_text_plain_for_content(monitor, web):
    web.routes[POST_API] = json.dumps({"version": "1.4.0", "name": "Adress API"})

    def page(headers):
        if headers.get("Accept") != "text/plain":
            return FakeResp(406, "not acceptable")
        return FakeResp(200, web.doc, {"Content-Type": "text/plain"})

    web.routes[POST_PAGE] = page
    web.doc = "Intro\nEndpoint /a returns 200"
    first = monitor.resolve(_ref(POST_PAGE), _spec())
    assert first.classification == Classification.NOT_MONITORED
    assert first.new.version == "1.4.0"
    web.doc = "Intro\nEndpoint /a returns 201"
    second = monitor.resolve(_ref(POST_PAGE), _spec())
    assert second.classification == Classification.CHANGED
    assert "Endpoint /a returns 201" in second.new_text
    assert "- Endpoint /a returns 200" in second.diff_text
    assert "+ Endpoint /a returns 201" in second.diff_text


def test_portal_invalid_json_is_error(monitor, web):
    web.routes[POST_API] = "<<not json"
    res = monitor.resolve(_ref(POST_API), _spec())
    assert res.classification == Classification.ERROR
    assert "JSON" in res.errors[0]


def test_generic_json_with_top_level_version(monitor, web):
    url = "https://x.example.ch/meta.json"
    web.routes[url] = (200, json.dumps({"version": "3.0", "k": 1}), {"Content-Type": "application/json"})
    res = monitor.resolve(_ref(url, baseline_version="3"), _spec())
    assert res.classification == Classification.UNCHANGED


def test_html_title_version_matches_normalised_baseline(monitor, web):
    web.routes[BURWEB] = _html("BurWeb - XML Schnittstelle zu BurWeb 1.8.0 | Publikation | Portal Statistik Schweiz")
    res = monitor.resolve(_ref(BURWEB, baseline_version="V1X8"), _spec())
    assert res.classification == Classification.UNCHANGED
    assert res.new.version == "1.8.0"


def test_html_title_version_differs(monitor, web):
    web.routes[BURWEB] = _html("BurWeb - XML Schnittstelle zu BurWeb 1.9.0 | Publikation | Portal Statistik Schweiz")
    res = monitor.resolve(_ref(BURWEB, baseline_version="1.8"), _spec())
    assert res.classification == Classification.CHANGED
    assert res.old.version == "1.8"
    assert res.new.version == "1.9.0"
    assert res.new.url == BURWEB
    assert "content not compared (no history available)" in res.warnings


def test_html_snapshot_flow_ignores_navigation_noise(monitor, web):
    links = [("XSD Schema", "/files/burweb.xsd"), ("Handbuch", "/de/asset/123")]
    web.routes[NOVERSION] = _html("Spezifikationen des Webservice | Publikation", ["Dokumente"], links, noise="Render 10:00")
    first = monitor.resolve(_ref(NOVERSION), _spec())
    assert first.classification == Classification.NOT_MONITORED
    assert any("baseline established today" in w and "future runs compare against this snapshot" in w for w in first.warnings)
    assert first.new is not None

    web.routes[NOVERSION] = _html("Spezifikationen des Webservice | Publikation", ["Dokumente"], links, noise="Render 11:30")
    second = monitor.resolve(_ref(NOVERSION), _spec())
    assert second.classification == Classification.UNCHANGED


def test_html_text_contains_title_headings_and_file_links_only(monitor, web):
    web.routes[NOVERSION] = _html(
        "Spec Doc", ["Dokumente"],
        [("XSD Schema", "/files/burweb.xsd"), ("Kontaktformular", "/de/kontakt-form")],
    )
    monitor.resolve(_ref(NOVERSION), _spec())
    web.routes[NOVERSION] = _html(
        "Spec Doc", ["Dokumente"],
        [("XSD Schema", "/files/burweb.xsd"), ("Kontaktformular", "/de/kontakt-form"), ("Neu", "/files/new.pdf")],
    )
    res = monitor.resolve(_ref(NOVERSION), _spec())
    assert res.classification == Classification.CHANGED
    assert "Spec Doc" in res.new_text and "Dokumente" in res.new_text
    assert "https://statistik.admin.ch/files/burweb.xsd" in res.new_text
    assert "Kontaktformular" not in res.new_text
    assert "Home" not in res.new_text
    assert "+ " in res.diff_text and "new.pdf" in res.diff_text
    assert "no" not in res.new_text.split()  # script content ignored
    assert res.old.inferred is True
    assert "snapshot of 2026-11-10 (local baseline)" == res.old.inferred_reason


def test_snapshot_cosmetic_only_change(monitor, web):
    web.routes[NOVERSION] = _html("Spec Doc", ["Dokumente und Schemas"])
    monitor.resolve(_ref(NOVERSION), _spec())
    web.routes[NOVERSION] = _html("Spec Doc", ["Dokumente  und  Schemas."])
    res = monitor.resolve(_ref(NOVERSION), _spec())
    assert res.classification == Classification.COSMETIC_ONLY


def test_snapshot_version_change_without_baseline(monitor, web):
    web.routes[BURWEB] = _html("BurWeb 1.8.0 | Portal")
    monitor.resolve(_ref(BURWEB), _spec())
    web.routes[BURWEB] = _html("BurWeb 1.9.0 | Portal")
    res = monitor.resolve(_ref(BURWEB), _spec())
    assert res.classification == Classification.CHANGED
    assert res.old.version == "1.8.0" and res.old.inferred
    assert res.new.version == "1.9.0"
    assert res.diff_text


def test_stated_baseline_without_extractable_version_falls_back_to_snapshot(monitor, web):
    web.routes[NOVERSION] = _html("Spec Doc", ["Dokumente"])
    res = monitor.resolve(_ref(NOVERSION, baseline_version="1.2"), _spec())
    assert res.classification == Classification.NOT_MONITORED
    assert any("no version" in w and "snapshot" in w for w in res.warnings)


def test_plain_body_hash_and_cap(monitor, web, monkeypatch):
    monkeypatch.setattr("digest.spec_monitor.url_monitor.MAX_TEXT_CHARS", 200 * 1024)
    url = "https://x.example.ch/readme.txt"
    web.routes[url] = (200, "line one\n\n  line   two ", {"Content-Type": "text/plain"})
    assert monitor.resolve(_ref(url), _spec()).classification == Classification.NOT_MONITORED
    web.routes[url] = (200, "line one\nline two", {"Content-Type": "text/plain"})
    assert monitor.resolve(_ref(url), _spec()).classification == Classification.UNCHANGED
    web.routes[url] = (200, "x" * 400_000, {"Content-Type": "text/plain"})
    res = monitor.resolve(_ref(url), _spec())
    assert res.classification == Classification.CHANGED
    assert len(res.new_text) <= 200 * 1024


def test_binary_content_hashed_without_text(monitor, web):
    url = "https://x.example.ch/spec.pdf"
    web.routes[url] = (200, b"%PDF-1.4 aaa", {"Content-Type": "application/pdf"})
    assert monitor.resolve(_ref(url), _spec()).classification == Classification.NOT_MONITORED
    web.routes[url] = (200, b"%PDF-1.4 bbb", {"Content-Type": "application/pdf"})
    res = monitor.resolve(_ref(url), _spec())
    assert res.classification == Classification.CHANGED
    assert res.new_text is None


def test_http_error_status_is_error(monitor, web):
    res = monitor.resolve(_ref("https://x.example.ch/missing"), _spec())
    assert res.classification == Classification.ERROR
    assert "404" in res.errors[0]


def test_request_failed_is_error_with_attempts(monitor, web):
    url = "https://x.example.ch/down"
    web.routes[url] = requests.ConnectionError("boom")
    res = monitor.resolve(_ref(url), _spec())
    assert res.classification == Classification.ERROR
    assert res.attempts == 2
    assert "boom" in res.errors[0]


def test_section_and_note_in_warnings(monitor, web):
    web.routes[ZEFIX_API] = _openapi("2.7.2.4")
    res = monitor.resolve(_ref(ZEFIX_UI, baseline_version="2.7", section="Abschnitt 3.1.8", note="Stand Q3"), _spec())
    assert any("Abschnitt 3.1.8" in w for w in res.warnings)
    assert any("Stand Q3" in w for w in res.warnings)


def test_missing_url_is_error(monitor, web):
    res = monitor.resolve(SourceRef(name="x", kind=SourceKind.URL), _spec())
    assert res.classification == Classification.ERROR


def test_requests_use_configured_timeout(web, tmp_path):
    web.routes[ZEFIX_UI] = _html("Swagger UI 5.1.0")
    mon = UrlMonitor(
        RetryConfig(attempts=1, backoff_seconds=0, timeout_seconds=77), SpecCache(tmp_path / "spec_cache"),
        baseline_dir=tmp_path / "url_baselines", today=TODAY,
    )
    mon.resolve(_ref(ZEFIX_UI, baseline_version="5.1"), _spec())
    calls = requests.request.call_args_list
    assert len(calls) >= 2 and all(c.kwargs["timeout"] == 77 for c in calls)


def _plain(web, text):
    url = "https://x.example.ch/readme.txt"
    web.routes[url] = (200, text, {"Content-Type": "text/plain"})
    return url


def _mon(tmp_path, dry_run=False):
    return UrlMonitor(
        RetryConfig(attempts=1, backoff_seconds=0), SpecCache(tmp_path / "spec_cache"),
        baseline_dir=tmp_path / "url_baselines", dry_run=dry_run, today=TODAY,
    )


def test_baseline_lives_outside_disposable_cache(tmp_path, web):
    url = _plain(web, "one")
    _mon(tmp_path).resolve(_ref(url), _spec())
    assert list((tmp_path / "url_baselines").rglob("*.json"))
    assert not (tmp_path / "spec_cache").exists() or not list((tmp_path / "spec_cache").rglob("*.json"))
    import shutil
    shutil.rmtree(tmp_path / "spec_cache", ignore_errors=True)
    web.routes[url] = (200, "two", {"Content-Type": "text/plain"})
    assert _mon(tmp_path).resolve(_ref(url), _spec()).classification == Classification.CHANGED


def test_default_baseline_dir_is_sibling_of_cache_dir(tmp_path, web):
    url = _plain(web, "one")
    mon = UrlMonitor(RetryConfig(attempts=1, backoff_seconds=0), SpecCache(tmp_path / "spec_cache"), today=TODAY)
    mon.resolve(_ref(url), _spec())
    assert list((tmp_path / "url_baselines").rglob("*.json"))


def test_dry_run_does_not_create_baseline(tmp_path, web):
    url = _plain(web, "one")
    res = _mon(tmp_path, dry_run=True).resolve(_ref(url), _spec())
    assert res.classification == Classification.NOT_MONITORED
    assert any("dry run" in w for w in res.warnings)
    assert not (tmp_path / "url_baselines").exists()
    assert _mon(tmp_path).resolve(_ref(url), _spec()).classification == Classification.NOT_MONITORED
    web.routes[url] = (200, "two", {"Content-Type": "text/plain"})
    assert _mon(tmp_path).resolve(_ref(url), _spec()).classification == Classification.CHANGED


def test_dry_run_reads_existing_baseline_without_advancing(tmp_path, web):
    url = _plain(web, "one")
    _mon(tmp_path).resolve(_ref(url), _spec())
    web.routes[url] = (200, "two", {"Content-Type": "text/plain"})
    for _ in range(2):
        assert _mon(tmp_path, dry_run=True).resolve(_ref(url), _spec()).classification == Classification.CHANGED
    assert _mon(tmp_path).resolve(_ref(url), _spec()).classification == Classification.CHANGED


def test_reported_change_advances_baseline(tmp_path, web):
    url = _plain(web, "one")
    mon = _mon(tmp_path)
    mon.resolve(_ref(url), _spec())
    web.routes[url] = (200, "two", {"Content-Type": "text/plain"})
    assert mon.resolve(_ref(url), _spec()).classification == Classification.CHANGED
    again = mon.resolve(_ref(url), _spec())
    assert again.classification == Classification.UNCHANGED
    assert again.old.inferred_reason == "snapshot of 2026-11-10 (local baseline)"
    web.routes[url] = (200, "three", {"Content-Type": "text/plain"})
    res = mon.resolve(_ref(url), _spec())
    assert res.classification == Classification.CHANGED
    assert "- two" in res.diff_text


def test_cosmetic_change_does_not_advance_baseline(tmp_path, web):
    url = _plain(web, "Dokumente und Schemas")
    mon = _mon(tmp_path)
    mon.resolve(_ref(url), _spec())
    web.routes[url] = (200, "Dokumente  und  Schemas.", {"Content-Type": "text/plain"})
    assert mon.resolve(_ref(url), _spec()).classification == Classification.COSMETIC_ONLY
    web.routes[url] = (200, "Dokumente und Schemas, neu", {"Content-Type": "text/plain"})
    res = mon.resolve(_ref(url), _spec())
    assert res.classification == Classification.CHANGED
    assert "- Dokumente und Schemas" in res.diff_text


def test_unwritable_baseline_is_warned(tmp_path, web):
    blocker = tmp_path / "blocked"
    blocker.write_text("file", encoding="utf-8")
    url = _plain(web, "one")
    mon = UrlMonitor(
        RetryConfig(attempts=1, backoff_seconds=0), SpecCache(tmp_path / "spec_cache"),
        baseline_dir=blocker / "sub", today=TODAY,
    )
    res = mon.resolve(_ref(url), _spec())
    assert any("could not be saved" in w for w in res.warnings)


# --- silent-failure fixes ---------------------------------------------------

def test_change_beyond_size_cap_is_not_unchanged(tmp_path, web, monkeypatch):
    monkeypatch.setattr("digest.spec_monitor.url_monitor.MAX_TEXT_CHARS", 200 * 1024)
    url = "https://x.example.ch/big.txt"
    head = "\n".join(f"line {i}" for i in range(30_000))      # > 200 KB
    web.routes[url] = (200, head + "\nTAIL one", {"Content-Type": "text/plain"})
    mon = _mon(tmp_path)
    first = mon.resolve(_ref(url), _spec())
    assert any("larger than 200 KB" in w for w in first.warnings)
    web.routes[url] = (200, head + "\nTAIL two", {"Content-Type": "text/plain"})
    res = mon.resolve(_ref(url), _spec())
    assert res.classification == Classification.CHANGED
    assert any("beyond the stored first part" in w for w in res.warnings)


def test_cosmetic_looking_diff_on_capped_content_is_reported_as_changed(tmp_path, web, monkeypatch):
    monkeypatch.setattr("digest.spec_monitor.url_monitor.MAX_TEXT_CHARS", 200 * 1024)
    url = "https://x.example.ch/big.txt"
    head = "\n".join(f"line {i}" for i in range(30_000))
    web.routes[url] = (200, "Intro text\n" + head, {"Content-Type": "text/plain"})
    mon = _mon(tmp_path)
    mon.resolve(_ref(url), _spec())
    web.routes[url] = (200, "Intro  text.\n" + head, {"Content-Type": "text/plain"})
    res = mon.resolve(_ref(url), _spec())
    assert res.classification == Classification.CHANGED
    assert any("cosmetic-looking" in w for w in res.warnings)


def test_corrupt_baseline_is_an_error_and_not_overwritten(tmp_path, web):
    url = _plain(web, "one")
    mon = _mon(tmp_path)
    mon.resolve(_ref(url), _spec())
    files = list((tmp_path / "url_baselines").rglob("*.json"))
    assert len(files) == 1
    files[0].write_text("{not json", encoding="utf-8")
    web.routes[url] = (200, "two", {"Content-Type": "text/plain"})
    res = _mon(tmp_path).resolve(_ref(url), _spec())
    assert res.classification == Classification.ERROR
    assert "corrupt" in res.errors[0]
    assert files[0].read_text(encoding="utf-8") == "{not json"


def test_swagger_probe_failure_is_warned(monitor, web):
    web.routes[ZEFIX_UI] = _html("Swagger UI 5.1.0")
    res = monitor.resolve(_ref(ZEFIX_UI, baseline_version="5.1"), _spec())
    assert any("swagger-ui" in w and "HTTP 404" in w and "falling back" in w for w in res.warnings)


def test_swagger_probe_not_openapi_is_warned(monitor, web):
    web.routes[ZEFIX_API] = json.dumps({"hello": "world"})
    web.routes[ZEFIX_UI] = _html("Swagger UI 5.1.0")
    res = monitor.resolve(_ref(ZEFIX_UI, baseline_version="5.1"), _spec())
    assert any("not an OpenAPI document" in w for w in res.warnings)


def test_html_snapshot_states_its_limitation(monitor, web):
    web.routes[BURWEB] = _html("BurWeb 1.8 | Portal")
    res = monitor.resolve(_ref(BURWEB, baseline_version="1.8"), _spec())
    assert any("body text are not detected" in w for w in res.warnings)


def test_deferred_baseline_is_only_written_on_commit(tmp_path):
    from digest.spec_monitor.url_monitor import BASELINE_NAMESPACE, UrlMonitor, hash_key
    from digest.spec_monitor.cache import SpecCache
    from digest.config import RetryConfig
    cache = SpecCache(tmp_path / "spec_cache")
    mon = UrlMonitor(RetryConfig(), cache, baseline_dir=tmp_path / "bl", defer_baselines=True)
    snap = type("S", (), {"version": "1", "text": "t", "fingerprint": "f", "truncated": False})()
    ref = type("R", (), {"url": "https://x.example/doc"})()
    warnings: list = []
    mon._save_baseline(ref, snap, "2026-11-10", warnings)
    assert mon.baselines.get_json(BASELINE_NAMESPACE, hash_key(ref.url)) is None
    assert mon.commit_baselines() == []
    stored = mon.baselines.get_json(BASELINE_NAMESPACE, hash_key(ref.url))
    assert stored["fingerprint"] == "f"
    assert mon.commit_baselines() == []


def test_big_document_is_stored_and_diffed_in_full_by_default(tmp_path, web):
    url = "https://x.example.ch/big.txt"
    head = "\n".join(f"line {i}" for i in range(60_000))     # ~600 KB, over the former 200 KB cap
    web.routes[url] = (200, head + "\nTAIL one", {"Content-Type": "text/plain"})
    mon = _mon(tmp_path)
    first = mon.resolve(_ref(url), _spec())
    assert not any("larger than" in w for w in first.warnings)
    web.routes[url] = (200, head + "\nTAIL two changed", {"Content-Type": "text/plain"})
    res = mon.resolve(_ref(url), _spec())
    assert res.classification == Classification.CHANGED
    assert "TAIL two changed" in res.diff_text and not any("beyond the stored" in w for w in res.warnings)
