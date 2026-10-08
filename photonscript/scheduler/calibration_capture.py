"""PS-113: on-demand darks + bias capture, one guarded job per rig.

A job dispatches a darks + bias sequence to the rig's OWN NINA (RC16 ->
NINA #1, Piggy-600 -> NINA #2), watches it, QA's every frame as it lands
(calibration_qa) and files the night into the Library (quarantining the bad
ones). The sequence never contains a mount, dome, filter-wheel or guider
instruction, and the job never sends anything to the other rig's NINA (the
Piggy-600 job may READ the RC16's safety monitor when NINA #2 has none).

Refused (start) unless ALL hold:
  * the armer is DISARMED or COMPLETE (never ARMED / RUNNING / PAUSED_UNSAFE)
  * no other capture job on this rig
  * the rig's NINA is not running a sequence
  * the roof / safety monitor reads CLOSED (unsafe); open or unreadable refuses
  * RC16: PhotonScript does not hold PHD2 (phd2_ops) and PHD2 is not
    guiding / calibrating / looping / settling
  * daytime capture is not disabled for the rig (day vs night leak check)
  * the rig's camera connects and the watch dir is known
  * PS-128: NINA's readout mode for sequence images (ninaAPI camera info:
    ReadoutModes[ReadoutModeForNormalImages]) is the rig's lights' mode
    (camera_readout_mode / piggyback_readout_mode). NINA has no per-exposure
    readout setting in the generated sequence: every dark and bias is shot
    at the profile's mode, so a profile left at LCG would fill an HCG quota
    with frames that never count. Unknown (field missing) does not refuse;
    the frames' READOUTM is checked against the asked mode by QA instead.
Then the job cools to the setpoint and waits until the sensor reads within
setpoint +/- calibration_temp_tol_c twice in a row (or aborts after
calibration_cool_timeout_min, default COOL_TIMEOUT_MIN) before dispatching
the first exposure.

PS-181 (calibration_cool_reach, default refuse): a DAYTIME start is refused
at once when the cooler cannot reach the setpoint now (the TEC reads flat
out above it, or the cooler history predicts more than
cooler_reach_max_power_pct at the live ambient), and a job that is cooling
aborts after calibration_cool_stall_min when the sensor stopped falling
above the setpoint, instead of holding the rig for the whole budget. Both
save the plan as the rig's deferred dawn plan (calibration_window) and say
when the dawn window opens. Every camera read of a job also feeds the
cooler history.

Stops (sequence stop + camera warm) on: Cancel, the time budget
(calibration_capture_budget_min), the roof reading open (or unreadable three
polls running), the sensor outside the tolerance two polls running, the
armer leaving DISARMED / COMPLETE, a daytime light leak or two frames in a
row failing a light check (leak / stars / level).

Daytime (sun above calibration_qa.NIGHT_SUN_ALT, -12) on a rig whose daytime state
is "untested": the plan starts with PROBE_COUNT darks at the longest exposure
that already has night darks, so the first daytime frames are compared with
night ones before the long sets run.

calibration_autofill (default False) starts a gap-driven job per rig in the
daytime only (sun up, finishing 2 h before sunset), once a day per rig.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

IDLE_STATES = ("DISARMED", "COMPLETE")
POLL_S = 30.0
COOL_TIMEOUT_MIN = 20.0
STALL_WINDOW_S = 180.0   # PS-181: "stopped falling" = < STALL_DROP_C in this long
STALL_DROP_C = 0.5
PROBE_COUNT = 3
ROOF_MISS_LIMIT = 3
TEMP_DRIFT_LIMIT = 2
LIGHT_STREAK_LIMIT = 2
NINA_MISS_LIMIT = 5
GUIDER_BUSY_STATES = ("guiding", "calibrating", "looping", "settling", "settledone")

_jobs: dict[str, "Job"] = {}
_starting: set = set()               # rigs between the guard checks and the job
_autofill_done: dict[str, str] = {}   # rig -> local date of the last autofill


def busy(rig: str) -> bool:
    """A capture job is active on this rig (armer.dispatch_raw refuses)."""
    if rig in _starting:
        return True
    j = _jobs.get(rig)
    return j is not None and j.active


def budget_min(config) -> float:
    try:
        return float(getattr(config, "calibration_capture_budget_min", 240) or 240)
    except (TypeError, ValueError):
        return 240.0


# --- NINA / device access (the test seam) -------------------------------------

class RigIO:
    """Everything a job sends to or reads from one rig's NINA."""

    def __init__(self, config, rig: str):
        from photonscript.shared.rigs import rig_config
        self.config = config
        self.rig = rig
        self.cfg = rig_config(config, rig)
        self.base = self.cfg.nina_base_url.rstrip("/")

    async def connect_camera(self) -> tuple[bool, str]:
        from photonscript.scheduler.preflight import _ensure_connected
        ok, _p, err = await _ensure_connected(self.cfg, "camera", attempts=2)
        return ok, err

    async def camera(self) -> dict | None:
        from photonscript.shared.rigs import nina_camera_info
        return await nina_camera_info(self.base)

    async def focuser_temp(self) -> float | None:
        """PS-181: the focuser temperature, the ambient proxy."""
        from photonscript.scheduler.preflight import _connected
        try:
            _ok, payload, _e = await _connected(self.cfg, "focuser")
            t = (payload or {}).get("Temperature")
            return float(t) if t is not None else None
        except Exception:  # noqa: BLE001
            return None

    async def cool(self, setpoint: float) -> dict:
        from photonscript.shared.rigs import nina_cool
        return await nina_cool(self.base, setpoint, minutes=5.0)

    async def warm(self) -> dict:
        from photonscript.shared.rigs import nina_warm
        return await nina_warm(self.base, minutes=float(
            getattr(self.config, "gradual_warm_minutes", 0.0)))

    async def stop(self) -> dict:
        from photonscript.shared.rigs import nina_sequence_stop
        return await nina_sequence_stop(self.base)

    async def dispatch(self, seq: dict) -> dict:
        from photonscript.shared.rigs import nina_dispatch
        return await nina_dispatch(self.base, seq, config=self.config,
                                   rig=self.rig)   # PS-132

    async def sequence_running(self) -> bool | None:
        """True if any item of the loaded sequence is RUNNING; None when the
        tree cannot be read."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(self.base + "/sequence/json")
                if r.status_code == 404:
                    return False  # nothing loaded
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # noqa: BLE001
            logger.debug("sequence tree (%s): %s", self.rig, e)
            return None
        if isinstance(data, dict) and data.get("Success") is False:
            err = str(data.get("Error") or "").lower()
            idle = ("no sequence", "not loaded", "not initialized")
            return False if any(s in err for s in idle) else None
        tree = data.get("Response", data) if isinstance(data, dict) else data
        return tree_running(tree)

    async def roof(self, connect: bool = True) -> tuple[bool | None, str]:
        """(closed, source): closed True = the safety monitor reads UNSAFE
        (roof closed), False = SAFE (open), None = unreadable. Reads the
        rig's own monitor (connecting it first when `connect`); the
        Piggy-600 falls back to a read-only look at the RC16's."""
        from photonscript.scheduler.preflight import _connected, _ensure_connected
        if connect:
            ok, payload, _err = await _ensure_connected(self.cfg, "safetymonitor",
                                                        attempts=1)
        else:
            ok, payload, _err = await _connected(self.cfg, "safetymonitor")
        if ok and "IsSafe" in (payload or {}):
            return (not bool(payload.get("IsSafe"))), f"{self.rig} safety monitor"
        if self.rig != "rc16":
            ok, payload, _err = await _connected(self.config, "safetymonitor")
            if ok and "IsSafe" in (payload or {}):
                return (not bool(payload.get("IsSafe"))), \
                    "RC16 safety monitor (read only)"
        return None, "safety monitor unreadable"

    async def guider_state(self) -> str | None:
        """PHD2 state as NINA #1 reports it (RC16 only); None = unknown."""
        if self.rig != "rc16":
            return ""
        from photonscript.scheduler.preflight import _connected
        ok, payload, _err = await _connected(self.cfg, "guider")
        if not ok:
            return "disconnected"
        return str((payload or {}).get("State") or "").lower()


def tree_running(tree) -> bool:
    found = False

    def walk(node):
        nonlocal found
        if found or not isinstance(node, dict):
            return
        if str(node.get("Status", "")).upper() == "RUNNING":
            found = True
            return
        for child in node.get("Items") or []:
            walk(child)

    for top in tree if isinstance(tree, list) else [tree]:
        walk(top)
    return found


# --- job ---------------------------------------------------------------------------

class Job:
    def __init__(self, rig: str, darks: list, bias: int, *, source: str,
                 budget_min: float, setpoint: float, tol: float, daytime: bool,
                 probe: list | None, expect: dict):
        self.id = uuid.uuid4().hex[:8]
        self.rig = rig
        self.darks = [[float(e), int(n)] for e, n in darks]
        self.bias = int(bias)
        self.source = source
        self.budget_min = float(budget_min)
        self.setpoint = setpoint
        self.tol = tol
        self.daytime = daytime
        self.probe = probe
        self.expect = expect
        self.created = datetime.utcnow()
        self.started_mono = time.monotonic()
        self.state = "starting"
        self.detail = ""
        self.frames: dict[str, dict] = {}
        self.sequence: str | None = None
        self.cancel_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.ended: datetime | None = None
        self.events: list[str] = []
        self.armer_status_fn: Callable[[], dict] | None = None
        self.reach_stalled = False   # PS-181: cooling stalled above the setpoint

    @property
    def active(self) -> bool:
        return self.state in ("starting", "cooling", "running", "stopping")

    @property
    def expected(self) -> int:
        return sum(n for _e, n in self.darks) + self.bias

    def log(self, msg: str) -> None:
        self.events.append(f"{datetime.utcnow():%H:%M:%S}Z {msg}")
        self.events = self.events[-60:]
        logger.info("calibration capture %s (%s): %s", self.id, self.rig, msg)

    def minutes(self) -> float:
        from photonscript.scheduler.calibration_plan import estimate_minutes
        return round(estimate_minutes(self.darks, self.bias), 1)

    def to_dict(self) -> dict:
        verdicts: dict[str, int] = {}
        for r in self.frames.values():
            v = r.get("verdict") or "unchecked"
            verdicts[v] = verdicts.get(v, 0) + 1
        return {"id": self.id, "rig": self.rig, "state": self.state,
                "detail": self.detail, "source": self.source,
                "darks": self.darks, "bias": self.bias, "probe": self.probe,
                "daytime": self.daytime, "setpoint": self.setpoint,
                "tol_c": self.tol, "budget_min": self.budget_min,
                "estimated_minutes": self.minutes(),
                "created": self.created.isoformat(timespec="seconds") + "Z",
                "ended": (self.ended.isoformat(timespec="seconds") + "Z"
                          if self.ended else None),
                "elapsed_min": round((time.monotonic() - self.started_mono) / 60, 1),
                "landed": len(self.frames), "expected": self.expected,
                "verdicts": verdicts, "sequence": self.sequence,
                "bad": [{"file": r.get("name"), "reasons": r.get("reasons")}
                        for r in self.frames.values() if r.get("verdict") == "fail"][:20],
                "events": self.events[-15:]}


def _history_path(config) -> Path:
    from photonscript.scheduler.calibration_qa import qa_dir
    return qa_dir(config) / "capture_jobs.jsonl"


def _record(config, job: Job) -> None:
    try:
        p = _history_path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(job.to_dict(), default=str) + "\n")
    except OSError as e:
        logger.warning("capture job history not written: %s", e)


def status(config=None, rig: str | None = None) -> dict:
    jobs = {r: j.to_dict() for r, j in _jobs.items() if rig in (None, r)}
    return {"jobs": jobs, "busy": {r: busy(r) for r in jobs}}


def sun_alt_now(config, now: datetime | None = None) -> float | None:
    from photonscript.scheduler.calibration_qa import sun_altitudes
    t = (now or datetime.utcnow()).isoformat()
    try:
        return sun_altitudes(config, [t])[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("sun altitude: %s", e)
        return None


def is_daytime(config, now: datetime | None = None) -> bool:
    """Daytime capture rules apply unless it is properly night (sun below
    calibration_qa.NIGHT_SUN_ALT): twilight counts as day here (stricter)."""
    from photonscript.scheduler.calibration_qa import NIGHT_SUN_ALT
    alt = sun_alt_now(config, now)
    return alt is None or alt >= NIGHT_SUN_ALT  # unknown: treat as day


# --- start guards ------------------------------------------------------------------

async def preflight(config, rig: str, armer_state: str, io: RigIO | None = None,
                    daytime: bool | None = None, connect: bool = True,
                    reserved: bool = False) -> tuple[list[str], dict]:
    """Every reason a capture on `rig` is refused right now (empty = go),
    plus what was read. Reads only; with `connect` the one write is
    connecting the rig's own camera / safety monitor (connect=False: the
    page's read-only check)."""
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.shared.rigs import rig_ids, rig_label
    refusals: list[str] = []
    seen: dict = {"armer": armer_state}
    if rig not in rig_ids(config):
        return [f"rig {rig!r} is not enabled"], seen
    io = io or RigIO(config, rig)
    label = rig_label(config, rig)
    if armer_state not in IDLE_STATES:
        refusals.append(f"armer is {armer_state}: capture only when DISARMED or "
                        "COMPLETE (never while a night is armed or running)")
    if not reserved and busy(rig):
        refusals.append(f"a capture job is already running on {label}")
    if not getattr(cq.rig_view(config, rig), "image_watch_dir", ""):
        refusals.append(f"{label} has no image watch dir: frames could not be QA'd")
    if rig != "rc16" and not getattr(config, "piggyback_image_watch_dir", ""):
        refusals.append("piggyback_image_watch_dir is not set: the Piggy-600 frames "
                        "could not be found and QA'd")
    day = is_daytime(config) if daytime is None else daytime
    seen["daytime"] = day
    if day and not cq.daytime_capture_allowed(config, rig):
        st = cq.daytime_state(config, rig)
        refusals.append(f"daytime capture is DISABLED for {label} (light leak seen "
                        f"{st.get('at', '?')}): shoot at night with the roof closed, "
                        "or reset with calibration-qa --rig "
                        f"{rig} --reset-daytime after fixing the leak")
    if rig == "rc16":
        try:
            from photonscript.telescope_agent import phd2_ops
            if phd2_ops.busy():
                refusals.append(f"PhotonScript holds PHD2 ({phd2_ops.owner()})")
        except Exception:  # noqa: BLE001
            pass
        g = await io.guider_state()
        seen["guider"] = g
        if g in GUIDER_BUSY_STATES:
            refusals.append(f"PHD2 is {g} on the RC16")
    running = await io.sequence_running()
    seen["sequence_running"] = running
    if running is None:
        refusals.append(f"cannot read {label}'s NINA sequence state (is NINA up?)")
    elif running:
        refusals.append(f"{label}'s NINA is running a sequence; stop it first")
    closed, src = await io.roof(connect=connect)
    seen["roof"] = {"closed": closed, "source": src}
    if closed is None:
        refusals.append(f"roof state unknown ({src}): darks need the roof reading "
                        "CLOSED")
    elif not closed:
        refusals.append(f"roof reads OPEN / safe ({src}): darks and bias need the "
                        "roof closed")
    if connect:
        ok, err = await io.connect_camera()
    else:
        cam = await io.camera()
        ok = bool((cam or {}).get("Connected", cam is not None))
        err = "camera info unreadable" if cam is None else "not connected"
    seen["camera_connected"] = ok
    if not ok:
        refusals.append(f"{label} camera not connected ({err})")
    else:
        refusals += await _readout_check(config, rig, io, seen, label,
                                         cam if not connect else None)
        if day:
            refusals += await _reach_check(config, rig, io, seen, label)
    return refusals, seen


def reach_mode(config) -> str:
    m = str(getattr(config, "calibration_cool_reach", "refuse") or "off").strip().lower()
    return m if m in ("refuse", "warn", "off") else "refuse"


def _next_window_text(config, rig: str) -> tuple[str, dict | None]:
    try:
        from photonscript.scheduler.calibration_window import dawn_window
        from photonscript.shared.localtime import to_local
        w = dawn_window(config, rig)
        if not w:
            return "", None
        s = to_local(config, datetime.fromisoformat(w["start"].rstrip("Z")))
        e = to_local(config, datetime.fromisoformat(w["end"].rstrip("Z")))
        pred = ("" if w.get("reachable") is None else
                f", predicted {'reachable' if w['reachable'] else 'NOT reachable'}"
                + (f" at {w['power_pct']:g} % TEC" if w.get("power_pct") is not None
                   else ""))
        return (f" Re-planned for dawn {s:%H:%M}-{e:%H:%M} local{pred}"
                + (" (calibration_dawn_capture starts it)"
                   if getattr(config, "calibration_dawn_capture", False)
                   else " (start it then, or turn on calibration_dawn_capture)")), w
    except Exception as e:  # noqa: BLE001
        logger.debug("dawn window: %s", e)
        return "", None


async def _reach_check(config, rig: str, io, seen: dict, label: str) -> list[str]:
    """PS-181: refuse a daytime start the cooler cannot serve: the TEC
    already reads flat out above the setpoint, or the cooler history
    predicts more than cooler_reach_max_power_pct at the live ambient.
    Unknown (no history, no ambient) never refuses."""
    from photonscript.scheduler import cooler_history as ch
    from photonscript.shared.rigs import rig_setpoint
    m = reach_mode(config)
    if m == "off":
        return []
    sp = rig_setpoint(config, rig)
    tol = float(getattr(config, "calibration_temp_tol_c", 1.0) or 1.0)
    try:
        cam = await io.camera()
        amb = await io.focuser_temp() if hasattr(io, "focuser_temp") else None
    except Exception:  # noqa: BLE001
        return []
    ch.record(config, rig, cam, focuser_temp=amb, source="capture-preflight")
    why = None
    if ch.live_saturated(cam, sp, tol):
        why = (f"{label} TEC is flat out ({cam.get('CoolerPower')} %) with the "
               f"sensor at {cam.get('Temperature')} C, above {sp:g} C")
    else:
        pred = await asyncio.to_thread(ch.reachability, config, rig, ambient=amb)
        seen["reach"] = {k: pred.get(k) for k in ("reachable", "power_pct",
                                                    "ambient_c", "why")}
        if pred.get("reachable") is False:
            why = f"{label} cooler cannot reach {sp:g} C now: {pred['why']}"
    if why is None:
        return []
    txt, w = _next_window_text(config, rig)
    seen["reach_refusal"] = why
    seen["dawn_window"] = w
    if m == "warn":
        logger.warning("calibration capture (warn only): %s", why)
        return []
    return [why + "." + txt]


async def _readout_check(config, rig: str, io, seen: dict, label: str,
                         cam: dict | None) -> list[str]:
    """PS-128: refuse when NINA would shoot the darks / bias at another
    readout mode than the rig's lights (fail open when NINA does not say)."""
    from photonscript.shared.rigs import camera_info_readout, rig_readout
    want = rig_readout(config, rig)
    if not want:
        return []
    if cam is None:
        cam = await io.camera()
    have, raw = camera_info_readout(cam)
    seen["readout"] = {"nina": raw, "want": want}
    if have is None or have == want:
        return []
    key = "piggyback_readout_mode" if rig != "rc16" else "camera_readout_mode"
    return [f"{label} NINA readout mode for sequence images is {raw}, the lights "
            f"use {want} ({key}): darks and bias at {have} would not count. Set "
            "it in NINA Options > Equipment > Camera (readout mode for "
            "sequence images), or change " + key]


def _probe_block(config, rig: str, darks: list) -> list | None:
    """[exp, PROBE_COUNT] at the longest exposure with >= 3 QA-passed NIGHT
    darks of the rig's epoch, or None when there is no night reference."""
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.scheduler.calibration_plan import rig_epoch
    ep = rig_epoch(config, rig)
    nights: dict[float, int] = {}
    for r in cq.load_store(config, rig)["frames"].values():
        if (r.get("type") == "DARK" and cq.passed(r) and r.get("sun_alt") is not None
                and r["sun_alt"] < cq.NIGHT_SUN_ALT and r.get("gain") == ep["gain"]
                and r.get("offset") == ep["offset"]):
            e = round(r.get("exptime") or 0, 1)
            nights[e] = nights.get(e, 0) + 1
    refs = [e for e, n in nights.items() if n >= 3]
    return [max(refs), PROBE_COUNT] if refs else None


def build_plan(config, rig: str, *, darks=None, bias=None, exposures=None,
               count=None, budget: float | None = None) -> dict:
    """The job's darks + bias: explicit `darks` [[exp, n]], else `exposures`
    x `count` (default the dark quota), else the gap report's capture plan.
    Trimmed to the budget (less the cooling allowance)."""
    from photonscript.scheduler import calibration_plan as cp
    budget = budget_min(config) if budget is None else float(budget)
    room = max(0.0, budget - cp.COOL_ESTIMATE_MIN)
    if darks:
        d = [[float(e), int(n)] for e, n in darks]
        b = int(bias or 0)
    elif exposures:
        n = int(count or getattr(config, "dark_target_count", 30))
        d = [[float(e), n] for e in exposures]
        b = int(bias or 0)
    else:
        rep = cp.gap_report(config, rig)
        sets = rep["rigs"][0]["sets"] if rep["rigs"] else []
        plan = cp.capture_plan(sets, budget_min=room, count=count)
        d, b = plan["darks"], plan["bias"] if bias is None else int(bias)
    # trim explicit plans to the budget too
    sets = [{"type": "DARK", "exp_s": e, "need": n, "gap": n, "capturable": True}
            for e, n in d]
    if b:
        sets.append({"type": "BIAS", "exp_s": 0.001, "need": b, "gap": b,
                     "capturable": True})
    trimmed = cp.capture_plan(sets, budget_min=room)
    # keep the caller's order (capture_plan sorts by gap share, all 1.0 here)
    order = {e: i for i, (e, _n) in enumerate(d)}
    out = sorted(trimmed["darks"], key=lambda x: order.get(x[0], 99))
    return {"darks": out, "bias": trimmed["bias"], "trimmed": trimmed["trimmed"],
            "minutes": trimmed["minutes"]}


async def start_job(config, rig: str, *, armer_state_fn: Callable[[], str],
                    darks=None, bias=None, exposures=None, count=None,
                    budget: float | None = None, source: str = "manual",
                    io: RigIO | None = None, poll_s: float = POLL_S,
                    daytime: bool | None = None,
                    armer_status_fn: Callable[[], dict] | None = None
                    ) -> tuple[bool, dict]:
    """Check the guards, build the plan and start the job task. Returns
    (ok, body): body has "refusals" when refused, else the job dict.
    armer_status_fn (Armer.status) lets a job that the armer interrupts stop
    its own sequence while tonight's dispatch is still far off."""
    from photonscript.shared.rigs import rig_label
    if busy(rig):
        return False, {"rig": rig, "refusals": [
            f"a capture job is already running on {rig_label(config, rig)}"], "read": {}}
    _starting.add(rig)   # no second job slips in while this one is checked
    try:
        io = io or RigIO(config, rig)
        day = (await asyncio.to_thread(is_daytime, config)) if daytime is None else daytime
        refusals, seen = await preflight(config, rig, armer_state_fn(), io, daytime=day,
                                         reserved=True)
        if refusals:
            if seen.get("reach_refusal") and not str(source).startswith("deferred"):
                _defer_plan(config, rig, darks=darks, bias=bias, exposures=exposures,
                            count=count, budget=budget, reason=seen["reach_refusal"],
                            window=seen.get("dawn_window"))
            return False, {"rig": rig, "refusals": refusals, "read": seen}
        return await _start(config, rig, armer_state_fn, io, poll_s, day, seen,
                            darks=darks, bias=bias, exposures=exposures, count=count,
                            budget=budget, source=source,
                            armer_status_fn=armer_status_fn)
    finally:
        _starting.discard(rig)


async def _start(config, rig, armer_state_fn, io, poll_s, day, seen, *, darks, bias,
                 exposures, count, budget, source,
                 armer_status_fn=None) -> tuple[bool, dict]:
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.shared.rigs import rig_setpoint
    budget = budget_min(config) if budget is None else float(budget)
    plan = await asyncio.to_thread(build_plan, config, rig, darks=darks, bias=bias,
                                   exposures=exposures, count=count, budget=budget)
    blocks = plan["darks"]
    if not blocks and not plan["bias"]:
        return False, {"rig": rig, "refusals": [
            "nothing to capture: every dark set and the bias are at quota "
            "(or the budget is too small)"], "read": seen}
    probe = None
    if day and cq.daytime_state(config, rig).get("status") == "untested":
        probe = _probe_block(config, rig, blocks)
        if probe:
            rest = []
            for e, n in blocks:
                if abs(e - probe[0]) < 0.5:
                    n = max(0, n - probe[1])
                if n:
                    rest.append([e, n])
            blocks = [probe] + rest
    from photonscript.scheduler.calibration_plan import rig_epoch
    ep = rig_epoch(config, rig)
    job = Job(rig, blocks, plan["bias"], source=source, budget_min=budget,
              setpoint=rig_setpoint(config, rig), tol=cq.temp_tol(config),
              daytime=day, probe=probe,
              expect={"gain": ep["gain"], "offset": ep["offset"], "xbin": 1,
                      "readout": ep.get("readout"),
                      "exposures": sorted({e for e, _n in blocks})})
    if plan["trimmed"]:
        job.log(f"plan trimmed to the {budget:g} min budget")
    if probe:
        job.log(f"daytime probe first: {probe[1]} x {probe[0]:g} s darks vs night ones")
    job.armer_status_fn = armer_status_fn
    _jobs[rig] = job
    job.task = asyncio.create_task(_run(config, job, io, armer_state_fn, poll_s))
    return True, job.to_dict()


def _defer_plan(config, rig: str, *, darks=None, bias=None, exposures=None,
                count=None, budget=None, reason: str = "", window=None) -> None:
    """PS-181: keep the plan a refused / stalled job would have shot for the
    rig's dawn window (calibration_window). Never raises."""
    try:
        from photonscript.scheduler.calibration_window import defer
        if darks is None and bias is None:
            plan = build_plan(config, rig, darks=darks, bias=bias, exposures=exposures,
                              count=count, budget=budget)
            darks, bias = plan["darks"], plan["bias"]
        elif darks is None:
            darks = []
        defer(config, rig, darks, int(bias or 0), reason, window)
    except Exception as e:  # noqa: BLE001
        logger.warning("deferred calibration plan not saved: %s", e)


async def cancel(rig: str) -> dict:
    j = _jobs.get(rig)
    if j is None or not j.active:
        return {"ok": False, "detail": f"no capture job running on {rig}"}
    j.cancel_event.set()
    j.log("cancel requested")
    return {"ok": True, "job": j.to_dict()}


# --- the run -----------------------------------------------------------------------

def _scan_new(config, rig: str, since_ts: float, known: set) -> list:
    """Calibration frames the rig's NINA wrote since the job started:
    [(TYPE, date folder, path)] in write order."""
    from photonscript.scheduler.calibration import _norm_cal_type
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.scheduler.runs import _CAL_DIRS
    watch = Path(cq.rig_view(config, rig).image_watch_dir)
    if not watch.exists():
        return []
    floor = (datetime.fromtimestamp(since_ts) - timedelta(days=1)).strftime("%Y-%m-%d")
    out = []
    for d in sorted(p for p in watch.iterdir() if p.is_dir() and p.name >= floor):
        for f in d.rglob("*.fits"):
            if str(f) in known:
                continue
            parts = f.relative_to(d).parts
            typ = next((_norm_cal_type(p) for p in parts if p.upper() in _CAL_DIRS), None)
            if typ not in ("DARK", "BIAS"):
                continue
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if mt >= since_ts - 2:
                out.append((mt, typ, d.name, f))
    out.sort(key=lambda x: x[0])
    return [(t, d, f) for _m, t, d, f in out]


def _file_frames(config, rig: str, nights: set) -> None:
    """File the job's nights into the Library (QA'd, bad ones quarantined)."""
    from photonscript.scheduler import runs
    for d in sorted(nights):
        try:
            if rig == "rc16":
                runs._link_calibration_night(config, Path(config.image_watch_dir),
                                             runs.library_root(config), d, rig="rc16")
            else:
                runs._build_piggyback_calibration(config, d)
        except Exception as e:  # noqa: BLE001
            logger.warning("filing %s %s failed: %s", rig, d, e)


def cool_timeout_min(config) -> float:
    """calibration_cool_timeout_min, 0 / unset = COOL_TIMEOUT_MIN."""
    try:
        v = float(getattr(config, "calibration_cool_timeout_min", 0.0) or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return v if v > 0 else COOL_TIMEOUT_MIN


def stalled(trace: list, setpoint: float, tol: float, now_mono: float,
            stall_min: float, started: float) -> bool:
    """PS-181: cooling for at least stall_min, the sensor still above the
    setpoint + tol and it fell less than STALL_DROP_C over the last
    STALL_WINDOW_S. trace = [(monotonic, temp)]."""
    if stall_min <= 0 or not trace or now_mono - started < stall_min * 60:
        return False
    t_now = trace[-1][1]
    if t_now <= setpoint + tol:
        return False
    old = [t for m, t in trace if now_mono - m >= STALL_WINDOW_S]
    if not old:
        return False
    return (old[-1] - t_now) < STALL_DROP_C


async def _wait_cooled(config, job: Job, io: RigIO, armer_state_fn, poll_s) -> str | None:
    """Cool and wait for two in-tolerance reads. Returns an abort reason or
    None when the sensor is at the setpoint. PS-181: a stall (the sensor
    stopped falling above the setpoint) aborts after
    calibration_cool_stall_min when calibration_cool_reach is refuse (warn
    and off wait for the timeout as before)."""
    from photonscript.scheduler import cooler_history as ch
    await io.cool(job.setpoint)
    ok_reads = 0
    t0 = time.monotonic()
    deadline = t0 + cool_timeout_min(config) * 60
    stall_min = (float(getattr(config, "calibration_cool_stall_min", 6.0) or 0.0)
                 if reach_mode(config) == "refuse" else 0.0)
    trace: list = []
    while True:
        if job.cancel_event.is_set():
            return "cancelled"
        st = armer_state_fn()
        if st not in IDLE_STATES:
            return f"armer went {st}"
        cam = await io.camera() or {}
        t = cam.get("Temperature")
        ch.record(config, job.rig, cam, source="capture-cooling")
        if t is not None:
            try:
                ok_reads = ok_reads + 1 if abs(float(t) - job.setpoint) <= job.tol else 0
                trace.append((time.monotonic(), float(t)))
            except (TypeError, ValueError):
                ok_reads = 0
        job.detail = f"cooling: sensor {t} C, setpoint {job.setpoint:g} C"
        if ok_reads >= 2:
            job.log(f"sensor at {t} C (setpoint {job.setpoint:g} +/- {job.tol:g})")
            return None
        if stalled(trace, job.setpoint, job.tol, time.monotonic(), stall_min, t0):
            pw = cam.get("CoolerPower")
            job.reach_stalled = True
            return (f"cooler stalled at {t} C"
                    + (f" (TEC {pw:.0f} %)" if isinstance(pw, (int, float)) else "")
                    + f": cannot reach {job.setpoint:g} +/- {job.tol:g} C now")
        if time.monotonic() > deadline:
            return (f"camera did not reach {job.setpoint:g} +/- {job.tol:g} C in "
                    f"{cool_timeout_min(config):g} min (last {t} C)")
        closed, src = await io.roof(connect=False)
        if closed is False:
            return f"roof opened ({src})"
        await asyncio.sleep(poll_s)


async def _run(config, job: Job, io: RigIO, armer_state_fn, poll_s: float) -> None:
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.scheduler.calibration import generate_darks_json
    from photonscript.shared.pushover import notify
    from photonscript.shared.rigs import rig_label
    label = rig_label(config, job.rig)
    view = cq.rig_view(config, job.rig)
    nights: set = set()
    stop_reason = None
    dispatched = False
    try:
        job.state = "cooling"
        job.log("cooling to setpoint")
        stop_reason = await _wait_cooled(config, job, io, armer_state_fn, poll_s)
        if stop_reason is None:
            closed, src = await io.roof(connect=False)
            running = await io.sequence_running()
            if closed is not True:
                stop_reason = f"roof not reading closed before dispatch ({src})"
            elif running is not False:
                stop_reason = "NINA busy or unreadable before dispatch"
        if stop_reason is None:
            seq_text, _m = generate_darks_json(
                view, [(e, n) for e, n in job.darks], job.bias, safety_gated=True,
                warm_minutes=float(getattr(config, "gradual_warm_minutes", 0.0)))
            try:
                seq_dir = Path.cwd() / "sequences"
                seq_dir.mkdir(exist_ok=True)
                path = seq_dir / f"calibration_capture_{job.rig}_{datetime.now():%Y%m%d_%H%M}.json"
                path.write_text(seq_text, encoding="utf-8")
                job.sequence = path.name
            except OSError:
                pass
            res = await io.dispatch(json.loads(seq_text))
            if not res.get("ok"):
                job.state = "failed"
                job.detail = f"NINA dispatch failed: {res.get('detail')}"
                job.log(job.detail)
                return
            dispatched = True
            job.state = "running"
            job.log(f"dispatched: {', '.join(f'{e:g} s x {n}' for e, n in job.darks)}"
                    + (f" + {job.bias} bias" if job.bias else "")
                    + f" (~{job.minutes():.0f} min)")
            await notify(config, f"{label} calibration capture started: "
                         + ", ".join(f"{e:g}s x{n}" for e, n in job.darks)
                         + (f" + {job.bias} bias" if job.bias else "")
                         + f", about {job.minutes():.0f} min. Roof must stay closed.",
                         title="PhotonScript calibration")
            stop_reason = await _monitor(config, job, io, armer_state_fn, poll_s, nights)
    except asyncio.CancelledError:
        stop_reason = "service stopping"
        raise
    except Exception as e:  # noqa: BLE001
        stop_reason = f"error: {type(e).__name__}: {e}"
        logger.exception("calibration capture %s failed", job.id)
    finally:
        await _finish(config, job, io, stop_reason, dispatched, nights, label)


async def _monitor(config, job: Job, io: RigIO, armer_state_fn, poll_s: float,
                   nights: set) -> str | None:
    """Watch the running job. Returns a stop reason, or None when it ended
    normally (every expected frame landed, or NINA finished)."""
    from photonscript.scheduler import calibration_qa as cq
    roof_miss = temp_bad = cam_miss = light = nina_miss = idle = 0
    t0 = time.monotonic()
    known: set = set()
    since = time.time() - (time.monotonic() - job.started_mono)
    while True:
        if job.cancel_event.is_set():
            return "cancelled"
        if (time.monotonic() - job.started_mono) / 60.0 > job.budget_min:
            return f"time budget {job.budget_min:g} min used up"
        st = armer_state_fn()
        if st not in IDLE_STATES:
            return f"armer went {st}"
        closed, src = await io.roof(connect=False)
        if closed is False:
            return f"roof opened ({src})"
        roof_miss = roof_miss + 1 if closed is None else 0
        if roof_miss >= ROOF_MISS_LIMIT:
            return f"roof state unreadable {roof_miss} polls running"
        cam = await io.camera()
        t = (cam or {}).get("Temperature")
        from photonscript.scheduler.cooler_history import record as _hist
        _hist(config, job.rig, cam, source="capture-job")   # PS-181
        if t is None:
            cam_miss += 1
            if cam_miss >= ROOF_MISS_LIMIT:
                return "camera unreadable"
        else:
            cam_miss = 0
            try:
                drift = abs(float(t) - job.setpoint) > job.tol
            except (TypeError, ValueError):
                drift = True
            temp_bad = temp_bad + 1 if drift else 0
            if temp_bad >= TEMP_DRIFT_LIMIT:
                return (f"sensor {t} C outside {job.setpoint:g} +/- {job.tol:g} C "
                        f"{temp_bad} polls running")
        new = await asyncio.to_thread(_scan_new, config, job.rig, since, known)
        if new:
            for _t, d, f in new:
                known.add(str(f))
                nights.add(d)
            recs = await asyncio.to_thread(cq.qa_frames, config, job.rig, new,
                                           expect=job.expect)
            for typ, d, f in new:
                r = recs.get(cq.frame_key(typ, d, f.name))
                if r is None:
                    continue
                job.frames[cq.frame_key(typ, d, f.name)] = r
                lit = [c for c in r.get("codes") or [] if c in cq.LIGHT_CODES]
                light = light + 1 if lit else 0
                if r.get("verdict") == "fail":
                    job.log(f"{f.name}: FAIL {'; '.join(r.get('reasons') or [])}")
            if light >= LIGHT_STREAK_LIMIT:
                return f"light in the frames ({light} in a row fail a light check)"
            if job.daytime and not cq.daytime_capture_allowed(config, job.rig):
                return "daytime light leak: day darks brighter than night darks"
        job.detail = (f"{len(job.frames)} of {job.expected} frames, sensor {t} C, "
                      f"roof closed ({src})")
        if len(job.frames) >= job.expected:
            job.log("every frame landed")
            return None
        running = await io.sequence_running()
        if running is None:
            nina_miss += 1
            if nina_miss >= NINA_MISS_LIMIT:
                return "NINA unreachable"
        else:
            nina_miss = 0
            if not running and time.monotonic() - t0 > 2 * poll_s:
                idle += 1
                if idle >= 2:
                    job.log(f"NINA sequence ended with {len(job.frames)} of "
                            f"{job.expected} frames")
                    return None
            else:
                idle = 0
        await asyncio.sleep(poll_s)


ARMER_STOP_MARGIN_MIN = 10.0


def _preconfig_far(job: "Job") -> bool:
    """The armer is ARMED and its pre-config dispatch is still more than
    ARMER_STOP_MARGIN_MIN away (needs the armer status; unknown = False)."""
    try:
        st = job.armer_status_fn() if job.armer_status_fn else None
        if not st or st.get("state") != "ARMED" or not st.get("preconfig_utc"):
            return False
        pre = datetime.fromisoformat(str(st["preconfig_utc"]).rstrip("Z"))
        return (pre - datetime.utcnow()).total_seconds() > ARMER_STOP_MARGIN_MIN * 60
    except Exception:  # noqa: BLE001
        return False


async def _finish(config, job: Job, io: RigIO, stop_reason, dispatched: bool,
                  nights: set, label: str) -> None:
    from photonscript.shared.pushover import notify
    # The armer took over (armed / running): it owns NINA and the coolers
    # now (arm() cuts them; a running night wants them cold), so never warm,
    # and only stop our sequence while tonight's dispatch is still more than
    # ARMER_STOP_MARGIN_MIN away (a stop could otherwise hit the night's own
    # freshly loaded sequence).
    armer_took_over = bool(stop_reason and stop_reason.startswith("armer went"))
    may_stop = dispatched
    if armer_took_over:
        may_stop = dispatched and _preconfig_far(job)
        job.log("armer took over: " + ("stopping our sequence, no warm" if may_stop
                                        else "leaving NINA and the cooler to it"))
    if stop_reason is not None:
        job.state = "stopping"
        job.log(f"stopping: {stop_reason}")
        if may_stop:
            try:
                await io.stop()
            except Exception as e:  # noqa: BLE001
                job.log(f"sequence stop failed: {e}")
    if not armer_took_over:
        try:
            await io.warm()
        except Exception as e:  # noqa: BLE001
            job.log(f"warm failed: {e}")
    if nights:
        await asyncio.to_thread(_file_frames, config, job.rig, nights)
    if job.reach_stalled and not dispatched and not job.source.startswith("deferred"):
        txt, w = _next_window_text(config, job.rig)   # PS-181
        _defer_plan(config, job.rig, darks=job.darks, bias=job.bias,
                    reason=stop_reason or "cooler stalled", window=w)
        if txt:
            job.log(txt.strip())
            stop_reason = (stop_reason or "") + "." + txt
    if job.state != "failed":
        if stop_reason is None:
            job.state = "complete"
        elif stop_reason == "cancelled":
            job.state = "cancelled"
        else:
            job.state = "aborted"
        job.detail = stop_reason or f"{len(job.frames)} of {job.expected} frames"
    job.ended = datetime.utcnow()
    _record(config, job)
    v = job.to_dict()["verdicts"]
    try:
        await notify(config, f"{label} calibration capture {job.state}: "
                     f"{len(job.frames)} of {job.expected} frames, "
                     f"{v.get('pass', 0) + v.get('warn', 0)} passed QA, "
                     f"{v.get('fail', 0)} quarantined"
                     + (f" ({stop_reason})" if stop_reason else "")
                     + ". Camera warmed.", title="PhotonScript calibration",
                     priority=1 if job.state == "aborted" else 0)
    except Exception:  # noqa: BLE001
        pass


# --- daytime auto-fill --------------------------------------------------------------

async def autofill_tick(config, armer_state_fn: Callable[[], str],
                        now: datetime | None = None, **kw: Any) -> dict:
    """calibration_autofill (default False): in the DAYTIME only (sun up),
    with the armer idle, start one gap-driven job per rig per day, budgeted
    to end 2 h before sunset. Never runs at night."""
    if not getattr(config, "calibration_autofill", False):
        return {"skipped": "calibration_autofill is off"}
    now = now or datetime.utcnow()
    alt = sun_alt_now(config, now)
    if alt is None or alt <= 0:
        return {"skipped": f"sun not up ({alt})"}
    if armer_state_fn() not in IDLE_STATES:
        return {"skipped": f"armer {armer_state_fn()}"}
    from photonscript.scheduler import calibration_plan as cp
    from photonscript.scheduler.night_plan import compute_night_times
    from photonscript.shared.localtime import to_local
    from photonscript.shared.rigs import rig_ids
    tw = compute_night_times(config.get_observatory(),
                             now.replace(hour=0, minute=0, second=0, microsecond=0))
    sunset = tw.get("sunset")
    if sunset is None:
        return {"skipped": "no sunset"}
    room = (sunset - timedelta(hours=2) - now).total_seconds() / 60.0
    budget = min(budget_min(config), room)
    if budget < 30:
        return {"skipped": f"only {budget:.0f} min before the sunset - 2 h cutoff"}
    today = to_local(config, now).strftime("%Y-%m-%d")
    out = {}
    for rig in rig_ids(config):
        if _autofill_done.get(rig) == today or busy(rig):
            out[rig] = "done today or busy"
            continue
        rep = cp.gap_report(config, rig)
        cap = rep["rigs"][0]["capture"] if rep["rigs"] else {}
        if not (cap.get("darks") or cap.get("bias")):
            out[rig] = "nothing to capture"
            _autofill_done[rig] = today
            continue
        _autofill_done[rig] = today  # one attempt a day, refused or not
        ok, body = await start_job(config, rig, armer_state_fn=armer_state_fn,
                                   budget=budget, source="autofill", daytime=True, **kw)
        out[rig] = body if not ok else {"started": body["id"]}
    return out


async def autofill_loop(config_fn, armer_fn, tick_s: float = 600.0) -> None:
    """Started with the service; idles each tick unless calibration_autofill
    (or, PS-181, calibration_dawn_capture with a deferred plan)."""
    while True:
        try:
            cfg = config_fn()
            if getattr(cfg, "calibration_autofill", False):
                res = await autofill_tick(cfg, lambda: armer_fn().state,
                                          armer_status_fn=lambda: armer_fn().status())
                logger.info("calibration autofill: %s", res)
            if getattr(cfg, "calibration_dawn_capture", False):   # PS-181
                from photonscript.scheduler.calibration_window import dawn_tick
                res = await dawn_tick(cfg, lambda: armer_fn().state,
                                      armer_status_fn=lambda: armer_fn().status())
                logger.info("calibration dawn capture: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("calibration autofill tick failed: %s", e)
        await asyncio.sleep(tick_s)
