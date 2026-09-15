"""Mosaic planner: split a wide framing into telescope-FOV panels.

Flat-sky panel math with a cos(dec) RA correction - accurate to well
under a panel overlap for the few-degree mosaics this exists for.
"""
import math

# AARO rig: RC16 3248 mm + IMX571 (23.5 x 15.7 mm) -> 0.414 x 0.277 deg
DEFAULT_FOV_W = 0.414
DEFAULT_FOV_H = 0.277


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
