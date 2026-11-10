import pytest
import requests

from digest.config import RetryConfig
from digest.spec_monitor import http
from digest.spec_monitor.http import RequestFailed, request_with_retry


class Resp:
    def __init__(self, status=200, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


def _script(monkeypatch, outcomes):
    calls = []
    queue = list(outcomes)

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http.requests, "request", fake_request)
    return calls


RETRY = RetryConfig(attempts=3, backoff_seconds=2)


def test_success_on_first_try_passes_timeout_and_kwargs(monkeypatch):
    calls = _script(monkeypatch, [Resp(200)])
    sleeps = []
    resp = request_with_retry("GET", "http://x", retry=RETRY, timeout=7, sleep=sleeps.append, headers={"A": "b"})
    assert resp.status_code == 200
    assert calls[0][2]["timeout"] == 7 and calls[0][2]["headers"] == {"A": "b"}
    assert sleeps == []


@pytest.mark.parametrize("exc", [requests.ConnectionError("down"), requests.Timeout("slow"), requests.exceptions.ChunkedEncodingError("cut")])
def test_retries_connection_errors_and_timeouts_with_doubling_backoff(monkeypatch, exc):
    calls = _script(monkeypatch, [exc, exc, Resp(200)])
    sleeps = []
    resp = request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append)
    assert resp.status_code == 200
    assert len(calls) == 3
    assert sleeps == [2, 4]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retries_retryable_statuses(monkeypatch, status):
    calls = _script(monkeypatch, [Resp(status), Resp(200)])
    sleeps = []
    assert request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append).status_code == 200
    assert len(calls) == 2 and sleeps == [2]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_client_errors_are_returned_not_retried(monkeypatch, status):
    calls = _script(monkeypatch, [Resp(status)])
    sleeps = []
    assert request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append).status_code == status
    assert len(calls) == 1 and sleeps == []


def test_retry_after_is_honoured_on_429_when_longer_than_backoff(monkeypatch):
    _script(monkeypatch, [Resp(429, {"Retry-After": "9"}), Resp(200)])
    sleeps = []
    request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append)
    assert sleeps == [9]


def test_retry_after_never_shortens_backoff_and_ignores_unparseable(monkeypatch):
    _script(monkeypatch, [Resp(429, {"Retry-After": "1"}), Resp(429, {"Retry-After": "soon-ish"}), Resp(200)])
    sleeps = []
    request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append)
    assert sleeps == [2, 4]


def test_retry_after_ignored_on_non_429(monkeypatch):
    _script(monkeypatch, [Resp(503, {"Retry-After": "30"}), Resp(200)])
    sleeps = []
    request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append)
    assert sleeps == [2]


def test_raises_request_failed_with_attempt_count_after_exhaustion(monkeypatch):
    calls = _script(monkeypatch, [Resp(503), Resp(503), Resp(502)])
    sleeps = []
    with pytest.raises(RequestFailed) as err:
        request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append)
    assert err.value.attempts == 3
    assert err.value.last_error == "HTTP 502"
    assert err.value.url == "http://x"
    assert len(calls) == 3
    assert sleeps == [2, 4]


def test_exhausted_connection_errors_report_error_type(monkeypatch):
    _script(monkeypatch, [requests.ConnectionError("dns")] * 2)
    with pytest.raises(RequestFailed) as err:
        request_with_retry("GET", "http://x", retry=RetryConfig(attempts=2, backoff_seconds=1), sleep=lambda s: None)
    assert "ConnectionError" in err.value.last_error and err.value.attempts == 2


def test_zero_attempts_still_makes_one_call(monkeypatch):
    calls = _script(monkeypatch, [Resp(200)])
    request_with_retry("GET", "http://x", retry=RetryConfig(attempts=0), sleep=lambda s: None)
    assert len(calls) == 1


def test_timeout_defaults_to_retry_timeout_seconds(monkeypatch):
    calls = _script(monkeypatch, [Resp(200)])
    request_with_retry("GET", "http://x", retry=RetryConfig(timeout_seconds=77), sleep=lambda s: None)
    assert calls[0][2]["timeout"] == 77


def test_explicit_timeout_wins_over_retry_timeout(monkeypatch):
    calls = _script(monkeypatch, [Resp(200)])
    request_with_retry("GET", "http://x", retry=RetryConfig(timeout_seconds=77), timeout=5, sleep=lambda s: None)
    assert calls[0][2]["timeout"] == 5


def test_default_retry_config_timeout_is_used_when_nothing_given(monkeypatch):
    calls = _script(monkeypatch, [Resp(200)])
    request_with_retry("GET", "http://x", retry=RETRY, sleep=lambda s: None)
    assert calls[0][2]["timeout"] == RETRY.timeout_seconds


def _http_date(seconds_ahead):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    return format_datetime(datetime.now(timezone.utc) + timedelta(seconds=seconds_ahead), usegmt=True)


def test_retry_after_http_date_is_honoured(monkeypatch):
    _script(monkeypatch, [Resp(429, {"Retry-After": _http_date(40)}), Resp(200)])
    sleeps = []
    request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append)
    assert len(sleeps) == 1 and 30 < sleeps[0] <= 40


def test_retry_after_http_date_in_the_past_keeps_backoff(monkeypatch):
    _script(monkeypatch, [Resp(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}), Resp(200)])
    sleeps = []
    request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append)
    assert sleeps == [2]


@pytest.mark.parametrize("header", ["100000", None])
def test_retry_after_is_capped_at_120_seconds(monkeypatch, header):
    value = header or _http_date(100000)
    _script(monkeypatch, [Resp(429, {"Retry-After": value}), Resp(200)])
    sleeps = []
    request_with_retry("GET", "http://x", retry=RETRY, sleep=sleeps.append)
    assert sleeps == [120]
