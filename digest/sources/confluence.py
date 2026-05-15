import concurrent.futures
import html
import difflib
import logging
import re
import warnings
import requests
from datetime import datetime, timezone
from typing import List, Optional
from digest.config import AtlassianConfig
from digest.models import SourceItem

log = logging.getLogger(__name__)

# Confluence space keys follow the convention [A-Z][A-Z0-9]* per Atlassian docs.
_SPACE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*$")


def fetch(config: AtlassianConfig, auth_header: str, since: datetime) -> List[SourceItem]:
    # Confluence CQL requires "YYYY-MM-DD HH:MM" format — the T-separator is not accepted.
    since_cql = since.strftime("%Y-%m-%d %H:%M")
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
        f"{config.url}/wiki/rest/api/user/current",
        headers={"Authorization": auth_header, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["accountId"]


def _cql_search(config: AtlassianConfig, auth_header: str, cql: str) -> list:
    resp = requests.get(
        f"{config.url}/wiki/rest/api/content/search",
        headers={"Authorization": auth_header, "Accept": "application/json"},
        params={"cql": cql, "expand": "history,version,ancestors", "limit": 50},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    total = data.get("totalSize", 0)
    if len(results) >= 50 and total > 50:
        warnings.warn(
            f"Confluence CQL returned 50+ results (total={total}); "
            "some items may be missing. Consider narrowing your space list or time window.",
            RuntimeWarning,
            stacklevel=3,
        )
    return results


def _fetch_mentions(config: AtlassianConfig, auth_header: str, account_id: str, since_cql: str) -> List[SourceItem]:
    cql = f'mention = "{account_id}" AND created > "{since_cql}"'
    results = _cql_search(config, auth_header, cql)
    return [
        SourceItem(
            source="confluence", kind="mention",
            title=r["title"],  # clean title — no "Mentioned in:" prefix
            url=f"{config.url}/wiki{r['_links'].get('webui', '')}",
            content=f"You were mentioned in '{r['title']}' by {r.get('history', {}).get('createdBy', {}).get('displayName') or 'unknown'}.",
            author=r.get("history", {}).get("createdBy", {}).get("displayName") or "unknown",
            timestamp=_parse_dt(r.get("history", {}).get("createdDate", since_cql + ":00Z")),
        )
        for r in results
    ]


def _fetch_page_updates(config: AtlassianConfig, auth_header: str, since_cql: str, since: datetime) -> List[SourceItem]:
    if not config.confluence_spaces:
        return []
    spaces = " OR ".join(f'space = "{s}"' for s in config.confluence_spaces)
    cql = f'({spaces}) AND type = page AND lastModified > "{since_cql}"'
    results = _cql_search(config, auth_header, cql)

    if not results:
        return []

    def _fetch_one(r: dict) -> Optional[SourceItem]:
        page_id = r["id"]
        version_num = r.get("version", {}).get("number", 1)
        diff = _fetch_page_diff(config, auth_header, page_id, version_num, since)
        if diff is None:
            return None
        return SourceItem(
            source="confluence", kind="page_update",
            title=r["title"],  # clean title — no "Updated:" prefix
            url=f"{config.url}/wiki{r['_links'].get('webui', '')}",
            content=diff,
            author=r.get("version", {}).get("by", {}).get("displayName") or "unknown",
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
    config: AtlassianConfig, auth_header: str, page_id: str, version_num: int, since: datetime
) -> Optional[str]:
    """Fetch current and baseline version bodies; return a text diff or None if trivial.

    Baseline is the most recent version whose timestamp is <= since, ensuring the diff
    covers all edits in the window even when a page was edited multiple times.
    """
    if version_num <= 1:
        return None  # no history to diff against

    headers = {"Authorization": auth_header, "Accept": "application/json"}

    # Walk backwards from version_num-1 fetching only metadata (no body) until we find
    # the most recent version that predates `since`.
    baseline = None
    for v in range(version_num - 1, 0, -1):
        try:
            resp = requests.get(
                f"{config.url}/wiki/rest/api/content/{page_id}",
                headers=headers,
                params={"expand": "version", "status": "historical", "version": v},
                timeout=30,
            )
            resp.raise_for_status()
            when_str = resp.json().get("version", {}).get("when", "")
            if when_str and _parse_dt(when_str) <= since:
                baseline = v
                break
        except requests.RequestException:
            break

    if baseline is None:
        return None  # page created entirely within the window; no pre-window baseline

    try:
        curr = requests.get(
            f"{config.url}/wiki/rest/api/content/{page_id}",
            headers=headers,
            params={"expand": "body.storage"},
            timeout=30,
        )
        curr.raise_for_status()
        curr_body = curr.json().get("body", {}).get("storage", {}).get("value", "")

        prev = requests.get(
            f"{config.url}/wiki/rest/api/content/{page_id}",
            headers=headers,
            params={"expand": "body.storage", "status": "historical", "version": baseline},
            timeout=30,
        )
        prev.raise_for_status()
        prev_body = prev.json().get("body", {}).get("storage", {}).get("value", "")
    except requests.RequestException:
        return None  # gracefully skip if diff fetch fails

    curr_text = _storage_to_text(curr_body)
    prev_text = _storage_to_text(prev_body)
    return _compute_diff(prev_text, curr_text)


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
    # Replace block elements with newlines to preserve paragraph/list structure
    text = re.sub(r"<(?:p|li|h[1-6]|br|tr|td|th)[^>]*/?>", "\n", text, flags=re.IGNORECASE)
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


def _merge_by_page(items: List[SourceItem]) -> List[SourceItem]:
    """Combine mention + page_update items for the same page URL into one item.

    When you were both mentioned in a page and the page was updated, the LLM
    gets a single item with both contexts rather than two separate items.
    """
    by_url: dict = {}
    for item in items:
        by_url.setdefault(item.url, []).append(item)

    merged: List[SourceItem] = []
    for url, page_items in by_url.items():
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
            url=url,
            content="\n\n".join(parts),
            author=max(page_items, key=lambda x: x.timestamp).author,
            timestamp=max(it.timestamp for it in page_items),
        ))

    return merged


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
