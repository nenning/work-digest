"""Team Jira fetcher for management summary mode."""
from __future__ import annotations

import requests
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

from digest.config import AtlassianConfig, MgmtSummaryConfig
from digest.models import SourceItem


def fetch_sprint(
    config: AtlassianConfig,
    auth_header: str,
    board_id: int,
    sprint_name: str,
) -> Tuple[int, datetime, datetime, str]:
    """Find a sprint by name on the given board.

    Pass sprint_name="current" to automatically select the active sprint.
    Returns (sprint_id, start_dt, end_dt, label). Raises ValueError if not found.
    """
    if sprint_name.strip().lower() == "current":
        return _fetch_active_sprint(config, auth_header, board_id)

    headers = {"Authorization": auth_header, "Accept": "application/json"}
    start_at = 0
    while True:
        resp = requests.get(
            f"{config.url}/rest/agile/1.0/board/{board_id}/sprint",
            headers=headers,
            params={"startAt": start_at, "maxResults": 50},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for sprint in data.get("values", []):
            if sprint.get("name", "").strip().lower() == sprint_name.strip().lower():
                start_dt = _parse_dt_optional(sprint.get("startDate"))
                end_dt = _parse_dt_optional(sprint.get("endDate"))
                return sprint["id"], start_dt, end_dt, sprint["name"]
        if data.get("isLast", True):
            break
        start_at += len(data.get("values", []))
    raise ValueError(
        f"Sprint {sprint_name!r} not found on board {board_id}. "
        "Check jira_board_id in config and the sprint name (case-insensitive match)."
    )


def _fetch_active_sprint(
    config: AtlassianConfig,
    auth_header: str,
    board_id: int,
) -> Tuple[int, datetime, datetime, str]:
    """Return the currently active sprint on the given board."""
    headers = {"Authorization": auth_header, "Accept": "application/json"}
    resp = requests.get(
        f"{config.url}/rest/agile/1.0/board/{board_id}/sprint",
        headers=headers,
        params={"state": "active", "maxResults": 1},
        timeout=30,
    )
    resp.raise_for_status()
    values = resp.json().get("values", [])
    if not values:
        raise ValueError(
            f"No active sprint found on board {board_id}. "
            "Use --sprint 'Sprint Name' to target a specific sprint by name."
        )
    sprint = values[0]
    start_dt = _parse_dt_optional(sprint.get("startDate"))
    end_dt = _parse_dt_optional(sprint.get("endDate"))
    return sprint["id"], start_dt, end_dt, sprint["name"]


def fetch_team_tickets(
    config: AtlassianConfig,
    auth_header: str,
    mgmt_cfg: MgmtSummaryConfig,
    since: datetime,
    until: datetime,
    sprint_id: Optional[int] = None,
) -> Tuple[List[SourceItem], Set[str]]:
    """Fetch all team tickets matching the configured JQL.

    Returns (items, team_account_ids) where team_account_ids is the set of
    assignee/reporter accountIds, used to filter Confluence pages.
    """
    since_str = since.strftime("%Y-%m-%d %H:%M")
    until_str = until.strftime("%Y-%m-%d %H:%M")

    if sprint_id is not None:
        jql = f"({mgmt_cfg.jira_jql}) AND sprint = {sprint_id} ORDER BY status ASC, updated DESC"
    else:
        jql = (
            f"({mgmt_cfg.jira_jql}) AND updated >= \"{since_str}\" "
            f"AND updated <= \"{until_str}\" ORDER BY status ASC, updated DESC"
        )

    ignore_names = {u.lower() for u in (mgmt_cfg.ignore_users or [])}
    ignore_types = {t.lower() for t in (mgmt_cfg.ignore_issue_types or [])}

    issues = _paginate_jql(config, auth_header, jql)

    items: List[SourceItem] = []
    team_account_ids: Set[str] = set()

    for issue in issues:
        fields = issue["fields"]
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        if issue_type.lower() in ignore_types:
            continue

        assignee = fields.get("assignee") or {}
        reporter = fields.get("reporter") or {}
        assignee_id = assignee.get("accountId", "")
        reporter_id = reporter.get("accountId", "")
        assignee_name = assignee.get("displayName") or "Unassigned"
        reporter_name = reporter.get("displayName") or ""

        if assignee_name.lower() in ignore_names:
            continue

        if assignee_id:
            team_account_ids.add(assignee_id)
        # Checked independently of the assignee -- a ticket reported (but not
        # assigned) by an ignored bot/tester must not leak that account into
        # team_account_ids, or their Confluence edits resurface via the
        # "contributor in (...)" query in mgmt_confluence.py.
        if reporter_id and reporter_name.lower() not in ignore_names:
            team_account_ids.add(reporter_id)

        status = fields.get("status") or {}
        status_name = status.get("name", "Unknown")
        status_category = (status.get("statusCategory") or {}).get("key", "new")

        if status_category == "done":
            kind = "ticket_done"
        elif status_category == "indeterminate":
            kind = "ticket_wip"
        else:
            kind = "ticket_todo"

        key = issue["key"]
        summary_text = fields.get("summary", "")
        description = _extract_text(fields.get("description") or "")

        content_parts = [f"Status: {status_name}. Assignee: {assignee_name}."]
        if description:
            content_parts.append(description[:300])
        content = " ".join(content_parts)

        updated_str = fields.get("updated") or fields.get("created", "")
        items.append(SourceItem(
            source="jira",
            kind=kind,
            title=f"{key}: {summary_text}",
            url=f"{config.url}/browse/{key}",
            content=content,
            author=assignee_name,
            timestamp=_parse_dt(updated_str) if updated_str else since,
            metadata={
                "status": status_name,
                "status_category": status_category,
                "assignee": assignee_name,
            },
        ))

    return items, team_account_ids


def _paginate_jql(config: AtlassianConfig, auth_header: str, jql: str) -> list:
    # /rest/api/3/search/jql uses cursor-based pagination (nextPageToken), not startAt.
    headers = {
        "Authorization": auth_header,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    fields = ["summary", "assignee", "reporter", "status", "issuetype", "description", "updated", "created"]
    all_issues: list = []
    next_page_token: Optional[str] = None

    while True:
        body: dict = {"jql": jql, "fields": fields, "maxResults": 100}
        if next_page_token:
            body["nextPageToken"] = next_page_token

        resp = requests.post(
            f"{config.url}/rest/api/3/search/jql",
            headers=headers,
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("issues", [])
        all_issues.extend(batch)

        if data.get("isLast", True) or not batch:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return all_issues


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_dt_optional(s: Optional[str]) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    return _parse_dt(s)


def _extract_text(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return " ".join(_extract_text(c) for c in node.get("content", []))
    if isinstance(node, list):
        return " ".join(_extract_text(n) for n in node)
    return ""
