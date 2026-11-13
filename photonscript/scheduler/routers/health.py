"""GET /api/health (PS-57): a cheap liveness + identity probe.

Answers from memory only (no disk, no NINA, no git per request), so its
response time is the event loop's own latency. Used by `photonscript status`
to tell slow from down, and by deploy.ps1 to confirm the scope runs the SHA
that was just pushed. Fields: version, commit, started_at, uptime_s, pid,
mode, loop lag (PS-55), process context, armer state, piggyback_enabled,
config source, and (PS-58) the self-update state from update_state.json
(one small file read) so deploy.ps1 can tell "rolled back" from "slow".
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from photonscript.shared import health
from photonscript.shared.version import repo_commit

router = APIRouter()

COMMIT = repo_commit()


@router.get("/api/health")
async def api_health():
    from photonscript.scheduler.app import VERSION, get_armer, get_config
    from photonscript.shared.rigs import PIGGYBACK, rig_ids
    cfg = get_config()
    try:
        armer = str(get_armer().state)
    except Exception:  # noqa: BLE001
        armer = None
    env = Path.cwd() / ".env"
    snap = health.snapshot()
    try:
        from photonscript.shared.updater import public_state
        update = public_state(cfg)
    except Exception:  # noqa: BLE001
        update = None
    return {
        "ok": True,
        "version": VERSION,
        "commit": COMMIT,
        **snap,
        "mode": snap.get("mode") or "scheduler",
        "armer": armer,
        "piggyback_enabled": PIGGYBACK in rig_ids(cfg),
        "config_source": str(env) if env.exists() else None,
        "update": update,
    }
