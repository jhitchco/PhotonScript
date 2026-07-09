"""Per-filter autofocus seed table — a self-populating record of known-good
focuser positions vs. focuser temperature.

Why this exists
---------------
The RC16's filters are NOT parfocal (L sits ~190 EAF steps out from the
narrowband cluster) and best focus drifts with temperature. A cold-start
autofocus that begins from a wildly wrong position can fail to find enough
stars to build a valid HFR curve — which is how a whole night ends up as
out-of-focus donuts. Seeding each filter's autofocus with a MoveFocuserAbsolute
to the nearest known-good position guarantees AF starts with tight stars.

We still run a full NINA RunAutofocus every time — the seed is only a starting
point. After a successful night we HARVEST the measured FOCPOS/FOCTEMP from the
accepted subs back into this table, so the seeds self-improve over time and we
stop guessing the temperature slope.

Data
----
Records live in focus_seeds.json next to this module (repo-tracked defaults)
and, if present, an override copy in config.data_dir that harvest() appends to.
Each record: {"filter","focpos","foctemp","date","source"}.

seed_for(filter, foctemp) picks a position:
  - >=2 records at different temps for that filter -> linear interp/extrapolate
    (clamped to observed range +/- a margin)
  - else nearest-temp record, else most-recent record for the filter
  - else a global fallback.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MODULE_TABLE = Path(__file__).with_name("focus_seeds.json")

# Absolute clamp — never seed outside the EAF's sane travel for this scope.
_FOCPOS_MIN, _FOCPOS_MAX = 4000, 7000
_GLOBAL_FALLBACK = 5600  # only used if the table is empty for every filter


def _table_paths(config=None) -> list[Path]:
    paths = [_MODULE_TABLE]
    if config is not None:
        try:
            p = Path(config.data_dir) / "focus_seeds.json"
            if p.exists():
                paths.append(p)
        except Exception:  # noqa: BLE001
            pass
    return paths


def load_records(config=None) -> list[dict]:
    recs: list[dict] = []
    for p in _table_paths(config):
        try:
            if p.exists():
                recs.extend(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("focus_seeds: could not read %s: %s", p, e)
    return recs


def _clamp(pos: float) -> int:
    return int(max(_FOCPOS_MIN, min(_FOCPOS_MAX, round(pos))))


def seed_for(filter_name: str, foctemp: float | None = None,
             config=None) -> int:
    """Best-guess focuser start position for a filter at a given temperature."""
    recs = [r for r in load_records(config)
            if str(r.get("filter")) == str(filter_name) and r.get("focpos")]
    if not recs:
        # fall back to the whole-table median so we at least start in the ballpark
        allr = [r for r in load_records(config) if r.get("focpos")]
        if not allr:
            return _GLOBAL_FALLBACK
        vals = sorted(float(r["focpos"]) for r in allr)
        return _clamp(vals[len(vals) // 2])

    pts = [(float(r["foctemp"]), float(r["focpos"]))
           for r in recs if r.get("foctemp") is not None]
    temps = sorted({t for t, _ in pts})

    if foctemp is not None and len(temps) >= 2:
        # linear fit (least squares) across all temp/pos points, then clamp to
        # the observed position range widened by a small margin.
        n = len(pts)
        sx = sum(t for t, _ in pts); sy = sum(p for _, p in pts)
        sxx = sum(t * t for t, _ in pts); sxy = sum(t * p for t, p in pts)
        denom = n * sxx - sx * sx
        if denom != 0:
            slope = (n * sxy - sx * sy) / denom
            intercept = (sy - slope * sx) / n
            pos = slope * foctemp + intercept
            lo = min(p for _, p in pts) - 150
            hi = max(p for _, p in pts) + 150
            return _clamp(max(lo, min(hi, pos)))

    if foctemp is not None and pts:
        # nearest temperature record
        _, pos = min(pts, key=lambda tp: abs(tp[0] - foctemp))
        return _clamp(pos)

    # No temperature known: median position for this filter — a neutral
    # cold-start seed that AF refines. (Avoids favouring the warm or cool
    # extreme of the table.)
    vals = sorted(float(r["focpos"]) for r in recs)
    return _clamp(vals[len(vals) // 2])


def add_record(filter_name: str, focpos: float, foctemp: float | None,
               date: str, source: str, config) -> None:
    """Append one measured focus point to the data_dir override table."""
    p = Path(config.data_dir) / "focus_seeds.json"
    try:
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, json.JSONDecodeError):
        existing = []
    existing.append({"filter": filter_name, "focpos": int(round(focpos)),
                     "foctemp": (round(float(foctemp), 1)
                                 if foctemp is not None else None),
                     "date": date, "source": source})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(existing, indent=1), encoding="utf-8")


def harvest_night(config, date: str, max_hfr_px: float = 4.0) -> int:
    """After a night, read FOCPOS/FOCTEMP from that night's in-focus accepted
    subs and append one record per filter (the median position of the best
    frames). Returns how many filter-records were added.

    Reads the graded sub records (which carry the abs_path) and pulls the two
    focus keywords straight from the FITS header. Only frames that actually
    focused well (real HFR below max_hfr_px) contribute — so a bad night never
    poisons the table.
    """
    from photonscript.scheduler.runs import _load_subs

    by_filter: dict[str, list[tuple[float, float]]] = {}
    for s in _load_subs(config, date):
        if not s.get("passed_qa"):
            continue
        hfr = s.get("hfr")
        if hfr is None or hfr > max_hfr_px:
            continue
        path = s.get("abs_path")
        if not path:
            continue
        hdr = _read_focus_header(path)
        fp, ft = hdr.get("FOCPOS"), hdr.get("FOCTEMP")
        if fp is None:
            continue
        by_filter.setdefault(s.get("filter", "?"), []).append(
            (float(fp), float(ft) if ft is not None else None))

    added = 0
    for filt, pts in by_filter.items():
        positions = sorted(p for p, _ in pts)
        med_pos = positions[len(positions) // 2]
        temps = [t for _, t in pts if t is not None]
        med_temp = (sorted(temps)[len(temps) // 2] if temps else None)
        add_record(filt, med_pos, med_temp, date, "harvest", config)
        added += 1
    logger.info("focus_seeds: harvested %d filter-records from %s", added, date)
    return added


def _read_focus_header(path: str) -> dict:
    """Pull FOCPOS/FOCTEMP from a FITS header without a full FITS dependency."""
    out: dict = {}
    try:
        with open(path, "rb") as f:
            data = f.read(2880 * 12)
    except OSError:
        return out
    for i in range(0, len(data), 80):
        card = data[i:i + 80].decode("latin-1", "replace")
        key = card[:8].strip()
        if key == "END":
            break
        if key in ("FOCPOS", "FOCTEMP") and card[8:10] == "= ":
            val = card[10:].split("/")[0].strip().strip("'").strip()
            try:
                out[key] = float(val)
            except ValueError:
                pass
    return out
