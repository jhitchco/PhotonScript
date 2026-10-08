"""TheSky / TPoint settings audit (PS-104). REPORT ONLY.

Why: the TPoint model (and unguided ProTrack, PS-66) depends on TheSky
settings made by hand and never checked: Image Link scale and search, the
camera TheSky uses, catalogs, site and time, model on / off and age. TPoint
"Take And Image Link Photo" fails and NINA's first slew lands about 4' off;
this audit turns those into rows with a why and a fix (the PS-89 format,
config/thesky/desired_rc16.toml), shown on the Guiding tab and in the night
report. It never writes TheSky, never moves / unparks / parks / syncs the
mount and never takes an image (the read-only scripts are in
telescope_agent/thesky_client.py and a test greps them).

    collect(config)       every source, never raises:
        thesky-script     TheSky TCP 3040: version, selected hardware, mount
                          flags (no Connect), site and clock, Automated Image
                          Link settings, camera add-on status (no Connect),
                          All Sky flags only with thesky_audit_allsky_read
        astap             the newest Image Link check (imagelink_check)
        pointing-log      NINA Center log first-slew error (nina_center_log),
                          the last filter NINA moved to, PS-67's mount vs
                          solve when its record is on disk
        processes         PS-138: the Bisque sky apps running on this PC, the
                          one listening on TheSky's TCP port and the TheSky
                          NINA's mount driver targets (thesky_procs; only
                          when TheSky runs on this PC)
        manual            <data_dir>/thesky/manual.json (TPoint numbers,
                          ProTrack, run binning, catalogs), unknown when
                          older than thesky_manual_max_age_days. PS-138: for
                          the live-state rows (row field live = true: model
                          on, points, RMS, ProTrack) the record is only a
                          fallback, shown "manual (date)" as info, never a
                          pass: TheSky's own state is read first
    evaluate(desired, observed, config)   pure: pass / warn / fail / unknown
                          / info per row (phd2_audit's helpers and statuses)
    run_audit()           collect + evaluate + save
                          <data_dir>/thesky_audit/<night>.json (+ latest.json)
    imagelink_check()     ASTAP on the newest RC16 L frame (solve_store) and,
                          only when asked and the armer is idle, TheSky's own
                          Image Link on a copy in <data_dir>/thesky_audit/tmp/
    summary()             the night report line (no Pushover)
"""
from __future__ import annotations

import logging
import math
import re
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from photonscript.scheduler.phd2_audit import (
    ARMER_IDLE, FAIL, INFO, PASS, STATUS_RANK, UNKNOWN, WARN,
    _bool, _desired_text, _equals, _g, _norm, _num, _pick)
from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

SOURCES = ("thesky-script", "astap", "pointing-log", "manual", "processes",
           "tpoint-files")   # PS-171: TPoint's own files (tpoint_files.py)
FORMS = ("equals", "min", "max", "near", "computed", "report")
CONFIDENCE = ("High", "Med", "Low")
DEFAULT_FILE = (Path(__file__).resolve().parents[2] / "config" / "thesky"
                / "desired_rc16.toml")
NARROWBAND = ("ha", "oiii", "sii", "h", "o", "s")
BROADBAND = ("L", "R", "G", "B")
# Equipment changes PhotonScript knows about: (date, what, applies(config)).
# A TPoint model older than one of these was built on a different load.
KNOWN_CHANGES = (
    ("2026-09-12", "dual-rig: the 600 mm piggyback imaging camera was added",
     lambda c: bool(getattr(c, "piggyback_enabled", False))),
)
MANUAL_FIELDS = {
    # key: (type, lo, hi)
    "model_date": ("date", None, None),
    "points": ("int", 0, 2000),
    "rms_arcsec": ("float", 0, 3600),
    "polar_az_arcmin": ("float", -600, 600),
    "polar_alt_arcmin": ("float", -600, 600),
    "model_active": ("bool", None, None),
    "protrack_on": ("bool", None, None),
    "run_binning": ("int", 1, 4),
    "allsky_automated": ("bool", None, None),
    "allsky_db_installed": ("bool", None, None),
    "ucac4_installed": ("bool", None, None),
    "gaia_installed": ("bool", None, None),
    "equipment_changed_on": ("date", None, None),
    # PS-138: the model's index terms from TPoint's Model tab (arcsec), for
    # "first slew vs model index terms" (IH -3484.63 / ID -2250.28 on the
    # 2026-10-04 model)
    "ih_arcsec": ("float", -36000, 36000),
    "id_arcsec": ("float", -36000, 36000),
    "notes": ("str", None, 500),
}
# PS-138: the ProTrack fix, shown by the audit row, the Guiding tab and the
# unguided arm warning
PROTRACK_FIX = ("TheSky: with the mount connected and tracking in TheSky, Telescope > "
                "Bisque TCS > ProTrack: tick Activate ProTrack and Enable tracking "
                "adjustments (both are greyed while TheSky's mount is not connected / "
                "not tracking).")
# PS-138: two Bisque sky apps at once (2026-10-05: an older TheSkyX next to
# TheSky64 10.5); shown by the audit row and the Guiding tab
TWO_THESKY_FIX = ("In NINA disconnect the mount; open the mount driver's setup (Driver for "
                  "telescope connected through TheSky) and point it at TheSky64; close "
                  "TheSkyX; reconnect the mount in NINA. If TheSkyX keeps relaunching, the "
                  "driver is configured for it (connecting starts it).")


# --------------------------------------------------------------------------
# paths and the desired-state file
# --------------------------------------------------------------------------

def audit_dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "thesky_audit"


def manual_path(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "thesky" / "manual.json"


def lint_desired(desired: dict) -> list[str]:
    """Problems with the desired-state file (empty = fine)."""
    out, seen = [], set()
    rows = desired.get("check") or []
    if not rows:
        out.append("no [[check]] rows")
    for i, r in enumerate(rows):
        rid = r.get("id") or f"#{i}"
        if not r.get("id"):
            out.append(f"row {i}: no id")
        if rid in seen:
            out.append(f"{rid}: duplicate id")
        seen.add(rid)
        for k in ("group", "label", "why", "fix"):
            if not str(r.get(k) or "").strip():
                out.append(f"{rid}: no {k}")
        srcs = r.get("sources") or []
        if not srcs or any(s not in SOURCES for s in srcs):
            out.append(f"{rid}: bad sources {srcs}")
        if r.get("severity") not in (FAIL, WARN):
            out.append(f"{rid}: severity must be fail or warn")
        if r.get("confidence") not in CONFIDENCE:
            out.append(f"{rid}: confidence must be one of {CONFIDENCE}")
        if not any(k in r for k in FORMS):
            out.append(f"{rid}: no desired value")
        if "near" in r and "tol" not in r:
            out.append(f"{rid}: near without tol")
        if r.get("computed") and r["computed"] not in COMPUTED:
            out.append(f"{rid}: unknown computed {r['computed']}")
        if "apply" in r:
            out.append(f"{rid}: TheSky rows are report only (no apply)")
        for k, v in r.items():
            if isinstance(v, str) and not v.isascii():
                out.append(f"{rid}: {k} is not plain ASCII")
    return out


def load_desired(config) -> dict:
    import tomllib
    p = DEFAULT_FILE
    try:
        d = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"error": f"{p}: {e}", "check": [], "path": str(p)}
    d["path"] = str(p)
    problems = lint_desired(d)
    if problems:
        logger.warning("TheSky desired-state file %s: %s", p, "; ".join(problems))
        d["lint"] = problems
    return d


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _within_pct(v, want, pct) -> bool:
    return abs(v - want) <= abs(want) * float(pct) / 100.0


def _dpa180(a, b) -> float:
    """Smallest angle between two position angles, modulo 180 (a meridian
    flip turns the image 180 deg)."""
    d = abs((a - b) % 180.0)
    return min(d, 180.0 - d)


def _median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def _parse_date(s) -> date | None:
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _today(config) -> date:
    """The observatory's local date now (PS-138: from _utcnow so tests can
    pin the clock; a test near local midnight saw two different "todays")."""
    from photonscript.shared.localtime import to_local
    return to_local(config, _utcnow()).date()


def _is_armed(observed) -> bool:
    st = str(observed.get("armer_state") or "")
    return st not in ARMER_IDLE and st.upper() not in ("UNKNOWN",)


def native_scale(observed, config) -> tuple[float | None, str]:
    a = observed.get("astap") or {}
    v = _num(a.get("true_scale"))
    if v:
        return v, "ASTAP"
    v = _num(getattr(config, "pixel_scale_arcsec", None))
    return (v, "config pixel_scale_arcsec") if v else (None, "")


def run_binning(observed, row) -> tuple[int, str]:
    b = _num((observed.get("manual") or {}).get("run_binning"))
    if b:
        return int(b), "manual record"
    return int(row.get("default_bin", 1)), "assumed (enter it in the manual record)"


# --------------------------------------------------------------------------
# computed rows: (row, val, observed, config, ctx) -> (status, desired, note)
# --------------------------------------------------------------------------

def _c_mount_connected(row, val, observed, config, ctx):
    want = "connected while a night is armed"
    b = None if val is None else _bool(val)
    if b is None:
        return UNKNOWN, want, None
    if b:
        return PASS, want, None
    if _is_armed(observed):
        return row["severity"], want, f"armer is {observed.get('armer_state')}"
    return INFO, want, "not connected, nothing armed (normal by day)"


def _c_mount_tracking(row, val, observed, config, ctx):
    ts = observed.get("thesky-script") or {}
    if ts.get("mount_connected") is False:
        ctx["current"] = "not connected"
        return INFO, "tracking while imaging, parked by day", None
    if ts.get("mount_connected") is None:
        return UNKNOWN, "tracking while imaging, parked by day", None
    parked = None if ts.get("mount_parked") is None else _bool(ts["mount_parked"])
    trk = None if ts.get("mount_tracking") is None else _bool(ts["mount_tracking"])
    if parked is None and trk is None:
        return UNKNOWN, "tracking while imaging, parked by day", None
    ctx["current"] = "parked" if parked else ("tracking" if trk else "idle (not tracking)")
    return INFO, "tracking while imaging, parked by day", None


def _c_site_clock(row, val, observed, config, ctx):
    lim = float(row.get("max_s", 2.0))
    want = f"within {lim:g} s"
    v = _num(val)
    if v is None:
        return UNKNOWN, want, None
    lat = (observed.get("thesky-script") or {}).get("site_clock_latency_s")
    ctx["current"] = f"{v:+.1f} s"
    note = f"round trip {lat:.2f} s subtracted" if lat is not None else None
    return (PASS if abs(v) <= lim else row["severity"]), want, note


def _c_camera_selected(row, val, observed, config, ctx):
    want = str(row.get("want") or "").strip()
    desired = f"contains {want}" if want else "the RC16 camera (AP26MC); see Question 2"
    if val is None:
        return UNKNOWN, desired, None
    t = _norm(val)
    if not t or "nocamera" in t or t in ("none", "nodevice"):
        return row["severity"], desired, \
            "TheSky has no camera selected: Take And Image Link Photo cannot take a picture"
    if want:
        return (PASS if _norm(want) in t else row["severity"]), desired, None
    return INFO, desired, "desired camera not set yet (want = in the desired file)"


_MONTHS = ("january|february|march|april|may|june|july|august|september|october|"
           "november|december")
_DATE_LEAF = re.compile(r"^(?:(?:" + _MONTHS + r")\s+\d{1,2},?\s+\d{4}"
                        r"|\d{4}[-_. ]?\d{2}[-_. ]?\d{2})$", re.IGNORECASE)


def autosave_base(path: str) -> tuple[str, str | None]:
    """PS-119: (the folder to check, the date leaf stripped). TheSky's
    "Create a date-based subfolder" adds e.g. "October 04 2026" and creates
    it only on the first save of the day, so a missing date leaf is normal."""
    p = str(path).strip().rstrip("\\/")
    head, _sep, leaf = p.replace("/", "\\").rpartition("\\")
    if head and _DATE_LEAF.match(leaf.strip()):
        return head, leaf.strip()
    return p, None


def _c_autosave_path(row, val, observed, config, ctx):
    want = "set, autosave on, and the folder exists (date subfolder made on the first save)"
    ts = observed.get("thesky-script") or {}
    if val is None:
        return UNKNOWN, want, None
    p = str(val).strip()
    if not p:
        return row["severity"], want, "empty: Image Link has no saved image to solve"
    if ts.get("autosave_on") is not None and _bool(ts.get("autosave_on")) is False:
        return row["severity"], want, ("Automatically save photos is off: Image Link "
                                       "solves saved images only")
    host = str(getattr(config, "thesky_tcp_host", "localhost") or "localhost").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        return INFO, want, f"TheSky runs on {host}: folder not checked from here"
    base, leaf = autosave_base(p)
    try:
        ok = Path(base).is_dir()
    except OSError:
        ok = False
    checked = f"checked {base}" + (f" (the date subfolder {leaf} is created on the first "
                                    "save)" if leaf else "")
    return (PASS if ok else row["severity"]), want, \
        checked if ok else f"{base} does not exist"


def gmst_deg(jd_ut: float) -> float:
    """Greenwich mean sidereal time in degrees (the nina_center_log formula;
    good to a second of time, far finer than the E / W verdict needs)."""
    return (280.46061837 + 360.98564736629 * (jd_ut - 2451545.0)) % 360.0


def _wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def lst_longitude(observed) -> float | None:
    """PS-120: the east-positive longitude TheSky is really using, from its
    own local sidereal time and its own Julian date (both read in one
    script): LST - GMST. None when either read is missing."""
    ts = observed.get("thesky-script") or {}
    lst, jd = _num(ts.get("site_lst_h")), _num(ts.get("site_jd"))
    if lst is None or jd is None:
        return None
    if lst > 24.0:                       # a build that answers in degrees
        lst /= 15.0
    return _wrap180(15.0 * lst - gmst_deg(jd))


def _ew(lon_east: float) -> str:
    return f"{abs(lon_east):.2f} {'W' if lon_east < 0 else 'E'}"


def _c_site_longitude(row, val, observed, config, ctx):
    """PS-120: the sign of TheSky's DocumentProperty(1) cannot tell E from
    W: it read +109.021 on 2026-10-04 while the Location dialog showed
    109 01' 16" E and on 2026-10-05 after Jeremy set it to W (TheSky's
    convention is west-positive, the E / W box did not change the scripted
    value). So the script value is checked for its MAGNITUDE only, and the
    E / W verdict comes from TheSky's own local sidereal time against GMST
    at TheSky's own Julian date: a site at 109 E is 218 deg (14.5 h of LST)
    away from 109 W. Without the LST read the row is unknown (verify by
    eye), never a false FAIL."""
    want_v, tol = float(row["near"]), float(row["tol"])      # east-positive
    lst_tol = float(row.get("lst_tol_deg", 1.0))
    want = f"{_ew(want_v)} (|script| {abs(want_v):g} +/- {tol:g} deg; E / W from TheSky's LST)"
    v = _num(val)
    if v is None:
        return UNKNOWN, want, None
    if abs(abs(v) - abs(want_v)) > tol:
        return row["severity"], want, (f"TheSky's site longitude {v:+.3f} is not "
                                       f"{abs(want_v):.3f} either way: the site is wrong")
    lon = lst_longitude(observed)
    if lon is None:
        ctx["current"] = f"{v:g} (E or W? the sign does not tell)"
        return UNKNOWN, want, ("verify by eye: TheSky Input > Location shows 109 01' 16\" W "
                               "(the scripted value reads the same for E and W, and "
                               "TheSky's sidereal time was not readable)")
    ctx["current"] = f"{v:g}; LST says {_ew(lon)}"
    if abs(_wrap180(lon - want_v)) <= lst_tol:
        return PASS, want, f"TheSky's sidereal time puts the site at {_ew(lon)}"
    if abs(_wrap180(lon + want_v)) <= lst_tol:
        return FAIL, want, (f"TheSky's sidereal time puts the site at {_ew(lon)} (EAST); AARO "
                            "is 109 01' 16\" WEST: every slew and the TPoint model are wrong")
    return row["severity"], want, (f"TheSky's sidereal time implies {_ew(lon)}: check "
                                   "TheSky's location and clock")


# TheSky's daylight saving list (Input > Location): index -> name. Only the
# two seen on the site's build are named; 17 was read on 2026-10-05 with
# "U.S. and Canada" selected (PS-119 had guessed 1).
DST_US_CANADA = (17,)
DST_NAMES = {0: "not observed", 17: "U.S. and Canada"}


def dst_name(dst) -> str:
    i = int(dst)
    return f"{DST_NAMES[i]} (index {i})" if i in DST_NAMES else f"index {i}"


def _utcnow() -> datetime:
    """Now (UTC); tests pin it to a date on either side of a DST change."""
    return datetime.utcnow()


def _c_time_zone(row, val, observed, config, ctx):
    """PS-119 / PS-120: TheSky's time zone (DocumentProperty 2) and DST rule
    (DocumentProperty 4, an index into TheSky's daylight saving list: 0 =
    not observed, 17 = U.S. and Canada on TheSky 10.5 build 14139, seen
    2026-10-05 with the Location dialog set to U.S. and Canada; other
    indexes are other countries' rules). PASS: the PC zone's standard
    offset with U.S. and Canada DST. WARN: another rule, or a combination
    that gives the right local time only today (e.g. -6 with DST not
    observed while New Mexico is on MDT). FAIL: TheSky's local time differs
    from the PC's now."""
    from zoneinfo import ZoneInfo
    ts = observed.get("thesky-script") or {}
    tz, dst = _num(ts.get("time_zone")), _num(ts.get("dst_index"))
    if tz is None or dst is None:
        return UNKNOWN, "the PC's time zone with US daylight saving", None
    now = _utcnow()
    try:
        z = ZoneInfo(config.observatory_tz)
        aware = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(z)
        pc_now = aware.utcoffset().total_seconds() / 3600.0
        std = pc_now - aware.dst().total_seconds() / 3600.0
        dst_now = bool(aware.dst().total_seconds())
    except Exception:  # noqa: BLE001
        pc_now = std = float(getattr(config, "utc_offset_hours", -7.0))
        dst_now = False
    want = f"{std:+g} h with U.S. daylight saving (the PC's zone)"
    ctx["current"] = f"tz {tz:+g}, DST {dst_name(dst)}"
    eff = tz + (1.0 if int(dst) != 0 and dst_now else 0.0)
    if abs(eff - pc_now) * 60.0 > 1.0:
        return FAIL, want, (f"TheSky's local time is {eff:+g} h from UTC, the PC's is "
                            f"{pc_now:+g} h: TheSky's local time is off")
    if abs(tz - std) < 1e-6 and int(dst) in DST_US_CANADA:
        return PASS, want, None
    if int(dst) != 0 and int(dst) not in DST_US_CANADA:
        return WARN, want, (f"DST rule index {int(dst)} is not U.S. and Canada "
                            f"(index {DST_US_CANADA[0]}): its change dates may differ")
    return WARN, want, ("correct only while the PC is on daylight time; one hour off "
                        "after the next DST change" if dst_now else
                        "correct only until the next DST change")


def _c_filter_in_beam(row, val, observed, config, ctx):
    want = "L or a broadband filter"
    if val is None:
        return UNKNOWN, want, "no filter change in the newest NINA logs"
    pl = observed.get("pointing-log") or {}
    note = f"{pl.get('last_filter_src') or 'NINA log'}"
    canon = str(val).strip()
    try:
        canon = config.reverse_filter_map().get(canon, canon)
    except Exception:  # noqa: BLE001
        pass
    if canon.lower() in NARROWBAND:
        return row["severity"], want, f"{note}: a 3 nm filter leaves too few stars in 10 s"
    if canon.upper() in BROADBAND:
        return PASS, want, note
    return INFO, want, note


def _c_ails_image_scale(row, val, observed, config, ctx):
    ns, ns_src = native_scale(observed, config)
    b, b_src = run_binning(observed, row)
    tol = float(row.get("tol_pct", 2.0))
    if not ns:
        return UNKNOWN, f"native scale x {b} (binning)", "native scale unknown"
    exp = ns * b
    want = f"{exp:.3f} +/- {tol:g}% ({ns:.4f} {ns_src} x bin {b}, {b_src})"
    v = _num(val)
    if v is None:
        return UNKNOWN, want, None
    if _within_pct(v, exp, tol):
        return PASS, want, None
    ratio = v / ns
    if abs(ratio - 4) / 4 < 0.03:
        note = (f"set for 4x4 binning (the HANDBOOK 4x4 era model, 0.942\"/px): "
                f"the run bins {b}, set {exp:.3f}")
    elif abs(ratio - 1) < 0.03:
        note = "native (1x1) scale: either the run bins 1x1 or this is wrong"
    else:
        note = f"{100 * (v - exp) / exp:+.1f}% off"
    return row["severity"], want, note


def _c_ails_position_angle(row, val, observed, config, ctx):
    pa = _num((observed.get("astap") or {}).get("astap_pa"))
    v = _num(val)
    want = "ASTAP camera angle (a hint only)"
    if v is None:
        return UNKNOWN, want, None
    if pa is None:
        return INFO, want, "no ASTAP check yet"
    return INFO, f"{pa:.1f} deg (ASTAP, a hint only)", f"differs by {_dpa180(v, pa):.1f} deg (mod 180)"


def _c_imagelink_selftest(row, val, observed, config, ctx):
    chk = observed.get("imagelink") or {}
    ts = chk.get("thesky") or {}
    tol = float(row.get("tol_pct", 1.0))
    want = f"succeeds, scale within {tol:g}% of ASTAP"
    if not ts or ts.get("skipped"):
        ctx["current"] = "not run"
        return UNKNOWN, want, (ts.get("note") if ts else None) or \
            "run it with the Guiding tab button (armer idle)"
    when = chk.get("t_utc") or ""
    if not ts.get("succeeded"):
        ctx["current"] = f"failed {when}"
        why = "; ".join(str(x) for x in (ts.get("error_code") and f"error {ts['error_code']}",
                                          ts.get("error_text"), ts.get("exec_error"),
                                          ts.get("note")) if x)
        return row["severity"], want, why or "Image Link did not succeed"
    sc = _num(ts.get("image_scale"))
    ref = _num((chk.get("astap") or {}).get("scale"))
    ctx["current"] = f"succeeded {when}, {_g(sc)}\"/px, RMS {_g(ts.get('solution_rms'))}"
    if sc is None or ref is None:
        return PASS, want, None
    if _within_pct(sc, ref, tol):
        return PASS, want, None
    return WARN, want, f"TheSky {sc:.4f} vs ASTAP {ref:.4f}\"/px"


def _allsky_on(observed) -> bool | None:
    for src in ("thesky-script", "manual"):
        v = (observed.get(src) or {}).get("allsky_automated")
        if v is not None and _bool(v) is not None:
            return _bool(v)
    return None


def catalog_solve_evidence(observed) -> str | None:
    """PS-120: why a solve without All Sky is known to work, or None."""
    m = observed.get("manual") or {}
    if m.get("allsky_automated") is not None and _bool(m["allsky_automated"]) is False:
        return "the manual record says the model was built with All Sky off"
    if ((observed.get("imagelink") or {}).get("thesky") or {}).get("succeeded"):
        return "TheSky's Image Link self-test solved a frame"
    cats = [n for k, n in (("ucac4_installed", "UCAC4"), ("gaia_installed", "Gaia"))
            if m.get(k) is not None and _bool(m[k])]
    if cats:
        return f"{' and '.join(cats)} installed (manual record)"
    return None


def _c_allsky(row, val, observed, config, ctx):
    """PS-120: All Sky Image Link is optional. Off is fine when a
    catalog-based solve works (UCAC4 / Gaia: the 2026-10-04 model was built
    that way, the All Sky database is not installed); on is a problem only
    without its database."""
    want = "on with the All Sky database, or off with a catalog-based solve"
    on = None if val is None else _bool(val)
    if on is None:
        return UNKNOWN, want, None
    db = (observed.get("manual") or {}).get("allsky_db_installed")
    db = None if db is None else _bool(db)
    if on:
        if db:
            return PASS, want, None
        if db is False:
            return row["severity"], want, ("All Sky is on but its database is not installed: "
                                           "every automated link fails")
        return UNKNOWN, want, "on: enter whether the All Sky database is installed"
    why = catalog_solve_evidence(observed)
    if why:
        return PASS, want, f"off; {why}"
    return row["severity"], want, ("off and no catalog-based solve confirmed yet: run the "
                                   "TheSky Image Link self-test or record UCAC4 / Gaia")


def _c_allsky_db(row, val, observed, config, ctx):
    want = "installed while All Sky Image Link is on"
    b = None if val is None else _bool(val)
    if b is None:
        return UNKNOWN, want, None
    if b:
        return PASS, want, None
    if _allsky_on(observed) is False:
        return INFO, want, "not needed: All Sky Image Link is off"
    return row["severity"], want, None


def _c_true_scale(row, val, observed, config, ctx):
    cfg = _num(getattr(config, "pixel_scale_arcsec", None))
    tol = float(row.get("tol_pct", 1.0))
    want = f"{_g(cfg)} +/- {tol:g}% (config pixel_scale_arcsec)"
    v = _num((observed.get("astap") or {}).get("true_scale"))
    if v is None:
        return UNKNOWN, want, "no ASTAP Image Link check yet"
    ctx["current"] = f"{v:.4f}"
    if not cfg:
        return INFO, want, None
    return (PASS if _within_pct(v, cfg, tol) else row["severity"]), want, \
        None if _within_pct(v, cfg, tol) else f"{100 * (v - cfg) / cfg:+.1f}% vs config"


def _pa_baseline(observed) -> tuple[float | None, str]:
    """Median ASTAP angle near the model date (14 days), else the first
    stored checks."""
    hist = observed.get("astap_history") or []
    md = _parse_date((observed.get("manual_record") or {}).get("model_date"))
    if md:
        near = [h["pa"] for h in hist if h.get("pa") is not None
                and (d := _parse_date(h.get("t_utc"))) and abs((d - md).days) <= 14]
        if near:
            return _median(near), f"median of {len(near)} check(s) near the model date"
    first = [h["pa"] for h in hist[:3] if h.get("pa") is not None]
    if first:
        return _median(first), "first stored checks (none near the model date)"
    return None, ""


def _c_camera_pa_stable(row, val, observed, config, ctx):
    lim = float(row.get("max_deg", 1.0))
    want = f"within {lim:g} deg of the model date (mod 180)"
    pa = _num((observed.get("astap") or {}).get("astap_pa"))
    if pa is None:
        return UNKNOWN, want, "no ASTAP Image Link check yet"
    base, src = _pa_baseline(observed)
    ctx["current"] = f"{pa:.2f} deg"
    if base is None:
        return UNKNOWN, want, "no baseline"
    d = _dpa180(pa, base)
    ctx["pa_moved_deg"] = d
    return (PASS if d <= lim else row["severity"]), want, f"{d:.2f} deg from {base:.2f} ({src})"


def _c_parity(row, val, observed, config, ctx):
    hist = observed.get("astap_history") or []
    p = (observed.get("astap") or {}).get("parity")
    want = "same as the first stored check"
    if p is None:
        return UNKNOWN, want, "no ASTAP Image Link check yet"
    ctx["current"] = "normal" if p == -1 else "mirrored"
    base = next((h.get("parity") for h in hist if h.get("parity") is not None), None)
    if base is None or base == p:
        return INFO, want, None
    return WARN, want, "parity changed since the first check"


def _c_tpoint_threshold(row, val, observed, config, ctx):
    lim = _num(getattr(config, row.get("cfg", ""), None))
    op = row.get("op", "min")
    want = f"{'>=' if op == 'min' else '<='} {_g(lim)} ({row.get('cfg')})"
    v = _num(val)
    if v is None or lim is None:
        return UNKNOWN, want, None
    ok = v >= lim if op == "min" else v <= lim
    return (PASS if ok else row["severity"]), want, None


def _model_age(observed, config) -> int | None:
    md = _parse_date((observed.get("manual_record") or {}).get("model_date"))
    return (_today(config) - md).days if md else None


def _c_tpoint_age(row, val, observed, config, ctx):
    lim = float(getattr(config, "tpoint_max_age_days", 90) or 90)
    want = f"under {lim:g} days (warn at {0.75 * lim:g})"
    age = _model_age(observed, config) if val is not None else None
    if age is None:
        return UNKNOWN, want, None
    ctx["current"] = f"{age} d (built {val})"
    if age > lim:
        return FAIL, want, None
    if age > 0.75 * lim:
        return WARN, want, None
    return PASS, want, None


def rebuild_reasons(observed, config) -> tuple[list[str], list[str]]:
    """(rebuild reasons, watch reasons) for the TPoint model."""
    rebuild, watch = [], []
    rec = observed.get("manual_record") or {}
    md = _parse_date(rec.get("model_date"))
    lim = float(getattr(config, "tpoint_max_age_days", 90) or 90)
    age = _model_age(observed, config)
    if age is not None:
        if age > lim:
            rebuild.append(f"model is {age} d old (limit {lim:g})")
        elif age > 0.75 * lim:
            watch.append(f"model is {age} d old (75% of {lim:g})")
    if md:
        ch = _parse_date(rec.get("equipment_changed_on"))
        if ch and ch > md:
            rebuild.append(f"equipment changed on {ch} (after the model)")
        for d, what, applies in KNOWN_CHANGES:
            try:
                hit = applies(config)
            except Exception:  # noqa: BLE001
                hit = False
            if hit and _parse_date(d) > md:
                rebuild.append(f"{what} on {d}, after the model ({md})")
        rigs_then = rec.get("rigs")
        rigs_now = observed.get("rigs")
        if rigs_then and rigs_now and sorted(rigs_then) != sorted(rigs_now):
            rebuild.append(f"rigs changed since the record ({'+'.join(rigs_then)} -> "
                           f"{'+'.join(rigs_now)})")
    a = observed.get("astap") or {}
    pa = _num(a.get("astap_pa"))
    base, _src = _pa_baseline(observed)
    if pa is not None and base is not None and _dpa180(pa, base) > 1.0:
        rebuild.append(f"camera angle moved {_dpa180(pa, base):.1f} deg")
    hist = [h for h in observed.get("astap_history") or [] if h.get("native_scale")]
    if hist and a.get("true_scale"):
        s0 = hist[0]["native_scale"]
        if abs(a["true_scale"] - s0) / s0 > 0.01:
            rebuild.append(f"image scale moved {100 * (a['true_scale'] - s0) / s0:+.1f}%")
    warn_l = float(getattr(config, "pointing_first_slew_warn_arcmin", 2) or 2)
    fail_l = float(getattr(config, "pointing_first_slew_fail_arcmin", 5) or 5)
    sides = ((observed.get("pointing-log") or {}).get("by_side") or {})
    try:
        shift = bool((index_shift(observed, config) or {}).get("match"))
    except Exception:  # noqa: BLE001
        shift = False
    if shift:
        # PS-138: the first slews miss by the model's IH / ID: Recalibrate
        watch.append("first slews match the model's IH / ID: TPoint Recalibrate, "
                     "not a rebuild")
    for side, st in sides.items():
        m = _num((st or {}).get("median_arcmin"))
        if m is None:
            continue
        if m > fail_l and not shift:
            rebuild.append(f"first-slew median {m:.1f}' on side {side} (fail {fail_l:g}')")
        elif m > warn_l:
            watch.append(f"first-slew median {m:.1f}' on side {side} (warn {warn_l:g}')")
    return rebuild, watch


def _c_tpoint_rebuild(row, val, observed, config, ctx):
    want = "no: model fresh, same equipment, first slews close"
    rebuild, watch = rebuild_reasons(observed, config)
    ctx["reasons"] = rebuild + watch
    if rebuild:
        ctx["current"] = "REBUILD"
        return row["severity"], want, "; ".join(rebuild + watch)
    if watch:
        ctx["current"] = "watch"
        return WARN, want, "; ".join(watch)
    if not (observed.get("manual_record") or {}).get("model_date"):
        ctx["current"] = "unknown"
        return UNKNOWN, want, "no model date: enter the TPoint record below"
    pl = observed.get("pointing-log") or {}
    if pl.get("since_utc") and not pl.get("runs"):
        # PS-120: slews from before the model say nothing about it
        ctx["current"] = "pending"
        return UNKNOWN, want, ("model fresh, same equipment; no NINA first slews since the "
                               "model yet (n=0): judged after the next imaging night")
    ctx["current"] = "no"
    return PASS, want, None


def first_slew_text(pointing: dict | None) -> str | None:
    sides = (pointing or {}).get("by_side") or {}
    parts = [f"{_g(st.get('median_arcmin'))}' {s}" for s, st in sides.items()
             if st and st.get("n")]
    return " / ".join(parts) or None


def _c_first_slew_error(row, val, observed, config, ctx):
    warn_l = float(getattr(config, "pointing_first_slew_warn_arcmin", 2) or 2)
    fail_l = float(getattr(config, "pointing_first_slew_fail_arcmin", 5) or 5)
    want = f"median under {warn_l:g}' per side (fail above {fail_l:g}')"
    pl = observed.get("pointing-log") or {}
    sides = pl.get("by_side") or {}
    meds = [(s, _num(st.get("median_arcmin")), st.get("n")) for s, st in sides.items()
            if st and st.get("n")]
    if not meds:
        if pl.get("since_utc"):
            ctx["current"] = "no slews since the model (n=0)"
        return UNKNOWN, want, pl.get("note") or "no NINA Center runs in the last 14 nights"
    ctx["current"] = " / ".join(f"{m:.1f}' {s} (n={n})" for s, m, n in meds)
    if pl.get("since_utc"):
        ctx["current"] += f" since the model ({pl.get('model_date')})"
    worst = max(m for _s, m, _n in meds)
    src = ", ".join(pl.get("side_src") or [])
    note = "side from the hour angle (no pier side in the log)" if src == "ha" else None
    if worst > fail_l:
        return row["severity"], want, note
    if worst > warn_l:
        return WARN, want, note
    return PASS, want, note


def _c_first_slew_pattern(row, val, observed, config, ctx):
    pl = observed.get("pointing-log") or {}
    want = "reported only"
    bands = []
    for key in ("by_dec", "by_ha"):
        for name, st in (pl.get(key) or {}).items():
            if st and st.get("n"):
                bands.append((name, st))
    if not bands:
        return UNKNOWN, want, pl.get("note") or "no NINA Center runs in the last 14 nights"
    ctx["current"] = "; ".join(
        f"{n}: {_g(st.get('median_arcmin'))}' (E {_g(st.get('median_east_arcmin'))}, "
        f"N {_g(st.get('median_north_arcmin'))})" for n, st in bands)
    es = [_num(st.get("median_east_arcmin")) for _n, st in bands]
    ns = [_num(st.get("median_north_arcmin")) for _n, st in bands]
    es, ns = [e for e in es if e is not None], [n for n in ns if n is not None]
    if len(es) >= 2 and len(ns) >= 2:
        spread = math.hypot(max(es) - min(es), max(ns) - min(ns))
        mag = math.hypot(_median(es), _median(ns))
        note = ("about constant across Dec and HA: looks like a sync / home offset"
                if spread < 0.5 * max(mag, 0.5) else
                "changes with Dec / HA: model terms or flexure")
        return INFO, want, note
    return INFO, want, "too few bands for a pattern"


def _c_mount_vs_solve(row, val, observed, config, ctx):
    ps = (observed.get("pointing-log") or {}).get("ps67") or {}
    want = "reported only"
    if not ps.get("n"):
        ctx["current"] = "-"
        return INFO, want, "PS-67 mount vs solve record not on disk"
    bp = ps.get("by_pier") or {}
    ctx["current"] = (f"median {_g(ps.get('median_arcmin'))}' (n={ps['n']}; "
                      f"E {_g(bp.get('E'))}', W {_g(bp.get('W'))}')")
    return INFO, want, "after centering (PS-67), a different quantity from the first slew"


def _manual_tag(observed) -> str:
    """'manual (2026-10-04)': the record's entry date, for a fallback value."""
    ent = str((observed.get("manual_record") or {}).get("entered_at") or "")[:10]
    return f"manual ({ent or 'undated'})"


def _live_state(observed) -> tuple[bool | None, bool | None]:
    """(TheSky's mount connected, tracking) from the live read, None unknown."""
    ts = observed.get("thesky-script") or {}
    c = ts.get("mount_connected")
    c = None if c is None else _bool(c)
    t = ts.get("mount_tracking")
    t = None if t is None else _bool(t)
    if c is False:
        t = False
    return c, t


def _c_protrack(row, val, observed, config, ctx):
    """PS-138: ProTrack from TheSky, never from the manual record. ProTrack
    (Activate ProTrack + Enable tracking adjustments) is greyed while TheSky's
    mount is not connected / not tracking, so that live state alone says it
    cannot be working (2026-10-05: the record said on, TheSky had it unticked
    and greyed, the unguided test trailed). Off: FAIL when TheSky's mount is
    connected and tracking or a night is armed, else WARN (by day). Nothing
    live: the record is shown as info (verify by eye), or unknown."""
    want = "Activate ProTrack and Enable tracking adjustments ticked (read from TheSky)"
    ts = observed.get("thesky-script") or {}
    act = ts.get("protrack_on")
    act = None if act is None else _bool(act)
    adj = ts.get("protrack_adjustments")
    adj = None if adj is None else _bool(adj)
    conn, trk = _live_state(observed)
    armed = _is_armed(observed)
    man = (observed.get("manual") or {}).get("protrack_on")
    hint = (f"; {_manual_tag(observed)} says {'on' if _bool(man) else 'off'} "
            "(not used for the status)") if man is not None else ""
    if act is False or adj is False:
        what = "Activate ProTrack" if act is False else "Enable tracking adjustments"
        ctx["current"] = "OFF"
        ctx["protrack"] = "off"
        if (conn and trk) or armed:
            return FAIL, want, f"TheSky: {what} is unticked{hint}"
        return WARN, want, (f"TheSky: {what} is unticked (greyed while the mount is "
                            f"{'not connected' if conn is False else 'not tracking'} "
                            f"in TheSky; tick it once it is){hint}")
    if act:
        ctx["protrack"] = "on"
        if adj:
            ctx["current"] = "on"
            return PASS, want, None
        ctx["current"] = "on (tracking adjustments not readable)"
        return UNKNOWN, want, "verify by eye: Enable tracking adjustments ticked"
    # nothing live about ProTrack itself: what TheSky's mount state implies
    if conn is False or (conn and trk is False):
        why = ("TheSky's mount is not connected" if conn is False else
               "TheSky's mount is not tracking")
        ctx["current"] = "OFF (greyed)"
        ctx["protrack"] = "off"
        return (FAIL if armed else WARN), want, (
            f"{why}: ProTrack is greyed and cannot be working (NINA may still drive "
            f"the mount through the ASCOM driver){hint}")
    ctx["protrack"] = "unknown"
    if man is not None:
        ctx["current"] = f"{_manual_tag(observed)}: {'on' if _bool(man) else 'off'}"
        return INFO, want, ("from the manual record, not read from TheSky: verify by eye "
                            "(Bisque TCS > ProTrack)")
    ctx["current"] = "not readable"
    return UNKNOWN, want, "verify by eye: TheSky Bisque TCS > ProTrack (not readable by script)"


def _local_thesky(config) -> bool:
    host = str(getattr(config, "thesky_tcp_host", "localhost") or "localhost").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def _c_one_thesky(row, val, observed, config, ctx):
    """PS-138: exactly one Bisque sky app (TheSky64 or TheSkyX) running. Two
    at once (2026-10-05) split the setup: the audit reads the one on TCP
    3040 while NINA's mount driver may connect through the other, bypassing
    its TPoint model and ProTrack. The note says which one listens on the
    TCP port and which one the driver targets (when the registry tells)."""
    want = "one TheSky (TheSky64), the one the mount driver targets"
    sc = observed.get("thesky_procs") or {}
    if not _local_thesky(config):
        ctx["current"] = "not checked"
        return INFO, want, (f"TheSky runs on {getattr(config, 'thesky_tcp_host', '?')}: "
                            "the process list is read on this PC only")
    if not sc.get("ok"):
        ctx["current"] = "not readable"
        return UNKNOWN, want, ("verify by eye: the taskbar / Task Manager shows one TheSky "
                               "(process list not readable"
                               + (f": {sc['note']}" if sc.get("note") else "") + ")")
    apps = sc.get("apps") or []
    drv = sc.get("driver") or {}
    target = drv.get("target")
    port = sc.get("port") or 3040
    bits = [f"TCP {port}: {sc.get('tcp_owner') or 'nobody listening'}",
            "driver targets " + (f"{target} ({drv.get('via')})" if target else
                                 f"unknown ({drv.get('note') or 'registry not read'})")]
    ctx["current"] = f"{len(apps)}: {sc.get('text')}" if apps else "none running"
    if not apps:
        return INFO, want, "no TheSky running; " + bits[1]
    if len(apps) > 1:
        return row["severity"], want, (
            f"{len(apps)} Bisque sky apps running; " + "; ".join(bits)
            + ". The audit reads the one on the TCP port; NINA's mount may go through "
              "the other (its TPoint model and ProTrack are then bypassed)")
    kind = apps[0].get("kind")
    if target and kind and target != kind and kind != "TheSky":
        return WARN, want, (f"{kind} runs but the driver targets {target}: connecting the "
                            f"mount in NINA starts {target} next to it; " + "; ".join(bits))
    return PASS, want, "; ".join(bits)


def index_terms(observed) -> tuple[float | None, float | None, str]:
    """(IH, ID) in arcsec: TheSky's live read first, then the manual record."""
    ts = observed.get("thesky-script") or {}
    ih, idd = _num(ts.get("tpoint_ih_arcsec")), _num(ts.get("tpoint_id_arcsec"))
    if ih is not None and idd is not None:
        return ih, idd, "TheSky"
    rec = observed.get("manual_record") or {}
    ih, idd = _num(rec.get("ih_arcsec")), _num(rec.get("id_arcsec"))
    if ih is not None and idd is not None:
        return ih, idd, _manual_tag(observed)
    return None, None, ""


def recent_first_slew(observed) -> tuple[dict | None, str]:
    """The newest night with first slews (per_night is newest first), else
    the overall stats."""
    pl = observed.get("pointing-log") or {}
    for st in pl.get("per_night") or []:
        if st and st.get("n") and _num(st.get("median_arcmin")) is not None:
            return st, f"night {st.get('night')}"
    st = pl.get("overall") or {}
    if st.get("n") and _num(st.get("median_arcmin")) is not None:
        return st, f"{pl.get('nights') or 14} nights"
    return None, ""


INDEX_MATCH_RATIO = 2.0      # first slew within x2 of hypot(IH, ID)
INDEX_MATCH_DEG = 25.0       # axis ratio angle within this


def index_shift(observed, config) -> dict | None:
    """PS-138: does the recent first-slew error look like the model's index
    terms (IH / ID), i.e. the mount's index / home moved since the model?
    2026-10-05: first slew 1 deg 14' off with IH -3484.63", ID -2250.28"
    (69' together). Then TPoint Recalibrate (IH / ID only) is the fix, not a
    rebuild. A match: the newest night's median first-slew separation is
    over pointing_first_slew_fail_arcmin and within INDEX_MATCH_RATIO of
    hypot(IH, ID); when its east / north medians are known the direction
    must agree too (the |north| / |east| angle within INDEX_MATCH_DEG of
    |ID| / |IH|; signs are not compared: TPoint's IH / ID sign convention vs
    NINA's east / north is not confirmed). None without both the terms and a
    recent first slew."""
    ih, idd, src = index_terms(observed)
    st, when = recent_first_slew(observed)
    if ih is None or st is None:
        return None
    fail_l = float(getattr(config, "pointing_first_slew_fail_arcmin", 5) or 5)
    mag = math.hypot(ih, idd) / 60.0
    sep = _num(st.get("median_arcmin"))
    e, n = _num(st.get("median_east_arcmin")), _num(st.get("median_north_arcmin"))
    out = {"ih": ih, "id": idd, "src": src, "index_arcmin": round(mag, 1),
           "sep_arcmin": round(sep, 1), "when": when, "n": st.get("n"),
           "large": sep > fail_l, "match": False, "direction": None}
    if not out["large"] or mag <= 0:
        return out
    mag_ok = 1.0 / INDEX_MATCH_RATIO <= sep / mag <= INDEX_MATCH_RATIO
    if e is not None and n is not None and (abs(e) + abs(n)) > 0:
        a_slew = math.degrees(math.atan2(abs(n), abs(e)))
        a_idx = math.degrees(math.atan2(abs(idd), abs(ih)))
        out["direction"] = abs(a_slew - a_idx) <= INDEX_MATCH_DEG
    out["match"] = bool(mag_ok and out["direction"] is not False)
    return out


def _c_index_shift(row, val, observed, config, ctx):
    want = "first slews small, or not matching the model's IH / ID"
    x = index_shift(observed, config)
    if x is None:
        ih, _i, _s = index_terms(observed)
        return UNKNOWN, want, ("no recent NINA first slew" if ih is not None else
                               "IH / ID not known: enter them in the TPoint record "
                               "(TPoint Model tab)")
    ctx["current"] = (f"{x['sep_arcmin']:g}' ({x['when']}, n={x['n']}) vs IH/ID "
                      f"{x['index_arcmin']:g}' ({x['src']})")
    if not x["large"]:
        return PASS, want, None
    if x["match"]:
        ctx["current"] += ": MATCH"
        return row["severity"], want, (
            f"the first slews miss by about the model's index terms (IH {x['ih']:g}\", ID "
            f"{x['id']:g}\"): the index / home moved since the model. TPoint Recalibrate "
            "(IH / ID only), not a rebuild")
    why = ("direction differs" if x["direction"] is False else
           f"{x['sep_arcmin']:g}' vs {x['index_arcmin']:g}'")
    return INFO, want, f"large first slews but not an index shift ({why}): see Rebuild the model?"


COMPUTED = {
    "site_longitude": _c_site_longitude,
    "time_zone": _c_time_zone,
    "mount_connected": _c_mount_connected,
    "mount_tracking": _c_mount_tracking,
    "site_clock": _c_site_clock,
    "camera_selected": _c_camera_selected,
    "autosave_path": _c_autosave_path,
    "filter_in_beam": _c_filter_in_beam,
    "ails_image_scale": _c_ails_image_scale,
    "ails_position_angle": _c_ails_position_angle,
    "imagelink_selftest": _c_imagelink_selftest,
    "allsky": _c_allsky,
    "allsky_db": _c_allsky_db,
    "true_scale": _c_true_scale,
    "camera_pa_stable": _c_camera_pa_stable,
    "parity": _c_parity,
    "tpoint_threshold": _c_tpoint_threshold,
    "tpoint_age": _c_tpoint_age,
    "tpoint_rebuild": _c_tpoint_rebuild,
    "first_slew_error": _c_first_slew_error,
    "first_slew_pattern": _c_first_slew_pattern,
    "mount_vs_solve": _c_mount_vs_solve,
    "protrack": _c_protrack,
    "index_shift": _c_index_shift,
    "one_thesky": _c_one_thesky,
}


# --------------------------------------------------------------------------
# evaluation (pure)
# --------------------------------------------------------------------------

def _unknown_note(row, observed) -> str:
    notes = []
    srcs = observed.get("_sources") or {}
    for s in row.get("sources") or []:
        if s in srcs and not srcs[s].get("ok") and srcs[s].get("note"):
            notes.append(f"{s}: {srcs[s]['note']}")
    return "; ".join(notes) or "no source had a value"


def evaluate_row(row: dict, observed: dict, config) -> dict:
    val, src = _pick(row, observed)
    out = {k: row.get(k) for k in ("id", "group", "label", "severity", "confidence",
                                   "why", "fix")}
    out.update({"current": _g(val), "source": src, "note": None, "apply": "manual"})
    ctx: dict = {}
    if row.get("report"):
        status, desired = (INFO if val is not None else UNKNOWN), "reported only"
    elif row.get("computed"):
        status, desired, out["note"] = COMPUTED[row["computed"]](row, val, observed,
                                                                  config, ctx)
    elif "near" in row:
        want, tol = float(row["near"]), float(row["tol"])
        desired = f"{_g(want)} +/- {_g(tol)}"
        v = _num(val)
        status = UNKNOWN
        if v is not None:
            if row.get("sign_free"):
                ok = abs(abs(v) - abs(want)) <= tol
                if ok and (v < 0) != (want < 0):
                    out["note"] = "stored with the opposite sign convention"
            else:
                ok = abs(v - want) <= tol
            status = PASS if ok else row["severity"]
    else:
        desired = _desired_text(row, None)
        status = UNKNOWN
        if val is not None:
            if "equals" in row:
                ok = _equals(row, val)
                status = UNKNOWN if ok is None else (PASS if ok else row["severity"])
            else:
                v = _num(val)
                lo, hi = row.get("min"), row.get("max")
                if v is not None:
                    ok = (lo is None or v >= lo - 1e-9) and (hi is None or v <= hi + 1e-9)
                    status = PASS if ok else row["severity"]
    if "current" in ctx:
        out["current"] = ctx["current"]
    if row.get("live") and src == "manual" and row.get("computed") != "protrack":
        # PS-138: a live-state row answered only by the manual record: a hint
        # (info), never a pass or a fail; TheSky's own state was not readable
        out["current"] = f"{_manual_tag(observed)}: {out['current']}"
        status = INFO
        out["note"] = ("from the manual record, not read from TheSky: verify by eye"
                       + (f" ({out['note']})" if out["note"] else ""))
    if ctx.get("protrack"):
        out["protrack"] = ctx["protrack"]
    if ctx.get("reasons"):
        out["reasons"] = ctx["reasons"]
    if status == UNKNOWN and not out["note"]:
        out["note"] = _unknown_note(row, observed)
    out["status"] = status
    out["desired"] = desired
    out["applicable"] = False      # report only, always
    return out


def evaluate(desired: dict, observed: dict, config) -> dict:
    rows = [evaluate_row(r, observed, config) for r in desired.get("check") or []]
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in (PASS, WARN, FAIL, UNKNOWN, INFO)}
    reb = next((r for r in rows if r["id"] == "tpoint_rebuild"), None)
    return {"rows": rows, "counts": counts,
            "rebuild": None if reb is None else
            {"status": reb["status"], "current": reb["current"],
             "reasons": reb.get("reasons") or []}}


# --------------------------------------------------------------------------
# collection (never raises)
# --------------------------------------------------------------------------

def _client(config, timeout=5.0):
    from photonscript.telescope_agent.thesky_client import TheSkyClient
    return TheSkyClient(getattr(config, "thesky_tcp_host", "localhost"),
                        int(getattr(config, "thesky_tcp_port", 3040) or 3040),
                        timeout=timeout)


def _f(v):
    return _num(v)


def _jd_now_utc() -> float:
    return time.time() / 86400.0 + 2440587.5


def collect_thesky(config, client=None) -> tuple[dict, dict]:
    """Every TheSky read; one failing read leaves only its keys out."""
    cl = client or _client(config)
    if not cl.ping():
        return ({"thesky_scripting": False},
                {"ok": False, "note": f"TheSky TCP {cl.host}:{cl.port} not reachable"})
    o: dict = {"thesky_scripting": True}
    errs: list[str] = []

    def read(name, fn):
        try:
            return fn() or {}
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {e}")
            return {}
    v = read("version", cl.version)
    if v.get("version") or v.get("build"):
        o["thesky_version"] = " ".join(x for x in (v.get("version"), v.get("build") and
                                                   f"build {v['build']}") if x)
    hw = read("selected_hardware", cl.selected_hardware)
    o["mount_selected"] = hw.get("mount")
    o["camera_selected"] = hw.get("camera")
    o["filter_wheel_selected"] = hw.get("filter_wheel")
    mf = read("mount_flags", cl.mount_flags)
    if mf:
        o["mount_connected"] = _bool(mf.get("connected"))
        o["mount_parked"] = _bool(mf.get("parked")) if mf.get("parked") is not None else None
        o["mount_tracking"] = _bool(mf.get("tracking")) if mf.get("tracking") is not None else None
        o["mount_last_slew_error"] = mf.get("last_slew_error")
    t0 = _jd_now_utc()
    st = read("site", cl.site)
    t1 = _jd_now_utc()
    if st:
        o["site_latitude"] = _f(st.get("latitude"))
        o["site_longitude"] = _f(st.get("longitude"))
        o["site_elevation"] = _f(st.get("elevation_m"))
        uc = st.get("use_computer_clock")
        o["use_computer_clock"] = _bool(uc) if uc is not None else None
        jd = _f(st.get("jd_now"))
        o["site_jd"] = jd
        o["site_lst_h"] = _f(st.get("lst_h"))
        if jd:
            o["site_clock"] = round((jd - (t0 + t1) / 2.0) * 86400.0, 2)
            o["site_clock_latency_s"] = round((t1 - t0) * 86400.0 / 2.0, 3)
        if st.get("time_zone") is not None or st.get("dst_index") is not None:
            o["time_zone_dst"] = f"tz {st.get('time_zone')}, DST index {st.get('dst_index')}"
            o["time_zone"] = _f(st.get("time_zone"))
            o["dst_index"] = _f(st.get("dst_index"))
    ai = read("ails", cl.ails)
    o["ails_image_scale"] = _f(ai.get("image_scale"))
    o["ails_position_angle"] = _f(ai.get("position_angle"))
    o["ails_exposure_s"] = _f(ai.get("exposure_s"))
    o["ails_fovs"] = _f(ai.get("fovs"))
    o["ails_retries"] = _f(ai.get("retries"))
    o["ails_filter"] = ai.get("filter")
    cf = read("camera_flags", cl.camera_flags)
    o["camera_status"] = cf.get("status")
    if cf.get("bin_x") is not None:
        o["camera_bin"] = f"{cf.get('bin_x')}x{cf.get('bin_y')}"
    o["autosave_path"] = cf.get("autosave_path") if cf else None
    if cf.get("autosave_on") not in (None, "", "?ERR"):
        o["autosave_on"] = _bool(cf.get("autosave_on"))
    if cf and cf.get("autosave_path") is None and "autosave_path" in cf:
        o["autosave_path"] = ""
    tp = read("tpoint_flags", cl.tpoint_flags)
    if tp:
        o.update(tpoint_observed(tp))
    if getattr(config, "thesky_audit_allsky_read", False):
        al = read("allsky_flags", cl.allsky_flags)
        for k in ("allsky_automated", "allsky_scripted"):
            if al.get(k) is not None:
                o[k] = _bool(al[k]) if _bool(al[k]) is not None else al[k]
    o = {k: v for k, v in o.items() if v is not None}
    return o, {"ok": True, "note": "; ".join(errs) or f"TheSky {cl.host}:{cl.port} read"}


def tpoint_observed(tp: dict) -> dict:
    """PS-138: the tpoint_flags reply -> observed keys (only what read)."""
    def b(*keys):
        for k in keys:
            if tp.get(k) is not None and _bool(tp[k]) is not None:
                return _bool(tp[k])
        return None
    o = {"tpoint_model_active": b("apply_corrections"),
         "tpoint_points": _num(tp.get("points")),
         "tpoint_rms_arcsec": _num(tp.get("rms_arcsec")),
         "tpoint_ih_arcsec": _num(tp.get("ih_arcsec")),
         "tpoint_id_arcsec": _num(tp.get("id_arcsec")),
         "protrack_on": b("protrack_active", "protrack_active_tele"),
         "protrack_adjustments": b("protrack_adjustments")}
    if o["tpoint_points"] is not None:
        o["tpoint_points"] = int(o["tpoint_points"])
    return {k: v for k, v in o.items() if v is not None}


def protrack_status(audit: dict | None) -> dict:
    """PS-138: {"state": on | off | unknown, "status", "current", "note",
    "fix"} from an audit's ProTrack row, for the Guiding tab and the
    unguided arm warning."""
    r = next((x for x in (audit or {}).get("rows") or [] if x.get("id") == "protrack_on"),
             None)
    if not r:
        return {"state": "unknown", "status": UNKNOWN, "current": None,
                "note": "no TheSky audit", "fix": PROTRACK_FIX}
    st = r.get("protrack") or ("on" if r.get("status") == PASS else "unknown")
    return {"state": st, "status": r.get("status"), "current": r.get("current"),
            "note": r.get("note"), "fix": PROTRACK_FIX}


def load_manual(config) -> dict | None:
    return store.read_json(manual_path(config))


def manual_observed(rec: dict | None, config) -> tuple[dict, dict]:
    if not rec:
        return {}, {"ok": False, "note": "no manual TPoint record: enter it on the Guiding tab"}
    lim = float(getattr(config, "thesky_manual_max_age_days", 30) or 30)
    ent = store.parse_z(rec.get("entered_at"))
    age = (datetime.utcnow() - ent).days if ent else None
    if age is None or age > lim:
        return {}, {"ok": False, "note": f"manual record is {age if age is not None else '?'} d "
                                         f"old: re-enter after the next TPoint session"}
    o = {"tpoint_model_active": rec.get("model_active"),
         "tpoint_points": rec.get("points"),
         "tpoint_rms_arcsec": rec.get("rms_arcsec"),
         "tpoint_model_date": rec.get("model_date"),
         "protrack_on": rec.get("protrack_on"),
         "run_binning": rec.get("run_binning"),
         "allsky_automated": rec.get("allsky_automated"),
         "allsky_db_installed": rec.get("allsky_db_installed"),
         "ucac4_installed": rec.get("ucac4_installed"),
         "gaia_installed": rec.get("gaia_installed")}
    az, alt = _num(rec.get("polar_az_arcmin")), _num(rec.get("polar_alt_arcmin"))
    if az is not None or alt is not None:
        o["tpoint_polar_error_arcmin"] = round(math.hypot(az or 0.0, alt or 0.0), 2)
    if rec.get("model_date"):
        o["tpoint_rebuild"] = rec.get("model_date")
    o = {k: v for k, v in o.items() if v is not None}
    return o, {"ok": True, "note": f"entered {rec.get('entered_at')}"}


def astap_observed(config) -> tuple[dict, list[dict], dict, dict]:
    """(astap keys, history, latest check, source note)."""
    latest = store.read_json(audit_dir(config) / "imagelink_latest.json") or {}
    hist = [{"t_utc": h.get("t_utc"), "pa": (h.get("astap") or {}).get("pa"),
             "parity": (h.get("astap") or {}).get("parity"),
             "native_scale": (h.get("astap") or {}).get("native_scale")}
            for h in store.read_jsonl(audit_dir(config) / "imagelink_checks.jsonl")
            if (h.get("astap") or {}).get("scale")]
    a = latest.get("astap") or {}
    if not a.get("scale"):
        return {}, hist, latest, {"ok": False, "note": latest.get("note") or
                                  "no ASTAP Image Link check yet (Guiding tab or --imagelink)"}
    o = {"true_scale": a.get("native_scale"), "astap_pa": a.get("pa"),
         "parity": a.get("parity"), "solve_stars": a.get("stars"),
         "imagelink_selftest": "run" if (latest.get("thesky") or {}).get("succeeded")
         is not None else None}
    o = {k: v for k, v in o.items() if v is not None}
    return o, hist, latest, {"ok": True, "note": f"check {latest.get('t_utc')} on {latest.get('file')}"}


def model_cutoff_utc(rec: dict | None, config) -> datetime | None:
    """PS-120: first slews older than the TPoint model say nothing about it.
    The cutoff is the earlier of the record's entered_at and the end of the
    model night (local noon after model_date), so no slew from before the
    model is counted; a few slews between the build and the entry may be
    left out (conservative). None without a model date."""
    from photonscript.shared.localtime import utc_offset_hours
    rec = rec or {}
    md = _parse_date(rec.get("model_date"))
    if not md:
        return None
    noon = datetime(md.year, md.month, md.day, 12) + timedelta(days=1)
    try:
        off = utc_offset_hours(config, noon + timedelta(hours=7))
    except Exception:  # noqa: BLE001
        off = float(getattr(config, "utc_offset_hours", -7.0))
    end = noon - timedelta(hours=off)
    ent = store.parse_z(rec.get("entered_at"))
    if ent is None or ent < datetime(md.year, md.month, md.day):
        return end                       # no (or an implausible) entry time
    return min(end, ent)


def pointing_observed(config, rec: dict | None = None) -> tuple[dict, dict]:
    from photonscript.scheduler import nina_center_log as ncl
    since = model_cutoff_utc(rec, config)
    s = ncl.summary(config, 14, since_utc=since)
    o = dict(s)
    # the newest night that has a filter change in its NINA log
    for n in ncl._nights(s["end_night"], 3):
        meta = ncl.load_meta(config, n)
        if meta and meta.get("last_filter"):
            o["last_filter"] = meta["last_filter"]
            o["last_filter_src"] = f"NINA log {n}"
            break
    if "last_filter" not in o:
        lf = _last_sub_filter(config, s["end_night"])
        if lf:
            o["last_filter"], o["last_filter_src"] = lf
    ok = bool(s.get("runs"))
    note = (f"{s['runs']} NINA Center runs in {s['nights']} nights" if ok else
            ("no NINA Center runs found" if s.get("logs_dir_found") else
             f"NINA logs folder {getattr(config, 'nina_logs_dir', '')} not found"))
    if since is not None:
        o["model_date"] = (rec or {}).get("model_date")
        cut = f"since the TPoint model ({o['model_date']}, counted from {store.iso_z(since)})"
        skipped = (f"; {s.get('before_since')} older run(s) ignored"
                   if s.get("before_since") else "")
        note = (f"{s['runs']} NINA Center runs {cut}{skipped}" if ok else
                f"no slews since the model (n=0): first slews are counted {cut}{skipped}")
    o["note"] = note
    return o, {"ok": ok, "note": note}


def _last_sub_filter(config, end_night: str) -> tuple[str, str] | None:
    """Fallback for the filter in the beam: the last RC16 light's filter
    (dawn flats may have moved it since)."""
    try:
        from photonscript.scheduler.runs import _load_subs
        from photonscript.scheduler import nina_center_log as ncl
        for n in ncl._nights(end_night, 3):
            subs = [s for s in _load_subs(config, n)
                    if (s.get("rig") or "rc16") == "rc16" and s.get("filter")]
            if subs:
                subs.sort(key=lambda s: str(s.get("time") or ""))
                return (subs[-1]["filter"],
                        f"last RC16 light {n} (dawn flats may have moved it since)")
    except Exception as e:  # noqa: BLE001
        logger.debug("last sub filter: %s", e)
    return None


def collect(config, *, client=None, armer_state: str | None = None) -> dict:
    obs: dict = {s: {} for s in SOURCES}
    src: dict = {}
    try:
        obs["thesky-script"], src["thesky-script"] = collect_thesky(config, client)
    except Exception as e:  # noqa: BLE001
        src["thesky-script"] = {"ok": False, "note": f"{type(e).__name__}: {e}"}
    try:
        obs["astap"], obs["astap_history"], obs["imagelink"], src["astap"] = astap_observed(config)
    except Exception as e:  # noqa: BLE001
        src["astap"] = {"ok": False, "note": str(e)}
    try:
        from photonscript.scheduler import thesky_procs
        sc = thesky_procs.scan(config) if _local_thesky(config) else {"ok": False}
        obs["thesky_procs"] = sc
        if sc.get("ok"):
            obs["processes"] = {"one_thesky": sc.get("count")}
            src["processes"] = {"ok": True, "note": f"{sc.get('count')} TheSky running: "
                                                    f"{sc.get('text')}"}
        else:
            src["processes"] = {"ok": False, "note": sc.get("note") or (
                "process list not readable" if _local_thesky(config) else
                "TheSky is not on this PC")}
    except Exception as e:  # noqa: BLE001
        src["processes"] = {"ok": False, "note": f"{type(e).__name__}: {e}"}
    try:   # PS-171: TPoint's own files, only where TheSky runs
        if _local_thesky(config):
            from photonscript.scheduler import tpoint_files
            obs["tpoint-files"], src["tpoint-files"] = tpoint_files.audit_observed(config)
        else:
            src["tpoint-files"] = {"ok": False, "note": "TheSky is not on this PC"}
    except Exception as e:  # noqa: BLE001
        src["tpoint-files"] = {"ok": False, "note": f"{type(e).__name__}: {e}"}
    rec = None
    try:
        rec = load_manual(config)
        obs["manual_record"] = rec or {}
        obs["manual"], src["manual"] = manual_observed(rec, config)
    except Exception as e:  # noqa: BLE001
        src["manual"] = {"ok": False, "note": str(e)}
    try:
        obs["pointing-log"], src["pointing-log"] = pointing_observed(config, rec)
    except Exception as e:  # noqa: BLE001
        src["pointing-log"] = {"ok": False, "note": str(e)}
    try:
        from photonscript.shared.rigs import rig_ids
        obs["rigs"] = rig_ids(config)
    except Exception:  # noqa: BLE001
        obs["rigs"] = None
    obs["armer_state"] = armer_state
    obs["_sources"] = src
    return obs


# --------------------------------------------------------------------------
# run, store, summarize
# --------------------------------------------------------------------------

def night_path(config, night: str) -> Path:
    return audit_dir(config) / f"{night}.json"


def save(config, audit: dict) -> None:
    try:
        store.write_json(night_path(config, audit["night"]), audit)
        store.write_json(audit_dir(config) / "latest.json", audit)
    except OSError as e:
        logger.warning("TheSky audit not saved: %s", e)


def load_latest(config) -> dict | None:
    return store.read_json(audit_dir(config) / "latest.json")


def load_night(config, night: str) -> dict | None:
    return store.read_json(night_path(config, night))


def run_audit(config, reason: str = "manual", *, client=None,
              armer_state: str | None = None, persist: bool = True) -> dict:
    """Collect, evaluate and (by default) save one audit. Never raises.
    Synchronous (socket reads, log parsing): call it from a thread."""
    now = datetime.utcnow()
    desired = load_desired(config)
    observed = collect(config, client=client, armer_state=armer_state)
    res = evaluate(desired, observed, config)
    res["rows"].sort(key=lambda r: STATUS_RANK.get(r["status"], 9))
    pl = observed.get("pointing-log") or {}
    audit = {"t_utc": store.iso_z(now), "night": store.night_of(config, now),
             "reason": reason, "title": desired.get("title"),
             "desired_file": desired.get("path"), "desired_error": desired.get("error"),
             "desired_lint": desired.get("lint"), "armer_state": armer_state,
             "sources": observed.get("_sources"), **res,
             "first_slew": first_slew_text(pl),
             "pointing": {k: pl.get(k) for k in ("nights", "end_night", "overall", "by_side",
                                                 "by_dec", "by_ha", "per_night", "ps67",
                                                 "side_src", "runs", "logs_dir_found")},
             "imagelink": observed.get("imagelink") or None,
             "manual": observed.get("manual_record") or None,
             "thesky_procs": observed.get("thesky_procs") or None,
             "fail_ids": [r["id"] for r in res["rows"] if r["status"] == FAIL]}
    if persist:
        save(config, audit)
    return audit


def summary_line(audit: dict | None) -> str | None:
    if not audit:
        return None
    c = audit.get("counts") or {}
    reb = (audit.get("rebuild") or {}).get("current") or "unknown"
    reb = {"REBUILD": "yes", "watch": "watch", "no": "no"}.get(reb, reb)
    fs = audit.get("first_slew")
    return (f"TheSky audit: {c.get('fail', 0)} fail, {c.get('warn', 0)} warn, "
            f"rebuild: {reb}" + (f", first slew median {fs}" if fs else ""))


def summary(config, night: str) -> dict | None:
    """The block for a night's report (runs page): the line only."""
    a = load_night(config, night)
    if not a:
        return None
    return {"t_utc": a.get("t_utc"), "reason": a.get("reason"), "counts": a.get("counts"),
            "rebuild": a.get("rebuild"), "first_slew": a.get("first_slew"),
            "line": summary_line(a)}


async def at_arm(config, armer_state: str | None = None) -> dict | None:
    """The armer's hook: one read-only audit in the background. No push."""
    if not getattr(config, "thesky_audit_enabled", True):
        return None
    import asyncio
    return await asyncio.to_thread(run_audit, config, "arm", armer_state=armer_state)


# --------------------------------------------------------------------------
# manual record
# --------------------------------------------------------------------------

def validate_manual(body: dict) -> tuple[dict, list[str]]:
    """Clean a POSTed manual record. (record, problems)."""
    rec, bad = {}, []
    if not isinstance(body, dict):
        return {}, ["body must be a JSON object"]
    for k in body:
        if k not in MANUAL_FIELDS:
            bad.append(f"{k}: unknown field")
    for k, (typ, lo, hi) in MANUAL_FIELDS.items():
        v = body.get(k)
        if v is None or v == "":
            continue
        if typ == "date":
            d = _parse_date(v)
            if d is None or len(str(v).strip()) != 10:
                bad.append(f"{k}: use YYYY-MM-DD")
            else:
                rec[k] = d.isoformat()
        elif typ == "bool":
            b = _bool(v)
            if b is None:
                bad.append(f"{k}: true or false")
            else:
                rec[k] = b
        elif typ == "str":
            s = str(v).strip()
            if len(s) > hi:
                bad.append(f"{k}: at most {hi} characters")
            elif not s.isascii():
                bad.append(f"{k}: plain ASCII only")
            else:
                rec[k] = s
        else:
            x = _num(v)
            if x is None or (typ == "int" and x != int(x)):
                bad.append(f"{k}: a {'whole ' if typ == 'int' else ''}number")
            elif (lo is not None and x < lo) or (hi is not None and x > hi):
                bad.append(f"{k}: {lo} to {hi}")
            else:
                rec[k] = int(x) if typ == "int" else float(x)
    return rec, bad


def save_manual(config, body: dict) -> dict:
    rec, bad = validate_manual(body)
    if bad:
        return {"ok": False, "problems": bad}
    try:
        from photonscript.shared.rigs import rig_ids
        rec["rigs"] = rig_ids(config)
    except Exception:  # noqa: BLE001
        pass
    rec["entered_at"] = store.iso_z(datetime.utcnow())
    try:
        store.write_json(manual_path(config), rec)
        store.append_jsonl(manual_path(config).with_name("manual_history.jsonl"), rec)
    except OSError as e:
        return {"ok": False, "problems": [f"not saved: {e}"]}
    return {"ok": True, "record": rec}


# --------------------------------------------------------------------------
# Image Link check
# --------------------------------------------------------------------------

def pick_frame(config, nights: int = 14, end_night: str | None = None) -> tuple[str, dict] | None:
    """(night, sub record) of the newest RC16 L light whose file is on this
    machine (accepted subs first), else the newest broadband one."""
    from photonscript.scheduler import nina_center_log as ncl
    from photonscript.scheduler.runs import _load_subs
    end_night = end_night or store.night_of(config, datetime.utcnow())
    try:
        rev = config.reverse_filter_map()
    except Exception:  # noqa: BLE001
        rev = {}
    by_night = []
    for n in ncl._nights(end_night, nights):
        subs = [s for s in _load_subs(config, n)
                if (s.get("rig") or "rc16") == "rc16" and s.get("abs_path")
                and str(s.get("imagetyp") or "LIGHT").upper() == "LIGHT"]
        if subs:
            by_night.append((n, subs))

    def best(filters):
        for n, subs in by_night:
            hits = [s for s in subs if rev.get(str(s.get("filter")), s.get("filter")) in filters
                    and Path(s["abs_path"]).exists()]
            if hits:
                hits.sort(key=lambda s: (bool(s.get("passed_qa")), str(s.get("time") or "")))
                return n, hits[-1]
        return None
    return best(("L",)) or best(("R", "G", "B"))


def _frame_bin(path) -> int:
    try:
        from astropy.io import fits as _fits
        h = _fits.getheader(str(path))
        return int(h.get("XBINNING") or 1)
    except Exception:  # noqa: BLE001
        return 1


def armer_idle(armer_state) -> bool:
    """Strict: only a known idle state (an unknown state is not idle)."""
    return str(armer_state or "").upper() in ("DISARMED", "COMPLETE")


def _thesky_imagelink(config, src: Path, scale: float, client=None) -> dict:
    """Copy the frame into <data_dir>/thesky_audit/tmp/, run TheSky's Image
    Link on the copy, delete the copy. Never raises."""
    tmp = audit_dir(config) / "tmp"
    cp = tmp / f"imagelink_{int(time.time())}{src.suffix or '.fits'}"
    out: dict = {"file": src.name}
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, cp)
        cl = client or _client(config, timeout=120.0)
        r = cl.imagelink_file(str(cp), scale)
        out.update({"succeeded": _bool(r.get("succeeded")),
                    "error_code": r.get("error_code"), "error_text": r.get("error_text"),
                    "exec_error": r.get("exec_error"),
                    "image_scale": _num(r.get("image_scale")),
                    "position_angle": _num(r.get("position_angle")),
                    "mirrored": _bool(r.get("mirrored")),
                    "image_stars": _num(r.get("image_stars")),
                    "solution_rms": _num(r.get("solution_rms")),
                    "solution_stars": _num(r.get("solution_stars")),
                    "catalog_stars": _num(r.get("catalog_stars"))})
        if out["succeeded"] is None:
            out["succeeded"] = False
    except Exception as e:  # noqa: BLE001
        out.update({"succeeded": False, "note": f"{type(e).__name__}: {e}"})
    finally:
        try:
            cp.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Image Link copy %s not deleted: %s", cp, e)
    return out


def imagelink_check(config, *, runner=None, thesky: bool = False, client=None,
                    armer_state: str | None = None, persist: bool = True,
                    frame: tuple[str, dict] | None = None) -> dict:
    """ASTAP on the newest RC16 L frame (stored through solve_store as
    usual) and, with thesky=True and the armer idle, TheSky's own Image Link
    on a temporary copy. Never touches the camera or the mount. Never
    raises."""
    from photonscript.scheduler.solve_store import solve
    now = datetime.utcnow()
    rec: dict = {"t_utc": store.iso_z(now), "ok": False}
    try:
        pick = frame or pick_frame(config)
    except Exception as e:  # noqa: BLE001
        pick = None
        rec["note"] = f"no frame: {e}"
    if not pick:
        rec.setdefault("note", "no RC16 L (or broadband) light on this machine "
                               "in the last 14 nights")
        return _store_check(config, rec, persist)
    night, sub = pick
    path = Path(sub["abs_path"])
    rec.update({"night": night, "file": sub.get("file") or path.name,
                "filter": sub.get("filter"), "abs_path": str(path)})
    latest = store.read_json(audit_dir(config) / "imagelink_latest.json") or {}
    if (latest.get("abs_path") == str(path) and (latest.get("astap") or {}).get("scale")
            and runner is None):
        rec["astap"] = latest["astap"]          # same frame: reuse the solve
        rec["astap_reused"] = True
    else:
        sol = solve(config, path, rig="rc16", night=night, runner=runner,
                    rel_file=rec["file"])
        if not sol:
            rec["note"] = "ASTAP did not solve the frame (or ASTAP is not on this machine)"
            rec["astap"] = None
        else:
            b = _frame_bin(path)
            rec["astap"] = {"scale": sol["scale"], "frame_bin": b,
                            "native_scale": round(sol["scale"] / b, 5) if sol["scale"] else None,
                            "pa": sol["pa"], "parity": sol["parity"],
                            "ra": sol["ra"], "dec": sol["dec"],
                            "stars": sol.get("stars")}
    if rec.get("astap"):
        rec["ok"] = True
    if thesky:
        if not armer_idle(armer_state):
            rec["thesky"] = {"skipped": True,
                             "note": f"armer is {armer_state or 'unknown'}: TheSky Image Link "
                                     "runs only while it is idle (DISARMED / COMPLETE)"}
        elif not (rec.get("astap") or {}).get("scale"):
            rec["thesky"] = {"skipped": True, "note": "needs the ASTAP scale first"}
        else:
            rec["thesky"] = _thesky_imagelink(config, path, rec["astap"]["scale"], client)
    elif latest.get("thesky") and latest.get("abs_path") == str(path):
        rec["thesky"] = latest["thesky"]      # keep the last self-test on this frame
    return _store_check(config, rec, persist)


def _store_check(config, rec: dict, persist: bool) -> dict:
    if persist:
        try:
            store.write_json(audit_dir(config) / "imagelink_latest.json", rec)
            store.append_jsonl(audit_dir(config) / "imagelink_checks.jsonl", rec)
        except OSError as e:
            logger.warning("Image Link check not saved: %s", e)
    return rec


def compare_settings(check: dict, observed_ails: dict | None, run_bin: int = 1) -> dict:
    """The Image Link card: ASTAP truth next to TheSky's settings."""
    a = (check or {}).get("astap") or {}
    ns = a.get("native_scale")
    want = round(ns * run_bin, 4) if ns else None
    have = _num((observed_ails or {}).get("ails_image_scale"))
    return {"native_scale": ns, "run_binning": run_bin, "expected_ails_scale": want,
            "ails_image_scale": have,
            "diff_pct": round(100 * (have - want) / want, 2) if want and have else None,
            "astap_pa": a.get("pa"), "ails_position_angle":
            _num((observed_ails or {}).get("ails_position_angle")),
            "parity": a.get("parity")}


def format_report(audit: dict) -> str:
    """Plain text for the CLI."""
    lines = [f"{audit.get('title')}  ({audit.get('reason')} {audit.get('t_utc')})",
             summary_line(audit) or ""]
    reb = audit.get("rebuild") or {}
    for r in reb.get("reasons") or []:
        lines.append(f"  rebuild: {r}")
    group = None
    for r in sorted(audit.get("rows") or [], key=lambda r: (r.get("group") or "")):
        if r.get("group") != group:
            group = r.get("group")
            lines.append(f"\n[{group}]")
        lines.append(f"  {r['status'].upper():7} {r['label']}: {r['current']}"
                     f" (want {r['desired']})" + (f"  - {r['note']}" if r.get("note") else ""))
    il = audit.get("imagelink")
    if il:
        lines.append(f"\nImage Link check {il.get('t_utc')}: {il.get('file')} "
                     f"{il.get('note') or ''}")
    return "\n".join(lines)

