"""TheSky / TPoint settings audit routes (PS-104). Report only.

GET  /api/thesky/audit?refresh=          the last audit; refresh=1 runs one now
GET  /api/thesky/imagelink-check?refresh=&thesky=0|1
                                         the newest Image Link check; refresh=1
                                         solves the newest RC16 L frame with
                                         ASTAP, thesky=1 also runs TheSky's own
                                         Image Link on a temporary copy (only
                                         while the armer is idle)
GET  /api/thesky/pointing?nights=14&refresh=   NINA first-slew error trend
GET  /api/thesky/manual                  the manual TPoint record, and how it
                                         compares with TPoint's files ("files":
                                         stale when they hold a newer model)
POST /api/thesky/manual {model_date, points, rms_arcsec, ...}   save it
POST /api/thesky/manual/use-files        the operator's "Use file values":
                                         model date, points, sky RMS, IH / ID
                                         from TPoint's files into the record
GET  /api/thesky/onsite-script           the read-only script for TheSky's
                                         Run Java Script window (on-site check)
GET  /api/thesky/tpoint                  PS-171: TPoint numbers from TPoint's own
                                         files (points, sky RMS, terms, polar
                                         ME / MA, ProTrack, model date)
GET  /api/thesky/tpoint/files            the TPoint files under TheSky's user
                                         folder (names, sizes, dates)
GET  /api/thesky/tpoint/file?rel=        the head of one listed file
GET  /api/thesky/tpoint/history?limit=   the fitted-model history and the
                                         unguided limits epoch (tpoint_watch)
GET  /api/thesky/tpoint/watch            the TPoint run watcher: run in
                                         progress (points, pace, ETA) or the
                                         last model, as one status line
GET  /api/tpoint/samples?date=YYYY-MM-DD PS-187: that night's tpoint-sample CSV
                                         rows (runs/<date>_tpoint.csv) and a
                                         summary (solved / added / add errors)

Nothing here writes TheSky, moves the mount or takes an image. Handlers
lazily import get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

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


@router.get("/api/thesky/audit")
async def api_thesky_audit(refresh: bool = False):
    """The last TheSky / TPoint audit (arm or a refresh); refresh=1 runs a
    new one now (read-only TheSky scripts, stored checks, NINA logs)."""
    from photonscript.scheduler import thesky_audit as ta
    cfg = _cfg()
    out = None if refresh else ta.load_latest(cfg)
    if out is None:
        out = await asyncio.to_thread(ta.run_audit, cfg, "manual",
                                      armer_state=_armer_state())
        out["cached"] = False
    else:
        out["cached"] = True
    out["armer_state_now"] = _armer_state() or None
    out["imagelink_thesky_allowed"] = ta.armer_idle(_armer_state())
    from photonscript.scheduler import guiding_attention
    guiding_attention.annotate_thesky(out)         # PS-119: source and its age
    return out


@router.get("/api/thesky/imagelink-check")
async def api_thesky_imagelink_check(refresh: bool = False, thesky: bool = False):
    """The newest Image Link check. refresh=1 runs a new ASTAP check;
    thesky=1 adds TheSky's own Image Link on a copy of the frame, refused
    (409) unless the armer is idle (DISARMED / COMPLETE)."""
    from photonscript.scheduler import thesky_audit as ta
    from photonscript.shared import phd2_store as store
    cfg = _cfg()
    st = _armer_state()
    if thesky and not ta.armer_idle(st):
        return JSONResponse(status_code=409, content={
            "ok": False, "note": f"armer is {st or 'unknown'}: TheSky Image Link runs only "
                                 "while it is idle (DISARMED / COMPLETE)"})
    if refresh or thesky:
        out = await asyncio.to_thread(ta.imagelink_check, cfg, thesky=thesky,
                                      armer_state=st)
    else:
        out = store.read_json(ta.audit_dir(cfg) / "imagelink_latest.json") or {
            "ok": False, "note": "no Image Link check yet"}
    latest = ta.load_latest(cfg) or {}
    obs = {r["id"]: r for r in latest.get("rows") or []}
    ails = {"ails_image_scale": (obs.get("ails_image_scale") or {}).get("current"),
            "ails_position_angle": (obs.get("ails_position_angle") or {}).get("current")}
    rb = (latest.get("manual") or {}).get("run_binning") or 1     # PS-120: 1x1 run
    out["compare"] = ta.compare_settings(out, ails, int(rb))
    out["armer_state_now"] = st or None
    return out


@router.get("/api/thesky/pointing")
async def api_thesky_pointing(nights: int = 14, refresh: bool = False):
    """NINA first-slew error over the last N nights by side of the meridian,
    Dec band and HA band, with PS-67's mount vs solve when on disk."""
    from photonscript.scheduler import nina_center_log as ncl
    return await asyncio.to_thread(ncl.summary, _cfg(), nights, None, refresh)


@router.get("/api/thesky/manual")
async def api_thesky_manual():
    from photonscript.scheduler import thesky_audit as ta
    from photonscript.scheduler import tpoint_files
    cfg = _cfg()
    rec = ta.load_manual(cfg)
    try:   # read only: TPoint's own files
        st = await asyncio.to_thread(tpoint_files.stats, cfg)
        files = ta.record_vs_files(rec, st)
    except Exception as e:  # noqa: BLE001
        files = {"stale": False, "note": f"TPoint files not read: {e}"}
    return {"record": rec, "fields": list(ta.MANUAL_FIELDS),
            "max_age_days": getattr(cfg, "thesky_manual_max_age_days", 30),
            "files": files}


@router.post("/api/thesky/manual/use-files")
async def api_thesky_manual_use_files():
    """The operator's one click: fill the manual record's model date,
    points, sky RMS and IH / ID from TPoint's files (the other fields are
    kept). Writes only PhotonScript's own manual.json."""
    from photonscript.scheduler import thesky_audit as ta
    res = await asyncio.to_thread(ta.use_file_values, _cfg())
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


@router.post("/api/thesky/manual")
async def api_thesky_manual_post(request: Request):
    """Save the manual TPoint record (after each TPoint session). Writes
    only PhotonScript's own <data_dir>/thesky/manual.json."""
    from photonscript.scheduler import thesky_audit as ta
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={
            "ok": False, "problems": ["body must be a JSON object"]})
    res = ta.save_manual(_cfg(), body)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


@router.get("/api/thesky/onsite-script", response_class=PlainTextResponse)
def api_thesky_onsite_script():
    """The read-only check script for TheSky's Tools > Run Java Script."""
    from photonscript.telescope_agent.thesky_client import onsite_script
    return onsite_script()


# ---------------------------------------------------------------- PS-171

@router.get("/api/thesky/tpoint")
async def api_thesky_tpoint():
    """TPoint numbers from TPoint's own files (read only; TheSky64 has no
    TPoint scripting object). Saves stats_latest / a history line."""
    from photonscript.scheduler import tpoint_files
    cfg = _cfg()
    st = await asyncio.to_thread(tpoint_files.stats, cfg)
    await asyncio.to_thread(tpoint_files.save, cfg, st)
    st["text"] = tpoint_files.format_stats(st)
    return st


@router.get("/api/thesky/tpoint/history")
async def api_thesky_tpoint_history(limit: int = 50):
    """The fitted-model history the TPoint watcher keeps (tpoint_watch:
    <data_dir>/tpoint/models.jsonl, newest last) and the unguided limits
    epoch. Reads PhotonScript's own files only."""
    from photonscript.scheduler import tpoint_watch as tw
    cfg = _cfg()
    recs = await asyncio.to_thread(tw.history, cfg)
    n = max(1, min(int(limit), 500))
    return {"models": recs[-n:], "count": len(recs), "epoch": tw.load_epoch(cfg),
            "text": tw.format_history(cfg, recs, n)}


@router.get("/api/thesky/tpoint/watch")
async def api_thesky_tpoint_watch():
    """The TPoint run watcher's last state (status idle / running /
    stopped / finished, points, pace, ETA, the last model) and its one-line
    text. Reads the watcher's saved state only (no TPoint file read)."""
    from photonscript.scheduler import tpoint_watch as tw
    cfg = _cfg()
    st = await asyncio.to_thread(tw.load_state, cfg)
    st.pop("file_sig", None)
    st["mode"] = tw.mode(cfg)
    st["text"] = (tw.status_text(cfg, st) if tw.mode(cfg) != "off"
                  else "TPoint watcher off (tpoint_watch_mode)")
    return st


@router.get("/api/thesky/tpoint/files")
async def api_thesky_tpoint_files():
    """The TPoint-related files under TheSky's user folder: read-only
    listing (rel path, size, mtime), newest first."""
    from photonscript.scheduler import tpoint_files
    lst = await asyncio.to_thread(tpoint_files.list_files, _cfg())
    for f in lst["files"]:
        f.pop("_ts", None)
        f.pop("path", None)
    return lst


@router.get("/api/tpoint/samples")
async def api_tpoint_samples(date: str = ""):
    """PS-187: the TPoint mapping samples of one night (read only): every
    row of runs/<date>_tpoint.csv plus a summary (points, solved, added,
    add_error / note counts). date empty = tonight."""
    import re
    from photonscript.shared.phd2_store import night_of
    from photonscript.scheduler import tpoint_mapping as tm
    cfg = _cfg()
    date = date.strip() or night_of(cfg)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return JSONResponse(status_code=400,
                            content={"detail": f"bad date {date!r} (YYYY-MM-DD)"})
    return await asyncio.to_thread(tm.samples, cfg, date)


@router.get("/api/thesky/tpoint/file")
async def api_thesky_tpoint_file(rel: str, max_kb: int = 64):
    """The first max_kb (at most 64) KB of one LISTED TPoint file, to check
    the parsers against a real file. Anything not listed is refused (404)."""
    from photonscript.scheduler import tpoint_files
    out = await asyncio.to_thread(tpoint_files.head, _cfg(), rel, max_kb * 1024)
    if not out.get("ok"):
        return JSONResponse(status_code=404, content=out)
    return out
