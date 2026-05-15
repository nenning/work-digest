"""LLM summarizer: converts SourceItem list into SummarizedItem list."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time
from typing import List, Optional

import anthropic
import openai

from digest.config import LLMConfig
from digest.models import SourceItem, SummarizedItem

log = logging.getLogger(__name__)

VALID_PRIORITIES = {"action_needed", "meeting_invite", "fyi", "info"}
# Cap content sent to LLM to avoid token limit rejections (~4k chars ≈ ~1k tokens).
_CONTENT_MAX_CHARS = 4000

# Sentinel: LLM returned {"summary": null} → skip this item entirely.
_SKIP = object()

_LANGUAGE_NAMES: dict[str, str] = {
    "de": "German",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
}

# Static labels for new-ticket formatting (no LLM involved).
_NEW_TICKET_LABELS: dict[str, dict[str, str]] = {
    "de": {"created_by": "Erstellt von", "assigned_to": "Zugewiesen an", "unassigned": "Nicht zugewiesen", "new": "Neu"},
    "en": {"created_by": "Created by", "assigned_to": "Assigned to", "unassigned": "Unassigned", "new": "New"},
}
_DEFAULT_LABELS = _NEW_TICKET_LABELS["en"]


def _language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get(code.lower(), code)


def _build_prompt(item: SourceItem, language: str = "de") -> str:
    """Build an adaptive prompt for the given item."""
    content = item.content[:_CONTENT_MAX_CHARS]
    lang = _language_name(language)

    # Confluence page updates: summarise the diff, not the whole page.
    if item.source == "confluence" and item.kind in ("page_update", "page"):
        null_note = (
            'Return {"summary": null} (skip entirely) if the changes are purely cosmetic. '
            'Cosmetic changes include: typo/spelling corrections, grammar fixes, '
            'punctuation or whitespace changes, minor rewording without new meaning, '
            'hyphenation or capitalisation standardisation, year/number corrections in references, '
            'and sentence restructuring that preserves the original meaning. '
            'Only summarise if there is genuinely new information: new requirements, '
            'new sections, updated decisions, added or removed content with substantive meaning.'
        )
        return (
            f"The following shows what changed in the Confluence page '{item.title}'.\n"
            f"Summarise only the meaningful additions or removals in 1-2 sentences.\n"
            f"Respond in {lang}.\n"
            f"{null_note}\n\n"
            f"Changes:\n{content}\n\n"
            'Respond with JSON only: {"summary": "..."} or {"summary": null}'
        )

    if len(item.content) < 100:
        content_instruction = "The content is short — quote it verbatim in the summary."
    else:
        content_instruction = (
            "Write a compact, keyword-focused summary using short, telegraphic phrases. "
            "Incomplete sentences are fine. Prioritize information density over readability."
        )

    if item.source == "outlook":
        priority_instruction = (
            '\nAlso classify the email priority as exactly one of: '
            '"action_needed", "meeting_invite", "fyi", "info".'
        )
        json_template = '{"summary": "...", "priority": "info"}'
    else:
        priority_instruction = ""
        json_template = '{"summary": "..."}'

    return (
        f"You are summarizing a work digest item.\n"
        f"Source: {item.source}\n"
        f"Kind: {item.kind}\n"
        f"Title: {item.title}\n"
        f"Author: {item.author}\n"
        f"Content:\n{content}\n\n"
        f"Instructions:\n"
        f"- {content_instruction}\n"
        f"- Respond in {lang}.\n"
        f"- Do NOT repeat or paraphrase the title or ticket key — the reader already sees it.\n"
        f"- Include any action items or deadlines mentioned.\n"
        f"{priority_instruction}"
        f"\nRespond with JSON only: {json_template}"
    )


def _call_llm(prompt: str, config: LLMConfig, model: str) -> str:
    """Call the configured LLM with the given model and return the raw response text."""
    if config.provider in ("openai", "azure_openai"):
        return _call_openai(prompt, config, model)
    return _call_anthropic(prompt, config, model)


def _call_llm_timed(prompt: str, config: LLMConfig, model: str) -> tuple[str, float]:
    """Like _call_llm but raises TimeoutError if the call exceeds config.llm_timeout seconds.
    Returns (result, elapsed_seconds).
    """
    result: list = [None]
    exc: list = [None]
    elapsed: list = [0.0]

    def _target():
        t0 = time.monotonic()
        try:
            result[0] = _call_llm(prompt, config, model)
        except Exception as e:
            exc[0] = e
        finally:
            elapsed[0] = time.monotonic() - t0

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=config.llm_timeout)
    if t.is_alive():
        raise TimeoutError(f"LLM call to {model!r} timed out after {config.llm_timeout}s")
    if exc[0] is not None:
        raise exc[0]
    return result[0], elapsed[0]


def _call_openai(prompt: str, config: LLMConfig, model: str) -> str:
    if config.provider == "azure_openai":
        client = openai.AzureOpenAI(
            api_key=config.api_key,
            azure_endpoint=config.endpoint,
            api_version="2024-02-01",
        )
    else:
        kwargs: dict = {"api_key": config.api_key}
        if config.endpoint:
            kwargs["base_url"] = config.endpoint
        client = openai.OpenAI(**kwargs)

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    if not response.choices:
        raise ValueError("LLM returned empty choices list")
    return response.choices[0].message.content


def _call_anthropic(prompt: str, config: LLMConfig, model: str) -> str:
    kwargs: dict = {"api_key": config.api_key}
    if config.endpoint:
        kwargs["base_url"] = config.endpoint
    client = anthropic.Anthropic(**kwargs)

    response = client.messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    if not response.content:
        raise ValueError("Anthropic returned empty content list")
    return response.content[0].text


def _models_to_try(assigned: str, config: LLMConfig) -> List[str]:
    seen: set = set()
    order: List[str] = []
    for m in [assigned] + config.models + config.fallback_models:
        if m not in seen:
            seen.add(m)
            order.append(m)
    return order


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_response(raw: str, item: SourceItem):
    """Parse the LLM JSON response; return (summary, priority) or _SKIP sentinel.
    Priority from LLM is only applied for outlook items; all others keep their original.
    """
    try:
        data = json.loads(_strip_code_fence(raw))
        if not isinstance(data, dict):
            return raw, item.priority
        summary = data.get("summary", raw)
        if summary is None:
            return _SKIP, None
        if item.source == "outlook":
            raw_priority = data.get("priority", item.priority)
            priority = raw_priority if raw_priority in VALID_PRIORITIES else item.priority
        else:
            priority = item.priority
        return summary, priority
    except json.JSONDecodeError:
        return raw, item.priority


def _build_description_prompt(item: SourceItem, language: str = "de") -> str:
    """Build a prompt to summarise the description of a new Jira ticket."""
    description = item.metadata.get("description", "")[:_CONTENT_MAX_CHARS]
    lang = _language_name(language)
    return (
        f"Summarise the following Jira ticket description in compact, keyword-focused phrases in {lang}.\n"
        f"Incomplete sentences are fine. Do NOT repeat the ticket title.\n"
        f"Ticket: {item.title}\n\n"
        f"Description:\n{description}\n\n"
        'Respond with JSON only: {"summary": "..."}'
    )


def _format_new_ticket(item: SourceItem, description_summary: str | None = None, language: str = "de") -> SummarizedItem:
    labels = _NEW_TICKET_LABELS.get(language.lower(), _DEFAULT_LABELS)
    new_label = labels.get("new", "Neu")
    summary = f"{new_label}. {description_summary}" if description_summary else f"{new_label}."
    return SummarizedItem(
        source=item.source,
        kind=item.kind,
        title=item.title,
        url=item.url,
        summary=summary,
        author=item.author,
        timestamp=item.timestamp,
        priority=item.priority,
    )


def _summarize_new_ticket(
    item: SourceItem,
    config: LLMConfig,
    assigned_model: str,
    language: str,
) -> tuple[SummarizedItem, str | None, tuple[str, float] | None, list[str]]:
    """Returns (SummarizedItem, notice | None, (model, elapsed) | None, failed_models). Never raises."""
    desc = item.metadata.get("description", "").strip()
    desc_summary = None
    notice = None
    timing: tuple[str, float] | None = None
    failed_models: list[str] = []
    if desc:
        raw = None
        errors: list[tuple[str, Exception]] = []
        for model in _models_to_try(assigned_model, config):
            try:
                raw, elapsed = _call_llm_timed(_build_description_prompt(item, language), config, model)
                timing = (model, elapsed)
                break
            except Exception as exc:
                errors.append((model, exc))
                failed_models.append(model)
                log.debug("Model %s failed for '%s': %s", model, item.title[:40], exc)
        if raw is None:
            detail = "; ".join(f"{m}: {e}" for m, e in errors)
            notice = f"LLM unavailable for '{item.title[:60]}': {detail}"
        else:
            result, _ = _parse_response(raw, item)
            if result is not _SKIP:
                desc_summary = result
    return _format_new_ticket(item, desc_summary, language), notice, timing, failed_models


def _summarize_one_item(
    item: SourceItem,
    config: LLMConfig,
    assigned_model: str,
    language: str,
) -> tuple[SummarizedItem | None, str | None, tuple[str, float] | None, list[str]]:
    """Returns (SummarizedItem | None, notice | None, (model, elapsed) | None, failed_models).
    Returns None as first element for Confluence cosmetic-only skips. Never raises.
    """
    prompt = _build_prompt(item, language)
    raw = None
    notice = None
    timing: tuple[str, float] | None = None
    failed_models: list[str] = []
    errors: list[tuple[str, Exception]] = []
    for model in _models_to_try(assigned_model, config):
        try:
            raw, elapsed = _call_llm_timed(prompt, config, model)
            timing = (model, elapsed)
            break
        except Exception as exc:
            errors.append((model, exc))
            failed_models.append(model)
            log.debug("Model %s failed for '%s': %s", model, item.title[:40], exc)
    if raw is None:
        detail = "; ".join(f"{m}: {e}" for m, e in errors)
        notice = f"LLM unavailable for '{item.title[:60]}': {detail}"

    if raw is None:
        return SummarizedItem(
            source=item.source, kind=item.kind, title=item.title, url=item.url,
            summary=f"[LLM error — {detail}]",
            author=item.author, timestamp=item.timestamp, priority=item.priority,
        ), notice, None, failed_models

    result, priority = _parse_response(raw, item)
    if result is _SKIP:
        return None, notice, timing, failed_models

    return SummarizedItem(
        source=item.source, kind=item.kind, title=item.title, url=item.url,
        summary=result,
        author=item.author, timestamp=item.timestamp, priority=priority,
    ), notice, timing, failed_models


def summarize_items(
    items: List[SourceItem],
    config: LLMConfig,
    notices: Optional[List[str]] = None,
    language: str = "de",
    model_stats: Optional[dict] = None,
) -> List[SummarizedItem]:
    """Summarize each SourceItem using the configured LLM provider.

    - Jira new_ticket items are formatted directly without LLM.
    - Confluence page updates use a diff-aware prompt; null summary → item skipped.
    - LLM failures fall back to fallback_model if configured; both failing → notice added.
    - notices: optional list to append infrastructure error messages to.
    """
    if notices is None:
        notices = []

    if config.provider not in ("openai", "azure_openai", "anthropic"):
        raise ValueError(f"Unknown LLM provider: {config.provider}")

    # Suppress verbose HTTP logs from the LLM client libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    ticket_items = [i for i in items if i.source == "jira" and i.kind == "new_ticket"]
    llm_items = [i for i in items if not (i.source == "jira" and i.kind in ("new_ticket", "assignment"))]
    total = len(ticket_items) + len(llm_items)

    if total == 0:
        return []

    model_str = ", ".join(config.models)
    print(f"Summarizing {total} item(s) ({config.llm_workers} workers, models: {model_str})...")

    ticket_results: dict[int, tuple] = {}
    llm_results: dict[int, tuple] = {}
    completed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, config.llm_workers)) as executor:
        ticket_futures = {
            executor.submit(_summarize_new_ticket, item, config, config.models[idx % len(config.models)], language): idx
            for idx, item in enumerate(ticket_items)
        }
        llm_futures = {
            executor.submit(_summarize_one_item, item, config, config.models[idx % len(config.models)], language): idx
            for idx, item in enumerate(llm_items)
        }

        for future in concurrent.futures.as_completed(ticket_futures):
            ticket_results[ticket_futures[future]] = future.result()
            completed += 1
            print(f"\r  [{completed}/{total}]", end="", flush=True)

        for future in concurrent.futures.as_completed(llm_futures):
            llm_results[llm_futures[future]] = future.result()
            completed += 1
            print(f"\r  [{completed}/{total}]", end="", flush=True)

    print()  # end the \r progress line

    model_times: dict[str, list[float]] = {}
    model_errors: dict[str, int] = {}
    for idx in sorted(ticket_results):
        _, notice, timing, failed = ticket_results[idx]
        if notice:
            notices.append(notice)
        if timing:
            model_times.setdefault(timing[0], []).append(timing[1])
        for m in failed:
            model_errors[m] = model_errors.get(m, 0) + 1

    for idx in sorted(llm_results):
        _, notice, timing, failed = llm_results[idx]
        if notice:
            notices.append(notice)
        if timing:
            model_times.setdefault(timing[0], []).append(timing[1])
        for m in failed:
            model_errors[m] = model_errors.get(m, 0) + 1

    if model_stats is not None:
        model_stats["times"] = model_times
        model_stats["errors"] = model_errors

    results: List[SummarizedItem] = []

    for idx in sorted(ticket_results):
        results.append(ticket_results[idx][0])

    for idx in sorted(llm_results):
        summarized, _, _t, _f = llm_results[idx]
        if summarized is not None:
            results.append(summarized)

    return results
