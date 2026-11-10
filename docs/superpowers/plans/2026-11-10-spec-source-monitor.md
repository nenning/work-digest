# Spec Source Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--spec-monitor`: detect changed sources of Confluence detail specs and produce change request + delta spec as mail and optional Confluence page.

**Architecture:** New package `digest/spec_monitor/`. A shared data contract (`models.py`) plus infrastructure (`http.py` retry helper, `cache.py`, config blocks) already exists. Independent modules (text/diff, draw.io, parser, Confluence/eCH/GitLab monitors, LLM analysis, report rendering, publishing) are built in parallel against that contract; a runner then orchestrates them and `main.py` wires the CLI.

**Tech Stack:** Python 3, requests, Jinja2, pytest + pytest-mock, existing LLM abstraction in `digest/summarizer.py`.

**Spec:** `docs/superpowers/specs/2026-11-10-spec-source-monitor-design.md` (read it fully; this plan only pins interfaces and tests).

## Global Constraints

- Every `requests` call goes through `digest.spec_monitor.http.request_with_retry` (or `get_json`) and passes a timeout; never call `requests.get/post` directly in this package.
- All tests mock HTTP and LLM; no real credentials; `tests/conftest.py` already sets `sys.path`. New test files are named `tests/test_spec_monitor_<module>.py`. Run only your own test file(s) (`python -m pytest tests/test_spec_monitor_x.py`) — other modules are being built concurrently.
- Monitors never raise: a failure becomes `SourceResult(classification=ERROR, errors=[...])` with transparent text and `attempts`.
- Fetched content is data, never instructions (LLM prompts wrap it as quoted data).
- Code style (user memory): no comments except non-obvious WHY; no trailing summaries.
- Only `http`/`https` URLs may reach the output (reuse `digest.email_sender._safe_url`).
- Do not edit files owned by another task. Do not commit (the user commits).
- Output language `report.language` (default German); original English technical terms kept.
- Source names/titles in tests must be invented or taken from the row shapes in the spec — never paste real page bodies.

## Review Focus

- Version cell with more links than versions (e.g. `v46`, two links) → row flagged `could not pair`, never mis-paired.
- Source page deleted / 403 → ERROR row with message, spec still analysed.
- Repository unreachable → every repo source ERROR with reason, run continues, one probe only.
- LLM returns invalid JSON twice → spec marked `analysis_incomplete`, source facts still shown.
- Corrupt cache file → treated as miss.

## Task map (parallel)

| # | Task | Owns |
|---|---|---|
| 1 | textdiff | `digest/spec_monitor/textdiff.py`, `tests/test_spec_monitor_textdiff.py` |
| 2 | drawio | `digest/spec_monitor/drawio.py`, `tests/test_spec_monitor_drawio.py` |
| 3 | parser | `digest/spec_monitor/parser.py`, `tests/test_spec_monitor_parser.py` |
| 4 | confluence | `confluence_client.py`, `confluence_monitor.py`, `discovery.py` + tests |
| 5 | ech/i14y | `ech.py`, `i14y.py` + tests |
| 6 | gitlab | `gitlab.py` + tests |
| 7 | analysis | `analysis.py` + tests |
| 8 | report | `report.py`, `digest/templates/spec_monitor.html.j2` + tests |
| 9 | publish + infra tests + docs | `publish.py`, tests for `http.py`/`cache.py`/config, `digest/config.yaml.example`, `docs/architecture/spec-monitor.md` |
| 10 | runner + CLI (after 1–9) | `runner.py`, `digest/main.py`, delivery, `tests/test_spec_monitor_runner.py`, README/CLAUDE.md pointer |

Existing contract (do not change signatures; extend only by adding): `digest/spec_monitor/models.py`, `http.py` (`request_with_retry`, `get_json`, `RequestFailed`), `cache.py` (`SpecCache`, `hash_key`), config dataclasses in `digest/config.py` (`SpecMonitorConfig`, `GitLabConfig`, `RetryConfig`).

Common monitor shape (tasks 4, 5, 6):

```python
class XMonitor:
    def handles(self, ref: SourceRef) -> bool
    def resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult   # never raises
```
`resolve` fills `old`, `new`, `classification` (`CHANGED` / `COSMETIC_ONLY` / `UNCHANGED` / `NOT_MONITORED` / `ERROR`), and where content is comparable `old_text`, `new_text`, `diff_text`, `added_links`, `other_links`, `diagram_notes`, `warnings`.

### Task 1: textdiff

**Files:** `digest/spec_monitor/textdiff.py` (replace stub, keep signatures), `tests/test_spec_monitor_textdiff.py`

**Interfaces — Produces:** `storage_to_text(str)->str`, `diff_text(old,new)->str`, `is_cosmetic(diff)->bool`, `extract_page_links(str)->List[str]`, `added_links(old_storage,new_storage)->List[str]`, `drawio_revisions(str)->Dict[str,int]` (docstrings in the stub are normative).

- [ ] Tests: CDATA code macro text survives; `ac:link` → `Text[->Page#anchor]`; table cells joined with ` | `; `diff_text` of identical → `""`; whitespace/punctuation-only change → `is_cosmetic` True; a changed sentence → False; `added_links` returns only new page titles; `drawio_revisions` parses `revision`+`diagramName` regardless of parameter order.
- [ ] Implement; reuse ideas from `digest/sources/confluence.py:_storage_to_text` (line 311) but this module must stay independent of it.
- [ ] Run `python -m pytest tests/test_spec_monitor_textdiff.py` → pass.

### Task 2: drawio

**Files:** `digest/spec_monitor/drawio.py` (replace stub), `tests/test_spec_monitor_drawio.py`

**Produces:** `DiagramDiff`, `diff_diagrams(old_xml,new_xml)->DiagramDiff` (stub docstring normative).

- [ ] Tests (build tiny mxfile strings in the test): plain mxGraphModel; compressed (base64 raw-deflate + URL-encoding) variant gives the same result; added/removed node text; changed edge label; layout-only change → `layout_only=True`, no changes; two `<diagram>` pages → warning; garbage input → `ValueError`.
- [ ] Implement per spec section "Pipeline" step 5; label cleaning `<br>`,`</p>`,`</div>` → `/`.
- [ ] Run its test file → pass.

### Task 3: parser

**Files:** `digest/spec_monitor/parser.py`, `tests/test_spec_monitor_parser.py`

**Produces:**
```python
def parse_sources(storage_html: str, *, stand_date: Optional[date], excluded_markers: List[str], gitlab: Optional[GitLabConfig] = None) -> ParsedSources
def parse_page_properties(storage_html: str) -> PageProperties
def find_sources_sentence(storage_html: str) -> Optional[str]   # plain text of a "Quellen: …" sentence when there is no table
```

- [ ] Tests with the row shapes from the spec table, built as storage-format tables in the test: `v86, 16. Okt. 2026` → baseline_version `86`, date 2026-10-16; `v258 (09.11.2026)`; three links + `v187, v352, v91` → paired by position; two links + `v46, v68`; two links + single `gelesen am 2026-11-05` → all get that date, no version; three links with two versions → `parse_issue` "could not pair" on those refs; `nicht erfasst`/`-`/`–` → no baseline; `Entwurf (PROJ-1856)` → no baseline; `Notizen Stephan` / `Arbeitsnotizen` row and `Vorgängerfassung dieser Seite` → `excluded_reason` set (marker match on name or used_for, case-insensitive); `eCH-0108 Abschnitt 3.1.8 und die i14y-Codeliste der Rechtsformen v1.2.0` → one ECH ref (section `Abschnitt 3.1.8`) and one I14Y ref (baseline_version `1.2.0`); external URL row with `[SRC-1]` → EXTERNAL with url; table with header `Quelle | Version | Verwendet für`, trailing empty cell ignored; heading not numbered / not `1.5` still found; no table but a `Quellen:` sentence → `found=False` and `find_sources_sentence` returns it; `Lücken in den Quellen: …` / `Nicht gelesen: …` paragraphs → `gap_notes`; the "über eine Markdown-Konvertierung gelesen, dabei gingen Makros und Bilder verloren" note → `gap_notes`; repo rows with `gitlab` config: path `docs/adr/0028-x.md` + `Accepted, 03.11.2026` → REPO ref with repo_path, baseline_date; row naming a configured repo `match` + `Branch X, Commit abc1234, 30.10.2026` → repo_branch/repo_commit/date, path from `documents` alias; without gitlab config → EXTERNAL with note; `parse_page_properties`: status macro title in the `details` table → status, `PROJ-1234` keys → jira, `Stand: …, 2026-10-27` (time element or text) → stand_date; missing → `unknown`.
- [ ] Implement; Confluence link → `SourceRef(kind=CONFLUENCE, title=…, space_key=… if given)`.
- [ ] Run its test file → pass.

### Task 4: confluence (client, monitor, discovery)

**Files:** `digest/spec_monitor/confluence_client.py`, `confluence_monitor.py`, `discovery.py`, tests `tests/test_spec_monitor_confluence.py`, `tests/test_spec_monitor_discovery.py`

**Interfaces — Consumes:** `textdiff.*`, `drawio.diff_diagrams` (mock in tests), `parser.parse_page_properties` (mock), `http.request_with_retry`, `SpecCache`.
**Produces:**
```python
class ConfluenceClient:
    def __init__(self, atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, cache: SpecCache)
    def page_url(self, page_id: str) -> str
    def search_descendants(self, parent_id: str, label: Optional[str] = None) -> List[dict]   # {"id","title","version"}
    def get_page(self, page_id: str) -> dict                       # live: body.storage, version, history
    def get_body(self, page_id: str, version: int) -> str          # historical, cached forever
    def list_versions(self, page_id: str) -> List[VersionPoint]    # ascending, version str + when
    def find_page_id(self, title: str, space_key: Optional[str] = None) -> Optional[str]
    def download_attachment(self, page_id: str, filename: str, version: int) -> Optional[bytes]
def pick_version_at(versions: List[VersionPoint], day: date) -> Optional[VersionPoint]   # last version at/before end of that local day
class ConfluenceMonitor:   # common monitor shape
    def __init__(self, client: ConfluenceClient)
@dataclass class Discovery: specs: List[SpecInfo]; excluded: List[Tuple[str, str]]
def discover_specs(client: ConfluenceClient, cfg: SpecMonitorConfig, only: Optional[str] = None) -> Discovery
```
Client methods raise `RequestFailed` on persistent transient failure and return None/raise `LookupError` on 404.

- [ ] Tests (client): `list_versions` pagination; `get_body` second call hits cache (one HTTP call total); `download_attachment` follows the 302 manually and requests the `Location` WITHOUT the Authorization header; `pick_version_at` boundaries (before first version → None).
- [ ] Tests (monitor): stated version exists → old/new bodies diffed, CHANGED; same version → UNCHANGED (no body fetch); cosmetic-only diff → COSMETIC_ONLY; no stated version but `baseline_date` → old `inferred=True` with reason `from date <date>`; no date either → falls back to `spec.stand_date` then `spec.created`, reason names the rule; stated version missing from history → ERROR row with warning text; page not found / 403 / `RequestFailed` → ERROR with `attempts`; drawio revision differs → attachment diffed, notes in `diagram_notes`, undiffable → note "diagram changed, not compared"; added links not in `spec`'s table → `added_links`; page ID resolved via `find_page_id` when `ref.page_id` is None.
- [ ] Tests (discovery): `page_properties_id` selector keeps only pages with the `details` macro of that ID; `label` selector uses a label CQL; non-selected children → `excluded` (id, title); statuses via `parse_page_properties`; `only` matches title case-insensitively or id, raises `ValueError` if not among specs.
- [ ] Implement all three files.
- [ ] Run both test files → pass.

### Task 5: eCH and i14y

**Files:** `digest/spec_monitor/ech.py`, `i14y.py`, `tests/test_spec_monitor_ech.py`, `tests/test_spec_monitor_i14y.py`

**Produces:** `EchMonitor(retry: RetryConfig, cache: SpecCache, base_url: str = "https://www.ech.ch")` and `I14yMonitor(retry, cache)` with the common monitor shape (`handles`: `kind == ECH` / `I14Y`).

- [ ] Tests with canned HTML modelled on `https://www.ech.ch/de/ech/ech-0108` (version links `/de/ech/ech-0108/6.0.0`, `…/5.1`, …; version page with `Status Genehmigt` and `Beschlussdatum`): old = latest approved at `ref.baseline_date` → else `spec.stand_date` → else `spec.created` (inferred, reason named) unless `ref.baseline_version` is stated; new = latest approved today; equal → UNCHANGED, differ → CHANGED with both `VersionPoint`s (url, status, date); non-approved newer versions are ignored; unreachable site → ERROR; `ref.section` kept in `warnings` ("check Abschnitt 3.1.8"); version pages cached.
- [ ] i14y: probe whether a machine-readable version list exists (try the public i14y API for code lists); if yes monitor like eCH; if not, return NOT_MONITORED with reason. The test pins whichever behaviour you implement.
- [ ] Implement; run both test files → pass.

### Task 6: gitlab

**Files:** `digest/spec_monitor/gitlab.py`, `tests/test_spec_monitor_gitlab.py`

**Produces:** `GitLabMonitor(cfg: GitLabConfig, retry: RetryConfig, cache: SpecCache)` with the common monitor shape (`handles`: `kind == REPO`).

- [ ] Tests: auth header `PRIVATE-TOKEN`; old = stated `repo_commit` if it exists, else last commit touching `repo_path` on/before end of `baseline_date` on `repo_branch` (fallback to repo default branch with warning if branch gone), else `spec.stand_date` (inferred); new = latest commit touching the file on the default branch; equal SHA → UNCHANGED; differ → file raw at both SHAs, `diff_text` via `textdiff`, CHANGED / COSMETIC_ONLY; `.drawio` path → `drawio.diff_diagrams` (mock); pinned branch ahead of default → info in `warnings`; file missing/deleted → ERROR text "file not found at <sha>"; **unreachable repo** (RequestFailed on probe, 401/403) → every ref of that repo returns ERROR `repository not reachable (<reason>)` and the probe is performed once per repo per monitor instance (assert call count); repo not configured → NOT_MONITORED with reason; raw files cached by SHA.
- [ ] Implement; run test file → pass.

### Task 7: analysis

**Files:** `digest/spec_monitor/analysis.py`, `tests/test_spec_monitor_analysis.py`

**Produces:**
```python
@dataclass class SpecSynthesis: change_request: str; delta_changes: List[DeltaChange]; open_points: List[OpenPoint]; effort: Optional[EffortEstimate]
def summarize_source_diff(result: SourceResult, llm: LLMConfig, language: str, cache: SpecCache) -> str
def synthesize_spec(spec: SpecInfo, results: List[SourceResult], llm: LLMConfig, language: str, cache: SpecCache) -> SpecSynthesis
def extract_sources_with_llm(sentence: str, llm: LLMConfig, language: str) -> List[SourceRef]      # low_confidence=True
def build_completeness_warnings(gap_notes: List[str], results: List[SourceResult], parse_issues: List[str]) -> List[str]
```
Raises `AnalysisError(str)` when no usable output after fallback models and one reparse attempt.

- [ ] Tests (LLM mocked by patching `digest.summarizer._call_openai` / `_call_anthropic`): summary describes content only — prompt contains the diff wrapped as data and the `used_for` text; cache hit on identical input (one LLM call for two invocations), miss when the diff changes; `synthesize_spec` parses the JSON (kinds new/changed/removed, blocking flag, open points, effort size+person_days); invalid JSON → one retry, then `AnalysisError`; primary model fails → fallback model used (`_models_to_try`); prompts demand plain-language German change request, original English terms kept, no personal names; Anthropic path passes a large `max_tokens`; `build_completeness_warnings` covers gap notes, added links, diagrams not compared, unmonitorable/error/inferred baseline, parse issues, `section` references.
- [ ] Implement using `digest.summarizer` helpers; the synthesis call uses timeout `max(llm.llm_timeout * 3, 90)`.
- [ ] Run test file → pass.

### Task 8: report

**Files:** `digest/spec_monitor/report.py`, `digest/templates/spec_monitor.html.j2`, `tests/test_spec_monitor_report.py`

**Produces:**
```python
def render_mail_html(report: RunReport) -> str
def render_confluence_storage(report: RunReport) -> str
def build_subject(prefix: str, report: RunReport) -> str
```

- [ ] Tests: mail starts with a ToC linking each spec section and the errors section; per spec section order: sources table old vs new → completeness warnings → change request (with AI effort estimate) → delta spec (changes, then open points); overview lists analysed-with-changes / analysed-without-changes / not analysed (status) / excluded pages; problem-count banner only when errors exist; errors section lists scope, message, attempts, impact; unsafe URLs (`javascript:`) not rendered as links; **Confluence storage**: starts with `toc` macro, no AI estimate text anywhere, empty effort placeholders (summary table at top, per-spec block size/person-days/remark, empty per-item effort column + empty sum row), `data-layout="full-width"` tables, valid escaping (`html.escape`); inferred versions labelled `inferred`; German labels.
- [ ] Implement with Jinja2 (autoescape) for the mail; build storage XHTML in Python.
- [ ] Run test file → pass.

### Task 9: publish, infra tests, docs

**Files:** `digest/spec_monitor/publish.py`, `tests/test_spec_monitor_publish.py`, `tests/test_spec_monitor_http.py`, `tests/test_spec_monitor_cache.py`, additions to `tests/test_config.py`, `digest/config.yaml.example`, `docs/architecture/spec-monitor.md`

**Produces:** `def publish_page(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, parent_page_id: str, title: str, storage: str, message: str) -> str` (returns page URL; raises `RequestFailed`/`RuntimeError` with clear text).

- [ ] Tests publish: space key resolved from the parent (`expand=space`), POST `/content` with `ancestors`, `body.storage`, version message; full-width properties `content-appearance-published` and `content-appearance-draft` set to `full-width` (create on 404, update with version+1 otherwise); returns URL.
- [ ] Tests http: retries on ConnectionError/Timeout/429/5xx with doubling backoff (inject `sleep`), honours `Retry-After`, 404 not retried, `RequestFailed` after exhaustion with attempt count. Tests cache: round trip, corrupt file → miss and counted, hash_key stable.
- [ ] Config tests: `spec_monitor`/`gitlab` blocks parse, defaults (`analysis_states`, retry 3/2), invalid selector type → `ValueError`, missing selector value → `ValueError`, gitlab without token → `ValueError`.
- [ ] Add commented `spec_monitor:` and `gitlab:` example blocks to `digest/config.yaml.example` (from the spec's Configuration section) and write `docs/architecture/spec-monitor.md` (data flow, modules, config, error handling, cache, follow-ups).

### Task 10: runner and CLI (after tasks 1–9 are merged)

**Files:** `digest/spec_monitor/runner.py`, `digest/main.py`, `tests/test_spec_monitor_runner.py`, `tests/test_main.py` additions, `README.md` + `CLAUDE.md` pointers

**Produces:**
```python
def run_spec_monitor(config: Config, *, only: Optional[str], command_line: str, progress=print) -> RunReport
```
- [ ] Runner: discovery → statuses split into analysed / listed-only (case-insensitive `analysis_states`) → per analysed spec: `parse_sources` (LLM fallback via `find_sources_sentence` + `extract_sources_with_llm` when `found` is False) → each ref to the monitor that `handles` it, `EXCLUDED` results for `excluded_reason`, `NOT_MONITORED` for EXTERNAL → `summarize_source_diff` for CHANGED → `synthesize_spec` when any CHANGED → completeness warnings. Per-source and per-spec failures become `ReportError`s; fatal only when discovery fails (invalid config, parent unreachable) → raise `SpecMonitorFatal`.
- [ ] CLI: `--spec-monitor [--spec X] [--publish-page] [--dry-run]` mutually exclusive with `--mgmt-summary`; `--dry-run` writes HTML to `<data_dir>/spec_cache/preview.html` and prints a console summary; otherwise sends the mail via the same delivery paths as `--mgmt-summary` (recipient fallback `spec_monitor.recipient` → `mgmt_summary.recipient` → `email.recipient`); `--publish-page` calls `publish_page` (needs `spec_monitor.confluence_page.parent_page_id`) and prints the URL; fatal error → short error mail (when delivery configured) and non-zero exit; `state.json` is never touched.
- [ ] Tests: runner with fake monitors covering analysed/listed-only/excluded, one failing source not failing the spec, LLM failure → `analysis_incomplete`, fatal discovery; CLI flag exclusivity and dry-run.
- [ ] Run the full suite `python -m pytest tests/` → pass.

## Self-review

Spec coverage: discovery & selector (4), parsing/pinning (3, 4), eCH/i14y (5), GitLab (6), diffs & draw.io (1, 2, 4), analysis & completeness (7), mail/page with ToC, order, placeholders (8, 9), errors/retries/cache (infra, all), CLI (10), follow-ups documented (9). Deferred items (`--create-ticket`, `--update-spec`, vision draw.io) are intentionally absent and listed in the architecture doc.
