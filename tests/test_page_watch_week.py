from datetime import date

import pytest

from digest.spec_monitor.week import Week, parse_week, previous_week, week_of


def test_previous_week_of_a_sunday():
    w = previous_week(date(2026, 11, 15))
    assert (w.start, w.end) == (date(2026, 11, 2), date(2026, 11, 8))
    assert w.label == "KW45 2026"
    assert w.title == "Page Watch KW45 2026"
    assert w.tag == "2026W45"
    assert w.file_stem == "CR-2026-KW45"


def test_previous_week_across_year_boundary():
    w = previous_week(date(2027, 1, 4))
    assert w.label == "KW53 2026" and w.start == date(2026, 12, 28)
    w = previous_week(date(2026, 1, 5))
    assert w.label == "KW01 2026" and w.start == date(2025, 12, 29)


def test_week_of_uses_iso_year():
    assert week_of(date(2025, 12, 29)).label == "KW01 2026"
    assert isinstance(week_of(date(2026, 11, 15)), Week)


@pytest.mark.parametrize("text", ["KW44-2026", "kw44 2026", "Kw44-2026"])
def test_parse_week_forms(text):
    assert parse_week(text, date(2026, 11, 15)).start == date(2026, 10, 26)


@pytest.mark.parametrize("text", ["KW99-2026", "KW53-2025", "2026-44", "", "KW0-2026", "KW44-26"])
def test_parse_week_rejects(text):
    with pytest.raises(ValueError):
        parse_week(text, date(2026, 11, 15))


def test_parse_week_rejects_future_but_allows_running_week():
    with pytest.raises(ValueError, match="future"):
        parse_week("KW47-2026", date(2026, 11, 15))
    assert parse_week("KW46-2026", date(2026, 11, 15)).start == date(2026, 11, 9)
