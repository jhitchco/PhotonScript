"""PS-67: where the telescope was pointing for each sub, and how far that
was from the target it was filed under.

Positions (degrees, the epoch the source reports):
  header      RC16 FITS: RA / DEC ("RA of telescope", the mount position),
              CENTALT, CENTAZ, AIRMASS, PIERSIDE. Piggy-600 frames carry none.
  mount-log   runs/<night>_mount.jsonl (shared.mount_log) at the exposure
              mid-time: how the Piggy-600 gets a position live.
  rc16-correlated  the RC16 header position nearest in time (Piggy-600,
              older nights without a mount log).
  solve       an ASTAP solve from scheduler.solve_store; wins over the mount
              position when present.

Sidecar ``runs/<night>_pointing.jsonl`` (append-only, one JSON object per
line, the LAST line for a (rig, file) wins):

  rig, file, t (exposure mid, UTC "...Z"), src (header | mount-log |
  rc16-correlated | solve), mount_src (where mount_* came from: header |
  mount-log | rc16-correlated), mount_ra, mount_dec, alt, az, airmass, pier
  (East | West), ha_h, solved_ra, solved_dec, rotation, scale,
  model_err_arcmin, model_err_pa (mount vs solve, RC16 only: a free TPoint
  model check), target, target_ra, target_dec, off_target_arcmin,
  off_target_pa, off_target_dir (compass word from the target to the frame),
  flag ("" | "flag" | "off-target"), drift_arcmin (since the rig's previous
  sub), at (when written).

The off-target limits come from shared.qa_rules.thresholds (per rig), so the
"On target" scorecard check and this record always agree. Nothing here
writes a FITS file.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


# ------------------------------------------------------------------ geometry

def sep_arcmin(ra1, dec1, ra2, dec2) -> float:
    """Great-circle separation in arcmin (inf when any input is None)."""
    from photonscript.scheduler.slew_gate import sep_arcmin as _sep
    return _sep(ra1, dec1, ra2, dec2)


def bearing(ra1, dec1, ra2, dec2) -> tuple[float, str] | tuple[None, None]:
    """Position angle (deg east of north) of point 2 seen from point 1, and
    its 8-point compass word ("NE"): which way the frame sits from the
    target."""
    if None in (ra1, dec1, ra2, dec2):
        return None, None
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    y = math.sin(r2 - r1) * math.cos(d2)
    x = math.cos(d1) * math.sin(d2) - math.sin(d1) * math.cos(d2) * math.cos(r2 - r1)
    pa = math.degrees(math.atan2(y, x)) % 360.0
    return round(pa, 1), COMPASS[int((pa + 22.5) // 45) % 8]


def fmt_ra(ra_deg) -> str:
    h = (float(ra_deg) % 360.0) / 15.0
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = (hh + 1) % 24, 0
    return f"{hh:02d}h{mm:02d}m"


def fmt_pos(ra, dec, pier=None, src="mount") -> str:
    """'mount RA 12h03m Dec +0.4, pier W'"""
    out = f"{src} RA {fmt_ra(ra)} Dec {float(dec):+.1f}"
    if pier:
        out += f", pier {str(pier)[:1].upper()}"
    return out


def alt_ha(ra_deg, dec_deg, when: datetime, lat_deg, lon_deg):
    """(altitude deg, hour angle h) from a GMST approximation; enough for
    the per-night pointing summary (same formula as scheduler.flexure)."""
    if ra_deg is None or dec_deg is None or when is None:
        return None, None
    jd = (when - datetime(2000, 1, 1, 12)).total_seconds() / 86400.0
    gmst = (280.46061837 + 360.98564736629 * jd) % 360.0
    ha = ((gmst + lon_deg - ra_deg + 180.0) % 360.0) - 180.0
    la, de, h = map(math.radians, (lat_deg, dec_deg, ha))
    alt = math.asin(max(-1.0, min(1.0, math.sin(la) * math.sin(de)
                                  + math.cos(la) * math.cos(de) * math.cos(h))))
    return math.degrees(alt), ha / 15.0


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def pier_name(v) -> str | None:
    """FITS PIERSIDE / ninaAPI SideOfPier -> East | West."""
    s = str(v or "").strip().lower()
    if s in ("east", "piereast", "0", "e"):
        return "East"
    if s in ("west", "pierwest", "1", "w"):
        return "West"
    return None


def from_header(hdr) -> dict | None:
    """Mount position of an RC16 sub from its FITS header:
    {mount_ra, mount_dec (deg), alt, az, airmass, pier, src: "header"}.
    None when the header has no coordinates (Piggy-600)."""
    if hdr is None:
        return None
    from photonscript.scheduler.identify import radec_from_header
    rd = radec_from_header(hdr)
    if rd is None:
        return None
    return {"src": "header", "mount_ra": round(rd[0], 5),
            "mount_dec": round(rd[1], 5),
            "alt": _round(_num(hdr.get("CENTALT")), 2),
            "az": _round(_num(hdr.get("CENTAZ")), 2),
            "airmass": _round(_num(hdr.get("AIRMASS")), 3),
            "pier": pier_name(hdr.get("PIERSIDE"))}


def _round(v, nd):
    return None if v is None else round(v, nd)


# ------------------------------------------------------------- target coords

_IDX: dict = {"t": 0.0, "v": None}
_IDX_TTL_S = 600.0


def _build_index(config) -> dict:
    """{target_key(name or catalog id): (name, ra_deg, dec_deg, is_project)};
    project targets win over the catalog."""
    from photonscript.shared.target_names import target_key
    out: dict = {}

    def add(t, is_project):
        for alias in (getattr(t, "name", ""), getattr(t, "catalog_id", "")):
            k = target_key(alias)
            if k and (k not in out or (is_project and not out[k][3])):
                out[k] = (t.name, float(t.ra_hours) * 15.0,
                          float(t.dec_degrees), is_project)
    try:
        from photonscript.scheduler.app import get_store
        for p in get_store().projects.values():
            add(p.target, True)
    except Exception:  # noqa: BLE001 - no store in this process / tests
        pass
    try:
        from photonscript.shared.astronomy import get_seasonal_targets
        for m in range(1, 13):
            for t in get_seasonal_targets(m):
                add(t, False)
    except Exception as e:  # noqa: BLE001
        logger.debug("catalog for pointing skipped: %s", e)
    return out


def coord_index(config) -> dict:
    now = time.monotonic()
    if _IDX["v"] is None or now - _IDX["t"] > _IDX_TTL_S:
        _IDX["v"] = _build_index(config)
        _IDX["t"] = now
    return _IDX["v"]


def target_coords(config, name) -> tuple[str, float, float] | None:
    """(known name, ra, dec) of a sub's target (project first, then the
    catalog, by name or catalog id; container names canonicalized, PS-78).
    None for '?' or an unknown name."""
    from photonscript.shared.target_names import canonical_target, target_key
    try:
        canon = canonical_target(name)
    except Exception:  # noqa: BLE001
        canon = str(name or "").strip() or None
    if not canon:
        return None
    hit = coord_index(config).get(target_key(canon))
    return (hit[0], hit[1], hit[2]) if hit else None


# ------------------------------------------------------------------ assess

def judged_position(rec: dict) -> tuple | None:
    """(ra, dec, label) the off-target test uses: the solve, else the mount."""
    if rec.get("solved_ra") is not None and rec.get("solved_dec") is not None:
        return rec["solved_ra"], rec["solved_dec"], "solved"
    if rec.get("mount_ra") is not None and rec.get("mount_dec") is not None:
        return rec["mount_ra"], rec["mount_dec"], "mount"
    return None


def assess(config, rig: str, rec: dict, target) -> dict:
    """Target, offset and flag for one pointing record (does not modify it):
    {target, target_ra, target_dec, off_target_arcmin, off_target_pa,
    off_target_dir, flag, note}. Offsets are None when the position or the
    target coordinates are unknown."""
    out = {"target": target if target not in (None, "") else None,
           "target_ra": None, "target_dec": None, "off_target_arcmin": None,
           "off_target_pa": None, "off_target_dir": None, "flag": "",
           "note": None}
    pos = judged_position(rec)
    tc = target_coords(config, target) if target else None
    if pos is None or tc is None:
        return out
    name, tra, tdec = tc
    off = sep_arcmin(tra, tdec, pos[0], pos[1])
    pa, comp = bearing(tra, tdec, pos[0], pos[1])
    from photonscript.shared.qa_rules import thresholds
    t = thresholds(config, rig or "rc16")
    flag = ("off-target" if off > t["offtarget_reject_arcmin"]
            else "flag" if off > t["offtarget_flag_arcmin"] else "")
    out.update(target=name, target_ra=round(tra, 5), target_dec=round(tdec, 5),
               off_target_arcmin=round(off, 2), off_target_pa=pa,
               off_target_dir=comp, flag=flag,
               note=f"{comp} from {name} ({fmt_pos(pos[0], pos[1], rec.get('pier'), pos[2])})")
    return out


def model_error(rec: dict) -> dict:
    """Mount vs solve for an RC16 record (the pointing-model error):
    {model_err_arcmin, model_err_pa}. Piggy-600: none (its center sits a
    fixed boresight offset from the mount position)."""
    if (rec.get("rig") or "rc16") != "rc16" or rec.get("solved_ra") is None \
            or rec.get("mount_ra") is None:
        return {}
    err = sep_arcmin(rec["mount_ra"], rec["mount_dec"], rec["solved_ra"],
                     rec["solved_dec"])
    pa, _ = bearing(rec["mount_ra"], rec["mount_dec"], rec["solved_ra"],
                    rec["solved_dec"])
    return {"model_err_arcmin": round(err, 2), "model_err_pa": pa}


# ------------------------------------------------------------------ sidecar

def sidecar_path(config, night: str) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "runs" / f"{night}_pointing.jsonl"


_KEEP = ("rig", "file", "t", "src", "mount_src", "mount_ra", "mount_dec",
         "alt", "az", "airmass", "pier", "ha_h", "solved_ra", "solved_dec", "rotation",
         "scale", "model_err_arcmin", "model_err_pa", "target", "target_ra",
         "target_dec", "off_target_arcmin", "off_target_pa", "off_target_dir",
         "flag", "drift_arcmin", "note")


def append_record(config, night: str, rec: dict) -> dict:
    """Append one pointing record (only the documented keys); returns what
    was written."""
    line = {k: rec.get(k) for k in _KEEP}
    line["rig"] = line["rig"] or "rc16"
    line["at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    p = sidecar_path(config, night)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError as e:
        logger.warning("pointing record not written for %s: %s",
                       rec.get("file"), e)
    return line


def load(config, night: str) -> dict[tuple[str, str], dict]:
    """(rig, file) -> the latest record. Never raises."""
    out: dict = {}
    try:
        text = sidecar_path(config, night).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("file"):
            out[(r.get("rig") or "rc16", r["file"])] = r
    return out


def same_record(a: dict | None, b: dict) -> bool:
    """True when b would add nothing over a (ignoring 'at')."""
    return a is not None and all(a.get(k) == b.get(k) for k in _KEEP)


# ------------------------------------------------------------------ summary

def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _group(pts, keyf):
    g: dict = {}
    for p in pts:
        k = keyf(p)
        if k is not None:
            g.setdefault(k, []).append(p["model_err_arcmin"])
    return {k: {"n": len(v), "median_arcmin": round(_median(v), 2)}
            for k, v in sorted(g.items())}


def _dec_band(p):
    d = _num(p.get("mount_dec"))
    if d is None:
        return None
    lo = int(math.floor(d / 30.0)) * 30
    return f"{lo:+d}..{lo + 30:+d}"


def _ha_band(p):
    h = _num(p.get("ha_h"))
    if h is None:
        return None
    lo = int(math.floor(h / 2.0)) * 2
    return f"{lo:+d}h..{lo + 2:+d}h"


def summarize(records) -> dict:
    """Per-night pointing summary: off-target counts per rig and the RC16
    mount vs plate-solve offset (median overall, by pier side, Dec band and
    hour-angle band). The model-offset numbers are what a TheSky / TPoint
    health check can trend night to night."""
    recs = list(records)
    by_rig: dict = {}
    for r in recs:
        rg = by_rig.setdefault(r.get("rig") or "rc16",
                               {"subs": 0, "with_position": 0, "solved": 0,
                                "flag": 0, "off_target": 0})
        rg["subs"] += 1
        rg["with_position"] += int(judged_position(r) is not None)
        rg["solved"] += int(r.get("solved_ra") is not None)
        rg["flag"] += int(r.get("flag") == "flag")
        rg["off_target"] += int(r.get("flag") == "off-target")
    pts = [r for r in recs if _num(r.get("model_err_arcmin")) is not None]
    model = None
    if pts:
        errs = [r["model_err_arcmin"] for r in pts]
        model = {"n": len(pts), "median_arcmin": round(_median(errs), 2),
                 "max_arcmin": round(max(errs), 2),
                 "by_pier": _group(pts, lambda p: p.get("pier")),
                 "by_dec": _group(pts, _dec_band),
                 "by_ha": _group(pts, _ha_band)}
    return {"rigs": by_rig, "off_target": sum(v["off_target"] for v in by_rig.values()),
            "flagged": sum(v["flag"] for v in by_rig.values()),
            "model": model}


def compact(rec: dict | None) -> dict | None:
    """The per-sub fields the runs page shows."""
    if not rec:
        return None
    return {k: rec.get(k) for k in (
        "src", "mount_ra", "mount_dec", "alt", "az", "pier", "solved_ra",
        "solved_dec", "off_target_arcmin", "off_target_dir", "flag",
        "model_err_arcmin", "drift_arcmin", "target")}


def iso_mid(start: datetime | None, exp_s) -> str | None:
    if start is None:
        return None
    mid = start + timedelta(seconds=(float(exp_s or 0) / 2.0))
    return mid.replace(microsecond=0).isoformat() + "Z"


# ------------------------------------------------------------- one sub

def sub_pointing(config, rig: str, hdr, start: datetime | None, exp_s,
                 target, mount_lines: list | None = None,
                 prev: dict | None = None, solve: dict | None = None,
                 base: dict | None = None) -> dict:
    """The pointing record for one sub (not yet written): mount position from
    the header (RC16), else the mount log at the exposure mid-time
    (Piggy-600), plus a stored solve when given; then target offset, the
    model error and the drift from `prev` (the rig's previous record).
    `base` = an already known mount position {src, mount_ra, mount_dec,
    alt, az, airmass, pier} (the dawn pass reuses stored ones instead of
    re-reading headers)."""
    rig = rig or "rc16"
    mid = (start + timedelta(seconds=float(exp_s or 0) / 2.0)) if start else None
    rec = dict(base) if base else (from_header(hdr) if hdr is not None else None)
    if rec is None and mount_lines and mid is not None:
        from photonscript.shared.mount_log import position_at
        m = position_at(mount_lines, mid)
        if m is not None:
            rec = {"src": "mount-log", "mount_ra": m.get("ra"),
                   "mount_dec": m.get("dec"), "alt": m.get("alt"),
                   "az": m.get("az"), "airmass": None, "pier": m.get("pier")}
    rec = dict(rec or {"src": None})
    rec.update(rig=rig, t=iso_mid(start, exp_s), mount_src=rec.get("src"))
    if solve and solve.get("solved") is not False and solve.get("ra") is not None:
        rec.update(src="solve", solved_ra=solve["ra"], solved_dec=solve["dec"],
                   rotation=solve.get("pa"), scale=solve.get("scale"))
    if rec.get("mount_ra") is not None and mid is not None:
        try:
            _alt, ha = alt_ha(rec["mount_ra"], rec["mount_dec"], mid,
                              float(config.observatory_lat),
                              float(config.observatory_lon))
            rec["ha_h"] = None if ha is None else round(ha, 3)
            if rec.get("alt") is None and _alt is not None:
                rec["alt"] = round(_alt, 2)
        except Exception:  # noqa: BLE001
            pass
    rec.update(model_error(rec))
    a = assess(config, rig, rec, target)
    rec.update(a)
    pos, ppos = judged_position(rec), judged_position(prev or {})
    if pos and ppos and (prev or {}).get("target") == rec.get("target"):
        d = sep_arcmin(ppos[0], ppos[1], pos[0], pos[1])
        rec["drift_arcmin"] = round(d, 2) if math.isfinite(d) else None
    return rec
