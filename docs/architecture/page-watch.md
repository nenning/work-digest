# Page watch (`--page-watch`)

Reports what changed on a Confluence page and all pages below it in the last N days (default 14), with an
overall summary that correlates the changes across pages, a change request, a delta specification, open points
and a rough effort estimate. Design: `docs/superpowers/specs/2026-11-13-page-watch-design.md`.

**No state.** Nothing remembers what was reported before: every run answers "what changed in the window". A
change stays in every report while it is inside the window, which is the "don't forget" mechanism. `state.json`,
`url_baselines/` and the spec-monitor baselines are never read or written; only the cache
(`<data_dir>/spec_cache/`, safe to delete) is used.

## Data flow

`main.py --page-watch [--days N] [--publish-page] [--dry-run]` → `spec_monitor/cli.dispatch()` →
`run_page_watch_cli()` → `page_watch.run_page_watch()`:

1. **Window.** `baseline_day = today − window_days`. A page's old state is its last version created at or before
   the end of that day (`pick_version_at`); with 14 days on 2026-11-13 the window covers changes from 2026-10-31
   00:00 on (14 calendar days, today included).
2. **Discovery.** The parent page (`get_page`) plus `search_descendants(parent, label)`. Failure here is
   `SpecMonitorFatal` (error mail, exit 1). Pages whose current version is not newer than the end of
   `baseline_day` are `UNCHANGED` without further calls (the search result carries `version.when`; if it is
   missing the page is resolved normally).
3. **Per page** (thread pool, `workers`): `ConfluenceMonitor.resolve()` with a synthetic `SourceRef`
   (`baseline_date = baseline_day`) gives the cosmetic-filtered diff, draw.io / image notes and per-version steps.
   A page with no version at or before the baseline is `NEW`: its whole text is the change. A failing page is an
   `ERROR` row; the others continue. `analysis.summarize_source_diff(..., page_mode=True)` summarises the diff; a
   diff the model judges cosmetic is demoted to `COSMETIC_ONLY` with a warning.
4. **Overall synthesis** (`watch_analysis.synthesize_watch`, heavy models) over all changed and new pages: overview
   (Gesamtzusammenfassung), themes (changes that belong together, also across pages), change request, delta
   changes (each with the page it comes from), open points and one effort estimate for the whole set. Input above
   150,000 characters is split by page, synthesised per part and merged by one more call; failure of the merge
   shows the parts one after the other with a warning. Failure of the synthesis marks the run
   `analysis_incomplete`; the per-page facts stay.
5. **Report** (`page_watch_report.py`, `templates/page_watch.html.j2`): header with the exact window, counts,
   per changed / new page its summary (one change per line, no version numbers or dates), notes and delta items,
   then (at the end) Gesamtzusammenfassung, themes, Änderungsantrag, effort (AI estimate, labelled), delta items
   without a matching page, open points, analysis notes; errors and limitations. Unchanged and cosmetic-only
   pages are only counted, never listed. draw.io node/edge lines go to the model only; the report shows its
   reading of them plus limitation notes ("not compared", visual AI comparison). The delta flag
   `blocking` is shown as "vor Umsetzung klären" (a gap in the spec prevents starting). Nothing changed → the
   mail still goes out and says so.

## Delivery and exit codes

- Mail (recipient: `page_watch.recipient` → `spec_monitor.recipient` → `mgmt_summary.recipient` →
  `email.recipient`), optional `--publish-page` (needs `page_watch.confluence_page.parent_page_id`; the page carries
  empty effort placeholders, never the AI estimate).
- `--dry-run`: no mail, no page; `<data_dir>/spec_cache/page-watch-preview.html` (with the run statistics) and
  with `--publish-page` `page-watch-preview.confluence.html`.
- After a delivered run the mail HTML is kept as `<data_dir>/spec_cache/page-watch-latest.html`.
- Exit 0 clean; 1 fatal or delivery failed; 2 delivered with problems (see the error table).
- Run statistics (`digest/usage.py`) are printed like for the other spec-monitor modes; LLM purposes
  `source_summary` (page summaries) and `watch_synthesis`.
- Launchers in the repo root: `page-watch.bat` (standard run), `dryrun-page-watch.bat`.

## Configuration (`page_watch` block, independent of `spec_monitor`)

`parent_page_id` (required), `window_days` (14), `label`, `include_parent` (true), `recipient`, `workers` (4),
`llm_timeout` (240), `retry`, `confluence_page.parent_page_id`. `spec_llm()` / `effective_vision_llm()` use
`spec_monitor.llm_timeout` when that block exists, else `page_watch.llm_timeout`. Models come from the shared
`llm` section (`summary_models` for page summaries, `heavy_models` for the synthesis).

## Known limits

- Deleted or moved-away pages are not detected (no baseline to compare the page list against).
- The window has day granularity (the baseline is the end of a day).
- A page created and deleted inside the window is not seen; a page created inside the window is `NEW`.
- The model has no view of the code: it never says whether something is already implemented.
