"""Data attachment of a weekly page-watch report page and the Confluence task markup that references it."""
from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from digest.spec_monitor.models import PageStatus, PageWatchReport
from digest.spec_monitor.page_watch import is_frozen

SCHEMA_VERSION = 1
ACTIONS = ("frozen", "ticket", "cr")
ATTACHMENT_NAME = "page-watch-data.json"
REJECT = "reject"
_ACTION_CODE = {"frozen": 1, "ticket": 2, "cr": 3, REJECT: 4}
_CODE_ACTION = {v: k for k, v in _ACTION_CODE.items()}


@dataclass
class ActionState:
    status: str = "pending"  # pending | done | failed
    result: str = ""
    date: str = ""
    error: str = ""


@dataclass
class PageEntry:
    index: int
    page_id: str
    title: str
    url: str
    summary: str
    deltas: List[dict]
    open_points: List[str]
    trace: Optional[dict]
    show_frozen: bool
    actions: Dict[str, ActionState] = field(default_factory=dict)


@dataclass
class WatchData:
    schema: int
    week_label: str
    week_tag: str
    file_stem: str
    window_start: str
    window_end: str
    parent_title: str
    parent_url: str
    language: str
    pages: List[PageEntry] = field(default_factory=list)


def build_data(report: PageWatchReport, freeze_label: str) -> WatchData:
    week = report.week
    if week is None:
        raise ValueError("page-watch data needs a calendar-week report")
    syn = report.synthesis
    pages: List[PageEntry] = []
    for pc in report.with_status(PageStatus.CHANGED, PageStatus.NEW):
        trace = pc.trace
        show_frozen = not is_frozen(pc, freeze_label) and not (trace is not None and trace.status == "FROZEN")
        actions = {"ticket": ActionState(), "cr": ActionState()}
        if show_frozen:
            actions = {"frozen": ActionState(), **actions}
        title = pc.title.casefold()
        pages.append(PageEntry(
            index=len(pages) + 1, page_id=pc.page_id, title=pc.title, url=pc.url, summary=pc.summary,
            deltas=[asdict(c) for c in syn.delta_changes if c.page == pc.title] if syn else [],
            open_points=[p.question for p in syn.open_points if p.raised_by and title in p.raised_by.casefold()]
            if syn else [],
            trace=asdict(trace) if trace is not None else None,
            show_frozen=show_frozen, actions=actions))
    return WatchData(
        schema=SCHEMA_VERSION, week_label=week.label, week_tag=week.tag, file_stem=week.file_stem,
        window_start=(report.window_start or week.start).isoformat(),
        window_end=(report.window_end or week.end).isoformat(),
        parent_title=report.parent_title, parent_url=report.parent_url, language=report.language, pages=pages)


def to_json(data: WatchData) -> str:
    return json.dumps(asdict(data), ensure_ascii=False, indent=2)


def from_json(text: str) -> WatchData:
    try:
        raw = json.loads(text)
    except (TypeError, ValueError) as e:
        raise ValueError(f"page-watch data is not valid JSON: {e}") from None
    if not isinstance(raw, dict):
        raise ValueError("page-watch data must be a JSON object")
    if raw.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"unsupported page-watch data schema {raw.get('schema')!r} (expected {SCHEMA_VERSION})")
    try:
        pages = []
        for p in raw["pages"]:
            p = dict(p)
            p["actions"] = {k: ActionState(**v) for k, v in p["actions"].items()}
            pages.append(PageEntry(**p))
        return WatchData(**{**raw, "pages": pages})
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"page-watch data is malformed: {type(e).__name__}: {e}") from None


def task_id(entry_index: int, action: str) -> int:
    return entry_index * 10 + _ACTION_CODE[action]


def task_action(tid: int) -> Tuple[int, str]:
    action = _CODE_ACTION.get(tid % 10)
    if action is None or tid < 10:
        raise ValueError(f"not a page-watch task id: {tid}")
    return tid // 10, action


_TASK = re.compile(r"<ac:task>(.*?)</ac:task>", re.DOTALL)
_ID = re.compile(r"<ac:task-id>\s*(\d+)\s*</ac:task-id>")
_STATUS = re.compile(r"(<ac:task-status>)\s*[a-z]*\s*(</ac:task-status>)")
_BODY = re.compile(r"(<ac:task-body>).*?(</ac:task-body>)", re.DOTALL)


def parse_tasks(storage: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for m in _TASK.finditer(storage):
        tid = _ID.search(m.group(1))
        status = re.search(r"<ac:task-status>\s*([a-z]*)\s*</ac:task-status>", m.group(1))
        if tid and status:
            out[int(tid.group(1))] = status.group(1)
    return out


def _done_line(state: ActionState, labels: Dict[str, str]) -> str:
    text = " ".join(x for x in (labels["done"], state.result, state.date) if x)
    return f"<p>✔ {html.escape(text, quote=False)}</p>"


def render_tasks(entry: PageEntry, labels: Dict[str, str]) -> str:
    link = f'<a href="{html.escape(entry.url, quote=True)}">{html.escape(entry.title, quote=False)}</a>'
    tasks: List[str] = [
        f"<ac:task><ac:task-id>{task_id(entry.index, REJECT)}</ac:task-id>"
        f"<ac:task-status>incomplete</ac:task-status>"
        f"<ac:task-body>{html.escape(labels[REJECT], quote=False)}: {link}</ac:task-body></ac:task>"]
    lines: List[str] = []
    for action in ACTIONS:
        state = entry.actions.get(action)
        if state is None or (action == "frozen" and not entry.show_frozen):
            continue
        if state.status == "done":
            lines.append(_done_line(state, labels))
        else:
            tasks.append(
                f"<ac:task><ac:task-id>{task_id(entry.index, action)}</ac:task-id>"
                f"<ac:task-status>incomplete</ac:task-status>"
                f"<ac:task-body>{html.escape(labels[action], quote=False)}: {link}</ac:task-body></ac:task>")
    task_list = f"<ac:task-list>{''.join(tasks)}</ac:task-list>"
    return "".join(lines) + task_list


def mark_task_done(storage: str, tid: int, body_html: str) -> str:
    found = False

    def patch(m: re.Match) -> str:
        nonlocal found
        inner = m.group(1)
        t = _ID.search(inner)
        if not t or int(t.group(1)) != tid:
            return m.group(0)
        found = True
        inner = _STATUS.sub(lambda s: f"{s.group(1)}complete{s.group(2)}", inner)
        inner = _BODY.sub(lambda b: f"{b.group(1)}{body_html}{b.group(2)}", inner)
        return f"<ac:task>{inner}</ac:task>"

    out = _TASK.sub(patch, storage)
    if not found:
        raise ValueError(f"task {tid} not found in the page")
    return out
