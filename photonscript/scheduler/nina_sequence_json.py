"""Generate NINA Advanced Sequencer JSON files.

NINA's Advanced Sequencer uses a JSON format with .NET $type annotations
for serialization. This module generates files that NINA can load directly
via File -> Open in the Advanced Sequencer.

Encodes the AARO acquisition order: tracking -> slew & center -> first
capture filter -> autofocus -> plate solve & center -> [guiding] -> expose.
Safety monitor condition on every target; unguided (CEM70G encoders) is
the default mode.
"""

from __future__ import annotations

import json
from typing import Optional

from photonscript.shared.models import (
    NinaSequenceFile, NinaSequenceTarget, ExposurePlan, FilterType,
)
from photonscript.scheduler.nina_sequence import FILTER_POSITIONS


def _decompose_ra(ra_hours: float) -> dict:
    """Decompose RA decimal hours into H/M/S components."""
    h = int(ra_hours)
    remainder = (ra_hours - h) * 60
    m = int(remainder)
    s = (remainder - m) * 60
    return {"RAHours": h, "RAMinutes": m, "RASeconds": round(s, 2)}


def _decompose_dec(dec_degrees: float) -> dict:
    """Decompose Dec decimal degrees into D/M/S components."""
    sign = 1 if dec_degrees >= 0 else -1
    d_abs = abs(dec_degrees)
    d = int(d_abs)
    remainder = (d_abs - d) * 60
    m = int(remainder)
    s = (remainder - m) * 60
    return {
        "DecDegrees": sign * d,
        "DecMinutes": m,
        "DecSeconds": round(s, 2),
        "NegativeDec": dec_degrees < 0,
    }


def _make_typed(type_name: str, **kwargs) -> dict:
    """Create a NINA $type-annotated object."""
    obj = {"$type": type_name}
    obj.update(kwargs)
    return obj


def _build_slew_and_center(target: NinaSequenceTarget) -> dict:
    """Build a SlewScopeAndCenter instruction."""
    coords = {**_decompose_ra(target.ra_hours), **_decompose_dec(target.dec_degrees)}
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Telescope.SlewScopeAndCenter, NINA.Sequencer",
        Inherited=True,
        Coordinates=_make_typed(
            "NINA.Astrometry.InputCoordinates, NINA.Astrometry",
            **coords,
        ),
    )


def _build_start_guiding(force_calibration: bool = False) -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Guider.StartGuiding, NINA.Sequencer",
        Inherited=True,
        ForceCalibration=force_calibration,
    )


def _build_stop_guiding() -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Guider.StopGuiding, NINA.Sequencer",
        Inherited=True,
    )


def _build_set_tracking(mode: int) -> dict:
    """Tracking mode: 0 = sidereal (imaging), 5 = stopped (park/shutdown)."""
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Telescope.SetTracking, NINA.Sequencer",
        TrackingMode=mode,
    )


def _build_center(target: NinaSequenceTarget) -> dict:
    """Plate Solve & Center — re-center after autofocus, before imaging."""
    coords = {**_decompose_ra(target.ra_hours), **_decompose_dec(target.dec_degrees)}
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Platesolving.Center, NINA.Sequencer",
        Inherited=True,
        Coordinates=_make_typed(
            "NINA.Astrometry.InputCoordinates, NINA.Astrometry",
            **coords,
        ),
    )


def _build_safety_condition() -> dict:
    """Abort imaging when the observatory safety monitor reports Unsafe."""
    return _make_typed(
        "NINA.Sequencer.Conditions.SafetyMonitorCondition, NINA.Sequencer",
    )


def _build_run_autofocus() -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Autofocus.RunAutofocus, NINA.Sequencer",
        Inherited=True,
    )


def _build_take_exposures(exp: ExposurePlan) -> dict:
    """Build a TakeSubframeExposure or TakeManyExposures block."""
    remaining = exp.count - exp.acquired
    if remaining <= 0:
        return None

    return _make_typed(
        "NINA.Sequencer.SequenceItem.Imaging.TakeManyExposures, NINA.Sequencer",
        Inherited=True,
        ExposureTime=exp.exposure_seconds,
        ExposureCount=remaining,
        Gain=exp.gain,
        Offset=exp.offset,
        Binning=_make_typed(
            "NINA.Equipment.Equipment.BinningMode, NINA.Equipment",
            X=exp.binning,
            Y=exp.binning,
        ),
        ImageType="LIGHT",
        FilterName=exp.filter_type.value,
    )


def _build_switch_filter(filter_type: FilterType) -> dict:
    position = FILTER_POSITIONS.get(filter_type, 0)
    return _make_typed(
        "NINA.Sequencer.SequenceItem.FilterWheel.SwitchFilter, NINA.Sequencer",
        Inherited=True,
        Filter=_make_typed(
            "NINA.Equipment.Filter.FilterInfo, NINA.Equipment",
            Name=filter_type.value,
            Position=position,
        ),
    )


def _build_dither_trigger(every_n: int = 5) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Guider.DitherAfterExposures, NINA.Sequencer",
        AfterExposures=every_n,
    )


def _build_autofocus_trigger(interval_minutes: int = 60) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus.AutofocusAfterTimeTrigger, NINA.Sequencer",
        Amount=interval_minutes,
    )


def _build_meridian_flip_trigger() -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.MeridianFlip.MeridianFlipTrigger, NINA.Sequencer",
        Inherited=True,
    )


def _build_altitude_condition(min_alt: float) -> dict:
    return _make_typed(
        "NINA.Sequencer.Conditions.AltitudeCondition, NINA.Sequencer",
        MinimumAltitude=min_alt,
    )


def _build_cool_camera(temp_c: float = -10.0, duration_minutes: int = 2) -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Camera.CoolCamera, NINA.Sequencer",
        Temperature=temp_c,
        Duration=duration_minutes * 60,
    )


def _build_wait_for_time(hhmmss: str) -> dict:
    """Hold the sequence until a local clock time (e.g. astro dusk).

    Lets the sequence be dispatched and started in the afternoon: equipment
    connects and cools immediately, imaging waits for dark.
    """
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Utility.WaitForTime, NINA.Sequencer",
        Hours=h, Minutes=m, Seconds=s,
    )


def _build_warm_camera() -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Camera.WarmCamera, NINA.Sequencer",
        Duration=600,
    )


def _build_park_scope() -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Telescope.ParkScope, NINA.Sequencer",
    )


def _build_unpark_scope() -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Telescope.UnparkScope, NINA.Sequencer",
    )


def _build_target_container(
    target: NinaSequenceTarget,
    min_altitude: float,
    force_calibration: bool = False,
) -> dict:
    """Build a DeepSkyObjectContainer for one target.

    AARO acquisition order (encoded, do not reorder):
      set tracking -> slew & center -> switch to first capture filter ->
      autofocus -> plate solve & center -> defensive tracking re-set ->
      [start guiding if guided] -> exposures
    """
    coords = {**_decompose_ra(target.ra_hours), **_decompose_dec(target.dec_degrees)}

    active_exposures = [e for e in target.exposures if e.count - e.acquired > 0]

    # Build instruction list
    instructions = [_build_set_tracking(0)]

    # Slew & center
    if target.slew_and_center:
        instructions.append(_build_slew_and_center(target))

    # Switch to the FIRST capture filter BEFORE autofocus — AF must run
    # through the filter it will shoot through, not whatever was left in the wheel.
    if active_exposures:
        instructions.append(_build_switch_filter(active_exposures[0].filter_type))

    # Autofocus on start
    if target.auto_focus_on_start:
        instructions.append(_build_run_autofocus())

    # Plate solve & center after AF, before imaging — never skip
    instructions.append(_build_center(target))

    # Defensive tracking re-set just before guiding/imaging
    instructions.append(_build_set_tracking(0))

    # Start guiding (guided runs only; unguided is the AARO default —
    # the CEM70G absolute encoders carry the load)
    if target.start_guiding:
        instructions.append(_build_start_guiding(force_calibration))

    # Exposure sets (with filter switches)
    for exp in active_exposures:
        instructions.append(_build_switch_filter(exp.filter_type))
        exposure_item = _build_take_exposures(exp)
        if exposure_item:
            instructions.append(exposure_item)

    # Build triggers
    triggers = []
    if target.meridian_flip:
        triggers.append(_build_meridian_flip_trigger())
    if target.auto_focus_interval_minutes > 0:
        triggers.append(_build_autofocus_trigger(target.auto_focus_interval_minutes))
    if target.start_guiding and target.dither_every_n > 0:
        triggers.append(_build_dither_trigger(target.dither_every_n))

    # Build conditions — safety monitor on EVERY target (weather abort)
    conditions = [_build_safety_condition(), _build_altitude_condition(min_altitude)]

    return _make_typed(
        "NINA.Sequencer.Container.DeepSkyObjectContainer, NINA.Sequencer",
        Strategy=_make_typed(
            "NINA.Sequencer.Container.ExecutionStrategy.SequentialStrategy, NINA.Sequencer",
        ),
        Target=_make_typed(
            "NINA.Astrometry.InputTarget, NINA.Astrometry",
            TargetName=target.name,
            InputCoordinates=_make_typed(
                "NINA.Astrometry.InputCoordinates, NINA.Astrometry",
                **coords,
            ),
            PositionAngle=target.rotation,
        ),
        Items={
            "$type": "System.Collections.ObjectModel.ObservableCollection`1"
                     "[[NINA.Sequencer.ISequenceItem, NINA.Sequencer]], System",
            "$values": instructions,
        },
        Triggers={
            "$type": "System.Collections.ObjectModel.ObservableCollection`1"
                     "[[NINA.Sequencer.ISequenceTrigger, NINA.Sequencer]], System",
            "$values": triggers,
        },
        Conditions={
            "$type": "System.Collections.ObjectModel.ObservableCollection`1"
                     "[[NINA.Sequencer.ISequenceCondition, NINA.Sequencer]], System",
            "$values": conditions,
        },
    )


def generate_nina_json(sequence: NinaSequenceFile) -> str:
    """Generate a NINA Advanced Sequencer compatible JSON file.

    This produces the .json format used by NINA's Advanced Sequencer,
    complete with $type annotations matching NINA's .NET serialization.
    """
    # Start area items
    start_items = [_build_unpark_scope()]
    if sequence.targets and sequence.targets[0].cool_camera:
        # 2-minute cool to -10.0°C — verify Temperature field is NEVER 0
        # (a misread 0 setpoint once cost a whole night of warm-sensor subs)
        start_items.append(_build_cool_camera(sequence.targets[0].camera_temp_c, 2))

    # Dusk gate: cool during twilight, image when dark
    if sequence.wait_until_local:
        start_items.append(_build_wait_for_time(sequence.wait_until_local))

    # Target containers — force guider recalibration on the FIRST guided target
    # (stale PHD2 calibration is a top cause of runaway guide errors)
    target_containers = []
    first_guided = True
    for t in sequence.targets:
        force_cal = first_guided and t.start_guiding
        if t.start_guiding:
            first_guided = False
        target_containers.append(
            _build_target_container(t, sequence.wait_for_altitude, force_cal)
        )

    # End area items: stop guiding (if any), warm, park, stop tracking
    end_items = []
    if any(t.start_guiding for t in sequence.targets):
        end_items.append(_build_stop_guiding())
    if sequence.warm_camera_on_finish:
        end_items.append(_build_warm_camera())
    if sequence.park_on_finish:
        end_items.append(_build_park_scope())
    end_items.append(_build_set_tracking(5))

    root = _make_typed(
        "NINA.Sequencer.Container.SequenceRootContainer, NINA.Sequencer",
        Strategy=_make_typed(
            "NINA.Sequencer.Container.ExecutionStrategy.SequentialStrategy, NINA.Sequencer",
        ),
        Name=sequence.name,
        Items={
            "$type": "System.Collections.ObjectModel.ObservableCollection`1"
                     "[[NINA.Sequencer.ISequenceItem, NINA.Sequencer]], System",
            "$values": [
                # Start area
                _make_typed(
                    "NINA.Sequencer.Container.StartAreaContainer, NINA.Sequencer",
                    Strategy=_make_typed(
                        "NINA.Sequencer.Container.ExecutionStrategy.SequentialStrategy, NINA.Sequencer",
                    ),
                    Items={
                        "$type": "System.Collections.ObjectModel.ObservableCollection`1"
                                 "[[NINA.Sequencer.ISequenceItem, NINA.Sequencer]], System",
                        "$values": start_items,
                    },
                ),
                # Target area
                _make_typed(
                    "NINA.Sequencer.Container.TargetAreaContainer, NINA.Sequencer",
                    Strategy=_make_typed(
                        "NINA.Sequencer.Container.ExecutionStrategy.SequentialStrategy, NINA.Sequencer",
                    ),
                    Items={
                        "$type": "System.Collections.ObjectModel.ObservableCollection`1"
                                 "[[NINA.Sequencer.ISequenceItem, NINA.Sequencer]], System",
                        "$values": target_containers,
                    },
                ),
                # End area
                _make_typed(
                    "NINA.Sequencer.Container.EndAreaContainer, NINA.Sequencer",
                    Strategy=_make_typed(
                        "NINA.Sequencer.Container.ExecutionStrategy.SequentialStrategy, NINA.Sequencer",
                    ),
                    Items={
                        "$type": "System.Collections.ObjectModel.ObservableCollection`1"
                                 "[[NINA.Sequencer.ISequenceItem, NINA.Sequencer]], System",
                        "$values": end_items,
                    },
                ),
            ],
        },
    )

    return json.dumps(root, indent=2)
