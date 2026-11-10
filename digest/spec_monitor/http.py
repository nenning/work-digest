"""HTTP helper with retries for every outgoing call of the spec monitor."""
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests

from digest import usage
from digest.config import RetryConfig

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 120.0


class RequestFailed(Exception):
    """Transient failures persisted through every retry attempt."""

    def __init__(self, url: str, attempts: int, last_error: str):
        super().__init__(f"{url}: {last_error} (after {attempts} attempt(s))")
        self.url = url
        self.attempts = attempts
        self.last_error = last_error


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """Retry-After as seconds: a number or an HTTP-date; None when absent/unparseable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def request_with_retry(
    method: str,
    url: str,
    *,
    retry: RetryConfig,
    timeout: Optional[float] = None,
    sleep=time.sleep,
    **kwargs,
) -> requests.Response:
    """Perform the request, retrying connection errors, timeouts, 429 and 5xx.

    Returns the Response for any other status (callers check .ok / status_code;
    4xx other than 429 is never retried). Raises RequestFailed once the attempts
    are exhausted. 429 honours Retry-After (seconds or HTTP-date; never shorter than
    the backoff, capped at 120 s). `timeout` defaults to retry.timeout_seconds.
    """
    if timeout is None:
        timeout = retry.timeout_seconds
    attempts = max(1, retry.attempts)
    last_error = ""
    t0 = time.monotonic()
    for attempt in range(1, attempts + 1):
        delay = retry.backoff_seconds * (2 ** (attempt - 1))
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code not in _RETRY_STATUS:
                usage.record_http(url, attempts=attempt, status=resp.status_code, failed=False,
                                  seconds=time.monotonic() - t0)
                return resp
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code == 429:
                wait = _retry_after_seconds(resp.headers.get("Retry-After"))
                if wait is not None:
                    delay = max(delay, min(wait, _MAX_RETRY_AFTER))
        if attempt < attempts:
            log.warning("%s %s failed (%s), retry %d/%d in %.1fs", method, url, last_error, attempt, attempts - 1, delay)
            sleep(delay)
    usage.record_http(url, attempts=attempts, status=None, failed=True, seconds=time.monotonic() - t0)
    raise RequestFailed(url, attempts, last_error)
