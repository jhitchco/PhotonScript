"""OSC (Piggy-600) core HDR combine (2026-10-14, "Piggy-600: shoot M31 at
120 s (nucleus clips at 300 s)").

An OSC stack holds every exposure length in one debayered master (PS-177),
so the M31 nucleus that 122 of 162 300 s subs burned stays flat-topped in
master_OSC even though the 120 s subs do not clip it. This module adds the
mono HDR idea (integration/hdr.py) for the nucleus only:

1. plan_subset(stack, counts): when the stack mixes lengths and has at
   least min_frames subs at or under integrate_osc_hdr_short_max_s (180 s)
   plus a longer group, integrate_stack.js also integrates those short
   subs (same calibration, debayer and registration as the full stack, no
   LocalNormalization: they keep their own scale) into
   out/master/osc_hdr/master_<S>_short.xisf (a subfolder, so blend and
   the finish never take it for a filter master). The full master is made
   exactly as before.
2. after the integration (run): the full master's core goes through
   core_master's plateau check. Not clipped (or unreadable): nothing more,
   today's single master. Clipped: deploy/hdr_core_osc.js (osc_hdr_run.js)
   replaces the nucleus region (a disk REGION_X core radii around the
   plateau, feathered over FEATHER_FRAC of it) from the short master fitted
   to the full one per channel (least squares over unclipped signal near
   the core; slope within 3x of the exposure ratio, else the ratio), with a
   smoothstep from (THRESHOLD - BLEND_WIDTH) to THRESHOLD of each channel's
   core maximum, and writes out/master/master_<S>_hdr.xisf (PSHDR
   'osc-core'). The finish then runs on it (named <target>_OSC as before),
   blend prefers it (master_<F>_hdr), the core check reads it.

Every step is guarded: no short master, a failed check, a failed or
refused combine all leave master_<S>.xisf as the master the finish uses.
Lines go to the run log and out/pipeline.log ("OSC core combine: ..."),
the record into the ledger's machine.hdr block (kind osc_core).
"""

from __future__ import annotations

import json
from pathlib import Path

from photonscript.integration import hdr, pjsr, runner

TEMPLATE = "hdr_core_osc.js"
SCRIPT = "osc_hdr_run.js"
LOG = "osc_hdr.log"
SUBDIR = "osc_hdr"
STEPS = "osc_hdr_steps.json"
SHORT_SUFFIX = "_short"
SHORT_MAX_S = 180.0
THRESHOLD = 0.80          # of each channel's core maximum (the plateau)
BLEND_WIDTH = 0.15        # smoothstep starts this far below THRESHOLD
REGION_X = 3.0            # replacement disk radius, in core radii
FEATHER_FRAC = 0.30       # of the disk radius
KIND = "osc_core"


def short_name(stack_name: str) -> str:
    return f"{stack_name}{SHORT_SUFFIX}"


def short_path(out_dir: Path, stack_name: str) -> Path:
    return Path(out_dir) / "master" / SUBDIR / f"master_{short_name(stack_name)}.xisf"


def plan_subset(stack: dict, counts: dict, *, max_s: float = SHORT_MAX_S,
                min_frames: int = 3) -> tuple[dict | None, str]:
    """(subset spec for integrate_stack.js or None, note). counts: exposure
    -> staged lights of that length in this stack."""
    groups = stack.get("groups") or []
    if len(groups) < 2:
        return None, ""
    short = [g for g in groups if float(g["exp"]) <= max_s + 1e-6]
    long = [g for g in groups if float(g["exp"]) > max_s + 1e-6]
    if not short or not long:
        return None, ""
    n = sum(int(counts.get(float(g["exp"]), counts.get(round(float(g["exp"]), 2), 0)) or 0)
            for g in short)
    lens = ", ".join(f"{float(g['exp']):g} s" for g in short)
    if n < max(3, int(min_frames)):
        return None, (f"OSC core combine: only {n} short sub(s) ({lens}), fewer than "
                      f"{max(3, int(min_frames))}: no short master")
    return ({"name": short_name(stack["name"]), "groups": [g["name"] for g in short],
             "exps": [float(g["exp"]) for g in short], "max_s": float(max_s), "frames": n,
             "output": None},
            f"OSC core combine planned: {n} subs of {lens} also make "
            f"master_{short_name(stack['name'])} (for the core when it clips in master_{stack['name']})")


def attach(stacks: list[dict], counts_by_stack: dict, out_dir: Path, *, max_s: float,
           min_frames: int) -> list[str]:
    """Plan the short subset of every mixed OSC stack in place
    (stack["short_subset"]). Returns the notes."""
    notes = []
    for s in stacks:
        sub, note = plan_subset(s, counts_by_stack.get(s["name"], {}), max_s=max_s,
                                min_frames=min_frames)
        if note:
            notes.append(note)
        if sub:
            sub["output"] = pjsr.fwd(short_path(out_dir, s["name"]))
            s["short_subset"] = sub
    return notes


def _exp_of(stack: dict, counts: dict, exps) -> float:
    """The nominal exposure of the groups `exps` (the most frames wins)."""
    best = max(exps, key=lambda e: (counts.get(e, 0), e)) if exps else 0.0
    return float(best)


def item_for(stack: dict, counts: dict, out_dir: Path, core: dict, r_core_px: float) -> dict:
    sub = stack["short_subset"]
    long_exps = [float(g["exp"]) for g in stack["groups"] if float(g["exp"]) > sub["max_s"] + 1e-6]
    exp_long = _exp_of(stack, counts, long_exps)
    exp_short = _exp_of(stack, counts, sub["exps"])
    name = f"{stack['filter']}{hdr.SUFFIX}"
    region = max(12.0, REGION_X * float(r_core_px))
    return {"kind": KIND, "filter": stack["filter"], "name": name,
            "output": pjsr.fwd(Path(out_dir) / "master" / f"master_{name}.xisf"),
            "inputs": [{"stack": stack["name"], "exp": exp_long,
                        "path": pjsr.fwd(Path(out_dir) / "master" / f"master_{stack['name']}.xisf")},
                       {"stack": sub["name"], "exp": exp_short, "path": sub["output"]}],
            "ratio": round(exp_long / exp_short, 4) if exp_short else 1.0,
            "core": {"x": float(core["x"]), "y": float(core["y"]),
                     "clipped_px": int(core.get("clipped_px") or 0),
                     "region_px": round(region, 1),
                     "feather_px": round(max(4.0, FEATHER_FRAC * region), 1)}}


def config(out_dir: Path, item: dict) -> dict:
    return {"out": pjsr.fwd(out_dir), "steps": pjsr.fwd(Path(out_dir) / "master" / SUBDIR / STEPS),
            "item": item, "threshold": THRESHOLD, "blend_width": BLEND_WIDTH}


def render(out_dir: Path, item: dict, deploy_dir: Path | None = None) -> str:
    return pjsr.render(pjsr.template(TEMPLATE, deploy_dir), config(out_dir, item))


def read_steps(out_dir: Path) -> dict | None:
    try:
        return json.loads((Path(out_dir) / "master" / SUBDIR / STEPS).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _log(out_dir: Path, lines: list[str]) -> None:
    try:
        with (Path(out_dir) / "pipeline.log").open("a", encoding="ascii", errors="replace") as fh:
            for ln in lines:
                fh.write(ln + "\n")
    except OSError:
        pass


def run(run_dir: Path, stack: dict, counts: dict, *, r_core_px: float, warn_px: int,
        exe: str, echo=print, check=None, target=None,
        object_type: str | None = None) -> dict:
    """After integrate_run.js: check the full master's core, combine when it
    is clipped. Never raises. Returns the ledger's hdr block shape: {kind,
    ok, made, items, steps, status, why, script, log, minutes}."""
    run_dir = Path(run_dir)
    out_dir = run_dir / "out"
    sub = stack.get("short_subset") or {}
    res = {"kind": KIND, "ok": False, "made": [], "items": [], "steps": None,
           "status": "skipped", "why": "", "minutes": 0.0,
           "script_label": f"{SCRIPT} (deploy/{TEMPLATE})", "log": str(out_dir / LOG)}

    def done(status, why):
        res["status"], res["why"] = status, why
        line = f"OSC core combine: {why}"
        echo("  " + line)
        _log(out_dir, [line])
        return res

    try:
        sp = Path(sub.get("output") or short_path(out_dir, stack["name"]))
        lp = out_dir / "master" / f"master_{stack['name']}.xisf"
        if not lp.is_file():
            return done("skipped", f"no master_{stack['name']} (single master kept)")
        if not sp.is_file():
            return done("skipped", f"no master_{sub.get('name', short_name(stack['name']))} "
                                   "(the short subs made no master, see pipeline.log): "
                                   f"master_{stack['name']} kept")
        from photonscript.integration import core_master
        skip = core_master.skip_line(target, object_type) if target else None
        if skip:   # an emission nebula / cluster: no nucleus to combine
            return done("skipped", f"{skip}: single master kept")
        chk = (check or core_master.check_master)(lp, r_core_px, warn_px)
        res["long_check"] = {k: chk.get(k) for k in ("clipped_px", "still_clipped", "x", "y",
                                                       "r_px", "error", "line") if k in chk}
        if chk.get("error"):
            return done("skipped", f"core of master_{stack['name']} not checked ({chk['error']}): "
                                   "single master kept")
        # the same rule and words as the core check line (core_master.verdict)
        v = core_master.verdict(f"master_{stack['name']}", chk.get("clipped_px"), warn_px)
        if not v["still_clipped"]:
            return done("skipped", f"{v['line']}: single master kept")
        it = item_for(stack, counts, out_dir, chk, r_core_px)
        res["items"] = [it]
        js = pjsr.write(run_dir / SCRIPT, render(out_dir, it))
        res["script"] = str(js)
        echo(f"  OSC core combine: {v['line']}: {js.name}")
        r = runner.run_script(js, out_dir / LOG, exe=exe, echo=echo)
        res.update(ok=bool(r.get("ok")), minutes=r.get("minutes", 0.0),
                   last_line=r.get("last_line", ""))
        res["steps"] = read_steps(out_dir)
        st = ((res["steps"] or {}).get("item") or {})
        if Path(it["output"]).is_file() and st.get("status") == "ok":
            res["made"] = [it["name"]]
            f = st.get("fits") or []
            fit_txt = ", ".join(f"{x.get('channel')}: a {x.get('a', 0):.3f} ({x.get('how', '?')})"
                                for x in f)
            return done("ok", f"master_{it['name']} = master_{stack['name']} with its nucleus "
                              f"({st.get('replaced_px', '?')} px, disk {it['core']['region_px']:g} px) "
                              f"from master_{sub.get('name')}" + (f"; fit {fit_txt}" if fit_txt else ""))
        if st.get("status") == "ok":
            why = "no output file"
        else:
            why = st.get("error") or ("PixInsight: " + str(r.get("last_line") or "no steps record"))
        return done("failed", f"not made ({why}): master_{stack['name']} kept")
    except Exception as e:  # noqa: BLE001 - never costs the run its master
        return done("failed", f"skipped ({e}): master_{stack['name']} kept")
