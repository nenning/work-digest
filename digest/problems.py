"""Collects non-fatal problems raised deep inside fetch/summarize code so they reach the user.

A bare `log.warning(...)` in a worker thread only lands on stderr, which nobody reads on a
scheduled run, while the run still looks successful. Code that skips or degrades an item
reports it here instead; the orchestrator drains the scope after each stage and adds the
messages to the email notices / console output.

`lossy=True` means data was dropped (a ticket, page or message is missing from the result);
the orchestrator then does not advance that source's last-run timestamp, so the next run
retries the window. `lossy=False` is for cosmetic degradation (a fallback was used).
"""
import logging
import threading
from typing import List, Tuple

log = logging.getLogger(__name__)

_lock = threading.Lock()
_problems: List[Tuple[str, str, bool]] = []


def report(scope: str, message: str, lossy: bool = True) -> None:
    log.warning("[%s] %s", scope, message)
    with _lock:
        _problems.append((scope, message, lossy))


def take(scope: str) -> Tuple[List[str], bool]:
    """Remove and return (messages, any_lossy) for `scope`."""
    with _lock:
        mine = [p for p in _problems if p[0] == scope]
        _problems[:] = [p for p in _problems if p[0] != scope]
    return [m for _, m, _ in mine], any(lossy for _, _, lossy in mine)


def clear() -> None:
    with _lock:
        _problems.clear()
