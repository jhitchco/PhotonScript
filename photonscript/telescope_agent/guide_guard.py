"""Live non-star lock guard (PS-91): is PHD2 guiding on a real star?

PHD2 locked onto non-stars on 2026-09-25 (a "star" that scattered 0.06" for
91 min) and 2026-09-26 (still for 25 min between dithers; still "guiding"
after the roof closed and the scope parked). A hot-pixel lock has a tiny
RMS, so it looked like excellent guiding. PS-88 finds this the next morning;
this guard finds it while it happens.

``NonStarLockGuard`` is pure: the telescope agent feeds it PHD2 events and,
every tick, a GuardContext (the client's guide-frame ring buffer plus mount,
safety and armer state); it answers with verdicts. Five detectors:

  D1  profile: median HFD of the last 10 guided frames under
      guide_min_star_hfd_px (given for bin 2, scaled by 2 / binning),
      confirmed by PHD2's star image (the central pixel holds over half the
      5x5 flux). A hot pixel is one bright pixel with no wings.
  D2  static: the star scattered under 0.15" for 5+ min within one lock
      epoch (shared.guide_motion thresholds, the same as PS-88's star_static).
  D3  impossible state: PHD2 guiding while the mount is parked, tracking is
      off, the safety monitor reads unsafe, or the armer has no night in
      progress, for over 120 s.
  D4  pulses at the max duration for 3 min with no star motion (shared
      response logic, rates from PHD2's calibration rescaled by cos Dec). A
      D1 or D5 hit makes it a non-star case; otherwise the star is real and
      it is the pulse path (handed to the PS-92 FAIL path, not re-selected).
  D5  the lock position sits within 2 px of a hot-pixel map entry (checked
      on every StarSelected / LockPositionSet).
  D6  (PS-85) guiding on noise: PHD2's SNR stayed under guide_viable_snr_min
      for guide_lowsnr_frames guided frames in a row within one lock epoch,
      confirmed by PHD2's star image not looking like a star (a jagged or
      one-pixel profile, guide_viability.profile_sanity), so a weak but real
      star is left alone (2026-10-05: a 3 nm "star" at SNR 21.9 to 30.9 with
      a jagged profile; 2026-09-26 real OIII / SII stars read SNR 20 to 22).
      Kind LOW_SNR: the agent stops guiding and switches that filter block to
      unguided (PS-85 guide_block_mode), instead of re-selecting a star.
  D7  (PS-155) no corrections: phd2_nocorr_frames guided frames in a row with
      the raw error over phd2_nocorr_px and no RA and no Dec pulse (2026-10-06:
      Guiding all night, star 5 to 160 px off the lock, every pulse 0 ms,
      because the mount driver failed IsSlewing and PHD2 dropped each pulse).
  D8  (PS-155) lock offset growing: within one lock epoch the star walks away
      from the lock position for phd2_drift_window_min, each quarter of the
      window further off. Kind NO_CORR for both: page once per night, never
      re-select a star (the star is fine; the corrections are not reaching
      the mount), hand the night to the PS-156 unguided fallback.

Also here: hot-pixel map helpers, star detection on a guide frame and the
guide-star vetting used by the recovery and the pulse self-test.
"""
from __future__ import annotations

import base64
import math
import statistics
from dataclasses import dataclass, field

from photonscript.shared import guide_motion as gm

NON_STAR, IMPOSSIBLE, PULSES = "non_star", "impossible_state", "pulses_not_moving"
LOW_SNR = "low_snr"       # PS-85 D6
NO_CORR = "no_corrections"  # PS-155 D7 / D8
D1_FRAMES = 10
D1_PEAK_FRAC = 0.5
D3_PERSIST_S = 120.0
D4_WINDOW_S = 180.0
D4_AT_MAX_PCT = 50.0
D5_RADIUS_PX = 2.0
# PS-155: PHD2's Guiding Assistant turns guide output off while it measures
# (2026-09-25 20:53: 75 frames, 132 s, star 10 px off, no pulses). D7 / D8
# hold off while output is known off for under this long.
OUTPUT_OFF_GRACE_S = 900.0
GUIDING_STATES = ("Guiding", "Calibrating", "LostLock")


@dataclass
class Verdict:
    code: str            # D1 .. D6
    kind: str            # NON_STAR | IMPOSSIBLE | PULSES | LOW_SNR
    detail: str
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"code": self.code, "kind": self.kind, "detail": self.detail,
                "evidence": self.evidence}


@dataclass
class GuardContext:
    """One tick's view of the world (None = unknown, never a trip)."""
    frames: list = field(default_factory=list)   # PHD2Client.recent_frames()
    app_state: str = "Unknown"                   # PHD2 AppState name
    scale: float | None = None                   # guide "/px
    binning: int | None = None
    lock: tuple | None = None                    # lock position (px)
    star_peak_frac: float | None = None          # D1 confirmation
    star_profile_ok: bool | None = None          # D6 confirmation (PS-85)
    at_park: bool | None = None
    tracking: bool | None = None
    safe: bool | None = None
    armer_active: bool | None = None
    ops_busy: bool = False                       # a PhotonScript actor holds PHD2
    rates_px_s: tuple | None = None              # (RA, Dec) px/s at this Dec
    now: float | None = None                     # unix s (frames' clock)


# ------------------------------------------------------------------ helpers

def hfd_threshold(config, binning) -> float:
    base = float(getattr(config, "guide_min_star_hfd_px", 1.5) or 1.5)
    b = max(1, int(binning or 2))
    return base * 2.0 / b


def peak_fraction(star_image: dict) -> float | None:
    """Share of the background-subtracted 5x5 flux in the central pixel of
    PHD2's get_star_image crop. A real star at the guide scale spreads over
    many pixels (about 0.1 to 0.3); a hot pixel puts nearly all of it in one."""
    import numpy as np
    try:
        w, h = int(star_image["width"]), int(star_image["height"])
        px = np.frombuffer(base64.b64decode(star_image["pixels"]), dtype="<u2")
        img = px[: w * h].reshape(h, w).astype(float)
        sx, sy = star_image.get("star_pos") or (w / 2, h / 2)
        cx, cy = int(round(float(sx))), int(round(float(sy)))
    except (KeyError, TypeError, ValueError):
        return None
    bkg = float(np.median(img))
    y0, y1 = max(0, cy - 2), min(h, cy + 3)
    x0, x1 = max(0, cx - 2), min(w, cx + 3)
    sub = np.clip(img[y0:y1, x0:x1] - bkg, 0, None)
    tot = float(sub.sum())
    if tot <= 0 or not (0 <= cy < h and 0 <= cx < w):
        return None
    # the brightest pixel near the reported position (centroid rounding)
    return float(sub.max()) / tot


def hotpix_points(hotpix: dict | None, binning: int | None) -> list[tuple]:
    """Hot-pixel map entries as (x, y) at the current PHD2 binning."""
    if not hotpix:
        return []
    mb = max(1, int(hotpix.get("binning") or 1))
    cb = max(1, int(binning or mb))
    k = mb / cb
    return [(float(p[0]) * k, float(p[1]) * k) for p in hotpix.get("pixels", [])]


def near_hot_pixel(xy, hotpix: dict | None, binning, radius=D5_RADIUS_PX):
    """The nearest hot-pixel entry within radius of xy, or None."""
    if xy is None:
        return None
    best = None
    for hx, hy in hotpix_points(hotpix, binning):
        d = math.hypot(hx - xy[0], hy - xy[1])
        if d <= radius and (best is None or d < best[2]):
            best = (hx, hy, d)
    return best


def hotpix_mask(shape, hotpix: dict | None, binning, grow: int = 1):
    """Boolean mask of the hot-pixel map at this frame's shape and binning."""
    import numpy as np
    m = np.zeros(shape, dtype=bool)
    for hx, hy in hotpix_points(hotpix, binning):
        x, y = int(round(hx)), int(round(hy))
        m[max(0, y - grow):y + grow + 1, max(0, x - grow):x + grow + 1] = True
    return m


def detect_stars(img, mask=None, nsigma: float = 5.0, min_pix: int = 3,
                 saturation: float = 60000.0) -> list[dict]:
    """Stars on one guide frame: a 3x3 median removes single hot pixels and
    noise spikes, masked pixels (the hot-pixel map) are set to the
    background, then connected pixels above nsigma form candidates. Each:
    x, y (flux-weighted, original frame), peak, flux, hfd (px), snr,
    clipped, npix."""
    import numpy as np
    from scipy import ndimage
    a = np.asarray(img, dtype=float)
    med = ndimage.median_filter(a, size=3)
    bkg = float(np.median(med))
    mad = float(np.median(np.abs(med - bkg))) * 1.4826 or 1.0
    work = med.copy()
    if mask is not None:
        work[np.asarray(mask, dtype=bool)] = bkg
    lbl, n = ndimage.label(work > bkg + nsigma * mad)
    out = []
    h, w = a.shape
    for i, sl in enumerate(ndimage.find_objects(lbl), start=1):
        if sl is None:
            continue
        sel = lbl[sl] == i
        npix = int(sel.sum())
        if npix < min_pix:
            continue
        sub = np.clip(a[sl] - bkg, 0, None) * sel
        flux = float(sub.sum())
        if flux <= 0:
            continue
        yy, xx = np.mgrid[sl[0], sl[1]]
        cx = float((sub * xx).sum() / flux)
        cy = float((sub * yy).sum() / flux)
        r = 8
        y0, y1 = max(0, int(cy) - r), min(h, int(cy) + r + 1)
        x0, x1 = max(0, int(cx) - r), min(w, int(cx) + r + 1)
        box = np.clip(a[y0:y1, x0:x1] - bkg, 0, None)
        by, bx = np.mgrid[y0:y1, x0:x1]
        rr = np.hypot(bx - cx, by - cy)
        inside = rr <= r
        tot = float(box[inside].sum())
        hfd = 2.0 * float((box * rr)[inside].sum()) / tot if tot > 0 else None
        peak = float(a[sl][sel].max())
        out.append({"x": cx, "y": cy, "peak": peak, "flux": flux, "hfd": hfd,
                    "snr": flux / (mad * math.sqrt(npix)), "npix": npix,
                    "clipped": peak >= saturation})
    out.sort(key=lambda s: -s["flux"])
    return out


def vet_stars(stars, shape, hfd_min=1.5, hfd_max=10.0, snr_min=15.0,
              edge_px=20, hotpix: dict | None = None, binning=None) -> list[dict]:
    """Guide-star candidates that look like real, usable stars: HFD inside
    hfd_min..hfd_max, SNR at least snr_min, not clipped, away from the frame
    edge and from every hot-pixel map entry. Best (highest SNR) first."""
    h, w = shape
    ok = []
    for s in stars:
        if s["hfd"] is None or not (hfd_min <= s["hfd"] <= hfd_max):
            continue
        if s["snr"] < snr_min or s["clipped"]:
            continue
        if not (edge_px <= s["x"] < w - edge_px and edge_px <= s["y"] < h - edge_px):
            continue
        if near_hot_pixel((s["x"], s["y"]), hotpix, binning, radius=3.0):
            continue
        ok.append(s)
    ok.sort(key=lambda s: -s["snr"])
    return ok


def load_fits(path):
    """(2-D float array, header dict) of a PHD2 save_image FITS."""
    import numpy as np
    from astropy.io import fits
    with fits.open(path) as hd:
        data = hd[0].data
        hdr = dict(hd[0].header)
    a = np.asarray(data, dtype=float)
    while a.ndim > 2:
        a = a[0]
    return a, hdr


# -------------------------------------------------------------------- guard

class NonStarLockGuard:
    """Feed PHD2 events; ask for verdicts each tick. Pure: no I/O."""

    def __init__(self, config, hotpix: dict | None = None):
        self.config = config
        self.hotpix = hotpix
        self.lock: tuple | None = None
        self.binning: int | None = None
        self._d3_since: float | None = None
        self._d5: Verdict | None = None
        self.last_alert: dict | None = None   # PS-155: PHD2's newest Alert event
        self.output_off_since: float | None = None  # PS-155: guide output off

    def set_hotpix(self, hotpix: dict | None) -> None:
        self.hotpix = hotpix
        self._d5 = self._check_d5(self.lock)

    def feed(self, event: dict) -> list[Verdict]:
        """Lock events trigger D5 at once; returns any new verdicts."""
        et = event.get("Event")
        if et == "Alert":
            # PS-155: e.g. "ASCOM driver failed checking for slewing" (each
            # pulse dropped); quoted in a D7 / D8 verdict
            self.last_alert = {"msg": str(event.get("Msg") or "")[:200],
                               "type": event.get("Type"),
                               "t": event.get("Timestamp")}
            return []
        if et == "GuideParamChange" and str(event.get("Name") or "").replace(
                " ", "").lower() == "mountguidingenabled":
            # PS-155: the Guiding Assistant (or a person) toggling output
            on = str(event.get("Value")).strip().lower() in ("true", "1")
            t = event.get("Timestamp")
            self.output_off_since = None if on else (
                float(t) if isinstance(t, (int, float)) else -1.0)
            return []
        if et in ("LockPositionSet", "StarSelected"):
            try:
                xy = (float(event.get("X")), float(event.get("Y")))
            except (TypeError, ValueError):
                return []
            if et == "LockPositionSet":
                self.lock = xy
            self._d5 = self._check_d5(xy)
            return [self._d5] if self._d5 else []
        if et == "LockPositionLost":
            self.lock = None
            self._d5 = None
        if et in ("GuidingStopped", "LoopingExposuresStopped"):
            self._d3_since = None
        return []

    def _check_d5(self, xy) -> Verdict | None:
        hit = near_hot_pixel(xy, self.hotpix, self.binning)
        if not hit:
            return None
        return Verdict("D5", NON_STAR,
                       f"lock ({xy[0]:.1f}, {xy[1]:.1f}) is {hit[2]:.1f} px from "
                       f"a mapped hot pixel ({hit[0]:.0f}, {hit[1]:.0f})",
                       {"lock": list(xy), "hot_pixel": [hit[0], hit[1]],
                        "distance_px": round(hit[2], 2)})

    def wants_star_image(self, ctx: GuardContext) -> bool:
        """True when D1's HFD test trips and needs PHD2's star image to
        confirm (the agent then asks get_star_image)."""
        return self._d1_hfd(ctx) is not None or self._d6_snr(ctx) is not None

    def _guided(self, ctx: GuardContext, seconds: float | None = None):
        fs = [f for f in ctx.frames if not f.get("drop")]
        if seconds is not None and fs:
            now = ctx.now if ctx.now is not None else fs[-1]["t"]
            fs = [f for f in fs if f["t"] >= now - seconds]
        return fs

    def _d1_hfd(self, ctx: GuardContext) -> float | None:
        if ctx.app_state != "Guiding":
            return None
        cur = [f for f in self._guided(ctx) if f.get("hfd")]
        if ctx.frames:
            ep = ctx.frames[-1].get("epoch")
            cur = [f for f in cur if f.get("epoch") == ep]
        last = cur[-D1_FRAMES:]
        if len(last) < D1_FRAMES:
            return None
        med = statistics.median(f["hfd"] for f in last)
        return med if med < hfd_threshold(self.config, ctx.binning) else None

    def verdicts(self, ctx: GuardContext) -> list[Verdict]:
        if ctx.binning:
            self.binning = ctx.binning
        if ctx.lock is not None:
            if ctx.lock != self.lock:
                self.lock = tuple(ctx.lock)
                self._d5 = self._check_d5(self.lock)
        out: list[Verdict] = []
        guiding = ctx.app_state == "Guiding"
        # D1 profile
        med = self._d1_hfd(ctx)
        if med is not None and ctx.star_peak_frac is not None \
                and ctx.star_peak_frac > D1_PEAK_FRAC:
            out.append(Verdict(
                "D1", NON_STAR,
                f"guide 'star' HFD {med:.2f} px (min "
                f"{hfd_threshold(self.config, ctx.binning):.2f}) and "
                f"{ctx.star_peak_frac:.0%} of its 5x5 flux in one pixel",
                {"hfd_median_px": round(med, 2),
                 "peak_frac": round(ctx.star_peak_frac, 2)}))
        # D2 static within the current lock epoch
        if guiding and ctx.frames and ctx.scale:
            ep = ctx.frames[-1].get("epoch")
            cur = [f for f in self._guided(ctx) if f.get("epoch") == ep]
            rows = gm.epoch_scatter(cur, ctx.scale)
            if rows and rows[-1]["static"]:
                r = rows[-1]
                out.append(Verdict(
                    "D2", NON_STAR,
                    f"guide 'star' scattered {r['std_arcsec']:.2f}\" over "
                    f"{r['span_s'] / 60:.0f} min at one lock (a real star "
                    f"scatters 0.3 to 0.6\" here; static under "
                    f"{gm.STATIC_STD_ARCSEC:g}\")",
                    {"std_arcsec": round(r["std_arcsec"], 3),
                     "minutes": round(r["span_s"] / 60, 1), "frames": r["n"]}))
        # D3 impossible state (debounced; never while PhotonScript holds PHD2)
        why = []
        pulsing = ctx.app_state in GUIDING_STATES
        if pulsing or ctx.app_state == "Looping":
            if ctx.at_park is True:
                why.append("mount parked")
            if ctx.tracking is False:
                why.append("tracking off")
            if ctx.safe is False and pulsing:
                why.append("safety monitor unsafe")
        if pulsing and ctx.armer_active is False:
            why.append("no night armed")
        now = ctx.now
        if why and not ctx.ops_busy and now is not None:
            if self._d3_since is None:
                self._d3_since = now
            elif now - self._d3_since >= D3_PERSIST_S:
                out.append(Verdict(
                    "D3", IMPOSSIBLE,
                    f"PHD2 {ctx.app_state} while " + ", ".join(why)
                    + f" for {int(now - self._d3_since)} s",
                    {"app_state": ctx.app_state, "why": why}))
        else:
            self._d3_since = None
        # D5 (from lock events)
        if self._d5 is not None and guiding:
            out.append(self._d5)
        # D4 max pulses, no motion
        d4 = self._d4(ctx) if guiding else None
        if d4 is not None:
            nonstar = any(v.code in ("D1", "D5") for v in out)
            d4.kind = NON_STAR if nonstar else PULSES
            out.append(d4)
        # D6 (PS-85) guiding on noise
        d6 = self._d6(ctx) if guiding else None
        if d6 is not None:
            out.append(d6)
        # D7 / D8 (PS-155) guiding that does not correct; never while a
        # PhotonScript actor holds PHD2 (a self-test pulses by hand)
        if guiding and not ctx.ops_busy and not self._output_off_young(ctx):
            out.extend(v for v in (self._d7(ctx), self._d8(ctx)) if v is not None)
        return out

    def _alert_note(self) -> str:
        a = self.last_alert or {}
        return f" PHD2 alert: {a['msg']}" if a.get("msg") else ""

    def _output_off_young(self, ctx: GuardContext) -> bool:
        """True while guide output is known off (a GuideParamChange event,
        or the newest frames carry output False) for under OUTPUT_OFF_GRACE_S:
        most likely the Guiding Assistant, which measures with output off."""
        since = self.output_off_since
        fs = self._guided(ctx)
        if fs and fs[-1].get("output", True) is False:
            k = len(fs) - 1
            while k > 0 and fs[k - 1].get("output", True) is False:
                k -= 1
            since = fs[k]["t"] if since is None or since < 0 else min(since, fs[k]["t"])
        if since is None:
            return False
        now = ctx.now if ctx.now is not None else (fs[-1]["t"] if fs else None)
        if since < 0 or now is None:
            return True
        return now - since < OUTPUT_OFF_GRACE_S

    def _d7(self, ctx: GuardContext) -> Verdict | None:
        """D7: the newest guided frames form an open run of at least
        phd2_nocorr_frames frames off the lock with no pulse on either axis."""
        n = int(getattr(self.config, "phd2_nocorr_frames", gm.NOCORR_FRAMES) or 0)
        if n <= 0 or not ctx.frames:
            return None
        px = float(getattr(self.config, "phd2_nocorr_px", gm.NOCORR_MIN_PX)
                   or gm.NOCORR_MIN_PX)
        runs = gm.no_correction_runs(self._guided(ctx), px, n)
        if runs["tail"] < n:
            return None
        ev = {"frames": runs["tail"], "threshold_px": px,
              "median_px": runs["median_px"]}
        if self.last_alert:
            ev["phd2_alert"] = self.last_alert.get("msg")
        return Verdict(
            "D7", NO_CORR,
            f"PHD2 is guiding but sending no corrections: {runs['tail']} frames "
            f"in a row with the star over {px:g} px off the lock (median "
            f"{runs['median_px']} px) and no RA or Dec pulse.{self._alert_note()}",
            ev)

    def _d8(self, ctx: GuardContext) -> Verdict | None:
        """D8: the star walks away from the lock position within one epoch."""
        win = float(getattr(self.config, "phd2_drift_window_min", 5.0) or 0) * 60
        if win <= 0 or not ctx.frames:
            return None
        px = float(getattr(self.config, "phd2_nocorr_px", gm.NOCORR_MIN_PX)
                   or gm.NOCORR_MIN_PX)
        g = gm.offset_growth(self._guided(ctx), win, px, now=ctx.now)
        if g is None:
            return None
        return Verdict(
            "D8", NO_CORR,
            f"the guide star is walking away from the lock position: "
            f"{g['from_px']} to {g['to_px']} px over {g['minutes']:g} min "
            f"while PHD2 reports guiding.{self._alert_note()}",
            dict(g, threshold_px=px))

    def _d6_snr(self, ctx: GuardContext) -> tuple | None:
        """(median SNR, frames, floor) when the last guide_lowsnr_frames
        guided (not lost, not settling) frames of the current lock epoch all
        read under guide_viable_snr_min; None when off or not so."""
        n = int(getattr(self.config, "guide_lowsnr_frames", 10) or 0)
        if n <= 0 or not ctx.frames or ctx.app_state != "Guiding":
            return None
        floor = float(getattr(self.config, "guide_viable_snr_min", 30.0) or 30.0)
        ep = ctx.frames[-1].get("epoch")
        cur = [f for f in self._guided(ctx) if f.get("epoch") == ep
               and not f.get("settling")]
        last = cur[-n:]
        if len(last) < n or any(f.get("snr") is None for f in last):
            return None
        if any(float(f["snr"]) >= floor for f in last):
            return None
        return statistics.median(float(f["snr"]) for f in last), n, floor

    def _d6(self, ctx: GuardContext) -> Verdict | None:
        """D6 when the SNR test holds and PHD2's star image is not star-like
        (ctx.star_profile_ok False; unknown never trips)."""
        hit = self._d6_snr(ctx)
        if hit is None or ctx.star_profile_ok is not False:
            return None
        med, n, floor = hit
        return Verdict(
            "D6", LOW_SNR,
            f"guide star SNR {med:.1f} (median of the last {n} frames, all "
            f"under {floor:g}) and its profile is not a star's: PHD2 is "
            "guiding on noise",
            {"snr_median": round(med, 1), "frames": n, "snr_min": floor})

    def _d4(self, ctx: GuardContext) -> Verdict | None:
        if not ctx.rates_px_s or not ctx.scale:
            return None
        win = [f for f in self._guided(ctx, D4_WINDOW_S) if not f.get("settling")]
        if len(win) < 10 or win[-1]["t"] - win[0]["t"] < D4_WINDOW_S * 0.9:
            return None
        hits = {}
        for ax, ms_key in (("ra", "ra_ms"), ("dec", "dec_ms")):
            mx = max((f[ms_key] for f in win), default=0.0)
            if mx < 500:
                continue
            st = gm.axis_stats(win, ax, ms_key, f"{ax}_dir",
                               ctx.rates_px_s[0 if ax == "ra" else 1], mx, ctx.scale)
            if (st.get("response_verdict") == "not moving"
                    and (st.get("at_max_pct") or 0) >= D4_AT_MAX_PCT):
                hits[ax] = st
        if not hits:
            return None
        ax, st = next(iter(hits.items()))
        return Verdict(
            "D4", PULSES,
            f"{ax.upper()}: {st['at_max_pct']:.0f}% of pulses at the max "
            f"({st['at_max']} of {st['pulses']}) for 3 min; commanded "
            f"{abs(st['commanded_arcsec_min']):.0f}\"/min, star moved "
            f"{abs(st['observed_arcsec_min']):.1f}\"/min",
            {ax: {k: st.get(k) for k in ("at_max_pct", "commanded_arcsec_min",
                                         "observed_arcsec_min", "response")}
             for ax, st in hits.items()})

