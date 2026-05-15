import json
import msvcrt
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

# Default "never run" timestamp returned for unknown sources
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@contextmanager
def process_lock(data_dir: Path):
    """Exclusive per-user lock that prevents two digest instances from running concurrently.

    Uses msvcrt.locking (Windows mandatory byte-range lock) on a lock file.
    Raises RuntimeError immediately if another instance holds the lock.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "digest.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        raise RuntimeError(
            "Another digest instance is already running. "
            "If you're sure no other instance is running, delete "
            f"{lock_path} and try again."
        )
    try:
        yield
    finally:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def load_state(state_file: Path) -> Dict[str, datetime]:
    """Returns {source: last_run_utc}. Returns {} if file missing or malformed."""
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text())
        return {
            k: datetime.fromisoformat(v["last_run"]).astimezone(timezone.utc)
            for k, v in data.items()
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}  # treat corrupt state as no prior runs


def save_state(state_file: Path, timestamps: Dict[str, datetime]) -> None:
    """Writes state via a temp file to protect against partial writes.
    Note: on Windows, os.replace is not strictly atomic (not POSIX rename)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    data = {k: {"last_run": v.isoformat()} for k, v in timestamps.items()}
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(state_file)


def get_last_run(state: Dict[str, datetime], source: str) -> datetime:
    """Returns the last run time for a source, or EPOCH if never run."""
    return state.get(source, EPOCH)
