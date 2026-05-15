"""Email sender: renders the HTML digest template and sends via Graph API."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from jinja2 import Environment, FileSystemLoader

from digest.config import EmailConfig
from digest.models import SummarizedItem

log = logging.getLogger(__name__)

VALID_SOURCES = {"jira", "confluence", "teams", "outlook"}

GROUP_ORDER = ["new", "updates"]
GROUP_ICONS: Dict[str, str] = {
    "new": "✨",
    "updates": "🔄",
}
GROUP_LABELS: Dict[str, str] = {
    "new": "Neue Tickets & Seiten",
    "updates": "Updates & Aktivität",
}

SOURCE_ICONS: Dict[str, str] = {
    "jira": "📋",
    "confluence": "📄",
    "teams": "💬",
    "outlook": "📧",
}
SOURCE_LABELS: Dict[str, str] = {
    "jira": "Jira",
    "confluence": "Confluence",
    "teams": "Teams",
    "outlook": "Outlook",
}

_NEW_KINDS = {"new_ticket"}


def _kind_to_group(kind: str) -> str:
    return "new" if kind in _NEW_KINDS else "updates"
TEMPLATES_DIR = Path(__file__).parent / "templates"

_GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
_ALLOWED_URL_SCHEMES = {"http", "https"}

# Fail fast at import time if the template file is missing (deployment error).
assert (TEMPLATES_DIR / "digest.html.j2").exists(), (
    f"Email template missing: {TEMPLATES_DIR / 'digest.html.j2'}. "
    "Ensure the digest/templates/ directory is present."
)


def _safe_url(url: str) -> str:
    """Return url if scheme is http/https, else '#' to prevent javascript: injection."""
    try:
        scheme = urlparse(url).scheme.lower()
    except Exception:
        return "#"
    return url if scheme in _ALLOWED_URL_SCHEMES else "#"


def _pluralise(count: int, singular: str, plural: Optional[str] = None) -> str:
    if plural is None:
        plural = singular + "s"
    return f"{count} {singular if count == 1 else plural}"


def _build_subject(prefix: str, now: datetime, n_items: int, n_sources: int) -> str:
    weekday = now.strftime("%a")          # "Thu"
    day = str(now.day)                    # "9" (no zero-pad)
    month = now.strftime("%b")            # "Apr"
    hhmm = now.strftime("%H:%M")          # "14:30"
    items_str = _pluralise(n_items, "item")
    sources_str = _pluralise(n_sources, "source")
    return f"{prefix} {weekday} {day} {month} · {hhmm} — {items_str} across {sources_str}"


def _render_html(
    sections: Dict[str, List[SummarizedItem]],
    subject: str,
    notices: Optional[List[str]] = None,
    time_range: Optional[str] = None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    env.filters["safe_url"] = _safe_url
    template = env.get_template("digest.html.j2")
    return template.render(
        subject=subject,
        sections=sections,
        group_order=GROUP_ORDER,
        group_icons=GROUP_ICONS,
        group_labels=GROUP_LABELS,
        source_icons=SOURCE_ICONS,
        source_labels=SOURCE_LABELS,
        notices=notices or [],
        time_range=time_range,
    )


def send_digest(
    items: List[SummarizedItem],
    config: EmailConfig,
    m365_token: str,
    recipient: str,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    notices: Optional[List[str]] = None,
    time_range: Optional[str] = None,
) -> bool:
    """Render and send (or dry-run) the digest email.

    Returns True on success, False when there are no items to send or when
    the Graph API call fails (failure is logged but not re-raised so that
    unattended scheduled runs do not crash).
    """
    if not items:
        return False

    # Group by kind category; filter out unknown sources to prevent unexpected CSS class names.
    sections: Dict[str, List[SummarizedItem]] = {}
    for item in items:
        if item.source not in VALID_SOURCES:
            continue
        group = _kind_to_group(item.kind)
        sections.setdefault(group, []).append(item)

    if not sections:
        return False

    n_items = sum(len(v) for v in sections.values())
    n_sources = len({it.source for grp in sections.values() for it in grp})
    _now = now if now is not None else datetime.now().astimezone()
    subject = _build_subject(config.subject_prefix, _now, n_items, n_sources)
    html_body = _render_html(sections, subject, notices, time_range)

    if dry_run:
        print(f"\nDRY RUN — {subject}\n{'─' * 60}")
        if notices:
            print("\n⚠️  NOTICES")
            for n in notices:
                print(f"  {n}")
        for grp in GROUP_ORDER:
            grp_items = sections.get(grp, [])
            if not grp_items:
                continue
            print(f"\n{GROUP_ICONS[grp]} {GROUP_LABELS[grp].upper()}")
            for it in grp_items:
                src_label = SOURCE_LABELS.get(it.source, it.source)
                print(f"  [{src_label}] {it.title}")
                print(f"  {it.summary}")
        print()
        return True

    try:
        resp = requests.post(
            _GRAPH_SEND_URL,
            headers={
                "Authorization": f"Bearer {m365_token}",
                "Content-Type": "application/json",
            },
            json={
                "message": {
                    "subject": subject,
                    "body": {
                        "contentType": "HTML",
                        "content": html_body,
                    },
                    "toRecipients": [
                        {"emailAddress": {"address": recipient}}
                    ],
                }
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.error("Failed to send digest email: %s", exc)
        return False

    return True


def send_via_com(
    items: List[SummarizedItem],
    config: EmailConfig,
    recipient: str,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    notices: Optional[List[str]] = None,
    time_range: Optional[str] = None,
) -> bool:
    """Render the digest and send (or preview as draft) via Outlook Classic COM.

    dry_run=True opens the draft in Outlook for review without sending.
    dry_run=False sends immediately via mail.Send().

    Requires pywin32 (`pip install pywin32`). Returns True on success, False if
    there are no items or if Outlook COM automation fails.
    """
    if not items:
        return False

    sections: Dict[str, List[SummarizedItem]] = {}
    for item in items:
        if item.source not in VALID_SOURCES:
            continue
        group = _kind_to_group(item.kind)
        sections.setdefault(group, []).append(item)

    if not sections:
        return False

    n_items = sum(len(v) for v in sections.values())
    n_sources = len({it.source for grp in sections.values() for it in grp})
    _now = now if now is not None else datetime.now().astimezone()
    subject = _build_subject(config.subject_prefix, _now, n_items, n_sources)
    html_body = _render_html(sections, subject, notices, time_range)

    try:
        import win32com.client  # pywin32 — Windows only
    except ImportError:
        log.error(
            "pywin32 is not installed — cannot send via Outlook COM. "
            "Run: pip install pywin32"
        )
        return False

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)  # 0 = olMailItem
        mail.To = recipient
        mail.Subject = subject
        mail.HTMLBody = html_body
        if dry_run:
            mail.Display()
        else:
            mail.Send()
    except Exception as exc:
        log.error("Failed to send via Outlook COM: %s", exc)
        return False

    return True
