"""Main orchestrator: CLI entry point for the work-digest tool."""
from __future__ import annotations

import sys
from pathlib import Path

# When main.py is invoked directly (e.g. `python main.py` from digest/), Python adds
# digest/ to sys.path. We need its parent so that `from digest.X import Y` resolves.
# This mirrors what tests/conftest.py does for the test suite.
_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

import argparse
import concurrent.futures
import getpass
import keyring
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from digest.auth.atlassian import get_auth_header
from digest.auth.microsoft import get_token
from digest.config import load_config
from digest.email_sender import (
    send_via_smtp, send_via_com,
    send_mgmt_summary_via_smtp, send_mgmt_summary_via_com,
)
from digest.models import SourceItem, SummarizedItem
from digest.sources import confluence, jira, outlook, teams
from digest.sources.mgmt_jira import fetch_sprint, fetch_team_tickets
from digest.sources.mgmt_confluence import fetch_team_pages
from digest.state import get_last_run, load_state, process_lock, save_state
from digest.summarizer import LLMEndpointError, summarize_items, synthesize_mgmt_summary

log = logging.getLogger(__name__)

ALL_SOURCES = ["jira", "confluence", "teams", "outlook"]


def parse_since(s: str) -> datetime:
    """Parse a time offset or ISO 8601 string into a UTC-aware datetime.

    Accepts:
    - "2h"  → now - 2 hours
    - "7d"  → now - 7 days
    - "2w"  → now - 2 weeks
    - ISO 8601 string → parsed and forced to UTC if naive
    """
    s = s.strip()
    if s and s[-1] in ("h", "d", "w"):
        unit = s[-1]
        try:
            n = int(s[:-1])
        except ValueError:
            raise ValueError(f"Invalid time offset {s!r}: expected format like '2h', '7d', '2w'") from None
        if n <= 0:
            raise ValueError(f"Time offset must be positive, got {n!r}")
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return datetime.now(timezone.utc) - delta
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _print_model_stats(model_stats: dict) -> None:
    model_times = model_stats.get("times", {})
    model_errors = model_stats.get("errors", {})
    all_models = sorted(set(model_times) | set(model_errors))
    if not all_models:
        return
    print()
    print("  Model response times:")
    for model in all_models:
        times = model_times.get(model, [])
        errs = model_errors.get(model, 0)
        avg_str = f"{sum(times)/len(times):.2f}s avg ({len(times)} call{'s' if len(times) != 1 else ''})" if times else "no successful calls"
        err_str = f", {errs} error{'s' if errs != 1 else ''}" if errs else ""
        print(f"    {model}: {avg_str}{err_str}")


def _print_timing(t_fetch: float, t_sum: float, t_del: float, n_fetched: int, n_summarized: int) -> None:
    sep = "  " + "-" * 30
    print()
    print(f"  {'fetch':<11} {t_fetch:5.1f}s   {n_fetched} items")
    print(f"  {'summarize':<11} {t_sum:5.1f}s   {n_summarized} items")
    print(f"  {'deliver':<11} {t_del:5.1f}s")
    print(sep)
    print(f"  {'total':<11} {t_fetch + t_sum + t_del:5.1f}s")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Work digest CLI")
    parser.add_argument(
        "--setup-auth",
        action="store_true",
        help="Authenticate with M365 only and exit",
    )
    parser.add_argument(
        "--setup-smtp-auth",
        action="store_true",
        help="Store SMTP password in OS keyring and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print digest to stdout instead of sending email",
    )
    parser.add_argument(
        "--source",
        choices=ALL_SOURCES,
        default=None,
        help="Fetch only this source",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Override state-based since timestamp (e.g. '2h', '7d', or ISO datetime)",
    )

    # Management summary mode
    parser.add_argument(
        "--mgmt-summary",
        action="store_true",
        help="Generate a team management summary instead of a personal digest",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        default=None,
        metavar="DATE",
        help="Start date for management summary (ISO 8601 or offset like '7d')",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        default=None,
        metavar="DATE",
        help="End date for management summary (ISO 8601, defaults to now)",
    )
    parser.add_argument(
        "--sprint",
        default=None,
        metavar="NAME",
        help="Sprint name for management summary (auto-derives start/end dates from Jira board)",
    )
    parser.add_argument(
        "--assume-done",
        action="store_true",
        help="In management summary: treat in-progress tickets as completed in the narrative",
    )
    parser.add_argument(
        "--short",
        action="store_true",
        help="In management summary: produce a very short executive bullet list instead of paragraphs",
    )
    args = parser.parse_args()

    # Load config.yaml from the same directory as main.py so it works regardless of CWD.
    # This ensures Task Scheduler can invoke `python path\to\main.py` without needing to
    # set the working directory separately.
    config = load_config(Path(__file__).parent / "config.yaml")

    data_dir = config.data_dir
    state_file = data_dir / "state.json"
    cache_file = data_dir / "token_cache.bin"

    # --setup-auth: authenticate then exit
    if args.setup_auth:
        if not config.m365.enabled:
            print("M365 is disabled in config (m365.enabled: false). Nothing to authenticate.")
            return
        get_token(config.m365.tenant_id, cache_file, client_id=config.m365.client_id)
        print("M365 authentication successful.")
        return

    # --setup-smtp-auth: store SMTP password in OS keyring then exit
    if args.setup_smtp_auth:
        if config.smtp is None:
            raise RuntimeError(
                "No smtp block configured. Add an smtp: block to config.yaml first."
            )
        password = getpass.getpass(f"SMTP password for {config.smtp.username}: ")
        keyring.set_password("digest-smtp", config.smtp.username, password)
        print("SMTP authentication stored successfully.")
        return

    with process_lock(data_dir):
        if args.mgmt_summary:
            _run_mgmt_summary(args, config, cache_file)
        else:
            while True:
                try:
                    _run(args, config, state_file, cache_file)
                    break
                except LLMEndpointError as exc:
                    log.warning("%s -- waiting 10 min before retry", exc)
                    time.sleep(600)


def _run_mgmt_summary(args, config, cache_file: Path) -> None:
    mgmt_cfg = config.mgmt_summary
    if mgmt_cfg is None:
        raise RuntimeError(
            "mgmt_summary section is missing from config.yaml. "
            "Add it with at least jira_jql set to define the team's tickets."
        )

    # Validate mutually exclusive time options
    has_sprint = bool(args.sprint)
    has_range = bool(args.from_date or args.to_date)
    has_since = bool(args.since)
    if sum([has_sprint, has_range, has_since]) > 1:
        raise ValueError("--sprint, --since, and --from/--to are mutually exclusive")
    if not has_sprint and not has_range and not has_since:
        raise ValueError(
            "Management summary requires a time range. "
            "Use --sprint 'Sprint Name', --since 7d, or --from/--to."
        )

    atlassian_auth = get_auth_header(config.atlassian)
    needs_token = config.m365.enabled or (config.smtp and config.smtp.use_oauth2)
    if needs_token:
        m365_token: Optional[str] = get_token(
            config.m365.tenant_id, cache_file, client_id=config.m365.client_id
        )
    else:
        m365_token = None

    now_utc = datetime.now(timezone.utc)
    sprint_id: Optional[int] = None

    # --- Resolve time range ---
    if has_sprint:
        if mgmt_cfg.jira_board_id is None:
            raise ValueError(
                "--sprint requires jira_board_id to be set in the mgmt_summary config block"
            )
        print(f"Looking up sprint {args.sprint!r} on board {mgmt_cfg.jira_board_id}...")
        sprint_id, since, until, sprint_label = fetch_sprint(
            config.atlassian, atlassian_auth, mgmt_cfg.jira_board_id, args.sprint
        )
        label = sprint_label
        print(f"  Sprint found: {sprint_label} ({since.date()} - {until.date()})")
    elif has_since:
        since = parse_since(args.since)
        until = now_utc
        label = f"{since.strftime('%Y-%m-%d')} - {until.strftime('%Y-%m-%d')}"
    else:
        since = parse_since(args.from_date)
        until = parse_since(args.to_date) if args.to_date else now_utc
        label = f"{since.strftime('%Y-%m-%d')} - {until.strftime('%Y-%m-%d')}"

    _tz_label = now_utc.astimezone().strftime('%z')
    _tz_fmt = f"{_tz_label[:3]}:{_tz_label[3:]}" if len(_tz_label) == 5 else _tz_label
    time_range = f"{label} ({_tz_fmt})"
    subject = f"[Team Summary] {label}"
    if args.assume_done:
        subject += " (as-if done)"

    print(f"Range:  {since.strftime('%Y-%m-%d')} -> {until.strftime('%Y-%m-%d')}")
    if args.assume_done:
        print("  assume-done: in-progress tickets treated as completed")
    print()

    # --- Fetch Jira team tickets ---
    print("Fetching Jira team tickets...")
    jira_items, team_account_ids = fetch_team_tickets(
        config.atlassian, atlassian_auth, mgmt_cfg, since, until, sprint_id=sprint_id
    )
    done_n = sum(1 for i in jira_items if i.kind == "ticket_done")
    wip_n  = sum(1 for i in jira_items if i.kind == "ticket_wip")
    todo_n = sum(1 for i in jira_items if i.kind == "ticket_todo")
    print(f"  {len(jira_items)} ticket(s)  ({done_n} done, {wip_n} in-progress, {todo_n} todo)")
    print(f"  {len(team_account_ids)} unique team member(s)")

    # --- Fetch Confluence team pages ---
    print("Fetching Confluence team pages...")
    try:
        confluence_items = fetch_team_pages(
            config.atlassian, atlassian_auth, mgmt_cfg, since, until, team_account_ids
        )
        print(f"  {len(confluence_items)} page(s)")
    except Exception as exc:
        print(f"  WARNING: Confluence fetch failed ({exc.__class__.__name__}: {exc}); skipping.")
        confluence_items = []
    print()

    if not jira_items and not confluence_items:
        print("Nothing found for the given time range.")
        return

    # --- Synthesize narrative ---
    print("Synthesizing management narrative...")
    t_syn_start = time.monotonic()
    narrative = synthesize_mgmt_summary(
        jira_items,
        confluence_items,
        config.llm,
        label=label,
        assume_done=args.assume_done,
        language=config.language,
        short=args.short,
    )
    t_syn_end = time.monotonic()
    print(f"  Done ({t_syn_end - t_syn_start:.1f}s)")
    print()

    # --- Deliver ---
    recipient = mgmt_cfg.recipient or config.email.recipient
    if not recipient:
        raise RuntimeError(
            "email.recipient (or mgmt_summary.recipient) must be set in config.yaml"
        )

    if config.smtp:
        if not args.dry_run:
            print(f"Sending via SMTP to {recipient}...")
        send_mgmt_summary_via_smtp(
            narrative, jira_items, confluence_items,
            subject, config.email, config.smtp, recipient,
            dry_run=args.dry_run, time_range=time_range,
            m365_token=m365_token,
        )
    elif sys.platform == "win32":
        if not args.dry_run:
            print(f"Sending via Outlook COM to {recipient}...")
        send_mgmt_summary_via_com(
            narrative, jira_items, confluence_items,
            subject, config.email, recipient,
            dry_run=args.dry_run, time_range=time_range,
        )
    else:
        raise RuntimeError(
            "No smtp block configured and COM is Windows-only. "
            "Add an smtp: block to config.yaml and run --setup-smtp-auth."
        )


def _run(args, config, state_file: Path, cache_file: Path) -> None:
    state = load_state(state_file)

    # Authenticate with both backends
    atlassian_auth = get_auth_header(config.atlassian)
    needs_token = config.m365.enabled or (config.smtp and config.smtp.use_oauth2)
    if needs_token:
        m365_token: Optional[str] = get_token(
            config.m365.tenant_id, cache_file, client_id=config.m365.client_id
        )
    else:
        m365_token = None

    # Determine which sources to run
    _M365_SOURCES = {"teams", "outlook"}
    sources_to_run: List[str] = [args.source] if args.source else ALL_SOURCES
    if not config.m365.enabled:
        excluded = [s for s in sources_to_run if s in _M365_SOURCES]
        if excluded:
            log.info("M365 disabled -- skipping sources: %s", ", ".join(excluded))
        sources_to_run = [s for s in sources_to_run if s not in _M365_SOURCES]
    if not sources_to_run:
        print("No sources to fetch (all requested sources require M365 which is disabled).")
        return

    def _fetch_source(src: str) -> List[SourceItem]:
        since = parse_since(args.since) if args.since else get_last_run(state, src)
        if src == "jira":
            return jira.fetch(config.atlassian, atlassian_auth, since)
        elif src == "confluence":
            return confluence.fetch(config.atlassian, atlassian_auth, since)
        elif src == "teams":
            return teams.fetch(m365_token, since)
        elif src == "outlook":
            return outlook.fetch(m365_token, since)
        else:
            raise ValueError(f"Unknown source: {src!r}")

    # --- Print active time range ---
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()  # system local timezone; used for all display
    if args.since:
        since_override = parse_since(args.since)
        range_start = since_override
        print(f"Range:  {since_override.strftime('%Y-%m-%d %H:%M')} -> {now_utc.strftime('%Y-%m-%d %H:%M')} UTC  (--since {args.since})")
    else:
        range_start = min(get_last_run(state, src) for src in sources_to_run)
        print(f"Range:  (per source, now = {now_utc.strftime('%Y-%m-%d %H:%M')} UTC)")
        for src in sources_to_run:
            since_src = get_last_run(state, src)
            since_str = since_src.strftime('%Y-%m-%d %H:%M') if since_src.year > 1970 else "never"
            print(f"  {src:<12} since {since_str}")

    _tz_label = now_local.strftime('%z')  # e.g. '+0200'
    _tz_fmt = f"{_tz_label[:3]}:{_tz_label[3:]}" if len(_tz_label) == 5 else _tz_label
    if range_start.year > 1970:
        _start_local = range_start.astimezone()
        time_range = f"{_start_local.strftime('%Y-%m-%d %H:%M')} -> {now_local.strftime('%Y-%m-%d %H:%M')} ({_tz_fmt})"
    else:
        time_range = f"All time -> {now_local.strftime('%Y-%m-%d %H:%M')} ({_tz_fmt})"
    print()

    # --- Fetch ---
    print("Fetching...")
    all_items: List[SourceItem] = []
    fetched_sources: List[str] = []
    notices: List[str] = []
    t_fetch_start = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_src = {
            executor.submit(_fetch_source, src): src for src in sources_to_run
        }
        for future in concurrent.futures.as_completed(future_to_src):
            src = future_to_src[future]
            try:
                items = future.result()
                print(f"  {src:<12} {len(items):>3} item(s)")
                all_items.extend(items)
                fetched_sources.append(src)
            except Exception as exc:
                log.warning("Failed to fetch %s: %s", src, exc)
                notices.append(f"Could not fetch {src}: {exc}")

    t_fetch_end = time.monotonic()

    if not all_items:
        print(f"Nothing new since last run. ({t_fetch_end - t_fetch_start:.1f}s)")
        return

    # --- Summarize ---
    print()
    t_sum_start = time.monotonic()
    model_stats: dict = {}
    summarized: List[SummarizedItem] = summarize_items(all_items, config.llm, notices=notices, language=config.language, model_stats=model_stats)
    t_sum_end = time.monotonic()

    # --- Deliver ---
    print()
    t_del_start = time.monotonic()

    timing = {
        "t_fetch": t_fetch_end - t_fetch_start,
        "t_sum": t_sum_end - t_sum_start,
        "n_fetched": len(all_items),
        "n_summarized": len(summarized),
        "model_stats": model_stats,
    }

    recipient = config.email.recipient
    if not recipient:
        raise RuntimeError("email.recipient must be set in config.yaml")

    if config.smtp:
        if not args.dry_run:
            print(f"Sending via SMTP to {recipient}...")
        sent = send_via_smtp(
            summarized,
            config.email,
            config.smtp,
            recipient,
            dry_run=args.dry_run,
            now=now_local,
            notices=notices,
            time_range=time_range,
            timing=timing,
            m365_token=m365_token,
        )
    elif sys.platform == "win32":
        if not args.dry_run:
            print(f"Sending via Outlook COM to {recipient}...")
        sent = send_via_com(
            summarized,
            config.email,
            recipient,
            dry_run=args.dry_run,
            now=now_local,
            notices=notices,
            time_range=time_range,
            timing=timing,
        )
    else:
        raise RuntimeError(
            "No smtp block configured and COM is Windows-only. "
            "Add an smtp: block to config.yaml and run --setup-smtp-auth."
        )

    t_del_end = time.monotonic()

    # Update state only on real sends. Note: send_digest returns True even on dry-run,
    # so the `not args.dry_run` guard is what prevents state update in that case.
    if sent and not args.dry_run:
        now = datetime.now(timezone.utc)
        new_state: Dict[str, datetime] = dict(state)
        for src in fetched_sources:
            new_state[src] = now
        save_state(state_file, new_state)

    _print_timing(
        t_fetch_end - t_fetch_start,
        t_sum_end - t_sum_start,
        t_del_end - t_del_start,
        len(all_items),
        len(summarized),
    )
    _print_model_stats(model_stats)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        input("\nPress Enter to exit...")
        sys.exit(1)
