from datetime import date, datetime
from html import escape
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from digest.config import LLMConfig, RetryConfig, SpecMonitorConfig
from digest.spec_monitor import spec_update
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.models import (
    Classification, DeltaChange, ReportError, RunReport, SourceKind, SourceRef, SourceResult, SpecAnalysis,
    SpecInfo, VersionPoint,
)
from digest.spec_monitor.parser import parse_sources

TODAY = date(2026, 11, 10)


def link(title):
    return f'<ac:link><ri:page ri:content-title="{escape(title)}" /></ac:link>'


def links(*titles):
    return ", ".join(link(t) for t in titles)


def row(src, ver="", used=""):
    return f"<tr><td>{src}</td><td>{ver}</td><td>{used}</td></tr>"


def table(*rows):
    return ("<table><tbody><tr><th>Quelle</th><th>Gelesene Version</th><th>Verwendet für</th></tr>"
            + "".join(rows) + "</tbody></table>")


def page(*rows, stand='<p>Stand: Entwurf zur Klärung, <time datetime="2026-10-27" /></p>', extra=""):
    return f"<h1>Spec</h1>{stand}{extra}<h2>1.5 Quellen</h2>{table(*rows)}"


def vp(version, when=None, inferred=False):
    return VersionPoint(version=version, when=when, inferred=inferred)


def confluence(title, stated, new, when=datetime(2026, 11, 9, 12, 0), cls=Classification.CHANGED, version_text="", **kw):
    ref = SourceRef(name=title, kind=SourceKind.CONFLUENCE, title=title, baseline_version=stated,
                    version_text=version_text, **kw)
    return SourceResult(ref=ref, classification=cls, old=vp(stated or "1"), new=vp(new, when))


def cells(storage):
    out = []
    for tr in storage.split("<tr>")[2:]:
        out.append([c.split("</td>")[0] for c in tr.split("<td>")[1:]])
    return out


def ver_cells(storage):
    return [c[1] for c in cells(storage)]


# ---------------------------------------------------------------- Quellen table cells

def test_single_cell_with_comma_date_style():
    s = page(row(link("Fach A"), "v86, 16. Okt. 2026"))
    new, changes, manual = spec_update.update_sources_table(
        s, [confluence("Fach A", "86", "90", version_text="v86, 16. Okt. 2026")], TODAY)
    assert ver_cells(new) == ["v90 (09.11.2026)"]
    assert manual == []
    assert changes and "Fach A" in changes[0] and "v90 (09.11.2026)" in changes[0]
    (ref,) = parse_sources(new, stand_date=None, excluded_markers=[]).refs
    assert ref.baseline_version == "90" and ref.baseline_date == date(2026, 11, 9)


def test_single_cell_with_parenthesised_date_and_paragraph_wrapper():
    s = page(row(link("Fach A"), "<p>v258 (09.11.2026)</p>"))
    new, _, _ = spec_update.update_sources_table(
        s, [confluence("Fach A", "258", "260", when=datetime(2026, 11, 10, 8, 0), version_text="v258 (09.11.2026)")], TODAY)
    assert ver_cells(new) == ["<p>v260 (10.11.2026)</p>"]


def test_unstated_baseline_inferred_gets_version_written():
    s = page(row(link("Fach A"), "nicht erfasst"))
    r = confluence("Fach A", None, "12", version_text="nicht erfasst")
    r.old = vp("11", inferred=True)
    new, changes, _ = spec_update.update_sources_table(s, [r], TODAY)
    assert ver_cells(new) == ["v12 (09.11.2026)"]
    assert len(changes) == 1


def test_gelesen_am_single_link_becomes_version_style():
    s = page(row(link("Fach A"), "gelesen am 2026-11-05"))
    new, _, _ = spec_update.update_sources_table(
        s, [confluence("Fach A", None, "7", cls=Classification.UNCHANGED, version_text="gelesen am 2026-11-05")], TODAY)
    assert ver_cells(new) == ["v7 (09.11.2026)"]


def test_positional_list_preserves_format_and_untouched_entries():
    s = page(row(links("A", "B", "C"), "v187, v352, v91"))
    results = [
        confluence("A", "187", "200", version_text="v187, v352, v91"),
        confluence("B", "352", "352", cls=Classification.UNCHANGED, version_text="v187, v352, v91"),
        confluence("C", "91", "95", version_text="v187, v352, v91"),
    ]
    new, changes, manual = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ["v200, v352, v95"]
    assert manual == [] and len(changes) == 1
    assert "A, C" in changes[0]


def test_positional_list_with_dates_updates_each_date():
    s = page(row(links("A", "B"), "v46 (01.10.2026), v68 (02.10.2026)"))
    results = [
        confluence("A", "46", "50", when=datetime(2026, 11, 3), version_text="v46 (01.10.2026), v68 (02.10.2026)"),
        confluence("B", "68", "68", cls=Classification.UNCHANGED, version_text="v46 (01.10.2026), v68 (02.10.2026)"),
    ]
    new, _, _ = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ["v50 (03.11.2026), v68 (02.10.2026)"]


def test_positional_list_blocked_member_stays_and_is_manual():
    s = page(row(links("A", "B"), "v46, v68"))
    err = SourceResult(ref=SourceRef(name="B", kind=SourceKind.CONFLUENCE, title="B", baseline_version="68",
                                     version_text="v46, v68"),
                       classification=Classification.ERROR, errors=["B: HTTP 403"])
    new, changes, manual = spec_update.update_sources_table(
        s, [confluence("A", "46", "50", version_text="v46, v68"), err], TODAY)
    assert ver_cells(new) == ["v50, v68"]
    assert any("B (error)" in m and "HTTP 403" in m for m in manual)


def test_shared_date_cell_replaced_by_new_date_keeping_format():
    s = page(row(links("A", "B"), "gelesen am 2026-11-05"))
    results = [confluence("A", None, "3", when=datetime(2026, 11, 8), version_text="gelesen am 2026-11-05"),
               confluence("B", None, "9", when=datetime(2026, 11, 9), version_text="gelesen am 2026-11-05")]
    new, changes, manual = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ["gelesen am 2026-11-09"]
    assert manual == [] and len(changes) == 1


def test_shared_date_dotted_format():
    s = page(row(links("A", "B"), "Stand 02.11.2026"))
    results = [confluence("A", None, "3", version_text="Stand 02.11.2026"),
               confluence("B", None, "9", version_text="Stand 02.11.2026")]
    new, _, _ = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ["Stand 09.11.2026"]


def test_shared_date_not_touched_when_one_link_failed():
    s = page(row(links("A", "B"), "gelesen am 2026-11-05"))
    err = SourceResult(ref=SourceRef(name="B", kind=SourceKind.CONFLUENCE, title="B", version_text="gelesen am 2026-11-05"),
                       classification=Classification.ERROR, errors=["gone"])
    new, changes, manual = spec_update.update_sources_table(
        s, [confluence("A", None, "3", version_text="gelesen am 2026-11-05"), err], TODAY)
    assert ver_cells(new) == ["gelesen am 2026-11-05"]
    assert changes == []
    assert any("B (error)" in m for m in manual)
    assert any(m.startswith("A:") for m in manual)


def test_no_version_cell_for_several_links_gets_positional_list():
    s = page(row(links("A", "B"), "nicht erfasst"))
    results = [confluence("A", None, "3", version_text="nicht erfasst"),
               confluence("B", None, "9", version_text="nicht erfasst")]
    new, _, manual = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ["v3, v9"]
    assert manual == []


def test_error_not_monitored_excluded_rows_untouched_and_manual():
    rows = [
        row(link("Err"), "v1"), row("Externes Dokument", "v2"), row(link("Alt"), "v3", "Arbeitsnotizen"),
        row(link("Ok"), "v4"),
    ]
    s = page(*rows)
    results = [
        SourceResult(ref=SourceRef(name="Err", kind=SourceKind.CONFLUENCE, title="Err", baseline_version="1", version_text="v1"),
                     classification=Classification.ERROR, errors=["HTTP 500"]),
        SourceResult(ref=SourceRef(name="Externes Dokument", kind=SourceKind.EXTERNAL, version_text="v2"),
                     classification=Classification.NOT_MONITORED, warnings=["check manually"]),
        SourceResult(ref=SourceRef(name="Alt", kind=SourceKind.CONFLUENCE, title="Alt", baseline_version="3", version_text="v3",
                                   excluded_reason="marker"),
                     classification=Classification.EXCLUDED, warnings=["marker"]),
        confluence("Ok", "4", "8", version_text="v4"),
    ]
    new, changes, manual = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ["v1", "v2", "v3", "v8 (09.11.2026)"]
    assert len(manual) == 3
    assert len(changes) == 1


def test_ech_row_without_link_is_matched_by_name_and_version_text():
    ech = SourceResult(
        ref=SourceRef(name="eCH-0108", kind=SourceKind.ECH, baseline_version="5.0", version_text="v5.0"),
        classification=Classification.CHANGED, old=vp("5.0"), new=vp("6.0.0", datetime(2026, 9, 1)))
    s = page(row("eCH-0108 Unternehmens-Identifikation", "v5.0"))
    new, _, manual = spec_update.update_sources_table(s, [ech], TODAY)
    assert ver_cells(new) == ["v6.0.0 (01.09.2026)"]
    assert manual == []


def test_non_confluence_without_date_writes_only_version():
    r = SourceResult(
        ref=SourceRef(name="Std", kind=SourceKind.URL, url="https://example.org/std", baseline_version="2.0", version_text="v2.0"),
        classification=Classification.CHANGED, old=vp("2.0"), new=vp("2.1"))
    s = page(row('<a href="https://example.org/std">Std</a>', "v2.0"))
    new, _, _ = spec_update.update_sources_table(s, [r], TODAY)
    assert ver_cells(new) == ["v2.1"]


def test_unchanged_current_version_is_not_touched():
    s = page(row(link("A"), "v5 (01.11.2026)"))
    new, changes, manual = spec_update.update_sources_table(
        s, [confluence("A", "5", "5", cls=Classification.UNCHANGED, version_text="v5 (01.11.2026)")], TODAY)
    assert new == s and changes == [] and manual == []


def test_shared_date_lozenge_cell_is_rewritten_keeping_iso():
    s = page(row(links("A", "B"), '<time datetime="2026-11-05" />'))
    results = [confluence("A", None, "3", when=datetime(2026, 11, 8)),
               confluence("B", None, "9", when=datetime(2026, 11, 9))]
    new, _, manual = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ['<time datetime="2026-11-09" />']
    assert manual == []


def test_positional_list_with_date_lozenges_updates_versions_and_dates():
    ver = 'v5 <time datetime="2026-09-01" /> v8 <time datetime="2026-09-02" />'
    s = page(row(links("A", "B"), ver))
    results = [confluence("A", "5", "7", when=datetime(2026, 10, 1)),
               confluence("B", "8", "9", when=datetime(2026, 10, 2))]
    new, _, manual = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ['v7 <time datetime="2026-10-01" /> v9 <time datetime="2026-10-02" />']
    assert manual == []


def test_positional_list_lozenge_only_updated_member_changes():
    ver = 'v5 <time datetime="2026-09-01" /> v8 <time datetime="2026-09-02" />'
    s = page(row(links("A", "B"), ver))
    results = [confluence("A", "5", "7", when=datetime(2026, 10, 1)),
               confluence("B", "8", "8", cls=Classification.UNCHANGED)]
    new, _, _ = spec_update.update_sources_table(s, results, TODAY)
    assert ver_cells(new) == ['v7 <time datetime="2026-10-01" /> v8 <time datetime="2026-09-02" />']


def test_unchanged_source_with_inferred_baseline_is_not_upd():
    r = confluence("A", None, "5", cls=Classification.UNCHANGED, version_text="2026-11-01")
    r.old = vp("5", inferred=True)
    assert spec_update._state(r) == "keep"
    s = page(row(link("A"), "2026-11-01"))
    new, changes, manual = spec_update.update_sources_table(s, [r], TODAY)
    assert new == s and changes == [] and manual == []


def test_no_sources_table_is_reported_manual():
    new, changes, manual = spec_update.update_sources_table("<h1>x</h1><p>text</p>", [confluence("A", "1", "2")], TODAY)
    assert new == "<h1>x</h1><p>text</p>"
    assert manual and "Quellen" in manual[0]


# ---------------------------------------------------------------- Stand date

def test_stand_time_element_set_to_today():
    s = page(row(link("A"), "v1"))
    new, change = spec_update.update_stand_date(s, TODAY)
    assert 'Stand: Entwurf zur Klärung, <time datetime="2026-11-10" />' in new
    assert change == "Stand: 2026-10-27 -> 2026-11-10"


def test_stand_absent_or_current_is_noop():
    for s in ("<p>no panel</p>", '<p>Stand: <time datetime="2026-11-10"/></p>'):
        new, change = spec_update.update_stand_date(s, TODAY)
        assert new == s and change is None


def test_stand_does_not_leak_into_later_paragraph():
    s = '<p>Stand: Entwurf</p><p>Anderes <time datetime="2026-01-01"/></p>'
    out, change = spec_update.update_stand_date(s, TODAY)
    assert out == s and change is None


# ---------------------------------------------------------------- section rewrite

BODY = ('<h2>3.1 Batch Processing</h2><p>CM-FR-03: Der Batch läuft täglich.</p><ul><li>CM-D-11 Retry</li></ul>'
        '<h2>3.2 Makro</h2><ac:structured-macro ac:name="info"><ac:rich-text-body><p>CM-FR-09 Text</p>'
        '</ac:rich-text-body></ac:structured-macro>'
        '<h2>3.3 Rest</h2><p>unverändert</p>')


def change(part, **kw):
    return DeltaChange(kind=kw.pop("kind", "changed"), affected_part=part, ids=kw.pop("ids", ["CM-FR-03"]),
                       change=kw.pop("change", "Batch läuft stündlich"), **kw)


def test_rewrite_accepted_replaces_only_that_section_body():
    seen = {}

    def rewriter(body, group):
        seen["body"], seen["group"] = body, group
        return body.replace("täglich", "stündlich")

    new, done, manual = spec_update.rewrite_sections(BODY, [change("3.1 Batch Processing")], rewriter)
    assert "<p>CM-FR-03: Der Batch läuft stündlich.</p>" in new
    assert new.replace("stündlich", "täglich") == BODY
    assert "Batch läuft täglich" in seen["body"] and "<h2>" not in seen["body"]
    assert len(done) == 1 and manual == []


def test_heading_match_by_number_and_partial_title():
    for part in ("3.1", "Abschnitt 3.1", "Batch Processing", "batch processing"):
        new, done, manual = spec_update.rewrite_sections(
            BODY, [change(part)], lambda b, g: b.replace("täglich", "stündlich"))
        assert len(done) == 1, part


@pytest.mark.parametrize("bad", [
    "<p>CM-FR-03: Der Batch läuft stündlich.</p><ul><li>Retry</li></ul>",                 # CM-D-11 lost
    "<p>CM-FR-03: stündlich</p><p>CM-D-11 <b>Retry</p>",                                   # unbalanced
    '<p>CM-FR-03 CM-D-11 <ac:link><ri:page ri:content-title="X"/></ac:link></p>',          # ac: introduced
    "<p>CM-FR-03 CM-D-11 &bad & text</p>",                                                 # bare ampersand
    "<h3>Neu</h3><p>CM-FR-03 CM-D-11</p>",                                                 # heading
    "",                                                                                    # empty
])
def test_rewrite_rejected_by_validation_becomes_manual(bad):
    new, done, manual = spec_update.rewrite_sections(BODY, [change("3.1")], lambda b, g: bad)
    assert new == BODY and done == []
    assert len(manual) == 1 and "3.1 Batch Processing" in manual[0] and "rejected" in manual[0]


def test_rewrite_llm_error_becomes_manual():
    def boom(b, g):
        raise AnalysisError("all models failed")
    new, done, manual = spec_update.rewrite_sections(BODY, [change("3.1")], boom)
    assert new == BODY and "all models failed" in manual[0]


def test_removed_id_may_disappear():
    c = change("3.1", kind="removed", ids=["CM-D-11"], change="Retry entfällt")
    new, done, manual = spec_update.rewrite_sections(
        BODY, [c], lambda b, g: b.replace("<ul><li>CM-D-11 Retry</li></ul>", ""))
    assert "CM-D-11" not in new and len(done) == 1


def test_section_with_macro_and_unmatched_heading_are_manual_without_llm_call():
    rewriter = MagicMock()
    new, done, manual = spec_update.rewrite_sections(
        BODY, [change("3.2 Makro", ids=["CM-FR-09"]), change("9.9 Gibt es nicht")], rewriter)
    rewriter.assert_not_called()
    assert new == BODY and done == []
    assert any("3.2 Makro" in m and "macros" in m for m in manual)
    assert any("9.9" in m and "no heading matches" in m for m in manual)


def test_two_changes_same_section_are_one_rewrite():
    calls = []

    def rewriter(body, group):
        calls.append(len(group))
        return body.replace("täglich", "stündlich")

    spec_update.rewrite_sections(BODY, [change("3.1"), change("3.1 Batch Processing", ids=["CM-D-11"])], rewriter)
    assert calls == [2]


def test_make_llm_rewriter_retries_when_validation_fails(mocker):
    llm = LLMConfig(provider="openai", api_key="k", models=["m1"])
    replies = iter([
        '{"storage": "<p>kaputt</p>"}',
        '{"storage": "<p>CM-FR-03: stündlich</p><ul><li>CM-D-11 Retry</li></ul>"}',
    ])
    mocker.patch.object(spec_update.analysis, "_call", side_effect=lambda *a, **k: next(replies))
    rewrite = spec_update.make_llm_rewriter(llm, "de")
    body = BODY.split("<h2>3.2")[0].split("</h2>", 1)[1]
    out = rewrite(body, [change("3.1")])
    assert "stündlich" in out


# ---------------------------------------------------------------- run_update_spec

def make_config(tmp_path):
    return SimpleNamespace(
        spec_monitor=SpecMonitorConfig(parent_page_id="1", selector_type="label", selector_value="spec",
                                       retry=RetryConfig(1, 0)),
        atlassian=SimpleNamespace(confluence_api_base="https://x.atlassian.net", url="https://x.atlassian.net"),
        llm=LLMConfig(provider="openai", api_key="k", models=["m"]),
        language="de", data_dir=tmp_path, gitlab=None,
    )


def make_analysis(storage, results, deltas=()):
    spec = SpecInfo(page_id="42", title="Spec Titel", url="https://x.atlassian.net/wiki/spaces/S/pages/42",
                    status="Refined", body_storage=storage)
    return SpecAnalysis(spec=spec, sources=list(results), delta_changes=list(deltas))


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, ""
        self.ok = status < 400

    def json(self):
        return self._data


DETAILS = ('<ac:structured-macro ac:name="details" ac:schema-version="1" ac:macro-id="m1">'
           '<ac:parameter ac:name="id">DETAILSPEC</ac:parameter><ac:rich-text-body>'
           '<table><tbody><tr><th>Status</th><td>Refined</td></tr></tbody></table>'
           '</ac:rich-text-body></ac:structured-macro>')
NEW_URL = "https://x.atlassian.net/wiki/spaces/S/pages/99"


@pytest.fixture
def env(mocker, tmp_path):
    mocker.patch.object(spec_update, "_today", return_value=TODAY)
    mocker.patch.object(spec_update, "get_auth_header", return_value="Basic abc")
    storage = DETAILS + page(row(link("Fach A"), "v86, 16. Okt. 2026", "Regeln"))
    result = confluence("Fach A", "86", "90", version_text="v86, 16. Okt. 2026")
    ana = make_analysis(storage, [result])
    ana.spec.ancestor_ids = ["1", "7", "8"]
    run = mocker.patch.object(spec_update.runner, "run_spec_monitor", return_value=RunReport(
        generated_at=datetime(2026, 11, 10), analysed=[ana]))
    publish = mocker.patch.object(spec_update, "publish_page", return_value=NEW_URL)
    http = mocker.patch("requests.request", side_effect=AssertionError("no direct HTTP expected"))
    return SimpleNamespace(cfg=make_config(tmp_path), storage=storage, ana=ana, run=run, publish=publish,
                           http=http, tmp=tmp_path)


def args(dry_run=False):
    return SimpleNamespace(update_spec="Spec Titel", dry_run=dry_run)


def published_storage(env):
    return env.publish.call_args.args[5]


def test_dry_run_writes_preview_files_and_creates_no_page(env, capsys):
    spec_update.run_update_spec(args(True), env.cfg)
    env.run.assert_called_once()
    assert env.run.call_args.kwargs["only"] == "Spec Titel"
    env.publish.assert_not_called()
    env.http.assert_not_called()
    out_dir = env.tmp / "spec_cache"
    new_storage = (out_dir / "spec-update-42.storage.html").read_text(encoding="utf-8")
    assert "v90 (09.11.2026)" in new_storage
    assert 'datetime="2026-11-10"' in new_storage
    diff = (out_dir / "spec-update-42.diff.txt").read_text(encoding="utf-8")
    assert "- Fach A[->Fach A] | v86, 16. Okt. 2026 | Regeln" in diff
    assert "+ Fach A[->Fach A] | v90 (09.11.2026) | Regeln" in diff
    assert "Stand: 2026-10-27 -> 2026-11-10" in diff
    out = capsys.readouterr().out
    assert "Preview only (--dry-run): no page created" in out and "Fach A" in out


def test_nothing_to_update(env, capsys):
    env.ana.sources[0] = confluence("Fach A", "86", "86", cls=Classification.UNCHANGED)
    spec_update.run_update_spec(args(False), env.cfg)
    assert "nothing to update" in capsys.readouterr().out
    env.publish.assert_not_called()
    assert not (env.tmp / "spec_cache").exists()


def test_unchanged_source_without_stated_version_exits_as_nothing_to_update(env, capsys):
    r = confluence("Fach A", None, "5", cls=Classification.UNCHANGED)
    r.old = vp("5", inferred=True)
    env.ana.sources[0] = r
    spec_update.run_update_spec(args(False), env.cfg)
    assert "nothing to update" in capsys.readouterr().out
    env.publish.assert_not_called()


def test_nothing_changed_automatically_with_manual_items_creates_no_page_and_exits_1(env, capsys):
    env.ana.spec.body_storage = "<p>no table, no stand</p>"
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(args(False), env.cfg)
    assert exc.value.code == 1
    env.publish.assert_not_called()
    cap = capsys.readouterr()
    assert "no page created" in cap.out
    assert "Stand date not found" in cap.out
    assert "manual follow-up needed" in cap.err
    assert (env.tmp / "spec_cache" / "spec-update-42.diff.txt").exists()


def test_summary_lists_manual_items(env, capsys):
    env.ana.sources.append(SourceResult(
        ref=SourceRef(name="Kaputt", kind=SourceKind.CONFLUENCE, title="Kaputt"),
        classification=Classification.ERROR, errors=["HTTP 404"]))
    spec_update.run_update_spec(args(True), env.cfg)
    out = capsys.readouterr().out
    assert "manual follow-up" in out and "Kaputt (error)" in out
    diff = (env.tmp / "spec_cache" / "spec-update-42.diff.txt").read_text(encoding="utf-8")
    assert "Kaputt (error)" in diff


def test_creates_sibling_page_with_dated_title_and_message(env, capsys):
    spec_update.run_update_spec(args(False), env.cfg)
    env.publish.assert_called_once()
    a = env.publish.call_args.args
    assert a[0] is env.cfg.atlassian and a[1] == "Basic abc" and a[2] is env.cfg.spec_monitor.retry
    assert a[3] == "8"
    assert a[4] == "Spec Titel (aktualisiert 2026-11-10)"
    assert a[6] == "Spec source monitor: sources updated to 2026-11-10"
    assert "v90 (09.11.2026)" in a[5]
    assert NEW_URL in capsys.readouterr().out
    env.http.assert_not_called()


def test_original_page_is_never_written(env):
    spec_update.run_update_spec(args(False), env.cfg)
    env.http.assert_not_called()
    assert not hasattr(spec_update, "_put_page")
    assert not hasattr(spec_update, "request_with_retry")


def test_duplicate_title_retries_once_with_time(env, mocker):
    mocker.patch.object(spec_update, "_now", return_value=datetime(2026, 11, 10, 14, 5))
    env.publish.side_effect = [
        RuntimeError("Cannot create page 'x' under 8 (HTTP 400: A page already exists with the title x in the space S)"),
        NEW_URL,
    ]
    spec_update.run_update_spec(args(False), env.cfg)
    titles = [c.args[4] for c in env.publish.call_args_list]
    assert titles == ["Spec Titel (aktualisiert 2026-11-10)", "Spec Titel (aktualisiert 2026-11-10 14:05)"]


def test_duplicate_title_twice_is_a_clear_error(env, mocker, capsys):
    mocker.patch.object(spec_update, "_now", return_value=datetime(2026, 11, 10, 14, 5))
    env.publish.side_effect = RuntimeError(
        "Cannot create page 'x' under 8 (HTTP 400: A page already exists with the title x)")
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(args(False), env.cfg)
    assert exc.value.code == 1
    assert env.publish.call_count == 2
    assert "already exists" in capsys.readouterr().err


def test_other_publish_failure_is_not_retried(env, capsys):
    env.publish.side_effect = RuntimeError("Cannot create page 'x' under 8 (HTTP 403: forbidden)")
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(args(False), env.cfg)
    assert exc.value.code == 1
    assert env.publish.call_count == 1
    assert "HTTP 403" in capsys.readouterr().err


def test_no_ancestors_is_a_clear_error(env, capsys):
    env.ana.spec.ancestor_ids = []
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(args(False), env.cfg)
    assert exc.value.code == 1
    env.publish.assert_not_called()
    assert "parent" in capsys.readouterr().err


def test_no_ancestors_is_fine_for_dry_run(env):
    env.ana.spec.ancestor_ids = []
    spec_update.run_update_spec(args(True), env.cfg)
    env.publish.assert_not_called()


def test_new_page_details_macro_id_renamed(env):
    spec_update.run_update_spec(args(False), env.cfg)
    body = published_storage(env)
    assert '<ac:parameter ac:name="id">DETAILSPEC-UPDATE</ac:parameter>' in body
    assert '">DETAILSPEC<' not in body
    preview = (env.tmp / "spec_cache" / "spec-update-42.storage.html").read_text(encoding="utf-8")
    assert ">DETAILSPEC<" in preview and "DETAILSPEC-UPDATE" not in preview and "ac:name=\"info\"" not in preview


def test_rename_details_ids_only_touches_details_macros():
    other = ('<ac:structured-macro ac:name="expand"><ac:parameter ac:name="id">KEEP</ac:parameter>'
             '<ac:rich-text-body><p>x</p></ac:rich-text-body></ac:structured-macro>')
    second = DETAILS.replace("DETAILSPEC", "OTHER")
    nameless = ('<ac:structured-macro ac:name="details"><ac:rich-text-body><p>y</p></ac:rich-text-body>'
                '</ac:structured-macro>')
    s = DETAILS + other + second + nameless + "<p>DETAILSPEC in text</p>"
    new, ids = spec_update.rename_details_macro_ids(s)
    assert ids == ["DETAILSPEC", "OTHER"]
    assert new.count("DETAILSPEC-UPDATE") == 1 and "OTHER-UPDATE" in new
    assert '<ac:parameter ac:name="id">KEEP</ac:parameter>' in new
    assert "<p>DETAILSPEC in text</p>" in new
    assert nameless in new
    assert new.replace("DETAILSPEC-UPDATE", "DETAILSPEC").replace("OTHER-UPDATE", "OTHER") == s


def test_rename_details_ids_without_details_macro_is_noop():
    new, ids = spec_update.rename_details_macro_ids("<p>x</p>")
    assert new == "<p>x</p>" and ids == []


def test_banner_inserted_at_top_with_changes_manual_and_original_link(env):
    env.ana.sources.append(SourceResult(
        ref=SourceRef(name="Kaputt <b>", kind=SourceKind.CONFLUENCE, title="Kaputt"),
        classification=Classification.ERROR, errors=["HTTP 404 & more"]))
    spec_update.run_update_spec(args(False), env.cfg)
    body = published_storage(env)
    assert body.startswith('<ac:structured-macro ac:name="info"')
    banner, rest = body.split("</ac:structured-macro>", 1)
    assert "2026-11-10" in banner
    assert 'href="https://x.atlassian.net/wiki/spaces/S/pages/42"' in banner
    assert "Fach A" in banner and "v90 (09.11.2026)" in banner
    assert "Stand: 2026-10-27 -&gt; 2026-11-10" in banner
    assert "Kaputt &lt;b&gt;" in banner and "HTTP 404 &amp; more" in banner
    assert "<b>" not in banner
    assert "DETAILSPEC" in banner and "DETAILSPEC-UPDATE" in banner
    assert rest.startswith('<ac:structured-macro ac:name="details"')


def test_build_banner_escapes_everything():
    out = spec_update.build_banner('Titel "<x>"', 'https://h/p?a=1&b="2"', date(2026, 11, 10),
                                   ["a <i>"], ["m & n"], ["ID<1>"])
    assert "<i>" not in out and "a &lt;i&gt;" in out and "m &amp; n" in out
    assert "&amp;b=&quot;2&quot;" in out
    assert "Titel &quot;&lt;x&gt;&quot;" in out
    assert "ID&lt;1&gt;" in out


def test_build_banner_lists_none_when_empty():
    out = spec_update.build_banner("T", "https://h/p", date(2026, 11, 10), [], [], [])
    assert "<ul>" not in out


def test_unknown_spec_exits_1(env, capsys):
    env.run.return_value = RunReport(generated_at=datetime(2026, 11, 10), analysed=[])
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(args(False), env.cfg)
    assert exc.value.code == 1
    assert "No spec matching" in capsys.readouterr().err


def test_runner_fatal_exits_1(env, capsys):
    env.run.side_effect = spec_update.runner.SpecMonitorFatal("discovery failed")
    with pytest.raises(SystemExit):
        spec_update.run_update_spec(args(False), env.cfg)
    assert "discovery failed" in capsys.readouterr().err


def test_text_sections_rewritten_through_llm_in_preview(env, mocker):
    body = "<h2>3.1 Batch</h2><p>CM-FR-03: täglich</p>"
    env.ana.spec.body_storage = env.storage + body
    env.ana.delta_changes = [change("3.1 Batch")]
    mocker.patch.object(spec_update, "make_llm_rewriter",
                        return_value=lambda b, g: b.replace("täglich", "stündlich"))
    spec_update.run_update_spec(args(True), env.cfg)
    new_storage = (env.tmp / "spec_cache" / "spec-update-42.storage.html").read_text(encoding="utf-8")
    assert "CM-FR-03: stündlich" in new_storage
    diff = (env.tmp / "spec_cache" / "spec-update-42.diff.txt").read_text(encoding="utf-8")
    assert "+ CM-FR-03: stündlich" in diff
    env.http.assert_not_called()


def test_analysis_warnings_are_printed(env, capsys):
    env.ana.errors.append(ReportError(scope="Spec Titel", message="synthesis failed"))
    env.ana.analysis_incomplete = True
    spec_update.run_update_spec(args(False), env.cfg)
    out = capsys.readouterr().out
    assert "synthesis failed" in out and "analysis incomplete" in out


# ---------------------------------------------------------------- --update-spec without value (all specs)

def all_args(dry_run=False):
    return SimpleNamespace(update_spec="*", dry_run=dry_run)


def extra_analysis(env, page_id, title, changed=True, ancestors=("1", "7", "8")):
    result = (confluence("Fach A", "86", "90", version_text="v86, 16. Okt. 2026") if changed
              else confluence("Fach A", "86", "86", cls=Classification.UNCHANGED))
    spec = SpecInfo(page_id=page_id, title=title, url=f"https://x.atlassian.net/wiki/spaces/S/pages/{page_id}",
                    status="Refined", body_storage=env.storage, ancestor_ids=list(ancestors))
    return SpecAnalysis(spec=spec, sources=[result])


def set_all(env, *analyses):
    env.run.return_value = RunReport(generated_at=datetime(2026, 11, 10), analysed=list(analyses))


def test_specific_spec_creates_exactly_one_page(env):
    set_all(env, env.ana, extra_analysis(env, "43", "Andere"))
    spec_update.run_update_spec(args(False), env.cfg)
    assert env.publish.call_count == 1
    assert env.run.call_args.kwargs["only"] == "Spec Titel"


def test_update_all_creates_one_page_per_spec_with_changes(env, capsys):
    set_all(env, env.ana, extra_analysis(env, "43", "Zweite"), extra_analysis(env, "44", "Dritte", changed=False))
    env.publish.side_effect = ["https://x/p/1", "https://x/p/2"]
    spec_update.run_update_spec(all_args(), env.cfg)
    assert env.run.call_args.kwargs["only"] is None
    titles = [c.args[4] for c in env.publish.call_args_list]
    assert titles == ["Spec Titel (aktualisiert 2026-11-10)", "Zweite (aktualisiert 2026-11-10)"]
    out = capsys.readouterr().out
    assert "created https://x/p/1" in out and "created https://x/p/2" in out
    assert "Dritte" in out and "nothing to update" in out
    assert (env.tmp / "spec_cache" / "spec-update-43.storage.html").exists()
    assert not (env.tmp / "spec_cache" / "spec-update-44.storage.html").exists()


def test_update_all_isolates_failures_and_exits_1(env, capsys):
    set_all(env, env.ana, extra_analysis(env, "43", "Zweite"), extra_analysis(env, "44", "Dritte"))
    env.publish.side_effect = ["https://x/p/1", RuntimeError("Cannot create page (HTTP 403: forbidden)"),
                               "https://x/p/3"]
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(all_args(), env.cfg)
    assert exc.value.code == 1
    assert env.publish.call_count == 3
    cap = capsys.readouterr()
    assert "created https://x/p/1" in cap.out and "created https://x/p/3" in cap.out
    summary = cap.out.split("Spec update summary:")[1]
    assert "Spec Titel" in summary and "Zweite" in summary and "Dritte" in summary
    assert "failed: Cannot create page (HTTP 403: forbidden)" in summary
    assert "HTTP 403" in cap.err


def test_update_all_no_ancestors_fails_only_that_spec(env, capsys):
    set_all(env, extra_analysis(env, "43", "Waise", ancestors=()), env.ana)
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(all_args(), env.cfg)
    assert exc.value.code == 1
    assert env.publish.call_count == 1
    assert "failed: Cannot create the updated page" in capsys.readouterr().out


def test_update_all_failed_analysis_is_reported_as_failed(env, capsys):
    broken = SpecAnalysis(spec=SpecInfo(page_id="45", title="Kaputt", url="u", status="Refined"),
                          analysis_incomplete=True, errors=[ReportError(scope="Kaputt", message="boom")])
    set_all(env, broken, env.ana)
    with pytest.raises(SystemExit):
        spec_update.run_update_spec(all_args(), env.cfg)
    assert env.publish.call_count == 1
    assert "failed: boom" in capsys.readouterr().out


def test_update_all_dry_run_writes_all_previews_and_creates_no_page(env, capsys):
    set_all(env, env.ana, extra_analysis(env, "43", "Zweite"))
    spec_update.run_update_spec(all_args(True), env.cfg)
    env.publish.assert_not_called()
    for pid in ("42", "43"):
        assert (env.tmp / "spec_cache" / f"spec-update-{pid}.storage.html").exists()
        assert (env.tmp / "spec_cache" / f"spec-update-{pid}.diff.txt").exists()
    assert capsys.readouterr().out.count("preview only") == 2


def test_update_all_without_analysed_specs_is_not_an_error(env, capsys):
    set_all(env)
    spec_update.run_update_spec(all_args(), env.cfg)
    env.publish.assert_not_called()


def test_rewriter_is_built_from_spec_llm_timeout(env, mocker):
    env.cfg.spec_monitor.llm_timeout = 77
    env.ana.spec.body_storage = env.storage + "<h2>3.1 Batch</h2><p>CM-FR-03: täglich</p>"
    env.ana.delta_changes = [change("3.1 Batch")]
    make = mocker.patch.object(spec_update, "make_llm_rewriter", return_value=lambda b, g: b)
    spec_update.run_update_spec(args(True), env.cfg)
    assert make.call_args.args[0].llm_timeout == 77
    assert env.cfg.llm.llm_timeout == 30


def test_make_llm_rewriter_uses_llm_timeout_without_multiplier(mocker):
    llm = LLMConfig(provider="openai", api_key="k", models=["m1"], llm_timeout=55)
    ask = mocker.patch.object(spec_update.analysis, "_ask_json",
                              return_value=("<p>CM-FR-03: stündlich</p><ul><li>CM-D-11 Retry</li></ul>", None))
    body = BODY.split("<h2>3.2")[0].split("</h2>", 1)[1]
    spec_update.make_llm_rewriter(llm, "de")(body, [change("3.1")])
    assert ask.call_args.args[2] == 55


def test_stand_date_not_found_is_listed_as_manual(env, capsys):
    env.ana.spec.body_storage = page(row(link("Fach A"), "v86, 16. Okt. 2026"), stand="<p>kein Datum</p>")
    spec_update.run_update_spec(args(True), env.cfg)
    diff = (env.tmp / "spec_cache" / "spec-update-42.diff.txt").read_text(encoding="utf-8")
    assert "Stand date not found" in diff


def test_errored_source_is_not_reported_as_up_to_date(env, capsys):
    env.ana.sources[0] = SourceResult(
        ref=SourceRef(name="Fach A", kind=SourceKind.CONFLUENCE, title="Fach A", baseline_version="86"),
        classification=Classification.ERROR, errors=["HTTP 503"])
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(args(False), env.cfg)
    assert exc.value.code == 1
    cap = capsys.readouterr()
    assert "nothing to update" not in cap.out
    assert "cannot confirm" in cap.err and "HTTP 503" in cap.err
    env.publish.assert_not_called()


def test_not_monitored_source_is_manual_and_exits_1(env, capsys):
    env.ana.sources[0] = SourceResult(
        ref=SourceRef(name="Fach A", kind=SourceKind.URL, url="https://x", baseline_version="86"),
        classification=Classification.NOT_MONITORED, warnings=["not monitorable"])
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(args(False), env.cfg)
    assert exc.value.code == 1
    assert "not monitorable" in capsys.readouterr().err


def test_excluded_source_does_not_block_nothing_to_update(env, capsys):
    env.ana.sources[0] = confluence("Fach A", "86", "86", cls=Classification.UNCHANGED)
    env.ana.sources.append(SourceResult(
        ref=SourceRef(name="Ignored", kind=SourceKind.CONFLUENCE, title="Ignored", excluded_reason="marker"),
        classification=Classification.EXCLUDED))
    spec_update.run_update_spec(args(False), env.cfg)
    assert "nothing to update" in capsys.readouterr().out


def test_spec_without_sources_fails_instead_of_up_to_date(env, capsys):
    env.ana.sources.clear()
    with pytest.raises(SystemExit):
        spec_update.run_update_spec(args(False), env.cfg)
    assert "no sources found" in capsys.readouterr().err


def test_update_all_does_not_report_unchecked_specs_as_up_to_date(env, capsys):
    bad = extra_analysis(env, "43", "Fehlerhaft")
    bad.sources[0] = SourceResult(
        ref=SourceRef(name="Fach A", kind=SourceKind.CONFLUENCE, title="Fach A", baseline_version="86"),
        classification=Classification.ERROR, errors=["HTTP 503"])
    empty = extra_analysis(env, "44", "Ohne Quellen")
    empty.sources.clear()
    set_all(env, env.ana, bad, empty)
    with pytest.raises(SystemExit) as exc:
        spec_update.run_update_spec(all_args(), env.cfg)
    assert exc.value.code == 1
    summary = capsys.readouterr().out.split("Spec update summary:")[1]
    assert "failed:" in summary and "HTTP 503" in summary and "no sources found" in summary
    assert "nothing to update" not in summary
    assert env.publish.call_count == 1
