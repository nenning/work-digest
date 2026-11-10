from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from digest.config import RetryConfig, SpecMonitorConfig
from digest.spec_monitor import cli
from digest.spec_monitor.models import RunReport


def _config(tmp_path, publish_parent=None, recipient="me@example.com"):
    return SimpleNamespace(
        spec_monitor=SpecMonitorConfig(
            parent_page_id="1", selector_type="label", selector_value="s", retry=RetryConfig(1, 0),
            recipient=recipient, publish_parent_page_id=publish_parent),
        mgmt_summary=SimpleNamespace(recipient=None),
        email=SimpleNamespace(recipient=None, subject_prefix="[Digest]"),
        smtp=SimpleNamespace(use_oauth2=False), m365=SimpleNamespace(tenant_id="t", client_id=None),
        atlassian=MagicMock(), data_dir=tmp_path,
    )


def _args(**kw):
    base = dict(spec=None, publish_page=False, dry_run=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def stubs(mocker):
    report = RunReport(generated_at=datetime(2026, 11, 10, 12, 0).astimezone())
    mocker.patch.object(cli, "run_spec_monitor", return_value=report)
    mocker.patch.object(cli, "render_mail_html", return_value="<html>mail</html>")
    mocker.patch.object(cli, "render_confluence_storage", return_value="<p>page</p>")
    mocker.patch.object(cli, "build_subject", return_value="Subject")
    mocker.patch.object(cli, "get_auth_header", return_value="Basic x")
    mocker.patch.object(cli, "send_html_via_smtp", return_value=True)
    mocker.patch.object(cli, "publish_page", return_value="https://x/page")
    return report


def test_dry_run_writes_preview_and_sends_nothing(tmp_path, stubs):
    cli.run_cli(_args(dry_run=True, publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert (tmp_path / "spec_cache" / "preview.html").read_text(encoding="utf-8") == "<html>mail</html>"
    assert (tmp_path / "spec_cache" / "preview.confluence.html").exists()
    cli.send_html_via_smtp.assert_not_called()
    cli.publish_page.assert_not_called()


def test_sends_mail_to_spec_monitor_recipient(tmp_path, stubs):
    cli.run_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    assert cli.send_html_via_smtp.call_args[0][3] == "me@example.com"
    cli.publish_page.assert_not_called()


def test_publish_page_after_mail(tmp_path, stubs, capsys):
    cli.run_cli(_args(publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert cli.publish_page.call_args[0][3] == "5"
    assert "https://x/page" in capsys.readouterr().out


def test_publish_requires_parent_page(tmp_path, stubs):
    with pytest.raises(ValueError, match="confluence_page.parent_page_id"):
        cli.run_cli(_args(publish_page=True), _config(tmp_path), tmp_path / "t", "cmd")


def test_mail_failure_exits_nonzero(tmp_path, stubs):
    cli.send_html_via_smtp.return_value = False
    with pytest.raises(SystemExit) as e:
        cli.run_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    assert e.value.code == 1


def test_fatal_error_sends_error_mail_and_exits(tmp_path, stubs):
    cli.run_spec_monitor.side_effect = cli.SpecMonitorFatal("parent unreachable")
    with pytest.raises(SystemExit) as e:
        cli.run_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    assert e.value.code == 1
    assert "parent unreachable" in cli.send_html_via_smtp.call_args[0][1]


def test_missing_recipient_is_an_error(tmp_path, stubs):
    with pytest.raises(RuntimeError, match="recipient"):
        cli.run_cli(_args(), _config(tmp_path, recipient=None), tmp_path / "t", "cmd")


def test_all_states_flag_sets_runtime_flag_and_never_comes_from_yaml(tmp_path, stubs):
    cfg = _config(tmp_path)
    assert cfg.spec_monitor.analyse_all_states is False
    cli.run_cli(_args(dry_run=True, all_states=True), cfg, tmp_path / "t", "cmd")
    assert cfg.spec_monitor.analyse_all_states is True


def test_dispatch_routes_create_ticket(tmp_path, mocker):
    import sys
    fake = MagicMock()
    mocker.patch.dict(sys.modules, {"digest.spec_monitor.ticket": fake})
    cfg = _config(tmp_path)
    cli.dispatch(_args(create_ticket="API X", update_spec=None), cfg, tmp_path / "t", "cmd")
    fake.run_create_ticket.assert_called_once()
    assert cfg.spec_monitor.analyse_all_states is True


def test_dispatch_routes_update_spec(tmp_path, mocker):
    import sys
    fake = MagicMock()
    mocker.patch.dict(sys.modules, {"digest.spec_monitor.spec_update": fake})
    cfg = _config(tmp_path)
    cli.dispatch(_args(create_ticket=None, update_spec="API X"), cfg, tmp_path / "t", "cmd")
    fake.run_update_spec.assert_called_once()
    assert cfg.spec_monitor.analyse_all_states is True


def test_dispatch_default_runs_the_report(tmp_path, stubs):
    cli.dispatch(_args(create_ticket=None, update_spec=None, dry_run=True), _config(tmp_path), tmp_path / "t", "cmd")
    assert (tmp_path / "spec_cache" / "preview.html").exists()


def test_dispatch_without_spec_monitor_block_is_an_error(tmp_path):
    cfg = _config(tmp_path)
    cfg.spec_monitor = None
    with pytest.raises(RuntimeError, match="spec_monitor block"):
        cli.dispatch(_args(create_ticket=None, update_spec=None), cfg, tmp_path / "t", "cmd")


def test_dispatch_update_spec_accepts_a_url_and_forces_all_states(tmp_path, mocker):
    import sys
    fake = MagicMock()
    mocker.patch.dict(sys.modules, {"digest.spec_monitor.spec_update": fake})
    cfg = _config(tmp_path)
    args = _args(create_ticket=None, update_spec="https://x.atlassian.net/wiki/spaces/R/pages/678297650/API+X")
    cli.dispatch(args, cfg, tmp_path / "t", "cmd")
    assert args.update_spec == "678297650"
    assert cfg.spec_monitor.analyse_all_states is True


def test_dispatch_update_all_respects_the_configured_states(tmp_path, mocker):
    import sys
    fake = MagicMock()
    mocker.patch.dict(sys.modules, {"digest.spec_monitor.spec_update": fake})
    cfg = _config(tmp_path)
    cli.dispatch(_args(create_ticket=None, update_spec="*"), cfg, tmp_path / "t", "cmd")
    assert cfg.spec_monitor.analyse_all_states is False
    assert fake.run_update_spec.call_args[0][0].update_spec == "*"


def test_dispatch_update_all_with_all_states_flag(tmp_path, mocker):
    import sys
    fake = MagicMock()
    mocker.patch.dict(sys.modules, {"digest.spec_monitor.spec_update": fake})
    cfg = _config(tmp_path)
    cli.dispatch(_args(create_ticket=None, update_spec="*", all_states=True), cfg, tmp_path / "t", "cmd")
    assert cfg.spec_monitor.analyse_all_states is True


def test_dispatch_normalises_url_for_create_ticket_and_spec(tmp_path, mocker):
    import sys
    fake = MagicMock()
    mocker.patch.dict(sys.modules, {"digest.spec_monitor.ticket": fake})
    args = _args(create_ticket="https://x.atlassian.net/wiki/pages/viewpage.action?pageId=42", update_spec=None,
                 spec="https://x.atlassian.net/wiki/spaces/R/pages/7/T")
    cli.dispatch(args, _config(tmp_path), tmp_path / "t", "cmd")
    assert args.create_ticket == "42" and args.spec == "7"


def _problem_report():
    from digest.spec_monitor.models import ReportError
    return RunReport(generated_at=datetime(2026, 11, 10, 12, 0).astimezone(),
                     errors=[ReportError(scope="Spec A", message="page could not be read")])


def test_problems_exit_with_code_2_after_mail_and_publish(tmp_path, stubs, capsys):
    cli.run_spec_monitor.return_value = _problem_report()
    with pytest.raises(SystemExit) as e:
        cli.run_cli(_args(publish_page=True), _config(tmp_path, publish_parent="5"), tmp_path / "t", "cmd")
    assert e.value.code == 2
    cli.send_html_via_smtp.assert_called_once()
    cli.publish_page.assert_called_once()
    assert "1 problem" in capsys.readouterr().err


def test_dry_run_with_problems_also_exits_2(tmp_path, stubs):
    cli.run_spec_monitor.return_value = _problem_report()
    with pytest.raises(SystemExit) as e:
        cli.run_cli(_args(dry_run=True), _config(tmp_path), tmp_path / "t", "cmd")
    assert e.value.code == 2


def test_failed_error_mail_is_announced(tmp_path, stubs, capsys):
    cli.run_spec_monitor.side_effect = cli.SpecMonitorFatal("parent unreachable")
    cli.send_html_via_smtp.return_value = False
    with pytest.raises(SystemExit) as e:
        cli.run_cli(_args(), _config(tmp_path), tmp_path / "t", "cmd")
    assert e.value.code == 1
    assert "error mail could not be sent" in capsys.readouterr().err
