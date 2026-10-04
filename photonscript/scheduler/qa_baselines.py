"""PS-114: data-driven QA baselines per rig (report only, never changes a gate).

    baselines(config, rig=None, nights=14, k=None, records=None) -> dict
    format_baselines(rep) -> str                     plain text for the CLI

For each rig, and each filter of that rig, the accepted subs (passed_qa) of
the last N nights with a subs log give the median and MAD of FWHM (arcsec,
live-measured only: backfill records carry HFR x scale, not a FWHM), HFR
(native px), eccentricity (the gating scale, sqrt form), star count and
background (ADU). Each gate gets a proposal next to the gate in force:

    max gates (FWHM, HFR, ecc):    median + k x sigma
    HFR outlier / background:      median + k x sigma of each sub's ratio to
                                   its night's median (same rig, target,
                                   filter): the factor the relative checks use
    min stars:                     the lower of median - k x sigma and a
                                   quarter of the median (a sub with a quarter
                                   of a normal sub's stars is cloud), at
                                   least 1

sigma = 1.4826 x MAD (the robust standard deviation; MAD alone understates
a normal spread by a third). k is qa_baseline_k (3: about 99.7% of a normal
night stays inside). The rig-wide row (filter "*") is the one a gate is set
from, since gates are per rig; the per-filter rows show where a filter
differs. A proposal needs MIN_SUBS (20) accepted subs, else it is left
blank.

Caveat printed with every report: the subs are the ones today's gates
accepted, so the upper tail is cut at today's gate. A proposal at or near
the current gate means the gate is what limits the sample, not the sky.
Gates are changed by hand (System page or .env), never from here.
"""

from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

MAD_SIGMA = 1.4826
MIN_SUBS = 20
CAVEAT = ("Built from subs today's gates accepted, so each metric's upper "
          "tail is cut at its current gate: a proposal close to the current "
          "gate means the gate limits the sample. Report only: gates change "
          "by hand (System page or .env).")

# metric -> (thresholds key it proposes, how, round to)
PROPOSE = {
    "fwhm": ("fwhm_max", "max", 0.1),
    "hfr": ("hfr_max", "max", 0.05),
    "ecc": ("ecc_max", "max", 0.01),
    "stars": ("star_min", "min", 1),
    "hfr_ratio": ("hfr_rel_factor", "max", 0.05),
    "bg_ratio": ("bg_rel_max", "max", 0.05),
    "background": (None, None, 1),
}


def _num(v):
    from photonscript.shared.qa_rules import _num as n
    return n(v)


def median(xs: list) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def mad(xs: list, med: float | None = None) -> float | None:
    if not xs:
        return None
    med = median(xs) if med is None else med
    return median([abs(x - med) for x in xs])


def _round(v, step):
    if v is None:
        return None
    return round(round(v / step) * step, 4) if step < 1 else int(round(v))


def stats(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    med = median(xs)
    m = mad(xs, med)
    return {"n": len(xs), "median": None if med is None else round(med, 4),
            "mad": None if m is None else round(m, 4),
            "sigma": None if m is None else round(MAD_SIGMA * m, 4)}


def propose(metric: str, st: dict, k: float, min_subs: int = MIN_SUBS):
    """The proposed gate for one metric's stats (None: too few subs, or the
    metric proposes no gate)."""
    _key, how, step = PROPOSE.get(metric, (None, None, 1))
    if how is None or st["n"] < min_subs or st["median"] is None:
        return None
    med, sig = st["median"], st["sigma"] or 0.0
    if how == "max":
        return _round(med + k * sig, step)
    return max(1, _round(min(med - k * sig, med / 4.0), 1))


def _night_list(config, nights: int) -> list[str]:
    from photonscript.scheduler.runs import runs_dir
    dates = sorted({p.name[:10] for p in runs_dir(config).glob("*_subs.jsonl")},
                   reverse=True)
    return dates[:max(0, int(nights))]


def _load(config, nights: int) -> list[dict]:
    from photonscript.scheduler.runs import _load_subs
    out = []
    for d in _night_list(config, nights):
        for r in _load_subs(config, d):
            r["_night"] = d
            out.append(r)
    return out


def _accepted(rec: dict) -> bool:
    """Accepted, or waiting for review with a passing verdict (a manual
    reject clears passed_qa)."""
    return bool(rec.get("passed_qa"))


def baselines(config, rig: str | None = None, nights: int = 14,
              k: float | None = None, records: list[dict] | None = None,
              min_subs: int | None = None) -> dict:
    """Per rig and filter: median / MAD of each metric over the accepted
    subs of the last `nights` nights, the proposed gates and the gates in
    force. `records` reports on given records instead (their `_night` or
    the date in `file` groups the ratios). Writes nothing."""
    from photonscript.shared import qa_rules
    from photonscript.shared.rigs import rig_label
    k = float(getattr(config, "qa_baseline_k", 3.0) if k is None else k)
    min_subs = MIN_SUBS if min_subs is None else int(min_subs)
    recs = list(records) if records is not None else _load(config, nights)
    lights = [r for r in recs if str(r.get("image_type") or "LIGHT").upper()
              == "LIGHT"]
    # night medians per (night, rig, target, filter), over every sub of the
    # group (as qa_rules.night_context does), for the ratio metrics
    groups: dict = defaultdict(lambda: {"hfr": [], "bg": []})
    for r in lights:
        g = groups[(r.get("_night") or str(r.get("file") or "")[:10],)
                   + qa_rules.group_key(r)]
        h, b = _num(r.get("hfr")), _num(r.get("background"))
        if h:
            g["hfr"].append(h)
        if b is not None:
            g["bg"].append(b)
    nmed = {key: (median(v["hfr"]) if len(v["hfr"]) >= 3 else None,
                  median(v["bg"]) if len(v["bg"]) >= 3 else None)
            for key, v in groups.items()}
    ts: dict = {}
    vals: dict = defaultdict(lambda: defaultdict(list))
    used_nights: dict = defaultdict(set)
    for r in lights:
        rg = r.get("rig") or "rc16"
        if rig and rg != rig:
            continue
        if not _accepted(r):
            continue
        t = ts.get(rg) or ts.setdefault(rg, qa_rules.thresholds(config, rg))
        m = qa_rules.metrics_from_record(r)
        night = r.get("_night") or str(r.get("file") or "")[:10]
        hm, bm = nmed.get((night,) + qa_rules.group_key(r), (None, None))
        row = {"fwhm": _num(m.get("fwhm_arcsec")), "hfr": _num(m.get("hfr")),
               "ecc": qa_rules.gating_ecc(r, t)[0],
               "stars": _num(m.get("stars")),
               "background": _num(m.get("background"))}
        row["hfr_ratio"] = (row["hfr"] / hm if row["hfr"] and hm else None)
        row["bg_ratio"] = (row["background"] / bm
                           if row["background"] is not None and bm else None)
        for f in (str(r.get("filter") or "?"), "*"):
            for metric, v in row.items():
                if v is not None:
                    vals[(rg, f)][metric].append(v)
            used_nights[(rg, f)].add(night)
    rigs_out = {}
    for (rg, f) in sorted(vals, key=lambda x: (x[0], x[1] != "*", x[1])):
        t = ts[rg]
        mets = {}
        for metric in PROPOSE:
            st = stats(vals[(rg, f)].get(metric, []))
            key = PROPOSE[metric][0]
            st["gate_key"] = key
            st["current"] = t.get(key) if key else None
            st["proposed"] = propose(metric, st, k, min_subs)
            mets[metric] = st
        n_subs = max((len(v) for v in vals[(rg, f)].values()), default=0)
        r_out = rigs_out.setdefault(rg, {"rig": rg, "name": rig_label(config, rg),
                                         "filters": []})
        r_out["filters"].append({"filter": f, "subs": n_subs,
                                 "nights": len(used_nights[(rg, f)]),
                                 "metrics": mets})
    return {"nights": nights if records is None else None,
            "k": k, "min_subs": min_subs, "sigma": f"{MAD_SIGMA} x MAD",
            "caveat": CAVEAT, "rigs": list(rigs_out.values())}


_LABEL = {"fwhm": "FWHM \"", "hfr": "HFR px", "ecc": "ecc", "stars": "stars",
          "background": "bkg ADU", "hfr_ratio": "HFR/night",
          "bg_ratio": "bkg/night"}


def _f(v):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return str(v)
    x = float(v)
    return f"{x:.0f}" if abs(x) >= 100 or x == int(x) else f"{x:.3g}"


def format_baselines(rep: dict) -> str:
    lines = [f"QA baselines (PS-114): k = {rep['k']:g}, sigma = {rep['sigma']}, "
             f"proposals need {rep['min_subs']} accepted subs"
             + (f", last {rep['nights']} nights" if rep.get("nights") else "")]
    if not rep["rigs"]:
        lines.append("  no accepted subs found")
    for rg in rep["rigs"]:
        for fl in rg["filters"]:
            lines.append(f"{rg['name']} ({rg['rig']}) filter {fl['filter']}: "
                         f"{fl['subs']} accepted subs, {fl['nights']} nights")
            lines.append(f"    {'metric':<10} {'n':>5} {'median':>8} {'MAD':>7} "
                         f"{'proposed':>9} {'current':>8}  gate")
            for metric, st in fl["metrics"].items():
                if not st["n"]:
                    continue
                lines.append(
                    f"    {_LABEL[metric]:<10} {st['n']:>5} {_f(st['median']):>8} "
                    f"{_f(st['mad']):>7} {_f(st['proposed']):>9} "
                    f"{_f(st['current']):>8}  {st['gate_key'] or '(info)'}")
    lines.append("Note: " + rep["caveat"])
    return "\n".join(lines)
