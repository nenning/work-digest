import concurrent.futures
import html
import difflib
import logging
import re
import requests
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlunparse
from digest.config import AtlassianConfig
from digest.models import SourceItem

log = logging.getLogger(__name__)

# Confluence space keys follow the convention [A-Z][A-Z0-9]* per Atlassian docs.
_SPACE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*$")


def fetch(config: AtlassianConfig, auth_header: str, since: datetime) -> List[SourceItem]:
    # Confluence CQL requires "YYYY-MM-DD HH:MM" format — the T-separator is not accepted.
    since_cql = since.astimezone().strftime("%Y-%m-%d %H:%M")
    _validate_space_keys(config.confluence_spaces)
    items: List[SourceItem] = []
    user_account_id = _get_account_id(config, auth_header)
    # mentions use "created >" (when the mention was added)
    # page updates use "lastModified >" (when the page was last edited)
    items.extend(_fetch_mentions(config, auth_header, user_account_id, since_cql))
    items.extend(_fetch_page_updates(config, auth_header, since_cql, since))
    return _merge_by_page(items)


def _validate_space_keys(keys: List[str]) -> None:
    for key in keys:
        if not _SPACE_KEY_RE.match(key):
            raise ValueError(
                f"Invalid Confluence space key {key!r}. "
                "Space keys must match [A-Z][A-Z0-9]* (e.g. 'ENG', 'DOC2')."
            )


def _get_account_id(config: AtlassianConfig, auth_header: str) -> str:
    # Use Confluence's own user endpoint, not the Jira /rest/api/3/myself endpoint.
    resp = requests.get(
        f"{config.confluence_api_base}/wiki/rest/api/user/current",
        headers={"Authorization": auth_header, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["accountId"]


_PAGE_SIZE = 50
_MAX_SEARCH_PAGES = 200  # safety net: 200 * _PAGE_SIZE = 10,000 results


def _cql_search(config: AtlassianConfig, auth_header: str, cql: str, expand: str = "history,version,ancestors") -> list:
    """Page through CQL search results, following `_links.next`.

    Confluence Cloud's CQL search paginates via an opaque cursor in `_links.next`,
    not a client-incremented `start` -- see mgmt_confluence._search_all for the
    same fix and the live-API evidence. A previous version of this function fetched
    only the first page and warned if more existed, silently dropping the rest.
    """
    headers = {"Authorization": auth_header, "Accept": "application/json"}
    url = f"{config.confluence_api_base}/wiki/rest/api/content/search"
    params = {"cql": cql, "expand": expand, "limit": _PAGE_SIZE}
    results: list = []
    for _ in range(_MAX_SEARCH_PAGES):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))

        next_link = (data.get("_links") or {}).get("next")
        if not next_link:
            return results
        url = data["_links"]["base"] + next_link
        params = None  # the next link already encodes cql/expand/limit/cursor
    log.warning("Confluence search hit the %d-page safety cap; results may be incomplete", _MAX_SEARCH_PAGES)
    return results


def _fetch_mentions(config: AtlassianConfig, auth_header: str, account_id: str, since_cql: str) -> List[SourceItem]:
    cql = f'mention = "{account_id}" AND created > "{since_cql}"'
    results = _cql_search(config, auth_header, cql)
    items = []
    for r in results:
        author = r.get("history", {}).get("createdBy", {}).get("displayName") or "unknown"
        timestamp = _parse_dt(r.get("history", {}).get("createdDate", since_cql + ":00Z"))
        if r.get("type") == "comment":
            content = _fetch_comment_body(config, auth_header, r["id"], author, r["title"])
        else:
            content = f"You were mentioned in '{r['title']}' by {author}."
        items.append(SourceItem(
            source="confluence", kind="mention",
            title=r["title"],
            url=f"{config.url}/wiki{r['_links'].get('webui', '')}",
            content=content,
            author=author,
            timestamp=timestamp,
        ))
    return items


def _fetch_comment_body(config: AtlassianConfig, auth_header: str, comment_id: str, author: str, fallback_title: str) -> str:
    try:
        resp = requests.get(
            f"{config.confluence_api_base}/wiki/rest/api/content/{comment_id}",
            headers={"Authorization": auth_header, "Accept": "application/json"},
            params={"expand": "body.storage"},
            timeout=30,
        )
        resp.raise_for_status()
        storage = resp.json().get("body", {}).get("storage", {}).get("value", "")
        text = _storage_to_text(storage)
        if text:
            return f"{author} hat dich in einem Kommentar erwähnt:\n{text}"
    except requests.RequestException as exc:
        log.warning("Failed to fetch comment body %s: %s", comment_id, exc)
    return f"You were mentioned in '{fallback_title}' by {author}."


def _fetch_page_updates(config: AtlassianConfig, auth_header: str, since_cql: str, since: datetime) -> List[SourceItem]:
    if not config.confluence_spaces:
        return []
    spaces = " OR ".join(f'space = "{s}"' for s in config.confluence_spaces)
    cql = f'({spaces}) AND type = page AND lastModified > "{since_cql}"'
    # Every result here already fell inside the window (lastModified > since), so unlike
    # mgmt_confluence.py's broad "contributor ever" search, there's no later filtering step
    # that would discard most matches -- fetching the current body alongside the search
    # avoids a guaranteed follow-up GET per page instead of speculatively pre-fetching.
    results = _cql_search(config, auth_header, cql, expand="history,version,ancestors,body.storage")

    if not results:
        return []

    def _fetch_one(r: dict) -> Optional[SourceItem]:
        page_id = r["id"]
        version_num = r.get("version", {}).get("number", 1)
        current_body = r.get("body", {}).get("storage", {}).get("value", "")
        result = _fetch_page_diff(config, auth_header, page_id, version_num, since, current_body)
        if result is None:
            return None
        diff, in_window_authors = result
        current_author = r.get("version", {}).get("by", {}).get("displayName") or "unknown"
        authors = sorted(set(in_window_authors) | {current_author})
        return SourceItem(
            source="confluence", kind="page_update",
            title=r["title"],  # clean title — no "Updated:" prefix
            url=f"{config.url}/wiki{r['_links'].get('webui', '')}",
            content=diff,
            author=", ".join(authors),
            timestamp=_parse_dt(r.get("version", {}).get("when", since_cql + ":00Z")),
        )

    items: List[SourceItem] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(results), 8)) as executor:
        futures = [executor.submit(_fetch_one, r) for r in results]
        for future in concurrent.futures.as_completed(futures):
            try:
                item = future.result()
                if item is not None:
                    items.append(item)
            except Exception as exc:
                log.warning("Failed to fetch page diff: %s", exc)
    return items


def _fetch_page_diff(
    config: AtlassianConfig, auth_header: str, page_id: str, version_num: int, since: datetime, current_body: str
) -> Optional[Tuple[str, List[str]]]:
    """Diff the already-fetched current body against the baseline version; return
    (diff, in-window authors excluding the current version's) or None if trivial.

    Baseline is the most recent version whose timestamp is <= since, ensuring the diff
    covers all edits in the window even when a page was edited multiple times. The same
    backward walk used to locate that baseline already fetches every intermediate
    version's metadata (to check its timestamp), which includes its author -- so the
    distinct authors of every other in-window edit are collected here for free, the
    same way mgmt_confluence.py's _walk_version_history does for the management summary.
    """
    if version_num <= 1:
        return None  # no history to diff against

    headers = {"Authorization": auth_header, "Accept": "application/json"}

    # Walk backwards from version_num-1 fetching only metadata (no body) until we find
    # the most recent version that predates `since`. Every version visited before that
    # point postdates `since` (still in-window), so its author is recorded along the way.
    baseline = None
    in_window_authors: dict = {}
    for v in range(version_num - 1, 0, -1):
        try:
            resp = requests.get(
                f"{config.confluence_api_base}/wiki/rest/api/content/{page_id}",
                headers=headers,
                params={"expand": "version", "status": "historical", "version": v},
                timeout=30,
            )
            resp.raise_for_status()
            version = resp.json().get("version", {})
        except requests.RequestException:
            break
        when_str = version.get("when", "")
        if not when_str:
            continue
        when = _parse_dt(when_str)
        if when <= since:
            baseline = v
            break
        name = (version.get("by") or {}).get("displayName") or "unknown"
        if name not in in_window_authors or when > in_window_authors[name]:
            in_window_authors[name] = when

    if baseline is None:
        return None  # page created entirely within the window; no pre-window baseline

    try:
        prev = requests.get(
            f"{config.confluence_api_base}/wiki/rest/api/content/{page_id}",
            headers=headers,
            params={"expand": "body.storage", "status": "historical", "version": baseline},
            timeout=30,
        )
        prev.raise_for_status()
        prev_body = prev.json().get("body", {}).get("storage", {}).get("value", "")
    except requests.RequestException:
        return None  # gracefully skip if diff fetch fails

    curr_text = _storage_to_text(current_body)
    prev_text = _storage_to_text(prev_body)
    diff = _compute_diff(prev_text, curr_text)
    if diff is None:
        return None
    return diff, sorted(in_window_authors)


_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)


def _cell_text(cell_html: str) -> str:
    text = re.sub(r"<(?:p|li|br)[^>]*/?>", " ", cell_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _table_to_rows(table_html: str) -> str:
    """Render a table as "cell | cell | cell" rows.

    Stripping table tags outright collapses every cell into one flat list of
    values with no way to tell a header from a row or which value belongs to
    which column -- an LLM summarizing that flat list tends to describe the
    columns it can see (i.e. the table's structure) rather than the specific
    values, since it can't attribute them to anything. Keeping cells grouped
    by row preserves enough context to summarize the actual content instead.
    """
    rows = []
    for row_match in _ROW_RE.finditer(table_html):
        cells = [_cell_text(c) for c in _CELL_RE.findall(row_match.group(1))]
        cells = [c for c in cells if c]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _storage_to_text(storage_html: str) -> str:
    """Convert Confluence storage format (XHTML) to plain text, preserving structure."""
    # Status macros contain both a colour parameter and a title parameter; strip all
    # tags would concatenate them (e.g. "GreenCore"). Replace each macro with just its title.
    def _status_title(m: re.Match) -> str:
        title_m = re.search(r'ac:name="title"[^>]*>(.*?)</ac:parameter', m.group(0), re.IGNORECASE | re.DOTALL)
        return title_m.group(1) if title_m else ""

    text = re.sub(
        r'<ac:structured-macro[^>]*ac:name="status"[^>]*>.*?</ac:structured-macro>',
        _status_title,
        storage_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Render tables as pipe-delimited rows before the generic tag stripping below
    # would otherwise flatten every cell into an undifferentiated list of values.
    text = _TABLE_RE.sub(lambda m: "\n" + _table_to_rows(m.group(1)) + "\n", text)
    # Replace block elements with newlines to preserve paragraph/list structure
    text = re.sub(r"<(?:p|li|h[1-6]|br)[^>]*/?>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities (e.g. &amp; → &, &nbsp; → space)
    text = html.unescape(text)
    # Normalize: remove blank lines, strip each line
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def _compute_diff(old_text: str, new_text: str) -> Optional[str]:
    """Compute a human-readable diff; return None if changes are trivially small."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))

    added = [ln[1:].strip() for ln in diff if ln.startswith("+") and not ln.startswith("+++") and ln[1:].strip()]
    removed = [ln[1:].strip() for ln in diff if ln.startswith("-") and not ln.startswith("---") and ln[1:].strip()]

    # Skip cosmetic-only changes (very short lines, punctuation, whitespace)
    significant_added = [ln for ln in added if len(ln) > 8]
    significant_removed = [ln for ln in removed if len(ln) > 8]

    if not significant_added and not significant_removed:
        return None

    parts: List[str] = []
    if significant_added:
        parts.append("Added:\n" + "\n".join(f"+ {ln}" for ln in significant_added[:30]))
    if significant_removed:
        parts.append("Removed:\n" + "\n".join(f"- {ln}" for ln in significant_removed[:30]))

    return "\n\n".join(parts)


def _page_key(url: str) -> str:
    """Normalize a page/comment URL to its page-level form (no query string or
    fragment), so mentions/comments on the same page group together regardless
    of which comment anchor their individual URL points at.
    """
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


def _merge_by_page(items: List[SourceItem]) -> List[SourceItem]:
    """Combine mention + page_update items for the same page into one item.

    When you were both mentioned in a page and the page was updated, the LLM
    gets a single item with both contexts rather than two separate items.
    Grouping uses a normalized page key so multiple mention-comments on the
    same page (each with a distinct comment-anchor URL) also merge.
    """
    by_key: dict = {}
    for item in items:
        by_key.setdefault(_page_key(item.url), []).append(item)

    merged: List[SourceItem] = []
    for key, page_items in by_key.items():
        if len(page_items) == 1:
            merged.append(page_items[0])
            continue

        # Build combined content so the LLM sees all activity on this page together.
        parts: List[str] = []
        for it in sorted(page_items, key=lambda x: x.timestamp):
            label = {"mention": "Mention", "page_update": "Page update"}.get(it.kind, it.kind.title())
            parts.append(f"[{label} by {it.author}] {it.content}")

        merged.append(SourceItem(
            source="confluence",
            kind="page",
            title=page_items[0].title,
            url=key,
            content="\n\n".join(parts),
            author=max(page_items, key=lambda x: x.timestamp).author,
            timestamp=max(it.timestamp for it in page_items),
        ))

    return merged


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
