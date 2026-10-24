"""PS-189: the Piggy-600's Wanderer Cover V4-EC (dust cover + EL flat panel).

The cover is a NINA "Flat Panel" (ASCOM CoverCalibrator) on NINA #2. With
piggyback_cover_enabled (default off until it is installed) the companion
(calibration.generate_piggyback_companion_json):

  Start    connect the Flat Panel, panel light off, cover CLOSED (the darks
           and bias are light-tight whatever the roof does)
  darks    OSC darks / bias with the cover closed: no roof gate needed
  lights   every OSC image pass starts with OpenCover (ErrorBehavior 1: a
           failed open skips the pass) and the cover gate ExternalScript
           (deploy\\cover-gate.cmd -> photonscript cover-gate -> POST
           /api/cover/gate -> run_gate here), which reads ninaAPI flatdevice
           info until the cover reads Open, re-sends the open once, at most
           piggyback_cover_gate_timeout_s; otherwise SKIP (the pass is
           skipped, never a light with the cover closed) and one page a night.
           Each pass of the light loop first closes the cover while the roof
           reads unsafe (COVER_CLOSE_IF_UNSAFE), so a closed roof also means
           a closed cover; the next safe pass opens it again.
  flats    at astro dawn, PANEL_FLATS_OSC: cover closed, light on, panel
           brightness, piggyback_flat_count flats (auto: NINA's
           AutoExposureFlat seeks a 50% histogram like the Flat Wizard;
           fixed: piggyback_panel_flat_exposure_s), light off. No sky, no
           roof: replaces the PS-163 dawn sky flats.
  End      light off, cover closed (closed by day)

The armer closes it too: dawn shutdown, PS-77 unsafe stop, paused unsafe
park (armer._close_piggy_cover). calibration_capture counts a closed cover
(light off) as light-tight for a Piggy-600 job, so daytime darks no longer
need the roof closed (the PS-113 daytime probe and leak checks still run).

LIVE holds the gate state per rig for the dashboard Cover row (row_note).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

EXIT_SKIP = 3                    # CLI exit code for a deliberate skip
GATE_SCRIPT_TOKEN = "cover-gate"  # file-name token the lint rule looks for
LOG_NAME = "cover_gate.jsonl"
LIVE_STALE_S = 300
SKIP_NOTE_S = 3600
UNREADABLE_POLLS = 3

COVER_CLOSE_IF_UNSAFE_NAME = "COVER_CLOSE_IF_UNSAFE"
COVER_OPEN_NAME = "OPEN_COVER"
PANEL_FLATS_NAME = "PANEL_FLATS_OSC"
FLAT_PANEL_DEVICE = "Flat Panel"   # NINA ConnectEquipment device label

_FD = "NINA.Sequencer.SequenceItem.FlatDevice."
OPEN_COVER_TYPE = _FD + "OpenCover, NINA.Sequencer"
CLOSE_COVER_TYPE = _FD + "CloseCover, NINA.Sequencer"
TOGGLE_LIGHT_TYPE = _FD + "ToggleLight, NINA.Sequencer"
SET_BRIGHTNESS_TYPE = _FD + "SetBrightness, NINA.Sequencer"
AUTO_EXPOSURE_FLAT_TYPE = _FD + "AutoExposureFlat, NINA.Sequencer"
PANEL_FLAT_MODES = ("auto", "fixed")

# 1 = SkipInstructionSetOnError (nina_sequence_json.COOLER_GATE_ERROR_BEHAVIOR)
SKIP_ON_ERROR = 1

# rig -> {"state": waiting|open|skipped, "cover", "label", "since", "updated",
#         "waited_s"} (epoch seconds)
LIVE: dict[str, dict] = {}
# rig -> night (YYYY-MM-DD) already paged for a skip, and whether to send a
# recovery line when the cover later opens
_PAGED: dict[str, dict] = {}


# --- config -----------------------------------------------------------------------

def enabled(cfg, rig: str = "piggyback") -> bool:
    from photonscript.shared.rigs import cover_enabled
    return cover_enabled(cfg, rig)


def panel_mode(cfg) -> str:
    m = str(getattr(cfg, "piggyback_panel_flat_mode", "auto") or "auto").strip().lower()
    return m if m in PANEL_FLAT_MODES else "auto"


def gate_script(cfg) -> str | None:
    """The cover-gate script path when the companion should carry the gate:
    cover enabled AND the file exists on this machine (the armer generates
    on the scope PC). Missing: no gate (OpenCover's own skip still guards,
    and the lint warns)."""
    if not getattr(cfg, "piggyback_cover_enabled", False):
        return None
    path = str(getattr(cfg, "piggyback_cover_script", "") or "").strip()
    if not path:
        return None
    try:
        return path if Path(path).is_file() else None
    except OSError:
        return None


def safe_label(text) -> str:
    s = re.sub(r"[^A-Za-z0-9 ._+\-]+", " ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60] or "OSC lights"


def script_args(rig: str, label: str) -> str:
    return f'{rig} --label="{safe_label(label)}"'


# --- sequence items ----------------------------------------------------------------

def open_cover(skip_on_fail: bool = True) -> dict:
    """NINA OpenCover. ErrorBehavior 1 inside an image pass: a cover that
    fails to open skips that pass instead of shooting dark lights."""
    from photonscript.scheduler.nina_sequence_json import _make_typed
    return _make_typed(OPEN_COVER_TYPE,
                       ErrorBehavior=SKIP_ON_ERROR if skip_on_fail else 0,
                       Attempts=1)


def close_cover() -> dict:
    from photonscript.scheduler.nina_sequence_json import _make_typed
    return _make_typed(CLOSE_COVER_TYPE, ErrorBehavior=0, Attempts=1)


def toggle_light(on: bool) -> dict:
    from photonscript.scheduler.nina_sequence_json import _make_typed
    return _make_typed(TOGGLE_LIGHT_TYPE, OnOff=bool(on), ErrorBehavior=0,
                       Attempts=1)


def set_brightness(value: int) -> dict:
    from photonscript.scheduler.nina_sequence_json import _make_typed
    return _make_typed(SET_BRIGHTNESS_TYPE, Brightness=int(value),
                       ErrorBehavior=0, Attempts=1)


def start_items() -> list:
    """Start area: connect the Flat Panel, light off, cover closed."""
    from photonscript.scheduler.nina_sequence_json import _connect
    return [_connect(FLAT_PANEL_DEVICE), toggle_light(False), close_cover()]


def end_items() -> list:
    """End area: light off, cover closed (closed by day)."""
    return [toggle_light(False), close_cover()]


def gate_item(cfg, rig: str, label: str) -> dict | None:
    """The cover-gate ExternalScript (ErrorBehavior 1: SKIP skips the image
    pass), or None when the script is missing here."""
    path = gate_script(cfg)
    if not path:
        return None
    from photonscript.scheduler.nina_sequence_json import _external_script
    item = _external_script(path, script_args(rig, label))
    item["ErrorBehavior"] = SKIP_ON_ERROR
    return item


def open_items(cfg, rig: str = "piggyback", label: str = "OSC lights") -> list:
    """What an OSC image pass starts with: OpenCover, then the gate."""
    g = gate_item(cfg, rig, label)
    return [open_cover()] + ([g] if g else [])


def gate_missing_notice(cfg) -> list:
    if not getattr(cfg, "piggyback_cover_enabled", False) or gate_script(cfg):
        return []
    from photonscript.scheduler.nina_sequence_json import _annotation
    path = str(getattr(cfg, "piggyback_cover_script", "") or "")
    return [_annotation(f"cover gate OFF tonight: script {path or '(unset)'} not "
                        "found, so only NINA's OpenCover guards the OSC lights "
                        "(PS-189)")]


def close_if_unsafe() -> dict:
    """Run once per light-loop pass: close the cover while the roof reads
    unsafe (LoopWhileUnsafe passes only when unsafe), so the cover is shut
    whenever the roof is. A no-op pass while safe."""
    from photonscript.scheduler.nina_sequence_json import (_seq_container,
                                                           _make_typed, _loop_once)
    return _seq_container(
        COVER_CLOSE_IF_UNSAFE_NAME, [close_cover()],
        conditions=[_make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                                "NINA.Sequencer"), _loop_once()])


def _flat_take(exp_s: float, gain: int, offset: int) -> dict:
    from photonscript.scheduler.nina_sequence_json import _make_typed
    return _make_typed(
        "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer",
        ExposureTime=float(exp_s), Gain=gain, Offset=offset,
        Binning=_make_typed("NINA.Core.Model.Equipment.BinningMode, NINA.Core",
                            X=1, Y=1),
        ImageType="FLAT", ExposureCount=0, ErrorBehavior=0, Attempts=1)


def _auto_exposure_flat(count: int, gain: int, offset: int, brightness: int,
                        max_s: float) -> dict:
    """NINA's AutoExposureFlat for a rig without a filter wheel. NINA finds
    its parts by position (NINA 3.x AutoExposureFlat.cs: Items[0] CloseCover,
    [1] ToggleLight on, [2] SwitchFilter, [3] SetBrightness, [4] the
    LoopCondition + TakeExposure container, [5] ToggleLight off, [6]
    OpenCover), so the order here is load-bearing. KeepPanelClosed true:
    the cover stays shut after the flats (closed by day). The SwitchFilter
    carries no filter (PS-132: validates without a wheel)."""
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _switch_filter_none)
    loop = _seq_container(
        f"{count} flats", [_flat_take(0.0, gain, offset)],
        conditions=[_make_typed(
            "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer",
            CompletedIterations=0, Iterations=count)])
    af = _seq_container(
        "Panel flats OSC (auto exposure)",
        [close_cover(), toggle_light(True), _switch_filter_none(),
         set_brightness(brightness), loop, toggle_light(False),
         open_cover(skip_on_fail=False)],
        container_type=AUTO_EXPOSURE_FLAT_TYPE)
    af["IsExpanded"] = False
    af.update(MinExposure=0.01, MaxExposure=float(max_s), Brightness=int(brightness),
              HistogramTargetPercentage=0.5, HistogramTolerancePercentage=0.1,
              KeepPanelClosed=True)
    return af


def panel_flats_block(cfg) -> dict:
    """PANEL_FLATS_OSC: run once, no roof or sky condition. cfg is the
    Piggy-600 view (rig_config): its OSC gain / offset."""
    from photonscript.scheduler.nina_sequence_json import (
        _seq_container, _make_typed, _loop_once, _pushover)
    n = int(getattr(cfg, "piggyback_flat_count", 25))
    gain = int(getattr(cfg, "piggyback_default_gain", 100))
    offset = int(getattr(cfg, "piggyback_default_offset", 256))
    bright = int(getattr(cfg, "piggyback_panel_brightness", 50))
    mode = panel_mode(cfg)
    if mode == "auto":
        body = [_auto_exposure_flat(n, gain, offset, bright,
                                    float(getattr(cfg, "piggyback_panel_flat_max_s", 10.0)))]
        how = f"auto exposure at brightness {bright}"
    else:
        exp_s = float(getattr(cfg, "piggyback_panel_flat_exposure_s", 1.0))
        shots = _seq_container(f"{n} flats", [_flat_take(exp_s, gain, offset)],
                               conditions=[_make_typed(
                                   "NINA.Sequencer.Conditions.LoopCondition, "
                                   "NINA.Sequencer",
                                   CompletedIterations=0, Iterations=n)])
        body = [close_cover(), toggle_light(True), set_brightness(bright), shots,
                toggle_light(False)]
        how = f"{exp_s:g} s at brightness {bright}"
    return _seq_container(
        PANEL_FLATS_NAME,
        [_pushover("Piggyback", f"panel flats: cover closed, {n} OSC flats ({how})")]
        + body
        + [_pushover("Piggyback", "OSC panel flats complete, cover closed")],
        conditions=[_loop_once()])


# --- the gate ----------------------------------------------------------------------

def _utc(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _night(ts: float) -> str:
    """The observing night a time belongs to (noon to noon UTC-7-ish: the
    UTC date 12 h earlier is good enough to page once a night)."""
    return (datetime.fromtimestamp(ts, timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%d")


def _log(cfg, rec: dict) -> None:
    try:
        d = Path(getattr(cfg, "data_dir", "") or ".")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / LOG_NAME, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning("cover gate log write failed: %s", e)


def recent(cfg, n: int = 20) -> list[dict]:
    try:
        p = Path(getattr(cfg, "data_dir", "") or ".") / LOG_NAME
        lines = p.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:  # noqa: BLE001
        return []


async def _read(cfg, rig: str):
    from photonscript.shared.rigs import rig_config, nina_flatdevice_info
    return await nina_flatdevice_info(rig_config(cfg, rig).nina_base_url)


async def _command(cfg, rig: str, command: str) -> dict:
    from photonscript.shared.rigs import rig_config, nina_flatdevice
    return await nina_flatdevice(rig_config(cfg, rig).nina_base_url, command)


async def run_gate(cfg, rig: str = "piggyback", label: str = "", *, read=None,
                   command=None, sleep=asyncio.sleep, clock=time.time,
                   notify_fn=None, disconnected=None) -> dict:
    """Hold until the rig's cover reads Open (panel light off), at most
    piggyback_cover_gate_timeout_s. Returns {"verdict": PASS | SKIP | OFF |
    ABORTED, "cover", "waited_s", "reason"}. Only SKIP stops the image pass.
    Unlike the cooler gate an unreadable cover does NOT pass: with the cover
    enabled, "cannot tell" most likely means closed, and a closed cover
    makes every light a dark. read(cfg, rig) -> info dict or None,
    command(cfg, rig, name) -> {ok}, notify_fn and disconnected() are
    injectable for tests."""
    from photonscript.shared.rigs import cover_state, rig_label
    label = safe_label(label)
    base = {"rig": rig, "label": label}
    if not enabled(cfg, rig):
        return {**base, "verdict": "OFF", "cover": None, "waited_s": 0.0,
                "reason": "piggyback_cover_enabled is off"}
    read = read or _read
    command = command or _command
    if notify_fn is None:
        from photonscript.shared.pushover import notify as notify_fn
    timeout_s = max(0.0, float(getattr(cfg, "piggyback_cover_gate_timeout_s", 180.0)))
    poll_s = max(0.5, float(getattr(cfg, "piggyback_cover_gate_poll_s", 5.0)))
    name = rig_label(cfg, rig)
    t0 = clock()
    sent: set = set()
    state = None
    misses = 0
    while True:
        if disconnected is not None:
            try:
                if await disconnected():
                    LIVE.pop(rig, None)
                    return {**base, "verdict": "ABORTED", "cover": state,
                            "waited_s": round(clock() - t0, 1),
                            "reason": "NINA cancelled the gate"}
            except Exception:  # noqa: BLE001
                pass
        try:
            info = await read(cfg, rig)
        except Exception as e:  # noqa: BLE001
            logger.warning("cover gate read failed (%s): %s", rig, e)
            info = None
        now = clock()
        waited = now - t0
        connected = bool((info or {}).get("Connected", info is not None))
        state = cover_state(info) if (info is not None and connected) else None
        light = (info or {}).get("LightOn")
        if state == "open" and light is not True:
            LIVE[rig] = {"state": "open", "cover": state, "label": label,
                         "since": now, "updated": now, "waited_s": round(waited, 1)}
            paged = _PAGED.get(rig) or {}
            if paged.get("recover"):
                paged["recover"] = False
                try:
                    await notify_fn(cfg, f"{name}: cover open again, imaging "
                                    f"{label} resumed", title="PhotonScript cover", kind="info")
                except Exception as e:  # noqa: BLE001
                    logger.warning("cover gate notify failed: %s", e)
            res = {**base, "verdict": "PASS", "cover": state,
                   "waited_s": round(waited, 1), "reason": "cover open"}
            if waited >= 1:
                _log(cfg, {"t_utc": _utc(now), **res})
            return res
        # nudge the device once per kind: connect when unreadable, light off
        # when the panel is lit, open when it is not opening by itself
        try:
            if state is None:
                misses += 1
                if "connect" not in sent and misses >= 1:
                    sent.add("connect")
                    await command(cfg, rig, "connect")
            elif light is True and "light_off" not in sent:
                sent.add("light_off")
                await command(cfg, rig, "light_off")
            elif state in ("closed", "unknown", "error") and "open" not in sent:
                sent.add("open")
                await command(cfg, rig, "open")
        except Exception as e:  # noqa: BLE001
            logger.warning("cover gate command failed (%s): %s", rig, e)
        prev = LIVE.get(rig) or {}
        LIVE[rig] = {"state": "waiting", "cover": state or "unreadable",
                     "label": label,
                     "since": prev.get("since") if prev.get("state") == "waiting" else t0,
                     "updated": now, "waited_s": round(waited, 1)}
        if waited >= timeout_s:
            what = state or "unreadable"
            night = _night(now)
            paged = _PAGED.setdefault(rig, {"night": None, "recover": False})
            if paged.get("night") != night:
                paged.update(night=night, recover=True)
                try:
                    await notify_fn(
                        cfg, f"{name}: not imaging {label}, the cover reads "
                        f"{what} after {waited:.0f} s (it must read Open). The "
                        "image pass is skipped and retried on the next pass; "
                        "check the Wanderer Cover and NINA #2's Flat Panel. "
                        "One alert per night.",
                        title="PhotonScript cover", priority=1, kind="act")
                except Exception as e:  # noqa: BLE001
                    logger.warning("cover gate notify failed: %s", e)
            LIVE[rig] = {"state": "skipped", "cover": what, "label": label,
                         "since": now, "updated": now, "waited_s": round(waited, 1)}
            res = {**base, "verdict": "SKIP", "cover": what,
                   "waited_s": round(waited, 1),
                   "reason": f"cover reads {what} after {waited:.0f} s"}
            _log(cfg, {"t_utc": _utc(now), **res})
            logger.warning("cover gate SKIP on %s (%s): %s", rig, label, res["reason"])
            return res
        await sleep(min(poll_s, max(0.5, timeout_s - waited)))


def row_note(rig: str, now: float | None = None) -> str | None:
    """The dashboard Cover-row line (or None): a waiting gate or a recent
    skip."""
    now = time.time() if now is None else now
    live = LIVE.get(rig) or {}
    st = live.get("state")
    if st == "waiting" and now - float(live.get("updated", 0)) <= LIVE_STALE_S:
        return (f"waiting for the cover to open (reads {live.get('cover')}, "
                f"{(now - float(live.get('since', now))):.0f} s, lights held)")
    if st == "skipped" and now - float(live.get("updated", 0)) <= SKIP_NOTE_S:
        return (f"not imaging: cover read {live.get('cover')} after "
                f"{float(live.get('waited_s', 0)):.0f} s ({live.get('label')})")
    return None
