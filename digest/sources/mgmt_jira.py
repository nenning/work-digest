"""Team Jira fetcher for management summary mode."""
from __future__ import annotations

import dataclasses
import requests
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple
from urllib.parse import quote

from digest.config import AtlassianConfig, MgmtSummaryConfig, ProjectConfig
from digest.models import SourceItem


def resolve_project_mgmt_config(project: ProjectConfig, global_cfg: MgmtSummaryConfig) -> MgmtSummaryConfig:
    """Build the effective per-project management-summary config.

    The project's own Jira key plus its optional extra filter are AND'd onto the
    shared/global base JQL ("the top jql"). ignore_users/ignore_issue_types stay
    the shared global values -- there's no per-project override for those.
    jira_board_id and recipient are read directly off the project/global config
    where needed (sprint lookup, email delivery), not through this object.
    """
    parts = [f"project = {project.jira.project}"]
    if global_cfg.jira_jql:
        parts.append(f"({global_cfg.jira_jql})")
    if project.mgmt_summary and project.mgmt_summary.jira_jql_extra:
        parts.append(f"({project.mgmt_summary.jira_jql_extra})")
    return dataclasses.replace(global_cfg, jira_jql=" AND ".join(parts))


def fetch_sprint(
    config: AtlassianConfig,
    auth_header: str,
    board_id: int,
    sprint_name: str,
) -> Tuple[int, datetime, datetime, str]:
    """Find a sprint by name or numeric id on the given board.

    Pass sprint_name="current" to automatically select the active sprint.
    Returns (sprint_id, start_dt, end_dt, label). Raises ValueError if not found.
    """
    wanted = sprint_name.strip().lower()
    if wanted == "current":
        return _fetch_active_sprint(config, auth_header, board_id)

    headers = {"Authorization": auth_header, "Accept": "application/json"}
    start_at = 0
    while True:
        resp = requests.get(
            f"{config.jira_api_base}/rest/agile/1.0/board/{board_id}/sprint",
            headers=headers,
            params={"startAt": start_at, "maxResults": 50},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for sprint in data.get("values", []):
            if sprint.get("name", "").strip().lower() == wanted or str(sprint.get("id")) == wanted:
                start_dt = _parse_dt_optional(sprint.get("startDate"))
                end_dt = _parse_dt_optional(sprint.get("endDate"))
                return sprint["id"], start_dt, end_dt, sprint["name"]
        if data.get("isLast", True):
            break
        start_at += len(data.get("values", []))
    raise ValueError(
        f"Sprint {sprint_name!r} not found on board {board_id}. "
        "Check jira_board_id in config and the sprint name (case-insensitive match) or id."
    )


def parse_sprint_specs(value: str) -> List[str]:
    """Split a comma-separated --sprint value into individual names/ids."""
    return [s.strip() for s in value.split(",") if s.strip()]


def fetch_sprints(
    config: AtlassianConfig,
    auth_header: str,
    board_id: int,
    specs: List[str],
) -> Tuple[List[int], datetime, datetime, List[str]]:
    """Resolve several sprints (names, ids or "current") into one combined window.

    Returns (sprint_ids, earliest_start, latest_end, names), in the order given and
    without duplicates. Raises ValueError if any sprint is not found.
    """
    resolved: dict[int, Tuple[datetime, datetime, str]] = {}
    for spec in specs:
        sid, start_dt, end_dt, name = fetch_sprint(config, auth_header, board_id, spec)
        resolved.setdefault(sid, (start_dt, end_dt, name))
    return (
        list(resolved),
        min(s for s, _, _ in resolved.values()),
        max(e for _, e, _ in resolved.values()),
        [n for _, _, n in resolved.values()],
    )


def _fetch_active_sprint(
    config: AtlassianConfig,
    auth_header: str,
    board_id: int,
) -> Tuple[int, datetime, datetime, str]:
    """Return the currently active sprint on the given board."""
    headers = {"Authorization": auth_header, "Accept": "application/json"}
    resp = requests.get(
        f"{config.jira_api_base}/rest/agile/1.0/board/{board_id}/sprint",
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
    sprint_ids: Optional[List[int]] = None,
    combine_subtasks: bool = False,
) -> Tuple[List[SourceItem], Set[str]]:
    """Fetch all team tickets matching the configured JQL.

    Returns (items, team_account_ids) where team_account_ids is the set of
    assignee/reporter accountIds, used to filter Confluence pages.

    With combine_subtasks, sub-tasks are not returned as items of their own: each is
    attached to its parent's item as metadata["subtasks"]. A parent outside the result
    set is fetched by key; a sub-task whose parent can't be found (or is ignored) stays
    a standalone item.
    """
    since_str = since.strftime("%Y-%m-%d %H:%M")
    until_str = until.strftime("%Y-%m-%d %H:%M")

    if sprint_ids:
        id_list = ", ".join(str(i) for i in sprint_ids)
        jql = f"({mgmt_cfg.jira_jql}) AND sprint in ({id_list}) ORDER BY status ASC, updated DESC"
    else:
        jql = (
            f"({mgmt_cfg.jira_jql}) AND updated >= \"{since_str}\" "
            f"AND updated <= \"{until_str}\" ORDER BY status ASC, updated DESC"
        )

    ignore_names = {u.lower() for u in (mgmt_cfg.ignore_users or [])}
    ignore_types = {t.lower() for t in (mgmt_cfg.ignore_issue_types or [])}

    extra_fields = ["parent"] if combine_subtasks else []
    issues = _paginate_jql(config, auth_header, jql, extra_fields)

    team_account_ids: Set[str] = set()

    def _parse(issue: dict) -> Optional[SourceItem]:
        fields = issue["fields"]
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        if issue_type.lower() in ignore_types:
            return None

        assignee = fields.get("assignee") or {}
        reporter = fields.get("reporter") or {}
        assignee_id = assignee.get("accountId", "")
        reporter_id = reporter.get("accountId", "")
        assignee_name = assignee.get("displayName") or "Unassigned"
        reporter_name = reporter.get("displayName") or ""

        if assignee_name.lower() in ignore_names:
            return None

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
        return SourceItem(
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
                "key": key,
                "is_subtask": bool((fields.get("issuetype") or {}).get("subtask")),
            },
        )

    parsed = [(issue, _parse(issue)) for issue in issues]

    if not combine_subtasks:
        return [item for _, item in parsed if item is not None], team_account_ids

    def _subtask_parent(issue: dict) -> Optional[str]:
        fields = issue["fields"]
        if (fields.get("issuetype") or {}).get("subtask"):
            return (fields.get("parent") or {}).get("key")
        return None

    parents: dict = {
        issue["key"]: item for issue, item in parsed
        if item is not None and _subtask_parent(issue) is None
    }
    missing = sorted({
        pk for issue, item in parsed
        if item is not None and (pk := _subtask_parent(issue)) and pk not in parents
    })
    fetched_parents: List[SourceItem] = []
    if missing:
        for issue in _paginate_jql(config, auth_header, f"key in ({', '.join(missing)})"):
            item = _parse(issue)
            if item is not None:
                parents[issue["key"]] = item
                fetched_parents.append(item)

    items: List[SourceItem] = []
    for issue, item in parsed:
        if item is None:
            continue
        parent = parents.get(_subtask_parent(issue) or "")
        if parent is None:
            items.append(item)
            continue
        parent.metadata.setdefault("subtasks", []).append({
            "title": item.title,
            "status": item.metadata["status"],
            "assignee": item.metadata["assignee"],
            "description": _extract_text(issue["fields"].get("description") or "")[:300],
        })
    return items + fetched_parents, team_account_ids


def build_ticket_queries(
    jira_items: List[SourceItem], base_url: str, assume_done: bool = False
) -> List[Tuple[str, str, str]]:
    """Return (label, jql, url) key-list queries over the non-sub-task tickets.

    Two queries (done / still open), or a single one with assume_done since every
    ticket then counts as done. Empty groups are omitted.
    """
    tickets = [i for i in jira_items if not i.metadata.get("is_subtask") and i.metadata.get("key")]
    groups = [("All tickets", tickets)] if assume_done else [
        ("Done tickets", [i for i in tickets if i.kind == "ticket_done"]),
        ("Open tickets", [i for i in tickets if i.kind != "ticket_done"]),
    ]
    queries = []
    for label, group in groups:
        if not group:
            continue
        jql = f"key in ({', '.join(i.metadata['key'] for i in group)})"
        queries.append((label, jql, f"{base_url}/issues/?jql={quote(jql)}"))
    return queries


def _paginate_jql(
    config: AtlassianConfig, auth_header: str, jql: str, extra_fields: Optional[List[str]] = None
) -> list:
    # /rest/api/3/search/jql uses cursor-based pagination (nextPageToken), not startAt.
    headers = {
        "Authorization": auth_header,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    fields = ["summary", "assignee", "reporter", "status", "issuetype", "description", "updated", "created"]
    fields += extra_fields or []
    all_issues: list = []
    next_page_token: Optional[str] = None

    while True:
        body: dict = {"jql": jql, "fields": fields, "maxResults": 100}
        if next_page_token:
            body["nextPageToken"] = next_page_token

        resp = requests.post(
            f"{config.jira_api_base}/rest/api/3/search/jql",
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
