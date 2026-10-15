"""Re-entry: skip the target's start AF, and the re-acquire, when nothing
changed (2026-10-15).

Why: 2026-10-14 (MDT) NINA #1 ran a full autofocus at 22:21, 22:29, 22:35
and 22:41, every one landing on 5719 (L, HFR 7.13 / 6.19 / 6.64), because
every entry into a target container (the SAFE_LOOP coming back after a
park, an armer restart, Resume, a sub-length change) runs slew, the start
AF (~3.3 min), center and StartGuiding again.

How (a runtime gate, the PS-61 cooler-gate pattern): the target container
holds small containers that each start with an ExternalScript
(deploy\\reentry-gate.cmd -> `photonscript reentry-gate` -> POST
/api/reentry/gate) with ErrorBehavior 1 (SkipInstructionSetOnError). When
the service answers SKIP the script exits 1 and NINA skips the rest of
that small container only; every other outcome (RUN, the service down, an
error, a timeout) exits 0 and the old items run. The decision is made when
NINA reaches the item, from NINA's own state, so it holds for an armer
re-dispatch and for the in-sequence SAFE_LOOP re-entry alike, and a
decision can never go stale between a dispatch and the target.

  "<target> start AF (re-entry gate)"   [gate af, MoveFocuserAbsolute(seed),
                                         RunAutofocus]
      SKIP only when the last AF on NINA #1 (event history: the newest
      AUTOFOCUS-STARTING .. AUTOFOCUS-FINISHED run that was not cut by an
      unsafe spell or a sequence stop; a later failed AF = RUN) finished
      under reentry_af_skip_min (45) ago, its NINA AF report (ninaAPI
      focuser/last-af, else the reports folder) is a good fit, the focuser
      temperature moved at most reentry_af_temp_c (1.0 C) since it (a
      frozen sensor, a known focus_frozen_temps value or unchanged to
      0.01 C for reentry_frozen_h (6) h, is ignored: time only), and the
      offset from the AF's filter to the requested filter is known (the
      same filter, a PS-76 measured offset or a configured one). Before it
      answers SKIP the service moves the focuser to that AF's best focus
      (+ the offset) and reads it back; a failed move answers RUN.
  "<target> slew (re-entry gate)"       [gate slew, SlewScopeToRaDec]
  "<target> center (re-entry gate)"     [gate center, Center]
      reentry_optimize "on" only. The slew is skipped when the mount is
      connected, tracking, not parked, not slewing and within
      reentry_mount_arcmin (10) of the target's NINA coordinates; the
      center too when the newest RC16 light (ended under
      reentry_center_max_age_min (60) ago, no park / unpark / flip /
      center / disconnect event since it started) plate-solves within
      reentry_center_arcmin (2) of them. Pointing error unknown (no such
      light) = the cheap re-center: the slew is skipped, the center runs.
      The center gate repeats the slew gate's decision for the same label
      (made at most ACQUIRE_MEMORY_MIN ago), else RUN.

The safety net stays in NINA: every light SmartExposure keeps its HFR
increase and temperature AF triggers (smart policy: the timed AF too),
and NINA measures them from its own last AF, the one the gate checked.

reentry_optimize: off (no gates, the old sequence) | advise (every gate,
always RUN, decisions logged) | af (default: the AF gate only) | on (AF +
slew + center). Every decision is a run event kind "reentry" and a
morning report line.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("off", "advise", "af", "on")
KINDS = ("af", "slew", "center")
EXIT_SKIP = 3                 # CLI exit for SKIP; the .cmd turns it into 1
GATE_TOKEN = "reentry-gate"   # file-name token the lint rule looks for
GATE_ERROR_BEHAVIOR = 1       # SkipInstructionSetOnError (see nsj)
AF_SUFFIX = " start AF (re-entry gate)"
SLEW_SUFFIX = " slew (re-entry gate)"
CENTER_SUFFIX = " center (re-entry gate)"
ACQUIRE_MEMORY_MIN = 20.0     # center gate follows the slew gate this long
MOVE_TOL_STEPS = 3            # focuser read-back tolerance after the move
MOVE_WAIT_S = 90.0
MOVE_POLL_S = 2.0
# what a skip saves (10-14: AF 3.2 to 3.3 min, center + settle ~1.5 min)
SAVED_S = {"af": 200.0, "slew": 20.0, "center": 90.0}
MOTION_EVENTS = ("MOUNT-PARKED", "MOUNT-UNPARKED", "MOUNT-HOMED",
                 "MOUNT-BEFORE-FLIP", "MOUNT-AFTER-FLIP", "MOUNT-CENTER",
                 "MOUNT-DISCONNECTED")
KIND = "reentry"

# label -> {"t": naive UTC, "skip_center": bool, "why": str}
_ACQUIRE: dict[str, dict] = {}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def mode(cfg) -> str:
    v = str(getattr(cfg, "reentry_optimize", "af") or "af").strip().lower()
    return v if v in MODES else "af"


def _f(cfg, key: str, default: float) -> float:
    try:
        v = float(getattr(cfg, key, default))
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def limits(cfg) -> dict:
    return {"af_min": _f(cfg, "reentry_af_skip_min", 45.0),
            "temp_c": _f(cfg, "reentry_af_temp_c", 1.0),
            "frozen_h": _f(cfg, "reentry_frozen_h", 6.0),
            "center_arcmin": _f(cfg, "reentry_center_arcmin", 2.0),
            "center_age_min": _f(cfg, "reentry_center_max_age_min", 60.0),
            "mount_arcmin": _f(cfg, "reentry_mount_arcmin", 10.0)}


def gate_kinds(cfg) -> set:
    """Which gates the generated RC16 night carries."""
    m = mode(cfg)
    if m == "off":
        return set()
    if m == "af":
        return {"af"}
    return set(KINDS)          # advise (logs only) and on


def gate_script(cfg) -> str | None:
    """The script path when the sequence should carry gates: mode not off
    and the file exists on this machine (the armer generates on the scope
    PC). A missing script means the old sequence, never a skip."""
    if mode(cfg) == "off":
        return None
    path = str(getattr(cfg, "reentry_gate_script", "") or "").strip()
    if not path:
        return None
    try:
        return path if Path(path).is_file() else None
    except OSError:
        return None


def _safe(text) -> str:
    return "".join(ch for ch in str(text or "") if ch not in '"%^&|<>'
                   ).strip()[:60]


def script_args(kind: str, label: str, filter_name: str | None = None,
                ra_hours: float | None = None,
                dec_deg: float | None = None) -> str:
    """Arguments after the script path (see deploy/reentry-gate.cmd)."""
    out = f'{kind} --label="{_safe(label)}"'
    if filter_name:
        out += f" --filter={_safe(filter_name)}"
    if ra_hours is not None and dec_deg is not None:
        out += f" --ra={float(ra_hours):.5f} --dec={float(dec_deg):+.5f}"
    return out


def is_gate(item: dict) -> bool:
    return (isinstance(item, dict)
            and "ExternalScript" in str(item.get("$type", ""))
            and GATE_TOKEN in str(item.get("Script", "")))


# --------------------------------------------------------------------------
# pure decisions
# --------------------------------------------------------------------------

def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _event_time(cfg, s):
    from photonscript.scheduler.stuck_loop import event_time
    return event_time(cfg, s)


def last_af(events, cfg=None, now: datetime | None = None) -> dict:
    """The newest usable AF on this NINA from its event history.
    {"run": the newest finished run or None, "bad_after": a later AF that
    failed (not cut by unsafe / a sequence stop) or None, "runs": n}.
    A run still open (AF running now) is ignored."""
    from photonscript.scheduler.af_stall import af_runs, judge
    rows = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        t = _event_time(cfg, e.get("Time"))
        if t is None:
            continue
        rows.append({**e, "Time": t.isoformat()})   # naive UTC for af_runs
    runs = af_runs(rows)
    ok_run, bad_after = None, None
    for r in runs:
        v = judge(r, 3, now)
        if v == "ok" and r.get("finished") is True:
            ok_run, bad_after = r, None
        elif v == "bad" and ok_run is not None:
            bad_after = r
    return {"run": ok_run, "bad_after": bad_after, "runs": len(runs)}


def best_point(run: dict, events, cfg=None) -> int | None:
    """The lowest-HFR AUTOFOCUS-POINT-ADDED position inside a run (the
    event-history stand-in when no report is matched; logged only)."""
    if not run:
        return None
    best = None
    for e in events or []:
        if str(e.get("Event") or "").upper() != "AUTOFOCUS-POINT-ADDED":
            continue
        t = _event_time(cfg, e.get("Time"))
        if t is None or t < run["start"] or (run.get("end") and t > run["end"]):
            continue
        h, p = _num(e.get("HFR")), _num(e.get("Position"))
        if h and p is not None and (best is None or h < best[0]):
            best = (h, int(p))
    return best[1] if best else None


def match_report(run: dict, reports, cfg=None) -> dict | None:
    """The NINA AF report written by this run: its Timestamp between the
    run start - 1 min and its end + 3 min (NINA writes it as the AF ends)."""
    if not run:
        return None
    lo = run["start"] - timedelta(minutes=1)
    hi = (run.get("end") or run["start"]) + timedelta(minutes=3)
    hit = None
    for r in reports or []:
        if not isinstance(r, dict):
            continue
        t = _event_time(cfg, r.get("Timestamp") or r.get("Time"))
        if t is not None and lo <= t <= hi and (hit is None or t > hit[0]):
            hit = (t, r)
    return hit[1] if hit else None


def frozen_sensor(cfg, temp_now, unchanged_min=None, af_points=None,
                  now: datetime | None = None) -> tuple[bool, str]:
    """(frozen, why). Frozen = the reading is a known frozen value
    (focus_frozen_temps), or it has not changed by 0.01 C for
    reentry_frozen_h: by the focus table's temperature track
    (unchanged_min) or by this rig's AF reports (every AF of the last
    reentry_frozen_h + read the same temperature as now, the oldest of them
    at least that long ago)."""
    t = _num(temp_now)
    if t is None:
        return False, "no focuser temperature"
    for tok in str(getattr(cfg, "focus_frozen_temps", "") or "").split(","):
        v = _num(tok.strip())
        if v is not None and abs(t - v) < 0.006:
            return True, f"{t:.2f} C is a known frozen reading (focus_frozen_temps)"
    hours = limits(cfg)["frozen_h"]
    if unchanged_min is not None and unchanged_min >= hours * 60:
        return True, (f"unchanged at {t:.2f} C for {unchanged_min / 60:.1f} h "
                      "(temperature track)")
    if af_points and now is not None:
        pts = []
        for p in af_points:
            pt, pv = _event_time(cfg, p.get("time")), _num(p.get("temp"))
            if pt is not None and pv is not None and pt <= now:
                pts.append((pt, pv))
        pts.sort()
        recent = [x for x in pts if (now - x[0]).total_seconds() / 3600 <= 24]
        if recent and (now - recent[0][0]).total_seconds() / 3600 >= hours \
                and all(abs(v - t) <= 0.0101 for _pt, v in recent):
            return True, (f"every AF since {recent[0][0]:%m-%d %H:%MZ} read "
                          f"{t:.2f} C")
    return False, "focuser temperature live"


def offset_between(cfg, from_filter: str, to_filter: str,
                   table: dict | None = None) -> tuple[int | None, str]:
    """EAF steps from from_filter's best focus to to_filter's, or (None,
    why) when unknown: both relative to the AF filter (af_policy
    offset_table: PS-76 measured, else focus_filter_offsets)."""
    if from_filter == to_filter:
        return 0, "same filter"
    try:
        if table is None:
            from photonscript.scheduler.af_policy import offset_table
            ref = str(getattr(cfg, "autofocus_filter", "") or "L") or "L"
            table = offset_table(cfg, ref)
    except Exception as e:  # noqa: BLE001
        return None, f"offset table unavailable ({e})"
    a, b = (table or {}).get(from_filter), (table or {}).get(to_filter)
    for f, row in ((from_filter, a), (to_filter, b)):
        if not row or row.get("source") == "none":
            return None, f"no known focus offset for {f}"
    return int(b["steps"]) - int(a["steps"]), (
        f"offset {from_filter}->{to_filter} {int(b['steps']) - int(a['steps']):+d}")


def af_decision(cfg, *, now: datetime, events, reports, temp_now,
                filter_name: str, frozen: tuple[bool, str] = (False, ""),
                offsets: dict | None = None) -> dict:
    """Skip the start AF? {"skip", "why", "reasons" (each failed rule),
    "af_t", "age_min", "af_filter", "af_temp", "temp_now", "dt",
    "frozen", "position" (the AF's best focus), "target" (+ offset)}."""
    from photonscript.scheduler.focus_model import norm_filter
    from photonscript.scheduler.focus_reports import evaluate
    lim = limits(cfg)
    out = {"skip": False, "why": "", "reasons": [], "af_t": None,
           "age_min": None, "af_filter": None, "af_temp": None,
           "temp_now": _num(temp_now), "dt": None, "frozen": frozen[0],
           "frozen_why": frozen[1], "position": None, "target": None,
           "event_best": None, "filter": filter_name}
    la = last_af(events, cfg, now)
    run = la["run"]
    if run is None:
        out["reasons"].append("no finished AF in NINA #1's event history "
                              "(NINA restarted, or none yet)")
        out["why"] = out["reasons"][0]
        return out
    out["af_t"] = run["end"]
    out["event_best"] = best_point(run, events, cfg)
    age = (now - run["end"]).total_seconds() / 60.0
    out["age_min"] = round(age, 1)
    if la["bad_after"] is not None:
        out["reasons"].append(
            f"a later AF ({la['bad_after']['start']:%H:%MZ}) failed")
    if age > lim["af_min"]:
        out["reasons"].append(f"last AF {age:.0f} min ago (> "
                              f"{lim['af_min']:g} min)")
    rep = match_report(run, reports, cfg)
    if rep is None:
        out["reasons"].append("no NINA AF report for it (filter, best focus "
                              "and temperature unknown)")
    else:
        g = evaluate(rep, float(getattr(cfg, "af_min_r2", 0.7) or 0.7))
        cfp = rep.get("CalculatedFocusPoint") or {}
        pos = _num(cfp.get("Position")) if isinstance(cfp, dict) else None
        out["af_filter"] = norm_filter(rep.get("Filter")
                                       or rep.get("AutoFocusFilter"), cfg)
        out["af_temp"] = _num(rep.get("Temperature"))
        if not g["ok"] or pos is None or pos <= 0:
            out["reasons"].append(f"its fit is not good ({g['reason']})")
        else:
            out["position"] = int(round(pos))
        off, why = offset_between(cfg, out["af_filter"] or "?", filter_name,
                                  offsets)
        if off is None:
            out["reasons"].append(why)
        elif out["position"] is not None:
            out["target"] = out["position"] + off
            out["offset"] = off
        t0, t1 = out["af_temp"], out["temp_now"]
        if frozen[0]:
            out["temp_rule"] = f"time only: {frozen[1]}"
        elif t0 is None or t1 is None:
            out["reasons"].append("temperature unknown (AF report or focuser)")
        else:
            out["dt"] = round(t1 - t0, 2)
            if abs(t1 - t0) > lim["temp_c"]:
                out["reasons"].append(
                    f"focuser {t1:.2f} C vs {t0:.2f} C at the AF "
                    f"({t1 - t0:+.2f} > {lim['temp_c']:g} C)")
    if out["reasons"]:
        out["why"] = "; ".join(out["reasons"])
        return out
    out["skip"] = True
    tdesc = (f"dT {out['dt']:+.2f} C" if out["dt"] is not None
             else "temperature ignored (frozen sensor)")
    out["why"] = (f"last AF {run['end']:%H:%MZ} ({age:.0f} min ago, "
                  f"{out['af_filter']} at {out['position']}), {tdesc}; "
                  f"focuser to {out['target']} for {filter_name}")
    return out


def mount_state(mount: dict | None) -> tuple[bool, str]:
    """(usable, why): connected, tracking, not parked, not slewing."""
    m = mount if isinstance(mount, dict) else {}
    if not m:
        return False, "mount unreadable"
    if m.get("Connected") is False:
        return False, "mount not connected"
    if m.get("AtPark") is True or m.get("AtHome") is True:
        return False, "mount parked"
    if m.get("Slewing") is True:
        return False, "mount slewing"
    trk = m.get("TrackingEnabled", m.get("Tracking"))
    if trk is not True:
        return False, "mount not tracking"
    return True, "tracking"


def _mount_sep(cfg, mount: dict, ra_deg: float, dec_deg: float,
               now: datetime) -> float | None:
    from photonscript.scheduler.off_target import (
        mount_epoch, precess_from_j2000, separation)
    ra_h, dec = _num(mount.get("RightAscension")), _num(mount.get("Declination"))
    if ra_h is None or dec is None:
        return None
    era, edec = ra_deg, dec_deg
    if mount_epoch(cfg, mount.get("EquatorialSystem")) == "JNow":
        era, edec = precess_from_j2000(era, edec, now)
    s = separation(era, edec, ra_h * 15.0, dec)
    return s["arcmin"] if s else None


def last_motion(events, cfg=None) -> datetime | None:
    """The newest mount motion / re-pointing event (MOTION_EVENTS)."""
    best = None
    for e in events or []:
        if str(e.get("Event") or "").upper() in MOTION_EVENTS:
            t = _event_time(cfg, e.get("Time"))
            if t is not None and (best is None or t > best):
                best = t
    return best


def acquire_decision(cfg, *, now: datetime, events, mount: dict | None,
                     ra_hours: float, dec_deg: float,
                     solve: dict | None) -> dict:
    """Skip the slew (and the center)? solve: {"ra_deg", "dec_deg",
    "start" (naive UTC), "end", "file"} of the newest RC16 light, or None.
    {"skip_slew", "skip_center", "why", "mount_arcmin", "solve_arcmin",
    "solve_age_min"}."""
    from photonscript.scheduler.off_target import separation
    lim = limits(cfg)
    out = {"skip_slew": False, "skip_center": False, "why": "",
           "mount_arcmin": None, "solve_arcmin": None, "solve_age_min": None}
    ok, why = mount_state(mount)
    if not ok:
        out["why"] = why
        return out
    ra_deg = float(ra_hours) * 15.0
    sep = _mount_sep(cfg, mount, ra_deg, float(dec_deg), now)
    out["mount_arcmin"] = sep
    if sep is None or sep > lim["mount_arcmin"]:
        out["why"] = ("mount position unknown" if sep is None else
                      f"mount {sep:.1f}' from the target (> "
                      f"{lim['mount_arcmin']:g}')")
        return out
    out["skip_slew"] = True
    moved = last_motion(events, cfg)
    if solve is None:
        out["why"] = (f"mount tracking {sep:.1f}' from the target, no recent "
                      "solved light: re-center only")
        return out
    age = (now - (solve.get("end") or solve["start"])).total_seconds() / 60.0
    out["solve_age_min"] = round(age, 1)
    s = separation(ra_deg, float(dec_deg), solve["ra_deg"], solve["dec_deg"])
    out["solve_arcmin"] = s["arcmin"] if s else None
    if age > lim["center_age_min"]:
        out["why"] = (f"newest solved light {age:.0f} min old (> "
                      f"{lim['center_age_min']:g}): re-center")
    elif moved is not None and moved >= solve["start"]:
        out["why"] = (f"the mount moved ({moved:%H:%MZ}) after the newest "
                      "solved light: re-center")
    elif s is None or s["arcmin"] > lim["center_arcmin"]:
        out["why"] = (f"newest light solved {s['arcmin'] if s else '?'}' "
                      f"from the target (> {lim['center_arcmin']:g}'): "
                      "re-center")
    else:
        out["skip_center"] = True
        out["why"] = (f"mount tracking on target; light "
                      f"{solve.get('file') or ''} ({age:.0f} min ago) solved "
                      f"{s['arcmin']:.1f}' from it")
    return out


# --------------------------------------------------------------------------
# live gate (service side)
# --------------------------------------------------------------------------

class LiveIO:
    """ninaAPI GETs / the focuser move on NINA #1, the AF reports, the
    newest light's solve. Every read fails soft (None)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = str(getattr(cfg, "nina_base_url", "") or "").rstrip("/")

    async def get(self, path: str):
        if not self.base:
            return None
        from photonscript.scheduler.night_pause import nina_get
        return await nina_get(self.base, path, timeout=15.0)

    async def move(self, position: int):
        return await self.get(f"/equipment/focuser/move?position={int(position)}")

    async def reports(self) -> list[dict]:
        out = []
        rep = await self.get("/equipment/focuser/last-af")
        if isinstance(rep, dict) and rep.get("Timestamp"):
            out.append(rep)
        out += await asyncio.to_thread(newest_reports, self.cfg)
        return out

    async def solve(self, night: str, ra_deg: float, dec_deg: float,
                    now: datetime):
        return await asyncio.to_thread(newest_light_solve, self.cfg, night,
                                       (ra_deg, dec_deg), now)

    async def sleep(self, s: float):
        await asyncio.sleep(s)


def newest_reports(cfg, n: int = 6) -> list[dict]:
    """The n newest RC16 NINA AF reports in nina_autofocus_reports_dir (by
    file time; the folder is shared with NINA #2)."""
    import json as _json
    d = str(getattr(cfg, "nina_autofocus_reports_dir", "") or "").strip()
    if not d:
        return []
    try:
        files = sorted(Path(d).glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[: n * 2]
    except OSError:
        return []
    out = []
    try:
        from photonscript.scheduler.focus_model import classify_report
    except Exception:  # noqa: BLE001
        classify_report = None
    for p in files:
        try:
            rep = _json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(rep, dict):
            continue
        if classify_report is not None and classify_report(rep, cfg) == "piggyback":
            continue
        out.append(rep)
        if len(out) >= n:
            break
    return out


def newest_light_solve(cfg, night: str, hint, now: datetime) -> dict | None:
    """The newest RC16 LIGHT of tonight, plate-solved (the stored solve,
    else ASTAP now): {"ra_deg", "dec_deg", "start", "end", "file"}, or None
    (no light in the age limit, or no solution)."""
    from photonscript.scheduler.off_target import _parse_t
    lim = limits(cfg)
    try:
        from photonscript.scheduler.runs import _load_subs
        subs = _load_subs(cfg, night)
    except Exception:  # noqa: BLE001
        return None
    best = None
    for s in subs:
        if (s.get("rig") or "rc16") != "rc16":
            continue
        f = str(s.get("file") or "").replace("\\", "/")
        if not f.upper().startswith("LIGHT/"):
            continue
        end = _parse_t(s.get("end_utc")) or _parse_t(s.get("time"))
        if end is None:
            continue
        if best is None or end > best[0]:
            best = (end, s)
    if best is None:
        return None
    end, s = best
    if (now - end).total_seconds() / 60.0 > lim["center_age_min"]:
        return None
    start = _parse_t(s.get("start_utc")) or end
    rec = None
    try:
        from photonscript.scheduler import solve_store
        rec = solve_store.lookup(cfg, night, "rc16").get(s.get("file"))
        if not (rec and rec.get("solved") and rec.get("ra") is not None):
            rec = None
            if s.get("abs_path"):
                res = solve_store.solve(cfg, s["abs_path"], rig="rc16",
                                        night=night, hint=hint,
                                        rel_file=s.get("file"),
                                        start_utc=s.get("start_utc"),
                                        radius_deg=5.0, timeout_s=60.0)
                if res and res.get("ra") is not None:
                    rec = res
    except Exception as e:  # noqa: BLE001
        logger.debug("reentry: no solve for %s: %s", s.get("file"), e)
        rec = None
    if not rec:
        return None
    return {"ra_deg": float(rec["ra"]), "dec_deg": float(rec["dec"]),
            "start": start, "end": end, "file": s.get("file")}


def _temperature(cfg, info: dict, wx: dict | None, now: datetime) -> dict:
    """The focuser temperature and how long it has not changed (the focus
    table's track, updated here)."""
    try:
        from photonscript.scheduler.focus_table import choose_temperature
        ch = choose_temperature(cfg, info.get("Temperature"),
                                (wx or {}).get("Temperature"), now=now)
        return {"temp": _num(info.get("Temperature")),
                "unchanged_min": ch.get("focuser_unchanged_min")}
    except Exception as e:  # noqa: BLE001
        logger.debug("reentry: temperature track unavailable: %s", e)
        return {"temp": _num(info.get("Temperature")), "unchanged_min": None}


def _af_points(cfg) -> list[dict]:
    try:
        from photonscript.scheduler.focus_model import load_points
        return load_points(cfg, "rc16")
    except Exception:  # noqa: BLE001
        return []


async def _move_and_verify(io, target: int, clock_sleep) -> tuple[bool, str]:
    r = await io.move(target)
    if r is None:
        return False, "the focuser move was refused"
    waited = 0.0
    last = None
    while waited <= MOVE_WAIT_S:
        info = await io.get("/equipment/focuser/info")
        info = info if isinstance(info, dict) else {}
        last = _num(info.get("Position"))
        if info.get("IsMoving") is not True and last is not None \
                and abs(last - target) <= MOVE_TOL_STEPS:
            return True, f"focuser at {int(last)}"
        await clock_sleep(MOVE_POLL_S)
        waited += MOVE_POLL_S
    return False, (f"focuser read {last if last is not None else '?'} after "
                   f"the move to {target}")


async def run_gate(cfg, kind: str, label: str = "", filter_name: str = "",
                   ra_hours: float | None = None, dec_deg: float | None = None,
                   io=None, now: datetime | None = None) -> dict:
    """One gate call from NINA. {"verdict": SKIP | RUN, "kind", "label",
    "why", ...}. SKIP only in modes af / on (the AF) and on (slew /
    center); advise logs what it would do and answers RUN. Never raises."""
    now = now or datetime.utcnow()
    io = io or LiveIO(cfg)
    m = mode(cfg)
    out = {"verdict": "RUN", "kind": kind, "label": label, "mode": m,
           "why": ""}
    try:
        if kind not in KINDS:
            out["why"] = f"unknown gate kind {kind!r}"
        elif m == "off":
            out["why"] = "reentry_optimize off"
        elif kind == "af":
            await _af_gate(cfg, out, io, now, filter_name or str(
                getattr(cfg, "autofocus_filter", "") or "L"))
        elif kind == "slew":
            await _slew_gate(cfg, out, io, now, label, ra_hours, dec_deg)
        else:
            mem = _ACQUIRE.get(label)
            fresh = (mem is not None and (now - mem["t"]).total_seconds()
                     / 60.0 <= ACQUIRE_MEMORY_MIN)
            want = bool(fresh and mem.get("skip_center"))
            out["would_skip"] = want
            out["why"] = (mem.get("why") if fresh else
                          "no slew-gate decision for this target")
            if want and m == "on":
                out["verdict"] = "SKIP"
            _ACQUIRE.pop(label, None)
    except Exception as e:  # noqa: BLE001
        logger.warning("reentry gate %s failed: %s", kind, e)
        out.update(verdict="RUN", why=f"error: {e}")
    if m == "advise" and out.get("would_skip"):
        out["why"] = "advise (would skip): " + out["why"]
    record(cfg, out, now)
    return out


async def _af_gate(cfg, out: dict, io, now: datetime, filter_name: str):
    hist = await io.get("/event-history")
    info = await io.get("/equipment/focuser/info")
    info = info if isinstance(info, dict) else {}
    wx = await io.get("/equipment/weather/info")
    reports = await io.reports()
    tmp = await asyncio.to_thread(_temperature, cfg, info,
                                  wx if isinstance(wx, dict) else None, now)
    fr = frozen_sensor(cfg, tmp["temp"], tmp["unchanged_min"],
                       await asyncio.to_thread(_af_points, cfg), now)
    dec = af_decision(cfg, now=now, events=hist if isinstance(hist, list) else [],
                      reports=reports, temp_now=tmp["temp"],
                      filter_name=filter_name, frozen=fr)
    out.update({k: v for k, v in dec.items() if k not in ("af_t",)},
               af_t=(dec["af_t"].isoformat() + "Z" if dec["af_t"] else None),
               position_before=_num(info.get("Position")))
    out["would_skip"] = dec["skip"]
    if not dec["skip"] or mode(cfg) not in ("af", "on"):
        return
    ok, why = await _move_and_verify(io, int(dec["target"]), io.sleep)
    out["move"] = why
    if ok:
        out["verdict"] = "SKIP"
    else:
        out["why"] = f"{dec['why']}; but {why}: AF runs"
        out["would_skip"] = False


async def _slew_gate(cfg, out: dict, io, now: datetime, label: str,
                     ra_hours, dec_deg):
    if ra_hours is None or dec_deg is None:
        out["why"] = "no target coordinates"
        _ACQUIRE.pop(label, None)
        return
    mount = await io.get("/equipment/mount/info")
    hist = await io.get("/event-history")
    from photonscript.shared.phd2_store import night_of
    solve = None
    ok, _why = mount_state(mount if isinstance(mount, dict) else None)
    if ok:
        solve = await io.solve(night_of(cfg, now), float(ra_hours) * 15.0,
                               float(dec_deg), now)
    dec = acquire_decision(cfg, now=now, events=hist if isinstance(hist, list)
                           else [], mount=mount if isinstance(mount, dict)
                           else None, ra_hours=float(ra_hours),
                           dec_deg=float(dec_deg), solve=solve)
    out.update(dec)
    out["would_skip"] = dec["skip_slew"]
    _ACQUIRE[label] = {"t": now, "skip_center": dec["skip_center"],
                       "why": dec["why"]}
    if dec["skip_slew"] and mode(cfg) == "on":
        out["verdict"] = "SKIP"


# --------------------------------------------------------------------------
# log + morning report
# --------------------------------------------------------------------------

def record(cfg, out: dict, now: datetime) -> None:
    """Run event kind "reentry" (value skip_<kind> | run_<kind> |
    advise_<kind>). Never raises."""
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        kind = out.get("kind")
        if out.get("verdict") == "SKIP":
            value = f"skip_{kind}"
        elif out.get("would_skip") and out.get("mode") == "advise":
            value = f"advise_{kind}"
        else:
            value = f"run_{kind}"
        rec = {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
               "kind": KIND, "value": value, "label": out.get("label"),
               "detail": out.get("why"),
               "saved_s": SAVED_S.get(kind, 0.0) if value.startswith("skip_")
               else 0.0}
        for k in ("age_min", "dt", "frozen", "af_filter", "position",
                  "target", "mount_arcmin", "solve_arcmin", "solve_age_min",
                  "skip_center"):
            if out.get(k) is not None:
                rec[k] = out[k]
        append_jsonl(events_path(cfg, night_of(cfg, now)), rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("reentry event not logged: %s", e)
    (logger.info if out.get("verdict") == "RUN" else logger.warning)(
        "reentry gate %s %s: %s (%s)", out.get("kind"), out.get("label"),
        out.get("verdict"), out.get("why"))


def morning_lines(cfg, date: str) -> list[str]:
    """One line for the night's re-entry gate decisions, or []."""
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import read_jsonl
    rows = [r for r in read_jsonl(events_path(cfg, date))
            if r.get("kind") == KIND]
    if not rows:
        return []
    from photonscript.scheduler.stuck_loop import _hm, _parse
    skips = [r for r in rows if str(r.get("value", "")).startswith("skip_")]
    adv = [r for r in rows if str(r.get("value", "")).startswith("advise_")]
    runs = [r for r in rows if str(r.get("value", "")).startswith("run_")
            and r.get("value") != "run_center"]
    saved = sum(float(r.get("saved_s") or 0) for r in skips) / 60.0
    bits = []
    for r in skips[:6]:
        what = {"skip_af": "AF", "skip_slew": "slew",
                "skip_center": "center"}.get(r["value"], r["value"])
        bits.append(f"{_hm(cfg, _parse(r.get('t')))} {r.get('label') or ''} "
                    f"{what}".strip())
    line = (f"Re-entry: {len(skips)} skip(s), ~{saved:.0f} min saved"
            + (f" ({'; '.join(bits)})" if bits else "")
            + f"; {len(runs)} ran")
    if adv:
        line += f"; advise: {len(adv)} would have skipped"
    return [line]
