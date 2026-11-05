"""PS-241: stop the Piggy-600 failed-autofocus loop.

Why: night 2026-11-04/05, from 20:52 local every Piggy-600 autofocus on
NINA #2 failed Hocus Focus validation ("AutoFocus failure (HfrRegression)",
"New focus point HFR 3.29 is significantly worse than original HFR 2.32";
Hocus Focus re-centers and re-tries once by itself, then "Instruction Run
Autofocus failed after 2 attempts ... ContinueOnError"), restored the old
focuser position, and the subs stayed sharp (HFR 1.9 to 2.5 px). NINA's AF
triggers (after time, after temperature change, after HFR increase) all
measure from the last SUCCESSFUL autofocus, so a failed AF resets none of
them: the trigger fired again right after the next sub. About 9 min of AF
per 3 min sub (four sweeps: 2 attempts x Hocus Focus's own retry), subs
every 12 to 21 min instead of 3, about 40 subs lost.

NINA has no "back off after a failed AF" setting, so the Piggy-600's
trigger runners carry a gate (the 2026-10-15 re-entry gate pattern,
scheduler/reentry.py): each AF trigger runs a small container

    "Piggy AF <trigger> (back-off gate)" = [gate ExternalScript, RunAutofocus]

The gate (deploy\\reentry-gate.cmd piggy_af --trigger=time|temp|hfr ->
`photonscript reentry-gate` -> POST /api/reentry/gate?kind=piggy_af) has
ErrorBehavior 1 (SkipInstructionSetOnError): on SKIP the script exits 1 and
NINA skips the RunAutofocus of that small container only; the light loop
goes on. RUN, the service down, a timeout or any error: exit 0, the AF runs
as before. The RunAutofocus in a trigger runner has Attempts 1 (Hocus Focus
already re-tries once inside a run), with or without the gate.

Decision (pure, `decide`), from NINA #2's own log on the scope PC (the
newest log of the piggyback rig, read backwards from its end):

    outcome lines   "BroadcastSuccessfulAutoFocusRun" = an AF succeeded;
                    "ERROR|SequenceItem.cs|Run|... Item: RunAutofocus" = an
                    AF attempt failed (a cancel is not counted); the
                    "New focus point HFR X ... original HFR Y" warning
                    before it is the reason.
    failures        tonight's failed attempts after the last success,
                    grouped into episodes (attempts under EPISODE_GAP_MIN
                    apart are one episode).
    SKIP            the newest episode ended under the back-off ago:
                    piggyback_af_backoff_min (60) x 2^(episodes - 1), at
                    most piggyback_af_backoff_max_min (240).
    RUN             no failure since the last good AF, the back-off is
                    over, or the HFR escape: the Piggy-600's graded lights
                    since the failure (the newest piggyback_af_hfr_sample_size)
                    have a median HFR more than piggyback_af_hfr_increase_pct
                    above the lights before the first failure (focus really
                    moved: refocus now). Log unreadable: RUN (fails open).

Alert: when tonight has at least piggyback_af_fail_alert (2) failed AF
episodes, one Pushover per night ("PhotonScript Piggy-600 autofocus"),
kind act, also in advise mode.

Mode `piggyback_af_backoff`: off (no gate, the old runner but Attempts 1) |
advise (gate in the sequence, decisions logged, always RUN) | on (default).
The gate needs the re-entry gate script on the machine
(`reentry_gate_script`); missing script = no gate.

Log: run events kind piggy_af_backoff (value run | skip | advise | alert),
`GET /api/piggy-af/backoff` (the decision now, tonight's events), morning
report line "Piggy AF back-off: 3 failed AF(s) (first 20:56), 25 triggered
AF(s) skipped (~112 min saved)".
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

logger = logging.getLogger(__name__)

KIND = "piggy_af_backoff"        # run event kind
GATE_KIND = "piggy_af"           # the reentry-gate.cmd kind argument
MODES = ("off", "advise", "on")
TRIGGERS = ("time", "temp", "hfr")
TRIGGER_AF_ATTEMPTS = 1          # Hocus Focus re-tries once inside a run
GATE_ERROR_BEHAVIOR = 1          # SkipInstructionSetOnError (see reentry)
CONTAINER_SUFFIX = " (back-off gate)"
EPISODE_GAP_MIN = 12.0           # attempts closer than this = one episode
SAVED_S = 270.0                  # one Hocus Focus run with its own retry
TITLE = "PhotonScript Piggy-600 autofocus"
SCAN_BYTES = 48 * 1024 * 1024    # how far back the log is read at most

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_HFR_WHY = re.compile(r"New focus point HFR ([\d.]+) is significantly worse "
                      r"than original HFR ([\d.]+)")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def mode(cfg) -> str:
    v = str(getattr(cfg, "piggyback_af_backoff", "on") or "on").strip().lower()
    return v if v in MODES else "on"


def _f(cfg, key: str, default: float) -> float:
    try:
        v = float(getattr(cfg, key, default))
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def limits(cfg) -> dict:
    base = max(1.0, _f(cfg, "piggyback_af_backoff_min", 60.0))
    return {"base_min": base,
            "max_min": max(base, _f(cfg, "piggyback_af_backoff_max_min", 240.0)),
            "alert_n": max(1, int(_f(cfg, "piggyback_af_fail_alert", 2))),
            "hfr_pct": max(1.0, _f(cfg, "piggyback_af_hfr_increase_pct", 10.0)),
            "samples": max(3, int(_f(cfg, "piggyback_af_hfr_sample_size", 4)))}


def gate_script(cfg) -> str | None:
    """The gate script path when the Piggy-600's AF triggers should carry
    the back-off gate: mode not off and the re-entry gate script exists on
    this machine (the armer generates on the scope PC)."""
    if mode(cfg) == "off":
        return None
    path = str(getattr(cfg, "reentry_gate_script", "") or "").strip()
    if not path:
        return None
    try:
        return path if Path(path).is_file() else None
    except OSError:
        return None


def script_args(trigger: str) -> str:
    """Arguments after the script path (deploy/reentry-gate.cmd)."""
    t = trigger if trigger in TRIGGERS else "time"
    return f'{GATE_KIND} --label="Piggy-600" --trigger={t}'


def container_name(trigger: str) -> str:
    return f"Piggy AF {trigger}{CONTAINER_SUFFIX}"


# --------------------------------------------------------------------------
# NINA #2 log -> AF outcomes
# --------------------------------------------------------------------------

def _line_time(line: str) -> datetime | None:
    m = _TS.match(line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def parse_outcomes(lines_newest_first, since: datetime | None = None) -> list[dict]:
    """AF outcomes from NINA log lines given newest first (iter_reverse):
    [{"t": local naive, "ok": bool, "why": str}] oldest first. Stops at
    the first line older than `since`."""
    out: list[dict] = []
    later = ""            # the line after this one in the file
    pending_fail = None   # a failure whose reason (an earlier line) is next
    for line in lines_newest_first:
        t = _line_time(line)
        if t is None:
            later = line
            continue
        if since is not None and t < since:
            break
        if "BroadcastSuccessfulAutoFocusRun" in line:
            pending_fail = None
            out.append({"t": t, "ok": True, "why": ""})
        elif ("|ERROR|SequenceItem.cs|Run|" in line
              and "Item: RunAutofocus" in line
              and "cancel" not in later.lower()):
            pending_fail = {"t": t, "ok": False, "why": "autofocus failed"}
            out.append(pending_fail)
        elif pending_fail is not None:
            m = _HFR_WHY.search(line)
            if m:
                pending_fail["why"] = (f"HFR regression: new focus HFR "
                                       f"{float(m.group(1)):.2f} vs "
                                       f"{float(m.group(2)):.2f} before")
                pending_fail = None
            elif "BroadcastAutoFocusRunStarting" in line:
                pending_fail = None
        later = line
    out.reverse()
    return out


def episodes(fails: list[dict], gap_min: float = EPISODE_GAP_MIN) -> list[list[dict]]:
    """Failed attempts (oldest first) grouped: attempts under gap_min apart
    belong to one episode (NINA's Attempts 2 or a back-to-back loop)."""
    eps: list[list[dict]] = []
    for f in fails:
        if eps and (f["t"] - eps[-1][-1]["t"]).total_seconds() / 60.0 < gap_min:
            eps[-1].append(f)
        else:
            eps.append([f])
    return eps


def night_start_local(cfg, now_local: datetime) -> datetime:
    """Local noon that opened the night `now_local` is in."""
    d = now_local - timedelta(hours=12)
    return datetime(d.year, d.month, d.day, 12)


def piggy_log_path(cfg) -> Path | None:
    try:
        from photonscript.scheduler.routers.triage import _rig_log
        p, _note = _rig_log(cfg, "piggyback")
        return Path(p) if p else None
    except Exception as e:  # noqa: BLE001
        logger.debug("piggy AF back-off: no NINA #2 log: %s", e)
        return None


def read_outcomes(cfg, now_local: datetime, path: Path | None = None) -> list[dict] | None:
    """Tonight's AF outcomes from NINA #2's newest log, or None when the
    log cannot be read (the gate then answers RUN)."""
    from photonscript.scheduler.log_files import iter_reverse
    path = path or piggy_log_path(cfg)
    if path is None:
        return None
    try:
        return parse_outcomes(iter_reverse(path, SCAN_BYTES),
                              since=night_start_local(cfg, now_local))
    except OSError as e:
        logger.debug("piggy AF back-off: %s unreadable: %s", path, e)
        return None


# --------------------------------------------------------------------------
# HFR escape (the Piggy-600's graded lights)
# --------------------------------------------------------------------------

def _sub_end_local(cfg, s: dict) -> datetime | None:
    from photonscript.shared.phd2_store import parse_z
    t = parse_z(s.get("end_utc") or s.get("time"))
    if t is None:
        return None
    if t.tzinfo is not None:
        t = t.replace(tzinfo=None)
    try:
        from photonscript.shared.localtime import to_local
        return to_local(cfg, t)
    except Exception:  # noqa: BLE001
        return None


def piggy_lights(cfg, subs) -> list[tuple[datetime, float, float]]:
    """[(end local, hfr, exp_s)] of the Piggy-600's graded lights, oldest
    first."""
    out = []
    for s in subs or []:
        if (s.get("rig") or "rc16") != "piggyback":
            continue
        f = str(s.get("file") or "").replace("\\", "/").upper()
        if f and not f.startswith("LIGHT/"):
            continue
        try:
            h = float(s.get("hfr"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(h) or h <= 0:
            continue
        t = _sub_end_local(cfg, s)
        if t is None:
            continue
        out.append((t, h, float(s.get("exp_s") or 0.0)))
    out.sort(key=lambda r: r[0])
    return out


def hfr_rise(lights, first_fail: datetime, last_fail: datetime, samples: int,
             pct: float) -> tuple[bool, str]:
    """(True, why) when the newest `samples` lights after last_fail have a
    median HFR more than pct % above the newest `samples` before
    first_fail, compared at the exposure of the newest light."""
    after = [r for r in lights if r[0] > last_fail]
    if len(after) < samples:
        return False, ""
    exp = after[-1][2]
    after = [r for r in after if r[2] == exp][-samples:]
    before = [r for r in lights if r[0] < first_fail and r[2] == exp][-samples:]
    if len(after) < samples or len(before) < min(3, samples):
        return False, ""
    a, b = median(r[1] for r in after), median(r[1] for r in before)
    if b > 0 and a > b * (1.0 + pct / 100.0):
        return True, (f"Piggy HFR rose {100.0 * (a / b - 1.0):.0f}% "
                      f"({b:.2f} -> {a:.2f} px) since the failed AF")
    return False, f"Piggy HFR steady ({b:.2f} -> {a:.2f} px)"


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def _hm(t: datetime | None) -> str:
    return t.strftime("%H:%M") if t else "?"


def decide(cfg, *, now_local: datetime, outcomes: list[dict] | None,
           trigger: str = "time", lights=None) -> dict:
    """{"verdict": SKIP | RUN, "why", "failed_episodes", "streak",
    "retry_at", "last_why", "alert"} (see the module doc). Pure."""
    lim = limits(cfg)
    out = {"verdict": "RUN", "trigger": trigger, "why": "",
           "failed_episodes": 0, "streak": 0, "retry_at": None,
           "last_why": None, "alert": False}
    if outcomes is None:
        out["why"] = "NINA #2 log unreadable: AF runs"
        return out
    fails_tonight = [o for o in outcomes if not o["ok"]]
    out["failed_episodes"] = len(episodes(fails_tonight))
    out["alert"] = out["failed_episodes"] >= lim["alert_n"]
    last_ok = max((o["t"] for o in outcomes if o["ok"]), default=None)
    streak = [o for o in fails_tonight if last_ok is None or o["t"] > last_ok]
    if not streak:
        out["why"] = ("no failed AF since the last good one"
                      + (f" ({_hm(last_ok)})" if last_ok else ""))
        return out
    eps = episodes(streak)
    out["streak"] = len(eps)
    last = streak[-1]
    out["last_why"] = last.get("why")
    wait = min(lim["max_min"], lim["base_min"] * 2 ** (len(eps) - 1))
    retry = last["t"] + timedelta(minutes=wait)
    out["retry_at"] = retry.isoformat(timespec="minutes")
    head = (f"{len(eps)} failed AF(s) since "
            + (f"the last good one at {_hm(last_ok)}" if last_ok else "dusk")
            + f", last {_hm(last['t'])} ({last.get('why') or 'failed'})")
    if now_local >= retry:
        out["why"] = f"{head}; back-off of {wait:.0f} min over: AF runs"
        return out
    rose, hwhy = hfr_rise(lights or [], streak[0]["t"], last["t"],
                          lim["samples"], lim["hfr_pct"])
    if rose:
        out["why"] = f"{head}; {hwhy}: AF runs"
        return out
    out["verdict"] = "SKIP"
    out["why"] = (f"{head}; holding {trigger}-triggered AFs {wait:.0f} min, "
                  f"next try {_hm(retry)}" + (f"; {hwhy}" if hwhy else ""))
    return out


# --------------------------------------------------------------------------
# the gate call (service side), alert, log
# --------------------------------------------------------------------------

def _now_local(cfg, now_utc: datetime) -> datetime:
    from photonscript.shared.localtime import to_local
    return to_local(cfg, now_utc)


def _load_lights(cfg, night: str):
    try:
        from photonscript.scheduler.runs import _load_subs
        return piggy_lights(cfg, _load_subs(cfg, night))
    except Exception:  # noqa: BLE001
        return []


def evaluate(cfg, trigger: str = "time", now: datetime | None = None,
             outcomes=None, lights=None) -> dict:
    """decide() with live inputs (blocking: reads the NINA #2 log tail and
    the night's subs). now: naive UTC."""
    from photonscript.shared.phd2_store import night_of
    now = now or datetime.utcnow()
    now_l = _now_local(cfg, now)
    if outcomes is None:
        outcomes = read_outcomes(cfg, now_l)
    if lights is None:
        lights = _load_lights(cfg, night_of(cfg, now))
    return decide(cfg, now_local=now_l, outcomes=outcomes, trigger=trigger,
                  lights=lights)


def _events(cfg, night: str) -> list[dict]:
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import read_jsonl
    return [r for r in read_jsonl(events_path(cfg, night))
            if r.get("kind") == KIND]


def alert_text(dec: dict) -> str:
    n = dec.get("failed_episodes") or 0
    why = dec.get("last_why") or "failed"
    retry = dec.get("retry_at")
    nxt = f" (next try {retry[11:16]})" if retry and len(retry) >= 16 else ""
    return (f"Piggy-600: autofocus failed {n} times tonight (last: {why}). "
            "NINA #2 keeps the last good focus and keeps shooting; "
            "PhotonScript now holds the triggered AFs after a failure "
            f"instead of re-running one after every sub{nxt}. Check the "
            "newest Piggy sub: sharp stars mean Hocus Focus validation is "
            "rejecting good fits (see the Piggy AF notes in the handbook).")


async def run_gate(cfg, trigger: str = "time", label: str = "",
                   now: datetime | None = None, notify=None,
                   outcomes=None, lights=None) -> dict:
    """One gate call from NINA #2. SKIP only in mode on. Never raises."""
    import asyncio
    now = now or datetime.utcnow()
    m = mode(cfg)
    trigger = trigger if trigger in TRIGGERS else "time"
    out = {"verdict": "RUN", "kind": GATE_KIND, "label": label or "Piggy-600",
           "trigger": trigger, "mode": m, "why": ""}
    try:
        if m == "off":
            out["why"] = "piggyback_af_backoff off"
        else:
            dec = await asyncio.to_thread(evaluate, cfg, trigger, now,
                                          outcomes, lights)
            out.update(dec)
            out["mode"] = m
            out["would_skip"] = dec["verdict"] == "SKIP"
            if m != "on":
                out["verdict"] = "RUN"
            if dec.get("alert"):
                await _maybe_alert(cfg, dec, now, notify)
    except Exception as e:  # noqa: BLE001
        logger.warning("piggy AF back-off gate failed: %s", e)
        out.update(verdict="RUN", why=f"error: {e}")
    if m == "advise" and out.get("would_skip"):
        out["why"] = "advise (would skip): " + out["why"]
    record(cfg, out, now)
    return out


async def _maybe_alert(cfg, dec: dict, now: datetime, notify=None) -> bool:
    """One Pushover per night once tonight has alert_n failed episodes."""
    from photonscript.shared.phd2_store import night_of
    night = night_of(cfg, now)
    if any(r.get("value") == "alert" for r in _events(cfg, night)):
        return False
    msg = alert_text(dec)
    _append(cfg, now, {"value": "alert", "detail": msg,
                       "failed_episodes": dec.get("failed_episodes")})
    if notify is None:
        from photonscript.shared.pushover import notify as _n
        notify = _n
    try:
        await notify(cfg, msg, title=TITLE, priority=0, kind="act")
    except Exception as e:  # noqa: BLE001
        logger.warning("piggy AF back-off push failed: %s", e)
    return True


def _append(cfg, now: datetime, fields: dict) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        append_jsonl(events_path(cfg, night_of(cfg, now)),
                     {"t": iso_z(now), "rig": "piggyback", "src": "photonscript",
                      "kind": KIND, **fields})
    except Exception as e:  # noqa: BLE001
        logger.warning("piggy AF back-off event not logged: %s", e)


def record(cfg, out: dict, now: datetime) -> None:
    """Run event kind piggy_af_backoff, value skip | advise | run."""
    if out.get("verdict") == "SKIP":
        value = "skip"
    elif out.get("would_skip") and out.get("mode") == "advise":
        value = "advise"
    else:
        value = "run"
    rec = {"value": value, "trigger": out.get("trigger"),
           "detail": out.get("why"),
           "saved_s": SAVED_S if value == "skip" else 0.0}
    for k in ("failed_episodes", "streak", "retry_at"):
        if out.get(k) is not None:
            rec[k] = out[k]
    _append(cfg, now, rec)
    (logger.info if value == "run" else logger.warning)(
        "piggy AF back-off gate (%s): %s (%s)", out.get("trigger"),
        out.get("verdict"), out.get("why"))


def morning_lines(cfg, date: str) -> list[str]:
    """One line for the night's Piggy AF back-off, or []."""
    rows = _events(cfg, date)
    if not rows:
        return []
    skips = [r for r in rows if r.get("value") == "skip"]
    adv = [r for r in rows if r.get("value") == "advise"]
    n_fail = max((int(r.get("failed_episodes") or 0) for r in rows), default=0)
    if not (skips or adv or n_fail):
        return []
    saved = sum(float(r.get("saved_s") or 0) for r in skips) / 60.0
    line = (f"Piggy AF back-off: {n_fail} failed AF(s); "
            f"{len(skips)} triggered AF(s) skipped (~{saved:.0f} min saved)")
    if adv:
        line += f"; advise: {len(adv)} would have skipped"
    return [line]
