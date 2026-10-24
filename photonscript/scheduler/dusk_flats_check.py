"""Dusk flats: shot vs filed, per rig (2026-10-24, read-only).

2026-10-23: the auto dusk flats were dispatched on both rigs and NINA
pushed "dusk sky flats complete" and "piggyback dusk flats complete", yet
no flat reached the Library: NINA's SkyFlat found no exposure for any set
(RC16 Ha / OIII "sky is too dim" at the 30 s cap, R cut by the 19:19 stop,
G / B / L never reached; the Piggy-600's first probe was 43.7 % against a
45-55 % band and the dimming sky outran the search). A failed SkyFlat is
skipped (ContinueOnError) and saves nothing, so the closing push said
"complete" over zero frames and nothing counted them.

night_report(config, date) looks at what that evening actually left:

  dispatched  the night's "dusk_flats" decision row with value dispatched
              (runs/<date>_events.jsonl); no row, no report
  shot        FLAT frames under <watch>/<date>/ (and the next date folder)
              whose file time is inside the dusk run (dispatch to the stop
              + DUSK_TAIL_MIN), per rig; the filter from the FITS header
  filed       the same names in <library>/Calibration/FLAT/<folder date>/;
              quarantined: in Calibration/_quarantine/FLAT/<folder date>/;
              the rest wait for the dawn calibration sweep
  why         with sets missing, NINA's own log over the run: "Instruction
              Sky flats Ha failed" after "Failed to determine exposure time
              for sky flats - sky is too dim" (forward scan, stops at the
              run's end, at most NINA_SCAN_BYTES per log)

morning_line: "Dusk flats 2026-10-23: RC16 0 shot, 0 filed (Ha, OIII: no
exposure, sky too dim; R, G, B, L: not shot by the 19:19 stop); Piggy-600 0
shot, 0 filed (OSC: no exposure, sky too bright)". Nothing here writes.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DUSK_TAIL_MIN = 20                 # frames this long after the stop still count
NINA_SCAN_BYTES = 64 * 1024 * 1024
_FLAT_DIRS = {"FLAT", "FLATS"}
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_FAILED = re.compile(r"Instruction Sky flats (\S+) failed")
_RC16_WANTED = re.compile(r"RC16: (.+?), starts")


def _parse_t(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).rstrip("Z")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def dispatch_row(config, date: str) -> dict | None:
    """The night's last auto dusk flats dispatch (decision row), or None."""
    from photonscript.scheduler.auto_armer import ACTION_FLATS, EVENT_KIND
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import read_jsonl
    rows = [r for r in read_jsonl(events_path(config, date))
            if r.get("kind") == EVENT_KIND and r.get("action") == ACTION_FLATS
            and r.get("value") == "dispatched"]
    return rows[-1] if rows else None


def wanted_filters(detail: str) -> list[str]:
    """The RC16 filters a dispatch row's detail names ("RC16: R, G, B, L,
    starts ..."); [] for the pointing hold."""
    m = _RC16_WANTED.search(detail or "")
    if not m or "pointing hold" in m.group(1):
        return []
    return [f.strip() for f in m.group(1).split(",") if f.strip()]


def _filter_of(config, rig: str, path: Path) -> str:
    if rig != "rc16":
        return "OSC"
    raw = "?"
    try:
        from astropy.io import fits
        raw = str(fits.getheader(path).get("FILTER") or "?").strip() or "?"
    except Exception:  # noqa: BLE001 - unreadable: counted, filter unknown
        pass
    try:
        return config.reverse_filter_map().get(raw, raw)
    except Exception:  # noqa: BLE001
        return raw


def dusk_frames(watch: Path, date: str, t0: datetime, t1: datetime) -> list[tuple[str, Path]]:
    """(folder date, path) of the FLAT frames written between t0 and t1
    (naive UTC) under <watch>/<date> and the next date folder."""
    out = []
    base = datetime.strptime(date, "%Y-%m-%d")
    for d in (date, (base + timedelta(days=1)).strftime("%Y-%m-%d")):
        root = Path(watch) / d
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.fits")):
            if not any(p.upper() in _FLAT_DIRS for p in f.relative_to(root).parts[:-1]):
                continue
            try:
                mt = datetime.utcfromtimestamp(f.stat().st_mtime)
            except OSError:
                continue
            if t0 <= mt <= t1:
                out.append((d, f))
    return out


def nina_failures(config, rig: str, date: str, t0_local: datetime,
                  t1_local: datetime) -> dict[str, str]:
    """{filter: reason} for the SkyFlat sets NINA's log says failed between
    t0_local and t1_local (NINA logs local time)."""
    try:
        from photonscript.scheduler.routers.triage import _rig_night_logs
        paths, _note = _rig_night_logs(config, rig, date)
    except Exception as e:  # noqa: BLE001
        logger.debug("dusk flats check: no NINA logs for %s: %s", rig, e)
        return {}
    lo, hi = t0_local.isoformat(timespec="seconds"), t1_local.isoformat(timespec="seconds")
    out: dict[str, str] = {}
    for p in paths:
        reason = ""
        inside = False   # the last timestamped line was inside the window
        try:
            with open(p, "rb") as fh:
                read = 0
                for raw in fh:
                    read += len(raw)
                    if read > NINA_SCAN_BYTES:
                        break
                    line = raw.decode("utf-8", "replace")
                    m = _TS.match(line)
                    if m:
                        ts = m.group(1)
                        if ts > hi:
                            break
                        inside = ts >= lo
                    if not inside:
                        continue
                    if "Failed to determine exposure time for sky flats" in line:
                        reason = ("no exposure, sky too dim" if "too dim" in line else
                                  "no exposure, sky too bright" if "too bright" in line
                                  else "no exposure found")
                        continue
                    f = _FAILED.search(line)
                    if f:
                        out[f.group(1)] = reason or "failed"
                        reason = ""
        except OSError as e:
            logger.debug("dusk flats check: %s unreadable: %s", p, e)
    return out


def _rig_report(config, rig: str, date: str, wanted: list[str], t0: datetime,
                t1: datetime, offset_h: float) -> dict:
    from photonscript.scheduler.calibration_qa import QUARANTINE_DIR
    from photonscript.scheduler.runs import library_root
    from photonscript.shared.rigs import rig_config, rig_label
    rcfg = rig_config(config, rig)
    frames = dusk_frames(Path(rcfg.image_watch_dir), date, t0, t1)
    lib = library_root(rcfg) / "Calibration"
    by_filter: dict[str, int] = {}
    filed = quarantined = 0
    for d, f in frames:
        flt = _filter_of(config, rig, f)
        by_filter[flt] = by_filter.get(flt, 0) + 1
        if (lib / "FLAT" / d / f.name).exists():
            filed += 1
        elif (lib / QUARANTINE_DIR / "FLAT" / d / f.name).exists():
            quarantined += 1
    missing = [w for w in wanted if not by_filter.get(w)]
    failed = {}
    if missing:
        failed = nina_failures(config, rig, date, t0 + timedelta(hours=offset_h),
                               t1 + timedelta(hours=offset_h))
    return {"rig": rig, "name": rig_label(config, rig), "wanted": wanted,
            "shot": len(frames), "by_filter": by_filter, "filed": filed,
            "quarantined": quarantined,
            "waiting": len(frames) - filed - quarantined,
            "missing": missing, "failed": {k: v for k, v in failed.items()
                                           if k in missing}}


def night_report(config, date: str) -> dict | None:
    """Dusk flats of the evening of `date` per dispatched rig, or None when
    no dusk flats were dispatched that evening."""
    from photonscript.scheduler.calibration import dusk_flat_times
    from photonscript.shared.localtime import utc_offset_hours
    row = dispatch_row(config, date)
    if not row:
        return None
    t0 = _parse_t(row.get("t"))
    if t0 is None:
        return None
    detail = str(row.get("detail") or "")
    try:
        stop = (dusk_flat_times(config, t0) or {}).get("stop") or t0 + timedelta(hours=1)
    except Exception:  # noqa: BLE001
        stop = t0 + timedelta(hours=1)
    t1 = max(stop, t0) + timedelta(minutes=DUSK_TAIL_MIN)
    t0w = t0 - timedelta(minutes=2)
    off = utc_offset_hours(config, t0)
    stop_local = (stop + timedelta(hours=off)).strftime("%H:%M")
    rigs = [_rig_report(config, "rc16", date, wanted_filters(detail), t0w, t1, off)]
    if "Piggy-600 OSC: dispatched" in detail:
        rigs.append(_rig_report(config, "piggyback", date, ["OSC"], t0w, t1, off))
    return {"night": date, "dispatched": row.get("t"), "detail": detail,
            "stop_local": stop_local, "rigs": rigs}


def _rig_text(r: dict, stop_local: str) -> str:
    s = f"{r['name']} {r['shot']} shot, {r['filed']} filed"
    bits = []
    if r["quarantined"]:
        bits.append(f"{r['quarantined']} quarantined by QA")
    if r["waiting"]:
        bits.append(f"{r['waiting']} waiting for the dawn sweep")
    groups: dict[str, list[str]] = {}
    for f in r["missing"]:
        why = r["failed"].get(f) or f"not shot by the {stop_local} stop"
        groups.setdefault(why, []).append(f)
    bits += [f"{', '.join(fs)}: {why}" for why, fs in groups.items()]
    return s + (f" ({'; '.join(bits)})" if bits else "")


def morning_line(config, date: str | None) -> str | None:
    """"Dusk flats <date>: RC16 N shot, M filed (...); Piggy-600 ..." or None
    when no dusk flats were dispatched that evening."""
    if not date:
        return None
    rep = night_report(config, date)
    if not rep:
        return None
    return (f"Dusk flats {date}: "
            + "; ".join(_rig_text(r, rep["stop_local"]) for r in rep["rigs"]))
