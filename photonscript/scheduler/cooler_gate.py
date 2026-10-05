"""PS-61 cooler gate: no lights until the sensor is at its setpoint.

The cooler nanny (armer._reconcile_cooler, PS-35) re-asserts the setpoint and
alerts, and grading rejects warm subs, but nothing stopped NINA from shooting
while the sensor was still off setpoint. A frame more than ~1 C off the
setpoint does not match the dark library (HANDBOOK: darks must match
temperature), so it is wasted shutter time at best.

How it runs. Core NINA has no "wait for camera temperature with a timeout"
instruction (CoolCamera waits with no timeout, so a dead cooler would wedge
the night), so the sequences carry the repo-verified ExternalScript item:

    NINA ExternalScript -> deploy\\cooler-gate.cmd -> photonscript cooler-gate
      -> POST /api/cooler/gate (this module, held while it waits)

run_gate() polls the rig's NINA camera info until |sensor - setpoint| is
within cooler_gate_tolerance_c, at most cooler_gate_timeout_min. On timeout it
sends one "not imaging" Pushover per episode (a reminder hourly, a recovery
notice when the sensor is back) and returns SKIP; the CLI then exits
EXIT_SKIP, the .cmd turns that (and only that) into exit 1, and the gate item's
ErrorBehavior 1 (SkipInstructionSetOnError) skips that light block. Anything
uncertain fails OPEN (verdict PASS / UNKNOWN, exit 0): sensor unreadable,
service down, mode "warn" or "off".

LIVE holds each rig's gate state for the dashboard camera row
(camera_row_note)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("skip", "warn", "off")
EXIT_SKIP = 3            # CLI exit code for a deliberate skip (cmd -> exit 1)
GATE_SCRIPT_TOKEN = "cooler-gate"   # file-name token the lint rule looks for
UNREADABLE_POLLS = 3     # sensor unreadable this many polls in a row: pass
REALERT_S = 3600         # reminder while a rig keeps skipping
SKIP_NOTE_S = 1800       # dashboard keeps "not imaging" this long after a skip
LIVE_STALE_S = 120       # a "waiting" state older than this is ignored
LOG_NAME = "cooler_gate.jsonl"

# rig -> {"state": waiting|skipped|ok, "temp_c", "setpoint_c", "tol_c",
#         "label", "since", "updated", "waited_s"} (epoch seconds)
LIVE: dict[str, dict] = {}
# rig -> {"alerted_at": epoch, "skips": n}: one alert per off-setpoint episode
_EPISODE: dict[str, dict] = {}


def gate_mode(cfg) -> str:
    m = str(getattr(cfg, "cooler_gate_mode", "skip") or "skip").strip().lower()
    return m if m in MODES else "skip"


def gate_script(cfg) -> str | None:
    """The script path when the sequences should carry the gate: mode not
    off AND the file exists on this machine (the armer generates on the
    scope PC). A missing script must never turn into a skipped night, so no
    gate is emitted then (the lint rule warns)."""
    if gate_mode(cfg) == "off":
        return None
    path = str(getattr(cfg, "cooler_gate_script", "") or "").strip()
    if not path:
        return None
    try:
        return path if Path(path).is_file() else None
    except OSError:
        return None


def safe_label(text) -> str:
    """A NINA-command-line-safe label: letters, digits, space and ._+-
    only (no quotes, no cmd metacharacters), at most 60 chars."""
    s = re.sub(r"[^A-Za-z0-9 ._+\-]+", " ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60] or "lights"


def script_args(rig: str, setpoint: float, label: str) -> str:
    """Arguments after the script path (see deploy/cooler-gate.cmd)."""
    return (f"{rig} --setpoint={float(setpoint):g} "
            f'--label="{safe_label(label)}"')


def within(temp, setpoint: float, tol: float) -> bool:
    return temp is not None and abs(float(temp) - float(setpoint)) <= float(tol)


def _rig_name(cfg, rig: str) -> str:
    try:
        from photonscript.shared.rigs import rig_label
        return rig_label(cfg, rig)
    except Exception:  # noqa: BLE001
        return rig


def _utc(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(cfg, rec: dict) -> None:
    try:
        d = Path(getattr(cfg, "data_dir", "") or ".")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / LOG_NAME, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning("cooler gate log write failed: %s", e)


def recent(cfg, n: int = 20) -> list[dict]:
    """The last n gate results that waited or did not pass (newest last)."""
    try:
        p = Path(getattr(cfg, "data_dir", "") or ".") / LOG_NAME
        lines = p.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:  # noqa: BLE001
        return []


async def _read_temp(cfg, rig: str):
    from photonscript.shared.rigs import rig_config, nina_camera_info
    info = await nina_camera_info(rig_config(cfg, rig).nina_base_url)
    t = (info or {}).get("Temperature")
    try:
        return float(t) if t is not None else None
    except (TypeError, ValueError):
        return None


def skip_message(name: str, label: str, temp, setpoint: float,
                 minutes: float, mode: str) -> str:
    what = f"not imaging {label}" if mode == "skip" else f"imaging {label} WARM"
    t = "unknown" if temp is None else f"{float(temp):.1f} C"
    tail = ("; check the cooler. The block is skipped and retried on the "
            "next pass." if mode == "skip" else
            "; check the cooler (cooler_gate_mode=warn, frames will not "
            "match the darks).")
    return (f"{name}: {what}, sensor at {t} vs setpoint {setpoint:g} C "
            f"after {minutes:.0f} min{tail}")


async def run_gate(cfg, rig: str, setpoint: float | None = None,
                   label: str = "", *, read=None, sleep=asyncio.sleep,
                   clock=time.time, notify_fn=None,
                   disconnected=None) -> dict:
    """Hold until the rig's sensor is within tolerance or the timeout runs
    out. Returns {"verdict": PASS | SKIP | WARN | UNKNOWN | OFF | ABORTED,
    ...}; only SKIP stops lights. read(cfg, rig) -> temperature or None,
    notify_fn(cfg, msg, title=, priority=) and disconnected() -> bool are
    injectable for tests (disconnected: NINA cancelled the script, e.g. the
    roof closed, so stop quietly)."""
    from photonscript.shared.rigs import rig_setpoint
    mode = gate_mode(cfg)
    if setpoint is None:
        setpoint = rig_setpoint(cfg, rig)
    setpoint = float(setpoint)
    tol = float(getattr(cfg, "cooler_gate_tolerance_c", 1.0))
    timeout_s = max(0.0, float(getattr(cfg, "cooler_gate_timeout_min", 20.0))) * 60
    poll_s = max(1.0, float(getattr(cfg, "cooler_gate_poll_s", 15.0)))
    label = safe_label(label)
    name = _rig_name(cfg, rig)
    read = read or _read_temp
    if notify_fn is None:
        from photonscript.shared.pushover import notify as notify_fn
    base = {"rig": rig, "label": label, "setpoint_c": setpoint, "tol_c": tol,
            "mode": mode}
    if mode == "off":
        return {**base, "verdict": "OFF", "temp_c": None, "waited_s": 0.0,
                "reason": "cooler_gate_mode=off"}

    t0 = clock()
    temp = None
    unreadable = 0
    ever_read = False
    while True:
        if disconnected is not None:
            try:
                if await disconnected():
                    LIVE.pop(rig, None)
                    return {**base, "verdict": "ABORTED", "temp_c": temp,
                            "waited_s": round(clock() - t0, 1),
                            "reason": "NINA cancelled the gate"}
            except Exception:  # noqa: BLE001
                pass
        try:
            temp = await read(cfg, rig)
        except Exception as e:  # noqa: BLE001
            logger.warning("cooler gate read failed (%s): %s", rig, e)
            temp = None
        now = clock()
        waited = now - t0
        if temp is None:
            unreadable += 1
            if not ever_read and unreadable >= UNREADABLE_POLLS:
                LIVE.pop(rig, None)
                res = {**base, "verdict": "UNKNOWN", "temp_c": None,
                       "waited_s": round(waited, 1),
                       "reason": "sensor temperature unreadable; imaging "
                                 "anyway (fails open)"}
                _log(cfg, {"t_utc": _utc(now), **res})
                return res
        else:
            ever_read = True
            unreadable = 0
            if within(temp, setpoint, tol):
                LIVE[rig] = {"state": "ok", "temp_c": temp, "setpoint_c": setpoint,
                             "tol_c": tol, "label": label, "since": now,
                             "updated": now, "waited_s": round(waited, 1)}
                ep = _EPISODE.pop(rig, None)
                if ep and ep.get("alerted_at"):
                    try:
                        await notify_fn(
                            cfg, f"{name}: sensor back at {temp:.1f} C "
                            f"(setpoint {setpoint:g} C), imaging {label} "
                            "resumed", title="PhotonScript cooler gate")
                    except Exception as e:  # noqa: BLE001
                        logger.warning("cooler gate notify failed: %s", e)
                res = {**base, "verdict": "PASS", "temp_c": temp,
                       "waited_s": round(waited, 1), "reason": "within tolerance"}
                if waited >= 1:
                    _log(cfg, {"t_utc": _utc(now), **res})
                return res
        if temp is not None or ever_read:
            prev = LIVE.get(rig) or {}
            since = prev.get("since") if prev.get("state") == "waiting" else None
            LIVE[rig] = {"state": "waiting", "temp_c": temp,
                         "setpoint_c": setpoint, "tol_c": tol, "label": label,
                         "since": since or t0, "updated": now,
                         "waited_s": round(waited, 1)}
        if waited >= timeout_s:
            verdict = "SKIP" if mode == "skip" else "WARN"
            ep = _EPISODE.setdefault(rig, {"alerted_at": None, "skips": 0})
            ep["skips"] += 1
            if ep["alerted_at"] is None or now - ep["alerted_at"] >= REALERT_S:
                ep["alerted_at"] = now
                try:
                    await notify_fn(cfg, skip_message(name, label, temp, setpoint,
                                                      waited / 60, mode),
                                    title="PhotonScript cooler gate", priority=1)
                except Exception as e:  # noqa: BLE001
                    logger.warning("cooler gate notify failed: %s", e)
            LIVE[rig] = {"state": "skipped" if verdict == "SKIP" else "warned",
                         "temp_c": temp, "setpoint_c": setpoint, "tol_c": tol,
                         "label": label, "since": now, "updated": now,
                         "waited_s": round(waited, 1)}
            res = {**base, "verdict": verdict, "temp_c": temp,
                   "waited_s": round(waited, 1),
                   "reason": (f"sensor {'unknown' if temp is None else f'{temp:.1f} C'}"
                              f" vs setpoint {setpoint:g} C after "
                              f"{waited / 60:.0f} min")}
            _log(cfg, {"t_utc": _utc(now), **res})
            logger.warning("cooler gate %s on %s (%s): %s", verdict, rig, label,
                           res["reason"])
            return res
        await sleep(min(poll_s, max(0.5, timeout_s - waited)))


def camera_row_note(cfg, rig: str, temp, cooler_on, armer_state: str,
                    now: float | None = None) -> str | None:
    """The dashboard camera-row line (or None): a waiting gate, a recent
    skip, or (armer RUNNING, cooler ON) a sensor off its setpoint."""
    from photonscript.shared.rigs import rig_setpoint
    now = time.time() if now is None else now
    tol = float(getattr(cfg, "cooler_gate_tolerance_c", 1.0))
    live = LIVE.get(rig) or {}
    st = live.get("state")
    if st == "waiting" and now - float(live.get("updated", 0)) <= LIVE_STALE_S:
        t = live.get("temp_c") if live.get("temp_c") is not None else temp
        mins = (now - float(live.get("since", now))) / 60
        return (f"waiting for cooler: {_fmt(t)} -> {live['setpoint_c']:g} C "
                f"({mins:.0f} min, lights held"
                + (f": {live['label']}" if live.get("label") else "") + ")")
    if st in ("skipped", "warned") and now - float(live.get("updated", 0)) <= SKIP_NOTE_S:
        what = "not imaging" if st == "skipped" else "imaging WARM"
        return (f"{what}: sensor {_fmt(live.get('temp_c'))} vs setpoint "
                f"{live['setpoint_c']:g} C after {float(live.get('waited_s', 0)) / 60:.0f}"
                f" min ({live.get('label') or 'block'}); check the cooler")
    sp = rig_setpoint(cfg, rig)
    if (str(armer_state or "").upper() == "RUNNING" and cooler_on is True
            and temp is not None and not within(temp, sp, tol)):
        return f"waiting for cooler: {_fmt(temp)} -> {sp:g} C"
    return None


def _fmt(t) -> str:
    try:
        return f"{float(t):.1f} C"
    except (TypeError, ValueError):
        return "? C"
