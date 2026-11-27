"""Replace names, emails and phone numbers with placeholders before text goes to an LLM, and restore them after."""
import re
import threading
from typing import Dict, Iterable, List, Optional, Tuple

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+|00)?\d[\d ()./\-]{7,}\d(?!\w)")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PHONE_SPLIT_RE = re.compile(r"(\s*/\s+|\s+/\s*|\s{2,})")
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
    return out


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
                    for ch in variant.lower():
                        node = node.setdefault(ch, {})
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
        date = _ISO_DATE_RE.match(raw)
        if date:
            return raw[:date.end()] + _PHONE_RE.sub(self._phone, raw[date.end():])
        digits = re.sub(r"\D", "", raw)
        if len(digits) > 15:
            # The greedy candidate can swallow several numbers ("079 ... / 0041..."); rescan the pieces so none leaks.
            pieces = _PHONE_SPLIT_RE.split(raw)
            if len(pieces) == 1:
                return raw
            return "".join(p if i % 2 else _PHONE_RE.sub(self._phone, p) for i, p in enumerate(pieces))
        if len(digits) < 9:
            return raw
        return self._issue("PHONE", _phone_key(raw), raw)

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
