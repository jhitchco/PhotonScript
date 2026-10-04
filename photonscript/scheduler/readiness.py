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
CalibrationContext. Only the RC16 ("rc16") is implemented. PS-30 adds the
"piggyback" branch: its Library subtree (piggyback_library_dir) and OSC dark
epoch (calibration._pb_gain_offset, piggyback_setpoint_c), by adding one
profile function here; nothing else needs to change.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RC16 = "rc16"


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
    _config: Any = None
    _darks: dict[float, int] = field(default_factory=dict)

    def darks(self, exp_s: float) -> int:
        """Epoch-matched darks for one exposure (memoized; 0 on error)."""
        e = float(exp_s)
        if e not in self._darks:
            from photonscript.scheduler.calibration import count_matching_darks
            try:
                self._darks[e] = count_matching_darks(self._config, e,
                                                      **self.dark_kwargs)
            except Exception:  # noqa: BLE001
                self._darks[e] = 0
        return self._darks[e]


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


# rig id -> fills ctx.lib / flats / bias / dark_kwargs from ctx.health
RIG_PROFILES: dict[str, Callable[[Any, CalibrationContext], None]] = {
    RC16: _rc16_profile,
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
    if health is None:
        from photonscript.scheduler.calibration import calibration_health
        health = calibration_health(config)
    ctx = CalibrationContext(rig=rig, lib=Path("."), health=health,
                             built_at=now, _config=config)
    profile(config, ctx)
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
    always returned per target)."""
    from photonscript.scheduler.runs import library_target_dirs
    if ctx is None:
        ctx = calibration_context(config)
    name = project.target.name
    tdirs = library_target_dirs(ctx.lib, name)
    filters: dict[str, dict] = {}
    dark_needs: dict[float, int] = {}
    for plan in project.exposure_plans:
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
    return {
        "target": name,
        "filters": filters,
        "darks_by_exposure": {f"{k:g}s": v for k, v in dark_needs.items()},
        "bias": ctx.bias,
        "lights_in_library": lights_total,
        "ready": ready,
        "command": f'.\\deploy\\prepare-integration.ps1 -Target "{name}"',
    }


def readiness_report(config, projects: Iterable,
                     ctx: CalibrationContext | None = None) -> dict:
    """Every active project's readiness (GET /api/integration/readiness)."""
    if ctx is None:
        ctx = calibration_context(config)
    return {"targets": [target_readiness(config, p, ctx)
                        for p in projects if p.active],
            "library_dir": str(ctx.lib)}


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
