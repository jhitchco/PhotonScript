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

_F = r"(-?\d+(?:\.\d+)?)"
_RX = {
    "pixel_scale": re.compile(r"Pixel scale\s*=\s*" + _F + r"\s*arc-?sec/px", re.IGNORECASE),
    "binning": re.compile(r"Binning\s*=\s*(\d+)", re.IGNORECASE),
    "focal_length_mm": re.compile(r"Focal length\s*=\s*" + _F + r"\s*mm", re.IGNORECASE),
    "profile": re.compile(r"Equipment Profile\s*=\s*(.+?)\s*$", re.IGNORECASE),
    "exposure_ms": re.compile(r"^Exposure\s*=\s*(\d+)\s*ms", re.IGNORECASE),
    "dec_deg": re.compile(r"(?<![A-Za-z])Dec\s*=\s*" + _F + r"\s*deg", re.IGNORECASE),
    "hour_angle_hr": re.compile(r"Hour angle\s*=\s*" + _F + r"\s*hr", re.IGNORECASE),
    "pier_side": re.compile(r"Pier side\s*=\s*([A-Za-z]+)", re.IGNORECASE),
    "alt_deg": re.compile(r"Alt\s*=\s*" + _F + r"\s*deg", re.IGNORECASE),
    "cal_dec": re.compile(r"Cal Dec\s*=\s*([^,]+)", re.IGNORECASE),
    "last_cal_issue": re.compile(r"Last Cal Issue\s*=\s*([^,]+)", re.IGNORECASE),
}
_CAL_AXIS = re.compile(r"^(West|East|North|South|Backlash)\s+calibration complete\.?"
                       r"\s*Angle\s*=\s*" + _F + r"\s*deg,\s*Rate\s*=\s*" + _F,
                       re.IGNORECASE)
_BEGIN = re.compile(r"^(Calibration|Guiding) Begins at (.+?)\s*$", re.IGNORECASE)
_END = re.compile(r"^(Calibration|Guiding) Ends at (.+?)\s*$", re.IGNORECASE)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rms(xs):
    return (sum(x * x for x in xs) / len(xs)) ** 0.5 if xs else None


def _new(kind, start, source):
    return {"kind": kind, "start": start, "end": None, "file": source,
            "header": {}, "rows": [], "cols": None, "settling": False,
            "events": []}


def _header(sec, line):
    h = sec["header"]
    for k, rx in _RX.items():
        if k in h:
            continue
        m = rx.search(line)
        if m:
            v = m.group(1).strip()
            h[k] = v if k in ("profile", "pier_side", "cal_dec",
                               "last_cal_issue") else _num(v)


def parse_guide_log(text: str, source: str = "") -> list[dict]:
    """Raw sections (calibration / guiding) of one PHD2 guide log."""
    sections, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _BEGIN.match(line)
        if m:
            cur = _new(m.group(1).lower(), m.group(2), source)
            sections.append(cur)
            continue
        if cur is None:
            continue
        m = _END.match(line)
        if m:
            cur["end"] = m.group(2)
            cur = None
            continue
        if cur["kind"] == "guiding" and line.lower().startswith("frame,time,mount"):
            cur["cols"] = [c.strip() for c in line.split(",")]
            continue
        if cur["kind"] == "calibration" and line.lower().startswith("direction,step"):
            continue
        low = line.lower()
        if cur["kind"] == "calibration":
            m = _CAL_AXIS.match(line)
            if m:
                cur["events"].append(("axis", m.group(1).title(),
                                      _num(m.group(2)), _num(m.group(3))))
                continue
            if "calibration complete" in low:
                cur["events"].append(("complete", line))
                continue
            if "calibration failed" in low or low.startswith("error"):
                cur["events"].append(("failed", line))
                continue
        if cur["kind"] == "guiding" and cur["cols"] and line[:1].isdigit():
            try:
                vals = next(csv.reader([line]))
            except (csv.Error, StopIteration):
                continue
            cur["rows"].append((vals, cur["settling"]))
            continue
        if low.startswith("info:"):
            if "settling started" in low:
                cur["settling"] = True
            elif "settling complete" in low or "settling failed" in low:
                cur["settling"] = False
            if "dither" in low and "settling state" not in low:
                cur["events"].append(("dither", line))
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


def _col(cols, row, name):
    try:
        i = cols.index(name)
    except ValueError:
        return None
    return row[i] if i < len(row) else None


def _calibration(sec) -> dict:
    h = sec["header"]
    axes = {e[1]: {"angle_deg": e[2], "rate_px_s": e[3]}
            for e in sec["events"] if e[0] == "axis"}
    failed = [e[1] for e in sec["events"] if e[0] == "failed"]
    done = any(e[0] == "complete" for e in sec["events"])
    return {"start": sec["start"], "end": sec["end"], "file": sec["file"],
            "profile": h.get("profile"), "dec_deg": h.get("dec_deg"),
            "hour_angle_hr": h.get("hour_angle_hr"),
            "pier_side": h.get("pier_side"), "alt_deg": h.get("alt_deg"),
            "axes": axes,
            "result": ("failed" if failed else "complete" if done
                       else "incomplete"),
            "messages": failed[:5]}


def _session(sec, config) -> tuple[dict, list, list, dict]:
    h, cols = sec["header"], sec["cols"] or []
    scale, src = _scale_for(h, config)
    ra, dec, ra_s, dec_s = [], [], [], []
    dropped, reasons = 0, {}
    for vals, settling in sec["rows"]:
        mount = (_col(cols, vals, "mount") or "").strip().strip('"')
        err = _num(_col(cols, vals, "ErrorCode")) or 0
        if mount.upper() == "DROP" or err:
            dropped += 1
            msg = (vals[-1] if len(vals) > len(cols) else "").strip().strip('"')
            key = msg or f"error code {int(err)}"
            reasons[key] = reasons.get(key, 0) + 1
            continue
        r, d = _num(_col(cols, vals, "RARawDistance")), _num(_col(cols, vals, "DECRawDistance"))
        if r is None or d is None:
            continue
        ra.append(r)
        dec.append(d)
        if not settling:
            ra_s.append(r)
            dec_s.append(d)
    star_lost = sum(n for k, n in reasons.items() if "star lost" in k.lower())

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
         "frames": len(ra), "dropped": dropped, "star_lost": star_lost,
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


def night_summary(config, date: str = "", file: str = "") -> dict:
    """GET /api/phd2/summary: the night's guide logs, summarized."""
    found = find_logs(config, "guide")
    paths, note = select(found["files"], date=date, file=file)
    if not paths:
        return {"ok": False, "note": note or "no PHD2 guide logs found",
                "searched": found["searched"], "date": date or None}
    sections = []
    for p in paths:
        sections += parse_guide_log(
            Path(p).read_text(encoding="utf-8", errors="replace"), p.name)
    out = summarize_sections(sections, config)
    out.update({"ok": True, "date": date or None, "dir": found["dir"],
                "files": [lf.describe(p) for p in paths]})
    return out
