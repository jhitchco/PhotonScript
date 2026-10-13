"""RC16 focus table: one focuser position per filter at a temperature, from
the PS-76 focus model and same-night autofocus pairs.

Why this exists
---------------
2026-10-13 (Jeremy): "We probably have enough calibration data on autofocus
that we can start using some tables." The PS-76 model already fits
position = a[filter] + b * T over ~300 RC16 AFs (slope -8.25 steps/C, fit
scatter 52 steps, CFZ 110 steps). The table turns it into the numbers the
sequence can drive to:

    position(filter, T) = intercept_L + slope * T + offset[filter]

intercept_L and slope come from the model (current focuser counter frame,
focus_model.counter_frame). offset[filter] is measured from same-night AF
pairs: an AF in the reference filter (autofocus_filter, L) followed by an
AF in the filter within PAIR_MAX_MIN minutes and PAIR_MAX_DT_C degrees, the
difference corrected by slope x (temperature difference). A pair is immune
to a counter shift between nights and to the slope. With fewer than
MIN_PAIRS pairs the model's own offset (intercept difference, every AF of
the filter at any temperature) stands in when it has >= 3 AFs per side,
then the configured focus_filter_offsets, then 0.

Each row has a confidence and a drive verdict. A filter is drive-ready
(focus_model_drive "drive" moves it to the table instead of running AF)
only when: the model is trusted (focus_model.trust: fit scatter under half
the CFZ, >= 8 reference AFs, a fitted slope), the row is "high"
confidence, the filter has >= MIN_OWN_AFS AFs of its own, its newest AF is
at most RECENT_DAYS older than the newest RC16 AF and landed within one
CFZ of the table, and no focuser counter shift is waiting for its
confirming AF.

Temperature
-----------
The RC16 EAF thermometer freezes (21.29 C for hours, the same value while
the device was missing). choose_temperature() uses the NINA weather
(AARO station) ambient instead when the focuser reads a known frozen value
(focus_frozen_temps) or has not changed for FROZEN_STALE_MIN minutes while
the ambient moved more than AMBIENT_MOVE_C, corrected by the learned
focuser-minus-ambient bias, and says which source it used. State lives in
<data_dir>/focus_temp_track.json.

Payoff
------
payoff() replays last week's AFs through drive_decision(): which of them
drive mode would still have run (night start, the timed verify AF, a
filter that is not drive-ready, an AF the table missed by more than one
CFZ) and what the others cost in minutes and shutter time.

Read-only except the temperature track file. Nothing here talks to NINA.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import focus_model as fm

logger = logging.getLogger(__name__)

FILTERS = ("L", "R", "G", "B", "Ha", "OIII", "SII")
PAIR_MAX_MIN = 30.0      # reference AF then filter AF within this many minutes
PAIR_MAX_DT_C = 1.0      # ... and this temperature difference
MIN_PAIRS = 3            # pairs before the pair offset is the table offset
MIN_MODEL_AFS = 3        # af_policy.MIN_OFFSET_AFS: model offset counts
MIN_OWN_AFS = 5          # drive: AFs of the filter's own (drive_filters)
RECENT_DAYS = 14         # drive: newest filter AF vs newest RC16 AF
HIGH_SE_CFZ = 0.25       # high confidence: offset standard error <= CFZ/4
MOVE_TEMP_CFZ = 0.5      # drive: re-move after drift of half a CFZ
MOVE_S = 20.0            # payoff: one table move (script + EAF travel), s
TEMP_TRACK_FILE = "focus_temp_track.json"
FROZEN_STALE_MIN = 60.0
AMBIENT_MOVE_C = 1.0
_BIAS_SAMPLES = 30


def _num(v):
    return fm._num(v)


# --------------------------------------------------------------------------
# Offsets from same-night pairs
# --------------------------------------------------------------------------

def pair_offsets(pts: list[dict], ref: str = "L", slope: float | None = None,
                 max_min: float = PAIR_MAX_MIN,
                 max_dt: float = PAIR_MAX_DT_C) -> dict:
    """Per filter, every same-night pair: the newest reference AF at most
    max_min minutes BEFORE the filter AF with |dT| <= max_dt (both
    temperatures known). delta = filter position - reference position -
    slope x (T_filter - T_ref). All nights pooled. {filter: {steps (median),
    n, se, spread, nights, last_night, deltas}}."""
    rows = []
    for p in pts or []:
        t = fm._when(p)
        pos, temp = _num(p.get("position")), _num(p.get("temp"))
        if t is None or pos is None or temp is None or not p.get("filter"):
            continue
        rows.append((t, p["filter"], pos, temp))
    rows.sort()
    refs = [r for r in rows if r[1] == ref]
    per: dict = {}
    for t, f, pos, temp in rows:
        if f == ref:
            continue
        cand = [r for r in refs if 0 <= (t - r[0]).total_seconds()
                <= max_min * 60 and abs(temp - r[3]) <= max_dt]
        if not cand:
            continue
        r = max(cand, key=lambda q: q[0])
        d = pos - r[2] - (slope or 0.0) * (temp - r[3])
        night = (t - timedelta(hours=12)).date().isoformat()
        per.setdefault(f, []).append((night, d))
    out = {}
    for f, items in per.items():
        ds = [d for _, d in items]
        n = len(ds)
        med = fm._median(ds)
        sig = fm._robust_sigma(ds) if n >= 3 else None
        if n >= 2 and not sig:
            mean = sum(ds) / n
            sig = math.sqrt(sum((d - mean) ** 2 for d in ds) / (n - 1))
        out[f] = {"steps": round(med), "n": n,
                  "se": (round(sig / math.sqrt(n), 1) if sig else None),
                  "spread": round(max(ds) - min(ds)),
                  "nights": sorted({nt for nt, _ in items}),
                  "last_night": max(nt for nt, _ in items),
                  "deltas": [round(d) for d in ds]}
    return out


def _offset_for(f: str, ref: str, pairs: dict, model: dict, conf: dict,
                cfz: float | None) -> dict:
    """The table offset for one filter and where it came from."""
    if f == ref:
        return {"steps": 0, "source": "reference", "se": 0.0, "n_pairs": 0}
    pr = pairs.get(f)
    mo = (model.get("offsets") or {}).get(f) or {}
    n_pairs = pr["n"] if pr else 0
    note = None
    if pr and n_pairs >= MIN_PAIRS:
        out = {"steps": pr["steps"], "source": "pairs", "se": pr["se"]}
    elif mo.get("steps") is not None and int(mo.get("n") or 0) >= \
            MIN_MODEL_AFS and int(mo.get("n_ref") or 0) >= MIN_MODEL_AFS:
        out = {"steps": int(mo["steps"]), "source": "model",
               "se": mo.get("se")}
        if pr and cfz and abs(pr["steps"] - int(mo["steps"])) > cfz:
            note = (f"{n_pairs} same-night pair(s) say {pr['steps']:+d}, "
                    f"{abs(pr['steps'] - int(mo['steps']))} steps from the "
                    "model offset")
    elif pr:
        out = {"steps": pr["steps"], "source": "pairs (few)", "se": pr["se"]}
    elif f in conf:
        out = {"steps": int(conf[f]) - int(conf.get(ref, 0)),
               "source": "config", "se": None}
    else:
        out = {"steps": 0, "source": "none", "se": None}
    out["n_pairs"] = n_pairs
    if note:
        out["note"] = note
    return out


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------

def _newest(pts):
    ts = [str(p.get("time") or "")[:19] for p in pts if p.get("time")]
    return max(ts) if ts else None


def _days_between(a: str, b: str) -> float | None:
    try:
        ta = datetime.strptime(a[:19], "%Y-%m-%dT%H:%M:%S")
        tb = datetime.strptime(b[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return abs((tb - ta).total_seconds()) / 86400


def build_table(config, temp=None, temp_basis: str | None = None,
                pts: list[dict] | None = None) -> dict:
    """The RC16 focus table at `temp` (default: focus_model.expected_temp,
    the first AF temperature of the last night). Never raises (an error key
    instead). See the module docstring for the rules."""
    try:
        return _build(config, temp, temp_basis, pts)
    except Exception as e:  # noqa: BLE001 - a read-out never breaks a page
        logger.warning("focus_table: build failed: %s", e)
        return {"error": str(e), "filters": {}, "drive_ready": []}


def _build(config, temp, temp_basis, pts) -> dict:
    ref = fm._ref_filter(config)
    pts = fm._rc16_points(config) if pts is None else pts
    model = fm.fit(pts, ref_filter=ref)
    tr = fm.trust(config, model, pts)
    cfz, cfz_src = fm.cfz_steps(config, pts)
    counter = fm.counter_status(config)
    t = _num(temp)
    if t is None:
        et = fm.expected_temp(config)
        if et:
            t, temp_basis = et["temp"], et["basis"]
        else:
            temp_basis = "no temperature known"
    else:
        temp_basis = temp_basis or "request"
    slope = model.get("slope_steps_per_c")
    b = slope or 0.0
    rows = model.get("filters") or {}
    ref_row = rows.get(ref)
    try:
        conf = config.focus_offset_map()
    except Exception:  # noqa: BLE001
        conf = {}
    pairs = pair_offsets(pts, ref, slope)
    newest = _newest(pts)
    tr_lo, tr_hi = (model.get("temp_range") or [None, None])
    out_rows = {}
    order = list(FILTERS) + sorted(f for f in rows if f not in FILTERS)
    for f in order:
        own = rows.get(f) or {}
        off = _offset_for(f, ref, pairs, model, conf, cfz)
        row = {"filter": f, "offset": off["steps"],
               "offset_source": off["source"], "offset_se": off.get("se"),
               "n_pairs": off["n_pairs"], "n_afs": int(own.get("n") or 0),
               "position": None, "confidence": "none",
               "drive_ready": False, "drive_reasons": []}
        if off.get("note"):
            row["note"] = off["note"]
        if ref_row is None:
            row["drive_reasons"] = [f"no {ref} AF points"]
            out_rows[f] = row
            continue
        base = ref_row["intercept"] + b * (t if t is not None
                                           else ref_row["median_temp"])
        row["position"] = int(round(base + off["steps"]))
        row["intercept"] = round(ref_row["intercept"] + off["steps"], 1)
        # confidence
        if off["source"] == "reference":
            c = ("high" if tr["trusted"] else
                 "med" if ref_row["n"] >= 3 else "low")
        elif off["source"] in ("pairs", "model"):
            se = _num(off.get("se"))
            c = ("high" if tr["trusted"] and se is not None and cfz
                 and se <= HIGH_SE_CFZ * cfz else "med")
        elif off["source"] == "pairs (few)":
            c = "low"
        elif off["source"] == "config":
            c = "low"
        else:
            c = "none"
        # outside the fitted temperatures (+/- 3 C) the table extrapolates;
        # with no temperature (a sequence built before the focuser is
        # connected) the row keeps its confidence and the move endpoint
        # checks the live temperature
        if c == "high" and (slope is None or tr_lo is None or (
                t is not None and not (tr_lo - fm._EXTRAP_MARGIN_C <= t
                                       <= tr_hi + fm._EXTRAP_MARGIN_C))):
            c = "med"
        row["confidence"] = c
        # the filter's newest AF against the table
        mine = [p for p in pts if p.get("filter") == f
                and _num(p.get("position")) is not None
                and _num(p.get("temp")) is not None]
        last = max(mine, key=lambda p: str(p.get("time") or "")) if mine \
            else None
        if last is not None:
            pred = ref_row["intercept"] + b * float(last["temp"]) + off["steps"]
            row["last_af"] = {"time": last.get("time"),
                              "position": int(round(float(last["position"]))),
                              "temp": last.get("temp"),
                              "table": int(round(pred)),
                              "residual": int(round(float(last["position"])
                                                    - pred))}
        # drive checks
        why = []
        if not tr["trusted"]:
            why += tr["reasons"] or ["model not trusted"]
        if c != "high":
            why.append(f"confidence {c} (needs high)")
        if row["n_afs"] < MIN_OWN_AFS:
            why.append(f"{row['n_afs']} AF(s) of its own, needs "
                       f"{MIN_OWN_AFS}")
        if last is None:
            why.append("no AF in this filter")
        else:
            age = _days_between(str(last.get("time") or ""), newest or "")
            if age is None or age > RECENT_DAYS:
                why.append(f"newest AF {str(last.get('time'))[:10]} is "
                           f"{age:.0f} days older than the newest RC16 AF "
                           f"(max {RECENT_DAYS})" if age is not None else
                           "newest AF has no time")
            if cfz and abs(row["last_af"]["residual"]) > cfz:
                why.append(f"newest AF landed {row['last_af']['residual']:+d}"
                           f" steps from the table (more than one CFZ, "
                           f"{round(cfz)})")
        if counter.get("pending"):
            why.append("focuser counter shift waiting for a confirming AF")
        row["drive_reasons"] = why
        row["drive_ready"] = not why
        out_rows[f] = row
    ready = [f for f in order if out_rows[f]["drive_ready"]]
    step_c = move_temp_step_c(slope, cfz)
    measured = {f: r["offset"] for f, r in out_rows.items()
                if f in ("Ha", "OIII", "SII")
                and r["offset_source"] in ("pairs", "model", "pairs (few)")}
    proposal = dict((f, int(conf.get(f, 0))) for f in conf)
    proposal.update(measured)
    return {
        "rig": "rc16", "ref_filter": ref, "temp": t, "temp_basis": temp_basis,
        "slope_steps_per_c": slope, "slope_se": model.get("slope_se"),
        "intercept_ref": (round(ref_row["intercept"], 1) if ref_row
                          else None),
        "sigma_steps": model.get("sigma_steps"),
        "n_points": model.get("n_points"),
        "temp_range": model.get("temp_range"),
        "cfz_steps": (round(cfz) if cfz else None), "cfz_source": cfz_src,
        "trusted": tr["trusted"], "trust_reasons": tr["reasons"],
        "sigma_limit": tr["sigma_limit"],
        "drive_mode": fm.drive_mode(config),
        "move_temp_step_c": step_c,
        "verify_af_min": tr["verify_af_min"],
        "counter": counter,
        "pairs": pairs,
        "filters": out_rows,
        "drive_ready": ready,
        "configured_offsets": conf,
        "proposed_offsets": proposal,
        "proposed_env": "PS_FOCUS_FILTER_OFFSETS=" + ",".join(
            f"{f}:{v}" for f, v in proposal.items()),
        "formula": f"position = intercept_{ref} + slope x T + offset[filter]",
    }


def move_temp_step_c(slope, cfz) -> float:
    """Temperature change after which drive mode re-moves to the table:
    half a CFZ of focus drift (MOVE_TEMP_CFZ x CFZ / |slope|), at least
    1 C (the EAF reports whole degrees) and at most 5 C. 1 C without a
    slope or a CFZ."""
    s, c = _num(slope), _num(cfz)
    if not s or not c:
        return 1.0
    return round(max(1.0, min(5.0, MOVE_TEMP_CFZ * c / abs(s))), 1)


def drive_ready_filters(config) -> set:
    """Filters drive mode may move to the table instead of running AF."""
    return set(build_table(config).get("drive_ready") or [])


# --------------------------------------------------------------------------
# Which thermometer
# --------------------------------------------------------------------------

def _frozen_values(config) -> list[float]:
    out = []
    for tok in str(getattr(config, "focus_frozen_temps", "") or "").split(","):
        if (v := _num(tok.strip())) is not None:
            out.append(v)
    return out


def _track_path(config) -> Path:
    return Path(config.data_dir) / TEMP_TRACK_FILE


def _load_track(config) -> dict:
    try:
        d = json.loads(_track_path(config).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def choose_temperature(config, focuser_t, ambient_t, now=None,
                       persist: bool = True) -> dict:
    """Pick the model temperature: the focuser's, or the NINA weather
    ambient (+ the learned focuser-minus-ambient bias) when the focuser
    reads a known frozen value (focus_frozen_temps) or has not changed for
    FROZEN_STALE_MIN minutes while the ambient moved more than
    AMBIENT_MOVE_C. {temp, source ("focuser" / "ambient" / "none"),
    reason, focuser, ambient, bias, focuser_unchanged_min}. Updates the
    track file when persist."""
    now = now or datetime.now()
    ft, at = _num(focuser_t), _num(ambient_t)
    track = _load_track(config)
    since = None
    try:
        since = datetime.fromisoformat(track["since"]) if track.get("since") \
            else None
    except (TypeError, ValueError):
        since = None
    prev = _num(track.get("focuser"))
    if ft is not None and (prev is None or abs(ft - prev) > 0.005
                           or since is None):
        track.update(focuser=ft, since=now.isoformat(timespec="seconds"),
                     ambient_at_since=at)
        since = now
    elif ft is not None and track.get("ambient_at_since") is None \
            and at is not None:
        track["ambient_at_since"] = at
    unchanged = ((now - since).total_seconds() / 60 if since and ft is not
                 None else None)
    samples = [x for x in (track.get("bias") or []) if _num(x) is not None]
    bias = fm._median(samples) if len(samples) >= 3 else 0.0
    amb = (at + bias) if at is not None else None
    frozen = ft is not None and any(abs(ft - v) < 0.006
                                    for v in _frozen_values(config))
    a0 = _num(track.get("ambient_at_since"))
    stale = (unchanged is not None and unchanged > FROZEN_STALE_MIN
             and at is not None and a0 is not None
             and abs(at - a0) > AMBIENT_MOVE_C)
    if ft is None and amb is not None:
        src, temp, why = "ambient", amb, "no focuser temperature"
    elif ft is None:
        src, temp, why = "none", None, "no focuser or ambient temperature"
    elif frozen and amb is not None:
        src, temp, why = ("ambient", amb, f"focuser reads {ft:.2f} C, a "
                          "known frozen value (focus_frozen_temps)")
    elif stale:
        src, temp, why = ("ambient", amb, f"focuser stuck at {ft:.2f} C for "
                          f"{unchanged:.0f} min while the ambient moved "
                          f"{at - a0:+.1f} C")
    else:
        src, temp, why = "focuser", ft, "focuser temperature"
        if frozen:
            why += " (a known frozen value, but no ambient to replace it)"
        if at is not None and not frozen and unchanged is not None \
                and unchanged <= FROZEN_STALE_MIN:
            samples = (samples + [round(ft - at, 2)])[-_BIAS_SAMPLES:]
            track["bias"] = samples
    if persist:
        try:
            p = _track_path(config)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(track), encoding="utf-8")
        except OSError as e:
            logger.debug("focus_table: could not write the track: %s", e)
    out = {"temp": (round(temp, 2) if temp is not None else None),
           "source": src, "reason": why, "focuser": ft, "ambient": at,
           "bias": round(bias, 2),
           "focuser_unchanged_min": (round(unchanged) if unchanged is not None
                                     else None)}
    logger.info("focus_table: model temperature %s from %s (%s)",
                out["temp"], src, why)
    return out


# --------------------------------------------------------------------------
# Drive decisions and the payoff
# --------------------------------------------------------------------------

def drive_decision(row: dict | None, *, mode: str = fm.DRIVE_DRIVE,
                   night_start: bool = False, after_flip: bool = False,
                   hfr_soft: bool = False, minutes_since_af: float | None = None,
                   verify_min: float = 120.0, filter_changed: bool = False,
                   temp_change_c: float = 0.0, temp_step_c: float = 1.0,
                   in_range: bool = True) -> dict:
    """What drive mode does at one decision point for one filter's table
    row: {"action": "af" | "move" | "hold", "reason"}. "af" is the full
    autofocus (seed mode always: today's behavior). Order: seed mode, night
    (target) start, a meridian flip, soft focus (HFR trend / sub doctor), a
    filter that is not drive-ready, a temperature outside the trusted range,
    the timed verify AF; then a table move on a filter change or a
    temperature change of temp_step_c; else hold."""
    if mode != fm.DRIVE_DRIVE:
        return {"action": "af", "reason": "seed mode: the model seeds the AF"}
    if night_start:
        return {"action": "af", "reason": "night / target start"}
    if after_flip:
        return {"action": "af", "reason": "after a meridian flip"}
    if hfr_soft:
        return {"action": "af", "reason": "HFR trend says focus is soft"}
    if not row or not row.get("drive_ready"):
        why = "; ".join((row or {}).get("drive_reasons") or ["no table row"])
        return {"action": "af", "reason": "not drive-ready: " + why}
    if not in_range:
        return {"action": "af", "reason": "temperature outside the range "
                "the table is trusted for"}
    if minutes_since_af is not None and verify_min and \
            minutes_since_af >= verify_min:
        return {"action": "af", "reason": f"verify AF due ({verify_min:.0f} "
                "min since the last AF)"}
    if filter_changed:
        return {"action": "move", "reason": "filter change: table position"}
    if abs(temp_change_c) >= temp_step_c:
        return {"action": "move", "reason": f"temperature moved "
                f"{temp_change_c:+.1f} C (>= {temp_step_c:.1f} C, half a CFZ)"}
    return {"action": "hold", "reason": "in focus by the table"}


def payoff(config, nights: int = 7, today=None, pts: list[dict] | None = None,
           af_s: float | None = None, sub_s: float | None = None,
           per_sub_s: float | None = None) -> dict:
    """Replay the last `nights` nights' RC16 AFs through drive_decision()
    with today's table: which AFs drive mode would have kept, the AF
    minutes saved (each saved AF costs a MOVE_S table move instead) and the
    shutter time that buys (saved time x sub / (sub + per-sub overhead)).
    A kept AF: the first of the night, the timed verify, a filter that is
    not drive-ready today, or one the table missed by more than one CFZ
    (the HFR trigger would have asked for it). Target-start AFs after the
    first and HFR-triggered AFs are not identified in the AF reports, so
    they count only through those rules."""
    pts = fm._rc16_points(config) if pts is None else pts
    tab = build_table(config, pts=pts)
    rows = tab.get("filters") or {}
    cfz = tab.get("cfz_steps")
    verify = float(tab.get("verify_af_min") or 120.0)
    slope = tab.get("slope_steps_per_c") or 0.0
    today = today or datetime.now().date()
    if af_s is None or per_sub_s is None:
        try:
            from photonscript.scheduler.shutter_efficiency import \
                measured_overheads
            oh = measured_overheads(config)
        except Exception as e:  # noqa: BLE001
            logger.debug("focus_table: no overheads: %s", e)
            oh = {"af_s": 250.0, "per_sub_s": 11.0}
        af_s = oh["af_s"] if af_s is None else af_s
        per_sub_s = oh["per_sub_s"] if per_sub_s is None else per_sub_s
    if sub_s is None:
        sub_s = float(getattr(config, "bb_exposure_s", 180.0) or 180.0)
    eff = sub_s / (sub_s + per_sub_s) if sub_s + per_sub_s > 0 else 1.0
    by_night: dict = {}
    for p in pts:
        t = fm._when(p)
        if t is None or _num(p.get("position")) is None:
            continue
        night = (t - timedelta(hours=12)).date()
        if not 1 <= (today - night).days <= nights:
            continue
        by_night.setdefault(night, []).append((t, p))
    out_n = []
    for night in sorted(by_night):
        afs = sorted(by_night[night], key=lambda x: x[0])
        kept, saved, saved_s, kept_s, why = 0, 0, 0.0, 0.0, {}
        last_af = None
        for i, (t, p) in enumerate(afs):
            row = rows.get(p.get("filter"))
            dur = _num(p.get("duration_s")) or af_s
            miss = False
            if row and row.get("position") is not None and \
                    _num(p.get("temp")) is not None and cfz and \
                    tab.get("intercept_ref") is not None:
                pred = (tab["intercept_ref"] + slope * float(p["temp"])
                        + row["offset"])
                miss = abs(float(p["position"]) - pred) > cfz
            mins = ((t - last_af).total_seconds() / 60 if last_af else None)
            d = drive_decision(row, night_start=(i == 0),
                               hfr_soft=miss, minutes_since_af=mins,
                               verify_min=verify)
            if d["action"] == "af":
                kept += 1
                kept_s += dur
                last_af = t
                key = d["reason"].split(":")[0].split(" (")[0]
                why[key] = why.get(key, 0) + 1
            else:
                saved += 1
                saved_s += max(0.0, dur - MOVE_S)
        out_n.append({"night": night.isoformat(), "afs": len(afs),
                      "kept": kept, "saved": saved,
                      "af_min_actual": round((kept_s + saved_s
                                              + saved * MOVE_S) / 60, 1),
                      "af_min_saved": round(saved_s / 60, 1),
                      "shutter_h_gained": round(saved_s * eff / 3600, 2),
                      "kept_reasons": why})
    tot_saved = sum(n["af_min_saved"] for n in out_n)
    return {"nights": out_n, "n_nights": len(out_n),
            "afs": sum(n["afs"] for n in out_n),
            "kept": sum(n["kept"] for n in out_n),
            "saved": sum(n["saved"] for n in out_n),
            "af_min_saved": round(tot_saved, 1),
            "shutter_h_gained": round(sum(n["shutter_h_gained"]
                                          for n in out_n), 2),
            "per_night_min": (round(tot_saved / len(out_n), 1) if out_n
                              else 0.0),
            "assumptions": {"af_s": round(af_s, 1), "move_s": MOVE_S,
                            "sub_s": sub_s, "per_sub_s": per_sub_s,
                            "verify_af_min": verify, "cfz_steps": cfz,
                            "drive_ready": tab.get("drive_ready")}}


# --------------------------------------------------------------------------
# Text (CLI)
# --------------------------------------------------------------------------

def format_table(tab: dict, pay: dict | None = None) -> str:
    """Plain-text table for `photonscript focus-table`."""
    if tab.get("error"):
        return f"focus table: {tab['error']}"
    t = tab.get("temp")
    lines = [f"RC16 focus table at {t:.2f} C ({tab.get('temp_basis')})"
             if t is not None else "RC16 focus table (no temperature known)",
             f"  {tab.get('formula')}: intercept_{tab.get('ref_filter')} "
             f"{tab.get('intercept_ref')}, slope {tab.get('slope_steps_per_c')}"
             f" steps/C (se {tab.get('slope_se')}), scatter "
             f"{tab.get('sigma_steps')} (limit {tab.get('sigma_limit')}), "
             f"CFZ {tab.get('cfz_steps')} ({tab.get('cfz_source')})",
             f"  focus_model_drive {tab.get('drive_mode')}; model "
             + ("trusted" if tab.get("trusted") else "NOT trusted: "
                + "; ".join(tab.get("trust_reasons") or []))
             + f"; drive re-move after {tab.get('move_temp_step_c')} C; "
             f"verify AF every {tab.get('verify_af_min'):.0f} min",
             "", f"  {'Filter':<6} {'Pos':>5} {'Offset':>7} {'Source':<12}"
             f" {'AFs':>4} {'Pairs':>5} {'Conf':<5} Drive"]
    for f, r in (tab.get("filters") or {}).items():
        pos = r["position"] if r["position"] is not None else "-"
        lines.append(f"  {f:<6} {pos!s:>5} {r['offset']:>+7d} "
                     f"{r['offset_source']:<12} {r['n_afs']:>4} "
                     f"{r['n_pairs']:>5} {r['confidence']:<5} "
                     + ("READY" if r["drive_ready"] else
                        "no: " + "; ".join(r["drive_reasons"])))
        if r.get("note"):
            lines.append(f"         note: {r['note']}")
    c = tab.get("counter") or {}
    lines.append("")
    lines.append("  Drive-ready today: " + (", ".join(tab.get("drive_ready")
                                                    or []) or "none"))
    if c.get("flag"):
        lines.append("  " + c["flag"])
    elif c.get("epoch"):
        lines.append(f"  Counter: no shift since {c['epoch']} "
                     f"(threshold {c.get('threshold')} steps)")
    lines.append("  Proposed offsets: " + tab.get("proposed_env", ""))
    if pay and not pay.get("error"):
        lines.append("")
        lines.append(f"  Payoff, last {pay['n_nights']} night(s) with AFs: "
                     f"{pay['afs']} AFs, drive keeps {pay['kept']}, saves "
                     f"{pay['saved']} = {pay['af_min_saved']} AF min, "
                     f"+{pay['shutter_h_gained']} h shutter "
                     f"({pay['per_night_min']} min/night)")
        for n in pay["nights"]:
            lines.append(f"    {n['night']}: {n['afs']} AFs, keep "
                         f"{n['kept']} {n['kept_reasons']}, save "
                         f"{n['af_min_saved']} min, +{n['shutter_h_gained']}"
                         " h")
    return "\n".join(lines)
