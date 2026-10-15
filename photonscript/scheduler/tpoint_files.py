"""PS-171: TPoint numbers from TPoint's own files (read only).

TheSky64 10.5 build 14139 has no TPoint scripting object (2026-10-08: the
TPoint / sky6TPoint globals are undefined; TheSkyXAction's TPoint members
are numeric action ids that open TPoint windows), so the model's point
count, sky RMS, terms (IH / ID / ME / MA ...) and ProTrack state cannot be
read over TCP 3040. TPoint keeps its pointing data and model under the
user's TheSky folder; this module finds and parses those files:

* roots: ``thesky_user_dir`` when set, else ``Documents\\Software Bisque\\
  TheSky Professional Edition 64`` (and the TheSkyX / older names) in the
  service user's home. Nothing is created, moved or written there.
* list_files: every file under a root whose path mentions TPoint /
  pointing / ProTrack (depth 6, at most MAX_FILES), with size and mtime.
* parse_data: the TPOINT input format (caption line, ":" option lines, the
  site line "lat d m s  yyyy mm dd  temp press height humid wl lapse",
  one line per observation, END): point count and the run date. TheSky64
  writes "<run> in.dat" / "Super Model Indat.dat" (raw input) and
  "Super Model Outdat.dat" (the observations kept in the fit, each line
  followed by "& <extra columns>").
* parse_model: TheSky64's model file ("<run> outmod.dat", "Super Model
  Outmod.dat", seen 2026-10-08): caption, then "T <n obs> <sky RMS>
  <refraction A> <refraction B>", then one "[&][=]<TERM> <value> [<sigma>]"
  line per term ("=" = fixed, not fitted; "&" = continues the previous
  group), END. Also a TPOINT fit report ("<n> <TERM> [change] <value>
  <sigma>", "Sky RMS = x", "Popn SD = x"). The model file carries no popn
  SD: it is derived as sky RMS x sqrt(n / (n - fitted terms)).
* protrack: a "ProTrack... = on/off" style line in any listed text file.
* stats (PS-184): the BASE model (the newest full run, any model file that
  is not a recalibration) with its data file, and separately any
  RECALIBRATION run after it (a "recal" run name or caption: IH / ID and a
  few terms refitted on a short run, the rest held). The active model
  (the one ProTrack uses) is the recal when it has data, else the base.
  Polar alignment from the active model's fitted ME / MA (else the
  base's), relative to the REFRACTED pole: TheSky's Polar Alignment
  Report recommends ME = -R, R = refraction at the pole altitude (the
  latitude) from the site temperature / pressure (2026-10-10: ME -79.6",
  "Recommended ME -76.1\" (refracted pole)", lower 3"); a "Recommended
  ME" line in any listed file wins over the computed R. Sign convention
  (TPOINT, matching that report): ME > 0 = polar axis below the pole
  (raise it), MA > 0 = polar axis east of the pole (move it west); the
  azimuth advice is in Paramount MX knob form (Bisque manual: tightening
  the right azimuth knob moves the axis east, the left one west; loosen
  the opposite knob first). Check TheSky's Polar Alignment Report before
  turning a knob. Limit tpoint_polar_max_arcmin. Saved to
  <data_dir>/tpoint/stats_latest.json and, when the model or recal
  changes, a line in stats_history.jsonl (the PS-168 drift trend keys
  nights by it).

The exact file names and formats TheSky64 10.5 writes are not documented
offline: GET /api/thesky/tpoint/files lists what is there and
GET /api/thesky/tpoint/file?rel= shows the head of one listed file. The
test fixtures (tests/fixtures/tpoint/) are the real files read that way
from the scope PC on 2026-10-08 (TheSky64 build 14139).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT_NAMES = ("TheSky Professional Edition 64", "TheSkyX Professional Edition",
              "TheSky Professional Edition", "TheSky64")
MAX_FILES = 300
MAX_DEPTH = 6
MAX_BYTES = 5 * 1024 * 1024
HEAD_BYTES = 64 * 1024
# the <root>\TPoint folder (seen on the desktop copy, 2026-10-08), anything
# named for TPoint / pointing / ProTrack, and the Imaging System Profiles
# (they may carry the mount add-on's model); Camera AutoSave is skipped
# (the Automated Pointing Run frames live there)
_PATH_RE = re.compile(r"tpoint|pointing|protrack|imaging system profiles|"
                      r"\.tpt$|\.tpd$", re.IGNORECASE)
_SKIP_DIRS = {"camera autosave", "satellites", "asteroids", "comets", "sdbs",
              "star chart gifs", "movies", "theater"}
# never parsed (images, archives); anything else is read when it has no NUL
# byte in its first 4 KB (TheSky's own extensions are not documented)
_BINARY_EXTS = {".fit", ".fits", ".fts", ".jpg", ".jpeg", ".png", ".gif", ".zip",
                ".7z", ".exe", ".dll", ".bmp", ".tif", ".tiff"}

# TPOINT term names: the geometric terms (TX10 = TX with a parameter) and
# the harmonics H<result>(<S|C><coordinate>[n])+ (HDSH, HDCD2, HDSHSD,
# HXSHCD7, HDSH7CD8 ...)
_TERM_RE = re.compile(
    r"^(IH|ID|CH|NP|MA|ME|TF|TX\d*|FO|DAF|DAB|DNP|DCES|DCEC|ECES|ECEC|PDD|POX|POY|"
    r"H[A-Z](?:[SC][A-Z]\d*)+)$")
_NUM = r"[+-]?\d+(?:\.\d+)?"
# "[<n>] [&][=]<TERM> <value> [<sigma>] [<sigma>]": TheSky64 writes
# "& HHSH2  -59.6319  7.05907", " =NP  +0.0000", "&=HDSD  +270.1741"
_TERM_LINE = re.compile(
    rf"^\s*(?:\d+\s+)?([&=\s]*?)([A-Z][A-Z0-9]{{1,9}})\s+({_NUM})(?:\s+({_NUM}))?(?:\s+({_NUM}))?\s*$")
# TheSky64 model file header: "T  233  15.7683   53.048  -0.0642"
_T_LINE = re.compile(rf"^\s*T\s+(\d+)\s+({_NUM})(?:\s+({_NUM}))?(?:\s+({_NUM}))?\s*$")
_RMS_RE = re.compile(rf"sky\s*rms\s*[=:]?\s*({_NUM})", re.IGNORECASE)
_PSD_RE = re.compile(rf"popn\.?\s*sd\s*[=:]?\s*({_NUM})", re.IGNORECASE)
_NOBS_RE = re.compile(
    r"(?:no\.?\s*of\s*)?(?:observations|data\s*points|points)\s*[=:]\s*(\d+)"
    r"|(\d+)\s+(?:observations|data\s*points)\b", re.IGNORECASE)
_PROTRACK_RE = re.compile(
    r"protrack\w*\s*[=:]\s*\"?(true|false|on|off|yes|no|1|0)\b", re.IGNORECASE)
# PS-184: TheSky's Polar Alignment Report "Recommended ME -76.1" (refracted
# pole), when a listed file carries it
_REC_ME_RE = re.compile(rf"recommended\s+ME\s*[=:]?\s*({_NUM})", re.IGNORECASE)
_RECAL_RE = re.compile(r"recal", re.IGNORECASE)
# below this distance from the refracted pole a knob turn is not worth it
POLAR_NO_ADJUST_ARCSEC = 10.0


# ------------------------------------------------------------- finding

def roots(config) -> list[Path]:
    cfg = str(getattr(config, "thesky_user_dir", "") or "").strip()
    if cfg:
        return [Path(cfg)]
    home = Path.home()
    out: list[Path] = []
    for d in (home / "Documents", home / "OneDrive" / "Documents"):
        sb = d / "Software Bisque"
        out += [sb / n for n in ROOT_NAMES]
        try:   # e.g. "TheSky Professional Edition 64 2" after a reinstall
            out += sorted(p for p in sb.glob("TheSky*")
                          if p.is_dir() and p not in out)
        except OSError:
            pass
    return out


def _iso(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_files(config) -> dict:
    """{"roots": [{path, exists}], "files": [{rel, root, path, size,
    mtime}]} newest first. Read only (os.walk + stat)."""
    out_roots, files = [], []
    for r in roots(config):
        ok = False
        try:
            ok = r.is_dir()
        except OSError:
            pass
        out_roots.append({"path": str(r), "exists": ok})
        if not ok:
            continue
        base = len(r.parts)
        for dirpath, dirnames, filenames in os.walk(r):
            depth = len(Path(dirpath).parts) - base
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            for fn in filenames:
                p = Path(dirpath) / fn
                rel = str(p.relative_to(r))
                if not _PATH_RE.search(rel):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append({"rel": rel, "root": str(r), "path": str(p),
                              "size": st.st_size, "mtime": _iso(st.st_mtime),
                              "_ts": st.st_mtime})
                if len(files) >= MAX_FILES:
                    break
            if len(files) >= MAX_FILES:
                break
    files.sort(key=lambda f: f["_ts"], reverse=True)
    return {"roots": out_roots, "files": files}


def _read(path: str, limit: int = MAX_BYTES) -> str | None:
    p = Path(path)
    if p.suffix.lower() in _BINARY_EXTS:
        return None
    try:
        with open(p, "rb") as fh:
            raw = fh.read(limit)
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None                       # binary
    return raw.decode("latin-1")


def head(config, rel: str, max_bytes: int = HEAD_BYTES) -> dict:
    """The first max_bytes of one LISTED file (by its rel path), for
    checking the parsers against a real file. Anything not in the listing
    is refused."""
    lst = list_files(config)["files"]
    hit = next((f for f in lst if f["rel"] == rel), None)
    if hit is None:
        return {"ok": False, "note": f"{rel!r} is not a listed TPoint file"}
    n = max(1, min(int(max_bytes), HEAD_BYTES))
    try:
        with open(hit["path"], "rb") as fh:
            raw = fh.read(n)
    except OSError as e:
        return {"ok": False, "note": str(e)}
    binary = b"\x00" in raw[:4096]
    return {"ok": True, "rel": rel, "size": hit["size"], "mtime": hit["mtime"],
            "binary": binary,
            "text": None if binary else raw.decode("latin-1"),
            "hex": raw[:256].hex() if binary else None,
            "truncated": hit["size"] > n}


# ------------------------------------------------------------- parsing

def _nums(line: str) -> list[float] | None:
    toks = line.split("&")[0].split()   # Outdat: "<obs> & <extra columns>"
    try:
        return [float(t) for t in toks]
    except ValueError:
        return None


def parse_data(text: str) -> dict | None:
    """TPOINT input data: {"points", "caption", "options", "date"} or None
    when the text is not in that format (no site line + observations)."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    caption, options, site, obs = None, [], None, 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.upper() == "END":
            break
        if s.startswith(":"):
            options.append(s[1:].strip().upper())
            continue
        if s.startswith(("!", "#")):
            continue
        n = _nums(s.replace("+", " +").replace("-", " -")) if s[0] in "+-0123456789 " else None
        if n is None:
            if caption is None and site is None:
                caption = s
            continue
        if site is None:
            site = n
            continue
        if len(n) >= 10:
            obs += 1
    if site is None or obs == 0:
        return None
    date = None
    cond = None
    if len(site) >= 9:   # "lat d m s  yyyy mm dd  temp press height ..."
        cond = {"temp_c": site[6], "pressure_mb": site[7], "height_m": site[8]}
    if len(site) >= 6:
        try:
            y, m, d = int(site[3]), int(site[4]), int(site[5])
            if 1990 < y < 2200 and 1 <= m <= 12 and 1 <= d <= 31:
                date = f"{y:04d}-{m:02d}-{d:02d}"
        except (TypeError, ValueError):
            pass
    return {"points": obs, "caption": caption, "options": options, "date": date,
            "site": cond}


def parse_model(text: str) -> dict | None:
    """A TheSky64 model file or a TPOINT fit report: {"terms": {name:
    {"value", "sigma", "fixed"}} (file order), "sky_rms_arcsec",
    "popn_sd_arcsec", "popn_sd_source" ("file" | "derived"),
    "observations", "fitted_terms", "refraction"} or None when nothing
    model-like is in it."""
    terms: dict = {}
    rms: list[float] = []
    nobs = None
    refr = None
    caption = None
    for ln in (text or "").splitlines():
        t = _T_LINE.match(ln)
        if caption is None and ln.strip() and not t and not terms and nobs is None:
            caption = ln.strip()
        if t:
            nobs = int(t.group(1))
            # a model with no data (the recal run: "T 0 0.0000 ...") has no RMS
            rms = [float(t.group(2))] if nobs else []
            if t.group(3) is not None:
                refr = {"a_arcsec": float(t.group(3)),
                        "b_arcsec": float(t.group(4)) if t.group(4) else None}
            continue
        m = _TERM_LINE.match(ln)
        if not m or not _TERM_RE.match(m.group(2)):
            continue
        nums = [float(x) for x in m.groups()[2:] if x is not None]
        if len(nums) == 3:
            val, sig = nums[1], nums[2]
        elif len(nums) == 2:
            val, sig = nums[0], nums[1]
        else:
            val, sig = nums[0], None
        terms[m.group(2)] = {"value": val, "sigma": sig,
                             "fixed": "=" in (m.group(1) or "")}
    rms = [float(x) for x in _RMS_RE.findall(text or "")] or rms
    psd = [float(x) for x in _PSD_RE.findall(text or "")]
    for a, b in _NOBS_RE.findall(text or ""):
        nobs = int(a or b)
    if not terms and not rms:
        return None
    fitted = sum(1 for v in terms.values() if not v["fixed"])
    sky = rms[-1] if rms else None
    popn, src = (psd[-1], "file") if psd else (None, None)
    if popn is None and sky and nobs and nobs > fitted > 0:
        # TPOINT: popn SD = sqrt(sum r^2 / (n - m)), sky RMS = sqrt(sum r^2 / n)
        popn, src = round(sky * math.sqrt(nobs / (nobs - fitted)), 2), "derived"
    return {"terms": terms, "sky_rms_arcsec": sky, "popn_sd_arcsec": popn,
            "popn_sd_source": src, "observations": nobs or None,
            "fitted_terms": fitted, "refraction": refr, "caption": caption}


def parse_protrack(text: str) -> bool | None:
    m = None
    for m in _PROTRACK_RE.finditer(text or ""):
        pass
    if not m:
        return None
    return m.group(1).lower() in ("true", "on", "yes", "1")


# ------------------------------------------------------------- polar

def refraction_arcsec(alt_deg: float, pressure_mb: float = 1010.0,
                      temp_c: float = 10.0) -> float:
    """Atmospheric refraction (arcsec) at true altitude alt_deg
    (Saemundsson), scaled by pressure / temperature."""
    h = float(alt_deg)
    r_arcmin = 1.02 / math.tan(math.radians(h + 10.3 / (h + 5.11)))
    return r_arcmin * 60.0 * (pressure_mb / 1010.0) * (283.0 / (273.0 + temp_c))


def std_pressure_mb(height_m: float) -> float:
    """Standard-atmosphere pressure at height_m."""
    return 1013.25 * (1.0 - 2.25577e-5 * float(height_m)) ** 5.25588


def site_refraction(lat_deg: float, site: dict | None = None,
                    height_m: float | None = None) -> dict:
    """The refraction at the pole altitude (= latitude): {"pole_alt_deg",
    "refraction_arcsec", "temp_c", "pressure_mb", "source"}. Temperature
    and pressure from a TPoint data file's site line; that pressure is used
    only when it is within 10% of the standard atmosphere at the site
    height (TheSky64 writes its default 1000.00 mB at 1300 m, 2026-10-04),
    else the standard atmosphere; no site line: 10 C and the standard
    atmosphere at height_m."""
    site = site or {}
    h = site.get("height_m")
    h = float(h) if isinstance(h, (int, float)) and 0 <= h < 6000 else float(height_m or 0.0)
    std = std_pressure_mb(h)
    p = site.get("pressure_mb")
    t = site.get("temp_c")
    src = []
    if isinstance(p, (int, float)) and abs(p - std) <= 0.10 * std:
        src.append("pressure from the TPoint site line")
    else:
        p = std
        src.append(f"pressure: standard atmosphere at {h:.0f} m")
    if isinstance(t, (int, float)) and -40 <= t <= 50:
        src.append("temperature from the TPoint site line")
    else:
        t = 10.0
        src.append("temperature 10 C (no site line)")
    return {"pole_alt_deg": round(float(lat_deg), 3),
            "refraction_arcsec": round(refraction_arcsec(lat_deg, p, t), 1),
            "temp_c": round(float(t), 2), "pressure_mb": round(float(p), 1),
            "source": "site " + "; ".join(src)}


def model_refraction(lat_deg: float, consts: dict | None) -> dict | None:
    """The refraction at the pole altitude from a model file's T line
    refraction constants (R = A tan z + B tan^3 z, arcsec, z = 90 - lat):
    what TheSky's Polar Alignment Report uses (2026-10-10: A 47.546, B
    -0.0564 at 31.907 deg give R 76.1", its "Recommended ME -76.1").
    None without a usable A."""
    a = (consts or {}).get("a_arcsec")
    if not isinstance(a, (int, float)) or a <= 0:
        return None
    b = (consts or {}).get("b_arcsec") or 0.0
    tz = math.tan(math.radians(90.0 - float(lat_deg)))
    return {"pole_alt_deg": round(float(lat_deg), 3),
            "refraction_arcsec": round(a * tz + b * tz ** 3, 1),
            "source": f"the model's refraction constants (A {a:g}\", B {b:g}\")"}


def polar(terms: dict, max_arcmin: float = 3.0, refraction: dict | None = None,
          recommended_me: float | None = None) -> dict | None:
    """ME / MA (arcsec) -> the misalignment from the refracted pole and
    what to adjust (PS-184). Sign convention (TPOINT, matching TheSky's
    Polar Alignment Report): ME > 0 = polar axis below the pole, MA > 0 =
    polar axis east of the pole (northern hemisphere). The refracted pole
    is R above the true pole, so the recommended ME is -R (TheSky's own
    "Recommended ME" when given); without either the target is ME 0 (the
    true pole). MA's target is 0 (refraction is vertical). Azimuth advice
    in Paramount MX knob form."""
    me = (terms.get("ME") or {}).get("value")
    ma = (terms.get("MA") or {}).get("value")
    if me is None and ma is None:
        return None
    if recommended_me is not None:
        me_rec, target = float(recommended_me), "TheSky's Recommended ME (refracted pole)"
    elif refraction and refraction.get("refraction_arcsec") is not None:
        me_rec = -float(refraction["refraction_arcsec"])
        target = (f"refracted pole (R {refraction['refraction_arcsec']:.1f}\" at "
                  f"{refraction['pole_alt_deg']:g} deg from {refraction.get('source')})")
    else:
        me_rec, target = 0.0, "true pole (no refraction known)"
    d_me = (me - me_rec) if me is not None else None
    total = math.hypot(d_me or 0.0, ma or 0.0) / 60.0
    true_total = math.hypot(me or 0.0, ma or 0.0) / 60.0
    adv = []
    if d_me is not None:
        if abs(d_me) < POLAR_NO_ADJUST_ARCSEC:
            adv.append(f"altitude: within {abs(d_me):.0f}\" of the target "
                       f"({'below' if d_me > 0 else 'above'} it), no adjustment "
                       f"(ME {me:+.1f}\", recommended {me_rec:+.1f}\")")
        else:
            adv.append(f"altitude: polar axis {abs(d_me) / 60:.2f}' "
                       f"{'below the target, raise it' if d_me > 0 else 'above the target, lower it'}"
                       f" (ME {me:+.1f}\", recommended {me_rec:+.1f}\")")
    if ma is not None:
        if abs(ma) < POLAR_NO_ADJUST_ARCSEC:
            adv.append(f"azimuth: within {abs(ma):.0f}\" of the pole, no "
                       f"adjustment (MA {ma:+.1f}\")")
        elif ma > 0:
            adv.append(f"azimuth: polar axis {ma / 60:.2f}' east of the pole, "
                       f"move it west: loosen the RIGHT azimuth knob first, then "
                       f"tighten the LEFT one (MA {ma:+.1f}\")")
        else:
            adv.append(f"azimuth: polar axis {-ma / 60:.2f}' west of the pole, "
                       f"move it east: loosen the LEFT azimuth knob first, then "
                       f"tighten the RIGHT one (MA {ma:+.1f}\")")
    return {"me_arcsec": me, "ma_arcsec": ma, "me_recommended_arcsec": round(me_rec, 1),
            "target": target, "total_arcmin": round(total, 2),
            "true_pole_arcmin": round(true_total, 2),
            "ok": total <= max_arcmin, "max_arcmin": max_arcmin, "advice": adv,
            "refraction": refraction,
            "note": "TPOINT sign convention (ME > 0 = axis low, MA > 0 = axis "
                    "east); MX knobs per the Bisque manual (right knob "
                    "tightened = east, left = west); confirm with TheSky's "
                    "Polar Alignment Report before turning a knob"}


# ------------------------------------------------------------- stats

def _date(iso: str | None) -> str | None:
    return str(iso)[:10] if iso else None


_RUN_SUFFIX = re.compile(r"(?:^|\s+)(?:in|out)(?:mod|dat)?$", re.IGNORECASE)


def run_key(f: dict) -> tuple[str, str]:
    """(folder, run name) of a TPoint file: "TPoint base run outmod.dat" and
    "TPoint base run in.dat" are one run, as are "Super Model Outmod.dat",
    "Super Model Indat.dat" and "Super Model Outdat.dat"."""
    p = Path(str(f.get("path") or f.get("rel") or ""))
    return str(p.parent).lower(), _RUN_SUFFIX.sub("", p.stem).strip().lower()


def _data_rank(f: dict) -> int:
    """Within one run: the raw input (in / Indat) before Outdat (the points
    kept in the fit; the model's own count covers those)."""
    stem = Path(str(f.get("rel") or "")).stem.lower()
    return 0 if re.search(r"(?:^|\s)in(?:dat)?$", stem) else 1


def _is_recal(f: dict, m: dict) -> bool:
    """A recalibration model: "recal" in the run name or the caption."""
    return bool(_RECAL_RE.search(run_key(f)[1])
                or _RECAL_RE.search(str(m.get("caption") or "")))


def _polar_terms(active: dict | None, base: dict | None):
    """ME / MA for the polar advice: each from the active model when it
    fitted it, else from the base model when it did. ({terms}, source,
    the model ME came from)."""
    out, src, me_model = {}, [], None
    for k in ("ME", "MA"):
        for label, m in (("active", active), ("base", base)):
            v = ((m or {}).get("terms") or {}).get(k)
            if v and v.get("value") is not None and not v.get("fixed"):
                out[k] = v
                src.append(f"{k} from the {label} model")
                if k == "ME":
                    me_model = m
                break
    return out, ", ".join(src), me_model


def stats(config, listing: dict | None = None) -> dict:
    """The TPoint numbers (PS-184): the base model (newest full run) with
    the data file of the same run (else the newest data file of a non-recal
    run), any recalibration run after it, which one is active, and the
    polar alignment from the refracted pole; see the module doc. Top-level
    model keys are the BASE model's (a recal-only folder reports the recal
    there). Never raises."""
    out: dict = {"ok": False, "source": "tpoint-files"}
    try:
        lst = listing or list_files(config)
        out["roots"] = lst["roots"]
        out["files_seen"] = len(lst["files"])
        models: list[dict] = []
        prot = None
        rec_me = None
        texts: dict = {}

        def text_of(f):
            if f["path"] not in texts:
                texts[f["path"]] = _read(f["path"])
            return texts[f["path"]]

        for f in lst["files"]:                    # newest first
            text = text_of(f)
            if text is None:
                continue
            if parse_data(text) is None:
                m = parse_model(text)
                if m:
                    models.append({**m, "file": f["rel"], "mtime": f["mtime"],
                                   "_run": run_key(f), "recal": _is_recal(f, m)})
            if prot is None:
                pv = parse_protrack(text)
                if pv is not None:
                    prot = {"on": pv, "file": f["rel"]}
            if rec_me is None:
                rm = _REC_ME_RE.findall(text)
                if rm:
                    rec_me = {"value": float(rm[-1]), "file": f["rel"]}
        base = next((m for m in models if not m["recal"]), None)
        recal = next((m for m in models if m["recal"]), None)
        # a recal saved before the base model was refitted belongs to an
        # older base: shown, never the active model
        after = bool(recal and (base is None or recal["mtime"] >= base["mtime"]))
        recal_runs = {m["_run"] for m in models if m["recal"]}

        def data_for(m, fallback: bool):
            same = [f for f in lst["files"] if run_key(f) == m["_run"]]
            rest = ([f for f in lst["files"] if run_key(f) not in recal_runs]
                    if fallback else [])
            for f in sorted(same, key=_data_rank) + rest:
                text = text_of(f)
                d = parse_data(text) if text is not None else None
                if d:
                    return {**d, "file": f["rel"], "mtime": f["mtime"], "path": f["path"]}
            return None

        main = base or recal
        data = data_for(main, True) if main else None
        if main is None:              # data only, no model file
            for f in lst["files"]:
                text = text_of(f)
                d = parse_data(text) if text is not None else None
                if d:
                    data = {**d, "file": f["rel"], "mtime": f["mtime"], "path": f["path"]}
                    break
        lim = float(getattr(config, "tpoint_polar_max_arcmin", 3.0) or 3.0)
        if data:
            out.update(points=data["points"], data_file=data["file"],
                       data_path=data.get("path"),
                       data_date=data.get("date") or _date(data["mtime"]))
        if main:
            out.update(model_file=main["file"], terms=main["terms"],
                       sky_rms_arcsec=main["sky_rms_arcsec"],
                       popn_sd_arcsec=main["popn_sd_arcsec"],
                       popn_sd_source=main.get("popn_sd_source"),
                       model_points=main.get("observations"),
                       fitted_terms=main.get("fitted_terms"),
                       model_kind="recal" if main is recal else "base")
            if out.get("points") is None and main.get("observations"):
                out["points"] = main["observations"]
        out["model_date"] = (_date(main["mtime"]) if main else None) or out.get("data_date")
        rc = None
        if recal is not None and base is not None:
            rd = data_for(recal, False)
            rc = {"model_file": recal["file"], "model_date": _date(recal["mtime"]),
                  "points": (rd or {}).get("points") or recal.get("observations"),
                  "model_points": recal.get("observations"),
                  "sky_rms_arcsec": recal["sky_rms_arcsec"],
                  "popn_sd_arcsec": recal["popn_sd_arcsec"],
                  "fitted_terms": recal.get("fitted_terms"),
                  "fitted": [k for k, v in recal["terms"].items() if not v["fixed"]],
                  "terms": recal["terms"], "data_file": (rd or {}).get("file"),
                  "has_data": bool(recal.get("observations")),
                  "after_base": after}
        out["recal"] = rc
        active = recal if (rc and rc["has_data"] and after) else main
        out["active_model"] = ("recal" if active is recal and recal is not None
                               else "base" if main else None)
        out["active_model_file"] = active["file"] if active else None
        # PS-120: when the active model was written (UTC ISO), the cutoff for
        # the audit's first slews (slews before it say nothing about it)
        out["active_model_mtime"] = active["mtime"] if active else None
        if main:
            pt, psrc, me_model = _polar_terms(active, base)
            lat = float(getattr(config, "observatory_lat", 31.9))
            refr = model_refraction(lat, (me_model or active or {}).get("refraction"))
            if refr is None:
                site = (data or {}).get("site")
                if not site:
                    for f in lst["files"]:
                        text = text_of(f)
                        d = parse_data(text) if text is not None else None
                        if d and d.get("site"):
                            site = d["site"]
                            break
                refr = site_refraction(
                    lat, site, float(getattr(config, "observatory_elev", 0.0) or 0.0))
            pol = polar(pt, lim, refr, rec_me["value"] if rec_me else None)
            if pol:
                pol["terms_source"] = psrc
                if rec_me:
                    pol["recommended_me_file"] = rec_me["file"]
            out["polar"] = pol
        if prot is not None:
            out["protrack"] = prot["on"]
            out["protrack_file"] = prot["file"]
        out["ok"] = bool(data or main)
        if not out["ok"]:
            found = [r["path"] for r in lst["roots"] if r["exists"]]
            out["note"] = ("no TPoint data or model file parsed under "
                           + (", ".join(found) if found else
                              "any TheSky user folder on this PC (set thesky_user_dir)"))
    except Exception as e:  # noqa: BLE001
        out["note"] = f"{type(e).__name__}: {e}"
    return out


def stats_dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "tpoint"


def save(config, st: dict, now: datetime | None = None) -> None:
    """stats_latest.json, plus a stats_history.jsonl line when the model
    (date, points, RMS) changed. Never raises."""
    if not st.get("ok"):
        return
    now = now or datetime.utcnow()
    rec = {"t_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           **{k: st.get(k) for k in ("model_date", "points", "sky_rms_arcsec",
                                     "popn_sd_arcsec", "protrack", "data_file",
                                     "model_file", "active_model")},
           "recal_date": (st.get("recal") or {}).get("model_date"),
           "recal_rms_arcsec": (st.get("recal") or {}).get("sky_rms_arcsec"),
           "polar_arcmin": (st.get("polar") or {}).get("total_arcmin")}
    try:
        d = stats_dir(config)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stats_latest.json").write_text(json.dumps(st, indent=2, default=str),
                                             encoding="utf-8")
        hist = history(config)
        key = ("model_date", "points", "sky_rms_arcsec", "recal_date",
               "recal_rms_arcsec")
        if not hist or any(hist[-1].get(k) != rec.get(k) for k in key):
            with open(d / "stats_history.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
    except OSError as e:
        logger.warning("TPoint stats not saved: %s", e)


def history(config) -> list[dict]:
    p = stats_dir(config) / "stats_history.jsonl"
    out = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def model_for_night(config, night: str) -> dict:
    """{model_date, points, sky_rms_arcsec, source} from the history: the
    newest model whose date is not after `night`; {} when none."""
    recs = [r for r in history(config)
            if r.get("model_date") and str(r["model_date"]) <= night]
    if not recs:
        return {}
    r = max(recs, key=lambda x: (str(x["model_date"]), str(x.get("t_utc"))))
    return {"model_date": r["model_date"], "points": r.get("points"),
            "sky_rms_arcsec": r.get("sky_rms_arcsec"), "source": "TPoint files"}


def audit_observed(config) -> tuple[dict, dict]:
    """PS-104 audit source "tpoint-files": observed keys + source note."""
    st = stats(config)
    if not st.get("ok"):
        return {}, {"ok": False, "note": st.get("note") or "no TPoint file"}
    save(config, st)
    o = {"tpoint_points": st.get("points"),
         "tpoint_rms_arcsec": st.get("sky_rms_arcsec"),
         "tpoint_model_date": st.get("model_date"),
         "tpoint_model_mtime": st.get("active_model_mtime"),
         "tpoint_polar_error_arcmin": (st.get("polar") or {}).get("total_arcmin")}
    o = {k: v for k, v in o.items() if v is not None}
    return o, {"ok": True, "note": f"{st.get('data_file') or '-'} / "
                                   f"{st.get('model_file') or '-'}"}


def format_stats(st: dict) -> str:
    if not st.get("ok"):
        return f"TPoint files: {st.get('note') or 'nothing found'}"
    t = st.get("terms") or {}

    def term(n):
        v = (t.get(n) or {}).get("value")
        fx = "= " if (t.get(n) or {}).get("fixed") else " "
        return f"{fx}{v:+.1f}\"" if v is not None else " -"
    def num(k):
        v = st.get(k)
        return f"{round(v, 2):g}" if isinstance(v, (int, float)) else "?"
    mp = st.get("model_points")
    psd = (f", popn SD {num('popn_sd_arcsec')}\""
           + (" (derived)" if st.get("popn_sd_source") == "derived" else "")
           if st.get("popn_sd_arcsec") is not None else "")
    kind = ("recal run (no base model file found)"
            if st.get("model_kind") == "recal" else "base model (newest full run)")
    lines = [f"TPoint {kind}, from TPoint's files (read only): "
             f"{st.get('points') if st.get('points') is not None else '?'} points"
             + (f" ({mp} in the fit)" if mp is not None and mp != st.get("points") else "")
             + f", sky RMS {num('sky_rms_arcsec')}\"{psd}"
             f", model date {st.get('model_date') or '?'}",
             f"  terms: IH{term('IH')}, ID{term('ID')}, ME{term('ME')}, "
             f"MA{term('MA')}, CH{term('CH')}, NP{term('NP')}"
             + (f" (all {len(t)} below)" if len(t) > 6 else "")]
    if t:
        cells = [f"{n}{'=' if (v or {}).get('fixed') else ''} "
                 + (f"{v['value']:+.1f}\"" if (v or {}).get("value") is not None else "-")
                 for n, v in t.items()]
        lines.append(f"  all {len(t)} terms ({st.get('fitted_terms', '?')} fitted, "
                     "= fixed):")
        for i in range(0, len(cells), 6):
            lines.append("    " + ", ".join(cells[i:i + 6]))
    rc = st.get("recal")
    if rc:
        rpts = rc.get("points")
        lines.append(
            ("  recal run after it: " if rc.get("after_base") else
             "  recal run BEFORE it (belongs to an older base, not active): ")
            + f"{rc.get('model_date') or '?'}, "
            + (f"{rpts} points, " if rpts is not None else "")
            + (f"{rc.get('fitted_terms')} fitted ({', '.join(rc.get('fitted') or [])})"
               + (f", sky RMS {round(rc['sky_rms_arcsec'], 2):g}\""
                  if rc.get("sky_rms_arcsec") is not None else "")
               if rc.get("has_data") else "no data yet (not fitted)")
            + f" ({rc.get('model_file')})")
    elif st.get("model_kind") == "base":
        lines.append("  recal run after it: none")
    if st.get("active_model"):
        lines.append(f"  active model (ProTrack uses it): "
                     f"{'the recal run' if st['active_model'] == 'recal' else 'the base model'}"
                     f" ({st.get('active_model_file')}; newest fitted model file)")
    pol = st.get("polar")
    if pol:
        lines.append(f"  polar alignment: {pol['total_arcmin']}' from the "
                     f"{pol.get('target', 'pole')} "
                     f"({'OK' if pol['ok'] else 'over'} the {pol['max_arcmin']:g}' limit; "
                     f"{pol.get('true_pole_arcmin')}' from the true pole)")
        if pol.get("terms_source"):
            lines.append(f"    ({pol['terms_source']})")
        lines += [f"    {a}" for a in pol["advice"]]
        lines.append(f"    ({pol['note']})")
    else:
        lines.append("  polar alignment: no ME / MA term in the model file")
    pt = st.get("protrack")
    lines.append(f"  ProTrack: {'on' if pt else 'off' if pt is False else 'unknown'}"
                 + (f" ({st.get('protrack_file')})" if pt is not None else
                    " (no file states it; check TheSky: Telescope > Bisque TCS > ProTrack)"))
    lines.append(f"  files: data {st.get('data_file') or '-'}, model {st.get('model_file') or '-'}")
    return "\n".join(lines)
