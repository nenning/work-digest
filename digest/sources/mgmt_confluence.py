"""Team Confluence fetcher for management summary mode."""
from __future__ import annotations

import warnings
import requests
from datetime import datetime, timezone
from typing import List, Set

from digest.config import AtlassianConfig, MgmtSummaryConfig
from digest.models import SourceItem


def fetch_team_pages(
    config: AtlassianConfig,
    auth_header: str,
    mgmt_cfg: MgmtSummaryConfig,
    since: datetime,
    until: datetime,
    team_account_ids: Set[str],
) -> List[SourceItem]:
    """Fetch Confluence pages last modified by team members within the time range.

    team_account_ids comes from the Jira ticket assignees/reporters, so only users
    who actually worked on team tickets are included.
    """
    if not team_account_ids:
        return []

    since_cql = since.strftime("%Y-%m-%d %H:%M")
    until_cql = until.strftime("%Y-%m-%d %H:%M")

    ids_str = ", ".join(f'"{aid}"' for aid in sorted(team_account_ids))
    cql = (
        f"lastModifier in ({ids_str}) "
        f"AND lastModified >= \"{since_cql}\" "
        f"AND lastModified <= \"{until_cql}\" "
        f"AND type = page"
    )

    headers = {"Authorization": auth_header, "Accept": "application/json"}
    resp = requests.get(
        f"{config.url}/wiki/rest/api/content/search",
        headers=headers,
        params={"cql": cql, "expand": "version", "limit": 50},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    total = data.get("totalSize", 0)
    if len(results) >= 50 and total > 50:
        warnings.warn(
            f"Confluence team pages query returned 50+ results (total={total}); "
            "some pages may be missing.",
            RuntimeWarning,
            stacklevel=2,
        )

    items: List[SourceItem] = []
    for r in results:
        version = r.get("version") or {}
        author_info = version.get("by") or {}
        author_name = author_info.get("displayName") or "unknown"
        timestamp_str = version.get("when", "")
        timestamp = _parse_dt(timestamp_str) if timestamp_str else since

        items.append(SourceItem(
            source="confluence",
            kind="page_update",
            title=r.get("title", "Untitled"),
            url=f"{config.url}/wiki{r.get('_links', {}).get('webui', '')}",
            content=f"Page updated by {author_name}.",
            author=author_name,
            timestamp=timestamp,
        ))

    return items


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
