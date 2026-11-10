import json
import logging
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from digest.config import AtlassianConfig, RetryConfig, TicketConfig
from digest.spec_monitor import runner, ticket
from digest.spec_monitor.models import (
    Classification, DeltaChange, EffortEstimate, OpenPoint, ReportError, RunReport, SourceKind, SourceRef,
    SourceResult, SpecAnalysis, SpecInfo, VersionPoint,
)
from digest.spec_monitor.ticket import TicketError, build_ticket, create_ticket, run_create_ticket

BASE = "https://example.atlassian.net"
TODAY = date(2026, 11, 10)
TCFG = TicketConfig(project="RMEG", issue_type="Task", labels=["quellen"], link_type="Relates")


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class FakeApi:
    """Records every call; responses by (method, path suffix)."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), handler in self.routes.items():
            if m == method and url.endswith(suffix):
                return handler(kwargs) if callable(handler) else handler
        raise AssertionError(f"unexpected call {method} {url}")

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def _analysis(changed=True, effort=True, jira=("RMEG-12", "RMEG-13")):
    spec = SpecInfo(page_id="1", title="Batch Verarbeitung", url=f"{BASE}/wiki/spaces/X/pages/1",
                    status="Refined", jira=list(jira))
    src = SourceResult(
        ref=SourceRef(name="eCH-0058", kind=SourceKind.ECH, used_for="Meldungen", url="https://ech.ch/0058"),
        classification=Classification.CHANGED if changed else Classification.UNCHANGED,
        old=VersionPoint(version="6.0.0", inferred=True, inferred_reason="from Stand date 2026-10-27"),
        new=VersionPoint(version="6.1.0", url="https://ech.ch/0058/6.1.0"),
        diff_summary="Neues Pflichtfeld X",
    )
    return SpecAnalysis(
        spec=spec, sources=[src],
        change_request="Pflichtfeld X muss unterstützt werden.",
        delta_changes=[
            DeltaChange(kind="new", affected_part="3.1 Meldung", ids=["CM-FR-03"], change="Feld X", reason="eCH", blocking=True),
            DeltaChange(kind="removed", affected_part="3.2", change="Feld Y entfällt"),
        ],
        open_points=[OpenPoint(question="Wie wird Feld X befüllt?", raised_by="eCH-0058")],
        effort=EffortEstimate(size="L", person_days="5-8", rationale="viel") if effort else None,
        completeness_warnings=["Lücke in den Quellen"],
        errors=[ReportError(scope="Batch", message="HTTP 500", impact="Quelle fehlt")],
    )


def _config(tmp_path, ticket_cfg=TCFG):
    return SimpleNamespace(
        atlassian=AtlassianConfig(url=BASE + "/", email="e@x.com", api_token="t"),
        spec_monitor=SimpleNamespace(ticket=ticket_cfg, retry=RetryConfig(attempts=1, backoff_seconds=0)),
        data_dir=tmp_path, language="de",
    )


def _args(**kw):
    d = dict(create_ticket="Batch Verarbeitung", dry_run=False)
    d.update(kw)
    return SimpleNamespace(**d)


@pytest.fixture
def wired(monkeypatch):
    def _wire(analysis, api):
        report = RunReport(generated_at=datetime(2026, 11, 10), analysed=[analysis] if analysis else [])
        seen = {}

        def fake_run(config, **kw):
            seen.update(kw)
            return report

        monkeypatch.setattr(runner, "run_spec_monitor", fake_run)
        monkeypatch.setattr(ticket, "request_with_retry", api)
        monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
        return seen
    return _wire


def _routes(create=None, link=None, remote=None):
    return {
        ("POST", "/rest/api/2/issue"): create or FakeResp(201, {"key": "RMEG-99"}),
        ("POST", "/rest/api/2/issueLink"): link or FakeResp(201),
        ("POST", "/remotelink"): remote or FakeResp(201),
    }


# --- build_ticket (pure) ---------------------------------------------------------

def test_summary_and_labels():
    d = build_ticket(_analysis(), TCFG, "de", today=TODAY)
    assert d.summary == "[DeltaSpec] [Batch Verarbeitung] [2026-11-10]"
    assert d.labels == ["quellen", "spec-monitor"]
    assert d.link_keys == ["RMEG-12", "RMEG-13"]
    assert d.spec_url.endswith("/pages/1")


def test_summary_truncated_to_255_keeps_brackets():
    a = _analysis()
    a.spec.title = "T" * 300
    s = build_ticket(a, TCFG, "de", today=TODAY).summary
    assert len(s) <= 255
    assert s.startswith("[DeltaSpec] [T") and s.endswith("…] [2026-11-10]")


def test_summary_exact_title_example():
    a = _analysis()
    a.spec.title = "API CustomerManagement"
    assert build_ticket(a, TCFG, "en", today=TODAY).summary == "[DeltaSpec] [API CustomerManagement] [2026-11-10]"


def test_summary_default_today_is_real_date():
    assert build_ticket(_analysis(), TCFG, "de").summary.endswith(f"[{date.today().isoformat()}]")


def test_labels_not_duplicated():
    cfg = TicketConfig(project="P", labels=["spec-monitor", "x"])
    assert build_ticket(_analysis(), cfg, "de").labels == ["spec-monitor", "x"]


def test_description_order_change_request_first():
    desc = build_ticket(_analysis(), TCFG, "de").description
    heads = [l for l in desc.splitlines() if l.startswith("h2.")]
    assert heads[0] == "h2. Änderungsantrag"
    pos = [desc.index(h) for h in ("h2. Änderungsantrag", "h2. Quellen alt gegen neu", "h2. Delta-Spezifikation",
                                    "h2. Vor der Umsetzung zu klären", "h2. Vollständigkeitshinweise",
                                    "h2. Fehler und Einschränkungen")]
    assert pos == sorted(pos)
    assert "Pflichtfeld X muss unterstützt werden." in desc


def test_description_content():
    desc = build_ticket(_analysis(), TCFG, "de").description
    assert "[Batch Verarbeitung|" in desc and "RMEG-12" in desc and "Refined" in desc
    assert "[eCH-0058|https://ech.ch/0058]" in desc
    assert "[v6.1.0|https://ech.ch/0058/6.1.0]" in desc or "[6.1.0|https://ech.ch/0058/6.1.0]" in desc
    assert "abgeleitet" in desc
    assert "CM-FR-03" in desc and "blockierend" in desc.lower()
    assert "h3. Neu" in desc and "h3. Entfallen" in desc
    assert "Wie wird Feld X befüllt?" in desc
    assert "Lücke in den Quellen" in desc and "HTTP 500" in desc


@pytest.mark.parametrize("lang", ["de", "en"])
def test_no_effort_estimate_anywhere(lang):
    d = build_ticket(_analysis(effort=True), TCFG, lang)
    text = (d.summary + d.description).lower()
    for forbidden in ("aufwand", "personentag", "person-day", "person days", "effort", "5-8"):
        assert forbidden not in text


def test_wiki_markup_is_escaped():
    a = _analysis()
    a.delta_changes[0].change = "Feld {code} | [x] *fett*"
    desc = build_ticket(a, TCFG, "de").description
    assert "\\{code\\}" in desc and "\\|" in desc and "\\*fett\\*" in desc


def test_unsafe_url_not_linked():
    a = _analysis()
    a.sources[0].ref.url = "javascript:alert(1)"
    assert "javascript:" not in build_ticket(a, TCFG, "de").description


# --- create_ticket ---------------------------------------------------------------

def test_create_payload_and_url(monkeypatch, tmp_path):
    api = FakeApi(_routes())
    monkeypatch.setattr(ticket, "request_with_retry", api)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    d = build_ticket(_analysis(), TCFG, "de")
    url = create_ticket(_config(tmp_path), d)
    assert url == f"{BASE}/browse/RMEG-99"
    (_, u, kw), = api.posts("/rest/api/2/issue")
    assert u == f"{BASE}/rest/api/2/issue"
    f = kw["json"]["fields"]
    assert f["project"] == {"key": "RMEG"} and f["issuetype"] == {"name": "Task"}
    assert f["labels"] == ["quellen", "spec-monitor"] and f["summary"] == d.summary
    assert kw["headers"]["Authorization"] == "Basic abc"
    links = api.posts("/rest/api/2/issueLink")
    assert len(links) == 2
    body = links[0][2]["json"]
    assert body["type"] == {"name": "Relates"}
    assert {body["inwardIssue"]["key"], body["outwardIssue"]["key"]} == {"RMEG-99", "RMEG-12"}


def test_link_failure_keeps_ticket(monkeypatch, tmp_path, capsys):
    api = FakeApi(_routes(link=FakeResp(404, text="no such issue")))
    monkeypatch.setattr(ticket, "request_with_retry", api)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    d = build_ticket(_analysis(), TCFG, "de")
    assert create_ticket(_config(tmp_path), d).endswith("RMEG-99")
    err = capsys.readouterr()
    assert "RMEG-12" in err.out + err.err and "link" in (err.out + err.err).lower()


def test_create_failure_raises_ticket_error(monkeypatch, tmp_path):
    api = FakeApi(_routes(create=FakeResp(400, text='{"errors":{"issuetype":"bad"}}')))
    monkeypatch.setattr(ticket, "request_with_retry", api)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    with pytest.raises(TicketError) as exc:
        create_ticket(_config(tmp_path), build_ticket(_analysis(), TCFG, "de"))
    assert "400" in str(exc.value)


def test_request_failed_becomes_ticket_error(monkeypatch, tmp_path):
    from digest.spec_monitor.http import RequestFailed

    def boom(*a, **k):
        raise RequestFailed("u", 3, "timeout")

    monkeypatch.setattr(ticket, "request_with_retry", boom)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    with pytest.raises(TicketError):
        create_ticket(_config(tmp_path), build_ticket(_analysis(), TCFG, "de"))


# --- run_create_ticket -----------------------------------------------------------

def test_run_creates_ticket(wired, tmp_path, capsys):
    api = FakeApi(_routes())
    seen = wired(_analysis(), api)
    run_create_ticket(_args(), _config(tmp_path))
    assert seen["only"] == "Batch Verarbeitung" and "--create-ticket" in seen["command_line"]
    assert f"{BASE}/browse/RMEG-99" in capsys.readouterr().out
    assert len(api.posts("/rest/api/2/issue")) == 1


def test_run_missing_ticket_config(wired, tmp_path):
    wired(_analysis(), FakeApi())
    with pytest.raises(RuntimeError, match="ticket"):
        run_create_ticket(_args(), _config(tmp_path, ticket_cfg=None))


def test_run_no_changes(wired, tmp_path, capsys):
    api = FakeApi()
    a = _analysis(changed=False)
    a.errors = []
    a.completeness_warnings = []
    wired(a, api)
    run_create_ticket(_args(), _config(tmp_path))
    assert "no source changes for Batch Verarbeitung; no ticket created" in capsys.readouterr().out
    assert api.calls == []


def _all_errored(spec_level=True):
    a = _analysis(changed=False)
    a.sources[0].classification = Classification.ERROR
    a.sources[0].errors = ["GitLab unreachable: HTTP 503"]
    a.errors = [ReportError(scope="eCH-0058", message="analysis raised ValueError")] if spec_level else []
    return a


def test_run_no_changes_with_errors_reports_them_and_exits_1(wired, tmp_path, capsys):
    api = FakeApi()
    wired(_all_errored(), api)
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path))
    assert exc.value.code == 1
    out = capsys.readouterr()
    text = out.out + out.err
    assert "GitLab unreachable: HTTP 503" in text and "eCH-0058" in text
    assert "analysis raised ValueError" in text
    assert "no ticket created" in text
    assert api.calls == []


def test_run_errored_source_without_message_is_still_reported(wired, tmp_path, capsys):
    a = _all_errored(spec_level=False)
    a.sources[0].errors = []
    wired(a, FakeApi())
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path))
    assert exc.value.code == 1
    assert "eCH-0058" in _out(capsys)


def test_run_spec_level_error_only_exits_1(wired, tmp_path, capsys):
    a = _analysis(changed=False)
    a.errors = [ReportError(scope="Batch", message="HTTP 500")]
    wired(a, FakeApi())
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path))
    assert exc.value.code == 1
    assert "HTTP 500" in _out(capsys)


def test_run_dry_run_writes_draft_no_http(wired, tmp_path, capsys):
    api = FakeApi()
    wired(_analysis(), api)
    run_create_ticket(_args(dry_run=True), _config(tmp_path))
    assert api.calls == []
    draft = tmp_path / "spec_cache" / "ticket-draft.md"
    text = draft.read_text(encoding="utf-8")
    assert "RMEG" in text and "Task" in text and "spec-monitor" in text and "RMEG-12" in text
    assert "# [DeltaSpec] [Batch Verarbeitung] [" in text and "h2. Änderungsantrag" in text
    assert "aufwand" not in text.lower()
    assert str(draft) in capsys.readouterr().out


def test_run_failure_exits_1(wired, tmp_path, capsys):
    api = FakeApi(_routes(create=FakeResp(500, text="boom")))
    wired(_analysis(), api)
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path))
    assert exc.value.code == 1


def test_run_fatal_exits_1(monkeypatch, tmp_path, capsys):
    def fatal(config, **kw):
        raise runner.SpecMonitorFatal("page not found")

    monkeypatch.setattr(runner, "run_spec_monitor", fatal)
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path))
    assert exc.value.code == 1
    assert "page not found" in capsys.readouterr().err


# --- PROJ Jira setup: payload, remote link, error translation ---------------------

PROJ = TicketConfig(project="PROJ", issue_type="Story", labels=["cr"], priority="Medium",
                    team_id="abc-123-team", parent="PROJ-123")


def _create(monkeypatch, tmp_path, cfg, routes=None, analysis=None):
    api = FakeApi(routes or _routes())
    monkeypatch.setattr(ticket, "request_with_retry", api)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    d = build_ticket(analysis or _analysis(), cfg, "de")
    url = create_ticket(_config(tmp_path, cfg), d)
    return api, url


def _fields(api):
    (_, _, kw), = api.posts("/rest/api/2/issue")
    return kw["json"]["fields"]


def _out(capsys):
    c = capsys.readouterr()
    return c.out + c.err


def test_proj_payload(monkeypatch, tmp_path):
    api, _ = _create(monkeypatch, tmp_path, PROJ)
    f = _fields(api)
    assert f["project"] == {"key": "PROJ"} and f["issuetype"] == {"name": "Story"}
    assert f["labels"] == ["cr", "spec-monitor"]
    assert f["priority"] == {"name": "Medium"}
    assert f["customfield_10001"] == "abc-123-team"
    assert f["parent"] == {"key": "PROJ-123"}
    for forbidden in ("customfield_10031", "customfield_11202", "fixVersions", "customfield_10091"):
        assert forbidden not in f


def test_optional_fields_absent_when_unset(monkeypatch, tmp_path):
    api, _ = _create(monkeypatch, tmp_path, TCFG)
    f = _fields(api)
    assert "priority" not in f and "parent" not in f and "customfield_10001" not in f


def test_custom_team_field(monkeypatch, tmp_path):
    cfg = TicketConfig(project="P", team_id="t1", team_field="customfield_99999")
    api, _ = _create(monkeypatch, tmp_path, cfg)
    f = _fields(api)
    assert f["customfield_99999"] == "t1" and "customfield_10001" not in f


def test_subtask_gets_no_team(monkeypatch, tmp_path):
    cfg = TicketConfig(project="PROJ", issue_type="Sub-task", team_id="t1", parent="PROJ-5")
    api, _ = _create(monkeypatch, tmp_path, cfg)
    f = _fields(api)
    assert "customfield_10001" not in f and f["parent"] == {"key": "PROJ-5"}


def test_extra_fields_added_but_cannot_override_core(monkeypatch, tmp_path):
    cfg = TicketConfig(project="PROJ", issue_type="Story", fields={
        "customfield_20000": {"value": "x"},
        "project": {"key": "HACK"}, "issuetype": {"name": "Bug"},
        "summary": "evil", "description": "evil",
    })
    api, _ = _create(monkeypatch, tmp_path, cfg)
    f = _fields(api)
    assert f["customfield_20000"] == {"value": "x"}
    assert f["project"] == {"key": "PROJ"} and f["issuetype"] == {"name": "Story"}
    assert f["summary"].startswith("[DeltaSpec] [Batch Verarbeitung] [") and f["description"] != "evil"


def test_forbidden_fields_dropped_with_warning(monkeypatch, tmp_path, capsys):
    cfg = TicketConfig(project="PROJ", fields={
        "customfield_11202": "x", "fixVersions": [{"name": "1"}], "customfield_10031": 5, "customfield_20000": 1})
    api, _ = _create(monkeypatch, tmp_path, cfg)
    f = _fields(api)
    for k in ("customfield_11202", "fixVersions", "customfield_10031"):
        assert k not in f
    assert f["customfield_20000"] == 1
    text = _out(capsys)
    for k in ("customfield_11202", "fixVersions", "customfield_10031"):
        assert k in text
    assert "WARNING" in text


def test_remote_link_posted(monkeypatch, tmp_path):
    api, url = _create(monkeypatch, tmp_path, PROJ)
    (_, u, kw), = api.posts("/remotelink")
    assert u == f"{BASE}/rest/api/2/issue/RMEG-99/remotelink"
    assert kw["json"] == {"object": {"url": f"{BASE}/wiki/spaces/X/pages/1",
                                     "title": "Spezifikation: Batch Verarbeitung"}}
    assert url.endswith("RMEG-99")


def test_remote_link_disabled(monkeypatch, tmp_path):
    cfg = TicketConfig(project="PROJ", remote_link=False)
    api, _ = _create(monkeypatch, tmp_path, cfg)
    assert not api.posts("/remotelink")


def test_remote_link_failure_keeps_ticket(monkeypatch, tmp_path, capsys):
    api, url = _create(monkeypatch, tmp_path, PROJ, routes=_routes(remote=FakeResp(403, text="forbidden")))
    assert url.endswith("RMEG-99")
    text = _out(capsys)
    assert "remote link" in text.lower() and "403" in text


def test_remote_link_exception_keeps_ticket(monkeypatch, tmp_path, capsys):
    from digest.spec_monitor.http import RequestFailed

    def handler(kw):
        raise RequestFailed("u", 1, "timeout")

    api, url = _create(monkeypatch, tmp_path, PROJ, routes=_routes(remote=handler))
    assert url.endswith("RMEG-99")
    assert "remote link" in _out(capsys).lower()


def test_no_explicit_timeout_passed(monkeypatch, tmp_path):
    api, _ = _create(monkeypatch, tmp_path, PROJ)
    assert api.calls and all("timeout" not in kw for _, _, kw in api.calls)


@pytest.mark.parametrize("body,key,hint", [
    ('{"errors":{"customfield_10001":"Cannot assign a non-existing team"}}', "spec_monitor.ticket.team_id", None),
    ('{"errors":{"customfield_10001":"Team id abc is not valid"}}', "spec_monitor.ticket.team_id", None),
    ('{"errors":{"parent":"Please select valid parent issue"}}', "spec_monitor.ticket.parent", "Feature"),
    ('{"errors":{"priority":"Priority name Urgent is not valid"}}', "spec_monitor.ticket.priority", None),
])
def test_rejected_field_log_is_translated(monkeypatch, tmp_path, capsys, body, key, hint):
    api = FakeApi(_routes(create=_seq(FakeResp(400, text=body), FakeResp(201, {"key": "RMEG-99"}))))
    monkeypatch.setattr(ticket, "request_with_retry", api)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    text = _out(capsys)
    assert key in text and "WARNING" in text
    assert list(json.loads(body)["errors"].values())[0] in text
    if hint:
        assert hint in text


def test_untranslated_error_unchanged(monkeypatch, tmp_path):
    api = FakeApi(_routes(create=FakeResp(400, text='{"errors":{"issuetype":"bad"}}')))
    monkeypatch.setattr(ticket, "request_with_retry", api)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    with pytest.raises(TicketError) as exc:
        create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert "spec_monitor.ticket." not in str(exc.value)


def test_dry_run_draft_lists_proj_details(wired, tmp_path):
    cfg = TicketConfig(project="PROJ", issue_type="Story", labels=["cr"], priority="Medium",
                       team_id="abc-123-team", parent="PROJ-123",
                       fields={"customfield_11202": "x", "customfield_10031": 3})
    api = FakeApi()
    wired(_analysis(), api)
    run_create_ticket(_args(dry_run=True), _config(tmp_path, cfg))
    assert api.calls == []
    text = (tmp_path / "spec_cache" / "ticket-draft.md").read_text(encoding="utf-8")
    assert "Priority: Medium" in text
    assert "Team (customfield_10001): abc-123-team" in text
    assert "Parent: PROJ-123" in text
    assert "Remote link: yes" in text and f"{BASE}/wiki/spaces/X/pages/1" in text
    assert "customfield_11202" in text and "customfield_10031" in text and "Dropped" in text


# --- field failures do not fail the run -------------------------------------------

def _seq(*resps):
    it = iter(resps)
    return lambda kw: next(it)


def _err(**errors):
    return FakeResp(400, text=json.dumps({"errorMessages": [], "errors": errors}))


def OK():
    return FakeResp(201, {"key": "RMEG-99"})


TEAM_MSG = "Cannot assign a non-existing team"


def _wire_create(monkeypatch, create):
    api = FakeApi(_routes(create=create))
    monkeypatch.setattr(ticket, "request_with_retry", api)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    return api


def _issue_payloads(api):
    return [kw["json"]["fields"] for _, _, kw in api.posts("/rest/api/2/issue")]


def test_team_rejected_created_without_team(monkeypatch, tmp_path, capsys, caplog):
    api = _wire_create(monkeypatch, _seq(_err(customfield_10001=TEAM_MSG), OK()))
    with caplog.at_level(logging.WARNING):
        res = ticket.create_ticket_detailed(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    first, second = _issue_payloads(api)
    assert "customfield_10001" in first and "customfield_10001" not in second
    assert second["priority"] == {"name": "Medium"} and second["parent"] == {"key": "PROJ-123"}
    assert res.url.endswith("RMEG-99")
    assert [d.key for d in res.dropped] == ["customfield_10001"]
    assert any(r.levelno == logging.WARNING and "customfield_10001" in r.getMessage() for r in caplog.records)
    err = capsys.readouterr().err
    assert "customfield_10001" in err and TEAM_MSG in err and "spec_monitor.ticket.team_id" in err
    assert "h3. Hinweis" not in first["description"]
    assert "h3. Hinweis" in second["description"] and TEAM_MSG in second["description"]
    assert second["description"].startswith(first["description"].rstrip())


def test_create_ticket_returns_url_string(monkeypatch, tmp_path):
    _wire_create(monkeypatch, _seq(_err(priority="bad"), OK()))
    assert create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de")).endswith("RMEG-99")


def test_run_team_rejected_does_not_exit_and_prints_summary(wired, tmp_path, capsys):
    api = FakeApi(_routes(create=_seq(_err(customfield_10001=TEAM_MSG, priority="Priority name X is not valid"), OK())))
    wired(_analysis(), api)
    run_create_ticket(_args(), _config(tmp_path, PROJ))
    out = capsys.readouterr().out
    assert f"Ticket created: {BASE}/browse/RMEG-99" in out
    line = [l for l in out.splitlines() if l.startswith("Created WITHOUT:")]
    assert len(line) == 1
    assert "team (" in line[0] and TEAM_MSG in line[0] and "priority (" in line[0]
    assert line[0].endswith("please set these manually")
    assert len(api.posts("/rest/api/2/issue")) == 2


def test_two_fields_rejected_in_one_response_dropped_in_one_retry(monkeypatch, tmp_path):
    api = _wire_create(monkeypatch, _seq(_err(customfield_10001=TEAM_MSG, priority="bad prio"), OK()))
    create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    first, second = _issue_payloads(api)
    assert len(_issue_payloads(api)) == 2
    assert "customfield_10001" not in second and "priority" not in second
    assert "parent" in second and "labels" in second
    assert "bad prio" in second["description"] and TEAM_MSG in second["description"]


def test_rejection_in_second_round_triggers_another_retry(monkeypatch, tmp_path):
    api = _wire_create(monkeypatch, _seq(
        _err(customfield_10001=TEAM_MSG), _err(parent="Please select valid parent issue"), OK()))
    create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    p1, p2, p3 = _issue_payloads(api)
    assert "customfield_10001" not in p2 and "parent" in p2
    assert "customfield_10001" not in p3 and "parent" not in p3 and "priority" in p3
    assert "parent" in p3["description"].split("h3. Hinweis")[1] and TEAM_MSG in p3["description"]


def test_extra_field_and_labels_are_droppable(monkeypatch, tmp_path):
    cfg = TicketConfig(project="PROJ", fields={"customfield_20000": {"value": "x"}})
    api = _wire_create(monkeypatch, _seq(_err(customfield_20000="Field cannot be set", labels="no labels"), OK()))
    create_ticket(_config(tmp_path, cfg), build_ticket(_analysis(), cfg, "de"))
    _, second = _issue_payloads(api)
    assert "customfield_20000" not in second and "labels" not in second


@pytest.mark.parametrize("core", ["summary", "description", "project", "issuetype"])
def test_core_field_error_fails(monkeypatch, tmp_path, core):
    api = _wire_create(monkeypatch, _seq(_err(**{core: "bad"}), OK()))
    with pytest.raises(TicketError) as exc:
        create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert "400" in str(exc.value) and len(_issue_payloads(api)) == 1


def test_core_plus_optional_error_fails_without_retry(monkeypatch, tmp_path):
    api = _wire_create(monkeypatch, _seq(_err(summary="too long", priority="bad"), OK()))
    with pytest.raises(TicketError):
        create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert len(_issue_payloads(api)) == 1


def test_run_core_error_exits_1(wired, tmp_path):
    api = FakeApi(_routes(create=_err(summary="bad")))
    wired(_analysis(), api)
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path, PROJ))
    assert exc.value.code == 1


def test_400_without_field_names_fails(monkeypatch, tmp_path):
    body = json.dumps({"errorMessages": ["Something odd"], "errors": {}})
    api = _wire_create(monkeypatch, _seq(FakeResp(400, text=body), OK()))
    with pytest.raises(TicketError) as exc:
        create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert "Something odd" in str(exc.value) and len(_issue_payloads(api)) == 1


def test_400_unparseable_body_fails(monkeypatch, tmp_path):
    api = _wire_create(monkeypatch, _seq(FakeResp(400, text="<html>nope</html>"), OK()))
    with pytest.raises(TicketError):
        create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert len(_issue_payloads(api)) == 1


def test_non_400_error_fails_as_before(monkeypatch, tmp_path):
    api = _wire_create(monkeypatch, _seq(FakeResp(500, text="boom"), OK()))
    with pytest.raises(TicketError) as exc:
        create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert "500" in str(exc.value) and len(_issue_payloads(api)) == 1


def test_error_naming_already_dropped_field_fails(monkeypatch, tmp_path):
    api = _wire_create(monkeypatch, _seq(_err(priority="bad"), _err(priority="bad")))
    with pytest.raises(TicketError):
        create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert len(_issue_payloads(api)) == 2


def test_retry_bound_is_optional_fields_plus_one(monkeypatch, tmp_path):
    # PROJ optional fields: labels, priority, team, parent -> at most 5 attempts
    api = _wire_create(monkeypatch, _seq(
        _err(priority="a"), _err(customfield_10001="b"), _err(parent="c"), _err(labels="d"),
        _err(summary="still bad"), OK(), OK()))
    with pytest.raises(TicketError):
        create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert len(_issue_payloads(api)) == 5


def test_all_optional_fields_dropped_then_success(monkeypatch, tmp_path):
    api = _wire_create(monkeypatch, _seq(
        _err(priority="a"), _err(customfield_10001="b"), _err(parent="c"), _err(labels="d"), OK()))
    create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    last = _issue_payloads(api)[-1]
    assert set(last) == {"project", "issuetype", "summary", "description"}


def test_success_path_has_no_note_and_no_without_line(wired, tmp_path, capsys):
    api = FakeApi(_routes())
    wired(_analysis(), api)
    run_create_ticket(_args(), _config(tmp_path, PROJ))
    out = capsys.readouterr()
    assert "Ticket created:" in out.out and "WITHOUT" not in out.out + out.err
    assert "Hinweis" not in _fields(api)["description"]


def test_remote_link_still_best_effort_after_drop(monkeypatch, tmp_path, capsys):
    api = FakeApi(_routes(create=_seq(_err(priority="bad"), OK()), remote=FakeResp(403, text="forbidden")))
    monkeypatch.setattr(ticket, "request_with_retry", api)
    monkeypatch.setattr(ticket, "get_auth_header", lambda a: "Basic abc")
    url = create_ticket(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert url.endswith("RMEG-99") and "403" in _out(capsys)


def _unchanged_clean():
    a = _analysis(changed=False)
    a.errors = []
    a.completeness_warnings = []
    return a


def test_run_no_changes_with_not_monitored_source_exits_1(wired, tmp_path, capsys):
    a = _unchanged_clean()
    a.sources[0].classification = Classification.NOT_MONITORED
    a.sources[0].warnings = ["login required"]
    api = FakeApi()
    wired(a, api)
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path))
    assert exc.value.code == 1
    text = _out(capsys)
    assert "eCH-0058" in text and "login required" in text and "no source change was confirmed" in text
    assert api.calls == []


def test_run_no_changes_without_sources_exits_1(wired, tmp_path, capsys):
    a = _unchanged_clean()
    a.sources = []
    wired(a, FakeApi())
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path))
    assert exc.value.code == 1
    assert "no sources found" in _out(capsys)


def test_run_no_changes_with_completeness_warning_exits_1(wired, tmp_path, capsys):
    a = _unchanged_clean()
    a.completeness_warnings = ["Lücke laut Spezifikation: Quelle fehlt"]
    wired(a, FakeApi())
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path))
    assert exc.value.code == 1
    assert "Quelle fehlt" in _out(capsys)


def test_run_failed_issue_link_exits_1_but_ticket_exists(wired, tmp_path, capsys):
    api = FakeApi(_routes(link=FakeResp(404, text="no such issue")))
    wired(_analysis(), api)
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path, PROJ))
    assert exc.value.code == 1
    cap = capsys.readouterr()
    assert "Ticket created" in cap.out
    assert "link(s) are missing" in cap.err and "issue link to RMEG-12" in cap.err
    assert len(api.posts("/rest/api/2/issue")) == 1


def test_run_failed_remote_link_exits_1(wired, tmp_path, capsys):
    api = FakeApi(_routes(remote=FakeResp(403, text="forbidden")))
    wired(_analysis(), api)
    with pytest.raises(SystemExit) as exc:
        run_create_ticket(_args(), _config(tmp_path, PROJ))
    assert exc.value.code == 1
    assert "remote link to the spec page" in capsys.readouterr().err


def test_create_ticket_detailed_collects_link_failures(monkeypatch, tmp_path):
    _create(monkeypatch, tmp_path, PROJ, routes=_routes(link=FakeResp(500, text="x"), remote=FakeResp(403, text="y")))
    result = ticket.create_ticket_detailed(_config(tmp_path, PROJ), build_ticket(_analysis(), PROJ, "de"))
    assert len(result.link_failures) == 3
