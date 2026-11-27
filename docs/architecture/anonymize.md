# Anonymization (`digest/anonymizer.py`)

Replaces names, email addresses and phone numbers with placeholders before text reaches an LLM and restores them in
the reply. Off by default (`anonymize.enabled: false`). Design: `docs/superpowers/specs/2026-11-27-page-exclusion-and-anonymization-design.md`.

```yaml
anonymize:
  enabled: false
  additional_names: []          # people not in the Atlassian user directory (customers, externals); single words allowed
  on_directory_failure: fail    # fail | continue
```

## Gate

One module-level `Anonymizer` per run, installed by `anonymizer.setup()` (called in `main.py` for every mode, inside
the process lock, after the config title annotation). `outgoing(prompt)` / `incoming(reply)` wrap every LLM exit:
`summarizer._call_openai` / `_call_anthropic` (covers summaries, mgmt summary, spec monitor and page watch text calls)
and the text prompt of `spec_monitor/vision._call_openai` / `_call_anthropic`. A new LLM call site must go through them.
Thread-safe; the same person always gets the same placeholder within a run.

Placeholders: `PERSON_001`, `EMAIL_001`, `PHONE_001` (counter per kind, at least 3 digits). `restore()` is
case-insensitive and maps back to the canonical name: the first registered spelling (`additional_names` first, then the
directory, then item names). Only a whole token is a placeholder: `email_1`, `HOME_PHONE_001`, `person_1_id` or
`XPERSON_001` are left alone. A genitive `s` stays outside the placeholder (`PERSON_001s`). A placeholder-shaped token
already in the source text is never issued as a placeholder, so restore leaves it as is. A reply that contains a
placeholder this run never issued is reported as a non-lossy problem (scope `anonymize`, once per placeholder) and left
as is.

## What is matched

- **Names:** full names in either order and rotated forms (`First Last`, `Last First`, `Last, First`; for 3+ tokens
  the last token rotated to the front), case-insensitive, word-bounded, optional trailing `s` (genitive).
- **Emails:** ASCII `local@domain.tld`, one placeholder per lower-cased address.
- **Phones:** 9-15 digits with space, non-breaking space (U+00A0, U+202F, e.g. Confluence `&nbsp;`), en dash or
  `()./-` separators, optional `+` / `00`; dates (`2026-11-27`, `27.11.2026`)
  are not phones. A longer digit run is split into 9-15 digit chunks, or masked whole when it cannot be split.

## Sources of names

1. Atlassian user directory: `GET /rest/api/3/users/search` (paged, 1000 per page, max 50 pages) on the global site and
   every distinct per-project `url` override; every account except `accountType: app` (bots), so Jira Service
   Management `customer` accounts are included. Scoped tokens need the
   `read:jira-user` scope. Loaded once per run.
2. Names seen in fetched items (`note_items`: item author, `mention_author(s)`, `assignee`) - personal digest (all
   items) and `--mgmt-summary` (Jira tickets and Confluence pages, before summarization); spec monitor and page watch
   rely on 1 and 3.
3. `anonymize.additional_names`: the only way to mask a single-token name (a bare first name or surname).

## Guarantee and limits

Guarantee: a full name from the sources above, an email or a phone number in text sent through the gate does not
reach the LLM. Not covered:

- A bare first name or surname alone is not matched (too many false hits), unless listed in `additional_names`.
- Names that are in none of the three sources (typo, nickname, customer contact outside Atlassian).
- Phones glued to letters (`Tel0791234567`), emails with unicode local parts, `ß`/`ss` and `ü`/`ue` spelling variants.
- A literal placeholder-shaped token (`PERSON_001`) that appears in source text only after that number was already
  issued in the run is restored to the issued value.
- A display name containing `"` or `\` can break the JSON parse of a reply (the restored name lands inside a JSON
  string before parsing). The failure is visible (salvaged/raw text or `AnalysisError`), not a privacy leak.
- Dev scripts (`scripts/compare_vision_models.py`) call vision without `setup()`, so the gate is off there.
- Teams and Outlook items are not anonymized (`summarizer` runs them under `anonymizer.skipped()`).
- Images (draw.io comparison) are not inspected; only the accompanying text prompt is.
- Local caches (`spec_cache/`, `url_baselines/`) and the sent email keep the real text; only the LLM request is masked.

## `on_directory_failure`

- `fail` (default): a failed directory load (HTTP error, missing `read:jira-user` scope, empty directory) raises a
  `RuntimeError` in `main.py` before any LLM call; non-zero exit, nothing sent, state untouched.
- `continue`: the run goes on with names from items + `additional_names` (emails and phones still masked) and a
  notice `ANONYMIZATION REDUCED: user directory unavailable (...); only names in anonymize.additional_names (and, in
  the digest and management summary, names of the people on fetched items) are replaced; ...` is raised: in the digest and mgmt-summary email
  notices (on the console when nothing is sent, or in the raised error), on the console only for `--spec-monitor` and
  `--page-watch`.

Related: the `exclude_pages` dropped-page counts are informational, not anonymization notices: email notice when
something is mailed, console `Info:` line otherwise, never a reason to fail the run (see `confluence.md`).

## Config validation

`anonymize.enabled` must be a YAML boolean (`"false"` or `1` is a `ValueError`), `additional_names` a list, and the
block a mapping.
