from unittest.mock import MagicMock, patch

import pytest
import requests

from digest import anonymizer, problems
from digest.anonymizer import DirectoryError
from digest.config import (
    AnonymizeConfig, AtlassianConfig, Config, EmailConfig, LLMConfig, M365Config,
    ProjectConfig, ProjectJiraConfig, ScheduleConfig,
)
from digest.models import SourceItem

from datetime import datetime, timezone
from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_gate():
    problems.clear()
    anonymizer.configure(None)
    yield
    anonymizer.configure(None)
    problems.clear()


def _user(name, kind="atlassian"):
    return {"accountType": kind, "displayName": name}


def _resp(users, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = users
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status}")
    return r


def _atlassian(projects=None):
    return AtlassianConfig(url="https://a.example.com", email="e", api_token="t", projects=projects or [])


def _project(name, url=None):
    return ProjectConfig(name=name, jira=ProjectJiraConfig(project="X"), url=url)


def _config(anonymize, atlassian=None):
    return Config(
        atlassian=atlassian or _atlassian(),
        m365=M365Config(),
        llm=LLMConfig(provider="openai", api_key="k", models=["m"]),
        schedule=ScheduleConfig(times=["08:00"]),
        email=EmailConfig(),
        data_dir=Path("."),
        anonymize=anonymize,
    )


def test_load_directory_paginates_and_keeps_humans_only():
    pages = [
        _resp([_user("Hans Müller"), _user("Bot Thing", "app")]),
        _resp([_user("Anna Meier"), {"accountType": "atlassian"}, _user("Carl Kunde", "customer")]),
        _resp([]),
    ]
    with patch("digest.anonymizer.requests.get", side_effect=pages) as get:
        names = anonymizer.load_directory(_atlassian(), "Basic x")
    assert names == ["Hans Müller", "Anna Meier", "Carl Kunde"]
    assert get.call_count == 3
    first = get.call_args_list[0]
    assert first.args[0] == "https://a.example.com/rest/api/3/users/search"
    assert first.kwargs["params"] == {"startAt": 0, "maxResults": 1000}
    assert first.kwargs["timeout"] == 30
    assert first.kwargs["headers"]["Authorization"] == "Basic x"
    assert get.call_args_list[1].kwargs["params"]["startAt"] == 2
    assert get.call_args_list[2].kwargs["params"]["startAt"] == 5


def test_load_directory_covers_project_url_overrides():
    atl = _atlassian([
        _project("A"),
        _project("B", url="https://b.example.com/"),
        _project("C", url="https://b.example.com"),
    ])
    pages = {
        "https://a.example.com": [_resp([_user("Hans Müller")]), _resp([])],
        "https://b.example.com": [_resp([_user("Hans Müller"), _user("Anna Meier")]), _resp([])],
    }

    def fake_get(url, **kw):
        base = url.split("/rest/")[0]
        return pages[base].pop(0)

    with patch("digest.anonymizer.requests.get", side_effect=fake_get):
        names = anonymizer.load_directory(atl, "Basic x")
    assert names == ["Hans Müller", "Anna Meier"]
    assert not pages["https://a.example.com"] and not pages["https://b.example.com"]


def test_http_error_raises_directory_error():
    with patch("digest.anonymizer.requests.get", return_value=_resp([], status=403)):
        with pytest.raises(DirectoryError):
            anonymizer.load_directory(_atlassian(), "Basic x")


def test_network_error_raises_directory_error():
    with patch("digest.anonymizer.requests.get", side_effect=requests.ConnectionError("boom")):
        with pytest.raises(DirectoryError):
            anonymizer.load_directory(_atlassian(), "Basic x")


def test_page_cap_raises_directory_error():
    with patch("digest.anonymizer.requests.get", return_value=_resp([_user("Hans Müller")])):
        with pytest.raises(DirectoryError):
            anonymizer.load_directory(_atlassian(), "Basic x")


def test_zero_humans_raises():
    pages = [_resp([_user("Bot", "app")]), _resp([])]
    with patch("digest.anonymizer.requests.get", side_effect=pages):
        with pytest.raises(DirectoryError):
            anonymizer.load_directory(_atlassian(), "Basic x")


def test_setup_fail_mode_raises():
    cfg = _config(AnonymizeConfig(enabled=True, on_directory_failure="fail"))
    with patch("digest.anonymizer.requests.get", return_value=_resp([], status=500)):
        with pytest.raises(DirectoryError):
            anonymizer.setup(cfg, "Basic x")


def test_setup_continue_mode_sets_notice_and_still_anonymizes_emails():
    cfg = _config(AnonymizeConfig(enabled=True, additional_names=["Acme"], on_directory_failure="continue"))
    with patch("digest.anonymizer.requests.get", return_value=_resp([], status=500)):
        a = anonymizer.setup(cfg, "Basic x")
    assert a is not None
    first = anonymizer.take_notices()
    second = anonymizer.take_notices()
    assert first == second
    assert len(first) == 1 and first[0].startswith("ANONYMIZATION REDUCED:")
    assert "in the digest and management summary" in first[0]
    out = anonymizer.outgoing("Acme wrote to hans@example.com")
    assert "hans@example.com" not in out and "Acme" not in out
    assert anonymizer.incoming(out) == "Acme wrote to hans@example.com"


def test_setup_success_uses_directory_and_additional_names():
    cfg = _config(AnonymizeConfig(enabled=True, additional_names=["Acme"]))
    pages = [_resp([_user("Hans Müller")]), _resp([])]
    with patch("digest.anonymizer.requests.get", side_effect=pages):
        a = anonymizer.setup(cfg, "Basic x")
    assert a is not None
    assert anonymizer.take_notices() == []
    out = anonymizer.outgoing("Hans Müller and Acme; Hans alone")
    assert "Müller" not in out and "Acme" not in out
    assert "Hans alone" in out


def test_setup_disabled_leaves_gate_off():
    with patch("digest.anonymizer.requests.get") as get:
        assert anonymizer.setup(_config(AnonymizeConfig(enabled=False)), "Basic x") is None
    get.assert_not_called()
    assert anonymizer.outgoing("hans@example.com") == "hans@example.com"
    assert anonymizer.incoming("PERSON_001") == "PERSON_001"
    assert anonymizer.incoming(None) is None
    assert anonymizer.take_notices() == []


def test_skipped_context_disables_both_directions():
    anonymizer.configure(anonymizer.Anonymizer(["Hans Müller"]))
    masked = anonymizer.outgoing("Hans Müller")
    assert masked == "PERSON_001"
    with anonymizer.skipped():
        assert anonymizer.outgoing("Hans Müller") == "Hans Müller"
        assert anonymizer.incoming("PERSON_001") == "PERSON_001"
        with anonymizer.skipped(False):
            assert anonymizer.outgoing("Hans Müller") == "PERSON_001"
    assert anonymizer.outgoing("Hans Müller") == "PERSON_001"


def test_incoming_reports_unissued_placeholder():
    anonymizer.configure(anonymizer.Anonymizer(["Hans Müller"]))
    anonymizer.outgoing("Hans Müller")
    assert anonymizer.incoming("PERSON_001 and PERSON_009") == "Hans Müller and PERSON_009"
    notices = anonymizer.take_notices()
    assert len(notices) == 1 and "PERSON_009" in notices[0]
    assert anonymizer.take_notices() == []


def test_note_items_adds_authors():
    anonymizer.configure(anonymizer.Anonymizer())
    item = SourceItem(
        source="jira", kind="comment", title="t", url="u", content="c",
        author="Hans Müller, Anna Meier", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        metadata={"mention_authors": ["Peter Frei"], "mention_author": "Eva Roth", "assignee": "Lisa Berg"},
    )
    anonymizer.note_items([item])
    out = anonymizer.outgoing("Hans Müller Anna Meier Peter Frei Eva Roth Lisa Berg")
    assert out == "PERSON_001 PERSON_002 PERSON_003 PERSON_004 PERSON_005"


def test_note_items_is_noop_when_gate_off():
    item = SourceItem(
        source="jira", kind="comment", title="t", url="u", content="c",
        author="Hans Müller", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    anonymizer.note_items([item])
    assert anonymizer.outgoing("Hans Müller") == "Hans Müller"


def test_reset_reported_lets_a_cleared_problem_be_reported_again():
    anonymizer.configure(anonymizer.Anonymizer(["Hans Müller"]))
    anonymizer.incoming("PERSON_009")
    assert len(anonymizer.take_notices()) == 1
    anonymizer.incoming("PERSON_009")
    assert anonymizer.take_notices() == []
    anonymizer.reset_reported()
    anonymizer.incoming("PERSON_009")
    assert len(anonymizer.take_notices()) == 1
