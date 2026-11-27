import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from digest.config import PageWatchConfig, RetryConfig
from digest.spec_monitor import cli
from digest.spec_monitor import page_watch_report as pr
from digest.spec_monitor.models import PageChange, PageStatus, PageWatchReport, ReportError
from digest.spec_monitor.runner import SpecMonitorFatal
from digest.spec_monitor.week import parse_week


def _config(tmp_path, publish_parent=None, recipient="me@example.com", page_watch=True):
    pw = PageWatchConfig(parent_page_id="1", retry=RetryConfig(1, 0), recipient=recipient,
                         publish_parent_page_id=publish_parent) if page_watch else None
    return SimpleNamespace(
        page_watch=pw, spec_monitor=None, mgmt_summary=SimpleNamespace(recipient=None),
        email=SimpleNamespace(recipient=None, subject_prefix="[Digest]"),
        smtp=SimpleNamespace(use_oauth2=False), m365=SimpleNamespace(tenant_id="t", client_id=None),
        atlassian=MagicMock(), data_dir=tmp_path,
    )


def _args(**kw):
    base = dict(page_watch=True, days=None, publish_page=False, dry_run=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _report(errors=None):
    page = PageChange(page_id="10", title="Batch", url="https://x/10", status=PageStatus.CHANGED)
    week = parse_week("KW44-2026", date(2026, 11, 15))
    return PageWatchReport(generated_at=datetime(2026, 11, 13, 8, 0).astimezone(), parent_title="Coremodul",
                           window_start=week.start, window_end=week.end, week=week, pages=[page],
                           errors=errors or [])


@pytest.fixture
def stubs(mocker):
    run = mocker.patch("digest.spec_monitor.page_watch.run_page_watch", return_value=_report())
    mocker.patch.object(cli, "get_auth_header", return_value="Basic x")
    mocker.patch.object(cli, "send_html_via_smtp", return_value=True)
    mocker.patch.object(cli, "find_child_page", return_value=None)
    mocker.patch.object(cli, "publish_page_detailed", return_value=("999", "https://x/page"))
    mocker.patch.object(cli, "put_attachment")
    return run


def test_dry_run_writes_only_the_preview_and_sends_nothing(tmp_path, stubs):
    cli.run_page_watch_cli(_args(dry_run=True, publish_page=True), _config(tmp_path, publish_parent="5"),
                           tmp_path / "t", "cmd")
    out = tmp_path / "spec_cache"
    assert "Coremodul" in (out / "page-watch-preview.html").read_text(encoding="utf-8")
    assert (out / "page-watch-preview.confluence.html").exists()
    assert not (out / "page-watch-latest.html").exists()
    cli.send_html_via_smtp.assert_not_called()
    cli.publish_page_detailed.assert_not_called()
    cli.put_attachment.assert_not_called()
    cli.find_child_page.assert_not_called()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["spec_cache"]


def test_dry_run_with_publish_page_writes_the_data_preview_with_task_boxes(tmp_path, stubs):
    cli.run_page_watch_cli(_args(dry_run=True, publish_page=True), _config(tmp_path, publish_parent="5"),
                           tmp_path / "t", "cmd")
    out = tmp_path / "spec_cache"
    data = json.loads((out / "page-watch-preview.data.json").read_text(encoding="utf-8"))
    assert data["week_label"] == "KW44 2026" and data["pages"][0]["page_id"] == "10"
    assert "<ac:task-id>12</ac:task-id>" in (out / "page-watch-preview.confluence.html").read_text(encoding="utf-8")


def test_dry_run_without_publish_page_writes_no_data_preview(tmp_path, stubs):
    cli.run_page_watch_cli(_args(dry_run=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert not (tmp_path / "spec_cache" / "page-watch-preview.data.json").exists()


def test_run_passes_days_and_command_line(tmp_path, stubs):
    cli.run_page_watch_cli(_args(days=7), _config(tmp_path), tmp_path / "t", "python digest/main.py --page-watch")
    assert stubs.call_args.kwargs == {"days": 7, "week": None, "command_line": "python digest/main.py --page-watch"}


def test_sends_mail_and_keeps_the_html(tmp_path, stubs):
    cli.run_page_watch_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    args = cli.send_html_via_smtp.call_args[0]
    assert args[0].startswith("[Digest] 13.11.2026: 1") and args[3] == "me@example.com"
    assert (tmp_path / "spec_cache" / "page-watch-latest.html").read_text(encoding="utf-8") == args[1]
    cli.publish_page_detailed.assert_not_called()
    cli.find_child_page.assert_not_called()


def test_recipient_falls_back_to_email_recipient(tmp_path, stubs):
    config = _config(tmp_path, recipient=None)
    config.email.recipient = "fallback@example.com"
    cli.run_page_watch_cli(_args(), config, tmp_path / "t", "cmd")
    assert cli.send_html_via_smtp.call_args[0][3] == "fallback@example.com"


def test_no_recipient_is_an_error_but_not_for_dry_run(tmp_path, stubs):
    with pytest.raises(RuntimeError, match="recipient"):
        cli.run_page_watch_cli(_args(), _config(tmp_path, recipient=None), tmp_path / "t", "cmd")
    cli.run_page_watch_cli(_args(dry_run=True), _config(tmp_path, recipient=None), tmp_path / "t", "cmd")


def test_publish_page_needs_a_parent_and_runs_after_the_mail(tmp_path, stubs):
    with pytest.raises(ValueError, match="confluence_page"):
        cli.run_page_watch_cli(_args(publish_page=True), _config(tmp_path), tmp_path / "t", "cmd")
    cli.run_page_watch_cli(_args(publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert cli.publish_page_detailed.call_args[0][3] == "5"


def test_delivery_failure_exits_1_and_keeps_no_latest_file(tmp_path, stubs):
    cli.send_html_via_smtp.return_value = False
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    assert exc.value.code == 1
    assert not (tmp_path / "spec_cache" / "page-watch-latest.html").exists()


def test_problems_exit_2_after_delivery(tmp_path, stubs):
    stubs.return_value = _report(errors=[ReportError(scope="Batch", message="HTTP 403")])
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    assert exc.value.code == 2
    cli.send_html_via_smtp.assert_called_once()


def test_fatal_error_sends_error_mail_and_exits_1(tmp_path, stubs):
    stubs.side_effect = SpecMonitorFatal("tree unreadable")
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    assert exc.value.code == 1
    assert "tree unreadable" in cli.send_html_via_smtp.call_args[0][1]


def test_fatal_error_in_dry_run_sends_nothing(tmp_path, stubs):
    stubs.side_effect = SpecMonitorFatal("tree unreadable")
    with pytest.raises(SystemExit):
        cli.run_page_watch_cli(_args(dry_run=True), _config(tmp_path), tmp_path / "t", "cmd")
    cli.send_html_via_smtp.assert_not_called()


def test_missing_block_is_a_clear_error(tmp_path, stubs):
    with pytest.raises(RuntimeError, match="page_watch"):
        cli.run_page_watch_cli(_args(), _config(tmp_path, page_watch=False), tmp_path / "t", "cmd")


def test_dispatch_routes_page_watch_without_a_spec_monitor_block(tmp_path, mocker):
    run = mocker.patch.object(cli, "run_page_watch_cli")
    cli._dispatch(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    run.assert_called_once()


def _main(monkeypatch, tmp_path, *argv):
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    config = MagicMock()
    config.data_dir = tmp_path
    config.anonymize.enabled = False
    with (
        patch("digest.main.load_config", return_value=config),
        patch("digest.main.resolve_atlassian_config", side_effect=lambda a: a),
        patch("digest.spec_monitor.cli.dispatch") as dispatch,
    ):
        from digest.main import main
        main()
    return dispatch


@pytest.mark.parametrize("argv", [
    ["--days", "7"],
    ["--spec-monitor", "--days", "7"],
    ["--page-watch", "--days", "0"],
    ["--page-watch", "--days", "-3"],
    ["--page-watch", "--days", "x"],
    ["--week", "KW44-2026"],
    ["--page-watch", "--week", "KW44-2026", "--days", "3"],
    ["--page-watch", "--week", "2026-44"],
    ["--page-watch", "--spec-monitor"],
    ["--page-watch", "--create-ticket", "X"],
    ["--page-watch", "--update-spec"],
    ["--page-watch", "--mgmt-summary", "--since", "7d"],
    ["--page-watch", "--spec", "API X"],
    ["--page-watch", "--all-states"],
    ["--page-watch", "--check-references"],
])
def test_invalid_flag_combinations_are_rejected(tmp_path, monkeypatch, argv):
    with pytest.raises(SystemExit):
        _main(monkeypatch, tmp_path, *argv)


@pytest.mark.parametrize("argv,days", [
    (["--page-watch"], None),
    (["--page-watch", "--dry-run"], None),
    (["--page-watch", "--days", "30", "--publish-page"], 30),
    (["--page-watch", "--week", "KW44-2026"], None),
])
def test_valid_page_watch_invocations_dispatch(tmp_path, monkeypatch, argv, days):
    dispatch = _main(monkeypatch, tmp_path, *argv)
    dispatch.assert_called_once()
    assert dispatch.call_args[0][0].page_watch is True and dispatch.call_args[0][0].days == days


def test_week_option_is_passed_on_as_text(tmp_path, monkeypatch):
    dispatch = _main(monkeypatch, tmp_path, "--page-watch", "--week", "kw44 2026")
    assert dispatch.call_args[0][0].week == "kw44 2026"


def test_week_option_is_parsed_and_passed_to_the_run(tmp_path, stubs):
    cli.run_page_watch_cli(_args(week="KW44-2026"), _config(tmp_path), tmp_path / "t", "cmd")
    week = stubs.call_args.kwargs["week"]
    assert week.label == "KW44 2026" and stubs.call_args.kwargs["days"] is None


def test_invalid_week_text_is_an_error_before_any_call(tmp_path, stubs):
    with pytest.raises(ValueError, match="week"):
        cli.run_page_watch_cli(_args(week="KW99-2026"), _config(tmp_path), tmp_path / "t", "cmd")
    stubs.assert_not_called()


@pytest.mark.parametrize("kw", [{"days": 3}, {"window": 14}])
def test_publish_is_refused_for_a_rolling_window(tmp_path, stubs, kw):
    config = _config(tmp_path, publish_parent="5")
    config.page_watch.window_days = kw.get("window")
    with pytest.raises(ValueError, match="calendar week"):
        cli.run_page_watch_cli(_args(publish_page=True, days=kw.get("days")), config, tmp_path / "t", "cmd")
    stubs.assert_not_called()
    cli.publish_page_detailed.assert_not_called()


def test_publish_is_allowed_for_the_default_week_and_for_an_explicit_week(tmp_path, stubs):
    config = _config(tmp_path, publish_parent="5")
    cli.run_page_watch_cli(_args(publish_page=True), config, tmp_path / "t", "cmd")
    config.page_watch.window_days = 14
    cli.run_page_watch_cli(_args(publish_page=True, week="KW44-2026"), config, tmp_path / "t", "cmd")
    assert cli.publish_page_detailed.call_count == 2


def test_weekly_publish_creates_the_page_with_boxes_then_uploads_the_attachment(tmp_path, stubs):
    calls = []
    cli.send_html_via_smtp.side_effect = lambda *a, **k: calls.append("mail") or True
    cli.find_child_page.side_effect = lambda *a, **k: calls.append("find")
    cli.publish_page_detailed.side_effect = lambda *a, **k: calls.append("page") or ("999", "https://x/page")
    cli.put_attachment.side_effect = lambda *a, **k: calls.append("attachment")
    cli.run_page_watch_cli(_args(publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert calls == ["mail", "find", "page", "attachment"]
    assert cli.find_child_page.call_args[0][3:] == ("5", "Page Watch KW44 2026")
    page_args = cli.publish_page_detailed.call_args[0]
    assert page_args[3] == "5" and page_args[4] == "Page Watch KW44 2026"
    assert "<ac:task-id>13</ac:task-id>" in page_args[5]
    att = cli.put_attachment.call_args[0]
    assert att[3] == "999" and att[4] == "page-watch-data.json"
    data = json.loads(att[5].decode("utf-8"))
    assert data["week_tag"] == "2026W44" and data["pages"][0]["actions"]["ticket"]["status"] == "pending"


def test_existing_week_page_refuses_the_publish_and_creates_nothing(tmp_path, stubs, capsys):
    cli.find_child_page.return_value = "777"
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_cli(_args(publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert exc.value.code == 1
    cli.publish_page_detailed.assert_not_called()
    cli.put_attachment.assert_not_called()
    err = capsys.readouterr().err
    assert "Page Watch KW44 2026" in err and "already exists" in err


def test_lookup_failure_exits_1_without_creating_a_page(tmp_path, stubs):
    cli.find_child_page.side_effect = RuntimeError("HTTP 500")
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_cli(_args(publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert exc.value.code == 1
    cli.publish_page_detailed.assert_not_called()


def test_page_creation_failure_exits_1_and_uploads_nothing(tmp_path, stubs):
    cli.publish_page_detailed.side_effect = RuntimeError("HTTP 400")
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_cli(_args(publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert exc.value.code == 1
    cli.put_attachment.assert_not_called()


def test_attachment_failure_exits_1_and_names_the_page_url(tmp_path, stubs, capsys):
    cli.put_attachment.side_effect = RuntimeError("HTTP 413")
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_cli(_args(publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "https://x/page" in err and "HTTP 413" in err


@pytest.mark.parametrize("argv", [
    ["--page-watch-apply", "--page-watch"],
    ["--page-watch-apply", "--spec-monitor"],
    ["--page-watch-apply", "--create-ticket", "X"],
    ["--page-watch-apply", "--update-spec"],
    ["--page-watch-apply", "--mgmt-summary", "--since", "7d"],
    ["--page-watch-apply", "--publish-page"],
    ["--page-watch-apply", "--days", "7"],
    ["--page-watch-apply", "--week", "KW44-2026"],
    ["--page-watch-apply", "KW44-2026", "--spec-monitor"],
    ["--page-watch-apply", "--spec", "X"],
])
def test_invalid_apply_flag_combinations_are_rejected(tmp_path, monkeypatch, argv):
    with pytest.raises(SystemExit):
        _main(monkeypatch, tmp_path, *argv)


@pytest.mark.parametrize("argv,target", [
    (["--page-watch-apply"], ""),
    (["--page-watch-apply", "--dry-run"], ""),
    (["--page-watch-apply", "KW44-2026"], "KW44-2026"),
    (["--page-watch-apply", "12345", "--dry-run"], "12345"),
    (["--page-watch-apply=https://x/wiki/spaces/A/pages/12/T"], "https://x/wiki/spaces/A/pages/12/T"),
])
def test_valid_apply_invocations_dispatch(tmp_path, monkeypatch, argv, target):
    dispatch = _main(monkeypatch, tmp_path, *argv)
    dispatch.assert_called_once()
    assert dispatch.call_args[0][0].page_watch_apply == target


def test_dispatch_routes_apply_before_page_watch(tmp_path, mocker):
    run = mocker.patch.object(cli, "run_page_watch_apply_cli")
    watch = mocker.patch.object(cli, "run_page_watch_cli")
    cli._dispatch(SimpleNamespace(page_watch_apply="", page_watch=False, dry_run=False), _config(tmp_path),
                  tmp_path / "t", "cmd")
    run.assert_called_once()
    watch.assert_not_called()


def _apply(mocker, results=None, error=None, dry_run=False, target=""):
    from digest.spec_monitor import page_watch_apply as pa
    run = mocker.patch.object(pa, "run_apply", side_effect=error, return_value=results or [])
    args = SimpleNamespace(page_watch_apply=target, dry_run=dry_run)
    return run, args


def test_apply_cli_exit_0_when_all_done_or_nothing_to_do(tmp_path, mocker, capsys):
    from digest.spec_monitor.page_watch_apply import ApplyResult
    run, args = _apply(mocker, [ApplyResult("Page Watch KW45 2026", done=["A: frozen: frozen"]), ApplyResult("x")],
                       target="KW45-2026")
    cli.run_page_watch_apply_cli(args, _config(tmp_path))
    assert run.call_args.kwargs == {"target": "KW45-2026", "dry_run": False}
    assert "2 report page(s), 1 action(s) done" in capsys.readouterr().out
    cli.run_page_watch_apply_cli(_apply(mocker)[1], _config(tmp_path))


def test_apply_cli_exit_1_on_fatal(tmp_path, mocker, capsys):
    from digest.spec_monitor.page_watch_apply import ApplyFatal
    _, args = _apply(mocker, error=ApplyFatal("no report page"))
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_apply_cli(args, _config(tmp_path))
    assert exc.value.code == 1 and "no report page" in capsys.readouterr().err


def test_apply_cli_exit_2_when_an_action_failed(tmp_path, mocker, capsys):
    from digest.spec_monitor.page_watch_apply import ApplyResult
    _, args = _apply(mocker, [ApplyResult("p", done=["a"], failed=["B: ticket: Jira down"])])
    with pytest.raises(SystemExit) as exc:
        cli.run_page_watch_apply_cli(args, _config(tmp_path))
    assert exc.value.code == 2 and "Jira down" in capsys.readouterr().err


def test_apply_cli_passes_dry_run(tmp_path, mocker):
    run, args = _apply(mocker, dry_run=True)
    cli.run_page_watch_apply_cli(args, _config(tmp_path))
    assert run.call_args.kwargs["dry_run"] is True


def test_dispatch_prints_anonymize_notices_without_failing_the_run(tmp_path, stubs, mocker, capsys):
    mocker.patch("digest.anonymizer.take_notices", return_value=["ANONYMIZATION REDUCED: directory down"])
    cli.dispatch(_args(dry_run=True), _config(tmp_path), tmp_path / "t", "cmd")
    assert "ANONYMIZATION REDUCED: directory down" in capsys.readouterr().out
