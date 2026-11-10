import base64
import urllib.parse
import zlib

import pytest

from digest.spec_monitor.drawio import diff_diagrams


def _model(cells: str) -> str:
    return (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        + cells
        + "</root></mxGraphModel>"
    )


def _plain(cells: str, pages: int = 1) -> str:
    diagrams = "".join(
        f'<diagram id="d{i}" name="Page-{i}">{_model(cells)}</diagram>' for i in range(pages)
    )
    return f"<mxfile>{diagrams}</mxfile>"


def _compressed(cells: str) -> str:
    raw = urllib.parse.quote(_model(cells), safe="").encode()
    co = zlib.compressobj(wbits=-15)
    data = co.compress(raw) + co.flush()
    b64 = base64.b64encode(data).decode()
    return f'<mxfile><diagram id="d" name="Page-1">{b64}</diagram></mxfile>'


def _node(cid, value, x=0, y=0, style="rounded=1;"):
    return (
        f'<mxCell id="{cid}" value="{value}" style="{style}" vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="80" height="40" as="geometry"/></mxCell>'
    )


def _edge(cid, src, tgt, value=""):
    return (
        f'<mxCell id="{cid}" value="{value}" edge="1" parent="1" source="{src}" target="{tgt}">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


BASE = _node("a", "Client") + _node("b", "Server") + _edge("e1", "a", "b", "calls")


def test_identical_has_no_changes_and_not_layout_only():
    d = diff_diagrams(_plain(BASE), _plain(BASE))
    assert d.changes == [] and d.layout_only is False and d.warnings == []


def test_added_and_removed_node_text():
    new = BASE + _node("c", "Gateway")
    d = diff_diagrams(_plain(BASE), _plain(new))
    assert any("Gateway" in c and "added" in c.lower() for c in d.changes)
    d2 = diff_diagrams(_plain(new), _plain(BASE))
    assert any("Gateway" in c and "removed" in c.lower() for c in d2.changes)


def test_edge_added_uses_source_label_target_format():
    new = BASE + _node("c", "Gateway") + _edge("e2", "b", "c", "routes")
    d = diff_diagrams(_plain(BASE), _plain(new))
    assert any("Server -[routes]-> Gateway" in c for c in d.changes)


def test_changed_edge_label():
    new = _node("a", "Client") + _node("b", "Server") + _edge("e1", "a", "b", "posts")
    d = diff_diagrams(_plain(BASE), _plain(new))
    joined = "\n".join(d.changes)
    assert "calls" in joined and "posts" in joined
    assert "Client" in joined and "Server" in joined


def test_compressed_variant_gives_same_result_as_plain():
    new = BASE + _node("c", "Gateway")
    plain = diff_diagrams(_plain(BASE), _plain(new))
    comp = diff_diagrams(_compressed(BASE), _compressed(new))
    assert comp.changes == plain.changes and comp.changes
    mixed = diff_diagrams(_compressed(BASE), _plain(new))
    assert mixed.changes == plain.changes


def test_layout_and_style_only_change():
    moved = (
        _node("a", "Client", x=300, y=200, style="rounded=0;fillColor=#ff0000;")
        + _node("b", "Server", x=10, y=10)
        + _edge("e1", "a", "b", "calls")
    )
    d = diff_diagrams(_plain(BASE), _plain(moved))
    assert d.changes == []
    assert d.layout_only is True


def test_html_labels_are_cleaned():
    old = _node("a", "Client") + _node("b", "Server")
    new = _node("a", "&lt;div&gt;Client&lt;br&gt;App&lt;/div&gt;") + _node("b", "Server")
    d = diff_diagrams(_plain(old), _plain(new))
    joined = "\n".join(d.changes)
    assert "Client/App" in joined
    assert "<" not in joined


def test_formatting_only_label_change_is_not_a_change():
    old = _node("a", "Client")
    new = _node("a", "&lt;b&gt;Client&lt;/b&gt;")
    d = diff_diagrams(_plain(old), _plain(new))
    assert d.changes == []
    assert d.layout_only is True


def test_edge_to_textless_node_has_readable_fallback():
    old = _node("a", "Client")
    new = old + _node("z", "") + _edge("e1", "a", "z", "x")
    d = diff_diagrams(_plain(old), _plain(new))
    joined = "\n".join(d.changes)
    assert "Client -[x]->" in joined
    assert "None" not in joined and "[]" not in joined.replace("-[x]->", "")


def test_edge_label_as_separate_label_cell():
    label_cell = (
        '<mxCell id="l1" value="sends" style="edgeLabel;html=1;" vertex="1" connectable="0" parent="e1">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )
    new = _node("a", "Client") + _node("b", "Server") + _edge("e1", "a", "b") + label_cell
    old = _node("a", "Client") + _node("b", "Server") + _edge("e1", "a", "b")
    d = diff_diagrams(_plain(old), _plain(new))
    joined = "\n".join(d.changes)
    assert "sends" in joined
    assert not any("Node" in c and "sends" in c for c in d.changes)


def test_user_object_wrapper_label():
    wrapped = (
        '<UserObject label="Wrapped" id="w"><mxCell style="rounded=1" vertex="1" parent="1">'
        '<mxGeometry width="80" height="40" as="geometry"/></mxCell></UserObject>'
    )
    d = diff_diagrams(_plain(_node("a", "Client")), _plain(_node("a", "Client") + wrapped))
    assert any("Wrapped" in c for c in d.changes)


def _pages(**cells_by_name):
    diagrams = "".join(
        f'<diagram id="d{i}" name="{name}">{_model(cells)}</diagram>' for i, (name, cells) in enumerate(cells_by_name.items())
    )
    return f"<mxfile>{diagrams}</mxfile>"


def test_change_on_second_page_is_detected():
    old = _pages(Main=BASE, Detail=_node("x", "Step 1"))
    new = _pages(Main=BASE, Detail=_node("x", "Step 1") + _node("y", "Step 2"))
    d = diff_diagrams(old, new)
    assert d.changes == ["[page Detail] Node added: Step 2"]


def test_added_and_removed_page_are_changes():
    d = diff_diagrams(_pages(Main=BASE), _pages(Main=BASE, Extra=_node("x", "Hidden")))
    assert "Page added: Extra" in d.changes and "[page Extra] Node added: Hidden" in d.changes
    d2 = diff_diagrams(_pages(Main=BASE, Extra=_node("x", "Hidden")), _pages(Main=BASE))
    assert "Page removed: Extra" in d2.changes


def test_identical_multi_page_has_no_changes():
    assert diff_diagrams(_plain(BASE, pages=2), _plain(BASE, pages=2)).changes == []


def test_single_renamed_page_is_still_compared():
    old = f'<mxfile><diagram name="A">{_model(BASE)}</diagram></mxfile>'
    new = f'<mxfile><diagram name="B">{_model(BASE + _node("c", "Gateway"))}</diagram></mxfile>'
    assert diff_diagrams(old, new).changes == ["Node added: Gateway"]


def _arrow_edge(style):
    return (
        f'<mxCell id="e1" value="calls" style="{style}" edge="1" parent="1" source="a" target="b">'
        '<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def test_arrow_direction_change_is_semantic():
    nodes = _node("a", "Client") + _node("b", "Server")
    old = nodes + _arrow_edge("endArrow=classic;")
    new = nodes + _arrow_edge("endArrow=none;startArrow=classic;")
    d = diff_diagrams(_plain(old), _plain(new))
    assert d.layout_only is False
    assert any("Edge arrows changed" in c and "Client -[calls]-> Server" in c for c in d.changes)


def test_default_arrows_are_not_shown_and_not_a_change():
    nodes = _node("a", "Client") + _node("b", "Server")
    d = diff_diagrams(_plain(nodes + _arrow_edge("")), _plain(nodes + _arrow_edge("startArrow=none;endArrow=classic;")))
    assert d.changes == []


def _obj(cid, label, **attrs):
    extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return (
        f'<object label="{label}" id="{cid}"{extra}><mxCell style="rounded=1" vertex="1" parent="1">'
        '<mxGeometry width="80" height="40" as="geometry"/></mxCell></object>'
    )


def test_object_custom_attribute_change_is_semantic():
    d = diff_diagrams(_plain(_obj("o", "Box", req="CM-FR-03")), _plain(_obj("o", "Box", req="CM-FR-04")))
    assert d.layout_only is False
    assert any("Node Box" in c and "req='CM-FR-03' removed" in c and "req='CM-FR-04' added" in c for c in d.changes)


def test_bare_mxgraphmodel_is_accepted():
    d = diff_diagrams(_model(BASE), _model(BASE + _node("c", "Gateway")))
    assert any("Gateway" in c for c in d.changes)


@pytest.mark.parametrize("bad", ["", "not xml at all", "<mxfile></mxfile>", "<html/>",
                                 "<mxfile><diagram>@@@not-base64@@@</diagram></mxfile>"])
def test_garbage_raises_value_error(bad):
    with pytest.raises(ValueError):
        diff_diagrams(bad, _plain(BASE))
    with pytest.raises(ValueError):
        diff_diagrams(_plain(BASE), bad)


def test_doctype_entities_rejected():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><mxfile><diagram>&a;</diagram></mxfile>'
    with pytest.raises(ValueError):
        diff_diagrams(evil, _plain(BASE))
