"""Sub doctor: why the last subs were bad, and what to do about it.

2026-10-12 (night_of): the armer re-armed UNGUIDED (TPoint + ProTrack) but
PHD2 was still Guiding on a manual calibration graded FAIL (axes 78 deg from
perpendicular, Dec 61.6). PHD2 said 0.22" RMS while the RC16 180 s and 300 s
subs trailed (ecc 0.63 to 0.78, HFR 9 to 12 px). Earlier nights: the RC16
focuser dropped off USB (FOCPOS 0, AF never finished: round 5" stars),
clouds (star counts down), a target dropped by a moon-rule bug. Each time
the scorer rejected subs and nothing said WHY or what to do.

After every graded light (telescope agent, sub_doctor_live) and over any past
night (replay_night, `photonscript sub-doctor --night D`), the last
sub_doctor_window (3) subs of a rig are judged together with the subs the
other rig exposed at the same time (sub_pairs) and the guiding context:

    focuser          FOCPOS 0 / a live focuser read of 0, or autofocus that
                     keeps restarting without finishing (af_stall) while
                     the subs are soft
    guider_conflict  PHD2 guiding on an UNGUIDED night; PHD2 not guiding on
                     a GUIDED night; PHD2 guiding on a FAIL calibration;
                     PHD2 RMS tiny while the subs trail (non-star lock,
                     PS-91). "Guiding" (2026-10-14) means AppState Guiding,
                     or the guide log shows a guiding session over the
                     window; LostStar / Looping / Stopped / Calibrating
                     without one fall through to tracking
    pointing         off target (the pointing gate failed) or plate solves
                     failing
    tracking         stars stretched one way (ecc gate failing) with no
                     guider conflict: trail direction from the star sidecar
                     (axial stats, PS-95) turned to the sky with the last
                     plate solve (RA drift / Dec drift / field rotation), a
                     trail rate from HFR + ecc, and the PS-169 drift-capped
                     sub length
    clouds           star counts down (gate, no stars, or under half the
                     night median) on both rigs (one rig only: dew or clouds)
    seeing           soft but round on both rigs at the same time
    focus            soft but round on this rig only: AF age, temperature
                     change since the last AF
    other            bad subs none of the above explains (the failing checks)

Each diagnosis: {cause, confidence (high | medium | low), headline,
evidence {...numbers...}, fix (one sentence), action {id, text, auto,
enabled}, secondary [other causes that also matched], t, file, n_window,
n_bad}. Action ids: stop_phd2, recalibrate_phd2, reselect_guide_star,
start_guiding, run_af, check_focuser, shorten_subs:<s>, check_tracking,
check_pointing, wait_clouds, none. auto: an action the night pilot can
take; enabled: always False (kept for the event schema).

record() keeps <data_dir>/sub_doctor/<night>.json (current diagnosis per rig,
causes already pushed), writes one run event per diagnosis
(runs/<night>_events.jsonl, kind "sub_doctor", see EVENT_FIELDS) and a
"none" event when a rig recovers, and decides the push (one per cause per
rig per night, sub_doctor_mode alert).

One actor for remedies (2026-10-13): the doctor only diagnoses. The night
pilot (scheduler/night_pilot.py) is the one actor that turns a
suggested_action into a command (PHD2 stop, sub cap, AF, skip). The
doctor's own opt-in remedies (sub_doctor_stop_phd2_unguided,
sub_doctor_request_af, sub_doctor_shorten_subs) and its caps.json sub cap
are retired: the config keys stay readable but have no effect.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import sub_pairs as sp
from photonscript.shared.sub_time import parse_utc, sub_window

logger = logging.getLogger(__name__)

MODES = ("off", "observe", "alert")
CAUSES = ("focuser", "guider_conflict", "pointing", "tracking", "clouds",
          "seeing", "focus", "other")
HIGH, MEDIUM, LOW = "high", "medium", "low"
ACTIVE_GUIDE = ("guiding", "settling", "lost_star", "calibrating")
# 2026-10-14: only these mean PHD2 is moving the mount. lost_star is
# ambiguous (StarLost fires while looping too), so it counts only with the
# guide log (or a calibration / guider start tonight) behind it.
GUIDING = ("guiding", "settling")
TONIGHT_H = 12.0           # a calibration this recent counts as tonight's
ALERT_TITLE = "PhotonScript sub doctor"
EVENT_KIND = "sub_doctor"
REMEDY_KIND = "sub_doctor_remedy"   # retired remedies: only in old event logs
# the stable run-event schema the night pilot reads (kind "sub_doctor")
EVENT_FIELDS = ("t", "rig", "src", "kind", "value", "cause", "confidence",
                "headline", "evidence", "fix", "suggested_action", "secondary",
                "file", "n_window", "n_bad")
RA_TOL_DEG = 30.0          # trail within this of the RA / Dec axis
TANGENTIAL_ROT = 0.55      # this share of stars stretched tangentially = rotation
AXIS_R_MIN = 0.45          # stars stretched one way (axial R) at least this
AXIS_SPREAD_MAX = 25.0     # window subs' trail axes agree within this


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def mode(config) -> str:
    m = str(getattr(config, "sub_doctor_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def _i(config, key, default):
    try:
        return int(getattr(config, key, default))
    except (TypeError, ValueError):
        return int(default)


def _fl(config, key, default):
    try:
        v = float(getattr(config, key, default))
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def window_n(config) -> int:
    return max(1, _i(config, "sub_doctor_window", 3))


def min_bad(config) -> int:
    return max(1, min(window_n(config), _i(config, "sub_doctor_min_bad", 2)))


def _scale(config, rig: str) -> float:
    key = "piggyback_pixel_scale_arcsec" if rig == "piggyback" else "pixel_scale_arcsec"
    return _fl(config, key, 1.29 if rig == "piggyback" else 0.236) or 0.236


def _label(config, rig: str) -> str:
    try:
        from photonscript.shared.rigs import rig_label
        return rig_label(config, rig)
    except Exception:  # noqa: BLE001
        return rig


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _med(xs, nd=2):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), nd) if xs else None


def _hm(t) -> str:
    d = parse_utc(t) if not isinstance(t, datetime) else t
    return d.strftime("%H:%M") + "Z" if d else "?"


def _iso_t(t) -> str | None:
    d = parse_utc(t) if not isinstance(t, datetime) else t
    return d.replace(microsecond=0).isoformat() + "Z" if d else None


def armed_mode(config) -> str | None:
    """'guided' / 'unguided' when a night is armed or watched, else None.
    The scheduler's armer when it runs in this process, else its persisted
    armer_state.json (an unset override = config.guided_default)."""
    import sys
    try:
        from photonscript.scheduler.armer import LIVE_STATES, norm_guiding_mode
    except Exception:  # noqa: BLE001
        return None
    app_mod = sys.modules.get("photonscript.scheduler.app")
    armer = getattr(app_mod, "_armer", None) if app_mod else None
    if armer is not None:
        try:
            if armer.state not in LIVE_STATES:
                return None
            return "guided" if armer._use_guiding() else "unguided"
        except Exception:  # noqa: BLE001
            return None
    p = Path(getattr(config, "data_dir", ".")) / "armer_state.json"
    try:
        saved = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if saved.get("state") not in LIVE_STATES:
        return None
    m = norm_guiding_mode(saved.get("guiding_override"))
    if m:
        return m
    return "guided" if getattr(config, "guided_default", True) else "unguided"


def plan_mode(plan: dict | None) -> str | None:
    """Guiding mode from a night plan snapshot (runs/<night>_plan.json)."""
    ts = (plan or {}).get("targets") or []
    flags = [t.get("guided") for t in ts if isinstance(t, dict) and "guided" in t]
    if not flags:
        return None
    return "guided" if any(flags) else "unguided"


# --------------------------------------------------------------------------
# star geometry: trail direction and length
# --------------------------------------------------------------------------

def elongation(table: dict | None) -> dict:
    """Trail axis of one sub from its star sidecar / in-memory star table:
    {axis_deg (image, from +x toward +y), R, tangential_frac, radial_frac,
    n_elongated} or {} (too few elongated stars or no angles)."""
    if not table or not table.get("theta"):
        return {}
    try:
        from photonscript.scheduler.optics_report import ELONG_FLOOR, axial_stats
        from photonscript.shared.star_shape import to_sqrt
        ecc = [None if e is None else to_sqrt(e, table.get("ecc_def") or "")
               for e in table.get("ecc") or []]
        return axial_stats(table.get("theta"), ecc, table.get("x"), table.get("y"),
                           table.get("w"), table.get("h"), floor=ELONG_FLOOR)
    except Exception as e:  # noqa: BLE001
        logger.debug("elongation skipped: %s", e)
        return {}


def trail_estimate(hfr_px, ecc, exp_s, scale_arcsec) -> dict:
    """Trail length from the median star: a Gaussian of sigma s smeared over
    a line of length L has sigma_major^2 = s^2 + L^2/12, so with b/a = q and
    e = sqrt(1-q^2): L = sqrt(12) * sigma_minor * e / q, sigma_minor from
    the HFR (Gaussian HFR = 1.1774 sigma, geometric mean of the axes)."""
    h, e, x, s = _num(hfr_px), _num(ecc), _num(exp_s), _num(scale_arcsec)
    if not h or e is None or not x or not s or not 0 < e < 1:
        return {}
    q = math.sqrt(1.0 - e * e)
    sig_minor = h / 1.1774 * math.sqrt(q)
    length = math.sqrt(12.0) * sig_minor * e / q
    return {"length_px": round(length, 1), "length_arcsec": round(length * s, 2),
            "rate_arcsec_min": round(length * s / (x / 60.0), 2)}


def latest_solve(config, night: str, rig: str, nights_back: int = 14) -> dict | None:
    """The newest solved plate solve of the rig: tonight, else up to
    nights_back nights before (the camera angle rarely changes)."""
    try:
        from photonscript.scheduler import solve_store
        d0 = datetime.strptime(night, "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None
    for k in range(nights_back + 1):
        n = (d0 - timedelta(days=k)).strftime("%Y-%m-%d")
        try:
            rows = solve_store.load(config, n, rig)
        except Exception:  # noqa: BLE001
            rows = []
        for r in reversed(rows):
            if r.get("pa") is not None or r.get("cd"):
                return {"pa": r.get("pa"), "parity": r.get("parity"), "cd": r.get("cd"),
                        "scale": r.get("scale"), "night": n, "file": r.get("file")}
    return None


def sky_direction(axis_deg, solve: dict | None, scale: float) -> dict:
    """Image trail axis -> sky position angle (deg E of N, 0..180) and the
    mount axis it runs along (RA / Dec / diagonal). {} without a solve."""
    a = _num(axis_deg)
    if a is None or not solve:
        return {}
    cd = solve.get("cd")
    if not cd and solve.get("pa") is not None:
        from photonscript.scheduler.solve_store import nominal_cd
        cd = nominal_cd(scale, float(solve["pa"]), int(solve.get("parity") or -1))
    if not cd:
        return {}
    r = math.radians(a)
    dx, dy = math.cos(r), math.sin(r)
    east = cd[0][0] * dx + cd[0][1] * dy
    north = cd[1][0] * dx + cd[1][1] * dy
    pa = math.degrees(math.atan2(east, north)) % 180.0
    off_ra = abs(pa - 90.0)
    off_dec = min(pa, 180.0 - pa)
    along = ("RA" if off_ra <= RA_TOL_DEG else
             "Dec" if off_dec <= RA_TOL_DEG else "diagonal")
    return {"sky_pa_deg": round(pa, 1), "along": along,
            "camera_pa_deg": solve.get("pa"),
            "solve": solve.get("night")}


def _axial_spread(angles) -> float | None:
    a = [x for x in angles if x is not None]
    if len(a) < 2:
        return None
    from photonscript.scheduler.optics_report import axial_mean
    m = axial_mean(a)
    d = [min(abs(x - m) % 180.0, 180.0 - abs(x - m) % 180.0) for x in a]
    return round(max(d), 1)


# --------------------------------------------------------------------------
# the diagnosis
# --------------------------------------------------------------------------

def _phd2_view(states: list[dict], live: dict | None) -> dict:
    """PHD2 as seen over the window: the live read wins for the state, the
    subs' own guide_state (at grading) otherwise; RMS only from guiding
    samples."""
    sub_states = [str(s.get("guide_state") or "").lower() for s in states]
    c = Counter(x for x in sub_states if x)
    live = live or {}
    st = str(live.get("state") or "").lower() or None
    active_subs = sum(1 for x in sub_states if x in ACTIVE_GUIDE)
    if st:
        active = st in ACTIVE_GUIDE
    else:
        active = active_subs * 2 > len(sub_states) if sub_states else False
    # 0 means "no samples yet" (a fresh PHD2 window), never a real RMS
    rms = _num(live.get("rms_arcsec")) if st == "guiding" else None
    if not rms or rms <= 0:
        rms = _med([v for s in states
                    if str(s.get("guide_state") or "").lower() == "guiding"
                    and (v := _num(s.get("guide_rms"))) and v > 0])
    return {"state": st or (c.most_common(1)[0][0] if c else None),
            "source": "live" if st else "subs", "active": active,
            "sub_states": dict(c), "active_subs": active_subs,
            "rms_arcsec": None if rms is None else round(rms, 2),
            "snr": _num(live.get("snr")), "hfd_px": _num(live.get("hfd_px"))}


def _guiding(phd2: dict, log: dict | None, cal: dict | None, gs: dict | None,
             t_end: datetime | None) -> tuple[bool, str]:
    """2026-10-14: is PHD2 actually guiding over the window? (yes/no, why).
    Guiding / Settling (live, or any sub in the window) is; otherwise the
    guide log decides when there is one (frames in a guiding session over
    the window: 2026-10-14 had PHD2 open, LostStar / Looping, 0 sessions,
    while both rigs trailed after the meridian flip). Without a log a
    LostStar counts only when PHD2 calibrated or a guider start was seen
    tonight; Looping / Stopped / Calibrating never do."""
    st = phd2.get("state")
    subs_guiding = any(k in GUIDING for k in (phd2.get("sub_states") or {}))
    if st in GUIDING or (phd2.get("source") == "subs" and subs_guiding):
        return True, f"PHD2 {st}"
    if log is not None:
        if log.get("pulses") or log.get("frames"):
            return True, (f"PHD2 guide log: {log.get('frames')} frames, "
                          f"{log.get('pulses')} corrections in the window")
        return False, "PHD2 guide log: no guiding in the window"
    if st == "lost_star":
        ct = parse_utc((cal or {}).get("t_utc"))
        if ct and t_end and timedelta(0) <= t_end - ct <= timedelta(hours=TONIGHT_H):
            return True, "PHD2 lost_star after a calibration tonight"
        if gs and gs.get("by"):
            return True, "PHD2 lost_star after a guider start tonight"
    return False, f"PHD2 {st or 'unknown'}, not guiding"


def _pier_ha(window: list[dict], pointing: dict | None) -> tuple[str | None, float | None]:
    """The last sub's pier side (East | West) and hour angle (h), from the
    PS-67 pointing record (ctx) or the sub record itself."""
    from photonscript.shared.pointing import pier_name
    p = pointing or {}
    last = window[-1] if window else {}
    pier = pier_name(p.get("pier") or last.get("pier") or last.get("pier_side"))
    ha = _num(p.get("ha_h"))
    if ha is None:
        ha = _num(last.get("ha_h"))
    return pier, (round(ha, 2) if ha is not None else None)


def _cal_view(cal: dict | None) -> dict | None:
    if not cal:
        return None
    return {"grade": cal.get("grade"), "t_utc": cal.get("t_utc"),
            "reasons": (cal.get("reasons") or [])[:2],
            "dec_deg": cal.get("dec_deg"), "ha_hr": cal.get("ha_hr"),
            "context": cal.get("context")}


def _action(aid: str, text: str, auto: bool = False, enabled: bool = False) -> dict:
    return {"id": aid, "text": text, "auto": bool(auto), "enabled": bool(enabled)}


def diagnose(config, rig: str, window: list[dict], *, subs: list[dict] | None = None,
             ctx: dict | None = None) -> dict | None:
    """The diagnosis for the rig's last subs (`window`, oldest first, from
    the night's `subs` of both rigs), or None when they are healthy (fewer
    than sub_doctor_min_bad bad subs among the non-excluded ones).

    ctx (all optional): armed ('guided' | 'unguided' | None), phd2 (live
    {state, rms_arcsec, snr, hfd_px}), calibration (the active PHD2
    calibration record), nonstar (a PS-91 non-star episode overlaps the
    window), focus {last_af_utc, last_af_temp_c, temp_c, af_stall},
    solve (latest_solve), plate_solve_errors (count tonight), now."""
    ctx = ctx or {}
    subs = subs if subs is not None else list(window)
    bases = ctx.get("bases") or sp.baselines(subs)
    cache = ctx.get("pair_cache")
    pairs = []
    for r in window:
        p = cache.get(id(r)) if cache is not None else None
        if p is None:
            p = sp.pair_for(rig, r, subs, bases)
            if cache is not None:
                cache[id(r)] = p
        pairs.append(p)
    states = [p["sub"] for p in pairs]
    for s, r in zip(states, window):
        s["guide_rms"] = r.get("guide_rms")
    live = [(s, p) for s, p in zip(states, pairs) if not s["excluded"]]
    if not live:
        return None
    bad = [(s, p) for s, p in live if s["bad"]]
    need = min_bad(config)
    if len(bad) < need:
        return None
    last = window[-1]
    label = _label(config, rig)
    bs = [s for s, _ in bad]
    n_b = len(bs)

    def most(key):
        """At least half of the bad subs (and at least one) show it."""
        k = sum(1 for s in bs if s.get(key))
        return k >= 1 and 2 * k >= n_b

    pv = Counter(p["verdict"] for _, p in bad)
    ev = {"window": len(window), "bad": n_b,
          "ecc": _med([s["ecc"] for s in bs]), "hfr_px": _med([s["hfr"] for s in bs]),
          "fwhm_arcsec": _med([s["fwhm"] for s in bs]),
          "stars": _med([s["stars"] for s in bs], 0),
          "exp_s": _med([s["exp_s"] for s in bs], 0),
          "fails": dict(Counter(f for s in bs for f in s["fails"])),
          "pair": dict(pv)}
    try:   # target core clipped (shared.core_sat): evidence only, not a cause
        from photonscript.shared.core_sat import warn_px
        thr = warn_px(config, rig)
        nc = sum(1 for r in window if (_num(r.get("core_sat_px")) or 0) > thr)
        if nc:
            ev["core_clipped_subs"] = nc
    except Exception:  # noqa: BLE001
        pass
    phd2 = _phd2_view(states, ctx.get("phd2"))
    cal = _cal_view(ctx.get("calibration"))
    armed = ctx.get("armed")
    w_last = sub_window(last)
    guiding, guiding_why = _guiding(phd2, ctx.get("guide_log"), cal,
                                    ctx.get("guider_start"),
                                    w_last[1] if w_last else None)
    nonstar = bool(ctx.get("nonstar")) or any(
        r.get("guide_lock") == "non-star" for r in window)
    found: list[dict] = []

    def add(cause, conf, headline, fix, action, extra=None):
        found.append({"cause": cause, "confidence": conf, "headline": headline,
                      "fix": fix, "action": action,
                      "evidence": {**ev, **(extra or {})}})

    trailing = most("trailing")
    soft = most("soft")
    stars_low = most("stars_low")

    # 1. focuser dead / frozen
    fz = sum(1 for s in bs if s["focuser_zero"])
    stall = (ctx.get("focus") or {}).get("af_stall")
    if fz * 2 >= n_b and fz:
        add("focuser", HIGH,
            f"{label} focuser reads position 0 on {fz} of {n_b} bad subs: focus unknown",
            "Check the focuser USB / power, reconnect it in NINA, then run AF.",
            _action("check_focuser", "reconnect the focuser, then run AF"),
            {"focpos_zero_subs": fz})
    elif stall and soft:
        add("focuser", HIGH,
            f"{label} autofocus keeps restarting without finishing "
            f"({stall.get('runs') or stall.get('value')} runs) while subs are soft",
            "The focuser is not moving or not reporting: check it in NINA "
            "(position, temperature changing), reconnect, then run AF.",
            _action("check_focuser", "reconnect the focuser, then run AF"),
            {"af_stall": stall})

    # 2. guider conflict: trailing subs, or soft subs under a PHD2 that is
    # visibly moving the mount around (RMS over sub_doctor_wild_rms_arcsec)
    wild = _fl(config, "sub_doctor_wild_rms_arcsec", 2.0)
    if trailing or (soft and (phd2["rms_arcsec"] or 0) >= wild):
        g_ev = {"armed": armed, "phd2_state": phd2["state"],
                "phd2_source": phd2["source"], "phd2_sub_states": phd2["sub_states"],
                "phd2_rms_arcsec": phd2["rms_arcsec"], "phd2_snr": phd2["snr"],
                "phd2_hfd_px": phd2["hfd_px"], "calibration": cal,
                "phd2_guiding": guiding, "phd2_guiding_why": guiding_why,
                "guide_log": ctx.get("guide_log")}
        cal_fail = bool(cal and cal.get("grade") == "FAIL")
        cal_txt = ""
        if cal:
            cal_txt = (f" on a {cal.get('grade')} calibration"
                       + (f" ({cal['reasons'][0]})" if cal.get("reasons") else ""))
        rms_txt = (f", PHD2 RMS {phd2['rms_arcsec']:g}\"" if phd2["rms_arcsec"]
                   is not None else "")
        tiny = _fl(config, "sub_doctor_tiny_rms_arcsec", 0.5)
        if armed == "unguided" and guiding:
            conf = HIGH if (phd2["state"] in ("guiding", "settling") or cal_fail) else MEDIUM
            # 2026-10-13: name what started it (the unguided guard's detect
            # event, or NINA's event history): NINA's meridian flip / an
            # autofocus resuming a guider that was connected
            gs = ctx.get("guider_start") or None
            if gs and gs.get("by"):
                from photonscript.scheduler.unguided_guard import cause_text
                g_ev["guider_start"] = {"by": gs["by"], "t": _iso_t(gs.get("t"))}
                head = (f"{cause_text(gs['by'])} ({_hm(gs.get('t'))}) in an "
                        f"unguided night: PHD2 is {phd2['state']}{cal_txt}{rms_txt}")
                fix = ("NINA #1 had PHD2 connected as its guider, and NINA's "
                       "meridian flip / autofocus resume guiding whenever one "
                       "is connected. Disconnect the guider in NINA #1 "
                       "(unguided_guider_policy=disconnect does it), stop PHD2, "
                       "or re-arm GUIDED after a good calibration near Dec 0 "
                       "and the meridian.")
            else:
                head = f"PHD2 is {phd2['state']} on an UNGUIDED night{cal_txt}{rms_txt}"
                fix = ("Stop PHD2 guiding (PHD2: Stop) so the mount tracks on "
                       "TPoint + ProTrack, and disconnect the guider in NINA #1 "
                       "(its meridian flip / autofocus restart a connected "
                       "guider), or re-arm GUIDED after a good calibration near "
                       "Dec 0 and the meridian.")
            add("guider_conflict", conf,
                f"{head} while {label} subs trail (ecc {ev['ecc']}, HFR "
                f"{ev['hfr_px']} px)", fix,
                _action("stop_phd2", "stop PHD2 guiding", auto=True),
                g_ev)
        elif armed == "guided" and not guiding and trailing:
            add("guider_conflict", MEDIUM,
                f"PHD2 is {phd2['state'] or 'not guiding'} on a GUIDED night while "
                f"{label} subs trail (ecc {ev['ecc']})",
                "Start guiding in PHD2 (or let NINA's next StartGuiding run); if "
                "PHD2 cannot find a star, check the guide camera and focus.",
                _action("start_guiding", "start PHD2 guiding"), g_ev)
        elif guiding and cal_fail:
            add("guider_conflict", HIGH,
                f"PHD2 is guiding{cal_txt}{rms_txt} while {label} subs are bad "
                f"(ecc {ev['ecc']}, HFR {ev['hfr_px']} px)",
                "Recalibrate PHD2 within 20 deg of Dec 0 and 3 h of the meridian "
                "(PS-93 calibration plan), or run unguided.",
                _action("recalibrate_phd2", "recalibrate PHD2"), g_ev)
        elif (guiding and trailing and phd2["rms_arcsec"] is not None
              and phd2["rms_arcsec"] < tiny) or (nonstar and trailing):
            small = phd2["rms_arcsec"] is not None and phd2["rms_arcsec"] < tiny
            lead = (f"PHD2 RMS {phd2['rms_arcsec']}\" looks perfect while {label} subs "
                    f"trail (ecc {ev['ecc']})" if small else
                    f"{label} subs trail (ecc {ev['ecc']}) while the PS-91 guard saw "
                    "PHD2 on a non-star")
            add("guider_conflict", HIGH if nonstar else MEDIUM,
                lead + ": PHD2 is probably locked on a hot pixel or a non-star"
                + (" (PS-91 guard saw a non-star lock)" if nonstar and small else ""),
                "In PHD2 stop, reselect the guide star (Auto Select with the "
                "hot-pixel map / dark library on), then guide again.",
                _action("reselect_guide_star", "reselect the PHD2 guide star"),
                {**g_ev, "nonstar_guard": nonstar})

    # 3. pointing
    pt = sum(1 for s in bs if s["pointing"])
    pse = int(ctx.get("plate_solve_errors") or 0)
    if pt * 2 >= n_b and pt:
        add("pointing", HIGH,
            f"{label} off target on {pt} of {n_b} bad subs",
            "Re-center on the target (NINA Slew and center); if the solve fails "
            "check ASTAP and its star database.",
            _action("check_pointing", "re-center the target"),
            {"off_target_subs": pt, "plate_solve_errors": pse or None})

    # 4. tracking
    if trailing:
        scale = _scale(config, rig)
        trail = trail_estimate(ev["hfr_px"], ev["ecc"], ev["exp_s"], scale)
        axes = [s["axis_deg"] for s in bs]
        rs = [s["axis_r"] for s in bs if s["axis_r"] is not None]
        spread = _axial_spread(axes)
        sky = {}
        if axes and axes[-1] is not None:
            sky = sky_direction(axes[-1], ctx.get("solve"), scale)
        tang = ctx.get("tangential_frac")
        if tang is None:
            tang = next((s["axis_tang"] for s in reversed(bs)
                         if s.get("axis_tang") is not None), None)
        mount = pv.get("mount", 0)
        only = pv.get(f"guider_{rig}", 0)
        if tang is not None and tang >= TANGENTIAL_ROT:
            kind = "field rotation"
        elif sky.get("along") == "RA":
            kind = "RA drift"
        elif sky.get("along") == "Dec":
            kind = "Dec drift"
        else:
            kind = "drift"
        one_way = (spread is not None and spread <= AXIS_SPREAD_MAX) or \
            (rs and _med(rs) >= AXIS_R_MIN)
        conf = (HIGH if (mount and one_way) else
                MEDIUM if (mount or one_way or only) else LOW)
        where = ("both rigs trail together: the shared mount" if mount else
                 f"only {label} trails: its guider or flexure" if only else
                 "the other rig has no overlapping sub")
        rate = trail.get("rate_arcsec_min")
        rec = None
        if rate:
            try:
                from photonscript.scheduler import tracking_drift as td
                rec = td.recommend(config, rig, rate)
            except Exception as e:  # noqa: BLE001
                logger.debug("drift recommend skipped: %s", e)
        new_s = (rec or {}).get("exposure_s")
        cur = ev["exp_s"] or 0
        dirn = (f" along {sky['along']} (sky PA {sky['sky_pa_deg']:g} deg)"
                if sky.get("along") else "")
        pier, ha = _pier_ha(window, ctx.get("pointing"))
        pos = ", ".join(x for x in (f"pier {pier}" if pier else "",
                                    f"HA {ha:+.1f} h" if ha is not None else "") if x)
        if pos:
            dirn += f" ({pos})"
        side = ""
        if pier:
            side = (f"; the mount is on the {pier} side"
                    + (f" at HA {ha:+.1f} h" if ha is not None else "")
                    + ": check the TPoint model has samples on that side (a "
                    "model built on one side only trails after the meridian flip)")
        phd2_txt = ""
        if armed != "guided" and phd2["state"] and not guiding:
            phd2_txt = f"; PHD2 {phd2['state']} but not guiding, so not the guider"
        head = (f"{label} subs trail{dirn}: ecc {ev['ecc']}, ~{trail.get('length_arcsec', '?')}\" "
                f"in {cur:g} s (~{rate if rate is not None else '?'}\"/min); {where}")
        if armed == "unguided" and new_s and new_s < cur - 0.5:
            fix = (f"Shorten {label} subs to {new_s:g} s (drift budget), or check "
                   "TheSky tracking rate / ProTrack and the TPoint model"
                   + ("; Dec drift = polar alignment" if kind == "Dec drift" else "")
                   + ("; field rotation = polar alignment" if kind == "field rotation" else "")
                   + side + phd2_txt)
            act = _action(f"shorten_subs:{new_s:g}", f"cap subs at {new_s:g} s",
                          auto=True)
        elif armed == "guided":
            fix = ("Guided subs trailing: check PHD2 (RMS, star, calibration) and the "
                   "pulse path (PS-167); if only one rig trails, check its flexure.")
            act = _action("check_tracking", "check the guiding")
        else:
            fix = ("Check TheSky tracking rate / ProTrack and the TPoint model"
                   + ("; Dec drift points at polar alignment" if kind == "Dec drift" else "")
                   + ("; if only one rig trails, check that rig's flexure / camera" if only else "")
                   + side + phd2_txt)
            act = _action("check_tracking", "check the mount tracking")
        add("tracking", conf, head, fix, act,
            {"trail": trail, "kind": kind, "axis_deg": axes[-1] if axes else None,
             "axis_spread_deg": spread, "axis_r": _med(rs), "sky": sky or None,
             "shared_mount": bool(mount), "this_rig_only": bool(only),
             "pier": pier, "ha_h": ha, "phd2_state": phd2["state"],
             "phd2_guiding": guiding, "phd2_guiding_why": guiding_why,
             "drift_cap": {k: (rec or {}).get(k) for k in
                           ("exposure_s", "drift_max_s", "why", "budget_arcsec")}
             if rec else None})

    # 5. clouds / transparency
    if stars_low:
        both = pv.get("clouds", 0)
        ratios = [s["stars_ratio"] for s in bs if s["stars_ratio"] is not None]
        add("clouds", HIGH if both else MEDIUM,
            f"{label} star counts down (median {ev['stars']:g} stars"
            + (f", {_med(ratios):g}x the night median" if ratios else "")
            + (")" if not both else "; the other rig too)"),
            ("Clouds: nothing to fix at the rig; the safety monitor closes the roof "
             "if it gets worse." if both else
             "Only this rig: check for dew on its optics (dew heater) or clouds "
             "drifting through its field."),
            _action("wait_clouds", "wait for the sky"),
            {"stars_ratio": _med(ratios), "both_rigs": bool(both),
             "background": _med([s["background"] for s in bs], 1)})

    # 6. seeing vs focus (soft but round)
    if soft and not trailing:
        if pv.get("seeing", 0) * 2 >= n_b and pv.get("seeing"):
            add("seeing", MEDIUM,
                f"{label} soft and round (FWHM {ev['fwhm_arcsec']}\", HFR "
                f"{ev['hfr_px']} px) and the other rig soft at the same time: seeing",
                "Seeing: nothing to fix at the rig. Narrowband or a longer focal "
                "target later in the night may use it better.",
                _action("none", "no action"), {"both_rigs": True})
        else:
            f = ctx.get("focus") or {}
            now = parse_utc(ctx.get("now")) or datetime.utcnow()
            af_t = parse_utc(f.get("last_af_utc"))
            age = round((now - af_t).total_seconds() / 60.0) if af_t else None
            t_af, t_now = _num(f.get("last_af_temp_c")), _num(f.get("temp_c"))
            dt = round(t_now - t_af, 1) if t_af is not None and t_now is not None else None
            lim = _fl(config, "piggyback_af_temp_change_c" if rig == "piggyback"
                      else "rc16_af_temp_change_c", 2.0)
            why = ("temperature moved " + f"{dt:+g} C since the last AF"
                   if dt is not None and abs(dt) >= lim else
                   f"AF last ran {age} min ago" if age is not None else
                   "no AF seen tonight" if f.get("af_known") else
                   "last AF time unknown")
            min_age = _fl(config, "sub_doctor_af_min_age_min", 30.0)
            can_af = age is None or age >= min_age
            add("focus", MEDIUM if pv.get(f"focus_{rig}") else LOW,
                f"{label} focus soft, stars round (FWHM {ev['fwhm_arcsec']}\", HFR "
                f"{ev['hfr_px']} px): {why}"
                + (" (the other rig is sharp)" if pv.get(f"focus_{rig}") else ""),
                "Run autofocus on " + label
                + (f" (AF {age} min ago" + (f" at {t_af:g} C, now {t_now:g} C" if dt is not None else "")
                   + ")" if age is not None else "")
                + ".",
                _action("run_af", "run autofocus", auto=can_af),
                {"af_age_min": age, "af_temp_c": t_af, "temp_c": t_now,
                 "temp_change_c": dt, "other_rig_sharp": bool(pv.get(f"focus_{rig}"))})

    if not found:
        fails = ev["fails"]
        top = ", ".join(f"{k} {v}" for k, v in Counter(fails).most_common(3)) or "score"
        add("other", LOW, f"{label}: {n_b} of {len(live)} subs bad ({top}) for no "
            "cause the doctor knows", "Look at the subs on the Runs page.",
            _action("none", "no action"))
    main, rest = found[0], found[1:]
    w = sub_window(last)
    main.update(rig=rig, file=last.get("file"),
                t=(w[1].strftime("%Y-%m-%dT%H:%M:%SZ") if w else last.get("time")),
                n_window=len(window), n_bad=n_b,
                secondary=[{"cause": x["cause"], "confidence": x["confidence"],
                            "headline": x["headline"]} for x in rest])
    return main


# --------------------------------------------------------------------------
# a whole night (backfill / runs page / CLI)
# --------------------------------------------------------------------------

def _cal_at(history: list[dict], t: datetime | None) -> dict | None:
    """The calibration PHD2 used at t: the newest record at or before it
    (re-reads of the same calibration count)."""
    best = None
    for r in history or []:
        rt = parse_utc(r.get("t_utc"))
        if rt is None or (t is not None and rt > t):
            continue
        if best is None or rt >= parse_utc(best.get("t_utc")):
            best = r
    return best


def _af_events(nina_events: list[dict] | None) -> list[datetime]:
    out = []
    for e in nina_events or []:
        if (e or {}).get("Event") == "AUTOFOCUS-FINISHED":
            t = parse_utc(e.get("Time"))
            if t:
                out.append(t)
    return sorted(out)


def guider_start_at(nina1_events, run_events, until: datetime | None) -> dict | None:
    """2026-10-13: the newest guider start at or before `until`, with what
    started it (unguided_guard.attribute_starts over NINA #1's event
    history, or the guard's own detect run events): {"t", "by"} or None."""
    from photonscript.scheduler import unguided_guard as ug
    cands = [c for c in (ug.latest_start(nina1_events, until=until) if nina1_events
                         else None,
                         ug.start_from_run_events(run_events, until=until)) if c]
    return max(cands, key=lambda c: c["t"]) if cands else None


def window_span(window: list[dict]) -> tuple[datetime | None, datetime | None]:
    ws = [w for w in (sub_window(r) for r in window or []) if w]
    if not ws:
        return None, None
    return min(w[0] for w in ws), max(w[1] for w in ws)


def guide_log(config, night: str, window: list[dict], timeline=None) -> dict | None:
    """2026-10-14: PHD2 guide-log activity over the window's subs
    (phd2_analysis.guide_activity); None when the night has no guide log
    on this machine or it cannot be read."""
    try:
        from photonscript.scheduler import phd2_analysis as pa
        tl = timeline if timeline is not None else pa.cached_timeline(config, night)
        a, b = window_span(window)
        return pa.guide_activity(tl, a, b)
    except Exception as e:  # noqa: BLE001
        logger.debug("sub doctor guide log skipped: %s", e)
        return None


def replay_night(config, night: str, subs: list[dict], *, rig: str | None = None,
                 plan: dict | None = None, armed: str | None = None,
                 cal_history: list[dict] | None = None,
                 events: list[dict] | None = None,
                 nina_events: dict | None = None,
                 solves: dict | None = None, guide_timeline=None,
                 pointing: dict | None = None) -> dict:
    """Run the doctor over a stored night as if live: after each graded
    sub of each rig, the last sub_doctor_window subs. Read only, no push.
    Returns {night, armed, rigs: {rig: {timeline, fired, counts, subs}}}.
    nina_events: {rig: ninaAPI event history} (the AF finish times, and NINA
    #1's GUIDER-START attribution for both rigs); events: the night's run
    events (af_stall / guard non-star / unguided_guard detect);
    guide_timeline: the night's PHD2 guide log (phd2_analysis.GuideTimeline,
    None = unknown); pointing: the PS-67 records {(rig, file): rec}."""
    armed = armed or plan_mode(plan)
    n = window_n(config)
    subs = [sp_norm(r) for r in subs or []]
    bases = sp.baselines(subs)
    rigs = [rig] if rig else sorted({r.get("rig") or "rc16" for r in subs},
                                    key=lambda x: (x != "rc16", x))   # RC16 first
    stalls = {}
    for e in events or []:
        if e.get("kind") == "af_stall":
            stalls.setdefault(e.get("rig") or "rc16", []).append(e)
    out = {"night": night, "armed": armed, "window": n, "rigs": {}}
    pair_cache: dict = {}
    for rg in rigs:
        rs = sp.rig_subs(subs, rg)
        afs = _af_events((nina_events or {}).get(rg))
        solve = (solves or {}).get(rg)
        if solve is None and config is not None:
            solve = latest_solve(config, night, rg)
        timeline, fired, prev = [], [], None
        for i in range(len(rs)):
            win = rs[max(0, i - n + 1):i + 1]
            w = sub_window(win[-1])
            t_end = w[1] if w else None
            last_af = max([a for a in afs if t_end is None or a <= t_end], default=None)
            st = [e for e in stalls.get(rg, [])
                  if t_end is None or (parse_utc(e.get("t")) or t_end) <= t_end]
            ctx = {"armed": armed, "bases": bases, "pair_cache": pair_cache,
                   "calibration": _cal_at(cal_history or [], t_end),
                   "focus": {"last_af_utc": last_af.isoformat() + "Z" if last_af else None,
                             "af_known": rg in (nina_events or {}),
                             "af_stall": st[-1] if st else None},
                   "solve": solve, "now": t_end.isoformat() + "Z" if t_end else None,
                   "guider_start": guider_start_at(
                       (nina_events or {}).get("rc16"), events, t_end),
                   "guide_log": (guide_log(config, night, win, guide_timeline)
                                 if guide_timeline is not None else None),
                   "pointing": (pointing or {}).get((rg, win[-1].get("file")))}
            d = diagnose(config, rg, win, subs=subs, ctx=ctx)
            cause = d["cause"] if d else None
            if d:
                timeline.append(d)
                if cause not in [f["cause"] for f in fired] and d["confidence"] != LOW:
                    fired.append(d)
            elif prev is not None:
                timeline.append({"cause": None, "t": win[-1].get("end_utc")
                                 or win[-1].get("time"), "rig": rg,
                                 "file": win[-1].get("file"), "headline": "healthy again"})
            prev = cause
        out["rigs"][rg] = {"subs": len(rs), "timeline": timeline, "fired": fired,
                           "counts": dict(Counter(d["cause"] for d in timeline
                                                  if d.get("cause")))}
    return out


def sp_norm(rec: dict) -> dict:
    from photonscript.shared.sub_file import norm_record
    try:
        return norm_record(dict(rec))
    except Exception:  # noqa: BLE001
        return rec


def local_night(config, night: str, rig: str | None = None) -> dict:
    """replay_night from this machine's stored records (subs log, plan
    snapshot, PHD2 calibration history, run events)."""
    from photonscript.scheduler import phd2_calibration as pc
    from photonscript.scheduler.runs import _load_subs, runs_dir
    from photonscript.shared import phd2_store as store
    from photonscript.shared.night_events import events_path
    subs = _load_subs(config, night)
    plan = store.read_json(runs_dir(config) / f"{night}_plan.json")
    try:
        hist = pc.history(config)
    except Exception:  # noqa: BLE001
        hist = []
    try:
        from photonscript.scheduler import phd2_analysis as pa
        tl = pa.cached_timeline(config, night)
    except Exception:  # noqa: BLE001
        tl = None
    try:
        from photonscript.shared import pointing as pt
        pts = pt.load(config, night)
    except Exception:  # noqa: BLE001
        pts = {}
    rep = replay_night(config, night, subs, rig=rig, plan=plan, cal_history=hist,
                       events=store.read_jsonl(events_path(config, night)),
                       guide_timeline=tl, pointing=pts)
    try:   # target core clipped with too few HDR shorts (core_check)
        from photonscript.scheduler import core_check
        from photonscript.scheduler.calibration_plan import load_projects
        rep["core"] = [c["message"] for c in core_check.night_notes(
            config, subs, load_projects(config))]
    except Exception as e:  # noqa: BLE001
        logger.debug("sub doctor core notes skipped: %s", e)
    st = load_state(config, night)
    if st.get("rigs"):
        rep["live"] = st
    return rep


# --------------------------------------------------------------------------
# live: state, run events, push and remedy decisions
# --------------------------------------------------------------------------

def state_path(config, night: str) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "sub_doctor" / f"{night}.json"


def load_state(config, night: str) -> dict:
    from photonscript.shared import phd2_store as store
    return store.read_json(state_path(config, night)) or {"night": night, "rigs": {}}


def save_state(config, night: str, st: dict) -> None:
    from photonscript.shared import phd2_store as store
    try:
        store.write_json(state_path(config, night), st)
    except OSError as e:
        logger.warning("sub doctor state not saved: %s", e)


def event_record(diag: dict | None, rig: str, now: datetime) -> dict:
    """The run event (kind sub_doctor) for a diagnosis, or the "none" event
    when the rig is healthy again. Stable schema: EVENT_FIELDS."""
    from photonscript.shared.phd2_store import iso_z
    d = diag or {}
    cause = d.get("cause") or "none"
    return {"t": iso_z(now), "rig": rig, "src": "photonscript", "kind": EVENT_KIND,
            "value": cause, "cause": cause, "confidence": d.get("confidence"),
            "headline": d.get("headline") or ("healthy again" if not diag else None),
            "evidence": d.get("evidence") or {}, "fix": d.get("fix"),
            "suggested_action": d.get("action") or _action("none", "no action"),
            "secondary": [x.get("cause") for x in d.get("secondary") or []],
            "file": d.get("file"), "n_window": d.get("n_window"), "n_bad": d.get("n_bad")}


def push_text(config, diag: dict) -> str:
    ev = diag.get("evidence") or {}
    bits = [diag["headline"] + "."]
    nums = [f"{k} {ev[k]}" for k in ("ecc", "hfr_px", "fwhm_arcsec", "stars")
            if ev.get(k) is not None]
    if nums:
        bits.append(f"Last {ev.get('window')} subs ({ev.get('bad')} bad): "
                    + ", ".join(nums) + ".")
    bits.append("Fix: " + diag["fix"])
    a = diag.get("action") or {}
    if a.get("auto"):
        try:
            from photonscript.scheduler.night_pilot import mode as pilot_mode
            pm = pilot_mode(config)
        except Exception:  # noqa: BLE001
            pm = "?"
        bits.append(f"Remedy {a['id']}: the night pilot's ({pm}).")
    return " ".join(bits)


def record(config, night: str, rig: str, diag: dict | None,
           now: datetime | None = None) -> dict:
    """Keep the state, write the run event, decide the push. Returns
    {"event": rec or None, "push": text or None, "remedy": None} ("remedy"
    stays for callers: the night pilot is the one actor now). mode off:
    nothing. observe: state + events, no push."""
    from photonscript.shared import phd2_store as store
    from photonscript.shared.night_events import events_path
    now = now or datetime.utcnow()
    m = mode(config)
    out = {"event": None, "push": None, "remedy": None}
    if m == "off":
        return out
    st = load_state(config, night)
    r = st.setdefault("rigs", {}).setdefault(rig, {"pushed": [], "remedies": []})
    prev = (r.get("current") or {}).get("cause")
    if diag is None and prev is None:
        return out
    ev = event_record(diag, rig, now)
    try:
        store.append_jsonl(events_path(config, night), ev)
        out["event"] = ev
    except Exception as e:  # noqa: BLE001
        logger.warning("sub doctor run event not written: %s", e)
    if diag is None:
        r["current"] = None
        r["cleared"] = ev["t"]
        save_state(config, night, st)
        return out
    cur = {k: diag.get(k) for k in ("cause", "confidence", "headline", "fix", "t",
                                    "file", "action", "evidence", "secondary")}
    if prev != diag["cause"]:
        cur["since"] = ev["t"]
    else:
        cur["since"] = (r.get("current") or {}).get("since") or ev["t"]
    r["current"] = cur
    r.setdefault("history", []).append({"t": ev["t"], "cause": diag["cause"],
                                        "confidence": diag["confidence"]})
    r["history"] = r["history"][-50:]
    if m == "alert" and diag["confidence"] != LOW and diag["cause"] not in r["pushed"]:
        r["pushed"].append(diag["cause"])
        out["push"] = push_text(config, diag)
    save_state(config, night, st)
    return out


# --------------------------------------------------------------------------
# report lines
# --------------------------------------------------------------------------

def night_lines(config, rep: dict) -> list[str]:
    """'What went wrong and the fix' lines: per rig, each cause the first
    time it fired (time, confidence, headline, fix)."""
    lines = []
    for rg, r in (rep.get("rigs") or {}).items():
        for d in r.get("fired") or []:
            lines.append(f"{_label(config, rg)} {_hm(d.get('t'))} {d['cause']} "
                         f"({d['confidence']}): {d['headline']}. Fix: {d['fix']}")
    return lines


def morning_line(config, date: str) -> str | None:
    """The morning report line (None for a healthy night)."""
    try:
        rep = local_night(config, date)
    except Exception as e:  # noqa: BLE001
        logger.debug("sub doctor morning line skipped: %s", e)
        return None
    parts = []
    for rg, r in (rep.get("rigs") or {}).items():
        f = r.get("fired") or []
        if f:
            parts.append(f"{_label(config, rg)}: " + "; ".join(
                f"{d['cause']} from {_hm(d.get('t'))} (fix: {d['fix']})" for d in f[:3]))
    return ("What went wrong and the fix: " + " | ".join(parts)) if parts else None


def format_report(config, rep: dict) -> str:
    out = [f"Sub doctor, night of {rep.get('night')} (armed {rep.get('armed') or '?'}, "
           f"window {rep.get('window')} subs)"]
    for rg, r in (rep.get("rigs") or {}).items():
        out.append(f"\n{_label(config, rg)}: {r['subs']} subs; diagnoses "
                   + (", ".join(f"{k} x{v}" for k, v in r["counts"].items()) or "none"))
        for d in r.get("fired") or []:
            out.append(f"  {_hm(d.get('t'))} {d['cause']} ({d['confidence']}): "
                       f"{d['headline']}")
            out.append(f"      fix: {d['fix']}  [action {d['action']['id']}]")
            for s in d.get("secondary") or []:
                out.append(f"      also: {s['cause']} ({s['confidence']})")
    for line in rep.get("core") or []:
        out.append(f"\nCore clipped, too few short subs: {line}")
    if rep.get("note"):
        out.append(rep["note"])
    return "\n".join(out)
