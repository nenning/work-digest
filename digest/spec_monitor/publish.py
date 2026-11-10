"""Publishes the rendered report as a new Confluence page (REST, storage format)."""
import logging
from urllib.parse import quote

from digest.config import AtlassianConfig, RetryConfig
from digest.spec_monitor.http import request_with_retry

log = logging.getLogger(__name__)

FULL_WIDTH_PROPERTIES = ("content-appearance-published", "content-appearance-draft")
FULL_WIDTH_VALUE = "full-width"


def _error_text(resp) -> str:
    return f"HTTP {resp.status_code}: {(resp.text or '').strip()[:300]}"


def _api(atlassian: AtlassianConfig) -> str:
    return f"{atlassian.confluence_api_base.rstrip('/')}/wiki/rest/api"


def _space_key(atlassian, headers, retry, parent_page_id) -> str:
    resp = request_with_retry(
        "GET", f"{_api(atlassian)}/content/{quote(str(parent_page_id), safe='')}",
        retry=retry, headers=headers, params={"expand": "space"},
    )
    if not resp.ok:
        raise RuntimeError(f"Cannot read parent page {parent_page_id} to resolve its space ({_error_text(resp)})")
    key = ((resp.json() or {}).get("space") or {}).get("key")
    if not key:
        raise RuntimeError(f"Parent page {parent_page_id} returned no space key")
    return key


def _set_full_width(atlassian, headers, retry, page_id: str) -> None:
    for prop in FULL_WIDTH_PROPERTIES:
        url = f"{_api(atlassian)}/content/{page_id}/property/{prop}"
        resp = request_with_retry("GET", url, retry=retry, headers=headers)
        if resp.status_code == 404:
            created = request_with_retry(
                "POST", f"{_api(atlassian)}/content/{page_id}/property",
                retry=retry, headers=headers, json={"key": prop, "value": FULL_WIDTH_VALUE},
            )
            if not created.ok:
                raise RuntimeError(f"Cannot create property {prop} ({_error_text(created)})")
            continue
        if not resp.ok:
            raise RuntimeError(f"Cannot read property {prop} ({_error_text(resp)})")
        body = resp.json() or {}
        if body.get("value") == FULL_WIDTH_VALUE:
            continue
        version = int((body.get("version") or {}).get("number") or 1)
        updated = request_with_retry(
            "PUT", url, retry=retry, headers=headers,
            json={"key": prop, "value": FULL_WIDTH_VALUE, "version": {"number": version + 1}},
        )
        if not updated.ok:
            raise RuntimeError(f"Cannot update property {prop} ({_error_text(updated)})")


def publish_page(
    atlassian: AtlassianConfig,
    auth_header: str,
    retry: RetryConfig,
    parent_page_id: str,
    title: str,
    storage: str,
    message: str,
) -> str:
    """Create a full-width page `title` under `parent_page_id`; returns its URL.

    Raises RequestFailed (transient failures exhausted) or RuntimeError (the API
    refused). A failure while setting full width leaves the page in place; the
    error text then names its URL.
    """
    headers = {"Authorization": auth_header, "Accept": "application/json", "Content-Type": "application/json"}
    space = _space_key(atlassian, headers, retry, parent_page_id)

    resp = request_with_retry(
        "POST", f"{_api(atlassian)}/content", retry=retry, headers=headers,
        json={
            "type": "page",
            "title": title,
            "space": {"key": space},
            "ancestors": [{"id": str(parent_page_id)}],
            "body": {"storage": {"value": storage, "representation": "storage"}},
            "version": {"number": 1, "message": message},
        },
    )
    if not resp.ok:
        raise RuntimeError(f"Cannot create page '{title}' under {parent_page_id} ({_error_text(resp)})")
    page = resp.json() or {}
    page_id = str(page["id"])

    webui = (page.get("_links") or {}).get("webui") or f"/spaces/{space}/pages/{page_id}"
    url = f"{atlassian.url.rstrip('/')}/wiki{webui}" if webui.startswith("/") else f"{atlassian.url.rstrip('/')}/wiki/spaces/{space}/pages/{page_id}"

    try:
        _set_full_width(atlassian, headers, retry, page_id)
    except Exception as exc:
        raise RuntimeError(f"Page was created at {url} but could not be set to full width: {exc}") from exc
    return url
