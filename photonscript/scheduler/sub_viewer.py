"""Review viewer data: PS-80 star overlay, PS-6 loupe, PS-17 3x3 mosaic.

* ``stars_for_view``: the sub's PS-80 star sidecar (the grader's own stars,
  written by both graders). A sub graded before the sidecars shipped has
  none: it is measured once on view with the backfill grader's function
  (runs._measure_native, PS-83 shared.star_measure) and cached as a sidecar
  marked ``on_view`` ("measured on view": the circles match a regrade, not
  necessarily the sub's original live numbers). The response carries every
  ecc in sqrt(1-(b/a)^2) form.
* ``crop``: a full-resolution window of one sub (the loupe). Memmap slice
  only (about 130 KB read for 256 x 256), never a full-frame load, so it
  does not take runs._HEAVY; at most two run at once. Stretched with the
  preview's black / white points (runs._stretch_points on a 2x2-binned
  sample of the frame, the same stats the 1400 px preview is built from).
  OSC frames are shown as the 2x2 superpixel (mono, each superpixel drawn
  as 2 x 2 native px, so the true star shape without a debayer). Small
  in-memory LRU; centers snap to an 8 px grid so a pause near the last spot
  is a hit.
* ``mosaic``: nine crops (four corners, four edge midpoints, center) from
  one FITS open, composed into one PNG with 2 px gutters, cached under
  <data_dir>/thumbs/<date>/ next to the preview thumbnails.
* ``mosaic_info``: tile origins and per-tile HFR / ecc readouts: median of
  the sidecar stars in each 3x3 zone (the zones runs._shape_diagnostics
  uses), else the record's corner_ecc, else blank.
"""

from __future__ import annotations

import io
import logging
import threading
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

LOUPE_SIZES = (128, 256, 384)
MOSAIC_SIZES = (128, 256)
SNAP_PX = 8           # loupe centers snap to this grid (cache hits)
SAMPLE_STEP = 8       # stretch sample: 2x2 mean every 8 native px
GUTTER_PX = 2
GUTTER_GRAY = 48
ZONE_MIN_STARS = 5    # as runs._shape_diagnostics
TILE_LABELS = ("TL", "T", "TR", "L", "C", "R", "BL", "B", "BR")

_SLOTS = threading.BoundedSemaphore(2)
_cache_lock = threading.Lock()
_crop_cache: OrderedDict = OrderedDict()
_CROP_CACHE_MAX = 96
_frame_cache: OrderedDict = OrderedDict()
_FRAME_CACHE_MAX = 32


class Busy(RuntimeError):
    """Two crops are already running; the caller answers 503."""


def _lru_get(cache: OrderedDict, key):
    with _cache_lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _lru_put(cache: OrderedDict, key, value, limit: int) -> None:
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)


def clear_caches() -> None:
    with _cache_lock:
        _crop_cache.clear()
        _frame_cache.clear()


# ------------------------------------------------------------ PS-80 stars

def stars_for_view(config, date: str, rel_file: str, rig: str | None = None,
                   compute: bool = True) -> dict | None:
    """The sub's star sidecar for the overlay, measured once on view when
    missing (compute=True and the FITS is on this PC). ecc normalized to
    the sqrt form. None when there is no sidecar and none can be made."""
    from photonscript.scheduler.runs import _sub_source
    from photonscript.shared import star_table
    src, rec = _sub_source(config, date, rel_file)
    rig = rig or rec.get("rig") or "rc16"
    t = star_table.read(config, date, rel_file, rig)
    if t is None and compute and src is not None:
        t = _measure_on_view(config, date, rel_file, rig, src)
    if t is None:
        return None
    return _normalized(t, rec)


def _normalized(t: dict, rec: dict) -> dict:
    from photonscript.scheduler.tracking_test import _sidecar_ecc_sqrt
    from photonscript.shared.star_shape import ECC_DEF
    out = dict(t)
    out["ecc_stored_def"] = t.get("ecc_def") or None
    out["ecc"] = [None if e is None else round(float(e), 3)
                  for e in _sidecar_ecc_sqrt(t, rec)]
    out["ecc_def"] = ECC_DEF
    out["on_view"] = bool(t.get("on_view"))
    return out


def _measure_on_view(config, date: str, rel_file: str, rig: str,
                     src: Path) -> dict | None:
    """Measure one legacy sub with the backfill grader's function and write
    the sidecar. Under runs._HEAVY (one full-res frame at a time); the
    sidecar is re-checked inside the lock so two viewers measure once."""
    import gc
    import time

    from astropy.io import fits as _fits

    from photonscript.scheduler import runs
    from photonscript.shared import star_measure, star_table
    from photonscript.shared.rigs import rig_config
    rcfg = rig_config(config, rig)
    t0 = time.monotonic()
    with runs._HEAVY:
        t = star_table.read(config, date, rel_file, rig)
        if t is not None:
            return t
        try:
            hdr = _fits.getheader(src)
            osc = star_measure.is_osc(rig, hdr)
            m = runs._measure_native(src, rcfg, rig, osc, header=hdr)
            if m is None:   # MemoryError: the binned fallback, as _fast_grade
                _, binned = runs._load_binned(src)
                m = star_measure.measure_frame(
                    binned, rcfg, rig, osc=osc, binned_input=True,
                    grader=runs.BACKFILL_GRADER, header=hdr)
                del binned
        except Exception as e:  # noqa: BLE001 - the viewer shows "no stars"
            logger.warning("stars on view failed for %s: %s", rel_file, e)
            return None
        finally:
            gc.collect()
    table = m.get("star_table") if m else None
    if not table:
        return None
    table["on_view"] = True
    star_table.write(config, date, rel_file, table, rig=rig)
    logger.info("stars measured on view %s: %d stars, %.1fs", rel_file,
                table.get("n") or 0, time.monotonic() - t0)
    return table


# ------------------------------------------------------- frame + windows

def _frame(src: Path, hdul, rig: str) -> dict:
    """w, h, BZERO / BSCALE, OSC and the preview stretch points of one
    frame, cached per (path, mtime)."""
    import numpy as np

    from photonscript.scheduler.runs import _stretch_points
    from photonscript.shared.star_measure import is_osc
    try:
        mt = src.stat().st_mtime_ns
    except OSError:
        mt = 0
    key = (str(src), mt, rig)
    hit = _lru_get(_frame_cache, key)
    if hit is not None:
        return hit
    hdr = hdul[0].header
    raw = hdul[0].data
    h, w = int(raw.shape[0]), int(raw.shape[1])
    bzero, bscale = float(hdr.get("BZERO", 0)), float(hdr.get("BSCALE", 1))
    h2, w2 = h // 2 * 2, w // 2 * 2
    s = SAMPLE_STEP
    # the preview is the 2x2-binned frame decimated: sample it the same way
    smp = raw[0:h2:s, 0:w2:s].astype(np.float32)
    smp += raw[1:h2:s, 0:w2:s]
    smp += raw[0:h2:s, 1:w2:s]
    smp += raw[1:h2:s, 1:w2:s]
    smp *= 0.25 * bscale
    smp += bzero
    lo, hi = _stretch_points(smp)
    info = {"w": w, "h": h, "bzero": bzero, "bscale": bscale,
            "osc": bool(is_osc(rig, hdr)), "lo": lo, "hi": hi}
    _lru_put(_frame_cache, key, info, _FRAME_CACHE_MAX)
    return info


def _clamp_origin(c: float, n: int, full: int, even: bool) -> int:
    """Origin of an n px window centered near c, inside [0, full)."""
    o = int(round(c - n / 2.0))
    o = max(0, min(o, full - n))
    if even:
        o -= o % 2
    return max(0, o)


def _window(raw, info: dict, x0: int, y0: int, n: int):
    """n x n float32 native window at (x0, y0), BZERO / BSCALE applied;
    padded with the black point where the frame is smaller than n. OSC:
    the 2x2 superpixel mean, each value repeated over its 2 x 2 px."""
    import numpy as np
    a = np.asarray(raw[y0:y0 + n, x0:x0 + n], dtype=np.float32)
    a = a * info["bscale"] + info["bzero"]
    if info["osc"]:
        hh, ww = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
        a = a[:hh, :ww]
        sp = 0.25 * (a[0::2, 0::2] + a[1::2, 0::2] + a[0::2, 1::2]
                     + a[1::2, 1::2])
        a = np.repeat(np.repeat(sp, 2, axis=0), 2, axis=1)
    if a.shape != (n, n):
        pad = np.full((n, n), info["lo"], dtype=np.float32)
        pad[:a.shape[0], :a.shape[1]] = a
        a = pad
    return a


def to_u8(a, lo: float, hi: float):
    """The preview's sqrt stretch (runs._stretch_and_save) as uint8."""
    import numpy as np
    s = np.sqrt(np.clip((a - lo) / max(hi - lo, 1e-3), 0, 1))
    return (s * 255).astype(np.uint8)


def upscale(u8, k: int):
    """Exact nearest-neighbor upscale by an integer factor."""
    import numpy as np
    if k <= 1:
        return u8
    return np.repeat(np.repeat(u8, k, axis=0), k, axis=1)


def _png(u8) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(u8, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def _open(src: Path):
    from astropy.io import fits as _fits
    return _fits.open(src, memmap=True, do_not_scale_image_data=True)


# ---------------------------------------------------------------- PS-6

def crop(config, date: str, rel_file: str, *, x: float | None = None,
         y: float | None = None, fx: float | None = None,
         fy: float | None = None, size: int = 256, scale: int = 1) -> dict | None:
    """Loupe crop centered at native (x, y), or at the frame fraction
    (fx, fy) when x / y are not given. `size` is the output edge in px;
    scale 2 shows a size/2 native window at 2:1 (exact nearest neighbor).
    Returns {"png", "x0", "y0", "n", "w", "h", "cx", "cy", "scale", "osc"}
    or None when the FITS is not on this PC. Raises Busy when two crops are
    already running."""
    from photonscript.scheduler.runs import _sub_source
    size = min(LOUPE_SIZES, key=lambda s: abs(s - int(size)))
    scale = 2 if int(scale) >= 2 else 1
    src, rec = _sub_source(config, date, rel_file)
    if src is None:
        return None
    rig = rec.get("rig") or "rc16"
    if not _SLOTS.acquire(timeout=10):
        raise Busy("loupe busy")
    try:
        with _open(src) as hdul:
            info = _frame(src, hdul, rig)
            w, h = info["w"], info["h"]
            cx = float(x) if x is not None else float(fx if fx is not None
                                                      else 0.5) * w
            cy = float(y) if y is not None else float(fy if fy is not None
                                                      else 0.5) * h
            cx = min(max(cx, 0.0), w - 1.0)
            cy = min(max(cy, 0.0), h - 1.0)
            cx = round(cx / SNAP_PX) * SNAP_PX
            cy = round(cy / SNAP_PX) * SNAP_PX
            n = size // scale
            key = (str(src), info["lo"], info["hi"], cx, cy, n, scale)
            hit = _lru_get(_crop_cache, key)
            if hit is not None:
                return hit
            x0 = _clamp_origin(cx, n, w, info["osc"])
            y0 = _clamp_origin(cy, n, h, info["osc"])
            u8 = upscale(to_u8(_window(hdul[0].data, info, x0, y0, n),
                               info["lo"], info["hi"]), scale)
    finally:
        _SLOTS.release()
    out = {"png": _png(u8), "x0": x0, "y0": y0, "n": n, "w": w, "h": h,
           "cx": cx, "cy": cy, "scale": scale, "osc": info["osc"]}
    _lru_put(_crop_cache, key, out, _CROP_CACHE_MAX)
    return out
