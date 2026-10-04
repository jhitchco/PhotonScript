"""PhotonScript's own PHD2 records and actions (PS-91, PS-92, PS-93, PS-89, PS-90).

GET  /api/phd2/guard?date=         non-star lock guard episodes for a night
GET  /api/phd2/hotpix              guide-camera hot-pixel map status
POST /api/phd2/hotpix/capture      capture a new map now (roof closed!)
GET  /api/phd2/selftest?date=|days=   pulse-path self-test results (PS-92)
POST /api/phd2/selftest/run?context=&slot=   run the self-test now
GET  /api/phd2/calibration?date=   calibration record, history, flip, plan (PS-93)
POST /api/phd2/calibrate?mode=next|now    ask for a calibration (PS-93)
GET  /api/phd2/calibration-sequence       the standalone calibration sequence
GET  /api/phd2/audit?refresh=&raw=        settings audit vs the desired state (PS-89)
POST /api/phd2/audit/apply {ids, dry_run}  apply audit rows (API / gated profile)
GET  /api/phd2/tuning?date=       guide-star auto-tune: per filter, changes, advice (PS-90)
GET  /api/phd2/live?probe=         live PHD2 state for the Guiding tab (PS-103)
GET  /guiding                      the Guiding tab page (PS-103)

The PHD2 guide-log endpoints (/api/phd2/log, /logs, /summary, /analysis)
stay in routers/triage.py. Handlers lazily import get_config to avoid an
import cycle with app.py.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _armer_state() -> str:
    try:
        from photonscript.scheduler.app import get_armer
        return str(get_armer().state or "")
    except Exception:  # noqa: BLE001
        return ""


def guard_summary(config, date: str) -> dict:
    """The guard block for a night (API, runs page, morning report)."""
    from photonscript.shared import phd2_store as store
    eps = store.guard_episodes(config, date)
    mins = 0.0
    for e in eps:
        a, b = store.parse_z(e.get("start_utc")), store.parse_z(e.get("end_utc"))
        if a and b:
            mins += max(0.0, (b - a).total_seconds() / 60)
    recs = [r for e in eps for r in e.get("recoveries", []) if r.get("ok") is not None]
    return {"date": date,
            "enabled": bool(getattr(config, "guard_enabled", True)),
            "auto_recover": bool(getattr(config, "guard_auto_recover", False)),
            "episodes": len(eps),
            "non_star": sum(1 for e in eps if e.get("kind") == "non_star"),
            "impossible_state": sum(1 for e in eps if e.get("kind") == "impossible_state"),
            "closed_minutes": round(mins, 1),
            "recoveries_ok": sum(1 for r in recs if r.get("ok")),
            "recoveries_failed": sum(1 for r in recs if r.get("ok") is False),
            "list": eps}


@router.get("/api/phd2/guard")
def api_phd2_guard(date: str = ""):
    """Guard episodes of a night (default: tonight / the night in progress)."""
    from photonscript.shared import phd2_store as store
    from photonscript.telescope_agent import phd2_ops
    cfg = _cfg()
    date = date or store.night_of(cfg, datetime.utcnow())
    out = guard_summary(cfg, date)
    out["phd2_ops"] = phd2_ops.status()
    return out


@router.get("/api/phd2/hotpix")
def api_phd2_hotpix(pixels: bool = False):
    from photonscript.telescope_agent import guide_hotpix
    cfg = _cfg()
    out = guide_hotpix.status(cfg)
    if pixels:
        out["pixels"] = (guide_hotpix.load(cfg) or {}).get("pixels", [])
    return out


@router.post("/api/phd2/hotpix/capture")
async def api_phd2_hotpix_capture():
    """Build the guide-camera hot-pixel map now. Take it with the roof
    closed (or the guide camera capped): stars that hold still in 8 frames
    are not hot pixels, but faint ones can be mistaken for them. Refused
    while a night is running and whenever PHD2 is guiding."""
    from photonscript.telescope_agent import guide_hotpix
    if _armer_state() == "RUNNING":
        return JSONResponse(status_code=409, content={
            "ok": False, "note": "a night is running: the map is built "
                                 "automatically while the roof is closed"})
    return await guide_hotpix.capture(_cfg(), "manual")


# ---- PS-92 pulse-path self-test --------------------------------------------

def selftest_summary(config, date: str) -> dict:
    """The self-test block for a night (runs page, morning report)."""
    from photonscript.shared import phd2_store as store
    rows = store.selftest_results(config, night=date)
    active = [r for r in rows if r.get("kind", "active") == "active"
              and r.get("verdict") not in ("SKIPPED",)]
    passive = [r for r in rows if r.get("kind") == "passive"]
    last = active[-1] if active else None
    return {"date": date, "runs": len(active),
            "verdict": last.get("verdict") if last else None,
            "by_pier": {r.get("pier_side") or "?": r.get("verdict") for r in active},
            "passive": [{k: r.get(k) for k in ("t_utc", "source", "verdict",
                                                "reasons", "pier_side")}
                        for r in passive],
            "last": {k: (last or {}).get(k) for k in (
                "t_utc", "context", "slot", "verdict", "reasons", "pier_side",
                "dec_deg", "speeds", "directions", "pairs")} if last else None}


def _trend(rows: list[dict]) -> list[dict]:
    """One row per night: the worst active verdict and its per-direction
    ratios (the 30-night table)."""
    rank = {"FAIL": 0, "WARN": 1, "PASS": 2, "INCONCLUSIVE": 3}
    by: dict[str, dict] = {}
    for r in rows:
        if r.get("kind", "active") != "active" or r.get("verdict") == "SKIPPED":
            continue
        cur = by.get(r.get("night"))
        if cur is None or rank.get(r.get("verdict"), 9) < rank.get(cur.get("verdict"), 9):
            by[r.get("night")] = r
    out = []
    for night in sorted(by, reverse=True):
        r = by[night]
        out.append({"night": night, "verdict": r.get("verdict"),
                    "pier_side": r.get("pier_side"),
                    "ratios": {d: (v or {}).get("ratio")
                               for d, v in (r.get("directions") or {}).items()},
                    "reasons": r.get("reasons")})
    return out


@router.get("/api/phd2/selftest")
def api_phd2_selftest(date: str = "", days: int = 0):
    """Self-test results: one night (date=, default tonight) or the last
    `days` nights as a per-night trend."""
    from photonscript.shared import phd2_store as store
    cfg = _cfg()
    if days:
        rows = store.selftest_results(cfg, days=days)
        return {"days": days, "nights": _trend(rows), "results": rows}
    date = date or store.night_of(cfg, datetime.utcnow())
    out = selftest_summary(cfg, date)
    out["results"] = store.selftest_results(cfg, night=date)
    return out


def _auto_slot() -> str:
    """twilight before the armed night's dusk, target after it."""
    try:
        from photonscript.scheduler.app import get_armer
        dusk = (get_armer().plan or {}).get("dusk_utc")
        if dusk and datetime.utcnow() < datetime.fromisoformat(dusk.rstrip("Z")):
            return "twilight"
    except Exception:  # noqa: BLE001
        pass
    return "target"


@router.post("/api/phd2/selftest/run")
async def api_phd2_selftest_run(context: str = "manual", slot: str = "auto"):
    """Run the pulse-path self-test now (NINA's ExternalScript slot posts
    context=nina). Blocks until it finishes (about 1.5 to 4 min)."""
    from photonscript.telescope_agent.pulse_selftest import run_selftest
    context = "nina" if context == "nina" else "manual"
    if slot not in ("twilight", "target", "manual"):
        slot = _auto_slot() if context == "nina" else "manual"
    return await run_selftest(_cfg(), context=context, slot=slot)


# ---- PS-93 calibration manager ----------------------------------------------

def calibration_summary(config, date: str) -> dict:
    """The calibration block for a night (runs page, morning report)."""
    from photonscript.scheduler import phd2_calibration as pc
    out = pc.summary(config, date)
    rec = out.pop("record", None) or {}
    out["record"] = {k: rec.get(k) for k in (
        "t_utc", "source", "context", "grade", "reasons", "warnings", "dec_deg",
        "ha_hr", "pier_side", "ortho_err_deg", "steps", "recommended_step_ms")}         if rec else None
    return out


@router.get("/api/phd2/calibration")
def api_phd2_calibration(date: str = "", days: int = 30):
    """The calibration PHD2 uses now (graded), tonight's plan, the flip
    checks, the recommended Calibration Step and the last `days` of graded
    calibrations."""
    from photonscript.scheduler import phd2_calibration as pc
    from photonscript.shared import phd2_store as store
    from photonscript.telescope_agent import phd2_ops
    cfg = _cfg()
    date = date or store.night_of(cfg, datetime.utcnow())
    out = pc.summary(cfg, date)
    out["date"] = date
    out["live"] = pc.load_live(cfg)
    out["history"] = pc.history(cfg, days=days)
    out["phd2_ops"] = phd2_ops.status()
    return out


def _field_now(cfg) -> dict:
    from photonscript.scheduler import phd2_calibration as pc
    return pc.pick_calibration_field(cfg, datetime.utcnow())


@router.get("/api/phd2/calibration-sequence")
def api_phd2_calibration_sequence():
    """A standalone NINA sequence that calibrates PHD2 on a field picked for
    right now (connect, unpark, the PHD2_CALIBRATION slot). For loading by
    hand in NINA; POST /api/phd2/calibrate?mode=now dispatches it."""
    import json as _json
    from photonscript.scheduler.nina_sequence_json import generate_phd2_calibration_json
    cfg = _cfg()
    field = _field_now(cfg)
    return {"field": field, "sequence": _json.loads(generate_phd2_calibration_json(
        field, int(getattr(cfg, "phd2_cal_hold_s", 240) or 240)))}


@router.post("/api/phd2/calibrate")
async def api_phd2_calibrate(request: Request, mode: str = ""):
    """Ask for a PHD2 calibration. mode (query or JSON body {"mode": ...}):
    next = tonight's (or the next) dispatch includes the calibration slot;
    now = while a night runs, one re-dispatch with the slot (once per night,
    1 h of dark left); armed and waiting = same as next; otherwise the
    standalone calibration sequence is dispatched to NINA right away (the
    roof must be open and the sky dark)."""
    from photonscript.scheduler import phd2_calibration as pc
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    mode = str(mode or (body or {}).get("mode") or "next").strip().lower()
    if mode not in ("next", "now"):
        return JSONResponse(status_code=400, content={
            "ok": False, "note": "mode must be next or now"})
    cfg = _cfg()
    state = _armer_state()
    if mode == "next" or state == "ARMED":
        req = pc.set_request(cfg, "next")
        return {"ok": True, "mode": "next", "request": req,
                "note": "the next dispatch includes a PHD2 calibration slot"}
    from photonscript.scheduler.app import get_armer
    armer = get_armer()
    if state in ("RUNNING", "PAUSED_UNSAFE"):
        ok = await armer.recalibrate("manual request")
        return {"ok": ok, "mode": "now",
                "note": "re-dispatched with a calibration slot" if ok else
                        "declined: only while RUNNING, once per night, with at "
                        "least 1 h of dark left"}
    import json as _json
    from photonscript.scheduler.nina_sequence_json import generate_phd2_calibration_json
    hold = int(getattr(cfg, "phd2_cal_hold_s", 240) or 240)
    field = _field_now(cfg)
    from photonscript.shared import phd2_store as store
    pc.save_plan(cfg, {"night": store.night_of(cfg), "created_utc": store.iso_z(datetime.utcnow()),
                       "reason": "manual request (now)", "field": field, "hold_s": hold,
                       "status": "pending", "attempts": 0, "forced": True})
    ok = await armer.dispatch_raw(_json.loads(generate_phd2_calibration_json(field, hold)),
                                  "phd2-calibration")
    return {"ok": ok, "mode": "now", "field": field,
            "note": "calibration sequence dispatched to NINA" if ok else
                    f"dispatch failed: {armer.detail}"}


# ---- PS-89 settings audit ---------------------------------------------------

@router.get("/api/phd2/audit")
async def api_phd2_audit(refresh: bool = False, raw: bool = False):
    """The last PHD2 settings audit (from the arm, a PHD2 configuration
    change or a refresh); refresh=1 runs a new one now. raw=1 (with refresh)
    adds every observed value, including every registry value read under
    the PHD2 profile, to fill in phd2_profile_store.KEYS."""
    from photonscript.scheduler import phd2_audit as pa
    from photonscript.telescope_agent import phd2_ops
    cfg = _cfg()
    out = None if (refresh or raw) else pa.load_latest(cfg)
    if out is None:
        out = await pa.run_audit(cfg, reason="manual", raw=raw)
        out["cached"] = False
    else:
        out["cached"] = True
    out["phd2_ops"] = phd2_ops.status()
    return out


@router.post("/api/phd2/audit/apply")
async def api_phd2_audit_apply(request: Request):
    """Apply audit rows: {"ids": [...], "dry_run": true|false} (dry_run
    defaults to true). API rows (exposure, RA min-move, Dec guide mode) only
    while PHD2 is Stopped or Looping and nothing else holds PHD2; profile
    rows only with phd2_audit_autofix, the armer idle, PHD2 closed, verified
    registry names and a backup; every other row is report only."""
    from photonscript.scheduler import phd2_audit as pa
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    ids = [str(x) for x in (body or {}).get("ids") or []]
    if not ids:
        return JSONResponse(status_code=400, content={
            "ok": False, "note": "ids: the audit rows to apply"})
    dry = (body or {}).get("dry_run", True)
    dry = dry if isinstance(dry, bool) else str(dry).lower() not in ("0", "false", "no")
    return await pa.apply(_cfg(), ids, dry_run=dry, armer_state=_armer_state())


# ---- PS-90 guide-star auto-tune --------------------------------------------

@router.get("/api/phd2/tuning")
def api_phd2_tuning(date: str = ""):
    """The night's guide-star tuning (default: tonight): per-filter peak %,
    SNR, HFD and exposure, every exposure change, the last measurement, the
    next-night gain / binning recommendation and the bin 3 HFD check."""
    from photonscript.scheduler import phd2_tuning as tn
    from photonscript.shared import phd2_store as store
    from photonscript.telescope_agent import phd2_ops
    cfg = _cfg()
    date = date or store.night_of(cfg, datetime.utcnow())
    out = tn.summary(cfg, date)
    out["phd2_ops"] = phd2_ops.status()
    return out


# ---- PS-103 Guiding tab -----------------------------------------------------

_PROBE_TTL_S = 20.0      # one PHD2 read per 20 s, however many tabs poll
_PROBE_TIMEOUT_S = 8.0
_probe: dict = {"t": 0.0, "out": None, "client": None, "lock": None}


async def _probe_phd2(cfg) -> dict:
    """One short read-only look at PHD2 itself: its own app state, guide
    exposure, binning, pixel scale and lock position. Cached for
    _PROBE_TTL_S. The client is kept between probes (connected only during
    one) so a closed PHD2 logs one warning per outage, not one per poll.
    Never raises."""
    from photonscript.telescope_agent.phd2_client import PHD2Client
    if _probe["lock"] is None:
        _probe["lock"] = asyncio.Lock()
    async with _probe["lock"]:
        age = time.monotonic() - _probe["t"]
        if _probe["out"] is not None and age < _PROBE_TTL_S:
            return dict(_probe["out"], age_s=round(age, 1))
        c = _probe["client"]
        if c is None or (c.host, c.port) != (cfg.phd2_host, cfg.phd2_port):
            c = _probe["client"] = PHD2Client(cfg.phd2_host, cfg.phd2_port, config=cfg)

        async def read() -> dict:
            if not await c.connect():
                return {"ok": False, "note": "PHD2 not reachable"}
            o: dict = {"ok": True}
            try:
                o["app_state"] = await c.get_app_state()
                for key, fn in (("exposure_ms", c.get_exposure),
                                ("binning", c.get_camera_binning),
                                ("pixel_scale", c.get_pixel_scale),
                                ("lock_position", c.get_lock_position)):
                    try:
                        o[key] = await fn()
                    except Exception:  # noqa: BLE001 - e.g. no lock set
                        o[key] = None
            finally:
                await c.disconnect()
            return o
        try:
            out = await asyncio.wait_for(read(), _PROBE_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass
            out = {"ok": False, "note": f"PHD2 read failed: {e or type(e).__name__}"}
        out["t_utc"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        _probe["t"], _probe["out"] = time.monotonic(), out
        return dict(out, age_s=0.0)


@router.get("/api/phd2/live")
async def api_phd2_live(probe: bool = True):
    """Live PHD2 state for the Guiding tab in one read: the RC16 agent's
    guiding snapshot (state, RMS in arcsec or px, SNR, HFD, exposure,
    binning, pixel scale), who holds PHD2 (phd2_ops), the armer state, the
    night date the other /api/phd2/* reads default to, and (probe=1, the
    default) a cached read-only look at PHD2 itself for its app state and
    lock position."""
    from photonscript.scheduler import app as _app
    from photonscript.shared import phd2_store as store
    from photonscript.telescope_agent import phd2_ops
    cfg = _cfg()
    ts = _app._telescope_state
    return {"night": store.night_of(cfg, datetime.utcnow()),
            "armer_state": _armer_state() or None,
            "phd2_ops": phd2_ops.status(),
            "session_state": getattr(ts.session_state, "value", ts.session_state),
            "target": ts.current_target,
            "filter": getattr(ts.current_filter, "value", ts.current_filter),
            "guiding": ts.guiding.model_dump(mode="json"),
            "phd2": await _probe_phd2(cfg) if probe else None}


@router.get("/guiding", response_class=HTMLResponse)
async def guiding_page(request: Request):
    """The Guiding tab: every PHD2 panel in one place (PS-103)."""
    from photonscript.scheduler.app import VERSION, templates
    from photonscript.shared import phd2_store as store
    cfg = _cfg()
    return templates.TemplateResponse(request, "guiding.html", {
        "version": VERSION, "observatory": cfg.get_observatory(),
        "night": store.night_of(cfg, datetime.utcnow())})
