import re
import requests
import warnings
from datetime import datetime, timezone
from typing import List
from digest.config import AtlassianConfig
from digest.models import SourceItem


def fetch(config: AtlassianConfig, auth_header: str, since: datetime) -> List[SourceItem]:
    since_str = since.strftime("%Y-%m-%d %H:%M")
    items: List[SourceItem] = []
    items.extend(_fetch_assigned(config, auth_header, since_str))
    items.extend(_fetch_comments(config, auth_header, since, since_str))
    items.extend(_fetch_new_tickets(config, auth_header, since_str))
    return items


def _append_extra(jql: str, extra: str | None) -> str:
    if not extra:
        return jql
    upper = jql.upper()
    if "ORDER BY" in upper:
        idx = upper.index("ORDER BY")
        return jql[:idx].rstrip() + f" AND {extra} " + jql[idx:]
    return jql + f" AND {extra}"


def _validate_project_keys(keys: list[str]) -> None:
    for key in keys:
        if not re.match(r'^[A-Z][A-Z0-9]+$', key):
            raise ValueError(f"Invalid Jira project key: {key!r}. Keys must match [A-Z][A-Z0-9]+")


def _jql_search(config: AtlassianConfig, auth_header: str, jql: str) -> list:
    # /rest/api/3/search (GET) was removed — use the POST /search/jql endpoint instead.
    resp = requests.post(
        f"{config.url}/rest/api/3/search/jql",
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={
            "jql": jql,
            "fields": ["summary", "assignee", "reporter", "comment", "status", "priority", "updated", "created", "description"],
            "maxResults": 50,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    issues = data.get("issues", [])
    if len(issues) >= 50 and data.get("total", 0) > 50:
        warnings.warn(
            f"Jira query returned {data['total']} results but only 50 were fetched. "
            "Some items may be missing from the digest.",
            RuntimeWarning,
            stacklevel=3,
        )
    return issues


def _fetch_assigned(config, auth_header, since_str) -> List[SourceItem]:
    if not config.jira_projects:
        return []
    _validate_project_keys(config.jira_projects)
    projects = ", ".join(config.jira_projects)
    issues = _jql_search(
        config, auth_header,
        _append_extra(
            f'project in ({projects}) AND assignee = currentUser() AND updated >= "{since_str}"',
            config.jira_jql_extra,
        ),
    )
    return [
        SourceItem(
            source="jira", kind="assignment",
            title=f"{i['key']}: {i['fields']['summary']}",
            url=f"{config.url}/browse/{i['key']}",
            content=f"Ticket {i['key']}: {i['fields']['summary']}. Status: {i['fields']['status']['name']}.",
            author=_display_name(i["fields"].get("reporter")),
            timestamp=_parse_dt(i["fields"]["updated"]),
        )
        for i in issues
    ]


def _fetch_comments(config, auth_header, since: datetime, since_str: str) -> List[SourceItem]:
    if not config.jira_projects:
        return []
    _validate_project_keys(config.jira_projects)
    projects = ", ".join(config.jira_projects)
    issues = _jql_search(
        config, auth_header,
        _append_extra(
            f'project in ({projects}) AND updated >= "{since_str}" AND '
            f'(assignee = currentUser() OR reporter = currentUser())',
            config.jira_jql_extra,
        ),
    )
    items = []
    for issue in issues:
        for comment in issue["fields"].get("comment", {}).get("comments", []):
            if _parse_dt(comment["updated"]) < since.astimezone(timezone.utc):
                continue
            items.append(SourceItem(
                source="jira", kind="comment",
                title=f"Comment on {issue['key']}: {issue['fields']['summary']}",
                url=f"{config.url}/browse/{issue['key']}",
                content=_extract_text(comment.get("body", "")),
                author=_display_name(comment.get("author")),
                timestamp=_parse_dt(comment["updated"]),
            ))
    return items


def _fetch_new_tickets(config, auth_header, since_str) -> List[SourceItem]:
    if not config.jira_projects:
        return []
    _validate_project_keys(config.jira_projects)
    projects = ", ".join(config.jira_projects)
    issues = _jql_search(
        config, auth_header,
        _append_extra(
            f'project in ({projects}) AND created >= "{since_str}" ORDER BY created DESC',
            config.jira_jql_extra,
        ),
    )
    return [
        SourceItem(
            source="jira", kind="new_ticket",
            title=f"{i['key']}: {i['fields']['summary']}",
            url=f"{config.url}/browse/{i['key']}",
            content=f"Reporter: {_display_name(i['fields'].get('reporter'))}. Assignee: {_display_name(i['fields'].get('assignee'))}.",
            author=_display_name(i["fields"].get("reporter")),
            timestamp=_parse_dt(i["fields"]["created"]),
            metadata={
                "assignee": _display_name(i["fields"].get("assignee")),
                "description": _extract_text(i["fields"].get("description") or ""),
            },
        )
        for i in issues
    ]


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _display_name(user: dict | None) -> str:
    if not user:
        return "unknown"
    return user.get("displayName") or "unknown"


def _extract_text(node) -> str:
    """Recursively extract plain text from Atlassian Document Format or plain string."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return " ".join(_extract_text(c) for c in node.get("content", []))
    if isinstance(node, list):
        return " ".join(_extract_text(n) for n in node)
    return ""
