"""PS-150 NINA watchdog state.

GET /api/nina/watch   per rig: ok | api_down | silent | not_running | idle,
                      why, since, whether it was pushed tonight, and the last
                      reads (API, process id, log file + last write, running
                      instruction). The dashboard chip reads this.

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
    return out
