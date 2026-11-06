"""PS-189 Piggy-600 cover (Wanderer Cover V4-EC) endpoints.

POST /api/cover/gate?rig=&label=   hold until the rig's cover reads Open
                                   (at most piggyback_cover_gate_timeout_s);
                                   NINA #2's ExternalScript reaches it through
                                   deploy\\cover-gate.cmd
GET  /api/cover/gate               live gate state, the config, recent results

The logic lives in scheduler/flat_cover.py. Handlers lazily import
get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.post("/api/cover/gate")
async def api_cover_gate(request: Request, rig: str = "piggyback", label: str = ""):
    """Held while the cover is not Open. verdict SKIP = do not shoot this
    image pass; anything else = go ahead."""
    from photonscript.scheduler import flat_cover as fc
    from photonscript.shared.rigs import rig_ids
    cfg = _cfg()
    if rig not in rig_ids(cfg):
        return {"verdict": "OFF", "rig": rig,
                "reason": f"unknown or disabled rig {rig!r}"}
    return await fc.run_gate(cfg, rig, label, disconnected=request.is_disconnected)


@router.get("/api/cover/gate")
def api_cover_gate_status(n: int = 20):
    from photonscript.scheduler import flat_cover as fc
    cfg = _cfg()
    return {"enabled": fc.enabled(cfg),
            "timeout_s": float(getattr(cfg, "piggyback_cover_gate_timeout_s", 180.0)),
            "script": str(getattr(cfg, "piggyback_cover_script", "")),
            "script_found": fc.gate_script(cfg) is not None,
            "panel_flat_mode": fc.panel_mode(cfg),
            "live": fc.LIVE, "recent": fc.recent(cfg, max(1, min(200, n)))}
