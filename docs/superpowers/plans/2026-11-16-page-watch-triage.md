# Page watch: weekly report, trace hints, Confluence actions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--page-watch` reports the previous calendar week, adds a per-page "already in progress?" hint from Jira and source code, publishes a Confluence page with action checkboxes, and a new `--page-watch-apply` command acts on the ticked boxes (frozen label, Jira ticket, local CR md file).

**Architecture:** The weekly window is an end-bounded version comparison on top of the existing per-page flow. A trace pass (search terms → Jira / local folder / GitLab candidates → LLM judge) runs after the synthesis and fills `PageChange.trace`. The report page carries Confluence task-list items whose ids encode page and action; a JSON attachment on that page holds the data and per-action status; `--page-watch-apply` reads ticks from the page and status from the attachment.

**Tech Stack:** Python 3 (Windows), `requests` via `spec_monitor/http.request_with_retry`, Jira / Confluence REST, GitLab REST v4, existing LLM plumbing (`analysis._ask_json`), pytest + pytest-mock, Jinja2 templates.

**Spec:** `docs/superpowers/specs/2026-11-15-page-watch-triage-design.md` (extends `2026-11-13-page-watch-design.md`). Work on a branch `page-watch-triage`; every commit message ends with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

**Deviation from the spec (decided in this plan):** `freeze_label` sits directly under `page_watch` (default `frozen`), not under `page_watch.trace`, because the `frozen page` box and its hiding must work without a trace block.

## Global Constraints

- CLAUDE.md: no silent failures — every problem is reported (`report.errors` → exit 2), never only `log.warning`; a failed send / publish / action raises or exits non-zero.
- Every HTTP call in `digest/spec_monitor/` goes through `spec_monitor/http.request_with_retry` (which carries the timeout); LLM calls use the configured timeout and model failover via `analysis._ask_json`.
- `~/.digest/state.json` and `url_baselines/` are never read or written by `--page-watch`, `--page-watch-apply`.
- Window default: previous calendar week, ISO week, Monday 00:00 – Sunday 23:59:59 local time; `--days N` / `page_watch.window_days` stay rolling overrides; `--week KW44-2026` selects a past week and is mutually exclusive with `--days`.
- Report page title `Page Watch KW45 2026`; an existing page with that title below the parent is never overwritten (publish refused, exit 1); publishing is refused for rolling windows.
- At most 3 references per page in the hint; hedged wording ("likely already a CR / traces found"); never "no traces found" when any source failed, was not searched, or no search terms were derived.
- Ticket label `pagewatch-<page id>-2026W45`; Jira is searched for it before creating; CR file `<data_dir>/page_watch/CR-2026-KW45.md`, regenerated in full on every apply run.
- Apply without argument processes every report page of the last 8 weeks (week start ≥ today − 56 days) with ticked, not-done boxes; exit 0 all done / nothing to do, 1 fatal, 2 some actions failed.
- Trace pass uses `summary_models` (`spec_llm(config, "summary")`); pages with the freeze label are not checked.

## Review Focus

1. Search terms come back empty → trace status `INCOMPLETE`, never "no traces" (Task 6).
2. Terms containing quotes, backslashes or JQL / Lucene special characters must not break or inject into the JQL (Task 5).
3. Huge or unreadable local folder (`node_modules`, binaries, permission errors, folder missing): bounded, ignored, reported, falls back to GitLab blob search (Task 5b).
4. Box ticked then unticked before apply, and reviewer edits the report page while apply runs (HTTP 409): untouched / retried once, never lost (Task 10).
5. Spec page deleted or renamed after the report was published (label add returns 404): that action is `failed`, reported, other actions continue (Task 10).

---

## File Structure

- Create `digest/spec_monitor/week.py` — `Week`, week arithmetic and parsing.
- Create `digest/spec_monitor/trace_jira.py` — Jira candidate search (+ existing-ticket lookup by label).
- Create `digest/spec_monitor/trace_code.py` — local folder search, GitLab MR / branch / blob search.
- Create `digest/spec_monitor/page_trace.py` — search terms, judge, per-page outcome, `run_trace`.
- Create `digest/spec_monitor/page_watch_data.py` — JSON attachment schema (`WatchData`), task-id scheme, task markup parse / patch helpers.
- Create `digest/spec_monitor/page_watch_apply.py` — `--page-watch-apply`.
- Modify `models.py`, `config.py`, `main.py`, `confluence_client.py`, `confluence_monitor.py`, `page_watch.py`, `page_watch_report.py`, `templates/page_watch.html.j2`, `publish.py`, `ticket.py`, `cli.py` (all under `digest/` resp. `digest/spec_monitor/`).
- Create `page-watch-apply.bat`, `dryrun-page-watch-apply.bat`; modify `README.md`, `CLAUDE.md`, `docs/architecture/page-watch.md`, `digest/config.yaml.example`, `digest/config.yaml` (remove `window_days: 14`).
- Tests (new): `tests/test_page_watch_week.py`, `test_page_watch_trace_jira.py`, `test_page_watch_trace_code.py`, `test_page_watch_trace.py`, `test_page_watch_data.py`, `test_page_watch_apply.py`; extended: `test_page_watch_config.py`, `test_page_watch_cli.py`, `test_page_watch_runner.py`, `test_page_watch_report.py`, `test_spec_monitor_confluence.py`, `test_spec_monitor_publish.py`, `test_spec_monitor_ticket.py`.

Run all tests with `python -m pytest tests/ -q` after each task; the suite must stay green.

---

### Task 1: Weeks, optional `window_days`, `--week`

**Files:**
- Create: `digest/spec_monitor/week.py`
- Modify: `digest/config.py` (`PageWatchConfig`, `_load_page_watch`), `digest/main.py` (argparse ~l.214-231, validation ~l.332-342)
- Test: `tests/test_page_watch_week.py`, `tests/test_page_watch_config.py`, `tests/test_page_watch_cli.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Week: year: int; number: int; start: date; end: date` (Monday, Sunday) with properties `label -> "KW45 2026"`, `title -> "Page Watch KW45 2026"`, `tag -> "2026W45"`, `file_stem -> "CR-2026-KW45"`.
  - `week_of(day: date) -> Week`, `previous_week(today: date) -> Week`, `parse_week(text: str, today: date) -> Week` (accepts `KW44-2026`, `KW44 2026`, case-insensitive; `ValueError` for bad format, week number invalid for that year, or `week.start > today`; the running week is allowed).
  - `PageWatchConfig.window_days: Optional[int] = None` (None = weekly default).
  - `args.week: Optional[str]` (raw text; parsed in `run_page_watch_cli` in Task 2).

- [ ] **Step 1: Write failing tests** in `tests/test_page_watch_week.py`:
  - `test_previous_week_of_a_sunday`: `previous_week(date(2026,11,15))` → start `2026-11-02`, end `2026-11-08`, `label == "KW45 2026"`, `title == "Page Watch KW45 2026"`, `tag == "2026W45"`, `file_stem == "CR-2026-KW45"`.
  - `test_previous_week_across_year_boundary`: `previous_week(date(2027,1,4)).label == "KW53 2026"` (start `2026-12-28`); `previous_week(date(2026,1,5)).label == "KW01 2026"` (start `2025-12-29`). Week numbers are always two digits (`KW01`); the apply step's title regex relies on it.
  - `test_parse_week_forms`: `parse_week("KW44-2026", d)`, `"kw44 2026"` both give start `2026-10-26`.
  - `test_parse_week_rejects`: `"KW99-2026"`, `"2026-44"`, `""` → `ValueError`; `parse_week("KW47-2026", date(2026,11,15))` → `ValueError` (future); `parse_week("KW46-2026", date(2026,11,15))` is accepted (running week).
  - In `test_page_watch_config.py`: loading a block without `window_days` gives `window_days is None`; `window_days: 14` still gives 14; `0` still raises.
  - In `test_page_watch_cli.py` (parametrised invalid/valid flag lists): `--week KW44-2026` valid with `--page-watch`; invalid without it, and invalid together with `--days 3`; an unparsable `--week` value is a parser error.
- [ ] **Step 2:** Run `python -m pytest tests/test_page_watch_week.py tests/test_page_watch_config.py tests/test_page_watch_cli.py -q`. Expected: FAIL (module / option missing).
- [ ] **Step 3:** Implement `week.py` with `datetime.date.isocalendar()` / `date.fromisocalendar()`. In `config.py` change the loader to `window_days=_positive_int(raw["window_days"], ...) if raw.get("window_days") is not None else None` and the dataclass default. In `main.py` add `--week` (`metavar="KWnn-YYYY"`, type validating the format only) with the exclusivity rules next to `--days`; update the `--days` help text (default is the previous calendar week).
- [ ] **Step 4:** Run the same command. Expected: PASS. Run `python -m pytest tests/ -q` — existing page-watch tests that relied on the 14-day default must be adjusted to pass `days=` / `window_days=14` explicitly (this change is expected, not a regression).
- [ ] **Step 5: Commit** — `git add digest tests && git commit -m "page-watch: calendar-week model and --week option"`.

---

### Task 2: End-bounded window through monitor and runner

**Files:**
- Modify: `digest/spec_monitor/models.py` (`SourceRef`, `PageWatchReport`), `digest/spec_monitor/confluence_monitor.py` (`_resolve`), `digest/spec_monitor/page_watch.py`, `digest/spec_monitor/page_watch_report.py` (`_window`, subject / header), `digest/spec_monitor/cli.py` (`run_page_watch_cli`)
- Test: `tests/test_spec_monitor_confluence.py`, `tests/test_page_watch_runner.py`, `tests/test_page_watch_report.py`, `tests/test_page_watch_cli.py`

**Interfaces:**
- Consumes: `Week`, `previous_week`, `parse_week` (Task 1); `pick_version_at(versions, day)`.
- Produces:
  - `SourceRef.target_date: Optional[date] = None` — when set, the "new" state is the last version at or before the end of that day instead of the live version.
  - `PageWatchReport.week: Optional[Week] = None`, `PageWatchReport.window_end: Optional[date] = None` (None = rolling).
  - `run_page_watch(config, *, days=None, week: Optional[Week]=None, command_line="", progress=print, client=None, monitor=None, cache_dir=None, today=None) -> PageWatchReport`. Resolution: explicit `week` > explicit `days` > `pw.window_days` > `previous_week(today)`. Weekly: `bday = week.start - 1 day`, `window_start = week.start`, `window_end = week.end`, `window_days = 7`.

- [ ] **Step 1: Write failing tests:**
  - Monitor (`test_spec_monitor_confluence.py`): `test_target_date_pins_the_new_version` — page has versions 1 (day D−10), 2 (D−3), 3 (D+2), ref has `baseline_date=D−5`, `target_date=D`; result `old.version == "1"`, `new.version == "2"`, diff compares bodies of 1 and 2, `diff_url` uses 1→2. `test_target_date_equal_versions_is_unchanged` (no change inside the window although a later version exists).
  - Runner (`test_page_watch_runner.py`, with `TODAY = date(2026,11,15)`): `test_default_window_is_previous_week` — `report.week.label == "KW45 2026"`, `window_start == date(2026,11,2)`, `window_end == date(2026,11,8)`; `test_days_overrides_the_week` (`week is None`, `window_end is None`); `test_explicit_week_beats_config_window_days`; `test_page_created_after_window_end_is_unchanged_without_error` (first version dated 2026-11-10: status `UNCHANGED`, no errors); `test_new_page_text_is_taken_at_window_end` (page created in the week, edited after Sunday: `NEW` page's text equals the body of the last version ≤ Sunday); `test_weekly_synthesis_label_names_the_week`.
  - Report (`test_page_watch_report.py`): weekly header contains `KW45 2026` and both dates; rolling header unchanged.
  - CLI (`test_page_watch_cli.py`): `--week KW44-2026` reaches `run_page_watch(week=Week(...))`; `--publish-page` with `--days 3` is refused with a clear message before any call (exit via `ValueError` / `SystemExit` consistent with the existing publish-parent check).
- [ ] **Step 2:** Run the four test files. Expected: FAIL.
- [ ] **Step 3: Implement.** `ConfluenceMonitor._resolve`: when `ref.target_date`, take `new` from `pick_version_at(client.list_versions(page_id), ref.target_date)` (page-id check against `get_page` for type stays; `LookupError` if no version at or before the date) and read bodies by version number; everything downstream (`diff_url`, `_fill_steps`) already works from the numbers. `_PageWatch` gets `end_day: Optional[date]`; in `process`, after `list_versions`: no version at or before `end_day` → return `UNCHANGED`; `_new_page` uses `pick_version_at(versions, end_day)` when bounded, else `versions[-1]`; `_changed_page` sets `SourceRef.target_date`. Replace the `window_label(start, today, window_days)` usage by a helper that yields `"2026-11-02 to 2026-11-08 (KW45 2026)"` for weeks and the existing text otherwise. `run_page_watch_cli`: parse `args.week` with `parse_week(text, date.today())` (`ValueError` → message, exit 1 — same style as the other config errors), refuse `--publish-page` when the effective window is rolling (`args.days` or `pw.window_days` set and no `--week`).
- [ ] **Step 4:** Run the four files, then `python -m pytest tests/ -q`. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "page-watch: end-bounded weekly window"` (add new files if any).

---

### Task 3: Labels and label writes

**Files:**
- Modify: `digest/spec_monitor/confluence_client.py`, `digest/spec_monitor/models.py` (`PageChange.labels`), `digest/spec_monitor/page_watch.py`, `digest/config.py` (`PageWatchConfig.freeze_label`)
- Test: `tests/test_spec_monitor_confluence.py` (client), `tests/test_page_watch_runner.py`, `tests/test_page_watch_config.py`

**Interfaces:**
- Produces:
  - `ConfluenceClient.get_labels(page_id: str) -> List[str]` (`GET /content/{id}/label`, paginated through `_paginate`, names only).
  - `ConfluenceClient.add_label(page_id: str, label: str) -> None` (`POST /content/{id}/label`, body `[{"prefix": "global", "name": label}]`; success also when the label already exists; raises `RequestFailed` / `LookupError` on 404 like the other client methods).
  - `PageChange.labels: Optional[List[str]] = None` (None = could not be read).
  - `PageWatchConfig.freeze_label: str = "frozen"` (config key `page_watch.freeze_label`, blank string rejected).
  - `def is_frozen(change: PageChange, freeze_label: str) -> bool` in `page_watch.py` (case-insensitive match against `change.labels`; unknown labels → `False`).

- [ ] **Step 1: Write failing tests:** client `get_labels` returns `["frozen","x"]` from a two-page fake response; `add_label` posts the exact body and treats an "already exists" 200 as success, raises on 404. Runner: `test_labels_are_read_for_changed_and_new_pages_only` (unchanged pages cause no label call); `test_unreadable_labels_are_a_reported_problem` — `labels is None` and `change.errors` names the failure, the page stays `CHANGED`; `test_is_frozen_is_case_insensitive`. Config: `freeze_label` default and override.
- [ ] **Step 2:** Run the three files. Expected: FAIL.
- [ ] **Step 3: Implement** the client methods following the existing `_get` / `_check` / `_paginate` style (a POST helper through `request_with_retry`; see `publish.py` for the header set), fill `labels` at the end of `_PageWatch._summarise` callers (`_new_page`, `_changed_page` when status is CHANGED / NEW), catching `RequestFailed` / `LookupError` into `change.errors`.
- [ ] **Step 4:** Run the files, then the whole suite. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "page-watch: read and write page labels"`.

---

### Task 4: Trace and ticket configuration

**Files:**
- Modify: `digest/config.py` (`GitLabRepoConfig`, `_load_gitlab`, `_load_page_watch`, the place where `Config` is assembled ~l.577, a shared ticket loader), `digest/config.yaml.example`
- Test: `tests/test_page_watch_config.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `GitLabRepoConfig.local_path: Optional[Path] = None` (key `local_path`; not checked for existence at load time — checked at run time, Task 5b).
  - `@dataclass class PageWatchTraceConfig: jira_projects: List[str]; lookback_days: int = 90; repositories: List[str] = []`.
  - `PageWatchConfig.trace: Optional[PageWatchTraceConfig] = None`; `PageWatchConfig.ticket: Optional[TicketConfig] = None`.
  - `def page_watch_ticket(config: Config) -> TicketConfig` — `page_watch.ticket` if present, else `spec_monitor.ticket`, else `ValueError("page_watch.ticket (or spec_monitor.ticket) is required ...")`.

- [ ] **Step 1: Write failing tests:** trace block with `jira_projects` omitted takes the keys of `atlassian.projects` (look at `ProjectJiraConfig` for the key field name); `lookback_days: 0` raises; a repository name not in `gitlab.repositories` raises `ValueError` naming the configured ones; `trace.repositories` without a `gitlab` block raises; `local_path` loads as `Path`; a `gitlab` repo may omit `docs_folder` (already defaults to root — pin it); `page_watch.ticket` loads with the same keys as `spec_monitor.ticket` (`project` required); `page_watch_ticket` precedence and error.
- [ ] **Step 2:** Run the two files. Expected: FAIL.
- [ ] **Step 3: Implement.** Extract the ticket-block parsing from `_load_spec_monitor` (~l.380-400) into `_load_ticket(raw, key_prefix)` and reuse it. Cross-validation (trace repositories vs gitlab, default `jira_projects`) happens where `Config` is assembled, after both `atlassian` and `gitlab` are loaded. Add the commented `trace`, `ticket`, `freeze_label` and `local_path` examples to `config.yaml.example` exactly as in spec §2.
- [ ] **Step 4:** Run the files and the suite. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "config: page_watch trace/ticket blocks and gitlab local_path"`.

---

### Task 5: Jira candidate search

**Files:**
- Modify: `digest/spec_monitor/models.py`
- Create: `digest/spec_monitor/trace_jira.py`
- Test: `tests/test_page_watch_trace_jira.py`

**Interfaces:**
- Produces (models.py):
  - `@dataclass class TraceCandidate: kind: str  # "jira"|"mr"|"branch"|"code"; ref: str; label: str; url: str; detail: str = ""; status: str = ""; own: bool = False`.
  - `class TraceSourceError(Exception)` (message is user-facing; raised when a source cannot be searched).
- Produces (trace_jira.py):
  - `OWN_LABEL_PREFIX = "pagewatch-"`.
  - `def build_jql(terms: List[str], projects: List[str], since: date) -> str`.
  - `def search_issues(atlassian: AtlassianConfig, auth_header: str, retry: RetryConfig, terms, projects, since: date, limit: int = 15) -> List[TraceCandidate]` — `POST {jira_api_base}/rest/api/3/search/jql` through `request_with_retry`, fields `summary,status,updated,labels`; `ref` = issue key, `url` = `{atlassian.url}/browse/KEY`, `status` = status name, `own` = any label starting with `OWN_LABEL_PREFIX`; raises `TraceSourceError` on non-OK.
  - `def find_ticket_by_label(atlassian, auth_header, retry, label: str) -> Optional[str]` — issue key or None (same endpoint, JQL `labels = "<label>"`).

- [ ] **Step 1: Write failing tests** (fake `request_with_retry` via `mocker.patch("digest.spec_monitor.trace_jira.request_with_retry")`, response objects like `FakeResp` in `tests/test_spec_monitor_ticket.py`):
  - `test_build_jql_shape`: result starts with `project in ("PSSA", "RMEG") AND updated >= "2026-08-10"` and joins terms with ` OR ` as `text ~ "…"`, ends with `ORDER BY updated DESC`.
  - `test_terms_are_escaped` (Review Focus 2): terms `['say "hi"', 'a\\b', 'x*y?', 'foo:bar', '(x)']` — the JQL contains no unescaped `"` inside a term, backslashes doubled, Lucene specials `* ? : ( ) [ ] { } ^ ~ ! & |` replaced by spaces; a term that is empty after cleaning is dropped; all terms empty → `ValueError`.
  - `test_candidates_carry_status_and_own_marker`: issue with label `pagewatch-123-2026W44` → `own is True`; others False; `status == "In Progress"`; capped at `limit`.
  - `test_non_ok_raises_trace_source_error` (message contains the HTTP status).
  - `test_find_ticket_by_label_returns_key_or_none`.
- [ ] **Step 2:** Run `python -m pytest tests/test_page_watch_trace_jira.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** as specified; `since` text format `YYYY-MM-DD`. Follow the cursor (`nextPageToken` / `isLast`) only until `limit` candidates are collected.
- [ ] **Step 4:** Run the file. Expected: PASS.
- [ ] **Step 5: Commit** — `git add digest tests && git commit -m "page-watch trace: Jira candidate search"`.

---

### Task 5b: Code candidate search (local folder, GitLab)

**Files:**
- Create: `digest/spec_monitor/trace_code.py`
- Test: `tests/test_page_watch_trace_code.py`

**Interfaces:**
- Consumes: `TraceCandidate`, `TraceSourceError` (Task 5); `GitLabConfig`, `GitLabRepoConfig`, `RetryConfig`.
- Produces:
  - `@dataclass class CodeSearch: candidates: List[TraceCandidate]; searched: List[str]; problems: List[str]`.
  - `def search_local(root: Path, terms: List[str], *, max_candidates: int = 10) -> Tuple[List[TraceCandidate], str]` — returns candidates (kind `code`, `ref` = relative path, `detail` = up to 3 matching lines, ranked by number of distinct terms matched) and a checkout note (branch + last commit date via `git -C <root> log -1 --format=%cI` and `git -C <root> rev-parse --abbrev-ref HEAD` with a 10 s timeout; "no git repository" when git fails). Walk skips `.git`, `node_modules`, `target`, `build`, `dist`, `bin`, `obj`, `.venv`, `vendor`; skips files > 1 MB and binaries (NUL byte in the first 4 KB); unreadable files are counted, not fatal; stops after 20,000 files and reports the cut-off. Raises `TraceSourceError` if `root` is not an existing directory.
  - `class GitLabTrace(gitlab: GitLabConfig, retry: RetryConfig)` with `merge_requests(repo, terms) -> List[TraceCandidate]` (`GET /projects/:id/merge_requests?state=opened&search=<term>&per_page=20`, `ref` = `!<iid>`, `kind` `mr`, `url` = `web_url`, de-duplicated across terms), `branches(repo, terms) -> List[TraceCandidate]` (`GET /repository/branches?search=<term>`, `kind` `branch`, `ref` = branch name, default branch excluded), `blobs(repo, terms) -> List[TraceCandidate]` (`GET /search?scope=blobs&search=<term>&ref=<default_branch>`, `kind` `code`, `ref` = path); non-OK → `TraceSourceError("GitLab <repo>: HTTP <code> ...")`. Header `PRIVATE-TOKEN`; project id quoting as in `gitlab.py::_api`.
  - `def search_code(gitlab: GitLabConfig, repos: List[GitLabRepoConfig], retry: RetryConfig, terms: List[str]) -> CodeSearch` — per repo: `local_path` set and existing → `search_local`; `local_path` set but missing / not a directory → problem "local folder … not found, GitLab search used instead" + `blobs`; no `local_path` → `blobs`; then always `merge_requests` and `branches` (each failure → one problem, the others continue). `searched` gets one human-readable line per source actually searched (repo name, local folder + checkout note, or "GitLab blob search, default branch"; "open merge requests, branches").

- [ ] **Step 1: Write failing tests** (real temp directories via `tmp_path`; GitLab via a patched `request_with_retry`):
  - `test_local_search_ranks_by_distinct_terms` (file with 2 terms ranks above file with 1; `detail` has ≤ 3 lines).
  - `test_local_search_skips_ignored_binary_and_huge_files` (Review Focus 3): `node_modules/x.js`, a file with a NUL byte, a 2 MB file all containing the term → no candidate.
  - `test_unreadable_file_is_counted_not_fatal` (monkeypatch `open` / `Path.read_text` to raise `PermissionError` for one file).
  - `test_file_cap_is_reported` (cap lowered through a module constant `MAX_FILES`).
  - `test_missing_local_folder_falls_back_to_blob_search_with_problem`.
  - `test_checkout_note_for_non_git_folder`.
  - `test_gitlab_merge_requests_and_branches` (dedup, default branch excluded, correct URLs / params) and `test_gitlab_failure_is_one_problem_other_sources_continue` (MR lookup 403 → problem; branch results still returned).
  - `test_searched_lines_name_what_was_searched`.
- [ ] **Step 2:** Run `python -m pytest tests/test_page_watch_trace_code.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Plain `os.walk` with in-place directory pruning; case-insensitive substring match per term; `git` through `subprocess.run(..., timeout=10, capture_output=True, text=True)`.
- [ ] **Step 4:** Run the file. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "page-watch trace: local folder and GitLab search"` (add new files).

---

### Task 6: Trace orchestration (terms, judge, outcome) and runner wiring

**Files:**
- Modify: `digest/spec_monitor/models.py`, `digest/spec_monitor/page_watch.py`, `digest/usage.py` (only if purposes must be registered — check how `source_summary` / `watch_synthesis` are handled and follow it)
- Create: `digest/spec_monitor/page_trace.py`
- Test: `tests/test_page_watch_trace.py`, `tests/test_page_watch_runner.py`

**Interfaces:**
- Consumes: `TraceCandidate`, `TraceSourceError`, `search_issues`, `search_code`, `CodeSearch`, `is_frozen`, `analysis._ask_json`, `spec_llm`, `SpecCache`.
- Produces (models.py):
  - `@dataclass class TraceReference: kind: str; ref: str; label: str; url: str; verdict: str  # in_progress|done_likely|related; reason: str = ""; own: bool = False`.
  - `@dataclass class PageTrace: status: str  # FROZEN|FOUND|NONE|INCOMPLETE; references: List[TraceReference] = []; searched: List[str] = []; problems: List[str] = []`.
  - `PageChange.trace: Optional[PageTrace] = None`; `PageWatchReport.trace_enabled: bool = False`.
- Produces (page_trace.py):
  - `def extract_terms(change: PageChange, deltas: List[DeltaChange], llm: LLMConfig, language: str, cache: SpecCache) -> List[str]` — ≤ 8 terms, de-duplicated case-insensitively, whitespace-trimmed, empty dropped; one cached `_ask_json` call, purpose `trace_terms`.
  - `def judge(change, deltas, candidates: List[TraceCandidate], llm, language, cache) -> Tuple[List[TraceReference], List[str]]` — returns ≤ 3 references plus problems; one cached call, purpose `trace_judge`; the model answers `{"references":[{"ref","verdict","reason"}]}`; a `ref` that matches no candidate is dropped and adds the problem "judge returned unknown reference <ref>"; candidates with `own` get verdict forced to `related` and `own=True`; order: verdict (`in_progress`, `done_likely`, `related`), then kind (`jira`, `mr`, `branch`, `code`).
  - `def trace_page(change, deltas, *, config, llm, cache, auth_header, today: date, window_end: Optional[date]) -> PageTrace`:
    1. `is_frozen` → `PageTrace("FROZEN")`, no calls.
    2. `change.labels is None` → continue but add problem "labels unreadable, freeze state unknown".
    3. terms empty → `INCOMPLETE` with problem "no search terms could be derived" (Review Focus 1).
    4. Jira via `search_issues` (`since = (window_end or today) − lookback_days`) and code via `search_code`; each source failure → problem, other source continues; `searched` collects the lines (Jira projects + lookback; the code lines).
    5. Judge (skipped when no candidates). Result: any non-own reference and no problems → `FOUND`; any problem → `INCOMPLETE` (references that were found are kept); no problem and no non-own reference → `NONE` (own references, if any, stay in `references`).
  - `def run_trace(config, report: PageWatchReport, *, auth_header, today, progress=print) -> None` — loops over `report.with_status(CHANGED, NEW)` in a `ThreadPoolExecutor(pw.workers)`, sets `pc.trace`, appends `ReportError(scope=page title, message=…, impact="the check for existing Jira tickets / code is incomplete for this page; absence of traces is not established")` for every problem; per-page unexpected exceptions are caught into an `INCOMPLETE` trace with a problem (one page never fails the run). Deltas per page come from `report.synthesis.delta_changes` filtered by `page == title` (empty list when the synthesis failed).
  - `run_page_watch` calls `run_trace` after the synthesis when `pw.trace` is set and sets `report.trace_enabled`.

- [ ] **Step 1: Write failing tests** (LLM via `mocker.patch("digest.spec_monitor.page_trace.analysis._ask_json")`, sources via patches of `page_trace.search_issues` / `page_trace.search_code`):
  - `test_frozen_page_makes_no_calls` (patches assert not called, status `FROZEN`).
  - `test_extract_terms_caps_and_dedupes` (10 terms incl. duplicates differing in case → ≤ 8, unique).
  - `test_empty_terms_is_incomplete_not_none` (Review Focus 1; no Jira / code call made).
  - `test_found_with_three_references_max` (5 candidates, model returns 5 → 3 kept in rank order).
  - `test_invented_reference_is_dropped_with_problem`.
  - `test_own_ticket_is_related_and_not_a_finding`: only an own candidate selected → status `NONE`, reference present with `own=True`.
  - `test_no_candidates_and_no_problems_is_none_and_names_what_was_searched`.
  - `test_failed_source_makes_incomplete_even_when_other_source_finds_nothing` (Jira raises `TraceSourceError`, code returns nothing → `INCOMPLETE`, problem text contains the Jira message); `test_failed_source_keeps_found_references`.
  - `test_unexpected_exception_is_contained_per_page`.
  - Runner: `test_trace_runs_after_synthesis_for_changed_pages_only`, `test_no_trace_block_means_trace_disabled`, `test_trace_problems_become_report_errors`.
- [ ] **Step 2:** Run `python -m pytest tests/test_page_watch_trace.py tests/test_page_watch_runner.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Prompts follow the style of `analysis.py` (JSON-only answer, report language, page summary + delta items + candidate list as input, "never invent references; only choose from the list; empty list if none relevant; wording: traces, not proof"), cached with `hash_key(_PROMPT_VERSION, model_key, prompt)` in cache namespaces `llm_trace_terms` / `llm_trace_judge`. `GitLabTrace` and the Jira auth header are created inside `run_trace` / `trace_page`.
- [ ] **Step 4:** Run the two files and the suite. Expected: PASS.
- [ ] **Step 5: Commit** — `git add digest tests && git commit -m "page-watch: trace pass (Jira, code, judge)"`.

---

### Task 7: Hint line in mail and Confluence report

**Files:**
- Modify: `digest/spec_monitor/page_watch_report.py` (`_labels`/label dicts, `_page_row`, `render_confluence_storage`, limitations), `digest/templates/page_watch.html.j2`
- Test: `tests/test_page_watch_report.py`

**Interfaces:**
- Consumes: `PageTrace`, `TraceReference`, `PageWatchReport.trace_enabled`.
- Produces: `_page_row` gains `"trace": Optional[dict]` = `{"status", "headline", "refs": [{"label","url","verdict","reason","own"}], "searched": [str]}` or `None` when `pc.trace is None`; German and English label entries: `trace_found` ("Vermutlich bereits CR / in Arbeit"), `trace_none` ("Keine Hinweise gefunden, dass das bereits in Arbeit ist."), `trace_frozen` ("Seite ist frozen – keine Prüfung"), `trace_incomplete` ("Prüfung nicht (vollständig) möglich"), `trace_searched` ("Geprüft"), `trace_prior_ticket` ("Früheres Delta-Ticket"), verdict labels (`in_progress` "in Arbeit", `done_likely` "vermutlich erledigt", `related` "verwandt"), `trace_off` ("Spurensuche nicht konfiguriert").

- [ ] **Step 1: Write failing tests:** mail and Confluence storage each contain, for a page per status: `FOUND` → headline `Vermutlich bereits CR / in Arbeit` and ≤ 3 reference links with verdict + reason; `NONE` → the exact "Keine Hinweise …" sentence plus the "Geprüft" line (and the prior-ticket line when an own reference exists, still not the found headline); `FROZEN` → frozen text only, no "Geprüft"; `INCOMPLETE` → incomplete headline plus the problem text and never the "Keine Hinweise" sentence; `trace is None` and `trace_enabled False` → no hint line on the page and one "Spurensuche nicht konfiguriert" line in the limitations; English report uses English labels.
- [ ] **Step 2:** Run `python -m pytest tests/test_page_watch_report.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** in both renderers (template block under the summary list; `parts.append(...)` after the page summary in `render_confluence_storage`). Escape everything with the existing `base._e` / `_a` helpers.
- [ ] **Step 4:** Run the file and the suite. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "page-watch report: trace hint per page"`.

---

### Task 8: Data attachment schema and task markup

**Files:**
- Create: `digest/spec_monitor/page_watch_data.py`
- Test: `tests/test_page_watch_data.py`

**Interfaces:**
- Consumes: `PageWatchReport`, `PageChange`, `DeltaChange`, `Week`.
- Produces:
  - `SCHEMA_VERSION = 1`; `ACTIONS = ("frozen", "ticket", "cr")`; `ATTACHMENT_NAME = "page-watch-data.json"`.
  - `@dataclass class ActionState: status: str = "pending"  # pending|done|failed; result: str = ""; date: str = ""; error: str = ""`.
  - `@dataclass class PageEntry: index: int; page_id: str; title: str; url: str; summary: str; deltas: List[dict]; open_points: List[str]; trace: Optional[dict]; show_frozen: bool; actions: Dict[str, ActionState]` (`actions` has `ticket` and `cr` always, `frozen` only when `show_frozen`).
  - `@dataclass class WatchData: schema: int; week_label: str; week_tag: str; file_stem: str; window_start: str; window_end: str; parent_title: str; parent_url: str; language: str; pages: List[PageEntry]`.
  - `def build_data(report: PageWatchReport, freeze_label: str) -> WatchData` — pages are `CHANGED` / `NEW` in report order, `index` 1-based; `show_frozen = not is_frozen(...) and trace.status != "FROZEN"` (unknown labels → shown); raises `ValueError` when `report.week is None`.
  - `def to_json(data: WatchData) -> str`, `def from_json(text: str) -> WatchData` (`ValueError` for invalid JSON, wrong `schema`, missing keys).
  - `def task_id(entry_index: int, action: str) -> int` = `index * 10 + {frozen:1, ticket:2, cr:3}[action]`; `def task_action(task_id: int) -> Tuple[int, str]`.
  - `def parse_tasks(storage: str) -> Dict[int, str]` — `{task_id: "complete"|"incomplete"}` from every `<ac:task>` in the page body.
  - `def render_tasks(entry: PageEntry, labels: Dict[str, str]) -> str` — one `<ac:task-list>` with an `<ac:task>` (`ac:task-id`, `ac:task-status` incomplete, body = action label + link to the spec page) per action that is still `pending` (frozen only when `show_frozen`); done actions render as a `<p>` done line; no task list when nothing is pending.
  - `def mark_task_done(storage: str, task_id: int, body_html: str) -> str` — sets that task's status to `complete` and replaces its body; `ValueError` when the task is not in the page.

- [ ] **Step 1: Write failing tests:** round trip `from_json(to_json(d)) == d`; `from_json` rejects `schema: 2`; `build_data` indexes pages 1..n, hides frozen for a labelled page and for trace `FROZEN`, shows it for `labels None`; `task_id(3,"cr") == 33`, `task_action(21) == (2,"frozen")`... use `task_action(31) == (3, "frozen")`; `parse_tasks` on storage with two lists returns correct states; `render_tasks` omits the frozen task when `show_frozen` False and emits `ac:task-id` 12 and 13 for page index 1; `mark_task_done` changes only the target task (other task and surrounding text byte-identical).
- [ ] **Step 2:** Run `python -m pytest tests/test_page_watch_data.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with `dataclasses.asdict` for JSON and regex / `xml`-free string handling on the task markup (the storage is not well-formed XML because of `ac:` prefixes; follow `textdiff`'s regex style).
- [ ] **Step 4:** Run the file. Expected: PASS.
- [ ] **Step 5: Commit** — `git add digest tests && git commit -m "page-watch: data attachment schema and task markup"`.

---

### Task 9: Publish the weekly report page with boxes and attachment

**Files:**
- Modify: `digest/spec_monitor/publish.py`, `digest/spec_monitor/page_watch_report.py` (`render_confluence_storage(report, *, actions: Optional[WatchData] = None)`), `digest/spec_monitor/cli.py` (`run_page_watch_cli`)
- Test: `tests/test_spec_monitor_publish.py`, `tests/test_page_watch_report.py`, `tests/test_page_watch_cli.py`

**Interfaces:**
- Consumes: `build_data`, `to_json`, `render_tasks`, `ATTACHMENT_NAME`, `Week.title`.
- Produces (publish.py; all through `request_with_retry`, headers as in `publish_page`):
  - `publish_page_detailed(atlassian, auth_header, retry, parent_page_id, title, storage, message) -> Tuple[str, str]` (`(page_id, url)`); `publish_page` becomes a thin wrapper returning the url (spec-monitor behaviour unchanged).
  - `list_child_pages(atlassian, auth_header, retry, parent_page_id) -> List[dict]` (`{"id","title"}`, paginated, `GET /content/{id}/child/page`).
  - `find_child_page(…, parent_page_id, title) -> Optional[str]`.
  - `read_page_storage(atlassian, auth_header, retry, page_id) -> Tuple[str, int, str]` (storage, version number, title).
  - `update_page_storage(atlassian, auth_header, retry, page_id, title, storage, version: int, message: str) -> None` (`PUT /content/{id}`, version `version + 1`; HTTP 409 raises `PageConflict(RuntimeError)`).
  - `put_attachment(atlassian, auth_header, retry, page_id, filename, data: bytes) -> None` (multipart `POST /content/{id}/child/attachment`, header `X-Atlassian-Token: no-check`; if the file exists, `POST /child/attachment/{attId}/data` to replace it) and `get_attachment(…, page_id, filename) -> Optional[bytes]`.
- Behaviour: with `actions` given, each changed-page block of `render_confluence_storage` gets `render_tasks(entry, labels)` under the hint; the section label text per action: `frozen page` / `Jira-Ticket erstellen` / `CR erstellen` (German; English `create Jira ticket` / `create CR`).
- CLI: for `--page-watch --publish-page` on a weekly run: build `WatchData`, title = `report.week.title`; refuse (message + `SystemExit(1)`) when `find_child_page` finds the title, **before** creating anything; after the mail is sent, create the page, then upload `page-watch-data.json`; an attachment failure after the page exists exits 1 with the page URL in the message. `--dry-run` additionally writes `<data_dir>/spec_cache/page-watch-preview.data.json`.

- [ ] **Step 1: Write failing tests:** publish helpers with a fake API (`FakeApi` style from `test_spec_monitor_ticket.py`): `find_child_page` hit / miss (case-insensitive exact title), `list_child_pages` pagination, `update_page_storage` sends `version.number == old + 1` and raises `PageConflict` on 409, `put_attachment` creates vs replaces (existing attachment found by name), `get_attachment` None when absent; `publish_page` still returns just the url. Report: storage with `actions` contains `ac:task-id` 12/13 for page 1 and omits 11 for a frozen page; without `actions` no `ac:task` at all (mail / rolling preview unchanged). CLI: weekly publish uploads the attachment after creating the page; existing title → exit 1 and no page / attachment call; dry-run writes both preview files; mail is sent before the page is created (existing order kept).
- [ ] **Step 2:** Run the three files. Expected: FAIL.
- [ ] **Step 3: Implement** with the existing `_error_text`, `_api`, `_space_key` helpers; keep `_set_full_width`.
- [ ] **Step 4:** Run the files and the suite. Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -am "page-watch: weekly report page with action boxes and data attachment"`.

---

### Task 10: `--page-watch-apply`

**Files:**
- Modify: `digest/spec_monitor/ticket.py` (`create_ticket_detailed` / `_call` accept `tcfg` and `retry`), `digest/spec_monitor/cli.py` (`_dispatch`), `digest/main.py`
- Create: `digest/spec_monitor/page_watch_apply.py`
- Test: `tests/test_page_watch_apply.py`, `tests/test_spec_monitor_ticket.py`, `tests/test_page_watch_cli.py`

**Interfaces:**
- Consumes: Tasks 3, 4, 5 (`find_ticket_by_label`), 8, 9; `TicketDraft`, `ticket._w`, `ticket._wl`.
- Produces:
  - `ticket.create_ticket_detailed(config, draft, *, tcfg: Optional[TicketConfig] = None, retry: Optional[RetryConfig] = None)` — defaults keep today's `spec_monitor.ticket` / `spec_monitor.retry` behaviour (existing tests unchanged).
  - `def build_page_ticket(data: WatchData, entry: PageEntry, tcfg: TicketConfig) -> TicketDraft` — summary `"<title> — Delta-Spezifikation KW45/2026"` (English `Delta specification`), description in Jira wiki markup (page link, window, change summary, delta items with reason and "vor Umsetzung klären" flag, open points, trace hint with references), labels `[*tcfg.labels, f"pagewatch-{entry.page_id}-{data.week_tag}"]`, `spec_url`, `spec_title`.
  - `def write_cr_file(data: WatchData, entries: List[PageEntry], out_dir: Path) -> Path` — writes `<out_dir>/<data.file_stem>.md` (utf-8): week header, one section per entry (title + link, summary, delta items, open points, trace hint + references); returns the path; overwrites.
  - `@dataclass class ApplyResult: page_title: str; done: List[str]; failed: List[str]; skipped: int`.
  - `def run_apply(config, *, target: Optional[str], dry_run: bool, today: Optional[date] = None, progress=print, auth_header=None) -> List[ApplyResult]`; `class ApplyFatal(Exception)`.
  - CLI `--page-watch-apply [TARGET]` (`nargs="?"`, `const=""`), mutually exclusive with every other mode, `--publish-page`, `--days`, `--week`; `--dry-run` allowed. `run_page_watch_apply_cli` maps `ApplyFatal` → stderr message + exit 1, any failed action → exit 2, else 0.

Behaviour of `run_apply`:
- Needs `page_watch.confluence_page.parent_page_id` (else `ApplyFatal`).
- Target: `KW45-2026` → child page titled `Week.title`; digits or a URL containing `/pages/<id>` → that page; none → `list_child_pages` filtered by title regex `^Page Watch KW(\d{2}) (\d{4})$` and week start ≥ `today − 56 days`; a named page that does not exist → `ApplyFatal`.
- Per page: `read_page_storage` + `get_attachment` (missing / invalid attachment → `ApplyFatal` naming the page), `parse_tasks`. An action is due when its task is `complete` in the page and `entries[].actions[a].status != "done"`. Unticked, pending actions are left alone.
- `frozen` → `ConfluenceClient.add_label(page_id, pw.freeze_label)`; result text = label. `ticket` → `find_ticket_by_label` first (found → that key, no creation), else `create_ticket_detailed(..., tcfg=page_watch_ticket(config), retry=pw.retry)`; result = ticket key. `cr` → handled after all pages of this report page: file is regenerated from all entries whose `cr` is ticked or already done (whether or not newly done); the action is done only if the file was written.
- After each action: update the attachment state (`done`, result, date `YYYY-MM-DD`) and `mark_task_done` + `update_page_storage`; on `PageConflict` re-read page, re-apply that single patch once, then record a failed action ("page update conflicted"). A failed action leaves the box ticked and the state `failed` with the error; other actions continue.
- `dry_run`: prints what would be done, performs no write of any kind.

- [ ] **Step 1: Write failing tests** (fakes for the publish helpers via `mocker.patch("digest.spec_monitor.page_watch_apply.<helper>")`, ticket creation patched):
  - `test_nothing_ticked_does_nothing` (no writes, exit-code path 0).
  - `test_frozen_ticked_adds_label_marks_done_and_updates_attachment`.
  - `test_ticket_created_once_and_second_run_does_nothing` (second run: state `done` → no `find_ticket_by_label`, no create).
  - `test_existing_ticket_found_by_label_is_reused_not_recreated` (crash between create and page update).
  - `test_ticket_draft_has_label_summary_and_delta_items` (`build_page_ticket`).
  - `test_cr_file_contains_ticked_and_done_pages_only`: entry A cr done earlier, entry B ticked now, entry C not ticked → file lists A and B; running again rewrites identical content; ticked-then-unticked page not done earlier is absent (Review Focus 4).
  - `test_conflict_is_retried_once_then_reported` (first `update_page_storage` raises `PageConflict`, second succeeds → done; two conflicts → failed, others continue).
  - `test_deleted_spec_page_fails_only_that_action` (`add_label` raises `LookupError`; the ticket action of the same page still runs) (Review Focus 5).
  - `test_default_target_selects_recent_report_pages_only` (titles for weeks older than 56 days and non-matching titles are ignored; a page without ticks is skipped without error).
  - `test_missing_attachment_is_fatal`, `test_named_page_not_found_is_fatal`, `test_missing_publish_parent_is_fatal`.
  - `test_dry_run_writes_nothing`.
  - Ticket module: `create_ticket_detailed(config, draft, tcfg=..., retry=...)` uses the given block and retry while the existing calls keep working; CLI: valid / invalid flag combinations for `--page-watch-apply`, exit codes 0 / 1 / 2.
- [ ] **Step 2:** Run the three files. Expected: FAIL.
- [ ] **Step 3: Implement.** Thread `tcfg` / `retry` through `ticket._call`; one `ConfluenceClient` for label writes (cache dir as in `run_page_watch`); tickets are created sequentially (no thread pool). The attachment is rewritten after every action.
- [ ] **Step 4:** Run the files and the suite. Expected: PASS.
- [ ] **Step 5: Commit** — `git add digest tests && git commit -m "page-watch: --page-watch-apply (label, ticket, CR file)"`.

---

### Task 11: Documentation, examples, launchers, real config

**Files:**
- Modify: `docs/architecture/page-watch.md`, `README.md`, `CLAUDE.md`, `digest/config.yaml.example`, `digest/config.yaml`
- Create: `page-watch-apply.bat`, `dryrun-page-watch-apply.bat`
- Test: `tests/test_main.py` (smoke: `--page-watch-apply --dry-run` is accepted by the parser)

- [ ] **Step 1:** `docs/architecture/page-watch.md`: replace the window text (previous calendar week, end-bounded comparison, `--week`, rolling overrides), add sections *Trace pass*, *Report page and actions*, *Apply*, extend *Configuration* and *Known limits* (the "model has no view of the code" line is replaced by the hedged-hint explanation; trace is traces, not proof; local checkout may be stale; own tickets shown separately). README: new flags `--week`, `--page-watch-apply [TARGET]`, scheduling advice (report weekly on Monday; apply daily or after ticking), write-scope note for scoped tokens (labels, pages, attachments, issues). CLAUDE.md: add `--page-watch-apply` to the list of modes that never write `state.json`, and the `docs/architecture/` pointer text if it names modes.
- [ ] **Step 2:** `config.yaml.example`: `window_days` commented as optional override of the weekly default; `freeze_label`, `trace`, `ticket`, `gitlab…local_path` blocks as in the spec. `digest/config.yaml`: remove the `window_days: 14` line (line ~94) — and tell the user in the final report that the real config was changed.
- [ ] **Step 3:** Launchers: `python "%~dp0run_digest.py" --page-watch-apply %*` and the same with `--dry-run` (same style as `page-watch.bat`).
- [ ] **Step 4:** Run `python -m pytest tests/ -q`, then `python digest/main.py --page-watch --dry-run` and `python digest/main.py --page-watch-apply --dry-run` against the real config. Expected: suite green; the dry runs finish without errors and write `page-watch-preview.html` (the first) and print what would be done (the second); report to the user exactly what was and was not exercised (e.g. no tickets created, no Confluence page published).
- [ ] **Step 5: Commit** — `git add -A && git commit -m "page-watch: docs, config examples, launchers"` (check `git status` first so `digest/config.yaml` is only added if it is tracked).

---

## Self-review notes

- Spec coverage: §1 Task 1–2; §2 Tasks 3–6 (labels, terms, Jira, local/GitLab, judge, outcomes, config); §3 Task 7; §4 Tasks 8–9; §5 Task 10; §6 data model spread over the owning tasks; §7 Task 11 + per-task tests. Spec calls: `--week`, rolling-window publish refusal, apply default of 8 weeks, own tickets → Tasks 1, 2, 9, 10, 6.
- Names checked across tasks: `Week.title/tag/file_stem/label`, `is_frozen`, `TraceCandidate`, `TraceSourceError`, `PageTrace.status` values `FROZEN|FOUND|NONE|INCOMPLETE`, `PageEntry.actions` keys `frozen|ticket|cr`, `task_id` scheme `index*10+{1,2,3}`, `PageConflict`, `ATTACHMENT_NAME`.
