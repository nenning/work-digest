"""Rendering of a PageWatchReport: HTML mail / page (Jinja2) and Confluence storage format."""
from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace
from typing import Dict, List

from jinja2 import Environment, FileSystemLoader

from digest.email_sender import TEMPLATES_DIR, _safe_url
from digest.spec_monitor import report as base
from digest.spec_monitor.models import PageChange, PageStatus, PageWatchReport, ReportError

_DE: Dict[str, str] = dict(base._DE)
_DE.update({
    "title": "Seitenänderungen der Spezifikation",
    "window": "Zeitraum",
    "window_to": "bis",
    "summary_title": "Gesamtzusammenfassung",
    "themes": "Zusammengehörige Änderungen",
    "pages": "Seiten",
    "checked": "Geprüfte Seiten",
    "changed_pages": "Geändert",
    "new_pages": "Neu",
    "quiet": "Seiten ohne relevante Änderung (unverändert oder nur kosmetisch, nicht aufgelistet)",
    "error_pages": "Fehler",
    "page_changes": "Geänderte und neue Seiten",
    "blocking": "vor Umsetzung klären",
    "footer_cache": "cache hits & misses",
    "no_changes": "Keine Änderungen im Zeitraum.",
    "no_version": "nicht vorhanden",
    "location": "Ort",
    "status_new": "neu",
    "status_changed": "geändert",
    "other_delta": "Delta-Punkte ohne eindeutige Seite",
    "incomplete": "Analyse unvollständig: Gesamtzusammenfassung, Änderungsantrag, Delta-Spezifikation und Aufwand konnten nicht (vollständig) erzeugt werden, die Änderungen pro Seite sind vollständig.",
    "synthesis_warnings": "Hinweise zur Analyse",
    "subject_changes": "Seiten mit Änderungen",
    "no_data": "Keine Änderungen gefunden.",
})
_EN: Dict[str, str] = dict(base._EN)
_EN.update({
    "title": "Page changes of the specification",
    "window": "Window",
    "window_to": "to",
    "summary_title": "Overall summary",
    "themes": "Changes that belong together",
    "pages": "Pages",
    "checked": "Pages checked",
    "changed_pages": "Changed",
    "new_pages": "New",
    "quiet": "Pages without relevant change (unchanged or cosmetic only, not listed)",
    "error_pages": "Errors",
    "page_changes": "Changed and new pages",
    "blocking": "clarify before implementing",
    "footer_cache": "cache hits & misses",
    "no_changes": "No changes in the window.",
    "no_version": "does not exist",
    "location": "Location",
    "status_new": "new",
    "status_changed": "changed",
    "other_delta": "Delta items without a matching page",
    "incomplete": "Analysis incomplete: overall summary, change request, delta specification and effort could not be (fully) generated; the changes per page are complete.",
    "synthesis_warnings": "Analysis notes",
    "subject_changes": "pages with changes",
    "no_data": "No changes found.",
})


def _labels(report: PageWatchReport) -> Dict[str, str]:
    return _DE if (report.language or "de").lower().startswith("de") else _EN


def _all_errors(report: PageWatchReport) -> List[ReportError]:
    rows = list(report.errors)
    if report.cache_write_errors:
        rows.append(ReportError(
            scope="cache", message=f"{report.cache_write_errors} cache write(s) failed (first: {report.cache_write_error})",
            impact="results are unaffected but every run repeats all requests (slower); check the data directory",
        ))
    return rows


def problem_count(report: PageWatchReport) -> int:
    return len(_all_errors(report))


def _changed(report: PageWatchReport) -> List[PageChange]:
    return report.with_status(PageStatus.CHANGED, PageStatus.NEW)


def build_subject(prefix: str, report: PageWatchReport) -> str:
    L = _labels(report)
    subject = f"{prefix} {report.generated_at:%d.%m.%Y}: {len(_changed(report))} {L['subject_changes']}"
    n = problem_count(report)
    if n:
        subject += f", {n} {L['subject_problems']}"
    return subject.strip()


def _deltas_by_page(report: PageWatchReport):
    titles = {p.title for p in _changed(report)}
    by_page: Dict[str, list] = {}
    other = []
    if report.synthesis:
        for c in report.synthesis.delta_changes:
            if c.page in titles:
                by_page.setdefault(c.page, []).append(c)
            else:
                other.append(c)
    return by_page, other


_RAW_DIAGRAM_CHANGE = re.compile(
    r"^diagram '.*?': .*?(?:Node (?:added|removed)|Edge (?:added|removed|label changed|arrows changed)"
    r"|Page (?:added|removed)): |^diagram '.*?': .*?properties changed \(")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•–]|\d+[.)])\s+")


def _is_raw_diagram_change(note: str) -> bool:
    """draw.io node/edge lines are input for the model, unreadable for people; the page summary carries them."""
    return "compared visually" not in note and bool(_RAW_DIAGRAM_CHANGE.match(note))


def _bullets(text: str) -> List[str]:
    return [_BULLET_PREFIX.sub("", line) for line in base._paragraphs(text)]


def _without_inferred(vp):
    return replace(vp, inferred=False, inferred_reason="") if vp is not None else None


def _page_row(pc: PageChange, deltas: list, L: Dict[str, str]) -> dict:
    r = pc.result
    notes = [n for n in (r.diagram_notes if r else []) if not _is_raw_diagram_change(n)]
    detail = list(dict.fromkeys(list(pc.warnings) + notes))
    old, new = (_without_inferred(r.old), _without_inferred(r.new)) if r else (None, None)
    return {
        "anchor": f"page-{pc.page_id}",
        "title": pc.title,
        "url": base._link(pc.url),
        "path": " > ".join(pc.path),
        "status": L["status_new"] if pc.status == PageStatus.NEW else L["status_changed"],
        "cls": pc.status.value.replace(" ", "-"),
        "old": base._version_label(old, L, L["no_version"]) if r else "",
        "old_url": base._link(old.url) if old else None,
        "new": base._version_label(new, L, "") if new else "",
        "new_url": base._link(new.url) if new else None,
        "diff_url": base._link(r.diff_url) if r else None,
        "summary": _bullets(pc.summary),
        "detail": detail,
        "delta_groups": base._delta_groups(deltas, L),
    }


def _link_rows(pages: List[PageChange]) -> List[dict]:
    return [{"title": p.title, "url": base._link(p.url), "anchor": f"page-{p.page_id}"} for p in pages]


def _counts(report: PageWatchReport) -> dict:
    return {
        "checked": len(report.pages),
        "changed": len(report.with_status(PageStatus.CHANGED)),
        "new": len(report.with_status(PageStatus.NEW)),
        "quiet": len(report.with_status(PageStatus.COSMETIC_ONLY, PageStatus.UNCHANGED)),
        "error": len(report.with_status(PageStatus.ERROR)),
    }


def _window(report: PageWatchReport) -> str:
    start = report.window_start.isoformat() if report.window_start else "?"
    return f"{start} {_labels(report)['window_to']} {report.generated_at:%Y-%m-%d} ({report.window_days})"


def render_mail_html(report: PageWatchReport) -> str:
    L = _labels(report)
    by_page, other = _deltas_by_page(report)
    pages = [_page_row(p, by_page.get(p.title, []), L) for p in _changed(report)]
    syn = report.synthesis
    toc = [("overview", L["overview"])] + [(p["anchor"], p["title"]) for p in pages]
    if syn or report.analysis_incomplete:
        toc.append(("summary", L["summary_title"]))
    toc.append(("errors", L["errors"]))

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.filters["safe_url"] = _safe_url
    env.filters["linkify"] = base._linkify
    return env.get_template("page_watch.html.j2").render(
        L=L,
        lang=(report.language or "de")[:2],
        report=report,
        generated=f"{report.generated_at:%Y-%m-%d %H:%M}",
        window=_window(report),
        parent_url=base._link(report.parent_url),
        toc=toc,
        counts=_counts(report),
        changed_links=_link_rows(_changed(report)),
        pages=pages,
        syn=syn,
        overview_paragraphs=base._paragraphs(syn.overview) if syn else [],
        change_request=base._paragraphs(syn.change_request) if syn else [],
        other_delta_groups=base._delta_groups(other, L),
        problem_count=problem_count(report),
        errors=_all_errors(report),
        cache_corrupt_note=(f" ({L['footer_cache_corrupt']}: {report.cache_corrupt})" if report.cache_corrupt else ""),
    )


def render_confluence_storage(report: PageWatchReport) -> str:
    L = _labels(report)
    e, a, table, td, macro, ul = base._e, base._a, base._table, base._td, base._macro, base._ul
    by_page, other = _deltas_by_page(report)
    errors = _all_errors(report)
    counts = _counts(report)
    syn = report.synthesis
    parts: List[str] = ['<ac:structured-macro ac:name="toc" ac:schema-version="1" />']
    parts.append(f"<h1>{e(L['title'])}: {a(report.parent_url, report.parent_title)}</h1>")
    parts.append(f"<p>{e(L['window'])}: {e(_window(report))} | {e(L['generated'])}: "
                 f"{e(report.generated_at.strftime('%Y-%m-%d %H:%M'))}</p>")
    if errors:
        parts.append(macro("warning", f"<p>{len(errors)} {e(L['problems'])} {e(L['errors'])}.</p>"))

    parts.append(f"<h2>{e(L['overview'])}</h2>")
    parts.append(ul([f"{e(L['checked'])}: {counts['checked']}", f"{e(L['changed_pages'])}: {counts['changed']}",
                     f"{e(L['new_pages'])}: {counts['new']}", f"{e(L['error_pages'])}: {counts['error']}",
                     f"{e(L['quiet'])}: {counts['quiet']}"]))
    if not _changed(report):
        parts.append(f"<p>{e(L['no_changes'])}</p>")

    for pc in _changed(report):
        row = _page_row(pc, by_page.get(pc.title, []), L)
        parts.append(f"<h2>{e(pc.title)}</h2>")
        meta = [a(pc.url, pc.title), e(row["status"])]
        if row["path"]:
            meta.append(f"{e(L['location'])}: {e(row['path'])}")
        meta.append(f"{a(row['old_url'], row['old'])} &rarr; {a(row['new_url'], row['new'])}")
        if row["diff_url"]:
            meta.append(a(row["diff_url"], L["diff"]))
        parts.append("<p>" + " | ".join(meta) + "</p>")
        if row["summary"]:
            parts.append(ul([e(p) for p in row["summary"]]))
        if row["detail"]:
            parts.append(macro("warning", ul([e(d) for d in row["detail"]])))
        delta = base._storage_delta(SimpleNamespace(delta_changes=by_page.get(pc.title, []), open_points=[]), L)
        if delta:
            parts.append(f"<h3>{e(L['delta'])}</h3>" + delta)

    if syn or report.analysis_incomplete:
        parts.append(f"<h2>{e(L['summary_title'])}</h2>")
        if report.analysis_incomplete:
            parts.append(macro("warning", f"<p>{e(L['incomplete'])}</p>"))
        if syn:
            parts.extend(f"<p>{e(p)}</p>" for p in base._paragraphs(syn.overview))
            if syn.themes:
                parts.append(f"<h3>{e(L['themes'])}</h3>")
                parts.append(ul([f"<strong>{e(t.title)}</strong>: {e(t.summary)} ({e(', '.join(t.pages))})"
                                 for t in syn.themes]))
            parts.append(f"<h3>{e(L['change_request'])}</h3>")
            parts.extend(f"<p>{e(p)}</p>" for p in base._paragraphs(syn.change_request))
            parts.append(f"<h3>{e(L['effort'])}</h3>")
            parts.append(table([L["size"], L["person_days"], L["remark"]], [[td(), td(), td()]]))
            if other:
                parts.append(f"<h3>{e(L['other_delta'])}</h3>")
                parts.append(base._storage_delta(SimpleNamespace(delta_changes=other, open_points=[]), L))
            if syn.open_points:
                parts.append(f"<h3>{e(L['open_points'])}</h3>" + ul([
                    e(p.question) + (f" ({e(L['raised_by'])}: {e(p.raised_by)})" if p.raised_by else "")
                    for p in syn.open_points]))
            if syn.warnings:
                parts.append(f"<h3>{e(L['synthesis_warnings'])}</h3>" + macro("warning", ul([e(w) for w in syn.warnings])))

    parts.append(f"<h2>{e(L['errors'])}</h2>")
    if errors:
        parts.append(table([L["err_scope"], L["err_message"], L["err_attempts"], L["err_impact"]],
                           [[td(x.scope), td(x.message), td(str(x.attempts)), td(x.impact)] for x in errors]))
    else:
        parts.append(f"<p>{e(L['no_errors'])}</p>")
    parts.append(f"<p>{e(L['footer_cmd'])}: {e(report.command_line)} | {e(L['footer_cache'])}: "
                 f"{report.cache_hits} &amp; {report.cache_misses}</p>")
    return "\n".join(parts)
