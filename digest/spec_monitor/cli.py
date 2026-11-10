"""CLI delivery of a spec-monitor run: preview, mail, optional Confluence page."""
import logging
import sys
from pathlib import Path
from typing import Optional

from digest import usage
from digest.auth.atlassian import get_auth_header
from digest.auth.microsoft import get_token
from digest.config import Config
from digest.email_sender import send_html_via_com, send_html_via_smtp
from digest.spec_monitor.models import RunReport
from digest.spec_monitor.publish import publish_page
from digest.spec_monitor.report import _problem_count, build_subject, render_confluence_storage, render_mail_html
from digest.spec_monitor.runner import SpecMonitorFatal, run_spec_monitor

log = logging.getLogger(__name__)


def _recipient(config: Config) -> Optional[str]:
    return config.spec_monitor.recipient or config.mgmt_summary.recipient or config.email.recipient


def _deliver(config: Config, cache_file: Path, subject: str, html_body: str, recipient: str) -> bool:
    if config.smtp:
        token = None
        if config.smtp.use_oauth2:
            token = get_token(config.m365.tenant_id, cache_file, client_id=config.m365.client_id)
        return send_html_via_smtp(subject, html_body, config.smtp, recipient, token)
    if sys.platform == "win32":
        return send_html_via_com(subject, html_body, recipient)
    raise RuntimeError(
        "No smtp block configured and COM is Windows-only. "
        "Add an smtp: block to config.yaml and run --setup-smtp-auth."
    )


def _console_summary(report: RunReport) -> None:
    changed = [a for a in report.analysed if a.has_changes]
    print(
        f"\nSpec monitor: {len(report.analysed)} analysed ({len(changed)} with changes), "
        f"{len(report.listed_only)} not analysed (status), {len(report.excluded_pages)} excluded page(s)"
    )
    for a in report.analysed:
        marker = "CHANGES" if a.has_changes else "no changes"
        flag = " [analysis incomplete]" if a.analysis_incomplete else ""
        print(f"  - {a.spec.title} ({a.spec.status}): {marker}{flag}")
        for e in a.errors:
            print(f"      ERROR {e.scope}: {e.message}")
        for s in a.sources:
            for e in s.errors:
                print(f"      ERROR {s.ref.name}: {e}")


def _with_statistics(html_body: str) -> str:
    """Dry-run preview only: the mail itself carries no run statistics. Placed above the footer."""
    stats = usage.current()
    if stats is None:
        return html_body
    block = usage.format_html(stats)
    marker = '<div class="footer">'
    if marker in html_body:
        return html_body.replace(marker, block + "\n\n        " + marker, 1)
    return html_body + block


def dispatch(args, config: Config, cache_file: Path, command_line: str) -> None:
    """Runs the selected spec-monitor mode and prints the run statistics afterwards, also on failure."""
    usage.start()
    try:
        _dispatch(args, config, cache_file, command_line)
    finally:
        stats = usage.stop()
        if stats is not None:
            report = usage.format_report(stats)
            print(report)
            log.info(report)


def _dispatch(args, config: Config, cache_file: Path, command_line: str) -> None:
    if config.spec_monitor is None:
        raise RuntimeError("No spec_monitor block configured. Add one to config.yaml to use the spec monitor.")
    from digest.spec_monitor.spec_ref import UPDATE_ALL, normalize_spec_ref
    if getattr(args, "spec", None):
        args.spec = normalize_spec_ref(args.spec)
    if args.create_ticket:
        from digest.spec_monitor.ticket import run_create_ticket
        args.create_ticket = normalize_spec_ref(args.create_ticket)
        config.spec_monitor.analyse_all_states = True   # the user named the spec explicitly
        run_create_ticket(args, config)
    elif args.update_spec:
        from digest.spec_monitor.spec_update import run_update_spec
        if args.update_spec != UPDATE_ALL:
            args.update_spec = normalize_spec_ref(args.update_spec)
            config.spec_monitor.analyse_all_states = True   # the user named the spec explicitly
        elif getattr(args, "all_states", False):
            config.spec_monitor.analyse_all_states = True
        run_update_spec(args, config)
    else:
        run_cli(args, config, cache_file, command_line)


def run_cli(args, config: Config, cache_file: Path, command_line: str) -> None:
    sm = config.spec_monitor
    if sm is None:
        raise RuntimeError("No spec_monitor block configured. Add one to config.yaml to use --spec-monitor.")
    if getattr(args, "all_states", False):
        sm.analyse_all_states = True
    if args.publish_page and not sm.publish_parent_page_id:
        raise ValueError("--publish-page requires spec_monitor.confluence_page.parent_page_id in config.yaml")
    recipient = _recipient(config)
    if not recipient and not args.dry_run:
        raise RuntimeError("email.recipient (or mgmt_summary.recipient / spec_monitor.recipient) must be set in config.yaml")

    try:
        report = run_spec_monitor(config, only=args.spec, command_line=command_line, dry_run=bool(args.dry_run),
                                 defer_baselines=not args.dry_run)
    except SpecMonitorFatal as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if recipient and not args.dry_run:
            try:
                if not _deliver(config, cache_file, f"{sm_prefix(config)} Fehler", f"<p>Spec-Monitor-Lauf fehlgeschlagen:</p><pre>{_esc(str(exc))}</pre>", recipient):
                    print("ERROR: the error mail could not be sent (see the delivery error above); "
                          "this console output is the only notice of the failed run", file=sys.stderr)
            except Exception as mail_exc:
                print(f"ERROR: could not send the error mail: {mail_exc}", file=sys.stderr)
        raise SystemExit(1)

    subject = build_subject(config.email.subject_prefix, report)
    html_body = render_mail_html(report)
    _console_summary(report)

    if args.dry_run:
        out_dir = config.data_dir / "spec_cache"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "preview.html").write_text(_with_statistics(html_body), encoding="utf-8")
        print(f"Preview written to {out_dir / 'preview.html'}")
        if args.publish_page:
            (out_dir / "preview.confluence.html").write_text(render_confluence_storage(report), encoding="utf-8")
            print(f"Confluence page preview written to {out_dir / 'preview.confluence.html'} (not published)")
        _exit_on_problems(report)
        return

    print(f"Sending to {recipient}...")
    if not _deliver(config, cache_file, subject, html_body, recipient):
        print("ERROR: mail delivery failed; URL baselines were not advanced, so the same changes are reported again next run",
              file=sys.stderr)
        raise SystemExit(1)

    if report.commit_baselines is not None:
        failed = report.commit_baselines()
        for msg in failed:
            print(f"ERROR: {msg}; this change will be reported again next run", file=sys.stderr)
        if failed:
            raise SystemExit(1)

    if args.publish_page:
        title = f"Quellen-Änderungen {report.generated_at.strftime('%Y-%m-%d %H:%M')}"
        try:
            url = publish_page(
                config.atlassian, get_auth_header(config.atlassian), sm.retry, sm.publish_parent_page_id,
                title, render_confluence_storage(report), "Spec source monitor run",
            )
        except Exception as exc:
            print(f"ERROR: Confluence page could not be published: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print(f"Confluence page: {url}")

    _exit_on_problems(report)


def _exit_on_problems(report) -> None:
    """The mail and page are out, but a run with errors must not look successful to the scheduler."""
    n = _problem_count(report)
    if n:
        print(f"ERROR: the run finished with {n} problem(s) (see the error table in the report); exiting with code 2",
              file=sys.stderr)
        raise SystemExit(2)


def sm_prefix(config: Config) -> str:
    return config.email.subject_prefix


def _esc(text: str) -> str:
    import html
    return html.escape(text)
