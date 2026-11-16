"""Jira candidate search for the page-watch trace pass."""
import re
from datetime import date
from typing import List, Optional

from digest.config import AtlassianConfig, RetryConfig
from digest.spec_monitor.http import RequestFailed, request_with_retry
from digest.spec_monitor.models import TraceCandidate, TraceSourceError

OWN_LABEL_PREFIX = "pagewatch-"

_FIELDS = ["summary", "status", "updated", "labels"]
_LUCENE_SPECIAL = re.compile(r"[*?:()\[\]{}^~!&|+\-/]")
_PAGE_SIZE = 50


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clean_term(term: str) -> str:
    return " ".join(_LUCENE_SPECIAL.sub(" ", term).split())


def build_jql(terms: List[str], projects: List[str], since: date) -> str:
    cleaned = [t for t in (_clean_term(t) for t in terms) if t]
    if not cleaned:
        raise ValueError("no usable search terms")
    if not projects:
        raise ValueError("no Jira projects to search")
    text = " OR ".join(f"text ~ {_quote(t)}" for t in cleaned)
    keys = ", ".join(_quote(p) for p in projects)
    return f'project in ({keys}) AND updated >= "{since.isoformat()}" AND ({text}) ORDER BY updated DESC'


def _search(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, jql: str, limit: int) -> List[dict]:
    url = f"{atlassian.jira_api_base.rstrip('/')}/rest/api/3/search/jql"
    headers = {"Authorization": auth_header, "Accept": "application/json", "Content-Type": "application/json"}
    issues: List[dict] = []
    token: Optional[str] = None
    while len(issues) < limit:
        body = {"jql": jql, "fields": _FIELDS, "maxResults": min(_PAGE_SIZE, limit - len(issues))}
        if token:
            body["nextPageToken"] = token
        try:
            resp = request_with_retry("POST", url, retry=retry, headers=headers, json=body)
        except RequestFailed as exc:
            raise TraceSourceError(f"Jira search failed: {exc}") from exc
        if not resp.ok:
            raise TraceSourceError(f"Jira search: HTTP {resp.status_code} {(resp.text or '')[:200]}".strip())
        data = resp.json()
        issues.extend(data.get("issues") or [])
        token = data.get("nextPageToken")
        if data.get("isLast", not token) or not token:
            break
    return issues[:limit]


def search_issues(
    atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig,
    terms: List[str], projects: List[str], since: date, limit: int = 15,
) -> List[TraceCandidate]:
    try:
        jql = build_jql(terms, projects, since)
    except ValueError as exc:
        raise TraceSourceError(f"Jira search not possible: {exc}") from exc
    base = atlassian.url.rstrip("/")
    out = []
    for item in _search(atlassian, auth_header, retry, jql, limit):
        f = item.get("fields") or {}
        labels = f.get("labels") or []
        out.append(TraceCandidate(
            kind="jira", ref=item["key"], label=f.get("summary") or item["key"],
            url=f"{base}/browse/{item['key']}",
            detail=f"updated {str(f.get('updated') or '')[:10]}".strip(),
            status=(f.get("status") or {}).get("name", ""),
            own=any(str(l).startswith(OWN_LABEL_PREFIX) for l in labels),
        ))
    return out


def find_ticket_by_label(
    atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, label: str,
) -> Optional[str]:
    issues = _search(atlassian, auth_header, retry, f"labels = {_quote(label)}", 1)
    return issues[0]["key"] if issues else None
