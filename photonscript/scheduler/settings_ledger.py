"""Settings ledger: what every program was set to, and when it changed.

Why: the 2026-10-12 night failed on settings, not hardware: PHD2 guided on a
FAIL calibration, AutoFind wanted SNR 50 from a 28 SNR star, the pulse-guide
alert was silenced, NINA #1 plate-solved at bin 2, TheSky lost its time
zone in a reinstall, the guide scale PhotonScript used was 5x PHD2's and
the RC16 EAF counter was reset. Each was found by hand, after the fact.
The ledger records every program's settings at the moments that matter so
"what was it set to when that sub was taken" has an answer, and judges them
against an editable desired-values file (config/settings/
desired_settings.toml) so a wrong one is reported the first time it shows.

READ ONLY. Every read is a GET (ninaAPI), a get_* JSON-RPC call (PHD2), the
PHD2 registry profile, a read-only TheSky script (the PS-104 reads) or
PhotonScript's own config. Nothing is ever written to NINA, PHD2, TheSky or
the mount.

    take(config, reason)     one snapshot: collect every app, carry forward
                             what could not be read, hash, store only when
                             the hash changed, evaluate the rules, push the
                             newly failing ones (once per rule per night)
    request(config, reason)  the armer's hook: take() in the background,
                             coalesced (never blocks, never raises)
    at / for_sub / diff      the read side (CLI `photonscript settings`,
                             GET /api/settings-ledger/...)
    backfill(config)         past nights from the PS-89 PHD2 audits, the
                             PS-104 TheSky audits, the sub records (camera
                             gain / offset / readout / binning / setpoint,
                             focuser position) and the PHD2 guide-log headers

Storage, <data_dir>/settings_ledger/:
    latest.json                      {night, id, t_utc, hash}: in force now
    <night>/index.jsonl              one line per snapshot TAKEN (changed or
                                     not): t_utc, reason, armer state, the id
                                     in force, the hash, compact live
                                     readings (focuser position, temps,
                                     PHD2 state), the diff when it changed,
                                     the failing / warning rule ids
    <night>/<id>.json                the full snapshot, written only when the
                                     hash changed (compact JSON)
    <night>/status.json              the newest rule results, the rules
                                     pushed tonight, the rules that failed at
                                     any time tonight, last change per app

A snapshot id is its UTC time, "20261013T034712Z"; its night follows from
it (store.night_of). Sub records get the id in force at capture time
(runs.append_sub_record -> current_id), and for older subs the id is
found by time (for_sub).

Hashes cover only settings. Readings that move by themselves (temperatures,
focuser position, mount coordinates, PHD2 state, file ages) are kept in
"live" (stored in every index line, compact) or dropped.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared.fileops import replace_retry
from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

APPS = ("nina1", "nina2", "phd2", "thesky", "photonscript")
APP_LABELS = {"nina1": "NINA #1 (RC16)", "nina2": "NINA #2 (Piggy-600)", "phd2": "PHD2",
              "thesky": "TheSky", "photonscript": "PhotonScript config"}
PASS, WARN, FAIL, UNKNOWN, INFO, NA = "pass", "warn", "fail", "unknown", "info", "na"
STATUS_RANK = {FAIL: 0, WARN: 1, UNKNOWN: 2, INFO: 3, PASS: 4, NA: 5}
FORMS = ("equals", "one_of", "min", "max", "not_contains", "any_true", "computed", "report")
WHEN = ("running", "guiding", "not_guiding", "tracking")
DEFAULT_RULES = (Path(__file__).resolve().parents[2] / "config" / "settings"
                 / "desired_settings.toml")
DIFF_CAP = 300            # changed paths kept per app in one diff
REDACTED = "<redacted>"
_SECRET = re.compile(r"(api_?key|secret|token|passw|user_key|credential)", re.IGNORECASE)

# ninaAPI v2 equipment read per NINA instance (GET .../equipment/<d>/info)
NINA_DEVICES = ("camera", "filterwheel", "focuser", "guider", "mount", "rotator",
                "safetymonitor", "weather", "flatdevice", "dome", "switch")
# readings that move by themselves: kept as live (not hashed)
NINA_LIVE = {
    "camera": ("Temperature", "CoolerPower", "CameraState", "AtTargetTemp", "IsExposing"),
    "focuser": ("Position", "Temperature", "IsMoving"),
    "filterwheel": ("SelectedFilter", "IsMoving"),
    "mount": ("TrackingEnabled", "TrackingMode", "AtPark", "AtHome", "SideOfPier",
              "Slewing"),
    "guider": ("State",),
    "safetymonitor": ("IsSafe",),
    "weather": ("Humidity", "DewPoint", "Temperature", "CloudCover", "SkyQuality",
                "WindSpeed"),
    "flatdevice": ("CoverState", "LightOn", "Brightness"),
    "dome": ("ShutterStatus", "AtPark", "Azimuth", "Slewing"),
    "rotator": ("Position", "MechanicalPosition", "IsMoving"),
}
# readings dropped entirely (coordinates, times, measurements)
NINA_DROP = {
    "camera": ("ExposureEndTime", "LastDownloadTime", "Battery"),
    "focuser": ("IsSettling",),
    "mount": ("SiderealTime", "RightAscension", "Declination", "RightAscensionString",
              "DeclinationString", "Coordinates", "TimeToMeridianFlip", "Altitude",
              "AltitudeString", "Azimuth", "AzimuthString", "SiderealTimeString",
              "HoursToMeridianString", "TimeToMeridianFlipString", "UTCDate",
              "IsPulseGuiding"),
    "guider": ("LastGuideStep", "RMSError"),
    "weather": ("Pressure", "RainRate", "SkyBrightness", "SkyTemperature", "StarFWHM",
                "WindDirection", "WindGust"),
}
NINA_PROFILE_DROP = ("LastUsed",)
THESKY_LIVE = ("mount_tracking", "mount_parked", "camera_status", "site_clock",
               "mount_last_slew_error")
THESKY_DROP = ("site_jd", "site_lst_h", "site_clock_latency_s")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _num(v):
    if isinstance(v, bool):
        return float(v)
    try:
        x = float(str(v).strip()) if isinstance(v, str) else float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _norm(v) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v).lower())


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    t = _norm(v)
    if t in ("1", "on", "true", "yes", "enabled", "tracking"):
        return True
    if t in ("0", "off", "false", "no", "disabled", "none", "notracking"):
        return False
    return None


def _g(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, default=str, separators=(",", ":"))
        return s if len(s) <= 160 else s[:157] + "..."
    return str(v)


def redact(obj):
    """A copy with every secret-looking key's value replaced (NINA's API key
    fields, PhotonScript's tokens). Empty values stay empty."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET.search(str(k)) and not isinstance(v, (dict, list)):
                out[k] = REDACTED if v not in (None, "", 0, False) else v
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:16]


def flatten(obj, prefix: str = "") -> dict:
    """{"a.b.0.c": leaf}; empty dicts / lists are leaves."""
    out: dict = {}
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list) and obj:
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}.{i}" if prefix else str(i)))
    else:
        out[prefix] = obj
    return out


def diff_settings(old, new) -> dict:
    """{path: [old, new]} for every leaf that differs (None = absent)."""
    a, b = flatten(old or {}), flatten(new or {})
    out: dict = {}
    for k in sorted(set(a) | set(b)):
        if canonical(a.get(k)) != canonical(b.get(k)):
            out[k] = [a.get(k), b.get(k)]
    if len(out) > DIFF_CAP:
        keys = list(out)[:DIFF_CAP]
        out = {k: out[k] for k in keys} | {"_truncated": len(out) - DIFF_CAP}
    return out


def resolve(obj, path: str):
    """The value at a dot path (list index as a number); None when absent."""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def snapshot_id(t: datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%SZ")


def id_time(sid: str) -> datetime | None:
    try:
        return datetime.strptime(str(sid)[:16], "%Y%m%dT%H%M%SZ")
    except (TypeError, ValueError):
        return None


def apps_enabled(config) -> list[str]:
    raw = str(getattr(config, "settings_ledger_apps", ",".join(APPS)) or "")
    want = [a.strip().lower() for a in raw.split(",") if a.strip()]
    out = [a for a in APPS if a in want]
    if "nina2" in out and not getattr(config, "piggyback_enabled", False):
        out.remove("nina2")
    return out


def audit_rows_paged(config) -> dict[str, str]:
    """{PS-89 audit row id: ledger rule id} for the PS-89 rows the ledger
    pages itself (a FAIL-severity phd2 rule naming it in audit_row, the
    ledger on, pushing and reading PHD2). Those rows stay in the PS-89
    report but never page from there: one owner per page (2026-10-13, the
    AutoFind min SNR could page twice a night). Empty when the ledger would
    not page, so PS-89 keeps paging them. Never raises."""
    try:
        if not (getattr(config, "settings_ledger_enabled", False)
                and getattr(config, "settings_ledger_push", True)
                and "phd2" in apps_enabled(config)):
            return {}
        return {str(r["audit_row"]): str(r.get("id"))
                for r in load_rules(config).get("rule") or []
                if r.get("audit_row") and r.get("app") == "phd2"
                and not r.get("report") and r.get("severity") == FAIL}
    except Exception as e:  # noqa: BLE001
        logger.debug("audit_rows_paged: %s", e)
        return {}


# --------------------------------------------------------------------------
# paths and storage
# --------------------------------------------------------------------------

def ledger_dir(config) -> Path:
    return Path(config.data_dir) / "settings_ledger"


def night_dir(config, night: str) -> Path:
    return ledger_dir(config) / night


def index_path(config, night: str) -> Path:
    return night_dir(config, night) / "index.jsonl"


def snap_path(config, sid: str, night: str | None = None) -> Path:
    night = night or night_of_id(config, sid)
    return night_dir(config, night) / f"{sid}.json"


def status_path(config, night: str) -> Path:
    return night_dir(config, night) / "status.json"


def latest_path(config) -> Path:
    return ledger_dir(config) / "latest.json"


def night_of_id(config, sid: str) -> str:
    t = id_time(sid)
    return store.night_of(config, t) if t else ""


def _write_compact(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str), encoding="utf-8")
    replace_retry(tmp, path)


def nights(config) -> list[str]:
    d = ledger_dir(config)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))


def index(config, night: str) -> list[dict]:
    return store.read_jsonl(index_path(config, night))


def load_snapshot(config, sid: str) -> dict | None:
    if not sid:
        return None
    return store.read_json(snap_path(config, sid))


def load_latest_pointer(config) -> dict | None:
    return store.read_json(latest_path(config))


def load_latest(config) -> dict | None:
    p = load_latest_pointer(config) or {}
    return load_snapshot(config, p.get("id")) if p.get("id") else None


def load_status(config, night: str) -> dict:
    return store.read_json(status_path(config, night)) or {}


_cur_cache: dict = {}


def current_id(config, night: str | None = None) -> str | None:
    """The snapshot id in force now (the latest pointer), for stamping a sub
    record; None while the ledger is off or empty. Cached on the pointer's
    mtime: one stat per sub."""
    if not getattr(config, "settings_ledger_enabled", False):
        return None
    p = latest_path(config)
    try:
        m = p.stat().st_mtime_ns
    except OSError:
        return None
    hit = _cur_cache.get(str(p))
    if hit and hit[0] == m:
        return hit[1]
    sid = (store.read_json(p) or {}).get("id")
    _cur_cache[str(p)] = (m, sid)
    return sid


# --------------------------------------------------------------------------
# collectors (each never raises: (settings, live, meta))
# --------------------------------------------------------------------------

def split_device(device: str, info: dict) -> tuple[dict, dict]:
    """A ninaAPI device reply -> (settings, live)."""
    live_keys = NINA_LIVE.get(device, ())
    drop = set(NINA_DROP.get(device, ()))
    settings = {k: v for k, v in info.items() if k not in drop and k not in live_keys}
    live = {k: info.get(k) for k in live_keys if k in info}
    if device == "camera":
        modes, i = info.get("ReadoutModes") or [], info.get("ReadoutMode")
        try:
            settings["ReadoutModeName"] = modes[int(i)]
        except (TypeError, ValueError, IndexError):
            pass
    return settings, live


def nina_profile_settings(profile: dict) -> dict:
    p = {k: v for k, v in (profile or {}).items() if k not in NINA_PROFILE_DROP}
    return redact(p)


def _unwrap(body):
    if isinstance(body, dict) and "Response" in body:
        return body["Response"]
    return body


async def collect_nina(config, base_url: str, http=None) -> tuple[dict, dict, dict]:
    """The active NINA profile (GET profile/show?active=true) and every
    device's info. A failed read leaves that part None (carried forward)."""
    import httpx
    own = http is None
    if own:
        http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=8.0)
    settings: dict = {"profile": None, "devices": {}}
    live: dict = {}
    notes: list[str] = []
    try:
        try:
            r = await http.get("/profile/show", params={"active": "true"})
            r.raise_for_status()
            prof = _unwrap(r.json())
            if isinstance(prof, dict) and prof:
                settings["profile"] = nina_profile_settings(prof)
            else:
                notes.append("no active profile")
        except Exception as e:  # noqa: BLE001
            notes.append(f"profile: {type(e).__name__}")
        if settings["profile"] is None and notes and "profile:" in notes[-1]:
            # NINA not answering: do not wait on eleven more timeouts
            return ({"profile": None, "devices": None}, {},
                    {"ok": False, "note": f"NINA {base_url} not reachable"})
        for d in NINA_DEVICES:
            try:
                r = await http.get(f"/equipment/{d}/info")
                r.raise_for_status()
                info = _unwrap(r.json())
                if not isinstance(info, dict):
                    settings["devices"][d] = None
                    continue
                s, lv = split_device(d, info)
                settings["devices"][d] = redact(s)
                if lv:
                    live[d] = lv
            except Exception as e:  # noqa: BLE001
                settings["devices"][d] = None
                notes.append(f"{d}: {type(e).__name__}")
    finally:
        if own:
            try:
                await http.aclose()
            except Exception:  # noqa: BLE001
                pass
    ok = settings["profile"] is not None
    return settings, live, {"ok": ok, "note": "; ".join(notes) or "read"}


async def collect_phd2(config, client=None) -> tuple[dict, dict, dict]:
    """PHD2's settings: the PS-89 audit's get_* reads (phd2_audit._collect_api),
    the full calibration data and the profile list over JSON-RPC, then the
    stored profile from the registry (readable with PHD2 closed), the
    "don't show again" alerts, the PS-93 calibration record and the dark
    library. Parts that cannot be read are None (carried forward)."""
    from photonscript.scheduler import phd2_audit as pa
    from photonscript.scheduler import phd2_profile_store as ps
    s: dict = {k: None for k in ("api", "calibration_data", "profiles", "profile",
                                 "profile_raw", "silenced_alerts", "calibration_record",
                                 "darks")}
    live: dict = {}
    notes: list[str] = []
    own = client is None
    try:
        if own:
            from photonscript.telescope_agent.phd2_client import PHD2Client
            client = PHD2Client(config.phd2_host, config.phd2_port, config=config)
            connected = await asyncio.wait_for(client.connect(), 10)
        else:
            connected = True
        if connected:
            try:
                api, src = await asyncio.wait_for(pa._collect_api(config, client), 60)
                if src.get("ok"):
                    live["app_state"] = api.pop("app_state", None)
                    api.pop("calibrated", None)
                    s["api"] = api
                else:
                    notes.append(src.get("note") or "PHD2 API")
            except Exception as e:  # noqa: BLE001
                notes.append(f"api: {type(e).__name__}: {e}")
            for key, fn in (("calibration_data", client.get_calibration_data),
                            ("profiles", client.get_profiles)):
                try:
                    s[key] = await asyncio.wait_for(fn(), 10)
                except Exception as e:  # noqa: BLE001
                    notes.append(f"{key}: {type(e).__name__}")
            if isinstance(s["calibration_data"], dict):
                live["calibrated"] = s["calibration_data"].get("calibrated")
        else:
            notes.append("PHD2 not reachable (registry profile only)")
    except Exception as e:  # noqa: BLE001
        notes.append(f"PHD2: {type(e).__name__}")
    finally:
        if own and client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    api = s["api"] or {}
    try:
        read = await asyncio.to_thread(ps.read, api.get("profile_id"), api.get("profile"))
        if read.get("available"):
            vals = {k: v.get("value") for k, v in (read.get("values") or {}).items()}
            der = {k: v for k, v in (read.get("derived") or {}).items()
                   if k not in ("calibration",)}
            s["profile"] = {"id": read.get("profile_id"), **vals, **der}
            s["profile_raw"] = read.get("raw")
            conf = await asyncio.to_thread(ps.read_confirm, read.get("profile_id"))
            if conf.get("available"):
                s["silenced_alerts"] = sorted(conf.get("silenced") or [])
        else:
            notes.append(read.get("note") or "registry profile not read")
    except Exception as e:  # noqa: BLE001
        notes.append(f"registry: {type(e).__name__}")
    try:
        from photonscript.scheduler import phd2_calibration as pc
        cal = pc.summary(config)
        rec = cal.get("record") or {}
        s["calibration_record"] = {k: rec.get(k) for k in (
            "t_utc", "grade", "reasons", "warnings", "context", "dec_deg", "ha_hr",
            "pier_side", "ortho_err_deg", "recommended_step_ms")} if rec else {"grade": None}
        live["calibration_age_days"] = cal.get("age_days")
    except Exception as e:  # noqa: BLE001
        notes.append(f"calibration record: {type(e).__name__}")
    try:
        files, _src = await asyncio.to_thread(pa._collect_files, config,
                                              api.get("profile_id"))
        lib = dict(files.get("dark_library") or {})
        if "age_days" in lib:
            live["dark_library_age_days"] = lib.pop("age_days")
        s["darks"] = {"dir": files.get("dir"), "defect_map": files.get("defect_map"),
                      "dark_library": lib or None}
    except Exception as e:  # noqa: BLE001
        notes.append(f"darks: {type(e).__name__}")
    ok = s["api"] is not None or s["profile"] is not None
    return s, {k: v for k, v in live.items() if v is not None}, \
        {"ok": ok, "note": "; ".join(notes) or "read"}


def collect_thesky(config, client=None) -> tuple[dict, dict, dict]:
    """The PS-104 audit's read-only TheSky scripts (thesky_audit.collect_thesky)."""
    from photonscript.scheduler import thesky_audit as ta
    try:
        obs, src = ta.collect_thesky(config, client)
    except Exception as e:  # noqa: BLE001
        return {}, {}, {"ok": False, "note": f"{type(e).__name__}: {e}"}
    if not src.get("ok"):
        return {}, {}, {"ok": False, "note": src.get("note") or "TheSky not reachable"}
    live = {k: obs.get(k) for k in THESKY_LIVE if obs.get(k) is not None}
    settings = {k: v for k, v in obs.items() if k not in THESKY_LIVE and k not in THESKY_DROP}
    return settings, live, {"ok": True, "note": src.get("note") or "read"}


def collect_photonscript(config) -> tuple[dict, dict, dict]:
    """PhotonScript's effective config, every field, secrets redacted."""
    try:
        if hasattr(config, "model_dump"):
            raw = config.model_dump()
        else:
            raw = {k: v for k, v in vars(config).items() if not k.startswith("_")}
        raw = {k: (str(v) if isinstance(v, Path) else v) for k, v in raw.items()
               if not callable(v)}
        return redact(json.loads(canonical(raw))), {}, {"ok": True, "note": "config"}
    except Exception as e:  # noqa: BLE001
        return {}, {}, {"ok": False, "note": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------
# sections from the stored audits (fallback and backfill)
# --------------------------------------------------------------------------

def parse_current(text):
    """An audit row's display value -> a value ("on" -> True, "1.6" -> 1.6,
    "manual (2026-10-13): on" -> True, "109.021; LST ..." -> 109.021)."""
    if text is None or text == "-":
        return None
    if not isinstance(text, str):
        return text
    t = re.sub(r"^manual \([^)]*\):\s*", "", text.strip())
    t = t.split(";")[0].strip()
    b = _bool(t)
    if b is not None and _norm(t) not in ("1", "0"):
        return b
    n = _num(t)
    if n is not None:
        return int(n) if n.is_integer() and re.fullmatch(r"[+-]?\d+", t) else n
    return t


PHD2_API_ROWS = {"camera": "camera", "binning": "binning", "guide_scale": "pixel_scale",
                 "exposure_ms": "exposure_ms", "search_region_px": "search_region_px",
                 "dec_guide_mode": "dec_guide_mode", "guide_output": "guide_output",
                 "ra_algorithm": "ra_algorithm", "dec_algorithm": "dec_algorithm",
                 "ra_min_move": "ra_min_move", "dec_min_move": "dec_min_move",
                 "ra_aggressiveness": "ra_aggressiveness",
                 "focal_length_mm": "focal_length_mm"}


def phd2_section_from_audit(audit: dict) -> dict:
    """The PS-89 audit's rows as a PHD2 settings section (rule paths
    profile.<id> / api.<key> / silenced_alerts / calibration_record.grade).
    Display values only: a FAIL row's "current" is what PHD2 had."""
    rows = {r.get("id"): r for r in (audit or {}).get("rows") or [] if r.get("id")}
    prof, api = {}, {}
    for rid, r in rows.items():
        v = parse_current(r.get("current"))
        if v is None:
            continue
        prof[rid] = v
        if rid in PHD2_API_ROWS:
            api[PHD2_API_ROWS[rid]] = v
    dd = str((rows.get("darks_or_defects") or {}).get("current") or "")
    if dd:
        m1, m2 = re.search(r"darks (on|off)", dd), re.search(r"defect map (on|off)", dd)
        if m1:
            prof["auto_load_darks"] = m1.group(1) == "on"
        if m2:
            prof["auto_load_defect_map"] = m2.group(1) == "on"
    if audit.get("profile_id") is not None:
        api["profile_id"] = audit.get("profile_id")
    sec: dict = {"api": api or None, "profile": prof or None,
                 "audit_rows": {rid: {"status": r.get("status"), "current": r.get("current")}
                                for rid, r in rows.items()}}
    sa = (rows.get("silenced_alerts") or {}).get("current")
    if sa is not None:
        sec["silenced_alerts"] = sorted(re.findall(r"(\w+Alert\w*)\s+off", str(sa)))
    cr = str((rows.get("calibration_record") or {}).get("current") or "")
    m = re.match(r"\s*(PASS|WARN|FAIL)\b", cr)
    sec["calibration_record"] = {"grade": m.group(1) if m else None}
    return sec


def thesky_section_from_audit(audit: dict) -> dict:
    """The PS-104 audit's rows as a TheSky settings section (row ids are
    the observed keys)."""
    out: dict = {}
    for r in (audit or {}).get("rows") or []:
        rid = r.get("id")
        if not rid or rid in THESKY_LIVE:
            continue
        v = parse_current(r.get("current"))
        if v is not None:
            out[rid] = v
    tz = str(next((r.get("current") for r in (audit or {}).get("rows") or []
                   if r.get("id") == "time_zone_dst"), "") or "")
    m = re.search(r"tz\s*([+-]?\d+(?:\.\d+)?)", tz)
    if m:
        out["time_zone"] = float(m.group(1))
    m = re.search(r"index\s*(\d+)", tz)
    if m:
        out["dst_index"] = int(m.group(1))
    return out


def _audit_fallback(config, app: str) -> tuple[dict, str] | None:
    """PHD2 / TheSky never read by the ledger yet: the newest stored audit."""
    try:
        if app == "phd2":
            from photonscript.scheduler import phd2_audit as pa
            a = pa.load_latest(config)
            return (phd2_section_from_audit(a), a.get("t_utc")) if a else None
        if app == "thesky":
            from photonscript.scheduler import thesky_audit as ta
            a = ta.load_latest(config)
            return (thesky_section_from_audit(a), a.get("t_utc")) if a else None
    except Exception as e:  # noqa: BLE001
        logger.debug("ledger audit fallback %s: %s", app, e)
    return None


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def rules_path(config) -> Path:
    p = str(getattr(config, "settings_rules_file", "") or "").strip()
    return Path(p) if p else DEFAULT_RULES


def lint_rules(doc: dict) -> list[str]:
    out, seen = [], set()
    rules = doc.get("rule") or []
    if not rules:
        out.append("no [[rule]] rows")
    for i, r in enumerate(rules):
        rid = r.get("id") or f"#{i}"
        if not r.get("id"):
            out.append(f"rule {i}: no id")
        if rid in seen:
            out.append(f"{rid}: duplicate id")
        seen.add(rid)
        if r.get("app") not in APPS:
            out.append(f"{rid}: app must be one of {APPS}")
        if not str(r.get("label") or "").strip():
            out.append(f"{rid}: no label")
        if not any(k in r for k in FORMS):
            out.append(f"{rid}: no desired form")
        if not r.get("report"):
            if r.get("severity") not in (FAIL, WARN):
                out.append(f"{rid}: severity must be fail or warn")
            for k in ("why", "fix"):
                if not str(r.get(k) or "").strip():
                    out.append(f"{rid}: no {k}")
        if r.get("computed") and r["computed"] not in COMPUTED:
            out.append(f"{rid}: unknown computed {r['computed']}")
        if not r.get("computed") and not r.get("any_true") and not r.get("path"):
            out.append(f"{rid}: no path")
        for w in r.get("when") or []:
            if w not in WHEN:
                out.append(f"{rid}: unknown when {w}")
        if "audit_row" in r and (r.get("app") != "phd2"
                                 or not isinstance(r["audit_row"], str)):
            out.append(f"{rid}: audit_row names a PS-89 row and needs app phd2")
        for k, v in r.items():
            if isinstance(v, str) and not v.isascii():
                out.append(f"{rid}: {k} is not plain ASCII")
    return out


def load_rules(config) -> dict:
    import tomllib
    p = rules_path(config)
    try:
        d = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"error": f"{p}: {e}", "rule": [], "path": str(p)}
    d["path"] = str(p)
    problems = lint_rules(d)
    if problems:
        logger.warning("settings rules %s: %s", p, "; ".join(problems))
        d["lint"] = problems
    return d


def _eq(v, want, tol=1e-6) -> bool:
    if isinstance(want, bool):
        return _bool(v) is want
    if isinstance(want, (int, float)):
        n = _num(v)
        return n is not None and not isinstance(v, bool) and abs(n - float(want)) <= tol
    return _norm(v) == _norm(want)


def _section(snap: dict, app: str) -> dict:
    return ((snap or {}).get("apps") or {}).get(app) or {}


def _fresh_live(snap: dict, app: str) -> dict:
    sec = _section(snap, app)
    return {} if sec.get("carried") else (sec.get("live") or {})


def _when_ok(cond: str, snap: dict) -> bool:
    if cond == "running":
        return str(snap.get("armer_state") or "") in ("RUNNING", "WATCHING")
    phd2 = _fresh_live(snap, "phd2")
    if cond == "guiding":
        return phd2.get("app_state") == "Guiding"
    if cond == "not_guiding":
        return phd2.get("app_state") != "Guiding"
    if cond == "tracking":
        ts = _fresh_live(snap, "thesky")
        if ts.get("mount_tracking") is not None:
            return bool(_bool(ts.get("mount_tracking"))) and not _bool(ts.get("mount_parked"))
        m = _fresh_live(snap, "nina1").get("mount") or {}
        return bool(m.get("TrackingEnabled")) and not m.get("AtPark")
    return False


WHEN_TEXT = {"running": "while a night runs", "guiding": "while PHD2 guides",
             "not_guiding": "while PHD2 is not guiding",
             "tracking": "while the mount tracks"}


def _desired_text(r: dict) -> str:
    if "equals" in r:
        return _g(r["equals"])
    if "one_of" in r:
        return "one of " + ", ".join(_g(x) for x in r["one_of"])
    if "min" in r and "max" in r:
        return f"{_g(r['min'])} to {_g(r['max'])}"
    if "min" in r:
        return f">= {_g(r['min'])}"
    if "max" in r:
        return f"<= {_g(r['max'])}"
    if "not_contains" in r:
        return f"not {r['not_contains']}"
    if "any_true" in r:
        return "one of " + " / ".join(p.split(".")[-1] for p in r["any_true"]) + " on"
    if r.get("report"):
        return "reported only"
    return r.get("desired") or "-"


def _c_double_focus_offsets(r, snap, config, ctx):
    p = (_section(snap, "nina1").get("settings") or {}).get("profile") or {}
    use = resolve(p, "FocuserSettings.UseFilterWheelOffsets")
    if use is None:
        return UNKNOWN, None, "NINA offsets off, or PhotonScript offsets empty", None
    ps_off = str(getattr(config, "focus_filter_offsets", "") or "").strip()
    nina_off = [f"{f.get('Name')}:{f.get('FocusOffset')}" for f in
                resolve(p, "FilterWheelSettings.FilterWheelFilters") or []
                if isinstance(f, dict) and _num(f.get("FocusOffset"))]
    cur = (f"NINA use filter offsets {_g(bool(use))}"
           + (f" ({', '.join(nina_off)})" if nina_off else "")
           + f"; PhotonScript focus_filter_offsets {ps_off or 'empty'}")
    bad = bool(use) and bool(ps_off)
    return (r["severity"] if bad else PASS), cur, \
        "NINA offsets off, or PhotonScript offsets empty", None


def _c_focuser_counter_reset(r, snap, config, ctx):
    app = r.get("app") or "nina1"
    now = _num((_fresh_live(snap, app).get("focuser") or {}).get("Position"))
    prev = _num(((ctx.get("prev_live") or {}).get(app) or {}).get("focuser", {})
                .get("Position")) if ctx.get("prev_live") else None
    want = "no drop to about 0 between readings"
    if now is None:
        return UNKNOWN, None, want, "focuser position not read now"
    cur = f"{now:g}" + (f" (was {prev:g})" if prev is not None else "")
    below, drop = float(r.get("reset_below", 1000)), float(r.get("drop_steps", 3000))
    if prev is not None and now < below and prev - now >= drop:
        return r["severity"], cur, want, f"dropped {prev - now:g} steps to {now:g}"
    return PASS, cur, want, None


def _phd2_scale(snap) -> float | None:
    s = _section(snap, "phd2").get("settings") or {}
    return _num((s.get("api") or {}).get("pixel_scale"))


def _c_dither_within_search_region(r, snap, config, ctx):
    p = (_section(snap, "nina1").get("settings") or {}).get("profile") or {}
    dither = _num(resolve(p, "GuiderSettings.DitherPixels"))
    s = _section(snap, "phd2").get("settings") or {}
    region = _num((s.get("api") or {}).get("search_region_px")
                  or (s.get("profile") or {}).get("search_region_px"))
    want = "search region >= dither pixels"
    if dither is None or region is None:
        return UNKNOWN, None, want, "NINA dither or PHD2 search region not read"
    cur = f"dither {dither:g} px, search region {region:g} px"
    return (PASS if region >= dither else r["severity"]), cur, want, None


def _c_settle_arcsec(r, snap, config, ctx):
    p = (_section(snap, "nina1").get("settings") or {}).get("profile") or {}
    px = _num(resolve(p, "GuiderSettings.SettlePixels"))
    scale = _phd2_scale(snap)
    lo, hi = _num(r.get("min_arcsec")), _num(r.get("max_arcsec"))
    want = (f"{_g(lo)} to {_g(hi)}\"" if lo is not None else f"<= {_g(hi)}\"")
    if px is None or not scale:
        return UNKNOWN, None, want, "NINA settle pixels or PHD2 scale not read"
    asec = px * scale
    if scale:
        want += f" ({(lo or 0) / scale:.1f} to {(hi or 0) / scale:.1f} px at {scale:.3g}\"/px)"
    cur = f"{px:g} px = {asec:.2f}\""
    bad = (lo is not None and asec < lo) or (hi is not None and asec > hi)
    return (r["severity"] if bad else PASS), cur, want, None


def _c_phd2_profile_matches(r, snap, config, ctx):
    p = (_section(snap, "nina1").get("settings") or {}).get("profile") or {}
    name = str(resolve(p, "GuiderSettings.GuiderName") or "")
    want = "same profile id"
    if "phd2" not in name.lower():
        return NA, name or None, want, "NINA #1 does not use PHD2"
    nina_id = _num(resolve(p, "GuiderSettings.PHD2ProfileId"))
    s = _section(snap, "phd2").get("settings") or {}
    phd2_id = _num((s.get("api") or {}).get("profile_id") or (s.get("profile") or {}).get("id"))
    if nina_id is None or phd2_id is None:
        return UNKNOWN, None, want, "a profile id not read"
    cur = f"NINA {nina_id:g}, PHD2 {phd2_id:g}"
    return (PASS if nina_id == phd2_id else r["severity"]), cur, want, None


def _c_guide_scale_matches(r, snap, config, ctx):
    s = _section(snap, "phd2").get("settings") or {}
    api = s.get("api") or {}
    phd2 = _num(api.get("pixel_scale"))
    from photonscript.telescope_agent.phd2_client import guide_scale_from_config
    try:
        mine = guide_scale_from_config(config, int(_num(api.get("binning")) or 1),
                                       api.get("camera"))
    except Exception:  # noqa: BLE001
        mine = None
    tol = float(r.get("tol_pct", 10))
    want = (f"PhotonScript {mine:.3g}\"/px (bin {_g(api.get('binning'))}) +/- {tol:g}%"
            if mine else "PhotonScript scale known")
    if phd2 is None or not mine:
        return UNKNOWN, _g(phd2) if phd2 else None, want, \
            "PHD2 scale or PhotonScript's guide scale not known"
    cur = f"PHD2 {phd2:.3g}\"/px"
    off = abs(phd2 - mine) / mine * 100.0
    return (PASS if off <= tol else r["severity"]), cur, want, \
        (None if off <= tol else f"{phd2 / mine:.2f}x PhotonScript's")


def _c_near_config(r, snap, config, ctx, absolute=False):
    s = _section(snap, r["app"]).get("settings") or {}
    v = _num(resolve(s, r.get("path") or ""))
    want_v = _num(getattr(config, r.get("config_key", ""), None))
    tol = float(r.get("tol", 0.02))
    want = f"{abs(want_v) if absolute and want_v is not None else want_v} +/- {tol:g}"
    if v is None or want_v is None:
        return UNKNOWN, None, want, "not read"
    a, b = (abs(v), abs(want_v)) if absolute else (v, want_v)
    return (PASS if abs(a - b) <= tol else r["severity"]), _g(v), want, None


def _c_near_config_abs(r, snap, config, ctx):
    return _c_near_config(r, snap, config, ctx, absolute=True)


def _c_config_matches_device(r, snap, config, ctx):
    want_v = getattr(config, r.get("config_key", ""), None)
    dev_app = r.get("device_app") or "nina1"
    s = _section(snap, dev_app).get("settings") or {}
    path = r.get("device_path") or ""
    m = re.match(r"devices\.(\w+)\.", path)
    if m and (s.get("devices") or {}).get(m.group(1), {}).get("Connected") is False:
        return NA, None, _g(want_v), "device not connected"
    v = resolve(s, path)
    if v is None or want_v is None:
        return UNKNOWN, None, _g(want_v), "not read"
    return (PASS if _eq(v, want_v) else r["severity"]), _g(v), \
        f"{_g(want_v)} ({r.get('config_key')})", None


COMPUTED = {
    "double_focus_offsets": _c_double_focus_offsets,
    "focuser_counter_reset": _c_focuser_counter_reset,
    "dither_within_search_region": _c_dither_within_search_region,
    "settle_arcsec": _c_settle_arcsec,
    "phd2_profile_matches": _c_phd2_profile_matches,
    "guide_scale_matches": _c_guide_scale_matches,
    "near_config": _c_near_config,
    "near_config_abs": _c_near_config_abs,
    "config_matches_device": _c_config_matches_device,
}


def evaluate_rule(r: dict, snap: dict, config, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    app = r.get("app")
    sec = _section(snap, app)
    settings = sec.get("settings") or {}
    out = {"id": r.get("id"), "app": app, "rig": r.get("rig"), "label": r.get("label"),
           "severity": r.get("severity"), "why": r.get("why"), "fix": r.get("fix"),
           "path": r.get("path"), "desired": _desired_text(r), "current": None,
           "note": None}

    def done(status, current=None, note=None, desired=None):
        out["status"], out["current"] = status, current
        if desired is not None:
            out["desired"] = desired
        if note:
            out["note"] = note
        if sec.get("carried") and status not in (NA,):
            tag = f"not read now; as read {sec.get('read_utc') or '?'}"
            if sec.get("source"):
                tag += f" ({sec['source']})"
            out["note"] = f"{out['note']}; {tag}" if out["note"] else tag
        return out
    if r.get("rig") == "piggyback" and not getattr(config, "piggyback_enabled", False):
        return done(NA, note="piggyback off")
    if not settings:
        return done(UNKNOWN, note=f"{APP_LABELS.get(app, app)} never read")
    for w in r.get("when") or []:
        if not _when_ok(w, snap):
            return done(NA, note=f"judged only {WHEN_TEXT.get(w, w)}")
    if r.get("computed"):
        fn = COMPUTED.get(r["computed"])
        if fn is None:
            return done(UNKNOWN, note=f"unknown computed {r['computed']}")
        try:
            st, cur, want, note = fn(r, snap, config, ctx)
        except Exception as e:  # noqa: BLE001
            return done(UNKNOWN, note=f"{type(e).__name__}: {e}")
        return done(st, cur, note, want)
    if r.get("any_true"):
        vals = [resolve(settings, p) for p in r["any_true"]]
        cur = ", ".join(f"{p.split('.')[-1]} {_g(_bool(v) if v is not None else None)}"
                        for p, v in zip(r["any_true"], vals))
        if all(v is None for v in vals):
            return done(UNKNOWN, None, "not read")
        return done(PASS if any(_bool(v) for v in vals) else r["severity"], cur)
    path = str(r.get("path") or "")
    base = (sec.get("live") or {}) if path.startswith("live.") else settings
    if path.startswith("live."):
        if sec.get("carried"):
            return done(UNKNOWN, note="live reading not taken now")
        path = path[5:]
    m = re.match(r"devices\.(\w+)\.", path)
    if m and not r.get("check_disconnected"):
        dev = (settings.get("devices") or {}).get(m.group(1)) or {}
        if dev.get("Connected") is False:
            return done(NA, note=f"{m.group(1)} not connected")
    v = resolve(base, path)
    if r.get("report"):
        return done(INFO, _g(v))
    if "not_contains" in r:
        if v is None:
            return done(UNKNOWN, None, "not read")
        items = v if isinstance(v, list) else [v]
        hit = any(_norm(x) == _norm(r["not_contains"]) for x in items)
        cur = ", ".join(map(str, items)) if items else "none"
        return done(r["severity"] if hit else PASS, cur)
    if v is None:
        return done(UNKNOWN, None, "not read")
    tol = float(r.get("tol", 1e-6))
    if "equals" in r:
        return done(PASS if _eq(v, r["equals"], tol) else r["severity"], _g(v))
    if "one_of" in r:
        return done(PASS if any(_eq(v, x, tol) for x in r["one_of"]) else r["severity"], _g(v))
    n = _num(v)
    if n is None:
        return done(UNKNOWN, _g(v), "not a number")
    if ("min" in r and n < float(r["min"])) or ("max" in r and n > float(r["max"])):
        return done(r["severity"], _g(v))
    return done(PASS, _g(v))


def evaluate(doc: dict, snap: dict, config, ctx: dict | None = None) -> dict:
    rows = [evaluate_rule(r, snap, config, ctx) for r in doc.get("rule") or []]
    rows.sort(key=lambda x: (STATUS_RANK.get(x["status"], 9), APPS.index(x["app"])
                             if x.get("app") in APPS else 9))
    counts: dict = {}
    for x in rows:
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    return {"rows": rows, "counts": counts, "rules_file": doc.get("path"),
            "rules_error": doc.get("error"), "rules_lint": doc.get("lint")}


# --------------------------------------------------------------------------
# taking a snapshot
# --------------------------------------------------------------------------

async def collect_all(config, *, collectors: dict | None = None) -> dict:
    """{app: (settings, live, meta)} for every enabled app. `collectors`
    overrides per app (tests): app -> async or plain callable(config)."""
    collectors = collectors or {}
    out: dict = {}

    async def run(app, fn):
        try:
            res = fn(config)
            if asyncio.iscoroutine(res):
                res = await asyncio.wait_for(res, 150)
            out[app] = res
        except Exception as e:  # noqa: BLE001
            out[app] = ({}, {}, {"ok": False, "note": f"{type(e).__name__}: {e}"})
    default = {
        "nina1": lambda c: collect_nina(c, c.nina_base_url),
        "nina2": lambda c: collect_nina(c, getattr(c, "piggyback_nina_base_url",
                                                   "http://localhost:1889/v2/api")),
        "phd2": collect_phd2,
        "thesky": lambda c: asyncio.to_thread(collect_thesky, c),
        "photonscript": collect_photonscript,
    }
    await asyncio.gather(*(run(a, collectors.get(a) or default[a])
                           for a in apps_enabled(config) if a in default or a in collectors))
    return out


def _merge_section(app: str, new: tuple, prev_sec: dict | None, now_z: str,
                   fallback=None) -> dict:
    """The stored section: what was read now, and for every part that could
    not be read (None, or the whole app) the previous section's part,
    marked carried."""
    settings, live, meta = new
    prev_settings = (prev_sec or {}).get("settings") or {}
    sec = {"ok": bool(meta.get("ok")), "note": meta.get("note"), "read_utc": now_z,
           "live": live or {}}
    if not meta.get("ok") or not settings:
        if prev_settings:
            sec.update(settings=prev_settings, carried=True,
                       read_utc=(prev_sec or {}).get("read_utc"),
                       source=(prev_sec or {}).get("source"))
        elif fallback:
            sec.update(settings=fallback[0], carried=True, read_utc=fallback[1],
                       source="newest stored audit")
        else:
            sec["settings"] = {}
        sec["live"] = {}
        return sec
    merged, carried = {}, []
    for k, v in settings.items():
        if v is None and k in prev_settings:
            merged[k] = prev_settings[k]
            carried.append(k)
        elif isinstance(v, dict) and isinstance(prev_settings.get(k), dict):
            sub = {}
            for k2, v2 in v.items():
                if v2 is None and prev_settings[k].get(k2) is not None:
                    sub[k2] = prev_settings[k][k2]
                    carried.append(f"{k}.{k2}")
                else:
                    sub[k2] = v2
            merged[k] = sub
        else:
            merged[k] = v
    sec["settings"] = {k: v for k, v in merged.items() if v is not None}
    if carried:
        sec["carried_parts"] = carried
    return sec


def _compact_live(apps: dict) -> dict:
    return {a: s.get("live") for a, s in apps.items() if s.get("live")}


async def take(config, reason: str = "manual", *, armer_state: str | None = None,
               collectors: dict | None = None, now: datetime | None = None,
               push: bool | None = None) -> dict:
    """Collect, store when changed, evaluate, push. Returns the index line
    plus "snapshot" (the record in force) and "rules". Never raises."""
    now = now or datetime.utcnow()
    now_z = store.iso_z(now)
    night = store.night_of(config, now)
    prev = load_latest(config)
    prev_apps = (prev or {}).get("apps") or {}
    raw = await collect_all(config, collectors=collectors)
    apps = {}
    for app in apps_enabled(config):
        if app not in raw:
            continue
        fb = _audit_fallback(config, app) if app in ("phd2", "thesky") and \
            not (prev_apps.get(app) or {}).get("settings") else None
        apps[app] = _merge_section(app, raw[app], prev_apps.get(app), now_z, fb)
    # an app switched off (or piggyback off) keeps its last section, carried
    for app, sec in prev_apps.items():
        if app not in apps:
            apps[app] = dict(sec, carried=True, live={})
    hashes = {a: digest(s.get("settings") or {}) for a, s in apps.items()}
    h = digest(hashes)
    changed = prev is None or prev.get("hash") != h
    diff = {}
    if prev is not None and changed:
        for a in sorted(set(apps) | set(prev_apps)):
            if (prev.get("hashes") or {}).get(a) != hashes.get(a):
                d = diff_settings((prev_apps.get(a) or {}).get("settings"),
                                  (apps.get(a) or {}).get("settings"))
                if d:
                    diff[a] = d
    if changed:
        sid = snapshot_id(now)
        if snap_path(config, sid, night).exists():
            sid = f"{sid}-{int(now.microsecond / 1000) or 1}"
        snap = {"id": sid, "t_utc": now_z, "night": night, "reason": reason,
                "armer_state": armer_state, "source": "live", "hash": h,
                "hashes": hashes, "prev_id": (prev or {}).get("id"), "apps": apps}
    else:
        sid = prev["id"]
        snap = dict(prev, apps={a: dict((prev_apps.get(a) or {}), live=s.get("live") or {},
                                        carried=s.get("carried", False))
                                for a, s in apps.items()},
                    armer_state=armer_state)
    # rules: on the record in force, with tonight's live readings
    prev_lines = index(config, night) or (index(config, (prev or {}).get("night") or "")
                                          if prev else [])
    prev_live = (prev_lines[-1].get("live") if prev_lines else None) or None
    eval_snap = dict(snap, apps=apps, armer_state=armer_state)
    res = evaluate(load_rules(config), eval_snap, config, {"prev_live": prev_live})
    line = {"t_utc": now_z, "reason": reason, "armer_state": armer_state,
            "id": sid, "changed": changed, "hash": h,
            "unread": sorted(a for a, s in apps.items() if s.get("carried")),
            "live": _compact_live(apps),
            "rules": {"fail": [r["id"] for r in res["rows"] if r["status"] == FAIL],
                      "warn": [r["id"] for r in res["rows"] if r["status"] == WARN]}}
    if diff:
        line["diff"] = diff
    try:
        if changed:
            _write_compact(snap_path(config, sid, night), snap)
        store.append_jsonl(index_path(config, night), line)
        store.write_json(latest_path(config), {"night": night, "id": sid,
                                               "t_utc": snap["t_utc"], "hash": h})
        st = _update_status(config, night, res, line, diff)
    except OSError as e:
        logger.warning("settings ledger not saved: %s", e)
        st = {}
    new_fails = [r for r in res["rows"] if r["status"] == FAIL
                 and r["id"] not in set(st.get("alerted_before") or [])]
    do_push = bool(getattr(config, "settings_ledger_push", True)) if push is None else push
    if new_fails and do_push and st:
        await _push(config, new_fails)
    line.update(snapshot=snap, rules=res, new_fails=[r["id"] for r in new_fails])
    return line


def _update_status(config, night: str, res: dict, line: dict, diff: dict) -> dict:
    st = load_status(config, night)
    alerted = list(st.get("alerted") or [])
    before = list(alerted)
    ever = set(st.get("ever_failed") or [])
    for r in res["rows"]:
        if r["status"] == FAIL:
            ever.add(r["id"])
            if r["id"] not in alerted:
                alerted.append(r["id"])
    last_change = dict(st.get("last_change") or {})
    for a in diff:
        last_change[a] = line["t_utc"]
    rule_change = dict(st.get("rule_status_since") or {})
    old = {r["id"]: r["status"] for r in st.get("rows") or []}
    for r in res["rows"]:
        if old.get(r["id"]) != r["status"]:
            rule_change[r["id"]] = line["t_utc"]
    new = {"night": night, "t_utc": line["t_utc"], "id": line["id"],
           "armer_state": line.get("armer_state"), "reason": line.get("reason"),
           "rows": res["rows"], "counts": res["counts"],
           "rules_file": res.get("rules_file"), "rules_error": res.get("rules_error"),
           "rules_lint": res.get("rules_lint"),
           "alerted": alerted, "ever_failed": sorted(ever),
           "last_change": last_change, "rule_status_since": rule_change,
           "unread": line.get("unread")}
    store.write_json(status_path(config, night), new)
    new["alerted_before"] = before
    return new


async def _push(config, rows: list[dict]) -> None:
    top = "; ".join(f"{r['label']} {_g(r.get('current'))} (want {r['desired']})"
                    for r in rows[:3])
    more = f" and {len(rows) - 3} more" if len(rows) > 3 else ""
    msg = (f"Settings wrong: {top}{more}. System page > Settings ledger has the "
           f"exact fixes.")
    try:
        from photonscript.shared import pushover
        await pushover.notify(config, msg, title="PhotonScript settings")
    except Exception as e:  # noqa: BLE001
        logger.warning("settings ledger push failed: %s", e)


class Snapshotter:
    """Background take() for the armer: one run at a time; requests that
    arrive while one runs are merged into one more run."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._pending: list[str] = []
        self._config = None
        self._state: str | None = None
        self.last_request: datetime | None = None
        self.last: dict | None = None

    def request(self, config, reason: str, armer_state: str | None = None) -> bool:
        loop = asyncio.get_running_loop()
        self._config, self._state = config, armer_state
        self._pending.append(reason)
        self.last_request = datetime.utcnow()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run())
        return True

    async def _run(self) -> None:
        while self._pending:
            reasons, self._pending = self._pending, []
            reason = ", ".join(dict.fromkeys(reasons))
            try:
                res = await asyncio.wait_for(
                    take(self._config, reason, armer_state=self._state), 300)
                self.last = {k: res.get(k) for k in ("t_utc", "id", "changed", "reason")}
                logger.info("settings ledger (%s): %s %s", reason, res.get("id"),
                            "changed" if res.get("changed") else "unchanged")
            except Exception as e:  # noqa: BLE001
                logger.warning("settings ledger snapshot (%s) failed: %s", reason, e)


SNAPSHOTTER = Snapshotter()


def request(config, reason: str, armer_state: str | None = None) -> bool:
    """The armer's hook: a snapshot in the background. False (and nothing
    done) while the ledger is off or no event loop runs. Never raises."""
    if not getattr(config, "settings_ledger_enabled", False):
        return False
    try:
        return SNAPSHOTTER.request(config, reason, armer_state)
    except RuntimeError:          # no running loop (sync caller, tests)
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("settings ledger request failed: %s", e)
        return False


def hourly_due(config, last: datetime | None, now: datetime | None = None) -> bool:
    mins = float(getattr(config, "settings_ledger_interval_min", 60) or 0)
    if mins <= 0:
        return False
    now = now or datetime.utcnow()
    return last is None or now - last >= timedelta(minutes=mins)


# --------------------------------------------------------------------------
# reading: at a time, for a sub, diffs
# --------------------------------------------------------------------------

def local_to_utc(config, night: str, hhmm: str) -> datetime:
    """HH:MM local on a night (noon to noon): 21:30 -> that evening, 03:10 ->
    the next morning."""
    from photonscript.shared.localtime import utc_offset_hours
    h, m = (int(x) for x in str(hhmm).strip().split(":")[:2])
    day = datetime.strptime(night, "%Y-%m-%d")
    if h < 12:
        day += timedelta(days=1)
    local = day.replace(hour=h, minute=m)
    guess = local - timedelta(hours=utc_offset_hours(config, local))
    return local - timedelta(hours=utc_offset_hours(config, guess))


def line_at(config, when_utc: datetime) -> dict | None:
    """The newest index line at or before when_utc (that night, else the
    nights before it)."""
    want = store.iso_z(when_utc)
    night = store.night_of(config, when_utc)
    for n in [x for x in reversed(nights(config)) if x <= night]:
        lines = [ln for ln in index(config, n) if str(ln.get("t_utc") or "") <= want]
        if lines:
            return lines[-1]
    return None


def at(config, when_utc: datetime) -> dict | None:
    """{"line", "snapshot"} in force at when_utc, None before the first."""
    ln = line_at(config, when_utc)
    if not ln:
        return None
    return {"line": ln, "snapshot": load_snapshot(config, ln.get("id"))}


def _sub_record(config, file: str, night: str | None = None) -> tuple[str, dict] | None:
    from photonscript.scheduler.runs import _load_subs, runs_dir
    name = Path(str(file).replace("\\", "/")).name
    if night:
        cands = [night]
    else:
        cands = sorted((p.name[:10] for p in Path(runs_dir(config)).glob("*_subs.jsonl")),
                       reverse=True)
        m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
        if m:   # the file's own date first (its night, or the night before)
            d = datetime.strptime(m.group(1), "%Y-%m-%d")
            first = [m.group(1), (d - timedelta(days=1)).strftime("%Y-%m-%d")]
            cands = first + [c for c in cands if c not in first]
    for n in cands:
        for rec in _load_subs(config, n):
            if Path(str(rec.get("file") or "")).name == name:
                return n, rec
    return None


SUB_FIELDS = ("rig", "file", "start_utc", "end_utc", "filter", "exp_s", "gain", "offset",
              "readout", "xbin", "set_temp", "setpoint_c", "ccd_temp", "focpos",
              "guide_rms", "settings_id")


def for_sub(config, file: str, night: str | None = None) -> dict:
    """The settings in force when a sub was taken: its stamped settings_id,
    else the snapshot at its start time."""
    hit = _sub_record(config, file, night)
    if not hit:
        return {"ok": False, "note": f"no sub record for {file}"}
    n, rec = hit
    sub = {k: rec.get(k) for k in SUB_FIELDS if rec.get(k) is not None}
    sid = rec.get("settings_id")
    how = "stamped on the sub record"
    line = None
    if not sid:
        from photonscript.shared.sub_time import parse_utc
        t = parse_utc(rec.get("start_utc") or rec.get("date_obs") or rec.get("time"))
        if t is None:
            return {"ok": False, "night": n, "sub": sub, "note": "sub has no time"}
        line = line_at(config, t)
        sid = (line or {}).get("id")
        how = "by capture time"
    snap = load_snapshot(config, sid) if sid else None
    if line is None and sid:
        line = next((ln for ln in reversed(index(config, night_of_id(config, sid)))
                     if ln.get("id") == sid), None)
    return {"ok": snap is not None, "night": n, "sub": sub, "id": sid, "how": how,
            "line": line, "snapshot": snap,
            "note": None if snap else "no settings snapshot before this sub"}


def last_good(config, before_night: str | None = None) -> dict | None:
    """The last snapshot of the newest earlier night on which no rule
    failed at any time ({night, id})."""
    cur = before_night or (load_latest_pointer(config) or {}).get("night") or "9999"
    for n in reversed(nights(config)):
        if n >= cur:
            continue
        st = load_status(config, n)
        if st and not st.get("ever_failed") and st.get("id"):
            return {"night": n, "id": st["id"]}
    return None


def resolve_ref(config, ref: str) -> str | None:
    """A snapshot id from: an id, "latest", "last_good", "YYYY-MM-DD"
    (that night's last), "YYYY-MM-DD@HH:MM" (local, in force then)."""
    ref = str(ref or "").strip()
    if ref in ("", "latest", "now"):
        return (load_latest_pointer(config) or {}).get("id")
    if ref in ("last_good", "good"):
        return (last_good(config) or {}).get("id")
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[@ T](\d{1,2}:\d{2}))?", ref)
    if m:
        if m.group(2):
            ln = line_at(config, local_to_utc(config, m.group(1), m.group(2)))
            return (ln or {}).get("id")
        lines = index(config, m.group(1))
        return lines[-1].get("id") if lines else None
    return ref if id_time(ref) else None


def diff(config, a: str, b: str = "latest") -> dict:
    ia, ib = resolve_ref(config, a), resolve_ref(config, b)
    sa, sb = load_snapshot(config, ia), load_snapshot(config, ib)
    if not sa or not sb:
        return {"ok": False, "a": ia, "b": ib,
                "note": f"snapshot not found: {a if not sa else b}"}
    out = {}
    for app in sorted(set(sa.get("apps") or {}) | set(sb.get("apps") or {})):
        d = diff_settings(((sa.get("apps") or {}).get(app) or {}).get("settings"),
                          ((sb.get("apps") or {}).get(app) or {}).get("settings"))
        if d:
            out[app] = d
    return {"ok": True, "a": {"id": ia, "t_utc": sa.get("t_utc"), "night": sa.get("night")},
            "b": {"id": ib, "t_utc": sb.get("t_utc"), "night": sb.get("night")},
            "diff": out, "changed": sum(len(v) for v in out.values())}


def status(config) -> dict:
    """The System page block: rule results, apps, last change times."""
    p = load_latest_pointer(config) or {}
    night = p.get("night")
    if not night:
        return {"ok": False, "enabled": bool(getattr(config, "settings_ledger_enabled", False)),
                "note": "no settings snapshot yet"}
    st = load_status(config, night)
    snap = load_snapshot(config, p.get("id")) or {}
    apps = {}
    for a, sec in (snap.get("apps") or {}).items():
        apps[a] = {"label": APP_LABELS.get(a, a), "ok": sec.get("ok"),
                   "carried": bool(sec.get("carried")), "read_utc": sec.get("read_utc"),
                   "note": sec.get("note"), "source": sec.get("source"),
                   "last_change_utc": (st.get("last_change") or {}).get(a)}
    since = st.get("rule_status_since") or {}
    rows = [dict(r, since_utc=since.get(r.get("id"))) for r in st.get("rows") or []]
    return {"ok": True, "enabled": bool(getattr(config, "settings_ledger_enabled", False)),
            "night": night, "id": p.get("id"), "snapshot_utc": snap.get("t_utc"),
            "checked_utc": st.get("t_utc"), "reason": st.get("reason"),
            "armer_state": st.get("armer_state"), "counts": st.get("counts"),
            "rows": rows, "apps": apps, "alerted": st.get("alerted"),
            "ever_failed": st.get("ever_failed"),
            "rules_file": st.get("rules_file"), "rules_lint": st.get("rules_lint"),
            "last_good": last_good(config, night), "nights": nights(config)[-30:]}


# --------------------------------------------------------------------------
# backfill
# --------------------------------------------------------------------------

def _guide_log_sections(config, night: str) -> list[tuple[str, dict]]:
    """(local session start, log_observed settings) for each PHD2 guide log
    that belongs to the night (file name date, before noon = night before)."""
    from photonscript.scheduler import phd2_audit as pa
    from photonscript.scheduler import phd2_logs as pl
    out = []
    try:
        files = pl.find_logs(config, "guide")["files"]
    except Exception:  # noqa: BLE001
        return out
    for p in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})", Path(p).name)
        if not m:
            continue
        d = datetime.strptime(m.group(1), "%Y-%m-%d")
        if int(m.group(2)) < 12:
            d -= timedelta(days=1)
        if d.strftime("%Y-%m-%d") != night:
            continue
        try:
            secs = pl.parse_guide_log(Path(p).read_text(encoding="utf-8", errors="replace"),
                                      Path(p).name)
            obs = pa.log_observed(secs, config)
        except Exception as e:  # noqa: BLE001
            logger.debug("backfill guide log %s: %s", p, e)
            continue
        if obs:
            out.append((obs.get("session_start_local") or "", obs))
    return out


def _sub_camera(rec: dict) -> dict:
    return {k: v for k, v in {"Gain": rec.get("gain"), "Offset": rec.get("offset"),
                              "ReadoutModeName": rec.get("readout"),
                              "BinX": rec.get("xbin"),
                              "TemperatureSetPoint": rec.get("set_temp",
                                                             rec.get("setpoint_c"))}.items()
            if v is not None}


def backfill_events(config, night: str) -> list[dict]:
    """Time-ordered settings events of a past night from what PhotonScript
    stored: [{t_utc, app, part, settings, live, source}]."""
    from photonscript.scheduler import phd2_audit as pa
    from photonscript.scheduler import thesky_audit as ta
    from photonscript.scheduler.phd2_analysis import to_utc
    ev: list[dict] = []
    a = pa.load_night(config, night)
    if a and a.get("t_utc"):
        ev.append({"t_utc": a["t_utc"], "app": "phd2", "settings": phd2_section_from_audit(a),
                   "source": f"PS-89 audit ({a.get('reason')})", "kind": "PS-89 PHD2 audit"})
    for start_local, obs in _guide_log_sections(config, night):
        try:
            t = store.iso_z(to_utc(config, datetime.fromisoformat(start_local[:19])))
        except (TypeError, ValueError):
            continue
        log = {k: v for k, v in obs.items() if k not in ("file", "session_start_local")}
        # the header's settings are what PHD2 guided with from then on
        api = {k: log[k] for k in PHD2_API_ROWS.values() if log.get(k) is not None}
        ev.append({"t_utc": t, "app": "phd2", "settings": {"log": log, "api": api},
                   "source": f"guide log {obs.get('file')}", "kind": "PHD2 guide log"})
    t = ta.load_night(config, night)
    if t and t.get("t_utc"):
        ev.append({"t_utc": t["t_utc"], "app": "thesky",
                   "settings": thesky_section_from_audit(t),
                   "source": f"PS-104 audit ({t.get('reason')})", "kind": "PS-104 TheSky audit"})
    try:
        from photonscript.scheduler.runs import _load_subs
        from photonscript.shared.sub_time import fmt_utc, parse_utc
        last: dict = {}
        for rec in _load_subs(config, night):
            app = "nina2" if (rec.get("rig") or "rc16") == "piggyback" else "nina1"
            cam = _sub_camera(rec)
            ts = fmt_utc(parse_utc(rec.get("start_utc") or rec.get("time")))
            if not ts:
                continue
            live = {"focuser": {"Position": rec.get("focpos")}} \
                if rec.get("focpos") is not None else {}
            if cam and cam != last.get(app):
                last[app] = cam
                ev.append({"t_utc": ts, "app": app,
                           "settings": {"devices": {"camera": cam}}, "live": live,
                           "source": f"sub {Path(str(rec.get('file'))).name}",
                           "kind": "sub records"})
            elif live:
                ev.append({"t_utc": ts, "app": app, "settings": None, "live": live,
                           "source": "sub focuser position", "kind": "sub records"})
    except Exception as e:  # noqa: BLE001
        logger.debug("backfill subs %s: %s", night, e)
    ev.sort(key=lambda e: e["t_utc"])
    return ev


BACKFILL_CANNOT = (
    "NINA #1 / #2 profiles (plate solve, autofocus, guider settle / dither, "
    "meridian flip, filter offsets): NINA keeps only the current profile and "
    "PhotonScript stored none of it before the ledger",
    "PHD2 settings the PS-89 audit does not report (AutoFind minimum SNR, the "
    "full registry profile, calibration angles) before the ledger; the guide-log "
    "headers give exposure, binning, scale, algorithms and min-moves per session",
    "TheSky before the first PS-104 audit; between audits only the arm-time "
    "reading exists (the 2026-10-12 time zone fix by hand is not in any record)",
    "PhotonScript's own config history (only the current .env)",
    "anything between two stored readings: a value changed and changed back is "
    "invisible",
)


def backfill(config, nights_list: list[str] | None = None, *, dry_run: bool = False,
             force: bool = False) -> dict:
    """Past nights from stored data, source "backfill": one snapshot per
    settings change found (the PS-89 / PS-104 audits, the guide-log headers,
    the camera settings and focuser position of every sub). A night that
    already has live ledger data is never touched; force rewrites a night
    that holds only backfilled data. Never pushes."""
    from photonscript.scheduler import phd2_audit as pa
    from photonscript.scheduler import thesky_audit as ta
    from photonscript.scheduler.runs import runs_dir
    cands = set(nights_list or [])
    if not nights_list:
        for d, pat in ((pa.audit_dir(config), "*.json"), (ta.audit_dir(config), "*.json"),
                       (Path(runs_dir(config)), "*_subs.jsonl")):
            for p in Path(d).glob(pat):
                m = re.match(r"(\d{4}-\d{2}-\d{2})", p.name)
                if m:
                    cands.add(m.group(1))
    report = {"nights": [], "skipped": [], "cannot": list(BACKFILL_CANNOT),
              "dry_run": dry_run}
    rules = load_rules(config)
    for night in sorted(cands):
        lines = index(config, night)
        if lines and (not force or any(ln.get("source") != "backfill" for ln in lines)):
            report["skipped"].append({"night": night, "why": "the ledger already has this night"
                                      + ("" if not force else " (live data: never rewritten)")})
            continue
        events = [e for e in backfill_events(config, night)
                  if store.night_of(config, store.parse_z(e["t_utc"])) == night]
        if not events:
            continue
        if not dry_run and lines:
            for p in night_dir(config, night).glob("*"):
                p.unlink()
        apps: dict = {}
        prev_h, last_id = None, None
        rec = {"night": night, "events": len(events), "snapshots": 0,
               "sources": sorted({e["kind"] for e in events})}
        ever: set = set()
        res = None
        for e in events:
            sec = apps.setdefault(e["app"], {"settings": {}, "live": {}, "ok": True,
                                             "source": "backfill"})
            if e.get("settings"):
                sec["settings"] = _deep_merge(sec["settings"], e["settings"])
                sec["read_utc"], sec["note"] = e["t_utc"], e["source"]
            hashes = {a: digest(s["settings"]) for a, s in apps.items()}
            h = digest(hashes)
            changed = h != prev_h
            sid = snapshot_id(store.parse_z(e["t_utc"]))
            if changed and last_id and sid <= last_id:
                sid = f"{last_id[:16]}-{rec['snapshots'] + 1}"
            line = {"t_utc": e["t_utc"], "reason": f"backfill: {e['source']}",
                    "armer_state": None, "id": sid if changed else last_id,
                    "changed": changed, "hash": h, "source": "backfill",
                    "live": {e["app"]: e["live"]} if e.get("live") else {}}
            if changed:
                snap = {"id": sid, "t_utc": e["t_utc"], "night": night,
                        "reason": line["reason"], "source": "backfill", "hash": h,
                        "hashes": hashes, "prev_id": last_id,
                        "apps": json.loads(canonical(apps))}
                res = evaluate(rules, dict(snap, armer_state=None), config)
                ever |= {r["id"] for r in res["rows"] if r["status"] == FAIL}
                line["rules"] = {"fail": [r["id"] for r in res["rows"] if r["status"] == FAIL],
                                 "warn": [r["id"] for r in res["rows"] if r["status"] == WARN]}
                if not dry_run:
                    _write_compact(snap_path(config, sid, night), snap)
                rec["snapshots"] += 1
                prev_h, last_id = h, sid
            if not dry_run:
                store.append_jsonl(index_path(config, night), line)
        rec["last_id"] = last_id
        rec["ever_failed"] = sorted(ever)
        report["nights"].append(rec)
        if not dry_run and res is not None:
            store.write_json(status_path(config, night), {
                "night": night, "t_utc": events[-1]["t_utc"], "id": last_id,
                "source": "backfill", "rows": res["rows"], "counts": res["counts"],
                "alerted": [], "ever_failed": sorted(ever), "last_change": {}})
    return report


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(
            out.get(k), dict) else v
    return out


# --------------------------------------------------------------------------
# text (CLI)
# --------------------------------------------------------------------------

def format_rules(rows: list[dict], show_pass: bool = False) -> str:
    lines = []
    for r in rows:
        if r["status"] in (PASS, NA) and not show_pass:
            continue
        lines.append(f"  {r['status'].upper():7} {r.get('app', ''):12} {r.get('label')}: "
                     f"{r.get('current') or '-'} (want {r.get('desired')})"
                     + (f"  [{r['note']}]" if r.get("note") else ""))
        if r["status"] in (FAIL, WARN) and r.get("fix"):
            lines.append(f"          fix: {r['fix']}")
    return "\n".join(lines) or "  (every rule passes)"


def format_snapshot(snap: dict, line: dict | None = None, *, full: bool = False) -> str:
    """A snapshot as text: per app, the settings that matter (full=True:
    every leaf)."""
    out = [f"Settings snapshot {snap.get('id')} ({snap.get('t_utc')}, night "
           f"{snap.get('night')}, {snap.get('source', 'live')}, reason {snap.get('reason')})"]
    if line and line.get("t_utc") != snap.get("t_utc"):
        out.append(f"  still in force at {line.get('t_utc')} ({line.get('reason')})")
    for app, sec in (snap.get("apps") or {}).items():
        tag = " (carried: not read then)" if sec.get("carried") else ""
        out.append(f"\n[{APP_LABELS.get(app, app)}] read {sec.get('read_utc')}{tag}")
        flat = flatten(sec.get("settings") or {})
        keys = list(flat) if full else [k for k in flat if _key_matters(app, k)]
        for k in keys:
            out.append(f"  {k} = {_g(flat[k])}")
        lv = (line or {}).get("live", {}).get(app) if line else sec.get("live")
        if lv:
            out.append(f"  live: {_g(lv)}")
    return "\n".join(out)


_MATTERS = {
    "nina1": ("profile.PlateSolveSettings.", "profile.FocuserSettings.",
              "profile.GuiderSettings.Dither", "profile.GuiderSettings.Settle",
              "profile.GuiderSettings.PHD2ProfileId", "profile.MeridianFlipSettings.",
              "profile.CameraSettings.Gain", "profile.CameraSettings.Offset",
              "profile.CameraSettings.Temperature", "profile.CameraSettings.ReadoutMode",
              "profile.FilterWheelSettings.FilterWheelFilters",
              "devices.camera.Gain", "devices.camera.Offset", "devices.camera.ReadoutModeName",
              "devices.camera.Bin", "devices.camera.TemperatureSetPoint",
              "devices.camera.CoolerOn", "devices.camera.Name",
              "devices.focuser.StepSize", "devices.focuser.Name",
              "devices.filterwheel.AvailableFilters", "devices.mount.Name",
              "devices.mount.GuideRate", "devices.safetymonitor.Connected",
              "devices.weather.Connected", "devices.guider.Connected"),
    "phd2": ("api.", "profile.min", "profile.search", "profile.exposure",
             "profile.binning", "profile.gain", "profile.pixel", "profile.focal",
             "profile.auto_load", "profile.ra_", "profile.dec_", "profile.calibration",
             "profile.mass", "silenced_alerts", "calibration_record.grade",
             "calibration_record.t_utc", "calibration_data.", "log."),
}


def _key_matters(app: str, key: str) -> bool:
    pre = _MATTERS.get(app)
    if pre is None:
        return app != "photonscript" or any(s in key for s in (
            "gain", "offset", "setpoint", "readout", "pixel", "guide_", "phd2_",
            "focus_filter", "piggyback_enabled"))
    if "FilterWheelFilters" in key:
        return key.endswith((".Name", ".FocusOffset"))
    return key.startswith(pre)
