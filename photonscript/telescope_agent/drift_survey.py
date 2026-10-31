"""Unguided drift survey (docs/DRIFT_SURVEY.md): how fast the RC16 drifts
when it tracks unguided, measured at N sky positions in a few minutes each.

Why: the RC16 (0.236"/px) drifts at about 1"/min in one steady direction
unguided (2026-10-28, NGC 2403: trail 2.8" at 300 s, 6.1" at 450 s, 10.2" at
600 s; ecc 0.49 / 0.78 / 0.86). The TPoint model behind ProTrack is thin (83
fitted points, sky RMS 22.8"). Long subs need to know the drift rate around
the sky, before and after a dense TPoint run, without spending hours on
long subs.

How, per point: slew (TheSky SlewToAzAlt, open loop), settle, then plate
solve a short frame every --every-s seconds for --window-s while the mount
tracks WITHOUT any slew or sync, and fit a straight line to the solved
J2000 centres (RA x cos Dec and Dec, arcsec) against the exposure
midpoints. The slope is the drift ("/min per axis), its direction the
position angle on the sky. All solves are J2000 (one frame), so precession
does not matter over minutes.

Reused from tpoint-autocal (tpoint_autocal_live.LoopRun, by subclassing):
the preflight (armer DISARMED, auto-arm off, both NINAs idle or
--stop-nina2, roof safe, dark, TheSky answering, the park state; NOT the
TPoint backup: this never touches TPoint), the filter wheel to L, the
camera lent from NINA #1 to TheSky and always handed back, the TheSky
camera setup, --unpark, --track, the Image Link / ASTAP solves and the abort.
The point picker is the same (select_points: both sides, meridian band,
pole and moon skipped, alt >= 30), with a pace of a whole tracking window.

Never syncs, never parks, never writes TPoint (no backup, no import file,
no AutoMap), never changes ProTrack. Tracking is turned on only with
--track (sidereal, as tpoint-autocal does).

Pure parts (fit_drift, smear / sub length, plan, summary, formatting) are
unit tested; the live run goes through the same small adapters as
tpoint-autocal, so the tests drive it with fakes.
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from photonscript.telescope_agent import tpoint_autocal as ta
from photonscript.telescope_agent import tpoint_autocal_live as tal

# --------------------------------------------------------------- defaults

DEFAULT_POINTS = 10
MAX_POINTS = 60
DEFAULT_WINDOW_S = 180.0
DEFAULT_EVERY_S = 60.0
DEFAULT_SETTLE_S = 10.0
DEFAULT_EXPOSURE_S = ta.DEFAULT_EXPOSURE_S       # 10 s
DEFAULT_BINNING = ta.DEFAULT_BINNING             # 1
DEFAULT_FILTER = ta.DEFAULT_FILTER               # L
DEFAULT_SOLVER = "fallback"
DEFAULT_MAX_SMEAR = 2.0                           # arcsec over one sub
DEFAULT_FWHM = 3.5                                # arcsec, a typical RC16 night
MIN_ALT = 30.0
SUB_LENGTHS = (300, 600, 900, 1200)
# sub lengths a recommendation is rounded DOWN to
STANDARD_SUBS = (60, 90, 120, 180, 240, 300, 360, 420, 480, 600, 720, 900,
                 1200, 1800)
# timing estimate per point (the slew, then the last frame's download and
# solve; Image Link took 5..25 s on 2026-10-28)
SLEW_EST_S = 45.0
SOLVE_EST_S = 25.0
MAX_POINT_FAILS = 2           # points in a row with fewer than 2 solves
MIN_GAP_S = 15.0              # every-s must leave this after the exposure
# alt bands of the summary (deg)
ALT_BANDS = ((30.0, 45.0), (45.0, 60.0), (60.0, 90.0))
# a remote job is killed at 1800 s before the camera goes back: --unattended
# runs must be planned well inside it (--max-run-min, local only)
UNATTENDED_MAX_MIN = 28.0

# never in a script this command sends (sky() refuses one; a test checks
# every script the fake TheSky receives too)
NEVER = ((r"\bSync\w*\s*\(", "sync"), (r"\bPark\w*\s*\(", "park"),
         (r"ClosedLoopSlew", "closed loop slew"), (r"AutoMap", "AutoMap"),
         (r"TPoint", "TPoint"), (r"SetDocumentProperty", "document write"),
         (r"ProTrack", "ProTrack"), (r"Unpark", "unpark"))


# --------------------------------------------------------------- the fit

def _wrap_h(dh: float) -> float:
    return ((float(dh) + 12.0) % 24.0) - 12.0


def offsets_arcsec(solves: list[dict]) -> list[tuple[float, float, float]]:
    """(t_min, x, y) per solve: t in minutes from the first solve's
    midpoint, x = dRA x cos(Dec) (+ = east), y = dDec (+ = north), arcsec,
    both from the first solve. solves: {"t" (s, any origin), "ra_h",
    "dec_d"}, sorted here by t."""
    s = sorted(solves, key=lambda r: float(r["t"]))
    if not s:
        return []
    t0, ra0, dec0 = float(s[0]["t"]), float(s[0]["ra_h"]), float(s[0]["dec_d"])
    cosd = math.cos(math.radians(statistics.fmean(float(r["dec_d"]) for r in s)))
    out = []
    for r in s:
        x = _wrap_h(float(r["ra_h"]) - ra0) * 15.0 * 3600.0 * cosd
        y = (float(r["dec_d"]) - dec0) * 3600.0
        out.append(((float(r["t"]) - t0) / 60.0, x, y))
    return out


def _line(ts: list[float], vs: list[float]) -> tuple[float, float, list[float]]:
    """Least-squares v = a + b t: (b, a, residuals)."""
    n = len(ts)
    tm, vm = sum(ts) / n, sum(vs) / n
    sxx = sum((t - tm) ** 2 for t in ts)
    b = sum((t - tm) * (v - vm) for t, v in zip(ts, vs)) / sxx if sxx > 0 else 0.0
    a = vm - b * tm
    return b, a, [v - (a + b * t) for t, v in zip(ts, vs)]


def position_angle(ra_rate: float, dec_rate: float) -> float | None:
    """The drift's direction on the sky, degrees from north through east
    (0 = north, 90 = east). None for no drift at all."""
    if ra_rate == 0 and dec_rate == 0:
        return None
    return math.degrees(math.atan2(ra_rate, dec_rate)) % 360.0


def fit_drift(solves: list[dict]) -> dict | None:
    """The drift from the solved centres of one point. solves: {"t" (the
    exposure midpoint, seconds, any origin), "ra_h", "dec_d"} (J2000).
    None with fewer than 2 solves or no time span.

    {"n", "span_s", "ra_rate" ("/min, RA x cos Dec, + = east), "dec_rate"
    ("/min, + = north), "rate" (total "/min), "pa_deg" (north through east),
    "rms_arcsec" (the 2-D residual about the two lines; 0 with 2 solves),
    "ra_rate_err", "dec_rate_err" (1-sigma "/min, None under 3 solves)}."""
    pts = offsets_arcsec([s for s in solves if s.get("ra_h") is not None
                          and s.get("dec_d") is not None and s.get("t") is not None])
    if len(pts) < 2:
        return None
    ts = [p[0] for p in pts]
    span = (max(ts) - min(ts)) * 60.0
    if span <= 0:
        return None
    bx, _ax, rx = _line(ts, [p[1] for p in pts])
    by, _ay, ry = _line(ts, [p[2] for p in pts])
    n = len(pts)
    ss = sum(a * a + b * b for a, b in zip(rx, ry))
    rms = math.sqrt(ss / n)
    ex = ey = None
    if n >= 3:
        tm = sum(ts) / n
        sxx = sum((t - tm) ** 2 for t in ts)
        ex = math.sqrt(sum(a * a for a in rx) / (n - 2) / sxx)
        ey = math.sqrt(sum(b * b for b in ry) / (n - 2) / sxx)
    rate = math.hypot(bx, by)
    pa = position_angle(bx, by)
    return {"n": n, "span_s": round(span, 1), "ra_rate": round(bx, 3),
            "dec_rate": round(by, 3), "rate": round(rate, 3),
            "pa_deg": None if pa is None else round(pa, 1),
            "rms_arcsec": round(rms, 3),
            "ra_rate_err": None if ex is None else round(ex, 3),
            "dec_rate_err": None if ey is None else round(ey, 3)}


# --------------------------------------------------------------- smear

def smear_arcsec(rate_per_min: float, sub_s: float) -> float:
    """The trail a sub of sub_s seconds gets at rate_per_min ("/min)."""
    return abs(float(rate_per_min)) * float(sub_s) / 60.0


def predicted_ecc(smear: float, fwhm: float) -> float:
    """Star eccentricity for a Gaussian star of FWHM fwhm smeared by a line
    of length smear (both arcsec): second moments s^2 and s^2 + L^2 / 12,
    ecc = sqrt(1 - minor^2 / major^2). Checked against the 2026-10-28 NGC
    2403 subs at ~3.5": 2.8" -> 0.48 (measured 0.49), 6.1" -> 0.76 (0.78),
    10.2" -> 0.89 (0.86)."""
    s2 = (float(fwhm) / 2.3548) ** 2
    l2 = float(smear) ** 2 / 12.0
    if s2 + l2 <= 0:
        return 0.0
    return math.sqrt(l2 / (s2 + l2))


def smear_for_ecc(ecc: float, fwhm: float) -> float:
    """The smear (arcsec) that gives `ecc` at `fwhm` (predicted_ecc's
    inverse)."""
    e2 = min(max(float(ecc), 0.0), 0.999) ** 2
    s2 = (float(fwhm) / 2.3548) ** 2
    return math.sqrt(12.0 * s2 * e2 / (1.0 - e2))


def max_sub_s(rate_per_min: float, max_smear: float) -> float | None:
    """The longest sub (s) that keeps the smear at or under max_smear.
    None = no measurable drift (no limit)."""
    r = abs(float(rate_per_min))
    if r <= 1e-6:
        return None
    return float(max_smear) / r * 60.0


def standard_sub(seconds: float | None) -> int | None:
    """Round a sub length DOWN to the usual lengths (STANDARD_SUBS); the
    longest one for no limit, None when under the shortest."""
    if seconds is None:
        return STANDARD_SUBS[-1]
    ok = [s for s in STANDARD_SUBS if s <= seconds + 1e-6]
    return ok[-1] if ok else None


def point_smear(fit: dict, max_smear: float, fwhm: float,
                subs=SUB_LENGTHS) -> dict:
    """Per point: smear and predicted ecc for each sub length, the longest
    sub under max_smear and its standard length."""
    rate = float(fit["rate"])
    m = max_sub_s(rate, max_smear)
    return {"smear": {int(s): round(smear_arcsec(rate, s), 2) for s in subs},
            "ecc": {int(s): round(predicted_ecc(smear_arcsec(rate, s), fwhm), 2)
                    for s in subs},
            "max_sub_s": None if m is None else round(m),
            "rec_sub_s": standard_sub(m)}


# --------------------------------------------------------------- the plan

def frame_offsets(window_s: float, every_s: float) -> list[float]:
    """Seconds after the first frame at which each frame starts: 0, every,
    2 x every ... and the window's end (180 / 60 -> 0, 60, 120, 180)."""
    w, e = float(window_s), float(every_s)
    if w <= 0 or e <= 0:
        raise ValueError("window and every must be positive")
    out, t = [], 0.0
    while t < w - 1e-6:
        out.append(round(t, 3))
        t += e
    out.append(round(w, 3))
    return out


def per_point_s(window_s: float, every_s: float, exposure_s: float,
                settle_s: float) -> float:
    """About how long one point takes: the slew, the settle, the window,
    then the last frame's exposure and solve."""
    return SLEW_EST_S + float(settle_s) + float(window_s) + float(exposure_s) + SOLVE_EST_S


def validate(window_s: float, every_s: float, exposure_s: float,
             settle_s: float, points: int) -> list[str]:
    """Option problems (empty = fine)."""
    bad = []
    if not 1 <= int(points) <= MAX_POINTS:
        bad.append(f"--points must be 1..{MAX_POINTS}")
    if not 30 <= float(window_s) <= 1800:
        bad.append("--window-s must be 30..1800")
    if not 10 <= float(every_s) <= float(window_s):
        bad.append("--every-s must be 10..--window-s")
    if not 0.5 <= float(exposure_s) <= 60:
        bad.append("--exposure-s must be 0.5..60")
    elif float(every_s) < float(exposure_s) + MIN_GAP_S:
        bad.append(f"--every-s must leave {MIN_GAP_S:g} s after the exposure for the "
                   f"download and the solve (>= {float(exposure_s) + MIN_GAP_S:g})")
    if not 0 <= float(settle_s) <= 300:
        bad.append("--settle-s must be 0..300")
    return bad


def build_plan(points: list[dict], window_s: float = DEFAULT_WINDOW_S,
               every_s: float = DEFAULT_EVERY_S,
               exposure_s: float = DEFAULT_EXPOSURE_S,
               settle_s: float = DEFAULT_SETTLE_S, binning: int = DEFAULT_BINNING,
               scale_per_bin1: float = 0.236, solver: str = DEFAULT_SOLVER,
               filter_name: str | None = DEFAULT_FILTER, track: bool = False,
               stop_nina2: bool = False, unpark: bool = False,
               max_smear: float = DEFAULT_MAX_SMEAR, fwhm: float = DEFAULT_FWHM,
               label: str = "") -> dict:
    """The run as data: per point timing, the frames, the scripts. Pure."""
    offs = frame_offsets(window_s, every_s)
    pp = per_point_s(window_s, every_s, exposure_s, settle_s)
    flips = sum(1 for a, b in zip(points, points[1:]) if a.get("side") != b.get("side"))
    est = len(points) * pp + flips * tal.FLIP_S
    return {"label": label, "points": points, "n_points": len(points),
            "frames": offs, "frames_per_point": len(offs),
            "per_point_s": round(pp), "flips": flips, "est_s": round(est),
            "params": {"window_s": float(window_s), "every_s": float(every_s),
                       "exposure_s": float(exposure_s), "settle_s": float(settle_s),
                       "binning": int(binning),
                       "image_scale": round(float(scale_per_bin1) * int(binning), 4),
                       "solver": solver, "filter": filter_name, "track": track,
                       "stop_nina2": stop_nina2, "unpark": unpark or stop_nina2,
                       "max_smear_arcsec": float(max_smear),
                       "fwhm_arcsec": float(fwhm),
                       "ecc_at_max_smear": round(predicted_ecc(max_smear, fwhm), 2)}}


def format_plan(plan: dict, pre: dict | None = None, model: dict | None = None,
                show_scripts: bool = True, min_alt: float = MIN_ALT) -> str:
    """The dry-run text: what runs, how long, the scripts. Sends nothing."""
    p = plan["params"]
    offs = ", ".join(f"+{o:g}" for o in plan["frames"])
    out = [f"Drift survey DRY RUN (nothing sent){': ' + plan['label'] if plan['label'] else ''}",
           f"  {plan['n_points']} point(s); per point: slew, settle {p['settle_s']:g} s"
           + (", tracking on (--track)" if p["track"] else "")
           + f", then {plan['frames_per_point']} frames of {p['exposure_s']:g} s at "
           f"{offs} s (window {p['window_s']:g} s, every {p['every_s']:g} s), bin "
           f"{p['binning']} at {p['image_scale']}\"/px, solver {p['solver']} "
           f"({ta.SOLVER_TEXT.get(p['solver'], p['solver'])})",
           f"  timing: ~{plan['per_point_s']} s a point (slew ~{SLEW_EST_S:g} + settle "
           f"+ window + exposure + solve ~{SOLVE_EST_S:g}), {plan['flips']} pier "
           f"flip(s) x {tal.FLIP_S:g} s: about {round(plan['est_s'] / 60)} min",
           f"  judging: smear limit {p['max_smear_arcsec']:g}\" (predicted ecc "
           f"{p['ecc_at_max_smear']:.2f} at FWHM {p['fwhm_arcsec']:g}\"); sub lengths "
           + ", ".join(f"{s} s" for s in SUB_LENGTHS),
           "  filter: " + (f"NINA #1's wheel to {p['filter']} before the camera is lent"
                           if p["filter"] else "left as it is (--filter none)")]
    if p["stop_nina2"]:
        out.append("  --stop-nina2: a running NINA #2 is stopped when it is the only "
                   "blocker (implies --unpark)")
    if p["unpark"]:
        out.append("  --unpark: a parked mount is unparked by NINA #1 after the preflight")
    out.append("  " + format_model(model))
    if pre is not None:
        out.append(f"Preflight: {'GO' if pre['go'] else 'NO GO'}")
        for c in pre["checks"]:
            out.append(f"  [{c['status'].upper():7}] {c['name']}: {c['detail']}")
    out.append("Steps:")
    steps = [
        ("preflight", "armer DISARMED, auto-arm off, both NINAs idle (or "
         "--stop-nina2), roof safe, dark, TheSky answering, the park state; no "
         "TPoint backup (TPoint is never touched)"),
        ("lend", "NINA #1's filter wheel to the filter, NINA #1 lets the camera "
         "go, TheSky connects it", ta.camera_connect_script()),
        ("setup", "TheSky camera exposure / bin / light / autosave / async",
         ta.camera_setup_script(p["exposure_s"], p["binning"])),
    ]
    if plan["points"]:
        q = plan["points"][0]
        steps.append(("point", f"e.g. point 1: slew to alt {q['alt']} az {q['az']} "
                      "(open loop, no sync)",
                      ta.slew_altaz_script(q["alt"], q["az"], "drift survey 1", min_alt)))
    steps += [("point", "poll the slew", ta.slew_poll_script()),
              ("point", f"settle {p['settle_s']:g} s")]
    if p["track"]:
        steps.append(("point", "tracking on (sidereal; ProTrack as configured)",
                      ta.tracking_on_script()))
    steps += [("point", "read the mount (read only: tracking, the solve hint)",
               ta.mount_snapshot_script()),
              ("frame", "expose (each frame, no slew or sync in between)",
               ta.expose_script()),
              ("frame", "poll the exposure", ta.expose_poll_script()),
              ("frame", f"solve the saved frame ({ta.SOLVER_TEXT.get(p['solver'])})"),
              ("point", "fit RA x cos(Dec) and Dec against the exposure midpoints: "
               "drift \"/min, direction, residual"),
              ("end", "TheSky disconnects its camera, NINA #1 connects it again "
               "(always, also after Ctrl+C)", ta.camera_disconnect_script())]
    for i, s in enumerate(steps, 1):
        out.append(f"  {i:2}. ({s[0]}) {s[1]}")
        if show_scripts and len(s) > 2 and s[2]:
            out.append(f"        JS: {s[2]}")
    out.append("Never syncs, never parks, never writes TPoint, never changes ProTrack.")
    return "\n".join(out)


# --------------------------------------------------------------- the model

def model_info(config, stats_fn=None) -> dict:
    """The TPoint model in force, from TPoint's own files (read only):
    {"model_date", "points", "model_points", "sky_rms_arcsec",
    "model_file", "active_model", "note"}. Never raises."""
    try:
        if stats_fn is None:
            from photonscript.scheduler import tpoint_files as tf
            stats_fn = tf.stats
        st = stats_fn(config) or {}
    except Exception as e:  # noqa: BLE001
        return {"note": f"TPoint files not read ({type(e).__name__}: {e})"}
    if not st.get("ok"):
        return {"note": st.get("note") or "no TPoint files found"}
    rms = st.get("active_sky_rms_arcsec")
    if rms is None:
        rms = st.get("sky_rms_arcsec")
    return {"model_date": st.get("model_date"), "points": st.get("points"),
            "model_points": st.get("model_points"), "sky_rms_arcsec": rms,
            "model_file": st.get("active_model_file") or st.get("model_file"),
            "active_model": st.get("active_model"), "note": st.get("note")}


def format_model(m: dict | None) -> str:
    """One line for the header: date, points, sky RMS (before / after runs
    are compared on it)."""
    m = m or {}
    if m.get("model_date") is None and m.get("points") is None:
        return f"TPoint model: unknown ({m.get('note') or 'not read'})"
    pts = m.get("points")
    out = f"TPoint model: {m.get('model_date') or '?'}, {pts if pts is not None else '?'} points"
    if m.get("model_points") not in (None, pts):
        out += f" ({m['model_points']} in the fit)"
    rms = m.get("sky_rms_arcsec")
    out += f", sky RMS {rms:.1f}\"" if isinstance(rms, (int, float)) else ", sky RMS ?"
    if m.get("model_file"):
        out += f" ({m['model_file']})"
    return out


# --------------------------------------------------------------- summary

def region_of(alt: float, side: str) -> str:
    for lo, hi in ALT_BANDS:
        if lo <= float(alt) < hi or (hi == 90.0 and float(alt) >= lo):
            return f"{side} alt {lo:g}-{hi:g}"
    return f"{side} alt <{ALT_BANDS[0][0]:g}"


def _group(results: list[dict], key) -> dict:
    out: dict = {}
    for r in results:
        out.setdefault(key(r), []).append(r)
    return out


def _stats(rs: list[dict], max_smear: float) -> dict:
    rates = [float(r["fit"]["rate"]) for r in rs]
    med = statistics.median(rates)
    worst = max(rates)
    return {"n": len(rs), "median_rate": round(med, 3), "worst_rate": round(worst, 3),
            "median_ra_rate": round(statistics.median(
                float(r["fit"]["ra_rate"]) for r in rs), 3),
            "median_dec_rate": round(statistics.median(
                float(r["fit"]["dec_rate"]) for r in rs), 3),
            "rec_sub_s_median": standard_sub(max_sub_s(med, max_smear)),
            "rec_sub_s_worst": standard_sub(max_sub_s(worst, max_smear))}


def summarize(results: list[dict], max_smear: float = DEFAULT_MAX_SMEAR,
              fwhm: float = DEFAULT_FWHM) -> dict:
    """Over the measured points (each {"side", "alt", "fit", ...} with a
    fit): by side, by region (alt band x side), the median and worst rate,
    the median drift direction, and the recommended unguided sub: the
    longest standard length whose smear stays under max_smear at the WORST
    measured point (everywhere surveyed), and at the median (typical)."""
    ok = [r for r in results if r.get("fit")]
    if not ok:
        return {"measured": 0, "max_smear_arcsec": max_smear, "fwhm_arcsec": fwhm}
    allst = _stats(ok, max_smear)
    pas = [r["fit"]["pa_deg"] for r in ok if r["fit"].get("pa_deg") is not None]
    # the mean direction (circular), with how tightly the points agree
    if pas:
        sx = sum(math.sin(math.radians(a)) for a in pas) / len(pas)
        cx = sum(math.cos(math.radians(a)) for a in pas) / len(pas)
        mean_pa = round(math.degrees(math.atan2(sx, cx)) % 360.0, 1)
        agree = round(math.hypot(sx, cx), 2)
    else:
        mean_pa = agree = None
    rec = allst["rec_sub_s_worst"]
    worst_pt = max(ok, key=lambda r: float(r["fit"]["rate"]))
    return {"measured": len(ok), "max_smear_arcsec": max_smear, "fwhm_arcsec": fwhm,
            "ecc_at_max_smear": round(predicted_ecc(max_smear, fwhm), 2),
            **allst,
            "worst_point": worst_pt.get("point"),
            "mean_pa_deg": mean_pa, "pa_agreement": agree,
            "recommended_sub_s": rec,
            "typical_sub_s": allst["rec_sub_s_median"],
            "smear_at_worst": {int(s): round(smear_arcsec(allst["worst_rate"], s), 2)
                               for s in SUB_LENGTHS},
            "smear_at_median": {int(s): round(smear_arcsec(allst["median_rate"], s), 2)
                                for s in SUB_LENGTHS},
            "by_side": {k: _stats(v, max_smear) for k, v in
                        sorted(_group(ok, lambda r: r["side"]).items())},
            "by_region": {k: _stats(v, max_smear) for k, v in
                          sorted(_group(ok, lambda r: region_of(r["alt"], r["side"])).items())}}


def _sub(s) -> str:
    return "none" if s is None else f"{s} s"


def format_summary(summ: dict, model: dict | None = None, label: str = "") -> list[str]:
    lines = [f"Drift survey summary{' (' + label + ')' if label else ''}",
             "  " + format_model(model)]
    if not summ.get("measured"):
        lines.append("  no point was measured (fewer than 2 solves everywhere)")
        return lines
    lines.append(f"  points measured {summ['measured']}; rate median "
                 f"{summ['median_rate']:.2f}\"/min, worst {summ['worst_rate']:.2f}\"/min "
                 f"(point {summ['worst_point']}); median dRA*cos(Dec) "
                 f"{summ['median_ra_rate']:+.2f}, dDec {summ['median_dec_rate']:+.2f} "
                 "\"/min")
    if summ.get("mean_pa_deg") is not None:
        lines.append(f"  drift direction: mean PA {summ['mean_pa_deg']:.0f} deg (N through "
                     f"E), agreement {summ['pa_agreement']:.2f} (1 = all the same way)")
    lines.append(f"  smear at the worst rate: " + ", ".join(
        f"{s} s {v:.1f}\"" for s, v in summ["smear_at_worst"].items())
        + "; at the median: " + ", ".join(
        f"{s} s {v:.1f}\"" for s, v in summ["smear_at_median"].items()))
    lines.append(f"  RECOMMENDED unguided sub: {_sub(summ['recommended_sub_s'])} "
                 f"everywhere surveyed (worst point), {_sub(summ['typical_sub_s'])} "
                 f"typical (median); smear limit {summ['max_smear_arcsec']:g}\" = "
                 f"predicted ecc {summ['ecc_at_max_smear']:.2f} at FWHM "
                 f"{summ['fwhm_arcsec']:g}\"")
    lines.append("  by side / region:  n  median \"/min  worst \"/min  sub (median / worst)")
    for title, grp in (("side", summ["by_side"]), ("region", summ["by_region"])):
        for k, v in grp.items():
            lines.append(f"    {k:<22} {v['n']:2}  {v['median_rate']:6.2f}  "
                         f"{v['worst_rate']:10.2f}    {_sub(v['rec_sub_s_median'])} / "
                         f"{_sub(v['rec_sub_s_worst'])}")
    return lines


def format_point(r: dict) -> str:
    f = r.get("fit")
    head = (f"  point {r['point']} ({r['side']}, alt {float(r['alt']):.0f} az "
            f"{float(r['az']):.0f}): ")
    if not f:
        return head + f"NOT measured ({r.get('note') or 'fewer than 2 solves'})"
    sm = r.get("smear") or {}
    pa = "-" if f.get("pa_deg") is None else f"{f['pa_deg']:.0f}"
    return (head + f"{f['rate']:.2f}\"/min (RA*cos(Dec) {f['ra_rate']:+.2f}, Dec "
            f"{f['dec_rate']:+.2f}, PA {pa} deg), resid {f['rms_arcsec']:.2f}\" over "
            f"{f['n']} solves / {f['span_s']:.0f} s; smear "
            + ", ".join(f"{s} s {v:.1f}\"" for s, v in (sm.get("smear") or {}).items())
            + f"; longest sub {_sub(sm.get('rec_sub_s'))}")


# --------------------------------------------------------------- files

SOLVE_FIELDS = ["utc", "label", "point", "of", "side", "alt", "az", "ha_h", "dec_deg",
                "frame", "t_offset_s", "exp_start_utc", "mid_utc", "t_mid_s", "image",
                "solver", "solved_by", "solved", "error_text", "imagelink_err",
                "imagelink_code", "astap_err", "image_scale", "image_stars",
                "solution_rms", "binning_used", "ra_j2000_h", "dec_j2000_d",
                "dx_arcsec", "dy_arcsec", "filter", "tracking", "unpark", "note"]
FIT_FIELDS = ["label", "point", "side", "alt", "az", "ha_h", "dec_deg", "region",
              "n_solves", "span_s", "ra_rate", "dec_rate", "rate", "pa_deg",
              "rms_arcsec", "ra_rate_err", "dec_rate_err",
              *[f"smear_{s}" for s in SUB_LENGTHS], *[f"ecc_{s}" for s in SUB_LENGTHS],
              "max_sub_s", "rec_sub_s", "note"]


def safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(label or "")).strip("-")[:30]


def run_stem(config, now: datetime, label: str = "") -> str:
    """<data_dir>/runs/<night>_drift_survey_<UTC HHMM>Z[_<label>] (no
    extension): the solve CSV, the fit CSV (_fits) and the JSON share it."""
    try:
        from photonscript.shared.localtime import night_base_date
        night = night_base_date(config, now).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        night = now.strftime("%Y-%m-%d")
    lab = safe_label(label)
    name = f"{night}_drift_survey_{now:%H%M}Z" + (f"_{lab}" if lab else "")
    return str(Path(getattr(config, "data_dir", ".")) / "runs" / name)


# --------------------------------------------------------------- the run

@dataclass
class DriftOpts:
    window_s: float = DEFAULT_WINDOW_S
    every_s: float = DEFAULT_EVERY_S
    settle_s: float = DEFAULT_SETTLE_S
    max_smear: float = DEFAULT_MAX_SMEAR
    fwhm: float = DEFAULT_FWHM
    label: str = ""
    stem: str | None = None              # files: <stem>.csv, <stem>_fits.csv, <stem>.json
    unattended: bool = False
    max_run_s: float | None = None       # stop starting points after this
    model: Callable[[], dict] | None = None
    extra: dict = field(default_factory=dict)


class DriftRun(tal.LoopRun):
    """tpoint-autocal's LoopRun with its own per-point measurement: the
    preflight (minus the TPoint backup), the filter / camera lend / setup /
    unpark / abort / hand-back are LoopRun's."""

    NEEDS_BACKUP = False

    def __init__(self, config, points: list[dict], deps: tal.Deps, opts: tal.Opts,
                 dopts: DriftOpts):
        super().__init__(config, points, deps, opts)
        self.do = dopts
        self.results: list[dict] = []       # one per point
        self.solve_rows: list[dict] = []
        self.model: dict = {}
        self.offsets = frame_offsets(dopts.window_s, dopts.every_s)
        self.started_utc: datetime | None = None
        self.last_snapshot_raw: dict = {}
        self.csv_file = (dopts.stem + ".csv") if dopts.stem else None
        self.fit_file = (dopts.stem + "_fits.csv") if dopts.stem else None
        self.json_file = (dopts.stem + ".json") if dopts.stem else None

    # -- the scripts sent never sync, park or write TPoint (a test checks too)
    def sky(self, name: str, js: str) -> dict:
        for pat, what in NEVER:
            if re.search(pat, js):
                raise tal.StopRun(f"refusing to send a script with {what} ({name})")
        return super().sky(name, js)

    # -- one frame
    def _frame(self, i: int, k: int, q: dict, snap: dict) -> dict:
        row = {"label": self.do.label, "point": i, "of": len(self.points),
               "side": q["side"], "alt": q["alt"], "az": q["az"],
               "ha_h": q.get("ha_h"), "dec_deg": q.get("dec_deg"), "frame": k + 1,
               "t_offset_s": self.offsets[k], "filter": self.filter_used,
               "tracking": snap.get("tracking")}
        if i == 1 and k == 0 and self.unpark_log:
            row["unpark"] = self.unpark_log
        start = self.d.now()
        self.busy = True
        r = self.sky("expose", ta.expose_script())
        if r.get("err"):
            self.busy = False
            raise tal.StopRun(f"TheSky could not start the exposure: {r['err']}")
        p = self._poll("expose_poll", ta.expose_poll_script(), "complete",
                       self.exposure_s + tal.EXPOSE_EXTRA_S)
        self.busy = False
        if p is None:
            raise tal.StopRun(f"the exposure at point {i} frame {k + 1} did not finish")
        mid = start + timedelta(seconds=self.exposure_s / 2.0)
        row.update(utc=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   exp_start_utc=start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                   mid_utc=mid.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                   image=p.get("last_image"), solver=self.o.solver, solved=False,
                   solved_by="")
        row["_mid"] = mid
        path = str(row.get("image") or "")
        sol = None
        if not path:
            row["error_text"] = "TheSky reported no saved frame"
        else:
            h: dict = {}
            if self.d.fits_header is not None:
                try:
                    h = self.d.fits_header(path) or {}
                except Exception as e:  # noqa: BLE001
                    self.v(f"FITS header read failed ({e})")
            scale = self._scale(h, row)
            if self.o.solver in ("imagelink", "fallback"):
                sol = self._imagelink(path, scale, row)
                if sol:
                    row["solved_by"] = "imagelink"
            if sol is None and self.o.solver in ("astap", "fallback"):
                hint = snap
                if self.solve_rows:
                    last = next((x for x in reversed(self.solve_rows)
                                 if x.get("point") == i and x.get("solved")), None)
                    if last:
                        hint = {"ra_h": last["ra_j2000_h"], "dec_d": last["dec_j2000_d"]}
                sol = self._astap(path, scale, hint, row)
                if sol:
                    row["solved_by"] = "astap"
        if sol is None:
            why = [x for x in (
                row.get("error_text"),
                row.get("imagelink_err") and f"ImageLink: {row['imagelink_err']}",
                row.get("astap_err") and f"ASTAP: {row['astap_err']}") if x]
            row["error_text"] = "; ".join(why)
        else:
            row.update(solved=True, ra_j2000_h=round(sol[0], 8),
                       dec_j2000_d=round(sol[1], 7))
        return row

    # -- one point
    def measure(self, i: int, q: dict) -> dict:
        n = len(self.points)
        res = {"point": i, "side": q["side"], "alt": q["alt"], "az": q["az"],
               "ha_h": q.get("ha_h"), "dec_deg": q.get("dec_deg"),
               "region": region_of(q["alt"], q["side"]), "fit": None,
               "smear": None, "note": ""}
        self.say(f"Point {i}/{n} ({q['side']} side, alt {q['alt']:.1f} az "
                 f"{q['az']:.1f}): slewing.")
        t = self.d.mono()
        self.busy = True
        r = self.sky("slew", ta.slew_altaz_script(q["alt"], q["az"], f"drift survey {i}",
                                                  self.o.min_alt))
        if r.get("err"):
            why = f"TheSky refused the slew to point {i}: {r['err']}"
            if ta.is_parked_error(r["err"]):
                why += ("; the mount still reads parked although --unpark ran"
                        if self.o.unpark else "; " + ta.UNPARK_HINT)
            raise tal.StopRun(why)
        if self._poll("slew_poll", ta.slew_poll_script(), "slew_complete",
                      self.o.slew_timeout_s) is None:
            raise tal.StopRun(f"slew to point {i} did not finish in "
                              f"{self.o.slew_timeout_s:.0f} s")
        self.busy = False
        slew_s = self.d.mono() - t
        self.d.sleep(self.do.settle_s)
        if self.o.track:
            tr = self.sky("tracking_on", ta.tracking_on_script())
            if str(tr.get("tracking")) not in ("1", "true", "True"):
                raise tal.StopRun(f"tracking did not turn on ({tr})")
        snap = self._snapshot({})
        trk = None
        raw = (self.last_snapshot_raw or {}).get("tracking")
        if raw is not None:
            trk = tal.truthy(raw)
            snap["tracking"] = trk
            if not trk:
                raise tal.StopRun("the mount reads NOT tracking after the slew: re-run "
                                  "with --track (sidereal; never syncs or parks)")
        self.say(f"  slewed in {slew_s:.0f} s, settled {self.do.settle_s:g} s"
                 + ("" if trk else " (tracking state unread)")
                 + f"; {len(self.offsets)} frames over {self.do.window_s:g} s, no slew "
                 "or sync in between.")
        start = self.d.mono()
        for k, off in enumerate(self.offsets):
            if k > 0:
                self.recheck()
            wait = start + off - self.d.mono()
            if wait > 0:
                self.d.sleep(wait)
            row = self._frame(i, k, q, snap)
            self.solve_rows.append(row)
            if row.get("solved"):
                self.v(f"frame {k + 1}: RA {row['ra_j2000_h']:.6f} h Dec "
                       f"{row['dec_j2000_d']:+.5f} ({row['solved_by']})")
            else:
                self.say(f"  frame {k + 1}: NOT solved ({row.get('error_text') or '?'})")
        mine = [x for x in self.solve_rows if x["point"] == i and x.get("solved")]
        t0 = mine[0]["_mid"] if mine else None
        solves = [{"t": (x["_mid"] - t0).total_seconds(), "ra_h": x["ra_j2000_h"],
                   "dec_d": x["dec_j2000_d"]} for x in mine]
        fit = fit_drift(solves)
        if mine:
            offs = offsets_arcsec(solves)
            for x, (_tm, dx, dy) in zip(mine, offs):
                x.update(t_mid_s=round((x["_mid"] - t0).total_seconds(), 2),
                         dx_arcsec=round(dx, 3), dy_arcsec=round(dy, 3))
        self._flush_solves()
        if fit is None:
            res["note"] = f"{len(mine)} of {len(self.offsets)} frames solved"
        else:
            res["fit"] = fit
            res["smear"] = point_smear(fit, self.do.max_smear, self.do.fwhm)
            if len(mine) < len(self.offsets):
                res["note"] = f"{len(mine)} of {len(self.offsets)} frames solved"
        self.say(format_point(res))
        return res

    # -- the loop
    def loop(self) -> None:
        fails = 0
        n = len(self.points)
        pp = per_point_s(self.do.window_s, self.do.every_s, self.exposure_s,
                         self.do.settle_s)
        for i, q in enumerate(self.points, 1):
            if (self.do.max_run_s is not None and i > 1
                    and self.d.mono() - self.t0 + pp > self.do.max_run_s):
                raise tal.StopRun(f"time budget: another point would end past "
                                  f"{self.do.max_run_s / 60:.0f} min (--max-run-min)")
            self.recheck()
            res = self.measure(i, q)
            self.results.append(res)
            self._write_fit(res)
            self._write_json("running")
            fails = 0 if res["fit"] else fails + 1
            if fails >= MAX_POINT_FAILS:
                raise tal.StopRun(f"{MAX_POINT_FAILS} points in a row with fewer than 2 "
                                  "solves (clouds, focus, or the solver)")
            if i < n:
                left = (n - i) * pp
                fin = self.d.now() + timedelta(seconds=left)
                self.say(f"  done {i}/{n}; about {tal._fmt_min(left)} left (finish "
                         f"~{tal._local(self.cfg, fin)} local).")

    # -- the mount read keeps the raw reply (tracking)
    def _snapshot(self, row: dict) -> dict:
        utc = self.d.now()
        try:
            m = self.sky("mount_snapshot", ta.mount_snapshot_script())
        except tal.StopRun:
            raise
        except Exception as e:  # noqa: BLE001
            self.say(f"  mount read failed ({e})")
            m = {}
        self.last_snapshot_raw = m
        return {"utc": utc, "ra_h": tal._f(m.get("ra_h")), "dec_d": tal._f(m.get("dec_d")),
                "alt": tal._f(m.get("alt")), "az": tal._f(m.get("az")),
                "lst_h": tal._f(m.get("lst_h")), "jd": tal._f(m.get("jd"))}

    # -- everything
    def run(self) -> dict:
        self.t0 = self.d.mono()
        self.started_utc = self.d.now()
        try:
            self.model = (self.do.model() if self.do.model else {}) or {}
        except Exception as e:  # noqa: BLE001
            self.model = {"note": f"not read ({e})"}
        self.say("Drift survey" + (f" ({self.do.label})" if self.do.label else "")
                 + ": " + format_model(self.model))
        pre = self.preflight()
        if not pre["go"]:
            self.say("NO GO: " + "; ".join(pre["blockers"]))
            return self.summary("refused", "preflight: " + "; ".join(pre["blockers"]))
        if not self.do.unattended and not self.yes(
                "  TheSky's Camera Setup selects the AP26MC, PHD2 is not guiding, "
                "TPoint corrections / ProTrack are as you want them measured?"):
            return self.summary("refused", "operator did not confirm")
        if self.o.unpark:
            why = self.check_park()
            if why:
                self.say("NO GO: " + why)
                return self.summary("refused", why)
        status = "stopped"
        try:
            self.lend()
            self.setup()
            if self.needs_unpark:
                self.unpark()
            self.say(f"Step 5: {len(self.points)} point(s) x {len(self.offsets)} frames. "
                     "Ctrl+C stops safely (camera and mount abort, camera goes back "
                     "to NINA #1).")
            self.loop()
            status, self.reason = "done", "all points done"
        except tal.StopRun as e:
            self.reason = str(e)
            self.say(f"STOP: {self.reason}")
            self.abort()
        except KeyboardInterrupt:
            self.reason = "operator Ctrl+C"
            self.say("Ctrl+C: stopping.")
            self.abort()
        except Exception as e:  # noqa: BLE001
            self.reason = f"error {type(e).__name__}: {e}"
            if ta.is_parked_error(e) and not self.o.unpark:
                self.reason += "; " + ta.UNPARK_HINT
            self.say(f"STOP on an error: {self.reason}")
            self.abort()
        finally:
            if self.lent:
                try:
                    self.returned = self.handback()
                except KeyboardInterrupt:
                    self.returned = False
                    self.say("Interrupted during the hand-back: give NINA #1 its "
                             "camera back by hand.")
        return self.summary(status, self.reason)

    def summary(self, status: str, reason: str) -> dict:
        self._flush_solves()
        summ = summarize(self.results, self.do.max_smear, self.do.fwhm)
        s = {"status": status, "reason": reason, "label": self.do.label,
             "attempted": len(self.results),
             "measured": summ.get("measured", 0),
             "solves": sum(1 for r in self.solve_rows if r.get("solved")),
             "frames": len(self.solve_rows),
             "seconds": round(self.d.mono() - self.t0),
             "camera_returned": self.returned, "filter": self.filter_used,
             "unparked": self.unparked, "unpark": self.unpark_log or None,
             "nina2_stopped": self.nina2_stopped,
             "csv": self.csv_file if self.solve_rows else None,
             "fits_csv": self.fit_file if self.results else None,
             "json": self.json_file, "model": self.model, "summary": summ,
             "points": self.results}
        self.say("")
        for line in format_summary(summ, self.model, self.do.label):
            self.say(line)
        self.say(f"  result: {status} ({reason}); points {len(self.results)}, frames "
                 f"{len(self.solve_rows)}, solved {s['solves']}; time "
                 f"{tal._fmt_min(s['seconds'])}")
        if self.nina2_stopped:
            self.say("  --stop-nina2: NINA #2's sequence was stopped (not restarted)")
        if self.unpark_log:
            self.say(f"  --unpark: {self.unpark_log} (it never parks)")
        if self.returned is not None:
            self.say("  camera back on NINA #1: " + ("yes" if self.returned else
                                                     "NO, fix by hand"))
        self._write_json(status, s)
        if self.json_file and (self.results or self.solve_rows):
            self.say(f"  files: {self.csv_file}, {self.fit_file}, {self.json_file}")
        self.say("  Nothing was synced, parked or written to TPoint; nothing was armed.")
        return s

    # -- files
    def _append(self, path: str | None, fields: list[str], row: dict) -> None:
        if not path:
            return
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.exists()
            with open(p, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            self.v(f"CSV write failed ({e})")

    def _flush_solves(self) -> None:
        """The solve rows not written yet (a point's rows go out once its
        fit has added the offsets; a stop mid-point flushes the rest)."""
        for row in self.solve_rows:
            if not row.get("_written"):
                row["_written"] = True
                self._append(self.csv_file, SOLVE_FIELDS, row)

    def _write_fit(self, res: dict) -> None:
        f = res.get("fit") or {}
        sm = res.get("smear") or {}
        row = {"label": self.do.label, "n_solves": f.get("n", 0), **res, **f,
               **{f"smear_{k}": v for k, v in (sm.get("smear") or {}).items()},
               **{f"ecc_{k}": v for k, v in (sm.get("ecc") or {}).items()},
               "max_sub_s": sm.get("max_sub_s"), "rec_sub_s": sm.get("rec_sub_s")}
        row.pop("fit", None)
        self._append(self.fit_file, FIT_FIELDS, row)

    def _write_json(self, status: str, s: dict | None = None) -> None:
        if not self.json_file:
            return
        doc = {"kind": "drift_survey", "version": 1, "status": status,
               "label": self.do.label,
               "started_utc": self.started_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
               if self.started_utc else None,
               "written_utc": self.d.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
               "model": self.model,
               "params": {"window_s": self.do.window_s, "every_s": self.do.every_s,
                          "settle_s": self.do.settle_s, "exposure_s": self.exposure_s,
                          "binning": self.o.binning, "solver": self.o.solver,
                          "filter": self.filter_used, "track": self.o.track,
                          "max_smear_arcsec": self.do.max_smear,
                          "fwhm_arcsec": self.do.fwhm, "frames": self.offsets},
               "points": self.results,
               "summary": summarize(self.results, self.do.max_smear, self.do.fwhm),
               "csv": self.csv_file, "fits_csv": self.fit_file}
        if s is not None:
            doc.update(reason=s.get("reason"), camera_returned=s.get("camera_returned"),
                       seconds=s.get("seconds"))
        try:
            p = Path(self.json_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
            tmp.replace(p)
        except OSError as e:
            self.v(f"JSON write failed ({e})")


def run_survey(config, points: list[dict], deps: tal.Deps, opts: tal.Opts,
               dopts: DriftOpts) -> dict:
    return DriftRun(config, points, deps, opts, dopts).run()


def select(config, n: int, side: str, now: datetime, window_s: float, every_s: float,
           exposure_s: float, settle_s: float, dark_end: datetime | None = None,
           sky_points: list | None = None, moon_altaz: list | None = None) -> dict:
    """tpoint-autocal's point picker (both sides, meridian band, pole and
    moon skipped) at alt >= 30 with a whole point's dwell as the pace: a
    point whose tracked position would set below 30 deg or reach the
    meridian band during its window is left out."""
    min_alt = max(MIN_ALT, float(getattr(config, "tpoint_mapping_min_alt", MIN_ALT)
                                 or MIN_ALT))
    return tal.select_points(config, n, side, now, exposure_s, dark_end,
                             sky_points=sky_points, moon_altaz=moon_altaz,
                             pace=per_point_s(window_s, every_s, exposure_s, settle_s),
                             min_alt=min_alt)
