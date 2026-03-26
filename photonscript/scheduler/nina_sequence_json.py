"""Generate NINA Advanced Sequencer JSON files.

NINA's Advanced Sequencer uses a JSON format with .NET $type annotations
for serialization. This module generates files that NINA can load directly
via File -> Open in the Advanced Sequencer.

Reference: https://github.com/adamfenn28/nina-sequences
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


def _build_start_guiding() -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Guider.StartGuiding, NINA.Sequencer",
        Inherited=True,
        ForceCalibration=False,
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


def _build_dither_trigger(every_n: int = 3) -> dict:
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


def _build_cool_camera(temp_c: float = -10.0, duration_minutes: int = 10) -> dict:
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Camera.CoolCamera, NINA.Sequencer",
        Temperature=temp_c,
        Duration=duration_minutes * 60,
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


def _build_target_container(target: NinaSequenceTarget, min_altitude: float) -> dict:
    """Build a DeepSkyObjectContainer for one target."""
    coords = {**_decompose_ra(target.ra_hours), **_decompose_dec(target.dec_degrees)}

    # Build instruction list
    instructions = []

    # Slew & center
    if target.slew_and_center:
        instructions.append(_build_slew_and_center(target))

    # Start guiding
    if target.start_guiding:
        instructions.append(_build_start_guiding())

    # Autofocus on start
    if target.auto_focus_on_start:
        instructions.append(_build_run_autofocus())

    # Exposure sets (with filter switches)
    for exp in target.exposures:
        remaining = exp.count - exp.acquired
        if remaining <= 0:
            continue
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
    if target.dither_every_n > 0:
        triggers.append(_build_dither_trigger(target.dither_every_n))

    # Build conditions
    conditions = [_build_altitude_condition(min_altitude)]

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
        start_items.append(_build_cool_camera(sequence.targets[0].camera_temp_c))

    # Target containers
    target_containers = [
        _build_target_container(t, sequence.wait_for_altitude)
        for t in sequence.targets
    ]

    # End area items
    end_items = []
    if sequence.warm_camera_on_finish:
        end_items.append(_build_warm_camera())
    if sequence.park_on_finish:
        end_items.append(_build_park_scope())

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
