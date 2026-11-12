"""Link crawl of `--check-references`: follows the links of the Confluence pages a spec lists in its Quellen
table (depth 1; depth 2 also follows the links of those pages) and suggests linked pages the spec should list
as sources too.

A linked page is only suggested after a feasibility check: pages without text are dropped, the rest is judged by
the LLM against the spec text and its listed sources (does the spec evidently build on that page, or is it
navigation, a protocol, an unrelated topic?). Judged-away pages are kept in the result with the reason, so the
report shows what was looked at. Pages that are listed, the spec itself, its ancestors and pages the text pass
already reported are never candidates."""
import json
import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from digest import summarizer
from digest.config import Config, LLMConfig
from digest.spec_monitor import analysis, refcheck
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.cache import SpecCache, hash_key
from digest.spec_monitor.confluence_client import ConfluenceClient, _storage_value
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import (
    RefCheckResult, RejectedLink, ReportError, SourceKind, SourceRef, SpecInfo, SuggestedSource,
)
from digest.spec_monitor.parser import body_link_refs, strip_sources_tables
from digest.spec_monitor.textdiff import storage_to_text

log = logging.getLogger(__name__)

_PROMPT_VERSION = "1"
_PURPOSE = "reference_links"
_MAX_TOKENS = 2000
_BATCH = 8                    # candidates per model call
_MIN_TEXT_CHARS = 100         # a page with less text carries nothing to judge
_EXCERPT_CHARS = 1500         # per candidate page in the prompt
_SPEC_CHARS = 12000           # spec text in the prompt
_CONFIDENCE = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Page:
    id: str
    title: str
    url: str
    storage: str
    text: str                 # readable plain text: no link markers, one line


class PageStore:
    """Pages and title lookups of one run, shared by all specs (the same concept page is often linked from
    several of them). Failures are not cached."""

    def __init__(self, client: ConfluenceClient):
        self.client = client
        self._pages: Dict[str, Page] = {}
        self._titles: Dict[Tuple[str, Optional[str]], Optional[str]] = {}
        self._lock = threading.Lock()

    def get(self, page_id: str) -> Page:
        """Raises LookupError (not found / no body) or RequestFailed."""
        with self._lock:
            cached = self._pages.get(page_id)
        if cached:
            return cached
        data = self.client.get_page(page_id)
        storage = _storage_value(data, page_id)
        page = Page(id=page_id, title=str(data.get("title") or "").strip() or page_id,
                    url=self.client.page_url(page_id), storage=storage,
                    text=refcheck._readable(storage_to_text(storage), {}))
        with self._lock:
            self._pages[page_id] = page
        return page

    def find(self, title: str, space_key: Optional[str]) -> Optional[str]:
        """Page id for a title, None when there is no such page. Raises LookupError (ambiguous) or RequestFailed."""
        key = (title.casefold(), space_key)
        with self._lock:
            if key in self._titles:
                return self._titles[key]
        found = self.client.find_page_id(title, space_key)
        with self._lock:
            self._titles[key] = found
        return found


@dataclass
class _Cand:
    page_id: str
    hint: str                 # title / label as written in the link
    mention: str
    chains: List[str]


def _sources_block(listed: List[SourceRef]) -> str:
    rows = [f"- {r.name}" + (f" — {r.used_for}" if r.used_for else "") for r in listed if not r.excluded_reason]
    return "\n".join(rows) or "(the specification lists no sources)"


def _spec_digest(stripped: str) -> str:
    text = "\n\n".join(f"## {h}\n{x}" if h else x for h, x in analysis._spec_sections(stripped))
    if len(text) > _SPEC_CHARS:
        return text[:_SPEC_CHARS] + "\n[specification text truncated]"
    return text


def _candidate_block(c: _Cand, page: Page) -> str:
    where = f"\nlinked from: {'; '.join(c.chains)}"
    if c.mention:
        where += f"\nsentence with the link: {c.mention}"
    return f"### [{page.id}] {page.title}{where}\npage text (start): {page.text[:_EXCERPT_CHARS]}"


def _build_prompt(spec: SpecInfo, listed: List[SourceRef], spec_text: str,
                  cands: List[Tuple[_Cand, Page]], language: str) -> str:
    lang = summarizer._language_name(language)
    return (
        f"The detail specification '{spec.title}' lists the documents it is based on in a sources table. "
        "The Confluence pages below are linked from pages listed there (directly, or via further linked pages) "
        "but are not in the table themselves. For each one decide whether the specification should list it as a "
        "source: the specification evidently builds on its content (rules, interfaces, data model, decisions, "
        "concepts that a reader needs to understand or implement the specification).\n"
        "Not a source: navigation / overview / index pages, meeting notes, protocols, planning or status pages, "
        "pages about another topic, pages that only mention something in passing, outdated or duplicate "
        "versions of a listed source.\n"
        f"Respond in {lang}; keep document names exactly as written.\n"
        f"{analysis._DATA_NOTE}\n\n"
        + analysis._quote("sources table of the specification (name — used for)", _sources_block(listed))
        + "\n\n" + analysis._quote("specification text (without the sources table)", spec_text or "(empty)")
        + "\n\n" + analysis._quote("linked pages", "\n\n".join(_candidate_block(c, p) for c, p in cands))
        + '\n\nRespond with JSON only, one entry for EVERY linked page: {"assessments": [{"id": "page id from '
        'the [..] in its heading", "relevant": true|false, "confidence": "high"|"medium"|"low", "reason": "one '
        'short phrase: what the specification would use the page for, or why it is not a source"}]}.'
    )


def _parse_assessments(data, ids: List[str]) -> Dict[str, Tuple[bool, str, str]]:
    items = data.get("assessments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("assessments list not found")
    by_id: Dict[str, Tuple[bool, str, str]] = {}
    for it in items:
        if not isinstance(it, dict) or it.get("id") is None:
            raise ValueError("entry without id")
        confidence = str(it.get("confidence") or "").strip().lower()
        by_id[str(it["id"]).strip()] = (
            analysis._truthy(it.get("relevant")),
            confidence if confidence in _CONFIDENCE else "low",
            str(it.get("reason") or "").strip(),
        )
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise ValueError(f"no verdict for page(s) {', '.join(missing)}")
    return {i: by_id[i] for i in ids}


def _assess(prompt: str, llm: LLMConfig, cache: SpecCache, ids: List[str]) -> Dict[str, Tuple[bool, str, str]]:
    key = hash_key(_PROMPT_VERSION, analysis._model_key(llm), prompt)
    cached = cache.get_text("llm_reflinks", key)
    if cached is not None:
        try:
            return _parse_assessments(json.loads(cached), ids)
        except (ValueError, TypeError):
            pass
    parsed, raw = analysis._ask_json(prompt, llm, llm.llm_timeout, _MAX_TOKENS,
                                     lambda d: _parse_assessments(d, ids), _PURPOSE)
    cache.put_text("llm_reflinks", key, raw)
    return parsed


class _Crawl:
    def __init__(self, result: RefCheckResult, spec: SpecInfo, store: PageStore, host: str, max_pages: int):
        self.result = result
        self.spec = spec
        self.store = store
        self.host = host
        self.max_pages = max_pages
        self.read: Set[str] = set()
        self.skipped_for_budget = 0

    def warn(self, message: str) -> None:
        self.result.warnings.append(message)

    def fail(self, message: str) -> None:
        self.result.link_incomplete = True
        self.result.errors.append(ReportError(
            scope=self.spec.title, message=message,
            impact="linked pages were not (all) read, so source suggestions may be incomplete"))

    def lookup(self, title: str, space_key: Optional[str], what: str, not_found: str = "") -> Optional[str]:
        try:
            found = self.store.find(title, space_key)
            if not found and not_found:
                self.warn(not_found)
            return found
        except LookupError as exc:
            self.warn(f"{what} '{title}' konnte nicht gefunden werden: {exc}")
        except RequestFailed as exc:
            self.fail(f"{what} '{title}' could not be looked up: {exc}")
        return None

    def read_page(self, page_id: str, label: str, what: str) -> Optional[Page]:
        if page_id not in self.read and len(self.read) >= self.max_pages:
            self.skipped_for_budget += 1
            return None
        try:
            page = self.store.get(page_id)
        except LookupError as exc:
            self.warn(f"{what} '{label}' ({page_id}) nicht lesbar: {exc}")
            return None
        except RequestFailed as exc:
            self.fail(f"{what} '{label}' ({page_id}) could not be read: {exc}")
            return None
        self.read.add(page_id)
        return page

    def _own_instance(self, url: str) -> bool:
        """Only Confluence of this instance is followed: git, other hosts, schemeless hosts, mailto: are not."""
        parsed = urlparse(url.strip())
        if parsed.scheme and parsed.scheme.lower() not in ("http", "https"):
            return False
        if parsed.netloc:
            return parsed.netloc.casefold() == self.host
        return url.strip().startswith("/")

    def links(self, page: Page) -> List[Tuple[str, _Cand]]:
        """Confluence pages of this instance that the page links to: (page id, candidate)."""
        sections = refcheck._sections(page.storage)
        out: List[Tuple[str, _Cand]] = []
        for ref in body_link_refs(page.storage, self.spec.ancestor_titles, self.spec.ancestor_ids):
            if ref.url and not self._own_instance(ref.url):
                continue
            pid = None
            if ref.kind == SourceKind.CONFLUENCE:
                pid = ref.page_id or refcheck._page_id(ref.url)
                if not pid and ref.title:
                    pid = self.lookup(ref.title, ref.space_key, "Verlinkte Seite")
            elif ref.kind == SourceKind.URL:
                pid = refcheck._page_id(ref.url)
            if not pid:
                continue
            hint = ref.title or ref.name
            _, mention = refcheck._find_mention(sections, [ref.title or "", ref.name])
            out.append((pid, _Cand(pid, hint, refcheck._readable(mention, {}), [])))
        return out


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def suggest_sources(result: RefCheckResult, spec: SpecInfo, listed: List[SourceRef], excluded_ids: Set[str],
                    store: PageStore, cache: SpecCache, llm: LLMConfig, config: Config) -> None:
    """Fills result.suggested / link_rejected / link_checked and its warnings and errors."""
    sm = config.spec_monitor
    result.link_depth = sm.link_depth
    if sm.link_depth < 1:
        return
    crawl = _Crawl(result, spec, store, urlparse(str(config.atlassian.url)).netloc.casefold(), sm.link_max_pages)
    excluded = set(excluded_ids) | {"p:" + spec.page_id, "n:" + refcheck._norm(spec.title)}
    excluded |= {"p:" + str(i) for i in spec.ancestor_ids} | {"n:" + refcheck._norm(t) for t in spec.ancestor_titles}

    visited: Set[str] = {spec.page_id, *map(str, spec.ancestor_ids)}
    level: List[Tuple[Page, str]] = []
    for ref in listed:
        if ref.kind != SourceKind.CONFLUENCE or ref.excluded_reason or ref.parse_issue:
            continue
        name = ref.title or ref.name
        pid = ref.page_id or refcheck._page_id(ref.url)
        if not pid and ref.title:
            pid = crawl.lookup(ref.title, ref.space_key, "Quelle",
                               f"Quelle '{name}' wurde in Confluence nicht gefunden, ihre Links wurden nicht verfolgt")
        if not pid or pid in visited:
            continue
        visited.add(pid)
        page = crawl.read_page(pid, name, "Quelle")
        if page:
            level.append((page, page.title))

    spec_text = ""
    for depth in range(1, sm.link_depth + 1):
        cands: Dict[str, _Cand] = {}
        for page, chain in level:
            for pid, cand in crawl.links(page):
                if pid in visited:
                    continue
                known = cands.setdefault(pid, cand)
                if chain not in known.chains:
                    known.chains.append(chain)
        if not cands:
            break

        judged: List[Tuple[_Cand, Page]] = []
        next_level: List[Tuple[Page, str]] = []
        for pid, cand in cands.items():
            visited.add(pid)
            if refcheck._identifiers(cand.hint, cand.hint, pid, None) & excluded:
                continue
            page = crawl.read_page(pid, cand.hint, "Verlinkte Seite")
            if page is None:
                continue
            if refcheck._identifiers(page.title, page.title, pid, page.url) & excluded:
                continue
            next_level.append((page, f"{cand.chains[0]} → {page.title}"))
            if len(page.text) < _MIN_TEXT_CHARS:
                result.link_rejected.append(RejectedLink(title=page.title, url=page.url, code="too_short"))
            else:
                judged.append((cand, page))

        if judged and not spec_text:
            spec_text = _spec_digest(strip_sources_tables(spec.body_storage))
        for batch in _batches(judged, _BATCH):
            ids = [p.id for _, p in batch]
            try:
                verdicts = _assess(_build_prompt(spec, listed, spec_text, batch, config.language), llm, cache, ids)
            except AnalysisError as exc:
                result.link_incomplete = True
                result.errors.append(ReportError(
                    scope=spec.title, message=f"assessment of linked pages by the LLM failed: {exc}",
                    impact="linked pages were not assessed, so no source suggestions are shown"))
                _finish(result)
                return
            for cand, page in batch:
                relevant, confidence, reason = verdicts[page.id]
                if relevant:
                    result.suggested.append(SuggestedSource(
                        title=page.title, page_id=page.id, url=page.url, linked_from=list(cand.chains), depth=depth,
                        mention=cand.mention, reason=reason, confidence=confidence))
                else:
                    result.link_rejected.append(RejectedLink(
                        title=page.title, url=page.url, reason=reason, code="llm"))

        if crawl.skipped_for_budget:
            break
        level = next_level

    if crawl.skipped_for_budget:
        result.link_incomplete = True
        crawl.warn(f"Linkprüfung nach {sm.link_max_pages} gelesenen Seiten abgebrochen, "
                   f"{crawl.skipped_for_budget} verlinkte Seite(n) nicht geprüft "
                   "(spec_monitor.reference_check.max_pages)")
    _finish(result)


def _finish(result: RefCheckResult) -> None:
    result.suggested.sort(key=lambda s: (s.depth, _CONFIDENCE.get(s.confidence, 2), s.title.casefold()))
    result.link_checked = len(result.suggested) + len(result.link_rejected)
