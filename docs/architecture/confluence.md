# Confluence source (`digest/sources/confluence.py`)

Mentions + page updates (CQL); deduplicates per page.

**Per-project grouping:** `_group_by_url()` groups `config.atlassian.projects` by each
project's effective URL (`config.for_project(project).url`), producing one scoped
`AtlassianConfig` per distinct URL rather than per project. Mention search isn't
space-scoped — it's one CQL query per site — so it runs once per URL group. Page-update
search also runs once per URL group, using the union of that group's projects' spaces
(`AtlassianConfig.confluence_spaces`). In the common case, no project overrides `url`, so
there's a single group containing every project — identical to a single flat
`confluence_spaces` list.

**Pagination:** CQL search paginates via the `_links.next` cursor —
Confluence Cloud ignores a client-incremented `start` past page 1 (verified
against a live instance that `start=0/50/100` all return the same first
page).

**Authorship:** `page_update` author is every distinct editor within the
window (comma-joined), not just the current version's — the backward walk to
the pre-window baseline already fetches each intermediate version's metadata
to check its timestamp, so collecting the `by` field along the way is free
(same idea as `mgmt_confluence.py`'s `_walk_version_history`, without the
team-account-id filter).
