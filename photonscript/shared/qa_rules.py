"""PS-21: one set of QA rules for every grader (the "scorecard").

Live grading (telescope_agent.agent via image_validator) and backfill /
regrade (scheduler.runs._fast_grade, rescore_night) both measure a sub and
then call ``evaluate`` here, so the two can never disagree about what a
limit is or which check rejected a sub.

    thresholds(config, rig, target, filter) -> dict   the ONLY place limits
                                                      are read (per rig, with
                                                      the PS-48 per-target
                                                      override hook)
    context(config, rig, target, filter, ...) -> QAContext
    evaluate(metrics, ctx) -> Scorecard               pure: no FITS, no I/O

A Scorecard is the "big list of things that are graded": one Check per rule
with the measured value, the limit actually used, a status (pass / warn /
fail / skip) and a plain reason. Verdict: any fail -> "rejected" (every
failing check is a driver); else any warn -> "needs-look"; else "approved"
(all green, auto-approved unless qa_auto_approve is off).

PS-91 adds the metrics key guide_lock ("star" | "non-star"): was PHD2 guiding
on a real star during the sub (see the guide_lock check and the end of this
module).

``metrics`` keys (all optional; a missing input makes its check "skip"):
  hfr (native px), fwhm_arcsec (only when the grader truly measured FWHM),
  ecc, stars, background, exp_s, ccd_temp, set_temp (header SET-TEMP),
  guide_rms, guide_state, doubled_frac, exposure (ok|under|clipped|sat-stars),
  clipped_pct, sat_stars_pct, swamp, pointing_offset_arcmin.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

RULES_VERSION = "ps21.1"

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
APPROVED, NEEDS_LOOK, REJECTED = "approved", "needs-look", "rejected"

# id -> (label, unit, "what I want to see"); order = scorecard order
CHECKS: dict[str, tuple[str, str, str]] = {
    "ecc": ("Eccentricity", "", "round stars (no trailing, wind or drift)"),
    "hfr": ("HFR (focus)", "px", "tight stars (autofocus held)"),
    "hfr_rel": ("HFR vs night median", "px",
                "as sharp as the night's other subs of this target + filter"),
    "fwhm": ("FWHM (focus)", "\"", "star size within the seeing budget"),
    "stars": ("Star count", "", "enough real stars, no defocus flood"),
    "bg_rel": ("Background vs night median", "ADU",
               "no moon, twilight or cloud glow"),
    "bg_floor": ("Background vs bias floor", "ADU", "sky signal above the bias"),
    "temp": ("Sensor temp vs setpoint", "C", "sensor at the configured setpoint"),
    "guide_lock": ("Guide star is a star", "",
                   "PHD2 guiding on a real star, not a hot pixel or artifact"),
    "guide_rms": ("Guiding RMS (snapshot at sub end)", "\"",
                  "guiding under the RMS limit"),
    "tracking_jump": ("Tracking jump (doubled stars)", "",
                      "no mount jump doubling the stars"),
    "exposure": ("Exposure (clipping)", "", "no clipped pixels or blown star cores"),
    "roof": ("Roof open / not parked", "",
             "a real sky frame (not a closed-roof or parked dark)"),
    "pointing": ("On target", "'", "frame centered on the target"),
}


# --------------------------------------------------------------- thresholds

def _f(cfg, name, default):
    v = getattr(cfg, name, default)
    return default if v is None else v


def _target_overrides(config) -> dict:
    """PS-48 hook: ``qa_target_overrides`` is a JSON object mapping a target
    name (case-insensitive; optionally "target|filter" or "rig:target") to
    threshold overrides, e.g. {"Cat's Eye Nebula": {"hfr_max": 6.0}}."""
    raw = getattr(config, "qa_target_overrides", "") or ""
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw) if str(raw).strip() else {}
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}


def _rig_auto_approves(config, rig: str) -> bool:
    """qa_auto_approve_rigs: comma list of rigs whose all-green subs are
    auto-approved; empty means every rig."""
    raw = str(_f(config, "qa_auto_approve_rigs", "rc16") or "").strip()
    if not raw:
        return True
    rigs = {r.strip().lower() for r in raw.split(",") if r.strip()}
    return str(rig or "rc16").lower() in rigs


def thresholds(config, rig: str = "rc16", target: str | None = None,
               filter: str | None = None) -> dict:  # noqa: A002
    """Every limit the rules use, resolved for one rig (and target/filter).

    The rig view comes from shared.rigs.rig_config, so the Piggy-600's own
    HFR / FWHM / ecc / setpoint / offset apply to its subs. Values are the
    ones actually in force (env on the scope PC wins over code defaults: on
    2026-09-27 PS_QUALITY_ECCENTRICITY_MAX=0.6 while the code default is 0.70).
    """
    from photonscript.shared.rigs import rig_config, rig_setpoint
    rig = rig or "rc16"
    cfg = rig_config(config, rig)
    t = {
        "rig": rig,
        "pixel_scale": float(_f(cfg, "pixel_scale_arcsec", 1.0)),
        "ecc_max": float(_f(cfg, "quality_eccentricity_max", 0.70)),
        "hfr_max": float(_f(cfg, "quality_hfr_abs_max", 10.0)),
        "hfr_rel_factor": float(_f(config, "qa_hfr_outlier_factor", 1.4)),
        "hfr_rel_min_subs": int(_f(config, "qa_night_min_subs", 5)),
        "fwhm_max": float(_f(cfg, "quality_fwhm_max", 4.0)),
        "fwhm_soft": bool(_f(cfg, "quality_fwhm_soft", False)),
        "star_min": int(_f(config, "quality_star_min", 5)),
        "star_max": int(_f(config, "quality_star_max", 5000)),
        "bg_rel_max": float(_f(config, "qa_background_rel_max", 2.0)),
        "bias_floor": float(_f(cfg, "default_offset", 0) or 0)
        + float(_f(cfg, "quality_bias_floor_margin_adu", 6.0)),
        "setpoint_c": float(rig_setpoint(config, rig)),
        "temp_over_c": float(_f(config, "sub_temp_over_setpoint_c", 5.0)),
        "temp_max_c": float(_f(config, "sub_temp_max_c", 10.0)),
        "cooling_tol_c": float(_f(config, "cooling_tolerance_c", 1.0)),
        "guide_rms_max": float(_f(cfg, "quality_tracking_rms_max", 1.5)),
        # PS-70: the PHD2 client's RMS is in guide-camera px, not arcsec, so
        # the gate stays informational ("info") until that fix lands; then
        # set qa_guide_rms_mode to "fail" (or "warn").
        "guide_rms_mode": str(_f(config, "qa_guide_rms_mode", "info")).lower(),
        # PS-91: a sub guided on a non-star lock (guard episode overlapping
        # it): "warn" (default) or "fail"
        "guide_lock_mode": str(_f(config, "qa_guide_lock_mode", "warn")).lower(),
        "doubled_max": float(_f(config, "qa_tracking_jump_max", 0.25)),
        "offtarget_max_arcmin": float(_f(config, "quality_offtarget_max_arcmin",
                                         5.0)),
        "warn_fraction": float(_f(config, "qa_warn_fraction", 0.10)),
        "auto_approve": bool(_f(config, "qa_auto_approve", True))
        and _rig_auto_approves(config, rig),
    }
    ov = _target_overrides(config)
    if ov and target:
        tl = str(target).strip().lower()
        keys = [tl, f"{rig}:{tl}"]
        if filter:
            keys += [f"{tl}|{str(filter).lower()}", f"{rig}:{tl}|{str(filter).lower()}"]
        applied = []
        lower = {str(k).strip().lower(): v for k, v in ov.items()}
        for k in keys:
            o = lower.get(k)
            if isinstance(o, dict):
                for kk, vv in o.items():
                    if kk in t and kk != "rig":
                        t[kk] = type(t[kk])(vv) if not isinstance(t[kk], bool) else bool(vv)
                        applied.append(kk)
        if applied:
            t["override"] = sorted(set(applied))
    return t


# ------------------------------------------------------------------ model

@dataclass
class Check:
    id: str
    value: Any
    limit: Any
    status: str
    reason: str = ""

    @property
    def label(self) -> str:
        return CHECKS.get(self.id, (self.id, "", ""))[0]

    @property
    def unit(self) -> str:
        return CHECKS.get(self.id, ("", "", ""))[1]

    @property
    def why(self) -> str:
        return CHECKS.get(self.id, ("", "", ""))[2]

    def row(self) -> list:
        """Compact storage row [id, value, limit, status(, reason)]. The
        reason is kept for fail / warn and notes; a skip keeps only its
        status (expand() supplies the generic text)."""
        if self.reason and self.status != SKIP:
            return [self.id, self.value, self.limit, self.status, self.reason]
        return [self.id, self.value, self.limit, self.status]

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.label, "value": self.value,
                "unit": self.unit, "limit": self.limit, "status": self.status,
                "reason": self.reason, "why": self.why}


@dataclass
class Scorecard:
    checks: list[Check] = field(default_factory=list)
    thresholds: dict = field(default_factory=dict)
    qa_flag: str = ""
    rules_version: str = RULES_VERSION

    @property
    def drivers(self) -> list[str]:
        return [c.id for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[str]:
        return [c.id for c in self.checks if c.status == WARN]

    @property
    def passed(self) -> bool:
        return not self.drivers

    @property
    def verdict(self) -> str:
        if self.drivers:
            return REJECTED
        return NEEDS_LOOK if self.warnings else APPROVED

    @property
    def reasons(self) -> list[str]:
        return [c.reason for c in self.checks if c.status == FAIL and c.reason]

    @property
    def reason(self) -> str:
        """Backward-compatible `reason` string: every failing check."""
        return "; ".join(self.reasons)

    @property
    def auto_approved(self) -> bool:
        return self.verdict == APPROVED and bool(
            self.thresholds.get("auto_approve", True))

    def compact(self) -> dict:
        """What the jsonl record stores (about 400-700 bytes)."""
        return {"v": self.rules_version, "verdict": self.verdict,
                "rows": [c.row() for c in self.checks]}

    def as_dict(self) -> dict:
        return {"rules_version": self.rules_version, "verdict": self.verdict,
                "passed": self.passed, "drivers": self.drivers,
                "warnings": self.warnings, "reason": self.reason,
                "qa_flag": self.qa_flag,
                "checks": [c.as_dict() for c in self.checks],
                "thresholds": self.thresholds}

    def record_fields(self) -> dict:
        """Fields both graders merge into a sub record."""
        out = {"passed_qa": self.passed, "reason": self.reason,
               "qa_flag": self.qa_flag, "scorecard": self.compact(),
               "auto_verdict": self.verdict, "auto_reason": self.reason,
               "drivers": self.drivers}
        if self.auto_approved:
            out.update(reviewed=True, review_source="auto")
        return out


SKIP_TEXT = {
    "hfr_rel": "needs enough subs of this target + filter tonight",
    "bg_rel": "needs enough subs of this target + filter tonight",
    "fwhm": "not measured by this grader",
    "tracking_jump": "not measured by this grader",
    "guide_rms": "not judged (unguided, or PS-70 units pending)",
    "guide_lock": "no guard data for this sub (PS-91)",
    "pointing": "pending PS-67 (pointing data)",
}


def expand(compact: dict | None) -> list[dict]:
    """Stored compact card -> labeled rows (API / UI)."""
    rows = []
    for r in (compact or {}).get("rows", []):
        try:
            c = Check(*r[:5])
        except TypeError:
            continue
        if c.status == SKIP and not c.reason:
            c.reason = SKIP_TEXT.get(c.id, "not measured")
        rows.append(c.as_dict())
    return rows


@dataclass
class QAContext:
    config: Any                      # rig config view (PS-71 signatures)
    thresholds: dict
    night: dict | None = None        # {"hfr_median", "bg_median", "n"}
    unsafe_windows: list | None = None
    start_utc: datetime | None = None
    image_type: str = "LIGHT"


def context(config, rig: str = "rc16", target: str | None = None,
            filter: str | None = None, *, night: dict | None = None,  # noqa: A002
            unsafe_windows: list | None = None,
            start_utc: datetime | None = None,
            image_type: str = "LIGHT") -> QAContext:
    from photonscript.shared.rigs import rig_config
    return QAContext(config=rig_config(config, rig or "rc16"),
                     thresholds=thresholds(config, rig, target, filter),
                     night=night, unsafe_windows=unsafe_windows,
                     start_utc=start_utc, image_type=image_type)


# ------------------------------------------------------------------- rules

def _num(v):
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _r(v, nd=3):
    return None if v is None else round(v, nd)


def _max_check(cid, value, limit, warn_frac, fail_text, nd=3):
    """value <= limit passes; within warn_frac of the limit warns."""
    if value is None:
        return Check(cid, None, limit, SKIP, "not measured")
    if value > limit:
        return Check(cid, _r(value, nd), limit, FAIL, fail_text)
    if warn_frac > 0 and value > limit * (1.0 - warn_frac):
        return Check(cid, _r(value, nd), limit, WARN,
                     f"within {warn_frac:.0%} of the limit")
    return Check(cid, _r(value, nd), limit, PASS)


def sensor_temp_reasons(ccd_temp, header_setpoint, config,
                        setpoint: float | None = None) -> list[str]:
    """Rejection reasons for a warm sub (empty list = temperature is fine).

    Judged against the CONFIGURED setpoint, never the header SET-TEMP: on
    2026-09-26 the camera was left with SET-TEMP=20, so 22-25C subs looked
    'at setpoint' and passed into review. The header value is only reported.
    Two rules: more than sub_temp_over_setpoint_c above setpoint, or above the
    absolute ceiling sub_temp_max_c (default 10C) whatever the setpoint."""
    t = _num(ccd_temp)
    if t is None:
        return []
    sp = float(setpoint if setpoint is not None
               else getattr(config, "camera_setpoint_c", 0.0))
    over = float(getattr(config, "sub_temp_over_setpoint_c", 5.0))
    ceiling = float(getattr(config, "sub_temp_max_c", 10.0))
    return _temp_reasons(t, _num(header_setpoint), sp, over, ceiling)


def _temp_reasons(t, hdr_sp, sp, over, ceiling) -> list[str]:
    hdr_note = ""
    if hdr_sp is not None and abs(hdr_sp - sp) > 1.0:
        hdr_note = f"; camera was set to {hdr_sp:.0f}C"
    if t > sp + over:
        return [f"sensor {t:.0f}C vs setpoint {sp:.0f}C (cooler failure{hdr_note})"]
    if t > ceiling:
        return [f"sensor {t:.0f}C above {ceiling:.0f}C limit{hdr_note}"]
    return []


def evaluate(metrics: dict, ctx: QAContext) -> Scorecard:
    """Grade one sub. Pure function of the metrics and the context."""
    m = metrics or {}
    t = ctx.thresholds
    wf = float(t.get("warn_fraction", 0.10))
    checks: list[Check] = []

    hfr = _num(m.get("hfr"))
    fwhm = _num(m.get("fwhm_arcsec"))
    ecc = _num(m.get("ecc"))
    stars = _num(m.get("stars"))
    bg = _num(m.get("background"))
    exp_s = _num(m.get("exp_s"))

    # ecc
    checks.append(_max_check(
        "ecc", ecc, t["ecc_max"], wf,
        f"Eccentricity {ecc:.2f} > {t['ecc_max']:g} (trailing/drift)"
        if ecc is not None else ""))
    # hfr absolute
    checks.append(_max_check(
        "hfr", hfr, t["hfr_max"], wf,
        f"HFR {hfr:.1f}px > {t['hfr_max']:g}px (out of focus)"
        if hfr is not None else "", nd=2))
    # hfr vs night median (rig + target + filter)
    night = ctx.night or {}
    med = _num(night.get("hfr_median"))
    n_night = int(night.get("n_hfr") or 0)
    if hfr is None:
        checks.append(Check("hfr_rel", None, None, SKIP, "not measured"))
    elif med is None or n_night < t["hfr_rel_min_subs"]:
        checks.append(Check("hfr_rel", _r(hfr, 2), None, SKIP,
                            f"needs {t['hfr_rel_min_subs']}+ subs of this "
                            "target + filter tonight"))
    else:
        lim = round(med * t["hfr_rel_factor"], 2)
        if hfr > lim:
            checks.append(Check("hfr_rel", _r(hfr, 2), lim, FAIL,
                                f"HFR outlier: {hfr:g} vs night median "
                                f"{med:g} (x{t['hfr_rel_factor']:g} limit)"))
        else:
            checks.append(Check("hfr_rel", _r(hfr, 2), lim, PASS))
    # fwhm (hard on RC16, advisory where quality_fwhm_soft)
    if fwhm is None:
        checks.append(Check("fwhm", None, t["fwhm_max"], SKIP,
                            "not measured by this grader"))
    elif t["fwhm_soft"]:
        checks.append(Check("fwhm", _r(fwhm, 2), t["fwhm_max"],
                             WARN if fwhm > t["fwhm_max"] else PASS,
                             f"FWHM {fwhm:.1f}\" > {t['fwhm_max']:g}\" "
                             "(advisory on this rig)"
                             if fwhm > t["fwhm_max"] else ""))
    else:
        checks.append(_max_check(
            "fwhm", fwhm, t["fwhm_max"], wf,
            f"FWHM {fwhm:.1f}\" > {t['fwhm_max']:g}\"", nd=2))
    # star count
    lim_s = [t["star_min"], t["star_max"]]
    if stars is None:
        checks.append(Check("stars", None, lim_s, SKIP, "not measured"))
    elif stars < t["star_min"]:
        checks.append(Check("stars", int(stars), lim_s, FAIL,
                            f"Only {int(stars)} stars detected "
                            f"(minimum {t['star_min']})"))
    elif stars > t["star_max"]:
        checks.append(Check("stars", int(stars), lim_s, FAIL,
                            f"{int(stars)} stars > {t['star_max']} "
                            "(defocus/false detections)"))
    else:
        checks.append(Check("stars", int(stars), lim_s, PASS))
    # background vs night median (warn only)
    bmed = _num(night.get("bg_median"))
    n_bg = int(night.get("n_bg") or 0)
    if bg is None:
        checks.append(Check("bg_rel", None, None, SKIP, "not measured"))
    elif bmed is None or bmed <= 0 or n_bg < t["hfr_rel_min_subs"]:
        checks.append(Check("bg_rel", _r(bg, 1), None, SKIP,
                            f"needs {t['hfr_rel_min_subs']}+ subs of this "
                            "target + filter tonight"))
    else:
        lim = round(bmed * t["bg_rel_max"], 1)
        checks.append(Check("bg_rel", _r(bg, 1), lim,
                            WARN if bg > lim else PASS,
                            f"background {bg:g} ADU > {t['bg_rel_max']:g}x "
                            f"night median {bmed:g} (moon, twilight, cloud "
                            "glow?)" if bg > lim else ""))
    # background vs bias floor (warn; the reject lives in the roof check)
    floor = t["bias_floor"]
    if bg is None or floor <= 0:
        checks.append(Check("bg_floor", _r(bg, 1), floor or None, SKIP,
                            "not measured" if bg is None else "no offset set"))
    elif bg <= floor:
        checks.append(Check("bg_floor", _r(bg, 1), floor, WARN,
                            f"background {bg:g} ADU at the bias floor "
                            f"(<= {floor:g}): little or no sky signal"))
    else:
        checks.append(Check("bg_floor", _r(bg, 1), floor, PASS))
    # sensor temp vs configured setpoint
    temp = _num(m.get("ccd_temp"))
    sp = t["setpoint_c"]
    lim_t = round(sp + t["temp_over_c"], 1)
    if temp is None:
        checks.append(Check("temp", None, lim_t, SKIP, "no sensor reading"))
    else:
        rs = _temp_reasons(temp, _num(m.get("set_temp")), sp,
                           t["temp_over_c"], t["temp_max_c"])
        if rs:
            checks.append(Check("temp", _r(temp, 1), lim_t, FAIL, rs[0]))
        elif temp > sp + t["cooling_tol_c"]:
            checks.append(Check("temp", _r(temp, 1), lim_t, WARN,
                                f"sensor {temp:.1f}C above setpoint {sp:g}C "
                                f"+ {t['cooling_tol_c']:g}C tolerance"))
        else:
            checks.append(Check("temp", _r(temp, 1), lim_t, PASS))
    # PS-91: was PHD2 guiding on a real star? A hot-pixel lock has a tiny
    # RMS, so the RMS of such a sub says nothing and is not judged.
    lock = str(m.get("guide_lock") or "").lower()
    nonstar = lock == "non-star"
    if nonstar:
        checks.append(Check("guide_lock", lock, "star",
                            FAIL if t.get("guide_lock_mode") == "fail" else WARN,
                            "PHD2 was guiding on a non-star (hot pixel or "
                            "artifact) during this sub: effectively unguided"))
    elif lock == "star":
        checks.append(Check("guide_lock", lock, "star", PASS))
    else:
        checks.append(Check("guide_lock", None, "star", SKIP,
                            "no guard data for this sub"))
    # guiding RMS snapshot (only judged while guiding)
    rms = _num(m.get("guide_rms"))
    gstate = str(m.get("guide_state") or "").lower()
    if nonstar:
        checks.append(Check("guide_rms", _r(rms, 2), t["guide_rms_max"], SKIP,
                            "guided on a non-star lock: the RMS is meaningless"))
    elif rms is None:
        checks.append(Check("guide_rms", None, t["guide_rms_max"], SKIP,
                            "no guiding data"))
    elif gstate not in ("guiding", "settling"):
        checks.append(Check("guide_rms", _r(rms, 2), t["guide_rms_max"], SKIP,
                            f"not guiding ({gstate or 'unknown'})"))
    elif t.get("guide_rms_mode", "info") not in ("fail", "warn"):
        checks.append(Check("guide_rms", _r(rms, 2), t["guide_rms_max"], SKIP,
                            "recorded, not judged: PHD2 client units "
                            "unverified until PS-70"))
    elif rms > t["guide_rms_max"]:
        checks.append(Check("guide_rms", _r(rms, 2), t["guide_rms_max"],
                            FAIL if t["guide_rms_mode"] == "fail" else WARN,
                            f"Tracking RMS {rms:.2f}\" > "
                            f"{t['guide_rms_max']:g}\""))
    else:
        checks.append(_max_check("guide_rms", rms, t["guide_rms_max"], wf,
                                 "", nd=2))
    # tracking jump (doubled stars)
    dbl = _num(m.get("doubled_frac"))
    if dbl is None:
        checks.append(Check("tracking_jump", None, t["doubled_max"], SKIP,
                            "not measured by this grader"))
    elif dbl >= t["doubled_max"]:
        checks.append(Check("tracking_jump", _r(dbl, 2), t["doubled_max"], FAIL,
                            f"tracking jump: {round(dbl * 100)}% of stars "
                            "doubled at a consistent offset"))
    else:
        checks.append(Check("tracking_jump", _r(dbl, 2), t["doubled_max"], PASS))
    # exposure: clipping / saturated cores warn; read-noise-limited is a note
    ex = m.get("exposure")
    if not ex:
        checks.append(Check("exposure", None, None, SKIP, "not measured"))
    elif ex in ("clipped", "sat-stars"):
        detail = (f"{_num(m.get('sat_stars_pct'))}% of stars saturated"
                  if ex == "sat-stars"
                  else f"{_num(m.get('clipped_pct'))}% of pixels clipped")
        checks.append(Check("exposure", ex, "ok", WARN, detail))
    elif ex == "under":
        checks.append(Check("exposure", ex, "ok", PASS,
                            f"read-noise limited (swamp "
                            f"{_num(m.get('swamp'))}): longer subs pay off"))
    else:
        checks.append(Check("exposure", ex, "ok", PASS))
    # roof closed / parked (PS-71 signatures)
    qa_flag = ""
    try:
        from photonscript.shared.qa_signatures import parked_frame_verdict
        v = parked_frame_verdict(
            ctx.config, hfr_px=hfr, fwhm_arcsec=fwhm, background=bg,
            exp_s=exp_s, stars=stars, start_utc=ctx.start_utc,
            unsafe_windows=ctx.unsafe_windows, image_type=ctx.image_type)
        qa_flag = v.flag
        if v.reject:
            checks.append(Check("roof", v.flag or "reject", "sky frame", FAIL,
                                "; ".join(v.reasons)))
        else:
            checks.append(Check("roof", "sky", "sky frame", PASS))
    except Exception as e:  # noqa: BLE001 - never lose a grade over this
        checks.append(Check("roof", None, "sky frame", SKIP, f"check error: {e}"))
    # pointing (PS-67 will supply the offset)
    off = _num(m.get("pointing_offset_arcmin"))
    if off is None:
        checks.append(Check("pointing", None, t["offtarget_max_arcmin"], SKIP,
                            "pending PS-67 (pointing data)"))
    elif off > t["offtarget_max_arcmin"]:
        checks.append(Check("pointing", _r(off, 1), t["offtarget_max_arcmin"],
                            FAIL, f"off target by {off:.1f}' "
                            f"(> {t['offtarget_max_arcmin']:g}')"))
    else:
        checks.append(Check("pointing", _r(off, 1), t["offtarget_max_arcmin"],
                            PASS))
    return Scorecard(checks=checks, thresholds=t, qa_flag=qa_flag)


# ------------------------------------------------- night context / records

def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def group_key(rec: dict) -> tuple:
    return (rec.get("rig") or "rc16", str(rec.get("target") or "?"),
            str(rec.get("filter") or "?"))


def night_context(records: list[dict]) -> dict[tuple, dict]:
    """Per (rig, target, filter): median HFR and background of the night's
    subs (all of them, accepted or not: medians are robust)."""
    groups: dict[tuple, dict] = {}
    for r in records:
        g = groups.setdefault(group_key(r), {"hfr": [], "bg": []})
        h, b = _num(r.get("hfr")), _num(r.get("background"))
        if h:
            g["hfr"].append(h)
        if b is not None:
            g["bg"].append(b)
    return {k: {"hfr_median": _median(v["hfr"]), "n_hfr": len(v["hfr"]),
                "bg_median": _median(v["bg"]), "n_bg": len(v["bg"])}
            for k, v in groups.items()}


def metrics_from_record(rec: dict) -> dict:
    """Stored sub record -> evaluate() metrics. Backfill records (graded_by
    set) carry fwhm_arcsec = HFR x scale, not a measured FWHM: dropped."""
    m = {k: rec.get(k) for k in (
        "hfr", "ecc", "stars", "background", "exp_s", "ccd_temp", "set_temp",
        "guide_rms", "guide_state", "doubled_frac", "exposure", "clipped_pct",
        "sat_stars_pct", "swamp", "pointing_offset_arcmin")}
    m["fwhm_arcsec"] = None if rec.get("graded_by") else rec.get("fwhm_arcsec")
    return m


def record_metrics(**kw) -> dict:
    """The canonical metrics dict both graders build (unknown keys dropped)."""
    keys = ("hfr", "fwhm_arcsec", "ecc", "stars", "background", "exp_s",
            "ccd_temp", "set_temp", "guide_rms", "guide_state",
            "doubled_frac", "exposure", "clipped_pct", "sat_stars_pct",
            "swamp", "pointing_offset_arcmin")
    return {k: kw.get(k) for k in keys}


# PS-91: guide_lock rides along in both metrics builders. Added here, apart
# from the key tuples above, so other tickets can extend those freely.
GUIDE_LOCK_KEYS = ("guide_lock",)
_metrics_from_record_base = metrics_from_record
_record_metrics_base = record_metrics


def metrics_from_record(rec: dict) -> dict:  # noqa: F811
    m = _metrics_from_record_base(rec)
    m.update({k: rec.get(k) for k in GUIDE_LOCK_KEYS})
    return m


def record_metrics(**kw) -> dict:  # noqa: F811
    m = _record_metrics_base(**kw)
    m.update({k: kw.get(k) for k in GUIDE_LOCK_KEYS})
    return m


metrics_from_record.__doc__ = _metrics_from_record_base.__doc__
record_metrics.__doc__ = _record_metrics_base.__doc__
