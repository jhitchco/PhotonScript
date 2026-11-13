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

2026-10-26 (the close did not happen): the 10-25 night ended at 12:39Z; a
deploy restarted the service at 12:55Z, 17 min into the dawn-shutdown
cooler verify wait, and the verify task died with the process. The
shutdown record (restored from armer_state.json) kept "verify": None, so
every scheduled stop until 11:00 said "dawn shutdown cooler verify has not
run yet", and the 11:45 start found the apps running and left them stale.
Since then:
  - the armer re-arms a lost verify on restore (armer._resume_shutdown_verify);
  - a verify still missing verify_delay + VERIFY_OVERDUE_MIN after the
    shutdown is derived here from a read-only camera read (every cooler
    off or the camera unreachable = done), so it never blocks all morning;
  - when the morning stop window passes without a close, the 11:45 start
    restarts the apps (close, then launch) when restart_blockers() is
    empty (app_lifecycle_restart_at_start), and pages when the apps stay
    stale; the noon re-arm holds while that restart runs
    (restart_in_progress);
  - a blocked scheduled stop posts its blockers (once per distinct reason a
    day), so the next start and the story can say why;
  - fresh_line() puts "restarted fresh / NOT restarted" in the night story,
    the evening plan heads-up and the morning report card.
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
RESTART_PHASES = ("begin", "done", "blocked", "failed", "none")
SUN_STOP_MAX_DEG = -6.0     # never close the apps while the sun is below this
SHUTDOWN_RECENT_H = 20.0    # a dawn-shutdown record this young is "last night"
START_GRACE_MIN = 15.0      # preflight fails only this long after the launch
VERIFY_OVERDUE_MIN = 10.0   # a cooler verify this late past its due time is lost
RESTART_MIN_BEFORE_PRECONFIG_MIN = 60.0   # default of the config key
NOON_MARGIN_MIN = 5.0       # no start-time restart this close to the noon re-arm
RESTART_HOLD_MIN = 20.0     # the noon re-arm waits at most this for a restart
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
        "nina2_profile": str(getattr(cfg, "app_nina2_profile", "piggyback600")),
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


def verify_delay_s(cfg) -> int:
    """When the armer runs the dawn-shutdown cooler verify (the same rule
    as armer._shutdown_verify_delay_s: warm minutes + 17 min, at least 5)."""
    try:
        warm = float(getattr(cfg, "gradual_warm_minutes", 0.0) or 0.0)
    except (TypeError, ValueError):
        warm = 0.0
    return max(300, int((warm + 17.0) * 60))


def verify_overdue(cfg, shutdown: Optional[dict], now: datetime) -> bool:
    """True when last night's shutdown (recent) has no verify although it was
    due VERIFY_OVERDUE_MIN ago: the verify task was lost (a service restart
    between the shutdown and the verify, 2026-10-26)."""
    at = _parse_utc((shutdown or {}).get("at"))
    if at is None or (shutdown or {}).get("verify") is not None:
        return False
    if now - at >= timedelta(hours=SHUTDOWN_RECENT_H):
        return False
    due = at + timedelta(seconds=verify_delay_s(cfg), minutes=VERIFY_OVERDUE_MIN)
    return now >= due


def derive_verify(rigs: dict) -> dict:
    """A verify result from read-only camera reads {rig: info or None}: ok
    when no cooler reads ON (an unreachable camera counts as off, as in the
    armer's verify: the app is closed or the camera disconnected)."""
    out, on = {}, []
    for rig, info in (rigs or {}).items():
        if not isinstance(info, dict):
            out[rig] = "unreachable"
            continue
        c = bool(info.get("CoolerOn", False))
        out[rig] = {"cooler_on": c, "temp_c": info.get("Temperature")}
        if c:
            on.append(rig)
    return {"at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "ok": not on, "rigs": out, "derived": True, "cooler_on": on}


async def _camera_reads(cfg) -> dict:
    from photonscript.shared.rigs import nina_camera_info, rig_config, rig_ids
    out = {}
    for rig in rig_ids(cfg):
        try:
            out[rig] = await nina_camera_info(rig_config(cfg, rig).nina_base_url)
        except Exception:  # noqa: BLE001
            out[rig] = None
    return out


def effective_shutdown(shutdown: Optional[dict], derived: Optional[dict]) -> Optional[dict]:
    """The shutdown record the guards judge: a derived verify fills a lost
    one when it found every cooler off; a cooler still ON keeps it missing."""
    if not shutdown or shutdown.get("verify") is not None or not derived:
        return shutdown
    if not derived.get("ok"):
        return shutdown
    return {**shutdown, "verify": derived}


def restart_min_before_preconfig(cfg) -> float:
    try:
        return float(getattr(cfg, "app_lifecycle_restart_min_before_preconfig",
                             RESTART_MIN_BEFORE_PRECONFIG_MIN))
    except (TypeError, ValueError):
        return RESTART_MIN_BEFORE_PRECONFIG_MIN


def restart_blockers(cfg, *, state: str, plan: Optional[dict],
                     shutdown: Optional[dict], running: dict,
                     sun_alt: Optional[float], now: datetime,
                     capture_busy: Optional[list] = None) -> list[str]:
    """Why the 11:45 start must not restart (close, then launch) the apps
    now; empty = safe. The stop guards, with two differences:

      - ARMED does not block while pre-config is at least
        app_lifecycle_restart_min_before_preconfig (60) min away: arm()
        loads no sequence (the dispatch at pre-config does) and the
        unguided guard runs again at dispatch, so fresh apps lose nothing.
        The noon re-arm's PS-125 guard only needs NINA #1 to answer, and it
        holds while the restart runs (restart_in_progress).
      - an idle armer with the noon re-arm on blocks from NOON_MARGIN_MIN
        before 12:00 local: the arm would race the closing apps.
    """
    out = []
    if not getattr(cfg, "app_lifecycle_restart_at_start", True):
        out.append("app_lifecycle_restart_at_start is off")
    plan = plan or {}
    guard_state = state
    if state == "ARMED":
        pc = _parse_utc(plan.get("preconfig_utc"))
        lead = restart_min_before_preconfig(cfg)
        if pc is not None and pc - now >= timedelta(minutes=lead):
            guard_state = ""          # waiting for pre-config: nothing loaded
        else:
            when = f"{pc:%H:%M}Z" if pc else "unknown"
            out.append(f"armer is ARMED and pre-config ({when}) is less than "
                       f"{lead:.0f} min away")
            guard_state = ""
    from photonscript.scheduler.armer import LIVE_STATES
    if state not in LIVE_STATES and getattr(cfg, "noon_arm_enabled", False):
        from photonscript.shared.localtime import to_local
        loc = to_local(cfg, now)
        mins = loc.hour * 60 + loc.minute
        if mins >= 12 * 60 - NOON_MARGIN_MIN:
            out.append("the noon re-arm is due at 12:00 local (a restart now "
                       "would race it)")
    out += stop_blockers(cfg, state=guard_state, night_of=plan.get("night_of"),
                         shutdown=shutdown, running=running, sun_alt=sun_alt,
                         now=now, capture_busy=capture_busy)
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
    # 2026-10-26: the start-time restart ("begin" before closing, then
    # "done" / "blocked" / "failed" / "none") and whether the apps the
    # script is responsible for are fresh today after this run
    rs = str(body.get("restart") or "").lower()
    if rs in RESTART_PHASES:
        rec["restart"] = rs
    if body.get("fresh") is not None:
        rec["fresh"] = bool(body.get("fresh"))
    if body.get("blocked"):
        rec["blocked"] = True
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


def _day_records(cfg, day: str) -> list[dict]:
    return [r for r in reports(cfg, 400)
            if r.get("day") == day and not r.get("dry_run")]


def restart_in_progress(cfg, now: Optional[datetime] = None) -> bool:
    """True while the start-time restart runs: today's newest start report
    is its "begin" (posted before the apps close) and is under
    RESTART_HOLD_MIN old. The noon re-arm holds meanwhile, so it neither
    pages "NINA state unreadable" nor arms against a closing NINA."""
    now = now or datetime.utcnow()
    starts = [r for r in _day_records(cfg, local_day(cfg, now))
              if r.get("mode") == "start"]
    if not starts or starts[-1].get("restart") != "begin":
        return False
    t = _parse_utc(starts[-1].get("t"))
    return t is not None and timedelta(0) <= now - t < timedelta(minutes=RESTART_HOLD_MIN)


def _hm_local(cfg, t) -> str:
    dt = _parse_utc(t)
    if dt is None:
        return "?"
    try:
        from photonscript.shared.localtime import to_local
        return to_local(cfg, dt).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return dt.strftime("%H:%MZ")


def fresh_for_day(cfg, day: str) -> dict:
    """Were the apps closed and relaunched on this local day (the morning
    before night `day`)? {"fresh": True | False | None (no lifecycle runs),
    "restarted": the 11:45 start did the close, "closed_at", "launched_at",
    "why": why not (or why the start had to restart)}."""
    recs = _day_records(cfg, day)
    out = {"day": day, "fresh": None, "restarted": False, "closed_at": None,
           "launched_at": None, "why": None}
    if not recs:
        return out
    closes = [r for r in recs if r.get("mode") == "stop" and r.get("ok")
              and r.get("acted")]
    blocked = [r for r in recs if r.get("mode") == "stop" and r.get("blocked")]
    starts = [r for r in recs if r.get("mode") == "start"
              and r.get("restart") != "begin"]
    if closes:
        out["closed_at"] = closes[0].get("t")
    if starts:
        out["launched_at"] = starts[-1].get("t")
        out["restarted"] = any(r.get("restart") == "done" for r in starts)
    stop_why = blocked[-1].get("message") if blocked else None
    if closes:
        out["fresh"] = True
        if out["restarted"]:
            out["why"] = stop_why or "the morning stop did not close them"
        return out
    if starts:
        last = starts[-1]
        out["fresh"] = bool(last.get("fresh")) if "fresh" in last else False
        if not out["fresh"]:
            note = next((s for s in last.get("steps") or []
                         if str(s).startswith("NOT restarted")), None)
            out["why"] = note or stop_why or last.get("message") or "not closed today"
        return out
    if blocked:
        out["fresh"] = False
        out["why"] = stop_why
    return out


def fresh_line(cfg, day: str) -> Optional[str]:
    """One line for the story, the plan heads-up and the morning report:
    were the apps restarted fresh on `day`? None when nothing ran."""
    f = fresh_for_day(cfg, day)
    if f["fresh"] is None:
        return None
    if f["fresh"]:
        if f["restarted"]:
            return (f"Apps restarted fresh {day} by the {_hm_local(cfg, f['launched_at'])} "
                    f"start (the morning stop did not close them: {f['why']}).")
        tail = (f", launched {_hm_local(cfg, f['launched_at'])}"
                if f["launched_at"] else ", not launched yet")
        return (f"Apps restarted fresh {day}: closed "
                f"{_hm_local(cfg, f['closed_at'])}{tail} local.")
    return (f"Apps NOT restarted {day} ({f['why']}): NINA #1 / #2 and PHD2 run "
            "on from an earlier day, so their drivers are not fresh.")


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
                 sun_fn=None, busy_fn=None, cam_fn=None) -> dict:
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
    derived = None
    if verify_overdue(cfg, shutdown, now):
        # 2026-10-26: the verify task was lost (service restart); judge the
        # coolers from a read-only camera read instead of blocking all day
        try:
            derived = derive_verify(await (cam_fn or _camera_reads)(cfg))
        except Exception as e:  # noqa: BLE001
            logger.warning("apps status: derived cooler verify failed: %s", e)
    judged = effective_shutdown(shutdown, derived)
    busy = (busy_fn or _capture_busy)(cfg)
    blockers = stop_blockers(cfg, state=state, night_of=plan.get("night_of"),
                             shutdown=judged, running=running,
                             sun_alt=sun_alt, now=now, capture_busy=busy)
    if derived and not derived.get("ok"):
        blockers.append("dawn shutdown cooler verify was lost and a cooler still "
                        "reads ON (" + ", ".join(derived.get("cooler_on") or []) + ")")
    r_blockers = restart_blockers(cfg, state=state, plan=plan, shutdown=judged,
                                  running=running, sun_alt=sun_alt, now=now,
                                  capture_busy=busy)
    if derived and not derived.get("ok"):
        r_blockers.append(blockers[-1])
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
                     "verify_done": (judged or {}).get("verify") is not None,
                     "verify_derived": bool(derived and derived.get("ok"))}
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
        "restart_at_start": bool(getattr(cfg, "app_lifecycle_restart_at_start", True)),
        "restart_ok": not r_blockers,
        "restart_blockers": r_blockers,
        "restart_in_progress": restart_in_progress(cfg, now),
        "fresh_today": fresh_for_day(cfg, local_day(cfg, now)),
        "launch": launch_settings(cfg),
        "last": last_reports(cfg),
    }
