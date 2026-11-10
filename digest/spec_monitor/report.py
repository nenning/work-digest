"""Rendering of a RunReport: HTML mail (Jinja2) and Confluence storage format."""
from __future__ import annotations

import html as _html
import re
from typing import Dict, List, Optional

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

from digest.email_sender import TEMPLATES_DIR, _safe_url
from digest.spec_monitor.models import (
    Classification, DeltaChange, ReportError, RunReport, SpecAnalysis, VersionPoint,
)

_DE: Dict[str, str] = {
    "title": "Quellen-Änderungen der Detailspezifikationen",
    "toc": "Inhaltsverzeichnis",
    "overview": "Übersicht",
    "errors": "Fehler und Einschränkungen",
    "problems": "Problem(e) aufgetreten, siehe Abschnitt",
    "with_changes": "Analysiert, mit Änderungen",
    "without_changes": "Analysiert, ohne Änderungen",
    "not_analysed": "Nicht analysiert (Status)",
    "excluded_pages": "Ausgeschlossene Seiten",
    "none": "keine",
    "jira": "Jira",
    "status": "Status",
    "sources": "Quellen alt gegen neu",
    "source": "Quelle",
    "spec": "Spezifikation",
    "kind": "Art",
    "old": "Alt",
    "new": "Neu",
    "diff": "Diff",
    "classification": "Einstufung",
    "warnings": "Vollständigkeitshinweise",
    "change_request": "Änderungsantrag",
    "effort_ai": "Aufwandsschätzung (KI, nicht validiert)",
    "size": "Grösse",
    "person_days": "Personentage",
    "remark": "Bemerkung",
    "effort": "Aufwand",
    "sum": "Summe",
    "delta": "Delta-Spezifikation",
    "kind_new": "Neu",
    "kind_changed": "Geändert",
    "kind_removed": "Entfallen",
    "part": "Betroffener Teil",
    "current": "Aktuelle Aussage",
    "change": "Änderung",
    "reason": "Grund",
    "blocking": "blockierend",
    "blocking_col": "Blockierend",
    "open_points": "Offene Punkte",
    "raised_by": "ausgelöst durch",
    "inferred": "abgeleitet",
    "unknown_version": "nicht angegeben",
    "incomplete": "Analyse unvollständig: Änderungsantrag und Delta-Spezifikation konnten nicht erzeugt werden, die Quellenfakten sind unten vollständig.",
    "err_scope": "Bereich",
    "err_message": "Meldung",
    "err_attempts": "Versuche",
    "err_impact": "Auswirkung",
    "limitations": "Einschränkungen (nicht überwachte Quellen)",
    "footer_cache": "Cache-Treffer / Fehlgriffe",
    "footer_cache_corrupt": "defekte Cache-Einträge",
    "footer_cmd": "Aufruf",
    "generated": "Erzeugt",
    "subject_changes": "Spezifikationen mit Änderungen",
    "subject_problems": "Problem(e)",
    "ai_note": "KI-generiert, Aufwandsschätzung unvalidiert",
    "no_errors": "Keine Fehler.",
    "no_data": "Keine Änderungen an Quellen gefunden.",
    "cls_changed": "geändert",
    "cls_cosmetic only": "nur kosmetisch",
    "cls_unchanged": "unverändert",
    "cls_not monitored": "nicht überwacht",
    "cls_error": "Fehler",
    "cls_excluded": "ausgeschlossen",
}

_EN: Dict[str, str] = dict(_DE)
_EN.update({
    "title": "Source changes of the detail specifications",
    "toc": "Table of contents",
    "overview": "Overview",
    "errors": "Errors and limitations",
    "problems": "problem(s) occurred, see section",
    "with_changes": "Analysed, with changes",
    "without_changes": "Analysed, without changes",
    "not_analysed": "Not analysed (status)",
    "excluded_pages": "Excluded pages",
    "none": "none",
    "sources": "Sources old vs new",
    "source": "Source",
    "spec": "Specification",
    "kind": "Kind",
    "old": "Old",
    "new": "New",
    "classification": "Classification",
    "warnings": "Completeness warnings",
    "change_request": "Change request",
    "effort_ai": "Effort estimate (AI, unvalidated)",
    "size": "Size",
    "person_days": "Person-days",
    "remark": "Remark",
    "effort": "Effort",
    "sum": "Sum",
    "delta": "Delta specification",
    "kind_new": "New",
    "kind_changed": "Changed",
    "kind_removed": "Removed",
    "part": "Affected part",
    "current": "Current statement",
    "change": "Change",
    "reason": "Reason",
    "blocking": "blocking",
    "blocking_col": "Blocking",
    "open_points": "Open points",
    "raised_by": "raised by",
    "inferred": "inferred",
    "unknown_version": "not stated",
    "incomplete": "Analysis incomplete: change request and delta specification could not be generated; the source facts below are complete.",
    "err_scope": "Scope",
    "err_message": "Message",
    "err_attempts": "Attempts",
    "err_impact": "Impact",
    "limitations": "Limitations (sources not monitored)",
    "footer_cache": "Cache hits / misses",
    "footer_cache_corrupt": "corrupt cache entries",
    "footer_cmd": "Command",
    "generated": "Generated",
    "subject_changes": "specs with changes",
    "subject_problems": "problem(s)",
    "ai_note": "AI generated, effort estimate unvalidated",
    "no_errors": "No errors.",
    "no_data": "No source changes found.",
    "cls_changed": "changed",
    "cls_cosmetic only": "cosmetic only",
    "cls_unchanged": "unchanged",
    "cls_not monitored": "not monitored",
    "cls_error": "error",
    "cls_excluded": "excluded",
})


def _labels(report: RunReport) -> Dict[str, str]:
    return _DE if (report.language or "de").lower().startswith("de") else _EN


def _link(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return url if _safe_url(url) == url else None


_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TRAILING = ".,;:!?)]}»«“”‘’"


def _linkify(text) -> Markup:
    s = "" if text is None else str(text)
    out: List[str] = []
    pos = 0
    for m in _URL_RE.finditer(s):
        url = m.group(0)
        stripped = url.rstrip(_URL_TRAILING)
        if not stripped or stripped.endswith("://"):
            continue
        end = m.start() + len(stripped)
        out.append(str(escape(s[pos:m.start()])))
        out.append(f'<a href="{escape(stripped)}" target="_blank" rel="noopener">{escape(stripped)}</a>')
        pos = end
    out.append(str(escape(s[pos:])))
    return Markup("".join(out))


def _paragraphs(text: Optional[str]) -> List[str]:
    return [p.strip() for p in (text or "").splitlines() if p.strip()]


def _version_label(vp: Optional[VersionPoint], L: Dict[str, str], fallback: str = "") -> str:
    if vp is None:
        return fallback or L["unknown_version"]
    v = vp.version
    label = f"v{v}" if v.isdigit() else v
    if vp.when:
        label += f" ({vp.when:%Y-%m-%d})"
    if vp.status:
        label += f", {vp.status}"
    if vp.inferred:
        reason = f": {vp.inferred_reason}" if vp.inferred_reason else ""
        label += f" [{L['inferred']}{reason}]"
    return label


def _source_row(s, L: Dict[str, str]) -> dict:
    ref = s.ref
    cls = s.classification
    detail: List[str] = []
    if s.diff_summary:
        detail.append(s.diff_summary)
    if ref.excluded_reason:
        detail.append(ref.excluded_reason)
    if ref.parse_issue:
        detail.append(ref.parse_issue)
    detail.extend(s.diagram_notes)
    detail.extend(s.warnings)
    detail.extend(s.errors)
    detail = list(dict.fromkeys(detail))
    return {
        "name": ref.name,
        "url": _link(ref.url),
        "section": ref.section or "",
        "used_for": ref.used_for,
        "old": _version_label(s.old, L, ref.version_text),
        "old_url": _link(s.old.url) if s.old else None,
        "new": _version_label(s.new, L, "") if s.new else "",
        "new_url": _link(s.new.url) if s.new else None,
        "diff_url": _link(s.diff_url),
        "classification": L.get(f"cls_{cls.value}", cls.value),
        "cls": cls.value.replace(" ", "-"),
        "detail": detail,
    }


def _delta_groups(changes: List[DeltaChange], L: Dict[str, str]) -> List[dict]:
    groups = []
    for kind in ("new", "changed", "removed"):
        if kind == "changed":
            items = [c for c in changes if c.kind not in ("new", "removed")]
        else:
            items = [c for c in changes if c.kind == kind]
        if items:
            groups.append({"kind": kind, "label": L[f"kind_{kind}"], "entries": items})
    return groups


def _cache_corrupt_note(report: RunReport, L: dict) -> str:
    return f" ({_e(L['footer_cache_corrupt'])}: {report.cache_corrupt})" if report.cache_corrupt else ""


def _all_errors(report: RunReport) -> List[ReportError]:
    rows: List[ReportError] = list(report.errors)
    if report.cache_write_errors:
        rows.append(ReportError(
            scope="cache", message=f"{report.cache_write_errors} cache write(s) failed (first: {report.cache_write_error})",
            impact="results are unaffected but every run repeats all requests (slower); check the data directory",
        ))
    for a in report.analysed:
        rows.extend(a.errors)
        if a.completeness_warnings and not a.has_changes:
            # Shown in the spec's own block only when it has changes; otherwise a broken or
            # missing sources table would read as "analysed, no changes".
            rows.append(ReportError(
                scope=a.spec.title, message="; ".join(a.completeness_warnings),
                impact="sources may be missing, unreadable or unmonitored; 'no changes' is not reliable",
            ))
        for s in a.sources:
            if s.classification == Classification.ERROR:
                rows.append(ReportError(
                    scope=f"{a.spec.title} / {s.ref.name}",
                    message="; ".join(s.errors) or "error",
                    attempts=s.attempts,
                    impact=s.ref.used_for,
                ))
    return rows


def _limitations(report: RunReport) -> List[dict]:
    rows = []
    for a in report.analysed:
        for s in a.sources:
            if s.classification == Classification.NOT_MONITORED:
                rows.append({
                    "scope": a.spec.title, "name": s.ref.name, "url": _link(s.ref.url),
                    "note": "; ".join(s.warnings) or s.ref.version_text,
                })
    return rows


def _changed_specs(report: RunReport) -> List[SpecAnalysis]:
    return [a for a in report.analysed if a.has_changes]


def _problem_count(report: RunReport) -> int:
    return len(_all_errors(report))


def build_subject(prefix: str, report: RunReport) -> str:
    L = _labels(report)
    subject = f"{prefix} {report.generated_at:%d.%m.%Y}: {len(_changed_specs(report))} {L['subject_changes']}"
    n = _problem_count(report)
    if n:
        subject += f", {n} {L['subject_problems']}"
    return subject.strip()


def render_mail_html(report: RunReport) -> str:
    L = _labels(report)
    with_changes = _changed_specs(report)
    without = [a for a in report.analysed if not a.has_changes]
    specs = []
    for a in with_changes:
        specs.append({
            "anchor": f"spec-{a.spec.page_id}",
            "title": a.spec.title,
            "url": _link(a.spec.url),
            "status": a.spec.status,
            "jira": a.spec.jira,
            "sources": [_source_row(s, L) for s in a.sources],
            "warnings": a.completeness_warnings,
            "incomplete": a.analysis_incomplete,
            "change_request": _paragraphs(a.change_request),
            "effort": a.effort,
            "delta_groups": _delta_groups(a.delta_changes, L),
            "open_points": a.open_points,
        })
    toc = [("overview", L["overview"])]
    toc += [(s["anchor"], s["title"]) for s in specs]
    toc.append(("errors", L["errors"]))

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.filters["safe_url"] = _safe_url
    env.filters["linkify"] = _linkify
    return env.get_template("spec_monitor.html.j2").render(
        L=L,
        lang=(report.language or "de")[:2],
        report=report,
        generated=f"{report.generated_at:%Y-%m-%d %H:%M}",
        toc=toc,
        with_changes=[{"title": a.spec.title, "url": _link(a.spec.url), "anchor": f"spec-{a.spec.page_id}",
                       "n": sum(1 for s in a.sources if s.classification == Classification.CHANGED)}
                      for a in with_changes],
        without_changes=[{"title": a.spec.title, "url": _link(a.spec.url)} for a in without],
        not_analysed=[{"title": s.title, "status": s.status, "url": _link(s.url)} for s in report.listed_only],
        excluded=[{"id": pid, "title": title} for pid, title in report.excluded_pages],
        problem_count=_problem_count(report),
        specs=specs,
        errors=_all_errors(report),
        cache_corrupt_note=(f" ({L['footer_cache_corrupt']}: {report.cache_corrupt})" if report.cache_corrupt else ""),
        limitations=_limitations(report),
        incomplete_specs=[a.spec.title for a in report.analysed if a.analysis_incomplete],
    )


# --- Confluence storage format -------------------------------------------------

def _e(text) -> str:
    return _html.escape(str(text), quote=True)


def _a(url: Optional[str], text: str) -> str:
    safe = _link(url)
    return f'<a href="{_e(safe)}">{_e(text)}</a>' if safe else _e(text)


def _table(headers: List[str], rows: List[List[str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(cells) + "</tr>" for cells in rows)
    return f'<table data-layout="full-width"><tbody><tr>{head}</tr>{body}</tbody></table>'


def _td(text: str = "") -> str:
    return f"<td>{_e(text)}</td>" if text else "<td><p /></td>"


def _td_html(inner: str) -> str:
    return f"<td>{inner}</td>"


def _macro(name: str, inner: str) -> str:
    return (f'<ac:structured-macro ac:name="{name}"><ac:rich-text-body>{inner}'
            f"</ac:rich-text-body></ac:structured-macro>")


def _ul(items: List[str]) -> str:
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>" if items else ""


def _storage_sources(a: SpecAnalysis, L: Dict[str, str]) -> str:
    rows = []
    for s in a.sources:
        r = _source_row(s, L)
        name = _a(r["url"], r["name"]) + (f" ({_e(r['section'])})" if r["section"] else "")
        detail = "".join(f"<br />{_e(d)}" for d in r["detail"])
        old = _a(r["old_url"], r["old"])
        new = _a(r["new_url"], r["new"]) if r["new"] else ""
        if r["diff_url"]:
            new += f"<br />{_a(r['diff_url'], L['diff'])}"
        rows.append([_td_html(name + detail), _td_html(old), _td_html(new), _td(r["classification"])])
    return _table([L["source"], L["old"], L["new"], L["classification"]], rows)


def _storage_delta(a: SpecAnalysis, L: Dict[str, str]) -> str:
    out = []
    rows = []
    for g in _delta_groups(a.delta_changes, L):
        for c in g["entries"]:
            part = c.affected_part + (f" ({', '.join(c.ids)})" if c.ids else "")
            rows.append([_td(g["label"]), _td(part), _td(c.current_statement), _td(c.change),
                         _td(c.reason), _td(L["blocking"] if c.blocking else ""), _td()])
    if rows:
        rows.append([f'<td colspan="6">{_e(L["sum"])}</td>', _td()])
        out.append(_table(
            [L["kind"], L["part"], L["current"], L["change"], L["reason"], L["blocking_col"], L["effort"]],
            rows))
    if a.open_points:
        out.append(f"<h4>{_e(L['open_points'])}</h4>" + _ul([
            _e(p.question) + (f" ({_e(L['raised_by'])}: {_e(p.raised_by)})" if p.raised_by else "")
            for p in a.open_points]))
    return "".join(out)


def render_confluence_storage(report: RunReport) -> str:
    L = _labels(report)
    changed = _changed_specs(report)
    errors = _all_errors(report)
    parts: List[str] = ['<ac:structured-macro ac:name="toc" ac:schema-version="1" />']

    parts.append(f"<h1>{_e(L['title'])}</h1>")
    parts.append(f"<p>{_e(L['generated'])}: {_e(report.generated_at.strftime('%Y-%m-%d %H:%M'))}</p>")
    if errors:
        parts.append(_macro("warning", f"<p>{len(errors)} {_e(L['problems'])} {_e(L['errors'])}.</p>"))

    parts.append(f"<h2>{_e(L['overview'])}</h2>")
    parts.append(f"<h3>{_e(L['effort'])}</h3>")
    parts.append(_table(
        [L["spec"], L["size"], L["person_days"], L["remark"]],
        [[_td(a.spec.title), _td(), _td(), _td()] for a in changed]) if changed else f"<p>{_e(L['no_data'])}</p>")

    def titled(heading: str, rows: List[str]) -> None:
        parts.append(f"<h3>{_e(heading)}</h3>")
        parts.append(_ul(rows) if rows else f"<p>{_e(L['none'])}</p>")

    titled(L["with_changes"], [_a(a.spec.url, a.spec.title) for a in changed])
    titled(L["without_changes"], [_a(a.spec.url, a.spec.title) for a in report.analysed if not a.has_changes])
    titled(L["not_analysed"], [_a(s.url, s.title) + f" ({_e(s.status)})" for s in report.listed_only])
    titled(L["excluded_pages"], [_e(t) + f" ({_e(pid)})" for pid, t in report.excluded_pages])

    for a in changed:
        parts.append(f"<h2>{_e(a.spec.title)}</h2>")
        meta = [f"{_e(L['status'])}: {_e(a.spec.status)}", _a(a.spec.url, a.spec.title)]
        if a.spec.jira:
            meta.append(f"{_e(L['jira'])}: {_e(', '.join(a.spec.jira))}")
        parts.append("<p>" + " | ".join(meta) + "</p>")
        parts.append(f"<h3>{_e(L['sources'])}</h3>")
        parts.append(_storage_sources(a, L))
        if a.completeness_warnings:
            parts.append(f"<h3>{_e(L['warnings'])}</h3>")
            parts.append(_macro("warning", _ul([_e(w) for w in a.completeness_warnings])))
        if a.analysis_incomplete:
            parts.append(_macro("warning", f"<p>{_e(L['incomplete'])}</p>"))
        parts.append(f"<h3>{_e(L['change_request'])}</h3>")
        parts.extend(f"<p>{_e(p)}</p>" for p in _paragraphs(a.change_request))
        parts.append(f"<h4>{_e(L['effort'])}</h4>")
        parts.append(_table(
            [L["size"], L["person_days"], L["remark"]], [[_td(), _td(), _td()]]))
        parts.append(f"<h3>{_e(L['delta'])}</h3>")
        parts.append(_storage_delta(a, L))

    parts.append(f"<h2>{_e(L['errors'])}</h2>")
    if errors:
        parts.append(_table(
            [L["err_scope"], L["err_message"], L["err_attempts"], L["err_impact"]],
            [[_td(e.scope), _td(e.message), _td(str(e.attempts)), _td(e.impact)] for e in errors]))
    else:
        parts.append(f"<p>{_e(L['no_errors'])}</p>")
    lim = _limitations(report)
    if lim:
        parts.append(f"<h3>{_e(L['limitations'])}</h3>")
        parts.append(_ul([f"{_e(r['scope'])}: {_a(r['url'], r['name'])}"
                          + (f" ({_e(r['note'])})" if r["note"] else "") for r in lim]))
    parts.append(
        f"<p>{_e(L['footer_cmd'])}: {_e(report.command_line)} | {_e(L['footer_cache'])}: "
        f"{report.cache_hits} / {report.cache_misses}{_cache_corrupt_note(report, L)}</p>")
    return "\n".join(parts)
