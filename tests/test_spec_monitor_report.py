import re
from datetime import date, datetime

import pytest

from digest.spec_monitor.models import (
    Classification, DeltaChange, EffortEstimate, OpenPoint, ReportError, RunReport,
    SourceKind, SourceRef, SourceResult, SourceStep, SpecAnalysis, SpecInfo, VersionPoint,
)
from digest.spec_monitor.report import build_subject, render_confluence_storage, render_mail_html


def _spec(title="API Alpha", page_id="111", status="Refined"):
    return SpecInfo(page_id=page_id, title=title, url=f"https://wiki.example.com/pages/{page_id}",
                    status=status, jira=["PROJ-1"])


def _changed_result(name="Quelle Eins", inferred=False):
    return SourceResult(
        ref=SourceRef(name=name, kind=SourceKind.CONFLUENCE, used_for="Berechtigungen",
                      url="https://wiki.example.com/pages/900"),
        classification=Classification.CHANGED,
        old=VersionPoint(version="86", when=datetime(2026, 10, 16), url="https://wiki.example.com/v86",
                         inferred=inferred, inferred_reason="from Stand date 2026-10-27" if inferred else None),
        new=VersionPoint(version="90", when=datetime(2026, 11, 5), url="https://wiki.example.com/v90"),
        diff_summary="Neues Feld requestInProgress ergaenzt",
    )


def _analysis(title="API Alpha", page_id="111", inferred=False):
    return SpecAnalysis(
        spec=_spec(title, page_id),
        sources=[
            _changed_result(inferred=inferred),
            SourceResult(ref=SourceRef(name="Quelle Zwei", kind=SourceKind.CONFLUENCE),
                         classification=Classification.UNCHANGED,
                         old=VersionPoint(version="4"), new=VersionPoint(version="4")),
            SourceResult(ref=SourceRef(name="Notizen intern", kind=SourceKind.CONFLUENCE,
                                       excluded_reason="Arbeitsnotiz"),
                         classification=Classification.EXCLUDED),
        ],
        completeness_warnings=["Diagramm geaendert, nicht verglichen"],
        change_request="Ausgangslage: Das Modul braucht ein neues Feld.\n\nRisiko: ohne Anpassung inkonsistent.",
        delta_changes=[
            DeltaChange(kind="changed", affected_part="3.1 Antrag", ids=["CM-FR-03"],
                        current_statement="Alt-Aussage", change="Neue Aussage", reason="Quelle Eins v86 -> v90",
                        blocking=True),
            DeltaChange(kind="new", affected_part="4.2 Status", change="Status ergaenzen"),
            DeltaChange(kind="removed", affected_part="5 Alt", change="entfaellt"),
        ],
        open_points=[OpenPoint(question="Wer pflegt das Feld?", raised_by="Quelle Eins")],
        effort=EffortEstimate(size="M", person_days="3-5", rationale="UNIQUE-RATIONALE-XYZ"),
    )


def _report(**kw):
    base = dict(
        generated_at=datetime(2026, 11, 10, 8, 30),
        analysed=[
            _analysis(),
            SpecAnalysis(spec=_spec("API Beta", "222"),
                         sources=[SourceResult(ref=SourceRef(name="Q", kind=SourceKind.CONFLUENCE),
                                               classification=Classification.UNCHANGED)]),
        ],
        listed_only=[_spec("API Gamma", "333", status="In Ausarbeitung")],
        excluded_pages=[("444", "Regeln DossierNode")],
        command_line="--spec-monitor",
        cache_hits=3, cache_misses=2,
    )
    base.update(kw)
    return RunReport(**base)


def _report_with_error():
    return _report(errors=[ReportError(scope="API Alpha / Quelle Drei", message="HTTP 403",
                                       attempts=3, impact="Quelle nicht geprueft")])


class TestMail:
    def test_toc_first_links_specs_and_errors(self):
        html = render_mail_html(_report())
        toc = html.index("Inhaltsverzeichnis")
        assert toc < html.index('id="overview"')
        assert toc < html.index('id="spec-111"')
        assert 'href="#spec-111"' in html
        assert 'href="#errors"' in html
        assert 'id="errors"' in html
        assert 'href="#spec-222"' not in html

    def test_section_order(self):
        html = render_mail_html(_report())
        sec = html[html.index('id="spec-111"'):]
        order = [sec.index(m) for m in
                 ("Quelle Eins", "Diagramm geaendert", "Änderungsantrag", "UNIQUE-RATIONALE-XYZ",
                  "Delta-Spezifikation", "Wer pflegt das Feld?")]
        assert order == sorted(order)
        assert sec.index("Delta-Spezifikation") < sec.index("Offene Punkte")

    def test_source_table_old_vs_new(self):
        html = render_mail_html(_report())
        assert "v86" in html and "v90" in html
        assert "geändert" in html and "unverändert" in html
        assert "ausgeschlossen" in html and "Arbeitsnotiz" in html

    def test_inferred_labelled(self):
        html = render_mail_html(_report(analysed=[_analysis(inferred=True)]))
        assert "abgeleitet" in html
        assert "from Stand date 2026-10-27" in html
        assert "abgeleitet" not in render_mail_html(_report())

    def test_delta_grouped_and_blocking_marked(self):
        html = render_mail_html(_report())
        delta = html[html.index("Delta-Spezifikation"):]
        assert delta.index("Neu") < delta.index("Geändert") < delta.index("Entfallen")
        assert "CM-FR-03" in html
        assert "blockierend" in html.lower()

    def test_overview_lists(self):
        html = render_mail_html(_report())
        for t in ("API Beta", "API Gamma", "In Ausarbeitung", "Regeln DossierNode"):
            assert t in html
        assert "ohne Änderungen" in html
        assert "Nicht analysiert" in html
        assert "Ausgeschlossene Seiten" in html

    def test_banner_only_with_errors(self):
        assert 'class="problem-banner"' not in render_mail_html(_report())
        html = render_mail_html(_report_with_error())
        assert 'class="problem-banner"' in html
        assert "1 Problem" in html

    def test_errors_section_content(self):
        html = render_mail_html(_report_with_error())
        err = html[html.index('id="errors"'):]
        for t in ("API Alpha / Quelle Drei", "HTTP 403", "3", "Quelle nicht geprueft"):
            assert t in err

    def test_source_error_result_listed_in_errors(self):
        a = _analysis()
        a.sources.append(SourceResult(ref=SourceRef(name="Kaputt", kind=SourceKind.REPO),
                                      classification=Classification.ERROR,
                                      errors=["repository not reachable (timeout)"], attempts=3))
        html = render_mail_html(_report(analysed=[a]))
        err = html[html.index('id="errors"'):]
        assert "repository not reachable (timeout)" in err and "Kaputt" in err

    def test_unsafe_urls_not_linked(self):
        a = _analysis()
        a.spec.url = "javascript:alert(1)"
        a.sources[0].ref.url = "javascript:alert(2)"
        a.sources[0].new.url = "javascript:alert(3)"
        html = render_mail_html(_report(analysed=[a]))
        assert "javascript:" not in html

    def test_html_escaped(self):
        a = _analysis()
        a.change_request = "<script>alert(1)</script>"
        html = render_mail_html(_report(analysed=[a]))
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_effort_estimate_in_mail(self):
        html = render_mail_html(_report())
        assert "3-5" in html and "UNIQUE-RATIONALE-XYZ" in html

    def test_analysis_incomplete_notice(self):
        a = _analysis()
        a.analysis_incomplete = True
        a.change_request = None
        a.delta_changes = []
        a.open_points = []
        a.effort = None
        html = render_mail_html(_report(analysed=[a]))
        assert "Analyse unvollständig" in html
        assert "Quelle Eins" in html

    def test_not_monitored_listed_as_limitation(self):
        a = _analysis()
        a.sources.append(SourceResult(ref=SourceRef(name="BFS Seite", kind=SourceKind.EXTERNAL,
                                                    url="https://bfs.example.com/x"),
                                      classification=Classification.NOT_MONITORED,
                                      warnings=["nicht ueberwachbar"]))
        html = render_mail_html(_report(analysed=[a]))
        assert "Einschränkungen" in html
        assert "BFS Seite" in html

    def test_footer(self):
        html = render_mail_html(_report())
        assert "--spec-monitor" in html
        assert "3" in html

    def test_english_fallback(self):
        html = render_mail_html(_report(language="en"))
        assert "Table of contents" in html
        assert "Inhaltsverzeichnis" not in html

    def test_no_changes_report_renders(self):
        html = render_mail_html(_report(analysed=[], listed_only=[], excluded_pages=[]))
        assert 'id="errors"' in html


class TestSubject:
    def test_subject(self):
        s = build_subject("[Spec-Monitor]", _report())
        assert s.startswith("[Spec-Monitor]")
        assert "1" in s
        assert "10.11.2026" in s

    def test_subject_mentions_problems(self):
        assert "Problem" in build_subject("[X]", _report_with_error())
        assert "Problem" not in build_subject("[X]", _report())


class TestConfluenceStorage:
    def test_starts_with_toc_macro(self):
        out = render_confluence_storage(_report())
        assert out.lstrip().startswith('<ac:structured-macro ac:name="toc"')

    def test_no_ai_estimate_anywhere(self):
        out = render_confluence_storage(_report())
        assert "UNIQUE-RATIONALE-XYZ" not in out
        assert "3-5" not in out
        assert "KI" not in out

    def test_effort_placeholders(self):
        out = render_confluence_storage(_report())
        empty = "<td><p /></td>"
        summary = out[:out.index("API Alpha", out.index("Aufwand"))]
        assert "Personentage" in summary
        assert empty in out
        assert out.count("Summe") == 1
        sum_row = re.search(r"<tr><td[^>]*>Summe</td>(.*?)</tr>", out, re.S).group(1)
        assert sum_row.count(empty) >= 1 and re.sub(r"<td><p /></td>|<td[^>]*></td>", "", sum_row) == ""
        assert "Grösse" in out and "Bemerkung" in out

    def test_summary_table_one_row_per_spec_with_changes(self):
        out = render_confluence_storage(_report())
        first_table = out[out.index("<table"):out.index("</table>")]
        assert first_table.count("API Alpha") == 1
        assert "API Beta" not in first_table

    def test_tables_full_width(self):
        out = render_confluence_storage(_report())
        assert out.count("<table") == out.count('<table data-layout="full-width"')
        assert out.count("<table") >= 4

    def test_escaping(self):
        a = _analysis()
        a.delta_changes[0].change = "a < b & <b>c</b>"
        a.change_request = "x <script>y</script> & z"
        out = render_confluence_storage(_report(analysed=[a]))
        assert "a &lt; b &amp; &lt;b&gt;c&lt;/b&gt;" in out
        assert "<script>" not in out

    def test_unsafe_urls_not_linked(self):
        a = _analysis()
        a.spec.url = "javascript:alert(1)"
        a.sources[0].new.url = "javascript:alert(3)"
        out = render_confluence_storage(_report(analysed=[a]))
        assert "javascript:" not in out

    def test_section_order(self):
        out = render_confluence_storage(_report())
        sec = out[out.index("<h2>API Alpha"):]
        order = [sec.index(m) for m in
                 ("Quelle Eins", "Diagramm geaendert", "Änderungsantrag", "Delta-Spezifikation",
                  "Wer pflegt das Feld?")]
        assert order == sorted(order)

    def test_inferred_labelled(self):
        out = render_confluence_storage(_report(analysed=[_analysis(inferred=True)]))
        assert "abgeleitet" in out

    def test_errors_section_and_banner(self):
        out = render_confluence_storage(_report_with_error())
        assert "HTTP 403" in out and "Quelle nicht geprueft" in out
        assert "1 Problem" in out
        assert "Problem" not in render_confluence_storage(_report())

    def test_overview_lists(self):
        out = render_confluence_storage(_report())
        for t in ("API Beta", "API Gamma", "Regeln DossierNode"):
            assert t in out


def _with_steps(report):
    src = report.analysed[0].sources[0]
    src.steps = [
        SourceStep(version="87", when=datetime(2026, 10, 20, 9, 0), diff_text="- a\n+ b\n+ c",
                   summary="Feld X <b>ergaenzt</b>"),
        SourceStep(version="89", when=datetime(2026, 11, 2, 9, 0), diff_text="+ nur eine Zeile"),
    ]
    return report


class TestNoHistoryList:
    def test_mail_omits_version_history(self):
        html = render_mail_html(_with_steps(_report()))
        assert "Verlauf" not in html and "geänderte Zeilen" not in html

    def test_storage_omits_version_history(self):
        out = render_confluence_storage(_with_steps(_report()))
        assert "Verlauf" not in out and "geänderte Zeilen" not in out

    def test_english_omits_version_history(self):
        out = render_confluence_storage(_with_steps(_report(language="en")))
        assert "History" not in out and "changed lines" not in out


def test_completeness_warnings_of_unchanged_spec_count_as_problems():
    from datetime import datetime
    from digest.spec_monitor.models import RunReport, SpecAnalysis, SpecInfo
    from digest.spec_monitor.report import _all_errors, _problem_count
    spec = SpecInfo(page_id="1", title="Spec A", url="u", status="Final", jira=[], stand_date=None,
                    created=None, body_storage="", ancestor_ids=[], ancestor_titles=[])
    a = SpecAnalysis(spec=spec, completeness_warnings=["Quellenzeile nicht auswertbar: no sources found on the page"])
    report = RunReport(generated_at=datetime.now().astimezone(), analysed=[a])
    assert _problem_count(report) == 1
    assert "no sources found" in _all_errors(report)[0].message


def test_cache_write_failures_are_a_problem_and_corrupt_entries_are_in_the_footer():
    from datetime import datetime
    from digest.spec_monitor.models import RunReport
    from digest.spec_monitor.report import _all_errors, _problem_count, render_confluence_storage, render_mail_html
    report = RunReport(generated_at=datetime.now().astimezone(), cache_corrupt=3,
                       cache_write_errors=2, cache_write_error="PermissionError: denied")
    assert _problem_count(report) == 1
    assert "2 cache write(s) failed" in _all_errors(report)[0].message
    assert "3" in render_mail_html(report) and "PermissionError: denied" in render_mail_html(report)
    assert "corrupt cache entries" in render_confluence_storage(report) or "defekte Cache-Einträge" in render_confluence_storage(report)


def test_linkify_makes_urls_clickable_and_escapes_text():
    from digest.spec_monitor.report import _linkify

    out = str(_linkify("see «https://faq.easygov.swiss/en/x?a=1&b=2». <b>x</b> javascript:alert(1)"))
    assert '<a href="https://faq.easygov.swiss/en/x?a=1&amp;b=2" target="_blank" rel="noopener">' in out
    assert '>https://faq.easygov.swiss/en/x?a=1&amp;b=2</a>».' in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out
    assert "<a href=\"javascript" not in out


def test_diff_link_is_rendered_next_to_new_version_in_mail_and_storage():
    analysis = _analysis()
    analysis.sources[0].diff_url = "https://wiki.example.com/diff?pageId=900&selectedPageVersions=86&selectedPageVersions=90"
    report = _report(analysed=[analysis])

    mail = render_mail_html(report)
    assert 'href="https://wiki.example.com/diff?pageId=900&amp;selectedPageVersions=86&amp;selectedPageVersions=90"' in mail
    assert mail.count(">Diff</a>") == 1

    storage = render_confluence_storage(report)
    assert 'selectedPageVersions=86&amp;selectedPageVersions=90">Diff</a>' in storage
