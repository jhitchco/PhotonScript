"""Narrowband two-rig blend readiness (the Heart Nebula, 2026-10-14).

The NB blend (integration/blend.py --mode nb, docs/OSC_INTEGRATION.md 0d)
puts RC16 Ha (main input), OIII (G and B) and SII (SHO core) into the
Piggy-600 frame. This answers, per two-rig narrowband goal (an RC16 Ha plan
and a Piggy-600 plan, target_planner.feeds_nb_blend): what would the blend
use today, is it worth running, and how many nights to each threshold.

    thresholds(config)                       {Ha, OIII, SII: hours}
    target_readiness(config, project, *, piggy=, planned=, rc16_master=)
    report(config, projects, *, target="", planned=None, ledgers=True)
    tonight_planned(config, projects, now=None)  {target: {filter: h/night}}
    morning_line(config, projects, planned=None) -> str | None
    format_report(rep) -> str                 CLI text

Counts are the accepted RC16 subs the goal holds (each plan's acquired /
acquired_s, the goal bars' numbers: what a new RC16 integrate would stack).
The Piggy master is the goal's newest Piggy-600 integration ledger that
integrated. Nights to a threshold = hours short / hours planned per night
for that filter (tonight's plan, target_planner.plan_night_sequence; None
when tonight's plan has none). Read-only; never raises from the callers'
point of view (report / morning_line catch per goal).

Thresholds (config): blend_nb_min_ha_h 2.0 (the blend), blend_nb_min_oiii_h
1.5 (add OIII), blend_nb_min_sii_h 1.5 (SHO core, with OIII).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)

NB = ("Ha", "OIII", "SII")
RC16 = "rc16"


def thresholds(config) -> dict[str, float]:
    return {"Ha": float(getattr(config, "blend_nb_min_ha_h", 2.0)),
            "OIII": float(getattr(config, "blend_nb_min_oiii_h", 1.5)),
            "SII": float(getattr(config, "blend_nb_min_sii_h", 1.5))}


def rc16_accepted(project) -> dict[str, dict]:
    """{filter: {subs, hours}} of the goal's RC16 narrowband plans."""
    out = {}
    for e in project.exposure_plans:
        if (getattr(e, "rig", RC16) or RC16) != RC16:
            continue
        f = e.filter_type.value
        if f in NB and f not in out:
            out[f] = {"subs": int(e.acquired),
                      "hours": round(e.long_seconds_done() / 3600.0, 2)}
    return out


def piggy_master(config, project) -> dict | None:
    """The newest Piggy-600 integration ledger of the goal that integrated
    (on the scheduler: the desktop posts them), else None."""
    from photonscript.scheduler.integrations import ledgers_for
    for led in ledgers_for(config, project.id or "", "piggyback"):
        if (led.kind or "integration") != "integration":
            continue
        if ((led.machine.get("integration") or {}).get("ok")) is False:
            continue
        return {"version": led.version, "run": led.run,
                "hours": round(led.hours, 1),
                "date": str(led.created_at or "")[:10]}
    return None


def rc16_master(config, project) -> dict | None:
    """The newest RC16 integration ledger: {version, run, filters: {f:
    {subs, hours}}} of its narrowband stacks (what a blend run today would
    take as its RC16 master), else None."""
    from photonscript.scheduler.integrations import ledgers_for
    for led in ledgers_for(config, project.id or "", RC16):
        if (led.kind or "integration") != "integration":
            continue
        sbf = ((led.machine.get("acquisition") or {}).get("subs_by_filter")
               or {})
        filters = {}
        for f, v in sbf.items():
            if f in NB and isinstance(v, dict):
                filters[f] = {"subs": int(v.get("integrated") or 0),
                              "hours": round(float(v.get("integrated_s") or 0)
                                             / 3600.0, 2)}
        return {"version": led.version, "run": led.run, "filters": filters}
    return None


def nights_to(have_h: float, need_h: float, per_night_h: float | None):
    """0 when met; else ceil(short / per night); None without a plan."""
    if have_h >= need_h:
        return 0
    if not per_night_h or per_night_h <= 0:
        return None
    return int(math.ceil((need_h - have_h) / per_night_h - 1e-9))


def target_readiness(config, project, *, piggy=None, planned: dict | None = None,
                     rc16_master_info=None) -> dict:
    """One goal. piggy: piggy_master() (None = no Piggy master yet);
    planned: {filter: hours per night} for this target."""
    thr = thresholds(config)
    acc = rc16_accepted(project)
    planned = planned or {}
    ch = {}
    for f in NB:
        if f not in acc:
            continue
        a = acc[f]
        pn = planned.get(f)
        ch[f] = {**a, "need_h": thr[f], "met": a["hours"] >= thr[f],
                 "planned_h_night": round(pn, 2) if pn else None,
                 "nights": nights_to(a["hours"], thr[f], pn)}
    ha_ok = bool(ch.get("Ha", {}).get("met"))
    oiii_ok = bool(ch.get("OIII", {}).get("met"))
    sii_ok = bool(ch.get("SII", {}).get("met"))
    ready = piggy is not None and ha_ok
    uses = (["Ha"] + (["OIII"] if oiii_ok else [])) if ha_ok else []
    core = ("SHO" if ha_ok and oiii_ok and sii_ok
            else "HOO" if ha_ok and oiii_ok else None)
    out = {"target": project.target.name, "project_id": project.id,
           "piggy_master": piggy, "rc16_master": rc16_master_info,
           "channels": ch, "ready": ready, "uses": uses, "core": core,
           "thresholds": thr}
    out["line"] = line(out)
    return out


def _ch_text(f: str, c: dict) -> str:
    t = f"{f} {c['hours']:.1f}/{c['need_h']:g} h ({c['subs']} subs)"
    if c["met"]:
        return t + " ok"
    if c["nights"] is not None:
        return t + (f", ~{c['nights']} night{'s' if c['nights'] != 1 else ''}"
                    f" at {c['planned_h_night']:.1f} h/night")
    return t + ", not planned tonight"


def line(r: dict) -> str:
    """One line: "Heart Nebula NB blend: not ready: Ha 0.0/2 h (0 subs),
    ~1 night at 2.8 h/night; OIII ...; Piggy master v2 3.0 h"."""
    if r["ready"]:
        head = ("ready: Ha" + (" + OIII" if "OIII" in r["uses"] else "")
                + (f", {r['core']} core" if r["core"] else ", no SHO / HOO core"))
    elif r["piggy_master"] is None and r["channels"].get("Ha", {}).get("met"):
        head = "waits for a Piggy-600 master"
    else:
        head = "not ready"
    chans = "; ".join(_ch_text(f, c) for f, c in r["channels"].items())
    pm = r["piggy_master"]
    tail = (f"Piggy master v{pm['version']} {pm['hours']:g} h" if pm
            else "no Piggy master yet")
    return f"{r['target']} NB blend: {head}: {chans}; {tail}"


def blend_goals(projects, target: str = "") -> list:
    from photonscript.scheduler.target_planner import feeds_nb_blend
    from photonscript.shared.target_names import target_key
    k = target_key(target) if target else ""
    out = []
    for p in projects or []:
        if k:
            if target_key(p.target.name) != k:
                continue
        elif not p.active:
            continue
        if feeds_nb_blend(p):
            out.append(p)
    return out


def tonight_planned(config, projects, now: datetime | None = None
                    ) -> dict[str, dict[str, float]]:
    """{target: {filter: hours}} of tonight's plan (per pass), on copies of
    the goals; {} when the planner cannot run."""
    try:
        from photonscript.scheduler.target_planner import plan_night_sequence
        copies = [p.model_copy(deep=True) for p in projects]
        seq = plan_night_sequence(copies, config, now or datetime.utcnow())
    except Exception as e:  # noqa: BLE001
        logger.info("blend readiness: tonight's plan unavailable: %s", e)
        return {}
    out: dict[str, dict[str, float]] = {}
    for t in seq:
        d = out.setdefault(t.name, {})
        for e in t.exposures:
            if (getattr(e, "rig", RC16) or RC16) != RC16:
                continue
            f = e.filter_type.value
            d[f] = d.get(f, 0.0) + e.count * float(e.exposure_seconds) / 3600.0
    return out


def report(config, projects, *, target: str = "", planned: dict | None = None,
           ledgers: bool = True, now: datetime | None = None) -> dict:
    """Every active two-rig NB goal (or the one named). planned None =
    compute tonight's plan."""
    goals = blend_goals(projects, target)
    if planned is None:
        planned = tonight_planned(config, projects, now) if goals else {}
    rows = []
    for p in goals:
        try:
            pig = piggy_master(config, p) if ledgers else None
            rcm = rc16_master(config, p) if ledgers else None
            rows.append(target_readiness(config, p, piggy=pig,
                                         planned=planned.get(p.target.name),
                                         rc16_master_info=rcm))
        except Exception as e:  # noqa: BLE001
            logger.warning("blend readiness %s: %s", p.target.name, e)
            rows.append({"target": p.target.name, "project_id": p.id,
                         "error": str(e),
                         "line": f"{p.target.name} NB blend: unavailable ({e})"})
    return {"targets": rows, "thresholds": thresholds(config),
            "planned_from": "tonight's plan" if planned else None}


def morning_line(config, projects, planned: dict | None = None) -> str | None:
    """One morning-report line for every two-rig NB goal, None without one."""
    rep = report(config, projects, planned=planned)
    if not rep["targets"]:
        return None
    return " | ".join(r["line"] for r in rep["targets"])


def format_report(rep: dict) -> str:
    if not rep.get("targets"):
        return "No two-rig narrowband goal (RC16 Ha plan + Piggy-600 plan)."
    thr = rep.get("thresholds") or {}
    out = ["Narrowband blend readiness (accepted RC16 subs; thresholds "
           + ", ".join(f"{f} {h:g} h" for f, h in thr.items()) + ")"]
    for r in rep["targets"]:
        out.append("")
        if r.get("error"):
            out.append(r["line"])
            continue
        state = ("READY" if r["ready"] else "not ready")
        out.append(f"{r['target']}: {state}"
                   + (f"; blend uses {' + '.join(r['uses'])}" if r["uses"] else "")
                   + (f"; core {r['core']}" if r["core"] else ""))
        pm = r.get("piggy_master")
        out.append("  Piggy-600 master: " + (
            f"v{pm['version']} {pm['hours']:g} h ({pm['date']}, {pm['run']})"
            if pm else "none yet (run photonscript integrate --rig piggyback)"))
        for f, c in r["channels"].items():
            n = c["nights"]
            when = ("met" if c["met"] else
                    f"~{n} night{'s' if n != 1 else ''} at "
                    f"{c['planned_h_night']:.1f} h/night" if n is not None
                    else "not in tonight's plan")
            out.append(f"  RC16 {f:<4} {c['subs']:>4} subs {c['hours']:>5.1f} h"
                       f" / {c['need_h']:g} h  {when}")
        rm = r.get("rc16_master")
        if rm:
            fs = ", ".join(f"{f} {v['subs']} subs {v['hours']:.1f} h"
                           for f, v in rm["filters"].items()) or "no NB stacks"
            out.append(f"  RC16 master now: v{rm['version']} ({fs})")
    return "\n".join(out)
