"""`photonscript finish`: finish one master again, without integrating.

Replaces deploy/run-finish-osc.ps1 for re-finishes. Renders the one finish
(deploy/finish_osc.js via pjsr.render_finish, the same defaults as
`photonscript integrate`: config finish_use_rcastro / bxt_* / nxt_denoise /
finish_palette, GraXpert when found, the ImageSolver include when
PixInsight has it) for ONE master into a NEW (or empty) folder and runs it
in PixInsight with the integrate runner (one instance at a time, refused
while PixInsight is open, log polled with short reads):

    <staging_root>/Finish/<name>_finish_<YYYYMMDD-HHMM>/   (or --out)
        finish_run.js  manifest.json  ledger.json (kind "finish")
        out/finish.log  out/timing.csv
        out/final/<name>_final.{xisf,tif,jpg}  <name>_linear.xisf
                 <name>_vivid.{xisf,tif,jpg} (color masters, finish_vivid)
                 <name>_final_steps.json  [<name>_gold / _hoo ...]

Defaults that integrate takes from the lights (focal length, pixel size,
the target's coordinates) come from the master's own run folder when it
is an integrate run (its finish_run.js and ledger / manifest), else from
the rig (piggyback 600 mm, rc16 3248 mm, 3.76 um) and --target.

The Finish folder sits under the staging root like Blend: local_ledgers()
reads <staging_root>/*/ledger.json only, and report / watch / blend only
take kind "integration", so a finish ledger never counts as a version,
never trips integrate-watch and is never posted.

`photonscript finish --mono-run <run folder> --color auto|lrgb|rgb|sho|hoo`
colors a mono (rc16) integrate run without restacking: its filter masters
(out/master, master_<F>_hdr preferred) go through deploy/finish_mono_color.js
(mono_color.py) into a NEW folder:

    <staging_root>/Finish/<name>_color_<YYYYMMDD-HHMM>/   (or --out)
        color_run.js  manifest.json  ledger.json (kind "finish", a color block)
        out/color.log  out/timing.csv  out/timing_color.csv
        out/final/<name>_LRGB.{xisf,tif,jpg} (_RGB / _SHO / _HOO)
                 <name>_<KIND>_linear.xisf  <name>_<KIND>_steps.json
                 mono_color_steps.json
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from photonscript.integration import pilock, pjsr, runner
from photonscript.integration.pipeline import PipelineError, Timer, catalog_coords, is_inside, safe_name

FINISH_DIR = "Finish"
SCRIPT = "finish_run.js"
RIG_OPTICS = {"piggyback": (600.0, 3.76), "rc16": (3248.0, 3.76)}   # focal mm, pixel um


class FinishError(PipelineError):
    pass


@dataclass
class Options:
    master: Path | None
    name: str
    target: str = ""
    rig: str = ""                     # "" = the master's run, else piggyback
    staging_root: Path = Path(r"D:\Astrophotography\Staging")
    out: Path | None = None
    gradient: str = "auto"
    use_rc: bool = True
    rc_mode: str = "auto"
    bxt_stars: float = pjsr.FINISH_DEFAULTS["bxt_stars"]
    bxt_nonstellar: float = pjsr.FINISH_DEFAULTS["bxt_nonstellar"]
    nxt_denoise: float = pjsr.FINISH_DEFAULTS["nxt_denoise"]
    palette: str = "off"
    palette_kinds: str = ""           # finish_palette_kinds ("" = no filter, e.g. an explicit --palette)
    vivid: bool = True                # finish_vivid: also <name>_vivid.{xisf,tif,jpg}
    vivid_sat_mid: float = pjsr.FINISH_DEFAULTS["vivid_sat_mid"]
    vivid_hue_boost: float = pjsr.FINISH_DEFAULTS["vivid_hue_boost"]
    vivid_lhe: float = pjsr.FINISH_DEFAULTS["vivid_lhe"]
    hoo: bool = False
    focal_mm: float = 0.0             # 0 = the source run's, else the rig's
    pixel_um: float = 0.0
    run_pixinsight: bool = True
    dry_run: bool = False
    pixinsight: str = runner.DEFAULT_EXE
    lock_wait_min: float = 60.0       # pixinsight_lock_wait_min: wait this long for the PixInsight queue lock
    mono_run: Path | None = None      # --mono-run: color combine of this run's filter masters
    colors: str = "auto"              # --color: auto | all | lrgb,rgb,sho,hoo


# ----------------------------------------------------------------- source run

def source_run(master: Path) -> Path | None:
    """The integrate run folder of <run>/out/master/<file>, else None."""
    p = Path(master)
    if p.parent.name == "master" and p.parent.parent.name == "out":
        return p.parent.parent.parent
    return None


_JS_NUM = re.compile(r"^var (FOCAL_MM|PIXEL_UM|RA_DEG|DEC_DEG)\s*=\s*([-0-9.eE]+|NaN);", re.M)


def source_finish_values(run_dir: Path | None) -> dict:
    """FOCAL_MM / PIXEL_UM / RA_DEG / DEC_DEG of the run's finish_run.js
    (what integrate took from the lights and the catalog); {} when absent."""
    if not run_dir:
        return {}
    p = Path(run_dir) / SCRIPT
    try:
        text = p.read_text(encoding="ascii")
    except Exception:  # noqa: BLE001 - no finish script in that folder
        return {}
    out = {}
    for k, v in _JS_NUM.findall(text):
        if v != "NaN":
            out[k] = float(v)
    return out


def resolve(o: Options) -> dict:
    """Everything the render needs that is not an option as given: rig,
    campaign, coordinates, optics, notes."""
    master = Path(o.master)
    notes: list[str] = []
    src = source_run(master)
    info = None
    if src:
        from photonscript.integration.blend import run_info
        info = run_info(src)
    rig = o.rig or (info.rig if info else "") or "piggyback"
    if rig not in RIG_OPTICS:
        raise FinishError(f"--rig must be one of {', '.join(RIG_OPTICS)} (got {rig!r})")
    campaign = o.target or (info.campaign if info else "") or o.name
    vals = source_finish_values(src)
    ra, dec = catalog_coords(o.target) if o.target else (None, None)
    if ra is None and "RA_DEG" in vals:
        ra, dec = vals["RA_DEG"], vals.get("DEC_DEG")
        notes.append("coordinates from the source run's finish_run.js")
    elif ra is None and info:
        ra, dec = catalog_coords(info.campaign)
    if ra is None:
        notes.append("target coordinates unknown: the plate solve is skipped (pass --target)")
    focal = o.focal_mm or vals.get("FOCAL_MM") or RIG_OPTICS[rig][0]
    pixel = o.pixel_um or vals.get("PIXEL_UM") or RIG_OPTICS[rig][1]
    palette, palette_why = pjsr.palette_for_target(o.palette, o.target or campaign, o.palette_kinds)
    if palette_why:
        notes.append(palette_why)
    m = re.match(r"^master_([A-Za-z0-9]+)$", master.stem)
    if m and (master.parent / f"master_{m.group(1)}_hdr.xisf").is_file():
        notes.append(f"master_{m.group(1)}_hdr.xisf exists beside it (the HDR combine of that "
                     "filter's exposure masters); pass it as --master to finish that one instead")
    return {"rig": rig, "campaign": campaign, "ra_deg": ra, "dec_deg": dec, "focal_mm": float(focal),
            "pixel_um": float(pixel), "source_run": str(src) if src else "",
            "source_version": info.version if info else None, "notes": notes,
            "palette": palette, "palette_why": palette_why}


def default_run_dir(o: Options, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return Path(o.staging_root) / FINISH_DIR / f"{safe_name(o.name)}_finish_{now:%Y%m%d-%H%M}"


def finish_config(o: Options, res: dict, run_dir: Path) -> dict:
    gx = pjsr.find_graxpert()
    out_dir = Path(run_dir) / "out"
    return {"out": pjsr.fwd(out_dir), "staging": pjsr.fwd(run_dir),
            "ra_deg": res["ra_deg"], "dec_deg": res["dec_deg"],
            "focal_mm": res["focal_mm"], "pixel_um": res["pixel_um"],
            "gradient": o.gradient, "use_rc": o.use_rc, "rc_mode": o.rc_mode,
            "bxt_stars": o.bxt_stars, "bxt_nonstellar": o.bxt_nonstellar, "nxt_denoise": o.nxt_denoise,
            "pi_library": pjsr.fwd(Path(o.pixinsight).parent.parent / "library"),
            "graxpert": gx, "graxpert_version": "unknown" if gx else "",
            "hoo": "on" if o.hoo else "off",            # finish_osc.js skips it on a mono master
            "palette": res.get("palette", pjsr.palette_mode(o.palette)),   # same: color masters only
            "palette_why": res.get("palette_why", ""),
            "vivid": "on" if o.vivid else "off",        # same: color masters only
            "vivid_sat_mid": o.vivid_sat_mid, "vivid_hue_boost": o.vivid_hue_boost, "vivid_lhe": o.vivid_lhe,
            "masters": [{"name": safe_name(o.name), "path": pjsr.fwd(o.master)}]}


# ----------------------------------------------------------------- records

def outputs(out_dir: Path) -> list[str]:
    return [str(p) for p in sorted((Path(out_dir) / "final").glob("*"))
            if p.suffix.lower() in (".xisf", ".tif", ".jpg", ".json")]


def build_ledger(o: Options, res: dict, run_dir: Path, cfg: dict, pi: dict | None, timing: list):
    from photonscript.integration.ledger import finish_steps, repo_commit
    from photonscript.shared import ledger as L
    out_dir = Path(run_dir) / "out"
    mp = Path(o.master)
    st = mp.stat() if mp.exists() else None
    machine = {
        "run_dir": str(run_dir), "repo_commit": repo_commit(), "kind": "finish",
        "input": {"master": str(mp), "bytes": st.st_size if st else None,
                  "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S") if st else "",
                  "run": res["source_run"], "run_version": res["source_version"]},
        "notes": res["notes"],
        "options": {k: cfg.get(k) for k in ("gradient", "rc_mode", "use_rc", "bxt_stars", "bxt_nonstellar",
                                            "nxt_denoise", "palette", "palette_why", "hoo", "vivid",
                                            "vivid_sat_mid", "vivid_hue_boost", "vivid_lhe", "focal_mm",
                                            "pixel_um", "ra_deg", "dec_deg")},
        "finish": {"script": f"{SCRIPT} (deploy/finish_osc.js)", "ok": (pi or {}).get("ok"),
                   "minutes": (pi or {}).get("minutes"), "last_line": (pi or {}).get("last_line", ""),
                   "steps": finish_steps(out_dir), "log": str(out_dir / "finish.log")},
        "outputs": outputs(out_dir),
        "timing": {"started_utc": timing[0][1] if timing else "",
                   "finished_utc": timing[-1][2] if timing else "",
                   "minutes": round(sum(float(r[3]) for r in timing), 2) if timing else 0,
                   "stages": timing},
    }
    return L.Ledger(campaign=res["campaign"], rig=res["rig"], kind="finish", run=Path(run_dir).name,
                    machine=machine)


# ----------------------------------------------------------------- run

def run(o: Options, echo=print) -> dict:
    """Re-finish one master, holding the PixInsight queue lock (pilock.py)
    when it runs PixInsight."""
    with pilock.job(f"finish {o.name or Path(o.master).name}", enabled=o.run_pixinsight and not o.dry_run,
                    wait_min=o.lock_wait_min, echo=echo):
        return _run(o, echo)


def _run(o: Options, echo=print) -> dict:
    master = Path(o.master)
    if not master.is_file():
        raise FinishError(f"no master at {master}")
    if not str(o.name or "").strip():
        raise FinishError("--name must not be blank")
    if safe_name(o.name) != o.name:
        echo(f"  name {o.name!r} -> {safe_name(o.name)!r} (file-name safe)")
    res = resolve(o)
    for n in res["notes"]:
        echo("  " + n)
    run_dir = Path(o.out) if o.out else default_run_dir(o)
    if is_inside(master, run_dir):
        raise FinishError(f"the master sits inside the finish folder {run_dir}: pick a NEW --out")
    result: dict = {"run_dir": str(run_dir), "dry_run": o.dry_run, "master": str(master),
                    "name": safe_name(o.name), "rig": res["rig"], "campaign": res["campaign"],
                    "notes": res["notes"]}
    if o.dry_run:
        cfg = finish_config(o, res, run_dir)
        problems = pjsr.check(pjsr.render_finish(cfg))
        result["config"] = cfg
        result["script_problems"] = problems
        echo(f"  would write {run_dir} (script check: {'ok' if not problems else '; '.join(problems)})")
        echo("dry run: nothing written, PixInsight not started")
        return result
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FinishError(f"{run_dir} exists and is not empty: every finish gets a NEW folder "
                          "(pass another --out or leave it out)")
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir = run_dir / "out"
    timer = Timer(out_dir / "timing.csv", echo)

    with timer.stage("generate the PixInsight finish script"):
        cfg = finish_config(o, res, run_dir)
        block, missing = pjsr.solver_include(o.pixinsight)
        if missing:
            echo(f"  ImageSolver files missing ({', '.join(missing)}): the finish skips the plate solve")
        js = pjsr.write(run_dir / SCRIPT, pjsr.render_finish(cfg, block))
        echo(f"  {js.name}")
    result["script"] = str(js)

    pi = None
    if o.run_pixinsight:
        with timer.stage("PixInsight finish (launch to exit)"):
            pi = runner.run_script(js, out_dir / "finish.log", exe=o.pixinsight, echo=echo)
            echo(f"  finish: {'EXIT OK' if pi['ok'] else 'FAILED'} in {pi['minutes']} min ({pi['last_line']})")
        result["finish"] = pi
    else:
        echo("  PixInsight not started (--no-pixinsight). Run it later with:")
        echo(f'    "{o.pixinsight}" -n --automation-mode --run={js} --force-exit')

    with timer.stage("manifest + ledger"):
        man = {"version": 1, "kind": "finish", "target": res["campaign"], "rig": res["rig"],
               "run_name": run_dir.name, "run_dir": str(run_dir),
               "built": datetime.now().strftime("%Y-%m-%d %H:%M"), "master": str(master),
               "source_run": res["source_run"], "notes": res["notes"], "config": cfg,
               "pixinsight": pi, "timing": timer.rows, "outputs": outputs(out_dir)}
        (run_dir / "manifest.json").write_text(json.dumps(man, indent=1, ensure_ascii=True, default=str),
                                               encoding="ascii")
        from photonscript.integration import ledger as writer
        led = build_ledger(o, res, run_dir, cfg, pi, timer.rows)
        result["ledger"] = str(writer.write_for_run(led, run_dir, run_dir.parent))
        result["ledger_version"] = led.version
    timer.flush()
    result["outputs"] = outputs(out_dir)
    result["timing"] = timer.rows
    echo(f"done: {run_dir}")
    return result


# ----------------------------------------------------------------- mono color (--mono-run)

def mono_run_dir(p: Path) -> Path:
    """The run folder of --mono-run: the folder itself, or the run above an
    out/ or out/master/ folder given instead."""
    p = Path(p)
    if p.name == "master" and p.parent.name == "out":
        return p.parent.parent
    if p.name == "out" and (p / "master").is_dir():
        return p.parent
    return p


def resolve_mono(o: Options, run_dir: Path) -> dict:
    """Rig, campaign, coordinates and optics of a mono run (its ledger /
    manifest and finish_run.js), like resolve() for one master."""
    from photonscript.integration.blend import run_info
    info = run_info(run_dir)
    notes: list[str] = []
    rig = o.rig or (info.rig if info else "") or "rc16"
    if rig not in RIG_OPTICS:
        raise FinishError(f"--rig must be one of {', '.join(RIG_OPTICS)} (got {rig!r})")
    if rig != "rc16":
        notes.append(f"the run's rig is {rig}: the color combine is meant for mono (rc16) runs")
    campaign = o.target or (info.campaign if info else "") or o.name or run_dir.name
    vals = source_finish_values(run_dir)
    ra, dec = catalog_coords(o.target) if o.target else (None, None)
    if ra is None and "RA_DEG" in vals:
        ra, dec = vals["RA_DEG"], vals.get("DEC_DEG")
        notes.append("coordinates from the run's finish_run.js")
    elif ra is None and info:
        ra, dec = catalog_coords(info.campaign)
    if ra is None:
        notes.append("target coordinates unknown: the plate solve is skipped (pass --target); "
                     "RGB color falls back to BN + ColorCalibration")
    focal = o.focal_mm or vals.get("FOCAL_MM") or RIG_OPTICS[rig][0]
    pixel = o.pixel_um or vals.get("PIXEL_UM") or RIG_OPTICS[rig][1]
    return {"rig": rig, "campaign": campaign, "ra_deg": ra, "dec_deg": dec, "focal_mm": float(focal),
            "pixel_um": float(pixel), "source_run": str(run_dir),
            "source_version": info.version if info else None, "notes": notes}


def mono_finish_config(o: Options, res: dict, run_dir: Path) -> dict:
    """The finish settings the color combine takes (the finish_config keys
    without the masters)."""
    gx = pjsr.find_graxpert()
    return {"staging": pjsr.fwd(run_dir), "ra_deg": res["ra_deg"], "dec_deg": res["dec_deg"],
            "focal_mm": res["focal_mm"], "pixel_um": res["pixel_um"],
            "gradient": o.gradient, "use_rc": o.use_rc, "rc_mode": o.rc_mode,
            "bxt_stars": o.bxt_stars, "bxt_nonstellar": o.bxt_nonstellar, "nxt_denoise": o.nxt_denoise,
            "pi_library": pjsr.fwd(Path(o.pixinsight).parent.parent / "library"),
            "graxpert": gx, "graxpert_version": "unknown" if gx else ""}


def default_color_dir(o: Options, name: str, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return Path(o.staging_root) / FINISH_DIR / f"{safe_name(name)}_color_{now:%Y%m%d-%H%M}"


def run_mono_color(o: Options, echo=print) -> dict:
    """--mono-run: the color combine of an existing mono run's masters, run
    in PixInsight into a NEW folder with a manifest and a ledger (kind
    finish). The source run is only read. Holds the PixInsight queue lock
    (pilock.py) when it runs PixInsight."""
    with pilock.job(f"finish --mono-run {Path(o.mono_run).name if o.mono_run else '?'}",
                    enabled=o.run_pixinsight and not o.dry_run, wait_min=o.lock_wait_min, echo=echo):
        return _run_mono_color(o, echo)


def _run_mono_color(o: Options, echo=print) -> dict:
    from photonscript.integration import mono_color as mc
    if not o.mono_run:
        raise FinishError("--mono-run: no run folder given")
    src = mono_run_dir(Path(o.mono_run))
    if not (src / "out" / "master").is_dir():
        raise FinishError(f"--mono-run: no out/master folder in {src} (pass the integrate run folder)")
    try:
        colors = mc.parse_colors(o.colors)
    except mc.MonoColorError as e:
        raise FinishError(str(e)) from None
    masters = mc.run_masters(src)
    if "OSC" in masters:
        raise FinishError(f"{src} is an OSC run (master_OSC): the color combine is for mono runs")
    res = resolve_mono(o, src)
    name = safe_name(o.name or res["campaign"])
    items, plan_notes = mc.plan(masters, name, colors)
    res["notes"] += plan_notes
    for n in res["notes"]:
        echo("  " + n)
    found = ", ".join(f"{f} {Path(p).name}" for f, p in sorted(mc.canonical_masters(masters).items()))
    echo(f"  masters: {found or 'none'}")
    if not items:
        raise FinishError(f"nothing to color in {src}: " + ("; ".join(plan_notes) or "no filter masters"))
    run_dir = Path(o.out) if o.out else default_color_dir(o, name)
    if is_inside(run_dir, src) or is_inside(src, run_dir):
        raise FinishError(f"the color folder {run_dir} overlaps the source run {src}: pick a NEW --out")
    result: dict = {"run_dir": str(run_dir), "dry_run": o.dry_run, "mono_run": str(src), "name": name,
                    "rig": res["rig"], "campaign": res["campaign"], "notes": res["notes"],
                    "items": items}
    fcfg = mono_finish_config(o, res, src)
    if o.dry_run:
        cfg = mc.config(run_dir / "out", items, fcfg, res["campaign"])
        problems = pjsr.check(mc.render(cfg))
        result["config"] = cfg
        result["script_problems"] = problems
        echo(f"  would write {run_dir}: " + ", ".join(it["name"] for it in items)
             + f" (script check: {'ok' if not problems else '; '.join(problems)})")
        echo("dry run: nothing written, PixInsight not started")
        return result
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FinishError(f"{run_dir} exists and is not empty: every finish gets a NEW folder "
                          "(pass another --out or leave it out)")
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir = run_dir / "out"
    timer = Timer(out_dir / "timing.csv", echo)
    with timer.stage("generate the PixInsight color script"):
        cfg = mc.config(out_dir, items, fcfg, res["campaign"])
        block, missing = pjsr.solver_include(o.pixinsight)
        if missing:
            echo(f"  ImageSolver files missing ({', '.join(missing)}): the RGB color skips the plate solve")
        js = mc.write_script(run_dir, cfg, block)
        echo(f"  {js.name} (" + ", ".join(it["name"] for it in items) + ")")
    result["script"] = str(js)
    color = None
    if o.run_pixinsight:
        with timer.stage("PixInsight color combine (launch to exit)"):
            color = mc.run(run_dir, cfg, solver_block=block, exe=o.pixinsight, echo=echo)
            echo(f"  color: {'EXIT OK' if color['ok'] else 'FAILED'} in {color['minutes']} min, "
                 f"{len(color['made'])} of {len(items)} made")
        result["color"] = color
    else:
        echo("  PixInsight not started (--no-pixinsight). Run it later with:")
        echo(f'    "{o.pixinsight}" -n --automation-mode --run={js} --force-exit')
    with timer.stage("manifest + ledger"):
        outs = mc.outputs(out_dir, items)
        srcs = {k: str(v) for k, v in masters.items()}
        man = {"version": 1, "kind": "finish", "target": res["campaign"], "rig": res["rig"],
               "run_name": run_dir.name, "run_dir": str(run_dir),
               "built": datetime.now().strftime("%Y-%m-%d %H:%M"), "mono_run": str(src),
               "masters": srcs, "notes": res["notes"], "config": cfg, "pixinsight": color,
               "timing": timer.rows, "outputs": outs}
        (run_dir / "manifest.json").write_text(json.dumps(man, indent=1, ensure_ascii=True, default=str),
                                               encoding="ascii")
        from photonscript.integration import ledger as writer
        from photonscript.shared import ledger as L
        machine = {"run_dir": str(run_dir), "repo_commit": writer.repo_commit(), "kind": "finish",
                   "input": {"mono_run": str(src), "run_version": res["source_version"], "masters": srcs},
                   "notes": res["notes"],
                   "options": {k: cfg.get(k) for k in ("gradient", "rc_mode", "bxt_stars", "bxt_nonstellar",
                                                       "nxt_denoise", "focal_mm", "pixel_um", "ra_deg",
                                                       "dec_deg", "lrgb_lightness", "lrgb_saturation",
                                                       "sho_scnr")},
                   "color": mc.ledger_block(color or {"items": items, "outputs": outs}, res["notes"]),
                   "outputs": outs,
                   "final": mc.preferred_final(out_dir),   # LRGB, RGB, SHO, then HOO
                   "timing": {"started_utc": timer.rows[0][1] if timer.rows else "",
                              "finished_utc": timer.rows[-1][2] if timer.rows else "",
                              "minutes": round(sum(float(r[3]) for r in timer.rows), 2),
                              "stages": timer.rows}}
        led = L.Ledger(campaign=res["campaign"], rig=res["rig"], kind="finish", run=run_dir.name,
                       machine=machine)
        result["ledger"] = str(writer.write_for_run(led, run_dir, run_dir.parent))
        result["ledger_version"] = led.version
    timer.flush()
    result["outputs"] = mc.outputs(out_dir, items)
    result["timing"] = timer.rows
    echo(f"done: {run_dir}")
    return result
