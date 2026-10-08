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
  one line per observation, END): point count and the run date.
* parse_model: a TPOINT fit report or model file: "<n> <TERM> [change]
  <value> <sigma>" lines (IH, ID, CH, NP, MA, ME, TF, TX, harmonics ...),
  "Sky RMS = x", "Popn SD = x", an observation count.
* protrack: a "ProTrack... = on/off" style line in any listed text file.
* stats: the newest data file and the newest model / report, merged, with
  the polar alignment from ME / MA (TPOINT's convention: ME > 0 = polar
  axis too high, MA > 0 = polar axis east of the pole; check TheSky's
  Polar Alignment Report before turning a knob) against
  tpoint_polar_max_arcmin. Saved to <data_dir>/tpoint/stats_latest.json
  and, when the model changes, a line in stats_history.jsonl (the PS-168
  drift trend keys nights by it).

The exact file names and formats TheSky64 10.5 writes are not documented
offline: GET /api/thesky/tpoint/files lists what is there and
GET /api/thesky/tpoint/file?rel= shows the head of one listed file, so the
parsers can be checked against a real file. Fixtures in the tests are
written from the documented TPOINT formats, not copied from a real file.
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

# TPOINT term names: the geometric terms and the harmonics
# H<result><S|C><coordinate>[n] (HDSH, HDCD2, HXSE ...)
_TERM_RE = re.compile(
    r"^(IH|ID|CH|NP|MA|ME|TF|TX|FO|DAF|DAB|DNP|DCES|DCEC|ECES|ECEC|PDD|POX|POY|"
    r"H[A-Z][SC][A-Z]\d?)$")
_NUM = r"[+-]?\d+(?:\.\d+)?"
_TERM_LINE = re.compile(
    rf"^\s*(?:\d+\s+)?&?([A-Z][A-Z0-9]{{1,7}})\s+({_NUM})(?:\s+({_NUM}))?(?:\s+({_NUM}))?\s*$")
_RMS_RE = re.compile(rf"sky\s*rms\s*[=:]?\s*({_NUM})", re.IGNORECASE)
_PSD_RE = re.compile(rf"popn\.?\s*sd\s*[=:]?\s*({_NUM})", re.IGNORECASE)
_NOBS_RE = re.compile(
    r"(?:no\.?\s*of\s*)?(?:observations|data\s*points|points)\s*[=:]\s*(\d+)"
    r"|(\d+)\s+(?:observations|data\s*points)\b", re.IGNORECASE)
_PROTRACK_RE = re.compile(
    r"protrack\w*\s*[=:]\s*\"?(true|false|on|off|yes|no|1|0)\b", re.IGNORECASE)


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
    toks = line.split()
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
    if len(site) >= 6:
        try:
            y, m, d = int(site[3]), int(site[4]), int(site[5])
            if 1990 < y < 2200 and 1 <= m <= 12 and 1 <= d <= 31:
                date = f"{y:04d}-{m:02d}-{d:02d}"
        except (TypeError, ValueError):
            pass
    return {"points": obs, "caption": caption, "options": options, "date": date}


def parse_model(text: str) -> dict | None:
    """A TPOINT fit report / model file: {"terms": {name: {"value",
    "sigma"}}, "sky_rms_arcsec", "popn_sd_arcsec", "observations"} or None
    when nothing model-like is in it."""
    terms: dict = {}
    for ln in (text or "").splitlines():
        m = _TERM_LINE.match(ln)
        if not m or not _TERM_RE.match(m.group(1)):
            continue
        nums = [float(x) for x in m.groups()[1:] if x is not None]
        if len(nums) == 3:
            val, sig = nums[1], nums[2]
        elif len(nums) == 2:
            val, sig = nums[0], nums[1]
        else:
            val, sig = nums[0], None
        terms[m.group(1)] = {"value": val, "sigma": sig}
    rms = [float(x) for x in _RMS_RE.findall(text or "")]
    psd = [float(x) for x in _PSD_RE.findall(text or "")]
    nobs = None
    for a, b in _NOBS_RE.findall(text or ""):
        nobs = int(a or b)
    if not terms and not rms:
        return None
    return {"terms": terms, "sky_rms_arcsec": rms[-1] if rms else None,
            "popn_sd_arcsec": psd[-1] if psd else None, "observations": nobs}


def parse_protrack(text: str) -> bool | None:
    m = None
    for m in _PROTRACK_RE.finditer(text or ""):
        pass
    if not m:
        return None
    return m.group(1).lower() in ("true", "on", "yes", "1")


# ------------------------------------------------------------- polar

def polar(terms: dict, max_arcmin: float = 3.0) -> dict | None:
    """ME / MA (arcsec) -> total misalignment and what to adjust. TPOINT
    convention: ME > 0 = polar axis too high, MA > 0 = polar axis east of
    the pole (northern hemisphere)."""
    me = (terms.get("ME") or {}).get("value")
    ma = (terms.get("MA") or {}).get("value")
    if me is None and ma is None:
        return None
    total = math.hypot(me or 0.0, ma or 0.0) / 60.0
    adv = []
    if me is not None:
        adv.append(f"altitude: polar axis {abs(me) / 60:.2f}' too "
                   f"{'high, lower it' if me > 0 else 'low, raise it'} (ME {me:+.1f}\")")
    if ma is not None:
        adv.append(f"azimuth: polar axis {abs(ma) / 60:.2f}' "
                   f"{'east of the pole, move it west' if ma > 0 else 'west of the pole, move it east'}"
                   f" (MA {ma:+.1f}\")")
    return {"me_arcsec": me, "ma_arcsec": ma, "total_arcmin": round(total, 2),
            "ok": total <= max_arcmin, "max_arcmin": max_arcmin, "advice": adv,
            "note": "TPOINT sign convention; confirm with TheSky's Polar "
                    "Alignment Report before turning a knob"}


# ------------------------------------------------------------- stats

def _date(iso: str | None) -> str | None:
    return str(iso)[:10] if iso else None


def stats(config, listing: dict | None = None) -> dict:
    """The TPoint numbers from the newest data file and the newest model /
    report (see the module doc). Never raises."""
    out: dict = {"ok": False, "source": "tpoint-files"}
    try:
        lst = listing or list_files(config)
        out["roots"] = lst["roots"]
        out["files_seen"] = len(lst["files"])
        data = model = None
        prot = None
        for f in lst["files"]:                    # newest first
            if data and model and prot is not None:
                break
            text = _read(f["path"])
            if text is None:
                continue
            if data is None:
                d = parse_data(text)
                if d:
                    data = {**d, "file": f["rel"], "mtime": f["mtime"]}
            if model is None:
                m = parse_model(text)
                if m:
                    model = {**m, "file": f["rel"], "mtime": f["mtime"]}
            if prot is None:
                pv = parse_protrack(text)
                if pv is not None:
                    prot = {"on": pv, "file": f["rel"]}
        lim = float(getattr(config, "tpoint_polar_max_arcmin", 3.0) or 3.0)
        if data:
            out.update(points=data["points"], data_file=data["file"],
                       data_date=data.get("date") or _date(data["mtime"]))
        if model:
            out.update(model_file=model["file"], terms=model["terms"],
                       sky_rms_arcsec=model["sky_rms_arcsec"],
                       popn_sd_arcsec=model["popn_sd_arcsec"],
                       polar=polar(model["terms"], lim))
            if out.get("points") is None and model.get("observations"):
                out["points"] = model["observations"]
        out["model_date"] = (_date(model["mtime"]) if model else None) or out.get("data_date")
        if prot is not None:
            out["protrack"] = prot["on"]
            out["protrack_file"] = prot["file"]
        out["ok"] = bool(data or model)
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
                                     "model_file")},
           "polar_arcmin": (st.get("polar") or {}).get("total_arcmin")}
    try:
        d = stats_dir(config)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stats_latest.json").write_text(json.dumps(st, indent=2, default=str),
                                             encoding="utf-8")
        hist = history(config)
        key = ("model_date", "points", "sky_rms_arcsec")
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
        return f"{v:+.1f}\"" if v is not None else "-"
    lines = [f"TPoint model (from TPoint's files, read only): "
             f"{st.get('points') if st.get('points') is not None else '?'} points, "
             f"sky RMS {st.get('sky_rms_arcsec') if st.get('sky_rms_arcsec') is not None else '?'}\""
             f", model date {st.get('model_date') or '?'}",
             f"  terms: IH {term('IH')}, ID {term('ID')}, ME {term('ME')}, "
             f"MA {term('MA')}, CH {term('CH')}, NP {term('NP')}"
             + (f" (+{len(t) - 6} more)" if len(t) > 6 else "")]
    pol = st.get("polar")
    if pol:
        lines.append(f"  polar alignment: {pol['total_arcmin']}' "
                     f"({'OK' if pol['ok'] else 'over'} the {pol['max_arcmin']:g}' limit)")
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
