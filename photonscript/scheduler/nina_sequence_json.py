"""Generate NINA Advanced Sequencer JSON files.

Schema is modeled on a sequence exported from the AARO scope PC's own NINA
3.2 install (the 'M42 2026-02-27-runtime' reference), so every $type below is
known-good against the exact deserializer that will load it. Key learnings
baked in from that reference:

  - SlewScopeToRaDec + Platesolving.Center (SlewScopeAndCenter does NOT exist)
  - CoolCamera/WarmCamera Duration is in MINUTES (2.0, not 120)
  - WaitForTime uses a DuskProvider so NINA recomputes dusk itself nightly
  - AltitudeCondition needs the full WaitLoopData (coordinates + offset)
  - FilterInfo is NINA.Core.Model.Equipment.FilterInfo with _name/_position
  - SmartExposure = LoopCondition(iterations) + SwitchFilter + TakeExposure
  - Equipment must be explicitly connected in the start area (cold start)
  - GroundStation Pushover items narrate every phase for remote monitoring
"""

from __future__ import annotations

import copy
import json
from typing import Optional

from photonscript.shared.models import (
    NinaSequenceFile, NinaSequenceTarget, ExposurePlan, FilterType,
)
from photonscript.scheduler.nina_sequence import FILTER_POSITIONS

OBS_COLLECTION_ITEMS = ("System.Collections.ObjectModel.ObservableCollection`1"
                        "[[NINA.Sequencer.SequenceItem.ISequenceItem, NINA.Sequencer]],"
                        " System.ObjectModel")
OBS_COLLECTION_CONDITIONS = ("System.Collections.ObjectModel.ObservableCollection`1"
                             "[[NINA.Sequencer.Conditions.ISequenceCondition, NINA.Sequencer]],"
                             " System.ObjectModel")
OBS_COLLECTION_TRIGGERS = ("System.Collections.ObjectModel.ObservableCollection`1"
                           "[[NINA.Sequencer.Trigger.ISequenceTrigger, NINA.Sequencer]],"
                           " System.ObjectModel")

# --- Container names (PS-78) ---------------------------------------------------
# NINA's running-container name is what the telescope agent falls back to when
# a frame has no OBJECT, so these names end up in sub records and Library
# folders. shared/target_names.canonical_target imports them to map a
# container back to its target (or to "unattributed" for structural loops):
# rename a container here and the mapping follows.
TARGET_IMAGING_SUFFIX = " imaging (repeats while safe and up)"
# PS-180: leftover time at the end of the night goes back to the
# highest-priority target still up in a final DSO container
# "<target> fill (rest of the night)" (target.fill_from_utc).
TARGET_FILL_SUFFIX = " fill (rest of the night)"
TARGET_FOCUS_CAL_SUFFIX = " focus calibration AFs"
# PS-144: the armer's dusk calibration target (and the standalone download's
# night) is "Focus calibration <field>" (PS-152: a test target, see
# shared/target_names.is_test_target).
FOCUS_CAL_PREFIX = "Focus calibration "
# PS-84: the unguided tracking test. Its DeepSkyObjectContainer (and so every
# sub's OBJECT) is "Tracking test <field>", a name that never matches a
# project, so test subs stay out of goal sync and project stacks. The ladder
# container is "<that name> unguided ladder".
TRACKING_TEST_PREFIX = "Tracking test "
TARGET_TRACKING_LADDER_SUFFIX = " unguided ladder"
# PS-84 standalone download: after the ladder the night loop parks and holds.
# PS-127: a sideloaded test (scheduler/sideload.py) swaps this sentence for
# its own, since tonight's targets follow it there.
TRACKING_TEST_PARK_NOTE = ("When the ladder is done the scope parks and holds "
                           "until dawn: stop the sequence to image.")
# PS-148: the through-focus optics test. The test container is "Optics test
# <field>", its sweep "<that name> through-focus sweep", and every step a
# nested DeepSkyObjectContainer "<step> through-focus step" whose TargetName
# (and so each sub's OBJECT) is "Optics test <field> <filter> <offset>".
OPTICS_TEST_PREFIX = "Optics test "
TARGET_OPTICS_SWEEP_SUFFIX = " through-focus sweep"
OPTICS_STEP_SUFFIX = " through-focus step"
OPTICS_TEST_PARK_NOTE = ("When the sweep is done the scope parks and holds "
                         "until dawn: stop the sequence to image.")
# PS-171: the TPoint mapping run. The target container is "TPoint mapping
# <n> points", its point loop "<that name> point loop", and every point a
# run-once container "TPoint point <i>/<n> alt <a> az <z> (<side>)".
TPOINT_MAPPING_PREFIX = "TPoint mapping "
TARGET_TPOINT_LOOP_SUFFIX = " point loop"
TPOINT_POINT_PREFIX = "TPoint point "
TPOINT_MAPPING_NOTE = ("No center, no sync, no meridian flip trigger: every "
                       "point is a blind Slew to Alt/Az, one short frame and "
                       "the tpoint-sample script.")
FILTER_UNTIL_MOONRISE_SUFFIX = " until moonrise"  # "<filter> until moonrise"
# PS-61: with the cooler gate on, each light block is its own container
# "<target> filter block (cooler-gated)" whose first item is the gate, so a
# gate SKIP interrupts just that block.
TARGET_BLOCK_SUFFIX = " filter block (cooler-gated)"
# PS-85: a guided target with some filter blocks unguided tonight (no real
# guide star through that filter) wraps each block in its own container
# "<target> filter block (guiding per block)": an unguided block starts with
# StopGuiding, a guided one with StartGuiding (a failure skips that block).
TARGET_GUIDE_BLOCK_SUFFIX = " filter block (guiding per block)"
# PS-176: with rc16_af_policy smart every light block is its own container
# "<target> filter block (focus by offset)": [cooler gate], SwitchFilter
# (or AF on the AF filter for a 3 nm filter with no measured offset), [guiding],
# MoveFocuserRelative(+offset), lights, MoveFocuserRelative(-offset). The
# focuser is back at the AF filter's best focus between blocks.
TARGET_SMART_BLOCK_SUFFIX = " filter block (focus by offset)"
# PS-176: a goal's HDR short subs (PS-47) are shot once per target visit, in
# "<target> HDR shorts (once per visit)" before the repeating imaging loop.
# Inside the loop NINA reset their LoopCondition on every pass, so the 12
# owed 30 s shorts per filter were re-shot each pass (2026-10-07: 225 of
# 281 M31 subs were 30 s R/G/B).
TARGET_HDR_SHORTS_SUFFIX = " HDR shorts (once per visit)"
# PS-26: a Piggy-600-driven target (centering mode on) gets a nested
# DeepSkyObjectContainer "<target> Piggy-600 center, pier West (before
# transit)" holding one Center on the pier-West coordinates; it runs once,
# only before the target's transit (the outer container's coordinates are
# the pier-East ones, which the meridian flip re-centers on).
PIGGY_WEST_CENTER_SUFFIX = " Piggy-600 center, pier West (before transit)"
DSO_CONTAINER_TYPE = ("NINA.Sequencer.Container.DeepSkyObjectContainer, "
                      "NINA.Sequencer")
# PS-160: the cooler-gated wrapper around the unsafe-time dark blocks
DARKS_AT_SETPOINT_NAME = "DARKS_AT_SETPOINT"
SAFE_LOOP_NAME = "SAFE_LOOP"
RESET_EQUIPMENT_NAME = "RESET_EQUIPMENT_ONCE_SAFE"
TARGETS_LOOP_NAME = "TARGETS_CONTAINER"
NIGHT_LOOP_NAME = "LOOP_ALL_NIGHT"
UNSAFE_BRANCH_NAME = "UNSAFE"
SAFE_LOOP_PACE_S = 60   # PS-149: the wait that ends every SAFE_LOOP pass
TARGET_IMAGING_PACE_S = 60   # PS-149: imaging loops with no AF / exposure
SMART_EXPOSURE_NAME = "Smart Exposure"
NIGHT_LOOP_CONTAINER_NAMES = (SAFE_LOOP_NAME, RESET_EQUIPMENT_NAME,
                              TARGETS_LOOP_NAME, NIGHT_LOOP_NAME,
                              UNSAFE_BRANCH_NAME, SMART_EXPOSURE_NAME)

# --- Guiding resilience knobs (2026-09-20) -----------------------------------
# On 2026-09-18 and -09-19 (both clear, roof open ~12.7 h) PHD2 never settled:
# 95 guide-start requests, 24 "timed-out waiting for guider to settle", 0
# successes. StartGuiding had Attempts=1 and ErrorBehavior=0 (continue-on-error),
# so a single failed settle was swallowed and the whole night exposed 900s subs
# effectively unguided -> every NB sub trailed and was rejected.
#
# GUIDING_STARTUP_ATTEMPTS: how many times NINA retries the guide-start+settle
#   before giving up. 3 gives the settle three shots (and, with ForceCalibration
#   on the night's first guided target, a fresh calibration to settle against).
GUIDING_STARTUP_ATTEMPTS = 3
# GUIDING_ERROR_BEHAVIOR: NINA InstructionErrorBehavior on StartGuiding.
# Enum ordinals VERIFIED against NINA source (isbeorn/nina, master:
# NINA.Sequencer/Utility/InstructionErrorBehavior.cs) - declared with no explicit
# values, so C# assigns in declaration order:
#     0 = ContinueOnError
#     1 = SkipInstructionSetOnError
#     2 = AbortOnError                 (NOT 3 - a natural guess would abort the night)
#     3 = SkipToSequenceEndInstructions
# StartGuiding.Execute throws SequenceEntityFailedException when the guider fails
# to start/settle; SequenceItem.Run retries Attempts times, then applies this
# behavior. 1 (SkipInstructionSetOnError) calls Parent.Interrupt() -> skips the
# rest of THIS target's container (its exposure blocks) and the night loop moves
# on to the next target, instead of dumping hours of unguided/trailed subs
# (the 09-18/-19 failure). It does NOT abort the sequence (that's 2) or jump to
# the end/park (3).
# Trade-off: on a night where guiding can't settle at all, this yields no data
# for that target rather than possibly-usable unguided subs (relevant once
# ProTrack is proven on the Paramount). Set back to 0 to keep exposing unguided.
GUIDING_ERROR_BEHAVIOR = 1


def _decompose_ra(ra_hours: float) -> dict:
    h = int(ra_hours)
    remainder = (ra_hours - h) * 60
    m = int(remainder)
    s = (remainder - m) * 60
    return {"RAHours": h, "RAMinutes": m, "RASeconds": round(s, 2)}


def _decompose_dec(dec_degrees: float) -> dict:
    sign = 1 if dec_degrees >= 0 else -1
    d_abs = abs(dec_degrees)
    d = int(d_abs)
    remainder = (d_abs - d) * 60
    m = int(remainder)
    s = (remainder - m) * 60
    return {"NegativeDec": dec_degrees < 0, "DecDegrees": sign * d,
            "DecMinutes": m, "DecSeconds": round(s, 2)}


def _coords(target) -> dict:
    return {"$type": "NINA.Astrometry.InputCoordinates, NINA.Astrometry",
            **_decompose_ra(target.ra_hours), **_decompose_dec(target.dec_degrees)}


def _make_typed(type_name: str, **kwargs) -> dict:
    obj = {"$type": type_name}
    obj.update(kwargs)
    return obj


def _items(values):  # ObservableCollection wrappers
    return {"$type": OBS_COLLECTION_ITEMS, "$values": values}


def _conditions(values):
    return {"$type": OBS_COLLECTION_CONDITIONS, "$values": values}


def _triggers(values):
    return {"$type": OBS_COLLECTION_TRIGGERS, "$values": values}


def _seq_container(name: str, items: list, conditions: list = None,
                   triggers: list = None,
                   container_type="NINA.Sequencer.Container.SequentialContainer, NINA.Sequencer",
                   **extra) -> dict:
    return _make_typed(
        container_type,
        Strategy=_make_typed("NINA.Sequencer.Container.ExecutionStrategy."
                             "SequentialStrategy, NINA.Sequencer"),
        Name=name,
        Conditions=_conditions(conditions or []),
        IsExpanded=True,
        Items=_items(items),
        Triggers=_triggers(triggers or []),
        ErrorBehavior=0,
        Attempts=1,
        **extra,
    )


def _trigger_runner(items: list = None) -> dict:
    return _seq_container(None, items or [])


_ENTITY_LISTS = ("Items", "Conditions", "Triggers")


def link_parents(root: dict) -> dict:
    """Give every sequence entity a NINA-native "$id" and every child a
    "Parent": {"$ref": <its container's $id>}, the way NINA's own exports do.

    PS-77 root cause: NINA's deserializer (SequenceJsonConverter + the
    *CreationConverter classes) sets an item's, condition's or trigger's
    Parent ONLY from this JSON reference; SequenceContainer.OnDeserialized
    does not re-attach children. Without it every entity loads with
    Parent == null, and then:
      - SequentialStrategy.CanContinue stops recursing at the running
        container, so a SmartExposure checks only its own LoopCondition and
        ignores Safety / Altitude / the dawn TimeCondition on every ancestor
        (2026-09-26: 38 min of subs with the roof closed, two more after the
        loop end);
      - the SafetyMonitorCondition (5 s) and TimeCondition (1 s) watchdogs
        require Parent != null and IsInRootContainer(Parent), so an in-flight
        exposure is never interrupted;
      - ancestor triggers (meridian flip, reconnect) never run mid-block,
        TakeExposure cannot find its DeepSkyObjectContainer (no OBJECT
        header, PS-51) and SkipInstructionSetOnError's Parent?.Interrupt()
        is a no-op.

    "$id" must be the FIRST key: Json.NET's Populate() only registers a
    reference id when "$id" is the object's first property. Only sequence
    entities (the root, members of Items / Conditions / Triggers, and trigger
    runners) get ids; other objects are emitted exactly as before. A trigger
    runner keeps Parent null (NINA constructs it unattached) while its own
    items point at it. Returns a new tree; the input is not modified."""
    counter = [0]

    def _entity(node: dict, parent_id) -> dict:
        counter[0] += 1
        nid = str(counter[0])
        new = {"$id": nid}
        new.update((k, v) for k, v in node.items() if k != "$id")
        new["Parent"] = {"$ref": parent_id} if parent_id is not None else None
        for key in _ENTITY_LISTS:
            coll = new.get(key)
            if isinstance(coll, dict) and isinstance(coll.get("$values"), list):
                coll = dict(coll)
                coll["$values"] = [_entity(ch, nid) if isinstance(ch, dict)
                                   else ch for ch in coll["$values"]]
                new[key] = coll
        runner = new.get("TriggerRunner")
        if isinstance(runner, dict):
            new["TriggerRunner"] = _entity(runner, None)
        return new

    return _entity(root, None)


# --- Instructions -----------------------------------------------------------

SOUND_NONE = 22  # GroundStation NotificationSound enum: silent


_gen_cfg_cache = None


_moon_window_cache: dict = {}


def _moon_window():
    import time as _t
    if _moon_window_cache.get("t", 0) > _t.time() - 1800:
        return _moon_window_cache["v"]
    try:
        from photonscript.scheduler.moon import moon_window_tonight
        v = moon_window_tonight(_gen_cfg())
    except Exception:  # noqa: BLE001
        v = {"available": False}
    _moon_window_cache.update(t=_t.time(), v=v)
    return v


def _gen_cfg():
    global _gen_cfg_cache
    if _gen_cfg_cache is None:
        from photonscript.shared.config import PhotonScriptConfig
        _gen_cfg_cache = PhotonScriptConfig()
    return _gen_cfg_cache


def _pushover(title: str, message: str, sound: int = SOUND_NONE) -> dict:
    """GroundStation Pushover — remote narration, always silent."""
    return _make_typed(
        "DaleGhent.NINA.GroundStation.SendToPushover.SendToPushover, "
        "DaleGhent.NINA.GroundStation",
        Title=title, Message=message, Priority=0,
        NotificationSound=SOUND_NONE,
        ErrorBehavior=0, Attempts=1)


def _connect(device: str) -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Connect.ConnectEquipment, "
                       "NINA.Sequencer", SelectedDevice=device,
                       ErrorBehavior=0, Attempts=1)


def _dew_heater(on: bool = True) -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Camera.DewHeater, "
                       "NINA.Sequencer", OnOff=on, ErrorBehavior=0, Attempts=1)


def _cool_camera(temp_c: float, duration_min: float = 0.0) -> dict:
    # Duration is MINUTES. 0 = drive straight to the setpoint (no forced ramp);
    # see config.cool_ramp_minutes. A ramp is what let the cooler fight the arm.
    return _make_typed("NINA.Sequencer.SequenceItem.Camera.CoolCamera, "
                       "NINA.Sequencer", Temperature=temp_c,
                       Duration=duration_min, ErrorBehavior=0, Attempts=1)


# PS-154: cooling never gates the night. NINA's CoolCamera (CameraVM
# RegulateTemperature, NINA 3.x source) sets the setpoint, then loops while
# the sensor is warmer than Temperature + 1 C. Its only way out besides
# success is a 2 min "idle" timeout that counts only while the cooler sits at
# <1 % or >99 % power with no progress, so a cooler regulating at a warmer
# setpoint (2026-10-06: item at -10 C, PhotonScript holding the driver at the
# configured 0 C, sensor 0.9 C) waits forever: no unpark, no lights. Every
# generated CoolCamera therefore (a) cools to the rig's configured setpoint
# and (b) sits in its own run-once container with a TimeSpanCondition: that
# condition's 1 s ConditionWatchdog calls Parent.Interrupt() when the span
# runs out (needs Parent links, PS-77), the container ends and the sequence
# moves on. An interrupted CoolCamera sets the driver setpoint to the current
# sensor temperature; the armer's cooler nanny re-asserts the configured
# setpoint on its next tick, and the PS-61 gate still holds each light block
# (bounded too).
COOL_BOUNDED_PREFIX = "COOL_CAMERA (bounded"   # container name prefix, lint


def cool_bound_minutes(cfg=None) -> int:
    """The bound on a cooling wait: cooler_gate_timeout_min (default 20),
    whole minutes, at least 1."""
    cfg = cfg if cfg is not None else _gen_cfg()
    try:
        v = float(getattr(cfg, "cooler_gate_timeout_min", 20.0))
    except (TypeError, ValueError):
        v = 20.0
    if v != v:   # NaN
        v = 20.0
    return max(1, int(round(v)))


def rig_cool_setpoint(cfg=None, rig: str = "rc16", target=None) -> float:
    """PS-154: the one temperature a generated sequence cools to, the rig's
    configured setpoint (RC16 camera_setpoint_c, Piggy-600
    piggyback_setpoint_c, shared.rigs.rig_setpoint; a rig_config view already
    carries its own setpoint as camera_setpoint_c). A per-target
    camera_temp_c that differs is ignored with a log line: the dark library,
    the cooler nanny, the PS-61 gate and grading all key on the configured
    setpoint, and a per-target value is exactly what stalled 2026-10-06."""
    import logging
    from photonscript.shared.rigs import rig_setpoint
    cfg = cfg if cfg is not None else _gen_cfg()
    sp = float(rig_setpoint(cfg, rig))
    t = getattr(target, "camera_temp_c", None) if target is not None else None
    if t is not None and abs(float(t) - sp) > 0.05:
        logging.getLogger(__name__).warning(
            "%s: camera_temp_c %g C ignored, cooling to the configured %s "
            "setpoint %g C (PS-154)", getattr(target, "name", "target"),
            float(t), rig, sp)
    return sp


def _cool_camera_bounded(temp_c: float, duration_min: float = 0.0,
                         cfg=None, bound_min: int | None = None) -> dict:
    """PS-154: CoolCamera inside a run-once container whose TimeSpanCondition
    interrupts it after the ramp (duration_min) plus bound_min (default
    cool_bound_minutes), so the sequence proceeds even if the setpoint is
    never reached. The ramp is added because the condition also refuses to
    START an item whose estimated duration (the ramp) exceeds the span."""
    import math
    n = int(bound_min if bound_min is not None else cool_bound_minutes(cfg))
    n += int(math.ceil(max(0.0, float(duration_min or 0.0))))
    span = _make_typed("NINA.Sequencer.Conditions.TimeSpanCondition, "
                       "NINA.Sequencer", Hours=n // 60, Minutes=n % 60,
                       Seconds=0)
    return _seq_container(
        f"{COOL_BOUNDED_PREFIX} {n} min, then continue)",
        [_cool_camera(temp_c, duration_min)],
        conditions=[_loop_once(), span])


def _warm_camera(duration_min: float = 0.0) -> dict:
    # duration_min=0 -> instant warm: cut the TEC now, no gradual ramp (a ramp
    # fights the next arm/precool). See config.gradual_warm_minutes.
    return _make_typed("NINA.Sequencer.SequenceItem.Camera.WarmCamera, "
                       "NINA.Sequencer", Duration=duration_min,
                       ErrorBehavior=0, Attempts=1)


def _wait_for_dusk(minutes_offset: int = 0) -> dict:
    """WaitForTime bound to NINA's own DuskProvider — recomputed nightly."""
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Utility.WaitForTime, NINA.Sequencer",
        Hours=0, Minutes=0, MinutesOffset=minutes_offset, Seconds=0,
        SelectedProvider=_make_typed(
            "NINA.Sequencer.Utility.DateTimeProvider.DuskProvider, NINA.Sequencer"),
        ErrorBehavior=0, Attempts=1)


def _unpark() -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Telescope.UnparkScope, "
                       "NINA.Sequencer", ErrorBehavior=0, Attempts=1)


def _park() -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Telescope.ParkScope, "
                       "NINA.Sequencer", ErrorBehavior=0, Attempts=1)


def _set_tracking(mode: int) -> dict:
    """0 = sidereal, 5 = stopped."""
    return _make_typed("NINA.Sequencer.SequenceItem.Telescope.SetTracking, "
                       "NINA.Sequencer", TrackingMode=mode,
                       ErrorBehavior=0, Attempts=2)


def _slew(target) -> dict:
    """SlewScopeToRaDec — SlewScopeAndCenter does not exist in NINA 3.2."""
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Telescope.SlewScopeToRaDec, NINA.Sequencer",
        Inherited=True, Coordinates=_coords(target), ErrorBehavior=0, Attempts=2)


def _center(target) -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Platesolving.Center, NINA.Sequencer",
        Inherited=True, Coordinates=_coords(target), ErrorBehavior=0, Attempts=2)


def _autofocus() -> dict:
    # Attempts=2: if the first autofocus run fails (e.g. too few stars because
    # the scope drifted off focus), retry once before giving up rather than
    # silently continuing to image out of focus (the donut-night failure).
    return _make_typed("NINA.Sequencer.SequenceItem.Autofocus.RunAutofocus, "
                       "NINA.Sequencer", ErrorBehavior=0, Attempts=2)


def _move_focuser(position: int) -> dict:
    """Seed the focuser to a known-good absolute position before autofocus so
    AF starts with tight stars (see focus_seeds.py)."""
    return _make_typed("NINA.Sequencer.SequenceItem.Focuser."
                       "MoveFocuserAbsolute, NINA.Sequencer",
                       Position=int(position), ErrorBehavior=0, Attempts=1)


def _move_focuser_relative(steps: int) -> dict:
    """Relative focuser move: applies a measured filter focus offset right
    after an autofocus on the AF filter (PS-65). Never used as a seed."""
    return _make_typed("NINA.Sequencer.SequenceItem.Focuser."
                       "MoveFocuserRelative, NINA.Sequencer",
                       RelativePosition=int(steps), ErrorBehavior=0, Attempts=1)


def _focus_offset(af_filter, imaging_filter, offsets: dict | None) -> int:
    """EAF steps to move after an AF on af_filter so imaging_filter is in focus:
    offsets are relative to the AF filter's best focus, so the delta is
    offset[imaging] - offset[af]. 0 when there is no AF filter, the filters
    match, or no offsets are configured."""
    if af_filter is None or not offsets or af_filter == imaging_filter:
        return 0
    return int(offsets.get(imaging_filter.value, 0)) \
        - int(offsets.get(af_filter.value, 0))


_FOCTEMP_CACHE = {"t": 0.0, "v": None}


def _current_focuser_temp(config) -> float | None:
    """Best-effort current focuser temperature — a proxy for tonight's ambient,
    so seed_for() can temperature-interpolate the seed instead of always using
    the median. Cached 60s (one sequence build = one read). Returns None on any
    failure, which falls seed_for() back to the median seed (prior behavior)."""
    import time as _t
    if _t.time() - _FOCTEMP_CACHE["t"] < 60:
        return _FOCTEMP_CACHE["v"]
    v = None
    try:
        import math

        import httpx
        base = str(getattr(config, "nina_base_url", "")).rstrip("/")
        if base:
            # PS-76 part 2: ninaAPI v2 serves the focuser at
            # /equipment/focuser/info; the bare /equipment/focuser 404s, so
            # this always returned None and every seed fell to the median.
            r = httpx.get(base + "/equipment/focuser/info", timeout=4)
            d = r.json()
            payload = d.get("Response", d) if isinstance(d, dict) else {}
            t = payload.get("Temperature") if isinstance(payload, dict) \
                else None
            v = float(t) if t is not None else None
            if v is not None and not math.isfinite(v):
                v = None   # focuser connected without a sensor reading
    except Exception:  # noqa: BLE001
        v = None
    _FOCTEMP_CACHE.update(t=_t.time(), v=v)
    return v


def _seed_position(filter_type, ambient_c=None) -> int:
    """Best-guess focuser start position for a filter, temperature-compensated.
    Passes config (so harvested per-site seeds in data_dir load) and the current
    focuser temperature (so the linear temp-fit branch activates) — both were
    missing before, which left the whole temperature model as dead code."""
    from photonscript.scheduler.focus_seeds import seed_for
    from photonscript.shared.config import PhotonScriptConfig
    cfg = PhotonScriptConfig()
    if ambient_c is None:
        ambient_c = _current_focuser_temp(cfg)
    return seed_for(filter_type.value, ambient_c, cfg)


def _start_guiding(force_calibration: bool = False) -> dict:
    # Attempts>1 so a single failed settle retries instead of silently falling
    # through to unguided exposures; ErrorBehavior gated by GUIDING_ERROR_BEHAVIOR
    # (see the constants block above). ForceCalibration on the night's first
    # guided target gives PHD2 a fresh calibration to settle against.
    return _make_typed("NINA.Sequencer.SequenceItem.Guider.StartGuiding, "
                       "NINA.Sequencer", ForceCalibration=force_calibration,
                       ErrorBehavior=GUIDING_ERROR_BEHAVIOR,
                       Attempts=GUIDING_STARTUP_ATTEMPTS)


def _external_script(path: str, arg: str = "") -> dict:
    """NINA ExternalScript: runs a program and waits for it. ErrorBehavior 0 +
    Attempts 1, so a failed script never blocks the sequence (PS-92)."""
    script = f'"{path}"' + (f" {arg}" if arg else "")
    return _make_typed("NINA.Sequencer.SequenceItem.Utility.ExternalScript, "
                       "NINA.Sequencer", Script=script, ErrorBehavior=0,
                       Attempts=1)


# PS-61: the cooler gate's ErrorBehavior in "skip" mode. 1 =
# SkipInstructionSetOnError (ordinals verified above, GUIDING_ERROR_BEHAVIOR):
# when the gate script exits non-zero (only on a deliberate SKIP, see
# deploy/cooler-gate.cmd) NINA interrupts the gate's own container, i.e. that
# one light block or OSC image pass, and the loop above it moves on. Attempts
# stays 1: a retry would hold another full timeout.
COOLER_GATE_ERROR_BEHAVIOR = 1


def _cooler_gate(gate, rig: str, label: str) -> dict:
    """PS-61 ExternalScript gate before lights. gate = (script path, setpoint
    C, mode) from _cooler_gate_spec. In "warn" mode the script never exits
    non-zero, and ErrorBehavior 0 keeps it harmless either way."""
    from photonscript.scheduler.cooler_gate import script_args
    path, setpoint, mode = gate
    item = _external_script(path, script_args(rig, setpoint, label))
    item["ErrorBehavior"] = COOLER_GATE_ERROR_BEHAVIOR if mode == "skip" else 0
    return item


def _cooler_gate_spec(cfg, setpoint: float):
    """(script, setpoint, mode) when the sequences should carry the gate,
    else None (mode off, or the script missing on this machine)."""
    from photonscript.scheduler.cooler_gate import gate_mode, gate_script
    path = gate_script(cfg)
    return (path, float(setpoint), gate_mode(cfg)) if path else None


def _cooler_gate_missing_notice(cfg) -> list:
    """Mode on but no script here: say so in the sequence and on Pushover,
    so an ungated night is never silent."""
    from photonscript.scheduler.cooler_gate import gate_mode
    if gate_mode(cfg) == "off":
        return []
    path = str(getattr(cfg, "cooler_gate_script", "") or "")
    msg = (f"cooler gate OFF tonight: script {path or '(unset)'} not found, so "
           "lights are not held for the setpoint (PS-61)")
    return [_annotation(msg), _pushover("Startup", msg)]


def _selftest_script(cfg) -> str | None:
    """PS-92: the pulse self-test script path when the slots are enabled."""
    if not getattr(cfg, "phd2_selftest_enabled", False):
        return None
    return str(getattr(cfg, "phd2_selftest_script", "") or "") or None


def _stop_guiding() -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Guider.StopGuiding, "
                       "NINA.Sequencer", ErrorBehavior=0, Attempts=1)


def _disconnect_all() -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Connect."
                       "DisconnectAllEquipment, NINA.Sequencer",
                       ErrorBehavior=0, Attempts=1)


_filter_names_cache: dict | None = None


def _nina_filter_name(filter_type: FilterType) -> str:
    global _filter_names_cache
    if _filter_names_cache is None:
        from photonscript.shared.config import PhotonScriptConfig
        _filter_names_cache = PhotonScriptConfig().filter_name_map()
    return _filter_names_cache.get(filter_type.value, filter_type.value)


# Narrowband filters have far fewer/fainter stars, so a default-length
# autofocus exposure often can't build a valid HFR curve (SII is the worst).
# Give the narrowband filters a longer dedicated AF exposure; broadband keeps
# the profile default (-1).
_NB_AF_EXPOSURE_S = {"Ha": 30.0, "OIII": 30.0, "SII": 45.0}


def _filter_info(filter_type: FilterType) -> dict:
    """NINA.Core FilterInfo shape (underscore fields), per the reference file."""
    return _make_typed(
        "NINA.Core.Model.Equipment.FilterInfo, NINA.Core",
        _name=_nina_filter_name(filter_type),
        _focusOffset=0,
        _position=FILTER_POSITIONS.get(filter_type, 0),
        _autoFocusExposureTime=_NB_AF_EXPOSURE_S.get(filter_type.value, -1.0),
        _autoFocusFilter=False,
        _autoFocusBinning=_make_typed(
            "NINA.Core.Model.Equipment.BinningMode, NINA.Core", X=1, Y=1),
        _autoFocusGain=-1,
        _autoFocusOffset=-1)


def _switch_filter(filter_type: FilterType) -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.FilterWheel.SwitchFilter, NINA.Sequencer",
        Filter=_filter_info(filter_type), ErrorBehavior=0, Attempts=1)


def _switch_filter_none() -> dict:
    """PS-132: a SwitchFilter with no filter, for a rig without a filter
    wheel. NINA's flat instructions (SkyFlat etc.) find their filter with
    Items.First(x is SwitchFilter), so one must be there or Validate throws
    "Sequence contains no matching element" and Start does nothing. With
    Filter null, SwitchFilter.Validate adds no issue (it checks the wheel
    only when a filter is set) and SkyFlat passes the null filter to its
    captures, NINA's normal no-filter path (NINA source, SwitchFilter.cs
    unchanged since 2024-01, so the same in 3.2.0.9001)."""
    return _make_typed(
        "NINA.Sequencer.SequenceItem.FilterWheel.SwitchFilter, NINA.Sequencer",
        Filter=None, ErrorBehavior=0, Attempts=1)


def _af_filter_type(config) -> "FilterType | None":
    """Resolve config.autofocus_filter (e.g. 'L') to a FilterType, or None when
    unset/unknown — the filter PhotonScript focuses on for the AFs it emits, so
    autofocus never runs on a star-starved narrowband filter. Matches on the
    NINA filter name ('L') or the enum name ('LUMINANCE'), case-insensitively."""
    name = (getattr(config, "autofocus_filter", "") or "").strip()
    if not name:
        return None
    for ft in FilterType:
        if name.lower() in (ft.value.lower(), ft.name.lower()):
            return ft
    return None


def _dither_trigger(after_exposures: int) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Guider.DitherAfterExposures, NINA.Sequencer",
        AfterExposures=after_exposures,
        TriggerRunner=_trigger_runner([_make_typed(
            "NINA.Sequencer.SequenceItem.Guider.Dither, NINA.Sequencer",
            ErrorBehavior=0, Attempts=1)]))


def _smart_exposure(exp: ExposurePlan, guided: bool,
                    dither_every_n: int,
                    guard_conditions: list | None = None,
                    extra_triggers: list | None = None,
                    unguided_dither: bool = False) -> dict:
    """SmartExposure: LoopCondition(count) wrapping SwitchFilter+TakeExposure.

    guard_conditions (PS-77) are appended AFTER the LoopCondition, which must
    stay at Conditions[0] (SmartExposure.GetLoopCondition indexes it). They make
    the SmartExposure itself, the innermost repeating container, check Safety
    and the loop end between every exposure. NINA checks a container's OWN
    conditions between its items whether or not parent links resolve, which is
    exactly what kept the Piggy-600 light loop honest on 2026-09-26 while the
    RC16 SmartExposure, guarded only by its ancestors, shot 38 min into a
    closed roof. extra_triggers go after the dither trigger, which must stay
    at Triggers[0] (GetDitherAfterExposures).

    unguided_dither (PS-66): an unguided target dithers too, through NINA's
    Direct Guider (mount pulses, no guide camera). The armer sets it only
    when config unguided_dither is on and NINA's guider is Direct Guider."""
    remaining = exp.count - exp.acquired
    # NINA's SmartExposure ALWAYS expects a DitherAfterExposures trigger at
    # Triggers[0]. Its Validate() calls GetDitherAfterExposures(), which in the
    # 3.2.0.9001 release indexes Triggers[0] with no empty-guard: an empty
    # Triggers list throws ArgumentOutOfRangeException during validation and
    # fails the whole container, so nothing images (observed 2026-07-26). Always
    # emit the trigger; AfterExposures=0 disables dithering — NINA's Execute()
    # early-returns and Validate() adds no "guider not connected" issue — so an
    # unguided run is unaffected while the crash is avoided.
    after = (dither_every_n if (dither_every_n > 0 and (guided or unguided_dither))
             else 0)
    triggers = [_dither_trigger(after)] + list(extra_triggers or [])
    smart = _seq_container(
        SMART_EXPOSURE_NAME,
        [
            _switch_filter(exp.filter_type),
            _make_typed(
                "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer",
                ExposureTime=exp.exposure_seconds,
                Gain=exp.gain, Offset=exp.offset,
                Binning=_make_typed(
                    "NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                    X=exp.binning, Y=exp.binning),
                ImageType="LIGHT", ExposureCount=0,
                ErrorBehavior=0, Attempts=1),
        ],
        conditions=[_make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=remaining)]
        + list(guard_conditions or []),
        triggers=triggers,
        container_type="NINA.Sequencer.SequenceItem.Imaging.SmartExposure, "
                       "NINA.Sequencer",
    )
    smart["IsExpanded"] = False
    return smart


def _sky_flat(filter_type: FilterType, count: int,
              gain: int, offset: int) -> dict:
    """Native NINA sky-flat instruction (verified from a sequencer export):
    auto-adjusts exposure between Min/MaxExposure to hit the histogram
    target while the twilight sky brightens. No flat panel involved."""
    loop = _seq_container(
        f"{count} flats",
        [_make_typed(
            "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
            "NINA.Sequencer",
            ExposureTime=0.0, Gain=gain, Offset=offset,
            Binning=_make_typed(
                "NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                X=1, Y=1),
            ImageType="FLAT", ExposureCount=0,
            ErrorBehavior=0, Attempts=1)],
        conditions=[_make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=count)])
    sf = _seq_container(
        f"Sky flats {filter_type.value}",
        [_switch_filter(filter_type), loop],
        container_type="NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat, "
                       "NINA.Sequencer")
    sf["IsExpanded"] = False
    sf.update(MinExposure=0.1, MaxExposure=30.0,
              HistogramTargetPercentage=0.5,
              HistogramTolerancePercentage=0.1,
              ShouldDither=False, DitherPixels=3.0, DitherSettleTime=5.0)
    return sf


def _autofocus_filter_trigger() -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus.AutofocusAfterFilterChange, "
        "NINA.Sequencer",
        TriggerRunner=_trigger_runner([_autofocus()]))


def _autofocus_hfr_trigger(amount_pct: float = 10.0,
                           sample_size: int = 4,
                           runner: list | None = None) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus.AutofocusAfterHFRIncreaseTrigger, "
        "NINA.Sequencer",
        Amount=amount_pct, SampleSize=sample_size,
        TriggerRunner=_trigger_runner(runner or [_autofocus()]))


def _autofocus_temp_trigger(amount_c: float = 1.0,
                            runner: list | None = None) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus."
        "AutofocusAfterTemperatureChangeTrigger, NINA.Sequencer",
        Amount=amount_c, TriggerRunner=_trigger_runner(runner or [_autofocus()]))


def _block_af_runner(af_filter: FilterType, offset: int,
                     imaging_filter: "FilterType | None" = None) -> list:
    """PS-77: the refocus recipe a mid-block AF trigger runs. Same order as
    the block start (PS-65): AF filter, autofocus, measured filter offset,
    so a triggered AF never focuses through 3 nm.

    PS-176: then back to the imaging filter. NINA fires an AF trigger just
    before the TakeExposure, i.e. after the SmartExposure's own
    SwitchFilter, so the next sub was shot through the AF filter
    (2026-10-07: 15 L 30 s subs inside the M31 R/G/B short runs, one after
    each triggered AF)."""
    out = [_switch_filter(af_filter), _autofocus()]
    if offset:
        out.append(_move_focuser_relative(offset))
    if imaging_filter is not None and imaging_filter != af_filter:
        out.append(_switch_filter(imaging_filter))
    return out


def _autofocus_time_trigger(minutes: float = 60.0,
                            runner: list | None = None) -> dict:
    """Periodic refocus: AF once `minutes` have passed since the last AF.
    Used on the Piggy-600 (PS-68), where NINA #2 cannot see the RC16's
    meridian flip and the HFR trigger baselines on the last AF (a bad AF
    never re-triggers it), so a timed AF is the in-sequence repair. PS-76
    part 2: the RC16 verify AF in focus-model drive mode (runner = the
    block's AF filter + offset recipe)."""
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus.AutofocusAfterTimeTrigger, "
        "NINA.Sequencer",
        Amount=float(minutes),
        TriggerRunner=_trigger_runner(runner or [_autofocus()]))


def _focus_model_move(script: str, filter_type) -> dict:
    """PS-76 part 2 (focus_model_drive): ExternalScript that asks the service
    to move the RC16 focuser to the lookup-table position for this filter
    at the focuser's current temperature (deploy\\focus-model-move.cmd ->
    POST /api/focus/model-move). Always exits 0, ErrorBehavior 0: a failed
    move never stops imaging (the HFR trigger and the verify AF remain)."""
    return _external_script(script, filter_type.value)


def _focus_drive_spec(cfg) -> dict | None:
    """{script, filters, verify_min} when the sequence should replace the
    per-block AF with model moves: focus_model_drive on, the move script on
    this machine, and focus_model.trust() says the model is trusted. The
    filters are those with enough AFs of their own; every other block keeps
    its AF. None = today's AF-per-block sequence (the default)."""
    from pathlib import Path
    if not bool(getattr(cfg, "focus_model_drive", False)):
        return None
    script = str(getattr(cfg, "focus_model_move_script", "") or "").strip()
    try:
        if not script or not Path(script).is_file():
            return None
        from photonscript.scheduler.focus_model import trust
        tr = trust(cfg)
    except Exception:  # noqa: BLE001
        return None
    if tr.get("mode") != "drive" or not tr.get("filters"):
        return None
    return {"script": script, "filters": set(tr["filters"]),
            "verify_min": float(tr.get("verify_af_min") or 120.0)}


def _smart_af_spec(cfg, af_filter) -> dict | None:
    """PS-176: af_policy.smart_spec, or None (every_block) on any error so
    a bad focus model never breaks a night."""
    try:
        from photonscript.scheduler.af_policy import smart_spec
        return smart_spec(cfg, af_filter)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "smart AF policy unavailable, using every_block: %s", e)
        return None


def _meridian_flip_trigger() -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.MeridianFlip.MeridianFlipTrigger, NINA.Sequencer",
        TriggerRunner=_trigger_runner())


def _reconnect_trigger() -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Connect.ReconnectOnDownloadFailure, NINA.Sequencer",
        TriggerRunner=_trigger_runner())


def _safety_condition() -> dict:
    return _make_typed(
        "NINA.Sequencer.Conditions.SafetyMonitorCondition, NINA.Sequencer")


def _altitude_condition(target, min_alt: float) -> dict:
    """Full WaitLoopData shape — bare MinimumAltitude loads with empty coords."""
    return _make_typed(
        "NINA.Sequencer.Conditions.AltitudeCondition, NINA.Sequencer",
        HasDsoParent=True,
        Data=_make_typed(
            "NINA.Sequencer.SequenceItem.Utility.WaitLoopData, NINA.Sequencer",
            Coordinates=_coords(target), Offset=min_alt, Comparator=1))


def _loop_once() -> dict:
    """LoopCondition(1): run a container a single time per entry. NINA resets
    the counter when a parent container loops (the SmartExposure counts rely
    on the same reset), so a target is re-acquired once after an unsafe pause,
    not on every pass."""
    return _make_typed("NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
                       CompletedIterations=0, Iterations=1)


def _annotation(text: str) -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Utility.Annotation, "
                       "NINA.Sequencer", Text=text, ErrorBehavior=0, Attempts=1)


def _wait_until_safe() -> dict:
    """Core NINA instruction: blocks until the safety monitor reports Safe."""
    return _make_typed("NINA.Sequencer.SequenceItem.SafetyMonitor.WaitUntilSafe, "
                       "NINA.Sequencer", ErrorBehavior=0, Attempts=1)


def _wait_for_timespan(seconds: int) -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Utility.WaitForTimeSpan, "
                       "NINA.Sequencer", Time=seconds, ErrorBehavior=0, Attempts=1)


def _wait_for_provider(provider: str, minutes_offset: int = 0) -> dict:
    """WaitForTime bound to a NINA date provider (recomputed nightly)."""
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Utility.WaitForTime, NINA.Sequencer",
        Hours=0, Minutes=0, MinutesOffset=minutes_offset, Seconds=0,
        SelectedProvider=_make_typed(
            f"NINA.Sequencer.Utility.DateTimeProvider.{provider}, NINA.Sequencer"),
        ErrorBehavior=0, Attempts=1)


def _time_condition(provider: str, minutes_offset: int = 0) -> dict:
    """Loop condition: run until a provider time (e.g. dawn)."""
    return _make_typed(
        "NINA.Sequencer.Conditions.TimeCondition, NINA.Sequencer",
        Hours=0, Minutes=0, MinutesOffset=minutes_offset, Seconds=0,
        SelectedProvider=_make_typed(
            f"NINA.Sequencer.Utility.DateTimeProvider.{provider}, NINA.Sequencer"))


def dark_library_exposures(cfg) -> list[float]:
    """RC16 dark-library lengths: config dark_exposures, plus PS-66's
    unguided_max_exposure_s when it is set and not already listed, so the
    capped unguided subs always have a dark set at quota."""
    wanted: list[float] = []
    for tok in str(getattr(cfg, "dark_exposures", "600,180")).split(","):
        try:
            wanted.append(float(tok.strip()))
        except ValueError:
            continue
    try:
        cap = float(getattr(cfg, "unguided_max_exposure_s", 0) or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if cap > 0 and not any(abs(cap - w) < 0.5 for w in wanted):
        wanted.append(cap)
    return wanted


def _dark_quota_blocks(dawn_provider, dawn_offset):
    """Dark blocks for unsafe time, capped by the library quota: for each
    exposure the current lights use, take only (quota - already on disk),
    600s first then 180s, then the PS-66 unguided cap (300s) when it
    is not listed. Lowest-priority work: any of LoopWhileUnsafe
    exit, dawn, or the cap ends the block."""
    cfg = _gen_cfg()
    quota = int(getattr(cfg, "dark_target_count", 30))
    blocks = []
    try:
        # PS-122: one quota rule with the companion and the Calibration owed
        # view (QA-passed darks once the rig has a QA store)
        # PS-160: plus the lengths the lights actually used
        from photonscript.scheduler.calibration import (dark_quota,
                                                        night_dark_exposures)
        for exp_s in night_dark_exposures(cfg, "rc16"):
            need = dark_quota(cfg, "rc16", exp_s)["need"]
            if need == 0:
                continue
            blocks.append(_seq_container(
                f"DARKS_{exp_s:.0f}s (need {need} of {quota})",
                [_make_typed(
                    "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
                    "NINA.Sequencer",
                    ExposureTime=exp_s,
                    Gain=cfg.default_gain, Offset=cfg.default_offset,
                    Binning=_make_typed(
                        "NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                        X=1, Y=1),
                    ImageType="DARK", ExposureCount=0,
                    ErrorBehavior=0, Attempts=1)],
                conditions=[
                    _make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                                "NINA.Sequencer"),
                    _time_condition(dawn_provider, dawn_offset),
                    _make_typed("NINA.Sequencer.Conditions.LoopCondition, "
                                "NINA.Sequencer",
                                CompletedIterations=0, Iterations=need)]))
    except Exception as e:  # noqa: BLE001
        logger_warn = getattr(__import__("logging").getLogger(__name__),
                              "warning")
        logger_warn("dark quota scan failed: %s", e)
    return blocks


def _time_condition_at(hh: int, mm: int) -> dict:
    """Loop condition: run until a fixed local time (e.g. moonrise)."""
    return _make_typed(
        "NINA.Sequencer.Conditions.TimeCondition, NINA.Sequencer",
        Hours=hh, Minutes=mm, MinutesOffset=0, Seconds=0,
        SelectedProvider=_make_typed(
            "NINA.Sequencer.Utility.DateTimeProvider.TimeProvider, "
            "NINA.Sequencer"))


def _handoff_condition(target) -> dict | None:
    """PS-180: TimeCondition at the target's handoff (planner window end) in
    local clock time, or None when the target keeps the mount to the loop
    end. Same fixed-time rule as the moonrise cap: NINA reads a time before
    noon as the next morning."""
    when = getattr(target, "handoff_utc", None)
    if when is None:
        return None
    from photonscript.shared.localtime import to_local
    tl = to_local(_gen_cfg(), when)
    return _time_condition_at(tl.hour, tl.minute)


def _slew_alt_az(alt_deg: int = 70, az_deg: int = 180) -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Telescope.SlewScopeToAltAz, NINA.Sequencer",
        Coordinates=_make_typed(
            "NINA.Astrometry.InputTopocentricCoordinates, NINA.Astrometry",
            AzDegrees=az_deg, AzMinutes=0, AzSeconds=0,
            AltDegrees=alt_deg, AltMinutes=0, AltSeconds=0),
        ErrorBehavior=0, Attempts=1)


# --- PS-26 Piggy-600 centering -------------------------------------------------

def _piggy_store(cfg):
    """The measured RC16-to-Piggy-600 offset (re-measured when stale).
    Separate so tests can pin it."""
    from photonscript.scheduler import piggy_offset as po
    return po.current(cfg)


def _piggy_centering(target):
    """(center target, nested pier-West container or None, annotation text
    or None) for one sequence target. RC16-driven targets: (target, None,
    None), nothing changes. A Piggy-600-driven target in mode "on" with a
    measured offset centers on the pier-East coordinates (container, slew,
    center, meridian-flip re-center) and re-centers on the pier-West ones
    in a nested container that runs once, only before the transit; in
    preview (or with no offset) only the annotation says what applies."""
    if getattr(target, "driving_rig", "rc16") != "piggyback":
        return target, None, None
    from types import SimpleNamespace
    from photonscript.scheduler import piggy_offset as po
    from photonscript.shared.config import PhotonScriptConfig
    cfg = PhotonScriptConfig()   # fresh: the System page may change the mode
    if po.mode(cfg) == "off":
        return target, None, None
    try:
        plan = po.center_plan(cfg, target.ra_hours, target.dec_degrees,
                              _piggy_store(cfg), po.frame_center_of(target))
    except Exception as e:  # noqa: BLE001 - never break a night over this
        return target, None, (f"Piggy-600 centering (PS-26): offset "
                              f"unavailable ({e}); no shift")
    if not plan["applied"]:
        return target, None, plan["note"]
    east, west = plan["by_pier"]["East"], plan["by_pier"]["West"]
    center_t = SimpleNamespace(name=target.name, ra_hours=east["ra_hours"],
                               dec_degrees=east["dec_degrees"],
                               rotation=getattr(target, "rotation", 0.0))
    west_t = SimpleNamespace(name=target.name, ra_hours=west["ra_hours"],
                             dec_degrees=west["dec_degrees"],
                             rotation=getattr(target, "rotation", 0.0))
    note = plan["note"]
    transit = getattr(target, "transit_utc", None)
    if transit is None:
        return center_t, None, (note + " No transit time for tonight, so "
                                "only the pier-East center is set (the "
                                "meridian flip side).")
    from photonscript.shared.localtime import to_local
    tl = to_local(cfg, transit)
    west_c = _seq_container(
        f"{target.name}{PIGGY_WEST_CENTER_SUFFIX}", [_center(west_t)],
        conditions=[_loop_once(), _time_condition_at(tl.hour, tl.minute)],
        container_type=DSO_CONTAINER_TYPE,
        Target=_make_typed("NINA.Astrometry.InputTarget, NINA.Astrometry",
                           Expanded=True, TargetName=target.name,
                           PositionAngle=getattr(target, "rotation", 0.0),
                           InputCoordinates=_coords(west_t)))
    note += (f" Pier West center until the transit at {tl:%H:%M} local "
             f"(RA {west['ra_hours']:.4f}h Dec {west['dec_degrees']:+.3f}); "
             f"pier East center RA {east['ra_hours']:.4f}h Dec "
             f"{east['dec_degrees']:+.3f}.")
    return center_t, west_c, note


# --- Containers ---------------------------------------------------------------

def _build_target_container(target: NinaSequenceTarget, min_altitude: float,
                            force_calibration: bool = False,
                            af_filter: "FilterType | None" = None,
                            narrate: str = "normal",
                            focus_offsets: dict | None = None,
                            loop_end: tuple | None = None,
                            selftest_script: str | None = None,
                            unguided_dither: bool = False,
                            cooler_gate: tuple | None = None,
                            focus_drive: dict | None = None,
                            followed: bool = False,
                            smart: dict | None = None,
                            fill: bool = False) -> dict:
    """AARO acquisition order: tracking -> slew -> first filter -> AF ->
    plate solve center -> tracking (defensive) -> [self-test] -> [guiding]
    -> [HDR shorts once] -> exposures.

    smart (PS-176, af_policy.smart_spec; None = every_block): each light
    block is a TARGET_SMART_BLOCK_SUFFIX container that moves the focuser by
    the filter's offset from the AF filter and back, with no AF; a 3 nm
    filter with no measured offset keeps the AF recipe. The block
    triggers (temperature, HFR, timed) run the AF recipe and switch back to
    the imaging filter. Ignored while focus_drive is active.

    selftest_script (PS-92): a guided target runs the pulse-path self-test
    script (NINA ExternalScript, slot "target") between SetTracking and
    StartGuiding; the service skips it once tonight passed on this pier side.

    af_filter (config.autofocus_filter, e.g. L) is the bright filter the
    start-of-target autofocus runs on so it never focuses through narrowband;
    None keeps the old behavior (focus in the imaging filter).

    focus_offsets (config.focus_offset_map()) are EAF steps from the AF
    filter's best focus to each imaging filter's. Every filter block runs
    SwitchFilter(AF filter) -> RunAutofocus -> MoveFocuserRelative(offset) ->
    exposures, so no block ever images at a stale absolute seed (PS-65).

    followed (PS-149): other targets come after this one tonight, so a
    focus-offset calibration runs its AF series once instead of repeating it
    while safe and up (which would hold the mount from the real targets).

    narrate (config.pushover_verbosity) controls Pushover chatter: "verbose"
    adds the per-block starting/done pair, "normal" keeps per-target step lines,
    "quiet" drops those too (target intro + done still fire).

    loop_end = (date provider, minutes offset) of the night loop's end (the
    LOOP_ALL_NIGHT TimeCondition). PS-77: every LIGHT SmartExposure carries
    SafetyMonitorCondition + this TimeCondition itself, and the temperature /
    HFR refocus triggers ride on each SmartExposure with the block's own
    AF-filter + offset recipe instead of on the target container.

    cooler_gate (PS-61, _cooler_gate_spec): each light block becomes its own
    container "<target> filter block (cooler-gated)" that starts with the
    cooler-gate ExternalScript, so lights wait for the setpoint and a gate
    SKIP (sensor still off after the timeout) skips only that block; the
    imaging loop retries it on its next pass. None = the flat block list, as
    before.

    focus_drive (PS-76 part 2, _focus_drive_spec; None by default): a block
    whose filter is in focus_drive["filters"] starts with SwitchFilter(its
    own filter) + the model-move ExternalScript instead of AF filter + AF +
    offset; its temperature trigger runs the model move, its HFR trigger
    keeps the full AF recipe, and a time trigger runs that AF recipe every
    verify_min as the verify AF. The start-of-target AF is unchanged.

    PS-180: target.handoff_utc (the planner's window end) ends the target
    there: a TimeCondition at that local clock time on the imaging loop and
    the DSO container, so the sequence moves on to the next target instead
    of repeating while safe and up all night. fill=True builds the final
    "<target> fill (rest of the night)" container instead: same plan, no
    handoff, it runs to the loop end."""
    if getattr(target, "focus_calibration", False):
        return _build_focus_calibration_container(target, min_altitude,
                                                  af_filter, focus_offsets,
                                                  once=followed)
    if getattr(target, "tpoint_mapping", False):   # PS-171
        return _build_tpoint_mapping_container(target, af_filter, loop_end,
                                               cooler_gate)
    if getattr(target, "optics_test", False):   # PS-148
        return _build_optics_test_container(target, min_altitude,
                                            af_filter, focus_offsets,
                                            loop_end, cooler_gate)
    if getattr(target, "tracking_test", False):
        return _build_tracking_test_container(target, min_altitude,
                                              af_filter, focus_offsets,
                                              loop_end, cooler_gate)
    chatty_block = narrate == "verbose"          # per-block starting/done pair
    narrate_steps = narrate in ("verbose", "normal")  # per-target step lines
    # PS-105: planner copies arrive with acquired 0 (count = still owed, see
    # target_planner.remaining_copy), so `count - acquired` here is just count.
    active = [e for e in target.exposures
              if e.count - e.acquired > 0 or e.short_remaining() > 0]

    # Moon ordering FIRST: if it leaves nothing to shoot, emit no container at
    # all. An empty DSO container still slews/AFs/centers and, looping under
    # Safety+Altitude, re-acquired every ~8 min all night (2026-09-21, PS-27).
    NB_SET = {"Ha", "OIII", "SII"}
    bb = [e for e in active if e.filter_type.value not in NB_SET]
    nb = [e for e in active if e.filter_type.value in NB_SET]
    ordered = active
    bb_condition = None
    bb_deferred_note = None
    if bb:
        from photonscript.scheduler.moon import broadband_deferred
        mw = _moon_window()
        if mw.get("available") and mw.get("down_at_dusk"):
            # dark evening: broadband first, capped at moonrise
            ordered = bb + nb
            if mw.get("rise_local_hh") is not None:
                bb_condition = _time_condition_at(mw["rise_local_hh"],
                                                  mw["rise_local_mm"])
        elif not broadband_deferred(mw):
            ordered = bb + nb  # faint moon: broadband fine any time
        else:
            # moon up at dusk and bright: defer broadband tonight
            bb_deferred_note = mw.get("illum_pct", "?")
            ordered = nb
    if not ordered:
        return None

    # PS-26: where NINA centers (the pier-East shifted coordinates for a
    # Piggy-600-driven target in mode on, else the target itself)
    center_t, piggy_west, piggy_note = _piggy_centering(target)
    # PS-112: name each set still owed, shorts first as shot (a plan can owe
    # only its HDR shorts once the long set is done)
    desc = []
    for e in active:
        if e.short_remaining() > 0:
            desc.append(f"{e.filter_type.value}×{e.short_remaining()}"
                        f"@{e.hdr_short_seconds:.0f}s")
        if e.count - e.acquired > 0:
            desc.append(f"{e.filter_type.value}×{e.count - e.acquired}"
                        f"@{e.exposure_seconds:.0f}s")
    plan_desc = ", ".join(desc)
    total_h = sum(e.exposure_seconds * max(0, e.count - e.acquired)
                  + (e.hdr_short_seconds or 0) * e.short_remaining()
                  for e in active) / 3600
    items = [
        _pushover("Imaging", f"{target.name}: slewing "
                  f"(RA {target.ra_hours:.2f}h Dec {target.dec_degrees:+.1f}°) "
                  f"— plan {plan_desc} (~{total_h:.1f}h)"),
        _set_tracking(0),
        _slew(center_t),
    ]
    if piggy_note:
        items.insert(0, _annotation(piggy_note))
    # Autofocus on the bright AF filter (L) when configured, else the imaging
    # filter. This AF gets the stars tight for centering and guiding; each
    # filter block below then runs its own AF + measured offset (PS-65). The
    # absolute seed is ONLY an AF starting point and is always followed by AF.
    focus_filter = (af_filter if (af_filter and active) else
                    (active[0].filter_type if active else None))
    if focus_filter is not None:
        items.append(_switch_filter(focus_filter))
    if target.auto_focus_on_start and active:
        seed0 = _seed_position(focus_filter)
        if narrate_steps:
            items.append(_pushover("Imaging",
                                   f"{target.name}: slew done — seeding focuser "
                                   f"to {seed0} for {focus_filter.value}, "
                                   "autofocusing, then plate solve & center"))
        items.append(_move_focuser(seed0))
        items.append(_autofocus())
    elif target.auto_focus_on_start:
        if narrate_steps:
            items.append(_pushover("Imaging",
                                   f"{target.name}: slew done — autofocusing, "
                                   "then plate solve & center"))
        items.append(_autofocus())
    items.append(_center(center_t))
    if piggy_west is not None:
        items.append(piggy_west)   # PS-26: pier-West center before transit
    items.append(_set_tracking(0))
    # PS-85: blocks decided unguided tonight on a guided target
    unguided_set = ({str(f) for f in (getattr(target, "unguided_filters", None) or [])}
                    if target.start_guiding else set())
    per_block = bool(unguided_set & {e.filter_type.value for e in ordered})
    if target.start_guiding and per_block:
        names = ", ".join(e.filter_type.value for e in ordered
                          if e.filter_type.value in unguided_set)
        items.append(_annotation(f"PS-85 per-block guiding: {names} unguided "
                                 "tonight (no real guide star through that "
                                 "filter); the other blocks start guiding "
                                 "themselves"))
        if narrate_steps:
            items.append(_pushover("Imaging",
                                   f"{target.name}: focused, centered; guiding per "
                                   f"block ({names} unguided, TPoint + ProTrack)"))
    elif target.start_guiding:
        if selftest_script:
            items.append(_external_script(selftest_script, "target"))
        items.append(_start_guiding(force_calibration))
        if narrate_steps:
            items.append(_pushover("Imaging",
                                   f"{target.name}: focused, centered, guiding — "
                                   "capturing"))
    else:
        if narrate_steps:
            items.append(_pushover("Imaging",
                                   f"{target.name}: focused, centered, unguided "
                                   "(TPoint + ProTrack), capturing"))
    if bb_deferred_note is not None:
        items.append(_pushover(
            "Imaging",
            f"{target.name}: moon up at dusk "
            f"({bb_deferred_note}%) — RGB/L deferred to a "
            "dark evening; narrowband only tonight"))

    first_guided = [True]   # PS-85: the first per-block StartGuiding

    def _guide_items(block_guided: bool) -> list:
        """PS-85: what a block runs first on a per-block guided target."""
        if not per_block:
            return []
        if not block_guided:
            return [_stop_guiding()]
        out = []
        if selftest_script:
            out.append(_external_script(selftest_script, "target"))
        out.append(_start_guiding(force_calibration and first_guided[0]))
        first_guided[0] = False
        return out

    # PS-176: smart AF policy (focus_drive, when active, owns the blocks)
    use_smart = (smart is not None and focus_drive is None
                 and af_filter is not None)
    if use_smart:
        from photonscript.scheduler.af_policy import describe
        items.append(_annotation(describe(smart)))

    def _block(exp, bi, n_blocks, condition=None, part="long"):
        """One filter block. part "short" = only the HDR short set (the
        once-per-visit container, PS-176), "long" = only the long set (the
        repeating imaging loop)."""
        f = exp.filter_type
        n = (exp.short_remaining() if part == "short"
             else exp.count - exp.acquired)
        sub_s = (exp.hdr_short_seconds if part == "short"
                 else exp.exposure_seconds)
        block_guided = bool(target.start_guiding
                            and f.value not in unguided_set)
        block_h = sub_s * n / 3600
        drive = (focus_drive is not None
                 and f.value in focus_drive["filters"])
        smart_move = use_smart and not drive
        # PS-65: every block focuses for itself. AF on the AF filter (L), or on
        # the block's own filter when no AF filter is configured, then move by
        # the measured filter offset. The old per-block MoveFocuserAbsolute to a
        # July seed with no AF after it put a whole night's Ha ~300 steps out
        # (2026-09-26), and on every loop pass undid the triggered AFs.
        # PS-176 smart: the block skips the AF (unless it is a 3 nm filter with
        # no measured offset) and moves by the offset from the AF filter's
        # focus (and back at the end).
        block_af = af_filter or f
        if smart_move:
            offset = (int(smart["offsets"].get(f.value, 0))
                      if f != block_af else 0)
            skip_af = f.value not in smart["af_blocks"]
        else:
            offset = _focus_offset(block_af, f, focus_offsets)
            skip_af = False
        out = []
        if chatty_block:   # per-block "starting/done" pair: the bulk of the noise
            if drive:
                how = "focus from the lookup table"
            elif smart_move and skip_af:
                how = (f"offset {offset:+d} from {block_af.value} focus "
                       "(no autofocus)")
            else:
                how = (f"autofocus on {block_af.value}"
                       + (f" then offset {offset:+d}" if offset else ""))
            out.append(_pushover(
                "Imaging",
                f"{target.name} [{bi}/{n_blocks}]: starting {f.value} "
                + ("HDR shorts " if part == "short" else "")
                + f": {n}x{sub_s:.0f}s "
                f"(~{block_h:.1f}h) gain {exp.gain}; " + how
                + (" (moon-free window)" if condition else "")))
        if drive:
            # PS-76 part 2: table position instead of AF (verify AF below)
            out.append(_switch_filter(f))
            out.append(_focus_model_move(focus_drive["script"], f))
        elif smart_move and skip_af:
            out.append(_switch_filter(f))
        else:
            out.append(_switch_filter(block_af))
            out.append(_autofocus())
            if offset and not smart_move:
                out.append(_move_focuser_relative(offset))
        # PS-77: the light loop guards itself. Safety + loop end sit on the
        # SmartExposure (the innermost repeating container) so NINA checks
        # them between EVERY exposure, and the refocus triggers carry this
        # block's AF filter + offset (a target-level AF trigger would focus
        # through the narrowband filter once parent links make it fire
        # mid-block).
        def _guards():
            g = [_safety_condition()]
            if loop_end:
                g.append(_time_condition(*loop_end))
            return g

        def _af_triggers():
            runner = lambda: _block_af_runner(block_af, offset, f)  # noqa: E731
            if drive:
                # temperature change -> table move (cheap, so 1 C); HFR creep
                # and the periodic verify keep the full AF recipe
                return [_autofocus_temp_trigger(1.0, [_focus_model_move(
                            focus_drive["script"], f)]),
                        _autofocus_hfr_trigger(10.0, 4, runner()),
                        _autofocus_time_trigger(focus_drive["verify_min"],
                                                runner())]
            if smart_move:
                # PS-176: the only AFs inside a target besides the start AF
                # and blocks of unmeasured filters: temperature, HFR, timed
                t = [_autofocus_temp_trigger(smart["temp_c"], runner()),
                     _autofocus_hfr_trigger(smart["hfr_pct"], 4, runner())]
                if smart["interval_min"] > 0:
                    t.append(_autofocus_time_trigger(smart["interval_min"],
                                                     runner()))
                return t
            return [_autofocus_temp_trigger(2.0, runner()),
                    _autofocus_hfr_trigger(10.0, 4, runner())]

        # HDR (PS-47): the SHORT companion set, same filter/gain/offset/
        # binning, its own (shorter) length + count, only what is still owed.
        # PS-176: shot from the once-per-visit container (part "short"); the
        # repeating loop holds only the long set.
        if n > 0 and part == "short":
            short_exp = exp.model_copy(update={
                "exposure_seconds": exp.hdr_short_seconds,
                "count": n, "acquired": 0,
                "hdr_short_seconds": None, "hdr_short_count": 0,
                "hdr_short_acquired": 0})
            out.append(_smart_exposure(short_exp, block_guided,
                                       target.dither_every_n,
                                       guard_conditions=_guards(),
                                       extra_triggers=_af_triggers(),
                                       unguided_dither=(unguided_dither
                                                        and not per_block)))
        elif n > 0:
            out.append(_smart_exposure(exp, block_guided,
                                       target.dither_every_n,
                                       guard_conditions=_guards(),
                                       extra_triggers=_af_triggers(),
                                       unguided_dither=(unguided_dither
                                                        and not per_block)))
        if chatty_block:
            out.append(_pushover(
                "Imaging",
                f"{target.name} [{bi}/{n_blocks}]: {f.value} block "
                f"done ({n}x{sub_s:.0f}s attempted)"))
        # PS-85: an unguided block stops guiding before its AF; a guided one
        # starts guiding after its AF and offset, right before its lights
        guide = _guide_items(block_guided)
        if guide and block_guided:
            k = next((i for i, it in enumerate(out)
                      if it.get("Name") == SMART_EXPOSURE_NAME), len(out))
            out[k:k] = guide
        elif guide:
            out = guide + out
        if smart_move and offset:
            # PS-176: the offset move goes right before the lights (after any
            # StartGuiding, so a skipped guiding start leaves the focuser at
            # the AF filter's focus) and is undone at the block end, so
            # every block starts from the AF filter's best focus
            k = next((i for i, it in enumerate(out)
                      if it.get("Name") == SMART_EXPOSURE_NAME), len(out))
            out.insert(k, _move_focuser_relative(offset))
            out.append(_move_focuser_relative(-offset))
        gate = (_cooler_gate(cooler_gate, "rc16", f"{target.name} {f.value}")
                if cooler_gate else None)
        if smart_move:
            # PS-176: its own container (a gate SKIP or a failed per-block
            # StartGuiding skips this block only; lint rule focus-offset
            # checks its moves add up to zero)
            return [_seq_container(f"{target.name}{TARGET_SMART_BLOCK_SUFFIX}",
                                   ([gate] if gate else []) + out)]
        if gate:
            # PS-61: gate first (before the AF: no point focusing for a block
            # that will not shoot), in a container of its own so a SKIP
            # interrupts this block only.
            return [_seq_container(f"{target.name}{TARGET_BLOCK_SUFFIX}",
                                   [gate] + out)]
        if per_block:
            # PS-85: its own container, so a failed StartGuiding (ErrorBehavior
            # skip) skips this block only, not the target's imaging loop
            return [_seq_container(f"{target.name}{TARGET_GUIDE_BLOCK_SUFFIX}", out)]
        return out

    def _moon_wrap(exp, blk):
        if exp.filter_type.value not in NB_SET and bb_condition is not None:
            return [_seq_container(
                f"{exp.filter_type.value}{FILTER_UNTIL_MOONRISE_SUFFIX}", blk,
                conditions=[copy.deepcopy(bb_condition)])]
        return blk

    shorts = [e for e in ordered if e.short_remaining() > 0]
    longs = [e for e in ordered if e.count - e.acquired > 0]
    n_blocks = len(shorts) + len(longs)
    bi = 0
    hdr_items = []
    for exp in shorts:
        bi += 1
        cond = bb_condition if exp.filter_type.value not in NB_SET else None
        hdr_items.extend(_moon_wrap(exp, _block(exp, bi, n_blocks, cond,
                                                part="short")))
    imaging = []
    for exp in longs:
        bi += 1
        cond = bb_condition if exp.filter_type.value not in NB_SET else None
        imaging.extend(_moon_wrap(exp, _block(exp, bi, n_blocks, cond)))
    # Acquire ONCE per entry, then keep shooting the plan while safe and above
    # the altitude limit (Jeremy 2026-09-26: keep shooting when the plan is
    # done and there's nothing else to do). Only this inner container loops;
    # the outer DSO container carries LoopCondition(1) so it never re-slews,
    # re-focuses and re-centers on every pass (PS-27).
    # PS-77: the loop end rides here too, so even if parent links were ever
    # lost this container stops re-running its filter switches and AFs once
    # the SmartExposures have stopped at the loop end.
    inner_conds = [_safety_condition(),
                   _altitude_condition(target, min_altitude)]
    if loop_end:
        inner_conds.append(_time_condition(*loop_end))
    # PS-180: the planner's window end hands the mount to the next target
    handoff = None if fill else _handoff_condition(target)
    if handoff is not None:
        inner_conds.append(handoff)
    # PS-111: a mosaic panel that is not the mosaic's last one tonight shoots
    # its owed subs once (LoopCondition(1)) and hands the mount to the next
    # panel; the last panel keeps the repeat-while-up loop.
    once = not getattr(target, "repeat_while_up", True)
    if once:
        inner_conds.append(_loop_once())
    # PS-149: when every block is broadband capped at moonrise, the target
    # itself ends at moonrise. Before, the imaging loop (Safety + Altitude +
    # loop end) held only the "until moonrise" containers, which are no-ops
    # after moonrise, so it spun until dawn (NINA #1, 2026-10-06 04:19 to
    # 05:56: M31 LRGB, NGC 604 and the Heart never got their turn). The DSO
    # container carries it too, so after moonrise the target is skipped
    # without a slew / AF / center.
    moon_capped = (bb_condition is not None and bool(ordered) and all(
        e.filter_type.value not in NB_SET for e in ordered))
    if moon_capped:
        inner_conds.append(copy.deepcopy(bb_condition))
    # PS-149: a loop with no item that surely takes time can spin when its
    # children are all skipped (focus-model and smart blocks have no AF):
    # pace it.
    from photonscript.scheduler.sequence_lint import _paces
    if imaging and not any(_paces(i) for i in imaging):
        imaging.append(_wait_for_timespan(TARGET_IMAGING_PACE_S))
    if getattr(target, "mosaic_note", ""):
        items.insert(0, _annotation(target.mosaic_note))
    dso_name = target.name + (TARGET_FILL_SUFFIX if fill else "")
    if fill:
        if not imaging:
            return None   # only HDR shorts owed: shot in the main visit
        items.insert(0, _annotation(
            f"PS-180: the rest of the night goes back to {target.name}, the "
            "highest-priority target still up, until it sets or the loop "
            "end"))
    elif handoff is not None:
        items.insert(0, _annotation(
            f"PS-180: {target.name}'s window ends at "
            f"{handoff['Hours']:02d}:{handoff['Minutes']:02d} local; then the "
            "next target gets the mount"))
    if hdr_items and not fill:
        # PS-176: HDR shorts once per visit, before the repeating loop
        # (PS-180: the fill visit skips them, the main visit shot them)
        items.append(_seq_container(f"{target.name}{TARGET_HDR_SHORTS_SUFFIX}",
                                    hdr_items))
    if imaging:
        items.append(_seq_container(
            f"{dso_name}{TARGET_IMAGING_SUFFIX}", imaging,
            conditions=inner_conds))
    items.append(_pushover("Imaging", f"{target.name}: leaving target "
                           f"({plan_desc}): "
                           + ("window over, " if handoff is not None else "")
                           + "below altitude or unsafe"))
    if once:
        items[-1] = _pushover("Imaging", f"{target.name}: leaving panel "
                              f"({plan_desc}): its subs are done (or unsafe / "
                              "below altitude), next panel")

    # AF triggers (temp drift 2.0 C + HFR creep 10%) moved onto each light
    # SmartExposure with the block's AF filter + offset (PS-77, see _block).
    # Until PS-77 the generated JSON carried no Parent links, so these
    # target-level triggers could only fire between the target container's
    # own items, never mid-block; with links they would fire between
    # exposures and autofocus through the narrowband filter.
    # AutofocusAfterFilterChange stays dropped (PS-65). The meridian flip and
    # reconnect triggers stay here: they are target-wide.
    triggers = [_meridian_flip_trigger(), _reconnect_trigger()]

    container = _seq_container(
        dso_name, items,
        conditions=[_safety_condition(),
                    _altitude_condition(target, min_altitude),
                    _loop_once()]
        + ([copy.deepcopy(bb_condition)] if moon_capped else [])   # PS-149
        + ([copy.deepcopy(handoff)] if handoff is not None else []),  # PS-180
        triggers=triggers,
        container_type="NINA.Sequencer.Container.DeepSkyObjectContainer, "
                       "NINA.Sequencer",
        Target=_make_typed(
            "NINA.Astrometry.InputTarget, NINA.Astrometry",
            Expanded=True, TargetName=target.name,
            PositionAngle=target.rotation,
            InputCoordinates=_coords(center_t)),
    )
    return container


FOCUS_CAL_ORDER = ("L", "R", "G", "B", "L", "Ha", "OIII", "SII", "L")


def _focus_cal_steps(filters, rounds: int, ref) -> list:
    """Resolve the calibration filter order to FilterTypes, repeated `rounds`
    times. Unknown names are skipped; the reference filter brackets each
    round so drift between AFs is visible in the reports."""
    names = [str(f) for f in (filters or FOCUS_CAL_ORDER)]
    ft_by = {ft.value.lower(): ft for ft in FilterType}
    ft_by.update({ft.name.lower(): ft for ft in FilterType})
    one = [ft_by[n.lower()] for n in names if n.lower() in ft_by]
    steps = []
    for _ in range(max(1, int(rounds or 1))):
        steps.extend(one)
    if ref is not None and (not steps or steps[-1] != ref):
        steps.append(ref)
    return steps


def _build_focus_calibration_container(target: NinaSequenceTarget,
                                       min_altitude: float,
                                       af_filter: "FilterType | None",
                                       focus_offsets: dict | None,
                                       once: bool = False) -> dict:
    """PS-76 focus-offset calibration: slew to a rich star field, AF on the
    reference filter (L), center, then run RunAutofocus in every filter of the
    bracketed order. Before each AF the focuser is moved by the configured
    offset delta from the previous filter, so every AF starts near its best
    focus. No lights are taken; the product is NINA's AF reports, which the
    backfill ingests into focus_model.

    NINA's profile "Autofocus filter" must be OFF for this run, or every
    RunAutofocus switches to that filter and only L gets measured.

    The AF series repeats while safe and up (a whole calibration night).
    once (PS-149): other targets follow (the PS-144 dusk calibration put
    first in a real night), so the series runs once (LoopCondition(1)):
    repeating it held the mount on the calibration field for hours."""
    ref = af_filter or FilterType.LUMINANCE
    steps = _focus_cal_steps(target.focus_calibration_filters,
                             target.focus_calibration_rounds, ref)
    if steps and steps[0] == ref:
        steps = steps[1:]  # the acquisition AF below already measured ref
    n_af = len(steps) + 1
    items = [
        _annotation("PS-76 focus-offset calibration. Turn OFF the NINA "
                    "profile Autofocus filter for this run, or every AF runs "
                    "in that filter. Results land in NINA's AutoFocus reports "
                    "and feed focus_model on the next backfill."),
        _pushover("Imaging", f"{target.name}: focus calibration, slewing - "
                  f"{n_af} autofocus runs "
                  f"({', '.join(f.value for f in [ref] + steps)})"),
        _set_tracking(0),
        _slew(target),
        _switch_filter(ref),
        _move_focuser(_seed_position(ref)),
        _autofocus(),
        _center(target),
        _set_tracking(0),
    ]
    prev = ref
    cal = []
    for f in steps:
        cal.append(_switch_filter(f))
        delta = _focus_offset(prev, f, focus_offsets)
        if delta:
            cal.append(_move_focuser_relative(delta))
        cal.append(_autofocus())
        prev = f
    items.append(_seq_container(f"{target.name}{TARGET_FOCUS_CAL_SUFFIX}", cal,
                                conditions=[_safety_condition()]
                                + ([_loop_once()] if once else [])))
    items.append(_pushover("Imaging", f"{target.name}: focus calibration "
                           f"done ({n_af} AF runs)"))
    return _seq_container(
        target.name, items,
        conditions=[_safety_condition(),
                    _altitude_condition(target, min_altitude),
                    _loop_once()],
        triggers=[_meridian_flip_trigger(), _reconnect_trigger()],
        container_type="NINA.Sequencer.Container.DeepSkyObjectContainer, "
                       "NINA.Sequencer",
        Target=_make_typed(
            "NINA.Astrometry.InputTarget, NINA.Astrometry",
            Expanded=True, TargetName=target.name,
            PositionAngle=target.rotation,
            InputCoordinates=_coords(target)),
    )


def generate_focus_calibration_json(name: str = "NGC 7789",
                                    ra_hours: float = 23.957,
                                    dec_degrees: float = 56.708,
                                    rounds: int = 1,
                                    filters: list[str] | None = None) -> str:
    """A whole-night NINA sequence (same startup, safety loop and shutdown as
    a normal night) whose only target is a focus-offset calibration. Default
    field NGC 7789: a rich open cluster that fills the RC16 frame and is high
    from Rodeo on autumn evenings. Pass another bright, uncrowded field any
    other season."""
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    t = NinaSequenceTarget(name=name, ra_hours=ra_hours,
                           dec_degrees=dec_degrees, focus_calibration=True,
                           focus_calibration_rounds=rounds,
                           focus_calibration_filters=list(filters or []))
    seq = build_sequence_for_night(f"{FOCUS_CAL_PREFIX}{name}", [t])
    return generate_nina_json(seq)


# --- PS-84 unguided tracking test --------------------------------------------

TRACKING_TEST_FILTERS = ("L", "Ha")
TRACKING_TEST_EXPOSURES = (60.0, 120.0, 180.0, 300.0)
TRACKING_TEST_REPEATS = 2
# Rough overheads for the duration estimate (and the target picker's "does
# it cross the meridian during the test" check): download + save per sub,
# one autofocus run, one plate-solve center, the initial slew.
_TT_DOWNLOAD_S = 6.0
_TT_AF_S = 240.0
_TT_CENTER_S = 90.0
_TT_SLEW_S = 120.0


def _tracking_test_filters(names) -> list:
    """Filter names ('L', 'Ha', 'H', 'luminance', ...) to FilterTypes, in the
    given order, de-duplicated; calibration frame types and unknown names are
    dropped. Empty input gives the default L then Ha."""
    ft_by = {ft.value.lower(): ft for ft in FilterType}
    ft_by.update({ft.name.lower(): ft for ft in FilterType})
    try:
        ft_by.update({str(v).lower(): FilterType(k) for k, v in
                      _gen_cfg().filter_name_map().items()
                      if k in {ft.value for ft in FilterType}})
    except Exception:  # noqa: BLE001 - NINA names are a convenience only
        pass
    skip = {FilterType.DARK, FilterType.FLAT, FilterType.BIAS,
            FilterType.OSC}  # PS-30: OSC is the piggyback, no RC16 wheel slot
    out = []
    for n in [str(x).strip() for x in (names or TRACKING_TEST_FILTERS)]:
        ft = ft_by.get(n.lower())
        if ft is not None and ft not in skip and ft not in out:
            out.append(ft)
    return out


def _tracking_test_exposures(values) -> list[float]:
    """Positive exposure lengths (s), ascending and de-duplicated, so the
    ladder always climbs from short to long."""
    out = set()
    for v in (values or TRACKING_TEST_EXPOSURES):
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if 0 < x <= 3600:
            out.add(round(x, 3))
    return sorted(out)


def tracking_test_duration_s(n_filters: int, exposures, repeats: int) -> float:
    """Estimated wall-clock length of the test (s): the ladder itself plus
    download, one AF and one center per filter, and the first slew."""
    exps = _tracking_test_exposures(exposures)
    n_subs = n_filters * max(1, int(repeats)) * len(exps)
    return (n_filters * max(1, int(repeats)) * sum(exps)
            + n_subs * _TT_DOWNLOAD_S + n_filters * (_TT_AF_S + _TT_CENTER_S)
            + _TT_SLEW_S)


def _build_tracking_test_container(target: NinaSequenceTarget,
                                   min_altitude: float,
                                   af_filter: "FilterType | None",
                                   focus_offsets: dict | None,
                                   loop_end: tuple | None,
                                   cooler_gate: tuple | None = None) -> dict:
    """PS-84 unguided tracking test (TPoint + ProTrack check on the Paramount
    MX): StopGuiding, cool, slew, AF on the reference filter (L), center, then
    for each filter an exposure ladder with `tracking_test_repeats` subs per
    length. Between filters the mount re-centers and refocuses on L, then the
    measured filter offset is applied (PS-65). No StartGuiding and no active
    dither anywhere. Every light SmartExposure carries Safety + the loop-end
    TimeCondition (PS-77); the ladder container adds Safety, Altitude, the
    loop end and LoopCondition(1) so it runs once per entry.

    Only a temperature refocus trigger rides on the ladder: an HFR-increase
    trigger would fire on trailed stars (the very thing being measured) and
    refocus mid-ladder."""
    ref = af_filter or FilterType.LUMINANCE
    filters = _tracking_test_filters(target.tracking_test_filters)
    exposures = _tracking_test_exposures(target.tracking_test_exposures)
    repeats = max(1, int(target.tracking_test_repeats or 1))
    cfg = _gen_cfg()
    gain = int(getattr(cfg, "default_gain", 100))
    offset_adu = int(getattr(cfg, "default_offset", 50))
    temp = rig_cool_setpoint(cfg, "rc16", target)   # PS-154
    est_h = tracking_test_duration_s(len(filters), exposures, repeats) / 3600
    ladder_desc = (", ".join(f.value for f in filters) + " x "
                   + "/".join(f"{e:g}" for e in exposures)
                   + f" s, {repeats} each")

    def _guards():
        g = [_safety_condition()]
        if loop_end:
            g.append(_time_condition(*loop_end))
        return g

    items = [
        _annotation("PS-84 unguided tracking test (TPoint + ProTrack). "
                    "Guiding is stopped and never restarted; dithers are off. "
                    f"Ladder: {ladder_desc}. Subs are named '{target.name}'. "
                    "Afterwards open /api/tracking-test/report on the "
                    "dashboard. " + TRACKING_TEST_PARK_NOTE),
        _pushover("Imaging", f"{target.name}: unguided tracking test, "
                  f"slewing (RA {target.ra_hours:.2f}h Dec "
                  f"{target.dec_degrees:+.1f} deg) - {ladder_desc} "
                  f"(~{est_h:.1f} h)"),
        _stop_guiding(),
        _cool_camera_bounded(temp, 0.0, cfg),   # PS-154: never blocks
        _set_tracking(0),
        _slew(target),
        _switch_filter(ref),
        _move_focuser(_seed_position(ref)),
        _autofocus(),
        _center(target),
        _set_tracking(0),
        _pushover("Imaging", f"{target.name}: focused on {ref.value}, "
                  "centered, guiding stopped - starting the unguided ladder"),
    ]
    # PS-61: one cooler gate in front of the whole ladder; a SKIP interrupts
    # the ladder container (it runs once per entry anyway).
    ladder = ([_cooler_gate(cooler_gate, "rc16", f"{target.name} ladder")]
              if cooler_gate else [])
    for i, f in enumerate(filters):
        off = _focus_offset(ref, f, focus_offsets)
        if i > 0:
            # Re-center once between filters (drift from the first ladder must
            # not carry into the second), refocus on L, apply the offset.
            ladder += [_center(target), _set_tracking(0),
                       _switch_filter(ref), _autofocus()]
        if off:
            ladder.append(_move_focuser_relative(off))
        for e in exposures:
            exp = ExposurePlan(filter_type=f, exposure_seconds=e,
                               count=repeats, gain=gain, offset=offset_adu)
            ladder.append(_smart_exposure(
                exp, False, 0, guard_conditions=_guards(),
                extra_triggers=[_autofocus_temp_trigger(
                    2.0, _block_af_runner(ref, off, f))]))   # PS-176
        ladder.append(_pushover("Imaging", f"{target.name}: {f.value} ladder "
                                f"done ({repeats} x {len(exposures)} subs)"))
    ladder_conds = [_safety_condition(),
                    _altitude_condition(target, min_altitude)]
    if loop_end:
        ladder_conds.append(_time_condition(*loop_end))
    ladder_conds.append(_loop_once())
    items.append(_seq_container(f"{target.name}{TARGET_TRACKING_LADDER_SUFFIX}",
                                ladder, conditions=ladder_conds))
    items.append(_pushover("Imaging", f"{target.name}: tracking test done - "
                           "open /api/tracking-test/report"))
    return _seq_container(
        target.name, items,
        conditions=[_safety_condition(),
                    _altitude_condition(target, min_altitude),
                    _loop_once()],
        triggers=[_meridian_flip_trigger(), _reconnect_trigger()],
        container_type="NINA.Sequencer.Container.DeepSkyObjectContainer, "
                       "NINA.Sequencer",
        Target=_make_typed(
            "NINA.Astrometry.InputTarget, NINA.Astrometry",
            Expanded=True, TargetName=target.name,
            PositionAngle=target.rotation,
            InputCoordinates=_coords(target)),
    )


def tracking_test_name(field: str) -> str:
    """'Heart Nebula' -> 'Tracking test Heart Nebula' (idempotent)."""
    f = str(field or "").strip() or "field"
    if f.lower().startswith(TRACKING_TEST_PREFIX.lower()):
        return f
    return f"{TRACKING_TEST_PREFIX}{f}"


def generate_tracking_test_json(name: str = "Heart Nebula",
                                ra_hours: float = 2.555,
                                dec_degrees: float = 61.47,
                                filters: list[str] | None = None,
                                exposures: list[float] | None = None,
                                repeats: int = TRACKING_TEST_REPEATS,
                                min_altitude: float = 30.0) -> str:
    """PS-84: a whole-night NINA sequence (same startup, safety loop and
    shutdown as a normal night) whose only target is an unguided tracking
    test on one field. Generated only: nothing is sent to NINA. Load it by
    hand in NINA #1 after the TPoint model is built and ProTrack is on."""
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    cfg = _gen_cfg()
    tname = tracking_test_name(name)
    t = NinaSequenceTarget(
        name=tname, ra_hours=ra_hours, dec_degrees=dec_degrees,
        start_guiding=False, dither_every_n=0,
        camera_temp_c=float(getattr(cfg, "camera_setpoint_c", 0.0)),
        tracking_test=True,
        tracking_test_filters=[str(f) for f in (filters or [])],
        tracking_test_exposures=(_tracking_test_exposures(exposures)
                                 if exposures else []),
        tracking_test_repeats=max(1, int(repeats or 1)))
    seq = build_sequence_for_night(tname, [t], min_altitude=min_altitude)
    return generate_nina_json(seq)


# --- PS-148 through-focus optics test ----------------------------------------

OPTICS_TEST_OFFSETS = (-300, -150, 150, 300)
OPTICS_TEST_FILTERS = ("L",)
OPTICS_TEST_EXPOSURE_S = 45.0
OPTICS_TEST_NB_EXPOSURE_S = 120.0
OPTICS_TEST_REPEATS = 2
_NB_FILTERS = ("Ha", "OIII", "SII")


def optics_test_name(field: str) -> str:
    """'M 2' -> 'Optics test M 2' (idempotent)."""
    f = str(field or "").strip() or "field"
    if f.lower().startswith(OPTICS_TEST_PREFIX.lower()):
        return f
    return f"{OPTICS_TEST_PREFIX}{f}"


def optics_offset_label(offset: int) -> str:
    """0 -> '0', 150 -> '+150', -300 -> '-300'."""
    o = int(offset)
    return "0" if o == 0 else f"{o:+d}"


def optics_step_name(test_name: str, filter_value: str, offset: int) -> str:
    """The OBJECT every sub of one step carries:
    'Optics test M 2 L -300' (scheduler/optics_test.parse_name reads it)."""
    return (f"{optics_test_name(test_name)} {filter_value} "
            f"{optics_offset_label(offset)}")


def _optics_test_offsets(values) -> list[int]:
    """Non-zero integer offsets (EAF steps), ascending and de-duplicated, so
    the sweep moves the focuser one way only (one backlash direction).
    None gives the default -300/-150/+150/+300."""
    out = set()
    for v in (OPTICS_TEST_OFFSETS if values is None else values):
        try:
            x = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if x and abs(x) <= 5000:
            out.add(x)
    return sorted(out)


def optics_test_exposure_for(filter_type: FilterType, exposure_s: float,
                             nb_exposure_s: float) -> float:
    return float(nb_exposure_s if filter_type.value in _NB_FILTERS
                 else exposure_s)


def optics_test_duration_s(filters, offsets, exposure_s: float,
                           nb_exposure_s: float, repeats: int) -> float:
    """Estimated wall-clock length (s): every step's subs plus download,
    one AF, one center and the first slew."""
    fts = _tracking_test_filters(filters or OPTICS_TEST_FILTERS)
    n_steps = len(_optics_test_offsets(offsets)) + 1
    rep = max(1, int(repeats or 1))
    shoot = sum(optics_test_exposure_for(f, exposure_s, nb_exposure_s)
                for f in fts) * n_steps * rep
    return (shoot + len(fts) * n_steps * rep * _TT_DOWNLOAD_S
            + _TT_AF_S + _TT_CENTER_S + _TT_SLEW_S)


def _build_optics_test_container(target: NinaSequenceTarget,
                                 min_altitude: float,
                                 af_filter: "FilterType | None",
                                 focus_offsets: dict | None,
                                 loop_end: tuple | None,
                                 cooler_gate: tuple | None = None) -> dict:
    """PS-148 through-focus optics test (astigmatism / collimation check):
    StopGuiding, cool, sidereal tracking, slew, AF on the reference filter
    (L), center. Then per filter: the filter's focus offset (PS-65), subs at
    best focus, then at each configured offset in ascending order (inside a
    sweep the focuser only moves outward, one backlash direction), then back
    to the L best focus. Every step is its own nested DeepSkyObjectContainer
    whose TargetName is "Optics test <field> <filter> <offset>", so NINA
    writes that OBJECT on each sub and the report knows the offset without
    reading the focuser.

    No autofocus trigger anywhere: an HFR or temperature refocus would fire
    on the defocused steps and wreck the offsets. No StartGuiding, no active
    dither. An unsafe spell ends the sweep (Safety conditions) and can leave
    the focuser off focus; every later target starts with its own seed move
    and autofocus, so nothing images defocused."""
    ref = af_filter or FilterType.LUMINANCE
    filters = _tracking_test_filters(target.optics_test_filters
                                     or list(OPTICS_TEST_FILTERS))
    offsets = _optics_test_offsets(target.optics_test_offsets
                                   if target.optics_test_offsets else None)
    repeats = max(1, int(target.optics_test_repeats or 1))
    exp_bb = float(target.optics_test_exposure_s or OPTICS_TEST_EXPOSURE_S)
    exp_nb = float(target.optics_test_nb_exposure_s
                   or OPTICS_TEST_NB_EXPOSURE_S)
    cfg = _gen_cfg()
    gain = int(getattr(cfg, "default_gain", 100))
    offset_adu = int(getattr(cfg, "default_offset", 50))
    temp = rig_cool_setpoint(cfg, "rc16", target)   # PS-154
    est_min = optics_test_duration_s([f.value for f in filters], offsets,
                                     exp_bb, exp_nb, repeats) / 60
    sweep_desc = (", ".join(
        f"{f.value} {optics_test_exposure_for(f, exp_bb, exp_nb):g} s"
        for f in filters)
        + " at 0 then " + "/".join(optics_offset_label(o) for o in offsets)
        + f" steps, {repeats} each")

    def _guards():
        g = [_safety_condition()]
        if loop_end:
            g.append(_time_condition(*loop_end))
        return g

    def _step(f: FilterType, off: int) -> dict:
        sname = optics_step_name(target.name, f.value, off)
        exp = ExposurePlan(filter_type=f,
                           exposure_seconds=optics_test_exposure_for(
                               f, exp_bb, exp_nb),
                           count=repeats, gain=gain, offset=offset_adu)
        return _seq_container(
            f"{sname}{OPTICS_STEP_SUFFIX}",
            [_smart_exposure(exp, False, 0, guard_conditions=_guards())],
            conditions=[_safety_condition(), _loop_once()],
            container_type=DSO_CONTAINER_TYPE,
            Target=_make_typed(
                "NINA.Astrometry.InputTarget, NINA.Astrometry",
                Expanded=True, TargetName=sname,
                PositionAngle=target.rotation,
                InputCoordinates=_coords(target)))

    items = [
        _annotation("PS-148 through-focus optics test (astigmatism / "
                    "collimation). Guiding is stopped, tracking is on, no "
                    f"autofocus triggers. Sweep: {sweep_desc}. Each step's "
                    f"subs are named '{target.name} <filter> <offset>'. "
                    "Afterwards open /api/optics-test/report (or the runs "
                    "page Optics section). " + OPTICS_TEST_PARK_NOTE),
        _pushover("Imaging", f"{target.name}: through-focus optics test, "
                  f"slewing (RA {target.ra_hours:.2f}h Dec "
                  f"{target.dec_degrees:+.1f} deg) - {sweep_desc} "
                  f"(~{est_min:.0f} min)"),
        _stop_guiding(),
        _cool_camera_bounded(temp, 0.0, cfg),   # PS-154: never blocks
        _set_tracking(0),
        _slew(target),
        _switch_filter(ref),
        _move_focuser(_seed_position(ref)),
        _autofocus(),
        _center(target),
        _set_tracking(0),
        _pushover("Imaging", f"{target.name}: focused on {ref.value}, "
                  "centered, guiding stopped - starting the focus sweep"),
    ]
    sweep = ([_cooler_gate(cooler_gate, "rc16", f"{target.name} sweep")]
             if cooler_gate else [])
    for f in filters:
        foff = _focus_offset(ref, f, focus_offsets)
        if foff:
            sweep.append(_move_focuser_relative(foff))
        sweep.append(_step(f, 0))
        cur = 0
        for o in offsets:
            sweep.append(_move_focuser_relative(o - cur))
            cur = o
            sweep.append(_step(f, o))
        back = -cur - foff
        if back:
            sweep.append(_move_focuser_relative(back))
        sweep.append(_pushover("Imaging", f"{target.name}: {f.value} sweep "
                               f"done ({len(offsets) + 1} steps), focuser "
                               f"back at {ref.value} best focus"))
    sweep_conds = [_safety_condition(),
                   _altitude_condition(target, min_altitude)]
    if loop_end:
        sweep_conds.append(_time_condition(*loop_end))
    sweep_conds.append(_loop_once())
    items.append(_seq_container(f"{target.name}{TARGET_OPTICS_SWEEP_SUFFIX}",
                                sweep, conditions=sweep_conds))
    items.append(_pushover("Imaging", f"{target.name}: optics test done - "
                           "open /api/optics-test/report"))
    return _seq_container(
        target.name, items,
        conditions=[_safety_condition(),
                    _altitude_condition(target, min_altitude),
                    _loop_once()],
        triggers=[_meridian_flip_trigger(), _reconnect_trigger()],
        container_type=DSO_CONTAINER_TYPE,
        Target=_make_typed(
            "NINA.Astrometry.InputTarget, NINA.Astrometry",
            Expanded=True, TargetName=target.name,
            PositionAngle=target.rotation,
            InputCoordinates=_coords(target)),
    )


def generate_optics_test_json(name: str = "Heart Nebula",
                              ra_hours: float = 2.555,
                              dec_degrees: float = 61.47,
                              filters: list[str] | None = None,
                              offsets: list[int] | None = None,
                              exposure_s: float = OPTICS_TEST_EXPOSURE_S,
                              nb_exposure_s: float = OPTICS_TEST_NB_EXPOSURE_S,
                              repeats: int = OPTICS_TEST_REPEATS,
                              min_altitude: float = 30.0) -> str:
    """PS-148: a whole-night NINA sequence (same startup, safety loop and
    shutdown as a normal night) whose only target is the through-focus
    optics test on one field. Generated only: nothing is sent to NINA (the
    sideload recipe optics_through_focus splices it before tonight's
    targets)."""
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    cfg = _gen_cfg()
    tname = optics_test_name(name)
    t = NinaSequenceTarget(
        name=tname, ra_hours=ra_hours, dec_degrees=dec_degrees,
        start_guiding=False, dither_every_n=0,
        camera_temp_c=float(getattr(cfg, "camera_setpoint_c", 0.0)),
        optics_test=True,
        optics_test_filters=[str(f) for f in (filters or [])],
        optics_test_offsets=_optics_test_offsets(offsets),
        optics_test_exposure_s=float(exposure_s or OPTICS_TEST_EXPOSURE_S),
        optics_test_nb_exposure_s=float(nb_exposure_s
                                        or OPTICS_TEST_NB_EXPOSURE_S),
        optics_test_repeats=max(1, int(repeats or 1)))
    seq = build_sequence_for_night(tname, [t], min_altitude=min_altitude)
    return generate_nina_json(seq)


# --- PS-171 TPoint mapping run ----------------------------------------------

TPOINT_EXPOSURE_S = 5.0
TPOINT_BINNING = 2
_TPOINT_SLEW_S = 25.0       # per point: a short alt/az hop on the Paramount
_TPOINT_DOWNLOAD_S = 4.0
_TPOINT_SOLVE_S = 20.0      # the tpoint-sample script (TheSky Image Link)


def _dms(value: float) -> tuple:
    v = abs(float(value))
    d = int(v)
    m = int((v - d) * 60)
    sec = round(((v - d) * 60 - m) * 60, 1)
    if sec >= 60.0:
        sec, m = 0.0, m + 1
    if m >= 60:
        m, d = 0, d + 1
    return d, m, sec


def _slew_alt_az_exact(alt_deg: float, az_deg: float) -> dict:
    """SlewScopeToAltAz to a fractional alt / az (deg, min, sec fields)."""
    ad, am, asec = _dms(alt_deg)
    zd, zm, zsec = _dms(float(az_deg) % 360.0)
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Telescope.SlewScopeToAltAz, NINA.Sequencer",
        Coordinates=_make_typed(
            "NINA.Astrometry.InputTopocentricCoordinates, NINA.Astrometry",
            AzDegrees=zd, AzMinutes=zm, AzSeconds=zsec,
            AltDegrees=ad, AltMinutes=am, AltSeconds=asec),
        ErrorBehavior=0, Attempts=1)


def tpoint_mapping_name(n_points: int) -> str:
    return f"{TPOINT_MAPPING_PREFIX}{int(n_points)} points"


def tpoint_point_name(i: int, n: int, alt: float, az: float, side: str) -> str:
    """'TPoint point 07/60 alt 45.0 az 120.0 (east)': the container name and
    the OBJECT of the point's frame (tpoint-sample reads it back)."""
    w = max(2, len(str(int(n))))
    return (f"{TPOINT_POINT_PREFIX}{int(i):0{w}d}/{int(n):0{w}d} "
            f"alt {float(alt):.1f} az {float(az):.1f} ({side})")


def tpoint_script_args(i: int, n: int, alt: float, az: float, side: str) -> str:
    """Arguments after the script path (see deploy/tpoint-sample.cmd)."""
    return (f"--rig rc16 --point {int(i)} --of {int(n)} "
            f"--alt {float(alt):.2f} --az {float(az):.2f} --side {side}")


def tpoint_mapping_duration_s(n_points: int, exposure_s: float) -> float:
    """Estimated wall-clock length (s): per point a slew, the frame, its
    download and the Image Link script; plus the first slew and one AF."""
    per = _TPOINT_SLEW_S + float(exposure_s) + _TPOINT_DOWNLOAD_S + _TPOINT_SOLVE_S
    return int(n_points) * per + _TT_AF_S + _TT_SLEW_S


def _build_tpoint_mapping_container(target: NinaSequenceTarget,
                                    af_filter: "FilterType | None",
                                    loop_end: tuple | None,
                                    cooler_gate: tuple | None = None) -> dict:
    """PS-171 TPoint mapping run: StopGuiding, cool, sidereal tracking, Slew
    to Alt/Az to the first point, AF on L there. Then for every point (east
    of the meridian first, then west: one pier flip) a run-once container:
    Slew to Alt/Az (no center, no sync), one short L frame, then the
    tpoint-sample ExternalScript (ErrorBehavior 0; the script always exits
    0, so a failed solve never stops the run). Each point container carries
    Safety + the loop end itself (it holds a LIGHT exposure, PS-77).

    No Platesolving.Center anywhere: NINA's Center syncs the mount, which
    would corrupt the very pointing errors TPoint is measuring. No
    MeridianFlipTrigger: the points are alt/az, so each one sits on a fixed
    side of the meridian, and the flip's re-center would sync too. The
    target has no AltitudeCondition (its coordinates are nominal); every
    point is above tpoint_mapping_min_alt by construction."""
    ref = af_filter or FilterType.LUMINANCE
    pts = [(float(p[0]), float(p[1]), str(p[2]) if len(p) > 2 else "")
           for p in (target.tpoint_points or [])]
    n = len(pts)
    exp_s = float(target.tpoint_exposure_s or TPOINT_EXPOSURE_S)
    binning = int(target.tpoint_binning or 1)
    script = str(target.tpoint_script or "")
    cfg = _gen_cfg()
    gain = int(getattr(cfg, "default_gain", 100))
    offset_adu = int(getattr(cfg, "default_offset", 50))
    temp = rig_cool_setpoint(cfg, "rc16", target)   # PS-154
    est_min = tpoint_mapping_duration_s(n, exp_s) / 60
    east = sum(1 for p in pts if p[2] == "east")

    def _point(i: int, alt: float, az: float, side: str) -> dict:
        pname = tpoint_point_name(i, n, alt, az, side)
        items = [
            _slew_alt_az_exact(alt, az),
            _make_typed(
                "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer",
                ExposureTime=exp_s, Gain=gain, Offset=offset_adu,
                Binning=_make_typed(
                    "NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                    X=binning, Y=binning),
                ImageType="LIGHT", ExposureCount=0,
                ErrorBehavior=0, Attempts=1),
        ]
        if script:
            items.append(_external_script(
                script, tpoint_script_args(i, n, alt, az, side)))
        conds = [_safety_condition()]
        if loop_end:
            conds.append(_time_condition(*loop_end))
        conds.append(_loop_once())
        # a nested DeepSkyObjectContainer so the frame's OBJECT is the
        # point name (the QA test-sub rule and the CSV read it)
        return _seq_container(
            pname, items, conditions=conds, container_type=DSO_CONTAINER_TYPE,
            Target=_make_typed(
                "NINA.Astrometry.InputTarget, NINA.Astrometry",
                Expanded=True, TargetName=pname, PositionAngle=0.0,
                InputCoordinates=_coords(target)))

    first = pts[0] if pts else (60.0, 90.0, "east")
    items = [
        _annotation(f"PS-171 TPoint mapping: {n} points ({east} east of the "
                    f"meridian, then {n - east} west), {exp_s:g} s L bin "
                    f"{binning} each, ~{est_min:.0f} min. "
                    + TPOINT_MAPPING_NOTE + " Watch TheSky's TPoint window; "
                    "Stop in NINA #1 ends it. Samples: runs/<night>_tpoint.csv."),
        _pushover("Imaging", f"TPoint mapping: {n} points, ~{est_min:.0f} min"
                  + ("" if script else " - NO tpoint-sample script on this "
                     "machine: frames only")),
        _stop_guiding(),
        _cool_camera_bounded(temp, 0.0, cfg),   # PS-154: never blocks
        _set_tracking(0),
        _slew_alt_az_exact(first[0], first[1]),
        _switch_filter(ref),
        _move_focuser(_seed_position(ref)),
        _autofocus(),
        _set_tracking(0),
    ]
    loop = ([_cooler_gate(cooler_gate, "rc16", "TPoint mapping")]
            if cooler_gate else [])
    for i, (alt, az, side) in enumerate(pts, 1):
        if i > 1 and side != pts[i - 2][2]:
            loop.append(_pushover("Imaging", f"TPoint mapping: {side} side "
                                  f"(point {i}/{n}), the mount flips once"))
        loop.append(_point(i, alt, az, side))
    loop_conds = [_safety_condition()]
    if loop_end:
        loop_conds.append(_time_condition(*loop_end))
    loop_conds.append(_loop_once())
    items.append(_seq_container(f"{target.name}{TARGET_TPOINT_LOOP_SUFFIX}",
                                loop, conditions=loop_conds))
    items.append(_pushover("Imaging", "TPoint mapping done - check TheSky's "
                           "TPoint window and runs/<night>_tpoint.csv"))
    return _seq_container(
        target.name, items,
        conditions=[_safety_condition(), _loop_once()],
        triggers=[_reconnect_trigger()],
        container_type=DSO_CONTAINER_TYPE,
        Target=_make_typed(
            "NINA.Astrometry.InputTarget, NINA.Astrometry",
            Expanded=True, TargetName=target.name,
            PositionAngle=target.rotation,
            InputCoordinates=_coords(target)),
    )


def generate_tpoint_mapping_json(points: list, ra_hours: float = 0.0,
                                 dec_degrees: float = 31.9,
                                 exposure_s: float = TPOINT_EXPOSURE_S,
                                 binning: int = TPOINT_BINNING,
                                 script: str = "",
                                 min_altitude: float = 30.0) -> str:
    """PS-171: a whole-night NINA sequence (same startup, safety loop and
    shutdown as a normal night) whose only target is the TPoint mapping
    run over `points` ([alt, az, side], in run order). ra / dec are the
    target's nominal coordinates (the zenith at the start). Generated only:
    the sideload recipe tpoint_mapping_then_tonight splices it before
    tonight's targets."""
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    cfg = _gen_cfg()
    pts = [[round(float(p[0]), 2), round(float(p[1]), 2),
            str(p[2]) if len(p) > 2 else ""] for p in points]
    tname = tpoint_mapping_name(len(pts))
    t = NinaSequenceTarget(
        name=tname, ra_hours=float(ra_hours) % 24.0,
        dec_degrees=max(-89.0, min(89.0, float(dec_degrees))),
        start_guiding=False, dither_every_n=0,
        camera_temp_c=float(getattr(cfg, "camera_setpoint_c", 0.0)),
        tpoint_mapping=True, tpoint_points=pts,
        tpoint_exposure_s=float(exposure_s or TPOINT_EXPOSURE_S),
        tpoint_binning=max(1, min(int(binning or 1), 4)),
        tpoint_script=str(script or ""))
    seq = build_sequence_for_night(tname, [t], min_altitude=min_altitude)
    return generate_nina_json(seq)


# --- PS-93 PHD2 calibration slot ---------------------------------------------

def _start_guiding_calibrate() -> dict:
    """StartGuiding that forces a fresh PHD2 calibration, one attempt,
    continue on error: a failed calibration skips only the calibration slot
    (PhotonScript grades it and retries over PHD2 during the hold)."""
    return _make_typed("NINA.Sequencer.SequenceItem.Guider.StartGuiding, "
                       "NINA.Sequencer", ForceCalibration=True,
                       ErrorBehavior=0, Attempts=1)


def _build_phd2_calibration_container(field: dict, hold_s: int = 240) -> dict:
    """PS-93: calibrate PHD2 on a star field near Dec +5, 0.25 to 1 h from
    the meridian (phd2_calibration.pick_calibration_field): SetTracking ->
    SwitchFilter(L, the OAG sits behind the wheel) -> slew -> center ->
    StartGuiding(ForceCalibration) -> StopGuiding -> hold. Every item
    continues on error and there is no flip trigger, so a failure skips only
    this slot. The hold (WaitForTimeSpan hold_s) is the window in which the
    RC16 agent grades the calibration and retries it once over PHD2. The way
    back is the first target's own slew, autofocus and center (the PS-64
    lesson: never assume the mount went back on its own)."""
    from types import SimpleNamespace
    from photonscript.scheduler.phd2_calibration import CONTAINER_NAME
    pt = SimpleNamespace(ra_hours=float(field["ra_hours"]),
                         dec_degrees=float(field["dec_degrees"]))
    name = field.get("name") or "calibration field"
    return _seq_container(CONTAINER_NAME, [
        _pushover("Guiding", f"PHD2 calibration: slewing to {name} "
                  f"(Dec {pt.dec_degrees:+.1f}, HA {field.get('ha_hours', 0):+.2f} h)"),
        _set_tracking(0),
        _switch_filter(FilterType.LUMINANCE),
        _slew(pt),
        _center(pt),
        _start_guiding_calibrate(),
        _stop_guiding(),
        _pushover("Guiding", "PHD2 calibration done: PhotonScript grades it "
                  f"(and retries once) during a {int(hold_s)} s hold"),
        _wait_for_timespan(int(hold_s)),
    ])


def generate_phd2_calibration_json(field: dict, hold_s: int = 240,
                                   name: str = "PhotonScript PHD2 calibration") -> str:
    """A standalone sequence for a calibration on demand (POST
    /api/phd2/calibrate now while no night runs; GET
    /api/phd2/calibration-sequence to load by hand): connect, unpark, the
    PHD2_CALIBRATION slot, then tracking off."""
    start = [
        _connect("Mount"), _connect("Camera"), _connect("Filter Wheel"),
        _connect("Guider"), _unpark(),
        _build_phd2_calibration_container(field, hold_s),
        _set_tracking(5),
    ]
    root = _seq_container(
        name,
        [_seq_container("Start", [_seq_container("PHD2 calibration", start)],
                        container_type="NINA.Sequencer.Container."
                                       "StartAreaContainer, NINA.Sequencer"),
         _seq_container("Targets", [], container_type="NINA.Sequencer.Container."
                                                      "TargetAreaContainer, NINA.Sequencer"),
         _seq_container("End", [], container_type="NINA.Sequencer.Container."
                                                  "EndAreaContainer, NINA.Sequencer")],
        container_type="NINA.Sequencer.Container.SequenceRootContainer, "
                       "NINA.Sequencer")
    return json.dumps(link_parents(root), indent=2)


def generate_nina_json(sequence: NinaSequenceFile,
                       cal_field: dict | None = None,
                       unguided_dither: bool = False) -> str:
    """Generate an Advanced Sequencer JSON with the full night-loop safety
    architecture (Jerry Macon / Patriot Astro pattern, all core NINA types):

    Start:   connect safety monitor -> WaitUntilSafe -> connect everything,
             cool during twilight, twilight autofocus, hold for astro dusk
    Targets: LOOP_ALL_NIGHT (until dawn)
               SAFE_LOOP (while safe): re-arm equipment, run targets
               UNSAFE: park, WaitUntilSafe, loop resumes automatically
    End:     stop guiding, park, warm, disconnect — always runs at dawn

    cal_field (PS-93, from the armer when phd2_calibration.needs_calibration
    says so): a PHD2_CALIBRATION slot after the twilight autofocus (and the
    PS-92 twilight self-test), before the imaging gate; on a late arm or a
    re-dispatch, right after the unpark. With the slot no target sets
    ForceCalibration (it overrides the PS-72 switch). None = no slot, the
    sequence is exactly as before.

    unguided_dither (PS-66): unguided targets keep their dither trigger
    active (NINA Direct Guider). False (default) = AfterExposures 0 on
    every unguided SmartExposure, as before.
    """
    from photonscript.shared.config import PhotonScriptConfig
    _cfg = PhotonScriptConfig()
    af_ft = _af_filter_type(_cfg)  # bright AF filter (e.g. L), or None
    guided = any(t.start_guiding for t in sequence.targets)
    selftest = _selftest_script(_cfg) if guided else None  # PS-92 slots
    # PS-154: the configured RC16 setpoint, never a target's camera_temp_c
    # (2026-10-06: the dusk focus-calibration target carried the old -10 C
    # model default into the Start area and the night stalled on it)
    temp = rig_cool_setpoint(_cfg, "rc16",
                             sequence.targets[0] if sequence.targets else None)
    for _t in sequence.targets[1:]:
        rig_cool_setpoint(_cfg, "rc16", _t)   # logs an ignored override
    gate_dark = sequence.wait_until_local is not None

    # Filter-aware imaging gate: narrowband rejects twilight glow, so an
    # Ha/SII-first night can start ~35 min earlier (sun ~-13/-14 deg) and,
    # if ALL targets are narrowband, run ~35 min later into morning twilight.
    NB = ("Ha", "SII", "OIII")
    first_exposures = [e for t in sequence.targets for e in t.exposures
                       if e.count - e.acquired > 0 or e.short_remaining() > 0]
    first_is_nb = bool(first_exposures) and         first_exposures[0].filter_type.value in NB
    all_nb = bool(first_exposures) and all(
        e.filter_type.value in NB for e in first_exposures)
    if first_is_nb:
        gate_provider, gate_offset = "NauticalDuskProvider", 10
        gate_msg = ("nautical dusk +10 — narrowband can start in twilight; "
                    "night loop begins")
    else:
        gate_provider, gate_offset = "DuskProvider", 0
        gate_msg = "astro dusk — night loop begins (runs until dawn)"
    if all_nb:
        dawn_provider, dawn_offset = "NauticalDawnProvider", -10
    else:
        dawn_provider, dawn_offset = "DawnProvider", 0

    # ---- Start area: cold start + twilight prep --------------------------
    # Cooling must NOT start at arm. After arming, the sequence idles at the
    # WaitForTime below, then starts cooling a configurable lead before
    # ASTRONOMICAL dark (default 30 min). If armed after that point,
    # WaitForTime returns immediately. The setpoint itself is unchanged
    # (config.camera_setpoint_c — currently 0 C).
    cool_lead = int(getattr(_gen_cfg(), "cool_lead_minutes", 30))
    start_items = [
        _pushover("Startup", f"{sequence.name}: standby — "
                  f"{len(sequence.targets)} target(s) queued; cooler stays OFF "
                  f"until ~{cool_lead}m before astro dark, then cools to "
                  f"{temp:.0f}°C"),
        _wait_for_provider("DuskProvider", -cool_lead),
    ]
    startup_dark_blocks = _dark_quota_blocks(dawn_provider, dawn_offset)
    start_unsafe_darks = _seq_container(
        "STARTUP_DARKS_IF_UNSAFE",
        [_wait_for_provider("DuskProvider", 0)] + startup_dark_blocks
        if startup_dark_blocks else [],
        conditions=[_make_typed(
            "NINA.Sequencer.Conditions.LoopWhileUnsafe, NINA.Sequencer"),
            _make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=1),
            _time_condition(dawn_provider, dawn_offset)])
    # Bias only needs an occasional top-up (it barely ages). Gate the
    # roof-closed 50-bias block on library age so a run of cloudy nights
    # doesn't bank 50 bias every single night. bias_refresh_days<=0 keeps the
    # legacy "capture whenever unsafe" behavior; None age (empty library)
    # always captures.
    _bias_refresh_days = int(getattr(_gen_cfg(), "bias_refresh_days", 60))
    try:
        from photonscript.scheduler.calibration import days_since_last_bias
        _bias_age = days_since_last_bias(_gen_cfg(), rig="rc16")
    except Exception:  # noqa: BLE001
        _bias_age = None
    _bias_due = (_bias_refresh_days <= 0 or _bias_age is None
                 or _bias_age >= _bias_refresh_days)
    if _bias_due:
        bias_if_still_unsafe = _seq_container(
            "BIAS_IF_STILL_UNSAFE",
            [_seq_container("50 bias", [_make_typed(
                "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
                "NINA.Sequencer",
                ExposureTime=0.001,
                Gain=_gen_cfg().default_gain, Offset=_gen_cfg().default_offset,
                Binning=_make_typed(
                    "NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                    X=1, Y=1),
                ImageType="BIAS", ExposureCount=0,
                ErrorBehavior=0, Attempts=1)],
                conditions=[_make_typed(
                    "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
                    CompletedIterations=0, Iterations=50)])],
            # skipped entirely if the sky is safe by the time we get here;
            # LoopCondition(1) makes it a one-shot when we are still unsafe
            conditions=[_make_typed(
                "NINA.Sequencer.Conditions.LoopWhileUnsafe, NINA.Sequencer"),
                _make_typed(
                "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
                CompletedIterations=0, Iterations=1)])
    else:
        bias_if_still_unsafe = _annotation(
            f"BIAS_IF_STILL_UNSAFE skipped: library bias (at the lights' readout "
            f"mode) is {_bias_age}d old "
            f"(refresh every {_bias_refresh_days}d)")
    start_items += [
        _connect("Safety Monitor"),
        _connect("Camera"),
        _dew_heater(True),
        # PS-154: bounded (cooler_gate_timeout_min), never blocks the unpark
        _cool_camera_bounded(temp, float(getattr(_cfg, "cool_ramp_minutes", 0.0)),
                             _cfg),
        _connect("Filter Wheel"),
        _connect("Focuser"),
        _connect("Mount"),
        _connect("Guider"),
        _connect("Weather"),
        _pushover("Startup", f"camera cooling to {temp:.0f}°C; if the night "
                  "starts UNSAFE the roof-closed time fills the dark-library "
                  "quota until conditions clear"),
        start_unsafe_darks,
        bias_if_still_unsafe,
        _wait_until_safe(),
        _pushover("Startup", "safety monitor SAFE — unparking"),
        _unpark(),
        _set_tracking(5),
        _pushover("Startup", "holding until nautical dusk"),
    ]
    # PS-93: the PHD2 calibration slot (guided nights that need one only)
    cal_slot = (_build_phd2_calibration_container(
        cal_field, int(getattr(_cfg, "phd2_cal_hold_s", 240) or 240))
        if cal_field and guided else None)
    if gate_dark and sequence.targets:
        # Twilight autofocus: spend twilight, not dark time, on first focus
        first_filter = next((e.filter_type for t in sequence.targets
                             for e in t.exposures), None)
        start_items += [
            _wait_for_provider("NauticalDuskProvider", 0),
            _wait_until_safe(),
            _pushover("Startup", "nautical dusk — twilight autofocus: "
                      "slewing to alt 70° az 180°"),
            _slew_alt_az(70, 180),
            _set_tracking(0),
        ]
        # Focus on the bright AF filter (L) if configured — a twilight AF through
        # a 3nm narrowband filter starves the star field and fails (donuts).
        focus_filter = af_ft or first_filter
        if focus_filter is not None:
            start_items.append(_switch_filter(focus_filter))
            start_items.append(_move_focuser(_seed_position(focus_filter)))
        start_items += [
            _wait_for_timespan(60),
            _autofocus(),
            _pushover("Startup", "twilight autofocus complete "
                      f"(filter {focus_filter.value if focus_filter else '—'}) "
                      "— holding for the imaging gate"),
        ]
        if selftest:
            # PS-92: test the guide-pulse path while the scope tracks at the
            # twilight slot, before any guided target needs it
            start_items.append(_external_script(selftest, "twilight"))
        if cal_slot is not None:
            start_items.append(cal_slot)   # PS-93: the gate wait follows
            cal_slot = None
        start_items += [
            _wait_for_provider(gate_provider, gate_offset),
            _pushover("Startup", gate_msg),
        ]

    if cal_slot is not None:
        # late arm / re-dispatch: calibrate right after the unpark
        start_items.append(cal_slot)

    # ---- Targets area: the night loop -------------------------------------
    # PS-61: the cooler gate holds every light block until the sensor is
    # within cooler_gate_tolerance_c of the temperature the Start area cooled
    # to (`temp`); None when off or the script is missing here (then the
    # Start area says so).
    gate = _cooler_gate_spec(_cfg, temp)
    if gate is None:
        start_items += _cooler_gate_missing_notice(_cfg)
    focus_drive = _focus_drive_spec(_cfg)   # PS-76 part 2, None by default
    smart = _smart_af_spec(_cfg, af_ft)     # PS-176, None = every_block
    target_containers = []
    first_guided = True
    force_first_cal = bool(getattr(_cfg, "guiding_force_first_calibration", False))
    if cal_field and guided:
        force_first_cal = False   # PS-93: the calibration slot replaces it
    for t in sequence.targets:
        force_cal = first_guided and t.start_guiding and force_first_cal
        c = _build_target_container(t, sequence.wait_for_altitude, force_cal,
                                    af_filter=af_ft,
                                    narrate=getattr(_cfg, "pushover_verbosity",
                                                    "normal"),
                                    focus_offsets=_cfg.focus_offset_map(),
                                    loop_end=(dawn_provider, dawn_offset),
                                    selftest_script=selftest,
                                    unguided_dither=unguided_dither,
                                    cooler_gate=gate,
                                    focus_drive=focus_drive,
                                    followed=len(sequence.targets) > 1,
                                    smart=smart)
        if c is None:
            # Nothing to shoot tonight (e.g. broadband-only under a bright
            # moon): skip it rather than emit an empty container that loops
            # slew/AF/center all night (PS-27).
            target_containers.append(_annotation(
                f"{t.name}: skipped tonight (nothing to shoot: broadband "
                "deferred by the moon, or plan complete)"))
            target_containers.append(_pushover(
                "Imaging", f"{t.name}: skipped tonight — nothing to shoot "
                "(broadband deferred by the moon, or plan complete)"))
            continue
        if t.start_guiding:
            first_guided = False  # only a target that actually runs uses it
        target_containers.append(c)
    # PS-180: the leftover time at the end of the night, back to the
    # highest-priority target still up (the planner sets fill_from_utc)
    for t in sequence.targets:
        if getattr(t, "fill_from_utc", None) is None:
            continue
        c = _build_target_container(t, sequence.wait_for_altitude, False,
                                    af_filter=af_ft,
                                    narrate=getattr(_cfg, "pushover_verbosity",
                                                    "normal"),
                                    focus_offsets=_cfg.focus_offset_map(),
                                    loop_end=(dawn_provider, dawn_offset),
                                    selftest_script=selftest,
                                    unguided_dither=unguided_dither,
                                    cooler_gate=gate,
                                    focus_drive=focus_drive,
                                    followed=False, fill=True,
                                    smart=smart)
        if c is not None:
            target_containers.append(c)

    unsafe_items = [
        _pushover("Safety", "UNSAFE — imaging stopped, parking scope; will "
                  "wait and auto-resume when safe"),
    ]
    if guided:
        unsafe_items.append(_stop_guiding())
    unsafe_items.append(_park())
    if getattr(_gen_cfg(), "unsafe_darks_enabled", True):
        night_dark_blocks = _dark_quota_blocks(dawn_provider, dawn_offset)
        if night_dark_blocks:
            from photonscript.scheduler.calibration import dark_gate_items
            dark_gate = dark_gate_items(_cfg, "rc16", temp)
            if dark_gate:
                # PS-160: darks only at the setpoint; the gate's skip skips
                # this container only, never the wait-for-safe below
                night_dark_blocks = [_seq_container(
                    DARKS_AT_SETPOINT_NAME, dark_gate + night_dark_blocks)]
            unsafe_items += [
                _pushover("Safety", "roof closed — filling the dark-library "
                          "quota until conditions clear"),
            ] + night_dark_blocks
    # Confirm-safe debounce: WaitUntilSafe releases on a SINGLE safe poll, so a
    # safety monitor bouncing across the threshold used to spin park/unpark and
    # fire a Pushover on every edge (the 2026-09-11 07:07 storm). Now the sky must
    # read safe, STAY safe for safety_confirm_seconds, and still be safe before we
    # narrate + resume. The scope stays parked for the whole hold.
    _safe_confirm_s = int(getattr(_gen_cfg(), "safety_confirm_seconds", 120))
    unsafe_items += [
        _wait_until_safe(),
        # PS-149: at least 1 s, so the night loop always has a pacing item
        _wait_for_timespan(max(1, _safe_confirm_s)),
        _wait_until_safe(),
        _pushover("Safety", f"SAFE for {max(1, _safe_confirm_s // 60)} min "
                  "straight — unparking and resuming targets"),
    ]

    safe_loop = _seq_container(SAFE_LOOP_NAME, [
        _seq_container(RESET_EQUIPMENT_NAME, [
            _annotation("Runs on every safe (re)entry; harmless on first pass. "
                        "The confirm-safe hold now lives in the UNSAFE branch, "
                        "so this no longer double-waits or re-narrates."),
            _unpark(),
            _set_tracking(0),
        ]),
        _seq_container(TARGETS_LOOP_NAME, target_containers),
        _annotation("All targets done: park and hold (interruptible) until "
                    "dawn ends LOOP_ALL_NIGHT and the End area runs."),
        _pushover("Imaging", "all targets complete — parked, holding until dawn"),
        _park(),
        # PS-149: hold until the night loop's OWN end, then a pace wait. The
        # hold used to be astro dawn, but an all-narrowband night loop ends
        # at nautical dawn -10: in between, a SAFE_LOOP with its targets done
        # (set) re-ran unpark / Pushover / park / an instant wait as a busy
        # loop. The pace makes every pass cost time, and past the loop end
        # the TimeCondition sees it and ends LOOP_ALL_NIGHT.
        _wait_for_provider(dawn_provider, dawn_offset),
        _wait_for_timespan(SAFE_LOOP_PACE_S),
    ], conditions=[_safety_condition()])

    night_loop = _seq_container(NIGHT_LOOP_NAME, [
        safe_loop,
        _seq_container(UNSAFE_BRANCH_NAME, unsafe_items),
    ], conditions=[_time_condition(dawn_provider, dawn_offset)])

    # ---- End area -----------------------------------------------------------
    end_items = []
    flat_filters = []
    for t in sequence.targets:
        for e in t.exposures:
            if e.filter_type not in flat_filters:
                flat_filters.append(e.filter_type)
    # Also refresh any STALE flat filters at dawn, even if tonight didn't image
    # them — otherwise broadband flats age out across a run of narrowband-only
    # nights and never get retaken. Staleness-gated, so it's a no-op when all
    # flats are fresh. (The OSC piggyback already reshoots its one flat set every
    # arm via the companion, so this covers the RC16 half of "both scopes".)
    if getattr(_cfg, "auto_stale_flats", True):
        try:
            from photonscript.scheduler.calibration import stale_flat_filters
            _by_val = {ft.value: ft for ft in FilterType}
            # PS-160: owed filters (as used, most owed first), at most
            # calibration_dawn_flat_extra_max on top of tonight's per morning
            _extra_max = max(0, int(getattr(
                _cfg, "calibration_dawn_flat_extra_max", 3) or 0))
            _added = 0
            for _name in stale_flat_filters(_cfg):
                _ft = _by_val.get(_name)
                if _ft is not None and _ft not in flat_filters:
                    if _added >= _extra_max:
                        break
                    flat_filters.append(_ft)
                    _added += 1
        except Exception:  # noqa: BLE001
            pass
    if getattr(_cfg, "dawn_flats_enabled", True) and flat_filters:
        # Dawn goes dark->bright: broadband first (fine in the dim sky),
        # narrowband LAST when the sky is bright enough that 3nm exposures
        # fit under MaxExposure (Jeremy's correction — NB-first put the
        # narrowband filters in sky too dark for the 30s cap).
        NBF = {"Ha", "OIII", "SII"}
        flat_filters.sort(key=lambda f: f.value in NBF)
        n = int(getattr(_cfg, "flat_count", 15))
        flat_block = _seq_container(
            "DAWN_SKY_FLATS (skipped if unsafe — closed roof makes junk "
            "flats)",
            [
                _pushover("Flats", "imaging done — waiting for sky-flat "
                          f"window (nautical dawn +5), then {n} sky flats "
                          "per filter: "
                          + ", ".join(f.value for f in flat_filters)),
                _wait_for_provider("NauticalDawnProvider", 5),
                _slew_alt_az(85, 200),
            ] + [_sky_flat(f, n, _cfg.default_gain, _cfg.default_offset)
                 for f in flat_filters]
            + [_pushover("Flats", "sky flats complete")],
            # LoopCondition(1): a container under a SafetyMonitorCondition
            # REPEATS while safe, so without it the flat set would reshoot
            # until the armer's dawn shutdown stopped NINA (latent: the
            # shutdown always fired before the flat window until PS-36;
            # caught by the PS-77 sequence simulator).
            conditions=[_safety_condition(), _loop_once()])
        end_items.append(flat_block)
    end_items.append(_pushover("Shutdown", "starting shutdown: stop guiding, "
                               "park, dew heater off, warm camera, disconnect"))
    end_items.append(_stop_guiding())
    if sequence.park_on_finish:
        end_items.append(_park())
    # Imaging done: turn the dew heater OFF explicitly (warm handles the cooler).
    end_items.append(_dew_heater(False))
    if sequence.warm_camera_on_finish:
        end_items.append(_warm_camera(
            float(getattr(_cfg, "gradual_warm_minutes", 0.0))))
    end_items.append(_disconnect_all())
    end_items.append(_pushover("Shutdown", "shutdown complete — parked, warm, "
                               "cooler + dew heater off, guider stopped"))

    root = _seq_container(
        sequence.name,
        [
            _seq_container("Start", [
                _seq_container("AARO startup", start_items),
            ], container_type="NINA.Sequencer.Container.StartAreaContainer, "
                              "NINA.Sequencer"),
            _seq_container("Targets", [night_loop],
                           container_type="NINA.Sequencer.Container."
                                          "TargetAreaContainer, NINA.Sequencer"),
            _seq_container("End", [
                _seq_container("AARO shutdown", end_items),
            ], container_type="NINA.Sequencer.Container.EndAreaContainer, "
                              "NINA.Sequencer"),
        ],
        container_type="NINA.Sequencer.Container.SequenceRootContainer, "
                       "NINA.Sequencer",
    )
    return json.dumps(link_parents(root), indent=2)
