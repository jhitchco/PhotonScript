"""PS-171: one TPoint sample from the newest saved frame (scope PC only).

Run by NINA #1's ExternalScript after each TPoint mapping frame
(deploy\\tpoint-sample.cmd -> ``photonscript tpoint-sample``):

1. find the newest RC16 frame under image_watch_dir (or the given file),
   waiting a few seconds for NINA to finish saving it;
2. ask TheSky (TCP 3040) to Image Link it at the frame's scale
   (pixel_scale_arcsec x XBINNING) and read the solution, the mount's
   position (RA / Dec as TheSky reports it, alt / az), the sidereal time
   and the TPoint flags, all in one script;
3. when ``tpoint_sample_add`` is "auto" AND the last probe found an add
   method, the same script calls that one method after a successful solve
   (the only TheSky write PhotonScript makes, see ADD_CANDIDATES);
4. always append a row to runs/<night>_tpoint.csv (the hand-import
   fallback, HANDBOOK "TPoint mapping") and a "tpoint_sample" event line.

Never syncs, slews, connects, parks or changes a TPoint / ProTrack setting,
never clears the model. Never raises: the CLI exits 0 whatever happens, so
the NINA loop never dies on a sample.

The probe (``photonscript tpoint-sample --probe``) is READ ONLY: typeof
checks on every candidate add method, the member names of the TheSky
objects that mention TPoint / point / sample / model (so the real API can
be read off the site's build), and the TPoint flags (model points, RMS) so
a manual run's before / after can be compared. Bisque's published
scripting reference names no TPoint add method we could confirm offline:
every name in ADD_CANDIDATES is a CANDIDATE.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from photonscript.telescope_agent import thesky_client as tc

logger = logging.getLogger(__name__)

FRAME_EXTS = (".fits", ".fit", ".fts")
FRAME_WAIT_S = 30.0          # NINA saves in the background after the exposure
FRAME_MAX_AGE_S = 180.0      # a frame older than this is not tonight's point
ADD_MODES = ("off", "auto")

# (id, object, method): zero-argument calls made right after a successful
# Image Link in the same script ("add the last solution as a pointing
# sample"). CANDIDATES, in preference order; the probe reports which exist.
# The only TheSky writes in PhotonScript; the denylist test checks that
# these names appear nowhere else in the code.
ADD_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("imagelinkresults_addToTPoint", "ImageLinkResults", "addToTPoint"),
    ("imagelink_addToTPoint", "ImageLink", "addToTPoint"),
    ("tpoint_AddImageLinkResults", "TPoint", "AddImageLinkResults"),
    ("tpoint_addPointingSample", "TPoint", "addPointingSample"),
    ("tpoint_AddData", "TPoint", "AddData"),
)

# objects whose member names the probe lists (filtered by _MEMBER_RE)
PROBE_OBJECTS = ("TPoint", "ImageLink", "ImageLinkResults", "sky6RASCOMTele",
                 "AutomatedImageLinkSettings", "TheSkyXAction")
_MEMBER_RE = "tpoint|point|sample|model|rms|protrack|adddata|add"

CSV_FIELDS = ("utc", "point", "of", "side", "cmd_alt", "cmd_az", "file",
              "bin", "scale", "solved", "solved_ra_j2000_h",
              "solved_dec_j2000_d", "image_stars", "solution_rms",
              "position_angle", "mount_ra_h", "mount_dec_d", "mount_alt",
              "mount_az", "lst_h", "jd", "apply_corrections", "add_mode",
              "add_method", "added", "add_error", "note")


# ------------------------------------------------------------- scripts

def _g(expr: str) -> str:
    return f"g(function(){{return {expr};}})"


def probe_script() -> str:
    """READ ONLY. typeof checks on each add candidate, the filtered member
    names of PROBE_OBJECTS (for..in, no call), and the TPoint flags."""
    pairs: list[tuple[str, str]] = []
    for cid, obj, meth in ADD_CANDIDATES:
        pairs.append((f"has_{cid}",
                      f"(typeof {obj} != 'undefined' && typeof {obj}.{meth} == 'function') ? 1 : 0"))
    for obj in PROBE_OBJECTS:
        pairs.append((f"type_{obj}", f"typeof {obj}"))
        pairs.append((f"members_{obj}", f"members({obj})"))
    flags, _pre = tc.READ_PAIRS["tpoint_flags"]
    pairs += [(f"flag_{k}", expr) for k, expr in flags]
    pre = ("function members(o){var r=[];var re=/" + _MEMBER_RE + "/i;"
           "for (var k in o) { if (re.test(k)) { r.push(k); } }"
           "return r.sort().join(' ');}")
    return tc._js_kv(pairs, pre=pre)


_SAMPLE_READS = [
    ("exec_error", "err"),
    ("succeeded", "ImageLinkResults.succeeded"),
    ("error_text", "ImageLinkResults.errorText"),
    ("solved_ra_j2000_h", "ImageLinkResults.imageCenterRAJ2000"),
    ("solved_dec_j2000_d", "ImageLinkResults.imageCenterDecJ2000"),
    ("image_scale", "ImageLinkResults.imageScale"),
    ("position_angle", "ImageLinkResults.imagePositionAngle"),
    ("image_stars", "ImageLinkResults.imageStarCount"),
    ("solution_rms", "ImageLinkResults.solutionRMS"),
    ("connected", "c"),
    ("mount_ra_h", "c ? (sky6RASCOMTele.GetRaDec(), sky6RASCOMTele.dRa) : ''"),
    ("mount_dec_d", "c ? sky6RASCOMTele.dDec : ''"),
    ("mount_az", "c ? (sky6RASCOMTele.GetAzAlt(), sky6RASCOMTele.dAz) : ''"),
    ("mount_alt", "c ? sky6RASCOMTele.dAlt : ''"),
    ("lst_h", "(sky6Utils.ComputeLocalSiderealTime(), sky6Utils.dOut0)"),
    ("jd", tc._doc(9)),
    ("apply_corrections", "TPoint.ApplyPointingCorrections"),
    ("added", "added"),
    ("add_error", "addErr"),
]


def _candidate(cid: str) -> tuple[str, str, str]:
    for c in ADD_CANDIDATES:
        if c[0] == cid:
            return c
    raise tc.TheSkyError(f"not an add candidate: {cid!r}")


def sample_script(path: str, scale: float, add: str | None = None) -> str:
    """Image Link on `path`, then the reads in _SAMPLE_READS. add = an
    ADD_CANDIDATES id: after a successful solve the script calls that one
    zero-argument method (the only write); None = read only."""
    pre = tc.imagelink_pre(path, scale) + "var added = ''; var addErr = '';"
    if add:
        _cid, obj, meth = _candidate(add)
        pre += ("var ok = false; try { ok = (err == '' && "
                "ImageLinkResults.succeeded == 1); } catch (e) {}"
                f"if (ok) {{ try {{ {obj}.{meth}(); added = '1'; }} "
                "catch (e) { addErr = String(e.message || e); } }"
                "else { addErr = 'not solved: not added'; }")
    return tc._js_kv(_SAMPLE_READS, pre=tc._MOUNT_PRE + pre)


# ------------------------------------------------------------- parsing

def parse_probe(kv: dict) -> dict:
    """{"found": [ids with typeof function], "use": first found or None,
    "objects": {obj: {"type", "members"}}, "tpoint": {flag: value}}."""
    found = [cid for cid, _o, _m in ADD_CANDIDATES
             if str(kv.get(f"has_{cid}") or "") == "1"]
    objects = {o: {"type": kv.get(f"type_{o}"),
                   "members": (kv.get(f"members_{o}") or "").split()}
               for o in PROBE_OBJECTS}
    flags = {k[5:]: v for k, v in kv.items() if k.startswith("flag_")}
    return {"found": found, "use": found[0] if found else None,
            "objects": objects, "tpoint": flags}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool(v):
    if v is None:
        return None
    return str(v).strip().lower() in ("1", "true", "-1", "yes")


# ------------------------------------------------------------- store

def tpoint_dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "tpoint"


def probe_path(config) -> Path:
    return tpoint_dir(config) / "probe_latest.json"


def load_probe(config) -> dict | None:
    try:
        return json.loads(probe_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_probe(config, client=None, now: datetime | None = None,
              persist: bool = True) -> dict:
    """Run the read-only probe; store it (latest + a dated copy) with the
    previous probe's TPoint flags for a before / after. Never raises."""
    now = now or datetime.utcnow()
    rec: dict = {"t_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "ok": False}
    prev = load_probe(config)
    try:
        cl = client or tc.client_from_config(config)
        rec.update(parse_probe(tc.parse_kv(cl.run_script(probe_script()))))
        rec["ok"] = True
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
    if prev and prev.get("ok"):
        rec["previous"] = {"t_utc": prev.get("t_utc"),
                           "tpoint": prev.get("tpoint")}
    if persist and rec["ok"]:
        try:
            d = tpoint_dir(config)
            d.mkdir(parents=True, exist_ok=True)
            text = json.dumps(rec, indent=2)
            probe_path(config).write_text(text, encoding="utf-8")
            (d / f"probe_{now:%Y%m%d_%H%M%S}.json").write_text(text, encoding="utf-8")
        except OSError as e:
            rec["note"] = f"not saved: {e}"
    return rec


def format_probe(rec: dict) -> str:
    if not rec.get("ok"):
        return f"TPoint probe FAILED: {rec.get('error')}"
    lines = [f"TPoint probe {rec['t_utc']} (read only)"]
    lines.append("Add methods found: " + (", ".join(rec["found"]) or
                 "none (samples go to the CSV only; see HANDBOOK)"))
    lines.append(f"Would use: {rec.get('use') or '-'}")
    t = rec.get("tpoint") or {}
    pv = ((rec.get("previous") or {}).get("tpoint")) or {}
    for k in ("points", "rms_arcsec", "apply_corrections", "protrack_active",
              "protrack_active_tele", "protrack_adjustments", "ih_arcsec",
              "id_arcsec"):
        was = f" (was {pv.get(k)} at {rec['previous']['t_utc']})" \
            if rec.get("previous") and pv.get(k) != t.get(k) else ""
        lines.append(f"  TPoint {k}: {t.get(k) if t.get(k) is not None else 'unknown (name not on this build)'}{was}")
    for o, info in (rec.get("objects") or {}).items():
        lines.append(f"  {o}: {info.get('type')}; members: "
                     + (" ".join(info.get("members") or []) or "-"))
    return "\n".join(lines)


# ------------------------------------------------------------- the frame

def newest_frame(root, since_ts: float, exclude: str = "") -> Path | None:
    """Newest FITS under root modified at or after since_ts (names with
    "tpoint" preferred), skipping `exclude` (the frame sampled last)."""
    best: tuple | None = None
    try:
        it = Path(root).rglob("*")
        for f in it:
            if f.suffix.lower() not in FRAME_EXTS:
                continue
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m < since_ts or str(f) == exclude:
                continue
            key = ("tpoint" in f.name.lower(), m)
            if best is None or key > best[0]:
                best = (key, f)
    except OSError:
        return None
    return best[1] if best else None


def _stable(f: Path, wait_s: float = 1.0) -> bool:
    try:
        a = f.stat().st_size
        time.sleep(wait_s)
        return a > 0 and f.stat().st_size == a
    except OSError:
        return False


def wait_frame(root, exclude: str = "", wait_s: float = FRAME_WAIT_S,
               max_age_s: float = FRAME_MAX_AGE_S, sleep=time.sleep) -> Path | None:
    t0 = time.time()
    while True:
        f = newest_frame(root, t0 - max_age_s, exclude)
        if f is not None and _stable(f, 0.5):
            return f
        if time.time() - t0 >= wait_s:
            return None
        sleep(1.0)


def frame_bin(path) -> int:
    try:
        from astropy.io import fits
        return int(fits.getheader(str(path)).get("XBINNING") or 1)
    except Exception:  # noqa: BLE001
        return 1


# ------------------------------------------------------------- CSV / event

def csv_path(config, night: str) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "runs" / f"{night}_tpoint.csv"


def last_file(path: Path) -> str:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return rows[-1].get("file") or "" if rows else ""
    except OSError:
        return ""


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: ("" if row.get(k) is None else row.get(k))
                    for k in CSV_FIELDS})


def _event(config, night: str, row: dict, now: datetime) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z
        append_jsonl(events_path(config, night), {
            "t": iso_z(now), "rig": "rc16", "src": "photonscript",
            "kind": "tpoint_sample",
            "value": f"{row.get('point')}/{row.get('of')}",
            "solved": row.get("solved"), "added": row.get("added"),
            "add_method": row.get("add_method"), "file": row.get("file"),
            "note": row.get("note")})
    except Exception as e:  # noqa: BLE001
        logger.warning("tpoint event not logged: %s", e)


# ------------------------------------------------------------- one sample

def add_mode(config) -> str:
    m = str(getattr(config, "tpoint_sample_add", "off") or "off").strip().lower()
    return m if m in ADD_MODES else "off"


def run_sample(config, file: str | None = None, point: int | None = None,
               of: int | None = None, alt: float | None = None,
               az: float | None = None, side: str = "", client=None,
               now: datetime | None = None, dry_run: bool = False,
               wait_s: float = FRAME_WAIT_S) -> dict:
    """One sample (see the module doc). Returns the CSV row (plus "script"
    on a dry run). Never raises. dry_run: pick the frame and build the
    script, but send nothing to TheSky and write nothing."""
    from photonscript.shared.phd2_store import night_of
    now = now or datetime.utcnow()
    night = night_of(config, now)
    out = csv_path(config, night)
    mode = add_mode(config)
    row: dict = {"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "point": point,
                 "of": of, "side": side, "cmd_alt": alt, "cmd_az": az,
                 "add_mode": mode, "solved": False}
    try:
        if file:
            f = Path(file)
            if not f.is_file():
                f = None
        else:
            f = wait_frame(getattr(config, "image_watch_dir", ""),
                           exclude=last_file(out), wait_s=wait_s)
        if f is None:
            row["note"] = "no new frame found (NINA still saving, or the save failed)"
        else:
            b = frame_bin(f)
            scale = float(getattr(config, "pixel_scale_arcsec", 0.236)) * b
            row.update({"file": str(f), "bin": b, "scale": round(scale, 4)})
            use = None
            if mode == "auto":
                probe = load_probe(config)
                use = (probe or {}).get("use")
                if not use:
                    row["note"] = ("add mode auto but the probe found no add "
                                   "method: CSV only")
            row["add_method"] = use or ""
            js = sample_script(str(f), scale, add=use)
            if dry_run:
                row["script"] = js
                row["note"] = (row.get("note") or "") + " dry run: nothing sent"
                return row
            cl = client or tc.client_from_config(config)
            kv = tc.parse_kv(cl.run_script(js))
            solved = (_bool(kv.get("succeeded")) is True
                      and not kv.get("exec_error"))
            row.update({
                "solved": solved,
                "solved_ra_j2000_h": _num(kv.get("solved_ra_j2000_h")),
                "solved_dec_j2000_d": _num(kv.get("solved_dec_j2000_d")),
                "image_stars": _num(kv.get("image_stars")),
                "solution_rms": _num(kv.get("solution_rms")),
                "position_angle": _num(kv.get("position_angle")),
                "mount_ra_h": _num(kv.get("mount_ra_h")),
                "mount_dec_d": _num(kv.get("mount_dec_d")),
                "mount_alt": _num(kv.get("mount_alt")),
                "mount_az": _num(kv.get("mount_az")),
                "lst_h": _num(kv.get("lst_h")), "jd": _num(kv.get("jd")),
                "apply_corrections": kv.get("apply_corrections"),
                "added": kv.get("added") == "1",
                "add_error": kv.get("add_error") or ""})
            if not solved:
                row["note"] = (kv.get("exec_error") or kv.get("error_text")
                               or "Image Link did not solve")
    except Exception as e:  # noqa: BLE001
        row["note"] = f"{type(e).__name__}: {e}"
    if dry_run:
        return row
    try:
        append_row(out, row)
    except OSError as e:
        logger.warning("tpoint CSV not written: %s", e)
    _event(config, night, row, now)
    return row


def format_row(row: dict) -> str:
    if row.get("solved"):
        how = ("added to TPoint via " + row["add_method"]) if row.get("added") \
            else ("NOT added (" + (row.get("add_error") or "CSV only") + ")")
        return (f"TPoint sample {row.get('point')}/{row.get('of')}: solved "
                f"RA {row.get('solved_ra_j2000_h')} Dec "
                f"{row.get('solved_dec_j2000_d')} ({row.get('image_stars')} "
                f"stars), {how}")
    return (f"TPoint sample {row.get('point')}/{row.get('of')}: not solved "
            f"({row.get('note') or '?'}); CSV row written")
