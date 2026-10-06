"""PS-94: eccentricity at the native 0.236"/px vs the 2x2-binned 0.47"/px.

The RC16 samples 0.236"/px against 1.5 to 2" seeing; the integration output
(the _bin2 masters) is 0.47"/px. This report answers "would grading at the
binned scale pass more subs?" on a real night, with nothing else changed:

* one FITS load per RC16 light, both scales measured with the SAME pipeline
  (shared.star_shape.measure, the live grader's) and the same formula
  (sqrt(1-(b/a)^2)), so the delta is scale only;
* per target + filter: medians at each scale and the median per-sub delta;
* pass counts per gate (default 0.60 and 0.70) per scale, and the subs that
  would flip;
* what the stored records say: how many were graded by which grader
  (`graded_by`; pre-PS-94 "sep-binned" records hold 1-b/a) and the stored
  ecc in sqrt form next to the fresh native measure (the formula effect).

DRY RUN ONLY: it writes <data_dir>/reports/ps94_<date>.json and never
touches <date>_subs.jsonl. Full-frame work runs one sub at a time under
runs._HEAVY. CLI `photonscript ecc-scale-report`, API GET /api/qa/ecc-scale.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median

logger = logging.getLogger(__name__)

DEFAULT_GATES = (0.60, 0.70)
KEEP_NATIVE_BELOW = 0.02   # grooming rule: median delta under this = keep native


def report_path(config, date: str) -> Path:
    return Path(config.data_dir) / "reports" / f"ps94_{date}.json"


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(median(xs), 3) if xs else None


def _measure_file(path: Path) -> dict:
    """Both scales from one load of the frame."""
    from photonscript.scheduler import runs
    from photonscript.shared import star_shape
    with runs._HEAVY:
        data = runs._load_native(path)
        nat = star_shape.measure(data, binned=False)
        b = star_shape.bin2x2_mean(data)
        del data
        binned = star_shape.measure(b, binned=True)
        del b
    if nat is None or binned is None:
        raise RuntimeError("sep not installed (pip install sep-pjw)")
    return {"ecc": nat["ecc"], "hfr": nat["hfr_px"], "stars": nat["n"],
            "ecc_bin": binned["ecc"], "hfr_bin": binned["hfr_px"],
            "stars_bin": binned["n"]}


def compare_night(config, date: str, gates=DEFAULT_GATES,
                  write: bool = True) -> dict:
    """Measure every RC16 light of one night at both scales (dry run)."""
    from astropy.io import fits
    from photonscript.scheduler import runs
    from photonscript.shared import qa_rules
    gates = tuple(round(float(g), 3) for g in gates)
    root = Path(config.image_watch_dir) / date
    records = runs._load_subs(config, date)
    by_file = {str(r.get("file") or "").replace("\\", "/"): r for r in records}
    rc16_recs = [r for r in records if (r.get("rig") or "rc16") == "rc16"]
    # which formula produced the stored numbers (PS-88's "0.52 to 0.58")
    graded_by = Counter(r.get("graded_by") or "live" for r in rc16_recs)
    n_lin = sum(1 for r in rc16_recs if r.get("ecc_def") is None
                and r.get("graded_by") == "sep-binned")
    plan_names = runs._plan_target_names(config, date)
    files = runs._light_files(root) if root.exists() else []
    t_start = time.monotonic()
    subs, errors = [], []
    for f in files:
        rel = str(f.relative_to(root)).replace("\\", "/")
        rec = by_file.get(rel) or {}
        if (rec.get("rig") or "rc16") != "rc16":
            continue
        t0 = time.monotonic()
        try:
            hdr = fits.getheader(f)
            m = _measure_file(f)
        except MemoryError:
            errors.append({"file": rel, "error": "MemoryError"})
            continue
        except Exception as e:  # noqa: BLE001
            errors.append({"file": rel, "error": str(e)})
            continue
        target = rec.get("target") or runs._resolve_target(
            hdr.get("OBJECT"), f.name, plan_names)
        flt = rec.get("filter") or hdr.get("FILTER", "?")
        e_n, e_b = m["ecc"], m["ecc_bin"]
        subs.append({
            "file": rel, "target": target, "filter": flt,
            "exp_s": float(hdr.get("EXPTIME", 0) or 0),
            "ecc": None if e_n is None else round(e_n, 3),
            "ecc_bin": None if e_b is None else round(e_b, 3),
            "delta": None if e_n is None or e_b is None
            else round(e_b - e_n, 3),
            "hfr": None if m["hfr"] is None else round(m["hfr"], 2),
            "hfr_bin": None if m["hfr_bin"] is None else round(m["hfr_bin"], 2),
            "stars": m["stars"], "stars_bin": m["stars_bin"],
            "stored": {"graded_by": rec.get("graded_by") or (
                "live" if rec else None),
                "ecc": rec.get("ecc"),
                "ecc_sqrt": None if not rec else (
                    None if qa_rules.record_ecc(rec) is None
                    else round(qa_rules.record_ecc(rec), 3)),
                "passed_qa": rec.get("passed_qa")},
            "secs": round(time.monotonic() - t0, 1),
        })

    def counts(rows):
        out = {}
        for g in gates:
            nat = [r for r in rows if r["ecc"] is not None and r["ecc"] <= g]
            binned = [r for r in rows if r["ecc_bin"] is not None
                      and r["ecc_bin"] <= g]
            out[f"{g:g}"] = {"native": len(nat), "binned": len(binned)}
        return out

    groups = {}
    for r in subs:
        groups.setdefault((r["target"], r["filter"]), []).append(r)
    group_rows = [{
        "target": t, "filter": fl, "n": len(rows),
        "ecc_median": _med([r["ecc"] for r in rows]),
        "ecc_bin_median": _med([r["ecc_bin"] for r in rows]),
        "delta_median": _med([r["delta"] for r in rows]),
        "stored_ecc_sqrt_median": _med([r["stored"]["ecc_sqrt"]
                                        for r in rows]),
        "pass": counts(rows)}
        for (t, fl), rows in sorted(groups.items())]
    flips = {}
    for g in gates:
        key = f"{g:g}"
        flips[key] = {
            "pass_binned_only": [r["file"] for r in subs
                                 if r["ecc_bin"] is not None
                                 and r["ecc"] is not None
                                 and r["ecc_bin"] <= g < r["ecc"]],
            "pass_native_only": [r["file"] for r in subs
                                 if r["ecc_bin"] is not None
                                 and r["ecc"] is not None
                                 and r["ecc"] <= g < r["ecc_bin"]]}
    d_med = _med([r["delta"] for r in subs])
    stored_vs = [r["stored"]["ecc_sqrt"] - r["ecc"] for r in subs
                 if r["stored"]["ecc_sqrt"] is not None
                 and r["ecc"] is not None]
    if d_med is None:
        verdict = "no measured subs"
    elif abs(d_med) < KEEP_NATIVE_BELOW:
        verdict = (f"median binned-native delta {d_med:+.3f} is under "
                   f"{KEEP_NATIVE_BELOW:g}: keep grading native")
    else:
        verdict = (f"median binned-native delta {d_med:+.3f}: binning moves "
                   "the gate; Jeremy decides (qa_ecc_scale)")
    rep = {
        "ticket": "PS-94", "date": date, "dry_run": True,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "gates": list(gates), "ecc_def": "sqrt(1-(b/a)^2)",
        "n_files": len(files), "n_measured": len(subs),
        "n_records": len(rc16_recs), "graded_by": dict(graded_by),
        "n_records_lin": n_lin,
        "delta_median": d_med,
        "stored_minus_native_median": _med(stored_vs),
        "pass": counts(subs), "flips": flips, "groups": group_rows,
        "verdict": verdict, "secs": round(time.monotonic() - t_start, 1),
        "errors": errors, "subs": subs,
    }
    if write:
        p = report_path(config, date)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rep, indent=1, default=str),
                         encoding="utf-8")
            rep["report_file"] = str(p)
        except OSError as e:
            logger.warning("ecc-scale report not saved: %s", e)
    return rep


def format_report(reps: list[dict]) -> str:
    lines = []
    for rep in reps:
        lines.append(f"PS-94 ecc scale report {rep['date']} (dry run; "
                     f"{rep['n_measured']} of {rep['n_files']} RC16 lights "
                     f"in {rep['secs']}s)")
        lines.append(f"  stored records by grader: {rep['graded_by']} "
                     f"({rep['n_records_lin']} hold 1-b/a)")
        lines.append(f"  median delta binned - native: {rep['delta_median']}"
                     f"   stored (sqrt form) - native: "
                     f"{rep['stored_minus_native_median']}")
        for g, c in rep["pass"].items():
            fl = rep["flips"][g]
            lines.append(f"  gate {g}: pass native {c['native']}, binned "
                         f"{c['binned']} (binned-only {len(fl['pass_binned_only'])}"
                         f", native-only {len(fl['pass_native_only'])})")
        lines.append(f"  {'target':<24s} {'flt':<5s} {'n':>3s} {'ecc':>6s} "
                     f"{'bin':>6s} {'delta':>6s} {'stored':>6s}")
        for g in rep["groups"]:
            def f(v):
                return "-" if v is None else f"{v:.3f}"
            lines.append(f"  {str(g['target'])[:24]:<24s} {g['filter']:<5s} "
                         f"{g['n']:>3d} {f(g['ecc_median']):>6s} "
                         f"{f(g['ecc_bin_median']):>6s} "
                         f"{f(g['delta_median']):>6s} "
                         f"{f(g['stored_ecc_sqrt_median']):>6s}")
        if rep["errors"]:
            lines.append(f"  errors: {len(rep['errors'])} "
                         f"(first: {rep['errors'][0]})")
        lines.append(f"  -> {rep['verdict']}")
        if rep.get("report_file"):
            lines.append(f"  saved {rep['report_file']}")
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------- API: background + cache

_jobs: dict[str, dict] = {}


def blockers(config=None) -> list[str]:
    """Why the report must wait: an armed / running night (it is full-frame
    work on the RAM-tight scope PC) or a grading job."""
    why = []
    try:
        from photonscript.scheduler.app import get_armer
        st = str(get_armer().state or "").upper()
    except Exception:  # noqa: BLE001
        st = ""
    if st in ("ARMED", "RUNNING", "PAUSED_UNSAFE", "WATCHING"):   # PS-136
        why.append(f"armer is {st}: run the ecc-scale report in daytime")
    try:
        from photonscript.scheduler.runs import running_jobs
        jobs = running_jobs()
    except Exception:  # noqa: BLE001
        jobs = []
    if jobs:
        why.append("background job(s) running: " + "; ".join(jobs))
    return why


def request(config, date: str, refresh: bool = False,
            gates=DEFAULT_GATES) -> tuple[int, dict]:
    """(http status, body) for GET /api/qa/ecc-scale. A saved report is
    returned as is unless refresh; otherwise one background job per date."""
    import threading
    job = _jobs.get(date) or {}
    if job.get("running"):
        return 202, {"status": "running", "date": date,
                     "started": job.get("started")}
    p = report_path(config, date)
    if not refresh and p.exists():
        try:
            return 200, {"status": "done",
                         **json.loads(p.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            pass
    if job.get("error") and not refresh:
        return 500, {"status": "error", "date": date, "detail": job["error"]}
    why = blockers(config)
    if why:
        return 409, {"status": "refused", "detail": " ".join(why)}
    _jobs[date] = {"running": True,
                   "started": datetime.now().isoformat(timespec="seconds")}

    def _work():
        try:
            compare_night(config, date, gates=gates)
            _jobs[date] = {"running": False}
        except Exception as e:  # noqa: BLE001
            logger.warning("ecc-scale report %s failed: %s", date, e)
            _jobs[date] = {"running": False, "error": str(e)}

    threading.Thread(target=_work, daemon=True,
                     name=f"ecc-scale-{date}").start()
    return 202, {"status": "started", "date": date}
