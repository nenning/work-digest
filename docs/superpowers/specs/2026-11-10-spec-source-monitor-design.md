# Spec source monitor (`--spec-monitor`)

Status: design, awaiting approval. Date: 2026-11-10.

## Intent

The Confluence detail specifications under
`https://yourcompany.atlassian.net/wiki/spaces/RMEG/pages/668860602/Detailspezifikationen`
(page ID 668860602) are derived from *source* pages (other specs, concepts,
ADRs, external standards). Each spec pins the version of every source it was
written against in its section **1.5 Quellen**. When a source moves on, the
spec — and the implementation built from it — is silently out of date.

This mode detects that. For every relevant spec whose sources changed since
the pinned version, it produces:

1. **Change request / management summary** (plain language, for less
   technical readers): the change framed as a change request the user can use
   to justify additional effort. **This part comes first** in every output.
2. **Delta specification** (technical, for the implementer): exactly what to
   change in the existing implementation of that spec, including the open
   points that must be clarified before implementation can start.

Output is one email, optionally also a Confluence page. Both start with a
**table of contents**. Old vs new source state is shown everywhere, and every
place where the source list may be incomplete is called out.

## Goals

- Find specs: only pages that appear in the table on the overview page
  (Page Properties macro with ID `DETAILSPEC`, descendants of 668860602).
- Parse each spec's source list robustly, tolerating the format variants listed
  below, and pin every source to a concrete old version (or date).
- Resolve the "new" version of every source: latest Confluence page version, or
  for external references the latest *approved* version (eCH) — and show
  old vs new side by side.
- Per changed source: diff old vs new, and from the diffs generate the delta
  spec and the change request for the owning spec.
- Report, in the same mail, what was *not* analysed (status filter, excluded
  pages, sources that cannot be monitored) so nothing is silently dropped.
- Warn wherever sources may be incomplete.
- Monitor repo documents (ADRs, API concept in the diagram repo) through an
  optional GitLab connection, API-token based.
- Error tolerant by design: retries for transient failures, failure isolation
  per source/spec, and every error reported transparently in the output.
- Local cache, persisted across runs, to make repeated runs cheap and diffs
  reproducible; entries are reused as long as their source did not change.

## Non-goals / anti-goals

- **Not** creating Jira tickets and **not** updating specs — see Follow-ups.
- **No state between runs.** Every run starts from the Quellen tables as they
  are today. There is no "already reported" memory; the mail is not deduplicated.
  (A cache of fetched page versions is not state in this sense — page versions
  are immutable.)
- **No numbers written to Confluence.** The generated Confluence page has empty
  effort fields the user fills in. The AI estimate appears only in the mail.
- No claim that something is or isn't implemented in code (the spec page is the
  baseline, not the code), and no claim that something is already covered by a
  Jira ticket.
- Anti-goal: silently ignoring a source, or presenting a guessed baseline
  version as if it were stated. Every inferred version is labelled as inferred.
- Anti-goal: hiding a failure. A source that could not be read, a repository
  that was unreachable or an LLM call that failed must appear in the output
  with what was attempted; the run must not abort because of one of them and
  must not pretend the affected part was checked.
- Anti-goal: treating fetched page text as instructions. All fetched content is
  data, wrapped as such in LLM prompts.
- The Atlassian MCP is not used; REST with the existing auth in
  `digest/auth/atlassian.py`.

## Which specs are in scope

Discovery is **configurable** (the user's requirement): a **parent page** plus
one **selector**, of which two kinds are supported now:

- `page_properties_id` (what this overview uses): keep descendants whose storage
  body contains a `<ac:structured-macro ac:name="details">` with parameter
  `id=<value>` (`DETAILSPEC` here — that is what the overview's
  `detailssummary` macro aggregates).
- `label`: keep descendants carrying the given Confluence label
  (CQL `label = "<value>"`). No spec page has a label today (verified via
  `/content/{id}/label`), so this is for when the user adds one.

Candidates always come from `GET /wiki/rest/api/content/search?cql=ancestor=<parent_page_id>
AND type=page` (paginated), then the selector filters. Selector kinds sit
behind a small interface so a further kind can be added later; the config
rejects an unknown kind or a missing value with a clear error. Status is read
from the page-properties table when present; a label-selected page without one
has status `unknown` and is treated as not analysed unless `unknown` is listed
in `analysis_states`.

Observed today (2026-11-10): 8 child pages, 6 with the macro.
`API DosserNode Berechtigungen` (679804930) and `Regeln DossierNode`
(673906744) have no macro → **excluded**, but listed in the report as excluded.

Status comes from the status macro in the page-properties table at the top of
each spec. Values seen: `In Ausarbeitung`, `Refined`, `Angenommen`; the overview
legend lists `In Ausarbeitung`, `Bereit zum Refinement`, `Refined`,
`Abgenommen`, `Abgelöst`. Note the spelling mismatch `Angenommen` (page) vs
`Abgenommen` (legend).

- **Analysed:** status in the configured set. Default (the user's call):
  `Refined`, `Abgenommen`, `Angenommen` (treated as the same approved state).
- **Listed only:** every other status — one line in the report (title, status,
  link), no fetching of sources, no LLM.
- Comparison is case-insensitive and the set is configurable.

Today that means two analysed specs (API CustomerManagement – Refined, Utility
Core-API: Proxy, Ports und Adapter – Angenommen).

## Parsing the source list

Be flexible. Order of attempts per spec:

1. A table whose header contains `Quelle` (columns seen: `Quelle | Gelesene
   Version | Verwendet für`, or `Quelle | Version | Verwendet für`; a trailing
   empty cell is noise). Normally under the heading `1.5 Quellen`, but do not
   depend on the number or the exact heading — find any heading containing
   `Quelle`, then any table with such a header anywhere on the page.
2. If no table: the page's status panel often has a sentence like `Quellen:
   Fachspezifikation X in Seitenversion 35 vom 28.10.2026 …` (seen on
   ReferenceDataService, which has no 1.5 table). Extract with the LLM
   (structured output) and mark the result as lower confidence.
3. If neither: report "no sources found" for that spec.

Each row yields one or more **source references**. Observed row shapes:

| Source cell | Version cell | Meaning |
|---|---|---|
| one Confluence link | `v86, 16. Okt. 2026` / `v258 (09.11.2026)` | page + version (date is informational) |
| one link | `v135` | page + version |
| one link | `v4 (Status Abgenommen)` | page + version, remark |
| several links, comma separated | `v187, v352, v91` | versions paired **by position** with the links in order |
| `Batch Processing, Utility` | `v46, v68` | Batch Processing = v46, Utility = v68 |
| several links | one single `gelesen am 2026-11-05` / `Stand 02.11.2026` / `Stand 30.10.2026` | one date for all → resolve each page to its last version on or before that date |
| link | `nicht erfasst` / `-` / `–` | no baseline stated |
| link | `Entwurf (PROJ-1856)` | no usable version → fall back (below) |
| plain text, no link | e.g. `eCH-0108 Abschnitt 3.1.8 und die i14y-Codeliste der Rechtsformen v1.2.0` | external reference(s) |
| plain text | `API-Konzept, Dokument im Diagramm-Repository (Branch PROJ-1856-…, Commit a206e36, 30.10.2026)` | `Stand 30.10.2026` | repo document (no path given) → GitLab monitor, needs a configured alias |
| plain text | `ADR-0028 «API contract conventions» im Repository (docs/adr/0028-api-contract-conventions.md)` | `Accepted, 03.11.2026` | repo document with path → GitLab monitor |
| external URL with `[SRC-n]` tag | `v2.0, Nov. 2025` / `–` / `1.2` | external document with its own version |

Rules:

- If the number of links and versions in a row differs and the version cell is
  not a single date, do not guess: flag the row `could not pair`.
- **Excluded sources** (not relevant): rows whose Verwendet-für or title marks
  them as working notes (`Notizen …`, `Arbeitsnotizen …`), and the spec's own
  predecessor (`Vorgängerfassung dieser Seite …`). Excluded rows are still shown
  in the report as excluded, with the reason.
- A row's extra text (branch/commit, remark) is kept as `note` for the report.
- Confluence links appear in storage format as `<ac:link><ri:page
  ri:content-title="…"/>` (title, optionally `ri:space-key`) — resolve title →
  page ID via CQL within the space; `<a href>` links may contain the page ID.
- A reference to another detail spec (e.g. API DossierManagement lists API
  CustomerManagement) is an ordinary Confluence source.

### Baseline fallback for sources without a version

In order: the date stated in the row; else the spec's own `Stand:` date from the
status panel (e.g. `Stand: Entwurf zur Klärung, 2026-10-27` for API
CustomerManagement's AutoDeployed API-Konzept row); else the spec page's
creation date. The baseline version is then the source's last version at or
before the **end of that day** (local time). The report marks it
`inferred from <date> (<which rule>)`. A row with a stated version that does
not exist in the page history is flagged, not silently replaced.

## Resolving "new" and monitoring external references

**Confluence pages:** latest version via `GET /content/{id}?expand=version`;
history via `GET /content/{id}/version`; old/new body via
`GET /content/{id}?status=historical&version=n&expand=body.storage`. A page that
is gone or not readable is reported as an error row, not skipped.

**External references.** The user wants to be told when a new version appears
and to see it. Supported monitors (a small registry keyed by recognised
pattern; each returns `current_version`, `status`, `date`, `url`, and the list
of all versions):

- **eCH standards** (`eCH-0108`): `https://www.ech.ch/de/ech/ech-0108` lists all
  versions (verified 2026-11-10: 1.0, 1.1, 2.0, 3.0, 4.0, 5.0, 5.1, 6.0.0 as
  links `/de/ech/ech-0108/<version>`); each version page shows a status
  (e.g. `Genehmigt`) and a decision date (`Beschlussdatum`). **Old** version =
  latest *approved* version at the spec's baseline date (the user's rule; the
  rationale on the example spec was "latest approved on the date of spec
  creation", e.g. 6.0.0). **New** = latest approved version today. If they
  differ, the report shows both with links and decision dates. Content diff of
  the standard itself is out of scope for a first cut (PDFs); the report says
  that a new approved version exists and links it.
- **i14y code lists** (`i14y-Codeliste der Rechtsformen v1.2.0`): best effort —
  the builder checks whether i14y offers a machine-readable version list; if
  yes, monitor it like eCH, otherwise treat as not monitorable.
- Anything else with a version number only in text (Swisspost API `v1.4.0`,
  python-stdnum `1.2`, PDFs, websites such as BFS/LLV pages, FAQ): **not
  monitorable** → listed in the report as `not monitored` with old reference
  and link, so the user can check manually. A generic mode (content hash of a
  URL) is **not** in the first cut. (Left to the builder if cheap.)
- Repo documents / ADRs in git (`docs/adr/…`, diagram repo): monitored through
  the optional GitLab connection (next section); without it, listed as `not
  monitored`.

Registry design lets further monitors be added without touching the pipeline.

### Repository sources (GitLab, optional)

Optional `gitlab` config block (below). Without it, or for a reference that no
configured repository matches, the source is listed as `not monitored` with the
reason. With it, a repo reference is monitored like a page:

- **Recognising a reference:** a source row that contains a repo-relative file
  path (`docs/adr/0028-….md`) and/or names a configured repository
  (`Diagramm-Repository`, `im Repository`). Extra details in the row are
  parsed as baseline hints: `Commit <sha>`, `Branch <name>`, a date, or a status
  remark like `Accepted, 03.11.2026`.
- **Which repository:** the first configured repository whose `match` strings
  occur in the row; otherwise `default_project`; otherwise not monitorable.
- **Which file:** the path in the row; if the row has none (the `API-Konzept`
  row), a configured `documents` alias (`match` text → path); otherwise flagged
  `repo document without path — add an alias in config`.
- **Old state:** the given commit SHA if it exists; else the last commit
  touching the file on or before the end of the stated date (branch given in the
  row, or the default branch if that branch no longer exists — noted); else the
  spec's `Stand:` date (inferred, marked as such).
- **New state:** the latest commit touching the file on the repository's default
  branch. If the row's pinned branch still exists and is ahead, that is added as
  an informational note (it may be unmerged work).
- **Diff:** raw file content at both commits (GitLab REST: `GET
  /api/v4/projects/:id/repository/files/:path/raw?ref=…` and `…/repository/commits?path=…&ref_name=…&until=…`),
  text diff → same cosmetic filter and LLM summarisation as Confluence diffs.
  Embedded `.drawio` files go through the same draw.io diff as attachments.
  Deleted, moved or renamed files are flagged (no rename following).
- **Auth:** `PRIVATE-TOKEN: <api_token>` header, token from config.
- **Unreachable repository** (DNS, VPN, timeout, 5xx after retries, 401/403):
  the run does **not** fail. One reachability probe per repository per run
  (circuit breaker, so a dead host costs one timeout, not one per source); every
  source depending on it is reported as `error: repository not reachable
  (<reason>)` in the sources table and in the errors section.

## Pipeline per analysed spec

1. Fetch spec page (current), parse page properties (status, Jira, `Stand`),
   parse sources.
2. For each source, resolve old and new versions (above).
3. Sources with old == new: record as `unchanged`.
4. For changed sources: fetch old and new body, convert storage XHTML to plain
   text suitable for diffing (CDATA unwrapped before stripping tags; `ac:link`
   rendered as `Text[→TargetPage#anchor]`; table cells separated; macro
   parameters kept), diff, drop cosmetic-only diffs (reuse the existing
   cosmetic-diff logic in `digest/sources/confluence.py`). If only cosmetic →
   source counts as `changed, cosmetic only`.
   Intermediate versions are diffed step by step so each change can be tied to
   a version and date (cap the number of steps; beyond the cap use net diff).
5. **draw.io diagrams** inside source pages: changes in embedded diagrams do not
   appear in the page text. For changed sources, compare the `drawio` macro
   `revision` parameter between old and new storage; if it differs, download
   both attachment revisions and diff semantically (nodes by text, edges as
   source-label-target; layout-only changes ⇒ none). Download path that works
   with API-token auth: `GET
   /wiki/rest/api/content/<pid>/child/attachment/<attId>/download?version=N`
   → 302 to the media API; follow the redirect manually and load the
   `Location` **without** the `Authorization` header. If a diagram cannot be
   diffed, say so in the report ("diagram changed, not compared"). Comparing
   the rendered images with a vision-capable LLM as a fallback is a follow-up
   (see below); keep the result shape open for a `method` of
   `semantic` / `visual` / `none`.
6. LLM, per changed source: summarise the diff (what actually changed in
   content — never page structure or formatting; see the user's existing
   summary-style feedback), together with the source row's `Verwendet für` text
   so the change can be mapped to the part of the spec it affects.
7. LLM, per spec (single call with all its changed-source summaries + the
   current spec text): produce the structured result (below).
8. Large pages: convert and chunk by heading; the spec text given to the LLM is
   limited to sections relevant via `Verwendet für`/requirement IDs where the
   page is too large. Details left to the builder.

### Structured result per spec

- `sources`: list with old ref, new ref, change classification (`changed`,
  `cosmetic only`, `unchanged`, `not monitorable`, `error`, `excluded`), links,
  dates, `inferred` flag.
- `delta_spec`: list of changes, each with: affected spec part (heading and
  requirement/decision IDs such as `CM-FR-03`, `CM-D-11` when present), the
  current statement in the spec, what must change, why (source + old vs new
  quote), and whether it is blocking. Plus `open_points`: questions that must
  be clarified before implementation can start, each with the source change that
  raised it. New, changed and removed requirements are separated.
- `change_request`: plain-language text — starting situation, what changes for
  the customer / the module, what the core must additionally do, impact and risk
  of not doing it. German by default (`language` in config), original English
  technical terms kept (e.g. `requestInProgress`, `createProcess`) with a plain
  explanation next to them; no personal names (roles or organisations only).
- `effort_estimate` (mail only): rough size (T-shirt) and person-day range, with
  a one-line rationale, labelled unvalidated AI estimate. The Confluence page
  gets the matching **empty placeholders** (see Outputs) and never the estimate.
- `completeness_warnings`: see below.

## Completeness warnings

Shown prominently per spec; any of these means "sources may be incomplete":

- The spec's own gap notes: lines such as `Lücken in den Quellen: …`,
  `Nicht gelesen: …`, and the note that pages were read through a Markdown
  conversion so macros and images were lost.
- A changed source page links to further Confluence pages that are **not** in
  the spec's source table. Priority list: links *added in the diff* (the new
  change points at new material); lower priority: other links in the new
  version not in the table (listed collapsed). The builder tunes the noise.
- A source contains a diagram or macro-rendered content that was not compared.
- Rows that could not be parsed/paired, baseline inferred, source unreadable.
- Sources that are not monitorable (external, repo).
- Section-scoped external references (`eCH-0108 Abschnitt 3.1.8`): the section
  reference is shown so the user can check just that part.

## Outputs

**Mail** (one per run; HTML, new Jinja2 template next to the existing ones;
subject prefix configurable):

1. **Table of contents** first (links to each section below).
2. Overview: counts and four lists — analysed with changes, analysed without
   changes, not analysed (status), excluded pages; a banner with the number of
   problems if the run had any.
3. One section per spec with changes, in this order: header (title, status,
   link, Jira), the **source table old vs new** (columns: source, old
   version/date, new version/date, classification, link to both), completeness
   warnings, **change request first** (with the AI effort estimate), then the
   **delta spec** (changes + open points).
4. **Errors and limitations** section (see Error handling).
5. Footer: command line, run timing, cache hits.

**Confluence page** (optional, flag `--publish-page`; the user's "final
option"): a new page under a configured parent, titled `Quellen-Änderungen
<date time>`, storage format, full-width tables, same structure and order as
the mail, starting with a table of contents (Confluence `toc` macro). It has
**no AI estimate**; instead it carries **empty effort placeholders** for the
user to fill in: an overall summary table at the top (one row per spec, empty
effort cells), per spec an empty effort block (size, person-days, remark), and
an empty effort column per delta item with an empty sum row. Each run creates a
new page (stateless). Old vs new source table at the top of every spec section.
Task checkboxes are not required. Page writes use the REST API with a
meaningful version message.

`--dry-run`: no mail, no page; writes the rendered HTML to the cache dir and
prints a console summary.

## Configuration

New optional top-level block in `digest/config.yaml` (example added to
`config.yaml.example`):

```yaml
spec_monitor:
  parent_page_id: "668860602"        # pages are searched below this page
  selector:                          # how a page below it is recognised as a spec
    type: page_properties_id         # or: label
    value: DETAILSPEC                # the macro ID, or the label name
  analysis_states: [Refined, Abgenommen, Angenommen]   # states that get the full analysis
  recipient: ...            # optional, falls back to mgmt_summary.recipient, then email.recipient
  excluded_source_markers: ["Notizen", "Arbeitsnotizen", "Vorgängerfassung"]
  retry:                    # applies to every outgoing call of this mode
    attempts: 3
    backoff_seconds: 2      # doubled per attempt; Retry-After honoured on 429
  confluence_page:
    parent_page_id: "..."   # where the report page is created; required only for --publish-page

gitlab:                     # optional; without it repo sources are "not monitored"
  url: https://gitlab.example.com
  api_token: ...            # read_api scope is enough
  default_project: group/repo          # used for "im Repository" without a name
  repositories:
    - name: Diagramm-Repository
      project: group/diagrams
      default_branch: main
      match: ["Diagramm-Repository"]   # text in a source row that selects this repo
      documents:                        # optional: rows that name a document but no path
        - match: "API-Konzept"
          path: docs/api-konzept.md
```

The Atlassian site/auth come from the existing `atlassian` block (same
`yourcompany.atlassian.net`; the classic/scoped auth handling stays as is). A
spec monitor run needs no M365 token unless mail goes through Graph (same
delivery paths as the other modes: SMTP or Outlook COM draft, via
`email_sender`).

## CLI

`python digest/main.py --spec-monitor [--spec <title or page id>] [--publish-page] [--dry-run] [--verbose]`

- `--spec` restricts to one spec (it must still be in the overview table).
- Mutually exclusive with `--mgmt-summary`. `state.json` is never written.

## Local cache

Directory under `data_dir` (`~/.digest/spec_cache/`), safe to delete anytime:

The cache persists on disk between runs and sessions. An entry is reused
whenever its source did not change; nothing is trusted unchecked.

- Historical page versions (`pageId/vN.storage.html` + text) — immutable,
  cached forever.
- Version lists per page and current-version checks: the live version number is
  checked every run; a changed number invalidates derived entries.
- GitLab files keyed by commit SHA (immutable); the "latest commit on the
  default branch" lookup is repeated every run.
- eCH/i14y version pages with a short TTL.
- Per-diff LLM summaries keyed by a hash of (source texts, prompt version,
  model) so an identical repeated input is never paid for twice, while a
  changed source, prompt or model automatically misses the cache.
- Derived files carry page ID and version in their header.
- The report footer states cache hits/misses; a corrupt or unreadable cache
  entry is treated as a miss, never as an error.

## Error handling

- **Retries:** a shared HTTP helper retries transient failures (connection
  errors, timeouts, HTTP 429 with `Retry-After`, 5xx) per the `retry` config;
  4xx other than 429 are not retried. Every call still passes `timeout=`.
- **Isolation:** the unit of failure is the source reference. One failing source
  does not fail its spec; one failing spec does not fail the run. LLM calls use
  the existing model fallback, one extra attempt on unparseable output (reuse
  the existing parse/salvage logic), then the affected result is marked
  `analysis incomplete` while the source-level facts (old/new versions, links,
  raw diff availability) are still shown.
- **Fatal only when nothing useful can be done:** invalid config, or the parent
  page unreachable after retries (specs cannot be discovered). Then the run
  stops with a non-zero exit code and, where delivery is configured, sends a
  short error mail rather than failing silently.
- **Transparency:** an **Errors and limitations** section (mail and Confluence
  page, listed in the ToC) lists every problem: which source/spec, what was
  attempted, number of retries, last error, and what is therefore missing from
  the analysis. A banner at the top of the overview shows the problem count.
  Console output and `--verbose` logging carry the same detail.
- Partial results are always delivered; nothing is dropped without a line in
  that section.

## Constraints (from the repo)

- Every `requests` call passes `timeout=`; LLM clients follow the existing
  timeout/`max_retries=0` rules in `summarizer.py`; the spec-level synthesis
  call gets the larger timeout like `synthesize_mgmt_summary()`.
- Reuse the LLM abstraction and fallback-model handling in
  `digest/summarizer.py`; do not add a second client stack.
- HTTP(S)-only URL safety in `email_sender.py` applies to all new links.
- Tests use pytest + pytest-mock; all HTTP and LLM calls mocked. The four real
  source-table shapes above should become fixtures (only the row strings, not
  whole pages). Add an architecture doc `docs/architecture/spec-monitor.md` and
  a short README/CLAUDE.md pointer.
- Run Python that reads fetched files with care for encoding on Windows
  (`utf-8` explicitly).

## Follow-ups (built after the first version; see docs/architecture/spec-monitor.md)

1. `--create-ticket <page>`: Jira ticket for one spec's delta (change request,
   delta, open points, old vs new sources; never an effort estimate).
2. `--update-spec <page>`: user decision "for now": creates a NEW sibling
   Confluence page "<title> (aktualisiert <date>)" with the Quellen table and
   text-only affected sections brought up to date; the original page is never
   modified, promotion stays manual.
3. Vision-based draw.io comparison with an image-capable model
   (no separate configuration: the standard `llm`, first `llm.models` entry); switched off for the
   rest of the run once the model turns out not to accept images.
4. Per-version change history for sources that moved several versions.

## Decisions made by me (the user has not confirmed these)

- Output language = `language` from config (German); delta spec and change
  request follow the plain-language rules above.
- `Angenommen` and `Abgenommen` both count as approved.
- Baseline fallback order and "end of day, local time" for date resolution.
- Predecessor-page and working-note rows excluded by marker words.
- draw.io semantic diff included as best effort, with a clear "not compared"
  fallback.
- eCH content itself is not diffed; only new approved version + link.
- Confluence page: one new page per run, timestamped title.
- AI estimate format: T-shirt size + person-day range.
- Cosmetic-only changes are reported as "cosmetic only" without delta/LLM.
- GitLab: "new" state is the default branch; a still-existing pinned branch that
  is ahead is only an informational note. Repo rows without a file path need a
  `documents` alias in config. A GitLab token with `read_api` scope is assumed;
  the GitLab URL and project paths in the example config are placeholders.
- Confluence effort placeholders: summary table, per-spec block and a per-item
  column with a sum row, all empty.
- A fatal discovery failure sends a short error mail and exits non-zero.
- Retry defaults: 3 attempts, 2 s doubling backoff.

## Left to the builder

- Module layout (suggested: `digest/spec_monitor/` or flat modules under
  `digest/sources/`), data-class shapes, prompts, chunking for large pages.
- Exact eCH/i14y scraping details and caching TTLs.
- Exact GitLab calls (commit lookup by date, branch existence), and how repo
  paths in free text are extracted.
- Noise control for the "additional links" warning.
- Template design for mail and storage-format page.
- Test strategy beyond the fixtures above.
