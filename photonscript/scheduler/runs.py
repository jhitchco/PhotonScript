"""Imaging Runs — plan vs. actual analysis, night scoring, thumbnails.

Data sources per night (local date of dusk):
  data/runs/<date>_plan.json   plan snapshot saved by the armer at dispatch
  data/runs/<date>_subs.jsonl  per-sub records appended by the telescope agent
                               (or generated on demand for older nights)
  NINA log                     phase timing (autofocus, plate solve, safety)

Night score (0-100):
  30%  sky utilization      shutter hours / safe hours
  25%  photon efficiency    integrating hours / shutter hours
  25%  QA pass rate         accepted subs / graded subs
  20%  plan completion      accepted subs / planned subs (capped at 1)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def runs_dir(config) -> Path:
    p = Path(config.data_dir) / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_plan_snapshot(config, night_of: str, plan: dict, targets) -> None:
    """Called by the armer at dispatch time."""
    snapshot = {
        "night_of": night_of,
        "saved_at": datetime.utcnow().isoformat() + "Z",
        "dusk_utc": plan.get("dusk_utc"),
        "dawn_utc": plan.get("dawn_utc"),
        "dark_hours": plan.get("dark_hours"),
        "targets": [{
            "name": t.name,
            "exposures": [{"filter": e.filter_type.value,
                           "exp_s": e.exposure_seconds,
                           "planned": e.count - e.acquired}
                          for e in t.exposures],
        } for t in targets],
    }
    (runs_dir(config) / f"{night_of}_plan.json").write_text(
        json.dumps(snapshot, indent=1), encoding="utf-8")


def append_sub_record(config, night_of: str, record: dict) -> None:
    """Called by the telescope agent for every graded sub."""
    try:
        with open(runs_dir(config) / f"{night_of}_subs.jsonl", "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        logger.error("Could not append sub record: %s", e)


def _load_subs(config, date: str) -> list[dict]:
    p = runs_dir(config) / f"{date}_subs.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _grade_missing_subs(config, date: str) -> list[dict]:
    """Backfill: grade FITS from the night folder for nights before per-sub
    logging existed (or after a PhotonScript outage). Cached to the jsonl."""
    from photonscript.telescope_agent.image_validator import validate_image
    from astropy.io import fits as _fits

    root = Path(config.image_watch_dir) / date
    if not root.exists():
        return []
    existing = {r.get("file") for r in _load_subs(config, date)}
    added = []
    for f in sorted(root.rglob("*.fits")):
        rel = str(f.relative_to(root))
        if rel in existing:
            continue
        try:
            with _fits.open(f) as hdul:
                hdr = hdul[0].header
            q = validate_image(str(f), config)
            record = {
                "file": rel, "abs_path": str(f),
                "time": hdr.get("DATE-OBS", ""),
                "target": hdr.get("OBJECT", "?"),
                "filter": hdr.get("FILTER", "?"),
                "exp_s": float(hdr.get("EXPTIME", 0)),
                "ccd_temp": hdr.get("CCD-TEMP"),
                "hfr": q.hfr_pixels, "fwhm_arcsec": q.fwhm_arcsec,
                "stars": q.star_count, "ecc": q.eccentricity,
                "background": q.background_adu,
                "passed_qa": q.passed_qa, "reason": q.rejection_reason,
            }
            append_sub_record(config, date, record)
            added.append(record)
        except Exception as e:  # noqa: BLE001
            logger.warning("Backfill grade failed for %s: %s", f, e)
    return added


_PHASE_PATTERNS = {
    "autofocus": re.compile(r"autofocus", re.IGNORECASE),
    "platesolve": re.compile(r"plate\s*sol", re.IGNORECASE),
    "meridian": re.compile(r"meridian\s*flip", re.IGNORECASE),
    "error": re.compile(r"\|ERROR\|"),
}
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+)")


def _phase_stats(config, date: str) -> dict:
    """Best-effort phase activity from the newest NINA log: event counts and
    the span of timestamps mentioning each phase."""
    import glob
    logs = sorted(glob.glob(str(Path(config.nina_logs_dir) / "*.log")))
    if not logs:
        return {}
    stats: dict[str, dict] = {k: {"events": 0, "first": None, "last": None}
                              for k in _PHASE_PATTERNS}
    try:
        with open(logs[-1], encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _TS.match(line)
                ts = m.group(1)[:19] if m else None
                for key, pat in _PHASE_PATTERNS.items():
                    if pat.search(line):
                        s = stats[key]
                        s["events"] += 1
                        if ts:
                            s["first"] = s["first"] or ts
                            s["last"] = ts
    except OSError:
        return {}
    return {k: v for k, v in stats.items() if v["events"]}


def night_score(util_pct: float, eff_pct: float, qa_pct: float,
                completion_pct: float) -> dict:
    parts = {
        "sky_utilization": (0.30, min(util_pct, 100)),
        "photon_efficiency": (0.25, min(eff_pct, 100)),
        "qa_pass_rate": (0.25, min(qa_pct, 100)),
        "plan_completion": (0.20, min(completion_pct, 100)),
    }
    total = sum(w * v for w, v in parts.values())
    return {"total": round(total),
            "breakdown": {k: {"weight": w, "value": round(v)}
                          for k, (w, v) in parts.items()}}


def list_runs(config) -> list[dict]:
    """Nights with any evidence: plan, subs log, or FITS folder."""
    dates = set()
    for f in runs_dir(config).glob("*_plan.json"):
        dates.add(f.name.split("_")[0])
    for f in runs_dir(config).glob("*_subs.jsonl"):
        dates.add(f.name.split("_")[0])
    fits_root = Path(config.image_watch_dir)
    if fits_root.exists():
        for d in fits_root.iterdir():
            if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}$", d.name):
                dates.add(d.name)
    out = []
    for d in sorted(dates, reverse=True):
        subs = _load_subs(config, d)
        out.append({"date": d, "subs_logged": len(subs),
                    "has_plan": (runs_dir(config) / f"{d}_plan.json").exists()})
    return out


def night_detail(config, date: str, backfill: bool = True) -> dict:
    """Full plan-vs-actual record for one night."""
    from photonscript.scheduler.daily_report import build_daily_report

    plan_path = runs_dir(config) / f"{date}_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) \
        if plan_path.exists() else None

    if backfill:
        _grade_missing_subs(config, date)
    subs = _load_subs(config, date)

    # Plan vs actual per target/filter
    planned: dict[tuple, int] = {}
    if plan:
        for t in plan["targets"]:
            for e in t["exposures"]:
                planned[(t["name"], e["filter"])] = \
                    planned.get((t["name"], e["filter"]), 0) + e["planned"]
    actual: dict[tuple, dict] = {}
    for s in subs:
        key = (s.get("target", "?"), s.get("filter", "?"))
        a = actual.setdefault(key, {"attempted": 0, "accepted": 0,
                                    "hfrs": [], "eccs": [], "bgs": []})
        a["attempted"] += 1
        if s.get("passed_qa"):
            a["accepted"] += 1
        if s.get("hfr"):
            a["hfrs"].append(s["hfr"])
        if s.get("ecc") is not None:
            a["eccs"].append(s["ecc"])
        if s.get("background") is not None:
            a["bgs"].append(s["background"])

    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        return round(xs[len(xs) // 2], 2) if xs else None

    table = []
    for key in sorted(set(planned) | set(actual)):
        a = actual.get(key, {})
        table.append({
            "target": key[0], "filter": key[1],
            "planned": planned.get(key, 0),
            "attempted": a.get("attempted", 0),
            "accepted": a.get("accepted", 0),
            "median_hfr": med(a.get("hfrs", [])),
            "median_ecc": med(a.get("eccs", [])),
            "median_background": med(a.get("bgs", [])),
        })

    report = build_daily_report(config, date)
    graded = len(subs)
    accepted = sum(1 for s in subs if s.get("passed_qa"))
    total_planned = sum(planned.values())
    score = night_score(
        report.sky_utilization_pct,
        report.photon_efficiency_pct,
        (accepted / graded * 100) if graded else 0,
        (accepted / total_planned * 100) if total_planned else
        (100 if accepted else 0),
    )

    return {
        "date": date,
        "plan": plan,
        "report": {
            "safe_hours": report.safe_hours,
            "shutter_hours": report.shutter_hours,
            "integrating_hours": report.integrating_hours,
            "sky_utilization_pct": round(report.sky_utilization_pct),
            "photon_efficiency_pct": round(report.photon_efficiency_pct),
        },
        "table": table,
        "subs": subs,
        "phases": _phase_stats(config, date),
        "score": score,
    }


# --- Thumbnails ---------------------------------------------------------------

def thumbnail(config, date: str, rel_file: str, width: int = 360) -> Path | None:
    """Stretched PNG thumbnail for a sub, generated once and cached."""
    import numpy as np
    from astropy.io import fits as _fits
    from PIL import Image

    src = Path(config.image_watch_dir) / date / rel_file
    if not src.exists() or ".." in rel_file:
        return None
    out_dir = Path(config.data_dir) / "thumbs" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (rel_file.replace("\\", "_").replace("/", "_") + ".png")
    if out.exists():
        return out
    try:
        with _fits.open(src) as hdul:
            data = hdul[0].data.astype(np.float32)
        lo, hi = np.percentile(data, (0.5, 99.7))
        stretched = np.clip((data - lo) / max(hi - lo, 1e-3), 0, 1)
        stretched = np.sqrt(stretched)  # gentle nonlinear stretch
        img = Image.fromarray((stretched * 255).astype(np.uint8), mode="L")
        h = int(img.height * width / img.width)
        img.resize((width, h)).save(out)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("Thumbnail failed for %s: %s", src, e)
        return None
