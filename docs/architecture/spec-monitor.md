# Spec source monitor (`--spec-monitor`)

Detects Confluence detail specifications whose *sources* changed since the
version each spec pinned in its "Quellen" table, and produces a change request
(plain language, first) plus a delta specification (technical) per affected
spec. Design: `docs/superpowers/specs/2026-11-10-spec-source-monitor-design.md`.

## Data flow

`main.py --spec-monitor [--spec X] [--publish-page] [--dry-run]` →
`runner.run_spec_monitor()`:

1. `discovery.discover_specs()` — candidates are the descendants of
   `spec_monitor.parent_page_id` (CQL `ancestor=…`, paginated); the selector
   (`page_properties_id` macro ID or `label`) keeps the specs, the rest is
   reported as excluded. Failure here (invalid config, parent unreachable
   after retries) is the only fatal case (`SpecMonitorFatal`).
2. Specs are split by status (case-insensitive `analysis_states`) into
   **analysed** and **listed only** (one report line, no fetching, no LLM).
3. Per analysed spec: `parser.parse_sources()` (LLM fallback via
   `find_sources_sentence` when there is no table) → every source ref goes to
   the monitor that `handles` it: `ConfluenceMonitor`, `EchMonitor`,
   `I14yMonitor`, `GitLabMonitor`; excluded rows become `EXCLUDED`, external
   references `NOT_MONITORED`.
4. Each monitor returns a `SourceResult` (`CHANGED` / `COSMETIC_ONLY` /
   `UNCHANGED` / `NOT_MONITORED` / `ERROR`) with old and new `VersionPoint`,
   cosmetic-filtered diff, added links and diagram notes.
5. `analysis.summarize_source_diff()` per `CHANGED` source, then
   `analysis.synthesize_spec()` once per spec with changes: change request,
   delta changes, open points, effort estimate; completeness warnings are
   collected.
6. `report.py` renders the HTML mail (`digest/templates/spec_monitor.html.j2`)
   and the Confluence storage page. Delivery uses the same paths as
   `--mgmt-summary`; `--publish-page` calls `publish.publish_page()`.

`state.json` is never read or written. Every run starts from the Quellen
tables as they are today; there is no "already reported" memory.

## Modules (`digest/spec_monitor/`)

| Module | Role |
|---|---|
| `models.py` | Shared dataclass contract (`SourceRef`, `SourceResult`, `SpecAnalysis`, `RunReport`, …); serialisable on purpose (follow-ups) |
| `http.py` | `request_with_retry()` / `get_json()`; `RequestFailed` |
| `cache.py` | `SpecCache`, `hash_key()` |
| `textdiff.py` | Storage XHTML → plain text, diff, cosmetic filter, link and draw.io revision extraction |
| `drawio.py` | Semantic draw.io diff (nodes by text, edges as source-label-target, layout-only ⇒ none) |
| `parser.py` | Quellen table / sentence → `SourceRef`s, page properties, gap notes |
| `confluence_client.py` | Confluence REST: descendants, versions, historical bodies, attachment download |
| `confluence_monitor.py` | Old/new resolution and diff for Confluence sources |
| `discovery.py` | Spec discovery and selector |
| `ech.py`, `i14y.py` | eCH (latest *approved* version) and i14y monitors |
| `gitlab.py` | Repository documents via GitLab REST |
| `analysis.py` | LLM: per-diff summaries, per-spec synthesis |
| `report.py` | Mail HTML and Confluence storage rendering |
| `publish.py` | Creates the report page |
| `runner.py` | Orchestration |

All monitors share one shape: `handles(ref)` and `resolve(ref, spec)`; `resolve`
never raises.

## Baselines

A stated version is used as is (and flagged when it does not exist in the page
history). Otherwise the baseline is inferred and always labelled
`inferred from <date> (<rule>)`: date in the row, else the spec's `Stand:`
date, else the spec's creation date; the version is the last one at or before
the end of that local day. A row whose link and version counts differ (and
whose version cell is not a single date) is flagged `could not pair` rather
than guessed.

## Configuration

Optional `spec_monitor` and `gitlab` blocks in `config.yaml` (commented
examples in `digest/config.yaml.example`); `atlassian`, `llm`, `email`/`smtp`
and `language` are reused.

- `spec_monitor.parent_page_id`, `selector.type` (`page_properties_id` |
  `label`) and `selector.value` are required; anything else raises `ValueError`
  at load time.
- `--all-states` (CLI flag, not a yaml key) gives every spec the full analysis
  whatever its state (`analysis_states` is then ignored;
  `SpecMonitorConfig.is_analysed()` is the single check used by discovery and
  runner).
- `analysis_states` defaults to `Refined, Abgenommen, Angenommen`;
  `excluded_source_markers` to `Notizen, Arbeitsnotizen, Vorgängerfassung`.
- `retry.attempts` / `retry.backoff_seconds` default to 3 / 2 s.
- `confluence_page.parent_page_id` is needed only for `--publish-page`.
- Timeouts: `retry.timeout_seconds` (default 60) is the per-request HTTP timeout
  of every monitor/client (attachment downloads use double);
  `llm_timeout` (default 240) is the per-call timeout of every LLM call of the
  mode (`config.spec_llm()` hands the standard llm with that value to the
  analysis, rewrite and vision-fallback code).
- Models live in the `llm` section (shared with `--mgmt-summary`): `llm.summary_models` (required;
  cheap per-source diff summaries) and `llm.heavy_models` (optional, default `summary_models`; change
  request / delta synthesis, `--update-spec` section rewrites, source extraction from free text, derived
  vision comparison -- its first entry), each a comma-separated string or YAML list. There are no
  separate fallback models: the other entries of the list are the failover.
  `spec_llm(config, 'heavy'|'summary')` builds the matching llm with this mode's `llm_timeout`;
  `heavy_llm(config.llm)` is the heavy list without it (used by `--mgmt-summary`). The old
  `spec_monitor.models` / `summary_models` / `vision_model` keys are rejected with a hint.
- `workers` (default 4): source lookups and diff summaries of one spec run in a
  thread pool; Confluence lookups in parallel, the eCH/i14y/URL/GitLab monitors
  serialised by one lock each. `1` is strictly sequential.
- `ticket` (only for `--create-ticket`): `project`, `issue_type`, `labels`,
  `priority`, `team_id` (raw id string, written to `team_field`, default
  `customfield_10001`), `parent` (a Feature key), `fields` (extra raw Jira
  fields that can never override the core fields; Story Points, fixVersions and
  Testbarkeit are always dropped), `remote_link` (web link to the Confluence
  page), `link_type`.
- `recipient` falls back to `mgmt_summary.recipient`, then `email.recipient`.
- `gitlab` needs `url` and `api_token` (`read_api` is enough); without the
  block repo sources are listed as `not monitored`.
- Per repository only `name`, `project` (full `group/project` path or numeric
  id — a bare project name or a group is rejected at load time) and the
  `docs_folder` (string or `docs_folders` list, `"."` = repository root) are
  needed. `default_project` is the *name* of the repository used for rows that
  say "im Repository" without naming one (a project path is accepted and
  resolved to the name). `match` defaults to `[name]`; `documents` aliases are
  optional.
- Locating a document: explicit alias → stated path (as is, then below the doc
  folders, then by basename) → search in the doc folders by ADR number
  (`ADR-0028` → `0028-*.md`), normalised file name (`API-Konzept` →
  `API-Konzept.adoc`) and name overlap → GitLab blob search as a last resort.
  Ambiguous matches are reported with the alternatives; nothing found is an
  error row naming the searched folders. The located path is shown in the
  report.

## Error handling

- **Retries:** every outgoing call goes through `request_with_retry()`:
  connection errors, timeouts, truncated bodies, HTTP 429 (numeric
  `Retry-After` honoured, never shortening the backoff) and 5xx are retried
  with a doubling backoff; other 4xx are returned to the caller unretried.
  After the last attempt `RequestFailed` carries the URL, attempt count and
  last error. Every call passes `timeout=`.
- **Isolation:** the unit of failure is the source reference; one failing
  source does not fail its spec and one failing spec does not fail the run.
  A failure becomes an `ERROR` result with the attempted action, attempts and
  last error.
- **Repository circuit breaker:** one reachability probe per repository per
  run; an unreachable repository turns every dependent source into
  `repository not reachable (<reason>)`.
- **LLM:** existing model fallback, one extra attempt on unparseable output,
  then the spec is marked `analysis_incomplete` while the source facts stay
  visible.
- **Big inputs are never cut:** a source diff above 30,000 characters is summarised
  in consecutive chunks and the partial summaries are merged in a further call
  (repeated in rounds if needed). A specification text above 200,000 characters goes
  through the synthesis in consecutive parts (every section included); delta changes
  and open points are concatenated and de-duplicated, the change requests and effort
  estimates are merged by one more call (if that fails the parts are shown one after
  the other, with a warning). URL snapshots and GitLab file listings are stored and
  paged in full; their limits (25 MB, 200,000 entries) are safety ceilings that warn
  when hit.
- **Transparency:** the "Errors and limitations" section (mail and page) lists
  every problem and what is missing because of it; nothing is dropped silently.
- **Publishing:** `publish_page()` raises `RequestFailed` / `RuntimeError` with
  the API's answer. If only the full-width step fails, the page exists and the
  error names its URL.

## Cache

`<data_dir>/spec_cache/`, safe to delete at any time, persisted between runs.
Writes are atomic (unique temp file per write) and safe from several threads.

- Historical page versions (immutable) are cached forever; the live version
  number is checked each run.
- GitLab files are keyed by commit SHA; "latest commit on the default branch"
  is looked up every run.
- eCH/i14y version pages use a short TTL.
- Per-diff LLM summaries are keyed by a hash of source texts, prompt version
  and model.

URL-source baselines are NOT cache: they live in `<data_dir>/url_baselines/`
(persistent state, do not delete). First observation of a source without a
usable stated version records its snapshot, except on `--dry-run`, which
never creates or advances baselines. After a non-dry run reports CHANGED the
baseline is advanced to the new snapshot, so a change is reported once;
COSMETIC_ONLY leaves it untouched. A run whose report is never delivered still
advances it.
- A corrupt, truncated or unreadable entry is a miss, never an error; write
  failures are ignored. Writes are atomic (temp file + replace). Keys that need
  sanitising for the file name get a hash suffix, so distinct keys never share
  a file. Hits and misses are counted and shown in the report footer.

## Confluence page (`--publish-page`)

`publish_page()` resolves the space key from the parent
(`GET /content/{parent}?expand=space`), creates a `page` with `ancestors`,
`body.storage` and a version message, then sets the content properties
`content-appearance-published` and `content-appearance-draft` to `full-width`
(create when the property is missing, `PUT` with version + 1 when it differs).
The page carries empty effort placeholders and never the AI estimate. Each run
creates a new, timestamped page.

## Follow-up modes

All three reuse `runner.run_spec_monitor(only=<spec>)` with `analyse_all_states`
set (the user named the spec explicitly), so the delta is always computed fresh.

- **`--create-ticket <page>`** (`ticket.py`, needs `spec_monitor.ticket`): Jira
  ticket (REST v2, wiki markup) with change request first, old-vs-new source
  table, delta, open points, completeness warnings and errors. Never writes an
  effort estimate. Best-effort issue links to the
  Jira keys named on the spec page. No duplicate check: running it twice creates
  two tickets. If no source is `CHANGED` but sources errored, the errors are printed
  and the exit code is 1; exit 0 only when there are genuinely no changes. `--dry-run` writes
  `<data_dir>/spec_cache/ticket-draft.md` and creates nothing.
- **`--update-spec [page]`** (`spec_update.py`): without a value every analysed
  spec with changes gets its own copy (failures isolated per spec, summary table,
  exit 1 if any failed); with a title, page id or Confluence URL only that spec.
  Never modifies the original.
  It creates a new sibling page (same parent) titled
  `<title> (aktualisiert <YYYY-MM-DD>)` (time appended when the title exists):
  Quellen version cells brought to the latest versions, `Stand` date set to
  today, and text-only sections affected by the delta rewritten by the LLM
  (validated: balanced tags, no requirement ID lost, no macros introduced).
  Sections with macros, rows that are not updatable and unmonitorable sources
  are listed as manual follow-ups in an info banner at the top. The copy's
  `details` macro id gets `-UPDATE` appended so it is not listed in the
  overview nor discovered as a second spec. `--dry-run` writes the preview
  (`spec-update-<pageid>.diff.txt` / `.storage.html`) and creates nothing.
- **Run statistics** (`digest/usage.py`): every spec-monitor mode (`--spec-monitor`,
  `--create-ticket`, `--update-spec`) prints a block at the end, also when the run fails:
  LLM calls per purpose (`source_summary`, `change_request`, `source_extraction`,
  `section_rewrite`, `vision`) and model with calls / failed / unusable replies /
  input and output tokens / average seconds, HTTP requests per host (attempts, retried,
  failed, error statuses) and cache hits per namespace. Collection starts in
  `cli.dispatch()` (`usage.start()`); outside it every recording call is a no-op, so the
  personal digest and management summary are unaffected. Tokens come from the provider's
  `usage` field (shown as `-` / "not reported" when the endpoint omits it). The
  `usage.llm_call(purpose, model)` context manager wraps each call; the low-level clients
  in `summarizer.py` and `vision.py` attribute tokens to it through a thread-local.
  The same text is logged at INFO.
  `--spec-monitor --dry-run` additionally embeds the same tables (`usage.format_html`) as a
  "Run statistics" block above the footer of `preview.html`; the sent mail does not carry them.
- **Visual draw.io comparison** (`vision.py`): fallback when the diagram XML or
  attachment cannot be compared. It uses the standard `llm` (provider, key,
  endpoint) with `llm.vision_model` when set, else the first
  `llm.heavy_models` entry, else the first `llm.summary_models` entry. The reply is
  read from the first `text` block (models that always think emit a thinking
  block first) and `max_tokens` (4000) includes thinking tokens. No `effort`
  is sent, so the model's own default applies. `scripts/compare_vision_models.py`
  runs several models over saved old/new PNG pairs to compare them. When the model turns out not to accept
  images (HTTP 401/403/404 or a "not supported" 400/422), visual comparison is
  switched off for the rest of the run and every later diagram says so; other
  failures only affect the single diagram. Results are labelled AI-generated.
- **Plain images** (`ConfluenceMonitor._compare_images`): every `ac:image` that is an
  attachment of the source page and present in both page versions is compared when its
  attachment changed. The attachment version current at the old page version (from
  `ConfluenceClient.attachment_versions` + `pick_version_before`) is compared with the latest
  one by `vision.compare_images(kind="image")`, whose prompt asks for changes in content
  (text, values, states, elements), not boxes and arrows. The media type is detected from the
  bytes (PNG, JPEG, GIF, WebP; other formats such as SVG and images over 5 MiB are "not
  compared"). At most `MAX_IMAGE_COMPARISONS` (5) images per source are sent to the model;
  the rest, a disabled/unavailable vision model, failed downloads and failed version lookups
  become "image '...' changed, not compared (...)" notes. Results are `diagram_notes` like the
  draw.io ones, so they reach the change section, the delta specification and the
  completeness warnings, and an image-only change keeps the source CHANGED. Images that are
  new or removed show up in the text diff (`[image: name]`). Not covered: images of other
  pages and external URLs (a warning says so) and an attachment re-uploaded under the same
  name without a new page version (the page version is the trigger for every comparison).
  Draw.io previews (`<name>.png`, `<name>.drawio.png`) are skipped here.
- **Per-version history:** for a Confluence source that moved several versions
  the monitor itemises up to 12 intermediate versions
  (`SourceResult.steps`); the LLM summary attributes changes to versions and
  dates; the reports deliberately show no per-version list (version numbers
  and changed-line counts carry no content). Beyond the cap or with an
  unreadable version only the net diff is shown, with a warning.

## Confluence access notes

- Discovery uses CQL `ancestor = <parent>` and follows the `_links.next`
  cursor (`start=` is ignored with `expand`); the pagination aborts discovery
  with an error when the cursor repeats, a page brings no new ids or after 500
  pages. Partial discovery data is never used.
- Databases, whiteboards, embeds and folders have no `body.storage`: such a
  source is reported as changed (versions differ) or unchanged, with a "not
  compared" warning, never as cosmetic.

## Known limits

- eCH and i14y content is not diffed, only the version.
- Sources without a version and without history (plain web pages) use a local
  snapshot as baseline (`url_baselines/`); deleting that directory resets it.
- A repo path counts as found when it has commits on the default branch, even
  if the file was deleted later.
