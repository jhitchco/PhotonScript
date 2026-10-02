"""PS-96 (first piece of PS-67): plate-solve one sub and keep the answer.

solve(config, path, rig) runs ASTAP on a FITS, reads the solution (CRVAL +
CD matrix) and returns {ra, dec, pa, scale, cd, parity}. With a night it
also appends the record to

    <data_dir>/solves/<night>/<rig>.jsonl

so a later report reads solves instead of re-running ASTAP (load / lookup).

Never passes ASTAP -update (that would rewrite the science FITS). ASTAP's
.ini/.wcs outputs go to a temporary folder via -o, so nothing is written
next to the capture (or into a receive-only Syncthing mirror). The solver
call is injectable (`runner`) so this is testable without ASTAP.

Geometry: with the CD matrix (deg/px), a pixel offset (dx, dy) is a sky
offset east = CD1_1*dx + CD1_2*dy, north = CD2_1*dx + CD2_2*dy (degrees,
east = increasing RA). `pa` is the position angle of the image +y axis,
degrees east of north; `parity` is -1 for the usual sky view (det CD < 0)
and +1 for a mirrored one.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def store_path(config, night: str, rig: str) -> Path:
    return (Path(getattr(config, "data_dir", ".")) / "solves" / night
            / f"{rig or 'rc16'}.jsonl")


def _parse_kv(text: str) -> dict:
    kv = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip().upper()] = v.split("/")[0].strip().strip("'").strip()
    return kv


def _astap_runner(exe: str, path: Path, fov_deg: float | None,
                  hint: tuple[float, float] | None, radius_deg: float,
                  timeout_s: float) -> dict | None:
    """Run ASTAP once; return the .ini key/values, or None."""
    with tempfile.TemporaryDirectory(prefix="ps_solve_") as td:
        out = Path(td) / path.stem
        args = [exe, "-f", str(path), "-r", f"{radius_deg:g}", "-o", str(out)]
        if fov_deg:
            args += ["-fov", f"{fov_deg:.4f}"]
        if hint is not None:
            args += ["-ra", f"{(hint[0] % 360) / 15.0:.6f}",
                     "-spd", f"{hint[1] + 90.0:.6f}"]
        subprocess.run(args, capture_output=True, timeout=timeout_s)
        ini = out.with_suffix(".ini")
        if ini.exists():
            return _parse_kv(ini.read_text(errors="replace"))
    # an ASTAP build that ignored -o wrote next to the FITS (the old
    # identify behavior): read it and clean up the same way
    ini = path.with_suffix(".ini")
    if not ini.exists():
        return None
    try:
        return _parse_kv(ini.read_text(errors="replace"))
    finally:
        ini.unlink(missing_ok=True)
        path.with_suffix(".wcs").unlink(missing_ok=True)


def _f(kv, k):
    try:
        return float(kv[k])
    except (KeyError, TypeError, ValueError):
        return None


def solution_from_kv(kv: dict | None) -> dict | None:
    """ASTAP .ini / WCS keys -> {ra, dec, pa, scale, cd, parity}; None when
    unsolved. Falls back to CDELT + CROTA2 when no CD matrix is given."""
    if not kv:
        return None
    if str(kv.get("PLTSOLVD", "T")).upper().startswith("F"):
        return None
    ra, dec = _f(kv, "CRVAL1"), _f(kv, "CRVAL2")
    if ra is None or dec is None:
        return None
    cd = [[_f(kv, "CD1_1"), _f(kv, "CD1_2")], [_f(kv, "CD2_1"), _f(kv, "CD2_2")]]
    if any(v is None for row in cd for v in row):
        c1, c2, rot = _f(kv, "CDELT1"), _f(kv, "CDELT2"), _f(kv, "CROTA2") or 0.0
        if c1 is None or c2 is None:
            cd = None
        else:
            t = math.radians(rot)
            cd = [[c1 * math.cos(t), -c2 * math.sin(t)],
                  [c1 * math.sin(t), c2 * math.cos(t)]]
    out = {"ra": round(ra % 360, 6), "dec": round(dec, 6),
           "pa": None, "scale": None, "cd": None, "parity": None}
    if cd:
        det = cd[0][0] * cd[1][1] - cd[0][1] * cd[1][0]
        out["cd"] = [[float(v) for v in row] for row in cd]
        out["scale"] = round(math.sqrt(abs(det)) * 3600.0, 5)
        out["pa"] = round(math.degrees(math.atan2(cd[0][1], cd[1][1])) % 360, 3)
        out["parity"] = -1 if det < 0 else 1
    return out


def pix_to_sky(cd, dx: float, dy: float) -> tuple[float, float]:
    """Pixel offset -> (east, north) in arcsec through a CD matrix."""
    return ((cd[0][0] * dx + cd[0][1] * dy) * 3600.0,
            (cd[1][0] * dx + cd[1][1] * dy) * 3600.0)


def nominal_cd(scale_arcsec: float, pa_deg: float = 0.0, parity: int = -1):
    """A CD matrix for a known scale and position angle (the inverse of
    solution_from_kv's pa/parity), for tests and for a fallback when a rig
    was never solved."""
    s = scale_arcsec / 3600.0
    t = math.radians(pa_deg)
    # +y axis -> (east, north) = s*(sin pa, cos pa); +x is +y turned by
    # -90 deg (parity -1, the sky as seen) or +90 deg (mirrored).
    yx, yy = s * math.sin(t), s * math.cos(t)
    xx, xy = (-yy, yx) if parity < 0 else (yy, -yx)
    return [[xx, yx], [xy, yy]]


def _fov_hint(config, path: Path, rig: str | None) -> float | None:
    if rig is None:
        return None
    try:
        from photonscript.shared.rigs import rig_config
        scale = float(getattr(rig_config(config, rig), "pixel_scale_arcsec", 0) or 0)
        from astropy.io import fits as _fits
        h = int(_fits.getheader(str(path)).get("NAXIS2") or 0)
        return h * scale / 3600.0 if scale > 0 and h > 0 else None
    except Exception:  # noqa: BLE001
        return None


def solve(config, path, rig: str | None = None, night: str | None = None,
          hint: tuple[float, float] | None = None, rel_file: str | None = None,
          start_utc: str | None = None, runner=None,
          radius_deg: float = 30.0, timeout_s: float = 120.0) -> dict | None:
    """Plate-solve `path`. Returns the solution dict (see module doc) plus
    file/rig, or None (no ASTAP, no file, not solved). `hint` = (ra, dec)
    deg seeds the search (the Piggy-600 has no mount position in its
    header: pass the simultaneous RC16 pointing). With `night`, successful
    AND failed attempts are appended to the store so a report knows what
    was tried."""
    path = Path(path)
    if runner is None:
        exe = getattr(config, "astap_exe", r"C:\Program Files\astap\astap.exe")
        if not Path(exe).exists() or not path.exists():
            return None

        def runner(p, fov, h, r, t):  # noqa: E306
            return _astap_runner(exe, p, fov, h, r, t)
    try:
        kv = runner(path, _fov_hint(config, path, rig), hint, radius_deg,
                    timeout_s)
        sol = solution_from_kv(kv)
    except Exception as e:  # noqa: BLE001
        logger.warning("ASTAP solve failed for %s: %s", path.name, e)
        sol = None
    rec = {"file": rel_file or path.name, "rig": rig, "solved": sol is not None,
           "start_utc": start_utc, "solver": "astap",
           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           **(sol or {})}
    if night:
        try:
            p = store_path(config, night, rig or "unknown")
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError as e:
            logger.debug("solve store write skipped: %s", e)
    return rec if sol is not None else None


def load(config, night: str, rig: str) -> list[dict]:
    """Every stored attempt for a night and rig (oldest first)."""
    p = store_path(config, night, rig)
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def lookup(config, night: str, rig: str) -> dict[str, dict]:
    """file -> its latest stored attempt (solved or not)."""
    return {r.get("file"): r for r in load(config, night, rig)}
