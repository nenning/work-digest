# Jira source (`digest/sources/jira.py`)

Loops `config.atlassian.projects`, running one JQL query per project (`config.for_project()`
scopes `AtlassianConfig` to that project's own `url`/API bases) — both for ticket updates
and for new tickets. Each project's `jql_extra` is AND'd only onto that project's own query,
not onto a combined cross-project one.

Ticket updates scope to every ticket matching `project = {key}` (+ `jql_extra`), the same
scope new-ticket detection uses — not just tickets the current user watches in Jira. Per-ticket
changelog via `GET /issue/{key}/changelog`, fetched concurrently across the project's matching
issues (`ThreadPoolExecutor`, up to 8 workers); a single issue's fetch failing is logged and
skipped rather than aborting the whole run. Detects @mentions in ADF.

**Per-ticket priority:** mentions > comments/desc changes > field changes.

**Field-change merging:** changes are merged to initial→final state (net-zero
dropped). Multi-value fields (e.g. `fixVersions`) log a value swap as two
changelog items in the same history — a removal (`toString` empty) and an
addition (`fromString` empty) — which are paired into one net from→to per
history before merging across histories, otherwise a stray removal fragment
can be picked up as the final value instead of what was actually left in
place.

**New tickets** come via a separate JQL query. JQL search paginates via
`nextPageToken`/`isLast` (`POST /rest/api/3/search/jql`).

**Summaries:** `new_ticket` and `field_change` items are formatted directly, without an LLM
call. `mention` items get an action-focused prompt that names who mentioned the user and
what is expected. Content under 100 chars is quoted verbatim; longer content gets a 2–4
sentence summary.

**Email sections:** Jira is grouped into four ordered sections — Erwähnungen (mentions) →
Kommentare & Beschreibungen → Neue Tickets → Feldänderungen. Confluence appears before all
of them.

**Authorship:** merged comment, mention, and field_change items credit every
distinct author involved (comma-joined), not just whoever's edit landed last
— field_change authorship is drawn from the pre-merge per-history entries so
someone's in-window edit to a field still counts even if it was later netted
out as a no-op.
