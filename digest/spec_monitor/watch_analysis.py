"""--page-watch: one synthesis over the changed pages of the watched tree (overall summary with themes,
change request, delta specification, open points, effort). Nothing is cut: input above the part size is
split by page and merged by a further call."""
import json
from typing import List, Optional, Tuple

from digest import summarizer
from digest.config import LLMConfig
from digest.spec_monitor import analysis
from digest.spec_monitor.analysis import AnalysisError, _DATA_NOTE, _quote
from digest.spec_monitor.cache import SpecCache, hash_key
from digest.spec_monitor.models import EffortEstimate, PageChange, PageStatus, Theme, WatchSynthesis

_PROMPT_VERSION = "1"
_PART_CHARS = 150000
_PURPOSE = "watch_synthesis"


def _page_block(pc: PageChange) -> str:
    r = pc.result
    lines = [f"### {pc.title}"]
    if pc.path:
        lines.append("Location: " + " > ".join(pc.path))
    if pc.status == PageStatus.NEW:
        lines.append("New page (did not exist at the start of the window)")
    elif r is not None:
        lines.append(f"Version: {analysis._version_str(r.old)} -> {analysis._version_str(r.new)}")
    if pc.summary:
        lines.append(f"Change summary: {pc.summary}")
    if r is not None:
        if r.diff_text:
            lines.append("Diff (- removed, + added):\n" + r.diff_text)
        if r.diagram_notes:
            lines.append("Diagram notes:\n" + "\n".join(r.diagram_notes))
    return "\n".join(lines)


def _parts(changes: List[PageChange]) -> List[str]:
    """Page blocks packed into parts of at most _PART_CHARS characters; a single over-long block is split."""
    parts: List[str] = []
    current: List[str] = []
    used = 0
    for block in (_page_block(c) for c in changes):
        for n, piece in enumerate(analysis._split_text(block, _PART_CHARS - 100)):
            if n:
                piece = "[continuation of the previous page]\n" + piece
            if current and used + len(piece) + 2 > _PART_CHARS:
                parts.append("\n\n".join(current))
                current, used = [], 0
            current.append(piece)
            used += len(piece) + 2
    if current:
        parts.append("\n\n".join(current))
    return parts or [""]


def _build_prompt(pages_block: str, window_label: str, watched_title: str, language: str,
                  part: Optional[Tuple[int, int]] = None) -> str:
    lang = summarizer._language_name(language)
    part_note = (
        f"The changes are long: the text below is part {part[0]} of {part[1]} (consecutive, split by page). "
        "Report only what is in THIS part; the parts are merged afterwards.\n" if part else ""
    )
    return (
        f"The pages below the Confluence page '{watched_title}' form a specification that the core implements. "
        f"They changed in the following window: {window_label}. Derive what this means for the existing "
        "implementation. Look at all pages together: changes on different pages often belong together.\n"
        "Never mention page version numbers or change dates in any text (overview, themes, change request, "
        "delta changes, open points); describe the current state of the specification only.\n"
        f"{part_note}{_DATA_NOTE}\n\n"
        "Produce one JSON object with:\n"
        '- "overview": an overall summary for non-technical readers, 3-8 sentences, plain language, '
        "no jargon: what changed in this window and why it matters.\n"
        '- "themes": groups of changes that belong together (also across pages), each {"title": short, '
        '"summary": what changes in this theme, "pages": [exact page titles as written below]}. A page may be '
        "in several themes; pages without relation to others form a theme of their own.\n"
        '- "change_request": a change request for non-technical readers who decide about additional effort. '
        "Plain language, no jargon. Cover, in this order: the starting situation; what changes for the "
        "customer / the module; what the core must additionally do; the impact and risk if it is not done. "
        "Keep original English technical terms (e.g. requestInProgress, createProcess) unchanged and add a "
        "short plain-language explanation next to each. Never use personal names; refer to roles or "
        "organisations only. Do not claim that something is or is not implemented in code and do not "
        "claim it is already covered by a ticket.\n"
        '- "delta_changes": list of technical changes for the implementer, each '
        '{"page": exact title of the page the change comes from, "kind": "new"|"changed"|"removed", '
        '"affected_part": heading/section, "ids": the requirement IDs or decision IDs (e.g. CM-FR-03) of the '
        'affected statements, copied only when they appear in the text below, "current_statement": what the '
        'specification said before, ONE short sentence (empty for new), "change": what the implementer must '
        'do, 1-2 short sentences, imperative, "reason": the evidence as a short old-versus-new quote, '
        'never a version number or date, "blocking": true ONLY when a concrete gap or contradiction in the '
        "specification prevents starting the implementation (e.g. a migration for existing data is not "
        "described); then \"change\" must name what is unclear}. One item per independently implementable "
        "change: never bundle unrelated changes (e.g. an enum value change and a deletion job) in one item, "
        "never list things that stay unchanged, and treat the text change and the diagram change of the same "
        "topic as one item. Keep new, changed and removed separate. Describe content only, never document "
        "structure or formatting. Diagram changes arrive as low-level node/edge lines: describe what the "
        "diagram now says, never the nodes and edges.\n"
        '- "open_points": list of {"question": ..., "raised_by": the page or change that raised it} for all '
        "open points that must be clarified before implementation can start.\n"
        '- "effort": ONE rough effort estimate for ALL changes together, based on the themes, not page by '
        'page: {"size": one of XS, S, M, L, XL, "person_days": range like "3-5", "rationale": one line} or '
        "null if it cannot be estimated. It is only a rough, unvalidated estimate.\n"
        f"Write all texts in {lang}; keep JSON keys and kind values in English. Be concise and information-dense.\n\n"
        + _quote("changed pages", pages_block)
        + '\n\nRespond with JSON only: {"overview": "...", "themes": [...], "change_request": "...", '
        '"delta_changes": [...], "open_points": [...], "effort": {...} or null}'
    )


def _parse_themes(value) -> List[Theme]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("themes must be a list")
    themes = []
    for t in value:
        if not isinstance(t, dict) or not str(t.get("title", "")).strip():
            raise ValueError("theme without title")
        themes.append(Theme(title=str(t["title"]).strip(), summary=str(t.get("summary", "") or "").strip(),
                            pages=analysis._str_list(t.get("pages"))))
    return themes


def _parse(data) -> WatchSynthesis:
    base = analysis._parse_synthesis(data)
    overview = data.get("overview")
    if not isinstance(overview, str) or not overview.strip():
        raise ValueError("missing overview")
    for change, raw in zip(base.delta_changes, data.get("delta_changes") or []):
        change.page = str(raw.get("page", "") or "").strip()
    return WatchSynthesis(
        overview=overview.strip(), themes=_parse_themes(data.get("themes")), change_request=base.change_request,
        delta_changes=base.delta_changes, open_points=base.open_points, effort=base.effort, warnings=base.warnings,
    )


def _ask_once(prompt: str, llm: LLMConfig, cache: SpecCache) -> WatchSynthesis:
    key = hash_key(_PROMPT_VERSION, analysis._model_key(llm), prompt)
    cached = cache.get_text("llm_watch", key)
    if cached is not None:
        try:
            return _parse(json.loads(cached))
        except (ValueError, TypeError):
            pass
    synthesis, raw = analysis._ask_json(prompt, llm, llm.llm_timeout, analysis._SYNTHESIS_MAX_TOKENS, _parse, _PURPOSE)
    cache.put_text("llm_watch", key, raw)
    return synthesis


def _parse_merge(data):
    if not isinstance(data, dict):
        raise ValueError("not an object")
    overview = data.get("overview")
    cr = data.get("change_request")
    if not isinstance(overview, str) or not overview.strip() or not isinstance(cr, str) or not cr.strip():
        raise ValueError("missing overview or change_request")
    effort = None
    e = data.get("effort")
    if isinstance(e, dict) and e.get("size"):
        effort = EffortEstimate(size=str(e["size"]), person_days=str(e.get("person_days", "") or ""),
                                rationale=str(e.get("rationale", "") or ""))
    return overview.strip(), _parse_themes(data.get("themes")), cr.strip(), effort


def _merge(partials: List[WatchSynthesis], watched_title: str, llm: LLMConfig, language: str,
           cache: SpecCache) -> WatchSynthesis:
    changes, seen = [], set()
    for p in partials:
        for c in p.delta_changes:
            k = (c.page, c.kind, c.affected_part, c.change)
            if k not in seen:
                seen.add(k)
                changes.append(c)
    points, questions = [], set()
    for p in partials:
        for o in p.open_points:
            if o.question not in questions:
                questions.add(o.question)
                points.append(o)
    warnings = [w for p in partials for w in p.warnings]

    lang = summarizer._language_name(language)
    blocks = []
    for i, p in enumerate(partials, 1):
        e = p.effort
        effort = f"{e.size}, {e.person_days} person days ({e.rationale})" if e else "none"
        themes = "\n".join(f"- {t.title}: {t.summary} [pages: {', '.join(t.pages)}]" for t in p.themes) or "(none)"
        blocks.append(f"Part {i}:\nOverview: {p.overview}\nThemes:\n{themes}\nChange request: {p.change_request}\n"
                      f"Estimated effort of this part: {effort}")
    prompt = (
        f"The changes of the pages below '{watched_title}' were analysed in {len(partials)} consecutive parts. "
        "Merge the per-part results into ONE: one overall summary (overview), one list of themes (merge themes "
        "that belong together across parts, keep page titles exactly), ONE change request with the same "
        "structure (starting situation; what changes for the customer / the module; what the core must "
        "additionally do; impact and risk) without repeating yourself, and one combined rough effort estimate "
        f"for all changes together.\nWrite in {lang}. Never use personal names.\n{_DATA_NOTE}\n\n"
        + _quote("per-part results", "\n\n".join(blocks))
        + '\n\nRespond with JSON only: {"overview": "...", "themes": [{"title": "...", "summary": "...", '
        '"pages": [...]}], "change_request": "...", "effort": {"size": "XS|S|M|L|XL", "person_days": "3-5", '
        '"rationale": "..."} or null}'
    )
    key = hash_key(_PROMPT_VERSION, analysis._model_key(llm), prompt)
    merged = None
    cached = cache.get_text("llm_watch_merge", key)
    if cached is not None:
        try:
            merged = _parse_merge(json.loads(cached))
        except (ValueError, TypeError):
            merged = None
    if merged is None:
        try:
            merged, raw = analysis._ask_json(prompt, llm, llm.llm_timeout, analysis._SYNTHESIS_MAX_TOKENS,
                                             _parse_merge, _PURPOSE)
            cache.put_text("llm_watch_merge", key, raw)
        except AnalysisError as exc:
            warnings.append(
                f"the per-part results could not be merged ({exc}); they are shown one after the other "
                "and no combined effort estimate is available"
            )
            merged = ("\n\n".join(p.overview for p in partials), [t for p in partials for t in p.themes],
                      "\n\n".join(p.change_request for p in partials), None)
    return WatchSynthesis(overview=merged[0], themes=merged[1], change_request=merged[2], delta_changes=changes,
                          open_points=points, effort=merged[3], warnings=warnings)


def synthesize_watch(changes: List[PageChange], window_label: str, watched_title: str, llm: LLMConfig,
                     language: str, cache: SpecCache) -> WatchSynthesis:
    """Raises AnalysisError when no usable answer was obtained."""
    parts = _parts(changes)
    if len(parts) == 1:
        synthesis = _ask_once(_build_prompt(parts[0], window_label, watched_title, language), llm, cache)
    else:
        partials = [_ask_once(_build_prompt(text, window_label, watched_title, language, (i, len(parts))), llm, cache)
                    for i, text in enumerate(parts, 1)]
        synthesis = _merge(partials, watched_title, llm, language, cache)
    titles = {c.title for c in changes}
    for theme in synthesis.themes:
        unknown = [t for t in theme.pages if t not in titles]
        if unknown:
            synthesis.warnings.append(
                f"theme '{theme.title}' names page(s) that are not among the changed pages: {', '.join(unknown)}")
    if not synthesis.delta_changes:
        synthesis.warnings.append(
            "the model returned no delta changes although pages changed; the delta spec is empty, check the pages manually")
    return synthesis
