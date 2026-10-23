"""Rewrite the stale guide_rms of subs graded before the window-RMS fix.

Until 2026-10-13 (commit "per-sub guide RMS only from frames inside the
exposure") a sub's guide_rms was the PHD2 client's rolling RMS at grading
time: on 2026-10-12 every RC16 sub read a constant 34.84" while PHD2 was
stopped or had lost the star. Newer records carry guide_rms_frames (the
guided frames inside the exposure the RMS came from); older ones do not.

For every record without guide_rms_frames this pass recomputes the RMS of
the sub's own exposure from the night's PHD2 guide log (phd2_analysis
GuideTimeline.stats, the same during-the-sub figure PS-88 reports), with
the agent's rule: under WINDOW_RMS_MIN_FRAMES guided frames (or no guide
pixel scale) the RMS is None. A sub PHD2 did not guide gets guide_rms None
and guide_rms_frames 0. Without a guide log for the night only records whose
own guide_state says PHD2 was not guiding are nulled; the rest are left.

The guide_rms row of the stored scorecard (and the PS-108 score) follows.
Human verdicts are never changed (runs._human_verdict: the MANUAL_FIELDS are
not written); an automatic verdict can only move when qa_guide_rms_mode is
"warn" or "fail" (default "info": recorded, not judged).

DRY RUN BY DEFAULT. CLI: photonscript guide-rms-backfill --date D [--apply]
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

GUIDING_STATES = ("guiding", "settling")
EXAMPLES = 8


def window_from_log(timeline, start_utc, exp_s, min_frames: int) -> tuple:
    """(rms_arcsec | None, guided frames) of one exposure from the log."""
    g = timeline.stats(start_utc, float(exp_s))
    frames = int(g.get("frames") or 0)
    rms = g.get("rms_total_arcsec")
    if frames < min_frames or rms is None:
        return None, frames
    return round(float(rms), 3), frames


def night_pass(config, date: str, apply: bool = False, timeline="load") -> dict:
    """Recompute guide_rms for one night's pre-fix records. Returns counts
    and a few examples. `timeline`: a GuideTimeline, None (no guide log), or
    "load" to read the night's PHD2 guide logs."""
    from photonscript.scheduler import phd2_analysis as pa
    from photonscript.scheduler.qa_remeasure import MANUAL_FIELDS
    from photonscript.scheduler.runs import (_human_verdict, edit_subs,
                                             sync_goal_progress)
    from photonscript.shared import qa_rules
    from photonscript.telescope_agent.phd2_client import WINDOW_RMS_MIN_FRAMES

    if timeline == "load":
        try:
            timeline = pa.night_timeline(config, date)   # slow: outside the lock
        except Exception as e:  # noqa: BLE001
            logger.warning("guide-rms backfill %s: guide log not read: %s", date, e)
            timeline = None
    out = {"date": date, "apply": bool(apply), "guide_log": timeline is not None,
           "subs": 0, "pre_fix": 0, "stale": 0, "from_log": 0, "nulled": 0,
           "unchanged": 0, "no_log_left": 0, "no_start": 0, "records_updated": 0,
           "scorecards": 0, "human_kept": 0, "verdicts_changed": 0,
           "newly_rejected": 0, "newly_passed": 0, "library_moves": 0,
           "examples": []}
    moved = []
    # PS-140: a metadata pass, under the night lock from load to rewrite
    with edit_subs(config, date, write=apply) as subs:
        for r in subs:
            out["subs"] += 1
            if "guide_rms_frames" in r:
                continue                      # graded with the window RMS
            out["pre_fix"] += 1
            old = r.get("guide_rms")
            out["stale"] += int(old is not None)
            gstate = str(r.get("guide_state") or "").lower()
            if timeline is not None:
                st = pa.sub_start_utc(r, config)
                if st is None or not r.get("exp_s"):
                    out["no_start"] += 1
                    continue
                rms, frames = window_from_log(timeline, st, r["exp_s"],
                                              WINDOW_RMS_MIN_FRAMES)
                src = "phd2_log" if frames else "not_guiding"
            elif gstate not in GUIDING_STATES:
                rms, frames, src = None, 0, "not_guiding"
            else:
                out["no_log_left"] += int(old is not None)
                continue
            if old is None and rms is None:
                out["unchanged"] += 1
                continue
            if rms is None:
                out["nulled"] += 1
            else:
                out["from_log"] += 1
            if len(out["examples"]) < EXAMPLES:
                out["examples"].append({"file": r.get("file"), "rig": r.get("rig"),
                                        "old": old, "new": rms, "frames": frames,
                                        "src": src})
            fields = {"guide_rms": rms, "guide_rms_frames": frames,
                      "guide_rms_src": src}
            t = qa_rules.thresholds(config, r.get("rig") or "rc16",
                                    r.get("target"), r.get("filter"))
            card = qa_rules.regrade_guide_rms(r, t, rms)
            human = _human_verdict(r)
            if card is not None:
                out["scorecards"] += 1
                if human:
                    held = {k: card.pop(k) for k in list(card) if k in MANUAL_FIELDS}
                    out["human_kept"] += int(any(r.get(k) != v for k, v in held.items()))
                    card.pop("reviewed", None)
                    card.pop("review_source", None)
                elif not card.get("reviewed") and r.get("review_source") == "auto":
                    card.update(reviewed=False, review_source=None)
                fields.update(card)
            was, was_verdict = bool(r.get("passed_qa")), r.get("auto_verdict")
            if apply:
                r.update(fields)
                if r.get("review_source") is None:
                    r.pop("review_source", None)
            now_passed = fields.get("passed_qa", r.get("passed_qa"))
            if not human and "auto_verdict" in fields and fields["auto_verdict"] != was_verdict:
                out["verdicts_changed"] += 1
                if was and not now_passed:
                    out["newly_rejected"] += 1
                    moved.append(dict(r, **fields))
                elif not was and now_passed:
                    out["newly_passed"] += 1
            out["records_updated"] += 1
    if apply:
        if moved:
            from photonscript.scheduler.pointing_record import _move_out_of_library
            for r in moved:
                out["library_moves"] += len(_move_out_of_library(config, r))
        if out["verdicts_changed"]:
            sync_goal_progress(config)
        if out["newly_passed"]:
            try:  # subs that now pass are filed, as the pointing pass does
                from photonscript.scheduler.runs import build_library
                build_library(config, date)
            except Exception as e:  # noqa: BLE001
                logger.warning("guide-rms backfill %s: library update failed: %s",
                               date, e)
    return out


def format_line(r: dict) -> str:
    """One console line per night."""
    if not r["pre_fix"]:
        return f"{r['date']}: {r['subs']} subs, all graded with the window RMS"
    s = (f"{r['date']}: {r['pre_fix']} pre-fix subs ({r['stale']} with an RMS), "
         f"guide log {'yes' if r['guide_log'] else 'NO'}: {r['from_log']} from the "
         f"log, {r['nulled']} set to none (PHD2 not guiding)")
    if r["no_log_left"]:
        s += f", {r['no_log_left']} left (guiding per the record, no log)"
    if r["no_start"]:
        s += f", {r['no_start']} without a start time"
    if r["verdicts_changed"] or r["human_kept"]:
        s += (f"; {r['verdicts_changed']} auto verdicts change "
              f"({r['newly_rejected']} rejected, {r['newly_passed']} passed), "
              f"{r['human_kept']} human verdicts kept")
    return s + ("" if r["apply"] else " [dry run]")
