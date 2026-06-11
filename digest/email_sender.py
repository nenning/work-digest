"""Email sender: renders the HTML digest template and sends via SMTP or Outlook COM."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import keyring
from jinja2 import Environment, FileSystemLoader

from digest.config import EmailConfig, SmtpConfig
from digest.models import SourceItem, SummarizedItem

log = logging.getLogger(__name__)

VALID_SOURCES = {"jira", "confluence", "teams", "outlook"}

GROUP_ORDER = ["confluence", "jira_mentions", "jira_comments", "new", "jira_changes", "updates"]
GROUP_ICONS: Dict[str, str] = {
    "confluence": "📄",
    "jira_mentions": "🔔",
    "jira_comments": "💬",
    "new": "✨",
    "jira_changes": "🔁",
    "updates": "🔄",
}
GROUP_LABELS: Dict[str, str] = {
    "confluence": "Confluence",
    "jira_mentions": "Jira: Erwähnungen",
    "jira_comments": "Jira: Kommentare & Beschreibungen",
    "new": "Jira: Neue Tickets",
    "jira_changes": "Jira: Feldänderungen",
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
_JIRA_COMMENT_KINDS = {"comment", "description_change"}
_JIRA_CHANGE_KINDS = {"field_change", "assignment"}


def _kind_to_group(source: str, kind: str) -> str:
    if source == "confluence":
        return "confluence"
    if source == "jira":
        if kind == "mention":
            return "jira_mentions"
        if kind in _JIRA_COMMENT_KINDS:
            return "jira_comments"
        if kind in _JIRA_CHANGE_KINDS:
            return "jira_changes"
    if kind in _NEW_KINDS:
        return "new"
    return "updates"
TEMPLATES_DIR = Path(__file__).parent / "templates"

_ALLOWED_URL_SCHEMES = {"http", "https"}

# Fail fast at import time if the template files are missing (deployment error).
assert (TEMPLATES_DIR / "digest.html.j2").exists(), (
    f"Email template missing: {TEMPLATES_DIR / 'digest.html.j2'}. "
    "Ensure the digest/templates/ directory is present."
)
assert (TEMPLATES_DIR / "mgmt_summary.html.j2").exists(), (
    f"Management summary template missing: {TEMPLATES_DIR / 'mgmt_summary.html.j2'}. "
    "Ensure the digest/templates/ directory is present."
)


def _safe_url(url: str) -> str:
    """Return url if scheme is http/https, else '#' to prevent javascript: injection."""
    try:
        scheme = urlparse(url).scheme.lower()
    except Exception:
        return "#"
    return url if scheme in _ALLOWED_URL_SCHEMES else "#"


def _format_timing_text(timing: dict) -> str:
    t_fetch = timing.get("t_fetch", 0)
    t_sum = timing.get("t_sum", 0)
    n_fetched = timing.get("n_fetched", 0)
    n_summarized = timing.get("n_summarized", 0)
    model_stats = timing.get("model_stats", {})

    sep = "  " + "─" * 30
    lines = [
        f"  {'fetch':<11} {t_fetch:5.1f}s   {n_fetched} item{'s' if n_fetched != 1 else ''}",
        f"  {'summarize':<11} {t_sum:5.1f}s   {n_summarized} item{'s' if n_summarized != 1 else ''}",
        sep,
        f"  {'total':<11} {t_fetch + t_sum:5.1f}s",
    ]

    if model_stats:
        model_times = model_stats.get("times", {})
        model_errors = model_stats.get("errors", {})
        all_models = sorted(set(model_times) | set(model_errors))
        if all_models:
            lines += ["", "  Model response times:"]
            for model in all_models:
                times = model_times.get(model, [])
                errs = model_errors.get(model, 0)
                avg_str = (
                    f"{sum(times)/len(times):.2f}s avg ({len(times)} call{'s' if len(times) != 1 else ''})"
                    if times else "no successful calls"
                )
                err_str = f", {errs} error{'s' if errs != 1 else ''}" if errs else ""
                lines.append(f"    {model}: {avg_str}{err_str}")

    return "\n".join(lines)


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
    timing: Optional[dict] = None,
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
        timing_text=_format_timing_text(timing) if timing else None,
    )


def _smtp_send(smtp_cfg: SmtpConfig, recipient: str, subject: str, html_body: str) -> bool:
    password = keyring.get_password("digest-smtp", smtp_cfg.username)
    if password is None:
        raise RuntimeError(
            f"No SMTP password found in keyring for {smtp_cfg.username!r}. "
            "Run: python digest/main.py --setup-smtp-auth"
        )

    sender = smtp_cfg.sender or smtp_cfg.username
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_cfg.host, smtp_cfg.port) as server:
            if smtp_cfg.use_tls:
                server.starttls()
            server.login(smtp_cfg.username, password)
            server.sendmail(sender, recipient, msg.as_string())
    except Exception as exc:
        log.error("Failed to send email via SMTP: %s", exc)
        return False

    return True


def send_via_smtp(
    items: List[SummarizedItem],
    config: EmailConfig,
    smtp_cfg: SmtpConfig,
    recipient: str,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    notices: Optional[List[str]] = None,
    time_range: Optional[str] = None,
    timing: Optional[dict] = None,
) -> bool:
    """Render and send (or dry-run) the digest email via SMTP."""
    if not items:
        return False

    sections: Dict[str, List[SummarizedItem]] = {}
    for item in items:
        if item.source not in VALID_SOURCES:
            continue
        group = _kind_to_group(item.source, item.kind)
        sections.setdefault(group, []).append(item)

    if not sections:
        return False

    n_items = sum(len(v) for v in sections.values())
    n_sources = len({it.source for grp in sections.values() for it in grp})
    _now = now if now is not None else datetime.now().astimezone()
    subject = _build_subject(config.subject_prefix, _now, n_items, n_sources)
    html_body = _render_html(sections, subject, notices, time_range, timing)

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

    return _smtp_send(smtp_cfg, recipient, subject, html_body)


def send_mgmt_summary_via_smtp(
    narrative: str,
    jira_items: List[SourceItem],
    confluence_items: List[SourceItem],
    subject: str,
    config: EmailConfig,
    smtp_cfg: SmtpConfig,
    recipient: str,
    dry_run: bool = False,
    time_range: Optional[str] = None,
    notices: Optional[List[str]] = None,
) -> bool:
    html_body = _render_mgmt_html(narrative, jira_items, confluence_items, subject, time_range, notices)

    if dry_run:
        print(f"\nDRY RUN — {subject}\n{'─' * 60}")
        print(narrative)
        print(f"\n[{len(jira_items)} Jira tickets, {len(confluence_items)} Confluence pages]")
        return True

    return _smtp_send(smtp_cfg, recipient, subject, html_body)


def send_via_com(
    items: List[SummarizedItem],
    config: EmailConfig,
    recipient: str,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    notices: Optional[List[str]] = None,
    time_range: Optional[str] = None,
    timing: Optional[dict] = None,
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
        group = _kind_to_group(item.source, item.kind)
        sections.setdefault(group, []).append(item)

    if not sections:
        return False

    n_items = sum(len(v) for v in sections.values())
    n_sources = len({it.source for grp in sections.values() for it in grp})
    _now = now if now is not None else datetime.now().astimezone()
    subject = _build_subject(config.subject_prefix, _now, n_items, n_sources)
    html_body = _render_html(sections, subject, notices, time_range, timing)

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


def _render_mgmt_html(
    narrative: str,
    jira_items: List[SourceItem],
    confluence_items: List[SourceItem],
    subject: str,
    time_range: Optional[str] = None,
    notices: Optional[List[str]] = None,
) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.filters["safe_url"] = _safe_url
    template = env.get_template("mgmt_summary.html.j2")

    lines = [ln.strip() for ln in narrative.strip().splitlines() if ln.strip()]
    bullet_items = [ln[2:] for ln in lines if ln.startswith("- ")]
    if bullet_items:
        paragraphs = []
    else:
        paragraphs = [p.strip() for p in narrative.split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [narrative.strip()]

    done_tickets = [i for i in jira_items if i.kind == "ticket_done"]
    wip_tickets  = [i for i in jira_items if i.kind == "ticket_wip"]
    todo_tickets = [i for i in jira_items if i.kind == "ticket_todo"]

    return template.render(
        subject=subject,
        time_range=time_range,
        notices=notices or [],
        narrative_paragraphs=paragraphs,
        bullet_items=bullet_items,
        done_tickets=done_tickets,
        wip_tickets=wip_tickets,
        todo_tickets=todo_tickets,
        confluence_items=confluence_items,
    )


def send_mgmt_summary_via_com(
    narrative: str,
    jira_items: List[SourceItem],
    confluence_items: List[SourceItem],
    subject: str,
    config: EmailConfig,
    recipient: str,
    dry_run: bool = False,
    time_range: Optional[str] = None,
    notices: Optional[List[str]] = None,
) -> bool:
    html_body = _render_mgmt_html(narrative, jira_items, confluence_items, subject, time_range, notices)

    try:
        import win32com.client
    except ImportError:
        log.error("pywin32 is not installed — cannot send via Outlook COM. Run: pip install pywin32")
        return False

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.To = recipient
        mail.Subject = subject
        mail.HTMLBody = html_body
        if dry_run:
            mail.Display()
        else:
            mail.Send()
    except Exception as exc:
        log.error("Failed to send management summary via Outlook COM: %s", exc)
        return False

    return True
