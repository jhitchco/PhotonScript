"""PS-113: calibration frame QA, the quarantine, and the day vs night leak check.

Every BIAS / DARK / FLAT frame is judged before it may count or enter a
master. Checks (reason codes in brackets):

  every type   [header]  IMAGETYP matches the folder, EXPTIME / GAIN / OFFSET
                         present, bias <= 0.01 s, dark > 0.01 s; in a capture
                         job also exposure / gain / offset / binning as asked
  dark, bias   [temp]    CCD-TEMP present and within SET-TEMP +/- tol
                         (calibration_temp_tol_c, 1 C); flats: warning only
  dark         [level]   median minus the bias level of its epoch inside
                         [-BIAS_LOW_ADU, base + rate x exposure x 2^(T/6)]
  dark, bias   [leak]    center vs corner medians differ by > LEAK_ADU
  dark, bias   [stars]   more than MAX_STARS real stars (the graders' sep
                         recipe: 3x3 median, 5 sigma, minarea 6, a >= 0.6)
  dark, bias   [outlier] level off the set's own median (sets of 5 or more)
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
LIGHT_CODES = ("leak", "stars", "level", "daytime_leak")
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
    os.replace(tmp, p)


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


def frame_readout(rec: dict, default: str | None) -> tuple[str | None, bool]:
    """PS-128: (readout mode, assumed) of a QA record. A record with no
    readout (no header keyword, or measured before PS-128 and not
    backfilled yet) takes the rig default and is flagged assumed."""
    ro = rec.get("readout")
    if ro:
        return ro, False
    return default, True


def readout_matches(rec: dict, want: str | None, default: str | None) -> bool:
    """PS-128: does a record count at readout `want`? want None = readout
    not matched (camera_readout_mode blank)."""
    if not want:
        return True
    return frame_readout(rec, default)[0] == want


def _set_key(rec: dict) -> tuple:
    return (rec.get("type"), rec.get("date"), round(rec.get("exptime") or 0, 1),
            *_epoch(rec), rec.get("settemp"))


def dark_excess_limit(exp_s: float, temp_c: float | None) -> float:
    t = 0.0 if temp_c is None else float(temp_c)
    return (DARK_EXCESS_BASE_ADU
            + DARK_EXCESS_ADU_PER_S * float(exp_s or 0) * 2 ** (t / DARK_DOUBLING_C))


def judge_frame(rec: dict, *, rig: str, tol: float = 1.0,
                bias_ref: tuple | None = None,
                setpoint: float | None = None) -> tuple[list, list]:
    """Per-frame checks. Returns (fails, warnings): lists of
    {"code", "detail"}. bias_ref = (level, source) for darks."""
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
        if typ == "DARK" and bias_ref is not None and bias_ref[0] is not None:
            excess = med - bias_ref[0]
            lim = dark_excess_limit(exp or 0, ref)
            if excess < -BIAS_LOW_ADU:
                bad("level", f"median {med:g} is {-excess:.1f} ADU below bias "
                    f"{bias_ref[0]:g} ({bias_ref[1]})")
            elif excess > lim:
                bad("level", f"median {med:g} is {excess:.1f} ADU over bias "
                    f"{bias_ref[0]:g} ({bias_ref[1]}); allowed {lim:.1f} for "
                    f"{exp or 0:g} s")
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


def bias_levels(recs: dict) -> dict:
    """Bias level per epoch: median of frame-clean bias medians, preferring
    the newest session."""
    by_epoch: dict[tuple, dict[str, list]] = {}
    for r in recs.values():
        if r.get("type") != "BIAS" or r.get("median") is None:
            continue
        if any(f["code"] in ("temp", "header", "leak", "stars")
               for f in r.get("frame_fails") or []):
            continue
        by_epoch.setdefault(_epoch(r), {}).setdefault(r.get("date") or "", []).append(
            r["median"])
    out = {}
    for ep, dates in by_epoch.items():
        newest = max(dates)
        out[ep] = (_median(dates[newest]), f"bias {newest}")
    return out


def _bias_ref(rec: dict, levels: dict):
    lv = levels.get(_epoch(rec))
    if lv is not None:
        return lv
    # same gain/offset on a camera whose INSTRUME string differs (or, PS-128,
    # whose readout mode differs: HCG and LCG bias both sit at the OFFSET
    # pedestal, 254 to 256 ADU on the AP26MC): still close. Same readout first.
    for ep, v in levels.items():
        if ep[:3] == _epoch(rec)[:3] and ep[4] == rec.get("readout"):
            return v
    for ep, v in levels.items():
        if ep[:3] == _epoch(rec)[:3]:
            return v
    if rec.get("offset") is not None:
        return (float(rec["offset"]), "OFFSET keyword (no bias QA'd yet)")
    return None


def judge_all(store: dict, *, rig: str, tol: float,
              setpoint: float | None = None) -> None:
    """Recompute every verdict in the store (cheap: no file reads)."""
    recs = store["frames"]
    for r in recs.values():
        if r.get("type") == "BIAS" and r.get("median") is not None:
            f, w = judge_frame(r, rig=rig, tol=tol, setpoint=setpoint)
            r["frame_fails"], r["frame_warns"] = f, w
    levels = bias_levels(recs)
    for r in recs.values():
        if r.get("median") is None:
            continue
        if r.get("type") != "BIAS":
            f, w = judge_frame(r, rig=rig, tol=tol, bias_ref=_bias_ref(r, levels),
                               setpoint=setpoint)
            r["frame_fails"], r["frame_warns"] = f, w
        r["set_fails"] = []
        r["day_fails"] = []
    # set outliers (darks and bias)
    sets: dict[tuple, list] = {}
    for r in recs.values():
        if r.get("type") in ("DARK", "BIAS") and r.get("median") is not None:
            sets.setdefault(_set_key(r), []).append(r)
    for members in sets.values():
        clean = [r for r in members if not r["frame_fails"]]
        if len(clean) < SET_MIN:
            continue
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
        judge_all(store, rig=rig, tol=temp_tol(config),
                  setpoint=_setpoint(config, rig))
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


def file_night(config, rig: str, lib: Path, night: str, frames) -> set:
    """The filing hook (runs._link_calibration_night): QA one night's
    calibration frames as a batch. With mode quarantine every failing frame
    is filed into the quarantine (moved there when it was already linked in
    the Library, else hardlinked from the watch dir). Returns the set of
    watch-dir paths (str) the caller must NOT link into the Library. Mode
    off: no QA, empty set; mode report: QA recorded, empty set. Frames that
    could not be read ("error") are filed as before."""
    m = mode(config)
    if m == "off" or not frames:
        return set()
    recs = qa_frames(config, rig, frames)
    held: set = set()
    if m != "quarantine":
        return held
    for typ, date, path in frames:
        rec = recs.get(frame_key(typ, date, Path(path).name))
        if rec is None or rec.get("verdict") != "fail":
            continue
        held.add(str(path))
        try:
            if _lib_link(lib, rec).exists():
                quarantine(lib, rec)
            else:
                link_into_quarantine(Path(path), lib, rec)
        except OSError as e:  # still kept out of the Library
            logger.warning("PS-113: quarantine of %s failed: %s", path, e)
    if held:
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


# --- counting (readiness / gap report) ----------------------------------------------

def _within_days(date: str, days: int) -> bool:
    try:
        return (datetime.now() - datetime.strptime(date, "%Y-%m-%d")).days <= days
    except (TypeError, ValueError):
        return False


def _readout_want(config, rig: str, readout) -> tuple[str | None, str | None]:
    """PS-128: (readout to match, default for frames without one). readout
    None = the rig's (rig_readout); a blank rig setting = not matched."""
    from photonscript.shared.rigs import normalize_readout, rig_readout
    default = rig_readout(config, rig)
    want = default if readout is None else normalize_readout(readout)
    return want, default


def count_passed_darks(config, rig: str, exp_s: float, *, gain, offset,
                       setpoint: float, readout: str | None = None,
                       store: dict | None = None) -> int:
    """QA-passed darks of the epoch (PS-128: at the readout mode, the rig's
    when None; frames with no readout keyword count as the rig's)."""
    store = store or load_store(config, rig)
    days = int(getattr(config, "library_cal_days", 120))
    want, default = _readout_want(config, rig, readout)
    n = 0
    for r in store["frames"].values():
        if r.get("type") != "DARK" or not passed(r):
            continue
        if not readout_matches(r, want, default):
            continue
        if (abs((r.get("exptime") or -1) - exp_s) < 0.5 and r.get("gain") == gain
                and r.get("offset") == offset
                and abs((r.get("settemp") if r.get("settemp") is not None else 99)
                        - setpoint) < 1.5
                and _within_days(r.get("date"), days)):
            n += 1
    return n


def count_passed_bias(config, rig: str, *, gain, offset, readout: str | None = None,
                      store: dict | None = None) -> int:
    """QA-passed bias of the epoch in its newest session that has any."""
    by_date = passed_bias_sessions(config, rig, gain=gain, offset=offset,
                                   readout=readout, store=store)
    return by_date[max(by_date)] if by_date else 0


def passed_bias_sessions(config, rig: str, *, gain, offset,
                         readout: str | None = None,
                         store: dict | None = None) -> dict:
    """PS-122: {date: QA-passed bias of the epoch} per session (PS-128: at
    the readout mode, the rig's when None)."""
    store = store or load_store(config, rig)
    want, default = _readout_want(config, rig, readout)
    by_date: dict[str, int] = {}
    for r in store["frames"].values():
        if r.get("type") == "BIAS" and passed(r) and r.get("gain") == gain \
                and r.get("offset") == offset \
                and readout_matches(r, want, default):
            by_date[r["date"]] = by_date.get(r["date"], 0) + 1
    return by_date


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
