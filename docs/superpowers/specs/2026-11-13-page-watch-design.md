# Page watch (`--page-watch`) — design

Status: draft for approval, 2026-11-13.

## Intent

A Confluence page ("Coremodul", `https://easygov-seco.atlassian.net/wiki/spaces/RMEG/pages/41484320/Coremodul`)
and all pages below it are the specification the core implements. When they change, the change has to reach the
code. The new mode `--page-watch` looks back over a configurable window (default 14 days), finds what changed on
the page tree and produces, in one mail:

1. what changed per page,
2. one overall summary that correlates the changes across pages (many belong together),
3. a change request (Änderungsantrag), a delta specification (Delta-Spezifikation) for the implementer, open
   points and a rough additional-effort estimate (Mehraufwand) — all derived from the overall picture, not page
   by page.

The delta specification is the "don't forget" mechanism: as long as a change is inside the window it is in every
report. Nothing remembers what was reported before.

## Goals / non-goals / anti-goals

Goals
- Rolling window, no state: every run answers "what changed in the last N days".
- Reuse the spec-monitor machinery (discovery, version history, diff, cosmetic filter, draw.io / image
  comparison, cache, LLM plumbing, mail / Confluence page delivery, usage statistics).
- Configurable: watched page, window, optional label filter, recipient.

Non-goals
- No baseline store, no acknowledgement (`--ack`), no "already reported" memory, no read or write of
  `~/.digest/state.json` or `url_baselines/`.
- No Quellen-table logic, no external sources (eCH, i14y, GitLab, URLs).
- No Jira ticket creation and no spec rewrite for this mode (the existing `--create-ticket` / `--update-spec`
  are about source changes and stay as they are).
- One watched page tree per config (a list of watches is a possible later extension).
- Deleted or moved-away pages are not detected (no baseline to compare the page list against); documented limit.

Anti-goals (would make it a failure even if it runs)
- A "no changes" mail when pages could not be read. Errors are never hidden behind "unchanged".
- Delta items or effort that describe document structure / formatting instead of content.
- A claim that something is or is not implemented in code (the model has no view of the code).

## Constraints (from CLAUDE.md and the existing spec monitor)

- Writes no state anywhere except the cache (`<data_dir>/spec_cache/`, safe to delete).
- Problems: no silent failures; per-page failure is isolated (`ERROR` result and report row); discovery failure
  is fatal (mail with the error, exit 1); a run with problems exits 2 after delivering.
- Every HTTP call has a timeout and goes through `spec_monitor/http.request_with_retry`; LLM calls use the
  configured timeout and model failover, no extra retry policy.
- Big inputs are never cut: chunk and merge like `analysis.synthesize_spec` does.
- Per-diff summaries and the synthesis are labelled AI-generated; the Confluence page carries empty effort
  placeholders, never the AI estimate (as in `--spec-monitor`).

## Approaches considered

- **A — new mode inside `digest/spec_monitor/`, each page treated as its own source with a date baseline
  (chosen).** `ConfluenceMonitor.resolve` already supports `SourceRef.baseline_date`: it picks the last version at
  or before the end of that day and gives diff, cosmetic filter, diagram / image comparison and per-version steps.
  A new orchestration, a cross-page synthesis, a report template and CLI wiring sit on top.
- **B — build on the digest source `sources/confluence.py::_fetch_page_updates`.** Private, space-wide,
  bound to the digest's `SourceItem`, no diagram / image comparison, and the synthesis would still be new.
  Rejected.
- **C — use `--spec-monitor` unchanged.** Needs Quellen tables and compares against pinned versions, has no
  window; not applicable.

## Design

### CLI

`python digest/main.py --page-watch [--days N] [--publish-page] [--dry-run] [--verbose]`

- Mutually exclusive with `--mgmt-summary`, `--spec-monitor`, `--create-ticket`, `--update-spec`.
- `--days N` overrides `page_watch.window_days`; `--publish-page` needs `page_watch.confluence_page.parent_page_id`.
- Dispatch through `spec_monitor.cli.dispatch()` so run statistics (`digest/usage.py`) are printed.
- `--dry-run`: no mail, no page; writes `<data_dir>/spec_cache/page-watch-preview.html` (with the run statistics
  block, like `preview.html`) and, with `--publish-page`, `page-watch-preview.confluence.html`.
- Delivery, recipient fallback (`page_watch.recipient` → `spec_monitor.recipient` → `mgmt_summary.recipient` →
  `email.recipient`), subject prefix and the fatal-error mail follow `spec_monitor/cli.py`; the two delivery
  helpers are reused, not copied.

### Configuration (`page_watch` block, optional, independent of `spec_monitor`)

```yaml
page_watch:
  parent_page_id: "41484320"      # required; this page and all descendants are watched
  window_days: 14                 # default 14, >= 1
  label: ""                       # optional: only descendants carrying this label (the parent is always included)
  include_parent: true            # default true
  recipient: ""                   # optional fallback chain as above
  workers: 4                      # default 4
  llm_timeout: 240                # default 240
  retry: {attempts: 3, backoff_seconds: 2, timeout_seconds: 60}
  confluence_page: {parent_page_id: ""}   # only for --publish-page
```

Invalid values raise `ValueError` at load time. Models come from the shared `llm` section (`summary_models` for
page summaries, `heavy_models` for the synthesis), like the spec monitor.

### Flow (`digest/spec_monitor/page_watch.py`, orchestration)

1. **Window.** `baseline_day = today(local) − window_days`. A page's old state is its last version created at or
   before the end of `baseline_day` (what `pick_version_at` does). With 14 days on 2026-11-13 the window covers
   changes from 2026-10-31 00:00 until now (14 calendar days including today). Report header states the exact
   boundary.
2. **Discovery.** `client.search_descendants(parent_page_id, label)` plus the parent page itself. Pages whose
   current version is not newer than the end of `baseline_day` are `UNCHANGED` without further calls (the search
   already expands `version`); if `version.when` is missing for a page it is resolved normally. Discovery failure
   (parent unreachable after retries) is `SpecMonitorFatal`; an unreadable single page is a report error.
3. **Per page.** A synthetic `SourceRef(kind=CONFLUENCE, page_id, baseline_date=baseline_day)` goes through
   `ConfluenceMonitor.resolve` → `SourceResult` (`CHANGED` / `COSMETIC_ONLY` / `UNCHANGED` / `ERROR`). Pages are
   resolved in the existing thread pool (`workers`). The `spec` argument of `resolve` is a minimal `SpecInfo`
   (the page itself); the link-suggestion code that reads a Quellen table must be a no-op for it.
   - **New page** (no version at or before the baseline): reported as `NEW` with its whole text as the change;
     this is handled in `page_watch.py` before `resolve`, which would fail with "page was created later".
   - Changed pages get `analysis.summarize_source_diff` (cheap models, cached, chunked) with a page-flavoured
     prompt (no "source of a specification" wording); per-version attribution (`steps`) is kept, as today. A diff
     the model judges cosmetic is demoted to `COSMETIC_ONLY` and listed with a warning, like in `_analyse_spec`.
4. **Overall synthesis** (`digest/spec_monitor/watch_analysis.py`, heavy models) over all changed / new pages in
   one input (per-page summary + cosmetic-filtered diff text, page title, hierarchy path, window). One JSON
   answer:
   - `overview`: the Gesamtzusammenfassung, plain language;
   - `themes`: `[{title, summary, pages: [page titles]}]`, groups of changes that belong together (a page may be in
     several themes; pages without relation form their own theme);
   - `change_request` (same structure and rules as today: starting point, what changes for the customer / module,
     what the core must additionally do, impact if not done; English technical terms kept with an explanation; no
     personal names; no statements about implementation or tickets);
   - `delta_changes`: as `DeltaChange` plus the page the change comes from (new optional field `page`), requirement
     IDs only when they appear in the page text, `reason` citing the version / date;
   - `open_points`: as today;
   - `effort`: T-shirt size + person-day range + rationale for the **whole** set of changes (themes, not pages).
   Over the size limit the input is split into consecutive parts by page, the parts are synthesised separately and
   merged by one more call (`overview`, `themes`, `change_request`, `effort`; `delta_changes` and `open_points`
   concatenated and de-duplicated) — the mechanism and failure handling of `_merge_syntheses`, with the same
   "show the parts one after the other plus a warning" fallback. Synthesis failure → `analysis_incomplete`, page
   facts stay visible.
5. **Report** (`digest/spec_monitor/page_watch_report.py` + `digest/templates/page_watch.html.j2`, mail and
   Confluence storage rendering). Order:
   1. header: watched page, window (exact dates), counts (pages checked / changed / new / errors);
   2. per changed or new page: title + link (Confluence diff link old→new), what changed (summary, attributed to
      versions / dates), draw.io / image notes, and the delta items that belong to this page;
   3. at the end: Gesamtzusammenfassung with the themes, Änderungsantrag, open points, Mehraufwand (AI estimate in
      the mail, labelled; empty placeholders on the Confluence page);
   4. errors and limitations (every failed page / call and what is missing because of it), unchanged page count,
      cosmetic-only pages, cache footer.
   If nothing changed in the window the mail says so explicitly with the window and the page count. It is still
   sent (a scheduled job must not be silent).
6. **Exit codes.** 0 clean; 1 fatal / delivery failed; 2 delivered with problems (same convention as
   `--spec-monitor`).

### Data model

New dataclasses in `models.py` (serialisable like the rest): `PageChange` (page info + `SourceResult` + status
`CHANGED|NEW|COSMETIC_ONLY|UNCHANGED|ERROR` + page summary + hierarchy path), `Theme`, `WatchSynthesis`,
`PageWatchReport`. `DeltaChange` gains `page: str = ""` (default keeps all current code unchanged).

### Documentation, tests, scripts

- `docs/architecture/page-watch.md` (and the pointer list in `CLAUDE.md`), `README.md` flags, commented
  `page_watch` example in `digest/config.yaml.example`, `dryrun-page-watch.bat` next to the other dry-run launchers.
- Tests in `tests/` with the same fakes as the spec-monitor tests: window / baseline-day computation; unchanged
  skip; new page; cosmetic-only; per-page error isolation; fatal discovery; synthesis parse + merge across parts;
  report rendering (changed, none, errors); CLI exclusivity and `--dry-run` writes nothing but the preview and
  the cache; config validation.

## Decisions taken without the user (calls)

- Window boundary at day granularity (reuse of `baseline_date`) instead of an exact timestamp.
- Parent page included in the watch (`include_parent: true`).
- Per-page delta items shown under their page, overall parts at the end.
- Mail is sent even when nothing changed.
- `page_watch` is an independent config block with its own `retry` / `workers` / `llm_timeout` (same defaults)
  so it works without a `spec_monitor` block; `spec_llm()` / `effective_vision_llm()` currently read
  `config.spec_monitor` and need to accept the page-watch values.
- A single watch per config; a list of watches is not built now.

## Left to the builder

Prompt wording and size limits (follow the existing prompts and the 30,000 / 200,000-character chunking), exact
template layout (follow `spec_monitor.html.j2`), module split inside the files named above, how `SpecInfo` is
minimally faked for `ConfluenceMonitor.resolve`.
