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
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from digest.auth.atlassian import get_auth_header
from digest.auth.microsoft import get_token
from digest.config import load_config
from digest.email_sender import send_digest, send_via_com
from digest.models import SourceItem, SummarizedItem
from digest.sources import confluence, jira, outlook, teams
from digest.state import get_last_run, load_state, process_lock, save_state
from digest.summarizer import summarize_items

log = logging.getLogger(__name__)

ALL_SOURCES = ["jira", "confluence", "teams", "outlook"]


def parse_since(s: str) -> datetime:
    """Parse a --since string into a UTC-aware datetime.

    Accepts:
    - "2h"  → now - 2 hours
    - ISO 8601 string → parsed and forced to UTC if naive
    """
    if s.endswith("h"):
        try:
            hours = int(s[:-1])
        except ValueError:
            raise ValueError(f"Invalid --since value {s!r}: expected format like '2h'") from None
        if hours <= 0:
            raise ValueError(f"--since hours must be positive, got {hours!r}")
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_recipient(token: str) -> str:
    """Return the current user's email address from the Graph /me endpoint.

    Raises RuntimeError if the address cannot be determined (e.g. guest account,
    malformed response, or network error). Callers should not proceed without a
    valid recipient.
    """
    try:
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to resolve recipient from Graph /me: {exc}") from exc

    address = data.get("mail") or data.get("userPrincipalName")
    if not address:
        raise RuntimeError(
            "Could not determine recipient email from Graph /me response. "
            "Neither 'mail' nor 'userPrincipalName' was present."
        )
    return address


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
    sep = "  " + "─" * 30
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
        help="Override state-based since timestamp (e.g. '2h' or ISO datetime)",
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

    with process_lock(data_dir):
        _run(args, config, state_file, cache_file)


def _run(args, config, state_file: Path, cache_file: Path) -> None:
    state = load_state(state_file)

    # Authenticate with both backends
    atlassian_auth = get_auth_header(config.atlassian)
    if config.m365.enabled:
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
            log.info("M365 disabled — skipping sources: %s", ", ".join(excluded))
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
        print(f"Range:  {since_override.strftime('%Y-%m-%d %H:%M')} → {now_utc.strftime('%Y-%m-%d %H:%M')} UTC  (--since {args.since})")
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
        time_range = f"{_start_local.strftime('%Y-%m-%d %H:%M')} → {now_local.strftime('%Y-%m-%d %H:%M')} ({_tz_fmt})"
    else:
        time_range = f"All time → {now_local.strftime('%Y-%m-%d %H:%M')} ({_tz_fmt})"
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

    if config.m365.enabled:
        recipient = get_recipient(m365_token)
        if args.dry_run:
            print("Dry run — no email sent.")
        else:
            print(f"Sending to {recipient}...")
        sent = send_digest(
            summarized,
            config.email,
            m365_token,
            recipient,
            dry_run=args.dry_run,
            now=now_local,
            notices=notices,
            time_range=time_range,
        )
    else:
        if not config.email.recipient:
            raise RuntimeError(
                "email.recipient must be set in config.yaml when m365.enabled is false"
            )
        if args.dry_run:
            print("Dry run — opening Outlook draft...")
        else:
            print(f"Sending via Outlook COM to {config.email.recipient}...")
        sent = send_via_com(
            summarized,
            config.email,
            config.email.recipient,
            dry_run=args.dry_run,
            now=now_local,
            notices=notices,
            time_range=time_range,
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
