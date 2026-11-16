"""--page-watch-apply: acts on the boxes ticked on weekly page-watch report pages (label, Jira ticket, CR file)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from digest.auth.atlassian import get_auth_header
from digest.config import Config, TicketConfig, page_watch_ticket
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.confluence_client import ConfluenceClient
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.page_watch_data import (
    ACTIONS, ATTACHMENT_NAME, ActionState, PageEntry, WatchData, from_json, mark_task_done, parse_tasks, task_id,
    to_json,
)
from digest.spec_monitor.publish import (
    PageConflict, find_child_page, get_attachment, list_child_pages, put_attachment, read_page_storage,
    update_page_storage,
)
from digest.spec_monitor.ticket import TicketDraft, _labels, _w, _wl, create_ticket_detailed
from digest.spec_monitor.trace_jira import find_ticket_by_label
from digest.spec_monitor.week import parse_week

RECENT_DAYS = 56
MAX_REFERENCES = 3
_SUMMARY_MAX = 255
_TITLE = re.compile(r"^Page Watch KW(\d{2}) (\d{4})$")
_PAGE_ID = re.compile(r"(?:/pages/|[?&]pageId=)(\d+)")
_UPDATE_MESSAGE = "page-watch apply"

_TEXT: Dict[str, Dict[str, str]] = {
    "de": {
        "suffix": "Delta-Spezifikation", "window": "Zeitraum", "summary": "Änderungen", "trace": "Bestehende Umsetzung",
        "found": "Spuren gefunden, vermutlich bereits (teilweise) in Arbeit oder umgesetzt",
        "none": "Keine Spuren gefunden", "incomplete": "Suche unvollständig, keine Aussage möglich",
        "frozen": "Seite ist eingefroren", "problems": "Probleme", "own": "eigenes Ticket",
        "in_progress": "in Arbeit", "done_likely": "vermutlich umgesetzt", "related": "verwandt",
        "week": "Woche", "done_frozen": "Label gesetzt", "done_ticket": "Ticket", "done_cr": "in",
    },
    "en": {
        "suffix": "Delta specification", "window": "Window", "summary": "Changes", "trace": "Existing implementation",
        "found": "Traces found, likely already (partly) in progress or implemented",
        "none": "No traces found", "incomplete": "Search incomplete, no statement possible",
        "frozen": "Page is frozen", "problems": "Problems", "own": "own ticket",
        "in_progress": "in progress", "done_likely": "likely done", "related": "related",
        "week": "Week", "done_frozen": "label set", "done_ticket": "ticket", "done_cr": "in",
    },
}


class ApplyFatal(Exception):
    """The run cannot continue (message is user-facing)."""


@dataclass
class ApplyResult:
    page_title: str
    done: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: int = 0
    warnings: List[str] = field(default_factory=list)


def _text(data: WatchData) -> Dict[str, str]:
    return _TEXT["en" if (data.language or "de").lower().startswith("en") else "de"]


def _flat(text: object) -> str:
    return " ".join(str(text or "").split())


def _md(text: object) -> str:
    return _flat(text).replace("[", "(").replace("]", ")")


def _ticket_label(data: WatchData, entry: PageEntry) -> str:
    return f"pagewatch-{entry.page_id}-{data.week_tag}"


def _delta_kind(raw: str, L: Dict[str, str]) -> str:
    return L["kind_new"] if raw == "new" else L["kind_removed"] if raw == "removed" else L["kind_changed"]


def _hint(entry: PageEntry, T: Dict[str, str]) -> str:
    trace = entry.trace
    if not trace:
        return ""
    status, problems = trace.get("status"), [_flat(p) for p in trace.get("problems") or []]
    if status == "FROZEN":
        return T["frozen"]
    if status == "FOUND":
        text = T["found"]
    elif status == "NONE" and not problems:
        text = T["none"]
    else:
        text = T["incomplete"]
    return text + (f" ({T['problems']}: {'; '.join(problems)})" if problems else "")


def _references(entry: PageEntry) -> List[dict]:
    return list((entry.trace or {}).get("references") or [])[:MAX_REFERENCES]


def _ref_note(ref: dict, T: Dict[str, str]) -> str:
    parts = [T.get(ref.get("verdict", ""), ref.get("verdict", "")), _flat(ref.get("reason"))]
    if ref.get("own"):
        parts.append(T["own"])
    return ", ".join(p for p in parts if p)


def build_page_ticket(data: WatchData, entry: PageEntry, tcfg: TicketConfig) -> TicketDraft:
    L, T = _labels(data.language), _text(data)
    suffix = f" — {T['suffix']} {data.week_label.replace(' ', '/')}"
    title = _flat(entry.title)
    room = _SUMMARY_MAX - len(suffix)
    summary = (title if len(title) <= room else title[:room - 1] + "…") + suffix

    lines = [f"* {L['spec']}: {_wl(entry.url, entry.title)}",
             f"* {T['window']}: {_w(data.window_start)} - {_w(data.window_end)} ({_w(data.week_label)})", ""]
    if entry.summary:
        lines += [f"h2. {T['summary']}", _w(entry.summary), ""]
    if entry.deltas:
        lines.append(f"h2. {L['delta']}")
        for d in entry.deltas:
            ids = f" ({_w(', '.join(d.get('ids') or []))})" if d.get("ids") else ""
            line = f"* *{_delta_kind(d.get('kind', ''), L)}* {_w(d.get('affected_part'))}{ids}"
            if d.get("change"):
                line += f": {_w(d['change'])}"
            if d.get("reason"):
                line += f" ({L['reason']}: {_w(d['reason'])})"
            if d.get("blocking"):
                line += f" *{L['clarify']}*"
            lines.append(line)
        lines.append("")
    if entry.open_points:
        lines.append(f"h2. {L['open_points']}")
        lines += [f"* {_w(q)}" for q in entry.open_points]
        lines.append("")
    hint = _hint(entry, T)
    if hint:
        lines += [f"h2. {T['trace']}", _w(hint)]
        for ref in _references(entry):
            name = f"{ref.get('label', '')} ({ref.get('ref', '')})"
            lines.append(f"* {_wl(ref.get('url'), name)} - {_w(_ref_note(ref, T))}")
        lines.append("")
    labels = list(dict.fromkeys([*tcfg.labels, _ticket_label(data, entry)]))
    return TicketDraft(summary=summary, description="\n".join(lines).strip() + "\n", labels=labels,
                       spec_url=entry.url, spec_title=entry.title)


def write_cr_file(data: WatchData, entries: List[PageEntry], out_dir: Path) -> Path:
    L, T = _labels(data.language), _text(data)
    lines = [f"# {data.file_stem}: {data.week_label}", "",
             f"{T['window']}: {data.window_start} - {data.window_end}", ""]
    for entry in sorted(entries, key=lambda e: e.index):
        lines += [f"## [{_md(entry.title)}]({entry.url})", ""]
        if entry.summary:
            lines += [_flat(entry.summary), ""]
        if entry.deltas:
            lines += [f"### {L['delta']}", ""]
            for d in entry.deltas:
                ids = f" ({', '.join(d.get('ids') or [])})" if d.get("ids") else ""
                line = f"- **{_delta_kind(d.get('kind', ''), L)}** {_flat(d.get('affected_part'))}{ids}"
                if d.get("change"):
                    line += f": {_flat(d['change'])}"
                if d.get("reason"):
                    line += f" ({L['reason']}: {_flat(d['reason'])})"
                if d.get("blocking"):
                    line += f" **{L['clarify']}**"
                lines.append(line)
            lines.append("")
        if entry.open_points:
            lines += [f"### {L['open_points']}", ""] + [f"- {_flat(q)}" for q in entry.open_points] + [""]
        hint = _hint(entry, T)
        if hint:
            lines += [f"### {T['trace']}", "", hint, ""]
            for ref in _references(entry):
                name = f"{_md(ref.get('label'))} ({_md(ref.get('ref'))})"
                lines.append(f"- [{name}]({ref['url']})" if ref.get("url") else f"- {name}")
                lines[-1] += f" - {_ref_note(ref, T)}"
            lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{data.file_stem}.md"
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return path


@dataclass
class _Page:
    page_id: str
    title: str
    storage: str
    version: int
    data: WatchData


class _Run:
    def __init__(self, config: Config, dry_run: bool, today: date, progress: Callable[[str], None],
                 auth_header: Optional[str]):
        self.config = config
        self.pw = config.page_watch
        self.atlassian = config.atlassian
        self.retry = self.pw.retry
        self.dry_run = dry_run
        self.today = today
        self.day = today.isoformat()
        self.progress = progress
        self.auth = auth_header or get_auth_header(config.atlassian)
        self._client: Optional[ConfluenceClient] = None

    def client(self) -> ConfluenceClient:
        if self._client is None:
            cache = SpecCache(self.config.data_dir / "spec_cache")
            self._client = ConfluenceClient(self.atlassian, self.auth, self.retry, cache)
        return self._client


def _select(run: _Run, target: Optional[str]) -> List[str]:
    parent = run.pw.publish_parent_page_id
    target = (target or "").strip()
    try:
        if not target:
            cutoff = run.today - timedelta(days=RECENT_DAYS)
            found = []
            for p in list_child_pages(run.atlassian, run.auth, run.retry, parent):
                m = _TITLE.match(p["title"])
                if not m:
                    continue
                try:
                    start = date.fromisocalendar(int(m.group(2)), int(m.group(1)), 1)
                except ValueError:
                    continue
                if start >= cutoff:
                    found.append((start, p["id"]))
            return [pid for _, pid in sorted(found)]
        if target.upper().startswith("KW"):
            try:
                week = parse_week(target, run.today)
            except ValueError as exc:
                raise ApplyFatal(str(exc)) from None
            pid = find_child_page(run.atlassian, run.auth, run.retry, parent, week.title)
            if pid is None:
                raise ApplyFatal(f"no report page '{week.title}' below page {parent}")
            return [pid]
    except (RuntimeError, RequestFailed) as exc:
        raise ApplyFatal(f"cannot list the report pages below page {parent}: {exc}") from None
    m = _PAGE_ID.search(target) or re.fullmatch(r"(\d+)", target)
    if not m:
        raise ApplyFatal(f"{target!r} is neither a week (KW45-2026), a page id nor a page URL")
    return [m.group(1)]


def _load(run: _Run, page_id: str) -> _Page:
    try:
        storage, version, title = read_page_storage(run.atlassian, run.auth, run.retry, page_id)
    except LookupError:
        raise ApplyFatal(f"report page {page_id} not found") from None
    except (RuntimeError, RequestFailed) as exc:
        raise ApplyFatal(f"cannot read report page {page_id}: {exc}") from None
    try:
        raw = get_attachment(run.atlassian, run.auth, run.retry, page_id, ATTACHMENT_NAME)
    except (RuntimeError, RequestFailed) as exc:
        raise ApplyFatal(f"cannot read the data attachment of report page '{title}' ({page_id}): {exc}") from None
    if raw is None:
        raise ApplyFatal(f"report page '{title}' ({page_id}) has no attachment {ATTACHMENT_NAME}")
    try:
        data = from_json(raw.decode("utf-8"))
    except ValueError as exc:
        raise ApplyFatal(f"attachment {ATTACHMENT_NAME} of report page '{title}' ({page_id}) is unusable: {exc}") from None
    return _Page(page_id, title, storage, version, data)


def _done_body(run: _Run, page: _Page, action: str, result: str) -> str:
    T = _text(page.data)
    if action == "ticket":
        base = run.atlassian.url.rstrip("/")
        shown = f'<a href="{html.escape(f"{base}/browse/{result}", quote=True)}">{html.escape(result, quote=False)}</a>'
    else:
        shown = html.escape(result, quote=False)
    return f"✔ {T['done_' + action]} {shown} {run.day}"


def _mark_done(run: _Run, page: _Page, tid: int, body: str) -> None:
    for attempt in (1, 2):
        try:
            new = mark_task_done(page.storage, tid, body)
            update_page_storage(run.atlassian, run.auth, run.retry, page.page_id, page.title, new, page.version,
                                _UPDATE_MESSAGE)
            page.storage, page.version = new, page.version + 1
            return
        except PageConflict:
            if attempt == 2:
                raise PageConflict("page update conflicted") from None
            page.storage, page.version, page.title = read_page_storage(run.atlassian, run.auth, run.retry, page.page_id)


def _settle(run: _Run, page: _Page, entry: PageEntry, action: str, result: str, error: str,
            out: ApplyResult) -> None:
    name = f"{entry.title}: {action}"
    if not error:
        try:
            _mark_done(run, page, task_id(entry.index, action), _done_body(run, page, action, result))
        except (RuntimeError, ValueError, LookupError, RequestFailed) as exc:
            error = f"page update failed: {exc}"
    if error:
        entry.actions[action] = ActionState("failed", result, "", error)
        out.failed.append(f"{name}: {error}")
    else:
        entry.actions[action] = ActionState("done", result, run.day)
        out.done.append(f"{name}: {result}")
    try:
        put_attachment(run.atlassian, run.auth, run.retry, page.page_id, ATTACHMENT_NAME,
                       to_json(page.data).encode("utf-8"))
    except (RuntimeError, RequestFailed) as exc:
        out.failed.append(f"{name}: data attachment could not be updated: {exc}")
    run.progress(f"  {name}: {'FAILED ' + error if error else result}")


def _do_frozen(run: _Run, entry: PageEntry, out: ApplyResult) -> str:
    try:
        run.client().add_label(entry.page_id, run.pw.freeze_label)
    except LookupError as exc:
        raise LookupError(f"spec page was deleted or renamed ({exc})") from None
    return run.pw.freeze_label


def _do_ticket(run: _Run, data: WatchData, entry: PageEntry, out: ApplyResult) -> str:
    key = find_ticket_by_label(run.atlassian, run.auth, run.retry, _ticket_label(data, entry))
    if key:
        return key
    tcfg = page_watch_ticket(run.config)
    result = create_ticket_detailed(run.config, build_page_ticket(data, entry, tcfg), tcfg=tcfg, retry=run.retry)
    key = result.url.rsplit("/", 1)[-1]
    if result.dropped:
        out.warnings.append(f"{entry.title}: ticket {key} was created WITHOUT: "
                            + ", ".join(f"{d.label} ({d.reason})" for d in result.dropped) + "; please set manually")
    out.failed += [f"{entry.title}: ticket {key} was created but {f}; please add manually" for f in result.link_failures]
    return key


def _apply_page(run: _Run, page_id: str) -> ApplyResult:
    page = _load(run, page_id)
    data = page.data
    out = ApplyResult(page.title)
    ticks = {tid for tid, status in parse_tasks(page.storage).items() if status == "complete"}
    if not ticks:
        return out

    def ticked(entry: PageEntry, action: str) -> bool:
        return action in entry.actions and task_id(entry.index, action) in ticks

    due: List[tuple] = []
    for entry in data.pages:
        for action in ACTIONS:
            if not ticked(entry, action):
                continue
            if entry.actions[action].status == "done":
                out.skipped += 1
            else:
                due.append((entry, action))
    if not due:
        return out
    run.progress(f"{page.title}: {len(due)} action(s) to do")

    for entry, action in due:
        if action == "cr":
            continue
        if run.dry_run:
            what = {"frozen": f"add label '{run.pw.freeze_label}' to", "ticket": "create (or reuse) a Jira ticket for"}[action]
            out.done.append(f"[dry-run] would {what} '{entry.title}'")
            run.progress(f"  [dry-run] would {what} '{entry.title}'")
            continue
        try:
            result = _do_frozen(run, entry, out) if action == "frozen" else _do_ticket(run, data, entry, out)
            error = ""
        except Exception as exc:
            result, error = "", str(exc) or type(exc).__name__
        _settle(run, page, entry, action, result, error, out)

    cr_due = [e for e, a in due if a == "cr"]
    if cr_due:
        members = [e for e in data.pages if "cr" in e.actions and (ticked(e, "cr") or e.actions["cr"].status == "done")]
        out_dir = run.config.data_dir / "page_watch"
        if run.dry_run:
            line = f"[dry-run] would write {out_dir / (data.file_stem + '.md')} with {len(members)} page(s)"
            out.done.append(line)
            run.progress(f"  {line}")
        else:
            try:
                name, error = write_cr_file(data, members, out_dir).name, ""
            except OSError as exc:
                name, error = "", f"CR file could not be written: {exc}"
            for entry in cr_due:
                _settle(run, page, entry, "cr", name, error, out)
    return out


def run_apply(config: Config, *, target: Optional[str], dry_run: bool, today: Optional[date] = None,
              progress: Callable[[str], None] = print, auth_header: Optional[str] = None) -> List[ApplyResult]:
    pw = config.page_watch
    if pw is None or not pw.publish_parent_page_id:
        raise ApplyFatal("page_watch.confluence_page.parent_page_id must be set in config.yaml to use --page-watch-apply")
    run = _Run(config, dry_run, today or date.today(), progress, auth_header)
    page_ids = _select(run, target)
    if not page_ids:
        progress("no report pages found for the last 8 weeks")
    return [_apply_page(run, pid) for pid in page_ids]
