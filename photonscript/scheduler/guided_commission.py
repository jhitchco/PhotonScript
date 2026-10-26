"""Guided commissioning: the first guided night's guiding test (2026-10-26).

Why: the RC16 has imaged unguided for weeks; its PHD2 path (OAG GP678C, then
the ASI174MM Mini of PS-190) has a FAIL calibration, an old dark library and
settings the audit warns about. A guided night that commits RC16 to 600 to
900 s Ha on a guider that does not hold the star loses the whole night.
So the first guided night with a guide camera proves the guiding first:

    cap_for_test(config, targets, night, guided, camera)
        armer dispatch: while tonight's test is pending (act mode), every
        guided RC16 set longer than guided_commission_test_exposure_s (300)
        is cut to it, same integration (target_planner.cap_unguided's rule)
    judge(config, subs, now)       pure: tonight's RC16 lights from the first
        one on: wait until at least guided_commission_test_min (15) minutes of
        guided exposure in at least guided_commission_min_subs (2) subs, then
        pass when the median per-sub PHD2 total RMS (guide_rms, the frames
        inside each exposure) is at most guided_commission_rms_max_arcsec
        (1.2") and PHD2 guided at least guided_commission_min_guided_frac
        (0.5) of the window's subs; fail on either, or when nothing is judged
        guided_commission_timeout_min (45) after the first RC16 light
    tick(config, armer)            background (app startup, every 60 s), only
        while the armer is RUNNING a GUIDED night:
            act    pass -> record, story push, restart after the current sub
                   when tonight was capped (the re-plan has the planned
                   lengths; the night pilot's guided narrowband ladder,
                   night_pilot_ladder_rc16_guided_nb, may go longer);
                   fail -> record, priority push with why, Armer.
                   fallback_unguided (the rest of tonight unguided: PS-156
                   caps, rc16_narrowband_requires_guided, moon-fill carriers
                   when the moon is up, the Piggy-600 untouched)
            advise the same verdict, one push "would have ...", nothing changed
    status(config, night, camera)  the readiness gate's view: needed / passed
                                   / failed, the text and the fix
    allows_long_nb(config, night, camera)   the guided NB ladder may climb

A pass is kept per guide camera (<data_dir>/guided/commission_record.json):
later guided nights with that camera skip the test. A fail, or a new guide
camera, tests again on the next guided night. Tonight's state:
<data_dir>/guided/commission/<night>.json. Run events kind
guided_commission. GET /api/guiding/commission.

guided_commission_mode advise (default) judges and pushes the verdict but
caps and switches nothing. guided_trial_night (YYYY-MM-DD) makes that one
night guided when nothing explicit chose (trial_tonight; the armer's
config default and the auto-arm both read it), whatever the readiness list
says, and runs the test in act (unless the mode is off): the test decides.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

MODES = ("off", "advise", "act")
KIND = "guided_commission"
GUIDING = ("guiding", "settling")
RECORD_FILE = "commission_record.json"
RESTART_DARK_MIN = 45.0


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _f(cfg, key, default) -> float:
    v = _num(getattr(cfg, key, default))
    return float(default) if v is None else v


def own_mode(config) -> str:
    m = str(getattr(config, "guided_commission_mode", "advise") or "advise").strip().lower()
    return m if m in MODES else "advise"


def mode(config, night: str | None = None) -> str:
    """The effective mode: guided_commission_mode, except that a guided trial
    night (guided_trial_night = night) always tests in act unless the mode
    is off."""
    m = own_mode(config)
    if m != "off" and trial_tonight(config, night):
        return "act"
    return m


def _dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "guided"


def night_path(config, night: str) -> Path:
    return _dir(config) / "commission" / f"{night}.json"


def load_night(config, night: str) -> dict:
    return store.read_json(night_path(config, night)) or {"night": night}


def save_night(config, st: dict) -> None:
    try:
        store.write_json(night_path(config, st["night"]), st)
    except OSError as e:
        logger.warning("guided commissioning state not saved: %s", e)


def load_record(config) -> dict:
    d = store.read_json(_dir(config) / RECORD_FILE) or {}
    d.setdefault("cameras", {})
    return d


def _save_record(config, d: dict) -> None:
    try:
        store.write_json(_dir(config) / RECORD_FILE, d)
    except OSError as e:
        logger.warning("guided commissioning record not saved: %s", e)


def current_camera(config) -> str | None:
    """The guide camera id of the newest PHD2 audit (PS-190), else the one
    the readiness gate saw last."""
    try:
        from photonscript.scheduler import phd2_audit as pa
        cam = (pa.load_latest(config) or {}).get("camera") or {}
        if cam.get("id") and cam.get("detected"):
            return str(cam["id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        from photonscript.scheduler.guided_readiness import camera_state
        return camera_state(config).get("id")
    except Exception:  # noqa: BLE001
        return None


def commissioned(config, camera: str | None) -> dict | None:
    """The pass on record for this camera (None: never passed, or the last
    test with it failed)."""
    e = (load_record(config).get("cameras") or {}).get(str(camera or "unknown"))
    return e if e and e.get("verdict") == "pass" else None


def trial_tonight(config, night: str | None) -> bool:
    t = str(getattr(config, "guided_trial_night", "") or "").strip()
    return bool(t and night and t == night)


def decided(st: dict) -> bool:
    return st.get("verdict") in ("pass", "fail")


def test_pending(config, night: str | None, camera: str | None = None) -> bool:
    """Tonight's test still has to run (mode not off, not decided tonight,
    no pass on record for this camera)."""
    if mode(config, night) == "off" or not night:
        return False
    if decided(load_night(config, night)):
        return False
    return commissioned(config, camera if camera is not None else current_camera(config)) is None


def allows_long_nb(config, night: str | None, camera: str | None = None) -> bool:
    """The guided narrowband ladder may climb: commissioning not in act
    (off or advise gate nothing), passed tonight, or passed before with this
    camera."""
    if mode(config, night) != "act":
        return True
    if night and load_night(config, night).get("verdict") == "pass":
        return True
    return commissioned(config, camera if camera is not None else current_camera(config)) is not None


def status(config, night: str | None, camera: str | None = None) -> dict:
    """The readiness gate's view of commissioning."""
    m = mode(config, night)
    camera = camera if camera is not None else current_camera(config)
    st = load_night(config, night) if night else {}
    rec = commissioned(config, camera)
    last = (load_record(config).get("cameras") or {}).get(str(camera or "unknown"))
    out = {"mode": m, "camera": camera, "tonight": {k: st.get(k) for k in (
        "verdict", "acted", "capped", "cap_s", "result", "t_decided") if st.get(k) is not None},
        "record": last, "needed": False, "text": None, "fix": None}
    if m == "off":
        out["text"] = "guided commissioning off (guided_commission_mode)"
        return out
    if st.get("verdict"):
        r = st.get("result") or {}
        out["text"] = f"tonight's guiding test: {st['verdict'].upper()} ({r.get('why')})"
        return out
    if rec:
        out["text"] = (f"commissioned {rec.get('night')} (RMS {rec.get('rms_arcsec')}\" over "
                       f"{rec.get('n')} subs)")
        return out
    out["needed"] = True
    tmin = _f(config, "guided_commission_test_min", 15)
    cap = _f(config, "guided_commission_test_exposure_s", 300)
    rms = _f(config, "guided_commission_rms_max_arcsec", 1.2)
    lead = (f"last test {last.get('night')} FAILED ({last.get('why')}); " if last else "")
    if m == "act":
        out["text"] = (lead + f"the first guided night runs a {tmin:g} min guiding test "
                       f"(RC16 subs at {cap:g} s) and keeps guiding only at median RMS "
                       f"<= {rms:g}\"")
        out["fix"] = ("Nothing to do: it runs on the next guided night. Pass: planned "
                      "lengths and the guided narrowband ladder. Fail: the rest of that "
                      "night unguided.")
    else:
        out["text"] = (lead + f"the first guided night's first {tmin:g} min of guided subs "
                       f"are judged against median RMS <= {rms:g}\" and the verdict pushed "
                       "(advise: nothing capped or switched)")
        out["fix"] = ("guided_trial_night = the night (System page) runs it in act: RC16 "
                      f"subs at {cap:g} s until it passes, unguided for the rest of the "
                      "night if it fails. Or set guided_commission_mode act.")
    return out


# --------------------------------------------------------------------------
# the cap while the test runs (armer dispatch)
# --------------------------------------------------------------------------

def cap_for_test(config, targets, night: str | None, guided: bool,
                 camera: str | None = None) -> list[str]:
    """Act mode, a guided night with tonight's test pending: cut every guided
    RC16 set over guided_commission_test_exposure_s to it (same
    integration). Records "capped" in tonight's state. Returns notes."""
    if not guided or mode(config, night) != "act" or not test_pending(config, night, camera):
        return []
    cap = _f(config, "guided_commission_test_exposure_s", 300.0)
    if cap <= 0:
        return []
    notes: list[str] = []
    for t in targets or []:
        if (not getattr(t, "start_guiding", False) or getattr(t, "focus_calibration", False)
                or getattr(t, "tracking_test", False)):
            continue
        new = []
        for e in t.exposures:
            owed = e.count - e.acquired
            if e.exposure_seconds > cap and owed > 0:
                n = math.ceil(owed * e.exposure_seconds / cap - 1e-9)
                notes.append(f"{t.name} {e.filter_type.value}: {owed}x"
                             f"{e.exposure_seconds:.0f}s -> {n}x{cap:.0f}s")
                e = e.model_copy(update={"exposure_seconds": cap, "count": n,
                                         "acquired": 0, "acquired_s": 0.0})
            new.append(e)
        t.exposures = new
    st = load_night(config, night)
    st.update(capped=bool(notes) or bool(st.get("capped")), cap_s=cap,
              cap_notes=notes or st.get("cap_notes"),
              camera=camera if camera is not None else current_camera(config))
    st.setdefault("t_start", store.iso_z(datetime.utcnow()))
    save_night(config, st)
    if notes:
        logger.info("Guided commissioning cap %.0fs: %s", cap, "; ".join(notes))
    return notes


# --------------------------------------------------------------------------
# the verdict (pure)
# --------------------------------------------------------------------------

def _rc16_lights(subs: list[dict]) -> list[dict]:
    from photonscript.scheduler.sub_ladder import rig_lights
    return rig_lights(subs, "rc16")


def _guided(r: dict) -> bool:
    rms = _num(r.get("guide_rms"))
    return (str(r.get("guide_state") or "").lower() in GUIDING
            and rms is not None and rms > 0)


def judge(config, subs: list[dict], now: datetime, since: datetime | None = None) -> dict:
    """{"verdict": wait | pass | fail, "why", "n", "n_guided", "guided_min",
    "rms_arcsec", "rms_worst", "ecc", "first_utc"} over tonight's RC16 lights
    (from since, default the first one)."""
    from photonscript.shared.sub_time import sub_start
    rows = _rc16_lights(subs)
    if since is not None:
        rows = [r for r in rows if sub_start(r) >= since]
    out = {"verdict": "wait", "why": "", "n": len(rows), "n_guided": 0,
           "guided_min": 0.0, "rms_arcsec": None, "rms_worst": None, "ecc": None,
           "first_utc": None}
    if not rows:
        out["why"] = "no RC16 lights yet"
        return out
    first = sub_start(rows[0])
    out["first_utc"] = store.iso_z(first)
    tmin = _f(config, "guided_commission_test_min", 15.0)
    nmin = max(1, int(_f(config, "guided_commission_min_subs", 2)))
    rms_max = _f(config, "guided_commission_rms_max_arcsec", 1.2)
    frac_min = _f(config, "guided_commission_min_guided_frac", 0.5)
    timeout = _f(config, "guided_commission_timeout_min", 45.0)
    g = [r for r in rows if _guided(r)]
    gsec = sum(_num(r.get("exp_s") or r.get("exposure")) or 0.0 for r in g)
    out.update(n_guided=len(g), guided_min=round(gsec / 60.0, 1))
    if g:
        rms = [_num(r.get("guide_rms")) for r in g]
        out["rms_arcsec"] = round(statistics.median(rms), 2)
        out["rms_worst"] = round(max(rms), 2)
        ecc = [e for e in (_num(r.get("ecc")) for r in g) if e is not None]
        out["ecc"] = round(statistics.median(ecc), 2) if ecc else None
    if gsec >= tmin * 60.0 - 1e-6 and len(g) >= nmin:
        frac = len(g) / len(rows)
        if frac < frac_min:
            out.update(verdict="fail", why=f"PHD2 guided only {len(g)} of {len(rows)} RC16 "
                       f"subs (need {frac_min:.0%})")
        elif out["rms_arcsec"] > rms_max:
            out.update(verdict="fail", why=f"median guide RMS {out['rms_arcsec']:g}\" over "
                       f"{len(g)} subs, over {rms_max:g}\"")
        else:
            out.update(verdict="pass", why=f"median guide RMS {out['rms_arcsec']:g}\" (worst "
                       f"{out['rms_worst']:g}\") over {len(g)} guided subs, "
                       f"{out['guided_min']:g} min")
        return out
    if (now - first).total_seconds() > timeout * 60.0:
        out.update(verdict="fail", why=f"only {len(g)} guided RC16 sub(s), "
                   f"{out['guided_min']:g} min, in {timeout:g} min since the first light: "
                   "PHD2 is not guiding")
        return out
    out["why"] = (f"{out['guided_min']:g} of {tmin:g} min guided ({len(g)} of at least "
                  f"{nmin} subs)")
    return out


# --------------------------------------------------------------------------
# the background tick
# --------------------------------------------------------------------------

def _event(config, night: str, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        store.append_jsonl(events_path(config, night),
                           {"t": store.iso_z(now), "rig": "rc16", "src": "photonscript",
                            "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.debug("guided commissioning event not logged: %s", e)


def _record(config, camera, night, res: dict, now: datetime) -> None:
    d = load_record(config)
    d["cameras"][str(camera or "unknown")] = {
        "verdict": res["verdict"], "night": night, "t": store.iso_z(now),
        "rms_arcsec": res.get("rms_arcsec"), "rms_worst": res.get("rms_worst"),
        "n": res.get("n_guided"), "guided_min": res.get("guided_min"),
        "ecc": res.get("ecc"), "why": res.get("why")}
    _save_record(config, d)


def _subs(config, night: str) -> list[dict]:
    try:
        from photonscript.scheduler.runs import _load_subs
        return _load_subs(config, night)
    except Exception:  # noqa: BLE001
        return []


async def _notify(config, msg: str, priority: int = 0, kind: str = "story") -> None:
    try:
        from photonscript.shared import pushover
        await pushover.notify(config, msg, title="PhotonScript guided commissioning",
                              priority=priority, kind=kind)
    except Exception as e:  # noqa: BLE001
        logger.debug("guided commissioning push failed: %s", e)


def _restart_guard(config, armer, now: datetime) -> str | None:
    try:
        from photonscript.scheduler.night_pilot import live_guard
        why = live_guard(config, armer, {"running": None, "target": None, "phd2": None})
        if why:
            return why
    except Exception:  # noqa: BLE001
        pass
    try:
        left = (armer._dawn() - now).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        left = None
    if left is not None and left < RESTART_DARK_MIN:
        return f"only {max(0, int(left))} min of dark left"
    return None


async def tick(config, armer, now: datetime | None = None, *, subs=None,
               notify=None) -> dict:
    """One pass (see the module doc). subs / notify: test hooks. Never
    raises."""
    now = now or datetime.utcnow()
    notify = notify or _notify
    m = own_mode(config)
    out = {"mode": m, "step": None}
    try:
        if m == "off":
            return out
        if str(getattr(armer, "state", "") or "") != "RUNNING":
            return out
        try:
            if not armer._use_guiding():
                return out
        except Exception:  # noqa: BLE001
            return out
        night = (getattr(armer, "plan", None) or {}).get("night_of") or store.night_of(config, now)
        m = out["mode"] = mode(config, night)
        st = load_night(config, night)
        if decided(st):
            out["step"] = "decided"
            return out
        camera = st.get("camera") or current_camera(config)
        rec = commissioned(config, camera)
        if rec and not st.get("capped"):
            if not st.get("skipped"):
                st.update(skipped=True, camera=camera, note=f"commissioned {rec.get('night')}")
                save_night(config, st)
            out["step"] = "commissioned"
            return out
        res = judge(config, _subs(config, night) if subs is None else subs, now)
        st["last"] = {k: res.get(k) for k in ("verdict", "why", "n", "n_guided",
                                              "guided_min", "rms_arcsec")}
        if res["verdict"] == "wait":
            save_night(config, st)
            out["step"] = "wait"
            return out
        st.update(verdict=res["verdict"], result=res, t_decided=store.iso_z(now),
                  camera=camera, acted=(m == "act"))
        save_night(config, st)
        _record(config, camera, night, res, now)
        _event(config, night, res["verdict"], res["why"], now, mode=m,
               rms_arcsec=res.get("rms_arcsec"), n=res.get("n_guided"))
        if res["verdict"] == "pass":
            out["step"] = "pass"
            tail = ("RC16 narrowband stays guided; the night pilot's guided narrowband "
                    "ladder may climb (night_pilot_ladder_rc16_guided_nb).")
            if m == "act" and st.get("capped"):
                why = _restart_guard(config, armer, now)
                if why:
                    st["restart"] = {"ok": False, "detail": why}
                    msg = (f"Guided commissioning PASSED: {res['why']}. Subs stay at "
                           f"{st.get('cap_s'):g} s tonight ({why}). " + tail)
                else:
                    r = await armer.restart("after_exposure", now=now)
                    st["restart"] = {"ok": bool((r or {}).get("ok")),
                                     "detail": (r or {}).get("detail")}
                    msg = (f"Guided commissioning PASSED: {res['why']}. Restarting after "
                           "the current sub at the planned lengths. " + tail
                           if st["restart"]["ok"] else
                           f"Guided commissioning PASSED: {res['why']}. The restart to "
                           f"the planned lengths was refused ({st['restart']['detail']}): "
                           "use Restart tonight from now. " + tail)
                save_night(config, st)
            elif m == "act":
                msg = f"Guided commissioning PASSED: {res['why']}. " + tail
            else:
                msg = (f"Guided commissioning (advise): would PASS: {res['why']}. "
                       "guided_commission_mode=advise: nothing changed.")
            await notify(config, msg, 0, "story")
            return out
        out["step"] = "fail"
        if m == "act":
            ok = False
            try:
                ok = bool(await armer.fallback_unguided(
                    f"guided commissioning failed: {res['why']}"))
            except Exception as e:  # noqa: BLE001
                logger.warning("guided commissioning fallback failed: %s", e)
            st["fallback"] = {"ok": ok}
            save_night(config, st)
            msg = (f"Guided commissioning FAILED: {res['why']}. "
                   + ("The rest of tonight runs UNGUIDED (Piggy-first under a bright moon; "
                      "RC16 narrowband only guided). " if ok else
                      "The switch to unguided did not run: check the night. ")
                   + "Fix: Guiding tab > Guided night readiness, then arm guided again "
                     "(the test repeats).")
            await notify(config, msg, 1, "act")
        else:
            await notify(config, f"Guided commissioning (advise): would FAIL and switch the "
                         f"rest of tonight unguided: {res['why']}. "
                         "guided_commission_mode=advise: nothing changed.", 0, "story")
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("guided commissioning tick failed: %s", e)
        out["error"] = str(e)
        return out


TICK_S = 60
PREP_EVERY_S = 1800


async def run(get_config, get_armer, tick_s: int = TICK_S) -> None:
    """Background loop (app startup): the commissioning tick every minute,
    the readiness fix-ups (guided_readiness.prep_tick) every 30 min. Never
    raises."""
    import asyncio
    last_prep = 0.0
    while True:
        try:
            cfg, armer = get_config(), get_armer()
            await tick(cfg, armer)
            import time
            if time.monotonic() - last_prep >= PREP_EVERY_S:
                last_prep = time.monotonic()
                from photonscript.scheduler.guided_readiness import prep_tick
                await prep_tick(cfg, armer)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("guided loop failed: %s", e)
        await asyncio.sleep(tick_s)
