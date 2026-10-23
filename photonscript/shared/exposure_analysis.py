"""PS-117 part (b): the light budget. How much light a target needs and the
best sub length per rig, filter and target, from the measured sky.

Pure functions (no file or network IO). The graders call `frame_sky` through
shared.star_measure.measure_frame so both record the same `sky_e_s` and
`rn_penalty_pct`; GET /api/target/light-budget and `photonscript
exposure-report` build their tables from the rest.

The noise model (verified on the PS-117 grooming frames: predicted vs
measured sky noise within 1 to 3%), per sub of length t, in electrons:

    per pixel        var = (sky + dark) t + RN^2
    RN penalty       sqrt(1 + RN^2 / ((sky + dark) t)) - 1
                     (how much read noise adds to the per-sub noise)
    sky-limited t    RN^2 / ((sky + dark) ((1 + p)^2 - 1)) for penalty p
    per 2x2 pixel    S t / sqrt((S + sky_sp + 4 dark) t + 4 RN^2)
    (a superpixel:   sky_sp = R + 2G + B per-pixel sky for OSC, 4 x sky mono)
    SNR per hour     snr_sub x sqrt(3600 x acceptance / (t + overhead))
    hours to goal    (goal / SNR per hour)^2
    combine weight   (SNR per sub)^2, relative to a 120 s sub

`S` is the target signal at the faintest feature to show, e-/s per 2x2
pixel of the rig (ImagingProject.feature_signal_e_s, seeded from a measured
profile by `photonscript exposure-report`; M31 outer disk 60' from the core
on the Piggy-600 = 0.19). Progress in SNR terms is (SNR now / goal)^2, the
share of the light the goal needs. Seconds stay the goal unit (PS-30 / 118);
this is advisory (Choice D): nothing here changes a sequence.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

DEFAULT_GOAL_SNR = 20.0       # per 2x2 pixel at the faintest feature (Q2)
CANDIDATE_LENGTHS = (60, 120, 180, 300, 400, 600)
REF_LENGTH = 120              # combine weights are relative to this length
SKY_LIMITED_PCT = 5.0         # RN adds at most this much to per-sub noise
RECOMMEND_WITHIN_PCT = 5.0    # shortest length within this % of the best
SAT_STARS_MAX_PCT = 5.0       # the PS-21 "sat-stars" exposure flag level
DEFAULT_OVERHEAD_S = 30.0     # per sub, when no DATE-OBS gaps are known
MAX_GAP_S = 900.0             # a longer gap is a break, not overhead
MIN_ACCEPT_N = 10             # subs of a length before its acceptance counts
SKY_BLOCK = 16                # sample px per block side (64 native px mono,
                              # 128 native px per CFA channel at step 8)
SKY_DARKEST = 0.10            # sky = median of the darkest 10% of blocks

_BAYER = {
    "RGGB": {(0, 0): "R", (0, 1): "G", (1, 0): "G", (1, 1): "B"},
    "BGGR": {(0, 0): "B", (0, 1): "G", (1, 0): "G", (1, 1): "R"},
    "GRBG": {(0, 0): "G", (0, 1): "R", (1, 0): "B", (1, 1): "G"},
    "GBRG": {(0, 0): "G", (0, 1): "B", (1, 0): "R", (1, 1): "G"},
}


# ------------------------------------------------------------- the camera

@dataclass(frozen=True)
class CameraModel:
    """One rig's camera in one readout mode. ADU are 16-bit."""
    gain_e_adu: float
    read_noise_adu: float
    bias_adu: float = 0.0
    dark_e_s: float = 0.0          # per pixel at the setpoint
    saturation_adu: float = 65000.0
    osc: bool = False
    rig: str = "rc16"
    readout: str = "HCG"

    @property
    def read_noise_e(self) -> float:
        return self.read_noise_adu * self.gain_e_adu

    @property
    def rn2_e(self) -> float:
        return self.read_noise_e ** 2

    def as_dict(self) -> dict:
        d = asdict(self)
        d["read_noise_e"] = round(self.read_noise_e, 3)
        return d

    @classmethod
    def from_config(cls, config, rig: str = "rc16", header=None,
                    readout: str | None = None) -> "CameraModel":
        """The rig's constants from the full config (its shared.rigs
        rig_config view). `header` (READOUTM) or `readout` (a record's raw
        READOUTM, or "LCG" / "HCG") picks the mode; neither = HCG."""
        from photonscript.shared.rigs import rig_config
        if header is None and readout:
            r = str(readout).strip()
            header = {"READOUTM": "Low Conversion Gain"
                      if r.upper() == "LCG" else r}
        return cls.from_view(rig_config(config, rig), rig, header)

    @classmethod
    def from_view(cls, view, rig: str = "rc16", header=None) -> "CameraModel":
        """From a rig's config view (what the graders hold): PS-117 part
        (a) read noise and gain by readout mode, plus camera_bias_adu and
        camera_dark_e_s."""
        from photonscript.shared.rigs import camera_constants
        cc = camera_constants(view, header)
        return cls(gain_e_adu=float(cc["gain_e_adu"] or 1.0),
                   read_noise_adu=float(cc["read_noise_adu"]),
                   bias_adu=float(getattr(view, "camera_bias_adu", 0.0) or 0.0),
                   dark_e_s=float(getattr(view, "camera_dark_e_s", 0.0) or 0.0),
                   saturation_adu=float(getattr(view, "qa_saturation_adu",
                                                65000.0) or 65000.0),
                   osc=(rig == "piggyback"), rig=rig, readout=cc["readout"])


# ------------------------------------------------------------- frame sky

def sky_level(sample: np.ndarray, block: int = SKY_BLOCK,
              darkest: float = SKY_DARKEST) -> float | None:
    """Sky ADU of one channel: block medians, then the median of the
    darkest `darkest` share of the blocks (the grooming measure: a large
    target or nebula does not lift it the way a whole-frame median does).
    Too small for 4 blocks: the plain median."""
    a = np.asarray(sample, dtype=np.float32)
    if a.size == 0:
        return None
    hb, wb = a.shape[0] // block, a.shape[1] // block
    if hb * wb < 4:
        return float(np.median(a))
    v = a[:hb * block, :wb * block].reshape(hb, block, wb, block)
    med = np.median(v.transpose(0, 2, 1, 3).reshape(hb, wb, -1), axis=2)
    flat = np.sort(med.ravel())
    n = max(1, int(round(flat.size * darkest)))
    return float(np.median(flat[:n]))


def bayer_sites(header=None) -> dict:
    """{(row parity, col parity): channel} from BAYERPAT / X/YBAYROFF
    (RGGB when the header has none: the AP26CC's pattern)."""
    get = getattr(header, "get", None)
    pat, xo, yo = "RGGB", 0, 0
    if get is not None:
        try:
            pat = str(get("BAYERPAT") or "RGGB").strip().upper() or "RGGB"
            xo, yo = int(get("XBAYROFF") or 0), int(get("YBAYROFF") or 0)
        except (TypeError, ValueError):
            pass
    base = _BAYER.get(pat, _BAYER["RGGB"])
    return {(r, c): base[((r + yo) % 2, (c + xo) % 2)]
            for r in (0, 1) for c in (0, 1)}


def frame_sky_adu(data: np.ndarray, osc: bool = False, header=None,
                  binned_input: bool = False) -> dict:
    """Sky ADU per channel: {"R", "G", "B"} for a full-resolution OSC
    mosaic (each CFA site sampled every 8th px, so a site never mixes),
    {"L"} for mono, {"mean"} for a 2x2-mean frame (OSC fallback: the
    channels are already mixed)."""
    if binned_input:
        return {"mean" if osc else "L": sky_level(data[::2, ::2])}
    if not osc:
        return {"L": sky_level(data[::4, ::4])}
    acc: dict[str, list] = {}
    for (r, c), ch in bayer_sites(header).items():
        acc.setdefault(ch, []).append(sky_level(data[r::8, c::8]))
    return {ch: float(np.mean([v for v in vals if v is not None]))
            for ch, vals in acc.items() if any(v is not None for v in vals)}


def sky_e_per_s(sky_adu: float | None, exp_s: float | None,
                cam: CameraModel) -> float | None:
    """Sky electrons per second per pixel: (sky - bias) x gain minus the
    dark current, over the exposure. Never negative."""
    if sky_adu is None or not exp_s or exp_s <= 0:
        return None
    e = (float(sky_adu) - cam.bias_adu) * cam.gain_e_adu - cam.dark_e_s * exp_s
    return max(0.0, e / float(exp_s))


def rn_penalty_pct(exp_s: float, sky_e_s: float, cam: CameraModel) -> float | None:
    """How much read noise adds to the per-sub noise of one pixel, %."""
    if exp_s is None or sky_e_s is None or exp_s <= 0:
        return None
    var = (float(sky_e_s) + cam.dark_e_s) * float(exp_s)
    if var <= 0:
        return None
    return 100.0 * (math.sqrt(1.0 + cam.rn2_e / var) - 1.0)


def sky_over_rn2(exp_s: float, sky_e_s: float, cam: CameraModel) -> float | None:
    """Sky electrons per pixel over RN^2 (the grooming's sky-limited index;
    the grader's swamp factor also counts the dark and RN itself)."""
    if exp_s is None or sky_e_s is None or cam.rn2_e <= 0:
        return None
    return float(sky_e_s) * float(exp_s) / cam.rn2_e


def sky_limited_length(sky_e_s: float, cam: CameraModel,
                       pct: float = SKY_LIMITED_PCT) -> float | None:
    """Sub length at which read noise adds `pct` % to per-sub noise."""
    rate = (float(sky_e_s or 0.0) + cam.dark_e_s)
    k = (1.0 + pct / 100.0) ** 2 - 1.0
    if rate <= 0 or k <= 0:
        return None
    return cam.rn2_e / (rate * k)


def frame_sky(data: np.ndarray, header, cam: CameraModel, osc: bool = False,
              binned_input: bool = False) -> dict:
    """The sky fields both graders record (shared.star_measure): sky_adu
    per channel, sky_e_s (the G channel for OSC: the one the penalty is
    quoted for; L for mono), sky_e_s_ch ({R, G, B} e-/s, OSC full
    resolution only) and rn_penalty_pct at the frame's EXPTIME. None
    values without a header exposure time."""
    out = {"sky_adu": None, "sky_e_s": None, "sky_e_s_ch": None,
           "rn_penalty_pct": None}
    get = getattr(header, "get", None)
    try:
        exp_s = float(get("EXPTIME") or 0) if get is not None else 0.0
    except (TypeError, ValueError):
        exp_s = 0.0
    adu = frame_sky_adu(data, osc=osc, header=header,
                        binned_input=binned_input)
    out["sky_adu"] = {k: round(v, 2) for k, v in adu.items() if v is not None}
    if exp_s <= 0 or not out["sky_adu"]:
        return out
    rates = {k: sky_e_per_s(v, exp_s, cam) for k, v in adu.items()}
    if osc and not binned_input:
        out["sky_e_s_ch"] = {k: round(v, 4) for k, v in rates.items()
                             if v is not None}
        main = rates.get("G")
    else:
        main = next(iter(rates.values()), None)
    if main is not None:
        out["sky_e_s"] = round(main, 4)
        p = rn_penalty_pct(exp_s, main, cam)
        out["rn_penalty_pct"] = None if p is None else round(p, 2)
    return out


def record_sky_fields(rec: dict, cam: CameraModel) -> dict:
    """PS-117 backfill for a record graded before the sky fields existed
    (qa-rescore): sky from the stored background median, minus bias and
    dark. For an OSC record that background is a single CFA site (the
    graders sample every 4th pixel), so it is marked sky_src "background"
    and only approximate. {} when the record lacks what is needed."""
    if rec.get("sky_e_s") is not None:
        return {}
    exp_s = rec.get("exp_s")
    bg = rec.get("bg_median", rec.get("background"))
    try:
        exp_s, bg = float(exp_s), float(bg)
    except (TypeError, ValueError):
        return {}
    s = sky_e_per_s(bg, exp_s, cam)
    if s is None:
        return {}
    p = rn_penalty_pct(exp_s, s, cam)
    return {"sky_e_s": round(s, 4),
            "rn_penalty_pct": None if p is None else round(p, 2),
            "sky_src": "background"}


def superpixel_sky(sky_e_s: float | None, sky_e_s_ch: dict | None = None,
                   osc: bool = False) -> float | None:
    """Sky e-/s per 2x2 pixel: R + 2G + B for OSC channels, else 4 x the
    per-pixel sky (an OSC record with only its G sky: 4 x G, slightly
    high, since R and B sit below G)."""
    if sky_e_s_ch and all(k in sky_e_s_ch for k in ("R", "G", "B")):
        return float(sky_e_s_ch["R"]) + 2 * float(sky_e_s_ch["G"]) \
            + float(sky_e_s_ch["B"])
    if sky_e_s is None:
        return None
    return 4.0 * float(sky_e_s)


# ------------------------------------------------------------- SNR model

def snr_sub(signal: float, sky_sp: float, exp_s: float,
            cam: CameraModel) -> float:
    """SNR of one sub at the feature, per 2x2 pixel."""
    t = float(exp_s)
    var = (float(signal) + float(sky_sp) + 4.0 * cam.dark_e_s) * t \
        + 4.0 * cam.rn2_e
    return float(signal) * t / math.sqrt(var) if var > 0 else 0.0


def snr_per_hour(signal: float, sky_sp: float, exp_s: float,
                 cam: CameraModel, overhead_s: float = DEFAULT_OVERHEAD_S,
                 acceptance: float = 1.0) -> float:
    """SNR after one hour of shooting at this length (subs per hour from
    the length plus the per-sub overhead, times the acceptance)."""
    n = 3600.0 * float(acceptance) / (float(exp_s) + float(overhead_s))
    return snr_sub(signal, sky_sp, exp_s, cam) * math.sqrt(max(n, 0.0))


def hours_to_snr(goal_snr: float, snr_h: float) -> float | None:
    if not snr_h or snr_h <= 0:
        return None
    return (float(goal_snr) / float(snr_h)) ** 2


def combine_weights(lengths, sky_sp: float, cam: CameraModel,
                    signal: float = 0.0, ref: float = REF_LENGTH) -> dict:
    """Inverse-variance weight per sub relative to a `ref` s sub: t^2 /
    var(t). Seconds plus a small read-noise bonus, so goals belong in
    seconds (PixInsight's PSF Signal Weight approximates this)."""
    def w(t):
        var = (signal + sky_sp + 4.0 * cam.dark_e_s) * t + 4.0 * cam.rn2_e
        return t * t / var if var > 0 else 0.0
    w0 = w(float(ref))
    return {int(t): (round(w(float(t)) / w0, 3) if w0 else None)
            for t in lengths}


def progress(subs, signal: float, goal_snr: float, cam: CameraModel,
             default_sky_sp: float | None = None) -> dict:
    """SNR reached by accepted subs [(exp_s, sky_sp or None), ...] at the
    feature and the share of the light the goal needs: (SNR now / goal)^2
    in % (may pass 100). A sub without its own sky takes default_sky_sp."""
    tot = 0.0
    n = used = 0
    secs = 0.0
    for exp_s, sky_sp in subs:
        n += 1
        if not exp_s or exp_s <= 0:
            continue
        s = sky_sp if sky_sp is not None else default_sky_sp
        if s is None:
            continue
        tot += snr_sub(signal, s, exp_s, cam) ** 2
        used += 1
        secs += float(exp_s)
    snr = math.sqrt(tot)
    pct = 100.0 * (snr / goal_snr) ** 2 if goal_snr else None
    return {"snr": round(snr, 2), "goal_snr": goal_snr,
            "pct_of_light": None if pct is None else round(pct, 1),
            "subs": n, "subs_used": used, "seconds_used": round(secs)}


# ------------------------------------------------------------- the table

def length_table(cam: CameraModel, sky_e_s: float, sky_sp: float | None,
                 signal: float | None = None,
                 goal_snr: float = DEFAULT_GOAL_SNR,
                 lengths=CANDIDATE_LENGTHS,
                 overhead_s: float = DEFAULT_OVERHEAD_S,
                 acceptance: dict | None = None,
                 sat_stars: dict | None = None,
                 measured: dict | None = None) -> list[dict]:
    """One row per candidate length: RN penalty and sky / RN^2 (per pixel,
    the main channel), and with a signal the SNR per sub, SNR per hour,
    hours to the goal at 100% acceptance and at the measured acceptance
    (`acceptance` {length: share}, used where known), the combine weight
    and the measured saturated-star % ({length: pct}). `measured` {length:
    n subs} marks lengths the target has data for."""
    acceptance = acceptance or {}
    sat_stars = sat_stars or {}
    measured = measured or {}
    weights = (combine_weights(lengths, sky_sp, cam, signal or 0.0)
               if sky_sp is not None else {})
    rows = []
    for t in lengths:
        t = int(t)
        pen = rn_penalty_pct(t, sky_e_s, cam)
        ratio = sky_over_rn2(t, sky_e_s, cam)
        row = {"exp_s": t, "rn_penalty_pct": _r(pen, 1),
               "sky_over_rn2": _r(ratio, 1),
               "sky_limited": pen is not None and pen <= SKY_LIMITED_PCT,
               "weight_vs_120": weights.get(t),
               "acceptance": acceptance.get(t),
               "sat_stars_pct": sat_stars.get(t),
               "measured_subs": measured.get(t, 0),
               "snr_sub": None, "snr_per_hour": None, "hours_to_goal": None,
               "hours_at_acceptance": None}
        if signal and sky_sp is not None:
            sh = snr_per_hour(signal, sky_sp, t, cam, overhead_s)
            row["snr_sub"] = _r(snr_sub(signal, sky_sp, t, cam), 3)
            row["snr_per_hour"] = _r(sh, 2)
            row["hours_to_goal"] = _r(hours_to_snr(goal_snr, sh), 1)
            a = acceptance.get(t)
            if a:
                row["hours_at_acceptance"] = _r(
                    hours_to_snr(goal_snr, sh) / a, 1)
        rows.append(row)
    return rows


def recommend_length(rows: list[dict], cam: CameraModel,
                     max_length: float | None = None,
                     within_pct: float = RECOMMEND_WITHIN_PCT,
                     sat_stars_max: float = SAT_STARS_MAX_PCT,
                     default_acceptance: float | None = None) -> dict:
    """Pick a length from length_table rows, with plain-language reasons.

    With SNR numbers: score = SNR per hour x the length's acceptance (when
    known for that length, else `default_acceptance`, the target's pooled
    share, so an unmeasured length is not favoured over a measured one;
    None = neutral); lengths over `max_length` (rig
    cap, core clip) or with saturated stars over `sat_stars_max` % are out;
    the SHORTEST length within `within_pct` % of the best score wins (less
    lost per ruined sub). Without a signal: the shortest sky-limited
    length (RN adds at most SKY_LIMITED_PCT %)."""
    ok = [r for r in rows
          if (max_length is None or r["exp_s"] <= max_length)
          and not (r.get("sat_stars_pct") is not None
                   and r["sat_stars_pct"] > sat_stars_max)]
    if not ok:
        return {"exp_s": None, "basis": "none",
                "reasons": ["no candidate length passes the limits"]}
    scored = [r for r in ok if r.get("snr_per_hour")]
    if not scored:
        sl = [r for r in ok if r["sky_limited"]]
        pick = sl[0] if sl else ok[-1]
        why = [f"sky-limited from {pick['exp_s']} s (read noise adds "
               f"{pick['rn_penalty_pct']}%, sky {pick['sky_over_rn2']} x RN^2)"
               if sl else f"not sky-limited at any candidate: longest "
               f"allowed ({pick['exp_s']} s) adds {pick['rn_penalty_pct']}% "
               f"read noise"]
        why.append("no feature signal set: SNR per hour and hours to goal "
                   "need one (exposure-report)")
        return {"exp_s": pick["exp_s"], "basis": "sky-limited", "reasons": why}

    def score(r):
        a = r.get("acceptance") or default_acceptance
        return r["snr_per_hour"] * (a if a else 1.0)
    best = max(scored, key=score)
    floor = score(best) * (1.0 - within_pct / 100.0)
    pick = min((r for r in scored if score(r) >= floor),
               key=lambda r: r["exp_s"])
    why = []
    if pick["sky_limited"]:
        why.append(f"sky-limited (sky {pick['sky_over_rn2']} x RN^2, read "
                   f"noise adds {pick['rn_penalty_pct']}%)")
    else:
        why.append(f"read noise still adds {pick['rn_penalty_pct']}% per sub")
    if pick is best:
        why.append("best SNR per hour of the candidates")
    else:
        gap = 100.0 * (1.0 - score(pick) / score(best))
        less = 100.0 * (1.0 - pick["exp_s"] / best["exp_s"])
        why.append(f"{gap:.0f}% from the best SNR per hour ({best['exp_s']} s)"
                   f", {less:.0f}% less lost per ruined sub")
    if pick.get("sat_stars_pct") is not None:
        why.append(f"saturated stars {pick['sat_stars_pct']}%")
    if any(r.get("acceptance") for r in scored):
        why.append("weighted by the measured acceptance per length")
    if max_length is not None:
        why.append(f"capped at {int(max_length)} s")
    return {"exp_s": pick["exp_s"], "basis": "snr", "reasons": why,
            "best_exp_s": best["exp_s"],
            "snr_per_hour": pick["snr_per_hour"],
            "hours_to_goal": pick["hours_to_goal"]}


def headline(rec: dict) -> str:
    """'Recommended 300 s: reason; reason' for the Targets page."""
    if not rec or rec.get("exp_s") is None:
        return "no recommendation: " + "; ".join((rec or {}).get(
            "reasons", ["no data"]))
    return f"Recommended {rec['exp_s']} s: " + "; ".join(rec["reasons"])


# ------------------------------------------------------------- sub data

def parse_time(s):
    """A record time (DATE-OBS, or the live grader's UTC ISO + 'Z') as a
    naive UTC datetime; None when unreadable. NINA writes 7 fractional
    digits, which fromisoformat (3.10) refuses: trimmed to 6."""
    from datetime import datetime
    if not s:
        return None
    t = str(s).strip().rstrip("Z")
    if "." in t:
        head, frac = t.split(".", 1)
        t = head + "." + frac[:6]
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d.replace(tzinfo=None)


def overhead_from_subs(subs) -> dict:
    """Per-sub overhead from consecutive subs [(night, time, exp_s), ...]
    of one rig: gap = start-to-start minus the earlier sub's length, only
    between subs of the same night and length, from 0 up to the sub length
    (and MAX_GAP_S): a longer gap is a break or hides a missing sub (a
    Library folder holds only the kept ones). Mean is what the SNR per hour
    uses (autofocus and recenters included); median is the typical gap."""
    by_night: dict = {}
    for night, ts, exp_s in subs:
        d = parse_time(ts)
        if d is None or not exp_s:
            continue
        by_night.setdefault(night, []).append((d, float(exp_s)))
    gaps = []
    for vals in by_night.values():
        vals.sort()
        for (d0, e0), (d1, e1) in zip(vals, vals[1:]):
            if e0 != e1:
                continue
            g = (d1 - d0).total_seconds() - e0
            if 0 <= g <= min(MAX_GAP_S, e0):
                gaps.append(g)
    if not gaps:
        return {"n": 0, "mean_s": None, "median_s": None,
                "used_s": DEFAULT_OVERHEAD_S, "source": "default"}
    mean = float(np.mean(gaps))
    return {"n": len(gaps), "mean_s": round(mean, 1),
            "median_s": round(float(np.median(gaps)), 1),
            "used_s": round(mean, 1), "source": "measured"}


def acceptance_by_length(subs, min_n: int = MIN_ACCEPT_N) -> dict:
    """{length: {"n", "accepted", "share", "used"}} from [(exp_s,
    passed), ...]; `used` once a length has min_n subs."""
    out: dict = {}
    for exp_s, passed in subs:
        if not exp_s:
            continue
        t = int(round(float(exp_s)))
        d = out.setdefault(t, {"n": 0, "accepted": 0})
        d["n"] += 1
        d["accepted"] += int(bool(passed))
    for d in out.values():
        d["share"] = round(d["accepted"] / d["n"], 3) if d["n"] else None
        d["used"] = d["n"] >= min_n
    return out


def percentiles(vals, ps=(10, 50, 90)) -> dict | None:
    v = [float(x) for x in vals if x is not None]
    if not v:
        return None
    a = np.asarray(v)
    return {f"p{p}": round(float(np.percentile(a, p)), 4) for p in ps} | {
        "n": len(v)}


def _r(v, nd):
    return None if v is None else round(float(v), nd)
