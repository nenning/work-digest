# Confluence source (`digest/sources/confluence.py`)

Mentions + page updates (CQL); deduplicates per page.

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
