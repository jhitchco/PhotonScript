"""PS-67: the per-night pointing pass (dawn backfill, CLI, API).

night_pass(config, date, solve) fills runs/<date>_pointing.jsonl (format in
shared.pointing) for every sub of a night and applies the "On target"
scorecard check to the attributed targets:

1. Position per sub: RC16 from its FITS header (header-only read); the
   Piggy-600 from the mount log at the exposure mid-time, else the RC16
   header position whose exposure covers it (rc16-correlated, within
   runs.PIGGYBACK_CORRELATE_TOL_MIN). A stored record's mount position is
   reused, so a second pass reads no headers.
2. Plate solves (solve=True, `pointing_solve_policy`): "sampled" (default,
   approved option B) = every `pointing_solve_every`-th sub per rig, every
   flagged / off-target sub and the first sub after each slew; "all" =
   every sub; "off" = none. Solves go through scheduler.solve_store (ASTAP
   with -o into a temp folder, never -update, stored under
   <data_dir>/solves/<night>/<rig>.jsonl), hinted with the mount position,
   capped by `pointing_solve_budget_min`. Already stored attempts (also the
   PS-96 flexure solves) are reused, never re-run. A solve wins over the
   mount position for the off-target test and gives the RC16 mount vs solve
   offset (the pointing-model error, summarized per night).
3. Verdicts: only the pointing row of each stored scorecard is swapped
   (qa_rules.regrade_pointing); human verdicts are never touched. A sub that
   becomes rejected leaves the Library (links move to Library/_rejected/,
   as the PS-21 rescore does).

PS-107: the check is source-aware. Without a plate solve (header, mount
log, rc16-correlated) only a gross miss above pointing_header_reject_deg
rejects; a sub rejected earlier by a header-only offset under that limit
returns to its other checks' verdict (un_rejected) and is filed again.
Those header "flag" subs lead the solve queue (pick_solves), so the solve
pass confirms or clears them. apply=False (CLI --dry-run) writes nothing
and reports what would change (written, verdicts_changed, newly_rejected,
un_rejected).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

RC16, PIGGY = "rc16", "piggyback"
SLEW_MOVE_ARCMIN = 5.0   # no mount log: a move this big between subs = a slew
_POS_KEYS = ("src", "mount_ra", "mount_dec", "alt", "az", "airmass", "pier")


def _header(path) -> dict | None:
    try:
        from astropy.io import fits as _fits
        return dict(_fits.getheader(str(path)))
    except Exception:  # noqa: BLE001 - not on this machine / unreadable
        return None


def _base_from_stored(prev: dict | None) -> dict | None:
    """A stored record's mount position (header / mount-log sources only)."""
    if not prev or prev.get("mount_ra") is None:
        return None
    src = prev.get("mount_src") or prev.get("src")
    if src in (None, "solve", "rc16-correlated"):
        return None      # re-derive: the RC16 neighbours may have changed
    b = {k: prev.get(k) for k in _POS_KEYS}
    b["src"] = src
    return b


def _frames(config, subs: list[dict]) -> dict[str, list[dict]]:
    """rig -> time-ordered [{rec, start, exp_s, mid}] for every sub that has
    a file and a start time."""
    from photonscript.scheduler.phd2_analysis import sub_start_utc
    out: dict = {}
    for r in subs:
        if not r.get("file"):
            continue
        st = sub_start_utc(r, config)
        if st is None:
            continue
        exp = float(r.get("exp_s") or 0)
        out.setdefault(r.get("rig") or RC16, []).append(
            {"rec": r, "start": st, "exp_s": exp,
             "mid": st + timedelta(seconds=exp / 2.0)})
    for v in out.values():
        v.sort(key=lambda f: f["start"])
    return out


def _correlated(rc_frames: list[dict], mid, tol_min: float) -> dict | None:
    """The RC16 position whose exposure covers `mid` (plus tol), or None."""
    best = None
    for f in rc_frames:
        b = f.get("base")
        if not b:
            continue
        end = f["start"] + timedelta(seconds=f["exp_s"])
        if f["start"] - timedelta(minutes=tol_min) <= mid <= end + timedelta(minutes=tol_min):
            d = abs((f["mid"] - mid).total_seconds())
            if best is None or d < best[0]:
                best = (d, b)
    if best is None:
        return None
    return {**best[1], "src": "rc16-correlated"}


def _first_after_slew(frames: list[dict], windows) -> set:
    """Files of the first sub after each slew: from mount-log slew windows
    when there are any, else from a big position change / target change."""
    from photonscript.shared.pointing import sep_arcmin
    out = set()
    if not frames:
        return out
    out.add(frames[0]["rec"]["file"])
    if windows:
        for _ws, we in windows:
            nxt = next((f for f in frames if f["start"] >= we), None)
            if nxt is not None:
                out.add(nxt["rec"]["file"])
        return out
    for a, b in zip(frames, frames[1:]):
        pa, pb = a.get("point") or {}, b.get("point") or {}
        moved = sep_arcmin(pa.get("mount_ra"), pa.get("mount_dec"),
                           pb.get("mount_ra"), pb.get("mount_dec"))
        if (moved != float("inf") and moved > SLEW_MOVE_ARCMIN) or \
                a["rec"].get("target") != b["rec"].get("target"):
            out.add(b["rec"]["file"])
    return out


def pick_solves(frames: list[dict], policy: str, every: int,
                windows=None, done: set | None = None) -> list[dict]:
    """The frames to solve, most useful first: flagged / off-target (PS-107:
    including the header-only "flag" subs, so a solve confirms or clears
    them), then the first after each slew, then every Nth. Skips files in
    `done`."""
    done = done or set()
    policy = (policy or "sampled").lower()
    if policy == "off" or not frames:
        return []
    if policy == "all":
        return [f for f in frames if f["rec"]["file"] not in done]
    every = max(1, int(every or 10))
    flagged = [f for f in frames if (f.get("point") or {}).get("flag")]
    slew = _first_after_slew(frames, windows)
    after = [f for f in frames if f["rec"]["file"] in slew]
    nth = [f for i, f in enumerate(frames) if i % every == 0]
    out, seen = [], set()
    for f in flagged + after + nth:
        k = f["rec"]["file"]
        if k in seen or k in done:
            continue
        seen.add(k)
        out.append(f)
    return out


def _move_out_of_library(config, rec: dict) -> list[str]:
    from photonscript.scheduler.qa_backfill import _library_links
    from photonscript.scheduler.runs import library_root
    lib = library_root(config)
    moves = []
    for src in _library_links(config, rec):
        dest = lib / "_rejected" / src.relative_to(lib)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                src.unlink()
            else:
                os.replace(src, dest)
            moves.append(f"{src} -> {dest}")
        except OSError as e:
            logger.warning("pointing: library move %s failed: %s", src, e)
    return moves


def night_pass(config, date: str, solve: bool = False, runner=None,
               apply: bool = True, budget_min: float | None = None) -> dict:
    """See the module doc. Returns counts plus the night summary."""
    from photonscript.scheduler import solve_store
    from photonscript.scheduler.runs import (PIGGYBACK_CORRELATE_TOL_MIN,
                                             _human_verdict, edit_subs,
                                             sync_goal_progress)
    from photonscript.shared import mount_log, pointing, qa_rules

    # PS-140: headers, mount log and plate solves run without the night
    # lock; on exit only the fields this pass changed are merged in under
    # it, and a verdict given meanwhile keeps every verdict field
    with edit_subs(config, date, hold_lock=False, write=apply) as subs:
        stored = pointing.load(config, date)
        lines = mount_log.load(config, date)
        windows = mount_log.slew_windows(lines)
        frames = _frames(config, subs)
        out = {"date": date, "subs": len(subs), "written": 0, "with_position": 0,
               "solve_attempts": 0, "solved": 0, "solve_s": 0.0,
               "records_updated": 0, "verdicts_changed": 0, "newly_rejected": 0,
               "un_rejected": 0, "library_moves": 0, "dry_run": not apply,
               "budget_hit": False, "mount_log_lines": len(lines)}
        sols = {rig: solve_store.lookup(config, date, rig) for rig in frames}

        def compute(rig, f, prev):
            r = f["rec"]
            s = sols.get(rig, {}).get(r["file"])
            return pointing.sub_pointing(
                config, rig, None, f["start"], f["exp_s"], r.get("target"),
                mount_lines=lines if not f.get("base") else None, prev=prev,
                solve=s if s and s.get("solved") else None, base=f.get("base"))

        # 1. mount positions (RC16 first: the Piggy-600 may borrow them)
        for rig in sorted(frames, key=lambda r: r != RC16):
            for f in frames[rig]:
                r = f["rec"]
                b = _base_from_stored(stored.get((rig, r["file"])))
                if b is None and rig == RC16 and r.get("abs_path") \
                        and Path(r["abs_path"]).exists():
                    b = pointing.from_header(_header(r["abs_path"]))
                if b is None and rig != RC16:
                    m = mount_log.position_at(lines, f["mid"])
                    if m is not None:
                        b = {"src": "mount-log", "mount_ra": m.get("ra"),
                             "mount_dec": m.get("dec"), "alt": m.get("alt"),
                             "az": m.get("az"), "airmass": None,
                             "pier": m.get("pier")}
                    else:
                        b = _correlated(frames.get(RC16, []), f["mid"],
                                        PIGGYBACK_CORRELATE_TOL_MIN)
                f["base"] = b

        def all_points():
            for rig, fr in frames.items():
                prev = None
                for f in fr:
                    f["point"] = compute(rig, f, prev)
                    prev = f["point"]

        all_points()

        # 2. sampled plate solves
        policy = str(getattr(config, "pointing_solve_policy", "sampled") or "sampled")
        if solve and policy.lower() != "off":
            budget = float(budget_min if budget_min is not None else
                           getattr(config, "pointing_solve_budget_min", 30.0) or 0) * 60.0
            every = int(getattr(config, "pointing_solve_every", 10) or 10)
            t0 = time.monotonic()
            for rig, fr in frames.items():
                done = set(sols.get(rig, {}))
                for f in pick_solves(fr, policy, every, windows, done):
                    if time.monotonic() - t0 > budget:
                        out["budget_hit"] = True
                        break
                    r = f["rec"]
                    p = f.get("point") or {}
                    hint = ((p["mount_ra"], p["mount_dec"])
                            if p.get("mount_ra") is not None else None)
                    res = solve_store.solve(
                        config, r.get("abs_path") or "", rig=rig, night=date,
                        hint=hint, rel_file=r["file"],
                        start_utc=f["start"].isoformat() + "Z", runner=runner,
                        radius_deg=5.0 if hint else 30.0)
                    out["solve_attempts"] += 1
                    sols.setdefault(rig, {})[r["file"]] = res or {"solved": False}
                    if res:
                        out["solved"] += 1
            out["solve_s"] = round(time.monotonic() - t0, 1)
            if out["solve_attempts"]:
                all_points()

        # 3. write changed records (a dry run only counts them)
        for rig, fr in frames.items():
            for f in fr:
                p = f["point"]
                line = {**p, "file": f["rec"]["file"]}
                out["with_position"] += int(pointing.judged_position(p) is not None)
                if p.get("src") is None and not p.get("target"):
                    continue
                if pointing.same_record(stored.get((rig, line["file"])), line):
                    continue
                if apply:
                    pointing.append_record(config, date, line)
                out["written"] += 1

        # 4. the "On target" check on the stored scorecards (PS-107: by source;
        # a dry run reports what would change and writes nothing)
        changed = 0
        for rig, fr in frames.items():
            for f in fr:
                r, p = f["rec"], f["point"]
                if _human_verdict(r):
                    continue
                t = qa_rules.thresholds(config, rig, r.get("target"),
                                        r.get("filter"))
                fields = qa_rules.regrade_pointing(
                    r, p.get("off_target_arcmin"), t, p.get("note"),
                    pointing.judged_src(p))
                if fields is None:
                    continue
                was = bool(r.get("passed_qa"))
                was_verdict = r.get("auto_verdict")
                changed += 1
                out["verdicts_changed"] += int(
                    was_verdict != fields.get("auto_verdict"))
                if was and not fields.get("passed_qa"):
                    out["newly_rejected"] += 1
                elif not was and fields.get("passed_qa"):
                    out["un_rejected"] += 1
                if not apply:
                    continue
                if not fields.get("reviewed") and r.get("review_source") == "auto":
                    fields.update(reviewed=False, review_source=None)
                r.update(fields)
                if r.get("review_source") is None:
                    r.pop("review_source", None)
                if was and not r.get("passed_qa"):
                    out["library_moves"] += len(_move_out_of_library(config, r))
    if apply and changed:
        if out["verdicts_changed"]:
            sync_goal_progress(config)
        if out["un_rejected"]:
            try:  # PS-107: subs back from a header-only reject are filed
                from photonscript.scheduler.runs import build_library
                build_library(config, date)
            except Exception as e:  # noqa: BLE001
                logger.warning("pointing %s: library update failed: %s", date, e)
    out["records_updated"] = changed
    out["summary"] = pointing.summarize(
        [f["point"] for fr in frames.values() for f in fr])
    return out


def bench(config, date: str, n: int = 10, runner=None) -> dict:
    """Seconds per ASTAP solve and success rate on up to n subs per rig of
    one night (stored like any other solve, so they are reused)."""
    from photonscript.scheduler import solve_store
    from photonscript.scheduler.runs import _load_subs
    from photonscript.shared import pointing
    subs = _load_subs(config, date)
    stored = pointing.load(config, date)
    out = {}
    for rig in (RC16, PIGGY):
        rs = [s for s in subs if (s.get("rig") or RC16) == rig and s.get("abs_path")]
        step = max(1, len(rs) // max(1, n))
        times, ok = [], 0
        for s in rs[::step][:n]:
            p = stored.get((rig, s.get("file"))) or {}
            hint = ((p["mount_ra"], p["mount_dec"])
                    if p.get("mount_ra") is not None else None)
            t0 = time.monotonic()
            res = solve_store.solve(config, s["abs_path"], rig=rig, night=date,
                                    hint=hint, rel_file=s.get("file"),
                                    runner=runner,
                                    radius_deg=5.0 if hint else 30.0)
            times.append(time.monotonic() - t0)
            ok += int(res is not None)
        if times:
            times.sort()
            out[rig] = {"tried": len(times), "solved": ok,
                        "median_s": round(times[len(times) // 2], 2),
                        "max_s": round(times[-1], 2)}
    return {"date": date, "rigs": out}
