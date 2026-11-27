# Ideas

## Anonymization

- NER (Presidio / spaCy) for names outside the Atlassian directory and `additional_names`.
- Teams / Outlook anonymization (currently skipped entirely).
- Transliteration variants: `ü`/`ue`, `ö`/`oe`, `ä`/`ae`, `ß`/`ss`.
- Phones glued to letters (`Tel0791234567`); emails with unicode local parts.
- Company / customer name masking (list-driven, like `additional_names`).
- `--show-anonymized`: dry-run that prints what the LLM would receive.
- Per-run leak report: placeholder counts per kind, unmatched capitalised tokens that look like names.
- Local LLM fallback for items too sensitive to send out.
