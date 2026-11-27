# Page exclusion and LLM anonymization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let config exclude Confluence pages (titles auto-annotated in `config.yaml`) and anonymize names, emails and phone numbers in every Jira/Confluence LLM prompt, restoring them in the answer.

**Architecture:** `PageExclusion` hangs off `AtlassianConfig` and is applied in the four Confluence page sources. A new `digest/anonymizer.py` holds a thread-safe `Anonymizer` plus a process-wide gate (`outgoing`/`incoming`) called inside `_call_openai`/`_call_anthropic` (summarizer) and the vision calls; `main.py` sets the gate up once per run.

**Tech Stack:** Python 3.14, `requests`, `pytest`, existing `digest/problems.py`.

**Spec:** `docs/superpowers/specs/2026-11-27-page-exclusion-and-anonymization-design.md`

## Global Constraints

- CLAUDE.md: non-fatal problems go through `digest/problems.py` `report(scope, msg, lossy=)`; every `requests.get/post` passes `timeout=`; spec-monitor HTTP goes through `spec_monitor/http.request_with_retry`; no silent failures.
- Code style: no comments except non-obvious WHY; at most one short line of docstring; terse.
- `config.yaml` rewrites keep every other byte (encoding, line endings, secrets) untouched.
- Placeholders: `PERSON_017`, `EMAIL_003`, `PHONE_002` (numbered per run, zero-padded to 3 digits, unique per person/string).
- Anonymization default off; `anonymize.on_directory_failure` is `fail` (default) or `continue`.
- Teams/Outlook items are never anonymized.
- Confluence `mention` items are never dropped by exclusion.
- Run tests with `python -m pytest tests/`; sanity check with `python digest/main.py --dry-run`.

## Review Focus

- A name split by a non-breaking space, written in other case, or in genitive ("Hans Müllers Ticket") must still be replaced and restored.
- An LLM reply that lowercases, inflects (`PERSON_001s`) or invents a placeholder (`PERSON_999`) must not crash; invented ones are reported and left as is.
- Ticket keys (`PSSA-1234`), ISO dates, versions (`1.2.3.4`) and 8-digit numbers must not be treated as phone numbers.
- `page_watch.parent_page_id` or `spec_monitor.parent_page_id` listed in `exclude_pages.page_ids` is a config error, not an empty report.
- A directory of 5000 users on a 50 KB prompt must anonymize in well under 2 seconds (one combined pattern, not 5000 passes).

---

### Task 1: Config for `exclude_pages` and `anonymize`

**Files:**
- Create: `digest/page_exclude.py`
- Modify: `digest/config.py` (`AtlassianConfig`, `Config`, `load_config`), `digest/config.yaml.example`
- Test: `tests/test_page_exclude.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `digest.page_exclude.PageExclusion` frozen dataclass: `page_ids: frozenset[str] = frozenset()`, `title_patterns: tuple[re.Pattern, ...] = ()`, `is_excluded(page_id: str, title: str, ancestors: Sequence[Mapping] = ()) -> bool` (ancestors are Confluence dicts with `id`/`title`; excluded when the page or any ancestor has an excluded id, or the page or any ancestor title matches a pattern with `re.IGNORECASE` + `search`), `__bool__` true when any rule exists.
  - `digest.page_exclude.load_exclusion(raw: Optional[dict]) -> PageExclusion` (keys `page_ids`, `title_patterns`; ids coerced to `str`; invalid regex raises `ValueError` naming `exclude_pages.title_patterns`).
  - `AtlassianConfig.exclude_pages: PageExclusion` (default empty).
  - `digest.config.AnonymizeConfig` dataclass: `enabled: bool = False`, `additional_names: List[str]`, `on_directory_failure: Literal["fail","continue"] = "fail"`; `Config.anonymize: AnonymizeConfig`.

- [ ] **Step 1: Write failing tests** in `tests/test_page_exclude.py`: `test_excludes_by_id`, `test_excludes_descendant_via_ancestor_id`, `test_excludes_by_title_pattern_case_insensitive`, `test_title_pattern_on_ancestor_excludes_subtree`, `test_empty_exclusion_is_falsy_and_excludes_nothing`, `test_invalid_regex_names_the_config_key` (`pytest.raises(ValueError, match="exclude_pages.title_patterns")`). In `tests/test_config.py`: `test_exclude_pages_and_anonymize_load` (YAML with ids `"123"` and `456` int, one pattern, `anonymize: {enabled: true, additional_names: ["Acme Corp"], on_directory_failure: continue}`; assert `config.atlassian.exclude_pages.page_ids == {"123","456"}` and the anonymize fields), `test_anonymize_defaults_off`, `test_on_directory_failure_must_be_fail_or_continue` (`ValueError` mentioning `anonymize.on_directory_failure`).
- [ ] **Step 2: Run** `python -m pytest tests/test_page_exclude.py tests/test_config.py -q`. Expected: FAIL (module/fields missing).
- [ ] **Step 3: Implement** the signatures above. `load_config` reads top-level `exclude_pages:` into `AtlassianConfig.exclude_pages` and `anonymize:` into `Config.anonymize` (tolerate `null` blocks like the existing `m365` handling). `for_project` already uses `dataclasses.replace`, so the field propagates.
- [ ] **Step 4: Document** both blocks in `digest/config.yaml.example` exactly as in the spec's Config section (block-style `page_ids` list, commented defaults, note that titles are written as `# «Title»` comments on the next run).
- [ ] **Step 5: Run** the same command. Expected: PASS.
- [ ] **Step 6: Commit** `feat: exclude_pages and anonymize config`.

### Task 2: Annotate excluded page ids in `config.yaml`

**Files:**
- Modify: `digest/config_annotate.py`
- Test: `tests/test_config_annotate.py`

**Interfaces:**
- Consumes: existing `annotate_text(text, titles)`, `page_ids(text)`, `_new_comment`, `annotate_config_file`.
- Produces: both functions also handle list items inside a block-style `page_ids:` list (`- "123"`, `- '123'`, `- 123`, optional trailing comment); the `# «Title»` marker and "keep hand-written text behind the marker" rules are the same as for `*page_id:` lines. Flow style (`page_ids: ["1"]`) and `parent_page_id` lines behave as before (the former is left untouched).

- [ ] **Step 1: Write failing tests:** `test_list_item_gets_title` (`"    - \"123\"\n"` → `"    - \"123\"  # «Meeting notes»\n"`), `test_list_item_keeps_hand_written_text` (`- '456'   # «Old» note` with title `Sandbox` → `- '456'   # «Sandbox» note`), `test_bare_int_item_and_crlf_preserved`, `test_items_outside_page_ids_block_untouched` (a `- 123` under `title_patterns:` or another key is not changed and not returned by `page_ids`), `test_flow_style_left_alone`, `test_page_ids_returns_list_items_deduplicated_with_page_id_lines`.
- [ ] **Step 2: Run** `python -m pytest tests/test_config_annotate.py -q`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** a small line scanner: a line matching `^\s*page_ids:\s*(#.*)?$` opens the block; subsequent `^\s*-\s*(["']?)(\d+)\1[ \t]*(#.*)?(eol)` lines are items; a blank or comment-only line keeps the block open, any other line closes it. Reuse `_new_comment` for the comment.
- [ ] **Step 4: Run** the same command. Expected: PASS (existing tests still pass).
- [ ] **Step 5: Commit** `feat: annotate exclude_pages.page_ids with page titles`.

### Task 3: Apply exclusion in the four page sources

**Files:**
- Modify: `digest/sources/confluence.py` (`_fetch_page_updates`), `digest/sources/mgmt_confluence.py` (`fetch_team_pages`, `_search_all` expand), `digest/spec_monitor/confluence_client.py` (`ConfluenceClient.__init__`, `search_descendants`), `digest/spec_monitor/page_watch.py` (`run_page_watch`), `digest/spec_monitor/discovery.py` (`discover_specs`)
- Test: `tests/test_sources_confluence.py`, `tests/test_sources_mgmt_confluence.py`, `tests/test_spec_monitor_confluence.py`, `tests/test_page_watch_runner.py`, `tests/test_spec_monitor_discovery.py`

**Interfaces:**
- Consumes: `AtlassianConfig.exclude_pages.is_excluded(page_id, title, ancestors)` (Task 1).
- Produces: `search_descendants` now also returns `"ancestors": List[dict]` per entry and drops excluded pages; sources drop excluded results.

- [ ] **Step 1: Write failing tests:**
  - personal: `test_page_update_on_excluded_page_skipped_and_reported` (patch `_cql_search` to return two results with `ancestors`; one excluded by id of an ancestor; assert only the other reaches `_fetch_page_diff`, and `problems.take("confluence")` has a non-lossy message containing "excluded" and the count); `test_mention_on_excluded_page_kept`.
  - mgmt: `test_team_pages_excluded_by_title_pattern` (search expand now `version,ancestors`; assert excluded page absent and a `mgmt-confluence` problem reported non-lossy).
  - client: `test_search_descendants_drops_excluded_subtree_and_returns_ancestors`.
  - page-watch/spec: `test_parent_in_exclude_page_ids_is_fatal` (`SpecMonitorFatal` for page-watch, `ValueError` mentioning `exclude_pages` for `discover_specs`), `test_excluded_descendants_do_not_appear_in_report`.
- [ ] **Step 2: Run** those files with `-q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Personal and mgmt: filter results right after the CQL search (mgmt: change `"expand": "version"` to `"version,ancestors"`), then `problems.report(scope, f"{n} page(s) skipped by exclude_pages", lossy=False)` when `n > 0`. `ConfluenceClient` stores `atlassian.exclude_pages`; `search_descendants` requests `expand=version,ancestors`, filters, and prints one line `N page(s) excluded by exclude_pages` when `n > 0` (these modes report on the console, not via problems, because `ReportError` makes the run exit 2). Parent check in `run_page_watch` and `discover_specs` is by id only: `parent_page_id in exclude_pages.page_ids` raises.
- [ ] **Step 4: Run** the same files, then `python -m pytest tests/ -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: apply exclude_pages in digest, mgmt-summary, page-watch and spec-monitor`.

### Task 4: `Anonymizer` core

**Files:**
- Create: `digest/anonymizer.py`
- Test: `tests/test_anonymizer.py`

**Interfaces:**
- Produces:
  - `class Anonymizer`: `__init__(self, names: Iterable[str] = ())`, `add_names(self, names: Iterable[str], allow_single_token: bool = False) -> None`, `anonymize(self, text: str) -> str`, `restore(self, text: str) -> str`, `leftover(self, text: str) -> List[str]` (placeholder-shaped tokens in `text` that this instance never issued). Thread-safe (one lock around the registry and pattern swap).
  - Name rules: names from directory need at least two whitespace-separated tokens (`allow_single_token=True` is for `additional_names`). Variants per name (commas stripped from tokens): the name itself, tokens in both orders, and for exactly two tokens `"B, A"`. Tokens are joined with `\s+` (matches NBSP). Match is case-insensitive, not preceded by a word char, and followed by an optional `s` then a non-word char (genitive); one combined alternation, longest first. Restoring a person yields the first-seen canonical name.
  - Email: `[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}`, keyed lowercase, restored to the first-seen original text.
  - Phone: candidate `(?<![\w+])(?:\+|00)?\d[\d ()./\-]{7,}\d(?!\w)` kept only if 9 to 15 digits and not starting `\d{4}-\d{2}-\d{2}`; keyed by digits (with a leading `+`/`00` normalised), restored to the first-seen original text.
  - Order inside `anonymize`: emails, phones, names. `restore` matches `(?i)(PERSON|EMAIL|PHONE)_(\d+)` (no right boundary, so `PERSON_001s` works) in one pass; unknown numbers are left unchanged.

- [ ] **Step 1: Write failing tests:** `test_round_trip_name_email_phone`; `test_reversed_and_comma_name_variants` (`"Max Mustermann"` matches `Mustermann Max` and `Mustermann, Max`); `test_case_insensitive_and_nbsp` (`"HANS MÜLLER"` replaced); `test_genitive_replaced_and_restored` (`"Hans Müllers Ticket"` → `"PERSON_001s Ticket"` → back); `test_same_person_same_placeholder_two_people_two`; `test_single_token_name_ignored_unless_allowed`; `test_email_before_name` (`hans.mueller@x.ch` becomes `EMAIL_001`, not mangled by name matching); `test_phone_formats_matched` (`+41 79 123 45 67`, `079 123 45 67`, `044/123 45 67`, `+41 (0)79 123 45 67`, `0041791234567`); `test_non_phone_numbers_untouched` (`PSSA-1234`, `2026-11-27`, `2026-11-27 10:50`, `1.2.3.4`, `12345678`); `test_restore_tolerates_case_and_inflection` (`person_001`, `PERSON_001s`); `test_leftover_reports_unissued_placeholder` (`PERSON_999`); `test_thread_safe_same_placeholder_under_concurrency` (ThreadPoolExecutor, 8 threads, same name, one placeholder); `test_5000_names_50kb_prompt_fast` (assert under 2 s).
- [ ] **Step 2: Run** `python -m pytest tests/test_anonymizer.py -q`. Expected: FAIL (no module).
- [ ] **Step 3: Implement** the class per Interfaces. Registry: `dict` original-key to placeholder and placeholder to original, counters per kind under the lock.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: Anonymizer for names, emails and phone numbers`.

### Task 5: User directory, setup and gate state

**Files:**
- Modify: `digest/anonymizer.py`
- Test: `tests/test_anonymizer_setup.py`

**Interfaces:**
- Consumes: `Anonymizer`, `AtlassianConfig`, `Config.anonymize` (Tasks 1, 4), `digest.problems`.
- Produces in `digest/anonymizer.py`:
  - `class DirectoryError(RuntimeError)`.
  - `load_directory(atlassian: AtlassianConfig, auth_header: str) -> List[str]`: for the global site and each distinct per-project `url` override (`atlassian.for_project(p)`), `GET {jira_api_base}/rest/api/3/users/search` with `startAt`/`maxResults=1000`, `timeout=30`, until an empty page (safety cap 50 pages raises `DirectoryError`); keeps users with `accountType == "atlassian"` and a `displayName`; HTTP or network errors raise `DirectoryError`; zero users in total raises `DirectoryError`. Returns de-duplicated display names.
  - `setup(config: Config, auth_header: str) -> Optional[Anonymizer]`: not enabled → `configure(None)`, return `None`. Else build `Anonymizer`, `add_names(additional_names, allow_single_token=True)`, load the directory. On `DirectoryError`: `fail` re-raises; `continue` stores a notice starting `ANONYMIZATION REDUCED:` (directory unavailable, only names seen in fetched items and `anonymize.additional_names` are replaced; emails and phones still are) on the module state. Then `configure(a)`.
  - Gate: `configure(a: Optional[Anonymizer]) -> None`, `outgoing(text: str) -> str`, `incoming(text: Optional[str]) -> Optional[str]` (restores, then reports unissued placeholders once via `problems.report("anonymize", ..., lossy=False)`), `skipped(flag: bool = True)` context manager built on a `ContextVar` (when active, `outgoing`/`incoming` return text unchanged), `note_items(items: Iterable[SourceItem]) -> None` (adds `item.author` split on `", "`, `metadata["mention_authors"]`, `metadata["mention_author"]`, `metadata["assignee"]` as names when a gate is active), `take_notices() -> List[str]` (the stored `ANONYMIZATION REDUCED` notice, kept across calls, plus `problems.take("anonymize")[0]`).

- [ ] **Step 1: Write failing tests** with `requests.get` patched: `test_load_directory_paginates_and_keeps_humans_only` (second page then empty page; an `accountType: "app"` user dropped), `test_load_directory_covers_project_url_overrides`, `test_http_error_raises_directory_error`, `test_zero_humans_raises`, `test_setup_fail_mode_raises`, `test_setup_continue_mode_sets_notice_and_still_anonymizes_emails` (`take_notices()` contains `ANONYMIZATION REDUCED`, twice in a row), `test_setup_disabled_leaves_gate_off`, `test_skipped_context_disables_both_directions`, `test_incoming_reports_unissued_placeholder`, `test_note_items_adds_authors`. Add an autouse fixture that calls `configure(None)` after each test.
- [ ] **Step 2: Run** `python -m pytest tests/test_anonymizer_setup.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat: anonymization user directory, setup and gate`.

### Task 6: Put the gate in every LLM exit

**Files:**
- Modify: `digest/summarizer.py` (`_call_openai`, `_call_anthropic`, `_call_llm_timed`, `_summarize_one_item`), `digest/spec_monitor/vision.py` (`_call_openai`, `_call_anthropic`)
- Test: `tests/test_summarizer.py`, `tests/test_spec_monitor_vision.py`

**Interfaces:**
- Consumes: `anonymizer.outgoing`, `incoming`, `skipped` (Task 5).
- Produces: all text sent to and returned from these calls passes the gate; `spec_monitor/analysis._call` and `synthesize_mgmt_summary` are covered because they call `summarizer._call_*`.

- [ ] **Step 1: Write failing tests** (patch `digest.summarizer.openai` as the existing tests do; `configure(Anonymizer(["Hans Muster"]))`, restore with `configure(None)` in a fixture): `test_prompt_sent_without_name_email_phone_and_reply_restored` (prompt `"Hans Muster, hans@x.ch, +41 79 123 45 67"`; assert the `messages[0]["content"]` passed to `create` contains none of them and the returned text has them back when the mock replies with placeholders), `test_anthropic_path_same`, `test_works_through_call_llm_timed_thread` (the skip flag and gate survive the worker thread), `test_teams_and_outlook_items_sent_unmodified` (via `summarize_items` with `source="teams"` and `"outlook"`; real name present in the payload), `test_jira_item_through_summarize_items_anonymized_end_to_end`. In `test_spec_monitor_vision.py`: `test_vision_text_prompt_anonymized_and_reply_restored` for both providers.
- [ ] **Step 2: Run** the two files. Expected: FAIL.
- [ ] **Step 3: Implement.** At the top of each `_call_*`: `prompt = anonymizer.outgoing(prompt)`; wrap the returned text in `anonymizer.incoming(...)` (handle `None` content). Vision: apply to the final text block only (images untouched). `_call_llm_timed`: run `_target` through `contextvars.copy_context().run` so the skip flag reaches the thread. `_summarize_one_item` and `_summarize_new_ticket`: wrap the body in `with anonymizer.skipped(item.source in ("teams", "outlook")):`.
- [ ] **Step 4: Run** `python -m pytest tests/ -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: anonymize prompts and restore replies at every LLM call`.

### Task 7: Wire setup and notices into the run modes

**Files:**
- Modify: `digest/main.py` (after `annotate_config(config_path, config)` around line 396; `_run` before delivery; `_run_mgmt_summary` near the `mgmt-confluence` notices at line 531), `digest/spec_monitor/cli.py` (`dispatch`)
- Test: `tests/test_main.py`, `tests/test_spec_monitor_cli.py`

**Interfaces:**
- Consumes: `anonymizer.setup`, `take_notices`, `note_items` (Task 5).

- [ ] **Step 1: Write failing tests:** `test_main_sets_up_anonymizer_before_dispatch` (patch `digest.anonymizer.setup`, assert called once with `config` and an auth header, in dry-run too); `test_directory_failure_in_fail_mode_exits_nonzero` (patch `load_directory` to raise, run main, expect non-zero exit / raised error); `test_personal_digest_notices_include_anonymize_notices` (stored `ANONYMIZATION REDUCED` notice appears in the email notices); `test_page_watch_prints_anonymize_notices` (console output contains them).
- [ ] **Step 2: Run** the two files. Expected: FAIL.
- [ ] **Step 3: Implement.** In `main.py` call `anonymizer.setup(config, get_auth_header(config.atlassian))` once, inside the `process_lock` block right after `annotate_config`. In `_run`: `anonymizer.note_items(all_items)` before `summarize_items`, and `notices.extend(anonymizer.take_notices())` after it. In `_run_mgmt_summary`: `note_items` on the fetched Jira items (find with `grep -n "jira_items" digest/main.py`) and extend `notices` beside the `mgmt-confluence` line. In `spec_monitor/cli.dispatch`: after the mode returns, print each `take_notices()` line to stdout (do not add `ReportError`; that would make the run exit 2).
- [ ] **Step 4: Run** `python -m pytest tests/ -q`, then `python digest/main.py --dry-run`. Expected: tests PASS; dry-run completes with `anonymize.enabled` both off and on (when on, the console shows no real names in `--verbose` LLM logs; do not paste secrets).
- [ ] **Step 5: Commit** `feat: wire anonymization setup and notices into all modes`.

### Task 8: Docs and `ideas.md`

**Files:**
- Create: `docs/architecture/anonymize.md`, `ideas.md`
- Modify: `README.md`, `CLAUDE.md`, `docs/architecture/confluence.md`, `docs/architecture/page-watch.md`, `docs/architecture/spec-monitor.md`

- [ ] **Step 1: Write `docs/architecture/anonymize.md`:** gate location, placeholder format, sources of names, the guarantee and its limit (full names, emails, phones; a bare first name or surname is not matched unless listed in `additional_names`), `on_directory_failure`, required Jira scope `read:jira-user` for scoped tokens, Teams/Outlook out of scope, caches keep real text locally.
- [ ] **Step 2: Update** README config reference (both new blocks, block-style `page_ids` requirement for annotation, mentions are not excluded); one paragraph each in the three architecture files on exclusion; CLAUDE.md gets one rule line: every LLM call goes through the anonymization gate in `digest/anonymizer.py`.
- [ ] **Step 3: Create `ideas.md`** with: Presidio/spaCy NER for names outside the directory; Teams/Outlook anonymization; transliteration variants (ü/ue); company/customer name masking; `--show-anonymized` dry-run that prints what the LLM would receive; per-run leak report; local LLM fallback for sensitive items.
- [ ] **Step 4: Verify** `python -m pytest tests/ -q` passes and the README matches `config.yaml.example` key for key.
- [ ] **Step 5: Commit** `docs: page exclusion, anonymization, ideas.md`.
