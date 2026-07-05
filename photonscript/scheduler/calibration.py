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


def calibration_health(config) -> dict:
    """Latest capture per type across every night folder + staleness."""
    from astropy.io import fits as _fits

    root = Path(config.image_watch_dir)
    latest: dict[str, str] = {}     # type -> newest date
    totals: dict[str, int] = {}
    files_by_type_date: dict[tuple, list[Path]] = {}
    if root.exists():
        for d in sorted(p for p in root.iterdir()
                        if p.is_dir() and _DATE_RE.match(p.name)):
            for f in d.rglob("*.fits"):
                parts = f.relative_to(d).parts
                if not _is_calibration(parts):
                    continue
                typ = next(({"BIAS": "BIAS"}.get(p.upper(), p.upper().rstrip("S")) for p in parts
                            if p.upper() in _CAL_DIRS), "CAL")
                if typ == "SNAPSHOT":
                    continue
                totals[typ] = totals.get(typ, 0) + 1
                if d.name >= latest.get(typ, ""):
                    latest[typ] = d.name
                files_by_type_date.setdefault((typ, d.name), []).append(f)

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
    root = Path(config.image_watch_dir)
    if not root.exists():
        return 0
    cal_days = int(getattr(config, "library_cal_days", 120))
    cutoff = (datetime.now() - __import__("datetime")
              .timedelta(days=cal_days)).strftime("%Y-%m-%d")
    n = 0
    for d in root.iterdir():
        if not (d.is_dir() and _DATE_RE.match(d.name) and d.name >= cutoff):
            continue
        for f in d.rglob("*.fits"):
            parts = f.relative_to(d).parts
            if not any(p.upper() in ("DARK", "DARKS") for p in parts):
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


def generate_dusk_flats_json(config) -> tuple:
    """Standalone dusk sky-flat run for TODAY: wait for sunset+15 local,
    slew high away from the sun, sky flats for every filter (broadband
    first — dusk DIMS, so narrowband gets the darker end), park.
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
    filters = [FilterType(v) for v in
               ("Ha", "OIII", "SII", "R", "G", "B", "L")]
    wait_start = _make_typed(
        "NINA.Sequencer.SequenceItem.Utility.WaitForTime, NINA.Sequencer",
        Hours=local.hour, Minutes=local.minute, MinutesOffset=0, Seconds=0,
        SelectedProvider=_make_typed(
            "NINA.Sequencer.Utility.DateTimeProvider.TimeProvider, "
            "NINA.Sequencer"))
    items = [
        _pushover("Flats", f"dusk sky flats: waiting for "
                  f"{local.strftime('%H:%M')} local (sunset +15), then "
                  f"{n} per filter — narrowband first (least light "
                  "through to most: Ha, OIII, SII, R, G, B, L)"),
        _connect("Safety Monitor"),
        _connect("Camera"),
        _cool_camera(config.camera_setpoint_c, 2.0),
        _connect("Filter Wheel"),
        _connect("Mount"),
        wait_start,
        _wait_until_safe(),
        _unpark(),
        _set_tracking(0),
        _slew_alt_az(85, 200),
    ] + [_sky_flat(f, n, config.default_gain, config.default_offset)
         for f in filters] + [
        _pushover("Flats", "dusk sky flats complete — parking (cooler "
                  "stays on for tonight's run)"),
        _park(),
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
