"""Hooks the night pilot calls that another feature fills.

guider_active_unguided: the night pilot's sub watch saw PHD2 active
(guiding, settling, lost star) during a sub of an UNGUIDED night, and its
guider_stop action is in act mode (night_pilot_guider_stop_mode=act; advise
pushes the alert only and never calls this). The unguided guard
(branch ps-unguided-guard: the armer's connect step, the generated
sequence's flip / AF guider resume, a live guard) fills this function so
the pilot's reaction goes through ONE place that knows how to stop guiding
and keep NINA from starting it again.

Contract:
  guider_active_unguided(cfg, rig, evidence, now) -> dict | None
    (may be async). evidence: {"because": the pilot's reason text, ...}
    from the sub watch (guide_state, phd2_live, rigs_guiding, ecc, file).
    Return {"ok": bool, "detail": str} when the guard handled it: ok True =
    guiding is stopped (the pilot then verifies on the next subs' ecc like
    any action), ok False = it tried and failed (the pilot pushes for a
    human). Return None when not handled here: the pilot falls back to its
    own PHD2 stop_capture (PilotIO.phd2_stop, which never touches the PHD2
    calibration). Must not raise (the pilot logs and falls back if it does).

Until the guard lands this returns None, so the pilot's fallback runs.
"""
from __future__ import annotations

from datetime import datetime


def guider_active_unguided(cfg, rig: str, evidence: dict,
                            now: datetime | None = None) -> dict | None:
    """Not handled here yet (the ps-unguided-guard branch fills it)."""
    return None
