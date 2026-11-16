from datetime import date, datetime

from digest.spec_monitor import page_watch_report as pr
from digest.spec_monitor.models import (
    Classification, DeltaChange, EffortEstimate, OpenPoint, PageChange, PageStatus, PageTrace, PageWatchReport,
    ReportError, SourceKind, SourceRef, SourceResult, Theme, TraceReference, VersionPoint, WatchSynthesis,
)


def _vp(n, day, url=None):
    return VersionPoint(version=str(n), when=datetime(2026, *day, 10, 0).astimezone(), url=url or f"https://x/v{n}")


def _page(pid, title, status=PageStatus.CHANGED, summary="", old=True, **kw):
    ref = SourceRef(name=title, kind=SourceKind.CONFLUENCE, title=title, page_id=pid)
    result = SourceResult(ref=ref, classification=Classification.CHANGED,
                          old=_vp(3, (10, 1)) if old else None, new=_vp(5, (11, 9)),
                          diff_url=f"https://x/diff/{pid}" if old else None,
                          diagram_notes=kw.pop("diagram_notes", []))
    return PageChange(page_id=pid, title=title, url=f"https://x/{pid}", status=status, path=["Coremodul"],
                      result=result, summary=summary, **kw)


def _synthesis(**over):
    data = dict(
        overview="Retry-Regeln und Fristen ändern sich gemeinsam.",
        themes=[Theme(title="Retry", summary="neue Regeln", pages=["Batch", "Fristen"])],
        change_request="Ausgangslage A.\nWas sich ändert.",
        delta_changes=[
            DeltaChange(kind="changed", affected_part="3.1", ids=["CM-FR-03"], current_statement="alt", change="neu",
                        reason="v3 -> v5", blocking=True, page="Batch"),
            DeltaChange(kind="new", affected_part="9", change="Neu", page="Unbekannt"),
        ],
        open_points=[OpenPoint(question="Welche Frist gilt?", raised_by="Batch")],
        effort=EffortEstimate(size="M", person_days="3-5", rationale="ein Modul"),
        warnings=["Hinweis des Modells"],
    )
    data.update(over)
    return WatchSynthesis(**data)


def _report(**over):
    pages = [
        _page("10", "Batch", summary="Retry von 3 auf 5"),
        _page("11", "Fristen", status=PageStatus.NEW, old=False, summary="Neue Seite zu Fristen"),
        PageChange(page_id="12", title="Alt", url="https://x/12", status=PageStatus.UNCHANGED),
        PageChange(page_id="13", title="Kosmetik", url="https://x/13", status=PageStatus.COSMETIC_ONLY),
    ]
    data = dict(generated_at=datetime(2026, 11, 13, 8, 0).astimezone(), parent_title="Coremodul",
                parent_url="https://x/1", window_days=14, window_start=date(2026, 10, 31), pages=pages,
                synthesis=_synthesis(), command_line="python digest/main.py --page-watch", language="de")
    data.update(over)
    return PageWatchReport(**data)


def test_mail_contains_pages_themes_change_request_effort_and_delta():
    html = pr.render_mail_html(_report())
    for needle in ("Batch", "Fristen", "Retry von 3 auf 5", "Neue Seite zu Fristen", "Gesamtzusammenfassung",
                   "Zusammengehörige Änderungen", "Änderungsantrag", "Ausgangslage A.", "Welche Frist gilt?",
                   "Aufwandsschätzung (KI", "3-5", "CM-FR-03", "vor Umsetzung klären", "2026-10-31", "Hinweis des Modells"):
        assert needle in html, needle


def test_delta_item_is_shown_under_its_page_and_unmatched_ones_in_the_overall_part():
    html = pr.render_mail_html(_report())
    assert html.index("CM-FR-03") < html.index('id="summary"')
    assert "Delta-Punkte ohne eindeutige Seite" in html and "Unbekannt" in html


def test_overall_part_comes_after_the_page_blocks():
    html = pr.render_mail_html(_report())
    assert html.index('id="page-10"') < html.index('id="page-11"') < html.index('id="summary"') < html.index('id="errors"')


def test_new_page_is_marked_and_has_no_old_version_or_diff_link():
    html = pr.render_mail_html(_report())
    assert "nicht vorhanden" in html and "https://x/diff/11" not in html and "https://x/diff/10" in html


def test_unchanged_and_cosmetic_pages_are_counted_but_not_listed():
    html = pr.render_mail_html(_report())
    assert 'id="page-12"' not in html and 'id="page-13"' not in html
    assert "Kosmetik" not in html and ">Alt<" not in html and "https://x/12" not in html and "https://x/13" not in html
    assert "2 Seiten ohne relevante Änderung" in html
    storage = pr.render_confluence_storage(_report())
    assert "Kosmetik" not in storage and "Seiten ohne relevante Änderung" in storage


def test_blocking_tag_explains_itself():
    html = pr.render_mail_html(_report())
    assert "vor Umsetzung klären" in html and "blockierend" not in html


def test_summary_lines_become_a_list_and_bullet_characters_are_stripped():
    page = _page("10", "Batch", summary="- Status: canceled entfernt\n• Diagramm Lifecycle: Batch Processing")
    html = pr.render_mail_html(_report(pages=[page]))
    assert "<li>Status: canceled entfernt</li>" in html and "<li>Diagramm Lifecycle: Batch Processing</li>" in html
    storage = pr.render_confluence_storage(_report(pages=[page]))
    assert "<li>Status: canceled entfernt</li>" in storage


def test_raw_drawio_lines_are_hidden_but_limitations_stay_visible():
    notes = [
        "diagram 'Lifecycle Process.drawio': Node added: Batch Processing",
        "diagram 'Lifecycle Process.drawio': Edge removed: A -> B",
        "diagram 'Lifecycle Process.drawio': Edge label changed: A -> B: 'x' => 'y'",
        "diagram 'Lifecycle Process.drawio': Node abc: properties changed (fontColor='1' added)",
        "diagram 'Lifecycle Process.drawio' compared visually (AI, high confidence): neue Frist",
        "diagram changed, not compared: Foo.drawio (download failed) (visual comparison is not enabled)",
        "diagram 'Bar.drawio' added, not compared",
        "diagram 'Baz.drawio' removed",
    ]
    html = pr.render_mail_html(_report(pages=[_page("10", "Batch", summary="x", diagram_notes=notes)]))
    for hidden in ("Node added: Batch Processing", "Edge removed", "Edge label changed", "properties changed"):
        assert hidden not in html, hidden
    for shown in ("compared visually", "not compared: Foo.drawio", "Bar.drawio&#39; added, not compared",
                  "Baz.drawio&#39; removed"):
        assert shown in html, shown


def test_baseline_version_is_not_marked_as_inferred():
    page = _page("10", "Batch", summary="x")
    page.result.old.inferred = True
    page.result.old.inferred_reason = "from date 2026-10-30"
    html = pr.render_mail_html(_report(pages=[page]))
    assert "abgeleitet" not in html and "from date" not in html


def test_nothing_changed_says_so_with_window():
    report = _report(pages=[PageChange(page_id="12", title="Alt", url="https://x/12", status=PageStatus.UNCHANGED)],
                     synthesis=None)
    html = pr.render_mail_html(report)
    assert "Keine Änderungen im Zeitraum" in html and "2026-10-31" in html
    assert 'id="summary"' not in html and "1 Seiten ohne relevante Änderung" in html and ">Alt<" not in html


def test_errors_table_banner_and_subject():
    report = _report(errors=[ReportError(scope="Batch", message="HTTP 403", attempts=3, impact="fehlt")])
    html = pr.render_mail_html(report)
    assert "HTTP 403" in html and "Problem(e) aufgetreten" in html
    assert pr.problem_count(report) == 1
    subject = pr.build_subject("[Digest]", report)
    assert subject == "[Digest] 13.11.2026: 2 Seiten mit Änderungen, 1 Problem(e)"


def test_footer_writes_cache_hits_and_misses():
    report = _report(cache_hits=12, cache_misses=3)
    html = pr.render_mail_html(report)
    assert "cache hits &amp; misses: 12 &amp; 3" in html and "Cache-Treffer" not in html
    assert "cache hits &amp; misses: 12 &amp; 3" in pr.render_confluence_storage(report)


def test_cache_write_errors_count_as_problems():
    report = _report(cache_write_errors=2, cache_write_error="disk full")
    assert pr.problem_count(report) == 1 and "disk full" in pr.render_mail_html(report)


def test_incomplete_analysis_is_flagged_and_pages_stay_visible():
    report = _report(synthesis=None, analysis_incomplete=True)
    html = pr.render_mail_html(report)
    assert "Analyse unvollständig" in html and "Retry von 3 auf 5" in html


def test_english_labels():
    html = pr.render_mail_html(_report(language="en"))
    assert "Overall summary" in html and "Change request" in html and "Page changes of the specification" in html


def test_html_is_escaped_and_unsafe_urls_dropped():
    page = _page("10", "<script>alert(1)</script>", summary="<b>x</b>")
    page.url = "javascript:alert(1)"
    html = pr.render_mail_html(_report(pages=[page]))
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript:' not in html


def test_confluence_storage_has_empty_effort_placeholders_and_no_ai_estimate():
    storage = pr.render_confluence_storage(_report())
    assert "Änderungsantrag" in storage and "Delta-Spezifikation" in storage and "Gesamtzusammenfassung" in storage
    assert "3-5" not in storage and "ein Modul" not in storage
    assert "<p />" in storage
    assert storage.index("<h2>Batch</h2>") < storage.index("<h2>Gesamtzusammenfassung")


def test_confluence_storage_lists_errors_and_escapes():
    report = _report(errors=[ReportError(scope="<x>", message="m", impact="i")])
    storage = pr.render_confluence_storage(report)
    assert "&lt;x&gt;" in storage and "<x>" not in storage


def test_weekly_header_names_the_week_and_both_dates():
    from digest.spec_monitor.week import parse_week
    week = parse_week("KW44-2026", date(2026, 11, 15))
    report = _report(week=week, window_start=week.start, window_end=week.end, window_days=7)
    html = pr.render_mail_html(report)
    assert "KW44 2026" in html and "2026-10-26" in html and "2026-11-01" in html
    assert "KW44 2026" in pr.render_confluence_storage(report)


def test_rolling_header_is_unchanged():
    html = pr.render_mail_html(_report())
    assert "KW" not in html.split("Zeitraum", 1)[1][:200] and "2026-10-31 bis 2026-11-13 (14)" in html
def _ref(ref="PSSA-1", verdict="in_progress", own=False, url="https://j/PSSA-1", reason="Ticket zu Retry"):
    return TraceReference("jira", ref, f"{ref} Retry", url, verdict, reason, own)


def _traced(trace, lang="de", enabled=True, **kw):
    pages = [_page("10", "Batch", summary="Retry von 3 auf 5", trace=trace)]
    return _report(pages=pages, trace_enabled=enabled, language=lang, **kw)


def _both(report):
    return pr.render_mail_html(report), pr.render_confluence_storage(report)


def test_found_trace_shows_headline_and_at_most_three_references():
    refs = [_ref(f"PSSA-{i}", reason=f"Grund {i}") for i in range(1, 6)]
    refs.append(_ref("PSSA-9", "done_likely"))
    for out in _both(_traced(PageTrace("FOUND", refs, searched=["Jira PSSA: x"]))):
        assert "Vermutlich bereits CR / in Arbeit" in out
        assert out.count("https://j/PSSA-") == 3 and "PSSA-4" not in out
        assert "in Arbeit" in out and "Grund 1" in out and "Geprüft:" in out


def test_verdict_labels():
    refs = [_ref("A-1", "done_likely"), _ref("A-2", "related")]
    for out in _both(_traced(PageTrace("FOUND", refs))):
        assert "vermutlich erledigt" in out and "verwandt" in out


def test_none_trace_says_no_hints_with_the_searched_line_and_prior_ticket():
    trace = PageTrace("NONE", [_ref("PSSA-7", "related", own=True, reason="")],
                      searched=["Jira PSSA: 90 Tage", "Code: kein Repository"])
    for out in _both(_traced(trace)):
        assert "Keine Hinweise gefunden, dass das bereits in Arbeit ist." in out
        assert "Geprüft:" in out and "Code: kein Repository" in out
        assert "Früheres Delta-Ticket" in out and "PSSA-7" in out
        assert "Vermutlich bereits CR" not in out
    plain = _traced(PageTrace("NONE", searched=["Jira PSSA"]))
    for out in _both(plain):
        assert "Früheres Delta-Ticket" not in out


def test_frozen_trace_shows_only_the_frozen_text():
    for out in _both(_traced(PageTrace("FROZEN", searched=["x"]))):
        assert "Seite ist frozen – keine Prüfung" in out and "Geprüft:" not in out


def test_incomplete_trace_shows_problem_and_never_the_none_sentence():
    trace = PageTrace("INCOMPLETE", searched=["Jira PSSA"], problems=["Jira search failed: HTTP 500"])
    for out in _both(_traced(trace)):
        assert "Prüfung nicht (vollständig) möglich" in out and "HTTP 500" in out
        assert "Keine Hinweise gefunden" not in out


def test_no_trace_means_no_hint_line_and_a_single_not_configured_note():
    html, storage = _both(_traced(None, enabled=False))
    for out in (html, storage):
        assert out.count("Spurensuche nicht konfiguriert") == 1
        assert "Keine Hinweise" not in out and "Geprüft:" not in out
    html, storage = _both(_traced(None, enabled=True))
    assert "Spurensuche nicht konfiguriert" not in html + storage


def test_trace_enabled_report_has_no_not_configured_note():
    html, storage = _both(_traced(PageTrace("NONE", searched=["Jira"])))
    assert "Spurensuche nicht konfiguriert" not in html + storage


def test_english_trace_labels():
    for out in _both(_traced(PageTrace("FOUND", [_ref()], searched=["Jira"]), lang="en")):
        assert "Likely already a CR / in progress" in out and "Checked:" in out and "in progress" in out
        assert "Vermutlich" not in out
    for out in _both(_traced(PageTrace("NONE", searched=["Jira"]), lang="en")):
        assert "No indication found that this is already in progress." in out
    html, storage = _both(_traced(None, lang="en", enabled=False))
    assert "Trace check not configured" in html and "Trace check not configured" in storage


def test_trace_texts_are_escaped_and_unsafe_urls_dropped():
    ref = _ref(url="javascript:alert(1)", reason="<b>x</b>")
    ref.label = "<i>l</i>"
    for out in _both(_traced(PageTrace("FOUND", [ref], searched=["<s>"]))):
        assert "<b>x</b>" not in out and "<i>l</i>" not in out and "<s>" not in out
        assert "javascript:" not in out


def _actions_report(**over):
    from digest.spec_monitor import page_watch_data as pd
    from digest.spec_monitor.week import parse_week
    week = parse_week("KW44-2026", date(2026, 11, 15))
    report = _report(week=week, window_start=week.start, window_end=week.end, **over)
    report.pages[0].labels = ["frozen"]
    report.pages[1].labels = []
    return report, pd.build_data(report, "frozen")


def test_storage_with_actions_has_task_boxes_per_page_and_hides_frozen_for_a_frozen_page():
    report, data = _actions_report()
    storage = pr.render_confluence_storage(report, actions=data)
    for tid in (12, 13, 21, 22, 23):
        assert f"<ac:task-id>{tid}</ac:task-id>" in storage, tid
    assert "<ac:task-id>11</ac:task-id>" not in storage
    assert "Jira-Ticket erstellen" in storage and "CR erstellen" in storage and "frozen page" in storage


def test_task_boxes_come_after_the_hint_and_before_the_delta():
    report, data = _actions_report(trace_enabled=True)
    report.pages[0].trace = PageTrace("NONE", searched=["Jira PSSA"])
    storage = pr.render_confluence_storage(report, actions=data)
    page = storage[storage.index("<h2>Batch</h2>"):]
    assert page.index("Keine Hinweise") < page.index("<ac:task-id>12</ac:task-id>") < page.index("<h3>Delta-Spezifikation")


def test_english_task_labels():
    report, data = _actions_report(language="en")
    storage = pr.render_confluence_storage(report, actions=data)
    assert "create Jira ticket" in storage and "create CR" in storage


def test_done_actions_render_as_a_done_line():
    report, data = _actions_report()
    data.pages[0].actions["ticket"].status = "done"
    data.pages[0].actions["ticket"].result = "PSSA-5"
    storage = pr.render_confluence_storage(report, actions=data)
    assert "<ac:task-id>12</ac:task-id>" not in storage and "PSSA-5" in storage


def test_storage_without_actions_has_no_tasks():
    report, _ = _actions_report()
    assert "<ac:task" not in pr.render_confluence_storage(report)
    assert "<ac:task" not in pr.render_mail_html(report)


def test_confluence_intro_explains_the_page_and_the_boxes():
    from digest.spec_monitor.page_watch_data import build_data
    from digest.spec_monitor.week import parse_week
    week = parse_week("KW44-2026", date(2026, 11, 15))
    report = _report(week=week, window_start=week.start, window_end=week.end, trace_enabled=True)
    storage = pr.render_confluence_storage(report, actions=build_data(report, "frozen"))
    assert storage.count('ac:name="info"') == 1
    for text in ("Über diese Seite", "Diese Seite zeigt", "Hinweise, kein Beweis", "--page-watch-apply",
                 "Änderung ablehnen: nur eine Markierung", "frozen page: setzt das Label", "Jira-Ticket erstellen: legt",
                 "CR erstellen: nimmt"):
        assert text in storage
    assert storage.index('ac:name="info"') < storage.index("<h2>")


def test_confluence_intro_without_boxes_and_without_trace_stays_short():
    storage = pr.render_confluence_storage(_report())
    assert "Diese Seite zeigt" in storage
    assert "--page-watch-apply" not in storage and "Hinweise, kein Beweis" not in storage


def test_confluence_intro_in_english():
    from digest.spec_monitor.page_watch_data import build_data
    from digest.spec_monitor.week import parse_week
    week = parse_week("KW44-2026", date(2026, 11, 15))
    report = _report(week=week, window_start=week.start, window_end=week.end, language="en")
    storage = pr.render_confluence_storage(report, actions=build_data(report, "frozen"))
    assert "About this page" in storage and "reject change: only a marker" in storage
    assert "Über diese Seite" not in storage


def test_mail_has_no_intro_box():
    assert "Über diese Seite" not in pr.render_mail_html(_report())
