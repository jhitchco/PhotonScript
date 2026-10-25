"""Per-target Piggy-600 sub length for NINA #2's companion (2026-10-14,
"Piggy-600: shoot M31 at 120 s (nucleus clips at 300 s)").

The companion (calibration.generate_piggyback_companion_json) is one dumb
OSC light loop that rides whatever the RC16 points at, and used to shoot
one length all night: piggyback_exposure_s (the System page), cut by the
learned unguided limit in unguided_auto_length apply. A goal's OSC plan
length (exposure_plans, rig piggyback) never reached NINA #2.

Now a goal can set its own Piggy length with the "OSC" key of
exposure_overrides (PATCH /api/projects2/<id> {"exposure_overrides":
{..., "OSC": 120}}; ProjectStore.update also resizes the OSC plan to it).
Per night, in the RC16 target order:

    a target's length = its "OSC" override
                        else (piggy_core_length apply) the core-check
                        length for a target whose Piggy core clips
                        else piggyback_exposure_s
                        never above the applied unguided Piggy limit

and the companion follows the RC16's PS-180 handoff times: one light loop
per run of equal lengths, each ending at the handoff of its last target
(the last one at astro dawn). With no override (and the core mode not
apply) nothing changes: one loop at piggyback_exposure_s, as before.

    osc_override(project) -> float | None
    plan(config, targets, projects, *, cap_s=None, core=None) -> dict
        {base_s, cap_s, targets: [{target, exp_s, source, until_utc}],
         segments: [{exp_s, until_utc, targets}], single_s, changed}
    core_lengths(config, targets, projects) -> {target name: (s, why)}
        the piggy_core_length apply input (never raises)

piggy_core_length: off | advise (default: the morning report and the
target page say "Piggy M31 core clips at 300 s: set OSC to 120 s", nothing
changes) | apply (the planner also shoots that length on that target's
Piggy subs when the goal has no OSC override).
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MODES = ("off", "advise", "apply")


def core_mode(config) -> str:
    m = str(getattr(config, "piggy_core_length", "advise") or "advise").strip().lower()
    return m if m in MODES else "advise"


def osc_override(project) -> float | None:
    from photonscript.scheduler.project_store import osc_override_s
    return osc_override_s(getattr(project, "exposure_overrides", None)) if project else None


def _project_for(projects, name: str):
    from photonscript.shared.target_names import target_key
    try:
        k = target_key(name)
    except Exception:  # noqa: BLE001
        return None
    for p in projects or []:
        try:
            if target_key(p.target.name) == k:
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


def _is_test(t) -> bool:
    return any(bool(getattr(t, k, False)) for k in
               ("focus_calibration", "tracking_test", "optics_test"))


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.replace(microsecond=0).isoformat() + "Z"


def plan(config, targets, projects=None, *, cap_s: float | None = None,
         core: dict | None = None) -> dict:
    """The Piggy lengths for tonight's RC16 targets (module doc). cap_s:
    the applied unguided Piggy limit (None = none). core: {target name:
    (seconds, why)} from core_lengths (apply mode only)."""
    base = float(getattr(config, "piggyback_exposure_s", 120.0) or 120.0)
    rows = []
    for t in targets or []:
        if _is_test(t):
            continue
        name = str(getattr(t, "name", "?"))
        proj = _project_for(projects, name)
        ov = osc_override(proj)
        if ov:
            s, src = ov, "override"
        elif core and name in core and core[name][0]:
            s, src = float(core[name][0]), "core"
        else:
            s, src = base, "default"
        if cap_s and s > cap_s + 0.5:
            s, src = float(cap_s), src + "+unguided cap"
        rows.append({"target": name, "exp_s": float(s), "source": src,
                     "until_utc": _iso(getattr(t, "handoff_utc", None))})
        if getattr(t, "handoff_utc", None) is None:
            break       # this target keeps the mount to the loop end (PS-180)
    segs: list[dict] = []
    for r in rows:
        if segs and abs(segs[-1]["exp_s"] - r["exp_s"]) < 0.5:
            segs[-1]["until_utc"] = r["until_utc"]
            segs[-1]["targets"].append(r["target"])
        else:
            segs.append({"exp_s": r["exp_s"], "until_utc": r["until_utc"],
                         "targets": [r["target"]]})
    if segs:
        segs[-1]["until_utc"] = None        # the last loop runs to astro dawn
    lengths = {round(s["exp_s"], 1) for s in segs}
    eff_base = min(base, cap_s) if cap_s else base
    changed = any(r["source"] != "default" and abs(r["exp_s"] - eff_base) >= 0.5
                  for r in rows)
    single = segs[0]["exp_s"] if len(lengths) == 1 and segs else None
    return {"base_s": base, "cap_s": cap_s, "core_mode": core_mode(config),
            "targets": rows, "segments": segs if len(lengths) > 1 else [],
            "single_s": single, "changed": changed}


def core_lengths(config, targets, projects) -> dict:
    """{target name: (seconds, why)} for tonight's targets whose Piggy-600
    core clips at the length they would be shot at (core_check.osc_advice).
    Empty unless piggy_core_length is apply. Never raises."""
    if core_mode(config) != "apply":
        return {}
    out = {}
    try:
        from photonscript.scheduler import core_check as cc
        from photonscript.scheduler.sub_index import all_rows
        from photonscript.shared.target_names import target_key
        rows = [cc.norm(r) for r in all_rows(config, projects or ())]
        for t in targets or []:
            if _is_test(t):
                continue
            name = str(getattr(t, "name", "?"))
            proj = _project_for(projects, name)
            if osc_override(proj):
                continue
            k = target_key(name)
            mine = [r for r in rows if r["rig"] == "piggyback"
                    and target_key(r["target"]) == k]
            if not mine:
                continue
            for c in cc.coverage(config, mine, proj):
                adv = c.get("osc_advice")
                if c.get("rig") == "piggyback" and adv and adv.get("suggest_s"):
                    out[name] = (float(adv["suggest_s"]), adv.get("why", ""))
    except Exception as e:  # noqa: BLE001 - the night plans as before
        logger.warning("Piggy core lengths skipped: %s", e)
        return {}
    return out


def companion_kwargs(rec: dict | None) -> dict:
    """What _dispatch_piggyback_companion passes on: {"exp_s": L} for one
    length that differs from the config, {"segments": [...]} for several,
    {} for no change. until_utc back to datetimes."""
    if not rec:
        return {}
    if rec.get("segments"):
        segs = []
        for s in rec["segments"]:
            u = s.get("until_utc")
            segs.append({"exp_s": float(s["exp_s"]),
                         "until_utc": (datetime.fromisoformat(str(u).rstrip("Z"))
                                       if u else None),
                         "targets": list(s.get("targets") or [])})
        return {"segments": segs}
    if rec.get("changed") and rec.get("single_s"):
        return {"exp_s": float(rec["single_s"])}
    return {}


def with_target_length(rec: dict | None, target: str | None, exp_s: float,
                       base_s: float) -> tuple[dict, float]:
    """Tonight's plan record with one target's Piggy length set to exp_s
    (the night pilot's sub ladder, 2026-10-25): that target's row (every
    row when the target is not in the plan), never above the record's
    applied unguided cap; the segments and single length rebuilt as plan()
    does. (new record, the length that applies)."""
    from photonscript.shared.target_names import target_key
    rec = dict(rec or {})
    cap = rec.get("cap_s")
    s = float(exp_s)
    if cap and s > float(cap) + 0.5:
        s = float(cap)
    rows = [dict(r) for r in rec.get("targets") or [] if isinstance(r, dict)]
    if not rows:
        rows = [{"target": target or "?", "exp_s": s, "source": "ladder",
                 "until_utc": None}]
    k = target_key(target) if target else ""
    hit = [r for r in rows if k and target_key(r.get("target")) == k]
    for r in hit or rows:
        r["exp_s"] = s
        r["source"] = "ladder"
    segs: list[dict] = []
    for r in rows:
        if segs and abs(segs[-1]["exp_s"] - float(r["exp_s"])) < 0.5:
            segs[-1]["until_utc"] = r.get("until_utc")
            segs[-1]["targets"].append(r["target"])
        else:
            segs.append({"exp_s": float(r["exp_s"]), "until_utc": r.get("until_utc"),
                         "targets": [r["target"]]})
    if segs:
        segs[-1]["until_utc"] = None
    lengths = {round(x["exp_s"], 1) for x in segs}
    base = float(rec.get("base_s") or base_s or 120.0)
    rec.update(base_s=base, targets=rows,
               segments=segs if len(lengths) > 1 else [],
               single_s=segs[0]["exp_s"] if len(lengths) == 1 and segs else None,
               changed=True)
    return rec, s


def summary(rec: dict | None) -> str:
    """One line for the log / plan: 'Piggy lengths: M31 120 s (override)
    until 06:10Z, then 450 s'."""
    if not rec or not rec.get("changed"):
        return ""
    if rec.get("segments"):
        parts = [f"{'+'.join(s['targets'])} {s['exp_s']:g} s"
                 + (f" until {s['until_utc'][11:16]}Z" if s.get("until_utc") else "")
                 for s in rec["segments"]]
        return "Piggy lengths: " + ", then ".join(parts)
    srcs = sorted({r["source"] for r in rec.get("targets") or [] if r["source"] != "default"})
    return f"Piggy length: {rec['single_s']:g} s all night ({', '.join(srcs)})"
