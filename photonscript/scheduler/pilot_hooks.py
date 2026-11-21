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

Filled 2026-10-14: the unguided guard (scheduler/unguided_guard.py) already
watches every 30 s tick while RUNNING and, with unguided_guider_policy
"disconnect", stops NINA's guiding, disconnects NINA's guider and stops PHD2
itself. So with that policy the pilot leaves it to the guard (one place
acts) and verifies on the next subs like any action; with "alert" or "off"
the guard does not stop anything and the pilot's own PHD2 stop runs.
"""
from __future__ import annotations

from datetime import datetime


def guider_active_unguided(cfg, rig: str, evidence: dict,
                            now: datetime | None = None) -> dict | None:
    try:
        from photonscript.scheduler import unguided_guard
        if unguided_guard.policy(cfg) == "disconnect":
            return {"ok": True,
                    "detail": "left to the unguided guard (policy disconnect: "
                              "it stops guiding and disconnects NINA's guider "
                              "on its 30 s tick)"}
    except Exception:  # noqa: BLE001
        pass
    return None
