"""APIRouter modules split out of the monolithic app.py (2355 lines, 86 routes).

Each router groups a cohesive slice of the API and is mounted in app.py via
`app.include_router(...)`. Handlers lazily import shared helpers
(`get_config`, `get_store`) from `photonscript.scheduler.app` at call time, so
there is no import cycle: app.py imports these router modules at the bottom,
after `app` and its helpers are defined.
"""
