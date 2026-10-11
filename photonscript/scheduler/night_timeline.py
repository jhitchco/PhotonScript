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


def timeline(config, date: str, raw: bool = False) -> dict:
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

    if raw:   # tonight_strip(): the same rows with datetimes
        return {"date": date, "ok": True, "t0": t0, "t1": t1, "rows": rows,
                "subs": spans}

    def ser(seg):
        return {"start": _iso(seg["start"]), "end": _iso(seg["end"]),
                "state": seg["state"], "label": seg.get("label")}
    return {"date": date, "ok": True, "t0": _iso(t0), "t1": _iso(t1),
            "rows": [{**r, "segments": [ser(s) for s in r["segments"]]}
                     for r in rows],
            "subs": {rig: [{**s, "start": _iso(s["start"]), "end": _iso(s["end"])}
                           for s in v] for rig, v in spans.items()}}


# ---------------------------------------------------------------------------
# Dashboard "Tonight so far" strip: the same rows, collapsed to a few states
# and clipped to tonight's sunset .. now (sunrise at the latest).
# ---------------------------------------------------------------------------

# A rig row on the strip has three states: exposing (lights), busy (what the
# rig does to get there: slew, center, autofocus, flip, guider start, dither,
# flats / darks, cooling) and idle (waiting, parked, tracking with nothing
# running). Gaps with no record read as idle on the page.
STRIP_IDLE = frozenset({"waiting", "parked", "idle", "tracking"})
SPAN_BRIDGE_S = 120


def strip_state(state: str | None) -> str:
    """A timeline state -> exposing | busy | idle."""
    if state == "exposing":
        return "exposing"
    if not state or state in STRIP_IDLE:
        return "idle"
    return "busy"


def _clip(segs: list[dict], a: datetime, b: datetime) -> list[dict]:
    out = []
    for s in segs:
        st, en = max(s["start"], a), min(s["end"], b)
        if en > st:
            out.append({**s, "start": st, "end": en})
    return out


def _collapse(segs: list[dict]) -> list[dict]:
    """Collapse to strip states and merge touching runs of one state. A busy
    or idle run's label lists the timeline states it covers (tooltip)."""
    out: list[dict] = []
    for s in sorted(segs, key=lambda x: x["start"]):
        st = strip_state(s["state"])
        if out and out[-1]["state"] == st and s["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
            if s["state"] not in out[-1]["parts"]:
                out[-1]["parts"].append(s["state"])
        else:
            out.append({"start": s["start"], "end": s["end"], "state": st,
                        "parts": [s["state"]]})
    for s in out:
        parts = s.pop("parts")
        s["label"] = (", ".join(parts)
                      if s["state"] != "exposing" and parts != [s["state"]]
                      else None)
    return out


def roof_segments(config, start: datetime, end: datetime) -> tuple[list, str]:
    """Roof open / closed / unknown over [start, end] from the safety history
    (PS-71): each recorded change holds until the next one, and the state
    recorded before `start` carries in (the armer writes changes only).
    Without a history file the Pushover audit fallback marks only the
    closed (paused) windows; the rest stays unknown."""
    from photonscript.shared import safety_history as sh
    ev, src = sh._history_events(config), "history"
    if not ev:
        ev = sh._notification_events(config)
        src = "notifications" if ev else "none"
    name = {"safe": "open", "unsafe": "closed", "unknown": "unknown"}
    if src == "notifications":
        name["safe"] = "unknown"
    cur, t, segs = "unknown", start, []
    for when, st in sorted(ev):
        if when <= start:
            cur = st
            continue
        if when >= end:
            break
        segs.append({"start": t, "end": when, "state": name[cur]})
        t, cur = when, st
    if end > t:
        segs.append({"start": t, "end": end, "state": name[cur]})
    merged: list[dict] = []
    for s in segs:
        if s["end"] <= s["start"]:
            continue
        if merged and merged[-1]["state"] == s["state"]:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    return merged, src


_night_times_cache: dict[str, dict] = {}


def _night_window(config, date: str) -> dict:
    """Sunset, astro dusk / dawn and sunrise (naive UTC) for a night date,
    cached per date (compute_night_times runs astropy)."""
    hit = _night_times_cache.get(date)
    if hit is None:
        from photonscript.scheduler.night_plan import compute_night_times
        try:
            hit = compute_night_times(config.get_observatory(),
                                      datetime.strptime(date, "%Y-%m-%d"))
        except Exception as e:  # noqa: BLE001
            logger.debug("night times for %s failed: %s", date, e)
            hit = {}
        if len(_night_times_cache) > 8:
            _night_times_cache.clear()
        _night_times_cache[date] = hit
    return hit


def _secs(segs: list[dict], state: str) -> int:
    return int(sum((s["end"] - s["start"]).total_seconds()
                   for s in segs if s["state"] == state))


def tonight_strip(config, date: str | None = None,
                  now: datetime | None = None) -> dict:
    """The dashboard strip for one night (default: the night `now` is in):
    a Roof row and one row per rig, clipped to sunset .. min(now, sunrise),
    per-rig sub ticks and the summary numbers. Read-only: it reuses
    timeline() and the safety history and computes nothing new."""
    from photonscript.shared.localtime import utc_offset_hours
    from photonscript.shared.phd2_store import night_of
    from photonscript.shared.rigs import rig_ids, rig_label
    now = now or datetime.utcnow()
    date = date or night_of(config, now)
    tw = _night_window(config, date)
    sunset, sunrise = tw.get("sunset"), tw.get("sunrise")
    dusk, dawn = tw.get("astro_dusk"), tw.get("astro_dawn")
    if not sunset or not sunrise:   # no twilight: 18:00 .. 07:00 local
        off = utc_offset_hours(config, now)
        base = datetime.strptime(date, "%Y-%m-%d") - timedelta(hours=off)
        sunset, sunrise = base + timedelta(hours=18), base + timedelta(hours=31)
    start = sunset
    if dusk:   # the armer's pre-config, if it ever falls before sunset
        lead = int(getattr(config, "arm_preconfig_lead_min", 30) or 30)
        start = min(start, dusk - timedelta(minutes=lead))
    end = sunrise
    dark_s = int((dawn - dusk).total_seconds()) if dusk and dawn else None
    out = {"date": date, "t0": _iso(start), "t1": _iso(end), "now": _iso(now),
           "sunset": _iso(sunset), "sunrise": _iso(sunrise),
           "astro_dusk": _iso(dusk), "astro_dawn": _iso(dawn),
           "tz_offset_h": utc_offset_hours(config, now), "dark_s": dark_s}
    if now <= start:
        return {**out, "ok": True, "started": False, "rows": [], "ticks": {},
                "summary": {}, "note": "tonight starts at sunset"}
    upto = min(now, end)

    tl = timeline(config, date, raw=True)
    by_id = {r["id"]: r for r in tl.get("rows") or []}
    roof, roof_src = roof_segments(config, start, upto)
    rows = [{"id": "roof", "label": "Roof", "segments": roof,
             "source": roof_src}]
    ticks: dict[str, list] = {}
    summary = {"roof_open_s": _secs(roof, "open"),
               "elapsed_s": int((upto - start).total_seconds()),
               "dark_s": dark_s, "rigs": {}, "subs": 0, "kept": 0}
    for rig in rig_ids(config):
        spans = (tl.get("subs") or {}).get(rig) or []
        row = by_id.get(rig)
        segs = list(row["segments"]) if row else []
        if not row or row.get("source") != "NINA sequence":
            # no NINA instruction log: the sub spans are the exposing time
            # (download / dither gaps up to SPAN_BRIDGE_S between subs are
            # bridged so a sub run reads as one bar, not a comb)
            ss = sorted(spans, key=lambda s: s["start"])
            for i, s in enumerate(ss):
                e = s["end"]
                if i + 1 < len(ss) and timedelta(0) < ss[i + 1]["start"] - e \
                        <= timedelta(seconds=SPAN_BRIDGE_S):
                    e = ss[i + 1]["start"]
                segs.append({"start": s["start"], "end": e, "state": "exposing"})
        segs = _collapse(_clip(segs, start, upto))
        try:
            label = rig_label(config, rig)
        except Exception:  # noqa: BLE001
            label = rig
        rows.append({"id": rig, "label": label, "segments": segs,
                     "source": (row or {}).get("source") or "none"})
        tk = [{"t": _iso(s["end"]), "kept": s.get("verdict") != "rejected",
               "target": s.get("target"), "filter": s.get("filter")}
              for s in spans if start <= s["end"] <= upto]
        kept = sum(1 for t in tk if t["kept"])
        ticks[rig] = tk
        summary["rigs"][rig] = {"label": label,
                                "exposing_s": _secs(segs, "exposing"),
                                "busy_s": _secs(segs, "busy"),
                                "subs": len(tk), "kept": kept}
        summary["subs"] += len(tk)
        summary["kept"] += kept

    def ser(s):
        return {"start": _iso(s["start"]), "end": _iso(s["end"]),
                "state": s["state"], "label": s.get("label")}
    started = bool(tl.get("ok"))
    return {**out, "ok": True, "started": started, "upto": _iso(upto),
            "rows": [{**r, "segments": [ser(s) for s in r["segments"]]}
                     for r in rows],
            "ticks": ticks, "summary": summary,
            "note": None if started else "no run yet tonight"}
