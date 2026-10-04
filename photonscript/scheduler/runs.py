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
import os
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
            # PS-66: unguided targets' subs are credited in seconds
            "guided": bool(getattr(t, "start_guiding", False)),
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


def _load_binned(path: Path, stats: dict | None = None,
                 sat_adu: float | None = None):
    """Header + 2x2-binned float32 frame, built without a full-res float copy.

    Peak memory ~65 MB for a 26 MP frame vs ~210 MB for a naive
    data.astype(float64) — the scope PC was hitting MemoryError.

    PS-108: with `stats` (a dict, filled in place) also counts saturated and
    zero pixels, max ADU and the background median + MAD on the raw
    full-resolution frame in row chunks (shared.pixel_stats.frame_stats).
    `sat_adu` is qa_saturation_adu; a FITS SATURATE keyword wins.
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
        if stats is not None:
            try:
                from types import SimpleNamespace
                from photonscript.shared.pixel_stats import (frame_stats,
                                                             saturation_level)
                lvl = saturation_level(hdr, SimpleNamespace(
                    qa_saturation_adu=sat_adu or 65000.0))
                st = frame_stats(raw, lvl, float(hdr.get("BZERO", 0)),
                                 float(hdr.get("BSCALE", 1)))
                st.pop("n_px", None)
                stats.update(st)
            except Exception as e:  # noqa: BLE001 - never costs a grade
                logger.debug("pixel stats skipped for %s: %s", path.name, e)
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


# PS-94: sqrt-form floors (were 0.30 / 0.25 in the old 1-b/a form; same
# stars: lin_to_sqrt(0.30) = 0.71, lin_to_sqrt(0.25) = 0.66)
_SHAPE_ELONG_FLOOR = 0.71
_SHAPE_ROUND_MAX = 0.66


def _shape_diagnostics(objs, W, H,
                       ecc_floor: float = _SHAPE_ELONG_FLOOR) -> dict:
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
    from photonscript.shared.star_shape import ecc_sqrt
    ecc = ecc_sqrt(a, b)
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
    if med_ecc < _SHAPE_ROUND_MAX:
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

    from photonscript.shared.star_shape import ECC_DEF, ecc_sqrt
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
        star_arrays = None  # PS-80 sidecar: the stars behind the medians
        nstars = int(len(objs))
        if len(objs):
            good = objs[(objs["a"] >= 0.6) & (objs["b"] > 0)]
            nstars = int(len(good))
            if len(good):
                top = good[np.argsort(good["flux"])[::-1][:500]]
                try:
                    # Radii measured on the ORIGINAL image at the positions
                    # found on the filtered one
                    r_all, _ = sep.flux_radius(data_sub, top["x"], top["y"],
                                               6.0 * top["a"], 0.5)
                    ok = np.isfinite(r_all) & (r_all > 0.2) & (r_all < 15)
                    r = r_all[ok]
                    if len(r):
                        hfr = round(float(np.median(r)) * 2, 2)  # ->native px
                    e_all = ecc_sqrt(top["a"], top["b"])
                    star_arrays = {"x": top["x"][ok], "y": top["y"][ok],
                                   "hfr": r_all[ok], "ecc": e_all[ok],
                                   "theta": top["theta"][ok],
                                   "flux": top["flux"][ok],
                                   "w": data.shape[1], "h": data.shape[0]}
                except Exception:  # noqa: BLE001
                    pass
                # PS-94: sqrt(1-(b/a)^2) like the live grader (was 1-b/a)
                e = ecc_sqrt(top["a"], top["b"])
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
                "graded_by": "sep-binned", "ecc_def": ECC_DEF,
                "_stars": star_arrays}

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


def _plan_unguided_targets(config, date: str) -> set:
    """PS-66: target keys the night's plan snapshot dispatched UNGUIDED
    ("guided": false). Snapshots from before PS-66 have no such key and
    yield nothing, so their subs keep counting one per sub."""
    p = runs_dir(config) / f"{date}_plan.json"
    try:
        targets = json.loads(p.read_text(encoding="utf-8")).get("targets") or []
    except Exception:  # noqa: BLE001 - no snapshot / unreadable
        return set()
    return {target_key(t.get("name")) for t in targets
            if isinstance(t, dict) and t.get("guided") is False and t.get("name")}


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
    """Rejection reasons for a warm sub; the rule lives in shared.qa_rules
    (PS-21) so live and backfill grading share it. Kept here for callers."""
    from photonscript.shared.qa_rules import sensor_temp_reasons as _str
    return _str(ccd_temp, header_setpoint, config, setpoint=setpoint)


def _load_native(path: Path):
    """Full-resolution float32 frame (BZERO / BSCALE applied) for the PS-94
    native measure. About 104 MB for a 26 MP frame: callers hold _HEAVY."""
    import numpy as np
    from astropy.io import fits as _fits
    with _fits.open(path, memmap=True,
                    do_not_scale_image_data=True) as hdul:
        hdr = hdul[0].header
        data = np.array(hdul[0].data, dtype=np.float32)
        bscale, bzero = float(hdr.get("BSCALE", 1)), float(hdr.get("BZERO", 0))
    if bscale != 1.0:
        data *= bscale
    if bzero:
        data += bzero
    return data


def _measure_native(path: Path) -> dict | None:
    """PS-94: eccentricity / HFR at the native 0.24"/px with the live
    grader's pipeline (shared.star_shape.measure), so backfill records carry
    the same native ecc the live watcher measures. None without sep or on
    MemoryError (the scope PC is RAM-tight): the record then keeps the binned
    measure only. Logs the time it took per sub."""
    import time
    from photonscript.shared import star_shape
    t0 = time.monotonic()
    try:
        data = _load_native(path)
        res = star_shape.measure(data, binned=False)
        del data
    except MemoryError:
        logger.warning("native measure skipped for %s: MemoryError (binned "
                       "measure only)", path.name)
        return None
    logger.info("native measure %s: %.1fs", path.name, time.monotonic() - t0)
    return res


def _fast_grade(path: Path, config, plan_names: list[str] | None = None,
                *, prewarm: tuple[str, str] | None = None,
                rig: str = "rc16", night: dict | None = None,
                stars_to: tuple[str, str] | None = None,
                native: bool = True) -> dict:
    """Per-sub metrics for backfill: sep on a 2x2-binned frame.

    PS-94: with native=True (RC16 only) it also measures the native frame
    with the live pipeline. Then `ecc` is the native value (what the native
    gate judges, as on live records) and `ecc_bin` / `hfr_bin` the binned
    ones. Without it (MemoryError, no sep, native=False) `ecc` stays the
    binned value and `ecc_at` says "binned". All ecc in sqrt(1-(b/a)^2)
    form (`ecc_def`).

    prewarm=(date, rel_file): while the frame is already loaded, also render the
    runs-grid thumbnail (w=264) so the runs page never generates it on first
    view. Reuses the loaded array — just a stretch + resize + PNG write, so it
    costs a fraction of the grade and never re-opens the FITS. Best-effort: a
    thumbnail failure never blocks grading.

    PS-21: graded by shared.qa_rules.evaluate (same rules as live grading);
    `night` is the night-median context (None: those checks skip until the
    rescore_night post-pass). stars_to=(date, rel_file) also writes the PS-80
    star sidecar from the stars already in memory.
    """
    px: dict = {}   # PS-108 full-resolution pixel counts
    with _HEAVY:
        hdr, binned = _load_binned(path, stats=px, sat_adu=getattr(
            config, "qa_saturation_adu", 65000.0))
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
        # PS-94: the native measure, one full-res frame at a time (_HEAVY)
        mn = None
        if native and rig == "rc16" and m.get("ecc") is not None:
            mn = _measure_native(path)
    gc.collect()
    ecc_bin, hfr_bin = m.get("ecc"), m.get("hfr")
    if mn and mn.get("ecc") is not None:
        ecc, ecc_at = round(mn["ecc"], 3), "native"
    else:
        ecc, ecc_at = ecc_bin, "binned"
    # PS-21: one set of rules for live and backfill (shared.qa_rules). This
    # grader has no true FWHM (its fwhm_arcsec is HFR x scale), so FWHM is
    # not judged here, and the PS-71 star-size signature runs on HFR only.
    from photonscript.shared import qa_rules
    star_arrays = m.pop("_stars", None)
    target = _resolve_target(hdr.get("OBJECT"), path.name, plan_names or [])
    flt = (lambda f: {**{}, **getattr(config, "reverse_filter_map",
                      lambda: {})()}.get(f, f))(hdr.get("FILTER", "?"))
    _exp = float(hdr.get("EXPTIME", 0) or 0)
    _start = _wins = None
    try:
        from photonscript.shared.qa_signatures import exposure_start
        from photonscript.shared.safety_history import unsafe_windows
        _start = exposure_start(hdr.get("DATE-OBS"))
        if _start is not None:
            _wins, _src = unsafe_windows(config, _start,
                                         _start + timedelta(seconds=_exp))
    except Exception as e:  # noqa: BLE001
        logger.debug("safety history skipped for %s: %s", path.name, e)
    # PS-91: guided on a real star? (guard episodes, else the PHD2 guide log)
    _lock = None
    _date = (prewarm or stars_to or (None,))[0]
    if rig == "rc16" and _date and _start is not None and             getattr(config, "guard_enabled", True):
        try:
            from photonscript.scheduler.phd2_analysis import sub_guide_lock
            _lock = sub_guide_lock(config, _date, _start, _exp)
        except Exception as e:  # noqa: BLE001
            logger.debug("guide lock skipped for %s: %s", path.name, e)
    _point = {}
    try:  # PS-67: header mount position vs the named target
        from photonscript.shared.pointing import assess, from_header
        _pos = from_header(hdr)
        if _pos is not None:
            _point = {**assess(config, rig, _pos, target), "src": _pos["src"]}
    except Exception as e:  # noqa: BLE001
        logger.debug("pointing skipped for %s: %s", path.name, e)
    _slew = {}
    if _date and _start is not None:
        try:  # PS-13: a rig riding the RC16 mount, exposed through a move?
            from photonscript.scheduler.slew_gate import NightWindows, gated_rigs
            if rig in gated_rigs(config):
                from photonscript.shared import mount_log
                _slew = NightWindows(
                    config, lines=mount_log.load(config, _date),
                    records=_load_subs(config, _date)).assess(
                        _start, _start + timedelta(seconds=_exp))
        except Exception as e:  # noqa: BLE001
            logger.debug("slew straddle skipped for %s: %s", path.name, e)
    metrics = qa_rules.record_metrics(
        pointing_offset_arcmin=_point.get("off_target_arcmin"),
        pointing_note=_point.get("note"),
        pointing_src=_point.get("src"),
        slew_overlap_s=_slew.get("overlap_s"), slew_note=_slew.get("note"),
        hfr=m["hfr"], fwhm_arcsec=None, ecc=ecc, ecc_bin=ecc_bin,
        stars=m["stars"],
        background=m.get("background"), exp_s=_exp,
        ccd_temp=hdr.get("CCD-TEMP"), set_temp=hdr.get("SET-TEMP"),
        guide_lock=_lock,
        doubled_frac=m.get("doubled_frac"), exposure=m.get("exposure"),
        clipped_pct=m.get("clipped_pct"), sat_stars_pct=m.get("sat_stars_pct"),
        swamp=m.get("swamp"), sat_px_pct=px.get("sat_px_pct"),
        zero_px_pct=px.get("zero_px_pct"), max_adu=px.get("max_adu"))
    card = qa_rules.evaluate(metrics, qa_rules.context(
        config, rig, target, flt, night=night, unsafe_windows=_wins,
        start_utc=_start, image_type=str(hdr.get("IMAGETYP", "LIGHT"))))
    if stars_to is not None and star_arrays is not None:
        try:
            from photonscript.shared import star_table
            n_max = int(getattr(config, "qa_star_sidecar_max", 500) or 0)
            tbl = star_table.build(
                star_arrays["x"], star_arrays["y"], star_arrays["hfr"],
                star_arrays["ecc"], theta=star_arrays["theta"],
                flux=star_arrays["flux"], w=star_arrays["w"],
                h=star_arrays["h"], scale=2.0, limit=n_max,
                grader="sep-binned", rig=rig,
                ecc_def=m.get("ecc_def") or "")
            star_table.write(config, stars_to[0], stars_to[1], tbl, rig=rig)
        except Exception as e:  # noqa: BLE001
            logger.debug("star sidecar skipped for %s: %s", path.name, e)
    hfr = m["hfr"]
    rec = {
        "rig": rig,
        "time": hdr.get("DATE-OBS", ""),
        "target": target,
        "filter": flt,
        "exp_s": float(hdr.get("EXPTIME", 0)),
        "ccd_temp": hdr.get("CCD-TEMP"),
        "set_temp": hdr.get("SET-TEMP"),
        "setpoint_c": card.thresholds.get("setpoint_c"),
        "hfr": hfr,
        "fwhm_arcsec": round(hfr * config.pixel_scale_arcsec, 2) if hfr else None,
        "stars": m["stars"], "ecc": ecc,
        # PS-94: the 0.48"/px measure next to the native one; sqrt form
        "ecc_bin": ecc_bin, "hfr_bin": hfr_bin, "ecc_at": ecc_at,
        "ecc_def": m.get("ecc_def"),
        "background": m["background"],
        "corner_ecc": m.get("corner_ecc"),
        "ecc_pa_R": m.get("ecc_pa_R"),
        "ecc_radial_frac": m.get("ecc_radial_frac"),
        "shape": m.get("shape"),
        "doubled_frac": m.get("doubled_frac"),
        "guide_lock": _lock,
        "clipped_pct": m.get("clipped_pct"),
        "sat_stars_pct": m.get("sat_stars_pct"),
        "swamp": m.get("swamp"), "exposure": m.get("exposure"),
        "noise": m.get("noise"),
        # PS-108: sat_px, sat_px_pct, zero_px, zero_px_pct, max_adu, sat_adu,
        # bg_median, bg_mad (full resolution, same function as live)
        **px,
        "graded_by": m["graded_by"],
        "pointing_offset_arcmin": _point.get("off_target_arcmin"),
        "pointing_note": _point.get("note"),
        "pointing_src": _point.get("src"),   # PS-107
    }
    if _slew.get("overlap_s") is not None:   # PS-13
        rec.update(slew_overlap_s=_slew["overlap_s"], slew_note=_slew.get("note"))
    # passed_qa, reason, qa_flag, scorecard, auto_verdict, auto_reason,
    # drivers (+ reviewed / review_source when all green)
    rec.update(card.record_fields())
    return rec


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
                                         prewarm=(date, rel),
                                         stars_to=(date, rel))
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
            try:  # PS-67: pointing record from headers / the mount log and
                # the off-target check on the attributed targets (before the
                # library build, so an off-target sub is never filed)
                from photonscript.scheduler.pointing_record import night_pass
                pr = night_pass(config, date, solve=False)
                if pr.get("verdicts_changed"):
                    logger.info("Pointing %s: %s", date, pr)
            except Exception as e:  # noqa: BLE001
                logger.warning("Pointing pass failed for %s: %s", date, e)
            try:  # PS-13: Piggy-600 subs that exposed through an RC16 move
                # (mount log, else the RC16 frames), before the library build
                from photonscript.scheduler.slew_gate import night_pass as _slew_pass
                sp = _slew_pass(config, date)
                if sp.get("subs"):
                    logger.info("Slew gate %s: %s of %s judged Piggy subs "
                                "straddle an RC16 move (%s newly rejected)",
                                date, sp["straddled"], sp["judged"],
                                sp["newly_rejected"])
            except Exception as e:  # noqa: BLE001
                logger.warning("Slew gate pass failed for %s: %s", date, e)
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
            try:  # PS-95: per-night tilt / collimation report from the star
                # sidecars (no FITS reads); cached for the trend just below.
                from photonscript.scheduler.optics_report import night_optics
                op = night_optics(config, date)
                logger.info("Optics %s: %s (%s subs measured)", date,
                            op["overall"]["verdict"],
                            op["overall"]["n_measured"])
            except Exception as e:  # noqa: BLE001
                logger.warning("Optics report failed for %s: %s", date, e)
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
            try:  # PS-96: Piggy-600 vs RC16 differential flexure (report
                # only; sampled ASTAP solves, cached for the night page)
                from photonscript.scheduler.flexure import build_report
                fx = build_report(config, date, solve=True)
                if fx.get("flagged"):
                    logger.warning("Flexure %s: Piggy-600 drifts %s\"/min more "
                                   "than the RC16", date,
                                   fx["summary"].get("max_diff_rate_arcsec_min")
                                   or fx["summary"].get("max_excess_arcsec_min"))
            except Exception as e:  # noqa: BLE001
                logger.warning("Flexure report failed for %s: %s", date, e)
            try:  # PS-67: sampled ASTAP solves (every Nth, flagged, first
                # after a slew; reuses the flexure solves), budget-capped
                from photonscript.scheduler.pointing_record import night_pass
                pr = night_pass(config, date, solve=True)
                logger.info("Pointing solves %s: %s solved of %s tried in "
                            "%.0fs", date, pr.get("solved"),
                            pr.get("solve_attempts"), pr.get("solve_s") or 0)
                if pr.get("verdicts_changed"):
                    build_library(config, date)
            except Exception as e:  # noqa: BLE001
                logger.warning("Pointing solve pass failed for %s: %s", date, e)
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


def _human_verdict(rec: dict) -> bool:
    """A reviewer decided this sub (manual accept/reject, or approved via
    approve_night): no automatic pass may change it."""
    return bool(rec.get("manual_qa")) or rec.get("review_source") == "manual" \
        or (bool(rec.get("reviewed")) and rec.get("review_source") != "auto")


def header_pointing_reject(rec: dict) -> bool:
    """PS-107: an automatic reject whose only driver is the On-target check
    judged without a plate solve (header, mount log, RC16-correlated, or a
    pre-PS-107 record that does not say)."""
    from photonscript.shared.qa_rules import pointing_confirmed
    return (not rec.get("passed_qa") and not _human_verdict(rec)
            and list(rec.get("drivers") or []) == ["pointing"]
            and not pointing_confirmed(rec.get("pointing_src")))


def _old_verdict(rec: dict) -> str:
    if not rec.get("passed_qa"):
        return "rejected"
    return rec.get("auto_verdict") or "passed (legacy)"


def rescore_night(config, date: str, apply: bool = False,
                  allow_unreject: bool = False,
                  extra_unsafe: list[tuple] | None = None,
                  records: list[dict] | None = None) -> dict:
    """PS-21: re-grade a night's stored metrics with the current rules and the
    night-median context (HFR and background per rig + target + filter). No
    FITS load. DRY-RUN BY DEFAULT: reports the verdict diff and changes
    nothing unless apply=True.

    Never touches a human verdict (manual accept/reject or approve_night).
    Without allow_unreject a sub rejected earlier stays rejected even if the
    new rules pass it (reported as `kept_rejected`): older records lack some
    inputs (guiding, safety windows) the original grade may have used.
    With apply, newly rejected subs leave the stack set (Library links move
    to Library/_rejected/, as PS-71) and newly approved subs are linked.
    `records` grades that list instead of the night's jsonl (offline dry
    runs on a copy); apply is refused then."""
    from collections import Counter
    from photonscript.shared import qa_rules
    from photonscript.scheduler.qa_backfill import _library_links
    if records is not None and apply:
        raise ValueError("apply needs the night's own records")
    subs = records if records is not None else _load_subs(config, date)
    counts: Counter = Counter()
    transitions: Counter = Counter()
    drivers: Counter = Counter()
    diffs, moves = [], []
    changed = 0
    lib = library_root(config)
    graded, source = _night_cards(config, date, subs,
                                  extra_unsafe=extra_unsafe,
                                  sidecars=records is None)

    def _keep_score(rec, card):
        """PS-108: a kept verdict still gets the current score."""
        nonlocal changed
        sf = card.score_fields()
        if not apply or not sf:
            return
        card_c = dict(rec.get("scorecard") or {})
        if card.score is not None and card_c.get("rows"):
            card_c["score"] = card.score.compact()
        if all(rec.get(k) == v for k, v in sf.items()) \
                and (rec.get("scorecard") or {}) == card_c:
            return
        rec.update(sf)
        if card_c.get("rows"):
            rec["scorecard"] = card_c
        changed += 1

    for rec, card in graded:
        key = qa_rules.group_key(rec)
        old = _old_verdict(rec)
        new = card.verdict
        counts[f"new_{new}"] += 1
        if card.score is not None:
            counts[f"score_{card.score.decision}"] += 1
        for d in card.drivers:
            drivers[d] += 1
        if _human_verdict(rec):
            counts["human_kept"] += 1
            _keep_score(rec, card)
            if (new == "rejected") == bool(rec.get("passed_qa")):
                diffs.append({"file": rec.get("file"), "rig": key[0],
                              "old": "human " + ("accepted" if rec.get(
                                  "passed_qa") else "rejected"),
                              "new": new, "action": "kept (human verdict)",
                              "drivers": card.drivers})
            continue
        transitions[f"{old} -> {new}"] += 1
        action = None
        if not rec.get("passed_qa") and card.passed:
            if allow_unreject:
                counts["unrejected"] += 1
                action = "un-rejected"
            elif header_pointing_reject(rec):
                # PS-107: rejected only by an unconfirmed header offset now
                # under the gross limit: every other check already passed
                counts["unrejected"] += 1
                counts["unrejected_pointing"] += 1
                action = "un-rejected (header-only pointing, PS-107)"
            else:
                counts["kept_rejected"] += 1
                action = "kept rejected (no --allow-unreject)"
        elif rec.get("passed_qa") and not card.passed:
            counts["newly_rejected"] += 1
            action = "rejected"
        elif (rec.get("reason") or "") != card.reason and not card.passed:
            counts["reason_changed"] += 1
            action = "reason updated"
        if old != new or action:
            diffs.append({"file": rec.get("file"), "rig": key[0],
                          "target": key[1], "filter": key[2], "old": old,
                          "new": new, "action": action or "verdict updated",
                          "drivers": card.drivers,
                          "warnings": card.warnings,
                          "old_reason": rec.get("reason") or "",
                          "new_reason": card.reason})
        if (action or "").startswith("kept"):
            _keep_score(rec, card)
            continue
        if not apply:
            continue
        was_passed = bool(rec.get("passed_qa"))
        fields = card.record_fields()
        if not card.auto_approved and rec.get("review_source") == "auto":
            fields.update(reviewed=False, review_source=None)
        if all(rec.get(k) == v for k, v in fields.items()):
            continue
        rec.update(fields)
        if rec.get("review_source") is None:
            rec.pop("review_source", None)
        rec["rescored"] = qa_rules.RULES_VERSION
        changed += 1
        if was_passed and not card.passed:
            for src in _library_links(config, rec):
                dest = lib / "_rejected" / src.relative_to(lib)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        src.unlink()
                    else:
                        os.replace(src, dest)
                    moves.append(f"{src} -> {dest}")
                except OSError as e:
                    logger.warning("rescore library move %s failed: %s",
                                   src, e)
    if apply and changed:
        _rewrite_subs(config, date, subs)
        try:
            build_library(config, date)
        except Exception as e:  # noqa: BLE001
            logger.warning("rescore %s: library update failed: %s", date, e)
        sync_goal_progress(config)
    result = {"date": date, "mode": "apply" if apply else "dry-run",
              "rules_version": qa_rules.RULES_VERSION, "subs": len(subs),
              "score_mode": qa_rules.thresholds(config)["score_mode"],
              "counts": dict(counts), "transitions": dict(transitions),
              "records_changed": changed,
              "drivers": dict(drivers),
              "unsafe_source": source, "diffs": diffs,
              "library_moves": moves}
    logger.info("PS-21 rescore %s (%s): %s", date, result["mode"],
                dict(counts))
    return result


def _night_cards(config, date: str, subs: list[dict],
                 extra_unsafe: list[tuple] | None = None,
                 sidecars: bool = True) -> tuple[list, str | None]:
    """Re-grade stored records with the current rules (no FITS): the night
    medians per rig + target + filter, the safety history and (sidecars)
    the PS-67 pointing sidecar, where a plate solve wins over the header.
    Returns ([(record, Scorecard)], safety source). Shared by rescore_night
    and score_report (PS-108) so both judge a sub the same way."""
    from photonscript.shared import qa_rules
    from photonscript.scheduler.qa_backfill import _start_of
    ctxs = qa_rules.night_context(subs)
    wins, source = None, None
    try:
        from photonscript.shared.safety_history import unsafe_windows
        day = datetime.fromisoformat(date)
        wins, source = unsafe_windows(config, day + timedelta(hours=12),
                                      day + timedelta(hours=40),
                                      extra=extra_unsafe)
    except Exception as e:  # noqa: BLE001
        logger.debug("rescore %s: no safety history (%s)", date, e)
    try:  # PS-67: the pointing sidecar (solve wins over the header)
        from photonscript.shared.pointing import load as _load_pointing
        pts = _load_pointing(config, date) if sidecars else {}
    except Exception:  # noqa: BLE001
        pts = {}
    out = []
    for rec in subs:
        key = qa_rules.group_key(rec)
        try:
            start = _start_of(rec)
        except Exception:  # noqa: BLE001
            start = None
        _m = qa_rules.metrics_from_record(rec)
        _p = pts.get((key[0], rec.get("file")))
        if _p and _p.get("off_target_arcmin") is not None:
            from photonscript.shared.pointing import judged_src
            _m["pointing_offset_arcmin"] = _p["off_target_arcmin"]
            _m["pointing_note"] = _p.get("note")
            _m["pointing_src"] = judged_src(_p) or _p.get("src")
        out.append((rec, qa_rules.evaluate(
            _m, qa_rules.context(config, key[0], key[1], key[2],
                                 night=ctxs.get(key), unsafe_windows=wins,
                                 start_utc=start))))
    return out, source


def today_state(rec: dict) -> str:
    """The sub's verdict as it stands: approved (passed and reviewed, by a
    person or the auto rule), review (passed, waiting) or rejected."""
    if not rec.get("passed_qa"):
        return "rejected"
    return "approved" if rec.get("reviewed") else "review"


_SCORE_TO_STATE = {"approve": "approved", "review": "review",
                   "reject": "rejected"}


def score_report(config, date: str, records: list[dict] | None = None) -> dict:
    """PS-108: per night, how many subs the score would approve / send to
    review / reject vs today's verdicts. Re-grades the stored metrics with
    the current rules and weights (same path as qa-rescore, no FITS, writes
    nothing). Human verdicts are counted apart: the score never moves them.
    `records` reports on a copy instead of the night's jsonl."""
    from collections import Counter
    from photonscript.shared import qa_rules
    subs = records if records is not None else _load_subs(config, date)
    graded, _src = _night_cards(config, date, subs, sidecars=records is None)
    t = qa_rules.thresholds(config)
    today: Counter = Counter()
    by_score: Counter = Counter()
    moves: Counter = Counter()
    human: Counter = Counter()
    by_rig: dict = {}
    rows = []
    hist: Counter = Counter()
    for rec, card in graded:
        rig = rec.get("rig") or "rc16"
        now = today_state(rec)
        sc = card.score
        dec = sc.decision if sc is not None else None
        would = _SCORE_TO_STATE.get(dec, now)
        today[now] += 1
        if dec:
            by_score[dec] += 1
            hist[min(9, int(sc.value) // 10)] += 1
        r = by_rig.setdefault(rig, {"subs": 0, "today": Counter(),
                                    "score": Counter(), "move": 0})
        r["subs"] += 1
        r["today"][now] += 1
        if dec:
            r["score"][dec] += 1
        if _human_verdict(rec):
            human["n"] += 1
            human["agree" if would == now else "disagree"] += 1
            continue
        if would != now:
            moves[f"{now} -> {would}"] += 1
            r["move"] += 1
            rows.append({"file": rec.get("file"), "rig": rig,
                         "target": rec.get("target"),
                         "filter": rec.get("filter"), "today": now,
                         "score": sc.value if sc else None, "would": would,
                         "why": sc.top_text() if sc else ""})
    return {"date": date, "mode": t["score_mode"],
            "approve_at": t["score_approve"], "reject_below": t["score_reject"],
            "rules_version": qa_rules.RULES_VERSION, "subs": len(subs),
            "today": dict(today), "score": dict(by_score),
            "moves": dict(moves), "would_move": sum(moves.values()),
            "human": dict(human),
            "histogram": [hist.get(i, 0) for i in range(10)],
            "by_rig": {k: {"subs": v["subs"], "today": dict(v["today"]),
                           "score": dict(v["score"]), "would_move": v["move"]}
                       for k, v in by_rig.items()},
            "rows": sorted(rows, key=lambda x: (x["score"] is None,
                                                x["score"] or 0))}


def format_score_report(rep: dict) -> str:
    """Plain-text score report (CLI)."""
    t, s = rep.get("today", {}), rep.get("score", {})
    lines = [
        f"{rep['date']}: {rep['subs']} subs; score mode {rep['mode']} "
        f"(approve >= {rep['approve_at']:g}, reject < {rep['reject_below']:g})",
        f"  today:    {t.get('approved', 0)} approved / {t.get('review', 0)} "
        f"review / {t.get('rejected', 0)} rejected",
        f"  by score: {s.get('approve', 0)} approve / {s.get('review', 0)} "
        f"review / {s.get('reject', 0)} reject",
        f"  would move: {rep['would_move']} "
        + (", ".join(f"{k} {v}" for k, v in sorted(rep['moves'].items()))
           or "(none)"),
    ]
    h = rep.get("human") or {}
    if h.get("n"):
        lines.append(f"  human verdicts kept: {h['n']} (score agrees with "
                     f"{h.get('agree', 0)})")
    hist = rep.get("histogram") or []
    if any(hist):
        lines.append("  score histogram: " + " ".join(
            f"{i * 10}-{i * 10 + 9}:{n}" for i, n in enumerate(hist) if n))
    for rig, v in sorted((rep.get("by_rig") or {}).items()):
        lines.append(f"  {rig}: {v['subs']} subs, today {v['today']}, score "
                     f"{v['score']}, would move {v['would_move']}")
    for r in rep.get("rows", [])[:30]:
        lines.append(f"    {r['today']:>8} -> {r['would']:<8} {r['score']!s:>3} "
                     f"{r['rig']} {r['filter']} {r['file']}  {r['why']}")
    return "\n".join(lines)


def flag_hfr_outliers(config, date: str, factor: float | None = None) -> int:
    """Backfill post-pass (kept for callers): now the PS-21 rescore, which
    applies the night-median checks (HFR outlier x qa_hfr_outlier_factor,
    background vs median) with every other rule. Never un-rejects, never
    overrides a human verdict. Returns the number of newly rejected subs.
    `factor` is ignored (qa_hfr_outlier_factor in config)."""
    res = rescore_night(config, date, apply=True)
    return int(res["counts"].get("newly_rejected", 0))


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
    # (rig, target key, filter) -> list of accepted sub lengths (s); lengths
    # let an HDR plan split its short companion subs from the long set. PS-78:
    # keyed on the canonical target, so container-named subs count toward the
    # goal. PS-30: and on the rig, so a plan only counts its own rig's subs.
    # PS-66: RC16 subs from a target the night dispatched unguided (capped)
    # are credited by their seconds; every other sub (guided nights, older
    # history, the piggyback, which is never capped) counts as one full sub.
    known = known_target_index(list(store.projects.values()))
    lengths: dict[tuple, list] = {}
    by_seconds: dict[tuple, list] = {}
    for f in runs_dir(config).glob("*_subs.jsonl"):
        date = f.name.split("_")[0]
        unguided = None
        for s_ in _load_subs(config, date):
            if not s_.get("passed_qa"):
                continue
            t = canonical_target(s_.get("target"), known)
            if t:
                if unguided is None:
                    unguided = _plan_unguided_targets(config, date)
                rig = s_.get("rig") or "rc16"
                key = (rig, target_key(t), s_.get("filter"))
                lengths.setdefault(key, []).append(s_.get("exp_s"))
                by_seconds.setdefault(key, []).append(
                    rig == "rc16" and target_key(t) in unguided)
    changed = []
    for p in store.projects.values():
        tname = target_key(p.target.name)
        touched = False
        for e in p.exposure_plans:
            key = (e.rig or "rc16", tname, e.filter_type.value)
            subs = lengths.get(key, [])
            secs = by_seconds.get(key, [])
            short = sum(1 for x in subs if e.is_short_exposure(x))
            # PS-66: the same rule as the live record_accepted_sub path
            long_s = sum(e.credit_seconds(x, by_s)
                         for x, by_s in zip(subs, secs)
                         if not e.is_short_exposure(x))
            n = min(e.subs_from_seconds(long_s), e.count)
            ns = min(short, e.hdr_short_count)
            if (n != e.acquired or ns != e.hdr_short_acquired
                    or abs(long_s - e.acquired_s) > 1e-6):
                e.acquired = n
                e.acquired_s = long_s
                e.hdr_short_acquired = ns
                touched = True
        if touched:
            changed.append(p.target.name)
    if changed:
        store.save()
        logger.info("Goal progress synced from history: %s", changed)
        try:  # PS-30 (PS-14): Pushover once when a goal completes
            from photonscript.scheduler.campaign import check_goal_transitions
            check_goal_transitions(config, store)
        except Exception as e:  # noqa: BLE001 - never break the goal sync
            logger.warning("campaign completion check failed: %s", e)
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
                  state: str | None = None,
                  why: str | None = None) -> dict | None:
    """Human verdict for one sub: 'accepted' | 'rejected' | 'review'.

    'review' hands the sub back to the automatic pipeline (pass, not yet
    reviewed). Legacy bool `passed` maps to accepted/rejected.

    PS-21: the automatic result is never erased. `auto_verdict` /
    `auto_reason` / `scorecard` / `drivers` stay on the record (captured from
    passed_qa / reason first for records graded before PS-21); `why` (a check
    id such as 'ecc', or 'visual') lands in `manual_reason`.
    """
    if state is None:
        state = "accepted" if passed else "rejected"
    subs = _load_subs(config, date)
    hit = None
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for s_ in subs:
        if s_.get("file") == rel_file:
            if "auto_verdict" not in s_ and not s_.get("manual_qa"):
                s_["auto_verdict"] = ("rejected" if not s_.get("passed_qa")
                                      else "passed (legacy)")
                s_["auto_reason"] = s_.get("reason") or ""
            if state == "accepted":
                s_.update(passed_qa=True, reviewed=True, manual_qa=True,
                          reason="", review_source="manual",
                          reviewed_at=now)
            elif state == "rejected":
                s_.update(passed_qa=False, reviewed=True, manual_qa=True,
                          reason="rejected manually" + (f": {why}" if why
                                                        else ""),
                          review_source="manual", reviewed_at=now)
            else:  # review
                # review_source stays "manual": a rescore must not
                # auto-approve a sub a person sent back to review
                s_.update(passed_qa=True, reviewed=False, manual_qa=False,
                          reason="", review_source="manual",
                          reviewed_at=now)
            if state in ("accepted", "rejected"):
                if why:
                    s_["manual_reason"] = str(why)[:120]
                else:
                    s_.pop("manual_reason", None)
            else:
                s_.pop("manual_reason", None)
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


def score_legacy_on_read(config, subs: list[dict]) -> int:
    """PS-21: records graded before the scorecard existed get one computed
    from their stored metrics (current thresholds, night medians, no safety
    history), marked scored_on_read. passed_qa / reason are NOT changed:
    the stored verdict stands until a rescore. Returns how many were scored."""
    from photonscript.shared import qa_rules
    legacy = [s_ for s_ in subs if not s_.get("scorecard")]
    if not legacy:
        return 0
    ctxs = qa_rules.night_context(subs)
    cache: dict = {}
    n = 0
    for s_ in legacy:
        key = qa_rules.group_key(s_)
        try:
            ctx = cache.get(key)
            if ctx is None:
                ctx = cache[key] = qa_rules.context(
                    config, key[0], key[1], key[2], night=ctxs.get(key))
            card = qa_rules.evaluate(qa_rules.metrics_from_record(s_), ctx)
            s_["scorecard"] = card.compact()
            s_["scored_on_read"] = True
            n += 1
        except Exception as e:  # noqa: BLE001 - a card never sinks the page
            logger.debug("on-read scorecard skipped for %s: %s",
                         s_.get("file"), e)
    return n


def _ecc_display_sqrt(subs: list[dict]) -> int:
    """PS-94: show every sub's ecc in sqrt(1-(b/a)^2) form. Pre-PS-94
    backfill records hold 1-b/a; on these in-memory copies `ecc` is
    converted (the stored value is kept as `ecc_raw`), so the runs page and
    its medians compare like with like. Nothing is written."""
    from photonscript.shared import qa_rules
    from photonscript.shared.star_shape import ECC_DEF
    n = 0
    for s_ in subs:
        if s_.get("ecc_def") is not None or s_.get("graded_by") != "sep-binned":
            continue
        e = qa_rules.record_ecc(s_)
        if e is not None:
            s_["ecc_raw"] = s_.get("ecc")
            s_["ecc"] = round(e, 3)
            s_["ecc_def"] = ECC_DEF
            n += 1
    return n


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
    score_legacy_on_read(config, subs)
    _ecc_display_sqrt(subs)

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


def _save_png_atomic(img, out: Path) -> None:
    """PS-59: write to a temp file beside ``out`` and os.replace it in, so a
    hard kill mid-write (supervisor restart, 15 s update fallback) can never
    leave a truncated PNG that the ``out.exists()`` cache then serves forever.
    If two writers race (same thumbnail, identical bytes) and Windows refuses
    the replace because a reader holds the file, the existing file wins."""
    import os
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        img.save(tmp, format="PNG")
        try:
            os.replace(tmp, out)
        except OSError:
            if not out.exists():
                raise
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


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
    _save_png_atomic(img.resize((width, h)), out)


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


def _sub_source(config, date: str, rel_file: str) -> tuple[Path | None, dict]:
    """(FITS path, record) for one sub: the watch dir, else the record's
    abs_path (other-rig frames), as thumbnail() resolves it."""
    rec = next((s for s in _load_subs(config, date)
                if s.get("file") == rel_file), {}) or {}
    if ".." in rel_file:
        return None, rec
    src = Path(config.image_watch_dir) / date / rel_file
    if not src.exists() and rec.get("abs_path"):
        cand = Path(rec["abs_path"])
        if cand.is_file():
            src = cand
    return (src if src.exists() else None), rec


def histogram(config, date: str, rel_file: str, refresh: bool = False) -> dict | None:
    """PS-5 (folded into PS-108): exact full-resolution 16-bit histogram of
    one sub (shared.pixel_stats.histogram), mono "L" or per Bayer channel
    R / G / B for OSC frames (BAYERPAT, RGGB assumed on the Piggy-600 when
    the header has none). Cached as JSON under <data_dir>/hist/<date>/;
    computed on a miss under _HEAVY (one full-frame read, row-chunked).
    None when the FITS cannot be found."""
    from photonscript.shared.pixel_stats import histogram as _hist
    from photonscript.shared.pixel_stats import saturation_level
    from photonscript.shared.rigs import rig_config
    out = (Path(config.data_dir) / "hist" / date
           / (rel_file.replace("\\", "_").replace("/", "_") + ".json"))
    if out.exists() and not refresh:
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    src, rec = _sub_source(config, date, rel_file)
    if src is None:
        return None
    import time as _time
    from astropy.io import fits as _fits
    rig = rec.get("rig") or "rc16"
    t0 = _time.monotonic()
    with _HEAVY:
        with _fits.open(src, memmap=True, do_not_scale_image_data=True) as hdul:
            hdr = hdul[0].header.copy()
            hdr_bayer = str(hdr.get("BAYERPAT") or "").strip()
            bayer = hdr_bayer or ("RGGB" if rig == "piggyback" else None)
            res = _hist(hdul[0].data, float(hdr.get("BZERO", 0)),
                        float(hdr.get("BSCALE", 1)), bayer=bayer,
                        sat_adu=saturation_level(hdr, config),
                        offset=float(getattr(rig_config(config, rig),
                                             "default_offset", 0) or 0))
    gc.collect()
    res.update(file=rel_file, rig=rig,
               compute_s=round(_time.monotonic() - t0, 2),
               bayer_assumed=bool(bayer) and not hdr_bayer)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(res), encoding="utf-8")
        os.replace(tmp, out)
    except OSError as e:
        logger.debug("histogram cache write failed for %s: %s", rel_file, e)
    return res


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
    _save_png_atomic(sheet, out)
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
