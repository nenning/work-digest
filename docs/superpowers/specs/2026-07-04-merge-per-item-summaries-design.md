# Design: merge multiple comments/mentions per ticket or page into a single summary

## Problem

Today, each Jira comment (or description change, or mention) on the same ticket
produces its own `SourceItem`, and each goes through the LLM separately,
producing one email digest entry per comment. Example: a ticket with two
comments added since the last run currently shows up as two separate
"📋 JIRA EGOV-1201: ..." entries under "Kommentare & Beschreibungen" instead of
one combined summary.

Confluence already merges mention + page-update items that share the same
page URL (`_merge_by_page`), but two mention-*comments* on the same page have
distinct comment-anchor URLs and so slip through unmerged today.

## Goal

Within one digest run, all comment/description-change activity on the same
Jira ticket is summarized as a single item, and all mention activity on the
same Jira ticket is summarized as a single item. Same for Confluence: all
mentions targeting the same page merge together (extending the existing
mention+page-update merge).

Field-change merging and single-item cases are unaffected.

## Changes

### `digest/sources/jira.py`

`_deduplicate()` already narrows candidates to one *tier* per ticket
(mentions > comments/description-changes > field-changes). Extend it so that
if the winning tier has more than one item, they're merged into a single
`SourceItem` before being returned:

- **Comments/description-changes tier**: merge into one item, `kind="comment"`,
  content built from per-item blocks in chronological order, e.g.
  `[Kommentar von {author}] {text}` / `[Beschreibung geändert von {author}] {text}`,
  joined with blank lines. `author`/`timestamp` = latest item's.
- **Mentions tier**: merge into one item, `kind="mention"`, content as
  `[Erwähnt von {author}] {text}` blocks in chronological order.
  `metadata["mention_authors"]` is set to the ordered list of unique authors
  (replacing the singular `mention_author` key used today for the single-item
  case). `author`/`timestamp` = latest item's.
- If a tier has exactly one item, it passes through unchanged (existing
  `mention_author` metadata key, existing content format) — no behavior
  change for the common case.

### `digest/sources/confluence.py`

`_merge_by_page()` groups items by exact `item.url`. Change the grouping key
to a normalized URL with query string and `#fragment` stripped, so that two
mention-comments on the same page (which differ only by comment-anchor
fragment/query) land in the same group as each other and as any page-update
item.

Output URL behavior:
- Group of 1 item: emit the item with its **original** URL unchanged (keeps
  the deep link straight to the relevant comment).
- Group of 2+ items: emit the merged item using the **normalized** (page-level)
  URL, since a single link can no longer point at every source comment.

The existing per-item content labeling (`[Mention by author] ...` /
`[Page update by author] ...`) is unchanged and will now also apply across
multiple mention items.

### `digest/summarizer.py`

The Jira mention prompt branch (`item.source == "jira" and item.kind ==
"mention"`) currently assumes a single mentioner via
`metadata.get("mention_author", item.author)`. Add a check: if
`metadata.get("mention_authors")` (list) is present, use an adapted prompt
that:

- Lists all mentioning authors.
- Presents the chronological mention blocks (already formatted in `content`).
- Asks the LLM to summarize, across all mentions, who asked what and what
  actions are expected in total.

The existing singular-mention prompt is unchanged and still used whenever
`mention_authors` is absent (i.e. exactly one mention).

## Out of scope

- Confluence page-update diffs are already single-item-per-page (multi-edit
  diffs are computed cumulatively against a pre-window baseline); no change
  needed there.
- No change to `email_sender.py` grouping — it already buckets `comment` and
  `description_change` into the same "Kommentare & Beschreibungen" section
  regardless of exact kind.
- No change to field-change merging (`_merge_field_changes`), which already
  produces one item per ticket.

## Testing

- `tests/test_sources_jira.py`: two comments on one ticket → single merged
  `SourceItem`; two mentions on one ticket → single merged item with
  `mention_authors` list; mixed comment+description-change → merged; single
  comment/mention → unchanged (regression).
- `tests/test_sources_confluence.py`: two mention-comments on the same page
  (different URLs, same normalized page) → merged with page-level URL; single
  mention → original URL preserved (regression); existing mention+page-update
  merge case still passes.
- `tests/test_summarizer.py`: merged mention item (with `mention_authors`)
  produces the adapted multi-author prompt; single-mention item (with
  `mention_author`) still produces the original prompt (regression).
