from datetime import date

import pytest

from digest.config import AtlassianConfig, RetryConfig
from digest.spec_monitor import trace_jira
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import TraceSourceError
from digest.spec_monitor.trace_jira import build_jql, find_ticket_by_label, search_issues

BASE = "https://example.atlassian.net"
ATL = AtlassianConfig(url=BASE, email="a@b.c", api_token="t")
RETRY = RetryConfig()
SINCE = date(2026, 8, 10)


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


def issue(key, status="To Do", labels=(), summary="S"):
    return {"key": key, "fields": {"summary": summary, "status": {"name": status},
                                   "updated": "2026-10-01T10:00:00.000+0200", "labels": list(labels)}}


@pytest.fixture
def api(mocker):
    return mocker.patch("digest.spec_monitor.trace_jira.request_with_retry")


def test_build_jql_shape():
    jql = build_jql(["Batch", "Meldung"], ["PSSA", "RMEG"], SINCE)
    assert jql.startswith('project in ("PSSA", "RMEG") AND updated >= "2026-08-10"')
    assert '(text ~ "Batch" OR text ~ "Meldung")' in jql
    assert jql.endswith("ORDER BY updated DESC")


def test_terms_are_escaped():
    jql = build_jql(['say "hi"', "a\\b", "x*y?", "foo:bar", "(x)", "[{^~!&|}]", "  ", "***"], ["P"], SINCE)
    terms = jql.split(" AND (", 1)[1].rsplit(") ORDER BY", 1)[0].split(" OR ")
    assert terms == ['text ~ "say \\"hi\\""', 'text ~ "a\\\\b"', 'text ~ "x y"', 'text ~ "foo bar"', 'text ~ "x"']
    for ch in "*?:()[]{}^~!&|":
        assert ch not in "".join(t[len('text ~ "'):-1] for t in terms)


def test_all_terms_empty_raises():
    with pytest.raises(ValueError):
        build_jql(["***", " "], ["P"], SINCE)


def test_projects_are_quoted_and_required():
    assert 'project in ("A\\"B")' in build_jql(["x"], ['A"B'], SINCE)
    with pytest.raises(ValueError):
        build_jql(["x"], [], SINCE)


def test_candidates_carry_status_and_own_marker(api):
    api.return_value = FakeResp(body={"issues": [
        issue("PSSA-1", "In Progress", ["x"], "Batch"),
        issue("PSSA-2", "Done", ["pagewatch-123-2026W44"]),
        issue("PSSA-3"),
    ], "isLast": True})
    found = search_issues(ATL, "Basic x", RETRY, ["Batch"], ["PSSA"], SINCE, limit=2)
    assert [c.ref for c in found] == ["PSSA-1", "PSSA-2"]
    assert [c.own for c in found] == [False, True]
    assert found[0].status == "In Progress"
    assert found[0].kind == "jira"
    assert found[0].label == "Batch"
    assert found[0].url == f"{BASE}/browse/PSSA-1"
    method, url = api.call_args.args
    assert method == "POST" and url == f"{BASE}/rest/api/3/search/jql"
    body = api.call_args.kwargs["json"]
    assert body["fields"] == ["summary", "status", "updated", "labels"]
    assert api.call_args.kwargs["retry"] is RETRY
    assert api.call_args.kwargs["headers"]["Authorization"] == "Basic x"


def test_follows_cursor_until_limit(api):
    api.side_effect = [
        FakeResp(body={"issues": [issue("A-1"), issue("A-2")], "nextPageToken": "n1", "isLast": False}),
        FakeResp(body={"issues": [issue("A-3"), issue("A-4")], "isLast": True}),
    ]
    found = search_issues(ATL, "h", RETRY, ["x"], ["A"], SINCE, limit=3)
    assert [c.ref for c in found] == ["A-1", "A-2", "A-3"]
    assert api.call_args.kwargs["json"]["nextPageToken"] == "n1"


def test_non_ok_raises_trace_source_error(api):
    api.return_value = FakeResp(status=400, text="bad jql")
    with pytest.raises(TraceSourceError, match="400"):
        search_issues(ATL, "h", RETRY, ["x"], ["A"], SINCE)


def test_transport_failure_raises_trace_source_error(api):
    api.side_effect = RequestFailed("u", 3, "timeout")
    with pytest.raises(TraceSourceError, match="timeout"):
        search_issues(ATL, "h", RETRY, ["x"], ["A"], SINCE)


def test_unusable_terms_raise_trace_source_error(api):
    with pytest.raises(TraceSourceError):
        search_issues(ATL, "h", RETRY, ["***"], ["A"], SINCE)
    api.assert_not_called()


def test_find_ticket_by_label_returns_key_or_none(api):
    api.return_value = FakeResp(body={"issues": [issue("PSSA-9")], "isLast": True})
    assert find_ticket_by_label(ATL, "h", RETRY, "pagewatch-1-2026W45") == "PSSA-9"
    assert api.call_args.kwargs["json"]["jql"] == 'labels = "pagewatch-1-2026W45"'
    api.return_value = FakeResp(body={"issues": [], "isLast": True})
    assert find_ticket_by_label(ATL, "h", RETRY, "pagewatch-1-2026W45") is None


def test_find_ticket_by_label_failure_raises(api):
    api.return_value = FakeResp(status=500, text="x")
    with pytest.raises(TraceSourceError, match="500"):
        find_ticket_by_label(ATL, "h", RETRY, "l")
