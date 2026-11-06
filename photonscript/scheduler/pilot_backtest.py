"""Night pilot backtest: replay a past night's graded subs through the real
pilot (tick: the sub watch, decide, guards, rate limits, the closed-roof
check) in advise mode, and list what it would have done and when.

Input is one JSON document (a test fixture, or what `photonscript
pilot-backtest --url` reads with GETs from a running PhotonScript):

    {"date", "plan" (/api/runs/<date> plan: night_of, dawn_utc, targets with
     guided), "subs" (/api/runs/<date> subs), "nina_events" {rig: NINA
     /event-history rows}, "unguided_limits" {"rigs": {rig: table}},
     "darks_owed" {rig: [{exp_s, owed}]} (/api/calibration/owed)}

Nothing is sent anywhere: the I/O is a fake fed from the document, the
pilot runs with night_pilot_mode=advise in a scratch data dir, and the
result is the run events it wrote. The armer reads RUNNING while NINA #1's
event history shows a sequence running (SEQUENCE-STARTING to
SEQUENCE-FINISHED); PHD2's live state is unknown in a replay, so the
guider signal comes from each sub's guide_state.

Hindsight: the unguided limits table is today's (it may hold the replayed
night itself); `use_limits=False` replays with none, as the scope had
when the TPoint model was new.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import night_pilot as npil
from photonscript.scheduler.af_stall import FINISHED, parse_time
from photonscript.shared.sub_time import parse_utc

KINDS = ("advise", "alert", "closed_roof", "annotate", "human", "held")


def _events(doc: dict, rig: str) -> list[tuple[datetime, dict]]:
    out = []
    for e in ((doc.get("nina_events") or {}).get(rig) or []):
        t = parse_time(e.get("Time"))
        if t is not None:
            out.append((t, e))
    out.sort(key=lambda x: x[0])
    return out


def running_windows(doc: dict) -> list[tuple[datetime, datetime]]:
    wins, start = [], None
    for t, e in _events(doc, "rc16"):
        ev = str(e.get("Event") or "").upper()
        if ev == "SEQUENCE-STARTING":
            start = t if start is None else start
        elif ev == "SEQUENCE-FINISHED" and start is not None:
            wins.append((start, t))
            start = None
    if start is not None:
        wins.append((start, datetime.max))
    return wins


def unsafe_since(doc: dict, now: datetime) -> datetime | None:
    since = None
    for t, e in _events(doc, "rc16"):
        if t > now:
            break
        if str(e.get("Event") or "").upper() == "SAFETY-CHANGED":
            safe = e.get("IsSafe")
            since = None if safe else (since or t)
    return since


class ReplayArmer:
    def __init__(self, doc: dict):
        self.doc = doc
        self.plan = dict(doc.get("plan") or {})
        self.plan.setdefault("night_of", doc.get("date"))
        self.windows = running_windows(doc)
        self.now = None
        self.last_restart = None
        self.detail = ""

    @property
    def state(self) -> str:
        if self.now is None:
            return "ARMED"
        return "RUNNING" if any(a <= self.now <= b for a, b in self.windows) else "ARMED"

    def _use_guiding(self) -> bool:
        return any(t.get("guided") for t in self.plan.get("targets") or [])

    def _dawn(self):
        return parse_utc(self.plan.get("dawn_utc"))

    def _recipe_tonight(self):
        return None

    async def restart(self, when="after_exposure", now=None):
        return {"ok": False, "detail": "replay: nothing is sent"}

    async def fallback_unguided(self, reason):
        return False


class _NoStore:
    projects: dict = {}


class ReplayIO(npil.PilotIO):
    """Every read from the document, every command refused (advise mode
    never sends one; this is a second lock)."""

    def __init__(self, doc: dict, use_limits: bool = True):
        self.doc = doc
        self.use_limits = use_limits
        self.now = None
        self.pushes = []

    def _graded(self):
        from photonscript.scheduler.sub_watch import graded_at
        return [r for r in self.doc.get("subs") or []
                if graded_at(r) is not None and graded_at(r) <= self.now]

    async def nina1_running(self, cfg):
        subs = [r for r in self._graded() if (r.get("rig") or "rc16") == "rc16"]
        tgt = subs[-1].get("target") if subs else None
        return ([tgt or "target", "Smart Exposure"], tgt)

    async def phd2_state(self, cfg):
        return None

    async def phd2_stop(self, cfg):
        return False, "replay: nothing is sent"

    async def nina1_wait_idle(self, cfg, max_s: float = 300.0):
        return None

    async def nina1_autofocus(self, cfg):
        return False, "replay: nothing is sent"

    async def piggy_af(self, cfg, armer):
        return False, "replay: nothing is sent"

    def store(self):
        return _NoStore()

    def subs(self, cfg, night):
        return self._graded()

    def events(self, cfg, night):
        return []

    async def notify(self, cfg, msg, priority=0, **_kw):
        self.pushes.append((priority, msg))

    def dark_status(self, cfg):
        """The RC16 darks as the document's owed view had them."""
        rows = {float(x["exp_s"]): x for x in (self.doc.get("darks_owed") or {}).get("rc16") or []}

        def status(s):
            x = rows.get(float(s))
            if x and int(x.get("have") or 0) > 0:
                return {"match": "matched", "text": f"darks match: {x['have']} {s:g} s "
                        "darks in the library"}
            return {"match": "none", "text": f"no {s:g} s darks in the library "
                    f"(owed {x.get('owed') if x else '?'})"}
        return status

    def limits_tables(self, cfg):
        if not self.use_limits:
            return {}
        return dict(((self.doc.get("unguided_limits") or {}).get("rigs") or {}))

    async def af_finished(self, cfg):
        out = {}
        for rig in ("rc16", "piggyback"):
            out[rig] = [t for t, e in _events(self.doc, rig)
                        if str(e.get("Event") or "").upper() == FINISHED and t <= self.now]
        return out

    def roof_closed_since(self, cfg, now):
        return unsafe_since(self.doc, now)

    def darks_owed(self, cfg):
        return dict(self.doc.get("darks_owed") or {})

    def light_lengths(self, cfg, armer):
        out = {"rc16": [], "piggyback": []}
        for r in self.doc.get("subs") or []:
            v = r.get("exp_s")
            rig = r.get("rig") or "rc16"
            if v and float(v) not in out.setdefault(rig, []):
                out[rig].append(float(v))
        return out

    async def guider_hook(self, cfg, rig, evidence, now):
        return None


def _times(doc: dict) -> list[datetime]:
    from photonscript.scheduler.sub_watch import graded_at
    ts = {graded_at(r) for r in doc.get("subs") or []}
    for t, e in _events(doc, "rc16"):
        if str(e.get("Event") or "").upper() == "SAFETY-CHANGED":
            ts.add(t + timedelta(minutes=1))
    return sorted(t for t in ts if t is not None)


def run(cfg, doc: dict, *, use_limits: bool = True, data_dir: str | None = None) -> dict:
    """Replay the night; {"night", "timeline": [{t, local, rig, kind,
    detail}], "actions": the pilot's advised actions, "pushes", "closed_roof"}."""
    tmp = None
    if data_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="pilot_backtest_")
        data_dir = tmp.name
    try:
        rcfg = cfg.model_copy(update={"data_dir": str(data_dir),
                                      "night_pilot_mode": "advise"})
        armer, io = ReplayArmer(doc), ReplayIO(doc, use_limits=use_limits)
        night = armer.plan.get("night_of") or doc.get("date")

        async def _go():
            for t in _times(doc):
                armer.now = io.now = t
                await npil.tick(rcfg, armer, io, now=t)
        asyncio.run(_go())
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import read_jsonl
        rows = [e for e in read_jsonl(events_path(rcfg, night))
                if e.get("kind") == npil.KIND and e.get("value") in KINDS
                and e.get("value") != "advise"]
        st = npil.load_state(rcfg, night)
        # every advised action (the run events hold only the first per rig
        # and action), with its reason
        rows += [{"t": a.get("t"), "rig": a.get("rig"), "value": "advise",
                  "detail": f"would have {a.get('what')} because {a.get('because')}"
                  + (f" ({(a.get('params') or {}).get('darks_text')})"
                     if (a.get("params") or {}).get("darks_text") else "")}
                 for a in st.get("actions") or [] if a.get("kind") == "advise"]
        rows.sort(key=lambda e: str(e.get("t")))
        timeline = [{"t": e.get("t"), "local": npil._hm(rcfg, parse_utc(e.get("t"))),
                     "rig": e.get("rig"), "kind": e.get("value"),
                     "detail": e.get("detail")} for e in rows]
        return {"night": night, "timeline": timeline,
                "actions": [a for a in st.get("actions") or []
                            if a.get("kind") == "advise"],
                "would_have": [npil._would_line(rcfg, a) for a in st.get("actions") or []
                               if a.get("kind") == "advise"],
                "closed_roof": st.get("closed_roof") or [],
                "morning": npil.morning_lines(rcfg, night),
                "pushes": io.pushes}
    finally:
        if tmp is not None:
            tmp.cleanup()


def format_report(rep: dict) -> str:
    out = [f"Night pilot backtest {rep['night']} (advise; nothing sent)"]
    for x in rep["timeline"]:
        if x["kind"] == "held":
            continue
        out.append(f"  {x['local']} {x['rig'] or '':9} {x['kind']:11} {x['detail']}")
    out.append("")
    out += rep.get("morning") or []
    return "\n".join(out)


def load_doc(path: str | Path) -> dict:
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))
