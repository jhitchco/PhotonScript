"""Sky flats scheduled by sun altitude, dusk and dawn (2026-10-24).

The sky flat windows used to be fixed offsets (dusk: sunset + 15, the OSC
sunset + 20; dawn: nautical dawn + 5, the OSC + 20) with one NINA SkyFlat
try per filter, 0.1-30 s. None of the clear windows of 09-09 .. 10-23 saved
a flat: at dawn every set started 30-45 min before the sky could fill it
in 30 s, at dusk the narrowband needed 30x the light the sky had left.

Now each rig / filter has a window: the stretch of twilight in which
sky_flat_model says a target exposure in flats_target_exposure_min_s ..
_max_s (2 .. 10 s) fills the histogram to the target. At DUSK the sky
darkens, so a window opens when the sun is high enough for the shortest
exposure and closes when even the longest one is too dim: the filters that
need the most light (narrowband) open first, then R / G / B, then L. At
DAWN the sky brightens, so the order reverses: L, B, G, R, then narrowband.
The RC16 shoots its owed filters one after the other, each from the later
of its window start and the previous set's end; a filter whose window has
passed by then (a late roof, a long earlier set) is skipped and stays owed
for the other twilight (flats_when both). The Piggy-600's OSC set has its
own window and runs in parallel (its own camera), riding the RC16 pointing.

Pointing: the flattest twilight sky is about 15-20 deg from the zenith on
the side opposite the sun, so both rigs point at alt flats_point_alt_deg
(75) and the sun's azimuth + 180 at the middle of the window (dusk: the
sun sets in the west, so the flat spot is in the east; dawn the other way),
recomputed for every window.

Times:
  dusk  ready = when the routine can shoot (dispatch + setup, or the roof);
        hard end = dusk_flat_times()["stop"] (before nautical dusk / the
        night's pre-config), so the night is never delayed.
  dawn  ready = nautical dawn (the End area / companion is cooled and
        pointed by then); hard end = sunrise +
        flats_dawn_end_after_sunrise_min (20; the roof stays open on clear
        mornings until about sunrise + 30). The armer's dawn backstop
        follows the stored dawn plan's end (armer._shutdown_due_at).

plan(...) returns a dict; save / load_plan keep it under
<data_dir>/flat_plans/<night>_<phase>.json for the window report.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import sky_flat_model as sfm

logger = logging.getLogger(__name__)

PLANS_DIR = "flat_plans"
SUNSET_ALT = -0.833
NAUT_ALT = -12.0
ASTRO_ALT = -18.0
SEARCH_S = 45            # NINA's exposure search before the first frame
RC16_DUSK_ORDER = ("Ha", "OIII", "SII", "R", "G", "B", "L")


# ---- twilight (no astropy: the NOAA sun of sky_flat_model) --------------------

def _off(config, utc: datetime) -> float:
    from photonscript.shared.localtime import utc_offset_hours
    return utc_offset_hours(config, utc)


def local_to_utc(config, local: datetime) -> datetime:
    return local - timedelta(hours=_off(config, local + timedelta(hours=7)))


def to_local(config, utc: datetime) -> datetime:
    return utc + timedelta(hours=_off(config, utc))


def spans(config, night: str) -> tuple:
    """((dusk start, dusk end), (dawn start, dawn end)) naive UTC: the
    stretches of the night of `night` on which the sun only sinks (13:30 to
    23:59 local; solar noon at AARO is about 12:15-13:15) and only climbs
    (00:30 to 11:30 the next morning)."""
    d = datetime.strptime(night, "%Y-%m-%d")
    return ((local_to_utc(config, d.replace(hour=13, minute=30)),
             local_to_utc(config, d.replace(hour=23, minute=59))),
            (local_to_utc(config, d.replace(hour=0, minute=30) + timedelta(days=1)),
             local_to_utc(config, d.replace(hour=11, minute=30) + timedelta(days=1))))


def twilight(config, night: str) -> dict:
    """Naive-UTC twilight of the night that starts on the local evening
    `night` (YYYY-MM-DD): sunset, naut_dusk, astro_dusk, astro_dawn,
    naut_dawn, sunrise, plus the monotonic search spans dusk_span /
    dawn_span."""
    (e0, e1), (m0, m1) = spans(config, night)
    out = {"night": night, "dusk_span": (e0, e1), "dawn_span": (m0, m1)}
    for k, alt, (a, b) in (("sunset", SUNSET_ALT, (e0, e1)),
                           ("naut_dusk", NAUT_ALT, (e0, e1)),
                           ("astro_dusk", ASTRO_ALT, (e0, e1)),
                           ("astro_dawn", ASTRO_ALT, (m0, m1)),
                           ("naut_dawn", NAUT_ALT, (m0, m1)),
                           ("sunrise", SUNSET_ALT, (m0, m1))):
        out[k] = sfm.time_at_alt(config, alt, a, b)
    return out


def _cross(config, alt: float, span: tuple) -> datetime:
    """When the sun crosses `alt` inside the monotonic span, clamped to the
    span's ends (an altitude the sun is already past = the span start)."""
    t0, t1 = span
    a0, a1 = sfm.sun_alt(config, t0), sfm.sun_alt(config, t1)
    lo, hi = min(a0, a1), max(a0, a1)
    if alt >= hi:
        return t0 if a0 >= a1 else t1
    if alt <= lo:
        return t1 if a0 >= a1 else t0
    return sfm.time_at_alt(config, alt, t0, t1) or t0


# ---- windows -------------------------------------------------------------------

def nina_exposure_bounds(config, phase: str = "dawn") -> tuple[float, float]:
    """MinExposure / MaxExposure for NINA's SkyFlat: wider than the planned
    range so a set that starts near a window edge still converges."""
    lo, hi = sfm.exposure_range(config, phase)
    cap = float(getattr(config, "flats_nina_max_exposure_s", 30.0) or 30.0)
    return round(max(0.1, lo / 2), 2), round(min(cap, max(hi * 3, hi + 1)), 2)


def filter_window(config, rig_fit: dict, flt: str, phase: str, tw: dict) -> dict | None:
    """{start, end, alt_start, alt_end, exp_start, exp_end} (naive UTC) of a
    filter's window, or None when the model has no intercept for it."""
    lo, hi = sfm.exposure_range(config, phase)
    a_bright = sfm.alt_for_exposure(rig_fit, flt, lo)
    a_dim = sfm.alt_for_exposure(rig_fit, flt, hi)
    if a_bright is None or a_dim is None:
        return None
    span = tw["dusk_span"] if phase == "dusk" else tw["dawn_span"]
    if phase == "dusk":    # darkening: short exposures first
        a_s, a_e, x_s, x_e = a_bright, a_dim, lo, hi
    else:                  # brightening: long exposures first
        a_s, a_e, x_s, x_e = a_dim, a_bright, hi, lo
    return {"start": _cross(config, a_s, span), "end": _cross(config, a_e, span),
            "alt_start": round(a_s, 2), "alt_end": round(a_e, 2),
            "exp_start": x_s, "exp_end": x_e}


def set_seconds(config, n: int, phase: str = "dawn") -> float:
    """A set's planned length: NINA's search plus n frames of the reference
    exposure (the range's geometric mean) and the per-frame overhead
    (download + save: ~1 s on the RC16, 09-08)."""
    lo, hi = sfm.exposure_range(config, phase)
    over = float(getattr(config, "flats_frame_overhead_s", 2.0) or 0.0)
    return SEARCH_S + n * (math.sqrt(lo * hi) + over)


def _schedule(config, wins: list[tuple[str, dict]], ready: datetime,
              hard_end: datetime, set_s: float, phase: str, attempts: int):
    """Sequential slots. Greedy: of the filters whose window is already
    open at the cursor, the one whose window closes first (so a late roof
    saves what it still can); when none is open yet, the next to open. At
    dusk that is the natural order narrowband, R / G / B, L; at dawn L,
    B / G / R, narrowband. A set may start up to half a set before its
    window closes (NINA's wider exposure bounds carry the rest); a filter
    that cannot start by then, or by the hard end, is skipped (owed)."""
    nmin, nmax = nina_exposure_bounds(config, phase)
    slots, skipped = [], {}
    cursor = ready
    half = timedelta(seconds=set_s / 2)
    todo = list(wins)
    while todo:
        fits = [(f, w) for f, w in todo
                if max(w["start"], cursor) <= min(w["end"], hard_end) - half]
        for f, w in [fw for fw in todo if fw not in fits]:
            if w["end"] <= ready + half:
                why = (f"window passed (closed {_hhmm(config, w['end'])}, ready "
                       f"{_hhmm(config, ready)})")
            elif w["start"] >= hard_end - half:
                why = f"window opens after the {_hhmm(config, hard_end)} end"
            else:
                why = (f"no time left in its window ({_hhmm(config, w['start'])}-"
                       f"{_hhmm(config, w['end'])}) after the sets before it")
            skipped[f] = why
        if not fits:
            break
        open_now = [fw for fw in fits if fw[1]["start"] <= cursor]
        flt, w = (min(open_now, key=lambda fw: (fw[1]["end"], fw[0])) if open_now
                  else min(fits, key=lambda fw: (fw[1]["start"], fw[0])))
        todo = [fw for fw in fits if fw[0] != flt]
        start = max(w["start"], cursor)
        end = start + timedelta(seconds=set_s)
        # the set's own stop (a TimeCondition in NINA): its window end plus
        # half a set, never past the hard end
        stop = min(hard_end, max(end, w["end"] + half))
        slots.append({"filter": flt, "start": start, "est_end": end, "stop": stop,
                      "window": [w["start"], w["end"]],
                      "alt_window": [w["alt_start"], w["alt_end"]],
                      "min_exp": nmin, "max_exp": nmax, "attempts": attempts})
        cursor = end
    return slots, skipped


def plan(config, phase: str, night: str, rc16_filters, osc: bool = False, *,
         ready: datetime | None = None, model: dict | None = None,
         tw: dict | None = None) -> dict:
    """The flat plan of one twilight window.

    phase "dusk" | "dawn"; night the local evening date; rc16_filters the
    RC16's owed filters (any order); osc the Piggy-600's OSC set owed;
    ready (naive UTC) when the routine can start shooting (default: dusk
    sunset - 60 min, i.e. no constraint; dawn nautical dawn)."""
    tw = tw or twilight(config, night)
    m = model or sfm.load(config)
    if phase == "dusk":
        from photonscript.scheduler.calibration import dusk_flat_times
        sunset = tw.get("sunset") or tw["dusk_span"][0]
        try:
            hard_end = (dusk_flat_times(config, sunset) or {}).get("stop")
        except Exception:  # noqa: BLE001
            hard_end = None
        hard_end = hard_end or ((tw.get("naut_dusk") or sunset + timedelta(minutes=55))
                                - timedelta(minutes=8))
        default_ready = sunset - timedelta(minutes=60)
        attempts = max(1, int(getattr(config, "flats_dusk_attempts", 2) or 1))
    else:
        rise = tw.get("sunrise") or tw["dawn_span"][1]
        hard_end = rise + timedelta(minutes=int(getattr(
            config, "flats_dawn_end_after_sunrise_min", 20) or 0))
        default_ready = tw.get("naut_dawn") or tw["dawn_span"][0]
        from photonscript.scheduler.calibration import dawn_flat_attempts
        attempts = dawn_flat_attempts(config)
    if phase == "dawn":
        ready = max(ready, default_ready) if ready else default_ready
    else:
        ready = ready or default_ready
    n_rc = int(getattr(config, "flat_count", 15) or 15)
    n_pb = int(getattr(config, "piggyback_flat_count", 25) or 25)
    out = {"phase": phase, "night": night, "ready": ready, "hard_end": hard_end,
           "exposure_range_s": list(sfm.exposure_range(config, phase)),
           "model": m.get("source", "defaults"), "rigs": {}}
    rc_fit = sfm.rig_fit(config, "rc16", m)
    order = list(RC16_DUSK_ORDER)
    wanted = [f for f in order if f in set(rc16_filters or [])]
    wins, nomodel = [], {}
    for f in wanted:
        w = filter_window(config, rc_fit, f, phase, tw)
        if w is None:
            nomodel[f] = "no sky model for this filter"
        else:
            wins.append((f, w))
    slots, skipped = _schedule(config, wins, ready, hard_end,
                               set_seconds(config, n_rc, phase), phase, attempts)
    skipped.update(nomodel)
    out["rigs"]["rc16"] = {"count": n_rc, "slots": slots, "skipped": skipped}
    if osc:
        pb_fit = sfm.rig_fit(config, "piggyback", m)
        w = filter_window(config, pb_fit, sfm.OSC, phase, tw)
        if w is None:
            out["rigs"]["piggyback"] = {"count": n_pb, "slots": [],
                                        "skipped": {sfm.OSC: "no sky model"}}
        else:
            ps, pk = _schedule(config, [(sfm.OSC, w)], ready, hard_end,
                               set_seconds(config, n_pb, phase), phase, attempts)
            out["rigs"]["piggyback"] = {"count": n_pb, "slots": ps, "skipped": pk}
    all_slots = [s for r in out["rigs"].values() for s in r["slots"]]
    if all_slots:
        first = min(s["start"] for s in all_slots)
        last = max(s["stop"] for s in all_slots)
        out["first_start"], out["end"] = first, last
        out["hold_until"] = max(s["stop"] for s in all_slots)
        out["pointing"] = pointing(config, first + (last - first) / 2)
    else:
        out["first_start"] = out["end"] = out["hold_until"] = None
        out["pointing"] = None
    return out


def pointing(config, when_utc: datetime) -> dict:
    """The twilight flat spot at `when_utc`: alt flats_point_alt_deg, the
    azimuth opposite the sun."""
    _alt, az = sfm.sun_alt_az(*sfm.site(config), when_utc)
    alt = float(getattr(config, "flats_point_alt_deg", 75.0) or 75.0)
    return {"alt": round(min(85.0, max(45.0, alt)), 1),
            "az": round((az + 180.0) % 360.0, 1), "sun_az": round(az, 1),
            "at": when_utc}


# ---- text / storage ------------------------------------------------------------

def _hhmm(config, utc: datetime | None) -> str:
    return to_local(config, utc).strftime("%H:%M") if utc else "--:--"


def describe(config, p: dict) -> str:
    """"RC16 L 18:41-18:44 (sun -2.3..-4.1), B ...; Piggy-600 OSC 18:40;
    skipped Ha (window passed ...); point alt 75 az 78"."""
    from photonscript.shared.rigs import rig_label
    bits = []
    for rig, r in p["rigs"].items():
        try:
            name = rig_label(config, rig)
        except Exception:  # noqa: BLE001
            name = rig
        sl = ", ".join(f"{s['filter']} {_hhmm(config, s['start'])}" for s in r["slots"])
        sk = "; ".join(f"{f}: {why}" for f, why in r["skipped"].items())
        bits.append(f"{name} " + (sl or "none") + (f" (skipped {sk})" if sk else ""))
    pt = p.get("pointing")
    return ("; ".join(bits)
            + (f"; point alt {pt['alt']:g} az {pt['az']:g}" if pt else ""))


def _jsonable(x):
    if isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def _parse(x):
    if isinstance(x, str) and 19 <= len(x) <= 26 and x[4] == "-" and x[10] == "T":
        try:
            return datetime.fromisoformat(x)
        except ValueError:
            return x
    if isinstance(x, dict):
        return {k: _parse(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_parse(v) for v in x]
    return x


def plan_path(config, night: str, phase: str) -> Path:
    return Path(config.data_dir) / PLANS_DIR / f"{night}_{phase}.json"


def save_plan(config, p: dict, **extra) -> None:
    """Store the plan (with extra fields, e.g. dispatched rigs) for the
    window report (two small files a night; nothing is pruned)."""
    try:
        path = plan_path(config, p["night"], p["phase"])
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _jsonable({**p, **extra,
                          "saved": datetime.utcnow().isoformat(timespec="seconds")})
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001 - never break a dispatch on this
        logger.warning("flat plan save failed: %s", e)


def load_plan(config, night: str, phase: str) -> dict | None:
    try:
        return _parse(json.loads(plan_path(config, night, phase).read_text(
            encoding="utf-8")))
    except (OSError, ValueError):
        return None


def mark(config, night: str, phase: str, **fields) -> None:
    """Add fields (e.g. reported=True) to a stored plan."""
    p = load_plan(config, night, phase)
    if p is not None:
        save_plan(config, {**p, **fields})
