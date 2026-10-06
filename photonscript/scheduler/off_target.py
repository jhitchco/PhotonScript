"""PS-143: off-target alert for the live "Where is it" panel (PS-64 follow-up).

The panel (where_panel.collect) shows how far the RC16 points from where
tonight's plan wants it, and a background loop (run_monitor, started at app
startup) alerts when that stays too far while imaging:

  expected   the running target's project coordinates (J2000). A PS-111
             mosaic panel is its own project, so the panel's center is
             used. A Piggy-600-driven target (PS-26) in piggy_center_mode
             "on" uses the shifted RC16 center for the mount's pier side
             (piggy_offset.center_plan), so a deliberate shift is not an
             alarm.
  measured   the mount position from NINA #1 (ninaAPI mount info, RA hours
             and Dec in the mount's own epoch: the expected point is
             precessed to the date when the mount reports JNow, see
             off_target_mount_epoch) and, when one is fresh, the latest RC16
             plate solve from solve_store (<data_dir>/solves/<night>/rc16.jsonl,
             J2000, PS-67 / PS-96). A fresh solve wins: it is free of the
             pointing model error.
  imaging    armer RUNNING or WATCHING (not paused), NINA #1's running leaf
             is an exposure and nothing on the running path is a slew,
             center, autofocus, flat, dark, bias, calibration, tracking test
             or meridian flip, and the mount is not slewing.

Alert (OffTargetMonitor): separation > off_target_arcmin for more than
off_target_subs consecutive subs, or for off_target_minutes, while imaging.
Any non-imaging read or a target change restarts the streak. One Pushover per
target per night (priority 1), the panel chip turns red while the condition
holds, events kind "off_target" (alert / clear) in runs/<night>_events.jsonl.
Observe only: nothing is sent to NINA. off_target_mode: alert (default) |
panel (show, never push) | off.

The geometry helpers are pure (tested offline); every live read fails soft.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)

MODES = ("alert", "panel", "off")
EPOCHS = ("auto", "jnow", "j2000")
MONITOR_TICK_S = 30
# a running path containing any of these is not "imaging" (lower case)
NOT_IMAGING = ("slew", "center", "autofocus", "auto focus", "run autofocus",
               "flat", "dark", "bias", "calibration", "tracking test",
               "meridian", "flip", "unsafe", "park")


# ------------------------------------------------------------------ config

def mode(cfg) -> str:
    m = str(getattr(cfg, "off_target_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def _f(cfg, key, default) -> float:
    try:
        v = float(getattr(cfg, key, default))
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def limits(cfg) -> dict:
    return {"arcmin": _f(cfg, "off_target_arcmin", 10.0),
            "subs": int(_f(cfg, "off_target_subs", 2)),
            "minutes": _f(cfg, "off_target_minutes", 5.0),
            "solve_max_age_min": _f(cfg, "off_target_solve_max_age_min", 15.0)}


# ------------------------------------------------------------------ geometry

def _wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def separation(ra1_deg, dec1_deg, ra2_deg, dec2_deg) -> dict | None:
    """Point 2 seen from point 1: {"arcmin" (great circle, haversine),
    "east" (RA difference wrapped to +/-180 deg times cos(mean Dec),
    arcmin, + = point 2 east), "north" (arcmin), "pa", "dir"}. None when an
    input is missing."""
    if None in (ra1_deg, dec1_deg, ra2_deg, dec2_deg):
        return None
    r1, d1, r2, d2 = map(math.radians, (float(ra1_deg), float(dec1_deg),
                                        float(ra2_deg), float(dec2_deg)))
    h = (math.sin((d2 - d1) / 2) ** 2
         + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2) ** 2)
    sep = math.degrees(2 * math.asin(min(1.0, math.sqrt(h)))) * 60.0
    dra = _wrap180(float(ra2_deg) - float(ra1_deg))
    east = dra * math.cos((d1 + d2) / 2) * 60.0
    north = (float(dec2_deg) - float(dec1_deg)) * 60.0
    from photonscript.shared.pointing import bearing
    pa, word = bearing(float(ra1_deg), float(dec1_deg), float(ra2_deg),
                       float(dec2_deg))
    return {"arcmin": round(sep, 2), "east": round(east, 2),
            "north": round(north, 2), "pa": pa, "dir": word}


def precess_from_j2000(ra_deg: float, dec_deg: float, when: datetime) -> tuple[float, float]:
    """J2000 mean place -> mean place of the date (IAU 1976 precession,
    Meeus 21.2 / 21.4). Nutation and aberration (under 1') are left out:
    this is for a 10' alarm, not for pointing."""
    jd = 2451545.0 + (when.replace(tzinfo=None)
                      - datetime(2000, 1, 1, 12, 0, 0)).total_seconds() / 86400.0
    t = (jd - 2451545.0) / 36525.0
    zeta = math.radians((2306.2181 * t + 0.30188 * t * t + 0.017998 * t ** 3) / 3600.0)
    z = math.radians((2306.2181 * t + 1.09468 * t * t + 0.018203 * t ** 3) / 3600.0)
    th = math.radians((2004.3109 * t - 0.42665 * t * t - 0.041833 * t ** 3) / 3600.0)
    a0, d0 = math.radians(ra_deg), math.radians(dec_deg)
    a = math.cos(d0) * math.sin(a0 + zeta)
    b = (math.cos(th) * math.cos(d0) * math.cos(a0 + zeta)
         - math.sin(th) * math.sin(d0))
    c = (math.sin(th) * math.cos(d0) * math.cos(a0 + zeta)
         + math.cos(th) * math.sin(d0))
    ra = math.degrees(math.atan2(a, b) + z) % 360.0
    dec = math.degrees(math.asin(max(-1.0, min(1.0, c))))
    return ra, dec


def mount_epoch(cfg, reported: str | None) -> str:
    """'JNow' | 'J2000' for the mount position: off_target_mount_epoch
    jnow / j2000 forces it; auto (default) takes what NINA reports and
    assumes JNow (the Paramount's TheSky ASCOM driver) when it says
    nothing."""
    m = str(getattr(cfg, "off_target_mount_epoch", "auto") or "auto").strip().lower()
    if m == "jnow":
        return "JNow"
    if m == "j2000":
        return "J2000"
    return "J2000" if str(reported or "").upper() == "J2000" else "JNow"


# ------------------------------------------------------------------ inputs

def is_imaging(path: list | None, leaf: str | None, slewing=None,
               target: str | None = None) -> tuple[bool, str]:
    """(imaging, reason) from NINA #1's running path (container names, root
    to leaf), the mount's Slewing flag and the running target (its own
    containers are skipped, so a "Dark Shark Nebula" is not a dark)."""
    if slewing:
        return False, "mount slewing"
    if not leaf:
        return False, "nothing running"
    from photonscript.shared.target_names import target_key
    tkey = target_key(target) if target else ""
    for name in list(path or []) + [leaf]:
        if tkey and target_key(name).startswith(tkey):
            continue
        low = str(name or "").lower()
        for k in NOT_IMAGING:
            if k in low:
                return False, f"not imaging ({name})"
    if "exposure" not in str(leaf).lower():
        return False, f"not exposing ({leaf})"
    return True, "imaging"


def _project_for(projects, target: str | None):
    """The goal (project) the running NINA target names, or None."""
    if not target:
        return None
    from photonscript.shared.target_names import canonical_target, target_key
    try:
        name = canonical_target(target, list(projects or [])) or target
    except Exception:  # noqa: BLE001
        name = target
    key = target_key(name)
    for p in projects or []:
        t = getattr(p, "target", None)
        if t is None or not key:
            continue
        if key in (target_key(getattr(t, "name", "")),
                   target_key(getattr(t, "catalog_id", "") or "")):
            return p
    return None


def expected_center(cfg, projects, target: str | None, pier: str | None = None,
                    mount_radec: tuple | None = None) -> dict | None:
    """Where the RC16 should point for the running target: {"ra_deg",
    "dec_deg", "source", "name"} (J2000), or None when the target is not a
    known goal. source: "target" | "mosaic panel" | "piggy shift (PS-26)"."""
    p = _project_for(projects, target)
    if p is None:
        return None
    t = p.target
    try:
        ra_h, dec = float(t.ra_hours), float(t.dec_degrees)
    except (TypeError, ValueError, AttributeError):
        return None
    m = getattr(p, "mosaic", None)
    out = {"ra_deg": ra_h * 15.0, "dec_deg": dec, "name": t.name,
           "source": "mosaic panel" if isinstance(m, dict) and m.get("id") else "target"}
    if (getattr(p, "driving_rig", "rc16") or "rc16") != "piggyback":
        return out
    try:
        from photonscript.scheduler import piggy_offset as po
        if po.mode(cfg) != "on":
            return out
        plan = po.center_plan(cfg, ra_h, dec, po.load(cfg), po.frame_center_of(p))
        if not plan.get("applied"):
            return out
        sides = plan.get("by_pier") or {}
        side = sides.get(str(pier or "").capitalize()) if pier else None
        if side is None and sides and mount_radec and None not in mount_radec:
            # pier unknown: the side nearer to where the mount points
            side = min(sides.values(), key=lambda s: (separation(
                mount_radec[0], mount_radec[1], s["ra_hours"] * 15.0,
                s["dec_degrees"]) or {}).get("arcmin", 1e9))
        if side:
            out.update(ra_deg=side["ra_hours"] * 15.0, dec_deg=side["dec_degrees"],
                       source="piggy shift (PS-26)")
    except Exception as e:  # noqa: BLE001
        logger.debug("off-target: piggy shift unavailable: %s", e)
    return out


def _parse_t(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).rstrip("Z")).replace(tzinfo=None) if s else None
    except ValueError:
        return None


def latest_solve(cfg, night: str | None, now: datetime,
                 max_age_min: float) -> dict | None:
    """The newest solved RC16 record of tonight no older than max_age_min
    (by its sub start, else its solve time): {"ra_deg", "dec_deg",
    "age_min", "file"}; None when there is none."""
    if not night:
        return None
    try:
        from photonscript.scheduler.solve_store import load
        recs = load(cfg, night, "rc16")
    except Exception:  # noqa: BLE001
        return None
    best = None
    for r in recs:
        if not r.get("solved") or r.get("ra") is None or r.get("dec") is None:
            continue
        t = _parse_t(r.get("start_utc")) or _parse_t(r.get("at"))
        if t is None:
            continue
        if best is None or t > best[0]:
            best = (t, r)
    if best is None:
        return None
    age = (now - best[0]).total_seconds() / 60.0
    if age > max_age_min or age < -5:
        return None
    return {"ra_deg": float(best[1]["ra"]), "dec_deg": float(best[1]["dec"]),
            "age_min": round(age, 1), "file": best[1].get("file")}


def assess(cfg, rc16: dict, mount: dict | None, armer_state: str,
           projects=None, now: datetime | None = None,
           night: str | None = None) -> dict:
    """The panel's off-target block (no state, no alerts): target, expected
    center, mount and solve separations, which one governs, the limit and
    whether NINA #1 is imaging. mount = {"ra_hours", "dec_deg", "epoch",
    "pier", "slewing"}."""
    now = now or datetime.utcnow()
    lim = limits(cfg)
    m = mode(cfg)
    rc16 = rc16 or {}
    mount = mount or {}
    target = rc16.get("target")
    out = {"mode": m, "limit_arcmin": lim["arcmin"], "target": target,
           "expected": None, "mount": None, "solve": None, "sep_arcmin": None,
           "source": None, "imaging": False, "reason": "",
           "sub_key": None, "night": night}
    if m == "off":
        out["reason"] = "off (off_target_mode)"
        return out
    imaging, why = is_imaging(rc16.get("path"), rc16.get("running"),
                              mount.get("slewing"), target)
    if armer_state not in ("RUNNING", "WATCHING"):
        imaging, why = False, f"armer {armer_state or '-'}"
    out["imaging"], out["reason"] = imaging, why
    ra_h, dec = mount.get("ra_hours"), mount.get("dec_deg")
    mradec = ((float(ra_h) * 15.0, float(dec))
              if ra_h is not None and dec is not None else None)
    exp = expected_center(cfg, projects, target, mount.get("pier"), mradec)
    if exp is None:
        if target and imaging:
            out["reason"] = f"{target}: not a goal, no planned coordinates"
        return out
    out["expected"] = {k: (round(v, 5) if isinstance(v, float) else v)
                       for k, v in exp.items()}
    if mradec is not None:
        ep = mount_epoch(cfg, mount.get("epoch"))
        era, edec = exp["ra_deg"], exp["dec_deg"]
        if ep == "JNow":
            era, edec = precess_from_j2000(era, edec, now)
        s = separation(era, edec, mradec[0], mradec[1])
        if s:
            out["mount"] = {**s, "epoch": ep}
    sol = latest_solve(cfg, night, now, lim["solve_max_age_min"])
    if sol is not None:
        s = separation(exp["ra_deg"], exp["dec_deg"], sol["ra_deg"], sol["dec_deg"])
        if s:
            out["solve"] = {**s, "age_min": sol["age_min"], "file": sol["file"]}
    gov = out["solve"] or out["mount"]
    if gov:
        out["sep_arcmin"] = gov["arcmin"]
        out["source"] = "plate solve" if out["solve"] else "mount"
    sub = rc16.get("sub") or {}
    if sub.get("n") is not None:
        out["sub_key"] = f"{target}|{rc16.get('filter')}|{sub.get('of')}|{sub.get('n')}"
    return out


# ------------------------------------------------------------------ monitor

class OffTargetMonitor:
    """Debounce + once-per-target-per-night alert latch. update() feeds one
    assess() result; view() reads the state for the panel without changing
    it."""

    def __init__(self):
        self.night: str | None = None
        self.alerted: dict[str, str] = {}   # target -> when (this night)
        self.streak: dict | None = None     # {"target", "since", "subs", "max"}
        self.status: str = "idle"
        self.at: datetime | None = None

    def _roll(self, night):
        if night and night != self.night:
            self.night = night
            self.alerted = {}
            self.streak = None

    def update(self, a: dict, cfg, now: datetime | None = None) -> dict:
        """Returns {"status", "alert" (push now), "clear" (an alerted
        streak ended), "streak"}. status: idle | unknown | ok | watch | off."""
        now = now or datetime.utcnow()
        self._roll(a.get("night"))
        lim = limits(cfg)
        prev = self.streak
        res = {"alert": False, "clear": False}
        target, sep = a.get("target"), a.get("sep_arcmin")
        if not a.get("imaging") or a.get("mode") == "off":
            self.streak, self.status = None, "idle"
        elif sep is None:
            self.streak, self.status = None, "unknown"
        elif sep <= lim["arcmin"]:
            self.streak, self.status = None, "ok"
        else:
            st = self.streak
            if st is None or st.get("target") != target:
                st = {"target": target, "since": now, "subs": [], "max": sep}
            key = a.get("sub_key")
            if key and key not in st["subs"]:
                st["subs"].append(key)
            st["max"] = max(st["max"], sep)
            st["last"] = sep
            self.streak = st
            mins = (now - st["since"]).total_seconds() / 60.0
            if len(st["subs"]) > lim["subs"] or mins >= lim["minutes"]:
                self.status = "off"
                if target not in self.alerted:
                    self.alerted[target] = now.replace(microsecond=0).isoformat() + "Z"
                    res["alert"] = True
            else:
                self.status = "watch"
        if (prev is not None and self.streak is None
                and prev.get("target") in self.alerted and self.status == "ok"):
            res["clear"] = True
        self.at = now
        res["status"] = self.status
        res["streak"] = self._streak_view(now)
        return res

    def _streak_view(self, now) -> dict | None:
        st = self.streak
        if not st:
            return None
        return {"target": st["target"], "subs": len(st["subs"]),
                "minutes": round((now - st["since"]).total_seconds() / 60.0, 1),
                "max_arcmin": round(st["max"], 1)}

    def view(self, target: str | None, now: datetime | None = None) -> dict:
        """The latest monitor state for the panel (read only)."""
        now = now or datetime.utcnow()
        st = self.streak if self.streak and self.streak.get("target") == target else None
        status = self.status if (st or self.status != "watch"
                                 and self.status != "off") else "idle"
        return {"status": status,
                "alerted": target in self.alerted if target else False,
                "streak": self._streak_view(now) if st else None,
                "checked_at": (self.at.replace(microsecond=0).isoformat() + "Z"
                               if self.at else None)}


MONITOR = OffTargetMonitor()


def _event(cfg, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        append_jsonl(events_path(cfg, night_of(cfg, now)),
                     {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                      "kind": "off_target", "value": value, "detail": detail,
                      **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("off-target event (%s) not logged: %s", value, e)


def alert_text(a: dict, res: dict, cfg) -> str:
    lim = limits(cfg)
    st = res.get("streak") or {}
    gov = (a.get("solve") or a.get("mount") or {})
    exp = a.get("expected") or {}
    src = a.get("source") or "mount"
    return (f"OFF TARGET: {a.get('target')} ({exp.get('source', 'target')}): the "
            f"{src} is {a.get('sep_arcmin'):.1f}' {gov.get('dir') or ''} of where "
            f"the plan wants the RC16 (limit {lim['arcmin']:g}'), for "
            f"{st.get('subs', 0)} sub(s) / {st.get('minutes', 0):.0f} min while "
            "imaging. Nothing was stopped: check the Where is it panel, then "
            "Pause or Restart tonight from now. Once per target per night.")


async def handle(cfg, a: dict, monitor: OffTargetMonitor | None = None,
                 now: datetime | None = None, notify=None) -> dict:
    """Feed one assessment to the monitor; push + event on an alert, event
    on a clear. notify is injectable for tests."""
    monitor = monitor or MONITOR
    now = now or datetime.utcnow()
    res = monitor.update(a, cfg, now)
    if res["alert"]:
        msg = alert_text(a, res, cfg)
        _event(cfg, "alert", msg, now, target=a.get("target"),
               sep_arcmin=a.get("sep_arcmin"), source=a.get("source"),
               streak=res.get("streak"), expected=a.get("expected"))
        if mode(cfg) == "alert":
            if notify is None:
                from photonscript.shared.pushover import notify as _n
                notify = _n
            await notify(cfg, msg, title="PhotonScript off target", priority=1)
    elif res["clear"]:
        _event(cfg, "clear", f"{a.get('target')}: back within "
               f"{limits(cfg)['arcmin']:g}' ({a.get('sep_arcmin')}')", now,
               target=a.get("target"), sep_arcmin=a.get("sep_arcmin"))
    return res


async def run_monitor(get_config, get_armer, get_tel, get_projects,
                      tick_seconds: int = MONITOR_TICK_S) -> None:
    """Background loop (app startup): while the armer is RUNNING or
    WATCHING, read the RC16 side of the where panel and feed the monitor.
    Idle otherwise. Never raises."""
    from photonscript.scheduler.where_panel import collect
    while True:
        try:
            cfg = get_config()
            armer = get_armer()
            if mode(cfg) != "off" and getattr(armer, "state", "") in ("RUNNING", "WATCHING"):
                d = await collect(cfg, armer, tel=get_tel(), projects=get_projects(),
                                  piggy=False)
                a = d.get("off_target")
                if a:
                    await handle(cfg, a)
            else:
                MONITOR.streak, MONITOR.status = None, "idle"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("off-target monitor tick failed: %s", e)
        await asyncio.sleep(tick_seconds)
