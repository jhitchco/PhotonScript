"""HDR combine of mono exposure-group masters (after PS-177).

PS-177 stacks every exposure length of a mono filter on its own: the length
with the most integration time is master_<F>, every other length is
master_<F>_<exp> (RC16 M31: master_L = 300 s + master_L_30s, master_B = 30 s
+ master_B_300s). This module plans and runs the combine that used to be
left to a human: per filter with two or more of those masters,
deploy/hdr_combine.js (HDRComposition, else a fitted replacement of the
saturated pixels, see the template) writes out/master/master_<F>_hdr.xisf
and out/master/hdr_steps.json.

The finish then runs on master_<F>_hdr (named <target>_<F>, as the long
master would be) instead of that filter's single-exposure masters, and the
PS-153 blend discovery prefers master_<F>_hdr over master_<F> (blend.masters).

Config integrate_hdr_combine (default on) switches it; it only ever applies
to mono runs (an OSC stack holds every exposure in one master).
"""

from __future__ import annotations

import json
from pathlib import Path

from photonscript.integration import pjsr, runner

TEMPLATE = "hdr_combine.js"
SCRIPT = "hdr_run.js"
LOG = "hdr.log"
STEPS = "hdr_steps.json"
SUFFIX = "_hdr"
METHOD = "auto"          # auto (HDRComposition, else fit) | hdrcomposition | fit
THRESHOLD = 0.80         # of the longer master's maximum (HDRComposition's binarizing default)
BLEND_WIDTH = 0.15
MASK_SIGMA = 2.0


def stack_exp(stack: dict) -> float:
    """The exposure of a single-length (mono) stack spec."""
    return max(float(g["exp"]) for g in stack.get("groups") or [{"exp": 0}])


def plan(stacks: list[dict], out_dir: Path, made: set[str] | None = None) -> list[dict]:
    """The HDR items of a run: per mono filter with at least two stacks of
    different lengths (that made a master when `made` is given), inputs
    longest first. A stack that mixes lengths (OSC) never takes part."""
    out_dir = Path(out_dir)
    by: dict[str, list[dict]] = {}
    for s in stacks:
        if s.get("mixed_exposures") or len(s.get("groups") or []) != 1:
            continue
        if made is not None and s["name"] not in made:
            continue
        by.setdefault(s["filter"], []).append(s)
    items = []
    for filt in sorted(by):
        ss = sorted(by[filt], key=lambda s: -stack_exp(s))
        if len({stack_exp(s) for s in ss}) < 2:
            continue
        name = f"{filt}{SUFFIX}"
        items.append({
            "filter": filt, "name": name,
            "output": pjsr.fwd(out_dir / "master" / f"master_{name}.xisf"),
            "inputs": [{"stack": s["name"], "exp": stack_exp(s),
                        "path": pjsr.fwd(out_dir / "master" / f"master_{s['name']}.xisf")} for s in ss]})
    return items


def config(out_dir: Path, items: list[dict]) -> dict:
    return {"out": pjsr.fwd(out_dir), "items": items, "method": METHOD, "threshold": THRESHOLD,
            "blend_width": BLEND_WIDTH, "mask_sigma": MASK_SIGMA}


def render(out_dir: Path, items: list[dict], deploy_dir: Path | None = None) -> str:
    return pjsr.render(pjsr.template(TEMPLATE, deploy_dir), config(out_dir, items))


def write_script(run_dir: Path, items: list[dict]) -> Path:
    return pjsr.write(Path(run_dir) / SCRIPT, render(Path(run_dir) / "out", items))


def read_steps(out_dir: Path) -> dict | None:
    p = Path(out_dir) / "master" / STEPS
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - not written (PixInsight failed early) or half-written
        return None


def finish_masters(masters: list[dict], items: list[dict], made: set[str] | list[str],
                   out_dir: Path) -> list[dict]:
    """The finish's [{name, path}] with each filter whose HDR master was
    made (its name in `made`) finished from master_<F>_hdr instead of its
    single-exposure masters. Each master is matched to its stack by the
    file name (master_<stack>.xisf); the HDR entry takes the place of the
    filter's first master and is named <prefix>_<F> (what the long master
    of that filter would be called, so blend and review find it)."""
    swap: dict[str, dict] = {}            # input stack name -> item
    for it in items:
        if it["name"] in made:
            for x in it["inputs"]:
                swap[x["stack"]] = it
    out, done = [], set()
    for m in masters:
        stack = Path(m["path"]).stem[len("master_"):]
        it = swap.get(stack)
        if it is None:
            out.append(m)
            continue
        if it["name"] in done:
            continue
        done.add(it["name"])
        prefix = m["name"][: len(m["name"]) - len(stack)] if m["name"].endswith(stack) else m["name"] + "_"
        out.append({"name": prefix + it["filter"],
                    "path": pjsr.fwd(Path(out_dir) / "master" / f"master_{it['name']}.xisf")})
    return out


def run(run_dir: Path, items: list[dict], *, exe: str, echo=print) -> dict:
    """Run hdr_run.js in PixInsight (one instance, the integrate runner) and
    report {ok, made: [names], steps, minutes, ...}. A filter whose combine
    failed simply has no master_<F>_hdr; nothing here raises for that."""
    run_dir = Path(run_dir)
    out_dir = run_dir / "out"
    js = write_script(run_dir, items)
    r = runner.run_script(js, out_dir / LOG, exe=exe, echo=echo)
    made = [it["name"] for it in items
            if (out_dir / "master" / f"master_{it['name']}.xisf").is_file()]
    steps = read_steps(out_dir)
    for it in items:
        if it["name"] in made:
            echo(f"  HDR {it['filter']}: master_{it['name']}.xisf from "
                 + " + ".join(x["stack"] for x in it["inputs"]))
        else:
            echo(f"  HDR {it['filter']}: not made (the finish keeps the single-exposure masters)")
    return dict(r, made=made, items=items, steps=steps, script=str(js), log=str(out_dir / LOG))
