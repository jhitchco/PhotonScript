"""Scheduler Web Application — FastAPI-based dashboard and API.

Accessible from anywhere (phone, laptop, etc.) to monitor and control
the remote telescope orchestration.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (
    AgentMessage, AgentRole, CelestialTarget, ExposurePlan, FilterType,
    ImagingProject, NinaSequenceTarget, TelescopeState,
)
from photonscript.shared.astronomy import (
    get_seasonal_targets, rank_targets_for_night, get_twilight_times,
    compute_visibility_window,
)
from photonscript.shared.messagebus import get_message_bus
from photonscript.scheduler.target_planner import (
    plan_night_sequence, create_project_from_target, suggest_exposure_plan,
)
from photonscript.scheduler.nina_sequence import generate_nina_xml, build_sequence_for_night
from photonscript.scheduler.nina_sequence_json import generate_nina_json

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="PhotonScript Scheduler", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# In-memory state (persisted to DB on changes)
_config: Optional[PhotonScriptConfig] = None
_projects: dict[str, ImagingProject] = {}
_telescope_state: TelescopeState = TelescopeState()
_ws_clients: list[WebSocket] = []


def get_config() -> PhotonScriptConfig:
    global _config
    if _config is None:
        _config = PhotonScriptConfig()
    return _config


# ---------------------------------------------------------------------------
# WebSocket for live updates
# ---------------------------------------------------------------------------

async def broadcast_state():
    """Push current state to all connected WebSocket clients."""
    state = {
        "telescope": _telescope_state.model_dump(mode="json"),
        "projects": {pid: p.model_dump(mode="json") for pid, p in _projects.items()},
        "timestamp": datetime.utcnow().isoformat(),
    }
    msg = json.dumps(state, default=str)
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        # Send initial state
        state = {
            "telescope": _telescope_state.model_dump(mode="json"),
            "projects": {pid: p.model_dump(mode="json") for pid, p in _projects.items()},
        }
        await ws.send_text(json.dumps(state, default=str))
        while True:
            data = await ws.receive_text()
            # Handle client commands if needed
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# Message bus listener — receive updates from other agents
# ---------------------------------------------------------------------------

async def on_agent_message(msg: AgentMessage):
    """Handle incoming messages from telescope agent, librarian, etc."""
    global _telescope_state

    if msg.msg_type == "telescope_state_update":
        _telescope_state = TelescopeState(**msg.payload)
        await broadcast_state()

    elif msg.msg_type == "image_captured":
        # Update project progress
        project_id = msg.payload.get("project_id")
        filter_type = msg.payload.get("filter_type")
        if project_id in _projects and filter_type:
            for plan in _projects[project_id].exposure_plans:
                if plan.filter_type.value == filter_type:
                    plan.acquired += 1
                    break
            _projects[project_id].compute_completion()
        await broadcast_state()

    elif msg.msg_type == "image_quality_report":
        await broadcast_state()

    elif msg.msg_type == "transfer_complete":
        await broadcast_state()


def setup_message_listeners():
    bus = get_message_bus()
    bus.subscribe("telescope_state_update", on_agent_message)
    bus.subscribe("image_captured", on_agent_message)
    bus.subscribe("image_quality_report", on_agent_message)
    bus.subscribe("transfer_complete", on_agent_message)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    config = get_config()
    obs = config.get_observatory()
    now = datetime.utcnow()
    twilight = get_twilight_times(obs, now)
    month = now.month
    seasonal = get_seasonal_targets(month)
    ranked = rank_targets_for_night(seasonal, obs, now)

    return templates.TemplateResponse(request, "dashboard.html", {
        "observatory": obs,
        "telescope_state": _telescope_state,
        "projects": list(_projects.values()),
        "twilight": twilight,
        "tonight_targets": ranked[:10],
        "month": now.strftime("%B"),
    })


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    return {
        "telescope": _telescope_state.model_dump(mode="json"),
        "active_projects": len([p for p in _projects.values() if p.active]),
        "total_projects": len(_projects),
    }


@app.get("/api/projects")
async def api_list_projects():
    return [p.model_dump(mode="json") for p in _projects.values()]


@app.post("/api/projects")
async def api_create_project(request: Request):
    data = await request.json()
    target = CelestialTarget(**data.get("target", {}))
    project = create_project_from_target(target)
    if "exposure_plans" in data:
        project.exposure_plans = [ExposurePlan(**ep) for ep in data["exposure_plans"]]
    if "priority" in data:
        project.priority = data["priority"]
    _projects[project.id] = project
    await broadcast_state()
    return project.model_dump(mode="json")


@app.get("/api/projects/{project_id}")
async def api_get_project(project_id: str):
    if project_id not in _projects:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    return _projects[project_id].model_dump(mode="json")


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str):
    if project_id in _projects:
        del _projects[project_id]
        await broadcast_state()
    return {"status": "deleted"}


@app.get("/api/tonight")
async def api_tonight_plan():
    """Get the planned targets and sequence for tonight."""
    config = get_config()
    obs = config.get_observatory()
    now = datetime.utcnow()
    twilight = get_twilight_times(obs, now)
    month = now.month

    # Use existing projects if available, otherwise suggest seasonal
    if _projects:
        projects = list(_projects.values())
    else:
        seasonal = get_seasonal_targets(month)
        ranked = rank_targets_for_night(seasonal, obs, now)
        projects = [create_project_from_target(r["target"]) for r in ranked[:5]]

    sequence_targets = plan_night_sequence(projects, config, now)

    return {
        "date": now.strftime("%Y-%m-%d"),
        "twilight": {
            k: v.isoformat() if v else None
            for k, v in twilight.items()
        },
        "targets": [
            {
                "name": st.name,
                "ra": st.ra_hours,
                "dec": st.dec_degrees,
                "exposures": [e.model_dump(mode="json") for e in st.exposures],
            }
            for st in sequence_targets
        ],
    }


@app.get("/api/tonight/sequence.xml")
async def api_tonight_sequence_xml():
    """Generate and download the NINA sequence XML for tonight."""
    config = get_config()
    now = datetime.utcnow()

    if _projects:
        projects = list(_projects.values())
    else:
        obs = config.get_observatory()
        seasonal = get_seasonal_targets(now.month)
        ranked = rank_targets_for_night(seasonal, obs, now)
        projects = [create_project_from_target(r["target"]) for r in ranked[:5]]

    targets = plan_night_sequence(projects, config, now)
    sequence = build_sequence_for_night(
        name=f"PhotonScript_{now.strftime('%Y%m%d')}",
        targets=targets,
    )
    xml_content = generate_nina_xml(sequence)
    return HTMLResponse(
        content=xml_content,
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename={sequence.name}.xml"},
    )


@app.get("/api/tonight/sequence.json")
async def api_tonight_sequence_json():
    """Generate and download the NINA Advanced Sequencer JSON for tonight.

    This is the preferred format for NINA's Advanced Sequencer, using
    .NET $type annotations that NINA can load directly.
    """
    config = get_config()
    now = datetime.utcnow()

    if _projects:
        projects = list(_projects.values())
    else:
        obs = config.get_observatory()
        seasonal = get_seasonal_targets(now.month)
        ranked = rank_targets_for_night(seasonal, obs, now)
        projects = [create_project_from_target(r["target"]) for r in ranked[:5]]

    targets = plan_night_sequence(projects, config, now)
    sequence = build_sequence_for_night(
        name=f"PhotonScript_{now.strftime('%Y%m%d')}",
        targets=targets,
    )
    json_content = generate_nina_json(sequence)
    return HTMLResponse(
        content=json_content,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={sequence.name}.json"},
    )


@app.get("/api/seasonal/{month}")
async def api_seasonal_targets(month: int):
    """Get seasonal target suggestions for a given month (1-12)."""
    config = get_config()
    obs = config.get_observatory()
    now = datetime.utcnow()
    targets = get_seasonal_targets(month)
    ranked = rank_targets_for_night(targets, obs, now)
    return [
        {
            "name": r["target"].name,
            "catalog_id": r["target"].catalog_id,
            "type": r["target"].object_type,
            "tier": r.get("tier", "good"),
            "visibility_hours": r["visibility"]["hours"],
            "transit_time": r["visibility"].get("transit_time", "").isoformat() if r["visibility"].get("transit_time") else None,
            "recommended_hours": r["target"].recommended_total_hours,
        }
        for r in ranked
    ]


@app.get("/api/telescope/state")
async def api_telescope_state():
    return _telescope_state.model_dump(mode="json")


@app.post("/api/telescope/command")
async def api_telescope_command(request: Request):
    """Send a command to the telescope agent."""
    data = await request.json()
    bus = get_message_bus()
    await bus.publish(AgentMessage(
        sender=AgentRole.SCHEDULER,
        recipient=AgentRole.TELESCOPE,
        msg_type="command",
        payload=data,
    ))
    return {"status": "sent", "command": data.get("action")}


@app.on_event("startup")
async def startup():
    setup_message_listeners()
    logger.info("PhotonScript Scheduler started on %s:%d", get_config().scheduler_host, get_config().scheduler_port)


# ---------------------------------------------------------------------------
# System page: preflight + web config editor
# ---------------------------------------------------------------------------

# Curated editable config fields: (attr, env var, label, group, type, secret, needs_restart)
_CONFIG_FIELDS = [
    ("observatory_name", "PS_OBSERVATORY_NAME", "Observatory name", "Observatory", "str", False, False),
    ("observatory_lat", "PS_OBSERVATORY_LAT", "Latitude (deg N)", "Observatory", "float", False, False),
    ("observatory_lon", "PS_OBSERVATORY_LON", "Longitude (deg E)", "Observatory", "float", False, False),
    ("observatory_elev", "PS_OBSERVATORY_ELEV", "Elevation (m)", "Observatory", "float", False, False),
    ("observatory_tz", "PS_OBSERVATORY_TZ", "Timezone", "Observatory", "str", False, True),
    ("nina_base_url", "PS_NINA_BASE_URL", "NINA Advanced API URL", "NINA", "str", False, True),
    ("image_watch_dir", "PS_IMAGE_WATCH_DIR", "NINA image output dir", "NINA", "str", False, True),
    ("nina_logs_dir", "PS_NINA_LOGS_DIR", "NINA logs dir", "NINA", "str", False, False),
    ("phd2_host", "PS_PHD2_HOST", "PHD2 host", "PHD2", "str", False, True),
    ("phd2_port", "PS_PHD2_PORT", "PHD2 port", "PHD2", "int", False, True),
    ("default_gain", "PS_DEFAULT_GAIN", "Camera gain", "Imaging", "int", False, False),
    ("default_offset", "PS_DEFAULT_OFFSET", "Camera offset", "Imaging", "int", False, False),
    ("camera_setpoint_c", "PS_CAMERA_SETPOINT_C", "Cooling setpoint (°C)", "Imaging", "float", False, False),
    ("guided_default", "PS_GUIDED_DEFAULT", "Guided by default", "Imaging", "bool", False, False),
    ("pixel_scale_arcsec", "PS_PIXEL_SCALE_ARCSEC", "Pixel scale (\"/px)", "Imaging", "float", False, False),
    ("quality_fwhm_max", "PS_QUALITY_FWHM_MAX", "Max FWHM (arcsec)", "Quality", "float", False, False),
    ("quality_eccentricity_max", "PS_QUALITY_ECCENTRICITY_MAX", "Max eccentricity", "Quality", "float", False, False),
    ("quality_tracking_rms_max", "PS_QUALITY_TRACKING_RMS_MAX", "Max guide RMS (arcsec)", "Quality", "float", False, False),
    ("quality_corner_spread_max", "PS_QUALITY_CORNER_SPREAD_MAX", "Max corner FWHM spread", "Quality", "float", False, False),
    ("pushover_user_key", "PS_PUSHOVER_USER_KEY", "Pushover user key", "Nanny / Alerts", "str", True, False),
    ("pushover_api_token", "PS_PUSHOVER_API_TOKEN", "Pushover API token", "Nanny / Alerts", "str", True, False),
    ("consecutive_reject_limit", "PS_CONSECUTIVE_REJECT_LIMIT", "Consecutive rejects before severe alert", "Nanny / Alerts", "int", False, False),
    ("auto_abort_on_severe", "PS_AUTO_ABORT_ON_SEVERE", "Auto-abort on severe (enable only once trusted)", "Nanny / Alerts", "bool", False, False),
    ("heartbeat_minutes", "PS_HEARTBEAT_MINUTES", "Heartbeat interval (min)", "Nanny / Alerts", "int", False, False),
    ("transfer_start_hour", "PS_TRANSFER_START_HOUR", "Transfer window start (local hour)", "Transfers", "int", False, False),
    ("transfer_end_hour", "PS_TRANSFER_END_HOUR", "Transfer window end (local hour)", "Transfers", "int", False, False),
    ("transfer_bandwidth_limit_mbps", "PS_TRANSFER_BANDWIDTH_LIMIT_MBPS", "Bandwidth limit (Mbps)", "Transfers", "float", False, False),
]

_MASK = "••••••••"


def _mask_secret(value: str) -> str:
    value = str(value or "")
    return (_MASK + value[-4:]) if len(value) > 4 else (_MASK if value else "")


@app.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    return templates.TemplateResponse(request, "system.html", {
        "observatory": get_config().get_observatory(),
    })


@app.post("/api/preflight")
async def api_preflight():
    from photonscript.scheduler.preflight import run_preflight
    return await run_preflight(get_config())


@app.get("/api/config")
async def api_get_config():
    config = get_config()
    groups: dict[str, list] = {}
    for attr, env_var, label, group, ftype, secret, restart in _CONFIG_FIELDS:
        raw = getattr(config, attr, "")
        value = _mask_secret(raw) if secret else str(raw)
        groups.setdefault(group, []).append({
            "env": env_var, "label": label, "type": ftype,
            "secret": secret, "restart": restart, "value": value,
        })
    return [{"group": g, "fields": f} for g, f in groups.items()]


@app.post("/api/config")
async def api_update_config(request: Request):
    from photonscript.shared.envfile import env_path, update_env

    body = await request.json()
    config = get_config()
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    casts = {"int": int, "float": float,
             "bool": lambda v: str(v).lower() in ("1", "true", "yes", "on")}

    updates: dict[str, str] = {}
    restart_recommended = False
    for env_var, raw in body.items():
        field = by_env.get(env_var)
        if field is None:
            continue
        attr, _, _, _, ftype, secret, restart = field
        raw = str(raw).strip()
        if secret and (raw == "" or raw.startswith(_MASK[:2])):
            continue  # masked/blank secret = keep current value
        current = str(getattr(config, attr, ""))
        if raw == current:
            continue
        try:
            typed = casts.get(ftype, str)(raw)
        except ValueError:
            return JSONResponse(status_code=400, content={
                "detail": f"{env_var}: '{raw}' is not a valid {ftype}"})
        updates[env_var] = raw
        setattr(config, attr, typed)  # live-apply where components re-read config
        restart_recommended = restart_recommended or restart

    if updates:
        update_env(env_path(), updates)
        logger.info("Config updated via web UI: %s",
                    ", ".join(k for k in updates
                              if not by_env[k][5]) or "(secrets)")
    return {"updated": len(updates), "restart_recommended": restart_recommended}


@app.post("/api/pushover/test")
async def api_pushover_test():
    from photonscript.shared.pushover import notify

    config = get_config()
    if not config.pushover_user_key or not config.pushover_api_token:
        return JSONResponse(status_code=400, content={
            "ok": False,
            "detail": "Pushover keys not set — enter them above and Save first."})
    ok = await notify(config,
                      "Test notification from the PhotonScript web UI. "
                      "If you can read this, nanny alerts will reach you.",
                      title="PhotonScript test")
    return {"ok": ok,
            "detail": "Sent — check your phone." if ok
            else "Pushover API rejected the request — check both keys."}
