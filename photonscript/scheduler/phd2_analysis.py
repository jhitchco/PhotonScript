"""PHD2 guide-log analysis: why guiding failed, per night and per sub (PS-88).

Builds on phd2_logs (PS-73 discovery + parser) and answers, for one night:

* per guiding session: RMS RA / Dec / total (arcsec; all frames and settled
  frames, about the lock position and PHD2-style standard deviation), peaks,
  SNR spread, saturated share, star-mass variability, drops by PHD2's own
  reason, star-lost rate per hour;
* corrections per axis: direction balance, share of pulses at the max
  duration, the drift of the raw error, and the correction PHD2 commanded
  against what the star actually did (a mount that ignores guide pulses
  shows a large commanded rate and no movement);
* dithers: size against the search region, how often and how fast the star
  got back to the new lock position, NINA/PHD2 settle success;
* the calibration in use: its orthogonality, measured rates against the
  cos(Dec) expectation, step count, where it was taken (pier side, Dec,
  hour angle) and whether guiding ran on the other pier side or far away in
  Dec ("calibrated East, guiding West");
* rule-based findings with severity and a plain-language fix;
* per sub: the guiding during that sub's own exposure window (RMS, drops,
  settling), so PS-21's guide_rms can use the true during-the-sub figure
  instead of the snapshot at sub end (see sub_guide_stats / GuideTimeline).

PHD2 writes its guide log in the scope PC's LOCAL time; every timestamp is
kept local and converted to UTC through the observatory time zone.
Read-only: nothing here talks to PHD2, NINA or the mount.
"""
from __future__ import annotations

import bisect
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from photonscript.scheduler import log_files as lf
from photonscript.scheduler import phd2_logs as pl

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
# within a severity, root causes first
RULE_ORDER = ["pulses_not_moving", "pulses_reversed", "calibration_no_motion",
              "star_static", "calibration_ortho",
              "calibration_other_pier", "max_duration_pulses", "dec_one_direction",
              "search_region_small", "saturated_star", "settle_failures",
              "mass_change_drops", "star_lost_rate", "low_snr",
              "scale_mismatch", "calibration_quality", "calibration_far_dec",
              "oversampled", "no_dark"]

# Rule thresholds (kept together so the report can quote them)
T = {
    "saturated_pct": 50.0,          # % guided frames flagged STAR_SATURATED
    "max_pulse_pct": 20.0,          # % pulses on one axis at the max duration
    "dec_one_way_pct": 90.0,        # % Dec pulses in one direction
    "ortho_warn_deg": 5.0,          # calibration orthogonality error
    "ortho_bad_deg": 10.0,
    "high_dec_deg": 60.0,           # calibrating above this |Dec| is poor
    "far_ha_hr": 3.0,               # calibrating further than this from the meridian
    "dec_offset_deg": 20.0,         # guiding this far in Dec from the calibration
    "mass_drop_pct": 2.0,           # % frames lost to "mass changed"
    "low_snr_median": 10.0,
    "drop_pct": 5.0,                # % frames lost to any reason
    "oversampled_arcsec_px": 0.5,
    "search_vs_dither": 2.0,        # search region >= 2 x largest dither
    "search_vs_rms": 3.0,           # search region >= 3 x total RMS (px)
    "settle_success_pct": 70.0,
    "cmd_rate_arcsec_min": 10.0,    # commanded correction worth judging
    "response_min": 0.25,           # observed / commanded below this = no response
    "min_steps": 8,                 # PHD2 aims for about 12 calibration steps
    "rate_ratio_tol": 0.3,          # measured RA/Dec rate ratio vs cos(Dec)
    "min_session_s": 120.0,         # shorter sessions get no per-axis rules
    "saturated_low_snr": 60.0,      # 'saturated' below this SNR = ADU ceiling
    "static_std_arcsec": 0.15,      # a real star jitters more than this
    "static_min_s": 300.0,          # ... over at least this long between dithers
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _r(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) and math.isfinite(v) else None


def _rms(xs):
    return (sum(x * x for x in xs) / len(xs)) ** 0.5 if xs else None


def _std(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else (0.0 if xs else None)


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def _quantile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _tz(config):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(getattr(config, "observatory_tz", "") or "America/Denver")
    except Exception:  # noqa: BLE001 - no tzdata: fixed offset fallback
        return None


def to_utc(config, local: datetime | None) -> datetime | None:
    """Scope-PC local wall time (naive) -> naive UTC via the observatory tz."""
    if local is None:
        return None
    tz = _tz(config)
    if tz is not None:
        return local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return local - timedelta(hours=float(getattr(config, "utc_offset_hours", -7.0)))


def _g(v) -> str:
    """Compact number for text: 66.6, 0 (never '-0')."""
    if v is None:
        return "?"
    v = round(float(v), 2)
    return f"{0.0 if v == 0 else v:g}"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(sep=" ", timespec="seconds") if dt else None


def _iso_z(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _slope(points):
    """Least-squares slope of (t, y) pooled within groups: {key: [(t, y)]}.
    Pooling within lock epochs keeps a dither's step out of the drift."""
    num = den = 0.0
    for pts in points.values():
        if len(pts) < 3:
            continue
        tm = sum(p[0] for p in pts) / len(pts)
        ym = sum(p[1] for p in pts) / len(pts)
        num += sum((p[0] - tm) * (p[1] - ym) for p in pts)
        den += sum((p[0] - tm) ** 2 for p in pts)
    return num / den if den > 0 else None


# --------------------------------------------------------------------------
# calibration quality
# --------------------------------------------------------------------------

def calibration_quality(dec_deg, hour_angle_hr, x_angle, x_rate, y_angle, y_rate,
                        scale=None, ra_speed=None, dec_speed=None, steps=None,
                        last_issue=None, ortho=None) -> dict:
    """Judge one calibration. Rates are px/s as PHD2 measured them."""
    issues = []
    ortho = ortho if ortho is not None else pl.ortho_error(x_angle, y_angle)
    if ortho is not None and ortho > T["ortho_warn_deg"]:
        issues.append(f"axes {ortho:.1f} deg from perpendicular "
                      f"(PHD2 wants under {T['ortho_warn_deg']:g})")
    out = {"ortho_err_deg": ortho}
    cosd = math.cos(math.radians(dec_deg)) if dec_deg is not None else None
    if x_rate and y_rate and cosd is not None:
        spd = (ra_speed / dec_speed) if ra_speed and dec_speed else 1.0
        exp_ratio = cosd * spd
        ratio = x_rate / y_rate
        out.update(rate_ratio=_r(ratio, 3), expected_ratio=_r(exp_ratio, 3),
                   ratio_vs_expected=_r(ratio / exp_ratio, 2) if exp_ratio > 0.02 else None)
        if exp_ratio > 0.02 and abs(ratio / exp_ratio - 1) > T["rate_ratio_tol"]:
            issues.append(f"RA/Dec rate ratio {ratio:.2f} vs {exp_ratio:.2f} expected "
                          f"from cos(Dec {_g(dec_deg)})")
    if scale and dec_speed and y_rate:
        exp_dec = dec_speed / scale
        out["dec_rate_vs_expected"] = _r(y_rate / exp_dec, 2)
        if ra_speed and x_rate and cosd:
            out["ra_rate_vs_expected"] = _r(x_rate / (ra_speed * cosd / scale), 2)
    if dec_deg is not None and abs(dec_deg) > T["high_dec_deg"]:
        issues.append(f"taken at Dec {_g(dec_deg)} (RA rate is poorly measured above "
                      f"{T['high_dec_deg']:g}; calibrate within 20 deg of Dec 0)")
    if hour_angle_hr is not None and abs(hour_angle_hr) > T["far_ha_hr"]:
        issues.append(f"taken {abs(hour_angle_hr):.1f} h from the meridian")
    if steps:
        few = {k: v for k, v in steps.items() if k in ("West", "North")
               and v is not None and v < T["min_steps"]}
        if few:
            issues.append("few steps (" + ", ".join(f"{k} {v}" for k, v in few.items())
                          + "; PHD2 aims for about 12)")
    if last_issue and str(last_issue).strip().lower() not in ("none", ""):
        issues.append(f"PHD2 flagged '{last_issue}'")
    ratio_off = out.get("ratio_vs_expected")
    bad = (ortho is not None and ortho > T["ortho_bad_deg"]) or (
        ratio_off is not None and abs(ratio_off - 1) > 0.5)
    out["issues"] = issues
    out["quality"] = "poor" if bad else ("fair" if issues else "good")
    return out


def _cal_record(i, sec, config) -> dict:
    c = pl._calibration(sec)
    h = sec["header"]
    scale, _ = pl._scale_for(h, config)
    x, y = c["axes"].get("West", {}), c["axes"].get("North", {})
    moved = {d: _r(max((abs(v or 0) for _, v in pts), default=0.0), 1)
             for d, pts in sec["cal_steps"].items()}
    q = calibration_quality(c["dec_deg"], c["hour_angle_hr"], x.get("angle_deg"),
                            x.get("rate_px_s"), y.get("angle_deg"),
                            y.get("rate_px_s"), scale, h.get("ra_guide_speed"),
                            h.get("dec_guide_speed"), c["steps"])
    if c["result"] != "complete":
        q["quality"] = "failed" if c["result"] == "failed" else "aborted"
        stuck = [d for d in ("West", "North") if c["steps"].get(d, 0) >= 10
                 and (moved.get(d) or 0) < 3]
        if stuck:
            each = ", ".join(f"{moved[d]} px in {c['steps'][d]} steps" for d in stuck)
            q["issues"].append(f"star barely moved on {', '.join(stuck)} pulses ({each})")
    start_l = sec["start_local"]
    return {"index": i, "start_local": _iso(start_l),
            "start_utc": _iso_z(to_utc(config, start_l)), "file": sec["file"],
            "result": c["result"], "pier_side": c["pier_side"],
            "dec_deg": c["dec_deg"], "hour_angle_hr": c["hour_angle_hr"],
            "alt_deg": c["alt_deg"],
            "ra": {"angle_deg": x.get("angle_deg"), "rate_px_s": x.get("rate_px_s")},
            "dec": {"angle_deg": y.get("angle_deg"), "rate_px_s": y.get("rate_px_s")},
            "steps": c["steps"], "moved_px": moved,
            "step_ms": c["step_ms"], "distance_px": c["distance_px"],
            "backlash_clearing": {"steps": c["backlash_steps"], "px": c["backlash_px"]},
            "star_lost": c["star_lost"], "exposure_ms": c["exposure_ms"],
            "messages": c["messages"], **q}


def pulse_rates(h, scale) -> tuple[float | None, float | None]:
    """px/s a guide pulse moves the star at this pointing, RA and Dec.
    From PHD2's 'Norm rates' ("/s at Dec 0, measured at calibration) when
    present: the header's xRate is sometimes already rescaled to the current
    Dec and sometimes not (PHD2 skips the cos(Dec) rescale above 60 deg)."""
    xr, yr = h.get("x_rate"), h.get("y_rate")
    nra, ndec, dec = h.get("norm_rate_ra"), h.get("norm_rate_dec"), h.get("dec_deg")
    if scale and nra and dec is not None:
        xr = nra * math.cos(math.radians(dec)) / scale
    if scale and ndec:
        yr = ndec / scale
    return xr, yr


_CAL_TS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S")


def _cal_ts(s):
    for fmt in _CAL_TS:
        try:
            return datetime.strptime(str(s or "").strip(), fmt)
        except ValueError:
            continue
    return None


def _match_calibration(h, start_local, cals):
    """The calibration a guiding session used. PHD2 stamps the guiding header
    with the calibration's time ('Timestamp = 9/26/2026 21:44:20', when it
    finished): take the latest complete calibration that began before that.
    Without a stamp: same Dec rate and RA angle (or its flip), latest first."""
    ts = _cal_ts(h.get("cal_timestamp"))
    done = [c for c in cals if c["result"] == "complete" and c["_start"] is not None]
    if ts is not None:
        best = [c for c in done if c["_start"] <= ts + timedelta(seconds=5)
                and ts - c["_start"] <= timedelta(minutes=30)]
        return best[-1] if best else None
    yr, xa = h.get("y_rate"), h.get("x_angle")
    best = None
    for c in done:
        if start_local is not None and c["_start"] > start_local:
            continue
        if yr is not None and abs((c["dec"]["rate_px_s"] or -1) - yr) > 0.01:
            continue
        if xa is not None and c["ra"]["angle_deg"] is not None:
            d = abs((xa - c["ra"]["angle_deg"] + 180) % 360 - 180)
            if min(d, abs(180 - d)) > 1.0:
                continue
        best = c
    return best


# --------------------------------------------------------------------------
# per session
# --------------------------------------------------------------------------

def _axis_stats(frames, key, ms_key, dir_key, rate_px_s, max_ms, scale):
    """Pulse balance, max-duration share, drift and commanded vs observed."""
    guided = [f for f in frames if not f["drop"]]
    live = [f for f in guided if f.get("output", True)]  # guide output on
    pulses = [f for f in live if f[ms_key] > 0 and f[dir_key]]
    by_dir: dict[str, int] = {}
    for f in pulses:
        by_dir[f[dir_key]] = by_dir.get(f[dir_key], 0) + 1
    # which direction PHD2 uses for a positive raw error (W and S in 2.6)
    votes: dict[str, int] = {}
    for f in pulses:
        if f[key]:
            d = f[dir_key] if f[key] > 0 else "-" + f[dir_key]
            votes[d] = votes.get(d, 0) + 1
    pos = max((d for d in votes if not d.startswith("-")),
              key=lambda d: votes[d], default=None)
    at_max = sum(1 for f in pulses if max_ms and f[ms_key] >= max_ms - 1)
    dom = max(by_dir, key=by_dir.get) if by_dir else None
    out = {"pulses": len(pulses), "by_direction": by_dir,
           "dominant": dom, "dominant_pct": _pct(by_dir.get(dom, 0), len(pulses)),
           "at_max": at_max, "at_max_pct": _pct(at_max, len(pulses)),
           "mean_ms": _r(statistics.fmean([f[ms_key] for f in pulses]), 0) if pulses else None}
    # drift of the raw error within each lock epoch
    pts: dict[int, list] = {}
    for f in guided:
        pts.setdefault(f["epoch"], []).append((f["t"], f[key]))
    sl = _slope(pts)
    out["drift_px_min"] = _r(sl * 60, 3) if sl is not None else None
    out["drift_arcsec_min"] = _r(sl * 60 * scale, 3) if sl is not None and scale else None
    if guided:
        out["error_first_px"] = _r(guided[0][key], 1)
        out["error_last_px"] = _r(guided[-1][key], 1)
        out["error_peak_px"] = _r(max((f[key] for f in guided), key=abs), 1)
    # commanded correction vs what the star did, per lock epoch
    C = O = span = 0.0
    if rate_px_s and pos:
        segs: dict[int, list] = {}
        for f in live:
            segs.setdefault(f["epoch"], []).append(f)
        for seg in segs.values():
            if len(seg) < 3:
                continue
            for f in seg[:-1]:
                if f[ms_key] > 0 and f[dir_key]:
                    sgn = -1.0 if f[dir_key] == pos else 1.0
                    C += sgn * f[ms_key] / 1000.0 * rate_px_s
            O += seg[-1][key] - seg[0][key]
            span += seg[-1]["t"] - seg[0]["t"]
    if span > 0 and scale:
        cmd = C * scale / span * 60
        obs = O * scale / span * 60
        out.update(commanded_arcsec_min=_r(cmd, 1), observed_arcsec_min=_r(obs, 1),
                   implied_drift_arcsec_min=_r(obs - cmd, 1))
        if abs(cmd) >= T["cmd_rate_arcsec_min"] and span >= T["min_session_s"]:
            resp = obs / cmd
            out["response"] = _r(resp, 2)
            out["response_verdict"] = (
                "reversed" if resp < -T["response_min"] else
                "not moving" if resp < T["response_min"] else
                "weak" if resp < 0.5 else "moving")
        else:
            out["response_verdict"] = "small demand"
    return out


def _dithers(sec, frames):
    """Dither sizes, recovery to the new lock, and PHD2 settle outcomes."""
    ev = sec["events"]
    sizes, rec = [], []
    for e in ev:
        if e[0] != "dither":
            continue
        size = math.hypot(e[2] or 0, e[3] or 0)
        sizes.append(size)
        after = [f for f in frames[e[1]:] if not f["drop"]]
        if after:
            ep = after[0]["epoch"]
            after = [f for f in after if f["epoch"] == ep]
        target = max(2.0, 0.25 * size)
        hit = next((f for f in after if math.hypot(f["ra"], f["dec"]) <= target), None)
        rec.append({"size_px": _r(size, 1), "frames_after": len(after),
                    "recovered": hit is not None,
                    "recover_s": _r(hit["t"] - after[0]["t"], 1) if hit and after else None,
                    "residual_px": _r(math.hypot(after[-1]["ra"], after[-1]["dec"]), 1)
                    if after else None})
    st = [e for e in ev if e[0] == "settle"]
    started = sum(1 for e in st if e[2] == "started")
    done = sum(1 for e in st if e[2] == "complete")
    failed = sum(1 for e in st if e[2] == "failed")
    times, open_i = [], None
    for e in st:
        if e[2] == "started":
            open_i = e[1]
        elif e[2] == "complete" and open_i is not None:
            a = frames[open_i]["t"] if open_i < len(frames) else None
            b = frames[e[1] - 1]["t"] if 0 < e[1] <= len(frames) else None
            if a is not None and b is not None and b >= a:
                times.append(b - a)
            open_i = None
        elif e[2] == "failed":
            open_i = None
    return {"count": len(sizes), "max_px": _r(max(sizes), 1) if sizes else None,
            "median_px": _r(statistics.median(sizes), 1) if sizes else None,
            "recovered": sum(1 for r in rec if r["recovered"]),
            "not_recovered": sum(1 for r in rec if not r["recovered"] and r["frames_after"] >= 3),
            "each": rec,
            "settle": {"started": started, "completed": done, "failed": failed,
                       "unfinished": max(0, started - done - failed),
                       "success_pct": _pct(done, done + failed),
                       "median_s": _r(statistics.median(times), 1) if times else None}}


def _epochs(guided, scale) -> dict:
    """Scatter of the star within each lock epoch (between dithers): a real
    star through seeing scatters 0.3 to 0.6\" here; a hot pixel or a fixed
    artifact sits still."""
    ep: dict[int, list] = {}
    for f in guided:
        if not f["settling"]:
            ep.setdefault(f["epoch"], []).append(f)
    num = den = 0.0
    static_s, stds = 0.0, []
    for fs in ep.values():
        if len(fs) < 20:
            continue
        v = statistics.pvariance([f["ra"] for f in fs]) + \
            statistics.pvariance([f["dec"] for f in fs])
        num += v * len(fs)
        den += len(fs)
        sd = math.sqrt(v) * (scale or 0)
        stds.append(sd)
        span = fs[-1]["t"] - fs[0]["t"]
        if scale and len(fs) >= 60 and span >= T["static_min_s"] and \
                sd < T["static_std_arcsec"]:
            static_s += span
    return {"count": len(ep),
            "pooled_std_arcsec": _r(math.sqrt(num / den) * scale, 3) if den and scale else None,
            "min_std_arcsec": _r(min(stds), 3) if stds else None,
            "static_minutes": _r(static_s / 60, 1)}


def _block(ra, dec, scale):
    rr, rd = _rms(ra), _rms(dec)
    tt = math.hypot(rr, rd) if rr is not None else None
    sr, sd = _std(ra), _std(dec)
    st = math.hypot(sr, sd) if sr is not None else None
    k = scale or 0
    out = {"n": len(ra), "ra_px": _r(rr, 3), "dec_px": _r(rd, 3), "total_px": _r(tt, 3),
           "std_ra_px": _r(sr, 3), "std_dec_px": _r(sd, 3), "std_total_px": _r(st, 3)}
    if k:
        out.update(ra_arcsec=_r(rr * k if rr is not None else None, 3),
                   dec_arcsec=_r(rd * k if rd is not None else None, 3),
                   total_arcsec=_r(tt * k if tt is not None else None, 3),
                   std_ra_arcsec=_r(sr * k if sr is not None else None, 3),
                   std_dec_arcsec=_r(sd * k if sd is not None else None, 3),
                   std_total_arcsec=_r(st * k if st is not None else None, 3))
    return out


def _config_scale(config, binning):
    try:
        from photonscript.telescope_agent.phd2_client import guide_scale_from_config
        return guide_scale_from_config(config, int(binning or 1))
    except Exception:  # noqa: BLE001
        return None


def analyze_session(idx, sec, cals, config) -> dict:
    h = sec["header"]
    frames = sec["frames"]
    scale, src = pl._scale_for(h, config)
    guided = [f for f in frames if not f["drop"]]
    drops = [f for f in frames if f["drop"]]
    start_l = sec["start_local"]
    last_t = frames[-1]["t"] if frames else 0.0
    # an 'Ends at' line is the end; otherwise (interrupted by a calibration,
    # log closed, file cut) the last frame is
    end_l = sec["end_local"] if sec["closed"] in ("ended", "aborted") else None
    if end_l is None and start_l:
        end_l = start_l + timedelta(seconds=last_t)
    dur = (end_l - start_l).total_seconds() if start_l and end_l else last_t
    dur = max(dur, last_t)
    sat = sum(1 for f in guided if f["code"] == 1)
    reasons: dict[str, int] = {}
    codes: dict[str, int] = {}
    for f in drops:
        reasons[f["reason"]] = reasons.get(f["reason"], 0) + 1
        cn = pl.FIND_RESULT.get(f["code"], str(f["code"]))
        codes[cn] = codes.get(cn, 0) + 1
    snr = [f["snr"] for f in guided if f["snr"] is not None]
    mass = [f["mass"] for f in guided if f["mass"]]
    mcv = (_std(mass) / statistics.fmean(mass)) if len(mass) > 2 else None
    settled = [f for f in guided if not f["settling"]]
    dec_now = h.get("dec_deg")
    try:
        cal_dec = float(h.get("cal_dec")) if h.get("cal_dec") not in (None, "") else None
    except ValueError:
        cal_dec = None
    xr, yr = pulse_rates(h, scale)
    ra_ax = _axis_stats(frames, "ra", "ra_ms", "ra_dir", xr, h.get("max_ra_ms"), scale)
    dec_ax = _axis_stats(frames, "dec", "dec_ms", "dec_dir", yr, h.get("max_dec_ms"), scale)
    dith = _dithers(sec, frames)
    # the calibration in use
    cal = _match_calibration(h, start_l, cals)
    ortho = h.get("ortho_err_deg")
    if cal:
        q = calibration_quality(cal["dec_deg"], cal["hour_angle_hr"],
                                cal["ra"]["angle_deg"], cal["ra"]["rate_px_s"],
                                cal["dec"]["angle_deg"], cal["dec"]["rate_px_s"],
                                scale, h.get("ra_guide_speed"), h.get("dec_guide_speed"),
                                cal["steps"], h.get("last_cal_issue"), ortho)
    else:  # calibrated before these logs: judge what the header says
        q = calibration_quality(cal_dec, None, h.get("x_angle"), None,
                                h.get("y_angle"), None, None, None, None, None,
                                h.get("last_cal_issue"), ortho)
    flipped = None
    if cal and cal["ra"]["angle_deg"] is not None and h.get("x_angle") is not None:
        d = abs((h["x_angle"] - cal["ra"]["angle_deg"] + 180) % 360 - 180)
        flipped = d > 90
    cal_pier = cal["pier_side"] if cal else None
    pier = h.get("pier_side")
    cal_use = {"index": cal["index"] if cal else None,
               "start_local": cal["start_local"] if cal else None,
               "timestamp": h.get("cal_timestamp"),
               "pier_side": cal_pier, "dec_deg": cal_dec,
               "hour_angle_hr": cal["hour_angle_hr"] if cal else None,
               "ra_angle_deg": h.get("x_angle"), "ra_rate_px_s": h.get("x_rate"),
               "dec_angle_deg": h.get("y_angle"), "dec_rate_px_s": h.get("y_rate"),
               "parity": h.get("parity"), "last_cal_issue": h.get("last_cal_issue"),
               "pier_mismatch": bool(cal_pier and pier and cal_pier.lower() != pier.lower()
                                     and "unknown" not in (cal_pier + pier).lower()),
               "flipped_by_phd2": flipped,
               "dec_offset_deg": _r(abs(dec_now - cal_dec), 1)
               if dec_now is not None and cal_dec is not None else None,
               **q}
    in_session_s = max(dur, 1.0)
    s = {
        "index": idx, "file": sec["file"], "closed": sec["closed"],
        "start_local": _iso(start_l), "end_local": _iso(end_l),
        "start_utc": _iso_z(to_utc(config, start_l)),
        "end_utc": _iso_z(to_utc(config, end_l)),
        "duration_min": _r(dur / 60, 1),
        "pointing": {k: h.get(k) for k in ("ra_hr", "dec_deg", "hour_angle_hr",
                                           "pier_side", "alt_deg", "az_deg")},
        "settings": {
            "exposure_ms": h.get("exposure_ms"), "exposure_changes": h.get("exposure_changes"),
            "pixel_scale_arcsec": _r(scale, 3), "scale_source": src,
            "config_scale_arcsec": _r(_config_scale(config, h.get("binning")), 3),
            "binning": h.get("binning"), "focal_length_mm": h.get("focal_length_mm"),
            "camera": h.get("camera"), "gain": h.get("gain"),
            "search_region_px": h.get("search_region_px"),
            "mass_tolerance": h.get("mass_tolerance"),
            "multi_star": h.get("multi_star"), "have_dark": h.get("have_dark"),
            "defect_map": h.get("defect_map"),
            "noise_reduction": h.get("noise_reduction"),
            "ra_algorithm": h.get("x_algorithm"), "ra_params": h.get("x_params"),
            "dec_algorithm": h.get("y_algorithm"), "dec_params": h.get("y_params"),
            "backlash_comp": h.get("backlash_comp"),
            "max_ra_ms": h.get("max_ra_ms"), "max_dec_ms": h.get("max_dec_ms"),
            "dec_mode": h.get("dec_mode"),
            "ra_guide_speed": h.get("ra_guide_speed"),
            "dec_guide_speed": h.get("dec_guide_speed"),
            "dither_scale": h.get("dither_scale"), "profile": h.get("profile"),
            "mount": h.get("mount")},
        "frames": {"total": len(frames), "guided": len(guided), "saturated": sat,
                   "saturated_pct": _pct(sat, len(guided)), "dropped": len(drops),
                   "dropped_pct": _pct(len(drops), len(frames)),
                   "drop_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
                   "drop_codes": codes,
                   "drops_per_hour": _r(len(drops) / (in_session_s / 3600), 1)},
        "rms": {"all": _block([f["ra"] for f in guided], [f["dec"] for f in guided], scale),
                "settled": _block([f["ra"] for f in settled], [f["dec"] for f in settled], scale)},
        "peak": {"ra_px": ra_ax.get("error_peak_px"), "dec_px": dec_ax.get("error_peak_px"),
                 "ra_arcsec": _r(abs(ra_ax["error_peak_px"]) * scale, 2)
                 if scale and ra_ax.get("error_peak_px") is not None else None,
                 "dec_arcsec": _r(abs(dec_ax["error_peak_px"]) * scale, 2)
                 if scale and dec_ax.get("error_peak_px") is not None else None},
        "snr": {"min": _r(min(snr), 1) if snr else None, "p10": _r(_quantile(snr, .1), 1),
                "median": _r(statistics.median(snr), 1) if snr else None,
                "max": _r(max(snr), 1) if snr else None},
        "star_mass": {"median": _r(statistics.median(mass), 0) if mass else None,
                      "cv": _r(mcv, 2)},
        "epochs": _epochs(guided, scale),
        "corrections": {"ra": ra_ax, "dec": dec_ax},
        "dithers": dith,
        "calibration": cal_use,
    }
    s["findings"] = session_findings(s)
    return s


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def _f(fid, sev, title, detail, fix, weight=None, **ev):
    """A finding. `weight` picks which session's detail the night shows
    (default: the longest session)."""
    return {"id": fid, "severity": sev, "title": title, "detail": detail,
            "recommendation": fix, "evidence": ev, "weight": weight}


def session_findings(s) -> list[dict]:
    out = []
    fr, st, cal = s["frames"], s["settings"], s["calibration"]
    long_enough = (s["duration_min"] or 0) * 60 >= T["min_session_s"]
    guided = fr["guided"]
    scale = st["pixel_scale_arcsec"]
    for ax, name, dirs in (("ra", "RA", "W/E"), ("dec", "Dec", "N/S")):
        a = s["corrections"][ax]
        v = a.get("response_verdict")
        if v in ("not moving", "reversed") and long_enough:
            cmd, obs = a["commanded_arcsec_min"], a["observed_arcsec_min"]
            if v == "not moving":
                out.append(_f(
                    "pulses_not_moving", "critical",
                    "Guide pulses did not move the mount",
                    f"{name}: PHD2 commanded {abs(cmd):.0f}\"/min of correction but the "
                    f"star moved {abs(obs):.1f}\"/min (response {a['response']:+.2f}; 1.0 "
                    f"= pulses fully acted on). Working pulses would need a "
                    f"{abs(a['implied_drift_arcsec_min']):.0f}\"/min tracking drift to "
                    f"hide them, far beyond this mount's unguided drift.",
                    "Check the pulse-guide path before the next night: in PHD2 run "
                    "Tools > Manual Guide (or Star-Cross Test) on this pier side and "
                    "watch the star move; check TheSky's autoguide / guide-rate "
                    "settings and whether ProTrack or a TheSky tracking-rate override "
                    "is swallowing ASCOM PulseGuide (PS-66); then recalibrate.",
                    weight=abs(cmd) * (s["duration_min"] or 0),
                    axis=name, commanded_arcsec_min=cmd, observed_arcsec_min=obs,
                    response=a["response"]))
            else:
                out.append(_f(
                    "pulses_reversed", "critical",
                    "Guide pulses moved the star the wrong way",
                    f"{name}: commanded {cmd:+.0f}\"/min, the star moved {obs:+.0f}\"/min "
                    f"(response {a['response']:+.2f}).",
                    "Recalibrate on this pier side; if it happens after a meridian "
                    "flip, toggle PHD2's 'Reverse Dec output after meridian flip' "
                    "(Advanced > Mount) to match the mount.",
                    axis=name, response=a["response"]))
        if long_enough and a["pulses"] >= 20 and (a["at_max_pct"] or 0) > T["max_pulse_pct"]:
            mx = st["max_ra_ms" if ax == "ra" else "max_dec_ms"]
            out.append(_f(
                "max_duration_pulses", "warning",
                f"{name} corrections pinned at the max pulse",
                f"{a['at_max']} of {a['pulses']} {name} pulses ({a['at_max_pct']:.0f}%) hit "
                f"the {mx:g} ms limit, mostly {a['dominant']} ({a['dominant_pct']:.0f}%).",
                "The guider could not keep up: wrong calibration rates for this "
                "pointing, the mount not acting on pulses, or a tracking problem. "
                "Recalibrate near Dec 0 and the meridian and check the tracking "
                "rate (ProTrack / TPoint, PS-66).",
                weight=a["at_max"],
                axis=name, at_max_pct=a["at_max_pct"], dominant=a["dominant"]))
    d = s["corrections"]["dec"]
    grew = (d.get("error_first_px") is not None and d.get("error_last_px") is not None
            and abs(d["error_last_px"]) > abs(d["error_first_px"]) + 5)
    if long_enough and d["pulses"] >= 30 and (d["dominant_pct"] or 0) > T["dec_one_way_pct"] and grew:
        other = {k: v for k, v in d["by_direction"].items() if k != d["dominant"]}
        out.append(_f(
            "dec_one_direction", "warning",
            "Dec pushed one way while the Dec error grew",
            f"{d['by_direction'].get(d['dominant'], 0)} {d['dominant']} vs "
            f"{sum(other.values())} other Dec pulses; Dec error went from "
            f"{d['error_first_px']:+.0f} to {d['error_last_px']:+.0f} px"
            + (f" ({d['error_last_px'] * scale:+.0f}\")" if scale else "")
            + (f", drift {d['drift_arcsec_min']:+.2f}\"/min between dithers"
               if abs(d.get("drift_arcsec_min") or 0) >= 0.05 else "")
            + ".",
            "Polar-alignment drift, Dec backlash or Dec reversed after the flip. "
            "Run PHD2's Guiding Assistant with the backlash measurement, enable "
            "backlash compensation if it finds backlash, and check the polar "
            "alignment; if it follows a flip, check the Dec reversal setting.",
            weight=abs(d["error_last_px"] - d["error_first_px"]),
            by_direction=d["by_direction"], error_first_px=d["error_first_px"],
            error_last_px=d["error_last_px"]))
    if guided >= 20 and (fr["saturated_pct"] or 0) > T["saturated_pct"]:
        snr_med = s["snr"]["median"]
        adu = (fr["saturated_pct"] or 0) > 90 and snr_med is not None and \
            snr_med < T["saturated_low_snr"]
        out.append(_f(
            "saturated_star", "warning", "Guide star saturated",
            f"{fr['saturated_pct']:.0f}% of guided frames were flagged STAR_SATURATED "
            f"(gain {_g(st['gain'])}, exposure {_g(st['exposure_ms'])} ms"
            + (f", SNR median {_g(snr_med)}" if snr_med is not None else "") + "). "
            + ("A star this faint does not really saturate: PHD2's saturation level is "
               "set too low (8-bit camera mode, Max ADU 255), so every star reads as "
               "clipped. " if adu else "")
            + "PHD2 still guides, but a clipped star centroids poorly.",
            ("Switch the PHD2 camera to 16-bit (or 'saturation by star profile') "
             "before touching gain or exposure; then re-check. " if adu else "")
            + "Lower the guide exposure (2 to 3 s) or the gain, or pick a fainter star; "
            "in multi-star mode PHD2 picks the brightest, so lower gain is the easier fix.",
            saturated_pct=fr["saturated_pct"], gain=st["gain"],
            exposure_ms=st["exposure_ms"], snr_median=snr_med, adu_limited=adu))
    sr = st["search_region_px"]
    dmax = s["dithers"]["max_px"]
    tot_px = s["rms"]["settled"]["total_px"]
    if sr and ((dmax and sr < T["search_vs_dither"] * dmax) or
               (tot_px and long_enough and sr < T["search_vs_rms"] * tot_px)):
        why = []
        if dmax and sr < T["search_vs_dither"] * dmax:
            why.append(f"largest dither {dmax:.1f} px")
        if tot_px and sr < T["search_vs_rms"] * tot_px:
            why.append(f"settled RMS {tot_px:.1f} px")
        out.append(_f(
            "search_region_small", "warning", "Search region too small",
            f"Search region {sr:g} px against {' and '.join(why)}; a star that moves "
            f"further than the region between frames is lost.",
            "Raise PHD2's search region to 30 to 40 px (Brain > Guiding), or reduce "
            "NINA's dither pixels / dither scale.",
            search_region_px=sr, max_dither_px=dmax, settled_rms_px=tot_px))
    if cal.get("ortho_err_deg") is not None and cal["ortho_err_deg"] > T["ortho_warn_deg"]:
        out.append(_f(
            "calibration_ortho", "warning" if cal["ortho_err_deg"] <= T["ortho_bad_deg"]
            else "critical",
            "Calibration axes not perpendicular",
            f"Calibration in use has an orthogonality error of {cal['ortho_err_deg']:.1f} deg"
            + (f", taken at Dec {_g(cal['dec_deg'])}" if cal.get("dec_deg") is not None else "")
            + (f", HA {cal['hour_angle_hr']:+.2f} h" if cal.get("hour_angle_hr") is not None else "")
            + (f", pier {cal['pier_side']}" if cal.get("pier_side") else "")
            + (f"; PHD2 flagged '{cal['last_cal_issue']}'" if cal.get("last_cal_issue")
               and cal["last_cal_issue"].lower() != "none" else "") + ".",
            "Recalibrate within 20 deg of Dec 0 and within an hour of the meridian, "
            "with the calibration step size set so each axis takes about 12 steps "
            "(PHD2's calibration step calculator).",
            ortho_err_deg=cal["ortho_err_deg"], cal_dec=cal.get("dec_deg")))
    elif cal.get("issues") and cal.get("quality") in ("poor", "fair"):
        out.append(_f(
            "calibration_quality", "info", "Calibration could be better",
            "Calibration in use: " + "; ".join(cal["issues"]) + ".",
            "Recalibrate within 20 deg of Dec 0 near the meridian with about 12 "
            "steps per axis.", issues=cal["issues"]))
    if cal.get("pier_mismatch"):
        out.append(_f(
            "calibration_other_pier", "warning",
            "Guiding on the other pier side from the calibration",
            f"The calibration was taken with the scope on the {cal['pier_side']} side "
            f"and this session guided on the {s['pointing']['pier_side']} side"
            + (" (PHD2 flipped it: RA angle "
               f"{cal['ra_angle_deg']:g}, parity {cal['parity']})" if cal.get("flipped_by_phd2") else
               " and PHD2 did NOT flip it") + ".",
            "Recalibrate on this side of the pier (or after each flip) until the "
            "flip handling is proven; confirm 'Reverse Dec output after meridian "
            "flip' matches the mount.",
            cal_pier=cal["pier_side"], guide_pier=s["pointing"]["pier_side"],
            flipped=cal.get("flipped_by_phd2")))
    if cal.get("dec_offset_deg") is not None and cal["dec_offset_deg"] > T["dec_offset_deg"]:
        out.append(_f(
            "calibration_far_dec", "info", "Guiding far from the calibration Dec",
            f"Guiding at Dec {_g(s['pointing']['dec_deg'])}, calibrated at "
            f"{_g(cal['dec_deg'])} ({_g(cal['dec_offset_deg'])} deg away).",
            "Fine if the calibration is good (PHD2 rescales RA by cos(Dec)); "
            "otherwise recalibrate near this Dec or near Dec 0.",
            dec_offset_deg=cal["dec_offset_deg"]))
    mass_drops = sum(n for k, n in fr["drop_reasons"].items() if "mass" in k.lower())
    if fr["total"] >= 20 and _pct(mass_drops, fr["total"]) and \
            _pct(mass_drops, fr["total"]) > T["mass_drop_pct"]:
        out.append(_f(
            "mass_change_drops", "warning", "Frames lost to 'star mass changed'",
            f"{mass_drops} of {fr['total']} frames ({_pct(mass_drops, fr['total']):.1f}%) "
            f"were dropped as mass changed (tolerance {st['mass_tolerance']}; star-mass "
            f"variation {s['star_mass']['cv']}).",
            "Raise the star mass tolerance to 80 to 100% or turn off star mass "
            "change detection (Brain > Guiding); seeing and clouds swing the "
            "mass of a bright star a lot.",
            mass_drops=mass_drops, mass_cv=s["star_mass"]["cv"]))
    snr_med = s["snr"]["median"]
    low_snr = sum(n for k, n in fr["drop_reasons"].items() if "snr" in k.lower())
    if guided >= 20 and ((snr_med is not None and snr_med < T["low_snr_median"]) or
                         (_pct(low_snr, fr["total"]) or 0) > T["mass_drop_pct"]):
        out.append(_f(
            "low_snr", "warning", "Guide star too faint at times",
            f"SNR median {snr_med}, min {s['snr']['min']}; {low_snr} frames lost to low SNR.",
            "Lengthen the exposure a little or let PHD2 pick a brighter star; "
            "clouds also do this.", snr_median=snr_med, low_snr_drops=low_snr))
    if fr["total"] >= 20 and (fr["dropped_pct"] or 0) > T["drop_pct"]:
        out.append(_f(
            "star_lost_rate", "warning", "Star lost often",
            f"{fr['dropped']} of {fr['total']} frames dropped ({fr['dropped_pct']:.1f}%, "
            f"{fr['drops_per_hour']:.0f}/h): "
            + ", ".join(f"{k} {v}" for k, v in fr["drop_reasons"].items()) + ".",
            "See the mass-change and SNR findings; each drop is a frame with no "
            "correction.", dropped_pct=fr["dropped_pct"]))
    se = s["dithers"]["settle"]
    if (se["completed"] + se["failed"]) >= 3 and (se["success_pct"] or 0) < T["settle_success_pct"]:
        out.append(_f(
            "settle_failures", "warning", "Dithers did not settle",
            f"{se['failed']} of {se['completed'] + se['failed']} settles failed; "
            f"{s['dithers']['recovered']} of {s['dithers']['count']} dithers got back "
            f"to the new lock position.",
            "Fix the guiding first (a mount that ignores pulses can never settle); "
            "then keep dithers at or under a third of the search region and give "
            "NINA's settle time 30 to 60 s.",
            settle=se))
    ep = s["epochs"]
    if (ep["static_minutes"] or 0) >= 5:
        out.append(_f(
            "star_static", "warning", "Guide star sat perfectly still",
            f"For {ep['static_minutes']:g} min (between dithers) the guide star scattered "
            f"under {T['static_std_arcsec']:g}\" (least {ep['min_std_arcsec']:.2f}\"); "
            f"seeing moves a real star 0.3 to 0.6\" here at {scale or 0:.2f}\"/px.",
            "PHD2 may have locked onto a hot pixel or a fixed artifact: look at the "
            "PHD2 star profile, use (or rebuild) the bad-pixel map and a dark library so "
            "auto-select cannot pick one. (It also happens with the roof closed: "
            "PHD2 keeps 'guiding' on a hot pixel.)",
            weight=ep["static_minutes"], static_minutes=ep["static_minutes"],
            min_std_arcsec=ep["min_std_arcsec"]))
    cfg_scale = st.get("config_scale_arcsec")
    if st["scale_source"] == "log" and scale and cfg_scale and \
            abs(scale / cfg_scale - 1) > 0.25:
        out.append(_f(
            "scale_mismatch", "warning", "PHD2 profile pixel scale disagrees with the guide optics",
            f"The guide log says {scale:.2f}\"/px (focal length "
            f"{_g(st['focal_length_mm'])} mm in the PHD2 profile); PhotonScript's guide "
            f"optics give {cfg_scale:.3f}\"/px, so every arcsec figure from this "
            f"session is {scale / cfg_scale:.1f}x off.",
            "Set the PHD2 profile's focal length (Profile > Camera / Guide scope) to "
            "the OAG path, about 3248 mm, or pick the right profile.",
            log_scale=scale, config_scale=cfg_scale))
    if scale and scale < T["oversampled_arcsec_px"]:
        out.append(_f(
            "oversampled", "info", "Guide camera oversampled",
            f"{scale:.2f}\"/px at binning {st['binning'] or 1:g}: seeing noise moves "
            f"the centroid by several pixels, and PHD2 chases it.",
            "Bin the guide camera further if it allows (about 0.5 to 1\"/px is "
            "plenty) or raise the minimum move.", scale=scale))
    if st["have_dark"] is False and not st["defect_map"] and guided:
        out.append(_f(
            "no_dark", "info", "No dark library or defect map",
            "PHD2 guided without a dark or bad-pixel map, so hot pixels can be "
            "taken for stars.",
            "Build a dark library for the guide exposures in use (Tools > Dark "
            "Library), with the camera at its guiding gain."))
    return out


def night_findings(cals, ga_ref=None) -> list[dict]:
    """Findings that belong to the night, not to one session."""
    out = []
    stuck = [c for c in cals if any("barely moved" in i for i in c.get("issues", []))]
    if stuck:
        c0 = max(stuck, key=lambda c: max(c["steps"].values() or [0]))
        good = [c["moved_px"].get("West") for c in cals if c["result"] == "complete"
                and c["moved_px"].get("West")]
        out.append(_f(
            "calibration_no_motion", "critical" if len(stuck) > 1 else "warning",
            "Calibration pulses did not move the star",
            f"{len(stuck)} calibration(s) failed or were abandoned with the star "
            f"standing still (worst: {c0['moved_px'].get('West')} px after "
            f"{c0['steps'].get('West')} West steps at Dec {_g(c0['dec_deg'])}, "
            f"{c0['start_local'][11:16]} local)"
            + (f"; completed calibrations moved it {min(good):g} to {max(good):g} px"
               if good else "") + ".",
            "Same symptom as guide pulses the mount ignores, or PHD2 locked onto a "
            "hot pixel. Run PHD2's Manual Guide on the selected star; if it moves, "
            "build a defect map and dark library so auto-select cannot pick a hot "
            "pixel; if it does not, check TheSky's pulse-guide path (PS-66).",
            calibrations=[c["index"] for c in stuck]))
    return out


def merge_findings(sessions, extra=None) -> list[dict]:
    """One entry per finding id: worst severity, the sessions it hit, the
    guided minutes affected, and the detail from the longest such session."""
    by: dict[str, dict] = {}
    for f in extra or []:
        by[f["id"]] = dict(f, sessions=[], minutes=0.0, _best=0)
    for s in sessions:
        for f in s["findings"]:
            m = by.get(f["id"])
            mins = s["duration_min"] or 0
            if m is None:
                m = dict(f, sessions=[], minutes=0.0, _best=-1)
                by[f["id"]] = m
            if s["index"] in m["sessions"]:
                continue  # same rule on both axes of one session
            m["sessions"].append(s["index"])
            m["minutes"] = round(m["minutes"] + mins, 1)
            if SEVERITY_RANK[f["severity"]] < SEVERITY_RANK[m["severity"]]:
                m["severity"] = f["severity"]
            w = f.get("weight") if f.get("weight") is not None else mins
            if w > m["_best"]:
                m.update(detail=f"{f['detail']} (session {s['index']}, "
                                f"{s['start_local'][11:16]} local)",
                         recommendation=f["recommendation"],
                         evidence=f["evidence"], _best=w)
    out = []
    for m in by.values():
        m.pop("_best", None)
        out.append(m)
    order = {k: i for i, k in enumerate(RULE_ORDER)}
    out.sort(key=lambda m: (SEVERITY_RANK[m["severity"]], order.get(m["id"], 99),
                            -m["minutes"]))
    return out


# --------------------------------------------------------------------------
# Guiding Assistant results
# --------------------------------------------------------------------------

_GA = {
    "ra_drift_arcsec_min": r"RA Drift Rate=\s*-?[\d.]+\s*px/min\s*\(\s*(-?[\d.]+)",
    "dec_drift_arcsec_min": r"Dec Drift Rate=\s*-?[\d.]+\s*px/min\s*\(\s*(-?[\d.]+)",
    "pa_error_arcmin": r"PA Error=\s*(-?[\d.]+)\s*arc-min",
    "backlash_ms": r"Backlash=\s*(-?[\d.]+)\s*\+/-",
    "backlash_arcsec": r"Backlash=.*?\(\s*(-?[\d.]+)\s*\+/-",
    "total_hpf_rms_arcsec": r"Total HPF-RMS=\s*[\d.]+\s*px\s*\(\s*([\d.]+)",
    "ra_peak_to_peak_arcsec": r"RA Peak-Peak\s*[\d.]+\s*px\s*\(\s*([\d.]+)",
    "max_ra_drift_arcsec_s": r"Max RA Drift Rate=\s*[\d.]+\s*px/sec\s*\(\s*([\d.]+)",
    "drift_limiting_exp_s": r"Drift-Limiting Exp=\s*([\d.]+)",
    "snr": r"SNR=\s*([\d.]+)",
}


def ga_results(sections, config) -> list[dict]:
    """PHD2 Guiding Assistant results ('INFO: GA Result - ...') per run."""
    out = []
    for sec in sections:
        run = None
        for e in sec["events"]:
            if e[0] != "ga":
                run = None
                continue
            if run is None:
                i = e[1]
                t = sec["frames"][i - 1]["t"] if 0 < i <= len(sec["frames"]) else 0.0
                loc = sec["start_local"] + timedelta(seconds=t) if sec["start_local"] else None
                run = {"time_local": _iso(loc), "time_utc": _iso_z(to_utc(config, loc)),
                       "file": sec["file"], "recommendations": []}
                out.append(run)
            body = e[2]
            if body.lower().startswith("recommendation"):
                run["recommendations"].append(body.split(":", 1)[-1].strip())
            for k, rx in _GA.items():
                m = re.search(rx, body)
                if m and k not in run:
                    run[k] = float(m.group(1))
    return out


def latest_ga_before(config, before_local: datetime, files=None, limit: int = 12):
    """The newest Guiding Assistant run in older guide logs (a GA is not run
    every night; its unguided drift is the yardstick for the pulse check)."""
    if files is None:
        files = pl.find_logs(config, "guide")["files"]
    older = sorted((p for p in files if (lf.file_start(Path(p).name) or before_local)
                    < before_local), key=lambda p: lf.file_start(Path(p).name), reverse=True)
    older = older[:limit]
    key = (str(before_local),) + tuple(
        (str(p), getattr(_stat(p), "st_mtime", 0)) for p in older)
    if key in _GA_CACHE:
        return _GA_CACHE[key]
    _GA_CACHE.clear()
    _GA_CACHE[key] = found = _latest_ga(config, before_local, older)
    return found


def _stat(p):
    try:
        return Path(p).stat()
    except OSError:
        return None


def _latest_ga(config, before_local, older):
    for p in older:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "GA Result" not in text:
            continue
        runs = [r for r in ga_results(pl.parse_guide_log(text, Path(p).name), config)
                if r["time_local"] and r["time_local"] < _iso(before_local)]
        if runs:
            return runs[-1]
    return None


# --------------------------------------------------------------------------
# per sub
# --------------------------------------------------------------------------

_NAME_TS = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")


def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).strip().rstrip("Z").replace(" ", "T"))
    except ValueError:
        return None


def sub_start_utc(rec: dict, config) -> datetime | None:
    """Exposure start (naive UTC) of a runs record: NINA's file-name local
    start time when present; else date_obs; else 'time', which the backfill
    grader fills with DATE-OBS (start, no Z) and the live agent with the
    processing time (end, ends in Z)."""
    name = Path(str(rec.get("file") or "").replace("\\", "/")).name
    m = _NAME_TS.search(name)
    if m:
        try:
            loc = datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                                    "%Y-%m-%d %H:%M:%S")
            return to_utc(config, loc)
        except ValueError:
            pass
    if rec.get("date_obs"):
        t = _parse_iso(rec["date_obs"])
        if t:
            return t
    t = _parse_iso(rec.get("time") or "")
    if t is None:
        return None
    if str(rec.get("time")).strip().endswith("Z"):
        return t - timedelta(seconds=float(rec.get("exp_s") or 0))
    return t


class GuideTimeline:
    """Every guide frame of a night on one UTC axis, for per-sub stats.
    PS-21 can call stats(start_utc, exp_s) for the true during-the-sub RMS."""

    def __init__(self, sessions_raw, config):
        self.frames, self.spans = [], []
        for sec, scale in sessions_raw:
            st = to_utc(config, sec["start_local"])
            if st is None:
                continue
            last = sec["frames"][-1]["t"] if sec["frames"] else 0.0
            en = to_utc(config, sec["end_local"]) if sec["end_local"] and \
                sec["closed"] in ("ended", "aborted") else None
            self.spans.append((st, max(en or st, st + timedelta(seconds=last))))
            for f in sec["frames"]:
                self.frames.append((st + timedelta(seconds=f["t"]), f, scale,
                                    id(sec)))
        self.frames.sort(key=lambda x: x[0])
        self._keys = [x[0] for x in self.frames]

    def stats(self, start_utc: datetime, exp_s: float) -> dict:
        end = start_utc + timedelta(seconds=float(exp_s or 0))
        cover = sum(max(0.0, (min(b, end) - max(a, start_utc)).total_seconds())
                    for a, b in self.spans)
        frac = cover / exp_s if exp_s else 0.0
        lo, hi = bisect.bisect_left(self._keys, start_utc), bisect.bisect_right(self._keys, end)
        win = self.frames[lo:hi]
        g = [(f, k) for _, f, k, _ in win if not f["drop"]]
        ra = [f["ra"] * k for f, k in g if k]
        dec = [f["dec"] * k for f, k in g if k]
        out = {"state": "unguided" if frac < 0.05 else "partial" if frac < 0.9 else "guided",
               "guided_pct": _r(100 * min(frac, 1.0), 0),
               "frames": len(g), "dropped": sum(1 for _, f, _, _ in win if f["drop"]),
               "settling_frames": sum(1 for f, _ in g if f["settling"]),
               "lock_changes": len({(sid, f["epoch"]) for _, f, _, sid in win}) - 1 if win else 0,
               "max_pulses": sum(1 for f, _ in g if f["ra_ms"] >= 2499 or f["dec_ms"] >= 2499),
               "snr_median": _r(statistics.median([f["snr"] for f, _ in g
                                                   if f["snr"] is not None]), 1)
               if any(f["snr"] is not None for f, _ in g) else None}
        if ra:
            rr, rd = _rms(ra), _rms(dec)
            out.update(rms_ra_arcsec=_r(rr, 2), rms_dec_arcsec=_r(rd, 2),
                       rms_total_arcsec=_r(math.hypot(rr, rd), 2),
                       std_total_arcsec=_r(math.hypot(_std(ra), _std(dec)), 2),
                       peak_arcsec=_r(max(math.hypot(a, b) for a, b in zip(ra, dec)), 2))
        else:
            out.update(rms_ra_arcsec=None, rms_dec_arcsec=None, rms_total_arcsec=None,
                       std_total_arcsec=None, peak_arcsec=None)
        return out


def sub_guide_stats(timeline: GuideTimeline, subs: list[dict], config) -> dict:
    rows = []
    for r in subs:
        st = sub_start_utc(r, config)
        if st is None or not r.get("exp_s"):
            continue
        g = timeline.stats(st, float(r["exp_s"]))
        rows.append({"file": r.get("file"), "rig": r.get("rig"), "target": r.get("target"),
                     "filter": r.get("filter"), "exp_s": r.get("exp_s"),
                     "start_utc": _iso_z(st), "passed_qa": r.get("passed_qa"),
                     "guide_rms_snapshot": r.get("guide_rms"), **g})

    def med(xs):
        xs = [x for x in xs if x is not None]
        return _r(statistics.median(xs), 2) if xs else None
    acc = [x for x in rows if x["passed_qa"]]
    rej = [x for x in rows if x["passed_qa"] is False]
    summary = {"subs": len(rows),
               "guided": sum(1 for x in rows if x["state"] == "guided"),
               "partial": sum(1 for x in rows if x["state"] == "partial"),
               "unguided": sum(1 for x in rows if x["state"] == "unguided"),
               "median_rms_accepted_arcsec": med([x["rms_total_arcsec"] for x in acc]),
               "median_rms_rejected_arcsec": med([x["rms_total_arcsec"] for x in rej]),
               # motion within the sub (what blurs it); a steady offset from
               # the lock position inflates the RMS above but not this
               "median_std_accepted_arcsec": med([x["std_total_arcsec"] for x in acc]),
               "median_std_rejected_arcsec": med([x["std_total_arcsec"] for x in rej]),
               "with_drops": sum(1 for x in rows if x["dropped"]),
               # star held still but far from the lock: PHD2 corrected and
               # nothing moved (pulses not acting, or a hot pixel)
               "static_offset_subs": sum(1 for x in rows if x["state"] == "guided"
                                         and (x["std_total_arcsec"] or 9) < 0.3
                                         and (x["rms_total_arcsec"] or 0) > 3),
               "with_settling": sum(1 for x in rows if x["settling_frames"])}
    by_rig: dict[str, dict] = {}
    for x in rows:
        b = by_rig.setdefault(x["rig"] or "?", {"subs": 0, "rms": [], "std": []})
        b["subs"] += 1
        b["rms"].append(x["rms_total_arcsec"])
        b["std"].append(x["std_total_arcsec"])
    summary["by_rig"] = {k: {"subs": v["subs"], "median_rms_arcsec": med(v["rms"]),
                             "median_std_arcsec": med(v["std"])}
                         for k, v in by_rig.items()}
    # the OAG sees through the RC16's filter: star loss / SNR by that filter
    # (does the guide star fade behind the narrowband filters?)
    by_f: dict[str, dict] = {}
    for x in rows:
        if (x["rig"] or "rc16") != "rc16" or x["state"] == "unguided":
            continue
        b = by_f.setdefault(str(x.get("filter") or "?"),
                            {"subs": 0, "frames": 0, "dropped": 0, "snr": []})
        b["subs"] += 1
        b["frames"] += x["frames"] + x["dropped"]
        b["dropped"] += x["dropped"]
        b["snr"].append(x["snr_median"])
    summary["rc16_by_filter"] = {
        k: {"subs": v["subs"], "dropped_pct": _pct(v["dropped"], v["frames"]),
            "median_snr": med(v["snr"])} for k, v in sorted(by_f.items())}
    return {"summary": summary, "subs": rows}


# --------------------------------------------------------------------------
# the night
# --------------------------------------------------------------------------

_CACHE: dict = {}      # parsed sections per set of files
_RESULTS: dict = {}    # finished analyses (the runs page asks often)
_GA_CACHE: dict = {}   # Guiding Assistant lookback per set of older files


def _load(config, date, file):
    """Parsed sections for the night, cached on the files' size + mtime."""
    found = pl.find_logs(config, "guide")
    paths, note = pl.select(found["files"], date=date, file=file)
    if not paths:
        return None, {"ok": False, "note": note or "no PHD2 guide logs found",
                      "searched": found["searched"], "date": date or None}
    key = []
    for p in paths:
        try:
            s = Path(p).stat()
            key.append((str(p), s.st_mtime, s.st_size))
        except OSError:
            key.append((str(p), 0, 0))
    key = tuple(key)
    secs = _CACHE.get(key)
    if secs is None:
        secs = []
        for p in paths:
            secs += pl.parse_guide_log(
                Path(p).read_text(encoding="utf-8", errors="replace"), Path(p).name)
        if len(_CACHE) > 6:
            _CACHE.clear()
        _CACHE[key] = secs
    return secs, {"ok": True, "date": date or None, "dir": found["dir"],
                  "files": [lf.describe(p) for p in paths], "_key": key,
                  "_all": found["files"]}


def analyze_sections(sections, config, date: str = "", subs=None,
                     ga_before=None) -> dict:
    """The full analysis of already-parsed sections (tests call this)."""
    window = None
    if date:
        try:
            window = lf.night_window(date)
        except ValueError:
            window = None

    def inside(sec):
        t = sec["start_local"]
        return window is None or t is None or window[0] <= t < window[1]

    cal_all = []
    for i, sec in enumerate(s for s in sections if s["kind"] == "calibration"):
        c = _cal_record(i, sec, config)
        c["_start"] = sec["start_local"]
        c["_inside"] = inside(sec)
        cal_all.append(c)
    sessions, raw = [], []
    for sec in sections:
        if sec["kind"] != "guiding" or not inside(sec) or not sec["frames"]:
            continue
        s = analyze_session(len(sessions) + 1, sec, cal_all, config)
        sessions.append(s)
        raw.append((sec, pl._scale_for(sec["header"], config)[0]))
    used = {s["calibration"]["index"] for s in sessions}
    cals = []
    for c in cal_all:
        if c.pop("_inside") or c["index"] in used:
            c.pop("_start", None)
            c["used_by_sessions"] = [s["index"] for s in sessions
                                     if s["calibration"]["index"] == c["index"]]
            cals.append(c)
    # night totals
    g_ra, g_dec, s_ra, s_dec = [], [], [], []
    reasons: dict[str, int] = {}
    for sec, k in raw:
        for f in sec["frames"]:
            if f["drop"]:
                reasons[f["reason"]] = reasons.get(f["reason"], 0) + 1
            elif k:
                g_ra.append(f["ra"] * k)
                g_dec.append(f["dec"] * k)
                if not f["settling"]:
                    s_ra.append(f["ra"] * k)
                    s_dec.append(f["dec"] * k)

    def tot(key, sub=None):
        return sum((s[key][sub] if sub else s[key]) or 0 for s in sessions)
    frames_total = sum(s["frames"]["total"] for s in sessions)
    guided = sum(s["frames"]["guided"] for s in sessions)
    sat = sum(s["frames"]["saturated"] for s in sessions)
    drops = sum(s["frames"]["dropped"] for s in sessions)
    settle = {k: sum(s["dithers"]["settle"][k] for s in sessions)
              for k in ("started", "completed", "failed", "unfinished")}
    settle["success_pct"] = _pct(settle["completed"], settle["completed"] + settle["failed"])
    rra, rdec = _rms(g_ra), _rms(g_dec)
    srr, srd = _rms(s_ra), _rms(s_dec)
    totals = {
        "guiding_sessions": len(sessions), "calibrations": len(cals),
        "guided_minutes": _r(tot("duration_min"), 1),
        "frames": frames_total, "guided_frames": guided,
        "saturated_frames": sat, "saturated_pct": _pct(sat, guided),
        "dropped_frames": drops, "dropped_pct": _pct(drops, frames_total),
        "drop_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "rms_ra_arcsec": _r(rra), "rms_dec_arcsec": _r(rdec),
        "rms_total_arcsec": _r(math.hypot(rra, rdec)) if rra is not None else None,
        "settled_rms_total_arcsec": _r(math.hypot(srr, srd)) if srr is not None else None,
        "dithers": sum(s["dithers"]["count"] for s in sessions),
        "dithers_recovered": sum(s["dithers"]["recovered"] for s in sessions),
        "max_dither_px": max((s["dithers"]["max_px"] or 0 for s in sessions), default=None),
        "settle": settle,
    }
    ga = [g for g in ga_results([s for s in sections if inside(s)], config)]
    ga_ref = ga[-1] if ga else ga_before
    if ga_ref and ga_ref.get("ra_drift_arcsec_min") is not None:
        note = (f" The Guiding Assistant ({ga_ref['time_local'][:16]}) measured "
                f"unguided drift RA {ga_ref['ra_drift_arcsec_min']:+.2f}\"/min, Dec "
                f"{ga_ref.get('dec_drift_arcsec_min', 0):+.2f}\"/min.")
        for s in sessions:
            for f in s["findings"]:
                if f["id"] == "pulses_not_moving":
                    f["detail"] = f["detail"].replace(
                        "far beyond this mount's unguided drift.",
                        "far beyond this mount's unguided drift." + note)
    findings = merge_findings(sessions, night_findings(cals, ga_ref))
    out = {"ok": True, "date": date or None, "totals": totals,
           "findings": findings, "calibrations": cals, "sessions": sessions,
           "guiding_assistant": {"this_night": ga, "reference": ga_ref},
           "thresholds": T,
           "notes": ["times: PHD2 logs the scope PC's local clock; *_utc fields "
                     f"use {getattr(config, 'observatory_tz', '?')}",
                     "rms 'all' and 'settled' are about the lock position (what "
                     "the camera sees, offsets included); std_* are PHD2-style "
                     "standard deviations",
                     "ErrorCode 1 (STAR_SATURATED) frames are guided frames"]}
    if subs is not None:
        out["per_sub"] = sub_guide_stats(GuideTimeline(raw, config), subs, config)
    return out


def night_analysis(config, date: str = "", file: str = "", with_subs: bool = True) -> dict:
    """GET /api/phd2/analysis."""
    secs, info = _load(config, date, file)
    if secs is None:
        return info
    key, files = info.pop("_key"), info.pop("_all")
    subs = None
    if with_subs and date:
        try:
            from photonscript.scheduler.runs import _load_subs
            subs = _load_subs(config, date)
        except Exception:  # noqa: BLE001 - no runs records is not an error
            subs = []
    ga_before = None
    if not any(e[0] == "ga" for s in secs for e in s["events"]):
        try:
            first = min((s["start_local"] for s in secs if s["start_local"]),
                        default=None)
            if first is not None:
                ga_before = latest_ga_before(config, first, files)
        except Exception:  # noqa: BLE001 - the yardstick is optional
            ga_before = None
    rkey = (key, date, file, with_subs, len(subs or []),
            max((str(r.get("time") or "") for r in subs or []), default=""))
    if rkey in _RESULTS:
        return _RESULTS[rkey]
    out = analyze_sections(secs, config, date=date, subs=subs, ga_before=ga_before)
    out.update(info)
    if len(_RESULTS) > 8:
        _RESULTS.clear()
    _RESULTS[rkey] = out
    return out


def night_timeline(config, date: str) -> GuideTimeline | None:
    """The night's frames on a UTC axis (for PS-21's during-the-sub RMS)."""
    secs, _info = _load(config, date, "")
    if secs is None:
        return None
    try:
        w = lf.night_window(date)
    except ValueError:
        return None
    raw = [(s, pl._scale_for(s["header"], config)[0]) for s in secs
           if s["kind"] == "guiding" and s["frames"] and s["start_local"]
           and w[0] <= s["start_local"] < w[1]]
    return GuideTimeline(raw, config)


def compact(a: dict, n: int = 4) -> dict:
    """The guiding block for the night report (/api/runs/<date>)."""
    if not a.get("ok"):
        return {"ok": False, "note": a.get("note")}
    t = a["totals"]
    return {"ok": True, "sessions": t["guiding_sessions"],
            "guided_minutes": t["guided_minutes"],
            "rms_total_arcsec": t["rms_total_arcsec"],
            "settled_rms_total_arcsec": t["settled_rms_total_arcsec"],
            "dropped_pct": t["dropped_pct"], "saturated_pct": t["saturated_pct"],
            "dithers": t["dithers"], "dithers_recovered": t["dithers_recovered"],
            "top_findings": [{"severity": f["severity"], "title": f["title"],
                              "detail": f["detail"],
                              "recommendation": f["recommendation"],
                              "sessions": f["sessions"]} for f in a["findings"][:n]],
            "detail_url": f"/api/phd2/analysis?date={a.get('date') or ''}"}


def format_report(a: dict) -> str:
    """Readable text for `photonscript guiding-report`."""
    if not a.get("ok"):
        return f"No PHD2 analysis: {a.get('note')}"
    t = a["totals"]
    L = [f"PHD2 guiding report, night of {a.get('date') or '?'}  "
         f"({', '.join(f['file'] for f in a.get('files', []))})", ""]
    L.append(f"{t['guiding_sessions']} guiding sessions, {t['guided_minutes']} min, "
             f"{t['calibrations']} calibrations")
    L.append(f"Frames: {t['guided_frames']} guided ({t['saturated_pct']}% saturated), "
             f"{t['dropped_frames']} dropped ({t['dropped_pct']}%): "
             + ", ".join(f"{k} {v}" for k, v in t["drop_reasons"].items()))
    L.append(f"RMS about lock: RA {t['rms_ra_arcsec']}\"  Dec {t['rms_dec_arcsec']}\"  "
             f"total {t['rms_total_arcsec']}\" (settled {t['settled_rms_total_arcsec']}\")")
    se = t["settle"]
    L.append(f"Dithers {t['dithers']} (largest {t['max_dither_px']} px), back on lock "
             f"{t['dithers_recovered']}; settles {se['completed']} ok / {se['failed']} "
             f"failed / {se['unfinished']} unfinished")
    L += ["", "FINDINGS"]
    for i, f in enumerate(a["findings"], 1):
        where = (f"sessions {','.join(map(str, f['sessions']))}; {f['minutes']:g} min"
                 if f["sessions"] else "night")
        L.append(f"{i}. [{f['severity'].upper()}] {f['title']}  ({where})")
        L.append(f"   {f['detail']}")
        L.append(f"   Fix: {f['recommendation']}")
    L += ["", "CALIBRATIONS"]
    for c in a["calibrations"]:
        L.append(f"  #{c['index']} {c['start_local']}  {c['result']:8s} pier {c['pier_side']}"
                 f"  Dec {c['dec_deg']}  HA {c['hour_angle_hr']}  ortho {c['ortho_err_deg']}"
                 f"  RA {c['ra']['rate_px_s']} / Dec {c['dec']['rate_px_s']} px/s"
                 f"  steps {c['steps'].get('West')}/{c['steps'].get('North')}"
                 f"  [{c['quality']}] used by {c.get('used_by_sessions') or '-'}")
    L += ["", "SESSIONS  (local start, min, pier, Dec, RMS\" all/settled, "
              "RA & Dec response, max-pulse %, drops)"]
    for s in a["sessions"]:
        ra, de = s["corrections"]["ra"], s["corrections"]["dec"]
        L.append(f"  {s['index']:2d} {s['start_local'][5:16]} {s['duration_min']:6.1f}  "
                 f"{(s['pointing']['pier_side'] or '?')[:4]:4s} {s['pointing']['dec_deg']!s:>5}  "
                 f"{s['rms']['all'].get('total_arcsec')!s:>6}/{s['rms']['settled'].get('total_arcsec')!s:<6} "
                 f"{ra.get('response_verdict', '-'):>12}/{de.get('response_verdict', '-'):<12} "
                 f"{ra['at_max_pct'] or 0:4.0f}/{de['at_max_pct'] or 0:<4.0f} "
                 f"{s['frames']['dropped']}")
    ps = a.get("per_sub")
    if ps:
        sm = ps["summary"]
        L += ["", f"SUBS: {sm['subs']} ({sm['guided']} guided, {sm['partial']} partial, "
                  f"{sm['unguided']} unguided)",
              f"  during-sub RMS about lock, median: accepted "
              f"{sm['median_rms_accepted_arcsec']}\" vs rejected {sm['median_rms_rejected_arcsec']}\"",
              f"  during-sub motion (std), median: accepted "
              f"{sm['median_std_accepted_arcsec']}\" vs rejected {sm['median_std_rejected_arcsec']}\""]
        for rig, v in sm["by_rig"].items():
            L.append(f"  {rig}: {v['subs']} subs, median RMS {v['median_rms_arcsec']}\", "
                     f"motion {v['median_std_arcsec']}\"")
        if sm.get("rc16_by_filter"):
            L.append("  guide star by RC16 filter (OAG): " + "; ".join(
                f"{k} {v['subs']} subs, {v['dropped_pct']}% frames lost, SNR {v['median_snr']}"
                for k, v in sm["rc16_by_filter"].items()))
    return "\n".join(L)
