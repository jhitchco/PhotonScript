"""PS-117 part (b): `photonscript exposure-report`, the grooming measurement
on Library FITS (desktop mirror or scope Library, read-only).

Per light: sky per CFA channel (median of the darkest 10% of 64 px block
medians, bias and dark removed), a noise check (measured sky noise in those
blocks vs the camera model), saturated pixels, and the DATE-OBS gaps. With
--profile, the target's signal above local sky along its major axis at
fixed distances from the core (the M31 table in the PS-117 grooming), and
--feature-arcmin picks the feature signal to seed the target with
(ImagingProject.feature_signal_e_s; set it with PATCH /api/projects2/<id>).
The per-length model table (exposure_analysis) follows from the measured
sky, overhead and that signal.

--camera-cal re-measures read noise, gain and dark current from the
Library's bias / flat / dark frames per rig and readout mode and prints the
config keys to compare with (Choice E: config keys, refreshed by hand).

Nothing is written anywhere.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np

from photonscript.shared import exposure_analysis as ea

logger = logging.getLogger(__name__)

BLOCK = 64                 # per-channel px per sky block (the grooming's)
PROFILE_RADII = (0, 5, 10, 20, 30, 40, 50, 60)   # arcmin from the core
PROFILE_HALF = 20          # 41 x 41 superpixel box per profile point


# ------------------------------------------------------------- finding files

def library_dir(config, library: str = "") -> Path:
    if library:
        return Path(library)
    d = Path(getattr(config, "desktop_library_dir", "") or "")
    if str(d) and d.is_dir():
        return d
    from photonscript.scheduler.runs import library_root
    return library_root(config)


def light_files(lib: Path, target: str, flt: str = "") -> list[Path]:
    """Every light of `target` (its catalog name, id and alias folders,
    e.g. "Andromeda Galaxy" and "M 31"), optionally one filter folder."""
    from photonscript.scheduler.runs import library_target_dirs
    from photonscript.shared.astronomy import find_catalog_entry
    names = {target}
    e = find_catalog_entry(target)
    if e:
        names |= {e.get("name") or "", e.get("catalog_id") or ""}
    dirs: list[Path] = []
    for n in sorted(x for x in names if x):
        for d in library_target_dirs(lib, n):
            if d.is_dir() and d not in dirs:
                dirs.append(d)
    out = []
    for d in dirs:
        for sub in sorted(x for x in d.iterdir() if x.is_dir()):
            if flt and sub.name.lower() != flt.lower():
                continue
            out += sorted(p for p in sub.iterdir()
                          if p.suffix.lower() in (".fits", ".fit", ".fts"))
    return out


# ------------------------------------------------------------- one light

def _load(path: Path):
    from astropy.io import fits

    from photonscript.shared.star_measure import load_native
    return load_native(path), fits.getheader(path)


def _blocks(a: np.ndarray, box: int):
    hb, wb = a.shape[0] // box, a.shape[1] // box
    v = a[:hb * box, :wb * box].reshape(hb, box, wb, box)
    v = v.transpose(0, 2, 1, 3).reshape(hb, wb, -1)
    return np.median(v, axis=2), v


def _robust_std(x) -> float:
    x = np.asarray(x, dtype=np.float64).ravel()
    med = np.median(x)
    return float(1.4826 * np.median(np.abs(x - med)))


def channel_sky(cal: np.ndarray, sites: dict) -> tuple[dict, dict]:
    """(sky ADU, sky noise ADU) per channel of a bias + dark removed frame;
    G averages its two sites. Noise is the robust std inside the darkest
    blocks (at most 40 of them)."""
    sky: dict = defaultdict(list)
    noise: dict = defaultdict(list)
    for (r, c), ch in sites.items():
        med, v = _blocks(np.ascontiguousarray(cal[r::2, c::2]), BLOCK)
        sel = med <= np.percentile(med, 100 * ea.SKY_DARKEST)
        sky[ch].append(float(np.median(med[sel])))
        idx = np.argwhere(sel)[:40]
        noise[ch].append(float(np.median([_robust_std(v[i, j])
                                          for i, j in idx])))
    return ({k: float(np.mean(v)) for k, v in sky.items()},
            {k: float(np.mean(v)) for k, v in noise.items()})


def measure_light(path: Path, cam: ea.CameraModel,
                  profile: bool = False) -> dict:
    """The grooming's per-sub numbers for one light."""
    raw, h = _load(path)
    t = float(h.get("EXPTIME") or 0)
    rec = {"file": path.name, "dir": path.parent.parent.name,
           "exp_s": t, "date_obs": h.get("DATE-OBS"),
           "readout": h.get("READOUTM"), "object": h.get("OBJECT"),
           "sat_px_pct": round(100.0 * float((raw >= cam.saturation_adu)
                                             .mean()), 4),
           "max_adu": float(raw.max())}
    if t <= 0:
        return rec
    dark_adu = cam.dark_e_s * t / cam.gain_e_adu
    cal = raw.astype(np.float32) - np.float32(cam.bias_adu + dark_adu)
    osc = cam.osc or bool(str(h.get("BAYERPAT") or "").strip())
    sites = ea.bayer_sites(h) if osc else {(0, 0): "L", (0, 1): "L",
                                           (1, 0): "L", (1, 1): "L"}
    sky, noise = channel_sky(cal, sites)
    g = cam.gain_e_adu
    main = "G" if osc else "L"
    rates = {k: max(0.0, v * g / t) for k, v in sky.items()}
    rec["sky_adu"] = {k: round(v, 2) for k, v in sky.items()}
    rec["sky_e_s"] = {k: round(v, 4) for k, v in rates.items()}
    rec["sky_over_rn2"] = {k: round(v * t / cam.rn2_e, 1)
                           for k, v in rates.items()}
    rec["rn_penalty_pct"] = ea._r(ea.rn_penalty_pct(t, rates[main], cam), 2)
    pred = np.sqrt(sky[main] * g + cam.dark_e_s * t + cam.rn2_e) / g
    rec["noise_adu"] = round(noise[main], 2)
    rec["noise_pred_adu"] = round(float(pred), 2)
    rec["sky_sp_e_s"] = ea._r(ea.superpixel_sky(
        rates.get(main), rates if osc else None), 4)
    if profile:
        try:
            rec.update(_profile(cal, raw, sites, cam, t, rec["sky_sp_e_s"]))
        except Exception as e:  # noqa: BLE001 - a profile never costs a sub
            rec["profile_error"] = str(e)
    del raw, cal
    return rec


def _profile(cal, raw, sites, cam, t, sky_sp) -> dict:
    """Signal above local sky (e-/s per 2x2 pixel) along the major axis at
    PROFILE_RADII arcmin from the core (the brightest smoothed point).
    Local sky: darkest blocks at the same distance from the frame centre
    (vignetting is radial). Both sides measured, the fainter kept (avoids
    companions such as M32 / NGC 206 / M110). Unflattened: good to about
    20% at the faint end."""
    from astropy.stats import sigma_clipped_stats
    from scipy import ndimage
    g = cam.gain_e_adu
    sp = sum(cal[r::2, c::2][:cal.shape[0] // 2, :cal.shape[1] // 2]
             for (r, c) in sites) * (g / t)
    H, W = sp.shape
    small, _ = _blocks(np.ascontiguousarray(sp), 16)
    sm = ndimage.median_filter(small, size=5)
    cy, cx = np.unravel_index(np.argmax(ndimage.gaussian_filter(small, 1.5)),
                              small.shape)
    cy, cx = cy * 16 + 8, cx * 16 + 8
    y0, x0 = max(cy - 40, 0), max(cx - 40, 0)
    win = ndimage.gaussian_filter(sp[y0:cy + 40, x0:cx + 40], 3)
    dy, dx = np.unravel_index(np.argmax(win), win.shape)
    cy, cx = y0 + dy, x0 + dx
    thr = sky_sp + 0.15 * sky_sp
    yy, xx = np.nonzero(sm > thr)
    w = sm[sm > thr] - sky_sp
    yy, xx = yy * 16 + 8, xx * 16 + 8
    near = np.hypot(xx - cx, yy - cy) < 1600
    xx, yy, w = xx[near], yy[near], w[near]
    if len(w) < 5:
        return {"profile_sp_e_s": {}, "core_xy_sp": [int(cx), int(cy)]}
    cxx = np.average((xx - cx) ** 2, weights=w)
    cyy = np.average((yy - cy) ** 2, weights=w)
    cxy = np.average((xx - cx) * (yy - cy), weights=w)
    ang = 0.5 * np.arctan2(2 * cxy, cxx - cyy)
    ux, uy = np.cos(ang), np.sin(ang)
    bm, _ = _blocks(np.ascontiguousarray(sp), 32)
    by, bx = np.mgrid[0:bm.shape[0], 0:bm.shape[1]]
    bd = np.hypot(bx * 32 + 16 - W / 2, by * 32 + 16 - H / 2)
    sat = raw >= cam.saturation_adu
    spsat = sat[0::2, 0::2][:H, :W] | sat[1::2, 1::2][:H, :W]
    return {"core_xy_sp": [int(cx), int(cy)],
            "major_axis_deg_img": round(float(np.degrees(ang)), 1),
            "_axis": (cx, cy, ux, uy), "_bd": bd, "_bm": bm, "_sp": sp,
            "_spsat": spsat,
            "_stats": sigma_clipped_stats}


def profile_points(rec: dict, arcsec_per_sp: float,
                   radii=PROFILE_RADII) -> dict:
    """Finish a _profile: the signal at each radius (arcmin)."""
    if "_sp" not in rec:
        return rec.get("profile_sp_e_s") or {}
    cx, cy, ux, uy = rec["_axis"]
    sp, bd, bm, spsat = rec["_sp"], rec["_bd"], rec["_bm"], rec["_spsat"]
    stats = rec["_stats"]
    H, W = sp.shape
    half = PROFILE_HALF
    prof = {}
    for r_arcmin in radii:
        r_px = r_arcmin * 60.0 / arcsec_per_sp
        vals = []
        for sgn in (1, -1):
            x = int(round(cx + sgn * r_px * ux))
            y = int(round(cy + sgn * r_px * uy))
            if not (half <= x < W - half and half <= y < H - half):
                continue
            d = np.hypot(x - W / 2, y - H / 2)
            ring = np.abs(bd - d) < 150
            lsky = float(np.percentile(bm[ring], 15)) if ring.sum() > 20 \
                else 0.0
            if r_arcmin == 0:
                box = sp[y - 2:y + 3, x - 2:x + 3]
                vals.append((float(np.median(box)) - lsky,
                             bool(spsat[y - 2:y + 3, x - 2:x + 3].any())))
            else:
                box = sp[y - half:y + half + 1, x - half:x + half + 1]
                bs = spsat[y - half:y + half + 1, x - half:x + half + 1]
                vals.append((float(stats(box, sigma=2.5, maxiters=5)[1])
                             - lsky, bool(bs.mean() > 0.01)))
        if vals:
            v = min(vals, key=lambda z: z[0])
            prof[str(r_arcmin)] = {"sp_e_s": round(v[0], 3), "sides": len(vals),
                              "sat": v[1]}
    return prof


# ------------------------------------------------------------- the report

def _night_of(date_obs) -> str:
    d = ea.parse_time(date_obs)
    if d is None:
        return "?"
    # DATE-OBS is UTC; a night runs 12:00 to 12:00 local (AARO about
    # UTC - 6 h), so the evening date is the UTC date 18 h earlier
    return (d - timedelta(hours=18)).strftime("%Y-%m-%d")


def _med(xs):
    v = [float(x) for x in xs if x is not None]
    return round(float(np.median(v)), 4) if v else None


def _rng(xs):
    v = [float(x) for x in xs if x is not None]
    return [round(min(v), 4), round(max(v), 4)] if v else None


def exposure_report(config, target: str, rig: str = "piggyback",
                    flt: str = "", library: str = "", every: int = 1,
                    profile: bool = False, feature_arcmin: float | None = None,
                    signal: float | None = None,
                    goal_snr: float | None = None, limit: int = 0) -> dict:
    """Measure the target's Library lights and build the tables."""
    from photonscript.shared.rigs import rig_config
    lib = library_dir(config, library)
    files = light_files(lib, target, flt)
    if every > 1:
        files = files[::every]
    if limit > 0:
        files = files[:limit]
    view = rig_config(config, rig)
    arcsec_sp = 2.0 * float(getattr(view, "pixel_scale_arcsec", 1.0))
    subs = []
    for p in files:
        try:
            from astropy.io import fits
            ro = fits.getheader(p).get("READOUTM")
            cam = ea.CameraModel.from_config(config, rig, readout=ro)
            rec = measure_light(p, cam, profile=profile or bool(
                feature_arcmin))
            if "_sp" in rec:
                rec["profile_sp_e_s"] = profile_points(rec, arcsec_sp)
            for k in [k for k in rec if k.startswith("_")]:
                rec.pop(k)
            rec["night"] = _night_of(rec.get("date_obs"))
            subs.append(rec)
        except Exception as e:  # noqa: BLE001
            logger.warning("exposure-report: %s skipped: %s", p.name, e)
            subs.append({"file": p.name, "error": str(e)})
    good = [s for s in subs if s.get("sky_e_s")]
    cam = ea.CameraModel.from_config(
        config, rig, readout=next((s.get("readout") for s in good), None))
    main = "G" if cam.osc else "L"
    groups = defaultdict(list)
    for s in good:
        groups[(int(round(s["exp_s"])), s["night"])].append(s)
    rows = []
    for (t, night), ss in sorted(groups.items(), key=lambda kv: (kv[0][1],
                                                                 kv[0][0])):
        over = ea.overhead_from_subs([(s["night"], s["date_obs"], s["exp_s"])
                                      for s in ss])
        row = {"exp_s": t, "night": night, "n": len(ss),
               "sky_e_s": _med([s["sky_e_s"][main] for s in ss]),
               "sky_e_s_range": _rng([s["sky_e_s"][main] for s in ss]),
               "sky_sp_e_s": _med([s["sky_sp_e_s"] for s in ss]),
               "sky_over_rn2": {k: _med([s["sky_over_rn2"].get(k) for s in ss])
                                for k in ss[0]["sky_over_rn2"]},
               "rn_penalty_pct": _med([s["rn_penalty_pct"] for s in ss]),
               "noise_meas_over_pred": _med([s["noise_adu"] / s["noise_pred_adu"]
                                             for s in ss
                                             if s.get("noise_pred_adu")]),
               "sat_px_pct": _med([s["sat_px_pct"] for s in ss]),
               "max_adu": _med([s["max_adu"] for s in ss]),
               "overhead": over}
        if any(s.get("profile_sp_e_s") for s in ss):
            row["profile_sp_e_s"] = {
                str(r): _med([(s.get("profile_sp_e_s") or {}).get(str(r), {})
                              .get("sp_e_s") for s in ss])
                for r in PROFILE_RADII}
        rows.append(row)
    sig_src = "given" if signal else None
    if signal is None and feature_arcmin is not None:
        fk = f"{feature_arcmin:g}"
        vals = [(s.get("profile_sp_e_s") or {}).get(fk, {}).get("sp_e_s")
                for s in good]
        # the longest subs carry the best signal estimate
        longest = max((s["exp_s"] for s in good), default=0)
        vals_long = [(s.get("profile_sp_e_s") or {}).get(fk, {})
                     .get("sp_e_s") for s in good if s["exp_s"] == longest]
        signal = _med(vals_long) or _med(vals)
        sig_src = f"profile at {feature_arcmin:g}' ({longest:g} s subs)"
    goal = float(goal_snr or getattr(config, "light_budget_goal_snr",
                                     ea.DEFAULT_GOAL_SNR))
    sky_all = _med([s["sky_e_s"][main] for s in good])
    sp_all = _med([s["sky_sp_e_s"] for s in good])
    # the overhead of the newest night with gaps: today's sequence, not an
    # older one (09-21 ran a free loop with 0.4 s gaps)
    nights = sorted({r["night"] for r in rows if r["overhead"]["n"]})
    over_all = ea.overhead_from_subs([])
    if nights:
        over_all = dict(ea.overhead_from_subs(
            [(s["night"], s["date_obs"], s["exp_s"]) for s in good
             if s["night"] == nights[-1]]), night=nights[-1])
    model = rec_ = prog = None
    if sky_all is not None:
        model = ea.length_table(cam, sky_all, sp_all, signal=signal,
                                goal_snr=goal, overhead_s=over_all["used_s"],
                                measured={t: sum(1 for s in good if int(round(
                                    s["exp_s"])) == t)
                                    for t in ea.CANDIDATE_LENGTHS})
        rec_ = ea.recommend_length(model, cam)
        if signal:
            prog = ea.progress([(s["exp_s"], s["sky_sp_e_s"]) for s in good],
                               signal, goal, cam)
    return {"target": target, "rig": rig, "filter": flt or "all",
            "library": str(lib), "files": len(files), "measured": len(good),
            "errors": [s for s in subs if s.get("error")],
            "camera": cam.as_dict(), "channel": main,
            "sky_limited_s": None if sky_all is None else {
                str(int(p)): ea._r(ea.sky_limited_length(sky_all, cam, p), 0)
                for p in (5.0, 10.0)},
            "groups": rows, "overhead": over_all,
            "signal_e_s": signal, "signal_source": sig_src, "goal_snr": goal,
            "model_sky_e_s": sky_all, "model_sky_sp_e_s": sp_all,
            "model": model, "recommendation": rec_, "progress_all_measured":
            prog, "subs": subs}


def format_report(rep: dict) -> str:
    cam = rep["camera"]
    ch = rep["channel"]
    L = [f"Exposure report: {rep['target']} ({rep['rig']}, {rep['filter']})"
         f" from {rep['library']}",
         f"  {rep['measured']} of {rep['files']} lights measured; camera "
         f"{cam['readout']} gain {cam['gain_e_adu']} e-/ADU, RN "
         f"{cam['read_noise_adu']} ADU = {cam['read_noise_e']} e-, bias "
         f"{cam['bias_adu']}, dark {cam['dark_e_s']} e-/s", ""]
    L.append(f"  {'sub':>5} {'night':10} {'n':>3} {'sky ' + ch + ' e-/s':>11}"
             f" {'2x2 e-/s':>8} {'sky/RN2':>18} {'RN adds':>7} {'noise m/p':>9}"
             f" {'sat px%':>7} {'gap med/mean':>12}")
    for r in rep["groups"]:
        ratios = " ".join(f"{k}{v}" for k, v in r["sky_over_rn2"].items())
        o = r["overhead"]
        gap = (f"{o['median_s']}/{o['mean_s']}" if o["n"] else "-")
        L.append(f"  {r['exp_s']:>4}s {r['night']:10} {r['n']:>3} "
                 f"{r['sky_e_s']:>11} {r['sky_sp_e_s']:>8} {ratios:>18} "
                 f"{r['rn_penalty_pct']:>6}% {r['noise_meas_over_pred']:>9} "
                 f"{r['sat_px_pct']:>7} {gap:>12}")
    prof = [r for r in rep["groups"] if r.get("profile_sp_e_s")]
    if prof:
        L += ["", "  signal above local sky, e-/s per 2x2 pixel, by arcmin "
                  "from the core (major axis, fainter side)",
              "  " + f"{'sub':>5} {'night':10} " + " ".join(
                  f"{str(k) + chr(39):>7}" for k in PROFILE_RADII)]
        for r in prof:
            pr = r["profile_sp_e_s"]
            L.append(f"  {r['exp_s']:>4}s {r['night']:10} " + " ".join(
                f"{pr.get(str(k)) if pr.get(str(k)) is not None else '-':>7}"
                for k in PROFILE_RADII))
    sl = rep["sky_limited_s"]
    if sl:
        L += ["", f"  sky-limited ({ch}, median sky {rep['model_sky_e_s']} "
                  f"e-/s): RN adds 5% at {sl['5']} s, 10% at {sl['10']} s; "
                  f"overhead {rep['overhead']['used_s']} s per sub "
                  f"({rep['overhead']['source']}"
                  + (f", night {rep['overhead']['night']}"
                     if rep['overhead'].get('night') else "") + ")"]
    if rep["signal_e_s"]:
        L.append(f"  feature signal {rep['signal_e_s']} e-/s per 2x2 pixel "
                 f"({rep['signal_source']}); goal SNR {rep['goal_snr']}")
    if rep["model"]:
        L += ["", f"  {'sub':>5} {'RN adds':>7} {'sky/RN2':>7} {'w/120s':>6} "
                  f"{'SNR/h':>6} {'h to goal':>9}"]
        for r in rep["model"]:
            L.append(f"  {r['exp_s']:>4}s {r['rn_penalty_pct']:>6}% "
                     f"{r['sky_over_rn2']:>7} {r['weight_vs_120']!s:>6} "
                     f"{r['snr_per_hour']!s:>6} {r['hours_to_goal']!s:>9}")
        L += ["", "  " + ea.headline(rep["recommendation"])]
    if rep.get("progress_all_measured"):
        p = rep["progress_all_measured"]
        L.append(f"  all measured lights (accepted or not): SNR {p['snr']} = "
                 f"{p['pct_of_light']}% of the light for SNR {p['goal_snr']}")
    if rep["signal_e_s"] and rep["signal_source"] != "given":
        L.append(f"  seed the target: PATCH /api/projects2/<id> "
                 f"{{\"feature_signal_e_s\": {rep['signal_e_s']}}}")
    if rep["errors"]:
        L.append(f"  {len(rep['errors'])} file(s) skipped (see --json)")
    return "\n".join(L)


# ------------------------------------------------------------- camera cal

def _crop(a, f=0.25):
    h, w = a.shape
    return a[int(h * f):int(h * (1 - f)), int(w * f):int(w * (1 - f))]


def _cal_frames(lib: Path, rig: str, kind: str) -> list[Path]:
    base = lib / "piggyback" / "Calibration" if rig == "piggyback" \
        else lib / "Calibration"
    d = base / kind
    if not d.is_dir():
        return []
    return sorted(p for p in d.rglob("*")
                  if p.suffix.lower() in (".fits", ".fit", ".fts"))


def _by_mode(paths: list[Path]) -> dict:
    from astropy.io import fits

    from photonscript.shared.rigs import is_lcg
    out: dict = defaultdict(list)
    for p in paths:
        try:
            h = fits.getheader(p)
        except Exception:  # noqa: BLE001
            continue
        key = ("LCG" if is_lcg(h.get("READOUTM")) else "HCG",
               h.get("GAIN"), float(h.get("EXPTIME") or 0),
               round(float(h.get("CCD-TEMP", 99) or 99)))
        out[key].append(p)
    return out


def _sites(cropped, osc):
    if not osc:
        return {"L": cropped}
    return {"R": cropped[0::2, 0::2], "G1": cropped[0::2, 1::2],
            "G2": cropped[1::2, 0::2], "B": cropped[1::2, 1::2]}


def _clip_std(x) -> float:
    """Sigma-clipped std (4 sigma): unlike the MAD it is not quantized by
    integer ADU on a bias difference."""
    from astropy.stats import sigma_clipped_stats
    return float(sigma_clipped_stats(np.asarray(x, dtype=np.float64),
                                     sigma=4, maxiters=5)[2])


def ptc_gain(a, c, rn_adu: float, box: int = 64,
             min_level: float = 1000.0) -> tuple[float, float, int] | None:
    """Photon-transfer gain from two bias-subtracted flats of one channel,
    box by box (each box level-matched, so a twilight gradient between the
    two flats cancels): median over boxes of level / (var(diff) / 2 - RN^2).
    Returns (level ADU, gain e-/ADU, boxes) or None."""
    h, w = a.shape
    gs, ss = [], []
    for y in range(0, h - box + 1, box):
        for x in range(0, w - box + 1, box):
            A, C = a[y:y + box, x:x + box], c[y:y + box, x:x + box]
            ma, mc = float(np.median(A)), float(np.median(C))
            if ma < min_level or mc < min_level:
                continue
            v = _clip_std(A - C * (ma / mc)) ** 2 / 2.0 - rn_adu ** 2
            if v <= 0:
                continue
            m = (ma + mc) / 2.0
            gs.append(m / v)
            ss.append(m)
    if not gs:
        return None
    return float(np.median(ss)), float(np.median(gs)), len(gs)


def camera_cal(config, library: str = "", rigs=("piggyback", "rc16"),
               max_frames: int = 16) -> dict:
    """The grooming's calibration (PS-117 calib2): read noise from bias
    pair differences (sigma-clipped), gain from flat pairs (box-wise photon
    transfer) and dark current from cooled darks minus the master bias,
    per rig, readout mode and gain, on the central half of each frame
    (flats: central 70% of each channel)."""
    lib = library_dir(config, library)
    out = {"library": str(lib), "rigs": {}}
    for rig in rigs:
        osc = rig == "piggyback"
        res: dict = {}
        bias_groups = _by_mode(_cal_frames(lib, rig, "BIAS"))
        flats = _by_mode(_cal_frames(lib, rig, "FLAT"))
        darks = _by_mode(_cal_frames(lib, rig, "DARK"))
        for key, ps in sorted(bias_groups.items(), key=lambda kv: -len(kv[1])):
            mode, gain = key[0], key[1]
            tag = f"{mode} gain {gain}"
            if tag in res or len(ps) < 2:
                continue
            frames = [_crop(_load(p)[0]) for p in sorted(ps)[:max_frames]]
            mbias = np.median(np.stack(frames), axis=0)
            rn = defaultdict(list)
            for i in range(0, len(frames) - 1, 2):
                for k, v in _sites(frames[i] - frames[i + 1], osc).items():
                    rn[k].append(_clip_std(v) / np.sqrt(2))
            rn_site = {k: float(np.median(v)) for k, v in rn.items()}
            rn_adu = float(np.median(list(rn_site.values())))
            r = {"bias_frames": len(frames),
                 "bias_median_adu": round(float(np.median(mbias)), 2),
                 "read_noise_adu": round(rn_adu, 3)}
            del frames
            gains = []
            fps = sorted(p for fk, v in flats.items()
                         if fk[0] == mode and fk[1] == gain for p in v)
            for i in range(0, min(len(fps), max_frames) - 1, 2):
                a = _crop(_load(fps[i])[0]) - mbias
                c = _crop(_load(fps[i + 1])[0]) - mbias
                sa_, sc_ = _sites(a, osc), _sites(c, osc)
                for k in sa_:
                    if not (k.startswith("G") or k == "L"):
                        continue
                    hit = ptc_gain(_crop(sa_[k], 0.15), _crop(sc_[k], 0.15),
                                   rn_site.get(k, rn_adu))
                    if hit:
                        gains.append(hit[1])
            if gains:
                r["gain_e_adu"] = round(float(np.median(gains)), 3)
                r["flat_pairs"] = len(gains)
            rates = []
            for dk, dps in darks.items():
                if dk[0] != mode or dk[1] != gain or abs(dk[3]) > 1 \
                        or dk[2] <= 0:
                    continue
                for p in sorted(dps)[:max_frames]:
                    d = _crop(_load(p)[0]) - mbias
                    rates.append(float(np.median(d)) / dk[2])
            if rates:
                r["dark_adu_s"] = round(float(np.median(rates)), 4)
                if r.get("gain_e_adu"):
                    r["dark_e_s"] = round(r["dark_adu_s"] * r["gain_e_adu"], 4)
            res[tag] = r
        out["rigs"][rig] = res
    out["config_now"] = {
        k: getattr(config, k, None) for k in (
            "camera_read_noise_adu", "camera_read_noise_lcg_adu",
            "camera_gain_e_adu", "camera_gain_lcg_e_adu", "camera_bias_adu",
            "camera_dark_e_s", "piggyback_read_noise_adu",
            "piggyback_gain_e_adu", "piggyback_bias_adu",
            "piggyback_dark_e_s")}
    return out


def format_cal(rep: dict) -> str:
    L = [f"Camera calibration from {rep['library']} (central half, read-only)"]
    for rig, modes in rep["rigs"].items():
        if not modes:
            L.append(f"  {rig}: no bias pairs found")
        for tag, r in modes.items():
            L.append(f"  {rig} {tag}: RN {r['read_noise_adu']} ADU "
                     f"({r['bias_frames']} bias, median {r['bias_median_adu']})"
                     + (f", gain {r['gain_e_adu']} e-/ADU ({r['flat_pairs']} "
                        f"flat channel pairs)" if r.get("gain_e_adu") else
                        ", no flat pairs")
                     + (f", dark {r['dark_e_s']} e-/s" if r.get("dark_e_s")
                        is not None else ""))
    L.append("  config now: " + ", ".join(f"{k}={v}" for k, v in
                                          rep["config_now"].items()))
    L.append("  Change a key on the System page if a value moved (PS-117 "
             "Choice E: refreshed by hand).")
    return "\n".join(L)
