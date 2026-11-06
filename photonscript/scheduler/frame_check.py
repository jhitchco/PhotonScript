"""Frame check: grade NINA's last frames (snapshots too) with the sub star QA.

2026-10-13: two 180 s NINA Imaging-tab snapshots (ProTrack off, then on)
tested tracking, but PhotonScript never graded them: NINA marks them
ImageType SNAPSHOT and they are not sequence lights, and downloading the raw
FITS through ninaAPI (GET /image/{i}?raw_fits=true) timed out over
Tailscale.

On the scope PC this reads NINA's image history (ninaAPI GET
/image-history?all=true: Filename, Date, ImageType, ExposureTime, Filter),
finds each file under NINA's image save path (the profile's pattern is
<FilePath>\\<DATEMINUS12>\\<IMAGETYPE>\\<file>), and measures it with the
function the sub graders use (star_measure.measure_frame through
runs._measure_native: HFR, FWHM", eccentricity, star count, background),
plus the elongation axis of the stretched stars (optics_report.axial_stats,
as the tracking test reads it) and the trail of the median bright star.

Trail: a star smeared along a line of length L has second moments
a^2 = s^2 + L^2 / 12 along the line and b^2 = s^2 across it, so
L = sqrt(12 (a^2 - b^2)) (sep's a / b, native px). Seeing alone leaves a
small L; a tracking drift shows as a larger one, in arcsec and in "/min over
the exposure. The axis is named RA or Dec only when the camera position angle
is known (FITS WCS / rotator keys, else the rig's newest plate solve, else
--pa) and the camera sits near square to the sky.

ninaAPI offers no star list or crop (only a stretched, 8-bit JPEG / PNG
preview, not fit for star shapes), so away from the scope PC the reduced,
fast form is this module's own answer: GET /api/frame-check on the scope's
scheduler (routers/frame_check.py), a few hundred bytes of JSON. Read only:
nothing is written, nothing is sent to NINA.

Optics or tracking (2026-10-23, optics_vs_tracking): each frame also gets a
corner analysis (optics_report.sub_field_map: center and corner FWHM, the
soft corner, a tilt / curvature / collimation / tracking verdict), and
--compare judges the frames together: a trail that does not grow with the
exposure (x1.8 apart, ratio under 1.3), trails in no common direction, or
the other rig (--control) round over the same time where the drift would
show say optics (focus / tilt / collimation), never shorter subs.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SCOPE_EXE = "C:\\astro\\venv\\Scripts\\photonscript.exe"
ECC_MARGIN = 0.05        # round = this far under the rig's ecc gate
TRAIL_ROUND_ARCSEC = 1.0  # a "trail" under this is seeing, not drift
LABELS = "ABCDEFGHIJ"
_CACHE: dict = {}        # (path, mtime, size, pa) -> measured row


# ------------------------------------------------------------- NINA history

def nina_url(config, rig: str) -> str:
    from photonscript.shared.rigs import rig_config
    return str(getattr(rig_config(config, rig), "nina_base_url", "")).rstrip("/")


def _get_json(url: str, timeout: float = 10.0):
    import httpx
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    return body.get("Response", body) if isinstance(body, dict) else body


def history(config, rig: str = "rc16", base_url: str = "", get=None) -> list[dict]:
    """NINA's image history, oldest first, each entry with its `index` (the
    position ninaAPI's /image/{index} uses). Raises when NINA is unreachable."""
    get = get or _get_json
    body = get(f"{(base_url or nina_url(config, rig)).rstrip('/')}"
               "/image-history?all=true")
    rows = body if isinstance(body, list) else []
    return [dict(r, index=i) for i, r in enumerate(rows) if isinstance(r, dict)]


def pick(entries: list[dict], last: int = 2, index: list[int] | None = None,
         image_type: str = "") -> list[dict]:
    """The entries to check: the given NINA indexes, else the last `last`
    (of `image_type` when given, e.g. SNAPSHOT)."""
    if index:
        want = {int(i) for i in index}
        return [e for e in entries if e["index"] in want]
    if image_type:
        entries = [e for e in entries
                   if str(e.get("ImageType") or "").upper() == image_type.upper()]
    return entries[-max(1, int(last or 1)):]


def _local(date_s) -> datetime | None:
    """NINA's Date ("2026-10-13T20:25:54.1734868-06:00", local with its
    offset) as naive local time."""
    try:
        return datetime.strptime(str(date_s or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def save_roots(config, rig: str, profile_path: str = "") -> list[Path]:
    from photonscript.shared.rigs import rig_config
    roots = []
    for d in (profile_path, getattr(rig_config(config, rig), "image_watch_dir", "")):
        if d and Path(d) not in roots:
            roots.append(Path(d))
    return roots


def locate(roots: list[Path], entry: dict, deep: bool = True) -> Path | None:
    """The file of one history entry under NINA's save path: the profile
    pattern's folder first (<DATEMINUS12>\\<IMAGETYPE>), then any folder of
    that night or the night next to it, then (deep) the whole tree."""
    name = Path(str(entry.get("Filename") or "").replace("\\", "/")).name
    if not name:
        return None
    t = _local(entry.get("Date"))
    nights = []
    if t is not None:
        n0 = (t - timedelta(hours=12)).date()
        nights = [str(n0), str(n0 - timedelta(days=1)), str(n0 + timedelta(days=1))]
    itype = str(entry.get("ImageType") or "")
    for root in roots:
        for n in nights[:1]:
            for p in (root / n / itype / name, root / n / itype.upper() / name):
                if p.is_file():
                    return p
        for n in nights:
            d = root / n
            if d.is_dir():
                hit = next((p for p in d.rglob(name) if p.is_file()), None)
                if hit:
                    return hit
    if deep:
        for root in roots:
            if root.is_dir():
                hit = next((p for p in root.rglob(name) if p.is_file()), None)
                if hit:
                    return hit
    return None


# ------------------------------------------------------------- measuring

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def trail_px(a, b, flux=None) -> float | None:
    """Median trail length (native px) of the brighter half of the stars:
    sqrt(12 (a^2 - b^2)) per star."""
    a, b = list(a if a is not None else []), list(b if b is not None else [])
    if not a or len(a) != len(b):
        return None
    idx = list(range(len(a)))
    if flux is not None and len(flux) == len(a) and len(a) >= 6:
        f = list(flux)
        idx = sorted(idx, key=lambda i: f[i], reverse=True)[:max(3, len(a) // 2)]
    ls = [math.sqrt(12.0 * max(0.0, float(a[i]) ** 2 - float(b[i]) ** 2))
          for i in idx]
    return _median(ls)


def corner_analysis(st: dict, w: int, h: int, scale: float) -> dict | None:
    """Center vs corner star size and shape of one frame (the PS-95 field
    map, optics_report.sub_field_map, over this frame's star list): center
    and corner FWHM-eq ("), corner ecc, the soft and sharp corners, the
    corner ratio and the optics verdict (tilt / curvature / collimation /
    tracking / fine / unclear). None when there is no star list."""
    try:
        from photonscript.scheduler import optics_report as orp
        from photonscript.shared.star_shape import ECC_DEF
        xs = list(st.get("x", []))
        if not xs:
            return None
        tbl = {"x": xs, "y": list(st.get("y", [])), "hfr": list(st.get("hfr", [])),
               "ecc": list(st.get("ecc", [])), "theta": list(st.get("theta", [])),
               "w": w or None, "h": h or None, "ecc_def": ECC_DEF}
        m = orp.sub_field_map(tbl, scale, min_zone=5)
        if not m.get("ok"):
            return {"ok": False, "why": m.get("why")}
        c = orp.classify(m)
        zones = m.get("zones") or {}
        return {"ok": True, "center_fwhm": m.get("center_fwhm"),
                "corner_fwhm": m.get("corner_fwhm"),
                "corner_ecc": {k: (zones.get(k) or {}).get("ecc") for k in orp.CORNERS},
                "center_ecc": m.get("center_ecc"),
                "corner_ratio": m.get("corner_ratio"),
                "soft_corner": m.get("soft_corner"), "sharp_corner": m.get("sharp_corner"),
                "verdict": c.get("verdict"), "why": c.get("why")}
    except Exception as e:  # noqa: BLE001
        logger.debug("frame check corners skipped: %s", e)
        return None


def camera_pa(config, rig: str, header: dict, pa: float | None = None) -> tuple:
    """(camera PA deg, source): --pa, the FITS header (WCS / rotator), else
    the rig's newest plate solve (solve_store, last 7 nights)."""
    from photonscript.scheduler.tracking_test import _camera_pa
    v, src = _camera_pa(header or {}, pa)
    if v is not None:
        return v, src
    try:
        from photonscript.scheduler import solve_store
        root = Path(getattr(config, "data_dir", ".")) / "solves"
        nights = sorted((p.name for p in root.iterdir() if p.is_dir()),
                        reverse=True)[:7] if root.is_dir() else []
        for n in nights:
            for rec in reversed(solve_store.load(config, n, rig)):
                if _num(rec.get("pa")) is not None:
                    return float(rec["pa"]) % 360, f"solve {n}"
    except Exception as e:  # noqa: BLE001
        logger.debug("frame check: no solve PA: %s", e)
    return None, None


def measure(config, path: Path, rig: str = "rc16", pa: float | None = None,
            exp_s: float | None = None) -> dict:
    """One frame measured as a sub is: the record fields plus the
    elongation axis and the trail."""
    from astropy.io import fits

    from photonscript.scheduler import runs
    from photonscript.scheduler.optics_report import axial_stats
    from photonscript.scheduler.tracking_test import (DIRECTION_R_MIN, ELONG_FLOOR,
                                                      _axis_label, _image_axis_label)
    from photonscript.shared import star_measure
    from photonscript.shared.rigs import rig_config
    path = Path(path)
    st_ = path.stat()
    key = (str(path), st_.st_mtime, st_.st_size, pa, rig)
    if key in _CACHE:
        return dict(_CACHE[key])
    rcfg = rig_config(config, rig)
    with runs._HEAVY:                 # one full frame at a time (the graders)
        hdr = fits.getheader(path)
        osc = star_measure.is_osc(rig, hdr)
        m = runs._measure_native(path, rcfg, rig, osc, header=hdr)
        if m is None:
            _, binned = runs._load_binned(path)
            m = star_measure.measure_frame(binned, rcfg, rig, osc=osc,
                                           binned_input=True,
                                           grader="frame-check-binned",
                                           header=hdr)
            del binned
    st = m.pop("_stars", None) or {}
    m.pop("star_table", None)
    scale = float(getattr(rcfg, "pixel_scale_arcsec", 1.0) or 1.0)
    exp = _num(exp_s) or _num(hdr.get("EXPTIME")) or _num(hdr.get("EXPOSURE"))
    h, w = int(hdr.get("NAXIS2") or 0), int(hdr.get("NAXIS1") or 0)
    ax = axial_stats(list(st.get("theta", [])), list(st.get("ecc", [])),
                     list(st.get("x", [])), list(st.get("y", [])), w, h,
                     floor=ELONG_FLOOR) if len(st.get("x", [])) else {}
    cam_pa, pa_src = camera_pa(config, rig, dict(hdr), pa)
    axis = ax.get("axis_deg") if ax and (ax.get("R") or 0) >= DIRECTION_R_MIN else None
    corners = corner_analysis(st, w, h, scale)
    tl = trail_px(st.get("a"), st.get("b"), st.get("flux"))
    tl_as = tl * scale if tl is not None else None
    out = {"file": path.name, "path": str(path), "rig": rig,
           "exp_s": exp, "filter": hdr.get("FILTER"),
           "stars": m.get("stars"), "hfr": m.get("hfr"),
           "fwhm_arcsec": m.get("fwhm_arcsec"), "ecc": m.get("ecc"),
           "background": m.get("background"), "measure_at": m.get("measure_at"),
           "axis_deg": axis, "axis_R": ax.get("R") if ax else None,
           "n_elongated": ax.get("n_elongated") if ax else 0,
           "pa_deg": None if cam_pa is None else round(cam_pa, 1), "pa_src": pa_src,
           "axis": (_axis_label(axis, cam_pa) if cam_pa is not None else None)
           or _image_axis_label(axis),
           "sky_axis": cam_pa is not None and _axis_label(axis, cam_pa) in ("RA", "Dec"),
           "trail_px": None if tl is None else round(tl, 2),
           "trail_arcsec": None if tl_as is None else round(tl_as, 2),
           "trail_rate": round(tl_as / (exp / 60.0), 2)
           if tl_as is not None and exp else None,
           "corners": corners}
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = dict(out)
    return out


# ------------------------------------------------------------- verdicts

def shape_of(config, row: dict) -> str:
    """round | marginal | trailed (the rig's ecc gate, ECC_MARGIN under it
    for round; a trail under TRAIL_ROUND_ARCSEC is seeing)."""
    from photonscript.shared import qa_rules
    ecc = _num(row.get("ecc"))
    if ecc is None:
        return "unmeasured"
    gate = qa_rules.thresholds(config, row.get("rig") or "rc16")["ecc_max"]
    tr = _num(row.get("trail_arcsec"))
    if ecc <= gate - ECC_MARGIN or (tr is not None and tr < TRAIL_ROUND_ARCSEC):
        return "round"
    return "marginal" if ecc <= gate else "trailed"


def _along(row: dict) -> str:
    a = row.get("axis")
    if not a:
        return "in no common direction"
    if a in ("RA", "Dec"):
        return f"along {a}"
    return f"along {a}" + ("" if row.get("pa_deg") is not None else " (sky PA unknown)")


def verdict(config, rows: list[dict]) -> str:
    """One line over the checked frames (labels A, B, ... oldest first)."""
    got = [r for r in rows if r.get("measured")]
    if not got:
        return "nothing measured"
    shapes = {r["label"]: shape_of(config, r) for r in got}
    if all(s == "round" for s in shapes.values()):
        return "both round" if len(got) == 2 else (
            "round" if len(got) == 1 else "all round")
    parts = []
    for r in got:
        s = shapes[r["label"]]
        if s == "round":
            parts.append(f"{r['label']} round")
            continue
        rate = r.get("trail_rate")
        parts.append(f"{r['label']} {'trails' if s == 'trailed' else 'is slightly elongated'} "
                     f"{_along(r)}" + (f" at {rate:.2f}\"/min" if rate is not None else "")
                     + f" (ecc {r.get('ecc')})")
    line = "; ".join(parts)
    rates = [(r["label"], r.get("trail_rate")) for r in got
             if shapes[r["label"]] != "round" and r.get("trail_rate") is not None]
    if len(got) == 2 and len(rates) == 2:
        (la, ra), (lb, rb) = rates
        if abs(ra - rb) >= 0.1 * max(ra, rb, 0.01):
            best = la if ra < rb else lb
            line += f"; {best} trails less"
        else:
            line += "; about the same"
    return line


def diagnose_frames(config, rows: list[dict], control_rows: list[dict] | None = None,
                    control_rig: str = "") -> dict | None:
    """Optics or tracking over the measured frames (optics_vs_tracking):
    exposure scaling of the measured trail, the common axis, the other rig
    as a control. None when every frame is round."""
    from photonscript.scheduler import optics_vs_tracking as ovt
    got = [r for r in rows if r.get("measured")]
    bad = [r for r in got if shape_of(config, r) != "round"]
    if not bad:
        return None
    scaling = ovt.exposure_scaling(got)
    axes = [r.get("axis_deg") for r in bad if r.get("axis_deg") is not None]
    nd = sum(1 for r in bad if r.get("axis_deg") is None)
    axis = ovt.axis_consistency(axes, [r.get("axis_R") for r in bad], no_direction=nd)
    rate = _median([_num(r.get("trail_rate")) for r in bad])
    ctrl = {"verdict": "none", "why": "no control rig"}
    if control_rows is not None:
        parts = [{"exp_s": r.get("exp_s"), "hfr": r.get("hfr"), "ecc": r.get("ecc"),
                  "trailing": shape_of(config, r) == "trailed"}
                 for r in control_rows if r.get("measured")]
        ctrl = ovt.control(rate, parts, _rig_scale(config, control_rig))
        ctrl["rig"] = control_rig
    c = ovt.classify(scaling, axis, ctrl)
    tilt = [r["corners"] for r in bad
            if (r.get("corners") or {}).get("verdict") in ("tilt", "curvature",
                                                            "collimation")]
    out = {"verdict": c["verdict"], "confidence": c["confidence"],
           "reasons": c["reasons"], "against": c["against"], "scaling": scaling,
           "axis": axis, "control": ctrl, "rate_arcsec_min": rate,
           "corners": [{"label": r["label"], **(r.get("corners") or {})} for r in bad
                       if r.get("corners")]}
    if c["verdict"] == "optics":
        hint = ""
        if tilt:
            t = tilt[-1]
            hint = (f" Corners: {t['verdict']} ({t.get('why')}, center "
                    f"{t.get('center_fwhm')}\", corners {t.get('corner_fwhm')}).")
        out["text"] = ("optics (focus / tilt / collimation), not tracking: "
                       + "; ".join(c["reasons"][:2]) + ". " + ovt.OPTICS_FIX + hint)
        out["action"] = "check_optics"
    else:
        out["text"] = ("tracking: " + ("; ".join(c["against"][:2]) or
                                       "nothing says otherwise"))
        out["action"] = "check_tracking"
    return out


def _rig_scale(config, rig: str) -> float:
    try:
        from photonscript.shared.rigs import rig_config
        return float(getattr(rig_config(config, rig), "pixel_scale_arcsec", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def control_entries(entries: list[dict], frames: list[dict]) -> list[dict]:
    """The other rig's history entries whose exposure overlaps a checked
    frame by half the shorter one (NINA's Date is the save time: the
    exposure ends there)."""
    def span(d, x):
        t = _local(d)
        x = _num(x) or 0.0
        return None if t is None else (t - timedelta(seconds=x), t)
    wins = [w for w in (span(f.get("date"), f.get("exp_s")) for f in frames) if w]
    out = []
    for e in entries:
        v = span(e.get("Date"), e.get("ExposureTime"))
        if v is None:
            continue
        for w in wins:
            ov = (min(w[1], v[1]) - max(w[0], v[0])).total_seconds()
            short = min((w[1] - w[0]).total_seconds(), (v[1] - v[0]).total_seconds())
            if ov > 0 and ov >= 0.5 * short:
                out.append(e)
                break
    return out


def check(config, rig: str = "rc16", last: int = 2, index: list[int] | None = None,
          image_type: str = "", pa: float | None = None, compare: bool = False,
          entries: list[dict] | None = None, base_url: str = "",
          profile_path: str | None = None, measure_fn=None, deep: bool = True,
          control_rig: str = "", control_history: list[dict] | None = None) -> dict:
    """Pick NINA's frames, find and measure them. `entries` (tests) skips
    the NINA read. Never raises for one bad frame. control_rig (with
    compare): also measure the other rig's frames that overlap these in
    time, a control for the optics / tracking call (`control_history`,
    tests, skips that rig's NINA read)."""
    out = {"rig": rig, "ok": True, "error": None, "frames": [], "verdict": None,
           "command": scope_command(rig, last, index, compare, image_type, pa,
                                    control_rig)}
    if entries is None:
        try:
            entries = history(config, rig, base_url)
        except Exception as e:  # noqa: BLE001
            out.update(ok=False, error=f"NINA image history not readable "
                       f"({nina_url(config, rig) if not base_url else base_url}): {e}")
            return out
    if profile_path is None:
        profile_path = nina_save_path(config, rig, base_url)
    roots = save_roots(config, rig, profile_path)
    measure_fn = measure_fn or measure
    out["frames"] = _measure_entries(config, rig, pick(entries, last, index, image_type),
                                     roots, measure_fn, pa, deep)
    if compare:
        out["verdict"] = verdict(config, out["frames"])
        ctrl_rows = None
        if control_rig and control_rig != rig:
            try:
                ents = (control_history if control_history is not None
                        else history(config, control_rig))
                c_roots = save_roots(config, control_rig,
                                     "" if control_history is not None
                                     else nina_save_path(config, control_rig))
                ctrl_rows = _measure_entries(
                    config, control_rig, control_entries(ents, out["frames"]),
                    c_roots, measure_fn, None, deep)
                out["control_frames"] = ctrl_rows
            except Exception as e:  # noqa: BLE001
                out["control_error"] = f"{control_rig}: {e}"
        out["diagnosis"] = diagnose_frames(config, out["frames"], ctrl_rows,
                                           control_rig)
    return out


def _measure_entries(config, rig, entries, roots, measure_fn, pa, deep) -> list[dict]:
    rows = []
    for i, e in enumerate(entries):
        row = {"label": LABELS[i % len(LABELS)], "index": e.get("index"),
               "date": e.get("Date"), "image_type": e.get("ImageType"),
               "exp_s": _num(e.get("ExposureTime")), "filter": e.get("Filter"),
               "name": e.get("Filename"), "measured": False}
        p = locate(roots, e, deep=deep)
        if p is None:
            row["error"] = "file not found under " + (
                ", ".join(str(r) for r in roots) or "no NINA save path")
        else:
            try:
                row.update(measure_fn(config, p, rig, pa=pa, exp_s=row["exp_s"]))
                row["measured"] = True
                row["shape"] = shape_of(config, row)
            except Exception as ex:  # noqa: BLE001
                logger.warning("frame check %s: %s", p, ex)
                row["error"] = f"measure failed: {ex}"
        rows.append(row)
    return rows


def nina_save_path(config, rig: str, base_url: str = "") -> str:
    """NINA's image save path from its active profile (ImageFileSettings
    FilePath), "" when unreadable."""
    try:
        prof = _get_json(f"{(base_url or nina_url(config, rig)).rstrip('/')}"
                         "/profile/show?active=true", timeout=5.0)
        return str(((prof or {}).get("ImageFileSettings") or {}).get("FilePath") or "")
    except Exception:  # noqa: BLE001
        return ""


def scope_command(rig: str = "rc16", last: int = 2, index=None, compare=False,
                  image_type: str = "", pa=None, control: str = "") -> str:
    """The exact command to run on the scope PC."""
    s = f"{SCOPE_EXE} frame-check --rig {rig}"
    if index:
        s += "".join(f" --index {int(i)}" for i in index)
    else:
        s += f" --last {int(last)}"
    if image_type:
        s += f" --type {image_type}"
    if pa is not None:
        s += f" --pa {pa:g}"
    if control:
        s += f" --control {control}"
    return s + (" --compare" if compare else "")


def _f(v, nd=2, w=6):
    return f"{v:.{nd}f}".rjust(w) if isinstance(v, (int, float)) else "-".rjust(w)


def format_report(rep: dict) -> str:
    lines = []
    if not rep.get("ok"):
        return f"{rep.get('error')}\nOn the scope PC run:\n  {rep.get('command')}"
    lines.append("   idx  time   type      exp  filt stars   HFR  FWHM\"   ecc  "
                 "axis        trail\"  \"/min      bg  shape")
    for r in rep.get("frames") or []:
        t = _local(r.get("date"))
        head = (f"{r['label']}  {str(r.get('index')).rjust(3)}  "
                f"{t.strftime('%H:%M') if t else '--:--'}  "
                f"{str(r.get('image_type') or '')[:8].ljust(8)} "
                f"{_f(r.get('exp_s'), 0, 4)}  {str(r.get('filter') or '')[:4].ljust(4)}")
        if not r.get("measured"):
            lines.append(f"{head}  {r.get('error')}")
            continue
        lines.append(f"{head} {str(r.get('stars')).rjust(5)} {_f(r.get('hfr'))} "
                     f"{_f(r.get('fwhm_arcsec'))} {_f(r.get('ecc'), 3)}  "
                     f"{str(r.get('axis') or '-')[:10].ljust(10)} "
                     f"{_f(r.get('trail_arcsec'))} {_f(r.get('trail_rate'))} "
                     f"{_f(r.get('background'), 0, 7)}  {r.get('shape') or ''}")
    pas = {(r.get("pa_deg"), r.get("pa_src")) for r in rep.get("frames") or []
           if r.get("measured")}
    for pa, src in sorted(pas, key=str):
        lines.append(f"camera PA: {pa} deg ({src})" if pa is not None else
                     "camera PA unknown (no WCS, rotator or recent solve; --pa "
                     "names the axis RA / Dec)")
    for r in rep.get("frames") or []:
        c = r.get("corners") or {}
        if c.get("ok"):
            cf = c.get("corner_fwhm") or {}
            ce = c.get("corner_ecc") or {}
            lines.append(f"{r['label']} corners: center {c.get('center_fwhm')}\" "
                         f"(ecc {c.get('center_ecc')}); "
                         + ", ".join(f"{k} {cf.get(k)}\" (ecc {ce.get(k)})"
                                     for k in ("TL", "TR", "BL", "BR"))
                         + f"; {c.get('verdict')}: {c.get('why')}")
    for r in rep.get("control_frames") or []:
        if r.get("measured"):
            lines.append(f"control {r.get('rig')} {r['label']}: {_f(r.get('exp_s'), 0, 4)} s "
                         f"ecc {r.get('ecc')} HFR {r.get('hfr')} {r.get('shape')}")
    if rep.get("verdict"):
        lines.append(f"Verdict: {rep['verdict']}")
    dg = rep.get("diagnosis")
    if dg:
        lines.append(f"Diagnosis ({dg.get('confidence')}): {dg.get('text')}")
    if any(not r.get("measured") for r in rep.get("frames") or []):
        lines.append(f"On the scope PC run:\n  {rep.get('command')}")
    return "\n".join(lines)
