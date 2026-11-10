"""--create-ticket: one Jira ticket for a spec's source changes (change request, delta spec, open
points, old vs new sources, link to the spec).

The AI effort estimate (SpecAnalysis.effort) is deliberately never written into the ticket: the
user fills effort fields in themselves.
"""
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from digest.auth.atlassian import get_auth_header
from digest.config import Config, TicketConfig
from digest.spec_monitor import runner
from digest.spec_monitor.http import RequestFailed, request_with_retry
from digest.spec_monitor.models import Classification, SpecAnalysis
from digest.spec_monitor.report import _DE, _EN, _delta_groups, _link, _source_row

logger = logging.getLogger(__name__)

SPEC_MONITOR_LABEL = "spec-monitor"
_SUMMARY_MAX = 255
_SUMMARY_PREFIX = "[DeltaSpec] ["
_SUMMARY_SUFFIX_LEN = len("] [YYYY-MM-DD]")
_CORE_FIELDS = frozenset({"summary", "description", "project", "issuetype"})


class TicketError(Exception):
    """Ticket could not be created (message is user-facing)."""


@dataclass
class TicketDraft:
    summary: str
    description: str                       # Jira wiki markup
    labels: List[str] = field(default_factory=list)
    link_keys: List[str] = field(default_factory=list)
    spec_url: str = ""
    spec_title: str = ""


_EXTRA_DE: Dict[str, str] = {
    "clarify": "Vor der Umsetzung zu klären",
    "note": "Hinweis",
    "note_intro": "Jira hat folgende Felder nicht akzeptiert, sie wurden nicht gesetzt:",
    "analysis_incomplete": "Analyse unvollständig: Änderungsantrag und Delta-Spezifikation konnten nicht vollständig erzeugt werden.",
}
_EXTRA_EN: Dict[str, str] = {
    "clarify": "To clarify before implementation",
    "note": "Note",
    "note_intro": "Jira did not accept the following fields; they were not set:",
    "analysis_incomplete": "Analysis incomplete: change request and delta specification could not be generated completely.",
}

_WIKI_SPECIAL = re.compile(r"([{}\[\]|*_^~!])")


def _labels(language: str) -> Dict[str, str]:
    de = (language or "de").lower().startswith("de")
    out = dict(_DE if de else _EN)
    out.update(_EXTRA_DE if de else _EXTRA_EN)
    return out


def _w(text: Optional[str]) -> str:
    """Single-line text with Jira wiki markup characters escaped."""
    flat = " ".join(str(text or "").split())
    flat = _WIKI_SPECIAL.sub(r"\\\1", flat)
    if flat[:1] in ("#", "-", "+"):
        flat = "\\" + flat
    return flat


def _wl(url: Optional[str], text: str) -> str:
    safe = _link(url)
    if not safe:
        return _w(text)
    label = " ".join(str(text).split()).replace("|", "/").replace("[", "(").replace("]", ")") or safe
    target = safe.replace("|", "%7C").replace("[", "%5B").replace("]", "%5D")
    return f"[{label}|{target}]"


def _cell(*lines: str) -> str:
    parts = [l for l in lines if l]
    return " \\\\ ".join(parts) if parts else " "


def _table(headers: List[str], rows: List[List[str]]) -> List[str]:
    out = ["||" + "||".join(headers) + "||"]
    out.extend("|" + "|".join(r) + "|" for r in rows)
    return out


def _sources_block(a: SpecAnalysis, L: Dict[str, str]) -> List[str]:
    rows = []
    for s in a.sources:
        r = _source_row(s, L)
        name = _wl(r["url"], r["name"]) + (f" ({_w(r['section'])})" if r["section"] else "")
        details = [_w(d) for d in r["detail"]]
        rows.append([
            _cell(name, *details),
            _cell(_wl(r["old_url"], r["old"])),
            _cell(_wl(r["new_url"], r["new"]) if r["new"] else ""),
            _cell(_w(r["classification"])),
        ])
    return _table([L["source"], L["old"], L["new"], L["classification"]], rows)


def _delta_block(a: SpecAnalysis, L: Dict[str, str]) -> List[str]:
    out: List[str] = []
    for g in _delta_groups(a.delta_changes, L):
        out.append(f"h3. {g['label']}")
        rows = []
        for c in g["entries"]:
            part = _w(c.affected_part) + (f" ({_w(', '.join(c.ids))})" if c.ids else "")
            rows.append([
                _cell(part), _cell(_w(c.current_statement)), _cell(_w(c.change)), _cell(_w(c.reason)),
                _cell(f"*{L['blocking']}*" if c.blocking else ""),
            ])
        out.extend(_table([L["part"], L["current"], L["change"], L["reason"], L["blocking_col"]], rows))
        out.append("")
    return out


def _error_lines(a: SpecAnalysis, L: Dict[str, str]) -> List[str]:
    lines = []
    for e in a.errors:
        line = f"{_w(e.scope)}: {_w(e.message)}"
        if e.impact:
            line += f" ({L['err_impact']}: {_w(e.impact)})"
        lines.append(line)
    for s in a.sources:
        if s.classification == Classification.ERROR:
            lines.append(f"{_w(s.ref.name)}: {_w('; '.join(s.errors) or 'error')}")
        elif s.classification == Classification.NOT_MONITORED:
            note = "; ".join(s.warnings) or s.ref.version_text
            lines.append(f"{_w(s.ref.name)}: {L['cls_not monitored']}" + (f" ({_w(note)})" if note else ""))
    return lines


def _build_description(a: SpecAnalysis, L: Dict[str, str]) -> str:
    lines: List[str] = [f"* {L['spec']}: {_wl(a.spec.url, a.spec.title)}", f"* {L['status']}: {_w(a.spec.status)}"]
    if a.spec.jira:
        lines.append(f"* {L['jira']}: {_w(', '.join(a.spec.jira))}")
    lines.append("")

    lines.append(f"h2. {L['change_request']}")
    paragraphs = [p.strip() for p in (a.change_request or "").splitlines() if p.strip()]
    for p in paragraphs:
        lines += [_w(p), ""]
    if a.analysis_incomplete:
        lines += [f"{{warning}}{L['analysis_incomplete']}{{warning}}", ""]

    lines.append(f"h2. {L['sources']}")
    lines += _sources_block(a, L)
    lines.append("")

    if a.delta_changes:
        lines.append(f"h2. {L['delta']}")
        lines += _delta_block(a, L)

    if a.open_points:
        lines.append(f"h2. {L['clarify']}")
        for p in a.open_points:
            lines.append(f"* {_w(p.question)}" + (f" ({L['raised_by']}: {_w(p.raised_by)})" if p.raised_by else ""))
        lines.append("")

    if a.completeness_warnings:
        lines.append(f"h2. {L['warnings']}")
        lines.append("{warning}")
        lines += [f"* {_w(w)}" for w in a.completeness_warnings]
        lines += ["{warning}", ""]

    errors = _error_lines(a, L)
    if errors:
        lines.append(f"h2. {L['errors']}")
        lines += [f"* {e}" for e in errors]
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _summary_name(title: str) -> str:
    """Page name as it appears in the summary: truncated (with an ellipsis) so the summary fits Jira's limit."""
    name = " ".join(str(title or "").split())
    room = _SUMMARY_MAX - len(_SUMMARY_PREFIX) - _SUMMARY_SUFFIX_LEN
    return name if len(name) <= room else name[:room - 1] + "…"


def build_ticket(analysis: SpecAnalysis, ticket_cfg: TicketConfig, language: str,
                 today: Optional[date] = None) -> TicketDraft:
    L = _labels(language)
    day = (today or date.today()).isoformat()
    summary = f"{_SUMMARY_PREFIX}{_summary_name(analysis.spec.title)}] [{day}]"
    labels = list(dict.fromkeys([*ticket_cfg.labels, SPEC_MONITOR_LABEL]))
    return TicketDraft(
        summary=summary,
        description=_build_description(analysis, L),
        labels=labels,
        link_keys=list(dict.fromkeys(analysis.spec.jira)),
        spec_url=analysis.spec.url,
        spec_title=analysis.spec.title,
    )


# --- Jira I/O ------------------------------------------------------------------

def _headers(config: Config) -> dict:
    return {
        "Authorization": get_auth_header(config.atlassian),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _call(config: Config, method: str, path: str, what: str, **kwargs):
    url = f"{config.atlassian.jira_api_base.rstrip('/')}{path}"
    try:
        return request_with_retry(method, url, retry=config.spec_monitor.retry, headers=_headers(config), **kwargs)
    except RequestFailed as exc:
        raise TicketError(f"{what} failed: {exc}") from exc


def _issue_url(config: Config, key: str) -> str:
    return f"{config.atlassian.url.rstrip('/')}/browse/{key}"


def _short(resp) -> str:
    return (getattr(resp, "text", "") or "")[:500]


FORBIDDEN_FIELDS = frozenset({"customfield_11202", "fixVersions", "customfield_10031"})
_PROTECTED_FIELDS = ("project", "issuetype", "summary", "description")
_SUBTASK_TYPES = ("sub-task", "subtask")


def build_fields(tcfg: TicketConfig, draft: TicketDraft):
    """(fields, dropped_forbidden_keys, ignored_protected_keys) for the create payload."""
    fields: Dict[str, object] = {
        "project": {"key": tcfg.project},
        "issuetype": {"name": tcfg.issue_type},
        "summary": draft.summary,
        "description": draft.description,
        "labels": draft.labels,
    }
    if tcfg.priority:
        fields["priority"] = {"name": tcfg.priority}
    if tcfg.team_id and tcfg.issue_type.strip().casefold() not in _SUBTASK_TYPES:
        fields[tcfg.team_field] = tcfg.team_id
    if tcfg.parent:
        fields["parent"] = {"key": tcfg.parent}
    dropped: List[str] = []
    ignored: List[str] = []
    for k, v in (tcfg.fields or {}).items():
        if k in FORBIDDEN_FIELDS:
            dropped.append(k)
        elif k in _PROTECTED_FIELDS:
            ignored.append(k)
        else:
            fields[k] = v
    return fields, dropped, ignored


def _translate_error(raw: str, tcfg: TicketConfig) -> str:
    """Raw Jira error text, prefixed with the config key to fix for known team/parent/priority problems."""
    low = raw.casefold()
    hints = []
    if "non-existing team" in low or re.search(r"team id .* is not valid", low):
        hints.append("spec_monitor.ticket.team_id is not accepted by Jira (must be the raw team id string)")
    if "valid parent" in low:
        hints.append("spec_monitor.ticket.parent must be a Feature, not a Story")
    if "priority" in low and ("valid" in low or "invalid" in low):
        hints.append("spec_monitor.ticket.priority is not accepted by Jira")
    if not hints:
        return raw
    return f"{'; '.join(hints)} (Jira: {raw})"


def _rejected_optional_fields(resp, fields: Dict[str, object]) -> Dict[str, str]:
    """{payload key: Jira message} to drop, or {} when the 400 must fail the run."""
    errors = _parse_errors(resp)
    if not errors or any(k.casefold() in _CORE_FIELDS for k in errors):
        return {}
    return {k: v for k, v in errors.items() if k in fields}


@dataclass
class DroppedField:
    key: str            # payload key, e.g. customfield_10001
    label: str          # human name for messages, e.g. team
    reason: str         # raw Jira message


@dataclass
class TicketResult:
    url: str
    dropped: List[DroppedField] = field(default_factory=list)
    link_failures: List[str] = field(default_factory=list)


def _config_key(key: str, tcfg: TicketConfig) -> Optional[str]:
    if key == tcfg.team_field:
        return "spec_monitor.ticket.team_id"
    if key in ("parent", "priority", "labels"):
        return f"spec_monitor.ticket.{key}"
    if key in (tcfg.fields or {}):
        return f"spec_monitor.ticket.fields.{key}"
    return None


def _parse_errors(resp) -> Dict[str, str]:
    """The `errors` object (field -> message) of a Jira 400 body; {} when absent or unparseable."""
    data = None
    for load in (lambda: json.loads(getattr(resp, "text", "") or ""), resp.json):
        try:
            data = load()
        except Exception:
            continue
        if isinstance(data, dict):
            break
    errors = data.get("errors") if isinstance(data, dict) else None
    if not isinstance(errors, dict):
        return {}
    return {str(k): " ".join(str(v).split()) for k, v in errors.items()}


def _with_note(description: str, dropped: List[DroppedField], language: str) -> str:
    if not dropped:
        return description
    L = _labels(language)
    lines = [description.rstrip(), "", f"h3. {L['note']}", L["note_intro"]]
    lines += [f"* {_w(d.label)}: {_w(d.reason)}" for d in dropped]
    return "\n".join(lines) + "\n"


def create_ticket(config: Config, draft: TicketDraft) -> str:
    """Create the issue (and best-effort links) and return its URL. Raises TicketError."""
    return create_ticket_detailed(config, draft).url


def create_ticket_detailed(config: Config, draft: TicketDraft) -> TicketResult:
    """Like create_ticket, but also reports optional fields Jira rejected (dropped, never fatal).

    A 400 whose `errors` name only non-core fields drops exactly those fields and retries; a core
    field (summary/description/project/issuetype), a 400 without field names, or any other failure raises.
    """
    tcfg = config.spec_monitor.ticket

    fields, dropped_forbidden, ignored = build_fields(tcfg, draft)
    for k in dropped_forbidden:
        print(f"WARNING: field {k} from spec_monitor.ticket.fields is never written and was dropped", file=sys.stderr)
    for k in ignored:
        print(f"WARNING: field {k} from spec_monitor.ticket.fields cannot override the generated value and was ignored",
              file=sys.stderr)

    dropped: List[DroppedField] = []
    max_attempts = len([k for k in fields if k not in _CORE_FIELDS]) + 1
    resp = None
    for _ in range(max_attempts):
        payload = dict(fields)
        payload["description"] = _with_note(draft.description, dropped, getattr(config, "language", "de"))
        resp = _call(config, "POST", "/rest/api/2/issue", "ticket creation", json={"fields": payload})
        if resp.ok:
            break
        rejected = _rejected_optional_fields(resp, fields) if resp.status_code == 400 else {}
        if not rejected:
            break
        for key, message in rejected.items():
            label = "team" if key == tcfg.team_field else key
            reason = _translate_error(message, tcfg)
            hint = _config_key(key, tcfg)
            if hint and hint not in reason:
                reason += f" [fix: {hint}]"
            text = f"field {key} was not accepted by Jira and is left out of the ticket: {reason}"
            logger.warning(text)
            print(f"WARNING: {text}", file=sys.stderr)
            dropped.append(DroppedField(key=key, label=label, reason=message))
            del fields[key]
    if resp is None or not resp.ok:
        raise TicketError(f"ticket creation failed: HTTP {resp.status_code} {_translate_error(_short(resp), tcfg)}")
    key = resp.json()["key"]

    link_failures: List[str] = []
    for other in draft.link_keys:
        body = {
            "type": {"name": tcfg.link_type},
            "inwardIssue": {"key": key},
            "outwardIssue": {"key": other},
        }
        try:
            lresp = _call(config, "POST", "/rest/api/2/issueLink", "issue link", json=body)
            if not lresp.ok:
                reason = f"HTTP {lresp.status_code} {_short(lresp)}"
                print(f"WARNING: issue link to {other} failed: {reason}", file=sys.stderr)
                link_failures.append(f"issue link to {other}: {reason}")
        except TicketError as exc:
            print(f"WARNING: issue link to {other} failed: {exc}", file=sys.stderr)
            link_failures.append(f"issue link to {other}: {exc}")

    if tcfg.remote_link and draft.spec_url:
        body = {"object": {"url": draft.spec_url, "title": f"Spezifikation: {draft.spec_title or draft.summary}"}}
        try:
            rresp = _call(config, "POST", f"/rest/api/2/issue/{key}/remotelink", "remote link", json=body)
            if not rresp.ok:
                reason = f"HTTP {rresp.status_code} {_short(rresp)}"
                print(f"WARNING: remote link to the spec page failed: {reason}", file=sys.stderr)
                link_failures.append(f"remote link to the spec page: {reason}")
        except TicketError as exc:
            print(f"WARNING: remote link to the spec page failed: {exc}", file=sys.stderr)
            link_failures.append(f"remote link to the spec page: {exc}")
    return TicketResult(url=_issue_url(config, key), dropped=dropped, link_failures=link_failures)


def _write_draft(config: Config, draft: TicketDraft) -> str:
    tcfg = config.spec_monitor.ticket
    _, dropped, ignored = build_fields(tcfg, draft)
    is_subtask = tcfg.issue_type.strip().casefold() in _SUBTASK_TYPES
    out_dir = config.data_dir / "spec_cache"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "ticket-draft.md"
    text = "\n".join([
        f"# {draft.summary}",
        "",
        f"- Project: {tcfg.project}",
        f"- Issue type: {tcfg.issue_type}",
        f"- Labels: {', '.join(draft.labels)}",
        f"- Priority: {tcfg.priority or '-'}",
        f"- Team ({tcfg.team_field}): {(tcfg.team_id if not is_subtask else None) or '-'}",
        f"- Parent: {tcfg.parent or '-'}",
        f"- Links ({tcfg.link_type}): {', '.join(draft.link_keys) or '-'}",
        f"- Remote link: {'yes (' + draft.spec_url + ')' if tcfg.remote_link and draft.spec_url else 'no'}",
        f"- Dropped fields (never written): {', '.join(dropped) or '-'}",
        f"- Ignored fields (cannot override): {', '.join(ignored) or '-'}",
        f"- Spec: {draft.spec_url}",
        "",
        "## Summary",
        "",
        draft.summary,
        "",
        "## Description (Jira wiki markup)",
        "",
        draft.description,
    ])
    path.write_text(text, encoding="utf-8")
    return str(path)


def _analysis_problems(analysis: SpecAnalysis) -> List[str]:
    """Everything that stops 'no source changed' from being a trustworthy conclusion."""
    lines = []
    covered = []
    if not analysis.sources:
        lines.append("no sources found on the page, nothing was checked")
    for s in analysis.sources:
        for e in s.errors:
            lines.append(f"{s.ref.name}: {e}")
        if s.classification == Classification.ERROR and not s.errors:
            lines.append(f"{s.ref.name}: source could not be read")
        if s.classification == Classification.NOT_MONITORED:
            reason = "; ".join(s.warnings) or s.ref.note or "cannot be monitored automatically"
            lines.append(f"{s.ref.name}: not monitored, check manually ({reason})")
        if s.classification in (Classification.ERROR, Classification.NOT_MONITORED):
            covered.append(f"Quelle {s.ref.name}:")
    lines += [f"{e.scope}: {e.message}" for e in analysis.errors]
    lines += [w for w in analysis.completeness_warnings if not any(w.startswith(c) for c in covered)]
    return lines


def run_create_ticket(args, config: Config, today: Optional[date] = None) -> None:
    sm = config.spec_monitor
    if sm is None or sm.ticket is None:
        raise RuntimeError(
            "No spec_monitor.ticket block configured. Add 'ticket: {project: ..., issue_type: ...}' "
            "under spec_monitor in config.yaml to use --create-ticket.")

    flags = " --dry-run" if args.dry_run else ""
    try:
        report = runner.run_spec_monitor(
            config, only=args.create_ticket, command_line=f"--create-ticket {args.create_ticket}{flags}",
            dry_run=bool(args.dry_run))
    except runner.SpecMonitorFatal as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    if not report.analysed:
        print(f"ERROR: spec '{args.create_ticket}' was not analysed", file=sys.stderr)
        raise SystemExit(1)
    analysis = report.analysed[0]
    if not analysis.has_changes:
        problems = _analysis_problems(analysis)
        if problems:
            print(f"ERROR: no ticket created for {analysis.spec.title}: no source change was confirmed "
                  f"because some sources could not be checked:", file=sys.stderr)
            for line in problems:
                print(f"  - {line}", file=sys.stderr)
            raise SystemExit(1)
        print(f"no source changes for {analysis.spec.title}; no ticket created")
        return

    draft = build_ticket(analysis, sm.ticket, config.language, today=today)
    if args.dry_run:
        print(f"Ticket draft written to {_write_draft(config, draft)} (nothing created)")
        return
    try:
        result = create_ticket_detailed(config, draft)
    except TicketError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Ticket created: {result.url}")
    if result.dropped:
        listed = ", ".join(f"{d.label} ({d.reason})" for d in result.dropped)
        print(f"Created WITHOUT: {listed} - please set these manually")
    if result.link_failures:
        print(f"ERROR: ticket {result.url} was created but {len(result.link_failures)} link(s) are missing, "
              "please add them manually:", file=sys.stderr)
        for line in result.link_failures:
            print(f"  - {line}", file=sys.stderr)
        raise SystemExit(1)
