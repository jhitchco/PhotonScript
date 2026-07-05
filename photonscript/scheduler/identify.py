"""Identify unattributed subs by sky position.

Order of evidence: FITS header coordinates (NINA stamps the mount's
RA/DEC on every frame — free), then an ASTAP plate solve of one frame
per time cluster when headers are missing. The position is matched
against project targets and the seasonal catalog.
"""

from __future__ import annotations

import logging
import math
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MATCH_RADIUS_DEG = 2.5
CLUSTER_GAP_MIN = 30


def _parse_angle(val, sexagesimal_is_hours: bool) -> float | None:
    """Accept float degrees or sexagesimal 'HH MM SS' / 'DD MM SS'."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        pass
    parts = str(val).replace(":", " ").split()
    try:
        a, b, c = (list(map(float, parts)) + [0, 0, 0])[:3]
    except ValueError:
        return None
    sign = -1 if str(parts[0]).strip().startswith("-") else 1
    mag = abs(a) + b / 60 + c / 3600
    deg = sign * mag * (15 if sexagesimal_is_hours else 1)
    return deg


def _header_radec(path: Path) -> tuple[float, float] | None:
    from astropy.io import fits as _fits
    try:
        hdr = _fits.getheader(path)
    except Exception:  # noqa: BLE001
        return None
    for ra_k, dec_k, hours in (("RA", "DEC", False),
                               ("OBJCTRA", "OBJCTDEC", True),
                               ("CRVAL1", "CRVAL2", False)):
        ra = _parse_angle(hdr.get(ra_k), hours)
        dec = _parse_angle(hdr.get(dec_k), False)
        if ra is not None and dec is not None and -90 <= dec <= 90:
            return ra % 360, dec
    return None


def _astap_solve(config, path: Path) -> tuple[float, float] | None:
    exe = getattr(config, "astap_exe",
                  r"C:\Program Files\astap\astap.exe")
    if not Path(exe).exists():
        return None
    try:
        subprocess.run([exe, "-f", str(path), "-r", "30"],
                       capture_output=True, timeout=120)
        ini = path.with_suffix(".ini")
        if not ini.exists():
            return None
        kv = {}
        for line in ini.read_text(errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip().upper()] = v.strip()
        ini.unlink(missing_ok=True)
        path.with_suffix(".wcs").unlink(missing_ok=True)
        if kv.get("PLTSOLVD", "").upper().startswith("T") or "CRVAL1" in kv:
            return float(kv["CRVAL1"]) % 360, float(kv["CRVAL2"])
    except Exception as e:  # noqa: BLE001
        logger.warning("ASTAP solve failed for %s: %s", path.name, e)
    return None


def _sep_deg(ra1, dec1, ra2, dec2) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _candidates(config) -> list[tuple[str, float, float]]:
    from photonscript.shared.astronomy import get_seasonal_targets
    out, seen = [], set()
    try:
        from photonscript.scheduler.app import get_store
        for p in get_store().projects.values():
            t = p.target
            if t.name.lower() not in seen:
                seen.add(t.name.lower())
                out.append((t.name, t.ra_hours * 15, t.dec_degrees))
    except Exception:  # noqa: BLE001
        pass
    for m in range(1, 13):
        for t in get_seasonal_targets(m):
            if t.name.lower() not in seen:
                seen.add(t.name.lower())
                out.append((t.name, t.ra_hours * 15, t.dec_degrees))
    return out


def identify_night(config, date: str) -> dict:
    """Attribute a night's unknown subs by position, one cluster at a time."""
    from photonscript.scheduler.runs import _load_subs, _rewrite_subs

    subs = _load_subs(config, date)
    unknown = [s for s in subs
               if s.get("target") in ("?", "", None) and s.get("time")]
    if not unknown:
        return {"identified": 0, "clusters": []}
    unknown.sort(key=lambda s: s["time"])

    # time clusters (a slew gap means a new target)
    clusters, cur = [], [unknown[0]]
    for prev, s in zip(unknown, unknown[1:]):
        try:
            gap = (datetime.fromisoformat(s["time"][:19])
                   - datetime.fromisoformat(prev["time"][:19])
                   ).total_seconds() / 60
        except ValueError:
            gap = 0
        if gap > CLUSTER_GAP_MIN:
            clusters.append(cur)
            cur = []
        cur.append(s)
    clusters.append(cur)

    cands = _candidates(config)
    results, n_assigned = [], 0
    for cl in clusters:
        mid = cl[len(cl) // 2]
        path = Path(mid.get("abs_path") or "")
        coords = _header_radec(path) if path.exists() else None
        how = "header"
        if coords is None and path.exists():
            coords = _astap_solve(config, path)
            how = "plate solve"
        entry = {"window": f'{cl[0]["time"][11:16]}-{cl[-1]["time"][11:16]}',
                 "subs": len(cl), "method": how, "matched": None}
        if coords:
            ra, dec = coords
            entry["ra_deg"], entry["dec_deg"] = round(ra, 3), round(dec, 3)
            best = min(cands, key=lambda c: _sep_deg(ra, dec, c[1], c[2]),
                       default=None)
            if best and _sep_deg(ra, dec, best[1], best[2]) <= MATCH_RADIUS_DEG:
                entry["matched"] = best[0]
                for s in cl:
                    s["target"] = best[0]
                    n_assigned += 1
        results.append(entry)

    if n_assigned:
        by_file = {s.get("file"): s for s in unknown}
        for s in subs:
            u = by_file.get(s.get("file"))
            if u is not None:
                s["target"] = u["target"]
        _rewrite_subs(config, date, subs)
    logger.info("Identify %s: %d subs attributed across %d clusters",
                date, n_assigned, len(clusters))
    return {"identified": n_assigned, "clusters": results}
