"""PHD2 log discovery and the night guide-log summary (PS-73).

Discovery: PHD2 writes PHD2_GuideLog_*.txt and PHD2_DebugLog_*.txt into
<Documents>\\PHD2 of the Windows user it runs as, unless its Global settings
point the log folder elsewhere. phd2_logs_dir (';' separates several) is
searched first; when it holds no logs the usual places are searched too
(this user's Documents and OneDrive Documents, then every user's), so logs
written by another account or under OneDrive are still found and the
/api/phd2/logs read-out says where.

Summary: parses a PHD2 guide log (log version 2.x) into calibration events
(Dec, hour angle, pier side, RA/Dec angle and rate, completed or failed) and
guiding sessions (frames, star-lost drops, dithers, RMS RA/Dec in pixels and
arcsec). The guide log records distances in guide-camera PIXELS; each
section's own "Pixel scale = ... arc-sec/px" header converts them. A missing
scale, or PHD2's 1.00 placeholder when its profile has no focal length, falls
back to the configured guide optics (PS-70), and otherwise stays pixels.
Read-only; nothing here talks to PHD2.
"""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime
from pathlib import Path

from photonscript.scheduler import log_files as lf

GUIDE = "PHD2_GuideLog*"
DEBUG = "PHD2_DebugLog*"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _users_roots() -> list[Path]:
    """Folders holding Windows user profiles (C:\\Users). Empty elsewhere."""
    drive = os.environ.get("SystemDrive", "C:")
    root = Path(drive + "\\Users")
    return [root] if os.name == "nt" and root.is_dir() else []


def candidate_dirs(config) -> list[tuple[str, str]]:
    """(dir, why) in search order, de-duplicated."""
    out: list[tuple[str, str]] = []
    for d in str(getattr(config, "phd2_logs_dir", "") or "").split(";"):
        if d.strip():
            out.append((d.strip(), "configured"))
    home = Path.home()
    out.append((str(home / "Documents" / "PHD2"), "this user's Documents"))
    out.append((str(home / "OneDrive" / "Documents" / "PHD2"),
                "this user's OneDrive Documents"))
    for root in _users_roots():
        for pat in ("*/Documents/PHD2", "*/OneDrive*/Documents/PHD2"):
            for p in sorted(root.glob(pat)):
                out.append((str(p), "another user profile"))
    seen, uniq = set(), []
    for d, why in out:
        k = os.path.normcase(os.path.normpath(d))
        if k not in seen:
            seen.add(k)
            uniq.append((d, why))
    return uniq


def _logs_in(d: str, pattern: str) -> list[Path]:
    p = Path(d)
    if not p.is_dir():
        return []
    try:
        return [x for x in p.rglob(pattern) if x.is_file()]
    except OSError:
        return []


def find_logs(config, kind: str = "guide") -> dict:
    """{files (oldest first), dir, why, searched: [{dir, why, exists, guide,
    debug}]}. The first searched folder holding logs of this kind wins."""
    pattern = DEBUG if str(kind).lower().startswith("debug") else GUIDE
    searched, chosen = [], None
    for d, why in candidate_dirs(config):
        g, dbg = _logs_in(d, GUIDE), _logs_in(d, DEBUG)
        searched.append({"dir": d, "why": why, "exists": Path(d).is_dir(),
                         "guide": len(g), "debug": len(dbg)})
        if chosen is None and (g if pattern == GUIDE else dbg):
            chosen = (d, why, g if pattern == GUIDE else dbg)
    if chosen is None:
        return {"files": [], "dir": None, "why": None, "searched": searched,
                "pattern": pattern}
    d, why, files = chosen
    files.sort(key=lambda p: p.stat().st_mtime)
    return {"files": files, "dir": d, "why": why, "searched": searched,
            "pattern": pattern}


def select(files: list[Path], date: str = "", file: str = "") -> tuple[list[Path], str]:
    """Pick by exact file name, by night (date=YYYY-MM-DD), or the newest.
    Returns (paths, note); an empty list comes with the reason."""
    if file:
        name = lf.safe_name(file)
        if name is None:
            return [], f"bad file name {file!r}"
        hit = [p for p in files if p.name == name]
        return (hit, "") if hit else ([], f"no PHD2 log named {name}")
    if date:
        try:
            hit = [p for p in files if lf.in_night(p, date)]
        except ValueError:
            return [], f"bad date {date!r} (use YYYY-MM-DD)"
        hit.sort(key=lambda p: lf.file_start(p.name) or lf.mtime_local(p))
        return (hit, "") if hit else ([], f"no PHD2 log covers the night of {date}")
    return (files[-1:], "") if files else ([], "no PHD2 logs")


# --------------------------------------------------------------------------
# Guide-log parser
# --------------------------------------------------------------------------
#
# Frame rows end with PHD2's FindResult code (star.h). STAR_OK and
# STAR_SATURATED are guided frames: PHD2 guides on a saturated star (its
# graph shows "SAT"), so a night where every frame is code 1 is still a
# guided night (PS-88: 2026-09-26 read as 0 frames / 4075 dropped). DROP
# rows carry PHD2's own quoted reason as a 19th field; that text is the
# source of truth, the names below only fill in when it is missing.

FIND_RESULT = {0: "STAR_OK", 1: "STAR_SATURATED", 2: "STAR_LOWSNR",
               3: "STAR_LOWMASS", 4: "STAR_LOWHFD", 5: "STAR_HIHFD",
               6: "STAR_TOO_NEAR_EDGE", 7: "STAR_MASSCHANGE", 8: "STAR_ERROR"}
_CODE_TEXT = {2: "Star lost - low SNR", 3: "Star lost - low mass",
              4: "Star lost - low HFD", 5: "Star lost - HFD too large",
              6: "Star lost - too near edge", 7: "Star lost - mass changed",
              8: "Star lost - error"}
GUIDED_CODES = (0, 1)

_F = r"(-?\d+(?:\.\d+)?)"
# Settings from each section header, first match wins (so the target's
# "Dec = 66.6 deg" is not overwritten by the "Norm rates ... Dec = 8.0"/s").
_RX = {
    "profile": re.compile(r"^Equipment Profile\s*=\s*(.+?)\s*$", re.IGNORECASE),
    "dither_mode": re.compile(r"^Dither\s*=\s*([^,]+)", re.IGNORECASE),
    "dither_scale": re.compile(r"Dither scale\s*=\s*" + _F, re.IGNORECASE),
    "noise_reduction": re.compile(r"Image noise reduction\s*=\s*([^,]+)", re.IGNORECASE),
    "pixel_scale": re.compile(r"Pixel scale\s*=\s*" + _F + r"\s*arc-?sec/px", re.IGNORECASE),
    "binning": re.compile(r"Binning\s*=\s*(\d+)", re.IGNORECASE),
    "focal_length_mm": re.compile(r"Focal length\s*=\s*" + _F + r"\s*mm", re.IGNORECASE),
    "search_region_px": re.compile(r"Search region\s*=\s*(\d+)\s*px", re.IGNORECASE),
    "mass_tolerance": re.compile(r"Star mass tolerance\s*=\s*([^,]+)", re.IGNORECASE),
    "multi_star_list": re.compile(r"list size\s*=\s*(\d+)", re.IGNORECASE),
    "camera": re.compile(r"^Camera\s*=\s*([^,]+)", re.IGNORECASE),
    "gain": re.compile(r"\bgain\s*=\s*" + _F, re.IGNORECASE),
    "dark_dur_ms": re.compile(r"dark dur\s*=\s*(\d+)", re.IGNORECASE),
    "pixel_size_um": re.compile(r"pixel size\s*=\s*" + _F + r"\s*um", re.IGNORECASE),
    "exposure_ms": re.compile(r"^Exposure\s*=\s*(\d+)\s*ms", re.IGNORECASE),
    "mount": re.compile(r"^Mount\s*=\s*([^,]+?)\s*(?:,|$)", re.IGNORECASE),
    "calibration_step_ms": re.compile(r"Calibration Step\s*=\s*(\d+)\s*ms", re.IGNORECASE),
    "calibration_distance_px": re.compile(r"Calibration Distance\s*=\s*(\d+)\s*px", re.IGNORECASE),
    "assume_orthogonal": re.compile(r"Assume orthogonal axes\s*=\s*(\w+)", re.IGNORECASE),
    "x_angle": re.compile(r"xAngle\s*=\s*" + _F), "x_rate": re.compile(r"xRate\s*=\s*" + _F),
    "y_angle": re.compile(r"yAngle\s*=\s*" + _F), "y_rate": re.compile(r"yRate\s*=\s*" + _F),
    "parity": re.compile(r"parity\s*=\s*([^,\s]+)"),
    "norm_rate_ra": re.compile(r"Norm rates RA\s*=\s*" + _F),
    "norm_rate_dec": re.compile(r"Norm rates RA.*?Dec\s*=\s*" + _F),
    "ortho_err_deg": re.compile(r"ortho\.?\s*err\.?\s*=\s*" + _F, re.IGNORECASE),
    "x_algorithm": re.compile(r"^X guide algorithm\s*=\s*([^,]+)", re.IGNORECASE),
    "y_algorithm": re.compile(r"^Y guide algorithm\s*=\s*([^,]+)", re.IGNORECASE),
    "backlash_comp": re.compile(r"Backlash comp\s*=\s*(\w+)", re.IGNORECASE),
    "backlash_pulse_ms": re.compile(r"Backlash comp.*?pulse\s*=\s*(\d+)", re.IGNORECASE),
    "max_ra_ms": re.compile(r"Max RA duration\s*=\s*(\d+)", re.IGNORECASE),
    "max_dec_ms": re.compile(r"Max DEC duration\s*=\s*(\d+)", re.IGNORECASE),
    "dec_mode": re.compile(r"DEC guide mode\s*=\s*(\w+)", re.IGNORECASE),
    "ra_guide_speed": re.compile(r"RA Guide Speed\s*=\s*" + _F + r"\s*a-s/s", re.IGNORECASE),
    "dec_guide_speed": re.compile(r"Dec Guide Speed\s*=\s*" + _F + r"\s*a-s/s", re.IGNORECASE),
    "cal_dec": re.compile(r"Cal Dec\s*=\s*([^,]+)", re.IGNORECASE),
    "last_cal_issue": re.compile(r"Last Cal Issue\s*=\s*([^,]+)", re.IGNORECASE),
    "cal_timestamp": re.compile(r"Last Cal Issue.*?Timestamp\s*=\s*(.+?)\s*$", re.IGNORECASE),
    "ra_hr": re.compile(r"^RA\s*=\s*" + _F + r"\s*hr", re.IGNORECASE),
    "dec_deg": re.compile(r"(?<![A-Za-z])Dec\s*=\s*" + _F + r"\s*deg", re.IGNORECASE),
    "hour_angle_hr": re.compile(r"Hour angle\s*=\s*" + _F + r"\s*hr", re.IGNORECASE),
    "pier_side": re.compile(r"Pier side\s*=\s*([A-Za-z]+)", re.IGNORECASE),
    "alt_deg": re.compile(r"Alt\s*=\s*" + _F + r"\s*deg", re.IGNORECASE),
    "az_deg": re.compile(r"Az\s*=\s*" + _F + r"\s*deg", re.IGNORECASE),
    "hfd_px": re.compile(r"HFD\s*=\s*" + _F + r"\s*px", re.IGNORECASE),
}
_TEXT_KEYS = {"profile", "dither_mode", "noise_reduction", "mass_tolerance",
              "camera", "mount", "assume_orthogonal", "parity", "x_algorithm",
              "y_algorithm", "backlash_comp", "dec_mode", "cal_dec",
              "last_cal_issue", "cal_timestamp", "pier_side"}
_ALGO_PARAM = re.compile(r"([A-Za-z][A-Za-z .]*?)\s*=\s*" + _F)
_CAL_AXIS = re.compile(r"^(West|East|North|South|Backlash)\s+calibration complete\.?"
                       r"\s*Angle\s*=\s*" + _F + r"\s*deg,\s*Rate\s*=\s*" + _F
                       + r"(?:.*?Parity\s*=\s*(\w+))?", re.IGNORECASE)
_CAL_STEP = re.compile(r"^(West|East|North|South|Backlash),(\d+),", re.IGNORECASE)
_BEGIN = re.compile(r"^(Calibration|Guiding) Begins at (.+?)\s*$", re.IGNORECASE)
_END = re.compile(r"^(Calibration|Guiding) Ends at (.+?)\s*$", re.IGNORECASE)
_LOG_MARK = re.compile(r"^(?:PHD2 version.*Log enabled at|Log closed at)", re.IGNORECASE)
_DITHER = re.compile(r"DITHER by\s*" + _F + r"\s*,\s*" + _F, re.IGNORECASE)
_LOCK = re.compile(r"new lock pos\s*=\s*" + _F + r"\s*,\s*" + _F, re.IGNORECASE)
_PARAM = re.compile(r"Guiding parameter change,\s*(.+?)\s*=\s*(.+?)\s*$", re.IGNORECASE)
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rms(xs):
    return (sum(x * x for x in xs) / len(xs)) ** 0.5 if xs else None


def parse_ts(s: str | None) -> datetime | None:
    """PHD2's 'YYYY-MM-DD HH:MM:SS' (the scope PC's LOCAL clock) -> naive."""
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime((s or "").strip(), fmt)
        except ValueError:
            continue
    return None


def _new(kind, start, source):
    return {"kind": kind, "start": start, "start_local": parse_ts(start),
            "end": None, "end_local": None, "closed": None, "file": source,
            "header": {}, "frames": [], "cols": None,
            "settling": False, "lock_epoch": 0, "events": [], "cal_steps": {},
            "cal_lost": {}, "output": True}


def _header(sec, line):
    h = sec["header"]
    for k, rx in _RX.items():
        if k in h:
            continue
        m = rx.search(line)
        if m:
            v = m.group(1).strip()
            h[k] = v if k in _TEXT_KEYS else _num(v)
    low = line.lower()
    if low.startswith("mount") and "guide_output" not in h and (
            "guiding enabled" in low or "guiding disabled" in low):
        h["guide_output"] = "guiding enabled" in low
        sec["output"] = h["guide_output"]
    if "search region" in low and "multi_star" not in h:
        h["multi_star"] = "multi-star" in low
    if low.lstrip().startswith("camera") and "dark" in low and "have_dark" not in h:
        h["have_dark"] = "have dark" in low
        h["defect_map"] = "defect map" in low and "no defect map" not in low
    for ax in ("x", "y"):
        if low.startswith(f"{ax} guide algorithm") and f"{ax}_params" not in h:
            h[f"{ax}_params"] = {m.group(1).strip().lower(): _num(m.group(2))
                                 for m in _ALGO_PARAM.finditer(line.split(",", 1)[-1])}


def _close(cur, how, when=None):
    if cur is not None and cur["closed"] is None:
        cur["closed"] = how
        cur["end"] = when
        cur["end_local"] = parse_ts(when) if when else None


def _frame(cols, vals, settling, epoch, output=True):
    """One guide-frame row as a dict (raw distances in guide-camera px)."""
    def col(name):
        try:
            i = cols.index(name)
        except ValueError:
            return None
        return vals[i] if i < len(vals) else None
    mount = (col("mount") or "").strip().strip('"')
    code = _num(col("ErrorCode"))
    code = int(code) if code is not None else 0
    msg = vals[len(cols)].strip().strip('"') if len(vals) > len(cols) else ""
    ra, dec = _num(col("RARawDistance")), _num(col("DECRawDistance"))
    drop = mount.upper() == "DROP" or ra is None or dec is None or \
        code not in GUIDED_CODES
    return {"n": int(_num(col("Frame")) or 0), "t": _num(col("Time")) or 0.0,
            "mount": mount, "ra": ra, "dec": dec,
            "ra_ms": _num(col("RADuration")) or 0.0,
            "ra_dir": (col("RADirection") or "").strip().upper(),
            "dec_ms": _num(col("DECDuration")) or 0.0,
            "dec_dir": (col("DECDirection") or "").strip().upper(),
            "mass": _num(col("StarMass")), "snr": _num(col("SNR")),
            "code": code, "drop": drop,
            "reason": (msg or _CODE_TEXT.get(code) or f"error code {code}")
            if drop else None,
            "settling": settling, "epoch": epoch, "output": output}


def parse_guide_log(text: str, source: str = "") -> list[dict]:
    """Sections (calibration / guiding) of one PHD2 guide log, in file order.

    A section ends at its own 'Ends at' line, at the next 'Begins at' (PHD2
    does not always write an end: a calibration started while guiding
    interrupts the guiding section), or at 'Log closed'. Guiding sections
    hold `frames` (dicts, see _frame) and `events` (dither, settle, lock,
    param, info) tagged with the index of the next frame, so each event can
    be placed in time. Calibration sections hold the steps per direction,
    the axis results and star-lost counts."""
    sections, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _BEGIN.match(line)
        if m:
            if cur is not None:
                _close(cur, "interrupted", m.group(2))
            cur = _new(m.group(1).lower(), m.group(2), source)
            sections.append(cur)
            continue
        if _LOG_MARK.match(line):
            if cur is not None:
                w = line.split(" at ", 1)[-1] if line.lower().startswith("log closed") else None
                _close(cur, "log closed", w)
            cur = None
            continue
        if cur is None:
            continue
        m = _END.match(line)
        if m:
            _close(cur, "ended" if m.group(1).lower() == cur["kind"] else
                   "aborted", m.group(2))
            cur = None
            continue
        low = line.lower()
        if cur["kind"] == "guiding" and low.startswith("frame,time,"):
            cur["cols"] = [c.strip() for c in line.split(",")]
            continue
        if cur["kind"] == "calibration":
            if low.startswith("direction,step"):
                continue
            m = _CAL_AXIS.match(line)
            if m:
                cur["events"].append(("axis", m.group(1).title(),
                                      _num(m.group(2)), _num(m.group(3)),
                                      m.group(4)))
                continue
            m = _CAL_STEP.match(line)
            if m:
                parts = line.split(",")
                cur["cal_steps"].setdefault(m.group(1).title(), []).append(
                    (int(m.group(2)), _num(parts[-1])))
                continue
            if "star lost during calibration" in low:
                why = line.split("Status=", 1)[-1].strip() if "status=" in low \
                    else "star lost"
                cur["cal_lost"][why] = cur["cal_lost"].get(why, 0) + 1
                continue
            if low.startswith("calibration complete") or \
                    "calibration complete, mount" in low:
                cur["events"].append(("complete", line))
                continue
            if "calibration failed" in low or low.startswith("error") or \
                    ("calibration" in low and "alert" in low):
                cur["events"].append(("failed", line))
                continue
        if cur["kind"] == "guiding" and cur["cols"] and line[:1].isdigit():
            try:
                vals = next(csv.reader([line]))
            except (csv.Error, StopIteration):
                continue
            cur["frames"].append(_frame(cur["cols"], vals, cur["settling"],
                                        cur["lock_epoch"], cur["output"]))
            continue
        if low.startswith("info:"):
            i = len(cur["frames"])
            body = line[5:].strip()
            if "settling state change" in low:
                st = ("started" if "started" in low else "complete"
                      if "complete" in low else "failed" if "failed" in low
                      else "other")
                cur["events"].append(("settle", i, st))
                cur["settling"] = st == "started"
            elif _DITHER.search(line):
                d = _DITHER.search(line)
                cur["lock_epoch"] += 1
                cur["events"].append(("dither", i, _num(d.group(1)),
                                      _num(d.group(2)), line))
            elif "set lock position" in low or _LOCK.search(line):
                cur["lock_epoch"] += 1
                cur["events"].append(("lock", i, body))
            elif _PARAM.search(line):
                p = _PARAM.search(line)
                cur["events"].append(("param", i, p.group(1).strip(),
                                      p.group(2).strip()))
                if p.group(1).strip().lower() == "mountguidingenabled":
                    cur["output"] = p.group(2).strip().lower() == "true"
                if p.group(1).strip().lower() == "exposure":
                    cur["header"].setdefault("exposure_changes", []).append(
                        _num(p.group(2).split()[0]))
            elif body.lower().startswith("ga result"):
                cur["events"].append(("ga", i, body.split("-", 1)[-1].strip()))
            else:
                cur["events"].append(("info", i, body))
            continue
        _header(cur, line)
    return sections


def _scale_for(h: dict, config) -> tuple[float | None, str | None]:
    s = h.get("pixel_scale")
    if s and s > 0 and abs(s - 1.0) > 1e-9:
        return s, "log"
    if config is not None:
        from photonscript.telescope_agent.phd2_client import guide_scale_from_config
        c = guide_scale_from_config(config, int(h.get("binning") or 1))
        if c:
            return c, "config"
    return None, None


def ortho_error(x_angle, y_angle) -> float | None:
    """Degrees the RA and Dec axes are away from perpendicular, the way PHD2
    reports 'ortho.err.': 86.6 / 11.7 -> 15.1; 91.6 / 3.4 -> 1.8."""
    if x_angle is None or y_angle is None:
        return None
    d = abs((x_angle - y_angle + 180.0) % 360.0 - 180.0)
    return round(abs(90.0 - d), 1)


def _calibration(sec) -> dict:
    h = sec["header"]
    axes = {e[1]: {"angle_deg": e[2], "rate_px_s": e[3]}
            for e in sec["events"] if e[0] == "axis"}
    failed = [e[1] for e in sec["events"] if e[0] == "failed"]
    done = any(e[0] == "complete" for e in sec["events"])
    steps = {d: max((s for s, _ in v), default=0)
             for d, v in sec["cal_steps"].items()}
    bl = sec["cal_steps"].get("Backlash") or []
    x, y = axes.get("West", {}), axes.get("North", {})
    return {"start": sec["start"], "end": sec["end"], "file": sec["file"],
            "profile": h.get("profile"), "dec_deg": h.get("dec_deg"),
            "hour_angle_hr": h.get("hour_angle_hr"),
            "pier_side": h.get("pier_side"), "alt_deg": h.get("alt_deg"),
            "axes": axes,
            "result": ("failed" if failed else "complete" if done
                       else "aborted"),
            "messages": failed[:5],
            "steps": steps,
            "backlash_steps": max((s for s, _ in bl), default=None) if bl else None,
            "backlash_px": bl[-1][1] if bl else None,
            "star_lost": dict(sec["cal_lost"]),
            "step_ms": h.get("calibration_step_ms"),
            "distance_px": h.get("calibration_distance_px"),
            "ortho_err_deg": ortho_error(x.get("angle_deg"), y.get("angle_deg")),
            "exposure_ms": h.get("exposure_ms")}


def _session(sec, config) -> tuple[dict, list, list, dict]:
    h = sec["header"]
    scale, src = _scale_for(h, config)
    ra, dec, ra_s, dec_s = [], [], [], []
    dropped, reasons, saturated = 0, {}, 0
    for f in sec["frames"]:
        if f["drop"]:
            dropped += 1
            reasons[f["reason"]] = reasons.get(f["reason"], 0) + 1
            continue
        saturated += f["code"] == 1
        ra.append(f["ra"])
        dec.append(f["dec"])
        if not f["settling"]:
            ra_s.append(f["ra"])
            dec_s.append(f["dec"])
    star_lost = sum(n for k, n in reasons.items()
                    if "star lost" in k.lower() or "no star" in k.lower())

    def block(a, b):
        rp, dp = _rms(a), _rms(b)
        tp = (rp ** 2 + dp ** 2) ** 0.5 if rp is not None else None
        out = {"rms_ra_px": _r(rp), "rms_dec_px": _r(dp), "rms_total_px": _r(tp)}
        k = scale
        out.update({"rms_ra_arcsec": _r(rp * k) if k and rp is not None else None,
                    "rms_dec_arcsec": _r(dp * k) if k and dp is not None else None,
                    "rms_total_arcsec": _r(tp * k) if k and tp is not None else None})
        return out

    s = {"start": sec["start"], "end": sec["end"], "file": sec["file"],
         "profile": h.get("profile"),
         "pixel_scale_arcsec": _r(scale, 4), "scale_source": src,
         "units": "arcsec" if scale else "px",
         "binning": int(h["binning"]) if h.get("binning") else None,
         "focal_length_mm": h.get("focal_length_mm"),
         "exposure_ms": h.get("exposure_ms"),
         "dec_deg": h.get("dec_deg"), "hour_angle_hr": h.get("hour_angle_hr"),
         "pier_side": h.get("pier_side"), "cal_dec": h.get("cal_dec"),
         "last_cal_issue": h.get("last_cal_issue"),
         "frames": len(ra), "saturated_frames": saturated,
         "dropped": dropped, "star_lost": star_lost,
         "dithers": sum(1 for e in sec["events"] if e[0] == "dither"),
         **block(ra, dec),
         "settled": block(ra_s, dec_s),
         "peak_ra_px": _r(max((abs(x) for x in ra), default=None)),
         "peak_dec_px": _r(max((abs(x) for x in dec), default=None))}
    k = scale or 0.0
    return s, [x * k for x in ra] if scale else [], [x * k for x in dec] if scale else [], reasons


def _r(v, n=3):
    return round(v, n) if isinstance(v, (int, float)) else None


def summarize_sections(sections: list[dict], config=None) -> dict:
    cals = [_calibration(s) for s in sections if s["kind"] == "calibration"]
    sess, all_ra, all_dec, reasons = [], [], [], {}
    unscaled = 0
    for s in sections:
        if s["kind"] != "guiding":
            continue
        row, ra_as, dec_as, rs = _session(s, config)
        if not row["frames"] and not row["dropped"]:
            continue
        sess.append(row)
        if row["units"] == "px":
            unscaled += row["frames"]
        all_ra += ra_as
        all_dec += dec_as
        for k, n in rs.items():
            reasons[k] = reasons.get(k, 0) + n
    rra, rdec = _rms(all_ra), _rms(all_dec)
    frames = sum(s["frames"] for s in sess)
    return {
        "totals": {
            "guiding_sessions": len(sess),
            "frames": frames,
            "saturated_frames": sum(s["saturated_frames"] for s in sess),
            "dropped_frames": sum(s["dropped"] for s in sess),
            "star_lost": sum(s["star_lost"] for s in sess),
            "dithers": sum(s["dithers"] for s in sess),
            "calibrations": len(cals),
            "calibrations_failed": sum(1 for c in cals if c["result"] == "failed"),
            "rms_ra_arcsec": _r(rra), "rms_dec_arcsec": _r(rdec),
            "rms_total_arcsec": (_r((rra ** 2 + rdec ** 2) ** 0.5)
                                 if rra is not None else None),
            "frames_without_scale": unscaled,
        },
        "calibrations": cals,
        "sessions": sess,
        "star_lost_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
    }


def night_sections(config, date: str = "", file: str = "") -> tuple[list, dict]:
    """(sections from every guide log of the night, info) where info holds
    ok/note/searched/dir/files. Shared by the summary and PS-88's analysis."""
    found = find_logs(config, "guide")
    paths, note = select(found["files"], date=date, file=file)
    if not paths:
        return [], {"ok": False, "note": note or "no PHD2 guide logs found",
                    "searched": found["searched"], "date": date or None}
    sections = []
    for p in paths:
        sections += parse_guide_log(
            Path(p).read_text(encoding="utf-8", errors="replace"), p.name)
    return sections, {"ok": True, "date": date or None, "dir": found["dir"],
                      "files": [lf.describe(p) for p in paths],
                      "paths": [str(p) for p in paths]}


def night_summary(config, date: str = "", file: str = "") -> dict:
    """GET /api/phd2/summary: the night's guide logs, summarized."""
    sections, info = night_sections(config, date=date, file=file)
    if not info["ok"]:
        return info
    out = summarize_sections(sections, config)
    info.pop("paths", None)
    out.update(info)
    return out
