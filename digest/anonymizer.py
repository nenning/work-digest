"""Replace names, emails and phone numbers with placeholders before text goes to an LLM, and restore them after."""
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import requests

from digest import problems
from digest.config import AtlassianConfig, Config
from digest.models import SourceItem

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+|00)?\d[\d \u00a0\u202f\u2013()./\-]{7,}\d(?!\w)")
_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"|(?:0?[1-9]|[12]\d|3[01])\.(?:0?[1-9]|1[0-2])\.(?:19|20)\d\d)(?!\d)"
)
_PHONE_TOKEN_RE = re.compile(r"\+?\d+")
_PLACEHOLDER_RE = re.compile(r"(?i)(PERSON|EMAIL|PHONE)_(\d+)")
_SEP = " "
_END = ""


def _name_key(text: str) -> str:
    return " ".join(t.replace(",", "") for t in text.lower().split())


def _variants(name: str) -> List[str]:
    tokens = [t.replace(",", "") for t in name.split()]
    tokens = [t for t in tokens if t]
    out = [name, " ".join(tokens), " ".join(reversed(tokens))]
    if len(tokens) == 2:
        out.append(f"{tokens[1]}, {tokens[0]}")
    elif len(tokens) >= 3:
        rest = " ".join(tokens[:-1])
        out += [f"{tokens[-1]} {rest}", f"{tokens[-1]}, {rest}"]
    return out


def _phone_chunks(tokens: List[re.Match]) -> Optional[List[Tuple[int, int]]]:
    """Partition tokens into consecutive (start, end) runs of 9 to 15 digits; None if impossible."""
    n = len(tokens)
    digits = [len(t.group(0).lstrip("+")) for t in tokens]
    cost: List[Optional[int]] = [0] + [None] * n
    prev = [0] * (n + 1)
    for i in range(n):
        if cost[i] is None:
            continue
        # A chunk not starting with 0 or + is less likely to be a real phone start, so it costs more.
        c = cost[i] + (1 if tokens[i].group(0)[0] in "0+" else 2)
        total = 0
        for j in range(i, n):
            total += digits[j]
            if total > 15:
                break
            if total >= 9 and (cost[j + 1] is None or c < cost[j + 1]):
                cost[j + 1], prev[j + 1] = c, i
    if cost[n] is None:
        return None
    chunks, end = [], n
    while end:
        chunks.append((prev[end], end - 1))
        end = prev[end]
    return chunks[::-1]


def _phone_key(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    return digits


def _trie_regex(node: dict) -> str:
    branches = [
        (r"\s+" if ch == _SEP else re.escape(ch)) + _trie_regex(child)
        for ch, child in sorted(node.items()) if ch != _END
    ]
    if not branches:
        return ""
    if _END in node:
        return "(?:" + "|".join(branches) + ")?"
    return branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"


class Anonymizer:
    """Thread-safe placeholder registry for one run."""

    def __init__(self, names: Iterable[str] = ()):
        self._lock = threading.Lock()
        self._trie: dict = {}
        self._person_by_key: Dict[str, str] = {}
        self._pattern: Optional[re.Pattern] = None
        self._placeholder: Dict[Tuple[str, str], str] = {}
        self._original: Dict[Tuple[str, int], str] = {}
        self._counter = {"PERSON": 0, "EMAIL": 0, "PHONE": 0}
        self.add_names(names)

    def add_names(self, names: Iterable[str], allow_single_token: bool = False) -> None:
        with self._lock:
            for raw in names:
                name = " ".join(str(raw).split())
                tokens = [t for t in (t.replace(",", "") for t in name.split()) if t]
                if not tokens or (len(tokens) < 2 and not allow_single_token):
                    continue
                for variant in _variants(name):
                    key = _name_key(variant)
                    if not key:
                        continue
                    self._person_by_key.setdefault(key, name)
                    node = self._trie
                    for ch in variant:
                        low = ch.lower()
                        # "İ".lower() is two characters; keep such letters as-is and let IGNORECASE handle them.
                        node = node.setdefault(low if len(low) == 1 else ch, {})
                    if _END not in node:
                        node[_END] = {}
                        self._pattern = None

    def _names_pattern(self) -> Optional[re.Pattern]:
        with self._lock:
            if self._pattern is None and self._trie:
                body = _trie_regex(self._trie)
                self._pattern = re.compile(r"(?<!\w)" + body + r"(?=s?(?!\w))", re.IGNORECASE)
            return self._pattern

    def _issue(self, kind: str, key: str, original: str) -> str:
        with self._lock:
            ph = self._placeholder.get((kind, key))
            if ph is None:
                self._counter[kind] += 1
                n = self._counter[kind]
                ph = f"{kind}_{n:03d}"
                self._placeholder[(kind, key)] = ph
                self._original[(kind, n)] = original
            return ph

    def _person(self, m: re.Match) -> str:
        key = _name_key(m.group(0))
        canonical = self._person_by_key.get(key, m.group(0))
        return self._issue("PERSON", _name_key(canonical), canonical)

    def _phone(self, m: re.Match) -> str:
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        date = _DATE_RE.search(raw)
        # A short candidate only loses a date at its start ("2026-11-27 10:50"); elsewhere it is likely part of the phone.
        if date and (len(digits) > 15 or date.start() == 0):
            pieces = _DATE_RE.split(raw)
            return "".join(p if i % 2 else _PHONE_RE.sub(self._phone, p) for i, p in enumerate(pieces))
        if len(digits) < 9:
            return raw
        if len(digits) <= 15:
            return self._issue("PHONE", _phone_key(raw), raw)
        # The greedy candidate swallows neighbouring numbers; split it into phones, and fail closed if that is impossible.
        tokens = list(_PHONE_TOKEN_RE.finditer(raw))
        if len(tokens) == 1:
            return raw
        chunks = _phone_chunks(tokens)
        if chunks is None:
            return self._issue("PHONE", _phone_key(raw), raw)
        out, pos = [], 0
        for i, j in chunks:
            start, end = tokens[i].start(), tokens[j].end()
            out += [raw[pos:start], self._issue("PHONE", _phone_key(raw[start:end]), raw[start:end])]
            pos = end
        return "".join(out) + raw[pos:]

    def anonymize(self, text: str) -> str:
        text = _EMAIL_RE.sub(lambda m: self._issue("EMAIL", m.group(0).lower(), m.group(0)), text)
        text = _PHONE_RE.sub(self._phone, text)
        pattern = self._names_pattern()
        if pattern is not None:
            text = pattern.sub(self._person, text)
        return text

    def _lookup(self, m: re.Match) -> Optional[str]:
        with self._lock:
            return self._original.get((m.group(1).upper(), int(m.group(2))))

    def restore(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            original = self._lookup(m)
            return m.group(0) if original is None else original
        return _PLACEHOLDER_RE.sub(repl, text)

    def leftover(self, text: str) -> List[str]:
        """Placeholder-shaped tokens in text that this instance never issued."""
        out: List[str] = []
        for m in _PLACEHOLDER_RE.finditer(text):
            if self._lookup(m) is None and m.group(0) not in out:
                out.append(m.group(0))
        return out


class DirectoryError(RuntimeError):
    """The Atlassian user directory could not be loaded."""


_DIRECTORY_PAGE_SIZE = 1000
_DIRECTORY_MAX_PAGES = 50
_REDUCED_PREFIX = "ANONYMIZATION REDUCED:"


def _directory_sites(atlassian: AtlassianConfig) -> List[AtlassianConfig]:
    sites, seen = [atlassian], {atlassian.jira_api_base.rstrip("/")}
    for project in atlassian.projects:
        if not project.url:
            continue
        site = atlassian.for_project(project)
        base = site.jira_api_base.rstrip("/")
        if base not in seen:
            seen.add(base)
            sites.append(site)
    return sites


def _site_users(site: AtlassianConfig, auth_header: str) -> List[str]:
    base = site.jira_api_base.rstrip("/")
    names: List[str] = []
    start = 0
    for _ in range(_DIRECTORY_MAX_PAGES):
        try:
            resp = requests.get(
                f"{base}/rest/api/3/users/search",
                headers={"Authorization": auth_header, "Accept": "application/json"},
                params={"startAt": start, "maxResults": _DIRECTORY_PAGE_SIZE},
                timeout=30,
            )
            resp.raise_for_status()
            page = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise DirectoryError(f"user directory request to {base} failed: {type(e).__name__}: {e}") from e
        if not isinstance(page, list):
            raise DirectoryError(f"user directory at {base} returned an unexpected response")
        if not page:
            return names
        start += len(page)
        names += [u["displayName"] for u in page
                  if isinstance(u, dict) and u.get("accountType") == "atlassian" and u.get("displayName")]
    raise DirectoryError(f"user directory at {base} exceeded {_DIRECTORY_MAX_PAGES} pages")


def load_directory(atlassian: AtlassianConfig, auth_header: str) -> List[str]:
    """Display names of all human Atlassian users on the global site and every per-project url override."""
    names: List[str] = []
    for site in _directory_sites(atlassian):
        names += _site_users(site, auth_header)
    names = list(dict.fromkeys(names))
    if not names:
        raise DirectoryError("user directory contains no human users")
    return names


_gate: Optional[Anonymizer] = None
_notice: Optional[str] = None
_reported: set = set()
_gate_lock = threading.Lock()
_skip: ContextVar[bool] = ContextVar("anonymizer_skip", default=False)


def configure(a: Optional[Anonymizer]) -> None:
    global _gate, _notice
    with _gate_lock:
        _gate = a
        _reported.clear()
        if a is None:
            _notice = None


def reset_reported() -> None:
    """Forget which placeholder problems were reported, for when the caller clears the problems."""
    with _gate_lock:
        _reported.clear()


def setup(config: Config, auth_header: str) -> Optional[Anonymizer]:
    global _notice
    cfg = config.anonymize
    if not cfg.enabled:
        configure(None)
        return None
    a = Anonymizer()
    a.add_names(cfg.additional_names, allow_single_token=True)
    notice = None
    try:
        a.add_names(load_directory(config.atlassian, auth_header))
    except DirectoryError as e:
        if cfg.on_directory_failure != "continue":
            raise
        notice = (f"{_REDUCED_PREFIX} user directory unavailable ({e}); only names seen in fetched items "
                  "and anonymize.additional_names are replaced, emails and phone numbers still are.")
    configure(a)
    with _gate_lock:
        _notice = notice
    return a


def _active() -> Optional[Anonymizer]:
    return None if _skip.get() else _gate


@contextmanager
def skipped(flag: bool = True) -> Iterator[None]:
    token = _skip.set(flag)
    try:
        yield
    finally:
        _skip.reset(token)


def outgoing(text: str) -> str:
    a = _active()
    return text if a is None else a.anonymize(text)


def incoming(text: Optional[str]) -> Optional[str]:
    a = _active()
    if a is None or text is None:
        return text
    restored = a.restore(text)
    with _gate_lock:
        fresh = [p for p in a.leftover(restored) if p not in _reported]
        _reported.update(fresh)
    if fresh:
        problems.report("anonymize", f"LLM output contains placeholders that were never issued: {', '.join(fresh)}", lossy=False)
    return restored


def note_items(items: Iterable[SourceItem]) -> None:
    a = _gate
    if a is None:
        return
    names: List[str] = []
    for item in items:
        names += item.author.split(", ")
        meta = item.metadata
        names += [n for n in meta.get("mention_authors") or [] if isinstance(n, str)]
        for key in ("mention_author", "assignee"):
            if isinstance(meta.get(key), str):
                names.append(meta[key])
    a.add_names(names)


def take_notices() -> List[str]:
    with _gate_lock:
        notices = [_notice] if _notice else []
    return notices + problems.take("anonymize")[0]
