"""PS-64: the dashboard's live "where is it" panel (GET /api/night/where).

One read of what the rigs are doing right now, for the top of the dashboard:
target (and mosaic panel), filter, sub n of N and seconds left, exposure
progress, the next action from NINA #1's sequence tree, guiding mode and
PHD2 state, cooler per rig, roof / safety, the Piggy-600 (running item, sub,
exposure, PS-27 split-guard state) and the dawn / shutdown countdown.
PS-143 adds "off_target" (off_target.assess: the mount / latest RC16 solve
vs the planned center, plus the off-target monitor's debounce state) and
the armer's can_restart / restart (Restart tonight from now).

Read only: ninaAPI GETs (both NINAs, in parallel, short timeouts), the
telescope agent's state, the armer's status, the cooler gate and split
guard's in-memory state. Every source fails soft: a missing value is None
and the panel says so. The tree helpers are pure (tested offline).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# NINA items that are bookkeeping, not an action worth naming as "next"
NOISE_ITEMS = ("pushover", "annotation", "send to", "message box")
DONE_STATUSES = ("FINISHED", "SKIPPED", "DISABLED", "FAILED")
# night-loop containers of the generated sequence (nina_sequence_json)
STRUCTURAL = ("LOOP_ALL_NIGHT", "SAFE_LOOP", "TARGETS_CONTAINER",
              "RESET_EQUIPMENT_ONCE_SAFE", "UNSAFE")

_TWILIGHT_CACHE: dict = {}


# --- sequence tree (pure) ---------------------------------------------------------

def _children(node, key: str = "Items") -> list:
    v = node.get(key) if isinstance(node, dict) else None
    if isinstance(v, dict):
        v = v.get("$values")
    return [x for x in (v or []) if isinstance(x, dict)]


def _name(node) -> str:
    s = str((node or {}).get("Name") or "")
    if s.endswith("_Container"):
        s = s[: -len("_Container")]
    if not s:
        t = str((node or {}).get("$type") or "")
        s = t.split(",")[0].split(".")[-1]
    return s


def _status(node) -> str:
    return str((node or {}).get("Status") or "").upper()


def _type(node) -> str:
    return str((node or {}).get("$type") or "")


def running_chain(tree) -> list[dict]:
    """Root-to-leaf list of the RUNNING nodes (first running child at each
    level). Empty when nothing runs."""
    if isinstance(tree, dict) and "Response" in tree:
        tree = tree["Response"]
    tops = tree if isinstance(tree, list) else [tree]
    for top in tops:
        if not isinstance(top, dict):
            continue
        chain = _chain_from(top)
        if chain:
            return chain
    return []


def _chain_from(node) -> list[dict]:
    """A node with another status ends the walk; one with no Status at all
    (some ninaAPI builds) is passed through when a descendant runs."""
    st = _status(node)
    if st and st != "RUNNING":
        return []
    for ch in _children(node):
        sub = _chain_from(ch)
        if sub:
            return [node] + sub
    return [node] if st == "RUNNING" else []


def _loop_counts(node) -> tuple[int, int] | None:
    """(completed, total) from a node's LoopCondition (or its own
    Iterations / CompletedIterations fields)."""
    for src in [node] + _children(node, "Conditions"):
        it, done = src.get("Iterations"), src.get("CompletedIterations")
        if isinstance(it, (int, float)) and isinstance(done, (int, float)) and it > 0:
            return int(done), int(it)
    return None


def _exposure_time(node) -> float | None:
    for src in [node] + _children(node):
        v = src.get("ExposureTime")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _is_noise(node) -> bool:
    n = _name(node).lower()
    return any(k in n for k in NOISE_ITEMS)


def next_actions(chain: list[dict], limit: int = 3) -> list[str]:
    """The next items NINA will run after the current one: siblings after
    the running item at the deepest level, then after its parent, and so
    on up. Bookkeeping items (Pushover, annotations) are left out."""
    out: list[str] = []
    for depth in range(len(chain) - 1, 0, -1):
        parent, cur = chain[depth - 1], chain[depth]
        sibs = _children(parent)
        try:
            i = next(k for k, s in enumerate(sibs) if s is cur)
        except StopIteration:
            continue
        for s in sibs[i + 1:]:
            if _status(s) in DONE_STATUSES or _is_noise(s):
                continue
            out.append(_name(s))
            if len(out) >= limit:
                return out
    return out


def sequence_position(tree, targets_area: str = "Targets") -> dict:
    """What a NINA tree says: running (bool), leaf (deepest running item),
    target (the running DeepSkyObjectContainer, else the first running
    container under TARGETS_CONTAINER, else under the Targets area), sub
    {n, of} from the running loop that holds the leaf (a SmartExposure's
    LoopCondition), exposure_s of the running exposure, next (list)."""
    chain = running_chain(tree)
    out = {"running": bool(chain), "leaf": None, "target": None, "sub": None,
           "exposure_s": None, "next": [], "path": [_name(n) for n in chain][-4:]}
    if not chain:
        return out
    out["leaf"] = _name(chain[-1])
    dso = [n for n in chain if "DeepSkyObjectContainer" in _type(n)]
    if dso:
        out["target"] = _name(dso[-1])
    else:
        names = [_name(n) for n in chain]
        for marker in ("TARGETS_CONTAINER", targets_area):
            if marker in names:
                rest = [x for x in names[names.index(marker) + 1:-1]
                        if x not in STRUCTURAL]
                if rest:
                    out["target"] = rest[0]
                    break
    # the loop that holds the leaf: a SmartExposure, or the leaf's own
    # container (a darks block); never a target's LoopCondition(1) further up
    for k in range(len(chain) - 1, -1, -1):
        n = chain[k]
        lc = _loop_counts(n)
        if lc is not None and ("SmartExposure" in _type(n)
                               or "smart exposure" in _name(n).lower()
                               or k >= len(chain) - 2):
            done, total = lc
            out["sub"] = {"n": min(total, done + 1), "of": total}
            out["exposure_s"] = _exposure_time(n)
            break
    if out["exposure_s"] is None:
        out["exposure_s"] = _exposure_time(chain[-1])
    out["next"] = next_actions(chain)
    return out


def exposure_view(cam, seq_exposure_s: float | None,
                  now_utc: datetime | None = None) -> dict | None:
    """{"exposing", "left_s", "total_s", "elapsed_s", "progress"} from the
    camera info (night_pause time parsing) and the tree's exposure time.
    None when the camera cannot be read."""
    from photonscript.scheduler.night_pause import camera_busy, seconds_left
    busy = camera_busy(cam)
    if busy is None:
        return None
    st = str((cam or {}).get("CameraState") or "")
    out = {"exposing": bool((cam or {}).get("IsExposing")), "state": st or None,
           "left_s": None, "total_s": seq_exposure_s, "elapsed_s": None,
           "progress": None}
    left = seconds_left(cam, now_utc)
    if left is not None:
        out["left_s"] = round(left)
        if seq_exposure_s:
            el = max(0.0, seq_exposure_s - left)
            out["elapsed_s"] = round(el)
            out["progress"] = round(min(1.0, el / seq_exposure_s), 3)
    return out


def mosaic_panel(projects, target: str | None) -> dict | None:
    """{"name", "panel", "of"} when the running target is a PS-111 mosaic
    panel goal (name match, case and spacing ignored)."""
    if not target:
        return None
    key = " ".join(str(target).split()).lower()
    for p in projects or []:
        m = getattr(p, "mosaic", None)
        t = getattr(p, "target", None)
        if not (isinstance(m, dict) and m.get("id") and t is not None):
            continue
        if " ".join(str(t.name).split()).lower() == key:
            return {"name": m.get("name"), "panel": m.get("panel"), "of": m.get("of")}
    return None


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def countdowns(plan: dict | None, shutdown_due: str | None,
               now: datetime) -> dict:
    """Dawn and shutdown times plus seconds to each (naive UTC)."""
    def parse(v):
        try:
            return datetime.fromisoformat(str(v).rstrip("Z")) if v else None
        except ValueError:
            return None
    dawn = parse((plan or {}).get("dawn_utc"))
    dusk = parse((plan or {}).get("dusk_utc"))
    due = parse(shutdown_due)
    secs = lambda t: None if t is None else int((t - now).total_seconds())  # noqa: E731
    return {"night_of": (plan or {}).get("night_of"),
            "dusk_utc": _iso(dusk), "dawn_utc": _iso(dawn),
            "shutdown_due_utc": _iso(due), "dusk_in_s": secs(dusk),
            "dawn_in_s": secs(dawn), "shutdown_in_s": secs(due)}


# --- live reads ---------------------------------------------------------------------

async def _get(base: str, path: str):
    from photonscript.scheduler.night_pause import nina_get
    return await nina_get(base, path, timeout=6.0)


# PS-174: the panel is polled every 10 s by every open dashboard tab (and by
# the off-target monitor); its ninaAPI reads are shared for NINA_TTL_S so
# the NINA load does not grow with the number of callers. The armer and
# telescope state parts stay live.
NINA_TTL_S = 4.0


async def _cached_get(base: str, path: str):
    from photonscript.shared.ttl_cache import cached
    return await cached(f"where:{base}{path}", NINA_TTL_S,
                        lambda: _get(base, path))


def _tonight_plan(cfg, armer_plan: dict | None) -> dict:
    """The armer's plan when it has dawn times, else tonight's times from
    armer.watch_plan (cached per night; it computes twilight)."""
    if armer_plan and armer_plan.get("dawn_utc"):
        return armer_plan
    try:
        from photonscript.scheduler.armer import watch_plan
        from photonscript.shared.phd2_store import night_of
        key = night_of(cfg)
        if key not in _TWILIGHT_CACHE:
            _TWILIGHT_CACHE.clear()
            _TWILIGHT_CACHE[key] = watch_plan(cfg)
        return _TWILIGHT_CACHE[key]
    except Exception as e:  # noqa: BLE001
        logger.debug("where panel: twilight lookup failed: %s", e)
        return {}


def _cooler(cfg, rig: str, cam, armer_state: str) -> dict | None:
    if not isinstance(cam, dict):
        return None
    out = {"temp_c": cam.get("Temperature"),
           "setpoint_c": cam.get("TemperatureSetPoint"),
           "cooler_on": cam.get("CoolerOn"), "power": cam.get("CoolerPower"),
           "note": None}
    try:
        from photonscript.scheduler.cooler_gate import camera_row_note
        out["note"] = camera_row_note(cfg, rig, out["temp_c"], out["cooler_on"],
                                      armer_state)
    except Exception as e:  # noqa: BLE001
        logger.debug("where panel: cooler note skipped: %s", e)
    return out


def _guiding(armer_status: dict, tel: dict) -> dict:
    g = (tel or {}).get("guiding") or {}
    mode = armer_status.get("guiding") or "guided"
    st = armer_status.get("state")
    watch = armer_status.get("watch") or {}
    if st == "WATCHING" and watch.get("guided_targets") is not None:
        mode = "guided" if watch.get("guided_targets") else "unguided"
    px = g.get("units") == "px" or g.get("rms_total_arcsec") is None
    return {"mode": mode,
            "label": ("guided (PHD2)" if mode == "guided"
                      else "unguided (TPoint + ProTrack)"),
            "state": str(g.get("state") or "").lower() or None,
            "rms": g.get("rms_total_px") if px else g.get("rms_total_arcsec"),
            "rms_units": "px" if px else "arcsec"}


def _split(cfg) -> dict:
    try:
        from photonscript.scheduler import split_guard as sg
        from photonscript.shared.mount_motion import TRACKER
        last = dict(sg.LAST)
        return {"settle_gate": sg.gate_enabled(cfg),
                "abort_on_move": sg.abort_enabled(cfg),
                "motion": TRACKER.snapshot(time.time()),
                "last_gate": {k: last.get(k) for k in
                              ("verdict", "label", "waited_s", "at", "reason")
                              if k in last} or None}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def collect(cfg, armer, tel: dict | None = None, projects=None,
                  get=None, now: datetime | None = None, piggy: bool = True) -> dict:
    """The panel payload. get(base, path) -> payload|None is injectable for
    tests (default: ninaAPI GETs). piggy=False skips NINA #2 (the PS-143
    off-target monitor reads only the RC16 side). Never raises."""
    from photonscript.shared.rigs import PIGGYBACK, rig_config, rig_ids
    shared = get is None
    get = get or _cached_get
    now = now or datetime.utcnow()
    tel = tel or {}
    status = armer.status() if armer is not None else {}
    state = str(status.get("state") or "")
    base1 = cfg.nina_base_url
    rigs = list(rig_ids(cfg))
    base2 = (rig_config(cfg, PIGGYBACK).nina_base_url
             if piggy and PIGGYBACK in rigs else None)

    async def seq_tree(base):
        from photonscript.scheduler.sideload import read_sequence_state
        from photonscript.shared.ttl_cache import cached

        async def read():
            return await asyncio.wait_for(read_sequence_state(base), 8)
        try:
            tree, err = (await cached(f"where:seq:{base}", NINA_TTL_S, read)
                         if shared else await read())
        except Exception as e:  # noqa: BLE001
            return None, str(e) or type(e).__name__
        return tree, err

    reads = [get(base1, "/equipment/camera/info"),
             get(base1, "/equipment/filterwheel/info"),
             get(base1, "/equipment/safetymonitor/info"),
             seq_tree(base1),
             get(base1, "/equipment/mount/info")]   # PS-143: epoch, position
    if base2:
        reads += [get(base2, "/equipment/camera/info"), seq_tree(base2)]
    res = await asyncio.gather(*reads, return_exceptions=True)
    res = [None if isinstance(r, Exception) else r for r in res]
    cam1, fw, saf, (tree1, err1) = res[0], res[1], res[2], (res[3] or (None, "error"))
    mnt = res[4] if isinstance(res[4], dict) else {}
    cam2, (tree2, err2) = (res[5], res[6] or (None, "error")) if base2 else (None, (None, None))

    pos1 = sequence_position(tree1 or [])
    filt = None
    if isinstance(fw, dict):
        sel = fw.get("SelectedFilter")
        raw = (sel.get("Name") if isinstance(sel, dict) else sel) or None
        if raw:
            try:
                filt = cfg.reverse_filter_map().get(str(raw), str(raw))
            except Exception:  # noqa: BLE001
                filt = str(raw)
    target = pos1["target"] or tel.get("current_target")
    exp1 = exposure_view(cam1, pos1["exposure_s"])
    if exp1 is not None and exp1["progress"] is None and exp1["exposing"]:
        prog = tel.get("current_exposure_progress")
        if isinstance(prog, (int, float)) and prog > 0:
            exp1["progress"] = round(float(prog), 3)
    is_safe = None
    if isinstance(saf, dict) and saf.get("Connected"):
        is_safe = bool(saf.get("IsSafe"))
    rc16 = {"target": target, "mosaic": mosaic_panel(projects, target),
            "filter": filt or tel.get("current_filter"), "sub": pos1["sub"],
            "exposure": exp1, "running": pos1["leaf"], "next": pos1["next"],
            "sequence_running": pos1["running"] if tree1 is not None else None,
            "nina_error": err1, "path": pos1["path"],
            "mount": {"ra_hours": tel.get("mount_ra"), "dec_deg": tel.get("mount_dec"),
                      "alt_deg": tel.get("mount_alt"), "az_deg": tel.get("mount_az"),
                      "pier": tel.get("mount_side_of_pier"),
                      "tracking": tel.get("mount_tracking"),
                      "slewing": tel.get("mount_slewing"),
                      "parked": tel.get("mount_at_park")}}
    piggy = None
    if base2:
        pos2 = sequence_position(tree2 or [])
        piggy = {"enabled": True, "running": pos2["leaf"],
                 "sequence_running": pos2["running"] if tree2 is not None else None,
                 "target": pos2["target"], "sub": pos2["sub"],
                 "exposure": exposure_view(cam2, pos2["exposure_s"]),
                 "next": pos2["next"], "nina_error": err2,
                 "split": _split(cfg)}
    plan = _tonight_plan(cfg, getattr(armer, "plan", None))
    pause = status.get("pause")
    watch = status.get("watch") or {}
    off = _off_target(cfg, rc16, mnt, tel, state, projects, now)
    return {"at": _iso(now),
            "armer": {"state": state, "detail": status.get("detail"),
                      "pause": pause,
                      "watch_paused": bool(watch.get("operator_paused")),
                      "can_pause": state == "RUNNING" or (
                          state == "WATCHING" and not watch.get("operator_paused")),
                      "can_resume": state == "PAUSED_OPERATOR" or bool(
                          state == "WATCHING" and watch.get("operator_paused")),
                      # PS-143: Restart tonight from now (refusals are the
                      # armer's; WATCHING shows the button to explain why)
                      "can_restart": state in ("RUNNING", "PAUSED_OPERATOR",
                                               "WATCHING"),
                      "restart": status.get("restart")},
            "rc16": rc16, "guiding": _guiding(status, tel),
            "cooler": {"rc16": _cooler(cfg, "rc16", cam1, state),
                       "piggyback": _cooler(cfg, PIGGYBACK, cam2, state) if base2 else None},
            "safety": {"is_safe": is_safe,
                       "roof": ("open" if is_safe else "closed" if is_safe is False
                                else "unknown")},
            "piggy": piggy,
            "off_target": off,
            "dawn": countdowns(plan, status.get("shutdown_due_utc"), now)}


def _off_target(cfg, rc16: dict, mnt: dict, tel: dict, state: str, projects,
                now: datetime) -> dict | None:
    """PS-143 block: off_target.assess (separation of the mount / latest
    RC16 solve from the planned center) plus the monitor's debounce state.
    The mount position comes from NINA #1's mount info (with its epoch),
    else the telescope agent's state."""
    try:
        from photonscript.scheduler import off_target as ot
        from photonscript.shared.mount_view import epoch_label
        from photonscript.shared.phd2_store import night_of
        m = rc16.get("mount") or {}
        ra = mnt.get("RightAscension") if mnt else None
        dec = mnt.get("Declination") if mnt else None
        mount = {"ra_hours": ra if ra is not None else m.get("ra_hours"),
                 "dec_deg": dec if dec is not None else m.get("dec_deg"),
                 "epoch": epoch_label(mnt) if mnt else None,
                 "pier": m.get("pier"),
                 "slewing": (mnt.get("Slewing") if mnt and "Slewing" in mnt
                             else m.get("slewing"))}
        a = ot.assess(cfg, rc16, mount, state, projects, now,
                      night=night_of(cfg, now))
        return {**a, **ot.MONITOR.view(a.get("target"), now)}
    except Exception as e:  # noqa: BLE001
        logger.debug("where panel: off-target skipped: %s", e)
        return None
