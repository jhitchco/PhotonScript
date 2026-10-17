"""Unguided fallback when PHD2 fails on a guided night (PS-156).

2026-10-06: PHD2 reported Guiding all night while every pulse failed in the
mount driver; the night was saved only because the Paramount MX tracks well
unguided (TPoint + ProTrack). This turns that luck into a decision: when a
guided night's PHD2 stops correcting (guard D7 / D8, PS-155) or stays
unlocked for guide_fallback_after_min, the rest of the night runs unguided
with every RC16 sub capped at the length the tracking test proved for its
filter (guide_blocks.fallback_exposure_s: the PS-84 report, else
guide_fallback_exposure_s, never over unguided_max_exposure_s).

    mode(config)            off | alert (default) | auto
    lengths(config, filts)  {filter: (seconds, source)} the fallback would use
    cap_targets(...)        caps the unguided targets of a re-dispatch per filter
    plan_text(...)          "L 60 s, Ha 300 s (tracking test 2026-10-05)"

PS-169: each length is also checked against the measured drift
(tracking_drift: smear budget / tonight's or last night's drift rate,
rounded to a dark length). tracking_drift_cap_mode auto takes the shorter;
observe (default) keeps the length and adds what auto would do to the
source text, so the alert push and the run event show it.

Armer.guide_fallback() acts on it once per night: alert records a run event
(kind guide_fallback) and pushes once what auto would do; auto calls
Armer.fallback_unguided (stop, guider stop, re-dispatch the remainder with
guiding_override "unguided": no StartGuiding, no dithers, no guiding waits;
the Piggy-600 companion is untouched) and the night stays unguided until
dawn. Returning to guided is deliberately not automatic: a PHD2 that
silently stopped correcting once can do it again without any visible state
change, each re-dispatch costs a slew, center and AF, and the unguided
length is proven. Re-arm guided by hand to go back.
"""
from __future__ import annotations

import logging

from photonscript.scheduler import guide_blocks as gb

logger = logging.getLogger(__name__)

MODES = ("off", "alert", "auto")


def mode(config) -> str:
    m = str(getattr(config, "guide_fallback_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def after_min(config) -> float:
    try:
        return max(0.0, float(getattr(config, "guide_fallback_after_min", 10.0)))
    except (TypeError, ValueError):
        return 10.0


def drift_rec(config) -> dict | None:
    """PS-169: the RC16 drift recommendation (tracking_drift.recommend), or
    None when the cap is off or nothing is measured. Never raises."""
    try:
        from photonscript.scheduler import tracking_drift as td
        if td.cap_mode(config) == "off":
            return None
        d = td.live_drift(config)
        if not d:
            return None
        rec = td.recommend(config, "rc16", d.get("total_arcsec_min"))
        rec["drift_source"] = d.get("source")
        return rec if rec.get("exposure_s") else None
    except Exception as e:  # noqa: BLE001 - the fallback never fails on this
        logger.warning("PS-169 drift cap unavailable: %s", e)
        return None


def lengths(config, filters, rec: dict | None = None) -> dict:
    """{filter: (seconds, source)} for each filter (None seconds = no cap).
    PS-169: capped by the drift recommendation in tracking_drift_cap_mode
    auto (observe: the source says what auto would do)."""
    from photonscript.scheduler import tracking_drift as td
    proven = gb.proven_lengths(config)
    if rec is None and filters:
        rec = drift_rec(config)
    out = {}
    for f in filters:
        if f and f not in out:
            s, src = gb.fallback_exposure_s(config, f, proven)
            s2, note = td.cap_length(config, s, rec)
            if note:
                src = f"{src}; {note}"
            out[f] = (s2, src)
    return out


def plan_text(lens: dict) -> str:
    parts = [f"{f} {s:g} s" for f, (s, _) in lens.items() if s]
    srcs = sorted({src for s, src in lens.values() if s})
    return (", ".join(parts) + (f" ({'; '.join(srcs)})" if srcs else "")) or "no cap"


def target_filters(targets) -> list[str]:
    out: list[str] = []
    for t in targets or []:
        for e in getattr(t, "exposures", []) or []:
            f = e.filter_type.value
            if f not in out:
                out.append(f)
    return out


def cap_targets(config, targets) -> list[str]:
    """Cap every unguided target's subs at its filter's fallback length
    (same integration). Tracking-test and focus-calibration targets are left
    alone. Edits the planner's per-night copies in place; one note per set."""
    notes: list[str] = []
    lens = lengths(config, target_filters(targets))
    for t in targets or []:
        if (getattr(t, "start_guiding", False) or getattr(t, "tracking_test", False)
                or getattr(t, "focus_calibration", False)):
            continue
        new = []
        for e in t.exposures:
            s, src = lens.get(e.filter_type.value, (None, "none"))
            c = gb._cap_plan(e, s) if s else e
            if c is not e:
                notes.append(f"{t.name} {e.filter_type.value}: "
                             f"{e.exposure_seconds:g} s -> {s:g} s ({src})")
            new.append(c)
        t.exposures = new
    if notes:
        logger.info("PS-156 unguided fallback caps: %s", "; ".join(notes))
    return notes
