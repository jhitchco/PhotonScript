"""Nightly tracking drift from the PHD2 guide log (PS-168) and the drift-capped
unguided sub length (PS-169).

2026-10-07: PHD2 measured the guide star all night but sent 0 pulses (the
stale TheSky driver, PS-167), so that night's guide log is an unguided
tracking record: per guiding session the RA / Dec raw distance walks at
about 0.4"/min with about 1.4" RMS of wobble on top. On the RC16 (0.236"/px)
a 300 s sub then smears about 2.5" (ecc 0.55 to 0.62; half the 300 s L was
rejected) while 30 s is fine. This module turns any night's guide log into
that number.

Per guiding session (a PHD2 "Guiding Begins" block, frames from
phd2_logs.parse_guide_log, raw distances in guide px), in windows of
tracking_drift_window_min (15; a 160 min session on 10-06 is not one line):

  * mode unguided-raw: no guide pulse in the session, so the raw distance
    IS the star's drift (plus seeing).
  * mode reconstructed: pulses were sent, so the raw distance is the
    post-correction error. The star's path is rebuilt as raw distance +
    the cumulative correction already applied (RADuration / DECDuration ms
    x the header "RA Guide Speed" / "Dec Guide Speed" ("/s, x cos(Dec) on
    RA), signed by the direction PHD2 uses for a positive error). A second
    estimate uses only the frame pairs whose first frame sent no pulse on
    that axis (zero-pulse intervals) and is reported next to it.
    Caveat: pulses the mount driver silently refused (PS-167) are counted
    as applied, which overstates the drift; PS-167 pages that night.

  * drift: least-squares slope pooled within lock epochs (a dither or a new
    lock position starts an epoch; guide_motion.slope), "/min per axis.
  * wobble: RMS of the residual after the per-epoch offset and that slope.
  * excluded: too short (tracking_drift_min_session_min), or wobble over
    tracking_drift_max_rms_arcsec (star lost and re-found on another star,
    jumps, clouds), or no pixel scale / guide rate.

Night summary: median |RA| and |Dec| drift over the clean sessions, the
total rate (their hypot), the median wobble, the linear smear of a 30 / 60
/ 120 / 300 s sub in arcsec and in pixels at each rig's image scale, and
the PS-169 recommended unguided sub length per rig. Trend: the same over
the last tracking_drift_trend_nights nights, each keyed by the TPoint model
in force (date and point count from the PS-138 audit / manual record).

PS-169: max unguided sub = tracking_smear_budget_px x rig scale / drift
rate ("/min), from tonight's sessions that ended within
tracking_drift_recent_min, else last night's summary; never above the
existing cap, never below tracking_sub_floor_s, rounded down to a dark
length the night quota fills (calibration.night_dark_exposures, PS-160) so
darks match. tracking_drift_cap_mode: off | observe (default: shown on the
drift API, the System page and the fallback push; nothing changes) | auto
(the PS-156 fallback caps its unguided RC16 subs with it).

Read-only: parses logs, writes nothing.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import phd2_logs as pl
from photonscript.shared import guide_motion as gm

logger = logging.getLogger(__name__)

SUB_LENGTHS_S = (30, 60, 120, 300)
MIN_FRAMES = 20
RAW, RECON, MIXED = "unguided-raw", "reconstructed", "mixed"
CAP_MODES = ("off", "observe", "auto")
_CACHE: dict = {}
_CACHE_MAX = 64
DARKS_TTL_S = 600.0
_darks_cache: dict = {}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _f(config, key, default):
    try:
        v = float(getattr(config, key, default))
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def max_rms_arcsec(config) -> float:
    return _f(config, "tracking_drift_max_rms_arcsec", 5.0)


def min_session_min(config) -> float:
    return _f(config, "tracking_drift_min_session_min", 5.0)


def cap_mode(config) -> str:
    m = str(getattr(config, "tracking_drift_cap_mode", "observe") or "observe").strip().lower()
    return m if m in CAP_MODES else "observe"


def rig_scales(config) -> dict[str, float]:
    """{rig: image scale "/px} of every enabled rig (all ride the mount)."""
    from photonscript.shared.rigs import PIGGYBACK, RC16, rig_ids
    out = {}
    for rig in rig_ids(config):
        key = "piggyback_pixel_scale_arcsec" if rig == PIGGYBACK else "pixel_scale_arcsec"
        s = _f(config, key, 1.29 if rig == PIGGYBACK else 0.236)
        if s > 0:
            out[rig] = s
    out.setdefault(RC16, 0.236)
    return out


# --------------------------------------------------------------------------
# one session
# --------------------------------------------------------------------------

def _r(v, n=3):
    return round(v, n) if isinstance(v, (int, float)) and math.isfinite(v) else None


def guide_scale(h: dict, config=None) -> tuple[float | None, str | None]:
    """Guide "/px. The header's "Pixel scale" is rounded to 0.01 (0.25 for
    the OAG at bin 2); when the header also gives the pixel size, binning
    and focal length, 206.265 x um x bin / mm (0.254) is used instead if it
    agrees within 10 %."""
    s, src = pl._scale_for(h, config)
    um, b, fl = h.get("pixel_size_um"), h.get("binning") or 1, h.get("focal_length_mm")
    if um and fl and fl > 0:
        fine = 206.265 * float(um) * float(b) / float(fl)
        if s is None or abs(fine - s) <= 0.1 * s:
            return fine, "log (pixel size x binning / focal length)"
    return s, src


def _pulse_rates(h: dict, scale: float) -> tuple[float | None, float | None, str]:
    """px/s one pulse moves the star, RA and Dec: the header guide speeds
    ("/s, x cos(Dec) on RA when the header knows the Dec), else PHD2's
    calibration rates (xRate / yRate, measured at the calibration Dec)."""
    ras, decs, dec = h.get("ra_guide_speed"), h.get("dec_guide_speed"), h.get("dec_deg")
    xr, yr = h.get("x_rate"), h.get("y_rate")
    src = []
    if ras and dec is not None:
        ra_rate = gm.expected_px(ras, dec, 1000.0, scale, "ra")
        src.append("RA guide speed x cos(Dec)")
    elif xr:
        ra_rate = float(xr)
        src.append("RA calibration rate (Dec unknown)")
    elif ras:
        ra_rate = gm.expected_px(ras, 0.0, 1000.0, scale, "ra")
        src.append("RA guide speed, Dec unknown (no cos(Dec))")
    else:
        ra_rate = None
    if decs:
        dec_rate = gm.expected_px(decs, None, 1000.0, scale, "dec")
        src.append("Dec guide speed")
    elif yr:
        dec_rate = float(yr)
        src.append("Dec calibration rate")
    else:
        dec_rate = None
    return ra_rate, dec_rate, "; ".join(src)


_OPPOSITE = {"W": "E", "E": "W", "N": "S", "S": "N"}
_PHD2_POSITIVE = ("W", "S")       # PHD2 2.6 for a positive raw error


def _signs(frames, key, ms_key, dir_key) -> dict[str, float]:
    """{direction letter: +1 / -1}: +1 for the letter PHD2 uses on a
    positive raw error on this axis, by vote (guide_motion.axis_stats does
    the same). A session that only ever pulsed one way (a steady Dec drift)
    still fixes both letters; with no pulses to vote, PHD2 2.6's W / S."""
    votes: dict[str, int] = {}
    for f in frames:
        if f[ms_key] > 0 and f[dir_key] and f[key]:
            d = f[dir_key] if f[key] > 0 else "-" + f[dir_key]
            votes[d] = votes.get(d, 0) + 1
    best = max(votes, key=votes.get, default=None)
    if best is None:
        pos = next((d for d in _PHD2_POSITIVE if d in "".join(
            f[dir_key] for f in frames)), None)
    elif best.startswith("-"):
        pos = _OPPOSITE.get(best[1:])
    else:
        pos = best
    if pos is None:
        return {}
    return {pos: 1.0, _OPPOSITE.get(pos, ""): -1.0}


def _track(frames, key, ms_key, dir_key, rate_px_s) -> list[float]:
    """The star's position on one axis (px): raw distance + the cumulative
    correction applied by the pulses of the earlier frames."""
    sg = _signs(frames, key, ms_key, dir_key)
    cum, out = 0.0, []
    for f in frames:
        out.append(f[key] + cum)
        if f[ms_key] > 0 and f[dir_key] and rate_px_s and f.get("output", True):
            cum += sg.get(f[dir_key], 0.0) * f[ms_key] / 1000.0 * rate_px_s
    return out


def _fit(frames, ys) -> tuple[float | None, float | None]:
    """(slope px/s pooled within lock epochs, RMS of the residual px)."""
    pts: dict[int, list] = {}
    for f, y in zip(frames, ys):
        pts.setdefault(f["epoch"], []).append((f["t"], y))
    sl = gm.slope(pts)
    if sl is None:
        return None, None
    res = []
    for p in pts.values():
        tm = sum(a for a, _ in p) / len(p)
        ym = sum(b for _, b in p) / len(p)
        res += [(b - ym) - sl * (a - tm) for a, b in p]
    return sl, (sum(x * x for x in res) / len(res)) ** 0.5 if res else None


def _zero_pulse_rate(frames, key, ms_key) -> float | None:
    """px/s from consecutive frame pairs (same lock epoch) whose first frame
    sent no pulse on this axis: the change is drift plus seeing only."""
    d = dt = 0.0
    for a, b in zip(frames, frames[1:]):
        if a["epoch"] != b["epoch"] or a[ms_key] > 0:
            continue
        step = b["t"] - a["t"]
        if 0 < step < 120:
            d += b[key] - a[key]
            dt += step
    return d / dt if dt >= 60 else None


def window_min(config) -> float:
    return max(1.0, _f(config, "tracking_drift_window_min", 15.0))


def _windows(frames: list[dict], w_s: float) -> list[list[dict]]:
    """Split a long session into windows of about w_s seconds (a short tail
    joins the window before it): over hours the star's path is not a line
    (periodic error, model terms), and a sub only sees minutes of it."""
    if not frames:
        return [[]]
    t0 = frames[0]["t"]
    out: list[list[dict]] = []
    for f in frames:
        k = int((f["t"] - t0) // w_s)
        while len(out) <= k:
            out.append([])
        out[k].append(f)
    out = [w for w in out if w]
    if len(out) > 1 and out[-1][-1]["t"] - out[-1][0]["t"] < w_s / 2:
        tail = out.pop()
        out[-1] = out[-1] + tail
    return out


def analyze_session(sec: dict, config=None) -> list[dict]:
    """Drift and wobble of one guiding section, one row per window of
    tracking_drift_window_min ([] for a calibration)."""
    if sec.get("kind") != "guiding":
        return []
    good = [f for f in sec["frames"] if not f["drop"]]
    wins = _windows(good, window_min(config) * 60.0)
    rows = []
    for i, w in enumerate(wins):
        lo = w[0]["t"] if w else None
        hi = wins[i + 1][0]["t"] if i + 1 < len(wins) else None
        dropped = sum(1 for f in sec["frames"] if f["drop"]
                      and (lo is None or i == 0 or f["t"] >= lo)
                      and (hi is None or f["t"] < hi))
        r = _analyze(sec, w, dropped, config)
        r["window"] = i + 1
        r["windows"] = len(wins)
        rows.append(r)
    return rows


def _analyze(sec: dict, frames: list[dict], dropped: int, config=None) -> dict:
    h = sec["header"]
    scale, scale_src = guide_scale(h, config)
    start = sec.get("start_local")
    t0 = frames[0]["t"] if frames else 0.0
    t1 = frames[-1]["t"] if frames else 0.0
    span_min = (t1 - t0) / 60.0
    pulses_ra = sum(1 for f in frames if f["ra_ms"] > 0 and f.get("output", True))
    pulses_dec = sum(1 for f in frames if f["dec_ms"] > 0 and f.get("output", True))
    mode = RECON if (pulses_ra or pulses_dec) else RAW
    out = {"start": (start + timedelta(seconds=t0)).strftime("%Y-%m-%d %H:%M:%S")
           if start and frames else sec.get("start"),
           "session": sec.get("start"), "file": sec.get("file"),
           "end": (start + timedelta(seconds=t1)).strftime("%Y-%m-%d %H:%M:%S")
           if start and frames else sec.get("end"),
           "frames": len(frames), "dropped": dropped,
           "span_min": _r(span_min, 1), "mode": mode,
           "pulses_ra": pulses_ra, "pulses_dec": pulses_dec,
           "epochs": len({f["epoch"] for f in frames}),
           "dithers": sum(1 for e in sec["events"] if e[0] == "dither"),
           "guide_scale_arcsec": _r(scale, 4), "scale_source": scale_src,
           "dec_deg": h.get("dec_deg"), "pier_side": h.get("pier_side"),
           "hour_angle_hr": h.get("hour_angle_hr"),
           "excluded": None}
    if not frames:
        out["excluded"] = "no guided frames"
        return out
    if not scale:
        out["excluded"] = "no pixel scale in the log or config"
        return out
    ys_ra = [f["ra"] for f in frames]
    ys_dec = [f["dec"] for f in frames]
    if mode == RECON:
        ra_rate, dec_rate, rsrc = _pulse_rates(h, scale)
        out["rate_source"] = rsrc
        if (pulses_ra and not ra_rate) or (pulses_dec and not dec_rate):
            out["excluded"] = "pulses sent but no guide speed or calibration rate in the header"
            return out
        ys_ra = _track(frames, "ra", "ra_ms", "ra_dir", ra_rate)
        ys_dec = _track(frames, "dec", "dec_ms", "dec_dir", dec_rate)
        zr = _zero_pulse_rate(frames, "ra", "ra_ms")
        zd = _zero_pulse_rate(frames, "dec", "dec_ms")
        out["zero_pulse_ra_arcsec_min"] = _r(zr * 60 * scale) if zr is not None else None
        out["zero_pulse_dec_arcsec_min"] = _r(zd * 60 * scale) if zd is not None else None
    sra, rra = _fit(frames, ys_ra)
    sdec, rdec = _fit(frames, ys_dec)
    if sra is None or sdec is None:
        out["excluded"] = "too few frames per lock epoch to fit a drift"
        return out
    ra_m, dec_m = sra * 60 * scale, sdec * 60 * scale
    wob = math.hypot(rra, rdec) * scale
    out.update(ra_arcsec_min=_r(ra_m), dec_arcsec_min=_r(dec_m),
               total_arcsec_min=_r(math.hypot(ra_m, dec_m)),
               rms_ra_arcsec=_r(rra * scale), rms_dec_arcsec=_r(rdec * scale),
               rms_arcsec=_r(wob))
    if len(frames) < MIN_FRAMES or span_min < min_session_min(config):
        out["excluded"] = (f"short: {span_min:.1f} min, {len(frames)} frames "
                           f"(need {min_session_min(config):g} min, {MIN_FRAMES} frames)")
    elif wob > max_rms_arcsec(config):
        out["excluded"] = (f"wobble {wob:.1f}\" RMS > {max_rms_arcsec(config):g}\" "
                           f"(star lost / jumps{f', {dropped} dropped frames' if dropped else ''})")
    return out


# --------------------------------------------------------------------------
# night summary
# --------------------------------------------------------------------------

def smear_table(rate_arcsec_min: float | None, scales: dict[str, float]) -> list[dict]:
    """Linear smear of each sub length, arcsec and px per rig."""
    if rate_arcsec_min is None:
        return []
    out = []
    for s in SUB_LENGTHS_S:
        a = rate_arcsec_min * s / 60.0
        out.append({"exp_s": s, "arcsec": _r(a, 2),
                    "px": {rig: _r(a / k, 1) for rig, k in scales.items()}})
    return out


def summarize(sessions: list[dict], config=None) -> dict:
    """Night numbers from analyze_session rows (clean sessions only)."""
    used = [s for s in sessions if s and not s.get("excluded")]
    excl = [{"start": s.get("start"), "reason": s["excluded"],
             "frames": s.get("frames"), "rms_arcsec": s.get("rms_arcsec")}
            for s in sessions if s and s.get("excluded")]
    out = {"sessions_used": len(used), "sessions_excluded": excl,
           "ra_arcsec_min": None, "dec_arcsec_min": None, "total_arcsec_min": None,
           "rms_arcsec": None, "mode": None, "smear": []}
    if not used:
        return out
    ra = statistics.median(abs(s["ra_arcsec_min"]) for s in used)
    dec = statistics.median(abs(s["dec_arcsec_min"]) for s in used)
    tot = math.hypot(ra, dec)
    modes = {s["mode"] for s in used}
    out.update(ra_arcsec_min=_r(ra, 2), dec_arcsec_min=_r(dec, 2),
               total_arcsec_min=_r(tot, 2),
               ra_signed_median=_r(statistics.median(s["ra_arcsec_min"] for s in used), 2),
               dec_signed_median=_r(statistics.median(s["dec_arcsec_min"] for s in used), 2),
               rms_arcsec=_r(statistics.median(s["rms_arcsec"] for s in used), 2),
               mode=modes.pop() if len(modes) == 1 else MIXED,
               frames=sum(s["frames"] for s in used),
               minutes=_r(sum(s["span_min"] or 0 for s in used), 1),
               first=used[0].get("start"), last_end=used[-1].get("end"),
               smear=smear_table(tot, rig_scales(config)))
    return out


def _key(paths) -> tuple:
    out = []
    for p in paths:
        try:
            st = Path(p).stat()
            out.append((str(p), st.st_mtime, st.st_size))
        except OSError:
            out.append((str(p), None, None))
    return tuple(out)


def night_drift(config, date: str) -> dict:
    """Per session and night summary for one night's guide logs. Cached on
    the log files' mtime / size (a live log re-parses as it grows)."""
    found = pl.find_logs(config, "guide")
    paths, note = pl.select(found["files"], date=date)
    if not paths:
        return {"date": date, "ok": False, "note": note or "no PHD2 guide logs",
                "sessions": [], "summary": summarize([], config)}
    k = (date, _key(paths), max_rms_arcsec(config), min_session_min(config),
         window_min(config),
         tuple(sorted(rig_scales(config).items())))
    hit = _CACHE.get(k)
    if hit is not None:
        return hit
    sections = []
    for p in paths:
        try:
            sections += pl.parse_guide_log(
                Path(p).read_text(encoding="utf-8", errors="replace"), Path(p).name)
        except OSError as e:
            logger.warning("PS-168: guide log %s unreadable: %s", p, e)
    # a log still being written past noon covers two nights: keep only the
    # sessions that started inside this night's window
    w0, w1 = pl.lf.night_window(date)
    sections = [s for s in sections if s.get("start_local") is None
                or w0 <= s["start_local"] < w1]
    rows = [r for s in sections for r in analyze_session(s, config)]
    rows = [r for r in rows if r["frames"] or r["dropped"]]
    out = {"date": date, "ok": True, "files": [Path(p).name for p in paths],
           "sessions": rows, "summary": summarize(rows, config)}
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[k] = out
    return out


def _nights_back(date: str, n: int) -> list[str]:
    d = datetime.strptime(date, "%Y-%m-%d").date()
    return [(d - timedelta(days=i)).isoformat() for i in range(max(1, n) - 1, -1, -1)]


def tpoint_model(config, night: str) -> dict:
    """{model_date, points, protrack, source} in force on `night`: the PS-138
    audit saved that night (its manual TPoint record and ProTrack row), else
    the newest manual record whose model_date is not after the night."""
    try:
        from photonscript.scheduler import thesky_audit as ta
        from photonscript.shared import phd2_store as store
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    try:
        a = ta.load_night(config, night) or {}
    except Exception:  # noqa: BLE001
        a = {}
    m = a.get("manual") or {}
    if m.get("model_date") and str(m["model_date"]) <= night:
        out = {"model_date": m.get("model_date"), "points": m.get("points"),
               "source": f"TheSky audit {night}"}
    for r in a.get("rows") or []:
        if r.get("id") == "protrack_on":
            out["protrack"] = r.get("protrack") or r.get("current")
    if not out.get("model_date"):
        try:
            hist = store.read_jsonl(ta.manual_path(config).with_name("manual_history.jsonl"))
            cur = ta.load_manual(config)
            recs = [r for r in hist + ([cur] if cur else [])
                    if r and r.get("model_date") and str(r["model_date"]) <= night]
            if recs:
                r = max(recs, key=lambda x: (str(x["model_date"]), str(x.get("entered_at"))))
                out.update(model_date=r.get("model_date"), points=r.get("points"),
                           source="manual TPoint record")
        except Exception:  # noqa: BLE001
            pass
    return out


def trend(config, date: str, nights: int | None = None) -> dict:
    """The night summary over the last N nights, each keyed by its TPoint
    model, plus a before / after table per model."""
    n = int(nights if nights is not None else _f(config, "tracking_drift_trend_nights", 14))
    n = max(1, min(n, 60))
    rows = []
    for night in _nights_back(date, n):
        nd = night_drift(config, night)
        s = nd["summary"]
        if not nd.get("ok") or not (s["sessions_used"] or s["sessions_excluded"]):
            continue
        m = tpoint_model(config, night)
        rows.append({"date": night, "ra_arcsec_min": s["ra_arcsec_min"],
                     "dec_arcsec_min": s["dec_arcsec_min"],
                     "total_arcsec_min": s["total_arcsec_min"],
                     "rms_arcsec": s["rms_arcsec"], "mode": s["mode"],
                     "sessions_used": s["sessions_used"],
                     "sessions_excluded": len(s["sessions_excluded"]),
                     "model_date": m.get("model_date"), "model_points": m.get("points"),
                     "protrack": m.get("protrack")})
    by_model: dict = {}
    for r in rows:
        if r["total_arcsec_min"] is None:
            continue
        key = (r["model_date"] or "unknown", r["model_points"])
        by_model.setdefault(key, []).append(r)
    models = []
    for (md, pts), rs in sorted(by_model.items(), key=lambda kv: str(kv[0][0])):
        models.append({"model_date": None if md == "unknown" else md, "points": pts,
                       "nights": len(rs),
                       "ra_arcsec_min": _r(statistics.median(x["ra_arcsec_min"] for x in rs), 2),
                       "dec_arcsec_min": _r(statistics.median(x["dec_arcsec_min"] for x in rs), 2),
                       "total_arcsec_min": _r(statistics.median(x["total_arcsec_min"] for x in rs), 2),
                       "rms_arcsec": _r(statistics.median(x["rms_arcsec"] for x in rs), 2)})
    return {"nights": n, "rows": rows, "by_model": models}


# --------------------------------------------------------------------------
# PS-169: drift-capped unguided sub length
# --------------------------------------------------------------------------

def _night_of(config, now: datetime) -> str:
    from photonscript.shared import phd2_store as store
    try:
        return store.night_of(config, now)
    except Exception:  # noqa: BLE001
        return (now - timedelta(hours=12)).strftime("%Y-%m-%d")


def live_drift(config, now: datetime | None = None) -> dict | None:
    """The drift to cap with: tonight's clean sessions that ended within
    tracking_drift_recent_min (local scope-PC clock, the guide log's), else
    last night's summary. {total_arcsec_min, ra, dec, source, sessions} or
    None when neither exists."""
    now = now or datetime.now()
    night = (now - timedelta(hours=12)).strftime("%Y-%m-%d")
    recent = _f(config, "tracking_drift_recent_min", 60.0)
    tonight = night_drift(config, night)
    fresh = []
    for s in tonight.get("sessions") or []:
        if s.get("excluded"):
            continue
        end = pl.parse_ts(s.get("end"))
        if end and timedelta(0) <= now - end <= timedelta(minutes=recent):
            fresh.append(s)
    if fresh:
        sm = summarize(fresh, config)
        return {"total_arcsec_min": sm["total_arcsec_min"],
                "ra_arcsec_min": sm["ra_arcsec_min"], "dec_arcsec_min": sm["dec_arcsec_min"],
                "sessions": len(fresh), "night": night,
                "source": f"tonight, {len(fresh)} session(s) in the last {recent:g} min"}
    prev = (datetime.strptime(night, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    last = night_drift(config, prev)["summary"]
    if last.get("total_arcsec_min"):
        return {"total_arcsec_min": last["total_arcsec_min"],
                "ra_arcsec_min": last["ra_arcsec_min"], "dec_arcsec_min": last["dec_arcsec_min"],
                "sessions": last["sessions_used"], "night": prev,
                "source": f"last night ({prev}, {last['sessions_used']} session(s))"}
    return None


def dark_lengths(config, rig: str) -> list[float]:
    """The dark lengths the night quota fills for the rig (PS-160), cached."""
    k = (str(getattr(config, "data_dir", "")), rig, str(getattr(config, "dark_exposures", "")),
         str(getattr(config, "piggyback_dark_exposures", "")),
         getattr(config, "unguided_max_exposure_s", None))
    hit = _darks_cache.get(k)
    if hit and time.monotonic() - hit[0] < DARKS_TTL_S:
        return hit[1]
    try:
        from photonscript.scheduler.calibration import night_dark_exposures
        v = sorted({float(x) for x in night_dark_exposures(config, rig) if x and x > 0})
    except Exception as e:  # noqa: BLE001
        logger.debug("PS-169: dark lengths unavailable: %s", e)
        v = []
    _darks_cache[k] = (time.monotonic(), v)
    return v


def recommend(config, rig: str, rate_arcsec_min: float | None, *,
              cap_s: float | None = None, darks: list[float] | None = None) -> dict:
    """The longest unguided sub that keeps the drift smear within the
    budget: budget_px x scale / rate. Never above cap_s (default
    unguided_max_exposure_s for the RC16, piggyback_exposure_s for the
    Piggy-600), never below tracking_sub_floor_s, rounded down to the
    longest dark length in [floor, max] so darks match; when none fits the
    raw value is kept (rounded down to 5 s) and dark_match is False (PS-160
    then adds that length to the night's darks once lights use it)."""
    from photonscript.shared.rigs import PIGGYBACK
    scale = rig_scales(config).get(rig) or _f(config, "pixel_scale_arcsec", 0.236)
    budget = _f(config, "tracking_smear_budget_px", 1.5)
    floor = max(1.0, _f(config, "tracking_sub_floor_s", 30.0))
    if cap_s is None:
        key = "piggyback_exposure_s" if rig == PIGGYBACK else "unguided_max_exposure_s"
        cap_s = _f(config, key, 120.0 if rig == PIGGYBACK else 300.0) or None
    out = {"rig": rig, "scale_arcsec": scale, "budget_px": budget,
           "budget_arcsec": _r(budget * scale, 3), "floor_s": floor, "cap_s": cap_s,
           "rate_arcsec_min": rate_arcsec_min, "drift_max_s": None,
           "exposure_s": None, "dark_match": None, "why": None}
    if not rate_arcsec_min or rate_arcsec_min <= 0:
        out["why"] = "no drift measurement"
        return out
    raw = budget * scale / rate_arcsec_min * 60.0
    out["drift_max_s"] = _r(raw, 1)
    s, why = raw, "drift budget"
    if cap_s and s > cap_s:
        s, why = float(cap_s), "existing cap"
    if s < floor:
        s, why = floor, "floor"
    darks = dark_lengths(config, rig) if darks is None else darks
    fit = [d for d in darks if floor - 0.5 <= d <= s + 0.5]
    if fit:
        s, out["dark_match"] = max(fit), True
    else:
        s = max(floor, math.floor(s / 5.0) * 5.0)
        out["dark_match"] = False
    out.update(exposure_s=_r(s, 1), why=why, darks=darks,
               smear_arcsec=_r(rate_arcsec_min * s / 60.0, 2),
               smear_px=_r(rate_arcsec_min * s / 60.0 / scale, 2))
    return out


def recommendations(config, drift: dict | None = None, now: datetime | None = None) -> dict:
    """{rig: recommend(...)} from live_drift (or the given drift)."""
    d = live_drift(config, now) if drift is None else drift
    rate = (d or {}).get("total_arcsec_min")
    return {"mode": cap_mode(config), "drift": d,
            "rigs": {rig: recommend(config, rig, rate) for rig in rig_scales(config)}}


def cap_length(config, seconds: float | None, rec: dict | None) -> tuple[float | None, str | None]:
    """PS-169 on one RC16 fallback length: (seconds, note). In mode auto the
    shorter of the two; observe / off leave it (observe returns the note of
    what auto would do)."""
    m = cap_mode(config)
    if m == "off" or not rec or not rec.get("exposure_s") or not seconds:
        return seconds, None
    d = float(rec["exposure_s"])
    if d >= seconds - 0.5:
        return seconds, None
    note = f"drift {rec['rate_arcsec_min']:g}\"/min caps at {d:g} s"
    if m == "auto":
        return d, note
    return seconds, f"observe: {note}"


# --------------------------------------------------------------------------
# API + report lines
# --------------------------------------------------------------------------

def latest_night(config) -> str | None:
    """The newest night with a guide log (12:00 to 12:00 local)."""
    found = pl.find_logs(config, "guide")
    for p in reversed(found["files"]):
        st = pl.lf.file_start(p.name)
        if st:
            return (st - timedelta(hours=12)).strftime("%Y-%m-%d")
    return None


def report(config, date: str = "", nights: int | None = None) -> dict:
    """GET /api/tracking/drift: the night (default the newest guide log's),
    the trend and the PS-169 recommendations."""
    date = date or latest_night(config) or _night_of(config, datetime.utcnow())
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "note": f"bad date {date!r} (use YYYY-MM-DD)"}
    out = night_drift(config, date)
    out = dict(out)
    out["scales"] = rig_scales(config)
    out["limits"] = {"max_rms_arcsec": max_rms_arcsec(config),
                     "min_session_min": min_session_min(config),
                     "min_frames": MIN_FRAMES}
    out["model"] = tpoint_model(config, date)
    out["trend"] = trend(config, date, nights)
    try:
        out["recommended"] = recommendations(config)
    except Exception as e:  # noqa: BLE001
        out["recommended"] = {"error": str(e)}
    out["line"] = report_line(out["summary"], out["scales"])
    return out


def report_line(summary: dict, scales: dict | None = None) -> str | None:
    """'Tracking: RA 0.41"/min, Dec 0.31"/min, 300 s smear 2.5" (11 px RC16)'."""
    if not summary or summary.get("total_arcsec_min") is None:
        n = len((summary or {}).get("sessions_excluded") or [])
        return f"Tracking: no clean guide-log session ({n} excluded)" if n else None
    s300 = next((r for r in summary.get("smear") or [] if r["exp_s"] == 300), None)
    t = (f"Tracking: RA {summary['ra_arcsec_min']:.2f}\"/min, "
         f"Dec {summary['dec_arcsec_min']:.2f}\"/min")
    if s300:
        px = (s300.get("px") or {}).get("rc16")
        t += f", 300 s smear {s300['arcsec']:.1f}\"" + (f" ({px:.0f} px RC16)" if px else "")
    t += f", wobble {summary['rms_arcsec']:.1f}\" RMS"
    if summary.get("mode") and summary["mode"] != RAW:
        t += f" [{summary['mode']}]"
    return t


def morning_line(config, date: str) -> str | None:
    """The PS-166 morning report line for a night (None: no guide log)."""
    nd = night_drift(config, date)
    if not nd.get("ok"):
        return None
    return report_line(nd["summary"], rig_scales(config))
