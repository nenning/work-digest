import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


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


def load_exclusion(raw: Optional[dict]) -> PageExclusion:
    if not raw:
        return PageExclusion()
    patterns = []
    for pattern in raw.get("title_patterns") or []:
        try:
            patterns.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"exclude_pages.title_patterns: invalid regex {pattern!r}: {exc}") from None
    return PageExclusion(
        page_ids=frozenset(str(i).strip() for i in (raw.get("page_ids") or []) if str(i).strip()),
        title_patterns=tuple(patterns),
    )
