"""Optics or tracking: does the elongation scale with the exposure?

2026-10-20 / 2026-10-22 (RC16 at 0.236"/px, unguided): RC16 stars were
elongated by about 3.3 to 3.8" (ecc 0.65 to 0.77) whether the sub was 60 s
or 120 s, frame-check saw the trails "in no common direction", and the
Piggy-600 on the same mount at 60 s stayed round (ecc ~0.28). The sub
doctor still called it tracking and asked for 30 s subs.

A tracking drift smears every star along one line whose length grows with
the exposure (twice the exposure, about twice the trail) and points the
same way across the field and from frame to frame. Optical elongation
(focus, camera tilt, collimation, astigmatism) does not grow with the
exposure and has no common direction. So, per rig:

    exposure scaling   group the subs (tonight and the last few nights) by
                       exposure; when two groups differ by at least
                       EXP_RATIO_MIN (1.8x), the median trail of the longer
                       over the shorter is the trail ratio. Under
                       FLAT_RATIO_MAX (1.3): "flat" (optics); at or over it:
                       "scales" (drift). Too few subs: "unknown".
    steadiness         the same exposure across time: a trail that barely
                       moves (CV under STEADY_CV over STEADY_SPAN_MIN) over
                       hours of hour angle is optics-like (supporting only)
    common axis        stars stretched one way within a frame (axial R) and
                       the frames' axes agreeing (spread): False when either
                       fails
    control rig        the other rig on the same mount, exposing at the same
                       time: if the drift this rig's trail implies would
                       raise the other rig's ecc by CONTROL_ECC_DELTA or more
                       and the other rig is round, the mount is not drifting

    classify(...)      optics when the trail is flat; or, with no proof it
                       scales, when the axis is not common or the control
                       rig is round. Tracking when it scales with a common
                       axis (or when nothing says otherwise). An optics
                       verdict never recommends shorter subs.

Pure functions: no I/O. Used by sub_doctor (the subs log) and frame_check
(NINA's frames).
"""
from __future__ import annotations

import math
import statistics

EXP_RATIO_MIN = 1.8        # exposure groups this far apart test the scaling
FLAT_RATIO_MAX = 1.3       # trail ratio under this: does not scale
MIN_GROUP = 2              # fewest subs in an exposure group (the doctor's
                           # first window is 2 subs)
STEADY_CV = 0.20           # same exposure: trail this steady over ...
STEADY_MIN_N = 6
STEADY_SPAN_MIN = 60.0     # ... at least this many minutes
AXIS_R_MIN = 0.45          # stars stretched one way (axial R) at least this
AXIS_SPREAD_MAX = 25.0     # frames' trail axes agree within this
CONTROL_ECC_DELTA = 0.12   # the drift would raise the other rig's ecc this much

OPTICS_FIX = ("Elongation that does not grow with the exposure is optical, "
              "not tracking: check focus (HFR now vs the AF curve minimum; "
              "re-run AF), camera tilt (per-corner FWHM / ecc: one soft corner "
              "or side = tilt, run frame-check for the corners), and "
              "collimation (center stars elongated with no common direction). "
              "Shorter subs will not help.")


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def trail_arcsec(hfr_px, ecc, scale_arcsec) -> float | None:
    """Trail length (arcsec) of a median star from its HFR and ecc: a
    Gaussian of sigma s smeared over a line of length L has
    sigma_major^2 = s^2 + L^2/12 (sub_doctor.trail_estimate)."""
    h, e, s = _num(hfr_px), _num(ecc), _num(scale_arcsec)
    if not h or e is None or not s or not 0 <= e < 1:
        return None
    q = math.sqrt(1.0 - e * e)
    sig_minor = h / 1.1774 * math.sqrt(q)
    return math.sqrt(12.0) * sig_minor * e / q * s


def _axial_spread(angles) -> float | None:
    a = [x for x in angles if x is not None]
    if len(a) < 2:
        return None
    c = sum(math.cos(math.radians(2 * x)) for x in a)
    s = sum(math.sin(math.radians(2 * x)) for x in a)
    m = (math.degrees(math.atan2(s, c)) / 2.0) % 180.0
    d = [min(abs(x - m) % 180.0, 180.0 - abs(x - m) % 180.0) for x in a]
    return round(max(d), 1)


# --------------------------------------------------------------------------
# the tests
# --------------------------------------------------------------------------

def exposure_scaling(rows: list[dict], *, exp_ratio_min: float = EXP_RATIO_MIN,
                     flat_max: float = FLAT_RATIO_MAX,
                     min_group: int = MIN_GROUP) -> dict:
    """rows: [{exp_s, trail_arcsec, ecc?}]. Compares the median trail of the
    shortest and the longest exposure groups (min_group subs each) that are
    at least exp_ratio_min apart. {verdict scales | flat | unknown, short,
    long, exp_ratio, trail_ratio, groups, why}."""
    groups: dict[float, list[dict]] = {}
    for r in rows or []:
        x, t = _num(r.get("exp_s")), _num(r.get("trail_arcsec"))
        if not x or t is None:
            continue
        groups.setdefault(round(x, 0), []).append(r)
    summ = {}
    for x, rs in sorted(groups.items()):
        summ[x] = {"exp_s": x, "n": len(rs),
                   "trail_arcsec": round(_med([_num(r["trail_arcsec"]) for r in rs]), 2),
                   "ecc": _med([_num(r.get("ecc")) for r in rs])}
    big = [g for g in summ.values() if g["n"] >= min_group]
    out = {"verdict": "unknown", "short": None, "long": None, "exp_ratio": None,
           "trail_ratio": None,
           "groups": [{k: (round(v, 3) if isinstance(v, float) else v)
                       for k, v in g.items()} for g in summ.values()]}
    if len(big) < 2:
        out["why"] = ("one exposure length only" if len(summ) <= 1 else
                      f"fewer than {min_group} subs at a second exposure")
        return out
    lo, hi = big[0], big[-1]
    er = hi["exp_s"] / lo["exp_s"]
    if er < exp_ratio_min:
        out["why"] = (f"exposures {lo['exp_s']:g} s and {hi['exp_s']:g} s differ "
                      f"by only x{er:.2f} (need x{exp_ratio_min:g})")
        return out
    if not lo["trail_arcsec"]:
        out["why"] = "no trail at the short exposure"
        return out
    tr = hi["trail_arcsec"] / lo["trail_arcsec"]
    out.update(short=lo, long=hi, exp_ratio=round(er, 2), trail_ratio=round(tr, 2),
               verdict="flat" if tr < flat_max else "scales")
    out["why"] = (f"{lo['exp_s']:g} s {lo['trail_arcsec']:g}\" (n {lo['n']}) vs "
                  f"{hi['exp_s']:g} s {hi['trail_arcsec']:g}\" (n {hi['n']}): "
                  f"trail x{tr:.2f} for x{er:.2f} exposure")
    return out


def steadiness(rows: list[dict], *, cv_max: float = STEADY_CV,
               min_n: int = STEADY_MIN_N, span_min: float = STEADY_SPAN_MIN) -> dict:
    """The same exposure across time: the largest exposure group with
    min_n subs spanning span_min minutes (rows carry t_min, minutes since
    any epoch). {steady True | False | None, exp_s, n, cv, span_min}."""
    groups: dict[float, list[dict]] = {}
    for r in rows or []:
        x, t, m = _num(r.get("exp_s")), _num(r.get("trail_arcsec")), _num(r.get("t_min"))
        if x and t is not None and m is not None:
            groups.setdefault(round(x, 0), []).append(r)
    best = None
    for x, rs in groups.items():
        if len(rs) < min_n:
            continue
        ts = [_num(r["t_min"]) for r in rs]
        span = max(ts) - min(ts)
        if span < span_min:
            continue
        vals = [_num(r["trail_arcsec"]) for r in rs]
        m = statistics.median(vals)
        cv = statistics.pstdev(vals) / m if m else None
        if best is None or len(rs) > best["n"]:
            best = {"exp_s": x, "n": len(rs), "cv": None if cv is None else round(cv, 3),
                    "span_min": round(span), "trail_arcsec": round(m, 2)}
    if best is None:
        return {"steady": None}
    return {"steady": best["cv"] is not None and best["cv"] <= cv_max, **best}


def axis_consistency(axes, rs=None, *, r_min: float = AXIS_R_MIN,
                     spread_max: float = AXIS_SPREAD_MAX,
                     no_direction: int = 0) -> dict:
    """axes: the frames' elongation axes (deg, None = no common direction in
    that frame); rs: their axial R. no_direction: frames measured as
    elongated but with no common direction (frame-check axis None).
    {common True | False | None, spread_deg, axis_r, why}."""
    spread = _axial_spread(axes or [])
    r_med = _med([_num(r) for r in (rs or [])])
    known = [a for a in (axes or []) if a is not None]
    s_ok = None if spread is None else spread <= spread_max
    r_ok = None if r_med is None else r_med >= r_min
    nd_bad = no_direction > 0 and no_direction * 2 >= no_direction + len(known)
    if nd_bad:
        common, why = False, (f"{no_direction} of {no_direction + len(known)} "
                              "frames trail in no common direction")
    elif s_ok is False:
        common, why = False, f"frame axes spread {spread:g} deg"
    elif r_ok is False:
        common, why = False, f"stars not stretched one way (axial R {r_med:.2f})"
    elif s_ok or r_ok:
        common, why = True, ("one axis" + (f" (spread {spread:g} deg" if spread is not None else " (")
                             + (f", R {r_med:.2f})" if r_med is not None else ")"))
    else:
        common, why = None, "axis unknown"
    return {"common": common, "spread_deg": spread,
            "axis_r": None if r_med is None else round(r_med, 2), "why": why}


def predicted_ecc(hfr_px, ecc, drift_px) -> float | None:
    """The ecc a star of (hfr_px, ecc) would have with drift_px more trail
    along its long axis: sigma_major^2 + L^2/12."""
    h, e, d = _num(hfr_px), _num(ecc), _num(drift_px)
    if not h or e is None or d is None or not 0 <= e < 1:
        return None
    q = math.sqrt(1.0 - e * e)
    minor = h / 1.1774 * math.sqrt(q)
    major2 = (minor / q) ** 2 + d * d / 12.0
    return math.sqrt(max(0.0, 1.0 - minor * minor / major2))


def control(rate_arcsec_min, partners: list[dict], scale_arcsec: float, *,
            delta: float = CONTROL_ECC_DELTA) -> dict:
    """The other rig as a control. partners: its overlapping subs
    [{exp_s, hfr, ecc, trailing}]; rate: the drift ("/min) this rig's trail
    would mean; scale: the other rig's "/px. {verdict round | trails |
    too_small | none, n, n_round, n_trailing, predicted_ecc, observed_ecc,
    why}. round: the drift would show on it (ecc up by `delta` or more)
    and it is not trailing."""
    rate, sc = _num(rate_arcsec_min), _num(scale_arcsec)
    ps = [p for p in partners or [] if p and not p.get("excluded")]
    if not ps or not rate or not sc:
        return {"verdict": "none", "n": len(ps),
                "why": "no overlapping sub on the other rig" if not ps else "no drift rate"}
    n_tr = n_round = 0
    preds, obs = [], []
    for p in ps:
        if p.get("trailing"):
            n_tr += 1
            continue
        x = _num(p.get("exp_s"))
        e = _num(p.get("ecc"))
        pe = predicted_ecc(p.get("hfr"), e, rate * (x or 0) / 60.0 / sc) if x else None
        if pe is None or e is None:
            continue
        preds.append(pe)
        obs.append(e)
        if pe - e >= delta:
            n_round += 1
    out = {"n": len(ps), "n_round": n_round, "n_trailing": n_tr,
           "predicted_ecc": None if not preds else round(_med(preds), 2),
           "observed_ecc": None if not obs else round(_med(obs), 2)}
    if n_tr * 2 >= len(ps) and n_tr:
        out.update(verdict="trails", why=f"the other rig trails too ({n_tr} of {len(ps)})")
    elif n_round * 2 >= len(ps) and n_round:
        out.update(verdict="round", why=(
            f"the other rig is round (ecc {out['observed_ecc']}) where a "
            f"{rate:.1f}\"/min drift would give ecc {out['predicted_ecc']}"))
    else:
        out.update(verdict="too_small", why="the drift would not show on the other rig")
    return out


def classify(scaling: dict | None, axis: dict | None, ctrl: dict | None,
             steady: dict | None = None, rotation: bool = False) -> dict:
    """{verdict optics | tracking, confidence high | medium | low, reasons
    [...], against [...]}. rotation: the stars stretch tangentially (field
    rotation), which has no single axis and still is tracking."""
    sv = (scaling or {}).get("verdict")
    common = (axis or {}).get("common")
    cv = (ctrl or {}).get("verdict")
    reasons, against = [], []
    if sv == "flat":
        reasons.append("trail does not scale with exposure: " + scaling.get("why", ""))
    elif sv == "scales":
        against.append("trail scales with exposure: " + scaling.get("why", ""))
    if common is False and not rotation:
        reasons.append("no common axis: " + axis.get("why", ""))
    elif common:
        against.append("common axis: " + axis.get("why", ""))
    if cv == "round":
        reasons.append("control rig round: " + ctrl.get("why", ""))
    elif cv == "trails":
        against.append("control rig trails: " + ctrl.get("why", ""))
    if steady and steady.get("steady"):
        reasons.append(f"same {steady['exp_s']:g} s trail steady over "
                       f"{steady['span_min']} min (CV {steady['cv']})")
    strong = [r for r in reasons if not r.startswith("same ")]
    if sv == "flat":
        optics = True
    elif sv == "scales":
        # scales: tracking unless the axis is not common
        optics = common is False and not rotation
    else:
        optics = bool(strong) and cv != "trails"
    if not optics:
        conf = "high" if sv == "scales" and common else (
            "low" if strong else "medium")
        return {"verdict": "tracking", "confidence": conf, "reasons": reasons,
                "against": against}
    n = len(strong) + (1 if steady and steady.get("steady") else 0)
    conf = ("high" if sv == "flat" and n >= 2 else
            "medium" if sv == "flat" or cv == "round" or n >= 2 else "low")
    if sv == "scales":
        conf = "low"
    return {"verdict": "optics", "confidence": conf, "reasons": reasons,
            "against": against}
