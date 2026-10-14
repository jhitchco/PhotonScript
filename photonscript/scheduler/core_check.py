"""Target core clipped: per target, filter and sub length, and whether the
HDR short subs cover it (2026-10-13, M31).

Each graded sub carries core_sat_px / core_peak_adu / core_p999 (shared.
core_sat). This module reads them back:

    norm(rec_or_row) -> dict                 a subs-log record, a sub_index
                                             row or an /api/subs row
    summarize(config, rows) -> list[dict]    per rig, filter and length
    coverage(config, rows, project) -> list  per rig and filter: do the long
                                             subs clip the core, and do the
                                             goal's HDR shorts cover it
    suggest_short(config, groups, long_s)    a short length that puts the
                                             core's 99.9th percentile at
                                             about 80% of saturation
    morning_line(config, date, subs, projects)   the morning report line
    night_notes(config, subs, ...)           the sub doctor's report lines
    target_core(config, rows, project)       the target page block
    format_report(...)                       photonscript core-check

A long length clips the core when at least half of its measured subs have
more than qa_core_sat_warn_px (25) core pixels at saturation (another rig:
scaled by the pixel area, core_sat.warn_px). Shorts are
the lengths at most 1/1.5 of the long one (ExposurePlan.is_short_exposure);
"in the Library" counts approved shorts (pending ones are named apart).
Wanted: the goal's hdr_short_count, else core_hdr_min_short (12).
"""

from __future__ import annotations

import math
from statistics import median

TARGET_FRAC = 0.80        # put the core's 99.9th percentile here (x saturation)
SHORT_FRAC = 1.0 / 1.5    # a length at most this x the long one is a short
NICE_S = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 600)
OK, UNMEASURED, COVERED, TOO_FEW, NO_PLAN, SHORT_CLIPS = (
    "ok", "unmeasured", "covered", "too_few", "no_plan", "short_clips")


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _thr(config, rig: str = "rc16") -> int:
    from photonscript.shared.core_sat import warn_px
    return warn_px(config, rig)


def norm(r: dict) -> dict:
    """One sub as {date, target, rig, filter, exp_s, verdict, core_sat_px,
    core_peak_adu, core_p999, sat_adu, offset} from a subs-log record or a
    sub_index (/api/subs) row."""
    m = r.get("metrics")
    if isinstance(m, dict):            # sub_index row
        verdict = r.get("verdict")
        src = m
        sat = m.get("core_sat_adu")
    else:
        from photonscript.scheduler.sub_index import verdict_of
        verdict = verdict_of(r)[0]
        src = r
        sat = r.get("core_sat_adu") or r.get("sat_adu")
    from photonscript.shared.target_names import canonical_target
    return {"date": r.get("date") or "", "target": canonical_target(r.get("target")) or "?",
            "rig": r.get("rig") or "rc16", "filter": r.get("filter") or "?",
            "exp_s": _num(r.get("exp_s")), "verdict": verdict,
            "core_sat_px": _num(src.get("core_sat_px")),
            "core_peak_adu": _num(src.get("core_peak_adu")),
            "core_p999": _num(src.get("core_p999")),
            "sat_adu": _num(sat), "offset": _num(src.get("offset"))}


def _med(xs, nd=0):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    v = median(xs)
    return round(v, nd) if nd else round(v)


def summarize(config, rows) -> list[dict]:
    """Per (rig, filter, exp_s): n, measured, clipped (core_sat_px over the
    threshold), median / max core_sat_px, median peak and p99.9, saturation
    level, approved / pending / rejected. Rows: norm() dicts. The
    threshold is the rig's (core_sat.warn_px)."""
    by: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["exp_s"] is None:
            continue
        by.setdefault((r["rig"], r["filter"], round(r["exp_s"], 1)), []).append(r)
    out = []
    for (rig, flt, exp), rs in sorted(by.items(), key=lambda kv: (kv[0][0] != "rc16",
                                                                  kv[0][0], kv[0][1],
                                                                  -kv[0][2])):
        meas = [r for r in rs if r["core_sat_px"] is not None]
        thr = _thr(config, rig)
        out.append({
            "rig": rig, "filter": flt, "exp_s": exp, "n": len(rs), "limit_px": thr,
            "measured": len(meas),
            "clipped": sum(1 for r in meas if r["core_sat_px"] > thr),
            "sat_px_median": _med([r["core_sat_px"] for r in meas]),
            "sat_px_max": max((int(r["core_sat_px"]) for r in meas), default=None),
            "peak_median": _med([r["core_peak_adu"] for r in meas]),
            "p999_median": _med([r["core_p999"] for r in meas]),
            "sat_adu": _med([r["sat_adu"] for r in meas]),
            "offset": _med([r["offset"] for r in rs]),
            "approved": sum(1 for r in rs if r["verdict"] == "approved"),
            "pending": sum(1 for r in rs if r["verdict"] == "pending"),
            "rejected": sum(1 for r in rs if r["verdict"] == "rejected")})
    return out


def _is_clipped(g: dict) -> bool:
    return bool(g["measured"]) and 2 * g["clipped"] >= g["measured"]


def nice_floor(s: float) -> float:
    """The largest of NICE_S at or below s (at least 1 s)."""
    best = NICE_S[0]
    for v in NICE_S:
        if v <= s + 1e-9:
            best = v
    return float(best)


def suggest_short(config, groups: list[dict], long_s: float) -> tuple[float, str]:
    """(seconds, why) for the short set of one rig + filter. From the
    longest measured length whose core does not clip: scale it so the core
    p99.9 lands at TARGET_FRAC of saturation (the bias offset does not
    scale). Nothing unclipped: core_hdr_short_s (30 s) when only the long
    length was measured, else a third of the shortest clipped length."""
    default = float(getattr(config, "core_hdr_short_s", 30.0) or 30.0)
    cap = long_s * SHORT_FRAC
    meas = [g for g in groups if g["measured"] and g["p999_median"] is not None]
    clean = sorted((g for g in meas if not _is_clipped(g)), key=lambda g: -g["exp_s"])
    for g in clean:
        sat = g["sat_adu"] or float(getattr(config, "qa_saturation_adu", 65000.0))
        ped = g["offset"] if g["offset"] is not None else float(
            getattr(config, "default_offset", 0) or 0)
        sig = g["p999_median"] - ped
        if sig <= 0:
            continue
        t = g["exp_s"] * (TARGET_FRAC * sat - ped) / sig
        s = nice_floor(min(t, cap))
        return s, (f"{g['exp_s']:g} s puts the core p99.9 at "
                   f"{100.0 * g['p999_median'] / sat:.0f}% of saturation; "
                   f"{s:g} s puts it near {100 * TARGET_FRAC:.0f}%")
    clipped = sorted((g["exp_s"] for g in meas if _is_clipped(g)))
    if clipped and clipped[0] < long_s:
        s = nice_floor(clipped[0] / 3.0)
        return s, (f"even {clipped[0]:g} s clips the core: try {s:g} s and "
                   "measure again")
    s = nice_floor(min(default, cap)) if cap >= 1 else 1.0
    return s, (f"no unclipped sub measures the core yet: start at {s:g} s "
               "(core-check refines it once they are graded)")


def _plan_for(project, rig: str, flt: str):
    if project is None:
        return None
    for e in getattr(project, "exposure_plans", None) or []:
        if e.filter_type.value == flt and (getattr(e, "rig", "rc16") or "rc16") == rig:
            return e
    return None


def coverage(config, rows, project=None) -> list[dict]:
    """Per (rig, filter): {rig, filter, target, status, long_s, long
    (summary), clipped, short_s (plan), short_have, short_pending,
    short_need, add_n, suggest_s, suggest_why, message}. Rows: norm()
    dicts of ONE target (every night: the Library counts)."""
    groups = summarize(config, rows)
    by: dict[tuple, list[dict]] = {}
    for g in groups:
        by.setdefault((g["rig"], g["filter"]), []).append(g)
    target = next((r["target"] for r in rows if r["target"] != "?"), "?")
    out = []
    for (rig, flt), gs in by.items():
        lg = max(gs, key=lambda g: g["exp_s"])
        if not lg["measured"]:
            out.append({"rig": rig, "filter": flt, "target": target,
                        "status": UNMEASURED, "message":
                        f"{target} {flt} {lg['exp_s']:g} s: core not measured yet "
                        f"({lg['n']} subs graded before the core check)"})
            continue
        long_s = lg["exp_s"]
        plan = _plan_for(project, rig, flt)
        plan_s = (float(plan.hdr_short_seconds) if plan is not None and plan.hdr_short_seconds
                  and plan.hdr_short_count else None)
        need = (int(plan.hdr_short_count) if plan_s else
                int(getattr(config, "core_hdr_min_short", 12) or 12))
        shorts = [g for g in gs if g["exp_s"] <= long_s * SHORT_FRAC + 1e-9]
        have = sum(g["approved"] for g in shorts)
        pend = sum(g["pending"] for g in shorts)
        s_meas = [g for g in shorts if g["measured"]]
        s_clip = bool(s_meas) and all(_is_clipped(g) for g in s_meas)
        sug, why = suggest_short(config, gs, long_s)
        rec = {"rig": rig, "filter": flt, "target": target, "long_s": long_s,
               "long": lg, "clipped": _is_clipped(lg), "short_s": plan_s,
               "shorts": shorts, "short_have": have, "short_pending": pend,
               "short_need": need, "add_n": max(0, need - have),
               "suggest_s": sug, "suggest_why": why}
        head = f"{target} {flt} {long_s:g} s"
        if not rec["clipped"]:
            pk = lg["peak_median"]
            sat = lg["sat_adu"]
            rec["status"] = OK
            touch = (lg["sat_px_max"] or 0) > 0
            rec["message"] = (f"{head}: core not clipped ({lg['clipped']} of "
                              f"{lg['measured']} subs over the {lg['limit_px']} px limit"
                              + (f", median peak {pk:,} ADU = {100.0 * pk / sat:.0f}% of "
                                 f"saturation" if pk is not None and sat else "") + ")"
                              + (f"; the nucleus touches saturation (up to "
                                 f"{lg['sat_px_max']} px)" if touch else ""))
            out.append(rec)
            continue
        clip = (f"core clipped on {lg['clipped']} of {lg['measured']} subs (median "
                f"{lg['sat_px_median']:,} px)")
        # the goal's short length, else the length the shorts were shot at,
        # else the suggestion; a measured better length rides along
        shot = max(shorts, key=lambda g: g["n"])["exp_s"] if shorts else None
        short_len = plan_s or shot or sug
        tune = ("" if abs(sug - short_len) < 1e-6
                else f"; measured: {why}")
        if s_clip:
            sm = max(s_meas, key=lambda g: g["exp_s"])
            rec["status"] = SHORT_CLIPS
            rec["message"] = (f"{head}: {clip}, and the {sm['exp_s']:g} s shorts clip it "
                              f"too: shorten the short set to {sug:g} s ({why})")
        elif not plan_s and not have and not pend:
            rec["status"] = NO_PLAN
            rec["message"] = (f"{head}: {clip} and no short subs: add {need} x "
                              f"{short_len:g} s (enable the goal's HDR set, hdr "
                              f"{{\"{flt}\": {short_len:g}}}; {why})")
        elif have < need:
            rec["status"] = TOO_FEW
            rec["message"] = (f"{head}: {clip}, {have} of {need} short subs in the Library"
                              + (f" (+{pend} pending review)" if pend else "")
                              + f": add {need - have} x {short_len:g} s"
                              + ("" if plan_s else " (enable the goal's HDR set, hdr "
                                 f"{{\"{flt}\": {short_len:g}}})") + tune)
        else:
            rec["status"] = COVERED
            fill = ("the HDR combine fills the core" if rig == "rc16" else
                    "OSC: integrate stacks every length in one master and there is "
                    "no HDR combine, so the core stays clipped in the stack")
            rec["message"] = f"{head}: {clip}, covered by {have} short subs ({fill}){tune}"
        if rig != "rc16" and rec["status"] == TOO_FEW:
            rec["message"] += (" (OSC: integrate stacks every length in one master, "
                               "there is no HDR combine)")
        out.append(rec)
    return sorted(out, key=lambda r: (r["rig"] != "rc16", r["rig"], r["filter"]))


GAPS = (NO_PLAN, TOO_FEW, SHORT_CLIPS)


def _projects_by_key(projects) -> dict:
    from photonscript.shared.target_names import target_key
    out = {}
    for p in projects or []:
        try:
            out[target_key(p.target.name)] = p
        except Exception:  # noqa: BLE001
            continue
    return out


def _target_rows(config, projects, target: str, night_rows: list[dict]) -> list[dict]:
    """Every night's rows for `target` (the Library counts), else the
    night's own rows when the index cannot be read."""
    from photonscript.shared.target_names import target_key
    try:
        from photonscript.scheduler.sub_index import all_rows
        k = target_key(target)
        rs = [norm(r) for r in all_rows(config, projects or ()) if r["target_key"] == k]
        if rs:
            return rs
    except Exception:  # noqa: BLE001
        pass
    return night_rows


def night_notes(config, subs: list[dict], projects=None, *, library: bool = True,
                only_gaps: bool = True) -> list[dict]:
    """Coverage records for the targets of one night's subs whose long subs
    clipped the core that night (only the gaps unless only_gaps=False).
    library=True counts shorts across every night (sub_index)."""
    from photonscript.shared.qa_rules import is_test_record
    from photonscript.shared.target_names import target_key
    rows = [norm(s) for s in subs or [] if not is_test_record(s)]
    hit = {r["target"] for r in rows if r["target"] != "?"
           and r["core_sat_px"] is not None
           and r["core_sat_px"] > _thr(config, r["rig"])}
    if not hit:
        return []
    goals = _projects_by_key(projects)
    out = []
    for t in sorted(hit):
        mine = [r for r in rows if r["target"] == t]
        rs = _target_rows(config, projects, t, mine) if library else mine
        for c in coverage(config, rs, goals.get(target_key(t))):
            if c.get("status") in GAPS or (not only_gaps and c.get("clipped")):
                out.append(c)
    return out


def morning_line(config, date: str, subs: list[dict], projects=None) -> str | None:
    """'Core clipped: ...' for the morning report (None: nothing to say)."""
    notes = night_notes(config, subs, projects)
    if not notes:
        return None
    return "Core clipped: " + " | ".join(n["message"] for n in notes[:4])


def target_core(config, rows: list[dict], project=None) -> dict:
    """The target page block: {lines, coverage} (rows: sub_index rows)."""
    cov = coverage(config, [norm(r) for r in rows], project)
    lines = [c["message"] for c in cov if c.get("status") != UNMEASURED] or (
        [cov[0]["message"]] if cov else [])
    return {"lines": lines, "coverage": [{k: v for k, v in c.items()
                                          if k not in ("long", "shorts")} for c in cov]}


def format_report(config, target: str, rows: list[dict], project=None,
                  night: str | None = None, coverage_rows: list[dict] | None = None) -> str:
    """photonscript core-check text: per rig, filter and length (`rows`),
    then the coverage line per filter (from `coverage_rows`, default
    `rows`: every night, so the Library's shorts count)."""
    groups = summarize(config, rows)
    out = [f"Core check: {target}" + (f", night {night}" if night else ", every night")
           + " (clipped = more than 'lim' core px at saturation in half the subs)"]
    if not groups:
        out.append("  no subs")
        return "\n".join(out)
    out.append(f"  {'rig':<10}{'filt':<6}{'exp s':>7}{'subs':>6}{'meas':>6}{'clip':>6}"
               f"{'lim':>5}{'med px':>9}{'max px':>9}{'peak':>9}{'p99.9':>9}{'appr':>6}")
    for g in groups:
        def f(v):
            return "-" if v is None else f"{v:,.0f}"
        out.append(f"  {g['rig']:<10}{g['filter']:<6}{g['exp_s']:>7g}{g['n']:>6}"
                   f"{g['measured']:>6}{g['clipped']:>6}{g['limit_px']:>5}"
                   f"{f(g['sat_px_median']):>9}"
                   f"{f(g['sat_px_max']):>9}{f(g['peak_median']):>9}"
                   f"{f(g['p999_median']):>9}{g['approved']:>6}")
    out.append("")
    for c in coverage(config, coverage_rows or rows, project):
        out.append(f"  [{c['status']}] {c['message']}")
    return "\n".join(out)
