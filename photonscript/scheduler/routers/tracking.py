"""PS-168 endpoint: the nightly tracking drift (scheduler/tracking_drift.py).

GET /api/tracking/drift?date=YYYY-MM-DD&nights=N   per window and night
    summary (RA / Dec drift "/min, detrended RMS, smear per sub length at
    each rig scale, mode, excluded windows and why), the trend over the last
    N nights keyed by the TPoint model, and the PS-169 recommended unguided
    sub length per rig. Default date: the newest guide log's night.

GET /api/tracking/unguided-limits?rig=&refresh=false   the learned unguided
    limits per rig and sky region (scheduler/unguided_limits.py): the store,
    refreshed when missing or with refresh=true, plus flat table rows for
    the Guiding page.

Plain def: FastAPI runs it in a worker thread (it parses guide logs).
Handlers lazily import get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/tracking/drift")
def api_tracking_drift(date: str = "", nights: int | None = None):
    from photonscript.scheduler.tracking_drift import report
    return report(_cfg(), date, nights)


@router.get("/api/tracking/unguided-limits")
def api_unguided_limits(rig: str = "", refresh: bool = False):
    from photonscript.scheduler import unguided_limits as ul
    cfg = _cfg()
    doc = ul.refresh(cfg) if refresh else ul.table(cfg)
    if not doc:
        return {"mode": ul.mode(cfg), "probe": ul.probe_mode(cfg), "rigs": {}}
    cov = ul.tpoint_coverage(cfg)
    rigs = {}
    for rg, t in (doc.get("rigs") or {}).items():
        if rig and rg != rig:
            continue
        rigs[rg] = {**{k: v for k, v in t.items() if k != "evidence"},
                    "rows": ul.table_rows(t, cov),
                    "dark_lengths": ul.learned_dark_lengths(cfg, rg, doc)}
    return {"refreshed_at": doc.get("refreshed_at"), "since": doc.get("since"),
            "mode": ul.mode(cfg), "probe": ul.probe_mode(cfg), "rigs": rigs,
            "tpoint_coverage": (cov or {}).get("overall")}
