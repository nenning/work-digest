"""
stdlib-only wrapper for digest/main.py.

Spawns main.py as a subprocess, tees stdout/stderr to the console in real-time,
and shows a Windows MessageBoxW error popup if the process exits with a non-zero
code. This catches import-time failures (e.g. broken dependency versions) that
main.py cannot catch itself because they occur before its try/except block runs.

Ctrl+C inside the error dialog copies the full text to clipboard.

Uses ONLY Python stdlib -- safe to run even when digest/ dependencies are broken.
Windows Task Scheduler tasks should point to this file rather than digest/main.py.
"""
from __future__ import annotations

import ctypes
import io
import subprocess
import sys
import threading
from pathlib import Path

_MB_ICONERROR = 0x10
_MAX_POPUP_CHARS = 3000


def _show_error_popup(title: str, message: str) -> None:
    if len(message) > _MAX_POPUP_CHARS:
        message = "...(showing tail)\n\n" + message[-_MAX_POPUP_CHARS:]
    ctypes.windll.user32.MessageBoxW(0, message, title, _MB_ICONERROR)


def _tee(src, console_dest, buf: io.StringIO, lock: threading.Lock) -> None:
    try:
        for line in src:
            console_dest.write(line)
            console_dest.flush()
            with lock:
                buf.write(line)
    except Exception:
        pass


def main() -> int:
    script = Path(__file__).resolve().parent / "digest" / "main.py"
    proc = subprocess.Popen(
        [sys.executable, str(script)] + sys.argv[1:],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    buf, lock = io.StringIO(), threading.Lock()
    t_out = threading.Thread(target=_tee, args=(proc.stdout, sys.stdout, buf, lock), daemon=True)
    t_err = threading.Thread(target=_tee, args=(proc.stderr, sys.stderr, buf, lock), daemon=True)
    t_out.start()
    t_err.start()
    t_out.join()
    t_err.join()
    exit_code = proc.wait()

    if exit_code != 0:
        captured = buf.getvalue()
        _show_error_popup(
            "Work Digest -- Fatal Error",
            f"digest/main.py exited with code {exit_code}.\n\n"
            + (captured.strip() or "(no output captured)"),
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
