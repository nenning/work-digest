from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from digest.sources.outlook import fetch

SINCE = datetime(2026, 4, 9, 7, 0, 0, tzinfo=timezone.utc)

EMAIL = {
    "id": "email-1",
    "subject": "Budget Approval Q2",
    "from": {"emailAddress": {"name": "Finance", "address": "finance@company.com"}},
    "receivedDateTime": "2026-04-09T08:15:00Z",
    "bodyPreview": "Please approve the Q2 budget by 17:00 today.",
    "webLink": "https://outlook.office.com/mail/id/email-1",
    "isRead": False,
}


def make_mock(data):
    m = MagicMock()
    m.json.return_value = data
    m.raise_for_status = lambda: None
    return m


def test_fetch_emails():
    with patch("digest.sources.outlook.requests.get", return_value=make_mock({"value": [EMAIL]})):
        items = fetch("token123", SINCE)

    assert len(items) == 1
    assert items[0].title == "Budget Approval Q2"
    assert items[0].author == "Finance"
    assert items[0].source == "outlook"
    assert items[0].kind == "email"


def test_fetch_handles_pagination():
    page1 = make_mock({"value": [EMAIL], "@odata.nextLink": "https://graph.microsoft.com/page2"})
    page2 = make_mock({"value": [dict(EMAIL, id="email-2", subject="Follow-up")]})
    with patch("digest.sources.outlook.requests.get", side_effect=[page1, page2]):
        items = fetch("token123", SINCE)
    assert len(items) == 2


def test_no_subject_uses_default():
    email_no_subject = dict(EMAIL, subject=None)
    with patch("digest.sources.outlook.requests.get", return_value=make_mock({"value": [email_no_subject]})):
        items = fetch("token123", SINCE)
    assert items[0].title == "(no subject)"


def test_content_includes_sender_and_preview():
    with patch("digest.sources.outlook.requests.get", return_value=make_mock({"value": [EMAIL]})):
        items = fetch("token123", SINCE)
    assert "Finance" in items[0].content
    assert "approve" in items[0].content
