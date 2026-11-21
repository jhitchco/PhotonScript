"""Autofocus that keeps restarting without finishing.

2026-10-08 to 10-11: the RC16 focuser dropped off USB. NINA #1's event
history showed 55 AUTOFOCUS-STARTING and 54 AUTOFOCUS-POINT-ADDED with no
finished event: every autofocus quit after one point, the sequence went on
imaging out of focus, and nothing paged for three nights.

This reads a rig's ninaAPI event history (GET <base>/event-history; read
only) and splits it into autofocus runs:

    af_runs(events, last_af_reports, tz_config) -> [run]
        one run per AUTOFOCUS-STARTING, ended by the next start, an
        AUTOFOCUS-* end event (FINISHED, or a failure event), or the
        sequence finishing. Each run: start / end (naive UTC), points,
        hfr_zero (points with HFR 0: no stars measured), positions,
        finished (True / False / None = unknown), unsafe (the roof was
        unsafe at the start or turned unsafe before the run ended),
        end_by (next_start | af_event | sequence | open).
    judge(run, min_points, now, watching_since) -> ok | bad | cut | open
        cut: the roof was unsafe during the run, or the sequence ended
        under it (not the focuser's fault). open: still running.
        bad: fewer than min_points points, or it did not complete
        (no AUTOFOCUS-FINISHED and no successful NINA AF report inside the
        run). Completion is only judged for runs that ended after the
        agent started watching (it saw the AF reports of that time); older
        runs are judged by their point count alone.
    stall(runs, n, min_points, ...) -> the trailing streak of bad runs when
        it is at least n long (cut / open runs are skipped, an ok run ends
        the streak), else None.
    alert_text(config, rig, streak, focuser) -> the page

The live check is the telescope agent's _af_stall_watch (one Pushover per
rig per night, key af-stall-<rig>-<night>, plus a run event kind af_stall
that the morning report lists). Config: af_stall_alert (on),
af_stall_runs (3), af_stall_min_points (4). Never alerts while the rig's
sequence is not imaging.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ALERT_TITLE = "PhotonScript autofocus not finishing"
START = "AUTOFOCUS-STARTING"
POINT = "AUTOFOCUS-POINT-ADDED"
FINISHED = "AUTOFOCUS-FINISHED"
SAFETY = "SAFETY-CHANGED"
SEQ_END = ("SEQUENCE-FINISHED", "SEQUENCE-STOPPED")
OPEN_MAX_MIN = 15.0   # an AF with no event for this long has ended


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def parse_time(s) -> datetime | None:
    """ninaAPI event Time ('2026-10-11T21:47:20.1606651-06:00') -> naive
    UTC. A naive string is taken as UTC."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def report_ok(report: dict | None) -> bool | None:
    """A NINA AF report (last-af) -> True when it has a finite calculated
    focus position, False when it failed (NaN), None when not a report."""
    if not isinstance(report, dict) or not report.get("Timestamp"):
        return None
    cfp = report.get("CalculatedFocusPoint") or {}
    pos = _num(cfp.get("Position")) if isinstance(cfp, dict) else None
    return pos is not None and pos > 0


def af_runs(events: list[dict], reports: list[dict] | None = None) -> list[dict]:
    """Split an event history into autofocus runs (oldest first). reports:
    NINA AF reports seen ({"Timestamp", "CalculatedFocusPoint", ...}); a
    report time inside a run marks it completed (or failed)."""
    rows = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        t = parse_time(e.get("Time"))
        if t is not None:
            rows.append((t, e))
    rows.sort(key=lambda r: r[0])
    rep = []
    for r in reports or []:
        t = parse_time((r or {}).get("Timestamp"))
        ok = report_ok(r)
        if t is not None and ok is not None:
            rep.append((t, ok))
    runs: list[dict] = []
    cur: dict | None = None
    safe: bool | None = None

    def close(end, by):
        nonlocal cur
        if cur is None:
            return
        cur["end"], cur["end_by"] = end, by
        runs.append(cur)
        cur = None

    for t, e in rows:
        ev = str(e.get("Event") or "").upper()
        if ev == SAFETY:
            safe = e.get("IsSafe")
            if cur is not None and safe is False:
                cur["unsafe"] = True
            continue
        if ev == START:
            close(t, "next_start")
            cur = {"start": t, "end": None, "end_by": "open", "points": 0,
                   "hfr_zero": 0, "positions": [], "finished": None,
                   "failed_event": None, "unsafe": safe is False,
                   "last_t": t}
            continue
        if cur is None:
            continue
        if ev == POINT:
            cur["points"] += 1
            cur["last_t"] = t
            if not _num(e.get("HFR")):
                cur["hfr_zero"] += 1
            pos = _num(e.get("Position"))
            if pos is not None:
                cur["positions"].append(int(pos))
        elif ev == FINISHED:
            cur["finished"] = True
            close(t, "af_event")
        elif ev.startswith("AUTOFOCUS-"):
            # an AF failure / cancel event (any other AUTOFOCUS-* name)
            cur["finished"] = False
            cur["failed_event"] = ev
            close(t, "af_event")
        elif ev in SEQ_END:
            close(t, "sequence")
    if cur is not None:
        runs.append(cur)
    # NINA AF reports: one inside a run (start .. end, open runs: after the
    # start) says it completed; a NaN fit says it failed
    for r in runs:
        if r["finished"] is not None:
            continue
        hi = r["end"]
        hits = [ok for t, ok in rep if t >= r["start"] and (hi is None or t <= hi)]
        if hits:
            r["finished"] = hits[-1]
            r["report"] = True
            if r["end"] is None:
                r["end"], r["end_by"] = max(
                    t for t, _ok in rep if t >= r["start"]), "report"
    return runs


def judge(run: dict, min_points: int, now: datetime | None = None,
          watching_since: datetime | None = None) -> str:
    """ok | bad | cut | open (see the module doc)."""
    if run.get("unsafe"):
        return "cut"
    if run.get("end_by") == "sequence":
        return "cut"
    if run.get("end") is None:
        last = run.get("last_t") or run["start"]
        if now is None or (now - last).total_seconds() / 60.0 < OPEN_MAX_MIN:
            return "open"
    if run.get("finished") is True and run["points"] >= min_points:
        return "ok"
    if run["points"] < min_points or run.get("finished") is False:
        return "bad"
    # enough points, no completion seen: only a miss when we were watching
    # (the agent saw the AF reports of that time)
    end = run.get("end") or run.get("last_t") or run["start"]
    if watching_since is not None and end >= watching_since:
        return "bad"
    return "ok"


def stall(runs: list[dict], n: int, min_points: int,
          now: datetime | None = None,
          watching_since: datetime | None = None) -> list[dict] | None:
    """The trailing streak of bad runs (oldest first) when it is at least n
    long, else None."""
    n = max(1, int(n or 1))
    streak: list[dict] = []
    for r in runs:
        v = judge(r, min_points, now, watching_since)
        r["verdict"] = v
        if v in ("cut", "open"):
            continue
        if v == "ok":
            streak = []
        else:
            streak.append(r)
    return streak if len(streak) >= n else None


def tonight(config, runs: list[dict], night: str) -> list[dict]:
    """The runs that started on this night (local noon to noon)."""
    from photonscript.shared.phd2_store import night_of
    return [r for r in runs if night_of(config, r["start"]) == night]


def _local_hm(config, t: datetime | None) -> str:
    if t is None:
        return "?"
    try:
        from photonscript.shared.localtime import to_local
        return to_local(config, t).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return t.strftime("%H:%M") + "Z"


def causes(streak: list[dict], focuser: dict | None) -> list[str]:
    """Likely causes, the best supported first."""
    pts = sum(r["points"] for r in streak)
    zero_hfr = sum(r["hfr_zero"] for r in streak)
    pos = {p for r in streak for p in r["positions"]}
    f = focuser or {}
    fpos = _num(f.get("Position"))
    stuck = (fpos == 0 or pos == {0}
             or (len(pos) == 1 and sum(len(r["positions"]) for r in streak) > 1))
    moving = ("the focuser is not moving (it dropped off USB or froze; "
              + (f"NINA reads position {int(fpos)}" if fpos is not None
                 else "NINA may still show it connected") + ")")
    clouds = ("clouds or a closed roof (no stars measured"
              + (": HFR 0 on every point" if pts and zero_hfr == pts else "")
              + ")")
    if stuck:
        return [moving, clouds]
    if pts and zero_hfr == pts:
        return [clouds, moving]
    return [moving, clouds]


def alert_text(config, rig: str, streak: list[dict], focuser: dict | None,
               min_points: int) -> str:
    from photonscript.shared.rigs import rig_label
    f = focuser or {}
    name = str(f.get("DisplayName") or f.get("Name") or "the focuser")
    counts = ", ".join(str(r["points"]) for r in streak)
    unfinished = sum(1 for r in streak if r.get("finished") is not True)
    why = " or ".join(causes(streak, focuser))
    return (f"{rig_label(config, rig)}: autofocus did not finish "
            f"{len(streak)} times in a row (focuser {name}). Points per run: "
            f"{counts} (a good run has {min_points}+), {unfinished} with no "
            f"completed fit; last start {_local_hm(config, streak[-1]['start'])} "
            f"local. Likely cause: {why}. The sequence keeps imaging out of "
            "focus. Check the focuser in NINA (Equipment > Focuser: move it a "
            "few steps, reconnect it) and the sky, then rerun autofocus.")


def summary(streak: list[dict]) -> dict:
    """The run event's extra fields."""
    from photonscript.shared.phd2_store import iso_z
    return {"runs": len(streak), "points": [r["points"] for r in streak],
            "first": iso_z(streak[0]["start"]),
            "last": iso_z(streak[-1]["start"])}

