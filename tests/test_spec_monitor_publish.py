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
