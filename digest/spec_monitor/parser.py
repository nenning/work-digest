"""Parses a spec page's source list (section "Quellen") and page properties from
Confluence storage format. Pure functions: no network, no LLM."""
import re
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from digest.config import GitLabConfig, GitLabRepoConfig
from digest.spec_monitor.models import PageProperties, ParsedSources, SourceKind, SourceRef

_MONTHS = {
    "jan": 1, "feb": 2, "mär": 3, "mae": 3, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12,
}
_DATE_RE = re.compile(
    r"(?P<iso>(\d{4})-(\d{2})-(\d{2}))"
    r"|(?P<dot>(\d{1,2})\.(\d{1,2})\.(\d{4}))"
    r"|(?P<txt>(\d{1,2})\.?\s*(Jan(?:uar)?|Feb(?:ruar)?|M(?:ä|ae|a)r(?:z)?|Apr(?:il)?|Mai|Jun(?:i)?"
    r"|Jul(?:i)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Okt(?:ober)?|Nov(?:ember)?|Dez(?:ember)?)\.?\s*(\d{4}))",
    re.I,
)
_VER_RE = re.compile(r"(?<![\w.])(?:v|version\s+)(\d+(?:\.\d+)*)", re.I)
_ECH_RE = re.compile(r"\beCH[-\s]?(\d{4})\b", re.I)
_I14Y_RE = re.compile(r"\bi14y\b", re.I)
_SECTION_RE = re.compile(r"(?:Abschnitt|Kapitel|Section|Ziffer)\s+\d+(?:\.\d+)*", re.I)
_PATH_RE = re.compile(
    r"(?<![\w/.\-:])((?:[\w.\-]+/)+[\w.\-]+\.(?:md|drawio|adoc|ya?ml|json|txt|puml|xml|html?))\b", re.I
)
_COMMIT_RE = re.compile(r"\bCommit\s+([0-9a-f]{7,40})\b", re.I)
_BRANCH_RE = re.compile(r"\bBranch\s+([^\s,;)]+)", re.I)
_JIRA_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_LINK_RE = re.compile(r"<ac:link\b.*?</ac:link>|<a\b[^>]*>.*?</a>", re.S | re.I)
_COMPACT_VER_RE = re.compile(r"V(\d+(?:X\d+)+)(?![\w])")
_URL_RE = re.compile(r"https?://[^\s<>\"'«»]+", re.I)
_SECTION_REF_RE = re.compile(
    r"(?:Links?\s+(?:in|aus)|siehe|vgl\.?)\s+(?:Kapitel|Abschnitt|Section|Ziffer)\s+(\d+(?:\.\d+)*)", re.I
)
_ECH_URL_RE = re.compile(r"ech-(\d{4})", re.I)
_CONNECTOR_RE = re.compile(r"^[\s,;&]*(?:(?:und|sowie|and)\s+)?(?:die\s+)?", re.I)


@dataclass
class _Link:
    kind: str                      # "confluence" | "url"
    label: str
    title: Optional[str] = None
    space_key: Optional[str] = None
    page_id: Optional[str] = None
    url: Optional[str] = None
    bare: bool = False             # URL found in plain text, not as a hyperlink


@dataclass
class _Cell:
    text: str
    links: List[_Link] = field(default_factory=list)
    unusable: List[str] = field(default_factory=list)   # links found but not usable as a source


def _attr(attrs: str, name: str) -> Optional[str]:
    m = re.search(name + r"""\s*=\s*(?:"([^"]*)"|'([^']*)')""", attrs, re.I)
    if not m:
        return None
    return unescape(m.group(1) if m.group(1) is not None else m.group(2))


def _plain(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html)).strip()


def _parse_link(block: str) -> Optional[_Link]:
    if block.lower().startswith("<ac:link"):
        page = re.search(r"<ri:page\b([^>]*)>", block, re.I)
        body = re.search(r"<ac:(?:plain-text-link-body|link-body)\b[^>]*>(.*?)</ac:(?:plain-text-link-body|link-body)>", block, re.S | re.I)
        label = _plain(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", body.group(1), flags=re.S)) if body else ""
        if page:
            title = _attr(page.group(1), "ri:content-title") or ""
            return _Link("confluence", label or title, title=title, space_key=_attr(page.group(1), "ri:space-key"))
        url = re.search(r"<ri:url\b([^>]*)>", block, re.I)
        if url:
            value = _attr(url.group(1), "ri:value") or ""
            return _Link("url", label or value, url=value)
        return None
    open_tag = re.match(r"<a\b([^>]*)>", block, re.I)
    label = _plain(block)
    href = _attr(open_tag.group(1), "href") if open_tag else None
    if not href:
        return None
    pid = re.search(r"/pages/(\d+)", href)
    if pid and "/wiki/" in href:
        return _Link("confluence", label, title=label or None, page_id=pid.group(1), url=href)
    if href.lower().startswith(("http://", "https://")):
        return _Link("url", label or href, url=href)
    return None


def _unusable_link(block: str) -> Optional[str]:
    """Why a link that _parse_link rejected matters (None for plain anchors, which carry no target)."""
    if block.lower().startswith("<ac:link"):
        att = re.search(r"<ri:attachment\b([^>]*)>", block, re.I)
        if att:
            return f"attachment '{_attr(att.group(1), 'ri:filename') or '?'}'"
        if re.search(r"<ri:user\b", block, re.I):
            return "user link"
        return "unsupported Confluence link"
    open_tag = re.match(r"<a\b([^>]*)>", block, re.I)
    href = _attr(open_tag.group(1), "href") if open_tag else None
    if not href or href.startswith("#"):
        return None
    return f"link '{_plain(block) or href}' (neither an http(s) nor a Confluence page link)"


def _to_text(html: str, sep: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", html, flags=re.S)

    def link_text(m: re.Match) -> str:
        link = _parse_link(m.group(0))
        if link is None:
            return _plain(m.group(0)) if not m.group(0).lower().startswith("<ac:") else ""
        return (link.title or link.label) if link.kind == "confluence" else link.label

    s = _LINK_RE.sub(link_text, s)
    s = re.sub(r"<time\b[^>]*datetime\s*=\s*[\"']([^\"']*)[\"'][^>]*>(?:</time>)?", r" \1 ", s, flags=re.I)
    s = re.sub(r"<ac:parameter\b[^>]*ac:name=[\"']colou?r[\"'][^>]*>.*?</ac:parameter>", "", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</(?:p|li|div|tr|td|th|h[1-6]|ac:parameter)>", sep, s, flags=re.I)
    s = unescape(re.sub(r"<[^>]+>", "", s))
    if sep == "\n":
        lines = (re.sub(r"[ \t ]+", " ", ln).strip() for ln in s.split("\n"))
        return "\n".join(ln for ln in lines if ln)
    return re.sub(r"\s+", " ", s.replace(" ", " ")).strip()


def _clean_url(url: str) -> str:
    strip = ".,;:!?'\""
    url = url.rstrip(strip)
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(strip)
    return url


def _cell(html: str) -> _Cell:
    links: List[_Link] = []
    unusable: List[str] = []
    for m in _LINK_RE.finditer(html):
        link = _parse_link(m.group(0))
        if link:
            links.append(link)
        else:
            reason = _unusable_link(m.group(0))
            if reason:
                unusable.append(reason)
    text = _to_text(html, " ")
    known = {l.url for l in links if l.url}
    for m in _URL_RE.finditer(text):
        url = _clean_url(m.group(0))
        if url and url not in known:
            known.add(url)
            links.append(_Link("url", url, url=url, bare=True))
    return _Cell(text, links, unusable)


def _context_version(text: str) -> Optional[str]:
    versions, _ = _parse_version_cell(text)
    return versions[0] if versions else None


def _ech_from_url(url: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    if not url:
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "ech.ch" or host.endswith(".ech.ch")):
        return None
    m = _ECH_URL_RE.search(parsed.path)
    if not m:
        return None
    version = None
    for seg in parsed.path.split("/"):
        vm = re.fullmatch(r"v?(\d+(?:\.\d+)+)", seg, re.I)
        if vm:
            version = vm.group(1)
    return f"eCH-{m.group(1)}", version


def _ref_from_link(link: _Link, name: Optional[str] = None) -> SourceRef:
    if link.kind == "confluence":
        return SourceRef(name=link.title or link.label, kind=SourceKind.CONFLUENCE, title=link.title,
                         page_id=link.page_id, space_key=link.space_key, url=link.url)
    ech = _ech_from_url(link.url)
    if ech:
        return SourceRef(name=ech[0], kind=SourceKind.ECH, url=link.url, baseline_version=ech[1])
    return SourceRef(name=name or link.label or link.url or "", kind=SourceKind.URL, url=link.url)


@dataclass
class _Ctx:
    html: str
    ancestor_titles: frozenset
    ancestor_ids: frozenset

    def is_ancestor(self, link: _Link) -> bool:
        if link.kind == "confluence" and link.title and link.title.strip().lower() in self.ancestor_titles:
            return True
        pid = link.page_id
        if not pid and link.url:
            m = re.search(r"/pages/(\d+)|[?&]pageId=(\d+)", link.url)
            pid = (m.group(1) or m.group(2)) if m else None
        return bool(pid and pid in self.ancestor_ids)


def _headings(html: str) -> List[Tuple[int, int, int, str]]:
    return [(m.start(), m.end(), int(m.group(1)), _plain(m.group(2)))
            for m in re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", html, re.S | re.I)]


def _section_html(html: str, number: str) -> Optional[str]:
    pat = re.compile(r"\s*" + re.escape(number) + r"\.?(?!\d|\.\d)")
    heads = _headings(html)
    for i, (_, end, level, text) in enumerate(heads):
        if pat.match(text):
            stop = len(html)
            for nxt in heads[i + 1:]:
                if nxt[2] <= level:
                    stop = nxt[0]
                    break
            return html[end:stop]
    return None


def _section_links(sec: str) -> List[Tuple[_Link, str, str]]:
    """(link, id of the table row it appears in, text of its cell/paragraph) in document order."""
    out: List[Tuple[_Link, str, str]] = []
    table_spans = _spans(sec, "table", False)
    for s, e, _ in table_spans:
        for cells in _rows(sec[s:e]):
            row_id = _cell(cells[0]).text if cells else ""
            for i, c in enumerate(cells):
                cell = _cell(c)
                for l in cell.links:
                    if not l.bare:
                        out.append((l, row_id if i > 0 else "", cell.text))
    rest = sec
    for s, e, _ in reversed(table_spans):
        rest = rest[:s] + " " + rest[e:]
    for chunk in re.split(r"</p>|</li>|<br\s*/?>|</h[1-6]>", rest, flags=re.I):
        cell = _cell(chunk)
        for l in cell.links:
            if not l.bare:
                out.append((l, "", cell.text))
    return out


def _expand_section(number: str, row_name: str, used: str, ver_text: str, ctx: _Ctx) -> Optional[List[SourceRef]]:
    sec = _section_html(ctx.html, number)
    if sec is None:
        return None
    merged: dict = {}
    for link, row_id, context in _section_links(sec):
        if ctx.is_ancestor(link):
            continue
        key = ("c", (link.title or "").lower(), link.page_id) if link.kind == "confluence" else ("u", link.url)
        if key not in merged:
            merged[key] = (_ref_from_link(link), [])
        ref, ids = merged[key]
        if row_id and row_id not in ids:
            ids.append(row_id)
        if ref.baseline_version is None and ref.kind != SourceKind.CONFLUENCE:
            ref.baseline_version = _context_version(context)
    refs = []
    for ref, ids in merged.values():
        base = f"{used} ({', '.join(ids)})" if used and ids else (used or ", ".join(ids))
        ref.used_for = f"{base} [Kapitel {number}]" if base else f"[Kapitel {number}]"
        ref.version_text = ver_text
        refs.append(ref)
    return refs or None


def _find_dates(text: str) -> List[Tuple[int, int, date]]:
    out = []
    for m in _DATE_RE.finditer(text):
        try:
            if m.group("iso"):
                d = date(int(m.group(2)), int(m.group(3)), int(m.group(4)))
            elif m.group("dot"):
                d = date(int(m.group(8)), int(m.group(7)), int(m.group(6)))
            else:
                key = m.group(11).lower()[:3]
                d = date(int(m.group(12)), _MONTHS[key], int(m.group(10)))
        except (ValueError, KeyError):
            continue
        out.append((m.start(), m.end(), d))
    return out


def _normalise_compact_versions(text: str) -> str:
    return _COMPACT_VER_RE.sub(lambda m: " v" + m.group(1).replace("X", "."), text)


def _parse_version_cell(text: str) -> Tuple[List[str], List[date]]:
    dates = _find_dates(text)
    rest = text
    for s, e, _ in reversed(dates):
        rest = rest[:s] + " " + rest[e:]
    rest = _normalise_compact_versions(rest)
    versions = [m.group(1) for m in _VER_RE.finditer(rest)]
    if not versions:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)+)\s*", rest)
        if m:
            versions = [m.group(1)]
    return versions, [d for _, _, d in dates]


def _spans(html: str, tag: str, nested: bool) -> List[Tuple[int, int, int]]:
    """Balanced <tag>...</tag> spans as (start, end, end of the opening tag). A non-greedy regex
    ends an element at the first closing tag, which truncates it at a nested element of the same
    kind (a table inside a cell) and loses every row after it. nested=False keeps only outermost."""
    tok = re.compile(rf"<(/?)(?:{tag})\b[^>]*?(/?)>", re.I)
    out: List[Tuple[int, int, int]] = []
    stack: List[Tuple[int, int]] = []
    for m in tok.finditer(html):
        if m.group(1):
            if stack:
                start, open_end = stack.pop()
                if nested or not stack:
                    out.append((start, m.end(), open_end))
        elif not m.group(2):
            stack.append((m.start(), m.end()))
    out.sort()
    return out


def _tables(html: str) -> List[Tuple[int, str]]:
    return [(s, html[s:e]) for s, e, _ in _spans(html, "table", True)]


def _inner(html: str, span: Tuple[int, int, int], close: str) -> str:
    _, end, open_end = span
    return re.sub(rf"</{close}\s*>\s*$", "", html[open_end:end], flags=re.I)


def _rows(table: str) -> List[List[str]]:
    rows = []
    for tr in _spans(table, "tr", False):
        inner = _inner(table, tr, "tr")
        rows.append([_inner(inner, c, "t[hd]") for c in _spans(inner, "t[hd]", False)])
    return rows


_ALT_SOURCE_WORDS = ("dokument", "document", "referenz", "reference", "unterlage")


def _header_texts(cells: List[str]) -> List[str]:
    """Header cell texts without nested tables, so an outer layout table is not mistaken for its inner one."""
    out = []
    for c in cells:
        for s, e, _ in reversed(_spans(c, "table", False)):
            c = c[:s] + " " + c[e:]
        out.append(_to_text(c, " ").lower())
    return out


def _is_sources_header(cells: List[str]) -> bool:
    texts = _header_texts(cells)
    if any("quelle" in t or "source" in t for t in texts):
        return True
    return any("version" in t for t in texts) and any(w in t for t in texts for w in _ALT_SOURCE_WORDS)


def _find_sources_tables(html: str, issues: Optional[List[str]] = None) -> List[Tuple[int, int, List[List[str]]]]:
    """All tables that list sources: (start, end, rows). Tables below a "Quellen" heading win over
    other tables that merely have a source-like column; with no such heading every candidate counts."""
    headings = [(m.start(), _plain(m.group(2))) for m in re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", html, re.S | re.I)]
    tables = _tables(html)
    candidates = []
    for pos, table in tables:
        rows = _rows(table)
        if rows and _is_sources_header(rows[0]):
            candidates.append((pos, pos + len(table), rows))
    if not candidates:
        if issues is not None:
            for hpos, htext in headings:
                if "quelle" not in htext.lower():
                    continue
                next_head = min((p for p, _ in headings if p > hpos), default=len(html))
                following = [(pos, tbl) for pos, tbl in tables if hpos < pos < next_head]
                if following:
                    rows = _rows(following[0][1])
                    cols = " | ".join(_header_texts(rows[0])) if rows else "(empty)"
                    issues.append(
                        f"heading '{htext}' is followed by a table whose columns ({cols}) "
                        "include no source column; its rows were not read as sources"
                    )
        return []
    under_heading = []
    for pos, end, rows in candidates:
        preceding = [t for p, t in headings if p < pos]
        if preceding and "quelle" in preceding[-1].lower():
            under_heading.append((pos, end, rows))
    return under_heading or candidates


def _column(header: List[str], word: str, default: int) -> int:
    for i, c in enumerate(header):
        if word in _to_text(c, " ").lower():
            return i
    return default


def _column_any(header: List[str], words: Tuple[str, ...], default: int) -> int:
    for i, c in enumerate(_header_texts(header)):
        if any(w in c for w in words):
            return i
    return default


def _lines(html: str) -> List[str]:
    return _to_text(html, "\n").split("\n")


_GAP_START_RE = re.compile(r"^(?:Lücken in den Quellen|Nicht gelesen)\s*:", re.I)


def _gap_notes(html: str) -> List[str]:
    notes: List[str] = []
    for line in _lines(html):
        if _GAP_START_RE.match(line) or "markdown-konvertierung" in line.lower():
            if line not in notes:
                notes.append(line)
    return notes


def find_sources_sentence(storage_html: str) -> Optional[str]:
    for line in _lines(storage_html):
        for m in re.finditer(r"\bQuellen\s*:", line):
            before = line[: m.start()].lower().rstrip()
            if before.endswith(("den", "der", "die")):
                continue
            return line[m.start():].strip()
    return None


def _first_date(text: str) -> Optional[date]:
    dates = _find_dates(text)
    return dates[0][2] if dates else None


def _excluded_reason(parts: List[Tuple[str, str]], markers: List[str]) -> Optional[str]:
    """Whole-word marker match, so "Notizen" does not exclude a source merely containing the letters;
    the reason names the column so an excluded row can be traced back to its cause."""
    for marker in markers:
        if not marker:
            continue
        pat = re.compile(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", re.I)
        for column, text in parts:
            if pat.search(text):
                return f"marked as not relevant (marker '{marker}' in column '{column}')"
    return None


_GENERIC_REPO_RE = re.compile(r"\bim\s+Repo(?:sitory)?\b|\bRepo-Spec\b|\bRepository\b", re.I)


def _named_repo(text: str, gitlab: GitLabConfig) -> Optional[GitLabRepoConfig]:
    low = text.lower()
    for repo in gitlab.repositories:
        if any(m and m.lower() in low for m in (repo.match or [repo.name])):
            return repo
    return None


def _default_repo(gitlab: GitLabConfig) -> Optional[GitLabRepoConfig]:
    default = gitlab.default_project
    if default:
        for repo in gitlab.repositories:
            if default in (repo.name, repo.project):
                return repo
    return None


def _select_repo(text: str, has_path: bool, gitlab: GitLabConfig) -> Optional[GitLabRepoConfig]:
    repo = _named_repo(text, gitlab)
    if repo is None and (_GENERIC_REPO_RE.search(text) or has_path):
        repo = _default_repo(gitlab)
    return repo


def _repo_ref(src: _Cell, ver_text: str, used: str, gitlab: Optional[GitLabConfig]) -> Optional[SourceRef]:
    if src.links:
        return None
    path_m = _PATH_RE.search(src.text)
    named = gitlab is not None and _named_repo(src.text, gitlab) is not None
    if not (path_m or named or _GENERIC_REPO_RE.search(src.text)):
        return None
    name = src.text.split("(", 1)[0].strip() or src.text
    base = dict(name=name, used_for=used, version_text=ver_text)
    if gitlab is None:
        return SourceRef(kind=SourceKind.EXTERNAL, note="repository document, GitLab connection not configured", **base)
    repo = _select_repo(src.text, path_m is not None, gitlab)
    if repo is None:
        return SourceRef(kind=SourceKind.EXTERNAL, note="repository document, no matching repository configured", **base)
    path = path_m.group(1) if path_m else None
    if path is None:
        for alias in repo.documents:
            if alias.match and alias.match.lower() in src.text.lower():
                path = alias.path
                break
    commit = _COMMIT_RE.search(src.text + " " + ver_text)
    branch = _BRANCH_RE.search(src.text + " " + ver_text)
    _, dates = _parse_version_cell(ver_text)
    when = dates[0] if dates else _first_date(src.text)
    remark = ver_text
    for s, e, _ in reversed(_find_dates(remark)):
        remark = remark[:s] + " " + remark[e:]
    remark = re.sub(r"\b(?:Stand(?:\s+vom)?|gelesen\s+am)\b", " ", remark, flags=re.I)
    remark = re.sub(r"\s+", " ", remark).strip(" ,;()-–—")
    paren = re.search(r"\(([^()]*)\)", src.text)
    hint = paren.group(1).strip() if paren and paren.group(1).strip() != path else ""
    note = "; ".join(x for x in (hint, remark) if x)
    return SourceRef(
        kind=SourceKind.REPO,
        repo_name=repo.name,
        repo_path=path,
        repo_branch=branch.group(1) if branch else None,
        repo_commit=commit.group(1).lower() if commit else None,
        baseline_date=when,
        note=note,
        **base,
    )


def _ech_refs(src: _Cell, ver_text: str, used: str) -> List[SourceRef]:
    mentions = sorted(
        [(m.start(), "ech", m) for m in _ECH_RE.finditer(src.text)]
        + [(m.start(), "i14y", m) for m in _I14Y_RE.finditer(src.text)],
        key=lambda x: x[0],
    )
    if not mentions:
        return []
    ver_versions, ver_dates = _parse_version_cell(ver_text)
    single = len(mentions) == 1
    refs = []
    for i, (start, kind, m) in enumerate(mentions):
        end = mentions[i + 1][0] if i + 1 < len(mentions) else len(src.text)
        seg = src.text[start:end]
        seg = re.sub(r"[\s,;&]*(?:(?:und|sowie|and)\s*)?$", "", seg, flags=re.I).strip()
        section = _SECTION_RE.search(seg)
        stripped = seg
        if kind == "ech":
            stripped = _ECH_RE.sub(" ", stripped)
        if section:
            stripped = stripped.replace(section.group(0), " ")
        vm = _VER_RE.search(stripped)
        version = vm.group(1) if vm else (ver_versions[0] if single and ver_versions else None)
        if kind == "ech":
            name = f"eCH-{m.group(1)}"
        else:
            name = _VER_RE.sub("", seg).strip(" ,;")
        refs.append(
            SourceRef(
                name=name,
                kind=SourceKind.ECH if kind == "ech" else SourceKind.I14Y,
                used_for=used,
                version_text=ver_text,
                section=section.group(0) if section else None,
                baseline_version=version,
                baseline_date=ver_dates[0] if len(ver_dates) == 1 else None,
            )
        )
    return refs


def _link_refs(src: _Cell, ver_text: str, used: str, issues: List[str]) -> List[SourceRef]:
    versions, dates = _parse_version_cell(ver_text)
    n = len(src.links)
    refs = [_ref_from_link(link, src.text if n == 1 and src.text else None) for link in src.links]
    for r in refs:
        r.used_for = used
        r.version_text = ver_text
    if len(versions) == n:
        for r, v in zip(refs, versions):
            r.baseline_version = v
        if len(dates) == n:
            for r, d in zip(refs, dates):
                r.baseline_date = d
        elif n == 1 and dates:
            refs[0].baseline_date = dates[0]
    elif not versions and len(dates) <= 1:
        if dates:
            for r in refs:
                r.baseline_date = dates[0]
    else:
        msg = f"could not pair {n} links with {len(versions)} versions ('{ver_text}')"
        for r in refs:
            r.parse_issue = msg
        issues.append(f"{', '.join(r.name for r in refs)}: {msg}")
    if (n == 1 and refs[0].kind != SourceKind.CONFLUENCE and refs[0].baseline_version is None
            and not refs[0].parse_issue):
        refs[0].baseline_version = _context_version(src.text)
    return refs


def _row_refs(cells: List[str], cols: Tuple[int, int, int], markers: List[str],
              gitlab: Optional[GitLabConfig], issues: List[str], ctx: _Ctx) -> List[SourceRef]:
    def get(i: int) -> str:
        return cells[i] if i < len(cells) else ""

    src = _cell(get(cols[0]))
    ver_text = _cell(get(cols[1])).text
    used = _cell(get(cols[2])).text
    if not src.text and not src.links:
        return []
    if src.unusable:
        issues.append(f"row '{src.text[:60]}': {len(src.unusable)} link(s) ignored: {'; '.join(src.unusable)}")
    if src.links:
        kept = [l for l in src.links if not ctx.is_ancestor(l)]
        if not kept:
            return []
        src.links = kept
    ver_text = "" if ver_text in ("-", "–", "—") else ver_text
    reason = _excluded_reason([("source", src.text), ("used for", used)], markers)
    if reason:
        if src.links:
            refs = [_ref_from_link(l, l.title or l.label) for l in src.links]
            for r in refs:
                r.used_for, r.version_text = used, ver_text
        else:
            refs = [SourceRef(name=src.text, kind=SourceKind.EXTERNAL, used_for=used, version_text=ver_text)]
        for r in refs:
            r.excluded_reason = reason
        return refs
    if not src.links:
        sm = _SECTION_REF_RE.search(src.text)
        if sm:
            expanded = _expand_section(sm.group(1), src.text, used, ver_text, ctx)
            if expanded is not None:
                return expanded
            msg = f"section 'Kapitel {sm.group(1)}' referenced by row '{src.text}' not found or without links"
            issues.append(msg)
            return [SourceRef(name=src.text, kind=SourceKind.EXTERNAL, used_for=used, version_text=ver_text,
                              parse_issue=msg)]
    if any(l.kind == "confluence" or _ech_from_url(l.url) for l in src.links):
        return _link_refs(src, ver_text, used, issues)
    repo = _repo_ref(src, ver_text, used, gitlab)
    if repo is not None:
        if repo.parse_issue:
            issues.append(f"{repo.name}: {repo.parse_issue}")
        return [repo]
    ech = _ech_refs(src, ver_text, used)
    if ech:
        return ech
    if src.links:
        return _link_refs(src, ver_text, used, issues)
    versions, dates = _parse_version_cell(ver_text)
    if not versions:
        cv = _context_version(src.text)
        versions = [cv] if cv else []
    return [SourceRef(name=src.text, kind=SourceKind.EXTERNAL, used_for=used, version_text=ver_text,
                      baseline_version=versions[0] if versions else None,
                      baseline_date=dates[0] if len(dates) == 1 else None)]


def parse_sources(storage_html: str, *, stand_date: Optional[date], excluded_markers: List[str],
                  gitlab: Optional[GitLabConfig] = None, ancestor_titles: Sequence[str] = (),
                  ancestor_ids: Sequence[str] = ()) -> ParsedSources:
    ctx = _Ctx(storage_html, frozenset(t.strip().lower() for t in ancestor_titles if t),
               frozenset(str(i) for i in ancestor_ids if i))
    gaps = _gap_notes(storage_html)
    issues: List[str] = []
    tables = _find_sources_tables(storage_html, issues)
    if not tables:
        return ParsedSources(found=False, gap_notes=gaps, issues=issues)
    result = ParsedSources(found=True, gap_notes=gaps)
    n_rows = 0
    for _, _, rows in tables:
        header = rows[0]
        cols = (_column_any(header, ("quelle", "source") + _ALT_SOURCE_WORDS, 0),
                _column(header, "version", 1), _column(header, "verwendet", 2))
        n_rows += len(rows) - 1
        for cells in rows[1:]:
            result.refs.extend(_row_refs(cells, cols, excluded_markers, gitlab, result.issues, ctx))
    result.refs = _dedupe_refs(result.refs, result.issues)
    if not result.refs:
        result.issues.append(
            f"sources table found ({n_rows} row(s)) but it yielded no source "
            "(rows empty, or every row links only to the spec's own ancestor pages)"
        )
    return result


def strip_sources_tables(storage_html: str) -> str:
    """The page without its Quellen table(s): what is left is what the spec says outside the source list."""
    out: List[str] = []
    pos = 0
    for start, end, _ in sorted(_find_sources_tables(storage_html)):
        out.append(storage_html[pos:start])
        pos = max(pos, end)
    out.append(storage_html[pos:])
    return "".join(out)


def body_link_refs(storage_html: str, ancestor_titles: Sequence[str] = (),
                   ancestor_ids: Sequence[str] = ()) -> List[SourceRef]:
    """Every Confluence page link and URL (hyperlink or bare) in the given storage HTML as a SourceRef,
    except links to the spec's own ancestor pages. Links to the same target are returned once."""
    ctx = _Ctx(storage_html, frozenset(t.strip().lower() for t in ancestor_titles if t),
               frozenset(str(i) for i in ancestor_ids if i))
    refs: List[SourceRef] = []
    seen = set()
    for link in _cell(storage_html).links:
        if ctx.is_ancestor(link):
            continue
        ref = _ref_from_link(link)
        key = (ref.kind, (ref.title or "").lower(), ref.page_id, (ref.url or "").rstrip("/").lower())
        if key not in seen:
            seen.add(key)
            refs.append(ref)
    return refs


def _ref_key(ref: SourceRef):
    if ref.excluded_reason or ref.parse_issue:
        return None
    if ref.kind == SourceKind.CONFLUENCE and (ref.title or ref.page_id):
        return ("c", (ref.title or "").lower(), ref.page_id)
    if ref.kind in (SourceKind.URL, SourceKind.ECH) and ref.url:
        return ("u", ref.url.rstrip("/").lower())
    return None


def _dedupe_refs(refs: List[SourceRef], issues: Optional[List[str]] = None) -> List[SourceRef]:
    """Same target listed in several places (explicit row and an expanded section) -> one ref;
    the explicit row wins, usage texts are merged. Two explicit rows with different stated
    versions cannot both be honoured: one baseline is dropped, so that is reported."""
    out: List[SourceRef] = []
    index: dict = {}
    for ref in refs:
        key = _ref_key(ref)
        if key is None or key not in index:
            if key is not None:
                index[key] = len(out)
            out.append(ref)
            continue
        kept = out[index[key]]
        expanded = "[Kapitel" in ref.used_for
        kept_expanded = "[Kapitel" in kept.used_for
        winner, other = (ref, kept) if (kept_expanded and not expanded) else (kept, ref)
        if issues is not None and not expanded and not kept_expanded:
            differs = (
                (kept.baseline_version and ref.baseline_version and kept.baseline_version != ref.baseline_version)
                or (kept.baseline_date and ref.baseline_date and kept.baseline_date != ref.baseline_date)
            )
            if differs:
                issues.append(
                    f"{kept.name}: listed more than once with different versions "
                    f"('{kept.version_text}' vs '{ref.version_text}'); only '{kept.version_text}' is used as baseline"
                )
        if not winner.baseline_version and other.baseline_version:
            winner.baseline_version = other.baseline_version
        if other.used_for and other.used_for not in winner.used_for:
            winner.used_for = f"{winner.used_for} / {other.used_for}" if winner.used_for else other.used_for
        out[index[key]] = winner
    return out


def _macro_bodies(html: str, name: str) -> List[str]:
    tok = re.compile(r"<ac:structured-macro\b([^>]*)>|</ac:structured-macro>", re.I)
    bodies: List[str] = []
    stack: List[Tuple[bool, int]] = []
    for m in tok.finditer(html):
        if m.group(0).startswith("</"):
            if stack:
                wanted, start = stack.pop()
                if wanted:
                    bodies.append(html[start:m.start()])
        else:
            attrs = m.group(1)
            stack.append((_attr(attrs, "ac:name") == name and not attrs.rstrip().endswith("/"), m.end()))
    return bodies


def parse_page_properties(storage_html: str) -> PageProperties:
    props = PageProperties()
    details = _macro_bodies(storage_html, "details")
    scope = "".join(details) if details else storage_html
    for _, table in _tables(scope):
        for cells in _rows(table):
            if len(cells) < 2:
                continue
            key = _to_text(cells[0], " ").lower()
            value = cells[1]
            if key.startswith("status") and props.status == "unknown":
                title = re.search(r"<ac:parameter\b[^>]*ac:name=[\"']title[\"'][^>]*>(.*?)</ac:parameter>", value, re.S | re.I)
                text = _plain(title.group(1)) if title else _to_text(value, " ")
                if text:
                    props.status = text
            elif key.startswith("jira"):
                for k in _JIRA_RE.findall(_to_text(value, " ")):
                    if k not in props.jira:
                        props.jira.append(k)
    outside = storage_html
    for start, end, _ in reversed(_find_sources_tables(storage_html)):
        outside = outside[:start] + " " + outside[end:]
    for line in _lines(outside):
        m = re.search(r"\bStand\s*:(.*)", line)
        if m:
            d = _first_date(m.group(1))
            if d:
                props.stand_date = d
                break
    return props
