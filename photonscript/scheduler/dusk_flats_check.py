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

Dawn flats (2026-10-24): the same count for the night sequences' own dawn
sets, so a morning that saved none says why. dawn_report(config, date):

  window      astro dawn - DAWN_LEAD_MIN to sunrise + DAWN_TAIL_MIN (the
              night's twilight times); a rig whose NINA log shows no sequence
              activity in it is left out, no rig = no report
  shot/filed  as for dusk, FLAT frames written inside the window
  why         from NINA's log: the safety state when the flat window opened
              (each rig's first planned set, else nautical dawn + 5) and
              when the roof closed, the sets NINA failed and why, and a stop
              (the armer's dawn shutdown cancels the run) before the window

dawn_morning_line: "Dawn flats 2026-10-23: RC16 0 shot, 0 filed (roof closed
at the flat window, closed 04:25); Piggy-600 0 shot, 0 filed (roof closed at
the flat window, closed 04:25; NINA stopped 06:35 before the 06:52 window)".
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


# ---- dawn flats (2026-10-24) -------------------------------------------------

DAWN_LEAD_MIN = 30      # the dawn window opens this long before astro dawn
DAWN_TAIL_MIN = 90      # and closes this long after sunrise
_STATE = re.compile(r"state changed to (Safe|Unsafe)|Status=(Safe|Unsafe)")
_SKYFLAT_START = "Determining Sky Flat Exposure Time"
_RUN_MARKS = ("|SequenceItem.cs|Run|", "|Sequencer.cs|")
_DISCONNECT_MARKS = ("DisconnectAllEquipment", "SafetyMonitorVM.cs|Disconnect")
DISCONNECT_GRACE_S = 10


def _just_after(t0: str | None, t1: str) -> bool:
    """t1 within DISCONNECT_GRACE_S after t0 (NINA local ISO stamps)."""
    if not t0:
        return False
    try:
        dt = (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds()
    except ValueError:
        return False
    return 0 <= dt <= DISCONNECT_GRACE_S


def dawn_times(config, date: str) -> dict:
    """{astro_dawn, naut_dawn, sunrise} (naive UTC) of the morning after the
    evening of `date`; {} when they cannot be computed."""
    try:
        from photonscript.scheduler.night_plan import compute_night_times
        tw = compute_night_times(config.get_observatory(),
                                 datetime.strptime(date, "%Y-%m-%d"))
    except Exception as e:  # noqa: BLE001
        logger.debug("dawn flats check: no twilight times for %s: %s", date, e)
        return {}
    return {k: tw.get(k) for k in ("astro_dawn", "naut_dawn", "sunrise")
            if tw.get(k) is not None}


def nina_dawn_scan(config, rig: str, date: str, lo_local: datetime,
                   hi_local: datetime, window_local: datetime) -> dict:
    """One forward pass over the rig's night logs (NINA logs local time):
    ran (any sequence line inside lo..hi), state (the safety monitor's last
    reading at window_local: "Safe" / "Unsafe" / None), closed (local time of
    the last Safe -> Unsafe change at or before the window, or the first one
    after it), tries (SkyFlat exposure searches), failed {filter: reason},
    stopped (the first "Sequence run was cancelled" inside lo..hi)."""
    out = {"ran": False, "state": None, "closed": None, "tries": 0,
           "failed": {}, "stopped": None}
    try:
        from photonscript.scheduler.routers.triage import _rig_night_logs
        paths, _note = _rig_night_logs(config, rig, date)
    except Exception as e:  # noqa: BLE001
        logger.debug("dawn flats check: no NINA logs for %s: %s", rig, e)
        return out
    lo, hi = lo_local.isoformat(timespec="seconds"), hi_local.isoformat(timespec="seconds")
    win = window_local.isoformat(timespec="seconds")
    state = live = None          # the reading at the window / the latest one
    closed_before = closed_after = disc = None
    for p in paths:
        reason, ts = "", ""
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
                    if not ts:
                        continue
                    if any(mk in line for mk in _DISCONNECT_MARKS):
                        disc = ts
                    st = _STATE.search(line)
                    if st:
                        new = st.group(1) or st.group(2)
                        if new == "Unsafe" and _just_after(disc, ts):
                            # NINA reads a disconnected monitor as Unsafe
                            # (the End area's DisconnectAllEquipment): not a
                            # roof close
                            live = None
                            continue
                        if new == "Unsafe" and live == "Safe":
                            if ts <= win:
                                closed_before = ts
                            elif closed_after is None:
                                closed_after = ts
                        live = new
                        if ts <= win:
                            state = new
                    if ts < lo:
                        continue
                    if any(mk in line for mk in _RUN_MARKS):
                        out["ran"] = True
                    if _SKYFLAT_START in line:
                        out["tries"] += 1
                    if "Sequence run was cancelled" in line and out["stopped"] is None:
                        out["stopped"] = ts
                    if "Failed to determine exposure time for sky flats" in line:
                        reason = ("no exposure, sky too dim" if "too dim" in line else
                                  "no exposure, sky too bright" if "too bright" in line
                                  else "no exposure found")
                        continue
                    f = _FAILED.search(line)
                    if f:
                        out["failed"][f.group(1)] = reason or "failed"
                        reason = ""
        except OSError as e:
            logger.debug("dawn flats check: %s unreadable: %s", p, e)
    out["state"] = state
    out["closed"] = closed_before if state == "Unsafe" else closed_after
    return out


def _hhmm(iso_local: str | None) -> str | None:
    return iso_local[11:16] if iso_local else None


def _dawn_rig(config, rig: str, date: str, t0: datetime, t1: datetime,
              window: datetime, off: float) -> dict | None:
    """One rig's dawn: frames shot / filed in t0..t1 (naive UTC), NINA's
    reading of the flat window (naive UTC `window`), None when the rig ran
    no sequence at dawn and wrote no flat."""
    from photonscript.scheduler.calibration_qa import QUARANTINE_DIR
    from photonscript.scheduler.runs import library_root
    from photonscript.shared.rigs import rig_config, rig_label
    rcfg = rig_config(config, rig)
    frames = dusk_frames(Path(rcfg.image_watch_dir), date, t0, t1)
    hop = timedelta(hours=off)
    scan = nina_dawn_scan(config, rig, date, t0 + hop, t1 + hop, window + hop)
    if not frames and not scan["ran"]:
        return None
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
    win_local = (window + hop).isoformat(timespec="seconds")
    stopped = scan["stopped"]
    return {"rig": rig, "name": rig_label(config, rig), "shot": len(frames),
            "by_filter": by_filter, "filed": filed, "quarantined": quarantined,
            "waiting": len(frames) - filed - quarantined,
            "window_local": win_local[11:16], "roof": scan["state"],
            "closed_local": _hhmm(scan["closed"]), "tries": scan["tries"],
            "failed": {k: v for k, v in scan["failed"].items()
                       if not by_filter.get(k)},
            "stopped_local": _hhmm(stopped),
            "stopped_before_window": bool(stopped and stopped < win_local)}


def dawn_report(config, date: str, times: dict | None = None) -> dict | None:
    """The night sequences' dawn flats on the morning after the evening of
    `date`, per rig that ran a sequence at dawn; None when neither did (or
    the twilight times are unknown). times: {astro_dawn, naut_dawn,
    sunrise} naive UTC (default: computed for the observatory)."""
    from photonscript.scheduler import flat_plan as fp
    from photonscript.shared.localtime import utc_offset_hours
    tw = times if times is not None else dawn_times(config, date)
    naut = tw.get("naut_dawn")
    if naut is None:
        return None
    t0 = (tw.get("astro_dawn") or naut - timedelta(minutes=30)) - timedelta(
        minutes=DAWN_LEAD_MIN)
    t1 = (tw.get("sunrise") or naut + timedelta(minutes=50)) + timedelta(
        minutes=DAWN_TAIL_MIN)
    off = utc_offset_hours(config, naut)
    # 2026-10-24: the flat window of each rig is its first planned set
    # (flat_plan, stored at dispatch). A night without a stored plan ran the
    # fixed windows of before (RC16 nautical dawn + 5, the Piggy-600 +
    # piggyback_flat_dawn_offset_min + 90 s).
    windows = {"rc16": naut + timedelta(minutes=5)}
    if getattr(config, "piggyback_enabled", False):
        windows["piggyback"] = naut + timedelta(minutes=int(getattr(
            config, "piggyback_flat_dawn_offset_min", 20) or 0), seconds=90)
    plan = fp.load_plan(config, date, "dawn")
    for rig, r in ((plan or {}).get("rigs") or {}).items():
        if r.get("slots") and rig in windows:
            windows[rig] = min(s["start"] for s in r["slots"])
    rigs = [r for r in (_dawn_rig(config, rig, date, t0, t1, w, off)
                        for rig, w in windows.items()) if r]
    if not rigs:
        return None
    return {"night": date, "naut_dawn": naut.isoformat() + "Z", "rigs": rigs}


def _dawn_text(r: dict) -> str:
    s = f"{r['name']} {r['shot']} shot, {r['filed']} filed"
    bits = []
    if r["quarantined"]:
        bits.append(f"{r['quarantined']} quarantined by QA")
    if r["waiting"]:
        bits.append(f"{r['waiting']} waiting for the dawn sweep")
    if r["roof"] == "Unsafe":
        bits.append("roof closed at the flat window"
                    + (f", closed {r['closed_local']}" if r["closed_local"] else ""))
    elif r["closed_local"] and not r["shot"]:
        bits.append(f"roof closed {r['closed_local']}")
    groups: dict[str, list[str]] = {}
    for f, why in r["failed"].items():
        groups.setdefault(why, []).append(f)
    bits += [f"{', '.join(fs)}: {why}" for why, fs in groups.items()]
    if r["stopped_before_window"] and not r["shot"]:
        bits.append(f"NINA stopped {r['stopped_local']} before the "
                    f"{r['window_local']} window")
    if not r["shot"] and not bits:
        bits.append("no sky flat in NINA's log" if not r["tries"]
                    else "no flat saved")
    return s + (f" ({'; '.join(bits)})" if bits else "")


def dawn_morning_line(config, date: str | None, times: dict | None = None) -> str | None:
    """"Dawn flats <date>: RC16 N shot, M filed (...); Piggy-600 ..." or None
    when no rig ran a sequence at that dawn."""
    if not date:
        return None
    rep = dawn_report(config, date, times)
    if not rep:
        return None
    return f"Dawn flats {date}: " + "; ".join(_dawn_text(r) for r in rep["rigs"])


# ---- plan windows: what each window actually saved (2026-10-24) ---------------

WINDOW_LEAD_MIN = 10    # frames this long before the plan's first set count
WINDOW_TAIL_MIN = 12    # and this long after its end


def _plan_span(p: dict) -> tuple[datetime, datetime] | None:
    t0 = p.get("dispatched_at") or p.get("first_start") or p.get("ready")
    t1 = p.get("end") or p.get("hard_end")
    if not isinstance(t0, datetime) or not isinstance(t1, datetime):
        return None
    first = p.get("first_start") if isinstance(p.get("first_start"), datetime) else t0
    return (min(t0, first) - timedelta(minutes=WINDOW_LEAD_MIN),
            t1 + timedelta(minutes=WINDOW_TAIL_MIN))


def _plan_rigs(p: dict) -> list[str]:
    disp = p.get("dispatched")
    rigs = []
    for rig, r in (p.get("rigs") or {}).items():
        if isinstance(disp, dict) and not disp.get(rig, False):
            continue
        if r.get("slots") or r.get("skipped"):
            rigs.append(rig)
    return rigs


def window_counts(config, night: str, phase: str) -> dict | None:
    """{rig: {filter: frames}} of the FLAT frames written inside the stored
    flat plan's window (flat_plan.load_plan), or None with no plan."""
    from photonscript.scheduler import flat_plan as fp
    from photonscript.shared.rigs import rig_config
    p = fp.load_plan(config, night, phase)
    if not p:
        return None
    span = _plan_span(p)
    if not span:
        return None
    out: dict = {}
    for rig in _plan_rigs(p):
        try:
            rcfg = rig_config(config, rig)
            frames = dusk_frames(Path(rcfg.image_watch_dir), night, *span)
        except Exception as e:  # noqa: BLE001
            logger.debug("window counts %s: %s", rig, e)
            continue
        c: dict = {}
        for _d, f in frames:
            flt = _filter_of(config, rig, f)
            c[flt] = c.get(flt, 0) + 1
        out[rig] = c
    return out


def window_report(config, night: str, phase: str, learn: dict | None = None) -> dict:
    """The truthful result of one flat window: per rig the frames saved per
    filter, and for each planned or owed filter without a set why (the
    plan's skip reason, NINA's failure, the roof). line: the push text
    "Dusk flats 2026-10-24: RC16 B 15, L 12; Piggy-600 OSC 25. Not saved:
    RC16 R (window passed ...). Sky model: ..." or "... none saved (...)"."""
    from photonscript.scheduler import flat_plan as fp
    from photonscript.shared.localtime import utc_offset_hours
    from photonscript.shared.rigs import rig_label
    p = fp.load_plan(config, night, phase) or {}
    counts = window_counts(config, night, phase) or {}
    span = _plan_span(p) if p else None
    need = int(getattr(config, "flats_min_saved", 10) or 1)
    title = f"{'Dusk' if phase == 'dusk' else 'Dawn'} flats {night}"
    saved_bits, miss_bits = [], []
    rigs = {}
    for rig in _plan_rigs(p):
        r = p["rigs"][rig]
        name = rig_label(config, rig)
        c = counts.get(rig) or {}
        planned = [s["filter"] for s in r.get("slots") or []]
        missing = [f for f in planned if c.get(f, 0) < need]
        why: dict = {}
        if missing and span:
            off = utc_offset_hours(config, span[0])
            hop = timedelta(hours=off)
            first = min((s["start"] for s in r["slots"]), default=span[0])
            scan = nina_dawn_scan(config, rig, night, span[0] + hop, span[1] + hop,
                                  first + hop)
            for f in missing:
                if f in scan["failed"]:
                    why[f] = scan["failed"][f]
                elif "OSC" in scan["failed"] and rig != "rc16":
                    why[f] = scan["failed"]["OSC"]
                elif scan["state"] == "Unsafe" and not c:
                    why[f] = "roof closed at the flat window" + (
                        f" (closed {_hhmm(scan['closed'])})" if scan["closed"] else "")
                elif c.get(f):
                    why[f] = f"only {c[f]} frames"
                elif not scan["ran"]:
                    why[f] = "NINA ran no sequence in the window"
                else:
                    why[f] = "no frame saved"
        for f, w in (r.get("skipped") or {}).items():
            why.setdefault(f, "not planned: " + w)
        got = {f: k for f, k in c.items() if k}
        rigs[rig] = {"name": name, "saved": got, "missing": why, "planned": planned}
        if got:
            saved_bits.append(f"{name} " + ", ".join(
                f"{f} {k}" for f, k in sorted(got.items(), key=lambda kv: _ord(kv[0]))))
        for f, w in why.items():
            miss_bits.append((name, f, w))
    groups: dict = {}
    for name, f, w in miss_bits:
        groups.setdefault((name, w), []).append(f)
    miss_txt = "; ".join(f"{name} {', '.join(fs)}: {w}" for (name, w), fs in groups.items())
    if saved_bits:
        line = f"{title}: " + "; ".join(saved_bits) + "."
        if miss_txt:
            line += f" Not saved: {miss_txt}."
    else:
        line = f"{title}: none saved" + (f" ({miss_txt})" if miss_txt else
                                        " (nothing was planned)") + "."
    ch = (learn or {}).get("changes") or []
    if ch:
        line += " Sky model: " + "; ".join(ch[:4]) + ("" if len(ch) <= 4 else " ...")
    return {"night": night, "phase": phase, "rigs": rigs, "line": line,
            "model_changes": ch}


def _ord(f: str) -> int:
    order = ("L", "R", "G", "B", "Ha", "OIII", "SII", "OSC")
    return order.index(f) if f in order else 99
