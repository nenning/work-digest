"""Storage-format XHTML -> diffable plain text, text diffs, link/diagram extraction."""
import difflib
import html as _html
import re
from collections import Counter
from typing import Dict, List, Tuple

_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_TOKEN_RE = re.compile(r"\x00(\d+)\x00")
_LINK_RE = re.compile(r"<ac:link\b([^>]*?)(?:/>|>(.*?)</ac:link>)", re.DOTALL | re.IGNORECASE)
_LINK_BODY_RE = re.compile(
    r"<ac:(?:plain-text-)?link-body[^>]*>(.*?)</ac:(?:plain-text-)?link-body>", re.DOTALL | re.IGNORECASE
)
_PAGE_TITLE_RE = re.compile(r"<ri:page\b[^>]*?ri:content-title=(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_FILENAME_RE = re.compile(r"<ri:attachment\b[^>]*?ri:filename=(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"ac:anchor=(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_PARAM_RE = re.compile(r"<ac:parameter\b[^>]*>(.*?)</ac:parameter>", re.DOTALL | re.IGNORECASE)
_PARAM_NAMED_RE = re.compile(
    r"<ac:parameter\b[^>]*?ac:name=(?:\"([^\"]*)\"|'([^']*)')[^>]*>(.*?)</ac:parameter>",
    re.DOTALL | re.IGNORECASE,
)
_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.DOTALL | re.IGNORECASE)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", re.DOTALL | re.IGNORECASE)
_DRAWIO_RE = re.compile(
    r"<ac:structured-macro\b[^>]*?ac:name=[\"']drawio[\"'][^>]*>(.*?)</ac:structured-macro>",
    re.DOTALL | re.IGNORECASE,
)
_BLOCK_RE = re.compile(
    r"</?(?:p|li|ul|ol|h[1-6]|br|div|tr|table|blockquote|ac:task|ac:rich-text-body|ac:plain-text-body)\b[^>]*>",
    re.IGNORECASE,
)
_HREF_LINK_RE = re.compile(
    r"<a\b[^>]*?\bhref=(?:\"([^\"]*)\"|'([^']*)')[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE
)
_TIME_RE = re.compile(
    r"<time\b[^>]*?\bdatetime=(?:\"([^\"]*)\"|'([^']*)')[^>]*?(?:/>|>(?:\s*</time>)?)", re.DOTALL | re.IGNORECASE
)
_IMAGE_RE = re.compile(r"<ac:image\b([^>]*?)(?:/>|>(.*?)</ac:image>)", re.DOTALL | re.IGNORECASE)
_IMAGE_URL_RE = re.compile(r"<ri:url\b[^>]*?ri:value=(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_OL_RE = re.compile(r"<ol\b[^>]*>(.*?)</ol>", re.DOTALL | re.IGNORECASE)
_LI_OPEN_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")


def _first(m: "re.Match", *groups: int) -> str:
    for g in groups:
        if m.group(g) is not None:
            return m.group(g)
    return ""


def _inline_text(fragment: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", fragment)).strip()


def storage_to_text(storage_html: str) -> str:
    """Confluence storage XHTML to plain text for diffing (CDATA unwrapped before
    tags are stripped; ac:link -> 'Text[->TargetPage#anchor]'; table cells joined by ' | ';
    macro parameters kept as '[value]'; block ends become newlines)."""
    cdata: List[str] = []

    def stash(m: "re.Match") -> str:
        cdata.append(m.group(1))
        return f"\x00{len(cdata) - 1}\x00"

    text = _CDATA_RE.sub(stash, storage_html or "")

    def render_link(m: "re.Match") -> str:
        attrs, inner = m.group(1) or "", m.group(2) or ""
        page = _PAGE_TITLE_RE.search(inner)
        att = _FILENAME_RE.search(inner)
        anchor = _ANCHOR_RE.search(attrs)
        target = _first(page, 1, 2) if page else (_first(att, 1, 2) if att else "")
        if anchor:
            target += "#" + _first(anchor, 1, 2)
        body = _LINK_BODY_RE.search(inner)
        label = _inline_text(body.group(1)) if body else ""
        if not label:
            label = _first(page, 1, 2) if page else (_first(att, 1, 2) if att else "")
        return f" {label}[->{target}] " if target else f" {label} "

    text = _LINK_RE.sub(render_link, text)

    def render_href(m: "re.Match") -> str:
        label = _inline_text(m.group(3))
        return f" {label}[->{_first(m, 1, 2)}] "

    text = _HREF_LINK_RE.sub(render_href, text)
    text = _TIME_RE.sub(lambda m: f" {_first(m, 1, 2)} ", text)

    def render_image(m: "re.Match") -> str:
        inner = m.group(2) or ""
        att = _FILENAME_RE.search(inner)
        url = _IMAGE_URL_RE.search(inner)
        name = _first(att, 1, 2) if att else (_first(url, 1, 2) if url else "")
        return f" [image: {name}] " if name else " [image] "

    text = _IMAGE_RE.sub(render_image, text)

    def render_ordered(m: "re.Match") -> str:
        # auto-numbering is not in the markup text: write it out so reordering steps is visible
        counter = iter(range(1, 10_000))
        return "<ol>" + _LI_OPEN_RE.sub(lambda _li: f"<li>{next(counter)}. ", m.group(1)) + "</ol>"

    text = _OL_RE.sub(render_ordered, text)

    def render_param(m: "re.Match") -> str:
        value = _inline_text(m.group(1))
        return f" [{value}] " if value else ""

    text = _PARAM_RE.sub(render_param, text)

    def render_table(m: "re.Match") -> str:
        rows = []
        for row in _ROW_RE.finditer(m.group(1)):
            cells = [_inline_text(c.group(1)) for c in _CELL_RE.finditer(row.group(1))]
            if any(cells):
                rows.append(" | ".join(cells))
        return "\n" + "\n".join(rows) + "\n"

    text = _TABLE_RE.sub(render_table, text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    text = _TOKEN_RE.sub(lambda m: cdata[int(m.group(1))], text)

    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def diff_text(old: str, new: str) -> str:
    """Line diff of two plain texts; only changed lines, prefixed '- ' / '+ '.
    Empty string when identical."""
    out = []
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))
    for ln in lines[2:]:
        if ln.startswith("@@"):
            continue
        out.append(f"{ln[0]} {ln[1:]}")
    return "\n".join(out)


_BULLET_RE = re.compile(r"^\s*[-*•]\s+")
# words plus symbols that change a requirement's meaning: comparison/arithmetic/percent/currency operators,
# arrows, math symbols (≤ ≥ ≠ ∈ ...), check/cross marks, a question mark, a minus only as a sign
# (before a digit) or a spaced operator, a decimal separator between digits. '*' stays noise (markdown).
_MEANING_RE = re.compile(
    r"[^\W_]+|!=|[<>=+%$€£¥&|/~^#@?\u00b1\u00d7\u00f7\u2190-\u21ff\u2200-\u22ff\u2713-\u2718\u2705\u274c]"
    r"|(?<![^\W_])-(?=\d)|(?<=\s)-(?=\s)|(?<=\d)[.,](?=\d)"
)


def _normalize(line: str) -> str:
    return " ".join(_MEANING_RE.findall(_BULLET_RE.sub("", line.lower())))


def is_cosmetic(diff: str) -> bool:
    """True when every changed line is cosmetic (whitespace, punctuation, case,
    pure reordering of identical lines). Empty diff counts as cosmetic. Short lines are NOT
    ignored: in specs `403` -> `404` or `v46` -> `v47` is a real change."""
    added: Counter = Counter()
    removed: Counter = Counter()
    for ln in diff.splitlines():
        if ln[:1] not in "+-" or not ln:
            continue
        norm = _normalize(ln[1:].strip())
        if not norm:
            continue
        (added if ln[0] == "+" else removed)[norm] += 1
    return not (added - removed) and not (removed - added)


def extract_page_links(storage_html: str) -> List[str]:
    """Titles of Confluence pages linked from the body (ac:link ri:page content-title),
    de-duplicated, in order of appearance."""
    titles: List[str] = []
    for link in _LINK_RE.finditer(storage_html or ""):
        page = _PAGE_TITLE_RE.search(link.group(2) or "")
        if not page:
            continue
        title = _html.unescape(_first(page, 1, 2)).strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def added_links(old_storage: str, new_storage: str) -> List[str]:
    """Page titles linked in new_storage but not in old_storage."""
    old = set(extract_page_links(old_storage))
    return [t for t in extract_page_links(new_storage) if t not in old]


def image_attachments(storage_html: str) -> Tuple[List[str], int]:
    """(filenames of images attached to this very page, de-duplicated in order of appearance;
    number of other images: attachments of other pages and external URLs)."""
    names: List[str] = []
    other = 0
    for m in _IMAGE_RE.finditer(storage_html or ""):
        inner = m.group(2) or ""
        att = _FILENAME_RE.search(inner)
        if att is None or _PAGE_TITLE_RE.search(inner):
            other += 1
            continue
        name = _html.unescape(_first(att, 1, 2)).strip()
        if name and name not in names:
            names.append(name)
    return names, other


def drawio_revisions(storage_html: str) -> Dict[str, int]:
    """drawio macro parameters: diagramName -> revision, from
    <ac:parameter ac:name="revision">N</ac:parameter> + <ac:parameter ac:name="diagramName">NAME</...>."""
    result: Dict[str, int] = {}
    for macro in _DRAWIO_RE.finditer(storage_html or ""):
        params = {
            _first(p, 1, 2): _html.unescape(_inline_text(p.group(3)))
            for p in _PARAM_NAMED_RE.finditer(macro.group(1))
        }
        name, rev = params.get("diagramName"), params.get("revision")
        if not name or rev is None or not rev.strip().isdigit():
            continue
        result[name] = int(rev)
    return result
