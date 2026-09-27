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
import math
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared.target_names import (canonical_target,
                                              known_target_index, target_key)

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


def _sanitize_floats(obj):
    """Replace non-finite floats (NaN/Inf) with None, recursively.

    NINA/QA occasionally emits NaN for stats like background (e.g. dawn
    frames). Python's json writes these as the literal `NaN`, which reads
    back fine but is NOT valid JSON — Starlette's JSONResponse serializes
    with allow_nan=False and raises, 500-ing the whole night's detail view.
    Coercing to None keeps a single un-computable stat from sinking the run.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    return obj


def append_sub_record(config, night_of: str, record: dict) -> None:
    """Called by the telescope agent for every graded sub."""
    try:
        with open(runs_dir(config) / f"{night_of}_subs.jsonl", "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(_sanitize_floats(record)) + "\n")
    except OSError as e:
        logger.error("Could not append sub record: %s", e)


_subs_cache: dict = {}  # path -> (signature, parsed rows)
_subs_cache_lock = threading.Lock()


def _invalidate_subs_cache(p: Path) -> None:
    with _subs_cache_lock:
        _subs_cache.pop(str(p), None)


def _load_subs(config, date: str) -> list[dict]:
    """Parsed ``<date>_subs.jsonl``. Memoized on the file's mtime + size, so
    the dashboard / runs polls stop re-parsing every night's log on every
    request; any write to the log changes the signature and re-reads it.
    Returns fresh top-level dicts each call (callers mutate them)."""
    p = runs_dir(config) / f"{date}_subs.jsonl"
    try:
        st = p.stat()
    except OSError:
        _invalidate_subs_cache(p)
        return []
    try:  # normalize NINA filter names (H/O/S) to class names (Ha/OIII/SII)
        rev = config.reverse_filter_map()
    except Exception:  # noqa: BLE001
        rev = {}
    sig = (st.st_mtime_ns, st.st_size, tuple(sorted(rev.items())))
    key = str(p)
    with _subs_cache_lock:
        hit = _subs_cache.get(key)
    if hit is not None and hit[0] == sig:
        return [dict(r) for r in hit[1]]
    out = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        try:
            r = _sanitize_floats(json.loads(line))
        except json.JSONDecodeError:
            continue
        f = r.get("filter")
        if f in rev:
            r["filter"] = rev[f]
        out.append(r)
    with _subs_cache_lock:
        _subs_cache[key] = (sig, out)
    return [dict(r) for r in out]


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


def _shape_diagnostics(objs, W, H, ecc_floor: float = 0.30) -> dict:
    """Per-corner eccentricity + elongation-direction diagnosis.

    Distinguishes the *cause* of elongated stars, which a single whole-frame
    ecc cannot:
      - uniform PA  -> stars stretched the same direction everywhere = mount
                       tracking error / wind shake
      - radial      -> stretch points out from the frame center = tilt /
                       collimation / field curvature
      - round       -> nothing to see
    Returns corner ecc (TL/TR/BL/BR/C), ecc_pa_R (0=random dir .. 1=one
    direction), ecc_radial_frac (fraction aligned with the radial vector),
    and a shape label.
    """
    import numpy as np
    good = objs[(objs["a"] >= 0.6) & (objs["b"] > 0)]
    if len(good) < 20:
        return {}
    x, y, a, b, th = (good["x"], good["y"], good["a"], good["b"],
                      good["theta"])
    ecc = 1.0 - b / a
    zx = np.clip((x / W * 3).astype(int), 0, 2)
    zy = np.clip((y / H * 3).astype(int), 0, 2)

    def zmed(cx, cy):
        m = (zx == cx) & (zy == cy)
        return round(float(np.median(ecc[m])), 2) if m.sum() >= 5 else None
    corners = {"TL": zmed(0, 0), "TR": zmed(2, 0), "BL": zmed(0, 2),
               "BR": zmed(2, 2), "C": zmed(1, 1)}
    el = ecc > ecc_floor
    if int(el.sum()) < 15:
        return {"corner_ecc": corners, "ecc_pa_R": 0.0,
                "ecc_radial_frac": 0.0, "shape": "round"}
    thE = th[el]
    # axial (mod-pi) data: resultant length of 2*theta
    R = float(np.hypot(np.mean(np.cos(2 * thE)), np.mean(np.sin(2 * thE))))
    cx, cy = W / 2.0, H / 2.0
    rad = np.arctan2(y[el] - cy, x[el] - cx)
    d = np.abs(((thE - rad + np.pi / 2) % np.pi) - np.pi / 2)
    radial_frac = float((d < np.deg2rad(25)).mean())
    med_ecc = float(np.median(ecc))
    if med_ecc < 0.25:
        shape = "round"
    elif radial_frac > 0.5:
        shape = "radial (tilt/collimation/curvature)"
    elif R > 0.55:
        shape = "uniform PA (tracking/wind)"
    else:
        shape = "mixed"
    return {"corner_ecc": corners, "ecc_pa_R": round(R, 2),
            "ecc_radial_frac": round(radial_frac, 2), "shape": shape}


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
        # Exposure scoring (binned frame: mean of 2x2, so full well and mean
        # sky level are preserved; noise is halved -> x2 to unbinned-equiv)
        clipped_pct = round(float((data >= 65000).mean() * 100.0), 3)
        sat_stars_pct = None
        if len(objs):
            try:
                good_pk = objs[(objs["a"] >= 0.6) & (objs["b"] > 0)]
                if len(good_pk):
                    sat_stars_pct = round(float(
                        ((good_pk["peak"] + float(bkg.globalback)) >= 65000)
                        .mean() * 100.0), 1)
            except Exception:  # noqa: BLE001
                pass
        rn = max(float(getattr(config, "camera_read_noise_adu", 8.0)), 0.1)
        swamp = round((2.0 * float(bkg.globalrms) / rn) ** 2, 1)
        exposure = ("sat-stars" if (sat_stars_pct or 0) > 5.0
                    else "clipped" if clipped_pct > 0.05
                    else "under" if swamp < 3.0 else "ok")
        try:
            shape = _shape_diagnostics(objs, data.shape[1], data.shape[0])
        except Exception:  # noqa: BLE001
            shape = {}
        del data_sub, data, err
        return {"stars": nstars, "hfr": hfr, "ecc": ecc,
                "corner_ecc": shape.get("corner_ecc"),
                "ecc_pa_R": shape.get("ecc_pa_R"),
                "ecc_radial_frac": shape.get("ecc_radial_frac"),
                "shape": shape.get("shape"),
                "doubled_frac": round(doubled_frac, 2),
                "background": round(float(bkg.globalback), 1),
                "noise": round(float(bkg.globalrms), 2),
                "clipped_pct": clipped_pct, "sat_stars_pct": sat_stars_pct,
                "swamp": swamp, "exposure": exposure,
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
    clipped_pct = round(float((binned >= 65000).mean() * 100.0), 3)
    rn = max(float(getattr(config, "camera_read_noise_adu", 8.0)), 0.1)
    swamp = round((2.0 * noise / rn) ** 2, 1)
    exposure = ("clipped" if clipped_pct > 0.05
                else "under" if swamp < 3.0 else "ok")
    return {"stars": nstars, "hfr": None, "ecc": None,
            "background": round(background, 1), "noise": round(noise, 2),
            "clipped_pct": clipped_pct, "sat_stars_pct": None,
            "swamp": swamp, "exposure": exposure,
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


def _resolve_target(raw, filename: str, plan_names: list[str],
                    known=None) -> str:
    """OBJECT header / recorded name, else filename match, else the plan's
    only target. PS-78: the name is canonicalized first, so a container name
    ("Heart Nebula imaging (repeats while safe and up)_Container") resolves to
    its target and a structural loop ("OSC_LIGHT_LOOP_Container") counts as
    unknown. known: extra target names / projects to match (default: the
    night's plan names)."""
    t = canonical_target(raw, known if known is not None else plan_names)
    if t:
        return t
    fn = filename.lower()
    for name in plan_names:
        if name.lower().replace(" ", "_") in fn or name.lower() in fn:
            return name
    if len(plan_names) == 1:
        return plan_names[0]
    return "?"


def sensor_temp_reasons(ccd_temp, header_setpoint, config,
                        setpoint: float | None = None) -> list[str]:
    """Rejection reasons for a warm sub (empty list = temperature is fine).

    Judged against the CONFIGURED setpoint, never the header SET-TEMP: on
    2026-09-26 the camera was left with SET-TEMP=20, so 22-25°C subs looked
    'at setpoint' and passed into review. The header value is only reported.
    Two rules: more than sub_temp_over_setpoint_c above setpoint, or above the
    absolute ceiling sub_temp_max_c (default 10°C) whatever the setpoint."""
    try:
        if ccd_temp is None:
            return []
        t = float(ccd_temp)
    except (TypeError, ValueError):
        return []
    sp = float(setpoint if setpoint is not None
               else getattr(config, "camera_setpoint_c", 0.0))
    over = float(getattr(config, "sub_temp_over_setpoint_c", 5.0))
    ceiling = float(getattr(config, "sub_temp_max_c", 10.0))
    hdr_note = ""
    try:
        if header_setpoint is not None and abs(float(header_setpoint) - sp) > 1.0:
            hdr_note = f"; camera was set to {float(header_setpoint):.0f}C"
    except (TypeError, ValueError):
        pass
    if t > sp + over:
        return [f"sensor {t:.0f}C vs setpoint {sp:.0f}C (cooler failure{hdr_note})"]
    if t > ceiling:
        return [f"sensor {t:.0f}C above {ceiling:.0f}C limit{hdr_note}"]
    return []


def _fast_grade(path: Path, config, plan_names: list[str] | None = None,
                *, prewarm: tuple[str, str] | None = None) -> dict:
    """Per-sub metrics for backfill: sep on a 2x2-binned frame.

    prewarm=(date, rel_file): while the frame is already loaded, also render the
    runs-grid thumbnail (w=264) so the runs page never generates it on first
    view. Reuses the loaded array — just a stretch + resize + PNG write, so it
    costs a fraction of the grade and never re-opens the FITS. Best-effort: a
    thumbnail failure never blocks grading.
    """
    with _HEAVY:
        hdr, binned = _load_binned(path)
        m = _measure(binned, config)
        if prewarm is not None:
            try:
                p_date, p_rel = prewarm
                small = None
                for w in PREWARM_WIDTHS:
                    p_out = _thumb_out_path(config, p_date, p_rel, w, False)
                    if not p_out.exists():
                        if small is None:
                            small = _decimate(binned)
                        _stretch_and_save(small, p_out, w)
            except Exception as e:  # noqa: BLE001
                logger.debug("thumb pre-warm skipped for %s: %s", path.name, e)
        del binned
    gc.collect()
    reasons = []
    if m["stars"] < 5:
        reasons.append(f"only {m['stars']} stars")
    # Cooler-failure subs: sensor way above the setpoint means the frame is
    # dominated by dark current no matching dark can calibrate out
    reasons.extend(sensor_temp_reasons(hdr.get("CCD-TEMP"), hdr.get("SET-TEMP"),
                                       config))
    ecc_max = float(getattr(config, "quality_eccentricity_max", 0.6))
    if m["ecc"] is not None and m["ecc"] > ecc_max:
        reasons.append(f"elongated stars (ecc {m['ecc']} > {ecc_max:g})")
    if m.get("doubled_frac", 0) >= 0.25:
        reasons.append(f"tracking jump: {round(m['doubled_frac']*100)}% of "
                       "stars doubled at a consistent offset")
    # Defocus / false-detection guards (2026-07-09): a badly out-of-focus
    # frame reads as donuts — either a flood of false "stars" or a huge HFR.
    # Both mean the frame is junk regardless of the other metrics.
    star_max = int(getattr(config, "quality_star_max", 5000))
    if m["stars"] is not None and m["stars"] > star_max:
        reasons.append(f"{m['stars']} stars > {star_max} "
                       "(defocus/false detections)")
    hfr_abs_max = float(getattr(config, "quality_hfr_abs_max", 8.0))
    if m["hfr"] is not None and m["hfr"] > hfr_abs_max:
        reasons.append(f"HFR {m['hfr']} > {hfr_abs_max:g}px (out of focus)")
    # PS-71: roof closed / parked. This grader has no true FWHM (its
    # fwhm_arcsec is HFR x scale), so judge star size on HFR only.
    qa_flag = ""
    try:
        from photonscript.shared.qa_signatures import (parked_frame_verdict,
                                                       exposure_start)
        from photonscript.shared.safety_history import unsafe_windows
        _exp = float(hdr.get("EXPTIME", 0) or 0)
        _start = exposure_start(hdr.get("DATE-OBS"))
        _wins = None
        if _start is not None:
            _wins, _src = unsafe_windows(config, _start,
                                         _start + timedelta(seconds=_exp))
        _v = parked_frame_verdict(
            config, hfr_px=m["hfr"], fwhm_arcsec=None,
            background=m.get("background"), exp_s=_exp, stars=m["stars"],
            start_utc=_start, unsafe_windows=_wins,
            image_type=str(hdr.get("IMAGETYP", "LIGHT")))
        reasons.extend(_v.reasons)
        qa_flag = _v.flag
    except Exception as e:  # noqa: BLE001
        logger.debug("parked-frame QA skipped for %s: %s", path.name, e)
    passed = not reasons
    hfr = m["hfr"]
    return {
        "time": hdr.get("DATE-OBS", ""),
        "target": _resolve_target(hdr.get("OBJECT"), path.name,
                                  plan_names or []),
        "filter": (lambda f: {**{}, **getattr(config, "reverse_filter_map",
                    lambda: {})()}.get(f, f))(hdr.get("FILTER", "?")),
        "exp_s": float(hdr.get("EXPTIME", 0)),
        "ccd_temp": hdr.get("CCD-TEMP"),
        "hfr": hfr,
        "fwhm_arcsec": round(hfr * config.pixel_scale_arcsec, 2) if hfr else None,
        "stars": m["stars"], "ecc": m["ecc"],
        "background": m["background"],
        "corner_ecc": m.get("corner_ecc"),
        "ecc_pa_R": m.get("ecc_pa_R"),
        "ecc_radial_frac": m.get("ecc_radial_frac"),
        "shape": m.get("shape"),
        "doubled_frac": m.get("doubled_frac"),
        "clipped_pct": m.get("clipped_pct"),
        "sat_stars_pct": m.get("sat_stars_pct"),
        "swamp": m.get("swamp"), "exposure": m.get("exposure"),
        "passed_qa": passed,
        "reason": "; ".join(reasons),
        "qa_flag": qa_flag,
        "graded_by": m["graded_by"],
    }


_backfill_state: dict[str, dict] = {}


_regrade_all: dict = {"running": False}


def regrade_all_status() -> dict:
    return dict(_regrade_all)


def running_jobs() -> list[str]:
    """Background jobs that write grades (PS-58: POST /api/update waits for
    them; a restart mid-job would cut a _subs.jsonl append). Thumbnail
    prewarm is not listed: thumbnails are written atomically (PS-59) and a
    restart only costs the rest of the warm-up."""
    jobs = []
    if _regrade_all.get("running"):
        jobs.append(f"re-grade all ({_regrade_all.get('done', 0)} of "
                    f"{_regrade_all.get('total', '?')} nights)")
    for date, st in list(_backfill_state.items()):
        if st.get("running"):
            jobs.append(f"grading {date}")
    return jobs


def start_regrade_all(config, since: str = "") -> dict:
    """Sequentially wipe + re-grade every night folder (>= since), one night
    at a time so memory stays flat. Each night's backfill also re-runs
    auto-identify and rebuilds its Library entries, so a metadata/grader fix
    propagates everywhere with one click. NOTE: deletes manual review
    verdicts for the affected nights - use 'since' to protect reviewed
    history."""
    import re as _re
    import threading
    import time

    if _regrade_all.get("running"):
        return dict(_regrade_all)
    root = Path(config.image_watch_dir)
    dates = sorted(d.name for d in root.iterdir()
                   if d.is_dir() and _re.match(r"^\d{4}-\d{2}-\d{2}$", d.name)
                   and d.name >= (since or ""))
    _regrade_all.update(running=True, total=len(dates), done=0,
                        current=None, since=since, last_error=None)
    logger.info("Re-grade ALL: %d nights (since %s)", len(dates),
                since or "beginning")

    def _work():
        try:
            for d in dates:
                _regrade_all["current"] = d
                try:
                    p = runs_dir(config) / f"{d}_subs.jsonl"
                    if p.exists():
                        p.unlink()
                    th = Path(config.data_dir) / "thumbs" / d
                    if th.exists():
                        for f in th.glob("*.ann.png"):
                            f.unlink(missing_ok=True)
                    start_backfill(config, d)
                    while _backfill_state.get(d, {}).get("running"):
                        time.sleep(2)
                except Exception as e:  # noqa: BLE001
                    _regrade_all["last_error"] = f"{d}: {e}"
                    logger.warning("Re-grade all: night %s failed: %s", d, e)
                _regrade_all["done"] += 1
            try:
                sync_goal_progress(config)
            except Exception:  # noqa: BLE001
                pass
            logger.info("Re-grade ALL finished: %d nights",
                        _regrade_all["done"])
        finally:
            _regrade_all["running"] = False
            _regrade_all["current"] = None

    threading.Thread(target=_work, daemon=True, name="regrade-all").start()
    return dict(_regrade_all)


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
                    record = _fast_grade(f, config, plan_names,
                                         prewarm=(date, rel))
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
            try:  # attribute '?' subs: header RA/DEC, piggyback time
                # correlation, then ASTAP for whatever is still unknown (PS-51)
                ra = attribute_night(config, date, solve=True)
                if ra.get("attributed"):
                    sync_goal_progress(config)
            except Exception as e:  # noqa: BLE001
                logger.warning("Auto-attribute failed for %s: %s", date, e)
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
            try:  # harvest measured FOCPOS/FOCTEMP from tonight's sharp subs so
                # the RC16 focus-seed table self-improves (was never called —
                # the whole temperature model sat dead until now).
                from photonscript.scheduler.focus_seeds import harvest_night
                n_seed = harvest_night(config, date)
                if n_seed:
                    logger.info("Focus-seed harvest %s: %d filter-records added",
                                date, n_seed)
            except Exception as e:  # noqa: BLE001
                logger.warning("Focus-seed harvest failed for %s: %s", date, e)
            try:  # cross-night trend/drift alarm — catch systematic rig faults
                # (polar drift, tilt, soft focus) that per-frame QA can't see.
                from photonscript.scheduler.trends import check_and_alert
                tr = check_and_alert(config)
                if tr.get("findings"):
                    logger.warning("Trend alarm %s: %s", date,
                                   [f["kind"] for f in tr["findings"]])
            except Exception as e:  # noqa: BLE001
                logger.warning("Trend check failed for %s: %s", date, e)
            try:  # autofocus-quality alert — a failed/soft AF (few stars through
                # narrowband) degrades every following sub; catch it from NINA's
                # AF reports instead of only via passive per-frame QA.
                from photonscript.scheduler.focus_reports import (
                    check_and_alert as af_check)
                af = af_check(config, date)
                if af.get("bad"):
                    logger.warning("AF-quality alarm %s: %s bad run(s)",
                                   date, len(af["bad"]))
            except Exception as e:  # noqa: BLE001
                logger.warning("AF-quality check failed for %s: %s", date, e)
            try:  # PS-76: learn the RC16 focus model (temperature slope +
                # measured filter offsets) from NINA's AF reports. No-op until
                # nina_autofocus_reports_dir is set.
                from photonscript.scheduler.focus_model import ingest_af_reports
                fm = ingest_af_reports(config)
                if fm.get("added"):
                    logger.info("Focus model %s: %d AF point(s) added (%d "
                                "stored)", date, fm["added"], fm["total"])
            except Exception as e:  # noqa: BLE001
                logger.warning("Focus-model ingest failed for %s: %s", date, e)
        finally:
            st["running"] = False
            st["current"] = None

    threading.Thread(target=_work, daemon=True,
                     name=f"backfill-{date}").start()


def attribute_night(config, date: str, solve: bool = False) -> dict:
    """PS-51: give every '?' sub of a night a campaign target.

    Order matters: (1) per-sub header RA/DEC match (RC16 frames carry the
    mount position, so this is exact and cheap); (2) Piggy-600 subs inherit the
    RC16 target by capture time (they carry no coordinates, and (1) must run
    first so the RC16 timeline has names to lend); (3) only with solve=True,
    one ASTAP plate solve per time cluster for anything still unknown. The
    library build runs (1)+(2) on every night it touches; the dawn backfill
    runs all three. Idempotent: only '?' subs are ever touched."""
    from photonscript.scheduler.identify import identify_night
    out = {"date": date, "header": 0, "piggyback": 0, "solved": 0}
    try:
        out["header"] = identify_night(config, date, solve=False).get(
            "identified", 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("Header attribution failed for %s: %s", date, e)
    try:
        out["piggyback"] = correlate_piggyback_targets(config, date).get(
            "attributed", 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("Piggyback correlate failed for %s: %s", date, e)
    if solve:
        try:
            out["solved"] = identify_night(config, date, solve=True).get(
                "identified", 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("Plate-solve attribution failed for %s: %s", date, e)
    out["attributed"] = out["header"] + out["piggyback"] + out["solved"]
    if out["attributed"]:
        logger.info("Attribute %s: %s", date, out)
    return out


def _rewrite_subs(config, date: str, records: list[dict]) -> None:
    p = runs_dir(config) / f"{date}_subs.jsonl"
    p.write_text("".join(json.dumps(_sanitize_floats(r)) + "\n"
                         for r in records),
                 encoding="utf-8")
    # A same-size rewrite inside one coarse NTFS mtime tick would keep the
    # cache signature, so drop the entry explicitly.
    _invalidate_subs_cache(p)


# Piggyback subs captured within this margin of an RC16 exposure inherit its
# target; frames further out (a slew/flip gap, or after the RC16 stopped) stay
# '?'. Was 45 min, measured from the RC16's last sub START, which on 2026-09-20
# handed an hour of M31 frames to Crescent after the mount moved on (PS-51):
# an unknown sub is recoverable, a wrong one silently pollutes a stack.
PIGGYBACK_CORRELATE_TOL_MIN = 10.0


def correlate_piggyback_records(subs: list[dict]) -> tuple[int, dict, list]:
    """In-memory core of correlate_piggyback_targets (no file writes).

    Sets s["target"] on each unattributed piggyback sub inside an RC16
    exposure window. PS-78: RC16 names are canonicalized before they are
    lent (a container-named RC16 sub lends its real target), and a piggyback
    sub named after its OSC loop container counts as unattributed. Returns
    (attributed, {target: n}, pending subs)."""
    import bisect

    def _t(s):
        try:
            return datetime.fromisoformat(str(s.get("time", ""))[:19])
        except ValueError:
            return None

    rc16 = sorted(
        ((_t(s), canonical_target(s.get("target")),
          float(s.get("exp_s") or 0.0)) for s in subs
         if s.get("rig", "rc16") == "rc16"
         and canonical_target(s.get("target")) and _t(s) is not None),
        key=lambda x: x[0])
    pending = [s for s in subs
               if s.get("rig") not in ("rc16", None, "")
               and canonical_target(s.get("target")) is None
               and _t(s) is not None]
    if not rc16 or not pending:
        return 0, {}, []

    starts = [r[0] for r in rc16]
    tol = timedelta(minutes=PIGGYBACK_CORRELATE_TOL_MIN)
    n, windows = 0, {}
    for s in pending:
        ts = _t(s)
        i = bisect.bisect_right(starts, ts) - 1
        name = None
        if i >= 0:
            # inherit rc16[i] only while its exposure (plus a short margin for
            # dither/filter/AF gaps) covers ts; a longer gap means the mount
            # may be somewhere else, and the next RC16 sub may be a new target
            if ts <= rc16[i][0] + timedelta(seconds=rc16[i][2]) + tol:
                name = rc16[i][1]
        elif starts[0] - ts <= tol:
            # piggyback opened a little before the RC16's first sub
            name = rc16[0][1]
        if name:
            if s.get("target") not in ("?", "", None) \
                    and not s.get("target_raw"):
                s["target_raw"] = s.get("target")
            s["target"] = name
            n += 1
            windows[name] = windows.get(name, 0) + 1
    return n, windows, pending


def correlate_piggyback_targets(config, date: str) -> dict:
    """Attribute piggyback (2nd-rig) subs by time-correlation to the RC16.

    The piggyback rides the RC16 mount with no target/OBJECT of its own, so
    every OSC sub lands as target='?' (identify's coordinate match can't help
    it — NINA #2 owns no mount, so the frames carry no RA/DEC). But the RC16's
    own sub timeline says exactly what the mount was pointed at over the night;
    a piggyback sub inherits the RC16 target whose imaging window covers its
    capture time. Both rigs share one {date}_subs.jsonl, so this is a cheap
    metadata pass (no FITS reopened). Idempotent: only touches unattributed
    piggyback subs, so re-running after more RC16 frames land fills in more.
    """
    subs = _load_subs(config, date)
    n, windows, pending = correlate_piggyback_records(subs)
    if n:
        _rewrite_subs(config, date, subs)
        # best-effort: stamp the OSC FITS OBJECT so the science file carries it
        if getattr(config, "stamp_fits_object", True):
            for s in pending:
                if s.get("target") not in ("?", "", None) and s.get("abs_path"):
                    try:
                        from photonscript.shared.fits_object import stamp_object
                        stamp_object(s["abs_path"], s["target"])
                    except Exception as e:  # noqa: BLE001
                        logger.debug("piggyback OBJECT stamp skipped: %s", e)
    logger.info("Piggyback correlate %s: %d subs attributed %s",
                date, n, windows)
    return {"attributed": n, "windows": windows}


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


def reset_library(config) -> dict:
    """Delete the library and rebuild it under the review gate — used to
    un-queue bulk transfers created before review-first existed. Removes
    only hardlinks/copies inside library_root; originals are never touched."""
    import shutil
    lib = library_root(config).resolve()
    watch = Path(config.image_watch_dir).resolve()
    # refuse anything that could touch originals
    if watch == lib or str(watch).lower().startswith(str(lib).lower()):
        raise RuntimeError(f"refusing: image_watch_dir {watch} is inside "
                           f"library_root {lib}")
    removed = 0
    if lib.exists():
        removed = sum(1 for _ in lib.rglob("*.fits"))
        shutil.rmtree(lib)
    res = build_library(config)  # honors the review gate
    logger.warning("Library reset: %d files removed, rebuilt with %d "
                   "reviewed links (%d await review)", removed,
                   res.get("linked", 0), res.get("pending_review", 0))
    return {"removed": removed, **res}


def sync_goal_progress(config) -> list:
    """Rebuild per-filter accepted counts in the goal store from every
    night's records. Called automatically after approvals, manual
    verdicts, and identification — the goal bars follow the evidence."""
    try:
        from photonscript.scheduler.app import get_store
        store = get_store()
    except Exception:  # noqa: BLE001
        return []
    # (target key, filter) -> list of accepted sub lengths (s); lengths let an
    # HDR plan split its short companion subs from the long set. PS-78: keyed
    # on the canonical target, so container-named subs count toward the goal.
    known = known_target_index(list(store.projects.values()))
    lengths: dict[tuple, list] = {}
    for f in runs_dir(config).glob("*_subs.jsonl"):
        date = f.name.split("_")[0]
        for s_ in _load_subs(config, date):
            if not s_.get("passed_qa"):
                continue
            t = canonical_target(s_.get("target"), known)
            if t:
                lengths.setdefault((target_key(t), s_.get("filter")),
                                   []).append(s_.get("exp_s"))
    changed = []
    for p in store.projects.values():
        tname = target_key(p.target.name)
        touched = False
        for e in p.exposure_plans:
            subs = lengths.get((tname, e.filter_type.value), [])
            short = sum(1 for x in subs if e.is_short_exposure(x))
            n = min(len(subs) - short, e.count)
            ns = min(short, e.hdr_short_count)
            if n != e.acquired or ns != e.hdr_short_acquired:
                e.acquired = n
                e.hdr_short_acquired = ns
                touched = True
        if touched:
            changed.append(p.target.name)
    if changed:
        store.save()
        logger.info("Goal progress synced from history: %s", changed)
    return changed


def approve_night(config, date: str, files: list[str] | None = None) -> dict:
    """Mark every QA-passing sub as reviewed, then update the library so
    they queue for transfer. The human gate between capture and sync.

    files: approve only these subs (the Runs page passes the subs currently
    shown by its rig/target/filter chips, e.g. just Cat's Eye OIII). None =
    the whole night. Rejected subs are never approved by this path."""
    subs = _load_subs(config, date)
    only = set(files) if files is not None else None
    n = 0
    for s_ in subs:
        if only is not None and s_.get("file") not in only:
            continue
        if s_.get("passed_qa") and not s_.get("reviewed"):
            s_["reviewed"] = True
            n += 1
    if n:
        _rewrite_subs(config, date, subs)
    res = build_library(config, date)
    logger.info("Night %s approved: %d subs -> library (%s new links)",
                date, n, res.get("linked"))
    sync_goal_progress(config)
    return {"approved": n, **res}


def set_manual_qa(config, date: str, rel_file: str,
                  passed: bool | None = None,
                  state: str | None = None) -> dict | None:
    """Human verdict for one sub: 'accepted' | 'rejected' | 'review'.

    'review' hands the sub back to the automatic pipeline (pass, not yet
    reviewed). Legacy bool `passed` maps to accepted/rejected.
    """
    if state is None:
        state = "accepted" if passed else "rejected"
    subs = _load_subs(config, date)
    hit = None
    for s_ in subs:
        if s_.get("file") == rel_file:
            if state == "accepted":
                s_.update(passed_qa=True, reviewed=True, manual_qa=True,
                          reason="")
            elif state == "rejected":
                s_.update(passed_qa=False, reviewed=True, manual_qa=True,
                          reason="rejected manually")
            else:  # review
                s_.update(passed_qa=True, reviewed=False, manual_qa=False,
                          reason="")
            hit = s_
    if hit is None:
        return None
    _rewrite_subs(config, date, subs)
    try:  # keep the library consistent with the verdict
        if state == "accepted":
            build_library(config, date)
        else:
            name = Path(hit.get("abs_path") or "").name
            if name:
                for f in library_root(config).rglob(name):
                    f.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("library update after manual QA failed: %s", e)
    sync_goal_progress(config)
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


def night_score(dark_h: float, light_h: float, accepted_h: float,
                completion_pct: float) -> dict:
    """The honest funnel: dark hours -> shutter hours -> accepted hours.

    sky_utilization = accepted/dark (photons that survived review over
    photons the night offered), keep_rate = accepted/shutter.
    """
    util = accepted_h / dark_h * 100 if dark_h else 0
    keep = accepted_h / light_h * 100 if light_h else 0
    parts = {
        "sky_utilization": (0.40, min(util, 100)),
        "keep_rate": (0.30, min(keep, 100)),
        "plan_completion": (0.30, min(completion_pct, 100)),
    }
    total = sum(w * v for w, v in parts.values())
    return {"total": round(total),
            "breakdown": {k: {"weight": w, "value": round(v)}
                          for k, (w, v) in parts.items()}}


def _archived_path(config) -> Path:
    return Path(config.data_dir) / "archived_nights.json"


def load_archived(config) -> set:
    """Set of dates the user has archived out of the runs sidenav."""
    p = _archived_path(config)
    try:
        return set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() \
            else set()
    except Exception:  # noqa: BLE001
        return set()


def _save_archived(config, dates: set) -> dict:
    p = _archived_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(dates)), encoding="utf-8")
    return {"archived": sorted(dates), "count": len(dates)}


def set_archived(config, date: str, archived: bool) -> dict:
    """Archive / unarchive a single night."""
    cur = load_archived(config)
    cur.add(date) if archived else cur.discard(date)
    return _save_archived(config, cur)


def archive_before(config, cutoff: str) -> dict:
    """Archive every run whose date is strictly before cutoff (YYYY-MM-DD).
    Archiving only hides nights from the sidenav — it never deletes data."""
    cur = load_archived(config)
    for r in list_runs(config):
        if r["date"] < cutoff:
            cur.add(r["date"])
    return _save_archived(config, cur)


def prunable_night_dirs(config, before: str) -> list[dict]:
    """Night folders (YYYY-MM-DD) strictly before `before`, across the RC16
    capture dir, the piggyback watch dir, and the thumbnail cache — the heavy
    pixels safe to delete once archived. Reads live config paths (no hardcoding).

    NEVER includes the grade records (runs_dir *_subs.jsonl / *_plan.json) or
    the contact sheets — those are the durable per-sub learnings and stay.
    Used by `photonscript prune-nights`.
    """
    roots = []
    for base in (getattr(config, "image_watch_dir", ""),
                 getattr(config, "piggyback_image_watch_dir", "")):
        if base:
            roots.append(Path(base))
    roots.append(Path(config.data_dir) / "thumbs")
    seen: set = set()
    out: list[dict] = []
    for root in roots:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if (d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}$", d.name)
                    and d.name < before):
                key = str(d.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append({"date": d.name, "root": str(root),
                            "path": str(d)})
    return sorted(out, key=lambda x: (x["date"], x["path"]))


_fits_count_cache: dict = {}  # night root -> (computed_at, (lights, cal))
_FITS_COUNT_TTL_RECENT = 30.0    # tonight / last night: frames still landing
_FITS_COUNT_TTL_OLD = 900.0      # older nights only change on prune/archive


def _night_fits_counts(night_root: Path, date: str) -> tuple[int, int]:
    """(lights, calibration) FITS counts for one night folder, TTL-cached.

    /api/runs used to rglob EVERY night's folder on every poll (tens of
    seconds on the scope PC once the archive grew). Recent nights refresh
    every 30 s; older ones every 15 min (or immediately via
    ``invalidate_fits_counts`` after a prune)."""
    import time as _time
    key = str(night_root)
    now = _time.monotonic()
    try:
        age_days = (datetime.utcnow().date()
                    - datetime.strptime(date, "%Y-%m-%d").date()).days
    except ValueError:
        age_days = 0
    ttl = _FITS_COUNT_TTL_RECENT if age_days <= 2 else _FITS_COUNT_TTL_OLD
    hit = _fits_count_cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    n_lights = n_cal = 0
    if night_root.exists():
        for f in night_root.rglob("*.fits"):
            if _is_calibration(f.relative_to(night_root).parts):
                n_cal += 1
            else:
                n_lights += 1
    _fits_count_cache[key] = (now, (n_lights, n_cal))
    return n_lights, n_cal


def invalidate_fits_counts(date: str | None = None) -> None:
    """Drop cached FITS counts (one night, or all) after files move/delete."""
    if date is None:
        _fits_count_cache.clear()
        return
    for k in [k for k in _fits_count_cache if Path(k).name == date]:
        _fits_count_cache.pop(k, None)


def _note_raw_targets(subs: list[dict]) -> None:
    """PS-78: keep what a sub was called (target_raw) when its recorded name
    is a container name that resolves to something else."""
    for s_ in subs:
        raw = s_.get("target")
        if raw in (None, "", "?") or s_.get("target_raw"):
            continue
        if canonical_target(raw) != raw:
            s_["target_raw"] = raw


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
    archived = load_archived(config)
    out = []
    for d in sorted(dates, reverse=True):
        subs = _load_subs(config, d)
        n_lights, n_cal = _night_fits_counts(fits_root / d, d)
        out.append({"date": d, "subs_logged": len(subs),
                    "lights": n_lights, "cal_frames": n_cal,
                    "has_plan": (runs_dir(config) / f"{d}_plan.json").exists(),
                    "archived": d in archived})
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


_night_extras_cache: dict = {}  # date -> {"key": (...), "cal": ..., "report": ...}


def _cached_night_extras(config, date: str, subs_count: int):
    """calibration_inventory() + build_daily_report() both rglob + read FITS
    headers for the whole night — slow on the RAM-tight scope PC and the reason
    /api/runs/{date} appeared to hang under the runs-page poll. Cache them per
    night, keyed on the sub count + the night folder's mtime, so repeat views
    are free and only a genuinely-changed night recomputes."""
    from photonscript.scheduler.daily_report import build_daily_report
    root = Path(config.image_watch_dir) / date
    try:
        mtime = round(root.stat().st_mtime) if root.exists() else 0
    except Exception:  # noqa: BLE001
        mtime = 0
    key = (subs_count, mtime)
    ent = _night_extras_cache.get(date)
    if ent and ent["key"] == key:
        return ent["cal"], ent["report"]
    cal = calibration_inventory(config, date)
    report = build_daily_report(config, date)
    _night_extras_cache[date] = {"key": key, "cal": cal, "report": report}
    return cal, report


def night_detail(config, date: str, backfill: bool = True) -> dict:
    """Full plan-vs-actual record for one night."""

    plan_path = runs_dir(config) / f"{date}_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) \
        if plan_path.exists() else None

    status = backfill_status(config, date)
    if backfill and status["pending"] and not status["running"]:
        start_backfill(config, date)
        status = backfill_status(config, date)
    subs = _load_subs(config, date)
    _note_raw_targets(subs)  # PS-78

    # Fix up subs recorded before target attribution existed
    plan_names = _plan_target_names(config, date)
    for s_ in subs:
        s_["target"] = _resolve_target(s_.get("target"),
                                       s_.get("file", ""), plan_names)

    # Plan vs actual per rig/target/filter (rig separates the two scopes)
    planned: dict[tuple, int] = {}
    if plan:
        for t in plan["targets"]:
            for e in t["exposures"]:
                planned[("rc16", t["name"], e["filter"])] = \
                    planned.get(("rc16", t["name"], e["filter"]), 0) + e["planned"]
    actual: dict[tuple, dict] = {}
    for s in subs:
        key = (s.get("rig", "rc16"), s.get("target", "?"), s.get("filter", "?"))
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
            "rig": key[0], "target": key[1], "filter": key[2],
            "planned": planned.get(key, 0),
            "attempted": a.get("attempted", 0),
            "accepted": a.get("accepted", 0),
            "median_hfr": med(a.get("hfrs", [])),
            "median_ecc": med(a.get("eccs", [])),
            "median_background": med(a.get("bgs", [])),
        })

    cal_tonight, report = _cached_night_extras(config, date, len(subs))
    for typ, g in sorted(cal_tonight.get("frames", {}).items()):
        exps = ", ".join(f"{k}×{v}" for k, v in
                         sorted(g.get("exposures", {}).items()))
        table.append({"rig": "rc16", "target": f"Calibration · {typ}",
                      "filter": exps or "—",
                      "planned": 0, "attempted": g["count"],
                      "accepted": g["count"], "median_hfr": None,
                      "median_ecc": None, "median_background": None})

    accepted = sum(1 for s in subs if s.get("passed_qa"))
    total_planned = sum(planned.values())
    # the honest funnel
    from photonscript.shared.astronomy import get_twilight_times
    try:
        base = datetime.strptime(date, "%Y-%m-%d")
        tw = get_twilight_times(config.get_observatory(), base)
        dark_h = ((tw["astro_dark_end"] - tw["astro_dark_start"])
                  .total_seconds() / 3600
                  if tw.get("astro_dark_start") else 0.0)
    except Exception:  # noqa: BLE001
        dark_h = 0.0
    light_h = sum(s.get("exp_s") or 0 for s in subs) / 3600
    accepted_h = sum(s.get("exp_s") or 0 for s in subs
                     if s.get("passed_qa")) / 3600
    score = night_score(
        dark_h, light_h, accepted_h,
        (accepted / total_planned * 100) if total_planned else
        (100 if accepted else 0),
    )

    # Per-rig score (the two scopes are graded independently): same dark window,
    # but each rig's own shutter/accepted hours and plan completion. Only emitted
    # when more than one rig has subs, so single-rig nights are unchanged.
    scores_by_rig: dict[str, dict] = {}
    rigs_present = sorted({s.get("rig", "rc16") for s in subs})
    if len(rigs_present) > 1:
        for rg in rigs_present:
            rsubs = [s for s in subs if s.get("rig", "rc16") == rg]
            r_light = sum(s.get("exp_s") or 0 for s in rsubs) / 3600
            r_acc_h = sum(s.get("exp_s") or 0 for s in rsubs
                          if s.get("passed_qa")) / 3600
            r_accepted = sum(1 for s in rsubs if s.get("passed_qa"))
            r_planned = sum(v for k, v in planned.items() if k[0] == rg)
            r_comp = (r_accepted / r_planned * 100) if r_planned else (
                100 if r_accepted else 0)
            scores_by_rig[rg] = night_score(dark_h, r_light, r_acc_h, r_comp)

    return {
        "date": date,
        "plan": plan,
        "report": {
            "dark_hours": round(dark_h, 1),
            "light_hours": round(light_h, 1),
            "accepted_hours": round(accepted_h, 1),
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
        "scores_by_rig": scores_by_rig,
        "backfill": status,
    }


# --- Librarian: integration-ready folder tree ----------------------------------

def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", str(name)).strip() or "Unknown"


def library_root(config) -> Path:
    d = getattr(config, "library_dir", "") or ""
    return Path(d) if d else Path(config.data_dir) / "Library"


def library_target_dirs(lib: Path, name: str, known=None) -> list[Path]:
    """PS-78: every top-level Library folder holding lights of target `name`:
    the canonical folder first, then any folder named after one of its
    containers (e.g. "Cat's Eye Nebula imaging (repeats while safe and
    up)_Container") until the rename backfill merges them."""
    canon = canonical_target(name, known) or name
    main = lib / _safe_name(canon)
    out = [main]
    if not lib.exists():
        return out
    key = target_key(canon)
    try:
        dirs = sorted(x for x in lib.iterdir() if x.is_dir())
    except OSError:
        return out
    for d in dirs:
        if d.name == main.name or d.name.startswith("_"):
            continue
        c = canonical_target(d.name, known)
        if c and target_key(c) == key:
            out.append(d)
    return out


def analysis_dropbox(config) -> Path:
    """Subfolder of the library Syncthing share where individual FITS are
    dropped for off-scope analysis (mirrors to the desktop via Syncthing)."""
    sub = getattr(config, "analysis_dropbox_subdir", "_analysis") or "_analysis"
    return library_root(config) / sub


def stage_for_analysis(config, date: str, files=None, which: str = "") -> dict:
    """Copy selected subs' FITS into the library Syncthing share so they
    replicate to the desktop for analysis (raw-FITS access off the scope PC).

    Select either by explicit `files` (the rel 'file' values from the run) or
    by QA state via which='rejected'|'accepted'|'all'. build_library only ever
    touches <Target>/<Filter>/ and Calibration/, so the _analysis subfolder is
    left alone (a manual Library reset would clear this scratch space).
    """
    import shutil

    subs = _load_subs(config, date)
    by_file = {s.get("file"): s for s in subs}
    picked: list[dict] = []
    if files:
        picked = [by_file[f] for f in files if f in by_file]
    elif which:
        for s in subs:
            st = "accepted" if s.get("passed_qa") else "rejected"
            if which == "all" or which == st:
                picked.append(s)

    dropbox = analysis_dropbox(config) / date
    dropbox.mkdir(parents=True, exist_ok=True)
    desktop_sub = getattr(config, "analysis_dropbox_subdir", "_analysis")
    desktop_base = getattr(config, "desktop_library_dir", "") or ""

    out = []
    for s in picked:
        src = Path(s.get("abs_path") or "")
        if not src.is_file():
            out.append({"file": s.get("file"), "ok": False,
                        "error": "source not found"})
            continue
        dest = dropbox / src.name
        try:
            if not dest.exists():
                shutil.copy2(src, dest)
            entry = {"file": s.get("file"), "ok": True,
                     "name": src.name, "bytes": dest.stat().st_size,
                     "dropbox_path": str(dest)}
            if desktop_base:
                # desktop_library_dir is a Windows path and the scheduler may
                # run off-Windows, so build it as a Windows path explicitly.
                from pathlib import PureWindowsPath
                entry["desktop_path"] = str(
                    PureWindowsPath(desktop_base) / desktop_sub / date
                    / src.name)
            out.append(entry)
        except Exception as e:  # noqa: BLE001
            out.append({"file": s.get("file"), "ok": False, "error": str(e)})

    ok = sum(1 for o in out if o.get("ok"))
    logger.info("stage_for_analysis %s: %d/%d copied to %s",
                date, ok, len(picked), dropbox)
    return {"date": date, "dropbox": str(dropbox), "requested": len(picked),
            "copied": ok, "files": out}


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
    linked = skipped = missing = rejected = pending_review = archived = 0
    attributed = retagged = 0
    # Target folders a sub could have been linked under before it was (re)named:
    # Library/_/ ('?' is not a legal path char) or another target after a
    # corrected attribution. Calibration, piggyback cal and the analysis
    # dropbox are never target folders.
    _not_targets = {"calibration", "piggyback", "_rejected",
                    str(getattr(config, "analysis_dropbox_subdir", "_analysis")
                        or "_analysis").lower()}
    target_dirs = ([x for x in lib.iterdir()
                    if x.is_dir() and x.name.lower() not in _not_targets]
                   if lib.exists() else [])
    from photonscript.scheduler.library_archive import archived_kinds
    for d in dates:
        # PS-51: name every '?' sub before linking, so lights land under their
        # campaign instead of Library/_/ (header match + piggyback correlation;
        # no plate solves here, that is the dawn backfill's job).
        if getattr(config, "library_attribute", True):
            attributed += attribute_night(config, d).get("attributed", 0)
        plan_names = _plan_target_names(config, d)
        gone = archived_kinds(config, d)
        for s_ in ([] if "lights" in gone else _load_subs(config, d)):
            if not s_.get("passed_qa"):
                rejected += 1
                continue
            if review_gate and not s_.get("reviewed"):
                pending_review += 1
                continue
            abs_path = s_.get("abs_path")
            src = Path(abs_path) if abs_path else None
            if src is None or not src.is_file():
                missing += 1
                continue
            target = _safe_name(_resolve_target(
                s_.get("target"), s_.get("file", ""), plan_names))
            fdir = _safe_name(s_.get("filter", "?"))
            dest = lib / target / fdir / src.name
            # PS-51: a sub linked under Library/_/ (or under the wrong target
            # before a correction) moves to its campaign folder: a rename, so
            # Syncthing ships a move, not a second copy. Only library links
            # are touched, never the originals.
            moved = False
            for tdir in target_dirs:
                if tdir.name == target:
                    continue
                stale = tdir / fdir / src.name
                if not stale.exists():
                    continue
                try:
                    if dest.exists():
                        stale.unlink()
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(stale, dest)
                    retagged += 1
                    moved = True
                except OSError as e:
                    logger.warning("Library retag %s -> %s failed: %s",
                                   stale, dest, e)
            if moved:
                continue
            if dest.exists():
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dest)
            except OSError:  # cross-volume or FS without hardlinks
                shutil.copy2(src, dest)
            linked += 1
        # Calibration: only recent sessions — old darks/flats rarely match
        # current gain/offset/exposures and were flooding the transfer queue
        if "lights" in gone:
            archived += 1
        n_l, n_s = _link_calibration_night(config, Path(config.image_watch_dir),
                                           lib, d)
        linked += n_l
        skipped += n_s
    result = {"library": str(lib), "nights": len(dates), "linked": linked,
              "archived_nights_skipped": archived,
              "already_there": skipped, "rejected_excluded": rejected,
              "pending_review": pending_review, "missing_files": missing,
              "attributed": attributed, "retagged": retagged}
    pb = _build_piggyback_calibration(config, date)
    if pb is not None:
        result["piggyback_calibration"] = pb
    logger.info("Library update: %s", result)
    return result


def _link_calibration_night(config, watch_dir: Path, lib: Path,
                            d: str) -> tuple[int, int]:
    """Hardlink (else copy) one night's BIAS/DARK/FLAT frames from a NINA
    output dir into <lib>/Calibration/<TYPE>/<date>/. Returns (linked,
    already_there). Nights older than library_cal_days are skipped."""
    import os
    import shutil
    from datetime import datetime as _dt
    cal_days = int(getattr(config, "library_cal_days", 120))
    try:
        night_age = (_dt.now() - _dt.strptime(d, "%Y-%m-%d")).days
    except ValueError:
        night_age = 0
    root = Path(watch_dir) / d
    linked = skipped = 0
    if not root.exists() or night_age > cal_days:
        return 0, 0
    from photonscript.scheduler.library_archive import archived_kinds
    gone = archived_kinds(config, d)  # archived calibration stays archived
    for f in root.rglob("*.fits"):
        parts = f.relative_to(root).parts
        if not _is_calibration(parts):
            continue
        typ = next(({"BIAS": "BIAS"}.get(p.upper(), p.upper().rstrip("S")) for p in parts
                    if p.upper() in _CAL_DIRS), "CAL")
        if typ in gone:
            continue
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
    return linked, skipped


def _build_piggyback_calibration(config, date: str | None = None) -> dict | None:
    """File NINA #2's (Piggy-600) calibration into the piggyback library
    subtree (<library>/piggyback/Calibration/...), which Syncthing carries to
    the desktop where prepare-integration-osc.ps1 reads it.

    Before this (PS-36), build_library only scanned the RC16's watch dir, so 69
    AP26CC darks, 50 bias and 10 flats sat on the scope PC under
    Documents/NINA-Piggyback and every OSC stack ran uncalibrated. Nights come
    from the piggyback watch dir itself (calibration-only nights have no subs
    file). Returns None when the piggyback is disabled or has no watch dir."""
    from photonscript.shared.rigs import PIGGYBACK, rig_config, rig_ids
    if PIGGYBACK not in rig_ids(config):
        return None
    if not getattr(config, "piggyback_image_watch_dir", ""):
        return None
    pcfg = rig_config(config, PIGGYBACK)
    watch = Path(pcfg.image_watch_dir)
    if not watch.exists():
        return {"error": f"piggyback watch dir not found: {watch}"}
    lib = library_root(pcfg)
    if date:
        nights = [date]
    else:
        nights = sorted(p.name for p in watch.iterdir()
                        if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))
    linked = skipped = 0
    for d in nights:
        n_l, n_s = _link_calibration_night(pcfg, watch, lib, d)
        linked += n_l
        skipped += n_s
    return {"library": str(lib), "nights": len(nights), "linked": linked,
            "already_there": skipped}


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
                ({"BIAS": "BIAS"}.get(p.upper(), p.upper().rstrip("S")) for p in parts
                 if p.upper() in _CAL_DIRS), "CAL")
            typ = typ.replace(" FRAME", "").replace("LIGHT", "CAL")
            g = frames.setdefault(typ, {"count": 0, "filters": {},
                                        "exposures": {}})
            g["count"] += 1
            g["filters"][filt] = g["filters"].get(filt, 0) + 1
            key = f"{exp:g}s"
            g["exposures"][key] = g["exposures"].get(key, 0) + 1

    # Per-night "no flats for X / no darks matching Ys" advice removed: flats
    # and darks come from the master calibration LIBRARY, not from each night's
    # capture, so comparing a night's captured cal against its lights produced
    # misleading noise. Library coverage + staleness now lives on the dedicated
    # Calibration page (/calibration).
    advice = []
    if light_filters and "BIAS" not in frames:
        advice.append("no bias frames this night (fine if you use a "
                      "master bias / dark library)")
    return {"frames": frames, "advice": advice,
            "lights_ok": bool(light_filters)}


# --- Thumbnails ---------------------------------------------------------------

# The runs grid requests w=264 non-annotated thumbnails on page load (see
# runs.html). Pre-warming exactly that size at grade time makes the strip
# render instantly instead of generating hundreds of thumbnails on first view,
# one at a time under _HEAVY on the RAM-tight scope PC.
PREWARM_THUMB_WIDTH = 264   # runs-page grid tile
PREVIEW_THUMB_WIDTH = 1400  # lightbox (click a tile)
PREWARM_WIDTHS = (PREWARM_THUMB_WIDTH, PREVIEW_THUMB_WIDTH)


def _thumb_out_path(config, date: str, rel_file: str, width: int,
                    annotate: bool) -> Path:
    """Cache path for a thumbnail — identical scheme for the lazy route and the
    grade-time pre-warm so they share one cache (a warmed file is a route hit)."""
    out_dir = Path(config.data_dir) / "thumbs" / date
    stem = rel_file.replace("\\", "_").replace("/", "_")
    return out_dir / f"{stem}.w{width}{'.ann' if annotate else ''}.png"


def _decimate(binned, target_w: int = 1400):
    """Decimate a binned frame to ~target_w px wide for cheap stretching."""
    import numpy as np
    step = max(1, binned.shape[1] // target_w)
    return np.ascontiguousarray(binned[::step, ::step])


def _stretch_and_save(small, out: Path, width: int, stars=None) -> None:
    """Sqrt-stretch a decimated frame to a PNG. Shared by thumbnail() and the
    grade-time pre-warm so both produce a byte-identical stretch.

    Black point ~1 sigma above the sky median (MAD-robust to stars) so the
    background noise floor clips to near-black instead of the old 0.5-percentile
    floor, which let the sqrt stretch amplify sky noise into spurious "extra"
    structure (2026-09-17: requested moderate background knockdown). Faint
    nebulosity above ~1 sigma still survives.
    """
    import numpy as np
    from PIL import Image, ImageDraw
    med = float(np.median(small))
    mad = float(np.median(np.abs(small - med))) or 1.0
    lo = med + 1.0 * 1.4826 * mad
    hi = float(np.percentile(small, 99.7))
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
    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((width, h)).save(out)


def thumbnail(config, date: str, rel_file: str, width: int = 360,
              annotate: bool = False, fill_prewarm: bool = False) -> Path | None:
    """Stretched PNG thumbnail; optionally with star-detection circles.

    Cached on disk per (file, width, annotate) — repeat remote views never
    reopen the FITS. Frame is loaded binned + decimated (a few MB), never
    at full resolution: full-res loads were exhausting the scope PC's RAM.
    The non-annotated w=264 tile is usually already cached by the grade-time
    pre-warm (see _fast_grade), so this only does work for other sizes,
    annotated views, or subs graded before the pre-warm shipped.
    """
    if ".." in rel_file:
        return None
    src = Path(config.image_watch_dir) / date / rel_file
    if not src.exists():
        # Other-rig frames (e.g. the piggyback OSC subs) live under a different
        # NINA watch dir than config's (the route only knows the RC16 config),
        # so date/rel_file won't resolve. Fall back to the sub record's stored
        # abs_path, which points at the frame wherever its rig wrote it.
        try:
            for s in _load_subs(config, date):
                if s.get("file") == rel_file and s.get("abs_path"):
                    cand = Path(s["abs_path"])
                    if cand.is_file():
                        src = cand
                        break
        except Exception:  # noqa: BLE001
            pass
    if not src.exists():
        return None
    out = _thumb_out_path(config, date, rel_file, width, annotate)
    # fill_prewarm (background warmers only): while the FITS is open anyway,
    # also write every other pre-warm size (grid 264 + lightbox 1400) so a
    # click on a tile never waits either. Page requests never pay for this.
    extra = [w for w in PREWARM_WIDTHS if w != width and not
             _thumb_out_path(config, date, rel_file, w, False).exists()] \
        if (fill_prewarm and not annotate) else []
    if out.exists() and not extra:
        return out
    try:
        with _HEAVY:
            _, binned = _load_binned(src)
            small = _decimate(binned)
            del binned
            stars = []
            if annotate:
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
        if not out.exists():
            _stretch_and_save(small, out, width, stars)
        for w in extra:
            _stretch_and_save(small, _thumb_out_path(config, date, rel_file, w,
                                                     False), w)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("Thumbnail failed for %s: %s", src, e)
        return None


_thumbwarm_state: dict[str, dict] = {}


def thumb_warm_status(config, date: str) -> dict:
    """How many of the night's grid thumbnails (w=264) are cached on disk."""
    subs = _load_subs(config, date)
    total = len(subs)
    cached = sum(
        1 for s in subs if s.get("file") and _thumb_out_path(
            config, date, s["file"], PREWARM_THUMB_WIDTH, False).exists())
    st = _thumbwarm_state.get(date, {})
    return {"total": total, "cached": cached,
            "running": bool(st.get("running")),
            "done": st.get("done", cached),
            "current": st.get("current")}


def start_thumb_warm(config, date: str) -> None:
    """Background-generate every missing grid thumbnail for the night so the
    Runs page fills in with local feedback instead of blocking on first view.
    Serialized through the same _HEAVY lock as grading, so it never blows the
    scope PC's RAM; safe to call repeatedly (no-op while already running)."""
    import threading

    st = _thumbwarm_state.setdefault(date, {})
    if st.get("running"):
        return
    st.update(running=True, done=0, total=0, current=None)

    def _work():
        try:
            subs = _load_subs(config, date)
            st["total"] = len(subs)
            done = 0
            for s in subs:
                rel = s.get("file")
                if rel:
                    st["current"] = rel
                    try:
                        thumbnail(config, date, rel,
                                  width=PREWARM_THUMB_WIDTH, annotate=False,
                                  fill_prewarm=True)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("warm thumb failed for %s: %s", rel, e)
                done += 1
                st["done"] = done
        finally:
            st["running"] = False
            st["current"] = None

    threading.Thread(target=_work, daemon=True,
                     name=f"thumbwarm-{date}").start()


def post_night_warm(config, hours: float = 30.0) -> list[str]:
    """Called at dawn shutdown: grade + thumbnail every night touched in the
    last `hours`, in background threads, so the Runs page opens with the work
    already done (before, RC16 grading and thumbnails only started when the
    page was first opened). Covers the RC16 folder (backfill grades and writes
    the grid + lightbox thumbnails in the same pass) and every sub record,
    including piggyback subs graded live (thumb warm fills both sizes).
    Returns the nights started. Safe to call repeatedly."""
    import re as _re
    import time as _time
    cutoff = _time.time() - hours * 3600
    nights: set[str] = set()
    root = Path(config.image_watch_dir)
    if root.exists():
        for d in root.iterdir():
            if (d.is_dir() and _re.match(r"^\d{4}-\d{2}-\d{2}$", d.name)
                    and d.stat().st_mtime >= cutoff):
                nights.add(d.name)
    rd = runs_dir(config)
    if rd.exists():
        for f in rd.glob("*_subs.jsonl"):
            if f.stat().st_mtime >= cutoff:
                nights.add(f.name[:10])
    for d in sorted(nights):
        try:
            if (root / d).exists() and backfill_status(config, d)["pending"]:
                start_backfill(config, d)
            start_thumb_warm(config, d)
        except Exception as e:  # noqa: BLE001
            logger.warning("post-night warm for %s failed: %s", d, e)
    if nights:
        logger.info("Post-night warm started for %s", ", ".join(sorted(nights)))
    return sorted(nights)


def contact_sheet(config, date: str, cols: int = 6, tile_w: int = 200,
                  max_subs: int = 400) -> Path | None:
    """Assemble one night's sub thumbnails into a single labeled montage PNG —
    the archival 'screenshot' of a run before its FITS are pruned.

    Reuses the per-sub thumbnail cache (thumbnail()), so a warmed night is
    cheap. Each tile gets a green (accepted) / red (rejected) border and a
    filter/exposure/HFR/ecc caption. Cached under data_dir/contact_sheets/.
    Returns None if the night has no subs / no renderable thumbnails.
    """
    from PIL import Image, ImageDraw
    subs = [s for s in _load_subs(config, date) if s.get("file")]
    if not subs:
        return None
    subs = subs[:max_subs]
    tiles = []
    for s in subs:
        p = thumbnail(config, date, s["file"], width=tile_w, annotate=False)
        if not p or not Path(p).exists():
            continue
        try:
            tiles.append((s, Image.open(p).convert("RGB")))
        except Exception:  # noqa: BLE001
            continue
    if not tiles:
        return None
    th = max(im.height for _, im in tiles)
    border, bar, pad, header = 2, 18, 6, 40
    cell_w, cell_h = tile_w + 2 * border, th + bar + 2 * border
    rows = (len(tiles) + cols - 1) // cols
    W = cols * cell_w + (cols + 1) * pad
    H = header + rows * cell_h + (rows + 1) * pad
    sheet = Image.new("RGB", (W, H), (10, 10, 14))
    dr = ImageDraw.Draw(sheet)
    acc = sum(1 for s, _ in tiles if s.get("passed_qa"))
    dr.text((pad, 12), f"{date}  ·  {len(tiles)} subs  ·  {acc} accepted / "
            f"{len(tiles) - acc} rejected  ·  PhotonScript contact sheet",
            fill=(226, 232, 240))
    for i, (s, im) in enumerate(tiles):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = header + pad + r * (cell_h + pad)
        col = (74, 222, 128) if s.get("passed_qa") else (248, 113, 113)
        dr.rectangle([x, y, x + cell_w - 1, y + cell_h - 1], fill=col)
        sheet.paste(im, (x + border, y + border))
        dr.rectangle([x + border, y + border + im.height,
                      x + cell_w - border - 1, y + cell_h - border - 1],
                     fill=(15, 15, 20))
        exp = s.get("exp_s") or s.get("exposure")
        label = f"{s.get('filter', '?')} {int(float(exp)) if exp else '?'}s"
        if s.get("hfr") is not None:
            label += f" H{float(s['hfr']):.1f}"
        if s.get("ecc") is not None:
            label += f" e{float(s['ecc']):.2f}"
        dr.text((x + border + 3, y + border + im.height + 3), label[:26],
                fill=(200, 210, 230))
    out = Path(config.data_dir) / "contact_sheets" / f"{date}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


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
