"""PHD2 settings audit against a desired-state profile (PS-89).

Why: guiding the RC16 through the OAG depends on dozens of interacting PHD2,
mount-driver, TheSky and NINA settings, and the 09-18 to 09-26 failures each
traced to several wrong ones at once (600 mm focal length, 8-bit camera,
search region 15 px, mass tolerance 50%, no dark or defect map, 250 ms
calibration steps). PhotonScript owns the desired state
(config/phd2/desired_oag_rc16.toml, one "why" per row), audits it at every
guided arm and on PHD2 ConfigurationChange, explains each miss, and applies
the fixes it safely can.

    collect(config)           read every source (never raises):
        api          a short-lived second PHD2 JSON-RPC client
        profile      PHD2's stored profile (scheduler.phd2_profile_store;
                     unverified registry names read as candidates only)
        log          the newest guide-log header (phd2_logs / phd2_analysis),
                     inferred 8-bit from PS-88's saturated_star rule
        ga           the newest Guiding Assistant (phd2_analysis.ga_results /
                     latest_ga_before)
        calibration  PS-93 phd2_calibration.summary() (stale / poor flags,
                     recommended_step_ms, after-flip record)
        nina         NINA's guider settle / dither settings and the mount's
                     guide speed (PS-92 pulse_selftest.guide_speeds)
        ascom        Bisque driver flags (unknown until the names are known)
        thesky       TheSky TCP server reachable
        file         PHD2's dark library and defect map files
    evaluate(desired, observed, config)   pure: pass / warn / fail / unknown
                     / info per row, current vs desired, why, fix, target
    run_audit()      collect + evaluate + save <data_dir>/phd2_audit/<night>.json
    apply()          Option B: API rows (exposure, RA min-move, Dec guide mode)
                     only while PHD2 is Stopped or Looping, under
                     phd2_ops.hold("audit"); profile rows only with
                     phd2_audit_autofix, armer idle, phd2.exe closed, verified
                     registry names and a fresh backup. Mount driver, TheSky
                     and NINA rows are report only. Every change goes to
                     <data_dir>/phd2_audit/changes.jsonl.
    at_arm()         the armer's hook: one Pushover only on at least one FAIL.
    ReAuditor        the RC16 agent's ConfigurationChange listener: re-audit
                     60 s after the last change, push only on a new FAIL while
                     a night is armed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import socket
from datetime import datetime
from pathlib import Path

from photonscript.scheduler import phd2_profile_store as ps
from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

PASS, WARN, FAIL, UNKNOWN, INFO = "pass", "warn", "fail", "unknown", "info"
STATUS_RANK = {FAIL: 0, WARN: 1, UNKNOWN: 2, INFO: 3, PASS: 4}
SOURCES = ("api", "profile", "log", "nina", "ascom", "thesky", "file",
           "calibration", "ga")
APPLY_KINDS = ("api", "profile", "manual")
FORMS = ("equals", "min", "max", "min_arcsec", "max_arcsec", "computed", "report")
PE_OWNERS = ("protrack", "phd2_ppec", "none")
APPLY_STATES = ("Stopped", "Looping")     # PHD2 states an API apply accepts
ARMER_IDLE = ("DISARMED", "COMPLETE", "")  # armer states a profile write accepts
DEBOUNCE_S = 60.0
DEFAULT_FILE = (Path(__file__).resolve().parents[2] / "config" / "phd2"
                / "desired_oag_rc16.toml")
# the API setters apply() knows (row id -> what it calls)
API_SETTERS = ("exposure_ms", "ra_min_move", "dec_guide_mode")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _num(v):
    try:
        x = float(str(v).strip().rstrip("%")) if isinstance(v, str) else float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _norm(v) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v).lower())


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    t = _norm(v)
    if t in ("1", "on", "true", "yes", "enabled", "enable"):
        return True
    if t in ("0", "off", "false", "no", "disabled", "disable", "none"):
        return False
    return None


def _g(v) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return f"{v:g}"
    return "-" if v is None else str(v)


def audit_dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "phd2_audit"


def pe_owner(config) -> str:
    v = str(getattr(config, "pe_owner", "protrack") or "protrack").strip().lower()
    return v if v in PE_OWNERS else "protrack"


def darks_dir(config) -> Path:
    d = str(getattr(config, "phd2_darks_dir", "") or "").strip()
    if d:
        return Path(d)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "phd2" / "darks_defects"


# --------------------------------------------------------------------------
# the desired-state file
# --------------------------------------------------------------------------

def desired_path(config) -> Path:
    p = str(getattr(config, "phd2_desired_file", "") or "").strip()
    return Path(p) if p else DEFAULT_FILE


def lint_desired(desired: dict) -> list[str]:
    """Problems with a desired-state file (empty = fine)."""
    out, seen = [], set()
    rows = desired.get("check") or []
    if not rows:
        out.append("no [[check]] rows")
    for i, r in enumerate(rows):
        rid = r.get("id") or f"#{i}"
        if not r.get("id"):
            out.append(f"row {i}: no id")
        if rid in seen:
            out.append(f"{rid}: duplicate id")
        seen.add(rid)
        for k in ("group", "label", "why", "fix"):
            if not str(r.get(k) or "").strip():
                out.append(f"{rid}: no {k}")
        srcs = r.get("sources") or []
        if not srcs or any(s not in SOURCES for s in srcs):
            out.append(f"{rid}: bad sources {srcs}")
        if r.get("severity") not in (FAIL, WARN):
            out.append(f"{rid}: severity must be fail or warn")
        if r.get("apply") not in APPLY_KINDS:
            out.append(f"{rid}: apply must be one of {APPLY_KINDS}")
        if not any(k in r for k in FORMS):
            out.append(f"{rid}: no desired value")
        if r.get("computed") and r["computed"] not in COMPUTED:
            out.append(f"{rid}: unknown computed {r['computed']}")
        if r.get("apply") == "api" and rid not in API_SETTERS:
            out.append(f"{rid}: apply=api but no API setter")
        if r.get("apply") == "api" and any(s in ("ascom", "thesky", "nina")
                                           for s in srcs):
            out.append(f"{rid}: mount driver / TheSky / NINA rows are report only")
        for k, v in r.items():
            if isinstance(v, str) and not v.isascii():
                out.append(f"{rid}: {k} is not plain ASCII")
    return out


def load_desired(config) -> dict:
    """The desired-state file, parsed. {"error": ...} when unreadable."""
    import tomllib
    p = desired_path(config)
    try:
        d = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"error": f"{p}: {e}", "check": [], "path": str(p)}
    d["path"] = str(p)
    problems = lint_desired(d)
    if problems:
        logger.warning("PHD2 desired-state file %s: %s", p, "; ".join(problems))
        d["lint"] = problems
    return d


# --------------------------------------------------------------------------
# evaluation (pure)
# --------------------------------------------------------------------------

def _pick(row: dict, observed: dict):
    key = row.get("key") or row["id"]
    for src in row.get("sources") or []:
        v = (observed.get(src) or {}).get(key)
        if v is not None:
            return v, src
    return None, None


def _within(v, want, tol_pct) -> bool:
    return abs(v - want) <= abs(want) * float(tol_pct) / 100.0


def _c_pe_owner(row, val, observed, config, ctx):
    owner = pe_owner(config)
    if val is None:
        return UNKNOWN, None, None, None
    ppec = any(t in _norm(val) for t in ("predictive", "ppec", "gaussian"))
    if owner == "protrack":
        want = "not Predictive PEC (ProTrack owns periodic error)"
        return (FAIL if ppec else PASS), want, None, None
    if owner == "phd2_ppec":
        return (PASS if ppec else row["severity"]), "Predictive PEC (pe_owner=phd2_ppec)", None, None
    return INFO, "any (pe_owner=none)", None, None


def _c_ra_min_move_ga(row, val, observed, config, ctx):
    rec = _num((observed.get("ga") or {}).get("ra_min_move_rec"))
    if rec is None:
        return UNKNOWN, "the Guiding Assistant's RA min-move", None, \
            "no Guiding Assistant recommendation in the recent guide logs"
    tol = float(row.get("tol_pct", 30))
    want = f"{rec:g} px +/- {tol:g}% (Guiding Assistant {(observed.get('ga') or {}).get('time_local') or ''})".strip()
    v = _num(val)
    if v is None:
        return UNKNOWN, want, rec, None
    return (PASS if _within(v, rec, tol) else row["severity"]), want, rec, None


def _c_calibration_step(row, val, observed, config, ctx):
    rec = _num((observed.get("calibration") or {}).get("recommended_step_ms"))
    if rec is None:
        return UNKNOWN, "the PS-93 recommended step", None, \
            "no graded calibration with a recommended step yet"
    tol = float(row.get("tol_pct", 30))
    want = f"about {rec:g} ms +/- {tol:g}% (PS-93, about 12 steps)"
    v = _num(val)
    if v is None:
        return UNKNOWN, want, int(rec), None
    return (PASS if _within(v, rec, tol) else row["severity"]), want, int(rec), None


def _c_calibration_record(row, val, observed, config, ctx):
    cal = observed.get("calibration") or {}
    want = "graded PASS or WARN, under phd2_cal_max_age_days"
    if not cal:
        return UNKNOWN, want, None, "PS-93 calibration record unreadable"
    ctx["current"] = (f"{cal.get('grade') or 'none'}"
                      + (f", {cal.get('age_days')} d old" if cal.get("age_days") is not None else ""))
    if not cal.get("grade"):
        return FAIL, want, None, "no calibration on record (PS-93 schedules one)"
    if cal.get("poor"):
        return FAIL, want, None, "; ".join(cal.get("reasons") or []) or None
    if cal.get("stale"):
        return WARN, want, None, "older than phd2_cal_max_age_days"
    return PASS, want, None, None


def _c_flip(row, val, observed, config, ctx):
    flip = (observed.get("calibration") or {}).get("flip") or {}
    want = "Dec holds after a meridian flip (PS-93 after-flip check)"
    bad = [p for p, f in flip.items() if isinstance(f, dict) and f.get("ok") is False]
    good = [p for p, f in flip.items() if isinstance(f, dict) and f.get("ok")]
    if bad:
        ctx["current"] = f"Dec ran away after the flip ({', '.join(bad)})"
        return FAIL, want, None, (flip[bad[0]] or {}).get("detail")
    if good:
        ctx["current"] = f"verified ({', '.join(good)})"
        return PASS, want, None, None
    ctx["current"] = "not checked yet"
    return INFO, want, None, "PS-93 checks it on the first minutes after a flip"


def _c_focal_length(row, val, observed, config, ctx):
    from photonscript.telescope_agent.phd2_client import guide_focal_length_mm
    fl = guide_focal_length_mm(config)
    if not fl:
        return UNKNOWN, "the guide focal length from config", None, \
            "no guide_focal_length_mm / pixel_scale_arcsec in config"
    tol = float(row.get("tol_pct", 5))
    want = f"{fl:.0f} mm +/- {tol:g}%"
    v = _num(val)
    if v is None:
        return UNKNOWN, want, round(fl), None
    return (PASS if _within(v, fl, tol) else row["severity"]), want, round(fl), None


def _c_guide_speed(row, val, observed, config, ctx):
    from photonscript.shared import guide_motion as gm
    sp = (observed.get("nina") or {}).get("guide_speed") or {}
    tol = float(row.get("tol_pct", 10))
    mount = _num(sp.get("ra")) if sp.get("source") == "nina" else None
    src = "mount (NINA)"
    if sp.get("zero"):
        return FAIL, "a non-zero mount guide rate", None, \
            "the mount reports a guide rate of 0 (TheSky autoguide rate)"
    if mount is None:
        k = float(getattr(config, "guide_rate_sidereal", 0.5) or 0.5)
        mount = k * gm.SIDEREAL_ARCSEC_S
        src = f"config ({k:g} x sidereal; NINA mount not read)"
    want = f"{mount:.2f}\"/s from {src} +/- {tol:g}%"
    v = _num(val)
    if v is None:
        return UNKNOWN, want, None, None
    ok = _within(v, mount, tol)
    if ok:
        return PASS, want, None, None
    return (row["severity"] if src.startswith("mount") else WARN), want, None, None


def _c_dark_library(row, val, observed, config, ctx):
    f = observed.get("file") or {}
    max_age = float(getattr(config, "phd2_dark_max_age_days", 30) or 30)
    want = f"covers 1 to 4 s (and the current exposure), under {max_age:g} days old"
    lib = f.get("dark_library")
    if not f:
        return UNKNOWN, want, None, None
    if f.get("dir_missing"):
        return UNKNOWN, want, None, f"no PHD2 darks folder at {f.get('dir')}"
    if not lib:
        ctx["current"] = "missing"
        return row["severity"], want, None, f"no PHD2_dark_lib_*.fit in {f.get('dir')}"
    exps = sorted(lib.get("exposures_ms") or [])
    ctx["current"] = (f"{len(exps)} darks {exps[0] / 1000:g} to {exps[-1] / 1000:g} s, "
                      f"{lib.get('age_days')} d old" if exps else
                      f"exposures unreadable, {lib.get('age_days')} d old")
    cur = _num((observed.get("api") or {}).get("exposure_ms")
               or (observed.get("log") or {}).get("exposure_ms"))
    if exps:
        missing = []
        if exps[0] > 1000 or exps[-1] < 4000:
            missing.append("does not span 1 to 4 s")
        if cur and int(cur) not in [int(e) for e in exps]:
            missing.append(f"no dark at the current {cur / 1000:g} s exposure")
        if missing:
            return row["severity"], want, None, "; ".join(missing)
    if lib.get("age_days") is not None and lib["age_days"] > max_age:
        return WARN, want, None, "older than phd2_dark_max_age_days; rebuild at the current gain and binning"
    return (PASS if exps else UNKNOWN), want, None, None


def _c_guide_gain(row, val, observed, config, ctx):
    """PS-90: the gain the guide-star tuner recommends from recent nights
    (scheduler.phd2_tuning.recommend, collected as observed["tuning"])."""
    rec = observed.get("tuning") or {}
    if not rec.get("ok") or rec.get("gain") is None:
        return INFO, "the PS-90 tuner's recommendation", None,             rec.get("note") or "no guide-star tuning data yet"
    want = _num(rec["gain"])
    desired = f"{want:g} (PS-90 tuner: {rec.get('note')})"
    v = _num(val)
    if v is None:
        return UNKNOWN, desired, int(want), None
    return (PASS if abs(v - want) < 1e-6 else row["severity"]), desired, int(want), None


COMPUTED = {
    "pe_owner": _c_pe_owner,
    "ra_min_move_ga": _c_ra_min_move_ga,
    "calibration_step": _c_calibration_step,
    "calibration_record": _c_calibration_record,
    "flip": _c_flip,
    "focal_length": _c_focal_length,
    "guide_speed": _c_guide_speed,
    "dark_library": _c_dark_library,
    "guide_gain": _c_guide_gain,
}


def _scale(observed: dict, config) -> tuple[float | None, str | None]:
    """Guide arcsec/px for arcsec rows: the configured optics at the
    observed binning (the physical scale, right even when the PHD2 profile's
    focal length is wrong), else what PHD2 or its log reports."""
    from photonscript.telescope_agent.phd2_client import guide_scale_from_config
    b = _num((observed.get("api") or {}).get("binning")
             or (observed.get("log") or {}).get("binning")) or 1
    v = guide_scale_from_config(config, int(b))
    if v:
        return v, "config"
    for src in ("api", "log"):
        v = _num((observed.get(src) or {}).get("pixel_scale"))
        if v:
            return v, src
    return None, None


def _equals(row, val) -> bool | None:
    want = row["equals"]
    if isinstance(want, bool):
        b = _bool(val)
        return None if b is None else b == want
    if isinstance(want, (int, float)):
        v = _num(val)
        return None if v is None else abs(v - want) <= 1e-6 * max(1.0, abs(want))
    if row.get("match") == "contains":
        return _norm(want) in _norm(val)
    return _norm(want) == _norm(val)


def _desired_text(row, scale) -> str:
    if "equals" in row:
        return _g(row["equals"])
    parts = []
    if row.get("allow_off"):
        parts.append("off")
    lo, hi = row.get("min"), row.get("max")
    if lo is not None and hi is not None:
        parts.append(f"{_g(lo)} to {_g(hi)}")
    elif lo is not None:
        parts.append(f">= {_g(lo)}")
    elif hi is not None:
        parts.append(f"<= {_g(hi)}")
    if row.get("min_arcsec") is not None or row.get("max_arcsec") is not None:
        a, b = row.get("min_arcsec"), row.get("max_arcsec")
        t = f"{_g(a)} to {_g(b)} arcsec"
        if scale:
            t += f" ({a / scale:.1f} to {b / scale:.1f} px at {scale:.3f}\"/px)"
        parts.append(t)
    return " or ".join(parts) if row.get("allow_off") else ", ".join(parts)


def evaluate_row(row: dict, observed: dict, config, scale) -> dict:
    val, src = _pick(row, observed)
    out = {k: row.get(k) for k in ("id", "group", "label", "severity", "apply",
                                   "why", "fix")}
    out.update({"current": _g(val), "source": src, "note": None,
                "target": row.get("target", row.get("equals"))})
    ctx: dict = {}
    if row.get("report"):
        status, desired = (INFO if val is not None else UNKNOWN), "reported only"
        out["target"] = None
    elif row.get("computed"):
        status, desired, target, note = COMPUTED[row["computed"]](
            row, val, observed, config, ctx)
        if target is not None:
            out["target"] = target
        out["note"] = note
    else:
        desired = _desired_text(row, scale)
        status = UNKNOWN
        if val is not None:
            if "equals" in row:
                ok = _equals(row, val)
                status = UNKNOWN if ok is None else (PASS if ok else row["severity"])
            elif row.get("allow_off") and _bool(val) is False:
                status = PASS
            else:
                v = _num(val)
                lo, hi = row.get("min"), row.get("max")
                if row.get("min_arcsec") is not None or row.get("max_arcsec") is not None:
                    if not scale:
                        status, out["note"] = UNKNOWN, "guide pixel scale unknown"
                        v = None
                    else:
                        lo = row["min_arcsec"] / scale if row.get("min_arcsec") is not None else None
                        hi = row["max_arcsec"] / scale if row.get("max_arcsec") is not None else None
                if v is not None:
                    ok = (lo is None or v >= lo - 1e-9) and (hi is None or v <= hi + 1e-9)
                    notes = []
                    k = row.get("min_x_dither")
                    if k:
                        dith = _num((observed.get("nina") or {}).get("dither_px")) or \
                            _num((observed.get("log") or {}).get("max_dither_px"))
                        if dith:
                            desired += f", >= {k:g} x dither ({k * dith:.0f} px)"
                            if v < k * dith:
                                ok = False
                                notes.append(f"under {k:g} x the {dith:.1f} px dither")
                    status = PASS if ok else row["severity"]
                    out["note"] = "; ".join(notes) or None
    if "current" in ctx:
        out["current"] = ctx["current"]
    if status == UNKNOWN and not out["note"]:
        out["note"] = _unknown_note(row, observed)
    out["status"] = status
    out["desired"] = desired
    out["applicable"] = bool(row.get("apply") in ("api", "profile")
                             and status in (WARN, FAIL) and out["target"] is not None)
    return out


def _unknown_note(row, observed) -> str:
    notes = []
    key = row.get("key") or row["id"]
    cands = observed.get("profile_candidates") or {}
    if "profile" in (row.get("sources") or []) and key in cands:
        c = cands[key]
        if c.get("location"):
            notes.append(f"registry name unverified (candidate {c['location']} = "
                         f"{_g(c.get('value'))})")
        else:
            notes.append("registry name not known yet (reg export on the scope PC)")
    if "ascom" in (row.get("sources") or []):
        notes.append("driver flag location not known yet: check it by hand")
    srcs = observed.get("_sources") or {}
    for s in row.get("sources") or []:
        if s in srcs and not srcs[s].get("ok") and srcs[s].get("note"):
            notes.append(f"{s}: {srcs[s]['note']}")
    return "; ".join(notes) or "no source had a value"


def evaluate(desired: dict, observed: dict, config) -> dict:
    """Every row judged, plus counts and the guide scale used."""
    scale, scale_src = _scale(observed, config)
    rows = [evaluate_row(r, observed, config, scale) for r in desired.get("check") or []]
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in (PASS, WARN, FAIL, UNKNOWN, INFO)}
    return {"rows": rows, "counts": counts,
            "scale_arcsec_px": round(scale, 4) if scale else None,
            "scale_source": scale_src}


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def _algo_family(params: dict) -> dict:
    return {_norm(k): v for k, v in (params or {}).items()}


def _mass(text):
    """'50.0%' -> 50.0, 'disabled' -> 'off'."""
    if text is None:
        return None
    if _bool(text) is False or "disab" in str(text).lower():
        return "off"
    return _num(text)


def log_observed(sections: list, config) -> dict:
    """Settings of the newest guiding session in parsed guide-log sections,
    plus the inferred bit depth, the largest dither, the calibration step
    and the newest Guiding Assistant. Pure: {} when there is no session."""
    from photonscript.scheduler import phd2_analysis as pa
    a = pa.analyze_sections(sections, config)
    sessions = a.get("sessions") or []
    if not sessions:
        return {}
    s = sessions[-1]
    st = s["settings"]
    ra_p, dec_p = _algo_family(st.get("ra_params")), _algo_family(st.get("dec_params"))
    o = {"file": s.get("file"), "session_start_local": s.get("start_local"),
         "profile": st.get("profile"), "camera": st.get("camera"),
         "exposure_ms": st.get("exposure_ms"), "binning": st.get("binning"),
         "pixel_scale": st.get("pixel_scale_arcsec") if st.get("scale_source") == "phd2" else None,
         "focal_length_mm": st.get("focal_length_mm"),
         "search_region_px": st.get("search_region_px"),
         "mass_change": _mass(st.get("mass_tolerance")),
         "multi_star": st.get("multi_star"), "gain": st.get("gain"),
         "have_dark": st.get("have_dark"), "defect_map": st.get("defect_map"),
         "ra_algorithm": st.get("ra_algorithm"), "dec_algorithm": st.get("dec_algorithm"),
         "ra_min_move": ra_p.get("minimummove"), "dec_min_move": dec_p.get("minimummove"),
         "dec_guide_mode": st.get("dec_mode"),
         "backlash_comp": _bool(st.get("backlash_comp")),
         "ra_guide_speed": st.get("ra_guide_speed"),
         "guide_speed": st.get("ra_guide_speed"),
         "max_dither_px": (a.get("totals") or {}).get("max_dither_px") or None}
    # pixel size and calibration step: the newest header that has them
    for sec in reversed(sections):
        h = sec.get("header") or {}
        if sec.get("kind") == "guiding" and h.get("pixel_size_um") is not None:
            o["pixel_size_um"] = h.get("pixel_size_um")
            break
    for sec in reversed(sections):
        h = sec.get("header") or {}
        if sec.get("kind") == "calibration" and h.get("calibration_step_ms") is not None:
            o["calibration_step_ms"] = h.get("calibration_step_ms")
            break
    # PS-88: >90% 'saturated' at a low SNR means an 8-bit ADU ceiling
    if any(f.get("id") == "saturated_star" and (f.get("evidence") or {}).get("adu_limited")
           for x in sessions for f in x.get("findings") or []):
        o["bit_depth"] = 8
        o["bit_depth_note"] = "inferred: every star read saturated at a low SNR"
    return {k: v for k, v in o.items() if v is not None}


def ga_observed(runs: list) -> dict:
    """The newest Guiding Assistant run: its RA / Dec min-move suggestions
    and backlash."""
    if not runs:
        return {}
    g = runs[-1]
    out = {"time_local": g.get("time_local"), "backlash_ms": g.get("backlash_ms")}
    for rec in g.get("recommendations") or []:
        m = re.search(r"(RA|Dec) min-move to\s*(-?\d+(?:\.\d+)?)", rec, re.IGNORECASE)
        if m:
            out[f"{m.group(1).lower()}_min_move_rec"] = float(m.group(2))
    return out


def _collect_log(config) -> tuple[dict, dict, dict]:
    from photonscript.scheduler import phd2_analysis as pa
    from photonscript.scheduler import phd2_logs as pl
    files = pl.find_logs(config, "guide")["files"]
    if not files:
        return {}, {}, {"ok": False, "note": "no PHD2 guide logs found"}
    log, ga = {}, {}
    for p in reversed(files[-3:]):
        try:
            secs = pl.parse_guide_log(Path(p).read_text(encoding="utf-8", errors="replace"),
                                      Path(p).name)
        except OSError:
            continue
        if not ga:
            ga = ga_observed(pa.ga_results(secs, config))
        if not log:
            log = log_observed(secs, config)
        if log and ga:
            break
    if not ga:
        try:
            ref = pa.latest_ga_before(config, datetime.now(), files=files)
            ga = ga_observed([ref] if ref else [])
        except Exception as e:  # noqa: BLE001
            logger.debug("audit: latest GA failed: %s", e)
    note = f"newest session in {log.get('file')}" if log else "no guiding session in the newest logs"
    return log, ga, {"ok": bool(log), "note": note}


async def _collect_api(config, client=None) -> tuple[dict, dict]:
    from photonscript.telescope_agent.phd2_client import PHD2Client
    own = client is None
    if own:
        client = PHD2Client(config.phd2_host, config.phd2_port, config=config)
        if not await client.connect():
            return {}, {"ok": False, "note": "PHD2 not reachable"}
    o: dict = {}
    errs: list[str] = []

    async def q(key, fn, *a):
        try:
            o[key] = await fn(*a)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{key}: {e}")
    try:
        o["app_state"] = await client.get_app_state()
        await q("_profile", client.get_profile)
        await q("_equipment", client.get_current_equipment)
        await q("exposure_ms", client.get_exposure)
        await q("exposure_durations", client.get_exposure_durations)
        await q("binning", client.get_camera_binning)
        await q("pixel_scale", client.get_pixel_scale)
        await q("search_region_px", client.get_search_region)
        await q("dec_guide_mode", client.get_dec_guide_mode)
        await q("_calibration", client.get_calibration_data)
        await q("variable_delay", client.get_variable_delay_settings)
        for axis in ("ra", "dec"):
            try:
                names = await client.get_algo_param_names(axis)
                params = {}
                for n in names:
                    try:
                        params[n] = await client.get_algo_param(axis, n)
                    except Exception:  # noqa: BLE001
                        continue
                fam = _algo_family(params)
                o[f"{axis}_algorithm"] = fam.get("algorithmname")
                o[f"{axis}_min_move"] = fam.get("minmove")
                o[f"{axis}_aggressiveness"] = fam.get("aggressiveness")
                o[f"{axis}_params"] = params
            except Exception as e:  # noqa: BLE001
                errs.append(f"{axis} algorithm: {e}")
    finally:
        if own:
            await client.disconnect()
    prof = o.pop("_profile", None) or {}
    if isinstance(prof, dict):
        o["profile"], o["profile_id"] = prof.get("name"), prof.get("id")
    eq = o.pop("_equipment", None) or {}
    cam = (eq.get("camera") or {}) if isinstance(eq, dict) else {}
    o["camera"] = cam.get("name") if isinstance(cam, dict) else None
    cal = o.pop("_calibration", None)
    if isinstance(cal, dict):
        o["calibrated"] = bool(cal.get("calibrated"))
    px = float(getattr(config, "guide_camera_pixel_um", 0) or 0)
    if o.get("pixel_scale") and o.get("binning") and px > 0:
        # the profile's focal length, from the scale PHD2 computes with it
        o["focal_length_mm"] = round(206.265 * px * int(o["binning"]) / o["pixel_scale"], 0)
    o = {k: v for k, v in o.items() if v is not None}
    return o, {"ok": True, "note": "; ".join(errs) or f"PHD2 {o.get('app_state')}"}


def _find_guider_settings(obj):
    if isinstance(obj, dict):
        if "SettlePixels" in obj or "SettleTimeout" in obj:
            return obj
        for v in obj.values():
            hit = _find_guider_settings(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_guider_settings(v)
            if hit:
                return hit
    return None


async def _collect_nina(config, nina=None) -> tuple[dict, dict]:
    from photonscript.telescope_agent.pulse_selftest import guide_speeds
    own = nina is None
    if own:
        from photonscript.telescope_agent.nina_client import NinaClient
        nina = NinaClient(config.nina_base_url)
    o, notes = {}, []
    try:
        try:
            gs = _find_guider_settings(await nina.get_profile())
            if gs:
                o["settle_px"] = _num(gs.get("SettlePixels"))
                o["settle_time_s"] = _num(gs.get("SettleTime"))
                o["settle_timeout_s"] = _num(gs.get("SettleTimeout"))
                o["dither_px"] = _num(gs.get("DitherPixels"))
            else:
                notes.append("no guider settings in the NINA profile")
        except Exception as e:  # noqa: BLE001
            notes.append(f"profile: {e}")
        try:
            mount = await nina.get_mount_info()
        except Exception as e:  # noqa: BLE001
            mount = None
            notes.append(f"mount: {e}")
        o["guide_speed"] = guide_speeds(config, mount or {})
        if mount is None:
            o["guide_speed"]["source"] = "config" if o["guide_speed"]["source"] == "nina" \
                else o["guide_speed"]["source"]
    finally:
        if own:
            try:
                await nina.close()
            except Exception:  # noqa: BLE001
                pass
    o = {k: v for k, v in o.items() if v is not None}
    o["nina_settle_px"] = o.get("settle_px")
    o["nina_settle_timeout_s"] = o.get("settle_timeout_s")
    ok = "settle_px" in o
    return o, {"ok": ok, "note": "; ".join(notes) or "NINA guider settings read"}


def _collect_thesky(config) -> tuple[dict, dict]:
    host = getattr(config, "thesky_tcp_host", "localhost")
    port = int(getattr(config, "thesky_tcp_port", 3040) or 3040)
    try:
        with socket.create_connection((host, port), timeout=2):
            return {"thesky_scripting": True}, {"ok": True, "note": f"{host}:{port} answers"}
    except OSError as e:
        return {"thesky_scripting": False}, {"ok": True, "note": f"{host}:{port}: {e}"}


def _dark_exposures(path: Path) -> list[int]:
    """Exposure (ms) of every frame in a PHD2 dark library (EXPOSURE per
    HDU; read as ms, or seconds when under 50)."""
    from astropy.io import fits
    out = []
    with fits.open(path, memmap=False) as hd:
        for h in hd:
            v = h.header.get("EXPOSURE", h.header.get("EXPTIME"))
            v = _num(v)
            if v is None:
                continue
            out.append(int(round(v if v >= 50 else v * 1000)))
    return sorted(set(out))


def _collect_files(config, profile_id=None) -> tuple[dict, dict]:
    d = darks_dir(config)
    if not d.is_dir():
        return {"dir": str(d), "dir_missing": True}, {"ok": False, "note": f"{d} missing"}
    now = datetime.now().timestamp()

    def newest(pattern):
        files = sorted(d.glob(pattern), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None
    pid = str(profile_id) if profile_id not in (None, "") else "*"
    lib = newest(f"PHD2_dark_lib_{pid}.fit*") or newest("PHD2_dark_lib_*.fit*")
    dmap = newest(f"PHD2_defect_map_{pid}.fit*") or newest("PHD2_defect_map_*.fit*")
    o: dict = {"dir": str(d), "defect_map": dmap is not None}
    if lib is not None:
        info = {"path": str(lib),
                "age_days": round((now - lib.stat().st_mtime) / 86400.0, 1)}
        try:
            info["exposures_ms"] = _dark_exposures(lib)
        except Exception as e:  # noqa: BLE001
            info["error"] = str(e)
        o["dark_library"] = info
    return o, {"ok": True, "note": f"dark library {'found' if lib else 'missing'}, "
                                   f"defect map {'found' if dmap else 'missing'}"}


def dark_inventory(config, profile_id=None) -> list[int] | None:
    """PS-90: exposures (ms) the PHD2 dark library holds, None when there is
    no library or it cannot be read (the tuner then changes nothing)."""
    o, _src = _collect_files(config, profile_id)
    lib = o.get("dark_library") or {}
    exps = lib.get("exposures_ms")
    return list(exps) if exps else None


def _profile_observed(read: dict) -> tuple[dict, dict]:
    """Verified registry values as observations; every read value as a
    candidate (shown in the unknown note)."""
    vals, cands = {}, {}
    for k, v in (read.get("values") or {}).items():
        cands[k] = v
        if v.get("verified") and v.get("value") is not None:
            vals[k] = v["value"]
    if "mass_change_enabled" in vals:
        en = _bool(vals.pop("mass_change_enabled"))
        pct = _num(vals.pop("mass_change_pct", None))
        vals["mass_change"] = "off" if en is False else pct
    if "mass_change_enabled" in cands:
        cands["mass_change"] = cands["mass_change_enabled"]
    return vals, cands


async def collect(config, *, client=None, nina=None, raw: bool = False) -> dict:
    """Every source, never raises. observed[source][key]; observed["_sources"]
    says which answered."""
    obs: dict = {s: {} for s in SOURCES}
    src: dict = {}

    async def guarded(name, coro, timeout=30.0):
        try:
            return await asyncio.wait_for(coro, timeout)
        except Exception as e:  # noqa: BLE001
            src[name] = {"ok": False, "note": f"{type(e).__name__}: {e}"}
            return None
    r = await guarded("api", _collect_api(config, client))
    if r:
        obs["api"], src["api"] = r
    try:
        obs["log"], obs["ga"], src["log"] = await asyncio.to_thread(_collect_log, config)
        src["ga"] = {"ok": bool(obs["ga"]), "note": "Guiding Assistant "
                     + (obs["ga"].get("time_local") or "found") if obs["ga"] else
                     "no Guiding Assistant run in the recent logs"}
    except Exception as e:  # noqa: BLE001
        src["log"] = {"ok": False, "note": str(e)}
    try:
        name = obs["api"].get("profile") or obs["log"].get("profile")
        read = await asyncio.to_thread(ps.read, obs["api"].get("profile_id"), name)
        obs["profile"], obs["profile_candidates"] = _profile_observed(read)
        obs["profile_id"] = read.get("profile_id")
        if raw:
            obs["profile_raw"] = read.get("raw")
        src["profile"] = {"ok": bool(read.get("available")),
                          "note": read.get("note") or f"profile {read.get('profile_id')} read; "
                          f"{len(obs['profile'])} verified keys"}
    except Exception as e:  # noqa: BLE001
        src["profile"] = {"ok": False, "note": str(e)}
    try:
        from photonscript.scheduler import phd2_calibration as pc
        cal = pc.summary(config)
        rec = cal.get("record") or {}
        obs["calibration"] = {"grade": cal.get("grade"), "age_days": cal.get("age_days"),
                              "stale": cal.get("stale"), "poor": cal.get("poor"),
                              "recommended_step_ms": cal.get("recommended_step_ms"),
                              "flip": cal.get("flip") or {},
                              "reasons": rec.get("reasons") or []}
        src["calibration"] = {"ok": True, "note": f"PS-93 record {cal.get('grade') or 'none'}"}
    except Exception as e:  # noqa: BLE001
        src["calibration"] = {"ok": False, "note": str(e)}
    r = await guarded("nina", _collect_nina(config, nina))
    if r:
        obs["nina"], src["nina"] = r
    try:
        asc = ps.read_ascom()
        obs["ascom"] = {k: v["value"] for k, v in asc.items()
                        if v.get("verified") and v.get("value") is not None}
        src["ascom"] = {"ok": bool(obs["ascom"]),
                        "note": "driver flag names not known yet" if not obs["ascom"] else "read"}
    except Exception as e:  # noqa: BLE001
        src["ascom"] = {"ok": False, "note": str(e)}
    try:
        obs["thesky"], src["thesky"] = await asyncio.to_thread(_collect_thesky, config)
    except Exception as e:  # noqa: BLE001
        src["thesky"] = {"ok": False, "note": str(e)}
    try:
        pid = obs["api"].get("profile_id") or obs.get("profile_id")
        obs["file"], src["file"] = await asyncio.to_thread(_collect_files, config, pid)
    except Exception as e:  # noqa: BLE001
        src["file"] = {"ok": False, "note": str(e)}
    try:
        from photonscript.scheduler import phd2_tuning
        obs["tuning"] = await asyncio.to_thread(phd2_tuning.recommend, config)
    except Exception as e:  # noqa: BLE001
        obs["tuning"] = {"ok": False, "note": f"PS-90 tuning: {e}"}
    obs["_sources"] = src
    return obs


# --------------------------------------------------------------------------
# run, store, summarize
# --------------------------------------------------------------------------

def night_path(config, night: str) -> Path:
    return audit_dir(config) / f"{night}.json"


def changes_path(config) -> Path:
    return audit_dir(config) / "changes.jsonl"


def save(config, audit: dict) -> None:
    try:
        store.write_json(night_path(config, audit["night"]), audit)
        store.write_json(audit_dir(config) / "latest.json", audit)
    except OSError as e:
        logger.warning("PHD2 audit not saved: %s", e)


def load_latest(config) -> dict | None:
    return store.read_json(audit_dir(config) / "latest.json")


def load_night(config, night: str) -> dict | None:
    return store.read_json(night_path(config, night))


def fails(audit: dict | None) -> list[dict]:
    return [r for r in (audit or {}).get("rows") or [] if r.get("status") == FAIL]


async def run_audit(config, reason: str = "manual", *, client=None, nina=None,
                    raw: bool = False, persist: bool = True) -> dict:
    """Collect, evaluate and (by default) save one audit. Never raises."""
    now = datetime.utcnow()
    desired = load_desired(config)
    observed = await collect(config, client=client, nina=nina, raw=raw)
    res = evaluate(desired, observed, config)
    res["rows"].sort(key=lambda r: STATUS_RANK.get(r["status"], 9))
    audit = {"t_utc": store.iso_z(now), "night": store.night_of(config, now),
             "reason": reason, "title": desired.get("title"),
             "desired_file": desired.get("path"), "desired_error": desired.get("error"),
             "desired_lint": desired.get("lint"),
             "pe_owner": pe_owner(config),
             "autofix": bool(getattr(config, "phd2_audit_autofix", False)),
             "profile_id": observed["api"].get("profile_id") or observed.get("profile_id"),
             "phd2_state": observed["api"].get("app_state"),
             "sources": observed.get("_sources"), **res,
             "fail_ids": [r["id"] for r in res["rows"] if r["status"] == FAIL]}
    if raw:
        audit["observed"] = {k: v for k, v in observed.items() if k != "_sources"}
    if persist:
        save(config, audit)
    return audit


def summary(config, date: str) -> dict | None:
    """The audit block for a night (runs page, morning report, preflight)."""
    a = load_night(config, date)
    if not a:
        return None
    return {"t_utc": a.get("t_utc"), "reason": a.get("reason"),
            "counts": a.get("counts"),
            "fails": [f"{r['label']}: {r['current']} (want {r['desired']})"
                      for r in fails(a)][:5]}


def push_text(audit: dict, head: str, rows: list[dict] | None = None) -> str:
    rows = rows if rows is not None else fails(audit)
    top = "; ".join(f"{r['label']} {r['current']} (want {r['desired']})" for r in rows[:3])
    more = f" and {len(rows) - 3} more" if len(rows) > 3 else ""
    return (f"{head}: {len(rows)} FAIL. {top}{more}. "
            "System page > PHD2 settings audit for the fixes.")


async def at_arm(config, **kw) -> dict:
    """The armer's hook: audit, then one Pushover only when there is at
    least one FAIL (once per night; the FAIL ids are latched so the
    ConfigurationChange re-audit does not push them again)."""
    audit = await run_audit(config, reason="arm", **kw)
    bad = fails(audit)
    if bad:
        night = audit["night"]
        first = store.alert_once(config, f"audit-arm-{night}")
        for r in bad:
            store.alert_once(config, f"audit-{night}-{r['id']}")
        if first:
            from photonscript.shared import pushover
            await pushover.notify(config, push_text(audit, "PHD2 settings at arm"),
                                  title="PhotonScript PHD2 audit")
    return audit


async def on_config_change(config, *, push: bool = True, **kw) -> dict:
    """Re-audit after a PHD2 ConfigurationChange; push only for a FAIL not
    pushed before tonight."""
    audit = await run_audit(config, reason="PHD2 configuration change", **kw)
    new = [r for r in fails(audit)
           if store.alert_once(config, f"audit-{audit['night']}-{r['id']}")]
    if new and push:
        from photonscript.shared import pushover
        await pushover.notify(config, push_text(audit, "PHD2 settings changed", new),
                              title="PhotonScript PHD2 audit")
    audit["new_fails"] = [r["id"] for r in new]
    return audit


class ReAuditor:
    """RC16 agent listener: re-audit DEBOUNCE_S after the last PHD2
    ConfigurationChange (PHD2 sends a burst of them for one profile edit).
    Pushes only while a night is armed (armed_fn; PS-66: the agent passes
    "armed and guided", so an unguided night stays quiet); the System page shows the
    new audit either way. Never awaits PHD2 from the event (runs as a task)."""

    def __init__(self, config, armed_fn=None, debounce_s: float = DEBOUNCE_S):
        self.config = config
        self.armed_fn = armed_fn or (lambda: False)
        self.debounce_s = debounce_s
        self._task: asyncio.Task | None = None
        self.last: dict | None = None

    async def on_event(self, ev: dict) -> None:
        if ev.get("Event") != "ConfigurationChange":
            return
        if not getattr(self.config, "phd2_audit_enabled", True):
            return
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.get_running_loop().create_task(self._later())

    async def _later(self) -> None:
        await asyncio.sleep(self.debounce_s)
        try:
            self.last = await on_config_change(self.config, push=bool(self.armed_fn()))
        except Exception as e:  # noqa: BLE001
            logger.warning("PHD2 re-audit failed: %s", e)


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def _record_change(config, rec: dict) -> None:
    rec = dict(rec, t_utc=store.iso_z(datetime.utcnow()))
    store.append_jsonl(changes_path(config), rec)


def _nearest_duration(durations: list[int], target, lo, hi) -> int | None:
    ok = [d for d in durations if (lo is None or d >= lo) and (hi is None or d <= hi)]
    if not ok:
        return None
    t = _num(target) or ok[len(ok) // 2]
    return min(ok, key=lambda d: (abs(d - t), d))


async def _apply_api(config, rows: list[dict], desired_rows: dict, client, dry_run) -> list[dict]:
    from photonscript.telescope_agent import phd2_ops
    out = []
    try:
        async with phd2_ops.hold("audit"):
            from photonscript.telescope_agent.phd2_client import PHD2Client
            own = client is None
            if own:
                client = PHD2Client(config.phd2_host, config.phd2_port, config=config)
                if not await client.connect():
                    return [{"id": r["id"], "ok": False, "note": "PHD2 not reachable"}
                            for r in rows]
            try:
                state = await client.get_app_state()
                if state not in APPLY_STATES:
                    return [{"id": r["id"], "ok": False,
                             "note": f"PHD2 is {state}: API settings are applied only "
                                     "while it is Stopped or Looping"} for r in rows]
                for r in rows:
                    out.append(await _apply_one_api(config, r, desired_rows.get(r["id"]) or {},
                                                    client, dry_run))
            finally:
                if own:
                    await client.disconnect()
    except phd2_ops.PHD2Busy as e:
        return [{"id": r["id"], "ok": False, "note": str(e)} for r in rows]
    return out


async def _apply_one_api(config, r, row, client, dry_run) -> dict:
    rid, target = r["id"], r.get("target")
    try:
        if rid == "exposure_ms":
            durs = await client.get_exposure_durations()
            v = _nearest_duration(durs, target, row.get("min"), row.get("max"))
            if v is None:
                return {"id": rid, "ok": False,
                        "note": f"no listed PHD2 exposure in range ({durs})"}
            if not dry_run:
                await client.set_exposure(v)
                after = await client.get_exposure()
                ok = after == v
            else:
                ok, after = True, None
        elif rid == "ra_min_move":
            names = await client.get_algo_param_names("ra")
            name = next((n for n in names if _norm(n) == "minmove"), None)
            if name is None:
                return {"id": rid, "ok": False, "note": "RA algorithm has no MinMove"}
            v = round(float(target), 2)
            if not dry_run:
                await client.set_algo_param("ra", name, v)
                after = await client.get_algo_param("ra", name)
                ok = abs(float(after) - v) < 1e-3
            else:
                ok, after = True, None
        elif rid == "dec_guide_mode":
            v = str(target)
            if not dry_run:
                await client.set_dec_guide_mode(v)
                after = await client.get_dec_guide_mode()
                ok = _norm(after) == _norm(v)
            else:
                ok, after = True, None
        else:
            return {"id": rid, "ok": False, "note": "no API setter"}
    except Exception as e:  # noqa: BLE001
        return {"id": rid, "ok": False, "note": f"PHD2 refused: {e}"}
    res = {"id": rid, "ok": ok, "kind": "api", "from": r.get("current"), "to": v,
           "dry_run": dry_run, "read_back": after}
    if not dry_run:
        _record_change(config, res)
    return res


def _profile_changes(r: dict) -> dict:
    if r["id"] == "mass_change":
        t = r.get("target")
        if _bool(t) is False:
            return {"mass_change_enabled": False}
        return {"mass_change_enabled": True, "mass_change_pct": t}
    return {r["id"]: r.get("target")}


def _apply_profile(config, rows: list[dict], audit: dict, armer_state: str,
                   dry_run: bool) -> list[dict]:
    def refuse(note):
        return [{"id": r["id"], "ok": False, "note": note} for r in rows]
    if not getattr(config, "phd2_audit_autofix", False):
        return refuse("profile writes are off (phd2_audit_autofix=false)")
    if str(armer_state or "") not in ARMER_IDLE:
        return refuse(f"armer is {armer_state}: profile writes only while disarmed / complete")
    if ps.phd2_running():
        return refuse("PHD2 is running: close it first (it rewrites its profile on exit)")
    pid = audit.get("profile_id")
    if pid in (None, ""):
        return refuse("PHD2 profile id unknown")
    changes = {}
    for r in rows:
        changes.update(_profile_changes(r))
    unverified = [k for k in changes if not (k in ps.KEYS and ps.KEYS[k][2] and ps.KEYS[k][1])]
    if unverified:
        return refuse("registry names unverified: " + ", ".join(sorted(unverified)))
    if dry_run:
        return [{"id": r["id"], "ok": True, "kind": "profile", "dry_run": True,
                 "to": _profile_changes(r)} for r in rows]
    b = ps.backup(config, pid)
    if not b.get("ok"):
        return refuse(f"no backup, not written ({b.get('note')})")
    res = ps.write(config, pid, changes, backup_path=b["reg"])
    out = []
    for r in rows:
        keys = list(_profile_changes(r))
        ok = all(k in res.get("written", {}) for k in keys)
        rec = {"id": r["id"], "ok": ok, "kind": "profile", "from": r.get("current"),
               "to": {k: res.get("written", {}).get(k) for k in keys},
               "backup": b["reg"],
               "note": "; ".join(f"{k}: {v}" for k, v in (res.get("refused") or {}).items()
                                 if k in keys) or None}
        _record_change(config, rec)
        out.append(rec)
    return out


async def apply(config, ids: list[str], *, dry_run: bool = True, client=None,
                armer_state: str | None = None, audit: dict | None = None) -> dict:
    """Apply the listed rows of the latest audit (Option B). API rows live,
    profile rows only behind phd2_audit_autofix; manual rows are refused."""
    audit = audit or load_latest(config)
    if not audit:
        audit = await run_audit(config, reason="apply")
    desired_rows = {r.get("id"): r for r in load_desired(config).get("check") or []}
    by_id = {r["id"]: r for r in audit.get("rows") or []}
    results, api_rows, prof_rows = [], [], []
    for rid in ids or []:
        r = by_id.get(rid)
        if r is None:
            results.append({"id": rid, "ok": False, "note": "no such audit row"})
        elif r.get("status") == PASS:
            results.append({"id": rid, "ok": True, "note": "already as desired"})
        elif r.get("apply") not in ("api", "profile"):
            results.append({"id": rid, "ok": False,
                            "note": "report only: change it by hand (see fix)"})
        elif r.get("target") is None:
            results.append({"id": rid, "ok": False, "note": "no target value to apply"})
        elif r["apply"] == "api":
            api_rows.append(r)
        else:
            prof_rows.append(r)
    if api_rows:
        results += await _apply_api(config, api_rows, desired_rows, client, dry_run)
    if prof_rows:
        if armer_state is None:
            try:
                from photonscript.scheduler.app import get_armer
                armer_state = str(get_armer().state or "")
            except Exception:  # noqa: BLE001
                armer_state = "unknown"
        results += _apply_profile(config, prof_rows, audit, armer_state, dry_run)
    return {"ok": bool(results) and all(x.get("ok") for x in results),
            "dry_run": dry_run, "results": results,
            "note": "re-run the audit (refresh) to see the new values" if not dry_run else
                    "dry run: nothing changed"}
