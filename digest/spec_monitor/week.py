"""ISO calendar weeks (Monday to Sunday) for the page-watch report."""
import re
from dataclasses import dataclass
from datetime import date, timedelta

_WEEK_RE = re.compile(r"^\s*KW(\d{1,2})[-\s](\d{4})\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Week:
    year: int
    number: int
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"KW{self.number:02d} {self.year}"

    @property
    def title(self) -> str:
        return f"Page Watch {self.label}"

    @property
    def tag(self) -> str:
        return f"{self.year}W{self.number:02d}"

    @property
    def file_stem(self) -> str:
        return f"CR-{self.year}-KW{self.number:02d}"


def _week(year: int, number: int) -> Week:
    start = date.fromisocalendar(year, number, 1)
    return Week(year, number, start, start + timedelta(days=6))


def week_of(day: date) -> Week:
    year, number, _ = day.isocalendar()
    return _week(year, number)


def previous_week(today: date) -> Week:
    return week_of(today - timedelta(days=7))


def parse_week(text: str, today: date) -> Week:
    m = _WEEK_RE.match(text or "")
    if not m:
        raise ValueError(f"week must look like KW44-2026, got: {text!r}")
    try:
        week = _week(int(m.group(2)), int(m.group(1)))
    except ValueError:
        raise ValueError(f"{text.strip()!r} is not a valid ISO week") from None
    if week.start > today:
        raise ValueError(f"{week.label} is in the future")
    return week
