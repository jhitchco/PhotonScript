"""PS-212 replay helpers: the pre-v2 Pushover audit log through the v2 policy.

legacy_kind(title, message, priority) maps a pre-v2 record (no kind) to the
kind its call site carries now (the docs/PUSHOVER_V2.md table, by title and
message), simulate(records) replays them through pushover.decide, the
once-per-fact rule, the daytime gate and the hourly / dedup limiter at each
record's own time, and story_pushes(records) adds the v2 story pushes a
night of those records would make (plan heads-up, imaging now, first light
per rig, target switches, flats, end of night, roof reopened).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from photonscript.shared import pushover as po

FIXTURE = Path(__file__).parent / "fixtures" / "ps212" / "notifications_2026-10-16_23.jsonl"


def load(path: Path = FIXTURE) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def legacy_kind(title: str, msg: str, prio: int) -> tuple[str, bool]:
    """(kind, safety) of a pre-v2 record, per the migrated call site."""
    t, m = title or "", msg or ""
    if t.startswith("Tonight:") or t in (
            "PhotonScript armed", "PhotonScript running", "PhotonScript disarmed",
            "PhotonScript TPoint coverage", "PhotonScript frozen sensor",
            "PhotonScript heartbeat", "PhotonScript resumed", "PhotonScript auto flats",
            "PhotonScript preflight"):     # auto-arm preflight: test_push off now
        return "info", False
    if t == "PhotonScript complete":
        return "story", False              # the end-of-night report replaces it
    if t.endswith(" integrated") or t == "PhotonScript TPoint run finished":
        return "story", False
    if t == "PhotonScript library":
        return ("act", True) if "Steps failed" in m else ("info", False)
    if t in ("PhotonScript transfer", "PhotonScript ERROR"):
        return "act", True
    if t == "PhotonScript safety":   # safety only while dark (after dawn: act)
        return ("act", True) if "UNREADABLE" in m else ("info", False)
    if t == "PhotonScript cooler recovery":
        return ("act", True) if "no more reconnects" in m else ("info", False)
    if t == "PhotonScript cooler":
        return ("info", False) if "turning it on" in m else ("act", False)
    if t == "PhotonScript paused":
        if m.startswith("Unsafe while paused") and "FAILED" in m:
            return "act", True
        return "info", False
    if t == "PhotonScript piggyback":
        return ("act", False) if "NOT" in m else ("info", False)
    if t == "PhotonScript PHD2 calibration":
        return ("info", False) if "re-dispatched" in m else ("act", False)
    if t == "PhotonScript TPoint preflight":
        return ("act", False) if prio >= 1 else ("info", False)
    if t == "PhotonScript unguided guard":
        return ("info", False) if "Disconnected it" in m else ("act", False)
    if t == "PhotonScript NANNY" and "consecutive rejected subs" in m:
        return "info", False               # the night pilot's push says it
    if t in ("PhotonScript NANNY", "PhotonScript NINA watch", "PhotonScript cooler gate"):
        return ("act", False) if prio >= 1 else ("info", False)
    if t == "PhotonScript night pilot":
        human = any(k in m for k in ("needs a human", "FAILED", "ALERT", "did NOT help"))
        return ("act", False) if human and (prio >= 1 or "needs a human" in m) else ("info", False)
    if t.startswith("PhotonScript sub doctor"):
        return ("act", False) if prio >= 1 else ("info", False)
    if t == "PhotonScript TPoint run":
        return "act", False                # no samples: quiet_reason first
    if t in ("PhotonScript settings", "PhotonScript PHD2 audit", "PhotonScript ProTrack",
             "PhotonScript Piggy guard", "PhotonScript off target",
             "PhotonScript observatory apps", "PhotonScript apps not running",
             "PhotonScript auto-arm warning", "PhotonScript auto-arm skipped"):
        return "act", False
    return "info", False


def _ts(r: dict) -> datetime:
    t = datetime.fromisoformat(r["ts"])
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def plan_windows(cfg, records: list[dict]) -> dict:
    """{night: (open, close)} naive UTC: from the night's first arm to its
    plan heads-up (pre-config - 90 min = astro dark - 120 min, or the arm
    when later), only for arms before astro dark. Astro dark from the
    evening forecast record ("Astro dark (gate): 20:04-06:00 local"), else
    20:00 local; local = UTC - 6 h (MDT, these nights)."""
    import re
    from datetime import timedelta
    dusk: dict = {}
    arms: dict = {}
    for r in sorted(records, key=lambda x: x["ts"]):
        now = _ts(r)
        n = po.night_of(cfg, now)
        m = re.search(r"Astro dark \(gate\): (\d\d):(\d\d)", r.get("message", ""))
        if m and n not in dusk:
            loc = datetime.strptime(n, "%Y-%m-%d").replace(hour=int(m[1]), minute=int(m[2]))
            dusk[n] = loc + timedelta(hours=6)
        if r.get("title") == "PhotonScript armed" and n not in arms:
            arms[n] = now
    out = {}
    for n, a in arms.items():
        d = dusk.get(n) or datetime.strptime(n, "%Y-%m-%d").replace(hour=20) + timedelta(hours=6)
        if a < d:
            # the arm opens the window before its checks push; the ARMED
            # record itself is written after them
            out[n] = (a - timedelta(minutes=5), max(a, d - timedelta(minutes=120)))
    return out


def simulate(cfg, records: list[dict], quiet=None, windows: dict | None = None) -> list[dict]:
    """Every record with its v2 decision: {"night", "kind", "safety", "sent",
    "reason", "fact", ...}. quiet(record) -> reason or None (a watcher's
    known reason). windows: plan_windows() (non-safety acts inside one wait
    for the plan heads-up). Uses the real policy functions at each record's
    time."""
    state: dict = {}
    out = []
    windows = windows or {}
    for r in sorted(records, key=lambda x: x["ts"]):
        now = _ts(r)
        epoch = now.replace(tzinfo=timezone.utc).timestamp()
        title, msg, prio = r.get("title", ""), r.get("message", ""), int(r.get("priority", 0))
        kind, safety = legacy_kind(title, msg, prio)
        loc = now - timedelta(hours=6)      # MDT
        if title == "PhotonScript safety" and safety and (6, 5) <= (loc.hour, loc.minute) < (12, 0):
            safety = False                  # past astro dawn (~06:05): not a safety act
        night = po.night_of(cfg, now)
        if kind == "story" and title == "PhotonScript complete":
            fact = f"story|end:{night}"
        else:
            fact = None if safety else po.fact_key(kind, title, msg)
        rec = {"ts": r["ts"], "night": night, "title": title, "kind": kind,
               "safety": safety, "fact": fact}
        why = quiet(r) if quiet else None
        if why:
            out.append({**rec, "sent": False, "reason": f"quiet:{why}"})
            continue
        push, reason = po.decide(cfg, kind, safety, now)
        if push and fact and po._fact_seen(state, night, fact):
            push, reason = False, "dup-fact"
        w = windows.get(night)
        if push and kind == "act" and not safety and w and w[0] <= now < w[1]:
            po._fact_mark(state, night, fact, epoch)   # told in the heads-up
            out.append({**rec, "sent": False, "reason": "for-plan"})
            continue
        if push and po._is_daytime(cfg, now):
            ok, reason2 = po._daytime_gate(state, epoch, title,
                                           max(prio, 1) if kind == "act" else min(prio, 0),
                                           4 * 3600)
            if not ok:
                push, reason = False, reason2
        if push:
            allow, reason, _note = po._gate(state, epoch, now.strftime("%Y-%m"),
                                            f"{title}\x1f{msg}",
                                            max(prio, 1) if kind == "act" else min(prio, 0),
                                            300, 20, 9000)
            push = allow
            if allow and fact:
                po._fact_mark(state, night, fact, epoch)
        out.append({**rec, "sent": bool(push), "reason": reason})
    return out


def imaging_nights(records: list[dict], cfg) -> dict:
    """{night: facts} for nights that dispatched a sequence: the number of
    targets armed, rigs imaging, a dusk flats dispatch, roof reopens."""
    nights: dict = defaultdict(lambda: {"targets": 0, "dispatched": False,
                                        "piggy": False, "flats": False, "reopens": 0})
    for r in records:
        n = po.night_of(cfg, _ts(r))
        t, m = r.get("title", ""), r.get("message", "")
        if t == "PhotonScript running":
            nights[n]["dispatched"] = True
        if t == "PhotonScript armed" and "ARMED for" in m:
            names = m.split("]:", 1)[-1].split(" - ")[0]
            nights[n]["targets"] = max(nights[n]["targets"], len(names.split(",")))
        if t == "PhotonScript piggyback" and "started" in m:
            nights[n]["piggy"] = True
        if t == "PhotonScript auto flats":
            nights[n]["flats"] = True
        if t == "PhotonScript resumed":
            nights[n]["reopens"] += 1
    return {k: v for k, v in nights.items() if v["dispatched"]}


def story_pushes(info: dict) -> int:
    """The v2 story pushes a dispatched night makes besides the replayed
    records (whose "PhotonScript complete" is the end-of-night report):
    plan heads-up, imaging now, first light per rig, a switch per further
    target, the dusk flats counts, each long roof closure's reopen."""
    rigs = 2 if info["piggy"] else 1
    return (1 + 1 + rigs + max(0, info["targets"] - 1)
            + (1 if info["flats"] else 0) + info["reopens"])
