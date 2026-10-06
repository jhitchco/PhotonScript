"""PS-153: blend the RC16 luminance core into the Piggy-600 color image
(two-rig goals such as M31, PS-134).

`photonscript blend --target M31` finds the newest Piggy-600 OSC run and the
newest RC16 run of the target among the `photonscript integrate` run folders
(their ledger.json, else manifest.json), or takes the masters by path, then
generates deploy/blend_rc16_osc.js into a NEW blend run folder and runs it in
PixInsight (one instance at a time, log polled with short reads):

    <staging_root>/Blend/<target>_blend_<YYYYMMDD-HHMM>/
        blend_run.js  manifest.json  ledger.json (kind "blend")
        out/blend.log  out/timing.csv  out/timing_pi.csv
        out/final/<name>_blend_linear.xisf  <name>_blend.{xisf,tif,jpg}
                 <name>_osc_ab.jpg  <name>_blend_mask.xisf
                 <name>_core_linear.xisf  <name>_core.{xisf,tif,jpg}
                 <name>_blend_steps.json

Inputs per stage:
  linear (default): OSC out/final/*_linear.xisf (gradient removed, color
                    calibrated by the finish), else out/master/master_OSC.xisf;
                    RC16 out/final/*_<F>_linear.xisf, else master_<F>.xisf
  final:            out/final/*_final.xisf of both runs (already stretched)
RC16 luminance = the L master, else the mean of the R/G/B (or any) masters.

The Blend folder sits under the staging root on purpose: local_ledgers()
reads <staging_root>/*/ledger.json only, so blend ledgers never count as
integration versions, never trip integrate-watch and are never posted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from photonscript.integration import pjsr, runner
from photonscript.integration.pipeline import PipelineError, Timer, catalog_coords, is_inside, safe_name

TEMPLATE = "blend_rc16_osc.js"
BLEND_DIR = "Blend"
STAGES = ("linear", "final")
LUM_ORDER = ("L", "R", "G", "B")
OSC_SCALE = 1.29      # piggyback_pixel_scale_arcsec (600 mm + IMX571 3.76 um)
RC16_SCALE = 0.236    # pixel_scale_arcsec (RC16 3248 mm + AP26MC, plate solves)
PIXEL_UM = 3.76
_SKIP_MASTER = re.compile(r"^master(Bias|Dark|Flat|OSC)", re.I)


class BlendError(PipelineError):
    pass


@dataclass
class RunInfo:
    run_dir: Path
    rig: str
    campaign: str
    version: int = 0
    created_at: str = ""
    finish_ok: bool | None = None


@dataclass
class Inputs:
    stage: str
    osc: Path
    rc16: dict[str, Path]                 # filter -> master
    osc_run: RunInfo | None = None
    rc16_run: RunInfo | None = None
    notes: list[str] = field(default_factory=list)

    def lum_filters(self) -> list[str]:
        return ["L"] if "L" in self.rc16 else sorted(self.rc16, key=_filter_rank)


@dataclass
class Options:
    target: str
    staging_root: Path = Path(r"D:\Astrophotography\Staging")
    out: Path | None = None
    osc: Path | None = None
    rc16: list[Path] | None = None
    stage: str = "linear"
    weight: float = 0.7
    inset_frac: float = 0.02
    feather_frac: float = 0.08
    lum_mask: bool = False
    core: bool = True
    core_margin_frac: float = 0.05
    osc_scale: float = OSC_SCALE
    rc16_scale: float = RC16_SCALE
    solve: bool = True
    resolve: bool = False
    keep_work: bool = False
    bg_target: float = pjsr.FINISH_DEFAULTS["bg_target"]
    shadow_sigma: float = pjsr.FINISH_DEFAULTS["shadow_sigma"]
    run_pixinsight: bool = True
    dry_run: bool = False
    pixinsight: str = runner.DEFAULT_EXE


def _filter_rank(f: str) -> tuple:
    return (LUM_ORDER.index(f) if f in LUM_ORDER else len(LUM_ORDER), f)


# ----------------------------------------------------------------- discovery

def run_info(run_dir: Path) -> RunInfo | None:
    """Rig / campaign / version of an integrate run folder: ledger.json,
    else manifest.json. None when neither is readable or it is a blend."""
    run_dir = Path(run_dir)
    lp = run_dir / "ledger.json"
    if lp.is_file():
        try:
            from photonscript.shared import ledger as L
            led = L.load(lp)
            if led.kind != "integration":
                return None
            return RunInfo(run_dir, led.rig, led.campaign, led.version, led.created_at,
                           (led.machine.get("finish") or {}).get("ok"))
        except Exception:  # noqa: BLE001 - half-written or hand-edited: try the manifest
            pass
    mp = run_dir / "manifest.json"
    if mp.is_file():
        try:
            m = json.loads(mp.read_text(encoding="ascii"))
            if m.get("rig") and m.get("target"):
                return RunInfo(run_dir, str(m["rig"]), str(m["target"]), 0, str(m.get("built", "")))
        except Exception:  # noqa: BLE001
            pass
    return None


def masters(run_dir: Path) -> dict[str, Path]:
    """filter -> out/master/master_<F>.xisf (calibration masters and the
    masterOSC copy left out)."""
    out = {}
    for p in sorted((Path(run_dir) / "out" / "master").glob("master_*.xisf")):
        stem = p.stem
        if _SKIP_MASTER.match(stem) or stem.endswith("_review"):
            continue
        out[stem[len("master_"):]] = p
    return out


def finished(run_dir: Path, kind: str) -> dict[str, Path]:
    """filter -> out/final/<name>_<F>_<kind>.xisf (kind: linear | final),
    matched to the run's masters by the filter suffix."""
    out = {}
    finals = sorted((Path(run_dir) / "out" / "final").glob(f"*_{kind}.xisf"))
    for f in masters(run_dir):
        hit = [p for p in finals if p.stem.endswith(f"_{f}_{kind}")]
        if hit:
            out[f] = hit[0]
    return out


def files_for(run_dir: Path, stage: str) -> tuple[dict[str, Path], str]:
    """(filter -> image, what) for a run at a stage. linear: the finish's
    *_linear.xisf per filter, else the raw master; final: *_final.xisf."""
    if stage == "final":
        return finished(run_dir, "final"), "finish *_final.xisf"
    lin = finished(run_dir, "linear")
    raw = masters(run_dir)
    got = {f: lin.get(f) or raw[f] for f in raw}
    what = ("finish *_linear.xisf" if lin and len(lin) == len(raw)
            else "raw master_*.xisf" if not lin else "mixed linear / raw")
    return got, what


def find_runs(staging_root: Path, target: str, rig: str) -> list[RunInfo]:
    """Integrate runs of `target` + `rig` under the staging root, newest
    first (version, then created time, then folder name)."""
    from photonscript.integration.ledger import same_campaign
    root = Path(staging_root)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name != BLEND_DIR):
        info = run_info(d)
        if info and info.rig == rig and same_campaign(info.campaign, target):
            out.append(info)
    return sorted(out, key=lambda r: (r.version, r.created_at, r.run_dir.name), reverse=True)


def _pick(runs: list[RunInfo], stage: str, want) -> tuple[RunInfo | None, dict[str, Path], str]:
    """Newest run with what `want` needs. Linear: a run whose finish made
    the *_linear files (gradient removed, color calibrated) beats a newer
    run that only has raw masters; raw masters are the fallback."""
    passes = [True, False] if stage == "linear" else [False]
    for finished_only in passes:
        for r in runs:
            got, what = files_for(r.run_dir, stage)
            if finished_only:
                got = {f: p for f, p in got.items() if p.stem.endswith("_linear")}
                what = "finish *_linear.xisf"
            if want(got):
                return r, got, what
    return None, {}, ""


def filter_of(path: Path) -> str:
    """Filter of a master given by path: master_<F>, <name>_<F>_linear /
    _final, else L."""
    stem = Path(path).stem
    m = re.match(r"^master_([A-Za-z0-9]+)$", stem)
    if m:
        return m.group(1)
    m = re.search(r"_([A-Za-z0-9]+)_(linear|final)$", stem)
    if m:
        return m.group(1)
    return "L"


def discover(o: Options) -> Inputs:
    """The OSC + RC16 inputs for a blend (given paths win over discovery)."""
    if o.stage not in STAGES:
        raise BlendError(f"--stage must be one of {', '.join(STAGES)}")
    notes: list[str] = []
    osc_run = rc_run = None
    if o.osc:
        osc = Path(o.osc)
    else:
        runs = find_runs(o.staging_root, o.target, "piggyback")
        osc_run, got, what = _pick(runs, o.stage, lambda g: "OSC" in g)
        if not osc_run:
            raise BlendError(f"no Piggy-600 OSC {o.stage} master for {o.target!r} under {o.staging_root} "
                             f"({len(runs)} piggyback run(s) found); run `photonscript integrate "
                             f"--target \"{o.target}\" --rig piggyback` or pass --osc")
        osc = got["OSC"]
        notes.append(f"OSC from {osc_run.run_dir.name} (v{osc_run.version}): {what}")
    if o.rc16:
        rc16 = {}
        for p in o.rc16:
            rc16.setdefault(filter_of(p), Path(p))
    else:
        runs = find_runs(o.staging_root, o.target, "rc16")
        rc_run, rc16, what = _pick(runs, o.stage, lambda g: bool(g))
        if not rc_run:
            raise BlendError(f"no RC16 {o.stage} master for {o.target!r} under {o.staging_root} "
                             f"({len(runs)} rc16 run(s) found); run `photonscript integrate "
                             f"--target \"{o.target}\" --rig rc16` or pass --rc16")
        notes.append(f"RC16 from {rc_run.run_dir.name} (v{rc_run.version}): {what}, "
                     f"filters {', '.join(sorted(rc16, key=_filter_rank))}")
    missing = [str(p) for p in [osc, *rc16.values()] if not Path(p).is_file()]
    if missing:
        raise BlendError("missing input file(s): " + ", ".join(missing))
    inp = Inputs(o.stage, osc, rc16, osc_run, rc_run, notes)
    if "L" not in rc16:
        notes.append("no RC16 L master: luminance = mean of " + "+".join(inp.lum_filters()))
    if o.stage == "linear" and osc.name.startswith("master_"):
        notes.append("OSC is the raw master (no finish yet): not gradient-removed or color calibrated")
    return inp


# ----------------------------------------------------------------- script

def default_run_dir(o: Options, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return Path(o.staging_root) / BLEND_DIR / f"{safe_name(o.target)}_blend_{now:%Y%m%d-%H%M}"


def blend_config(o: Options, inp: Inputs, run_dir: Path) -> dict:
    ra, dec = catalog_coords(o.target)
    lum = inp.lum_filters()
    return {
        "out": pjsr.fwd(Path(run_dir) / "out"), "name": safe_name(o.target), "stage": inp.stage,
        "weight": float(o.weight), "inset_frac": float(o.inset_frac),
        "feather_frac": float(o.feather_frac), "lum_mask": bool(o.lum_mask),
        "core": bool(o.core), "core_margin_frac": float(o.core_margin_frac),
        "osc": {"path": pjsr.fwd(inp.osc), "scale": float(o.osc_scale), "pixel_um": PIXEL_UM,
                "step_deg": 0.6, "rings": 2},
        "rc16": [{"filter": f, "path": pjsr.fwd(inp.rc16[f])} for f in lum],
        "rc16_scale": float(o.rc16_scale), "rc16_pixel_um": PIXEL_UM, "rc16_step_deg": 0.15,
        "ra_deg": ra, "dec_deg": dec, "solve": bool(o.solve), "resolve": bool(o.resolve),
        "keep_work": bool(o.keep_work),
        "bg_target": float(o.bg_target), "shadow_sigma": float(o.shadow_sigma),
    }


def render(cfg: dict, solver_block: str = "", deploy_dir: Path | None = None) -> str:
    return pjsr.render(pjsr.template(TEMPLATE, deploy_dir), cfg, solver_block)


# ----------------------------------------------------------------- records

def _file_entry(p: Path, role: str, filt: str, run: RunInfo | None) -> dict:
    p = Path(p)
    st = p.stat() if p.exists() else None
    return {"role": role, "filter": filt, "path": str(p), "bytes": st.st_size if st else None,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S") if st else "",
            "run": str(run.run_dir) if run else "", "run_version": run.version if run else None}


def outputs(out_dir: Path) -> list[str]:
    return [str(p) for p in sorted((Path(out_dir) / "final").glob("*"))
            if p.suffix.lower() in (".xisf", ".tif", ".jpg", ".json")]


def steps(out_dir: Path) -> dict | None:
    for f in sorted((Path(out_dir) / "final").glob("*_blend_steps.json")):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def build_manifest(o: Options, inp: Inputs, run_dir: Path, cfg: dict, pi: dict | None,
                   timing: list) -> dict:
    files = [_file_entry(inp.osc, "osc", "OSC", inp.osc_run)]
    files += [_file_entry(inp.rc16[f], "rc16", f, inp.rc16_run) for f in sorted(inp.rc16, key=_filter_rank)]
    return {"version": 1, "kind": "blend", "target": o.target, "run_name": Path(run_dir).name,
            "run_dir": str(run_dir), "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "stage": inp.stage, "inputs": files, "luminance_from": inp.lum_filters(),
            "notes": inp.notes, "config": cfg, "pixinsight": pi, "timing": timing,
            "outputs": outputs(Path(run_dir) / "out")}


def build_ledger(o: Options, inp: Inputs, run_dir: Path, man: dict, pi: dict | None, timing: list):
    from photonscript.integration.ledger import repo_commit
    from photonscript.shared import ledger as L
    out_dir = Path(run_dir) / "out"
    st = steps(out_dir)
    machine = {
        "run_dir": str(run_dir), "repo_commit": repo_commit(), "kind": "blend",
        "inputs": man["inputs"], "luminance_from": man["luminance_from"], "notes": inp.notes,
        "options": {k: man["config"][k] for k in ("stage", "weight", "inset_frac", "feather_frac",
                                                  "lum_mask", "core", "core_margin_frac")},
        "blend": {"script": "blend_run.js (deploy/blend_rc16_osc.js)",
                  "ok": (pi or {}).get("ok"), "minutes": (pi or {}).get("minutes"),
                  "last_line": (pi or {}).get("last_line", ""),
                  "products": (st or {}).get("products"), "steps": st,
                  "log": str(out_dir / "blend.log")},
        "outputs": man["outputs"],
        "timing": {"started_utc": timing[0][1] if timing else "",
                   "finished_utc": timing[-1][2] if timing else "",
                   "minutes": round(sum(float(r[3]) for r in timing), 2) if timing else 0,
                   "stages": timing},
    }
    return L.Ledger(campaign=o.target, rig="piggyback", kind="blend", run=Path(run_dir).name,
                    machine=machine)


def write_ledger(led, run_dir: Path, blend_root: Path) -> Path:
    from photonscript.integration import ledger as writer
    return writer.write_for_run(led, run_dir, blend_root)


# ----------------------------------------------------------------- run

def run(o: Options, echo=print) -> dict:
    if not 0.0 <= o.weight <= 1.0:
        raise BlendError("--weight must be within 0..1")
    timer = Timer(None, echo)                 # the file is set once the run folder exists
    with timer.stage("find the OSC and RC16 masters"):
        inp = discover(o)
        for n in inp.notes:
            echo("  " + n)
        echo(f"  OSC:  {inp.osc}")
        for f in sorted(inp.rc16, key=_filter_rank):
            echo(f"  RC16 {f}: {inp.rc16[f]}")
    run_dir = Path(o.out) if o.out else default_run_dir(o)
    for src in [inp.osc, *inp.rc16.values()]:
        if is_inside(src, run_dir):
            raise BlendError(f"an input sits inside the blend folder {run_dir}: pick a NEW --out")
    result: dict = {"run_dir": str(run_dir), "dry_run": o.dry_run, "stage": inp.stage,
                    "osc": str(inp.osc), "rc16": {k: str(v) for k, v in inp.rc16.items()},
                    "luminance_from": inp.lum_filters(), "notes": inp.notes}
    if o.dry_run:
        cfg = blend_config(o, inp, run_dir)
        problems = pjsr.check(render(cfg))
        result["config"] = cfg
        result["script_problems"] = problems
        echo(f"  would write {run_dir} (script check: {'ok' if not problems else '; '.join(problems)})")
        echo("dry run: nothing written, PixInsight not started")
        return result
    if run_dir.exists() and any(run_dir.iterdir()):
        raise BlendError(f"{run_dir} exists and is not empty: every blend gets a NEW folder "
                         "(pass another --out or leave it out)")
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir = run_dir / "out"
    timer.path = out_dir / "timing.csv"

    with timer.stage("generate the PixInsight script"):
        cfg = blend_config(o, inp, run_dir)
        block, missing = pjsr.solver_include(o.pixinsight)
        if missing:
            echo(f"  ImageSolver files missing ({', '.join(missing)}): no plate solve, "
                 "StarAlignment only (no WCS fallback)")
        js = pjsr.write(run_dir / "blend_run.js", render(cfg, block))
        echo(f"  {js.name}")
    result["script"] = str(js)

    pi = None
    if o.run_pixinsight:
        with timer.stage("PixInsight blend (launch to exit)"):
            pi = runner.run_script(js, out_dir / "blend.log", exe=o.pixinsight, echo=echo)
            echo(f"  blend: {'EXIT OK' if pi['ok'] else 'FAILED'} in {pi['minutes']} min ({pi['last_line']})")
        result["blend"] = pi
    else:
        echo("  PixInsight not started (--no-pixinsight). Run it later with:")
        echo(f'    "{o.pixinsight}" -n --automation-mode --run={js} --force-exit')

    with timer.stage("manifest + ledger"):
        man = build_manifest(o, inp, run_dir, cfg, pi, timer.rows)
        (run_dir / "manifest.json").write_text(json.dumps(man, indent=1, ensure_ascii=True, default=str),
                                               encoding="ascii")
        led = build_ledger(o, inp, run_dir, man, pi, timer.rows)
        result["ledger"] = str(write_ledger(led, run_dir, run_dir.parent))
        result["ledger_version"] = led.version
    timer.flush()
    result["outputs"] = man["outputs"]
    result["timing"] = timer.rows
    echo(f"done: {run_dir}")
    return result
