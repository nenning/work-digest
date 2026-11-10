"""Semantic diff of draw.io diagrams."""
import base64
import binascii
import html
import re
import urllib.parse
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

try:
    from defusedxml import ElementTree as _SafeET
except ImportError:
    _SafeET = None

_UNSAFE_XML = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_BREAK = "\x00"
_BREAK_TAGS = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>|</\s*div\s*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]*>")
_MAX_DECOMPRESSED = 50 * 1024 * 1024
_OBJECT_META_ATTRS = {"label", "id", "placeholders", "tooltip"}
_DEFAULT_ARROWS = "none->classic"


@dataclass
class DiagramDiff:
    changes: List[str] = field(default_factory=list)   # human-readable semantic changes
    layout_only: bool = False                          # only geometry/style changed
    warnings: List[str] = field(default_factory=list)


def _parse_xml(text: str) -> ET.Element:
    if _UNSAFE_XML.search(text):
        raise ValueError("draw.io file contains a DOCTYPE/ENTITY declaration; refusing to parse")
    try:
        if _SafeET is not None:
            return _SafeET.fromstring(text)
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"not parseable XML: {e}") from e
    except Exception as e:
        raise ValueError(f"not parseable XML: {e}") from e


def _decode_diagram_text(text: str) -> str:
    text = text.strip()
    if text.startswith("<"):
        return text
    try:
        data = base64.b64decode(text, validate=False)
        d = zlib.decompressobj(-15)
        raw = d.decompress(data, _MAX_DECOMPRESSED)
        if d.unconsumed_tail:
            raise ValueError("decompressed diagram too large")
        return urllib.parse.unquote(raw.decode("utf-8"))
    except (binascii.Error, zlib.error, UnicodeDecodeError) as e:
        raise ValueError(f"diagram content is neither XML nor compressed draw.io data: {e}") from e


def _load_models(xml_text: str) -> List[Tuple[str, ET.Element]]:
    """Every diagram page of a draw.io file as (page key, mxGraphModel)."""
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise ValueError("empty draw.io content")
    root = _parse_xml(xml_text.strip())
    if root.tag == "mxGraphModel":
        return [("", root)]
    if root.tag != "mxfile":
        raise ValueError(f"not a draw.io file (root element <{root.tag}>)")
    diagrams = root.findall("diagram")
    if not diagrams:
        raise ValueError("draw.io file has no <diagram>")
    models: List[Tuple[str, ET.Element]] = []
    seen: Counter = Counter()
    for index, diagram in enumerate(diagrams, 1):
        name = diagram.get("name") or f"page {index}"
        seen[name] += 1
        key = name if seen[name] == 1 else f"{name} ({seen[name]})"
        inline = diagram.find("mxGraphModel")
        if inline is not None:
            models.append((key, inline))
            continue
        text = diagram.text or ""
        if not text.strip():
            raise ValueError(f"<diagram> '{key}' is empty")
        model = _parse_xml(_decode_diagram_text(text))
        if model.tag != "mxGraphModel":
            raise ValueError(f"diagram content is not an mxGraphModel (<{model.tag}>)")
        models.append((key, model))
    return models


def _clean(value: str) -> str:
    if not value:
        return ""
    s = _BREAK_TAGS.sub(_BREAK, value)
    s = _ANY_TAG.sub("", s)
    s = html.unescape(s).replace("\xa0", " ")
    parts = [" ".join(p.split()) for p in s.split(_BREAK)]
    return "/".join(p for p in parts if p)


def _signature(el: ET.Element):
    return (el.tag, tuple(sorted(el.attrib.items())), tuple(_signature(c) for c in el))


def _graph(model: ET.Element):
    root = model.find("root")
    if root is None:
        raise ValueError("mxGraphModel has no <root>")
    cells: Dict[str, dict] = {}
    for el in root:
        attrs: Tuple[Tuple[str, str], ...] = ()
        if el.tag == "mxCell":
            cell, label = el, el.get("value", "")
            cid = el.get("id")
        elif el.tag in ("object", "UserObject"):
            cell = el.find("mxCell")
            if cell is None:
                continue
            label, cid = el.get("label", ""), el.get("id")
            # custom properties of the shape (ids, requirement refs, ...) carry meaning
            attrs = tuple(sorted((k, v) for k, v in el.attrib.items() if k not in _OBJECT_META_ATTRS))
        else:
            continue
        if not cid:
            continue
        cells[cid] = {
            "id": cid,
            "text": _clean(label),
            "edge": cell.get("edge") == "1",
            "vertex": cell.get("vertex") == "1",
            "source": cell.get("source"),
            "target": cell.get("target"),
            "parent": cell.get("parent"),
            "style": cell.get("style", ""),
            "attrs": attrs,
        }

    edge_labels: Dict[str, List[str]] = {}
    label_cells = set()
    for c in cells.values():
        parent = cells.get(c["parent"] or "")
        if c["vertex"] and parent is not None and parent["edge"] and "edgeLabel" in c["style"]:
            label_cells.add(c["id"])
            if c["text"]:
                edge_labels.setdefault(parent["id"], []).append(c["text"])

    def node_name(cid: Optional[str]) -> str:
        if not cid or cid not in cells:
            return "(unconnected end)"
        text = cells[cid]["text"]
        return text if text else f"(unlabeled node {cid})"

    nodes: Counter = Counter()
    edges: Counter = Counter()
    attrs: Dict[str, set] = {}
    for c in cells.values():
        if c["id"] in label_cells:
            continue
        if c["edge"]:
            labels = ([c["text"]] if c["text"] else []) + edge_labels.get(c["id"], [])
            edge = (node_name(c["source"]), "/".join(labels), node_name(c["target"]), _arrows(c["style"]))
            edges[edge] += 1
            if c["attrs"]:
                attrs.setdefault(f"Edge {_fmt_edge(edge[:3])}", set()).update(c["attrs"])
        elif c["vertex"] and c["text"]:
            nodes[c["text"]] += 1
            if c["attrs"]:
                attrs.setdefault(f"Node {c['text']}", set()).update(c["attrs"])
    return nodes, edges, attrs, _signature(root)


def _arrows(style: str) -> str:
    """'<start>-><end>' arrow heads of an edge style; draw.io defaults are none -> classic."""
    opts = dict(part.split("=", 1) for part in style.split(";") if "=" in part)
    return f"{opts.get('startArrow', 'none')}->{opts.get('endArrow', 'classic')}"


def _fmt_edge(e: Tuple[str, ...]) -> str:
    src, label, tgt = e[0], e[1], e[2]
    arrows = e[3] if len(e) > 3 and e[3] != _DEFAULT_ARROWS else ""
    text = f"{src} -[{label}]-> {tgt}" if label else f"{src} -> {tgt}"
    return f"{text} (arrows {arrows})" if arrows else text


def _diff_graph(old, new, prefix: str) -> List[str]:
    old_nodes, old_edges, old_attrs, _ = old
    new_nodes, new_edges, new_attrs, _ = new
    changes: List[str] = []
    for text in sorted((new_nodes - old_nodes).elements()):
        changes.append(f"{prefix}Node added: {text}")
    for text in sorted((old_nodes - new_nodes).elements()):
        changes.append(f"{prefix}Node removed: {text}")

    removed = sorted((old_edges - new_edges).elements())
    added = sorted((new_edges - old_edges).elements())
    for r in list(removed):  # same ends and arrows, different label
        match = next((a for a in added if a[0] == r[0] and a[2] == r[2] and a[3] == r[3]), None)
        if match is None:
            continue
        removed.remove(r)
        added.remove(match)
        changes.append(
            f"{prefix}Edge label changed: {r[0]} -> {r[2]}: '{r[1] or '(none)'}' => '{match[1] or '(none)'}'"
        )
    for r in list(removed):  # same ends and label, different arrow heads
        match = next((a for a in added if a[:3] == r[:3]), None)
        if match is None:
            continue
        removed.remove(r)
        added.remove(match)
        changes.append(f"{prefix}Edge arrows changed: {_fmt_edge(r[:3])}: {r[3]} => {match[3]}")
    for e in added:
        changes.append(f"{prefix}Edge added: {_fmt_edge(e)}")
    for e in removed:
        changes.append(f"{prefix}Edge removed: {_fmt_edge(e)}")

    for key in sorted(set(old_attrs) & set(new_attrs)):
        gone = sorted(old_attrs[key] - new_attrs[key])
        came = sorted(new_attrs[key] - old_attrs[key])
        if gone or came:
            parts = [f"{k}='{v}' removed" for k, v in gone] + [f"{k}='{v}' added" for k, v in came]
            changes.append(f"{prefix}{key}: properties changed ({'; '.join(parts)})")
    return changes


def diff_diagrams(old_xml: str, new_xml: str) -> DiagramDiff:
    """Compare two .drawio files (mxfile), every <diagram> page (paired by name). <diagram> content is
    either a plain mxGraphModel or base64 + raw deflate + URL-encoded. Nodes compared by cleaned text;
    edges as 'source -[label]-> target' incl. arrow heads; custom properties of <object> shapes are
    compared too. Labels are cleaned (<br>, </p>, </div> -> '/', HTML stripped).
    Raises ValueError on content that is not a parseable draw.io file."""
    old_pages = dict(_load_models(old_xml))
    new_pages = dict(_load_models(new_xml))
    if len(old_pages) == 1 and len(new_pages) == 1:   # a renamed single page (or bare model) is still the same page
        new_pages = {next(iter(old_pages)): next(iter(new_pages.values()))}
    multi = len(old_pages) > 1 or len(new_pages) > 1
    empty = (Counter(), Counter(), {}, None)

    result = DiagramDiff()
    layout_changed = False
    for key in list(old_pages) + [k for k in new_pages if k not in old_pages]:
        prefix = f"[page {key}] " if multi else ""
        old = _graph(old_pages[key]) if key in old_pages else empty
        new = _graph(new_pages[key]) if key in new_pages else empty
        if key not in old_pages:
            result.changes.append(f"Page added: {key}")
        elif key not in new_pages:
            result.changes.append(f"Page removed: {key}")
        result.changes.extend(_diff_graph(old, new, prefix))
        if old[3] != new[3]:
            layout_changed = True

    result.layout_only = not result.changes and layout_changed
    return result
