"""Remote CLI endpoints (scheduler/remote_cli.py, config/remote_cli.toml).

POST /api/cli/run        {"args": ["frame-check", "--last", "3"], "timeout_s": 300,
                          "confirm": "write"}  -> 202 {"id", "status", ...}
GET  /api/cli/jobs/{id}  status, exit code, duration, stdout / stderr tail
                         (?tail=N bytes; default tail_bytes from the TOML)
GET  /api/cli/jobs       recent jobs (in memory) and the audit log, newest
                         first: metadata only, no output (the System page)

POST and the job detail need the X-PhotonScript-Token header equal to
PS_REMOTE_CLI_TOKEN; with no token configured (the default) the feature is
off and they answer 403. The API is reachable only on the tailnet.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, Request
from photonscript.shared.json_safe import SafeJSONResponse as JSONResponse

from photonscript.scheduler import remote_cli as rc

router = APIRouter()

TOKEN_HEADER = "X-PhotonScript-Token"


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _armer_state() -> str:
    try:
        from photonscript.scheduler.app import get_armer
        return str(get_armer().state or "")
    except Exception:  # noqa: BLE001
        return ""


def _grading_jobs() -> list[str]:
    try:
        from photonscript.scheduler.runs import running_jobs
        return running_jobs()
    except Exception:  # noqa: BLE001
        return []


def _client(request: Request) -> dict:
    h = request.headers
    return {"ip": request.client.host if request.client else None,
            "forwarded_for": h.get("x-forwarded-for"),
            "tailscale_user": h.get("tailscale-user-login")}


def _refuse(cfg, status: int, detail: str, client: dict, args=None) -> JSONResponse:
    rc.audit(cfg, {"event": "refused", **client, "args": args, "status_code": status,
                   "detail": detail})
    return JSONResponse(status_code=status, content={"detail": detail})


@router.post("/api/cli/run")
async def api_cli_run(request: Request,
                      x_photonscript_token: Optional[str] = Header(None)):
    cfg = _cfg()
    client = _client(request)
    ok, status, detail = rc.token_ok(getattr(cfg, "remote_cli_token", ""),
                                     x_photonscript_token or "")
    if not ok:
        return _refuse(cfg, status, detail, client)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        return _refuse(cfg, 400, "body must be JSON: {\"args\": [...]}", client)
    args = body.get("args")
    allow = rc.load_allowlist()
    try:
        plan = rc.validate(args, allow)
        if plan.cls == "write" and body.get("confirm") != "write":
            raise rc.Refused(403, f"{plan.command} is a write command: send "
                                  '"confirm": "write" (photonscript remote --confirm-write)')
        why = rc.write_blockers(plan, _armer_state(), _grading_jobs())
        if why:
            raise rc.Refused(409, " ".join(why))
        try:
            timeout_s = float(body.get("timeout_s") or allow.default_timeout_s)
        except (TypeError, ValueError):
            raise rc.Refused(400, "timeout_s must be a number")
        timeout_s = max(5.0, min(timeout_s, float(allow.max_timeout_s)))
        job = rc.RUNNER.start(cfg, plan, timeout_s, allow, client)
    except rc.Refused as e:
        return _refuse(cfg, e.status, e.detail, client,
                       args if isinstance(args, list) else None)
    return JSONResponse(status_code=202, content=rc.public(job, 0))


@router.get("/api/cli/jobs/{job_id}")
def api_cli_job(job_id: str, tail: Optional[int] = None,
                x_photonscript_token: Optional[str] = Header(None)):
    cfg = _cfg()
    ok, status, detail = rc.token_ok(getattr(cfg, "remote_cli_token", ""),
                                     x_photonscript_token or "")
    if not ok:
        return JSONResponse(status_code=status, content={"detail": detail})
    allow = rc.load_allowlist()
    n = allow.tail_bytes if tail is None else max(1, min(int(tail), allow.output_cap_bytes))
    v = rc.RUNNER.view(job_id, n)
    if v is None:
        hit = next((a for a in rc.read_audit(cfg, 500) if a.get("id") == job_id), None)
        if hit is None:
            return JSONResponse(status_code=404, content={"detail": "no such job"})
        return {**hit, "stdout": None, "stderr": None,
                "note": "finished before the last restart: the output is gone, "
                        "the audit line is all there is"}
    return v


@router.get("/api/cli/jobs")
def api_cli_jobs(limit: int = 30):
    cfg = _cfg()
    limit = max(1, min(int(limit), 200))
    audit = []
    for a in rc.read_audit(cfg, limit):
        if a.get("detail"):
            a["detail"] = str(a["detail"])[:200]
        audit.append(a)
    return {"enabled": len((getattr(cfg, "remote_cli_token", "") or "").strip())
            >= rc.MIN_TOKEN_LEN,
            "running": [j for j in rc.RUNNER.recent() if j["status"] == "running"],
            "audit": audit}
