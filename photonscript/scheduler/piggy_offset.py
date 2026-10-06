"""PS-26: center a Piggy-600-driven target in the 600 mm frame.

Both rigs ride one Paramount MX and NINA #1 centers by plate solving the
RC16 frame. The Piggy-600 boresight sits a fixed angle away from the
RC16's, so a target the Piggy-600 drives (ImagingProject.driving_rig
"piggyback", e.g. M31) lands off-center in the wide frame. This module
measures that boresight offset and turns it into the coordinates NINA #1
should center the RC16 on.

Measure. The PS-67 pointing sidecars (runs/<night>_pointing.jsonl) hold a
plate solve per sub when the dawn pass solved it (solved_ra / solved_dec /
rotation, through scheduler.solve_store). A Piggy-600 solve is paired with
the RC16 solve nearest in time (mid-exposure within PAIR_MAX_DT_S, same
target): both rigs point through the same mount at that moment, so the
difference of the two solve centers is the boresight offset, free of the
mount's pointing-model error and of dithers. Per pair:

    east, north   the Piggy-600 center in the tangent plane about the RC16
                  center (arcmin, east = increasing RA)
    rot           Piggy-600 position angle minus the RC16's (deg, report
                  only: the Piggy camera angle is set by hand)
    pier          the RC16 record's pier side (East | West)

Mount-position pairs are NOT used: the header / mount log is off by up to
about 1 deg before the TPoint model (PS-107), larger than the offset.

Per pier side the pairs are reduced robustly (median, sigma = 1.4826 x MAD,
3 sigma clipping, twice) and also night by night, so the panel shows the
night-to-night scatter. A German mount turns both OTAs 180 deg on the sky
at a meridian flip, so a side with too few pairs is inferred by negating
the other (flagged `inferred`). Store: <data_dir>/piggy_offset.json.

Center. For pier side p the RC16 must point at the desired Piggy center P
moved back by the offset: the inverse gnomonic projection of (-east,
-north) about P. Before the meridian (hour angle < 0) the Paramount sits
pier West; after it, pier East. The generator (nina_sequence_json) uses
the East coordinates for the target container (its slew, its center and
the meridian flip's re-center) and a nested pier-West centering container
that runs only before the target's transit.

Modes (config piggy_center_mode): preview (default, report only: the
sequence carries an annotation with the shift it would apply), on (apply),
off (nothing). RC16-driven targets never change.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STORE_NAME = "piggy_offset.json"
PIERS = ("East", "West")
MODES = ("preview", "on", "off")
RC16, PIGGYBACK = "rc16", "piggyback"
PAIR_MAX_DT_S = 450.0        # Piggy mid vs RC16 mid (a 900 s RC16 sub covers it)
CLIP_K = 3.0
CLIP_FLOOR_ARCMIN = 1.0      # never clip tighter than this (good nights agree)
SANITY_MAX_ARCMIN = 240.0    # a pair farther apart is a wrong match
STALE_S = 12 * 3600          # the generator re-measures an older store
KEEP_PAIRS = 300             # pairs kept in the store for the scatter plot
PIER_SWITCH_NOTE = ("pier West before the target's transit, pier East after "
                    "it (and after the meridian flip)")


# ------------------------------------------------------------------ config

def mode(cfg) -> str:
    m = str(getattr(cfg, "piggy_center_mode", "preview") or "preview")
    m = m.strip().lower()
    return m if m in MODES else "preview"


def _cfg_int(cfg, key, default):
    try:
        return int(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return default


def _cfg_float(cfg, key, default):
    try:
        return float(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ geometry

def tangent_offset(ra0, dec0, ra, dec) -> tuple[float, float]:
    """(east, north) arcmin of (ra, dec) in the gnomonic plane about
    (ra0, dec0). All inputs in degrees."""
    a0, d0, a, d = map(math.radians, (ra0, dec0, ra, dec))
    cosc = (math.sin(d0) * math.sin(d) + math.cos(d0) * math.cos(d)
            * math.cos(a - a0))
    xi = math.cos(d) * math.sin(a - a0) / cosc
    eta = (math.cos(d0) * math.sin(d) - math.sin(d0) * math.cos(d)
           * math.cos(a - a0)) / cosc
    return math.degrees(xi) * 60.0, math.degrees(eta) * 60.0


def tangent_inverse(ra0, dec0, east_arcmin, north_arcmin) -> tuple[float, float]:
    """(ra, dec) degrees of the point (east, north) arcmin in the gnomonic
    plane about (ra0, dec0)."""
    a0, d0 = math.radians(ra0), math.radians(dec0)
    xi = math.radians(east_arcmin / 60.0)
    eta = math.radians(north_arcmin / 60.0)
    rho = math.hypot(xi, eta)
    if rho == 0:
        return ra0 % 360.0, dec0
    c = math.atan(rho)
    dec = math.asin(math.cos(c) * math.sin(d0)
                    + eta * math.sin(c) * math.cos(d0) / rho)
    ra = a0 + math.atan2(xi * math.sin(c),
                         rho * math.cos(d0) * math.cos(c)
                         - eta * math.sin(d0) * math.sin(c))
    return math.degrees(ra) % 360.0, math.degrees(dec)


def rc16_center_for(ra_deg, dec_deg, east_arcmin, north_arcmin) -> tuple[float, float]:
    """Where the RC16 must center (ra, dec deg) so a boresight offset of
    (east, north) arcmin, measured about the RC16 center, puts the Piggy-600
    center on (ra_deg, dec_deg). Start from the offset reversed about the
    target and correct a few times (the tangent planes differ by a few
    arcsec at 30' and Dec 40)."""
    cra, cdec = tangent_inverse(ra_deg, dec_deg, -east_arcmin, -north_arcmin)
    for _ in range(6):
        lra, ldec = tangent_inverse(cra, cdec, east_arcmin, north_arcmin)
        de, dn = tangent_offset(ra_deg, dec_deg, lra, ldec)
        if math.hypot(de, dn) < 1e-4:
            break
        cra, cdec = tangent_inverse(cra, cdec, -de, -dn)
    return cra, cdec


def _wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def _parse_t(s) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).strip().rstrip("Z"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _pier(v) -> str | None:
    from photonscript.shared.pointing import pier_name
    return pier_name(v)


# ------------------------------------------------------------------ pairs

def _solved(recs, rig):
    out = []
    for r in recs:
        if (r.get("rig") or RC16) != rig:
            continue
        ra, dec, t = _num(r.get("solved_ra")), _num(r.get("solved_dec")), _parse_t(r.get("t"))
        if ra is None or dec is None or t is None:
            continue
        out.append((t, ra, dec, r))
    out.sort(key=lambda x: x[0])
    return out


def _same_target(a, b) -> bool:
    ta, tb = a.get("target"), b.get("target")
    if not ta or not tb:
        return True
    from photonscript.shared.target_names import target_key
    return target_key(ta) == target_key(tb)


def pairs_from_records(records, night: str = "",
                       max_dt_s: float = PAIR_MAX_DT_S) -> list[dict]:
    """Simultaneous (Piggy-600 solve, RC16 solve) pairs from pointing
    records (see module doc). Each Piggy sub pairs with the RC16 solve
    nearest in time; RC16 subs may be reused (one long RC16 sub covers
    several Piggy subs)."""
    recs = list(records or [])
    rc = _solved(recs, RC16)
    pg = _solved(recs, PIGGYBACK)
    out = []
    if not rc or not pg:
        return out
    for t, ra, dec, r in pg:
        best = None
        for t2, ra2, dec2, r2 in rc:
            dt = abs((t - t2).total_seconds())
            if dt <= max_dt_s and _same_target(r, r2) \
                    and (best is None or dt < best[0]):
                best = (dt, ra2, dec2, r2)
        if best is None:
            continue
        dt, ra2, dec2, r2 = best
        try:
            east, north = tangent_offset(ra2, dec2, ra, dec)
        except (ValueError, ZeroDivisionError):
            continue
        if math.hypot(east, north) > SANITY_MAX_ARCMIN:
            continue
        rot_p, rot_r = _num(r.get("rotation")), _num(r2.get("rotation"))
        out.append({
            "night": night, "t": r.get("t"), "target": r.get("target") or r2.get("target"),
            "pier": _pier(r2.get("pier")) or _pier(r.get("pier")),
            "east": round(east, 3), "north": round(north, 3),
            "rot": (round(_wrap180(rot_p - rot_r), 3)
                    if rot_p is not None and rot_r is not None else None),
            "dt_s": round(dt, 1), "piggy_file": r.get("file"),
            "rc16_file": r2.get("file")})
    return out


def night_pairs(cfg, night: str) -> list[dict]:
    from photonscript.shared import pointing
    return pairs_from_records(pointing.load(cfg, night).values(), night)


def list_nights(cfg, nights: int | None = None) -> list[str]:
    """Nights with a pointing sidecar, oldest first (the last `nights`)."""
    d = Path(getattr(cfg, "data_dir", ".")) / "runs"
    try:
        found = sorted({p.name[:10] for p in d.glob("*_pointing.jsonl")})
    except OSError:
        found = []
    n = nights if nights is not None else _cfg_int(cfg, "piggy_center_nights", 30)
    return found[-n:] if n and n > 0 else found


# ------------------------------------------------------------------ stats

def robust(values) -> tuple[float | None, float | None]:
    """(median, 1.4826 x MAD); (None, None) for no values."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None, None
    n = len(xs)
    med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    dev = sorted(abs(x - med) for x in xs)
    mad = dev[n // 2] if n % 2 else (dev[n // 2 - 1] + dev[n // 2]) / 2
    return med, 1.4826 * mad


def _clip(pairs) -> tuple[list, list]:
    """Keep pairs within CLIP_K sigma (floor CLIP_FLOOR_ARCMIN) of the
    median on both axes, twice. Returns (kept, rejected)."""
    kept = list(pairs)
    for _ in range(2):
        me, se = robust(p["east"] for p in kept)
        mn, sn = robust(p["north"] for p in kept)
        if me is None:
            break
        le = max(CLIP_K * (se or 0.0), CLIP_FLOOR_ARCMIN)
        ln = max(CLIP_K * (sn or 0.0), CLIP_FLOOR_ARCMIN)
        nxt = [p for p in kept
               if abs(p["east"] - me) <= le and abs(p["north"] - mn) <= ln]
        if len(nxt) == len(kept):
            break
        kept = nxt
    ids = {id(p) for p in kept}
    return kept, [p for p in pairs if id(p) not in ids]


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def side_stats(pairs) -> dict:
    """Robust offset of one pier side's pairs."""
    kept, rej = _clip(pairs)
    me, se = robust(p["east"] for p in kept)
    mn, sn = robust(p["north"] for p in kept)
    mr, sr = robust(p["rot"] for p in kept)
    by_night = {}
    for p in kept:
        by_night.setdefault(p["night"], []).append(p)
    nights = []
    for night in sorted(by_night):
        ps = by_night[night]
        ne, _ = robust(q["east"] for q in ps)
        nn, _ = robust(q["north"] for q in ps)
        nights.append({"night": night, "n": len(ps), "east": _r(ne),
                       "north": _r(nn)})
    _, n2n_e = robust(n["east"] for n in nights)
    _, n2n_n = robust(n["north"] for n in nights)
    return {"east": _r(me), "north": _r(mn),
            "total": _r(math.hypot(me, mn)) if me is not None else None,
            "sigma_east": _r(se), "sigma_north": _r(sn),
            "night_sigma_east": _r(n2n_e) if len(nights) > 1 else None,
            "night_sigma_north": _r(n2n_n) if len(nights) > 1 else None,
            "rot": _r(mr), "sigma_rot": _r(sr),
            "n_pairs": len(kept), "n_rejected": len(rej),
            "n_nights": len(nights), "nights": nights, "inferred": False}


def summarize(pairs, min_pairs: int) -> dict:
    """{pier: side stats} for East / West, a side under min_pairs inferred
    from the other (negated) when that one has enough."""
    out = {}
    for pier in PIERS:
        ps = [p for p in pairs if p.get("pier") == pier]
        out[pier] = side_stats(ps) if ps else None
    for pier, other in (("East", "West"), ("West", "East")):
        mine, theirs = out.get(pier), out.get(other)
        have = mine is not None and mine["n_pairs"] >= min_pairs
        can = theirs is not None and theirs["n_pairs"] >= min_pairs \
            and not theirs.get("inferred")
        if not have and can:
            out[pier] = {**{k: None for k in theirs}, "nights": [],
                         "east": _r(-theirs["east"]),
                         "north": _r(-theirs["north"]),
                         "total": theirs["total"], "rot": theirs["rot"],
                         "sigma_east": theirs["sigma_east"],
                         "sigma_north": theirs["sigma_north"],
                         "n_pairs": 0, "n_rejected": 0, "n_nights": 0,
                         "inferred": True,
                         "measured_pairs": mine["n_pairs"] if mine else 0}
    return out


def measure(cfg, nights: int | None = None) -> dict:
    """Pairs and per-pier offsets over the last `nights` nights of pointing
    sidecars. Pure: reads the sidecars, writes nothing."""
    min_pairs = _cfg_int(cfg, "piggy_center_min_pairs", 6)
    use = list_nights(cfg, nights)
    pairs = []
    for night in use:
        pairs += night_pairs(cfg, night)
    piers = summarize(pairs, min_pairs)
    no_pier = sum(1 for p in pairs if p.get("pier") not in PIERS)
    return {"measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "nights_scanned": len(use), "first_night": use[0] if use else None,
            "last_night": use[-1] if use else None, "n_pairs": len(pairs),
            "n_no_pier": no_pier, "min_pairs": min_pairs, "piers": piers,
            "pairs": pairs[-KEEP_PAIRS:]}


# ------------------------------------------------------------------ store

def store_path(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / STORE_NAME


def save(cfg, result: dict) -> None:
    p = store_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.warning("piggy offset store not written: %s", e)


_CACHE: dict = {"mtime": None, "path": None, "v": None}


def load(cfg) -> dict | None:
    """The stored measurement, or None. Cached on the file's mtime."""
    p = store_path(cfg)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    if _CACHE["path"] == str(p) and _CACHE["mtime"] == mt:
        return _CACHE["v"]
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _CACHE.update(mtime=mt, path=str(p), v=v)
    return v


def refresh(cfg, nights: int | None = None) -> dict:
    res = measure(cfg, nights)
    save(cfg, res)
    return res


def current(cfg, max_age_s: float = STALE_S) -> dict | None:
    """The store, re-measured (and saved) when missing or older than
    max_age_s. Never raises."""
    try:
        p = store_path(cfg)
        fresh = p.exists() and time.time() - p.stat().st_mtime < max_age_s
        if fresh:
            return load(cfg)
        return refresh(cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("piggy offset unavailable: %s", e)
        return load(cfg)


def usable(store: dict | None, pier: str, min_pairs: int) -> dict | None:
    """The pier side's offset when it can be applied (measured with
    min_pairs, or inferred from a side that was), else None."""
    s = ((store or {}).get("piers") or {}).get(pier)
    if not s or s.get("east") is None or s.get("north") is None:
        return None
    if s.get("inferred") or s.get("n_pairs", 0) >= min_pairs:
        return s
    return None


# ------------------------------------------------------------------ center

def _fmt_side(pier, s) -> str:
    src = ("inferred from the other side" if s.get("inferred") else
           f"{s.get('n_pairs')} pairs over {s.get('n_nights')} night(s), "
           f"+/- {s.get('sigma_east')}'/{s.get('sigma_north')}'")
    return (f"pier {pier}: Piggy-600 sits E {s['east']:+.1f}' N "
            f"{s['north']:+.1f}' of the RC16 ({src})")


def center_plan(cfg, ra_hours: float, dec_degrees: float,
                store: dict | None, frame_center: tuple | None = None) -> dict:
    """What NINA #1 should center on for a Piggy-600-driven target.

    frame_center = (ra_hours, dec_degrees) of the point the Piggy-600 frame
    should be centered on (a project option, e.g. between M31 and M110);
    None = the target. Returns {mode, applied, point_ra_hours,
    point_dec_degrees, by_pier: {pier: {ra_hours, dec_degrees, east, north,
    inferred}}, note}. applied is True only in mode "on" with an offset;
    in preview by_pier is filled but not used."""
    m = mode(cfg)
    pra, pdec = (frame_center if frame_center and None not in frame_center
                 else (ra_hours, dec_degrees))
    out = {"mode": m, "applied": False, "point_ra_hours": float(pra),
           "point_dec_degrees": float(pdec), "by_pier": {}, "note": ""}
    if m == "off":
        return out
    min_pairs = _cfg_int(cfg, "piggy_center_min_pairs", 6)
    cap = _cfg_float(cfg, "piggy_center_max_shift_arcmin", 90.0)
    point_txt = ("the project's frame center "
                 f"(RA {float(pra):.3f}h Dec {float(pdec):+.2f})"
                 if frame_center and None not in frame_center else "the target")
    sides = {p: usable(store, p, min_pairs) for p in PIERS}
    if not any(sides.values()):
        n = (store or {}).get("n_pairs", 0)
        out["note"] = (f"Piggy-600 centering (PS-26): no RC16-to-Piggy-600 "
                       f"offset measured yet ({n} simultaneous plate-solve "
                       f"pair(s), {min_pairs} needed per pier side); centering "
                       "on the target for the RC16, no shift")
        if frame_center and None not in frame_center:
            out["note"] += " (the frame center option needs the offset too)"
        return out
    if not all(sides.values()):
        missing = [p for p in PIERS if not sides[p]]
        out["note"] = (f"Piggy-600 centering (PS-26): offset for pier "
                       f"{', '.join(missing)} not measured; no shift")
        return out
    too_far = [p for p in PIERS
               if math.hypot(sides[p]["east"], sides[p]["north"]) > cap]
    if too_far:
        out["note"] = (f"Piggy-600 centering (PS-26): measured offset over "
                       f"piggy_center_max_shift_arcmin ({cap:g}') on pier "
                       f"{', '.join(too_far)}; no shift, check the "
                       "Guiding tab's Piggy-600 offset panel")
        return out
    ra_deg = float(pra) * 15.0
    for pier, s in sides.items():
        cra, cdec = rc16_center_for(ra_deg, float(pdec), s["east"], s["north"])
        out["by_pier"][pier] = {"ra_hours": round(cra / 15.0, 6),
                                "dec_degrees": round(cdec, 5),
                                "east": s["east"], "north": s["north"],
                                "inferred": bool(s.get("inferred"))}
    sides_txt = "; ".join(_fmt_side(p, sides[p]) for p in PIERS)
    if m == "on":
        out["applied"] = True
        out["note"] = (f"Piggy-600 centering (PS-26): the RC16 centers off "
                       f"target so {point_txt} lands in the middle of the "
                       f"Piggy-600 frame ({PIER_SWITCH_NOTE}). {sides_txt}. "
                       "The RC16 frame sits off-center by design.")
    else:
        out["note"] = (f"Piggy-600 centering (PS-26) PREVIEW, not applied "
                       f"(piggy_center_mode=preview): would center the RC16 "
                       f"so {point_txt} lands in the middle of the Piggy-600 "
                       f"frame. {sides_txt}.")
    return out


# ------------------------------------------------------------------ assess

def expected_rc16_center(cfg, target_name, pier) -> tuple[float, float] | None:
    """(ra_deg, dec_deg) where a deliberately shifted RC16 should point for
    a Piggy-600-driven target in mode "on" (pier from the sub's record;
    unknown pier = None). None when nothing is shifted, so the PS-67
    on-target check keeps judging against the target."""
    if mode(cfg) != "on" or not target_name:
        return None
    pier = _pier(pier)
    if pier is None:
        return None
    try:
        from photonscript.scheduler.app import get_store
        from photonscript.shared.target_names import target_key
        key = target_key(target_name)
        proj = next((p for p in get_store().projects.values()
                     if getattr(p, "driving_rig", RC16) == PIGGYBACK
                     and key in (target_key(p.target.name),
                                 target_key(p.target.catalog_id or ""))), None)
    except Exception:  # noqa: BLE001 - no store in this process / tests
        return None
    if proj is None:
        return None
    plan = center_plan(cfg, proj.target.ra_hours, proj.target.dec_degrees,
                       load(cfg), frame_center_of(proj))
    side = plan["by_pier"].get(pier) if plan["applied"] else None
    if not side:
        return None
    return side["ra_hours"] * 15.0, side["dec_degrees"]


def frame_center_of(obj) -> tuple | None:
    """(ra_hours, dec_degrees) of a project's / sequence target's frame
    center option, or None."""
    ra = getattr(obj, "frame_center_ra_hours", None)
    dec = getattr(obj, "frame_center_dec_degrees", None)
    return (float(ra), float(dec)) if ra is not None and dec is not None else None


# ------------------------------------------------------------------ report

def format_report(res: dict) -> str:
    lines = [f"Piggy-600 boresight offset (PS-26), {res.get('nights_scanned', 0)} "
             f"night(s) {res.get('first_night') or '-'} to "
             f"{res.get('last_night') or '-'}: {res.get('n_pairs', 0)} "
             f"simultaneous solve pair(s)"]
    for pier in PIERS:
        s = (res.get("piers") or {}).get(pier)
        if not s:
            lines.append(f"  pier {pier}: no pairs")
            continue
        if s.get("inferred"):
            lines.append(f"  pier {pier}: E {s['east']:+.2f}' N {s['north']:+.2f}' "
                         "(inferred from the other side)")
            continue
        lines.append(
            f"  pier {pier}: E {s['east']:+.2f}' N {s['north']:+.2f}' "
            f"(|{s['total']}'|), sigma {s['sigma_east']}'/{s['sigma_north']}', "
            f"rotation {s['rot']} deg, {s['n_pairs']} pairs "
            f"({s['n_rejected']} clipped) over {s['n_nights']} night(s)")
        for n in s.get("nights") or []:
            lines.append(f"      {n['night']}: E {n['east']:+.2f}' N "
                         f"{n['north']:+.2f}' ({n['n']} pairs)")
    return "\n".join(lines)
