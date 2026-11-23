# Page watch (`--page-watch`)

Reports what changed on a Confluence page and all pages below it in the previous calendar week (Mon-Sun), with an
overall summary that correlates the changes across pages, a change request, a delta specification, open points
and a rough effort estimate. Per changed page a hint says whether Jira or the source code show traces that the
change is already in progress or done. `--publish-page` creates a weekly Confluence page with checkboxes and
`--page-watch-apply` acts on the ticked boxes. Designs: `docs/superpowers/specs/2026-11-13-page-watch-design.md`
(report) and `docs/superpowers/specs/2026-11-15-page-watch-triage-design.md` (week, trace pass, actions).

**No state in the report run.** Nothing remembers what was reported before: every run answers "what changed in the
window". A change stays in every report while it is inside the window, which is the "don't forget" mechanism.
`state.json`, `url_baselines/` and the spec-monitor baselines are never read or written; only the cache
(`<data_dir>/spec_cache/`, safe to delete) is used. The only durable state lives on the Confluence report page
(task boxes and the `page-watch-data.json` attachment, see *Report page and actions*).

## Window

Default: the previous calendar week (ISO week, Monday 00:00 - Sunday 23:59:59, local time), e.g. run on Sunday
2026-11-15 -> KW45 2026 (2026-11-02 to 2026-11-08). `--week KW44-2026` selects another week (not in the future).
`--days N` and `page_watch.window_days` give a rolling window of N days up to now instead; such a run is not a
week and cannot be published. A week has an end: a page's old state is its last version at or before the end of the
day before Monday, its new state the last version at or before Sunday (`SourceRef.target_date`). Pages created after
the window end are not part of it; changes after Sunday show up in the next week's report.

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

## Trace pass (`page_trace.py`)

After the synthesis, for every changed or new page (one check per page, in the worker pool):

1. Labels are read (`confluence_client.get_labels`). A page with `page_watch.freeze_label` (default `frozen`) is
   `FROZEN`: no Jira or code calls, the hint says so.
2. A cheap model (`summary_models`, cached) derives up to 8 search terms from the page summary and delta items.
3. Jira (`trace_jira.py`): text search in `trace.jira_projects` (default: the `atlassian.projects` keys) for tickets
   updated within `trace.lookback_days` before the window end, any status.
4. Code (`trace_code.py`), per repository in `trace.repositories`: the optional local clone (`local_path`) is
   searched for what exists; GitLab open merge requests and branches are always searched (what is in progress, which
   a checkout cannot show); without `local_path` (or when the folder is missing, which is a reported problem) the
   GitLab blob search on the default branch replaces the folder search.
5. A cheap model picks at most 3 references from the candidates (it can not invent any; unknown references are
   dropped and reported). Tickets carrying a `pagewatch-*` label were created by `--page-watch-apply`: they are shown
   as "earlier delta ticket" and never count on their own as "likely already a CR".

Outcomes: `FOUND` (hint "Vermutlich bereits CR / in Arbeit" with the references), `NONE` ("Keine Hinweise gefunden"
plus what was searched, including the local checkout's branch and last commit date), `FROZEN`, `INCOMPLETE` (a source
failed, was not searched or no search terms could be derived: the hint says the check was not possible, the problem
goes to the error table, exit 2; absence of traces is never claimed then). The hint is a trace, not proof, and a stale
local checkout can miss recent work.

## Report page and actions

`--publish-page` (weekly runs only) creates `Page Watch KW45 2026` under `page_watch.confluence_page.parent_page_id`
after the mail was sent. An existing page of that week is never overwritten (exit 1, nothing created). Per changed
page the page carries task-list boxes (task id = page index * 10 + 1 frozen, 2 ticket, 3 CR, 4 reject):
`reject change` (first, marks a change that would have to be reverted; ignored by the apply step and not stored in the data),
`frozen page` (hidden when the page already has the label), `create Jira ticket`, `create CR`. The attachment
`page-watch-data.json` (`page_watch_data.py`, schema version 1) holds per page the summary, delta items, open points,
trace result and the status of every action (`pending` / `done` / `failed`).

`--page-watch-apply [KW45-2026 | page id or URL] [--dry-run]` (`page_watch_apply.py`) reads the report page(s) (default:
every report page of the last 8 weeks with ticked, not-done boxes), and for each ticked box:

- `frozen`: adds the freeze label (idempotent).
- `ticket`: one Jira ticket per page and week (summary "<page> - Delta-Spezifikation KW45/2026", description with
  summary, delta items, open points and trace hint), label `pagewatch-<page id>-2026W45`, web link to the spec page,
  fields from `page_watch.ticket` (fallback `spec_monitor.ticket`). Jira is searched for the label first, so a crash
  between creating the ticket and updating the page never produces a duplicate.
- `cr`: regenerates `<data_dir>/page_watch/CR-2026-KW45.md` with all pages of that week whose CR box is ticked or
  already done (basis for the combined CR ticket created later, by hand).

Done boxes are rewritten as done lines and the attachment is updated after every action; reviewer edits elsewhere on
the page stay (HTTP 409 on the page update: re-read and retried once). A failed action keeps its box ticked, is
reported and retried by the next run. Exit 0 all done / nothing to do, 1 fatal (page or attachment unreadable, parent
not configured), 2 some actions failed. The Atlassian token needs write access (labels, pages, attachments, issues).

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
- Launchers in the repo root: `page-watch.bat` (standard run, always with `--publish-page`), `dryrun-page-watch.bat`.

## Configuration (`page_watch` block, independent of `spec_monitor`)

`parent_page_id` (required), `window_days` (optional: rolling window instead of the previous week), `label`,
`include_parent` (true), `recipient`, `workers` (4), `llm_timeout` (240), `retry`, `confluence_page.parent_page_id`,
`freeze_label` (`frozen`), `trace` (`jira_projects`, `lookback_days` 90, `repositories`: names from
`gitlab.repositories`; without the block the trace pass is off and the report says so), `ticket` (same keys as
`spec_monitor.ticket`, for `--page-watch-apply`). `gitlab.repositories[].local_path` is the optional local clone. `spec_llm()` / `effective_vision_llm()` use
`spec_monitor.llm_timeout` when that block exists, else `page_watch.llm_timeout`. Models come from the shared
`llm` section (`summary_models` for page summaries, `heavy_models` for the synthesis).

## Known limits

- Deleted or moved-away pages are not detected (no baseline to compare the page list against).
- The window has day granularity (the baseline is the end of a day).
- A page created and deleted inside the window is not seen; a page created inside the window is `NEW`.
- The page-summary model has no view of the code; only the trace pass looks at Jira and code, and its hint is a trace,
  not proof (a stale local checkout or a differently worded ticket is missed).
