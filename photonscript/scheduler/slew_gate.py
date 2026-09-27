"""Piggyback slew-gating (DUAL_RIG Phase 4), time-correlation flavor.

The OSC piggyback rides the RC16 mount, so when the RC16 slews/centers to a new
pointing the OSC is still exposing and that sub straddles two fields (2026-09-21:
~half the M31 OSC subs split across two pointings). The chosen fix (DUAL_RIG §6)
is time-correlation: infer the RC16's slew windows from its own frames, then any
OSC sub whose exposure overlaps a window is a straddler and can be dropped
BEFORE image analysis — no pixel inspection needed.

Everything here is pure and structure-based (dicts with datetimes/coords), so it
is unit-testable without FITS or a live mount. Callers pass the RC16 frames and
the OSC subs; the grader consumes the `straddled_slew` tag.
"""
from __future__ import annotations

import math
from datetime import timedelta


def sep_arcmin(ra1_deg, dec1_deg, ra2_deg, dec2_deg) -> float:
    """Angular separation (arcminutes) between two RA/Dec points, via the
    haversine formula. Any None input -> inf (treated as 'moved')."""
    if None in (ra1_deg, dec1_deg, ra2_deg, dec2_deg):
        return float("inf")
    r1, d1, r2, d2 = map(math.radians, (ra1_deg, dec1_deg, ra2_deg, dec2_deg))
    dr, dd = r2 - r1, d2 - d1
    a = (math.sin(dd / 2) ** 2
         + math.cos(d1) * math.cos(d2) * math.sin(dr / 2) ** 2)
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(a)))) * 60.0


def _moved(a: dict, b: dict, min_move_arcmin: float) -> bool:
    """Did the mount move between frame a and frame b? Prefer RA/Dec (a real
    pointing change), else fall back to a target-name change."""
    if a.get("ra") is not None and b.get("ra") is not None:
        return sep_arcmin(a["ra"], a["dec"], b["ra"], b["dec"]) >= min_move_arcmin
    ta, tb = a.get("target"), b.get("target")
    return ta is not None and tb is not None and ta != tb


def infer_slew_windows(rc16_frames, min_move_arcmin: float = 5.0,
                       settle_s: float = 30.0):
    """From time-ordered RC16 frames [{start,end,ra,dec,target}], return the
    [(window_start, window_end)] intervals during which the mount was slewing/
    settling — i.e. between two consecutive frames whose pointing moved. The
    window runs from the previous frame's END to the next frame's START, padded
    by `settle_s` on each side to cover the centering/settle the next frame
    already absorbed."""
    frames = sorted((f for f in rc16_frames
                     if f.get("start") is not None and f.get("end") is not None),
                    key=lambda f: f["start"])
    pad = timedelta(seconds=settle_s)
    windows = []
    for a, b in zip(frames, frames[1:]):
        if _moved(a, b, min_move_arcmin):
            windows.append((a["end"] - pad, b["start"] + pad))
    return windows


def straddles(sub_start, sub_end, windows) -> bool:
    """True if the OSC sub's [start,end] exposure overlaps any slew window."""
    for w_start, w_end in windows:
        if sub_start < w_end and w_start < sub_end:   # interval overlap
            return True
    return False


def tag_straddlers(osc_subs, rc16_frames, min_move_arcmin: float = 5.0,
                   settle_s: float = 30.0) -> int:
    """Annotate each OSC sub dict ({start,end,...}) with `straddled_slew` = True
    when its exposure overlaps an inferred RC16 slew window. Returns the count
    tagged. A sub missing start/end is left untagged (unknown, never dropped)."""
    windows = infer_slew_windows(rc16_frames, min_move_arcmin, settle_s)
    n = 0
    for s in osc_subs:
        st, en = s.get("start"), s.get("end")
        hit = (st is not None and en is not None and straddles(st, en, windows))
        s["straddled_slew"] = bool(hit)
        n += int(hit)
    return n
