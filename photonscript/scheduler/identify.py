"""Identify unattributed subs by sky position.

Order of evidence: FITS header coordinates (NINA stamps the mount's
RA/DEC on every frame — free), then an ASTAP plate solve of one frame
per time cluster when headers are missing. The position is matched
against project targets and the seasonal catalog.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MATCH_RADIUS_DEG = 2.5
CLUSTER_GAP_MIN = 30


def _maybe_stamp(config, abs_path, name: str) -> None:
    """Write the attributed name into the sub's FITS OBJECT header so the
    science file itself carries the target once identify has matched it."""
    if not getattr(config, "stamp_fits_object", True):
        return
    try:
        from photonscript.shared.fits_object import stamp_object
        stamp_object(abs_path, name)
    except Exception as e:  # noqa: BLE001
        logger.debug("identify OBJECT stamp skipped for %s: %s", abs_path, e)


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
    return radec_from_header(hdr)


def radec_from_header(hdr) -> tuple[float, float] | None:
    """Mount/plate coordinates (deg) from an already-open FITS header or any
    mapping with .get(); None when the frame carries no position (Piggy-600:
    NINA #2 owns no mount)."""
    if hdr is None:
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
    """(ra, dec) deg of one frame by ASTAP (-r 30, no FOV hint), via the
    PS-96 solve store's runner (which never rewrites the FITS and keeps
    ASTAP's .ini/.wcs out of the capture folder). Attribution only: not
    stored."""
    from photonscript.scheduler.solve_store import solve
    sol = solve(config, path)
    if sol is None:
        return None
    return sol["ra"], sol["dec"]


def _sep_deg(ra1, dec1, ra2, dec2) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def match_target(ra: float, dec: float,
                 cands: list[tuple[str, float, float, bool]]) -> str | None:
    """Nearest candidate within MATCH_RADIUS_DEG. Project targets win over
    the seasonal catalog (PS-51): M31 subs must credit the M31 campaign, not a
    catalog neighbour such as M32/M110 that happens to sit a little closer to
    the mount's pointing."""
    def _best(pool):
        hit = min(pool, key=lambda c: _sep_deg(ra, dec, c[1], c[2]),
                  default=None)
        if hit and _sep_deg(ra, dec, hit[1], hit[2]) <= MATCH_RADIUS_DEG:
            return hit[0]
        return None
    return (_best([c for c in cands if len(c) > 3 and c[3]])
            or _best(cands))


_CAND_CACHE: dict = {"t": 0.0, "v": None}
_CAND_TTL_S = 600.0


def cached_candidates(config) -> list[tuple[str, float, float, bool]]:
    """_candidates() with a short TTL, for the live per-sub path (the agent
    calls this once per new frame; the catalog walk is not free)."""
    import time
    now = time.monotonic()
    if _CAND_CACHE["v"] is None or now - _CAND_CACHE["t"] > _CAND_TTL_S:
        _CAND_CACHE["v"] = _candidates(config)
        _CAND_CACHE["t"] = now
    return _CAND_CACHE["v"]


def target_from_header(config, hdr) -> str | None:
    """Live attribution (PS-51): match a new sub's header RA/DEC to a project
    or catalog target. None when the header has no coordinates or nothing is
    within MATCH_RADIUS_DEG."""
    coords = radec_from_header(hdr)
    if coords is None:
        return None
    try:
        return match_target(*coords, cached_candidates(config))
    except Exception as e:  # noqa: BLE001
        logger.debug("live header match failed: %s", e)
        return None


def _candidates(config) -> list[tuple[str, float, float, bool]]:
    from photonscript.shared.astronomy import get_seasonal_targets
    out, seen = [], set()
    try:
        from photonscript.scheduler.app import get_store
        for p in get_store().projects.values():
            t = p.target
            if t.name.lower() not in seen:
                seen.add(t.name.lower())
                out.append((t.name, t.ra_hours * 15, t.dec_degrees, True))
    except Exception:  # noqa: BLE001
        pass
    for m in range(1, 13):
        for t in get_seasonal_targets(m):
            if t.name.lower() not in seen:
                seen.add(t.name.lower())
                out.append((t.name, t.ra_hours * 15, t.dec_degrees, False))
    return out


def identify_night(config, date: str, solve: bool = True) -> dict:
    """Attribute unknown subs by sky position.

    Every sub with header coordinates is matched INDIVIDUALLY (NINA stamps
    the mount RA/DEC on each frame), so back-to-back target handoffs tag
    correctly. Only subs without coordinates fall back to one ASTAP solve
    per time cluster, and only when solve=True (the library build runs the
    cheap header pass only; runs.attribute_night orders the passes).
    """
    from photonscript.scheduler.runs import _load_subs, _rewrite_subs
    from photonscript.shared.target_names import canonical_target

    subs = _load_subs(config, date)
    # PS-78: a sub named after a structural loop container
    # (OSC_LIGHT_LOOP_Container) is as unknown as '?'
    unknown = [s for s in subs
               if canonical_target(s.get("target")) is None
               and s.get("time")]
    if not unknown:
        return {"identified": 0, "clusters": []}
    unknown.sort(key=lambda s: s["time"])
    cands = _candidates(config)

    def _match(ra, dec):
        return match_target(ra, dec, cands)

    # Pass 1: per-sub header coordinates
    n_assigned = 0
    no_coords = []
    header_hits: dict[str, dict] = {}
    for s in unknown:
        path = Path(s.get("abs_path") or "")
        coords = _header_radec(path) if path.exists() else None
        if coords is None:
            no_coords.append(s)
            continue
        name = _match(*coords)
        if name:
            s["target"] = name
            n_assigned += 1
            _maybe_stamp(config, path, name)
            e = header_hits.setdefault(name, {"matched": name, "subs": 0,
                                              "method": "header (per-sub)",
                                              "first": s["time"][11:16],
                                              "last": s["time"][11:16]})
            e["subs"] += 1
            e["last"] = s["time"][11:16]
        else:
            e = header_hits.setdefault("(no match)", {
                "matched": None, "subs": 0, "method": "header (per-sub)",
                "ra_deg": round(coords[0], 3), "dec_deg": round(coords[1], 3),
                "first": s["time"][11:16], "last": s["time"][11:16]})
            e["subs"] += 1
            e["last"] = s["time"][11:16]
    results = [{"window": f'{e.pop("first")}-{e.pop("last")}', **e}
               for e in header_hits.values()]

    # Pass 2: coordinate-less subs -> one ASTAP solve per time cluster
    if no_coords and solve:
        clusters, cur = [], [no_coords[0]]
        for prev, s in zip(no_coords, no_coords[1:]):
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
        for cl in clusters:
            mid = cl[len(cl) // 2]
            path = Path(mid.get("abs_path") or "")
            coords = _astap_solve(config, path) if path.exists() else None
            entry = {"window": f'{cl[0]["time"][11:16]}-{cl[-1]["time"][11:16]}',
                     "subs": len(cl), "method": "plate solve",
                     "matched": None}
            if coords:
                entry["ra_deg"] = round(coords[0], 3)
                entry["dec_deg"] = round(coords[1], 3)
                name = _match(*coords)
                if name:
                    entry["matched"] = name
                    for s in cl:
                        s["target"] = name
                        n_assigned += 1
                        _maybe_stamp(config, s.get("abs_path"), name)
            results.append(entry)

    if n_assigned:
        by_file = {s.get("file"): s.get("target") for s in unknown}
        for s in subs:
            if s.get("file") in by_file:
                s["target"] = by_file[s.get("file")]
        _rewrite_subs(config, date, subs)
    logger.info("Identify %s: %d subs attributed (%d groups)",
                date, n_assigned, len(results))
    return {"identified": n_assigned, "clusters": results}
