# Work Digest

A Windows command-line tool that fetches activity from Jira, Confluence, Teams, and Outlook, summarizes it with a configurable LLM (OpenAI, Anthropic, or Azure OpenAI), and delivers an HTML digest to your inbox.

**M365 is optional.** If you don't have Azure/M365 set up, set `m365.enabled: false` and the tool will fetch Jira + Confluence only and open a pre-composed draft in Outlook Classic instead of sending via the Graph API.

## Prerequisites

- **Python 3.11+** — Download from [python.org](https://www.python.org/downloads/)
- **pip** (comes with Python)
- **Windows 10 or 11** (for Task Scheduler integration)
- Internet connection and access to Jira Cloud and Confluence Cloud
- Microsoft 365 access (optional — only needed for Teams/Outlook sources and Graph API email delivery)
- **pywin32** — required when `m365.enabled: false` to open local Outlook drafts (`pip install pywin32`)

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

### Initial Setup

1. Copy the example config:
   ```bash
   copy config.yaml.example config.yaml
   ```

2. Edit `config.yaml` and fill in all required fields (see table below).

### Configuration Fields

| Field | Required | Example | Notes |
|-------|----------|---------|-------|
| `atlassian.url` | Yes | `https://yourcompany.atlassian.net` | Your Jira/Confluence instance URL |
| `atlassian.email` | Yes | `you@company.com` | Your Atlassian account email |
| `atlassian.api_token` | Yes | (see [API Token](#atlassian-api-token)) | API token for authentication |
| `atlassian.jira_projects` | Yes | `[PROJ, OTHER]` | List of Jira project keys to fetch |
| `atlassian.confluence_spaces` | Yes | `[ENG, DOC]` | List of Confluence space keys to fetch |
| `m365.enabled` | No | `true` | Set `false` to skip Teams/Outlook and open a local Outlook draft instead of sending via Graph API |
| `m365.tenant_id` | No | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` | Azure AD tenant ID; omit to use default. Only used when `m365.enabled: true` |
| `m365.client_id` | No | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` | Custom Azure AD app ID; required if your tenant blocks the Azure CLI public client (AADSTS65002) |
| `llm.provider` | Yes | `openai` | LLM provider: `openai`, `anthropic`, or `azure_openai` |
| `llm.api_key` | Yes | `sk-...` (OpenAI) or `sk-ant-...` (Anthropic) | API key for LLM provider |
| `llm.model` | Yes | `gpt-4o` | Comma-separated model list (e.g., `"gpt-4o, gpt-4o-mini"`). Items are distributed round-robin; all models run in parallel. |
| `llm.endpoint` | No | `https://...` | Optional custom endpoint (overrides provider default) |
| `llm.fallback_model` | No | `gpt-4o-mini` | Comma-separated fallback list, tried in order after all primary models fail |
| `llm.llm_workers` | No | `4` | Parallel LLM calls during summarization (default: 4) |
| `schedule.times` | Yes | `["08:00", "13:00", "17:00"]` | List of times (24-hour HH:MM) to run digest |
| `email.subject_prefix` | No | `[Digest]` | Prefix for digest email subject (default: `[Digest]`) |
| `data_dir` | No | `~/.digest` | Where to store state and token cache (default: `~/.digest`) |

## Atlassian API Token

Your Jira and Confluence API token is used to fetch recent activity. To create one:

1. Go to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click **Create API token**
3. Give it a name like `WorkDigest`
4. Copy the token and paste it into `config.yaml` under `atlassian.api_token`
5. Restrict it to your IP address if your organization requires it

The token needs permission to read Jira issues and Confluence pages. This is automatically granted for personal API tokens.

## M365 Authentication

> **No M365 yet?** Set `m365.enabled: false` in `config.yaml` and skip this section. The tool will fetch Jira and Confluence only, then open a local Outlook Classic draft for you to review and send manually.

The tool uses **device code flow** to authenticate with Microsoft 365 without requiring a custom app registration. Your organization's Azure AD tenant must allow the Azure CLI public client.

### First-Time Setup

Run the authentication setup:

```bash
python main.py --setup-auth
```

You will see a prompt like:

```
To sign in, use a web browser to open the page https://microsoft.com/devicelogin
and enter the code XXXXXXXXX to authenticate.
```

1. Open the URL in your browser
2. Enter the code shown
3. Sign in with your Microsoft 365 account
4. Grant the requested permissions (Mail, Teams, User profile)

The tool stores your refresh token locally at `~/.digest/token_cache.bin` (permissions: 0600).

### Token Cache

- **Location**: `~/.digest/token_cache.bin`
- **Content**: Serialized MSAL token cache (includes refresh token)
- **Permissions**: Read/write by user only (0600)

Tokens are refreshed automatically; you only need to run `--setup-auth` again if:
- The refresh token expires (typically 90 days)
- Your password changes
- IT revokes your token

### If Your Tenant Blocks Device Flow

If you see an error like `AADSTS90002: Tenant not found`, your organization may have blocked the Azure CLI public client. Ask your IT department to:

1. Register an Azure AD app with these **delegated scopes**:
   - `Mail.Read`
   - `Mail.Send`
   - `Chat.Read`
   - `ChannelMessage.Read.All`
   - `User.Read`

2. Provide the **Client ID** (application ID)

3. Add the client ID to `config.yaml`:
   ```yaml
   m365:
     client_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
   ```

## First Run

Before scheduling, test the tool manually:

```bash
# Test without sending anything (prints to console)
python main.py --dry-run

# Test a single source
python main.py --dry-run --source jira

# Normal run
python main.py
```

**With `m365.enabled: true` (default):** fetches all four sources, sends an HTML email to your Outlook inbox via Graph API, saves timestamp to `~/.digest/state.json`.

**With `m365.enabled: false`:** fetches Jira + Confluence only, opens a pre-composed draft in Outlook Classic for you to review and send. State is saved after the draft is opened.

## Scheduling with Windows Task Scheduler

Once you've tested and confirmed the tool works, set up automatic daily runs.

### Register Tasks

Run the setup script **as Administrator**:

```bash
setup.bat
```

This reads the times from `config.yaml` (`schedule.times`) and creates a scheduled task for each time.

Example: If `schedule.times: ["08:00", "13:00", "17:00"]`, three tasks are created:
- `WorkDigest-08-00` — runs at 08:00
- `WorkDigest-13-00` — runs at 13:00
- `WorkDigest-17-00` — runs at 17:00

### Verify Tasks

List all work-digest tasks:

```bash
schtasks /query /tn "WorkDigest*"
```

Output should show all registered tasks with status `Ready`.

### View Task Logs

Task Scheduler logs are available in **Event Viewer**:
1. Press `Win + R`, type `eventvwr`, press Enter
2. Navigate to **Windows Logs** → **System**
3. Filter by source `Task Scheduler` to see runs and errors

### Remove Tasks

To remove all work-digest tasks:

```bash
for /f "tokens=2 delims= " %a in ('schtasks /query /tn "WorkDigest*" /fo list ^| find "TaskName"') do schtasks /delete /tn "%a" /f
```

Or remove individual tasks:

```bash
schtasks /delete /tn "WorkDigest-08-00" /f
```

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `FileNotFoundError: config.yaml` | Not running from digest folder | `cd` to the digest folder before running |
| `RuntimeError: non-interactive session` | Token expired; Task Scheduler can't prompt | Run `python main.py --setup-auth` manually to refresh |
| `HTTPError 403 from Jira/Confluence` | Bad API token or insufficient permissions | Check token at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens); verify project/space keys in config |
| `HTTPError 403 from Graph` | M365 permissions not granted or revoked | Run `python main.py --setup-auth` again and re-grant permissions; if still blocked, ask IT to allow the Azure CLI public client or register a custom app |
| `No module named 'yaml'` | PyYAML not installed | Run `pip install -r requirements.txt` |
| `openai.APIError: 401 ...` | Invalid LLM API key | Check your LLM provider API key in `config.yaml` |
| Digest not sent | Check email address resolved from M365 | Ensure your Microsoft 365 account has a valid primary email address |
| `pywin32 is not installed` | Local draft mode requires pywin32 | Run `pip install pywin32` |
| Outlook draft doesn't open | COM automation failed (Outlook not running or not installed) | Open Outlook Classic first, then retry |

### Debug Mode

Enable verbose logging by editing the line in `main.py` that sets up logging:

```python
logging.basicConfig(
    level=logging.DEBUG,  # Change from INFO to DEBUG
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
```

Then run with `--dry-run` to see detailed output without sending email.

## CLI Flags

- `python main.py --setup-auth` — Authenticate with M365 (device code flow)
- `python main.py --dry-run` — Test digest without sending email
- `python main.py --dry-run --source jira` — Test single source (jira, confluence, teams, outlook)
- `python main.py --since 2h` — Fetch activity from last 2 hours instead of last run
- `python main.py` — Normal run: fetch, summarize, send email

## How It Works

### What is fetched and from where

Each run fetches activity since the last successful run (or 24 hours ago on first run). All four sources are fetched in parallel.

| Source | What is fetched | API used |
|--------|----------------|----------|
| **Jira** | Tickets assigned to you (updated since last run) | Jira REST API v3, JQL search |
| **Jira** | Comments on tickets you reported or are assigned to | JQL + per-issue comment list |
| **Jira** | Newly created tickets in your configured projects | JQL `created >= since` |
| **Confluence** | Pages where you are `@mentioned` (created since last run) | Confluence CQL, `/wiki/rest/api/content/search` |
| **Confluence** | Pages modified in your configured spaces | CQL `lastModified >= since` |
| **Teams** | Messages in every channel of every team you belong to | Graph API `/me/joinedTeams` → channels → messages |
| **Teams** | Direct messages and group chats | Graph API `/me/chats` → messages |
| **Outlook** | All emails received in your inbox | Graph API `/me/mailFolders/inbox/messages` |

Pagination is handled automatically for all sources. System messages and bot events (Teams) are filtered out.

### Processing pipeline

```
Fetch (parallel)          Summarize (parallel)         Deliver
─────────────────         ────────────────────         ──────────────────────────────
Jira    ──┐               ThreadPoolExecutor            Render Jinja2 HTML template
Confluence─┤──► merge ──► (llm_workers, default 4) ──► │
Teams   ──┤    list       LLM prompt per item           ├─ m365.enabled: true  ──► Graph API sendMail
Outlook ──┘    (Teams +   verbatim if short,            │                           → your inbox
               Outlook    compact phrases if long       │
               skipped    ↓                             └─ m365.enabled: false ──► win32com
               if m365    JSON: {summary, priority}         Outlook.CreateItem        → local draft
               disabled)  (priority for Outlook only)       .Display()                  window
```

**Adaptive summarization:** Content under 100 characters is quoted verbatim. Longer content gets compact, keyword-focused phrases — title/ticket key is never repeated. Summarization runs in parallel (`llm_workers` threads, default 4). Outlook emails additionally get a priority label (`action_needed`, `meeting_invite`, `fyi`, or `info`) which drives visual highlighting in the email.

### What is stored and where

| File | Content | When updated |
|------|---------|--------------|
| `~/.digest/state.json` | Last successful run timestamp per source, e.g. `{"jira": "2026-04-09T08:00:00+00:00"}` | After each successful email send (not on dry-run) |
| `~/.digest/token_cache.bin` | MSAL token cache — contains your M365 refresh token; file permissions 0600 | After each M365 auth (initial + silent refresh). Not created when `m365.enabled: false` |

No email content, summaries, or fetched items are written to disk. State only tracks *when* each source was last successfully processed so the next run knows where to start.

If a source fails (network error, API down), its timestamp is **not** updated — the next run will re-fetch that source from the previous window, so no activity is missed.

## Architecture

```
digest/
  main.py           # CLI entry point
  config.py         # YAML configuration loader
  models.py         # Data models (SourceItem, SummarizedItem)
  state.py          # Persistent state management
  summarizer.py     # LLM summarization logic
  email_sender.py   # Graph API email sending
  sources/          # Activity fetchers
    jira.py
    confluence.py
    teams.py
    outlook.py
  auth/
    atlassian.py    # Jira/Confluence auth
    microsoft.py    # M365 device code flow auth
setup.bat           # Task Scheduler registration script
config.yaml.example # Configuration template
```

## Notes

- The digest runs in the logged-in user's context; Task Scheduler will not prompt for M365 auth if the token expires
- All times in `schedule.times` are in 24-hour format (HH:MM) and assumed to be local time
- The tool respects the user's local timezone for activity windows
- Email is sent to the authenticated user's primary email address (resolved from Graph /me endpoint)
