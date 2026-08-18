# Atlassian auth: classic vs. scoped tokens (`digest/auth/atlassian.py`)

`atlassian.auth_type` (default `classic`) selects the auth scheme.

- **Classic** API tokens use `Basic base64(email:token)` sent directly to the
  tenant domain (`atlassian.url`) — this covers both personal accounts and
  service accounts using a classic token.
- **Scoped** API tokens (id.atlassian.com's "API tokens with scopes" flow)
  use `Bearer <token>` and must be routed through
  `https://api.atlassian.com/ex/{jira,confluence}/{cloudId}/...` instead —
  Atlassian rejects them under Basic auth against the tenant domain with a
  401 regardless of which email/username is paired with them.

`resolve_atlassian_config()` resolves this once at startup: for `auth_type:
scoped` it resolves `cloud_id` (from config, or via the tenant's public
unauthenticated `{url}/_edge/tenant_info` endpoint) and sets
`AtlassianConfig.jira_api_base`/`confluence_api_base` to the
`api.atlassian.com` routes; for `classic` these default to `url` via
`__post_init__`, so nothing changes.

All Jira/Confluence REST calls in `sources/*.py` use
`jira_api_base`/`confluence_api_base` — human-facing links (`/browse/{key}`,
page `webui` links) always use the real `url` instead, since those must stay
on the tenant domain regardless of auth scheme.

A scoped token needs Jira **and** Confluence scopes granted separately — one
without the other fails only that product's calls with a scope error (`401
Unauthorized; scope does not match` on `/wiki/rest/api/...`), which is a
token-configuration problem to fix at id.atlassian.com, not something the
code works around.
