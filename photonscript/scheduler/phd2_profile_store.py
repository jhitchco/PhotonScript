"""PHD2's stored equipment profile in the Windows registry (PS-89).

Bit depth, saturation, star mass tolerance, minimum HFD, auto exposure,
auto-restore calibration, Dec compensation, the calibration step and more are
not exposed by PHD2's JSON-RPC API; PHD2 keeps them per profile under

    HKCU\\Software\\StarkLabs\\PHDGuidingV2\\profile\\<id>\\...

(wxWidgets' registry config: a config path "/profile/1/camera/gain" is the
value "gain" of the key "profile\\1\\camera").

KEYS below is the one table that maps each audited setting to its registry
location. PS-119: the names were verified for READING against the scope PC's
`reg export` of 2026-10-04 (PHD2 2.6.14, profile 2 "Primary RC Profile
(Guider)", currentProfile=2); an unverified row would be read and shown
("candidate") but reported unknown. Writing is a separate gate: only the
logical keys in WRITABLE may be written (none yet), on top of
phd2_audit_autofix, PHD2 closed and a backup. To re-check after a PHD2
update: on the scope PC run

    reg export HKCU\\Software\\StarkLabs\\PHDGuidingV2 phd2.reg

(or GET /api/phd2/audit?refresh=1&raw=1, which lists every value read).

derived() turns the raw values into what the audit compares: the camera bit
depth (camera/<driver>/bpp), the active RA / Dec algorithm names (the
X/YGuideAlgorithm enum) and their minMove / aggressiveness under
scope/GuideAlgorithm/<X|Y>/<name>, the Dec guide mode name, auto exposure,
the mass-change percent, the newest sane Guiding Assistant run (GA/<ts>,
pa_error under GA_PA_MAX_ARCMIN) and the stored calibration
(scope/calibration).

read() flattens the whole profile key (so the raw dump shows real names);
backup() saves `reg export` of the profile to
<data_dir>/phd2_profile_backups/<ts>_<id>.reg plus a JSON copy; write()
refuses while phd2.exe runs (PHD2 rewrites its config on exit), without a
backup, for an unverified key or one that does not already exist.
Restore a backup with `reg import <file>` while PHD2 is closed.
Off Windows (and without winreg) everything reports available=False.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = r"Software\StarkLabs\PHDGuidingV2"

# logical key -> (sub path under profile\<id>, value name, verified for read).
# Verified against the scope PC's reg export (2026-10-04, PHD2 2.6.14). A
# "*" in the sub path matches one key level (the camera driver's own key).
# A None name = no candidate known (always unknown until filled in).
KEYS: dict[str, tuple[str, str | None, bool]] = {
    "name":                   ("", "name", True),
    "pixel_size_um":          ("camera", "pixelsize", True),
    "gain":                   ("camera", "gain", True),
    "binning":                ("camera", "binning", True),
    "bit_depth":              ("camera/*", "bpp", True),
    "saturation_by_adu":      ("camera", "SaturationByADU", True),
    "saturation_adu":         ("camera", "SaturationADU", True),
    "auto_load_darks":        ("camera", "AutoLoadDarks", True),
    "auto_load_defect_map":   ("camera", "AutoLoadDefectMap", True),
    "exposure_ms":            ("", "ExposureDurationMs", True),
    "focal_length_mm":        ("frame", "focalLength", True),
    "search_region_px":       ("guider/onestar", "SearchRegion", True),
    "mass_change_enabled":    ("guider/onestar", "MassChangeThresholdEnabled", True),
    "mass_change_pct":        ("guider/onestar", "MassChangeThreshold", True),
    "min_hfd_px":             ("guider", "StarMinHFD", True),
    "min_snr":                ("guider", "StarMinSNR", True),
    "multi_star":             ("guider/multistar", "enabled", True),
    "auto_restore_cal":       ("", "AutoLoadCalibration", True),
    "dec_compensation":       ("scope", "UseDecComp", True),
    "reverse_dec_after_flip": ("scope", "CalFlipRequiresDecFlip", True),
    "calibration_step_ms":    ("scope", "CalibrationDuration", True),
    "calibration_distance_px": ("scope", "CalibrationDistance", True),
    "backlash_comp":          ("scope", "BacklashCompEnabled", True),
    "stop_guiding_when_slewing": ("scope", "StopGuidingWhenSlewing", True),
    "assume_orthogonal":      ("scope", "AssumeOrthogonal", True),
    "dec_guide_mode_enum":    ("scope", "DecGuideMode", True),
    "ra_algorithm_enum":      ("scope", "XGuideAlgorithm", True),
    "dec_algorithm_enum":     ("scope", "YGuideAlgorithm", True),
}
# Logical keys write() may change (behind phd2_audit_autofix, PHD2 closed and
# a backup). Empty: reading is verified, writing is not enabled yet.
WRITABLE: frozenset = frozenset()
# the selected profile id, at the root key
CURRENT_PROFILE = ("", "currentProfile", True)
# PHD2 2.6 GUIDE_ALGORITHM enum -> (display name, GuideAlgorithm key name)
ALGORITHMS = {0: ("None", "None"), 1: ("Hysteresis", "Hysteresis"),
              2: ("Lowpass", "Lowpass"), 3: ("Lowpass2", "Lowpass2"),
              4: ("Resist Switch", "ResistSwitch"),
              5: ("Predictive PEC", "GaussianProcess"), 6: ("ZFilter", "ZFilter")}
# PHD2 DEC_GUIDE_MODE enum
DEC_MODES = {0: "Off", 1: "Auto", 2: "North", 3: "South"}
# PHD2 CalibrationIssueType (scope/calibration/last_issue)
CAL_ISSUES = {0: None, 1: "too few steps", 2: "axes not orthogonal",
              3: "RA / Dec rates inconsistent", 4: "differs from the last calibration"}
GA_PA_MAX_ARCMIN = 60.0   # a Guiding Assistant run with a larger polar error is bad
# Software Bisque ASCOM telescope driver flags (HKLM, report only, never
# written). Where the driver keeps them is unknown (ASCOM profile or its own
# file): no candidate names yet, so these rows read as unknown.
ASCOM_DRIVER_KEY = r"SOFTWARE\WOW6432Node\ASCOM\Telescope Drivers\ASCOM.SoftwareBisque.Telescope"
ASCOM_KEYS: dict[str, tuple[str, str | None, bool]] = {
    "ascom_direct_guide":    (ASCOM_DRIVER_KEY, None, False),
    "ascom_pointing_state":  (ASCOM_DRIVER_KEY, None, False),
}


def _winreg():
    """The winreg module, or None off Windows (tests monkeypatch this)."""
    try:
        import winreg  # noqa: F401
        return winreg
    except ImportError:
        return None


def phd2_running() -> bool:
    """True while a phd2.exe process exists (tests monkeypatch this)."""
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if str(p.info.get("name") or "").lower() == "phd2.exe":
                return True
    except Exception as e:  # noqa: BLE001
        logger.debug("phd2_running check failed: %s", e)
    return False


def _reg_export(key: str, path: Path) -> tuple[bool, str]:
    """`reg export` a key to a .reg file (tests monkeypatch this)."""
    if sys.platform != "win32":
        return False, "reg export needs Windows"
    try:
        p = subprocess.run(["reg", "export", key, str(path), "/y"],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    ok = p.returncode == 0 and path.exists() and path.stat().st_size > 0
    return ok, (p.stdout or p.stderr or "").strip()


def _walk(reg, key, prefix: str, out: dict, types: dict) -> None:
    i = 0
    while True:
        try:
            name, value, typ = reg.EnumValue(key, i)
        except OSError:
            break
        k = f"{prefix}/{name}" if prefix else name
        out[k] = value
        types[k] = typ
        i += 1
    i = 0
    while True:
        try:
            sub = reg.EnumKey(key, i)
        except OSError:
            break
        try:
            with reg.OpenKey(key, sub) as h:
                _walk(reg, h, f"{prefix}/{sub}" if prefix else sub, out, types)
        except OSError:
            pass
        i += 1


def _flat(reg, path: str) -> tuple[dict, dict]:
    out, types = {}, {}
    with reg.OpenKey(reg.HKEY_CURRENT_USER, path) as h:
        _walk(reg, h, "", out, types)
    return out, types


def list_profiles() -> dict:
    """{"available", "profiles": [{id, name}], "current_id"} from the
    registry (names and the current id are candidates, see KEYS)."""
    reg = _winreg()
    if reg is None:
        return {"available": False, "note": "not Windows (no registry)",
                "profiles": []}
    try:
        root, _t = _flat(reg, ROOT)
    except OSError as e:
        return {"available": False, "note": f"no PHD2 registry key ({e})",
                "profiles": []}
    ids = sorted({k.split("/")[1] for k in root
                  if k.startswith("profile/") and k.count("/") >= 2})
    profiles = [{"id": i, "name": root.get(f"profile/{i}/{KEYS['name'][1]}")}
                for i in ids]
    cur = root.get(CURRENT_PROFILE[1])
    return {"available": True, "profiles": profiles,
            "current_id": str(cur) if cur is not None else None}


def resolve_id(profile_id=None, name: str | None = None) -> str | None:
    """The registry id of a profile: the API id when known, else the one
    whose name matches, else the current one, else the only one."""
    if profile_id not in (None, ""):
        return str(profile_id)
    ps = list_profiles()
    if not ps.get("available"):
        return None
    if name:
        hit = [p for p in ps["profiles"]
               if str(p.get("name") or "").strip().lower() == name.strip().lower()]
        if len(hit) == 1:
            return hit[0]["id"]
    if ps.get("current_id"):
        return ps["current_id"]
    if len(ps["profiles"]) == 1:
        return ps["profiles"][0]["id"]
    return None


def _loc(logical: str, raw: dict | None = None) -> str | None:
    """The flat path of a logical key; a "*" level resolves against raw."""
    sub, name, _v = KEYS[logical]
    if name is None:
        return None
    loc = f"{sub}/{name}" if sub else name
    if "*" in loc and raw is not None:
        import fnmatch
        hits = sorted(k for k in raw if fnmatch.fnmatchcase(k, loc)
                      and k.count("/") == loc.count("/"))
        return hits[0] if hits else loc
    return loc


def writable(logical: str) -> bool:
    """True when write() may change this key (verified name, in WRITABLE)."""
    k = KEYS.get(logical)
    return bool(k and k[1] and k[2] and logical in WRITABLE and "*" not in k[0])


def _fnum(v):
    try:
        return float(str(v).strip().split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _ga_runs(raw: dict) -> list[dict]:
    """Every Guiding Assistant run stored under GA/<timestamp>, oldest first."""
    runs: dict[str, dict] = {}
    for k, v in raw.items():
        parts = k.split("/")
        if len(parts) == 3 and parts[0] == "GA":
            runs.setdefault(parts[1], {})[parts[2]] = v
    out = []
    for ts, r in sorted(runs.items()):
        out.append({"time_local": str(r.get("timestamp") or ts).replace(" ", "T"),
                    "ra_min_move_rec": _fnum(r.get("rec_ra_minmove")),
                    "dec_min_move_rec": _fnum(r.get("rec_dec_minmove")),
                    "pa_error_arcmin": _fnum(r.get("pa_error")),
                    "snr": _fnum(r.get("snr"))})
    return out


def newest_sane_ga(raw: dict) -> dict | None:
    """The newest GA run with a polar error under GA_PA_MAX_ARCMIN (a run
    with a wild polar error measured nothing useful)."""
    ok = [g for g in _ga_runs(raw) if g["ra_min_move_rec"] is not None and
          (g["pa_error_arcmin"] is None or g["pa_error_arcmin"] < GA_PA_MAX_ARCMIN)]
    return ok[-1] if ok else None


def stored_calibration(raw: dict) -> dict | None:
    """scope/calibration as stored (angles rad, rates px/ms, declination
    rad, guide rates deg/s) plus the step counts and last_issue."""
    c = {k.split("/", 2)[2]: v for k, v in raw.items()
         if k.startswith("scope/calibration/") and k.count("/") == 2}
    if not c or c.get("xRate") is None:
        return None
    return c


def derived(raw: dict, values: dict) -> dict:
    """{logical: value} computed from the raw profile (see the module doc)."""
    out: dict = {}

    def val(k):
        return (values.get(k) or {}).get("value")
    for axis, key in (("ra", "ra_algorithm_enum"), ("dec", "dec_algorithm_enum")):
        try:
            name, folder = ALGORITHMS[int(val(key))]
        except (TypeError, ValueError, KeyError):
            continue
        out[f"{axis}_algorithm"] = name
        base = f"scope/GuideAlgorithm/{'X' if axis == 'ra' else 'Y'}/{folder}/"
        mm = _fnum(raw.get(base + "minMove"))
        if mm is not None:
            out[f"{axis}_min_move"] = mm
        ag = _fnum(raw.get(base + "Aggressiveness"))
        if ag is None:
            ag = _fnum(raw.get(base + "aggression"))
            ag = ag * 100.0 if ag is not None and ag <= 1.0 else ag
        if ag is not None:
            out[f"{axis}_aggressiveness"] = ag
    try:
        out["dec_guide_mode"] = DEC_MODES[int(val("dec_guide_mode_enum"))]
    except (TypeError, ValueError, KeyError):
        pass
    exp = _fnum(val("exposure_ms"))
    if exp is not None:
        # PHD2 keeps a fixed exposure in ms; a negative value is "Auto"
        out["auto_exposure"] = exp < 0
    pct = _fnum(val("mass_change_pct"))
    if pct is not None:
        out["mass_change_pct"] = pct * 100.0 if pct <= 1.0 else pct
    ga = newest_sane_ga(raw)
    if ga:
        out["ga"] = ga
    cal = stored_calibration(raw)
    if cal:
        out["calibration"] = cal
    return out


def read(profile_id=None, name: str | None = None) -> dict:
    """{"available", "profile_id", "values": {logical: {value, verified,
    location}}, "raw": {flat path: value}}. Never raises."""
    reg = _winreg()
    if reg is None:
        return {"available": False, "note": "not Windows (no registry)",
                "values": {}, "raw": {}}
    pid = resolve_id(profile_id, name)
    if pid is None:
        return {"available": False, "note": "PHD2 profile id unknown",
                "values": {}, "raw": {}}
    try:
        raw, _t = _flat(reg, f"{ROOT}\\profile\\{pid}")
    except OSError as e:
        return {"available": False, "profile_id": pid,
                "note": f"profile {pid} not in the registry ({e})",
                "values": {}, "raw": {}}
    values = {}
    for logical, (_sub, _name, verified) in KEYS.items():
        loc = _loc(logical, raw)
        values[logical] = {"value": raw.get(loc) if loc else None,
                           "verified": bool(verified and loc),
                           "location": loc}
    try:
        der = derived(raw, values)
    except Exception as e:  # noqa: BLE001
        logger.debug("PHD2 profile derive failed: %s", e)
        der = {}
    return {"available": True, "profile_id": pid, "values": values,
            "derived": der, "raw": raw}


def read_ascom() -> dict:
    """{logical: {value, verified, location}} of the Bisque driver flags
    (HKLM, read only). Never raises."""
    reg = _winreg()
    out = {}
    for logical, (path, name, verified) in ASCOM_KEYS.items():
        v = None
        if reg is not None and name:
            try:
                with reg.OpenKey(reg.HKEY_LOCAL_MACHINE, path) as h:
                    v = reg.QueryValueEx(h, name)[0]
            except OSError:
                v = None
        out[logical] = {"value": v, "verified": bool(verified and name),
                        "location": f"HKLM\\{path}\\{name}" if name else None}
    return out


def backup_dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "phd2_profile_backups"


def backup(config, profile_id) -> dict:
    """Export profile <id> to <ts>_<id>.reg (+ .json). ok only when the .reg
    file was written."""
    d = backup_dir(config)
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    reg_path = d / f"{ts}_{profile_id}.reg"
    ok, note = _reg_export(f"HKCU\\{ROOT}\\profile\\{profile_id}", reg_path)
    snap = read(profile_id)
    json_path = d / f"{ts}_{profile_id}.json"
    try:
        json_path.write_text(json.dumps(snap.get("raw") or {}, indent=1,
                                        default=str), encoding="utf-8")
    except OSError as e:
        note = f"{note}; json copy failed: {e}"
    return {"ok": bool(ok), "reg": str(reg_path) if ok else None,
            "json": str(json_path), "note": note}


def write(config, profile_id, changes: dict, *, backup_path: str | None) -> dict:
    """Write {logical: value} into profile <id>. Refused while phd2.exe runs,
    without a backup, or for an unverified / missing value; each value keeps
    its registry type (DWORD -> int, string -> str)."""
    reg = _winreg()
    if reg is None:
        return {"ok": False, "note": "not Windows (no registry)", "written": {}}
    if phd2_running():
        return {"ok": False, "note": "PHD2 is running (it rewrites its profile "
                                     "on exit): close it first", "written": {}}
    if not backup_path or not Path(backup_path).exists():
        return {"ok": False, "note": "no backup of the profile", "written": {}}
    written, refused = {}, {}
    for logical, value in changes.items():
        if logical not in KEYS:
            refused[logical] = "not a profile key"
            continue
        sub, name, verified = KEYS[logical]
        if not (verified and name):
            refused[logical] = "registry name unverified"
            continue
        if not writable(logical):
            refused[logical] = "registry name unverified for writing (not in WRITABLE)"
            continue
        path = f"{ROOT}\\profile\\{profile_id}" + (
            "\\" + sub.replace("/", "\\") if sub else "")
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0,
                             reg.KEY_READ | reg.KEY_SET_VALUE) as h:
                _old, typ = reg.QueryValueEx(h, name)
                if typ == reg.REG_DWORD:
                    v = int(bool(value)) if isinstance(value, bool) else int(round(float(value)))
                elif typ == reg.REG_SZ:
                    v = str(value)
                else:
                    refused[logical] = f"unexpected registry type {typ}"
                    continue
                reg.SetValueEx(h, name, 0, typ, v)
                written[logical] = v
        except OSError as e:
            refused[logical] = f"not written ({e})"
    return {"ok": not refused and bool(written), "written": written,
            "refused": refused, "backup": backup_path}
