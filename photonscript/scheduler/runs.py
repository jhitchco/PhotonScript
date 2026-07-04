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

import gc
import json
import logging
import re
import threading
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


# One full-frame FITS operation at a time: the scope PC is RAM-tight and
# concurrent regrade + thumbnail requests were failing with MemoryError.
_HEAVY = threading.Lock()

# Calibration frames live in these folders (NINA default layout)
_CAL_DIRS = {"FLAT", "FLATS", "DARK", "DARKS", "BIAS", "BIASES", "SNAPSHOT"}


def _is_calibration(parts) -> bool:
    return any(p.upper() in _CAL_DIRS for p in parts)


def _light_files(root: Path) -> list[Path]:
    return [f for f in sorted(root.rglob("*.fits"))
            if not _is_calibration(f.relative_to(root).parts)]


def _load_binned(path: Path):
    """Header + 2x2-binned float32 frame, built without a full-res float copy.

    Peak memory ~65 MB for a 26 MP frame vs ~210 MB for a naive
    data.astype(float64) — the scope PC was hitting MemoryError.
    """
    import numpy as np
    from astropy.io import fits as _fits

    # do_not_scale_image_data: BZERO-scaled uint16 (NINA's format) refuses
    # memmap otherwise. Scaling is linear so we apply it after binning.
    with _fits.open(path, memmap=True,
                    do_not_scale_image_data=True) as hdul:
        hdr = hdul[0].header.copy()
        raw = hdul[0].data
        h2, w2 = raw.shape[0] // 2 * 2, raw.shape[1] // 2 * 2
        binned = raw[0:h2:2, 0:w2:2].astype(np.float32)
        binned += raw[1:h2:2, 0:w2:2]
        binned += raw[0:h2:2, 1:w2:2]
        binned += raw[1:h2:2, 1:w2:2]
        binned *= 0.25 * float(hdr.get("BSCALE", 1))
        binned += float(hdr.get("BZERO", 0))
    return hdr, binned


def _sep_module():
    try:
        import sep
        return sep
    except ImportError:
        try:
            import sep_pjw as sep
            return sep
        except ImportError:
            return None


def _measure(binned, config) -> dict:
    """Star metrics on a 2x2-binned frame. sep gives real HFR/eccentricity;
    without sep we only report a star count (no fabricated HFR)."""
    import numpy as np

    sep = _sep_module()
    if sep is not None:
        data = np.ascontiguousarray(binned, dtype=np.float32)
        bkg = sep.Background(data)
        data_sub = data - bkg
        # Local noise map, not the global scalar: on nebula frames the
        # global rms underestimates noise inside nebulosity, which produced
        # tens of thousands of false "stars" and sub-pixel HFRs.
        err = np.maximum(bkg.rms(), max(float(bkg.globalrms) * 0.2, 1e-3))
        # Detect on a 3x3 median-filtered image: single-pixel hot pixels
        # (thousands on a 300s uncalibrated CMOS frame) vanish, while real
        # stars — heavily oversampled at this image scale — survive. This
        # is what produced 9700 "stars" at HFR 0.84 and the minute-long
        # segmentation of hot-pixel storms.
        from scipy import ndimage
        det_img = ndimage.median_filter(data_sub, size=3)
        try:
            sep.set_extract_pixstack(1_000_000)
        except Exception:  # noqa: BLE001
            pass
        objs = np.empty(0)
        for thresh in (5.0, 12.0):
            try:
                objs = sep.extract(det_img, thresh, err=err, minarea=6,
                                   clean=True)
            except Exception:  # noqa: BLE001  (pixel buffer overflow etc.)
                continue
            if len(objs) <= 6000:  # plausible; else escalate once
                break
        del det_img
        hfr = ecc = None
        nstars = int(len(objs))
        if len(objs):
            good = objs[(objs["a"] >= 0.6) & (objs["b"] > 0)]
            nstars = int(len(good))
            if len(good):
                top = good[np.argsort(good["flux"])[::-1][:500]]
                try:
                    # Radii measured on the ORIGINAL image at the positions
                    # found on the filtered one
                    r, _ = sep.flux_radius(data_sub, top["x"], top["y"],
                                           6.0 * top["a"], 0.5)
                    r = r[np.isfinite(r) & (r > 0.2) & (r < 15)]
                    if len(r):
                        hfr = round(float(np.median(r)) * 2, 2)  # ->native px
                except Exception:  # noqa: BLE001
                    pass
                with np.errstate(divide="ignore", invalid="ignore"):
                    e = 1.0 - top["b"] / top["a"]
                e = e[np.isfinite(e)]
                if len(e):
                    ecc = round(float(np.median(e)), 3)
        # Tracking-jump detector: a mount jump doubles every star — two
        # ROUND images per star, so ecc/HFR barely move. Signature: many
        # stars have a nearest neighbor at the SAME offset vector.
        doubled_frac = 0.0
        if len(objs) and nstars >= 20:
            try:
                from scipy.spatial import cKDTree
                good_all = objs[(objs["a"] >= 0.6) & (objs["b"] > 0)]
                pts = np.column_stack([good_all["x"], good_all["y"]])[:400]
                dist, idx = cKDTree(pts).query(pts, k=2)
                vec = pts[idx[:, 1]] - pts
                close = dist[:, 1] < 25  # binned px
                if close.sum() >= 10:
                    v = np.round(np.abs(vec[close]) / 1.5)  # 1.5px bins, sign-folded
                    _, counts = np.unique(v, axis=0, return_counts=True)
                    doubled_frac = float(counts.max() / len(pts))
            except Exception:  # noqa: BLE001
                pass
        del data_sub, data, err
        return {"stars": nstars, "hfr": hfr, "ecc": ecc,
                "doubled_frac": round(doubled_frac, 2),
                "background": round(float(bkg.globalback), 1),
                "noise": round(float(bkg.globalrms), 2),
                "graded_by": "sep-binned"}

    # Honest fallback: count stars, don't invent an HFR (the old area-based
    # estimate quantized to 3.91 px for every frame).
    from scipy import ndimage
    sample = binned[::4, ::4]
    background = float(np.median(sample))
    noise = float(np.median(np.abs(sample - background))) * 1.4826 or 1.0
    mask = binned > background + 6 * noise
    labeled, n = ndimage.label(mask)
    nstars = 0
    if n:
        sizes = ndimage.sum(mask, labeled, range(1, min(n, 2000) + 1))
        nstars = int((np.atleast_1d(sizes) >= 3).sum())
    return {"stars": nstars, "hfr": None, "ecc": None,
            "background": round(background, 1), "noise": round(noise, 2),
            "graded_by": "no-sep (install sep-pjw for HFR/ecc)"}


def _plan_target_names(config, date: str) -> list[str]:
    p = runs_dir(config) / f"{date}_plan.json"
    if not p.exists():
        return []
    try:
        return [t["name"] for t in
                json.loads(p.read_text(encoding="utf-8"))["targets"]]
    except Exception:  # noqa: BLE001
        return []


def _resolve_target(raw, filename: str, plan_names: list[str]) -> str:
    """OBJECT header, else filename match, else the plan's only target."""
    t = str(raw or "").strip()
    if t and t != "?":
        return t
    fn = filename.lower()
    for name in plan_names:
        if name.lower().replace(" ", "_") in fn or name.lower() in fn:
            return name
    if len(plan_names) == 1:
        return plan_names[0]
    return "?"


def _fast_grade(path: Path, config, plan_names: list[str] | None = None) -> dict:
    """Per-sub metrics for backfill: sep on a 2x2-binned frame."""
    with _HEAVY:
        hdr, binned = _load_binned(path)
        m = _measure(binned, config)
        del binned
    gc.collect()
    reasons = []
    if m["stars"] < 5:
        reasons.append(f"only {m['stars']} stars")
    ecc_max = float(getattr(config, "quality_eccentricity_max", 0.6))
    if m["ecc"] is not None and m["ecc"] > ecc_max:
        reasons.append(f"elongated stars (ecc {m['ecc']} > {ecc_max:g})")
    if m.get("doubled_frac", 0) >= 0.25:
        reasons.append(f"tracking jump: {round(m['doubled_frac']*100)}% of "
                       "stars doubled at a consistent offset")
    passed = not reasons
    hfr = m["hfr"]
    return {
        "time": hdr.get("DATE-OBS", ""),
        "target": _resolve_target(hdr.get("OBJECT"), path.name,
                                  plan_names or []),
        "filter": hdr.get("FILTER", "?"),
        "exp_s": float(hdr.get("EXPTIME", 0)),
        "ccd_temp": hdr.get("CCD-TEMP"),
        "hfr": hfr,
        "fwhm_arcsec": round(hfr * config.pixel_scale_arcsec, 2) if hfr else None,
        "stars": m["stars"], "ecc": m["ecc"],
        "background": m["background"],
        "doubled_frac": m.get("doubled_frac"),
        "passed_qa": passed,
        "reason": "; ".join(reasons),
        "graded_by": m["graded_by"],
    }


_backfill_state: dict[str, dict] = {}


def backfill_status(config, date: str) -> dict:
    root = Path(config.image_watch_dir) / date
    total = len(_light_files(root)) if root.exists() else 0
    logged = len(_load_subs(config, date))
    st = _backfill_state.get(date, {})
    pending = max(0, total - logged)
    rate = st.get("rate")  # frames/s this run
    import time
    since = st.get("current_since")
    return {"running": st.get("running", False),
            "graded": logged, "total_files": total,
            "pending": pending,
            "current": st.get("current"),
            "current_s": round(time.monotonic() - since) if since else None,
            "rate": rate,
            "eta_s": round(pending / rate) if (rate and pending) else None,
            "last_error": st.get("last_error")}


def start_backfill(config, date: str) -> None:
    """Grade missing FITS in a background thread, appending incrementally."""
    import threading

    st = _backfill_state.setdefault(date, {})
    if st.get("running"):
        logger.info("Backfill for %s already running (%s) — not starting "
                    "another", date, st.get("current") or "between frames")
        return
    st["running"] = True  # set before the thread spawns: closes the window
    root = Path(config.image_watch_dir) / date
    if not root.exists():
        st["running"] = False
        logger.warning("Backfill for %s: image folder %s does not exist",
                       date, root)
        return
    n_total = len(_light_files(root))
    n_done = len(_load_subs(config, date))
    logger.info("Backfill starting for %s: %d light frames, %d already "
                "graded, %d to do", date, n_total, n_done, n_total - n_done)

    def _work():
        import time
        st.update(current=None, rate=None, last_error=None)
        started, done = time.monotonic(), 0
        try:
            existing = {r.get("file") for r in _load_subs(config, date)}
            plan_names = _plan_target_names(config, date)
            for f in _light_files(root):
                rel = str(f.relative_to(root))
                if rel in existing:
                    continue
                st["current"] = rel
                st["current_since"] = time.monotonic()
                t0 = time.monotonic()
                try:
                    record = _fast_grade(f, config, plan_names)
                    record["file"] = rel
                    record["abs_path"] = str(f)
                    append_sub_record(config, date, record)
                    logger.info("Graded %s in %.1fs: HFR %s ecc %s stars %s",
                                rel, time.monotonic() - t0,
                                record.get("hfr"), record.get("ecc"),
                                record.get("stars"))
                except Exception as e:  # noqa: BLE001
                    st["last_error"] = f"{rel}: {e}"
                    logger.warning("Backfill grade failed for %s: %s", f, e)
                done += 1
                st["rate"] = round(done / max(time.monotonic() - started,
                                              0.001), 2)
            logger.info("Backfill finished for %s: %d frames graded in "
                        "%.0fs", date, done, time.monotonic() - started)
            try:
                n_out = flag_hfr_outliers(config, date)
                if n_out:
                    logger.info("HFR outlier pass for %s: %d subs flagged",
                                date, n_out)
            except Exception as e:  # noqa: BLE001
                logger.warning("Outlier pass failed for %s: %s", date, e)
            try:  # keep the accepted-lights library current
                build_library(config, date)
            except Exception as e:  # noqa: BLE001
                logger.warning("Library update failed for %s: %s", date, e)
        finally:
            st["running"] = False
            st["current"] = None

    threading.Thread(target=_work, daemon=True,
                     name=f"backfill-{date}").start()


def _rewrite_subs(config, date: str, records: list[dict]) -> None:
    p = runs_dir(config) / f"{date}_subs.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records),
                 encoding="utf-8")


def flag_hfr_outliers(config, date: str, factor: float = 1.4) -> int:
    """Post-pass: reject subs whose HFR is far above the night's per-filter
    median — soft/trailed frames that pass absolute checks. Never
    un-rejects, never overrides a manual verdict."""
    subs = _load_subs(config, date)
    by_filter: dict[str, list[float]] = {}
    for s_ in subs:
        if s_.get("hfr"):
            by_filter.setdefault(s_.get("filter", "?"), []).append(s_["hfr"])
    med = {f: sorted(v)[len(v) // 2] for f, v in by_filter.items()
           if len(v) >= 5}
    n = 0
    for s_ in subs:
        if not s_.get("passed_qa") or s_.get("manual_qa"):
            continue
        m_ = med.get(s_.get("filter", "?"))
        if m_ and s_.get("hfr") and s_["hfr"] > m_ * factor:
            s_["passed_qa"] = False
            s_["reason"] = (f"HFR outlier: {s_['hfr']} vs night median "
                            f"{m_} (x{factor:g} limit)")
            n += 1
    if n:
        _rewrite_subs(config, date, subs)
    return n


def approve_night(config, date: str) -> dict:
    """Mark every QA-passing sub as reviewed, then update the library so
    they queue for transfer. The human gate between capture and sync."""
    subs = _load_subs(config, date)
    n = 0
    for s_ in subs:
        if s_.get("passed_qa") and not s_.get("reviewed"):
            s_["reviewed"] = True
            n += 1
    if n:
        _rewrite_subs(config, date, subs)
    res = build_library(config, date)
    logger.info("Night %s approved: %d subs -> library (%s new links)",
                date, n, res.get("linked"))
    return {"approved": n, **res}


def set_manual_qa(config, date: str, rel_file: str,
                  passed: bool) -> dict | None:
    """Human override for one sub; wins over every automatic pass."""
    subs = _load_subs(config, date)
    hit = None
    for s_ in subs:
        if s_.get("file") == rel_file:
            s_["passed_qa"] = passed
            s_["manual_qa"] = True
            if passed:
                s_["reviewed"] = True  # a manual pass IS the review
            s_["reason"] = "" if passed else "rejected manually"
            hit = s_
    if hit is None:
        return None
    _rewrite_subs(config, date, subs)
    try:  # keep the library consistent with the verdict
        if passed:
            build_library(config, date)
        else:
            name = Path(hit.get("abs_path") or "").name
            if name:
                for f in library_root(config).rglob(name):
                    f.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("library update after manual QA failed: %s", e)
    return hit


_PHASE_PATTERNS = {
    "autofocus": re.compile(r"autofocus", re.IGNORECASE),
    "platesolve": re.compile(r"plate\s*sol", re.IGNORECASE),
    "meridian": re.compile(r"meridian\s*flip", re.IGNORECASE),
    "flip_errors": re.compile(r"\|ERROR\|MeridianFlip"),
    "solve_failures": re.compile(r"plate\s*sol.*fail", re.IGNORECASE),
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
    lo, hi = f"{date}T12:00:00", None
    from datetime import timedelta as _td
    hi = (datetime.fromisoformat(date) + _td(days=1)).strftime("%Y-%m-%dT12:00:00")
    try:
        with open(logs[-1], encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _TS.match(line)
                ts = m.group(1)[:19] if m else None
                if ts and not (lo <= ts <= hi):
                    continue
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
        n_lights = n_cal = 0
        night_root = fits_root / d
        if night_root.exists():
            for f in night_root.rglob("*.fits"):
                if _is_calibration(f.relative_to(night_root).parts):
                    n_cal += 1
                else:
                    n_lights += 1
        out.append({"date": d, "subs_logged": len(subs),
                    "lights": n_lights, "cal_frames": n_cal,
                    "has_plan": (runs_dir(config) / f"{d}_plan.json").exists()})
    return out


def nights_by_target(config) -> dict:
    """{target_lower: [{date, accepted, attempted}]} across all graded nights."""
    out: dict[str, dict[str, dict]] = {}
    for f in sorted(runs_dir(config).glob("*_subs.jsonl")):
        date = f.name.split("_")[0]
        plan_names = _plan_target_names(config, date)
        for s_ in _load_subs(config, date):
            t = _resolve_target(s_.get("target"), s_.get("file", ""),
                                plan_names).strip().lower()
            if not t or t == "?":
                continue
            e = out.setdefault(t, {}).setdefault(
                date, {"date": date, "accepted": 0, "attempted": 0})
            e["attempted"] += 1
            if s_.get("passed_qa"):
                e["accepted"] += 1
    return {t: sorted(d.values(), key=lambda x: x["date"], reverse=True)
            for t, d in out.items()}


def night_detail(config, date: str, backfill: bool = True) -> dict:
    """Full plan-vs-actual record for one night."""
    from photonscript.scheduler.daily_report import build_daily_report

    plan_path = runs_dir(config) / f"{date}_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) \
        if plan_path.exists() else None

    status = backfill_status(config, date)
    if backfill and status["pending"] and not status["running"]:
        start_backfill(config, date)
        status = backfill_status(config, date)
    subs = _load_subs(config, date)

    # Fix up subs recorded before target attribution existed
    plan_names = _plan_target_names(config, date)
    for s_ in subs:
        s_["target"] = _resolve_target(s_.get("target"),
                                       s_.get("file", ""), plan_names)

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
        "calibration": calibration_inventory(config, date),
        "phases": _phase_stats(config, date),
        "score": score,
        "backfill": status,
    }


# --- Librarian: integration-ready folder tree ----------------------------------

def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", str(name)).strip() or "Unknown"


def library_root(config) -> Path:
    d = getattr(config, "library_dir", "") or ""
    return Path(d) if d else Path(config.data_dir) / "Library"


def build_library(config, date: str | None = None) -> dict:
    """Maintain Library/{Target}/{Filter}/ hardlinks of QA-accepted lights,
    plus Calibration/{TYPE}/ for bias/darks/flats.

    Hardlinks cost no disk and stay in sync with the originals; point
    Syncthing (or robocopy) at this folder to pull integration-ready data
    to another machine. Falls back to copy across volumes.
    """
    import os
    import shutil

    lib = library_root(config)
    if date:
        dates = [date]
    else:
        dates = sorted({f.name.split("_")[0]
                        for f in runs_dir(config).glob("*_subs.jsonl")})
    review_gate = bool(getattr(config, "review_gate", True))
    linked = skipped = missing = rejected = pending_review = 0
    for d in dates:
        plan_names = _plan_target_names(config, d)
        for s_ in _load_subs(config, d):
            if not s_.get("passed_qa"):
                rejected += 1
                continue
            if review_gate and not s_.get("reviewed"):
                pending_review += 1
                continue
            src = Path(s_.get("abs_path") or "")
            if not src.exists():
                missing += 1
                continue
            target = _safe_name(_resolve_target(
                s_.get("target"), s_.get("file", ""), plan_names))
            dest = lib / target / _safe_name(s_.get("filter", "?")) / src.name
            if dest.exists():
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dest)
            except OSError:  # cross-volume or FS without hardlinks
                shutil.copy2(src, dest)
            linked += 1
        root = Path(config.image_watch_dir) / d
        if root.exists():
            for f in root.rglob("*.fits"):
                parts = f.relative_to(root).parts
                if not _is_calibration(parts):
                    continue
                typ = next((p.upper().rstrip("S") for p in parts
                            if p.upper() in _CAL_DIRS), "CAL")
                dest = lib / "Calibration" / typ / d / f.name
                if dest.exists():
                    skipped += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(f, dest)
                except OSError:
                    shutil.copy2(f, dest)
                linked += 1
    result = {"library": str(lib), "nights": len(dates), "linked": linked,
              "already_there": skipped, "rejected_excluded": rejected,
              "pending_review": pending_review, "missing_files": missing}
    logger.info("Library update: %s", result)
    return result


# --- Calibration frames --------------------------------------------------------

def calibration_inventory(config, date: str) -> dict:
    """Count BIAS/DARK/FLAT frames for the night and flag missing coverage.

    Header-only reads — cheap even for hundreds of frames.
    """
    from astropy.io import fits as _fits

    root = Path(config.image_watch_dir) / date
    frames: dict[str, dict] = {}
    light_filters: set[str] = set()
    light_exps: set[float] = set()
    if root.exists():
        for f in sorted(root.rglob("*.fits")):
            parts = f.relative_to(root).parts
            try:
                hdr = _fits.getheader(f)
            except Exception:  # noqa: BLE001
                continue
            filt = str(hdr.get("FILTER", "?"))
            exp = float(hdr.get("EXPTIME", 0))
            if not _is_calibration(parts):
                light_filters.add(filt)
                light_exps.add(exp)
                continue
            typ = str(hdr.get("IMAGETYP", "")).strip().upper() or next(
                (p.upper().rstrip("S") for p in parts
                 if p.upper() in _CAL_DIRS), "CAL")
            typ = typ.replace(" FRAME", "").replace("LIGHT", "CAL")
            g = frames.setdefault(typ, {"count": 0, "filters": {},
                                        "exposures": {}})
            g["count"] += 1
            g["filters"][filt] = g["filters"].get(filt, 0) + 1
            key = f"{exp:g}s"
            g["exposures"][key] = g["exposures"].get(key, 0) + 1

    advice = []
    if light_filters:
        flat_filters = set(frames.get("FLAT", {}).get("filters", {}))
        missing_flats = sorted(light_filters - flat_filters - {"?"})
        if missing_flats:
            advice.append("no flats for: " + ", ".join(missing_flats))
        dark_exps = {float(k.rstrip("s")) for k in
                     frames.get("DARK", {}).get("exposures", {})}
        missing_darks = sorted(e for e in light_exps
                               if e > 0 and e not in dark_exps)
        if missing_darks:
            advice.append("no darks matching light exposures: "
                          + ", ".join(f"{e:g}s" for e in missing_darks))
        if "BIAS" not in frames:
            advice.append("no bias frames this night (fine if you use a "
                          "master bias / dark library)")
    return {"frames": frames, "advice": advice,
            "lights_ok": bool(light_filters)}


# --- Thumbnails ---------------------------------------------------------------

def thumbnail(config, date: str, rel_file: str, width: int = 360,
              annotate: bool = False) -> Path | None:
    """Stretched PNG thumbnail; optionally with star-detection circles.

    Cached on disk per (file, width, annotate) — repeat remote views never
    reopen the FITS. Frame is loaded binned + decimated (a few MB), never
    at full resolution: full-res loads were exhausting the scope PC's RAM.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    src = Path(config.image_watch_dir) / date / rel_file
    if not src.exists() or ".." in rel_file:
        return None
    out_dir = Path(config.data_dir) / "thumbs" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = rel_file.replace("\\", "_").replace("/", "_")
    out = out_dir / f"{stem}.w{width}{'.ann' if annotate else ''}.png"
    if out.exists():
        return out
    try:
        with _HEAVY:
            _, binned = _load_binned(src)
            step = max(1, binned.shape[1] // 1400)
            small = np.ascontiguousarray(binned[::step, ::step])
            del binned
            stars = []
            if annotate:
                stars_m = _measure(small, config)
                sep = _sep_module()
                if sep is not None:
                    bkg = sep.Background(small)
                    try:
                        objs = sep.extract(small - bkg, 5.0,
                                           err=bkg.globalrms)
                        stars = [(float(o["x"]), float(o["y"]),
                                  float(o["a"])) for o in objs[:300]]
                    except Exception:  # noqa: BLE001
                        pass
        gc.collect()
        lo, hi = np.percentile(small, (0.5, 99.7))
        stretched = np.sqrt(np.clip((small - lo) / max(hi - lo, 1e-3), 0, 1))
        img = Image.fromarray((stretched * 255).astype(np.uint8),
                              mode="L").convert("RGB")
        if stars:
            draw = ImageDraw.Draw(img)
            for x, y, a in stars:
                r0 = max(4, a * 3)
                draw.ellipse([x - r0, y - r0, x + r0, y + r0],
                             outline=(248, 113, 113), width=2)
        h = int(img.height * width / img.width)
        img.resize((width, h)).save(out)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("Thumbnail failed for %s: %s", src, e)
        return None


def build_bundle(config, date: str) -> Path:
    """Package the night's evidence into one zip (shared by CLI and web)."""
    import zipfile
    from photonscript.scheduler.daily_report import build_daily_report

    out = Path(config.data_dir) / f"night_bundle_{date}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        try:
            z.writestr("report.txt", build_daily_report(config, date).to_text())
        except Exception as e:  # noqa: BLE001
            z.writestr("report.txt", f"report failed: {e}")
        try:
            z.writestr("night_detail.json",
                       json.dumps(night_detail(config, date, backfill=False), indent=1,
                                  default=str))
        except Exception as e:  # noqa: BLE001
            z.writestr("night_detail.json", f'{{"error": "{e}"}}')
        for name in ("armer_state.json", "projects.json"):
            p = Path(config.data_dir) / name
            if p.exists():
                z.write(p, name)
        for suffix in ("_plan.json", "_subs.jsonl"):
            p = runs_dir(config) / f"{date}{suffix}"
            if p.exists():
                z.write(p, f"runs/{p.name}")
        seq_dir = Path.cwd() / "sequences"
        if seq_dir.exists():
            for f in sorted(seq_dir.glob("*.json"))[-3:]:
                z.write(f, f"sequences/{f.name}")
        import glob as _glob
        logs = sorted(_glob.glob(str(Path(config.nina_logs_dir) / "*.log")))
        if logs:
            z.write(logs[-1], f"nina/{Path(logs[-1]).name}")
        fits_root = Path(config.image_watch_dir) / date
        if fits_root.exists():
            listing = "\n".join(
                f"{f.stat().st_size:>12}  {f.relative_to(fits_root)}"
                for f in sorted(fits_root.rglob("*.fits")))
            z.writestr("fits_inventory.txt", listing or "(no FITS files)")
    return out
