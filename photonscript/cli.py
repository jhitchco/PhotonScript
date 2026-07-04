"""PhotonScript CLI — command-line interface for all operations.

Usage:
    photonscript start [--mode scheduler|telescope|librarian|full]
    photonscript plan [--month 3] [--date 2024-03-15]
    photonscript targets [--month 3]
    photonscript sequence [--output tonight.json] [--guided]
    photonscript lint <sequence.json>
    photonscript report [--date 2026-07-01]
    photonscript status
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

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


@app.command()
def start(
    mode: str = typer.Option("scheduler", help="Run mode: full, scheduler, telescope, librarian"),
    host: str = typer.Option("0.0.0.0", help="Bind address for scheduler"),
    port: int = typer.Option(8100, help="Port for scheduler web UI"),
):
    """Start PhotonScript agents."""
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.orchestrator import start as _start

    config = PhotonScriptConfig(scheduler_host=host, scheduler_port=port)
    console.print(f"[bold blue]PhotonScript[/bold blue] starting in [green]{mode}[/green] mode...")
    _start(mode=mode, config=config)


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
def status():
    """Show current system status (connects to running scheduler)."""
    import httpx

    try:
        resp = httpx.get("http://localhost:8100/api/status", timeout=5)
        data = resp.json()

        console.print(Panel("[bold]PhotonScript Status[/bold]", border_style="blue"))

        telescope = data.get("telescope", {})
        state = telescope.get("session_state", "unknown")
        state_color = {"imaging": "green", "idle": "dim", "error": "red"}.get(state, "yellow")

        console.print(f"  Telescope: [{state_color}]{state.upper()}[/]")
        console.print(f"  Target:    {telescope.get('current_target', '—')}")
        console.print(f"  Filter:    {telescope.get('current_filter', '—')}")
        console.print(f"  Guiding:   {telescope.get('guiding', {}).get('rms_total_arcsec', 0):.2f}\"")
        console.print(f"  Images:    {telescope.get('images_captured_tonight', 0)} tonight")
        console.print(f"  Projects:  {data.get('active_projects', 0)} active / {data.get('total_projects', 0)} total")

    except Exception:
        console.print("[red]Cannot connect to PhotonScript scheduler at localhost:8100[/red]")
        console.print("[dim]Start the scheduler with: photonscript start --mode scheduler[/dim]")


if __name__ == "__main__":
    app()
