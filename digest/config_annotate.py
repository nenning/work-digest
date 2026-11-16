"""Keeps the real title of every Confluence page id in config.yaml visible as a comment behind the id.

    parent_page_id: "41484320"      # «Coremodul» this page and all its descendants are watched

Only the inline comment of `*page_id: <digits>` lines is touched; every other byte of the file (secrets,
other comments, encoding, line endings) stays as it was. A hand-written comment keeps its text behind
the «title» marker. Titles that cannot be fetched leave the line unchanged.
"""
import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

log = logging.getLogger(__name__)

_LINE = re.compile(r"^(?P<head>\s*\w*page_id:\s*)(?P<q>[\"']?)(?P<id>\d+)(?P=q)(?P<gap>[ \t]*)(?P<comment>#.*?)?(?P<eol>\r?\n)?$")
_MARKER = re.compile(r"^«.*?»\s*")


def _clean_title(title: str) -> str:
    return " ".join(title.replace("»", ">").replace("«", "<").split())


def _new_comment(old_comment: str, title: str) -> str:
    text = (old_comment or "").lstrip("#").strip()
    marker = _MARKER.match(text)
    if marker:
        text = text[marker.end():]
    elif text.casefold().startswith(title.casefold() + ":"):
        text = text[len(title) + 1:].strip()   # earlier hand-written "Title: explanation"
    return f"# «{title}»" + (f" {text}" if text else "")


def annotate_text(text: str, titles: Dict[str, str]) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        m = _LINE.match(line)
        title = titles.get(m.group("id")) if m else None
        if not m or not title:
            out.append(line)
            continue
        q = m.group("q")
        out.append(f"{m.group('head')}{q}{m.group('id')}{q}{m.group('gap') or '  '}"
                   f"{_new_comment(m.group('comment') or '', title)}{m.group('eol') or ''}")
    return "".join(out)


def page_ids(text: str) -> List[str]:
    ids: List[str] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m and m.group("id") not in ids:
            ids.append(m.group("id"))
    return ids


def _decode(raw: bytes) -> Tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "cp1252"


def annotate_config_file(path: Path, get_title: Callable[[str], str]) -> List[str]:
    """Rewrites the comments in `path`; returns one message per page id whose title could not be looked up
    (or for a file that could not be rewritten). `get_title` raises when the title is not available."""
    problems: List[str] = []
    raw = path.read_bytes()
    text, encoding = _decode(raw)
    titles: Dict[str, str] = {}
    for page_id in page_ids(text):
        try:
            title = _clean_title(get_title(page_id))
        except Exception as exc:
            problems.append(f"title of page {page_id} could not be looked up: {exc}")
            continue
        if title:
            titles[page_id] = title
    new_text = annotate_text(text, titles)
    if new_text == text:
        return problems
    try:
        data = new_text.encode(encoding, errors="replace")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as exc:
        problems.append(f"{path.name} could not be updated with the page titles: {exc}")
    return problems


def annotate_config(path: Path, config) -> None:
    """Looks the titles up in Confluence and writes them into `path`. Never fails the run: cosmetic."""
    from digest.auth.atlassian import get_auth_header
    from digest.config import RetryConfig
    from digest.spec_monitor.cache import SpecCache
    from digest.spec_monitor.confluence_client import ConfluenceClient

    try:
        if not page_ids(_decode(path.read_bytes())[0]):
            return
        retry = RetryConfig(attempts=2, backoff_seconds=1.0, timeout_seconds=20.0)
        client = ConfluenceClient(config.atlassian, get_auth_header(config.atlassian), retry,
                                  SpecCache(config.data_dir / "spec_cache"))
        problems = annotate_config_file(path, client.get_title)
    except Exception as exc:
        problems = [f"page titles were not written to {path.name}: {exc}"]
    for message in problems:
        log.warning("config page titles: %s", message)
        print(f"WARNING: config page titles: {message}", file=sys.stderr)
