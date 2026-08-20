# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Windows Python CLI tool that fetches work activity from Jira, Confluence, Microsoft Teams, and Outlook, summarizes each item using an LLM, and sends an HTML email digest. Runs on a schedule via Windows Task Scheduler.

## Commands

```powershell
# Install dependencies
pip install -r requirements.txt

# Quick sanity check after a change (no email sent, no state saved)
python digest/main.py --dry-run
python digest/main.py --dry-run --verbose   # debug a slow/stuck run

# Run all tests / a single test file
python -m pytest tests/ -v
python -m pytest tests/test_summarizer.py -v
```

Full CLI reference (all flags, personal digest + management summary examples,
scheduling, troubleshooting) is in README.md — not duplicated here.

## Architecture

```
digest/main.py          CLI entry point; orchestrates everything
digest/config.py        Loads & validates config.yaml
digest/models.py        SourceItem (raw) → SummarizedItem (after LLM)
digest/state.py         Per-source last-run timestamps in ~/.digest/state.json
digest/summarizer.py    LLM abstraction (OpenAI / Azure OpenAI / Anthropic)
digest/email_sender.py  Jinja2 HTML rendering; Graph API send or COM Outlook draft
digest/auth/
  atlassian.py          Auth header + API base URL resolution for Jira/Confluence — classic
                        vs. scoped API tokens. Details: docs/architecture/atlassian-auth.md
  microsoft.py          MSAL device code flow; token cache at ~/.digest/token_cache.bin
digest/sources/
  jira.py               Watched tickets; changelog-based mention/comment/field-change
                        detection. Details: docs/architecture/jira.md
  confluence.py         Mentions + page updates (CQL), deduplicated per page.
                        Details: docs/architecture/confluence.md
  teams.py              Channel messages + DMs via Graph API
  outlook.py            Inbox messages via Graph API
  mgmt_jira.py          Management summary: team ticket fetch + sprint lookup.
                        Details: docs/architecture/mgmt-summary.md
  mgmt_confluence.py    Management summary: team-authored Confluence page diffs.
                        Details: docs/architecture/mgmt-summary.md
digest/templates/
  digest.html.j2        Inline-CSS responsive HTML email template; shows `item.author`
                        next to the title for every item (all sources) when present
  mgmt_summary.html.j2  Management summary template: narrative block + supporting ticket table
```

**Personal digest data flow:** `main.py` → parallel fetch (ThreadPoolExecutor, 4 workers) → merge all `SourceItem` lists → `summarizer.summarize_items()` → `email_sender.send()` or local draft → update `state.json`.

**Management summary data flow:** `main.py --mgmt-summary` loops every `atlassian.projects` entry with a `mgmt_summary` block: resolve that project's own time range (sprint/since/from-to; `--sprint` looks up the sprint on the project's own `jira_board_id`) → `mgmt_jira.fetch_team_tickets()` → derive team_account_ids → `mgmt_confluence.fetch_team_pages()` (diffed page content, incl. brand-new pages) → `summarizer.summarize_items()` (same per-page 1-2 sentence LLM summary as the personal digest) → `summarizer.synthesize_mgmt_summary()` (single LLM call, free-text narrative) → one section per project. All sections are sent as one email via `email_sender.send_mgmt_summary()`. State is never updated.

State is only written on a successful personal digest send, never on `--dry-run` and never in `--mgmt-summary` mode.

## Key design decisions

- **M365 optional:** `m365.enabled: false` skips Teams/Outlook fetching and opens a local Outlook draft via `win32com` COM instead of sending via Graph API.
- **LLM prompts:** Content < 100 chars is quoted verbatim; longer content gets a 2–4 sentence summary. Confluence cosmetic diffs return `{"summary": null}` (skipped). Jira `new_ticket` and `field_change` items are formatted directly without an LLM call. Jira `mention` items get an action-focused prompt that names who mentioned you and what is expected.
- **Jira digest sections:** Email groups Jira into four ordered sections: Erwähnungen (mentions) → Kommentare & Beschreibungen → Neue Tickets → Feldänderungen. Confluence appears before all Jira sections.
- **Fallback model:** If the primary LLM call fails, `summarizer.py` retries with `fallback_model` if configured.
- **Outlook priority:** Outlook items are classified as `action_needed / meeting_invite / fyi / info` and color-coded in the HTML template.
- **URL safety:** `email_sender.py` allows only `http`/`https` URLs to prevent `javascript:` injection.
- **Management summary:** `--mgmt-summary` mode synthesizes a narrative per project (each `atlassian.projects` entry with a `mgmt_summary` block) from that project's Jira tickets + team-authored Confluence page diffs, and sends them as one email with one section per project. Details: docs/architecture/mgmt-summary.md
- **No unbounded external calls:** every `requests.get/post` call passes `timeout=`. `smtplib.SMTP(...)` and `msal.PublicClientApplication(...)` also get an explicit `timeout` — both default to none at all otherwise, which blocks forever on a hung connection (the M365 silent-refresh path runs on every scheduled run, so this matters even outside `--setup-auth`). The OpenAI/Anthropic clients in `summarizer.py` get `timeout=` (and `max_retries=0`, since callers already fail over to the next configured model rather than wanting the SDK to retry the same one) — `synthesize_mgmt_summary()`'s single large narrative call gets a bigger timeout (`max(llm_timeout * 3, 90)`) than the per-item path's `config.llm_timeout`, since it previously had no timeout at all and reusing the small per-item value risked spurious failures on a legitimately slower large generation.
- **Atlassian auth (classic vs. scoped tokens):** `atlassian.auth_type` selects Basic-vs-Bearer auth and tenant-domain-vs-api.atlassian.com routing. Details: docs/architecture/atlassian-auth.md

## Configuration

Copy `digest/config.yaml.example` → `digest/config.yaml`. Required fields: Atlassian URL/email/token, `atlassian.projects` (each with `name` + `jira.project`), LLM provider/key/model, `schedule.times`. Optional: `m365` block (tenant_id, client_id), `llm.endpoint` for Azure, `llm.fallback_model`, `atlassian.auth_type`/`atlassian.cloud_id` (see above — only needed for scoped API tokens). For management summary: at least one project needs a `mgmt_summary` block; the top-level `mgmt_summary` block (shared `jira_jql`, `ignore_users`, `ignore_issue_types`, `recipient`) is optional.

## Testing

Tests use `pytest` + `pytest-mock`. `tests/conftest.py` sets `sys.path`. All external HTTP calls and LLM clients are mocked; no real credentials needed to run the suite.
