"""PS-150 NINA watchdog state.

GET /api/nina/watch   per rig: ok | api_down | silent | not_running | idle,
                      why, since, whether it was pushed tonight, and the last
                      reads (API, process id, log file + last write, running
                      instruction). The dashboard chip reads this.
                      "frozen" (2026-10-16): the frozen sensor watch
                      (scheduler/frozen_sensor.py): per rig and sensor the
                      value and how long it has not changed, and the list of
                      frozen readings (warnings, never acted on).

Read only: the background loop (scheduler/nina_watchdog.run_watchdog) does
the checks; this only reports its latest view.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/nina/watch")
def api_nina_watch():
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import nina_watchdog as nw
    cfg = app_mod.get_config()
    out = nw.MONITOR.view()
    out["mode"] = nw.mode(cfg)
    out["silent_minutes"] = nw.silent_minutes(cfg)
    out["names"] = {r: nw.rig_name(cfg, r) for r in out["rigs"]}
    from photonscript.scheduler import frozen_sensor as fz
    out["frozen"] = {**fz.MONITOR.view(), "mode": fz.mode(cfg),
                     "minutes": fz.limit_min(cfg)}
    return out
