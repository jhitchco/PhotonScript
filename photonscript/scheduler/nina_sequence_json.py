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
TARGET_FOCUS_CAL_SUFFIX = " focus calibration AFs"
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
FILTER_UNTIL_MOONRISE_SUFFIX = " until moonrise"  # "<filter> until moonrise"
# PS-61: with the cooler gate on, each light block is its own container
# "<target> filter block (cooler-gated)" whose first item is the gate, so a
# gate SKIP interrupts just that block.
TARGET_BLOCK_SUFFIX = " filter block (cooler-gated)"
SAFE_LOOP_NAME = "SAFE_LOOP"
RESET_EQUIPMENT_NAME = "RESET_EQUIPMENT_ONCE_SAFE"
TARGETS_LOOP_NAME = "TARGETS_CONTAINER"
NIGHT_LOOP_NAME = "LOOP_ALL_NIGHT"
UNSAFE_BRANCH_NAME = "UNSAFE"
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
        import httpx
        base = str(getattr(config, "nina_base_url", "")).rstrip("/")
        if base:
            r = httpx.get(base + "/equipment/focuser", timeout=4)
            d = r.json()
            payload = d.get("Response", d) if isinstance(d, dict) else {}
            t = payload.get("Temperature")
            v = float(t) if t is not None else None
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


def _block_af_runner(af_filter: FilterType, offset: int) -> list:
    """PS-77: the refocus recipe a mid-block AF trigger runs. Same order as
    the block start (PS-65): AF filter, autofocus, measured filter offset.
    The SmartExposure's own SwitchFilter puts the imaging filter back on the
    next iteration, so a triggered AF never focuses through 3 nm."""
    out = [_switch_filter(af_filter), _autofocus()]
    if offset:
        out.append(_move_focuser_relative(offset))
    return out


def _autofocus_time_trigger(minutes: float = 60.0) -> dict:
    """Periodic refocus: AF once `minutes` have passed since the last AF.
    Used on the Piggy-600 (PS-68), where NINA #2 cannot see the RC16's
    meridian flip and the HFR trigger baselines on the last AF (a bad AF
    never re-triggers it), so a timed AF is the in-sequence repair."""
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus.AutofocusAfterTimeTrigger, "
        "NINA.Sequencer",
        Amount=float(minutes), TriggerRunner=_trigger_runner([_autofocus()]))


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
        from photonscript.scheduler.calibration import dark_quota, quota_exposures
        for exp_s in quota_exposures(cfg, "rc16"):
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


def _slew_alt_az(alt_deg: int = 70, az_deg: int = 180) -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Telescope.SlewScopeToAltAz, NINA.Sequencer",
        Coordinates=_make_typed(
            "NINA.Astrometry.InputTopocentricCoordinates, NINA.Astrometry",
            AzDegrees=az_deg, AzMinutes=0, AzSeconds=0,
            AltDegrees=alt_deg, AltMinutes=0, AltSeconds=0),
        ErrorBehavior=0, Attempts=1)


# --- Containers ---------------------------------------------------------------

def _build_target_container(target: NinaSequenceTarget, min_altitude: float,
                            force_calibration: bool = False,
                            af_filter: "FilterType | None" = None,
                            narrate: str = "normal",
                            focus_offsets: dict | None = None,
                            loop_end: tuple | None = None,
                            selftest_script: str | None = None,
                            unguided_dither: bool = False,
                            cooler_gate: tuple | None = None) -> dict:
    """AARO acquisition order: tracking -> slew -> first filter -> AF ->
    plate solve center -> tracking (defensive) -> [self-test] -> [guiding]
    -> exposures.

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
    before."""
    if getattr(target, "focus_calibration", False):
        return _build_focus_calibration_container(target, min_altitude,
                                                  af_filter, focus_offsets)
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

    plan_desc = ", ".join(f"{e.filter_type.value}×{e.count - e.acquired}"
                          f"@{e.exposure_seconds:.0f}s" for e in active)
    total_h = sum(e.exposure_seconds * max(0, e.count - e.acquired)
                  + (e.hdr_short_seconds or 0) * e.short_remaining()
                  for e in active) / 3600
    items = [
        _pushover("Imaging", f"{target.name}: slewing "
                  f"(RA {target.ra_hours:.2f}h Dec {target.dec_degrees:+.1f}°) "
                  f"— plan {plan_desc} (~{total_h:.1f}h)"),
        _set_tracking(0),
        _slew(target),
    ]
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
    items.append(_center(target))
    items.append(_set_tracking(0))
    if target.start_guiding:
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

    def _block(exp, bi, n_blocks, condition=None):
        n = exp.count - exp.acquired
        block_h = exp.exposure_seconds * n / 3600
        # PS-65: every block focuses for itself. AF on the AF filter (L), or on
        # the block's own filter when no AF filter is configured, then move by
        # the measured filter offset. The old per-block MoveFocuserAbsolute to a
        # July seed with no AF after it put a whole night's Ha ~300 steps out
        # (2026-09-26), and on every loop pass undid the triggered AFs.
        block_af = af_filter or exp.filter_type
        offset = _focus_offset(block_af, exp.filter_type, focus_offsets)
        out = []
        if chatty_block:   # per-block "starting/done" pair — the bulk of the noise
            out.append(_pushover(
                "Imaging",
                f"{target.name} [{bi}/{n_blocks}]: starting "
                f"{exp.filter_type.value} — {n}×{exp.exposure_seconds:.0f}s "
                f"(~{block_h:.1f}h) gain {exp.gain}; autofocus on "
                f"{block_af.value}"
                + (f" then offset {offset:+d}" if offset else "")
                + (" (moon-free window)" if condition else "")))
        out.append(_switch_filter(block_af))
        out.append(_autofocus())
        if offset:
            out.append(_move_focuser_relative(offset))
        # HDR: emit a SHORT companion SmartExposure alongside the long one, same
        # filter/gain/offset/binning, its own (shorter) length + count. Short
        # first (bright cores), then the long set. Dither + AF triggers are
        # container-level, so both blocks share them; each SmartExposure carries
        # its own dither trigger via _smart_exposure (guided/dither_every_n).
        # Only what is still owed: the short set stops once hdr_short_acquired
        # reaches its count (it used to re-shoot all of it every night), and a
        # finished long set is not re-emitted just because shorts remain.
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
            runner = lambda: _block_af_runner(block_af, offset)  # noqa: E731
            return [_autofocus_temp_trigger(2.0, runner()),
                    _autofocus_hfr_trigger(10.0, 4, runner())]

        short_n = exp.short_remaining()
        if short_n > 0:
            short_exp = exp.model_copy(update={
                "exposure_seconds": exp.hdr_short_seconds,
                "count": short_n, "acquired": 0,
                "hdr_short_seconds": None, "hdr_short_count": 0,
                "hdr_short_acquired": 0})
            out.append(_smart_exposure(short_exp, target.start_guiding,
                                       target.dither_every_n,
                                       guard_conditions=_guards(),
                                       extra_triggers=_af_triggers(),
                                       unguided_dither=unguided_dither))
        if exp.count - exp.acquired > 0:
            out.append(_smart_exposure(exp, target.start_guiding,
                                       target.dither_every_n,
                                       guard_conditions=_guards(),
                                       extra_triggers=_af_triggers(),
                                       unguided_dither=unguided_dither))
        if chatty_block:
            out.append(_pushover(
                "Imaging",
                f"{target.name} [{bi}/{n_blocks}]: {exp.filter_type.value} block "
                f"done ({n}×{exp.exposure_seconds:.0f}s attempted)"))
        if cooler_gate:
            # PS-61: gate first (before the AF: no point focusing for a block
            # that will not shoot), in a container of its own so a SKIP
            # interrupts this block only.
            gate = _cooler_gate(cooler_gate, "rc16",
                                f"{target.name} {exp.filter_type.value}")
            return [_seq_container(f"{target.name}{TARGET_BLOCK_SUFFIX}",
                                   [gate] + out)]
        return out

    n_blocks = len(ordered)
    imaging = []
    for bi, exp in enumerate(ordered, 1):
        is_bb = exp.filter_type.value not in NB_SET
        blk = _block(exp, bi, n_blocks, bb_condition if is_bb else None)
        if is_bb and bb_condition is not None:
            imaging.append(_seq_container(
                f"{exp.filter_type.value}{FILTER_UNTIL_MOONRISE_SUFFIX}", blk,
                conditions=[bb_condition]))
        else:
            imaging.extend(blk)
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
    items.append(_seq_container(
        f"{target.name}{TARGET_IMAGING_SUFFIX}", imaging,
        conditions=inner_conds))
    items.append(_pushover("Imaging", f"{target.name}: leaving target "
                           f"({plan_desc}) — below altitude or unsafe"))

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
        target.name, items,
        conditions=[_safety_condition(),
                    _altitude_condition(target, min_altitude),
                    _loop_once()],
        triggers=triggers,
        container_type="NINA.Sequencer.Container.DeepSkyObjectContainer, "
                       "NINA.Sequencer",
        Target=_make_typed(
            "NINA.Astrometry.InputTarget, NINA.Astrometry",
            Expanded=True, TargetName=target.name,
            PositionAngle=target.rotation,
            InputCoordinates=_coords(target)),
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
                                       focus_offsets: dict | None) -> dict:
    """PS-76 focus-offset calibration: slew to a rich star field, AF on the
    reference filter (L), center, then run RunAutofocus in every filter of the
    bracketed order. Before each AF the focuser is moved by the configured
    offset delta from the previous filter, so every AF starts near its best
    focus. No lights are taken; the product is NINA's AF reports, which the
    backfill ingests into focus_model.

    NINA's profile "Autofocus filter" must be OFF for this run, or every
    RunAutofocus switches to that filter and only L gets measured."""
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
                                conditions=[_safety_condition()]))
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
    seq = build_sequence_for_night(f"Focus calibration {name}", [t])
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
    temp = float(target.camera_temp_c)
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
        _cool_camera(temp, 0.0),
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
                    2.0, _block_af_runner(ref, off))]))
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
    temp = (sequence.targets[0].camera_temp_c if sequence.targets else 0.0)
    gate_dark = sequence.wait_until_local is not None

    # Filter-aware imaging gate: narrowband rejects twilight glow, so an
    # Ha/SII-first night can start ~35 min earlier (sun ~-13/-14 deg) and,
    # if ALL targets are narrowband, run ~35 min later into morning twilight.
    NB = ("Ha", "SII", "OIII")
    first_exposures = [e for t in sequence.targets for e in t.exposures
                       if e.count - e.acquired > 0]
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
        _bias_age = days_since_last_bias(_gen_cfg())
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
            f"BIAS_IF_STILL_UNSAFE skipped: library bias is {_bias_age}d old "
            f"(refresh every {_bias_refresh_days}d)")
    start_items += [
        _connect("Safety Monitor"),
        _connect("Camera"),
        _dew_heater(True),
        _cool_camera(temp, float(getattr(_cfg, "cool_ramp_minutes", 0.0))),
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
                                    cooler_gate=gate)
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
        _wait_for_timespan(_safe_confirm_s),
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
        _wait_for_provider("DawnProvider", 0),
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
            for _name in stale_flat_filters(_cfg):
                _ft = _by_val.get(_name)
                if _ft is not None and _ft not in flat_filters:
                    flat_filters.append(_ft)
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
