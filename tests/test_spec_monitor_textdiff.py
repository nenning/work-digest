import pytest

from digest.spec_monitor.textdiff import (
    added_links,
    diff_text,
    drawio_revisions,
    extract_page_links,
    is_cosmetic,
    storage_to_text,
)


def _link(title, body=None, anchor=None):
    a = f' ac:anchor="{anchor}"' if anchor else ""
    b = f"<ac:link-body>{body}</ac:link-body>" if body else ""
    return f'<ac:link{a}><ri:page ri:content-title="{title}" />{b}</ac:link>'


def test_cdata_code_macro_text_survives():
    html = (
        '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
        "<![CDATA[if (a < b && c > d) {\n  <tag>x</tag>\n}]]>"
        "</ac:plain-text-body></ac:structured-macro>"
    )
    text = storage_to_text(html)
    assert "if (a < b && c > d) {" in text
    assert "<tag>x</tag>" in text


def test_ac_link_rendered_with_target_and_anchor():
    text = storage_to_text("<p>Siehe " + _link("Zielseite", "Mehr", "abschnitt-1") + " dazu.</p>")
    assert "Mehr[->Zielseite#abschnitt-1]" in text


def test_ac_link_without_anchor_or_body():
    text = storage_to_text("<p>" + _link("Zielseite") + "</p>")
    assert "Zielseite[->Zielseite]" in text


def test_ac_link_plain_text_cdata_body():
    html = (
        '<ac:link><ri:page ri:content-title="Seite B" />'
        "<ac:plain-text-link-body><![CDATA[Klartext]]></ac:plain-text-link-body></ac:link>"
    )
    assert "Klartext[->Seite B]" in storage_to_text(html)


def test_table_cells_joined_with_pipe():
    html = "<table><tbody><tr><th>Name</th><th>Wert</th></tr><tr><td><p>Alpha</p></td><td>1</td></tr></tbody></table>"
    lines = storage_to_text(html).splitlines()
    assert lines == ["Name | Wert", "Alpha | 1"]


def test_macro_parameters_kept_in_brackets():
    html = (
        '<ac:structured-macro ac:name="info"><ac:parameter ac:name="title">Hinweis</ac:parameter>'
        "<ac:rich-text-body><p>Inhalt</p></ac:rich-text-body></ac:structured-macro>"
    )
    text = storage_to_text(html)
    assert "[Hinweis]" in text
    assert "Inhalt" in text


def test_block_ends_become_newlines_and_entities_decoded():
    text = storage_to_text("<h1>Titel</h1><p>A &amp; B</p><ul><li>eins</li><li>zwei</li></ul>")
    assert text.splitlines() == ["Titel", "A & B", "eins", "zwei"]


def test_diff_identical_is_empty():
    assert diff_text("a\nb", "a\nb") == ""


def test_diff_only_changed_lines_prefixed():
    d = diff_text("eins\nzwei\ndrei", "eins\nzwei geaendert\ndrei")
    assert d.splitlines() == ["- zwei", "+ zwei geaendert"]


def test_empty_diff_is_cosmetic():
    assert is_cosmetic("") is True


def test_whitespace_and_punctuation_change_is_cosmetic():
    d = diff_text(
        "Das System speichert alle Daten.", "Das  System speichert alle Daten!"
    )
    assert d
    assert is_cosmetic(d) is True


def test_changed_sentence_is_not_cosmetic():
    d = diff_text(
        "Das System speichert alle Daten.", "Das System verwirft alle Daten sofort."
    )
    assert is_cosmetic(d) is False


def test_short_value_change_is_not_cosmetic():
    assert is_cosmetic(diff_text("Status\n403", "Status\n404")) is False
    assert is_cosmetic(diff_text("Version v46", "Version v47")) is False


@pytest.mark.parametrize("old,new", [
    ("Alter >= 18", "Alter > 18"),
    ("Betrag < 100", "Betrag > 100"),
    ("Saldo 100", "Saldo -100"),
    ("Anteil 5%", "Anteil 5"),
    ("Preis 10 CHF", "Preis 10 EUR"),
    ("Preis 10 €", "Preis 10 $"),
    ("Wert = 3", "Wert != 3"),
    ("Feld a + b", "Feld a - b"),
])
def test_meaning_carrying_symbols_are_not_cosmetic(old, new):
    assert is_cosmetic(diff_text(old, new)) is False


def test_markup_and_bullet_noise_is_cosmetic():
    assert is_cosmetic("- * Feld Alter\n+ - Feld Alter") is True
    assert is_cosmetic("- Feld **Alter**\n+ Feld Alter:") is True
    assert is_cosmetic('- Feld "Alter"\n+ Feld Alter') is True
    assert is_cosmetic("- Feld (Alter)\n+ Feld Alter") is True
    assert is_cosmetic("- Feld a_b\n+ Feld a b") is True


def test_case_only_change_is_cosmetic():
    assert is_cosmetic(diff_text("Der Dienst liefert Daten", "der dienst liefert daten")) is True


def test_pure_reordering_is_cosmetic():
    old = "Erste lange Zeile hier\nZweite lange Zeile hier\nDritte lange Zeile hier"
    new = "Dritte lange Zeile hier\nErste lange Zeile hier\nZweite lange Zeile hier"
    d = diff_text(old, new)
    assert d
    assert is_cosmetic(d) is True


def test_added_line_is_not_cosmetic():
    d = diff_text("Erste lange Zeile hier", "Erste lange Zeile hier\nNeue inhaltliche Anforderung")
    assert is_cosmetic(d) is False


def test_extract_page_links_dedup_in_order():
    html = _link("B") + _link("A") + _link("B", "x") + "<ac:link><ri:attachment ri:filename=\"f.png\" /></ac:link>"
    assert extract_page_links(html) == ["B", "A"]


def test_extract_page_links_unescapes_titles():
    assert extract_page_links(_link("Q &amp; A")) == ["Q & A"]


def test_added_links_only_new_titles():
    old = _link("A") + _link("B")
    new = _link("B") + _link("C") + _link("D")
    assert added_links(old, new) == ["C", "D"]


def test_drawio_revisions_any_parameter_order():
    m1 = (
        '<ac:structured-macro ac:name="drawio" ac:schema-version="1">'
        '<ac:parameter ac:name="revision">3</ac:parameter>'
        '<ac:parameter ac:name="diagramName">Fluss A</ac:parameter>'
        "</ac:structured-macro>"
    )
    m2 = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="diagramName">Fluss B</ac:parameter>'
        '<ac:parameter ac:name="simple">0</ac:parameter>'
        '<ac:parameter ac:name="revision">12</ac:parameter>'
        "</ac:structured-macro>"
    )
    other = (
        '<ac:structured-macro ac:name="info"><ac:parameter ac:name="revision">9</ac:parameter>'
        "</ac:structured-macro>"
    )
    assert drawio_revisions(m1 + other + m2) == {"Fluss A": 3, "Fluss B": 12}


def test_drawio_revisions_none():
    assert drawio_revisions("<p>nichts</p>") == {}


@pytest.mark.parametrize("old,new", [
    ("Wert ≤ 5", "Wert ≥ 5"),
    ("Wert ≠ 5", "Wert = 5"),
    ("Status A → B", "Status B → A"),
    ("Feld ✓", "Feld ✗"),
    ("Toleranz ±5", "Toleranz 5"),
    ("Pflichtfeld?", "Pflichtfeld"),
])
def test_unicode_symbol_flips_are_not_cosmetic(old, new):
    assert is_cosmetic(diff_text(old, new)) is False


def test_symbol_only_line_is_not_skipped():
    assert is_cosmetic("- ✓\n+ ✗") is False
    assert is_cosmetic("- ≤\n+ ≥") is False


def test_href_target_is_rendered_and_change_detected():
    old = storage_to_text('<p>Siehe <a href="https://a.example/spec">Spezifikation</a></p>')
    new = storage_to_text('<p>Siehe <a href="https://b.example/spec">Spezifikation</a></p>')
    assert "Spezifikation[->https://a.example/spec]" in old
    assert is_cosmetic(diff_text(old, new)) is False


def test_href_inside_table_cell_is_kept():
    text = storage_to_text('<table><tr><td><a href="https://x.example">Doc</a></td><td>1</td></tr></table>')
    assert "Doc[->https://x.example] | 1" in text


def test_time_datetime_is_rendered():
    old = storage_to_text('<p>Frist <time datetime="2026-01-01" /></p>')
    new = storage_to_text('<p>Frist <time datetime="2026-03-01" /></p>')
    assert "2026-01-01" in old
    assert is_cosmetic(diff_text(old, new)) is False


def test_image_filename_is_rendered():
    old = storage_to_text('<p><ac:image ac:alt="x"><ri:attachment ri:filename="flow-v1.png" /></ac:image></p>')
    new = storage_to_text('<p><ac:image ac:alt="x"><ri:attachment ri:filename="flow-v2.png" /></ac:image></p>')
    assert "[image: flow-v1.png]" in old
    assert is_cosmetic(diff_text(old, new)) is False


def test_reordering_ordered_list_is_a_real_change():
    old = storage_to_text("<ol><li>Antrag prüfen</li><li>Antrag freigeben</li><li>Antrag archivieren</li></ol>")
    new = storage_to_text("<ol><li>Antrag freigeben</li><li>Antrag prüfen</li><li>Antrag archivieren</li></ol>")
    assert old.splitlines()[0] == "1. Antrag prüfen"
    assert is_cosmetic(diff_text(old, new)) is False


def test_reordering_unordered_list_stays_cosmetic():
    old = storage_to_text("<ul><li>Erste lange Zeile</li><li>Zweite lange Zeile</li></ul>")
    new = storage_to_text("<ul><li>Zweite lange Zeile</li><li>Erste lange Zeile</li></ul>")
    assert diff_text(old, new)
    assert is_cosmetic(diff_text(old, new)) is True
