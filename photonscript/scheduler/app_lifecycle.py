"""PS-170: daily app lifecycle for NINA #1, NINA #2 and PHD2.

PHD2 ran from 2026-10-05 to 10-08 on one mount connection; after TheSky was
reworked its in-process SoftwareBisque driver went stale and refused every
guide pulse for three nights (PS-167). A fresh start each day prevents stale
driver instances, so the apps are closed after the dawn shutdown and launched
again before the noon re-arm.

Who does what:
  - deploy\\observatory-apps.ps1 (-Stop / -Start / -Status) does the closing
    and launching. It runs from two scheduled tasks in jeremy's interactive
    session (deploy\\install-app-lifecycle-tasks.ps1): GUI apps started from
    a session-0 process would be invisible and unusable.
  - This module (and routers/apps.py) only REPORTS and DECIDES: which apps
    answer, whether a stop is safe now (armer idle, no NINA sequence
    running, the dawn shutdown and its cooler verify done, sun up), the
    launch settings, and the script's posted results. It never starts,
    closes or kills anything.
  - The armer pages once at arm and once at pre-config - 60 min when an app
    the night needs does not answer (app_lifecycle_alert); preflight has an
    "Observatory apps running" check.

The scheduled runs act only when app_lifecycle_enabled is set (default
False); a hand run of the script may always act, behind the same guards.
"""
from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

REPORT_FILE = "app_lifecycle.jsonl"
MODES = ("stop", "start", "status")
SUN_STOP_MAX_DEG = -6.0     # never close the apps while the sun is below this
SHUTDOWN_RECENT_H = 20.0    # a dawn-shutdown record this young is "last night"
START_GRACE_MIN = 15.0      # preflight fails only this long after the launch
NINA_EXE = "nina.exe"
PHD2_EXE = "phd2.exe"
THESKY_EXE = "thesky64.exe"

LABELS = {"nina1": "NINA #1 (RC16)", "nina2": "NINA #2 (Piggy-600)",
          "phd2": "PHD2", "thesky": "TheSky64"}


# ------------------------------------------------------------------ settings

def enabled(cfg) -> bool:
    return bool(getattr(cfg, "app_lifecycle_enabled", False))


def parse_hhmm(text, default=(11, 45)) -> tuple[int, int]:
    """'11:45' -> (11, 45); anything unreadable -> default."""
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return default


def _host_port(url: str, default_port: int) -> tuple[str, int]:
    p = urlparse(str(url or ""))
    return (p.hostname or "localhost"), int(p.port or default_port)


def endpoints(cfg) -> dict:
    """{key: (host, port)} for every app this config uses. NINA #2 only
    when the piggyback rig is enabled."""
    from photonscript.shared.rigs import PIGGYBACK, rig_ids
    out = {"nina1": _host_port(getattr(cfg, "nina_base_url", ""), 1888)}
    if PIGGYBACK in rig_ids(cfg):
        out["nina2"] = _host_port(getattr(cfg, "piggyback_nina_base_url", ""), 1889)
    out["phd2"] = (str(getattr(cfg, "phd2_host", "localhost") or "localhost"),
                   int(getattr(cfg, "phd2_port", 4400)))
    out["thesky"] = (str(getattr(cfg, "thesky_tcp_host", "localhost") or "localhost"),
                     int(getattr(cfg, "thesky_tcp_port", 3040)))
    return out


def launch_settings(cfg) -> dict:
    """What the script launches (it falls back to its own defaults when the
    service does not answer)."""
    from photonscript.shared.rigs import PIGGYBACK, rig_ids
    ep = endpoints(cfg)
    return {
        "nina_exe": str(getattr(cfg, "app_nina_exe", "")),
        "nina1_profile": str(getattr(cfg, "app_nina1_profile", "RC16")),
        "nina1_port": ep["nina1"][1],
        "nina2_enabled": PIGGYBACK in rig_ids(cfg),
        "nina2_profile": str(getattr(cfg, "app_nina2_profile", "Piggy-600")),
        "nina2_port": ep.get("nina2", ("", 1889))[1],
        "phd2_exe": str(getattr(cfg, "app_phd2_exe", "")),
        "phd2_port": ep["phd2"][1],
        "phd2_profile_id": int(getattr(cfg, "app_phd2_profile_id", 2)),
        "thesky_port": ep["thesky"][1],
    }


def expected(cfg, guided: bool) -> list[str]:
    """The apps a night needs: NINA #1, NINA #2 when the piggyback is on,
    PHD2 on a guided night."""
    keys = ["nina1"]
    if "nina2" in endpoints(cfg):
        keys.append("nina2")
    if guided:
        keys.append("phd2")
    return keys


def guided_by_default(cfg) -> bool:
    """Guided tonight as far as config can tell (no armer at hand)."""
    return bool(getattr(cfg, "guided_default", False)
                or (getattr(cfg, "noon_arm_enabled", False)
                    and getattr(cfg, "noon_arm_guided", True)))


# ------------------------------------------------------------------ probes

def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def process_counts() -> dict:
    """{exe name (lower case): count} for NINA, PHD2 and TheSky; {} when
    psutil cannot be read."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return {}
    want = (NINA_EXE, PHD2_EXE, THESKY_EXE)
    out = {n: 0 for n in want}
    try:
        for p in psutil.process_iter(["name"]):
            n = str(p.info.get("name") or "").lower()
            if n in out:
                out[n] += 1
    except Exception as e:  # noqa: BLE001
        logger.debug("process list unreadable: %s", e)
        return {}
    return out


def probe(cfg, port_fn: Callable = port_open,
          procs_fn: Callable = process_counts) -> dict:
    """{key: {"label", "host", "port", "answering", "processes"}}."""
    procs = procs_fn() or {}
    exe = {"nina1": NINA_EXE, "nina2": NINA_EXE, "phd2": PHD2_EXE,
           "thesky": THESKY_EXE}
    out = {}
    for key, (host, port) in endpoints(cfg).items():
        out[key] = {"label": LABELS[key], "host": host, "port": port,
                    "answering": bool(port_fn(host, port)),
                    "processes": procs.get(exe[key]) if procs else None}
    return out


def missing(apps: dict, keys: list[str]) -> list[str]:
    return [k for k in keys if not (apps.get(k) or {}).get("answering")]


def describe(apps: dict, keys: list[str]) -> str:
    return ", ".join(f"{LABELS[k]} (:{(apps.get(k) or {}).get('port', '?')})"
                     for k in keys)


# ------------------------------------------------------------------ guards

def _parse_utc(text) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text).rstrip("Z"))
    except (TypeError, ValueError):
        return None


def stop_blockers(cfg, *, state: str, night_of: Optional[str],
                  shutdown: Optional[dict], running: dict,
                  sun_alt: Optional[float], now: datetime,
                  capture_busy: Optional[list] = None) -> list[str]:
    """Why the apps must not be closed now (empty = safe to close).

    running: {label: [names of RUNNING items] or None (unreadable)}.
    capture_busy: rigs with a calibration capture job active (PS-113; a
    daytime calibration_autofill job may start at sunrise).
    Unreadable NINA state is not a blocker by itself: the armer state and
    the dawn-shutdown record carry the night, and a hung NINA is what a
    restart is for."""
    from photonscript.scheduler.armer import LIVE_STATES
    out = []
    if state in LIVE_STATES:
        out.append(f"armer is {state}")
    for rig in capture_busy or []:
        out.append(f"calibration capture job running on {rig}")
    for label, items in (running or {}).items():
        if items:
            out.append(f"{label} is running a sequence ({', '.join(items[:3])})")
    if sun_alt is not None and sun_alt < SUN_STOP_MAX_DEG:
        out.append(f"sun at {sun_alt:.1f} deg (below {SUN_STOP_MAX_DEG:.0f}): night")
    at = _parse_utc((shutdown or {}).get("at"))
    recent = at is not None and now - at < timedelta(hours=SHUTDOWN_RECENT_H)
    if state == "COMPLETE" and night_of and not recent:
        out.append("night over but its dawn shutdown has not recorded yet")
    if recent:
        if (shutdown or {}).get("verify") is None:
            out.append("dawn shutdown cooler verify has not run yet")
        mins = float(getattr(cfg, "app_lifecycle_stop_after_shutdown_min", 30.0))
        not_before = at + timedelta(minutes=mins)
        if now < not_before:
            out.append(f"waiting until {not_before:%H:%M}Z ({mins:.0f} min "
                       "after the dawn shutdown)")
    return out


def start_time_utc(cfg, now: datetime) -> datetime:
    """Today's launch time (scope local app_lifecycle_start_local) in UTC."""
    from photonscript.shared.localtime import utc_offset_hours
    off = utc_offset_hours(cfg, now)
    local = now + timedelta(hours=off)
    h, m = parse_hhmm(getattr(cfg, "app_lifecycle_start_local", "11:45"))
    start_local = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return start_local - timedelta(hours=off)


def local_day(cfg, now: datetime) -> str:
    from photonscript.shared.localtime import to_local
    return to_local(cfg, now).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ reports

def _report_path(cfg) -> Path:
    return Path(cfg.data_dir) / REPORT_FILE


def record_report(cfg, body: dict, now: Optional[datetime] = None) -> dict:
    """Append one script run (posted by observatory-apps.ps1)."""
    now = now or datetime.utcnow()
    mode = str(body.get("mode") or "").lower()
    rec = {"t": now.replace(microsecond=0).isoformat() + "Z",
           "day": local_day(cfg, now),
           "mode": mode if mode in MODES else "other",
           "ok": bool(body.get("ok", False)),
           "acted": bool(body.get("acted", False)),
           "dry_run": bool(body.get("dry_run", False)),
           "scheduled": bool(body.get("scheduled", False)),
           "message": str(body.get("message") or "")[:2000],
           "steps": [str(s)[:300] for s in (body.get("steps") or [])][:60],
           "page": bool(body.get("page", False))}
    p = _report_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def reports(cfg, n: int = 50) -> list[dict]:
    p = _report_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def last_reports(cfg) -> dict:
    """{mode: newest record}."""
    out: dict = {}
    for r in reports(cfg, 200):
        out[r.get("mode")] = r
    return out


def stop_done_today(cfg, now: Optional[datetime] = None) -> bool:
    """A real (not dry-run) stop that acted and succeeded today (scope
    local): the repeating Stop task then leaves apps someone reopened."""
    day = local_day(cfg, now or datetime.utcnow())
    return any(r.get("mode") == "stop" and r.get("day") == day and r.get("ok")
               and r.get("acted") and not r.get("dry_run")
               for r in reports(cfg, 200))


def should_page(cfg, rec: dict, now: Optional[datetime] = None) -> bool:
    """Page for a report that asks for it, once per mode per local day."""
    if not rec.get("page") or rec.get("dry_run"):
        return False
    day = rec.get("day") or local_day(cfg, now or datetime.utcnow())
    earlier = [r for r in reports(cfg, 200)[:-1]
               if r.get("mode") == rec.get("mode") and r.get("day") == day
               and r.get("page") and not r.get("dry_run")]
    return not earlier


def page_text(rec: dict) -> str:
    steps = "; ".join(rec.get("steps") or [])[:600]
    return (f"Observatory apps {rec.get('mode')}: {rec.get('message') or 'problem'}"
            + (f" ({steps})" if steps else "")
            + ". Log: <data_dir>\\logs\\observatory-apps.log on the scope PC.")


# ------------------------------------------------------------------ checks

def preflight_check(cfg, apps: dict, now: datetime, armed_day: bool,
                    guided: bool) -> dict:
    """The "Observatory apps running" preflight row. FAIL only with the
    lifecycle enabled, on an armed day, past the launch time + grace;
    otherwise a missing app is a WARN."""
    keys = expected(cfg, guided)
    miss = missing(apps, keys)
    name = "Observatory apps running"
    if not miss:
        return {"name": name, "status": "pass",
                "detail": "answering: " + describe(apps, keys)}
    due = start_time_utc(cfg, now) + timedelta(minutes=START_GRACE_MIN)
    late = now >= due
    status = "fail" if (enabled(cfg) and armed_day and late) else "warn"
    why = ("" if late else
           f" (the daily launch is at {getattr(cfg, 'app_lifecycle_start_local', '?')} local)")
    return {"name": name, "status": status,
            "detail": (f"not answering: {describe(apps, miss)}{why}. Start: "
                       "deploy\\observatory-apps.ps1 -Start on the scope PC")}


def armed_day(cfg, state: str = "") -> bool:
    from photonscript.scheduler.armer import LIVE_STATES
    return bool(state in LIVE_STATES
                or getattr(cfg, "auto_arm_enabled", False)
                or getattr(cfg, "noon_arm_enabled", False))


def alert_text(cfg, apps: dict, guided: bool, phase: str,
               preconfig_utc: Optional[str]) -> Optional[str]:
    """The armer's page text, or None when everything the night needs
    answers (or app_lifecycle_alert is off)."""
    if not getattr(cfg, "app_lifecycle_alert", True):
        return None
    miss = missing(apps, expected(cfg, guided))
    if not miss:
        return None
    pc = _parse_utc(preconfig_utc)
    when = f" before pre-config {pc:%H:%M}Z" if pc else ""
    return (f"{phase}: {describe(apps, miss)} not running. Start "
            f"{'it' if len(miss) == 1 else 'them'}{when}: "
            "deploy\\observatory-apps.ps1 -Start on the scope PC (or by hand).")


# ------------------------------------------------------------------ status

async def _sequence_running(cfg) -> dict:
    """{label: [RUNNING item names] or None (unreadable)} per NINA."""
    from photonscript.scheduler import sideload as sd
    from photonscript.shared.rigs import PIGGYBACK, RC16, rig_config, rig_ids
    out = {}
    for rig, label in ((RC16, LABELS["nina1"]), (PIGGYBACK, LABELS["nina2"])):
        if rig not in rig_ids(cfg):
            continue
        tree, err = await sd.read_sequence_state(rig_config(cfg, rig).nina_base_url)
        out[label] = None if err else sd.nina_running(tree)
    return out


def _thesky_mount(cfg) -> dict:
    """TheSky's own mount connection (read-only script, never Connect)."""
    from photonscript.telescope_agent.thesky_client import (TheSkyClient,
                                                            TheSkyError, _truthy)
    c = TheSkyClient(getattr(cfg, "thesky_tcp_host", "localhost"),
                     int(getattr(cfg, "thesky_tcp_port", 3040)), timeout=4.0)
    try:
        flags = c.mount_flags()
    except TheSkyError as e:
        return {"answering": False, "mount_connected": None, "error": str(e)}
    v = flags.get("connected")
    return {"answering": True,
            "mount_connected": _truthy(v) if v is not None else None,
            "parked": flags.get("parked")}


def _capture_busy(cfg) -> list:
    from photonscript.scheduler import calibration_capture as cc
    from photonscript.shared.rigs import rig_ids
    return [r for r in rig_ids(cfg) if cc.busy(r)]


async def status(cfg, armer, now: Optional[datetime] = None,
                 seq_fn=None, thesky_fn=None, probe_fn=None,
                 sun_fn=None, busy_fn=None) -> dict:
    """GET /api/apps/status."""
    import asyncio
    now = now or datetime.utcnow()
    apps = await asyncio.to_thread(probe_fn or (lambda: probe(cfg)))
    running = {}
    if any((apps.get(k) or {}).get("answering") for k in ("nina1", "nina2")):
        try:
            running = await (seq_fn or _sequence_running)(cfg)
        except Exception as e:  # noqa: BLE001
            logger.warning("apps status: NINA sequence state unreadable: %s", e)
    thesky = {"answering": (apps.get("thesky") or {}).get("answering")}
    if thesky["answering"]:
        try:
            thesky = await asyncio.to_thread(thesky_fn or (lambda: _thesky_mount(cfg)))
        except Exception as e:  # noqa: BLE001
            thesky = {"answering": True, "mount_connected": None, "error": str(e)}
    if sun_fn is not None:
        sun_alt = sun_fn(now)
    else:
        sun_alt = None
        try:
            from datetime import timezone
            from photonscript.shared.pushover import sun_altitude_deg
            sun_alt = round(sun_altitude_deg(float(cfg.observatory_lat),
                                             float(cfg.observatory_lon),
                                             now.replace(tzinfo=timezone.utc)), 1)
        except Exception:  # noqa: BLE001
            sun_alt = None
    state = str(getattr(armer, "state", "") or "")
    plan = getattr(armer, "plan", None) or {}
    shutdown = getattr(armer, "shutdown", None)
    blockers = stop_blockers(cfg, state=state, night_of=plan.get("night_of"),
                             shutdown=shutdown, running=running,
                             sun_alt=sun_alt, now=now,
                             capture_busy=(busy_fn or _capture_busy)(cfg))
    guided = guided_by_default(cfg)
    if state and hasattr(armer, "_use_guiding") and plan:
        try:
            guided = bool(armer._use_guiding())
        except Exception:  # noqa: BLE001
            pass
    keys = expected(cfg, guided)
    return {
        "enabled": enabled(cfg),
        "alert": bool(getattr(cfg, "app_lifecycle_alert", True)),
        "start_local": getattr(cfg, "app_lifecycle_start_local", "11:45"),
        "start_utc": start_time_utc(cfg, now).isoformat() + "Z",
        "stop_after_shutdown_min": float(getattr(
            cfg, "app_lifecycle_stop_after_shutdown_min", 30.0)),
        "armer_state": state,
        "night_of": plan.get("night_of"),
        "shutdown": {"at": (shutdown or {}).get("at"),
                     "verify_done": (shutdown or {}).get("verify") is not None}
        if shutdown else None,
        "sun_alt_deg": sun_alt,
        "apps": apps,
        "needed": keys,
        "missing": missing(apps, keys),
        "sequence_running": running,
        "thesky": thesky,
        "stop_ok": not blockers,
        "stop_blockers": blockers,
        "stop_done_today": stop_done_today(cfg, now),
        "launch": launch_settings(cfg),
        "last": last_reports(cfg),
    }
