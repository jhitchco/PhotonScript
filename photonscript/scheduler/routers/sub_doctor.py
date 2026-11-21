"""Sub doctor endpoints (scheduler/sub_doctor.py, scheduler/sub_pairs.py).

GET /api/sub-doctor                    tonight's current diagnosis per rig
                                       (the dashboard chip): the live state
                                       file <data_dir>/sub_doctor/<night>.json
GET /api/runs/{date}/sub-doctor        the night replayed from its stored
                                       subs ("What went wrong and the fix" on
                                       the Runs page), plus the live state
GET /api/sub-doctor/pairs?date=&rig=&n=  the rig's last n subs paired with
                                       the other rig's overlapping subs and
                                       the pair verdicts (for the night pilot)

Read only: nothing here pushes, writes a verdict or talks to a device.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/sub-doctor")
def api_sub_doctor(date: str = ""):
    from photonscript.scheduler import sub_doctor as sd
    from photonscript.shared import phd2_store as store
    from photonscript.shared.rigs import rig_ids, rig_label
    cfg = _cfg()
    night = date or store.night_of(cfg)
    st = sd.load_state(cfg, night)
    rigs = {}
    for rig in rig_ids(cfg):
        r = (st.get("rigs") or {}).get(rig) or {}
        rigs[rig] = {"name": rig_label(cfg, rig), "current": r.get("current"),
                     "pushed": r.get("pushed") or [], "remedies": r.get("remedies") or [],
                     "cleared": r.get("cleared")}
    return {"night": night, "mode": sd.mode(cfg), "rigs": rigs}


@router.get("/api/runs/{date}/sub-doctor")
async def api_runs_sub_doctor(date: str, rig: str = ""):
    from photonscript.scheduler import sub_doctor as sd
    cfg = _cfg()
    rep = await asyncio.to_thread(sd.local_night, cfg, date, rig or None)
    rep["lines"] = sd.night_lines(cfg, rep)
    # the timeline is long (one row per sub with a diagnosis): keep the
    # fired causes and the counts, the CLI has the full timeline
    for r in (rep.get("rigs") or {}).values():
        r["timeline_n"] = len(r.pop("timeline", []) or [])
    return rep


@router.get("/api/sub-doctor/pairs")
async def api_sub_doctor_pairs(date: str = "", rig: str = "rc16", n: int = 3):
    from photonscript.scheduler import sub_pairs as sp
    from photonscript.scheduler.runs import _load_subs
    from photonscript.shared import phd2_store as store
    cfg = _cfg()
    night = date or store.night_of(cfg)
    subs = await asyncio.to_thread(_load_subs, cfg, night)
    return {"night": night, **sp.paired_summary(subs, rig, max(1, min(int(n), 50)))}
