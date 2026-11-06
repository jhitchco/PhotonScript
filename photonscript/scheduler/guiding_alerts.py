"""Collapse the guided-but-not-guiding Pushover flood (PS-66).

On 2026-09-26 PHD2 flapped between LostLock and Guiding all night. The armer's
watchdog treated every flap as a fresh episode, so each one produced a "not
guiding" push, often an "Auto-recovery" push and a "Guiding recovered" push:
109 guiding notifications, 77 of them sent to the phone. The shared Pushover
dedup could not help because every message carries a changing "N min into
dark".

This module is the pure decision logic the armer consults before it pushes a
guiding alert. It never hides anything: the armer writes every event it holds
back to the notification audit (notifications.jsonl, sent=false) so
/api/notifications still shows the full story.

Rules (all windows configurable, defaults in brackets):
  * one budget - "lost", "flapping" and "auto-recovery" pushes share ONE
             budget: at most one of them per guiding_alert_repeat_min [30].
  * lost   - the first loss pushes. Losses inside the window are held back.
  * flapping - when the budget comes due again and guiding_flap_count [3] or
             more losses happened inside guiding_flap_window_min [60], the push
             is a single "guiding flapping: N losses" summary instead.
  * auto-recovery - pushes when this episode's loss was itself pushed (the
             lost -> auto-recovery -> escalation story of one continuous loss
             stays intact) or the budget is due; a FAILED restart (guider
             unreachable) also pushes, at most once per window.
  * recovered - pushed only when the loss lasted at least
             guiding_recovered_push_min [10] minutes; a short blip's recovery is
             audited at priority -1 instead.
  * escalation - a loss that STAYS lost through the armer's whole ladder
             (~5 min) still sends the priority-2 escalation, at most one per
             repeat window (priority 2 now really alarms: retry + expire).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

_DEFAULTS = {
    "guiding_alert_repeat_min": 30.0,
    "guiding_recovered_push_min": 10.0,
    "guiding_flap_count": 3,
    "guiding_flap_window_min": 60.0,
}


def _knob(config, key):
    try:
        v = getattr(config, key, None)
        return _DEFAULTS[key] if v is None else type(_DEFAULTS[key])(v)
    except (TypeError, ValueError):
        return _DEFAULTS[key]


class GuidingAlertGate:
    """Per-night state for guiding alert decisions. Times are the armer's
    naive-UTC `now`. Construct once per night (the armer resets it on arm)."""

    def __init__(self, config=None):
        self.repeat = timedelta(minutes=float(_knob(config, "guiding_alert_repeat_min")))
        self.recovered_min = float(_knob(config, "guiding_recovered_push_min"))
        self.flap_count = max(2, int(_knob(config, "guiding_flap_count")))
        self.flap_window = timedelta(minutes=float(_knob(config, "guiding_flap_window_min")))
        self.losses: deque[datetime] = deque()
        self.episode_start: datetime | None = None
        self.episode_pushed = False   # this episode's loss reached the phone
        self.last_push: datetime | None = None          # shared budget
        self.last_autorec_fail_push: datetime | None = None
        self.last_escalation: datetime | None = None

    # -- helpers -------------------------------------------------------------
    def _prune(self, now: datetime) -> None:
        while self.losses and now - self.losses[0] > self.flap_window:
            self.losses.popleft()

    def losses_in_window(self, now: datetime) -> int:
        self._prune(now)
        return len(self.losses)

    def _due(self, last: datetime | None, now: datetime) -> bool:
        return last is None or now - last >= self.repeat

    # -- decisions -----------------------------------------------------------
    def on_lost(self, now: datetime, since: datetime | None = None) -> str:
        """A new loss episode was confirmed (the armer's first warning).
        `since` is when the not-locked streak began (defaults to now).
        Returns 'push', 'flap' (send the flapping summary instead) or
        'suppress'."""
        self.episode_start = since or now
        self.losses.append(now)
        self._prune(now)
        self.episode_pushed = False
        if not self._due(self.last_push, now):
            return "suppress"
        self.last_push = now
        self.episode_pushed = True
        return "flap" if len(self.losses) >= self.flap_count else "push"

    def on_auto_recover(self, now: datetime, ok: bool = True) -> bool:
        """True = push the auto-recovery notice; False = audit only."""
        if self.episode_pushed or self._due(self.last_push, now):
            self.last_push = now
            return True
        if not ok and self._due(self.last_autorec_fail_push, now):
            self.last_autorec_fail_push = now
            return True
        return False

    def on_escalate(self, now: datetime) -> bool:
        """True = send the priority-2 escalation; False = audit only."""
        if self._due(self.last_escalation, now):
            self.last_escalation = now
            return True
        return False

    def on_recovered(self, now: datetime) -> tuple[bool, float]:
        """Guiding locked again. Returns (push?, minutes the loss lasted)."""
        start = self.episode_start or now
        mins = max(0.0, (now - start).total_seconds() / 60.0)
        self.episode_start = None
        self.episode_pushed = False
        return mins >= self.recovered_min, mins

    def flap_message(self, now: datetime) -> str:
        n = self.losses_in_window(now)
        win = int(self.flap_window.total_seconds() // 60)
        hold = int(self.repeat.total_seconds() // 60)
        return (f"Guiding flapping: {n} losses in the last {win} min (PHD2 "
                "re-locks, then loses the star again). Holding further "
                f"guiding pushes for {hold} min; every event is still in "
                "the notification log. Likely a weak or wrong guide star or a "
                "bad calibration: check PHD2 (star SNR/mass, dark library, "
                "recalibrate near Dec 0 at the meridian).")
