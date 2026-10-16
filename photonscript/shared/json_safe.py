"""JSON responses that never 500 on a non-finite float.

2026-10-16: the scope log showed "ValueError: Out of range float values are
not JSON compliant: nan" once a minute from Starlette's JSONResponse (a
disconnected mount's alt / az reached a dashboard endpoint as NaN). Every
FastAPI response of the scheduler goes through SafeJSONResponse: NaN and
+-Infinity become null (recursively), then json.dumps(allow_nan=False).
"""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi.responses import JSONResponse as _StarletteJSON


def finite_or_none(v):
    """v as a finite float, or None for None / NaN / +-Inf / "NaN" text /
    anything not a number."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def sanitize(obj: Any) -> Any:
    """A copy of obj with every non-finite float replaced by None (dicts,
    lists, tuples recursed; numpy scalars unwrapped). Other values unchanged."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    item = getattr(obj, "item", None)
    if item is not None and type(obj).__module__ == "numpy":
        try:
            return sanitize(item())
        except (TypeError, ValueError):
            return obj
    return obj


class SafeJSONResponse(_StarletteJSON):
    """JSONResponse whose render turns NaN / Infinity into null."""

    def render(self, content: Any) -> bytes:
        return json.dumps(sanitize(content), ensure_ascii=False,
                          allow_nan=False, indent=None,
                          separators=(",", ":")).encode("utf-8")
