import json
import re
from html import escape
from types import SimpleNamespace

import pytest

from digest.config import LLMConfig, RetryConfig, SpecMonitorConfig, _load_spec_monitor
from digest.spec_monitor import refcheck, refcheck_links, refcheck_report
from digest.spec_monitor.analysis import AnalysisError
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import RefCheckReport, RefCheckResult, RejectedLink, SpecInfo, SuggestedSource

HOST = "https://acme.atlassian.net"
LONG = "Fachliche Regeln und Schnittstellen dieser Seite. " * 6


def link(title):
    return f'<ac:link><ri:page ri:content-title="{escape(title)}" /></ac:link>'


def sources_table(*titles):
    head = "<tr><th>Quelle</th><th>Gelesene Version</th><th>Verwendet für</th></tr>"
    body = "".join(f"<tr><td>{link(t)}</td><td>v1</td><td>Regeln</td></tr>" for t in titles)
    return f"<table><tbody>{head}{body}</tbody></table>"


def spec_body(*sources, text="<p>Die Spezifikation beschreibt die Kundenverwaltung.</p>"):
    return f"<h1>Spec</h1><h2>1 Einleitung</h2>{text}<h2>1.5 Quellen</h2>{sources_table(*sources)}"


def page_body(*links, text=LONG):
    return f"<p>{text}</p>" + "".join(f"<p>Siehe {link(t)} dazu.</p>" for t in links)


class FakeClient:
    """Pages by title; ids are assigned in insertion order starting at 100."""

    def __init__(self, pages):
        self.pages = {}
        self.titles = {}
        for i, (title, body) in enumerate(pages.items()):
            self.pages[str(100 + i)] = (title, body)
            self.titles[title] = str(100 + i)
        self.fetched = []
        self.fail = {}
        self.lookup_fail = {}

    def page_url(self, pid, version=None):
        return f"{HOST}/wiki/pages/viewpage.action?pageId={pid}"

    def find_page_id(self, title, space_key=None):
        if title in self.lookup_fail:
            raise self.lookup_fail[title]
        return self.titles.get(title)

    def get_page(self, pid, with_body=True):
        self.fetched.append(pid)
        if pid in self.fail:
            raise self.fail[pid]
        if pid not in self.pages:
            raise LookupError(f"page {pid} not found (HTTP 404)")
        title, body = self.pages[pid]
        data = {"id": pid, "title": title, "version": {"number": 1}}
        if body is not None:
            data["body"] = {"storage": {"value": body}}
        return data


def config(tmp_path, depth=1, max_pages=50):
    return SimpleNamespace(
        spec_monitor=SpecMonitorConfig(parent_page_id="1", selector_type="label", selector_value="spec",
                                       retry=RetryConfig(1, 0), workers=1, link_depth=depth,
                                       link_max_pages=max_pages),
        atlassian=SimpleNamespace(url=HOST), llm=LLMConfig(provider="openai", api_key="k", models=["m"], llm_timeout=30),
        language="de", data_dir=tmp_path, gitlab=None,
    )


def make_spec(body, **kw):
    return SpecInfo(page_id="42", title="API Customer", url="https://x/42", status="Draft", body_storage=body, **kw)


@pytest.fixture
def judge(monkeypatch):
    """Replaces the LLM calls: text findings none, link assessment by a title -> verdict map."""
    monkeypatch.setattr(refcheck, "_ask", lambda *a, **k: [])
    seen = SimpleNamespace(prompts=[], verdicts={}, default=(False, "low", "nicht relevant"))

    def assess(prompt, llm, cache, ids):
        seen.prompts.append(prompt)
        out = {}
        for pid in ids:
            title = re.search(rf"\[{pid}\] (.*)", prompt).group(1).strip()
            out[pid] = seen.verdicts.get(title, seen.default)
        return out
    monkeypatch.setattr(refcheck_links, "_assess", assess)
    return seen


def run(tmp_path, client, body, depth=1, max_pages=50, **kw):
    cfg = config(tmp_path, depth, max_pages)
    return refcheck.check_spec(make_spec(body, **kw), cfg, SpecCache(tmp_path / "c"), cfg.llm, client)


class TestSuggestions:
    def test_relevant_page_linked_from_a_listed_source_is_suggested(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("Fach B"), "Fach B": page_body()})
        judge.verdicts = {"Fach B": (True, "high", "Regeln zur Adressprüfung")}
        result = run(tmp_path, client, spec_body("Fach A"))
        (s,) = result.suggested
        assert (s.title, s.depth, s.confidence, s.reason) == ("Fach B", 1, "high", "Regeln zur Adressprüfung")
        assert s.linked_from == ["Fach A"] and s.page_id == "101"
        assert s.url == client.page_url("101")
        assert "Fach B" in s.mention and "[->" not in s.mention
        assert result.link_checked == 1 and result.link_depth == 1

    def test_page_judged_not_relevant_is_rejected_with_reason(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("Protokoll"), "Protokoll": page_body()})
        judge.verdicts = {"Protokoll": (False, "high", "Sitzungsprotokoll")}
        result = run(tmp_path, client, spec_body("Fach A"))
        assert result.suggested == []
        assert [(r.title, r.reason, r.code) for r in result.link_rejected] == [("Protokoll", "Sitzungsprotokoll", "llm")]

    def test_listed_spec_ancestor_and_text_found_pages_are_never_candidates(self, tmp_path, judge):
        client = FakeClient({
            "Fach A": page_body("Fach C", "API Customer", "Parent", "Fach B"),
            "Fach C": page_body(), "API Customer": page_body(), "Parent": page_body(), "Fach B": page_body(),
        })
        judge.default = (True, "high", "x")
        body = spec_body("Fach A", "Fach C", text=f"<p>Basiert auf {link('Fach B')}.</p>")
        result = run(tmp_path, client, body, ancestor_titles=["Parent"], ancestor_ids=["103"])
        assert result.suggested == []
        assert result.link_checked == 0
        assert [m.reference for m in result.missing] == ["Fach B"]   # the text finding stays the stronger one

    def test_page_without_text_is_rejected_without_asking_the_model(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("Übersicht"), "Übersicht": "<p>Index</p>"})
        judge.default = (True, "high", "x")
        result = run(tmp_path, client, spec_body("Fach A"))
        assert result.suggested == []
        assert [(r.title, r.code) for r in result.link_rejected] == [("Übersicht", "too_short")]
        assert judge.prompts == []

    def test_candidate_linked_from_two_sources_is_one_suggestion_with_both(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("Fach X"), "Fach B": page_body("Fach X"), "Fach X": page_body()})
        judge.verdicts = {"Fach X": (True, "medium", "Regeln")}
        (s,) = run(tmp_path, client, spec_body("Fach A", "Fach B")).suggested
        assert sorted(s.linked_from) == ["Fach A", "Fach B"]

    def test_prompt_carries_spec_text_sources_and_candidate_excerpt_untrusted(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("Fach B"), "Fach B": page_body(text="Eindeutiger Seitentext B. " * 6)})
        run(tmp_path, client, spec_body("Fach A", text="<p>Kundenverwaltung mit Adressprüfung.</p>"))
        prompt = judge.prompts[0]
        assert "Kundenverwaltung mit Adressprüfung" in prompt and "Fach A" in prompt and "Regeln" in prompt
        assert "Eindeutiger Seitentext B" in prompt and "UNTRUSTED DATA" in prompt
        assert "Gelesene Version" not in prompt


class TestDepth:
    def pages(self):
        return FakeClient({"Fach A": page_body("Fach B"), "Fach B": page_body("Fach C"), "Fach C": page_body()})

    def test_depth_one_does_not_follow_further(self, tmp_path, judge):
        client = self.pages()
        judge.default = (True, "high", "x")
        result = run(tmp_path, client, spec_body("Fach A"), depth=1)
        assert [s.title for s in result.suggested] == ["Fach B"]
        assert "102" not in client.fetched

    def test_depth_two_follows_the_links_of_linked_pages_and_names_the_chain(self, tmp_path, judge):
        judge.default = (True, "high", "x")
        result = run(tmp_path, self.pages(), spec_body("Fach A"), depth=2)
        assert [(s.title, s.depth, s.linked_from) for s in result.suggested] == [
            ("Fach B", 1, ["Fach A"]), ("Fach C", 2, ["Fach A → Fach B"])]

    def test_depth_two_also_follows_through_rejected_pages(self, tmp_path, judge):
        judge.verdicts = {"Fach C": (True, "high", "x")}
        result = run(tmp_path, self.pages(), spec_body("Fach A"), depth=2)
        assert [s.title for s in result.suggested] == ["Fach C"]
        assert [r.title for r in result.link_rejected] == ["Fach B"]

    def test_depth_zero_does_nothing(self, tmp_path, judge):
        client = self.pages()
        result = run(tmp_path, client, spec_body("Fach A"), depth=0)
        assert client.fetched == [] and result.suggested == [] and result.link_depth == 0

    def test_cycles_do_not_loop(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("Fach B"), "Fach B": page_body("Fach A", "Fach B")})
        judge.default = (True, "high", "x")
        result = run(tmp_path, client, spec_body("Fach A"), depth=2)
        assert [s.title for s in result.suggested] == ["Fach B"]


class TestLimitsAndFailures:
    def test_page_budget_stops_the_crawl_and_says_so(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("B1", "B2", "B3"), "B1": page_body(), "B2": page_body(),
                             "B3": page_body()})
        judge.default = (True, "high", "x")
        result = run(tmp_path, client, spec_body("Fach A"), max_pages=3)   # the source itself + two candidates
        assert len(result.suggested) == 2
        assert any("nach 3 gelesenen Seiten" in w and "1 verlinkte" in w for w in result.warnings)

    def test_unreadable_candidate_is_a_warning_not_a_crash(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("B1", "B2"), "B1": page_body(), "B2": page_body()})
        client.fail["101"] = LookupError("page 101 not found (HTTP 404)")
        judge.default = (True, "high", "x")
        result = run(tmp_path, client, spec_body("Fach A"))
        assert [s.title for s in result.suggested] == ["B2"]
        assert any("B1" in w or "101" in w for w in result.warnings)
        assert result.errors == []

    def test_request_failure_is_an_error_with_impact(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("B1"), "B1": page_body()})
        client.fail["101"] = RequestFailed("u", 3, "HTTP 500")
        result = run(tmp_path, client, spec_body("Fach A"))
        assert result.link_incomplete
        assert result.errors and "linked pages" in result.errors[0].impact

    def test_unreadable_source_is_a_warning(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body()})
        client.fail["100"] = LookupError("page 100 not found (HTTP 404)")
        result = run(tmp_path, client, spec_body("Fach A"))
        assert any("Fach A" in w for w in result.warnings)

    def test_unknown_source_page_is_a_warning(self, tmp_path, judge):
        result = run(tmp_path, FakeClient({}), spec_body("Gibt es nicht"))
        assert any("Gibt es nicht" in w for w in result.warnings)

    def test_ambiguous_link_title_is_a_warning(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body("Doppelt")})
        client.lookup_fail["Doppelt"] = LookupError("title is ambiguous without a space key (A:1, B:2)")
        result = run(tmp_path, client, spec_body("Fach A"))
        assert any("Doppelt" in w for w in result.warnings)

    def test_model_failure_is_an_error_and_yields_no_suggestions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(refcheck, "_ask", lambda *a, **k: [])

        def boom(*a, **k):
            raise AnalysisError("timeout")
        monkeypatch.setattr(refcheck_links, "_assess", boom)
        client = FakeClient({"Fach A": page_body("Fach B"), "Fach B": page_body()})
        result = run(tmp_path, client, spec_body("Fach A"))
        assert result.suggested == [] and result.link_incomplete
        assert "timeout" in result.errors[0].message and "linked pages" in result.errors[0].impact

    def test_without_a_client_the_step_is_skipped(self, tmp_path, judge):
        assert run(tmp_path, None, spec_body("Fach A")).suggested == []

    def test_external_and_foreign_host_links_are_not_followed(self, tmp_path, judge):
        client = FakeClient({"Fach A": page_body(text=LONG + '<a href="https://example.org/doc">Doc</a>')})
        result = run(tmp_path, client, spec_body("Fach A"))
        assert result.link_checked == 0 and judge.prompts == []

    def test_git_and_schemeless_links_are_not_followed_but_own_confluence_urls_are(self, tmp_path, judge):
        hrefs = ["https://gitlab.example.com/group/app/-/blob/main/docs/pages/123456",
                 "gitlab.example.com/group/app/-/tree/main?pageId=123457",
                 "mailto:someone@acme.atlassian.net",
                 f"{HOST}/wiki/spaces/ENG/pages/101/Eigene-Seite"]
        text = LONG + "".join(f'<a href="{h}">L{i}</a>' for i, h in enumerate(hrefs))
        judge.verdicts = {"Eigene Seite": (True, "high", "Regeln")}
        client = FakeClient({"Fach A": page_body(text=text), "Eigene Seite": page_body(text=LONG)})
        result = run(tmp_path, client, spec_body("Fach A"))
        assert result.link_checked == 1 and [s.page_id for s in result.suggested] == ["101"]


class TestAssessParsing:
    def test_every_candidate_needs_a_verdict(self):
        with pytest.raises(ValueError):
            refcheck_links._parse_assessments({"assessments": []}, ["1"])

    def test_verdicts_are_read_and_unknown_confidence_is_normalised(self):
        data = {"assessments": [{"id": "1", "relevant": True, "confidence": "HIGH", "reason": " Regeln "},
                                {"id": "2", "relevant": False, "confidence": "??", "reason": ""},
                                {"id": "9", "relevant": True}]}
        assert refcheck_links._parse_assessments(data, ["1", "2"]) == {
            "1": (True, "high", "Regeln"), "2": (False, "low", "")}

    def test_assess_uses_the_cache(self, tmp_path, monkeypatch):
        calls = []

        def ask_json(prompt, llm, timeout, max_tokens, validate, purpose):
            calls.append(prompt)
            raw = json.dumps({"assessments": [{"id": "1", "relevant": True, "confidence": "high", "reason": "r"}]})
            return validate(json.loads(raw)), raw
        monkeypatch.setattr(refcheck_links.analysis, "_ask_json", ask_json)
        cache = SpecCache(tmp_path)
        llm = LLMConfig(provider="openai", api_key="k", models=["m"], llm_timeout=30)
        first = refcheck_links._assess("prompt", llm, cache, ["1"])
        second = refcheck_links._assess("prompt", llm, cache, ["1"])
        assert first == second == {"1": (True, "high", "r")} and len(calls) == 1


class TestConfig:
    RAW = {"parent_page_id": "1", "selector": {"type": "label", "value": "s"}}

    def test_defaults(self):
        cfg = _load_spec_monitor(dict(self.RAW))
        assert (cfg.link_depth, cfg.link_max_pages) == (1, 50)

    def test_block_is_read(self):
        cfg = _load_spec_monitor({**self.RAW, "reference_check": {"link_depth": 2, "max_pages": 10}})
        assert (cfg.link_depth, cfg.link_max_pages) == (2, 10)

    @pytest.mark.parametrize("block", [{"link_depth": 3}, {"link_depth": -1}, {"max_pages": 0}])
    def test_invalid_values_are_refused(self, block):
        with pytest.raises(ValueError, match="reference_check"):
            _load_spec_monitor({**self.RAW, "reference_check": block})


class TestCliFlag:
    def test_link_depth_flag_overrides_the_configured_depth(self, tmp_path, mocker):
        from digest.spec_monitor import cli
        report = TestReport().report()
        run = mocker.patch.object(cli, "run_reference_check", return_value=report)
        mocker.patch.object(cli, "send_html_via_smtp", return_value=True)
        cfg = SimpleNamespace(
            spec_monitor=SpecMonitorConfig(parent_page_id="1", selector_type="label", selector_value="s",
                                           retry=RetryConfig(1, 0), recipient="me@example.com", link_depth=1),
            mgmt_summary=SimpleNamespace(recipient=None), email=SimpleNamespace(recipient=None, subject_prefix="[D]"),
            smtp=SimpleNamespace(use_oauth2=False), m365=SimpleNamespace(tenant_id="t", client_id=None),
            atlassian=None, data_dir=tmp_path)
        args = SimpleNamespace(spec=None, publish_page=False, dry_run=True, check_references=True, link_depth=2)
        cli.run_ref_check_cli(args, cfg, tmp_path / "t", "cmd")
        assert cfg.spec_monitor.link_depth == 2
        run.assert_called_once()


class TestReport:
    def report(self):
        spec = make_spec("<p/>")
        result = RefCheckResult(
            spec=spec, link_depth=2, link_checked=3,
            suggested=[SuggestedSource(title="Fach B", page_id="101", url=f"{HOST}/b", linked_from=["Fach A"],
                                       depth=1, mention="Siehe Fach B <dazu>", reason="Adressregeln",
                                       confidence="high")],
            link_rejected=[RejectedLink(title="Protokoll", url=f"{HOST}/p", reason="Sitzungsprotokoll", code="llm"),
                           RejectedLink(title="Index", url=f"{HOST}/i", reason="", code="too_short")])
        return RefCheckReport(generated_at=__import__("datetime").datetime(2026, 11, 11, 9).astimezone(),
                              results=[result], command_line="cmd")

    def test_mail_shows_suggestions_and_rejections_escaped(self):
        html = refcheck_report.render_mail_html(self.report())
        assert "Fach B" in html and "Adressregeln" in html and "Fach A" in html
        assert "Protokoll" in html and "Sitzungsprotokoll" in html and "Index" in html
        assert "Siehe Fach B &lt;dazu&gt;" in html

    def test_spec_with_only_suggestions_is_flagged_and_counted_in_the_subject(self):
        report = self.report()
        assert "1 Spezifikationen" in refcheck_report.build_subject("[D]", report)
        assert 'id="spec-42"' in refcheck_report.render_mail_html(report)

    def test_confluence_page_shows_suggestions(self):
        storage = refcheck_report.render_confluence_storage(self.report())
        assert "Fach B" in storage and "Adressregeln" in storage and "Sitzungsprotokoll" in storage
