"""PhotonScript CLI — command-line interface for all operations.

Usage:
    photonscript start [--mode scheduler|telescope|librarian|full]
    photonscript plan [--month 3] [--date 2024-03-15]
    photonscript targets [--month 3]
    photonscript sequence [--output tonight.xml]
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
    output: str = typer.Option("", help="Output XML file path"),
    month: int = typer.Option(0, help="Month (1-12), 0 = current"),
):
    """Generate a NINA sequence XML file for tonight."""
    from photonscript.shared.astronomy import get_seasonal_targets
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.scheduler.target_planner import (
        create_project_from_target, plan_night_sequence,
    )
    from photonscript.scheduler.nina_sequence import generate_nina_xml, build_sequence_for_night

    config = PhotonScriptConfig()
    now = datetime.utcnow()
    if month == 0:
        month = now.month

    seasonal = get_seasonal_targets(month)
    projects = [create_project_from_target(t) for t in seasonal]
    targets = plan_night_sequence(projects, config, now)
    seq = build_sequence_for_night(f"PhotonScript_{now.strftime('%Y%m%d')}", targets)
    xml = generate_nina_xml(seq)

    if output:
        Path(output).write_text(xml)
        console.print(f"[green]Sequence saved to {output}[/green]")
    else:
        default_path = f"PhotonScript_{now.strftime('%Y%m%d')}.xml"
        Path(default_path).write_text(xml)
        console.print(f"[green]Sequence saved to {default_path}[/green]")

    console.print(f"[dim]{len(targets)} targets, ready for NINA import[/dim]")


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
