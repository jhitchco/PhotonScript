"""Guide-star viability at the start of a guided block (PS-85). RC16 agent.

The RC16's off-axis guider sits behind the filter wheel, so the guide star
the guide camera sees changes with every filter. On 3 nm narrowband it may
be no star at all: on 2026-10-05 PHD2 locked on noise through Ha / OIII /
SII (SNR 21.9 to 30.9, a jagged profile, HFD 5.6 to 6.2) and "guided" on it.

Pure helpers (tested offline):

    profile_sanity(reply, full_scale)   PHD2 get_star_image crop -> is it a
        star? amplitude at least PROFILE_MIN_SIGMA x the border noise, round
        about its peak (the flux within PROFILE_R px departs from its own
        radial average by at most ASYM_MAX of the flux: a lock on noise is a
        jagged cluster of lumps) and not one bright pixel (PS-91
        peak_fraction).
    judge(frames, profile, cfg, binning)   N guide frames (PHD2's own SNR
        and HFD per frame, lost frames) plus the profile -> viable or not,
        with the numbers. Viable = SNR median at least guide_viable_snr_min,
        HFD inside guide_viable_hfd_px (given at bin 2, scaled by 2 / binning
        like the guard's D1), at most MAX_LOST_FRAC frames lost and a sane
        profile.

ViabilityMonitor listens to the RC16 agent's PHD2 client. After a filter
change (the agent's filter-wheel poll) and after a settle, while PHD2 is
guiding or looping on a selected star, it reads the next guide_viable_frames
frames and one star image. It only reads (no PHD2 command), and it stays out
of the way while another PhotonScript actor holds PHD2, during the PS-93
calibration slot, while slewing or flipping, and on nights that are not
armed guided. Each check is recorded (scheduler.guide_blocks). A final
"not viable" asks the armer to run that block unguided
(armer.request_block_unguided; guide_block_mode decides whether anything is
switched). While the PS-90 tuner (mode exposure) can still lengthen the
narrowband guide exposure, a not-viable check is "pending" and re-checked
after the tuner's next settle, at most MAX_PENDING times.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import statistics
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PROFILE_MIN_SIGMA = 5.0    # star amplitude over the border noise
PROFILE_R = 4              # px around the peak the roundness test uses
ASYM_MAX = 0.35            # departure from the radial average, share of flux
ONE_PIXEL_SHARE = 0.5      # PS-91 D1: over half the 5x5 flux in one pixel
MAX_LOST_FRAC = 0.4        # more lost frames than this = not viable
FRAME_WAIT_S = 30.0        # wait for one guide frame at most this long
MAX_PENDING = 3            # re-checks while the tuner still has headroom
GUIDING_STATES = ("Guiding", "Looping", "LostLock")


def _pair(text, default):
    try:
        a, b = (float(x) for x in str(text).split(",")[:2])
        return (min(a, b), max(a, b))
    except (TypeError, ValueError):
        return default


@dataclass
class ViableCfg:
    snr_min: float = 30.0
    hfd_lo: float = 1.5      # PHD2 px at bin 2
    hfd_hi: float = 10.0
    frames: int = 5

    def hfd_band(self, binning) -> tuple[float, float]:
        try:
            k = 2.0 / float(binning) if binning else 1.0
        except (TypeError, ValueError):
            k = 1.0
        return (self.hfd_lo * k, self.hfd_hi * k)


def viable_cfg(config) -> ViableCfg:
    lo, hi = _pair(getattr(config, "guide_viable_hfd_px", "1.5,10"), (1.5, 10.0))
    return ViableCfg(
        snr_min=float(getattr(config, "guide_viable_snr_min", 30.0) or 30.0),
        hfd_lo=lo, hfd_hi=hi,
        frames=max(1, int(getattr(config, "guide_viable_frames", 5) or 5)))


# ------------------------------------------------------------------- pure

def profile_sanity(reply: dict, full_scale: int = 65535) -> dict | None:
    """Is PHD2's star image a star? None when the reply is unusable.
    {"ok", "why", "peak_sigma", "asymmetry", "center_share"}"""
    import numpy as np
    from photonscript.telescope_agent.guide_guard import peak_fraction
    try:
        w, h = int(reply["width"]), int(reply["height"])
        px = np.frombuffer(base64.b64decode(reply["pixels"]), dtype="<u2")
        img = px[: w * h].reshape(h, w).astype(float)
    except (KeyError, TypeError, ValueError):
        return None
    if w < 7 or h < 7:
        return None
    border = np.concatenate([img[:2].ravel(), img[-2:].ravel(),
                             img[2:-2, :2].ravel(), img[2:-2, -2:].ravel()])
    bkg = float(np.median(border))
    noise = float(np.median(np.abs(border - bkg))) * 1.4826 or 1.0
    sub = img - bkg
    py, pxx = np.unravel_index(int(np.argmax(sub)), sub.shape)
    peak_sigma = float(sub[py, pxx]) / noise
    yy, xx = np.mgrid[0:h, 0:w]
    ri = np.rint(np.hypot(xx - pxx, yy - py)).astype(int)
    inside = ri <= PROFILE_R
    prof = np.array([float(sub[ri == k].mean()) if (ri == k).any() else 0.0
                     for k in range(PROFILE_R + 1)])
    model = prof[np.clip(ri, 0, PROFILE_R)]
    flux = float(np.clip(sub[inside], 0, None).sum())
    # what a round star would leave: about 0.8 sigma of noise per pixel
    dev = float(np.abs(sub[inside] - model[inside]).sum()) - 0.8 * noise * int(inside.sum())
    asym = max(0.0, dev) / flux if flux > 0 else 1.0
    share = peak_fraction(reply)
    why = []
    if peak_sigma < PROFILE_MIN_SIGMA:
        why.append(f"peak only {peak_sigma:.1f} sigma over the background")
    elif asym > ASYM_MAX:
        why.append(f"jagged profile ({asym:.0%} of the flux off a round star)")
    if share is not None and share > ONE_PIXEL_SHARE:
        why.append(f"{share:.0%} of the 5x5 flux in one pixel")
    return {"ok": not why, "why": "; ".join(why) or "star-like profile",
            "peak_sigma": round(peak_sigma, 1), "asymmetry": round(asym, 3),
            "center_share": round(share, 3) if share is not None else None}


def judge(frames: list[dict], profile: dict | None, cfg: ViableCfg,
          binning=None) -> dict | None:
    """Viable or not from N guide frames ({"snr", "hfd", "drop"}) and the
    profile check. None when there is nothing to judge (no frames)."""
    if not frames:
        return None
    n = len(frames)
    lost = sum(1 for f in frames if f.get("drop"))
    kept = [f for f in frames if not f.get("drop")]
    snrs = [float(f["snr"]) for f in kept if f.get("snr") is not None]
    hfds = [float(f["hfd"]) for f in kept if f.get("hfd")]
    snr = round(statistics.median(snrs), 1) if snrs else None
    hfd = round(statistics.median(hfds), 2) if hfds else None
    lo, hi = cfg.hfd_band(binning)
    why = []
    if lost / n > MAX_LOST_FRAC:
        why.append(f"star lost in {lost} of {n} frames")
    if snr is None:
        why.append("no SNR reported")
    elif snr < cfg.snr_min:
        why.append(f"SNR {snr:g} under {cfg.snr_min:g}")
    if hfd is not None and not (lo <= hfd <= hi):
        why.append(f"HFD {hfd:g} px outside {lo:g} to {hi:g}")
    if profile is not None and not profile.get("ok"):
        why.append(str(profile.get("why")))
    ok = not why
    detail = (f"SNR {snr if snr is not None else '-'}, HFD "
              f"{hfd if hfd is not None else '-'} px, {n - lost}/{n} frames")
    return {"viable": ok, "reason": (detail if ok else "; ".join(why) + f" ({detail})"),
            "snr": snr, "hfd_px": hfd, "lost_frac": round(lost / n, 2),
            "frames": n, "profile": (None if profile is None else
                                     ("ok" if profile.get("ok") else profile.get("why")))}


def frame_sample(client) -> dict | None:
    """The newest guide frame's star numbers: the last GuideStep / StarLost
    frame while guiding, else the last LoopingExposures star fields."""
    st = getattr(client, "app_state", None)
    if st in ("Guiding", "LostLock"):
        fs = client.recent_frames()
        if fs:
            f = fs[-1]
            return {"snr": f.get("snr"), "hfd": f.get("hfd"), "drop": bool(f.get("drop")),
                    "t": f.get("t")}
        return None
    ls = getattr(client, "loop_star", None)
    if st == "Looping" and ls:
        return {"snr": ls.get("SNR"), "hfd": ls.get("HFD"), "drop": False, "t": ls.get("t")}
    return None


# ------------------------------------------------------------------- live

class ViabilityMonitor:
    """context_fn() -> {"target", "filter", "mount_ra", "mount_dec",
    "slewing", "flip"} from the agent (None = unknown). tuner: the agent's
    GuideStarTuner (or None), for the narrowband headroom test."""

    def __init__(self, config, phd2, context_fn=None, tuner=None,
                 guided_fn=None, clock=time.time):
        self.config = config
        self.phd2 = phd2
        self.context_fn = context_fn or (lambda: {})
        self.tuner = tuner
        self.guided_fn = guided_fn
        self.clock = clock
        self.tasks: set[asyncio.Task] = set()
        self._busy = False
        self._final: set[tuple] = set()      # (night, target, filter) decided
        self._pending: dict[tuple, int] = {}
        self._filter: str | None = None
        self.last: dict | None = None

    @property
    def mode(self) -> str:
        from photonscript.scheduler.guide_blocks import block_mode
        return block_mode(self.config)

    def attach(self) -> None:
        self.phd2.on_event(self.on_event)

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def on_event(self, ev: dict) -> None:
        if self.mode == "off":
            return
        if ev.get("Event") == "SettleDone" and int(ev.get("Status") or 0) == 0:
            self._spawn(self.check("settled"))

    def filter_changed(self, new_filter: str | None) -> None:
        """Called by the agent's filter-wheel poll on a change."""
        if not new_filter or new_filter == self._filter:
            self._filter = new_filter or self._filter
            return
        self._filter = new_filter
        if self.mode == "off":
            return
        self._spawn(self.check(f"filter {new_filter}"))

    def _guided_night(self) -> bool:
        if self.guided_fn is not None:
            return bool(self.guided_fn())
        from photonscript.scheduler.armer import armer_guided_now
        return armer_guided_now(self.config)

    def blocked(self) -> str | None:
        """Why no check now (None = go ahead)."""
        from photonscript.telescope_agent import phd2_ops
        from photonscript.telescope_agent.guide_tuner import calibration_slot_active
        if self.mode == "off":
            return "guide_block_mode off"
        if not self.phd2.connected:
            return "PHD2 not connected"
        st = self.phd2.app_state
        if st not in GUIDING_STATES:
            return f"PHD2 {st}"
        if self.phd2.settling:
            return "settling"
        if phd2_ops.busy():
            return f"PHD2 held by {phd2_ops.owner()}"
        ctx = self.context_fn() or {}
        if ctx.get("slewing") or ctx.get("flip"):
            return "mount slewing / meridian flip"
        slot = calibration_slot_active(self.config, ctx.get("mount_ra"),
                                       ctx.get("mount_dec"))
        if slot:
            return slot
        if not self._guided_night():
            return "no guided night armed"
        return None

    async def _headroom(self, filt) -> tuple[bool, int | None]:
        """(the tuner can still lengthen the guide exposure on this filter,
        current exposure ms)."""
        from photonscript.scheduler.guide_blocks import is_nb
        from photonscript.telescope_agent.guide_tuner import tune_cfg
        cur = None
        try:
            cur = await self.phd2.refresh_exposure()
        except Exception as e:  # noqa: BLE001
            logger.debug("viability: exposure read failed: %s", e)
        t = self.tuner
        if t is None or getattr(t, "mode", "off") != "exposure" or not is_nb(filt):
            return False, cur
        hi = tune_cfg(self.config, filt).exp_hi
        return (cur is not None and int(cur) < int(hi)), cur

    async def check(self, why: str = "settled") -> dict | None:
        """One viability check of the current block. Never raises."""
        if self._busy:
            return None
        why_not = self.blocked()
        if why_not:
            logger.debug("viability: %s skipped (%s)", why, why_not)
            return None
        from photonscript.shared import phd2_store as store
        ctx = self.context_fn() or {}
        target, filt = ctx.get("target"), ctx.get("filter") or self._filter
        if not target or not filt:
            return None
        night = store.night_of(self.config)
        key = (night, target, filt)
        if key in self._final:
            return None
        self._busy = True
        try:
            return await self._check(why, key)
        except Exception as e:  # noqa: BLE001 - never let it hurt guiding
            logger.warning("viability check failed: %s", e)
            return None
        finally:
            self._busy = False

    async def _check(self, why: str, key: tuple) -> dict | None:
        from photonscript.scheduler import guide_blocks as gb
        night, target, filt = key
        cfg = viable_cfg(self.config)
        frames = []
        for _ in range(cfg.frames):
            if not await self.phd2.wait_frames(1, timeout=FRAME_WAIT_S):
                break
            if self.blocked():
                return None
            s = frame_sample(self.phd2)
            if s is not None:
                frames.append(s)
        profile = None
        try:
            from photonscript.telescope_agent.guide_tuner import tune_cfg
            profile = profile_sanity(await self.phd2.get_star_image(),
                                     tune_cfg(self.config).full_scale)
        except Exception as e:  # noqa: BLE001 - no star selected, old PHD2
            logger.debug("viability: get_star_image failed: %s", e)
        v = judge(frames, profile, cfg, self.phd2.binning)
        if v is None:
            return None
        head, cur = (False, None) if v["viable"] else await self._headroom(filt)
        n_pend = self._pending.get(key, 0)
        final = bool(v["viable"] or not head or n_pend >= MAX_PENDING)
        if not final:
            self._pending[key] = n_pend + 1
        else:
            self._final.add(key)
        try:
            from photonscript.scheduler import phd2_tuning
            gain = phd2_tuning.current_gain(self.config)
        except Exception:  # noqa: BLE001
            gain = None
        rec = gb.append(self.config, night, {
            "event": "check", "target": target, "filter": filt, "why": why,
            **v, "final": final, "exposure_ms": cur,
            "binning": self.phd2.binning, "gain": gain,
            "app_state": self.phd2.app_state, "mode": self.mode})
        self.last = rec
        logger.info("viability %s %s (%s): %s%s", target, filt, why,
                    "viable" if v["viable"] else "NOT viable", f"; {v['reason']}")
        if final and not v["viable"]:
            d = gb.decide(mode=self.mode, night_guided=True, verdict=v)
            from photonscript.scheduler.armer import request_block_unguided
            await request_block_unguided(self.config, target, filt, d["reason"],
                                         source="live", evidence={
                                             k: v.get(k) for k in ("snr", "hfd_px",
                                                                   "lost_frac", "profile")})
        return rec

    def forget(self, target, filt) -> None:
        """A decision was made elsewhere (guard D6): no further checks."""
        from photonscript.shared import phd2_store as store
        self._final.add((store.night_of(self.config), target, filt))
