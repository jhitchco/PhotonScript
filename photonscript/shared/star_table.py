"""PS-80 (backend part A): per-sub star sidecar for the review overlay.

Both graders already hold the detected stars in memory; this writes the
brightest N of them (the same stars the medians come from) as compact arrays
so the viewer can draw circles colored by HFR or eccentricity without
re-opening the FITS:

    <data_dir>/stars/<night>/<rig>/<file stem>.json
    {"v": 1, "rig", "grader", "w", "h", "n", "ecc_def",
     "x": [...], "y": [...], "hfr": [...], "ecc": [...], "theta": [...]}

x, y, hfr in native pixels; theta in radians (sep). About 20 KB for 500
stars. Best-effort: a failed write never costs a grade.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAX = 500


def sidecar_path(config, night: str, rel_file: str, rig: str = "rc16") -> Path:
    stem = Path(str(rel_file).replace("\\", "/")).stem
    return (Path(getattr(config, "data_dir", ".")) / "stars" / night
            / (rig or "rc16") / f"{stem}.json")


def _clean(v, nd):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def build(x, y, hfr, ecc, theta=None, flux=None, *, w=None, h=None,
          scale: float = 1.0, limit: int = DEFAULT_MAX, grader: str = "",
          rig: str = "rc16", ecc_def: str = "") -> dict | None:
    """Top `limit` stars by flux (input order when flux is None).
    `scale` multiplies x, y and hfr (2.0 for the backfill's 2x2 binning)."""
    n = min(len(x), len(y), len(hfr), len(ecc))
    if n == 0 or limit <= 0:
        return None
    idx = list(range(n))
    if flux is not None and len(flux) >= n:
        idx.sort(key=lambda i: -(float(flux[i]) if flux[i] is not None
                                 and math.isfinite(float(flux[i])) else 0.0))
    idx = idx[:limit]
    th = theta if theta is not None and len(theta) >= n else None
    return {
        "v": 1, "rig": rig, "grader": grader, "ecc_def": ecc_def,
        "w": int(w * scale) if w else None, "h": int(h * scale) if h else None,
        "n": len(idx),
        "x": [_clean(float(x[i]) * scale, 1) for i in idx],
        "y": [_clean(float(y[i]) * scale, 1) for i in idx],
        "hfr": [_clean(float(hfr[i]) * scale, 2) for i in idx],
        "ecc": [_clean(ecc[i], 3) for i in idx],
        "theta": [_clean(th[i], 3) for i in idx] if th is not None else None,
    }


def write(config, night: str, rel_file: str, table: dict | None,
          rig: str = "rc16") -> Path | None:
    if not table:
        return None
    try:
        p = sidecar_path(config, night, rel_file, rig)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(table, separators=(",", ":")), encoding="utf-8")
        return p
    except Exception as e:  # noqa: BLE001
        logger.debug("star sidecar write skipped for %s: %s", rel_file, e)
        return None


def read(config, night: str, rel_file: str, rig: str = "rc16") -> dict | None:
    p = sidecar_path(config, night, rel_file, rig)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
