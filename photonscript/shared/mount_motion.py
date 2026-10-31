"""PS-27 part 2: is the shared mount moving right now?

One in-process tracker (TRACKER) of the mount NINA #1 owns, fed by:

  * the RC16 telescope agent's existing 5 s ninaAPI mount poll (the same
    poll that writes the PS-67 mount log), through observe();
  * the Piggy-600 settle gate's own 2 s read-only polls while it holds;
  * PHD2 SettleBegin / SettleDone (the RC16 agent's guider listener),
    through observe_guider().

Readers: the settle gate (scheduler.split_guard.run_settle_gate) asks how
long the mount has been still and whether PHD2 is settling; the abort
watcher (split_guard.on_rc16_mount) acts on the move kinds observe()
returns. Everything is in memory: `photonscript start` runs the agents and
the web service in one process (mode full on the scope PC). A tracker with
no recent sample (STALE_S) starts over, so a stopped poll never reads as
"still for an hour".

Kinds observe() returns (None = no change):
    slew-start   Slewing went true        slewing   still slewing
    slew-end     Slewing went false       pier      side of pier changed
    park/unpark  AtPark changed           move      RA/Dec jumped more than
                                                    move_arcmin since the
                                                    previous sample
"""

from __future__ import annotations

STALE_S = 30.0          # no sample for this long: start observing again
SETTLE_STALE_S = 180.0  # a PHD2 settle "in progress" this long is ignored
                        # (PHD2 gone mid-settle, SettleDone never came)
MOVE_KINDS = ("slew-start", "slewing", "slew-end", "pier", "park", "unpark",
              "move")


class MotionTracker:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.last: dict | None = None      # last mount sample
        self.last_t: float | None = None   # when it was taken (epoch s)
        self.first_t: float | None = None  # start of continuous observation
        self.last_move_t: float | None = None
        self.last_move_kind: str | None = None
        self.settling = False
        self.settle_t: float | None = None  # when the current settle began

    def observe(self, sample: dict | None, now: float,
                move_arcmin: float = 0.5) -> str | None:
        """Feed one mount sample (shared.mount_log.sample_from_nina shape:
        ra, dec in degrees, slewing, parked, pier). Returns the move kind
        seen, or None."""
        from photonscript.shared.pointing import sep_arcmin
        if not sample:
            return None
        prev = self.last
        if self.last_t is not None and now - self.last_t > STALE_S:
            prev = None
        kind = None
        if prev is None:
            self.first_t = now
            if sample.get("slewing"):
                kind = "slewing"
        elif sample.get("slewing") and not prev.get("slewing"):
            kind = "slew-start"
        elif sample.get("slewing"):
            kind = "slewing"
        elif prev.get("slewing"):
            kind = "slew-end"
        elif (sample.get("pier") is not None and prev.get("pier") is not None
              and sample.get("pier") != prev.get("pier")):
            kind = "pier"
        elif bool(sample.get("parked")) != bool(prev.get("parked")):
            kind = "park" if sample.get("parked") else "unpark"
        else:
            try:
                d = sep_arcmin(prev["ra"], prev["dec"], sample["ra"], sample["dec"])
            except Exception:  # noqa: BLE001
                d = 0.0
            if d > float(move_arcmin or 0.5):
                kind = "move"
        if kind is not None:
            self.last_move_t, self.last_move_kind = now, kind
        self.last, self.last_t = dict(sample), now
        return kind

    def observe_guider(self, settling: bool, now: float) -> None:
        settling = bool(settling)
        if settling and not self.settling:
            self.settle_t = now
        self.settling = settling
        if not settling:
            self.settle_t = None

    def still_s(self, now: float) -> float | None:
        """Seconds since the last move (or since observation started),
        None when there is no fresh sample."""
        if self.first_t is None or self.last_t is None \
                or now - self.last_t > STALE_S:
            return None
        since = self.first_t
        if self.last_move_t is not None and self.last_move_t > since:
            since = self.last_move_t
        return max(0.0, now - since)

    def guider_settling(self, now: float) -> bool:
        return (self.settling and self.settle_t is not None
                and now - self.settle_t <= SETTLE_STALE_S)

    def snapshot(self, now: float) -> dict:
        s = self.still_s(now)
        return {"slewing": bool((self.last or {}).get("slewing")),
                "still_s": None if s is None else round(s, 1),
                "last_move_kind": self.last_move_kind,
                "last_move_ago_s": (None if self.last_move_t is None
                                    else round(now - self.last_move_t, 1)),
                "guider_settling": self.guider_settling(now),
                "sample_age_s": (None if self.last_t is None
                                 else round(now - self.last_t, 1))}


TRACKER = MotionTracker()
