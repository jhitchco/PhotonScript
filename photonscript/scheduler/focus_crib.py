"""PS-213: the focus crib sheet, a printable focuser position by temperature
and filter for manual work (where focus should be without running AF).

The numbers are the focus table's (focus_table.build_table): per RC16
filter position = intercept_L + slope x T + offset[filter], built once per
temperature on a fixed grid (CRIB_TEMPS, 0 to 25 C by 5). Temperatures
outside the measured AF range (focus model temp_range) are marked
"extrapolated" (the page greys them). Each filter column carries its slope,
offset and its source, confidence and the date of its newest AF.

Trust gate: the sheet is built only from a trusted model
(focus_model.trust, the same gate as drive mode). An untrusted RC16 model
gives "grid": None and the reasons, never numbers to print.

The Piggy-600 (one OSC channel on its own EAF) gets a row from its own
model (focus_model.piggyback_summary data) only when that model passes the
same readiness bar (8 AFs, a slope, scatter under half its CFZ, the CFZ
being the median AF step of its own runs).

Read only: nothing here moves a focuser or writes a file.
"""
from __future__ import annotations

import logging
from datetime import datetime

from photonscript.scheduler import focus_model as fm
from photonscript.scheduler import focus_table as ft

logger = logging.getLogger(__name__)

CRIB_TEMPS = (0, 5, 10, 15, 20, 25)


def _date(ts) -> str | None:
    s = str(ts or "")
    return s[:10] if len(s) >= 10 else None


def _extrapolated(t: float, rng) -> bool:
    """Outside the measured AF temperatures (no margin: a printed number
    beyond the data is marked even where the table still says high)."""
    if not rng or rng[0] is None or rng[1] is None:
        return True
    return not (float(rng[0]) <= t <= float(rng[1]))


def _piggy(config) -> dict:
    """The Piggy-600 row: {"trusted", "reasons", "slope_steps_per_c",
    "temp_range", "positions": {T: pos} | None, "n_afs", "last_af"}."""
    pts = fm.load_points(config, "piggyback")
    model = fm.fit(pts, ref_filter=fm.OSC_FILTER)
    steps = [x for p in pts if (x := fm._num(p.get("step"))) and x > 0]
    cfz = fm._median(steps) if steps else None
    limit = (round(fm._TRUST_CFZ_FRACTION * cfz, 1) if cfz
             else fm._TRUST_FALLBACK_SIGMA)
    rd = fm.af_skip_readiness(model, fm._TRUST_MIN_POINTS, limit)
    row = (model.get("filters") or {}).get(fm.OSC_FILTER) or {}
    out = {"trusted": bool(rd["ready"]), "reasons": rd["reasons"],
           "slope_steps_per_c": model.get("slope_steps_per_c"),
           "sigma_steps": model.get("sigma_steps"), "sigma_limit": limit,
           "temp_range": model.get("temp_range"),
           "n_afs": int(row.get("n") or 0),
           "last_af": _date(row.get("last_time")), "positions": None}
    if out["trusted"] and row:
        b = model.get("slope_steps_per_c") or 0.0
        out["positions"] = {str(t): {"position": int(round(row["intercept"]
                                                           + b * t)),
                                     "extrapolated": _extrapolated(
                                         t, model.get("temp_range"))}
                            for t in CRIB_TEMPS}
    return out


def build_crib(config, temps=CRIB_TEMPS, pts: list[dict] | None = None,
               piggy: bool = True) -> dict:
    """The crib sheet. Never raises (an "error" key instead)."""
    try:
        return _build(config, tuple(temps), pts, piggy)
    except Exception as e:  # noqa: BLE001 - a read-out never breaks a page
        logger.warning("focus_crib: build failed: %s", e)
        return {"error": str(e), "trusted": False, "grid": None,
                "trust_reasons": [str(e)]}


def _build(config, temps, pts, piggy) -> dict:
    pts = fm._rc16_points(config) if pts is None else pts
    # base: the table at the expected temperature (trust, slope, each
    # filter's offset / confidence); tabs: one table per grid temperature
    base = ft.build_table(config, pts=pts)
    tabs = {t: ft.build_table(config, temp=float(t), pts=pts) for t in temps}
    if base.get("error"):
        raise RuntimeError(base["error"])
    rng = base.get("temp_range")
    out = {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "rig": "rc16", "ref_filter": base.get("ref_filter"),
        "formula": base.get("formula"),
        "trusted": bool(base.get("trusted")),
        "trust_reasons": base.get("trust_reasons") or [],
        "slope_steps_per_c": base.get("slope_steps_per_c"),
        "slope_se": base.get("slope_se"),
        "sigma_steps": base.get("sigma_steps"),
        "sigma_limit": base.get("sigma_limit"),
        "cfz_steps": base.get("cfz_steps"), "cfz_source": base.get("cfz_source"),
        "confidence_temp": base.get("temp"),
        "confidence_basis": base.get("temp_basis"),
        "n_points": base.get("n_points"), "temp_range": rng,
        "counter": base.get("counter") or {},
        "temps": list(temps), "filters": [], "columns": {}, "grid": None,
    }
    if piggy and getattr(config, "piggyback_enabled", False):
        try:
            out["piggyback"] = _piggy(config)
        except Exception as e:  # noqa: BLE001 - the RC16 sheet stands alone
            out["piggyback"] = {"trusted": False, "reasons": [str(e)],
                                "positions": None}
    if not out["trusted"]:
        return out   # the gate: no numbers from an untrusted model
    rows = base.get("filters") or {}
    filters = [f for f, r in rows.items() if r.get("position") is not None]
    out["filters"] = filters
    for f in filters:
        r = rows[f]
        out["columns"][f] = {
            "offset": r.get("offset"), "offset_source": r.get("offset_source"),
            "offset_se": r.get("offset_se"), "n_afs": r.get("n_afs"),
            "confidence": r.get("confidence"),
            "last_af": _date((r.get("last_af") or {}).get("time")),
        }
    grid = []
    for t in temps:
        trow = tabs[t].get("filters") or {}
        grid.append({
            "temp": t, "extrapolated": _extrapolated(float(t), rng),
            "positions": {f: (trow.get(f) or {}).get("position")
                          for f in filters},
        })
    out["grid"] = grid
    return out
