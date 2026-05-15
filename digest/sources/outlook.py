import requests
from datetime import datetime, timezone
from typing import List
from digest.models import SourceItem

GRAPH = "https://graph.microsoft.com/v1.0"


def fetch(token: str, since: datetime) -> List[SourceItem]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"{GRAPH}/me/mailFolders/inbox/messages"
        f"?$filter=receivedDateTime ge {since_iso}"
        f"&$orderby=receivedDateTime desc&$top=50"
        f"&$select=id,subject,from,receivedDateTime,bodyPreview,webLink,isRead"
    )
    items = []
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for msg in data.get("value", []):
            items.append(SourceItem(
                source="outlook", kind="email",
                title=msg.get("subject") or "(no subject)",
                url=msg.get("webLink", "https://outlook.office.com"),
                content=_build_content(msg),
                author=msg.get("from", {}).get("emailAddress", {}).get("name") or "unknown",
                timestamp=_parse_dt(msg["receivedDateTime"]),
                priority="info",  # LLM will classify in summarizer
            ))
        next_url = data.get("@odata.nextLink")
        url = next_url if next_url and next_url.startswith("https://graph.microsoft.com/") else None
    return items


def _build_content(msg: dict) -> str:
    sender = msg.get("from", {}).get("emailAddress", {})
    return (
        f"From: {sender.get('name', 'unknown')} <{sender.get('address', '')}>\n"
        f"Subject: {msg.get('subject', '(no subject)')}\n"
        f"Preview: {msg.get('bodyPreview', '')}"
    )


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
