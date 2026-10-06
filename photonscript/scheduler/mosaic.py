"""Mosaic planner (v1 layout, PS-111 v2 goals).

v1 (plan_panels): flat-sky panel math with a cos(dec) RA correction, kept
for GET /api/mosaic/plan.

v2 (PS-111): a mosaic is ONE goal made of panel goals. Each panel is its own
ImagingProject (own coordinates, own plans and PS-118 seconds crediting)
carrying `mosaic` = {id, name, panel, row, col, of, companion, layout}; the
group of panels sharing an id is the mosaic goal (dashboard card, Targets
page group, /api/mosaics). Choices (M31 first, approved 2026-10-05):

* Geometry: gnomonic (TAN) tangent plane around the mosaic center, so panel
  centers are exact at any size. Panels step by fov x (1 - overlap) along
  the camera axes. `pa_deg` is the position angle (deg east of north) of
  the panel's +y (short) axis, the same angle ASTAP reports (solve_store
  `pa`); the long side runs along PA - 90. Row 1 is the row furthest along
  +y, column 1 the column furthest toward PA + 90 (east at PA 0: the left
  of a north-up, east-left view). Panels are numbered row by row: P1 is
  row 1 col 1, and that is the capture order.
* Rotation: neither rig has a rotator, so every panel is shot at the
  camera's own fixed angle and the grid MUST use that angle or it leaves
  gaps. rotation="camera" (default) takes the RC16's measured PA (circular
  median of its plate solves, mod 180), else 0 (north up) when nothing is
  solved yet. A requested angle that differs from the measured camera PA is
  kept but flagged: the camera has to be turned by hand to match.
  camera_pa_for_major_axis() gives the camera angle that would lay the
  long side along a galaxy's major axis (M31: PA 35 -> camera 125).
* Order: panels are finished IN ORDER (P1 first). The planner gives tonight
  only the first unfinished panels whose owed hours fill the mosaic's
  visible time; a panel shoots its owed subs once and hands over, the last
  one keeps the usual repeat-while-up loop. Why not balance: complete
  panels are usable on their own (a half-done mosaic is still N finished
  frames), each panel's data comes from fewer, more similar nights, and
  there is at most one panel move a night, so the riding Piggy-600 loses
  at most one sub per move to the PS-13 slew gate.
"""
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

# AARO rig: RC16 3248 mm + IMX571 (23.5 x 15.7 mm) -> 0.414 x 0.277 deg
DEFAULT_FOV_W = 0.414
DEFAULT_FOV_H = 0.277

DEFAULT_OVERLAP_PCT = 15.0
DEFAULT_HOURS_PER_PANEL = 4.0
MAX_PANELS = 16
PA_MISMATCH_DEG = 2.0      # requested grid vs measured camera angle
CAMERA_PA_NIGHTS = 30      # solve nights the camera PA looks back over


def plan_panels(name: str, ra_hours: float, dec_degrees: float,
                rows: int = 2, cols: int = 2, overlap_pct: float = 15.0,
                rotation_deg: float = 0.0,
                fov_w: float = DEFAULT_FOV_W,
                fov_h: float = DEFAULT_FOV_H) -> dict:
    """Panel centers for a rows x cols mosaic centered on (ra, dec).

    +x = east, +y = north in degrees on the sky; rotation rotates the
    whole grid. Overlap is the fraction each panel shares with its
    neighbor (15% is a comfortable registration margin).
    """
    rows, cols = max(1, int(rows)), max(1, int(cols))
    ov = max(0.0, min(60.0, float(overlap_pct))) / 100.0
    step_x = fov_w * (1.0 - ov)
    step_y = fov_h * (1.0 - ov)
    rot = math.radians(rotation_deg)
    cosr, sinr = math.cos(rot), math.sin(rot)
    panels = []
    for r in range(rows):
        for c in range(cols):
            dx = (c - (cols - 1) / 2.0) * step_x
            dy = ((rows - 1) / 2.0 - r) * step_y
            east = dx * cosr - dy * sinr
            north = dx * sinr + dy * cosr
            dec = dec_degrees + north
            cosd = max(0.05, math.cos(math.radians(dec)))
            ra = (ra_hours + (east / cosd) / 15.0) % 24.0
            panels.append({
                "name": f"{name} P{r + 1}{c + 1}",
                "row": r + 1, "col": c + 1,
                "ra_hours": round(ra, 6),
                "dec_degrees": round(dec, 6),
                "east_deg": round(east, 5),
                "north_deg": round(north, 5),
                "fov_w": fov_w, "fov_h": fov_h,
                "rotation_deg": rotation_deg,
            })
    return {
        "panels": panels,
        "span_w_deg": round(step_x * (cols - 1) + fov_w, 4),
        "span_h_deg": round(step_y * (rows - 1) + fov_h, 4),
    }


# --- v2 geometry (PS-111) -----------------------------------------------------

def deproject(ra0_hours: float, dec0_deg: float, east_deg: float,
              north_deg: float) -> tuple[float, float]:
    """(ra hours, dec deg) of a tangent-plane offset (degrees east / north
    of the center), inverse gnomonic."""
    a0, d0 = math.radians(ra0_hours * 15.0), math.radians(dec0_deg)
    xi, eta = math.radians(east_deg), math.radians(north_deg)
    den = math.cos(d0) - eta * math.sin(d0)
    ra = a0 + math.atan2(xi, den)
    dec = math.atan2(math.sin(d0) + eta * math.cos(d0),
                     math.hypot(xi, den))
    return (math.degrees(ra) / 15.0) % 24.0, math.degrees(dec)


def project(ra0_hours: float, dec0_deg: float, ra_hours: float,
            dec_deg: float) -> tuple[float, float]:
    """Gnomonic offset (east deg, north deg) of (ra, dec) from the center."""
    a0, d0 = math.radians(ra0_hours * 15.0), math.radians(dec0_deg)
    a, d = math.radians(ra_hours * 15.0), math.radians(dec_deg)
    cosc = (math.sin(d0) * math.sin(d)
            + math.cos(d0) * math.cos(d) * math.cos(a - a0))
    xi = math.cos(d) * math.sin(a - a0) / cosc
    eta = (math.cos(d0) * math.sin(d)
           - math.sin(d0) * math.cos(d) * math.cos(a - a0)) / cosc
    return math.degrees(xi), math.degrees(eta)


def separation_deg(ra1_h, dec1, ra2_h, dec2) -> float:
    r1, r2 = math.radians(ra1_h * 15.0), math.radians(ra2_h * 15.0)
    d1, d2 = math.radians(dec1), math.radians(dec2)
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _axes(pa_deg: float) -> tuple[tuple, tuple]:
    """Unit vectors (east, north) of the panel +x (long side, toward
    PA - 90) and +y (PA) axes."""
    p = math.radians(pa_deg)
    y = (math.sin(p), math.cos(p))
    x = (-math.cos(p), math.sin(p))
    return x, y


def _offset(u: float, v: float, pa_deg: float) -> tuple[float, float]:
    """Panel-frame (u along +x, v along +y) -> (east, north) degrees."""
    x, y = _axes(pa_deg)
    return u * x[0] + v * y[0], u * x[1] + v * y[1]


def frame_corners(east: float, north: float, w_deg: float, h_deg: float,
                  pa_deg: float) -> list[list[float]]:
    """Tangent-plane corners (east, north deg) of a w x h frame centered on
    (east, north) at pa_deg: top-left, top-right, bottom-right,
    bottom-left as the camera sees it."""
    out = []
    for su, sv in ((-1, 1), (1, 1), (1, -1), (-1, -1)):
        de, dn = _offset(su * w_deg / 2.0, sv * h_deg / 2.0, pa_deg)
        out.append([round(east + de, 5), round(north + dn, 5)])
    return out


def layout(name: str, ra_hours: float, dec_degrees: float, rows: int = 2,
           cols: int = 2, overlap_pct: float = DEFAULT_OVERLAP_PCT,
           pa_deg: float = 0.0, fov_w_deg: float = DEFAULT_FOV_W,
           fov_h_deg: float = DEFAULT_FOV_H) -> dict:
    """Panel centers and outlines of a rows x cols mosaic (module doc)."""
    rows, cols = max(1, int(rows)), max(1, int(cols))
    ov = max(0.0, min(60.0, float(overlap_pct))) / 100.0
    pa = float(pa_deg) % 360.0
    step_w = fov_w_deg * (1.0 - ov)
    step_h = fov_h_deg * (1.0 - ov)
    panels = []
    for r in range(rows):
        for c in range(cols):
            # column 1 toward PA + 90 (-x), row 1 toward +y
            u = (c - (cols - 1) / 2.0) * step_w
            v = ((rows - 1) / 2.0 - r) * step_h
            east, north = _offset(u, v, pa)
            ra, dec = deproject(ra_hours, dec_degrees, east, north)
            idx = r * cols + c + 1
            panels.append({
                "panel": idx, "row": r + 1, "col": c + 1,
                "name": f"{name} P{idx}",
                "ra_hours": round(ra, 6), "dec_degrees": round(dec, 6),
                "east_deg": round(east, 5), "north_deg": round(north, 5),
                "corners": frame_corners(east, north, fov_w_deg, fov_h_deg,
                                         pa),
            })
    span_w = step_w * (cols - 1) + fov_w_deg
    span_h = step_h * (rows - 1) + fov_h_deg
    return {
        "name": name, "ra_hours": float(ra_hours),
        "dec_degrees": float(dec_degrees), "rows": rows, "cols": cols,
        "overlap_pct": round(ov * 100.0, 1), "pa_deg": round(pa, 2),
        "fov_w_deg": round(fov_w_deg, 5), "fov_h_deg": round(fov_h_deg, 5),
        "step_w_deg": round(step_w, 5), "step_h_deg": round(step_h, 5),
        "span_w_deg": round(span_w, 4), "span_h_deg": round(span_h, 4),
        "span_w_arcmin": round(span_w * 60.0, 1),
        "span_h_arcmin": round(span_h * 60.0, 1),
        "outline": frame_corners(0.0, 0.0, span_w, span_h, pa),
        "panels": panels,
    }


def overlap_fraction(lay: dict, a: int, b: int) -> float:
    """Shared fraction of the frame along the axis joining two neighboring
    panels (1-based numbers), from their real sky separation."""
    pa, pb = lay["panels"][a - 1], lay["panels"][b - 1]
    sep = separation_deg(pa["ra_hours"], pa["dec_degrees"],
                         pb["ra_hours"], pb["dec_degrees"])
    side = lay["fov_w_deg"] if pa["row"] == pb["row"] else lay["fov_h_deg"]
    return 1.0 - sep / side


def camera_pa_for_major_axis(major_axis_pa: float) -> float:
    """Camera PA (of the +y axis) that lays the long side along an object's
    major axis: long side = PA - 90, so camera = major + 90 (mod 180)."""
    return round((float(major_axis_pa) + 90.0) % 180.0, 1)


def _pa_diff180(a: float, b: float) -> float:
    d = abs((float(a) - float(b)) % 180.0)
    return min(d, 180.0 - d)


def camera_pa(config, rig: str = "rc16",
              nights: int = CAMERA_PA_NIGHTS) -> dict:
    """The rig's fixed camera angle from its stored plate solves (PS-96
    solve_store, <data_dir>/solves/<night>/<rig>.jsonl): circular median
    of `pa` mod 180 over the newest `nights` nights (a meridian flip turns
    the frame by 180, the same rectangle). {pa_deg, n, source}; pa_deg None
    when nothing is solved."""
    import json
    root = Path(getattr(config, "data_dir", ".")) / "solves"
    vals: list[float] = []
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                      key=lambda d: d.name, reverse=True)[:max(1, nights)]
    except OSError:
        dirs = []
    for d in dirs:
        f = d / f"{rig}.jsonl"
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("solved") and rec.get("pa") is not None:
                vals.append(float(rec["pa"]) % 180.0)
    if not vals:
        return {"pa_deg": None, "n": 0,
                "source": f"no {rig} plate solves on record"}
    # circular mean on doubled angles, then the median of the residuals
    sx = sum(math.cos(math.radians(2 * v)) for v in vals)
    sy = sum(math.sin(math.radians(2 * v)) for v in vals)
    mean = (math.degrees(math.atan2(sy, sx)) / 2.0) % 180.0
    res = sorted(((v - mean + 90.0) % 180.0) - 90.0 for v in vals)
    mid = res[len(res) // 2] if len(res) % 2 else \
        (res[len(res) // 2 - 1] + res[len(res) // 2]) / 2.0
    pa = (mean + mid) % 180.0
    return {"pa_deg": round(pa, 2), "n": len(vals),
            "source": f"median of {len(vals)} {rig} plate solve(s)"}


def resolve_rotation(config, rotation="camera",
                     major_axis_pa: float | None = None,
                     rig: str = "rc16") -> dict:
    """The grid angle to use and why. rotation: "camera" (default: the
    measured camera PA, else 0) or a number (deg). Returns {pa_deg, source,
    camera_pa, aligned_to_camera, warnings[], major_axis: {...}|None}."""
    cam = camera_pa(config, rig)
    warnings: list[str] = []
    if rotation in (None, "", "camera"):
        if cam["pa_deg"] is None:
            pa, src = 0.0, ("north up (0 deg): " + cam["source"]
                            + "; check the first panel's solve and re-plan "
                              "if the camera sits at another angle")
            warnings.append("camera angle unknown: panels laid out north up; "
                            "the RC16 has no rotator, so if its camera is "
                            "turned the panels will not tile as drawn")
        else:
            pa, src = float(cam["pa_deg"]), "camera: " + cam["source"]
    else:
        pa, src = float(rotation) % 360.0, "requested"
        if cam["pa_deg"] is not None and \
                _pa_diff180(pa, cam["pa_deg"]) > PA_MISMATCH_DEG:
            warnings.append(
                f"requested {pa:.1f} deg but the RC16 camera sits at "
                f"{cam['pa_deg']:.1f} deg ({cam['source']}); with no rotator "
                "every panel is shot at the camera angle, so turn the camera "
                "by hand to match or use rotation \"camera\"")
        elif cam["pa_deg"] is None:
            warnings.append("camera angle unknown: make sure the camera sits "
                            f"at {pa:.1f} deg (mod 180) before the first panel")
    aligned = (cam["pa_deg"] is not None
               and _pa_diff180(pa, cam["pa_deg"]) <= PA_MISMATCH_DEG)
    major = None
    if major_axis_pa is not None:
        want = camera_pa_for_major_axis(major_axis_pa)
        major = {"major_axis_pa": float(major_axis_pa),
                 "camera_pa_needed": want,
                 "grid_along_major_axis": _pa_diff180(pa, want)
                 <= PA_MISMATCH_DEG}
        if not major["grid_along_major_axis"]:
            major["note"] = (f"grid follows the camera ({pa:.1f} deg), not the "
                             f"major axis (PA {float(major_axis_pa):g}); a "
                             f"galaxy-aligned grid needs the camera turned to "
                             f"{want:g} deg (no rotator), then re-plan")
    return {"pa_deg": round(pa, 2), "source": src, "camera_pa": cam,
            "aligned_to_camera": aligned, "warnings": warnings,
            "major_axis": major}


# --- v2 goals: create, group, order -------------------------------------------

def mosaic_of(project) -> dict | None:
    m = getattr(project, "mosaic", None)
    return m if isinstance(m, dict) and m.get("id") else None


def panel_index(project) -> int:
    m = mosaic_of(project)
    return int(m.get("panel") or 0) if m else 0


def panels_by_mosaic(projects) -> dict[str, list]:
    """{mosaic id: [panel projects in capture order]}."""
    out: dict[str, list] = {}
    for p in projects:
        m = mosaic_of(p)
        if m:
            out.setdefault(m["id"], []).append(p)
    for v in out.values():
        v.sort(key=panel_index)
    return out


def rc16_owed_hours(project) -> float:
    """RC16 hours still owed (seconds-based, PS-118)."""
    from photonscript.scheduler.campaign import plan_seconds
    owed = 0.0
    for e in project.exposure_plans:
        if (getattr(e, "rig", "rc16") or "rc16") != "rc16":
            continue
        g, d = plan_seconds(e)
        owed += max(0.0, g - d)
    return owed / 3600.0


PANEL_OVERHEAD = 1.15  # the night planner's slew / dither / AF allowance


def tonight_panels(panels: list, usable_hours: float,
                   overhead: float = PANEL_OVERHEAD) -> tuple[list, list]:
    """In-order rule: (admitted, held) for one mosaic's unfinished panels
    (capture order). The next panel is admitted only while the panels
    already admitted (owed hours x overhead) finish before the night's
    usable hours for the mosaic run out, so every admitted panel but the
    last is shot in full and the last one (repeat-while-up) takes the
    rest of the night."""
    admitted, held = [], []
    cum = 0.0
    for p in sorted(panels, key=panel_index):
        if not admitted or cum * overhead < usable_hours:
            admitted.append(p)
            cum += rc16_owed_hours(p)
        else:
            held.append(p)
    return admitted, held


def _norm_mix(mix: dict | None) -> dict | None:
    if not mix:
        return None
    clean = {str(k): float(v) for k, v in mix.items() if v and float(v) > 0}
    return clean or None


def build_panels(config, spec: dict, existing_names=()) -> dict:
    """Validate a create request and return {id, layout, rotation, panels:
    [ImagingProject...], errors, warnings} without saving anything.

    spec: name, ra_hours, dec_degrees, rows, cols, overlap_pct, rotation
    ("camera" | deg), major_axis_pa, hours_per_panel, filter_mix ({filter:
    %}), exposure_s (one sub length for every filter), object_type,
    priority, companion_id (project id whose Piggy-600 plan the passenger
    subs credit, resolved by the caller with find_companion), rig."""
    from photonscript.scheduler.project_store import (allocate_exposures,
                                                      target_kind)
    from photonscript.scheduler.refimage import rig_fov
    from photonscript.shared.models import CelestialTarget, ImagingProject

    errors: list[str] = []
    name = str(spec.get("name") or "").strip()
    if not name:
        errors.append("name is required")
    try:
        ra = float(spec["ra_hours"])
        dec = float(spec["dec_degrees"])
        if not (0.0 <= ra < 24.0) or not (-90.0 <= dec <= 90.0):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        errors.append("ra_hours (0..24) and dec_degrees (-90..90) are required")
        ra, dec = 0.0, 0.0
    rows = int(spec.get("rows") or 2)
    cols = int(spec.get("cols") or 2)
    if rows < 1 or cols < 1 or rows * cols > MAX_PANELS:
        errors.append(f"rows x cols must be 1..{MAX_PANELS} panels")
        rows, cols = max(1, min(rows, 4)), max(1, min(cols, 4))
    overlap = float(spec.get("overlap_pct", DEFAULT_OVERLAP_PCT))
    if not (0.0 <= overlap <= 50.0):
        errors.append("overlap_pct must be 0..50")
    hours = float(spec.get("hours_per_panel") or DEFAULT_HOURS_PER_PANEL)
    if hours <= 0:
        errors.append("hours_per_panel must be > 0")
        hours = DEFAULT_HOURS_PER_PANEL
    rig = str(spec.get("rig") or "rc16")
    if rig != "rc16":
        errors.append("panels are RC16 goals (the Piggy-600 rides along; "
                      "give its goal as companion)")
    fov = rig_fov(config, "rc16")
    fw, fh = fov["w_arcmin"] / 60.0, fov["h_arcmin"] / 60.0
    rot = resolve_rotation(config, spec.get("rotation", "camera"),
                           spec.get("major_axis_pa"))
    lay = layout(name or "Mosaic", ra, dec, rows, cols, overlap,
                 rot["pa_deg"], fw, fh)
    taken = {str(n).strip().lower() for n in existing_names}
    clash = [p["name"] for p in lay["panels"] if p["name"].lower() in taken]
    if clash:
        errors.append("a goal already has these names: " + ", ".join(clash))
    mix = _norm_mix(spec.get("filter_mix"))
    exp_s = spec.get("exposure_s")
    obj_type = str(spec.get("object_type") or "galaxy")
    mid = str(spec.get("id") or uuid.uuid4().hex[:12])
    definition = {k: lay[k] for k in (
        "ra_hours", "dec_degrees", "rows", "cols", "overlap_pct", "pa_deg",
        "fov_w_deg", "fov_h_deg", "span_w_arcmin", "span_h_arcmin")}
    definition.update({"pa_source": rot["source"], "rig": rig,
                       "hours_per_panel": hours, "order": "in_order",
                       "created_utc": datetime.now(timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ")})
    stored_mix = ({k: round(v / sum(mix.values()) * 100)
                   for k, v in mix.items()} if mix else None)
    projects = []
    for pnl in lay["panels"]:
        target = CelestialTarget(
            name=pnl["name"], ra_hours=pnl["ra_hours"],
            dec_degrees=pnl["dec_degrees"], object_type=obj_type,
            angular_size_arcmin=round(max(fov["w_arcmin"], fov["h_arcmin"]),
                                      1),
            notes=f"PS-111 mosaic {name} panel {pnl['panel']} of "
                  f"{len(lay['panels'])}")
        kind = target_kind(target)
        overrides = None
        if exp_s:
            fs = list(mix) if mix else (["Ha", "OIII", "SII"]
                                        if kind == "narrowband"
                                        else ["L", "R", "G", "B"])
            overrides = {f: float(exp_s) for f in fs}
        plans = allocate_exposures(kind, hours, config, custom_mix=mix,
                                   overrides=overrides)
        projects.append(ImagingProject(
            id=str(uuid.uuid4()), target=target,
            priority=max(0, min(100, int(spec.get("priority", 50)))),
            budget_hours=round(hours, 1), filter_mix=stored_mix,
            exposure_overrides=overrides, exposure_plans=plans,
            total_integration_hours=round(hours, 1),
            mosaic={"id": mid, "name": name, "panel": pnl["panel"],
                    "row": pnl["row"], "col": pnl["col"],
                    "of": len(lay["panels"]),
                    "companion": spec.get("companion_id"),
                    "layout": definition}))
    return {"id": mid, "layout": lay, "rotation": rot, "panels": projects,
            "errors": errors, "warnings": list(rot["warnings"])}


def find_companion(projects, ref) -> object | None:
    """The goal a mosaic's Piggy-600 passenger subs credit: by project id,
    name or catalog id (target_key match), only if it has a piggyback plan."""
    from photonscript.shared.target_names import target_key
    if not ref:
        return None
    k = target_key(ref)
    for p in projects:
        if mosaic_of(p):
            continue
        if p.id == ref or k in (target_key(p.target.name),
                                target_key(p.target.catalog_id)):
            if any((e.rig or "rc16") != "rc16" for e in p.exposure_plans):
                return p
    return None


# --- the M31 suggestion (Jeremy 2026-10-05) and a generic one -----------------

M31_SUGGESTION = {
    "name": "M31 Core", "ra_hours": 0.712306, "dec_degrees": 41.269167,
    "rows": 2, "cols": 2, "overlap_pct": 15.0, "rotation": "camera",
    "major_axis_pa": 35.0, "hours_per_panel": 4.0,
    "filter_mix": {"L": 50, "R": 17, "G": 17, "B": 17}, "exposure_s": 300,
    "object_type": "galaxy", "priority": 50, "companion": "M 31",
    "why": "RC16 close-up of the M31 core and inner dust lanes while the "
           "Piggy-600 shoots the whole galaxy: 2 x 2 RC16 panels (about 45' "
           "x 30' with 15% overlap) centered on the nucleus, LRGB 50/17/17/17 "
           "at 300 s, 4 h per panel, finished in order. Approved 2026-10-05.",
}


def suggest(config, name: str) -> dict | None:
    """A starting mosaic for a catalog target: the M31 recipe for M31, else a
    grid that covers the object's catalog size with the RC16 panels (at
    most 4 x 4), 4 h per panel at the type-default mix."""
    from photonscript.shared.astronomy import find_catalog_entry
    from photonscript.shared.target_names import target_key
    from photonscript.scheduler.refimage import rig_fov
    entry = find_catalog_entry(name)
    if entry is None:
        return None
    if target_key(entry.get("catalog_id", "")) == "m31":
        return dict(M31_SUGGESTION)
    fov = rig_fov(config, "rc16")
    size = float(entry.get("size") or 0.0)
    step_w = fov["w_arcmin"] * (1 - DEFAULT_OVERLAP_PCT / 100)
    step_h = fov["h_arcmin"] * (1 - DEFAULT_OVERLAP_PCT / 100)
    cols = max(1, min(4, math.ceil(max(0.0, size - fov["w_arcmin"])
                                   / step_w) + 1))
    rows = max(1, min(4, math.ceil(max(0.0, size - fov["h_arcmin"])
                                   / step_h) + 1))
    return {"name": f"{entry['name']} Mosaic", "ra_hours": entry["ra"],
            "dec_degrees": entry["dec"], "rows": rows, "cols": cols,
            "overlap_pct": DEFAULT_OVERLAP_PCT, "rotation": "camera",
            "hours_per_panel": DEFAULT_HOURS_PER_PANEL, "filter_mix": None,
            "exposure_s": None, "object_type": entry.get("type", ""),
            "priority": 50, "companion": entry.get("catalog_id") or None,
            "why": f"{rows} x {cols} RC16 panels to cover the catalog size "
                   f"({size:g}'), {DEFAULT_HOURS_PER_PANEL:g} h per panel at "
                   "the type-default mix"}


# --- read side: progress + preview ---------------------------------------------

def _panel_progress(p) -> dict:
    from photonscript.scheduler.campaign import plan_seconds
    goal = done = 0.0
    filters = []
    for e in p.exposure_plans:
        g, d = plan_seconds(e)
        goal += g
        done += d
        filters.append({"filter": e.filter_type.value, "rig": e.rig,
                        "exposure_s": e.exposure_seconds, "count": e.count,
                        "acquired": e.acquired,
                        "goal_h": round(g / 3600, 2),
                        "done_h": round(d / 3600, 2)})
    m = mosaic_of(p)
    return {"id": p.id, "name": p.target.name, "panel": m["panel"],
            "row": m["row"], "col": m["col"], "active": p.active,
            "priority": p.priority,
            "ra_hours": p.target.ra_hours, "dec_degrees": p.target.dec_degrees,
            "goal_h": round(goal / 3600, 2), "done_h": round(done / 3600, 2),
            "pct": round(done / goal * 100) if goal else 0,
            "complete": goal > 0 and done >= goal - 1.0,
            "filters": filters}


def summaries(config, projects) -> list[dict]:
    """Every mosaic goal: definition, per-panel progress, the next panel,
    the companion goal and the preview geometry (panel outlines plus the
    RC16 and Piggy-600 footprints, tangent-plane degrees east / north of
    the mosaic center)."""
    from photonscript.scheduler.refimage import rig_fov
    projects = list(projects)
    by_id = {p.id: p for p in projects}
    piggy = rig_fov(config, "piggyback")
    pw, ph = piggy["w_arcmin"] / 60.0, piggy["h_arcmin"] / 60.0
    piggy_pa = camera_pa(config, "piggyback")
    ppa = piggy_pa["pa_deg"] if piggy_pa["pa_deg"] is not None else 0.0
    out = []
    for mid, panels in panels_by_mosaic(projects).items():
        m0 = mosaic_of(panels[0])
        lay0 = dict(m0.get("layout") or {})
        ra0 = float(lay0.get("ra_hours", panels[0].target.ra_hours))
        dec0 = float(lay0.get("dec_degrees", panels[0].target.dec_degrees))
        pa = float(lay0.get("pa_deg", 0.0))
        fw = float(lay0.get("fov_w_deg", DEFAULT_FOV_W))
        fh = float(lay0.get("fov_h_deg", DEFAULT_FOV_H))
        rows = [_panel_progress(p) for p in panels]
        for r, p in zip(rows, panels):
            e, n = project(ra0, dec0, p.target.ra_hours, p.target.dec_degrees)
            r["east_deg"], r["north_deg"] = round(e, 5), round(n, 5)
            r["corners"] = frame_corners(e, n, fw, fh, pa)
        nxt = next((r for r in rows if r["active"] and not r["complete"]),
                   None)
        goal = sum(r["goal_h"] for r in rows)
        done = sum(r["done_h"] for r in rows)
        comp = by_id.get(m0.get("companion") or "")
        comp_info = None
        if comp is not None:
            from photonscript.scheduler.campaign import plan_seconds
            osc = [plan_seconds(e) for e in comp.exposure_plans
                   if (e.rig or "rc16") != "rc16"]
            comp_info = {"id": comp.id, "name": comp.target.name,
                         "goal_h": round(sum(g for g, _ in osc) / 3600, 1),
                         "done_h": round(sum(d for _, d in osc) / 3600, 1)}
        shift = max((math.hypot(r["east_deg"], r["north_deg"]) for r in rows),
                    default=0.0)
        span_w = float(lay0.get("span_w_arcmin") or 0) / 60.0
        span_h = float(lay0.get("span_h_arcmin") or 0) / 60.0
        out.append({
            "id": mid, "name": m0.get("name") or mid,
            "panels": rows, "n_panels": len(rows),
            "rows": lay0.get("rows"), "cols": lay0.get("cols"),
            "overlap_pct": lay0.get("overlap_pct"), "pa_deg": pa,
            "pa_source": lay0.get("pa_source"),
            "ra_hours": ra0, "dec_degrees": dec0,
            "span_w_arcmin": lay0.get("span_w_arcmin"),
            "span_h_arcmin": lay0.get("span_h_arcmin"),
            "hours_per_panel": lay0.get("hours_per_panel"),
            "order": lay0.get("order", "in_order"),
            "goal_h": round(goal, 1), "done_h": round(done, 1),
            "pct": round(done / goal * 100) if goal else 0,
            "next_panel": nxt["name"] if nxt else None,
            "complete": all(r["complete"] for r in rows),
            "active": any(r["active"] for r in rows),
            "priority": max(p.priority for p in panels),
            "companion": comp_info,
            "preview": {
                "outline": frame_corners(0.0, 0.0, span_w, span_h, pa),
                "rc16": {"w_deg": round(fw, 4), "h_deg": round(fh, 4),
                         "pa_deg": pa},
                "piggy": {"w_deg": round(pw, 4), "h_deg": round(ph, 4),
                          "pa_deg": ppa,
                          "pa_source": piggy_pa["source"],
                          "corners": frame_corners(0.0, 0.0, pw, ph, ppa),
                          "max_shift_arcmin": round(shift * 60, 1)},
            },
        })
    out.sort(key=lambda m: (-m["priority"], m["name"].lower()))
    return out
