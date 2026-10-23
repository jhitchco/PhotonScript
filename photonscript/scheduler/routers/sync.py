"""PS-43 sync hygiene: what the Syncthing backlog is made of and why it stalls.

GET /api/sync/hygiene           per-folder backlog (complete census, astro vs
                                other), the "non-astronomy folders are being
                                synced" warning with the ignore patterns to
                                add, Syncthing folder errors and a diagnosis.
GET /api/sync/ignore-suggestion the ignore-pattern TEXT for review. It is
                                never applied: adding it in Syncthing is
                                Jeremy's step.

Observe only: served from the cache that a background thread fills
(scheduler/sync_hygiene.py), so a request never waits on Syncthing.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter()


def _state():
    """(settings or None, folder id, folder path, cached need) and kick the
    background refresh when due."""
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import sync_hygiene as sh
    settings = app_mod._syncthing_settings()
    if settings is None:
        return None, "", "", None
    sh.refresh_bg(settings, armed=app_mod._armer_active())
    cache = app_mod._remoteneed_cache
    need = None
    if isinstance(cache.get("need_items"), int):
        need = {"items": cache["need_items"], "bytes": cache.get("need_bytes")}
    path = (sh._cache.get("status") or {}).get("path", "")
    return settings, settings[2], path, need


@router.get("/api/sync/hygiene")
def api_sync_hygiene():
    from photonscript.scheduler import sync_hygiene as sh
    settings, folder_id, path, need = _state()
    if settings is None:
        return {"configured": False}
    out = sh.build_report(sh._cache, folder_id, path, need=need)
    out["need"] = need
    return out


@router.get("/api/sync/ignore-suggestion")
def api_sync_ignore_suggestion(format: str = "json"):
    """Generated ignore patterns for the non-astronomy folders. Read only;
    format=text returns just the lines to paste."""
    from photonscript.scheduler import sync_hygiene as sh
    settings, folder_id, path, _need = _state()
    if settings is None:
        return {"configured": False, "applied": False, "patterns": [],
                "text": ""}
    rows = sh.folder_rows(sh._cache.get("census"), top_n=0)
    out = sh.ignore_suggestion(rows, folder_id, path)
    out["configured"] = True
    out["census_complete"] = bool((sh._cache.get("census") or {})
                                  .get("complete"))
    if format == "text":
        return PlainTextResponse(out["text"] or "// nothing to ignore\n")
    return out
