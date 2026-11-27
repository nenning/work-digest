# Page exclusion and LLM anonymization

Ported from the `reqs` project (`config_annotator.py`, `anonymizer.py`, `traversal.py` exclude handling), adapted to the digest tool.

## Intent

1. **Exclude pages.** Some Confluence pages (meeting notes, sandboxes, personal scratch pages) are noise in every mode. A config list excludes them; the tool writes each excluded page's title as a comment behind its id on the next run so the config stays readable.
2. **Anonymize before the LLM.** Content from Jira and Confluence must not reach the LLM provider with personal data. Names, email addresses and phone numbers are replaced by placeholders before each call and restored in the answer, so the email the user reads shows real names.

## Goals / non-goals / anti-goals

Goals
- Exclusion applies to all four Confluence page sources: personal digest (`sources/confluence.py`), mgmt-summary (`sources/mgmt_confluence.py`), page-watch and spec-monitor discovery.
- Anonymization covers every LLM call that carries Jira/Confluence content: personal digest, mgmt-summary, spec-monitor, page-watch.
- Names restored in the final output (email / report / published page).
- Catch names, email addresses and phone numbers.

Non-goals
- Teams and Outlook items are not anonymized (explicitly out of scope for now); their prompts go through unchanged.
- No NER for people missing from the user directory (see `ideas.md`).
- Images sent to the vision LLM are not inspected (text part of the vision prompt is anonymized, pixels are not).

Anti-goals
- Silent partial anonymization. A privacy feature that quietly degrades is a failure even if the run "works".
- Anonymization that breaks the "summarize, split, merge, retry, failover" paths in `summarizer.py` / `spec_monitor/analysis.py`.

## Constraints

- CLAUDE.md rules apply: no silent failures (`problems.report`, raise on fatal), every `requests` call has `timeout=`, spec-monitor HTTP goes through `request_with_retry`.
- `config.yaml` rewrites must stay byte-preserving apart from the comment (existing `config_annotate.py` contract: encoding, line endings, secrets untouched).
- No comments in code except non-obvious WHY; terse.
- LLM calls run from worker threads: the anonymizer must be thread-safe.

## Approaches considered

- **A. One gate at the LLM call layer (chosen).** `_call_openai`, `_call_anthropic` and the vision call are the only exits for text. Anonymize the prompt just before sending, restore the response just after. One place, covers all modes, new prompts cannot bypass it, retry/failover/merge unchanged.
- B. Anonymize `SourceItem`s at fetch, restore at render. Matches the mental flow literally, but touches every source and renderer; page-watch and spec-monitor build their own prompts and would need separate handling.
- C. A plus local NER (spaCy/Presidio). Only way to catch people not in the directory; heavy dependency, weak German models. Deferred to `ideas.md`.

## Design

### Config

```yaml
exclude_pages:                  # global, all Confluence page sources
  page_ids:
    - "123456"                  # «Meeting notes»   <- written by the tool
  title_patterns:               # regex, case-insensitive, matched against the title
    - '\bSandbox\b'
anonymize:
  enabled: false                # default off
  additional_names: []          # people not in the Atlassian directory (customers, externals)
  on_directory_failure: fail    # fail | continue
```

An excluded page and its whole subtree are skipped (as in `reqs`). Placement of the keys (top level vs. under `atlassian`) is the builder's call; keep one block each.

### Exclusion

- Filter helper (one function, one place): given a page's id, title and ancestor ids, decide excluded. Used by all four sources. Subtree = any ancestor id in `page_ids`, or the page or an ancestor matching a title pattern.
- CQL sources prefer a server-side filter for ids if CQL supports it for `ancestor` (builder to verify); otherwise filter client-side with `expand=ancestors`. Title patterns are always client-side.
- page-watch: excluded pages and descendants drop out of the change list. spec-monitor: dropped from discovery. A watched/parent page that is itself excluded is a config error (raise).
- Excluded counts are reported as a non-lossy notice, never silently.
- Annotation: extend `config_annotate.py` so list items under `exclude_pages.page_ids` (`- "123"` / `- 123`) get `# «Title»`, same marker format and "keep hand-written text behind the marker" behaviour as the existing `*page_id:` lines. Runs where `annotate_config` already runs; a failed lookup leaves the line unchanged.

### Anonymization (`digest/anonymizer.py`)

- Entities and placeholders: `PERSON_017`, `EMAIL_003`, `PHONE_002`. Numbered per run, unique per person/string. Restore regex is case-insensitive and tolerant of surrounding text (German genitive `PERSON_017s`).
- Names: from the Atlassian user directory (Jira `/rest/api/3/users/search`, paginated, humans only, per distinct Atlassian site), authors/mentioned users seen in the fetched items, and `additional_names`. Matching as in `reqs`: at least two tokens, both token orders, case-insensitive, word-bounded, longest first. Placeholder restores to the canonical display name.
- Emails: regex. Phones: Swiss/international formats (`+41 79 123 45 67`, `079 123 45 67`, with spaces, dots, dashes, parentheses) with a minimum of about 9 digits so ticket keys, dates and versions do not match. A false positive is harmless because it is restored.
- Gate: a process-wide, thread-safe registry. `_call_openai` / `_call_anthropic` / vision text take the active anonymizer: `anonymize(prompt)` before sending, `restore(text)` on the answer. Callers pass a flag; `summarize_items` passes "off" for Teams/Outlook items.
- Placeholders that survive in the final output unrestored (LLM rewrote them) are reported via `problems.report` (non-lossy).
- Local caches (spec cache keyed by prompt) keep real text; they never leave the machine.

### Guarantee and limits (my call)

The guarantee is: full names (either token order), email addresses and phone numbers never leave the machine. A name alone (first name, surname, nickname, single-token display name) is NOT matched by default because it collides with ordinary words. A single token listed in `additional_names` IS matched (the user opted in explicitly). README and `anonymize.md` state this limit; the notice is not extended to warn per mention.

### Failure behaviour (my call)

`anonymize.on_directory_failure` (user-requested option):
- `fail` (default): if the user directory cannot be loaded (or returns zero human users), the run raises (non-zero exit, popup), in every mode.
- `continue`: the run goes on anonymizing with the names it does have (authors/mentioned users seen in the fetched items plus `additional_names`), emails and phones as usual, and adds a prominent notice to the email/report that name coverage is reduced. Emails and phones are always anonymized.

One shared loader, sites taken from the same Atlassian config the mode already uses (`config.atlassian`, including per-project `url` overrides, one call per distinct site), authenticated with the existing `get_auth_header`. A scoped token needs the Jira user-read scope (`read:jira-user`); README says so.

### Mentions on excluded pages (my call)

Exclusion drops page-update items. Confluence `mention` items (direct "you were mentioned") are not dropped: they are addressed to the user and dropping them would lose an action item. Excluded-page counts in the notice say how many page updates were excluded.

## Verification

- Unit tests: round trip names/emails/phones; reversed names; false-positive guard (ticket keys, ISO dates, versions); thread safety; restore of mangled placeholders; exclusion filter (id, title pattern, subtree); annotator (flow/block style, existing comments, CRLF, failed lookup).
- Gate test with a fake LLM client asserting the outgoing payload contains no directory name/email/phone and the returned text does.
- `python digest/main.py --dry-run` still passes; `python -m pytest tests/`.

## Docs

README (config + flags), `docs/architecture/` (new `anonymize.md`, exclusion in `confluence.md`/`page-watch.md`/`spec-monitor.md`), `config.yaml.example`, CLAUDE.md rule ("every LLM call goes through the anonymization gate"), new root `ideas.md`.

## `ideas.md` (to create)

Presidio/spaCy NER for names outside the directory; Teams/Outlook anonymization; transliteration variants (ü/ue); company/customer name masking; a `--show-anonymized` dry-run that prints what the LLM would receive; per-run leak report; local LLM fallback for sensitive items.

## Left to the builder

Config key placement; exact phone regex; CQL vs client-side id filtering; registry/placeholder numbering details; test layout.
