from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

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
