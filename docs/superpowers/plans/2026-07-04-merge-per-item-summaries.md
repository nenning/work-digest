# Merge Per-Item Summaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When multiple comments/mentions land on the same Jira ticket, or multiple mentions land on the same Confluence page, within one digest run, produce a single combined digest item instead of one per comment/mention.

**Architecture:** Extend the existing per-ticket/per-page dedup-and-merge logic that already exists for Jira field-changes (`_merge_field_changes`) and Confluence mention+page-update merging (`_merge_by_page`). Add analogous merge helpers for Jira's comment/description-change tier and mention tier, and fix Confluence's page-grouping key so mention-comments on the same page group together. Then adapt the Jira mention LLM prompt to handle a merged, multi-author mention item.

**Tech Stack:** Python, pytest, pytest-mock (existing test stack — no new dependencies).

## Global Constraints

- Single-item tiers/groups must produce byte-for-byte the same `SourceItem` as before this change (no regression for the common case) — spec section "Changes".
- Field-change merging (`_merge_field_changes`) and Confluence page-update diffing are out of scope — spec section "Out of scope".
- No changes to `email_sender.py` grouping logic — it already buckets by kind-set, not exact kind — spec section "Out of scope".

---

### Task 1: Merge multiple Jira comments/description-changes/mentions per ticket

**Files:**
- Modify: `digest/sources/jira.py` (`_deduplicate`, around line 291-299)
- Test: `tests/test_sources_jira.py`

**Interfaces:**
- Consumes: `SourceItem` (from `digest/models.py`) — existing dataclass, unchanged.
- Produces: `_merge_comment_tier(items: list[SourceItem]) -> SourceItem` and `_merge_mention_tier(items: list[SourceItem]) -> SourceItem`, both module-private functions in `digest/sources/jira.py`. `_deduplicate` continues to return `list[SourceItem]`, now containing at most one item per tier.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_sources_jira.py` (after `test_deduplication_comment_suppresses_field_change`, around line 263):

```python
def test_multiple_comments_on_same_ticket_merge_into_one_item():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [
        {"id": "101", "body": "First question", "author": {"displayName": "Marco"}, "updated": "2026-04-09T08:10:00Z"},
        {"id": "102", "body": "Second question", "author": {"displayName": "Anna"}, "updated": "2026-04-09T08:20:00Z"},
    ]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    comments = [i for i in items if i.kind == "comment"]
    assert len(comments) == 1
    assert "First question" in comments[0].content
    assert "Second question" in comments[0].content
    assert comments[0].author == "Anna"  # latest comment's author


def test_comment_and_description_change_merge_into_one_item():
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["description"] = "Updated description text"
    issue["fields"]["comment"]["comments"] = [
        {"id": "101", "body": "A plain comment", "author": {"displayName": "Marco"}, "updated": "2026-04-09T08:10:00Z"},
    ]
    issue["changelog"]["histories"] = [{
        "created": "2026-04-09T08:20:00Z",
        "author": {"displayName": "Anna"},
        "items": [{"field": "description", "fromString": "old", "toString": "new"}],
    }]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    comments = [i for i in items if i.kind == "comment"]
    assert len(comments) == 1
    assert "A plain comment" in comments[0].content
    assert "Updated description text" in comments[0].content


def test_multiple_mentions_on_same_ticket_merge_with_mention_authors():
    def _mention_body():
        return {
            "type": "doc", "content": [{"type": "paragraph", "content": [
                {"type": "mention", "attrs": {"id": "user-123", "text": "@Chris"}},
                {"type": "text", "text": " please check"},
            ]}],
        }
    issue = copy.deepcopy(ISSUE_BASE)
    issue["fields"]["comment"]["comments"] = [
        {"id": "101", "body": _mention_body(), "author": {"displayName": "Marco"}, "updated": "2026-04-09T08:10:00Z"},
        {"id": "102", "body": _mention_body(), "author": {"displayName": "Anna"}, "updated": "2026-04-09T08:20:00Z"},
    ]
    mock_get, mock_post = _mock_responses([issue])
    with patch("digest.sources.jira.requests.get", mock_get), \
         patch("digest.sources.jira.requests.post", mock_post):
        items = fetch(make_config(), "Basic xxx", SINCE)

    mentions = [i for i in items if i.kind == "mention"]
    assert len(mentions) == 1
    assert mentions[0].metadata["mention_authors"] == ["Marco", "Anna"]
    assert mentions[0].author == "Anna"  # latest mention's author
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sources_jira.py -k "merge_into_one_item or merge_with_mention_authors" -v`
Expected: 3 FAILED — each currently returns 2 items instead of 1 (no merging happens yet).

- [ ] **Step 3: Implement the merge helpers and wire them into `_deduplicate`**

In `digest/sources/jira.py`, replace the existing `_deduplicate` function (lines 291-299) with:

```python
def _merge_comment_tier(items: List[SourceItem]) -> SourceItem:
    """Merge multiple comment/description_change items for one ticket into a single item."""
    labels = {"comment": "Kommentar", "description_change": "Beschreibung geändert"}
    ordered = sorted(items, key=lambda i: i.timestamp)
    parts = [f"[{labels.get(i.kind, i.kind)} von {i.author}] {i.content}" for i in ordered]
    latest = ordered[-1]
    return SourceItem(
        source="jira", kind="comment",
        title=latest.title, url=latest.url,
        content="\n\n".join(parts),
        author=latest.author,
        timestamp=latest.timestamp,
    )


def _merge_mention_tier(items: List[SourceItem]) -> SourceItem:
    """Merge multiple mention items for one ticket into a single item."""
    ordered = sorted(items, key=lambda i: i.timestamp)
    parts = [f"[Erwähnt von {i.author}] {i.content}" for i in ordered]
    latest = ordered[-1]
    authors = list(dict.fromkeys(i.author for i in ordered))
    return SourceItem(
        source="jira", kind="mention",
        title=latest.title, url=latest.url,
        content="\n\n".join(parts),
        author=latest.author,
        timestamp=latest.timestamp,
        metadata={"mention_authors": authors},
    )


def _deduplicate(candidates: List[SourceItem]) -> List[SourceItem]:
    """Keep only the highest-priority tier: mentions > comments/descriptions > field changes.
    If the winning tier has more than one item, merge them into a single combined item.
    """
    mentions = [i for i in candidates if i.kind == "mention"]
    if mentions:
        return [_merge_mention_tier(mentions)] if len(mentions) > 1 else mentions
    comments = [i for i in candidates if i.kind in ("comment", "description_change")]
    if comments:
        return [_merge_comment_tier(comments)] if len(comments) > 1 else comments
    return [i for i in candidates if i.kind == "field_change"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sources_jira.py -v`
Expected: all PASS, including the 3 new tests and all pre-existing tests (single-comment, single-mention, and dedup-suppression tests are unaffected since their tiers have exactly one item).

- [ ] **Step 5: Commit**

```bash
git add digest/sources/jira.py tests/test_sources_jira.py
git commit -m "Merge multiple Jira comments/description-changes/mentions per ticket into one item"
```

---

### Task 2: Merge multiple Confluence mentions on the same page

**Files:**
- Modify: `digest/sources/confluence.py` (`_merge_by_page`, lines 263-295; add `_page_key` helper; add `urlparse`/`urlunparse` import)
- Test: `tests/test_sources_confluence.py`

**Interfaces:**
- Consumes: `SourceItem` (unchanged).
- Produces: `_page_key(url: str) -> str`, a new module-private helper in `digest/sources/confluence.py`. `_merge_by_page` keeps its existing signature `(items: List[SourceItem]) -> List[SourceItem]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_sources_confluence.py` (after `test_fetch_mentions`, around line 104):

```python
def test_multiple_mentions_on_same_page_merge():
    mention1 = {
        "title": "API Guidelines",
        "type": "comment",
        "id": "c1",
        "_links": {"webui": "/spaces/ENG/pages/456?focusedCommentId=1#comment-1"},
        "history": {"createdBy": {"displayName": "Bob"}, "createdDate": "2026-04-09T08:00:00Z"},
    }
    mention2 = {
        "title": "API Guidelines",
        "type": "comment",
        "id": "c2",
        "_links": {"webui": "/spaces/ENG/pages/456?focusedCommentId=2#comment-2"},
        "history": {"createdBy": {"displayName": "Anna"}, "createdDate": "2026-04-09T09:00:00Z"},
    }
    responses = [
        make_mock({"accountId": "user-abc"}),                                          # user id
        make_mock({"results": [mention1, mention2]}),                                  # mentions CQL
        make_mock({"body": {"storage": {"value": "<p>Please fix ASAP</p>"}}}),          # mention1 comment body
        make_mock({"body": {"storage": {"value": "<p>Also check this</p>"}}}),          # mention2 comment body
        make_mock({"results": []}),                                                    # page updates CQL
    ]
    with patch("digest.sources.confluence.requests.get", side_effect=responses):
        items = fetch(make_config(), "Basic xxx", SINCE)

    merged = [i for i in items if i.kind == "page"]
    assert len(merged) == 1
    assert merged[0].url == "https://example.atlassian.net/wiki/spaces/ENG/pages/456"
    assert "Bob" in merged[0].content
    assert "Anna" in merged[0].content
    assert "Please fix ASAP" in merged[0].content
    assert "Also check this" in merged[0].content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sources_confluence.py -k test_multiple_mentions_on_same_page_merge -v`
Expected: FAIL — currently `merged` (kind == "page") is empty because the two mentions have different URLs (different comment anchors) and never group together; both remain `kind == "mention"`.

- [ ] **Step 3: Implement the normalized page-key grouping**

In `digest/sources/confluence.py`, add `urlparse, urlunparse` to the existing `from urllib.parse import ...` — there is no such import yet, so add a new import line near the top (after the `import requests` line, around line 7):

```python
from urllib.parse import urlparse, urlunparse
```

Replace `_merge_by_page` (lines 263-295) with:

```python
def _page_key(url: str) -> str:
    """Normalize a page/comment URL to its page-level form (no query string or
    fragment), so mentions/comments on the same page group together regardless
    of which comment anchor their individual URL points at.
    """
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


def _merge_by_page(items: List[SourceItem]) -> List[SourceItem]:
    """Combine mention + page_update items for the same page into one item.

    When you were both mentioned in a page and the page was updated, the LLM
    gets a single item with both contexts rather than two separate items.
    Grouping uses a normalized page key so multiple mention-comments on the
    same page (each with a distinct comment-anchor URL) also merge.
    """
    by_key: dict = {}
    for item in items:
        by_key.setdefault(_page_key(item.url), []).append(item)

    merged: List[SourceItem] = []
    for key, page_items in by_key.items():
        if len(page_items) == 1:
            merged.append(page_items[0])
            continue

        # Build combined content so the LLM sees all activity on this page together.
        parts: List[str] = []
        for it in sorted(page_items, key=lambda x: x.timestamp):
            label = {"mention": "Mention", "page_update": "Page update"}.get(it.kind, it.kind.title())
            parts.append(f"[{label} by {it.author}] {it.content}")

        merged.append(SourceItem(
            source="confluence",
            kind="page",
            title=page_items[0].title,
            url=key,
            content="\n\n".join(parts),
            author=max(page_items, key=lambda x: x.timestamp).author,
            timestamp=max(it.timestamp for it in page_items),
        ))

    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sources_confluence.py -v`
Expected: all PASS, including the new test and all pre-existing ones (`test_fetch_mentions` has exactly one mention so its group size is 1 and its original URL/kind is preserved unchanged).

- [ ] **Step 5: Commit**

```bash
git add digest/sources/confluence.py tests/test_sources_confluence.py
git commit -m "Merge multiple Confluence mentions on the same page into one item"
```

---

### Task 3: Adapt the Jira mention LLM prompt for merged, multi-author mentions

**Files:**
- Modify: `digest/summarizer.py` (`_build_prompt`, lines 79-89)
- Test: `tests/test_summarizer.py`

**Interfaces:**
- Consumes: `SourceItem.metadata["mention_authors"]` (list[str]), produced by Task 1's `_merge_mention_tier`. `SourceItem.metadata["mention_author"]` (str), the existing single-mention key, unchanged.
- Produces: `_build_prompt(item: SourceItem, language: str = "de") -> str` — same signature, now with an additional branch.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_summarizer.py` (after `test_long_content_prompt_says_summarize`, around line 121). `SourceItem`, `datetime`, and `timezone` are already imported at the top of the file, so no new imports are needed:

```python
def test_jira_single_mention_prompt_unchanged():
    item = SourceItem(
        source="jira", kind="mention",
        title="PROJ-1: Fix bug", url="https://example.com/1",
        content="please check this",
        author="Anna",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        metadata={"mention_author": "Anna"},
    )
    prompt = _build_prompt(item)
    assert "Erwähnt von: Anna" in prompt


def test_jira_merged_mention_prompt_lists_all_authors():
    item = SourceItem(
        source="jira", kind="mention",
        title="PROJ-1: Fix bug", url="https://example.com/1",
        content="[Erwähnt von Marco] first\n\n[Erwähnt von Anna] second",
        author="Anna",
        timestamp=datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc),
        metadata={"mention_authors": ["Marco", "Anna"]},
    )
    prompt = _build_prompt(item)
    assert "Marco" in prompt
    assert "Anna" in prompt
    assert "insgesamt" in prompt
```

- [ ] **Step 2: Run tests to verify results**

Run: `python -m pytest tests/test_summarizer.py -k "mention_prompt" -v`
Expected: `test_jira_single_mention_prompt_unchanged` PASSES already (no code change needed for it — it's a regression guard). `test_jira_merged_mention_prompt_lists_all_authors` FAILS because `_build_prompt` has no branch for `mention_authors` and falls through to `mention_author = item.metadata.get("mention_author", item.author)` which resolves to `item.author` ("Anna") only — "Marco" and "insgesamt" are absent from the prompt.

- [ ] **Step 3: Implement the adapted prompt branch**

In `digest/summarizer.py`, replace the Jira mention branch (lines 79-89):

```python
    if item.source == "jira" and item.kind == "mention":
        mention_author = item.metadata.get("mention_author", item.author)
        return (
            f"Jira-Erwähnung in Ticket '{item.title}'.\n"
            f"Erwähnt von: {mention_author}\n"
            f"Kommentar:\n{content}\n\n"
            f"Fasse zusammen: wer hat dich erwähnt, was wurde gefragt/gesagt, welche Aktion wird erwartet.\n"
            f"Sei konkret — der Leser soll ohne Öffnen des Tickets handeln können.\n"
            f"Antworte auf {lang}. Wiederhole den Ticket-Key nicht.\n"
            'Antworte nur mit JSON: {"summary": "..."}'
        )
```

with:

```python
    if item.source == "jira" and item.kind == "mention":
        mention_authors = item.metadata.get("mention_authors")
        if mention_authors:
            authors_str = ", ".join(mention_authors)
            return (
                f"Mehrere Jira-Erwähnungen in Ticket '{item.title}' von: {authors_str}.\n"
                f"Erwähnungen (chronologisch):\n{content}\n\n"
                f"Fasse zusammen: wer hat dich jeweils erwähnt, was wurde gefragt/gesagt, "
                f"welche Aktionen werden insgesamt erwartet.\n"
                f"Sei konkret — der Leser soll ohne Öffnen des Tickets handeln können.\n"
                f"Antworte auf {lang}. Wiederhole den Ticket-Key nicht.\n"
                'Antworte nur mit JSON: {"summary": "..."}'
            )
        mention_author = item.metadata.get("mention_author", item.author)
        return (
            f"Jira-Erwähnung in Ticket '{item.title}'.\n"
            f"Erwähnt von: {mention_author}\n"
            f"Kommentar:\n{content}\n\n"
            f"Fasse zusammen: wer hat dich erwähnt, was wurde gefragt/gesagt, welche Aktion wird erwartet.\n"
            f"Sei konkret — der Leser soll ohne Öffnen des Tickets handeln können.\n"
            f"Antworte auf {lang}. Wiederhole den Ticket-Key nicht.\n"
            'Antworte nur mit JSON: {"summary": "..."}'
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_summarizer.py -v`
Expected: all PASS, including both new tests and all pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add digest/summarizer.py tests/test_summarizer.py
git commit -m "Adapt Jira mention prompt to summarize merged multi-author mentions"
```

---

### Task 4: Full regression pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: all tests PASS (no regressions across `test_sources_jira.py`, `test_sources_confluence.py`, `test_summarizer.py`, `test_email_sender.py`, `test_main.py`, and the rest of the suite).

- [ ] **Step 2: Manual dry-run smoke check (optional, requires real/mock credentials per `digest/config.yaml`)**

Run: `python digest/main.py --dry-run --source jira`
Expected: no crash; if any watched ticket has multiple comments/mentions in the window, the printed output shows one Jira comment/mention entry for that ticket instead of several.
