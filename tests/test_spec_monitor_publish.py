import pytest

from digest.config import AtlassianConfig, RetryConfig
from digest.spec_monitor import publish
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.publish import publish_page

BASE = "https://example.atlassian.net/wiki/rest/api"


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class FakeApi:
    """Routes (method, url) to canned responses and records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        handler = self.routes[(method, url)]
        if isinstance(handler, Exception):
            raise handler
        return handler


@pytest.fixture
def atlassian():
    return AtlassianConfig(url="https://example.atlassian.net/", email="e@x.com", api_token="t")


RETRY = RetryConfig(attempts=1, backoff_seconds=0)
CREATE_OK = FakeResp(200, {"id": "999", "_links": {"webui": "/spaces/RMEG/pages/999/Report"}})


def _run(atlassian, api, monkeypatch):
    monkeypatch.setattr(publish, "request_with_retry", api)
    return publish_page(atlassian, "Basic abc", RETRY, "123", "Quellen-Änderungen 2026-11-10 08:00", "<p>x</p>", "Spec monitor run")


def _base_routes(overrides=None):
    routes = {
        ("GET", f"{BASE}/content/123"): FakeResp(200, {"space": {"key": "RMEG"}}),
        ("POST", f"{BASE}/content"): CREATE_OK,
        ("GET", f"{BASE}/content/999/property/content-appearance-published"): FakeResp(404),
        ("GET", f"{BASE}/content/999/property/content-appearance-draft"): FakeResp(404),
        ("POST", f"{BASE}/content/999/property"): FakeResp(200, {}),
    }
    routes.update(overrides or {})
    return routes


def test_creates_page_under_parent_with_resolved_space(atlassian, monkeypatch):
    api = FakeApi(_base_routes())
    url = _run(atlassian, api, monkeypatch)

    assert url == "https://example.atlassian.net/wiki/spaces/RMEG/pages/999/Report"
    space_call = api.calls[0]
    assert space_call[0] == "GET" and space_call[2]["params"] == {"expand": "space"}
    assert space_call[2]["headers"]["Authorization"] == "Basic abc"

    post = next(c for c in api.calls if c[0] == "POST" and c[1] == f"{BASE}/content")
    body = post[2]["json"]
    assert body["type"] == "page"
    assert body["title"] == "Quellen-Änderungen 2026-11-10 08:00"
    assert body["space"] == {"key": "RMEG"}
    assert body["ancestors"] == [{"id": "123"}]
    assert body["body"]["storage"] == {"value": "<p>x</p>", "representation": "storage"}
    assert body["version"]["message"] == "Spec monitor run"


def test_full_width_properties_created_on_404(atlassian, monkeypatch):
    api = FakeApi(_base_routes())
    _run(atlassian, api, monkeypatch)

    created = [c[2]["json"] for c in api.calls if c[0] == "POST" and c[1] == f"{BASE}/content/999/property"]
    assert created == [
        {"key": "content-appearance-published", "value": "full-width"},
        {"key": "content-appearance-draft", "value": "full-width"},
    ]


def test_full_width_property_updated_with_incremented_version(atlassian, monkeypatch):
    pub = f"{BASE}/content/999/property/content-appearance-published"
    api = FakeApi(_base_routes({
        ("GET", pub): FakeResp(200, {"value": "default", "version": {"number": 3}}),
        ("PUT", pub): FakeResp(200, {}),
    }))
    _run(atlassian, api, monkeypatch)

    put = next(c for c in api.calls if c[0] == "PUT")
    assert put[1] == pub
    assert put[2]["json"] == {"key": "content-appearance-published", "value": "full-width", "version": {"number": 4}}


def test_property_already_full_width_is_left_alone(atlassian, monkeypatch):
    pub = f"{BASE}/content/999/property/content-appearance-published"
    draft = f"{BASE}/content/999/property/content-appearance-draft"
    api = FakeApi(_base_routes({
        ("GET", pub): FakeResp(200, {"value": "full-width", "version": {"number": 1}}),
        ("GET", draft): FakeResp(200, {"value": "full-width", "version": {"number": 1}}),
    }))
    _run(atlassian, api, monkeypatch)

    assert not [c for c in api.calls if c[0] == "PUT"]
    assert not [c for c in api.calls if c[1] == f"{BASE}/content/999/property"]


def test_url_fallback_when_response_has_no_webui(atlassian, monkeypatch):
    api = FakeApi(_base_routes({("POST", f"{BASE}/content"): FakeResp(200, {"id": "999"})}))
    assert _run(atlassian, api, monkeypatch) == "https://example.atlassian.net/wiki/spaces/RMEG/pages/999"


def test_scoped_auth_uses_confluence_api_base(monkeypatch):
    cfg = AtlassianConfig(
        url="https://example.atlassian.net", email="e", api_token="t", auth_type="scoped",
        confluence_api_base="https://api.atlassian.com/ex/confluence/cid",
    )
    base = "https://api.atlassian.com/ex/confluence/cid/wiki/rest/api"
    api = FakeApi({
        ("GET", f"{base}/content/123"): FakeResp(200, {"space": {"key": "RMEG"}}),
        ("POST", f"{base}/content"): CREATE_OK,
        ("GET", f"{base}/content/999/property/content-appearance-published"): FakeResp(404),
        ("GET", f"{base}/content/999/property/content-appearance-draft"): FakeResp(404),
        ("POST", f"{base}/content/999/property"): FakeResp(200, {}),
    })
    url = _run(cfg, api, monkeypatch)
    assert url.startswith("https://example.atlassian.net/wiki/spaces/")


def test_parent_not_readable_raises_without_creating(atlassian, monkeypatch):
    api = FakeApi(_base_routes({("GET", f"{BASE}/content/123"): FakeResp(403, text="forbidden")}))
    with pytest.raises(RuntimeError, match="parent page 123.*403"):
        _run(atlassian, api, monkeypatch)
    assert not [c for c in api.calls if c[0] == "POST"]


def test_missing_space_key_raises(atlassian, monkeypatch):
    api = FakeApi(_base_routes({("GET", f"{BASE}/content/123"): FakeResp(200, {})}))
    with pytest.raises(RuntimeError, match="no space key"):
        _run(atlassian, api, monkeypatch)


def test_create_rejected_raises_with_api_text(atlassian, monkeypatch):
    api = FakeApi(_base_routes({("POST", f"{BASE}/content"): FakeResp(400, text="A page with this title already exists")}))
    with pytest.raises(RuntimeError, match="already exists"):
        _run(atlassian, api, monkeypatch)


def test_transient_failure_propagates_as_request_failed(atlassian, monkeypatch):
    api = FakeApi(_base_routes({("POST", f"{BASE}/content"): RequestFailed(f"{BASE}/content", 3, "HTTP 503")}))
    with pytest.raises(RequestFailed):
        _run(atlassian, api, monkeypatch)


def test_property_failure_names_created_page_url(atlassian, monkeypatch):
    api = FakeApi(_base_routes({("POST", f"{BASE}/content/999/property"): FakeResp(500, text="boom")}))
    with pytest.raises(RuntimeError, match="created at https://example.atlassian.net/wiki/spaces/RMEG/pages/999/Report"):
        _run(atlassian, api, monkeypatch)


def test_requests_use_configured_timeout(atlassian, monkeypatch):
    api = FakeApi(_base_routes())
    monkeypatch.setattr("digest.spec_monitor.http.requests.request", api)
    publish_page(atlassian, "Basic abc", RetryConfig(attempts=1, backoff_seconds=0, timeout_seconds=77),
                 "123", "T", "<p>x</p>", "m")
    assert api.calls and all(c[2]["timeout"] == 77 for c in api.calls)


# ---- page-watch helpers: children, read / update, attachments ----

H = {"Authorization": "Basic abc"}


def _children_page(results, next_link=None):
    return FakeResp(200, {"results": results, "_links": {"next": next_link} if next_link else {}})


def test_publish_page_detailed_returns_id_and_url(atlassian, monkeypatch):
    api = FakeApi(_base_routes())
    monkeypatch.setattr(publish, "request_with_retry", api)
    page_id, url = publish.publish_page_detailed(atlassian, "Basic abc", RETRY, "123", "T", "<p>x</p>", "m")
    assert page_id == "999" and url == "https://example.atlassian.net/wiki/spaces/RMEG/pages/999/Report"


def test_list_child_pages_paginates(atlassian, monkeypatch):
    first = f"{BASE}/content/123/child/page"
    api = FakeApi({
        ("GET", first): _children_page([{"id": "1", "title": "A"}], "/rest/api/content/123/child/page?cursor=2"),
        ("GET", "https://example.atlassian.net/wiki/rest/api/content/123/child/page?cursor=2"):
            _children_page([{"id": 2, "title": "B"}]),
    })
    monkeypatch.setattr(publish, "request_with_retry", api)
    assert publish.list_child_pages(atlassian, "Basic abc", RETRY, "123") == [
        {"id": "1", "title": "A"}, {"id": "2", "title": "B"}]
    assert api.calls[0][2]["params"]["limit"] == 100


def test_find_child_page_hit_miss_and_case(atlassian, monkeypatch):
    api = FakeApi({("GET", f"{BASE}/content/123/child/page"): _children_page(
        [{"id": "7", "title": "Page Watch KW45 2026"}, {"id": "8", "title": "Other"}])})
    monkeypatch.setattr(publish, "request_with_retry", api)
    assert publish.find_child_page(atlassian, "Basic abc", RETRY, "123", "page watch kw45 2026") == "7"
    assert publish.find_child_page(atlassian, "Basic abc", RETRY, "123", "Page Watch KW46 2026") is None


def test_list_child_pages_failure_raises(atlassian, monkeypatch):
    api = FakeApi({("GET", f"{BASE}/content/123/child/page"): FakeResp(403, text="no")})
    monkeypatch.setattr(publish, "request_with_retry", api)
    with pytest.raises(RuntimeError, match="403"):
        publish.list_child_pages(atlassian, "Basic abc", RETRY, "123")


def test_read_page_storage(atlassian, monkeypatch):
    api = FakeApi({("GET", f"{BASE}/content/50"): FakeResp(200, {
        "title": "Page Watch KW45 2026", "version": {"number": 4}, "body": {"storage": {"value": "<p>x</p>"}}})})
    monkeypatch.setattr(publish, "request_with_retry", api)
    assert publish.read_page_storage(atlassian, "Basic abc", RETRY, "50") == ("<p>x</p>", 4, "Page Watch KW45 2026")
    assert api.calls[0][2]["params"] == {"expand": "body.storage,version"}


def test_read_page_storage_404_raises_lookup_error(atlassian, monkeypatch):
    monkeypatch.setattr(publish, "request_with_retry", FakeApi({("GET", f"{BASE}/content/50"): FakeResp(404)}))
    with pytest.raises(LookupError):
        publish.read_page_storage(atlassian, "Basic abc", RETRY, "50")


def test_update_page_storage_increments_the_version(atlassian, monkeypatch):
    api = FakeApi({("PUT", f"{BASE}/content/50"): FakeResp(200, {})})
    monkeypatch.setattr(publish, "request_with_retry", api)
    publish.update_page_storage(atlassian, "Basic abc", RETRY, "50", "T", "<p>y</p>", 4, "Page watch apply")
    body = api.calls[0][2]["json"]
    assert body["version"] == {"number": 5, "message": "Page watch apply"}
    assert body["type"] == "page" and body["title"] == "T"
    assert body["body"]["storage"] == {"value": "<p>y</p>", "representation": "storage"}


def test_update_page_storage_conflict_raises_page_conflict(atlassian, monkeypatch):
    api = FakeApi({("PUT", f"{BASE}/content/50"): FakeResp(409, text="conflict")})
    monkeypatch.setattr(publish, "request_with_retry", api)
    with pytest.raises(publish.PageConflict):
        publish.update_page_storage(atlassian, "Basic abc", RETRY, "50", "T", "<p>y</p>", 4, "m")


def test_update_page_storage_other_failure_raises_runtime_error(atlassian, monkeypatch):
    api = FakeApi({("PUT", f"{BASE}/content/50"): FakeResp(403, text="no")})
    monkeypatch.setattr(publish, "request_with_retry", api)
    with pytest.raises(RuntimeError, match="403") as exc:
        publish.update_page_storage(atlassian, "Basic abc", RETRY, "50", "T", "<p>y</p>", 4, "m")
    assert not isinstance(exc.value, publish.PageConflict)


def test_put_attachment_creates_when_absent(atlassian, monkeypatch):
    att = f"{BASE}/content/50/child/attachment"
    api = FakeApi({("GET", att): FakeResp(200, {"results": []}), ("POST", att): FakeResp(200, {"results": [{"id": "att1"}]})})
    monkeypatch.setattr(publish, "request_with_retry", api)
    publish.put_attachment(atlassian, "Basic abc", RETRY, "50", "page-watch-data.json", b"{}")
    lookup, post = api.calls
    assert lookup[2]["params"] == {"filename": "page-watch-data.json"}
    assert post[2]["headers"]["X-Atlassian-Token"] == "no-check"
    assert "Content-Type" not in post[2]["headers"]
    name, content, ctype = post[2]["files"]["file"]
    assert (name, content, ctype) == ("page-watch-data.json", b"{}", "application/json")


def test_put_attachment_replaces_an_existing_one(atlassian, monkeypatch):
    att = f"{BASE}/content/50/child/attachment"
    api = FakeApi({
        ("GET", att): FakeResp(200, {"results": [{"id": "att9", "title": "page-watch-data.json"}]}),
        ("POST", f"{att}/att9/data"): FakeResp(200, {}),
    })
    monkeypatch.setattr(publish, "request_with_retry", api)
    publish.put_attachment(atlassian, "Basic abc", RETRY, "50", "page-watch-data.json", b"{}")
    assert api.calls[1][1] == f"{att}/att9/data"


def test_put_attachment_failure_raises(atlassian, monkeypatch):
    att = f"{BASE}/content/50/child/attachment"
    api = FakeApi({("GET", att): FakeResp(200, {"results": []}), ("POST", att): FakeResp(413, text="big")})
    monkeypatch.setattr(publish, "request_with_retry", api)
    with pytest.raises(RuntimeError, match="413"):
        publish.put_attachment(atlassian, "Basic abc", RETRY, "50", "f.json", b"{}")


def test_get_attachment_downloads_the_file(atlassian, monkeypatch):
    att = f"{BASE}/content/50/child/attachment"
    download = "https://example.atlassian.net/wiki/download/attachments/50/page-watch-data.json?version=2"
    file_resp = FakeResp(200)
    file_resp.content = b'{"a": 1}'
    api = FakeApi({
        ("GET", att): FakeResp(200, {"results": [{"id": "att9", "title": "page-watch-data.json",
                                                   "_links": {"download": "/download/attachments/50/page-watch-data.json?version=2"}}]}),
        ("GET", download): file_resp,
    })
    monkeypatch.setattr(publish, "request_with_retry", api)
    assert publish.get_attachment(atlassian, "Basic abc", RETRY, "50", "page-watch-data.json") == b'{"a": 1}'


def test_get_attachment_returns_none_when_absent(atlassian, monkeypatch):
    api = FakeApi({("GET", f"{BASE}/content/50/child/attachment"): FakeResp(200, {"results": []})})
    monkeypatch.setattr(publish, "request_with_retry", api)
    assert publish.get_attachment(atlassian, "Basic abc", RETRY, "50", "x.json") is None
