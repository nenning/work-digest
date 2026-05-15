# Work Digest — Design Spec
**Date:** 2026-04-09  
**Status:** Approved

---

## Context

A personal productivity tool that periodically fetches activity from Jira, Confluence, Microsoft Teams, and Outlook, summarizes it using an LLM, and delivers an HTML email digest to the user's own Outlook inbox. The goal is a single email that gives a complete picture of what happened since the last run — no need to manually check four different tools.

---

## Summary of Decisions

| Question | Decision |
|---|---|
| Delivery | HTML email → own Outlook inbox |
| Sources | Jira Cloud, Confluence Cloud, Teams (channels + chats), Outlook |
| Deployment | Local Windows machine |
| Scheduling | Windows Task Scheduler (configurable times) |
| LLM | Configurable: provider, model, endpoint, API key |
| M365 auth | MSAL device code flow using Azure CLI well-known client ID |
| Atlassian auth | API token in config.yaml |
| Email send | Graph API (same M365 account) |
| State tracking | `state.json` — last-seen timestamp per source |

---

## Architecture

```
Windows Task Scheduler
        │
        ▼
    main.py
        │
        ├── config.py         load + validate config.yaml
        ├── state.py          read state.json (last-seen timestamps)
        │
        ├── [parallel fetch]
        │     ├── sources/jira.py
        │     ├── sources/confluence.py
        │     ├── sources/teams.py      (channels + DMs/group chats)
        │     └── sources/outlook.py
        │
        ├── summarizer.py     LLM abstraction — formats per-item summaries
        ├── email_sender.py   build HTML email + send via Graph API
        │
        └── state.py          write updated timestamps to state.json
```

All four sources are fetched in parallel (asyncio or ThreadPoolExecutor). Each source returns a list of structured items. The summarizer processes each item independently, adapting length to content volume (short content → quote verbatim; long threads → concise paragraph).

---

## M365 Authentication

**Approach:** MSAL device code flow using the Azure CLI well-known public client ID (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`). This is a Microsoft-registered app that supports delegated Graph API permissions without requiring a custom Azure App Registration. Works in most corporate tenants unless the admin has explicitly blocked all external app consent.

**First-run flow:**
1. User runs `python main.py --setup-auth`
2. MSAL prints a device code URL + code to the terminal
3. User opens the URL in a browser, signs in with their M365 account, enters the code
4. Token is cached to `data_dir/token_cache.bin`
5. All subsequent runs refresh silently from cache

**Fallback:** If the tenant blocks this client ID, the error message instructs the user to ask IT to register a custom app and provides the exact scopes and redirect URI to request.

**Required Graph API scopes (delegated):**
- `Mail.Read` — read Outlook mail
- `Mail.Send` — send the digest email
- `Chat.Read` — read Teams DMs and group chats
- `ChannelMessage.Read.All` — read Teams channel messages
- `User.Read` — get the authenticated user's email address

---

## Atlassian Authentication

API token stored in `config.yaml`. Used as HTTP Basic Auth (`email:api_token` base64-encoded). Create at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens).

---

## Config File (`config.yaml`)

```yaml
atlassian:
  url: https://yourcompany.atlassian.net
  email: you@company.com
  api_token: "your-api-token"
  jira_projects: [PROJ, OTHER]       # watch only these Jira projects
  confluence_spaces: [ENG, DOC]      # watch only these Confluence spaces

m365:
  # tenant_id: optional — omit to use "organizations" authority (works for most corp accounts)
  # tenant_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

llm:
  provider: openai                    # openai | anthropic | azure_openai
  api_key: "sk-..."
  model: gpt-4o
  endpoint: "https://..."            # optional — overrides provider default

schedule:
  times: ["08:00", "13:00", "17:00"] # local time

email:
  subject_prefix: "[Digest]"

data_dir: ~/.digest                  # state.json + token_cache.bin stored here
```

---

## What Each Source Fetches

All sources only fetch items **newer than the last successful run** (tracked in `state.json`).

### Jira
- Tickets assigned to the user since last run
- New comments on tickets the user is watching or assigned to
- Newly created tickets in configured projects

### Confluence
- Pages mentioning `@user` since last run
- Page updates in watched spaces (title, author, summary of change)
- New comments on pages, with mention of whether user was mentioned

### Teams — Channels
- All messages in channels the user is a member of
- Grouped by channel, summarized as a thread narrative

### Teams — Chats (DMs + Group Chats)
- All new messages in direct and group chats
- Each conversation summarized individually (not merged)

### Outlook
- New emails in the inbox
- Classified as: action needed / meeting invite / FYI
- Action-needed items highlighted in orange in the email

---

## Email Format

**Layout:** Compact sections with colored left-border per source (Atlassian = blue `#0ea5e9`, M365 = indigo `#6366f1`). Each item is its own entry with a headline and detail block. Action-needed Outlook items get an orange highlight (`#fff7ed` background).

**Summary length:** Adaptive — the LLM prompt instructs it to quote verbatim when content is short, summarize concisely when content is long. Every item includes a clickable link to the original source.

**Subject line format:** `[Digest] Thu 9 Apr · 08:00 — 12 items across 4 sources`

---

## Project File Structure

```
digest/
  main.py                # entry point — orchestrates fetch, summarize, email, state
  config.py              # loads and validates config.yaml (fails fast on missing fields)
  state.py               # reads/writes state.json
  auth/
    atlassian.py         # returns Authorization header from config API token
    microsoft.py         # MSAL device code flow + silent token refresh
  sources/
    jira.py
    confluence.py
    teams.py             # handles both channels and chats
    outlook.py
  summarizer.py          # LLM abstraction — openai / anthropic / azure_openai
  email_sender.py        # builds HTML email + sends via Graph API
  config.yaml.example    # template — copy to config.yaml
  requirements.txt
  setup.bat              # registers Windows Task Scheduler entries from config
  README.md              # full setup walkthrough incl. Azure CLI auth steps
```

---

## CLI Interface

```
python main.py                  # normal run: fetch → summarize → email → save state
python main.py --setup-auth     # interactive M365 device code login
python main.py --dry-run        # fetch + summarize, print email content, no send
python main.py --source jira    # run only one source (for debugging)
python main.py --since 2h       # override last-run time (e.g. "fetch last 2 hours")
```

---

## State File (`state.json`)

```json
{
  "jira":        { "last_run": "2026-04-09T07:58:00Z" },
  "confluence":  { "last_run": "2026-04-09T07:58:00Z" },
  "teams":       { "last_run": "2026-04-09T07:58:00Z" },
  "outlook":     { "last_run": "2026-04-09T07:58:00Z" }
}
```

Updated atomically after successful send. If the run fails mid-way, state is not updated so the next run retries the same window.

---

## Error Handling

- Any single source failing does not abort the run — the email is sent with a "⚠️ Source unavailable" notice in that section
- M365 auth failure prints clear instructions for the IT fallback
- Config validation errors fail immediately at startup with a clear message pointing to the missing field

---

## Dependencies (`requirements.txt`)

```
msal              # M365 auth
requests          # Atlassian + Graph API HTTP calls
openai            # OpenAI/Azure OpenAI LLM
anthropic         # Anthropic LLM
pyyaml            # config parsing
jinja2            # HTML email templating
```

---

## Verification

1. `python main.py --setup-auth` → completes without error, `token_cache.bin` created
2. `python main.py --dry-run --source jira` → prints Jira items for the last interval
3. `python main.py --dry-run` → prints all sections, including LLM summaries
4. `python main.py` → email arrives in Outlook inbox with correct content and working links
5. Run again immediately → no email sent (skipped silently when nothing is new since last run)
6. `setup.bat` → tasks visible in Windows Task Scheduler, fire at configured times
