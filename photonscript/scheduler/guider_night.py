"""Guider commissioning night: recipe guider_commission (2026-10-28).

The first night with the ZWO ASI174MM Mini on the RC16 OAG (0.372"/px,
5.86 um, PHD2 2.6.14). Jeremy: script the guider commissioning so it runs
without him. Built on what exists (guided_readiness, guided_commission,
PS-89 audit apply, PS-90 star stats, PS-92 self-test, PS-93 calibration
slot, the PS-148 optics-test report) as one arm recipe (PS-181):

    POST /api/arm {"armed": true, "guiding": "guided",
                   "recipe": "guider_commission",
                   "recipe_opts": {"skip": [...], "rounds": 2, "star_tries": 3}}

or no recipe at all on the guided trial night (guided_trial_night = the
night, guider_commission_on_trial_night on): the arm attaches it.

The steps, in order (every one logs to the night story and the run events,
kind guider_commission_night; each can be skipped with recipe_opts.skip and
each fails soft: a failed step never stops the night):

  1 darks       before dusk (service tick): does the PHD2 dark library fit
                the current camera (built after the camera change, holds the
                guide exposure)? PHD2 2.6 has no RPC to build darks or load
                the defect map and the desired rows are manual, so this is
                always a human step (an act push with the GUI steps), never
                faked
  2 exposure    before dusk: the PHD2 guide exposure to the audit target
                (the readiness fast path when it has one) through the PS-89
                audit apply (PHD2 Stopped / Looping only, phd2_ops.hold)
  3 star        in tonight's PS-93 PHD2_CALIBRATION slot, after its Center:
                loop, find_star, measure SNR / HFD (PS-90 star_stats, PS-91
                hot-pixel share), guider_commission_star_tries tries. No
                star: STOP. The script exits 1, NINA skips the rest of the
                slot, an act push says the OAG camera needs focusing by hand,
                and the guiding test fails at once (guided_commission falls
                back to unguided; the narrowband focus test still runs)
  4 calibration the PS-93 slot itself (graded, one retry by the calmanager);
                the armer asks for it at the dispatch (a calibration request)
  5 selftest    the PS-92 pulse self-test (context "commission")
  6 ga          the Guiding Assistant has no PHD2 RPC: 5 min of guiding,
                RMS and a min-move from the frame-to-frame star motion
                (PHD2's GA multipliers), then (ga-apply, PHD2 Stopped) the
                RA / Dec min-move through the PS-89 apply rows
  7 nb_focus    the RC16 narrowband focus test (nina_sequence_json
                ._build_guider_commission_container): AF per filter in L, Ha,
                OIII with 30 s frames at each filter's own focus, two rounds
                interleaved, then an Ha sweep -2,-1,+1,+2 x 110 steps; the
                report (nb_report) measures ecc, FWHM and the stretch axis
                per frame (PS-80 sidecars through optics_test) and decides:
                an axis that flips through focus = astigmatism (set the NB
                focus offsets), a constant axis at Ha best focus with L round
                = filter seating / tilt
  8 handover    tonight's targets with guided_commission in act for this
                night (RC16 subs capped at 300 s until the first 15 min pass
                at median RMS <= 1.2", then the guided narrowband ladder;
                unguided for the rest of the night on a fail)

Never touches the roof, parks or unparks (only NINA's own sequence does,
behind its Safety conditions; the service steps refuse while NINA's safety
monitor reads unsafe), TPoint or ProTrack.

    plan(config, night)        the dry run: every step with its time
    pre_tick(config, armer)    steps 1 and 2 (from guided_commission.run)
    run_step(config, step)     the NINA-side steps (deploy\\guider-commission.cmd
                               -> photonscript guider-commission-step ->
                               POST /api/guiding/commission-night/step)
    nb_report(config, night)   the narrowband focus report
State: <data_dir>/guided/guider_night/<night>.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

RECIPE = "guider_commission"
KIND = "guider_commission_night"
TITLE = "PhotonScript guider commissioning"
STEPS = (
    ("darks", "PHD2 dark library for this camera"),
    ("exposure", "PHD2 guide exposure"),
    ("star", "Guide star on the OAG"),
    ("calibration", "PS-93 calibration"),
    ("selftest", "PS-92 pulse self-test"),
    ("ga", "Min-move from 5 min of guiding"),
    ("nb_focus", "RC16 narrowband focus test"),
    ("handover", "Guided night with the guiding test"),
)
STEP_IDS = tuple(s for s, _ in STEPS)
STEP_TITLES = dict(STEPS)
SKIPPABLE = STEP_IDS[:-1] + ("ga_apply",)
NINA_STEPS = ("gate", "star", "selftest", "ga", "ga-apply", "nb-report")
OK, WARN, FAIL, SKIPPED, HUMAN, HELD = "ok", "warn", "fail", "skipped", "human", "held"
EXIT_STOP = 3            # the CLI exit code deploy\guider-commission.cmd turns into 1
PRE_RETRY_S = 600.0      # pre-dusk steps retry at most every 10 min
AUDIT_FRESH_H = 2.0
GA_MIN_FRAMES = 30
GA_RA_K = 0.65           # PHD2 Guiding Assistant: RA min-move = 0.65 x RA RMS
GA_DEC_K_FINE = 1.28     # ... Dec 1.28 x Dec RMS under 1.5"/px, else 1.65
GA_DEC_K = 1.65
MINMOVE_RANGE = (0.10, 3.0)
STAR_SHARE_MAX = 0.5     # PS-91: over half the 5x5 flux in one pixel = hot pixel
FWHM_PER_HFR = 2.0       # a Gaussian star: FWHM = 2 x half-flux radius
TILT_AXIS_DEG = 38.0     # 2026-10-28 195-sub analysis: the NB stretch axis
NB_REPORT_WAIT_S = 180.0


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _f(cfg, key, default) -> float:
    v = _num(getattr(cfg, key, default))
    return float(default) if v is None else v


def _i(cfg, key, default) -> int:
    try:
        return int(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return int(default)


def _dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "guided" / "guider_night"


def state_path(config, night: str) -> Path:
    return _dir(config) / f"{night}.json"


def load_state(config, night: str) -> dict:
    st = store.read_json(state_path(config, night)) or {}
    st.setdefault("night", night)
    st.setdefault("steps", {})
    return st


def save_state(config, st: dict) -> None:
    try:
        store.write_json(state_path(config, st["night"]), st)
    except OSError as e:
        logger.warning("guider night state not saved: %s", e)


def script_path(config) -> str:
    return str(getattr(config, "guider_commission_script", "") or "").strip()


def script_ready(config) -> str | None:
    """The deploy\\guider-commission.cmd path when it exists here, else None."""
    p = script_path(config)
    try:
        return p if p and Path(p).is_file() else None
    except OSError:
        return None


# --------------------------------------------------------------------------
# recipe options
# --------------------------------------------------------------------------

def parse_opts(opts) -> tuple[dict | None, str | None]:
    """recipe_opts for guider_commission: skip (a list or a comma string of
    step ids from SKIPPABLE), rounds (1 to 3 narrowband rounds), star_tries
    (1 to 10). (opts, None) or (None, error)."""
    if opts is not None and not isinstance(opts, dict):
        return None, "recipe_opts must be an object"
    opts = {k: v for k, v in (opts or {}).items() if v not in (None, "", [])}
    allowed = ("skip", "rounds", "star_tries")
    bad = sorted(set(opts) - set(allowed))
    if bad:
        return None, (f"recipe_opts {bad} not allowed for {RECIPE} "
                      f"(allowed: {', '.join(allowed)})")
    out: dict = {}
    if "skip" in opts:
        raw = opts["skip"]
        items = raw.split(",") if isinstance(raw, str) else list(raw)
        skip = []
        for x in items:
            k = str(x).strip().lower().replace("-", "_")
            if not k:
                continue
            if k not in SKIPPABLE:
                return None, (f"skip {k!r} is not a step: use "
                              + ", ".join(SKIPPABLE))
            if k not in skip:
                skip.append(k)
        if skip:
            out["skip"] = skip
    for key, lo, hi in (("rounds", 1, 3), ("star_tries", 1, 10)):
        if key in opts:
            try:
                out[key] = max(lo, min(hi, int(opts[key])))
            except (TypeError, ValueError):
                return None, f"{key} must be a whole number, not {opts[key]!r}"
    return out, None


def _skip(opts: dict | None) -> set:
    return set((opts or {}).get("skip") or ())


def recipe_tonight(config, night: str | None = None, armer=None) -> dict | None:
    """The armed guider_commission recipe of the armer when it belongs to
    `night` (default its plan's night), else None. Never raises."""
    try:
        if armer is None:   # the running armer only: never create one here
            from photonscript.scheduler import app as app_mod
            armer = getattr(app_mod, "_armer", None)
            if armer is None:
                return None
        r = armer._recipe_tonight()
    except Exception:  # noqa: BLE001
        return None
    if not r or r.get("id") != RECIPE:
        return None
    if night and r.get("night") != night:
        return None
    return r


def recipe_armed(config, night: str | None) -> bool:
    """guided_commission.mode: this night's guiding test runs in act."""
    return recipe_tonight(config, night) is not None


def auto_recipe(config, night: str | None, guided: bool) -> dict | None:
    """The recipe an arm without one gets on the guided trial night
    (guided_trial_night = night, guider_commission_on_trial_night on, a
    guided arm), else None."""
    if not guided or not bool(getattr(config, "guider_commission_on_trial_night", True)):
        return None
    from photonscript.scheduler.guided_commission import trial_tonight
    if not trial_tonight(config, night):
        return None
    from photonscript.scheduler.sideload import arm_recipe
    rec, _ = arm_recipe(RECIPE, None)
    return rec


def want_calibration(config, recipe: dict | None) -> bool:
    """The dispatch should ask PS-93 for a calibration slot: the recipe is
    still to run, calibration not skipped, phd2_cal_mode not never."""
    if not recipe or recipe.get("id") != RECIPE or recipe.get("status") != "armed":
        return False
    if "calibration" in _skip(recipe.get("opts")):
        return False
    from photonscript.scheduler import phd2_calibration as pc
    return pc.cfg_mode(config) != "never"


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------

def _event(config, night: str, step: str, status: str, detail: str,
           now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        store.append_jsonl(events_path(config, night), {
            "t": store.iso_z(now), "rig": "rc16", "src": "photonscript",
            "kind": KIND, "value": f"{step}:{status}", "step": step,
            "status": status, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.debug("guider night event not logged: %s", e)


async def _notify(config, msg: str, priority: int = 0, kind: str = "story") -> None:
    try:
        from photonscript.shared import pushover
        await pushover.notify(config, msg, title=TITLE, priority=priority, kind=kind)
    except Exception as e:  # noqa: BLE001
        logger.debug("guider night push failed: %s", e)


async def record(config, night: str, step: str, status: str, detail: str, *,
                 now: datetime | None = None, kind: str = "info",
                 priority: int = 0, notify=None, **extra) -> dict:
    """One step outcome: tonight's state, a run event and the night story
    (notify kind info = logged only, story = pushed once, act = Jeremy)."""
    now = now or datetime.utcnow()
    st = load_state(config, night)
    st["steps"][step] = {"status": status, "detail": detail,
                         "t": store.iso_z(now), **extra}
    save_state(config, st)
    _event(config, night, step, status, detail, now)
    title = STEP_TITLES.get(step, step)
    await (notify or _notify)(config, f"{title}: {status.upper()}. {detail}",
                              priority, kind)
    logger.info("guider commissioning %s %s: %s", step, status, detail)
    return st["steps"][step]


def stop_reason(config, night: str | None) -> str | None:
    """Why tonight's guider steps were stopped (the guide-star check), or
    None. guided_commission.tick fails the guiding test on it."""
    if not night:
        return None
    return load_state(config, night).get("stopped")


# --------------------------------------------------------------------------
# step 1 and 2: before dusk (pure verdicts + the tick)
# --------------------------------------------------------------------------

def darks_verdict(readiness: dict, lib_ms: list | None = None,
                  target_ms: int | None = None) -> dict:
    """{status, detail, fix} for the dark library from the readiness list
    (guided_readiness.build: a library older than the camera change or a
    FAIL is a blocker) and the library's exposures. HUMAN whenever a library
    has to be built or loaded: PHD2 has no RPC for it."""
    items = {i.get("id"): i for i in readiness.get("items") or []}
    # frame geometry blockers (Max ADU, binning, bit depth ...) come first:
    # each change invalidates the darks (guided_readiness order 20)
    geo = [i for i in readiness.get("items") or []
           if i.get("order") == 20 and i.get("level") == "blocker"]
    first = ("First, before the darks (each invalidates them): " + "; ".join(
        f"{g.get('what')} {g.get('current')}: {g.get('fix')}" for g in geo) + " "
        if geo else "")
    cam = readiness.get("camera") or {}
    name = cam.get("name") or cam.get("id") or "guide camera"
    lib = items.get("dark_library")
    age = items.get("dark_library_age")
    use = [items[k] for k in ("darks_or_defects", "have_dark") if k in items]
    gui = (first + "Roof closed, PHD2 open with the " + str(name) + " connected: "
           "Darks > Dark Library, 1 to 4 s (and the guide exposure), at the "
           "current gain and bin 1, then Darks > Bad-pixel map, then Darks > "
           "Use Dark Library (auto-loads on connect). GUI only: PHD2 has no "
           "API to build darks or load the defect map.")
    if lib and lib.get("level") == "blocker":
        return {"status": HUMAN, "detail": f"{lib.get('current')}", "fix": gui}
    if target_ms and lib_ms and int(target_ms) not in [int(x) for x in lib_ms]:
        return {"status": HUMAN,
                "detail": (f"the library has {', '.join(f'{x / 1000:g}' for x in lib_ms)} s,"
                           f" not the {target_ms / 1000:g} s guide exposure"),
                "fix": gui}
    if geo:
        return {"status": HUMAN, "detail": "; ".join(
            f"{g.get('what')}: {g.get('current')}" for g in geo), "fix": gui}
    if lib or age:
        it = lib or age
        return {"status": WARN, "detail": f"{it.get('current')}",
                "fix": gui}
    if use:
        return {"status": HUMAN,
                "detail": "; ".join(f"{u.get('what')}: {u.get('current')}" for u in use),
                "fix": "PHD2 Darks > Use Dark Library and Use Bad-pixel Map (GUI "
                       "only: no PHD2 API)."}
    if lib_ms is None:
        return {"status": HUMAN, "detail": "no PHD2 dark library file found",
                "fix": gui}
    return {"status": OK, "detail": (f"library for {name}: "
                                     f"{', '.join(f'{x / 1000:g}' for x in lib_ms)} s"
                                     " (gain / binning are not in the file: not "
                                     "verifiable here)"),
            "fix": None}


def exposure_target(readiness: dict, audit: dict | None) -> tuple[int | None, str]:
    """(target ms, why) for step 2: the readiness fast path when it has one
    (a length the library holds), else the audit's exposure row target."""
    fast = readiness.get("fast_path") or {}
    if fast.get("target_ms"):
        return int(fast["target_ms"]), "readiness fast path: a length the dark library holds"
    row = next((r for r in (audit or {}).get("rows") or [] if r.get("id") == "exposure_ms"),
               None) or {}
    t = _num(row.get("target"))
    if row.get("status") == "pass":
        return None, f"already {row.get('current')} ms (as desired)"
    if t is None:
        return None, "no exposure target in the PHD2 audit"
    return int(t), "the PHD2 audit target"


async def pre_steps(config, armer, now: datetime | None = None, *,
                    readiness=None, audit=None, lib_ms="load", apply_fn=None,
                    audit_fn=None, notify=None) -> dict:
    """Steps 1 (dark library) and 2 (guide exposure), once per night, at most
    every PRE_RETRY_S while a step is held (PHD2 closed or busy). Never
    raises."""
    now = now or datetime.utcnow()
    out = {"did": []}
    try:
        r = recipe_tonight(config, None, armer)
        if r is None:
            return out
        night = r.get("night") or store.night_of(config, now)
        st = load_state(config, night)
        if st.get("pre_done"):
            return out
        last = store.parse_z(st.get("pre_last_try"))
        if last and (now - last).total_seconds() < PRE_RETRY_S:
            return out
        st["pre_last_try"] = store.iso_z(now)
        save_state(config, st)
        skip = _skip(r.get("opts"))
        from photonscript.scheduler import guided_readiness as gr
        from photonscript.scheduler import phd2_audit as pa
        held = None
        try:
            from photonscript.scheduler.night_pilot import _calibration_busy
            cal = _calibration_busy()
            if cal:
                held = f"a calibration capture job runs on the {cal}"
        except Exception:  # noqa: BLE001
            pass
        try:
            from photonscript.telescope_agent import phd2_ops
            if phd2_ops.busy():
                held = f"PHD2 is held by {phd2_ops.owner()}"
        except Exception:  # noqa: BLE001
            pass
        if held:
            out["held"] = held
            return out
        if audit_fn is None:
            async def audit_fn():
                return await pa.run_audit(config, reason="guider commission")
        if audit is None:
            audit = pa.load_latest(config)
            at = store.parse_z((audit or {}).get("t_utc"))
            if not audit or not at or (now - at).total_seconds() > AUDIT_FRESH_H * 3600:
                try:
                    audit = await audit_fn() or audit
                except Exception as e:  # noqa: BLE001
                    logger.debug("guider night audit refresh failed: %s", e)
        if readiness is None:
            readiness = gr.build(config, now, audit=audit, night=night, guided=True)
        if lib_ms == "load":
            lib_ms = gr._lib_exposures(config, audit)
        target, why_t = exposure_target(readiness, audit)
        steps = st.get("steps") or {}
        if "darks" not in steps:
            if "darks" in skip:
                await record(config, night, "darks", SKIPPED, "skipped (recipe_opts)",
                             now=now, notify=notify)
            else:
                v = darks_verdict(readiness, lib_ms, target)
                if v["status"] == HUMAN:
                    await record(config, night, "darks", HUMAN,
                                 f"{v['detail']}. If you act: {v['fix']} If not: PHD2 "
                                 "guides without matched darks tonight (hot pixels can "
                                 "pass for stars; the PS-91 guard and the guide-star "
                                 "check watch for them).",
                                 now=now, kind="act", notify=notify)
                else:
                    await record(config, night, "darks", v["status"], v["detail"],
                                 now=now, notify=notify)
            out["did"].append("darks")
        if "exposure" not in steps:
            if "exposure" in skip:
                await record(config, night, "exposure", SKIPPED, "skipped (recipe_opts)",
                             now=now, notify=notify)
                out["did"].append("exposure")
            elif target is None:
                await record(config, night, "exposure", OK if "already" in why_t else SKIPPED,
                             why_t, now=now, notify=notify)
                out["did"].append("exposure")
            else:
                if apply_fn is None:
                    async def apply_fn():
                        return await pa.apply(config, ["exposure_ms"], dry_run=False,
                                              armer_state=str(getattr(armer, "state", "")))
                res = await apply_fn() or {}
                notes = "; ".join(str(x.get("note") or "") for x in res.get("results") or []
                                  if x.get("note"))
                if res.get("ok"):
                    to = next((x.get("to") for x in res.get("results") or []
                               if x.get("to") is not None), target)
                    await record(config, night, "exposure", OK,
                                 f"PHD2 guide exposure set to {float(to) / 1000:g} s "
                                 f"({why_t}) through the PS-89 audit apply",
                                 now=now, notify=notify, target_ms=target)
                    out["did"].append("exposure")
                    try:
                        await audit_fn()
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    retry = ("not reachable" in notes or "PHD2 is" in notes
                             or "held by" in notes)
                    dusk = store.parse_z(((getattr(armer, "plan", None) or {})
                                          .get("dusk_utc")))
                    late = dusk is not None and now >= dusk - timedelta(minutes=45)
                    if retry and not late:
                        out["held"] = notes or "PHD2 refused"
                        st = load_state(config, night)
                        st["exposure_held"] = notes
                        save_state(config, st)
                        return out
                    await record(config, night, "exposure", WARN,
                                 f"not set ({notes or 'PHD2 refused'}): PHD2 keeps its "
                                 f"exposure; the PS-90 tuner may change it later",
                                 now=now, notify=notify, target_ms=target)
                    out["did"].append("exposure")
        st = load_state(config, night)
        st["pre_done"] = all(k in st["steps"] for k in ("darks", "exposure"))
        save_state(config, st)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("guider night pre-steps failed: %s", e)
        out["error"] = str(e)
        return out


async def pre_tick(config, armer, now: datetime | None = None, **kw) -> dict:
    """guided_commission.run calls this every minute: the pre-dusk steps
    while the armer is ARMED (or RUNNING before dusk) with the recipe."""
    now = now or datetime.utcnow()
    state = str(getattr(armer, "state", "") or "")
    if state not in ("ARMED", "RUNNING"):
        return {"did": []}
    if state == "RUNNING":
        dusk = store.parse_z(((getattr(armer, "plan", None) or {}).get("dusk_utc")))
        if dusk is not None and now >= dusk:
            return {"did": []}
    return await pre_steps(config, armer, now, **kw)


# --------------------------------------------------------------------------
# step 3: the guide-star check (pure verdict + the live half)
# --------------------------------------------------------------------------

def star_verdict(readings: list[dict], min_snr: float, hfd_max: float) -> dict:
    """{status, star, why} over the tries: OK on a real star (not a hot
    pixel by the PS-91 share) at SNR >= min_snr, WARN when that star is
    clipped or wider than hfd_max (defocused), FAIL with no real star."""
    real = [r for r in readings if r.get("found")
            and (r.get("center_share") is None or r["center_share"] <= STAR_SHARE_MAX)]
    good = [r for r in real if (_num(r.get("snr")) or 0) >= min_snr]
    hot = [r for r in readings if r.get("found") and r not in real]
    if good:
        best = max(good, key=lambda r: _num(r.get("snr")) or 0)
        hfd = _num(best.get("hfd_px"))
        notes = []
        if hfd is not None and hfd > hfd_max:
            notes.append(f"HFD {hfd:g} px over {hfd_max:g}: the OAG camera looks out "
                         "of focus (guiding works but poorly: focus it by hand)")
        if best.get("clipped"):
            notes.append("the star is clipped (the PS-90 tuner shortens the exposure "
                         "when it guides)")
        return {"status": WARN if notes else OK, "star": best,
                "why": (f"star SNR {best.get('snr')}, HFD {best.get('hfd_px')} px"
                        + (": " + "; ".join(notes) if notes else ""))}
    if real:
        best = max(real, key=lambda r: _num(r.get("snr")) or 0)
        return {"status": FAIL, "star": best,
                "why": f"only faint stars (best SNR {best.get('snr')}, need {min_snr:g})"}
    if hot:
        return {"status": FAIL, "star": None,
                "why": "PHD2 only found hot pixels (no dark library or defect map)"}
    return {"status": FAIL, "star": None,
            "why": f"no star found in {len(readings)} tries"}


async def star_check(config, *, client=None, tries: int | None = None,
                     now: datetime | None = None) -> dict:
    """Loop PHD2, find_star, measure; returns {readings, exposure_ms,
    state, notes}. Holds phd2_ops ("commission"). Leaves PHD2 Stopped when
    it started the loop (NINA's StartGuiding follows)."""
    from photonscript.telescope_agent import phd2_ops
    from photonscript.telescope_agent.guide_tuner import star_stats
    tries = max(1, int(tries or _i(config, "guider_commission_star_tries", 3)))
    out = {"readings": [], "notes": []}
    own = client is None
    task = None
    if own:
        from photonscript.telescope_agent.phd2_client import PHD2Client
        client = PHD2Client(config.phd2_host, config.phd2_port, config=config)
        if not await client.connect():
            out["error"] = "PHD2 not reachable"
            return out
        task = await client.start_event_loop()
    started = False
    try:
        async with phd2_ops.hold("commission", wait_s=10):
            state = await client.get_app_state()
            out["state"] = state
            if state in ("Guiding", "Calibrating", "LostLock", "Paused"):
                await client.stop_capture()
                out["notes"].append(f"stopped PHD2 ({state}) for the check")
                state = "Stopped"
            if state != "Looping":
                await client.loop()
                started = True
            exp = await client.get_exposure() or 2000
            out["exposure_ms"] = exp
            wait = exp / 1000.0 * 3 + 10
            for i in range(tries):
                await client.wait_frames(2, timeout=wait)
                rd = {"try": i + 1, "found": False}
                try:
                    pos = await client.find_star()
                    rd["found"] = True
                    rd["pos"] = pos
                except Exception as e:  # noqa: BLE001
                    rd["note"] = str(e)[:160]
                    out["readings"].append(rd)
                    continue
                await client.wait_frames(1, timeout=wait)
                try:
                    img = await client.get_star_image()
                    s = star_stats(img, error_code=getattr(client.metrics, "error_code", None))
                except Exception as e:  # noqa: BLE001
                    s, rd["note"] = None, f"no star image: {e}"
                ls = getattr(client, "loop_star", None) or {}
                rd.update({
                    "snr": _num(ls.get("SNR")) if ls.get("SNR") is not None
                    else (s or {}).get("snr_est"),
                    "hfd_px": _num(ls.get("HFD")) if ls.get("HFD") is not None
                    else (s or {}).get("hfd_px"),
                    "center_share": (s or {}).get("center_share"),
                    "peak_frac": (s or {}).get("peak_frac"),
                    "amp_frac": (s or {}).get("amp_frac"),
                    "clipped": bool((s or {}).get("clipped"))})
                out["readings"].append(rd)
                if (_num(rd.get("snr")) or 0) >= _f(config, "guider_commission_min_snr", 10.0) \
                        and (rd.get("center_share") is None
                             or rd["center_share"] <= STAR_SHARE_MAX):
                    break
    except phd2_ops.PHD2Busy as e:
        out["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if started:
            try:
                await asyncio.wait_for(client.stop_capture(), 10)
            except Exception:  # noqa: BLE001
                pass
        if own:
            await client.disconnect()
            if task:
                task.cancel()
    return out


# --------------------------------------------------------------------------
# step 6: the Guiding Assistant equivalent (pure)
# --------------------------------------------------------------------------

def _clamp(v: float) -> float:
    lo, hi = MINMOVE_RANGE
    return round(max(lo, min(hi, v)), 2)


def ga_derive(frames: list[dict], scale: float | None) -> dict:
    """RMS and a min-move from guided frames (shared guide-frame shape: ra /
    dec raw px, drop, settling). PHD2's Guiding Assistant measures the star
    motion with guiding off; here guiding is on, so the high-frequency part
    (frame-to-frame differences / sqrt 2, which a guided lock leaves as
    seeing) stands in for it, with PHD2's GA multipliers: RA 0.65 x, Dec
    1.28 x (under 1.5"/px) or 1.65 x. An approximation, clamped to
    MINMOVE_RANGE px."""
    g = [f for f in frames or [] if not f.get("drop") and not f.get("settling")
         and _num(f.get("ra")) is not None and _num(f.get("dec")) is not None]
    drops = sum(1 for f in frames or [] if f.get("drop"))
    out = {"ok": False, "n": len(g), "drops": drops, "scale": scale}
    if len(g) < GA_MIN_FRAMES:
        out["why"] = f"only {len(g)} guided frames (need {GA_MIN_FRAMES})"
        return out
    ra = [float(f["ra"]) for f in g]
    dec = [float(f["dec"]) for f in g]
    rms_ra, rms_dec = statistics.pstdev(ra), statistics.pstdev(dec)
    hf_ra = statistics.pstdev([b - a for a, b in zip(ra, ra[1:])]) / math.sqrt(2)
    hf_dec = statistics.pstdev([b - a for a, b in zip(dec, dec[1:])]) / math.sqrt(2)
    k_dec = GA_DEC_K_FINE if (scale or 0) and scale < 1.5 else GA_DEC_K
    out.update({
        "ok": True,
        "rms_ra_px": round(rms_ra, 3), "rms_dec_px": round(rms_dec, 3),
        "rms_total_px": round(math.hypot(rms_ra, rms_dec), 3),
        "hf_ra_px": round(hf_ra, 3), "hf_dec_px": round(hf_dec, 3),
        "ra_min_move": _clamp(GA_RA_K * hf_ra),
        "dec_min_move": _clamp(k_dec * hf_dec)})
    if scale:
        out.update({"rms_ra_arcsec": round(rms_ra * scale, 2),
                    "rms_dec_arcsec": round(rms_dec * scale, 2),
                    "rms_total_arcsec": round(math.hypot(rms_ra, rms_dec) * scale, 2)})
    return out


async def ga_measure(config, *, client=None, seconds: float | None = None,
                     sleep=asyncio.sleep) -> dict:
    """Collect the guided frames for `seconds` (guider_commission_ga_s, 300)
    while NINA guides; {frames, scale, state, error}. Read only: no
    phd2_ops hold, so the PS-91 guard can still recover a lost star."""
    seconds = float(seconds or _f(config, "guider_commission_ga_s", 300.0))
    out: dict = {"frames": []}
    own = client is None
    task = None
    if own:
        from photonscript.telescope_agent.phd2_client import PHD2Client
        client = PHD2Client(config.phd2_host, config.phd2_port, config=config)
        if not await client.connect():
            out["error"] = "PHD2 not reachable"
            return out
        task = await client.start_event_loop()
    try:
        state = await client.get_app_state()
        waited = 0.0
        while state != "Guiding" and waited < 120:
            await sleep(5)
            waited += 5
            state = await client.get_app_state()
        out["state"] = state
        if state != "Guiding":
            out["error"] = f"PHD2 is {state}, not guiding"
            return out
        client.clear_frames()
        await sleep(seconds)
        out["frames"] = client.recent_frames()
        scale = getattr(client, "pixel_scale", None)
        if not scale:
            from photonscript.telescope_agent.phd2_client import guide_scale_from_config
            scale = guide_scale_from_config(config, getattr(client, "binning", None) or 1)
        out["scale"] = scale
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if own:
            await client.disconnect()
            if task:
                task.cancel()
    return out


def minmove_audit(audit: dict | None, ga: dict) -> tuple[dict, list[str]]:
    """A copy of the PS-89 audit whose ra_min_move / dec_min_move rows carry
    the derived targets (apply "api"), and the ids that need a change."""
    import copy
    a = copy.deepcopy(audit or {"rows": []})
    rows = a.setdefault("rows", [])
    by = {r.get("id"): r for r in rows}
    ids = []
    for rid in ("ra_min_move", "dec_min_move"):
        v = _num(ga.get(rid))
        if v is None:
            continue
        r = by.get(rid)
        if r is None:
            r = {"id": rid, "label": rid}
            rows.append(r)
        cur = _num(r.get("current"))
        if cur is not None and abs(cur - v) < 0.01:
            continue
        r.update(status="warn", apply="api", applicable=True, target=v)
        ids.append(rid)
    return a, ids


# --------------------------------------------------------------------------
# step 7: the narrowband focus report
# --------------------------------------------------------------------------

def _is_nb_record(rec: dict) -> bool:
    from photonscript.scheduler.nina_sequence_json import NB_FOCUS_FIELD_TAG
    from photonscript.scheduler.optics_test import parse_name
    p = parse_name(rec.get("target"))
    return bool(p and p["field"].startswith(NB_FOCUS_FIELD_TAG.strip() + " "))


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def af_positions(records: list[dict]) -> dict:
    """{filter: median FOCPOS} of the frames shot right after each filter's
    AF (offset 0): each filter's own best focus tonight."""
    from photonscript.scheduler.optics_test import parse_name
    by: dict[str, list] = {}
    for r in records:
        p = parse_name(r.get("target"))
        if not p or p["offset"] != 0:
            continue
        v = _num(r.get("focpos"))
        if v is not None:
            by.setdefault(str(r.get("filter") or p["filter"]), []).append(v)
    return {f: round(_median(v)) for f, v in by.items() if v}


def nb_decision(filters: list[dict], positions: dict, scale: float) -> dict:
    """The decision over optics_test.build_report's per-filter verdicts:
    astigmatism (the sweep filter's axis flips through focus: narrowband
    sits off the L focus and shows it; set NB focus offsets), filter tilt
    (a constant axis at the narrowband best focus while L is round: the
    filter's seating in the wheel), fine, or unclear."""
    from photonscript.scheduler import optics_test as ot
    by = {f.get("filter"): f for f in filters or []}

    def best(flt):
        f = by.get(flt) or {}
        s = next((x for x in f.get("steps") or [] if x.get("offset") == 0), None)
        if not s:
            return None
        hfr = _num(s.get("hfr_median"))
        cz = _num(((s.get("zones") or {}).get("C") or {}).get("fwhm"))
        fwhm_px = cz if cz is not None else (hfr * FWHM_PER_HFR if hfr else None)
        return {"filter": flt, "ecc": s.get("ecc_median"), "axis_deg": s.get("axis_deg"),
                "R": s.get("R"), "hfr_px": hfr,
                "fwhm_px": round(fwhm_px, 2) if fwhm_px else None,
                "fwhm_arcsec": round(fwhm_px * scale, 2) if fwhm_px else None,
                "n": s.get("n"), "focpos": positions.get(flt)}
    rows = {f: best(f) for f in ("L", "Ha", "OIII", "SII") if best(f)}
    ref = positions.get("L")
    offsets = {f: int(p - ref) for f, p in positions.items()
               if ref is not None and f != "L"}
    lb = rows.get("L") or {}
    l_round = lb.get("ecc") is not None and lb["ecc"] < ot.ROUND_ECC
    sweep = next((f for f in filters or [] if len(f.get("steps") or []) > 1
                  and f.get("filter") != "L"), None)
    out = {"best": rows, "af_positions": positions, "nb_offsets": offsets,
           "l_round": l_round, "sweep_filter": sweep and sweep.get("filter")}
    off_txt = (", ".join(f"{f} {o:+d}" for f, o in sorted(offsets.items()))
               if offsets else "not measured (no FOCPOS)")
    if sweep and sweep.get("verdict") == ot.ASTIGMATISM:
        out.update(verdict="astigmatism", headline=(
            f"{sweep['filter']} {sweep.get('headline')}: the narrowband stretch is "
            "astigmatism shown by defocus. Set the narrowband focus offsets from "
            f"tonight's AFs (steps from L: {off_txt}): System page "
            "focus_filter_offsets, and in NINA the filter offsets with "
            "UseFilterWheelOffsets on, then re-check Ha subs at their own focus."))
    elif sweep and sweep.get("verdict") == ot.CONSTANT and l_round:
        ax = sweep.get("axis_inside") or (rows.get(sweep["filter"]) or {}).get("axis_deg")
        out.update(verdict="filter_tilt", headline=(
            f"{sweep['filter']} stars keep one stretch axis (about {ax} deg) through "
            f"focus and at their own best focus while L is round (ecc {lb.get('ecc')}): "
            "the narrowband filter's seating / tilt in the wheel, not focus. Reseat "
            "the Ha / OIII / SII filters (check the retaining rings) and repeat."))
    elif sweep and sweep.get("verdict") == ot.DEFOCUS_ONLY and l_round:
        out.update(verdict="fine", headline=(
            f"{sweep['filter']} is round at its own focus (and L too): the "
            "elongation came from imaging narrowband at the L focus. Set the "
            f"narrowband focus offsets (steps from L: {off_txt})."))
    else:
        nb = rows.get("Ha") or rows.get("OIII") or {}
        out.update(verdict="unclear", headline=(
            "Not decided: " + (f"{sweep['filter']} {sweep.get('verdict')}: "
                               f"{sweep.get('headline')}" if sweep else
                               "no narrowband sweep frames")
            + (f". L ecc {lb.get('ecc')}, {nb.get('filter')} ecc {nb.get('ecc')} at "
               f"axis {nb.get('axis_deg')} deg" if nb else "")
            + ". Rerun on a steadier night."))
    return out


def nb_report(config, date: str, records: list | None = None) -> dict:
    """The narrowband focus report for a night: optics_test.build_report on
    the "Optics test NB ..." frames, each filter's AF position, and the
    decision. Saved next to tonight's state."""
    from photonscript.scheduler import optics_test as ot
    from photonscript.shared.rigs import rig_config
    if records is None:
        from photonscript.scheduler.runs import _load_subs
        records = _load_subs(config, date)
    nb = [r for r in records if (r.get("rig") or "rc16") == "rc16" and _is_nb_record(r)]
    rep = ot.build_report(config, date, records=nb)
    scale = float(getattr(rig_config(config, "rc16"), "pixel_scale_arcsec", 0.236) or 0.236)
    pos = af_positions(nb)
    dec = nb_decision(rep.get("filters") or [], pos, scale)
    out = {"date": date, "n_frames": len(nb), **dec, "optics": rep,
           "scale_arcsec_px": scale}
    try:
        store.write_json(_dir(config) / f"{date}_nb_focus.json", out)
    except OSError as e:
        logger.debug("NB focus report not saved: %s", e)
    return out


def format_nb_report(rep: dict) -> str:
    lines = [f"Narrowband focus test {rep.get('date')}: {rep.get('n_frames')} frames, "
             f"verdict {rep.get('verdict')}"]
    for f, b in (rep.get("best") or {}).items():
        lines.append(f"  {f:5s} best focus {b.get('focpos')}: ecc {b.get('ecc')}, FWHM "
                     f"{b.get('fwhm_arcsec')}\" ({b.get('fwhm_px')} px), axis "
                     f"{b.get('axis_deg')} deg (R {b.get('R')}), n {b.get('n')}")
    if rep.get("nb_offsets"):
        lines.append("  NB focus offsets from L: " + ", ".join(
            f"{k} {v:+d}" for k, v in sorted(rep["nb_offsets"].items())))
    lines.append("  " + str(rep.get("headline")))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the NINA-side steps
# --------------------------------------------------------------------------

async def _unsafe(config, nina=None) -> str | None:
    """'roof unsafe' when NINA's safety monitor reads unsafe, else None
    (unreadable is not unsafe: NINA's own Safety conditions still gate)."""
    own = nina is None
    try:
        if own:
            from photonscript.telescope_agent.nina_client import NinaClient
            nina = NinaClient(config.nina_base_url)
        info = await nina.get_safety_info() or {}
        if info.get("Connected") and info.get("IsSafe") is False:
            return "NINA's safety monitor reads unsafe"
    except Exception:  # noqa: BLE001
        return None
    finally:
        if own and nina is not None:
            try:
                await nina.close()
            except Exception:  # noqa: BLE001
                pass
    return None


async def run_step(config, step: str, *, now: datetime | None = None, night: str | None = None,
                   notify=None, client=None, nina=None, star_fn=None, selftest_fn=None,
                   ga_fn=None, apply_fn=None, audit_fn=None, records=None,
                   sleep=asyncio.sleep, wait_s: float | None = None) -> dict:
    """One NINA-side step (NINA_STEPS). {"step", "status", "stop", "detail"}.
    stop True only from "gate" (the recipe was stopped) and "star" (no guide
    star): the CLI exits EXIT_STOP and NINA skips the rest of that
    container. Never raises."""
    now = now or datetime.utcnow()
    night = night or store.night_of(config, now)
    step = str(step or "").strip().lower()
    out = {"step": step, "night": night, "status": OK, "stop": False, "detail": ""}
    try:
        if step not in NINA_STEPS:
            out.update(status=FAIL, detail=f"unknown step {step!r}: use "
                       + ", ".join(NINA_STEPS))
            return out
        if step == "gate":
            why = stop_reason(config, night)
            if why:
                out.update(status=SKIPPED, stop=True,
                           detail=f"guider steps stopped tonight: {why}")
            else:
                out["detail"] = "guider steps go ahead"
            return out
        if step in ("star", "selftest", "ga"):
            why = await _unsafe(config, nina)
            if why:
                key = {"ga": "ga"}.get(step, step)
                await record(config, night, key, SKIPPED, why, now=now, notify=notify)
                out.update(status=SKIPPED, detail=why)
                return out
        if step == "star":
            return await _step_star(config, night, now, out, notify, client, star_fn)
        if step == "selftest":
            return await _step_selftest(config, night, now, out, notify, selftest_fn)
        if step == "ga":
            return await _step_ga(config, night, now, out, notify, client, ga_fn)
        if step == "ga-apply":
            return await _step_ga_apply(config, night, now, out, notify, apply_fn, audit_fn)
        return await _step_nb_report(config, night, now, out, notify, records, sleep,
                                     wait_s)
    except Exception as e:  # noqa: BLE001
        logger.warning("guider commissioning step %s failed: %s", step, e)
        out.update(status=FAIL, detail=f"{type(e).__name__}: {e}")
        return out


async def _step_star(config, night, now, out, notify, client, star_fn):
    tries = _i(config, "guider_commission_star_tries", 3)
    r = recipe_tonight(config, night)
    if r and (r.get("opts") or {}).get("star_tries"):
        tries = int(r["opts"]["star_tries"])
    res = await (star_fn or (lambda: star_check(config, client=client, tries=tries)))()
    if res.get("error") and not res.get("readings"):
        detail = f"not checked ({res['error']}): the calibration goes ahead"
        await record(config, night, "star", WARN, detail, now=now, notify=notify)
        out.update(status=WARN, detail=detail)
        return out
    min_snr = _f(config, "guider_commission_min_snr", 10.0)
    hfd_max = _f(config, "guider_commission_hfd_max_px", 8.0)
    v = star_verdict(res.get("readings") or [], min_snr, hfd_max)
    try:
        from photonscript.scheduler import phd2_tuning
        s = v.get("star") or {}
        phd2_tuning.record(config, {"target": "guider commission star check",
                                    "filter": "L", "exposure_ms": res.get("exposure_ms"),
                                    "snr": s.get("snr"), "hfd_px": s.get("hfd_px"),
                                    "peak_frac": s.get("peak_frac"),
                                    "amp_frac": s.get("amp_frac"),
                                    "clipped": s.get("clipped"), "in_band": False})
    except Exception as e:  # noqa: BLE001
        logger.debug("star check not recorded for the tuner: %s", e)
    exp = res.get("exposure_ms")
    if v["status"] == FAIL:
        why = (f"no guide star on the OAG camera after {len(res.get('readings') or [])} "
               f"tries ({v['why']}, {float(exp or 0) / 1000:g} s frames)")
        st = load_state(config, night)
        st["stopped"] = why
        save_state(config, st)
        await record(config, night, "star", FAIL,
                     f"{why}. The OAG camera needs focusing by hand. If you act: "
                     "loop PHD2 (2 s, gain 40) on a bright star and move the OAG "
                     "camera's helical / spacer until the stars are sharp (HARDWARE.md, "
                     "ASI174MM Mini swap checklist step 1), then arm guided another "
                     "night. If not: the guider steps stop here, the guiding test fails "
                     "and the rest of tonight runs unguided; the narrowband focus test "
                     "still runs.",
                     now=now, kind="act", priority=1, notify=notify,
                     readings=res.get("readings"))
        out.update(status=FAIL, stop=True, detail=why)
        return out
    await record(config, night, "star", v["status"], v["why"], now=now,
                 kind="story" if v["status"] == WARN else "info", notify=notify,
                 readings=res.get("readings"))
    out.update(status=v["status"], detail=v["why"])
    return out


async def _step_selftest(config, night, now, out, notify, selftest_fn):
    if selftest_fn is None:
        from photonscript.telescope_agent.pulse_selftest import run_selftest

        async def selftest_fn():
            return await run_selftest(config, context="commission", slot="twilight")
    rec = await selftest_fn() or {}
    verdict = str(rec.get("verdict") or "INCONCLUSIVE").upper()
    status = {"PASS": OK, "WARN": WARN, "FAIL": FAIL, "SKIPPED": SKIPPED}.get(verdict, WARN)
    detail = f"{verdict}: " + ("; ".join(rec.get("reasons") or []) or "no reasons given")
    if status == FAIL:
        detail += " (PS-92 pages the likely causes; the guiding test still decides)"
    await record(config, night, "selftest", status, detail, now=now, notify=notify,
                 kind="story" if status == FAIL else "info")
    out.update(status=status, detail=detail)
    return out


async def _step_ga(config, night, now, out, notify, client, ga_fn):
    res = await (ga_fn or (lambda: ga_measure(config, client=client)))()
    if res.get("error") and not res.get("frames"):
        detail = f"not measured ({res['error']})"
        await record(config, night, "ga", SKIPPED, detail, now=now, notify=notify)
        out.update(status=SKIPPED, detail=detail)
        return out
    d = ga_derive(res.get("frames") or [], res.get("scale"))
    if not d.get("ok"):
        await record(config, night, "ga", WARN, d.get("why") or "no result", now=now,
                     notify=notify, ga=d)
        out.update(status=WARN, detail=d.get("why") or "")
        return out
    rms = (f"{d['rms_total_arcsec']:g}\" total (RA {d['rms_ra_arcsec']:g}\", Dec "
           f"{d['rms_dec_arcsec']:g}\")" if d.get("rms_total_arcsec") is not None
           else f"{d['rms_total_px']:g} px total")
    detail = (f"{d['n']} guided frames, {d['drops']} dropped: RMS {rms}; derived "
              f"min-move RA {d['ra_min_move']:g} px, Dec {d['dec_min_move']:g} px "
              "(GA-style, from guided frames)")
    await record(config, night, "ga", OK, detail, now=now, notify=notify,
                 kind="story", ga=d)
    out.update(detail=detail, ga=d)
    return out


async def _step_ga_apply(config, night, now, out, notify, apply_fn, audit_fn):
    st = load_state(config, night)
    ga = ((st.get("steps") or {}).get("ga") or {}).get("ga") or {}
    if not ga.get("ok"):
        detail = "no min-move measured: PHD2's min-move unchanged"
        await record(config, night, "ga_apply", SKIPPED, detail, now=now, notify=notify)
        out.update(status=SKIPPED, detail=detail)
        return out
    from photonscript.scheduler import phd2_audit as pa
    audit = pa.load_latest(config)
    copy_, ids = minmove_audit(audit, ga)
    if not ids:
        detail = "PHD2's min-move already matches the derived values"
        await record(config, night, "ga_apply", OK, detail, now=now, notify=notify)
        out["detail"] = detail
        return out
    if not bool(getattr(config, "guider_commission_minmove_apply", True)):
        detail = ("advise (guider_commission_minmove_apply off): would set "
                  + ", ".join(f"{i} {ga.get(i):g} px" for i in ids))
        await record(config, night, "ga_apply", SKIPPED, detail, now=now, notify=notify)
        out.update(status=SKIPPED, detail=detail)
        return out
    res = await (apply_fn or (lambda: pa.apply(config, ids, dry_run=False,
                                               audit=copy_)))() or {}
    ok = bool(res.get("ok"))
    notes = "; ".join(f"{x.get('id')}: {x.get('note') or x.get('to')}"
                      for x in res.get("results") or [])
    detail = (("set " if ok else "not set: ") + notes)
    await record(config, night, "ga_apply", OK if ok else WARN, detail, now=now,
                 notify=notify)
    if ok:
        try:
            await (audit_fn or (lambda: pa.run_audit(config, reason="guider commission")))()
        except Exception:  # noqa: BLE001
            pass
    out.update(status=OK if ok else WARN, detail=detail)
    return out


def expected_nb_frames(spec_nb: dict | None = None) -> int:
    from photonscript.scheduler import nina_sequence_json as g
    nb = spec_nb or {}
    rounds = g.nb_focus_rounds(nb.get("filters"), nb.get("rounds", g.NB_FOCUS_ROUNDS),
                               nb.get("sweep_filter", g.NB_FOCUS_SWEEP_FILTER))
    frames = max(1, int(nb.get("frames") or g.NB_FOCUS_FRAMES))
    return sum(len(o) for o in rounds) * frames + 2 * int(nb.get("sweep_n")
                                                          or g.NB_FOCUS_SWEEP_N)


async def _step_nb_report(config, night, now, out, notify, records, sleep, wait_s):
    r = recipe_tonight(config, night)
    want = expected_nb_frames(nb_params(config, (r or {}).get("opts")))
    waited, step = 0.0, 15.0
    limit = NB_REPORT_WAIT_S if wait_s is None else float(wait_s)
    rep = nb_report(config, night, records=records)
    while records is None and rep["n_frames"] < want and waited < limit:
        await sleep(step)
        waited += step
        rep = nb_report(config, night)
    status = OK if rep.get("verdict") in ("astigmatism", "filter_tilt", "fine") else WARN
    detail = (f"{rep['n_frames']} of {want} frames: " + str(rep.get("headline")))
    await record(config, night, "nb_focus", status, detail, now=now, notify=notify,
                 kind="story", verdict=rep.get("verdict"),
                 nb_offsets=rep.get("nb_offsets"), af_positions=rep.get("af_positions"))
    out.update(status=status, detail=detail, verdict=rep.get("verdict"))
    return out


# --------------------------------------------------------------------------
# the build (sideload / armer) and the dry run
# --------------------------------------------------------------------------

def nb_params(config, opts: dict | None = None) -> dict:
    from photonscript.scheduler import nina_sequence_json as g
    return {"filters": list(g.NB_FOCUS_FILTERS),
            "exposure_s": _f(config, "guider_commission_nb_exposure_s", g.NB_FOCUS_EXPOSURE_S),
            "rounds": int((opts or {}).get("rounds")
                          or _i(config, "guider_commission_nb_rounds", g.NB_FOCUS_ROUNDS)),
            "frames": g.NB_FOCUS_FRAMES,
            "sweep_filter": g.NB_FOCUS_SWEEP_FILTER,
            "sweep_step": _i(config, "guider_commission_sweep_step", g.NB_FOCUS_SWEEP_STEP),
            "sweep_n": g.NB_FOCUS_SWEEP_N}


def enabled_steps(opts: dict | None, guided: bool) -> set:
    """The generator's step ids (star, selftest, ga, ga_apply, nb_focus,
    nb_report) for these options; no guider step on an unguided night."""
    skip = _skip(opts)
    on = {"nb_focus", "nb_report"} - ({"nb_report", "nb_focus"} if "nb_focus" in skip else set())
    if guided:
        for s in ("star", "selftest", "ga"):
            if s not in skip:
                on.add(s)
        if "ga" in on and "ga_apply" not in skip:
            on.add("ga_apply")
    return on


def build_spec(config, *, guided: bool, opts: dict | None, nb_field: dict,
               star_field: dict | None, script: str | None) -> dict:
    return {"name": nb_field.get("name"), "guided": bool(guided), "script": script or "",
            "steps": sorted(enabled_steps(opts, guided)), "star_field": star_field,
            "nb_field": nb_field, "nb": nb_params(config, opts),
            "ga_s": _f(config, "guider_commission_ga_s", 300.0), "min_altitude": 30.0}


def _parse_at(at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(at).replace("Z", "")).replace(tzinfo=None) if at else None
    except ValueError:
        return None


def build_test(config, night_seq: dict, guided: bool, at: str = "",
               opts: dict | None = None, projects=None) -> dict:
    """The recipe container for tonight's sequence (routers/sideload
    .build_rc16_from): {"field", "test", "star_item", "spec"}. The
    guide-star check rides in tonight's PHD2_CALIBRATION slot when there is
    one (star_item), else it gets its own slew to a calibration-style field
    inside the recipe container. Raises ValueError when a guider step is
    due and deploy\\guider-commission.cmd is missing on this machine (the
    armer then falls back to the normal plan with one page)."""
    from photonscript.scheduler import nina_sequence_json as g
    from photonscript.scheduler import phd2_calibration as pc
    from photonscript.scheduler import sideload as sd
    from photonscript.scheduler.tracking_test import pick_target
    opts = opts or {}
    steps = enabled_steps(opts, guided)
    script = script_ready(config)
    if steps & {"star", "selftest", "ga"} and not script:
        raise ValueError(f"guider_commission_script {script_path(config) or '(unset)'} "
                         "not found on this machine: the guider steps cannot run")
    when = _parse_at(at) or datetime.utcnow()
    cal_in_night = sd.calibration_slot(night_seq) is not None
    star_field = None
    if "star" in steps and not cal_in_night:
        star_field = pc.pick_calibration_field(config, when)
    spec = build_spec(config, guided=guided, opts=opts,
                      nb_field={"name": "field", "ra_hours": 0.0, "dec_degrees": 0.0},
                      star_field=star_field, script=script)
    t_nb = when + timedelta(seconds=g.guider_steps_duration_s(spec))
    nb_s = g.nb_focus_duration_s(spec["nb"]) if "nb_focus" in steps else 600.0
    field = pick_target(config, t_nb, nb_s, projects)
    spec["nb_field"] = {k: field[k] for k in ("name", "ra_hours", "dec_degrees")}
    spec["name"] = field["name"]
    test = json.loads(g.generate_guider_commission_json(spec))
    star_item = (g.guider_step_item(script, "star", stop_on_fail=True)
                 if "star" in steps and cal_in_night else None)
    est = round(g.guider_commission_duration_s(spec) / 60.0)
    field = dict(field, est_minutes=est, summary=(
        f"guider steps then the narrowband focus test on {field['name']}"
        if guided else f"narrowband focus test on {field['name']} (unguided: no "
        "guider steps)") + f", about {est} min")
    return {"field": field, "test": test, "star_item": star_item, "spec": spec}


def _fmt(config, dt: datetime | None) -> dict | None:
    if dt is None:
        return None
    from photonscript.shared.localtime import to_local
    return {"utc": store.iso_z(dt), "local": to_local(config, dt).strftime("%H:%M")}


def plan(config, night: str, opts: dict | None = None, *, guided: bool = True,
         readiness: dict | None = None, projects=None) -> dict:
    """The dry run for `night` (YYYY-MM-DD, the local evening): every step
    in order with its expected time, who does it, and what it needs. Pure
    (sun math, the field pickers, the stored readiness records). Times are
    estimates: NINA's own waits (nautical dusk, the imaging gate) anchor
    them; the narrowband test and the guiding test follow."""
    from photonscript.scheduler import flat_plan
    from photonscript.scheduler import nina_sequence_json as g
    from photonscript.scheduler import phd2_calibration as pc
    from photonscript.scheduler.tracking_test import pick_target
    opts = opts or {}
    skip = _skip(opts)
    tw = flat_plan.twilight(config, night)
    naut, astro, dawn = tw.get("naut_dusk"), tw.get("astro_dusk"), tw.get("astro_dawn")
    lead = _f(config, "arm_preconfig_lead_min", 30)
    pre = astro - timedelta(minutes=lead) if astro else None
    steps = enabled_steps(opts, guided)
    rows: list[dict] = []

    def add(t, sid, who, what, detail="", dur_min=None):
        rows.append({"time": _fmt(config, t), "step": sid,
                     "title": STEP_TITLES.get(sid, sid), "who": who, "what": what,
                     "detail": detail,
                     "minutes": None if dur_min is None else round(dur_min, 1),
                     "skipped": sid in skip})

    rdy = readiness
    if rdy is None:
        try:
            from photonscript.scheduler import guided_readiness as gr
            rdy = gr.safe_build(config, night=night, guided=guided)
        except Exception:  # noqa: BLE001
            rdy = {}
    lib = [int(x * 1000) for x in ((rdy or {}).get("fast_path") or {}).get("lib_s") or []]
    if not lib:
        try:
            from photonscript.scheduler import guided_readiness as gr
            from photonscript.scheduler import phd2_audit as pa
            lib = gr._lib_exposures(config, pa.load_latest(config)) or []
        except Exception:  # noqa: BLE001
            lib = []
    v = darks_verdict(rdy or {}, lib or None, None) if rdy and rdy.get("items") is not None         else {"status": HUMAN, "detail": "readiness unavailable", "fix": ""}
    add(pre - timedelta(minutes=60) if pre else None, "darks",
        "human" if v["status"] in (HUMAN, FAIL) else "PhotonScript",
        "Check the PHD2 dark library against the connected guide camera (first "
        "service tick after the arm, retried every 10 min until pre-config). "
        "PHD2 has no API to build darks: an act push with the GUI steps when "
        "it must be built.",
        f"now: {v['status']}: {v['detail']}")
    et, ew = exposure_target(rdy or {}, None) if rdy else (None, "readiness unavailable")
    add(pre - timedelta(minutes=60) if pre else None, "exposure", "PhotonScript",
        "Set the PHD2 guide exposure to the audit target through the PS-89 apply "
        "(PHD2 Stopped / Looping only).",
        f"target: {et / 1000:g} s ({ew})" if et else f"target: {ew}")
    if pre:
        rows.append({"time": _fmt(config, pre), "step": "preconfig", "title": "Pre-config",
                     "who": "armer", "what": "Tonight's sequence built, recipe spliced, "
                     "loaded and started in NINA #1 (calibration requested from PS-93).",
                     "detail": "", "minutes": None, "skipped": False})
    t = (naut or astro) + timedelta(minutes=3) if (naut or astro) else None
    if t:
        rows.append({"time": _fmt(config, t), "step": "twilight_af",
                     "title": "Twilight autofocus", "who": "NINA",
                     "what": "Nautical dusk: slew alt 70 az 180, L autofocus.",
                     "detail": "", "minutes": 6, "skipped": False})
        t += timedelta(minutes=6)
    cal_on = guided and "calibration" not in skip and pc.cfg_mode(config) != "never"
    cal_when = (astro - timedelta(minutes=20)) if astro else t
    cal_field = pc.pick_calibration_field(config, cal_when) if (cal_on and cal_when) else None
    if guided and cal_on and t:
        add(t, "star", "PhotonScript",
            f"In the PHD2_CALIBRATION slot on {cal_field['name']} (Dec "
            f"{cal_field['dec_degrees']:+.1f}, HA {cal_field['ha_hours']:+.2f} h) after "
            f"Center: loop, find star, SNR / HFD, "
            f"{int(opts.get('star_tries') or _i(config, 'guider_commission_star_tries', 3))} "
            "tries. No star: stop, act push (focus the OAG camera by hand), the rest of "
            "tonight unguided.", "", 5)
        add(t + timedelta(minutes=5), "calibration", "PhotonScript / PHD2",
            "PS-93 calibration (ForceCalibration), graded, one retry during the "
            f"{_i(config, 'phd2_cal_hold_s', 240)} s hold.", "", 9)
        t += timedelta(minutes=14)
    elif guided and "star" in steps:
        add(t, "calibration", "PhotonScript", "Skipped: " + (
            "recipe_opts" if "calibration" in skip else "phd2_cal_mode never")
            + "; the guide-star check runs in the recipe container instead.", "")
    else:
        add(t, "calibration", "-", "Skipped: unguided night.", "")
    gate = astro or t
    rows.append({"time": _fmt(config, gate), "step": "imaging_gate", "title": "Imaging gate",
                 "who": "NINA", "what": "Astro dusk (nautical +10 min when tonight's first "
                 "set is narrowband): the Targets area starts with the recipe container.",
                 "detail": "", "minutes": None, "skipped": False})
    star_field = pc.pick_calibration_field(config, gate) if (guided and "star" in steps
                                                            and not cal_on) else None
    spec = build_spec(config, guided=guided, opts=opts,
                      nb_field={"name": "field", "ra_hours": 0, "dec_degrees": 0},
                      star_field=star_field, script="(script)")
    t = gate
    if guided and star_field:
        add(t, "star", "PhotonScript", f"Slew to {star_field['name']}, center, then the "
            "guide-star check (no PHD2_CALIBRATION slot tonight).", "", 5)
        t += timedelta(minutes=5)
    if guided and "selftest" in steps:
        add(t, "selftest", "PhotonScript", "PS-92 pulse self-test (W/E/N/S pulses, "
            "PHD2 looping).", "", g._GC_SELFTEST_S / 60)
        t += timedelta(seconds=g._GC_SELFTEST_S)
    elif guided:
        add(t, "selftest", "-", "Skipped (recipe_opts).", "")
    if guided and "ga" in steps:
        ga_s = spec["ga_s"]
        add(t, "ga", "PhotonScript", f"StartGuiding, {ga_s / 60:g} min of guiding: RMS and "
            "a GA-style min-move, StopGuiding, then "
            + ("apply RA / Dec min-move (PS-89 apply rows)." if "ga_apply" in steps
               and bool(getattr(config, "guider_commission_minmove_apply", True))
               else "record only (apply off)."), "", (ga_s + g._GC_GA_SETTLE_S) / 60)
        t += timedelta(seconds=ga_s + g._GC_GA_SETTLE_S)
    elif guided:
        add(t, "ga", "-", "Skipped (recipe_opts).", "")
    if "nb_focus" in steps:
        nb_s = g.nb_focus_duration_s(spec["nb"])
        try:
            fld = pick_target(config, t, nb_s, projects)
        except Exception:  # noqa: BLE001
            fld = {"name": "?", "alt_deg": None, "ha_hours": None}
        nb = spec["nb"]
        order = " / ".join("+".join(f.value for f in r) for r in g.nb_focus_rounds(
            nb["filters"], nb["rounds"], nb["sweep_filter"]))
        add(t, "nb_focus", "NINA + PhotonScript",
            f"On {fld.get('name')} (alt {fld.get('alt_deg')}, HA {fld.get('ha_hours')} h): "
            f"AF + {nb['exposure_s']:g} s frames per filter, rounds {order}, then "
            f"{nb['sweep_filter']} sweep "
            + ", ".join(f"{o:+d}" for o in g.nb_focus_offsets(nb["sweep_step"],
                                                              nb["sweep_n"]))
            + " steps; the report decides astigmatism vs filter tilt.",
            "", nb_s / 60)
        t += timedelta(seconds=nb_s)
    else:
        add(t, "nb_focus", "-", "Skipped (recipe_opts).", "")
    if guided:
        tmin = _f(config, "guided_commission_test_min", 15)
        cap = _f(config, "guided_commission_test_exposure_s", 300)
        rms = _f(config, "guided_commission_rms_max_arcsec", 1.2)
        tout = _f(config, "guided_commission_timeout_min", 45)
        add(t, "handover", "PhotonScript",
            f"Tonight's targets, guided, guiding test in act: RC16 subs capped at "
            f"{cap:g} s until {tmin:g} min of guided subs pass at median RMS <= "
            f"{rms:g}\" (verdict by about "
            f"{(_fmt(config, t + timedelta(minutes=tmin + 10)) or {}).get('local')}, fail "
            f"after {tout:g} min without one), then the planned lengths and the guided "
            "narrowband ladder; a fail runs the rest of tonight unguided.", "")
    else:
        add(t, "handover", "PhotonScript", "Tonight's targets, unguided.", "")
    human = [r for r in rows if r["who"] == "human"]
    human_steps = [{"step": "darks", "when": "before the roof opens",
                    "what": (v.get("fix") if v.get("status") == HUMAN and v.get("fix") else
                             "PHD2 dark library (and bad-pixel map) for the guide camera "
                             "at the current gain and bin 1: GUI only."),
                    "needed_now": v.get("status") in (HUMAN, FAIL)}]
    human_steps.append({"step": "oag_focus", "when": "only if the guide-star check fails",
                        "what": "Focus the OAG camera by hand (helical / spacer)."})
    return {"night": night, "guided": guided, "opts": opts,
            "twilight": {k: _fmt(config, tw.get(k)) for k in
                         ("sunset", "naut_dusk", "astro_dusk", "astro_dawn")},
            "preconfig": _fmt(config, pre), "calibration_field": cal_field,
            "rows": rows, "human_steps": human_steps, "human_now": human,
            "dawn": _fmt(config, dawn)}


def format_plan(p: dict) -> str:
    tw = p.get("twilight") or {}
    lines = [f"Guider commissioning night {p['night']} ("
             + ("guided" if p.get("guided") else "unguided") + ")"
             + (f", skip {', '.join(p['opts'].get('skip'))}" if (p.get("opts") or {}).get("skip")
                else ""),
             "  sunset {} | nautical dusk {} | astro dusk {} | astro dawn {} (local)".format(
                 *[(tw.get(k) or {}).get("local", "?") for k in
                   ("sunset", "naut_dusk", "astro_dusk", "astro_dawn")])]
    for r in p["rows"]:
        tm = (r.get("time") or {}).get("local") or "--:--"
        dur = f" ({r['minutes']:g} min)" if r.get("minutes") else ""
        lines.append(f"  {tm}  {r['title']}{dur} [{r['who']}]"
                     + (" SKIPPED" if r.get("skipped") else ""))
        lines.append(f"         {r['what']}")
        if r.get("detail"):
            lines.append(f"         {r['detail']}")
    lines.append("Human steps:")
    for h in p.get("human_steps") or []:
        lines.append(f"  - {h['step']} ({h['when']}): {h['what']}")
    return "\n".join(lines)

