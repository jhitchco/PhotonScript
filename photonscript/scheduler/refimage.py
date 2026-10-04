"""Reference sky image per target, with each rig's field of view (PS-81).

A DSS2 color cutout from CDS hips2fits, fetched ONCE per target and view and
cached under ``<data_dir>/refimg/`` with a JSON sidecar (center, fov, size,
projection, source, fetched time, credit). Approved 2026-09-27: DSS2 from
CDS, own best sub as the offline fallback (the fallback is chosen by the
caller, see routers/targets.py).

Only the Targets page asks for it (a person opening the page); the armer and
the night jobs never call it. A failed fetch is remembered for an hour so an
offline scope PC does not retry on every page view.

Geometry: hips2fits TAN projection centered on (ra, dec), north up, east
LEFT; ``fov`` is the WIDTH in degrees (height = fov x H / W).
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HIPS2FITS = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"
HIPS = "CDS/P/DSS2/color"
WIDTH, HEIGHT = 1000, 667
TIMEOUT_S = 20.0
FAIL_RETRY_S = 3600.0
WIDE_FOV_DEG = 3.0          # fits the Piggy-600 frame (2.24 x 1.50 deg)
CLOSE_MIN_FOV_DEG = 0.62    # RC16 frame (0.41 deg wide) plus margin
CREDIT = ("DSS2 color via CDS hips2fits (CDS, Strasbourg; DSS: "
          "STScI / Caltech / AURA)")
VIEWS = ("wide", "close")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


# --- rig fields of view ---------------------------------------------------------

def rig_fov(config, rig: str) -> dict:
    """Field of view of one rig from its pixel scale and sensor size
    (sensor_width_px / sensor_height_px; piggyback_sensor_* overrides when
    set, 0 = same sensor as the RC16)."""
    from photonscript.shared.rigs import PIGGYBACK, rig_config, rig_label
    rc = rig_config(config, rig)
    w = int(getattr(config, "sensor_width_px", 6224) or 6224)
    h = int(getattr(config, "sensor_height_px", 4168) or 4168)
    if rig == PIGGYBACK:
        w = int(getattr(config, "piggyback_sensor_width_px", 0) or 0) or w
        h = int(getattr(config, "piggyback_sensor_height_px", 0) or 0) or h
    scale = float(getattr(rc, "pixel_scale_arcsec", 0.24) or 0.24)
    return {"rig": rig, "label": rig_label(config, rig),
            "pixel_scale_arcsec": scale, "width_px": w, "height_px": h,
            "w_arcmin": round(w * scale / 60.0, 1),
            "h_arcmin": round(h * scale / 60.0, 1)}


def rig_fovs(config) -> list[dict]:
    """Both rigs (the piggyback flagged by piggyback_enabled)."""
    from photonscript.shared.rigs import PIGGYBACK, RC16
    out = []
    for rig in (RC16, PIGGYBACK):
        f = rig_fov(config, rig)
        f["enabled"] = rig == RC16 or bool(getattr(config, "piggyback_enabled",
                                                     False))
        out.append(f)
    return out


def view_fov(view: str, size_arcmin: float | None) -> float:
    """Cutout width (deg): wide = 3.0; close = max(0.62, 2 x object size)."""
    if view == "wide":
        return WIDE_FOV_DEG
    size = float(size_arcmin or 0.0)
    return round(max(CLOSE_MIN_FOV_DEG, 2.0 * size / 60.0), 3)


# --- cutout cache -----------------------------------------------------------------

def slug(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", str(name or "")).strip("_").lower()
    return s or "target"


def cache_dir(config) -> Path:
    return Path(config.data_dir) / "refimg"


def cutout_url(ra_deg: float, dec_deg: float, fov_deg: float,
               width: int = WIDTH, height: int = HEIGHT) -> str:
    return (f"{HIPS2FITS}?hips={HIPS.replace('/', '%2F')}"
            f"&width={width}&height={height}&fov={fov_deg:.4f}"
            f"&projection=TAN&coordsys=icrs"
            f"&ra={ra_deg:.5f}&dec={dec_deg:.5f}&format=jpg")


def _paths(config, name: str, fov_deg: float) -> tuple[Path, Path, Path]:
    d, stem = cache_dir(config), f"{slug(name)}_{fov_deg:.2f}"
    return d / f"{stem}.jpg", d / f"{stem}.json", d / f"{stem}.fail.json"


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _http_get(url: str) -> bytes:
    import httpx
    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True, headers={
            "User-Agent": "PhotonScript/0.1 (AARO observatory; target page)"}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


def cached(config, name: str, fov_deg: float) -> dict | None:
    """Sidecar of a cached cutout (with "path"), or None. Never fetches."""
    img, meta, _fail = _paths(config, name, fov_deg)
    if img.exists() and meta.exists():
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
            d["path"] = str(img)
            return d
        except (OSError, ValueError):
            return None
    return None


def reference(config, name: str, ra_deg: float, dec_deg: float,
              fov_deg: float, *, fetch=None) -> dict | None:
    """The cached cutout's sidecar (+ "path"), fetching it the first time.
    None when it cannot be had (offline, service error, failure cached for an
    hour). fetch: url -> bytes (tests)."""
    hit = cached(config, name, fov_deg)
    if hit is not None:
        return hit
    img, meta, fail = _paths(config, name, fov_deg)
    with _lock(str(img)):
        hit = cached(config, name, fov_deg)
        if hit is not None:
            return hit
        try:
            f = json.loads(fail.read_text(encoding="utf-8"))
            if time.time() - float(f.get("t", 0)) < FAIL_RETRY_S:
                return None
        except (OSError, ValueError):
            pass
        url = cutout_url(ra_deg, dec_deg, fov_deg)
        try:
            data = (fetch or _http_get)(url)
            if not data or data[:2] != b"\xff\xd8":
                raise ValueError("not a JPEG (service error page?)")
        except Exception as e:  # noqa: BLE001 - offline is a normal state
            logger.info("Reference image for %s unavailable: %s", name, e)
            try:
                fail.parent.mkdir(parents=True, exist_ok=True)
                fail.write_text(json.dumps({"t": time.time(), "error": str(e)[:200],
                                            "url": url}), encoding="utf-8")
            except OSError:
                pass
            return None
        side = {
            "name": name, "source": "dss2", "url": url,
            "ra_deg": round(float(ra_deg), 6), "dec_deg": round(float(dec_deg), 6),
            "fov_deg": float(fov_deg),
            "fov_h_deg": round(float(fov_deg) * HEIGHT / WIDTH, 4),
            "width": WIDTH, "height": HEIGHT, "projection": "TAN",
            "orientation": "north up, east left",
            "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "credit": CREDIT,
        }
        try:
            img.parent.mkdir(parents=True, exist_ok=True)
            tmp = img.with_name(img.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(img)
            meta.write_text(json.dumps(side, indent=1), encoding="utf-8")
            fail.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not cache reference image %s: %s", img, e)
            return None
        side["path"] = str(img)
        return side


# --- geometry (mirrored in static/js/sky_geom.js; kept here so it is tested) --

def gnomonic_px(ra_deg: float, dec_deg: float, ra0_deg: float, dec0_deg: float,
                fov_deg: float, width: int = WIDTH, height: int = HEIGHT
                ) -> tuple[float, float] | None:
    """Pixel of (ra, dec) on a TAN cutout centered on (ra0, dec0), fov = the
    width in degrees, north up, east left. None behind the tangent plane."""
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    ra0, dec0 = math.radians(ra0_deg), math.radians(dec0_deg)
    cosc = (math.sin(dec0) * math.sin(dec)
            + math.cos(dec0) * math.cos(dec) * math.cos(ra - ra0))
    if cosc <= 0:
        return None
    x = math.cos(dec) * math.sin(ra - ra0) / cosc
    y = (math.cos(dec0) * math.sin(dec)
         - math.sin(dec0) * math.cos(dec) * math.cos(ra - ra0)) / cosc
    px_per_deg = width / fov_deg
    return (width / 2 - math.degrees(x) * px_per_deg,
            height / 2 - math.degrees(y) * px_per_deg)
