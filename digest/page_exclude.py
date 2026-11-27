import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from digest import problems

EXCLUDED_SCOPE = "exclude_pages"


@dataclass(frozen=True)
class PageExclusion:
    page_ids: frozenset = frozenset()
    title_patterns: tuple = ()

    def __bool__(self) -> bool:
        return bool(self.page_ids or self.title_patterns)

    def is_excluded(self, page_id: str, title: str, ancestors: Sequence[Mapping] = ()) -> bool:
        """True when the page or any ancestor has an excluded id or a title matching a pattern."""
        for node_id, node_title in [(page_id, title)] + [(a.get("id"), a.get("title")) for a in ancestors]:
            if node_id is not None and str(node_id) in self.page_ids:
                return True
            if any(p.search(node_title or "") for p in self.title_patterns):
                return True
        return False


def _list(raw: dict, key: str) -> list:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"exclude_pages.{key} must be a list, got: {value!r}")
    return value


def load_exclusion(raw: Optional[dict]) -> PageExclusion:
    if raw is None:
        return PageExclusion()
    if not isinstance(raw, dict):
        raise ValueError(f"exclude_pages must be a mapping with page_ids and/or title_patterns, got: {raw!r}")
    patterns = []
    for pattern in _list(raw, "title_patterns"):
        try:
            patterns.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"exclude_pages.title_patterns: invalid regex {pattern!r}: {exc}") from None
    return PageExclusion(
        page_ids=frozenset(str(i).strip() for i in _list(raw, "page_ids") if str(i).strip()),
        title_patterns=tuple(patterns),
    )


def report_excluded(count: int) -> None:
    """Record an informational count of excluded pages; main.py shows it without treating it as an error."""
    problems.report(EXCLUDED_SCOPE, f"{count} page(s) excluded by exclude_pages", lossy=False)
