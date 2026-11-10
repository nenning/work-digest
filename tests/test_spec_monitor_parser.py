from datetime import date
from html import escape

from digest.config import GitLabConfig, GitLabDocumentAlias, GitLabRepoConfig
from digest.spec_monitor.models import SourceKind
from digest.spec_monitor.parser import (
    find_sources_sentence,
    parse_page_properties,
    parse_sources,
)

MARKERS = ["Notizen", "Arbeitsnotizen", "Vorgängerfassung"]


def link(title, space=None):
    sk = f' ri:space-key="{space}"' if space else ""
    return f'<ac:link><ri:page ri:content-title="{escape(title)}"{sk} /></ac:link>'


def links(*titles):
    return ", ".join(link(t) for t in titles)


def row(src, ver="", used="", tag="td"):
    return f"<tr><{tag}>{src}</{tag}><{tag}>{ver}</{tag}><{tag}>{used}</{tag}><{tag}></{tag}></tr>"


def sources_table(*rows, header=("Quelle", "Gelesene Version", "Verwendet für")):
    h = "<tr>" + "".join(f"<th>{c}</th>" for c in header) + "<th></th></tr>"
    return f"<table><tbody>{h}{''.join(rows)}</tbody></table>"


def page(*rows, heading="<h2>1.5 Quellen</h2>", extra="", header=("Quelle", "Gelesene Version", "Verwendet für")):
    return f"<h1>Spec</h1>{extra}{heading}{sources_table(*rows, header=header)}"


def parse(html, gitlab=None, stand=None):
    return parse_sources(html, stand_date=stand, excluded_markers=MARKERS, gitlab=gitlab)


def test_single_link_version_and_date():
    r = parse(page(row(link("Fach A"), "v86, 16. Okt. 2026", "Regeln")))
    assert r.found
    (ref,) = r.refs
    assert ref.kind == SourceKind.CONFLUENCE
    assert ref.title == "Fach A"
    assert ref.baseline_version == "86"
    assert ref.baseline_date == date(2026, 10, 16)
    assert ref.used_for == "Regeln"
    assert ref.version_text == "v86, 16. Okt. 2026"


def test_version_with_parenthesised_dotted_date():
    (ref,) = parse(page(row(link("Fach A"), "v258 (09.11.2026)"))).refs
    assert ref.baseline_version == "258"
    assert ref.baseline_date == date(2026, 11, 9)


def test_version_with_remark():
    (ref,) = parse(page(row(link("Fach A"), "v4 (Status Abgenommen)"))).refs
    assert ref.baseline_version == "4"
    assert ref.baseline_date is None


def test_space_key_taken_from_link():
    (ref,) = parse(page(row(link("Fach A", "RMEG"), "v1"))).refs
    assert ref.space_key == "RMEG"


def test_three_links_paired_by_position():
    r = parse(page(row(links("A", "B", "C"), "v187, v352, v91")))
    assert [(x.title, x.baseline_version) for x in r.refs] == [("A", "187"), ("B", "352"), ("C", "91")]
    assert all(x.parse_issue is None for x in r.refs)


def test_two_links_two_versions():
    r = parse(page(row(links("Batch Processing", "Utility"), "v46, v68")))
    assert [(x.title, x.baseline_version) for x in r.refs] == [("Batch Processing", "46"), ("Utility", "68")]


def test_single_date_applies_to_all_links():
    r = parse(page(row(links("A", "B"), "gelesen am 2026-11-05")))
    assert [x.baseline_date for x in r.refs] == [date(2026, 11, 5)] * 2
    assert all(x.baseline_version is None for x in r.refs)
    r2 = parse(page(row(links("A", "B"), "Stand 02.11.2026")))
    assert all(x.baseline_date == date(2026, 11, 2) for x in r2.refs)


def test_more_links_than_versions_is_flagged_not_paired():
    r = parse(page(row(links("A", "B", "C"), "v1, v2")))
    assert len(r.refs) == 3
    for ref in r.refs:
        assert "could not pair" in ref.parse_issue
        assert ref.baseline_version is None
    assert any("could not pair" in i for i in r.issues)


def test_single_version_for_two_links_is_flagged():
    r = parse(page(row(links("A", "B"), "v46")))
    assert all("could not pair" in x.parse_issue for x in r.refs)
    assert all(x.baseline_version is None for x in r.refs)


def test_more_versions_than_links_is_flagged():
    (ref,) = parse(page(row(link("A"), "v1, v2"))).refs
    assert "could not pair" in ref.parse_issue


def test_no_baseline_variants():
    for cell in ["nicht erfasst", "-", "–", "", "Entwurf (PROJ-1856)"]:
        r = parse(page(row(link("A"), cell)))
        (ref,) = r.refs
        assert ref.baseline_version is None, cell
        assert ref.baseline_date is None, cell
        assert ref.parse_issue is None, cell


def test_excluded_rows():
    r = parse(
        page(
            row("Notizen Stephan", "v1", ""),
            row(link("Konzept"), "v2", "Arbeitsnotizen zum Thema"),
            row(link("Alte Seite") + " (Vorgängerfassung dieser Seite)", "v3"),
            row(link("Normal"), "v4", "Regeln"),
        )
    )
    reasons = [x.excluded_reason for x in r.refs]
    assert all(reasons[:3])
    assert reasons[3] is None
    assert "notizen" in reasons[0].lower()


def test_excluded_marker_is_case_insensitive():
    (ref,) = parse(page(row("notizen von heute", "-"))).refs
    assert ref.excluded_reason


def test_ech_and_i14y_in_one_cell():
    r = parse(
        page(
            row(
                "eCH-0108 Abschnitt 3.1.8 und die i14y-Codeliste der Rechtsformen v1.2.0",
                "",
                "Rechtsformen",
            )
        )
    )
    kinds = [x.kind for x in r.refs]
    assert kinds == [SourceKind.ECH, SourceKind.I14Y]
    ech, i14y = r.refs
    assert ech.section == "Abschnitt 3.1.8"
    assert ech.name == "eCH-0108"
    assert ech.baseline_version is None
    assert i14y.baseline_version == "1.2.0"
    assert i14y.used_for == "Rechtsformen"


def test_external_url_row():
    html = page(
        row('<a href="https://example.org/std.pdf">Standard X [SRC-1]</a>', "v2.0, Nov. 2025", "Basis"),
        row('<a href="https://example.org/b">Other [SRC-2]</a>', "–"),
        row('<a href="https://example.org/c">Third [SRC-3]</a>', "1.2"),
    )
    a, b, c = parse(html).refs
    assert a.kind == SourceKind.URL
    assert a.url == "https://example.org/std.pdf"
    assert "SRC-1" in a.name
    assert a.baseline_version == "2.0"
    assert b.baseline_version is None
    assert c.baseline_version == "1.2"


def test_plain_text_without_link_or_pattern_is_external():
    (ref,) = parse(page(row("Swisspost API", "v1.4.0"))).refs
    assert ref.kind == SourceKind.EXTERNAL
    assert ref.baseline_version == "1.4.0"


def test_header_variant_and_trailing_empty_cell():
    r = parse(
        page(row(link("A"), "v5", "Zweck"), header=("Quelle", "Version", "Verwendet für"))
    )
    assert r.found
    (ref,) = r.refs
    assert ref.baseline_version == "5" and ref.used_for == "Zweck"


def test_unnumbered_heading_and_other_headings_still_found():
    for heading in ["<h2>Quellen</h2>", "<h3>Grundlagen und Quellen</h3>", ""]:
        r = parse(page(row(link("A"), "v5"), heading=heading))
        assert r.found and len(r.refs) == 1


def test_other_table_is_ignored():
    other = "<table><tbody><tr><th>Name</th><th>Wert</th></tr><tr><td>x</td><td>y</td></tr></tbody></table>"
    r = parse(other + page(row(link("A"), "v5")))
    assert [x.title for x in r.refs] == ["A"]


def test_table_after_quellen_heading_preferred():
    decoy = sources_table(row(link("Decoy"), "v1"), header=("Quelle", "Version", "Verwendet für"))
    html = f"<h2>Anhang</h2>{decoy}" + page(row(link("Real"), "v2"))
    assert [x.title for x in parse(html).refs] == ["Real"]


def test_no_table_with_sentence():
    html = (
        "<h1>Spec</h1><ac:structured-macro ac:name=\"panel\"><ac:rich-text-body>"
        "<p>Stand: Entwurf zur Klärung, <time datetime=\"2026-10-27\"/></p>"
        "<p>Quellen: Fachspezifikation X in Seitenversion 35 vom 28.10.2026</p>"
        "</ac:rich-text-body></ac:structured-macro>"
    )
    r = parse(html)
    assert not r.found and r.refs == []
    assert find_sources_sentence(html) == "Quellen: Fachspezifikation X in Seitenversion 35 vom 28.10.2026"


def test_find_sources_sentence_ignores_gap_notes_and_missing():
    html = "<p>Lücken in den Quellen: Seite Y nicht gelesen</p><p>Sonst nichts</p>"
    assert find_sources_sentence(html) is None
    assert find_sources_sentence(page(row(link("A"), "v1"))) is None


def test_gap_notes():
    html = page(
        row(link("A"), "v1"),
        extra=(
            "<p>Lücken in den Quellen: Seite Q fehlt.</p>"
            "<p>Nicht gelesen: Anhang B</p>"
            "<p>Alle Seiten wurden über eine Markdown-Konvertierung gelesen, dabei gingen Makros und Bilder verloren.</p>"
            "<p>Irrelevanter Absatz</p>"
        ),
    )
    r = parse(html)
    assert len(r.gap_notes) == 3
    assert r.gap_notes[0].startswith("Lücken in den Quellen")
    assert r.gap_notes[1].startswith("Nicht gelesen")
    assert "Markdown-Konvertierung" in r.gap_notes[2]


GITLAB = GitLabConfig(
    url="https://gitlab.example.com",
    api_token="t",
    default_project="application",
    repositories=[
        GitLabRepoConfig(name="application", project="group/application", match=["application"],
                         docs_folders=["docs"]),
        GitLabRepoConfig(
            name="Diagramm-Repository",
            project="group/diagrams",
            match=["Diagramm-Repository"],
            docs_folders=["docs"],
        ),
    ],
)

ADR_ROW = "ADR-0028 «API contract conventions» im Repository (docs/adr/0028-api-contract-conventions.md)"


def test_repo_adr_row_with_path_uses_default_repo():
    r = parse(page(row(ADR_ROW, "Accepted, 03.11.2026", "Konventionen")), gitlab=GITLAB)
    (ref,) = r.refs
    assert ref.kind == SourceKind.REPO
    assert ref.repo_name == "application"
    assert ref.repo_path == "docs/adr/0028-api-contract-conventions.md"
    assert ref.baseline_date == date(2026, 11, 3)
    assert ref.name == "ADR-0028 «API contract conventions» im Repository"
    assert "Accepted" in ref.note
    assert ref.parse_issue is None


def test_repo_adr_row_without_path_uses_default_repo():
    r = parse(page(row("ADR-0028 im Repository", "Accepted, 03.11.2026")), gitlab=GITLAB)
    (ref,) = r.refs
    assert ref.kind == SourceKind.REPO
    assert ref.repo_name == "application"
    assert ref.repo_path is None
    assert ref.baseline_date == date(2026, 11, 3)
    assert ref.parse_issue is None
    assert not r.issues


def test_repo_row_named_repository_with_branch_commit_date():
    r = parse(
        page(
            row(
                "API-Konzept, Dokument im Diagramm-Repository (Branch PROJ-1856-x, Commit a206e36, 30.10.2026)",
                "Stand 30.10.2026",
            )
        ),
        gitlab=GITLAB,
    )
    (ref,) = r.refs
    assert ref.kind == SourceKind.REPO
    assert ref.repo_name == "Diagramm-Repository"
    assert ref.repo_branch == "PROJ-1856-x"
    assert ref.repo_commit == "a206e36"
    assert ref.baseline_date == date(2026, 10, 30)
    assert ref.repo_path is None
    assert ref.name == "API-Konzept, Dokument im Diagramm-Repository"
    assert ref.parse_issue is None
    assert not r.issues


def test_repo_row_alias_still_gives_path():
    cfg = GitLabConfig(
        url="https://g",
        api_token="t",
        default_project="application",
        repositories=[GitLabRepoConfig(
            name="application", project="g/a", match=["application"],
            documents=[GitLabDocumentAlias(match="API-Konzept", path="docs/api-konzept.md")])],
    )
    (ref,) = parse(page(row("API-Konzept im Repo", "-")), gitlab=cfg).refs
    assert ref.kind == SourceKind.REPO
    assert ref.repo_path == "docs/api-konzept.md"


def test_repo_row_path_only_selects_default_repo():
    (ref,) = parse(page(row("Konventionen docs/adr/0028-x.md", "-")), gitlab=GITLAB).refs
    assert ref.kind == SourceKind.REPO
    assert ref.repo_name == "application"
    assert ref.repo_path == "docs/adr/0028-x.md"


def test_repo_spec_phrase_selects_default_repo():
    (ref,) = parse(page(row("Repo-Spec Konventionen", "-")), gitlab=GITLAB).refs
    assert ref.kind == SourceKind.REPO
    assert ref.repo_name == "application"


def test_confluence_linked_adr_row_is_not_repo_doc():
    (ref,) = parse(page(row(link("ADR-018: Naming") , "v3")), gitlab=GITLAB).refs
    assert ref.kind == SourceKind.CONFLUENCE


def test_repo_row_without_gitlab_config_is_external_with_note():
    r = parse(
        page(row("ADR-0028 im Repository (docs/adr/0028-x.md)", "Accepted, 03.11.2026")),
    )
    (ref,) = r.refs
    assert ref.kind == SourceKind.EXTERNAL
    assert "docs/adr/0028-x.md" in ref.note or "GitLab" in ref.note
    assert "GitLab" in ref.note


def test_repo_row_matching_no_repository_is_external():
    cfg = GitLabConfig(
        url="https://g",
        api_token="t",
        repositories=[GitLabRepoConfig(name="Other", project="g/o", match=["Other"])],
    )
    (ref,) = parse(page(row("ADR im Repository (docs/adr/1.md)", "-")), gitlab=cfg).refs
    assert ref.kind == SourceKind.EXTERNAL
    assert ref.note


def status_macro(title):
    return (
        '<ac:structured-macro ac:name="status"><ac:parameter ac:name="colour">Green</ac:parameter>'
        f'<ac:parameter ac:name="title">{title}</ac:parameter></ac:structured-macro>'
    )


def props_page(status="Refined", jira="PROJ-1234", stand=None):
    stand_html = (
        f"<ac:structured-macro ac:name=\"panel\"><ac:rich-text-body><p>Stand: {stand}</p>"
        "</ac:rich-text-body></ac:structured-macro>"
        if stand
        else ""
    )
    return (
        '<ac:structured-macro ac:name="details"><ac:parameter ac:name="id">DETAILSPEC</ac:parameter>'
        "<ac:rich-text-body><table><tbody>"
        f"<tr><th>Status</th><td>{status_macro(status) if status else ''}</td></tr>"
        f"<tr><th>Jira</th><td>{jira}</td></tr>"
        "<tr><th>Owner</th><td>Team</td></tr>"
        "</tbody></table></ac:rich-text-body></ac:structured-macro>" + stand_html
    )


def test_page_properties_full():
    p = parse_page_properties(
        props_page("Refined", "<a>PROJ-1234</a>, PROJ-99 und PROJ-1234", 'Entwurf zur Klärung, <time datetime="2026-10-27" />')
    )
    assert p.status == "Refined"
    assert p.jira == ["PROJ-1234", "PROJ-99"]
    assert p.stand_date == date(2026, 10, 27)


def test_page_properties_stand_as_text():
    p = parse_page_properties(props_page(stand="Entwurf, 27.10.2026"))
    assert p.stand_date == date(2026, 10, 27)


def test_page_properties_missing_is_unknown():
    p = parse_page_properties("<p>nothing here</p>")
    assert p.status == "unknown"
    assert p.jira == []
    assert p.stand_date is None


def test_page_properties_empty_status_is_unknown():
    assert parse_page_properties(props_page(status="")).status == "unknown"


def test_page_properties_status_plain_text():
    html = "<table><tr><th>Status</th><td>In Ausarbeitung</td></tr></table>"
    assert parse_page_properties(html).status == "In Ausarbeitung"


def parse_anc(html, titles=(), ids=()):
    return parse_sources(html, stand_date=None, excluded_markers=MARKERS, ancestor_titles=titles, ancestor_ids=ids)


def test_ancestor_links_are_never_sources():
    html = page(
        row(link("Parent Page"), "v1"),
        row('<a href="https://x.example/wiki/spaces/S/pages/555/Up">Up</a>', "v2"),
        row('<a href="https://x.example/wiki/spaces/S/pages/777/Other">Other</a>', "v3"),
        row(link("Fach A"), "v4"),
    )
    r = parse_anc(html, titles=["parent PAGE"], ids=["555"])
    assert [x.title for x in r.refs] == ["Other", "Fach A"]


def test_ancestor_link_among_several_in_one_cell_is_dropped():
    r = parse_anc(page(row(links("Parent", "Fach A"), "v4")), titles=["Parent"])
    assert [x.title for x in r.refs] == ["Fach A"]


SECTION_TAIL = (
    "<h1>2. Übersicht</h1><h2>2.1 Proxy</h2><p><a href=\"https://nope.example/x\">Nope</a></p>"
    "<h2>2.2 Targets</h2><table><tbody>"
    "<tr><th>Target-ID</th><th>Spezifikation</th></tr>"
    "<tr><td><code>t-one</code></td><td>SOAP, <a href=\"https://api.example/spec/1\">Spec One</a> (<code>QueryServiceV1X8</code>)</td></tr>"
    "<tr><td><code>t-two</code></td><td><a href=\"https://api.example/spec/1\">Spec One</a> (<code>MutationServiceV1X8</code>)</td></tr>"
    "<tr><td><code>t-three</code></td><td><a href=\"https://api.example/spec/3\">Spec Three</a></td></tr>"
    "<tr><td><code>t-four</code></td><td>" + link("Fach Seite") + " und " + link("Parent") + "</td></tr>"
    "</tbody></table><h3>2.2.1 Detail</h3><p>Siehe <a href=\"https://api.example/detail\">Detail</a>.</p>"
    "<h2>2.3 Rest</h2><p><a href=\"https://after.example/y\">After</a></p>"
)


def section_page(cell, used="Schnittstellen", ver="nicht erfasst"):
    return page(row(cell, ver, used)) + SECTION_TAIL


def test_section_reference_is_expanded():
    r = parse_anc(section_page("Eins- und Zwei-Spezifikationen (Links in Kapitel 2.2)"), titles=["Parent"])
    by_name = {x.name: x for x in r.refs}
    assert list(by_name) == ["Spec One", "Spec Three", "Fach Seite", "Detail"]
    one = by_name["Spec One"]
    assert one.kind == SourceKind.URL
    assert one.url == "https://api.example/spec/1"
    assert one.used_for == "Schnittstellen (t-one, t-two) [Kapitel 2.2]"
    assert one.baseline_version == "1.8"
    assert one.version_text == "nicht erfasst"
    assert one.note == ""
    assert by_name["Spec Three"].url == "https://api.example/spec/3"
    assert by_name["Spec Three"].baseline_version is None
    assert by_name["Fach Seite"].kind == SourceKind.CONFLUENCE
    assert by_name["Fach Seite"].used_for == "Schnittstellen (t-four) [Kapitel 2.2]"
    assert by_name["Detail"].used_for == "Schnittstellen [Kapitel 2.2]"
    assert not r.issues


def test_section_reference_variants():
    for cell in ["X (Links in Abschnitt 2.2)", "Quellen siehe Kapitel 2.2", "Y, Links in Kapitel 2.2"]:
        r = parse_anc(section_page(cell))
        assert [x.name for x in r.refs][:2] == ["Spec One", "Spec Three"], cell


def test_section_number_does_not_match_longer_number():
    html = page(row("X (Links in Kapitel 2.2)")) + "<h2>2.20 Anderes</h2><p><a href=\"https://bad.example/\">Bad</a></p>"
    r = parse_anc(html)
    assert [x.kind for x in r.refs] == [SourceKind.EXTERNAL]


def test_section_reference_not_found_keeps_row_and_flags():
    r = parse_anc(page(row("Spezifikationen (Links in Kapitel 9.9)", "nicht erfasst", "Zweck")))
    (ref,) = r.refs
    assert ref.kind == SourceKind.EXTERNAL
    assert ref.name == "Spezifikationen (Links in Kapitel 9.9)"
    assert "9.9" in ref.parse_issue and "not found" in ref.parse_issue
    assert any("9.9" in i for i in r.issues)


def test_row_with_own_link_is_not_section_expanded():
    r = parse_anc(section_page(link("Fach A") + " (siehe Kapitel 2.2)"))
    assert [x.title for x in r.refs] == ["Fach A"]


def test_ech_url_becomes_ech_ref():
    html = page(
        row('<a href="https://www.ech.ch/de/standards/ech-0108/6.0.0">eCH-0108 Unternehmens-Identifikation</a>', ""),
        row('<a href="https://www.ech.ch/de/dokument/ech-0097">Standard</a>', "v2.1"),
        row('<a href="https://www.ech.ch/de/about">About</a>', ""),
    )
    a, b, c = parse(html).refs
    assert (a.kind, a.name, a.baseline_version) == (SourceKind.ECH, "eCH-0108", "6.0.0")
    assert a.url == "https://www.ech.ch/de/standards/ech-0108/6.0.0"
    assert (b.kind, b.name, b.baseline_version) == (SourceKind.ECH, "eCH-0097", "2.1")
    assert c.kind == SourceKind.URL


def test_stated_version_from_link_context():
    html = page(
        row('<a href="https://a.example/1">Swisspost Address Query v1.4.0</a>', "nicht erfasst"),
        row('<a href="https://a.example/2">Spec</a> (Version 2.3.1)', "nicht erfasst"),
        row('<a href="https://a.example/3">Spec</a> (<code>QueryServiceV1X8</code>)', ""),
        row('<a href="https://a.example/4">Swagger</a>', "nicht erfasst"),
        row('<a href="https://a.example/5">Spec</a> V2X1X3', ""),
    )
    refs = parse(html).refs
    assert [x.kind for x in refs] == [SourceKind.URL] * 5
    assert [x.baseline_version for x in refs] == ["1.4.0", "2.3.1", "1.8", None, "2.1.3"]


def test_version_cell_wins_over_context_and_compact_form_in_version_cell():
    html = page(
        row('<a href="https://a.example/1">Spec v1.0</a>', "v2.0"),
        row('<a href="https://a.example/2">Spec</a>', "V1X8"),
    )
    a, b = parse(html).refs
    assert a.baseline_version == "2.0"
    assert b.baseline_version == "1.8"


def test_plain_text_url_trailing_punctuation_stripped():
    html = page(
        row("Spec siehe https://a.example/spec.html.", "v1.2"),
        row("Other (https://a.example/o)", ""),
    )
    a, b = parse(html).refs
    assert (a.kind, a.url, a.baseline_version) == (SourceKind.URL, "https://a.example/spec.html", "1.2")
    assert (b.kind, b.url) == (SourceKind.URL, "https://a.example/o")


def test_plain_text_version_context_without_link():
    (ref,) = parse(page(row("Swisspost API V1X8", ""))).refs
    assert ref.kind == SourceKind.EXTERNAL
    assert ref.baseline_version == "1.8"


def test_bare_urls_in_section_are_endpoints_not_sources():
    tail = SECTION_TAIL.replace(
        "<tr><td><code>t-three</code>",
        "<tr><td><code>t-live</code></td><td>Basis-URL https://live.example/service/v1, OAuth2</td></tr>"
        "<tr><td><code>t-three</code>",
    )
    r = parse_anc(page(row("X (Links in Kapitel 2.2)", "nicht erfasst", "Schnittstellen")) + tail, titles=["Parent"])
    assert "https://live.example/service/v1" not in [x.url for x in r.refs]


def test_same_target_in_explicit_row_and_expanded_section_is_listed_once():
    html = page(
        row("X (Links in Kapitel 2.2)", "nicht erfasst", "Schnittstellen"),
        row('<a href="https://api.example/spec/3">Spec Three explicit</a>', "v2.0", "Operationen"),
    ) + SECTION_TAIL
    r = parse_anc(html, titles=["Parent"])
    three = [x for x in r.refs if x.url == "https://api.example/spec/3"]
    assert len(three) == 1
    assert three[0].name == "Spec Three explicit"
    assert "Operationen" in three[0].used_for and "[Kapitel 2.2]" in three[0].used_for


# --- error visibility: nothing may drop out of the sources silently ---------------------------

def test_found_table_without_any_source_is_an_issue():
    r = parse(page(row("", "", "")))
    assert r.found and r.refs == []
    assert any("yielded no source" in i for i in r.issues)


def test_alternative_header_is_recognised():
    r = parse(page(row(link("Fach A"), "v1"), header=("Dokument/Referenz", "Version", "Verwendet für")))
    assert r.found
    assert [x.title for x in r.refs] == ["Fach A"]


def test_unrecognised_table_under_quellen_heading_is_an_issue():
    html = "<h2>1.5 Quellen</h2><table><tr><th>Name</th><th>Wert</th></tr><tr><td>a</td><td>b</td></tr></table>"
    r = parse(html)
    assert not r.found
    assert any("include no source column" in i for i in r.issues)


def test_all_sources_tables_are_used():
    html = "<h2>Quellen</h2>" + sources_table(row(link("Fach A"), "v1")) + "<h2>Weitere Quellen</h2>" + sources_table(row(link("Fach B"), "v2"))
    assert [x.title for x in parse(html).refs] == ["Fach A", "Fach B"]


def test_rows_after_a_nested_table_are_kept():
    nested = "<table><tr><th>x</th></tr><tr><td>y</td></tr></table>"
    r = parse(page(row(link("Fach A") + nested, "v1"), row(link("Fach B"), "v2"), row(link("Fach C"), "v3")))
    assert [x.title for x in r.refs] == ["Fach A", "Fach B", "Fach C"]


def test_marker_matches_whole_words_only_and_names_its_column():
    (ref,) = parse(page(row(link("Eingabenotizen-Konzept"), "v1", "Regeln"))).refs
    assert ref.excluded_reason is None
    (ref,) = parse(page(row(link("Konzept"), "v1", "Arbeitsnotizen zum Thema"))).refs
    assert "column 'used for'" in ref.excluded_reason


def test_same_source_with_different_versions_is_an_issue():
    r = parse(page(row(link("Fach A"), "v1", "Regeln"), row(link("Fach A"), "v2", "Texte")))
    assert len(r.refs) == 1
    assert any("listed more than once with different versions" in i for i in r.issues)


def test_same_source_with_same_version_is_not_an_issue():
    r = parse(page(row(link("Fach A"), "v1", "Regeln"), row(link("Fach A"), "v1", "Texte")))
    assert not any("more than once" in i for i in r.issues)


def test_stand_date_is_not_read_from_the_sources_table():
    html = page(row("Spec Stand: 01.01.2020", "v1")) + "<p>Stand: 05.05.2026</p>"
    assert parse_page_properties(html).stand_date == date(2026, 5, 5)
    only_in_table = page(row("Spec Stand: 01.01.2020", "v1"))
    assert parse_page_properties(only_in_table).stand_date is None


def test_unusable_links_in_a_source_cell_are_an_issue():
    cell = link("Fach A") + ', <ac:link><ri:attachment ri:filename="spec.pdf" /></ac:link>'
    r = parse(page(row(cell, "v1, v2")))
    assert any("attachment 'spec.pdf'" in i for i in r.issues)
