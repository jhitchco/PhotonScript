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


# --- Instructions -----------------------------------------------------------

def _pushover(title: str, message: str, sound: int = 1) -> dict:
    """GroundStation Pushover — Jeremy's remote narration channel."""
    return _make_typed(
        "DaleGhent.NINA.GroundStation.SendToPushover.SendToPushover, "
        "DaleGhent.NINA.GroundStation",
        Title=title, Message=message, Priority=0, NotificationSound=sound,
        ErrorBehavior=0, Attempts=1)


def _connect(device: str) -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Connect.ConnectEquipment, "
                       "NINA.Sequencer", SelectedDevice=device,
                       ErrorBehavior=0, Attempts=1)


def _dew_heater(on: bool = True) -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Camera.DewHeater, "
                       "NINA.Sequencer", OnOff=on, ErrorBehavior=0, Attempts=1)


def _cool_camera(temp_c: float, duration_min: float = 2.0) -> dict:
    # Duration is MINUTES (reference file uses 2.0)
    return _make_typed("NINA.Sequencer.SequenceItem.Camera.CoolCamera, "
                       "NINA.Sequencer", Temperature=temp_c,
                       Duration=duration_min, ErrorBehavior=0, Attempts=1)


def _warm_camera(duration_min: float = 3.0) -> dict:
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
    return _make_typed("NINA.Sequencer.SequenceItem.Autofocus.RunAutofocus, "
                       "NINA.Sequencer", ErrorBehavior=0, Attempts=1)


def _start_guiding(force_calibration: bool = False) -> dict:
    return _make_typed("NINA.Sequencer.SequenceItem.Guider.StartGuiding, "
                       "NINA.Sequencer", ForceCalibration=force_calibration,
                       ErrorBehavior=0, Attempts=1)


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


def _filter_info(filter_type: FilterType) -> dict:
    """NINA.Core FilterInfo shape (underscore fields), per the reference file."""
    return _make_typed(
        "NINA.Core.Model.Equipment.FilterInfo, NINA.Core",
        _name=_nina_filter_name(filter_type),
        _focusOffset=0,
        _position=FILTER_POSITIONS.get(filter_type, 0),
        _autoFocusExposureTime=-1.0,
        _autoFocusFilter=False,
        _autoFocusBinning=_make_typed(
            "NINA.Core.Model.Equipment.BinningMode, NINA.Core", X=1, Y=1),
        _autoFocusGain=-1,
        _autoFocusOffset=-1)


def _switch_filter(filter_type: FilterType) -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.FilterWheel.SwitchFilter, NINA.Sequencer",
        Filter=_filter_info(filter_type), ErrorBehavior=0, Attempts=1)


def _dither_trigger(after_exposures: int) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Guider.DitherAfterExposures, NINA.Sequencer",
        AfterExposures=after_exposures,
        TriggerRunner=_trigger_runner([_make_typed(
            "NINA.Sequencer.SequenceItem.Guider.Dither, NINA.Sequencer",
            ErrorBehavior=0, Attempts=1)]))


def _smart_exposure(exp: ExposurePlan, guided: bool,
                    dither_every_n: int) -> dict:
    """SmartExposure: LoopCondition(count) wrapping SwitchFilter+TakeExposure."""
    remaining = exp.count - exp.acquired
    triggers = []
    if guided and dither_every_n > 0:
        triggers.append(_dither_trigger(dither_every_n))
    smart = _seq_container(
        "Smart Exposure",
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
            CompletedIterations=0, Iterations=remaining)],
        triggers=triggers,
        container_type="NINA.Sequencer.SequenceItem.Imaging.SmartExposure, "
                       "NINA.Sequencer",
    )
    smart["IsExpanded"] = False
    return smart


def _autofocus_time_trigger(interval_minutes: int) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus.AutofocusAfterTimeTrigger, NINA.Sequencer",
        Amount=interval_minutes,
        TriggerRunner=_trigger_runner([_autofocus()]))


def _autofocus_temp_trigger(amount_c: float = 1.0) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus."
        "AutofocusAfterTemperatureChangeTrigger, NINA.Sequencer",
        Amount=amount_c, TriggerRunner=_trigger_runner([_autofocus()]))


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


# --- Containers ---------------------------------------------------------------

def _build_target_container(target: NinaSequenceTarget, min_altitude: float,
                            force_calibration: bool = False) -> dict:
    """AARO acquisition order: tracking -> slew -> first filter -> AF ->
    plate solve center -> tracking (defensive) -> [guiding] -> exposures."""
    active = [e for e in target.exposures if e.count - e.acquired > 0]

    items = [
        _pushover("Imaging", f"{target.name}: slewing"),
        _set_tracking(0),
        _slew(target),
    ]
    if active:
        items.append(_switch_filter(active[0].filter_type))
    if target.auto_focus_on_start:
        items.append(_autofocus())
    items.append(_center(target))
    items.append(_set_tracking(0))
    if target.start_guiding:
        items.append(_start_guiding(force_calibration))
        items.append(_pushover("Imaging", f"{target.name}: guiding, capturing"))
    else:
        items.append(_pushover("Imaging",
                               f"{target.name}: encoders engaged, capturing"))
    for exp in active:
        items.append(_smart_exposure(exp, target.start_guiding,
                                     target.dither_every_n))
    items.append(_pushover("Imaging", f"{target.name}: block complete"))

    triggers = [_meridian_flip_trigger(), _reconnect_trigger()]
    if target.auto_focus_interval_minutes > 0:
        triggers.append(_autofocus_time_trigger(target.auto_focus_interval_minutes))
    triggers.append(_autofocus_temp_trigger(1.0))

    container = _seq_container(
        target.name, items,
        conditions=[_safety_condition(),
                    _altitude_condition(target, min_altitude)],
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


def generate_nina_json(sequence: NinaSequenceFile) -> str:
    """Generate an Advanced Sequencer JSON matching the proven AARO schema."""
    guided = any(t.start_guiding for t in sequence.targets)
    temp = (sequence.targets[0].camera_temp_c if sequence.targets else 0.0)

    # Start area: full cold-start — connect everything, cool during twilight,
    # hold at the dusk gate (NINA's own DuskProvider, recomputed nightly)
    start_items = [
        _pushover("Startup", "AARO equipment standby — entered the loop", 22),
        _connect("Camera"),
        _dew_heater(True),
        _cool_camera(temp, 2.0),
        _connect("Filter Wheel"),
        _connect("Focuser"),
        _connect("Mount"),
        _unpark(),
        _set_tracking(5),
        _connect("Safety Monitor"),
        _connect("Guider"),
        _connect("Weather"),
        _pushover("Startup", "AARO equipment standby done", 22),
    ]
    if sequence.wait_until_local is not None:
        start_items.append(_wait_for_dusk(0))
        start_items.append(_pushover("Startup", "astro dusk — imaging", 22))

    target_containers = []
    first_guided = True
    for t in sequence.targets:
        force_cal = first_guided and t.start_guiding
        if t.start_guiding:
            first_guided = False
        target_containers.append(
            _build_target_container(t, sequence.wait_for_altitude, force_cal))

    end_items = [_pushover("Shutdown", "starting shutdown")]
    if guided:
        end_items.append(_stop_guiding())
    if sequence.park_on_finish:
        end_items.append(_park())
    if sequence.warm_camera_on_finish:
        end_items.append(_warm_camera(3.0))
    end_items.append(_pushover("Shutdown", "shutdown complete — parked & warm"))

    root = _seq_container(
        sequence.name,
        [
            _seq_container("Start", [
                _seq_container("AARO startup", start_items),
                *target_containers,
            ], container_type="NINA.Sequencer.Container.StartAreaContainer, "
                              "NINA.Sequencer"),
            _seq_container("Targets", [],
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
    root["Parent"] = None
    return json.dumps(root, indent=2)
