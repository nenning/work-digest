# CLAUDE.md

Windows Python CLI: fetches Jira/Confluence/Teams/Outlook activity, summarizes it with an LLM, sends an HTML email digest. Runs via Windows Task Scheduler.

## Commands

```powershell
pip install -r requirements.txt
python digest/main.py --dry-run   # sanity check after a change: no email sent, no state saved (add --verbose for a slow/stuck run)
python -m pytest tests/
```

CLI reference (all flags, scheduling, troubleshooting): README.md. Config: copy `digest/config.yaml.example` → `digest/config.yaml`.

Per-area details: `docs/architecture/` (`atlassian-auth.md`, `jira.md`, `confluence.md`, `mgmt-summary.md`, `spec-monitor.md`).

## Rules

- **State:** `~/.digest/state.json` is written only on a successful personal digest send — never on `--dry-run`, `--mgmt-summary` or `--spec-monitor`.
- **No silent failures:** never swallow an error with only `log.warning` or a bare `return None`. Non-fatal problems in fetch/summarize code go through `digest/problems.py` (`report(scope, msg, lossy=)`); `main.py` drains them into the email notices. A `lossy` problem (data dropped) keeps that source's last-run timestamp so the next run retries the window. A failed send or "nothing fetched because everything errored" raises (non-zero exit → `run_digest.py` popup) instead of exiting 0.
- **No unbounded external calls:** every `requests.get/post` passes `timeout=`. `smtplib.SMTP(...)` and `msal.PublicClientApplication(...)` also get an explicit `timeout` (default is none; the M365 silent refresh runs on every scheduled run). OpenAI/Anthropic clients in `summarizer.py` get `timeout=` and `max_retries=0` (callers fail over to the next configured model instead).
- **Spec monitor HTTP:** every request in `digest/spec_monitor/` goes through `spec_monitor/http.request_with_retry`.
