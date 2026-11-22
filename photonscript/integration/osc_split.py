"""PS-265: split the OSC masters of a plain-folder (Seestar) run into mono
masters, so the mono color combine (mono_color.py, finish_mono_color.js)
can make an HOO image with RGB stars from one OSC run:

    --hoo <stack>   a dual-band (LP) master  -> master_Ha (R), master_OIII ((G + B) / 2)
    --rgb <stack>   a broadband (IR-cut) one -> master_R, master_G, master_B

The masters of one integrate run share one registration (and, drizzled,
one geometry), so the split masters line up without resampling. A stack's
drizzle master (out/master/drizzle/master_<S>_drizzle<N>x.xisf) is taken
when there is one, else out/master/master_<S>.xisf.

The run itself is only read: the split goes into a NEW folder (default
<staging root>/<run>_split) with out/master/ and a manifest.json naming the
rig, target, optics and sources; then

    photonscript finish --mono-run <split folder> --rig seestar --target <T>

makes <T>_RGB and <T>_HOO (stars from the RGB star layer).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from photonscript.integration import pilock, pjsr, runner

TEMPLATE = "osc_split_channels.js"
SCRIPT = "split_run.js"
LOG = "split.log"
_DRZ = re.compile(r"^master_(?P<s>.+)_drizzle(?P<n>\d+)x$")


class SplitError(ValueError):
    pass


def source_master(run_dir: Path, stack: str) -> tuple[Path, int]:
    """(master path, drizzle scale or 1) of one stack of a run: the drizzle
    master when there is one (the largest scale), else master_<stack>."""
    out = Path(run_dir) / "out" / "master"
    best, scale = None, 1
    for p in sorted((out / "drizzle").glob(f"master_{stack}_drizzle*x.xisf")):
        m = _DRZ.match(p.stem)
        if m and m.group("s") == stack and int(m.group("n")) > scale:
            best, scale = p, int(m.group("n"))
    if best is not None:
        return best, scale
    p = out / f"master_{stack}.xisf"
    if p.is_file():
        return p, 1
    raise SplitError(f"no master for stack {stack!r} in {out}")


def run_meta(run_dir: Path) -> dict:
    """rig, target and optics of the source run (ledger.json, manifest.json,
    finish_run.js values)."""
    run_dir = Path(run_dir)
    meta: dict = {"rig": "", "target": "", "focal_mm": None, "pixel_um": None}
    for name in ("ledger.json", "manifest.json"):
        p = run_dir / name
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            meta["rig"] = meta["rig"] or str(d.get("rig") or "")
            meta["target"] = meta["target"] or str(d.get("campaign") or d.get("target") or "")
    fj = run_dir / "finish_run.js"
    if fj.is_file():
        t = fj.read_text(encoding="ascii", errors="replace")
        for key, k in (("FOCAL_MM", "focal_mm"), ("PIXEL_UM", "pixel_um")):
            m = re.search(rf"var {key}\s*=\s*([0-9.]+)", t)
            if m:
                meta[k] = float(m.group(1))
    return meta


def plan(run_dir: Path, hoo: str = "", rgb: str = "") -> list[dict]:
    if not hoo and not rgb:
        raise SplitError("nothing to split: pass --hoo <stack> and / or --rgb <stack>")
    items = []
    scales = set()
    for kind, stack in (("hoo", hoo), ("rgb", rgb)):
        if not stack:
            continue
        p, n = source_master(run_dir, stack)
        scales.add(n)
        items.append({"kind": kind, "stack": stack, "src": pjsr.fwd(p), "drizzle": n})
    if len(scales) > 1:
        raise SplitError("the stacks have different drizzle scales (" + ", ".join(
            f"{it['stack']} {it['drizzle']}x" for it in items) + "): they would not line up")
    return items


def default_out(run_dir: Path, staging_root: Path | None = None) -> Path:
    run_dir = Path(run_dir)
    root = Path(staging_root) if staging_root else run_dir.parent
    return root / f"{run_dir.name}_split"


def render(items: list[dict], out_dir: Path, deploy_dir: Path | None = None) -> str:
    cfg = {"out": pjsr.fwd(out_dir), "items": items}
    return pjsr.render(pjsr.template(TEMPLATE, deploy_dir), cfg)


def run(run_dir: Path, *, hoo: str = "", rgb: str = "", out: Path | None = None,
        staging_root: Path | None = None, exe: str = runner.DEFAULT_EXE, run_pixinsight: bool = True,
        lock_wait_min: float = 60.0, echo=print) -> dict:
    """Split, into a NEW folder; returns {split_dir, items, masters, ok,
    finish_line}. The source run is only read."""
    run_dir = Path(run_dir)
    if not (run_dir / "out" / "master").is_dir():
        raise SplitError(f"no out/master in {run_dir}")
    items = plan(run_dir, hoo, rgb)
    split = Path(out) if out else default_out(run_dir, staging_root)
    try:
        split.resolve().relative_to(run_dir.resolve())
        inside = True
    except ValueError:
        inside = False
    if inside:
        raise SplitError(f"the split folder {split} is inside the run {run_dir}")
    if split.exists() and any(split.iterdir()):
        raise SplitError(f"{split} exists and is not empty: pass a NEW --out")
    meta = run_meta(run_dir)
    n = items[0]["drizzle"]
    pixel = (meta["pixel_um"] or 2.9)
    if n > 1 and meta["pixel_um"] and abs(meta["pixel_um"] - 2.9) < 0.01:
        pixel = meta["pixel_um"] / n      # finish_run.js still names the native pixel
    split.mkdir(parents=True, exist_ok=True)
    (split / "out" / "master").mkdir(parents=True, exist_ok=True)
    js = pjsr.write(split / SCRIPT, render(items, split / "out"))
    man = {"version": 1, "kind": "osc_split", "rig": meta["rig"] or "seestar",
           "target": meta["target"], "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "source_run": str(run_dir), "items": items, "drizzle": n,
           "focal_mm": meta["focal_mm"], "pixel_um": pixel}
    (split / "manifest.json").write_text(json.dumps(man, indent=1), encoding="ascii")
    res = {"split_dir": str(split), "items": items, "script": str(js), "ok": None, "manifest": man}
    finish_line = (f'photonscript finish --mono-run "{split}" --rig {man["rig"]} --target "{meta["target"]}"'
                   + (f" --focal-mm {meta['focal_mm']:g}" if meta["focal_mm"] else "")
                   + f" --pixel-um {pixel:g}")
    res["finish_line"] = finish_line
    if not run_pixinsight:
        echo(f'  PixInsight not started: "{exe}" -n --automation-mode --run={js} --force-exit')
        return res
    with pilock.job(f"osc-split {run_dir.name}", enabled=True, wait_min=lock_wait_min, echo=echo):
        r = runner.run_script(js, split / "out" / LOG, exe=exe, echo=echo)
    res.update(ok=r["ok"], pixinsight=r)
    res["masters"] = sorted(str(p) for p in (split / "out" / "master").glob("master_*.xisf"))
    echo(f"  split: {'EXIT OK' if r['ok'] else 'FAILED'}; " + ", ".join(Path(p).stem for p in res["masters"]))
    echo("  next: " + finish_line)
    return res
