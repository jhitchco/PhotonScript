"""Self-seeding autofocus for the piggyback (OSC) rig.

The OSC has its OWN EAF, a different focuser than the RC16's, so it can't share
the RC16 focus_seeds table (different absolute range, and the RC16 clamp would
corrupt OSC positions). This module keeps a tiny per-rig store of known-good OSC
focuser positions and hands the sequence generator a cold-start seed, so the
first AF of the night starts near focus instead of failing to build an HFR curve
from a wild position (the 2026-09-20 defocus night: FWHM 16.8"->6.5" crept in
over hours, 271/283 rejected).

Loop:
  * harvest_piggyback_night() reads FOCPOS/FOCTEMP from the night's sharp OSC
    subs (passed QA, real HFR below a tight threshold) and appends one median
    record to piggyback_focus_seeds.json.
  * piggyback_seed_for() opportunistically harvests the most recent nights, then
    returns a temperature-aware position (falling back to the static
    piggyback_focus_seed config, then to 0 = disabled).

Everything is best-effort: any I/O or parse error degrades to the static seed so
sequence generation never fails on a focus-history read.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _store_path(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "piggyback_focus_seeds.json"


def _load(config) -> list[dict]:
    p = _store_path(config)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, json.JSONDecodeError):
        return []


def _clamp(config, pos: float) -> int:
    lo = int(getattr(config, "piggyback_focpos_min", 0) or 0)
    hi = int(getattr(config, "piggyback_focpos_max", 0) or 0)
    pos = round(pos)
    if hi > lo:  # only clamp when a real OSC EAF range is configured
        pos = max(lo, min(hi, pos))
    return int(pos)


def harvest_piggyback_night(config, date: str,
                            max_hfr_px: float | None = None) -> int:
    """Append one median FOCPOS record from the night's sharp OSC subs.

    Returns 1 if a record was added, else 0. Only piggyback subs that passed QA
    with a real HFR at/below the threshold contribute, so a soft/defocused night
    never poisons the seed.
    """
    from photonscript.scheduler.runs import _load_subs
    from photonscript.scheduler.focus_seeds import _read_focus_header

    if max_hfr_px is None:
        max_hfr_px = float(getattr(config, "piggyback_focus_harvest_max_hfr", 3.0))

    # already harvested this night? keep it idempotent so repeat arms don't pile
    # duplicate records into the store.
    if any(r.get("date") == date and r.get("source") == "harvest"
           for r in _load(config)):
        return 0

    pts: list[tuple[float, float | None]] = []
    for s in _load_subs(config, date):
        if s.get("rig") != "piggyback" or not s.get("passed_qa"):
            continue
        hfr = s.get("hfr")
        if hfr is None or hfr > max_hfr_px:
            continue
        path = s.get("abs_path")
        if not path:
            continue
        hdr = _read_focus_header(path)
        fp, ft = hdr.get("FOCPOS"), hdr.get("FOCTEMP")
        if fp is None:
            continue
        pts.append((float(fp), float(ft) if ft is not None else None))

    if not pts:
        return 0
    positions = sorted(p for p, _ in pts)
    med_pos = positions[len(positions) // 2]
    temps = sorted(t for _, t in pts if t is not None)
    med_temp = temps[len(temps) // 2] if temps else None

    store = _load(config)
    store.append({"focpos": _clamp(config, med_pos),
                  "foctemp": round(med_temp, 1) if med_temp is not None else None,
                  "n": len(pts), "date": date, "source": "harvest"})
    p = _store_path(config)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(store, indent=1), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        logger.warning("piggyback_focus: store write failed: %s", e)
        return 0
    logger.info("piggyback_focus: harvested FOCPOS %d (%d frames) from %s",
                _clamp(config, med_pos), len(pts), date)
    return 1


def _recent_dates(days: int) -> list[str]:
    from datetime import datetime, timedelta
    today = datetime.utcnow().date()
    return [(today - timedelta(days=d)).strftime("%Y-%m-%d")
            for d in range(days + 1)]


def _seed_from_records(config, recs: list[dict], foctemp: float | None) -> int | None:
    """Temperature-aware position from harvested records, or None if empty."""
    recs = [r for r in recs if r.get("focpos") is not None]
    if not recs:
        return None
    pts = [(float(r["foctemp"]), float(r["focpos"]))
           for r in recs if r.get("foctemp") is not None]
    if foctemp is not None and len(pts) >= 2:
        n = len(pts)
        sx = sum(t for t, _ in pts); sy = sum(p for _, p in pts)
        sxx = sum(t * t for t, _ in pts); sxy = sum(t * p for t, p in pts)
        denom = n * sxx - sx * sx
        if denom != 0:
            slope = (n * sxy - sx * sy) / denom
            intercept = (sy - slope * sx) / n
            lo = min(p for _, p in pts) - 300
            hi = max(p for _, p in pts) + 300
            return _clamp(config, max(lo, min(hi, slope * foctemp + intercept)))
    if foctemp is not None and pts:
        _, pos = min(pts, key=lambda tp: abs(tp[0] - foctemp))
        return _clamp(config, pos)
    vals = sorted(float(r["focpos"]) for r in recs)
    return _clamp(config, vals[len(vals) // 2])


def current_seed(config, foctemp: float | None = None) -> int:
    """Read-only seed from the EXISTING store (no harvest side effects).

    Harvested records win; else the static piggyback_focus_seed; else 0.
    Safe to call on every dashboard refresh. Never raises.
    """
    try:
        seed = _seed_from_records(config, _load(config), foctemp)
        if seed is not None:
            return seed
    except Exception as e:  # noqa: BLE001
        logger.warning("piggyback_focus: current_seed failed (%s)", e)
    return int(getattr(config, "piggyback_focus_seed", 0) or 0)


def seed_source(config) -> str:
    """Where the active seed comes from: 'harvested', 'static', or 'disabled'."""
    if any(r.get("focpos") is not None for r in _load(config)):
        return "harvested"
    if int(getattr(config, "piggyback_focus_seed", 0) or 0) > 0:
        return "static"
    return "disabled"


def piggyback_seed_for(config, foctemp: float | None = None,
                       harvest_recent_nights: int = 3) -> int:
    """Cold-start focuser position for the OSC's own EAF.

    Refreshes the store from recent nights, then returns a temperature-aware
    seed from the harvested records. Falls back to the static
    piggyback_focus_seed config, then 0 (disabled). Never raises.
    """
    try:
        for d in _recent_dates(int(harvest_recent_nights)):
            try:
                harvest_piggyback_night(config, d)
            except Exception as e:  # noqa: BLE001 — one bad night can't block a seed
                logger.debug("piggyback_focus: harvest %s skipped (%s)", d, e)
    except Exception as e:  # noqa: BLE001 — seeding is advisory; never break generation
        logger.warning("piggyback_focus: harvest pass failed (%s)", e)
    return current_seed(config, foctemp)
