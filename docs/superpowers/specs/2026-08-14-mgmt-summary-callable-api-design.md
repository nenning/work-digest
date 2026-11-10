# Idea: callable API for management summary generation

Status: idea, not approved for implementation.

## Problem

`--mgmt-summary` currently only runs as a full CLI flow that ends in sending
an email (or an SMTP/COM dry-run preview). There's no way for another tool
running in this Python environment to get the generated summary as text
without going through email delivery.

## Proposed design

**1. New module `digest/timeutil.py`**

Move `parse_since()` out of `main.py` verbatim, so both `main.py` and the new
`mgmt_summary` module can use it without a circular import. `main.py` keeps
`from digest.timeutil import parse_since` so `digest.main.parse_since` still
works for existing callers/tests.

**2. New module `digest/mgmt_summary.py`** — the reusable core

- `MgmtSummaryResult` dataclass: `label`, `time_range`, `since`, `until`,
  `narrative: Optional[str]`, `jira_items: List[SourceItem]`,
  `confluence_items: List[SummarizedItem]`.
- `generate_mgmt_summary(*, sprint=None, since=None, from_date=None,
  to_date=None, assume_done=False, short=False, config: Optional[Config]=None,
  config_path: Optional[Path]=None, quiet=False) -> MgmtSummaryResult`
  - Loads `digest/config.yaml` itself if `config` isn't passed, so a caller
    can invoke it with zero config plumbing.
  - Same validation as today: missing `mgmt_summary` block, mutually
    exclusive/missing time-range args.
  - To override specific settings, a caller loads the config itself and uses
    `dataclasses.replace()` to patch just the fields it needs (e.g.
    `replace(config, mgmt_summary=replace(config.mgmt_summary, jira_jql=...))`)
    before passing it in as `config=`.
  - Does auth + sprint lookup + Jira/Confluence fetch + per-page
    summarization + narrative synthesis — the body currently inlined in
    `main._run_mgmt_summary`, minus email delivery. No M365 token needed
    here at all (that's only used for OAuth SMTP/email sending).
  - `quiet=False` keeps today's `print()` progress lines so the CLI looks
    unchanged; `quiet=True` suppresses them for programmatic callers.
  - If nothing is found for the time range, returns a result with
    `narrative=None` and empty item lists instead of printing and returning,
    so the caller decides what to do with that case.
- `format_mgmt_summary_text(result: MgmtSummaryResult) -> str` — plain-text
  rendering mirroring `mgmt_summary.html.j2`'s structure: header/time range,
  narrative (paragraphs or bullets), then Done / In Progress / To Do ticket
  lists, then a Documentation section with page title/author/summary.
  Returns `"Nothing found for the given time range."` when `narrative is None`.

**3. Refactor `main._run_mgmt_summary`**

Becomes thin: build kwargs from `args`, call `generate_mgmt_summary(config=config,
...)`, handle the "nothing found" case, then do the existing email-delivery
branch (SMTP/COM) using `result.narrative` / `jira_items` / `confluence_items`
/ `label`. CLI behavior and output stay identical.

**4. Tests**

`tests/test_mgmt_summary.py`: `generate_mgmt_summary` happy path (mocked
fetch/summarize/synthesize), time-range validation errors, config override via
`dataclasses.replace` works, "nothing found" case, and `format_mgmt_summary_text`
output for both narrative and bullet styles plus the empty case.

## Why Python import, not CLI subprocess

Chosen call style: **Python import** — `from digest.mgmt_summary import
generate_mgmt_summary`.

This is a personal/local tool that already runs in one Python environment on
one Windows machine, so there's no cross-language or cross-process boundary
to justify subprocess overhead (serializing to text, reparsing stdout, extra
startup cost for imports/auth per call). If a future caller isn't a Python
process in this environment, the CLI route (`python digest/main.py
--mgmt-summary ...` plus a text-output flag) would need to be revisited.
