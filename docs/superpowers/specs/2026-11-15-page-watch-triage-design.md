# Page watch: weekly report, "already in progress" hints, Confluence actions — design

Status: draft for approval, 2026-11-15. Extends `docs/superpowers/specs/2026-11-13-page-watch-design.md`
(`--page-watch`, already implemented: window, discovery, per-page summaries, overall synthesis, mail,
`--publish-page`).

## Intent

The spec pages below "Coremodul" change during the week. For every changed page the reviewer has to decide:
is this already known / in progress, does it need a ticket, does it go into a change request (CR), or is the page
done and should be frozen? Today the report only says *what* changed. This work adds:

1. A **trace pass**: per changed page, look in Jira and in the source code for signs that the change is already in
   progress or done, and say so in the report (at most 3 references per page, marked "likely already a CR"), or say
   that no trace was found. Pages labelled `frozen` skip the check.
2. A **weekly rhythm**: the default window is the previous calendar week (Mon–Sun).
3. A **Confluence report page** per week below a configured parent, with three checkboxes per page. A second
   command acts on the ticked boxes: set the `frozen` label, create a Jira ticket with the delta spec of the page,
   collect the page into a local CR markdown file (basis for one combined CR ticket later).

## Goals / non-goals / anti-goals

Goals
- Reviewer sees per page, in the report, whether traces of implementation exist, with the evidence (Jira key and
  status, merge request, file path), at most 3.
- One click-equivalent (ticking a box) per decision; the tool does the rest on the next apply run.
- Weekly report with stable name (`KW45 2026`), reproducible for a given week.
- Re-running anything never creates duplicates (labels, tickets, files).

Non-goals
- No automatic running of the apply step (it is a CLI command; scheduling is up to the user's Task Scheduler).
- No creation of the combined CR ticket (only the local md file; the ticket is a later step).
- No un-freezing, no ticket updates / comments, no writes to the code repositories, no `git fetch` / `git pull`
  of the local checkout.
- No per-change-line trace check (one check per page).
- No replacement of existing report parts (summaries, synthesis, effort) and no state outside what is named below.

Anti-goals (a failure even if it runs)
- "No traces found" shown when the check could not run or only partly ran. A failed / partial check says so
  ("check not possible: …", "Jira checked, code not") — absence of traces is only claimed for what was searched,
  and the report names what was searched (Jira projects, repositories, age of the local checkout).
- Stating that something *is* implemented. Wording is hedged: "likely already a CR / traces found".
- Creating a ticket twice, or marking an action done that failed.
- Overwriting reviewer edits on the Confluence page.

## Constraints

- CLAUDE.md rules apply: no silent failures (problems reported, per-page isolation, non-zero exit when the run had
  problems), every HTTP call has `timeout=` and goes through `spec_monitor/http.request_with_retry`, LLM clients with
  the configured timeout / failover, big inputs chunked not cut.
- `state.json`, `url_baselines/` untouched. Page watch itself stays stateless: the only things written are the cache,
  the preview/latest HTML, the report page on Confluence (with its data attachment), the CR md file, labels and
  tickets created by the apply step.
- Atlassian token must be allowed to write (labels, pages, attachments, issues); with `auth_type: scoped` the scopes
  include the write scopes — documented in the architecture doc.

## Approaches considered

- **A — apply command reads the ticked boxes from the report page (chosen).** Boxes are Confluence task-list
  items. `--page-watch-apply` reads the page, acts on ticked boxes, rewrites them as "done". No server, runs on the
  same machine / scheduler as everything else, re-runnable. Cost: someone must run the command after ticking.
- B — Confluence automation / webhook triggers the actions on ticking. Immediate, but needs an always-on endpoint
  or an admin-configured rule; not available in this Task-Scheduler setup.
- C — local web form instead of Confluence boxes. Easy, but not where the reviewers work.

Trace search, local folder vs. GitLab (decided with the user): the local folder answers "what exists"
(fast full-text search, no API limits, but only the checked-out branch); the GitLab API answers "what is in
progress" (open merge requests, unmerged branches). Both are used when configured; without a local folder the API
blob search on the default branch replaces the folder search; the API MR / branch lookup always runs when the repo is
reachable.

## Design

### 1. Window: previous calendar week

- Default window: Monday 00:00 – Sunday 23:59:59 (local time) of the **previous** calendar week relative to the run
  date. Run on Sunday 2026-11-15 → 2026-11-02 … 2026-11-08 ("KW45 2026", ISO week). Report header and page title use
  the ISO week and the exact dates.
- `--days N` (and `page_watch.window_days` if set) keeps today's meaning: rolling window of N days up to now.
  Because the existing `config.yaml` has `window_days: 14`, that key has to be removed from the user's real config
  for the weekly default to apply; the example config and docs say that `window_days` is optional and overrides the
  weekly default. (Builder: do this in `digest/config.yaml` too, mention it in the final report.)
- A fixed window has an end. Old state = last version at or before the end of the day before Monday (as today).
  New state = last version at or before Sunday 23:59:59 (a rolling window uses the latest version, i.e. no end).
  `SourceRef` gets an optional end date; `ConfluenceMonitor` / `pick_version_at` use it. Pages whose newest version
  is not newer than the baseline are `UNCHANGED` (as today); pages created after the window end are not part of the
  window (counted, not listed, no error). A page created inside the window is `NEW` with its text at the window end.
  Changes after Sunday show up in the next weekly report.
- Page title: `Page Watch KW45 2026`.
- `--week KW44-2026` selects an explicit past calendar week (same Mon–Sun logic, week must not be in the future),
  for a missed run. Mutually exclusive with `--days`.
- **Week identity.** Title, ticket label, CR file name and apply argument all derive from the ISO week. A rolling
  window (`--days N` / `window_days`) is not a week: mail and `--dry-run` work as before, but `--publish-page` is
  refused with a clear error ("publishing needs a calendar week; use the default or --week"). The apply step only
  ever works on week report pages.

### 2. Trace pass (`digest/spec_monitor/page_trace.py`, new)

Runs after the per-page summaries and the synthesis (so per-page delta items are available), for pages with status
`CHANGED` or `NEW` only, in the existing thread pool, one page at a time per worker. Result per page:
`PageTrace(status, label_hit, references[≤3], searched: [str], problems: [str])` stored on `PageChange`.

Per page:
1. **Labels.** The labels of the page are read (page fetch with labels; also needed to decide whether the
   "frozen" box is shown). If the page has the configured freeze label (default `frozen`): trace status `FROZEN`,
   no Jira / code calls; the hint says "frozen".
2. **Search terms.** One cheap-model call (`summary_models`, cached by page + diff hash) turns the page summary and
   delta items into ≤ 8 search terms: requirement / ticket IDs verbatim, domain nouns, identifier-style names
   (code spelling), German and English variants. No generic words.
3. **Jira.** JQL text search over `page_watch.trace.jira_projects` (default: the project keys of the existing
   `atlassian.projects`) for each term, tickets updated within `lookback_days` (default 90) before the window end,
   any status. Collect key, summary, status, status category, updated date. Cap candidates (≤ 15 per page).
4. **Source.**
   - Local folder (if the repo has `local_path`): search the folder for the terms (skip `.git`, build / vendor
     folders, binaries, files above a size limit); keep file path + a few matching lines per hit; cap candidates.
     The checkout's current branch and last commit date (if it is a git repo) go into `searched`.
   - Without `local_path`: GitLab blob search on the default branch (existing API plumbing).
   - GitLab API always (if the repo is reachable): open merge requests whose title / description match a term,
     and branches whose name matches a term. These are the "in progress" evidence the local folder cannot see.
5. **Judge.** One cheap-model call sees the page change (summary + delta items) and the candidates and returns ≤ 3
   references, each `{kind: jira|mr|branch|code, ref, label, verdict: in_progress|done_likely|related,
   reason (one short line)}`, or an empty list. Candidates the model does not select are dropped; the model may not
   invent references (every returned reference must match a candidate, otherwise it is dropped with a problem).
   Ranking: in progress / done before related; Jira and MR before code.
   Tickets carrying a `pagewatch-*` label (created by the apply step) are kept as candidates but marked as the tool's
   own delta ticket: they never count on their own as "likely already a CR"; the hint shows them as a separate
   reference ("früheres Delta-Ticket PSSA-123, KW45") within the 3-reference limit, and the judge is told which
   candidates are own tickets.
6. **Outcome per page:**
   - `FROZEN` → hint "frozen";
   - references ≥ 1 → hint "Vermutlich bereits CR / in Arbeit" with the references (≤ 3, each with a link:
     Jira browse URL, MR URL, branch URL, file in GitLab or local path);
   - no reference and every source searched without error → "Keine Hinweise gefunden, dass das bereits in Arbeit
     ist." plus the `searched` line (what was checked, local checkout age);
   - any source failed → "Prüfung nicht (vollständig) möglich: …" and the problem goes to the report's error
     table (run exits 2). Never "no traces" in that case, even if the other sources found nothing.

Config (`page_watch.trace`, optional; without it the pass is off and the report says "trace check not configured"
once in the limitations; `freeze_label` sits directly under `page_watch` because the `frozen page` box works
without a trace block):
```yaml
page_watch:
  freeze_label: frozen            # default
  trace:
    jira_projects: [PSSA]         # default: atlassian.projects keys
    lookback_days: 90
    repositories: [application]   # names from gitlab.repositories; the repos whose code is searched
gitlab:
  repositories:
    - name: application
      project: group/application
      local_path: C:\work\application      # NEW, optional; local clone searched first
      # docs_folder stays as today; for a repo used only for tracing it may be omitted (today it defaults to "")
```
`local_path` is validated at load time (must exist when the pass is used; a missing folder at run time is a
reported problem, with fallback to the API blob search for that repo). Everything else follows the existing
`gitlab` validation.

### 3. Report (mail and Confluence)

Each changed / new page block gets a hint line under the summary (see outcomes above). Mail has no boxes. The
synthesis parts are unchanged. Limitations list: trace not configured, local checkout age, failed sources.

### 4. Confluence report page (`--publish-page`, extended)

Exists today (creates a page with empty effort placeholders under `page_watch.confluence_page.parent_page_id`).
Changes:
- Title `Page Watch KW45 2026`. If a page with that title already exists below the parent, publishing is refused
  with a clear error (exit 1, nothing overwritten, nothing lost); the user deletes the page or goes on with the
  existing one. `--dry-run` writes `page-watch-preview.confluence.html` as before.
- Per changed / new page, under the hint, up to three task-list items (storage format `ac:task-list`):
  - `frozen page` — omitted when the page already carries the freeze label (including pages with trace status
    `FROZEN`);
  - `create Jira ticket`;
  - `create CR`.
  Each task body contains a link to the spec page so the apply step can tell which page and action it is.
- **Data attachment.** The structured data the apply step needs (per page: id, title, summary, delta items, trace
  hint and references, window, week; per action: status pending / done / failed with result and date) is stored
  as a JSON attachment of the report page. It must be invisible to page editors, survive page edits and be
  readable from any machine (no local files needed). The attachment is the authority for action status; the page
  body is the authority for "ticked".

### 5. Apply step: `--page-watch-apply`

`python digest/main.py --page-watch-apply [KW45-2026 | <page id or URL>] [--dry-run]`. Without argument: **every
report page below the parent from the last 8 weeks** (found by listing the parent's children whose title matches
`Page Watch KWnn yyyy`), skipping pages with no ticked, not-yet-done box. Reviewers tick during the week after
publishing, so the previous week's page is not the right default; with an argument only that page is processed.
Per page the steps below apply (each week has its own CR file); the exit code is the worst of all pages. Mutually
exclusive with the other modes. Reads no state, writes none of the mail / state files.

For every page block, in this order, for each **ticked, not yet done** box (an action already done is skipped;
done actions stay as text):
- **frozen page** → add label `frozen` (idempotent: label already set counts as done).
- **create Jira ticket** → one ticket per page and week: summary "`<page title> — Delta-Spezifikation KW45/2026`",
  description with the page link, window, change summary, the delta items of this page (with reasons and
  blocking flags), related open points, the trace hint and references. Project / issue type / labels / team /
  parent / extra fields from `page_watch.ticket` (same keys as `spec_monitor.ticket`; falls back to
  `spec_monitor.ticket` if only that exists; neither → refused with a clear error). The ticket carries a label
  `pagewatch-<page id>-2026W45` and a web link to the spec page. Before creating, Jira is searched for that label;
  an existing ticket counts as the result (duplicate protection after a crash between create and page update).
  Reuses `ticket.create_ticket_detailed` and its field handling / rejected-field notes.
- **create CR** → (re)write `<data_dir>/page_watch/CR-2026-KW45.md` with **all** pages of that week whose CR box is
  ticked or already done: per page title + link, change summary, delta items, open points, trace hint with
  references. The whole file is regenerated on every apply run from the attachment + ticked state, so the file is
  idempotent and the md can be edited into the later CR ticket by hand.

After each action the box is rewritten in the page body as a done line ("✔ Label gesetzt 2026-11-16", "✔ Ticket
PSSA-123", "✔ in CR-2026-KW45.md") and the attachment is updated; only the three action lines of a block are
touched, nothing else on the page (reviewer edits stay). A failed action keeps the box ticked, is reported and the
run exits 2 (the next run retries it). A concurrent edit conflict (HTTP 409) on the page update is retried once
after re-reading, then reported. `--dry-run`: shows what would be done, changes nothing.

Exit codes: 0 all ticked actions done / nothing to do; 1 fatal (page not found, parse failure of the page or
attachment); 2 some actions failed.

### 6. Data model, files

`PageChange` gains `labels: List[str]` and `trace: Optional[PageTrace]`; `SourceRef` an optional end date;
`GitLabRepoConfig` optional `local_path`; new `PageWatchTraceConfig`, optional `page_watch.ticket` as `TicketConfig`.
New modules: `page_trace.py` (search + judge), `page_watch_apply.py` (apply step); `publish.py` gains page lookup
by title, body update and attachment helpers (all through `request_with_retry`); `confluence_client.py` gains
label read / add. Report template and Confluence storage renderer get the hint line and the boxes.

### 7. Documentation, tests, scripts

- `docs/architecture/page-watch.md` (new sections: weekly window, trace pass, actions), README flags,
  `config.yaml.example` (`page_watch.trace`, `page_watch.ticket`, `gitlab…local_path`, `window_days` optional),
  CLAUDE.md pointer text if needed, launchers `page-watch-apply.bat` and `dryrun-page-watch-apply.bat`.
- Tests with fakes like the existing ones: weekly window arithmetic (incl. ISO week at year boundary, run on Sunday /
  Monday); end-bounded version pick and page created after the window end; frozen page skips Jira / code; term
  extraction → Jira / local / MR candidates → judge, max 3, invented reference dropped; failed source never yields
  "no traces"; local folder missing → fallback with problem; task-list storage rendering (frozen box hidden for a
  frozen page); apply: each action, idempotency (second run does nothing; ticket found by label), failure keeps the
  box, conflict retry, CR file regenerated; title-exists refusal; config validation.

## Decisions taken without the user (calls)

- Calendar week = ISO week, Monday start, local time; `--days` / `window_days` stay as rolling overrides.
- Check order frozen → Jira → code; one trace check per page, ≤ 3 references in total across Jira / source.
- Local folder for "what exists", GitLab API for "what is in progress"; API blob search only as replacement for a
  missing local folder.
- Existing report page of the same week is never overwritten.
- Apply step is a separate command; boxes become done lines; JSON attachment holds the data.
- One CR md file per week, regenerated in full each apply run.
- Jira ticket per page and week with duplicate protection by label; ticket settings from `page_watch.ticket` with
  fallback to `spec_monitor.ticket`.
- Trace pass uses the cheap models (`summary_models`) for term extraction and judging.
- Frozen pages still appear in the report as changed and keep the Jira / CR boxes.
- Statuses shown for Jira references cover every status (a ticket in "To Do" is also a trace of an existing CR).
- Apply without argument processes all report pages of the last 8 weeks that have open ticks, not "previous week".
- Added `--week KW44-2026` (explicit past week); publishing is refused for rolling windows (`--days`).
- Own `pagewatch-*` tickets are shown as earlier delta tickets, not as independent evidence of a CR.

## Left to the builder

Prompt wording and candidate caps, local search implementation (git-aware or plain walk), ignore lists and size
limits, the exact storage markup of the task items and the done lines, whether the attachment or a content property
carries the data (constraints above), the JQL construction and term escaping, how a rolling window (`--days`)
passes "no end" through the new end-date plumbing, JSON schema of the attachment (versioned), template layout.
