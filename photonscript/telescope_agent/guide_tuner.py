"""Guide-star auto-tune, the live half (PS-90). RC16 agent only.

Goal: a real guide star whose peak sits at about 60 to 80% of full scale
(bright, but never clipped: a flat top degrades the centroid and PHD2 flags
it as ErrorCode 1), SNR over 20 and HFD about 2 to 5 px, with the guide
exposure inside 1 to 4 s. The 09-26 night guided 8-bit, gain 100 and auto
exposure, so every star read "saturated" and the centroids were poor.

Pure helpers (tested offline):

    star_stats(reply, full_scale, error_code)   PHD2 get_star_image crop ->
        background (border median), peak and star amplitude as fractions of
        full scale, clipped (any pixel at 98% of full scale, or ErrorCode 1),
        HFD, an SNR estimate, the central-pixel share (PS-91 guide_guard
        peak_fraction: a hot pixel puts most of its flux in one pixel) and
        an 8-bit flag (the crop never goes over 255).
    decide_exposure(readings, cur_ms, durations, cfg, darks)
        aim at the middle of the band (70%): step down at once on a clipped
        reading, otherwise only after 3 consecutive readings outside the
        hysteresis band (55 to 85%); the new exposure is a linear prediction
        on the star amplitude, snapped down (never toward a clip) to PHD2's
        own exposure list, clamped to
        phd2_tune_exp_ms and limited to exposures the PHD2 dark library has
        (PS-89 inventory). Pinned at a bound = "faint" or "bright".

GuideStarTuner listens to the RC16 agent's PHD2 client:

  * SettleDone (once a target is guided, after every dither) and a filter
    change: measure 5 frames (get_star_image plus the GuideStep SNR / HFD /
    ErrorCode the client keeps) and record them (scheduler.phd2_tuning).
  * phd2_tune_mode observe (the default): that is all. Nothing is set.
  * phd2_tune_mode exposure: decide_exposure, and on a change set_exposure
    under phd2_ops.hold("tuner") after re-checking that PHD2 is still
    Guiding. On a filter change the remembered (or predicted) exposure for
    that target and filter is applied first, then re-verified.
  * Never acts (not even a measurement) while PHD2 is not Guiding (so never
    while Calibrating), while settling, while another PhotonScript actor
    holds PHD2 (the PS-91 guard recovery, the PS-92 self-test, the PS-93
    retry, a PS-89 apply, the hot-pixel map), inside the PS-93 calibration
    slot, while the PS-91 guard has a non-star episode open, or while the
    mount slews or flips.

Gain and binning are not settable over PHD2's API: scheduler.phd2_tuning
recommends them for the next night and the armer writes them pre-dusk
through the PS-89 profile writer (only with phd2_audit_autofix).
Event listeners never await PHD2 (they run inside the socket reader): work
is scheduled as tasks.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import statistics
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MODES = ("off", "observe", "exposure")
CLIP_FRAC = 0.98          # a pixel at this share of full scale is clipped
HYST = 0.05               # hysteresis: act only outside lo - HYST .. hi + HYST
OUT_OF_BAND_N = 3         # consecutive out-of-band readings before a change
MEASURE_FRAMES = 5        # frames per measurement
MIN_GAP_S = 120.0         # at most one measurement per this many seconds
FRAME_WAIT_S = 30.0       # wait for one guide frame at most this long
BIT8_MAX = 255
SNR_FLOOR = 10.0          # still guided above this; under it PS-85 territory
STAR_SHARE_MAX = 0.5      # PS-91 D1: over half the 5x5 flux in one pixel


def _pair(text, default: tuple[float, float]) -> tuple[float, float]:
    try:
        a, b = (float(x) for x in str(text).split(",")[:2])
        return (min(a, b), max(a, b))
    except (TypeError, ValueError):
        return default


@dataclass
class TuneCfg:
    peak_lo: float = 0.60
    peak_hi: float = 0.80
    snr_min: float = 20.0
    hfd_lo: float = 2.0
    hfd_hi: float = 5.0
    exp_lo: int = 1000
    exp_hi: int = 4000
    full_scale: int = 65535

    @property
    def target(self) -> float:
        return (self.peak_lo + self.peak_hi) / 2.0

    @property
    def hyst_lo(self) -> float:
        return self.peak_lo - HYST

    @property
    def hyst_hi(self) -> float:
        return self.peak_hi + HYST


NB_FILTERS = ("Ha", "OIII", "SII")   # 3 nm: the OAG behind the wheel sees little


def tune_cfg(config, filt: str | None = None) -> TuneCfg:
    """The tuner's band. PS-85: on a 3 nm filter (Ha / OIII / SII) the
    exposure band is phd2_tune_exp_ms_nb (default 1 to 8 s) when set, so a
    faint narrowband guide star gets longer exposures before PS-85 calls the
    block not viable."""
    hfd = _pair(getattr(config, "phd2_tune_hfd_px", "2,5"), (2.0, 5.0))
    exp = _pair(getattr(config, "phd2_tune_exp_ms", "1000,4000"), (1000.0, 4000.0))
    if str(filt or "") in NB_FILTERS:
        nb = str(getattr(config, "phd2_tune_exp_ms_nb", "") or "").strip()
        if nb:
            exp = _pair(nb, exp)
    return TuneCfg(peak_lo=float(getattr(config, "phd2_tune_peak_lo", 0.60) or 0.60),
                   peak_hi=float(getattr(config, "phd2_tune_peak_hi", 0.80) or 0.80),
                   snr_min=float(getattr(config, "phd2_tune_snr_min", 20.0) or 20.0),
                   hfd_lo=hfd[0], hfd_hi=hfd[1], exp_lo=int(exp[0]), exp_hi=int(exp[1]),
                   full_scale=int(getattr(config, "phd2_guide_full_scale_adu", 65535)
                                  or 65535))


def tune_mode(config) -> str:
    m = str(getattr(config, "phd2_tune_mode", "observe") or "observe").strip().lower()
    return m if m in MODES else "observe"


# ----------------------------------------------------------------- pure

def star_stats(reply: dict, full_scale: int = 65535,
               error_code: int | None = None) -> dict | None:
    """One get_star_image crop measured. None when the reply is unusable."""
    import numpy as np
    from photonscript.telescope_agent.guide_guard import peak_fraction
    try:
        w, h = int(reply["width"]), int(reply["height"])
        px = np.frombuffer(base64.b64decode(reply["pixels"]), dtype="<u2")
        img = px[: w * h].reshape(h, w).astype(float)
    except (KeyError, TypeError, ValueError):
        return None
    if w < 5 or h < 5:
        return None
    fs = float(full_scale or 65535)
    border = np.concatenate([img[:2].ravel(), img[-2:].ravel(),
                             img[2:-2, :2].ravel(), img[2:-2, -2:].ravel()])
    bkg = float(np.median(border))
    noise = float(np.median(np.abs(border - bkg))) * 1.4826 or 1.0
    peak = float(img.max())
    sub = np.clip(img - bkg, 0, None)
    tot = float(sub.sum())
    sx, sy = reply.get("star_pos") or (w / 2, h / 2)
    try:
        sx, sy = float(sx), float(sy)
    except (TypeError, ValueError):
        sx, sy = w / 2, h / 2
    yy, xx = np.mgrid[0:h, 0:w]
    if tot > 0:
        cx, cy = float((sub * xx).sum() / tot), float((sub * yy).sum() / tot)
    else:
        cx, cy = sx, sy
    r = np.hypot(xx - cx, yy - cy)
    inside = r <= min(w, h) / 2.0
    flux = float(sub[inside].sum())
    hfd = 2.0 * float((sub * r)[inside].sum()) / flux if flux > 0 else None
    sig = sub > 3 * noise
    npix = int(max(1, (sig & inside).sum()))
    snr = float(sub[sig & inside].sum()) / (noise * math.sqrt(npix)) if sig.any() else 0.0
    clipped = peak >= CLIP_FRAC * fs or error_code == 1
    share = peak_fraction(reply)
    return {"peak_adu": round(peak, 1), "bkg_adu": round(bkg, 1),
            "peak_frac": round(peak / fs, 4),
            "amp_frac": round(max(0.0, peak - bkg) / fs, 4),
            "clipped": bool(clipped), "bit8": bool(peak <= BIT8_MAX),
            "hfd_px": round(hfd, 2) if hfd else None, "snr_est": round(snr, 1),
            "center_share": round(share, 3) if share is not None else None,
            "width": w, "height": h}


def _snap(want: float, durations, cfg: TuneCfg, darks) -> list[int]:
    ok = sorted({int(d) for d in durations or []
                 if cfg.exp_lo <= int(d) <= cfg.exp_hi})
    if darks is not None:
        have = {int(d) for d in darks}
        ok = [d for d in ok if d in have]
    return ok


def decide_exposure(readings: list[dict], cur_ms: int | None, durations,
                    cfg: TuneCfg, darks=None) -> dict:
    """The next guide exposure from the latest readings (oldest first; each
    a star_stats dict). darks: exposures (ms) the PHD2 dark library holds,
    or None when unknown (then no change is made at all: a guide exposure
    without a matching dark would bring the hot pixels back).

    {"action": "set" | "hold", "ms", "want_ms", "reason", "pinned":
     None | "faint" | "bright", "in_band"}"""
    out = {"action": "hold", "ms": cur_ms, "want_ms": None, "reason": "",
           "pinned": None, "in_band": None}
    if not readings or not cur_ms:
        out["reason"] = "no readings" if not readings else "current exposure unknown"
        return out
    last = readings[-1]
    if any(r.get("bit8") for r in readings[-OUT_OF_BAND_N:]):
        out["reason"] = ("star never above 255 ADU: the guide camera is 8-bit "
                         "(PS-89 bit-depth row); no exposure change")
        return out
    if (last.get("center_share") or 0) > STAR_SHARE_MAX:
        out["reason"] = "one-pixel 'star' (hot pixel?): not tuned"
        return out
    out["in_band"] = (cfg.peak_lo <= last["peak_frac"] <= cfg.peak_hi
                      and not last.get("clipped"))
    bkg = max(0.0, last["peak_frac"] - last["amp_frac"])
    target_amp = max(0.05, cfg.target - bkg)
    if last.get("clipped"):
        amp = max(last["amp_frac"], 1.0 - bkg)        # true peak is higher
        want = cur_ms * target_amp / amp
        out["reason"] = "clipped: step down now"
    else:
        tail = readings[-OUT_OF_BAND_N:]
        out_of_band = (len(tail) >= OUT_OF_BAND_N and all(
            (r["peak_frac"] < cfg.hyst_lo or r["peak_frac"] > cfg.hyst_hi)
            and not r.get("clipped") for r in tail))
        same_side = out_of_band and len({r["peak_frac"] < cfg.hyst_lo for r in tail}) == 1
        if not same_side:
            out["reason"] = ("in band" if out["in_band"] else
                             "inside the hysteresis band or not yet "
                             f"{OUT_OF_BAND_N} readings out of band")
            return out
        amp = statistics.median(r["amp_frac"] for r in tail)
        if amp <= 0:
            out["reason"] = "no star signal"
            return out
        want = cur_ms * target_amp / amp
        out["reason"] = ("faint" if tail[-1]["peak_frac"] < cfg.hyst_lo else "bright") + \
            f" for {OUT_OF_BAND_N} readings"
    out["want_ms"] = int(round(want))
    ok = _snap(want, durations, cfg, darks)
    if darks is None:
        out["reason"] += "; dark library unknown: no change"
        return out
    if not ok:
        out["reason"] += "; no allowed exposure has a dark"
        return out
    # the longest allowed exposure not over the prediction (never overshoot
    # toward a clip), else the shortest allowed
    pick = max([d for d in ok if d <= want], default=ok[0])
    if want > ok[-1] and pick == ok[-1]:
        out["pinned"] = "faint"
    elif want < ok[0] and pick == ok[0]:
        out["pinned"] = "bright"
    if pick == cur_ms:
        out["reason"] += f"; already at {cur_ms} ms" + (
            f" (pinned {out['pinned']})" if out["pinned"] else "")
        return out
    out.update(action="set", ms=int(pick))
    out["reason"] += f"; {cur_ms} -> {pick} ms (wanted {out['want_ms']} ms)"
    return out


def merge_reading(stats: dict, frame: dict | None) -> dict:
    """A star_stats dict plus PHD2's own GuideStep SNR / HFD / ErrorCode for
    the same moment (PHD2's SNR beats the crop estimate)."""
    r = dict(stats)
    if frame:
        if frame.get("snr") is not None:
            r["snr"] = round(float(frame["snr"]), 1)
        if frame.get("hfd"):
            r["hfd_phd2_px"] = round(float(frame["hfd"]), 2)
        if int(frame.get("code") or 0) == 1:
            r["clipped"] = True
            r["error_code"] = 1
    r.setdefault("snr", r.get("snr_est"))
    return r


def summarize(readings: list[dict]) -> dict:
    def med(k):
        xs = [r[k] for r in readings if r.get(k) is not None]
        return round(statistics.median(xs), 3) if xs else None
    return {"frames": len(readings), "peak_frac": med("peak_frac"),
            "amp_frac": med("amp_frac"), "snr": med("snr"),
            "hfd_px": med("hfd_phd2_px") or med("hfd_px"),
            "clipped": sum(1 for r in readings if r.get("clipped")),
            "bit8": any(r.get("bit8") for r in readings),
            "center_share": med("center_share")}


# ----------------------------------------------------------------- live

class GuideStarTuner:
    """context_fn() -> {"target", "filter", "mount_ra", "mount_dec",
    "slewing", "flip", "guard_open"} from the agent (None = unknown)."""

    def __init__(self, config, phd2, context_fn=None, clock=time.time):
        self.config = config
        self.phd2 = phd2
        self.context_fn = context_fn or (lambda: {})
        self.clock = clock
        self.tasks: set[asyncio.Task] = set()
        self.readings: list[dict] = []       # current target + filter + exposure
        self._key: tuple | None = None
        self._last_measure = 0.0
        self._busy = False
        self._filter: str | None = None
        self.last: dict | None = None        # last measurement / decision
        self._darks: tuple[float, list | None] | None = None
        self._profile: dict | None = None

    def attach(self) -> None:
        self.phd2.on_event(self.on_event)

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    @property
    def mode(self) -> str:
        return tune_mode(self.config)

    # ------------------------------------------------------------ events

    async def on_event(self, ev: dict) -> None:
        if self.mode == "off":
            return
        t = ev.get("Event")
        if t == "SettleDone" and int(ev.get("Status") or 0) == 0:
            self._spawn(self.measure("settled"))
        elif t == "ConfigurationChange":
            self._profile = None
            self._darks = None
            self.readings = []

    def filter_changed(self, new_filter: str | None) -> None:
        """Called by the agent's NINA poll when the RC16 filter changes."""
        if not new_filter or new_filter == self._filter:
            self._filter = new_filter or self._filter
            return
        first = self._filter is None
        self._filter = new_filter
        if first or self.mode == "off":
            return
        self._spawn(self._on_filter(new_filter))

    # ------------------------------------------------------------ gating

    def blocked(self, holder: str | None = None) -> str | None:
        """Why the tuner must not touch PHD2 now (None = free). holder: the
        phd2_ops owner tag that does not count as busy (the tuner itself)."""
        from photonscript.telescope_agent import phd2_ops
        if self.mode == "off":
            return "phd2_tune_mode off"
        if not self.phd2.connected:
            return "PHD2 not connected"
        st = self.phd2.app_state
        if st != "Guiding":
            return f"PHD2 {st}"
        if self.phd2.settling:
            return "settling"
        if phd2_ops.busy() and phd2_ops.owner() != holder:
            return f"PHD2 held by {phd2_ops.owner()}"
        ctx = self.context_fn() or {}
        if ctx.get("guard_open"):
            return "PS-91 guard episode open (non-star lock)"
        if ctx.get("slewing") or ctx.get("flip"):
            return "mount slewing / meridian flip"
        slot = calibration_slot_active(self.config, ctx.get("mount_ra"),
                                       ctx.get("mount_dec"))
        if slot:
            return slot
        return None

    # ------------------------------------------------------------ work

    async def _profile_info(self) -> dict:
        if self._profile is None:
            from photonscript.scheduler import phd2_tuning as tn
            p = {}
            try:
                p = await self.phd2.get_profile() or {}
            except Exception as e:  # noqa: BLE001
                logger.debug("tuner: get_profile failed: %s", e)
            self._profile = {"profile": p.get("name"), "profile_id": p.get("id"),
                             "binning": self.phd2.binning,
                             "gain": tn.current_gain(self.config)}
        return dict(self._profile, binning=self.phd2.binning or self._profile.get("binning"))

    async def _dark_list(self, profile_id) -> list | None:
        """Exposures (ms) in the PHD2 dark library (PS-89 inventory), cached
        10 min. None = no library / unreadable."""
        now = self.clock()
        if self._darks is not None and now - self._darks[0] < 600:
            return self._darks[1]
        from photonscript.scheduler import phd2_audit
        try:
            lst = await asyncio.to_thread(phd2_audit.dark_inventory, self.config, profile_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("tuner: dark inventory failed: %s", e)
            lst = None
        self._darks = (now, lst)
        return lst

    def _ctx_key(self, info: dict, ctx: dict, exp_ms) -> tuple:
        return (info.get("profile"), info.get("binning"), info.get("gain"),
                ctx.get("target"), ctx.get("filter") or self._filter, exp_ms)

    async def _frame_readings(self, n: int) -> list[dict]:
        cfg = tune_cfg(self.config)
        out = []
        for _ in range(n):
            if not await self.phd2.wait_frames(1, timeout=FRAME_WAIT_S):
                break
            if self.blocked():
                break
            frames = self.phd2.recent_frames()
            fr = frames[-1] if frames else None
            try:
                img = await self.phd2.get_star_image()
            except Exception as e:  # noqa: BLE001
                logger.debug("tuner: get_star_image failed: %s", e)
                continue
            s = star_stats(img, cfg.full_scale,
                           int((fr or {}).get("code") or 0))
            if s is not None:
                out.append(merge_reading(s, fr))
        return out

    async def measure(self, why: str = "settled", force: bool = False) -> dict | None:
        """Measure MEASURE_FRAMES frames, record them and (mode exposure)
        decide. Never raises."""
        if self._busy:
            return None
        now = self.clock()
        if not force and now - self._last_measure < MIN_GAP_S:
            return None
        why_not = self.blocked()
        if why_not:
            logger.debug("tuner: %s skipped (%s)", why, why_not)
            return None
        self._busy = True
        self._last_measure = now
        try:
            return await self._measure(why)
        except Exception as e:  # noqa: BLE001 - never let the tuner hurt guiding
            logger.warning("guide tuner: measurement failed: %s", e)
            return None
        finally:
            self._busy = False

    async def _measure(self, why: str) -> dict | None:
        from photonscript.scheduler import phd2_tuning as tn
        info = await self._profile_info()
        cur = await self.phd2.refresh_exposure()
        ctx = self.context_fn() or {}
        cfg = tune_cfg(self.config, ctx.get("filter") or self._filter)   # PS-85 NB band
        key = self._ctx_key(info, ctx, cur)
        if key != self._key:
            self._key, self.readings = key, []
        got = await self._frame_readings(MEASURE_FRAMES)
        if not got:
            return None
        self.readings = (self.readings + got)[-20:]
        sm = summarize(got)
        rec = {"why": why, "mode": self.mode, **info,
               "target": ctx.get("target"), "filter": ctx.get("filter") or self._filter,
               "exposure_ms": cur, **sm,
               "in_band": bool(sm["peak_frac"] is not None
                               and cfg.peak_lo <= sm["peak_frac"] <= cfg.peak_hi
                               and not sm["clipped"])}
        try:
            durs = await self.phd2.get_exposure_durations()
        except Exception as e:  # noqa: BLE001
            durs = []
            logger.debug("tuner: get_exposure_durations failed: %s", e)
        darks = await self._dark_list(info.get("profile_id"))
        d = decide_exposure(self.readings, cur, durs, cfg, darks)
        # observe records what exposure mode would do; only exposure acts
        rec["decision" if self.mode == "exposure" else "would"] = {
            k: d.get(k) for k in ("action", "ms", "want_ms", "reason", "pinned")}
        decision = d if self.mode == "exposure" else None
        await asyncio.to_thread(tn.record, self.config, rec)
        self.last = rec
        logger.info("guide tuner (%s, %s): %s %s exp %s ms peak %s SNR %s HFD %s%s",
                    self.mode, why, rec.get("target"), rec.get("filter"), cur,
                    sm["peak_frac"], sm["snr"], sm["hfd_px"],
                    f"; {decision['reason']}" if decision else "")
        if decision and decision["action"] == "set":
            await self._set(decision["ms"], decision["reason"], rec)
        if (self.mode == "exposure" and decision and decision.get("pinned") == "faint"
                and sm["snr"] is not None and sm["snr"] < SNR_FLOOR):
            await self._faint_alert(rec)
        return rec

    async def _set(self, ms: int, reason: str, rec: dict) -> bool:
        from photonscript.scheduler import phd2_tuning as tn
        from photonscript.telescope_agent import phd2_ops
        before = rec.get("exposure_ms")
        try:
            async with phd2_ops.hold("tuner"):
                if self.blocked(holder="tuner"):
                    return False
                if await self.phd2.get_app_state() != "Guiding":
                    return False
                await self.phd2.set_exposure(int(ms))
                after = await self.phd2.refresh_exposure()
        except phd2_ops.PHD2Busy as e:
            logger.info("guide tuner: no change (%s)", e)
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("guide tuner: set_exposure %s failed: %s", ms, e)
            return False
        ok = after == int(ms)
        await asyncio.to_thread(tn.record_change, self.config, {
            "kind": "exposure", "from": before, "to": int(ms), "read_back": after,
            "ok": ok, "reason": reason, "target": rec.get("target"),
            "filter": rec.get("filter"), "profile": rec.get("profile"),
            "binning": rec.get("binning"), "gain": rec.get("gain")})
        self.readings = []
        self._key = None
        logger.info("guide tuner: exposure %s -> %s ms (%s)%s", before, ms, reason,
                    "" if ok else f"; PHD2 reads back {after}")
        return ok

    async def _on_filter(self, new_filter: str) -> None:
        """Filter change: apply the remembered / predicted exposure for this
        target and filter (mode exposure), then re-verify."""
        from photonscript.scheduler import phd2_tuning as tn
        self.readings, self._key = [], None
        if self.mode == "exposure" and self.blocked() is None:
            try:
                info = await self._profile_info()
                ctx = self.context_fn() or {}
                hit = await asyncio.to_thread(
                    tn.recall, self.config, info.get("profile"), info.get("binning"),
                    info.get("gain"), ctx.get("target"), new_filter,
                    tune_cfg(self.config, new_filter))
                if hit and hit.get("exposure_ms"):
                    cur = await self.phd2.refresh_exposure()
                    durs = await self.phd2.get_exposure_durations()
                    darks = await self._dark_list(info.get("profile_id"))
                    ok = _snap(hit["exposure_ms"], durs, tune_cfg(self.config, new_filter),
                               darks) \
                        if darks is not None else []
                    if ok:
                        pick = min(ok, key=lambda d: (abs(d - hit["exposure_ms"]), d))
                        if pick != cur:
                            await self._set(pick, f"filter {new_filter}: {hit['source']}",
                                            {"exposure_ms": cur, "target": ctx.get("target"),
                                             "filter": new_filter, **info})
            except Exception as e:  # noqa: BLE001
                logger.warning("guide tuner: filter change handling failed: %s", e)
        await self.measure(f"filter {new_filter}", force=True)

    async def _faint_alert(self, rec: dict) -> None:
        """Still faint at the longest allowed exposure with SNR under 10:
        one alert per night (PS-85 decides unguided; nothing is switched)."""
        from photonscript.shared import phd2_store as store
        night = store.night_of(self.config)
        if not store.alert_once(self.config, f"tune-faint-{night}"):
            return
        from photonscript.shared import pushover
        await pushover.notify(
            self.config,
            f"Guide star faint on {rec.get('target')} {rec.get('filter')}: SNR "
            f"{rec.get('snr')} at {rec.get('exposure_ms')} ms (the longest allowed). "
            "More gain or bin 3 pre-dusk (System page > Guide star), or PS-85 unguided.",
            title="PhotonScript guide tuner")


def calibration_slot_active(config, mount_ra=None, mount_dec=None) -> str | None:
    """The PS-93 calibration slot owns PHD2: a plan still graded / retrying,
    or a pending plan with the mount on its field. Reason text or None."""
    try:
        from photonscript.scheduler import phd2_calibration as pc
        from photonscript.telescope_agent.phd2_calmanager import FIELD_TOL_DEG, _sep_deg
        plan = pc.load_plan(config)
    except Exception:  # noqa: BLE001
        return None
    if not pc.plan_active(plan):
        return None
    if plan.get("status") in ("graded", "retrying"):
        return f"PS-93 calibration slot ({plan.get('status')})"
    f = plan.get("field") or {}
    if mount_ra is None or mount_dec is None or f.get("ra_hours") is None:
        return None
    try:
        if _sep_deg(float(mount_ra), float(mount_dec), float(f["ra_hours"]),
                    float(f["dec_degrees"])) <= FIELD_TOL_DEG:
            return "PS-93 calibration slot (mount on the calibration field)"
    except (TypeError, ValueError):
        return None
    return None
