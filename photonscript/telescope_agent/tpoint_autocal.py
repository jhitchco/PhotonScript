"""TPoint autocal: grow the TPoint model with TheSky's own camera (design +
offline building blocks only, docs/TPOINT_AUTOCAL.md).

Why: the scripted TPoint add does not add points (PS-187: TheSky's Add
Pointing Sample acts on TheSky's own camera Image Link, not on a scripted
Image Link of NINA's FITS). The 356-point model of 2026-10-10 came from
TheSky's Automated Pointing Calibration Run with TheSky's camera connected,
run by hand. This module plans an OPERATOR-STARTED, SUPERVISED version of
that: Jeremy starts it, it never arms anything, and it refuses to run while
a night is armed or any NINA sequence runs.

What is here (all pure, nothing is sent anywhere):

* preflight(): the go / no-go over the scheduler's GET /api/arm and GET
  /api/apps/status payloads, the roof (safety monitor) reading, the TPoint
  backup plan and two config switches. Unknown counts as no-go.
* backup_plan(): which TPoint files to copy to C:\\astro before anything
  touches TheSky (from tpoint_files.list_files, no I/O here).
* nina_handoff(): the ninaAPI requests that lend NINA #1's camera to TheSky
  and take it back (descriptions only).
* the TheSky script builders (ES5 strings for the TCP 3040 server): read
  the camera, connect / disconnect TheSky's camera, set exposure / binning
  and the Automated Image Link settings, slew to an alt / az, expose, the
  AutoMap add (Image Link of TheSky's last camera image + a TPoint sample),
  poll, abort, open the Automated Pointing Calibration window.
* build_plan() / format_plan(): the whole procedure as numbered steps, for
  ``photonscript tpoint-autocal --dry-run``.
* run_go(): NOT IMPLEMENTED (raises). No code path in this module opens a
  socket or an HTTP connection; a test greps for that.

Confirmed vs candidate (see the doc's "What is confirmed" table): the
ccdsoftCamera / sky6RASCOMTele / ImageLink / AutomatedImageLinkSettings
members used below are in Bisque's published scripting reference and in
several public TheSkyX clients; sky6RASCOMTheSky.AutoMap() is a published
method name whose exact effect on build 14139 is NOT confirmed (believed:
Image Link the camera's last image, then add a TPoint sample);
TheSkyXAction.execute(<action>) runs a toolbar command (opens a window), so
the Automated Pointing Calibration Run itself has no scripted start / point
count / progress: CANDIDATE only.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PureWindowsPath

# --------------------------------------------------------------- limits

MIN_POINTS = 6           # TPoint needs at least 6 samples for a bare model
MAX_POINTS = 500         # 2026-10-10 planned ~414
DEFAULT_POINTS = 30      # a first supervised run, not a full model
MODES = ("loop", "window")
DEFAULT_MODE = "loop"
SUN_MAX_DEG = -12.0      # at least nautical dark for Image Link
ARMER_OK_STATES = ("DISARMED",)
NINA1_LABEL = "NINA #1 (RC16)"
NINA2_LABEL = "NINA #2 (Piggy-600)"
DEFAULT_BACKUP_ROOT = r"C:\astro\tpoint_backup"
# per point (loop mode): slew + settle + download + Image Link, plus the
# exposure; 2026-10-10 by hand: ~414 points over several hours
OVERHEAD_S = 35.0
PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"

GO_NOT_IMPLEMENTED = (
    "tpoint-autocal --go is not implemented yet: this build only plans "
    "(--dry-run). Run TheSky's Automated Pointing Calibration by hand as on "
    "2026-10-10, or see docs/TPOINT_AUTOCAL.md for the supervised first "
    "test that must pass before --go is written.")


# --------------------------------------------------------------- preflight

def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def preflight(arm: dict | None, apps: dict | None, roof_safe: bool | None,
              backup: dict | None, auto_arm_enabled: bool = False,
              auto_dusk_flats: bool = False) -> dict:
    """Go / no-go for a TPoint autocal run. Pure.

    arm: GET /api/arm (None = unreadable). apps: GET /api/apps/status
    (None = unreadable). roof_safe: NINA #1's safety monitor IsSafe (True =
    roof open), None = unreadable. backup: backup_plan() output, None = not
    planned. Every check is pass / warn / fail / unknown; go only when
    nothing is fail or unknown (warn is shown, not blocking).
    {"go", "checks": [{name, status, detail}], "blockers": [str]}."""
    checks: list[dict] = []

    # 1. the armer: no armed / running / watched / paused night
    state = str((arm or {}).get("state") or "") if arm is not None else ""
    if arm is None or not state:
        checks.append(_check("armer idle", UNKNOWN,
                             "GET /api/arm unreadable: cannot prove no night is armed"))
    elif state in ARMER_OK_STATES:
        checks.append(_check("armer idle", PASS, f"armer {state}"))
    else:
        checks.append(_check("armer idle", FAIL,
                             f"armer {state}: disarm first (never during an "
                             "armed or imaging night)"))

    # 2. automation that could start a night or a NINA job mid-run
    checks.append(_check(
        "auto-arm off", FAIL if auto_arm_enabled else PASS,
        "PS_AUTO_ARM_ENABLED=true: the evening auto-arm could arm (and load "
        "NINA #1) mid-run; turn it off for the night" if auto_arm_enabled
        else "auto-arm disabled"))
    checks.append(_check(
        "dusk flats", WARN if auto_dusk_flats else PASS,
        "PS_AUTO_DUSK_FLATS=true: start after the dusk flats window (they "
        "load a NINA #1 sequence and would want the camera back)"
        if auto_dusk_flats else "automatic dusk flats off"))

    # 3. NINA #1 and #2 idle (the mount is shared; NINA #1 owns the camera)
    running = (apps or {}).get("sequence_running") if apps is not None else None
    for label in (NINA1_LABEL, NINA2_LABEL):
        items = None if running is None else running.get(label, None)
        if apps is None or running is None or (label not in running):
            checks.append(_check(f"{label} idle", UNKNOWN,
                                 "sequence state not read"))
        elif items is None:
            checks.append(_check(f"{label} idle", UNKNOWN,
                                 "sequence state unreadable"))
        elif items:
            checks.append(_check(f"{label} idle", FAIL,
                                 "running: " + ", ".join(map(str, items[:3]))))
        else:
            checks.append(_check(f"{label} idle", PASS, "no RUNNING item"))

    # 4. calibration capture jobs (they connect the camera themselves)
    blockers = [str(b) for b in ((apps or {}).get("stop_blockers") or [])]
    cap = [b for b in blockers if "calibration capture" in b]
    if apps is None:
        checks.append(_check("no capture job", UNKNOWN, "apps status not read"))
    else:
        checks.append(_check("no capture job", FAIL if cap else PASS,
                             "; ".join(cap) or "none running"))

    # 5. TheSky answering with the mount connected
    sky = (apps or {}).get("thesky") or {}
    if apps is None or sky.get("answering") is None:
        checks.append(_check("TheSky answering", UNKNOWN, "not read"))
    elif not sky.get("answering"):
        checks.append(_check("TheSky answering", FAIL,
                             "TheSky TCP 3040 not answering"))
    elif sky.get("mount_connected") is not True:
        checks.append(_check("TheSky answering", FAIL if sky.get(
            "mount_connected") is False else UNKNOWN,
            "TheSky answers but its mount connection reads "
            f"{sky.get('mount_connected')}"))
    else:
        checks.append(_check("TheSky answering", PASS, "mount connected in TheSky"))

    # 6. dark enough
    sun = (apps or {}).get("sun_alt_deg")
    if sun is None:
        checks.append(_check("dark", UNKNOWN, "sun altitude not read"))
    else:
        try:
            s = float(sun)
            checks.append(_check("dark", PASS if s <= SUN_MAX_DEG else FAIL,
                                 f"sun {s:.1f} deg (need <= {SUN_MAX_DEG:.0f})"))
        except (TypeError, ValueError):
            checks.append(_check("dark", UNKNOWN, f"sun altitude {sun!r}"))

    # 7. roof open (safety monitor safe)
    if roof_safe is None:
        checks.append(_check("roof open", UNKNOWN, "safety monitor not read"))
    else:
        checks.append(_check("roof open", PASS if roof_safe else FAIL,
                             "safety monitor SAFE (roof open)" if roof_safe
                             else "safety monitor UNSAFE (roof closed)"))

    # 8. a TPoint backup is planned (copied in --go before anything else)
    if backup is None:
        checks.append(_check("TPoint backup", UNKNOWN, "no backup planned"))
    elif not backup.get("copies"):
        checks.append(_check("TPoint backup", FAIL,
                             "no TPoint files found to back up "
                             "(thesky_user_dir?)"))
    else:
        checks.append(_check("TPoint backup", PASS,
                             f"{len(backup['copies'])} file(s) to "
                             f"{backup.get('dest')}"))

    bad = [f"{c['name']}: {c['detail']}" for c in checks
           if c["status"] in (FAIL, UNKNOWN)]
    return {"go": not bad, "checks": checks, "blockers": bad}


# --------------------------------------------------------------- backup

_BACKUP_RE = re.compile(r"tpoint|\.dat$|\.tpt$|\.tpd$", re.IGNORECASE)


def backup_plan(listing: dict | None, now: datetime,
                dest_root: str = DEFAULT_BACKUP_ROOT) -> dict:
    """The copies to make before the run: every listed TPoint file (from
    tpoint_files.list_files) into <dest_root>\\<UTC stamp>\\<rel>. Pure,
    nothing is copied here. {"dest", "copies": [{"src", "dst", "size"}]}."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    dest = PureWindowsPath(dest_root) / stamp
    copies = []
    for f in (listing or {}).get("files") or []:
        rel = str(f.get("rel") or "")
        if not rel or not _BACKUP_RE.search(rel):
            continue
        copies.append({"src": str(f.get("path") or ""),
                       "dst": str(dest / PureWindowsPath(rel)),
                       "size": f.get("size")})
    return {"dest": str(dest), "copies": copies}


# --------------------------------------------------------------- NINA handoff

def nina_handoff(base_url: str) -> dict:
    """The ninaAPI requests that lend NINA #1's camera to TheSky ("lend")
    and take it back ("return"). Descriptions only: nothing here sends.
    Each: {"method", "url", "why"}."""
    b = str(base_url or "").rstrip("/")
    return {
        "lend": [
            {"method": "GET", "url": f"{b}/sequence/state",
             "why": "re-check: no RUNNING item right before the lend"},
            {"method": "GET", "url": f"{b}/equipment/camera/info",
             "why": "record Connected / cooler / setpoint to restore later"},
            {"method": "GET", "url": f"{b}/equipment/camera/disconnect",
             "why": "free the AP26MC (one app can hold it)"},
            {"method": "GET", "url": f"{b}/equipment/camera/info",
             "why": "confirm Connected = false before TheSky connects"},
        ],
        "return": [
            {"method": "GET", "url": f"{b}/equipment/camera/connect",
             "why": "after TheSky disconnected its camera"},
            {"method": "GET", "url": f"{b}/equipment/camera/info",
             "why": "confirm Connected = true; re-apply the cooler if it "
                    "was on before"},
        ],
    }


# --------------------------------------------------------------- TheSky scripts
#
# ES5 for TheSky's TCP server (result in Out, "k=v;k=v" like the client's
# reads). Inputs are numbers validated here; the one string (a slew label)
# is restricted to a safe alphabet. These are WRITE scripts on purpose and
# never join thesky_client.READ_ONLY_JS (the PS-104 denylist covers that
# dict); a test checks the two stay apart.

_HELPERS = ("var Out;"
            "function s(v){return String(v).replace(/[|;=\\r\\n]/g,'_');}"
            "function g(f){try{return s(f());}catch(e){return '?ERR';}}")
_LABEL_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,40}$")


def _num(v, lo: float, hi: float, what: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, got {v!r}") from None
    if x != x or not lo <= x <= hi:
        raise ValueError(f"{what} {x} outside {lo}..{hi}")
    return x


def _kv(pairs: list[tuple[str, str]], pre: str = "") -> str:
    body = " + ';' + ".join(f"'{k}=' + g(function(){{return {e};}})"
                            for k, e in pairs)
    return _HELPERS + pre + "Out = " + body + ";"


def camera_read_script() -> str:
    """READ ONLY: which camera TheSky has selected and its state. Safe any
    time (no Connect)."""
    return _kv([("camera_model", "SelectedHardware.cameraModel"),
                ("status", "ccdsoftCamera.Status"),
                ("bin_x", "ccdsoftCamera.BinX"),
                ("exposure_s", "ccdsoftCamera.ExposureTime"),
                ("autosave_path", "ccdsoftCamera.AutoSavePath"),
                ("last_image", "ccdsoftCamera.LastImageFileName")])


def camera_connect_script() -> str:
    """Connect TheSky's camera (the AP26MC through TheSky's camera add-on,
    selected by hand in TheSky's Camera Setup). Only after NINA #1 let go."""
    return _kv([("err", "err"), ("status", "ccdsoftCamera.Status")],
               pre="var err = '';"
                   "try { ccdsoftCamera.Connect(); } catch (e) { err = String(e.message || e); }")


def camera_disconnect_script() -> str:
    """Disconnect TheSky's camera so NINA #1 can take it back."""
    return _kv([("err", "err"), ("status", "ccdsoftCamera.Status")],
               pre="var err = '';"
                   "try { ccdsoftCamera.Disconnect(); } catch (e) { err = String(e.message || e); }")


def camera_setup_script(exposure_s: float, binning: int) -> str:
    """Exposure, binning, light frame, no reduction, autosave on,
    asynchronous (the TCP call returns at once, poll with
    expose_poll_script)."""
    t = _num(exposure_s, 0.5, 60.0, "exposure_s")
    b = int(_num(binning, 1, 4, "binning"))
    return _kv([("exposure_s", "ccdsoftCamera.ExposureTime"),
                ("bin_x", "ccdsoftCamera.BinX"),
                ("async", "ccdsoftCamera.Asynchronous")],
               pre=(f"ccdsoftCamera.ExposureTime = {t:g};"
                    f"ccdsoftCamera.BinX = {b};"
                    f"ccdsoftCamera.BinY = {b};"
                    "ccdsoftCamera.Frame = 1;"
                    "ccdsoftCamera.ImageReduction = 0;"
                    "ccdsoftCamera.AutoSaveOn = 1;"
                    "ccdsoftCamera.Asynchronous = 1;"))


def ails_setup_script(image_scale: float, exposure_s: float) -> str:
    """The Automated Image Link settings TheSky's own runs (and AutoMap)
    use: image scale ("/px at the binning) and exposure."""
    sc = _num(image_scale, 0.05, 20.0, "image_scale")
    t = _num(exposure_s, 0.5, 60.0, "exposure_s")
    return _kv([("image_scale", "AutomatedImageLinkSettings.imageScale"),
                ("exposure_s", "AutomatedImageLinkSettings.exposureTimeAILS")],
               pre=(f"AutomatedImageLinkSettings.imageScale = {sc:.5f};"
                    f"AutomatedImageLinkSettings.exposureTimeAILS = {t:g};"))


def slew_altaz_script(alt: float, az: float, label: str,
                      min_alt: float = 20.0) -> str:
    """Asynchronous open-loop slew to an alt / az (NO closed loop slew, NO
    sync: either would change what TPoint measures). Poll with
    slew_poll_script."""
    a = _num(alt, max(15.0, float(min_alt)), 85.0, "alt")
    z = _num(az, 0.0, 360.0, "az") % 360.0
    if not _LABEL_RE.match(str(label)):
        raise ValueError(f"unsafe slew label {label!r}")
    return _kv([("err", "err"), ("last_slew_error", "sky6RASCOMTele.LastSlewError")],
               pre=("var err = '';"
                    "sky6RASCOMTele.Asynchronous = 1;"
                    f"try {{ sky6RASCOMTele.SlewToAzAlt({z:.4f}, {a:.4f}, '{label}'); }}"
                    " catch (e) { err = String(e.message || e); }"))


def slew_poll_script() -> str:
    """READ ONLY: slew complete?"""
    return _kv([("slew_complete", "sky6RASCOMTele.IsSlewComplete"),
                ("last_slew_error", "sky6RASCOMTele.LastSlewError")])


def expose_script() -> str:
    """Start one exposure with the camera_setup_script settings."""
    return _kv([("err", "err")],
               pre="var err = '';"
                   "try { ccdsoftCamera.TakeImage(); } catch (e) { err = String(e.message || e); }")


def expose_poll_script() -> str:
    """READ ONLY: exposure done? and the file TheSky saved."""
    return _kv([("complete", "ccdsoftCamera.IsExposureComplete"),
                ("last_image", "ccdsoftCamera.LastImageFileName")])


def automap_script() -> str:
    """CANDIDATE add: sky6RASCOMTheSky.AutoMap() (a published method:
    Image Link TheSky's last camera image and add it to TPoint as a
    pointing sample), then the Image Link results. Unconfirmed on build
    14139: the first supervised test checks the TPoint point count before
    and after ONE call."""
    return _kv([("err", "err"),
                ("succeeded", "ImageLinkResults.succeeded"),
                ("error_text", "ImageLinkResults.errorText"),
                ("image_scale", "ImageLinkResults.imageScale"),
                ("solution_rms", "ImageLinkResults.solutionRMS")],
               pre="var err = '';"
                   "try { sky6RASCOMTheSky.AutoMap(); } catch (e) { err = String(e.message || e); }")


def abort_script() -> str:
    """Stop: abort the exposure and the slew (tracking is left as is)."""
    return _kv([("cam_err", "ce"), ("mount_err", "me")],
               pre=("var ce = '', me = '';"
                    "try { ccdsoftCamera.Abort(); } catch (e) { ce = String(e.message || e); }"
                    "try { sky6RASCOMTele.Abort(); } catch (e) { me = String(e.message || e); }"))


def open_autocal_window_script() -> str:
    """CANDIDATE (window mode): TheSkyXAction runs a toolbar command, so
    this can at most OPEN the Automated Pointing Calibration window; the
    point count, Create Pointing Targets and Start stay with the operator.
    The numeric id form (AutoPointingCalibration = 105 on build 14139);
    unconfirmed which argument form execute() takes."""
    return _kv([("err", "err")],
               pre=("var err = '';"
                    "try { TheSkyXAction.execute(TheSkyXAction.AutoPointingCalibration); }"
                    " catch (e) { err = String(e.message || e); }"))


SCRIPT_BUILDERS = {
    "camera_read": camera_read_script,
    "camera_connect": camera_connect_script,
    "camera_disconnect": camera_disconnect_script,
    "slew_poll": slew_poll_script,
    "expose": expose_script,
    "expose_poll": expose_poll_script,
    "automap": automap_script,
    "abort": abort_script,
    "open_autocal_window": open_autocal_window_script,
}
READ_ONLY_SCRIPTS = ("camera_read", "slew_poll", "expose_poll")


# --------------------------------------------------------------- the plan

def clamp_points(n) -> int:
    return max(MIN_POINTS, min(int(n), MAX_POINTS))


def est_minutes(points: int, exposure_s: float) -> int:
    return round(points * (float(exposure_s) + OVERHEAD_S) / 60.0)


def build_plan(config, points: int = DEFAULT_POINTS, mode: str = DEFAULT_MODE,
               exposure_s: float | None = None, binning: int | None = None,
               now: datetime | None = None, sky_points: list | None = None) -> dict:
    """The whole operator-started procedure as numbered steps. Pure apart
    from the point grid (tpoint_mapping.plan_points; sky_points overrides
    it, for tests). Nothing is sent. {"mode", "points", "params", "steps",
    "sky_points", "est_minutes"}."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    n = clamp_points(points)
    t = float(exposure_s if exposure_s is not None else
              getattr(config, "tpoint_mapping_exposure_s", 5.0) or 5.0)
    b = int(binning if binning is not None else
            getattr(config, "tpoint_mapping_binning", 2) or 2)
    scale = float(getattr(config, "pixel_scale_arcsec", 0.236) or 0.236) * b
    min_alt = float(getattr(config, "tpoint_mapping_min_alt", 30.0) or 30.0)
    lat = float(getattr(config, "observatory_lat", 31.9))
    if sky_points is None and mode == "loop":
        from photonscript.scheduler import tpoint_mapping as tm
        sky_points = tm.plan_points(n, lat, min_alt)["points"]
    sky_points = list(sky_points or [])
    nina = str(getattr(config, "nina_base_url", "") or "http://localhost:1888/v2/api")
    hand = nina_handoff(nina)
    steps: list[dict] = []

    def step(phase, actor, what, script=None, requests=None, status="confirmed"):
        steps.append({"n": len(steps) + 1, "phase": phase, "actor": actor,
                      "what": what, "script": script, "requests": requests,
                      "status": status})

    step("preflight", "photonscript", "Read GET /api/arm and GET /api/apps/status "
         "from the scheduler and NINA #1's safety monitor; refuse unless every "
         "check passes (preflight())")
    step("preflight", "operator", "Confirm at the console: PHD2 not guiding, "
         "roof open by eye (webcam), TheSky camera setup = the AP26MC driver "
         "(Camera Setup, once by hand), TPoint window open")
    step("backup", "photonscript", "Copy every TPoint file to "
         f"{DEFAULT_BACKUP_ROOT}\\<UTC stamp> (backup_plan()), then "
         "tpoint-stats (the BEFORE numbers)")
    step("handoff", "photonscript", "Lend NINA #1's camera to TheSky",
         requests=hand["lend"])
    step("handoff", "photonscript", "TheSky: read the selected camera",
         script=camera_read_script())
    step("handoff", "photonscript", "TheSky: connect its camera",
         script=camera_connect_script())
    step("setup", "photonscript", f"TheSky camera: {t:g} s, bin {b}, light, "
         "autosave, asynchronous", script=camera_setup_script(t, b))
    step("setup", "photonscript", f"TheSky Automated Image Link: scale "
         f"{scale:.3f}\"/px, {t:g} s", script=ails_setup_script(scale, t))
    if mode == "loop":
        step("run", "photonscript",
             f"Per point (x{len(sky_points)}): slew (async) and poll, expose "
             "and poll, AutoMap, record a CSV row; stop on 3 unsolved in a "
             "row, an operator abort, the roof going unsafe or the armer "
             "leaving DISARMED", status="candidate (AutoMap unconfirmed)")
        if sky_points:
            p = sky_points[0]
            label = "tpoint autocal 1"
            step("run", "photonscript", f"e.g. point 1: slew to alt "
                 f"{p['alt']} az {p['az']}",
                 script=slew_altaz_script(p["alt"], p["az"], label, min_alt))
        step("run", "photonscript", "poll slew", script=slew_poll_script())
        step("run", "photonscript", "expose", script=expose_script())
        step("run", "photonscript", "poll exposure", script=expose_poll_script())
        step("run", "photonscript", "add the point (Image Link + TPoint sample)",
             script=automap_script(), status="candidate")
    else:
        step("run", "photonscript", "Open TheSky's Automated Pointing "
             "Calibration window", script=open_autocal_window_script(),
             status="candidate")
        step("run", "operator", f"In the window: Create Pointing Targets "
             f"({n}), check the Image Link settings, Start. PhotonScript "
             "watches the camera AutoSave folder (frames) and the mount "
             "(read only) and reports progress")
    step("abort", "operator / photonscript", "Abort any time: TheSky Abort "
         "in the window (window mode) or photonscript abort (loop mode)",
         script=abort_script())
    step("finish", "operator", "TPoint window: fit (Super Model), check "
         "the RMS, save the model, Apply pointing corrections / ProTrack as "
         "usual (no scripting object for any of it)")
    step("finish", "photonscript", "TheSky: disconnect its camera",
         script=camera_disconnect_script())
    step("finish", "photonscript", "Give the camera back to NINA #1",
         requests=hand["return"])
    step("finish", "photonscript", "tpoint-stats (the AFTER numbers), a "
         "before / after line in runs/<night>_events.jsonl; never arms")
    return {"mode": mode, "points": n if mode == "window" else len(sky_points),
            "requested_points": n,
            "params": {"exposure_s": t, "binning": b, "image_scale": round(scale, 4),
                       "min_alt": min_alt},
            "sky_points": sky_points, "steps": steps,
            "est_minutes": est_minutes(n, t)}


def format_plan(plan: dict, pre: dict | None = None,
                show_scripts: bool = True) -> str:
    """The dry-run text: the preflight, then every step (and its script /
    requests)."""
    out = [f"TPoint autocal DRY RUN (nothing sent): mode {plan['mode']}, "
           f"{plan['points']} points (asked {plan['requested_points']}), "
           f"{plan['params']['exposure_s']:g} s bin {plan['params']['binning']} "
           f"at {plan['params']['image_scale']}\"/px, ~{plan['est_minutes']} min"]
    if pre is not None:
        out.append(f"Preflight: {'GO' if pre['go'] else 'NO GO'}")
        for c in pre["checks"]:
            out.append(f"  [{c['status'].upper():7}] {c['name']}: {c['detail']}")
    out.append("Steps:")
    for s in plan["steps"]:
        tag = "" if s["status"] == "confirmed" else f" [{s['status']}]"
        out.append(f"  {s['n']:2}. ({s['phase']}, {s['actor']}) {s['what']}{tag}")
        for r in s.get("requests") or []:
            out.append(f"        {r['method']} {r['url']}  ({r['why']})")
        if show_scripts and s.get("script"):
            out.append(f"        JS: {s['script']}")
    out.append("--go is not implemented: " + GO_NOT_IMPLEMENTED)
    return "\n".join(out)


def run_go(*_args, **_kwargs):
    """The live run. Deliberately not implemented (docs/TPOINT_AUTOCAL.md:
    the supervised first test comes first)."""
    raise NotImplementedError(GO_NOT_IMPLEMENTED)
