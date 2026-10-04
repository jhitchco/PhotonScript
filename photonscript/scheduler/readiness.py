"""Integration readiness: the single home for calibration / readiness counting.

"Can the desktop integrate this target?" is answered here from the scope's
Library: accepted lights per filter, epoch-matched darks, bias, flats. Moved
out of app.py (PS-81) so the integration readiness endpoint, the Targets page
(PS-81) and the campaign planner (PS-30) count the same way.

API (keep it stable, PS-30 stacks on it):

    calibration_context(config, rig="rc16", *, health=None, max_age_s=0)
        -> CalibrationContext
        What calibration the Library holds for one rig: flats per canonical
        filter, bias count, and a memoized dark counter per exposure
        (ctx.darks(exp_s)). Built once, shared by every target. max_age_s > 0
        reuses a context built less than that many seconds ago (the
        calibration scan reads FITS headers and takes seconds on the scope
        PC); 0 always rebuilds.

    target_readiness(config, project, ctx=None) -> dict
        One project: lights in the Library per planned filter, darks per
        exposure, bias, ready flag and the prepare-integration command.

    readiness_report(config, projects, ctx=None) -> dict
        Body of GET /api/integration/readiness: every ACTIVE project.

    library_lights(lib, name, filter) -> int
        Accepted lights of one target + filter in the Library (canonical and
        not-yet-merged container-named folders, PS-78).

    library_fits_count(folder) -> int
        Cached FITS count of one Library folder (projects list, 2 min TTL).

Rigs: RIG_PROFILES maps a rig id to the function that fills a
CalibrationContext. "rc16" and (PS-30) "piggyback": the Piggy-600's
calibration is scanned in its own Library subtree (piggyback_library_dir)
and its watch dir, darks match its OSC epoch (calibration._pb_gain_offset,
piggyback_setpoint_c), and every OSC flat counts for the "OSC" filter (no
wheel). Its lights live in the main Library (<lib>/<target>/OSC) like the
RC16's. A context only judges the plans of its own rig (ExposurePlan.rig).

    PS-113: once a rig has a calibration QA store (calibration_qa), the
    context counts only QA-passed frames: ctx.qa == "passed".

    calibration_missing(readiness) -> list[str]
        PS-30 campaign gate: what calibration one target_readiness() result
        still lacks for its PLANNED filters and exposures (bias, darks per
        exposure, flats per filter). [] = calibrated.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RC16 = "rc16"
PIGGYBACK = "piggyback"


@dataclass
class CalibrationContext:
    """Calibration the Library holds for one rig (see calibration_context)."""
    rig: str
    lib: Path                         # Library root holding this rig's lights
    flats: dict[str, int] = field(default_factory=dict)  # canonical filter
    bias: int = 0                     # bias frames in the newest bias session
    health: dict = field(default_factory=dict)  # calibration_health() output
    dark_kwargs: dict = field(default_factory=dict)  # count_matching_darks epoch
    built_at: float = 0.0             # time.monotonic() when built
    qa: str = ""                      # PS-113: "passed" = QA-passed frames only
    _config: Any = None
    _darks: dict[float, int] = field(default_factory=dict)
    _qa_store: dict | None = None

    def darks(self, exp_s: float) -> int:
        """Epoch-matched darks for one exposure (memoized; 0 on error).
        PS-113: QA-passed darks only once the rig has a QA store."""
        e = float(exp_s)
        if e not in self._darks:
            try:
                if self._qa_store is not None:
                    from photonscript.scheduler.calibration_qa import (
                        count_passed_darks)
                    self._darks[e] = count_passed_darks(
                        self._config, self.rig, e, store=self._qa_store,
                        **_qa_epoch(self._config, self.dark_kwargs))
                else:
                    from photonscript.scheduler.calibration import (
                        count_matching_darks)
                    self._darks[e] = count_matching_darks(self._config, e,
                                                          **self.dark_kwargs)
            except Exception:  # noqa: BLE001
                self._darks[e] = 0
        return self._darks[e]


def _qa_epoch(config, dark_kwargs: dict) -> dict:
    """gain / offset / setpoint of a rig's dark epoch (count_matching_darks'
    defaults: the RC16's)."""
    return {"gain": dark_kwargs.get("gain", getattr(config, "default_gain", 200)),
            "offset": dark_kwargs.get("offset", getattr(config, "default_offset", 256)),
            "setpoint": float(dark_kwargs.get(
                "setpoint", getattr(config, "camera_setpoint_c", 0.0)))}


def _apply_qa(view, ctx: CalibrationContext) -> None:
    """PS-113: once a rig has a calibration QA store, readiness counts only
    QA-passed frames (bias in the newest bias session, flats per filter,
    darks per exposure). Without a store (QA never ran, or
    calibration_qa_mode=off) the header count stays as before."""
    from photonscript.scheduler import calibration_qa as cq
    if cq.mode(view) == "off":
        return
    store = cq.load_store(view, ctx.rig)
    if not store["frames"]:
        return
    ep = _qa_epoch(view, ctx.dark_kwargs)
    ctx.qa = "passed"
    ctx._qa_store = store
    ctx.bias = cq.count_passed_bias(view, ctx.rig, gain=ep["gain"],
                                    offset=ep["offset"], store=store)
    ctx.flats = cq.count_passed_flats(view, ctx.rig, gain=ep["gain"],
                                      offset=ep["offset"], store=store)


def _rc16_profile(config, ctx: CalibrationContext) -> None:
    from photonscript.scheduler.runs import library_root
    ctx.lib = library_root(config)
    try:
        rev = config.reverse_filter_map()
    except Exception:  # noqa: BLE001
        rev = {}
    flats: dict[str, int] = {}
    for k, v in ((ctx.health.get("FLAT") or {}).get("detail") or {}).items():
        canon = rev.get(k, k)
        flats[canon] = flats.get(canon, 0) + v
    ctx.flats = flats
    ctx.bias = (ctx.health.get("BIAS") or {}).get("count_latest") or 0
    ctx.dark_kwargs = {}  # the RC16 epoch is count_matching_darks' default


def _piggyback_profile(config, ctx: CalibrationContext) -> None:
    from photonscript.scheduler.calibration import _pb_gain_offset
    flat = ctx.health.get("FLAT") or {}
    n = flat.get("count_latest")
    if n is None:
        n = sum((flat.get("detail") or {}).values())
    ctx.flats = {"OSC": int(n or 0)}
    ctx.bias = (ctx.health.get("BIAS") or {}).get("count_latest") or 0
    gain, offset = _pb_gain_offset(config)
    ctx.dark_kwargs = {"gain": gain, "offset": offset,
                       "setpoint": float(getattr(config,
                                                 "piggyback_setpoint_c", 0.0))}


def _piggyback_view(config):
    """The piggyback's config view for calibration scans: its Library subtree
    and its own watch dir. Without a piggyback watch dir the view would fall
    back to the RC16's capture tree, so point it at the subtree instead."""
    from photonscript.shared.rigs import PIGGYBACK as _PB, rig_config
    view = rig_config(config, _PB)
    if not getattr(config, "piggyback_image_watch_dir", ""):
        try:
            view = view.model_copy(update={"image_watch_dir": view.library_dir})
        except Exception:  # noqa: BLE001 - non-pydantic config in tests
            view.image_watch_dir = view.library_dir
    return view


# rig id -> fills ctx.lib / flats / bias / dark_kwargs from ctx.health
RIG_PROFILES: dict[str, Callable[[Any, CalibrationContext], None]] = {
    RC16: _rc16_profile,
    PIGGYBACK: _piggyback_profile,
}
# rig id -> the config view its calibration is scanned with (default: config)
RIG_VIEWS: dict[str, Callable[[Any], Any]] = {
    PIGGYBACK: _piggyback_view,
}

_ctx_cache: dict[tuple, CalibrationContext] = {}
_ctx_lock = threading.Lock()


def calibration_context(config, rig: str = RC16, *, health: dict | None = None,
                        max_age_s: float = 0) -> CalibrationContext:
    """Calibration on hand for `rig` (see module doc). Unknown rig ->
    ValueError (add a RIG_PROFILES entry)."""
    rig = rig or RC16
    profile = RIG_PROFILES.get(rig)
    if profile is None:
        raise ValueError(f"no readiness profile for rig {rig!r}")
    key = (rig, str(getattr(config, "library_dir", "") or ""),
           str(getattr(config, "data_dir", "") or ""))
    now = time.monotonic()
    if max_age_s > 0 and health is None:
        with _ctx_lock:
            hit = _ctx_cache.get(key)
        if hit is not None and now - hit.built_at < max_age_s:
            return hit
    view = RIG_VIEWS.get(rig, lambda c: c)(config)
    if health is None:
        from photonscript.scheduler.calibration import calibration_health
        health = calibration_health(view)
    ctx = CalibrationContext(rig=rig, lib=Path("."), health=health,
                             built_at=now, _config=view)
    profile(view, ctx)
    try:
        _apply_qa(view, ctx)
    except Exception:  # noqa: BLE001 - readiness never breaks over QA
        pass
    if rig != RC16:
        from photonscript.scheduler.runs import library_root
        ctx.lib = library_root(config)  # its lights: the main Library
    with _ctx_lock:
        _ctx_cache[key] = ctx
    return ctx


def library_lights(lib: Path, name: str, filter_name: str,
                   dirs: list[Path] | None = None) -> int:
    """Accepted lights of target `name` in filter `filter_name` (PS-78: the
    canonical folder plus container-named ones until they are merged)."""
    from photonscript.scheduler.runs import library_target_dirs
    tdirs = dirs if dirs is not None else library_target_dirs(lib, name)
    return sum(len(list((td / filter_name).glob("*.fits")))
               for td in tdirs if (td / filter_name).exists())


def target_readiness(config, project, ctx: CalibrationContext | None = None
                     ) -> dict:
    """Readiness of one project (the shape /api/integration/readiness has
    always returned per target). Only the plans of ctx.rig count; a
    non-RC16 result also carries "rig"."""
    from photonscript.scheduler.runs import library_target_dirs
    if ctx is None:
        ctx = calibration_context(config)
    name = project.target.name
    tdirs = library_target_dirs(ctx.lib, name)
    filters: dict[str, dict] = {}
    dark_needs: dict[float, int] = {}
    for plan in project.exposure_plans:
        if (getattr(plan, "rig", RC16) or RC16) != ctx.rig:
            continue
        f = plan.filter_type.value
        filters[f] = {"accepted_in_library": library_lights(ctx.lib, name, f,
                                                            tdirs),
                      "planned": plan.count,
                      "exposure_s": plan.exposure_seconds,
                      "flats": ctx.flats.get(f, 0)}
        e = float(plan.exposure_seconds)
        if e not in dark_needs:
            dark_needs[e] = ctx.darks(e)
    lights_total = sum(v["accepted_in_library"] for v in filters.values())
    used = {f: v for f, v in filters.items() if v["accepted_in_library"] > 0}
    ready = bool(lights_total and ctx.bias
                 and all(n > 0 for e, n in dark_needs.items()
                         if any(v["exposure_s"] == e for v in used.values()))
                 and all(v["flats"] > 0 for v in used.values()))
    out = {
        "target": name,
        "filters": filters,
        "darks_by_exposure": {f"{k:g}s": v for k, v in dark_needs.items()},
        "bias": ctx.bias,
        "lights_in_library": lights_total,
        "ready": ready,
        "command": f'.\\deploy\\prepare-integration.ps1 -Target "{name}"',
    }
    if ctx.rig != RC16:
        out["rig"] = ctx.rig
        out["command"] = (f'.\\deploy\\prepare-integration-osc.ps1 '
                          f'-Name "{name}"')
    return out


def project_rigs(project) -> list[str]:
    """Rigs a project's plans use, RC16 first (an empty project: RC16)."""
    rigs = {getattr(e, "rig", RC16) or RC16 for e in project.exposure_plans}
    return sorted(rigs or {RC16}, key=lambda r: (r != RC16, r))


def calibration_missing(r: dict) -> list[str]:
    """What calibration a target_readiness() result lacks for its planned
    filters and exposures; [] = calibrated (the PS-30 `complete` gate)."""
    miss = []
    if not r.get("bias"):
        miss.append("bias")
    for exp, n in (r.get("darks_by_exposure") or {}).items():
        if not n:
            miss.append(f"darks {exp}")
    for f, v in (r.get("filters") or {}).items():
        if not v.get("flats"):
            miss.append(f"flats {f}")
    return miss


def readiness_report(config, projects: Iterable,
                     ctx: CalibrationContext | None = None) -> dict:
    """Every active project's readiness (GET /api/integration/readiness).
    PS-30: a project with Piggy-600 plans gets one more entry for that rig
    (with "rig"); its RC16 entry is listed only if it has RC16 plans."""
    if ctx is None:
        ctx = calibration_context(config)
    targets = []
    others: dict[str, CalibrationContext] = {}
    for p in projects:
        if not p.active:
            continue
        for rig in project_rigs(p):
            if rig == ctx.rig:
                targets.append(target_readiness(config, p, ctx))
                continue
            try:
                if rig not in others:
                    others[rig] = calibration_context(config, rig,
                                                      max_age_s=300)
                targets.append(target_readiness(config, p, others[rig]))
            except ValueError:
                continue  # no readiness profile for that rig
    return {"targets": targets, "library_dir": str(ctx.lib)}


# --- Library folder counts (projects list) ----------------------------------

_lib_count_cache: dict = {}  # library folder -> (monotonic t, count)
_LIB_COUNT_TTL_S = 120.0


def library_fits_count(lib: Path) -> int:
    """FITS count for one target's Library folder, cached 2 min: the projects
    list rglob'd every project's library on every poll."""
    now = time.monotonic()
    hit = _lib_count_cache.get(str(lib))
    if hit is not None and now - hit[0] < _LIB_COUNT_TTL_S:
        return hit[1]
    n = sum(1 for _ in lib.rglob("*.fits")) if lib.exists() else 0
    _lib_count_cache[str(lib)] = (now, n)
    return n
