from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Literal

Priority = Literal["action_needed", "meeting_invite", "fyi", "info"]


@dataclass
class SourceItem:
    source: str       # "jira" | "confluence" | "teams" | "outlook"
    kind: str         # "assignment" | "comment" | "mention" | "email" | ...
    title: str
    url: str
    content: str      # raw text for LLM
    author: str
    timestamp: datetime
    priority: Priority = "info"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")


@dataclass
class SummarizedItem:
    source: str
    kind: str
    title: str
    url: str
    summary: str
    author: str
    timestamp: datetime
    priority: Priority = "info"

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")


@dataclass
class MgmtSection:
    """One project's worth of content in a management summary email."""
    name: str                              # project display name
    label: str                             # this project's own resolved time-range label
    narrative: str
    jira_items: List[SourceItem]
    confluence_items: List[SummarizedItem]
