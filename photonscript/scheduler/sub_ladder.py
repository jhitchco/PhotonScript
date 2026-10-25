"""Sub-length ladder: each rig finds its own longest good sub length per
night, guided or unguided (night pilot action, 2026-10-25).

2026-10-24/25: the Piggy-600 (1.29"/px, FWHM ~6.5") at 120 s unguided with
the new TPoint model + ProTrack drifted ~1.7"/min with ecc ~0.34 (round);
300 s and 400 s kept well on good nights before, 450 s failed after a
meridian flip with an east-only model. The RC16 (0.236"/px) shows
exposure-independent optical elongation (ecc ~0.6, sub doctor "optics"), so
longer subs never help it. Jeremy set the Piggy OSC to 180 s by hand; the
ladder does that from the subs themselves.

Per rig and target, over tonight's subs (runs/<night>_subs.jsonl rows):

    step up     the last night_pilot_ladder_up_subs (6) subs at the current
                length, in a row, all accepted (passed_qa) with ecc under the
                rig's limit (night_pilot_ladder_ecc_<rig>: RC16 0.5,
                Piggy-600 0.45): the next rung of night_pilot_ladder_<rig>
                (RC16 "120,180,240,300", Piggy-600 "120,180,240,300,450")
    step down   at a rung the ladder stepped to, night_pilot_ladder_down_fails
                (3) of the last night_pilot_ladder_down_window (5) subs fail
                on ecc / trail: back to the rung it came from, and the rig
                holds there for the rest of the night

Never a step up when (blocks(), each logged once):
    * the sub doctor read the rig's elongation as optical tonight (any
      confidence): longer subs cannot be judged on round-ness then
    * the window's stars are too few (night_pilot_ladder_min_stars), or the
      frames are narrowband (Ha / OIII / SII) with fewer than
      night_pilot_ladder_nb_min_stars
    * the sky background is at, or at the next rung would be over,
      night_pilot_ladder_sky_frac of the rig's saturation ADU (moon / sky
      limited: a longer sub only buys sky)
    * the next rung has no darks: neither in the library nor in the night
      dark quota closed-roof time fills (night_pilot.dark_match "matched")
    * the rig holds (stepped down tonight), the pilot shortened its subs
      tonight, or the current length is the top rung

The night pilot (night_pilot.ladder) applies a step the way its other sub
length actions do: a tonight-only, ledgered goal override (RC16: the
filters shot at the old rung; Piggy-600: the "OSC" key) reverted at dawn,
then the RC16 restarts after the current sub, the Piggy-600 companion is
stopped after its sub and re-dispatched with the new length.
night_pilot_ladder_mode off | advise (default: "would step to N s" in the
storyline and the run events, nothing changes) | act; night_pilot_mode
advise caps it.

The night's best proven rung per rig and target is kept in
<data_dir>/night_pilot/ladder_learned.json and named in the next plan
heads-up as a suggestion (never applied).

Pure functions plus the learned-rung file; no NINA, PHD2 or store calls.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("off", "advise", "act")
RIGS = ("rc16", "piggyback")
NB_FILTERS = ("ha", "oiii", "sii", "h", "o", "s")
DEFAULT_RUNGS = {"rc16": "120,180,240,300", "piggyback": "120,180,240,300,450"}
DEFAULT_ECC = {"rc16": 0.5, "piggyback": 0.45}
LEARNED_FILE = "ladder_learned.json"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _f(cfg, key: str, default: float) -> float:
    try:
        v = float(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) else float(default)


def _i(cfg, key: str, default: int) -> int:
    try:
        return int(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return int(default)


def own_mode(cfg) -> str:
    m = str(getattr(cfg, "night_pilot_ladder_mode", "advise") or "advise").strip().lower()
    return m if m in MODES else "advise"


def mode(cfg) -> str:
    """The ladder's effective mode: its own switch, capped by
    night_pilot_mode (off there = off, advise there = at most advise)."""
    pm = str(getattr(cfg, "night_pilot_mode", "act") or "act").strip().lower()
    m = own_mode(cfg)
    if pm == "off" or m == "off":
        return "off"
    if pm == "advise":
        return "advise"
    return m


def rungs(cfg, rig: str) -> list[float]:
    """night_pilot_ladder_<rig> as seconds, shortest first."""
    raw = str(getattr(cfg, f"night_pilot_ladder_{rig}", DEFAULT_RUNGS.get(rig, ""))
              or "")
    out = set()
    for x in raw.replace(";", ",").split(","):
        try:
            v = float(x.strip())
        except ValueError:
            continue
        if math.isfinite(v) and v > 0:
            out.add(v)
    if not out:
        out = {float(x) for x in DEFAULT_RUNGS.get(rig, "120").split(",")}
    return sorted(out)


def ecc_max(cfg, rig: str) -> float:
    return _f(cfg, f"night_pilot_ladder_ecc_{rig}", DEFAULT_ECC.get(rig, 0.5))


def limits(cfg) -> dict:
    return {"up_subs": max(1, _i(cfg, "night_pilot_ladder_up_subs", 6)),
            "down_fails": max(1, _i(cfg, "night_pilot_ladder_down_fails", 3)),
            "down_window": max(1, _i(cfg, "night_pilot_ladder_down_window", 5)),
            "min_stars": max(0, _i(cfg, "night_pilot_ladder_min_stars", 30)),
            "nb_min_stars": max(0, _i(cfg, "night_pilot_ladder_nb_min_stars", 100)),
            "sky_frac": max(0.0, _f(cfg, "night_pilot_ladder_sky_frac", 0.25))}


def saturation_adu(cfg, rig: str) -> float:
    v = None
    if rig == "piggyback":
        try:
            from photonscript.shared.rigs import PIGGYBACK, rig_config
            v = getattr(rig_config(cfg, PIGGYBACK), "qa_saturation_adu", None)
        except Exception:  # noqa: BLE001
            v = None
    try:
        v = float(v if v is not None else getattr(cfg, "qa_saturation_adu", 65000.0))
    except (TypeError, ValueError):
        v = 65000.0
    return v if v > 0 else 65000.0


def next_rung(cfg, rig: str, cur: float | None) -> float | None:
    """The next longer rung above cur, None at (or over) the top."""
    if cur is None:
        return None
    up = [r for r in rungs(cfg, rig) if r > cur + 0.5]
    return up[0] if up else None


# --------------------------------------------------------------------------
# the subs
# --------------------------------------------------------------------------

def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _med(xs, nd=2):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), nd) if xs else None


def _exp(r: dict) -> float | None:
    return _num(r.get("exp_s") or r.get("exposure"))


def _same(a, b) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) < 0.5


def _tkey(name) -> str:
    try:
        from photonscript.shared.target_names import target_key
        return target_key(name)
    except Exception:  # noqa: BLE001
        return str(name or "").strip().lower()


def _state(r: dict) -> dict:
    from photonscript.scheduler import sub_pairs as sp
    return sp.sub_state(r)


def rig_lights(subs: list[dict], rig: str) -> list[dict]:
    """The rig's measured lights, oldest first (test and excluded subs
    left out: a roof-closed or slew-straddled sub says nothing about the
    sub length)."""
    from photonscript.scheduler import sub_pairs as sp
    from photonscript.shared.sub_time import sub_start
    out = []
    for r in subs or []:
        if (r.get("rig") or "rc16") != rig or sp.is_test(r) or not _exp(r):
            continue
        parts = str(r.get("file") or "").replace("\\", "/").upper().split("/")
        if any(p in ("FLAT", "FLATS", "DARK", "DARKS", "BIAS", "BIASES") for p in parts):
            continue
        if sub_start(r) is None or _state(r).get("excluded"):
            continue
        out.append(r)
    out.sort(key=sub_start)
    return out


def _bad_ecc(cfg, rig: str, r: dict) -> bool:
    e = _num(r.get("ecc"))
    st = _state(r)
    return bool(st.get("trailing")) or (e is not None and e >= ecc_max(cfg, rig))


def _round_ok(cfg, rig: str, r: dict) -> bool:
    e = _num(r.get("ecc"))
    return (r.get("passed_qa") is True and e is not None and e < ecc_max(cfg, rig)
            and not _state(r).get("trailing"))


def is_narrowband(filt) -> bool:
    return str(filt or "").strip().lower() in NB_FILTERS


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def evaluate(cfg, rig: str, subs: list[dict], *, since: datetime | None = None,
             last_up: dict | None = None, held: dict | None = None,
             optics: bool = False, shortened: bool = False,
             dark_status=None) -> dict:
    """One rig's ladder decision over tonight's subs.

    since: subs before this (the last applied step) do not count.
    last_up: the ladder's last applied step up on this rig ({target,
    from_s, to_s}); a step down is only ever back from such a step.
    held / optics / shortened: the night's blocks. dark_status: test hook
    (seconds -> night_pilot.dark_match dict).

    {"kind": "up" | "down" | "blocked" | "wait" | "none", "rig", "target",
    "from_s", "to_s", "why", "n", "ecc", "filters", "proven_s"}. proven_s:
    the rung the window proved round (for the learned rung), else None."""
    from photonscript.shared.sub_time import sub_start
    lim = limits(cfg)
    rows = rig_lights(subs, rig)
    if since is not None:
        rows = [r for r in rows if sub_start(r) >= since]
    out = {"kind": "none", "rig": rig, "target": None, "from_s": None, "to_s": None,
           "why": "", "n": 0, "ecc": None, "filters": [], "proven_s": None}
    if not rows:
        out["why"] = "no subs yet"
        return out
    newest = rows[-1]
    target = newest.get("target")
    cur = _exp(newest)
    tk = _tkey(target)
    mine = [r for r in rows if _tkey(r.get("target")) == tk]
    at_cur = [r for r in mine if _same(_exp(r), cur)]
    out.update(target=target, from_s=cur)

    # step down: back from a rung the ladder stepped this target to
    if last_up and _same(last_up.get("to_s"), cur) and \
            _tkey(last_up.get("target")) == tk:
        win = at_cur[-lim["down_window"]:]
        fails = [r for r in win if _bad_ecc(cfg, rig, r)]
        if len(fails) >= lim["down_fails"]:
            out.update(kind="down", to_s=float(last_up["from_s"]), n=len(win),
                       ecc=_med([_num(r.get("ecc")) for r in fails]),
                       filters=sorted({str(r.get("filter")) for r in at_cur if r.get("filter")}),
                       why=f"{len(fails)} of the last {len(win)} subs at {cur:g} s "
                           f"fail on ecc / trail (limit {ecc_max(cfg, rig):g})",
                       proven_s=float(last_up["from_s"]))
            return out

    win = mine[-lim["up_subs"]:]
    if len(win) < lim["up_subs"] or not all(_same(_exp(r), cur) for r in win):
        out.update(kind="wait", n=len([r for r in win if _same(_exp(r), cur)]),
                   why=f"{len(at_cur[-lim['up_subs']:])} of {lim['up_subs']} subs "
                       f"at {cur:g} s so far")
        return out
    bad = [r for r in win if not _round_ok(cfg, rig, r)]
    ecc = _med([_num(r.get("ecc")) for r in win])
    out.update(n=len(win), ecc=ecc,
               filters=sorted({str(r.get("filter")) for r in win if r.get("filter")}))
    if bad:
        out.update(kind="wait", why=f"{len(bad)} of the last {len(win)} subs at "
                   f"{cur:g} s not accepted or not round (ecc limit "
                   f"{ecc_max(cfg, rig):g})")
        return out
    out["proven_s"] = cur
    nxt = next_rung(cfg, rig, cur)

    def blocked(why: str) -> dict:
        out.update(kind="blocked", to_s=nxt, why=why)
        return out

    if held:
        return blocked(f"holding at {held.get('at_s'):g} s tonight "
                       f"({held.get('why')})")
    if optics:
        return blocked("the sub doctor reads this rig's elongation as optical "
                       "tonight: longer subs are not judged on round stars")
    if shortened:
        return blocked("the pilot shortened this rig's subs tonight (trailing)")
    if nxt is None:
        out.update(kind="none", why=f"{cur:g} s is the top rung "
                   f"({','.join(f'{r:g}' for r in rungs(cfg, rig))})")
        return out
    stars = _med([_num(r.get("stars")) for r in win], 0)
    stars = int(stars) if stars is not None else None
    nb = any(is_narrowband(r.get("filter")) for r in win)
    if nb and (stars is None or stars < lim["nb_min_stars"]):
        return blocked(f"narrowband frames with {stars if stars is not None else 'no'} "
                       f"stars (need {lim['nb_min_stars']}): ecc not trusted")
    if stars is None or stars < lim["min_stars"]:
        return blocked(f"{stars if stars is not None else 'no'} stars "
                       f"(need {lim['min_stars']}): ecc not trusted")
    bg = _med([_num(r.get("background")) for r in win], 0)
    sat = saturation_adu(cfg, rig)
    if bg is not None and lim["sky_frac"] > 0:
        cap = lim["sky_frac"] * sat
        if bg >= cap:
            return blocked(f"sky-limited: background {bg:g} ADU at {cur:g} s is over "
                           f"{lim['sky_frac']:g} of saturation ({cap:g} ADU)")
        if bg * nxt / cur > cap:
            return blocked(f"sky-limited at {nxt:g} s: background {bg:g} ADU at "
                           f"{cur:g} s would be ~{bg * nxt / cur:.0f} ADU, over "
                           f"{lim['sky_frac']:g} of saturation ({cap:g} ADU)")
    dk = dark_info(cfg, rig, nxt, dark_status)
    if dk.get("match") != "matched":
        return blocked(f"no {nxt:g} s darks in the library or the night dark "
                       f"quota ({dk.get('text') or dk.get('match')})")
    out.update(kind="up", to_s=nxt, darks_text=dk.get("text"),
               why=f"{len(win)} round subs at {cur:g} s, ecc {ecc}")
    return out


def dark_info(cfg, rig: str, s: float, status=None) -> dict:
    try:
        if status is not None:
            return status(s) or {"match": "none", "text": ""}
        from photonscript.scheduler.night_pilot import dark_match
        return dark_match(cfg, rig, s)
    except Exception as e:  # noqa: BLE001
        return {"match": "none", "text": f"darks unknown: {e}"}


# --------------------------------------------------------------------------
# texts
# --------------------------------------------------------------------------

def _label(cfg, rig: str) -> str:
    if rig == "piggyback":
        name = str(getattr(cfg, "piggyback_name", "Piggy-600") or "Piggy-600")
        return name.split("-")[0] if name.startswith("Piggy") else name
    return "RC16"


def step_text(cfg, dec: dict) -> str:
    """'Piggy stepped to 240 s (6 round subs at 180 s, ecc 0.33)'."""
    lab = _label(cfg, dec["rig"])
    if dec["kind"] == "down":
        return (f"{lab} stepped back to {dec['to_s']:g} s and holds tonight "
                f"({dec['why']}, ecc {dec.get('ecc')})")
    return (f"{lab} stepped to {dec['to_s']:g} s ({dec.get('n')} round subs at "
            f"{dec['from_s']:g} s, ecc {dec.get('ecc')})")


def would_text(cfg, dec: dict) -> str:
    lab = _label(cfg, dec["rig"])
    tgt = f" on {dec['target']}" if dec.get("target") else ""
    if dec["kind"] == "down":
        return (f"{lab} would step back to {dec['to_s']:g} s{tgt} and hold "
                f"({dec['why']})")
    return (f"{lab} would step to {dec['to_s']:g} s{tgt} ({dec.get('n')} round "
            f"subs at {dec['from_s']:g} s, ecc {dec.get('ecc')})")


# --------------------------------------------------------------------------
# the learned rung (a suggestion for the next night)
# --------------------------------------------------------------------------

def learned_path(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "night_pilot" / LEARNED_FILE


def load_learned(cfg) -> dict:
    try:
        d = json.loads(learned_path(cfg).read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("rigs", {})
            return d
    except (OSError, ValueError):
        pass
    return {"rigs": {}}


def _save_learned(cfg, d: dict) -> None:
    p = learned_path(cfg)
    try:
        from photonscript.shared.fileops import replace_retry
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
        replace_retry(tmp, p)
    except OSError as e:
        logger.warning("sub ladder learned rung not saved: %s", e)


def record_best(cfg, night: str, rig: str, target: str | None, rung_s: float,
                *, ecc=None, n: int = 0, failed_s: float | None = None,
                now: datetime | None = None) -> bool:
    """Keep the night's best proven rung for (rig, target): the highest
    rung proven tonight; a new night replaces an older one. failed_s: the
    rung that failed (a step down). True when the file changed."""
    if not target or not rung_s:
        return False
    d = load_learned(cfg)
    rigs = d.setdefault("rigs", {})
    per = rigs.setdefault(rig, {})
    k = _tkey(target)
    e = per.get(k) or {}
    same_night = e.get("night") == night
    old = _num(e.get("rung_s")) if same_night else None
    # a rung is only ever recorded once proven (N round subs), and a step
    # down goes back to the highest proven one, so the best is the max
    keep_old = old is not None and old > float(rung_s)
    best = old if keep_old else float(rung_s)
    if failed_s is None and same_night:
        failed_s = _num(e.get("failed_s"))
    new = {"target": target, "rung_s": best, "night": night,
           "ecc": e.get("ecc") if keep_old else ecc,
           "n": e.get("n") if keep_old else n,
           "failed_s": failed_s,
           "t": (now or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")}
    keep = {x: e.get(x) for x in ("target", "rung_s", "night", "failed_s")}
    if keep == {x: new.get(x) for x in ("target", "rung_s", "night", "failed_s")}:
        return False
    per[k] = new
    _save_learned(cfg, d)
    return True


def learned_for(cfg, rig: str, target: str | None) -> dict | None:
    if not target:
        return None
    return (load_learned(cfg).get("rigs", {}).get(rig) or {}).get(_tkey(target))


def headsup_line(cfg, plan: dict | None) -> str | None:
    """One plan heads-up line: tonight's targets with a learned rung
    ("Sub ladder learned: M31 Piggy 240 s, RC16 180 s (2026-10-25); a
    suggestion, tonight starts at the planned lengths"). None without any."""
    try:
        names = []
        for r in (plan or {}).get("schedule") or []:
            if isinstance(r, dict) and r.get("name") and not r.get("fill"):
                names.append(str(r["name"]))
        for t in (plan or {}).get("targets") or []:
            n = t.get("name") if isinstance(t, dict) else t
            if n and str(n) not in names:
                names.append(str(n))
        rigs = load_learned(cfg).get("rigs") or {}
        bits, nights = [], set()
        for n in names:
            parts = []
            for rig in ("piggyback", "rc16"):
                e = (rigs.get(rig) or {}).get(_tkey(n))
                if e and _num(e.get("rung_s")):
                    tail = f" ({e['failed_s']:g} s failed)" if _num(e.get("failed_s")) else ""
                    parts.append(f"{_label(cfg, rig)} {float(e['rung_s']):g} s{tail}")
                    nights.add(str(e.get("night")))
            if parts:
                bits.append(f"{n} " + ", ".join(parts))
        if not bits:
            return None
        when = f" ({', '.join(sorted(nights))})" if nights else ""
        return ("Sub ladder learned: " + "; ".join(bits) + when
                + "; a suggestion, tonight starts at the planned lengths")
    except Exception as e:  # noqa: BLE001
        logger.debug("sub ladder heads-up line skipped: %s", e)
        return None
