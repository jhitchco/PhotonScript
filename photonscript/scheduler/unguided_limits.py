"""Learned unguided exposure limits per rig and sky region.

2026-10-13 (unguided, TPoint + ProTrack, Paramount MX, RC16 at 0.24"/px):
near the meridian at Dec +12 (M15) 180 s L test frames were round with
ProTrack off and on; on M31 (Dec +41, east, alt about 46 deg) 300 s L and R
trailed (ecc 0.79 / 0.71, rejected) while 180 s L was round (ecc 0.43,
FWHM 3.9"). 2026-10-12 (Heart, Dec +61) trailed at 300 s and 180 s, but
PHD2 was guiding on a FAIL calibration the whole time (guider conflict), so
that night says nothing about the mount. One cap (unguided_max_exposure_s,
PS-66) for the whole sky is either too long for some regions or too short
for others. This module learns the longest unguided sub that stays round,
per region, from the graded subs themselves.

Evidence (collect): every graded light of the rig in the lookback window
(unguided_limit_lookback_days, 60) on nights since the TPoint model in
force (tracking_drift.tpoint_model, or the TPoint watcher's epoch when a
new base model moved the polar axis and that is later; unguided_limit_since
overrides both), that
ran unguided: the plan snapshot says the target (or its PS-85 block) was
unguided, a tracking-test target, or (no plan entry) PHD2 was not active.
Excluded, and counted by reason:

    guided          the plan guided this target / block
    unknown         no plan entry and no PHD2 state recorded (old nights)
    guider_conflict PHD2 active during the sub (guiding / settling /
                    lost_star / calibrating), a sub within GUIDE_MARGIN_MIN
                    of such a sub on either rig, or a sub doctor
                    guider_conflict diagnosis covering it (run events)
    focuser         FOCPOS 0 (the focuser dropped off: round, soft stars)
    clouds          star count under half the night's median (or the gate)
    soft_round      HFR over its gate (out of focus) while the ecc passes:
                    soft stars hide drift, so they say nothing about
                    tracking (a FWHM over its gate is seeing: kept)
    test            TPoint / optics / focus-calibration subs (tracking-test
                    subs are kept: they are the best evidence there is)
    no_position     no mount position and no known target coordinates
    unmeasured      no eccentricity

Region: declination band (DEC_BANDS, 20 deg) x hour-angle band (HA_BANDS:
east far / east / meridian / west / west far). Altitude follows from the
two at one site, so it is reported (median) but not binned.

Per region (and pooled per Dec band and over the whole sky):
  * per sub length: subs, passes (ecc at or under the rig's ecc gate,
    qa_rules.thresholds) and the pass rate;
  * drift: per sub the trail from HFR + ecc (sub_doctor.trail_estimate:
    sigma_major^2 = sigma^2 + L^2/12), as a rate "/min (median), and the
    elongation per second k = e/sqrt(1-e^2)/t (median), which extrapolates
    to an untested length L: e(L) = kL/sqrt(1+(kL)^2);
  * limit: the longest ladder length (unguided_limit_steps, "300,240,180,
    120") that passes: measured (at least unguided_limit_min_subs subs,
    pass rate >= unguided_limit_pass_rate, 0.8) or, untested, the drift
    model predicting ecc at or under the gate minus
    unguided_limit_model_margin (0.05); never above a shorter length that
    fails on measurement. confidence high (8+ subs measured at the limit),
    medium (4+), low (fewer, or the model). uncertain = not high, or the
    next longer step is untested: probe_s is that step.

Planner (plan_target / apply_targets, armer dispatch): on an unguided
target (or a guided target's PS-85 unguided blocks) the target's window is
sampled every SAMPLE_MIN; each sample's region is looked up (region, else
its Dec band pooled, else the whole sky pooled) and the shortest limit
wins. Each RC16 set longer than the limit is cut to the longest ladder
length at or under the limit whose darks match (night_pilot.dark_match:
the night quota or the library), else whose darks can be scaled from a
bias, else the longest; never longer than the goal's own length; a goal's
hand-set exposure override for that filter wins. unguided_auto_length:
off | advise (default: logged, in the plan snapshot and the morning
report; nothing changes) | apply (the sets are cut, same integration).
The Piggy-600 companion (one sub length all night) gets the shortest
Piggy limit over tonight's targets the same way.

Probe (unguided_limit_probe off | advise | apply, default off): on an
uncertain region, advise names the next longer step to try; apply adds
unguided_limit_probe_subs (2) subs at that step in front of the target's
longest-owed set (its count reduced to keep the integration). The probe
subs are graded like any other: a rejected probe sub stays rejected and
counts against that length at the next refresh, a passing one raises it.

Calibration (learned_dark_lengths): the ladder lengths the table uses
(each region's limit, its probe step and, for an untested limit from the
drift model, the step under it; at or under the rig's cap) join the
night dark quota (calibration.night_dark_exposures) and the Calibration
owed view, so a roof-closed night captures 180 s and 240 s darks before
the planner needs them. unguided_limit_darks_max (3) caps how many.

Store: <data_dir>/unguided_limits.json, refreshed by the dawn filing
(dawn_autofile.file_night) and on demand (GET /api/tracking/unguided-limits
?refresh=true, CLI `photonscript unguided-limits --refresh`). Reads subs
logs, plan snapshots, run events and pointing sidecars; writes only the
store.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("off", "advise", "apply")
PROBE_MODES = ("off", "advise", "apply")
RIGS = ("rc16", "piggyback")
STORE_NAME = "unguided_limits.json"
DEC_BANDS = ((-30, -10), (-10, 10), (10, 30), (30, 50), (50, 70), (70, 90))
HA_BANDS = ((-12.0, -3.0, "east far"), (-3.0, -1.0, "east"),
            (-1.0, 1.0, "meridian"), (1.0, 3.0, "west"), (3.0, 12.0, "west far"))
ACTIVE_GUIDE = ("guiding", "settling", "lost_star", "calibrating")
GUIDE_MARGIN_MIN = 10.0     # a sub this close to a PHD2-active sub is suspect
CONFLICT_LEAD_MIN = 30.0    # a guider_conflict diagnosis covers its window
SAMPLE_MIN = 30.0           # planner: sample a target window this often
HIGH_N, MEDIUM_N = 8, 4


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _f(config, key, default) -> float:
    try:
        v = float(getattr(config, key, default))
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def mode(config) -> str:
    m = str(getattr(config, "unguided_auto_length", "advise") or "advise").strip().lower()
    return m if m in MODES else "advise"


def probe_mode(config) -> str:
    m = str(getattr(config, "unguided_limit_probe", "off") or "off").strip().lower()
    return m if m in PROBE_MODES else "off"


def ladder(config, rig: str | None = None) -> list[float]:
    """unguided_limit_steps ("300,240,180,120") as seconds, longest first,
    plus the rig's own length (rig_cap: the Piggy-600's 450 s) when a rig
    is given."""
    raw = str(getattr(config, "unguided_limit_steps", "300,240,180,120") or "")
    out = set()
    for x in raw.replace(";", ",").split(","):
        try:
            v = float(x.strip())
        except ValueError:
            continue
        if math.isfinite(v) and v > 0:
            out.add(v)
    if not out:
        out = {300.0, 240.0, 180.0, 120.0}
    if rig:
        cap = rig_cap(config, rig)
        if cap:
            out.add(float(cap))
    return sorted(out, reverse=True)


def pass_rate_target(config) -> float:
    return min(1.0, max(0.05, _f(config, "unguided_limit_pass_rate", 0.8)))


def min_subs(config) -> int:
    return max(1, int(_f(config, "unguided_limit_min_subs", 2)))


def model_margin(config) -> float:
    return max(0.0, _f(config, "unguided_limit_model_margin", 0.05))


def lookback_days(config) -> int:
    return max(1, int(_f(config, "unguided_limit_lookback_days", 60)))


def probe_subs(config) -> int:
    return max(1, int(_f(config, "unguided_limit_probe_subs", 2)))


def rig_cap(config, rig: str) -> float | None:
    """The longest length the rig ever runs unguided: unguided_max_exposure_s
    for the RC16 (0 = none), piggyback_exposure_s for the Piggy-600."""
    key = "piggyback_exposure_s" if rig == "piggyback" else "unguided_max_exposure_s"
    v = _f(config, key, 120.0 if rig == "piggyback" else 300.0)
    return v if v > 0 else None


def _gates(config, rig: str) -> dict:
    try:
        from photonscript.shared.qa_rules import thresholds
        t = thresholds(config, rig)
    except Exception as e:  # noqa: BLE001
        logger.debug("unguided limits: thresholds unavailable: %s", e)
        t = {}
    return {"ecc_max": float(t.get("ecc_max") or _f(config, "quality_eccentricity_max", 0.7)),
            "hfr_max": float(t.get("hfr_max") or 0) or None,
            "fwhm_max": float(t.get("fwhm_max") or 0) or None,
            "star_min": int(t.get("star_min") or 5),
            "scale": float(t.get("pixel_scale") or _scale(config, rig))}


def _scale(config, rig: str) -> float:
    key = "piggyback_pixel_scale_arcsec" if rig == "piggyback" else "pixel_scale_arcsec"
    return _f(config, key, 1.29 if rig == "piggyback" else 0.236) or 0.236


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(v, nd=2):
    return None if v is None else round(float(v), nd)


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _key_len(s: float) -> str:
    return f"{float(s):g}"


def dec_band(dec: float) -> tuple[int, int] | None:
    for lo, hi in DEC_BANDS:
        if lo <= dec < hi or (hi == 90 and dec == 90):
            return lo, hi
    return None


def ha_band(ha: float) -> tuple[float, float, str] | None:
    ha = ((ha + 12.0) % 24.0) - 12.0
    for lo, hi, name in HA_BANDS:
        if lo <= ha < hi:
            return lo, hi, name
    return HA_BANDS[-1]


def side(ha: float | None) -> str:
    if ha is None:
        return "?"
    if abs(ha) < 0.25:
        return "meridian"
    return "east" if ha < 0 else "west"


def region_key(dec: float, ha: float) -> str | None:
    db, hb = dec_band(dec), ha_band(ha)
    if not db or not hb:
        return None
    return f"dec{db[0]:+d}..{db[1]:+d}|ha{hb[0]:+g}..{hb[1]:+g}"


def region_label(dec_lo: int, dec_hi: int, ha_name: str | None = None) -> str:
    s = f"Dec {dec_lo:+d}..{dec_hi:+d}"
    return s + (f", {ha_name}" if ha_name else "")


def alt_ha(ra_deg: float, dec_deg: float, when: datetime, config) -> tuple:
    from photonscript.shared.pointing import alt_ha as _alt_ha
    return _alt_ha(ra_deg, dec_deg, when, _f(config, "observatory_lat", 31.906944),
                   _f(config, "observatory_lon", -109.021367))


def ecc_k(ecc: float) -> float | None:
    """Elongation measure k = e / sqrt(1 - e^2) (= L / (sqrt(12) sigma) for
    a Gaussian star smeared over a line of length L)."""
    if ecc is None or not 0 <= ecc < 1:
        return None
    return ecc / math.sqrt(1.0 - ecc * ecc)


def ecc_from_k(k: float) -> float:
    return k / math.sqrt(1.0 + k * k)


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------

def _plan_index(plan: dict | None) -> dict:
    from photonscript.shared.target_names import target_key
    out = {}
    for t in (plan or {}).get("targets") or []:
        if isinstance(t, dict) and t.get("name"):
            out[target_key(t["name"])] = t
    return out


def _guiding_of(rec: dict, plan_idx: dict) -> str:
    """'unguided' | 'guided' | 'unknown' for one sub."""
    from photonscript.shared.target_names import target_key
    name = rec.get("target")
    if target_key(name).startswith("trackingtest"):
        return "unguided"
    t = plan_idx.get(target_key(name))
    if t is None:
        try:
            from photonscript.shared.target_names import canonical_target
            t = plan_idx.get(target_key(canonical_target(name)))
        except Exception:  # noqa: BLE001
            t = None
    if t is not None and "guided" in t and t.get("guided") is not None:
        if not t["guided"]:
            return "unguided"
        if str(rec.get("filter") or "") in (t.get("unguided_filters") or []):
            return "unguided"
        return "guided"
    gs = str(rec.get("guide_state") or "").lower()
    if gs in ACTIVE_GUIDE:
        return "guided"
    if gs:
        return "unguided"       # PHD2 present and stopped
    return "unknown"


def _window(rec: dict):
    from photonscript.shared.sub_time import sub_window
    try:
        return sub_window(rec)
    except Exception:  # noqa: BLE001
        return None


def _active_spans(subs: list[dict]) -> list[tuple]:
    out = []
    for r in subs:
        if str(r.get("guide_state") or "").lower() in ACTIVE_GUIDE:
            w = _window(r)
            if w:
                out.append(w)
    return out


def conflict_spans(events: list[dict] | None, rig: str,
                   night_end: datetime | None = None) -> list[tuple]:
    """Spans a sub doctor guider_conflict diagnosis covers on `rig`: from
    CONFLICT_LEAD_MIN before the diagnosis (its window of subs) to the
    rig's next sub doctor event with another cause (or 'none'), else
    night_end (or 6 h later)."""
    from photonscript.shared.sub_time import parse_utc
    evs = []
    for e in events or []:
        if e.get("kind") != "sub_doctor" or (e.get("rig") or "rc16") != rig:
            continue
        t = parse_utc(e.get("t"))
        if t is not None:
            evs.append((t, str(e.get("cause") or e.get("value") or "none")))
    evs.sort()
    spans = []
    for i, (t, cause) in enumerate(evs):
        if cause != "guider_conflict":
            continue
        end = None
        for t2, c2 in evs[i + 1:]:
            if c2 != "guider_conflict":
                end = t2
                break
        spans.append((t - timedelta(minutes=CONFLICT_LEAD_MIN),
                      end or night_end or t + timedelta(hours=6)))
    return spans


def _near(w, spans, margin_min: float) -> bool:
    m = timedelta(minutes=margin_min)
    return any(w[0] < b + m and w[1] > a - m for a, b in spans)


def _position(config, rec: dict, pointing: dict | None) -> tuple | None:
    """(ra_deg, dec_deg, alt or None) of a sub: the stored mount position
    (pointing sidecar or the record's own 'pointing'), else the target's
    coordinates."""
    p = rec.get("pointing") if isinstance(rec.get("pointing"), dict) else None
    if p is None and pointing is not None:
        p = pointing.get((rec.get("rig") or "rc16", rec.get("file")))
    if p:
        ra = _num(p.get("solved_ra")) if p.get("solved_ra") is not None else _num(p.get("mount_ra"))
        dec = _num(p.get("solved_dec")) if p.get("solved_dec") is not None else _num(p.get("mount_dec"))
        if ra is not None and dec is not None:
            return ra, dec, _num(p.get("alt"))
    try:
        from photonscript.shared.pointing import target_coords
        tc = target_coords(config, rec.get("target"))
    except Exception:  # noqa: BLE001
        tc = None
    if tc:
        return tc[1], tc[2], None
    return None


def evidence(config, rig: str, night: str, subs: list[dict], *,
             plan: dict | None = None, events: list[dict] | None = None,
             pointing: dict | None = None) -> tuple[list[dict], Counter]:
    """Unguided evidence rows of one night for one rig and the excluded
    counts by reason (module doc). Pure apart from target coordinates."""
    from photonscript.shared.qa_rules import is_test_record, record_ecc
    from photonscript.shared.target_names import target_key
    g = _gates(config, rig)
    plan_idx = _plan_index(plan)
    active = _active_spans(subs)
    rs = [r for r in subs if (r.get("rig") or "rc16") == rig]
    ends = [w[1] for w in (_window(r) for r in rs) if w]
    spans = conflict_spans(events, rig, max(ends) if ends else None)
    stars = [_num(r.get("stars")) for r in rs]
    stars_med = _med([s for s in stars if s])
    rows, excl = [], Counter()
    for r in rs:
        it = str(r.get("image_type") or "LIGHT").upper()
        if it not in ("LIGHT", ""):
            continue
        tt = target_key(r.get("target")).startswith("trackingtest")
        if is_test_record(r) and not tt:
            excl["test"] += 1
            continue
        exp = _num(r.get("exp_s"))
        ecc = record_ecc(r)
        if not exp or exp <= 0 or ecc is None:
            excl["unmeasured"] += 1
            continue
        how = _guiding_of(r, plan_idx)
        if how != "unguided":
            excl[how] += 1
            continue
        w = _window(r)
        if w is None:
            excl["unmeasured"] += 1
            continue
        if str(r.get("guide_state") or "").lower() in ACTIVE_GUIDE or (
                active and _near(w, active, GUIDE_MARGIN_MIN)) or (
                spans and _near(w, spans, 0.0)):
            excl["guider_conflict"] += 1
            continue
        fp = r.get("focpos")
        if fp is not None and _num(fp) == 0:
            excl["focuser"] += 1
            continue
        st = _num(r.get("stars"))
        if st is not None and (st < g["star_min"]
                               or (stars_med and st < 0.5 * stars_med)):
            excl["clouds"] += 1
            continue
        hfr, fwhm = _num(r.get("hfr")), _num(r.get("fwhm_arcsec"))
        # out of focus (the HFR gate), not seeing: a FWHM over its gate on
        # round stars is the sky and still says the mount tracked
        soft = bool(g["hfr_max"] and hfr and hfr > g["hfr_max"])
        passed = ecc <= g["ecc_max"] + 1e-9
        if soft and passed:
            excl["soft_round"] += 1
            continue
        pos = _position(config, r, pointing)
        if pos is None:
            excl["no_position"] += 1
            continue
        mid = w[0] + (w[1] - w[0]) / 2
        alt, ha = alt_ha(pos[0], pos[1], mid, config)
        if pos[2] is not None:
            alt = pos[2]
        k = ecc_k(ecc)
        rows.append({
            "night": night, "rig": rig, "file": r.get("file"),
            "target": r.get("target"), "filter": r.get("filter"),
            "t": mid.replace(microsecond=0).isoformat() + "Z",
            "exp_s": exp, "ecc": round(ecc, 3), "pass": passed,
            "hfr": hfr, "fwhm": fwhm, "dec": _r(pos[1], 2),
            "ha_h": _r(ha, 2), "alt": _r(alt, 1),
            "k_per_s": (k / exp) if k is not None else None,
            "rate_arcsec_min": _trail_rate(hfr, ecc, exp, g["scale"])})
    return rows, excl


def _trail_rate(hfr, ecc, exp, scale):
    try:
        from photonscript.scheduler.sub_doctor import trail_estimate
        return trail_estimate(hfr, ecc, exp, scale).get("rate_arcsec_min")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

def predict_ecc(k_per_s: float | None, length_s: float) -> float | None:
    if not k_per_s or k_per_s <= 0:
        return None
    return ecc_from_k(k_per_s * float(length_s))


def stats(rows: list[dict], config, rig: str) -> dict:
    """Per-length pass counts, the drift model and the limit of one group."""
    g = _gates(config, rig)
    target = pass_rate_target(config)
    need = min_subs(config)
    margin = model_margin(config)
    steps = ladder(config, rig)
    cap = rig_cap(config, rig)
    by: dict[str, dict] = {}
    for r in rows:
        b = by.setdefault(_key_len(r["exp_s"]), {"exp_s": r["exp_s"], "n": 0, "pass": 0,
                                                 "ecc": []})
        b["n"] += 1
        b["pass"] += 1 if r["pass"] else 0
        b["ecc"].append(r["ecc"])
    lengths = {}
    for k, b in sorted(by.items(), key=lambda kv: kv[1]["exp_s"]):
        lengths[k] = {"exp_s": b["exp_s"], "n": b["n"], "pass": b["pass"],
                      "rate": _r(b["pass"] / b["n"], 2),
                      "ecc_med": _r(_med(b["ecc"]), 3)}
    # a length measured often enough is a real option too (the
    # Piggy-600's 450 s lights against a 120 s config default)
    steps = sorted({*steps, *(float(v["exp_s"]) for v in lengths.values()
                              if v["n"] >= need)}, reverse=True)
    kps = _med([r["k_per_s"] for r in rows if r.get("k_per_s")])
    n_model = sum(1 for r in rows if r.get("k_per_s"))
    model_ok = kps is not None and n_model >= need
    verdict = {}
    for s in sorted(steps):
        d = lengths.get(_key_len(s))
        pred = predict_ecc(kps, s) if model_ok else None
        if d and d["n"] >= need:
            ok, src = d["pass"] / d["n"] >= target - 1e-9, "measured"
        elif pred is not None:
            ok = pred <= g["ecc_max"] - margin + 1e-9
            src = "drift model"
            if d and d["n"] and d["pass"] == 0:
                ok = False      # every sub tried there trailed
                src = "measured"
        else:
            ok, src = None, None
        verdict[_key_len(s)] = {"exp_s": s, "ok": ok, "source": src,
                                "predicted_ecc": _r(pred, 3)}
    limit = src = None
    fails_all = False
    for s in sorted(steps):
        v = verdict[_key_len(s)]
        if v["ok"] is False:
            fails_all = limit is None
            break
        if v["ok"]:
            limit, src = s, v["source"]
    n_at = (lengths.get(_key_len(limit)) or {}).get("n", 0) if limit else 0
    if limit is None:
        conf = None
    elif src == "measured" and n_at >= HIGH_N:
        conf = "high"
    elif src == "measured" and n_at >= MEDIUM_N:
        conf = "medium"
    else:
        conf = "low"
    longer = [s for s in sorted(steps) if limit is not None and s > limit + 0.5
              and (cap is None or s <= cap + 0.5)]
    nxt = longer[0] if longer else None
    nxt_measured = bool(nxt and verdict[_key_len(nxt)]["source"] == "measured")
    uncertain = limit is not None and (conf != "high" or (nxt is not None and not nxt_measured))
    alts = [r["alt"] for r in rows if r.get("alt") is not None]
    return {
        "n": len(rows), "nights": sorted({r["night"] for r in rows}),
        "lengths": lengths, "verdicts": verdict,
        "k_per_s": None if kps is None else float(f"{kps:.3e}"),
        "drift_arcsec_min": _r(_med([r.get("rate_arcsec_min") for r in rows]), 2),
        "hfr_med": _r(_med([r.get("hfr") for r in rows]), 2),
        "alt_med": _r(_med(alts), 1),
        "limit_s": limit, "source": src, "confidence": conf,
        "fails_all": fails_all and limit is None,
        "uncertain": bool(uncertain),
        "probe_s": nxt if uncertain and nxt is not None and not nxt_measured else None}


def build_table(config, rig: str, rows: list[dict], *, excluded: Counter | None = None,
                nights: list[str] | None = None, since: str | None = None) -> dict:
    """The learned table of one rig from its evidence rows."""
    g = _gates(config, rig)
    regions: dict[str, dict] = {}
    dec_groups: dict[tuple, list] = {}
    for r in rows:
        db, hb = dec_band(r["dec"]), ha_band(r["ha_h"] or 0.0)
        if not db or not hb:
            continue
        k = region_key(r["dec"], r["ha_h"] or 0.0)
        reg = regions.setdefault(k, {"key": k, "dec_band": list(db),
                                     "ha_band": [hb[0], hb[1]], "ha_name": hb[2],
                                     "label": region_label(db[0], db[1], hb[2]),
                                     "rows": []})
        reg["rows"].append(r)
        dec_groups.setdefault(db, []).append(r)
    out_regions = []
    for k, reg in sorted(regions.items(), key=lambda kv: (kv[1]["dec_band"][0],
                                                          kv[1]["ha_band"][0])):
        st = stats(reg.pop("rows"), config, rig)
        out_regions.append({**reg, **st})
    out_dec = []
    for db, rs in sorted(dec_groups.items()):
        out_dec.append({"key": f"dec{db[0]:+d}..{db[1]:+d}", "dec_band": list(db),
                        "label": region_label(db[0], db[1]), **stats(rs, config, rig)})
    return {
        "rig": rig, "gate_ecc": g["ecc_max"], "scale_arcsec": g["scale"],
        "pass_rate": pass_rate_target(config), "min_subs": min_subs(config),
        "ladder": ladder(config, rig), "cap_s": rig_cap(config, rig),
        "since": since, "nights_scanned": nights or [],
        "n_used": len(rows), "excluded": dict(excluded or {}),
        "regions": out_regions, "dec_bands": out_dec,
        "overall": {"key": "all", "label": "whole sky", **stats(rows, config, rig)},
        "evidence": [{k: r[k] for k in ("night", "t", "target", "filter", "exp_s",
                                        "ecc", "pass", "dec", "ha_h", "alt")}
                     for r in rows[-200:]]}


# --------------------------------------------------------------------------
# IO: nights, refresh, store
# --------------------------------------------------------------------------

def store_path(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / STORE_NAME


def load(config) -> dict | None:
    from photonscript.shared import phd2_store as store
    d = store.read_json(store_path(config))
    return d if isinstance(d, dict) and d.get("rigs") else None


def since_date(config, today: str) -> str | None:
    """Evidence starts at the TPoint model in force (a new model changes
    the tracking), or at the TPoint watcher's epoch when that is later (a
    new base model with a moved polar axis, tpoint_watch.check_epoch),
    unless unguided_limit_since names a date."""
    s = str(getattr(config, "unguided_limit_since", "") or "").strip()
    if s:
        return s
    d = None
    try:
        from photonscript.scheduler.tracking_drift import tpoint_model
        m = tpoint_model(config, today) or {}
        d = str(m["model_date"])[:10] if m.get("model_date") else None
    except Exception as e:  # noqa: BLE001
        logger.debug("unguided limits: no TPoint model date: %s", e)
    try:
        from photonscript.scheduler.tpoint_watch import epoch_since
        ep = epoch_since(config)
    except Exception as e:  # noqa: BLE001
        logger.debug("unguided limits: no TPoint epoch: %s", e)
        ep = None
    if ep and ep <= today and (d is None or ep > d):
        return ep
    return d


def local_nights(config, today: str, since: str | None) -> list[str]:
    from photonscript.scheduler.runs import runs_dir
    d0 = (datetime.strptime(today, "%Y-%m-%d").date()
          - timedelta(days=lookback_days(config))).isoformat()
    out = []
    for p in sorted(runs_dir(config).glob("*_subs.jsonl")):
        n = p.name[:10]
        if n >= d0 and n <= today and (not since or n >= since):
            out.append(n)
    return out


def _local_night(config, night: str) -> dict:
    from photonscript.scheduler.runs import _load_subs, runs_dir
    from photonscript.shared import phd2_store as store
    from photonscript.shared.night_events import events_path
    try:
        from photonscript.shared.pointing import load as load_pointing
        pt = load_pointing(config, night)
    except Exception:  # noqa: BLE001
        pt = {}
    return {"subs": _load_subs(config, night),
            "plan": store.read_json(runs_dir(config) / f"{night}_plan.json"),
            "events": store.read_jsonl(events_path(config, night)),
            "pointing": pt}


def compute(config, nights_data: dict[str, dict], *, since: str | None = None,
            rigs=RIGS) -> dict:
    """{rig: table} from {night: {subs, plan, events, pointing}}."""
    from photonscript.shared.rigs import rig_config
    out = {}
    for rig in rigs:
        rows, excl = [], Counter()
        for night in sorted(nights_data):
            nd = nights_data[night] or {}
            try:
                rs, ex = evidence(rig_config(config, rig), rig, night, nd.get("subs") or [],
                                  plan=nd.get("plan"), events=nd.get("events"),
                                  pointing=nd.get("pointing"))
            except Exception:  # noqa: BLE001 - one bad night never breaks the table
                logger.warning("unguided limits: night %s skipped", night, exc_info=True)
                continue
            rows.extend(rs)
            excl.update(ex)
        out[rig] = build_table(rig_config(config, rig), rig, rows, excluded=excl,
                               nights=sorted(nights_data), since=since)
    return out


def refresh(config, today: str | None = None, *, save: bool = True,
            nights_data: dict | None = None) -> dict:
    """Recompute the table from this machine's nights (or nights_data) and
    store it. Never raises on a bad night."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    since = since_date(config, today)
    if nights_data is None:
        nights_data = {n: _local_night(config, n) for n in local_nights(config, today, since)}
    else:
        nights_data = {n: v for n, v in nights_data.items() if not since or n >= since}
    doc = {"refreshed_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
           "since": since, "mode": mode(config), "probe": probe_mode(config),
           "rigs": compute(config, nights_data, since=since)}
    if save:
        from photonscript.shared import phd2_store as store
        try:
            store.write_json(store_path(config), doc)
        except OSError as e:
            logger.warning("unguided limits not saved: %s", e)
    return doc


def table(config, *, refresh_if_missing: bool = True) -> dict | None:
    d = load(config)
    if d is None and refresh_if_missing:
        try:
            d = refresh(config)
        except Exception:  # noqa: BLE001
            logger.warning("unguided limits refresh failed", exc_info=True)
            return None
    return d


# --------------------------------------------------------------------------
# lookup and the planner
# --------------------------------------------------------------------------

def _decided(st: dict | None) -> bool:
    return bool(st and (st.get("limit_s") or st.get("fails_all")))


def lookup(tbl: dict | None, dec: float, ha: float) -> dict | None:
    """The table entry for one sky position: the region, else its Dec band
    pooled, else the whole sky pooled; None when nothing is decided."""
    if not tbl:
        return None
    k = region_key(dec, ha)
    for r in tbl.get("regions") or []:
        if r.get("key") == k and _decided(r):
            return {**r, "scope": "region"}
    db = dec_band(dec)
    for r in tbl.get("dec_bands") or []:
        if db and r.get("dec_band") == list(db) and _decided(r):
            return {**r, "scope": "dec band"}
    ov = tbl.get("overall")
    if _decided(ov):
        return {**ov, "scope": "whole sky"}
    return None


def _entry_limit(e: dict, steps: list[float]) -> float:
    if e.get("limit_s"):
        return float(e["limit_s"])
    return float(min(steps))          # fails even at the shortest step


def plan_target(config, tbl: dict | None, ra_hours: float, dec_deg: float,
                start: datetime | None, end: datetime | None) -> dict | None:
    """The learned limit over a target's window: the shortest limit of the
    regions the target passes (sampled every SAMPLE_MIN). None when the
    table has nothing for it."""
    if not tbl:
        return None
    steps = list(tbl.get("ladder") or ladder(config, tbl.get("rig")))
    start = start or datetime.utcnow()
    end = end if end and end > start else start
    t, best = start, None
    while True:
        alt, ha = alt_ha(float(ra_hours) * 15.0, float(dec_deg), t, config)
        e = lookup(tbl, float(dec_deg), ha)
        if e is not None:
            lim = _entry_limit(e, steps)
            if best is None or lim < best["limit_s"] - 0.5:
                best = {"limit_s": lim, "ha_h": round(ha, 2), "alt": round(alt, 1),
                        "dec": round(float(dec_deg), 1), "side": side(ha),
                        "region": e.get("label"), "scope": e.get("scope"),
                        "source": e.get("source") or "measured",
                        "confidence": e.get("confidence") or "low",
                        "fails_all": bool(e.get("fails_all")),
                        "uncertain": bool(e.get("uncertain")),
                        "probe_s": e.get("probe_s"), "n": e.get("n"),
                        "at": t.replace(microsecond=0).isoformat() + "Z"}
        if t >= end:
            break
        t = min(end, t + timedelta(minutes=SAMPLE_MIN))
    if best:
        best["why"] = (f"unguided limit learned: {best['limit_s']:g} s at Dec "
                       f"{best['dec']:+.0f} {best['side']}"
                       + (f" ({best['scope']}, {best['source']}, "
                          f"{best['confidence']})"))
        if best["fails_all"]:
            best["why"] += f": trails even at {best['limit_s']:g} s"
    return best


def snap_length(config, rig: str, limit_s: float, goal_s: float, *,
                status=None) -> tuple[float, dict | None]:
    """The sub length for a set: the longest ladder length at or under
    min(limit, goal) whose darks match, else one whose darks scale from a
    bias, else the longest. (seconds, dark_match info). status: test hook
    (seconds -> dark_match dict), default night_pilot.dark_match."""
    top = min(float(limit_s), float(goal_s))
    cands = [s for s in ladder(config, rig) if s <= top + 0.5]
    if not cands:
        return top, None
    if status is None:
        from photonscript.scheduler.night_pilot import dark_match

        def status(x):
            return dark_match(config, rig, x)
    info = {}
    for want in ("matched", "scaled"):
        for s in cands:
            if s not in info:
                try:
                    info[s] = status(s) or {"match": "none", "text": ""}
                except Exception:  # noqa: BLE001
                    info[s] = {"match": "none", "text": ""}
            if info[s].get("match") == want:
                return s, info[s]
    return cands[0], info.get(cands[0])


def _set_length(e, new_s: float):
    """A copy of ExposurePlan e at new_s (same integration owed, like
    target_planner.cap_unguided)."""
    owed = e.count - e.acquired
    return e.model_copy(update=dict(
        exposure_seconds=float(new_s), acquired=0, acquired_s=0.0,
        count=max(1, math.ceil(owed * e.exposure_seconds / float(new_s) - 1e-9))))


def _overrides_for(projects, name: str) -> dict:
    from photonscript.shared.target_names import target_key
    k = target_key(name)
    for p in projects or []:
        try:
            if target_key(p.target.name) == k:
                return dict(getattr(p, "exposure_overrides", None) or {})
        except Exception:  # noqa: BLE001
            continue
    return {}


def apply_targets(config, targets, projects=None, *, tbl: dict | None = None,
                  status=None, now: datetime | None = None) -> dict:
    """Planner integration (module doc). Edits targets in place only in
    mode apply (and the probe only in probe mode apply). Returns the plan
    record: {mode, probe_mode, targets: [{target, filter, from_s, to_s,
    applied, why, dark, ...}], probes: [...], piggyback: {...}}."""
    m, pm = mode(config), probe_mode(config)
    rec = {"mode": m, "probe_mode": pm, "targets": [], "probes": [], "piggyback": None}
    if m == "off":
        return rec
    doc = tbl if tbl is not None else table(config)
    rc = ((doc or {}).get("rigs") or {}).get("rc16") if doc else None
    pg = ((doc or {}).get("rigs") or {}).get("piggyback") if doc else None
    piggy_best = None
    for t in targets or []:
        if getattr(t, "focus_calibration", False) or getattr(t, "tracking_test", False) \
                or getattr(t, "optics_test", False):
            continue
        guided = bool(getattr(t, "start_guiding", False))
        ub = list(getattr(t, "unguided_filters", None) or [])
        if guided and not ub:
            continue
        w0 = getattr(t, "window_start_utc", None) or now
        w1 = getattr(t, "window_end_utc", None) or w0
        try:
            dec_ra = (float(t.ra_hours), float(t.dec_degrees))
        except (TypeError, ValueError, AttributeError):
            continue
        if pg:
            pb = plan_target(config, pg, dec_ra[0], dec_ra[1], w0, w1)
            if pb and (piggy_best is None or pb["limit_s"] < piggy_best["limit_s"]):
                piggy_best = {**pb, "target": t.name}
        lim = plan_target(config, rc, dec_ra[0], dec_ra[1], w0, w1) if rc else None
        if not lim:
            continue
        ov = _overrides_for(projects, t.name)
        goal_s = max([e.exposure_seconds for e in t.exposures
                      if (getattr(e, "rig", "rc16") or "rc16") == "rc16"
                      and (not guided or e.filter_type.value in ub)] or [0.0])
        new_exps, changed_any = [], False
        for e in t.exposures:
            fv = e.filter_type.value
            if guided and fv not in ub:
                new_exps.append(e)
                continue
            if (getattr(e, "rig", "rc16") or "rc16") != "rc16" or e.count - e.acquired <= 0:
                new_exps.append(e)
                continue
            row = {"target": t.name, "filter": fv, "from_s": e.exposure_seconds,
                   "limit_s": lim["limit_s"], "region": lim["region"],
                   "source": lim["source"], "confidence": lim["confidence"],
                   "why": lim["why"], "applied": False}
            if ov.get(fv):
                row.update(to_s=e.exposure_seconds, note="hand-set exposure override wins")
                rec["targets"].append(row)
                new_exps.append(e)
                continue
            if lim["limit_s"] >= e.exposure_seconds - 0.5:
                new_exps.append(e)
                continue
            s, dm = snap_length(config, "rc16", lim["limit_s"], e.exposure_seconds,
                                status=status)
            row.update(to_s=s, dark=(dm or {}).get("match"),
                       dark_text=(dm or {}).get("text"))
            if m == "apply" and s < e.exposure_seconds - 0.5:
                new_exps.append(_set_length(e, s))
                row["applied"] = True
                changed_any = True
            else:
                new_exps.append(e)
            rec["targets"].append(row)
        if changed_any:
            t.exposures = new_exps
        _probe(config, t, lim, rec, pm, guided, ub, goal_s)
    if piggy_best:
        cur = rig_cap(config, "piggyback") or 0
        s = None
        if cur and piggy_best["limit_s"] < cur - 0.5:
            s, dm = snap_length(config, "piggyback", piggy_best["limit_s"], cur,
                                status=status)
        rec["piggyback"] = {**piggy_best, "from_s": cur, "to_s": s or cur,
                            "applied": bool(m == "apply" and s and s < cur - 0.5)}
    lines = [f"{r['target']} {r['filter']} {r['from_s']:g}->{r.get('to_s', r['from_s']):g} s"
             f" ({'applied' if r['applied'] else m}): {r['why']}" for r in rec["targets"]]
    if lines:
        logger.info("Unguided limits (%s): %s", m, "; ".join(lines))
    return rec


def _probe(config, t, lim: dict, rec: dict, pm: str, guided: bool, ub: list,
           goal_s: float) -> None:
    if pm == "off" or not lim.get("uncertain") or not lim.get("probe_s"):
        return
    ps = float(lim["probe_s"])
    cands = [e for e in t.exposures
             if (getattr(e, "rig", "rc16") or "rc16") == "rc16"
             and e.count - e.acquired > 0
             and (not guided or e.filter_type.value in ub)]
    if not cands:
        return
    base = max(cands, key=lambda e: (e.count - e.acquired) * e.exposure_seconds)
    if ps > goal_s + 0.5 or (pm == "apply" and ps <= base.exposure_seconds + 0.5):
        return      # the goal never runs that long, or the set already does
    n = probe_subs(config)
    row = {"target": t.name, "filter": base.filter_type.value, "probe_s": ps,
           "subs": n, "limit_s": lim["limit_s"], "region": lim["region"],
           "applied": False,
           "why": (f"uncertain at {lim['region']} ({lim['source']}, "
                   f"{lim['confidence']}): try {n} x {ps:g} s, kept only if they pass")}
    if pm == "apply":
        owed = base.count - base.acquired
        cut = max(0, min(owed - 1, round(n * ps / base.exposure_seconds)))
        probe = base.model_copy(update=dict(
            exposure_seconds=ps, count=n, acquired=0, acquired_s=0.0,
            hdr_short_seconds=None, hdr_short_count=0, hdr_short_acquired=0))
        new = []
        for e in t.exposures:
            if e is base:
                new.append(probe)
                new.append(e.model_copy(update=dict(count=e.count - cut)) if cut else e)
            else:
                new.append(e)
        t.exposures = new
        row["applied"] = True
    rec["probes"].append(row)


# --------------------------------------------------------------------------
# calibration: the dark lengths the table needs
# --------------------------------------------------------------------------

def learned_dark_lengths(config, rig: str, doc: dict | None = None) -> list[float]:
    """Ladder lengths the learned table uses for the rig (each region's
    limit, its probe step and, for a limit from the drift model, the step
    under it; at or under the rig's cap), most used first, at most
    unguided_limit_darks_max. [] in mode off or with no table. Reads
    the store only (never recomputes: the dark quota calls this)."""
    if mode(config) == "off":
        return []
    cap_n = int(_f(config, "unguided_limit_darks_max", 3))
    if cap_n <= 0:
        return []
    d = doc if doc is not None else load(config)
    tbl = ((d or {}).get("rigs") or {}).get(rig)
    if not tbl:
        return []
    cap = rig_cap(config, rig)
    steps = list(tbl.get("ladder") or ladder(config, rig))
    counts: Counter = Counter()
    for grp in [*(tbl.get("regions") or []), *(tbl.get("dec_bands") or []),
                tbl.get("overall") or {}]:
        lim = _num(grp.get("limit_s"))
        below = None
        if lim and grp.get("source") == "drift model":
            # an untested limit: darks for the step under it too (where a
            # night that trails after all steps down to)
            lower = [x for x in steps if x < lim - 0.5]
            below = max(lower) if lower else None
        for v, w in ((lim, 3), (_num(grp.get("probe_s")), 2), (below, 1)):
            if v and (cap is None or v <= cap + 0.5):
                counts[float(v)] += w
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))
    return [s for s, _n in ranked[:cap_n]]


# --------------------------------------------------------------------------
# report lines
# --------------------------------------------------------------------------

def _rate_txt(d: dict) -> str:
    return f"{d['pass']}/{d['n']}"


def table_rows(tbl: dict) -> list[dict]:
    """Flat rows for the dashboard / CLI table (regions, then Dec bands,
    then the whole sky)."""
    out = []
    for scope, grp in (("region", tbl.get("regions") or []),
                       ("dec band", tbl.get("dec_bands") or []),
                       ("whole sky", [tbl.get("overall") or {}])):
        for r in grp:
            if not r.get("n"):
                continue
            out.append({"scope": scope, "label": r.get("label"), "n": r.get("n"),
                        "alt_med": r.get("alt_med"),
                        "lengths": {k: _rate_txt(v) for k, v in (r.get("lengths") or {}).items()},
                        "drift_arcsec_min": r.get("drift_arcsec_min"),
                        "limit_s": r.get("limit_s"), "source": r.get("source"),
                        "confidence": r.get("confidence"),
                        "fails_all": r.get("fails_all"),
                        "uncertain": r.get("uncertain"), "probe_s": r.get("probe_s"),
                        "nights": r.get("nights")})
    return out


def format_table(doc: dict, rig: str | None = None) -> str:
    lines = [f"Unguided limits (refreshed {doc.get('refreshed_at')}, evidence since "
             f"{doc.get('since') or 'lookback start'}, mode {doc.get('mode')}, "
             f"probe {doc.get('probe')})"]
    for rg, tbl in (doc.get("rigs") or {}).items():
        if rig and rg != rig:
            continue
        lines.append(f"{rg}: gate ecc {tbl.get('gate_ecc')}, pass rate "
                     f"{tbl.get('pass_rate')}, ladder "
                     f"{'/'.join(f'{s:g}' for s in tbl.get('ladder') or [])} s, "
                     f"{tbl.get('n_used')} subs used, excluded "
                     + (", ".join(f"{k} {v}" for k, v in sorted(
                         (tbl.get('excluded') or {}).items())) or "none"))
        rows = table_rows(tbl)
        if not rows:
            lines.append("  no unguided evidence yet")
        for r in rows:
            lim = ("trails at every step" if r["fails_all"] else
                   (f"{r['limit_s']:g} s" if r["limit_s"] else "undecided"))
            lines.append(
                f"  {r['scope']:9s} {r['label']:32s} n {r['n']:3d}  "
                + " ".join(f"{k}s {v}" for k, v in r["lengths"].items())
                + (f"  drift {r['drift_arcsec_min']}\"/min" if r["drift_arcsec_min"] else "")
                + f"  limit {lim}"
                + (f" ({r['source']}, {r['confidence']})" if r["limit_s"] else "")
                + (f" probe {r['probe_s']:g} s" if r["probe_s"] else ""))
    return "\n".join(lines)


def morning_line(config, date: str, *, night_data: dict | None = None,
                 doc: dict | None = None) -> str | None:
    """One line: tonight's unguided evidence on the RC16 per region and
    length, and what the plan did or would do with the learned limits."""
    if mode(config) == "off":
        return None
    nd = night_data if night_data is not None else _local_night(config, date)
    try:
        rows, excl = evidence(config, "rc16", date, nd.get("subs") or [],
                              plan=nd.get("plan"), events=nd.get("events"),
                              pointing=nd.get("pointing"))
    except Exception as e:  # noqa: BLE001
        logger.debug("unguided limits morning line: %s", e)
        return None
    plan_rec = (nd.get("plan") or {}).get("unguided_limits") or {}
    bits = []
    groups: dict[tuple, dict] = {}
    for r in rows:
        g = groups.setdefault((str(r["target"]), round(r["dec"]), side(r["ha_h"])), {})
        g.setdefault(r["exp_s"], []).append(r["pass"])
    for (tg, dec, sd), by in sorted(groups.items()):
        bits.append(f"{tg} (Dec {dec:+d} {sd}) "
                    + ", ".join(f"{e:g} s {sum(v)}/{len(v)}" for e, v in sorted(by.items()))
                    + " round")
    advice = []
    for r in plan_rec.get("targets") or []:
        if r.get("to_s") and r["to_s"] < r["from_s"] - 0.5:
            advice.append(f"{r['target']} {r['filter']} {r['from_s']:g}->{r['to_s']:g} s"
                          + (" applied" if r.get("applied") else " suggested"))
    for p in plan_rec.get("probes") or []:
        advice.append(f"probe {p['target']} {p['subs']} x {p['probe_s']:g} s"
                      + (" ran" if p.get("applied") else " suggested"))
    if not bits and not advice and not excl.get("guider_conflict"):
        return None
    line = "Unguided limits: "
    line += ("tonight " + "; ".join(bits)) if bits else "no clean unguided subs tonight"
    if excl.get("guider_conflict"):
        line += f" ({excl['guider_conflict']} excluded: PHD2 active)"
    if advice:
        line += ". Plan: " + "; ".join(advice[:4])
        if mode(config) == "advise" and any(not r.get("applied") for r in plan_rec.get("targets") or []):
            line += " (unguided_auto_length=apply to use them)"
    return line
