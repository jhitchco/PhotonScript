"""Calibration frames: inventory health + dark/bias capture sequences.

Setup reality at AARO Pier 3: no shutter and no flat panel. Darks and bias
need external darkness — the closed roll-off roof at night (unsafe/cloudy
nights are perfect). Flats are dusk/dawn sky flats (generation pending a
NINA template export to confirm the auto-exposure-flat instruction type).

Staleness guidance: flats age with dust/optics changes (45 d), darks with
sensor drift (90 d), bias rarely (180 d).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from photonscript.scheduler.runs import _CAL_DIRS, _is_calibration

logger = logging.getLogger(__name__)

STALE_DAYS = {"FLAT": 45, "DARK": 90, "BIAS": 180}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_CAL_TYPES = ("BIAS", "DARK", "FLAT")


def _norm_cal_type(name: str) -> str:
    """DARKS -> DARK, FLATS -> FLAT, BIAS -> BIAS (not 'BIA')."""
    up = str(name).upper()
    return {"BIAS": "BIAS", "BIASES": "BIAS"}.get(up, up.rstrip("S"))


def _iter_watch_frames(root: Path):
    """NINA output layout: <watch>/<YYYY-MM-DD>/.../<TYPE>/*.fits"""
    if not root.exists():
        return
    for d in sorted(p for p in root.iterdir()
                    if p.is_dir() and _DATE_RE.match(p.name)):
        for f in d.rglob("*.fits"):
            parts = f.relative_to(d).parts
            if not _is_calibration(parts):
                continue
            typ = next((_norm_cal_type(p) for p in parts
                        if p.upper() in _CAL_DIRS), "CAL")
            if typ == "SNAPSHOT":
                continue
            yield typ, d.name, f


def _iter_library_frames(lib: Path):
    """Librarian layout: <lib>/Calibration/<TYPE>/<YYYY-MM-DD>/*.fits

    build_library() hardlinks frames here; once the NINA output dir is
    pruned/archived this tree is the only surviving copy, so calibration
    discovery has to look here too or the library reads as empty.
    """
    cal = lib / "Calibration"
    if not cal.exists():
        return
    for tdir in sorted(p for p in cal.iterdir() if p.is_dir()):
        typ = _norm_cal_type(tdir.name)
        if typ not in _CAL_TYPES:
            continue
        for ddir in sorted(p for p in tdir.iterdir()
                           if p.is_dir() and _DATE_RE.match(p.name)):
            for f in ddir.rglob("*.fits"):
                yield typ, ddir.name, f


def iter_calibration_frames(config):
    """Yield (type, night_date, path) across every place frames may live.

    Scans the NINA output dir first, then the librarian tree. Library
    entries are hardlinks of watch-dir files, so dedupe on
    (type, date, filename) to avoid double-counting.
    """
    from photonscript.scheduler.runs import library_root

    seen: set[tuple[str, str, str]] = set()
    sources = [_iter_watch_frames(Path(config.image_watch_dir))]
    try:
        sources.append(_iter_library_frames(library_root(config)))
    except Exception:  # noqa: BLE001 - library dir misconfigured; watch dir still counts
        logger.warning("library_root unavailable for calibration scan", exc_info=True)
    for src in sources:
        for typ, date, f in src:
            key = (typ, date, f.name)
            if key in seen:
                continue
            seen.add(key)
            yield typ, date, f


def calibration_health(config) -> dict:
    """Latest capture per type across every night folder + staleness."""
    from astropy.io import fits as _fits

    latest: dict[str, str] = {}     # type -> newest date
    totals: dict[str, int] = {}
    files_by_type_date: dict[tuple, list[Path]] = {}
    for typ, date, f in iter_calibration_frames(config):
        totals[typ] = totals.get(typ, 0) + 1
        if date >= latest.get(typ, ""):
            latest[typ] = date
        files_by_type_date.setdefault((typ, date), []).append(f)

    today = datetime.now().date()
    out = {}
    for typ in ("BIAS", "DARK", "FLAT"):
        if typ not in latest:
            out[typ] = {"latest": None, "age_days": None, "total": 0,
                        "stale": True, "detail": {}, "location": None,
                        "note": "none on disk"}
            continue
        newest = latest[typ]
        age = (today - datetime.strptime(newest, "%Y-%m-%d").date()).days
        # Detail (filters / exposures) from the newest session's headers
        detail: dict[str, int] = {}
        for f in files_by_type_date.get((typ, newest), [])[:400]:
            try:
                hdr = _fits.getheader(f)
            except Exception:  # noqa: BLE001
                continue
            key = (str(hdr.get("FILTER", "?")) if typ == "FLAT"
                   else f"{float(hdr.get('EXPTIME', 0)):g}s")
            detail[key] = detail.get(key, 0) + 1
        loc = sorted({str(f.parent) for f in
                      files_by_type_date.get((typ, newest), [])})
        out[typ] = {"latest": newest, "age_days": age,
                    "location": loc[0] if loc else None,
                    "count_latest": len(files_by_type_date.get((typ, newest), [])),
                    "total": totals.get(typ, 0),
                    "stale": age > STALE_DAYS[typ],
                    "stale_after_days": STALE_DAYS[typ],
                    "detail": detail}

    # Per-bucket latest ACROSS sessions: a filter's newest flat set (or an
    # exposure's newest dark set) is often older than the newest session of
    # that type - e.g. tonight's S/H/O flats hide July's RGB set. Walk
    # sessions newest-first until every bucket is seen (8-session cap).
    try:
        rev = config.reverse_filter_map()
    except Exception:  # noqa: BLE001
        rev = {}
    for typ in ("FLAT", "DARK"):
        if typ not in latest:
            continue
        buckets: dict[str, dict] = {}
        dates = sorted({d for (t, d) in files_by_type_date if t == typ},
                       reverse=True)[:8]
        for dt in dates:
            counts: dict[str, int] = {}
            for f in files_by_type_date.get((typ, dt), [])[:400]:
                try:
                    hdr = _fits.getheader(f)
                except Exception:  # noqa: BLE001
                    continue
                if typ == "FLAT":
                    raw = str(hdr.get("FILTER", "?"))
                    key = rev.get(raw, raw)
                else:
                    key = f"{float(hdr.get('EXPTIME', 0)):g}s"
                counts[key] = counts.get(key, 0) + 1
            age_d = (today - datetime.strptime(dt, "%Y-%m-%d").date()).days
            for k, v in counts.items():
                if k not in buckets:
                    buckets[k] = {"date": dt, "count": v, "age_days": age_d}
        out[typ]["by_bucket"] = buckets
    return out


def generate_darks_json(config, darks: list[tuple[float, int]],
                        bias_count: int = 50) -> tuple[str, float]:
    """NINA sequence: cool -> DARK exposures -> BIAS -> warm.

    Mount untouched; run only with the roof closed at night (no shutter).
    Returns (json_text, estimated_minutes).
    """
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _pushover, _connect,
        _cool_camera, _warm_camera)

    def _exposures(name, exp_s, count, image_type):
        return _seq_container(name, [
            _make_typed(
                "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
                "NINA.Sequencer",
                ExposureTime=exp_s,
                Gain=config.default_gain, Offset=config.default_offset,
                Binning=_make_typed(
                    "NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                    X=1, Y=1),
                ImageType=image_type, ExposureCount=0,
                ErrorBehavior=0, Attempts=1),
        ], conditions=[_make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=count)])

    total_min = (sum(e * c for e, c in darks) + 0.005 * bias_count) / 60 + 12
    dark_items = [_exposures(f"DARK {e:g}s x{c}", e, c, "DARK")
                  for e, c in darks]
    plan_txt = ", ".join(f"{e:g}s×{c}" for e, c in darks)

    root = _seq_container(
        "PhotonScript_Calibration",
        [
            _seq_container("Start", [_seq_container("Calibration startup", [
                _pushover("Calibration", f"darks starting: {plan_txt} + "
                          f"{bias_count} bias — roof must be CLOSED. "
                          f"~{total_min:.0f} min"),
                _connect("Camera"),
                _cool_camera(config.camera_setpoint_c, 2.0),
            ])], container_type="NINA.Sequencer.Container.StartAreaContainer,"
                " NINA.Sequencer"),
            _seq_container("Targets",
                           dark_items
                           + [_exposures(f"BIAS x{bias_count}", 0.001,
                                         bias_count, "BIAS")],
                           container_type="NINA.Sequencer.Container."
                           "TargetAreaContainer, NINA.Sequencer"),
            _seq_container("End", [_seq_container("Calibration shutdown", [
                _pushover("Calibration", "darks + bias complete — warming"),
                _warm_camera(3.0),
            ])], container_type="NINA.Sequencer.Container.EndAreaContainer, "
                "NINA.Sequencer"),
        ],
        container_type="NINA.Sequencer.Container.SequenceRootContainer, "
                       "NINA.Sequencer",
    )
    return json.dumps(root, indent=2), total_min


def count_matching_darks(config, exp_s: float) -> int:
    """Darks on disk (within the library age window) matching the current
    epoch: exposure + gain + offset + setpoint temperature."""
    from astropy.io import fits as _fits
    cal_days = int(getattr(config, "library_cal_days", 120))
    cutoff = (datetime.now() - __import__("datetime")
              .timedelta(days=cal_days)).strftime("%Y-%m-%d")
    n = 0
    for typ, date, f in iter_calibration_frames(config):
        if typ != "DARK" or date < cutoff:
            continue
        try:
            h = _fits.getheader(f)
        except Exception:  # noqa: BLE001
            continue
        if (abs(float(h.get("EXPTIME", -1)) - exp_s) < 0.5
                and int(h.get("GAIN", -1)) == config.default_gain
                and int(h.get("OFFSET", -1)) == config.default_offset
                and abs(float(h.get("SET-TEMP", 99))
                        - config.camera_setpoint_c) < 1.5):
            n += 1
    return n


def days_since_last_bias(config) -> int | None:
    """Age (in days) of the newest night folder holding BIAS frames, or None
    if the library has no bias at all. Lightweight: matches on the BIAS
    directory name only (no FITS header reads), unlike calibration_health."""
    newest: str | None = None
    for typ, date, _f in iter_calibration_frames(config):
        if typ == "BIAS" and date > (newest or ""):
            newest = date
    if newest is None:
        return None
    return (datetime.now().date()
            - datetime.strptime(newest, "%Y-%m-%d").date()).days


def stale_flat_filters(config) -> list[str]:
    """Canonical filter names whose newest flat set is missing or stale."""
    health = calibration_health(config)
    bb = (health.get("FLAT") or {}).get("by_bucket") or {}
    out = []
    for f in ("Ha", "OIII", "SII", "R", "G", "B", "L"):
        b = bb.get(f)
        if b is None or b.get("age_days", 9999) > STALE_DAYS["FLAT"]:
            out.append(f)
    return out


def _osc_sky_flat(count: int, gain: int, offset: int) -> dict:
    """A SkyFlat block for a one-shot-color rig — no filter wheel / SwitchFilter,
    just the auto-exposure flat loop (mirrors _sky_flat minus the filter step)."""
    from photonscript.scheduler.nina_sequence_json import _seq_container, _make_typed
    loop = _seq_container(
        f"{count} flats",
        [_make_typed(
            "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer",
            ExposureTime=0.0, Gain=gain, Offset=offset,
            Binning=_make_typed(
                "NINA.Core.Model.Equipment.BinningMode, NINA.Core", X=1, Y=1),
            ImageType="FLAT", ExposureCount=0, ErrorBehavior=0, Attempts=1)],
        conditions=[_make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=count)])
    sf = _seq_container(
        "Sky flats OSC", [loop],
        container_type="NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat, "
                       "NINA.Sequencer")
    sf["IsExpanded"] = False
    sf.update(MinExposure=0.1, MaxExposure=30.0, HistogramTargetPercentage=0.5,
              HistogramTolerancePercentage=0.1, ShouldDither=False,
              DitherPixels=3.0, DitherSettleTime=5.0)
    return sf


def generate_dusk_flats_json(config, only_filters: list[str] | None = None,
                             osc: bool = False, owns_mount: bool = True) -> tuple:
    """Standalone dusk sky-flat run for TODAY: wait for sunset+15 local,
    slew high away from the sun, sky flats, park. Filtered rigs shoot one set
    per filter (broadband first — dusk DIMS); an OSC rig (osc=True) shoots a
    single set with no filter wheel.

    owns_mount=False is the piggyback case: NINA #2 owns only its camera, so
    the sequence never connects/slews/parks the mount or safety monitor — it
    rides the main rig's mount and must be triggered alongside the main rig's
    dusk flats (that slew points both scopes at the flat sky).
    Returns (json_text, start_local_hhmm)."""
    import json as _json
    from datetime import timedelta
    from photonscript.scheduler import night_plan as _np
    from photonscript.shared.localtime import utc_offset_hours
    from photonscript.shared.models import FilterType
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _pushover, _connect, _cool_camera,
        _sky_flat, _slew_alt_az, _wait_until_safe, _unpark, _park,
        _set_tracking)

    obs = config.get_observatory()
    now = datetime.utcnow()
    tw = _np.compute_night_times(obs, now.replace(hour=0, minute=0,
                                                     second=0, microsecond=0))
    sunset = tw.get("sunset")
    if not sunset:
        raise RuntimeError("could not compute sunset")
    local = (sunset + timedelta(minutes=15)
             + timedelta(hours=utc_offset_hours(config, sunset)))
    n = int(getattr(config, "flat_count", 15))
    # dusk DIMS: narrowband needs the bright end (longest exposures),
    # L needs the darkest — Jeremy's order: NB -> R,G,B -> L
    wanted = ("Ha", "OIII", "SII", "R", "G", "B", "L")
    if only_filters:
        wanted = tuple(f for f in wanted if f in only_filters)
    filters = [FilterType(v) for v in wanted]
    def _wait_for(t):
        return _make_typed(
            "NINA.Sequencer.SequenceItem.Utility.WaitForTime, NINA.Sequencer",
            Hours=t.hour, Minutes=t.minute, MinutesOffset=0, Seconds=0,
            SelectedProvider=_make_typed(
                "NINA.Sequencer.Utility.DateTimeProvider.TimeProvider, "
                "NINA.Sequencer"))
    wait_start = _wait_for(local)
    # cool only ~30 min before flats begin - dispatching at noon should NOT
    # run the cooler all afternoon (2026-09-08 request)
    wait_cool = _wait_for(local - timedelta(minutes=30))
    intro = (f"dusk sky flats: waiting for {local.strftime('%H:%M')} local "
             f"(sunset +15), then {n} " + ("OSC flats (one-shot color, no "
             "filter wheel)" if osc else "per filter — narrowband first "
             "(least light through to most: Ha, OIII, SII, R, G, B, L)"))
    flat_blocks = ([_osc_sky_flat(n, config.default_gain, config.default_offset)]
                   if osc else
                   [_sky_flat(f, n, config.default_gain, config.default_offset)
                    for f in filters])
    if owns_mount:
        items = [
            _pushover("Flats", intro),
            _connect("Safety Monitor"),
            _connect("Camera"),
            wait_cool,
            _cool_camera(config.camera_setpoint_c, 2.0),
        ] + ([] if osc else [_connect("Filter Wheel")]) + [
            _connect("Mount"),
            wait_start,
            _wait_until_safe(),
            _unpark(),
            _set_tracking(0),
            _slew_alt_az(85, 200),
        ] + flat_blocks + [
            _pushover("Flats", "dusk sky flats complete — parking (cooler "
                      "stays on for tonight's run)"),
            _park(),
        ]
    else:
        # Piggyback rides the main rig's mount: NINA #2 owns only its camera,
        # so this run never connects/slews/parks the mount or safety monitor.
        # Trigger it with the main rig's dusk flats — that slew aims both scopes.
        items = [
            _pushover("Flats", intro + " — piggyback rides the main mount; "
                      "run this alongside the main rig's flats"),
            _connect("Camera"),
            wait_cool,
            _cool_camera(config.camera_setpoint_c, 2.0),
            wait_start,
        ] + flat_blocks + [
            _pushover("Flats", "piggyback dusk flats complete"),
        ]
    root = _seq_container(
        "PhotonScript_DuskFlats",
        [
            _seq_container("Start", [], container_type="NINA.Sequencer."
                           "Container.StartAreaContainer, NINA.Sequencer"),
            _seq_container("Targets", items, container_type="NINA.Sequencer."
                           "Container.TargetAreaContainer, NINA.Sequencer"),
            _seq_container("End", [], container_type="NINA.Sequencer."
                           "Container.EndAreaContainer, NINA.Sequencer"),
        ],
        container_type="NINA.Sequencer.Container.SequenceRootContainer, "
                       "NINA.Sequencer")
    root["Parent"] = None
    return _json.dumps(root, indent=2), local.strftime("%H:%M")


def _osc_dark_blocks(config, dawn_provider="DawnProvider", dawn_offset=0):
    """OSC dark blocks for the piggyback, capped by its own library quota
    (count_matching_darks keys off the piggyback config's library_dir + OSC
    gain/offset/setpoint). Roof-closed work: each block exits on any of
    LoopWhileUnsafe clearing, dawn, or the per-exposure cap."""
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _time_condition)
    quota = int(getattr(config, "dark_target_count", 30))
    blocks = []
    for tok in str(getattr(config, "dark_exposures", "120")).split(","):
        try:
            exp_s = float(tok.strip())
        except ValueError:
            continue
        try:
            have = count_matching_darks(config, exp_s)
        except Exception:  # noqa: BLE001
            have = 0
        need = max(0, quota - have)
        if need == 0:
            continue
        blocks.append(_seq_container(
            f"OSC DARKS_{exp_s:.0f}s (need {need} of {quota})",
            [_make_typed(
                "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
                "NINA.Sequencer",
                ExposureTime=exp_s, Gain=config.default_gain,
                Offset=config.default_offset,
                Binning=_make_typed(
                    "NINA.Core.Model.Equipment.BinningMode, NINA.Core", X=1, Y=1),
                ImageType="DARK", ExposureCount=0, ErrorBehavior=0, Attempts=1)],
            conditions=[
                _make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                            "NINA.Sequencer"),
                _time_condition(dawn_provider, dawn_offset),
                _make_typed("NINA.Sequencer.Conditions.LoopCondition, "
                            "NINA.Sequencer",
                            CompletedIterations=0, Iterations=need)]))
    return blocks


def generate_piggyback_companion_json(config, has_safety: bool = False) -> str:
    """A full-night calibration companion for the piggyback (NINA #2), meant to
    be dispatched alongside the RC16 armed sequence so ONE arm covers both
    scopes' calibration.

    The piggyback owns only its camera + focuser and rides the RC16 mount, so
    this sequence never slews, unparks or parks. It:
      * cools the OSC camera ~cool_lead before astro dark,
      * (has_safety) fills OSC darks to quota + a 50-bias top-up while the roof
        is CLOSED (LoopWhileUnsafe) — needs the SHARED safety monitor connected
        in the NINA #2 profile, since NINA #2 can't otherwise tell roof state,
      * at nautical dawn +5 shoots one OSC sky-flat set (riding the RC16's dawn
        slew), then warms.

    has_safety=False (no safety monitor on NINA #2): darks/bias are skipped with
    an annotation (use the Calibration page button on a closed-roof night) and
    the dawn flats fire time-gated only. Returns the sequence JSON text.
    """
    import json as _json
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _pushover, _connect, _cool_camera,
        _warm_camera, _dew_heater, _wait_until_safe, _wait_for_provider,
        _annotation)

    n = int(getattr(config, "flat_count", 15))
    cool_lead = int(getattr(config, "cool_lead_minutes", 30))
    setpoint = float(getattr(config, "camera_setpoint_c", 0.0))

    start_items = [
        _pushover("Piggyback", "companion calibration armed: cools the OSC "
                  f"camera, {'fills darks/bias while the roof is closed, ' if has_safety else ''}"
                  "then shoots OSC dawn flats. No mount control — rides the RC16."),
        _connect("Camera"),
        _dew_heater(True),
    ]
    if has_safety:
        start_items.append(_connect("Safety Monitor"))
    start_items += [
        _wait_for_provider("DuskProvider", -cool_lead),
        _cool_camera(setpoint, 2.0),
    ]

    target_items = []
    if has_safety:
        dark_blocks = _osc_dark_blocks(config)
        if dark_blocks:
            target_items.append(_seq_container(
                "OSC_DARKS_IF_UNSAFE", dark_blocks,
                conditions=[
                    _make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                                "NINA.Sequencer"),
                    _make_typed("NINA.Sequencer.Conditions.LoopCondition, "
                                "NINA.Sequencer",
                                CompletedIterations=0, Iterations=1)]))
        # bias top-up only when due (bias barely ages)
        _bias_refresh_days = int(getattr(config, "bias_refresh_days", 60))
        try:
            _bias_age = days_since_last_bias(config)
        except Exception:  # noqa: BLE001
            _bias_age = None
        _bias_due = (_bias_refresh_days <= 0 or _bias_age is None
                     or _bias_age >= _bias_refresh_days)
        if _bias_due:
            target_items.append(_seq_container(
                "OSC_BIAS_IF_UNSAFE",
                [_seq_container("50 bias", [_make_typed(
                    "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
                    "NINA.Sequencer",
                    ExposureTime=0.001, Gain=config.default_gain,
                    Offset=config.default_offset,
                    Binning=_make_typed(
                        "NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                        X=1, Y=1),
                    ImageType="BIAS", ExposureCount=0,
                    ErrorBehavior=0, Attempts=1)],
                    conditions=[_make_typed(
                        "NINA.Sequencer.Conditions.LoopCondition, "
                        "NINA.Sequencer",
                        CompletedIterations=0, Iterations=50)])],
                conditions=[
                    _make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                                "NINA.Sequencer"),
                    _make_typed("NINA.Sequencer.Conditions.LoopCondition, "
                                "NINA.Sequencer",
                                CompletedIterations=0, Iterations=1)]))
        target_items.append(_wait_until_safe())
    else:
        target_items.append(_annotation(
            "OSC darks/bias skipped: NINA #2 is not seeing the safety monitor, "
            "so it can't tell when the roof is closed. Add the shared safety "
            "monitor to the NINA #2 profile (it auto-connects on arm and this "
            "block turns on by itself), or shoot darks/bias from the Calibration "
            "page on a closed-roof night."))

    # Dawn flats — wait for the RC16's flat window (nautical dawn +5), then one
    # OSC set. WaitUntilSafe only when NINA #2 can see the monitor.
    target_items.append(_wait_for_provider("NauticalDawnProvider", 5))
    if has_safety:
        target_items.append(_wait_until_safe())
    target_items += [
        _pushover("Piggyback", f"dawn flat window — shooting {n} OSC sky flats "
                  "(riding the RC16 slew)"),
        _osc_sky_flat(n, config.default_gain, config.default_offset),
        _pushover("Piggyback", "OSC dawn flats complete — warming"),
    ]

    end_items = [_warm_camera(3.0),
                 _pushover("Piggyback", "companion calibration done — camera warm")]

    root = _seq_container(
        "PhotonScript_PiggybackCompanion",
        [
            _seq_container("Start", start_items,
                           container_type="NINA.Sequencer.Container."
                           "StartAreaContainer, NINA.Sequencer"),
            _seq_container("Targets", target_items,
                           container_type="NINA.Sequencer.Container."
                           "TargetAreaContainer, NINA.Sequencer"),
            _seq_container("End", end_items,
                           container_type="NINA.Sequencer.Container."
                           "EndAreaContainer, NINA.Sequencer"),
        ],
        container_type="NINA.Sequencer.Container.SequenceRootContainer, "
                       "NINA.Sequencer")
    root["Parent"] = None
    return _json.dumps(root, indent=2)
