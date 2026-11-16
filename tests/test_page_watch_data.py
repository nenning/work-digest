import json
from datetime import datetime

import pytest

from digest.spec_monitor import page_watch_data as pd
from digest.spec_monitor.models import (
    DeltaChange, OpenPoint, PageChange, PageStatus, PageTrace, PageWatchReport, TraceReference, WatchSynthesis,
)
from digest.spec_monitor.week import _week

LABELS = {"frozen": "frozen page", "ticket": "create Jira ticket", "cr": "create CR", "done": "done"}


def _pc(pid, title, status=PageStatus.CHANGED, labels=None, trace=None):
    return PageChange(page_id=pid, title=title, url=f"https://x/{pid}", status=status, summary=f"sum {pid}",
                      labels=labels, trace=trace)


def _report(pages, week=True, synthesis=None):
    w = _week(2026, 45)
    return PageWatchReport(
        generated_at=datetime(2026, 11, 9, 8, 0), parent_title="Spec", parent_url="https://x/parent",
        window_start=w.start, window_end=w.end, week=w if week else None, pages=pages, synthesis=synthesis,
        language="en")


def _data(pages=None, **kw):
    pages = pages or [_pc("1", "A", labels=["frozen"]), _pc("2", "B", labels=["frozen"])]
    return pd.build_data(_report(pages, **kw), "frozen")


def _task(i, status, body="x"):
    return (f"<ac:task><ac:task-id>{i}</ac:task-id><ac:task-status>{status}</ac:task-status>"
            f"<ac:task-body>{body}</ac:task-body></ac:task>")


def test_round_trip():
    trace = PageTrace("FOUND", [TraceReference("jira", "PSSA-1", "t", "https://j/1", "in_progress", "r", True)],
                      ["jira"], ["p"])
    syn = WatchSynthesis(
        overview="o", delta_changes=[DeltaChange("new", "Sec", ["X-1"], page="A", blocking=True)],
        open_points=[OpenPoint("q?", "A")])
    d = pd.build_data(_report([_pc("1", "A", trace=trace), _pc("2", "B")], synthesis=syn), "frozen")
    d.pages[0].actions["ticket"] = pd.ActionState("done", "PSSA-9", "2026-11-16")
    assert pd.from_json(pd.to_json(d)) == d
    assert d.pages[0].trace["references"][0]["ref"] == "PSSA-1"
    assert d.pages[0].deltas[0]["ids"] == ["X-1"]
    assert d.pages[0].open_points == ["q?"]
    assert d.pages[1].deltas == []


def test_from_json_rejects_bad_input():
    d = _data()
    with pytest.raises(ValueError):
        pd.from_json(pd.to_json(d).replace('"schema": 1', '"schema": 2'))
    with pytest.raises(ValueError):
        pd.from_json("not json")
    with pytest.raises(ValueError):
        pd.from_json("[]")
    raw = json.loads(pd.to_json(d))
    del raw["pages"][0]["title"]
    with pytest.raises(ValueError):
        pd.from_json(json.dumps(raw))
    raw = json.loads(pd.to_json(d))
    del raw["week_tag"]
    with pytest.raises(ValueError):
        pd.from_json(json.dumps(raw))


def test_build_data_header_and_indexing():
    d = pd.build_data(_report([_pc("1", "A"), _pc("9", "Q", PageStatus.UNCHANGED), _pc("2", "B", PageStatus.NEW)]),
                      "frozen")
    assert [(p.index, p.page_id) for p in d.pages] == [(1, "1"), (2, "2")]
    assert (d.week_label, d.week_tag, d.file_stem) == ("KW45 2026", "2026W45", "CR-2026-KW45")
    assert (d.window_start, d.window_end) == ("2026-11-02", "2026-11-08")
    assert (d.parent_title, d.parent_url, d.language) == ("Spec", "https://x/parent", "en")


def test_build_data_requires_week():
    with pytest.raises(ValueError):
        pd.build_data(_report([_pc("1", "A")], week=False), "frozen")


def test_show_frozen_rules():
    d = pd.build_data(_report([
        _pc("1", "A", labels=["Frozen"]),
        _pc("2", "B", labels=[], trace=PageTrace("FROZEN")),
        _pc("3", "C", labels=None),
        _pc("4", "D", labels=["x"], trace=PageTrace("NONE")),
    ]), "frozen")
    assert [p.show_frozen for p in d.pages] == [False, False, True, True]
    assert set(d.pages[0].actions) == {"ticket", "cr"}
    assert set(d.pages[2].actions) == {"frozen", "ticket", "cr"}
    assert d.pages[2].trace is None


def test_task_id_mapping():
    assert pd.task_id(3, "cr") == 33
    assert pd.task_id(1, "frozen") == 11
    assert pd.task_action(31) == (3, "frozen")
    assert pd.task_action(12) == (1, "ticket")
    with pytest.raises(ValueError):
        pd.task_action(14)


def test_parse_tasks_over_two_lists():
    s = (f"<p>a</p><ac:task-list>{_task(11, 'complete')}{_task(12, 'incomplete')}</ac:task-list>"
         f"<h2>b</h2><ac:task-list>{_task(23, 'complete')}</ac:task-list>")
    assert pd.parse_tasks(s) == {11: "complete", 12: "incomplete", 23: "complete"}
    assert pd.parse_tasks("<p>none</p>") == {}


def test_render_tasks_without_frozen():
    e = _data().pages[0]
    out = pd.render_tasks(e, LABELS)
    assert pd.parse_tasks(out) == {12: "incomplete", 13: "incomplete"}
    assert out.count("<ac:task-list>") == 1
    assert 'href="https://x/1"' in out and "create Jira ticket" in out and "create CR" in out
    assert "frozen page" not in out


def test_render_tasks_with_frozen_and_ids_for_page_two():
    d = pd.build_data(_report([_pc("1", "A", labels=None), _pc("2", "B", labels=None)]), "frozen")
    assert pd.parse_tasks(pd.render_tasks(d.pages[1], LABELS)) == {21: "incomplete", 22: "incomplete", 23: "incomplete"}


def test_render_tasks_done_lines_and_empty():
    e = _data().pages[0]
    e.actions["ticket"] = pd.ActionState("done", "PSSA-1", "2026-11-16")
    out = pd.render_tasks(e, LABELS)
    assert "<p>" in out and "done" in out and "PSSA-1" in out and "2026-11-16" in out
    assert pd.parse_tasks(out) == {13: "incomplete"}
    e.actions["cr"] = pd.ActionState("done", "CR-2026-KW45.md", "2026-11-16")
    out = pd.render_tasks(e, LABELS)
    assert "<ac:task" not in out and out.count("<p>") == 2


def test_render_tasks_failed_action_stays_a_task_and_escapes():
    d = pd.build_data(_report([_pc("1", "A<b>&", labels=None)]), "frozen")
    e = d.pages[0]
    e.actions["frozen"] = pd.ActionState("failed", error="boom")
    out = pd.render_tasks(e, LABELS)
    assert 11 in pd.parse_tasks(out)
    assert "A<b>&" not in out and "A&lt;b&gt;&amp;" in out


def test_mark_task_done_changes_only_target():
    old_body = '<a href="u">p</a>'
    s = (f"<p>keep &amp; me</p><ac:task-list>{_task(12, 'incomplete', old_body)}\n{_task(13, 'incomplete')}"
         f"</ac:task-list><h2>tail</h2>")
    out = pd.mark_task_done(s, 12, "<p>new</p>")
    assert out == (f"<p>keep &amp; me</p><ac:task-list>{_task(12, 'complete', '<p>new</p>')}\n"
                   f"{_task(13, 'incomplete')}</ac:task-list><h2>tail</h2>")
    assert pd.parse_tasks(out) == {12: "complete", 13: "incomplete"}


def test_mark_task_done_body_with_backslash_and_group_refs():
    s = f"<ac:task-list>{_task(12, 'incomplete')}</ac:task-list>"
    out = pd.mark_task_done(s, 12, r"a\1\g<0>")
    assert r"a\1\g<0>" in out


def test_mark_task_done_missing_task():
    with pytest.raises(ValueError):
        pd.mark_task_done(f"<ac:task-list>{_task(12, 'incomplete')}</ac:task-list>", 13, "x")
