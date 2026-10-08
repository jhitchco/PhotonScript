"""PS-67: what each rig was doing over a night, as timeline rows.

timeline(config, date) merges
  runs/<date>_mount.jsonl    mount: slewing / tracking / parked / idle
  runs/<date>_events.jsonl   NINA running instruction per rig, PHD2 state
                             and 60 s RMS samples
  safety_history.jsonl       unsafe windows (PS-71)
  runs/<date>_subs.jsonl     sub exposure spans per rig
into rows of segments {start, end, state, label}. Nights before the mount
log and event log existed show only sub spans and safety windows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

RC16, PIGGY = "rc16", "piggyback"


def classify_instruction(name) -> str | None:
    """A NINA sequence item name -> a timeline state (None = idle)."""
    n = str(name or "").strip().lower()
    if not n:
        return None
    rules = (("meridian", "meridian flip"), ("autofocus", "autofocus"),
             ("auto focus", "autofocus"), ("center", "centering"),
             ("centre", "centering"), ("unpark", "slewing"),
             ("park", "parked"), ("slew", "slewing"),
             ("calibrat", "calibrating"), ("guid", "guider"),
             ("dither", "dither"), ("flat", "flats"), ("dark", "darks"),
             ("bias", "darks"), ("exposure", "exposing"),
             ("wait", "waiting"), ("cool", "cooling"), ("warm", "cooling"))
    for key, state in rules:
        if key in n:
            return state
    return "other"


GUIDER_STATES = {"guiding": "guiding", "settling": "settling",
                 "calibrating": "calibrating", "lost_star": "guider lost",
                 "error": "guider lost", "stopped": None}


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _merge(segs: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in sorted(segs, key=lambda x: x["start"]):
        if s["end"] <= s["start"]:
            continue
        if out and out[-1]["state"] == s["state"] \
                and out[-1].get("label") == s.get("label") \
                and s["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append(dict(s))
    return out


def _from_events(evs: list[dict], t1: datetime, classify) -> list[dict]:
    """Change events -> segments: each value holds until the next event."""
    segs = []
    for i, e in enumerate(evs):
        state = classify(e.get("value"))
        if state is None:
            continue
        end = evs[i + 1]["dt"] if i + 1 < len(evs) else t1
        lab = str(e.get("value") or "")
        segs.append({"start": e["dt"], "end": end, "state": state,
                     "label": lab if state in ("other",) or lab != state else None})
    return segs


def _sub_spans(config, subs: list[dict]) -> dict[str, list[dict]]:
    from photonscript.scheduler.phd2_analysis import sub_start_utc
    out: dict = {}
    for r in subs:
        st = sub_start_utc(r, config)
        exp = float(r.get("exp_s") or 0)
        if st is None or exp <= 0:
            continue
        verdict = ("rejected" if not r.get("passed_qa")
                   else "accepted" if r.get("reviewed") else "review")
        out.setdefault(r.get("rig") or RC16, []).append(
            {"start": st, "end": st + timedelta(seconds=exp), "file": r.get("file"),
             "verdict": verdict, "target": r.get("target"),
             "filter": r.get("filter")})
    return out


def timeline(config, date: str) -> dict:
    from photonscript.scheduler.runs import _load_subs
    from photonscript.shared import mount_log, night_events
    from photonscript.shared.rigs import rig_label

    lines = mount_log.load(config, date)
    events = night_events.load(config, date)
    spans = _sub_spans(config, _load_subs(config, date))
    stamps = [r["dt"] for r in lines] + [e["dt"] for e in events]
    for v in spans.values():
        stamps += [s["start"] for s in v] + [s["end"] for s in v]
    if not stamps:
        return {"date": date, "ok": False, "rows": [], "subs": {},
                "note": "no mount log, events or subs for this night"}
    t0, t1 = min(stamps), max(stamps)
    rows = []

    mseg = [{**s, "label": None} for s in mount_log.segments(lines, end=t1)]
    flips = [e for e in events if e.get("kind") == "instruction"
             and classify_instruction(e.get("value")) == "meridian flip"]
    for e in flips:
        nxt = next((x["dt"] for x in events if x["dt"] > e["dt"]
                    and x.get("rig") == e.get("rig")
                    and x.get("kind") == "instruction"), t1)
        mseg.append({"start": e["dt"], "end": nxt, "state": "meridian flip",
                     "label": None})
    rows.append({"id": "mount", "label": "Mount", "segments": _merge(mseg),
                 "source": "mount log" if lines else None})

    gev = [e for e in events if e.get("kind") == "guider"]
    gseg = _from_events(gev, t1, lambda v: GUIDER_STATES.get(str(v or "").lower(),
                                                             "other"))
    rms = [e for e in events if e.get("kind") == "rms"]
    for s in gseg:
        if s["state"] == "guiding":
            vals = sorted(e["value"] for e in rms
                          if s["start"] <= e["dt"] <= s["end"]
                          and e.get("value") is not None)
            if vals:
                u = next((e.get("units") for e in rms), "px")
                med = vals[len(vals) // 2]
                s["label"] = (f'RMS {med:.2f}"' if u == "arcsec"
                              else f"RMS {med:.2f} px (guide px, PS-70)")
    rows.append({"id": "guider", "label": "Guider", "segments": _merge(gseg),
                 "source": "PHD2 events" if gev else None})

    for rig in (RC16, PIGGY):
        iev = [e for e in events if e.get("kind") == "instruction"
               and (e.get("rig") or RC16) == rig]
        segs = _from_events(iev, t1, classify_instruction)
        if not segs and rig not in spans:
            continue
        try:
            label = rig_label(config, rig)
        except Exception:  # noqa: BLE001
            label = rig
        rows.append({"id": rig, "label": label, "segments": _merge(segs),
                     "source": "NINA sequence" if iev else "sub spans only"})

    try:
        from photonscript.shared.safety_history import unsafe_windows
        wins, src = unsafe_windows(config, t0, t1)
        sseg = [{"start": max(a, t0), "end": min(b, t1), "state": "unsafe",
                 "label": None} for a, b in (wins or [])]
        if sseg:
            rows.append({"id": "safety", "label": "Safety", "segments":
                         _merge(sseg), "source": src})
    except Exception as e:  # noqa: BLE001
        logger.debug("timeline safety windows skipped: %s", e)

    def ser(seg):
        return {"start": _iso(seg["start"]), "end": _iso(seg["end"]),
                "state": seg["state"], "label": seg.get("label")}
    return {"date": date, "ok": True, "t0": _iso(t0), "t1": _iso(t1),
            "rows": [{**r, "segments": [ser(s) for s in r["segments"]]}
                     for r in rows],
            "subs": {rig: [{**s, "start": _iso(s["start"]), "end": _iso(s["end"])}
                           for s in v] for rig, v in spans.items()}}
