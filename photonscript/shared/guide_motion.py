"""Guide-star motion math shared by the offline guide-log analysis (PS-88),
the live non-star lock guard (PS-91) and the pulse-path self-test (PS-92).

Frames are dicts in the phd2_logs._frame shape (raw distances in guide-camera
PIXELS): t (s), ra, dec, ra_ms, ra_dir, dec_ms, dec_dir (E/W/N/S), drop,
output (guide output on), settling, epoch (lock epoch: bumped by a dither or
a new lock position). The live PHD2 client's ring buffer keeps the same keys,
so one set of numbers judges a log the next morning and the sky right now.

Pure: no I/O, no PHD2, no NINA.
"""
from __future__ import annotations

import math
import statistics

# A real star through seeing scatters 0.3 to 0.6" at AARO; a hot pixel or a
# fixed artifact sits still. Static = scatter under STATIC_STD_ARCSEC within
# one lock epoch held for at least STATIC_MIN_S (60+ guided frames).
STATIC_STD_ARCSEC = 0.15
STATIC_MIN_S = 300.0
STATIC_MIN_FRAMES = 60
EPOCH_MIN_FRAMES = 20
# Commanded vs observed correction (per axis)
CMD_RATE_ARCSEC_MIN = 10.0     # commanded correction worth judging
RESPONSE_MIN = 0.25            # observed / commanded below this = no response
MIN_SESSION_S = 120.0          # shorter windows get no response verdict

# The thresholds above, under the names the PS-88 report quotes.
T = {
    "cmd_rate_arcsec_min": CMD_RATE_ARCSEC_MIN,
    "response_min": RESPONSE_MIN,
    "min_session_s": MIN_SESSION_S,
    "static_std_arcsec": STATIC_STD_ARCSEC,
    "static_min_s": STATIC_MIN_S,
}

SIDEREAL_ARCSEC_S = 15.041     # sidereal rate, arcsec per second of time


def _r(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) and math.isfinite(v) else None


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def slope(points):
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


def axis_stats(frames, key, ms_key, dir_key, rate_px_s, max_ms, scale):
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
    sl = slope(pts)
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
        if abs(cmd) >= CMD_RATE_ARCSEC_MIN and span >= MIN_SESSION_S:
            resp = obs / cmd
            out["response"] = _r(resp, 2)
            out["response_verdict"] = (
                "reversed" if resp < -RESPONSE_MIN else
                "not moving" if resp < RESPONSE_MIN else
                "weak" if resp < 0.5 else "moving")
        else:
            out["response_verdict"] = "small demand"
    return out


def response(frames, ra_rate_px_s, dec_rate_px_s, scale, max_ra_ms=None,
             max_dec_ms=None) -> dict:
    """Both axes of axis_stats over one window: {"ra": ..., "dec": ...}.
    The live guard (PS-91 D4) and the passive post-flip check (PS-92) call
    this on the PHD2 client's ring buffer."""
    return {"ra": axis_stats(frames, "ra", "ra_ms", "ra_dir", ra_rate_px_s,
                             max_ra_ms, scale),
            "dec": axis_stats(frames, "dec", "dec_ms", "dec_dir", dec_rate_px_s,
                              max_dec_ms, scale)}


def epoch_scatter(guided, scale) -> list[dict]:
    """Per lock epoch (between dithers, settling frames left out): frames,
    span (s), scatter (arcsec, None without a scale) and whether it counts as
    a static 'star'. Epochs under EPOCH_MIN_FRAMES frames are skipped."""
    ep: dict[int, list] = {}
    for f in guided:
        if not f["settling"]:
            ep.setdefault(f["epoch"], []).append(f)
    out = []
    for k, fs in ep.items():
        if len(fs) < EPOCH_MIN_FRAMES:
            continue
        v = statistics.pvariance([f["ra"] for f in fs]) + \
            statistics.pvariance([f["dec"] for f in fs])
        sd = math.sqrt(v) * (scale or 0)
        span = fs[-1]["t"] - fs[0]["t"]
        out.append({"epoch": k, "n": len(fs), "var_px2": v, "span_s": span,
                    "std_arcsec": sd if scale else None,
                    "static": bool(scale and len(fs) >= STATIC_MIN_FRAMES
                                   and span >= STATIC_MIN_S
                                   and sd < STATIC_STD_ARCSEC)})
    return out


def epochs(guided, scale) -> dict:
    """Scatter of the star within each lock epoch (between dithers): a real
    star through seeing scatters 0.3 to 0.6\" here; a hot pixel or a fixed
    artifact sits still."""
    ep = {f["epoch"] for f in guided if not f["settling"]}
    rows = epoch_scatter(guided, scale)
    num = sum(r["var_px2"] * r["n"] for r in rows)
    den = sum(r["n"] for r in rows)
    stds = [math.sqrt(r["var_px2"]) * (scale or 0) for r in rows]
    static_s = sum(r["span_s"] for r in rows if r["static"])
    return {"count": len(ep),
            "pooled_std_arcsec": _r(math.sqrt(num / den) * scale, 3) if den and scale else None,
            "min_std_arcsec": _r(min(stds), 3) if stds else None,
            "static_minutes": _r(static_s / 60, 1)}


# --------------------------------------------------------------------------
# PS-92 pulse self-test helpers
# --------------------------------------------------------------------------

def expected_px(speed_arcsec_s, dec_deg, ms, scale, axis) -> float | None:
    """Pixels a guide pulse should move the star: guide speed ("/s) x
    duration, x cos(Dec) on RA, over the guide pixel scale."""
    if not speed_arcsec_s or not scale or ms is None:
        return None
    k = math.cos(math.radians(dec_deg or 0.0)) if axis == "ra" else 1.0
    return float(speed_arcsec_s) * abs(k) * float(ms) / 1000.0 / float(scale)


def pulse_ms_for(step_px, speed_arcsec_s, dec_deg, scale, axis,
                 lo=100, hi=2000) -> int:
    """Pulse length that moves the star about step_px, clamped to lo..hi."""
    per_ms = expected_px(speed_arcsec_s, dec_deg, 1.0, scale, axis)
    if not per_ms:
        return hi
    return int(max(lo, min(hi, round(step_px / per_ms))))


def _median3(a):
    from scipy import ndimage
    return ndimage.median_filter(a, size=3)


def register_shift(a, b, mask=None) -> tuple[float, float, float]:
    """(dx, dy, peak) shift of frame b relative to frame a, in pixels, by FFT
    phase correlation on 3x3-median-filtered frames (hot pixels, single-pixel
    noise and the masked pixels cannot dominate), with a sub-pixel parabolic
    peak. `mask` is a boolean array of pixels to ignore (the PS-91 hot-pixel
    map); masked pixels are set to the frame median. A static field gives
    (0, 0); a star field moved by +3 px in x gives dx about +3."""
    import numpy as np
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape != b.shape or a.ndim != 2:
        raise ValueError("frames must be 2-D and the same shape")
    fa, fb = _median3(a), _median3(b)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        fa = np.where(m, np.median(fa), fa)
        fb = np.where(m, np.median(fb), fb)
    fa = fa - np.median(fa)
    fb = fb - np.median(fb)
    # Hann window: the frame edge must not correlate with itself
    wy = np.hanning(a.shape[0])[:, None]
    wx = np.hanning(a.shape[1])[None, :]
    A = np.fft.fft2(fa * wy * wx)
    B = np.fft.fft2(fb * wy * wx)
    R = B * np.conj(A)
    R /= np.maximum(np.abs(R), 1e-12)
    c = np.fft.ifft2(R).real
    iy, ix = np.unravel_index(int(np.argmax(c)), c.shape)
    peak = float(c[iy, ix])
    ny, nx = c.shape

    def sub(cm, c0, cp):
        d = cm - 2 * c0 + cp
        return 0.0 if d == 0 else 0.5 * (cm - cp) / d

    dy = iy + sub(c[(iy - 1) % ny, ix], c[iy, ix], c[(iy + 1) % ny, ix])
    dx = ix + sub(c[iy, (ix - 1) % nx], c[iy, ix], c[iy, (ix + 1) % nx])
    if dy > ny / 2:
        dy -= ny
    if dx > nx / 2:
        dx -= nx
    return float(dx), float(dy), peak


def _angle(v) -> float:
    return math.degrees(math.atan2(v[1], v[0]))


def _ang_diff(a, b) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def selftest_verdict(steps, ratio_min=0.5, ratio_max=1.5) -> dict:
    """Judge the pulse self-test. steps: [{"dir": "W"|"E"|"N"|"S",
    "dx", "dy", "expected_px"}] (one per pulse, measured separately).

    Per direction: median observed / expected ratio and mean motion vector.
    FAIL when any direction moves under ratio_min of expected or a pair
    (W/E, N/S) does not oppose (angle between them under 135 deg); WARN when
    any ratio is above ratio_max (guide-rate mismatch, PS-89); PASS otherwise.
    INCONCLUSIVE with no usable steps. Return-to-start and the W/E vs N/S
    angle are recorded, not judged."""
    by: dict[str, list] = {}
    for s in steps or []:
        if s.get("expected_px") and s.get("dx") is not None:
            by.setdefault(s["dir"], []).append(s)
    if not by:
        return {"verdict": "INCONCLUSIVE", "reasons": ["no measured pulses"],
                "directions": {}}
    dirs = {}
    for d, ss in by.items():
        mx = statistics.fmean(s["dx"] for s in ss)
        my = statistics.fmean(s["dy"] for s in ss)
        ratios = [math.hypot(s["dx"], s["dy"]) / s["expected_px"] for s in ss]
        dirs[d] = {"n": len(ss), "ratio": _r(statistics.median(ratios), 2),
                   "moved_px": _r(math.hypot(mx, my), 2),
                   "expected_px": _r(statistics.fmean(s["expected_px"] for s in ss), 2),
                   "angle_deg": _r(_angle((mx, my)), 1), "_v": (mx, my)}
    reasons, warn = [], []
    for d, v in dirs.items():
        if v["ratio"] is not None and v["ratio"] < ratio_min:
            reasons.append(f"{d} pulses moved the star {v['ratio']:.2f}x of "
                           f"expected (under {ratio_min:g})")
        elif v["ratio"] is not None and v["ratio"] > ratio_max:
            warn.append(f"{d} pulses moved the star {v['ratio']:.2f}x of "
                        f"expected (over {ratio_max:g}: guide rate mismatch)")
    pairs = {}
    for a, b in (("W", "E"), ("N", "S")):
        if a in dirs and b in dirs:
            va, vb = dirs[a]["_v"], dirs[b]["_v"]
            moving = (dirs[a]["ratio"] or 0) >= ratio_min and \
                (dirs[b]["ratio"] or 0) >= ratio_min
            sep = _ang_diff(_angle(va), _angle(vb))
            ret = math.hypot(va[0] * dirs[a]["n"] + vb[0] * dirs[b]["n"],
                             va[1] * dirs[a]["n"] + vb[1] * dirs[b]["n"])
            oppose = sep >= 135.0
            pairs[a + b] = {"angle_between_deg": _r(sep, 1), "oppose": oppose,
                            "return_residual_px": _r(ret, 2)}
            if moving and not oppose:
                reasons.append(f"{a} and {b} pulses do not oppose "
                               f"({sep:.0f} deg apart)")
    if "W" in dirs and "N" in dirs:
        pairs["axes_angle_deg"] = _r(_ang_diff(dirs["W"]["angle_deg"],
                                               dirs["N"]["angle_deg"]), 1)
    for v in dirs.values():
        v.pop("_v", None)
    verdict = "FAIL" if reasons else ("WARN" if warn else "PASS")
    return {"verdict": verdict, "reasons": reasons + warn,
            "directions": dirs, "pairs": pairs}
