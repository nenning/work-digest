import concurrent.futures
import html
import logging
import re
import requests
from datetime import datetime, timezone
from typing import List
from digest.models import SourceItem

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"


def fetch(token: str, since: datetime) -> List[SourceItem]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    items: List[SourceItem] = []
    items.extend(_fetch_channel_messages(headers, since))
    items.extend(_fetch_chat_messages(headers, since))
    return items


def _graph_get(headers: dict, url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _get_all_pages(headers: dict, url: str, params: dict | None = None) -> list:
    results = []
    data = _graph_get(headers, url, params)
    results.extend(data.get("value", []))
    while "@odata.nextLink" in data:
        data = _graph_get(headers, data["@odata.nextLink"])
        results.extend(data.get("value", []))
    return results


def _fetch_channel_messages(headers: dict, since: datetime) -> List[SourceItem]:
    teams = _get_all_pages(headers, f"{GRAPH}/me/joinedTeams")
    if not teams:
        return []
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fetch_team(team: dict) -> List[SourceItem]:
        team_items = []
        channels = _get_all_pages(headers, f"{GRAPH}/teams/{team['id']}/channels")
        for channel in channels:
            messages = _get_all_pages(
                headers,
                f"{GRAPH}/teams/{team['id']}/channels/{channel['id']}/messages",
                params={"$filter": f"createdDateTime ge {since_iso}", "$top": "50"},
            )
            for msg in messages:
                body = msg.get("body", {}).get("content", "")
                if not body or msg.get("messageType") != "message":
                    continue
                team_items.append(SourceItem(
                    source="teams", kind="channel_message",
                    title=f"#{channel['displayName']} ({team['displayName']})",
                    url=msg.get("webUrl", "https://teams.microsoft.com"),
                    content=_strip_html(body),
                    author=msg.get("from", {}).get("user", {}).get("displayName") or "unknown",
                    timestamp=_parse_dt(msg["createdDateTime"]),
                ))
        return team_items

    items: List[SourceItem] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(teams), 8)) as executor:
        futures = [executor.submit(_fetch_team, team) for team in teams]
        for future in concurrent.futures.as_completed(futures):
            try:
                items.extend(future.result())
            except Exception as exc:
                log.warning("Failed to fetch team messages: %s", exc)
    return items


def _fetch_chat_messages(headers: dict, since: datetime) -> List[SourceItem]:
    chats = _get_all_pages(headers, f"{GRAPH}/me/chats", params={"$expand": "members"})
    if not chats:
        return []
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fetch_chat(chat: dict) -> List[SourceItem]:
        messages = _get_all_pages(
            headers,
            f"{GRAPH}/me/chats/{chat['id']}/messages",
            params={"$filter": f"createdDateTime ge {since_iso}", "$top": "50"},
        )
        chat_label = _chat_label(chat)
        chat_items = []
        for msg in messages:
            body = msg.get("body", {}).get("content", "")
            if not body or msg.get("messageType") != "message":
                continue
            chat_items.append(SourceItem(
                source="teams", kind="chat_message",
                title=f"DM: {chat_label}",
                url=f"https://teams.microsoft.com/l/chat/{chat['id']}",
                content=_strip_html(body),
                author=msg.get("from", {}).get("user", {}).get("displayName") or "unknown",
                timestamp=_parse_dt(msg["createdDateTime"]),
            ))
        return chat_items

    items: List[SourceItem] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chats), 8)) as executor:
        futures = [executor.submit(_fetch_chat, chat) for chat in chats]
        for future in concurrent.futures.as_completed(futures):
            try:
                items.extend(future.result())
            except Exception as exc:
                log.warning("Failed to fetch chat messages: %s", exc)
    return items


def _chat_label(chat: dict) -> str:
    members = chat.get("members", [])
    names = [m.get("displayName", "") for m in members if m.get("displayName")]
    return ", ".join(names[:3]) or "Unknown chat"


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
