from datetime import datetime
from html import escape
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from digest.config import LLMConfig, RetryConfig, SpecMonitorConfig
from digest.spec_monitor import cli, refcheck, refcheck_report
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.discovery import Discovery
from digest.spec_monitor.models import MissingReference, RefCheckReport, RefCheckResult, ReportError, SpecInfo
from digest.spec_monitor.parser import body_link_refs, strip_sources_tables


def link(title):
    return f'<ac:link><ri:page ri:content-title="{escape(title)}" /></ac:link>'


def sources_table(*rows):
    head = "<tr><th>Quelle</th><th>Gelesene Version</th><th>Verwendet für</th></tr>"
    body = "".join(f"<tr><td>{s}</td><td>{v}</td><td>{u}</td></tr>" for s, v, u in rows)
    return f"<table><tbody>{head}{body}</tbody></table>"


def spec_html(*rows, text=""):
    return f"<h1>Spec</h1><h2>1 Einleitung</h2>{text}<h2>1.5 Quellen</h2>{sources_table(*rows)}"


def make_spec(body, title="API Customer", **kw):
    return SpecInfo(page_id="42", title=title, url="https://x/42", status="Draft", body_storage=body, **kw)


def config(tmp_path):
    return SimpleNamespace(
        spec_monitor=SpecMonitorConfig(parent_page_id="1", selector_type="label", selector_value="spec",
                                       retry=RetryConfig(1, 0), workers=1, link_depth=0),
        atlassian=MagicMock(), llm=LLMConfig(provider="openai", api_key="k", models=["m"], llm_timeout=30),
        language="de", data_dir=tmp_path, gitlab=None,
    )


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(refcheck, "_ask", lambda *a, **k: [])


def check(tmp_path, body, **kw):
    cfg = config(tmp_path)
    return refcheck.check_spec(make_spec(body, **kw), cfg, SpecCache(tmp_path / "c"), cfg.llm)


class TestParserHelpers:
    def test_strip_sources_tables_removes_only_the_table(self):
        html = spec_html((link("Fach A"), "v1", "Regeln"), text="<p>Siehe ADR-0028.</p>")
        stripped = strip_sources_tables(html)
        assert "Fach A" not in stripped and "ADR-0028" in stripped and "Quellen" in stripped

    def test_body_link_refs_skips_ancestors_and_dedupes(self):
        html = f"<p>{link('Parent')} {link('Konzept')} {link('Konzept')} <a href=\"https://example.org/doc\">Doc</a></p>"
        refs = body_link_refs(html, ancestor_titles=["parent"])
        assert [r.name for r in refs] == ["Konzept", "Doc"]


class TestPatternFindings:
    def test_unlisted_adr_and_ech_are_reported_with_mention(self, tmp_path, no_llm):
        body = spec_html((link("Fach A"), "v1", "x"),
                         text="<p>Die Auth folgt ADR-0028.</p><p>Das Format richtet sich nach eCH-0200.</p>")
        result = check(tmp_path, body)
        found = {m.reference: m for m in result.missing}
        assert set(found) == {"ADR-0028", "eCH-0200"}
        assert found["ADR-0028"].mention == "Die Auth folgt ADR-0028."
        assert found["ADR-0028"].section == "1 Einleitung"
        assert result.listed_count == 1

    def test_listed_adr_in_other_spelling_is_not_reported(self, tmp_path, no_llm):
        body = spec_html(("ADR 28 Authentifizierung", "v1", "x"), ("eCH-0200 V2.0", "", "x"),
                         text="<p>ADR-0028 und eCH-0200.</p>")
        assert check(tmp_path, body).missing == []

    def test_unlisted_page_link_is_reported_and_listed_one_is_not(self, tmp_path, no_llm):
        body = spec_html((link("Fach A"), "v1", "x"), text=f"<p>Vgl. {link('Fach A')} und {link('Fach B')}.</p>")
        result = check(tmp_path, body)
        assert [(m.kind, m.reference) for m in result.missing] == [("confluence", "Fach B")]

    def test_own_ancestor_own_page_and_jira_links_are_ignored(self, tmp_path, no_llm):
        body = spec_html((link("Fach A"), "v1", "x"), text=(
            f"<p>{link('Parent')} {link('API Customer')} "
            '<a href="https://jira.example/browse/ABC-12">ABC-12</a></p>'))
        assert check(tmp_path, body, ancestor_titles=["Parent"]).missing == []

    def test_unlisted_url_is_reported(self, tmp_path, no_llm):
        body = spec_html((link("Fach A"), "v1", "x"), text='<p><a href="https://api.example/spec">API</a></p>')
        (m,) = check(tmp_path, body).missing
        assert m.kind == "url" and m.url == "https://api.example/spec"

    def test_adr_in_listed_repo_path_counts_as_listed(self, tmp_path, no_llm):
        body = spec_html(("docs/adr/0028-auth.md", "", "x"), text="<p>Siehe ADR-0028.</p>")
        assert check(tmp_path, body).missing == []

    def test_adr_from_text_and_from_link_are_one_finding(self, tmp_path, no_llm):
        body = spec_html((link("Fach A"), "v1", "x"),
                         text='<p>ADR-0028: <a href="https://example.org/adr-0028">ADR 28</a></p>')
        refs = [m.reference for m in check(tmp_path, body).missing]
        assert refs.count("ADR-0028") == 1 and len(refs) == 1

    def test_no_sources_table_warns_and_reports_everything(self, tmp_path, no_llm):
        result = check(tmp_path, "<h1>Spec</h1><p>Siehe ADR-0028.</p>")
        assert [m.reference for m in result.missing] == ["ADR-0028"]
        assert any("Keine Quellentabelle" in w for w in result.warnings)

    def test_excluded_row_counts_as_listed(self, tmp_path, no_llm):
        body = spec_html((f"Notizen {link('Fach B')}", "", "x"), text=f"<p>{link('Fach B')}</p>")
        assert check(tmp_path, body).missing == []


class TestReadableLinks:
    HOST = "https://acme.atlassian.net"
    TINY = HOST + "/wiki/x/AgDuDw"

    def run(self, tmp_path, client, rows=()):
        cfg = config(tmp_path)
        cfg.atlassian = SimpleNamespace(url=self.HOST)
        body = spec_html(*rows, text=f'<p>Links <a href="{self.TINY}">{self.TINY}</a></p>')
        return refcheck.check_spec(make_spec(body), cfg, SpecCache(tmp_path / "c"), cfg.llm, client)

    def test_short_link_is_named_by_page_title_and_mention_has_no_markup(self, tmp_path, no_llm):
        client = MagicMock()
        client.get_page.return_value = {"title": "API Konzept"}
        (m,) = self.run(tmp_path, client).missing
        client.get_page.assert_called_once_with("267255810", with_body=False)
        assert (m.reference, m.kind, m.url) == ("API Konzept", "confluence", self.TINY)
        assert "[->" not in m.mention and self.TINY not in m.mention and "API Konzept" in m.mention

    def test_short_link_to_a_listed_page_is_not_reported(self, tmp_path, no_llm):
        client = MagicMock()
        client.get_page.return_value = {"title": "API Konzept"}
        assert self.run(tmp_path, client, rows=(("API Konzept", "v1", "x"),)).missing == []

    def test_unresolvable_short_link_stays_a_link_and_warns(self, tmp_path, no_llm):
        client = MagicMock()
        client.get_page.side_effect = LookupError("page 267255810 not found (HTTP 404)")
        result = self.run(tmp_path, client)
        (m,) = result.missing
        assert m.kind == "url" and "[->" not in m.mention
        assert any(self.TINY in w for w in result.warnings)

    def test_page_named_without_link_is_looked_up_by_title_and_linked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(refcheck, "_ask", lambda *a, **k: [MissingReference(
            reference="Seite ServiceRequestApp", kind="confluence", origin="llm", why="Eigene Confluence-Seite")])
        client = MagicMock()
        client.find_page_id.side_effect = lambda name: "77" if name == "ServiceRequestApp" else None
        client.page_url.return_value = self.HOST + "/wiki/pages/viewpage.action?pageId=77"
        client.get_page.return_value = {"title": "ServiceRequestApp"}
        cfg = config(tmp_path)
        cfg.atlassian = SimpleNamespace(url=self.HOST)
        result = refcheck.check_spec(make_spec(spec_html()), cfg, SpecCache(tmp_path / "c"), cfg.llm, client)
        (m,) = result.missing
        assert (m.reference, m.url) == ("ServiceRequestApp", client.page_url.return_value)

    def test_readable_keeps_label_or_target(self):
        assert refcheck._readable("Text[->Seite#a] ok", {}) == "Text ok"
        assert refcheck._readable("siehe [->https://x/y] dort", {}) == "siehe https://x/y dort"


class TestLlmPass:
    def llm_says(self, monkeypatch, *refs):
        monkeypatch.setattr(refcheck, "_ask", lambda *a, **k: list(refs))

    def ref(self, name, **kw):
        return MissingReference(reference=name, kind="other", mention=f"... {name} ...", origin="llm", **kw)

    def test_model_findings_are_added_with_origin(self, tmp_path, monkeypatch):
        self.llm_says(monkeypatch, self.ref("Datenschutzkonzept"))
        result = check(tmp_path, spec_html((link("Fach A"), "v1", "x"), text="<p>Gemäss Datenschutzkonzept.</p>"))
        (m,) = result.missing
        assert m.origin == "llm" and m.reference == "Datenschutzkonzept"

    def test_model_findings_already_listed_or_found_by_pattern_are_dropped(self, tmp_path, monkeypatch):
        self.llm_says(monkeypatch, self.ref("Fach A"), self.ref("Fach A v2"), self.ref("ADR 28"),
                      self.ref("API-Konzept"))
        body = spec_html((link("Fach A"), "v1", "x"), text="<p>ADR-0028 und API-Konzept.</p>")
        result = check(tmp_path, body)
        # listed ("Fach A", "Fach A v2") and pattern-found ("ADR 28") are dropped; the unlisted one stays
        assert sorted(m.reference for m in result.missing) == ["ADR-0028", "API-Konzept"]

    def test_llm_failure_keeps_pattern_findings_and_reports_error(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise AnalysisError("timeout")
        monkeypatch.setattr(refcheck, "_ask", boom)
        result = check(tmp_path, spec_html((link("Fach A"), "v1", "x"), text="<p>ADR-0028</p>"))
        assert result.llm_incomplete
        assert [m.reference for m in result.missing] == ["ADR-0028"]
        assert "timeout" in result.errors[0].message

    def test_prompt_lists_sources_and_excludes_the_table_text(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(refcheck, "_ask", lambda prompt, llm, cache: seen.append(prompt) or [])
        check(tmp_path, spec_html((link("Fach A"), "v1", "x"), text="<p>Siehe ADR-0028.</p>"))
        prompt = seen[0]
        assert "- Fach A" in prompt and "ADR-0028" in prompt
        assert "Gelesene Version" not in prompt


class TestRun:
    @pytest.fixture
    def discover(self, mocker):
        return mocker.patch.object(refcheck, "discover_specs")

    def run(self, tmp_path, specs, **kw):
        cfg = config(tmp_path)
        report = refcheck.run_reference_check(cfg, client=MagicMock(), progress=lambda _m: None,
                                              cache_dir=tmp_path / "c", **kw)
        return cfg, report

    def test_checks_every_spec_and_sets_all_states(self, tmp_path, discover, no_llm):
        discover.return_value = Discovery(
            specs=[make_spec(spec_html((link("A"), "v1", "x"), text="<p>ADR-1</p>"), title="S1"),
                   make_spec(spec_html((link("A"), "v1", "x")), title="S2")],
            excluded=[("9", "Other")])
        cfg, report = self.run(tmp_path, None)
        assert cfg.spec_monitor.analyse_all_states is True
        assert [(r.spec.title, len(r.missing)) for r in report.results] == [("S1", 1), ("S2", 0)]
        assert report.excluded_pages == [("9", "Other")]

    def test_one_failing_spec_does_not_fail_the_run(self, tmp_path, discover, no_llm, monkeypatch):
        discover.return_value = Discovery(specs=[make_spec("<p/>", title="S1"), make_spec("<p/>", title="S2")])
        real = refcheck.check_spec

        def flaky(spec, *a, **k):
            if spec.title == "S1":
                raise RuntimeError("kaputt")
            return real(spec, *a, **k)
        monkeypatch.setattr(refcheck, "check_spec", flaky)
        _, report = self.run(tmp_path, None)
        assert "kaputt" in report.results[0].errors[0].message and report.results[1].errors == []

    def test_unreadable_status_problem_is_not_shown(self, tmp_path, discover, no_llm):
        discover.return_value = Discovery(specs=[], problems=[
            ReportError(scope="S", message="status could not be read from the page properties"),
            ReportError(scope="P", message="no spec found")])
        _, report = self.run(tmp_path, None)
        assert [e.scope for e in report.errors] == ["P"]

    def test_discovery_failure_is_fatal(self, tmp_path, discover):
        discover.side_effect = ValueError("nope")
        with pytest.raises(refcheck.SpecMonitorFatal, match="nope"):
            self.run(tmp_path, None)


def sample_report(**kw):
    spec = make_spec("<p/>")
    result = RefCheckResult(spec=spec, listed_count=2, missing=[
        MissingReference(reference="ADR-0028", kind="adr", mention="Siehe ADR-0028 <b>", section="1 Einleitung"),
        MissingReference(reference="Datenschutzkonzept", kind="other", mention="gemäss DSK", origin="llm",
                         why="Konzept", url="https://x/dsk")])
    return RefCheckReport(generated_at=datetime(2026, 11, 11, 9, 0).astimezone(), results=[result],
                          command_line="python digest/main.py --spec-monitor --check-references", **kw)


class TestReport:
    def test_mail_contains_findings_escaped(self):
        html = refcheck_report.render_mail_html(sample_report())
        assert "ADR-0028" in html and "Datenschutzkonzept" in html
        assert "Siehe ADR-0028 &lt;b&gt;" in html and "<b>" not in html.split("<body>")[1]

    def test_storage_page_contains_table(self):
        storage = refcheck_report.render_confluence_storage(sample_report())
        assert "<table" in storage and "ADR-0028" in storage and "Datenschutzkonzept" in storage

    def test_subject_counts_specs_and_problems(self):
        report = sample_report(errors=[ReportError(scope="x", message="y")])
        subject = refcheck_report.build_subject("[Digest]", report)
        assert "1 Spezifikationen" in subject and "1 Problem" in subject

    def test_spec_without_findings_is_listed_but_has_no_block(self):
        report = sample_report()
        report.results.append(RefCheckResult(spec=make_spec("<p/>", title="Clean Spec")))
        html = refcheck_report.render_mail_html(report)
        assert "Clean Spec" in html and 'id="spec-42"' in html


class TestCli:
    def cfg(self, tmp_path):
        return SimpleNamespace(
            spec_monitor=SpecMonitorConfig(parent_page_id="1", selector_type="label", selector_value="s",
                                           retry=RetryConfig(1, 0), recipient="me@example.com"),
            mgmt_summary=SimpleNamespace(recipient=None),
            email=SimpleNamespace(recipient=None, subject_prefix="[Digest]"),
            smtp=SimpleNamespace(use_oauth2=False), m365=SimpleNamespace(tenant_id="t", client_id=None),
            atlassian=MagicMock(), data_dir=tmp_path,
        )

    def args(self, **kw):
        base = dict(spec=None, publish_page=False, dry_run=False, create_ticket=None, update_spec=None,
                    check_references=True)
        base.update(kw)
        return SimpleNamespace(**base)

    @pytest.fixture
    def stubs(self, mocker):
        report = sample_report()
        mocker.patch.object(cli, "run_reference_check", return_value=report)
        mocker.patch.object(cli, "send_html_via_smtp", return_value=True)
        return report

    def test_dry_run_writes_preview_and_sends_nothing(self, tmp_path, stubs):
        cli.run_ref_check_cli(self.args(dry_run=True), self.cfg(tmp_path), tmp_path / "t", "cmd")
        assert "ADR-0028" in (tmp_path / "spec_cache" / "ref-check-preview.html").read_text(encoding="utf-8")
        cli.send_html_via_smtp.assert_not_called()

    def test_sends_mail_and_findings_alone_do_not_fail_the_run(self, tmp_path, stubs):
        cli.run_ref_check_cli(self.args(), self.cfg(tmp_path), tmp_path / "t", "cmd")
        assert cli.send_html_via_smtp.call_args[0][3] == "me@example.com"

    def test_errors_exit_with_code_2_after_sending(self, tmp_path, stubs):
        stubs.errors.append(ReportError(scope="S", message="unreadable"))
        with pytest.raises(SystemExit) as e:
            cli.run_ref_check_cli(self.args(), self.cfg(tmp_path), tmp_path / "t", "cmd")
        assert e.value.code == 2
        cli.send_html_via_smtp.assert_called_once()

    def test_dispatch_routes_to_ref_check(self, tmp_path, mocker):
        run = mocker.patch.object(cli, "run_ref_check_cli")
        run_normal = mocker.patch.object(cli, "run_cli")
        cli._dispatch(self.args(), self.cfg(tmp_path), tmp_path / "t", "cmd")
        run.assert_called_once()
        run_normal.assert_not_called()

    def test_standard_spec_monitor_does_not_run_the_check(self, tmp_path, mocker):
        run = mocker.patch.object(cli, "run_ref_check_cli")
        run_normal = mocker.patch.object(cli, "run_cli")
        cli._dispatch(self.args(check_references=False), self.cfg(tmp_path), tmp_path / "t", "cmd")
        run.assert_not_called()
        run_normal.assert_called_once()
