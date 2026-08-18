# Management summary mode (`--mgmt-summary`)

## Data flow

`main.py --mgmt-summary` → resolve time range (sprint/since/from-to) →
`mgmt_jira.fetch_team_tickets()` → derive `team_account_ids` →
`mgmt_confluence.fetch_team_pages()` (diffed page content, incl. brand-new
pages) → `summarizer.summarize_items()` (same per-page 1-2 sentence LLM
summary as the personal digest) → `summarizer.synthesize_mgmt_summary()`
(single LLM call, free-text narrative) → `email_sender.send_mgmt_summary()`.
State is never updated.

`--mgmt-summary` fetches all team tickets via a configurable
`mgmt_summary.jira_jql`, derives team members from assignee/reporter
accountIds (skipping ignored users on either field), fetches Confluence
pages contributed to by those accountIds, then:

- A page's `author` field is a comma-joined list when multiple team members
  edited it.
- Each page's diff is summarized in 1-2 sentences via
  `summarizer.summarize_items()` (identical to personal digest page-update
  handling, including the cosmetic-only skip).
- Jira tickets are **not** individually summarized.
- A final single free-text LLM call (`synthesize_mgmt_summary()`) produces a
  2–3 paragraph narrative from the ticket lists and the per-page summaries.
- The `--assume-done` flag instructs the LLM to present in-progress and todo
  tickets as completed.

## `digest/sources/mgmt_jira.py`

Paginated team ticket fetch (`nextPageToken`); sprint lookup via `GET
/rest/agile/1.0/board/{id}/sprint` (paginates with case-insensitive name
match — requires `mgmt_summary.jira_board_id` in config); kinds:
`ticket_done`/`wip`/`todo`. `ignore_users` is checked against both assignee
and reporter so an ignored account never enters `team_account_ids`.

## `digest/sources/mgmt_confluence.py`

Pages filtered by team accountIds (CQL `contributor in (...)`, scoped to
`confluence_spaces` — unscoped, this CQL clause scans the whole instance and
is slow enough to look like a hang on a wide `--since` range), paginated via
`_links.next` (same cursor scheme as `confluence.py`) with a 200-page safety
cap.

The CQL has no upper `lastModified` bound — that field reflects the page's
*live* latest version, so a page edited again after `until` (by anyone, team
or not) would otherwise drop out of the search entirely and hide a team edit
that happened well inside the window on an earlier version. A single
version-history walk (`_walk_version_history`) instead:

- resolves the latest version at or before `until` itself (skipping any
  later, out-of-window versions),
- collects every distinct team author who edited the page within the window
  (not just the first one found) in the same pass, and
- locates the pre-window baseline version.

`ignore_users` is applied here too. The diff is computed against that
resolved in-window version fetched explicitly by version number, not the
page's current live body — otherwise a report over a past window (`--sprint`,
`--to`) could pick up edits made after `until`.

## Verbose diagnostics

`--verbose` sets logging to DEBUG, enables raw HTTP wire logging
(`http.client.HTTPConnection.debuglevel = 1`) and urllib3 debug logging, and
turns on per-page timing/progress logs in `mgmt_confluence.py`. Added after a
management-summary hang turned out to be Confluence Cloud's CQL search
silently ignoring a client-incremented `start` past page 1.
