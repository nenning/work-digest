"""Confluence REST access for the spec monitor (versions, historical bodies, search, attachments)."""
import json
from datetime import date, datetime, time
from typing import List, Optional
from urllib.parse import urljoin

from digest.config import AtlassianConfig, RetryConfig
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.http import RequestFailed, request_with_retry
from digest.spec_monitor.models import VersionPoint

_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_PAGES = 500  # safety cap for _paginate


def parse_when(value: Optional[str]) -> Optional[datetime]:
    """ISO timestamp from the REST API as a timezone-aware local datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone()


class NoStorageBody(LookupError):
    """The content has no body.storage (database, whiteboard, embed, folder)."""


def _storage_value(data: dict, page_id: str) -> str:
    value = (((data or {}).get("body") or {}).get("storage") or {}).get("value")
    if value is None:
        raise NoStorageBody(f"content {page_id} has no storage body")
    return value


def _cql_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ConfluenceClient:
    def __init__(self, atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, cache: SpecCache):
        self._atlassian = atlassian
        self._auth = auth_header
        self._retry = retry
        self._cache = cache
        self._root = atlassian.confluence_api_base.rstrip("/") + "/wiki"
        self._api = self._root + "/rest/api"

    def page_url(self, page_id: str, version: Optional[int] = None) -> str:
        url = f"{self._atlassian.url.rstrip('/')}/wiki/pages/viewpage.action?pageId={page_id}"
        if version is not None:
            url += f"&pageVersion={version}"
        return url

    def diff_url(self, page_id: str, old_version: int, new_version: int) -> str:
        return (f"{self._atlassian.url.rstrip('/')}/wiki/pages/diffpagesbyversion.action?pageId={page_id}"
                f"&selectedPageVersions={old_version}&selectedPageVersions={new_version}")

    def _headers(self) -> dict:
        return {"Authorization": self._auth, "Accept": "application/json"}

    def _get(self, url: str, **kwargs):
        return request_with_retry("GET", url, retry=self._retry, headers=self._headers(), **kwargs)

    @staticmethod
    def _check(resp, url: str, what: str) -> None:
        if resp.status_code == 404:
            raise LookupError(f"{what} not found (HTTP 404)")
        if not resp.ok:
            raise RequestFailed(url, 1, f"HTTP {resp.status_code}")

    def _next_url(self, data: dict) -> Optional[str]:
        nxt = (data.get("_links") or {}).get("next")
        if not nxt:
            return None
        if nxt.startswith("http"):
            return nxt
        return self._root + nxt

    def _paginate(self, url: str, params: dict, what: str) -> List[dict]:
        # Confluence ignores start= when expand is used; the cursor lives in _links.next. A cursor that
        # does not advance would loop forever, and partial data is never usable, so abort the run.
        results: List[dict] = []
        seen_ids: set = set()
        requested = {url}
        pages = 0
        while url:
            pages += 1
            if pages > _MAX_PAGES:
                raise RequestFailed(url, 1, "pagination did not advance")
            resp = self._get(url, params=params) if params is not None else self._get(url)
            self._check(resp, url, what)
            data = resp.json()
            batch = data.get("results", [])
            fresh = 0
            for r in batch:
                key = str(r.get("id", r.get("number", json.dumps(r, sort_keys=True, default=str))))
                if key not in seen_ids:
                    seen_ids.add(key)
                    results.append(r)
                    fresh += 1
            url = self._next_url(data)
            params = None
            if url and (fresh == 0 or url in requested):
                raise RequestFailed(url, 1, "pagination did not advance")
            if url is None and batch and fresh == 0:
                raise RequestFailed(url or "", 1, "pagination did not advance")
            requested.add(url)
        return results

    def search_descendants(self, parent_id: str, label: Optional[str] = None) -> List[dict]:
        cql = f"ancestor={parent_id} AND type=page"
        if label:
            cql += f" AND label = {_cql_quote(label)}"
        raw = self._paginate(
            f"{self._api}/content/search",
            {"cql": cql, "limit": 100, "expand": "version"},
            f"descendants of {parent_id}",
        )
        return [
            {"id": str(r["id"]), "title": r.get("title", ""), "version": (r.get("version") or {}).get("number"),
             "when": (r.get("version") or {}).get("when")}
            for r in raw
        ]

    def get_page(self, page_id: str, with_body: bool = True) -> dict:
        expand = "version,history" + (",body.storage" if with_body else "") + ",ancestors"
        url = f"{self._api}/content/{page_id}"
        resp = self._get(url, params={"expand": expand})
        self._check(resp, url, f"page {page_id}")
        return resp.json()

    def get_body(self, page_id: str, version: int) -> str:
        key = f"{page_id}_v{version}"
        cached = self._cache.get_text("confluence_body", key)
        if cached is not None:
            return cached
        url = f"{self._api}/content/{page_id}"
        resp = self._get(url, params={"status": "historical", "version": version, "expand": "body.storage"})
        if resp.status_code == 404:
            # the current version is not always served as "historical"
            live = self.get_page(page_id)
            if (live.get("version") or {}).get("number") != int(version):
                raise LookupError(f"version {version} of page {page_id} not found")
            body = _storage_value(live, page_id)
        else:
            self._check(resp, url, f"page {page_id}")
            body = _storage_value(resp.json(), page_id)
        self._cache.put_text("confluence_body", key, body)
        return body

    def list_versions(self, page_id: str) -> List[VersionPoint]:
        raw = self._paginate(f"{self._api}/content/{page_id}/version", {"limit": 200}, f"history of page {page_id}")
        points = [
            VersionPoint(version=str(r["number"]), when=parse_when(r.get("when")),
                         url=self.page_url(page_id, r["number"]))
            for r in raw
        ]
        return sorted(points, key=lambda p: int(p.version))

    def find_page_id(self, title: str, space_key: Optional[str] = None) -> Optional[str]:
        cql = f"type=page AND title = {_cql_quote(title)}"
        if space_key:
            cql += f" AND space = {_cql_quote(space_key)}"
        url = f"{self._api}/content/search"
        resp = self._get(url, params={"cql": cql, "limit": 5, "expand": "space"})
        self._check(resp, url, f"page '{title}'")
        results = resp.json().get("results", [])
        if not results:
            return None
        exact = [r for r in results if (r.get("title") or "").casefold() == title.casefold()]
        candidates = exact or results
        if space_key or len(candidates) == 1:
            return str(candidates[0]["id"])
        spaces = ", ".join(
            f"{((r.get('space') or {}).get('key') or '?')}:{r['id']}" for r in candidates
        )
        raise LookupError(f"title is ambiguous without a space key ({spaces})")

    def attachment_versions(self, page_id: str, filename: str) -> Optional[List[VersionPoint]]:
        """Versions (number and upload time) of an attachment of the page, oldest first;
        None when the page has no attachment of that name."""
        list_url = f"{self._api}/content/{page_id}/child/attachment"
        resp = self._get(list_url, params={"filename": filename})
        self._check(resp, list_url, f"attachments of page {page_id}")
        results = resp.json().get("results", [])
        if not results:
            return None
        att_id = results[0]["id"]
        raw = self._paginate(f"{self._api}/content/{att_id}/version", {"limit": 200},
                             f"versions of attachment {filename}")
        points = [
            VersionPoint(version=str(r["number"]), when=parse_when(r.get("when")),
                         url=self.page_url(page_id))
            for r in raw
        ]
        return sorted(points, key=lambda p: int(p.version))

    def download_attachment(self, page_id: str, filename: str, version: int) -> Optional[bytes]:
        list_url = f"{self._api}/content/{page_id}/child/attachment"
        resp = self._get(list_url, params={"filename": filename})
        self._check(resp, list_url, f"attachments of page {page_id}")
        results = resp.json().get("results", [])
        if not results:
            return None
        dl_url = f"{list_url}/{results[0]['id']}/download"
        resp = request_with_retry(
            "GET", dl_url, retry=self._retry, timeout=2 * self._retry.timeout_seconds,
            headers=self._headers(), params={"version": version}, allow_redirects=False,
        )
        if resp.status_code in _REDIRECTS:
            target = urljoin(dl_url, resp.headers.get("Location", ""))
            # media API rejects/leaks the Atlassian Authorization header: token is in the URL
            resp = request_with_retry("GET", target, retry=self._retry, timeout=2 * self._retry.timeout_seconds)
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise RequestFailed(dl_url, 1, f"HTTP {resp.status_code}")
        return resp.content


def pick_version_before(versions: List[VersionPoint], when: datetime) -> Optional[VersionPoint]:
    """Last version created at or before the moment `when`."""
    when = when if when.tzinfo else when.astimezone()
    best: Optional[VersionPoint] = None
    for v in versions:
        if v.when is None:
            continue
        v_when = v.when if v.when.tzinfo else v.when.astimezone()
        if v_when <= when and (best is None or int(v.version) > int(best.version)):
            best = v
    return best


def pick_version_at(versions: List[VersionPoint], day: date) -> Optional[VersionPoint]:
    """Last version created at or before the end of `day` (local time)."""
    end = datetime.combine(day, time.max).astimezone()
    best: Optional[VersionPoint] = None
    for v in versions:
        if v.when is None:
            continue
        when = v.when if v.when.tzinfo else v.when.astimezone()
        if when <= end and (best is None or int(v.version) > int(best.version)):
            best = v
    return best
