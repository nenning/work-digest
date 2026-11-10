# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Windows Python CLI tool that fetches work activity from Jira, Confluence, Microsoft Teams, and Outlook, summarizes each item using an LLM, and sends an HTML email digest. Runs on a schedule via Windows Task Scheduler.

## Commands

```powershell
pip install -r requirements.txt

# Quick sanity check after a change (no email sent, no state saved)
python digest/main.py --dry-run
python digest/main.py --dry-run --verbose   # debug a slow/stuck run

# Tests (pytest.ini defaults to -q --no-header)
python -m pytest tests/
python -m pytest tests/test_summarizer.py
```

Full CLI reference (all flags, scheduling, troubleshooting) is in README.md. Config: copy `digest/config.yaml.example` → `digest/config.yaml` (validation in `digest/config.py`).

## Architecture

```
digest/main.py          CLI entry point; orchestrates everything
digest/config.py        Loads & validates config.yaml
digest/models.py        SourceItem (raw) → SummarizedItem (after LLM)
digest/state.py         Per-source last-run timestamps in ~/.digest/state.json
digest/problems.py      Non-fatal problem collection (see "No silent failures")
digest/summarizer.py    LLM abstraction (OpenAI / Azure OpenAI / Anthropic)
digest/email_sender.py  Jinja2 HTML rendering; Graph API send or COM Outlook draft
digest/auth/            atlassian.py (classic vs. scoped tokens), microsoft.py (MSAL device code)
digest/sources/         jira, confluence, teams, outlook; mgmt_jira, mgmt_confluence
digest/spec_monitor/    `--spec-monitor` mode
digest/templates/       Inline-CSS HTML email templates (digest, mgmt_summary, spec_monitor)
```

Per-area details in `docs/architecture/`: `atlassian-auth.md`, `jira.md`, `confluence.md`, `mgmt-summary.md`, `spec-monitor.md`.

**Personal digest data flow:** `main.py` → parallel fetch (ThreadPoolExecutor, 4 workers) → merge all `SourceItem` lists → `summarizer.summarize_items()` → `email_sender.send()` or local draft → update `state.json`.

**Modes:** `--mgmt-summary` (one email, one section per `atlassian.projects` entry with a `mgmt_summary` block) and `--spec-monitor` (changed sources of Confluence detail specs) are separate flows; see their docs. State is written only on a successful personal digest send — never on `--dry-run`, `--mgmt-summary` or `--spec-monitor`.

## Key design decisions

- **M365 optional:** `m365.enabled: false` skips Teams/Outlook fetching and opens a local Outlook draft via `win32com` COM instead of sending via Graph API.
- **No silent failures:** never swallow an error with only `log.warning` or a bare `return None`. Non-fatal problems inside fetch/summarize code go through `digest/problems.py` (`report(scope, msg, lossy=)`); `main.py` drains them into the email notices. A `lossy` problem (data dropped) keeps that source's last-run timestamp so the next run retries the window. A failed send or "nothing fetched because everything errored" raises (non-zero exit → `run_digest.py` popup) instead of exiting 0.
- **No unbounded external calls:** every `requests.get/post` passes `timeout=`. `smtplib.SMTP(...)` and `msal.PublicClientApplication(...)` also get an explicit `timeout` (default is none; the M365 silent refresh runs on every scheduled run). OpenAI/Anthropic clients in `summarizer.py` get `timeout=` and `max_retries=0` (callers fail over to the next configured model instead).
- **Spec monitor HTTP:** every request in `digest/spec_monitor/` goes through `spec_monitor/http.request_with_retry`.
