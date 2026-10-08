"""PS-124: catalog lookups for the dashboard Add box.

* find a catalog row by name, catalog id or alias (astronomy.find_catalog_entry);
* parse "any target by RA/Dec" text ("NGC 604 01:34:33 +30:47", decimal
  "NGC 604 1.5758 30.783", or "01h34m33s +30d47m");
* keep the user catalog (<data_dir>/user_catalog.json): every coordinate add
  lands there, and astronomy.get_seasonal_targets() includes it, so the
  seasonal fallback, tonight's picker and the identify pass see it;
* size-aware creation defaults: rig hint, sub length per filter, the goal
  hours, narrowband mix and Piggy-600 OSC goal a catalog row carries.

No network: names resolve only against the built-in list and the user
catalog. Pure helpers here; the endpoint is routers/catalog.py.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from photonscript.shared import astronomy
from photonscript.shared.target_names import target_key

logger = logging.getLogger(__name__)

USER_CATALOG_FILE = "user_catalog.json"
DEFAULT_BUDGET_HOURS = 8.0
PICKER_LIMIT = 60
NB_FILTERS = ("Ha", "OIII", "SII")
BB_FILTERS = ("L", "R", "G", "B")


# --- RA/Dec text ---------------------------------------------------------------

_NUM = r"\d+(?:\.\d+)?"
# 01:34:33.2 / 01h34m33s / 01h34.5m / 01:34
_RA_SEX = (r"(?P<rh>\d{1,2})\s*[:h]\s*(?P<rm>\d{1,2}(?:\.\d+)?)"
           r"(?:\s*[:m]\s*(?P<rs>" + _NUM + r")\s*s?|\s*m)?")
# +30:47:00 / +30d47m / -05:23 / 30d47'12"
_DEC_SEX = (r"(?P<ds>[+-]?)\s*(?P<dd>\d{1,2})\s*[:d\u00b0]\s*"
            r"(?P<dm>\d{1,2}(?:\.\d+)?)"
            r"(?:\s*[:m']\s*(?P<dsec>" + _NUM + r")\s*(?:s|\"|'')?|\s*[m'])?")
_SEX_RE = re.compile(r"^(?P<name>.*?)\s*" + _RA_SEX + r"\s*,?\s+" + _DEC_SEX
                     + r"\s*$", re.IGNORECASE)
# 01 34 33 +30 47 00 (spaces only; the Dec needs its sign so the name's
# numbers cannot be read as coordinates)
_SPACED_RE = re.compile(
    r"^(?P<name>.*?)\s*(?P<rh>\d{1,2})\s+(?P<rm>\d{1,2})\s+(?P<rs>" + _NUM
    + r")\s+(?P<ds>[+-])(?P<dd>\d{1,2})\s+(?P<dm>\d{1,2})"
    r"(?:\s+(?P<dsec>" + _NUM + r"))?\s*$")
# 1.5758 30.783 (RA in hours, or degrees when over 24) - needs a decimal
# point in the RA or a signed Dec, so "NGC 604" alone is never coordinates
_DEC_RE = re.compile(
    r"^(?P<name>.*?)\s*(?P<ra>\d{1,3}(?:\.\d+)?)\s*,?\s+(?P<dec>[+-]?\d{1,2}(?:\.\d+)?)\s*$")


def _range_check(ra_h: float, dec_d: float) -> tuple[float, float]:
    if not 0 <= ra_h < 24:
        raise ValueError(f"RA {ra_h:g} h is outside 0 to 24 h")
    if not -90 <= dec_d <= 90:
        raise ValueError(f"Dec {dec_d:g} deg is outside -90 to +90")
    return round(ra_h, 5), round(dec_d, 5)


def _sex_to_float(m: re.Match) -> tuple[float, float]:
    rm, rs = float(m["rm"]), float(m["rs"] or 0)
    dm, dsec = float(m["dm"]), float(m["dsec"] or 0)
    if rm >= 60 or rs >= 60 or dm >= 60 or dsec >= 60:
        raise ValueError("minutes and seconds must be under 60")
    ra = int(m["rh"]) + rm / 60 + rs / 3600
    dec = int(m["dd"]) + dm / 60 + dsec / 3600
    return ra, -dec if m["ds"] == "-" else dec


def parse_target_text(text: str) -> tuple[str, float, float] | None:
    """(name, ra_hours, dec_degrees) from Add-box text, None when the text
    carries no coordinates. ValueError when it does but they are out of
    range. The name may be empty (the caller names it)."""
    s = " ".join(str(text or "").split())
    if not s:
        return None
    for rx in (_SEX_RE, _SPACED_RE):
        m = rx.match(s)
        if m:
            ra, dec = _sex_to_float(m)
            return (m["name"].strip(" ,"), *_range_check(ra, dec))
    m = _DEC_RE.match(s)
    if m and ("." in m["ra"] or m["dec"][0] in "+-" or "." in m["dec"]):
        ra = float(m["ra"])
        if ra >= 24:          # degrees
            if ra >= 360:
                raise ValueError(f"RA {ra:g} is outside 0 to 360 deg")
            ra /= 15.0
        return (m["name"].strip(" ,"), *_range_check(ra, float(m["dec"])))
    return None


def coord_name(ra_h: float, dec_d: float) -> str:
    """A name for an unnamed coordinate target: "RA 01h34m Dec +30d47m"."""
    rh, rm = int(ra_h), int(round((ra_h % 1) * 60))
    if rm == 60:
        rh, rm = (rh + 1) % 24, 0
    sign = "-" if dec_d < 0 else "+"
    dd, dm = int(abs(dec_d)), int(round((abs(dec_d) % 1) * 60))
    if dm == 60:
        dd, dm = dd + 1, 0
    return f"RA {rh:02d}h{rm:02d}m Dec {sign}{dd:02d}d{dm:02d}m"


# --- user catalog --------------------------------------------------------------

def user_catalog_path(config) -> Path:
    return Path(config.data_dir) / USER_CATALOG_FILE


def load_user_catalog(config) -> list[dict]:
    """Read the user catalog and hand it to astronomy (so every
    get_seasonal_targets caller sees it). A bad file logs and loads nothing."""
    path = user_catalog_path(config)
    entries: list[dict] = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for e in raw.get("targets", []) if isinstance(raw, dict) else []:
                try:
                    entries.append(_clean_entry(e))
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("PS-124: skipping user catalog row %r: %s",
                                   e, exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("PS-124: cannot read %s: %s", path, exc)
    astronomy.set_user_targets(entries)
    return entries


def _clean_entry(e: dict) -> dict:
    ra, dec = _range_check(float(e["ra"]), float(e["dec"]))
    name = str(e["name"]).strip()
    if not name:
        raise ValueError("empty name")
    months = [int(m) for m in (e.get("months") or astronomy.months_for_ra(ra))
              if 1 <= int(m) <= 12]
    size = e.get("size")
    return {"name": name, "catalog_id": str(e.get("catalog_id") or ""),
            "ra": ra, "dec": dec, "type": str(e.get("type") or "custom"),
            "mag": e.get("mag"),
            "size": float(size) if size not in (None, "") else None,
            "months": months or astronomy.months_for_ra(ra),
            "hours": float(e.get("hours") or 10),
            "user": True}


def save_user_entry(config, entry: dict) -> dict:
    """Add or replace (same name, by target_key) a user catalog row; write
    the file and refresh astronomy's copy. Returns the stored row."""
    row = _clean_entry(entry)
    rows = [r for r in load_user_catalog(config)
            if target_key(r["name"]) != target_key(row["name"])]
    rows.append(row)
    path = user_catalog_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"targets": rows}, indent=2), encoding="utf-8")
    tmp.replace(path)
    astronomy.set_user_targets(rows)
    return row


# --- creation defaults ---------------------------------------------------------

def creation_defaults(entry: dict, config) -> dict:
    """Size-aware defaults for a new goal from a catalog row: rig hint,
    long-sub seconds per filter (config nb_exposure_s, 600 s on the 3 nm
    filters per PS-117; rc16_rgb_exposure_s for R/G/B and bb_exposure_s
    for L, PS-176), goal hours, the row's
    narrowband mix and Piggy-600 OSC hours (None = type default / none)."""
    from photonscript.scheduler.project_store import target_kind
    target = astronomy.entry_to_target(entry)
    kind = target_kind(target)
    from photonscript.scheduler.project_store import default_sub_seconds
    # PS-176: R/G/B take rc16_rgb_exposure_s, L bb_exposure_s
    subs = {f: default_sub_seconds(f, config)
            for f in (NB_FILTERS if kind == "narrowband" else BB_FILTERS)}
    mix = entry.get("mix") if kind == "narrowband" else None
    return {"rig_hint": astronomy.rig_hint(entry.get("size")),
            "kind": kind,
            "sub_seconds": subs,
            "goal_hours": float(entry.get("goal_hours") or DEFAULT_BUDGET_HOURS),
            "mix": dict(mix) if mix else None,
            "osc_hours": (float(entry["osc_hours"])
                          if entry.get("osc_hours") else None),
            "note": entry.get("note", "")}


def create_project(store, entry: dict, config,
                   budget_hours: float | None = None):
    """Add a goal for a catalog row with its creation defaults. budget_hours
    from the caller wins over the row's goal_hours. Returns (project,
    defaults)."""
    d = creation_defaults(entry, config)
    budget = float(budget_hours) if budget_hours else d["goal_hours"]
    proj = store.add_from_target(astronomy.entry_to_target(entry), budget)
    if d["mix"] or d["osc_hours"]:
        proj = store.update(proj.id, filter_mix=d["mix"],
                            osc_hours=d["osc_hours"])
    return proj, d


def picker_targets(ranked: list[dict], limit: int = PICKER_LIMIT) -> list[dict]:
    """Tonight's Add-box list: the best `limit` by hours visible, plus any
    PS-124 row (CATALOG_EXTRAS or user catalog) ranked below the cut, so a
    new entry never drops out of the picker."""
    head = list(ranked[:limit])
    extra_ids = set(astronomy.CATALOG_EXTRAS)
    user = {target_key(e["name"]) for e in astronomy._USER_TARGETS}
    for r in ranked[limit:]:
        t = r["target"]
        if t.catalog_id in extra_ids or target_key(t.name) in user:
            head.append(r)
    return head
