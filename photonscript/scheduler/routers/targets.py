"""PS-81 Targets tab: every sub for a target with its review status.

GET /targets                          Targets tab (card per target)
GET /target?name=                     per-target page
GET /api/targets                      target cards (counts, goal, top reason)
GET /api/targets/detail?name=         per-target facts, totals, nights, reasons
GET /api/targets/readiness?name=      integration readiness (scheduler/readiness)
GET /api/subs?target=&rig=&filter=&verdict=&night=&reason=&sort=&offset=&limit=
                                      paged sub rows (scheduler/sub_index)
GET /api/target/history?name=         old per-target history (same shape as
                                      before, now built on sub_index)
GET /api/rigs/fov                     each rig's field of view (arcmin)
GET /api/targets/refimage/meta?name=&view=wide|close
                                      reference image + geometry (DSS2 cutout,
                                      cached; own best sub as offline fallback)
GET /api/targets/refimage?name=&view= the cached cutout (JPEG)
GET /api/targets/live?name=           mount position when the RC16 is on it
GET /api/target/light-budget?name=&rig=&filter=
                                      PS-117 (b): sky, sub length advice and
                                      SNR progress (scheduler/light_budget)

PS-24: the per-target page gives verdicts in place through the review
module (static/js/review.js, POST /api/runs/{date}/qa in routers/review.py);
these routes stay read-only. Every handler is a plain def (threadpool) except
/live, and none of them asks Syncthing for transfer state (PS-24 pitfall).
Kept out of app.py (PS-8 router split); app helpers are imported lazily.
"""
from __future__ import annotations

import math
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from photonscript.scheduler import sub_index

router = APIRouter()

PAGE_MAX = 200


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _projects() -> list:
    from photonscript.scheduler.app import get_store
    try:
        return list(get_store().projects.values())
    except Exception:  # noqa: BLE001
        return []


def _page(request: Request, template: str):
    from photonscript.scheduler.app import VERSION, templates
    return templates.TemplateResponse(request, template, {
        "version": VERSION, "observatory": _cfg().get_observatory()})


@router.get("/targets", response_class=HTMLResponse)
def targets_page(request: Request):
    return _page(request, "targets.html")


@router.get("/target", response_class=HTMLResponse)
def target_page(request: Request):
    return _page(request, "target.html")


@router.get("/api/targets")
def api_targets():
    return {"targets": sub_index.targets(_cfg(), _projects())}


@router.get("/api/targets/detail")
def api_target_detail(name: str):
    from photonscript.scheduler.runs import flush_goal_sync
    flush_goal_sync()   # PS-24: goal bars include a verdict just given
    d = sub_index.target_detail(_cfg(), name, _projects())
    if not d["found"]:
        return JSONResponse(status_code=404, content={
            "detail": f"no project and no subs for {name!r}", **d})
    return d


@router.get("/api/targets/readiness")
def api_target_readiness(name: str):
    """One target's integration readiness. The calibration scan is reused
    for 5 min (it reads FITS headers); /api/integration/readiness rescans."""
    from photonscript.scheduler.readiness import (calibration_context,
                                                  target_readiness)
    from photonscript.shared.target_names import target_key
    cfg = _cfg()
    projects = _projects()
    k = sub_index._match_target(name, projects)
    p = next((x for x in projects if target_key(x.target.name) == k), None)
    if p is None:
        return {"target": name, "project": False,
                "note": "no imaging project: readiness is counted per "
                        "planned filter"}
    out = target_readiness(cfg, p, calibration_context(cfg, max_age_s=300))
    return {**out, "project": True}


@router.get("/api/target/light-budget")
def api_target_light_budget(name: str, rig: str = "",
                            filter: str = ""):  # noqa: A002
    """PS-117 (b): sky, per-length table, advisory sub length and SNR
    progress per rig + filter (scheduler/light_budget). Read-only."""
    from photonscript.scheduler.light_budget import target_light_budget
    return target_light_budget(_cfg(), name, _projects(), rig=rig,
                               filter=filter)


@router.get("/api/subs")
def api_subs(target: str | None = None, rig: str = "", filter: str = "",  # noqa: A002
             verdict: str = "", night: str = "", reason: str = "",
             sort: str = "time", offset: int = 0, limit: int = 60):
    cfg = _cfg()
    rows = sub_index.rows(cfg, _projects(), target=target, rig=rig or None,
                          filter=filter or None, verdict=verdict or None,
                          night=night or None, reason=reason or None,
                          sort="hfr" if sort == "hfr" else "time")
    offset = max(0, int(offset))
    limit = min(max(1, int(limit)), PAGE_MAX)
    return {"total": len(rows), "offset": offset, "limit": limit,
            "rows": rows[offset:offset + limit]}


@router.get("/api/target/history")
def api_target_history(name: str):
    """Every sub ever recorded for one target, grouped by night, with QA
    state and whether each accepted light made it into the Library.
    PS-78: names are canonicalized, so subs recorded under a container name
    count for the target, and a container name as `name` shows the real
    target. PS-81: built on sub_index; the response shape is unchanged."""
    from photonscript.scheduler.runs import library_root, library_target_dirs
    from photonscript.shared.target_names import canonical_target, known_target_index
    cfg = _cfg()
    projects = _projects()
    known = known_target_index(projects) if projects else None
    name = canonical_target(name, known) or name
    tdir = library_target_dirs(library_root(cfg), name, known)[0]
    lib_files = sub_index.library_files(cfg, name, projects)
    by_night: dict[str, list] = {}
    for r in sub_index.rows(cfg, projects, target=name):
        by_night.setdefault(r["date"], []).append(r)
    nights = []
    totals = {"accepted": 0, "rejected": 0, "in_library": 0, "by_filter": {}}
    for date in sorted(by_night, reverse=True):
        out = []
        n_acc = n_rej = 0
        for r in sorted(by_night[date], key=lambda x: x["time"]):
            passed = r["verdict"] != sub_index.REJECTED
            base = Path(r["file"]).name
            in_lib = base in lib_files
            if passed:
                n_acc += 1
                totals["accepted"] += 1
                totals["in_library"] += int(in_lib)
            else:
                n_rej += 1
                totals["rejected"] += 1
            bf = totals["by_filter"].setdefault(r["filter"], {"accepted": 0,
                                                              "rejected": 0})
            bf["accepted" if passed else "rejected"] += 1
            m = r["metrics"]
            out.append({
                "time": r["time"][11:16], "filter": r["filter"],
                "exp_s": r["exp_s"], "hfr": m["hfr"], "ecc": m["ecc"],
                "stars": m["stars"], "passed": passed,
                "reviewed": r["verdict"] == sub_index.APPROVED,
                "reason": "; ".join(r["reasons"]), "in_library": in_lib,
                "file": base, "target_raw": r["target_raw"]})
        nights.append({"date": date, "accepted": n_acc, "rejected": n_rej,
                       "subs": out})
    return {"target": name, "nights": nights, "totals": totals,
            "library_dir": str(tdir),
            "desktop_path": str(Path(cfg.desktop_library_dir) / name),
            "desktop_hint": "desktop copy appears once /api/sync shows "
                            "library_synced"}


# --- phase 2: reference image, fields of view, live marker --------------------

@router.get("/api/rigs/fov")
def api_rigs_fov():
    from photonscript.scheduler.refimage import rig_fovs
    return {"rigs": rig_fovs(_cfg())}


def _facts(name: str) -> tuple[dict | None, str]:
    d = sub_index.target_detail(_cfg(), name, _projects())
    return (d.get("facts") if d["found"] else None), d["target"]


def _own_sub(cfg, name: str, view: str) -> dict | None:
    """Offline fallback: the sharpest approved sub (Piggy-600 for the wide
    view, RC16 for the close one, else whichever rig has one)."""
    from photonscript.scheduler.refimage import rig_fov
    from photonscript.shared.rigs import PIGGYBACK, RC16
    rows = [r for r in sub_index.rows(cfg, _projects(), target=name,
                                      verdict=sub_index.APPROVED, sort="hfr")
            if r["metrics"]["hfr"]]
    prefer = (PIGGYBACK, RC16) if view == "wide" else (RC16, PIGGYBACK)
    for rig in prefer:
        hit = next((r for r in rows if r["rig"] == rig), None)
        if hit is not None:
            fov = rig_fov(cfg, rig)
            return {"source": "own_sub", "rig": rig, "date": hit["date"],
                    "file": hit["file"],
                    "label": f"own sub, {hit['date']} ({fov['label']})",
                    "image_url": hit["thumb"].replace(
                        f"&w={sub_index.THUMB_W}", "&w=1000"),
                    "fov_deg": round(fov["w_arcmin"] / 60.0, 4),
                    "aspect": fov["height_px"] / fov["width_px"],
                    "approximate": True,
                    "note": "framing and rotation as shot; FOV boxes drawn "
                            "about the frame center"}
    return None


@router.get("/api/targets/refimage/meta")
def api_refimage_meta(name: str, view: str = "close"):
    """Reference image for the overlay: the cached DSS2 cutout (fetched once
    on first view, up to 20 s), else the own-sub fallback, else none (the
    page draws a dark panel with the FOV boxes)."""
    from urllib.parse import quote

    from photonscript.scheduler import refimage
    view = view if view in refimage.VIEWS else "close"
    cfg = _cfg()
    facts, canon = _facts(name)
    fovs = refimage.rig_fovs(cfg)
    if not facts or facts.get("ra_hours") is None:
        return {"target": canon, "view": view, "source": "none", "rigs": fovs,
                "note": "no coordinates for this target"}
    ra, dec = float(facts["ra_hours"]) * 15.0, float(facts["dec_degrees"])
    fov = refimage.view_fov(view, facts.get("size_arcmin"))
    base = {"target": canon, "view": view, "ra_deg": ra, "dec_deg": dec,
            "rigs": fovs}
    side = refimage.reference(cfg, canon, ra, dec, fov)
    if side is not None:
        side = {k: v for k, v in side.items() if k != "path"}
        return {**base, **side, "label": "DSS2 (CDS hips2fits)",
                "image_url": f"/api/targets/refimage?name={quote(canon)}"
                             f"&view={view}",
                "aspect": side["height"] / side["width"],
                "approximate": False}
    own = _own_sub(cfg, canon, view)
    if own is not None:
        return {**base, **own}
    return {**base, "source": "none", "fov_deg": fov,
            "aspect": refimage.HEIGHT / refimage.WIDTH,
            "note": "no cached cutout (offline?) and no approved sub yet"}


@router.get("/api/targets/refimage")
def api_refimage(name: str, view: str = "close"):
    from photonscript.scheduler import refimage
    view = view if view in refimage.VIEWS else "close"
    facts, canon = _facts(name)
    if not facts:
        return JSONResponse(status_code=404, content={"detail": "unknown target"})
    hit = refimage.cached(_cfg(), canon,
                          refimage.view_fov(view, facts.get("size_arcmin")))
    if hit is None:
        return JSONResponse(status_code=404,
                            content={"detail": "not cached; GET .../meta first"})
    return FileResponse(hit["path"], media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


def separation_arcmin(ra1_h, dec1_d, ra2_h, dec2_d) -> float:
    r1, r2 = math.radians(ra1_h * 15.0), math.radians(ra2_h * 15.0)
    d1, d2 = math.radians(dec1_d), math.radians(dec2_d)
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 60.0


@router.get("/api/targets/live")
async def api_target_live(name: str):
    """Where the mount points while the RC16's current target is this one
    (from /api/rigs). The rig-tagged state fix of PS-67 phase 1 makes this
    trustworthy; until then treat the marker as indicative."""
    import asyncio

    from photonscript.scheduler.app import api_rigs
    from photonscript.shared.rigs import RC16
    from photonscript.shared.target_names import canonical_target, target_key
    projects = await asyncio.to_thread(_projects)
    facts, canon = await asyncio.to_thread(_facts, name)
    try:
        rigs = (await api_rigs()).get("rigs", [])
    except Exception as e:  # noqa: BLE001
        return {"active": False, "error": str(e)}
    rc = next((r for r in rigs if r.get("rig") == RC16), {}) or {}
    raw = rc.get("target")
    cur = canonical_target(raw, projects or None)
    mount = (rc.get("devices") or {}).get("mount") or {}
    active = bool(cur and target_key(cur) == target_key(canon))
    out = {"active": active, "target_raw": raw, "current": cur,
           "session_state": rc.get("session_state"),
           "parked": mount.get("parked"), "tracking": mount.get("tracking"),
           "ra_hours": mount.get("ra"), "dec_degrees": mount.get("dec"),
           "trust": "indicative until PS-67 phase 1 (rig-tagged state)"}
    if (active and facts and mount.get("ra") is not None
            and mount.get("dec") is not None
            and facts.get("ra_hours") is not None):
        out["sep_arcmin"] = round(separation_arcmin(
            float(mount["ra"]), float(mount["dec"]),
            float(facts["ra_hours"]), float(facts["dec_degrees"])), 2)
    return out
