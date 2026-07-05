"""Team Confluence fetcher for management summary mode."""
from __future__ import annotations

import concurrent.futures
import requests
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

from digest.config import AtlassianConfig, MgmtSummaryConfig
from digest.models import SourceItem
from digest.sources.confluence import _compute_diff, _storage_to_text

_PAGE_SIZE = 50


def fetch_team_pages(
    config: AtlassianConfig,
    auth_header: str,
    mgmt_cfg: MgmtSummaryConfig,
    since: datetime,
    until: datetime,
    team_account_ids: Set[str],
) -> List[SourceItem]:
    """Fetch Confluence pages edited by a team member within the time range.

    team_account_ids comes from the Jira ticket assignees/reporters, so only users
    who actually worked on team tickets are included.
    """
    if not team_account_ids:
        return []

    since_cql = since.strftime("%Y-%m-%d %H:%M")
    until_cql = until.strftime("%Y-%m-%d %H:%M")

    ids_str = ", ".join(f'"{aid}"' for aid in sorted(team_account_ids))
    # "contributor" matches anyone who has ever edited the page, not just the current
    # version's author. This is combined with a version-history walk below so a team
    # member's edit isn't missed just because someone else edited the page again
    # later in the same window ("lastModifier in (...)" alone would miss that case).
    cql = (
        f"contributor in ({ids_str}) "
        f"AND lastModified >= \"{since_cql}\" "
        f"AND lastModified <= \"{until_cql}\" "
        f"AND type = page"
    )

    headers = {"Authorization": auth_header, "Accept": "application/json"}
    results = _search_all(config, headers, cql)
    if not results:
        return []

    def _resolve(r: dict) -> Optional[SourceItem]:
        page_id = r["id"]
        current_version = r.get("version") or {}
        match = _match_version(current_version, team_account_ids)
        if match is None:
            match = _find_earlier_team_edit(
                config, headers, page_id, current_version.get("number", 1) - 1, since, team_account_ids
            )
        if match is None:
            # contributor matched somewhere in the page's history, but not within
            # [since, until] -> not actually team activity for this period.
            return None

        author_name, timestamp = match

        # Diff against the pre-window baseline (or against "" for a page created
        # within the window, so the whole page reads as newly added content).
        # Reuses the personal digest's diff logic so downstream summarize_items()
        # produces the same kind of 1-2 sentence LLM summary for page changes.
        diff = _page_diff(config, headers, page_id, current_version.get("number", 1), since)
        if diff is None:
            return None  # cosmetic-only change, or body fetch failed

        return SourceItem(
            source="confluence",
            kind="page_update",
            title=r.get("title", "Untitled"),
            url=f"{config.url}/wiki{r.get('_links', {}).get('webui', '')}",
            content=diff,
            author=author_name,
            timestamp=timestamp,
        )

    items: List[SourceItem] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(results), 8)) as executor:
        futures = [executor.submit(_resolve, r) for r in results]
        for future in concurrent.futures.as_completed(futures):
            item = future.result()
            if item is not None:
                items.append(item)

    return items


def _search_all(config: AtlassianConfig, headers: dict, cql: str) -> List[dict]:
    """Page through CQL search results so matches past the first batch aren't dropped."""
    results: List[dict] = []
    start = 0
    while True:
        resp = requests.get(
            f"{config.url}/wiki/rest/api/content/search",
            headers=headers,
            params={"cql": cql, "expand": "version", "limit": _PAGE_SIZE, "start": start},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json().get("results", [])
        results.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        start += _PAGE_SIZE
    return results


def _match_version(version: dict, team_account_ids: Set[str]) -> Optional[Tuple[str, datetime]]:
    author = version.get("by") or {}
    if author.get("accountId") not in team_account_ids:
        return None
    when_str = version.get("when", "")
    timestamp = _parse_dt(when_str) if when_str else None
    if timestamp is None:
        return None
    return author.get("displayName") or "unknown", timestamp


def _find_earlier_team_edit(
    config: AtlassianConfig,
    headers: dict,
    page_id: str,
    start_version: int,
    since: datetime,
    team_account_ids: Set[str],
) -> Optional[Tuple[str, datetime]]:
    """Walk version history backward from start_version to find a team-authored edit >= since.

    The CQL search already guarantees the current version's timestamp is within
    [since, until], so once we find a version older than `since` we can stop.
    """
    for v in range(start_version, 0, -1):
        try:
            resp = requests.get(
                f"{config.url}/wiki/rest/api/content/{page_id}",
                headers=headers,
                params={"expand": "version", "status": "historical", "version": v},
                timeout=30,
            )
            resp.raise_for_status()
            version = resp.json().get("version", {})
        except requests.RequestException:
            return None

        when_str = version.get("when", "")
        if not when_str:
            continue
        when = _parse_dt(when_str)
        if when < since:
            return None  # walked past the window; nothing earlier can match

        author = version.get("by") or {}
        if author.get("accountId") in team_account_ids:
            return author.get("displayName") or "unknown", when

    return None


def _page_diff(
    config: AtlassianConfig, headers: dict, page_id: str, version_num: int, since: datetime
) -> Optional[str]:
    """Diff the page's pre-window baseline against its current body.

    If the page was created within the window (no version predates `since`), the
    baseline is treated as empty so the entire page shows up as newly added content.
    """
    baseline_version = None
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
                baseline_version = v
                break
        except requests.RequestException:
            break

    try:
        curr = requests.get(
            f"{config.url}/wiki/rest/api/content/{page_id}",
            headers=headers,
            params={"expand": "body.storage"},
            timeout=30,
        )
        curr.raise_for_status()
        curr_body = curr.json().get("body", {}).get("storage", {}).get("value", "")
    except requests.RequestException:
        return None

    if baseline_version is None:
        prev_body = ""
    else:
        try:
            prev = requests.get(
                f"{config.url}/wiki/rest/api/content/{page_id}",
                headers=headers,
                params={"expand": "body.storage", "status": "historical", "version": baseline_version},
                timeout=30,
            )
            prev.raise_for_status()
            prev_body = prev.json().get("body", {}).get("storage", {}).get("value", "")
        except requests.RequestException:
            return None

    return _compute_diff(_storage_to_text(prev_body), _storage_to_text(curr_body))


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
