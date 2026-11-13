# Page Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `--page-watch`: report what changed on a Confluence page tree in the last N days, with a cross-page summary, change request, delta spec and effort.

**Architecture:** New modules in `digest/spec_monitor/` reuse `ConfluenceMonitor` (each page = synthetic `SourceRef` with `baseline_date`), `analysis.summarize_source_diff` (page flavour) and the LLM plumbing; a new `watch_analysis.py` does the cross-page synthesis; `page_watch.py` orchestrates; `page_watch_report.py` + template render mail / Confluence page; `cli.py` / `main.py` wire it. No state is read or written.

**Tech Stack:** Python 3, requests, Jinja2, pytest + pytest-mock.

**Spec:** `docs/superpowers/specs/2026-11-13-page-watch-design.md`

## Global Constraints

- Every HTTP call via `spec_monitor/http.request_with_retry` with timeout; LLM calls with the configured timeout; no new retry policy.
- Never write `state.json` / `url_baselines/`; only the cache (`<data_dir>/spec_cache/`).
- No silent failures: per-page error isolation into report rows; discovery failure is `SpecMonitorFatal`; run with problems exits 2 after delivery.
- Big inputs never cut: chunk and merge.
- Code style (user memory): no comments except non-obvious WHY; no trailing summaries. Do not commit (the user commits).
- Tests mock HTTP and LLM; new files `tests/test_page_watch_*.py`.

## Review Focus

- Page created inside the window (no version at/before baseline) → `NEW`, whole text as change, not an error.
- One unreadable page → error row, other pages still analysed, exit code 2.
- Nothing changed → mail still produced, says so with window dates.
- Synthesis returns invalid JSON twice → `analysis_incomplete`, per-page facts still shown.
- Window of 0 / negative / non-integer in config or `--days` → rejected.

## Tasks

### Task 1: Config + models
**Files:** Modify `digest/config.py`, `digest/spec_monitor/models.py`, `digest/spec_monitor/confluence_client.py` (add `"when"` to `search_descendants` dicts), `tests/test_spec_monitor_confluence.py:131`; Test `tests/test_page_watch_config.py`.
**Produces:** `PageWatchConfig(parent_page_id, window_days=14, label=None, include_parent=True, recipient=None, workers=4, llm_timeout=240, retry=RetryConfig(), publish_parent_page_id=None)`; `Config.page_watch: Optional[PageWatchConfig]`; `spec_llm()` / `effective_vision_llm()` use `config.spec_monitor or config.page_watch` for the timeout; models `PageChange`, `Theme`, `WatchSynthesis`, `PageWatchReport`, `DeltaChange.page: str = ""`.
- [ ] Tests: defaults; `window_days: 0` and `"x"` raise `ValueError`; missing `parent_page_id` raises; `spec_llm` takes `page_watch.llm_timeout` when `spec_monitor` is None.
- [ ] Implement `_load_page_watch(raw)` and wire into `load_config`; models as dataclasses.

### Task 2: Page-flavoured summary prompt
**Files:** Modify `digest/spec_monitor/analysis.py`; Test `tests/test_page_watch_analysis.py`.
**Produces:** `summarize_source_diff(result, llm, language, cache, page_mode: bool = False)`; page mode words the prompt as a change of a specification page (no "source document" / "used for").
- [ ] Test: prompt in page mode contains the page title and not "used in the specification for"; default prompt unchanged.

### Task 3: Cross-page synthesis
**Files:** Create `digest/spec_monitor/watch_analysis.py`; Test `tests/test_page_watch_analysis.py`.
**Produces:** `synthesize_watch(changes: List[PageChange], window_label: str, llm, language, cache) -> WatchSynthesis` (overview, themes, change_request, delta_changes with `page`, open_points, effort, warnings); splits by page into parts above `_SYNTHESIS_PART_CHARS` and merges (overview/themes/change request/effort by one call, deltas/open points concatenated + de-duplicated; merge failure → parts shown one after the other + warning).
- [ ] Tests: parse valid answer; theme referencing unknown page is kept but warned; two parts → merge call; merge failure fallback; invalid JSON twice raises `AnalysisError`.

### Task 4: Orchestration
**Files:** Create `digest/spec_monitor/page_watch.py`; Test `tests/test_page_watch_runner.py`.
**Produces:** `baseline_day(window_days, today=None) -> date`; `run_page_watch(config, *, days=None, command_line="", progress=print, client=None, monitor=None, cache_dir=None) -> PageWatchReport`.
- [ ] Tests: baseline_day(14, 2026-11-13) == 2026-10-30; unchanged page skipped via `when`; changed page summarised; new page → `NEW`; cosmetic demotion; failing page → error row, others continue; discovery failure → `SpecMonitorFatal`; synthesis failure → `analysis_incomplete`.

### Task 5: Report
**Files:** Create `digest/spec_monitor/page_watch_report.py`, `digest/templates/page_watch.html.j2`; Test `tests/test_page_watch_report.py`.
**Produces:** `build_subject(prefix, report)`, `render_mail_html(report)`, `render_confluence_storage(report)`, `problem_count(report)`.
- [ ] Tests: changed run contains page titles, themes, Änderungsantrag, effort; none-changed run says no changes with window; errors table; Confluence storage has empty effort placeholders and no AI estimate; URLs go through `_safe_url`.

### Task 6: CLI, docs, scripts
**Files:** Modify `digest/spec_monitor/cli.py`, `digest/main.py`, `digest/config.yaml.example`, `README.md`, `CLAUDE.md`; Create `docs/architecture/page-watch.md`, `dryrun-page-watch.bat`; Test `tests/test_page_watch_cli.py`.
**Produces:** `cli.run_page_watch_cli(args, config, cache_file, command_line)`; flags `--page-watch`, `--days N` (positive int, only with `--page-watch`); exclusivity with other modes.
- [ ] Tests: `--dry-run` writes only `page-watch-preview.html` (and `.confluence.html` with `--publish-page`), sends nothing; delivery failure exits 1; problems exit 2; `--days` without `--page-watch` is an argparse error; no `page_watch` block raises a clear error.
- [ ] Docs, example config, README, CLAUDE.md pointer, `.bat`.
- [ ] Final: `python -m pytest tests/` green; `python digest/main.py --page-watch --dry-run` sanity run if a `config.yaml` exists.
