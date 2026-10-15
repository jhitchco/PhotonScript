"""PhotonScript CLI — command-line interface for all operations.

Usage:
    photonscript start [--mode scheduler|telescope|librarian|full]
    photonscript plan [--month 3] [--date 2024-03-15]
    photonscript targets [--month 3]
    photonscript sequence [--output tonight.json] [--guided]
    photonscript lint <sequence.json>
    photonscript report [--date 2026-07-01]
    photonscript ecc-scale-report --date D [--date D2] [--json]  # PS-94
    photonscript status [--url http://host:8100] [--timeout 30]
    photonscript autostart-check [--watch-restart] [--kill]   # PS-34a
    photonscript rename-targets [--apply] [--date D] [--stamp-headers]  # PS-78
    photonscript tracking-test-report [--date D] [--pa DEG] [--json]  # PS-84
    photonscript optics-test-report [--date D] [--json]  # PS-148
    photonscript guiding-report [--date D] [--url http://host:8100] [--json]  # PS-88
    photonscript optics-report [--date D] [--rig rc16] [--json]  # PS-95
    photonscript sub-doctor --night D [--rig R] [--url U] [--nina-events] [--timeline] [--json]
    photonscript unguided-limits [--rig R] [--refresh] [--url U] [--since D] [--json]
    photonscript pilot-backtest --night D [--url U | --doc FILE] [--no-limits] [--json]
    photonscript core-check --target M31 [--rig rc16] [--night D] [--url U] [--measure DIR] [--json]
    photonscript blend-readiness [--target T] [--json] [--url U]   # two-rig NB blend
    photonscript target-links --target T [--add LABEL URL] [--remove LABEL] [--url U] [--json]  # goal links
    photonscript exposure-report --target M31 [--rig piggyback] [--filter OSC]
        [--library PATH] [--profile] [--feature-arcmin 60] [--json]
        [--camera-cal]                                            # PS-117
    photonscript thesky-audit [--json] [--imagelink] [--thesky-imagelink]  # PS-104
    photonscript settings [--night D] [--at HH:MM] [--diff A B] [--sub FILE]
        [--full] [--json] [--url U]                               # settings ledger
    photonscript settings-snapshot [--no-push] [--json]
    photonscript settings-backfill [--night D] [--apply] [--force] [--json]
    photonscript guiding-status [--json]                          # PS-119
    photonscript guide-recover [--dry-run] [--yes] [--force]       # PS-167
    photonscript calibration-plan [--rig R] [--json]              # PS-113
    photonscript calibration-owed [--rig R] [--json]              # PS-122
    photonscript calibration-capture --rig R [--exposures 300,400] [--count N]
    photonscript calibration-qa [--backfill] [--rig R] [--dry-run]
    photonscript calibration-library [--rig R] [--url U] [--desktop-library P] [--json]  # PS-178
    photonscript calibration-status [--rig R] [--url U] [--mirror P] [--nights N] [--json]  # PS-182
    photonscript calibration-file --url U [--rig R] [--dry-run] [--rejudge] [--regrade] [--days N]  # PS-188
    photonscript integrate --target T [--rig piggyback|rc16] [--since D] [--out DIR]  # PS-22
    photonscript qa-focuser-zero [--since D] [--rig R] [--library P] [--apply] [--json]  # FOCPOS 0
    photonscript integrate-report [--dry-run]                     # PS-33 ledger queue
    photonscript integrate-watch [--once] [--dry-run]             # PS-31
    photonscript blend --target T [--osc P] [--rc16 P] [--stage linear|final] [--mode l|nb|auto]  # PS-153
    photonscript finish --master P --name N [--target T] [--out DIR] [--dry-run]  # re-finish one master
    photonscript pixinsight-gaia-check [--fix]                   # Gaia DR3 / DR3SP paths per PixInsight slot
    photonscript ledger-import <path> [--variant v4b] [--dry-run] [--apply]  # PS-142
    photonscript focus-ingest [--local] [--dir D] [--json]       # PS-76
    photonscript focus-move <filter> [--dry-run]                  # PS-76
    photonscript reentry-gate af|slew|center --label T [--filter L] [--ra H --dec D]  # 2026-10-15
    photonscript focus-table [--rig rc16] [--temp T] [--payoff] [--local] [--json]  # focus table
    photonscript supervise [--mode full]      # keep it running (PS-44)
    photonscript self-update [--dry-run]      # staged, smoke-checked pull (PS-58)
    photonscript stop | restart
    photonscript notify "message"
    photonscript monitor [--url http://host:8100] [--grep cooler] [--level warning]
    photonscript remote [--url U] [--no-wait] [--confirm-write] -- <command> [flags]  # remote CLI
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

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


@app.callback()
def _cli_root() -> None:
    """PhotonScript: Remote Telescope Orchestration Platform."""
    _pin_iers_offline()


def _pin_iers_offline() -> None:
    """PS-62: every CLI command (not only `start`, which PS-55 covers) pins
    astropy IERS offline, so a scope-PC command run under an account that
    cannot write ~/.cache/astropy never retries the finals2000A.all download
    (the WinError 5 IERSWarning). Honors PS_IERS_OFFLINE; never fails a
    command."""
    try:
        from photonscript.shared.config import PhotonScriptConfig
        offline = bool(getattr(PhotonScriptConfig(), "iers_offline", True))
    except Exception:  # noqa: BLE001
        offline = True
    if not offline:
        return
    try:
        from photonscript.shared import health
        health.configure_astropy_iers(offline=True, probe=False)
    except Exception:  # noqa: BLE001
        pass


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

    from photonscript.shared.localtime import local_evening_utc, night_window
    if date:
        # a local evening date; the planner takes an instant inside it
        dt = datetime.strptime(date, "%Y-%m-%d")
        at = local_evening_utc(config, dt)
        twilight = get_twilight_times(obs, dt)
    else:
        # the local night (noon to noon), not the UTC date (tomorrow from
        # 18:00 MDT)
        at = datetime.utcnow()
        dt, twilight = night_window(config, at)

    if month == 0:
        month = dt.month

    seasonal = get_seasonal_targets(month)
    projects = [create_project_from_target(t) for t in seasonal]
    sequence = plan_night_sequence(projects, config, at)

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
    guided: bool = typer.Option(False, help="Guided run (default: unguided, Paramount MX, TPoint + ProTrack)"),
    guiding: str = typer.Option("", help="guided | unguided (alias encoders); overrides --guided"),
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
    if guiding:
        from photonscript.scheduler.armer import norm_guiding_mode
        mode = norm_guiding_mode(guiding)
        if mode is None:
            console.print(f"[red]Unknown --guiding {guiding!r}: use guided or unguided.[/red]")
            raise typer.Exit(2)
        guided = mode == "guided"

    seasonal = get_seasonal_targets(month)
    projects = [create_project_from_target(t) for t in seasonal]
    targets = plan_night_sequence(projects, config, now_dt)
    for t in targets:
        t.start_guiding = guided
    from photonscript.scheduler.target_planner import cap_unguided
    cap_unguided(targets, getattr(config, "unguided_max_exposure_s", 300))  # PS-66
    seq = build_sequence_for_night(f"PhotonScript_{now_dt.strftime('%Y%m%d')}", targets)
    seq.wait_until_local = None if now else "00:00:00"  # flag: gate on dusk providers

    if fmt == "xml":
        content = generate_nina_xml(seq)
        default_path = f"PhotonScript_{now_dt.strftime('%Y%m%d')}.xml"
    else:
        from photonscript.scheduler.nina_sequence_json import generate_nina_json
        from photonscript.scheduler.sequence_lint import lint as lint_seq, format_result

        u_dither = (not guided) and bool(getattr(config, "unguided_dither", False))
        content = generate_nina_json(seq, unguided_dither=u_dither)
        default_path = f"PhotonScript_{now_dt.strftime('%Y%m%d')}.json"

        # Lint gate — refuse to write a sequence that would fail at 3 AM
        result = lint_seq(json.loads(content), guided=guided, unguided_dither=u_dither)
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


@app.command("phd2-selftest")
def phd2_selftest(
    slot: str = typer.Argument("auto", help="twilight | target | auto (from the "
                                            "armed night's dusk)"),
    from_nina: bool = typer.Option(False, "--from-nina",
                                   help="Called by NINA's ExternalScript slot: "
                                        "always exit 0 so NINA never stalls"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
):
    """PS-92: run the pulse-path self-test through the PhotonScript service
    (POST /api/phd2/selftest/run) and print the verdict. NINA runs this via
    deploy\\phd2-selftest.cmd; the service skips it when tonight already
    passed on this pier side, or (from NINA) when phd2_selftest_enabled is
    off."""
    import json as _json
    import urllib.parse
    import urllib.request
    q = urllib.parse.urlencode({"context": "nina" if from_nina else "manual",
                                "slot": slot})
    try:
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        timeout = float(getattr(cfg, "selftest_timeout_s", 240) or 240) + 60
    except Exception:  # noqa: BLE001
        timeout = 300.0
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/phd2/selftest/run?" + q,
                                     data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rep = _json.loads(r.read().decode("utf-8"))
        print(f"pulse self-test: {rep.get('verdict')} "
              + "; ".join(rep.get("reasons") or []))
    except Exception as e:  # noqa: BLE001
        print(f"pulse self-test not run: {e}")
        rep = {"verdict": "INCONCLUSIVE"}
    if from_nina:
        raise typer.Exit(0)
    raise typer.Exit(0 if rep.get("verdict") in ("PASS", "WARN", "SKIPPED") else 1)


@app.command("focus-ingest")
def focus_ingest_cmd(
    reports_dir: str = typer.Option("", "--dir", help="AF reports folder "
                                    "(default: nina_autofocus_reports_dir)"),
    local: bool = typer.Option(False, "--local",
                               help="Ingest in this process instead of "
                                    "asking the running service"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
    as_json: bool = typer.Option(False, "--json", help="Print the raw result"),
):
    """PS-76: read NINA's autofocus reports into the RC16 / Piggy-600 focus
    models now (POST /api/focus/ingest; --local runs it here against this
    checkout's config). Re-reading is safe: stored points are de-duplicated,
    so this also backfills every report still in the folder."""
    import json as _json
    import urllib.parse
    import urllib.request
    if local:
        from photonscript.scheduler.focus_model import ingest_af_reports
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        res = ingest_af_reports(cfg, reports_dir or None, "cli")
    else:
        q = urllib.parse.urlencode({"dir": reports_dir, "trigger": "cli"})
        try:
            req = urllib.request.Request(
                url.rstrip("/") + "/api/focus/ingest?" + q, data=b"",
                method="POST")
            with urllib.request.urlopen(req, timeout=300) as r:
                res = _json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"focus ingest: service not reachable ({e}); try --local")
            raise typer.Exit(1)
    if as_json:
        print(_json.dumps(res, indent=1))
    elif not res.get("enabled"):
        print("focus ingest: nina_autofocus_reports_dir is not set "
              "(or pass --dir)")
    else:
        pb = res.get("piggyback") or {}
        print(f"focus ingest from {res.get('dir')}: {res.get('files', 0)} "
              f"file(s), {res.get('parse_errors', 0)} unparseable, "
              f"{res.get('read', 0)} report(s); RC16 +{res.get('added', 0)} "
              f"({res.get('total', 0)} stored, {res.get('rejected', 0)} "
              f"rejected), Piggy-600 +{pb.get('added', 0)} "
              f"({pb.get('total', 0)} stored), {res.get('unclassified', 0)} "
              "unclassified")
        for why, n in (res.get("reject_reasons") or {}).items():
            print(f"  rejected {n}x: {why}")
        if res.get("error"):
            print(f"  error: {res['error']}")
    raise typer.Exit(1 if res.get("error") else 0)


@app.command("focus-table")
def focus_table_cmd(
    rig: str = typer.Option("rc16", "--rig", help="Rig (the table is RC16 "
                            "only)"),
    temp: float = typer.Option(None, "--temp", help="Temperature (C); "
                               "default: the first AF temperature of the "
                               "last night"),
    payoff: bool = typer.Option(False, "--payoff",
                                help="Also replay last week's AFs: AF "
                                     "minutes drive mode would have saved"),
    nights: int = typer.Option(7, "--nights", help="Payoff: nights back"),
    local: bool = typer.Option(False, "--local",
                               help="Compute it here instead of asking the "
                                    "running service"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
    as_json: bool = typer.Option(False, "--json", help="Print the raw table"),
):
    """Focus table: per filter (L R G B Ha OIII SII) the RC16 focuser
    position at a temperature, position = intercept_L + slope x T + offset
    (offsets from same-night AF pairs, else the model, else
    focus_filter_offsets), its confidence, and which filters
    focus_model_drive "drive" may move to the table instead of running AF.
    Read-only (GET /api/focus/table; --local reads this checkout's data)."""
    import json as _json
    import urllib.parse
    import urllib.request
    from photonscript.scheduler import focus_table as ft
    if (rig or "rc16").strip().lower() != "rc16":
        print("focus table: RC16 only (the Piggy-600 has one channel; see "
              "/api/focus piggyback.model)")
        raise typer.Exit(2)
    if local:
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        tab = ft.build_table(cfg, temp=temp)
        if payoff:
            tab["payoff"] = ft.payoff(cfg, nights=max(1, nights))
    else:
        q = {"rig": "rc16", "payoff": "true" if payoff else "false",
             "nights": str(max(1, nights))}
        if temp is not None:
            q["temp"] = str(temp)
        try:
            with urllib.request.urlopen(
                    url.rstrip("/") + "/api/focus/table?"
                    + urllib.parse.urlencode(q), timeout=60) as r:
                tab = _json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"focus table: service not reachable ({e}); try --local")
            raise typer.Exit(1)
    if as_json:
        print(_json.dumps(tab, indent=1))
    else:
        print(ft.format_table(tab, tab.get("payoff")))
    raise typer.Exit(1 if tab.get("error") else 0)


@app.command("focus-move")
def focus_move_cmd(
    filter_name: str = typer.Argument("L", help="Filter the block images in"),
    from_nina: bool = typer.Option(False, "--from-nina",
                                   help="Called by NINA's ExternalScript item "
                                        "(deploy\\focus-model-move.cmd)"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Say where it would move, do not move"),
    trigger: bool = typer.Option(False, "--trigger",
                                 help="Run by the block's temperature trigger: "
                                      "the service holds a re-fire within the "
                                      "step of the last table move"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
):
    """PS-76 part 2: move the RC16 focuser to the focus-model lookup-table
    position for this filter at the focuser's current temperature (POST
    /api/focus/model-move). Only moves with focus_model_drive on and the
    model trusted. Always exits 0 from NINA: a failed move never stops
    imaging."""
    import json as _json
    import urllib.parse
    import urllib.request
    q = urllib.parse.urlencode({"filter": filter_name,
                                "dry_run": "true" if dry_run else "false",
                                "trigger": "true" if trigger else "false"})
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/focus/model-move?" + q, data=b"",
            method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            rep = _json.loads(r.read().decode("utf-8"))
        print(f"focus model move ({filter_name}): {rep.get('verdict')} "
              f"{rep.get('position', '')} - {rep.get('reason', '')}")
    except Exception as e:  # noqa: BLE001
        print(f"focus model move not run: {e}")
        rep = {"verdict": "ERROR"}
    if from_nina:
        raise typer.Exit(0)
    raise typer.Exit(0 if rep.get("verdict") != "ERROR" else 1)


@app.command("cooler-gate")
def cooler_gate_cmd(
    rig: str = typer.Argument("rc16", help="rc16 | piggyback"),
    setpoint: Optional[float] = typer.Option(
        None, "--setpoint", help="Setpoint (C) the sequence cooled to "
                                 "(default: the rig's configured setpoint)"),
    label: str = typer.Option("", "--label", help="Target + filter, for the "
                                                  "messages"),
    from_nina: bool = typer.Option(False, "--from-nina",
                                   help="Called by NINA's ExternalScript item "
                                        "(deploy\\cooler-gate.cmd)"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
):
    """PS-61: hold until the rig's sensor is within cooler_gate_tolerance_c
    of the setpoint (POST /api/cooler/gate), at most cooler_gate_timeout_min.
    Exit 3 ONLY when the gate says SKIP (deploy\\cooler-gate.cmd turns that
    into exit 1, and NINA skips the light block); exit 0 on a pass, in warn
    mode, and on any error (the gate fails open)."""
    import json as _json
    import urllib.parse
    import urllib.request
    from photonscript.scheduler.cooler_gate import EXIT_SKIP
    q = {"rig": rig, "label": label}
    if setpoint is not None:
        q["setpoint"] = f"{setpoint:g}"
    try:
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        timeout = float(getattr(cfg, "cooler_gate_timeout_min", 20.0)) * 60 + 120
    except Exception:  # noqa: BLE001
        timeout = 1320.0
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/cooler/gate?" + urllib.parse.urlencode(q),
            data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rep = _json.loads(r.read().decode("utf-8"))
        print(f"cooler gate ({rig}{' ' + label if label else ''}"
              f"{', from NINA' if from_nina else ''}): {rep.get('verdict')} "
              f"- {rep.get('reason', '')}")
    except Exception as e:  # noqa: BLE001
        print(f"cooler gate not run ({e}); imaging anyway")
        rep = {"verdict": "UNKNOWN"}
    raise typer.Exit(EXIT_SKIP if rep.get("verdict") == "SKIP" else 0)


@app.command("reentry-gate")
def reentry_gate_cmd(
    kind: str = typer.Argument("af", help="af | slew | center"),
    label: str = typer.Option("", "--label", help="The target"),
    filter_name: str = typer.Option("", "--filter",
                                    help="af: the filter the AF would run in"),
    ra: Optional[float] = typer.Option(None, "--ra", help="slew / center: "
                                       "target RA (J2000 hours)"),
    dec: Optional[float] = typer.Option(None, "--dec", help="slew / center: "
                                        "target Dec (J2000 deg)"),
    from_nina: bool = typer.Option(False, "--from-nina",
                                   help="Called by NINA's ExternalScript item "
                                        "(deploy\\reentry-gate.cmd)"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
):
    """2026-10-15 re-entry gate: ask the service whether a target's start
    AF / slew / center can be skipped (POST /api/reentry/gate). Exit 3 ONLY
    when it says SKIP (deploy\\reentry-gate.cmd turns that into exit 1 and
    NINA skips those items); exit 0 on RUN and on any error (the gate fails
    open: the step runs)."""
    import json as _json
    import urllib.parse
    import urllib.request
    from photonscript.scheduler.reentry import EXIT_SKIP
    q = {"kind": kind, "label": label}
    if filter_name:
        q["filter"] = filter_name
    if ra is not None and dec is not None:
        q["ra"], q["dec"] = f"{ra:.6f}", f"{dec:.6f}"
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/reentry/gate?" + urllib.parse.urlencode(q),
            data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            rep = _json.loads(r.read().decode("utf-8"))
        print(f"re-entry gate {kind} ({label}{', from NINA' if from_nina else ''}"
              f"): {rep.get('verdict')} - {rep.get('why', '')}")
    except Exception as e:  # noqa: BLE001
        print(f"re-entry gate not run ({e}); running the step")
        rep = {"verdict": "RUN"}
    raise typer.Exit(EXIT_SKIP if rep.get("verdict") == "SKIP" else 0)


def _cover_fallback(cfg, rig: str) -> dict:
    """PS-189: the service is down: read NINA #2's flat device directly
    (one read-only GET). PASS only when the cover reads Open."""
    import asyncio as _asyncio
    from photonscript.shared.rigs import (cover_enabled, cover_state, rig_config,
                                          nina_flatdevice_info)
    if cfg is None or not cover_enabled(cfg, rig):
        return {"verdict": "OFF", "reason": "cover not enabled"}
    info = _asyncio.run(nina_flatdevice_info(rig_config(cfg, rig).nina_base_url))
    st = cover_state(info)
    if st == "open" and (info or {}).get("LightOn") is not True:
        return {"verdict": "PASS", "reason": "cover open (read directly)"}
    return {"verdict": "SKIP", "reason": f"cover reads {st or 'unreadable'} "
                                         "(read directly, service down)"}


@app.command("cover-gate")
def cover_gate_cmd(
    rig: str = typer.Argument("piggyback", help="piggyback"),
    label: str = typer.Option("", "--label", help="For the messages"),
    from_nina: bool = typer.Option(False, "--from-nina",
                                   help="Called by NINA #2's ExternalScript "
                                        "item (deploy\\cover-gate.cmd)"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
):
    """PS-189: hold until the Piggy-600's Wanderer Cover reads Open (POST
    /api/cover/gate), at most piggyback_cover_gate_timeout_s. Exit 3 when the
    gate says SKIP (deploy\\cover-gate.cmd turns that into exit 1 and NINA
    skips the image pass). Unlike the other gates it does not fail open:
    with the service unreachable it reads NINA #2's flat device itself and
    skips unless the cover reads Open. With the cover disabled it exits 0."""
    import json as _json
    import urllib.parse
    import urllib.request
    from photonscript.scheduler.flat_cover import EXIT_SKIP
    cfg = None
    try:
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        timeout = float(getattr(cfg, "piggyback_cover_gate_timeout_s", 180.0)) + 120
    except Exception:  # noqa: BLE001
        timeout = 300.0
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/cover/gate?"
            + urllib.parse.urlencode({"rig": rig, "label": label}),
            data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rep = _json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"cover gate service unreachable ({e}); reading NINA #2 directly")
        try:
            rep = _cover_fallback(cfg, rig)
        except Exception as e2:  # noqa: BLE001
            rep = {"verdict": "SKIP", "reason": f"cover unreadable: {e2}"}
    print(f"cover gate ({rig}{' ' + label if label else ''}"
          f"{', from NINA' if from_nina else ''}): {rep.get('verdict')} "
          f"- {rep.get('reason', '')}")
    raise typer.Exit(EXIT_SKIP if rep.get("verdict") == "SKIP" else 0)


@app.command("settle-gate")
def settle_gate_cmd(
    label: str = typer.Option("", "--label", help="For the log"),
    from_nina: bool = typer.Option(False, "--from-nina",
                                   help="Called by NINA #2's ExternalScript "
                                        "item (deploy\\settle-gate.cmd)"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
    resume: bool = typer.Option(False, "--resume",
                                help="PS-25: hold until NINA #1 exposes again "
                                     "after a resume (the OSC image pass)"),
):
    """PS-27: hold the Piggy-600's next OSC light until the RC16 mount is
    still and PHD2 is not settling (POST /api/piggyback/settle-gate), at
    most piggyback_settle_timeout_s; PS-158: and while the mount is parked
    or not tracking, at most piggyback_tracking_hold_s. Always exits 0: the
    gate can delay a sub, never skip one, and fails open when the service
    is down. --resume (PS-25): the resume gate instead (mode=resume), at
    most piggyback_resume_wait_max_s."""
    import json as _json
    import urllib.parse
    import urllib.request
    try:
        from photonscript.scheduler.split_guard import (gate_bound_s,
                                                        resume_wait_max_s)
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        timeout = (resume_wait_max_s(cfg) if resume else gate_bound_s(cfg)) + 30
    except Exception:  # noqa: BLE001
        timeout = 1020.0
    q = {"label": label, **({"mode": "resume"} if resume else {})}
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/piggyback/settle-gate?"
            + urllib.parse.urlencode(q), data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rep = _json.loads(r.read().decode("utf-8"))
        print(f"{'resume' if resume else 'settle'} gate"
              f"{' (from NINA)' if from_nina else ''}: "
              f"{rep.get('verdict')} after {rep.get('waited_s')} s - "
              f"{rep.get('reason', '')}")
    except Exception as e:  # noqa: BLE001
        print(f"settle gate not run ({e}); shooting anyway")
    raise typer.Exit(0)


@app.command("tpoint-stats")
def tpoint_stats_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
    files: bool = typer.Option(False, "--files", help="Also list the TPoint "
                               "files found under TheSky's user folder"),
    show_history: bool = typer.Option(
        False, "--history", help="The fitted-model history the TPoint watcher "
        "keeps (date, points, RMS, popn SD, terms, IH / ID / MA / ME / CH / NP, "
        "polar error) and the unguided limits epoch; reads no TPoint file"),
):
    """PS-171: the TPoint model's numbers from TPoint's own files (TheSky64
    has no TPoint scripting object): points, sky RMS, terms, polar
    alignment (ME / MA and what to adjust), ProTrack, model date, and the
    coverage of the model's samples (pier side x HA x Dec grid, empty /
    thin cells, the top-up run to fill them). Read only.

    photonscript tpoint-stats
    photonscript tpoint-stats --history
    """
    import json as _json
    from photonscript.scheduler import tpoint_files as tf
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if show_history:
        from photonscript.scheduler import tpoint_watch as tw
        recs = tw.history(cfg)
        if as_json:
            print(_json.dumps({"models": recs, "epoch": tw.load_epoch(cfg),
                               "watch": tw.load_state(cfg)}, indent=2, default=str))
        else:
            console.print(tw.format_history(cfg, recs), markup=False, highlight=False)
            st = tw.load_state(cfg)
            if st:
                console.print("  watcher: " + tw.status_text(cfg, st), markup=False,
                              highlight=False)
        raise typer.Exit(0)
    from photonscript.scheduler import tpoint_coverage as tc
    lst = tf.list_files(cfg)
    st = tf.stats(cfg, lst)
    tf.save(cfg, st)
    try:   # where on the sky the base model has samples
        cov = tc.from_stats(cfg, st)
    except Exception as e:  # noqa: BLE001
        cov = {"ok": False, "note": f"{type(e).__name__}: {e}"}
    if as_json:
        print(_json.dumps({**st, "coverage": cov}, indent=2, default=str))
    else:
        console.print(tf.format_stats(st), markup=False, highlight=False)
        if cov:
            console.print(tc.format_text(cov), markup=False, highlight=False)
    if files:
        for r in lst["roots"]:
            print(f"root {r['path']}: {'found' if r['exists'] else 'missing'}")
        for f in lst["files"]:
            print(f"  {f['mtime']}  {f['size']:>9}  {f['rel']}")
    raise typer.Exit(0)


@app.command("tpoint-sample")
def tpoint_sample_cmd(
    file: str = typer.Argument("", help="Frame to sample (default: the newest "
                                        "FITS under image_watch_dir)"),
    rig: str = typer.Option("rc16", "--rig", help="rc16 only"),
    probe: bool = typer.Option(False, "--probe", help="READ ONLY: report which "
                               "TPoint add methods this TheSky build has, and "
                               "the model's point count / RMS"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Pick the frame and "
                                 "print the script; send nothing to TheSky"),
    point: Optional[int] = typer.Option(None, "--point"),
    of: Optional[int] = typer.Option(None, "--of"),
    alt: Optional[float] = typer.Option(None, "--alt"),
    az: Optional[float] = typer.Option(None, "--az"),
    side: str = typer.Option("", "--side"),
    add: str = typer.Option("", "--add", help="off | auto: override "
                            "tpoint_sample_add for this sample (PS-181: an "
                            "arm with the TPoint recipe sets it)"),
    from_nina: bool = typer.Option(False, "--from-nina",
                                   help="Called by NINA's ExternalScript item "
                                        "(deploy\\tpoint-sample.cmd)"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """PS-171: one TPoint sample from the newest saved frame: TheSky Image
    Link, then (PS_TPOINT_SAMPLE_ADD=auto and a probed add method) a TPoint
    sample, and always a row in runs/<night>_tpoint.csv. Never syncs or
    moves the mount. Always exits 0 (the NINA loop must never die).

    photonscript tpoint-sample --probe
    photonscript tpoint-sample --dry-run
    """
    import json as _json
    try:
        from photonscript.telescope_agent import tpoint_sample as ts
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        if probe:
            rec = ts.run_probe(cfg)
            print(_json.dumps(rec, indent=2) if as_json else ts.format_probe(rec))
            raise typer.Exit(0)
        if rig != "rc16":
            print(f"tpoint-sample: rig {rig!r} not supported (rc16 only); skipped")
            raise typer.Exit(0)
        row = ts.run_sample(cfg, file or None, point=point, of=of, alt=alt,
                            az=az, side=side, dry_run=dry_run,
                            add=add or None)
        if as_json:
            print(_json.dumps(row, indent=2, default=str))
        else:
            print(ts.format_row(row) + (" (from NINA)" if from_nina else ""))
            if dry_run:
                print(f"frame: {row.get('file')}  scale: {row.get('scale')}  "
                      f"add: {row.get('add_mode')} {row.get('add_method') or ''}")
                print(f"add call: {row.get('add_call') or '(none)'}  "
                      f"[{row.get('add_call_note') or ''}]")
                print(row.get("script") or "")
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"tpoint-sample failed ({type(e).__name__}: {e}); continuing")
    raise typer.Exit(0)


@app.command("tpoint-autocal")
def tpoint_autocal_cmd(
    points: int = typer.Option(30, "--points", help="Pointing samples "
                               "(6..500; a first supervised run: small)"),
    mode: str = typer.Option("loop", "--mode", help="loop (PhotonScript "
                             "slews / exposes / AutoMaps each point) | window "
                             "(open TheSky's Automated Pointing Calibration, "
                             "the operator starts it)"),
    exposure_s: Optional[float] = typer.Option(None, "--exposure-s"),
    binning: Optional[int] = typer.Option(None, "--binning"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan, "
                                 "the preflight and every script; send nothing"),
    go: bool = typer.Option(False, "--go", help="The live run (loop mode "
                            "only; operator at the console; untested on "
                            "hardware: start with --test)"),
    side: str = typer.Option("both", "--side", help="east | west | both "
                             "(which side of the meridian to sample)"),
    from_coverage: bool = typer.Option(False, "--from-coverage", help="Pick "
                                       "the points from the TPoint coverage "
                                       "module when this build has one, else "
                                       "the grid + --side"),
    test: bool = typer.Option(False, "--test", help="Supervised first test: "
                              "--points 3 --side west, verbose, and a pause "
                              "after point 1 to check TPoint by eye"),
    verbose: bool = typer.Option(False, "--verbose", help="Print every "
                                 "TheSky script call and reply"),
    slew_timeout: float = typer.Option(180.0, "--slew-timeout",
                                       help="Seconds before a slew counts "
                                       "as stuck (abort + stop)"),
    status: bool = typer.Option(False, "--status", help="Dry run: also read "
                                "the scheduler's GET /api/arm and GET "
                                "/api/apps/status for the preflight"),
    url: str = typer.Option("http://localhost:8100", "--url",
                            help="Scheduler for --status"),
    scripts: bool = typer.Option(True, "--scripts/--no-scripts",
                                 help="Print each step's TheSky script"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """TPoint autocal: grow the TPoint model with TheSky's own camera,
    operator-started and supervised (docs/TPOINT_AUTOCAL.md). --dry-run
    prints the preflight, the points, the NINA camera hand-off and every
    TheSky script, and sends nothing. --go --mode loop runs it live (never
    arms, never syncs, never parks; Ctrl+C stops and gives the camera back).

    photonscript tpoint-autocal --side west --points 20 --dry-run
    photonscript tpoint-autocal --go --test
    photonscript tpoint-autocal --go --side west --points 20
    """
    import json as _json
    from datetime import datetime as _dt
    from photonscript.telescope_agent import tpoint_autocal as ta
    if dry_run and go:
        console.print("pick one of --dry-run and --go", markup=False)
        raise typer.Exit(2)
    if mode not in ta.MODES:
        console.print(f"--mode must be one of {', '.join(ta.MODES)}", markup=False)
        raise typer.Exit(2)
    if go and mode != "loop":
        try:
            ta.run_go()
        except NotImplementedError as e:
            console.print(str(e), markup=False)
            raise typer.Exit(2)
    from photonscript.telescope_agent import tpoint_autocal_live as tal
    if side not in tal.SIDES:
        console.print(f"--side must be one of {', '.join(tal.SIDES)}", markup=False)
        raise typer.Exit(2)
    if test:
        points, side = tal.TEST_POINTS, tal.TEST_SIDE
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if mode == "loop":
        _now = _dt.utcnow()
        _exp = float(exposure_s if exposure_s is not None else
                     getattr(cfg, "tpoint_mapping_exposure_s", 5.0) or 5.0)
        sel = tal.select_points(cfg, points, side, _now, _exp,
                                tal.dark_end(cfg, _now),
                                from_coverage=from_coverage)
    if go:
        say = lambda m: console.print(m, markup=False, highlight=False)  # noqa: E731
        say("TPoint autocal LIVE, loop mode" + (" (TEST: 3 west points)" if test else "")
            + ". It will borrow the scope and NINA #1's camera; it never arms, "
            "syncs or parks.")
        say(tal.format_points(sel))
        if not sel["points"]:
            say("No usable points: nothing to do.")
            raise typer.Exit(1)
        b = int(binning if binning is not None else
                getattr(cfg, "tpoint_mapping_binning", 2) or 2)
        opts = tal.Opts(exposure_s=exposure_s, binning=b,
                        image_scale_per_bin1=float(getattr(cfg, "pixel_scale_arcsec", 0.236)
                                                   or 0.236),
                        test=test, verbose=verbose or test,
                        slew_timeout_s=slew_timeout,
                        csv_path=tal.csv_path(cfg, _now),
                        auto_arm_enabled=bool(getattr(cfg, "auto_arm_enabled", False)),
                        auto_dusk_flats=bool(getattr(cfg, "auto_dusk_flats", False)),
                        min_alt=float(sel["min_alt"]))
        res = tal.run_loop(cfg, sel["points"], tal.live_deps(cfg, url, say=say), opts)
        if as_json:
            print(_json.dumps({k: v for k, v in res.items() if k != "rows"},
                              indent=2, default=str))
        raise typer.Exit(0 if res["status"] == "done" else 1)
    arm = apps = None
    if status:   # read-only GETs on PhotonScript's own scheduler
        import httpx
        base = url.rstrip("/")
        try:
            arm = httpx.get(base + "/api/arm", timeout=10).json()
        except Exception as e:  # noqa: BLE001
            console.print(f"GET /api/arm failed ({e})", markup=False)
        try:
            apps = httpx.get(base + "/api/apps/status", timeout=30).json()
        except Exception as e:  # noqa: BLE001
            console.print(f"GET /api/apps/status failed ({e})", markup=False)
    backup = None
    try:
        from photonscript.scheduler import tpoint_files as tf
        backup = ta.backup_plan(tf.list_files(cfg), _dt.utcnow())
    except Exception as e:  # noqa: BLE001
        console.print(f"TPoint file listing failed ({e})", markup=False)
    pre = ta.preflight(arm, apps, None, backup,
                       auto_arm_enabled=bool(getattr(cfg, "auto_arm_enabled", False)),
                       auto_dusk_flats=bool(getattr(cfg, "auto_dusk_flats", False)))
    plan = ta.build_plan(cfg, points=points, mode=mode, exposure_s=exposure_s,
                         binning=binning,
                         sky_points=sel["points"] if mode == "loop" else None)
    if as_json:
        print(_json.dumps({"dry_run": True, "preflight": pre, "backup": backup,
                           "plan": plan, "selection": sel if mode == "loop" else None},
                          indent=2, default=str))
    else:
        console.print(ta.format_plan(plan, pre, show_scripts=scripts),
                      markup=False, highlight=False)
        if mode == "loop":
            console.print("Points for --go:\n" + tal.format_points(sel),
                          markup=False, highlight=False)
    raise typer.Exit(0)


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
    date: str = typer.Option("", help="Night (YYYY-MM-DD, the runs page date)"),
    apply: bool = typer.Option(False, "--apply",
                               help="Write the changes (default: dry run)"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Report only (the default)"),
    remeasure: bool = typer.Option(
        False, "--remeasure",
        help="PS-130: first re-measure pre-PS-83 backfill records (no "
             "measure_v) from their FITS, then re-judge; human verdicts kept"),
    all_before_ps83: bool = typer.Option(
        False, "--all-before-ps83",
        help="With --remeasure: every night that still has pre-PS-83 "
             "backfill records (instead of --date)"),
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
    changed. PS-130 --remeasure: re-measure the pre-PS-83 backfill records
    from their FITS first (missing FITS are skipped and counted)."""
    import json as _json
    from pathlib import Path as _P
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.runs import rescore_night

    if apply and dry_run:
        raise typer.BadParameter("--apply and --dry-run exclude each other")
    if all_before_ps83 and not remeasure:
        raise typer.BadParameter("--all-before-ps83 needs --remeasure")
    if remeasure:
        if records:
            raise typer.BadParameter("--remeasure reads the FITS, not --records")
        if bool(date) == all_before_ps83:
            raise typer.BadParameter("give --date D or --all-before-ps83")
        from photonscript.scheduler import qa_remeasure
        rep = qa_remeasure.remeasure(
            PhotonScriptConfig(), [date] if date else None, apply=apply,
            allow_unreject=allow_unreject)
        if full:
            console.print_json(_json.dumps(rep, default=str))
        else:
            print(qa_remeasure.format_report(rep))
        return
    if not date:
        raise typer.BadParameter("--date is required")
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


@app.command("qa-focuser-zero")
def qa_focuser_zero_cmd(
    since: str = typer.Option("", "--since", help="First night (evening date YYYY-MM-DD)"),
    until: str = typer.Option("", "--until", help="Last night (evening date YYYY-MM-DD)"),
    rig: str = typer.Option("", "--rig", help="rc16 | piggyback (default both)"),
    library: str = typer.Option("", "--library",
                                help="Library to scan, read only (default: the desktop mirror, "
                                     "D:/ninashare/Library or integration_library_dir)"),
    apply: bool = typer.Option(False, "--apply",
                               help="Reject the FOCPOS 0 lights in this machine's subs logs "
                                    "(the scope PC; default: dry run)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List only (the default)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """Lights shot while the focuser reported position 0 (FOCPOS 0: the
    2026-10-08 RC16 EAF off USB). Lists them from the Library (header scan,
    read only) grouped by rig, target and night, and checks this machine's
    subs logs. --apply rejects them there with reason focuser_zero (the
    scorecard row swap of the dawn passes; human verdicts kept; Library
    links move to _rejected). Nothing is posted to the scope: run --apply ON
    the scope PC.

    photonscript qa-focuser-zero --since 2026-10-08 --rig rc16
    """
    import json as _json
    from photonscript.integration import pipeline as pl
    from photonscript.scheduler import focuser_zero as fz

    if apply and dry_run:
        raise typer.BadParameter("--apply and --dry-run exclude each other")
    if rig and rig not in ("rc16", "piggyback"):
        raise typer.BadParameter("--rig must be rc16 or piggyback")
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    lib = Path(library) if library else pl.default_library(cfg)
    hits = fz.scan_library(lib, since, until, rig, tz=cfg.observatory_tz)
    groups = fz.summarize(hits)
    nights = [fz.night_pass(cfg, d, apply=apply, rig=rig)
              for d in fz.log_nights(cfg, since, until)]
    res = {"library": str(lib), "mode": "apply" if apply else "dry-run",
           "library_lights": len(hits), "groups": groups, "nights": nights}
    if as_json:
        res["library_files"] = hits
        print(_json.dumps(res, indent=1, default=str))
        return
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    say(f"Library {lib}: {len(hits)} light(s) with FOCPOS 0"
        + (f" (rig {rig})" if rig else "") + (f" since {since}" if since else ""))
    for g in groups:
        say(f"  {g['night']}  {g['rig']:<9} {g['instrument'] or '?':<8} {g['target']}: {g['n']}"
            + (f" ({g['in_rejected']} already in _rejected)" if g["in_rejected"] else ""))
    logs = [n for n in nights if not n.get("missing_log")]
    if not logs:
        say("No subs logs here for that range (run on the scope PC to judge / --apply).")
    for n in logs:
        say(f"{n['date']} subs log ({n['mode']}): {n['zero']} of {n['subs']} subs at FOCPOS 0, "
            f"{n['newly_rejected']} newly rejected, {n['already_rejected']} already rejected, "
            f"{n['human_kept']} human verdicts kept, {n['unknown_focpos']} without FOCPOS"
            + (f"; {n['records_updated']} records updated, {n['library_moves']} Library moves"
               if apply else ""))


@app.command("score-report")
def score_report_cmd(
    date: str = typer.Option(..., help="Night (YYYY-MM-DD, the runs page date)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
    records: str = typer.Option(
        "", help="Report on a copy instead: a <date>_subs.jsonl or a saved "
                 "/api/runs/<date> JSON"),
):
    """PS-108: how many subs the 0 to 100 score would approve / send to
    review / reject vs today's verdicts, per rig, plus the subs that would
    move. Re-grades the stored metrics (no FITS) and writes nothing."""
    import json as _json
    from pathlib import Path as _P
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.runs import format_score_report, score_report

    recs = None
    if records:
        text = _P(records).read_text(encoding="utf-8")
        try:
            doc = _json.loads(text)
            recs = doc["subs"] if isinstance(doc, dict) else doc
        except ValueError:
            recs = [_json.loads(x) for x in text.splitlines() if x.strip()]
    rep = score_report(PhotonScriptConfig(), date, records=recs)
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(format_score_report(rep), markup=False, highlight=False)


@app.command("qa-baselines")
def qa_baselines_cmd(
    rig: str = typer.Option("", help="rc16 or piggyback (default: both)"),
    nights: int = typer.Option(14, help="Last N nights with a subs log"),
    k: float = typer.Option(None, help="Proposed gate = median + k x sigma "
                                       "(default qa_baseline_k, 3)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
    records: str = typer.Option(
        "", help="Report on copies instead: <date>_subs.jsonl files or a "
                 "folder of them (comma list)"),
):
    """PS-114: each rig's baseline per filter (median and MAD of FWHM, HFR,
    ecc, stars, background over its accepted subs) with the proposed gate
    next to the gate in force. Writes nothing and never changes a gate."""
    import json as _json
    from pathlib import Path as _P
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.qa_baselines import baselines, format_baselines

    recs = None
    if records:
        recs = []
        files = []
        for part in records.split(","):
            p = _P(part.strip())
            files += sorted(p.glob("*_subs.jsonl")) if p.is_dir() else [p]
        for f in files[-nights:] if nights else files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = _json.loads(line)
                    r.setdefault("_night", f.name[:10])
                    recs.append(r)
    rep = baselines(PhotonScriptConfig(), rig or None, nights, k, records=recs)
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(format_baselines(rep), markup=False, highlight=False)


@app.command("ecc-scale-report")
def ecc_scale_report(
    date: list[str] = typer.Option(..., "--date",
                                   help="Night (YYYY-MM-DD, the runs page "
                                        "date); repeat for several"),
    gate: list[float] = typer.Option(
        [0.60, 0.70], "--gate", help="Eccentricity gates to count (repeat)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-94: eccentricity at the native 0.236"/px vs 2x2-binned 0.47"/px for
    every RC16 light of a night, same pipeline and formula at both scales:
    medians per target + filter, pass counts per gate, subs that would flip,
    and which grader produced the stored numbers. Dry run: writes
    <data_dir>/reports/ps94_<date>.json only. Full-frame work: run it in
    daytime on the scope PC (the API refuses while armed)."""
    import json as _json
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.ecc_scale import compare_night, format_report
    cfg = PhotonScriptConfig()
    reps = [compare_night(cfg, d, gates=tuple(gate)) for d in date]
    if as_json:
        print(_json.dumps(reps if len(reps) > 1 else reps[0], indent=1,
                          default=str))
    else:
        console.print(format_report(reps), markup=False, highlight=False)
    raise typer.Exit(0 if all(r["n_measured"] for r in reps) else 1)


@app.command("calibration-plan")
def calibration_plan_cmd(
    rig: str = typer.Option("", "--rig", help="rc16 | piggyback (default: every rig)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-113: calibration needs vs QA-passed frames per rig (darks per
    exposure from config, active goals, tonight's plan and the Library's
    lights; bias; flats per filter as needs only) and what a capture job
    would shoot. Read only.

    photonscript calibration-plan --rig piggyback
    """
    import json as _json
    from photonscript.scheduler.calibration_plan import format_report, gap_report
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    rep = gap_report(cfg, rig or None)
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(format_report(rep), markup=False, highlight=False)


@app.command("calibration-owed")
def calibration_owed_cmd(
    rig: str = typer.Option("", "--rig", help="rc16 | piggyback (default: every rig)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-122: which calibration frames each rig still owes for the lights of
    active goals (last calibration_owed_lookback_days nights) and tonight's
    plan: darks per epoch with the night quota's own count, flats per filter,
    bias, uncalibrated nights and config fixes. Read only.

    photonscript calibration-owed --rig piggyback
    """
    import json as _json
    from photonscript.scheduler.calibration_owed import format_report, owed_report
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    rep = owed_report(cfg, rig or None)
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(format_report(rep), markup=False, highlight=False)


@app.command("calibration-capture")
def calibration_capture_cmd(
    rig: str = typer.Option(..., "--rig", help="rc16 | piggyback"),
    exposures: str = typer.Option("", "--exposures",
                                  help="Dark exposures (s, comma list), e.g. 300,400; "
                                       "default: the calibration-plan gap"),
    count: Optional[int] = typer.Option(None, "--count",
                                        help="Darks per exposure (default: the quota "
                                             "or the gap)"),
    bias: Optional[int] = typer.Option(None, "--bias",
                                       help="Bias frames (default: 0 with --exposures, "
                                            "else the gap)"),
    budget: Optional[float] = typer.Option(None, "--budget-min",
                                           help="Time budget (min, cooling included)"),
    cancel: bool = typer.Option(False, "--cancel", help="Cancel the rig's running job"),
    follow: bool = typer.Option(True, "--follow/--no-follow",
                                help="Print progress until the job ends"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url",
                            help="The running PhotonScript service"),
):
    """PS-113: start a guarded darks + bias capture job on one rig's own NINA
    through the PhotonScript service (POST /api/calibration/capture-job).
    Refused unless the armer is DISARMED or COMPLETE, the rig's NINA is idle,
    the roof reads closed and (RC16) PHD2 is idle; cools to the setpoint
    first; stops if the roof opens or the sensor drifts. Ctrl+C stops
    following, not the job (use --cancel).

    photonscript calibration-capture --rig piggyback --exposures 300,400 --count 30
    """
    import httpx
    base = url.rstrip("/")
    if cancel:
        r = httpx.post(base + "/api/calibration/capture-job/cancel",
                       json={"rig": rig}, timeout=30)
        console.print(r.json(), markup=False)
        raise typer.Exit(0 if r.status_code == 200 else 1)
    body = {"rig": rig, "source": "cli"}
    if exposures:
        body["exposures"] = [float(x) for x in exposures.split(",") if x.strip()]
    if count is not None:
        body["count"] = count
    if bias is not None:
        body["bias"] = bias
    if budget is not None:
        body["budget_min"] = budget
    try:
        r = httpx.post(base + "/api/calibration/capture-job", json=body, timeout=180)
    except Exception as e:  # noqa: BLE001
        console.print(f"service unreachable ({e}); nothing started", markup=False)
        raise typer.Exit(2)
    d = r.json()
    if r.status_code != 200:
        console.print("REFUSED:", markup=False)
        for x in d.get("refusals") or [d.get("detail")]:
            console.print(f"  - {x}", markup=False)
        raise typer.Exit(1)
    plan = " + ".join(f"{e:g} s x {n}" for e, n in d["darks"])
    if d.get("bias"):
        plan += f" + {d['bias']} bias"
    console.print(f"started job {d['id']} on {rig}: {plan}, about "
                  f"{d['estimated_minutes']:.0f} min + cooling (budget "
                  f"{d['budget_min']:g} min)", markup=False)
    if not follow:
        return
    import time as _time
    last = None
    try:
        while True:
            _time.sleep(30)
            try:
                st = httpx.get(base + "/api/calibration/capture-job", timeout=30).json()
            except Exception:  # noqa: BLE001
                continue
            j = (st.get("jobs") or {}).get(rig) or {}
            line = f"{j.get('state')}: {j.get('detail')}"
            if line != last:
                console.print(f"{datetime.now():%H:%M} {line}", markup=False)
                last = line
            if j.get("state") not in ("starting", "cooling", "running", "stopping"):
                console.print(f"verdicts {j.get('verdicts')}", markup=False)
                for b in j.get("bad") or []:
                    console.print(f"  quarantined {b['file']}: {b['reasons']}",
                                  markup=False)
                raise typer.Exit(0 if j.get("state") == "complete" else 1)
    except KeyboardInterrupt:
        console.print("stopped following; the job keeps running "
                      "(--cancel to stop it)", markup=False)


@app.command("calibration-qa")
def calibration_qa_cmd(
    backfill: bool = typer.Option(False, "--backfill",
                                  help="QA every calibration frame in the watch dir "
                                       "and the Library (else: show the stored verdicts)"),
    rig: str = typer.Option("", "--rig", help="rc16 | piggyback (default: every rig)"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Move nothing: list what would be quarantined"),
    recheck: bool = typer.Option(False, "--recheck", help="Re-measure cached frames"),
    restore: bool = typer.Option(False, "--restore",
                                 help="Move the rig's quarantined frames back "
                                      "(false-positive escape hatch; needs --rig)"),
    reset_daytime: bool = typer.Option(False, "--reset-daytime",
                                       help="Re-allow daytime capture on --rig after "
                                            "fixing a light leak"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
    url: str = typer.Option("http://localhost:8100", "--url",
                            help="Scheduler to ask for the armer state"),
):
    """PS-113: calibration frame QA. --backfill reads every BIAS / DARK / FLAT
    frame (temperature, header, level vs bias, light leak, stars, set
    outliers; flats: level band, saturation, vignetting) and, unless
    --dry-run, moves the failing Library links to Calibration/_quarantine/
    with the reasons. NINA's originals are never touched. Refused while a
    night is RUNNING (it competes with the grader).

    photonscript calibration-qa --backfill --dry-run
    """
    import json as _json
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.shared.rigs import rig_ids
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if reset_daytime:
        if not rig:
            console.print("--reset-daytime needs --rig", markup=False)
            raise typer.Exit(2)
        console.print(cq.set_daytime_state(cfg, rig, "untested", note="reset by hand"),
                      markup=False)
        return
    if restore:
        if not rig:
            console.print("--restore needs --rig", markup=False)
            raise typer.Exit(2)
        console.print_json(_json.dumps(cq.restore(cfg, rig, dry_run=dry_run)))
        return
    if backfill:
        try:
            import httpx
            state = str(httpx.get(url.rstrip("/") + "/api/arm", timeout=10).json()
                        .get("state") or "")
        except Exception:  # noqa: BLE001
            state = ""  # service down: nothing is imaging through it
        if state in ("RUNNING", "PAUSED_UNSAFE", "WATCHING",   # PS-136
                     "PAUSED_OPERATOR"):                      # PS-64
            console.print(f"armer is {state}: run the backfill in the day", markup=False)
            raise typer.Exit(2)

        def _prog(i, n, k):
            if i == 1 or i % 25 == 0 or i == n:
                console.print(f"  measuring {i}/{n} {k}", markup=False)

        rep = cq.backfill(cfg, rig or None, dry_run=dry_run, recheck=recheck,
                          progress=None if as_json else _prog)
    else:
        rep = {"dry_run": None, "mode": cq.mode(cfg), "rigs": {}}
        for rg in ([rig] if rig else rig_ids(cfg)):
            store = cq.load_store(cfg, rg)
            rep["rigs"][rg] = {**cq.summarize(store["frames"].values()),
                               "daytime": store.get("daytime")}
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
        return
    console.print(cq.format_report(rep, cfg), markup=False, highlight=False)


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


@app.command("sub-doctor")
def sub_doctor_cmd(
    night: str = typer.Option("", "--night", help="Night (YYYY-MM-DD, the evening "
                              "date); default the night that started last"),
    rig: str = typer.Option("", "--rig", help="rc16 | piggyback (default both)"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Read a running PhotonScript instead (GET "
                                 "/api/runs/<night> and /api/phd2/calibration)"),
    nina_events: bool = typer.Option(False, "--nina-events",
                                     help="With --url: also read both NINAs' event "
                                          "history (:1888 / :1889, AF finish times; "
                                          "NINA keeps only its current session)"),
    timeline: bool = typer.Option(False, "--timeline",
                                  help="Print every diagnosis, not only the first of each cause"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """Sub doctor over a past night: replays the night's graded subs as the
    live doctor would (rolling window, both rigs paired) and prints, per rig,
    each cause the first time it would have fired, with the evidence and the
    fix. Read only: no push, no remedy, nothing written.

    photonscript sub-doctor --night 2026-10-12 --url http://100.94.189.77:8100
    """
    import json as _json
    from photonscript.scheduler import sub_doctor as sd
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    d = night or _last_night()
    if url:
        import urllib.parse
        import urllib.request

        def _get(u, timeout=170):
            with urllib.request.urlopen(u, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8"))
        base = url.rstrip("/")
        try:
            run = _get(f"{base}/api/runs/{d}")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Could not read {base}/api/runs/{d}: {e}[/red]")
            raise typer.Exit(2)
        try:
            hist = _get(f"{base}/api/phd2/calibration", 60).get("history") or []
        except Exception:  # noqa: BLE001
            hist = []
        nev = None
        if nina_events:
            host = urllib.parse.urlparse(base).hostname
            nev = {}
            for rg, port in (("rc16", 1888), ("piggyback", 1889)):
                try:
                    body = _get(f"http://{host}:{port}/v2/api/event-history", 30)
                    nev[rg] = body.get("Response", body) if isinstance(body, dict) else body
                except Exception:  # noqa: BLE001
                    pass
        rep = sd.replay_night(cfg, d, run.get("subs") or [], rig=rig or None,
                              plan=run.get("plan"), cal_history=hist,
                              nina_events=nev, solves={})
    else:
        rep = sd.local_night(cfg, d, rig or None)
    if not timeline:
        for r in (rep.get("rigs") or {}).values():
            r["timeline_n"] = len(r.pop("timeline", []) or [])
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(sd.format_report(cfg, rep), markup=False, highlight=False)
        if timeline:
            for rg, r in (rep.get("rigs") or {}).items():
                for x in r.get("timeline") or []:
                    console.print(f"  {rg} {x.get('t')} {x.get('cause') or 'healthy'} "
                                  f"{x.get('confidence') or ''} {x.get('file') or ''}",
                                  markup=False, highlight=False)


@app.command("pilot-backtest")
def pilot_backtest_cmd(
    night: str = typer.Option("", "--night", help="Night (YYYY-MM-DD, the evening date)"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Read a running PhotonScript (GET /api/runs/<night>, "
                                 "/api/tracking/unguided-limits, /api/calibration/owed, "
                                 "both NINAs' /event-history at :1888 / :1889)"),
    doc: str = typer.Option("", "--doc", help="A saved backtest document (JSON) instead"),
    no_limits: bool = typer.Option(False, "--no-limits",
                                   help="Replay without the learned unguided limits"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """Night pilot backtest: replay a past night's graded subs through the
    pilot (sub watch, policy, guards, rate limits, closed-roof check) in
    advise mode and print what it would have done and when. Read only:
    GETs only, nothing is sent, the pilot state goes to a scratch dir.

    photonscript pilot-backtest --night 2026-10-13 --url http://100.94.189.77:8100
    """
    import json as _json
    from photonscript.scheduler import pilot_backtest as pb
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if doc:
        body = pb.load_doc(doc)
    elif url:
        import urllib.parse
        import urllib.request

        def _get(u, timeout=170):
            with urllib.request.urlopen(u, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8"))
        base = url.rstrip("/")
        d = night or _last_night()
        run = _get(f"{base}/api/runs/{d}")
        host = urllib.parse.urlparse(base).hostname
        nev = {}
        for rg, port in (("rc16", 1888), ("piggyback", 1889)):
            try:
                b = _get(f"http://{host}:{port}/v2/api/event-history", 30)
                nev[rg] = b.get("Response", b) if isinstance(b, dict) else b
            except Exception:  # noqa: BLE001
                nev[rg] = []
        lim = {}
        for rg in ("rc16", "piggyback"):
            try:
                lim[rg] = (_get(f"{base}/api/tracking/unguided-limits?rig={rg}", 60)
                           .get("rigs") or {}).get(rg)
            except Exception:  # noqa: BLE001
                pass
        owed = {}
        try:
            for r in _get(f"{base}/api/calibration/owed", 120).get("rigs") or []:
                owed[r["rig"]] = [{"exp_s": x["exp_s"], "have": x["have"],
                                   "owed": x["owed"]} for x in r.get("darks") or []]
        except Exception:  # noqa: BLE001
            pass
        body = {"date": d, "plan": run.get("plan"), "subs": run.get("subs") or [],
                "nina_events": nev, "unguided_limits": {"rigs": lim},
                "darks_owed": owed}
    else:
        console.print("[red]--url or --doc is required[/red]")
        raise typer.Exit(2)
    rep = pb.run(cfg, body, use_limits=not no_limits)
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(pb.format_report(rep), markup=False, highlight=False)


@app.command("frame-check")
def frame_check_cmd(
    rig: str = typer.Option("rc16", "--rig", help="rc16 | piggyback"),
    last: int = typer.Option(2, "--last", help="Check NINA's last N frames"),
    index: Optional[List[int]] = typer.Option(None, "--index",
                                              help="NINA image-history index "
                                                   "(repeatable; wins over --last)"),
    image_type: str = typer.Option("", "--type",
                                   help="Only this NINA ImageType for --last "
                                        "(e.g. SNAPSHOT, LIGHT)"),
    pa: Optional[float] = typer.Option(None, "--pa",
                                       help="Camera position angle (deg) when "
                                            "the FITS and the solves do not say"),
    compare: bool = typer.Option(False, "--compare",
                                 help="Verdict over the frames (A, B, ... "
                                      "oldest first): round or trailing, along "
                                      "RA / Dec, at what rate"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Ask a running PhotonScript instead (GET "
                                 "/api/frame-check on the scope PC: small "
                                 "JSON, no FITS over the network)"),
    nina_url: str = typer.Option("", "--nina-url",
                                 help="Read this NINA's image history (default "
                                      "the rig's nina_base_url)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """Grade NINA's last frames, snapshots included, with the sub star QA:
    HFR, FWHM", eccentricity, star count, background, the elongation axis
    and the trail (arcsec and "/min). Read only.

    On the scope PC it reads NINA's image history and the files under NINA's
    save path. Elsewhere, --url reads the scope's /api/frame-check; with no
    file here and no --url it prints the command to run on the scope PC.

    photonscript frame-check --last 2 --compare
    photonscript frame-check --url http://100.94.189.77:8100 --compare
    """
    import json as _json
    from photonscript.scheduler import frame_check as fc
    if rig not in ("rc16", "piggyback"):
        console.print("[red]--rig must be rc16 or piggyback[/red]")
        raise typer.Exit(2)
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if url:
        import urllib.parse
        import urllib.request
        q = [("rig", rig), ("last", last), ("compare", str(compare).lower())]
        q += [("index", i) for i in (index or [])]
        if image_type:
            q.append(("type", image_type))
        if pa is not None:
            q.append(("pa", pa))
        u = f"{url.rstrip('/')}/api/frame-check?{urllib.parse.urlencode(q)}"
        try:
            with urllib.request.urlopen(u, timeout=180) as r:
                rep = _json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            console.print(f"Could not read {u}: {e}\nOn the scope PC run:\n  "
                          + fc.scope_command(rig, last, index, compare, image_type, pa),
                          markup=False, highlight=False)
            raise typer.Exit(2)
    else:
        rep = fc.check(cfg, rig=rig, last=last, index=index or None,
                       image_type=image_type, pa=pa, compare=compare,
                       base_url=nina_url)
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(fc.format_report(rep), markup=False, highlight=False)
    if not rep.get("ok"):
        raise typer.Exit(1)


@app.command("unguided-limits")
def unguided_limits_cmd(
    rig: str = typer.Option("", "--rig", help="rc16 | piggyback (default both)"),
    refresh: bool = typer.Option(False, "--refresh",
                                 help="Recompute from this machine's nights and save the store"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Compute from a running PhotonScript instead (read-only "
                                 "GET /api/runs/<night> per night; nothing saved)"),
    nights: int = typer.Option(14, "--nights", help="With --url: nights back from today"),
    since: str = typer.Option("", "--since", help="Evidence from this night on "
                              "(overrides unguided_limit_since / the TPoint model date)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """Learned unguided exposure limits per rig and sky region: per Dec /
    hour-angle region the subs and passes (ecc gate) per sub length, the
    drift rate, the longest length that stays round (measured or from the
    drift model), its confidence and the probe step. Read only unless
    --refresh (writes <data_dir>/unguided_limits.json).

    photonscript unguided-limits --rig rc16 --url http://100.94.189.77:8100
    """
    import json as _json
    from datetime import timedelta as _td
    from photonscript.scheduler import unguided_limits as ul
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if since:
        cfg = cfg.model_copy(update={"unguided_limit_since": since})
    today = datetime.now().strftime("%Y-%m-%d")
    if url:
        import urllib.request

        def _get(u, timeout=170):
            with urllib.request.urlopen(u, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8"))
        base = url.rstrip("/")
        d0 = datetime.now().date()
        data = {}
        for i in range(max(1, nights)):
            n = (d0 - _td(days=i)).isoformat()
            try:
                run = _get(f"{base}/api/runs/{n}")
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]{n}: not read ({e})[/yellow]")
                continue
            if run.get("subs"):
                data[n] = {"subs": run.get("subs") or [], "plan": run.get("plan")}
        doc = ul.refresh(cfg, today, save=False, nights_data=data)
    elif refresh:
        doc = ul.refresh(cfg, today)
    else:
        doc = ul.table(cfg)
    if not doc:
        console.print("No unguided limits yet (no unguided nights in the lookback).")
        raise typer.Exit(0)
    if as_json:
        out = doc if not rig else {**doc, "rigs": {rig: (doc.get("rigs") or {}).get(rig)}}
        print(_json.dumps(out, indent=1, default=str))
    else:
        console.print(ul.format_table(doc, rig or None, ul.tpoint_coverage(cfg)),
                      markup=False, highlight=False)
        for rg in (doc.get("rigs") or {}):
            if rig and rg != rig:
                continue
            dl = ul.learned_dark_lengths(cfg, rg, doc)
            if dl:
                console.print(f"{rg}: darks owed for learned lengths: "
                              + ", ".join(f"{x:g} s" for x in dl),
                              markup=False, highlight=False)
def _goal_from_detail(detail: dict):
    """A project-like object (exposure_plans with the HDR short set) from
    /api/targets/detail's goal block, for core_check.coverage."""
    from types import SimpleNamespace
    g = (detail or {}).get("goal") or {}
    plans = []
    for p in g.get("plans") or []:
        hs = p.get("hdr_short") or {}
        plans.append(SimpleNamespace(
            filter_type=SimpleNamespace(value=p.get("filter")), rig=p.get("rig") or "rc16",
            hdr_short_seconds=hs.get("exposure_s"), hdr_short_count=hs.get("count") or 0))
    return SimpleNamespace(exposure_plans=plans,
                           exposure_overrides=g.get("exposure_overrides") or {}) if plans else None


def _measure_folder(cfg, folder: Path, target: str, rig: str) -> list[dict]:
    """core_check rows measured from the FITS under `folder` (read only;
    a Library folder: every light counts as approved)."""
    from astropy.io import fits as _fits
    from photonscript.shared.core_sat import sub_core
    rev = cfg.reverse_filter_map()
    out = []
    for f in sorted(folder.rglob("*.fit*")):
        try:
            hdr = _fits.getheader(f)
        except Exception:  # noqa: BLE001
            continue
        if "LIGHT" not in str(hdr.get("IMAGETYP", "LIGHT")).upper():
            continue
        if bool(hdr.get("BAYERPAT")) != (rig == "piggyback"):
            continue        # the other rig's frames (OSC = Piggy-600)
        c = sub_core(cfg, rig, f, hdr, target)
        flt = "OSC" if rig == "piggyback" else str(hdr.get("FILTER") or "?").strip()
        out.append({"date": str(hdr.get("DATE-OBS", ""))[:10], "target": target,
                    "rig": rig, "filter": rev.get(flt, flt),
                    "exp_s": float(hdr.get("EXPTIME") or 0) or None,
                    "verdict": "approved", "core_sat_px": c.get("core_sat_px"),
                    "core_peak_adu": c.get("core_peak_adu"),
                    "core_p999": c.get("core_p999"), "sat_adu": c.get("core_sat_adu"),
                    "offset": hdr.get("OFFSET"), "file": f.name,
                    "core_src": c.get("core_src")})
    return out


@app.command("core-check")
def core_check_cmd(
    target: str = typer.Option(..., "--target", help="Target (M31, NGC 7331, ...)"),
    rig: str = typer.Option("rc16", "--rig", help="rc16 | piggyback | all"),
    night: str = typer.Option("", "--night", help="Only this night's subs in the table "
                              "(the HDR coverage always counts every night)"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Read a running PhotonScript (GET /api/subs and "
                                 "/api/targets/detail) instead of this machine's logs"),
    measure: str = typer.Option("", "--measure", help="Measure the FITS under this folder "
                                "now (read only), e.g. the desktop Library's target folder"),
    as_json: bool = typer.Option(False, "--json", help="Print the rows and coverage as JSON"),
):
    """Is the target's core (galaxy nucleus, bright core) clipped? Per filter
    and sub length: subs, measured, clipped, median / max clipped core px,
    peak and p99.9 ADU; then per filter whether the HDR short subs cover it
    and a short length that puts the core near 80% of saturation. Read only.

    photonscript core-check --target M31 --rig rc16 --url http://100.94.189.77:8100
    """
    import json as _json
    from photonscript.scheduler import core_check as cc
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    rg = "" if rig in ("", "all") else rig
    project = None
    if measure:
        rows = _measure_folder(cfg, Path(measure), target, rg or "rc16")
    elif url:
        import urllib.parse
        import urllib.request
        base = url.rstrip("/")

        def _get(u, timeout=120):
            with urllib.request.urlopen(u, timeout=timeout) as r:
                return _json.loads(r.read().decode("utf-8"))
        rows, off = [], 0
        try:
            while True:
                q = urllib.parse.urlencode({"target": target, "rig": rg, "offset": off,
                                            "limit": 200})
                page = _get(f"{base}/api/subs?{q}")
                rows += page.get("rows") or []
                off += 200
                if off >= int(page.get("total") or 0):
                    break
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Could not read {base}/api/subs: {e}[/red]")
            raise typer.Exit(2)
        try:
            project = _goal_from_detail(_get(f"{base}/api/targets/detail?"
                                             + urllib.parse.urlencode({"name": target})))
        except Exception:  # noqa: BLE001
            project = None
        rows = [cc.norm(r) for r in rows]
    else:
        from photonscript.scheduler import sub_index
        from photonscript.scheduler.calibration_plan import load_projects
        from photonscript.shared.target_names import target_key
        projects = load_projects(cfg)
        rows = [cc.norm(r) for r in sub_index.rows(cfg, projects, target=target,
                                                   rig=rg or None)]
        project = next((p for p in projects
                        if target_key(p.target.name) == target_key(target)), None)
    table = [r for r in rows if not night or r.get("date") == night]
    if as_json:
        print(_json.dumps({"target": target, "night": night or None,
                           "groups": cc.summarize(cfg, table),
                           "coverage": [{k: v for k, v in c.items() if k != "shorts"}
                                        for c in cc.coverage(cfg, rows, project)]},
                          indent=1, default=str))
        return
    console.print(cc.format_report(cfg, target, table, project, night or None,
                                   coverage_rows=rows),
                  markup=False, highlight=False)


@app.command("blend-readiness")
def blend_readiness_cmd(
    target: str = typer.Option("", "--target", help="One goal (default: every active "
                               "two-rig narrowband goal)"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Read a running PhotonScript (GET /api/blend-readiness) "
                                 "instead of this machine's goal store"),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON"),
):
    """Narrowband two-rig blend readiness: per goal with an RC16 Ha plan and
    a Piggy-600 plan, the accepted RC16 Ha / OIII / SII subs and hours the
    NB blend would use, the Piggy-600 master, whether it is ready
    (blend_nb_min_ha_h, blend_nb_min_oiii_h, blend_nb_min_sii_h) and nights
    to each threshold from tonight's plan. Read only.

    photonscript blend-readiness --target "Heart Nebula" --url http://100.94.189.77:8100
    """
    import json as _json
    from photonscript.scheduler import blend_readiness as br
    if url:
        import urllib.parse
        import urllib.request
        u = (url.rstrip("/") + "/api/blend-readiness?"
             + urllib.parse.urlencode({"target": target}))
        try:
            with urllib.request.urlopen(u, timeout=120) as r:
                rep = _json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Could not read {u}: {e}[/red]")
            raise typer.Exit(2)
    else:
        from photonscript.scheduler.calibration_plan import load_projects
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        rep = br.report(cfg, load_projects(cfg), target=target)
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
        return
    console.print(br.format_report(rep), markup=False, highlight=False)


def _target_links_http(method: str, url: str, body=None, timeout: float = 30):
    """One JSON request for target-links; (status, parsed body)."""
    import json as _json
    import urllib.error
    import urllib.request
    data = None if body is None else _json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=(
        {"Content-Type": "application/json"} if data is not None else {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json.loads(e.read().decode("utf-8") or "null")
        except Exception:  # noqa: BLE001
            return e.code, None


@app.command("target-links")
def target_links_cmd(
    target: str = typer.Option(..., "--target", help="Goal name, catalog id or alias"),
    add: Optional[tuple[str, str]] = typer.Option(
        (None, None), "--add", help="LABEL URL: add a link (replaces one with "
                                   "the same label)"),
    remove: Optional[str] = typer.Option(None, "--remove",
                                         help="Remove the link with this label"),
    url: str = typer.Option("http://127.0.0.1:8100", "--url", envvar="PS_MONITOR_URL",
                            help="The running PhotonScript to read and PATCH"),
    as_json: bool = typer.Option(False, "--json", help="Print the links as JSON"),
):
    """List, add or remove a goal's target links (shown on its target page).

    Resolves the goal with GET /api/targets/detail, then PATCHes
    /api/projects2/{id} {"links": [...]} (the whole list). Desktop-side.

    photonscript target-links --target M31 --add "Progression sheet" https://claude.ai/artifact/P5vqZKUQW2eQxyRhhaNRcq --url http://100.94.189.77:8100
    """
    import json as _json
    import urllib.parse
    base = url.rstrip("/")
    u = base + "/api/targets/detail?" + urllib.parse.urlencode({"name": target})
    try:
        st, d = _target_links_http("GET", u)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Could not read {u}: {e}[/red]")
        raise typer.Exit(2)
    if st != 200 or not isinstance(d, dict) or not d.get("project_id"):
        console.print(f"[red]No imaging project for {target!r} at {base}[/red]")
        raise typer.Exit(1)
    links = [{"label": x["label"], "url": x["url"]} for x in d.get("links") or []]
    add_label, add_url = add if add else (None, None)
    if add_label or remove:
        if remove:
            keep = [x for x in links if x["label"].lower() != remove.strip().lower()]
            if len(keep) == len(links):
                console.print(f"[red]No link labelled {remove!r} on {d['target']}[/red]")
                raise typer.Exit(1)
            links = keep
        if add_label:
            links = [x for x in links if x["label"].lower() != add_label.strip().lower()]
            links.append({"label": add_label.strip(), "url": (add_url or "").strip()})
        pu = base + "/api/projects2/" + urllib.parse.quote(d["project_id"])
        try:
            st, r = _target_links_http("PATCH", pu, {"links": links})
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Could not PATCH {pu}: {e}[/red]")
            raise typer.Exit(2)
        if st != 200:
            detail = r.get("detail") if isinstance(r, dict) else r
            console.print(f"[red]Not saved ({st}): {detail}[/red]")
            raise typer.Exit(1)
        links = r.get("links") or []
    if as_json:
        print(_json.dumps({"target": d["target"], "project_id": d["project_id"],
                           "links": links}, indent=1))
        return
    print(f"{d['target']} ({d['project_id']}): {len(links)} link"
          f"{'' if len(links) == 1 else 's'}")
    for x in links:
        print(f"  {x['label']}: {x['url']}")


@app.command("optics-report")
def optics_report_cmd(
    date: str = typer.Option("", help="Night (YYYY-MM-DD, the evening date); "
                                      "default the night that started last"),
    rig: str = typer.Option("rc16", help="Rig whose sidecars to read"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-95: RC16 tilt and collimation report from the night's star
    sidecars: 3x3 zone FWHM / eccentricity / stretch direction, the field
    fit, per-filter verdicts and a recommendation. Reads stored data only
    (writes the report cache under data_dir/optics).

    photonscript optics-report --date 2026-09-26
    """
    import json as _json
    from photonscript.scheduler.optics_report import format_report, night_optics
    rep = night_optics(_config_for_repo(Path(__file__).resolve().parents[1]),
                       date or _last_night(), rig or "rc16")
    if as_json:
        print(_json.dumps(rep, indent=2, default=str))
    else:
        console.print(format_report(rep), markup=False, highlight=False)
    raise typer.Exit(0 if rep["overall"].get("n_measured") else 1)


@app.command("exposure-report")
def exposure_report_cmd(
    target: str = typer.Option("", "--target", help="Target name or catalog "
                               "id (its alias folders are read too)"),
    rig: str = typer.Option("piggyback", "--rig", help="rc16 | piggyback"),
    flt: str = typer.Option("", "--filter", help="One filter folder (OSC, "
                            "Ha, ...); default all"),
    library: str = typer.Option("", "--library", help="Library root; default "
                                "the desktop mirror, else the scope Library"),
    every: int = typer.Option(1, "--every", help="Measure every Nth light"),
    limit: int = typer.Option(0, "--limit", help="At most N lights (0 = all)"),
    profile: bool = typer.Option(False, "--profile", help="Signal profile "
                                 "along the major axis from the core"),
    feature_arcmin: Optional[float] = typer.Option(
        None, "--feature-arcmin", help="Feature distance from the core "
        "(arcmin): its profile signal seeds the SNR model (implies --profile)"),
    signal: Optional[float] = typer.Option(None, "--signal", help="Feature "
                                           "signal, e-/s per 2x2 pixel"),
    goal_snr: Optional[float] = typer.Option(None, "--goal-snr"),
    camera_cal: bool = typer.Option(False, "--camera-cal", help="Re-measure "
                                    "read noise, gain and dark current from "
                                    "the Library bias / flat / dark frames"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-117: how much light a target needs and the best sub length, from
    its Library lights (read-only): sky per CFA channel, read-noise share,
    saturation, DATE-OBS overhead, an optional signal profile, and the
    per-length SNR model. --camera-cal measures the camera constants.

    photonscript exposure-report --target M31 --feature-arcmin 60
    photonscript exposure-report --camera-cal
    """
    import json as _json
    from photonscript.scheduler import exposure_report as er
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if camera_cal:
        rep = er.camera_cal(cfg, library)
        print(_json.dumps(rep, indent=2, default=str) if as_json
              else er.format_cal(rep))
        raise typer.Exit(0 if any(rep["rigs"].values()) else 1)
    if not target:
        console.print("--target is required (or --camera-cal)")
        raise typer.Exit(2)
    rep = er.exposure_report(cfg, target, rig=rig, flt=flt, library=library,
                             every=max(1, every), profile=profile,
                             feature_arcmin=feature_arcmin, signal=signal,
                             goal_snr=goal_snr, limit=limit)
    if as_json:
        print(_json.dumps(rep, indent=2, default=str))
    else:
        print(er.format_report(rep))   # wide tables: no rich wrapping
    raise typer.Exit(0 if rep["measured"] else 1)


@app.command("thesky-audit")
def thesky_audit_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
    imagelink: bool = typer.Option(False, "--imagelink", help="Also solve the newest "
                                   "RC16 L frame with ASTAP (the Image Link check)"),
    thesky_imagelink: bool = typer.Option(False, "--thesky-imagelink", help="Also run "
                                          "TheSky's own Image Link on a temporary copy "
                                          "(needs thesky_audit_imagelink_thesky and an "
                                          "idle armer)"),
    url: str = typer.Option("http://localhost:8100", "--url",
                            help="Scheduler to ask for the armer state"),
):
    """PS-104: TheSky / TPoint settings audit, REPORT ONLY (read-only TheSky
    scripts, the stored Image Link check, the NINA Center log, the manual
    TPoint record). Never writes TheSky, moves the mount or takes an image.

    photonscript thesky-audit --json
    """
    import json as _json
    from photonscript.scheduler import thesky_audit as ta
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    state = ""
    if thesky_imagelink:
        if not getattr(cfg, "thesky_audit_imagelink_thesky", False):
            console.print("--thesky-imagelink needs PS_THESKY_AUDIT_IMAGELINK_THESKY=true "
                          "(or use the Guiding tab button)", markup=False)
            raise typer.Exit(2)
        try:
            import httpx
            state = str(httpx.get(url.rstrip("/") + "/api/arm", timeout=10).json()
                        .get("state") or "")
        except Exception as e:  # noqa: BLE001
            console.print(f"cannot confirm the armer is idle ({e}): not running TheSky "
                          "Image Link", markup=False)
            raise typer.Exit(2)
    if imagelink or thesky_imagelink:
        chk = ta.imagelink_check(cfg, thesky=thesky_imagelink, armer_state=state)
        if not as_json:
            console.print(f"Image Link check: {chk.get('file')} "
                          f"{_json.dumps(chk.get('astap'))} {chk.get('note') or ''}"
                          + (f"\nTheSky: {_json.dumps(chk.get('thesky'))}"
                             if chk.get("thesky") else ""), markup=False, highlight=False)
    audit = ta.run_audit(cfg, "cli", armer_state=state or None)
    if as_json:
        print(_json.dumps(audit, indent=2, default=str))
    else:
        console.print(ta.format_report(audit), markup=False, highlight=False)


@app.command("settings")
def settings_cmd(
    night: str = typer.Option("", "--night", help="Night YYYY-MM-DD (noon to noon)"),
    at: str = typer.Option("", "--at", help="Local HH:MM on that night (21:30, 03:10)"),
    diff: Optional[tuple[str, str]] = typer.Option(
        None, "--diff", help="What changed from A to B: snapshot ids, latest, last_good, "
                             "YYYY-MM-DD or YYYY-MM-DD@HH:MM"),
    sub: str = typer.Option("", "--sub", help="A sub's file name: the settings it was "
                                              "taken with"),
    full: bool = typer.Option(False, "--full", help="Every stored setting, not only the "
                                                    "ones that matter"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
    url: str = typer.Option("", "--url", help="Ask a scheduler (http://host:8100) "
                                              "instead of this PC's data folder"),
):
    """Settings ledger, READ ONLY: what NINA #1 / #2, PHD2, TheSky and
    PhotonScript were set to at a time or for a sub, the rule results, and
    what changed between two snapshots.

    photonscript settings --night 2026-10-12 --at 23:40
    photonscript settings --sub "2026-10-13_05-56-11_Heart Nebula_O_180.00s_0034.fits"
    photonscript settings --diff last_good latest
    """
    import json as _json
    from photonscript.scheduler import settings_ledger as sl

    def show(obj, text):
        if as_json:
            print(_json.dumps(obj, indent=2, default=str))
        else:
            console.print(text, markup=False, highlight=False)

    def get(path, **params):
        import httpx
        r = httpx.get(url.rstrip("/") + path, params=params, timeout=60)
        r.raise_for_status()
        return r.json()
    cfg = None if url else _config_for_repo(Path(__file__).resolve().parents[1])
    if diff:
        a, b = diff
        d = get("/api/settings-ledger/diff", a=a, b=b) if url else sl.diff(cfg, a, b)
        if not d.get("ok"):
            show(d, d.get("note") or "no such snapshot")
            raise typer.Exit(1)
        lines = [f"From {d['a']['id']} ({d['a']['night']}) to {d['b']['id']} "
                 f"({d['b']['night']}): {d['changed']} settings changed"]
        for app_, rows in d["diff"].items():
            lines.append(f"\n[{sl.APP_LABELS.get(app_, app_)}]")
            for k, (old, new) in ((k, v) for k, v in rows.items() if k != "_truncated"):
                lines.append(f"  {k}: {sl._g(old)} -> {sl._g(new)}")
        show(d, "\n".join(lines))
        return
    if sub:
        hit = get("/api/settings-ledger/sub", file=sub, night=night) if url \
            else sl.for_sub(cfg, sub, night or None)
        head = (f"Sub {hit.get('sub', {}).get('file') or sub} (night {hit.get('night')}): "
                + ", ".join(f"{k} {v}" for k, v in (hit.get("sub") or {}).items()
                            if k not in ("file",)))
        if not hit.get("ok"):
            show(hit, head + "\n" + str(hit.get("note")))
            raise typer.Exit(1)
        show(hit, head + f"\nsettings {hit['id']} ({hit['how']})\n"
             + sl.format_snapshot(hit["snapshot"], hit.get("line"), full=full))
        return
    if night or at:
        if not night:
            console.print("--at needs --night", markup=False)
            raise typer.Exit(2)
        if url:
            hit = get("/api/settings-ledger/at", night=night, at=at or "12:00")
        else:
            when = sl.local_to_utc(cfg, night, at) if at else None
            if when is None:   # the night's last snapshot
                sid = sl.resolve_ref(cfg, night)
                snap = sl.load_snapshot(cfg, sid)
                hit = {"ok": bool(snap), "snapshot": snap,
                       "line": (sl.index(cfg, night) or [None])[-1]}
            else:
                hit = sl.at(cfg, when) or {"ok": False}
                hit.setdefault("ok", bool(hit.get("snapshot")))
        if not hit.get("snapshot"):
            show(hit, f"No settings snapshot for {night} {at}".strip())
            raise typer.Exit(1)
        st = sl.load_status(cfg, hit["snapshot"].get("night")) if cfg else {}
        text = sl.format_snapshot(hit["snapshot"], hit.get("line"), full=full)
        if st.get("rows"):
            text += "\n\nRules (newest evaluation that night):\n" + sl.format_rules(st["rows"])
        show(hit, text)
        return
    st = get("/api/settings-ledger/status") if url else sl.status(cfg)
    if not st.get("ok"):
        show(st, st.get("note") or "no settings snapshot yet")
        raise typer.Exit(1)
    apps_ = "\n".join(f"  {a['label']}: read {a.get('read_utc')}"
                      + (" (not read now)" if a.get("carried") else "")
                      + (f", last change {a['last_change_utc']}" if a.get("last_change_utc") else "")
                      for a in (st.get("apps") or {}).values())
    show(st, f"Settings ledger: night {st['night']}, snapshot {st['id']}, checked "
             f"{st.get('checked_utc')} ({st.get('reason')})\n{apps_}\n\n"
             + sl.format_rules(st.get("rows") or []))


@app.command("settings-snapshot")
def settings_snapshot_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
    no_push: bool = typer.Option(False, "--no-push", help="Never push, even on a new FAIL"),
):
    """Settings ledger: take one snapshot now on this PC (reads NINA #1 / #2,
    PHD2, TheSky and the config; writes only <data_dir>/settings_ledger)."""
    import asyncio as _asyncio
    import json as _json
    from photonscript.scheduler import settings_ledger as sl
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    res = _asyncio.run(sl.take(cfg, "cli", push=False if no_push else None))
    if as_json:
        print(_json.dumps(res, indent=2, default=str))
        return
    console.print(f"snapshot {res['id']} ({'changed' if res['changed'] else 'unchanged'}); "
                  f"not read now: {', '.join(res['unread']) or 'none'}\n"
                  + sl.format_rules(res["rules"]["rows"]), markup=False, highlight=False)


@app.command("settings-backfill")
def settings_backfill_cmd(
    night: list[str] = typer.Option([], "--night", help="Night(s) YYYY-MM-DD; default "
                                                         "every night with stored data"),
    apply: bool = typer.Option(False, "--apply", help="Write (default: dry run)"),
    force: bool = typer.Option(False, "--force", help="Rewrite nights that hold only "
                                                      "backfilled data"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """Settings ledger backfill from what PhotonScript stored (PS-89 / PS-104
    audits, sub records, PHD2 guide logs). Nights with live ledger data are
    never touched. Lists what cannot be reconstructed."""
    import json as _json
    from photonscript.scheduler import settings_ledger as sl
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    rep = sl.backfill(cfg, night or None, dry_run=not apply, force=force)
    if as_json:
        print(_json.dumps(rep, indent=2, default=str))
        return
    lines = [("DRY RUN: " if not apply else "") + f"{len(rep['nights'])} nights"]
    for n in rep["nights"]:
        lines.append(f"  {n['night']}: {n['events']} readings, {n['snapshots']} snapshots, "
                     f"failing then: {', '.join(n['ever_failed']) or 'none'}")
    for s in rep["skipped"]:
        lines.append(f"  {s['night']}: skipped ({s['why']})")
    lines.append("Cannot be reconstructed:")
    lines += [f"  - {c}" for c in rep["cannot"]]
    console.print("\n".join(lines), markup=False, highlight=False)


@app.command("guiding-status")
def guiding_status_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-119: the Guiding tab's "What to change" list from the cached
    records (PHD2 and TheSky audits, calibration, self-test, guard, tuner):
    every fail then warn with the fix and where to do it, old readings that
    may already be fixed, and what is not checked yet. Never runs an audit.

    photonscript guiding-status --json
    """
    import json as _json
    from photonscript.scheduler import guiding_attention as ga
    s = ga.safe_build(_config_for_repo(Path(__file__).resolve().parents[1]))
    if as_json:
        print(_json.dumps(s, indent=2, default=str))
    else:
        console.print(ga.format_text(s), markup=False, highlight=False)


@app.command("guide-recover")
def guide_recover_cmd(
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Show the readings and what it would do; change nothing"),
    yes: bool = typer.Option(False, "--yes", help="Do the steps without the typed "
                             "confirmation (operator only)"),
    force: bool = typer.Option(False, "--force", help="Reconnect PHD2's mount even "
                               "when the debug log shows no refused pulse"),
    as_json: bool = typer.Option(False, "--json", help="Print the readings and plan as JSON"),
):
    """PS-167: recover from the mount driver refusing PHD2's guide pulses
    (PHD2 debug log 'IsSlewing failed ... pulseguide command failed').
    OPERATOR ONLY, run on the scope PC. Reads PHD2's debug log, TheSky's
    slew state (TCP 3040, read script) and PHD2's state (JSON-RPC), then:
    abort a slew TheSky reports while the mount stands still, stop PHD2,
    set_connected false / true, verify. --dry-run changes nothing.

    photonscript guide-recover --dry-run
    """
    import json as _json
    from photonscript.scheduler import guide_recover as gr
    from photonscript.telescope_agent.pulse_refusal_watch import DebugLogTail, newest_debug_log
    from photonscript.telescope_agent.thesky_client import client_from_config
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    thesky = client_from_config(cfg)
    state = {}
    try:
        state["debug"] = gr.debug_summary(cfg)
    except Exception as e:  # noqa: BLE001
        state["debug"] = {"ok": False, "note": f"debug log not read: {e}"}
    state["thesky"] = gr.thesky_state(thesky)
    try:
        with gr.Phd2Rpc(cfg.phd2_host, cfg.phd2_port) as phd2:
            state["phd2"] = gr.phd2_state(phd2)
    except Exception as e:  # noqa: BLE001
        state["phd2"] = {"ok": False, "note": f"PHD2 not answering: {e}"}
    p = gr.plan(state, force=force)
    if as_json:
        print(_json.dumps({"state": state, "plan": p, "dry_run": dry_run},
                          indent=2, default=str))
    else:
        console.print(gr.format_plan(state, p, dry_run), markup=False, highlight=False)
    if dry_run or not p["steps"]:
        raise typer.Exit(0 if p["verdict"] != "blocked" else 1)
    if not yes:
        if not sys.stdin.isatty():
            console.print("not a terminal and no --yes: nothing done", markup=False)
            raise typer.Exit(2)
        if input("Type yes to do these steps now: ").strip().lower() != "yes":
            console.print("nothing done", markup=False)
            raise typer.Exit(1)
    tail = DebugLogTail(lambda: newest_debug_log(cfg))
    tail.skip_to_end()                    # the verify step reads from here

    def _since():
        return tail.read_new()[1]
    with gr.Phd2Rpc(cfg.phd2_host, cfg.phd2_port, timeout=30.0) as phd2:
        res = gr.execute(p["steps"], phd2=phd2, thesky=thesky, log_tail=_since,
                         say=lambda m: console.print(m, markup=False, highlight=False))
    raise typer.Exit(0 if res and all(r["ok"] for r in res) else 1)


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


@app.command("optics-test-report")
def optics_test_report(
    date: str = typer.Option("", help="Night (YYYY-MM-DD, the runs page "
                                      "date); default tonight"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-148: through-focus optics test report: subs named 'Optics test
    <field> <filter> <offset>' per filter and focuser offset (bright-star
    ecc, HFR, stretch axis per zone) and the verdict (astigmatism / constant
    axis / defocus only, tilt). Read-only."""
    import json as _json
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.optics_test import build_report, format_report
    rep = build_report(PhotonScriptConfig(), date or None)
    if as_json:
        console.print_json(_json.dumps(rep, default=str))
    else:
        console.print(format_report(rep), markup=False, highlight=False)


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


@app.command("rotation-report")
def rotation_report(
    nights: int = typer.Option(14, help="Nights back from --end"),
    end: str = typer.Option("", help="Last night (YYYY-MM-DD, the evening "
                                     "date); default the current night"),
    split: str = typer.Option("", help="Compare before / after: a night "
                                       "(YYYY-MM-DD, that night on = after) or "
                                       "a UTC timestamp; default the TPoint "
                                       "record's model date"),
    ma: float = typer.Option(None, help="Polar error MA (arcmin); default "
                                        "the TPoint record"),
    me: float = typer.Option(None, help="Polar error ME (arcmin); default "
                                        "the TPoint record"),
    night_hours: float = typer.Option(6.0, help="Hours on one target for the "
                                                "per-night corner cost"),
    refresh: bool = typer.Option(False, "--refresh",
                                 help="Re-measure nights (ignore the cache)"),
    url: str = typer.Option("", "--url", envvar="PS_MONITOR_URL",
                            help="Ask a running PhotonScript (GET "
                                 "/api/rotation/report) instead"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-97: field rotation measured (solve PAs, star registration) vs the
    rotation the TPoint polar error predicts, per rig, pier side, Dec and
    HA, before / after a split, with a verdict and the corner cost. Report
    only.

    photonscript rotation-report --nights 14 --split 2026-10-04
    """
    import json as _json
    from photonscript.scheduler.field_rotation import format_report, report
    if url:
        import urllib.parse
        import urllib.request
        q = {"nights": nights, "night_hours": night_hours,
             "refresh": "true" if refresh else "false"}
        for k, v in (("end", end), ("split", split), ("ma", ma), ("me", me)):
            if v not in (None, ""):
                q[k] = v
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/rotation/report?"
                                        + urllib.parse.urlencode(q), timeout=170) as r:
                rep = _json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Could not read {url}: {e}[/red]")
            raise typer.Exit(2)
    else:
        rep = report(_config_for_repo(Path(__file__).resolve().parents[1]),
                     nights, end or None, split or None, ma, me, refresh, night_hours)
    if as_json:
        print(_json.dumps(rep, indent=2, default=str))
    else:
        console.print(format_report(rep), markup=False, highlight=False)
    raise typer.Exit(0 if rep.get("ok") else 1)


@app.command("piggy-offset")
def piggy_offset_cmd(
    nights: int = typer.Option(0, help="Nights of pointing sidecars "
                                       "(0 = config piggy_center_nights)"),
    save: bool = typer.Option(False, "--save",
                              help="Store the result (<data_dir>/piggy_offset.json)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-26: the RC16-to-Piggy-600 boresight offset per pier side from
    simultaneous plate solves in the PS-67 pointing sidecars (stored solves
    only, never ASTAP). Report only unless --save.

    photonscript piggy-offset --nights 30
    """
    import json as _json
    from photonscript.scheduler import piggy_offset as po
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    rep = po.measure(cfg, nights or None)
    if save:
        po.save(cfg, rep)
    if as_json:
        print(_json.dumps(rep, indent=2, default=str))
    else:
        console.print(po.format_report(rep), markup=False, highlight=False)


@app.command("pointing-backfill")
def pointing_backfill(
    since: str = typer.Option("", help="First night (YYYY-MM-DD); with no "
                                       "--date, every night from here on"),
    date: str = typer.Option("", help="One night only (YYYY-MM-DD)"),
    solve: bool = typer.Option(False, "--solve/--no-solve",
                               help="Also run the sampled ASTAP solves "
                                    "(pointing_solve_policy, budget-capped)"),
    apply: bool = typer.Option(True, "--apply/--dry-run",
                               help="Apply the On-target check to the stored "
                                    "scorecards (human verdicts never change); "
                                    "--dry-run writes nothing and reports what "
                                    "would change"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-67: fill runs/<night>_pointing.jsonl from FITS headers (RC16), the
    mount log or the RC16 neighbours (Piggy-600), and apply the off-target
    check. Header-only reads: milliseconds per sub. Stop PhotonScript or run
    in daytime: it rewrites the subs log when a verdict changes. PS-107:
    without a plate solve only a gross miss (pointing_header_reject_deg)
    rejects; subs rejected earlier by a header-only offset under it flip
    back ("back from a header reject").

    photonscript pointing-backfill --since 2026-09-18 --dry-run
    """
    import json as _json
    from photonscript.scheduler.pointing_record import night_pass
    from photonscript.scheduler.runs import runs_dir
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if date:
        nights = [date]
    else:
        nights = sorted(p.name[:10] for p in runs_dir(cfg).glob("*_subs.jsonl")
                        if p.name[:10] >= (since or "0000"))
    results = []
    for d in nights:
        r = night_pass(cfg, d, solve=solve, apply=apply)
        results.append(r)
        if not as_json:
            sm = r.get("summary") or {}
            m = sm.get("model") or {}
            would = "would be " if not apply else ""
            console.print(
                f"{d}{' (dry run)' if not apply else ''}: {r['subs']} subs, "
                f"{r['with_position']} with a position, "
                f"{r['written']} records {would}written, "
                f"{sm.get('off_target', 0)} off "
                f"target, {sm.get('flagged', 0)} flagged, "
                f"{r['verdicts_changed']} verdicts {would}changed "
                f"({r['newly_rejected']} newly rejected, "
                f"{r.get('un_rejected', 0)} back from a header reject)"
                + (f", solved {r['solved']}/{r['solve_attempts']}" if solve else "")
                + (f", mount vs solve median {m['median_arcmin']}' (n={m['n']})"
                   if m else ""), markup=False, highlight=False)
    if as_json:
        print(_json.dumps(results, indent=2, default=str))
    elif results:
        console.print(
            f"Total: {sum(r['verdicts_changed'] for r in results)} verdicts "
            f"{'would change' if not apply else 'changed'}, "
            f"{sum(r['newly_rejected'] for r in results)} newly rejected, "
            f"{sum(r.get('un_rejected', 0) for r in results)} "
            f"{'would flip' if not apply else 'flipped'} back from a "
            "header-only reject", markup=False, highlight=False)


@app.command("slew-backfill")
def slew_backfill(
    since: str = typer.Option("", help="First night (YYYY-MM-DD); with no "
                                       "--date, every night from here on"),
    date: str = typer.Option("", help="One night only (YYYY-MM-DD)"),
    apply: bool = typer.Option(True, "--apply/--dry-run",
                               help="Apply the Clear-of-RC16-moves check to "
                                    "the stored scorecards (human verdicts "
                                    "never change)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-13: judge every Piggy-600 sub against the night's RC16 moves (the
    mount log, else the RC16 frames' header RA/Dec) and apply the
    slew_straddle check. Stop PhotonScript or run in daytime: it rewrites
    the subs log when a verdict changes.

    photonscript slew-backfill --since 2026-09-18 --dry-run
    """
    import json as _json
    from photonscript.scheduler.runs import runs_dir
    from photonscript.scheduler.slew_gate import night_pass
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if date:
        nights = [date]
    else:
        nights = sorted(p.name[:10] for p in runs_dir(cfg).glob("*_subs.jsonl")
                        if p.name[:10] >= (since or "0000"))
    results = []
    for d in nights:
        r = night_pass(cfg, d, apply=apply)
        results.append(r)
        if not as_json and r["subs"]:
            rate = r.get("split_rate")
            console.print(
                f"{d}: {r['subs']} Piggy subs, {r['judged']} judged "
                f"({', '.join(f'{k} {v}' for k, v in r['src'].items()) or 'no data'}), "
                f"{r['straddled']} straddle an RC16 move"
                + (f" ({rate:.0%})" if rate is not None else "")
                + f", {r['verdicts_changed']} verdicts changed "
                f"({r['newly_rejected']} newly rejected)",
                markup=False, highlight=False)
    if as_json:
        print(_json.dumps(results, indent=2, default=str))


@app.command("guide-rms-backfill")
def guide_rms_backfill_cmd(
    date: str = typer.Option("", help="One night only (YYYY-MM-DD)"),
    since: str = typer.Option("", help="First night (YYYY-MM-DD); with no "
                                       "--date, every night from here on"),
    apply: bool = typer.Option(False, "--apply/--dry-run",
                               help="Write the recomputed guide_rms (default: "
                                    "dry run, report only; human verdicts "
                                    "never change)"),
    examples: bool = typer.Option(False, "--examples",
                                  help="Print a few old -> new values per night"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """Recompute guide_rms of subs graded before the window-RMS fix (they
    kept the rolling RMS at grading time, e.g. a constant 34.84" on the
    2026-10-12 RC16 subs) from the night's PHD2 guide log, or set it to none
    with guide_rms_frames 0 where PHD2 was not guiding. Run it where the
    PHD2 guide logs are (the scope PC); stop PhotonScript or run in daytime
    with --apply: it rewrites the subs log.

    photonscript guide-rms-backfill --date 2026-10-12
    photonscript guide-rms-backfill --since 2026-09-01 --apply
    """
    import json as _json
    from photonscript.scheduler.guide_rms_backfill import format_line, night_pass
    from photonscript.scheduler.runs import runs_dir
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if date:
        nights = [date]
    else:
        nights = sorted(p.name[:10] for p in runs_dir(cfg).glob("*_subs.jsonl")
                        if p.name[:10] >= (since or "0000"))
    results = []
    for d in nights:
        r = night_pass(cfg, d, apply=apply)
        results.append(r)
        if not as_json and r["subs"]:
            console.print(format_line(r), markup=False, highlight=False)
            for x in (r["examples"] if examples else []):
                console.print(f"    {x['file']}: {x['old']} -> {x['new']} "
                              f"({x['frames']} frames, {x['src']})",
                              markup=False, highlight=False)
    if as_json:
        print(_json.dumps(results, indent=2, default=str))


@app.command("dawn-file")
def dawn_file_cmd(
    night: str = typer.Option(..., "--night", help="The night (YYYY-MM-DD)"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Only show what would be named and "
                                      "approved (writes nothing, no solves)"),
    approve: bool = typer.Option(True, "--approve/--no-approve",
                                 help="--no-approve files without approving "
                                      "(default: auto_approve_at_dawn)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """Run the dawn filing (PS-157) of one night now: attribution (RC16
    timeline, per-sub solve, plan window), pointing and slew passes,
    auto-approve of QA-passing subs, Library build, filing record. The same
    code as the dawn shutdown and POST /api/runs/{date}/autofile. Human
    verdicts never change; re-running is safe. It rewrites the subs log:
    with PhotonScript running prefer the POST, else run it in daytime.

    photonscript dawn-file --night 2026-10-09 --dry-run
    """
    import json as _json

    from photonscript.scheduler import dawn_autofile as da
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if dry_run:
        r = da.preview_night(cfg, night)
        if as_json:
            print(_json.dumps(r, indent=2, default=str))
            return
        tl = r["timeline"]
        console.print(
            f"DRY RUN {night}: {r['subs']} subs; filing record "
            f"{'exists' if r['record'] else 'none'}; RC16 timeline names "
            f"{tl['named']} Piggy subs {tl['by_target'] or ''}, holds "
            f"{tl['held']}; {r['would_solve']} would be plate-solved; plan "
            f"window names {r['plan_fallback']['named']} "
            f"{r['plan_fallback']['skipped'] or ''}; would approve "
            f"{r['would_approve']} {r['by_target'] or ''} {r['by_rig'] or ''}"
            f"{'' if r['auto_approve_at_dawn'] else ' (auto_approve_at_dawn off)'}"
            f"; Piggy subs still '?': {r['piggy_still_unknown']}",
            markup=False, highlight=False)
        return
    rec = da.file_night(cfg, night, push=False,
                        approve=None if approve else False)
    if as_json:
        print(_json.dumps(rec, indent=2, default=str))
        return
    console.print(f"{night}: {rec['line']}", markup=False, highlight=False)
    for e in rec["errors"]:
        console.print(f"  step failed: {e}", markup=False, highlight=False)


@app.command("regrade")
def regrade(
    date: str = typer.Option("", help="One night (YYYY-MM-DD)"),
    since: str = typer.Option("", help="Every night folder from this date "
                                       "on (YYYY-MM-DD)"),
    all_nights: bool = typer.Option(False, "--all",
                                    help="Every night folder"),
    discard_manual: bool = typer.Option(
        False, "--discard-manual",
        help="The old wipe: delete the subs log, manual accept / reject / "
             "review verdicts included, and grade from scratch"),
    yes: bool = typer.Option(False, "--yes",
                             help="No confirm for --discard-manual"),
):
    """PS-141: re-measure and re-judge every sub of a night (the Runs page
    Re-grade night / Re-grade all), then the backfill post passes
    (attribution, pointing, Library). A sub a person decided keeps its
    verdict (and a target assigned by hand) unless --discard-manual. Runs in
    the foreground; stop PhotonScript or run in daytime (it rewrites the
    subs log).

    photonscript regrade --date 2026-09-26
    photonscript regrade --since 2026-07-28 --discard-manual
    """
    import time as _time
    from photonscript.scheduler import runs
    if bool(date) == bool(since or all_nights):
        raise typer.BadParameter("give --date D, or --since D / --all")
    if discard_manual and not yes:
        typer.confirm("Delete the subs log(s), manual verdicts included, "
                      "and re-grade from scratch?", abort=True)
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if date:
        res = runs.regrade_night(cfg, date, discard_manual=discard_manual)
        if not res["started"]:
            console.print(f"{date}: not started, {res['detail']}",
                          markup=False, highlight=False)
            raise typer.Exit(1)
        while runs._backfill_state.get(date, {}).get("running"):
            _time.sleep(2)
        st = runs._backfill_state.get(date, {})
        kept = st.get("regrade_kept") or {}
        console.print(
            f"{date} ({res['mode']}): {res['records']} record(s) before, "
            f"{kept.get('replaced', 0)} replaced in place, "
            f"{kept.get('manual_kept', 0)} manual verdict(s) kept"
            + (f"; last error: {st['last_error']}" if st.get("last_error")
               else ""), markup=False, highlight=False)
        return
    runs.start_regrade_all(cfg, since=since, discard_manual=discard_manual)
    while runs._regrade_all.get("running"):
        _time.sleep(2)
    s = runs.regrade_all_status()
    console.print(f"Re-grade all: {s.get('done', 0)} of {s.get('total', 0)} "
                  "night(s)" + (f"; last error: {s['last_error']}"
                                if s.get("last_error") else ""),
                  markup=False, highlight=False)


@app.command("pointing-bench")
def pointing_bench(
    date: str = typer.Option("", help="Night (YYYY-MM-DD); default last night"),
    n: int = typer.Option(10, help="Subs per rig to solve"),
):
    """PS-67: seconds per ASTAP solve and success rate on N RC16 and N
    Piggy-600 subs of one night (decides the dawn solve policy: switch
    PS_POINTING_SOLVE_POLICY=all if a solve costs under ~3 s)."""
    import json as _json
    from photonscript.scheduler.pointing_record import bench
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    print(_json.dumps(bench(cfg, date or _last_night(), n=n), indent=2))


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


@app.command("target-aliases")
def target_aliases_cmd(
    apply: bool = typer.Option(False, "--apply",
                               help="Rewrite the local subs logs only "
                                    "(default: dry run; never the Library)"),
    date: list[str] = typer.Option(
        [], help="Only these nights (YYYY-MM-DD, repeatable; default all)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """2026-10-14 alias hygiene: report the '?' / *_Container / loop names
    the target pages no longer list as "also recorded as", and re-attribute
    any sub still stored under a container name, or still unattributed where
    the RC16 target imaged at that time names it (PS-51). Dry run unless
    --apply; --apply rewrites only the subs logs (target_raw keeps the old
    name), never moves a Library file. Afterwards: POST
    /api/projects2/recount."""
    import json as _json

    from photonscript.scheduler.target_alias_report import run
    from photonscript.shared.config import PhotonScriptConfig

    r = run(PhotonScriptConfig(), dates=date or None, apply=apply)
    if as_json:
        console.print_json(_json.dumps(r))
        return
    title = "APPLIED" if r["applied"] else "DRY RUN"
    t = Table(title=f"{title} - target aliases ({r['nights_scanned']} nights)")
    for c in ("Target", "Re-attributed", "Hidden aliases (subs)"):
        t.add_column(c)
    names = sorted(set(r["reattributed_by_target"])
                   | set(r["hidden_aliases_by_target"]))
    for n in names:
        h = r["hidden_aliases_by_target"].get(n, {})
        t.add_row(n, str(r["reattributed_by_target"].get(n, 0)),
                  "; ".join(f"{k} ({v})" for k, v in h.items()) or "-")
    console.print(t)
    for n in r["nights_changed"]:
        for x in n["renamed"]:
            console.print(f"  {n['date']} {x['rig']}: {x['from']} -> "
                          f"{x['to']} ({x['subs']})")
        for name, k in n["correlated"].items():
            console.print(f"  {n['date']} piggyback: ? -> {name} ({k}, "
                          "RC16 at that time)")
    console.print(f"Re-attributed: {r['reattributed']} (accepted, refiled by "
                  f"the next build_library: {r['accepted_to_refile']}); "
                  f"hidden alias subs: {r['hidden_alias_subs']}; still "
                  f"unattributed: {r['still_unattributed']}")
    if r["applied"]:
        console.print("[green]Done (subs logs only). Now resync goals: POST "
                      "/api/projects2/recount[/green]")
    else:
        console.print("[yellow]Dry run only. Add --apply to write.[/yellow]")


@app.command("piggy-attribution")
def piggy_attribution_cmd(
    date: list[str] = typer.Option(
        [], help="Only these nights (YYYY-MM-DD, repeatable; default all)"),
    apply: bool = typer.Option(False, "--apply",
                               help="Rename and move Library links "
                                    "(default: dry run)"),
    solve: bool = typer.Option(False, "--solve",
                               help="Plate-solve Piggy subs nothing places "
                                    "yet (ASTAP, stored for reuse)"),
    max_solves: int = typer.Option(30, help="Solve cap per night"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-137: name Piggy-600 subs after the goal their own frame holds
    (plate solve, else the mount position; Piggy-driven goals first) instead
    of the RC16's target name, e.g. the 2026-09-21 M31 subs filed as
    "Crescent Nebula". Dry run unless --apply; with --apply the subs logs
    are rewritten (raw name kept in target_raw) and the Library links move
    to the new target folder on this machine (collisions reported, nothing
    deleted, no FITS written). Afterwards: POST /api/projects2/recount.

    photonscript piggy-attribution --date 2026-09-21 --solve
    """
    import json as _json

    from photonscript.scheduler.piggy_attribution import reattribute
    from photonscript.shared.config import PhotonScriptConfig

    r = reattribute(PhotonScriptConfig(), date or [], apply=apply,
                    solve=solve, max_solves=max_solves)
    if as_json:
        console.print_json(_json.dumps(r))
        return
    title = "APPLIED" if r["applied"] else "DRY RUN"
    t = Table(title=f"{title} - PS-137 Piggy attribution "
                    f"({r['nights_scanned']} nights scanned)")
    for c in ("Night", "Piggy subs", "Placed", "Kept", "No goal in frame",
              "No position", "Change"):
        t.add_column(c)
    for n in r["nights"]:
        ch = "; ".join(f"{c['from']} -> {c['to']} ({c['subs']}, {c['src']})"
                       for c in n["changes"]) or "-"
        t.add_row(n["date"], str(n["piggy"]), str(n["placed"]), str(n["kept"]),
                  str(n["no_goal"]), str(n["no_position"]), ch)
    console.print(t)
    console.print(f"Subs re-attributed: {r['subs_changed']}; Library "
                  f"{r['library']}: {len(r['library_moves'])} link(s) "
                  f"{'moved' if r['applied'] else 'to move'}, "
                  f"{len(r['library_collisions'])} collision(s)")
    for c in r["library_collisions"]:
        console.print(f"  [yellow]collision (left in place): {c['from']} -> "
                      f"{c['to']}{' ' + c['error'] if c.get('error') else ''}"
                      "[/yellow]")
    for c in r.get("library_skipped") or []:   # PS-147
        console.print(f"  links kept: {c['date']} {c['file']} is now "
                      f"{c['now'] or 'gone from the log'} (not {c['to']})")
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


REMOTE_DEFAULT_URL = "https://teles-feb25.lobster-bleak.ts.net"


def remote_run(url: str, token: str, args: list, *, confirm_write: bool = False,
               timeout_s: float = 300, wait: bool = True, poll_s: float = 2.0,
               client=None, out=None, err=None) -> int:
    """POST /api/cli/run, then (wait) poll GET /api/cli/jobs/{id} and print
    the output. Returns the exit code to leave with: the remote exit code,
    2 when refused or unreachable, 124 on the remote hard timeout. The token
    goes only into the X-PhotonScript-Token header and is never printed."""
    import time as _time
    import httpx
    out = out or sys.stdout
    err = err or sys.stderr
    base = url.rstrip("/")
    own = client is None
    cl = client or httpx.Client(timeout=30)
    hdr = {"X-PhotonScript-Token": token}
    try:
        body = {"args": list(args), "timeout_s": timeout_s}
        if confirm_write:
            body["confirm"] = "write"
        try:
            r = cl.post(f"{base}/api/cli/run", json=body, headers=hdr)
        except Exception as e:  # noqa: BLE001
            print(f"remote: {base} unreachable ({type(e).__name__})", file=err)
            return 2
        if r.status_code != 202:
            try:
                detail = r.json().get("detail")
            except Exception:  # noqa: BLE001
                detail = r.text[:300]
            print(f"remote: refused ({r.status_code}): {detail}", file=err)
            return 2
        job = r.json()
        if not wait:
            print(f"remote job {job['id']} started ({job['class']}): "
                  f"GET {base}/api/cli/jobs/{job['id']}", file=out)
            return 0
        deadline = _time.monotonic() + timeout_s + 120
        while True:
            _time.sleep(poll_s)
            try:
                r = cl.get(f"{base}/api/cli/jobs/{job['id']}", headers=hdr,
                           params={"tail": 262144})
                r.raise_for_status()
                job = r.json()
            except Exception as e:  # noqa: BLE001
                if _time.monotonic() > deadline:
                    print(f"remote: lost job {job['id']} ({type(e).__name__})", file=err)
                    return 2
                continue
            if job.get("status") != "running":
                break
            if _time.monotonic() > deadline:
                print(f"remote: job {job['id']} still running past its timeout", file=err)
                return 2
        if job.get("stdout"):
            out.write(job["stdout"])
        if job.get("stderr"):
            err.write(job["stderr"])
        if job.get("truncated"):
            print("remote: output truncated to the last bytes kept (stdout "
                  f"{job.get('stdout_bytes')} B, stderr {job.get('stderr_bytes')} B)",
                  file=err)
        if job.get("status") == "timeout":
            return 124
        code = job.get("exit_code")
        return int(code) if isinstance(code, int) else 2
    finally:
        if own:
            cl.close()


@app.command("remote", context_settings={"allow_extra_args": True,
                                         "ignore_unknown_options": True})
def remote_cmd(
    args: List[str] = typer.Argument(..., help="The photonscript command and its "
                                     "flags, e.g. frame-check --last 3 --compare"),
    url: str = typer.Option(REMOTE_DEFAULT_URL, "--url", envvar="PS_REMOTE_CLI_URL",
                            help="Scope scheduler (default the tailscale serve URL)"),
    wait: bool = typer.Option(True, "--wait/--no-wait",
                              help="Wait for the job and print its output (default)"),
    confirm_write: bool = typer.Option(False, "--confirm-write",
                                       help="Needed for write commands (settings-"
                                            "snapshot, unguided-limits --refresh, ...)"),
    timeout_s: float = typer.Option(300, "--remote-timeout",
                                    help="Hard timeout on the scope PC (s, max 1800)"),
):
    """Run an allowlisted photonscript command ON THE SCOPE PC (POST
    /api/cli/run) and print its output; exits with the remote exit code.
    Needs PS_REMOTE_CLI_TOKEN in this machine's .env (the scope PC's value).
    Put the remote command after `--` so its flags never clash with these
    options. The allowlist is config/remote_cli.toml.

    photonscript remote -- frame-check --last 3 --type SNAPSHOT --compare
    photonscript remote --confirm-write -- unguided-limits --refresh
    """
    import os as _os
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    token = (getattr(cfg, "remote_cli_token", "")
             or _os.environ.get("PS_REMOTE_CLI_TOKEN", "")).strip()
    if not token:
        console.print("PS_REMOTE_CLI_TOKEN is not set in this machine's .env "
                      "(docs/HANDBOOK.md, Remote CLI)", markup=False)
        raise typer.Exit(2)
    raise typer.Exit(remote_run(url, token, list(args), confirm_write=confirm_write,
                                timeout_s=timeout_s, wait=wait))


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


@app.command("calibration-library")
def calibration_library_cmd(
    rig: str = typer.Option("rc16", "--rig", help="rc16 | piggyback"),
    url: str = typer.Option("", "--url",
                            help="Ask this scheduler (GET /api/calibration/library-report), "
                                 "e.g. http://100.94.189.77:8100; default: compute here"),
    desktop_library: str = typer.Option("", "--desktop-library",
                                        help="Also compare with this Library mirror (default "
                                             "with --url: the integrate Library mirror)"),
    frames: int = typer.Option(0, "--frames", help="Also list up to N single frames"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-178: calibration frames on the scope (NINA watch dir) vs in the
    Library, the reason each one is not there (QA quarantine, not filed:
    calibration-only folder, too old, archived, waiting for dawn) and
    whether a dark / bias fits the lights' epoch (gain, offset, SET-TEMP,
    readout). With --url on the desktop it also says which Library frames
    the mirror still lacks (Syncthing pending). Read only.

    photonscript calibration-library --url http://100.94.189.77:8100
    """
    import json as _json
    from photonscript.scheduler import calibration_library as cl
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if url:
        import httpx
        try:
            r = httpx.get(url.rstrip("/") + "/api/calibration/library-report",
                          params={"rig": rig, "frames": frames}, timeout=120)
            r.raise_for_status()
            rep = r.json()
        except Exception as e:  # noqa: BLE001
            console.print(f"scheduler unreachable or refused ({e})", markup=False)
            raise typer.Exit(2)
        if not desktop_library:
            from photonscript.integration.pipeline import default_library
            desktop_library = str(default_library(cfg))
    else:
        rep = cl.library_report(cfg, rig, frames_limit=frames)
    if desktop_library:
        cl.compare_desktop(rep, Path(desktop_library))
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
        return
    console.print(cl.format_report(rep), markup=False, highlight=False)
    for f in rep.get("frames") or []:
        console.print(f"  {f['type']}/{f['date']}/{f['name']}: {f['status']} {f['reason']}",
                      markup=False, highlight=False)


@app.command("calibration-status")
def calibration_status_cmd(
    rig: str = typer.Option("", "--rig", help="rc16 | piggyback (default: every rig)"),
    url: str = typer.Option("", "--url",
                            help="Ask this scheduler (GET /api/calibration/completeness), "
                                 "e.g. http://100.94.189.77:8100; default: compute here"),
    mirror: str = typer.Option("", "--mirror",
                               help="Also check this Library mirror (default with --url: "
                                    "the integrate Library mirror, D:/ninashare/Library)"),
    nights: int = typer.Option(0, "--nights", help="Light sets of the last N nights "
                                                   "(default calibration_completeness_nights)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-182: calibration completeness. For every light set (target, filter,
    exposure, gain, offset, SET-TEMP, readout) of the last nights and
    tonight's plan: are its bias, darks and flats captured, QA-passed, in
    the Library on the scope and (desktop, --url) in the mirror? The first
    stage that is short and why, the darks windows the cooler can serve,
    the bias plan and whether the darks are light-tight. Exit 1 when
    anything is missing. Read only.

    photonscript calibration-status --url http://100.94.189.77:8100
    """
    import json as _json
    from photonscript.scheduler import calibration_completeness as cc
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    if url:
        import httpx
        params = {"names": "true"}
        if rig:
            params["rig"] = rig
        if nights:
            params["nights"] = nights
        try:
            r = httpx.get(url.rstrip("/") + "/api/calibration/completeness",
                          params=params, timeout=300)
            r.raise_for_status()
            rep = r.json()
        except Exception as e:  # noqa: BLE001
            console.print(f"scheduler unreachable or refused ({e})", markup=False)
            raise typer.Exit(2)
        if not mirror:
            from photonscript.integration.pipeline import default_library
            mirror = str(default_library(cfg))
    else:
        rep = cc.completeness(cfg, rig or None, names=bool(mirror),
                              nights=nights or None)
    if mirror:
        cc.apply_mirror(rep, Path(mirror))
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(cc.format_report(rep), markup=False, highlight=False)
    if not all(r.get("complete") for r in rep.get("rigs") or []):
        raise typer.Exit(1)


def _vivid_overrides(base: dict, vivid, sat_mid: float, hue_boost: float, lhe: float) -> dict:
    """The vivid-variant settings: base (pjsr.vivid_settings of the config)
    with the CLI values that were given (None / negative = keep). Raises
    ValueError (PjsrError) on an out-of-range value."""
    from photonscript.integration import pjsr
    out = {k: base[k] for k in ("vivid", "vivid_sat_mid", "vivid_hue_boost", "vivid_lhe")}
    if vivid is not None:
        out["vivid"] = bool(vivid)
    if sat_mid >= 0:
        out["vivid_sat_mid"] = pjsr.vivid_sat_mid(sat_mid)
    if hue_boost >= 0:
        out["vivid_hue_boost"] = pjsr.unit_value("vivid_hue_boost", hue_boost)
    if lhe >= 0:
        out["vivid_lhe"] = pjsr.unit_value("vivid_lhe", lhe)
    return out


def _rc_overrides(rcastro: str, bxt_stars: float, bxt_nonstellar: float, nxt_denoise: float) -> dict:
    """PS-186: integrate's --rcastro / --bxt-* / --nxt-denoise over the config
    (blank / negative = keep the config value). Raises ValueError."""
    from photonscript.integration import pjsr
    out: dict = {}
    if rcastro:
        out["rc_mode"] = pjsr.rc_mode(rcastro)
    for k, v in (("bxt_stars", bxt_stars), ("bxt_nonstellar", bxt_nonstellar), ("nxt_denoise", nxt_denoise)):
        if v is not None and v >= 0:
            out[k] = pjsr.unit_value(k, v)
    return out


@app.command("calibration-file")
def calibration_file_cmd(
    url: str = typer.Option("", "--url",
                            help="The scope scheduler (POST /api/calibration/file), "
                                 "e.g. http://100.94.189.77:8100"),
    local: bool = typer.Option(False, "--local",
                               help="File on this machine instead (the scope PC only: "
                                    "it links into this config's Library)"),
    rig: str = typer.Option("", "--rig", help="rc16 | piggyback (default: both)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only: move, link and save nothing"),
    rejudge: bool = typer.Option(False, "--rejudge",
                                 help="Judge every QA record by today's rule first and move "
                                      "filed frames to match (quarantine <-> Library)"),
    regrade: bool = typer.Option(False, "--regrade", help="Re-measure every frame (slow)"),
    days: int = typer.Option(0, "--days", help="Folders of the last N days (default 14, "
                                               "at most library_cal_days)"),
    as_json: bool = typer.Option(False, "--json", help="Print the full JSON"),
):
    """PS-188: file calibration now instead of waiting for the dawn pass.
    Every recent calibration folder of both rigs: QA (cached; a new frame
    is measured unless the armer is imaging), failing frames to the
    quarantine, the rest linked into Library/Calibration for Syncthing.
    Prints what was filed, quarantined and left, and why. Exit 1 when
    anything is left.

    photonscript calibration-file --url http://100.94.189.77:8100 --dry-run
    photonscript calibration-file --url http://100.94.189.77:8100 --rejudge
    """
    import json as _json
    from photonscript.scheduler import calibration_filing as cf
    if not url and not local:
        console.print("give --url of the scope scheduler (or --local on the scope PC)",
                      markup=False)
        raise typer.Exit(2)
    if url:
        import httpx
        body = {"dry_run": dry_run, "rejudge": rejudge, "regrade": regrade,
                "source": "cli"}
        if rig:
            body["rig"] = rig
        if days:
            body["days"] = days
        try:
            r = httpx.post(url.rstrip("/") + "/api/calibration/file", json=body,
                           timeout=1800)
            rep = r.json()
            if r.status_code >= 400:
                console.print(f"scheduler refused ({r.status_code}): "
                              f"{rep.get('detail') or rep.get('error')}", markup=False)
                raise typer.Exit(2)
        except typer.Exit:
            raise
        except Exception as e:  # noqa: BLE001
            console.print(f"scheduler unreachable or refused ({e})", markup=False)
            raise typer.Exit(2)
    else:
        cfg = _config_for_repo(Path(__file__).resolve().parents[1])
        rep = cf.file_now(cfg, rigs=[rig] if rig else None, days=days or None,
                          dry_run=dry_run, regrade=regrade, rejudge=rejudge,
                          source="cli")
    if as_json:
        print(_json.dumps(rep, indent=1, default=str))
    else:
        console.print(cf.format_report(rep), markup=False, highlight=False)
    if any(r.get("error") or r.get("n_left") for r in (rep.get("rigs") or {}).values()):
        raise typer.Exit(1)


def _blend_nb_defaults() -> dict:
    """The narrowband blend weights (blend.NB_DEFAULTS) for the option defaults."""
    from photonscript.integration.blend import NB_DEFAULTS
    return NB_DEFAULTS


def _pilock_wait(cfg) -> float:
    """pixinsight_lock_wait_min (integration/pilock.py)."""
    from photonscript.integration import pilock
    return pilock.wait_min(cfg)


@app.command("integrate")
def integrate_cmd(
    target: str = typer.Option(..., "--target", help='Target name, catalog id or alias ("Andromeda Galaxy", "M31")'),
    rig: str = typer.Option("piggyback", "--rig", help="piggyback (Piggy-600 OSC) | rc16 (mono, per filter)"),
    since: str = typer.Option("", "--since", help="First night (evening date YYYY-MM-DD)"),
    until: str = typer.Option("", "--until", help="Last night (evening date YYYY-MM-DD)"),
    out: str = typer.Option("", "--out", help="Run folder (NEW or empty; default <staging-root>/<target>_<rig>_<time>)"),
    staging_root: str = typer.Option("", "--staging-root",
                                     help=r"Default D:\Astrophotography\Staging, else ~\Astrophotography\Staging"),
    library: str = typer.Option("", "--library", help="Library mirror (read-only; default integration_library_dir, "
                                                       "else the first that exists of D:/ninashare/Library and "
                                                       "desktop_library_dir)"),
    filters: str = typer.Option("", "--filters", help="Mono: comma list of filters (default all)"),
    qa: str = typer.Option("report", "--qa", help="Star QA: report (stack all, default) | apply (drop rejects) | off"),
    flats: bool = typer.Option(True, "--flats/--no-flats", help="Use matched flats when present"),
    flat_temp_tol: float = typer.Option(5.0, "--flat-temp-tol",
                                        help="Flats whose sensor is within this many C of the lights "
                                             "(default 5; wider for short sky flats shot uncooled)"),
    min_darks: int = typer.Option(10, "--min-darks", help="Darks needed to use a dark length"),
    max_cal: int = typer.Option(50, "--max-cal", help="At most this many bias / darks / flats per master"),
    limit: int = typer.Option(0, "--limit", help="At most N subs per filter + exposure (small smoke runs)"),
    pixinsight: bool = typer.Option(True, "--pixinsight/--no-pixinsight",
                                    help="Run PixInsight (else stage + scripts only)"),
    finish: bool = typer.Option(True, "--finish/--no-finish", help="Run the finish script after integrating"),
    pixinsight_exe: str = typer.Option(r"C:\Program Files\PixInsight\bin\PixInsight.exe", "--pixinsight-exe"),
    gradient: str = typer.Option("auto", "--gradient", help="auto | abe | none"),
    no_rc: bool = typer.Option(False, "--no-rc", help="Skip the RC Astro tools (same as --rcastro off)"),
    rcastro: str = typer.Option("", "--rcastro",
                                help="BlurX / NoiseX / StarXTerminator in the finish: auto | on | off "
                                     "(default finish_use_rcastro; PS-186)"),
    bxt_stars: float = typer.Option(-1.0, "--bxt-stars",
                                    help="BlurXTerminator sharpen stars 0..1 (default bxt_sharpen_stars)"),
    bxt_nonstellar: float = typer.Option(-1.0, "--bxt-nonstellar",
                                         help="BlurXTerminator sharpen nonstellar 0..1 "
                                              "(default bxt_sharpen_nonstellar)"),
    nxt_denoise: float = typer.Option(-1.0, "--nxt-denoise",
                                      help="NoiseXTerminator denoise 0..1 (default nxt_denoise)"),
    hoo: bool = typer.Option(False, "--hoo/--no-hoo",
                             help="OSC finish: also the HOO-mapped image (Ha = R, OIII = mean G, B; PS-161)"),
    palette: str = typer.Option("", "--palette",
                                help="OSC finish: also a palette image, off | gold (gold-blue: "
                                     "<name>_gold.{xisf,jpg}; default finish_palette, off; the configured "
                                     "palette only applies to finish_palette_kinds targets)"),
    vivid: Optional[bool] = typer.Option(None, "--vivid/--no-vivid",
                                         help="OSC finish: also <name>_vivid.{xisf,tif,jpg} (more saturation + "
                                              "a pink / warm / blue lift through a mask; default finish_vivid, on)"),
    vivid_sat_mid: float = typer.Option(-1.0, "--vivid-sat-mid",
                                        help="Vivid saturation midpoint 0.5..0.95 "
                                             "(default finish_vivid_sat_mid, 0.72)"),
    vivid_hue_boost: float = typer.Option(-1.0, "--vivid-hue-boost",
                                          help="Vivid pink / warm / blue chroma lift 0..1 "
                                               "(default finish_vivid_hue_boost, 0.25)"),
    vivid_lhe: float = typer.Option(-1.0, "--vivid-lhe",
                                    help="Vivid local contrast (LocalHistogramEqualization) amount 0..1, 0 = off "
                                         "(default finish_vivid_lhe, 0)"),
    workers: int = typer.Option(0, "--workers", help="Star QA processes (0 = auto)"),
    min_group: int = typer.Option(3, "--min-group-frames",
                                  help="Mono: an exposure group with fewer subs is left out "
                                       "(each exposure length is its own stack, PS-177)"),
    allow_uncalibrated: bool = typer.Option(False, "--allow-uncalibrated",
                                            help="Stack a light length that has no dark "
                                                 "(refused by default, PS-178)"),
    keep_focuser_zero: bool = typer.Option(False, "--keep-focuser-zero",
                                           help="Stack lights whose FOCPOS is 0 (focuser disconnected "
                                                "or frozen; left out by default in every --qa mode)"),
    pointing: str = typer.Option("", "--pointing",
                                 help="Pointing sanity of the lights (header RA / DEC vs the catalog, "
                                      "altitude, mount not tracking): warn | refuse | off "
                                      "(default integrate_pointing_check, warn)"),
    hdr: Optional[bool] = typer.Option(None, "--hdr/--no-hdr",
                                       help="Mono: HDR combine a filter's exposure-length masters into "
                                            "master_<F>_hdr (default integrate_hdr_combine, on). OSC: the "
                                            "core combine from the short subs into master_OSC_hdr "
                                            "(default integrate_osc_hdr_core, on)"),
    mono_color: Optional[bool] = typer.Option(None, "--mono-color/--no-mono-color",
                                              help="Mono: after the finish, combine the filter masters into "
                                                   "<target>_LRGB / _RGB / _SHO / _HOO (default "
                                                   "integrate_mono_color, on)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Select, QA and match only; write nothing"),
    use_scratch: bool = typer.Option(True, "--scratch/--no-scratch",
                                     help="Build the run on integration_scratch_root (when set) and "
                                          "archive it to the staging root; --no-scratch = build on "
                                          "the staging root"),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
    report: bool = typer.Option(True, "--report/--no-report",
                                help="Post the run's ledger to the scheduler (PS-33; queued when unreachable)"),
    report_url: str = typer.Option("", "--report-url", help="Scheduler (default integration_report_url)"),
):
    """PS-22: stack a target from the PhotonScript library (desktop).

    Selects the approved subs from the Library mirror (read-only), runs the
    per-sub star QA (incl. the second-star-set check), matches bias / darks
    / flats, COPIES everything into a new staging run folder with a
    manifest, generates and runs the PixInsight integration and finish
    (one instance at a time), and writes an AstroBin CSV + packet draft.

    photonscript integrate --target "Andromeda Galaxy" --rig piggyback
    """
    import json as _json
    from photonscript.integration import pipeline as pl
    from photonscript.integration import report as rp
    from photonscript.integration.runner import PixInsightBusy
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    root = Path(staging_root) if staging_root else pl.default_staging_root(cfg)
    try:
        kw = pl.config_options(cfg, rig)
        kw.update(_rc_overrides(rcastro, bxt_stars, bxt_nonstellar, nxt_denoise))
        if palette:
            from photonscript.integration import pjsr as _pjsr
            kw["palette"] = _pjsr.palette_mode(palette)
            kw["palette_kinds"] = ""      # an explicit --palette always applies
        kw.update(_vivid_overrides(kw, vivid, vivid_sat_mid, vivid_hue_boost, vivid_lhe))
    except ValueError as e:
        console.print(f"[red]integrate:[/red] {e}", markup=True)
        raise typer.Exit(2)
    if library:
        kw["library"] = Path(library)
    if not use_scratch:
        kw["scratch_root"] = None
    if hdr is not None:
        kw["hdr_combine"] = hdr
        kw["osc_hdr_core"] = hdr
    if mono_color is not None:
        kw["mono_color"] = mono_color
    if pointing:
        kw["pointing_check"] = pointing.strip().lower()
    opts = pl.Options(
        target=target, rig=rig, since=since, until=until,
        staging_root=root, out=Path(out) if out else None,
        filters=[f.strip() for f in filters.split(",") if f.strip()] or None,
        qa=qa, flats=flats, flat_temp_tol=flat_temp_tol, min_darks=min_darks, max_cal=max_cal, limit=limit, run_pixinsight=pixinsight, finish=finish,
        dry_run=dry_run, pixinsight=pixinsight_exe, workers=workers or None,
        gradient=gradient, use_rc=not no_rc, hoo=hoo, min_group_frames=min_group,
        allow_uncalibrated=allow_uncalibrated, keep_focuser_zero=keep_focuser_zero, **kw,
    )
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    try:
        res = pl.run(opts, echo=say)
    except (pl.PipelineError, PixInsightBusy, FileNotFoundError, ValueError) as e:
        console.print(f"[red]integrate:[/red] {e}", markup=True)
        raise typer.Exit(1)
    # PS-33: post the ledger (and any queued one); a down scope only warns
    if report and res.get("ledger"):
        url = report_url or getattr(cfg, "integration_report_url", "")
        say("== report the ledger to the scheduler")
        rp.sweep(root, url, echo=say, scratch_root=opts.scratch_root)
        if Path(res["ledger"]).parent.parent != root:
            r = rp.post_ledger(Path(res["ledger"]), url)
            say(("  posted " + r.get("detail", "")) if r["ok"]
                else f"  WARNING: ledger queued, not posted ({r['detail']})")
    if as_json:
        print(_json.dumps(res, indent=1, default=str))
    ok = res.get("integration", {}).get("ok", True) and res.get("finish", {}).get("ok", True)
    raise typer.Exit(0 if ok else 1)


@app.command("integrate-report")
def integrate_report_cmd(
    staging_root: str = typer.Option("", "--staging-root", help="Default integration_staging_root"),
    url: str = typer.Option("", "--url", help="Scheduler (default integration_report_url)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List the queued ledgers, post nothing"),
):
    """PS-33: post every queued ledger (run folders whose ledger.json says
    reported: false) to the scheduler. Safe to repeat; a down scope only
    warns and leaves them queued.

    photonscript integrate-report --dry-run
    """
    from photonscript.integration import pipeline as pl
    from photonscript.integration import report as rp
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    root = Path(staging_root) if staging_root else pl.default_staging_root(cfg)
    from photonscript.integration import scratch as sc
    res = rp.sweep(root, url or getattr(cfg, "integration_report_url", ""),
                   echo=lambda s: console.print(s, markup=False, highlight=False), dry_run=dry_run,
                   scratch_root=sc.configured_root(cfg))
    console.print(f"{res['pending']} queued, {len(res['posted'])} posted, "
                  f"{len(res['failed'])} still queued", markup=False)


@app.command("scratch-status")
def scratch_status_cmd(
    root: str = typer.Option("", "--root", help="Scratch root (default integration_scratch_root, "
                                                "else C:/Astro/Scratch)"),
    legacy_root: str = typer.Option("C:/Astro/Staging", "--legacy-root",
                                    help="Legacy --out run folders ('' = skip)"),
    staging_root: str = typer.Option("", "--staging-root",
                                     help="Archive root for the twin check (default integration_staging_root)"),
    clean: str = typer.Option("", "--clean", help="Delete this ONE listed run folder (asks first)"),
    yes: bool = typer.Option(False, "--yes", help="With --clean: do not ask"),
    as_json: bool = typer.Option(False, "--json", help="Print the list as JSON"),
):
    """Run folders left on the fast scratch disk (and legacy --out runs under
    C:/Astro/Staging): size, age, master / final, archive twin under the
    staging root, and the exact Remove-Item line per folder. --clean <name>
    deletes only that folder inside those roots, after showing its size and
    asking (y/N) unless --yes.

    photonscript scratch-status
    photonscript scratch-status --clean M31_piggyback_20261011-2200
    """
    import json as _json
    from photonscript.integration import pipeline as pl
    from photonscript.integration import scratch as sc
    from photonscript.integration import scratch_status as ss
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    sroot = Path(root) if root else (sc.configured_root(cfg) or ss.DEFAULT_SCRATCH)
    staging = Path(staging_root) if staging_root else pl.default_staging_root(cfg)
    legacy = Path(legacy_root) if legacy_root.strip() else None
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    used = [r for r, _k in ss.roots(sroot, staging, legacy)]
    for r in (sroot, legacy):
        if r and r not in used:
            say(f"  {r}: not listed (missing, or it overlaps the staging root {staging})")
    folders = ss.scan(sroot, staging, legacy)
    if clean:
        hits = ss.find(clean, folders)
        if len(hits) != 1:
            say(f"scratch-status: {clean!r} is " + ("not a listed run folder" if not hits else
                "ambiguous: " + ", ".join(str(h.path) for h in hits)))
            raise typer.Exit(2)
        f = hits[0]
        try:
            ss.check(f, staging, [pl.default_library(cfg)])
        except ValueError as e:
            say(f"scratch-status: {e}")
            raise typer.Exit(2)
        say(f"{f.path}: {sc.gb(f.size)}, {ss.age_text(f.age_days)} old, "
            + (f"archived to {f.archive}" if f.archive else "NOT archived")
            + ("" if f.product or f.archive else ", no master"))
        if f.product and not f.archive:
            say("  WARNING: it holds a master / final with no archive twin: deleting loses it")
        if not yes and not typer.confirm(f"Delete {f.path}?", default=False):
            say("  not deleted")
            raise typer.Exit(1)
        try:
            n = ss.clean(f, staging, [pl.default_library(cfg)])
        except (OSError, ValueError) as e:
            say(f"scratch-status: not deleted: {e}")
            raise typer.Exit(1)
        say(f"  deleted, {sc.gb(n)} freed")
        return
    if as_json:
        print(_json.dumps([f.as_dict() for f in folders], indent=1))
        return
    if not folders:
        say("no run folders left in " + ", ".join(str(r) for r in used or [sroot]))
        return
    for line in ss.format_rows(folders):
        say(line)
    say(f"{len(folders)} folder(s), {sc.gb(sum(f.size for f in folders))}")


@app.command("restage")
def restage_cmd(
    run: str = typer.Argument(..., help="Archived integrate run folder (has manifest.json)"),
    out: str = typer.Option("", "--out", help="NEW or empty folder (default <scratch root, else "
                                              "staging root>/<run>_restage_<time>)"),
    library: str = typer.Option("", "--library",
                                help="Library mirror the sources moved to (default: the manifest's)"),
    pixinsight_exe: str = typer.Option(r"C:\Program Files\PixInsight\bin\PixInsight.exe", "--pixinsight-exe"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan and check only; copy nothing"),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
):
    """Rebuild an archived run's LIGHTS / BIAS / DARKS / FLATS copies from
    manifest.json frames[].source (the Library mirror, read only) into a NEW
    or empty folder, sizes verified, with the run's scripts rewritten to it;
    then prints the PixInsight integrate / finish lines to rerun it.

    photonscript restage D:/Astrophotography/Staging/M31_piggyback_20261011-2200
    """
    import json as _json
    from photonscript.integration import pipeline as pl
    from photonscript.integration import restage as rs
    from photonscript.integration import scratch as sc
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    run_dir = Path(run)
    base = sc.configured_root(cfg) or pl.default_staging_root(cfg)
    dest = Path(out) if out else rs.default_out(run_dir, base)
    lib = Path(library) if library else None
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    try:
        p = rs.plan(run_dir, dest, lib)
        guard = lib or pl.default_library(cfg)
        rs.check_out(dest, run_dir, guard)
        if dry_run:
            say(f"dry run: {len(p.items)} frames, {sc.gb(p.total)} would be copied into {dest}")
            for e in p.errors:
                say(f"  ERROR: {e}")
            raise typer.Exit(1 if p.errors else 0)
        res = rs.restage(p, echo=say, library=guard)
    except rs.RestageError as e:
        console.print(f"[red]restage:[/red] {e}", markup=True)
        raise typer.Exit(2)
    if as_json:
        print(_json.dumps(res, indent=1, default=str))
    if res["errors"]:
        say("restage INCOMPLETE: fix the errors above before rerunning")
        raise typer.Exit(1)
    say("== rerun (PowerShell; one PixInsight at a time):")
    for line in rs.rerun_commands(res, pixinsight_exe, run_dir.name):
        say(f"  {line}")


@app.command("blend")
def blend_cmd(
    target: str = typer.Option(..., "--target", help='Target name, catalog id or alias ("M31")'),
    osc: str = typer.Option("", "--osc", help="Piggy-600 OSC master (default: newest piggyback integrate run)"),
    rc16: str = typer.Option("", "--rc16", help="RC16 master(s), comma list (default: newest rc16 integrate run)"),
    stage: str = typer.Option("linear", "--stage", help="linear (finish *_linear / raw masters) | final (stretched)"),
    weight: float = typer.Option(0.7, "--weight", help="RC16 share of L inside the mask (0..1)"),
    feather: float = typer.Option(0.08, "--feather", help="Mask feather, fraction of the RC16 footprint's short side"),
    inset: float = typer.Option(0.02, "--inset", help="Mask inset from the RC16 edge, same unit"),
    lum_mask: bool = typer.Option(False, "--lum-mask/--no-lum-mask", help="Blend only bright structure"),
    core: bool = typer.Option(True, "--core/--no-core", help="Also make the RC16-scale core crop with OSC color"),
    out: str = typer.Option("", "--out", help="Blend folder (NEW or empty; default <staging-root>/Blend/<target>_blend_<time>)"),
    staging_root: str = typer.Option("", "--staging-root", help="Default integration_staging_root"),
    keep_work: bool = typer.Option(False, "--keep-work", help="Keep the registration work files"),
    rc16_bxt: str = typer.Option("", "--rc16-bxt",
                                 help="BlurXTerminator pass on the RC16 L linear master first: auto | on | off "
                                      "(default blend_rc16_bxt + finish_use_rcastro; PS-186)"),
    mode: str = typer.Option("auto", "--mode",
                             help="l (RC16 L core) | nb (RC16 Ha / OIII / SII: HaRGB + OIII, gold, SHO / HOO "
                                  "core) | auto (nb when the RC16 run has an Ha master)"),
    ha_weight: float = typer.Option(_blend_nb_defaults()["ha_weight"], "--ha-weight",
                                    help="nb: Ha model share of the OSC red inside the mask (0..1)"),
    oiii_weight: float = typer.Option(_blend_nb_defaults()["oiii_weight"], "--oiii-weight",
                                      help="nb: OIII model share of the OSC green and blue (0..1)"),
    ha_boost: float = typer.Option(_blend_nb_defaults()["ha_boost"], "--ha-boost",
                                   help="nb: extra Ha line on top of red (0..1; 0 = replace only)"),
    oiii_boost: float = typer.Option(_blend_nb_defaults()["oiii_boost"], "--oiii-boost",
                                     help="nb: extra OIII line on top of green and blue (0..1)"),
    nb_lum_weight: float = typer.Option(_blend_nb_defaults()["nb_lum_weight"], "--nb-lum-weight",
                                        help="nb: Ha share of CIE L inside the mask (an RC16 L uses --weight)"),
    palette: str = typer.Option("gold", "--palette", help="nb: gold = also <name>_haRGB_gold | off"),
    vivid: Optional[bool] = typer.Option(None, "--vivid/--no-vivid",
                                         help="L blend: also <name>_blend_vivid.{xisf,tif,jpg} (the finish vivid "
                                              "look on the stretched blend; default blend_vivid, galaxies)"),
    pixinsight: bool = typer.Option(True, "--pixinsight/--no-pixinsight", help="Run PixInsight (else script only)"),
    pixinsight_exe: str = typer.Option(r"C:\Program Files\PixInsight\bin\PixInsight.exe", "--pixinsight-exe"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Find the inputs and check the script; write nothing"),
    use_scratch: bool = typer.Option(True, "--scratch/--no-scratch",
                                     help="Build the blend on integration_scratch_root (when set) and "
                                          "archive it to <staging-root>/Blend"),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
):
    """PS-153: blend the RC16 luminance core into the Piggy-600 color image
    (two-rig goals). Registers the RC16 L (else the mean of its LRGB
    masters) onto the OSC frame, matches background and scale inside a
    feathered mask of the RC16 footprint and replaces the OSC CIE L there
    (weight 0.7); also an RC16-scale core crop with the OSC color. Runs in a
    NEW folder under <staging-root>/Blend, one PixInsight at a time. The
    ledger (kind blend) stays on the desktop.

    --mode nb (auto picks it when the RC16 run has an Ha master, e.g. the
    Heart Nebula): Ha into the OSC red, OIII into green and blue
    (continuum-aware fits), Ha (or the RC16 L) into CIE L, the natural HaRGB
    and gold palette finishes, and RC16-scale SHO / HOO core crops with the
    OSC stars.

    photonscript blend --target M31 --dry-run
    """
    import json as _json
    from photonscript.integration import blend as bl
    from photonscript.integration import pipeline as pl
    from photonscript.integration import pjsr
    from photonscript.integration import scratch as sc
    from photonscript.integration.runner import PixInsightBusy
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    root = Path(staging_root) if staging_root else pl.default_staging_root(cfg)
    try:
        bxt = bl.config_bxt(cfg)
        if rc16_bxt:
            bxt["rc16_bxt"] = pjsr.rc_mode(rc16_bxt)
        viv = bl.config_vivid(cfg)
        if vivid is not None:
            viv["vivid"] = vivid
            if vivid:
                viv["vivid_kinds"] = ""          # an explicit --vivid applies to any target
    except ValueError as e:
        console.print(f"[red]blend:[/red] {e}", markup=True)
        raise typer.Exit(2)
    opts = bl.Options(
        target=target, staging_root=root, out=Path(out) if out else None,
        osc=Path(osc) if osc else None,
        rc16=[Path(x.strip()) for x in rc16.split(",") if x.strip()] or None,
        stage=stage, weight=weight, feather_frac=feather, inset_frac=inset, lum_mask=lum_mask,
        core=core, keep_work=keep_work, run_pixinsight=pixinsight, dry_run=dry_run,
        pixinsight=pixinsight_exe, scratch_root=sc.configured_root(cfg) if use_scratch else None,
        lock_wait_min=_pilock_wait(cfg),
        osc_scale=float(getattr(cfg, "piggyback_pixel_scale_arcsec", bl.OSC_SCALE) or bl.OSC_SCALE),
        rc16_scale=float(getattr(cfg, "pixel_scale_arcsec", bl.RC16_SCALE) or bl.RC16_SCALE),
        mode=mode, ha_weight=ha_weight, oiii_weight=oiii_weight, ha_boost=ha_boost, oiii_boost=oiii_boost,
        nb_lum_weight=nb_lum_weight, palette=palette, extra_roots=bl.extra_roots(cfg),
        **bxt, **viv,
    )
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    try:
        res = bl.run(opts, echo=say)
    except (bl.BlendError, pl.PipelineError, PixInsightBusy, FileNotFoundError, ValueError) as e:
        console.print(f"[red]blend:[/red] {e}", markup=True)
        raise typer.Exit(1)
    if as_json:
        print(_json.dumps(res, indent=1, default=str))
    raise typer.Exit(0 if res.get("blend", {}).get("ok", True) else 1)


@app.command("finish")
def finish_cmd(
    master: str = typer.Option("", "--master", help="The master to finish (.xisf, linear)"),
    name: str = typer.Option("", "--name", help="Output name: <name>_final.{xisf,tif,jpg} (--mono-run: "
                                               "<name>_LRGB ..., default the run's target)"),
    mono_run: str = typer.Option("", "--mono-run",
                                 help="Instead of --master: a mono (rc16) integrate run folder whose filter "
                                      "masters are combined into color images"),
    color: str = typer.Option("auto", "--color",
                              help="--mono-run: auto | all | comma list of lrgb, rgb, sho, hoo "
                                   "(auto: LRGB or RGB from R G B (+ L); SHO + HOO, or HOO without SII)"),
    target: str = typer.Option("", "--target", help="Catalog name for the plate solve seed "
                                                   "(default: the master's integrate run)"),
    rig: str = typer.Option("", "--rig", help="piggyback | rc16 (default: the master's integrate run, "
                                              "else piggyback)"),
    out: str = typer.Option("", "--out", help="Finish folder (NEW or empty; default "
                                              "<staging-root>/Finish/<name>_finish_<time>)"),
    staging_root: str = typer.Option("", "--staging-root", help="Default integration_staging_root"),
    palette: str = typer.Option("", "--palette", help="off | gold (color masters; default finish_palette, "
                                                      "which only applies to finish_palette_kinds targets; "
                                                      "--palette gold forces it)"),
    vivid: Optional[bool] = typer.Option(None, "--vivid/--no-vivid",
                                         help="OSC finish: also <name>_vivid.{xisf,tif,jpg} (more saturation + "
                                              "a pink / warm / blue lift through a mask; default finish_vivid, on)"),
    vivid_sat_mid: float = typer.Option(-1.0, "--vivid-sat-mid",
                                        help="Vivid saturation midpoint 0.5..0.95 "
                                             "(default finish_vivid_sat_mid, 0.72)"),
    vivid_hue_boost: float = typer.Option(-1.0, "--vivid-hue-boost",
                                          help="Vivid pink / warm / blue chroma lift 0..1 "
                                               "(default finish_vivid_hue_boost, 0.25)"),
    vivid_lhe: float = typer.Option(-1.0, "--vivid-lhe",
                                    help="Vivid local contrast (LocalHistogramEqualization) amount 0..1, 0 = off "
                                         "(default finish_vivid_lhe, 0)"),
    gradient: str = typer.Option("auto", "--gradient", help="auto | abe | none"),
    rcastro: str = typer.Option("", "--rcastro",
                                help="BlurX / NoiseX / StarXTerminator: auto | on | off "
                                     "(default finish_use_rcastro; PS-186)"),
    bxt_stars: float = typer.Option(-1.0, "--bxt-stars", help="BlurXTerminator sharpen stars 0..1"),
    bxt_nonstellar: float = typer.Option(-1.0, "--bxt-nonstellar", help="BlurXTerminator sharpen nonstellar 0..1"),
    nxt_denoise: float = typer.Option(-1.0, "--nxt-denoise", help="NoiseXTerminator denoise 0..1"),
    hoo: bool = typer.Option(False, "--hoo/--no-hoo", help="Color masters: also the HOO-mapped image"),
    solve: bool = typer.Option(True, "--solve/--no-solve",
                               help="Plate solve (SPCC color); --no-solve skips it (a solve that hung)"),
    focal_mm: float = typer.Option(0.0, "--focal-mm", help="0 = the master's run, else the rig's"),
    pixel_um: float = typer.Option(0.0, "--pixel-um", help="0 = the master's run, else 3.76"),
    pixinsight: bool = typer.Option(True, "--pixinsight/--no-pixinsight", help="Run PixInsight (else script only)"),
    pixinsight_exe: str = typer.Option(r"C:\Program Files\PixInsight\bin\PixInsight.exe", "--pixinsight-exe"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve and check the script; write nothing"),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON"),
):
    """Finish one master again (no integration): the same finish and
    defaults as `photonscript integrate` (deploy/finish_osc.js), run in
    PixInsight one instance at a time, into a NEW folder with a ledger
    (kind finish, stays on the desktop). Replaces run-finish-osc.ps1 for
    re-finishes.

    photonscript finish --master D:/.../out/master/master_OSC.xisf --name M31_v5 --palette gold --dry-run

    Mono color (no restack): combine an rc16 run's filter masters into
    <name>_LRGB / _RGB / _SHO / _HOO (deploy/finish_mono_color.js):

    photonscript finish --mono-run "C:/Astro/Staging/M_27_rc16_20261012-2300" --color lrgb --dry-run
    """
    import json as _json
    from photonscript.integration import finish as fn
    from photonscript.integration import pipeline as pl
    from photonscript.integration import pjsr
    from photonscript.integration.runner import PixInsightBusy
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    root = Path(staging_root) if staging_root else pl.default_staging_root(cfg)
    try:
        kw = pjsr.rc_settings(cfg)
        kw.update(_rc_overrides(rcastro, bxt_stars, bxt_nonstellar, nxt_denoise))
        kw["palette"] = pjsr.palette_mode(palette or getattr(cfg, "finish_palette", "off"))
        # an explicit --palette always applies; the configured one only to finish_palette_kinds
        kw["palette_kinds"] = "" if palette else str(getattr(cfg, "finish_palette_kinds", pjsr.PALETTE_KINDS) or "")
        kw.update(_vivid_overrides(pjsr.vivid_settings(cfg), vivid, vivid_sat_mid, vivid_hue_boost, vivid_lhe))
    except ValueError as e:
        console.print(f"[red]finish:[/red] {e}", markup=True)
        raise typer.Exit(2)
    if bool(master) == bool(mono_run):
        console.print("[red]finish:[/red] pass exactly one of --master or --mono-run", markup=True)
        raise typer.Exit(2)
    if master and not name:
        console.print("[red]finish:[/red] --master needs --name", markup=True)
        raise typer.Exit(2)
    opts = fn.Options(
        master=Path(master) if master else None, name=name, target=target, rig=rig, staging_root=root,
        out=Path(out) if out else None, gradient=gradient, hoo=hoo, focal_mm=focal_mm, pixel_um=pixel_um,
        run_pixinsight=pixinsight, dry_run=dry_run, pixinsight=pixinsight_exe,
        mono_run=Path(mono_run) if mono_run else None, colors=color, lock_wait_min=_pilock_wait(cfg),
        solve=solve, gaia_check=bool(getattr(cfg, "pixinsight_gaia_check", True)), **kw,
    )
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    try:
        res = fn.run_mono_color(opts, echo=say) if mono_run else fn.run(opts, echo=say)
    except (pl.PipelineError, PixInsightBusy, FileNotFoundError, ValueError) as e:
        console.print(f"[red]finish:[/red] {e}", markup=True)
        raise typer.Exit(1)
    if as_json:
        print(_json.dumps(res, indent=1, default=str))
    ok = res.get("finish", {}).get("ok", True) and res.get("color", {}).get("ok", True)
    raise typer.Exit(0 if ok else 1)


@app.command("pixinsight-gaia-check")
def pixinsight_gaia_check_cmd(
    fix: bool = typer.Option(False, "--fix",
                             help="Copy the Gaia DR3 / DR3/SP paths into the other core-00N slot files "
                                  "(only while no PixInsight runs; each file backed up first)"),
    settings_dir: str = typer.Option("", "--settings-dir",
                                      help=r"Default %APPDATA%\Pleiades (env PS_PIXINSIGHT_SETTINGS_DIR)"),
):
    """Gaia preflight (integration/pigaia.py), read-only: which PixInsight
    instance slot an automation run uses (core-00N-pxi.settings), the Gaia
    DR3 (plate solve) and DR3/SP (SPCC) paths set there, and whether the
    files exist. DR3 not usable = finishes skip the plate solve (no online
    VizieR fallback, the 2026-10-14 hang). --fix copies the Gaia keys into
    the slot files that lack them (refused while PixInsight runs).

    photonscript pixinsight-gaia-check
    """
    from photonscript.integration import pigaia
    from photonscript.integration.runner import pixinsight_running
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    d = Path(settings_dir) if settings_dir else None
    # the runner starts PixInsight only while none runs: its instance takes slot 1
    st = pigaia.check(d, running_count=0)
    for line in pigaia.report_lines(st):
        say(line)
    busy = pixinsight_running()
    if busy:
        say(f"note: PixInsight is running now (pid {', '.join(map(str, busy))}): a -n instance started "
            f"by hand now would use slot {pigaia.automation_slot(len(busy))}; the runner refuses to "
            "start while it runs, then uses slot 1")
    if fix:
        try:
            done = pigaia.fix(d, echo=say)
        except pigaia.GaiaFixError as e:
            console.print(f"[red]pixinsight-gaia-check --fix:[/red] {e}", markup=True)
            raise typer.Exit(1)
        say(f"fixed {len(done)} slot file(s)" if done else "nothing to fix: every slot has the Gaia DR3 paths")
        st = pigaia.check(d, running_count=0)
    raise typer.Exit(0 if (st.dr3_ok or st.unknown) else 1)


@app.command("integrate-watch")
def integrate_watch_cmd(
    once: bool = typer.Option(False, "--once", help="One cycle, then exit (for a Scheduled Task)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the decisions; integrate and post nothing"),
    url: str = typer.Option("", "--url", help="Scheduler (default integration_report_url)"),
    staging_root: str = typer.Option("", "--staging-root", help="Default integration_staging_root"),
    rigs: str = typer.Option("", "--rigs", help="Override the scope's integrate_watch_rigs (comma list)"),
    new_data_h: float = typer.Option(-1.0, "--new-data-h", help="Override: hours of new data that re-integrate"),
    first_h: float = typer.Option(-1.0, "--first-h", help="Override: first run at this many hours (0 = at goal)"),
    min_interval_h: float = typer.Option(-1.0, "--min-interval-h", help="Override: hours between runs of one goal"),
    require_calibration: Optional[bool] = typer.Option(
        None, "--require-calibration/--no-require-calibration",
        help="Override: wait while calibration is missing"),
    qa: str = typer.Option("report", "--qa", help="Star QA mode for the runs (report | apply | off)"),
    interval_min: float = typer.Option(30.0, "--interval-min", help="Minutes between cycles without --once"),
    pixinsight_exe: str = typer.Option(r"C:\Program Files\PixInsight\bin\PixInsight.exe", "--pixinsight-exe"),
):
    """PS-31: goal to finished image. Polls the scheduler's integration
    candidates (goal met, new data since the last ledger, calibration
    missing / owed) and, when a goal is ready, runs `photonscript
    integrate` into a NEW staging folder (integration, finish, AstroBin
    packet draft; nothing uploads), then posts its ledger. Never starts
    while PixInsight is running; one run per cycle. Install it yourself as
    a Windows Scheduled Task running `photonscript integrate-watch --once`
    every 30 min (this command installs nothing).

    photonscript integrate-watch --once --dry-run
    """
    from photonscript.integration import pipeline as pl
    from photonscript.integration import scratch as sc
    from photonscript.integration import watch as w
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    root = Path(staging_root) if staging_root else pl.default_staging_root(cfg)
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    o = w.WatchOptions(
        base_url=url or getattr(cfg, "integration_report_url", ""), staging_root=root,
        scratch_root=sc.configured_root(cfg),
        rigs=[r.strip() for r in rigs.split(",") if r.strip()] or None,
        new_data_h=new_data_h if new_data_h >= 0 else None,
        first_h=first_h if first_h >= 0 else None,
        min_interval_h=min_interval_h if min_interval_h >= 0 else None,
        require_calibration=require_calibration, qa=qa, dry_run=dry_run,
        interval_min=interval_min)
    if not o.base_url:
        console.print("[red]integrate-watch:[/red] no scheduler URL (--url or integration_report_url)")
        raise typer.Exit(2)

    def run_integrate(target: str, rig: str, trigger: dict) -> dict:
        opts = pl.Options(target=target, rig=rig, staging_root=root, qa=qa,
                          pixinsight=pixinsight_exe, trigger=trigger, **pl.config_options(cfg, rig))
        return pl.run(opts, echo=say)

    try:
        w.loop(o, once=once, run_integrate=run_integrate, echo=say)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]integrate-watch:[/red] {e}", markup=True)
        raise typer.Exit(1)


@app.command("autointegrate")
def autointegrate_cmd(
    once: bool = typer.Option(False, "--once", help="One cycle, then exit (the Scheduled Task runs this)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the decisions; integrate, blend and post nothing"),
    url: str = typer.Option("", "--url", help="Scheduler (default integration_report_url)"),
    staging_root: str = typer.Option("", "--staging-root", help="Default integration_staging_root"),
    library: str = typer.Option("", "--library",
                                help=r"Library mirror (read-only; default integration_library_dir, "
                                     r"else D:\ninashare\Library when it exists)"),
    rigs: str = typer.Option("", "--rigs", help="Override the scope's integrate_watch_rigs (comma list)"),
    settle_min: float = typer.Option(-1.0, "--settle-min",
                                     help="Override autointegrate_settle_min (Syncthing settle wait)"),
    blend: Optional[bool] = typer.Option(None, "--blend/--no-blend", help="Override autointegrate_blend"),
    notify: Optional[bool] = typer.Option(None, "--notify/--no-notify", help="Override autointegrate_notify"),
    hoo: Optional[bool] = typer.Option(None, "--hoo/--no-hoo", help="Override autointegrate_hoo (OSC HOO image)"),
    guard: bool = typer.Option(True, "--guard/--no-guard",
                               help="Desktop-load guard (autointegrate_idle_min / _max_cpu_pct); "
                                    "--no-guard starts PixInsight even while the desktop is in use"),
    qa: str = typer.Option("report", "--qa", help="Star QA mode for the runs (report | apply | off)"),
    interval_min: float = typer.Option(30.0, "--interval-min", help="Minutes between cycles without --once"),
    pixinsight_exe: str = typer.Option(r"C:\Program Files\PixInsight\bin\PixInsight.exe", "--pixinsight-exe"),
):
    """PS-161: desktop auto-integrate. integrate-watch's decision (goal met,
    or new approved data since the last ledger) plus: waits until Syncthing
    has settled the target's Library folders (no temp files, file count
    stable for autointegrate_settle_min), runs `photonscript integrate`
    (OSC natural color + HOO image; RC16 per filter), posts the ledger,
    blends two-rig goals once both masters exist and an input is newer
    than the last blend's (PS-153), and sends a review JPG with Pushover.
    A run whose finish failed gets a finish-only retry (at most
    autointegrate_finish_retries); the first cycle after
    autointegrate_summary_hour writes the morning summary. Nothing while
    PixInsight is open or the desktop is in use (--no-guard); one
    PixInsight at a time; never re-integrates without new subs. Install it
    yourself with deploy\\install-autointegrate-task.ps1 (this command
    installs nothing).

    photonscript autointegrate --once --dry-run
    """
    from photonscript.integration import autointegrate as ai
    from photonscript.integration import blend as bl
    from photonscript.integration import pipeline as pl
    from photonscript.integration import scratch as sc
    from photonscript.integration import watch as w
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    root = Path(staging_root) if staging_root else pl.default_staging_root(cfg)
    lib = Path(library) if library else pl.default_library(cfg)
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    wo = w.WatchOptions(
        base_url=url or getattr(cfg, "integration_report_url", ""), staging_root=root,
        scratch_root=sc.configured_root(cfg),
        rigs=[r.strip() for r in rigs.split(",") if r.strip()] or None,
        qa=qa, dry_run=dry_run, interval_min=interval_min)
    if not wo.base_url:
        console.print("[red]autointegrate:[/red] no scheduler URL (--url or integration_report_url)")
        raise typer.Exit(2)
    o = ai.AutoOptions(
        watch=wo, library=lib,
        settle_min=(settle_min if settle_min >= 0
                    else float(getattr(cfg, "autointegrate_settle_min", 15.0))),
        blend=bool(getattr(cfg, "autointegrate_blend", True)) if blend is None else blend,
        notify=bool(getattr(cfg, "autointegrate_notify", True)) if notify is None else notify,
        hoo=bool(getattr(cfg, "autointegrate_hoo", True)) if hoo is None else hoo,
        interval_min=interval_min,
        nb_gate=bool(getattr(cfg, "blend_nb_gate", True)),
        nb_min_ha_h=float(getattr(cfg, "blend_nb_min_ha_h", 2.0)),
        nb_min_oiii_h=float(getattr(cfg, "blend_nb_min_oiii_h", 1.5)),
        nb_min_sii_h=float(getattr(cfg, "blend_nb_min_sii_h", 1.5)),
        finish_retries=int(getattr(cfg, "autointegrate_finish_retries", 2)),
        idle_min=(float(getattr(cfg, "autointegrate_idle_min", 10.0)) if guard else 0.0),
        max_cpu_pct=(float(getattr(cfg, "autointegrate_max_cpu_pct", 80.0)) if guard else 0.0),
        summary_hour=float(getattr(cfg, "autointegrate_summary_hour", 8.0)),
        extra_roots=bl.extra_roots(cfg))
    say(f"autointegrate: Library {lib}, staging {root}"
        + (f", scratch {wo.scratch_root}" if wo.scratch_root else "")
        + f", settle {o.settle_min:g} min"
        + (" (dry run)" if dry_run else ""))

    def run_integrate(target: str, rig: str, trigger: dict) -> dict:
        kw = pl.config_options(cfg, rig)
        kw["library"] = lib
        opts = pl.Options(target=target, rig=rig, staging_root=root, qa=qa, hoo=o.hoo,
                          pixinsight=pixinsight_exe, trigger=trigger, **kw)
        return pl.run(opts, echo=say)

    def _blend_opts(target: str, dry: bool) -> bl.Options:
        return bl.Options(
            target=target, staging_root=root, dry_run=dry, pixinsight=pixinsight_exe,
            scratch_root=wo.scratch_root, extra_roots=o.extra_roots,
            osc_scale=float(getattr(cfg, "piggyback_pixel_scale_arcsec", bl.OSC_SCALE) or bl.OSC_SCALE),
            rc16_scale=float(getattr(cfg, "pixel_scale_arcsec", bl.RC16_SCALE) or bl.RC16_SCALE),
            **bl.config_bxt(cfg), **bl.config_vivid(cfg))

    def run_retry(need):
        from photonscript.integration import finish as fn
        from photonscript.integration import finish_retry as fr
        from photonscript.integration import pjsr
        kw = pjsr.rc_settings(cfg)
        kw["palette"] = pjsr.palette_mode(getattr(cfg, "finish_palette", "off"))
        kw["palette_kinds"] = str(getattr(cfg, "finish_palette_kinds", pjsr.PALETTE_KINDS) or "")
        kw.update(pjsr.vivid_settings(cfg))
        common = dict(target=need.target, rig=need.rig, staging_root=root, solve=need.solve,
                      gaia_check=bool(getattr(cfg, "pixinsight_gaia_check", True)),
                      pixinsight=pixinsight_exe, lock_wait_min=_pilock_wait(cfg), **kw)

        def run_finish(master, n):
            filt = "OSC" if master.stem.startswith("master_OSC") else bl.filter_of(master)
            return fn.run(fn.Options(master=master, name=f"{pl.safe_name(n.target)}_{filt}",
                                     hoo=o.hoo and filt == "OSC", **common), echo=say)

        def run_color(n):
            return fn.run_mono_color(fn.Options(master=None, name=pl.safe_name(n.target),
                                                mono_run=n.run_dir, colors="auto", **common), echo=say)

        def run_core(n):
            return fr.core_combine(n, exe=pixinsight_exe, echo=say,
                                   core_radius_arcmin=float(getattr(cfg, "qa_core_radius_arcmin", 1.0) or 1.0),
                                   warn_px=int(getattr(cfg, "qa_core_sat_warn_px", 25) or 25))
        return fr.run(need, run_finish=run_finish, run_color=run_color, run_core=run_core,
                      roots=bl.discovery_roots(root, wo.scratch_root, o.extra_roots), echo=say)

    try:
        ai.loop(o, once=once, echo=say,
                run_integrate=run_integrate,
                run_blend=lambda t: bl.run(_blend_opts(t, False), echo=say),
                discover_blend=lambda t: bl.discover(_blend_opts(t, True)),
                run_retry=run_retry,
                send=ai.pushover_sender(cfg))
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]autointegrate:[/red] {e}", markup=True)
        raise typer.Exit(1)


@app.command("autointegrate-summary")
def autointegrate_summary_cmd(
    hours: float = typer.Option(0.0, "--hours", help="Look back this many hours (0 = since yesterday noon)"),
    staging_root: str = typer.Option("", "--staging-root", help="Default integration_staging_root"),
):
    """The autointegrate morning summary now (read-only): one line per goal,
    what was stacked / finished / blended, with file:/// links to the final
    JPGs. autointegrate itself writes it to <staging>/Summaries/<date>.txt
    and Pushovers it once a day after autointegrate_summary_hour.

    photonscript autointegrate-summary --hours 24
    """
    from datetime import datetime, timedelta, timezone

    from photonscript.integration import blend as bl
    from photonscript.integration import night_summary as ns
    from photonscript.integration import pipeline as pl
    from photonscript.integration import scratch as sc
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    root = Path(staging_root) if staging_root else pl.default_staging_root(cfg)
    now = datetime.now()
    since = (now - timedelta(hours=hours)) if hours > 0 else \
        datetime.combine(now.date() - timedelta(days=1), datetime.min.time()) + timedelta(hours=12)
    roots = bl.discovery_roots(root, sc.configured_root(cfg), bl.extra_roots(cfg))
    print(ns.render(ns.collect(roots, since.astimezone(timezone.utc)), now.date().isoformat()))


@app.command("ledger-import")
def ledger_import_cmd(
    path: str = typer.Argument(..., help="A ledger.json (0.1 or 0.2), a folder holding one, "
                                         "or a hand-run staging folder to synthesize from"),
    variant: str = typer.Option("", "--variant", help="Synthesize: out/<variant> (v4b or "
                                                     "v4b_noflat; default the newest)"),
    campaign: str = typer.Option("", "--campaign", help="Override the campaign name"),
    version: int = typer.Option(0, "--version", help="Override the version"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print only (the default without --apply)"),
    apply: bool = typer.Option(False, "--apply", help="POST it to the scheduler"),
    url: str = typer.Option("", "--url", help="Scheduler (default integration_report_url)"),
    out: str = typer.Option("", "--out", help="Also write the ledger JSON here (never into the "
                                              "source folder)"),
    as_json: bool = typer.Option(False, "--json", help="Print the whole ledger as JSON"),
):
    r"""PS-142: one-time import of a ledger from before PS-33 (hand-written
    0.1 ledger, or a ledger synthesized from a hand-run staging folder's
    manifest / weights.csv / logs / AstroBin packet). Read-only on the
    source. Posts (POST /api/integrations, idempotent per run, no Pushover
    for imports) only with --apply.

    photonscript ledger-import C:\Users\sleep\Astrophotography\Staging\M31_OSC3\ledger.json
    photonscript ledger-import D:\Astrophotography\Staging\M31_OSC4 --variant v4b --apply
    """
    import json as _json
    from photonscript.integration import ledger_import as li
    say = lambda s: console.print(s, markup=False, highlight=False)  # noqa: E731
    try:
        led, how = li.load_any(Path(path), variant=variant or None, campaign=campaign,
                               version=version)
    except (li.LedgerImportError, ValueError, OSError) as e:
        console.print(f"[red]ledger-import:[/red] {e}", markup=True)
        raise typer.Exit(1)
    say(how)
    for line in li.summary_lines(led):
        say("  " + line)
    if as_json:
        print(_json.dumps(led.dump(), indent=1))
    if out:
        src = Path(path).resolve()
        src_dir = src if src.is_dir() else src.parent
        dest = Path(out).resolve()
        if dest == src or src_dir in dest.parents:
            console.print("[red]ledger-import:[/red] --out must be outside the source folder "
                          "(it is read-only)", markup=True)
            raise typer.Exit(2)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_json.dumps(led.dump(), indent=1), encoding="ascii")
        say(f"wrote {dest}")
    if not apply or dry_run:
        say("dry run: nothing posted (add --apply to post it to the scheduler)")
        return
    cfg = _config_for_repo(Path(__file__).resolve().parents[1])
    base = url or getattr(cfg, "integration_report_url", "")
    r = li.post(led, base)
    if not r["ok"]:
        console.print(f"[red]ledger-import:[/red] not posted: {r['detail']}", markup=True)
        raise typer.Exit(1)
    say(f"posted to {base}: v{r.get('version')} {r.get('detail', '')}")


if __name__ == "__main__":
    app()
