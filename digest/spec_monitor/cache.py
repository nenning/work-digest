"""On-disk cache of the spec monitor (safe to delete anytime; state that must survive lives elsewhere,
see url_monitor baselines).

Entries are reused only when their key says the source did not change (page
version number, commit SHA, input hash). A corrupt or unreadable entry is a
miss, never an error -- but it is counted (`corrupt`), as are failed writes
(`write_errors`), so a read-only or full data directory shows up in the report.
"""
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from digest import usage


_REPLACE_LOCK = threading.Lock()  # on Windows concurrent os.replace onto one target raises PermissionError


def hash_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


class SpecCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.hits = 0
        self.misses = 0
        self.corrupt = 0
        self.write_errors = 0
        self.first_write_error: Optional[str] = None
        self._lock = threading.Lock()  # lookups run from several worker threads

    def _count(self, hit: bool, namespace: str = "") -> None:
        usage.record_cache(namespace, hit)
        with self._lock:
            if hit:
                self.hits += 1
            else:
                self.misses += 1

    def _note_corrupt(self) -> None:
        with self._lock:
            self.corrupt += 1

    def _note_write_error(self, exc: Exception) -> None:
        with self._lock:
            self.write_errors += 1
            if self.first_write_error is None:
                self.first_write_error = f"{type(exc).__name__}: {exc}"

    def _path(self, namespace: str, key: str, ext: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        if safe != key:
            safe = f"{safe}-{hash_key(key)[:8]}"  # sanitising must not make distinct keys collide
        return self.root / namespace / f"{safe}{ext}"

    @staticmethod
    def _replace(tmp: Path, p: Path) -> None:
        for attempt in range(5):
            try:
                with _REPLACE_LOCK:
                    os.replace(tmp, p)
                return
            except PermissionError:  # Windows: target briefly held open (scanner, concurrent reader)
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))

    @staticmethod
    def _write(p: Path, data: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")  # atomic: readers never see a half-written file; unique per write (threads share the pid)
        try:
            tmp.write_text(data, encoding="utf-8")
            SpecCache._replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)

    def get_text(self, namespace: str, key: str) -> Optional[str]:
        p = self._path(namespace, key, ".txt")
        try:
            value = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._count(False, namespace)
            return None
        except (OSError, UnicodeDecodeError):
            self._note_corrupt()
            self._count(False, namespace)
            return None
        self._count(True, namespace)
        return value

    def put_text(self, namespace: str, key: str, value: str) -> bool:
        p = self._path(namespace, key, ".txt")
        try:
            self._write(p, value)
        except OSError as exc:
            self._note_write_error(exc)  # best effort, but counted and reported
            return False
        return True

    def get_json(self, namespace: str, key: str) -> Optional[Any]:
        p = self._path(namespace, key, ".json")
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._count(False, namespace)
            return None
        except (OSError, UnicodeDecodeError, ValueError):
            self._note_corrupt()
            self._count(False, namespace)
            return None
        self._count(True, namespace)
        return value

    def put_json(self, namespace: str, key: str, value: Any) -> bool:
        p = self._path(namespace, key, ".json")
        try:
            self._write(p, json.dumps(value, ensure_ascii=False))
        except (OSError, TypeError, ValueError) as exc:
            self._note_write_error(exc)
            return False
        return True
