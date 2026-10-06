"""PS-150: alert on a NINA that is not running, or up but silent.

2026-10-04 (the "blind night"): both NINAs were closed around 19:31-19:45
local and nothing started them again until 06:10. The nanny could only say
"Safety monitor DISCONNECTED ... reconnect it in NINA" every hour, which never
said that NINA itself was gone. This watchdog says what is wrong with each
NINA in one push per state change per night.

Every TICK_S, per rig (RC16, plus the Piggy-600 when piggyback_enabled):

  window     the sun is at or below nina_watch_sun_alt_deg (dusk to dawn,
             dawn flats included). Outside it everything is "idle".
  expected   the armer has a night (ARMED, RUNNING, a pause or WATCHING), or
             this rig's NINA process is running. A closed NINA on an unarmed
             night is nobody's business.
  api        GET <nina_base_url>/version answers.
  log        the rig's newest NINA log (picked by the Advanced API port its
             startup line names, the same rule as /api/nina/log): mtime, size,
             the process id NINA puts in the file name
             (20261005-061046-3.2.0.9001.<pid>-202610.log), and whether its
             tail ends in "Application shutting down" (closed).
  process    that pid is alive and is a NINA process (psutil). None when it
             cannot be told (no psutil, no log).
  sequence   NINA's running leaf instruction (sequence state), when the API
             answers.

States (classify(), pure):

  ok           API answers and nothing below holds
  api_down     NINA runs and still logs, but its Advanced API does not
               answer: PhotonScript is blind to it (safety reconnect,
               guiding watchdog, dawn shutdown cannot reach it)
  silent       NINA is up but silent: the process is there and its log has
               not grown for nina_watch_silent_minutes, while the API is down
               too (hung), or while the API says the same non-wait
               instruction has run that long (stuck instruction)
  not_running  no NINA process for this rig (or its log ends in a clean
               shutdown when the process cannot be checked) and no API
  idle / off   outside the window, rig not expected, or nina_watch_mode off

A non-ok state must hold for CONFIRM_TICKS consecutive ticks before it
counts, so a NINA restart does not alert. Then one Pushover per (rig, state)
per night with what to do (priority 1 while the armer has a night), one
recovery push when an alerted rig is ok again, events kind "nina_watch" in
runs/<night>_events.jsonl, and the dashboard chip (GET /api/nina/watch).
nina_watch_mode: alert (default) | panel (chip + events, no push) | off.

Observe only: this never starts, restarts, stops or commands NINA.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("alert", "panel", "off")
BAD = ("api_down", "silent", "not_running")
TICK_S = 60
CONFIRM_TICKS = 3
LIVE = ("ARMED", "RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR", "WATCHING")
# a running leaf with any of these (lower case) may legitimately log nothing
QUIET_OK = ("wait", "loop", "safe", "time", "altitude", "dusk", "dawn")
_PID_RE = re.compile(r"\.(\d+)-\d{6}\.log$", re.I)
_CLOSED = "Application shutting down"


# ------------------------------------------------------------------ config

def mode(cfg) -> str:
    m = str(getattr(cfg, "nina_watch_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def _f(cfg, key, default) -> float:
    try:
        v = float(getattr(cfg, key, default))
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def silent_minutes(cfg) -> float:
    return max(1.0, _f(cfg, "nina_watch_silent_minutes", 15.0))


def rig_name(cfg, rig: str) -> str:
    if rig == "piggyback":
        return f"NINA #2 ({getattr(cfg, 'piggyback_name', 'Piggy-600')})"
    return "NINA #1 (RC16)"


def rig_base(cfg, rig: str) -> str:
    if rig == "piggyback":
        return str(getattr(cfg, "piggyback_nina_base_url", "") or "")
    return str(getattr(cfg, "nina_base_url", "") or "")


def in_window(cfg, now: datetime) -> bool:
    """Sun at or below nina_watch_sun_alt_deg. Unknown site: True."""
    from photonscript.shared.pushover import sun_altitude_deg
    try:
        lat = float(cfg.observatory_lat)
        lon = float(cfg.observatory_lon)
    except (AttributeError, TypeError, ValueError):
        return True
    alt = sun_altitude_deg(lat, lon, now.replace(tzinfo=timezone.utc))
    return alt <= _f(cfg, "nina_watch_sun_alt_deg", -6.0)


# ------------------------------------------------------------------ log + process

def pid_of(name: str) -> int | None:
    """The NINA process id in a log file name, or None."""
    m = _PID_RE.search(Path(str(name)).name)
    return int(m.group(1)) if m else None


def rig_log(cfg, rig: str) -> Path | None:
    """The newest NINA log of this rig (by the Advanced API port its startup
    line names; a dedicated NINA #2 logs folder: just the newest file)."""
    from photonscript.scheduler.routers.triage import (
        _all_nina_logs, _log_port, _nina_setup)
    logs_dir, dedicated, port, _pig = _nina_setup(cfg, rig)
    if not logs_dir:
        return None
    logs = _all_nina_logs(logs_dir, 15)
    if not logs:
        return None
    if dedicated:
        return logs[0]
    if port:
        for p in logs:
            if _log_port(p) == port:
                return p
    return None


def closed_at(p: Path, tail_bytes: int = 16384) -> str | None:
    """The timestamp of the "Application shutting down" line when the log's
    tail has one (NINA was closed), else None."""
    try:
        with open(p, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    hit = None
    for line in tail.splitlines():
        if _CLOSED in line:
            hit = line.split("|", 1)[0].strip()
    return hit or None


def log_facts(p: Path | None) -> dict:
    """{"file", "mtime" (naive UTC), "size", "pid", "closed_at"} of a log
    (all None when there is none)."""
    out = {"file": None, "mtime": None, "size": None, "pid": None,
           "closed_at": None}
    if p is None:
        return out
    try:
        st = Path(p).stat()
    except OSError:
        return out
    out.update(file=Path(p).name, size=st.st_size,
               mtime=datetime.fromtimestamp(st.st_mtime, timezone.utc).replace(tzinfo=None),
               pid=pid_of(Path(p).name), closed_at=closed_at(Path(p)))
    return out


def process_alive(pid: int | None) -> bool | None:
    """True / False when the pid is (not) a running NINA process, None when it
    cannot be told."""
    if not pid:
        return None
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return None
    try:
        if not psutil.pid_exists(pid):
            return False
        return "nina" in psutil.Process(pid).name().lower()
    except Exception as e:  # noqa: BLE001 (gone between calls, access denied)
        name = type(e).__name__
        if name == "NoSuchProcess":
            return False
        return None


# ------------------------------------------------------------------ classify

def _quiet_ok(leaf: str | None) -> bool:
    low = str(leaf or "").lower()
    return any(k in low for k in QUIET_OK)


def classify(r: dict, limit_min: float) -> tuple[str, str]:
    """(state, why) for one rig's reads. r keys: window, expected, api_ok,
    process, log_quiet_min, closed_at, seq_leaf, seq_exposure_s,
    leaf_age_min."""
    if not r.get("window"):
        return "idle", "outside the night window"
    if not r.get("expected"):
        return "idle", "not armed and NINA not running"
    quiet = r.get("log_quiet_min")
    long_quiet = quiet is not None and quiet >= limit_min
    proc = r.get("process")
    if r.get("api_ok"):
        leaf = r.get("seq_leaf")
        age = r.get("leaf_age_min")
        # a long exposure logs nothing until it ends: give it its length
        exp_min = float(r.get("seq_exposure_s") or 0) / 60.0
        lim = max(limit_min, exp_min + 5.0) if exp_min else limit_min
        if (leaf and not _quiet_ok(leaf) and quiet is not None and quiet >= lim
                and age is not None and age >= lim):
            return "silent", (f"the sequence shows {leaf} running for "
                              f"{age:.0f} min but the log has not grown for "
                              f"{quiet:.0f} min")
        return "ok", "API answers"
    if proc is False:
        return "not_running", "no NINA process and no API"
    if proc is None and r.get("closed_at"):
        return "not_running", f"its log ends in a shutdown at {r['closed_at']}"
    if long_quiet:
        return "silent", (f"the API does not answer and the log has not grown "
                          f"for {quiet:.0f} min")
    return "api_down", "the API does not answer but NINA still logs"


# ------------------------------------------------------------------ monitor

class NinaWatch:
    """Per-rig debounce + once per (rig, state) per night latch. update()
    feeds one rig's reads; view() is the chip payload."""

    def __init__(self):
        self.night: str | None = None
        self.alerted: set[tuple[str, str]] = set()
        self.rigs: dict[str, dict] = {}

    def _roll(self, night: str | None) -> None:
        if night and night != self.night:
            self.night = night
            self.alerted = set()
            for s in self.rigs.values():
                s["alerted_state"] = None

    def _rig(self, rig: str) -> dict:
        return self.rigs.setdefault(rig, {
            "state": "idle", "why": "", "since": None, "pending": None,
            "pending_n": 0, "alerted_state": None, "leaf": None,
            "leaf_since": None, "size": None, "size_at": None, "reads": {},
            "checked_at": None})

    def track(self, rig: str, now: datetime, leaf: str | None, size,
              mtime: datetime | None) -> tuple[float | None, float | None]:
        """(log_quiet_min, leaf_age_min) from what this monitor has seen: the
        log is quiet since the later of its mtime and the last size change we
        saw; the leaf ages from when we first saw it running."""
        s = self._rig(rig)
        if size is not None and size != s["size"]:
            # the first sighting is not a change (a restart of PhotonScript
            # must not reset the quiet clock); later growth is
            s["size_at"] = now if s["size"] is not None else None
            s["size"] = size
        if leaf != s["leaf"]:
            s["leaf"], s["leaf_since"] = leaf, now
        quiet = None
        marks = [t for t in (mtime, s["size_at"]) if t is not None]
        if marks:
            quiet = max(0.0, (now - max(marks)).total_seconds() / 60.0)
        age = ((now - s["leaf_since"]).total_seconds() / 60.0
               if leaf and s["leaf_since"] else None)
        return quiet, age

    def update(self, rig: str, state: str, why: str, reads: dict,
               night: str | None, now: datetime) -> dict:
        """Returns {"state" (confirmed), "changed", "alert" (bad, first time
        tonight), "recovered" (ok after an alerted bad state), "prev"}."""
        self._roll(night)
        s = self._rig(rig)
        s["reads"], s["checked_at"] = reads, now
        res = {"changed": False, "alert": False, "recovered": False,
               "prev": s["state"]}
        if state in BAD:
            if s["pending"] == state:
                s["pending_n"] += 1
            else:
                s["pending"], s["pending_n"] = state, 1
            # still debouncing: keep the confirmed state we had
            confirmed = state if s["pending_n"] >= CONFIRM_TICKS else s["state"]
        else:
            s["pending"], s["pending_n"] = None, 0
            confirmed = state
        if confirmed != s["state"]:
            res["changed"] = True
            s["state"], s["since"] = confirmed, now
        s["why"] = why if confirmed == state else s["why"]
        if res["changed"] and confirmed in BAD and (rig, confirmed) not in self.alerted:
            self.alerted.add((rig, confirmed))
            s["alerted_state"] = confirmed
            res["alert"] = True
        elif res["changed"] and confirmed == "ok" and s["alerted_state"]:
            res["recovered"] = True
            res["prev"] = s["alerted_state"]
            s["alerted_state"] = None
        res["state"] = confirmed
        return res

    def view(self) -> dict:
        def iso(t):
            return t.replace(microsecond=0).isoformat() + "Z" if t else None
        rigs = {}
        for rig, s in self.rigs.items():
            reads = dict(s["reads"] or {})
            for k in ("log_mtime",):
                if isinstance(reads.get(k), datetime):
                    reads[k] = iso(reads[k])
            rigs[rig] = {"state": s["state"], "why": s["why"],
                         "since": iso(s["since"]),
                         "pending": s["pending"], "alerted": bool(s["alerted_state"]),
                         "checked_at": iso(s["checked_at"]), "reads": reads}
        return {"night": self.night, "rigs": rigs}


MONITOR = NinaWatch()


# ------------------------------------------------------------------ texts

def _local_hm(cfg, t: datetime | None) -> str:
    if t is None:
        return "?"
    try:
        from photonscript.shared.localtime import to_local
        return to_local(cfg, t).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return t.strftime("%H:%M") + "Z"


def alert_text(cfg, rig: str, state: str, why: str, reads: dict,
               armer_state: str) -> str:
    who = rig_name(cfg, rig)
    port = rig_base(cfg, rig)
    night = (f"The armer is {armer_state}, so this rig is losing the night."
             if armer_state in LIVE else "")
    last = (f"last log line {_local_hm(cfg, reads.get('log_mtime'))} local"
            + (f" ({reads.get('log_file')})" if reads.get("log_file") else ""))
    if state == "not_running":
        closed = (f" Its log ends in an orderly shutdown at "
                  f"{str(reads.get('closed_at'))[11:16]} (closed by hand or by "
                  "Windows, not a crash)." if reads.get("closed_at") else "")
        return (f"{who} is NOT RUNNING: no NINA process and its API ({port}) "
                f"refuses connections; {last}.{closed} {night} To fix: start "
                f"{who.split(' (')[0]} on the scope PC (remote desktop), connect "
                "the equipment, then re-arm or reload tonight's sequence. "
                "PhotonScript never starts NINA itself.").replace("  ", " ")
    if state == "api_down":
        return (f"{who} is running (pid {reads.get('pid') or '?'}) and still "
                f"logging, but its Advanced API ({port}) does not answer "
                f"({reads.get('api_error') or 'no reply'}). PhotonScript is "
                "blind to this NINA: safety reconnect, guiding watchdog and the "
                "dawn shutdown cannot reach it. To fix: in NINA check Plugins > "
                "Advanced API (server on, port), or restart NINA when the "
                "sequence allows. Nothing was changed.")
    if state == "silent":
        return (f"{who} is UP BUT SILENT: the process (pid "
                f"{reads.get('pid') or '?'}) is there but {why}; {last}. NINA "
                f"may be hung. {night} To fix: look at {who.split(' (')[0]} on the "
                "scope PC (remote desktop); if it is frozen, Stop & Make Safe "
                "from the dashboard, close and restart NINA, then re-arm. "
                "PhotonScript restarts nothing.").replace("  ", " ")
    return f"{who}: {state} ({why})"


def recovery_text(cfg, rig: str, prev: str) -> str:
    label = {"not_running": "not running", "api_down": "API down",
             "silent": "up but silent"}.get(prev, prev)
    return (f"{rig_name(cfg, rig)} is OK again (API answers, log growing) "
            f"after {label}.")


def _event(cfg, rig: str, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        append_jsonl(events_path(cfg, night_of(cfg, now)),
                     {"t": iso_z(now), "rig": rig, "src": "photonscript",
                      "kind": "nina_watch", "value": value, "detail": detail,
                      **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("nina watch event (%s) not logged: %s", value, e)


# ------------------------------------------------------------------ reads

async def api_version(base: str, timeout: float = 8.0) -> tuple[bool, str]:
    """(answered, error) for GET <base>/version."""
    if not base:
        return False, "no API URL configured"
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(base.rstrip("/") + "/version")
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}"
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}".rstrip(": ")


async def running_leaf(base: str) -> tuple[str | None, float | None]:
    """(name of NINA's running leaf instruction, its exposure time in s):
    name '' when idle, None when unknown."""
    try:
        from photonscript.scheduler.sideload import read_sequence_state
        from photonscript.scheduler.where_panel import (
            _exposure_time, _name, running_chain)
        tree, _err = await asyncio.wait_for(read_sequence_state(base), 10)
    except Exception:  # noqa: BLE001
        return None, None
    if tree is None:
        return None, None
    chain = running_chain(tree)
    if not chain:
        return "", None
    return _name(chain[-1]), _exposure_time(chain[-1])


async def read_rig(cfg, rig: str) -> dict:
    """The live reads for one rig (never raises)."""
    base = rig_base(cfg, rig)
    ok, err = await api_version(base)
    try:
        facts = await asyncio.to_thread(lambda: log_facts(rig_log(cfg, rig)))
    except Exception as e:  # noqa: BLE001
        logger.debug("nina watch: log read failed for %s: %s", rig, e)
        facts = log_facts(None)
    proc = await asyncio.to_thread(process_alive, facts["pid"])
    leaf, exp_s = await running_leaf(base) if ok else (None, None)
    return {"api_ok": ok, "api_error": err, "process": proc, "pid": facts["pid"],
            "log_file": facts["file"], "log_mtime": facts["mtime"],
            "log_size": facts["size"], "closed_at": facts["closed_at"],
            "seq_leaf": leaf, "seq_exposure_s": exp_s}


# ------------------------------------------------------------------ tick

async def tick(cfg, armer_state: str, now: datetime | None = None,
               read=None, notify=None, window: bool | None = None,
               monitor: NinaWatch | None = None) -> dict:
    """One pass over the rigs. read(cfg, rig) -> reads and notify are
    injectable for tests. Returns {rig: update result}."""
    from photonscript.shared.phd2_store import night_of
    from photonscript.shared.rigs import rig_ids
    monitor = monitor or MONITOR
    now = now or datetime.utcnow()
    m = mode(cfg)
    read = read or read_rig
    win = in_window(cfg, now) if window is None else window
    night = night_of(cfg, now)
    limit = silent_minutes(cfg)
    out = {}
    for rig in rig_ids(cfg):
        if m == "off" or not win:
            reads = {"window": win}
            res = monitor.update(rig, "idle", "off (nina_watch_mode)" if m == "off"
                                 else "outside the night window", reads, night, now)
            out[rig] = res
            continue
        reads = await read(cfg, rig)
        quiet, age = monitor.track(rig, now, reads.get("seq_leaf"),
                                   reads.get("log_size"), reads.get("log_mtime"))
        reads = {**reads, "window": win, "log_quiet_min": quiet,
                 "leaf_age_min": age,
                 "expected": armer_state in LIVE or reads.get("process") is True}
        state, why = classify(reads, limit)
        res = monitor.update(rig, state, why, reads, night, now)
        out[rig] = res
        if res["alert"]:
            msg = alert_text(cfg, rig, res["state"], why, reads, armer_state)
            _event(cfg, rig, res["state"], msg, now, pid=reads.get("pid"),
                   log_file=reads.get("log_file"), api_error=reads.get("api_error"))
            if m == "alert":
                await _push(cfg, msg, 1 if armer_state in LIVE else 0, notify)
        elif res["recovered"]:
            msg = recovery_text(cfg, rig, res["prev"])
            _event(cfg, rig, "ok", msg, now)
            if m == "alert":
                await _push(cfg, msg, 0, notify)
        elif res["changed"]:
            _event(cfg, rig, res["state"], why, now)
    return out


async def _push(cfg, msg: str, priority: int, notify=None) -> None:
    if notify is None:
        from photonscript.shared.pushover import notify as _n
        notify = _n
    try:
        await notify(cfg, msg, title="PhotonScript NINA watch", priority=priority)
    except Exception as e:  # noqa: BLE001
        logger.warning("nina watch push failed: %s", e)


async def run_watchdog(get_config, get_armer, tick_seconds: int = TICK_S) -> None:
    """Background loop (app startup). Never raises, never commands NINA."""
    while True:
        try:
            cfg = get_config()
            await tick(cfg, str(getattr(get_armer(), "state", "") or ""))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("nina watch tick failed: %s", e)
        await asyncio.sleep(tick_seconds)
