"""PS-212: the night storyline (plan, milestones, problems) instead of event noise.

Every notify() now carries a kind (shared/pushover.py): act (needs
Jeremy), story (the night's milestones) and info (logged only). This module
builds the story pushes and reads the storyline back:

  plan heads-up   maybe_plan_headsup(armer): once per plan per night,
                  pushover_plan_lead_min (90) before pre-config, early enough
                  to change the plan before bed: targets and windows, start
                  time, sub lengths per rig, guided / unguided, moon, TPoint
                  and ProTrack, calibration owed, the evening forecast.
  imaging now     maybe_imaging_now(armer): once per night when the night
                  actually starts (astro dark and the roof open).
  first light     after_sub(): the first kept light per rig, with a
                  thumbnail and HFR / ecc / stars.
  target switch   after_sub(): the RC16 (the mount; the Piggy-600 rides it)
                  moves to a new target.
  roof            roof_tick(): debounced. A closure is logged (info); when
                  the roof reopens after pushover_roof_debounce_min (10) or
                  more, one story push with the closure's length.
  flats           dusk_flats.maybe_report_windows (flats v2): one push per
                  window with the frames actually saved; the dusk count rides
                  in "imaging now" when that push is still to come.
  end of night    end_of_night(): as soon as imaging is over (the dawn
                  shutdown, or the roof closed for good before dawn), one
                  push: per rig and target subs shot / kept / rejected with
                  the top reject reason and hours added, star quality and
                  the sub doctor's verdict, the roof timeline, flats and
                  darks saved, goal progress, what is owed or held for
                  Jeremy, and one thumbnail. filing_kind(): the dawn filing
                  push is a follow-up story only when the kept count moved
                  materially, else info.

Watchers that alarm on "nothing happened in N min" ask quiet_reason() first
(an operator run in progress, the roof closed, deliberately disarmed) and
stay quiet with a reason.

Readers: entries(config, night) for GET /api/night/story and the dashboard's
"Tonight" panel; weekly_counts(config) for GET /api/pushover/weekly.
State: <data_dir>/night_story/state.json (one dict per night, the last 4
nights kept). Never raises into a caller: a story that cannot be built is
logged and skipped.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import statistics
import threading
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from photonscript.shared import pushover as po

logger = logging.getLogger(__name__)

TITLE_PLAN = "PhotonScript tonight"
TITLE_IMAGING = "PhotonScript imaging"
TITLE_FIRST = "PhotonScript first light"
TITLE_TARGET = "PhotonScript target"
TITLE_ROOF = "PhotonScript roof"
TITLE_FLATS = "PhotonScript flats"
TITLE_END = "PhotonScript night report"

THUMB_W = 720
STATE_NIGHTS = 4
ROOF_FOR_GOOD_MIN = 40        # = armer.RESUME_MIN_REMAINING_MIN
FILING_MATERIAL_SUBS = 5      # a dawn filing follow-up needs this many subs
FILING_MATERIAL_FRAC = 0.10   # ...and this share of the kept count changed
AUTOCAL_RECENT_H = 3.0        # TPoint autocal files this fresh = a run is on
_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

act_text = po.act_text
dark_left_h = po.dark_left_h


def _parse(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).rstrip("Z")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _hm(config, when: datetime | str | None) -> str:
    """Naive UTC (or an ISO string) -> 'HH:MM' local; '?' when unknown."""
    if isinstance(when, str):
        when = _parse(when)
    if when is None:
        return "?"
    try:
        from photonscript.shared.localtime import to_local
        return to_local(config, when).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return when.strftime("%H:%MZ")


def _dur(minutes: float) -> str:
    m = int(round(max(0.0, float(minutes))))
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h} h {mm} min"
    return f"{h} h" if h else f"{mm} min"


def _label(config, rig: str) -> str:
    try:
        from photonscript.shared.rigs import rig_label
        return rig_label(config, rig)
    except Exception:  # noqa: BLE001
        return "RC16" if rig == "rc16" else str(rig)


def _night(config, now: datetime | None = None) -> str:
    return po.night_of(config, now)


def _state_file(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "night_story" / "state.json"


def _load_all(config) -> dict:
    try:
        return json.loads(_state_file(config).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def load_state(config, night: str) -> dict:
    return dict(_load_all(config).get(night) or {})


def save_state(config, night: str, st: dict) -> None:
    with _LOCK:
        allst = _load_all(config)
        allst[night] = st
        for k in sorted(allst)[:-STATE_NIGHTS]:
            allst.pop(k, None)
        try:
            p = _state_file(config)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(allst, indent=1, default=str), encoding="utf-8")
            tmp.replace(p)
        except OSError as e:
            logger.warning("night story state not saved: %s", e)


def _update(config, night: str, fn) -> dict:
    """Read-modify-write one night's state under the lock; fn(st) mutates."""
    with _LOCK:
        allst = _load_all(config)
        st = dict(allst.get(night) or {})
        fn(st)
        allst[night] = st
        for k in sorted(allst)[:-STATE_NIGHTS]:
            allst.pop(k, None)
        try:
            p = _state_file(config)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(allst, indent=1, default=str), encoding="utf-8")
            tmp.replace(p)
        except OSError as e:
            logger.warning("night story state not saved: %s", e)
        return st


def thumb_url(night: str, rel_file: str, w: int = THUMB_W) -> str:
    """The dashboard URL of a sub's thumbnail (GET /api/runs/{date}/thumb)."""
    return f"/api/runs/{night}/thumb?file={quote(str(rel_file))}&w={int(w)}"


def _thumb_path(config, night: str, rel_file: str) -> str | None:
    try:
        from photonscript.scheduler.runs import thumbnail
        p = thumbnail(config, night, rel_file, width=THUMB_W, annotate=False)
        return str(p) if p else None
    except Exception as e:  # noqa: BLE001
        logger.debug("story thumbnail skipped: %s", e)
        return None


async def _push(config, msg: str, *, title: str, kind: str = "story",
                fact: str | None = None, attachment=None, thumb=None,
                safety: bool = False, priority: int = 0) -> bool:
    try:
        return bool(await po.notify(config, msg, title=title, priority=priority,
                                    kind=kind, fact=fact, attachment=attachment,
                                    thumb=thumb, safety=safety))
    except Exception as e:  # noqa: BLE001
        logger.warning("story push %s failed: %s", title, e)
        return False


def _fit(lines: list[tuple[int, str]]) -> str:
    from photonscript.scheduler.morning_report import fit_push
    return fit_push(lines)


# --------------------------------------------------------------------------
# the evening plan heads-up
# --------------------------------------------------------------------------

def _sets_text(sets) -> str:
    """[{'f','n','exp'}...] -> 'Ha 20x300s, OIII 20x300s' (same lengths
    folded: 'R/G/B 10x120s')."""
    groups: dict[tuple, list[str]] = {}
    for x in sets or []:
        try:
            key = (int(x.get("n") or 0), int(float(x.get("exp") or 0)),
                   bool(x.get("short")))
        except (TypeError, ValueError):
            continue
        groups.setdefault(key, []).append(str(x.get("f")))
    out = []
    for (n, exp, short), fs in groups.items():
        out.append(f"{'/'.join(fs)} {n}x{exp}s" + (" HDR short" if short else ""))
    return ", ".join(out)


def _moon_text(mw: dict | None) -> str | None:
    if not mw or not mw.get("available"):
        return None
    illum = mw.get("illum_pct")
    bits = [f"Moon {illum:.0f}%" if isinstance(illum, (int, float)) else "Moon"]
    if mw.get("down_at_dusk"):
        if mw.get("rise_local_hh") is not None:
            bits.append(f"down at dusk, rises {int(mw['rise_local_hh']):02d}:"
                        f"{int(mw.get('rise_local_mm') or 0):02d}")
        else:
            bits.append("down all night")
    else:
        bits.append("up at dusk" + (f", sets {mw['set_local']}"
                                    if mw.get("set_local") else ""))
    try:
        from photonscript.scheduler.moon import broadband_deferred, broadband_from_local
        if broadband_deferred(mw):
            bf = broadband_from_local(mw)
            bits.append(f"broadband from {bf}" if bf else "broadband deferred")
    except Exception:  # noqa: BLE001
        pass
    return bits[0] + ": " + ", ".join(bits[1:]) if len(bits) > 1 else bits[0]


def plan_headsup_text(config, plan: dict, *, guided: bool, moon: dict | None = None,
                      tpoint: str | None = None, protrack: str | None = None,
                      owed: str | None = None, forecast: str | None = None,
                      recipe: str | None = None, piggy: str | None = None,
                      warnings=None, needs: str | None = None,
                      ladder: str | None = None) -> str:
    """The evening plan heads-up (pure: everything comes in)."""
    night = plan.get("night_of") or "?"
    pc = _hm(config, plan.get("preconfig_utc"))
    dusk = _hm(config, plan.get("dusk_utc"))
    dawn = _hm(config, plan.get("dawn_utc"))
    head = (f"Tonight {night}: imaging {dusk}-{dawn} ({plan.get('dark_hours')} h dark), "
            f"pre-config {pc}. Change the plan before {pc}.")
    lines: list[tuple[int, str]] = [(0, head)]
    if needs:
        lines.append((0, needs))
    mode = "guided (PHD2)" if guided else "unguided (TPoint + ProTrack)"
    rows = []
    for r in plan.get("schedule") or []:
        w0 = _hm(config, r.get("window_start_utc")) if r.get("window_start_utc") \
            else r.get("window_start")
        w1 = _hm(config, r.get("window_end_utc")) if r.get("window_end_utc") \
            else r.get("window_end")
        if r.get("fill"):
            rows.append(f"{r.get('name')} fill {w0}-{w1}")
        elif r.get("carrier"):
            # 2026-10-25 moon fill: the RC16 only carries the Piggy pointing
            rows.append(f"{r.get('name')} {w0}-{w1} (Piggy carrier, RC16 Ha)")
        else:
            st = _sets_text(r.get("filters"))
            rows.append(f"{r.get('name')} {w0}-{w1}" + (f" ({st})" if st else ""))
    if not rows and plan.get("targets"):
        rows = list(plan["targets"])
    lines.append((0, f"RC16 {mode}: " + ("; ".join(rows) if rows else "no targets planned")))
    if plan.get("moon_fill_note"):
        lines.append((1, str(plan["moon_fill_note"])))
    if recipe:
        lines.append((1, f"First: {recipe}."))
    if piggy:
        lines.append((1, piggy))
    for w in warnings or []:
        if w:
            lines.append((1, w))
    if moon:
        lines.append((2, moon))
    if tpoint:
        lines.append((1 if not guided else 3, tpoint))
    if protrack and not guided:
        lines.append((1, protrack))
    if owed:
        lines.append((2, owed))
    if ladder:
        # the night pilot's sub ladder: last nights' best rung per target
        lines.append((2, ladder))
    if forecast:
        lines.append((2, forecast))
    return _fit(lines)


def plan_signature(plan: dict, guided: bool) -> str:
    """Same targets, windows and mode = the same plan (no second heads-up
    for an identical re-arm)."""
    sig = json.dumps([plan.get("night_of"), guided,
                      [(r.get("name"), r.get("window_start_utc"), r.get("window_end_utc"))
                       for r in plan.get("schedule") or []]], default=str)
    return hashlib.sha1(sig.encode()).hexdigest()[:10]


def _story_title_tonight(config, night: str, prefix: str) -> str | None:
    for e in reversed(entries(config, night)):
        if str(e.get("title") or "").startswith(prefix):
            return e.get("title")
    return None


def _tpoint_line(config, armer) -> str | None:
    bits = []
    try:
        from photonscript.scheduler import tpoint_watch as tw
        st = tw.load_state(config) or {}
        if st.get("text"):
            bits.append(str(st["text"]))
    except Exception:  # noqa: BLE001
        pass
    pf = getattr(armer, "tpoint_pf", None) or {}
    if pf.get("level") in ("warn", "block"):
        bits.append(f"preflight {pf.get('level').upper()}")
    return ("TPoint: " + "; ".join(bits)) if bits else None


def _gate_note(config) -> str | None:
    """PS-61's notice, on Pushover now that NINA's own items are off: the
    cooler gate is on but its script is missing on this machine."""
    try:
        from photonscript.scheduler.cooler_gate import gate_mode, gate_script
        if gate_mode(config) != "off" and not gate_script(config):
            return ("Cooler gate OFF tonight: its script is not on this machine, "
                    "so lights are not held for the setpoint (PS-61)")
    except Exception:  # noqa: BLE001
        return None
    return None


def _owed_line(config) -> str | None:
    try:
        from photonscript.scheduler.calibration_owed import morning_note
        return morning_note(config)
    except Exception as e:  # noqa: BLE001
        logger.debug("owed note unavailable: %s", e)
        return None


def _ladder_line(config, plan: dict) -> str | None:
    """The night pilot's learned sub-ladder rungs for tonight's targets
    (scheduler/sub_ladder.py): a suggestion, never applied."""
    try:
        from photonscript.scheduler.sub_ladder import headsup_line
        return headsup_line(config, plan)
    except Exception as e:  # noqa: BLE001
        logger.debug("ladder line unavailable: %s", e)
        return None


def _piggy_line(config, plan: dict) -> str | None:
    if not getattr(config, "piggyback_enabled", False):
        return None
    name = _label(config, "piggyback")
    pl = plan.get("piggy_length") or {}   # piggy_length.plan(): targets[].exp_s
    lens = []
    if isinstance(pl, dict):
        for r in pl.get("targets") or []:
            try:
                lens.append(int(float(r.get("exp_s"))))
            except (TypeError, ValueError, AttributeError):
                continue
        if not lens and pl.get("base_s"):
            try:
                lens = [int(float(pl["base_s"]))]
            except (TypeError, ValueError):
                lens = []
        lens = sorted(set(lens))
    if not lens:
        try:
            lens = [int(float(getattr(config, "piggyback_exposure_s", 120)))]
        except (TypeError, ValueError):
            lens = []
    return (f"{name}: OSC {'/'.join(str(x) for x in lens)}s riding the RC16"
            if lens else f"{name}: OSC riding the RC16")


def open_plan_window(config, plan: dict) -> None:
    """At arm: until tonight's plan heads-up goes out (or astro dark), a
    non-safety act waits for it (pushover.plan_window_open) and the heads-up
    lists it, so the checks an arm runs (settings, ProTrack, TPoint, PHD2,
    apps) make one push, not five. Never raises."""
    try:
        night = (plan or {}).get("night_of")
        if not night or not plan.get("dusk_utc"):
            return
        _update(config, night, lambda st: st.update(
            plan_window={"dusk": plan["dusk_utc"],
                         "preconfig": plan.get("preconfig_utc")}))
    except Exception as e:  # noqa: BLE001
        logger.debug("plan window not opened: %s", e)


def plan_window_open(config, now: datetime | None = None) -> bool:
    now = po._utc_naive(now)
    st = load_state(config, _night(config, now))
    w = st.get("plan_window") or {}
    dusk = _parse(w.get("dusk"))
    return bool(dusk and not st.get("plan_t") and now < dusk)


def held_for_plan(config, night: str) -> list[dict]:
    """Tonight's acts that waited for the plan heads-up (newest per title)."""
    by_title: dict = {}
    for e in entries(config, night):
        if e.get("kind") == "act" and e.get("reason") == "for-plan":
            by_title[e.get("title")] = e
    return list(by_title.values())


def _needs_line(config, held: list[dict], pc: str) -> str | None:
    if not held:
        return None
    bits = []
    for e in held:
        t = str(e.get("title") or "").replace("PhotonScript ", "")
        bits.append(f"{t}: {str(e.get('text') or '')[:170]}")
    return f"Needs you before pre-config {pc}: " + " | ".join(bits)


async def maybe_plan_headsup(armer, now: datetime | None = None) -> bool:
    """Once per plan per night, pushover_plan_lead_min before pre-config
    (an arm inside that lead sends it at once); not after astro dark (the
    "imaging now" push says it then)."""
    cfg = armer.config
    plan = getattr(armer, "plan", None) or {}
    pc = _parse(plan.get("preconfig_utc"))
    dusk = _parse(plan.get("dusk_utc"))
    if pc is None or not plan.get("night_of"):
        return False
    now = now or datetime.utcnow()
    lead = float(getattr(cfg, "pushover_plan_lead_min", 90) or 0)
    if now < pc - timedelta(minutes=lead) or (dusk and now >= dusk):
        return False
    guided = bool(armer._use_guiding())
    sig = plan_signature(plan, guided)
    night = plan["night_of"]
    if load_state(cfg, night).get("plan_sig") == sig:
        return False
    _update(cfg, night, lambda st: st.update(plan_sig=sig, plan_t=po._utc_naive(now)
                                             .isoformat() + "Z"))
    try:
        from photonscript.scheduler.moon import moon_window_tonight
        mw = await asyncio.to_thread(moon_window_tonight, cfg)
    except Exception:  # noqa: BLE001
        mw = None
    owed = None
    try:
        owed = await asyncio.wait_for(asyncio.to_thread(_owed_line, cfg), timeout=60)
    except Exception:  # noqa: BLE001
        owed = None
    pt = getattr(armer, "protrack_state", None)
    protrack = None if guided else (
        "ProTrack: on" if pt == "on" else
        "ProTrack: OFF, tick Activate ProTrack in TheSky" if pt == "off" else
        "ProTrack: not confirmed yet" if pt else None)
    rec = getattr(armer, "recipe", None) or {}
    held = held_for_plan(cfg, night)
    text = plan_headsup_text(
        cfg, plan, guided=guided, moon=_moon_text(mw), tpoint=_tpoint_line(cfg, armer),
        protrack=protrack, owed=owed,
        forecast=_story_title_tonight(cfg, night, "Tonight:"),
        recipe=rec.get("label"), piggy=_piggy_line(cfg, plan),
        warnings=[_gate_note(cfg)],
        needs=_needs_line(cfg, held, _hm(cfg, plan.get("preconfig_utc"))),
        ladder=_ladder_line(cfg, plan))
    sent = await _push(cfg, text, title=TITLE_PLAN, fact=f"plan:{night}:{sig}",
                       priority=1 if held else 0)
    if sent and held:   # told: the same problems do not buzz again tonight
        po.mark_facts(cfg, [po.fact_key("act", e.get("title"), e.get("text"))
                            for e in held], now)
    return sent


# --------------------------------------------------------------------------
# imaging now
# --------------------------------------------------------------------------

def imaging_pending(config, now: datetime | None = None) -> bool:
    """Armed tonight and "imaging now" still to come (before astro dark):
    the dusk flats count then rides in that push instead of its own."""
    now = po._utc_naive(now)
    st = load_state(config, _night(config, now))
    dusk = _parse((st.get("plan_window") or {}).get("dusk"))
    return bool(dusk and not st.get("imaging_t") and now < dusk)


def _flats_line(config, night: str, prefix: str) -> str | None:
    """Tonight's flats v2 window count line ("Dusk flats ..." / "Dawn
    flats ...") from the storyline."""
    for e in reversed(entries(config, night)):
        if e.get("title") == TITLE_FLATS and str(e.get("text") or "").startswith(prefix):
            return e.get("text")
    return None


def _dusk_flats_line(config, night: str) -> str | None:
    return _flats_line(config, night, "Dusk flats")


def imaging_now_text(config, plan: dict, *, guided: bool, now: datetime,
                     flats: str | None = None) -> str:
    sched = [r for r in plan.get("schedule") or [] if not r.get("fill")]
    first = sched[0] if sched else {}
    mode = "guided" if guided else "unguided"
    bits = [f"Imaging now ({_hm(config, now)}): RC16 {mode} on "
            f"{first.get('name') or (plan.get('targets') or ['?'])[0]}"
            + (f" ({_sets_text(first.get('filters'))})" if first.get("filters") else "")]
    pig = _piggy_line(config, plan)
    if pig:
        bits.append(pig)
    st = plan.get("stats") or {}
    n = len(sched) or len(plan.get("targets") or [])
    tail = f"{n} target(s) until {_hm(config, plan.get('dawn_utc'))}"
    if st.get("est_integration_h") is not None:
        tail += f", ~{st['est_integration_h']} h planned"
    bits.append(tail)
    out = ". ".join(bits) + "."
    return out + ("\n" + flats if flats else "")


async def maybe_imaging_now(armer, now: datetime | None = None,
                            safe: bool | None = True) -> bool:
    """Once per night: astro dark has come and the roof is open."""
    cfg = armer.config
    plan = getattr(armer, "plan", None) or {}
    dusk, dawn = _parse(plan.get("dusk_utc")), _parse(plan.get("dawn_utc"))
    now = now or datetime.utcnow()
    if not dusk or now < dusk or (dawn and now >= dawn) or safe is not True:
        return False
    night = plan.get("night_of") or _night(cfg, now)
    if load_state(cfg, night).get("imaging_t"):
        return False
    _update(cfg, night, lambda st: st.update(imaging_t=now.isoformat() + "Z"))
    text = imaging_now_text(cfg, plan, guided=bool(armer._use_guiding()), now=now,
                            flats=_dusk_flats_line(cfg, night))
    return await _push(cfg, text, title=TITLE_IMAGING, fact=f"imaging:{night}")


# --------------------------------------------------------------------------
# first light and target switches (the telescope agent, after each sub)
# --------------------------------------------------------------------------

_PANEL = re.compile(r"\s*(panel|p)\s*\d+\s*$", re.I)


def _canon(name) -> str | None:
    if not name:
        return None
    try:
        from photonscript.shared.target_names import canonical_target
        c = canonical_target(name) or str(name)
    except Exception:  # noqa: BLE001
        c = str(name)
    return _PANEL.sub("", c).strip() or c


def first_light_text(config, rig: str, rec: dict, now: datetime | None = None) -> str:
    def num(k, nd=2):
        v = rec.get(k)
        try:
            return f"{float(v):.{nd}f}"
        except (TypeError, ValueError):
            return "?"
    exp = rec.get("exp_s")
    try:
        exp = f"{float(exp):g}s"
    except (TypeError, ValueError):
        exp = "?"
    when = _parse(rec.get("time")) or now or datetime.utcnow()
    return (f"First light {_label(config, rig)} ({_hm(config, when)}): "
            f"{rec.get('target')} {rec.get('filter') or ''} {exp}, "
            f"HFR {num('hfr')} px, ecc {num('ecc')}, {rec.get('stars') or '?'} stars.")


def _target_tally(config, night: str, rig: str, target: str) -> tuple[int, int, float]:
    """(kept, rejected, kept hours) of a rig's lights on a target tonight."""
    try:
        from photonscript.scheduler.runs import _load_subs
        from photonscript.shared.qa_rules import is_test_record
        subs = _load_subs(config, night)
    except Exception:  # noqa: BLE001
        return 0, 0, 0.0
    k = r = 0
    sec = 0.0
    for s in subs:
        if (s.get("rig") or "rc16") != rig or is_test_record(s):
            continue
        if _canon(s.get("target")) != target:
            continue
        if s.get("passed_qa"):
            k += 1
            try:
                sec += float(s.get("exp_s") or 0)
            except (TypeError, ValueError):
                pass
        else:
            r += 1
    return k, r, sec / 3600.0


def _carrier(config, night: str | None, target) -> bool:
    """The RC16 target is an unguided moon-fill carrier (carrier_quiet)."""
    try:
        from photonscript.scheduler.carrier_quiet import carrier_for
        return carrier_for(config, target=target, night=night) is not None
    except Exception:  # noqa: BLE001
        return False


async def after_sub(config, night: str | None, rec: dict, rig: str = "rc16") -> dict:
    """First light per rig (a kept light, with a thumbnail) and RC16 target
    switches. Returns what was pushed ({"first_light": bool, "switch": bool})."""
    out = {"first_light": False, "switch": False}
    try:
        from photonscript.shared.qa_rules import is_test_record
        if not night or not rec or is_test_record(rec) or not rec.get("target"):
            return out
    except Exception:  # noqa: BLE001
        return out
    target = _canon(rec.get("target"))
    st = load_state(config, night)
    fl = st.get("first_light") or {}
    if rig not in fl and rec.get("passed_qa"):
        def mark(s):
            s.setdefault("first_light", {})[rig] = {
                "t": rec.get("time"), "file": rec.get("file"), "target": target,
                "hfr": rec.get("hfr"), "ecc": rec.get("ecc")}
        _update(config, night, mark)
        path = await asyncio.to_thread(_thumb_path, config, night, rec.get("file") or "")
        out["first_light"] = await _push(
            config, first_light_text(config, rig, rec), title=TITLE_FIRST,
            fact=f"first-light:{night}:{rig}", attachment=path,
            thumb=thumb_url(night, rec.get("file") or ""))
    if rig != "rc16":
        return out
    prev = (st.get("target") or {}).get(rig)
    _update(config, night, lambda s: s.setdefault("target", {}).__setitem__(rig, target))
    if prev and prev != target:
        k, r, h = await asyncio.to_thread(_target_tally, config, night, rig, prev)
        when = _parse(rec.get("time")) or datetime.utcnow()
        exp = rec.get("exp_s")
        try:
            exp = f" {float(exp):g}s"
        except (TypeError, ValueError):
            exp = ""
        if _carrier(config, night, prev):
            # 2026-10-25: an unguided moon-fill carrier is not judged
            from photonscript.scheduler.carrier_quiet import summary_text
            tally = summary_text(k + r, kept=k)
        else:
            tally = f"{k} kept / {r} rejected, {h:.1f} h"
        msg = (f"RC16 now on {target} ({rec.get('filter') or '?'}{exp}, "
               f"{_hm(config, when)}) after {prev}: {tally}.")
        out["switch"] = await _push(config, msg, title=TITLE_TARGET,
                                    fact=f"target:{night}:{prev}->{target}")
    return out


# --------------------------------------------------------------------------
# the roof, debounced
# --------------------------------------------------------------------------

def _debounce_min(config) -> float:
    try:
        return max(0.0, float(getattr(config, "pushover_roof_debounce_min", 10.0)))
    except (TypeError, ValueError):
        return 10.0


async def roof_tick(config, safe: bool | None, now: datetime, plan: dict | None) -> dict | None:
    """Follow the roof inside the dark window. A closure opens on the first
    unsafe read and closes on the first safe one; at least the debounce
    long, the reopen is one story push with its length. None reads (the
    monitor unreadable) change nothing. Returns the closure that just ended."""
    plan = plan or {}
    dusk, dawn = _parse(plan.get("dusk_utc")), _parse(plan.get("dawn_utc"))
    if safe is None or not dusk or now < dusk or (dawn and now > dawn):
        return None
    night = plan.get("night_of") or _night(config, now)
    st = load_state(config, night)
    roof = st.get("roof") or {}
    closed = roof.get("closed_since")
    if safe is False and not closed:
        def _c(s):
            s.setdefault("roof", {})["closed_since"] = now.isoformat() + "Z"
        _update(config, night, _c)
        await _push(config, f"Roof closed at {_hm(config, now)} local.",
                    title=TITLE_ROOF, kind="info")
        return None
    if safe is True and closed:
        t0 = _parse(closed) or now
        mins = (now - t0).total_seconds() / 60.0
        spell = [closed, now.isoformat() + "Z", round(mins, 1)]

        def _o(s):
            r = s.setdefault("roof", {})
            r["closed_since"] = None
            r.setdefault("closures", []).append(spell)
        _update(config, night, _o)
        left = ((dawn - now).total_seconds() / 3600.0) if dawn else None
        msg = (f"Roof reopened {_hm(config, now)} after {_dur(mins)} closed "
               f"(since {_hm(config, t0)}); imaging resumes"
               + (f", {left:.1f} h of dark left." if left is not None else "."))
        # before "imaging now" went out the reopen is info: that push says
        # the night started
        long = mins >= _debounce_min(config) and bool(st.get("imaging_t"))
        await _push(config, msg, title=TITLE_ROOF, kind="story" if long else "info",
                    fact=f"roof-reopen:{night}:{closed}")
        return {"closed": closed, "opened": spell[1], "minutes": spell[2]}
    return None


def roof_timeline(config, night: str, dusk: datetime | None, dawn: datetime | None,
                  now: datetime | None = None) -> str:
    """'Roof: open all night' / 'Roof: closed 22:10-23:22 (1 h 12 min);
    closed 04:25 to the end'."""
    roof = load_state(config, night).get("roof") or {}
    spells = [(_parse(a), _parse(b)) for a, b, *_ in roof.get("closures") or []]
    if roof.get("closed_since"):
        spells.append((_parse(roof["closed_since"]), None))
    if not spells:
        return "Roof: open all night"
    parts = []
    for a, b in spells:
        if b is None:
            parts.append(f"closed {_hm(config, a)} to the end")
        else:
            parts.append(f"closed {_hm(config, a)}-{_hm(config, b)} "
                         f"({_dur((b - a).total_seconds() / 60.0)})")
    return "Roof: " + "; ".join(parts)


def roof_closed_for_good(plan: dict | None, now: datetime, safe_eff: bool | None) -> bool:
    """The roof is closed with less dark left than a resume needs."""
    dawn = _parse((plan or {}).get("dawn_utc"))
    return (safe_eff is False and dawn is not None
            and (dawn - now) < timedelta(minutes=ROOF_FOR_GOOD_MIN))


# --------------------------------------------------------------------------
# the end-of-night report
# --------------------------------------------------------------------------

def _median(xs) -> float | None:
    vals = []
    for x in xs:
        try:
            if x is not None:
                vals.append(float(x))
        except (TypeError, ValueError):
            continue
    return statistics.median(vals) if vals else None


def _calib_frames(config, night: str) -> dict:
    """{rig: {"DARK": n, "BIAS": n}} frames written under tonight's (and the
    next) date folder of each rig's watch dir."""
    out: dict = {}
    try:
        from photonscript.shared.rigs import rig_config, rig_ids
    except Exception:  # noqa: BLE001
        return out
    base = datetime.strptime(night, "%Y-%m-%d")
    for rig in rig_ids(config):
        try:
            watch = Path(rig_config(config, rig).image_watch_dir)
        except Exception:  # noqa: BLE001
            continue
        c: Counter = Counter()
        for d in (night, (base + timedelta(days=1)).strftime("%Y-%m-%d")):
            root = watch / d
            if not root.is_dir():
                continue
            for f in root.rglob("*.fits"):
                parts = {p.upper() for p in f.relative_to(root).parts[:-1]}
                for k in ("DARK", "BIAS"):
                    if k in parts or (k + "S") in parts:
                        c[k] += 1
        if c:
            out[rig] = dict(c)
    return out


def _doctor_verdict(config, night: str, rig: str) -> str | None:
    try:
        from photonscript.scheduler import sub_doctor as sd
        r = (sd.load_state(config, night).get("rigs") or {}).get(rig) or {}
        causes = Counter(h.get("cause") for h in r.get("history") or [] if h.get("cause"))
    except Exception:  # noqa: BLE001
        return None
    if not causes:
        return "sub doctor: healthy"
    return "sub doctor: " + ", ".join(c for c, _ in causes.most_common(2))


def _held_acts(config, night: str) -> list[str]:
    out = []
    for e in entries(config, night):
        if e.get("kind") == "act" and e.get("reason") == "act-after-cutoff":
            out.append(f"{e.get('t', '')[11:16]}Z {e.get('text', '')[:140]}")
    return out


def end_of_night_report(config, night: str, *, reason: str,
                        subs: list[dict] | None = None, projects=None,
                        plan: dict | None = None, shutdown_failed=None,
                        extra=None, dawn_line: str | None = None,
                        now: datetime | None = None) -> dict:
    """{"text", "kept", "shot", "thumb_file", "lines"}: the end-of-night
    push (pure apart from the reads it names). subs / projects for tests."""
    from photonscript.shared.qa_rules import is_test_record
    from photonscript.shared.rigs import rig_ids
    now = now or datetime.utcnow()
    plan = plan or {}
    if subs is None:
        try:
            from photonscript.scheduler.runs import _load_subs
            subs = _load_subs(config, night)
        except Exception:  # noqa: BLE001
            subs = []
    if projects is None:
        try:
            from photonscript.scheduler.calibration_plan import load_projects
            projects = load_projects(config)
        except Exception:  # noqa: BLE001
            projects = []
    try:
        from photonscript.scheduler.morning_report import _goal_index
        goals = _goal_index(projects)
    except Exception:  # noqa: BLE001
        goals = {}
    try:
        from photonscript.shared.target_names import target_key
    except Exception:  # noqa: BLE001
        def target_key(x):
            return str(x).lower()
    # 2026-10-25 quiet carriers: the RC16 subs of an unguided moon-fill
    # carrier are not judged (one line instead of a kept / rejected tally;
    # their rejects are not in the headline's count)
    try:
        from photonscript.scheduler import carrier_quiet as cq
        quiet = cq.quiet_targets(config, night=night,
                                 plan=plan if plan.get("moon_fill") is not None else None)
    except Exception:  # noqa: BLE001
        cq, quiet = None, {}
    lights = [s for s in subs if not is_test_record(s)]
    carrier_subs = [s for s in lights if quiet and cq.is_carrier_sub(s, quiet)]
    if carrier_subs:
        lights = [s for s in lights if not cq.is_carrier_sub(s, quiet)
                  or s.get("passed_qa")]
    kept_all = [s for s in lights if s.get("passed_qa")]
    hours_all = sum(float(s.get("exp_s") or 0) for s in kept_all) / 3600.0
    lines: list[tuple[int, str]] = [(0, (
        f"Night {night} over ({reason}, {_hm(config, now)}): {len(kept_all)} kept "
        f"of {len(lights)} subs, {hours_all:.1f} h added."))]
    for rig in rig_ids(config):
        mine = [s for s in lights if (s.get("rig") or "rc16") == rig]
        if rig == "rc16" and carrier_subs:
            names = []
            for s in carrier_subs:
                n = _canon(s.get("target")) or "?"
                if n not in names:
                    names.append(n)
            lines.append((1, cq.summary_text(
                len(carrier_subs), names,
                kept=sum(1 for s in carrier_subs if s.get("passed_qa")))))
            mine = [s for s in mine if not cq.is_carrier_sub(s, quiet)]
        if not mine:
            continue
        by_t: dict[str, list[dict]] = {}
        for s in mine:
            by_t.setdefault(_canon(s.get("target")) or "?", []).append(s)
        tparts = []
        for t, ss in sorted(by_t.items(), key=lambda kv: -len(kv[1])):
            k = [s for s in ss if s.get("passed_qa")]
            rej = [s for s in ss if not s.get("passed_qa")]
            h = sum(float(s.get("exp_s") or 0) for s in k) / 3600.0
            top = Counter(d for s in rej for d in (s.get("drivers") or ["unspecified"]))
            bit = f"{t} {len(ss)} shot, {len(k)} kept, {len(rej)} rej"
            if top:
                d, n = top.most_common(1)[0]
                bit += f" (top {d} {n})"
            bit += f", +{h:.1f} h"
            p = goals.get(target_key(t)) if t != "?" else None
            if p is not None:
                try:
                    bit += (f" (goal {float(p.total_integration_hours):.1f}/"
                            f"{float(p.budget_hours):.0f} h)")
                except (TypeError, ValueError, AttributeError):
                    pass
            tparts.append(bit)
        lines.append((1, f"{_label(config, rig)}: " + "; ".join(tparts)))
        kept = [s for s in mine if s.get("passed_qa")]
        hfr, ecc = _median(s.get("hfr") for s in kept), _median(s.get("ecc") for s in kept)
        q = []
        if hfr is not None:
            q.append(f"median HFR {hfr:.2f} px")
        if ecc is not None:
            q.append(f"ecc {ecc:.2f}")
        v = _doctor_verdict(config, night, rig)
        if v:
            q.append(v)
        if q:
            lines.append((2, f"{_label(config, rig)} stars: " + ", ".join(q)))
    lines.append((2, roof_timeline(config, night, _parse(plan.get("dusk_utc")),
                                   _parse(plan.get("dawn_utc")), now)))
    try:
        from photonscript.scheduler import dusk_flats_check as dfc
        # flats v2's window counts first (the pushes' own lines), else
        # the older morning lines
        dusk_ln = _dusk_flats_line(config, night) or dfc.morning_line(config, night)
        dawn_ln = (dawn_line or _flats_line(config, night, "Dawn flats")
                   or dfc.dawn_morning_line(config, night))
        for ln in (dusk_ln, dawn_ln):
            if ln:
                lines.append((2, ln))
    except Exception as e:  # noqa: BLE001
        logger.debug("end of night: flats lines unavailable: %s", e)
    cal = _calib_frames(config, night)
    if cal:
        lines.append((3, "Darks saved: " + "; ".join(
            f"{_label(config, r)} " + ", ".join(f"{n} {k.lower()}" for k, n in sorted(c.items()))
            for r, c in cal.items())))
    for pri, text in extra or []:
        if text:
            lines.append((int(pri), str(text)))
    held = _held_acts(config, night)
    if shutdown_failed:
        lines.append((0, "Needs you: shutdown step FAILED: " + "; ".join(shutdown_failed)))
    if held:
        lines.append((1, "Held overnight for you: " + " | ".join(held[:3])))
    owed = _owed_line(config)
    if owed:
        lines.append((2, owed))
    best = None
    rc = [s for s in kept_all if (s.get("rig") or "rc16") == "rc16"] or kept_all
    try:
        best = min((s for s in rc if s.get("hfr") is not None and s.get("file")),
                   key=lambda s: float(s["hfr"]) * (1.0 + float(s.get("ecc") or 0)))
    except (ValueError, TypeError):
        best = None
    if best is None:
        fl = load_state(config, night).get("first_light") or {}
        f = (fl.get("rc16") or next(iter(fl.values()), None) or {})
        best = {"file": f.get("file")} if f.get("file") else None
    return {"text": _fit(lines), "kept": len(kept_all), "shot": len(lights),
            "hours": round(hours_all, 2), "thumb_file": (best or {}).get("file"),
            "lines": [t for _p, t in lines]}


async def end_of_night(config, night: str | None, *, reason: str,
                       plan: dict | None = None, shutdown_failed=None,
                       extra=None, dawn_line: str | None = None,
                       now: datetime | None = None) -> bool:
    """The end-of-night story push, once per night."""
    now = now or datetime.utcnow()
    night = night or _night(config, now)
    if load_state(config, night).get("end_t"):
        return False
    try:
        rep = await asyncio.wait_for(asyncio.to_thread(
            end_of_night_report, config, night, reason=reason, plan=plan,
            shutdown_failed=shutdown_failed, extra=extra, dawn_line=dawn_line,
            now=now), timeout=120)
    except Exception as e:  # noqa: BLE001
        logger.warning("end of night report failed: %s", e)
        rep = {"text": f"Night {night} over ({reason}). Report unavailable: {e}",
               "kept": None, "shot": None, "thumb_file": None}
    _update(config, night, lambda s: s.update(
        end_t=now.isoformat() + "Z", end_kept=rep.get("kept"), end_reason=reason))
    path = url = None
    if rep.get("thumb_file"):
        path = await asyncio.to_thread(_thumb_path, config, night, rep["thumb_file"])
        url = thumb_url(night, rep["thumb_file"])
    sent = await _push(config, rep["text"], title=TITLE_END, fact=f"end:{night}",
                       attachment=path, thumb=url)
    if shutdown_failed:   # a real shutdown failure also pages on its own
        await _push(config, act_text(
            "Dawn shutdown step FAILED: " + "; ".join(shutdown_failed),
            "Stop & Make Safe or NINA by hand parks and warms the rig now",
            "the rig may sit unparked or cooled all day"),
            title="PhotonScript shutdown warning", kind="act", safety=True)
    return sent


END_WAIT_MAX_MIN = 30   # the report waits at most this long for the dawn count


def dawn_count_pending(config, night: str | None) -> bool:
    """A flats v2 dawn window planned tonight and not counted yet."""
    try:
        from photonscript.scheduler import flat_plan as fp
        p = fp.load_plan(config, night, "dawn") if night else None
    except Exception:  # noqa: BLE001
        return False
    return bool(p and p.get("end") and not p.get("reported"))


def defer_end(config, night: str, *, reason: str, extra=None, shutdown_failed=None,
              now: datetime | None = None) -> None:
    """The dawn shutdown is done but the dawn flats count comes about 10 min
    later (flats v2 reports 12 min after the window's end, the shutdown 3):
    hold the report so it carries the count; maybe_report_windows (or the
    END_WAIT_MAX_MIN fallback, flush_stale_end) sends it."""
    now = now or datetime.utcnow()
    _update(config, night, lambda s: s.update(end_pending={
        "t": now.isoformat() + "Z", "reason": reason,
        "extra": [list(x) for x in extra or []],
        "shutdown_failed": list(shutdown_failed or [])}))


async def flush_end(config, night: str, dawn_line: str | None = None,
                    now: datetime | None = None) -> bool:
    """Send a held end-of-night report (once). False when none is held."""
    pend = load_state(config, night).get("end_pending")
    if not pend:
        return False
    _update(config, night, lambda s: s.update(end_pending=None))
    return await end_of_night(config, night, reason=pend.get("reason") or "dawn",
                              extra=[tuple(x) for x in pend.get("extra") or []],
                              shutdown_failed=pend.get("shutdown_failed") or None,
                              dawn_line=dawn_line, now=now)


async def flush_stale_end(config, now: datetime | None = None) -> bool:
    """The fallback: a held report older than END_WAIT_MAX_MIN goes out."""
    now = now or datetime.utcnow()
    for night in sorted(_load_all(config))[-2:]:
        pend = load_state(config, night).get("end_pending") or {}
        t = _parse(pend.get("t"))
        if t and now - t >= timedelta(minutes=END_WAIT_MAX_MIN):
            return await flush_end(config, night, now=now)
    return False


def filing_kind(config, night: str, kept_now: int | None) -> str:
    """'story' when the dawn filing moved the kept count materially against
    the end-of-night report (or no report went out), else 'info'."""
    st = load_state(config, night)
    if not st.get("end_t"):
        return "story"
    before = st.get("end_kept")
    if kept_now is None or before is None:
        return "info"
    delta = abs(int(kept_now) - int(before))
    if delta >= FILING_MATERIAL_SUBS and delta >= FILING_MATERIAL_FRAC * max(1, int(before)):
        return "story"
    return "info"


# --------------------------------------------------------------------------
# watchers: a known reason to stay quiet
# --------------------------------------------------------------------------

def _autocal_recent(config, now: datetime) -> str | None:
    """A TPoint autocal (operator) run writing its CSV or import files."""
    try:
        from photonscript.telescope_agent import tpoint_import as ti
        cands = list((Path(getattr(config, "data_dir", ".")) / "runs")
                     .glob("*_tpoint_autocal.csv"))
        idir = Path(getattr(config, "tpoint_import_dir", "") or ti.DEFAULT_IMPORT_DIR)
        if idir.is_dir():
            cands += list(idir.glob("*.dat"))
        newest = max((datetime.utcfromtimestamp(p.stat().st_mtime) for p in cands),
                     default=None)
    except Exception:  # noqa: BLE001
        return None
    if newest and now - newest <= timedelta(hours=AUTOCAL_RECENT_H):
        return f"TPoint autocal run in progress (files written {_hm(config, newest)} local)"
    return None


def _roof_closed(config, now: datetime) -> str | None:
    try:
        from photonscript.shared.safety_history import _last_recorded, history_path
        last = _last_recorded(history_path(config))
    except Exception:  # noqa: BLE001
        return None
    return "roof closed" if str(last or "").lower() in ("unsafe", "false") else None


def quiet_reason(config, now: datetime | None = None, armer=None,
                 checks: tuple = ("operator", "roof", "disarmed")) -> str | None:
    """A known reason why "nothing happened in N min" is expected, else
    None: an operator run in progress (a TPoint autocal run, a watched
    sideload, a calibration capture), the roof closed, or the night
    deliberately disarmed."""
    now = now or datetime.utcnow()
    if "operator" in checks:
        why = _autocal_recent(config, now)
        if why:
            return why
        if armer is not None and str(getattr(armer, "state", "")) == "WATCHING":
            return "operator sideload being watched"
        try:
            from photonscript.scheduler.armer import Armer
            busy = Armer._calibration_busy()
            if busy:
                return f"calibration capture running ({busy})"
        except Exception:  # noqa: BLE001
            pass
    if "roof" in checks:
        why = _roof_closed(config, now)
        if why:
            return why
    if "disarmed" in checks:
        try:
            from photonscript.scheduler.arm_memory import manual_disarm_tonight
            if manual_disarm_tonight(config, now):
                return "disarmed by hand tonight"
        except Exception:  # noqa: BLE001
            pass
    return None


# --------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------

def entries(config, night: str, kinds=None) -> list[dict]:
    """The night's storyline, time-ordered (kinds: an iterable to keep)."""
    p = po.story_path(config, night)
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if kinds and r.get("kind") not in kinds:
                continue
            out.append(r)
    except OSError:
        return []
    out.sort(key=lambda r: str(r.get("t") or ""))
    return out


def story(config, night: str | None = None, kinds=None, include_held: bool = True) -> dict:
    """The GET /api/night/story payload."""
    night = night or _night(config)
    ks = set(kinds) if kinds else None
    rows = entries(config, night, ks)
    counts = Counter((r.get("kind"), bool(r.get("sent"))) for r in rows)
    return {"night": night,
            "entries": [{"t": r.get("t"), "kind": r.get("kind"), "title": r.get("title"),
                         "text": r.get("text"), "sent": r.get("sent"),
                         "reason": r.get("reason"), "thumb": r.get("thumb"),
                         **({"safety": True} if r.get("safety") else {})}
                        for r in rows if include_held or r.get("sent")],
            "pushed": sum(n for (k, s), n in counts.items() if s),
            "by_kind": {k: {"pushed": counts.get((k, True), 0),
                            "logged": counts.get((k, False), 0)} for k in po.KINDS}}


def weekly_counts(config, days: int = 7, now: datetime | None = None) -> dict:
    """Push counts per night and kind over the last `days` nights, from the
    audit log (notifications.jsonl; rows before PS-212 have no kind and
    count as "legacy"), plus the top titles pushed."""
    from datetime import timezone
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=int(days))
    nights: dict = {}
    titles: Counter = Counter()
    held: Counter = Counter()
    try:
        text = po._audit_path(config).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        try:
            r = json.loads(line)
            ts = datetime.fromisoformat(r["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        if ts < cutoff:
            continue
        kind = r.get("kind") or "legacy"
        n = nights.setdefault(po.night_of(config, ts), {"sent": 0, "held": 0, "by_kind": {}})
        k = n["by_kind"].setdefault(kind, {"sent": 0, "held": 0})
        if r.get("sent"):
            n["sent"] += 1
            k["sent"] += 1
            titles[r.get("title") or "?"] += 1
        else:
            n["held"] += 1
            k["held"] += 1
            held[r.get("reason") or "?"] += 1
    sent = [v["sent"] for v in nights.values()]
    return {"days": int(days), "nights": dict(sorted(nights.items())),
            "sent_total": sum(sent),
            "sent_per_night_max": max(sent) if sent else 0,
            "top_titles": titles.most_common(10),
            "held_reasons": held.most_common(10)}
