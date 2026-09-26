"""Cross-night data-quality trend analysis — catches SYSTEMATIC rig faults that
per-frame QA misses.

The 2026-07-28 -> 09-04 "month of trailed subs" went uncaught because grading
is strictly per-frame: each elongated sub was individually rejected, but nobody
saw the sustained pattern. This aggregates recent nights' shape metrics and
flags a persistent systematic signature — polar/tracking drift, optical tilt, or
soft focus — so a rig fault surfaces in ONE night instead of a month.

Signatures (using the per-sub fields _shape_diagnostics already stores):
  * polar / tracking drift : elongated (ecc high) + coherent direction
    (ecc_pa_R high) + NOT radial (ecc_radial_frac low)
  * optical tilt/curvature : elongated + radial from field center
    (ecc_radial_frac high) — a spacing/collimation issue, not polar
  * focus drift            : median HFR persistently soft
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from statistics import median

logger = logging.getLogger(__name__)

# Thresholds tuned to the code's ecc / PA conventions (ecc 0=round..1=line;
# ecc_pa_R 0=random direction..1=one direction; ecc_radial_frac = fraction of
# elongation aligned with the radial vector).
_ECC_ELONG = 0.60      # median eccentricity above this = elongated
_PA_COHERENT = 0.60    # direction-coherence above this = all one way (drift)
_RADIAL_MAX = 0.45     # radial fraction below this = NOT optics
_RADIAL_MIN = 0.60     # radial fraction above this = optics (tilt/curvature)
_HFR_SOFT = 5.0        # median HFR (px) above this = soft focus
_MIN_SUBS = 25         # need this many light subs before calling anything systemic


def _recent_light_subs(config, nights: int) -> list[dict]:
    """RC16 light-sub records from the most recent `nights` nights that have
    lights (OSC has its own scale/QA, so it's analysed separately if ever)."""
    from photonscript.scheduler.runs import list_runs, _load_subs
    out: list[dict] = []
    dates = [r["date"] for r in list_runs(config)
             if r.get("lights") or r.get("subs_logged")]
    for d in dates[:nights]:
        for s in _load_subs(config, d):
            if s.get("rig", "rc16") != "rc16" or s.get("ecc") is None:
                continue
            s = dict(s)
            s["_night"] = d
            out.append(s)
    return out


def analyze_trends(config, nights: int = 14) -> dict:
    """Aggregate recent RC16 shape metrics and return systematic findings."""
    subs = _recent_light_subs(config, nights)
    n = len(subs)
    if n < _MIN_SUBS:
        return {"n_subs": n, "nights_analyzed": nights, "findings": []}

    def med(key):
        xs = [float(s[key]) for s in subs if s.get(key) is not None]
        return round(median(xs), 3) if xs else None

    m_ecc, m_pa = med("ecc"), med("ecc_pa_R")
    m_rad, m_hfr = med("ecc_radial_frac"), med("hfr")
    nights_span = len({s["_night"] for s in subs})
    findings: list[dict] = []

    if (m_ecc is not None and m_ecc >= _ECC_ELONG
            and m_pa is not None and m_pa >= _PA_COHERENT
            and (m_rad is None or m_rad < _RADIAL_MAX)):
        findings.append({
            "kind": "polar_drift", "severity": "high",
            "detail": (f"Stars elongated in ONE direction across {n} subs over "
                       f"{nights_span} nights (median ecc {m_ecc}, direction-"
                       f"coherence {m_pa}, radial {m_rad}). Signature of polar/"
                       "tracking drift — not seeing. Check polar alignment, "
                       "guiding, and TPoint/ProTrack.")})
    elif (m_ecc is not None and m_ecc >= _ECC_ELONG
          and m_rad is not None and m_rad >= _RADIAL_MIN):
        findings.append({
            "kind": "optical_tilt", "severity": "medium",
            "detail": (f"Stars elongated RADIALLY from field center across {n} "
                       f"subs (median ecc {m_ecc}, radial {m_rad}). Signature of "
                       "tilt/curvature — a spacing/collimation issue, not polar.")})

    if m_hfr is not None and m_hfr >= _HFR_SOFT:
        findings.append({
            "kind": "focus_drift", "severity": "high",
            "detail": (f"Median HFR {m_hfr}px across {n} subs is soft "
                       f"(>= {_HFR_SOFT}px) — a persistent focus problem, not a "
                       "one-off. Check the focus-seed table / autofocus.")})

    return {"n_subs": n, "nights_analyzed": nights, "nights_span": nights_span,
            "medians": {"ecc": m_ecc, "ecc_pa_R": m_pa,
                        "ecc_radial_frac": m_rad, "hfr": m_hfr},
            "findings": findings}


def _alert_state_path(config) -> Path:
    return Path(config.data_dir) / "trend_alerts.json"


def check_and_alert(config, nights: int = 14) -> dict:
    """Run the analysis and fire ONE Pushover per newly-appeared systematic
    finding (deduped via data_dir/trend_alerts.json so it doesn't re-alert every
    night, and re-fires if the issue clears then returns). Safe from the sync
    backfill thread — wraps the async notify itself. Never raises."""
    res = analyze_trends(config, nights)
    kinds = sorted({f["kind"] for f in res.get("findings", [])})
    p = _alert_state_path(config)
    try:
        prev = set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()
    except Exception:  # noqa: BLE001
        prev = set()
    new = [f for f in res.get("findings", []) if f["kind"] not in prev]
    if new:
        try:
            import asyncio
            from photonscript.shared.pushover import notify

            async def _send():
                for f in new:
                    await notify(config, "TREND: " + f["detail"],
                                 title="PhotonScript trend alert",
                                 priority=1 if f["severity"] == "high" else 0)
            asyncio.run(_send())
        except Exception as e:  # noqa: BLE001
            logger.warning("trend alert notify failed: %s", e)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(kinds), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return res
