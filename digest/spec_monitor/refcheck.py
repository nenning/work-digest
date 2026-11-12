"""`--spec-monitor --check-references`: finds references a spec's text points to (ADRs, eCH standards,
Confluence pages, repository documents, links) that its Quellen table does not list.

Independent of source changes: it reads each spec as it is today and compares what the text mentions with
what the Quellen table lists. Two passes: deterministic patterns (ADR / eCH numbers, page links, URLs)
and an LLM reading of the text for everything else. Reads and writes no state."""
import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from digest import summarizer
from digest.auth.atlassian import get_auth_header
from digest.config import Config, LLMConfig, spec_llm
from digest.spec_monitor import analysis
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.cache import SpecCache, hash_key
from digest.spec_monitor.confluence_client import ConfluenceClient
from digest.spec_monitor.discovery import discover_specs
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import (
    MissingReference, RefCheckReport, RefCheckResult, ReportError, SourceKind, SourceRef, SpecInfo,
)
from digest.spec_monitor.parser import body_link_refs, parse_sources, strip_sources_tables
from digest.spec_monitor.runner import SpecMonitorFatal

if TYPE_CHECKING:
    from digest.spec_monitor.refcheck_links import PageStore

log = logging.getLogger(__name__)

_PROMPT_VERSION = "2"
_MAX_TOKENS = 4000
_MENTION_MAX = 300
_PURPOSE = "reference_check"
_KINDS = {"confluence", "ech", "adr", "url", "repo", "standard", "other"}

_ADR_RE = re.compile(r"\bADR[-\s]?(\d{1,4})\b", re.I)
_ECH_RE = re.compile(r"\beCH[-\s]?(\d{4})\b", re.I)
_PAGE_ID_RE = re.compile(r"/pages/(\d+)|[?&]pageId=(\d+)")
_TINY_RE = re.compile(r"/wiki/x/([A-Za-z0-9_-]+)")
_LINK_MARK_RE = re.compile(r"\[->([^\]]*)\]")
_JIRA_URL_RE = re.compile(r"/browse/[A-Z][A-Z0-9]+-\d+", re.I)

_DATA_NOTE = analysis._DATA_NOTE


def _norm(text: Optional[str]) -> str:
    return re.sub(r"[\W_]+", "", (text or "").casefold())


def _url_key(url: Optional[str]) -> str:
    return (url or "").strip().rstrip("/").casefold()


def _tiny_page_id(code: str) -> Optional[str]:
    """Confluence short link code (/wiki/x/AgDuDw) = url-safe base64 of the little-endian page id."""
    try:
        raw = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
    except ValueError:
        return None
    return str(int.from_bytes(raw, "little")) if raw else None


def _page_id(url: Optional[str]) -> Optional[str]:
    m = _PAGE_ID_RE.search(url or "")
    if m:
        return m.group(1) or m.group(2)
    t = _TINY_RE.search(url or "")
    return _tiny_page_id(t.group(1)) if t else None


def _readable(text: str, titles: Dict[str, str]) -> str:
    """The text as the page shows it: drops the '[->target]' link markers of the text rendering (the label
    stays; a link without label keeps its target) and writes resolved Confluence links as the page title."""
    def drop(m: "re.Match") -> str:
        labelled = m.start() > 0 and not text[m.start() - 1].isspace()
        return "" if labelled else m.group(1)

    out = _LINK_MARK_RE.sub(drop, text)
    for url, title in titles.items():
        out = out.replace(url, title)
    return re.sub(r"\s+", " ", out).strip()


_TITLE_PREFIX_RE = re.compile(r"^(?:confluence[-\s]?)?(?:seite|page)\s+", re.I)


def _title_candidates(reference: str) -> List[str]:
    """A model-found page is often written 'Seite Foo' for the page 'Foo': try both."""
    stripped = _TITLE_PREFIX_RE.sub("", reference).strip()
    return [reference] + ([stripped] if stripped and stripped != reference else [])


def _resolve_titles(findings: List[MissingReference], listed_ids: Set[str], client: ConfluenceClient,
                    atlassian_url: str, result: RefCheckResult) -> Dict[str, str]:
    """Gives Confluence findings their page title and a link: a short / page-id link of this instance is
    named by the title of its page, a page named without link is looked up by title. A page that turns out
    to be listed under that title is dropped. Returns url -> title."""
    host = urlparse(str(atlassian_url)).netloc.casefold()
    titles: Dict[str, str] = {}
    for f in list(findings):
        if f.kind == "confluence" and not f.url:
            found = None
            try:
                for name in _title_candidates(f.reference):
                    found = client.find_page_id(name)
                    if found:
                        break
            except (LookupError, RequestFailed) as exc:
                result.warnings.append(f"Seite '{f.reference}' konnte nicht in Confluence gesucht werden: {exc}")
                continue
            if not found:
                continue
            f.url = client.page_url(found)
        pid = _page_id(f.url)
        if not f.url or not pid or urlparse(f.url).netloc.casefold() != host:
            continue
        try:
            title = str(client.get_page(pid, with_body=False).get("title") or "").strip()
        except (LookupError, RequestFailed) as exc:
            result.warnings.append(f"Link konnte keiner Confluence-Seite zugeordnet werden: {f.url} ({exc})")
            continue
        if not title:
            continue
        if _identifiers(title, title, pid, None) & listed_ids:
            findings.remove(f)
            continue
        titles[f.url] = title
        f.reference, f.kind = title, "confluence"
    return titles


def _doc_tokens(*texts: Optional[str]) -> Set[str]:
    ids: Set[str] = set()
    for text in texts:
        for m in _ADR_RE.finditer(text or ""):
            ids.add(f"adr:{int(m.group(1))}")
        for m in _ECH_RE.finditer(text or ""):
            ids.add(f"ech:{m.group(1)}")
    return ids


def _identifiers(name: Optional[str], title: Optional[str], page_id: Optional[str], url: Optional[str],
                 path: Optional[str] = None, note: Optional[str] = None) -> Set[str]:
    """Every way one document can be named: normalised names, page id, URL, ADR / eCH number."""
    ids: Set[str] = set()
    for value in (name, title, path):
        if _norm(value):
            ids.add("n:" + _norm(value))
    pid = page_id or _page_id(url)
    if pid:
        ids.add("p:" + str(pid))
    if url:
        ids.add("u:" + _url_key(url))
    ids |= _doc_tokens(name, title, url, note, path)
    for value in (name, title, path):   # a row stating a path ("docs/adr/0028-auth.md") need not be a repo ref
        if value and "adr" in value.lower():
            m = re.match(r"(\d{1,4})\b", re.split(r"[\\/]", value.strip())[-1])
            if m:
                ids.add(f"adr:{int(m.group(1))}")
    return ids


def _listed_ids(listed: List[SourceRef]) -> Set[str]:
    ids: Set[str] = set()
    for ref in listed:
        ids |= _identifiers(ref.name, ref.title, ref.page_id, ref.url, ref.repo_path, ref.note)
    return ids


def _listed_names(listed: List[SourceRef]) -> List[str]:
    return [n for n in (_norm(v) for ref in listed for v in (ref.name, ref.title, ref.repo_path)) if n]


def _loosely_listed(reference: str, names: List[str]) -> bool:
    """A model-found reference whose name contains / is contained in a listed source name ('API-Konzept
    v2' vs 'API-Konzept'); too short names never match this way."""
    n = _norm(reference)
    return len(n) >= 4 and any(len(s) >= 4 and (n in s or s in n) for s in names)


def _sections(storage: str) -> List[Tuple[str, List[str]]]:
    return [(heading, [ln.strip() for ln in text.splitlines() if ln.strip()])
            for heading, text in analysis._spec_sections(storage)]


def _find_mention(sections: List[Tuple[str, List[str]]], needles: List[str]) -> Tuple[str, str]:
    """(section heading, line) of the first line that contains one of the needles."""
    wanted = [n.casefold() for n in needles if n and len(n) >= 3]
    for heading, lines in sections:
        for line in lines:
            low = line.casefold()
            if any(n in low for n in wanted):
                return heading, line[:_MENTION_MAX]
    return "", ""


def _pattern_findings(spec: SpecInfo, stripped: str, sections: List[Tuple[str, List[str]]],
                      listed_ids: Set[str]) -> List[MissingReference]:
    found: Dict[str, MissingReference] = {}

    def add(key: str, ids: Set[str], ref: MissingReference) -> None:
        if ids & listed_ids or key in found:
            return
        found[key] = ref

    for heading, lines in sections:
        for line in lines:
            for m in _ADR_RE.finditer(line):
                n = int(m.group(1))
                add(f"adr:{n}", {f"adr:{n}"}, MissingReference(
                    reference=f"ADR-{m.group(1)}", kind="adr", mention=line[:_MENTION_MAX], section=heading))
            for m in _ECH_RE.finditer(line):
                add(f"ech:{m.group(1)}", {f"ech:{m.group(1)}"}, MissingReference(
                    reference=f"eCH-{m.group(1)}", kind="ech", mention=line[:_MENTION_MAX], section=heading))

    for ref in body_link_refs(stripped, spec.ancestor_titles, spec.ancestor_ids):
        pid = ref.page_id or _page_id(ref.url)
        if pid and (pid == spec.page_id or pid in spec.ancestor_ids):
            continue
        if ref.kind == SourceKind.CONFLUENCE:
            if (ref.title or "").casefold() == spec.title.casefold():
                continue
            kind, label = "confluence", ref.title or ref.name
        elif ref.kind == SourceKind.ECH:
            kind, label = "ech", ref.name
        else:
            if not ref.url or _JIRA_URL_RE.search(ref.url):
                continue
            kind, label = "url", ref.name or ref.url
        ids = _identifiers(ref.name, ref.title, ref.page_id, ref.url)
        doc_tokens = sorted(i for i in ids if i.startswith(("ech:", "adr:")))
        if pid:
            key = "p:" + str(pid)
        elif doc_tokens:
            key = doc_tokens[0]
        elif kind == "url":
            key = "u:" + _url_key(ref.url)
        else:
            key = "n:" + _norm(label)
        heading, mention = _find_mention(sections, [ref.title or "", ref.name, ref.url or ""])
        add(key, ids, MissingReference(
            reference=label, kind=kind, mention=mention, section=heading, url=ref.url))
    return list(found.values())


def _listed_block(listed: List[SourceRef]) -> str:
    rows = []
    for ref in listed:
        extra = " | ".join(p for p in (ref.url, ref.repo_path, ref.note) if p)
        rows.append(f"- {ref.name}" + (f" ({extra})" if extra else ""))
    return "\n".join(rows) or "(the specification lists no sources)"


def _build_prompt(spec: SpecInfo, listed: List[SourceRef], gap_notes: List[str], already: List[MissingReference],
                  text: str, language: str, part: Optional[Tuple[int, int]]) -> str:
    lang = summarizer._language_name(language)
    part_note = (f"The text below is part {part[0]} of {part[1]} (consecutive); report only what this part mentions.\n"
                 if part else "")
    already_block = "\n".join(f"- {m.reference}" for m in already) or "(none)"
    gaps = "\n".join(f"- {g}" for g in gap_notes) or "(none)"
    return (
        f"The detail specification '{spec.title}' lists the documents it is based on in a sources table. "
        "Find references in its text to documents the specification evidently relies on that are NOT in that table: "
        "architecture decision records, standards (e.g. eCH), other specifications or concept pages, API "
        "descriptions, repository documents, external documents.\n"
        "Report a reference only when the text points to a concrete, identifiable document. Do NOT report:\n"
        "- anything already in the sources table, also under another name, abbreviation or spelling\n"
        "- the specification's own requirement or decision IDs (e.g. CM-FR-03, CM-D-11) and sections of the spec itself\n"
        "- Jira tickets, people, teams, products, or generic words without a concrete document behind them\n"
        "- the references listed under 'already found'\n"
        "- references the specification explicitly declares as known gaps\n"
        f"{part_note}"
        f"Respond in {lang}; keep document names exactly as written. For a Confluence page give only its page "
        "title as 'reference', without a word like 'Seite' or 'page' in front.\n"
        f"{_DATA_NOTE}\n\n"
        + analysis._quote("sources table of the specification", _listed_block(listed))
        + "\n\n" + analysis._quote("known gaps stated by the specification", gaps)
        + "\n\n" + analysis._quote("already found", already_block)
        + "\n\n" + analysis._quote("specification text (without the sources table)", text or "(empty)")
        + '\n\nRespond with JSON only: {"missing": [{"reference": "document name as written", "kind": '
        '"confluence"|"ech"|"adr"|"repo"|"url"|"standard"|"other", "mention": "the sentence that mentions it '
        '(verbatim, at most 300 characters)", "section": "heading of the section or empty", "why": "one short '
        'phrase on what the document is used for; never say that it is missing from the table"}]}. Use {"missing": []} when nothing is missing.'
    )


def _parse_missing(data) -> List[MissingReference]:
    items = data.get("missing") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("missing list not found")
    out: List[MissingReference] = []
    for it in items:
        if not isinstance(it, dict) or not str(it.get("reference") or "").strip():
            raise ValueError("entry without reference")
        kind = str(it.get("kind") or "other").lower()
        out.append(MissingReference(
            reference=str(it["reference"]).strip(),
            kind=kind if kind in _KINDS else "other",
            mention=str(it.get("mention") or "").strip()[:_MENTION_MAX],
            section=str(it.get("section") or "").strip(),
            origin="llm",
            why=str(it.get("why") or "").strip(),
        ))
    return out


def _ask(prompt: str, llm: LLMConfig, cache: SpecCache) -> List[MissingReference]:
    key = hash_key(_PROMPT_VERSION, analysis._model_key(llm), prompt)
    cached = cache.get_text("llm_refcheck", key)
    if cached is not None:
        try:
            return _parse_missing(json.loads(cached))
        except (ValueError, TypeError):
            pass
    found, raw = analysis._ask_json(prompt, llm, llm.llm_timeout, _MAX_TOKENS, _parse_missing, _PURPOSE)
    cache.put_text("llm_refcheck", key, raw)
    return found


def _llm_findings(spec: SpecInfo, stripped: str, listed: List[SourceRef], gap_notes: List[str],
                  pattern_found: List[MissingReference], listed_ids: Set[str], llm: LLMConfig, language: str,
                  cache: SpecCache) -> List[MissingReference]:
    parts = analysis._spec_text_parts(replace(spec, body_storage=stripped))
    names = _listed_names(listed)
    known = {_norm(m.reference) for m in pattern_found} | {
        i for m in pattern_found for i in _doc_tokens(m.reference)}
    out: List[MissingReference] = []
    for n, text in enumerate(parts, 1):
        part = (n, len(parts)) if len(parts) > 1 else None
        for ref in _ask(_build_prompt(spec, listed, gap_notes, pattern_found, text, language, part), llm, cache):
            ids = _identifiers(ref.reference, None, None, None) | _doc_tokens(ref.reference, ref.mention)
            if ids & listed_ids or _loosely_listed(ref.reference, names):
                continue
            if _norm(ref.reference) in known or (ids & known):
                continue
            known.add(_norm(ref.reference))
            known |= {i for i in ids if i.startswith(("adr:", "ech:"))}
            out.append(ref)
    return out


def check_spec(spec: SpecInfo, config: Config, cache: SpecCache, llm: LLMConfig,
               client: Optional[ConfluenceClient] = None, store: Optional["PageStore"] = None) -> RefCheckResult:
    sm = config.spec_monitor
    result = RefCheckResult(spec=spec)
    if not spec.body_storage:
        result.errors.append(ReportError(
            scope=spec.title, message="page body is empty", impact="this spec was not checked"))
        return result

    parsed = parse_sources(
        spec.body_storage, stand_date=spec.stand_date, excluded_markers=sm.excluded_source_markers,
        gitlab=config.gitlab, ancestor_titles=spec.ancestor_titles, ancestor_ids=spec.ancestor_ids,
    )
    listed = parsed.refs
    result.listed_count = len(listed)
    if not parsed.found:
        result.warnings.append(
            "Keine Quellentabelle gefunden: jede gefundene Referenz gilt als nicht gelistet")
    result.warnings.extend(f"Quellenzeile nicht auswertbar, Gelistetes evtl. unvollständig: {i}"
                           for i in parsed.issues)
    listed_ids = _listed_ids(listed)

    stripped = strip_sources_tables(spec.body_storage)
    sections = _sections(stripped)
    pattern_found = _pattern_findings(spec, stripped, sections, listed_ids)
    llm_found: List[MissingReference] = []
    try:
        llm_found = _llm_findings(spec, stripped, listed, parsed.gap_notes, pattern_found, listed_ids, llm,
                                  config.language, cache)
    except AnalysisError as exc:
        result.llm_incomplete = True
        result.errors.append(ReportError(
            scope=spec.title, message=f"reference extraction by the LLM failed: {exc}",
            impact="only ADR / eCH numbers and links are checked; other mentioned documents may be missing"))
    result.missing = pattern_found + llm_found
    titles: Dict[str, str] = {}
    if client is not None:
        titles = _resolve_titles(result.missing, listed_ids, client, config.atlassian.url, result)
    for m in result.missing:
        m.mention = _readable(m.mention, titles)

    if client is not None and sm.link_depth > 0:
        from digest.spec_monitor import refcheck_links   # imports this module, so not at the top
        known = set(listed_ids)
        for m in result.missing:
            known |= _identifiers(m.reference, None, _page_id(m.url), m.url)
        refcheck_links.suggest_sources(result, spec, listed, known, store or refcheck_links.PageStore(client),
                                       cache, llm, config)
    return result


def run_reference_check(
    config: Config,
    *,
    only: Optional[str] = None,
    command_line: str = "",
    progress: Callable[[str], None] = print,
    client: Optional[ConfluenceClient] = None,
    cache_dir: Optional[Path] = None,
) -> RefCheckReport:
    """Checks every spec below the parent page, whatever its status (the status filter only exists to save
    LLM cost in the source analysis). Raises SpecMonitorFatal when the specs cannot be discovered."""
    sm = config.spec_monitor
    if sm is None:
        raise SpecMonitorFatal("No spec_monitor block configured in config.yaml")
    sm.analyse_all_states = True   # bodies of every spec are needed

    cache = SpecCache(cache_dir or (config.data_dir / "spec_cache"))
    if client is None:
        client = ConfluenceClient(config.atlassian, get_auth_header(config.atlassian), sm.retry, cache)
    try:
        discovery = discover_specs(client, sm, only)
    except (RequestFailed, LookupError, ValueError) as exc:
        raise SpecMonitorFatal(f"Specs could not be discovered below page {sm.parent_page_id}: {exc}") from exc

    report = RefCheckReport(
        generated_at=datetime.now().astimezone(),
        excluded_pages=list(discovery.excluded),
        errors=[
            ReportError(scope=title, message=f"page could not be read: {reason}",
                        impact="this page was not checked; it may be a spec")
            for _pid, title, reason in discovery.unreadable
        # the unreadable-status problem is about the status filter, which this mode does not use
        ] + [e for e in discovery.problems if "status could not be read" not in e.message],
        command_line=command_line,
        language=config.language,
    )
    progress(f"{len(discovery.specs)} spec(s) found, {len(discovery.excluded)} page(s) excluded")

    llm = spec_llm(config)
    from digest.spec_monitor.refcheck_links import PageStore
    store = PageStore(client)

    def run_one(spec: SpecInfo) -> RefCheckResult:
        try:
            result = check_spec(spec, config, cache, llm, client, store)
        except Exception as exc:
            log.exception("reference check of %s failed", spec.title)
            return RefCheckResult(spec=spec, errors=[ReportError(
                scope=spec.title, message=f"{type(exc).__name__}: {exc}", impact="this spec was not checked")])
        progress(f"  {spec.title}: {len(result.missing)} possibly missing reference(s), "
                 f"{len(result.suggested)} suggested source page(s)")
        return result

    if sm.workers > 1 and len(discovery.specs) > 1:
        with ThreadPoolExecutor(max_workers=sm.workers) as pool:
            report.results = list(pool.map(run_one, discovery.specs))
    else:
        report.results = [run_one(s) for s in discovery.specs]

    report.cache_hits = cache.hits
    report.cache_misses = cache.misses
    return report
