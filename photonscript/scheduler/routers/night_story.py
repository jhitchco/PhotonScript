"""PS-212 endpoints: the night storyline and the weekly push counts
(scheduler/night_story.py).

GET /api/night/story?night=YYYY-MM-DD&kinds=act,story&sent_only=false
    the storyline the pushes tell (default: tonight, noon to noon local):
    {"night", "entries": [{"t", "kind", "title", "text", "sent", "reason",
    "thumb"?}], "pushed", "by_kind"}. thumb is a /api/runs/.../thumb URL.
GET /api/pushover/weekly?days=7
    pushes per night and kind from the audit log, top titles, held reasons
    (tune the noise with numbers).

Plain defs: FastAPI runs them in a worker thread (file reads only).
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/night/story")
def api_night_story(night: str = "", kinds: str = "", sent_only: bool = False):
    from photonscript.scheduler.night_story import story
    ks = [k.strip() for k in kinds.split(",") if k.strip()] or None
    return story(_cfg(), night or None, kinds=ks, include_held=not sent_only)


@router.get("/api/pushover/weekly")
def api_pushover_weekly(days: int = 7):
    from photonscript.scheduler.night_story import weekly_counts
    return weekly_counts(_cfg(), days=max(1, min(int(days), 60)))
