"""PS-113: calibration frame QA, the quarantine, and the day vs night leak check.

Every BIAS / DARK / FLAT frame is judged before it may count or enter a
master. Checks (reason codes in brackets):

  every type   [header]  IMAGETYP matches the folder, EXPTIME / GAIN / OFFSET
                         present, bias <= 0.01 s, dark > 0.01 s; in a capture
                         job also exposure / gain / offset / binning as asked
  dark, bias   [temp]    CCD-TEMP present and within SET-TEMP +/- tol
                         (calibration_temp_tol_c, 1 C); flats: warning only
  dark         [level]   median minus the bias level of its epoch inside
                         [-BIAS_LOW_ADU, limit]; the limit is the per-camera
                         dark model's (expected excess for the length from
                         accepted darks of other lengths and earlier nights
                         of the same length, plus an allowance; see
                         dark_expectation), else base + rate x exposure x
                         2^(T/6) while the model has no anchors;
                         the bias is the QA-clean session in force at the
                         dark's date (SET-TEMP matched), named in the reason
                         (date, median, frames). When a newer bias drifted
                         more than calibration_bias_drift_adu (8) from it and
                         explains the dark, it is [bias_drift] (a warning,
                         level_verdict "bias drift"), not light
  dark, bias   [leak]    center vs corner medians differ by > LEAK_ADU
  dark, bias   [stars]   more than MAX_STARS real stars (the graders' sep
                         recipe: 3x3 median, 5 sigma, minarea 6, a >= 0.6)
  dark, bias   [outlier] level off the set's own median (sets of 5 or more)
  dark         [trend]   level changes > TREND_ADU through the set (light
                         that follows the sky); whole set
  dark         [gradient] zone pattern off the set's own pattern; a [leak]
                         pattern every frame of the set shares is the
                         sensor's fixed pattern (zone_pattern warning)
               [noise]   MAD over NOISE_RATIO x the set's median MAD
  flat         [flat_level] median outside FLAT_MIN_FRAC..FLAT_MAX_FRAC of
                         65535; [saturated] over FLAT_SAT_PCT % of pixels at
                         saturation; [vignetting] corners not darker than the
                         center by VIGNETTE_MIN (fail on the Piggy-600 where
                         the falloff is large, warning on the RC16 whose dusk
                         sky gradient can mask it)
  dark         [daytime_leak] daytime darks (sun up) brighter than night
                         darks (sun below -12) of the same epoch
                         (check_daytime); disables daytime capture for that
                         rig until reset

The library on the scope is PhotonScript-owned (hardlinks of NINA's frames),
so a failing frame's Library link is MOVED (a rename, so Syncthing ships a
move) to <lib>/Calibration/_quarantine/<TYPE>/<date>/ with the reasons in
reasons.json beside it. NINA's originals in the watch dir are never touched.
calibration_health / readiness never scan _quarantine, and the desktop's
prepare-integration scripts read only Calibration\\{DARK,BIAS,FLAT}.

PS-128: every record carries the frame's readout mode ("readout", normalized
HCG / LCG from READOUTM, raw text in "readout_raw"; None when the header has
none). Darks and bias count only at the rig's readout (rig_readout); a frame
with no readout keyword is assumed to be at it (frame_readout says so).
Records from before PS-128 get the field from a header-only read on the next
pass (calibration-qa --backfill, --dry-run to move nothing): no re-measuring.

Store: <data_dir>/calibration_qa/<rig>.json, one record per frame keyed
"<TYPE>/<date>/<name>" (the calibration scan's own dedupe key). Measurements
are cached by file size and QA_VERSION; verdicts are recomputed on every pass
(set checks need the whole set). daytime.json holds the per-rig daytime
capture state (untested | ok | disabled).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

QA_VERSION = 1

# --- thresholds (module constants; the reasons quote them) -------------------
BIAS_LOW_ADU = 3.0            # a dark this far below its bias is broken
DARK_EXCESS_BASE_ADU = 10.0   # dark minus bias allowed at 0 s ...
DARK_EXCESS_ADU_PER_S = 0.02  # ... plus this per second at 0 C (AARO 0 C
                              # darks: 120 s +5..8, 600 s +3..7 ADU)
DARK_DOUBLING_C = 6.0         # dark current doubles every ~6 C
LEAK_ADU = 3.0                # center vs any corner (good frames: < 1 ADU)
MAX_STARS = 30                # good darks: 0 to 8 (hot pixel clusters); a
                              # roof-open dark: 120 to 190
OUTLIER_LEVEL_ADU = 3.0
OUTLIER_MAD_K = 4.0
NOISE_RATIO = 1.5
SET_MIN = 5                   # set checks need this many frame-clean frames
FLAT_MIN_FRAC, FLAT_MAX_FRAC = 0.20, 0.80
FLAT_SAT_PCT = 0.05
VIGNETTE_MIN = 0.03
RIG_FLAT_VIGNETTING = {"rc16": "warn", "piggyback": "fail"}
DAY_SUN_ALT = 0.0             # a frame is "daytime" when the sun is up ...
NIGHT_SUN_ALT = -12.0         # ... and a night reference below nautical dusk
                              # (twilight frames are neither)
DAY_MIN_FRAMES = 3            # day darks needed before the check decides
DAYTIME_LEAK_ADU = 3.0        # day darks this much above night darks = light
DAYTIME_LIGHT_FRAC = 0.3      # or this share of day darks failing light checks
LIGHT_CODES = ("leak", "stars", "level", "daytime_leak", "trend",
               "gradient")
MODES = ("off", "report", "quarantine")
QUARANTINE_DIR = "_quarantine"

_lock = threading.RLock()


# --- store -------------------------------------------------------------------

def qa_dir(config) -> Path:
    return Path(config.data_dir) / "calibration_qa"


def _store_path(config, rig: str) -> Path:
    return qa_dir(config) / f"{rig}.json"


class _FileLock:
    """Cross-process lock (the CLI backfill and the service can both write
    the store): an O_EXCL lock file, stale after 10 min."""

    def __init__(self, path: Path, wait_s: float = 60.0):
        self.path = path
        self.wait_s = wait_s

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait_s
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 600:
                        self.path.unlink()
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"calibration QA store locked: {self.path}")
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def load_store(config, rig: str) -> dict:
    p = _store_path(config, rig)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("frames"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": QA_VERSION, "rig": rig, "frames": {}}


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    from photonscript.shared.fileops import replace_retry
    replace_retry(tmp, p)   # Windows AV / indexer may hold the file a moment


def save_store(config, rig: str, store: dict) -> None:
    store["updated"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _write_json(_store_path(config, rig), store)


def has_store(config, rig: str) -> bool:
    return bool(load_store(config, rig)["frames"])


def frame_key(typ: str, date: str, name: str) -> str:
    return f"{typ}/{date}/{name}"


def mode(config) -> str:
    m = str(getattr(config, "calibration_qa_mode", "quarantine") or "").strip().lower()
    return m if m in MODES else "quarantine"


def temp_tol(config) -> float:
    try:
        return float(getattr(config, "calibration_temp_tol_c", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0


# --- daytime state -------------------------------------------------------------

def _daytime_path(config) -> Path:
    return qa_dir(config) / "daytime.json"


def daytime_state(config, rig: str | None = None) -> dict:
    try:
        data = json.loads(_daytime_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if rig is None:
        return data
    return data.get(rig) or {"status": "untested"}


def set_daytime_state(config, rig: str, status: str, **extra) -> dict:
    with _lock, _FileLock(qa_dir(config) / "daytime.lock"):
        data = daytime_state(config)
        rec = {"status": status,
               "at": datetime.utcnow().isoformat(timespec="seconds") + "Z", **extra}
        data[rig] = rec
        _write_json(_daytime_path(config), data)
    return rec


def daytime_capture_allowed(config, rig: str) -> bool:
    return daytime_state(config, rig).get("status") != "disabled"


# --- measurement -----------------------------------------------------------------

def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _norm_imagetyp(v) -> str:
    s = str(v or "").strip().upper().replace(" FRAME", "")
    return {"BIASES": "BIAS", "DARKS": "DARK", "FLATS": "FLAT",
            "FLAT FIELD": "FLAT", "DARK CURRENT": "DARK"}.get(s, s)


def header_fields(hdr) -> dict:
    from photonscript.shared.optics_state import optics_fields
    from photonscript.shared.rigs import header_readout
    ro, ro_raw = header_readout(hdr)
    return {"imagetyp": _norm_imagetyp(hdr.get("IMAGETYP")),
            "readout": ro, "readout_raw": ro_raw,
            "exptime": _num(hdr.get("EXPTIME", hdr.get("EXPOSURE"))),
            "gain": _num(hdr.get("GAIN")),
            "offset": _num(hdr.get("OFFSET")),
            "settemp": _num(hdr.get("SET-TEMP")),
            "ccdtemp": _num(hdr.get("CCD-TEMP")),
            "xbin": int(_num(hdr.get("XBINNING")) or 1),
            "filter": (str(hdr.get("FILTER")).strip() or None)
            if hdr.get("FILTER") is not None else None,
            "instrume": str(hdr.get("INSTRUME") or "").strip() or None,
            "date_obs": str(hdr.get("DATE-OBS") or "").strip() or None,
            "bayer": str(hdr.get("BAYERPAT") or "").strip() or None,
            # PS-164: focuser position / rotator angle (None when absent)
            **optics_fields(hdr)}


def _zones(binned) -> dict:
    """Median of the center box and the four corner boxes (1/8 of each
    side) on the 2x2-binned frame."""
    import numpy as np
    h, w = binned.shape
    bh, bw = max(2, h // 8), max(2, w // 8)
    c = float(np.median(binned[h // 2 - bh // 2:h // 2 + bh // 2,
                               w // 2 - bw // 2:w // 2 + bw // 2]))
    corners = [float(np.median(binned[:bh, :bw])), float(np.median(binned[:bh, -bw:])),
               float(np.median(binned[-bh:, :bw])), float(np.median(binned[-bh:, -bw:]))]
    return {"center": round(c, 2), "corners": [round(x, 2) for x in corners]}


def _star_count(binned) -> int | None:
    """Real stars by the graders' recipe (runs._measure): sep on a 3x3
    median-filtered, background-subtracted frame, so hot pixels vanish."""
    import numpy as np
    from photonscript.scheduler.runs import _sep_module
    sep = _sep_module()
    if sep is None:
        return None
    from scipy import ndimage
    data = np.ascontiguousarray(binned, dtype=np.float32)
    bkg = sep.Background(data)
    sub = data - bkg
    err = np.maximum(bkg.rms(), max(float(bkg.globalrms) * 0.2, 1e-3))
    det = ndimage.median_filter(sub, size=3)
    try:
        sep.set_extract_pixstack(1_000_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        objs = sep.extract(det, 5.0, err=err, minarea=6, clean=True)
    except Exception:  # noqa: BLE001 - pixel buffer overflow: lots of "stars"
        return 100000
    if not len(objs):
        return 0
    return int(((objs["a"] >= 0.6) & (objs["b"] > 0)).sum())


def measure(path: Path, config=None) -> dict:
    """Header + pixel metrics of one frame (one full read, under the
    graders' _HEAVY lock: the scope PC is RAM-tight)."""
    from photonscript.scheduler.runs import _HEAVY, _load_binned
    sat = float(getattr(config, "qa_saturation_adu", 65000.0) or 65000.0)
    st: dict = {}
    with _HEAVY:
        hdr, binned = _load_binned(Path(path), stats=st, sat_adu=sat)
        z = _zones(binned)
        stars = _star_count(binned)
        del binned
    out = header_fields(hdr)
    out.update({"median": st.get("bg_median"), "mad": st.get("bg_mad"),
                "sat_pct": st.get("sat_px_pct"), "max_adu": st.get("max_adu"),
                "sat_adu": st.get("sat_adu"), "stars": stars, **z})
    return out


# --- judging ---------------------------------------------------------------------

def _epoch(rec: dict) -> tuple:
    return (rec.get("gain"), rec.get("offset"), rec.get("xbin") or 1,
            rec.get("instrume"), rec.get("readout"))


def frame_readout(rec: dict, default: str | None,
                  since: str | None = None) -> tuple[str | None, bool]:
    """PS-128: (readout mode, assumed) of a QA record. A record with no
    readout (no header keyword, or measured before PS-128 and not
    backfilled yet) takes the rig default and is flagged assumed.
    PS-182: unless it is older than `since` (rigs.rig_readout_since): then
    its mode is unknown (None, True) and it matches no mode."""
    ro = rec.get("readout")
    if ro:
        return ro, False
    if since and str(rec.get("date") or "") < since:
        return None, True
    return default, True


def readout_matches(rec: dict, want: str | None, default: str | None,
                    since: str | None = None) -> bool:
    """PS-128: does a record count at readout `want`? want None = readout
    not matched (camera_readout_mode blank)."""
    if not want:
        return True
    return frame_readout(rec, default, since)[0] == want


SETTEMP_TOL_C = 1.5   # PS-182: one SET-TEMP match for every counter and report


def usable_misses(rec: dict, epoch: dict, *, default_ro: str | None = None,
                  since: str | None = None) -> list[str]:
    """PS-182: the one rule for "can this DARK / BIAS calibrate lights of
    `epoch`" (calibration.dark_epoch shape: gain, offset, setpoint,
    readout). [] = usable. The owed view, the night quota, the library
    report and the completeness model all count with it: gain and offset
    equal, SET-TEMP within SETTEMP_TOL_C of the setpoint, readout equal
    (a frame with none recorded is the rig's unless older than `since`)."""
    out = []
    g, o = _num(rec.get("gain")), _num(rec.get("offset"))
    if epoch.get("gain") is not None and g is not None and g != float(epoch["gain"]):
        out.append(f"gain {g:g} vs {epoch['gain']}")
    if epoch.get("offset") is not None and o is not None and o != float(epoch["offset"]):
        out.append(f"offset {o:g} vs {epoch['offset']}")
    st, sp = _num(rec.get("settemp")), epoch.get("setpoint")
    if sp is not None and st is not None and abs(st - float(sp)) >= SETTEMP_TOL_C:
        out.append(f"SET-TEMP {st:g} C vs setpoint {float(sp):g} C")
    want = epoch.get("readout")
    if want:
        ro, _assumed = frame_readout(rec, want if default_ro is None else default_ro,
                                     since)
        if ro is None:
            out.append(f"readout unknown (no READOUTM recorded, shot before "
                       f"{since}, the rig's {want} start)")
        elif ro != want:
            out.append(f"readout {ro} vs {want}")
    return out


def _set_key(rec: dict) -> tuple:
    return (rec.get("type"), rec.get("date"), round(rec.get("exptime") or 0, 1),
            *_epoch(rec), rec.get("settemp"))


def dark_excess_limit(exp_s: float, temp_c: float | None, scale: float = 1.0) -> float:
    """Dark minus bias allowed (ADU). PS-182: `scale` multiplies the dark
    current term for a readout mode with more ADU per electron than the
    LCG frames DARK_EXCESS_ADU_PER_S was measured on (AP26MC HCG: 0.79 /
    0.25 = 3.16 x)."""
    t = 0.0 if temp_c is None else float(temp_c)
    return (DARK_EXCESS_BASE_ADU
            + DARK_EXCESS_ADU_PER_S * float(exp_s or 0) * 2 ** (t / DARK_DOUBLING_C)
            * float(scale or 1.0))


def dark_excess_scales(config, rig: str) -> dict:
    """PS-182: {readout: dark-current scale} for judge_all. The RC16 AP26MC
    at HCG converts 0.25 e-/ADU (camera_gain_e_adu) against 0.79 at LCG
    (camera_gain_lcg_e_adu), so the same dark current reads 3.16 x more
    ADU: the 600 s HCG darks of 2026-10-05 / 10-06 sat 28 to 46 ADU over
    bias and failed a limit of 22 ADU meant for LCG. {} (no scaling) when
    calibration_qa_dark_scale_by_readout is off, for the Piggy-600 (LCG
    only) or when the gains are not set."""
    if rig != "rc16" or not getattr(config, "calibration_qa_dark_scale_by_readout", True):
        return {}
    try:
        hcg = float(getattr(config, "camera_gain_e_adu", 0.0) or 0.0)
        lcg = float(getattr(config, "camera_gain_lcg_e_adu", 0.0) or 0.0)
    except (TypeError, ValueError):
        return {}
    if hcg <= 0 or lcg <= 0:
        return {}
    return {"HCG": round(lcg / hcg, 2), "LCG": 1.0}


def judge_frame(rec: dict, *, rig: str, tol: float = 1.0,
                bias_ref: tuple | None = None,
                setpoint: float | None = None,
                excess_scale: float = 1.0,
                bias_alt: tuple | None = None,
                expect: tuple | None = None) -> tuple[list, list]:
    """Per-frame checks. Returns (fails, warnings): lists of
    {"code", "detail"}. bias_ref = (level, source) for darks;
    excess_scale (PS-182) scales the dark-current allowance for the
    frame's readout mode (dark_excess_scales). bias_alt = (level, source,
    drift): a newer bias session that drifted from bias_ref; a dark that
    fails the level check against bias_ref but sits inside the allowance
    over bias_alt is "bias drift" (a bias_drift warning, not light).
    Darks record "level_verdict": None (level fine), "light" or
    "bias drift". expect = (expected excess, limit, source) from the
    per-camera dark model (dark_expectation): the upper level limit is then
    the model's instead of the generic dark_excess_limit formula."""
    fails: list[dict] = []
    warns: list[dict] = []
    typ = rec.get("type")

    def bad(code, detail, warn=False):
        (warns if warn else fails).append({"code": code, "detail": detail})

    # header
    it = rec.get("imagetyp")
    if it and it != typ:
        bad("header", f"IMAGETYP {it} in a {typ} folder")
    for k in ("exptime", "gain", "offset"):
        if rec.get(k) is None:
            bad("header", f"{k.upper()} missing")
    exp = rec.get("exptime")
    if exp is not None:
        if typ == "BIAS" and exp > 0.01:
            bad("header", f"bias EXPTIME {exp:g} s > 0.01 s")
        if typ == "DARK" and exp <= 0.01:
            bad("header", f"dark EXPTIME {exp:g} s")
    exp_set = rec.get("expect")
    if exp_set:
        for k, label in (("gain", "GAIN"), ("offset", "OFFSET"), ("xbin", "XBINNING")):
            if exp_set.get(k) is not None and rec.get(k) is not None \
                    and float(rec[k]) != float(exp_set[k]):
                bad("header", f"{label} {rec[k]:g} != {exp_set[k]:g} asked")
        want_ro = exp_set.get("readout")
        if want_ro and rec.get("readout") and rec["readout"] != want_ro:
            bad("header", f"READOUTM {rec.get('readout_raw') or rec['readout']} "
                f"!= {want_ro} asked (NINA's camera readout mode differs from "
                "the lights')")
        want = exp_set.get("exposures")
        if typ == "DARK" and want and exp is not None \
                and not any(abs(exp - w) < 0.5 for w in want):
            bad("header", f"EXPTIME {exp:g} s not one of {want}")

    # temperature
    ccd, st = rec.get("ccdtemp"), rec.get("settemp")
    ref = st if st is not None else setpoint
    flat = typ == "FLAT"
    if ccd is None:
        bad("temp", "CCD-TEMP missing", warn=flat)
    elif ref is None:
        bad("temp", "SET-TEMP missing and no setpoint", warn=True)
    else:
        if st is None:
            bad("temp", f"SET-TEMP missing, judged against setpoint {ref:g} C", warn=True)
        if abs(ccd - ref) > tol:
            bad("temp", f"CCD-TEMP {ccd:g} C vs set {ref:g} C (tol {tol:g})", warn=flat)

    med = rec.get("median")
    if typ in ("DARK", "BIAS") and med is not None:
        if typ == "DARK":
            rec["level_verdict"] = None
        if typ == "DARK" and bias_ref is not None and bias_ref[0] is not None:
            excess = med - bias_ref[0]
            rec["dark_excess"] = round(excess, 2)
            if expect is not None:
                lim = expect[1]
                allowed = (f"allowed {lim:.1f} for {exp or 0:g} s (expected "
                           f"{expect[0]:+.1f} from {expect[2]})"
                           + (f"; {expect[3]}" if len(expect) > 3 and expect[3]
                              else ""))
            else:
                lim = dark_excess_limit(exp or 0, ref, excess_scale)
                allowed = (f"allowed {lim:.1f} for {exp or 0:g} s"
                           + (f" (x{excess_scale:g} for {rec.get('readout')})"
                              if excess_scale and excess_scale != 1.0 else ""))
            rec["dark_limit"] = round(lim, 2)
            msg = None
            if excess < -BIAS_LOW_ADU:
                msg = (f"median {med:g} is {-excess:.1f} ADU below bias "
                       f"{bias_ref[0]:g} ({bias_ref[1]})")
            elif excess > lim:
                msg = (f"median {med:g} is {excess:.1f} ADU over bias "
                       f"{bias_ref[0]:g} ({bias_ref[1]}); {allowed}")
            if msg:
                alt_ex = (None if not bias_alt or bias_alt[0] is None
                          else med - bias_alt[0])
                if alt_ex is not None and -BIAS_LOW_ADU <= alt_ex <= lim:
                    rec["level_verdict"] = "bias drift"
                    rec["dark_excess"] = round(alt_ex, 2)
                    bad("bias_drift", f"{msg}, but {alt_ex:.1f} ADU over the newer "
                        f"bias {bias_alt[0]:g} ({bias_alt[1]}; drift "
                        f"{bias_alt[2]:+.1f} ADU): bias drift, not light", warn=True)
                else:
                    rec["level_verdict"] = "light" if excess > 0 else "below bias"
                    if alt_ex is not None:
                        msg += (f"; the newer bias {bias_alt[0]:g} ({bias_alt[1]}) "
                                f"does not explain it either ({alt_ex:.1f} ADU over)")
                    bad("level", msg)
        c, corners = rec.get("center"), rec.get("corners") or []
        if c is not None and corners:
            spread = max(abs(x - c) for x in corners)
            if spread > LEAK_ADU:
                bad("leak", f"corner vs center {spread:.1f} ADU (> {LEAK_ADU:g}): "
                    f"center {c:g}, corners {', '.join(f'{x:g}' for x in corners)}")
        stars = rec.get("stars")
        if stars is not None and stars > MAX_STARS:
            bad("stars", f"{stars} stars detected (> {MAX_STARS})")
    if flat and med is not None:
        full = 65535.0
        lo, hi = FLAT_MIN_FRAC * full, FLAT_MAX_FRAC * full
        if not lo <= med <= hi:
            bad("flat_level", f"median {med:g} ADU outside {lo:.0f}..{hi:.0f}")
        sp = rec.get("sat_pct")
        if sp is not None and sp > FLAT_SAT_PCT:
            bad("saturated", f"{sp:g} % of pixels saturated (> {FLAT_SAT_PCT:g} %)")
        c, corners = rec.get("center"), rec.get("corners") or []
        if c and corners:
            fall = 1.0 - (sum(corners) / len(corners)) / c
            rec["vignetting"] = round(fall, 3)
            if fall < VIGNETTE_MIN:
                bad("vignetting", f"corners only {fall * 100:.1f} % darker than "
                    f"the center (< {VIGNETTE_MIN * 100:g} %): not a sky flat "
                    "through the optics?",
                    warn=RIG_FLAT_VIGNETTING.get(rig, "warn") != "fail")
    return fails, warns


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


BIAS_DRIFT_ADU = 8.0   # a bias session this far from the one before = drift


def bias_drift_adu(config) -> float:
    """calibration_bias_drift_adu (0 / unset / bad = BIAS_DRIFT_ADU)."""
    try:
        v = float(getattr(config, "calibration_bias_drift_adu", BIAS_DRIFT_ADU) or 0)
    except (TypeError, ValueError):
        v = 0.0
    return v if v > 0 else BIAS_DRIFT_ADU


def _bias_clean(r: dict) -> bool:
    return not any(f["code"] in ("temp", "header", "leak", "stars")
                   for f in r.get("frame_fails") or [])


def bias_sessions(recs: dict) -> dict:
    """{epoch: [session, ...] oldest first} of the QA-clean bias (no temp /
    header / leak / stars fail); a session is one date folder at one
    SET-TEMP: {"date", "median" (of the frames' medians), "n", "settemp"}."""
    groups: dict[tuple, dict[tuple, list]] = {}
    for r in recs.values():
        if r.get("type") != "BIAS" or r.get("median") is None or not _bias_clean(r):
            continue
        st = _num(r.get("settemp"))
        groups.setdefault(_epoch(r), {}).setdefault(
            (r.get("date") or "", None if st is None else round(st)), []).append(r)
    out: dict[tuple, list] = {}
    for ep, by in groups.items():
        rows = [{"date": date, "median": _median([x["median"] for x in rs]),
                 "n": len(rs), "settemp": _median([_num(x.get("settemp")) for x in rs])}
                for (date, _t), rs in by.items()]
        out[ep] = sorted(rows, key=lambda x: (x["date"], x["n"]))
    return out


def bias_label(s: dict) -> str:
    """How a dark's light check names the bias it compared against."""
    return f"bias {s['date']}, median {s['median']:g} of {s['n']} frames"


def _same_temp(rows: list, settemp) -> list:
    st = _num(settemp)
    if st is None:
        return rows
    same = [x for x in rows if x.get("settemp") is not None
            and abs(x["settemp"] - st) < SETTEMP_TOL_C]
    return same or rows


def _epoch_rows(rec: dict, sessions: dict) -> list:
    ep = _epoch(rec)
    if ep in sessions:
        return sessions[ep]
    # same gain/offset on a camera whose INSTRUME string differs (or, PS-128,
    # whose readout mode differs: HCG and LCG bias both sit at the OFFSET
    # pedestal, 254 to 256 ADU on the AP26MC): still close. Same readout first.
    for e, rows in sessions.items():
        if e[:3] == ep[:3] and e[4] == rec.get("readout"):
            return rows
    for e, rows in sessions.items():
        if e[:3] == ep[:3]:
            return rows
    return []


def _bias_pick(rec: dict, sessions: dict, drift_adu: float = BIAS_DRIFT_ADU):
    """(in force, newer) bias sessions for a dark. In force: the newest
    QA-clean session of its epoch (SET-TEMP matched when one is) shot on
    or before the dark's date, else the oldest after it, so a September
    dark keeps its September bias when the level later drifts. Newer: the
    newest session after that one when it sits more than drift_adu away
    (the bias level drifted since), else None."""
    rows = _same_temp(_epoch_rows(rec, sessions), rec.get("settemp"))
    if not rows:
        return None, None
    d = str(rec.get("date") or "")
    before = [x for x in rows if x["date"] <= d]
    cur = before[-1] if before else rows[0]
    new = rows[-1]
    if new is cur or new["date"] <= cur["date"] or new["median"] is None \
            or cur["median"] is None or abs(new["median"] - cur["median"]) <= drift_adu:
        new = None
    return cur, new


def _bias_ref(rec: dict, sessions: dict, drift_adu: float = BIAS_DRIFT_ADU):
    """(bias_ref, bias_alt) for judge_frame."""
    cur, new = _bias_pick(rec, sessions, drift_adu)
    if cur is not None:
        alt = None if new is None else (new["median"], bias_label(new),
                                        new["median"] - cur["median"])
        return (cur["median"], bias_label(cur)), alt
    if rec.get("offset") is not None:
        return (float(rec["offset"]), "OFFSET keyword (no bias QA'd yet)"), None
    return None, None


def bias_drift(recs: dict) -> list:
    """Per epoch and SET-TEMP: the newest QA-clean bias session against the
    one before it. [{"gain", "offset", "xbin", "instrume", "readout",
    "settemp", "date", "median", "n", "prev_date", "prev_median",
    "prev_n", "drift"}]; "drift" is None without an earlier session."""
    out = []
    for ep, rows in bias_sessions(recs).items():
        by_t: dict = {}
        for x in rows:
            by_t.setdefault(None if x["settemp"] is None else round(x["settemp"]),
                            []).append(x)
        for t, rs in by_t.items():
            new, prev = rs[-1], (rs[-2] if len(rs) > 1 else None)
            drift = (None if prev is None or prev["median"] is None
                     or new["median"] is None
                     else round(new["median"] - prev["median"], 1))
            out.append({"gain": ep[0], "offset": ep[1], "xbin": ep[2],
                        "instrume": ep[3], "readout": ep[4], "settemp": t,
                        "date": new["date"], "median": new["median"], "n": new["n"],
                        "prev_date": prev["date"] if prev else None,
                        "prev_median": prev["median"] if prev else None,
                        "prev_n": prev["n"] if prev else None, "drift": drift})
    return out


def drift_for(config, rig: str, *, gain, offset, readout=None, setpoint=None,
              store: dict | None = None) -> dict | None:
    """The bias_drift row of one epoch (the newest when several match);
    None when the rig has no QA-clean bias of it."""
    store = store or load_store(config, rig)
    rows = store.get("bias_drift")
    if rows is None:
        rows = bias_drift(store["frames"])
    best = None
    for x in rows:
        if _num(x.get("gain")) != _num(gain) or _num(x.get("offset")) != _num(offset):
            continue
        if readout and x.get("readout") and x["readout"] != readout:
            continue
        if setpoint is not None and x.get("settemp") is not None \
                and abs(float(x["settemp"]) - float(setpoint)) >= SETTEMP_TOL_C:
            continue
        if best is None or x["date"] > best["date"]:
            best = x
    return best


# --- the per-camera dark level model ------------------------------------------------
#
# The generic limit (dark_excess_limit: 10 ADU + 0.02 ADU/s at 0 C) is a
# ceiling for thermal dark current only. Long darks on the AP26CC carry more
# than that (uniform, tens of ADU at 450 s), so one formula either lets light
# through on short darks or quarantines good long ones. The model judges a
# dark against what the same camera did before:
#
#   anchors   dark sets (one date, one exposure, one epoch, one SET-TEMP)
#             with at least ANCHOR_MIN frames that pass every check; the set
#             median of (dark - its bias) is the anchor's excess
#   expected  the newest DARK_MODEL_NIGHTS earlier nights of the same
#             exposure when there are any (so the longest length is held to
#             its own history); else piecewise-linear in exposure through the
#             anchors of other lengths and the bias at 0 s (interpolated
#             between known lengths, extrapolated past the longest along the
#             top segment, never with a negative slope)
#   limit     expected + calibration_dark_model_allow_adu
#             + the larger of calibration_dark_model_allow_frac x expected
#             and calibration_dark_model_rate_allow x the measured dark
#             current x exposure x 2^(dT/6) (dT: the set's CCD-TEMP minus
#             SET-TEMP). The measured dark current (ADU/s) is pooled over
#             the camera's anchors of DARK_RATE_MIN_S or longer (sum of
#             their excess / sum of their exposure), so a long dark may
#             carry night-to-night spread that grows with its length
#             (2026-10-20 RC16 750 s +38..+56 and 10-22 900 s +52..+63 vs
#             +28 / +33.5 on 10-21: one night's excess + 25 % held them
#             as "level"), while a leak several times the dark current
#             still fails here and a non-uniform or changing one fails
#             [gradient] / [trend] below
#
# Sets are judged in date order, so a set the model accepts anchors later
# nights. With no anchor at all the generic formula still applies.
#
# Light is judged inside the set, not from the pedestal:
#   [trend]    the level changes by more than TREND_ADU through the set
#              (Theil-Sen slope x the set's span): light that follows the
#              sky; the whole set fails
#   [gradient] a frame's zone pattern (each corner minus the center) differs
#              from the set's median pattern by more than LEAK_ADU
#   [leak]     stays a per-frame fail, except that a pattern shared by the
#              set (within PATTERN_TOL_ADU) and no larger than
#              LEAK_ADU + PATTERN_FRAC x the expected excess is the sensor's
#              fixed pattern (a zone_pattern warning)
#   [outlier]  level off the set's own median (unchanged)

DARK_MODEL_ALLOW_ADU = 5.0
DARK_MODEL_ALLOW_FRAC = 0.25
DARK_MODEL_RATE_ALLOW = 1.5   # x the measured dark current x exposure
DARK_RATE_MIN_S = 60.0        # anchors this long or longer measure the rate
DARK_MODEL_NIGHTS = 3
ANCHOR_MIN = 3
TREND_ADU = 3.0
TREND_MIN = 3
PATTERN_MIN = 3
PATTERN_TOL_ADU = 1.5
PATTERN_FRAC = 0.25


def dark_model_settings(config) -> dict:
    """{"enabled", "allow_adu", "allow_frac", "rate_allow"} from the config
    (calibration_dark_model, calibration_dark_model_allow_adu / _frac /
    calibration_dark_model_rate_allow)."""
    def f(name, default):
        try:
            v = getattr(config, name, default)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default
    return {"enabled": bool(getattr(config, "calibration_dark_model", True)),
            "allow_adu": max(0.0, f("calibration_dark_model_allow_adu",
                                    DARK_MODEL_ALLOW_ADU)),
            "allow_frac": max(0.0, f("calibration_dark_model_allow_frac",
                                     DARK_MODEL_ALLOW_FRAC)),
            "rate_allow": max(0.0, f("calibration_dark_model_rate_allow",
                                     DARK_MODEL_RATE_ALLOW))}


def _model_key(rec: dict) -> tuple:
    st = _num(rec.get("settemp"))
    return (*_epoch(rec), None if st is None else round(st))


def dark_rate(anchors: list, date: str | None = None) -> float | None:
    """The camera's measured dark current (ADU/s) from the model's anchors
    of DARK_RATE_MIN_S or longer, shot on or before `date`: the sum of
    their excess over the sum of their exposure (long darks weigh most;
    short ones are mostly bias noise). None without such anchors."""
    use = [a for a in anchors if float(a.get("exptime") or 0) >= DARK_RATE_MIN_S
           and (date is None or str(a.get("date") or "") <= date)]
    t = sum(float(a["exptime"]) for a in use)
    if t <= 0:
        return None
    return max(0.0, sum(max(float(a["excess"]), 0.0) for a in use) / t)


def dark_expectation(anchors: list, exp_s: float, date: str, *,
                     allow_adu: float = DARK_MODEL_ALLOW_ADU,
                     allow_frac: float = DARK_MODEL_ALLOW_FRAC,
                     rate_allow: float = DARK_MODEL_RATE_ALLOW,
                     temp_delta_c: float | None = None,
                     detail: bool = False):
    """(expected excess, limit, source) of a dark of `exp_s` seconds shot on
    `date` from the model's anchors ([{"exptime", "date", "excess", "n"}] of
    one camera epoch and SET-TEMP), or None when there are none.

    The limit is expected + allow_adu + the larger of allow_frac x expected
    and rate_allow x dark_rate x exp_s x 2^(temp_delta_c / DARK_DOUBLING_C)
    (temp_delta_c: the set's CCD-TEMP minus SET-TEMP, None = 0). detail:
    a 4th element names the allowance ("" when the share of expected won)."""
    exp_s = float(exp_s or 0)
    same = sorted((a for a in anchors if abs(a["exptime"] - exp_s) < 0.5
                   and a["date"] < date), key=lambda a: a["date"])
    if same:
        use = same[-DARK_MODEL_NIGHTS:]
        e = _median([a["excess"] for a in use])
        src = (f"{exp_s:g} s on " + ", ".join(
            f"{a['date']} ({a['excess']:+.1f})" for a in use))
    else:
        by_len: dict = {}
        for a in anchors:
            by_len.setdefault(round(a["exptime"], 1), []).append(a["excess"])
        if not by_len:
            return None
        pts = sorted([(0.0, 0.0)] + [(t, _median(v)) for t, v in by_len.items()
                                     if t > 0])
        lo = max((p for p in pts if p[0] <= exp_s), key=lambda p: p[0])
        hi = [p for p in pts if p[0] > exp_s]
        if hi:
            h = hi[0]
            e = lo[1] + (h[1] - lo[1]) * (exp_s - lo[0]) / (h[0] - lo[0])
            how = (f"interpolated between {lo[0]:g} s ({lo[1]:+.1f}) and "
                   f"{h[0]:g} s ({h[1]:+.1f})")
        else:
            a, b = pts[-2], pts[-1]
            slope = max(0.0, (b[1] - a[1]) / (b[0] - a[0])) if b[0] > a[0] else 0.0
            e = b[1] + slope * (exp_s - b[0])
            how = (f"extrapolated from {a[0]:g} s ({a[1]:+.1f}) and "
                   f"{b[0]:g} s ({b[1]:+.1f})")
        src = f"the dark model, {how}"
    frac_term = allow_frac * max(e, 0.0)
    rate = dark_rate(anchors, date)
    tf = 2 ** (float(temp_delta_c or 0.0) / DARK_DOUBLING_C)
    rate_term = 0.0 if rate is None else float(rate_allow) * rate * exp_s * tf
    note = ""
    if rate_term > frac_term:
        note = (f"allowance {rate_term:.1f} = {float(rate_allow):g} x the measured "
                f"dark current {rate:.4f} ADU/s x {exp_s:g} s"
                + (f" x {tf:.2f} for {float(temp_delta_c):+.1f} C"
                   if temp_delta_c else ""))
    lim = e + allow_adu + max(frac_term, rate_term)
    if detail:
        return round(e, 2), round(lim, 2), src, note
    return round(e, 2), round(lim, 2), src


def _theil_sen(xs: list, ys: list) -> float | None:
    sl = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs))
          for j in range(i + 1, len(xs)) if xs[j] != xs[i]]
    return _median(sl) if sl else None


def _obs_seconds(rec: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(rec.get("date_obs")).rstrip("Z")[:26]
                                      ).timestamp()
    except (TypeError, ValueError):
        return None


def _dark_set_checks(members: list, expected: float | None) -> dict | None:
    """Light checks of one dark set: the level trend, each frame's zone
    pattern vs the set's ([gradient]) and the fixed-pattern pardon for
    [leak]. Returns the trend {"change", "minutes", "sun"} or None."""
    rows = [r for r in members if not any(f["code"] in ("temp", "header")
                                          for f in r["frame_fails"])]
    rows.sort(key=lambda r: (str(r.get("date_obs") or ""), str(r.get("name"))))
    trend = None
    timed = [(t, r) for t, r in ((_obs_seconds(r), r) for r in rows) if t is not None]
    if len(timed) >= TREND_MIN:   # DATE-OBS needed: no times, no trend
        ts = [t for t, _r in timed]
        rows_t = [r for _t, r in timed]
        slope = _theil_sen(ts, [r["median"] for r in rows_t])
        if slope is not None:
            change = slope * (ts[-1] - ts[0])
            sun = [r.get("sun_alt") for r in rows_t]
            trend = {"change": round(change, 1),
                     "minutes": round((ts[-1] - ts[0]) / 60.0),
                     "sun": [sun[0], sun[-1]]}
            if abs(change) > TREND_ADU:
                sun_txt = ("" if sun[0] is None or sun[-1] is None
                           else f", sun {sun[0]:g} to {sun[-1]:g} deg")
                why = (f"level {rows_t[0]['median']:g} to {rows_t[-1]['median']:g}, "
                       f"{change:+.1f} ADU through the set over "
                       f"{trend['minutes']:g} min (> {TREND_ADU:g}){sun_txt}: "
                       "light that changes with the sky")
                for r in members:
                    r["set_fails"].append({"code": "trend", "detail": why})
    zoned = [r for r in rows if r.get("center") is not None
             and len(r.get("corners") or []) == 4]
    if len(zoned) < PATTERN_MIN:
        return trend
    pats = [[c - r["center"] for c in r["corners"]] for r in zoned]
    ref = [_median([p[i] for p in pats]) for i in range(4)]
    cap = LEAK_ADU + PATTERN_FRAC * max(expected or 0.0, 0.0)
    ref_spread = max(abs(x) for x in ref)
    for r, p in zip(zoned, pats):
        dev = max(abs(p[i] - ref[i]) for i in range(4))
        if dev > LEAK_ADU:
            r["set_fails"].append({"code": "gradient", "detail":
                                   f"zone pattern differs from its set by "
                                   f"{dev:.1f} ADU (> {LEAK_ADU:g}): corners minus "
                                   f"center {', '.join(f'{x:+.2f}' for x in p)} vs set "
                                   f"{', '.join(f'{x:+.2f}' for x in ref)}"})
            continue
        leak = [f for f in r["frame_fails"] if f["code"] == "leak"]
        if leak and dev <= PATTERN_TOL_ADU and ref_spread <= cap:
            r["frame_fails"] = [f for f in r["frame_fails"] if f["code"] != "leak"]
            r["frame_warns"].append({"code": "zone_pattern", "detail":
                                     f"{leak[0]['detail']}; the same pattern in all "
                                     f"{len(zoned)} frames of the set (within "
                                     f"{dev:.1f} ADU, cap {cap:.1f}): the sensor's "
                                     "fixed pattern, not light"})
    return trend


def _set_outliers(members: list) -> None:
    clean = [r for r in members if not r["frame_fails"]]
    if len(clean) < SET_MIN:
        return
    med = _median([r["median"] for r in clean])
    dev = _median([abs(r["median"] - med) for r in clean]) or 0.0
    thr = max(OUTLIER_LEVEL_ADU, OUTLIER_MAD_K * 1.4826 * dev)
    nmed = _median([r.get("mad") for r in clean])
    for r in members:
        if abs(r["median"] - med) > thr:
            r["set_fails"].append({"code": "outlier", "detail":
                                   f"median {r['median']:g} vs set median {med:g} "
                                   f"(> {thr:.1f} ADU)"})
        if nmed and r.get("mad") is not None and r["mad"] > NOISE_RATIO * nmed \
                and r["mad"] - nmed >= 2:
            r["set_fails"].append({"code": "noise", "detail":
                                   f"MAD {r['mad']:g} vs set {nmed:g} "
                                   f"(> {NOISE_RATIO:g}x)"})


def judge_all(store: dict, *, rig: str, tol: float,
              setpoint: float | None = None, scales: dict | None = None,
              default_readout: str | None = None,
              drift_adu: float = BIAS_DRIFT_ADU,
              dark_model: dict | None = None) -> None:
    """Recompute every verdict in the store (cheap: no file reads).
    PS-182: `scales` = dark_excess_scales (readout -> dark-current scale);
    a dark with no readout recorded takes `default_readout`'s. Darks are
    judged against the bias in force at their date (_bias_pick) and, when
    the bias drifted more than `drift_adu` since, also against the newer
    one (bias drift vs light); store["bias_drift"] = bias_drift.
    `dark_model` = dark_model_settings (None: on, default allowances): the
    dark level limit comes from the per-camera model (dark_expectation);
    dark sets are judged in date order and the ones that pass anchor it;
    store["dark_model"] has one row per dark set."""
    recs = store["frames"]
    scales = scales or {}
    dm = dark_model or {"enabled": True, "allow_adu": DARK_MODEL_ALLOW_ADU,
                        "allow_frac": DARK_MODEL_ALLOW_FRAC,
                        "rate_allow": DARK_MODEL_RATE_ALLOW}
    for r in recs.values():
        if r.get("type") == "BIAS" and r.get("median") is not None:
            f, w = judge_frame(r, rig=rig, tol=tol, setpoint=setpoint)
            r["frame_fails"], r["frame_warns"] = f, w
    sessions = bias_sessions(recs)
    store["bias_drift"] = bias_drift(recs)
    for r in recs.values():
        if r.get("median") is None:
            continue
        r["set_fails"] = []
        r["day_fails"] = []
        for k in ("dark_expected", "dark_excess", "dark_limit"):
            r.pop(k, None)
        if r.get("type") not in ("BIAS", "DARK"):
            f, w = judge_frame(r, rig=rig, tol=tol, setpoint=setpoint)
            r["frame_fails"], r["frame_warns"] = f, w
    sets: dict[tuple, list] = {}
    for r in recs.values():
        if r.get("type") in ("DARK", "BIAS") and r.get("median") is not None:
            sets.setdefault(_set_key(r), []).append(r)
    for members in sets.values():   # bias sets: outliers only
        if members[0].get("type") == "BIAS":
            _set_outliers(members)
    # dark sets, oldest first: model limit, light checks, then anchor
    anchors: dict[tuple, list] = {}
    model_rows = []
    darks = sorted((m for m in sets.values() if m[0].get("type") == "DARK"),
                   key=lambda m: (str(m[0].get("date") or ""),
                                  float(m[0].get("exptime") or 0)))
    for members in darks:
        r0 = members[0]
        mk = _model_key(r0)
        dts = [_num(r.get("ccdtemp")) - _num(r.get("settemp")) for r in members
               if _num(r.get("ccdtemp")) is not None
               and _num(r.get("settemp")) is not None]
        expect = (dark_expectation(anchors.get(mk, []), r0.get("exptime") or 0,
                                   str(r0.get("date") or ""),
                                   allow_adu=dm["allow_adu"],
                                   allow_frac=dm["allow_frac"],
                                   rate_allow=dm.get("rate_allow",
                                                     DARK_MODEL_RATE_ALLOW),
                                   temp_delta_c=_median(dts), detail=True)
                  if dm.get("enabled") else None)
        rate = (dark_rate(anchors.get(mk, []), str(r0.get("date") or ""))
                if expect else None)
        for r in members:
            sc = scales.get(r.get("readout") or default_readout, 1.0)
            ref, alt = _bias_ref(r, sessions, drift_adu)
            f, w = judge_frame(r, rig=rig, tol=tol, bias_ref=ref, bias_alt=alt,
                               setpoint=setpoint, excess_scale=sc, expect=expect)
            r["frame_fails"], r["frame_warns"] = f, w
            if expect is not None:
                r["dark_expected"] = expect[0]
        trend = _dark_set_checks(members, expect[0] if expect else None)
        _set_outliers(members)
        good = [r for r in members if not r["frame_fails"] and not r["set_fails"]
                and r.get("dark_excess") is not None]
        row = {"date": r0.get("date"), "exptime": float(r0.get("exptime") or 0),
               "gain": r0.get("gain"), "offset": r0.get("offset"),
               "readout": r0.get("readout"), "settemp": mk[-1],
               "n": len(members), "n_pass": len(good),
               "excess": _median([r.get("dark_excess") for r in members]),
               "expected": expect[0] if expect else None,
               "limit": expect[1] if expect else None,
               "source": expect[2] if expect else "generic formula (no anchors)",
               "allowance": (expect[3] or None) if expect else None,
               "rate": None if rate is None else round(rate, 4),
               "trend": trend, "anchor": False}
        if dm.get("enabled") and len(good) >= ANCHOR_MIN:
            anchors.setdefault(mk, []).append(
                {"exptime": row["exptime"], "date": str(row["date"] or ""),
                 "excess": _median([r["dark_excess"] for r in good]), "n": len(good)})
            row["anchor"] = True
        model_rows.append(row)
    store["dark_model"] = model_rows
    check_daytime(store)
    for r in recs.values():
        _finish(r)


def _finish(r: dict) -> None:
    if r.get("median") is None:
        r["verdict"] = "error" if r.get("error") else "unchecked"
        return
    fails = (r.get("frame_fails") or []) + (r.get("set_fails") or []) \
        + (r.get("day_fails") or [])
    r["reasons"] = [f"{f['code']}: {f['detail']}" for f in fails]
    r["warnings"] = [f"{f['code']}: {f['detail']}" for f in r.get("frame_warns") or []]
    r["codes"] = sorted({f["code"] for f in fails})
    if r.get("override") == "pass":
        r["verdict"] = "pass"
    elif fails:
        r["verdict"] = "fail"
    else:
        r["verdict"] = "warn" if r["warnings"] else "pass"


def passed(rec: dict) -> bool:
    return rec.get("verdict") in ("pass", "warn")


# --- day vs night ----------------------------------------------------------------

def sun_altitudes(config, iso_times: list[str]) -> list[float | None]:
    """Sun altitude (deg) at each DATE-OBS (UTC ISO), one vectorized
    transform; None where the time does not parse."""
    import numpy as np
    out: list[float | None] = [None] * len(iso_times)
    idx, ts = [], []
    for i, s in enumerate(iso_times):
        if not s:
            continue
        try:
            ts.append(datetime.fromisoformat(str(s).rstrip("Z")[:26]))
            idx.append(i)
        except ValueError:
            continue
    if not ts:
        return out
    from astropy.coordinates import AltAz, get_sun
    from astropy.time import Time
    from photonscript.shared.astronomy import get_earth_location
    t = Time(ts)
    alts = get_sun(t).transform_to(AltAz(
        obstime=t, location=get_earth_location(config.get_observatory()))).alt.deg
    for i, a in zip(idx, np.atleast_1d(alts)):
        out[i] = round(float(a), 1)
    return out


def check_daytime(store: dict) -> dict:
    """Compare daytime darks with night darks of the same epoch and
    exposure. Day = sun above DAY_SUN_ALT at DATE-OBS, night = below
    NIGHT_SUN_ALT; it decides once DAY_MIN_FRAMES day darks exist. Leak when the day
    median sits DAYTIME_LEAK_ADU over the night median, or (no night set)
    when DAYTIME_LIGHT_FRAC of the day darks fail a light check. Leaking day
    frames get a daytime_leak fail. Returns {"status", "evidence"} and
    stores it as store["daytime"] (the sticky per-rig switch is
    set_daytime_state, applied by the callers that may change it)."""
    groups: dict[tuple, dict[str, list]] = {}
    for r in store["frames"].values():
        if r.get("type") != "DARK" or r.get("median") is None or r.get("sun_alt") is None:
            continue
        if any(f["code"] in ("temp", "header") for f in r.get("frame_fails") or []):
            continue
        g = groups.setdefault((round(r.get("exptime") or 0), *_epoch(r),
                               r.get("settemp")), {"day": [], "night": []})
        if r["sun_alt"] > DAY_SUN_ALT:
            g["day"].append(r)
        elif r["sun_alt"] < NIGHT_SUN_ALT:
            g["night"].append(r)
    evidence = []
    status = "untested"
    for key, g in groups.items():
        if len(g["day"]) < DAY_MIN_FRAMES:
            if g["day"]:
                evidence.append({"exp_s": key[0], "leak": None, "detail":
                                 f"{key[0]} s: only {len(g['day'])} day dark(s), "
                                 f"{DAY_MIN_FRAMES} needed"})
            continue
        clean_night = [r for r in g["night"] if not r.get("frame_fails")]
        day_med = _median([r["median"] for r in g["day"]])
        if len(clean_night) >= 3:
            night_med = _median([r["median"] for r in clean_night])
            delta = day_med - night_med
            leak = delta > DAYTIME_LEAK_ADU
            why = (f"{key[0]} s: day median {day_med:g} vs night {night_med:g} "
                   f"({delta:+.1f} ADU, {len(g['day'])} day / {len(clean_night)} night)")
        else:
            lit = [r for r in g["day"] if any(
                f["code"] in LIGHT_CODES for f in r.get("frame_fails") or [])]
            frac = len(lit) / len(g["day"])
            leak = frac >= DAYTIME_LIGHT_FRAC
            delta = None
            why = (f"{key[0]} s: no night set; {len(lit)} of {len(g['day'])} day "
                   "darks fail a light check")
        evidence.append({"exp_s": key[0], "leak": leak, "detail": why})
        if leak:
            status = "leak"
            for r in g["day"]:
                r["day_fails"].append({"code": "daytime_leak", "detail": why})
        elif status != "leak":
            status = "ok"
    store["daytime"] = {"status": status, "evidence": evidence}
    return store["daytime"]


# --- QA passes ---------------------------------------------------------------------

def rig_view(config, rig: str):
    from photonscript.scheduler.readiness import RIG_VIEWS
    return RIG_VIEWS.get(rig, lambda c: c)(config)


def _setpoint(config, rig):
    from photonscript.shared.rigs import rig_setpoint
    return rig_setpoint(config, rig)


def qa_frames(config, rig: str, frames, *, recheck: bool = False,
              expect: dict | None = None, persist: bool = True,
              apply_daytime: bool = True, progress=None) -> dict:
    """Measure (cached) and judge `frames` [(TYPE, date, path)] for `rig`
    against the whole rig store. Returns {key: record} for those frames.
    Never raises on one bad file (verdict "error")."""
    frames = list(frames)
    # 1. what needs measuring (no lock: measuring is the slow part, and a
    #    long CLI backfill must not block the service's filing hook)
    known = load_store(config, rig)["frames"]
    todo = []
    hdr_only: list = []   # PS-128: cached records with no readout field yet
    for typ, date, path in frames:
        p = Path(path)
        key = frame_key(typ, date, p.name)
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        old = known.get(key)
        fresh = (old is not None and not recheck and old.get("qa_version") == QA_VERSION
                 and old.get("size") == size and old.get("median") is not None)
        if not fresh:
            todo.append((key, typ, date, p, size, old))
        elif "readout" not in old or "focpos" not in old:   # PS-128, PS-164
            hdr_only.append((key, p))
    measured: dict[str, dict] = {}
    for i, (key, typ, date, p, size, old) in enumerate(todo):
        if progress:
            progress(i + 1, len(todo), key)
        rec = {"type": typ, "date": date, "name": p.name, "path": str(p),
               "size": size, "qa_version": QA_VERSION,
               "checked": datetime.utcnow().isoformat(timespec="seconds") + "Z",
               "location": (old or {}).get("location") or "library"}
        if old and old.get("override"):
            rec["override"] = old["override"]
        if expect:
            rec["expect"] = expect
        try:
            rec.update(measure(p, config))
        except Exception as e:  # noqa: BLE001 - one unreadable frame
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["median"] = None
        measured[key] = rec
    readouts = _read_readouts(hdr_only)
    need_sun = [k for k, r in measured.items() if r.get("date_obs")]
    if need_sun:
        try:
            alts = sun_altitudes(config, [measured[k]["date_obs"] for k in need_sun])
            for k, a in zip(need_sun, alts):
                measured[k]["sun_alt"] = a
        except Exception as e:  # noqa: BLE001
            logger.warning("calibration QA: sun altitude failed: %s", e)
    # 2. merge, judge, save under the lock
    with _lock, _FileLock(qa_dir(config) / f"{rig}.lock"):
        store = load_store(config, rig)
        recs = store["frames"]
        recs.update(measured)
        for k, ro in readouts.items():
            if k in recs and k not in measured:
                recs[k].update(ro)
        for typ, date, path in frames:
            r = recs.get(frame_key(typ, date, Path(path).name))
            if r is not None:
                r["path"] = str(path)
                if expect and not r.get("expect"):
                    r["expect"] = expect
        from photonscript.shared.rigs import rig_readout
        judge_all(store, rig=rig, tol=temp_tol(config),
                  setpoint=_setpoint(config, rig),
                  scales=dark_excess_scales(config, rig),          # PS-182
                  default_readout=rig_readout(config, rig),
                  drift_adu=bias_drift_adu(config),
                  dark_model=dark_model_settings(config))
        if persist:
            save_store(config, rig, store)
    day = store.get("daytime", {}).get("status")
    if apply_daytime and persist and day == "leak":
        if daytime_state(config, rig).get("status") != "disabled":
            set_daytime_state(config, rig, "disabled",
                              evidence=store["daytime"]["evidence"])
            logger.warning("PS-113: daytime calibration capture DISABLED for %s: %s",
                           rig, store["daytime"]["evidence"])
    elif apply_daytime and persist and day == "ok":
        if daytime_state(config, rig).get("status") == "untested":
            set_daytime_state(config, rig, "ok", evidence=store["daytime"]["evidence"])
    out = {}
    for typ, date, path in frames:
        k = frame_key(typ, date, Path(path).name)
        if k in recs:
            out[k] = recs[k]
    return out


def _read_readouts(items) -> dict:
    """PS-128: {key: {"readout", "readout_raw"}} from a header-only read of
    each (key, path): the backfill for records measured before PS-128. An
    unreadable header records None (the rig default is then assumed).
    PS-164: also "focpos" / "rotator_deg" (shared.optics_state)."""
    from astropy.io import fits as _fits
    from photonscript.shared.optics_state import optics_fields
    from photonscript.shared.rigs import header_readout
    out = {}
    for key, p in items:
        try:
            hdr = _fits.getheader(p)
            ro, raw = header_readout(hdr)
            opt = optics_fields(hdr)   # PS-164: same backfill path
        except Exception:  # noqa: BLE001 - one unreadable frame
            ro, raw = None, None
            opt = {"focpos": None, "rotator_deg": None}
        out[key] = {"readout": ro, "readout_raw": raw, **opt}
    return out


def _lib_link(lib: Path, rec: dict) -> Path:
    return lib / "Calibration" / rec["type"] / rec["date"] / rec["name"]


def _quarantine_path(lib: Path, rec: dict) -> Path:
    return lib / "Calibration" / QUARANTINE_DIR / rec["type"] / rec["date"] / rec["name"]


def _write_reasons(folder: Path, name: str, rec: dict | None) -> None:
    p = folder / "reasons.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        data = {}
    if rec is None:
        data.pop(name, None)
    else:
        data[name] = {"reasons": rec.get("reasons") or [],
                      "at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    if data:
        _write_json(p, data)
    elif p.exists():
        p.unlink()


def quarantine(lib: Path, rec: dict) -> Path | None:
    """Move a failing frame's Library link into the quarantine folder (a
    rename inside the PhotonScript-owned Library). Returns the new path,
    or None when there is no Library link to move."""
    src = _lib_link(lib, rec)
    dest = _quarantine_path(lib, rec)
    if not src.exists():
        return dest if dest.exists() else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        src.unlink()
    else:
        os.replace(src, dest)
    _write_reasons(dest.parent, rec["name"], rec)
    rec["location"] = "quarantine"
    try:
        if not any(src.parent.iterdir()):
            src.parent.rmdir()
    except OSError:
        pass
    return dest


def link_into_quarantine(src: Path, lib: Path, rec: dict) -> Path:
    """File a failing watch-dir frame straight into the quarantine (hardlink,
    else copy) instead of the Library."""
    import shutil
    dest = _quarantine_path(lib, rec)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)
    _write_reasons(dest.parent, rec["name"], rec)
    rec["location"] = "quarantine"
    return dest


_last_filed: dict[str, dict] = {}


def last_filed(rig: str) -> dict:
    """PS-188: {key: record} of the rig's last file_night batch."""
    return _last_filed.get(rig) or {}


def file_night(config, rig: str, lib: Path, night: str, frames, *,
               recheck: bool = False, dry_run: bool = False) -> set:
    """The filing hook (runs._link_calibration_night): QA one night's
    calibration frames as a batch. With mode quarantine every failing frame
    is filed into the quarantine (moved there when it was already linked in
    the Library, else hardlinked from the watch dir). Returns the set of
    watch-dir paths (str) the caller must NOT link into the Library. Mode
    off: no QA, empty set; mode report: QA recorded, empty set. Frames that
    could not be read ("error") are filed as before. PS-188: recheck
    re-measures cached frames; dry_run judges without saving or moving."""
    m = mode(config)
    _last_filed[rig] = {}
    if m == "off" or not frames:
        return set()
    recs = qa_frames(config, rig, frames, recheck=recheck, persist=not dry_run,
                     apply_daytime=not dry_run)
    _last_filed[rig] = recs
    held: set = set()
    if m != "quarantine":
        return held
    for typ, date, path in frames:
        rec = recs.get(frame_key(typ, date, Path(path).name))
        if rec is None or rec.get("verdict") != "fail":
            continue
        held.add(str(path))
        if dry_run:
            continue
        try:
            if _lib_link(lib, rec).exists():
                quarantine(lib, rec)
            else:
                link_into_quarantine(Path(path), lib, rec)
        except OSError as e:  # still kept out of the Library
            logger.warning("PS-113: quarantine of %s failed: %s", path, e)
    if held and not dry_run:
        _persist_locations(config, rig, recs)
        logger.info("PS-113: %d %s calibration frame(s) of %s quarantined",
                    len(held), rig, night)
    return held


def _persist_locations(config, rig: str, recs: dict) -> None:
    with _lock, _FileLock(qa_dir(config) / f"{rig}.lock"):
        store = load_store(config, rig)
        for k, r in recs.items():
            if k in store["frames"]:
                store["frames"][k]["location"] = r.get("location", "library")
        save_store(config, rig, store)


def backfill(config, rig: str | None = None, *, dry_run: bool = False,
             recheck: bool = False, progress=None) -> dict:
    """QA every calibration frame of one rig (or every enabled rig) in the
    watch dir and the Library. Real run: failing Library links move to the
    quarantine. --dry-run moves nothing and leaves the daytime switch alone,
    but records the measurements and verdicts so the real run is quick."""
    from photonscript.scheduler.calibration import iter_calibration_frames
    from photonscript.scheduler.runs import library_root
    from photonscript.shared.rigs import rig_ids
    rigs = [rig] if rig else rig_ids(config)
    report = {"dry_run": dry_run, "mode": mode(config), "rigs": {}}
    for rg in rigs:
        view = rig_view(config, rg)
        lib = library_root(view)
        frames = list(iter_calibration_frames(view))
        recs = qa_frames(config, rg, frames, recheck=recheck,
                         apply_daytime=not dry_run, progress=progress)
        moved, would = [], []
        for k, r in recs.items():
            if r.get("verdict") != "fail":
                continue
            if not _lib_link(lib, r).exists():
                continue
            if dry_run:
                would.append(k)
            else:
                quarantine(lib, r)
                moved.append(k)
        if moved:
            _persist_locations(config, rg, recs)
        report["rigs"][rg] = {"library": str(lib), "frames": len(frames),
                              **summarize(recs.values()),
                              "quarantined": moved, "would_quarantine": would,
                              "daytime": load_store(config, rg).get("daytime")}
    return report


def summarize(recs) -> dict:
    recs = list(recs)
    out = {"pass": 0, "warn": 0, "fail": 0, "error": 0, "unchecked": 0,
           "by_reason": {}, "sets": []}
    sets: dict[tuple, dict] = {}
    for r in recs:
        v = r.get("verdict") or "unchecked"
        out[v] = out.get(v, 0) + 1
        for c in r.get("codes") or []:
            out["by_reason"][c] = out["by_reason"].get(c, 0) + 1
        flat = r.get("type") == "FLAT"   # flats: one row per filter, any exposure
        k = (r.get("type"), r.get("date"), None if flat else r.get("exptime"),
             r.get("gain"),
             r.get("offset"), r.get("settemp"), r.get("filter")
             if r.get("type") == "FLAT" else None, r.get("readout"))
        s = sets.setdefault(k, {"type": k[0], "date": k[1], "exptime": k[2],
                                "gain": k[3], "offset": k[4], "settemp": k[5],
                                "filter": k[6], "readout": k[7],
                                "n": 0, "pass": 0, "fail": 0,
                                "ccdtemp": [None, None], "median": [None, None],
                                "reasons": {}})
        s["n"] += 1
        s["pass" if passed(r) else "fail"] += 1
        for fld, src in (("ccdtemp", "ccdtemp"), ("median", "median")):
            v2 = r.get(src)
            if v2 is not None:
                lo, hi = s[fld]
                s[fld] = [v2 if lo is None else min(lo, v2), v2 if hi is None else max(hi, v2)]
        for c in r.get("codes") or []:
            s["reasons"][c] = s["reasons"].get(c, 0) + 1
    out["sets"] = sorted(sets.values(), key=lambda s: (str(s["type"]), str(s["date"]),
                                                        s["exptime"] or 0,
                                                        str(s["readout"])))
    return out


def restore(config, rig: str, *, dry_run: bool = False) -> dict:
    """Undo the quarantine for a rig (false-positive escape hatch): move every
    quarantined Library link back and mark it override=pass so the filing
    hook leaves it in the Library."""
    from photonscript.scheduler.runs import library_root
    lib = library_root(rig_view(config, rig))
    qroot = lib / "Calibration" / QUARANTINE_DIR
    moved = []
    with _lock, _FileLock(qa_dir(config) / f"{rig}.lock"):
        store = load_store(config, rig)
        if qroot.exists():
            for f in sorted(qroot.rglob("*.fits")):
                rel = f.relative_to(qroot).parts
                if len(rel) != 3:
                    continue
                typ, date, name = rel
                dest = lib / "Calibration" / typ / date / name
                moved.append(str(dest))
                if dry_run:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    os.replace(f, dest)
                else:
                    f.unlink()
                _write_reasons(f.parent, name, None)
                r = store["frames"].get(frame_key(typ, date, name))
                if r is not None:
                    r["override"] = "pass"
                    r["location"] = "library"
                    _finish(r)
        if not dry_run:
            save_store(config, rig, store)
    return {"rig": rig, "dry_run": dry_run, "restored": moved}


def _judge_store(config, rig: str, store: dict) -> None:
    from photonscript.shared.rigs import rig_readout
    judge_all(store, rig=rig, tol=temp_tol(config), setpoint=_setpoint(config, rig),
              scales=dark_excess_scales(config, rig),
              default_readout=rig_readout(config, rig),
              drift_adu=bias_drift_adu(config),
              dark_model=dark_model_settings(config))


def _quarantine_reason(qpath: Path) -> str:
    """The reasons written next to a quarantined frame (reasons.json)."""
    try:
        data = json.loads((qpath.parent / "reasons.json").read_text(encoding="utf-8"))
        return "; ".join((data.get(qpath.name) or {}).get("reasons", [])[:2])
    except (OSError, ValueError, AttributeError):
        return ""


def rejudged_verdicts(config, rig: str, store: dict | None = None) -> dict:
    """PS-188: {key: (verdict, reasons)} of the rig's store judged by
    today's rule (no file reads, nothing saved). The store keeps the
    verdict of the last filing pass: the RC16 HCG darks of 2026-10-01 /
    10-05 / 10-06 still carried the LCG dark-current limit (allowed 22 ADU
    for 600 s) after PS-182 scaled it for HCG (47.9 ADU), because no
    filing ran after that deploy."""
    import copy
    st = copy.deepcopy(store if store is not None else load_store(config, rig))
    if not st.get("frames"):
        return {}
    _judge_store(config, rig, st)
    return {k: (r.get("verdict"), list(r.get("reasons") or []))
            for k, r in st["frames"].items()}


def rejudge(config, rig: str, *, dry_run: bool = False) -> dict:
    """PS-188: judge the rig's whole store by today's rule (cheap, no
    file reads) and put each filed frame where its new verdict says: a
    quarantined frame that passes now goes back into the Library (its
    quarantine copy removed when the Library already has it), and, in
    quarantine mode, a Library frame that fails now goes to the quarantine.
    dry_run reports the moves and saves nothing. Returns {"restored",
    "quarantined", "changed"} with [key, old reason / new reason]."""
    from photonscript.scheduler.runs import library_root
    lib = library_root(rig_view(config, rig))
    out = {"rig": rig, "dry_run": dry_run, "restored": [], "quarantined": [],
           "changed": 0}
    with _lock, _FileLock(qa_dir(config) / f"{rig}.lock"):
        store = load_store(config, rig)
        if not store["frames"]:
            return out
        before = {k: (r.get("verdict"), list(r.get("reasons") or []))
                  for k, r in store["frames"].items()}
        if dry_run:
            import copy
            store = copy.deepcopy(store)
        _judge_store(config, rig, store)
        qmode = mode(config)
        for k, r in store["frames"].items():
            old_v, old_r = before.get(k, (None, []))
            if r.get("verdict") != old_v:
                out["changed"] += 1
            if not all(r.get(f) for f in ("type", "date", "name")):
                continue
            qpath, lpath = _quarantine_path(lib, r), _lib_link(lib, r)
            if passed(r) and qpath.exists():
                was = "; ".join(old_r[:2]) if old_v == "fail" else ""
                out["restored"].append([k, was or _quarantine_reason(qpath)
                                        or "quarantined"])
                if dry_run:
                    continue
                try:
                    if lpath.exists():
                        qpath.unlink()
                    else:
                        lpath.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(qpath, lpath)
                    _write_reasons(qpath.parent, r["name"], None)
                    r["location"] = "library"
                except OSError as e:
                    logger.warning("PS-188: restore of %s failed: %s", k, e)
            elif (r.get("verdict") == "fail" and qmode == "quarantine"
                  and lpath.exists()):
                out["quarantined"].append([k, "; ".join((r.get("reasons") or [])[:2])])
                if dry_run:
                    continue
                try:
                    quarantine(lib, r)
                except OSError as e:
                    logger.warning("PS-188: quarantine of %s failed: %s", k, e)
        if not dry_run:
            save_store(config, rig, store)
    return out


# --- counting (readiness / gap report) ----------------------------------------------

def _within_days(date: str, days: int) -> bool:
    try:
        return (datetime.now() - datetime.strptime(date, "%Y-%m-%d")).days <= days
    except (TypeError, ValueError):
        return False


def _readout_want(config, rig: str, readout) -> tuple[str | None, str | None, str | None]:
    """PS-128: (readout to match, default for frames without one, PS-182
    rig_readout_since). readout None = the rig's (rig_readout); a blank
    rig setting = not matched."""
    from photonscript.shared.rigs import (normalize_readout, rig_readout,
                                          rig_readout_since)
    default = rig_readout(config, rig)
    want = default if readout is None else normalize_readout(readout)
    return want, default, rig_readout_since(config, rig)


def count_passed_darks(config, rig: str, exp_s: float, *, gain, offset,
                       setpoint: float, readout: str | None = None,
                       store: dict | None = None) -> int:
    """QA-passed darks of the epoch (PS-128: at the readout mode, the rig's
    when None; frames with no readout keyword count as the rig's)."""
    store = store or load_store(config, rig)
    days = int(getattr(config, "library_cal_days", 120))
    want, default, since = _readout_want(config, rig, readout)
    n = 0
    for r in store["frames"].values():
        if r.get("type") != "DARK" or not passed(r):
            continue
        if not readout_matches(r, want, default, since):
            continue
        if (abs((r.get("exptime") or -1) - exp_s) < 0.5 and r.get("gain") == gain
                and r.get("offset") == offset
                and abs((r.get("settemp") if r.get("settemp") is not None else 99)
                        - setpoint) < SETTEMP_TOL_C
                and _within_days(r.get("date"), days)):
            n += 1
    return n


def count_passed_bias(config, rig: str, *, gain, offset, readout: str | None = None,
                      setpoint: float | None = None,
                      store: dict | None = None) -> int:
    """QA-passed bias of the epoch in its newest session that has any."""
    by_date = passed_bias_sessions(config, rig, gain=gain, offset=offset,
                                   readout=readout, setpoint=setpoint, store=store)
    return by_date[max(by_date)] if by_date else 0


def passed_bias_sessions(config, rig: str, *, gain, offset,
                         readout: str | None = None,
                         setpoint: float | None = None,
                         store: dict | None = None) -> dict:
    """PS-122: {date: QA-passed bias of the epoch} per session (PS-128: at
    the readout mode, the rig's when None; PS-182: at SET-TEMP within
    SETTEMP_TOL_C of `setpoint` when given, the library report's rule)."""
    store = store or load_store(config, rig)
    want, default, since = _readout_want(config, rig, readout)
    by_date: dict[str, int] = {}
    for r in store["frames"].values():
        if r.get("type") == "BIAS" and passed(r) and r.get("gain") == gain \
                and r.get("offset") == offset \
                and readout_matches(r, want, default, since):
            if setpoint is not None and (r.get("settemp") is None or abs(
                    float(r["settemp"]) - float(setpoint)) >= SETTEMP_TOL_C):
                continue
            by_date[r["date"]] = by_date.get(r["date"], 0) + 1
    return by_date


def fill_missing_readouts(config, rig: str, limit: int = 400) -> dict:
    """PS-182: header-read READOUTM (and PS-164 focpos / rotator) for up to
    `limit` DARK / BIAS records of the rig that have no readout recorded
    and were never header-read for it, and store them. The counters then
    use the frame's real mode instead of assuming the rig's (the RC16 July
    bias / 600 s darks are LCG). Metadata only: nothing is measured, judged
    again or moved. Returns {"checked", "filled", "left"}."""
    if limit <= 0 or mode(config) == "off":
        return {"checked": 0, "filled": 0, "left": 0}
    store = load_store(config, rig)
    todo = [(k, r) for k, r in store["frames"].items()
            if r.get("type") in ("DARK", "BIAS") and not r.get("readout")
            and not r.get("readout_checked") and r.get("path")]
    todo.sort(key=lambda kr: kr[1].get("date") or "", reverse=True)
    batch = todo[:int(limit)]
    got = _read_readouts([(k, Path(r["path"])) for k, r in batch])
    filled = 0
    if batch:
        with _lock, _FileLock(qa_dir(config) / f"{rig}.lock"):
            store = load_store(config, rig)
            for k, ro in got.items():
                r = store["frames"].get(k)
                if r is None:
                    continue
                r["readout_checked"] = True
                if ro.get("readout"):
                    r["readout"] = ro["readout"]
                    r["readout_raw"] = ro.get("readout_raw")
                    filled += 1
                for f in ("focpos", "rotator_deg"):
                    if r.get(f) is None and ro.get(f) is not None:
                        r[f] = ro[f]
            save_store(config, rig, store)
    return {"checked": len(batch), "filled": filled, "left": len(todo) - len(batch)}


def count_passed_flats(config, rig: str, *, gain=None, offset=None,
                       stale_days: int | None = None,
                       store: dict | None = None) -> dict:
    """{canonical filter: QA-passed flats in that filter's newest session},
    only sessions within stale_days when given. The Piggy-600 (no wheel)
    counts everything as OSC."""
    per = passed_flat_sessions(config, rig, gain=gain, offset=offset,
                               stale_days=stale_days, store=store)
    return {f: d[max(d)] for f, d in per.items()}


def passed_flat_sessions(config, rig: str, *, gain=None, offset=None,
                         stale_days: int | None = None,
                         store: dict | None = None) -> dict:
    """PS-122: {canonical filter: {date: QA-passed flats}}, only sessions
    within stale_days when given. The Piggy-600 (no wheel) files everything
    as OSC."""
    store = store or load_store(config, rig)
    try:
        rev = config.reverse_filter_map()
    except Exception:  # noqa: BLE001
        rev = {}
    per: dict[str, dict[str, int]] = {}
    for r in store["frames"].values():
        if r.get("type") != "FLAT" or not passed(r):
            continue
        if gain is not None and r.get("gain") != gain:
            continue
        if offset is not None and r.get("offset") != offset:
            continue
        if stale_days is not None and not _within_days(r.get("date"), stale_days):
            continue
        f = "OSC" if rig != "rc16" else rev.get(r.get("filter") or "?", r.get("filter") or "?")
        per.setdefault(f, {})
        per[f][r["date"]] = per[f].get(r["date"], 0) + 1
    return per


def flat_session_optics(config, rig: str, *, gain=None, offset=None,
                        store: dict | None = None) -> dict:
    """PS-164: {canonical filter: {date: {"focpos", "rotator_deg", "n"}}}:
    the median focuser position and rotator angle of each QA-passed flat
    session (None when no frame of the session recorded one). Same filter
    naming as passed_flat_sessions."""
    store = store or load_store(config, rig)
    try:
        rev = config.reverse_filter_map()
    except Exception:  # noqa: BLE001
        rev = {}
    acc: dict = {}
    for r in store["frames"].values():
        if r.get("type") != "FLAT" or not passed(r):
            continue
        if gain is not None and r.get("gain") != gain:
            continue
        if offset is not None and r.get("offset") != offset:
            continue
        f = "OSC" if rig != "rc16" else rev.get(r.get("filter") or "?", r.get("filter") or "?")
        a = acc.setdefault(f, {}).setdefault(r.get("date"), {"fp": [], "rot": []})
        if r.get("focpos") is not None:
            a["fp"].append(float(r["focpos"]))
        if r.get("rotator_deg") is not None:
            a["rot"].append(float(r["rotator_deg"]))
    out: dict = {}
    for f, by_date in acc.items():
        for d, a in by_date.items():
            fp, rot = _median(a["fp"]), _median(a["rot"])
            out.setdefault(f, {})[d] = {
                "focpos": int(round(fp)) if fp is not None else None,
                "rotator_deg": round(rot, 2) if rot is not None else None,
                "n": len(a["fp"])}
    return out


def format_report(rep: dict, config=None) -> str:
    """Plain-text backfill / summary report (CLI)."""
    head = "Calibration QA"
    if rep.get("dry_run") is not None:
        head += " backfill" + (" (DRY RUN: nothing moved)" if rep["dry_run"] else "")
    lines = [f"{head}, mode {rep.get('mode')}"]
    for rig, r in (rep.get("rigs") or {}).items():
        lines.append("")
        lines.append(f"{rig}: {r.get('pass', 0)} pass, {r.get('warn', 0)} warn, "
                     f"{r.get('fail', 0)} fail, {r.get('error', 0)} unreadable"
                     + (f"  ({r['library']})" if r.get("library") else ""))
        if r.get("by_reason"):
            lines.append("  fail reasons: " + ", ".join(
                f"{k} {v}" for k, v in sorted(r["by_reason"].items())))
        lines.append(f"  {'type':<5} {'date':<10} {'exp':>8} {'gain':>4} {'off':>4} "
                     f"{'set':>5} {'ro':>3} {'n':>3} {'ok':>3} {'bad':>3}  {'CCD-TEMP':<13} "
                     f"{'median ADU':<15} reasons")
        for s in r.get("sets") or []:
            def rng(v, fmt):
                return "-" if v[0] is None else (
                    fmt.format(v[0]) if v[0] == v[1] else
                    (fmt + " to " + fmt).format(v[0], v[1]))
            exp = "-" if s["exptime"] is None else f"{s['exptime']:g}"
            what = s["type"] + ("" if not s.get("filter") else f" {s['filter']}")
            lines.append(
                f"  {what:<5} {s['date'] or '-':<10} {exp:>8} {s['gain'] or '-':>4} "
                f"{s['offset'] or '-':>4} {'-' if s['settemp'] is None else s['settemp']:>5} "
                f"{s.get('readout') or '?':>3} {s['n']:>3} {s['pass']:>3} {s['fail']:>3}  "
                f"{rng(s['ccdtemp'], '{:g}'):<13} {rng(s['median'], '{:g}'):<15} "
                + ", ".join(f"{k} {v}" for k, v in sorted(s["reasons"].items())))
        d = r.get("daytime") or {}
        if d.get("evidence"):
            lines.append(f"  day vs night: {d.get('status')}: "
                         + "; ".join(e["detail"] for e in d["evidence"]))
        if r.get("would_quarantine"):
            lines.append(f"  would quarantine {len(r['would_quarantine'])} Library frames")
        if r.get("quarantined"):
            lines.append(f"  quarantined {len(r['quarantined'])} Library frames")
    return "\n".join(lines)


# --- release check (read-only dry run) ----------------------------------------------

def _exp_token(name: str) -> str:
    import re
    m = re.search(r"_(\d+(?:\.\d+)?)s_", name)
    return m.group(1) if m else "?"


def _sample(files: list, n: int) -> list:
    """Up to n files spread evenly through a set (first and last kept)."""
    if n <= 0 or len(files) <= n:
        return list(files)
    if n == 1:
        return [files[0]]
    step = (len(files) - 1) / (n - 1)
    return [files[round(i * step)] for i in range(n)]


def release_check(config, rig: str, lib: Path, qdir: Path, *,
                  per_set: int = 6, progress=None, measure_fn=None) -> dict:
    """Which quarantined frames in `qdir` (<lib>/Calibration/_quarantine/
    <TYPE>/<date>) would pass the current rule. READ-ONLY: it measures the
    frames of qdir and, for context (the bias in force, the dark model's
    anchors, the set checks), up to `per_set` frames of every other
    Library and quarantine DARK / BIAS set of the rig. Nothing is saved,
    moved or written; releasing from the quarantine is the operator's step
    on the scope (calibration-file --rejudge, which judges the scope's own
    store). measure_fn (tests) replaces measure(path, config)."""
    import copy
    measure_fn = measure_fn or measure
    lib, qdir = Path(lib), Path(qdir)
    cal = lib / "Calibration"
    qroot = cal / QUARANTINE_DIR
    try:
        qrel = qdir.resolve().relative_to(qroot.resolve()).parts
    except ValueError:
        raise ValueError(f"{qdir} is not under {qroot}")
    if len(qrel) != 2:
        raise ValueError(f"{qdir}: expected {qroot}\\<TYPE>\\<date>")
    q_typ, q_date = qrel
    todo: list = []   # (key, typ, date, path, location, target)
    for loc, root in (("library", cal), ("quarantine", qroot)):
        for typ in ("DARK", "BIAS"):
            for d in sorted((root / typ).glob("*")) if (root / typ).is_dir() else []:
                if not d.is_dir():
                    continue
                target = loc == "quarantine" and typ == q_typ and d.name == q_date
                files = sorted(d.glob("*.fits")) + sorted(d.glob("*.fit"))
                groups: dict = {}
                for f in files:
                    groups.setdefault(_exp_token(f.name), []).append(f)
                for _e, fs in sorted(groups.items()):
                    for f in (fs if target else _sample(fs, per_set)):
                        todo.append((f"{loc}:{frame_key(typ, d.name, f.name)}",
                                     typ, d.name, f, loc, target))
    frames: dict = {}
    for i, (key, typ, date, f, loc, target) in enumerate(todo):
        if progress:
            progress(i + 1, len(todo), key)
        rec = {"type": typ, "date": date, "name": f.name, "path": str(f),
               "location": loc, "target": target}
        try:
            rec.update(measure_fn(f, config))
        except Exception as e:  # noqa: BLE001 - one unreadable frame
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["median"] = None
        frames[key] = rec
    timed = [k for k, r in frames.items() if r.get("date_obs") and r.get("sun_alt") is None]
    if timed and config is not None:
        try:
            for k, a in zip(timed, sun_altitudes(config, [frames[k]["date_obs"]
                                                          for k in timed])):
                frames[k]["sun_alt"] = a
        except Exception as e:  # noqa: BLE001
            logger.warning("release check: sun altitude failed: %s", e)
    try:
        from photonscript.shared.rigs import rig_readout
        default_ro = rig_readout(config, rig) if config is not None else None
    except Exception:  # noqa: BLE001
        default_ro = None
    kw = dict(rig=rig, tol=temp_tol(config) if config is not None else 1.0,
              setpoint=_setpoint(config, rig) if config is not None else None,
              scales=dark_excess_scales(config, rig) if config is not None else {},
              default_readout=default_ro,
              drift_adu=bias_drift_adu(config) if config is not None else BIAS_DRIFT_ADU)
    dm = dark_model_settings(config)
    legacy = {"frames": copy.deepcopy(frames)}
    judge_all(legacy, dark_model={**dm, "enabled": False}, **kw)
    store = {"frames": frames}
    judge_all(store, dark_model=dm, **kw)
    try:
        reasons = json.loads((qdir / "reasons.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reasons = {}
    rows = []
    for k, r in frames.items():
        if not r.get("target"):
            continue
        rows.append({"name": r["name"], "date_obs": r.get("date_obs"),
                     "sun_alt": r.get("sun_alt"), "median": r.get("median"),
                     "center": r.get("center"), "corners": r.get("corners"),
                     "excess": r.get("dark_excess"), "expected": r.get("dark_expected"),
                     "limit": r.get("dark_limit"),
                     "quarantined_for": (reasons.get(r["name"]) or {}).get("reasons", []),
                     "old_rule": legacy["frames"][k].get("verdict"),
                     "verdict": r.get("verdict"), "would_pass": passed(r),
                     "reasons": r.get("reasons") or [], "warnings": r.get("warnings") or []})
    rows.sort(key=lambda x: x["name"])
    return {"rig": rig, "dry_run": True, "quarantine_dir": str(qdir),
            "measured": len(frames), "per_set": per_set,
            "dark_model": dm, "sets": store.get("dark_model") or [],
            "bias_drift": store.get("bias_drift") or [],
            "frames": rows,
            "would_pass": [x["name"] for x in rows if x["would_pass"]],
            "still_fail": [x["name"] for x in rows if not x["would_pass"]]}


def format_release_check(rep: dict) -> str:
    """Plain text of release_check (CLI)."""
    dm = rep.get("dark_model") or {}
    lines = [f"Quarantine release check (DRY RUN, read-only: nothing moved), rig "
             f"{rep['rig']}",
             f"  {rep['quarantine_dir']}",
             f"  measured {rep['measured']} frames (all of the folder, up to "
             f"{rep['per_set']} per other set for context); dark model "
             f"{'on' if dm.get('enabled') else 'off'}, allowance "
             f"{dm.get('allow_adu', 0):g} ADU + {dm.get('allow_frac', 0) * 100:g} %",
             "", "  bias sessions (newest vs previous):"]
    for b in rep.get("bias_drift") or []:
        lines.append(f"    gain {b['gain']} offset {b['offset']} {b.get('readout') or '?'} "
                     f"SET-TEMP {b['settemp']}: {b['date']} median {b['median']:g} "
                     f"({b['n']}), previous {b.get('prev_date') or '-'} "
                     f"{'-' if b.get('prev_median') is None else format(b['prev_median'], 'g')}"
                     f", drift {'-' if b.get('drift') is None else format(b['drift'], '+g')}")
    lines += ["", "  dark sets in date order (excess = set median minus its bias, ADU):",
              f"    {'date':<10} {'exp':>5} {'set':>4} {'n':>3} {'ok':>3} {'excess':>7} "
              f"{'expect':>7} {'limit':>6} {'trend':>6} anchor  source"]
    for s in rep.get("sets") or []:
        def n(v, f="{:+.1f}"):
            return "-" if v is None else f.format(v)
        tr = (s.get("trend") or {}).get("change")
        lines.append(f"    {s['date']:<10} {s['exptime']:>5g} "
                     f"{'-' if s['settemp'] is None else s['settemp']:>4} {s['n']:>3} "
                     f"{s['n_pass']:>3} {n(s['excess']):>7} {n(s['expected']):>7} "
                     f"{n(s['limit'], '{:.1f}'):>6} {n(tr):>6} "
                     f"{'yes' if s['anchor'] else 'no':<7} {s['source']}")
    lines += ["", "  quarantined frames:"]
    for x in rep.get("frames") or []:
        lines.append(f"    {x['name']}  median {x['median']}, sun "
                     f"{'-' if x['sun_alt'] is None else x['sun_alt']} deg: old rule "
                     f"{x['old_rule']}, now {x['verdict'].upper()}"
                     f"{' (would pass)' if x['would_pass'] else ''}")
        for t in x["reasons"]:
            lines.append(f"      fail: {t}")
        for t in x["warnings"]:
            lines.append(f"      warn: {t}")
    lines += ["", f"  would pass now: {len(rep['would_pass'])} of {len(rep['frames'])}"
              + (": " + ", ".join(rep["would_pass"]) if rep["would_pass"] else ""),
              "  (releasing is a separate operator step on the scope)"]
    return "\n".join(lines)
