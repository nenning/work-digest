"""Team Confluence fetcher for management summary mode."""
from __future__ import annotations

import concurrent.futures
import logging
import requests
import time
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

from digest.config import AtlassianConfig, MgmtSummaryConfig
from digest.models import SourceItem
from digest.sources.confluence import _compute_diff, _storage_to_text

log = logging.getLogger(__name__)

_PAGE_SIZE = 50
_REQUEST_TIMEOUT = 60


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

    ignore_names = {u.lower() for u in (mgmt_cfg.ignore_users or [])}

    since_cql = since.strftime("%Y-%m-%d %H:%M")
    until_cql = until.strftime("%Y-%m-%d %H:%M")

    ids_str = ", ".join(f'"{aid}"' for aid in sorted(team_account_ids))
    # "contributor" matches anyone who has ever edited the page, not just the current
    # version's author. This is combined with a version-history walk below so a team
    # member's edit isn't missed just because someone else edited the page again
    # later in the same window ("lastModifier in (...)" alone would miss that case).
    #
    # "contributor in (...)" alone is an expensive, largely unindexed CQL clause --
    # without a space restriction it scans every page in the whole instance before
    # narrowing by contributor, which stays fast for a narrow sprint window but can
    # take minutes (looking like a hang) for a wider --since range. Scope it to the
    # configured spaces, same as the personal digest's confluence.py.
    cql = (
        f"contributor in ({ids_str}) "
        f"AND lastModified >= \"{since_cql}\" "
        f"AND lastModified <= \"{until_cql}\" "
        f"AND type = page"
    )
    if config.confluence_spaces:
        spaces = " OR ".join(f'space = "{s}"' for s in config.confluence_spaces)
        cql = f"({spaces}) AND {cql}"

    headers = {"Authorization": auth_header, "Accept": "application/json"}
    log.debug("Confluence team-page CQL: %s", cql)
    t0 = time.monotonic()
    results = _search_all(config, headers, cql)
    log.debug("Confluence search returned %d result(s) in %.1fs", len(results), time.monotonic() - t0)
    if not results:
        return []

    def _resolve(r: dict) -> Optional[SourceItem]:
        page_id = r["id"]
        title = r.get("title", "Untitled")
        t_page = time.monotonic()
        log.debug("Resolving page %s (%s)...", page_id, title)
        current_version = r.get("version") or {}
        version_num = current_version.get("number", 1)

        authors, baseline_version, timestamp = _walk_version_history(
            config, headers, page_id, current_version, version_num, since, team_account_ids, ignore_names
        )
        if not authors:
            # contributor matched somewhere in the page's history, but not within
            # the window (or only by an ignored user) -> not team activity for this period.
            log.debug("Page %s (%s): no team edit in window (%.1fs)", page_id, title, time.monotonic() - t_page)
            return None

        # Diff the CQL-verified version (lastModified <= until) against the pre-window
        # baseline (or against "" for a page created within the window, so the whole
        # page reads as newly added content) -- NOT the page's current live body, which
        # could include edits made after `until` for a report covering a past window
        # (e.g. --sprint or --to). Reuses the personal digest's diff logic so downstream
        # summarize_items() produces the same kind of 1-2 sentence LLM summary.
        diff = _diff_against_baseline(config, headers, page_id, version_num, baseline_version)
        if diff is None:
            log.debug("Page %s (%s): no diff / body fetch failed (%.1fs)", page_id, title, time.monotonic() - t_page)
            return None  # cosmetic-only change, or body fetch failed

        log.debug("Page %s (%s): resolved in %.1fs", page_id, title, time.monotonic() - t_page)
        return SourceItem(
            source="confluence",
            kind="page_update",
            title=r.get("title", "Untitled"),
            url=f"{config.url}/wiki{r.get('_links', {}).get('webui', '')}",
            content=diff,
            author=", ".join(authors),
            timestamp=timestamp,
        )

    items: List[SourceItem] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(results), 8)) as executor:
        futures = [executor.submit(_resolve, r) for r in results]
        for future in concurrent.futures.as_completed(futures):
            try:
                item = future.result()
            except requests.RequestException:
                # e.g. a page we lost access to between search and fetch -- skip it
                # rather than losing every other page's result in the same batch.
                continue
            if item is not None:
                items.append(item)

    return items


_MAX_SEARCH_PAGES = 200  # safety net: 200 * _PAGE_SIZE = 10,000 results


def _search_all(config: AtlassianConfig, headers: dict, cql: str) -> List[dict]:
    """Page through CQL search results so matches past the first batch aren't dropped.

    Confluence Cloud's CQL search paginates via an opaque cursor embedded in
    `_links.next`, not by incrementing `start` -- verified against a live instance
    that start=0/50/100 all return the *same* first page once a cursor is in play.
    Manually incrementing `start` therefore never reaches a short/empty page and
    loops forever, which is what caused management summaries to hang. Follow
    `_links.next` instead, which is absent once there are no more results.
    """
    results: List[dict] = []
    url = f"{config.url}/wiki/rest/api/content/search"
    params = {"cql": cql, "expand": "version", "limit": _PAGE_SIZE}
    for _ in range(_MAX_SEARCH_PAGES):
        resp = requests.get(url, headers=headers, params=params, timeout=_REQUEST_TIMEOUT)
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


def _walk_version_history(
    config: AtlassianConfig,
    headers: dict,
    page_id: str,
    current_version: dict,
    version_num: int,
    since: datetime,
    team_account_ids: Set[str],
    ignore_names: Set[str],
) -> Tuple[List[str], Optional[int], Optional[datetime]]:
    """Walk a page's version history backward from `version_num`, collecting every
    team-authored edit after `since` and locating the most recent version at or
    before `since` to use as the diff baseline.

    The CQL search already guarantees `current_version`'s timestamp is <= until, and
    every earlier version is necessarily earlier still, so nothing here needs an
    upper-bound check -- only the `since` boundary matters.

    A page with several team members editing it within the window previously
    surfaced only whichever one this walk happened to hit first; now every distinct
    team author in-window is collected so the digest can credit all of them.

    Note: this makes one HTTP request per version walked, sequentially. A page
    edited many times within the window means many sequential round-trips here --
    with --verbose this shows up as a long run of "checking version N" log lines
    for a single page_id, which looks like a hang but is actually just a busy page.

    Returns (sorted distinct author display names, baseline version number or None
    if the page was created within the window, latest team-edit timestamp or None).
    """
    team_authors: dict[str, datetime] = {}

    def _consider(version: dict) -> None:
        author = version.get("by") or {}
        when_str = version.get("when", "")
        when = _parse_dt(when_str) if when_str else None
        name = author.get("displayName") or "unknown"
        if when is not None and author.get("accountId") in team_account_ids and name.lower() not in ignore_names:
            if name not in team_authors or when > team_authors[name]:
                team_authors[name] = when

    _consider(current_version)

    baseline_version: Optional[int] = None
    for v in range(version_num - 1, 0, -1):
        log.debug("Page %s: checking version %d", page_id, v)
        try:
            resp = requests.get(
                f"{config.url}/wiki/rest/api/content/{page_id}",
                headers=headers,
                params={"expand": "version", "status": "historical", "version": v},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            version = resp.json().get("version", {})
        except requests.RequestException:
            break

        when_str = version.get("when", "")
        when = _parse_dt(when_str) if when_str else None
        if when is not None and when <= since:
            baseline_version = v
            break

        _consider(version)

    latest_timestamp = max(team_authors.values()) if team_authors else None
    return sorted(team_authors), baseline_version, latest_timestamp


def _diff_against_baseline(
    config: AtlassianConfig, headers: dict, page_id: str, version_num: int, baseline_version: Optional[int]
) -> Optional[str]:
    """Diff the page's pre-window baseline against its state at `version_num`.

    `version_num` is the CQL-verified version (lastModified <= until), fetched
    explicitly by number rather than as the page's current live body -- otherwise a
    report covering a past window (e.g. --sprint or --to) would pick up edits made
    after `until` but before the report actually ran. If the page was created within
    the window (no version predates `since`), the baseline is treated as empty so
    the entire page shows up as newly added content.
    """
    try:
        curr = requests.get(
            f"{config.url}/wiki/rest/api/content/{page_id}",
            headers=headers,
            params={"expand": "body.storage", "status": "historical", "version": version_num},
            timeout=_REQUEST_TIMEOUT,
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
                timeout=_REQUEST_TIMEOUT,
            )
            prev.raise_for_status()
            prev_body = prev.json().get("body", {}).get("storage", {}).get("value", "")
        except requests.RequestException:
            return None

    return _compute_diff(_storage_to_text(prev_body), _storage_to_text(curr_body))


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
