from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from digest.sources.teams import fetch, _strip_html, _chat_label

SINCE = datetime(2026, 4, 9, 7, 0, 0, tzinfo=timezone.utc)

TEAM = {"id": "team-1", "displayName": "Engineering"}
CHANNEL = {"id": "ch-1", "displayName": "general"}
MESSAGE = {
    "id": "msg-1",
    "messageType": "message",
    "createdDateTime": "2026-04-09T08:00:00Z",
    "webUrl": "https://teams.microsoft.com/msg",
    "body": {"content": "Deploy is live"},
    "from": {"user": {"displayName": "Marco"}},
}
CHAT = {"id": "chat-1", "chatType": "oneOnOne", "members": [
    {"displayName": "Lisa"},
    {"displayName": "Chris"},
]}
CHAT_MSG = {
    "id": "cmsg-1",
    "messageType": "message",
    "createdDateTime": "2026-04-09T09:00:00Z",
    "body": {"content": "See you Thursday?"},
    "from": {"user": {"displayName": "Lisa"}},
}


def make_mock(data):
    m = MagicMock()
    m.json.return_value = data
    m.raise_for_status = lambda: None
    return m


def test_fetch_channel_message():
    side_effects = [
        make_mock({"value": [TEAM]}),      # joinedTeams
        make_mock({"value": [CHANNEL]}),   # channels
        make_mock({"value": [MESSAGE]}),   # channel messages
        make_mock({"value": [CHAT]}),      # chats
        make_mock({"value": []}),          # chat messages
    ]
    with patch("digest.sources.teams.requests.get", side_effect=side_effects):
        items = fetch("token123", SINCE)

    ch_items = [i for i in items if i.kind == "channel_message"]
    assert len(ch_items) == 1
    assert ch_items[0].author == "Marco"
    assert ch_items[0].title == "#general (Engineering)"


def test_fetch_chat_message():
    side_effects = [
        make_mock({"value": []}),           # joinedTeams
        make_mock({"value": [CHAT]}),       # chats
        make_mock({"value": [CHAT_MSG]}),   # chat messages
    ]
    with patch("digest.sources.teams.requests.get", side_effect=side_effects):
        items = fetch("token123", SINCE)

    chat_items = [i for i in items if i.kind == "chat_message"]
    assert len(chat_items) == 1
    assert chat_items[0].author == "Lisa"


def test_strip_html():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_chat_label():
    assert _chat_label(CHAT) == "Lisa, Chris"


def test_system_messages_excluded():
    system_msg = dict(MESSAGE, messageType="systemEventMessage")
    side_effects = [
        make_mock({"value": [TEAM]}),
        make_mock({"value": [CHANNEL]}),
        make_mock({"value": [system_msg]}),
        make_mock({"value": []}),
        make_mock({"value": []}),
    ]
    with patch("digest.sources.teams.requests.get", side_effect=side_effects):
        items = fetch("token123", SINCE)
    assert len(items) == 0
