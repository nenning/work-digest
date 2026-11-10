"""--update-spec [SPEC]: brings one spec page (or, without a value, every analysed spec) up to the latest source versions.

Rewrites the Quellen table ('Gelesene Version' cells), the 'Stand:' date of the status panel and,
where it is safe, text-only sections named by the delta spec (LLM, validated). A preview is always
written. Unless --dry-run, the result is published as a NEW sibling page (dated title, info banner,
page-properties id renamed so it is not a second spec); the original page is never modified.
"""
import html as _html
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, List, Optional, Sequence, Tuple

from digest import summarizer
from digest.auth.atlassian import get_auth_header
from digest.config import spec_llm
from digest.spec_monitor import analysis, runner, textdiff
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import Classification, DeltaChange, SourceKind, SourceResult
from digest.spec_monitor.publish import publish_page
from digest.spec_monitor.spec_ref import UPDATE_ALL
from digest.spec_monitor.parser import (
    _COMPACT_VER_RE,
    _VER_RE,
    _Link,
    _cell,
    _column,
    _find_dates,
    _parse_version_cell,
    _plain,
    _rows,
    _tables,
    _to_text,
)

_ID_RE = re.compile(r"\b[A-Z]{2,}(?:-[A-Z]+)*-\d+\b")
_VOID_TAGS = {"br", "hr", "img", "col", "input", "wbr"}
_TAG_RE = re.compile(r"<(/?)([A-Za-z][\w:.\-]*)\b[^>]*?(/?)>")
_MACRO_RE = re.compile(r"</?(?:ac|ri):", re.I)
_BARE_AMP_RE = re.compile(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)")
_NUM_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")
_SHA_RE = re.compile(r"[0-9a-f]{7,40}", re.I)
_SECTION_NUM_RE = re.compile(r"(?:(?:abschnitt|kapitel|section|ziffer)\s+)?(\d+(?:\.\d+)*)\.?(?!\d)", re.I)
_EMPTY_VERSION_CELLS = {"", "-", "–", "—", "nicht erfasst"}
_SECTION_MAX_CHARS = 20000
_REWRITE_MAX_TOKENS = 8000

_BLOCKED_CLASSES = (Classification.ERROR, Classification.NOT_MONITORED, Classification.EXCLUDED)


def _today() -> date:
    return date.today()


def _now() -> datetime:
    return datetime.now()


def _fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


# --------------------------------------------------------------------------- source states

def _stated(result: SourceResult) -> Optional[str]:
    ref = result.ref
    return ref.repo_commit if ref.kind == SourceKind.REPO else ref.baseline_version


def _same_version(result: SourceResult, stated: str) -> bool:
    new = result.new.version.strip().lstrip("vV").lower()
    old = stated.strip().lstrip("vV").lower()
    if result.ref.kind == SourceKind.REPO:
        return bool(old) and (new.startswith(old) or old.startswith(new))
    return old == new


def _token(result: SourceResult) -> Optional[str]:
    """Version as written in a cell (no date); None when it cannot be expressed."""
    ref, version = result.ref, result.new.version.strip()
    if ref.kind == SourceKind.REPO:
        if not _SHA_RE.fullmatch(version):
            return None
        out = f"Commit {version[:12]}"
        if ref.repo_branch:
            out += f", Branch {_html.escape(ref.repo_branch)}"
        return out
    return f"v{version}" if _NUM_VERSION_RE.fullmatch(version) else None


def _new_date(result: SourceResult) -> Optional[date]:
    return result.new.when.date() if result.new and result.new.when else None


def _cell_text(result: SourceResult) -> str:
    text = _token(result) or ""
    d = _new_date(result)
    return f"{text} ({_fmt_date(d)})" if d else text


def _state(result: SourceResult) -> str:
    """upd (cell must change) | keep (already current) | blocked (cannot be updated automatically)."""
    ref = result.ref
    if result.classification in _BLOCKED_CLASSES or result.new is None or ref.parse_issue:
        return "blocked"
    if "[Kapitel" in (ref.used_for or ""):
        return "blocked"
    stated = _stated(result)
    if stated and _same_version(result, stated):
        return "keep"
    if result.classification == Classification.UNCHANGED and result.old and _same_version(result, result.old.version):
        return "keep"
    if _token(result) is None:
        return "keep" if result.classification == Classification.UNCHANGED else "blocked"
    return "upd"


def _manual_reason(result: SourceResult) -> str:
    ref = result.ref
    if result.classification in _BLOCKED_CLASSES:
        detail = (result.errors or result.warnings or [ref.excluded_reason or ""])[0]
        return f"{ref.name} ({result.classification.value}): {detail}".rstrip(": ") + " - check 'Gelesene Version' manually"
    if ref.parse_issue:
        return f"{ref.name}: {ref.parse_issue} - update the version cell manually"
    if "[Kapitel" in (ref.used_for or ""):
        return f"{ref.name}: listed via a chapter reference - update the version cell manually"
    if result.new is None:
        return f"{ref.name}: new version could not be determined - check 'Gelesene Version' manually"
    if _token(result) is None:
        return f"{ref.name}: new version '{result.new.version}' cannot be written as a version cell - update manually"
    return f"{ref.name}: version cell could not be rewritten automatically - update manually"


# --------------------------------------------------------------------------- Quellen table

_TIME_TAG_RE = re.compile(r"<time\b[^>]*?datetime\s*=\s*([\"'])([^\"']*)\1[^>]*>", re.I)


def _blank_tag(m: "re.Match") -> str:
    tag = m.group(0)
    t = _TIME_TAG_RE.fullmatch(tag)
    if not t:
        return "\x00" * len(tag)
    s, e = t.span(2)
    return "\x00" * s + tag[s:e] + "\x00" * (len(tag) - e)


def _mask(html: str) -> str:
    """Same-length copy where tags and nbsp entities cannot take part in regex matches.

    Only the datetime value of a <time> lozenge stays visible: it is a date like any other."""
    return re.sub(r"<[^>]*>", _blank_tag, html).replace("&nbsp;", "      ")


def _fmt_like(inner: str, start: int, new: date) -> str:
    return new.isoformat() if inner[start - 1:start] in ("\"", "'") else _fmt_date(new)


def _norm_url(url: Optional[str]) -> str:
    return (url or "").rstrip("/").lower()


def _match_link(link: _Link, results: Sequence[SourceResult]) -> Optional[SourceResult]:
    for r in results:
        ref = r.ref
        if link.kind == "confluence" and ref.kind == SourceKind.CONFLUENCE:
            if link.page_id and ref.page_id == link.page_id:
                return r
            if link.title and ref.title and link.title.strip().lower() == ref.title.strip().lower():
                return r
        elif link.kind == "url" and link.url and ref.url and _norm_url(link.url) == _norm_url(ref.url):
            return r
    return None


def _row_members(src_html: str, ver_text: str, results: Sequence[SourceResult]) -> List[SourceResult]:
    src = _cell(src_html)
    if src.links:
        members = []
        for link in src.links:
            r = _match_link(link, results)
            if r is not None:
                members.append(r)
        return members
    low = src.text.lower()
    return [r for r in results if r.ref.version_text == ver_text and r.ref.name and r.ref.name.lower() in low]


def _wrap_like(old_inner: str, text: str) -> str:
    m = re.match(r"\s*<p\b([^>]*)>", old_inner, re.I)
    return f"<p{m.group(1)}>{text}</p>" if m else text


def _apply_edits(text: str, edits: List[Tuple[int, int, str]]) -> str:
    for s, e, repl in sorted(edits, key=lambda x: -x[0]):
        text = text[:s] + repl + text[e:]
    return text


def _positional_cell(inner: str, members: List[SourceResult], states: List[str]) -> Optional[str]:
    masked = _mask(inner)
    if _COMPACT_VER_RE.search(masked):
        return None
    ver_matches = list(_VER_RE.finditer(masked))
    dates = _find_dates(masked)
    if [d for _, _, d in dates] != _parse_version_cell(_cell(inner).text)[1]:
        return None
    n = len(members)
    edits: List[Tuple[int, int, str]] = []
    expected = [m.group(1) for m in ver_matches]
    expected_dates = [d for _, _, d in dates]
    for i, (m, r, st) in enumerate(zip(ver_matches, members, states)):
        if st != "upd":
            continue
        new_num = r.new.version.strip()
        edits.append((m.start(1), m.end(1), new_num))
        expected[i] = new_num
        stop = ver_matches[i + 1].start() if i + 1 < n else len(masked)
        for j, (ds, de, _) in enumerate(dates):
            if m.end() <= ds < stop:
                nd = _new_date(r)
                if nd is None:
                    return None
                edits.append((ds, de, _fmt_like(inner, ds, nd)))
                expected_dates[j] = nd
                break
    new_inner = _apply_edits(inner, edits)
    versions, new_dates = _parse_version_cell(_cell(new_inner).text)
    if versions != expected or new_dates != expected_dates:
        return None
    return new_inner


def _shared_date_cell(inner: str, members: List[SourceResult], today: date) -> Optional[str]:
    found = _find_dates(_mask(inner))
    if len(found) != 1:
        return None
    ds, de, _ = found[0]
    when = [d for d in (_new_date(r) for r in members) if d]
    new = max(when) if when else today
    iso = re.fullmatch(r"\d{4}-\d{2}-\d{2}", inner[ds:de]) is not None
    new_inner = _apply_edits(inner, [(ds, de, new.isoformat() if iso else _fmt_date(new))])
    return new_inner if _parse_version_cell(_cell(new_inner).text)[1] == [new] else None


def _rewrite_row(inner: str, members: List[SourceResult], states: List[str], today: date) -> Optional[str]:
    n = len(members)
    if n == 1:
        return _wrap_like(inner, _cell_text(members[0])) if states[0] == "upd" else None
    text = _cell(inner).text
    versions, dates = _parse_version_cell(text)
    if len(versions) == n:
        return _positional_cell(inner, members, states)
    if versions:
        return None
    if len(dates) == 1:
        return _shared_date_cell(inner, members, today) if "blocked" not in states else None
    if not dates and text.strip().lower() in _EMPTY_VERSION_CELLS and all(s == "upd" for s in states):
        if any(_token(r) is None or _token(r).startswith("Commit") for r in members):
            return None
        return _wrap_like(inner, ", ".join(_token(r) for r in members))
    return None


def _find_table_span(storage: str) -> Optional[Tuple[int, int]]:
    heads = [(m.start(), _plain(m.group(2)))
             for m in re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", storage, re.S | re.I)]
    candidates = []
    for pos, table in _tables(storage):
        rows = _rows(table)
        if rows and any("quelle" in _to_text(c, " ").lower() for c in rows[0]):
            candidates.append((pos, pos + len(table)))
    if not candidates:
        return None
    for pos, end in candidates:
        preceding = [t for p, t in heads if p < pos]
        if preceding and "quelle" in preceding[-1].lower():
            return pos, end
    return candidates[0]


def update_sources_table(
    storage: str, results: Sequence[SourceResult], today: Optional[date] = None,
) -> Tuple[str, List[str], List[str]]:
    """Rewrites the 'Gelesene Version' cells; returns (new_storage, changes, manual)."""
    today = today or _today()
    results = list(results)
    changes: List[str] = []
    applied: set = set()
    edits: List[Tuple[int, int, str]] = []

    span = _find_table_span(storage)
    if span is None:
        manual = ["no Quellen table found on the page - update the source versions manually"]
        return storage, changes, manual
    start, end = span
    table = storage[start:end]
    row_matches = list(re.finditer(r"<tr\b[^>]*>(.*?)</tr>", table, re.S | re.I))
    cell_re = re.compile(r"<(t[hd])\b[^>]*>(.*?)</\1>", re.S | re.I)
    if row_matches:
        header = [m.group(2) for m in cell_re.finditer(row_matches[0].group(1))]
        src_col, ver_col = _column(header, "quelle", 0), _column(header, "version", 1)
        for rm in row_matches[1:]:
            cells = list(cell_re.finditer(rm.group(1)))
            if len(cells) <= max(src_col, ver_col):
                continue
            ver_m = cells[ver_col]
            ver_inner = ver_m.group(2)
            ver_text = _cell(ver_inner).text
            ver_text = "" if ver_text in ("-", "–", "—") else ver_text
            members = _row_members(cells[src_col].group(2), ver_text, results)
            states = [_state(r) for r in members]
            if "upd" not in states:
                continue
            new_inner = _rewrite_row(ver_inner, members, states, today)
            if new_inner is None or new_inner == ver_inner:
                continue
            abs_start = start + rm.start(1) + ver_m.start(2)
            edits.append((abs_start, abs_start + len(ver_inner), new_inner))
            for r, st in zip(members, states):
                if st == "upd":
                    applied.add(id(r))
            names = ", ".join(r.ref.name for r, st in zip(members, states) if st == "upd")
            changes.append(f"{names}: '{ver_text or 'nicht erfasst'}' -> '{_cell(new_inner).text}'")

    manual: List[str] = []
    for r in results:
        if id(r) in applied:
            continue
        if r.classification in _BLOCKED_CLASSES or _state(r) in ("upd", "blocked"):
            line = _manual_reason(r)
            if line not in manual:
                manual.append(line)
    return _apply_edits(storage, edits), changes, manual


# --------------------------------------------------------------------------- Stand date

_STAND_RE = re.compile(
    r"(Stand\s*:(?:(?!</p>|</li>|</td>|</th>|<h[1-6]\b).)*?<time\b[^>]*?datetime\s*=\s*)([\"'])([^\"']*)\2",
    re.S | re.I,
)


def update_stand_date(storage: str, today: Optional[date] = None) -> Tuple[str, Optional[str]]:
    """Sets the <time datetime> element behind 'Stand:' to today; returns (storage, change or None)."""
    today = today or _today()
    iso = today.isoformat()
    m = _STAND_RE.search(storage)
    if not m or m.group(3) == iso:
        return storage, None
    new = storage[:m.start()] + f"{m.group(1)}{m.group(2)}{iso}{m.group(2)}" + storage[m.end():]
    return new, f"Stand: {m.group(3)} -> {iso}"


# --------------------------------------------------------------------------- text sections

@dataclass
class _Section:
    heading: str
    body_start: int
    body_end: int


def _sections(storage: str) -> List[_Section]:
    heads = list(re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", storage, re.S | re.I))
    out = []
    for i, m in enumerate(heads):
        stop = heads[i + 1].start() if i + 1 < len(heads) else len(storage)
        out.append(_Section(_plain(m.group(2)), m.end(), stop))
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _number(text: str) -> Optional[str]:
    m = _SECTION_NUM_RE.match(_norm(text))
    return m.group(1) if m else None


def _match_heading(affected: str, sections: List[_Section]) -> Tuple[Optional[int], str]:
    """(index, '') on a unique match; (None, reason) when nothing or several headings match."""
    target = _norm(affected)
    if not target:
        return None, "no section named"
    num = _number(affected)
    rules = (
        lambda s: _norm(s.heading) == target,
        lambda s: num is not None and _number(s.heading) == num,
        lambda s: len(target) >= 4 and (target in _norm(s.heading) or (len(_norm(s.heading)) >= 4 and _norm(s.heading) in target)),
    )
    for rule in rules:
        hits = [i for i, s in enumerate(sections) if rule(s)]
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, "several headings match"
    return None, "no heading matches"


def validate_rewrite(old_body: str, new_body: str, allowed_lost_ids: Sequence[str] = ()) -> None:
    """Raises ValueError unless new_body is a safe replacement of old_body."""
    if not new_body or not new_body.strip():
        raise ValueError("empty result")
    if new_body.strip() == old_body.strip():
        raise ValueError("no change produced")
    if _MACRO_RE.search(new_body):
        raise ValueError("introduces ac:/ri: markup")
    if re.search(r"<\s*(?:h[1-6]|script|style|iframe)\b|<!", new_body, re.I):
        raise ValueError("contains headings, scripts or comments")
    if _BARE_AMP_RE.search(new_body) or re.search(r"<(?![/A-Za-z])", new_body):
        raise ValueError("not well-formed (stray '<' or '&')")
    stack: List[str] = []
    for m in _TAG_RE.finditer(new_body):
        closing, name, selfclose = m.group(1), m.group(2).lower(), m.group(3)
        if selfclose or name in _VOID_TAGS:
            continue
        if closing:
            if not stack or stack.pop() != name:
                raise ValueError(f"unbalanced tag </{name}>")
        else:
            stack.append(name)
    if stack:
        raise ValueError(f"unclosed tag <{stack[-1]}>")
    old_ids = set(_ID_RE.findall(_html.unescape(re.sub(r"<[^>]+>", " ", old_body))))
    new_ids = set(_ID_RE.findall(_html.unescape(re.sub(r"<[^>]+>", " ", new_body))))
    lost = old_ids - new_ids - set(allowed_lost_ids)
    if lost:
        raise ValueError("IDs lost: " + ", ".join(sorted(lost)))


def _removed_ids(changes: Sequence[DeltaChange]) -> List[str]:
    return [i for c in changes if c.kind == "removed" for i in c.ids]


def _describe(c: DeltaChange) -> str:
    ids = f" [{', '.join(c.ids)}]" if c.ids else ""
    return f"{c.kind}{ids}: {c.change or c.current_statement}".strip()[:200]


def rewrite_sections(
    storage: str,
    changes: Sequence[DeltaChange],
    rewrite: Callable[[str, List[DeltaChange]], str],
) -> Tuple[str, List[str], List[str]]:
    """Applies delta changes to text-only sections via `rewrite(old_body, changes) -> new_body`.

    Returns (new_storage, changes, manual). Sections with macros, unmatched headings and
    rewrites that fail validation (or raise) are listed as manual and left untouched."""
    sections = _sections(storage)
    done: List[str] = []
    manual: List[str] = []
    grouped: dict = {}
    for c in changes:
        idx, reason = _match_heading(c.affected_part, sections)
        if idx is None:
            manual.append(f"Section '{c.affected_part}': {reason} - apply manually: {_describe(c)}")
        else:
            grouped.setdefault(idx, []).append(c)

    edits: List[Tuple[int, int, str]] = []
    for idx in sorted(grouped):
        sec, group = sections[idx], grouped[idx]
        body = storage[sec.body_start:sec.body_end]
        what = "; ".join(_describe(c) for c in group)
        if _MACRO_RE.search(body):
            manual.append(f"Section '{sec.heading}': contains macros - apply manually: {what}")
            continue
        if len(body) > _SECTION_MAX_CHARS:
            manual.append(f"Section '{sec.heading}': too large for automatic rewrite - apply manually: {what}")
            continue
        try:
            new_body = rewrite(body, group)
            validate_rewrite(body, new_body, _removed_ids(group))
        except (AnalysisError, ValueError) as exc:
            manual.append(f"Section '{sec.heading}': automatic rewrite rejected ({exc}) - apply manually: {what}")
            continue
        edits.append((sec.body_start, sec.body_end, new_body))
        done.append(f"Section '{sec.heading}' rewritten ({len(group)} change(s))")
    return _apply_edits(storage, edits), done, manual


def make_llm_rewriter(llm, language: str) -> Callable[[str, List[DeltaChange]], str]:
    timeout = llm.llm_timeout
    lang = summarizer._language_name(language)

    def rewrite(body: str, group: List[DeltaChange]) -> str:
        change_lines = []
        for c in group:
            change_lines.append(json.dumps({
                "kind": c.kind, "ids": c.ids, "current_statement": c.current_statement,
                "change": c.change, "reason": c.reason,
            }, ensure_ascii=False))
        allowed = _removed_ids(group)
        prompt = (
            "You update one section of a Confluence specification page. Below are the section body "
            "(Confluence storage-format XHTML, without its heading) and the changes the specification "
            "must reflect. Rewrite the body so that it reflects exactly these changes, with minimal "
            "edits: leave every statement the changes do not touch unchanged, character for character; "
            "keep the existing tags and structure (lists, tables, paragraphs, formatting).\n"
            "Rules: keep every requirement and decision ID (e.g. CM-FR-03, CM-D-11) verbatim "
            "- an ID may only disappear if its change has kind 'removed'; do not add Confluence macros "
            "or links (no ac: or ri: elements), headings, comments or scripts; every tag must be closed; "
            "escape & and < as entities; write new text in the style of the surrounding text "
            f"in {lang} and keep original English technical terms.\n"
            f"{analysis._DATA_NOTE}\n\n"
            + analysis._quote("changes (JSON, one per line)", "\n".join(change_lines)) + "\n\n"
            + analysis._quote("section body", body)
            + '\n\nRespond with JSON only: {"storage": "<the complete rewritten section body>"}'
        )

        def validate(data):
            if not isinstance(data, dict) or not isinstance(data.get("storage"), str):
                raise ValueError("missing storage")
            validate_rewrite(body, data["storage"], allowed)
            return data["storage"]

        new_body, _ = analysis._ask_json(prompt, llm, timeout, _REWRITE_MAX_TOKENS, validate, "section_rewrite")
        return new_body

    return rewrite


# --------------------------------------------------------------------------- new page

_DETAILS_HEAD_RE = re.compile(
    r'<ac:structured-macro\b[^>]*\bac:name="details"[^>]*>(?P<head>.*?)(?:<ac:rich-text-body>|</ac:structured-macro>)',
    re.S,
)
_ID_PARAM_RE = re.compile(r'(<ac:parameter\s+ac:name="id"\s*>)(?P<value>.*?)(</ac:parameter>)', re.S)
_ID_SUFFIX = "-UPDATE"


def rename_details_macro_ids(storage: str) -> Tuple[str, List[str]]:
    """Appends '-UPDATE' to the id parameter of every `details` macro; returns (storage, original ids).

    The copy then no longer matches the overview's page-properties id (and the monitor's selector)."""
    originals: List[str] = []
    edits: List[Tuple[int, int, str]] = []
    for m in _DETAILS_HEAD_RE.finditer(storage):
        head_start = m.start("head")
        for p in _ID_PARAM_RE.finditer(m.group("head")):
            value = p.group("value")
            originals.append(value.strip())
            edits.append((head_start + p.start("value"), head_start + p.end("value"), value.strip() + _ID_SUFFIX))
    return _apply_edits(storage, edits), originals


def _ul(items: Sequence[str]) -> str:
    return "<ul>" + "".join(f"<li>{_html.escape(i)}</li>" for i in items) + "</ul>" if items else ""


def build_banner(title: str, original_url: str, today: date, changes: Sequence[str],
                 manual: Sequence[str], original_ids: Sequence[str]) -> str:
    """German info macro placed at the top of the new page; all text is escaped."""
    link = f'<a href="{_html.escape(original_url, quote=True)}">{_html.escape(title)}</a>'
    ids = ", ".join(dict.fromkeys(original_ids))
    parts = [
        f"<p>Dies ist ein aktualisierter Entwurf, erstellt am {today.isoformat()} vom Spec-Source-Monitor "
        f"aus der Originalseite {link}. Die Originalseite wurde nicht verändert.</p>",
        "<p><strong>Automatisch geänderte Stellen:</strong></p>" + (_ul(changes) or "<p>keine</p>"),
        "<p><strong>Manuell nachzuarbeiten:</strong></p>" + (_ul(manual) or "<p>keine</p>"),
    ]
    if ids:
        parts.append(
            "<p>Zum Übernehmen den Inhalt dieser Seite in die Originalseite kopieren und die Page-Properties-"
            f"Makro-ID von {_html.escape(', '.join(i + _ID_SUFFIX for i in dict.fromkeys(original_ids)))} "
            f"wieder auf {_html.escape(ids)} zurückbenennen.</p>")
    else:
        parts.append("<p>Zum Übernehmen den Inhalt dieser Seite in die Originalseite kopieren.</p>")
    return ('<ac:structured-macro ac:name="info" ac:schema-version="1"><ac:rich-text-body>'
            + "".join(parts) + "</ac:rich-text-body></ac:structured-macro>")


def _is_duplicate_title(exc: Exception) -> bool:
    text = str(exc).lower()
    return "http 400" in text and ("already exists" in text or "title" in text)


def _publish_new_page(config, auth: str, spec, storage: str, today: date) -> str:
    if not spec.ancestor_ids:
        raise RuntimeError(f"Cannot create the updated page: '{spec.title}' has no parent page to put it next to")
    parent = spec.ancestor_ids[-1]
    message = f"Spec source monitor: sources updated to {today.isoformat()}"
    retry = config.spec_monitor.retry
    title = f"{spec.title} (aktualisiert {today.isoformat()})"
    try:
        return publish_page(config.atlassian, auth, retry, parent, title, storage, message)
    except RuntimeError as exc:
        if not _is_duplicate_title(exc):
            raise
    title = f"{spec.title} (aktualisiert {_now().strftime('%Y-%m-%d %H:%M')})"
    try:
        return publish_page(config.atlassian, auth, retry, parent, title, storage, message)
    except RuntimeError as exc:
        if _is_duplicate_title(exc):
            raise RuntimeError(f"A page titled '{title}' already exists as well; "
                               f"rename or delete the earlier updated copy and run again ({exc})") from exc
        raise


def _write_preview(config, spec, old_storage: str, new_storage: str, changes: List[str], manual: List[str]):
    out_dir = config.data_dir / "spec_cache"
    out_dir.mkdir(parents=True, exist_ok=True)
    diff_path = out_dir / f"spec-update-{spec.page_id}.diff.txt"
    storage_path = out_dir / f"spec-update-{spec.page_id}.storage.html"
    diff = textdiff.diff_text(textdiff.storage_to_text(old_storage), textdiff.storage_to_text(new_storage))
    lines = [diff or "(no visible text difference)", "", "Changes:"]
    lines += [f"  - {c}" for c in changes] or ["  (none)"]
    lines += ["", "Manual follow-up:"] + ([f"  - {m}" for m in manual] or ["  (none)"])
    diff_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    storage_path.write_text(new_storage, encoding="utf-8")
    return diff_path, storage_path


# --------------------------------------------------------------------------- entry point

_PROBLEM_STATUSES = ("failed", "manual")


@dataclass
class UpdateOutcome:
    title: str
    status: str            # created | preview | nothing | manual | failed
    detail: str = ""       # url (created), reason (nothing / manual / failed)

    def describe(self) -> str:
        return {
            "created": f"created {self.detail}",
            "preview": "preview only",
            "nothing": f"nothing to update ({self.detail})" if self.detail else "nothing to update",
            "manual": f"NOT UPDATED, manual follow-up needed: {self.detail}",
            "failed": f"failed: {self.detail}",
        }[self.status]


def _unchecked_sources(spec_analysis) -> Tuple[List[str], bool]:
    """(reasons, any_error): sources whose state could not be established or applied, so that
    'no source needs an update' cannot be concluded for them. Deliberately excluded sources do not count."""
    reasons: List[str] = []
    any_error = False
    for r in spec_analysis.sources:
        if r.classification == Classification.EXCLUDED:
            continue
        unchecked = (r.classification in (Classification.ERROR, Classification.NOT_MONITORED) or r.new is None
                     or (r.classification == Classification.CHANGED and _state(r) == "blocked"))
        if unchecked:
            reasons.append(_manual_reason(r))
            any_error = any_error or r.classification == Classification.ERROR
    if not spec_analysis.sources:
        reasons = ["no sources found on the page"] + list(spec_analysis.completeness_warnings)
        any_error = True
    return reasons, any_error


def _update_one(spec_analysis, config, today: date, dry_run: bool) -> UpdateOutcome:
    """Updates one analysed spec (preview files + new sibling page). Raises on failure."""
    spec = spec_analysis.spec
    for e in spec_analysis.errors:
        print(f"WARNING {e.scope}: {e.message}")

    if not any(_state(r) == "upd" for r in spec_analysis.sources):
        reasons, any_error = _unchecked_sources(spec_analysis)
        if reasons:
            print(f"ERROR {spec.title}: cannot confirm that the sources are up to date:", file=sys.stderr)
            for line in reasons:
                print(f"  - {line}", file=sys.stderr)
            return UpdateOutcome(spec.title, "failed" if any_error else "manual", "; ".join(reasons))
        print(f"{spec.title}: nothing to update (every source is at the stated version)")
        return UpdateOutcome(spec.title, "nothing", "every source is at the stated version")

    old_storage = spec.body_storage
    storage, changes, manual = update_sources_table(old_storage, spec_analysis.sources, today)
    storage, stand_change = update_stand_date(storage, today)
    if stand_change:
        changes.append(stand_change)
    elif not _STAND_RE.search(storage):
        manual.append("Stand date not found on the page ('Stand:' followed by a date) - update it manually")
    if spec_analysis.delta_changes:
        storage, sec_changes, sec_manual = rewrite_sections(
            storage, spec_analysis.delta_changes, make_llm_rewriter(spec_llm(config), config.language))
        changes += sec_changes
        manual += sec_manual
    elif spec_analysis.analysis_incomplete:
        manual.append("no delta spec available (analysis incomplete) - spec text was not updated")

    diff_path, storage_path = _write_preview(config, spec, old_storage, storage, changes, manual)
    print(f"\nSpec update preview for '{spec.title}':")
    for c in changes:
        print(f"  changed: {c}")
    if not changes:
        print("  (no automatic changes)")
    if manual:
        print("  manual follow-up needed:")
        for m in manual:
            print(f"    - {m}")
    print(f"Diff written to {diff_path}")
    print(f"New storage written to {storage_path}")

    if storage == old_storage:
        print("Nothing could be changed automatically; no page created.")
        if manual:
            return UpdateOutcome(spec.title, "manual", f"{len(manual)} manual change(s) pending, see the list above")
        return UpdateOutcome(spec.title, "nothing", "no automatic changes")
    if dry_run:
        print("Preview only (--dry-run): no page created")
        return UpdateOutcome(spec.title, "preview")

    new_body, original_ids = rename_details_macro_ids(storage)
    banner = build_banner(spec.title, spec.url, today, changes, manual, original_ids)
    url = _publish_new_page(config, get_auth_header(config.atlassian), spec, banner + new_body, today)
    print(f"Updated copy created: {url}")
    return UpdateOutcome(spec.title, "created", url)


def _failure_reason(exc: Exception) -> str:
    return str(exc) if isinstance(exc, (RequestFailed, AnalysisError, RuntimeError, LookupError, ValueError))         else f"unexpected {type(exc).__name__}: {exc}"


def _update_all(config, today: date, dry_run: bool) -> None:
    report = runner.run_spec_monitor(config, only=None, command_line="--update-spec", dry_run=dry_run)
    if not report.analysed:
        print("No spec was analysed (nothing to update)")
        return
    outcomes: List[UpdateOutcome] = []
    for spec_analysis in report.analysed:
        spec = spec_analysis.spec
        if not spec_analysis.sources and spec_analysis.analysis_incomplete:
            reason = spec_analysis.errors[0].message if spec_analysis.errors else "analysis incomplete"
            print(f"ERROR {spec.title}: {reason}", file=sys.stderr)
            outcomes.append(UpdateOutcome(spec.title, "failed", reason))
            continue
        try:
            outcomes.append(_update_one(spec_analysis, config, today, dry_run))
        except Exception as exc:  # noqa: BLE001 - one failing spec must not stop the others
            reason = _failure_reason(exc)
            print(f"ERROR {spec.title}: {reason}", file=sys.stderr)
            outcomes.append(UpdateOutcome(spec.title, "failed", reason))

    width = max(len(o.title) for o in outcomes)
    print("\nSpec update summary:")
    for o in outcomes:
        print(f"  {o.title.ljust(width)}  {o.describe()}")
    if any(o.status in _PROBLEM_STATUSES for o in outcomes):
        raise SystemExit(1)


def _run(args, config) -> None:
    sm = config.spec_monitor
    if sm is None:
        raise RuntimeError("No spec_monitor block configured. Add one to config.yaml to use --update-spec.")
    dry_run = bool(getattr(args, "dry_run", False))
    today = _today()

    if args.update_spec == UPDATE_ALL:
        _update_all(config, today, dry_run)
        return

    report = runner.run_spec_monitor(config, only=args.update_spec, command_line=f"--update-spec {args.update_spec}", dry_run=dry_run)
    if not report.analysed:
        raise RuntimeError(f"No spec matching '{args.update_spec}' could be analysed")
    outcome = _update_one(report.analysed[0], config, today, dry_run)
    if outcome.status in _PROBLEM_STATUSES:
        print(f"ERROR: {outcome.title}: {outcome.describe()}", file=sys.stderr)
        raise SystemExit(1)


def run_update_spec(args, config) -> None:
    try:
        _run(args, config)
    except SystemExit:
        raise
    except (RequestFailed, AnalysisError, runner.SpecMonitorFatal, RuntimeError, LookupError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - always end with a clear message
        print(f"ERROR: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
