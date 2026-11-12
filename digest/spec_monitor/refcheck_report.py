"""Rendering of a RefCheckReport: HTML mail (Jinja2) and Confluence storage format."""
from __future__ import annotations

from typing import Dict, List

from jinja2 import Environment, FileSystemLoader

from digest.email_sender import TEMPLATES_DIR, _safe_url
from digest.spec_monitor.models import (
    MissingReference, RefCheckReport, RefCheckResult, RejectedLink, ReportError, SuggestedSource,
)
from digest.spec_monitor.report import _a, _e, _link, _linkify, _macro, _table, _td, _td_html, _ul

_DE: Dict[str, str] = {
    "title": "Mögliche fehlende Quellen der Detailspezifikationen",
    "overview": "Übersicht",
    "toc": "Inhaltsverzeichnis",
    "problems": "Problem(e) aufgetreten, siehe Abschnitt",
    "errors": "Fehler und Einschränkungen",
    "no_errors": "Keine Fehler.",
    "with_missing": "Mit möglicherweise fehlenden Referenzen",
    "without_missing": "Ohne fehlende Referenzen",
    "excluded_pages": "Ausgeschlossene Seiten",
    "none": "keine",
    "status": "Status",
    "jira": "Jira",
    "listed": "Quellen in der Tabelle",
    "reference": "Referenz",
    "kind": "Art",
    "mention": "Erwähnung im Text",
    "section": "Abschnitt",
    "found_by": "Gefunden durch",
    "origin_pattern": "Muster",
    "origin_llm": "KI",
    "warnings": "Hinweise",
    "llm_incomplete": "KI-Prüfung fehlgeschlagen: nur ADR-/eCH-Nummern und Links wurden geprüft.",
    "no_missing": "Keine fehlenden Referenzen gefunden.",
    "intro": "Diese Referenzen werden im Text der Spezifikation genannt, stehen aber nicht in der Quellentabelle. "
             "Es sind Hinweise zum Prüfen, keine gesicherten Lücken.",
    "err_scope": "Bereich",
    "err_message": "Meldung",
    "err_attempts": "Versuche",
    "err_impact": "Auswirkung",
    "footer_cache": "Cache-Treffer / Fehlgriffe",
    "footer_cmd": "Aufruf",
    "generated": "Erzeugt",
    "ai_note": "Teilweise KI-generiert, Treffer prüfen",
    "subject": "mögliche fehlende Quellen in",
    "subject_specs": "Spezifikationen",
    "subject_problems": "Problem(e)",
    "kind_confluence": "Confluence-Seite",
    "kind_ech": "eCH-Standard",
    "kind_adr": "ADR",
    "kind_url": "Link",
    "kind_repo": "Repository-Dokument",
    "kind_standard": "Standard",
    "kind_other": "Sonstiges",
    "suggested": "Vorgeschlagene Quellen aus verlinkten Seiten",
    "suggested_intro": "Seiten, die von gelisteten Quellen verlinkt werden und laut KI-Einschätzung in die Quellentabelle "
                       "gehören könnten.",
    "page": "Seite",
    "linked_from": "Verlinkt von",
    "link_mention": "Verlinkung im Text der Quelle",
    "confidence": "Einschätzung",
    "conf_high": "hoch",
    "conf_medium": "mittel",
    "conf_low": "niedrig",
    "depth": "Tiefe",
    "link_stats": "Verlinkte Seiten geprüft",
    "link_rejected": "Verworfen",
    "link_too_short": "kaum Text",
    "link_incomplete": "Linkprüfung unvollständig: die Vorschläge können unvollständig sein.",
    "with_suggested": "Mit vorgeschlagenen Quellen aus verlinkten Seiten",
    "stats_suggested": "davon vorgeschlagen",
    "stats_rejected": "verworfen",
}

_EN: Dict[str, str] = dict(_DE)
_EN.update({
    "title": "Possibly missing sources of the detail specifications",
    "overview": "Overview",
    "toc": "Table of contents",
    "problems": "problem(s) occurred, see section",
    "errors": "Errors and limitations",
    "no_errors": "No errors.",
    "with_missing": "With possibly missing references",
    "without_missing": "Without missing references",
    "excluded_pages": "Excluded pages",
    "none": "none",
    "listed": "Sources in the table",
    "reference": "Reference",
    "kind": "Kind",
    "mention": "Mentioned in the text",
    "section": "Section",
    "found_by": "Found by",
    "origin_pattern": "pattern",
    "origin_llm": "AI",
    "warnings": "Notes",
    "llm_incomplete": "AI check failed: only ADR / eCH numbers and links were checked.",
    "no_missing": "No missing references found.",
    "intro": "These references are mentioned in the text of the specification but are not in its sources table. "
             "They are hints to check, not confirmed gaps.",
    "err_scope": "Scope",
    "err_message": "Message",
    "err_attempts": "Attempts",
    "err_impact": "Impact",
    "footer_cache": "Cache hits / misses",
    "footer_cmd": "Command",
    "generated": "Generated",
    "ai_note": "Partly AI generated, check the findings",
    "subject": "possibly missing sources in",
    "subject_specs": "specifications",
    "subject_problems": "problem(s)",
    "kind_confluence": "Confluence page",
    "kind_ech": "eCH standard",
    "kind_adr": "ADR",
    "kind_url": "link",
    "kind_repo": "repository document",
    "kind_standard": "standard",
    "kind_other": "other",
    "suggested": "Suggested sources from linked pages",
    "suggested_intro": "Pages linked from listed sources that the AI judged could belong in the sources table.",
    "page": "Page",
    "linked_from": "Linked from",
    "link_mention": "Link in the text of the source",
    "confidence": "Confidence",
    "conf_high": "high",
    "conf_medium": "medium",
    "conf_low": "low",
    "depth": "Depth",
    "link_stats": "Linked pages checked",
    "link_rejected": "Rejected",
    "link_too_short": "hardly any text",
    "link_incomplete": "Link check incomplete: the suggestions may be incomplete.",
    "with_suggested": "With suggested sources from linked pages",
    "stats_suggested": "of which suggested",
    "stats_rejected": "rejected",
})


def _labels(report: RefCheckReport) -> Dict[str, str]:
    return _DE if (report.language or "de").lower().startswith("de") else _EN


def _all_errors(report: RefCheckReport) -> List[ReportError]:
    rows: List[ReportError] = list(report.errors)
    for r in report.results:
        rows.extend(r.errors)
    return rows


def _problem_count(report: RefCheckReport) -> int:
    return len(_all_errors(report))


def _with_missing(report: RefCheckReport) -> List[RefCheckResult]:
    return [r for r in report.results if r.missing]


def _with_findings(report: RefCheckReport) -> List[RefCheckResult]:
    return [r for r in report.results if r.missing or r.suggested]


def build_subject(prefix: str, report: RefCheckReport) -> str:
    L = _labels(report)
    subject = (f"{prefix} {report.generated_at:%d.%m.%Y}: {L['subject']} "
               f"{len(_with_findings(report))} {L['subject_specs']}")
    n = _problem_count(report)
    if n:
        subject += f", {n} {L['subject_problems']}"
    return subject.strip()


def _row(m: MissingReference, L: Dict[str, str]) -> dict:
    return {
        "reference": m.reference,
        "url": _link(m.url),
        "kind": L.get(f"kind_{m.kind}", m.kind),
        "mention": m.mention,
        "section": m.section,
        "origin": L["origin_llm"] if m.origin == "llm" else L["origin_pattern"],
        "why": m.why,
    }


def _suggested_row(s: SuggestedSource, L: Dict[str, str]) -> dict:
    return {
        "title": s.title,
        "url": _link(s.url),
        "linked_from": s.linked_from,
        "depth": s.depth,
        "mention": s.mention,
        "reason": s.reason,
        "confidence": L.get(f"conf_{s.confidence}", s.confidence),
    }


def _rejected_text(r: RejectedLink, L: Dict[str, str]) -> str:
    reason = L["link_too_short"] if r.code == "too_short" else r.reason
    return f"{r.title} ({reason})" if reason else r.title


def _link_stats(r: RefCheckResult, L: Dict[str, str]) -> str:
    return (f"{L['link_stats']}: {r.link_checked} ({L['depth']} {r.link_depth}), {L['stats_suggested']}: "
            f"{len(r.suggested)}, {L['stats_rejected']}: {len(r.link_rejected)}")


def render_mail_html(report: RefCheckReport) -> str:
    L = _labels(report)
    flagged = [r for r in report.results if r.missing or r.suggested or r.warnings]
    specs = [{
        "anchor": f"spec-{r.spec.page_id}",
        "title": r.spec.title,
        "url": _link(r.spec.url),
        "status": r.spec.status,
        "jira": r.spec.jira,
        "listed": r.listed_count,
        "rows": [_row(m, L) for m in r.missing],
        "suggested": [_suggested_row(s, L) for s in r.suggested],
        "link_stats": _link_stats(r, L) if r.link_depth else "",
        "rejected": [{"text": _rejected_text(x, L), "url": _link(x.url)} for x in r.link_rejected],
        "link_incomplete": r.link_incomplete,
        "warnings": r.warnings,
        "llm_incomplete": r.llm_incomplete,
    } for r in flagged]
    toc = [("overview", L["overview"])] + [(s["anchor"], s["title"]) for s in specs] + [("errors", L["errors"])]
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    env.filters["safe_url"] = _safe_url
    env.filters["linkify"] = _linkify
    return env.get_template("ref_check.html.j2").render(
        L=L,
        lang=(report.language or "de")[:2],
        report=report,
        generated=f"{report.generated_at:%Y-%m-%d %H:%M}",
        toc=toc,
        with_missing=[{"title": r.spec.title, "url": _link(r.spec.url), "anchor": f"spec-{r.spec.page_id}",
                       "n": len(r.missing)} for r in _with_missing(report)],
        with_suggested=[{"title": r.spec.title, "url": _link(r.spec.url), "anchor": f"spec-{r.spec.page_id}",
                         "n": len(r.suggested)} for r in report.results if r.suggested],
        without_missing=[{"title": r.spec.title, "url": _link(r.spec.url)}
                         for r in report.results if not r.missing and not r.suggested],
        excluded=[{"id": pid, "title": title} for pid, title in report.excluded_pages],
        problem_count=_problem_count(report),
        specs=specs,
        errors=_all_errors(report),
    )


def render_confluence_storage(report: RefCheckReport) -> str:
    L = _labels(report)
    errors = _all_errors(report)
    flagged = [r for r in report.results if r.missing or r.suggested or r.warnings]
    parts: List[str] = ['<ac:structured-macro ac:name="toc" ac:schema-version="1" />']
    parts.append(f"<h1>{_e(L['title'])}</h1>")
    parts.append(f"<p>{_e(L['generated'])}: {_e(report.generated_at.strftime('%Y-%m-%d %H:%M'))}</p>")
    parts.append(f"<p>{_e(L['intro'])}</p>")
    if errors:
        parts.append(_macro("warning", f"<p>{len(errors)} {_e(L['problems'])} {_e(L['errors'])}.</p>"))

    parts.append(f"<h2>{_e(L['overview'])}</h2>")
    for heading, items in (
        (L["with_missing"], [f"{_a(r.spec.url, r.spec.title)} ({len(r.missing)})" for r in _with_missing(report)]),
        (L["with_suggested"], [f"{_a(r.spec.url, r.spec.title)} ({len(r.suggested)})"
                               for r in report.results if r.suggested]),
        (L["without_missing"], [_a(r.spec.url, r.spec.title) for r in report.results
                                if not r.missing and not r.suggested]),
        (L["excluded_pages"], [f"{_e(t)} ({_e(pid)})" for pid, t in report.excluded_pages]),
    ):
        parts.append(f"<h3>{_e(heading)} ({len(items)})</h3>")
        parts.append(_ul(items) if items else f"<p>{_e(L['none'])}</p>")

    for r in flagged:
        parts.append(f"<h2>{_e(r.spec.title)}</h2>")
        meta = [f"{_e(L['status'])}: {_e(r.spec.status)}", _a(r.spec.url, r.spec.title),
                f"{_e(L['listed'])}: {r.listed_count}"]
        if r.spec.jira:
            meta.append(f"{_e(L['jira'])}: {_e(', '.join(r.spec.jira))}")
        parts.append("<p>" + " | ".join(meta) + "</p>")
        if r.warnings:
            parts.append(_macro("warning", _ul([_e(w) for w in r.warnings])))
        if r.llm_incomplete:
            parts.append(_macro("warning", f"<p>{_e(L['llm_incomplete'])}</p>"))
        if r.missing:
            rows = []
            for m in r.missing:
                row = _row(m, L)
                why = f"<br />{_e(row['why'])}" if row["why"] else ""
                rows.append([_td_html(_a(row["url"], row["reference"]) + why), _td(row["kind"]),
                             _td(row["section"]), _td(row["mention"]), _td(row["origin"])])
            parts.append(_table(
                [L["reference"], L["kind"], L["section"], L["mention"], L["found_by"]], rows))
        else:
            parts.append(f"<p>{_e(L['no_missing'])}</p>")
        if r.link_depth:
            parts.append(f"<h3>{_e(L['suggested'])}</h3>")
            if r.link_incomplete:
                parts.append(_macro("warning", f"<p>{_e(L['link_incomplete'])}</p>"))
            parts.append(f"<p>{_e(_link_stats(r, L))}</p>")
            if r.suggested:
                rows = []
                for s in r.suggested:
                    row = _suggested_row(s, L)
                    why = f"<br />{_e(row['reason'])}" if row["reason"] else ""
                    rows.append([_td_html(_a(row["url"], row["title"]) + why), _td("; ".join(row["linked_from"])),
                                 _td(row["mention"]), _td(row["confidence"])])
                parts.append(_table([L["page"], L["linked_from"], L["link_mention"], L["confidence"]], rows))
            if r.link_rejected:
                parts.append(f"<p>{_e(L['link_rejected'])}:</p>")
                parts.append(_ul([_a(_link(x.url), _rejected_text(x, L)) for x in r.link_rejected]))

    parts.append(f"<h2>{_e(L['errors'])}</h2>")
    if errors:
        parts.append(_table(
            [L["err_scope"], L["err_message"], L["err_attempts"], L["err_impact"]],
            [[_td(e.scope), _td(e.message), _td(str(e.attempts)), _td(e.impact)] for e in errors]))
    else:
        parts.append(f"<p>{_e(L['no_errors'])}</p>")
    parts.append(f"<p>{_e(L['footer_cmd'])}: {_e(report.command_line)} | {_e(L['footer_cache'])}: "
                 f"{report.cache_hits} / {report.cache_misses}</p>")
    return "\n".join(parts)
