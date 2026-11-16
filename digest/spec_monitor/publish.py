"""Publishes the rendered report as a new Confluence page (REST, storage format)."""
import logging
from typing import List, Optional, Tuple
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


class PageConflict(RuntimeError):
    """The page was edited by someone else since it was read (HTTP 409)."""


def publish_page(
    atlassian: AtlassianConfig,
    auth_header: str,
    retry: RetryConfig,
    parent_page_id: str,
    title: str,
    storage: str,
    message: str,
) -> str:
    """Create a full-width page `title` under `parent_page_id`; returns its URL (see publish_page_detailed)."""
    return publish_page_detailed(atlassian, auth_header, retry, parent_page_id, title, storage, message)[1]


def publish_page_detailed(
    atlassian: AtlassianConfig,
    auth_header: str,
    retry: RetryConfig,
    parent_page_id: str,
    title: str,
    storage: str,
    message: str,
) -> Tuple[str, str]:
    """Create a full-width page `title` under `parent_page_id`; returns (page id, URL).

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
    return page_id, url


def _headers(auth_header: str) -> dict:
    return {"Authorization": auth_header, "Accept": "application/json", "Content-Type": "application/json"}


def list_child_pages(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig,
                     parent_page_id: str) -> List[dict]:
    """Direct children of the page as [{"id", "title"}]; raises RuntimeError when the listing fails."""
    url: Optional[str] = f"{_api(atlassian)}/content/{quote(str(parent_page_id), safe='')}/child/page"
    params: Optional[dict] = {"limit": 100}
    base = atlassian.confluence_api_base.rstrip("/")
    seen = set()
    out: List[dict] = []
    while url:
        resp = request_with_retry("GET", url, retry=retry, headers=_headers(auth_header), params=params)
        if not resp.ok:
            raise RuntimeError(f"Cannot list the child pages of {parent_page_id} ({_error_text(resp)})")
        data = resp.json() or {}
        out.extend({"id": str(r["id"]), "title": r.get("title", "")} for r in data.get("results") or [])
        nxt = (data.get("_links") or {}).get("next")
        url = None
        params = None
        if nxt:
            url = nxt if nxt.startswith("http") else f"{base}/wiki{nxt}"
            if url in seen:
                raise RuntimeError(f"Child page listing of {parent_page_id} did not advance")
            seen.add(url)
    return out


def find_child_page(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, parent_page_id: str,
                    title: str) -> Optional[str]:
    for page in list_child_pages(atlassian, auth_header, retry, parent_page_id):
        if page["title"].casefold() == title.casefold():
            return page["id"]
    return None


def read_page_storage(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig,
                      page_id: str) -> Tuple[str, int, str]:
    """(storage body, version number, title); LookupError when the page does not exist."""
    resp = request_with_retry(
        "GET", f"{_api(atlassian)}/content/{quote(str(page_id), safe='')}", retry=retry,
        headers=_headers(auth_header), params={"expand": "body.storage,version"},
    )
    if resp.status_code == 404:
        raise LookupError(f"page {page_id} not found")
    if not resp.ok:
        raise RuntimeError(f"Cannot read page {page_id} ({_error_text(resp)})")
    data = resp.json() or {}
    storage = ((data.get("body") or {}).get("storage") or {}).get("value")
    if storage is None:
        raise RuntimeError(f"Page {page_id} has no storage body")
    return storage, int((data.get("version") or {}).get("number") or 1), data.get("title") or ""


def update_page_storage(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, page_id: str,
                        title: str, storage: str, version: int, message: str) -> None:
    """Replace the body (version + 1). PageConflict when the page changed in between (HTTP 409)."""
    resp = request_with_retry(
        "PUT", f"{_api(atlassian)}/content/{quote(str(page_id), safe='')}", retry=retry,
        headers=_headers(auth_header),
        json={
            "type": "page", "title": title,
            "body": {"storage": {"value": storage, "representation": "storage"}},
            "version": {"number": version + 1, "message": message},
        },
    )
    if resp.status_code == 409:
        raise PageConflict(f"Page {page_id} was edited by someone else ({_error_text(resp)})")
    if not resp.ok:
        raise RuntimeError(f"Cannot update page {page_id} ({_error_text(resp)})")


def _find_attachment(atlassian, auth_header, retry, page_id: str, filename: str) -> Optional[dict]:
    resp = request_with_retry(
        "GET", f"{_api(atlassian)}/content/{quote(str(page_id), safe='')}/child/attachment", retry=retry,
        headers=_headers(auth_header), params={"filename": filename},
    )
    if not resp.ok:
        raise RuntimeError(f"Cannot look up attachment {filename} on page {page_id} ({_error_text(resp)})")
    for item in (resp.json() or {}).get("results") or []:
        if (item.get("title") or filename) == filename:
            return item
    return None


def put_attachment(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, page_id: str,
                   filename: str, data: bytes) -> None:
    """Create the attachment, or replace its data when a file of that name exists."""
    existing = _find_attachment(atlassian, auth_header, retry, page_id, filename)
    url = f"{_api(atlassian)}/content/{quote(str(page_id), safe='')}/child/attachment"
    if existing:
        url += f"/{existing['id']}/data"
    headers = {"Authorization": auth_header, "Accept": "application/json", "X-Atlassian-Token": "no-check"}
    resp = request_with_retry("POST", url, retry=retry, headers=headers,
                              files={"file": (filename, data, "application/json")})
    if not resp.ok:
        raise RuntimeError(f"Cannot store attachment {filename} on page {page_id} ({_error_text(resp)})")


def get_attachment(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, page_id: str,
                   filename: str) -> Optional[bytes]:
    item = _find_attachment(atlassian, auth_header, retry, page_id, filename)
    if item is None:
        return None
    link = (item.get("_links") or {}).get("download")
    base = atlassian.confluence_api_base.rstrip("/")
    if link:
        url = link if link.startswith("http") else f"{base}/wiki{link}"
    else:
        url = f"{base}/wiki/download/attachments/{page_id}/{quote(filename)}"
    resp = request_with_retry("GET", url, retry=retry, headers={"Authorization": auth_header, "Accept": "*/*"})
    if not resp.ok:
        raise RuntimeError(f"Cannot download attachment {filename} of page {page_id} ({_error_text(resp)})")
    return resp.content
