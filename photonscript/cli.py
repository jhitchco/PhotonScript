"""PhotonScript CLI — command-line interface for all operations.

Usage:
    photonscript start [--mode scheduler|telescope|librarian|full]
    photonscript plan [--month 3] [--date 2024-03-15]
    photonscript targets [--month 3]
    photonscript sequence [--output tonight.json] [--guided]
    photonscript lint <sequence.json>
    photonscript report [--date 2026-07-01]
    photonscript status [--url http://host:8100] [--timeout 30]
    photonscript autostart-check [--watch-restart] [--kill]   # PS-34a
    photonscript rename-targets [--apply] [--date D] [--stamp-headers]  # PS-78
    photonscript tracking-test-report [--date D] [--pa DEG] [--json]  # PS-84
    photonscript guiding-report [--date D] [--url http://host:8100] [--json]  # PS-88
    photonscript supervise [--mode full]      # keep it running (PS-44)
    photonscript self-update [--dry-run]      # staged, smoke-checked pull (PS-58)
    photonscript stop | restart
    photonscript notify "message"
    photonscript monitor [--url http://host:8100] [--grep cooler] [--level warning]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

app = typer.Typer(
    name="photonscript",
    help="PhotonScript — Remote Telescope Orchestration Platform",
    no_args_is_help=True,
)
console = Console()

# `photonscript start` binds here unless --port is given; the supervisor's
# child command passes no --port, so its health check probes the same port.
DEFAULT_PORT = 8100


@app.command()
def start(
    mode: str = typer.Option("full", help="Run mode: full, scheduler, telescope, librarian"),
    host: str = typer.Option("0.0.0.0", help="Bind address for scheduler"),
    port: int = typer.Option(DEFAULT_PORT, help="Port for scheduler web UI"),
):
    """Start PhotonScript (foreground). Writes a PID file, sets up console +
    rotating-file logging, and runs the orchestrator. Stop it with Ctrl-C or
    `photonscript stop`.

    This is a foreground process. On the scope PC it runs under
    `photonscript supervise` (deploy/run-photonscript.ps1, started at boot by
    the "PhotonScript" scheduled task), which restarts it after a crash.
    """
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import process_control
    from photonscript.orchestrator import start as _start

    config = PhotonScriptConfig(scheduler_host=host, scheduler_port=port)

    # Refuse to start a second copy if a live instance already holds the PID.
    live = process_control.running_pid(config)
    if live is not None:
        console.print(f"[red]PhotonScript is already running (pid {live}).[/red] "
                      f"Stop it first with [bold]photonscript stop[/bold].")
        from photonscript.shared.supervisor import EXIT_ALREADY_RUNNING
        raise typer.Exit(EXIT_ALREADY_RUNNING)

    # Fresh start: clear any leftover STOP sentinel so we don't immediately
    # shut ourselves down, then claim the PID file (overwrites a stale one).
    import os
    process_control.clear_stop_sentinel(config)
    pid_path = process_control.write_pid_file(config)
    console.print(f"[bold blue]PhotonScript[/bold blue] starting in "
                  f"[green]{mode}[/green] mode (pid {os.getpid()}, "
                  f"pidfile {pid_path})...")
    try:
        _start(mode=mode, config=config)  # sets up logging + runs the orchestrator
    finally:
        # Best-effort: a clean/operator stop removes the PID file. Since PS-58
        # the exit-42 update also shuts down gracefully and lands here; only
        # its 15 s hard-exit fallback (os._exit) skips this.
        process_control.remove_pid_file(config)
    from photonscript.orchestrator import requested_exit_code
    code = requested_exit_code()
    if code:
        raise typer.Exit(code)


@app.command()
def stop(
    force: bool = typer.Option(
        False, "--force", help="Hard-kill the process (taskkill /F on Windows) "
        "instead of asking it to stop gracefully."),
):
    """Stop a running PhotonScript instance.

    Default: drop a STOP sentinel that the running process polls (~1s) and then
    shuts down cleanly. --force reads the PID file and hard-kills as a fallback.
    Either way a HOLD marker tells the supervisor to leave it down; start it
    again with `Start-ScheduledTask PhotonScript` (or the wrapper script).
    """
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import process_control
    from photonscript.shared import supervisor as sv

    config = PhotonScriptConfig()
    sv.create_hold(config)
    pid = process_control.read_pid_file(config)

    if pid is None:
        console.print("[yellow]PhotonScript is not running (no PID file).[/yellow]")
        raise typer.Exit(0)

    if not process_control.is_pid_alive(pid):
        console.print(f"[yellow]No live process for pid {pid} — clearing stale "
                      f"PID file.[/yellow]")
        process_control.remove_pid_file(config)
        raise typer.Exit(0)

    if force:
        ok, detail = process_control.force_kill(pid)
        if ok:
            process_control.remove_pid_file(config)
            console.print(f"[green]Force-killed pid {pid}.[/green] {detail}")
            raise typer.Exit(0)
        console.print(f"[red]Failed to force-kill pid {pid}:[/red] {detail}")
        raise typer.Exit(1)

    sentinel = process_control.create_stop_sentinel(config)
    console.print(f"[green]Signaled pid {pid} to shut down gracefully.[/green]")
    console.print(f"[dim]Wrote STOP sentinel {sentinel}; the running process "
                  f"will stop within ~1s and remove it.[/dim]")


@app.command()
def restart():
    """Restart PhotonScript in place (supervisor starts it again right away)."""
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import process_control
    from photonscript.shared import supervisor as sv

    config = PhotonScriptConfig()
    pid = process_control.running_pid(config)
    if pid is None:
        console.print("[yellow]PhotonScript is not running.[/yellow] Start it with "
                      "[bold]Start-ScheduledTask PhotonScript[/bold].")
        raise typer.Exit(1)
    if sv.running_supervisor_pid(config) is None:
        console.print("[red]No supervisor is running[/red], so a restart would "
                      "leave it stopped. Use [bold]photonscript stop[/bold] and "
                      "start it by hand instead.")
        raise typer.Exit(1)
    sv.create_restart(config)
    process_control.create_stop_sentinel(config)
    console.print(f"[green]Asked pid {pid} to stop; the supervisor will start it "
                  f"again.[/green]")


@app.command()
def supervise(
    mode: str = typer.Option("full", help="Run mode passed to `photonscript start`"),
):
    """Run PhotonScript and keep it running: restart on crash with backoff,
    give up after a crash loop, alert via Pushover. Exits 42 on an update
    request so deploy/run-photonscript.ps1 can pull and relaunch. (PS-44)"""
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import supervisor as sv

    repo_root = Path(__file__).resolve().parents[1]
    import os
    os.chdir(repo_root)  # .env is read relative to the working directory
    config = PhotonScriptConfig()
    log_path = sv.setup_supervisor_logging(config.data_dir)
    sv.logger.info("Config from %s, data_dir %s, log %s",
                   repo_root / ".env", config.data_dir, log_path)
    # PS-58: verify a freshly pulled update via /api/health, but only when the
    # wrapper can act on exit 43 (it sets PS_WRAPPER_ROLLBACK=1); an older
    # wrapper or a console run keeps the plain PS-44 behavior.
    rollback = os.environ.get("PS_WRAPPER_ROLLBACK") == "1"
    health_url = (f"http://127.0.0.1:{DEFAULT_PORT}/api/health"
                  if mode in ("full", "scheduler") else None)
    rc = sv.run(config, sv.child_command(mode), cwd=repo_root,
                notify=sv.pushover_notifier(config),
                health_url=health_url, rollback=rollback,
                verify_s=float(getattr(config, "update_verify_s", 90)))
    raise typer.Exit(rc)


@app.command("self-update")
def self_update(
    dry_run: bool = typer.Option(False, "--dry-run", help="Stage and smoke-"
                                 "check the upstream commit; change nothing."),
    tests: Optional[bool] = typer.Option(None, "--tests/--no-tests", help="Also run the "
                               "fast test subset in staging (default: "
                               "PS_UPDATE_SMOKE_TESTS)."),
):
    """Fetch, stage, smoke-check and fast-forward this checkout (PS-58).
    Run by deploy/run-photonscript.ps1 before it starts the supervisor.
    Exit codes: 0 updated, 10 unchanged, 11 deferred (night running),
    2 refused (old code kept, alert sent)."""
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import supervisor as sv
    from photonscript.shared import updater

    repo_root = Path(__file__).resolve().parents[1]
    import os
    os.chdir(repo_root)
    config = PhotonScriptConfig()
    sv.setup_supervisor_logging(config.data_dir)   # updater logs go there too
    rc, detail = updater.self_update(config, repo_root, run_tests=tests,
                                     dry_run=dry_run,
                                     notify=sv.pushover_notifier(config))
    console.print(detail)
    raise typer.Exit(rc)


@app.command("rollback-done")
def rollback_done(
    reason: str = typer.Option("", "--reason", help="Why the wrapper rolled back"),
):
    """Record a rollback and alert (PS-58). The wrapper calls this after it
    reset the checkout to the previous SHA, so it runs the restored code."""
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import supervisor as sv
    from photonscript.shared import updater

    repo_root = Path(__file__).resolve().parents[1]
    import os
    os.chdir(repo_root)
    config = PhotonScriptConfig()
    sv.setup_supervisor_logging(config.data_dir)
    st = updater.rollback_done(config, reason,
                               notify=sv.pushover_notifier(config))
    sv.logger.error("Rolled back %s -> %s: %s", (st.get("bad_sha") or "")[:7],
                    (st.get("target") or "")[:7], st.get("reason"))
    console.print(f"rolled back to {(st.get('target') or '')[:7]}")


@app.command()
def notify(
    message: str = typer.Argument(..., help="Message text"),
    title: str = typer.Option("PhotonScript", "--title", "-t"),
    priority: int = typer.Option(0, "--priority", "-p",
                                 help="-1 quiet, 0 normal, 1 high"),
):
    """Send a Pushover alert with the configured keys (used by the wrapper)."""
    import asyncio
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import pushover

    sent = asyncio.run(pushover.notify(PhotonScriptConfig(), message,
                                       title=title, priority=priority))
    console.print("[green]sent[/green]" if sent else "[yellow]not sent[/yellow] "
                  "(keys unset, rate-limited or quiet daytime)")


@app.command()
def targets(
    month: int = typer.Option(0, help="Month number (1-12), 0 = current"),
):
    """Show recommended targets for a given month."""
    from photonscript.shared.astronomy import get_seasonal_targets, rank_targets_for_night
    from photonscript.shared.config import PhotonScriptConfig

    config = PhotonScriptConfig()
    obs = config.get_observatory()

    if month == 0:
        month = datetime.utcnow().month

    targets = get_seasonal_targets(month)
    now = datetime.utcnow()
    ranked = rank_targets_for_night(targets, obs, now)

    month_names = ["", "January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]

    table = Table(title=f"Targets for {month_names[month]} — {obs.name}")
    table.add_column("Tier", style="bold")
    table.add_column("Name")
    table.add_column("Catalog ID")
    table.add_column("Type")
    table.add_column("Visible (hrs)", justify="right")
    table.add_column("Transit")
    table.add_column("Rec. Hours", justify="right")

    tier_colors = {"best": "green", "better": "blue", "good": "dim"}

    for r in ranked:
        tier = r.get("tier", "good")
        t = r["target"]
        vis = r["visibility"]
        transit = vis.get("transit_time")
        transit_str = transit.strftime("%H:%M UTC") if transit else "—"

        table.add_row(
            f"[{tier_colors.get(tier.value, 'dim')}]{tier.value.upper()}[/]",
            t.name,
            t.catalog_id,
            t.object_type,
            f"{vis['hours']:.1f}",
            transit_str,
            f"{t.recommended_total_hours:.0f}",
        )

    console.print(table)
    console.print(f"\n[dim]{len(ranked)} targets visible tonight from {obs.name}[/dim]")


@app.command()
def plan(
    month: int = typer.Option(0, help="Month (1-12), 0 = current"),
    date: str = typer.Option("", help="Specific date (YYYY-MM-DD)"),
):
    """Plan tonight's imaging session."""
    from photonscript.shared.astronomy import get_seasonal_targets, get_twilight_times
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.target_planner import (
        create_project_from_target, plan_night_sequence,
    )

    config = PhotonScriptConfig()
    obs = config.get_observatory()

    if date:
        dt = datetime.strptime(date, "%Y-%m-%d")
    else:
        dt = datetime.utcnow()

    if month == 0:
        month = dt.month

    twilight = get_twilight_times(obs, dt)
    seasonal = get_seasonal_targets(month)
    projects = [create_project_from_target(t) for t in seasonal]
    sequence = plan_night_sequence(projects, config, dt)

    console.print(Panel(
        f"[bold]Tonight's Plan[/bold] — {dt.strftime('%B %d, %Y')}\n"
        f"Dark: {twilight.get('astro_dark_start', 'N/A')} → {twilight.get('astro_dark_end', 'N/A')} UTC",
        title="PhotonScript",
        border_style="blue",
    ))

    for i, target in enumerate(sequence, 1):
        total_exp = sum(e.exposure_seconds * e.count for e in target.exposures)
        console.print(f"\n[bold cyan]#{i} {target.name}[/bold cyan]")
        console.print(f"   RA: {target.ra_hours:.3f}h  Dec: {target.dec_degrees:.1f}°")
        for exp in target.exposures:
            console.print(
                f"   [dim]{exp.filter_type.value:>4}[/dim]  "
                f"{exp.exposure_seconds:.0f}s × {exp.count} "
                f"(gain {exp.gain})"
            )
        console.print(f"   Total: {total_exp / 3600:.1f} hours")


@app.command()
def sequence(
    output: str = typer.Option("", help="Output file path"),
    month: int = typer.Option(0, help="Month (1-12), 0 = current"),
    fmt: str = typer.Option("json", "--format", help="json (Advanced Sequencer) or xml"),
    guided: bool = typer.Option(False, help="Guided run (default: unguided, CEM70G encoders)"),
    now: bool = typer.Option(False, "--now",
                             help="No dusk gate — starts immediately (daytime testing)"),
):
    """Generate a NINA sequence file for tonight (lint-gated for JSON)."""
    from photonscript.shared.astronomy import get_seasonal_targets
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.target_planner import (
        create_project_from_target, plan_night_sequence,
    )
    from photonscript.scheduler.nina_sequence import generate_nina_xml, build_sequence_for_night

    config = PhotonScriptConfig()
    now_dt = datetime.utcnow()
    if month == 0:
        month = now_dt.month

    seasonal = get_seasonal_targets(month)
    projects = [create_project_from_target(t) for t in seasonal]
    targets = plan_night_sequence(projects, config, now_dt)
    for t in targets:
        t.start_guiding = guided
    seq = build_sequence_for_night(f"PhotonScript_{now_dt.strftime('%Y%m%d')}", targets)
    seq.wait_until_local = None if now else "00:00:00"  # flag: gate on dusk providers

    if fmt == "xml":
        content = generate_nina_xml(seq)
        default_path = f"PhotonScript_{now_dt.strftime('%Y%m%d')}.xml"
    else:
        from photonscript.scheduler.nina_sequence_json import generate_nina_json
        from photonscript.scheduler.sequence_lint import lint as lint_seq, format_result

        content = generate_nina_json(seq)
        default_path = f"PhotonScript_{now_dt.strftime('%Y%m%d')}.json"

        # Lint gate — refuse to write a sequence that would fail at 3 AM
        result = lint_seq(json.loads(content), guided=guided)
        console.print(format_result(result))
        if not result.ok:
            console.print("[red]REFUSING to write sequence: lint failed.[/red]")
            raise typer.Exit(1)

    path = Path(output) if output else Path(default_path)
    path.write_text(content)
    console.print(f"[green]Sequence saved to {path}[/green]")
    console.print(f"[dim]{len(targets)} targets, ready for NINA import[/dim]")


@app.command()
def lint(
    file: str = typer.Argument(..., help="Sequence JSON file to validate"),
    guided: bool = typer.Option(None, help="Expected mode (default: auto-detect)"),
):
    """Validate a NINA Advanced Sequencer JSON against AARO operational rules."""
    from photonscript.scheduler.sequence_lint import lint_file, format_result

    result = lint_file(file, guided=guided)
    console.print(format_result(result))
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def report(
    date: str = typer.Option("", help="Night ending on date (YYYY-MM-DD), default yesterday"),
):
    """Daily report: sky utilization + photon efficiency for a night."""
    from datetime import timedelta
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.daily_report import build_daily_report

    config = PhotonScriptConfig()
    d = date or (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    rpt = build_daily_report(config, d)
    console.print(Panel(rpt.to_text(), title="PhotonScript daily", border_style="blue"))


@app.command("qa-backfill")
def qa_backfill(
    date: str = typer.Option(..., help="Night (YYYY-MM-DD, the runs page date)"),
    apply: bool = typer.Option(False, "--apply",
                               help="Write the changes (default: dry run)"),
    unsafe: list[str] = typer.Option(
        [], help="Extra unsafe window FROM/TO in UTC ISO, e.g. "
                 "2026-09-27T11:39:44Z/2026-09-27T13:00:00Z (repeatable)"),
):
    """PS-71: re-grade a night for roof-closed / parked frames. Dry run unless
    --apply; with --apply rejected subs leave the stack set (Library links
    move to Library/_rejected/)."""
    import json as _json
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.qa_backfill import regrade_parked

    extra = []
    for w in unsafe:
        a, _, b = w.partition("/")
        extra.append((a, b))
    res = regrade_parked(PhotonScriptConfig(), date, apply=apply,
                         extra_unsafe=extra)
    console.print_json(_json.dumps(res))


@app.command("qa-rescore")
def qa_rescore(
    date: str = typer.Option(..., help="Night (YYYY-MM-DD, the runs page date)"),
    apply: bool = typer.Option(False, "--apply",
                               help="Write the changes (default: dry run)"),
    allow_unreject: bool = typer.Option(
        False, "--allow-unreject",
        help="Let the new rules pass a sub that was rejected before"),
    records: str = typer.Option(
        "", help="Grade a copy instead: a <date>_subs.jsonl or a saved "
                 "/api/runs/<date> JSON (dry run only)"),
    full: bool = typer.Option(False, "--full", help="Print every diff row"),
):
    """PS-21: re-grade a night's stored metrics with the unified QA rules and
    show the verdict diff. Dry run unless --apply; human verdicts are never
    changed."""
    import json as _json
    from pathlib import Path as _P
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.runs import rescore_night

    recs = None
    if records:
        text = _P(records).read_text(encoding="utf-8")
        try:
            doc = _json.loads(text)
            recs = doc["subs"] if isinstance(doc, dict) else doc
        except ValueError:
            recs = [_json.loads(x) for x in text.splitlines() if x.strip()]
    res = rescore_night(PhotonScriptConfig(), date, apply=apply,
                        allow_unreject=allow_unreject, records=recs)
    if not full:
        res = {**res, "diffs": res["diffs"][:20],
               "diffs_total": len(res["diffs"])}
    console.print_json(_json.dumps(res, default=str))


def _last_night() -> str:
    """The evening date of the night that started most recently (local)."""
    from datetime import timedelta
    now = datetime.now()
    return (now - timedelta(days=1 if now.hour < 12 else 0)).strftime("%Y-%m-%d")


@app.command("guiding-report")
def guiding_report(
    date: str = typer.Option("", help="Night (YYYY-MM-DD, the evening date); "
                                      "default the night that started last"),
    file: str = typer.Option("", help="One PHD2 guide log by name instead"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Ask a running PhotonScript (GET /api/phd2/analysis) "
                                 "instead of reading PHD2's logs on this machine"),
    no_subs: bool = typer.Option(False, "--no-subs", help="Skip the per-sub part"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-88: why guiding went the way it did. Reads the night's PHD2 guide
    logs and prints the findings (with fixes), the calibrations and every
    guiding session, plus the guiding during each graded sub. Read-only.

    photonscript guiding-report --date 2026-09-26
    photonscript guiding-report --date 2026-09-26 --url http://100.94.189.77:8100
    """
    import json as _json
    from photonscript.scheduler.phd2_analysis import format_report, night_analysis
    d = date or ("" if file else _last_night())
    if url:
        import urllib.parse
        import urllib.request
        q = urllib.parse.urlencode({"date": d, "file": file,
                                    "subs": "false" if no_subs else "true"})
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/phd2/analysis?" + q,
                                        timeout=170) as r:
                rep = _json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Could not read {url}: {e}[/red]")
            raise typer.Exit(2)
    else:
        rep = night_analysis(_config_for_repo(Path(__file__).resolve().parents[1]),
                             date=d, file=file, with_subs=not no_subs)
    if as_json:
        print(_json.dumps(rep, indent=2, default=str))
    else:
        console.print(format_report(rep), markup=False, highlight=False)
    raise typer.Exit(0 if rep.get("ok") else 1)


@app.command("flexure-report")
def flexure_report(
    date: str = typer.Option("", help="Night (YYYY-MM-DD, the evening date); "
                                      "default the night that started last"),
    solve: bool = typer.Option(True, "--solve/--no-solve",
                               help="Plate-solve missing sampled subs with "
                                    "ASTAP (first/middle/last Piggy sub and "
                                    "one RC16 sub per block)"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Ask a running PhotonScript (GET /api/flexure) "
                                 "instead of reading this machine's data"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-96: Piggy-600 vs RC16 differential flexure for one night: drift
    rates of both rigs ("/min), their difference, per-pair shape comparison
    and the hardware causes the pattern points to. Report only.

    photonscript flexure-report --date 2026-09-25
    """
    import json as _json
    from photonscript.scheduler.flexure import build_report, format_report
    d = date or _last_night()
    if url:
        import urllib.parse
        import urllib.request
        q = urllib.parse.urlencode({"date": d, "refresh": "true"})
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/flexure?" + q,
                                        timeout=170) as r:
                rep = _json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Could not read {url}: {e}[/red]")
            raise typer.Exit(2)
    else:
        rep = build_report(_config_for_repo(Path(__file__).resolve().parents[1]),
                           d, solve=solve)
    if as_json:
        print(_json.dumps(rep, indent=2, default=str))
    else:
        console.print(format_report(rep), markup=False, highlight=False)
    raise typer.Exit(0 if rep.get("ok") else 1)


@app.command("tracking-test-report")
def tracking_test_report(
    date: str = typer.Option("", help="Night (YYYY-MM-DD, the runs page "
                                      "date); default tonight"),
    pa: Optional[float] = typer.Option(
        None, help="Camera position angle (NINA plate-solve rotation, deg) "
                   "to label the elongation axis RA or Dec"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-84: unguided tracking test report (TPoint + ProTrack): subs named
    'Tracking test ...' grouped by filter and exposure, the longest length
    that passes unguided per filter, and a recommendation. Read-only."""
    import json as _json
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.tracking_test import (build_report,
                                                      format_report)
    rep = build_report(PhotonScriptConfig(), date or None, pa_override=pa)
    if as_json:
        console.print_json(_json.dumps(rep, default=str))
    else:
        console.print(format_report(rep), markup=False, highlight=False)


@app.command()
def preflight():
    """Run the full daytime system test (config, dirs, NINA, PHD2, lint, Pushover)."""
    import asyncio
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.preflight import run_preflight

    result = asyncio.run(run_preflight(PhotonScriptConfig()))
    colors = {"pass": "green", "warn": "yellow", "fail": "red"}
    for c in result["checks"]:
        console.print(f"[{colors[c['status']]}]{c['status'].upper():5s}[/] "
                      f"[bold]{c['name']:28s}[/] {c['detail']}")
    verdict = "[bold green]GO[/]" if result["go"] else "[bold red]NO-GO[/]"
    s = result["summary"]
    console.print(f"\n{verdict} — {s['pass']} pass, {s['warn']} warn, {s['fail']} fail")
    raise typer.Exit(0 if result["go"] else 1)


@app.command()
def bundle(
    date: str = typer.Option("", help="Night ending on date (YYYY-MM-DD), default yesterday"),
):
    """Package the night's evidence into one zip for post-mortem analysis.

    Contents: daily report, armer state, projects, dispatched sequences,
    and the most recent NINA log. Copy the zip to your analysis machine.
    """
    from datetime import timedelta
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.runs import build_bundle

    config = PhotonScriptConfig()
    d = date or (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    out = build_bundle(config, d)
    console.print(f"[green]Bundle written: {out}[/green]")
    console.print("Copy it to your analysis machine (e.g. the Claude folder) "
                  "for post-mortem review.")


@app.command()
def analyze(
    date: str = typer.Option(..., help="Night (YYYY-MM-DD) to pull subs from"),
    which: str = typer.Option("rejected", help="rejected | accepted | all"),
    files: str = typer.Option("", help="Comma-separated rel 'file' values "
                              "(overrides --which)"),
):
    """Copy a night's subs into the library Syncthing share for off-scope
    analysis. They mirror to the desktop (desktop_library_dir/_analysis/<date>)
    where PixInsight — or Claude — can open the raw FITS.
    """
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.runs import stage_for_analysis

    config = PhotonScriptConfig()
    picked = [f.strip() for f in files.split(",") if f.strip()] or None
    res = stage_for_analysis(config, date, files=picked, which=which)
    console.print(f"[green]Copied {res['copied']}/{res['requested']} sub(s) "
                  f"to the analysis dropbox:[/green] {res['dropbox']}")
    for f in res["files"]:
        if f.get("ok"):
            console.print(f"  • {f['name']}  →  "
                          f"{f.get('desktop_path', '(desktop path unset)')}")
        else:
            console.print(f"  [red]✗ {f.get('file')}: {f.get('error')}[/red]")


@app.command("rename-targets")
def rename_targets(
    apply: bool = typer.Option(False, "--apply",
                               help="Write the changes (default: dry run)"),
    date: list[str] = typer.Option(
        [], help="Only these nights (YYYY-MM-DD, repeatable; default all)"),
    stamp_headers: bool = typer.Option(
        False, "--stamp-headers",
        help="Also rewrite FITS OBJECT headers that still hold a container "
             "name (header only; off by default)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-78: rename container-named subs to their real target
    ("Heart Nebula imaging (repeats while safe and up)_Container" -> "Heart
    Nebula"; OSC loop names -> unattributed, then PS-51 piggyback
    correlation) and merge container-named Library folders into the target
    folders (moves only, collisions reported, nothing deleted). Dry run
    unless --apply. Afterwards: POST /api/projects2/recount."""
    import json as _json

    from photonscript.scheduler.target_backfill import rename_backfill
    from photonscript.shared.config import PhotonScriptConfig

    r = rename_backfill(PhotonScriptConfig(), apply=apply, dates=date or None,
                        stamp_headers=stamp_headers)
    if as_json:
        console.print_json(_json.dumps(r))
        return
    title = "APPLIED" if r["applied"] else "DRY RUN"
    t = Table(title=f"{title} - PS-78 rename ({r['nights_scanned']} nights "
                    f"scanned, {r['nights_changed']} changed)")
    for c in ("Night", "Rig", "From", "To", "Subs"):
        t.add_column(c, justify="right" if c == "Subs" else "left")
    for n in r["nights"]:
        for x in n["renamed"]:
            t.add_row(n["date"], x["rig"], x["from"], x["to"], str(x["subs"]))
        for name, k in n["piggyback_correlated"].items():
            t.add_row(n["date"], "piggyback", "? (correlated)", name, str(k))
    console.print(t)
    console.print(f"Subs per target: {r['subs_by_target']}; piggyback still "
                  f"unattributed: {r['piggyback_still_unattributed']}")
    lt = Table(title=f"Library {r['library']}")
    for c in ("Folder", "Moves to", "Links", "Collisions"):
        lt.add_column(c)
    for f in r["library_folders"]:
        to = ", ".join(f"{k} ({v})" for k, v in f["to"].items()) or "-"
        lt.add_row(f["folder"], to, str(f["moved"]), str(len(f["collisions"])))
    console.print(lt)
    for f in r["library_folders"]:
        for c in f["collisions"]:
            console.print(f"  [yellow]collision (left in place): "
                          f"{f['folder']}/{c['file']} -> {c['dest']}"
                          f"{' (same file)' if c.get('same_file') else ''}"
                          f"{' ' + c['error'] if c.get('error') else ''}"
                          "[/yellow]")
    if r["headers"]["requested"]:
        console.print(f"OBJECT headers: {r['headers']['candidates']} "
                      f"candidates, {r['headers']['stamped']} rewritten")
    if r["applied"]:
        console.print("[green]Done. Now resync goals: POST "
                      "/api/projects2/recount[/green]")
    else:
        console.print("[yellow]Dry run only. Add --apply to write.[/yellow]")


@app.command("archive-library")
def archive_library(
    before: str = typer.Option(..., help="Archive nights before this date (YYYY-MM-DD)"),
    calibration: str = typer.Option("flats", help="flats (default) | all | none"),
    apply: bool = typer.Option(False, "--apply", help="Actually move (default: dry run)"),
    dest: str = typer.Option("", help="Archive root (default <share parent>/NINAArchive)"),
):
    """Move old Library lights (+ calibration) OUT of the Syncthing share so the
    desktop stops tracking them. Dry run unless --apply. Desktop copies of moved
    files are removed by Syncthing (the desktop mirrors deletions)."""
    from photonscript.scheduler.library_archive import run_archive
    from photonscript.shared.config import PhotonScriptConfig
    config = PhotonScriptConfig()
    r = run_archive(config, before, calibration, apply=apply, dest=dest or None)
    table = Table(title=f"{'APPLIED' if r['applied'] else 'DRY RUN'} - archive "
                        f"before {before} (calibration: {calibration})")
    table.add_column("Kind"); table.add_column("Files", justify="right")
    table.add_column("GB", justify="right")
    for k, v in r["summary"].items():
        table.add_row(k, str(v["files"]), f"{v['gb']:.2f}")
    console.print(table)
    console.print(f"[cyan]{r['files']} files, {r['gb']} GB -> "
                  f"{r['archive_root']}[/cyan]")
    if r.get("kept_calibration"):
        kept = ", ".join(f"{k['type']} {k['date']} ({k['files']})"
                         for k in r["kept_calibration"][:12])
        console.print(f"[yellow]Kept in the share (older calibration, not "
                      f"archived in this mode): {kept}[/yellow]")
    if r["applied"]:
        console.print(f"[green]Moved {r['moved']}, failed {r['failed']}[/green]")
        for e in r.get("errors", []):
            console.print(f"  [red]{e}[/red]")
    else:
        console.print("[yellow]Dry run only. Add --apply to move.[/yellow]")


@app.command("prune-nights")
def prune_nights(
    before: str = typer.Option(
        "2026-09-01", help="Delete night folders strictly before this date "
        "(YYYY-MM-DD)."),
    execute: bool = typer.Option(
        False, "--execute", help="Actually delete. Omit for a dry run."),
    quarantine: str = typer.Option(
        "", help="Instead of deleting, MOVE matched folders here (recoverable, "
        "frees space only if the target is another drive)."),
    permanent: bool = typer.Option(
        False, "--permanent", help="With --execute and no --quarantine, "
        "hard-delete (default is also a hard delete; kept for clarity)."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation."),
):
    """Prune captured FITS from nights before a cutoff to free the capture drive.

    Dry run by default — prints what WOULD go. Uses the live config paths
    (image_watch_dir, piggyback dir, thumbnail cache). Grade records and
    contact sheets are ALWAYS kept — the per-sub learnings survive the prune.

    Examples:
        photonscript prune-nights                 # dry run, cutoff 2026-09-01
        photonscript prune-nights --execute       # delete, with a prompt
        photonscript prune-nights --before 2026-08-01 --execute --yes
    """
    import shutil
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.runs import prunable_night_dirs

    config = PhotonScriptConfig()
    items = prunable_night_dirs(config, before)
    if not items:
        console.print(f"[green]Nothing before {before} found.[/green]")
        return

    total = 0.0
    table = Table(title=f"{'EXECUTE' if execute else 'DRY RUN'} — folders before "
                        f"{before}")
    table.add_column("Date"); table.add_column("GB", justify="right")
    table.add_column("Path")
    for it in items:
        b = sum(f.stat().st_size for f in Path(it["path"]).rglob("*")
                if f.is_file())
        it["gb"] = round(b / 1e9, 2)
        total += it["gb"]
        table.add_row(it["date"], f"{it['gb']:.2f}", it["path"])
    console.print(table)
    console.print(f"[cyan]{len(items)} folders · {round(total, 2)} GB[/cyan]")

    if not execute:
        console.print("[yellow]Dry run only. Add --execute to delete "
                      "(or --execute --quarantine <dir> to move).[/yellow]")
        return

    action = "MOVE" if quarantine else "DELETE"
    if not yes and not typer.confirm(
            f"{action} {len(items)} folders ({round(total, 2)} GB)?"):
        console.print("[red]Aborted.[/red]")
        raise typer.Exit(1)

    freed = 0.0
    for it in items:
        try:
            if quarantine:
                dest = Path(quarantine) / it["date"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(it["path"], str(dest))
            else:
                shutil.rmtree(it["path"])
            freed += it["gb"]
            console.print(f"  [green]{'moved' if quarantine else 'removed'}[/green] "
                          f"{it['date']} ({it['gb']:.2f} GB)")
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]FAILED[/red] {it['date']} — {e}")
    console.print(f"[cyan]Done. {'Moved' if quarantine else 'Freed'} "
                  f"~{round(freed, 2)} GB. Grade records + contact sheets "
                  "kept.[/cyan]")


@app.command()
def monitor(
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Remote scheduler, e.g. http://100.94.189.77:8100 "
                                 "(default: tail the local log file)"),
    lines: int = typer.Option(50, "--lines", "-n", help="Lines of history first"),
    since: str = typer.Option("", "--since", help="Only lines newer than 30m / 2h / 1d"),
    grep: str = typer.Option("", "--grep", "-g", help="Only lines containing this text"),
    level: str = typer.Option("DEBUG", "--level", "-l",
                              help="Minimum level: debug, info, warning, error"),
    no_color: bool = typer.Option(False, "--no-color", help="Plain text"),
    follow: bool = typer.Option(True, "--follow/--no-follow",
                                help="Keep tailing (default) or print and exit"),
    interval: float = typer.Option(1.0, "--interval", help="Poll seconds"),
):
    """Tail the PhotonScript service log live, colorized by level and event.

    photonscript monitor                      # local log on this machine
    photonscript monitor --url http://100.94.189.77:8100 -g cooler
    photonscript monitor --since 2h --level warning --no-follow
    """
    import time
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import logmonitor as lm

    try:
        flt = lm.LineFilter(level, grep, lm.parse_since(since))
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    out = Console(highlight=False, no_color=no_color, soft_wrap=True)

    def emit(raw_lines):
        for s in lm.render(raw_lines, flt, color=not no_color):
            if no_color:
                out.print(s, markup=False)
            else:
                out.print(s)

    try:
        if url:
            import httpx
            base = url.rstrip("/")
            r = httpx.get(f"{base}/api/logs/tail",
                          params={"offset": -1, "lines": lines}, timeout=20)
            r.raise_for_status()
            d = r.json()
            emit(d.get("lines", []))
            off = d.get("offset", 0)
            while follow:
                time.sleep(interval)
                try:
                    d = httpx.get(f"{base}/api/logs/tail", params={"offset": off},
                                  timeout=20).json()
                except Exception as e:  # noqa: BLE001 — keep tailing through blips
                    out.print(f"[dim]… {base} unreachable ({e.__class__.__name__}), "
                              "retrying[/dim]")
                    time.sleep(max(interval, 5))
                    continue
                if d.get("rotated"):
                    out.print("[dim]— log rotated —[/dim]")
                emit(d.get("lines", []))
                off = d.get("offset", off)
        else:
            path = lm.service_log_path(PhotonScriptConfig())
            if not path.exists():
                console.print(f"[red]No log at {path}[/red] — is PhotonScript "
                              "running on this machine? Use --url for a remote one.")
                raise typer.Exit(1)
            out.print(f"[dim]tailing {path} (Ctrl-C to stop)[/dim]")
            emit(lm.last_lines(path, lines))
            fol = lm.FileFollower(path, from_end=True)
            while follow:
                time.sleep(interval)
                emit(fol.poll())
    except KeyboardInterrupt:
        pass


def _probe_health(base: str, timeout: float = 30.0, slow_s: float = 2.0) -> dict:
    """GET /api/health and classify the answer (PS-57): up (with latency),
    slow (answered, but over 2 s), old (server predates /api/health),
    refused (nothing listening), timeout (listening but not answering, i.e.
    running and stalled) or error."""
    import time as _time
    import httpx
    t0 = _time.monotonic()
    try:
        r = httpx.get(f"{base}/api/health", timeout=timeout)
    except httpx.ConnectError as e:
        return {"state": "refused", "detail": str(e) or "connection refused"}
    except httpx.TimeoutException:
        return {"state": "timeout", "after_s": timeout}
    except Exception as e:  # noqa: BLE001
        return {"state": "error", "detail": f"{type(e).__name__}: {e}"}
    dt = _time.monotonic() - t0
    if r.status_code == 404:
        return {"state": "old", "latency_s": dt}
    if r.status_code != 200:
        return {"state": "error", "detail": f"HTTP {r.status_code}", "latency_s": dt}
    try:
        data = r.json()
    except ValueError:
        data = {}
    return {"state": "slow" if dt > slow_s else "up", "latency_s": dt, "data": data}


@app.command()
def status(
    url: str = typer.Option("http://localhost:8100", "--url",
                            help="Scheduler to query, e.g. "
                                 "https://teles-feb25.lobster-bleak.ts.net"),
    timeout: float = typer.Option(30.0, "--timeout",
                                  help="Seconds to wait for the scheduler"),
):
    """Show whether PhotonScript is running (PID, uptime, supervisor, version),
    whether its API is up, slow or down (GET /api/health), and the telescope
    state from the running scheduler."""
    import httpx
    import time as _time
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared import process_control
    from photonscript.shared import supervisor as sv

    config = PhotonScriptConfig()
    console.print(Panel("[bold]PhotonScript Status[/bold]", border_style="blue"))

    # --- this machine's process (meaningful on the scope PC) ---
    pid = process_control.running_pid(config)
    if pid is not None:
        up = ""
        try:
            import psutil
            up = f", up {_fmt_uptime(_time.time() - psutil.Process(pid).create_time())}"
        except Exception:  # noqa: BLE001
            pass
        console.print(f"  Process:    [green]running[/green] (pid {pid}{up})")
    else:
        console.print("  Process:    [dim]not running on this machine[/dim]")
    spid = sv.running_supervisor_pid(config)
    console.print("  Supervisor: " + (f"[green]running[/green] (pid {spid})" if spid
                                      else "[yellow]not running[/yellow]"))
    if sv.hold_active(config):
        console.print("  Hold:       [yellow]HOLD set (operator stop)[/yellow]")

    # --- the API: up, slow or down? (PS-57) ---
    base = url.rstrip("/")
    h = _probe_health(base, timeout=timeout)
    st = h["state"]
    if st == "refused":
        console.print(f"[red]Cannot reach the scheduler at {base}[/red]: "
                      f"connection refused (nothing listening)")
        return
    if st == "timeout":
        console.print(f"[red]Cannot reach the scheduler at {base}[/red]: no answer "
                      f"within {timeout:.0f} s. The port is open, so the process is "
                      f"probably running but stalled; check "
                      f"{config.data_dir / 'logs' / 'stalls.log'}")
        return
    if st == "error":
        console.print(f"[red]Cannot reach the scheduler at {base}[/red]: {h['detail']}")
        return
    lat = h["latency_s"]
    d = h.get("data") or {}
    loop = d.get("loop") or {}
    lag = (f", loop lag {loop.get('lag_ms', 0):.0f} ms, max "
           f"{loop.get('max_lag_ms_5min', 0):.0f} ms / 5 min"
           + (f", {loop['stalls_5min']} stall(s)" if loop.get("stalls_5min") else "")
           if loop else "")
    if st == "slow":
        console.print(f"  API:        [yellow]SLOW[/yellow], answered in {lat:.1f} s{lag}")
    elif st == "up":
        console.print(f"  API:        [green]up[/green], answered in {lat:.2f} s{lag}")
    else:  # old server without /api/health
        console.print(f"  API:        [green]up[/green] ({lat:.2f} s, no /api/health: "
                      f"older version)")
    if d:
        pr = d.get("process") or {}
        up_s = d.get("uptime_s")
        bits = [f"pid {d.get('pid')}", f"mode {d.get('mode')}"]
        if up_s is not None:
            bits.append(f"up {_fmt_uptime(up_s)}")
        for key, label in (("session_id", "session"), ("priority_class", "priority"),
                           ("power_throttling", "throttling"), ("launcher", "launcher")):
            if pr.get(key) not in (None, ""):
                bits.append(f"{label} {pr[key]}")
        console.print("  Service:    " + ", ".join(bits))
        console.print(f"  Armer:      {d.get('armer', '?')}")

    try:
        ver = httpx.get(f"{base}/api/update/check", timeout=timeout).json()
        console.print(f"  Version:    {ver.get('running', '?')}"
                      + (f" ([yellow]{ver.get('behind')} behind[/yellow])"
                         if ver.get("behind") else ""))
    except Exception:  # noqa: BLE001
        if d.get("version"):
            console.print(f"  Version:    {d['version']}")

    try:
        data = httpx.get(f"{base}/api/status", timeout=timeout).json()
        telescope = data.get("telescope", {})
        state = telescope.get("session_state", "unknown")
        state_color = {"imaging": "green", "idle": "dim", "error": "red"}.get(state, "yellow")

        console.print(f"  Telescope:  [{state_color}]{state.upper()}[/]")
        console.print(f"  Target:     {telescope.get('current_target', '-')}")
        console.print(f"  Filter:     {telescope.get('current_filter', '-')}")
        g = telescope.get('guiding', {}) or {}
        if g.get('units', 'arcsec') == 'arcsec' and g.get('rms_total_arcsec') is not None:
            gtxt = f"{g['rms_total_arcsec']:.2f}\""
        else:  # PS-70: guide pixel scale unknown, say so instead of fake arcsec
            gtxt = f"{g.get('rms_total_px', 0) or 0:.2f} guide px (scale unknown)"
        console.print(f"  Guiding:    {gtxt}")
        console.print(f"  Images:     {telescope.get('images_captured_tonight', 0)} tonight")
        console.print(f"  Projects:   {data.get('active_projects', 0)} active / {data.get('total_projects', 0)} total")
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]/api/status failed:[/yellow] {type(e).__name__}")


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m")


def _config_for_repo(repo: Path):
    """PhotonScriptConfig with the repo's .env, whatever the current directory."""
    from photonscript.shared.config import PhotonScriptConfig
    env = repo / ".env"
    return PhotonScriptConfig(_env_file=str(env)) if env.exists() else PhotonScriptConfig()


@app.command("autostart-check")
def autostart_check(
    url: str = typer.Option("http://localhost:8100", "--url",
                            help="Scheduler to probe (GET /api/health)"),
    task: str = typer.Option("PhotonScript", "--task", help="Scheduled task name"),
    repo_opt: str = typer.Option("", "--repo",
                                 help="Checkout the task runs (default: this one)"),
    user: str = typer.Option("jeremy", "--user", help="Account the task runs as"),
    hours: float = typer.Option(24.0, "--hours",
                                help="Look-back window for stalls.log and supervisor.log"),
    tailscale_url: str = typer.Option(
        "https://teles-feb25.lobster-bleak.ts.net", "--tailscale-url",
        help='tailscale serve URL to probe ("" skips it)'),
    watch_restart: bool = typer.Option(
        False, "--watch-restart",
        help="After the checks, wait for the service to be killed and time how "
             "long the supervisor takes to bring it back"),
    kill: bool = typer.Option(
        False, "--kill",
        help="With --watch-restart: hard-kill the service here instead of asking "
             "you to. Refused while the armer is ARMED, RUNNING or PAUSED_UNSAFE"),
    max_wait: float = typer.Option(180.0, "--max-wait",
                                   help="Seconds to wait for the restart"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of lines"),
):
    """Check the autostart setup (PS-34a): scheduled task, Windows auto-logon,
    one supervised PhotonScript in a desktop session, /api/health fast and on
    the checked-out commit, no stalls, NINA / PHD2 / tailscale reachable.

    Read-only unless --kill is given. Exit 1 when any check FAILs.

    photonscript autostart-check                      # after install / reboot
    photonscript autostart-check --hours 16           # morning after a night
    photonscript autostart-check --watch-restart      # you kill it, it times recovery
    photonscript autostart-check --watch-restart --kill
    """
    from rich.text import Text
    from photonscript.shared import autostart_check as ac
    from photonscript.shared.rigs import PIGGYBACK, rig_ids

    if kill and not watch_restart:
        console.print("[red]--kill only works together with --watch-restart.[/red]")
        raise typer.Exit(2)
    repo = Path(repo_opt) if repo_opt else Path(__file__).resolve().parents[1]
    config = _config_for_repo(repo)
    nina = [("1", config.nina_base_url)]
    if PIGGYBACK in rig_ids(config):
        nina.append(("2", getattr(config, "piggyback_nina_base_url",
                                  "http://localhost:1889/v2/api")))
    opts = ac.Options(repo=repo, data_dir=Path(config.data_dir), url=url,
                      task_name=task, user=user, hours=hours, nina_urls=nina,
                      tailscale_url=tailscale_url)
    env = ac.Env()
    styles = {ac.PASS: "green", ac.WARN: "yellow", ac.FAIL: "bold red",
              ac.SKIP: "dim", ac.INFO: "cyan"}

    def show(checks):
        for c in checks:
            console.print(Text.assemble((f"{c.status.upper():5s}", styles[c.status]),
                                        f" {c.name:20s} {c.detail}"))

    checks, _ = ac.run_checks(env, opts)
    if not as_json:
        console.print(f"PhotonScript autostart check, {datetime.now():%Y-%m-%d %H:%M}, "
                      f"repo {repo}, data_dir {config.data_dir}")
        show(checks)
    if watch_restart:
        say = (lambda s: None) if as_json else (lambda s: console.print(s, markup=False))
        more = ac.watch_restart(env, opts, kill=kill, max_recover_s=max_wait, say=say)
        if not as_json:
            show(more)
        checks += more
    text, rc = ac.verdict(checks)
    if as_json:
        print(json.dumps({"verdict": text, "exit": rc,
                          "checks": [c.as_dict() for c in checks]}, indent=2))
    else:
        console.print(Text(text, style="bold green" if rc == 0 else "bold red"))
    raise typer.Exit(rc)


if __name__ == "__main__":
    app()
