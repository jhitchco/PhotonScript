"""PS-256 COM heal: restart the mount's serial port when Windows marks it bad.

11-13: after a Windows Update reboot (KB5132061) the Paramount MX's port
(COM3, usbser) came up in Device Manager with problem code 10 and TheSky's
mount connect failed with Error 201. The fix each time was Jeremy disabling
and re-enabling COM3 in Device Manager. The PhotonScript service runs
without admin, so it cannot restart a device itself.

How it heals without admin: Jeremy runs deploy\\register-com-heal.ps1 ONCE
from an elevated PowerShell. It registers the scheduled task
"PhotonScript COM heal" (runs as SYSTEM, highest privileges) whose only
action is `pnputil /restart-device "<instance id>"`, with the instance id
baked in at registration (no parameters: it cannot be pointed at anything
else), and grants the PhotonScript user read + run on that task. This
module only STARTS it (`schtasks /run /tn ...`) and never registers,
changes or deletes it.

    detect   port_problems(devs, port): a Ports-class device whose name holds
             the port (COM3, not COM30) with a problem code (code 22,
             disabled on purpose, is ignored like prearm.PNP_IGNORE_CODES).
             Pure, over prearm.read_pnp() output. detect() also takes an
             optional connect error ("Error 201") for callers that have one.
    gate     never while a night runs: only when the armer is DISARMED or
             COMPLETE, or ARMED before pre-config, and no NINA sequence is
             running.
    heal     schtasks /run, wait SETTLE_S, re-read PnP: healed = the port has
             no problem code. Then TheSky reconnects the mount WITHOUT
             unparking (sky6RASCOMTele.ConnectAndDoNotUnpark, RECONNECT_JS;
             the only TheSky write here).
    limit    com_heal_max_per_day attempts per local day, kept in
             data_dir/com_heal/<day>.json (each attempt, result, times).
    page     after a failed heal, the limit, or the task not registered: ONE
             priority 1 Pushover per day with the manual fix.

Hooks: prearm.run (after the Windows PnP read) and GET /api/com-heal,
POST /api/com-heal/run?dry_run=1 (routers/com_heal.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SETTLE_S = 20.0
CMD_TIMEOUT_S = 30.0
IDLE_STATES = ("DISARMED", "COMPLETE")
PNP_IGNORE_CODES = (22,)       # kept equal to prearm.PNP_IGNORE_CODES (a test checks)
REGISTER_SCRIPT = r"deploy\register-com-heal.ps1"
NOT_REGISTERED = f"not registered: Jeremy runs {REGISTER_SCRIPT} once as admin"
PUSH_TITLE = "PhotonScript: mount COM port needs you"
# The only TheSky write in this module: connect the mount, never unpark.
RECONNECT_JS = ("var Out; sky6RASCOMTele.ConnectAndDoNotUnpark(); "
                "var c = false; try { c = sky6RASCOMTele.IsConnected; } catch (e) {} "
                "var p = ''; try { p = c ? sky6RASCOMTele.IsParked() : ''; } catch (e) {} "
                "Out = 'connected=' + c + ';parked=' + p;")


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "com_heal_enabled", True))


def port(cfg) -> str:
    return str(getattr(cfg, "com_heal_port", "COM3") or "COM3").strip().upper()


def task_name(cfg) -> str:
    return str(getattr(cfg, "com_heal_task_name", "PhotonScript COM heal")
               or "PhotonScript COM heal")


def max_per_day(cfg) -> int:
    try:
        return max(0, int(getattr(cfg, "com_heal_max_per_day", 2)))
    except (TypeError, ValueError):
        return 2


def manual_fix(p: str) -> str:
    return (f"Device Manager > Ports (COM & LPT) > the {p} device: right-click, Disable "
            f"device, then right-click, Enable device. Then TheSky: Telescope > Connect. "
            f"If it comes back, power-cycle the USB hub (power switch outlet 7)")


# ------------------------------------------------------------------ pure

def port_matches(name: str, p: str) -> bool:
    """'USB Serial Device (COM3)' holds COM3; 'COM30' does not."""
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(p)}(?![0-9])", str(name or ""),
                          re.IGNORECASE))


def port_problems(devs: list[dict] | None, p: str) -> list[dict]:
    """The Ports-class entries for port p with a problem code (not 0, not
    an ignored code). devs: prearm.read_pnp() output."""
    out = []
    for d in devs or []:
        if str(d.get("PNPClass") or "") != "Ports":
            continue
        try:
            code = int(d.get("ConfigManagerErrorCode"))
        except (TypeError, ValueError):
            continue
        if code == 0 or code in PNP_IGNORE_CODES:
            continue
        if port_matches(str(d.get("Name") or ""), p):
            out.append({"name": d.get("Name"), "code": code, "device_id": d.get("DeviceID")})
    return out


def is_error_201(err) -> bool:
    return bool(re.search(r"error\s*(?:=\s*)?201\b", str(err or ""), re.IGNORECASE))


def detect(devs: list[dict] | None, p: str, connect_error=None) -> dict:
    """{"unhealthy", "problems", "error_201"}: unhealthy when the port has
    a problem code, or (optional input) TheSky's connect said Error 201."""
    probs = port_problems(devs, p)
    e201 = is_error_201(connect_error)
    return {"unhealthy": bool(probs) or e201, "problems": probs, "error_201": e201}


# ------------------------------------------------------------------ io

def heal_dir(cfg) -> Path:
    return Path(cfg.data_dir) / "com_heal"


def day_path(cfg, day: str) -> Path:
    return heal_dir(cfg) / f"{day}.json"


def local_day(cfg, now: datetime) -> str:
    from photonscript.shared.localtime import to_local
    return to_local(cfg, now).strftime("%Y-%m-%d")


def load_day(cfg, day: str) -> dict:
    try:
        d = json.loads(day_path(cfg, day).read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("attempts", [])
            d.setdefault("paged", None)
            return d
    except (OSError, ValueError):
        pass
    return {"day": day, "attempts": [], "paged": None}


def save_day(cfg, day: str, d: dict) -> None:
    try:
        heal_dir(cfg).mkdir(parents=True, exist_ok=True)
        day_path(cfg, day).write_text(json.dumps(d, default=str, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning("com heal log not saved: %s", e)


def run_cmd(args: list[str], timeout: float = CMD_TIMEOUT_S) -> tuple[int | None, str]:
    """(return code, stdout + stderr); (None, why) when it could not run."""
    if sys.platform != "win32":
        return None, "not Windows"
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def task_registered(cfg, runner=run_cmd) -> tuple[bool | None, str]:
    """schtasks /query: True, False (no such task, or no right to see it),
    None (could not ask)."""
    rc, out = runner(["schtasks", "/query", "/tn", task_name(cfg)])
    if rc is None:
        return None, out
    return rc == 0, out.splitlines()[-1].strip() if out else ""


def reconnect_mount(cfg) -> dict:
    """TheSky: connect the mount WITHOUT unparking (RECONNECT_JS)."""
    from photonscript.telescope_agent.thesky_client import (TheSkyClient, TheSkyError,
                                                            _truthy, parse_kv)
    cl = TheSkyClient(getattr(cfg, "thesky_tcp_host", "localhost"),
                      int(getattr(cfg, "thesky_tcp_port", 3040) or 3040), timeout=30.0)
    try:
        kv = parse_kv(cl.run_script(RECONNECT_JS))
    except TheSkyError as e:
        return {"ok": False, "detail": str(e)}
    conn = _truthy(kv.get("connected"))
    return {"ok": conn, "parked": kv.get("parked"),
            "detail": "connected (not unparked)" if conn else "TheSky says not connected"}


async def night_block(cfg, armer, now: datetime, seq_fn=None) -> str | None:
    """Why a heal must not run now (a night is running), or None."""
    state = str(getattr(armer, "state", "") or "DISARMED") if armer is not None else "DISARMED"
    if state == "ARMED":
        pc = (getattr(armer, "plan", None) or {}).get("preconfig_utc")
        try:
            pc_dt = datetime.fromisoformat(str(pc).rstrip("Z"))
        except (TypeError, ValueError):
            return "armer ARMED, pre-config time unknown"
        if now >= pc_dt:
            return f"armer ARMED and past pre-config ({pc})"
    elif state not in IDLE_STATES:
        return f"armer {state} (a night is running)"
    if seq_fn is None:
        from photonscript.scheduler.app_lifecycle import _sequence_running as seq_fn
    try:
        seq = await seq_fn(cfg)
    except Exception as e:  # noqa: BLE001
        logger.debug("com heal: sequence read failed: %s", e)
        seq = {}
    busy = [f"{k} ({', '.join(str(x) for x in v[:2])})" for k, v in (seq or {}).items() if v]
    if busy:
        return "NINA sequence running: " + "; ".join(busy)
    return None


# ------------------------------------------------------------------ heal

async def _page_once(cfg, day: str, d: dict, why: str, notify, now: datetime) -> bool:
    if d.get("paged"):
        return False
    p = port(cfg)
    msg = f"{p} (mount serial port) is not working: {why}. Fix by hand: {manual_fix(p)}"
    try:
        if notify is None:
            from photonscript.shared.pushover import notify
        await notify(cfg, msg, title=PUSH_TITLE, priority=1, kind="act")
    except Exception as e:  # noqa: BLE001
        logger.warning("com heal page failed: %s", e)
        return False
    d["paged"] = {"at": now.replace(microsecond=0).isoformat() + "Z", "why": why}
    save_day(cfg, day, d)
    return True


async def heal(cfg, armer=None, *, trigger: str = "manual", now: datetime | None = None,
               dry_run: bool = False, devs: list[dict] | None = None, connect_error=None,
               runner=run_cmd, pnp_fn=None, reconnect_fn=None, seq_fn=None, notify=None,
               sleep=asyncio.sleep, settle_s: float = SETTLE_S) -> dict:
    """Detect, gate, run the task, verify, reconnect, log, page. Never raises.

    status: disabled | unknown (PnP unreadable) | healthy | refused (night
    running) | limit | not_registered | would_heal (dry run) | healed |
    failed. devs: a PnP read the caller already has (else read here).
    """
    now = now or datetime.utcnow()
    p = port(cfg)
    res: dict = {"port": p, "trigger": trigger, "dry_run": bool(dry_run),
                 "at": now.replace(microsecond=0).isoformat() + "Z"}
    if not enabled(cfg):
        return {**res, "status": "disabled", "detail": "com_heal_enabled is off"}
    if pnp_fn is None:
        from photonscript.scheduler.prearm import read_pnp as pnp_fn
    if devs is None:
        devs, perr = await asyncio.to_thread(pnp_fn)
        if devs is None:
            return {**res, "status": "unknown", "detail": f"Device Manager not readable: {perr}"}
    det = detect(devs, p, connect_error)
    res["before"] = det["problems"]
    if not det["unhealthy"]:
        return {**res, "status": "healthy", "detail": f"{p} has no problem code"}

    block = await night_block(cfg, armer, now, seq_fn)
    if block:
        return {**res, "status": "refused", "detail": f"not healing while a night runs: {block}",
                "fix": manual_fix(p)}

    day = local_day(cfg, now)
    d = load_day(cfg, day)
    lim = max_per_day(cfg)
    if len(d["attempts"]) >= lim:
        why = f"{len(d['attempts'])} auto heal attempts today (limit {lim})"
        out = {**res, "status": "limit", "detail": why, "fix": manual_fix(p)}
        if not dry_run:
            out["paged"] = await _page_once(cfg, day, d, why, notify, now)
        return out

    reg, qtxt = await asyncio.to_thread(task_registered, cfg, runner)
    if not reg:
        why = NOT_REGISTERED if reg is False else f"task state unknown ({qtxt})"
        out = {**res, "status": "not_registered", "detail": why, "fix": manual_fix(p)}
        if not dry_run and reg is False:
            out["paged"] = await _page_once(cfg, day, d, why, notify, now)
        return out

    if dry_run:
        return {**res, "status": "would_heal", "attempts_today": len(d["attempts"]),
                "detail": f"would run schtasks /run /tn \"{task_name(cfg)}\", wait "
                          f"{settle_s:g} s, re-read Device Manager, then reconnect the mount "
                          "in TheSky without unparking"}

    att = {"started": now.replace(microsecond=0).isoformat() + "Z", "trigger": trigger,
           "before": det["problems"], "error_201": det["error_201"]}
    rc, rtxt = await asyncio.to_thread(runner, ["schtasks", "/run", "/tn", task_name(cfg)])
    att["run_rc"], att["run_out"] = rc, rtxt
    healed = False
    after = None
    if rc == 0:
        await sleep(settle_s)
        after, aerr = await asyncio.to_thread(pnp_fn)
        if after is None:
            att["after_error"] = aerr
        else:
            att["after"] = port_problems(after, p)
            healed = not att["after"]
    reconnect = None
    if healed:
        try:
            reconnect = await asyncio.to_thread(reconnect_fn or reconnect_mount, cfg)
        except Exception as e:  # noqa: BLE001
            reconnect = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
        att["reconnect"] = reconnect
        if not reconnect.get("ok") and is_error_201(reconnect.get("detail")):
            healed = False          # the port reads clean but TheSky still cannot open it
    att["healed"] = healed
    att["finished"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    d["attempts"].append(att)
    save_day(cfg, day, d)

    out = {**res, "attempts_today": len(d["attempts"]), "after": att.get("after"),
           "devices_after": after, "reconnect": reconnect}
    if healed:
        rtxt2 = (reconnect or {}).get("detail") or "not tried"
        return {**out, "status": "healed",
                "detail": f"{p} restarted by the task and now has no problem code; "
                          f"TheSky mount: {rtxt2}"}
    if rc != 0:
        why = f"schtasks /run failed ({rc}: {rtxt})"
    elif after is None:
        why = f"Device Manager not readable after the restart ({att.get('after_error')})"
    elif att.get("after"):
        why = f"{p} still has problem code {att['after'][0]['code']} after the restart"
    else:
        why = f"{p} reads clean but TheSky still reports Error 201"
    out = {**out, "status": "failed", "detail": why, "fix": manual_fix(p)}
    out["paged"] = await _page_once(cfg, day, d, why, notify, now)
    return out


def result_row(res: dict | None) -> dict:
    """The prearm Windows-section row for a heal result."""
    from photonscript.scheduler.prearm import row
    res = res or {}
    st = res.get("status")
    p = res.get("port") or "COM3"
    status = {"healed": "pass", "healthy": "pass", "would_heal": "info",
              "disabled": "info"}.get(st, "fail")
    return row("Windows", f"COM heal ({p})", status, current=res.get("detail") or st,
               want=f"{p} working, mount connected", who="auto" if status == "pass" else "jeremy",
               fix=None if status in ("pass", "info") else res.get("fix"),
               rid="com_heal")


async def status(cfg, now: datetime | None = None, runner=run_cmd, pnp_fn=None) -> dict:
    """GET /api/com-heal: today's attempts, the task, the port."""
    now = now or datetime.utcnow()
    day = local_day(cfg, now)
    if pnp_fn is None:
        from photonscript.scheduler.prearm import read_pnp as pnp_fn
    reg, qtxt = await asyncio.to_thread(task_registered, cfg, runner)
    devs, perr = await asyncio.to_thread(pnp_fn)
    p = port(cfg)
    if devs is None:
        pstat = {"ok": None, "detail": f"Device Manager not readable: {perr}"}
    else:
        probs = port_problems(devs, p)
        pstat = {"ok": not probs, "problems": probs,
                 "detail": "no problem code" if not probs else
                 f"problem code {probs[0]['code']} ({probs[0]['name']})"}
    d = load_day(cfg, day)
    return {"enabled": enabled(cfg), "port": p, "port_status": pstat,
            "task": {"name": task_name(cfg), "registered": reg, "detail": qtxt,
                     "hint": None if reg else NOT_REGISTERED},
            "day": day, "attempts": d["attempts"], "max_per_day": max_per_day(cfg),
            "paged": d.get("paged")}
