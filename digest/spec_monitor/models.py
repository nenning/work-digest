"""Shared data contract of the spec source monitor (see
docs/superpowers/specs/2026-11-10-spec-source-monitor-design.md).

Every module of digest/spec_monitor builds against these types. All of them are
plain dataclasses so the per-spec result stays serialisable (needed by the
deferred --create-ticket / --update-spec follow-ups).
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Callable, List, Optional, Tuple


class SourceKind(str, Enum):
    CONFLUENCE = "confluence"
    ECH = "ech"
    I14Y = "i14y"
    REPO = "repo"
    URL = "url"             # http(s) link to a web page / OpenAPI / portal API: UrlMonitor (best effort)
    EXTERNAL = "external"   # free text without link: not monitorable


class Classification(str, Enum):
    CHANGED = "changed"
    COSMETIC_ONLY = "cosmetic only"
    UNCHANGED = "unchanged"
    NOT_MONITORED = "not monitored"
    ERROR = "error"
    EXCLUDED = "excluded"


@dataclass
class SourceRef:
    """One source as stated in a spec's Quellen table (one row can yield several)."""
    name: str                              # display name, e.g. "Batch Processing"
    kind: SourceKind
    used_for: str = ""                     # "Verwendet für" cell
    version_text: str = ""                 # raw version cell (whole cell, for the report)
    title: Optional[str] = None            # Confluence page title
    page_id: Optional[str] = None
    space_key: Optional[str] = None
    url: Optional[str] = None
    baseline_version: Optional[str] = None   # stated version: "86", "6.0.0", commit sha
    baseline_date: Optional[date] = None     # stated date (alone or next to the version)
    note: str = ""                           # extra row text: branch/commit, remarks
    section: Optional[str] = None            # e.g. "Abschnitt 3.1.8"
    repo_name: Optional[str] = None          # configured repository name (kind REPO)
    repo_path: Optional[str] = None
    repo_branch: Optional[str] = None
    repo_commit: Optional[str] = None
    excluded_reason: Optional[str] = None    # set => classification EXCLUDED
    parse_issue: Optional[str] = None        # e.g. "could not pair links and versions"
    low_confidence: bool = False             # extracted by the LLM fallback


@dataclass
class ParsedSources:
    found: bool                              # a sources table / sentence was found
    refs: List[SourceRef] = field(default_factory=list)
    gap_notes: List[str] = field(default_factory=list)   # "Lücken in den Quellen: …" etc.
    issues: List[str] = field(default_factory=list)      # parse problems for the report


@dataclass
class VersionPoint:
    version: str                             # "86", "6.0.0", commit sha
    when: Optional[datetime] = None
    url: Optional[str] = None
    status: Optional[str] = None             # eCH: "Genehmigt"
    inferred: bool = False
    inferred_reason: Optional[str] = None    # e.g. "from Stand date 2026-10-27"


@dataclass
class SourceStep:
    """One intermediate version between the old and new state of a source."""
    version: str
    when: Optional[datetime] = None
    diff_text: str = ""                      # diff against the previous step (cosmetic-filtered)
    summary: Optional[str] = None


@dataclass
class SourceResult:
    ref: SourceRef
    classification: Classification
    old: Optional[VersionPoint] = None
    new: Optional[VersionPoint] = None
    diff_url: Optional[str] = None           # Confluence page-comparison URL of old vs new
    old_text: Optional[str] = None           # plain text, when comparable
    new_text: Optional[str] = None
    diff_text: Optional[str] = None          # cosmetic-filtered diff, when comparable
    diff_summary: Optional[str] = None       # filled by analysis
    steps: List[SourceStep] = field(default_factory=list)    # per-version history (empty: net diff only)
    diagram_notes: List[str] = field(default_factory=list)   # draw.io changes / "not compared"
    added_links: List[str] = field(default_factory=list)     # links added in the diff, not in table
    other_links: List[str] = field(default_factory=list)     # other links not in table (low priority)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)          # transparent error lines
    attempts: int = 1                                        # HTTP attempts of the failing call


@dataclass
class PageProperties:
    """Values read from a spec's page-properties table / status panel."""
    status: str = "unknown"
    jira: List[str] = field(default_factory=list)
    stand_date: Optional[date] = None


@dataclass
class SpecInfo:
    page_id: str
    title: str
    url: str
    status: str                              # raw status text, "unknown" if absent
    jira: List[str] = field(default_factory=list)
    stand_date: Optional[date] = None        # date of the "Stand:" panel line
    created: Optional[datetime] = None
    body_storage: str = ""                   # empty for listed-only specs
    ancestor_ids: List[str] = field(default_factory=list)      # links to these are never sources/warnings
    ancestor_titles: List[str] = field(default_factory=list)


@dataclass
class DeltaChange:
    kind: str                                # "new" | "changed" | "removed"
    affected_part: str                       # heading / section
    ids: List[str] = field(default_factory=list)   # e.g. ["CM-FR-03"]
    current_statement: str = ""
    change: str = ""
    reason: str = ""                         # source + old vs new quote
    blocking: bool = False


@dataclass
class OpenPoint:
    question: str
    raised_by: str = ""                      # source change that raised it


@dataclass
class EffortEstimate:
    size: str                                # "XS".."XL"
    person_days: str                         # e.g. "3-5"
    rationale: str = ""


@dataclass
class ReportError:
    scope: str                               # spec or source name
    message: str
    attempts: int = 1
    impact: str = ""                         # what is missing because of it


@dataclass
class SpecAnalysis:
    spec: SpecInfo
    sources: List[SourceResult] = field(default_factory=list)
    gap_notes: List[str] = field(default_factory=list)
    change_request: Optional[str] = None
    delta_changes: List[DeltaChange] = field(default_factory=list)
    open_points: List[OpenPoint] = field(default_factory=list)
    effort: Optional[EffortEstimate] = None
    completeness_warnings: List[str] = field(default_factory=list)
    errors: List[ReportError] = field(default_factory=list)
    analysis_incomplete: bool = False

    @property
    def has_changes(self) -> bool:
        return any(s.classification == Classification.CHANGED for s in self.sources)


@dataclass
class MissingReference:
    """Something a spec's text points to that its Quellen table does not list."""
    reference: str                           # as written, e.g. "ADR-0028", "eCH-0200", a page title
    kind: str                                # "confluence" | "ech" | "adr" | "url" | "repo" | "other"
    mention: str = ""                        # the sentence / line that mentions it
    section: str = ""                        # heading of the section it was found in
    origin: str = "pattern"                  # "pattern" (deterministic) | "llm"
    url: Optional[str] = None
    why: str = ""                            # LLM: why it looks like a source


@dataclass
class SuggestedSource:
    """A page linked from a listed source (directly or via further pages) that the model judged to be
    a source the spec should list."""
    title: str
    page_id: str
    url: str
    linked_from: List[str] = field(default_factory=list)   # "Quelle A" (depth 1) / "Quelle A → Seite B" (depth 2)
    depth: int = 1
    mention: str = ""                        # sentence of the linking page that carries the link
    reason: str = ""                         # LLM: what the spec would use the page for
    confidence: str = "low"                  # "high" | "medium" | "low"


@dataclass
class RejectedLink:
    """A linked page that was read but not suggested."""
    title: str
    url: str
    reason: str = ""
    code: str = "llm"                        # "llm" (model verdict) | "too_short" (page has hardly any text)


@dataclass
class RefCheckResult:
    spec: SpecInfo
    listed_count: int = 0                    # sources in the Quellen table (excluded rows included)
    missing: List[MissingReference] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[ReportError] = field(default_factory=list)
    llm_incomplete: bool = False             # the LLM pass failed: only the pattern findings are shown
    suggested: List[SuggestedSource] = field(default_factory=list)   # link crawl: pages the spec should list
    link_rejected: List[RejectedLink] = field(default_factory=list)
    link_checked: int = 0                    # linked pages read and judged (suggested + rejected)
    link_depth: int = 0                      # depth the crawl ran with (0 = off)
    link_incomplete: bool = False            # the crawl or its assessment failed or was cut off


@dataclass
class RefCheckReport:
    generated_at: datetime
    results: List[RefCheckResult] = field(default_factory=list)
    excluded_pages: List[Tuple[str, str]] = field(default_factory=list)  # (page_id, title)
    errors: List[ReportError] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    command_line: str = ""
    language: str = "de"


@dataclass
class RunReport:
    generated_at: datetime
    analysed: List[SpecAnalysis] = field(default_factory=list)
    listed_only: List[SpecInfo] = field(default_factory=list)
    excluded_pages: List[Tuple[str, str]] = field(default_factory=list)  # (page_id, title)
    errors: List[ReportError] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    cache_corrupt: int = 0                   # unreadable/corrupt entries met (treated as misses)
    cache_write_errors: int = 0
    cache_write_error: str = ""              # first write error message
    command_line: str = ""
    commit_baselines: Optional[Callable[[], List[str]]] = None   # set when baseline writes were deferred
    language: str = "de"
