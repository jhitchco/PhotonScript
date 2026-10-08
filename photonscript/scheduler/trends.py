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
  * optical tilt / collimation : PS-95, from the per-night optics report
    (scheduler.optics_report, cached in <data_dir>/optics/): the same tilt
    direction, or collimation, on most of the recent measured nights. Used
    to read ecc_radial_frac here, which mixed ecc formulas (PS-94).
  * focus drift            : median HFR persistently soft
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from statistics import median

logger = logging.getLogger(__name__)

# Thresholds tuned to the code's ecc / PA conventions (ecc 0=round..1=line,
# sqrt(1-(b/a)^2) form: PS-94 converts old 1-b/a backfill records on read;
# ecc_pa_R 0=random direction..1=one direction; ecc_radial_frac = fraction of
# elongation aligned with the radial vector).
_ECC_ELONG = 0.60      # median eccentricity above this = elongated
_PA_COHERENT = 0.60    # direction-coherence above this = all one way (drift)
_RADIAL_MAX = 0.45     # radial fraction below this = NOT optics
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


# PS-95 optics trend: a finding is "persistent" when at least _OPT_MIN_NIGHTS
# of the last _OPT_RECENT measured nights agree (share _OPT_SHARE).
_OPT_RECENT = 5
_OPT_MIN_NIGHTS = 3
_OPT_SHARE = 2 / 3


def _optics_point(rep: dict) -> dict | None:
    o = rep.get("overall") or {}
    if not o.get("n_measured"):
        return None
    t = o.get("tilt") or {}
    return {"date": rep.get("date"), "verdict": o.get("verdict"),
            "n_measured": o.get("n_measured"),
            "tilt_pct": None if not t.get("ratio_median")
            else round((t["ratio_median"] - 1) * 100, 1),
            "tilt_dir": t.get("direction"),
            "soft_corner": t.get("soft_corner"),
            "corner_ratio": o.get("corner_ratio"),
            "center_fwhm": o.get("center_fwhm"),
            "center_ecc": o.get("center_ecc"),
            "headline": rep.get("headline")}


def _optics_key(p: dict) -> str:
    return (f"tilt:{p['tilt_dir']}" if p["verdict"] == "tilt"
            else str(p["verdict"]))


def _same_optics(a: str, b: str) -> bool:
    """Night keys agree: same verdict, tilt within one 45 deg sector."""
    from photonscript.scheduler.optics_report import _sector_close
    if a.startswith("tilt:") and b.startswith("tilt:"):
        return _sector_close(a[5:], b[5:])
    return a == b


def _change_point(points: list[dict]) -> dict | None:
    """The split where the optics signature changed and stayed changed
    (both sides at least _OPT_SHARE consistent, the later side at least 2
    nights). A collimation or tilt fix shows up here."""
    keys = [_optics_key(p) for p in points]
    best = None
    for i in range(1, len(keys) - 1):
        before, after = keys[:i], keys[i:]
        mb = Counter(before).most_common(1)[0][0]
        ma = Counter(after).most_common(1)[0][0]
        if _same_optics(mb, ma):
            continue
        sb = sum(_same_optics(k, mb) for k in before) / len(before)
        sa = sum(_same_optics(k, ma) for k in after) / len(after)
        if sb >= _OPT_SHARE and sa >= _OPT_SHARE:
            score = sb * len(before) + sa * len(after)
            if best is None or score > best[0]:
                best = (score, i, mb, ma)
    if best is None:
        return None
    _, i, mb, ma = best
    return {"after": points[i - 1]["date"], "first_new": points[i]["date"],
            "from": mb, "to": ma,
            "text": f"Optics changed after {points[i - 1]['date']}: "
                    f"{mb} -> {ma} (from {points[i]['date']} on)"}


def optics_trend(config, nights: int = 30, compute_missing: bool = False) -> dict:
    """PS-95: per-night optics (tilt size and direction, verdict) from the
    cached reports in <data_dir>/optics/, oldest first, plus the night the
    signature changed and any persistent finding. compute_missing builds
    the report for logged nights without one (sidecar reads only)."""
    from photonscript.scheduler import optics_report as orp
    d = Path(config.data_dir) / "optics"
    if compute_missing:
        try:
            from photonscript.scheduler.runs import list_runs
            for r in list_runs(config)[:nights]:
                if not orp.cache_path(config, r["date"]).exists():
                    orp.night_optics(config, r["date"])
        except Exception as e:  # noqa: BLE001
            logger.warning("optics trend backfill failed: %s", e)
    reps: dict[str, dict] = {}
    files = sorted(d.glob("????-??-??.json")) if d.exists() else []
    for p in files[-nights:]:
        try:
            reps[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    points = [pt for pt in (_optics_point(reps[k]) for k in sorted(reps))
              if pt is not None]
    recent = points[-_OPT_RECENT:]
    persistent = None
    if len(recent) >= _OPT_MIN_NIGHTS:
        keys = [_optics_key(p) for p in recent]
        mode = Counter(keys).most_common(1)[0][0]
        agree = [p for p, k in zip(recent, keys) if _same_optics(k, mode)]
        if (mode.startswith("tilt:") or mode == "collimation") \
                and len(agree) >= _OPT_MIN_NIGHTS \
                and len(agree) / len(recent) >= _OPT_SHARE:
            persistent = {"key": mode, "nights": [p["date"] for p in agree],
                          "of": len(recent), "latest": agree[-1]}
    return {"nights": points, "n_nights": len(points),
            "change": _change_point(points), "persistent": persistent}


def _optics_findings(config) -> list[dict]:
    try:
        tr = optics_trend(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("optics trend failed: %s", e)
        return []
    p = tr.get("persistent")
    if not p:
        return []
    last = p["latest"]
    n_ok, n_of = len(p["nights"]), p["of"]
    if p["key"].startswith("tilt:"):
        return [{
            "kind": "optical_tilt", "severity": "medium",
            "key": f"optical_tilt:{last['tilt_dir']}",
            "detail": (f"Optics: the same tilt on {n_ok} of the last {n_of} "
                       f"measured nights (soft side {last['tilt_dir']}, "
                       f"{last['tilt_pct']}% last night). {last['headline']}")}]
    return [{
        "kind": "optical_collimation", "severity": "medium",
        "key": "optical_collimation",
        "detail": (f"Optics: possible collimation on {n_ok} of the last "
                   f"{n_of} measured nights. {last['headline']}")}]


def analyze_trends(config, nights: int = 14) -> dict:
    """Aggregate recent RC16 shape metrics and return systematic findings."""
    subs = _recent_light_subs(config, nights)
    n = len(subs)
    optics = _optics_findings(config)
    if n < _MIN_SUBS:
        return {"n_subs": n, "nights_analyzed": nights, "findings": optics}

    def med(key):
        xs = [float(s[key]) for s in subs if s.get(key) is not None]
        return round(median(xs), 3) if xs else None

    # PS-94: the gating-scale ecc in sqrt form (old "sep-binned" records
    # held 1-b/a, which read far rounder than the same stars graded live)
    from photonscript.shared import qa_rules
    t = qa_rules.thresholds(config, "rc16")
    eccs = [e for e in (qa_rules.gating_ecc(s, t)[0] for s in subs)
            if e is not None]
    m_ecc = round(median(eccs), 3) if eccs else None
    m_pa = med("ecc_pa_R")
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
    # PS-95: optical tilt / collimation come from the optics report trend
    # (_optics_findings), no longer from the mixed-formula ecc_radial_frac.
    findings.extend(optics)

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


def flexure_nights(config, nights: int = 14) -> list[dict]:
    """PS-96: per-night Piggy-600 vs RC16 flexure flag from the cached
    flexure reports (written by the daytime backfill). Report only: no
    Pushover (the piggyback stays silent, DUAL_RIG section 2)."""
    from photonscript.scheduler.flexure import cached_report
    from photonscript.scheduler.runs import list_runs
    out = []
    try:
        dates = [r["date"] for r in list_runs(config)][:nights]
    except Exception:  # noqa: BLE001
        return out
    for d in dates:
        rep = cached_report(config, d)
        if not rep or not rep.get("ok"):
            continue
        sm = rep.get("summary") or {}
        out.append({"date": d, "flagged": bool(rep.get("flagged")),
                    "max_diff_rate_arcsec_min": sm.get("max_diff_rate_arcsec_min"),
                    "max_excess_arcsec_min": sm.get("max_excess_arcsec_min"),
                    "top_cause": (rep.get("causes") or [{}])[0].get("cause")})
    return out


def _alert_state_path(config) -> Path:
    return Path(config.data_dir) / "trend_alerts.json"


def check_and_alert(config, nights: int = 14) -> dict:
    """Run the analysis and fire ONE Pushover per newly-appeared systematic
    finding (deduped via data_dir/trend_alerts.json so it doesn't re-alert every
    night, and re-fires if the issue clears then returns). Safe from the sync
    backfill thread — wraps the async notify itself. Never raises."""
    res = analyze_trends(config, nights)
    # dedupe on "key" when a finding has one (PS-95: a tilt that changes
    # direction is a new finding), else on its kind
    kinds = sorted({f.get("key") or f["kind"]
                    for f in res.get("findings", [])})
    p = _alert_state_path(config)
    try:
        prev = set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()
    except Exception:  # noqa: BLE001
        prev = set()
    new = [f for f in res.get("findings", [])
           if (f.get("key") or f["kind"]) not in prev]
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
