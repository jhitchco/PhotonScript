"""PS-130: re-measure pre-PS-83 backfill records from their FITS.

PS-83 made the live and the backfill grader measure with one function
(shared.star_measure.measure_frame). Backfill records written before it (a
`graded_by` and no `measure_v`) still hold the old binned HFR (about 2x the
live one: 13 to 15 px vs 6.5 to 7.2 px on the 09-26 Crescent Ha subs), an
uncapped star count and HFR x scale as FWHM. `qa-rescore` only re-judges
stored numbers, and a full re-grade of the night (runs.regrade_night)
measures every sub again (PS-141: manual verdicts kept).

This pass re-measures only those records, in place:

* each record lacking measure_v is measured again from its FITS with
  measure_frame (native frame; the 2x2-binned frame on MemoryError, as the
  backfill grader does) and its star / exposure metrics, `measure_v`,
  `ecc_def`, `ecc_at` and `graded_by` are replaced. The old numbers are kept
  under `pre_ps83` and the record says `remeasured`;
* a record whose FITS is gone is skipped and counted (`missing_fits`);
* the night is then re-judged by runs.rescore_night (PS-21 scorecard,
  PS-108 score, same night medians, kept-reject rule, --allow-unreject);
* human verdicts are never changed: the fields a person sets (MANUAL_FIELDS,
  from runs.set_manual_qa and runs.approve_night) are not written. The
  scorecard rows and the PS-108 score of every re-measured record are
  refreshed, since they describe the measurement (the lightbox side panel
  shows the row values).

DRY RUN BY DEFAULT: measures, then reports per night what would change
(counts, auto verdict changes, score moves) and writes nothing. CLI
`photonscript qa-rescore --remeasure --date D | --all-before-ps83 [--apply]`,
API GET / POST /api/runs/{date}/remeasure (background job).
"""

from __future__ import annotations

import copy
import gc
import logging
import re
from datetime import datetime
from pathlib import Path
from statistics import median

logger = logging.getLogger(__name__)

# Set by a person (runs.set_manual_qa: accepted / rejected / review, and
# runs.approve_night: reviewed). This pass never writes them; on an auto
# record rescore_night may (that is the auto verdict).
MANUAL_FIELDS = ("passed_qa", "reason", "reviewed", "review_source",
                 "manual_qa", "manual_reason", "reviewed_at",
                 # what the automatic grade said when the person decided
                 "auto_verdict", "auto_reason", "drivers")

# measure_frame key -> record key (the backfill record names, runs._fast_grade)
MEASURED = (("hfr", "hfr"), ("fwhm_arcsec", "fwhm_arcsec"),
            ("stars", "stars"), ("ecc", "ecc"), ("ecc_bin", "ecc_bin"),
            ("hfr_bin", "hfr_bin"), ("measure_at", "ecc_at"),
            ("ecc_def", "ecc_def"), ("measure_v", "measure_v"),
            ("background", "background"), ("noise", "noise"),
            ("corner_spread", "corner_spread"),
            ("clipped_pct", "clipped_pct"),
            ("sat_stars_pct", "sat_stars_pct"), ("swamp", "swamp"),
            ("exposure", "exposure"), ("graded_by", "graded_by"),
            # PS-117 (b): the sky rate fields
            ("sky_adu", "sky_adu"), ("sky_e_s", "sky_e_s"),
            ("sky_e_s_ch", "sky_e_s_ch"),
            ("rn_penalty_pct", "rn_penalty_pct"),
            # PS-146: what the judged ecc / FWHM came from
            ("ecc_all", "ecc_all"), ("ecc_bright", "ecc_bright"),
            ("ecc_bright_n", "ecc_bright_n"), ("ecc_src", "ecc_src"),
            ("ecc_why", "ecc_why"), ("ecc_bin_all", "ecc_bin_all"),
            ("fwhm_moment_arcsec", "fwhm_moment_arcsec"),
            ("fwhm_src", "fwhm_src"), ("fwhm_unreliable", "fwhm_unreliable"))

# the old numbers kept on the record for the audit trail
KEPT_OLD = ("hfr", "fwhm_arcsec", "stars", "ecc", "ecc_bin", "ecc_def",
            "background", "graded_by")


def needs_remeasure(rec: dict) -> bool:
    """A backfill record from before PS-83 (live records never carried
    graded_by and always used the live measure)."""
    return bool(rec.get("graded_by")) and not rec.get("measure_v")


def fits_path(config, date: str, rec: dict) -> Path | None:
    """The sub's FITS: abs_path, else <image_watch_dir>/<date>/<file>."""
    for p in (rec.get("abs_path"),
              str(Path(config.image_watch_dir) / date / str(rec.get("file")))
              if rec.get("file") else None):
        if p and Path(p).is_file():
            return Path(p)
    return None


def measure_sub(config, path: Path, rig: str = "rc16") -> tuple[dict, dict | None]:
    """(record fields, PS-80 star table) for one FITS, measured as the
    backfill grader measures it now (runs._fast_grade): measure_frame on the
    native frame, the 2x2-binned frame on MemoryError. One full frame at a
    time (runs._HEAVY)."""
    from astropy.io import fits
    from photonscript.scheduler import runs
    from photonscript.shared import star_measure
    from photonscript.shared.rigs import rig_config
    rcfg = rig_config(config, rig)
    with runs._HEAVY:
        hdr = fits.getheader(path)
        osc = star_measure.is_osc(rig, hdr)
        m = runs._measure_native(path, rcfg, rig, osc, header=hdr)
        if m is None:
            _, binned = runs._load_binned(path)
            m = star_measure.measure_frame(binned, rcfg, rig, osc=osc,
                                           binned_input=True,
                                           grader=runs.BACKFILL_GRADER,
                                           header=hdr)
            del binned
    gc.collect()
    m.pop("_stars", None)
    return {rk: m.get(mk) for mk, rk in MEASURED}, m.get("star_table")


def _key(rec: dict) -> tuple:
    return (rec.get("rig") or "rc16", str(rec.get("file") or ""))


def _med(xs):
    xs = [float(x) for x in xs if isinstance(x, (int, float))]
    return round(median(xs), 2) if xs else None


def _merge(rec: dict, fields: dict) -> None:
    rec["pre_ps83"] = {k: rec.get(k) for k in KEPT_OLD}
    rec.update(fields)
    rec["remeasured"] = fields.get("measure_v")


def remeasure_night(config, date: str, apply: bool = False,
                    allow_unreject: bool = False, measure=None) -> dict:
    """Re-measure one night's pre-PS-83 backfill records and re-judge the
    night. Dry run unless apply. `measure(config, path, rig)` replaces
    measure_sub (tests)."""
    from photonscript.scheduler import runs
    measure = measure or measure_sub
    subs = runs._load_subs(config, date)
    todo = [r for r in subs if needs_remeasure(r)]
    out = {"date": date, "mode": "apply" if apply else "dry-run",
           "subs": len(subs), "pre_ps83": len(todo), "remeasured": 0,
           "missing_fits": 0, "failed": 0, "missing": [], "errors": [],
           "human_kept": 0, "verdict_changes": [], "transitions": {},
           "counts": {}, "records_changed": 0}
    if not todo:
        return out
    measured: dict[tuple, tuple[dict, dict | None]] = {}
    old_hfr, new_hfr, old_fwhm, new_fwhm = [], [], [], []
    for rec in todo:
        rig = rec.get("rig") or "rc16"
        p = fits_path(config, date, rec)
        if p is None:
            out["missing_fits"] += 1
            out["missing"].append(rec.get("file"))
            continue
        try:
            fields, table = measure(config, p, rig)
        except Exception as e:  # noqa: BLE001 - one bad file never stops the night
            out["failed"] += 1
            out["errors"].append({"file": rec.get("file"), "error": str(e)})
            logger.warning("remeasure %s %s failed: %s", date,
                           rec.get("file"), e)
            continue
        measured[_key(rec)] = (fields, table)
        old_hfr.append(rec.get("hfr"))
        new_hfr.append(fields.get("hfr"))
        old_fwhm.append(rec.get("fwhm_arcsec"))
        new_fwhm.append(fields.get("fwhm_arcsec"))
    out["remeasured"] = len(measured)
    out["hfr_median"] = {"old": _med(old_hfr), "new": _med(new_hfr)}
    out["fwhm_arcsec_median"] = {"old": _med(old_fwhm), "new": _med(new_fwhm)}
    if out["missing_fits"] and measured:
        # stale binned HFR left next to fresh native ones skews the night
        # median the hfr_rel check compares against
        out["warning"] = (f"{out['missing_fits']} pre-PS-83 record(s) keep "
                          "the old binned HFR (FITS missing): the night HFR "
                          "median mixes both measures")
    if not measured:
        return out

    if not apply:
        merged = copy.deepcopy(subs)
        for rec in merged:
            hit = measured.get(_key(rec))
            if hit and needs_remeasure(rec):
                _merge(rec, hit[0])
        res = runs.rescore_night(config, date, apply=False,
                                 allow_unreject=allow_unreject,
                                 records=merged, sidecars=True)
    else:
        if _night_busy(date):
            raise RuntimeError(f"a grading job is running for {date}")
        # re-read: the log may have grown while the FITS were measured.
        # PS-140: the measuring ran unlocked; this re-load, the merge of the
        # measured fields (never MANUAL_FIELDS) and the rewrite hold the
        # night lock, so a verdict given during the run survives
        written = 0
        tables = []
        with runs.edit_subs(config, date) as fresh:
            for rec in fresh:
                hit = measured.get(_key(rec))
                if hit and needs_remeasure(rec):
                    _merge(rec, hit[0])
                    written += 1
                    if hit[1]:
                        tables.append((rec["file"], hit[1],
                                       rec.get("rig") or "rc16"))
            _refresh_cards(config, date, fresh, set(measured))
        for name, table, rig in tables:
            try:
                from photonscript.shared import star_table
                star_table.write(config, date, name, table, rig=rig)
            except Exception as e:  # noqa: BLE001
                logger.debug("star sidecar skipped: %s", e)
        res = runs.rescore_night(config, date, apply=True,
                                 allow_unreject=allow_unreject)
        out["records_written"] = written     # new metrics + scorecard
        out["records_changed"] = res.get("records_changed", 0)  # rescore
    out["counts"] = res.get("counts", {})
    out["transitions"] = res.get("transitions", {})
    out["human_kept"] = int(out["counts"].get("human_kept", 0))
    out["score_mode"] = res.get("score_mode")
    out["rules_version"] = res.get("rules_version")
    # auto verdicts only: a human verdict is reported as kept, never changed
    out["verdict_changes"] = [d for d in res.get("diffs", [])
                              if not str(d.get("action") or "")
                              .startswith("kept (human")]
    out["library_moves"] = res.get("library_moves", [])
    logger.info("PS-130 remeasure %s (%s): %d of %d pre-PS-83 measured, "
                "%d FITS missing, %d failed, %d auto verdict change(s)",
                date, out["mode"], out["remeasured"], out["pre_ps83"],
                out["missing_fits"], out["failed"],
                len(out["verdict_changes"]))
    return out


def _refresh_cards(config, date: str, subs: list[dict], keys: set) -> None:
    """Re-measured records get the scorecard (rows + PS-108 score) of their
    new numbers. Verdict fields are left to rescore_night, which keeps every
    human verdict; on a human record the stored auto_verdict / auto_reason /
    drivers stay what the grader said when the person decided."""
    from photonscript.scheduler import runs
    graded, _src = runs._night_cards(config, date, subs)
    for rec, card in graded:
        if _key(rec) not in keys or not rec.get("remeasured"):
            continue
        rec["scorecard"] = card.compact()
        rec.update(card.score_fields())


def _night_busy(date: str) -> bool:
    from photonscript.scheduler import runs
    return bool(runs._backfill_state.get(date, {}).get("running")) \
        or bool(runs._regrade_all.get("running"))


def nights_before_ps83(config) -> list[str]:
    """Every night whose subs log still holds a pre-PS-83 backfill record."""
    from photonscript.scheduler import runs
    out = []
    for p in sorted(runs.runs_dir(config).glob("*_subs.jsonl")):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_subs\.jsonl$", p.name)
        if m and any(needs_remeasure(r)
                     for r in runs._load_subs(config, m.group(1))):
            out.append(m.group(1))
    return out


def remeasure(config, dates: list[str] | None = None, apply: bool = False,
              allow_unreject: bool = False, measure=None) -> dict:
    """remeasure_night over `dates` (None: nights_before_ps83) plus totals."""
    if dates is None:
        dates = nights_before_ps83(config)
    nights = []
    for d in dates:
        try:
            nights.append(remeasure_night(config, d, apply=apply,
                                          allow_unreject=allow_unreject,
                                          measure=measure))
        except Exception as e:  # noqa: BLE001
            logger.warning("remeasure %s failed: %s", d, e)
            nights.append({"date": d, "error": str(e)})
    totals: dict = {}
    for n in nights:
        for k in ("subs", "pre_ps83", "remeasured", "missing_fits", "failed",
                  "human_kept", "records_changed"):
            totals[k] = totals.get(k, 0) + int(n.get(k) or 0)
        totals["verdict_changes"] = totals.get("verdict_changes", 0) + len(
            n.get("verdict_changes") or [])
    return {"mode": "apply" if apply else "dry-run", "nights": nights,
            "totals": totals}


def format_report(rep: dict, full: bool = False) -> str:
    """Plain-text summary per night (CLI)."""
    t = rep.get("totals") or {}
    lines = [f"PS-130 remeasure ({rep['mode']}): {len(rep['nights'])} "
             f"night(s), {t.get('remeasured', 0)} of {t.get('pre_ps83', 0)} "
             f"pre-PS-83 subs measured, {t.get('missing_fits', 0)} FITS "
             f"missing, {t.get('failed', 0)} failed, "
             f"{t.get('verdict_changes', 0)} auto verdict change(s), "
             f"{t.get('human_kept', 0)} human verdict(s) kept"]
    for n in rep["nights"]:
        if n.get("error"):
            lines.append(f"  {n['date']}: ERROR {n['error']}")
            continue
        c = n.get("counts") or {}
        h, f = n.get("hfr_median") or {}, n.get("fwhm_arcsec_median") or {}
        lines.append(
            f"  {n['date']}: {n['subs']} subs, {n['pre_ps83']} pre-PS-83, "
            f"{n['remeasured']} measured, {n['missing_fits']} FITS missing, "
            f"{n['failed']} failed; HFR median {h.get('old')} -> "
            f"{h.get('new')} px, FWHM {f.get('old')} -> {f.get('new')}\"; "
            f"{len(n.get('verdict_changes') or [])} auto verdict change(s), "
            f"{n.get('human_kept', 0)} human kept, score changed "
            f"{c.get('score_changed', 0)} (up {c.get('score_up', 0)}, down "
            f"{c.get('score_down', 0)})")
        if n.get("warning"):
            lines.append(f"    warning: {n['warning']}")
        rows = n.get("verdict_changes") or []
        for d in (rows if full else rows[:20]):
            lines.append(f"    {d.get('old')} -> {d.get('new')} "
                         f"({d.get('action')}) {d.get('rig')} "
                         f"{d.get('filter') or ''} {d.get('file')}  "
                         f"{d.get('new_reason') or ''}".rstrip())
        if not full and len(rows) > 20:
            lines.append(f"    ... {len(rows) - 20} more (--full)")
    return "\n".join(lines)


# ------------------------------------------------ API: background job

_jobs: dict[str, dict] = {}


def running() -> list[str]:
    """For runs.running_jobs (PS-58: an update waits for a writing job)."""
    return [f"re-measuring {d}" for d, j in list(_jobs.items())
            if j.get("running") and j.get("apply")]


def status(date: str) -> dict:
    j = _jobs.get(date)
    if not j:
        return {"status": "idle", "date": date}
    return {"date": date, **j}


def request(config, date: str, apply: bool = False,
            allow_unreject: bool = False) -> tuple[int, dict]:
    """(http status, body) for POST /api/runs/{date}/remeasure: one
    background job per night (full-frame work, minutes per night). Refused
    (409) while a job for the night runs, the armer is armed / running or a
    grading job is active."""
    import threading
    from photonscript.scheduler.ecc_scale import blockers
    if (_jobs.get(date) or {}).get("running"):
        return 409, {"status": "running", **status(date)}
    why = blockers(config)
    if why:
        return 409, {"status": "refused", "detail": " ".join(
            w.replace("run the ecc-scale report", "re-measure") for w in why)}
    _jobs[date] = {"status": "running", "running": True, "apply": apply,
                   "started": datetime.now().isoformat(timespec="seconds")}

    def _work():
        try:
            res = remeasure_night(config, date, apply=apply,
                                  allow_unreject=allow_unreject)
            _jobs[date] = {**_jobs[date], "status": "done", "running": False,
                           "result": res}
        except Exception as e:  # noqa: BLE001
            logger.warning("remeasure %s failed: %s", date, e)
            _jobs[date] = {**_jobs[date], "status": "error",
                           "running": False, "detail": str(e)}

    threading.Thread(target=_work, daemon=True,
                     name=f"remeasure-{date}").start()
    return 202, {"status": "started", "date": date, "apply": apply}
