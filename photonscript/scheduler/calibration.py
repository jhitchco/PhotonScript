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

# Piggyback OSC loop container names (PS-78): structural loops, not targets.
# shared/target_names imports them so a sub named after one of these reads as
# unattributed and the PS-51 time correlation names it instead.
OSC_LIGHT_LOOP_NAME = "OSC_LIGHT_LOOP"
OSC_IMAGE_PASS_NAME = "OSC_IMAGE_PASS"
OSC_LIGHTS_UNTIL_DAWN_NAME = "OSC_LIGHTS_UNTIL_DAWN"
PIGGYBACK_LOOP_CONTAINER_NAMES = (OSC_LIGHT_LOOP_NAME, OSC_IMAGE_PASS_NAME,
                                  OSC_LIGHTS_UNTIL_DAWN_NAME)
# PS-25 resume debounce pieces (no exposures inside, so not structural loops).
OSC_RESUME_HOLD_NAME = "OSC_RESUME_HOLD"
OSC_WAIT_SAFE_CONFIRM_NAME = "WAIT_SAFE_CONFIRM_OR_NAUTICAL_DAWN"
OSC_ROOF_OPEN_NOTICE_NAME = "OSC_ROOF_OPEN_NOTICE"

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

    # --- Per-camera view (INSTRUME) ------------------------------------------
    # The flat aggregate above hides that one camera may have NO calibration at
    # all: at AARO the mono AP26MC has darks/bias/flats while the OSC AP26CC has
    # none, so OSC lights were integrating uncalibrated. Split by camera so that
    # gap is visible. One header read per (type,date) bucket keeps it cheap.
    def _bucket_camera(files) -> str:
        for f in files[:5]:
            try:
                return str(_fits.getheader(f).get("INSTRUME", "?")).strip() or "?"
            except Exception:  # noqa: BLE001
                continue
        return "?"

    by_camera: dict[str, dict] = {}
    for (typ, date), files in files_by_type_date.items():
        cam = _bucket_camera(files)
        slot = by_camera.setdefault(cam, {})
        t = slot.setdefault(typ, {"total": 0, "latest": None,
                                  "count_latest": 0, "buckets": {}})
        t["total"] += len(files)
        age_d = (today - datetime.strptime(date, "%Y-%m-%d").date()).days
        t["buckets"][date] = {"count": len(files), "age_days": age_d}
        if t["latest"] is None or date > t["latest"]:
            t["latest"] = date
            t["count_latest"] = len(files)
    for _cam, _types in by_camera.items():
        for _typ, _t in _types.items():
            if _t["latest"]:
                _t["age_days"] = (today - datetime.strptime(
                    _t["latest"], "%Y-%m-%d").date()).days
                _t["stale"] = _t["age_days"] > STALE_DAYS[_typ]
    out["by_camera"] = by_camera
    out["cameras"] = sorted(c for c in by_camera if c != "?")

    # Flag a dual-rig setup where a camera has no calibration at all.
    if getattr(config, "piggyback_enabled", False):
        have_types = {c: set(by_camera[c]) for c in by_camera if c != "?"}
        missing = [c for c, ts in have_types.items()
                   if ts != set(_CAL_TYPES)]
        if len(by_camera) < 2 or missing:
            out["multi_camera_note"] = (
                "Piggyback (OSC) enabled — calibration present for "
                f"{sorted(have_types) or 'no cameras'}. A camera missing here "
                "integrates UNCALIBRATED; capture its BIAS/DARK/FLAT.")
    return out


def generate_darks_json(config, darks: list[tuple[float, int]],
                        bias_count: int = 50, *, safety_gated: bool = False,
                        warm_minutes: float = 3.0) -> tuple[str, float]:
    """NINA sequence: cool -> DARK exposures -> BIAS -> warm.

    Mount untouched; run only with the roof closed at night (no shutter).
    Returns (json_text, estimated_minutes).

    PS-113 (calibration_capture): safety_gated adds LoopWhileUnsafe to every
    exposure block, so NINA itself stops shooting darks the moment its
    safety monitor reads safe (roof open); bias_count 0 leaves the bias
    block out; warm_minutes is the End-area warm.
    """
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _pushover, _connect,
        _cool_camera, _warm_camera)

    def _exposures(name, exp_s, count, image_type):
        conds = ([_make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                              "NINA.Sequencer")] if safety_gated else [])
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
        ], conditions=conds + [_make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=count)])

    total_min = (sum(e * c for e, c in darks) + 0.005 * bias_count) / 60 + 12
    dark_items = [_exposures(f"DARK {e:g}s x{c}", e, c, "DARK")
                  for e, c in darks]
    plan_txt = ", ".join(f"{e:g}s×{c}" for e, c in darks)
    bias_items = ([_exposures(f"BIAS x{bias_count}", 0.001, bias_count, "BIAS")]
                  if bias_count > 0 else [])

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
                           dark_items + bias_items,
                           container_type="NINA.Sequencer.Container."
                           "TargetAreaContainer, NINA.Sequencer"),
            _seq_container("End", [_seq_container("Calibration shutdown", [
                _pushover("Calibration", "darks + bias complete — warming"),
                _warm_camera(warm_minutes),
            ])], container_type="NINA.Sequencer.Container.EndAreaContainer, "
                "NINA.Sequencer"),
        ],
        container_type="NINA.Sequencer.Container.SequenceRootContainer, "
                       "NINA.Sequencer",
    )
    from photonscript.scheduler.nina_sequence_json import link_parents
    return json.dumps(link_parents(root), indent=2), total_min


def _pb_gain_offset(config) -> tuple[int, int]:
    """OSC (piggyback) capture gain/offset. The OSC images at its own values
    (100/256), NOT the mono RC16's default_gain/offset (200/…) — so its darks,
    bias and flats MUST be shot at these or they won't calibrate the lights."""
    return (int(getattr(config, "piggyback_default_gain", 100)),
            int(getattr(config, "piggyback_default_offset", 256)))


def count_matching_darks(config, exp_s: float, *, gain: int | None = None,
                         offset: int | None = None,
                         setpoint: float | None = None,
                         readout: str | None = None) -> int:
    """Darks on disk (within the library age window) matching the epoch:
    exposure + gain + offset + setpoint temperature + (PS-128) readout mode.
    gain/offset/setpoint/readout default to the config's (the mono RC16's;
    a rig_config view carries the piggyback's) so the two cameras' dark
    libraries don't cross-count (gain 200 vs 100). A dark with no readout
    keyword is assumed to be at the config's camera_readout_mode; a blank
    camera_readout_mode matches any readout."""
    from astropy.io import fits as _fits
    from photonscript.shared.rigs import header_readout, normalize_readout
    gain = config.default_gain if gain is None else gain
    offset = config.default_offset if offset is None else offset
    setpoint = config.camera_setpoint_c if setpoint is None else setpoint
    ro_default = normalize_readout(getattr(config, "camera_readout_mode", "HCG"))
    readout = ro_default if readout is None else normalize_readout(readout)
    cal_days = int(getattr(config, "library_cal_days", 120))
    cutoff = (datetime.now() - __import__("datetime")
              .timedelta(days=cal_days)).strftime("%Y-%m-%d")
    bad = _qa_failed_keys(config)
    n = 0
    for typ, date, f in iter_calibration_frames(config):
        if typ != "DARK" or date < cutoff:
            continue
        if f"{typ}/{date}/{f.name}" in bad:
            continue  # PS-113: QA failed (its watch-dir original still exists)
        try:
            h = _fits.getheader(f)
        except Exception:  # noqa: BLE001
            continue
        if (abs(float(h.get("EXPTIME", -1)) - exp_s) < 0.5
                and int(h.get("GAIN", -1)) == gain
                and int(h.get("OFFSET", -1)) == offset
                and abs(float(h.get("SET-TEMP", 99)) - setpoint) < 1.5
                and (not readout
                     or (header_readout(h)[0] or ro_default) == readout)):
            n += 1
    return n


def dark_epoch(config, rig: str = "rc16") -> dict:
    """PS-122: gain / offset / setpoint a rig's night dark quota fills at.
    Reads the rig's own keys, so the base config and the rig view agree
    (never re-wraps a view: rig_config on a view nests its library dir).
    PS-128: plus the readout mode (rig_readout; None = not matched)."""
    from photonscript.shared.rigs import rig_readout
    if rig == "rc16":
        return {"gain": int(config.default_gain),
                "offset": int(config.default_offset),
                "setpoint": float(config.camera_setpoint_c),
                "readout": rig_readout(config, rig)}
    gain, offset = _pb_gain_offset(config)
    return {"gain": gain, "offset": offset,
            "setpoint": float(getattr(config, "piggyback_setpoint_c", 0.0)),
            "readout": rig_readout(config, rig)}


def quota_exposures(config, rig: str = "rc16") -> list[float]:
    """PS-122: the dark lengths the night quota fills for a rig. RC16: config
    dark_exposures plus the PS-66 unguided cap (dark_library_exposures);
    Piggy-600: piggyback_dark_exposures. One list for the armer, the
    companion and the Calibration owed view."""
    if rig == "rc16":
        from photonscript.scheduler.nina_sequence_json import dark_library_exposures
        return dark_library_exposures(config)
    out: list[float] = []
    for tok in str(getattr(config, "piggyback_dark_exposures", "120")).split(","):
        try:
            out.append(float(tok.strip()))
        except ValueError:
            continue
    return out


def darks_have(config, rig: str, exp_s: float, *, gain: int | None = None,
               offset: int | None = None, setpoint: float | None = None,
               readout: str | None = None,
               store: dict | None = None) -> int:
    """PS-122: darks that count toward the quota for one exposure, the one
    rule the night quota (RC16 armer, Piggy-600 companion), readiness and the
    Calibration owed view share. Once the rig has a calibration QA store
    (calibration_qa_mode not off) only QA-passed frames count; without one,
    the header count minus QA-failed frames (count_matching_darks).
    `config` is the rig's scan view (the base config for the RC16); the
    epoch defaults to dark_epoch(rig). PS-128: only darks at the readout
    mode count (the rig's lights' mode by default; a dark whose header has
    no readout keyword is assumed to be at the rig's)."""
    from photonscript.shared.rigs import normalize_readout
    ep = dark_epoch(config, rig)
    if gain is not None:
        ep["gain"] = int(gain)
    if offset is not None:
        ep["offset"] = int(offset)
    if setpoint is not None:
        ep["setpoint"] = float(setpoint)
    if readout is not None:
        ep["readout"] = normalize_readout(readout)
    if ep["readout"] is None:
        ep["readout"] = ""   # readout not matched ("" = match any, both counters)
    try:
        from photonscript.scheduler import calibration_qa as cq
        if cq.mode(config) != "off":
            st = store if store is not None else cq.load_store(config, rig)
            if st["frames"]:
                return cq.count_passed_darks(config, rig, exp_s, store=st, **ep)
    except Exception:  # noqa: BLE001 - never break the quota over QA
        logger.warning("QA dark count failed; using the header count", exc_info=True)
    return count_matching_darks(config, exp_s, **ep)


def dark_quota(config, rig: str, exp_s: float, *, store: dict | None = None,
               **epoch) -> dict:
    """PS-122: {"exp_s", "have", "quota", "need"} for one dark length:
    quota = dark_target_count, need = quota - have (never below 0)."""
    quota = int(getattr(config, "dark_target_count", 30))
    have = darks_have(config, rig, exp_s, store=store, **epoch)
    return {"exp_s": float(exp_s), "have": have, "quota": quota,
            "need": max(0, quota - have)}


def _qa_failed_keys(config) -> set:
    """PS-113: "<TYPE>/<date>/<name>" of every calibration frame QA failed
    (either rig's store), so the dark quota refills instead of counting a
    quarantined frame whose NINA original is still in the watch dir. Frames
    never QA'd still count. Empty with calibration_qa_mode=off."""
    try:
        from photonscript.scheduler import calibration_qa as cq
        if cq.mode(config) == "off":
            return set()
        out = set()
        for rig in ("rc16", "piggyback"):
            for k, r in cq.load_store(config, rig)["frames"].items():
                if r.get("verdict") == "fail":
                    out.add(k)
        return out
    except Exception:  # noqa: BLE001 - never break the quota over QA
        return set()


def days_since_last_bias(config, rig: str = "rc16") -> int | None:
    """Age (in days) of the newest night folder holding BIAS frames, or None
    if the library has no bias at all. Lightweight: matches on the BIAS
    directory name, plus (PS-128) one header per session for the readout
    mode: a session shot at another readout than the rig's lights (the
    RC16's LCG bias of July vs its HCG lights) does not count. A frame with
    no readout keyword, or an unreadable one, counts as the rig's."""
    from photonscript.shared.rigs import rig_readout
    want = rig_readout(config, rig)
    sessions: dict[str, Path] = {}
    for typ, date, f in iter_calibration_frames(config):
        if typ == "BIAS":
            sessions.setdefault(date, f)
    newest: str | None = None
    for date in sorted(sessions, reverse=True):
        if want and _session_readout(sessions[date], want) != want:
            continue
        newest = date
        break
    if newest is None:
        return None
    return (datetime.now().date()
            - datetime.strptime(newest, "%Y-%m-%d").date()).days


def _session_readout(f: Path, default: str | None) -> str | None:
    """PS-128: readout mode of one frame (its session's), the default when
    the header has none or cannot be read."""
    try:
        from astropy.io import fits as _fits
        from photonscript.shared.rigs import header_readout
        return header_readout(_fits.getheader(f))[0] or default
    except Exception:  # noqa: BLE001
        return default


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
    _fg, _fo = (_pb_gain_offset(config) if osc
                else (config.default_gain, config.default_offset))
    flat_blocks = ([_osc_sky_flat(n, _fg, _fo)]
                   if osc else
                   [_sky_flat(f, n, _fg, _fo) for f in filters])
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
    from photonscript.scheduler.nina_sequence_json import link_parents
    return (_json.dumps(link_parents(root), indent=2),
            local.strftime("%H:%M"))


def _osc_dark_blocks(config, dawn_provider="DawnProvider", dawn_offset=0,
                     gated=True):
    """OSC dark blocks for the piggyback, capped by its own library quota
    (count_matching_darks keys off the piggyback config's library_dir + OSC
    gain/offset/setpoint). Each block exits on the per-exposure count, the time
    cap (dawn_provider), and — when ``gated`` — LoopWhileUnsafe clearing.

    ``gated=False`` drops the LoopWhileUnsafe guard so the darks fire even when
    NINA #2 can't see the safety monitor (the caller then caps the time window
    at dusk so they run in the roof-closed pre-dark window; frames that catch a
    just-opened roof are rejected by QA — accepted trade-off vs. no OSC darks)."""
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _time_condition)
    quota = int(getattr(config, "dark_target_count", 30))
    pb_gain, pb_offset = _pb_gain_offset(config)
    blocks = []
    # PS-122: same lengths and count as the Calibration owed view
    for exp_s in quota_exposures(config, "piggyback"):
        try:
            need = dark_quota(config, "piggyback", exp_s)["need"]
        except Exception:  # noqa: BLE001
            need = quota
        if need == 0:
            continue
        blocks.append(_seq_container(
            f"OSC DARKS_{exp_s:.0f}s (need {need} of {quota})",
            [_make_typed(
                "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
                "NINA.Sequencer",
                ExposureTime=exp_s, Gain=pb_gain, Offset=pb_offset,
                Binning=_make_typed(
                    "NINA.Core.Model.Equipment.BinningMode, NINA.Core", X=1, Y=1),
                ImageType="DARK", ExposureCount=0, ErrorBehavior=0, Attempts=1)],
            conditions=(
                [_make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                             "NINA.Sequencer")] if gated else [])
                + [_time_condition(dawn_provider, dawn_offset),
                   _make_typed("NINA.Sequencer.Conditions.LoopCondition, "
                               "NINA.Sequencer",
                               CompletedIterations=0, Iterations=need)]))
    return blocks


def _wait_safe_until(provider: str, minutes_offset: int = 0,
                     name: str = "WAIT_SAFE_OR_TIME", step_s: int = 30) -> dict:
    """Bounded WaitUntilSafe: loop a short WaitForTimeSpan while the roof is
    UNSAFE and the provider time has not passed, so it exits on safe OR time.

    The core WaitUntilSafe has no timeout, and a parent TimeCondition does not
    pull a running instruction out: on 2026-09-26 the roof closed for clouds at
    11:39Z and the companion sat in the light loop's WaitUntilSafe until NINA #2
    was restarted at 13:04Z, never reaching its dawn flats (PS-36). Same
    LoopWhileUnsafe + TimeCondition pattern the RC16 uses for its unsafe darks.
    Skipped outright (no wait) when the roof is already safe."""
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _time_condition, _wait_for_timespan)
    return _seq_container(
        name, [_wait_for_timespan(step_s)],
        conditions=[_make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                                "NINA.Sequencer"),
                    _time_condition(provider, minutes_offset)])


def _osc_af_triggers(config) -> list:
    """Refocus triggers for the OSC light loop (PS-68), same NINA trigger JSON
    as the RC16 target containers: focuser-temperature change, HFR rise over
    the post-AF baseline, and a periodic AF. The periodic AF stands in for
    "after meridian flip" (NINA #2 has no mount, so it can't see the RC16's
    flip) and repairs a bad AF, which the HFR trigger can't: it baselines on
    the last AF (2026-09-26: after a 7-min gap in OSC subs, likely an AF, that
    overlapped the RC16's 04:00Z target change, the OSC sat near 6 px HFR for
    five hours)."""
    from photonscript.scheduler.nina_sequence_json import (
        _autofocus_hfr_trigger, _autofocus_temp_trigger, _autofocus_time_trigger)
    temp_c = float(getattr(config, "piggyback_af_temp_change_c", 1.5))
    hfr_pct = float(getattr(config, "piggyback_af_hfr_increase_pct", 10.0))
    every_min = int(getattr(config, "piggyback_af_interval_min", 60))
    trig = [_autofocus_temp_trigger(temp_c), _autofocus_hfr_trigger(hfr_pct, 4)]
    if every_min > 0:
        trig.append(_autofocus_time_trigger(every_min))
    return trig


def _osc_light_loop(config) -> dict:
    """Dumb OSC light loop for the piggyback (§4.3 DUAL_RIG): shoot continuous
    OSC lights while the roof is safe, until nautical dawn, resilient to cloud
    gaps. No slew/center/dither/guide (those belong to the RC16); the
    piggyback just rides the mount at its fixed offset. Exposure defaults to
    piggyback_exposure_s (120 s OSC — the piggyback is background-limited in
    seconds at 1.29"/px, so 120 s is set by star saturation, not read noise;
    see DUAL_RIG §4.5).

    Each pass of OSC_LIGHTS_UNTIL_DAWN: a BOUNDED wait for safe (exits at
    nautical dawn even if the roof stays shut), the resume hold (PS-25), a
    second bounded wait for safe, then a safety-gated, run-once image pass
    (seed + AF, then lights until unsafe or dawn). A closed roof at dawn
    therefore falls through to the dawn-flat step instead of wedging (PS-36).
    Refocus triggers: _osc_af_triggers (PS-68).

    Resume hold (PS-25): NINA #1 resumes only after safety_confirm_seconds of
    continuous safe, then unparks, slews, focuses and centers. NINA #2 used to
    go straight to AF and lights on a single safe poll, so on a flapping
    monitor it focused and shot while the mount was parked or moving. It now
    mirrors NINA #1 (wait safe, hold, wait safe) with a longer hold of
    safety_confirm_seconds + piggyback_resume_grace_s. The hold is short
    WaitForTimeSpan steps under a nautical-dawn TimeCondition, so it can not
    push the OSC past dawn or delay the dawn flats. The "roof open" Pushover
    is not in here (it would fire on every pass): see _osc_roof_open_notice.

    PS-61 cooler gate: when on (nina_sequence_json._cooler_gate_spec), the
    image pass starts with the cooler-gate ExternalScript. A SKIP (sensor
    still off setpoint after the timeout) interrupts the image pass, and
    OSC_LIGHTS_UNTIL_DAWN loops back through the waits and the resume hold
    to gate again, so the OSC never shoots off its setpoint and never wedges.

    PS-27 settle gate: when on (_osc_settle_gate), the settle-gate
    ExternalScript runs right before the light loop and right after every
    OSC light, so each light starts only after it, which holds (at most
    piggyback_settle_timeout_s) while the RC16 mount slews, has just moved,
    or PHD2 settles, so a sub starts on a still mount. ErrorBehavior 0 and
    the script always exits 0: it can delay a Piggy-600 light, never skip
    one, and never touches NINA #1."""
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _autofocus, _move_focuser,
        _safety_condition, _time_condition, _loop_once, _cooler_gate,
        _cooler_gate_spec)
    exp_s = float(getattr(config, "piggyback_exposure_s", 120.0))
    gain = int(getattr(config, "piggyback_default_gain", 100))
    offset = int(getattr(config, "piggyback_default_offset", 256))
    # Seed the OSC's OWN focuser to a known-good absolute position before the
    # first AF so it starts near focus, instead of AF failing to build an HFR
    # curve from a wild start and the rig imaging soft for hours (2026-09-20).
    # This is a different EAF than the RC16's, so the RC16 focus_seeds table
    # can't be reused. piggyback_seed_for() self-harvests the last few nights'
    # sharp OSC frames and falls back to the static piggyback_focus_seed; 0 =
    # disabled (no history yet and no static seed set).
    from photonscript.scheduler.piggyback_focus import piggyback_seed_for
    focus_seed = piggyback_seed_for(config)
    pre_af = [_move_focuser(focus_seed)] if focus_seed > 0 else []
    take = _make_typed(
        "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer",
        ExposureTime=exp_s, Gain=gain, Offset=offset,
        Binning=_make_typed("NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                            X=1, Y=1),
        ImageType="LIGHT", ExposureCount=0, ErrorBehavior=0, Attempts=1)
    dawn = ("NauticalDawnProvider", 0)
    # Inner loop: repeat exposures WHILE safe and before dawn (checked between
    # exposures, so the loop ends on its own at dawn); triggers refocus.
    # PS-27: the settle gate sits AFTER each light (and once before the
    # loop, below), so every light still starts on a still mount, while the
    # loop's dawn TimeCondition sees the TakeExposure as the next item: with
    # the gate first, the 0 s gate would pass the dawn check and the loop
    # would spin gate, gate, gate through the last exposure-length of night.
    settle = _osc_settle_gate(config)
    inner = _seq_container(
        OSC_LIGHT_LOOP_NAME, [take, *settle],
        conditions=[_safety_condition(), _time_condition(*dawn)],
        triggers=_osc_af_triggers(config))
    # config is the Piggy-600 view (rig_config), so camera_setpoint_c is the
    # piggyback setpoint the Start area cooled to
    gate = _cooler_gate_spec(config, float(getattr(config, "camera_setpoint_c", 0.0)))
    gate_items = [_cooler_gate(gate, "piggyback", "OSC lights")] if gate else []
    image_pass = _seq_container(
        OSC_IMAGE_PASS_NAME, [*gate_items, *pre_af, _autofocus(), *settle, inner],
        conditions=[_safety_condition(), _loop_once(), _time_condition(*dawn)])
    return _seq_container(
        OSC_LIGHTS_UNTIL_DAWN_NAME,
        [_wait_safe_until(*dawn, name="WAIT_SAFE_OR_NAUTICAL_DAWN"),
         _osc_resume_hold(config, *dawn),
         _wait_safe_until(*dawn, name=OSC_WAIT_SAFE_CONFIRM_NAME),
         image_pass],
        conditions=[_time_condition(*dawn)])


def _osc_settle_gate(config) -> list:
    """PS-27: [the settle-gate ExternalScript] when the gate is on and its
    script exists on this machine, else []. ErrorBehavior 0, Attempts 1."""
    from photonscript.scheduler.nina_sequence_json import _external_script
    from photonscript.scheduler.split_guard import gate_script
    path = gate_script(config)
    if not path:
        return []
    exp_s = float(getattr(config, "piggyback_exposure_s", 120.0))
    return [_external_script(path, f"--label=\"OSC {exp_s:g}s\"")]


def _osc_settle_gate_missing_notice(config) -> list:
    """Gate on but no script here: say so in the sequence (lint warns too)."""
    from photonscript.scheduler.nina_sequence_json import _annotation
    from photonscript.scheduler.split_guard import gate_enabled, gate_script
    if not gate_enabled(config) or gate_script(config):
        return []
    path = str(getattr(config, "piggyback_settle_script", "") or "")
    return [_annotation(f"settle gate OFF tonight: script {path or '(unset)'} "
                        "not found, so OSC lights do not wait for a still "
                        "mount (PS-27)")]


def _osc_resume_hold_seconds(config) -> int:
    """NINA #2's post-safe hold (PS-25): NINA #1's confirm hold plus a grace
    for its unpark, slew, AF and center."""
    return (max(0, int(getattr(config, "safety_confirm_seconds", 120)))
            + max(0, int(getattr(config, "piggyback_resume_grace_s", 300))))


def _osc_resume_hold(config, provider: str, minutes_offset: int = 0,
                     step_s: int = 30) -> dict:
    """The PS-25 resume hold as LoopCondition(n) x WaitForTimeSpan(step_s)
    under a TimeCondition, so it ends at the provider time (nautical dawn)
    within one step instead of overrunning it (a parent TimeCondition does not
    pull a running WaitForTimeSpan out). NINA resets the loop counter when the
    parent loops, so every safe re-entry gets the full hold."""
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _time_condition, _wait_for_timespan)
    total = _osc_resume_hold_seconds(config)
    steps = -(-total // step_s)  # ceil
    return _seq_container(
        f"{OSC_RESUME_HOLD_NAME}_{total}s",
        [_wait_for_timespan(step_s)],
        conditions=[_make_typed("NINA.Sequencer.Conditions.LoopCondition, "
                                "NINA.Sequencer",
                                CompletedIterations=0, Iterations=steps),
                    _time_condition(provider, minutes_offset)])


def _osc_roof_open_notice(config) -> list:
    """The "roof open, OSC lights" Pushover, once per night (PS-25). It sits
    before OSC_LIGHTS_UNTIL_DAWN, not inside it, because that container loops
    and resets its children on every safe re-entry. A bounded wait for safe
    (exits at nautical dawn), then a run-once notice that is skipped if the
    roof is still closed at dawn."""
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _safety_condition, _time_condition, _loop_once,
        _pushover)
    exp_s = float(getattr(config, "piggyback_exposure_s", 120.0))
    hold = _osc_resume_hold_seconds(config)
    dawn = ("NauticalDawnProvider", 0)
    return [
        _wait_safe_until(*dawn, name="WAIT_SAFE_FOR_FIRST_OSC_LIGHTS"),
        _seq_container(
            OSC_ROOF_OPEN_NOTICE_NAME,
            [_pushover("Piggyback", f"roof open: OSC lights {exp_s:g}s until "
                       f"nautical dawn, after a {hold} s resume hold")],
            conditions=[_safety_condition(), _loop_once(),
                        _time_condition(*dawn)]),
    ]


def generate_piggyback_companion_json(config, has_safety: bool = False,
                                      with_lights: bool = False) -> str:
    """A full-night companion for the piggyback (NINA #2), meant to
    be dispatched alongside the RC16 armed sequence so ONE arm covers both
    scopes' calibration.

    The piggyback owns only its camera + focuser and rides the RC16 mount, so
    this sequence never slews, unparks or parks. It:
      * cools the OSC camera ~cool_lead before astro dark,
      * (has_safety) fills OSC darks to quota + a 50-bias top-up while the roof
        is CLOSED (LoopWhileUnsafe) — needs the SHARED safety monitor connected
        in the NINA #2 profile, since NINA #2 can't otherwise tell roof state,
      * at nautical dawn +5 (+90 s for the RC16's dawn slew) shoots one OSC
        sky-flat set of piggyback_flat_count at the OSC gain/offset, then warms.
        With the safety monitor the flats wait (bounded) for safe until
        nautical dawn + piggyback_flat_wait_min and are skipped, not wedged,
        if the roof stays closed. The armer's dawn shutdown waits for this
        window (dawn_flats_window_min, PS-36).

    has_safety=False (no safety monitor on NINA #2): darks/bias fire ungated
    (time-capped at dusk) with an annotation, and the dawn flats fire
    time-gated only. Returns the sequence JSON text.
    """
    import json as _json
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _pushover, _connect, _cool_camera,
        _warm_camera, _dew_heater, _wait_for_provider, _wait_for_timespan,
        _annotation, _safety_condition, _loop_once)

    # OSC flats count is its own knob (PS-36: 20-30 wanted; flat_count is the
    # RC16's per-filter count).
    n = int(getattr(config, "piggyback_flat_count",
                    getattr(config, "flat_count", 15)))
    flat_wait = int(getattr(config, "piggyback_flat_wait_min", 25))
    cool_lead = int(getattr(config, "cool_lead_minutes", 30))
    setpoint = float(getattr(config, "camera_setpoint_c", 0.0))

    start_items = [
        _pushover("Piggyback", "piggyback armed: cools the OSC camera, "
                  + ("fills darks/bias while the roof is closed, " if has_safety
                     else "fills darks/bias (unconditional — no safety monitor), ")
                  + ("shoots OSC lights while the roof is open, "
                     if (with_lights and has_safety) else "")
                  + "then shoots OSC dawn flats. No mount control — rides the RC16."),
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
    # Gate roof-closed capture on the safety monitor when NINA #2 has it. Without
    # it, fire darks/bias ANYWAY (Jeremy's call 2026-09-25): the OSC otherwise
    # ends up with zero matching calibration. Ungated darks are time-capped at
    # astro dusk so they run in the roof-closed pre-dark window; any frame that
    # catches a just-opened roof is rejected by QA. Bias is 0.001s (light-safe).
    gated = has_safety
    if not gated:
        target_items.append(_annotation(
            "OSC darks/bias running UNCONDITIONALLY: NINA #2 can't see the safety "
            "monitor, so these are time-capped at dusk instead of roof-gated. Add "
            "the shared safety monitor to the NINA #2 profile to roof-gate them."))
    dark_blocks = _osc_dark_blocks(
        config, dawn_provider=("DawnProvider" if gated else "DuskProvider"),
        dawn_offset=0, gated=gated)
    if dark_blocks:
        _dark_conds = ([_make_typed(
            "NINA.Sequencer.Conditions.LoopWhileUnsafe, NINA.Sequencer")]
            if gated else [])
        _dark_conds.append(_make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=1))
        target_items.append(_seq_container(
            "OSC_DARKS" + ("_IF_UNSAFE" if gated else "_UNCONDITIONAL"),
            dark_blocks, conditions=_dark_conds))
    # bias top-up only when due (bias barely ages)
    _bias_refresh_days = int(getattr(config, "bias_refresh_days", 60))
    try:
        _bias_age = days_since_last_bias(config, rig="piggyback")
    except Exception:  # noqa: BLE001
        _bias_age = None
    _bias_due = (_bias_refresh_days <= 0 or _bias_age is None
                 or _bias_age >= _bias_refresh_days)
    if _bias_due:
        _bias_conds = ([_make_typed(
            "NINA.Sequencer.Conditions.LoopWhileUnsafe, NINA.Sequencer")]
            if gated else [])
        _bias_conds.append(_make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=1))
        target_items.append(_seq_container(
            "OSC_BIAS" + ("_IF_UNSAFE" if gated else "_UNCONDITIONAL"),
            [_seq_container("50 bias", [_make_typed(
                "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
                "NINA.Sequencer",
                ExposureTime=0.001, Gain=_pb_gain_offset(config)[0],
                Offset=_pb_gain_offset(config)[1],
                Binning=_make_typed(
                    "NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                    X=1, Y=1),
                ImageType="BIAS", ExposureCount=0,
                ErrorBehavior=0, Attempts=1)],
                conditions=[_make_typed(
                    "NINA.Sequencer.Conditions.LoopCondition, "
                    "NINA.Sequencer",
                    CompletedIterations=0, Iterations=50)])],
            conditions=_bias_conds))
    if has_safety and with_lights:
        # Shoot OSC lights while the roof is open until nautical dawn. The loop
        # waits for safe itself (bounded), so a roof that never opens, or
        # closes before dawn, falls through to the dawn-flat window below
        # instead of wedging in an unbounded WaitUntilSafe (PS-36, 2026-09-26).
        target_items += _osc_roof_open_notice(config)
        target_items.append(_osc_light_loop(config))
        from photonscript.scheduler.nina_sequence_json import (
            _cooler_gate_spec, _cooler_gate_missing_notice)
        if _cooler_gate_spec(config, setpoint) is None:
            start_items += _cooler_gate_missing_notice(config)   # PS-61
        start_items += _osc_settle_gate_missing_notice(config)   # PS-27

    # Dawn flats: the RC16's flat window opens at nautical dawn +5 (its End
    # area slews to alt 85 / az 200); give that slew 90 s to land, then one OSC
    # set. With the safety monitor: wait (bounded) for safe, and skip the flats
    # if the roof is still closed (closed roof = junk flats).
    target_items.append(_wait_for_provider("NauticalDawnProvider", 5))
    target_items.append(_wait_for_timespan(90))
    flat_items = [
        _pushover("Piggyback", f"dawn flat window — shooting {n} OSC sky flats "
                  "(riding the RC16 slew)"),
        _osc_sky_flat(n, *_pb_gain_offset(config)),
        _pushover("Piggyback", "OSC dawn flats complete — warming"),
    ]
    if has_safety:
        target_items.append(_wait_safe_until(
            "NauticalDawnProvider", flat_wait, name="WAIT_SAFE_FOR_OSC_FLATS"))
        target_items.append(_seq_container(
            "DAWN_SKY_FLATS_OSC (skipped if unsafe: closed roof makes junk "
            "flats)", flat_items,
            conditions=[_safety_condition(), _loop_once()]))
    else:
        target_items += flat_items

    end_items = [_warm_camera(float(getattr(config, "gradual_warm_minutes", 0.0))),
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
    from photonscript.scheduler.nina_sequence_json import link_parents
    return _json.dumps(link_parents(root), indent=2)
