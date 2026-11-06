"""Unguided guard: nothing may guide on an UNGUIDED night (2026-10-13).

Why: three nights lost the same way (2026-10-12, 10-13). The night was
armed unguided (Paramount MX, TPoint + ProTrack), but NINA #1 had PHD2
connected as its guider (the armer's connect-all at arm and the sequence's
Start area both connected it; on 10-13 Jeremy had also connected it by hand
to experiment, and PHD2's calibration graded FAIL). NINA's meridian flip
resumes guiding whenever a guider is connected, whether or not it was
guiding before, and every autofocus after that stopped and restarted it:

    20:38 GUIDER-CONNECTED (no GUIDER-START until the flip)
    02:24 MOUNT-BEFORE-FLIP, 02:28:03 GUIDER-START, 02:28:32 MOUNT-AFTER-FLIP
    02:52 AF start GUIDER-STOP ... 02:57 GUIDER-START after AF-FINISHED
    (again 03:40, 04:18), 04:20 unsafe, parked

PHD2 guided at 37" RMS on the FAIL calibration and both rigs trailed.

Three layers, one policy (`unguided_guider_policy`):

  1. Sequences: an unguided night's Start area no longer connects the
     guider (nina_sequence_json._guider_connect_item), and the armer's
     connect-all skips it (Armer.connect_all).
  2. Armer, at arm, at a restart (restore) and before every (re-)dispatch:
     check() below. A PHD2 guider connected in NINA #1 is disconnected
     through the ninaAPI (policy disconnect) or paged with the manual step
     (alert).
  3. Live watch, every armer tick while RUNNING: check(phase="watch").
  4. PS-197: at arm, restore and right after a dispatch's load, NINA #1's
     loaded sequence is read (GET only) for StartGuiding; one found on an
     unguided night is an event, and a push when that sequence runs.
     NINA #1's guider guiding / calibrating / lost lock, or PHD2's own
     AppState doing so: per the policy, stop it (ninaAPI guider stop, then
     disconnect; PHD2 stop_capture when PHD2 still guides) and page once
     per episode, naming what started it from NINA's event history
     (attribute_start: the meridian flip, an autofocus, a center).

Policy: disconnect (default) | alert | off.
  disconnect  stop guiding and disconnect NINA #1's guider. Touches the
              guider only (ninaAPI /equipment/guider/stop and /disconnect,
              PHD2 stop_capture); never the mount, the camera or a
              sequence, never starts anything. PHD2 stays open with its own
              camera and mount connections.
  alert       page with the exact manual step, send nothing.
  off         no guard (and the armer's connect-all connects the guider
              again, as before).

Why disconnect is the default: the flip trigger has no "do not resume
guiding" setting, so a connected guider is a loaded gun on every unguided
night, and the only command sent concerns a device the night does not use.
NINA's Direct Guider (PS-66 unguided dithers) is left alone: its "guiding"
moves nothing.

Every step is a run event (kind `unguided_guard`: detect, stopped,
disconnected, failed, clear, alert) in runs/<night>_events.jsonl; the sub
doctor reads the newest `detect` to name the cause of a guider_conflict.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

KIND = "unguided_guard"
POLICIES = ("disconnect", "alert", "off")
DEFAULT_POLICY = "disconnect"
ALERT_TITLE = "PhotonScript unguided guard"

# States in which PHD2 (directly, or through NINA's guider) moves the mount.
# Looping only exposes the guide camera; stopped / idle / connected move
# nothing.
MOVING = {"guiding", "calibrating", "lostlock", "settling"}

# A command per episode is repeated at most this often, and at most this
# many times a night (then alert only), so a guider that keeps coming back
# is never hammered.
RETRY_S = 120.0
MAX_ACTIONS = 8

# attribute_start windows (minutes before a GUIDER-START; an AFTER-FLIP may
# also come up to FLIP_AFTER_MIN after it: on 10-13 NINA resumed guiding 29 s
# before it logged MOUNT-AFTER-FLIP)
FLIP_BEFORE_MIN = 15.0
FLIP_AFTER_MIN = 3.0
AF_BEFORE_MIN = 5.0
CENTER_BEFORE_MIN = 3.0

CAUSE_TEXT = {
    "meridian_flip": "NINA started PHD2 after the meridian flip",
    "autofocus": "NINA restarted PHD2 after an autofocus",
    "center": "NINA restarted PHD2 after a plate-solve center",
    "nina": "NINA started PHD2 (no flip, autofocus or center just before)",
    "phd2": "PHD2 is guiding but NINA never started it (started in PHD2 itself?)",
}

MANUAL_STEP = ("In NINA #1: Equipment > Guider > Disconnect (leave PHD2 open, "
               "just not connected to NINA), or in PHD2 press Stop; re-arm "
               "GUIDED only after a good calibration.")


def policy(config) -> str:
    v = str(getattr(config, "unguided_guider_policy", DEFAULT_POLICY)
            or DEFAULT_POLICY).strip().lower()
    return v if v in POLICIES else DEFAULT_POLICY


def norm_state(v) -> str:
    """'LostLock' / 'Lost Lock' / 'GUIDING' -> 'lostlock' / 'guiding'."""
    return "".join(ch for ch in str(v or "").lower() if ch.isalnum())


def moving(state) -> bool:
    return norm_state(state) in MOVING


def is_direct_guider(name) -> bool:
    s = "".join(ch for ch in str(name or "").lower() if ch.isalnum())
    return "directguider" in s


def guider_view(payload) -> dict:
    """NINA #1's /equipment/guider/info Response -> {readable, connected,
    name, direct, state}. A disconnected guider reports no Name (ninaAPI
    2.2.15)."""
    if not isinstance(payload, dict):
        return {"readable": False, "connected": None, "name": None,
                "direct": False, "state": None}
    p = payload.get("Response", payload)
    if not isinstance(p, dict):
        p = {}
    name = p.get("Name") or p.get("DisplayName") or p.get("DeviceId") or None
    conn = p.get("Connected")
    return {"readable": True, "connected": bool(conn) if conn is not None else None,
            "name": str(name) if name else None,
            "direct": is_direct_guider(name),
            "state": norm_state(p.get("State")) or None}


# --------------------------------------------------------------------------
# what started it: NINA's event history
# --------------------------------------------------------------------------

def _events_list(events) -> list[dict]:
    if isinstance(events, dict):
        events = events.get("Response", [])
    return [e for e in (events or []) if isinstance(e, dict)]


_CFG = None


def _t(e) -> datetime | None:
    """Event Time -> naive UTC. NINA writes MOUNT-CENTER with no offset, as
    observatory local time (2026-10-14); reading it as UTC put every center
    6 h off, so the "started after a center" attribution never matched."""
    global _CFG
    from photonscript.scheduler.stuck_loop import event_time
    if _CFG is None:
        try:
            from photonscript.shared.config import PhotonScriptConfig
            _CFG = PhotonScriptConfig()
        except Exception:  # noqa: BLE001
            _CFG = False
    return event_time(_CFG or None, (e or {}).get("Time"))


def attribute_starts(events) -> list[dict]:
    """Every GUIDER-START in a ninaAPI event history with what NINA had just
    done: [{"t": naive UTC, "by": meridian_flip | autofocus | center | nina}]
    oldest first. The nearest preceding trigger inside its window wins; a
    MOUNT-AFTER-FLIP up to FLIP_AFTER_MIN after the start counts for the
    flip (NINA resumes the guider before it logs the flip as done)."""
    evs = [(e.get("Event"), _t(e)) for e in _events_list(events)]
    evs = [(n, t) for n, t in evs if n and t is not None]
    out = []
    for name, t in evs:
        if name != "GUIDER-START":
            continue
        cands = []
        for n2, t2 in evs:
            dt = (t - t2).total_seconds() / 60.0   # > 0: before the start
            if n2 == "MOUNT-BEFORE-FLIP" and 0 <= dt <= FLIP_BEFORE_MIN:
                cands.append((dt, "meridian_flip"))
            elif n2 == "MOUNT-AFTER-FLIP" and -FLIP_AFTER_MIN <= dt <= FLIP_BEFORE_MIN:
                cands.append((abs(dt), "meridian_flip"))
            elif n2 == "AUTOFOCUS-FINISHED" and 0 <= dt <= AF_BEFORE_MIN:
                cands.append((dt, "autofocus"))
            elif n2 == "MOUNT-CENTER" and 0 <= dt <= CENTER_BEFORE_MIN:
                cands.append((dt, "center"))
        by = min(cands)[1] if cands else "nina"
        out.append({"t": t, "by": by})
    return out


def latest_start(events, until: datetime | None = None,
                 since: datetime | None = None) -> dict | None:
    """The newest attributed GUIDER-START at or before `until` (and after
    `since`), else None."""
    best = None
    for s in attribute_starts(events):
        if until is not None and s["t"] > until:
            continue
        if since is not None and s["t"] < since:
            continue
        best = s
    return best


def start_from_run_events(run_events, until: datetime | None = None) -> dict | None:
    """The newest `detect` of this guard in a night's run events (what the
    sub doctor reads live): {"t", "by", "text"} or None."""
    from photonscript.shared.phd2_store import parse_z
    best = None
    for e in run_events or []:
        if e.get("kind") != KIND or e.get("value") != "detect" or not e.get("by"):
            continue
        t = parse_z(e.get("start_t") or e.get("t"))
        if t is None or (until is not None and t > until):
            continue
        if best is None or t >= best["t"]:
            best = {"t": t, "by": e["by"], "text": CAUSE_TEXT.get(e["by"], e["by"])}
    return best


def cause_text(by: str | None) -> str:
    return CAUSE_TEXT.get(by or "", CAUSE_TEXT["nina"])


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

def new_state(night: str | None) -> dict:
    return {"night": night, "episode": None, "actions": 0, "last_action": None,
            "connected_alerted": False, "alerted": [], "episodes": 0}


def _hm(config, t: datetime | None) -> str:
    if t is None:
        return "?"
    try:
        from photonscript.shared.localtime import to_local
        return to_local(config, t).strftime("%H:%M") + " local"
    except Exception:  # noqa: BLE001
        return t.strftime("%H:%M") + "Z"


def _iso(t: datetime | None) -> str | None:
    return t.replace(microsecond=0).isoformat() + "Z" if t else None


def _may_act(st: dict, now: datetime) -> bool:
    if st.get("actions", 0) >= MAX_ACTIONS:
        return False
    last = st.get("last_action")
    if last:
        try:
            if (now - datetime.fromisoformat(str(last).rstrip("Z"))
                    ).total_seconds() < RETRY_S:
                return False
        except ValueError:
            pass
    return True


# PS-197: phases that also read NINA #1's loaded sequence for StartGuiding
# (at arm a re-arm UNGUIDED over a running guided night; at restore; right
# after a dispatch's load, which must hold no StartGuiding on an unguided
# night). A StartGuiding there fails against the disconnected guider and
# SkipInstructionSetOnError skips its whole target container (2026-10-14).
SEQUENCE_PHASES = ("arm", "restore", "loaded")
DONE_STATUSES = {"finished", "skipped", "disabled", "failed"}


def _sname(node: dict) -> str:
    n = str(node.get("Name") or "")
    return n[: -len("_Container")] if n.endswith("_Container") else n


def _is_start_guiding(node: dict) -> bool:
    t = str(node.get("$type") or "").split(",")[0].strip()
    if t:
        return t.endswith(".StartGuiding") or t == "StartGuiding"
    return norm_state(node.get("Name")) == "startguiding"


def loaded_start_guiding(tree) -> dict:
    """StartGuiding items in a sequence tree (ninaAPI /sequence/state or
    /sequence/json, or a sequence file): {"items": [{"where", "status"}],
    "pending": n not finished / skipped, "running": the sequence runs}."""
    items, running = [], [False]

    def visit(node, path: str) -> None:
        if isinstance(node, list):
            for ch in node:
                visit(ch, path)
            return
        if not isinstance(node, dict):
            return
        if norm_state(node.get("Status")) == "running":
            running[0] = True
        if _is_start_guiding(node):
            items.append({"where": path or "root",
                          "status": norm_state(node.get("Status")) or None})
        name = _sname(node)
        here = (f"{path}/{name}" if path else name) if name else path
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                visit(v, here)

    if isinstance(tree, dict) and "Response" in tree and "$type" not in tree:
        tree = tree.get("Response")
    visit(tree, "")
    pending = [i for i in items if (i["status"] or "") not in DONE_STATUSES]
    return {"items": items, "pending": len(pending), "running": running[0]}


async def _loaded_sequence(config, io, st, phase, now) -> dict | None:
    """PS-197: NINA #1's loaded sequence on an unguided night must hold no
    StartGuiding. Read only: an event every time one is found, a push once
    per night when the sequence runs (or right after our own load)."""
    reader = getattr(io, "nina_sequence", None)
    if reader is None:
        return None
    tree = await reader()
    if tree is None:
        return {"readable": False}
    seq = loaded_start_guiding(tree)
    out = {"readable": True, "start_guiding": len(seq["items"]),
           "pending": seq["pending"], "running": seq["running"]}
    if not seq["pending"]:
        return out
    where = sorted({i["where"] for i in seq["items"]
                    if (i["status"] or "") not in DONE_STATUSES})
    text = (f"NINA #1's loaded sequence has {seq['pending']} StartGuiding on an "
            f"UNGUIDED night ({'; '.join(where[:3])}): with the guider "
            "disconnected each fails and skips its whole target container, "
            "so the night loops slew / center / autofocus with no lights "
            "(2026-10-14).")
    await io.event("loaded_start_guiding", text, phase=phase,
                   running=seq["running"], pending=seq["pending"])
    if (seq["running"] or phase == "loaded") and not st.get("sequence_alerted"):
        st["sequence_alerted"] = True
        await io.notify(text + " Restart tonight from now (it re-plans "
                        "unguided and the dispatch gate checks it), or re-arm.",
                        1)
    out["alerted"] = bool(st.get("sequence_alerted"))
    return out


async def check(config, io, st: dict, *, phase: str,
                now: datetime | None = None) -> dict:
    """One look at NINA #1's guider and PHD2 on an unguided night, and the
    policy's response. phase: arm | restore | dispatch | loaded | watch. `st` is the
    caller's per-night state (new_state), updated in place. `io` (GuardIO
    on the scope, a fake in tests) provides nina_guider, nina_stop,
    nina_disconnect, nina_events, phd2_state, phd2_stop, notify, event.
    Returns {status, ...}: off | direct | clear | unknown | connected |
    guiding. Never raises."""
    now = now or datetime.utcnow()
    pol = policy(config)
    if pol == "off":
        return {"status": "off"}
    seq_out = None
    if phase in SEQUENCE_PHASES:
        try:
            seq_out = await _loaded_sequence(config, io, st, phase, now)
        except Exception as e:  # noqa: BLE001
            logger.warning("unguided guard sequence read (%s) failed: %s", phase, e)
    try:
        res = await _check(config, io, st, phase, now, pol)
    except Exception as e:  # noqa: BLE001
        logger.warning("unguided guard (%s) failed: %s", phase, e)
        res = {"status": "error", "error": str(e)}
    if seq_out is not None:
        res["loaded_sequence"] = seq_out
    return res


async def _check(config, io, st, phase, now, pol) -> dict:
    g = guider_view(await io.nina_guider())
    if g["connected"] and g["direct"]:
        return {"status": "direct", "guider": g}
    phd2 = norm_state(await io.phd2_state()) or None
    nina_moving = bool(g["connected"]) and moving(g["state"])
    phd2_moving = moving(phd2)
    out = {"guider": g, "phd2": phd2, "policy": pol, "phase": phase}

    if nina_moving or phd2_moving:
        return {**out, **await _guiding(config, io, st, phase, now, pol, g,
                                        phd2, nina_moving, phd2_moving)}
    if st.get("episode"):
        ep = st["episode"]
        st["episode"] = None
        await io.event("clear", f"no guiding now (NINA guider "
                       f"{'connected' if g['connected'] else 'disconnected'}, "
                       f"PHD2 {phd2 or 'unknown'})", since=ep.get("since"))
    if g["connected"]:
        return {**out, **await _connected(config, io, st, phase, now, pol, g)}
    if not g["readable"] and phd2 is None:
        return {**out, "status": "unknown"}
    return {**out, "status": "clear"}


async def _guiding(config, io, st, phase, now, pol, g, phd2,
                   nina_moving, phd2_moving) -> dict:
    ep = st.get("episode")
    fresh = ep is None
    if fresh:
        by, start_t = "phd2", None
        if nina_moving:
            s = latest_start(await io.nina_events(), until=now + timedelta(minutes=1))
            by = s["by"] if s else "nina"
            start_t = s["t"] if s else None
        ep = st["episode"] = {"since": _iso(now), "by": by,
                              "start_t": _iso(start_t), "alerted": False}
        st["episodes"] = st.get("episodes", 0) + 1
        state_txt = g["state"] if nina_moving else phd2
        await io.event("detect", f"{cause_text(by)}: guiding "
                       f"({state_txt}) on an UNGUIDED night ({phase})",
                       by=by, start_t=ep["start_t"], nina_state=g["state"],
                       phd2_state=phd2, phase=phase)
    by = ep["by"]
    when = (_hm(config, datetime.fromisoformat(ep["start_t"].rstrip("Z")))
            if ep.get("start_t") else _hm(config, now))
    lead = (f"{cause_text(by)} ({when}) on an UNGUIDED night: PHD2 is "
            f"{phd2 or g['state']}, the mount is being guided instead of "
            "tracking on TPoint + ProTrack, so the subs trail.")
    done = None
    if pol == "disconnect" and _may_act(st, now):
        st["actions"] = st.get("actions", 0) + 1
        st["last_action"] = _iso(now)
        steps = []
        if g["connected"]:
            ok = bool(await io.nina_stop())
            steps.append(f"NINA guider stop {'ok' if ok else 'FAILED'}")
            ok_d = bool(await io.nina_disconnect())
            steps.append(f"NINA guider disconnect {'ok' if ok_d else 'FAILED'}")
        now_phd2 = norm_state(await io.phd2_state()) or None
        if moving(now_phd2):
            ok_p, why = await io.phd2_stop()
            steps.append(f"PHD2 stop_capture {'ok' if ok_p else 'FAILED'} ({why})")
        g2 = guider_view(await io.nina_guider())
        p2 = norm_state(await io.phd2_state()) or None
        stopped = not (g2["connected"] and moving(g2["state"])) and not moving(p2)
        done = {"steps": steps, "stopped": stopped,
                "disconnected": g2["connected"] is False}
        await io.event("stopped" if stopped else "failed", "; ".join(steps)
                       or "nothing to send", by=by, phd2_state=p2,
                       nina_connected=g2["connected"])
        if not stopped and not ep.get("failed_alerted"):
            ep["failed_alerted"] = True
            await io.notify(f"{lead} The guard could not stop it "
                            f"({'; '.join(steps) or 'no command possible'}). "
                            f"{MANUAL_STEP}", 2 if phase == "watch" else 1)
            ep["alerted"] = True
            return {"status": "guiding", "by": by, "action": done}
    if not ep.get("alerted"):
        ep["alerted"] = True
        if done and done["stopped"]:
            tail = ("Stopped and disconnected NINA #1's guider"
                    if done["disconnected"] else "Stopped guiding")
            msg = (f"{lead} {tail} ({'; '.join(done['steps'])}); the mount "
                   "tracks unguided again. Policy unguided_guider_policy="
                   f"{pol}.")
        else:
            msg = (f"{lead} Policy unguided_guider_policy={pol}: nothing "
                   f"sent. {MANUAL_STEP}")
        # PS-212: stopped by the guard = handled (info); else act
        await io.notify(msg, 1, kind="info" if done and done["stopped"] else "act")
        await io.event("alert", msg, by=by)
    return {"status": "guiding", "by": by, "action": done}


async def _connected(config, io, st, phase, now, pol, g) -> dict:
    """NINA #1 has a (PHD2) guider connected but nothing guides yet."""
    what = (f"NINA #1 has its guider ({g['name'] or 'PHD2'}) connected on an "
            "UNGUIDED night: NINA's meridian flip would start PHD2 guiding "
            "and every autofocus after it would restart it (2026-10-13).")
    if pol == "disconnect" and _may_act(st, now):
        st["actions"] = st.get("actions", 0) + 1
        st["last_action"] = _iso(now)
        ok = bool(await io.nina_disconnect())
        g2 = guider_view(await io.nina_guider())
        gone = g2["connected"] is False
        await io.event("disconnected" if gone else "failed",
                       f"NINA guider disconnect {'ok' if ok else 'FAILED'}, "
                       f"now {'disconnected' if gone else 'connected'} ({phase})",
                       phase=phase)
        if gone:
            if not st.get("connected_alerted"):
                st["connected_alerted"] = True
                await io.notify(f"{what} Disconnected it ({phase}); PHD2 stays "
                                "open. Policy unguided_guider_policy=disconnect.",
                                0, kind="info")
            return {"status": "connected", "action": "disconnected"}
        if not st.get("connected_failed_alerted"):
            st["connected_failed_alerted"] = True
            await io.notify(f"{what} The guard could not disconnect it. "
                            f"{MANUAL_STEP}", 1)
        return {"status": "connected", "action": "failed"}
    if pol == "alert" and not st.get("connected_alerted"):
        st["connected_alerted"] = True
        msg = f"{what} Policy alert: nothing sent. {MANUAL_STEP}"
        await io.notify(msg, 1)
        await io.event("alert", msg, phase=phase)
    return {"status": "connected", "action": None}


# --------------------------------------------------------------------------
# the scope's io (through the armer's ninaAPI helper and the night pilot's
# PHD2 client)
# --------------------------------------------------------------------------

class GuardIO:
    """The real scope: NINA #1 through Armer._nina (GET), PHD2 through the
    night pilot's JSON-RPC helpers (get_app_state, stop_capture under the
    PS-91 PHD2 lock). Every method never raises."""

    def __init__(self, armer):
        self.armer = armer
        self.config = armer.config

    async def _nina(self, key: str):
        detail = self.armer.detail        # a read is not a dispatch error
        try:
            return await self.armer._nina(key)
        except Exception:  # noqa: BLE001
            return None
        finally:
            self.armer.detail = detail

    async def nina_guider(self):
        return await self._nina("guider")

    async def nina_stop(self) -> bool:
        return await self._nina("guider_stop") is not None

    async def nina_disconnect(self) -> bool:
        return await self._nina("guider_disconnect") is not None

    async def nina_events(self):
        return await self._nina("event_history")

    async def nina_sequence(self):
        """PS-197: NINA #1's loaded sequence (GET /sequence/state, falls
        back to /sequence/json). Read only."""
        return await self._nina("sequence_state")

    async def phd2_state(self):
        try:
            from photonscript.scheduler.night_pilot import PilotIO
            return await PilotIO().phd2_state(self.config)
        except Exception:  # noqa: BLE001
            return None

    async def phd2_stop(self) -> tuple[bool, str]:
        try:
            from photonscript.scheduler.night_pilot import PilotIO
            return await PilotIO().phd2_stop(self.config)
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    async def notify(self, message: str, priority: int = 1,
                     kind: str = "act") -> None:
        try:
            from photonscript.shared.pushover import notify
            await notify(self.config, message, title=ALERT_TITLE,
                         priority=priority, kind=kind)
        except Exception as e:  # noqa: BLE001
            logger.warning("unguided guard push failed: %s", e)

    async def event(self, value: str, detail: str, **extra) -> None:
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import append_jsonl, night_of
            now = datetime.utcnow()
            append_jsonl(events_path(self.config, night_of(self.config, now)),
                         {"t": _iso(now), "rig": "rc16", "src": "photonscript",
                          "kind": KIND, "value": value, "detail": detail,
                          **{k: v for k, v in extra.items() if v is not None}})
        except Exception as e:  # noqa: BLE001
            logger.warning("unguided guard event (%s) not logged: %s", value, e)
        logger.warning("unguided guard %s: %s", value, detail)
