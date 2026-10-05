"""PS-61 cooler gate endpoints.

POST /api/cooler/gate?rig=&setpoint=&label=   hold until the rig's sensor is
                                              within tolerance of the setpoint
                                              (or the timeout); NINA's
                                              ExternalScript reaches it through
                                              deploy\\cooler-gate.cmd
GET  /api/cooler/gate                         live gate state per rig, the
                                              config, recent results

The logic lives in scheduler/cooler_gate.py. Handlers lazily import
get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.post("/api/cooler/gate")
async def api_cooler_gate(request: Request, rig: str = "rc16",
                          setpoint: Optional[float] = None, label: str = ""):
    """Held while the sensor is off setpoint (at most cooler_gate_timeout_min).
    verdict SKIP = do not shoot this block; anything else = go ahead."""
    from photonscript.scheduler import cooler_gate as cg
    from photonscript.shared.rigs import rig_ids
    cfg = _cfg()
    if rig not in rig_ids(cfg):
        return {"verdict": "UNKNOWN", "rig": rig,
                "reason": f"unknown or disabled rig {rig!r}; imaging anyway"}
    return await cg.run_gate(cfg, rig, setpoint, label,
                             disconnected=request.is_disconnected)


@router.get("/api/cooler/gate")
def api_cooler_gate_status(n: int = 20):
    from photonscript.scheduler import cooler_gate as cg
    cfg = _cfg()
    return {"mode": cg.gate_mode(cfg),
            "tolerance_c": float(getattr(cfg, "cooler_gate_tolerance_c", 1.0)),
            "timeout_min": float(getattr(cfg, "cooler_gate_timeout_min", 20.0)),
            "script": str(getattr(cfg, "cooler_gate_script", "")),
            "script_found": cg.gate_script(cfg) is not None,
            "live": cg.LIVE, "recent": cg.recent(cfg, max(1, min(200, n)))}
