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

**Summaries:** page-update diffs are summarized by the LLM (content under 100 chars is
quoted verbatim, longer gets 2–4 sentences). Cosmetic-only diffs return
`{"summary": null}` and the item is skipped.

**Authorship:** `page_update` author is every distinct editor within the
window (comma-joined), not just the current version's — the backward walk to
the pre-window baseline already fetches each intermediate version's metadata
to check its timestamp, so collecting the `by` field along the way is free
(same idea as `mgmt_confluence.py`'s `_walk_version_history`, without the
team-account-id filter).

**Exclusion:** `exclude_pages` (`digest/page_exclude.py`, config: top-level `exclude_pages.page_ids` /
`title_patterns`) drops page-update results whose own id or any ancestor's id matches, or whose own or any ancestor's
title matches a case-insensitive regex (the search already `expand`s `ancestors`), so a subtree goes with its root.
The same filter runs in `mgmt_confluence.py` and in `ConfluenceClient.search_descendants`. Mention items are not
filtered. The dropped count is a non-lossy problem (`confluence` / `mgmt-confluence`), shown as an email notice.
`config_annotate.annotate_config()` runs at startup and writes `# «Title»` behind every `*page_id:` value and every
item of a block-style `page_ids:` list (flow style is not annotated).
