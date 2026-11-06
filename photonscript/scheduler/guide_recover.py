"""PS-167 recovery playbook: the mount driver refuses PHD2's guide pulses.

Operator-run only: `photonscript guide-recover --dry-run` shows what it
would do; without --dry-run (and after a typed confirmation, or --yes) it
does it. Nothing in the service or the agents calls execute(); the agent
only pages (telescope_agent.pulse_refusal_watch).

What it looks at (all reads):
  1. PHD2's debug log (the newest, its last TAIL_BYTES): refused pulses, the
     failing driver members and when PHD2 last connected the mount
     (shared.pulse_refusal).
  2. TheSky over TCP 3040: thesky_client.READ_ONLY_JS["slew_state"] twice,
     SAMPLE_GAP_S apart: connected, IsSlewComplete, tracking, RA / Dec. A
     slew that TheSky still reports while RA / Dec stand still is "stuck".
  3. PHD2 JSON-RPC: get_app_state, get_connected, get_current_equipment.

What it would do, in order (each step only when the readings call for it):
  abort_slew     TheSky reports a slew but the mount is not moving: send
                 sky6RASCOMTele.Abort() (ABORT_JS, the only TheSky write in
                 this module; never when the mount is really moving).
  stop_capture   PHD2 is guiding / calibrating / looping: stop it (needed
                 before PHD2 lets go of the mount).
  disconnect     PHD2 set_connected false (PHD2 drops its gear, including
                 its copy of the TheSky driver).
  connect        PHD2 set_connected true (a fresh driver instance attaches
                 to the TheSky that is running).
  verify         get_connected true, and PHD2's debug log after the
                 reconnect: "Connect success" and no new SideOfPier /
                 pulse failure.
Blocked (nothing done) when TheSky does not answer, TheSky's mount is not
connected (connecting may unpark: the operator does that), the mount is
really slewing, or PHD2 does not answer. When the log shows no refusal since
PHD2 last connected the mount there is nothing to recover (--force reconnects
anyway). NINA restarts guiding at its next StartGuiding.
"""
from __future__ import annotations

import json
import logging
import math
import socket
import time
from datetime import datetime
from pathlib import Path

from photonscript.shared import pulse_refusal as pr

logger = logging.getLogger(__name__)

SAMPLE_GAP_S = 3.0
MOVE_ARCSEC = 120.0          # RA / Dec change over the gap that means "moving"
TAIL_BYTES = 4 * 1024 * 1024
STOP_WAIT_S = 15.0
VERIFY_WAIT_S = 8.0
ACTIVE_STATES = ("Guiding", "Calibrating", "Looping", "LostLock", "Paused",
                 "Selected")

# The only TheSky write this module can send (execute(), operator-run, and
# only for a slew TheSky reports while the mount stands still).
ABORT_JS = "var Out; sky6RASCOMTele.Abort(); Out = 'aborted';"


class Phd2RpcError(RuntimeError):
    pass


class Phd2Rpc:
    """A small blocking PHD2 JSON-RPC client for the CLI (the agent's async
    client lives in its own event loop). PHD2 interleaves events with
    replies; replies are matched by id."""

    def __init__(self, host: str = "localhost", port: int = 4400,
                 timeout: float = 10.0):
        self.host, self.port, self.timeout = host, int(port), timeout
        self._sock = None
        self._buf = b""
        self._id = 0

    def __enter__(self):
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)
        return self

    def __exit__(self, *exc):
        try:
            if self._sock:
                self._sock.close()
        finally:
            self._sock = None

    def _line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise Phd2RpcError("PHD2 closed the connection")
            self._buf += chunk
        line, _sep, self._buf = self._buf.partition(b"\n")
        return line

    def call(self, method: str, params: list | None = None):
        self._id += 1
        rid = self._id
        msg = {"method": method, "id": rid}
        if params is not None:
            msg["params"] = params
        self._sock.sendall((json.dumps(msg) + "\r\n").encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                obj = json.loads(self._line().decode("utf-8", "replace").strip() or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("id") == rid and "Event" not in obj:
                if obj.get("error") is not None:
                    err = obj["error"]
                    raise Phd2RpcError(err.get("message") if isinstance(err, dict)
                                       else str(err))
                return obj.get("result")
        raise Phd2RpcError(f"no reply to {method}")


# --------------------------------------------------------------------------
# readings
# --------------------------------------------------------------------------

def debug_summary(config, tail_bytes: int = TAIL_BYTES) -> dict:
    """pulse_refusal summary of the newest PHD2 debug log's tail."""
    from photonscript.scheduler.phd2_logs import find_logs
    files = find_logs(config, "debug").get("files") or []
    if not files:
        return {"ok": False, "note": "no PHD2 debug log found"}
    p = Path(files[-1])
    size = p.stat().st_size
    with open(p, "rb") as fh:
        start = max(0, size - tail_bytes)
        fh.seek(start)
        data = fh.read()
    text = data.decode("utf-8", "replace")
    if start:
        text = text.split("\n", 1)[1] if "\n" in text else ""
        sc = pr.Scanner(None, p.name, day=datetime.now())
    else:
        sc = pr.Scanner(pr.file_start(p.name), p.name)
    s = sc.feed_text(text).summary()
    s.update(ok=True, file=p.name, path=str(p), size=size, partial=bool(start))
    return s


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _truthy(v) -> bool | None:
    if v is None or str(v).strip() == "":
        return None
    return str(v).strip().lower() in ("1", "true", "yes", "on", "-1")


def sep_arcsec(a: dict, b: dict) -> float | None:
    """Angular change between two slew_state reads (RA hours, Dec degrees)."""
    ra1, d1, ra2, d2 = (_f(a.get("ra_h")), _f(a.get("dec_d")),
                        _f(b.get("ra_h")), _f(b.get("dec_d")))
    if None in (ra1, d1, ra2, d2):
        return None
    dra = (ra2 - ra1) * 15.0 * math.cos(math.radians((d1 + d2) / 2))
    return math.hypot(dra, d2 - d1) * 3600.0


def thesky_state(thesky, sleep=time.sleep, gap_s: float = SAMPLE_GAP_S) -> dict:
    """Two slew_state reads gap_s apart. ok False when TheSky does not answer."""
    try:
        a = thesky.slew_state()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "note": f"TheSky not answering: {e}"}
    out = {"ok": True, "connected": _truthy(a.get("connected")),
           "slew_complete": _truthy(a.get("slew_complete")),
           "tracking": _truthy(a.get("tracking")), "parked": _truthy(a.get("parked")),
           "ra_h": _f(a.get("ra_h")), "dec_d": _f(a.get("dec_d")),
           "last_slew_error": a.get("last_slew_error"), "moved_arcsec": None}
    if out["connected"] and out["slew_complete"] is False:
        sleep(gap_s)
        try:
            b = thesky.slew_state()
            out["moved_arcsec"] = sep_arcsec(a, b)
            out["slew_complete_after"] = _truthy(b.get("slew_complete"))
        except Exception as e:  # noqa: BLE001
            out["note"] = f"second TheSky read failed: {e}"
    return out


def phd2_state(phd2) -> dict:
    try:
        st = phd2.call("get_app_state")
        conn = phd2.call("get_connected")
        try:
            eq = phd2.call("get_current_equipment")
        except Exception:  # noqa: BLE001
            eq = None
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "note": f"PHD2 not answering: {e}"}
    mount = (eq or {}).get("mount") if isinstance(eq, dict) else None
    return {"ok": True, "app_state": st, "connected": bool(conn),
            "mount": (mount or {}).get("name"),
            "mount_connected": (mount or {}).get("connected")}


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------

def _still_failing(s: dict) -> bool:
    """Refusals after PHD2's last mount connect (or no connect in view)."""
    if not s.get("refusals"):
        return False
    last, conn = s.get("last_error_at"), s.get("mount_connected_at")
    return not conn or (last or "") >= conn


def plan(state: dict, force: bool = False) -> dict:
    """{"verdict", "blocked", "steps": [{id, what, why}], "diagnosis"}. Pure."""
    dbg, sky, ph = state.get("debug") or {}, state.get("thesky") or {}, state.get("phd2") or {}
    steps: list[dict] = []
    diag = pr.diagnose(dbg) if dbg.get("refusals") else None

    def blocked(why):
        return {"verdict": "blocked", "blocked": why, "steps": [], "diagnosis": diag}

    if not sky.get("ok"):
        return blocked((sky.get("note") or "TheSky not answering")
                       + ": start TheSky64 with its TCP server on (port 3040), then rerun.")
    if not sky.get("connected"):
        return blocked("TheSky's mount is not connected: connect the Paramount in "
                       "TheSky64 yourself (Telescope > Connect; connecting can unpark, so "
                       "guide-recover never does it), then rerun.")
    if sky.get("slew_complete") is False:
        moved = sky.get("moved_arcsec")
        if moved is None:
            return blocked("TheSky reports a slew and the second position read failed: "
                           "check TheSky by eye, then rerun.")
        if moved >= MOVE_ARCSEC or sky.get("slew_complete_after") is True:
            return blocked(f"TheSky is really slewing (moved {moved:.0f} arcsec in "
                           f"{SAMPLE_GAP_S:g} s): wait for it to finish, then rerun.")
        steps.append({"id": "abort_slew", "what": "TheSky: sky6RASCOMTele.Abort()",
                      "why": f"TheSky reports a slew but the mount moved {moved:.0f} "
                             "arcsec: a stuck slew state blocks every pulse"})
    if not ph.get("ok"):
        return {"verdict": "blocked", "blocked": (ph.get("note") or "PHD2 not answering")
                + ": start PHD2 (server on), then rerun.", "steps": steps,
                "diagnosis": diag}
    failing = _still_failing(dbg)
    if not failing and not steps and not force:
        why = ("no refused pulse in PHD2's debug log" if not dbg.get("refusals") else
               f"no refused pulse since PHD2 reconnected the mount "
               f"({dbg.get('mount_connected_at')})")
        return {"verdict": "nothing to recover", "blocked": None, "steps": [],
                "diagnosis": diag, "note": why + " (--force reconnects anyway)"}
    if ph.get("app_state") in ACTIVE_STATES:
        steps.append({"id": "stop_capture", "what": "PHD2: stop_capture",
                      "why": f"PHD2 is {ph.get('app_state')}; it lets go of the mount "
                             "only when stopped"})
    if ph.get("connected"):
        steps.append({"id": "disconnect", "what": "PHD2: set_connected false",
                      "why": "drop PHD2's copy of the TheSky driver"
                             + (f" (connected {dbg.get('mount_connected_at')})"
                                if dbg.get("mount_connected_at") else "")})
    steps.append({"id": "connect", "what": "PHD2: set_connected true",
                  "why": "a fresh driver instance attaches to the running TheSky"})
    steps.append({"id": "verify", "what": "PHD2: get_connected; debug log after",
                  "why": "expect 'Connect success' and no new SideOfPier / "
                         "PulseGuide failure"})
    return {"verdict": "recover", "blocked": None, "steps": steps, "diagnosis": diag}


# --------------------------------------------------------------------------
# execution (operator only)
# --------------------------------------------------------------------------

def execute(steps: list[dict], *, phd2, thesky, log_tail=None,
            sleep=time.sleep, say=print) -> list[dict]:
    """Run the planned steps in order; stop at the first failure. log_tail()
    returns PHD2 debug-log lines written since it was created (for verify)."""
    out = []
    for st in steps:
        sid = st["id"]
        try:
            if sid == "abort_slew":
                res = thesky.run_script(ABORT_JS)
                after = thesky.slew_state()
                ok = _truthy(after.get("slew_complete")) is not False
                note = (f"{res}; IsSlewComplete now {after.get('slew_complete')}, "
                        f"tracking {after.get('tracking')} (turn tracking back on in "
                        "TheSky if Abort stopped it)")
            elif sid == "stop_capture":
                phd2.call("stop_capture")
                t0, state = time.monotonic(), None
                while time.monotonic() - t0 < STOP_WAIT_S:
                    state = phd2.call("get_app_state")
                    if state == "Stopped":
                        break
                    sleep(0.5)
                ok, note = state == "Stopped", f"PHD2 {state}"
            elif sid == "disconnect":
                phd2.call("set_connected", [False])
                ok = not phd2.call("get_connected")
                note = "disconnected" if ok else "PHD2 still connected"
            elif sid == "connect":
                phd2.call("set_connected", [True])
                ok = bool(phd2.call("get_connected"))
                note = "connected" if ok else "PHD2 did not connect"
            elif sid == "verify":
                sleep(VERIFY_WAIT_S)
                ok = bool(phd2.call("get_connected"))
                note = "PHD2 connected" if ok else "PHD2 not connected"
                if log_tail is not None:
                    s = pr.Scanner(None, "", day=datetime.now())
                    for ln in log_tail():
                        s.feed(ln)
                    v = s.summary()
                    bad = v["side_of_pier_errors"] or v["refusals"] or v["slewing_errors"]
                    note += (f"; debug log since: connect {'seen' if v['mount_connected_at'] else 'not seen'}, "
                             f"{v['side_of_pier_errors']} SideOfPier / {v['refusals']} pulse failures")
                    if bad:
                        ok = False
                        note += (" -> still failing: check the TheSky audit row 'One "
                                 "TheSky running' and the driver setup (TheSky64)")
            else:
                ok, note = False, f"unknown step {sid}"
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"{type(e).__name__}: {e}"
        out.append({"id": sid, "ok": ok, "note": note})
        say(f"  {'OK ' if ok else 'FAIL'} {st['what']}: {note}")
        if not ok:
            break
    return out


def format_plan(state: dict, p: dict, dry_run: bool) -> str:
    dbg, sky, ph = state.get("debug") or {}, state.get("thesky") or {}, state.get("phd2") or {}
    lines = ["guide-recover (PS-167)" + (" DRY RUN: nothing will be changed" if dry_run else "")]
    if dbg.get("ok"):
        lines.append(f"PHD2 debug log {dbg.get('file')}: {dbg.get('refusals', 0)} refused "
                     f"pulse(s), members {', '.join(dbg.get('members') or []) or '-'}, "
                     f"last {dbg.get('last_error_at') or '-'}; mount connected "
                     f"{dbg.get('mount_connected_at') or 'before this log'}")
        if dbg.get("first_error"):
            lines.append(f"  first error: {dbg['first_error']}")
        if dbg.get("silenced"):
            lines.append(f"  silenced in PHD2: {', '.join(dbg['silenced'])}")
    else:
        lines.append(f"PHD2 debug log: {dbg.get('note') or 'not read'}")
    if sky.get("ok"):
        lines.append(f"TheSky: connected {sky.get('connected')}, slew complete "
                     f"{sky.get('slew_complete')}, tracking {sky.get('tracking')}, "
                     f"parked {sky.get('parked')}"
                     + (f", moved {sky['moved_arcsec']:.0f} arcsec in {SAMPLE_GAP_S:g} s"
                        if sky.get("moved_arcsec") is not None else ""))
    else:
        lines.append(f"TheSky: {sky.get('note')}")
    lines.append(f"PHD2: {ph.get('app_state')}, connected {ph.get('connected')}, mount "
                 f"{ph.get('mount')}" if ph.get("ok") else f"PHD2: {ph.get('note')}")
    if p.get("diagnosis"):
        lines.append(f"Diagnosis: {p['diagnosis']['title']}. {p['diagnosis']['detail']}")
    lines.append(f"Verdict: {p['verdict']}"
                 + (f" ({p['blocked']})" if p.get("blocked") else "")
                 + (f" ({p['note']})" if p.get("note") else ""))
    for i, st in enumerate(p.get("steps") or [], 1):
        lines.append(f"  {i}. {'would ' if dry_run else ''}{st['what']}: {st['why']}")
    if p.get("steps") and dry_run:
        lines.append("Run without --dry-run on the scope PC to do these steps "
                     "(you will be asked to confirm). NINA restarts guiding at its "
                     "next StartGuiding.")
    return "\n".join(lines)
