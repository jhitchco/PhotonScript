"""PS-255 pre-arm readiness: one checklist of everything the night needs.

Jeremy, 2026-11-14: "a pre-arm check to see if everything is configured the
right way... not just the mount but EVERYTHING". On 11-13 the arm ran with
COM3 dead after a Windows Update reboot, and the arm-time audit said
"ProTrack is OFF, tick it" while both boxes were ticked. The checks existed
but were scattered, ran only at arm time, and called "unreadable" "wrong".

This module runs them all, hours before the arm, as one table:

    Basics        preflight.run_preflight (NINA API, camera, mount, safety
                  monitor, PHD2 + its audit, apps, sequence lint, disk...)
    Devices       armer.connect_all_rigs: every device on every rig, with
                  connect-and-retry (the one auto-fix this module does)
    Live readings frozen_sensor.read_rig: camera / focuser / weather report
                  values (a focuser with no temperature blinds the focus model)
    Mount         TheSky's own view (PS-252 mount_truth): connected, parked,
                  clock and LST skew
    ProTrack      protrack_recheck (read live, "not judged" while greyed)
    TPoint        tpoint_preflight.evaluate
    Settings      settings_ledger.take: the ~75 rules (coolers, filters,
                  PHD2, TheSky, NINA profiles), failing / unknown rows only
    Windows       PnP devices in error (COM ports, USB), pending reboot,
                  Windows Update active hours against tonight's window, pause
    Tonight       forecast rating, auto-arm on, the arm window

Row: {id, section, label, status, current, want, fix, who}. status is
fail | warn | wait (not judged yet; the note says when) | unknown | pass |
info. who is auto (fixed by this run or by the system later), jeremy (needs
hands at the scope PC or the observatory) or claude (config / code).

Runs (run_loop): after the daily app start (app_lifecycle_start_local +
prearm_after_start_min) and prearm_before_arm_min before the auto-arm window
opens; on demand at POST /api/prearm/run. One Pushover, only for rows that
need Jeremy, and not repeated at the second run unless they changed or a
fail remains. Never touches Windows, TheSky or NINA settings; the auto-arm
gate is unchanged (still preflight).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

TICK_S = 120
SLOT_GRACE_H = 2.0          # a slot missed by more than this is skipped
SOURCE_TIMEOUT_S = 240.0
STATUS_RANK = {"fail": 0, "warn": 1, "wait": 2, "unknown": 3, "pass": 4, "info": 5}
SECTIONS = ("Basics", "Devices", "Live readings", "Mount", "ProTrack", "TPoint",
            "Settings", "Windows", "Tonight")
ATTENTION = ("fail", "warn")
PUSH_TITLE = "PhotonScript pre-arm"
# rig devices a night cannot run without (fail); the rest warn
CRITICAL = {"rc16": ("camera", "mount", "safetymonitor", "focuser", "filterwheel"),
            "piggyback": ("camera",)}
# Windows device classes the rigs hang off (a dead one = a dead device)
PNP_FAIL_CLASSES = ("Ports", "USB", "Camera", "Image", "libusb", "libusbK", "WinUSB")
PNP_IGNORE_CODES = (22,)    # disabled on purpose


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "prearm_enabled", True))


def row(section: str, label: str, status: str, current=None, want=None, fix=None,
        who: str = "jeremy", rid: str | None = None, note: str | None = None) -> dict:
    return {"id": rid or f"{section}:{label}".lower().replace(" ", "_"),
            "section": section, "label": label, "status": status,
            "current": None if current is None else str(current),
            "want": want, "fix": fix, "who": who, "note": note}


# ------------------------------------------------------------------ pure

def _in_window(h: float, start: float, end: float) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


def active_hours_cover(start_h, end_h, need_from_h: float, need_to_h: float) -> bool:
    """Windows Update active hours [start, end) (may wrap midnight) cover
    the need window [from, to) (may wrap)? Checked every 15 min."""
    if start_h is None or end_h is None:
        return False
    span = (need_to_h - need_from_h) % 24.0
    steps = int(span * 4)
    return all(_in_window((need_from_h + i / 4.0) % 24.0, float(start_h), float(end_h))
               for i in range(steps))


def suggest_active_hours(need_from_h: float, need_to_h: float) -> tuple[int, int]:
    """The widest legal window (Windows allows 18 h) around the need."""
    start = int(math.floor(need_from_h)) % 24
    end = int(math.ceil(need_to_h)) % 24
    if (end - start) % 24 > 18:
        end = (start + 18) % 24
    return start, end


def pnp_rows(devs: list[dict] | None) -> list[dict]:
    """Win32_PnPEntity entries with ConfigManagerErrorCode != 0 -> rows."""
    out = []
    for d in devs or []:
        code = d.get("ConfigManagerErrorCode")
        try:
            code = int(code)
        except (TypeError, ValueError):
            continue
        if code == 0 or code in PNP_IGNORE_CODES:
            continue
        cls = str(d.get("PNPClass") or "")
        name = str(d.get("Name") or d.get("DeviceID") or "?")
        st = "fail" if cls in PNP_FAIL_CLASSES else "warn"
        out.append(row("Windows", f"Device: {name}", st,
                       current=f"problem code {code} ({cls or 'no class'})", want="code 0 (working)",
                       fix=("Device Manager: right-click it, Disable device, then Enable device "
                            "(fixed COM3 on 11-13). If it comes back, power-cycle the USB hub "
                            "(power switch outlet 7)"),
                       rid=f"pnp:{d.get('DeviceID') or name}"))
    return out


def windows_rows(reg: dict, need_from_h: float | None, need_to_h: float | None) -> list[dict]:
    """reg: {"reboot_pending": [reasons], "active_start", "active_end",
    "paused": bool | None, "ok": bool, "note"}."""
    if not reg.get("ok"):
        return [row("Windows", "Windows Update state", "unknown",
                    current=reg.get("note") or "not readable", who="claude")]
    out = []
    pend = reg.get("reboot_pending") or []
    out.append(row("Windows", "Restart pending", "warn" if pend else "pass",
                   current=", ".join(pend) if pend else "none", want="none before the night",
                   fix=("Restart the scope PC now, by day (then reconnect the mount; check "
                        "COM ports), so Windows does not pick a time itself") if pend else None))
    a, b = reg.get("active_start"), reg.get("active_end")
    if need_from_h is None or need_to_h is None:
        out.append(row("Windows", "Active hours", "info", current=f"{a}:00 to {b}:00"))
    else:
        ok = active_hours_cover(a, b, need_from_h, need_to_h)
        s, e = suggest_active_hours(need_from_h, need_to_h)
        out.append(row("Windows", "Active hours cover the night", "pass" if ok else "warn",
                       current=f"{a}:00 to {b}:00" if a is not None else "not set",
                       want=f"cover {need_from_h:04.1f} to {need_to_h:04.1f} local (arm to app stop)",
                       fix=None if ok else (f"Settings > Windows Update > Advanced options > Active "
                                            f"hours > Manually: {s}:00 to {e}:00")))
    if reg.get("paused") is not None:
        out.append(row("Windows", "Updates paused", "info",
                       current="paused" if reg["paused"] else "not paused"))
    return out


def device_rows(results: dict | None) -> list[dict]:
    """armer.connect_all_rigs() -> rows."""
    out = []
    for rig, devs in (results or {}).items():
        if not isinstance(devs, dict):
            out.append(row("Devices", f"{rig}: connect", "fail", current=str(devs)))
            continue
        for dev, res in devs.items():
            txt = str(res)
            if txt == "connected":
                st = "pass"
            elif txt.startswith("skipped"):
                st = "info"
            else:
                st = "fail" if dev in CRITICAL.get(rig, ()) else "warn"
            nina = "NINA #1" if rig == "rc16" else "NINA #2"
            out.append(row("Devices", f"{rig} {dev}", st, current=txt, want="connected",
                           fix=None if st in ("pass", "info") else
                           (f"Auto-connect tried twice. In {nina}: Equipment > {dev}, connect "
                            "by hand; check its cable / power (USB hub outlet 7)"),
                           rid=f"dev:{rig}:{dev}"))
    return out


def ledger_rows(rules: dict | None) -> list[dict]:
    """settings_ledger.take(...)["rules"] -> the rows worth showing."""
    out = []
    rows = (rules or {}).get("rows") or []
    for r in rows:
        st = str(r.get("status") or "")
        if st not in ("fail", "warn", "unknown"):
            continue
        if r.get("id") == "thesky_protrack":
            continue                       # the live ProTrack row covers it
        app = str(r.get("app") or "")
        out.append(row("Settings", str(r.get("label") or r.get("id")), st,
                       current=r.get("current"), want=r.get("desired"), fix=r.get("fix"),
                       who="claude" if app == "photonscript" else "jeremy",
                       rid=f"ledger:{r.get('id')}", note=r.get("note")))
    counts = (rules or {}).get("counts") or {}
    if counts:
        out.append(row("Settings", "Rules checked", "info",
                       current=", ".join(f"{k} {v}" for k, v in counts.items() if v),
                       rid="ledger:counts"))
    return out


def mount_rows(m: dict | None, der: dict | None) -> list[dict]:
    if not m:
        return [row("Mount", "TheSky mount read", "fail", current="TheSky not answering on TCP",
                    fix="Open TheSky on the scope PC (or connect the mount in NINA #1, which "
                        "launches it)")]
    out = []
    conn = m.get("connected")
    out.append(row("Mount", "Mount connected in TheSky", "pass" if conn else "fail",
                   current="connected" if conn else "not connected", want="connected",
                   fix=None if conn else
                   ("TheSky: Telescope > Connect. Error 201 = its COM port is gone: Device "
                    "Manager, disable / enable the port (see the Windows rows). 'Cannot "
                    "slew' after connect: Bisque TCS > Utilities > Reboot")))
    if conn:
        out.append(row("Mount", "Park state", "info",
                       current=("parked" if m.get("parked") else "not parked") +
                       (", tracking" if m.get("tracking") else ", not tracking")))
    for key, lim, label in (("clock_skew_s", 2.0, "TheSky clock vs PC (s)"),
                            ("lst_skew_s", 5.0, "TheSky LST vs computed (s)")):
        v = (der or {}).get(key)
        if v is None:
            continue
        ok = abs(v) <= lim
        out.append(row("Mount", label, "pass" if ok else "fail", current=f"{v:+.1f}",
                       want=f"within {lim:g}", fix=None if ok else
                       "Check TheSky's location / time zone / clock (Input > Location)"))
    return out


def protrack_row(rec: dict | None) -> dict:
    r = (rec or {}).get("row") or {}
    cur = str(r.get("current") or "")
    st = str(r.get("status") or "unknown")
    if cur.startswith("not judged"):
        st = "wait"
    return row("ProTrack", "Activate ProTrack + Enable tracking adjustments", st,
               current=cur or "not read", want="both ticked",
               fix=r.get("note") if st in ATTENTION else None,
               note="judged once the mount unparks and tracks" if st == "wait" else None,
               rid="protrack")


def tpoint_rows(rec: dict | None) -> list[dict]:
    if not rec:
        return [row("TPoint", "TPoint preflight", "unknown", current="not run", who="claude")]
    lvl = str(rec.get("level") or "unknown")
    st = {"ok": "pass", "warn": "warn", "block": "fail", "fail": "fail"}.get(lvl, "unknown")
    return [row("TPoint", "TPoint model", st, current=rec.get("line"), fix=rec.get("fix"),
                rid="tpoint")]


def basics_rows(pf: dict | None) -> list[dict]:
    out = []
    for c in (pf or {}).get("checks") or []:
        st = str(c.get("status") or "unknown")
        name = str(c.get("name") or "?")
        out.append(row("Basics", name, st, current=c.get("detail"),
                       who="auto" if name == "Observatory apps running" and st == "warn" else "jeremy",
                       rid=f"pf:{name}"))
    return out


def live_rows(reads: dict | None) -> list[dict]:
    out = []
    for rig, vals in (reads or {}).items():
        for sensor in ("camera", "focuser", "weather"):
            if sensor not in (vals or {}):
                continue
            if sensor == "weather" and rig != "rc16":
                continue
            v = vals.get(sensor)
            st = "pass" if v is not None else "warn"
            fix = None
            if v is None:
                fix = (f"{rig} {sensor} reports no value: reconnect it in NINA "
                       f"({'#1' if rig == 'rc16' else '#2'}); a focuser with no temperature "
                       "blinds the temperature focus model")
            out.append(row("Live readings", f"{rig} {sensor}", st,
                           current="reading" if v is not None else "no value",
                           fix=fix, rid=f"live:{rig}:{sensor}"))
    return out


def tonight_rows(fc_night: dict | None, auto_arm: bool, window_local: str | None) -> list[dict]:
    out = [row("Tonight", "Auto-arm", "pass" if auto_arm else "warn",
               current=("on" + (f", window opens {window_local}" if window_local else ""))
               if auto_arm else "off",
               fix=None if auto_arm else "Settings: PS_AUTO_ARM_ENABLED=true (or arm by hand)",
               rid="tonight:auto_arm")]
    if fc_night:
        out.append(row("Tonight", "Forecast", "info",
                       current=(f"{fc_night.get('rating')}: {fc_night.get('usable_hours')} of "
                                f"{fc_night.get('dark_hours')} dark h usable"),
                       rid="tonight:forecast"))
    return out


def counts(rows: list[dict]) -> dict:
    return {s: sum(1 for r in rows if r["status"] == s) for s in STATUS_RANK}


def attention(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["status"] in ATTENTION and r.get("who") != "auto"]


def push_text(rows: list[dict], url: str | None = None, cap: int = 8) -> str:
    need = sorted(attention(rows), key=lambda r: STATUS_RANK[r["status"]])
    lines = []
    for r in need[:cap]:
        line = f"{'FAIL' if r['status'] == 'fail' else 'warn'} {r['label']}: {r.get('current') or '?'}"
        if r.get("fix"):
            line += f". Fix: {r['fix']}"
        lines.append(line)
    if len(need) > cap:
        lines.append(f"+{len(need) - cap} more")
    if url:
        lines.append(url)
    return "\n".join(lines)


def should_push(rows: list[dict], trigger: str, pushed_today: list[str] | None) -> bool:
    need = attention(rows)
    if not need:
        return False
    ids = sorted(r["id"] for r in need)
    if pushed_today is None:
        return True
    if ids != sorted(pushed_today):
        return True
    return trigger == "before_arm" and any(r["status"] == "fail" for r in need)


# ------------------------------------------------------------------ times

def night_window(cfg, now: datetime) -> dict:
    """Local-day schedule (UTC naive): app start, the auto-arm window open,
    astro dusk / dawn, and the active-hours need window in local hours."""
    from photonscript.scheduler.app_lifecycle import start_time_utc
    from photonscript.scheduler import night_plan as np_
    from photonscript.shared.localtime import utc_offset_hours
    off = utc_offset_hours(cfg, now)
    local = now + timedelta(hours=off)
    out = {"app_start_utc": start_time_utc(cfg, now), "offset_h": off}
    try:
        tw = np_.compute_night_times(cfg.get_observatory(),
                                     datetime(local.year, local.month, local.day))
    except Exception as e:  # noqa: BLE001
        logger.debug("prearm: night times failed: %s", e)
        tw = {}
    dusk, dawn = tw.get("astro_dusk"), tw.get("astro_dawn")
    out["dusk_utc"], out["dawn_utc"] = dusk, dawn
    if dusk:
        cool = float(getattr(cfg, "cool_lead_minutes", 30) or 30)
        lead = float(getattr(cfg, "auto_arm_lead_hours", 3.0) or 3.0)
        out["arm_open_utc"] = dusk - timedelta(minutes=cool) - timedelta(hours=lead)
    if out.get("arm_open_utc") and dawn:
        stop = dawn + timedelta(minutes=float(getattr(cfg, "app_lifecycle_stop_after_shutdown_min",
                                                      30) or 30) + 45)
        out["need_from_h"] = ((out["arm_open_utc"] + timedelta(hours=off)).hour +
                              (out["arm_open_utc"] + timedelta(hours=off)).minute / 60.0)
        sl = stop + timedelta(hours=off)
        out["need_to_h"] = sl.hour + sl.minute / 60.0
    return out


def slot_times(cfg, win: dict) -> dict:
    out = {"after_start": win["app_start_utc"] +
           timedelta(minutes=float(getattr(cfg, "prearm_after_start_min", 30) or 30))}
    if win.get("arm_open_utc"):
        out["before_arm"] = win["arm_open_utc"] - timedelta(
            minutes=float(getattr(cfg, "prearm_before_arm_min", 60) or 60))
    return out


def due_slot(slots: dict, done: list[str], now: datetime) -> str | None:
    for name in ("after_start", "before_arm"):
        t = slots.get(name)
        if t and name not in done and t <= now < t + timedelta(hours=SLOT_GRACE_H):
            return name
    return None


# ------------------------------------------------------------------ io

def prearm_dir(cfg) -> Path:
    return Path(cfg.data_dir) / "prearm"


def day_path(cfg, day: str) -> Path:
    return prearm_dir(cfg) / f"{day}.json"


def load_day(cfg, day: str) -> dict:
    try:
        return json.loads(day_path(cfg, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"day": day, "runs": [], "done": [], "pushed": None}


def save(cfg, day: str, rep: dict, slot: str | None) -> None:
    d = load_day(cfg, day)
    d["runs"] = (d.get("runs") or [])[-9:] + [rep]
    if slot and slot not in d.get("done", []):
        d.setdefault("done", []).append(slot)
    if rep.get("pushed"):
        d["pushed"] = rep.get("attention_ids")
    try:
        prearm_dir(cfg).mkdir(parents=True, exist_ok=True)
        day_path(cfg, day).write_text(json.dumps(d, default=str), encoding="utf-8")
        (prearm_dir(cfg) / "latest.json").write_text(json.dumps(rep, default=str),
                                                     encoding="utf-8")
    except OSError as e:
        logger.warning("prearm not saved: %s", e)


def latest(cfg) -> dict | None:
    try:
        return json.loads((prearm_dir(cfg) / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_windows() -> dict:
    """Registry reads (no admin needed): pending reboot, active hours,
    pause. Never raises."""
    if sys.platform != "win32":
        return {"ok": False, "note": "not Windows"}
    try:
        import winreg
    except ImportError:
        return {"ok": False, "note": "winreg unavailable"}
    H = winreg.HKEY_LOCAL_MACHINE

    def exists(path):
        try:
            winreg.CloseKey(winreg.OpenKey(H, path))
            return True
        except OSError:
            return False

    def val(path, name):
        try:
            with winreg.OpenKey(H, path) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    pend = []
    if exists(r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"):
        pend.append("Windows Update")
    if exists(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
        pend.append("component servicing")
    ux = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    paused = None
    pq = val(r"SOFTWARE\Microsoft\WindowsUpdate\UpdatePolicy\Settings", "PausedQualityStatus")
    if pq is not None:
        paused = bool(pq)
    return {"ok": True, "reboot_pending": pend, "active_start": val(ux, "ActiveHoursStart"),
            "active_end": val(ux, "ActiveHoursEnd"), "paused": paused}


PNP_PS = ("Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode<>0' | "
          "Select-Object Name,PNPClass,ConfigManagerErrorCode,DeviceID | ConvertTo-Json -Compress")


def read_pnp(timeout: float = 45.0) -> tuple[list[dict] | None, str | None]:
    if sys.platform != "win32":
        return None, "not Windows"
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PNP_PS],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = (p.stdout or "").strip()
        if not txt:
            return [], None
        data = json.loads(txt)
        return (data if isinstance(data, list) else [data]), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


async def _guard(name: str, coro_or_fn, timeout: float = SOURCE_TIMEOUT_S):
    """Run one source; (result, error)."""
    try:
        if asyncio.iscoroutine(coro_or_fn):
            return await asyncio.wait_for(coro_or_fn, timeout), None
        return await asyncio.wait_for(asyncio.to_thread(coro_or_fn), timeout), None
    except Exception as e:  # noqa: BLE001
        logger.warning("prearm %s failed: %s", name, e)
        return None, f"{type(e).__name__}: {e}"


def _err_row(section: str, what: str, err: str) -> dict:
    return row(section, what, "unknown", current=f"check failed: {err}", who="claude")


async def run(cfg, armer=None, trigger: str = "manual", push: bool | None = None,
              connect: bool = True, now: datetime | None = None, notify=None) -> dict:
    """Run every check now. Returns the report (and saves it). Never raises."""
    from photonscript.shared.localtime import to_local
    now = now or datetime.utcnow()
    day = to_local(cfg, now).strftime("%Y-%m-%d")
    state = str(getattr(armer, "state", "") or "")
    rows: list[dict] = []
    win = {}
    try:
        win = night_window(cfg, now)
    except Exception as e:  # noqa: BLE001
        logger.warning("prearm window failed: %s", e)

    # Devices first: the connect is the auto-fix, so later reads see it
    if connect and armer is not None:
        res, err = await _guard("devices", armer.connect_all_rigs())
        rows += device_rows(res) if err is None else [_err_row("Devices", "Connect all", err)]

    from photonscript.scheduler.preflight import run_preflight
    pf, err = await _guard("preflight", run_preflight(cfg, test_push=False))
    rows += basics_rows(pf) if err is None else [_err_row("Basics", "Preflight", err)]

    from photonscript.scheduler import frozen_sensor as fs
    from photonscript.shared.rigs import rig_ids
    reads = {}
    for rig in rig_ids(cfg):
        r, err = await _guard(f"live {rig}", fs.read_rig(cfg, rig), 60)
        if err is None:
            reads[rig] = r
    rows += live_rows(reads)

    from photonscript.scheduler import mount_ledger as ml
    m, err = await _guard("mount", lambda: ml.read_thesky(cfg), 30)
    rows += mount_rows(m, ml.derive(cfg, m, datetime.utcnow()) if m else None)

    from photonscript.scheduler import protrack_recheck as pr
    rec, err = await _guard("protrack", lambda: pr.recheck(cfg, "prearm", armer_state=state,
                                                           persist=False), 60)
    rows.append(protrack_row(rec) if err is None else _err_row("ProTrack", "ProTrack", err))

    from photonscript.scheduler import tpoint_preflight as tpp
    guided = False
    try:
        guided = bool(armer._use_guiding()) if armer is not None else False
    except Exception:  # noqa: BLE001
        pass
    tr, err = await _guard("tpoint", lambda: tpp.evaluate(cfg, guided=guided), 60)
    rows += tpoint_rows(tr) if err is None else [_err_row("TPoint", "TPoint", err)]

    from photonscript.scheduler import settings_ledger as sl
    led, err = await _guard("ledger", sl.take(cfg, "prearm", armer_state=state, push=False))
    rows += ledger_rows((led or {}).get("rules")) if err is None else \
        [_err_row("Settings", "Settings ledger", err)]

    reg, err = await _guard("windows", read_windows, 20)
    rows += windows_rows(reg or {"ok": False, "note": err}, win.get("need_from_h"),
                         win.get("need_to_h"))
    pnp, perr = await asyncio.to_thread(read_pnp)
    if pnp is None:
        rows.append(row("Windows", "Device Manager errors", "unknown", current=perr, who="claude"))
    else:
        bad = pnp_rows(pnp)
        rows += bad or [row("Windows", "Device Manager errors", "pass", current="none",
                            rid="pnp:none")]

    fc_night = None
    try:
        from photonscript.scheduler.forecast import get_forecast
        fc, _ = await _guard("forecast", get_forecast(cfg), 60)
        nights = (fc or {}).get("nights") or []
        fc_night = next((n for n in nights if n.get("date") == day), None)
    except Exception:  # noqa: BLE001
        pass
    wl = None
    if win.get("arm_open_utc"):
        wl = (win["arm_open_utc"] + timedelta(hours=win.get("offset_h", 0))).strftime("%H:%M")
    rows += tonight_rows(fc_night, bool(getattr(cfg, "auto_arm_enabled", False)), wl)

    order = {s: i for i, s in enumerate(SECTIONS)}
    rows.sort(key=lambda r: (order.get(r["section"], 99), STATUS_RANK.get(r["status"], 9)))
    need = attention(rows)
    rep = {"ran_at": now.replace(microsecond=0).isoformat() + "Z", "day": day,
           "trigger": trigger, "armer_state": state, "counts": counts(rows),
           "go": not any(r["status"] == "fail" for r in rows),
           "attention_ids": sorted(r["id"] for r in need), "rows": rows, "pushed": False}
    d = load_day(cfg, day)
    do_push = bool(getattr(cfg, "prearm_push", True)) if push is None else bool(push)
    if do_push and should_push(rows, trigger, d.get("pushed")):
        base = str(getattr(cfg, "after_action_base_url", "") or "").rstrip("/")
        msg = push_text(rows, f"{base}/prearm" if base else None)
        try:
            if notify is None:
                from photonscript.shared.pushover import notify
            await notify(cfg, msg, title=f"{PUSH_TITLE}: {len(need)} need you", kind="act")
            rep["pushed"] = True
        except Exception as e:  # noqa: BLE001
            logger.warning("prearm push failed: %s", e)
    save(cfg, day, rep, trigger if trigger in ("after_start", "before_arm") else None)
    logger.info("prearm (%s): %s", trigger, rep["counts"])
    return rep


async def run_loop(get_config, get_armer, tick_s: int = TICK_S) -> None:
    """Background loop (app startup): the two daily slots."""
    from photonscript.shared.localtime import to_local
    while True:
        try:
            cfg = get_config()
            if enabled(cfg):
                now = datetime.utcnow()
                day = to_local(cfg, now).strftime("%Y-%m-%d")
                slot = due_slot(slot_times(cfg, night_window(cfg, now)),
                                load_day(cfg, day).get("done") or [], now)
                if slot:
                    await run(cfg, get_armer(), trigger=slot)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("prearm loop failed: %s", e)
        await asyncio.sleep(tick_s)


# ------------------------------------------------------------------ page

_COLORS = {"fail": "#c0392b", "warn": "#b9770e", "wait": "#2e6da4", "unknown": "#7f8c8d",
           "pass": "#2e8b57", "info": "#7f8c8d"}


def page_html(rep: dict | None) -> str:
    e = html.escape
    if not rep:
        body = "<p>No pre-arm check yet. Press Run now.</p>"
    else:
        c = rep.get("counts") or {}
        head = (f"<p><b>{e(rep.get('ran_at') or '')}</b> ({e(rep.get('trigger') or '')}), "
                f"armer {e(rep.get('armer_state') or '?')}: " +
                ", ".join(f"<span style='color:{_COLORS[k]}'>{v} {k}</span>"
                          for k, v in c.items() if v) + "</p>")
        trs, sec = [], None
        for r in rep.get("rows") or []:
            if r["section"] != sec:
                sec = r["section"]
                trs.append(f"<tr><th colspan=5 class=sec>{e(sec)}</th></tr>")
            col = _COLORS.get(r["status"], "#7f8c8d")
            fix = r.get("fix") or r.get("note") or ""
            trs.append(f"<tr><td><b style='color:{col}'>{e(r['status'])}</b></td>"
                       f"<td>{e(r['label'])}</td><td>{e(r.get('current') or '')}</td>"
                       f"<td>{e(str(r.get('want') or ''))}</td>"
                       f"<td>{e(str(fix))}{' <i>(' + e(r['who']) + ')</i>' if fix else ''}</td></tr>")
        body = head + ("<table><tr><th>status</th><th>check</th><th>now</th><th>want</th>"
                       "<th>fix (who)</th></tr>" + "".join(trs) + "</table>")
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Pre-arm check</title><style>"
            ":root{--bg:#fff;--fg:#1d2329;--line:#dde2e6;--sec:#f1f4f6}"
            "@media (prefers-color-scheme:dark){:root{--bg:#14181c;--fg:#e4e8eb;"
            "--line:#2c333a;--sec:#1d2329}}"
            "body{background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;"
            "margin:16px;max-width:1200px}table{border-collapse:collapse;width:100%}"
            "td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;"
            "vertical-align:top}th.sec{background:var(--sec);font-size:13px}"
            "button{font:inherit;padding:6px 14px}</style></head><body>"
            "<h2>Pre-arm check</h2><p><button onclick=\"this.disabled=true;this.textContent="
            "'Running (a few minutes)...';fetch('/api/prearm/run?push=0',{method:'POST'})"
            ".then(()=>location.reload())\">Run now</button></p>" + body + "</body></html>")
