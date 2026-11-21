"""PS-252: mount truth ledger. Catch what changes TheSky's sky mapping.

2026-11-12 00:43:51 to 00:44:12 MST TheSky slewed the RC16 out of park to
M33 Core P3 and reported arriving on it (Dec +30.68). The first Center solve
at 00:49:07 put the tube 25.6 deg further north. TheSky's position stayed
offset by that much (Dec -25d34m, RA -1m29s) for the rest of the night; every
Center hid it with an offset re-slew, and the dawn park later failed. On
other nights the first-try error was 18' (10-15), 8 deg in RA (10-18), about
1 deg (10-23 / 24) and 16 to 19' (11-11 evening). The mount has absolute
encoders (FindHome reports 228 "not supported"); a TCS reboot clears the
"mount cannot slew" state. Nothing logged a sync, location or time push.

The PS-67 mount log (runs/<night>_mount.jsonl, NINA #1's mount poll) already
records where the mount says it is. This ledger adds what that log cannot
see, one JSON line each, in <data_dir>/mount/<night>_ledger.jsonl:

  sample   TheSky's own read (mount_truth: position, parked / tracking /
           slew complete, LastSlewError, site, time zone, DST index, use
           computer clock, JD, LST) with clock_skew_s (TheSky's JD against
           this PC's UTC) and lst_skew_s (TheSky's LST against the LST
           computed from this PC's UTC and observatory_lon: a time zone,
           DST, clock or longitude error shows here). Every poll_s while
           unparked, every PARKED_EVERY_S while parked, and on every event.
  event    what changed since the last sample: connect / disconnect, park /
           unpark, tracking on / off, slew start / end, a new LastSlewError,
           a site / time zone / DST / clock-mode change, a clock or LST skew
           crossing its limit, an ImagingSystem.ini rewrite (key diff, and a
           copy of each version under mount/ini/).
  center   every NINA #1 "Centering Solver" line: the scope position
           (JNOW, precessed to J2000) against the solve. mapping_east /
           mapping_north (arcmin) = where TheSky says the mount points minus
           where the camera really points: TheSky's mapping error, measured
           at every Center. A change of more than alert_arcmin from the
           previous Center is a mapping_jump event.
  nina     NINA #1 mount lines: ERROR / WARNING lines about the telescope,
           and Sync / location / UTCDate / park / unpark / FindHome lines.

mapping_jump, slew_error, site_change, lst_skew and clock_skew are
incidents: the last INCIDENT_MIN minutes of this ledger and of the PS-67
mount log are frozen to mount/incidents/<night>_<HHMMSS>_<kind>.json and, in
mode alert, one Pushover (priority 1 for a mapping jump) names the slews,
parks and NINA lines inside the window.

mount_ledger_mode: alert (default) | observe (ledger only) | off.
Reads only: PhotonScript never syncs, parks, slews or changes a setting here.
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

logger = logging.getLogger(__name__)

MODES = ("alert", "observe", "off")
PARKED_EVERY_S = 600.0
CLOCK_SKEW_S = 2.0
LST_SKEW_S = 5.0
INCIDENT_MIN = 15.0
CENTER_SCAN_BYTES = 4 * 1024 * 1024
NINA_LINES_MAX = 60
SITE_KEYS = ("latitude", "longitude", "time_zone", "elevation_m", "dst_index",
             "use_computer_clock")
INCIDENT_KINDS = ("mapping_jump", "slew_error", "site_change", "lst_skew",
                  "clock_skew")
INI_NAME = "ImagingSystem.ini"

_NINA_LEVEL = re.compile(r"\|(ERROR|WARNING)\|", re.I)
_NINA_MOUNTISH = re.compile(r"telescope|mount|slew|park|ascom", re.I)
_NINA_PUSH = re.compile(r"\bsync|sitelatitude|sitelongitude|siteelevation|utcdate|"
                        r"location|unpark|commanded to park|has parked|findhome|"
                        r"setpark", re.I)


# ------------------------------------------------------------------ config

def mode(cfg) -> str:
    m = str(getattr(cfg, "mount_ledger_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def _f(cfg, name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(getattr(cfg, name, default))
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v)) if math.isfinite(v) else default


def poll_s(cfg) -> float:
    return _f(cfg, "mount_ledger_poll_s", 15.0, 5.0, 600.0)


def slew_poll_s(cfg) -> float:
    return _f(cfg, "mount_ledger_slew_poll_s", 3.0, 1.0, 60.0)


def alert_arcmin(cfg) -> float:
    return _f(cfg, "mount_ledger_alert_arcmin", 5.0, 0.5, 600.0)


# ------------------------------------------------------------------ paths

def ledger_dir(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "mount"


def ledger_path(cfg, night: str) -> Path:
    return ledger_dir(cfg) / f"{night}_ledger.jsonl"


def incidents_dir(cfg) -> Path:
    return ledger_dir(cfg) / "incidents"


def _night(cfg, now: datetime) -> str:
    from photonscript.shared.phd2_store import night_of
    return night_of(cfg, now)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


# ------------------------------------------------------------------ astronomy

def julian_date(dt_utc: datetime) -> float:
    return dt_utc.timestamp() / 86400.0 + 2440587.5 if dt_utc.tzinfo else \
        (dt_utc - datetime(1970, 1, 1)).total_seconds() / 86400.0 + 2440587.5


def lst_hours(dt_utc: datetime, lon_east_deg: float) -> float:
    """Local mean sidereal time (hours) from UTC and an east-positive
    longitude (AARO is -109.02)."""
    d = julian_date(dt_utc) - 2451545.0
    gmst = (280.46061837 + 360.98564736629 * d) % 360.0
    return ((gmst + lon_east_deg) % 360.0) / 15.0


def _wrap_h(h: float) -> float:
    return ((h + 12.0) % 24.0) - 12.0


def precess(ra_deg: float, dec_deg: float, jd_from: float, jd_to: float) -> tuple[float, float]:
    """IAU 1976 precession of mean coordinates (degrees) between epochs."""
    T = (jd_from - 2451545.0) / 36525.0
    t = (jd_to - jd_from) / 36525.0
    a = math.radians
    zeta = a(((2306.2181 + 1.39656 * T - 0.000139 * T * T) * t
              + (0.30188 - 0.000344 * T) * t * t + 0.017998 * t ** 3) / 3600.0)
    z = a(((2306.2181 + 1.39656 * T - 0.000139 * T * T) * t
           + (1.09468 + 0.000066 * T) * t * t + 0.018203 * t ** 3) / 3600.0)
    th = a(((2004.3109 - 0.85330 * T - 0.000217 * T * T) * t
            - (0.42665 + 0.000217 * T) * t * t - 0.041833 * t ** 3) / 3600.0)
    ra, dec = a(ra_deg), a(dec_deg)
    A = math.cos(dec) * math.sin(ra + zeta)
    B = math.cos(th) * math.cos(dec) * math.cos(ra + zeta) - math.sin(th) * math.sin(dec)
    C = math.sin(th) * math.cos(dec) * math.cos(ra + zeta) + math.cos(th) * math.sin(dec)
    ra2 = (math.degrees(math.atan2(A, B) + z)) % 360.0
    dec2 = math.degrees(math.asin(max(-1.0, min(1.0, C))))
    return ra2, dec2


def east_north_arcmin(ra_from, dec_from, ra_to, dec_to) -> tuple[float, float]:
    """(east, north) arcmin of `to` relative to `from` (degrees)."""
    dra = ((ra_to - ra_from + 180.0) % 360.0) - 180.0
    return (dra * math.cos(math.radians((dec_from + dec_to) / 2.0)) * 60.0,
            (dec_to - dec_from) * 60.0)


# ------------------------------------------------------------------ TheSky

def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _flag(v):
    if v is None or v == "":
        return None
    return str(v).strip().lower() in ("1", "true", "-1", "yes")


def parse_truth(kv: dict | None) -> dict | None:
    """mount_truth's parsed reply -> typed dict, None when TheSky gave
    nothing."""
    if not isinstance(kv, dict) or not kv:
        return None
    out = {"connected": _flag(kv.get("connected"))}
    for k in ("parked", "tracking", "slew_complete", "use_computer_clock"):
        out[k] = _flag(kv.get(k))
    for k in ("last_slew_error", "ra_h", "dec_d", "az", "alt", "latitude",
              "longitude", "time_zone", "elevation_m", "dst_index", "jd_now", "lst_h"):
        out[k] = _num(kv.get(k))
    return out


def read_thesky(cfg) -> dict | None:
    from photonscript.telescope_agent.thesky_client import TheSkyClient
    try:
        cl = TheSkyClient(getattr(cfg, "thesky_tcp_host", "localhost"),
                          int(getattr(cfg, "thesky_tcp_port", 3040) or 3040),
                          timeout=5.0)
        return parse_truth(cl.mount_truth())
    except Exception as e:  # noqa: BLE001
        logger.debug("mount ledger: TheSky read failed: %s", e)
        return None


def derive(cfg, m: dict, now: datetime) -> dict:
    """clock_skew_s, lst_skew_s and the hour angle from a TheSky read."""
    out: dict = {}
    jd = m.get("jd_now")
    if jd is not None:
        out["clock_skew_s"] = round((jd - julian_date(now)) * 86400.0, 2)
    lon = _num(getattr(cfg, "observatory_lon", None))
    lst = m.get("lst_h")
    if lon is not None and lst is not None:
        out["lst_calc_h"] = round(lst_hours(now, lon), 6)
        out["lst_skew_s"] = round(_wrap_h(lst - out["lst_calc_h"]) * 3600.0, 1)
    if lst is not None and m.get("ra_h") is not None:
        out["ha_h"] = round(_wrap_h(lst - m["ra_h"]), 5)
    return out


def diff_events(prev: dict | None, cur: dict | None, der: dict, pder: dict) -> list[dict]:
    """What changed between two TheSky reads (typed by parse_truth)."""
    ev: list[dict] = []
    if cur is None:
        if prev is not None:
            ev.append({"what": "thesky_unreachable"})
        return ev
    if prev is None:
        ev.append({"what": "start"})
        return ev
    pairs = (("connected", "connect", "disconnect"), ("parked", "park", "unpark"),
             ("tracking", "tracking_on", "tracking_off"),
             ("slew_complete", "slew_end", "slew_start"))
    for k, on, off in pairs:
        a, b = prev.get(k), cur.get(k)
        if a is not None and b is not None and a != b:
            ev.append({"what": on if b else off})
    lse = cur.get("last_slew_error")
    if lse is not None and lse != prev.get("last_slew_error") and lse not in (0, 0.0):
        ev.append({"what": "slew_error", "code": int(lse)})
    changed = {k: [prev.get(k), cur.get(k)] for k in SITE_KEYS
               if prev.get(k) is not None and cur.get(k) is not None
               and abs(prev[k] - cur[k]) > 1e-6}
    if changed:
        ev.append({"what": "site_change", "changed": changed})
    for key, lim, what in (("clock_skew_s", CLOCK_SKEW_S, "clock_skew"),
                           ("lst_skew_s", LST_SKEW_S, "lst_skew")):
        a, b = pder.get(key), der.get(key)
        if b is not None and abs(b) > lim and (a is None or abs(a) <= lim):
            ev.append({"what": what, "value_s": b, "limit_s": lim})
        elif b is not None and a is not None and abs(a) > lim and abs(b) <= lim:
            ev.append({"what": what + "_ok", "value_s": b})
    return ev


# ------------------------------------------------------------------ NINA log

_RE_SCOPE_EPOCH = None


def _scope_re():
    global _RE_SCOPE_EPOCH
    if _RE_SCOPE_EPOCH is None:
        from photonscript.scheduler import nina_center_log as ncl
        _RE_SCOPE_EPOCH = re.compile(r"Scope Position:\s*" + ncl._COORD
                                     + r"\s*;?\s*(?:Epoch:\s*(\w+))?", re.I)
    return _RE_SCOPE_EPOCH


def center_mapping(cfg, line: str) -> dict | None:
    """One "Centering Solver" line -> {t, scope / solved / target J2000,
    mapping_east, mapping_north, mapping_arcmin} or None."""
    from photonscript.scheduler import nina_center_log as ncl
    recs = ncl.parse(line)
    if not recs or recs[0].get("solved_ra") is None:
        return None
    r = recs[0]
    m = _scope_re().search(line)
    if not m:
        return None
    s_ra, s_dec = ncl._sexa(m.group(1)), ncl._sexa(m.group(2))
    if s_ra is None or s_dec is None:
        return None
    t_local = datetime.fromisoformat(r["t_local"])
    t_utc = ncl._to_utc(cfg, t_local)
    s_ra, epoch = s_ra * 15.0, (m.group(3) or "JNOW").upper()
    if epoch != "J2000":
        s_ra, s_dec = precess(s_ra, s_dec, julian_date(t_utc), 2451545.0)
    e, n = east_north_arcmin(r["solved_ra"], r["solved_dec"], s_ra, s_dec)
    return {"t": _iso(t_utc), "t_local": r["t_local"],
            "scope_ra": round(s_ra, 5), "scope_dec": round(s_dec, 5),
            "solved_ra": r["solved_ra"], "solved_dec": r["solved_dec"],
            "target_ra": r["target_ra"], "target_dec": r["target_dec"],
            "sep_arcmin": r["sep_arcmin"], "pier": r.get("pier"),
            "mapping_east": round(e, 2), "mapping_north": round(n, 2),
            "mapping_arcmin": round(math.hypot(e, n), 2)}


def nina_mount_line(line: str) -> bool:
    if "centering solver" in line.lower():
        return False
    if _NINA_LEVEL.search(line) and _NINA_MOUNTISH.search(line):
        return True
    return bool(_NINA_PUSH.search(line))


def rc16_log(cfg, night: str) -> Path | None:
    try:
        from photonscript.scheduler.routers.triage import _rig_night_logs
        logs, _err = _rig_night_logs(cfg, "rc16", night)
        return logs[-1] if logs else None
    except Exception as e:  # noqa: BLE001
        logger.debug("mount ledger: no RC16 NINA log: %s", e)
        return None


def scan_nina(cfg, path: Path, since_local: str | None,
              max_bytes: int = CENTER_SCAN_BYTES) -> tuple[list[dict], list[dict], str | None]:
    """New lines of the RC16 NINA log after `since_local` (ISO local time;
    None = the last max_bytes). Returns (centers, nina lines, newest t)."""
    from photonscript.scheduler.log_files import iter_reverse
    from photonscript.scheduler import nina_center_log as ncl
    centers, others, newest = [], [], None
    for line in iter_reverse(path, max_bytes):
        m = ncl._TS.match(line)
        if not m:
            continue
        t = m.group(1)[:26]
        if since_local is not None and t <= since_local:
            break
        newest = newest or t
        low = line.lower()
        if "centering solver" in low:
            c = center_mapping(cfg, line)
            if c:
                centers.append(c)
        elif nina_mount_line(line) and len(others) < NINA_LINES_MAX:
            try:
                t_utc = ncl._to_utc(cfg, datetime.fromisoformat(t))
            except ValueError:
                continue
            others.append({"t": _iso(t_utc), "t_local": t[:19],
                           "level": m.group(2), "line": line[:400]})
    centers.reverse()
    others.reverse()
    return centers, others, newest


# ------------------------------------------------------------------ ini watch

def ini_path(cfg) -> Path | None:
    try:
        from photonscript.scheduler.tpoint_files import roots
        for r in roots(cfg):
            p = Path(r) / INI_NAME
            if p.is_file():
                return p
    except Exception:  # noqa: BLE001
        pass
    return None


def ini_keys(text: str) -> dict[str, str]:
    out, sect = {}, ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            sect = line[1:-1]
        elif "=" in line and not line.startswith((";", "#")):
            k, _, v = line.partition("=")
            out[f"{sect}/{k.strip()}" if sect else k.strip()] = v.strip()
    return out


def ini_diff(old: dict, new: dict, cap: int = 40) -> dict:
    ch = {k: [old.get(k), new.get(k)] for k in sorted(set(old) | set(new))
          if old.get(k) != new.get(k)}
    return dict(list(ch.items())[:cap])


def check_ini(cfg, state: dict, now: datetime) -> dict | None:
    """An ini_change event when ImagingSystem.ini changed since the last
    look (the first look only records it); a copy of each version is kept."""
    p = ini_path(cfg)
    if p is None:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    sig = (st.st_mtime, st.st_size)
    if state.get("ini_sig") == sig:
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    text = raw.decode("latin-1")
    keys = ini_keys(text)
    digest = hashlib.sha1(raw).hexdigest()[:12]
    first = "ini_sig" not in state
    old = state.get("ini_keys") or {}
    state.update(ini_sig=sig, ini_keys=keys)
    if not first and state.get("ini_digest") == digest:
        return None
    state["ini_digest"] = digest
    try:
        d = ledger_dir(cfg) / "ini"
        d.mkdir(parents=True, exist_ok=True)
        stamp = (datetime(1970, 1, 1) + timedelta(seconds=st.st_mtime)).strftime("%Y%m%dT%H%M%SZ")
        out = d / f"{stamp}_{digest}_{INI_NAME}"
        if not out.exists():
            out.write_bytes(raw)
    except OSError as e:
        logger.debug("mount ledger: ini copy failed: %s", e)
    if first:
        return {"what": "ini_seen", "file": str(p), "digest": digest,
                "mtime": _iso((datetime(1970, 1, 1) + timedelta(seconds=st.st_mtime)))}
    return {"what": "ini_change", "file": str(p), "digest": digest,
            "mtime": _iso((datetime(1970, 1, 1) + timedelta(seconds=st.st_mtime))),
            "changed": ini_diff(old, keys)}


# ------------------------------------------------------------------ storage

def append(cfg, night: str, rec: dict) -> None:
    try:
        from photonscript.shared.phd2_store import append_jsonl
        p = ledger_path(cfg, night)
        p.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(p, rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("mount ledger line not written: %s", e)


def load(cfg, night: str) -> list[dict]:
    try:
        from photonscript.shared.phd2_store import read_jsonl
        return read_jsonl(ledger_path(cfg, night))
    except Exception:  # noqa: BLE001
        return []


def _window(rows: list[dict], start: datetime, end: datetime) -> list[dict]:
    a, b = _iso(start), _iso(end)
    return [r for r in rows if a <= str(r.get("t", "")) <= b]


def incident(cfg, night: str, kind: str, now: datetime, detail: dict) -> Path | None:
    """Freeze the last INCIDENT_MIN minutes of this ledger and the PS-67
    mount log to an incident file."""
    start = now - timedelta(minutes=INCIDENT_MIN)
    if kind == "mapping_jump" and detail.get("prev_t"):
        try:
            prev_t = datetime.fromisoformat(str(detail["prev_t"]).rstrip("Z"))
            start = min(start, prev_t - timedelta(minutes=2))
        except ValueError:
            pass
    mount_rows: list[dict] = []
    try:
        from photonscript.shared.mount_log import log_path
        from photonscript.shared.phd2_store import read_jsonl
        mount_rows = _window(read_jsonl(log_path(cfg, night)), start, now)
    except Exception:  # noqa: BLE001
        pass
    rep = {"kind": kind, "t": _iso(now), "night": night, "detail": detail,
           "window": [_iso(start), _iso(now)],
           "ledger": _window(load(cfg, night), start, now),
           "mount_log": mount_rows}
    try:
        d = incidents_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{night}_{now.strftime('%H%M%S')}_{kind}.json"
        p.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
        return p
    except OSError as e:
        logger.warning("mount ledger incident not written: %s", e)
        return None


def _hms_local(cfg, t) -> str:
    try:
        from photonscript.shared.localtime import to_local
        return to_local(cfg, datetime.fromisoformat(str(t).rstrip("Z"))).strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(t or "")[11:19]


def between_summary(cfg, rows: list[dict]) -> str:
    """'slew_start 00:43:51, slew_end 00:44:12, NINA ERROR 00:45:02' (local
    times) for a push."""
    bits = []
    for r in rows:
        k = r.get("kind")
        if k == "event" and r.get("what") not in ("start", "mapping_jump"):
            bits.append(f"{r.get('what')} {_hms_local(cfg, r.get('t'))}")
        elif k == "nina":
            bits.append(f"NINA {r.get('level')} {_hms_local(cfg, r.get('t'))}")
    return ", ".join(bits[-12:]) or "no slew, park or NINA mount line logged"


def incident_text(kind: str, detail: dict, between: str) -> str:
    if kind == "mapping_jump":
        return (f"Mount mapping moved {detail.get('delta_arcmin')}' between Centers "
                f"(TheSky now says the mount is {detail.get('mapping_east')}' E, "
                f"{detail.get('mapping_north')}' N of where the camera points; was "
                f"{detail.get('prev_east')}' E, {detail.get('prev_north')}' N). "
                f"In between: {between}. Imaging is still centered by offset; a "
                "park or limit check now uses the wrong position. A TCS reboot "
                "(Bisque TCS > Utilities > Reboot Normally) clears it.")
    if kind == "slew_error":
        return f"TheSky LastSlewError {detail.get('code')}. Recent: {between}."
    if kind == "site_change":
        return f"TheSky site / time zone / clock setting changed: {detail.get('changed')}."
    if kind in ("lst_skew", "clock_skew"):
        return (f"TheSky's {'sidereal time' if kind == 'lst_skew' else 'clock'} is "
                f"{detail.get('value_s')} s off this PC's UTC (limit "
                f"{detail.get('limit_s')} s): check TheSky's time zone, DST and "
                "computer-clock settings.")
    return f"Mount ledger: {kind}"


# ------------------------------------------------------------------ tick

_STATE: dict = {}


async def tick(cfg, armer_state: str = "", now: datetime | None = None,
               read=None, nina_scan=None, notify=None, state: dict | None = None) -> dict:
    """One ledger step. read(cfg) -> parse_truth dict or None,
    nina_scan(cfg, night, since_local) -> (centers, nina, newest) and notify
    are injectable for tests. Returns {"state", "events", "centers", ...}."""
    now = now or datetime.utcnow()
    st = state if state is not None else _STATE
    md = mode(cfg)
    if md == "off":
        return {"state": "off"}
    night = _night(cfg, now)
    if st.get("night") != night:
        st.update(night=night, center_since=None, mapping=None, last_sample=None)
    cur = (read or (lambda c: asyncio.to_thread(read_thesky, c)))(cfg)
    if asyncio.iscoroutine(cur):
        cur = await cur
    der = derive(cfg, cur, now) if cur else {}
    prev, pder = st.get("prev"), st.get("prev_der") or {}
    events = diff_events(prev, cur, der, pder)
    ini_ev = check_ini(cfg, st, now)
    if ini_ev:
        events.append(ini_ev)
    st["prev"], st["prev_der"] = cur, der

    rows: list[dict] = []
    t = _iso(now)
    for e in events:
        rows.append({"t": t, "kind": "event", **e})

    # NINA #1's log: Centers (mapping error) and mount lines
    centers, nina = [], []
    try:
        if nina_scan is not None:
            centers, nina, newest = nina_scan(cfg, night, st.get("center_since"))
        else:
            path = rc16_log(cfg, night)
            centers, nina, newest = (await asyncio.to_thread(
                scan_nina, cfg, path, st.get("center_since")) if path else ([], [], None))
        backfill = st.get("center_since") is None
        if newest:
            st["center_since"] = newest
        elif backfill:   # nothing yet tonight: later lines are live
            from photonscript.shared.localtime import to_local
            st["center_since"] = to_local(cfg, now).isoformat(timespec="microseconds")
    except Exception as e:  # noqa: BLE001
        logger.debug("mount ledger: NINA scan failed: %s", e)
        backfill, newest = False, None
    for n in nina:
        rows.append({"kind": "nina", **n})
    incidents: list[tuple[str, dict]] = []
    for c in centers:
        rec = {"kind": "center", **c}
        if backfill:
            rec["backfill"] = True
        pm = st.get("mapping")
        if pm is not None:
            de = c["mapping_east"] - pm["mapping_east"]
            dn = c["mapping_north"] - pm["mapping_north"]
            delta = round(math.hypot(de, dn), 2)
            rec["delta_arcmin"] = delta
            if delta > alert_arcmin(cfg) and not backfill:
                jump = {"what": "mapping_jump", "delta_arcmin": delta,
                        "mapping_east": c["mapping_east"], "mapping_north": c["mapping_north"],
                        "prev_east": pm["mapping_east"], "prev_north": pm["mapping_north"],
                        "prev_t": pm["t"], "center_t": c["t"]}
                rows.append({"t": c["t"], "kind": "event", **jump})
                incidents.append(("mapping_jump", jump))
        st["mapping"] = c
        rows.append(rec)
    for e in events:
        if e["what"] in INCIDENT_KINDS:
            incidents.append((e["what"], e))

    # the TheSky sample line
    parked = bool(cur and cur.get("parked"))
    last = st.get("last_sample")
    due = (last is None or events or
           (now - last).total_seconds() >= (PARKED_EVERY_S if parked or not cur else 0))
    if due:
        rows.insert(0, {"t": t, "kind": "sample", "armer": armer_state or None,
                        "thesky": cur, **der})
        st["last_sample"] = now
    rows.sort(key=lambda r: str(r.get("t", "")))
    for r in rows:
        append(cfg, night, r)

    sent = []
    for kind, det in incidents:
        p = incident(cfg, night, kind, now, det)
        if md != "alert":
            continue
        key = (night, kind, now.strftime("%Y%m%d%H"))
        if key in st.setdefault("alerted", set()):
            continue
        st["alerted"].add(key)
        start = now - timedelta(minutes=INCIDENT_MIN)
        if det.get("prev_t"):
            try:
                start = datetime.fromisoformat(str(det["prev_t"]).rstrip("Z"))
            except ValueError:
                pass
        between = between_summary(cfg, _window(load(cfg, night), start, now))
        msg = incident_text(kind, det, between)
        if p is not None:
            msg += f" Incident: mount/incidents/{p.name}"
        if notify is None:
            from photonscript.shared.pushover import notify as _notify
            notify = _notify
        try:
            await notify(cfg, msg, title="PhotonScript mount truth",
                         priority=1 if kind == "mapping_jump" else 0,
                         kind="act", safety=kind == "mapping_jump")
            sent.append(kind)
        except Exception as e:  # noqa: BLE001
            logger.warning("mount ledger push not sent: %s", e)
    return {"state": "ok", "night": night, "events": [e["what"] for e in events],
            "centers": len(centers), "nina": len(nina),
            "incidents": [k for k, _ in incidents], "sent": sent,
            "slewing": bool(cur and cur.get("slew_complete") is False)}


def next_sleep(cfg, res: dict) -> float:
    if res.get("slewing"):
        return slew_poll_s(cfg)
    return poll_s(cfg)


async def run_mount_ledger(get_config, get_armer) -> None:
    """Background loop (app startup). Never raises, never moves the mount."""
    while True:
        cfg = get_config()
        res: dict = {}
        try:
            res = await tick(cfg, str(getattr(get_armer(), "state", "") or ""))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("mount ledger tick failed: %s", e)
        await asyncio.sleep(next_sleep(cfg, res) if res.get("state") != "off" else 300.0)


# ------------------------------------------------------------------ API view

def view(cfg, night: str, tail: int = 300, kind: str = "") -> dict:
    rows = load(cfg, night)
    kinds = {k.strip() for k in (kind or "").split(",") if k.strip()}
    sel = [r for r in rows if not kinds or r.get("kind") in kinds]
    centers = [r for r in rows if r.get("kind") == "center"]
    samples = [r for r in rows if r.get("kind") == "sample"]
    try:
        inc = sorted(p.name for p in incidents_dir(cfg).glob(f"{night}_*.json"))
    except OSError:
        inc = []
    return {"night": night, "file": str(ledger_path(cfg, night)), "count": len(rows),
            "lines": sel[-max(1, min(int(tail or 300), 5000)):],
            "latest": samples[-1] if samples else None,
            "mapping": centers[-1] if centers else None,
            "mapping_series": [{k: c.get(k) for k in ("t", "mapping_east", "mapping_north",
                                                       "mapping_arcmin", "pier")}
                               for c in centers],
            "incidents": inc}
