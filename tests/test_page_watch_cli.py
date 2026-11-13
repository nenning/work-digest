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
    return PageWatchReport(generated_at=datetime(2026, 11, 13, 8, 0).astimezone(), parent_title="Coremodul",
                           window_start=date(2026, 10, 31), pages=[page], errors=errors or [])


@pytest.fixture
def stubs(mocker):
    run = mocker.patch("digest.spec_monitor.page_watch.run_page_watch", return_value=_report())
    mocker.patch.object(cli, "get_auth_header", return_value="Basic x")
    mocker.patch.object(cli, "send_html_via_smtp", return_value=True)
    mocker.patch.object(cli, "publish_page", return_value="https://x/page")
    return run


def test_dry_run_writes_only_the_preview_and_sends_nothing(tmp_path, stubs):
    cli.run_page_watch_cli(_args(dry_run=True, publish_page=True), _config(tmp_path, publish_parent="5"),
                           tmp_path / "t", "cmd")
    out = tmp_path / "spec_cache"
    assert "Coremodul" in (out / "page-watch-preview.html").read_text(encoding="utf-8")
    assert (out / "page-watch-preview.confluence.html").exists()
    assert not (out / "page-watch-latest.html").exists()
    cli.send_html_via_smtp.assert_not_called()
    cli.publish_page.assert_not_called()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["spec_cache"]


def test_run_passes_days_and_command_line(tmp_path, stubs):
    cli.run_page_watch_cli(_args(days=7), _config(tmp_path), tmp_path / "t", "python digest/main.py --page-watch")
    assert stubs.call_args.kwargs == {"days": 7, "command_line": "python digest/main.py --page-watch"}


def test_sends_mail_and_keeps_the_html(tmp_path, stubs):
    cli.run_page_watch_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    args = cli.send_html_via_smtp.call_args[0]
    assert args[0].startswith("[Digest] 13.11.2026: 1") and args[3] == "me@example.com"
    assert (tmp_path / "spec_cache" / "page-watch-latest.html").read_text(encoding="utf-8") == args[1]
    cli.publish_page.assert_not_called()


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
    assert cli.publish_page.call_args[0][3] == "5"


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
])
def test_valid_page_watch_invocations_dispatch(tmp_path, monkeypatch, argv, days):
    dispatch = _main(monkeypatch, tmp_path, *argv)
    dispatch.assert_called_once()
    assert dispatch.call_args[0][0].page_watch is True and dispatch.call_args[0][0].days == days
