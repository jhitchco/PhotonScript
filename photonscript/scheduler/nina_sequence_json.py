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
                           sample_size: int = 4) -> dict:
    return _make_typed(
        "NINA.Sequencer.Trigger.Autofocus.AutofocusAfterHFRIncreaseTrigger, "
        "NINA.Sequencer",
        Amount=amount_pct, SampleSize=sample_size,
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


def _dark_quota_blocks(dawn_provider, dawn_offset):
    """Dark blocks for unsafe time, capped by the library quota: for each
    exposure the current lights use, take only (quota - already on disk),
    600s first then 180s. Lowest-priority work: any of LoopWhileUnsafe
    exit, dawn, or the cap ends the block."""
    cfg = _gen_cfg()
    quota = int(getattr(cfg, "dark_target_count", 30))
    blocks = []
    try:
        from photonscript.scheduler.calibration import count_matching_darks
        wanted = []
        for tok in str(getattr(cfg, "dark_exposures", "600,180")).split(","):
            try:
                wanted.append(float(tok.strip()))
            except ValueError:
                continue
        for exp_s in wanted:
            have = count_matching_darks(cfg, exp_s)
            need = max(0, quota - have)
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
                            force_calibration: bool = False) -> dict:
    """AARO acquisition order: tracking -> slew -> first filter -> AF ->
    plate solve center -> tracking (defensive) -> [guiding] -> exposures."""
    active = [e for e in target.exposures if e.count - e.acquired > 0]

    plan_desc = ", ".join(f"{e.filter_type.value}×{e.count - e.acquired}"
                          f"@{e.exposure_seconds:.0f}s" for e in active)
    total_h = sum(e.exposure_seconds * (e.count - e.acquired)
                  for e in active) / 3600
    items = [
        _pushover("Imaging", f"{target.name}: slewing "
                  f"(RA {target.ra_hours:.2f}h Dec {target.dec_degrees:+.1f}°) "
                  f"— plan {plan_desc} (~{total_h:.1f}h)"),
        _set_tracking(0),
        _slew(target),
    ]
    if active:
        items.append(_switch_filter(active[0].filter_type))
    if target.auto_focus_on_start:
        items.append(_pushover("Imaging",
                               f"{target.name}: slew done — autofocusing "
                               f"through {active[0].filter_type.value if active else 'L'}, "
                               "then plate solve & center"))
        items.append(_autofocus())
    items.append(_center(target))
    items.append(_set_tracking(0))
    if target.start_guiding:
        items.append(_start_guiding(force_calibration))
        items.append(_pushover("Imaging",
                               f"{target.name}: focused, centered, guiding — "
                               "capturing"))
    else:
        items.append(_pushover("Imaging",
                               f"{target.name}: focused, centered, unguided "
                               "on encoders — capturing"))
    NB_SET = {"Ha", "OIII", "SII"}
    bb = [e for e in active if e.filter_type.value not in NB_SET]
    nb = [e for e in active if e.filter_type.value in NB_SET]
    ordered = active
    bb_condition = None
    if bb:
        mw = _moon_window()
        if mw.get("available") and mw.get("down_at_dusk"):
            # dark evening: broadband first, capped at moonrise
            ordered = bb + nb
            if mw.get("rise_local_hh") is not None:
                bb_condition = _time_condition_at(mw["rise_local_hh"],
                                                  mw["rise_local_mm"])
        elif mw.get("available") and (mw.get("illum_pct") or 100) < 20:
            ordered = bb + nb  # faint moon: broadband fine any time
        else:
            # moon up at dusk and bright: defer broadband tonight
            items.append(_pushover(
                "Imaging",
                f"{target.name}: moon up at dusk "
                f"({mw.get('illum_pct', '?')}%) — RGB/L deferred to a "
                "dark evening; narrowband only tonight"))
            ordered = nb

    def _block(exp, bi, n_blocks, condition=None):
        n = exp.count - exp.acquired
        block_h = exp.exposure_seconds * n / 3600
        out = [_pushover(
            "Imaging",
            f"{target.name} [{bi}/{n_blocks}]: starting "
            f"{exp.filter_type.value} — {n}×{exp.exposure_seconds:.0f}s "
            f"(~{block_h:.1f}h) gain {exp.gain}"
            + (" (moon-free window)" if condition else "")),
            _smart_exposure(exp, target.start_guiding,
                            target.dither_every_n),
            _pushover(
            "Imaging",
            f"{target.name} [{bi}/{n_blocks}]: {exp.filter_type.value} block "
            f"done ({n}×{exp.exposure_seconds:.0f}s attempted)")]
        return out

    n_blocks = len(ordered)
    for bi, exp in enumerate(ordered, 1):
        is_bb = exp.filter_type.value not in NB_SET
        blk = _block(exp, bi, n_blocks, bb_condition if is_bb else None)
        if is_bb and bb_condition is not None:
            items.append(_seq_container(
                f"{exp.filter_type.value} until moonrise", blk,
                conditions=[bb_condition]))
        else:
            items.extend(blk)
    items.append(_pushover("Imaging", f"{target.name}: ALL blocks complete "
                           f"({plan_desc}) — moving on"))

    # AF triggers: temp drift + filter change + HFR creep — the proven trio
    # from the known-good AARO sequence (the time-based trigger validated
    # badly against disconnected equipment at load)
    triggers = [_meridian_flip_trigger(), _reconnect_trigger(),
                _autofocus_temp_trigger(1.0),
                _autofocus_filter_trigger(),
                _autofocus_hfr_trigger(10.0, 4)]

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
    """Generate an Advanced Sequencer JSON with the full night-loop safety
    architecture (Jerry Macon / Patriot Astro pattern, all core NINA types):

    Start:   connect safety monitor -> WaitUntilSafe -> connect everything,
             cool during twilight, twilight autofocus, hold for astro dusk
    Targets: LOOP_ALL_NIGHT (until dawn)
               SAFE_LOOP (while safe): re-arm equipment, run targets
               UNSAFE: park, WaitUntilSafe, loop resumes automatically
    End:     stop guiding, park, warm, disconnect — always runs at dawn
    """
    guided = any(t.start_guiding for t in sequence.targets)
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
    start_items = [
        _pushover("Startup", f"{sequence.name}: standby — "
                  f"{len(sequence.targets)} target(s) queued, cooling to "
                  f"{temp:.0f}°C at nautical dusk -30m"),
    ]
    if gate_dark:
        start_items.append(_wait_for_provider("NauticalDuskProvider", -30))
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
    start_items += [
        _connect("Safety Monitor"),
        _connect("Camera"),
        _dew_heater(True),
        _cool_camera(temp, 2.0),
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
        if first_filter is not None:
            start_items.append(_switch_filter(first_filter))
        start_items += [
            _wait_for_timespan(60),
            _autofocus(),
            _pushover("Startup", "twilight autofocus complete — holding "
                      "for the imaging gate"),
            _wait_for_provider(gate_provider, gate_offset),
            _pushover("Startup", gate_msg),
        ]

    # ---- Targets area: the night loop -------------------------------------
    target_containers = []
    first_guided = True
    for t in sequence.targets:
        force_cal = first_guided and t.start_guiding
        if t.start_guiding:
            first_guided = False
        target_containers.append(
            _build_target_container(t, sequence.wait_for_altitude, force_cal))

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
    unsafe_items += [
        _wait_until_safe(),
        _pushover("Safety", "SAFE again — waiting 2 min of confirmed-safe, "
                  "then unparking and resuming targets"),
    ]

    safe_loop = _seq_container("SAFE_LOOP", [
        _seq_container("RESET_EQUIPMENT_ONCE_SAFE", [
            _annotation("Runs on every safe (re)entry; harmless on first pass."),
            _pushover("Safety", "SAFE_LOOP entry — holding 2 min of "
                      "confirmed-safe, then unpark + track"),
            _wait_for_timespan(120),
            _unpark(),
            _set_tracking(0),
            _pushover("Safety", "equipment re-armed — proceeding to targets"),
        ]),
        _seq_container("TARGETS_CONTAINER", target_containers),
        _annotation("All targets done: park and hold (interruptible) until "
                    "dawn ends LOOP_ALL_NIGHT and the End area runs."),
        _pushover("Imaging", "all targets complete — parked, holding until dawn"),
        _park(),
        _wait_for_provider("DawnProvider", 0),
    ], conditions=[_safety_condition()])

    night_loop = _seq_container("LOOP_ALL_NIGHT", [
        safe_loop,
        _seq_container("UNSAFE", unsafe_items),
    ], conditions=[_time_condition(dawn_provider, dawn_offset)])

    # ---- End area -----------------------------------------------------------
    end_items = []
    from photonscript.shared.config import PhotonScriptConfig
    _cfg = PhotonScriptConfig()
    flat_filters = []
    for t in sequence.targets:
        for e in t.exposures:
            if e.filter_type not in flat_filters:
                flat_filters.append(e.filter_type)
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
            conditions=[_safety_condition()])
        end_items.append(flat_block)
    end_items.append(_pushover("Shutdown", "starting shutdown: park, "
                               "warm camera, disconnect"))
    if guided:
        end_items.append(_stop_guiding())
    if sequence.park_on_finish:
        end_items.append(_park())
    if sequence.warm_camera_on_finish:
        end_items.append(_warm_camera(3.0))
    end_items.append(_disconnect_all())
    end_items.append(_pushover("Shutdown", "shutdown complete — parked & warm"))

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
    root["Parent"] = None
    return json.dumps(root, indent=2)
