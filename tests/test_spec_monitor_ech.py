from collections import Counter
from datetime import date, datetime

import pytest
import requests

from digest.config import RetryConfig
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.ech import EchMonitor
from digest.spec_monitor.models import Classification, SourceKind, SourceRef, SpecInfo

BASE = "https://www.ech.ch"
LIST_URL = f"{BASE}/de/ech/ech-0108"
TODAY = date(2026, 11, 10)


class FakeResp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.ok = status < 400
        self.headers = {}
        self.content = text.encode("utf-8")
        self.text = text


def _list_html(rows):
    links = "".join(
        f'<a href="/de/ech/ech-0108/{v}">{v} {status} <time datetime="2024-01-01T12:00:00Z">01.01.2024</time></a>'
        for v, status in rows
    )
    return (
        '<a href="/de/ech/ech-0108/6.0.0" hreflang="de">Deutsch</a>'
        '<a href="/fr/ech/ech-0108/6.0.0" hreflang="fr">Français</a>'
        f"<div>{links}</div>"
    )


def _version_html(status, decided, approved_on=None):
    html = (
        f'<div class="field__label">Status</div><span class="t">{status}</span>'
        f'<div class="field__label">Beschlussdatum</div>'
        f'<div class="field__item"><time datetime="{decided}T12:00:00Z" class="datetime">x</time></div>'
    )
    if approved_on:
        html += (
            f'<div class="field__label">Genehmigt am</div>'
            f'<div class="field__item"><time datetime="{approved_on}T12:00:00Z">y</time></div>'
        )
    return html


ROWS = [
    ("7.0.0", "In Bearbeitung"),
    ("6.0.0", "Genehmigt"),
    ("5.1", "Abgelöst"),
    ("1.0", "Abgelöst"),
]
PAGES = {
    f"{LIST_URL}/6.0.0": _version_html("Genehmigt", "2024-03-20"),
    f"{LIST_URL}/5.1": _version_html("Abgelöst", "2024-03-20", approved_on="2020-11-25"),
    f"{LIST_URL}/1.0": _version_html("Abgelöst", "2010-09-01"),
    f"{LIST_URL}/7.0.0": _version_html("In Bearbeitung", "2027-01-01"),
}


@pytest.fixture
def web(mocker):
    calls = Counter()
    routes = {LIST_URL: _list_html(ROWS), **PAGES}

    def fake(method, url, **kwargs):
        calls[url] += 1
        assert kwargs.get("timeout")
        if url not in routes:
            return FakeResp(404, "nope")
        value = routes[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return FakeResp(*value)
        return FakeResp(200, value)

    mocker.patch("digest.spec_monitor.http.requests.request", side_effect=fake)
    return type("Web", (), {"calls": calls, "routes": routes})


@pytest.fixture
def monitor(tmp_path):
    return EchMonitor(RetryConfig(attempts=2, backoff_seconds=0), SpecCache(tmp_path), today=lambda: TODAY)


def _ref(**kw):
    kw.setdefault("name", "eCH-0108")
    kw.setdefault("kind", SourceKind.ECH)
    return SourceRef(**kw)


def _spec(**kw):
    kw.setdefault("page_id", "1")
    kw.setdefault("title", "Spec")
    kw.setdefault("url", "https://x/1")
    kw.setdefault("status", "Refined")
    return SpecInfo(**kw)


def test_handles_only_ech(monitor):
    assert monitor.handles(_ref())
    assert not monitor.handles(_ref(kind=SourceKind.I14Y))
    assert not monitor.handles(_ref(kind=SourceKind.CONFLUENCE))


def test_old_is_latest_approved_at_baseline_date_new_is_latest_approved_today(monitor, web):
    res = monitor.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert res.classification == Classification.CHANGED
    assert res.old.version == "5.1"
    assert res.old.inferred is True
    assert "2023-05-01" in res.old.inferred_reason
    assert res.old.status == "Abgelöst"
    assert res.old.url == f"{LIST_URL}/5.1"
    assert res.old.when == datetime(2020, 11, 25, 12, 0)
    assert res.new.version == "6.0.0"
    assert res.new.status == "Genehmigt"
    assert res.new.url == f"{LIST_URL}/6.0.0"
    assert res.new.when == datetime(2024, 3, 20, 12, 0)
    assert res.new.inferred is False


def test_non_approved_newer_version_is_ignored(monitor, web):
    res = monitor.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert res.new.version == "6.0.0"
    assert f"{LIST_URL}/7.0.0" not in web.calls


def test_baseline_after_latest_approved_is_unchanged(monitor, web):
    res = monitor.resolve(_ref(baseline_date=date(2025, 1, 1)), _spec())
    assert res.classification == Classification.UNCHANGED
    assert res.old.version == res.new.version == "6.0.0"


def test_decision_day_itself_counts_as_end_of_day(monitor, web):
    res = monitor.resolve(_ref(baseline_date=date(2024, 3, 20)), _spec())
    assert res.old.version == "6.0.0"


def test_stated_version_wins_and_is_not_inferred(monitor, web):
    res = monitor.resolve(_ref(baseline_version="5.1", baseline_date=date(2025, 1, 1)), _spec())
    assert res.classification == Classification.CHANGED
    assert res.old.version == "5.1"
    assert res.old.inferred is False


def test_stated_version_equal_to_latest_is_unchanged(monitor, web):
    res = monitor.resolve(_ref(baseline_version="v6.0.0"), _spec())
    assert res.classification == Classification.UNCHANGED
    assert res.old.inferred is False


def test_stated_version_not_listed_is_error(monitor, web):
    res = monitor.resolve(_ref(baseline_version="9.9"), _spec())
    assert res.classification == Classification.ERROR
    assert "9.9" in res.errors[0]


def test_fallback_to_spec_stand_date(monitor, web):
    res = monitor.resolve(_ref(), _spec(stand_date=date(2023, 5, 1), created=datetime(2010, 1, 1)))
    assert res.old.version == "5.1"
    assert res.old.inferred is True
    assert "Stand" in res.old.inferred_reason and "2023-05-01" in res.old.inferred_reason


def test_fallback_to_spec_creation_date(monitor, web):
    res = monitor.resolve(_ref(), _spec(created=datetime(2010, 10, 1, 9, 30)))
    assert res.classification == Classification.CHANGED
    assert res.old.version == "1.0"
    assert res.old.inferred is True
    assert "creation" in res.old.inferred_reason and "2010-10-01" in res.old.inferred_reason


def test_no_baseline_information_is_error(monitor, web):
    res = monitor.resolve(_ref(), _spec())
    assert res.classification == Classification.ERROR
    assert res.errors


def test_baseline_before_first_approved_version_is_error(monitor, web):
    res = monitor.resolve(_ref(baseline_date=date(2000, 1, 1)), _spec())
    assert res.classification == Classification.ERROR
    assert "2000-01-01" in res.errors[0]


def test_section_kept_in_warnings(monitor, web):
    ref = _ref(baseline_date=date(2025, 1, 1), section="Abschnitt 3.1.8")
    res = monitor.resolve(ref, _spec())
    assert any("check Abschnitt 3.1.8" in w for w in res.warnings)


def test_changed_notes_that_standard_content_is_not_compared(monitor, web):
    res = monitor.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert res.diff_text is None
    assert any("not compared" in w for w in res.warnings)


def test_standard_number_taken_from_url_or_text(monitor, web):
    ref = _ref(name="Unternehmensstammdaten", url="https://www.ech.ch/de/ech/ech-0108", baseline_date=date(2025, 1, 1))
    assert monitor.resolve(ref, _spec()).classification == Classification.UNCHANGED
    ref = _ref(name="Standard", note="siehe eCH 0108", baseline_date=date(2025, 1, 1))
    assert monitor.resolve(ref, _spec()).classification == Classification.UNCHANGED


def test_unknown_standard_number_is_not_monitored(monitor, web):
    res = monitor.resolve(_ref(name="eCH Standard"), _spec())
    assert res.classification == Classification.NOT_MONITORED
    assert res.warnings


def test_unreachable_site_is_error_with_attempts(monitor, web):
    web.routes[LIST_URL] = requests.ConnectionError("dns")
    res = monitor.resolve(_ref(baseline_date=date(2025, 1, 1)), _spec())
    assert res.classification == Classification.ERROR
    assert res.attempts == 2
    assert "dns" in res.errors[0]


def test_http_403_is_error(monitor, web):
    web.routes[LIST_URL] = (403, "denied")
    res = monitor.resolve(_ref(baseline_date=date(2025, 1, 1)), _spec())
    assert res.classification == Classification.ERROR
    assert "403" in res.errors[0]


def test_page_without_version_links_is_error(monitor, web):
    web.routes[LIST_URL] = "<html>maintenance</html>"
    res = monitor.resolve(_ref(baseline_date=date(2025, 1, 1)), _spec())
    assert res.classification == Classification.ERROR


def test_version_page_failure_is_error(monitor, web):
    web.routes[f"{LIST_URL}/5.1"] = (404, "gone")
    res = monitor.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert res.classification == Classification.ERROR
    assert "5.1" in res.errors[0]


def test_version_pages_are_cached_but_list_is_always_live(tmp_path, web):
    cache = SpecCache(tmp_path)
    ref = _ref(baseline_date=date(2023, 5, 1))
    for _ in range(2):
        mon = EchMonitor(RetryConfig(attempts=1, backoff_seconds=0), cache, today=lambda: TODAY)
        assert mon.resolve(ref, _spec()).classification == Classification.CHANGED
    assert web.calls[LIST_URL] == 2
    assert web.calls[f"{LIST_URL}/6.0.0"] == 1
    assert web.calls[f"{LIST_URL}/5.1"] == 1


def test_decision_date_falls_back_to_german_text(monitor, web):
    web.routes[f"{LIST_URL}/6.0.0"] = (
        '<div class="field__label">Status</div><span>Genehmigt</span>'
        '<div class="field__label">Beschlussdatum</div><div class="field__item"><time>20. März 2024</time></div>'
    )
    res = monitor.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert res.new.when.date() == date(2024, 3, 20)


def test_page_status_overrides_list_status(monitor, web):
    web.routes[f"{LIST_URL}/6.0.0"] = _version_html("Zurückgezogen", "2024-03-20")
    res = monitor.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert res.classification == Classification.UNCHANGED
    assert res.new.version == "5.1"


def test_corrupt_cache_entry_is_a_miss(tmp_path, web):
    cache = SpecCache(tmp_path)
    mon = EchMonitor(RetryConfig(attempts=1, backoff_seconds=0), cache, today=lambda: TODAY)
    mon.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    for p in tmp_path.rglob("*.json"):
        p.write_text("{not json", encoding="utf-8")
    res = mon.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert res.classification == Classification.CHANGED


def test_requests_use_configured_timeout(web, tmp_path):
    mon = EchMonitor(RetryConfig(attempts=1, backoff_seconds=0, timeout_seconds=77), SpecCache(tmp_path), today=lambda: TODAY)
    mon.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    calls = requests.request.call_args_list
    assert calls and all(c.kwargs["timeout"] == 77 for c in calls)


def test_version_link_with_unexpected_text_is_warned(monitor, web):
    web.routes[LIST_URL] = _list_html(ROWS) + '<a href="/de/ech/ech-0108/8.0.0">Version 8.0.0 Genehmigt</a>'
    res = monitor.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert any("ignored" in w and "8.0.0" in w for w in res.warnings)


def test_skipped_link_is_added_to_resolution_errors(monitor, web):
    web.routes[LIST_URL] = _list_html(ROWS) + '<a href="/de/ech/ech-0108/8.0.0">Version 8.0.0 Genehmigt</a>'
    res = monitor.resolve(_ref(baseline_version="9.9"), _spec())
    assert res.classification == Classification.ERROR
    assert "8.0.0" in res.errors[0]


def test_missing_status_label_is_warned_and_not_cached(tmp_path, web):
    web.routes[LIST_URL] = _list_html([("6.0.0", ""), ("5.1", "Abgelöst")])
    web.routes[f"{LIST_URL}/6.0.0"] = '<div class="field__label">Beschlussdatum</div><time datetime="2024-03-20T12:00:00Z">x</time>'
    cache = SpecCache(tmp_path)
    ref = _ref(baseline_date=date(2023, 5, 1))
    for _ in range(2):
        mon = EchMonitor(RetryConfig(attempts=1, backoff_seconds=0), cache, today=lambda: TODAY)
        res = mon.resolve(ref, _spec())
        assert any("no status found" in w and "6.0.0" in w for w in res.warnings)
    assert web.calls[f"{LIST_URL}/6.0.0"] == 2


def test_approved_version_without_date_is_warned(monitor, web):
    web.routes[f"{LIST_URL}/6.0.0"] = '<div class="field__label">Status</div><span>Genehmigt</span>'
    res = monitor.resolve(_ref(baseline_date=date(2023, 5, 1)), _spec())
    assert any("no parseable approval date" in w and "6.0.0" in w for w in res.warnings)
