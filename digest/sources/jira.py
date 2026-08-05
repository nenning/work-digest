import re
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from markupsafe import Markup
from digest.config import AtlassianConfig
from digest.models import SourceItem


def _load_field_ignore() -> set:
    path = Path(__file__).parent.parent.parent / "jira_field_ignore.txt"
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {l.strip().lower() for l in lines if l.strip() and not l.startswith("#")}


_IGNORED_FIELDS = _load_field_ignore()

_JIRA_KEY_RE = re.compile(r'\b([A-Z][A-Z0-9]+-\d+)\b')

_BLOCKING_OUTWARD_PHRASES = {"blocks", "has to be done before"}

_REMOTE_LINK_FIELDS = {"remoteworkitemlink", "remoteissuelink"}
_REMOTE_LINK_RE = re.compile(r'links to "([^"]+)"')
_REMOTE_LINK_SUFFIX_RE = re.compile(r'\s*\([^()]*\)$')

_DATE_ONLY_FIELDS = {"duedate"}
_DATE_TIME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$')


def _format_field_value(field: str, value: str) -> str:
    if field.lower() in _DATE_ONLY_FIELDS:
        match = _DATE_TIME_RE.match(value)
        if match:
            return match.group(1)
    return value


def _fetch_issue_summary(config: AtlassianConfig, auth_header: str, key: str, cache: dict) -> str | None:
    if key in cache:
        return cache[key]
    try:
        resp = requests.get(
            f"{config.url}/rest/api/3/issue/{key}",
            headers={"Authorization": auth_header, "Accept": "application/json"},
            params={"fields": "summary"},
            timeout=10,
        )
        if resp.ok:
            cache[key] = resp.json()["fields"]["summary"]
            return cache[key]
    except Exception:
        pass
    cache[key] = None
    return None


def _enrich_keys(value: str, config: AtlassianConfig, auth_header: str, cache: dict) -> str:
    def replace(m):
        summary = _fetch_issue_summary(config, auth_header, m.group(1), cache)
        return f"{m.group(1)} ({summary})" if summary else m.group(1)
    return _JIRA_KEY_RE.sub(replace, value)


def _fetch_remote_links(config: AtlassianConfig, auth_header: str, key: str) -> list[dict]:
    try:
        resp = requests.get(
            f"{config.url}/rest/api/3/issue/{key}/remotelink",
            headers={"Authorization": auth_header, "Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


def _linkify_remote_link(value: str, remote_links: list[dict]):
    match = _REMOTE_LINK_RE.search(value)
    if not match:
        return value
    quoted_title = match.group(1)
    bare_title = _REMOTE_LINK_SUFFIX_RE.sub("", quoted_title).strip()
    remote_url = next(
        (rl["object"]["url"] for rl in remote_links
         if rl.get("object", {}).get("title") in (quoted_title, bare_title)),
        None,
    )
    if not remote_url or not remote_url.lower().startswith(("http://", "https://")):
        return value
    return Markup('"[<a href="{}">{}</a>]"').format(remote_url, bare_title)



def fetch(config: AtlassianConfig, auth_header: str, since: datetime) -> List[SourceItem]:
    since_str = since.astimezone().strftime("%Y-%m-%d %H:%M")
    current_user = _get_current_user(config, auth_header)
    account_id = current_user.get("accountId", "")
    items: List[SourceItem] = []
    items.extend(_fetch_watched(config, auth_header, since, since_str, account_id))
    items.extend(_fetch_new_tickets(config, auth_header, since_str))
    new_ticket_keys = {i.title.split(":")[0] for i in items if i.kind == "new_ticket"}
    return [i for i in items if not (i.kind != "new_ticket" and i.title.split(":")[0] in new_ticket_keys)]


def _get_current_user(config: AtlassianConfig, auth_header: str) -> dict:
    resp = requests.get(
        f"{config.url}/rest/api/3/myself",
        headers={"Authorization": auth_header, "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


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
    """Page through /rest/api/3/search/jql via its cursor (nextPageToken), same as
    mgmt_jira._paginate_jql -- a previous version of this function fetched only the
    first 50 results and warned instead of paginating, silently dropping the rest.
    """
    headers = {
        "Authorization": auth_header,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    fields = ["summary", "assignee", "reporter", "comment", "status", "priority", "updated", "created", "description", "issuelinks"]
    all_issues: list = []
    next_page_token = None

    while True:
        body: dict = {"jql": jql, "fields": fields, "maxResults": 50}
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


def _fetch_issue_changelog(config: AtlassianConfig, auth_header: str, issue_key: str) -> list:
    """Return changelog history entries for a single issue (up to 100)."""
    resp = requests.get(
        f"{config.url}/rest/api/3/issue/{issue_key}/changelog",
        headers={"Authorization": auth_header, "Accept": "application/json"},
        params={"maxResults": 100},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("values", [])


def _fetch_watched(
    config: AtlassianConfig,
    auth_header: str,
    since: datetime,
    since_str: str,
    account_id: str,
) -> List[SourceItem]:
    if not config.jira_projects:
        return []
    _validate_project_keys(config.jira_projects)
    projects = ", ".join(config.jira_projects)
    issues = _jql_search(
        config, auth_header,
        _append_extra(
            f'project in ({projects}) AND watcher = currentUser() AND updated >= "{since_str}"',
            config.jira_jql_extra,
        ),
    )

    since_utc = since.astimezone(timezone.utc)
    items: List[SourceItem] = []
    summary_cache: dict = {}

    for issue in issues:
        key = issue["key"]
        title = f"{key}: {issue['fields']['summary']}"
        url = f"{config.url}/browse/{key}"
        issue["changelog"] = {"histories": _fetch_issue_changelog(config, auth_header, key)}
        candidates = _collect_candidates(issue, since_utc, account_id, title, url, config, auth_header, summary_cache)
        items.extend(_deduplicate(candidates))

    return items


def _collect_candidates(
    issue: dict,
    since_utc: datetime,
    account_id: str,
    title: str,
    url: str,
    config: AtlassianConfig,
    auth_header: str,
    summary_cache: dict,
) -> List[SourceItem]:
    """Collect all candidate SourceItems from comments, description changes, and field changes."""
    candidates: List[SourceItem] = []

    # comments
    for comment in issue["fields"].get("comment", {}).get("comments", []):
        if _parse_dt(comment["updated"]) < since_utc:
            continue
        body = comment.get("body", "")
        author = _display_name(comment.get("author"))
        ts = _parse_dt(comment["updated"])
        text = _extract_text(body)
        if _has_mention(body, account_id):
            candidates.append(SourceItem(
                source="jira", kind="mention",
                title=title, url=url,
                content=text,
                author=author,
                timestamp=ts,
                metadata={"mention_author": author},
            ))
        else:
            candidates.append(SourceItem(
                source="jira", kind="comment",
                title=title, url=url,
                content=text,
                author=author,
                timestamp=ts,
            ))

    # changelog: description changes and other field changes
    changelog_histories = issue.get("changelog", {}).get("histories", [])
    field_changes: list[dict] = []
    desc_item: SourceItem | None = None

    for history in changelog_histories:
        if _parse_dt(history["created"]) < since_utc:
            continue
        history_author = _display_name(history.get("author"))
        history_ts = _parse_dt(history["created"])

        history_field_items: dict[str, list[dict]] = {}
        for change in history.get("items", []):
            field = change.get("field", "")
            if field in ("comment", "Attachment"):
                continue
            if field.lower() in _IGNORED_FIELDS:
                continue
            if field == "description":
                desc_node = issue["fields"].get("description") or ""
                if _has_mention(desc_node, account_id):
                    desc_item = SourceItem(
                        source="jira", kind="mention",
                        title=title, url=url,
                        content=_extract_text(desc_node),
                        author=history_author,
                        timestamp=history_ts,
                        metadata={"mention_author": history_author},
                    )
                else:
                    desc_item = SourceItem(
                        source="jira", kind="description_change",
                        title=title, url=url,
                        content=_extract_text(desc_node),
                        author=history_author,
                        timestamp=history_ts,
                    )
                continue
            history_field_items.setdefault(field, []).append(change)

        for field, field_items in history_field_items.items():
            from_str, to_str = _net_field_change(field_items)
            from_val = _format_field_value(field, from_str or "—")
            to_val = _format_field_value(field, to_str or "—")
            field_changes.append({
                "field": field,
                "from": from_val,
                "to": to_val,
                "author": history_author,
                "ts": history_ts,
            })

    if desc_item is not None:
        candidates.append(desc_item)

    if field_changes:
        net_changes = _merge_field_changes(field_changes)
        if net_changes:
            status_category = (issue["fields"].get("status") or {}).get("statusCategory", {}).get("key", "")
            if status_category == "done":
                for c in net_changes:
                    if c["field"] == "status":
                        unblocks = _find_unblocked_tickets(issue, config)
                        if unblocks:
                            c["unblocks"] = unblocks
            latest_ts = max(c["ts"] for c in net_changes)
            latest_author = next(c["author"] for c in net_changes if c["ts"] == latest_ts)
            remote_links = None
            enriched = []
            for c in net_changes:
                new_from = _enrich_keys(c["from"], config, auth_header, summary_cache)
                new_to = _enrich_keys(c["to"], config, auth_header, summary_cache)
                if c["field"].lower() in _REMOTE_LINK_FIELDS:
                    if remote_links is None:
                        remote_links = _fetch_remote_links(config, auth_header, issue["key"])
                    new_from = _linkify_remote_link(new_from, remote_links)
                    new_to = _linkify_remote_link(new_to, remote_links)
                enriched.append({**c, "from": new_from, "to": new_to})
            content = "; ".join(f"{c['field']}: {c['from']} → {c['to']}" for c in enriched)
            candidates.append(SourceItem(
                source="jira", kind="field_change",
                title=title, url=url,
                content=content,
                author=latest_author,
                timestamp=latest_ts,
                metadata={"changes": enriched},
            ))

    return candidates


def _find_unblocked_tickets(issue: dict, config: AtlassianConfig) -> list[dict]:
    """Return {"key", "title", "url"} for tickets this issue blocks, excluding tickets already Done."""
    result = []
    for link in issue["fields"].get("issuelinks", []):
        outward_phrase = (link.get("type", {}).get("outward") or "").strip().lower()
        if outward_phrase not in _BLOCKING_OUTWARD_PHRASES:
            continue
        target = link.get("outwardIssue")
        if not target:
            continue
        target_fields = target.get("fields", {})
        target_category = (target_fields.get("status") or {}).get("statusCategory", {}).get("key", "")
        if target_category == "done":
            continue
        key = target.get("key")
        title = target_fields.get("summary")
        if not key or not title:
            continue
        result.append({"key": key, "title": title, "url": f"{config.url}/browse/{key}"})
    return result


def _net_field_change(changes: list[dict]) -> tuple[str | None, str | None]:
    """Combine changelog items for one field within a single history entry into one net from/to.

    Multi-value fields (e.g. fixVersions) log a value swap as two separate items in the same
    history: a removal (toString empty) and an addition (fromString empty). Left unpaired, a
    chronological merge across histories can pick up a stray removal fragment as the "final"
    state instead of the value actually left in place (see EGOV-595: repeated Fix Version swaps
    ending back at the original value showed as "PI 3 → —").
    """
    if len(changes) == 1:
        return changes[0].get("fromString"), changes[0].get("toString")
    removed = [c["fromString"] for c in changes if c.get("fromString") and not c.get("toString")]
    added = [c["toString"] for c in changes if c.get("toString") and not c.get("fromString")]
    return (", ".join(removed) or None), (", ".join(added) or None)


def _merge_field_changes(changes: list[dict]) -> list[dict]:
    """Per field: keep initial state (earliest from) and final state (latest to). Drop no-ops."""
    by_field: dict[str, list[dict]] = {}
    for c in changes:
        by_field.setdefault(c["field"], []).append(c)

    result = []
    for entries in by_field.values():
        entries.sort(key=lambda x: x["ts"])
        first_from = entries[0]["from"]
        last = entries[-1]
        if first_from != last["to"]:
            result.append({**last, "from": first_from})
    return result


def _merge_comment_tier(items: List[SourceItem]) -> SourceItem:
    """Merge multiple comment/description_change items for one ticket into a single item."""
    labels = {"comment": "Kommentar", "description_change": "Beschreibung geändert"}
    ordered = sorted(items, key=lambda i: i.timestamp)
    parts = [f"[{labels.get(i.kind, i.kind)} von {i.author}] {i.content}" for i in ordered]
    latest = ordered[-1]
    return SourceItem(
        source="jira", kind="comment",
        title=latest.title, url=latest.url,
        content="\n\n".join(parts),
        author=latest.author,
        timestamp=latest.timestamp,
    )


def _merge_mention_tier(items: List[SourceItem]) -> SourceItem:
    """Merge multiple mention items for one ticket into a single item."""
    ordered = sorted(items, key=lambda i: i.timestamp)
    parts = [f"[Erwähnt von {i.author}] {i.content}" for i in ordered]
    latest = ordered[-1]
    authors = list(dict.fromkeys(i.author for i in ordered))
    return SourceItem(
        source="jira", kind="mention",
        title=latest.title, url=latest.url,
        content="\n\n".join(parts),
        author=latest.author,
        timestamp=latest.timestamp,
        metadata={"mention_authors": authors},
    )


def _deduplicate(candidates: List[SourceItem]) -> List[SourceItem]:
    """Keep only the highest-priority tier: mentions > comments/descriptions > field changes.
    If the winning tier has more than one item, merge them into a single combined item.
    """
    mentions = [i for i in candidates if i.kind == "mention"]
    if mentions:
        return [_merge_mention_tier(mentions)] if len(mentions) > 1 else mentions
    comments = [i for i in candidates if i.kind in ("comment", "description_change")]
    if comments:
        return [_merge_comment_tier(comments)] if len(comments) > 1 else comments
    return [i for i in candidates if i.kind == "field_change"]


def _has_mention(node, account_id: str) -> bool:
    """Return True if the ADF node tree contains a mention of account_id."""
    if not account_id:
        return False
    if isinstance(node, dict):
        if node.get("type") == "mention" and node.get("attrs", {}).get("id") == account_id:
            return True
        return any(_has_mention(c, account_id) for c in node.get("content", []))
    if isinstance(node, list):
        return any(_has_mention(n, account_id) for n in node)
    return False


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
