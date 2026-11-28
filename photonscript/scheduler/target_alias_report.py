"""Target alias hygiene report and backfill (2026-10-14).

The Heart Nebula target page read "also recorded as: ? . Heart Nebula imaging
(repeats while safe and up)_Container . OSC_LIGHTS_UNTIL_DAWN_Container .
OSC_LIGHT_LOOP_Container" and M 31 "? . M 31". Every one of those subs was
already credited to the right goal (sub_index canonicalizes `target` on
read, PS-78); the list showed each sub's provenance `target_raw` as an alias:

* '?': Piggy-600 subs written unattributed (the companion's OSC light loop
  sits in no DeepSkyObjectContainer, so NINA #2 has no target name for the
  OBJECT keyword or the $$TARGETNAME$$ file token) and named later by the
  RC16 timeline (PS-157), Piggy attribution (PS-137) or the PS-51
  correlation, which keep target_raw '?' as "was unattributed".
* "<target> imaging (repeats while safe and up)_Container",
  "OSC_LIGHT_LOOP_Container", "OSC_LIGHTS_UNTIL_DAWN_Container": subs graded
  before PS-77/PS-78, named after NINA's running container by the telescope
  agent; the PS-78 rename kept that name in target_raw.

The read side now hides those (shared.target_names.recorded_alias), so no
alias data has to be rewritten. This module reports, per target, what the
"also recorded as" list loses, and finds any sub whose stored `target` is
still a placeholder (a container name) or still unattributed where the
existing PS-51 rule (the RC16 target being imaged at that time) names it.

Dry run unless apply=True. --apply rewrites only the local subs logs
(runs.edit_subs, the night lock held, target_raw keeps the old name). It
never moves, links or deletes a Library file and never writes a FITS: the
next build_library (Approve night / dawn filing) refiles a re-attributed
accepted sub from Library/_ as it always does. Goal progress follows on the
next recount (POST /api/projects2/recount).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from photonscript.shared.target_names import (
    UNATTRIBUTED,
    canonical_target,
    is_placeholder_name,
)


def _rig(s: dict) -> str:
    return s.get("rig") or "rc16"


def plan_night(subs: list[dict], known: Any = None) -> dict:
    """Decide (and make, in place) one night's changes. Pure: no I/O.

    1. a stored `target` that is a placeholder other than '?' (a container
       or loop name) becomes its canonical target, or '?' for a loop;
    2. unattributed Piggy-600 subs go through the PS-51 correlation (the
       RC16 target imaged at that time; parked / slewing stay '?');
    3. counts, per target, the placeholder target_raw values the "also
       recorded as" list no longer shows (read side, nothing written).
    Returns {"renamed": Counter{(rig, from, to)}, "correlated": {target: n},
    "hidden_aliases": Counter{(target, raw)}, "unattributed": Counter{rig},
    "in_library": n re-attributed subs already accepted}."""
    from photonscript.scheduler.runs import correlate_piggyback_records
    renamed: Counter = Counter()
    in_library = 0
    for s in subs:
        t = s.get("target")
        if t in (None, "", UNATTRIBUTED) or not is_placeholder_name(t):
            continue
        new = canonical_target(t, known) or UNATTRIBUTED
        if new == t:
            continue
        if not s.get("target_raw"):
            s["target_raw"] = t
        s["target"] = new
        renamed[(_rig(s), str(t), new)] += 1
        if new != UNATTRIBUTED and s.get("passed_qa"):
            in_library += 1
    before = {id(s): s.get("target") for s in subs}
    _n, windows, _pending = correlate_piggyback_records(subs)
    for s in subs:
        if before[id(s)] != s.get("target"):
            s["target_src"] = "rc16-correlate"
            if s.get("passed_qa"):
                in_library += 1
    hidden: Counter = Counter()
    unattributed: Counter = Counter()
    for s in subs:
        canon = canonical_target(s.get("target"), known) or UNATTRIBUTED
        if canon == UNATTRIBUTED:
            unattributed[_rig(s)] += 1
            continue
        raw = s.get("target_raw")
        if raw and is_placeholder_name(raw):
            hidden[(canon, str(raw))] += 1
    return {"renamed": renamed, "correlated": dict(windows),
            "hidden_aliases": hidden, "unattributed": unattributed,
            "in_library": in_library}


def summarize(nights: Iterable[tuple[str, dict]]) -> dict:
    """Fold plan_night results into the report (JSON-safe)."""
    out_nights = []
    per_target: Counter = Counter()
    hidden_per_target: dict = {}
    unattr: Counter = Counter()
    in_lib = 0
    for date, r in nights:
        for (_rig, _frm, to), n in r["renamed"].items():
            if to != UNATTRIBUTED:
                per_target[to] += n
        for name, n in r["correlated"].items():
            per_target[name] += n
        for (tgt, raw), n in r["hidden_aliases"].items():
            d = hidden_per_target.setdefault(tgt, {})
            d[raw] = d.get(raw, 0) + n
        unattr.update(r["unattributed"])
        in_lib += r["in_library"]
        if r["renamed"] or r["correlated"]:
            out_nights.append({
                "date": date,
                "renamed": [{"rig": rg, "from": f, "to": t, "subs": n}
                            for (rg, f, t), n in sorted(r["renamed"].items())],
                "correlated": r["correlated"]})
    return {
        "nights_changed": out_nights,
        "reattributed_by_target": dict(sorted(per_target.items())),
        "reattributed": sum(per_target.values()),
        "hidden_aliases_by_target": {
            t: dict(sorted(v.items())) for t, v in sorted(hidden_per_target.items())},
        "hidden_alias_subs": sum(sum(v.values())
                                 for v in hidden_per_target.values()),
        "still_unattributed": dict(sorted(unattr.items())),
        "accepted_to_refile": in_lib,
    }


def run(config, dates: list[str] | None = None, apply: bool = False) -> dict:
    """Scan every night (or `dates`); write the subs logs only when apply."""
    from photonscript.scheduler.runs import edit_subs, runs_dir
    from photonscript.scheduler.target_backfill import _known_targets
    all_dates = sorted({p.name[:10] for p in
                        runs_dir(config).glob("*_subs.jsonl")})
    if dates:
        all_dates = [d for d in all_dates if d in set(dates)]
    known = _known_targets(config, all_dates)
    results = []
    for d in all_dates:
        # PS-140: metadata pass, the night lock held from load to rewrite
        with edit_subs(config, d, write=apply) as subs:
            results.append((d, plan_night(subs, known)))
    out = summarize(results)
    out.update({"applied": bool(apply), "nights_scanned": len(all_dates),
                "library_touched": False})
    return out
