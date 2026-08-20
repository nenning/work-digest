# Multi-project Jira/Confluence config

## Context

Today `atlassian.jira_projects` is a flat list of Jira project keys and
`atlassian.jira_jql_extra` is a single string AND'd onto every personal-digest
Jira query across *all* projects combined into one `project in (...)` clause.
`atlassian.confluence_spaces` is likewise a flat list. `mgmt_summary` is a
single independent block (`jira_jql`, `jira_board_id`, `ignore_users`,
`ignore_issue_types`, `recipient`) unrelated to `jira_projects`, producing one
team narrative per `--mgmt-summary` run.

This doesn't support teams that track several Jira projects with different
extra filters (e.g. a different `Team[Team]` value per project), or want a
management summary broken out per project instead of one combined narrative.

## Goals

- Support multiple Jira projects, each with its own extra JQL filter for the
  personal digest.
- Let each project optionally define its own management-summary settings
  (extra JQL, sprint board, recipient override); projects without one are
  excluded from `--mgmt-summary` entirely.
- Produce one narrative section per project in the management summary,
  combined into as few emails as recipients require.
- Allow a project to carry its own display name and (rarely) its own
  Atlassian URL, while credentials stay global.

## Non-goals

- Per-project credentials (email/api_token/auth_type/cloud_id) — deferred;
  classic API tokens are account-level and already work across sites.
- Backward compatibility with the old flat `jira_projects`/`confluence_spaces`/
  `jira_jql_extra` keys — this is a breaking config change with no shim.

## Config schema

```yaml
atlassian:
  url: https://yourcompany.atlassian.net
  email: you@company.com
  api_token: "..."
  auth_type: classic
  cloud_id: ...

  projects:
    - name: EGOV                                   # display label (mgmt-summary headings, logs)
      jira:
        project: EGOV                              # Jira project key
        jql_extra: '"Team[Team]"=981844de-...'     # optional, AND'd onto this project's personal-digest queries
      confluence:
        spaces: [ENG]                              # optional
      mgmt_summary:                                # optional -- omitting excludes this project from --mgmt-summary
        jira_jql_extra: '...'                      # optional, AND'd onto "project = EGOV" + global mgmt_summary.jira_jql
        jira_board_id: 123                         # optional, required only for --sprint on this project
      # url: https://other-tenant.atlassian.net    # optional -- overrides atlassian.url for this project's requests only

    - name: OTHER
      jira:
        project: OTHER
      confluence:
        spaces: [DOC]
      # no mgmt_summary block -> OTHER is excluded from --mgmt-summary

mgmt_summary:                              # shared across every project that opts in
  jira_jql: 'statusCategory != Done'       # optional shared/base clause ("the top jql")
  ignore_users: []                         # global
  ignore_issue_types: []                   # global
  recipient: you@company.com               # default; a project's own recipient overrides this
```

A project's effective management-summary JQL is:
`project = {jira.project} AND ({mgmt_summary.jira_jql}) AND ({project.mgmt_summary.jira_jql_extra})`,
omitting whichever of the two AND clauses is unset.

### Dataclasses (`config.py`)

```python
@dataclass
class ProjectJiraConfig:
    project: str
    jql_extra: Optional[str] = None

@dataclass
class ProjectConfluenceConfig:
    spaces: List[str] = field(default_factory=list)

@dataclass
class ProjectMgmtSummaryConfig:
    jira_jql_extra: Optional[str] = None
    jira_board_id: Optional[int] = None

@dataclass
class ProjectConfig:
    name: str
    jira: ProjectJiraConfig
    confluence: ProjectConfluenceConfig = field(default_factory=ProjectConfluenceConfig)
    mgmt_summary: Optional[ProjectMgmtSummaryConfig] = None
    url: Optional[str] = None   # overrides atlassian.url for this project's requests only
```

`AtlassianConfig` drops `jira_projects`, `confluence_spaces`, `jira_jql_extra`
and gains `projects: List[ProjectConfig]`. It keeps a computed
`confluence_spaces` property (union of every project's spaces) so
`confluence.py`'s space-validation and any other flat-list reads keep working
without change.

`AtlassianConfig.for_project(project: ProjectConfig) -> AtlassianConfig`
returns an effective copy scoped to one project:
- `projects=[project]` (so `confluence_spaces` narrows to just this project)
- if `project.url` is set: `url`, `jira_api_base`, `confluence_api_base` all
  become `project.url` (mirroring how `__post_init__` already derives the
  global api bases from `url` for classic auth) — a project's `url` override
  is not re-resolved through the scoped-auth `cloud_id` flow; that combination
  (per-project URL + `auth_type: scoped`) is a documented limitation, not
  implemented.

`mgmt_summary.jira_jql` becomes `Optional[str]` (previously required)
since a project's own filter now supplies the project scoping.

**Validation:** if `--mgmt-summary` is invoked and no project defines a
`mgmt_summary` block, raise a clear error (mirrors today's "mgmt_summary
section is missing" check, now phrased per-project).

## Personal digest fetch changes

**`jira.py`** (`_fetch_watched`, `_fetch_new_tickets`): loop
`config.projects`; for each, get `pconf = config.for_project(project)` and run
the existing watched/new-ticket JQL scoped to `project = {project.jira.project}`
with that project's own `jql_extra` appended, using `pconf.jira_api_base` /
`pconf.url` for requests and browse links. Merge results across projects
before the existing new-ticket-dedup step.

**`confluence.py`** (`fetch`): mention-search (`_fetch_mentions`) is not
space-scoped — it's one CQL query per site for "was I mentioned." Since
projects can (rarely) point at different URLs, group `config.projects` by
effective URL (`config.for_project(p).url`, falling back to the global
`atlassian.url`). For each URL group: build a group config (`url`/api bases
from the group, `projects` = the group's members so `confluence_spaces`
unions just their spaces), run one `_fetch_mentions` + one
`_fetch_page_updates` against it, and merge across groups. When no project
overrides `url` (the common case), this is exactly one group containing all
projects — identical API-call count and behavior to today.

## Management summary changes

**`mgmt_jira.py`**: add `resolve_project_mgmt_config(project, global_cfg) ->
MgmtSummaryConfig` that builds the effective per-project config:
- `jira_jql` = the AND-combined expression above
- `jira_board_id` = `project.mgmt_summary.jira_board_id`
- `ignore_users` / `ignore_issue_types` = `global_cfg`'s (global, unchanged)

`recipient` stays a single global setting (`mgmt_summary.recipient`, falling
back to `email.recipient`) — no per-project override.

`fetch_team_tickets` and `fetch_sprint` keep their existing signatures —
they already accept a `MgmtSummaryConfig`/`board_id` directly, so the
per-project resolution happens one level up.

**`main.py` (`_run_mgmt_summary`)**: rewritten to loop over
`[p for p in config.atlassian.projects if p.mgmt_summary]`:

1. Validate up front: if `--sprint` is used, every enabled project must have
   `jira_board_id` set — collect and report *all* missing ones in one error
   rather than failing on the first.
2. For each enabled project, resolve its own time range:
   - `--sprint NAME`: call `fetch_sprint` against *that project's own*
     `jira_board_id` — different projects' boards can have different sprint
     date spans even for the same sprint name, so `(since, until, label)` is
     resolved per project, not shared.
   - `--since` / `--from`/`--to`: the same explicit range applies to every
     project (no board lookup involved).
3. For each project: `pconf = config.atlassian.for_project(project)`; fetch
   team tickets (`fetch_team_tickets`, unchanged) and team Confluence pages
   (`fetch_team_pages`, unchanged — `pconf.confluence_spaces` already narrows
   to this project's own spaces); summarize the Confluence items; synthesize
   one narrative (`synthesize_mgmt_summary`, unchanged). Skip a project from
   the output (with a console note) if it produced zero tickets and zero
   pages.
4. If no project produced anything, print "Nothing found" and return (same
   as today).
5. Send **one email** containing all project sections, to
   `mgmt_summary.recipient or email.recipient` (recipient is a single global
   setting, no per-project override).
6. The email subject/header uses a run-level label (the sprint name, or the
   since/until range) rather than a single resolved date span, since
   per-project sprint dates can differ; each section shows its own resolved
   date span underneath its heading.

## Email rendering changes

**`email_sender.py`**: `send_mgmt_summary_via_smtp` / `_via_com` and
`_render_mgmt_html` change from taking a single
`(narrative, jira_items, confluence_items)` to taking a list of per-project
sections (e.g. a small `MgmtSection` dataclass: `name`, `label`, `narrative`,
`jira_items`, `confluence_items`). `_render_mgmt_html` does the existing
paragraph/bullet parsing and ticket-status bucketing per section instead of
once globally.

**`templates/mgmt_summary.html.j2`**: wrap the existing narrative + supporting
detail block in a loop over sections, adding a heading per section
(`{{ section.name }} — {{ section.label }}`) before its narrative and ticket
groups.

## Testing

Existing tests touching the changed surface need updates:
`test_config.py`, `test_sources_jira.py`, `test_sources_confluence.py`,
`test_sources_mgmt_jira.py`, `test_sources_mgmt_confluence.py`,
`test_email_sender.py`, `test_main.py`. No new test files are anticipated;
this is covered in the implementation plan rather than here.

## Documentation

`config.yaml.example` and `README.md` need updating to the new schema
(the config example above is close to final). `docs/architecture/mgmt-summary.md`
and `docs/architecture/jira.md` (referenced from CLAUDE.md) need updating to
describe the per-project loop.
