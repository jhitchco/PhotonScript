"""PS-113: what calibration each rig needs vs what QA-passed frames it has.

    rig_needs(config, rig, projects=None) -> list[dict]
        The calibration sets one rig needs at its CURRENT epoch (its
        gain / offset / setpoint / bin 1): darks for every exposure named by
        the config dark list, the active goals (long and HDR short subs),
        tonight's plan snapshot and the accepted lights of active goals in the
        Library (header EXPTIME at the rig's epoch: this is where the M31
        Piggy-600 300 s and 400 s subs come from); one bias set; flats per
        filter (needs-only: no flat panel, flats stay on the dusk/dawn paths).

    gap_report(config, rig=None, projects=None) -> dict
        Per rig: need / have (QA-passed) / bad (QA-failed) / gap per set,
        frames on disk not yet QA'd, the capturable plan (darks + bias,
        largest gap first) with its time estimate, and the daytime capture
        state. Body of GET /api/calibration/plan and `photonscript
        calibration-plan`.

Counting: darks = QA-passed frames of the epoch within library_cal_days;
bias = QA-passed in the newest bias session; flats = QA-passed in each
filter's newest session within 45 days (calibration.STALE_DAYS).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

BIAS_COUNT = 50              # the bias set size every existing path shoots
FRAME_OVERHEAD_S = 5.0       # download + save per frame (26 MP, USB3)
BIAS_FRAME_S = 4.0           # one bias incl. download
COOL_ESTIMATE_MIN = 10.0     # typical pull-down to 0 C in the daytime


def rig_epoch(config, rig: str) -> dict:
    from photonscript.shared.rigs import rig_config, rig_readout, rig_setpoint
    view = rig_config(config, rig)
    return {"gain": int(view.default_gain), "offset": int(view.default_offset),
            "setpoint": rig_setpoint(config, rig), "binning": 1,
            "readout": rig_readout(config, rig)}   # PS-128


def load_projects(config) -> list:
    """Active goals: the running service's store when it is loaded, else
    projects.json read-only (the CLI must not trigger store migrations)."""
    try:
        from photonscript.scheduler import app as _app
        if getattr(_app, "_store", None) is not None:
            return [p for p in _app._store.projects.values() if p.active]
    except Exception:  # noqa: BLE001
        pass
    from photonscript.shared.models import ImagingProject
    p = Path(config.data_dir) / "projects.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for v in raw.values():
        try:
            pr = ImagingProject(**v)
        except Exception:  # noqa: BLE001
            continue
        if pr.active:
            out.append(pr)
    return out


def _floats(csv) -> list[float]:
    out = []
    for tok in str(csv or "").split(","):
        try:
            out.append(float(tok.strip()))
        except ValueError:
            continue
    return out


def _tonight_plan_exposures(config) -> list[tuple[float, str]]:
    """(exp_s, label) of tonight's RC16 plan snapshot (runs/<night>_plan.json,
    the newest one from the last two days)."""
    return [(e, label) for e, _f, label in tonight_plan_rows(config)]


def tonight_plan_rows(config) -> list[tuple[float, str, str]]:
    """(exp_s, filter, label) of tonight's RC16 plan snapshot (PS-122: the
    Calibration owed view needs the filter too)."""
    from photonscript.scheduler.runs import runs_dir
    try:
        snaps = sorted(runs_dir(config).glob("*_plan.json"))
    except OSError:
        return []
    cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    snaps = [s for s in snaps if s.name[:10] >= cutoff]
    if not snaps:
        return []
    try:
        snap = json.loads(snaps[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for t in snap.get("targets") or []:
        for e in t.get("exposures") or []:
            if e.get("exp_s"):
                out.append((float(e["exp_s"]), str(e.get("filter") or "?"),
                            f"tonight {snap.get('night_of')}: {t.get('name')}"))
    return out


_light_cache: dict = {}  # folder -> ((n files, newest mtime), {(exp, g, o, t)})


def _library_light_epochs(folder: Path, limit: int = 400) -> set:
    """{(EXPTIME, GAIN, OFFSET, SET-TEMP)} of the lights in one Library
    target/filter folder (header reads, cached on the folder's file list)."""
    from astropy.io import fits as _fits
    try:
        files = sorted(folder.glob("*.fits"))
        sig = (len(files), max((f.stat().st_mtime for f in files), default=0))
    except OSError:
        return set()
    hit = _light_cache.get(str(folder))
    if hit and hit[0] == sig:
        return hit[1]
    out = set()
    for f in files[:limit]:
        try:
            h = _fits.getheader(f)
            out.add((round(float(h.get("EXPTIME", 0)), 1), h.get("GAIN"),
                     h.get("OFFSET"), h.get("SET-TEMP")))
        except Exception:  # noqa: BLE001
            continue
    _light_cache[str(folder)] = (sig, out)
    return out


def rig_needs(config, rig: str, projects=None) -> list[dict]:
    """See module doc. Each set: {"type", "exp_s", "filter", "need",
    "sources", "capturable"}."""
    from photonscript.shared.rigs import RC16
    if projects is None:
        projects = load_projects(config)
    ep = rig_epoch(config, rig)
    quota = int(getattr(config, "dark_target_count", 30))
    darks: dict[float, list[str]] = {}

    def add(exp, src):
        try:
            e = round(float(exp), 1)
        except (TypeError, ValueError):
            return
        if e <= 0.01:
            return
        darks.setdefault(e, [])
        if src not in darks[e]:
            darks[e].append(src)

    if rig == RC16:
        from photonscript.scheduler.nina_sequence_json import dark_library_exposures
        for e in dark_library_exposures(config):
            add(e, "config dark_exposures" if any(
                abs(e - x) < 0.5 for x in _floats(getattr(config, "dark_exposures", "")))
                else "PS-66 unguided cap")
        for e, src in _tonight_plan_exposures(config):
            add(e, src)
    else:
        for e in _floats(getattr(config, "piggyback_dark_exposures", "120")):
            add(e, "config piggyback_dark_exposures")
        add(getattr(config, "piggyback_exposure_s", 120.0), "piggyback_exposure_s")
    filters: dict[str, list[str]] = {}
    from photonscript.scheduler.runs import library_root, library_target_dirs
    lib = library_root(config)
    for p in projects:
        name = p.target.name
        plan_filters = set()
        for plan in p.exposure_plans:
            if (getattr(plan, "rig", RC16) or RC16) != rig:
                continue
            add(plan.exposure_seconds, f"goal {name} {plan.filter_type.value}")
            if plan.hdr_short_seconds and plan.hdr_short_count:
                add(plan.hdr_short_seconds, f"goal {name} {plan.filter_type.value} HDR short")
            f = "OSC" if rig != RC16 else plan.filter_type.value
            plan_filters.add(f)
            filters.setdefault(f, [])
            if name not in filters[f]:
                filters[f].append(name)
        for f in plan_filters:
            for td in library_target_dirs(lib, name, known=projects):
                for exp, g, o, t in _library_light_epochs(td / f):
                    if (g == ep["gain"] and o == ep["offset"]
                            and t is not None and abs(float(t) - ep["setpoint"]) < 1.5):
                        add(exp, f"lights {name} {f} in the Library")
    if rig != RC16 and not filters:
        filters["OSC"] = []
    out = [{"type": "DARK", "exp_s": e, "filter": None, "need": quota,
            "sources": srcs, "capturable": True} for e, srcs in sorted(darks.items())]
    out.append({"type": "BIAS", "exp_s": 0.001, "filter": None, "need": BIAS_COUNT,
                "sources": ["master bias"], "capturable": True})
    nflat = int(getattr(config, "flat_count", 15) if rig == RC16
                else getattr(config, "piggyback_flat_count", 25))
    for f, names in sorted(filters.items()):
        out.append({"type": "FLAT", "exp_s": None, "filter": f, "need": nflat,
                    "sources": [f"goal {n}" for n in names] or ["OSC lights"],
                    "capturable": False})
    return out


def _bad_counts(store: dict, ep: dict, days: int) -> dict:
    """QA-failed frames per (TYPE, exp or filter) of the rig's epoch."""
    from photonscript.scheduler.calibration_qa import _within_days
    out: dict[tuple, int] = {}
    for r in store["frames"].values():
        if r.get("verdict") != "fail":
            continue
        if r.get("gain") != ep["gain"] or r.get("offset") != ep["offset"]:
            continue
        if not _within_days(r.get("date"), days):
            continue
        k = (r.get("type"), round(r.get("exptime") or 0, 1)
             if r.get("type") == "DARK" else None)
        out[k] = out.get(k, 0) + 1
    return out


def estimate_minutes(blocks: list[tuple[float, int]], bias: int = 0) -> float:
    return (sum((e + FRAME_OVERHEAD_S) * n for e, n in blocks)
            + bias * BIAS_FRAME_S) / 60.0


def _unchecked(config, rig: str, store: dict) -> int:
    from photonscript.scheduler.calibration import iter_calibration_frames
    from photonscript.scheduler.calibration_qa import frame_key, rig_view
    n = 0
    try:
        for typ, date, f in iter_calibration_frames(rig_view(config, rig)):
            if frame_key(typ, date, f.name) not in store["frames"]:
                n += 1
    except Exception:  # noqa: BLE001
        return -1
    return n


def gap_report(config, rig: str | None = None, projects=None) -> dict:
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.scheduler.calibration import STALE_DAYS, darks_have
    from photonscript.shared.rigs import rig_ids, rig_label
    rigs = [rig] if rig else rig_ids(config)
    if projects is None:
        projects = load_projects(config)
    days = int(getattr(config, "library_cal_days", 120))
    out = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "qa_mode": cq.mode(config), "rigs": []}
    for rg in rigs:
        view = cq.rig_view(config, rg)
        ep = rig_epoch(config, rg)
        store = cq.load_store(config, rg)
        bad = _bad_counts(store, ep, days)
        flats = cq.count_passed_flats(view, rg, gain=ep["gain"], offset=ep["offset"],
                                      stale_days=STALE_DAYS["FLAT"], store=store)
        sets = []
        for s in rig_needs(config, rg, projects):
            if s["type"] == "DARK":
                # PS-122: the night quota's own count (calibration.darks_have)
                have = darks_have(view, rg, s["exp_s"], gain=ep["gain"],
                                  offset=ep["offset"], setpoint=ep["setpoint"],
                                  store=store)
                label = f"dark {s['exp_s']:g} s"
                nbad = bad.get(("DARK", s["exp_s"]), 0)
            elif s["type"] == "BIAS":
                have = cq.count_passed_bias(view, rg, gain=ep["gain"],
                                            offset=ep["offset"], store=store)
                label = "bias"
                nbad = bad.get(("BIAS", None), 0)
            else:
                have = flats.get(s["filter"], 0)
                label = f"flat {s['filter']}"
                nbad = bad.get(("FLAT", None), 0) if rg != "rc16" else None
            gap = max(0, s["need"] - have)
            sets.append({**s, "label": label, "have": have, "bad": nbad, "gap": gap,
                         "text": f"{rig_label(config, rg)} {label}: {have} of {s['need']}"})
        capture = capture_plan(sets, budget_min=max(
            0.0, float(getattr(config, "calibration_capture_budget_min", 240) or 240)
            - COOL_ESTIMATE_MIN))
        capture["full_minutes"] = capture_plan(sets)["minutes"]
        out["rigs"].append({
            "rig": rg, "name": rig_label(config, rg), "epoch": ep,
            "qa_checked": len(store["frames"]),
            "unchecked": _unchecked(config, rg, store),
            "sets": sets, "capture": capture,
            "daytime": cq.daytime_state(config, rg)})
    return out


def capture_plan(sets: list[dict], budget_min: float | None = None,
                 count: int | None = None) -> dict:
    """Darks + bias to shoot, largest gap first (share of the set missing,
    then the shorter exposure), trimmed to budget_min. count overrides each
    dark set's frame count. Returns {"darks": [[exp, n]], "bias": n,
    "minutes": est, "trimmed": bool}."""
    want = [s for s in sets if s.get("capturable") and s["gap"] > 0]
    want.sort(key=lambda s: (-(s["gap"] / s["need"] if s["need"] else 0),
                             s["exp_s"] or 0))
    darks, bias, trimmed = [], 0, False
    left = None if budget_min is None else max(0.0, budget_min * 60.0)
    for s in want:
        n = int(count) if (count and s["type"] == "DARK") else int(s["gap"])
        per = (BIAS_FRAME_S if s["type"] == "BIAS" else s["exp_s"] + FRAME_OVERHEAD_S)
        if left is not None:
            fit = int(left // per)
            if fit < n:
                n, trimmed = fit, True
            left -= n * per
        if n <= 0:
            continue
        if s["type"] == "BIAS":
            bias = n
        else:
            darks.append([s["exp_s"], n])
    return {"darks": darks, "bias": bias, "trimmed": trimmed,
            "minutes": round(estimate_minutes([(e, n) for e, n in darks], bias), 1)}


def format_report(rep: dict) -> str:
    lines = [f"Calibration plan ({rep['generated']}, QA mode {rep['qa_mode']})"]
    for r in rep["rigs"]:
        ep = r["epoch"]
        lines.append("")
        lines.append(f"{r['name']} ({r['rig']}): gain {ep['gain']} offset {ep['offset']} "
                     f"setpoint {ep['setpoint']:g} C bin 1; {r['qa_checked']} frames QA'd, "
                     f"{r['unchecked']} on disk not QA'd yet; daytime capture "
                     f"{r['daytime'].get('status')}")
        lines.append(f"  {'set':<16}{'need':>6}{'have':>6}{'bad':>6}{'gap':>6}  sources")
        for s in r["sets"]:
            bad = "-" if s["bad"] is None else s["bad"]
            note = "" if s["capturable"] else " (flats: dusk/dawn only)"
            lines.append(f"  {s['label']:<16}{s['need']:>6}{s['have']:>6}{bad:>6}"
                         f"{s['gap']:>6}  {'; '.join(s['sources'][:3])}{note}")
        c = r["capture"]
        if c["darks"] or c["bias"]:
            plan = " + ".join(f"{e:g} s x {n}" for e, n in c["darks"])
            if c["bias"]:
                plan += (" + " if plan else "") + f"{c['bias']} bias"
            lines.append(f"  capture now: {plan}, about {c['minutes'] + COOL_ESTIMATE_MIN:.0f} "
                         "min with cooling"
                         + (f" (trimmed to the budget; the whole gap is about "
                            f"{c['full_minutes'] + COOL_ESTIMATE_MIN:.0f} min)"
                            if c.get("trimmed") else ""))
        else:
            lines.append("  capture: nothing to shoot (darks and bias at quota)")
    return "\n".join(lines)
