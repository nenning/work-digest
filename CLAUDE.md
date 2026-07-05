# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Windows Python CLI tool that fetches work activity from Jira, Confluence, Microsoft Teams, and Outlook, summarizes each item using an LLM, and sends an HTML email digest. Runs on a schedule via Windows Task Scheduler.

## Commands

```powershell
# Install dependencies
pip install -r requirements.txt

# First-time M365 auth (device code flow)
python digest/main.py --setup-auth

# Run (sends email)
python digest/main.py

# Dry-run (prints output, no email sent, no state saved)
python digest/main.py --dry-run

# Single source dry-run
python digest/main.py --dry-run --source jira   # jira | confluence | teams | outlook

# Override time window (h/d/w suffixes supported)
python digest/main.py --since 24h

# Management summary mode
python digest/main.py --mgmt-summary --sprint current --dry-run
python digest/main.py --mgmt-summary --sprint "Sprint 42" --dry-run
python digest/main.py --mgmt-summary --sprint "Sprint 42" --assume-done
python digest/main.py --mgmt-summary --sprint current --short --dry-run
python digest/main.py --mgmt-summary --since 7d --dry-run
python digest/main.py --mgmt-summary --from 2026-05-01 --to 2026-05-29

# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_summarizer.py -v

# Register scheduled tasks (run as Administrator)
schedule-digest.bat
```

## Architecture

```
digest/main.py          CLI entry point; orchestrates everything
digest/config.py        Loads & validates config.yaml
digest/models.py        SourceItem (raw) → SummarizedItem (after LLM)
digest/state.py         Per-source last-run timestamps in ~/.digest/state.json
digest/summarizer.py    LLM abstraction (OpenAI / Azure OpenAI / Anthropic)
digest/email_sender.py  Jinja2 HTML rendering; Graph API send or COM Outlook draft
digest/auth/
  atlassian.py          Basic Auth header for Jira/Confluence
  microsoft.py          MSAL device code flow; token cache at ~/.digest/token_cache.bin
digest/sources/
  jira.py               Watched tickets (watcher = currentUser()); per-ticket changelog
                        via GET /issue/{key}/changelog; detects @mentions in ADF;
                        per-ticket priority: mentions > comments/desc changes > field changes;
                        field changes merged to initial→final state (net-zero dropped);
                        new tickets via separate JQL query
  confluence.py         Mentions + page updates (CQL); deduplicates per page
  teams.py              Channel messages + DMs via Graph API
  outlook.py            Inbox messages via Graph API
  mgmt_jira.py          Management summary: paginated team ticket fetch; sprint lookup via
                        GET /rest/agile/1.0/board/{id}/sprint; kinds: ticket_done/wip/todo
  mgmt_confluence.py    Management summary: pages filtered by team accountIds (CQL contributor in,
                        plus a version-history walk to attribute the actual in-window edit —
                        `lastModifier` alone would miss a team edit later overwritten by someone else)
digest/templates/
  digest.html.j2        Inline-CSS responsive HTML email template
  mgmt_summary.html.j2  Management summary template: narrative block + supporting ticket table
```

**Personal digest data flow:** `main.py` → parallel fetch (ThreadPoolExecutor, 4 workers) → merge all `SourceItem` lists → `summarizer.summarize_items()` → `email_sender.send()` or local draft → update `state.json`.

**Management summary data flow:** `main.py --mgmt-summary` → resolve time range (sprint/since/from-to) → `mgmt_jira.fetch_team_tickets()` → derive team_account_ids → `mgmt_confluence.fetch_team_pages()` (diffed page content, incl. brand-new pages) → `summarizer.summarize_items()` (same per-page 1-2 sentence LLM summary as the personal digest) → `summarizer.synthesize_mgmt_summary()` (single LLM call, free-text narrative) → `email_sender.send_mgmt_summary()`. State is never updated.

State is only written on a successful personal digest send, never on `--dry-run` and never in `--mgmt-summary` mode.

## Key design decisions

- **M365 optional:** `m365.enabled: false` skips Teams/Outlook fetching and opens a local Outlook draft via `win32com` COM instead of sending via Graph API.
- **LLM prompts:** Content < 100 chars is quoted verbatim; longer content gets a 2–4 sentence summary. Confluence cosmetic diffs return `{"summary": null}` (skipped). Jira `new_ticket` and `field_change` items are formatted directly without an LLM call. Jira `mention` items get an action-focused prompt that names who mentioned you and what is expected.
- **Jira digest sections:** Email groups Jira into four ordered sections: Erwähnungen (mentions) → Kommentare & Beschreibungen → Neue Tickets → Feldänderungen. Confluence appears before all Jira sections.
- **Fallback model:** If the primary LLM call fails, `summarizer.py` retries with `fallback_model` if configured.
- **Outlook priority:** Outlook items are classified as `action_needed / meeting_invite / fyi / info` and color-coded in the HTML template.
- **URL safety:** `email_sender.py` allows only `http`/`https` URLs to prevent `javascript:` injection.
- **Management summary:** `--mgmt-summary` mode fetches all team tickets via a configurable `mgmt_summary.jira_jql`, derives team members from assignee/reporter accountIds, fetches Confluence pages contributed to by those accountIds (CQL `contributor in (...)`, version-history walk to find the in-window team edit and diff it against the pre-window baseline — or against "" for brand-new pages). Each page's diff is summarized in 1-2 sentences via `summarizer.summarize_items()` (identical to personal digest page-update handling, including the cosmetic-only skip). Jira tickets are NOT individually summarized. A final single free-text LLM call (`synthesize_mgmt_summary()`) produces a 2–3 paragraph narrative from the ticket lists and the per-page summaries. The `--assume-done` flag instructs the LLM to present in-progress and todo tickets as completed.
- **Sprint lookup:** Paginates `GET /rest/agile/1.0/board/{boardId}/sprint` with case-insensitive name match. Requires `mgmt_summary.jira_board_id` in config.

## Configuration

Copy `digest/config.yaml.example` → `digest/config.yaml`. Required fields: Atlassian URL/email/token, LLM provider/key/model, `schedule.times`. Optional: `m365` block (tenant_id, client_id), `llm.endpoint` for Azure, `llm.fallback_model`. For management summary: `mgmt_summary` block with at minimum `jira_jql`.

## Testing

Tests use `pytest` + `pytest-mock`. `tests/conftest.py` sets `sys.path`. All external HTTP calls and LLM clients are mocked; no real credentials needed to run the suite.
