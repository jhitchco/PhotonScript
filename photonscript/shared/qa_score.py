"""PS-108: one 0 to 100 score per sub, built from the PS-21 scorecard.

    weights(config) -> dict                    config/qa/score_weights.toml
    score(checks, metrics, t) -> Score         pure: no FITS, no I/O beyond
                                               the (cached) weights file
    panel_rows(rec, t) -> list[dict]           the runs-page side panel

Each judged check (pass / warn / fail; skip is not judged) gets a grade q
from 0 to 1 by how far inside or outside its gate the value sits, weighted
per rig; failed gates cap the score (roof closed, solve-confirmed off target,
slew straddle, ...). The decision uses qa_score_approve (80) and
qa_score_reject (60). qa_score_mode "preview" (default) only records and
shows it; "on" lets it set the verdict (qa_rules.Scorecard). A human verdict
always wins (runs.rescore_night never touches one).

The file format and every weight's "why" are documented in the TOML itself.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_FILE = (Path(__file__).resolve().parents[2] / "config" / "qa"
                / "score_weights.toml")
APPROVE, REVIEW, REJECT = "approve", "review", "reject"
MODES = ("preview", "on")

# Used when the weights file is missing or unreadable, so grading never
# stops over a config file. Same numbers as the shipped TOML.
_FALLBACK = {
    "version": "score1-fallback",
    "curve": {"good": 0.80, "zero": 1.25, "star_good_x": 4.0,
              "star_full_max": 0.80},
    "caps": {"roof": {"cap": 0}, "pointing_solved": {"cap": 20},
             "slew_straddle": {"cap": 20}, "guide_lock": {"cap": 40},
             "temp": {"cap": 40}, "tracking_jump": {"cap": 40},
             "stars": {"cap": 40}, "far_out_of_gate": {"cap": 59},
             "out_of_gate": {"cap": 79}},
    "rc16": {k: {"weight": w} for k, w in (
        ("ecc", 25), ("ecc_bin", 25), ("hfr", 20), ("hfr_rel", 10),
        ("fwhm", 10), ("stars", 10), ("bg_rel", 5), ("bg_floor", 3),
        ("temp", 5), ("guide_lock", 5), ("guide_rms", 10),
        ("tracking_jump", 10), ("exposure", 8), ("roof", 10),
        ("slew_straddle", 0), ("pointing", 10))},
    "piggyback": {k: {"weight": w} for k, w in (
        ("ecc", 20), ("ecc_bin", 0), ("hfr", 25), ("hfr_rel", 10),
        ("fwhm", 5), ("stars", 10), ("bg_rel", 5), ("bg_floor", 3),
        ("temp", 5), ("guide_lock", 0), ("guide_rms", 0),
        ("tracking_jump", 10), ("exposure", 10), ("roof", 10),
        ("slew_straddle", 10), ("pointing", 5))},
}

# PS-21 exposure gates the "exposure" check warns on (image_validator /
# runs._measure): saturated star cores above 5%, clipped pixels above 0.05%
SAT_STARS_LIMIT_PCT = 5.0
SAT_PX_LIMIT_PCT = 0.05

_cache: dict = {}
_cache_lock = threading.Lock()


def weights_path(config) -> Path:
    p = str(getattr(config, "qa_score_weights_file", "") or "").strip()
    return Path(p) if p else DEFAULT_FILE


def weights(config) -> dict:
    """The parsed weights file (cached on path + mtime). Falls back to the
    built-in copy, with `error` set, when the file cannot be read."""
    import tomllib
    p = weights_path(config)
    try:
        mtime = p.stat().st_mtime
    except OSError as e:
        return {**_FALLBACK, "error": f"{p}: {e}", "path": str(p)}
    key = (str(p), mtime)
    with _cache_lock:
        hit = _cache.get("w")
        if hit and hit[0] == key:
            return hit[1]
    try:
        d = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("score weights %s unreadable (%s): built-in weights", p, e)
        return {**_FALLBACK, "error": f"{p}: {e}", "path": str(p)}
    d["path"] = str(p)
    with _cache_lock:
        _cache["w"] = (key, d)
    return d


def rig_weights(w: dict, rig: str) -> dict[str, float]:
    tbl = w.get(rig or "rc16") or w.get("rc16") or {}
    out = {}
    for cid, spec in tbl.items():
        if isinstance(spec, dict):
            try:
                out[cid] = float(spec.get("weight", 0) or 0)
            except (TypeError, ValueError):
                out[cid] = 0.0
    return out


def _cap(w: dict, name: str, default: float) -> float:
    try:
        return float(((w.get("caps") or {}).get(name) or {}).get("cap", default))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ grading

def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def ramp(r: float, good: float, zero: float) -> float:
    """1 up to `good`, 0 from `zero`, a straight line between."""
    if r <= good:
        return 1.0
    if r >= zero or zero <= good:
        return 0.0
    return (zero - r) / (zero - good)


def _ratio(value, limit):
    v, lim = _num(value), _num(limit)
    if v is None or lim is None or lim <= 0:
        return None
    return v / lim


def grade(cid: str, value, limit, status: str, metrics: dict, t: dict,
          curve: dict) -> float | None:
    """q in [0, 1] for one judged check, or None when it cannot be graded
    (then its status alone decides: pass / warn 1, fail 0)."""
    good = float(curve.get("good", 0.80))
    zero = float(curve.get("zero", 1.25))
    m = metrics or {}
    if cid in ("roof", "guide_lock", "slew_straddle", "bg_floor"):
        return 1.0 if status == "pass" else 0.0
    if cid == "stars":
        n = _num(value)
        if n is None:
            return None
        lo, hi = float(t.get("star_min", 5)), float(t.get("star_max", 5000))
        if n < lo or n > hi:
            return 0.0
        q_lo = min(1.0, (n - lo) / max(lo * float(curve.get("star_good_x", 4.0))
                                         - lo, 1.0))
        full_max = hi * float(curve.get("star_full_max", 0.80))
        q_hi = 1.0 if n <= full_max else max(0.0, (hi - n) / max(hi - full_max, 1.0))
        return max(0.0, min(q_lo, q_hi))
    if cid == "temp":
        v = _num(value)
        if v is None:
            return None
        d = v - float(t.get("setpoint_c", 0.0))
        tol = float(t.get("cooling_tol_c", 1.0))
        over = float(t.get("temp_over_c", 5.0))
        if d <= tol:
            return 1.0
        return max(0.0, (over - d) / max(over - tol, 0.1))
    if cid == "exposure":
        qs = []
        r = _ratio(m.get("sat_stars_pct"), SAT_STARS_LIMIT_PCT)
        if r is not None:
            qs.append(ramp(r, 0.5, 2.0))
        px = m.get("sat_px_pct")
        if _num(px) is None:
            px = m.get("clipped_pct")
        r = _ratio(px, SAT_PX_LIMIT_PCT)
        if r is not None:
            qs.append(ramp(r, 0.5, 2.0))
        if qs:
            return min(qs)
        return 1.0 if status == "pass" else 0.5 if status == "warn" else 0.0
    if cid == "pointing":
        # PS-107: graded against the source-aware limits, so a header-only
        # offset is measured against the gross-miss reject (5 deg), not the
        # rig bands only a plate solve can hold it to
        from photonscript.shared.qa_rules import pointing_limits
        flag, rej = pointing_limits(t, m.get("pointing_src"))
        r = _ratio(value, rej or 1.0)
        if r is None:
            return None
        return ramp(r, min(flag / (rej or 1.0), 0.99), 1.5)
    # max gates: ecc, ecc_bin, hfr, hfr_rel, fwhm, guide_rms, tracking_jump,
    # bg_rel
    r = _ratio(value, limit)
    if r is None:
        return None
    return ramp(r, good, zero)


# -------------------------------------------------------------------- model

@dataclass
class Score:
    value: int
    raw: float
    decision: str
    deductions: list = field(default_factory=list)  # [id, points, note]
    cap: list | None = None                        # [name, cap, check id]
    mode: str = "preview"
    approve_at: float = 80.0
    reject_below: float = 60.0
    version: str = ""
    weights_error: str = ""

    def compact(self) -> dict:
        """What the record stores inside scorecard (about 120 bytes)."""
        out = {"s": self.value, "d": self.decision, "v": self.version,
               "top": [d[:2] for d in self.deductions[:3]]}
        if self.cap:
            out["cap"] = self.cap
        return out

    def top_text(self, n: int = 3) -> str:
        parts = [f"{d[0]} -{d[1]:g}" + (f" ({d[2]})" if len(d) > 2 and d[2] else "")
                 for d in self.deductions[:n]]
        if self.cap:
            parts.insert(0, f"capped at {self.cap[1]:g} by {self.cap[2]}")
        return "; ".join(parts)

    def as_dict(self) -> dict:
        return {"score": self.value, "raw": round(self.raw, 1),
                "decision": self.decision, "mode": self.mode,
                "approve_at": self.approve_at, "reject_below": self.reject_below,
                "deductions": [{"id": d[0], "points": d[1],
                                "note": d[2] if len(d) > 2 else ""}
                               for d in self.deductions],
                "cap": ({"name": self.cap[0], "cap": self.cap[1],
                         "check": self.cap[2]} if self.cap else None),
                "version": self.version, "text": self.top_text(),
                "weights_error": self.weights_error or None}


def decide(value: float, approve_at: float, reject_below: float) -> str:
    if value >= approve_at:
        return APPROVE
    if value < reject_below:
        return REJECT
    return REVIEW


def _note(cid, value, limit, metrics) -> str:
    if cid == "exposure":
        bits = []
        if _num(metrics.get("sat_stars_pct")) is not None:
            bits.append(f"sat stars {metrics['sat_stars_pct']:g}% vs "
                        f"{SAT_STARS_LIMIT_PCT:g}%")
        px = metrics.get("sat_px_pct")
        if _num(px) is None:
            px = metrics.get("clipped_pct")
        if _num(px) is not None:
            bits.append(f"sat px {float(px):g}% vs {SAT_PX_LIMIT_PCT:g}%")
        return ", ".join(bits)
    if isinstance(limit, list) and len(limit) == 2:
        return f"{value} vs {limit[0]}..{limit[1]}"
    if value is None or limit is None:
        return ""
    return f"{value} vs {limit}"


def score(checks, metrics: dict, t: dict, w: dict | None = None) -> Score:
    """Score one graded sub. `checks` are qa_rules.Check objects (or rows
    [id, value, limit, status, ...]); `t` the qa_rules thresholds; `w` the
    weights (weights(config); default the built-in copy)."""
    w = w or _FALLBACK
    curve = w.get("curve") or _FALLBACK["curve"]
    rw = rig_weights(w, t.get("rig") or "rc16")
    mode = str(t.get("score_mode", "preview")).lower()
    approve_at = float(t.get("score_approve", 80))
    reject_below = float(t.get("score_reject", 60))
    rows = []
    for c in checks:
        if isinstance(c, (list, tuple)):
            rows.append((c[0], c[1], c[2], c[3]))
        else:
            rows.append((c.id, c.value, c.limit, c.status))
    total = gained = 0.0
    lost = []
    caps = []
    for cid, value, limit, status in rows:
        if status == "skip":
            continue
        wt = rw.get(cid, 0.0)
        q = grade(cid, value, limit, status, metrics, t, curve)
        if q is None:
            q = 1.0 if status in ("pass", "warn") else 0.0
        if wt > 0:
            total += wt
            gained += wt * q
            if q < 1.0:
                lost.append((cid, wt * (1.0 - q), _note(cid, value, limit,
                                                        metrics or {})))
        if status == "fail":
            name = cid
            if cid == "pointing":
                # PS-107: only a plate solve confirms an off target; a
                # header / mount-log gross miss falls under the general
                # failed-gate caps (review or reject by how far)
                from photonscript.shared.qa_rules import pointing_confirmed
                name = ("pointing_solved" if pointing_confirmed(
                    (metrics or {}).get("pointing_src")) else "")
            if name and name in (w.get("caps") or {}):
                caps.append((_cap(w, name, 59), name, cid))
            elif q <= 0.0:
                caps.append((_cap(w, "far_out_of_gate", 59), "far_out_of_gate", cid))
            else:
                caps.append((_cap(w, "out_of_gate", 79), "out_of_gate", cid))
    raw = 100.0 * gained / total if total > 0 else 100.0
    value = raw
    cap = None
    if caps:
        lowest = min(caps, key=lambda c: c[0])
        if lowest[0] < value:
            value = lowest[0]
        cap = [lowest[1], lowest[0], lowest[2]]
    value_i = int(math.floor(value + 0.5))
    deductions = sorted(
        ([cid, round(100.0 * pts / total, 1), note] for cid, pts, note in lost
         if total > 0 and pts > 0),
        key=lambda d: -d[1])
    return Score(value=value_i, raw=raw, decision=decide(value_i, approve_at,
                                                         reject_below),
                 deductions=deductions, cap=cap, mode=mode,
                 approve_at=approve_at, reject_below=reject_below,
                 version=str(w.get("version") or ""),
                 weights_error=str(w.get("error") or ""))


def from_compact(c: dict | None) -> dict | None:
    """Stored compact score -> API dict (without re-grading)."""
    if not c or "s" not in c:
        return None
    return {"score": c.get("s"), "decision": c.get("d"), "version": c.get("v"),
            "deductions": [{"id": d[0], "points": d[1]} for d in c.get("top", [])],
            "cap": ({"name": c["cap"][0], "cap": c["cap"][1],
                     "check": c["cap"][2]} if c.get("cap") else None)}


# ------------------------------------------------------------- side panel

_RAG = {"pass": "green", "warn": "amber", "fail": "red", "skip": "none"}


def _fmt(v, nd=2):
    x = _num(v)
    if x is None:
        return "n/a" if v in (None, "") else str(v)
    if abs(x) >= 100 or x == int(x):
        return f"{x:.0f}"
    return f"{x:.{nd}f}".rstrip("0").rstrip(".")


def _frac(value, limit):
    r = _ratio(value, limit)
    return None if r is None else round(max(0.0, min(r, 1.5)) / 1.5, 3)


# PS-114: scorecard check -> the thresholds key(s) that are its gate (checks
# whose limit depends on the night or the offset source keep the stored one)
_GATE_OF = {"ecc": "ecc_max", "ecc_bin": "ecc_max_bin", "hfr": "hfr_max",
            "fwhm": "fwhm_max", "stars": ("star_min", "star_max"),
            "guide_rms": "guide_rms_max", "tracking_jump": "doubled_max",
            "bg_floor": "bias_floor"}


def _gate_now(cid: str, t: dict):
    key = _GATE_OF.get(cid)
    if key is None or not t:
        return None
    if isinstance(key, tuple):
        lo, hi = t.get(key[0]), t.get(key[1])
        return None if lo is None or hi is None else [lo, hi]
    v = t.get(key)
    if cid == "bg_floor" and not v:
        return None
    return v


def panel_rows(rec: dict, card_rows: list[dict], t: dict,
               sc: dict | None = None) -> list[dict]:
    """Every metric for the lightbox side panel: label, value, gate, a
    red / amber / green status and a bar fraction (value / limit, the bar
    full at 1.5x the limit), plus the points the score lost on it. The page
    renders these as given (it never compares a number with a limit).

    `card_rows` are qa_rules.expand() rows, `sc` the Score.as_dict()."""
    lost = {d["id"]: d["points"] for d in ((sc or {}).get("deductions") or [])}
    out = []
    by_id = {r["id"]: r for r in card_rows}

    def add(id_, label, value, gate, status, frac=None, note="", unit=""):
        out.append({"id": id_, "label": label,
                    "value": (_fmt(value) + (unit if _num(value) is not None
                                             else "")),
                    "gate": gate, "status": status, "frac": frac,
                    "lost": lost.get(id_), "note": note})

    def check_row(cid, label=None, unit=""):
        r = by_id.get(cid)
        if not r:
            return
        lim = r.get("limit")
        # PS-114: show the rig's gate in force now; when the stored card was
        # graded against another value, say so (a rescore updates the score)
        now = _gate_now(cid, t)
        note = r.get("reason") or ""
        if now is not None and r.get("status") != "skip" and lim != now:
            was = (f"{_fmt(lim[0])} to {_fmt(lim[1])}"
                   if isinstance(lim, list) and len(lim) == 2 else
                   _fmt(lim) + unit)
            stale = f"graded against {was}; qa-rescore updates the score"
            note = f"{note} ({stale})" if note else stale
        if now is not None:
            lim = now
        if isinstance(lim, list) and len(lim) == 2:
            gate = f"{_fmt(lim[0])} to {_fmt(lim[1])}"
            frac = _frac(r.get("value"), lim[1])
        elif cid == "bg_floor":
            gate = f"> {_fmt(lim)}" if lim is not None else ""
            frac = None
        elif cid in ("roof", "guide_lock"):
            gate, frac = str(lim or ""), None
        else:
            gate = f"<= {_fmt(lim)}{unit}" if _num(lim) is not None else ""
            frac = _frac(r.get("value"), lim)
        add(cid, label or r.get("name") or cid, r.get("value"), gate,
            _RAG.get(r.get("status"), "none"), frac,
            note, unit if cid not in ("roof",) else "")

    check_row("stars", "Stars")
    check_row("hfr", "HFR", " px")
    check_row("hfr_rel", "HFR vs night median", " px")
    check_row("fwhm", "FWHM", "\"")
    check_row("ecc", "Ecc (native)")
    check_row("ecc_bin", "Ecc (2x2 binned)")
    cs = _num(rec.get("corner_spread"))
    cs_lim = _num(t.get("corner_spread_max"))
    if cs is not None:
        add("corner_spread", "Corner FWHM spread", cs,
            f"<= {_fmt(cs_lim)}" if cs_lim else "",
            ("amber" if cs_lim and cs > cs_lim else "green"),
            _frac(cs, cs_lim), "info only (collimation / tilt watch)")
    else:
        add("corner_spread", "Corner FWHM spread", None, "", "none",
            note="not measured")
    # background, its spread and the new pixel counts
    med, mad = rec.get("bg_median"), rec.get("bg_mad")
    add("background", "Background median", med if med is not None
        else rec.get("background"), "", "none",
        note=("MAD " + _fmt(mad) + " ADU") if mad is not None else "")
    check_row("bg_rel", "Background vs night median", " ADU")
    check_row("bg_floor", "Background vs bias floor", " ADU")
    sat_lvl = rec.get("sat_adu") or t.get("saturation_adu")
    sp = _num(rec.get("sat_px_pct"))
    if sp is not None:
        add("sat_px", "Saturated pixels", sp,
            f"<= {SAT_PX_LIMIT_PCT:g}% (>= {_fmt(sat_lvl)} ADU)",
            "amber" if sp > SAT_PX_LIMIT_PCT else "green",
            _frac(sp, SAT_PX_LIMIT_PCT),
            f"{int(rec.get('sat_px') or 0):,} px", "%")
    else:
        add("sat_px", "Saturated pixels", None, "", "none",
            note="graded before PS-108 (see the histogram)")
    zp = _num(rec.get("zero_px_pct"))
    if zp is not None:
        add("zero_px", "Pixels at 0 (black clip)", zp, "0%",
            "amber" if (rec.get("zero_px") or 0) > 0 else "green", None,
            f"{int(rec.get('zero_px') or 0):,} px", "%")
    else:
        add("zero_px", "Pixels at 0 (black clip)", None, "", "none",
            note="graded before PS-108")
    mx = _num(rec.get("max_adu"))
    add("max_adu", "Max ADU", mx,
        f"saturation {_fmt(sat_lvl)}" if sat_lvl else "",
        ("none" if mx is None else "amber" if sat_lvl and mx >= float(sat_lvl)
         else "green"), _frac(mx, sat_lvl) if mx is not None else None)
    ssp = _num(rec.get("sat_stars_pct"))
    add("sat_stars", "Saturated star cores", ssp, f"<= {SAT_STARS_LIMIT_PCT:g}%",
        "none" if ssp is None else "amber" if ssp > SAT_STARS_LIMIT_PCT
        else "green", _frac(ssp, SAT_STARS_LIMIT_PCT), unit="%")
    sw = _num(rec.get("swamp"))
    add("swamp", "Sky vs read noise", sw, ">= 3 x RN^2 (10 fully sky-limited)",
        "none" if sw is None else "amber" if sw < 3 else "green", None,
        unit=" x RN^2")
    check_row("exposure", "Exposure verdict")
    check_row("temp", "Sensor temp", " C")
    check_row("guide_rms", "Guide RMS", "\"")
    check_row("guide_lock", "Guide star is a star")
    check_row("tracking_jump", "Doubled stars (tracking jump)")
    check_row("pointing", "Pointing offset", "'")
    if out and out[-1]["id"] == "pointing" and _num(
            by_id["pointing"].get("value")) is not None:
        # PS-107: say where the offset came from; only a solve confirms it
        from photonscript.shared.qa_rules import _SRC_LABEL, pointing_confirmed
        src = rec.get("pointing_src")
        if pointing_confirmed(src):
            tag = "(plate solve)"
        else:
            tag = "(" + _SRC_LABEL.get(str(src or ""), "header") + ", unconfirmed)"
        note = out[-1]["note"]
        out[-1]["note"] = (note + " " + tag) if note else tag
    check_row("slew_straddle", "Clear of RC16 moves", " s")
    check_row("roof", "Roof open / not parked")
    return out
