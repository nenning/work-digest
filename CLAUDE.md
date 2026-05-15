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

# Override time window
python digest/main.py --since 24h

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
  jira.py               Assigned tickets, comments, new tickets (JQL POST)
  confluence.py         Mentions + page updates (CQL); deduplicates per page
  teams.py              Channel messages + DMs via Graph API
  outlook.py            Inbox messages via Graph API
digest/templates/
  digest.html.j2        Inline-CSS responsive HTML email template
```

**Data flow:** `main.py` → parallel fetch (ThreadPoolExecutor, 4 workers) → merge all `SourceItem` lists → `summarizer.summarize_items()` → `email_sender.send()` or local draft → update `state.json`.

State is only written on a successful send, never on `--dry-run`.

## Key design decisions

- **M365 optional:** `m365.enabled: false` skips Teams/Outlook fetching and opens a local Outlook draft via `win32com` COM instead of sending via Graph API.
- **LLM prompts:** Content < 100 chars is quoted verbatim; longer content gets a 2–4 sentence summary. Confluence cosmetic diffs return `{"summary": null}` (skipped). Jira new tickets are formatted directly without an LLM call.
- **Fallback model:** If the primary LLM call fails, `summarizer.py` retries with `fallback_model` if configured.
- **Outlook priority:** Outlook items are classified as `action_needed / meeting_invite / fyi / info` and color-coded in the HTML template.
- **URL safety:** `email_sender.py` allows only `http`/`https` URLs to prevent `javascript:` injection.

## Configuration

Copy `digest/config.yaml.example` → `digest/config.yaml`. Required fields: Atlassian URL/email/token, LLM provider/key/model, `schedule.times`. Optional: `m365` block (tenant_id, client_id), `llm.endpoint` for Azure, `llm.fallback_model`.

## Testing

Tests use `pytest` + `pytest-mock`. `tests/conftest.py` sets `sys.path`. All external HTTP calls and LLM clients are mocked; no real credentials needed to run the suite.
