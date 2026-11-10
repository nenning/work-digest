from collections import Counter
from datetime import date, datetime
import json

import pytest
import requests

from digest.config import RetryConfig
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.i14y import I14yMonitor
from digest.spec_monitor.models import Classification, SourceKind, SourceRef, SpecInfo

API = "https://api.i14y.admin.ch"
TODAY = date(2026, 11, 10)


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.ok = status < 400
        self.headers = {}
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


def _entry(version, status, valid_from, *, id_="id-" , identifier="legalForm", title="Rechtsform eines Unternehmens",
           type_="Concept", concept_type="CodeList"):
    return {
        "id": f"{id_}{version}",
        "identifier": identifier,
        "title": {"de": title, "en": "Legal form of a company"},
        "type": type_,
        "conceptType": concept_type,
        "version": version,
        "registrationStatus": status,
        "validFrom": valid_from,
    }


ENTRIES = [
    _entry("1.2.0", "Standard", "2025-12-02T23:00:00+00:00"),
    _entry("1.1.0", "Superseded", "2010-12-31T23:00:00+00:00"),
    _entry("1.3.0", "Recorded", "2027-01-01T00:00:00+00:00"),
    {"id": "ds1", "type": "Dataset", "title": {"de": "Firmen nach Rechtsform"}, "identifier": "ds"},
]


@pytest.fixture
def web(mocker):
    state = {"payload": {"data": list(ENTRIES)}, "status": 200, "raises": None, "calls": [], "params": []}

    def fake(method, url, **kwargs):
        assert kwargs.get("timeout")
        state["calls"].append(url)
        state["params"].append(kwargs.get("params"))
        if state["raises"]:
            raise state["raises"]
        return FakeResp(state["status"], state["payload"])

    mocker.patch("digest.spec_monitor.http.requests.request", side_effect=fake)
    return state


@pytest.fixture
def monitor(tmp_path):
    return I14yMonitor(RetryConfig(attempts=2, backoff_seconds=0), SpecCache(tmp_path), today=lambda: TODAY)


def _ref(**kw):
    kw.setdefault("name", "i14y-Codeliste der Rechtsformen v1.2.0")
    kw.setdefault("kind", SourceKind.I14Y)
    return SourceRef(**kw)


def _spec(**kw):
    kw.setdefault("page_id", "1")
    kw.setdefault("title", "Spec")
    kw.setdefault("url", "https://x/1")
    kw.setdefault("status", "Refined")
    return SpecInfo(**kw)


def test_handles_only_i14y(monitor):
    assert monitor.handles(_ref())
    assert not monitor.handles(_ref(kind=SourceKind.ECH))


def test_stated_current_version_is_unchanged_and_query_is_cleaned(monitor, web):
    res = monitor.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert res.classification == Classification.UNCHANGED
    assert res.old.version == res.new.version == "1.2.0"
    assert res.old.inferred is False
    assert web["calls"] == [f"{API}/api/public/v1/search"]
    assert web["params"][0]["query"] == "Rechtsformen"


def test_stated_older_version_is_changed(monitor, web):
    res = monitor.resolve(_ref(baseline_version="1.1.0"), _spec())
    assert res.classification == Classification.CHANGED
    assert res.old.version == "1.1.0"
    assert res.old.status == "Superseded"
    assert res.new.version == "1.2.0"
    assert res.new.status == "Standard"
    assert res.new.when == datetime(2025, 12, 3, 0, 0)
    assert res.new.url == "https://www.i14y.admin.ch/de/concepts/id-1.2.0/description"


def test_unreleased_version_is_ignored_for_new(monitor, web):
    res = monitor.resolve(_ref(baseline_version="1.1.0"), _spec())
    assert res.new.version == "1.2.0"


def test_old_inferred_from_baseline_date(monitor, web):
    res = monitor.resolve(_ref(baseline_version=None, baseline_date=date(2024, 6, 1)), _spec())
    assert res.classification == Classification.CHANGED
    assert res.old.version == "1.1.0"
    assert res.old.inferred is True
    assert "2024-06-01" in res.old.inferred_reason


def test_old_inferred_from_stand_date_then_created(monitor, web):
    res = monitor.resolve(_ref(baseline_version=None), _spec(stand_date=date(2024, 6, 1)))
    assert res.old.version == "1.1.0" and "Stand" in res.old.inferred_reason
    res = monitor.resolve(_ref(baseline_version=None), _spec(created=datetime(2026, 1, 1)))
    assert res.old.version == "1.2.0" and "creation" in res.old.inferred_reason
    assert res.classification == Classification.UNCHANGED


def test_no_baseline_is_error(monitor, web):
    res = monitor.resolve(_ref(baseline_version=None), _spec())
    assert res.classification == Classification.ERROR


def test_stated_version_missing_is_error(monitor, web):
    res = monitor.resolve(_ref(baseline_version="0.9.0"), _spec())
    assert res.classification == Classification.ERROR
    assert "0.9.0" in res.errors[0]


def test_section_kept_in_warnings(monitor, web):
    res = monitor.resolve(_ref(baseline_version="1.2.0", section="Abschnitt 2"), _spec())
    assert any("check Abschnitt 2" in w for w in res.warnings)


def test_code_list_not_found_is_not_monitored_with_reason(monitor, web):
    web["payload"] = {"data": [ENTRIES[3]]}
    res = monitor.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert res.classification == Classification.NOT_MONITORED
    assert "Rechtsformen" in res.warnings[0]


def test_name_without_usable_words_is_not_monitored(monitor, web):
    res = monitor.resolve(_ref(name="i14y-Codeliste v1.2.0", baseline_version="1.2.0"), _spec())
    assert res.classification == Classification.NOT_MONITORED
    assert web["calls"] == []


def test_several_code_lists_pick_best_title_match(monitor, web):
    other = [_entry("1.0.0", "Standard", "2020-01-01T00:00:00+00:00", id_="o-", identifier="other", title="Kanton")]
    web["payload"] = {"data": other + ENTRIES}
    res = monitor.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert res.classification == Classification.UNCHANGED


def test_ambiguous_code_lists_are_not_monitored(monitor, web):
    a = _entry("1.0.0", "Standard", "2020-01-01T00:00:00+00:00", id_="a-", identifier="a", title="Alpha")
    b = _entry("1.0.0", "Standard", "2020-01-01T00:00:00+00:00", id_="b-", identifier="b", title="Beta")
    web["payload"] = {"data": [a, b]}
    res = monitor.resolve(_ref(baseline_version="1.0.0"), _spec())
    assert res.classification == Classification.NOT_MONITORED
    assert "ambiguous" in res.warnings[0]


def test_unreachable_api_is_error_with_attempts(monitor, web):
    web["raises"] = requests.ConnectionError("dns")
    res = monitor.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert res.classification == Classification.ERROR
    assert res.attempts == 2
    assert "dns" in res.errors[0]


def test_http_error_is_error(monitor, web):
    web["status"] = 403
    web["payload"] = None
    res = monitor.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert res.classification == Classification.ERROR
    assert "403" in res.errors[0]


def test_malformed_payload_is_error(monitor, web):
    web["payload"] = {"unexpected": True}
    res = monitor.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert res.classification in (Classification.ERROR, Classification.NOT_MONITORED)


def test_search_result_is_cached_with_ttl(tmp_path, web):
    cache = SpecCache(tmp_path)
    for _ in range(2):
        mon = I14yMonitor(RetryConfig(attempts=1, backoff_seconds=0), cache, today=lambda: TODAY)
        assert mon.resolve(_ref(baseline_version="1.2.0"), _spec()).classification == Classification.UNCHANGED
    assert len(web["calls"]) == 1


def test_expired_cache_entry_is_refetched(tmp_path, web):
    cache = SpecCache(tmp_path)
    mon = I14yMonitor(RetryConfig(attempts=1, backoff_seconds=0), cache, today=lambda: TODAY)
    mon.resolve(_ref(baseline_version="1.2.0"), _spec())
    for p in tmp_path.rglob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        data["t"] = 0
        p.write_text(json.dumps(data), encoding="utf-8")
    mon.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert len(web["calls"]) == 2


def test_requests_use_configured_timeout(web, tmp_path):
    mon = I14yMonitor(RetryConfig(attempts=1, backoff_seconds=0, timeout_seconds=77), SpecCache(tmp_path), today=lambda: TODAY)
    mon.resolve(_ref(baseline_version="1.2.0"), _spec())
    calls = requests.request.call_args_list
    assert calls and all(c.kwargs["timeout"] == 77 for c in calls)


def test_single_hit_without_title_overlap_is_warned(monitor, web):
    web["payload"] = {"data": [_entry("1.2.0", "Standard", "2025-12-02T23:00:00+00:00", title="Kantone und Gemeinden")]}
    res = monitor.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert any("shares no word" in w and "Kantone" in w for w in res.warnings)


def test_single_hit_with_matching_title_has_no_overlap_warning(monitor, web):
    res = monitor.resolve(_ref(baseline_version="1.2.0"), _spec())
    assert not any("shares no word" in w for w in res.warnings)


def test_paged_search_result_is_warned_and_not_cached(tmp_path, web):
    web["payload"] = {"data": list(ENTRIES), "totalCount": 120}
    cache = SpecCache(tmp_path)
    for _ in range(2):
        mon = I14yMonitor(RetryConfig(attempts=1, backoff_seconds=0), cache, today=lambda: TODAY)
        res = mon.resolve(_ref(baseline_version="1.2.0"), _spec())
        assert any("of 120 hits" in w for w in res.warnings)
    assert len(web["calls"]) == 2
