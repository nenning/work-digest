from datetime import datetime, timezone
from digest.models import SourceItem, SummarizedItem


def test_source_item_defaults():
    item = SourceItem(
        source="jira", kind="comment", title="T", url="http://x",
        content="c", author="a", timestamp=datetime.now(timezone.utc)
    )
    assert item.priority == "info"
    assert item.metadata == {}


def test_summarized_item():
    item = SummarizedItem(
        source="outlook", kind="email", title="T", url="http://x",
        summary="s", author="a", timestamp=datetime.now(timezone.utc),
        priority="action_needed"
    )
    assert item.priority == "action_needed"


def test_metadata_not_shared_between_instances():
    ts = datetime.now(timezone.utc)
    a = SourceItem(source="jira", kind="comment", title="T", url="u", content="c", author="a", timestamp=ts)
    b = SourceItem(source="jira", kind="comment", title="T", url="u", content="c", author="a", timestamp=ts)
    a.metadata["key"] = "value"
    assert "key" not in b.metadata


def test_naive_timestamp_raises():
    import pytest
    with pytest.raises(ValueError, match="timezone-aware"):
        SourceItem(source="jira", kind="comment", title="T", url="u", content="c", author="a",
                   timestamp=datetime(2026, 4, 9, 8, 0, 0))
