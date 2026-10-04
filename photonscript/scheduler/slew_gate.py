"""Piggyback slew-gating (DUAL_RIG Phase 4), time-correlation flavor.

The OSC piggyback rides the RC16 mount, so when the RC16 slews/centers to a new
pointing the OSC is still exposing and that sub straddles two fields (2026-09-21:
~half the M31 OSC subs split across two pointings). The chosen fix (DUAL_RIG §6)
is time-correlation: infer the RC16's slew windows from its own frames, then any
OSC sub whose exposure overlaps a window is a straddler and can be dropped
BEFORE image analysis — no pixel inspection needed.

The window and straddle functions are pure and structure-based (dicts with
datetimes/coords), so they are unit-testable without FITS or a live mount;
only NightWindows (when built from records) and night_pass read files. Callers pass the RC16 frames and
the OSC subs; the grader consumes the `straddled_slew` tag.

PS-13 wires this into grading as the scorecard check `slew_straddle`
(shared.qa_rules) for every rig whose NINA does not own the mount (the
Piggy-600):

- Windows: from the PS-67 mount log when the night has one
  (windows_from_mount_log: every slewing segment plus the pier / park /
  unpark events, padded by slew_gate_pad_s, 10 s), else inferred from
  the night's RC16 frames (infer_slew_windows over flexure.frames_from_records: older
  nights). A sub the mount log does not cover (poll stopped) falls back to
  the RC16 frames too.
- Live: telescope_agent._process_new_image grades each Piggy sub against
  the windows known at readout (NightWindows.assess).
- Dawn: night_pass() (backfill, after the pointing pass and before the
  Library build; CLI slew-backfill) recomputes every Piggy sub with the
  whole night's data and swaps only the slew_straddle row of its stored
  scorecard. Human verdicts are never changed.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

# padding on each side of a mount-log window (slew_gate_pad_s): the 5 s poll
# granularity plus a few seconds of settle. Header-inferred windows keep
# infer_slew_windows settle_s (30 s): they are guesses from frame gaps.
PAD_S = 10.0
MIN_MOVE_ARCMIN = 5.0     # a pointing change this big is a move
# mount-log lines that mark a one-off mount event (slews have segments)
EVENT_WHYS = ("pier", "park", "unpark")


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


# --------------------------------------------------------------------------
# PS-13: windows from the mount log, per-sub assessment, the dawn pass
# --------------------------------------------------------------------------

def merge_windows(windows):
    """Sort and merge overlapping (start, end) windows."""
    out: list[list] = []
    for a, b in sorted(w for w in windows
                       if w[0] is not None and w[1] is not None):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def windows_from_mount_log(lines, pad_s: float = PAD_S,
                           min_move_arcmin: float = MIN_MOVE_ARCMIN):
    """Move windows from a night's mount-log lines (shared.mount_log.load):
    every slewing segment (mount_log.slew_windows: opens on the first
    slewing=true line, closes on the next slewing=false one) padded by pad_s
    on both sides, plus pad_s around every pier change (meridian flip), park
    and unpark (the events mount_log.moved_during counts), and around a
    "move" line that jumped min_move_arcmin or more from the line before it
    (a short centering slew the 5 s poll never saw as Slewing). Merged."""
    from photonscript.shared import mount_log
    pad = timedelta(seconds=float(pad_s or 0))
    wins = [(a - pad, b + pad) for a, b in mount_log.slew_windows(lines)]
    prev = None
    for r in lines:
        why = r.get("why")
        if why in EVENT_WHYS:
            wins.append((r["dt"] - pad, r["dt"] + pad))
        elif why == "move" and prev is not None and sep_arcmin(
                prev.get("ra"), prev.get("dec"), r.get("ra"),
                r.get("dec")) >= min_move_arcmin:
            wins.append((prev["dt"] - pad, r["dt"] + pad))
        prev = r
    return merge_windows(wins)


def mount_log_covers(lines, start: datetime, end: datetime,
                     stale_s: float | None = None) -> bool:
    """Does the mount log say what the mount did during [start, end]? The
    last line at or before the start must be fresh (the poll writes on every
    change and a heartbeat while tracking) or say the mount was not tracking
    (parked / idle: no heartbeat, the state holds), or a line must fall
    inside the exposure."""
    from photonscript.shared import mount_log
    if not lines or start is None or end is None:
        return False
    stale = float(stale_s if stale_s is not None else mount_log.STALE_S)
    before = [r for r in lines if r["dt"] <= start]
    if before:
        last = before[-1]
        if (start - last["dt"]).total_seconds() <= stale \
                or not last.get("tracking"):
            return True
    return any(start < r["dt"] <= end for r in lines)


def overlap_s(sub_start, sub_end, windows) -> float:
    """Seconds of [sub_start, sub_end] inside the (merged) windows."""
    tot = 0.0
    for a, b in windows:
        ov = (min(sub_end, b) - max(sub_start, a)).total_seconds()
        if ov > 0:
            tot += ov
    return tot


def sub_straddle(sub_start, sub_end, windows, src: str) -> dict:
    """{"overlap_s", "note", "src"} for one sub against one window source
    ("mount-log" | "rc16-frames"). overlap_s 0 = clear of every move."""
    if sub_start is None or sub_end is None:
        return {"overlap_s": None, "note": None, "src": None}
    hits = [(a, b) for a, b in windows if sub_start < b and a < sub_end]
    if not hits:
        return {"overlap_s": 0.0, "note": None, "src": src}
    what = "mount log" if src == "mount-log" else "RC16 frames"
    a, b = hits[0]
    more = f" +{len(hits) - 1} more" if len(hits) > 1 else ""
    return {"overlap_s": round(overlap_s(sub_start, sub_end, hits), 1),
            "note": (f"RC16 move {a:%H:%M:%S} to {b:%H:%M:%S} UTC{more} "
                     f"({what})"),
            "src": src}


def config_pad(config) -> float:
    v = getattr(config, "slew_gate_pad_s", PAD_S)
    return PAD_S if v is None else float(v)


def config_min_move(config) -> float:
    v = getattr(config, "slew_gate_min_move_arcmin", MIN_MOVE_ARCMIN)
    return float(v) if v else MIN_MOVE_ARCMIN


class NightWindows:
    """A night's move windows from both sources, built lazily once: the
    mount log (preferred) and the RC16 frames (older nights, and subs the
    mount log does not cover). Pass `rc16_frames` (flexure frames) or
    `records` (the night's subs; the RC16 frames are then built with
    flexure.frames_from_records, which reads header RA/Dec when missing)."""

    def __init__(self, config, lines=None, rc16_frames=None, records=None):
        self.config = config
        self.lines = lines or []
        self.pad = config_pad(config)
        self.min_move = config_min_move(config)
        self._records = records
        self._rc16 = rc16_frames
        self._mount_wins = None
        self._frame_wins: list | None = None
        self._frames_done = False

    @property
    def mount_windows(self):
        if self._mount_wins is None:
            self._mount_wins = (windows_from_mount_log(
                self.lines, self.pad, self.min_move) if self.lines else [])
        return self._mount_wins

    @property
    def frame_windows(self):
        """Header-inferred windows, or None with fewer than two RC16 frames
        (nothing to compare)."""
        if not self._frames_done:
            self._frames_done = True
            frames = self._rc16
            if frames is None and self._records is not None:
                from photonscript.scheduler.flexure import RC16, frames_from_records
                frames = frames_from_records(self._records, self.config, RC16)
            if frames and len(frames) > 1:
                self._frame_wins = infer_slew_windows(
                    [{"start": f["start"], "end": f["end"], "ra": f.get("ra"),
                      "dec": f.get("dec"), "target": f.get("target")}
                     for f in frames], self.min_move)
        return self._frame_wins

    def assess(self, start, end) -> dict:
        """The sub's straddle record; overlap_s None when nothing can say
        (the mount log does not cover it and there are fewer than two RC16
        frames)."""
        if start is None or end is None:
            return sub_straddle(None, None, [], "")
        if self.lines and mount_log_covers(self.lines, start, end):
            return sub_straddle(start, end, self.mount_windows, "mount-log")
        fw = self.frame_windows
        if fw is None:
            return {"overlap_s": None, "note": None, "src": None}
        return sub_straddle(start, end, fw, "rc16-frames")


def gated_rigs(config) -> list[str]:
    """Rigs whose subs are gated: every enabled rig whose NINA does not own
    the mount (the Piggy-600 rides the RC16's)."""
    from photonscript.shared.rigs import rig_devices, rig_ids
    return [r for r in rig_ids(config) if "mount" not in rig_devices(r)]


def night_pass(config, date: str, apply: bool = True) -> dict:
    """Dawn pass: grade every gated-rig sub of a night against the whole
    night's move windows and swap only the slew_straddle row of its stored
    scorecard (qa_rules.regrade_slew_straddle). Human verdicts are never
    touched; a sub that becomes rejected leaves the Library (links move to
    Library/_rejected/, as the PS-67 pointing pass does). Returns counts and
    the night's split rate (straddled / judged)."""
    from photonscript.scheduler.flexure import frames_from_records
    from photonscript.scheduler.pointing_record import _move_out_of_library
    from photonscript.scheduler.runs import (_human_verdict, _load_subs,
                                             _rewrite_subs, sync_goal_progress)
    from photonscript.shared import mount_log, qa_rules

    subs = _load_subs(config, date)
    lines = mount_log.load(config, date)
    nw = NightWindows(config, lines=lines, records=subs)
    out = {"date": date, "subs": 0, "judged": 0, "straddled": 0,
           "unknown": 0, "src": {}, "mount_log_lines": len(lines),
           "records_updated": 0, "verdicts_changed": 0, "newly_rejected": 0,
           "library_moves": 0, "split_rate": None}
    changed = 0
    for rig in gated_rigs(config):
        for f in frames_from_records(subs, config, rig, read_headers=False):
            r = f["rec"]
            out["subs"] += 1
            a = nw.assess(f["start"], f["end"])
            ov = a.get("overlap_s")
            if ov is None:
                out["unknown"] += 1
            else:
                out["judged"] += 1
                out["straddled"] += int(ov > 0)
                out["src"][a["src"]] = out["src"].get(a["src"], 0) + 1
            if not apply or _human_verdict(r):
                continue
            t = qa_rules.thresholds(config, rig, r.get("target"),
                                    r.get("filter"))
            fields = qa_rules.regrade_slew_straddle(r, ov, t, a.get("note"))
            if fields is None:
                continue
            was = bool(r.get("passed_qa"))
            was_verdict = r.get("auto_verdict")
            if not fields.get("reviewed") and r.get("review_source") == "auto":
                fields.update(reviewed=False, review_source=None)
            r.update(fields)
            if r.get("review_source") is None:
                r.pop("review_source", None)
            changed += 1
            if was_verdict != r.get("auto_verdict"):
                out["verdicts_changed"] += 1
            if was and not r.get("passed_qa"):
                out["newly_rejected"] += 1
                out["library_moves"] += len(_move_out_of_library(config, r))
    if apply and changed:
        _rewrite_subs(config, date, subs)
        if out["verdicts_changed"]:
            sync_goal_progress(config)
    out["records_updated"] = changed
    if out["judged"]:
        out["split_rate"] = round(out["straddled"] / out["judged"], 3)
    return out
