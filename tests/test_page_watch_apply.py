from datetime import date, datetime
from types import SimpleNamespace

import pytest

from digest.config import AtlassianConfig, PageWatchConfig, RetryConfig, TicketConfig
from digest.spec_monitor import page_watch_apply as pa
from digest.spec_monitor import page_watch_data as pd
from digest.spec_monitor.models import (
    DeltaChange, OpenPoint, PageChange, PageStatus, PageTrace, PageWatchReport, TraceReference, WatchSynthesis,
)
from digest.spec_monitor.publish import PageConflict
from digest.spec_monitor.ticket import TicketDraft, TicketResult
from digest.spec_monitor.week import _week

TODAY = date(2026, 11, 16)
LABELS = {"reject": "reject change", "frozen": "frozen page", "ticket": "create Jira ticket", "cr": "create CR", "done": "done"}
BASE = "https://example.atlassian.net"


def _pc(pid, title, trace=None):
    return PageChange(page_id=pid, title=title, url=f"{BASE}/wiki/pages/{pid}", status=PageStatus.CHANGED,
                      summary=f"summary {title}", labels=[], trace=trace)


def _data(week=(2026, 45), pages=None):
    w = _week(*week)
    syn = WatchSynthesis(
        overview="o",
        delta_changes=[DeltaChange("new", "Sec 1", ["X-1"], change="add field", reason="eCH", blocking=True, page="A"),
                       DeltaChange("changed", "Sec 2", change="rename", page="B")],
        open_points=[OpenPoint("How is X filled?", "A")])
    pages = pages or [_pc("10", "A"), _pc("20", "B"), _pc("30", "C")]
    report = PageWatchReport(
        generated_at=datetime(2026, 11, 9, 8, 0), parent_title="Spec", parent_url=f"{BASE}/wiki/pages/1",
        window_start=w.start, window_end=w.end, week=w, pages=pages, synthesis=syn, language="en")
    return pd.build_data(report, "frozen")


def _storage(data, ticked=()):
    out = "<p>intro</p>" + "".join(pd.render_tasks(e, LABELS) for e in data.pages) + "<p>outro</p>"
    for tid in ticked:
        out = out.replace(f"<ac:task-id>{tid}</ac:task-id><ac:task-status>incomplete",
                          f"<ac:task-id>{tid}</ac:task-id><ac:task-status>complete")
    return out


class Wiki:
    def __init__(self):
        self.pages = {}
        self.children = []
        self.updates = []
        self.attachment_puts = []
        self.conflicts = 0

    def add(self, page_id, data, ticked=(), title=None, with_attachment=True):
        title = title or f"Page Watch {data.week_label}"
        self.pages[page_id] = {"storage": _storage(data, ticked), "version": 3, "title": title,
                               "attachment": pd.to_json(data).encode("utf-8") if with_attachment else None}
        self.children.append({"id": page_id, "title": title})

    def read(self, atl, auth, retry, page_id):
        if page_id not in self.pages:
            raise LookupError(page_id)
        p = self.pages[page_id]
        return p["storage"], p["version"], p["title"]

    def update(self, atl, auth, retry, page_id, title, storage, version, message):
        if self.conflicts:
            self.conflicts -= 1
            raise PageConflict("409")
        p = self.pages[page_id]
        assert version == p["version"]
        p["storage"], p["version"] = storage, version + 1
        self.updates.append((page_id, storage))

    def get_att(self, atl, auth, retry, page_id, filename):
        assert filename == pd.ATTACHMENT_NAME
        return self.pages[page_id]["attachment"]

    def put_att(self, atl, auth, retry, page_id, filename, data):
        self.pages[page_id]["attachment"] = data
        self.attachment_puts.append(page_id)

    def state(self, page_id):
        return pd.from_json(self.pages[page_id]["attachment"].decode("utf-8"))


class FakeClient:
    instances = []

    def __init__(self, *a, **kw):
        self.labels = []
        self.fail = {}
        FakeClient.instances.append(self)

    def add_label(self, page_id, label):
        if page_id in self.fail:
            raise self.fail[page_id]
        self.labels.append((page_id, label))


@pytest.fixture
def env(mocker, tmp_path):
    wiki = Wiki()
    FakeClient.instances = []
    base = "digest.spec_monitor.page_watch_apply."
    mocker.patch(base + "read_page_storage", side_effect=wiki.read)
    mocker.patch(base + "update_page_storage", side_effect=wiki.update)
    mocker.patch(base + "get_attachment", side_effect=wiki.get_att)
    mocker.patch(base + "put_attachment", side_effect=wiki.put_att)
    mocker.patch(base + "list_child_pages", side_effect=lambda *a: list(wiki.children))
    mocker.patch(base + "find_child_page", side_effect=lambda atl, auth, retry, parent, title: next(
        (c["id"] for c in wiki.children if c["title"] == title), None))
    mocker.patch(base + "ConfluenceClient", FakeClient)
    find = mocker.patch(base + "find_ticket_by_label", return_value=None)
    created = []

    def create(config, draft, *, tcfg=None, retry=None):
        created.append((draft, tcfg, retry))
        return TicketResult(url=f"{BASE}/browse/PSSA-{100 + len(created)}")

    mocker.patch(base + "create_ticket_detailed", side_effect=create)
    config = SimpleNamespace(
        page_watch=PageWatchConfig(parent_page_id="1", retry=RetryConfig(1, 0), publish_parent_page_id="5",
                                   ticket=TicketConfig(project="PSSA", labels=["pw"])),
        spec_monitor=None, atlassian=AtlassianConfig(url=BASE, email="e", api_token="t"), data_dir=tmp_path,
        language="en")
    wiki.config, wiki.find, wiki.created, wiki.tmp = config, find, created, tmp_path
    wiki.run = lambda target="", dry_run=False: pa.run_apply(
        config, target=target, dry_run=dry_run, today=TODAY, progress=lambda m: None, auth_header="Basic x")
    wiki.client = lambda: FakeClient.instances[-1]
    return wiki


def _writes(env):
    return env.updates or env.attachment_puts or env.created or any(c.labels for c in FakeClient.instances)


def test_ticked_reject_box_is_ignored_and_stays_ticked(env):
    env.add("100", _data(), ticked=[14, 24])
    (res,) = env.run("")
    assert res.done == [] and res.failed == [] and res.skipped == 0
    assert not _writes(env)
    env.find.assert_not_called()


def test_reject_ticked_next_to_an_action_does_not_change_that_action(env):
    env.add("100", _data(), ticked=[14, 11])
    (res,) = env.run("")
    assert env.client().labels == [("10", "frozen")] and res.failed == []


def test_nothing_ticked_does_nothing(env):
    env.add("100", _data())
    results = env.run("")
    assert [r.done + r.failed for r in results] == [[]]
    assert not _writes(env)
    env.find.assert_not_called()


def test_frozen_ticked_adds_label_marks_done_and_updates_attachment(env):
    env.add("100", _data(), ticked=[11])
    (res,) = env.run("")
    assert env.client().labels == [("10", "frozen")] and res.failed == []
    state = env.state("100").pages[0].actions["frozen"]
    assert (state.status, state.result, state.date) == ("done", "frozen", "2026-11-16")
    storage = env.pages["100"]["storage"]
    assert pd.parse_tasks(storage)[11] == "complete"
    assert "<p>intro</p>" in storage and "<p>outro</p>" in storage
    assert "✔ label set frozen 2026-11-16" in storage
    assert pd.parse_tasks(storage)[12] == "incomplete"


def test_ticket_created_once_and_second_run_does_nothing(env):
    env.add("100", _data(), ticked=[12])
    (res,) = env.run("")
    assert len(env.created) == 1 and env.state("100").pages[0].actions["ticket"].result == "PSSA-101"
    assert res.done == ["A: ticket: PSSA-101"]
    assert f"{BASE}/browse/PSSA-101" in env.pages["100"]["storage"]
    env.find.reset_mock()
    again = env.run("")
    assert len(env.created) == 1 and again[0].skipped == 1
    env.find.assert_not_called()


def test_existing_ticket_found_by_label_is_reused_not_recreated(env):
    env.add("100", _data(), ticked=[22])
    env.find.return_value = "PSSA-7"
    (res,) = env.run("")
    assert env.created == []
    assert env.find.call_args[0][3] == "pagewatch-20-2026W45"
    assert env.state("100").pages[1].actions["ticket"].result == "PSSA-7"
    assert res.done == ["B: ticket: PSSA-7"]


def test_ticket_gets_page_config_and_retry(env):
    env.add("100", _data(), ticked=[12])
    env.run("")
    _, tcfg, retry = env.created[0]
    assert tcfg is env.config.page_watch.ticket and retry is env.config.page_watch.retry


def test_ticket_falls_back_to_spec_monitor_block(env):
    sm_cfg = TicketConfig(project="SM")
    env.config.page_watch.ticket = None
    env.config.spec_monitor = SimpleNamespace(ticket=sm_cfg)
    env.add("100", _data(), ticked=[12])
    env.run("")
    assert env.created[0][1] is sm_cfg


def test_ticket_without_any_ticket_block_fails_that_action(env):
    env.config.page_watch.ticket = None
    env.add("100", _data(), ticked=[12, 11])
    (res,) = env.run("")
    assert len(res.failed) == 1 and "ticket" in res.failed[0] and len(res.done) == 1
    assert env.state("100").pages[0].actions["ticket"].status == "failed"
    assert pd.parse_tasks(env.pages["100"]["storage"])[12] == "complete"
    assert "✔ ticket" not in env.pages["100"]["storage"]


def test_ticket_draft_has_label_summary_and_delta_items():
    data = _data()
    data.pages[0].trace = {
        "status": "FOUND", "searched": ["jira"], "problems": [],
        "references": [{"kind": "jira", "ref": "PSSA-5", "label": "Field X", "url": "https://j/5",
                        "verdict": "in_progress", "reason": "same field", "own": False}] * 5}
    tcfg = TicketConfig(project="PSSA", labels=["pw"])
    draft = pa.build_page_ticket(data, data.pages[0], tcfg)
    assert draft.summary == "A — Delta specification KW45/2026"
    assert draft.labels == ["pw", "pagewatch-10-2026W45"]
    assert draft.spec_url == data.pages[0].url and draft.spec_title == "A"
    d = draft.description
    assert "Sec 1" in d and "X-1" in d and "add field" in d and "eCH" in d and "How is X filled?" in d
    assert "to clarify before implementation".casefold() in d.casefold()
    assert "Sec 2" not in d
    assert d.count("PSSA-5") == 3 and "in progress" in d and "same field" in d
    assert "Traces found" in d and "2026" in d


def test_ticket_draft_german_summary_and_long_title():
    data = _data()
    data.language = "de"
    data.pages[0].title = "T" * 400
    draft = pa.build_page_ticket(data, data.pages[0], TicketConfig(project="P"))
    assert draft.summary.endswith(" — Delta-Spezifikation KW45/2026") and len(draft.summary) <= 255


def test_ticket_draft_never_claims_no_traces_when_incomplete():
    data = _data()
    data.pages[0].trace = {"status": "INCOMPLETE", "references": [], "searched": [], "problems": ["Jira down"]}
    d = pa.build_page_ticket(data, data.pages[0], TicketConfig(project="P")).description
    assert "No traces" not in d and "Jira down" in d


def test_cr_file_contains_ticked_and_done_pages_only(env):
    data = _data()
    data.pages[0].actions["cr"] = pd.ActionState("done", "CR-2026-KW45.md", "2026-11-10")
    env.add("100", data, ticked=[23])
    (res,) = env.run("")
    path = env.tmp / "page_watch" / "CR-2026-KW45.md"
    text = path.read_text(encoding="utf-8")
    assert "## [A]" in text and "## [B]" in text and "## [C]" not in text
    assert text.index("## [A]") < text.index("## [B]")
    assert res.done == ["B: cr: CR-2026-KW45.md"]
    assert env.state("100").pages[1].actions["cr"].status == "done"
    env.add("101", env.state("100"), ticked=[13, 23, 33], title="Page Watch KW45 2026 copy")
    env.run("101")
    assert "## [C]" in path.read_text(encoding="utf-8")


def test_cr_file_is_rewritten_identically_and_ticked_then_unticked_page_is_absent(env):
    data = _data()
    env.add("100", data, ticked=[13, 23])
    env.run("")
    path = env.tmp / "page_watch" / "CR-2026-KW45.md"
    first = path.read_text(encoding="utf-8")
    assert "## [A]" in first and "## [B]" in first
    env.attachment_puts.clear()
    assert env.run("")[0].skipped == 2
    assert path.read_text(encoding="utf-8") == first

    env2 = _data()
    env2.pages[0].actions["cr"] = pd.ActionState("done", "x", "2026-11-10")
    env.add("200", env2, ticked=[33], title="Page Watch KW44 2026")
    env.run("200")
    text = path.read_text(encoding="utf-8")
    assert "## [A]" in text and "## [C]" in text and "## [B]" not in text


def test_conflict_is_retried_once_then_done(env):
    env.add("100", _data(), ticked=[11])
    env.conflicts = 1
    (res,) = env.run("")
    assert res.failed == [] and res.done == ["A: frozen: frozen"]
    assert pd.parse_tasks(env.pages["100"]["storage"])[11] == "complete"


def test_two_conflicts_fail_that_action_and_others_continue(env):
    env.add("100", _data(), ticked=[11, 22])
    env.conflicts = 2
    (res,) = env.run("")
    assert len(res.failed) == 1 and "page update conflicted" in res.failed[0] and len(res.done) == 1
    st = env.state("100")
    assert st.pages[0].actions["frozen"].status == "failed" and "conflicted" in st.pages[0].actions["frozen"].error
    assert st.pages[1].actions["ticket"].status == "done"
    assert "✔ label set" not in env.pages["100"]["storage"] and "✔ ticket" in env.pages["100"]["storage"]


def test_reviewer_edit_during_run_is_kept(env):
    env.add("100", _data(), ticked=[11])
    orig = env.update

    def edit_then_update(atl, auth, retry, page_id, title, storage, version, message):
        if not env.updates and env.conflicts == 0:
            env.pages[page_id]["storage"] += "<p>reviewer note</p>"
            env.pages[page_id]["version"] += 1
            raise PageConflict("409")
        orig(atl, auth, retry, page_id, title, storage, version, message)

    import digest.spec_monitor.page_watch_apply as mod
    mod.update_page_storage.side_effect = edit_then_update
    env.run("")
    assert "reviewer note" in env.pages["100"]["storage"]
    assert pd.parse_tasks(env.pages["100"]["storage"])[11] == "complete"


def test_deleted_spec_page_fails_only_that_action(env):
    env.add("100", _data(), ticked=[11, 12])
    orig_init = FakeClient.__init__

    def init(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.fail = {"10": LookupError("page 10 not found (HTTP 404)")}

    FakeClient.__init__ = init
    try:
        (res,) = env.run("")
    finally:
        FakeClient.__init__ = orig_init
    assert len(res.failed) == 1 and "A: frozen" in res.failed[0] and "deleted or renamed" in res.failed[0]
    assert res.done == ["A: ticket: PSSA-101"]
    st = env.state("100").pages[0].actions
    assert st["frozen"].status == "failed" and st["ticket"].status == "done"
    assert "✔ label set" not in env.pages["100"]["storage"] and "✔ ticket" in env.pages["100"]["storage"]


def test_failed_action_is_retried_next_run(env):
    env.add("100", _data(), ticked=[12])
    import digest.spec_monitor.page_watch_apply as mod
    ok = mod.create_ticket_detailed.side_effect
    mod.create_ticket_detailed.side_effect = RuntimeError("Jira down")
    (res,) = env.run("")
    assert "Jira down" in res.failed[0] and env.state("100").pages[0].actions["ticket"].error == "Jira down"
    mod.create_ticket_detailed.side_effect = ok
    (res,) = env.run("")
    assert res.failed == [] and env.state("100").pages[0].actions["ticket"].status == "done"


def test_ticket_link_failure_is_reported(env):
    env.add("100", _data(), ticked=[12])
    import digest.spec_monitor.page_watch_apply as mod
    mod.create_ticket_detailed.side_effect = lambda *a, **kw: TicketResult(
        url=f"{BASE}/browse/PSSA-5", link_failures=["issue link to X-1: HTTP 404"])
    (res,) = env.run("")
    assert res.done and any("issue link to X-1" in f for f in res.failed)


def test_default_target_selects_recent_report_pages_only(env):
    recent, old = _data((2026, 45)), _data((2026, 30))
    env.add("100", recent, ticked=[11])
    env.add("101", old, ticked=[11])
    env.add("102", _data((2026, 44)))
    env.children.append({"id": "103", "title": "Some other page"})
    env.children.append({"id": "104", "title": "Page Watch KW99 2026"})
    results = env.run("")
    assert [r.page_title for r in results] == ["Page Watch KW44 2026", "Page Watch KW45 2026"]
    assert [r.done for r in results] == [[], ["A: frozen: frozen"]]
    assert env.state("101").pages[0].actions["frozen"].status == "pending"


def test_week_target_and_page_id_and_url_targets(env):
    env.add("100", _data(), ticked=[11])
    for target in ("KW45-2026", "100", f"{BASE}/wiki/spaces/X/pages/100/Page+Watch"):
        env.pages["100"]["storage"] = _storage(_data(), ticked=[11])
        env.pages["100"]["attachment"] = pd.to_json(_data()).encode("utf-8")
        (res,) = env.run(target)
        assert res.done == ["A: frozen: frozen"], target


def test_missing_attachment_is_fatal(env):
    env.add("100", _data(), ticked=[11], with_attachment=False)
    with pytest.raises(pa.ApplyFatal, match="Page Watch KW45 2026"):
        env.run("")


def test_invalid_attachment_is_fatal(env):
    env.add("100", _data(), ticked=[11])
    env.pages["100"]["attachment"] = b"not json"
    with pytest.raises(pa.ApplyFatal, match="unusable"):
        env.run("100")


def test_named_page_not_found_is_fatal(env):
    env.add("100", _data(), ticked=[11])
    with pytest.raises(pa.ApplyFatal, match="KW40 2026"):
        env.run("KW40-2026")
    with pytest.raises(pa.ApplyFatal, match="not found"):
        env.run("999")
    with pytest.raises(pa.ApplyFatal):
        env.run("garbage")
    with pytest.raises(pa.ApplyFatal, match="future"):
        env.run("KW52-2030")


def test_missing_publish_parent_is_fatal(env):
    env.config.page_watch.publish_parent_page_id = None
    with pytest.raises(pa.ApplyFatal, match="parent_page_id"):
        env.run("")
    env.config.page_watch = None
    with pytest.raises(pa.ApplyFatal):
        env.run("")


def test_listing_failure_is_fatal(env, mocker):
    mocker.patch("digest.spec_monitor.page_watch_apply.list_child_pages", side_effect=RuntimeError("HTTP 403"))
    with pytest.raises(pa.ApplyFatal, match="HTTP 403"):
        env.run("")


def test_dry_run_writes_nothing(env):
    env.add("100", _data(), ticked=[11, 12, 13])
    before = dict(env.pages["100"])
    (res,) = env.run("", dry_run=True)
    assert len(res.done) == 3 and all("dry-run" in d for d in res.done) and res.failed == []
    assert not _writes(env) and env.pages["100"] == before
    env.find.assert_not_called()
    assert not (env.tmp / "page_watch").exists()
