"""Find the spec pages below the configured parent page."""
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from digest.config import SpecMonitorConfig
from digest.spec_monitor.confluence_client import ConfluenceClient, parse_when
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import PageProperties, ReportError, SpecInfo
from digest.spec_monitor.spec_ref import normalize_spec_ref

_DETAILS_HEAD = re.compile(
    r'<ac:structured-macro\b[^>]*\bac:name="details"[^>]*>(?P<head>.*?)(?:<ac:rich-text-body>|</ac:structured-macro>)',
    re.S,
)
_FETCH_WORKERS = 6
_ID_PARAM = re.compile(r'<ac:parameter\s+ac:name="id"\s*>(?P<value>.*?)</ac:parameter>', re.S)


@dataclass
class Discovery:
    specs: List[SpecInfo] = field(default_factory=list)
    excluded: List[Tuple[str, str]] = field(default_factory=list)   # (page_id, title)
    unreadable: List[Tuple[str, str, str]] = field(default_factory=list)   # (page_id, title, reason)
    problems: List[ReportError] = field(default_factory=list)   # discovery results that look wrong


def _parse_properties(body: str) -> PageProperties:
    from digest.spec_monitor.parser import parse_page_properties
    return parse_page_properties(body)


def has_details_macro(storage: str, macro_id: str) -> bool:
    for m in _DETAILS_HEAD.finditer(storage):
        for p in _ID_PARAM.finditer(m.group("head")):
            if p.group("value").strip() == macro_id:
                return True
    return False


def discover_specs(client: ConfluenceClient, cfg: SpecMonitorConfig, only: Optional[str] = None) -> Discovery:
    """Raises RequestFailed/LookupError when the parent cannot be searched (fatal for the run)
    and ValueError for an unknown selector type or an `only` that is not among the specs."""
    if cfg.selector_type not in ("page_properties_id", "label"):
        raise ValueError(f"unknown spec selector type '{cfg.selector_type}'")

    candidates = client.search_descendants(cfg.parent_page_id, None)
    ref = needle = None
    if only is not None:
        try:
            ref = normalize_spec_ref(only) or ""
        except ValueError as exc:
            raise ValueError(f"'{only}' is not among the discovered specs: {exc}") from exc
        needle = ref.casefold()
        candidates = [c for c in candidates if c["id"] == ref or c["title"].casefold() == needle]
        if not candidates:
            raise ValueError(f"'{only}' is not among the discovered specs: no page below the parent matches")
    labelled = None
    if cfg.selector_type == "label":
        labelled = {c["id"] for c in client.search_descendants(cfg.parent_page_id, cfg.selector_value)}

    to_fetch = [c for c in candidates if labelled is None or c["id"] in labelled]

    def fetch(cand: dict):
        try:
            return client.get_page(cand["id"]), None
        except (LookupError, RequestFailed) as exc:
            return None, exc

    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        fetched = dict(zip((c["id"] for c in to_fetch), pool.map(fetch, to_fetch)))

    result = Discovery()
    for cand in candidates:
        pid, title = cand["id"], cand["title"]
        if pid not in fetched:
            result.excluded.append((pid, title))
            continue
        page, exc = fetched[pid]
        if exc is not None:
            reason = exc.last_error if isinstance(exc, RequestFailed) else str(exc)
            result.unreadable.append((pid, title, str(reason)))
            continue
        body = page["body"]["storage"]["value"]
        if labelled is None and not has_details_macro(body, cfg.selector_value):
            result.excluded.append((pid, title))
            continue
        props = _parse_properties(body)
        ancestors = page.get("ancestors") or []
        if props.status == "unknown":
            result.problems.append(ReportError(
                scope=title, message="status could not be read from the page properties (shown as 'unknown')",
                impact="the status filter cannot decide whether this spec is analysed; it is not checked unless "
                       "'unknown' is in analysis_states"))
        analysed = cfg.is_analysed(props.status)
        result.specs.append(SpecInfo(
            page_id=pid,
            title=title,
            url=client.page_url(pid),
            status=props.status,
            jira=list(props.jira),
            stand_date=props.stand_date,
            created=parse_when((page.get("history") or {}).get("createdDate")),
            body_storage=body if analysed else "",
            ancestor_ids=[str(a["id"]) for a in ancestors if a.get("id") is not None],
            ancestor_titles=[a["title"] for a in ancestors if a.get("title")],
        ))

    if not result.specs and only is None:
        how = (f"label '{cfg.selector_value}'" if cfg.selector_type == "label"
               else f"page-properties macro id '{cfg.selector_value}'")
        result.problems.append(ReportError(
            scope=f"parent page {cfg.parent_page_id}",
            message=f"no spec found among {len(candidates)} page(s) below the parent (selector: {how})",
            impact="nothing was checked; 'no changes' is meaningless -- check the selector and parent_page_id"))

    if only is not None:
        matching = [s for s in result.specs if s.page_id == ref or s.title.casefold() == needle]
        if not matching:
            known = ", ".join(f"{s.title} ({s.page_id})" for s in result.specs) or "none"
            raise ValueError(f"'{only}' is not among the discovered specs: {known}")
        result.specs = matching
    return result
