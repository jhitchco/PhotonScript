"""Pushover notifications for supervisor/nanny alerts.

(In-sequence notifications come from NINA's GroundStation plugin; these are
the out-of-band alerts from the telescope agent itself.)

Rate limiting (added 2026-09-12 after a safe/unsafe flap emitted a push on
every 30 s tick and burned through the 10,000/mo Pushover cap):

  * identical-message dedup   - drop the same (title, message) inside
                                pushover_dedup_window_s (default 300 s).
                                This alone collapses a RUNNING<->PAUSED flap.
  * rolling hourly burst cap  - at most pushover_max_per_hour messages in any
                                60-minute window; excess is suppressed and
                                counted, then summarised on the next message
                                that gets through ("[+N suppressed]").
  * hard monthly cap          - stop sending once pushover_monthly_cap is
                                reached in the calendar month (default 9000,
                                headroom under Pushover's 10k). One final
                                high-priority notice is sent when the cap trips.

  * quiet daytime (PS-50)     - while the sun is up at the observatory,
                                heartbeats are dropped and each title sends at
                                most once per pushover_daytime_title_window_h.

Emergency messages (priority >= 2) bypass the hourly burst cap but still count
toward (and are still stopped by) the monthly cap. Everything is best-effort:
any limiter error falls through to sending. Set pushover_ratelimit_enabled
False to restore the old unconditional behaviour.

Pushover v2 (PS-212, 2026-10-24): a night storyline instead of event noise.
Every notify() carries a kind, decided BEFORE the limiter above:

  * act    needs Jeremy. Pushed (priority >= 1) before
           pushover_act_cutoff_local ("00:00"); from the cutoff until
           pushover_act_resume_local ("08:00") only with safety=True (unsafe
           while not parked, camera / cooler dead after recovery, disk or
           sync failure, arm / deploy blocked at night, a test push). A held
           act stays in the storyline and the end-of-night report lists it.
  * story  the night's milestones (plan heads-up, imaging now, first light,
           target switch, roof reopened, flats, end of night). Pushed at
           priority 0, once per fact per night.
  * info   everything else: logged to the storyline only, never pushed.

Callers without a kind default to info (opt in to noise, not out).
pushover_kinds ("act,story") turns a kind's pushes off (a safety act is
always sent). A story or non-safety act is pushed once per fact per night:
fact= names it, else the title and the message with digits masked.
Every decision, pushed or not, is appended to the night storyline
(<data_dir>/night_story/<night>.jsonl), which GET /api/night/story and the
dashboard's "Tonight" panel read (scheduler/night_story.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API = "https://api.pushover.net/1/messages.json"

# Defaults used when the config object does not define the knob.
_DEFAULTS = {
    "pushover_ratelimit_enabled": True,
    "pushover_dedup_window_s": 300,
    "pushover_max_per_hour": 20,
    "pushover_monthly_cap": 9000,
    "pushover_quiet_daytime": True,
    "pushover_daytime_title_window_h": 4.0,
    "pushover_daytime_sun_alt_deg": -3.0,
    "pushover_emergency_retry_s": 300,
    "pushover_emergency_expire_s": 1800,
    # PS-212 storyline policy
    "pushover_kinds": "act,story",
    "pushover_act_cutoff_local": "00:00",
    "pushover_act_resume_local": "08:00",
}

# PS-212: the three message kinds (see the module docstring).
KINDS = ("act", "story", "info")
ACT, STORY, INFO = KINDS

_lock = asyncio.Lock()

# Message characters kept in notifications.jsonl: a whole Pushover message
# (1024) plus the limiter's suppressed note. Was 500 until 2026-10-12 (PS-166:
# the stored "PhotonScript complete" text read as cut off at 500).
AUDIT_MESSAGE_CHARS = 1100


def _cfg(config, key):
    return getattr(config, key, _DEFAULTS[key])


# Push text is plain ASCII (2026-10-12: the dawn "PhotonScript complete"
# push showed "A-circumflex, middle dot" where " U+00B7 " separated the
# shutdown steps.
# PhotonScript stored and posted correct UTF-8, notifications.jsonl reads
# U+00B7, so the UTF-8 bytes were read as Latin-1 / cp1252 after the form
# POST. ASCII cannot be misread.) Typography first, then any accent
# stripped (NFKD), anything else left becomes "?".
_ASCII_MAP = {
    "\u00b7": "|", "\u2022": "*", "\u2014": "-", "\u2013": "-", "\u2012": "-",
    "\u2010": "-", "\u2011": "-", "\u2212": "-", "\u2018": "'", "\u2019": "'",
    "\u201a": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2026": "...",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u00b0": " deg", "\u00b1": "+/-",
    "\u00d7": "x", "\u00b5": "u", "\u03bc": "u", "\u2192": "->", "\u2190": "<-",
    "\u2264": "<=", "\u2265": ">=", "\u2248": "~", "\u2032": "'", "\u2033": '"',
    "\u26a0": "!", "\u2713": "ok", "\u2714": "ok", "\u2717": "x", "\u2718": "x",
    "\ufe0f": "",
}


def ascii_text(text) -> str:
    """text as plain ASCII for a push (see _ASCII_MAP); None -> ""."""
    import unicodedata
    s = "" if text is None else str(text)
    if s.isascii():
        return s
    s = "".join(_ASCII_MAP.get(ch, ch) for ch in s)
    if s.isascii():
        return s
    out = []
    for ch in s:
        if ch.isascii():
            out.append(ch)
            continue
        base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
        out.append(base or "?")
    return "".join(out)


def _state_path(config) -> Path:
    data_dir = Path(getattr(config, "data_dir", Path.home() / ".photonscript"))
    return data_dir / "pushover_state.json"


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - missing/corrupt => fresh state
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except Exception as e:  # noqa: BLE001
        logger.warning("Pushover state save failed: %s", e)


def _audit_path(config) -> Path:
    data_dir = Path(getattr(config, "data_dir", Path.home() / ".photonscript"))
    return data_dir / "notifications.jsonl"


def _audit(config, title: str, message: str, priority: int, sent: bool,
           reason: str, kind: str | None = None) -> None:
    """Append every notification DECISION (sent or suppressed) to
    notifications.jsonl so alert volume is auditable later: nothing was logged
    for *sent* alerts before, so there was no trail. Best-effort; append-only,
    trimmed to the last ~5000 lines when it grows past ~4 MB."""
    try:
        p = _audit_path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(),
               "title": title, "message": (message or "")[:AUDIT_MESSAGE_CHARS],
               "priority": int(priority), "sent": bool(sent), "reason": reason}
        if kind:
            rec["kind"] = kind
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        try:
            if p.stat().st_size > 4_000_000:
                tail = p.read_text(encoding="utf-8",
                                   errors="replace").splitlines()[-5000:]
                p.write_text("\n".join(tail) + "\n", encoding="utf-8")
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001 - auditing must never break an alert
        logger.debug("notification audit-log write failed: %s", e)


def record(config, message: str, title: str = "PhotonScript",
           priority: int = 0, reason: str = "held") -> None:
    """Audit a notification that the CALLER decided not to push (e.g. the
    armer collapsing a guiding flap, PS-66), so notifications.jsonl and
    /api/notifications still show every event with sent=false."""
    message, title = ascii_text(message), ascii_text(title)
    logger.info("[pushover held:%s] %s: %s", reason, title, message)
    _audit(config, title, message, priority, False, reason, INFO)
    story_append(config, INFO, title, message, sent=False, reason=reason,
                 priority=priority)


# ---------------------------------------------------------------------------
# PS-212: kinds, the midnight cutoff, facts and the night storyline
# ---------------------------------------------------------------------------

def act_text(problem: str, upside: str, downside: str) -> str:
    """An act push: what is wrong, what acting saves, what happens if not
    ("Piggy-600 cooler stalled after 2 reconnects. If you act: a camera
    power cycle saves ~7 h of Piggy data. If not: the RC16 still images
    and the Piggy sits out.")."""
    def _s(t):
        return str(t or "").strip().rstrip(".")
    return f"{_s(problem)}. If you act: {_s(upside)}. If not: {_s(downside)}."


def dark_left_h(config, now_utc: datetime | None = None) -> float | None:
    """Hours of astronomical dark left tonight (None when unknown)."""
    try:
        from photonscript.shared.localtime import night_window
        now = _utc_naive(now_utc)
        _base, tw = night_window(config, now)
        dusk, dawn = tw.get("astro_dark_start"), tw.get("astro_dark_end")
        if dawn is None:
            return None
        dawn = _utc_naive(dawn)
        start = max(now, _utc_naive(dusk)) if dusk is not None else now
        return max(0.0, (dawn - start).total_seconds() / 3600.0)
    except Exception:  # noqa: BLE001
        return None


# PS-212: what acting buys and what happens if not, per act title (exact
# title first, then the longest matching prefix). notify() appends it to an
# act push that does not already say "If you act:". A caller with a more
# specific line (cooler recovery's hours saved) writes act_text itself.
ACT_CONSEQUENCES = {
    "PhotonScript apps not running": (
        "start it before pre-config and that rig images tonight",
        "that rig sits the night out"),
    "PhotonScript observatory apps": (
        "start the missing app on the scope PC and its rig images tonight",
        "that rig cannot image tonight"),
    "PhotonScript guider": (
        "switch NINA's guider (or re-arm unguided) and the guided targets run",
        "StartGuiding fails and the guided targets are lost"),
    "PhotonScript ProTrack": (
        "tick Activate ProTrack in TheSky and unguided subs stay round",
        "unguided subs trail and are rejected"),
    "PhotonScript TPoint preflight": (
        "load the TPoint model (or run a calibration) and NINA #1 loads tonight",
        "NINA #1 stays unloaded: no RC16 lights"),
    "PhotonScript TPoint run": (
        "check the roof, the camera and Image Link and the run collects samples",
        "the TPoint run collects nothing"),
    "PhotonScript guiding": (
        "fix PHD2 (guide star, calibration, mount) and the guided subs are kept",
        "subs trail; the unguided fallback may take the rest of the night"),
    "PhotonScript cooler": (
        "check the camera cooler and its power and subs match the dark library",
        "subs are shot warm and fail the dark match"),
    "PhotonScript cover": (
        "check the Wanderer Cover and NINA #2's Flat Panel and the Piggy images",
        "the Piggy image passes are skipped"),
    "PhotonScript cooler gate": (
        "check the cooler and its power and the block runs",
        "that block is skipped"),
    "PhotonScript safety": (
        "reconnect the safety monitor in NINA and roof gating is back",
        "only NINA's own SafetyMonitorCondition gates the roof"),
    "PhotonScript dispatch gate": (
        "check the plan and re-arm to run the full night",
        "the split fallback runs instead"),
    "PhotonScript arm recipe": (
        "fix the recipe and re-arm to run it tonight",
        "the normal night runs without the test"),
    "PhotonScript ERROR": (
        "fix the cause and re-arm to save the night",
        "no imaging tonight"),
    "PhotonScript piggyback": (
        "start the Piggy companion by hand and the Piggy-600 images",
        "the RC16 still images and the Piggy sits out"),
    "PhotonScript watching": (
        "check NINA or use Stop & Make Safe",
        "the sideloaded sequence runs on unattended"),
    "PhotonScript paused": (
        "park the mount by hand (NINA or TheSky)",
        "the scope sits unparked while unsafe"),
    "PhotonScript SAFETY STOP": (
        "park the mount by hand (NINA or TheSky)",
        "the scope sits unparked while unsafe"),
    "PhotonScript shutdown warning": (
        "Stop & Make Safe (or NINA by hand) warms and parks the rig now",
        "a cooler or the mount stays on all day"),
    "PhotonScript mount after dawn": (
        "park the mount by hand",
        "the mount stays unparked all day"),
    "PhotonScript unguided fallback": (
        "set guide_fallback_mode=auto or re-arm unguided to save the rest of the night",
        "no RC16 lights for the rest of the night"),
    "PhotonScript transfer": (
        "check Syncthing and the scope PC disk and the night reaches the desktop",
        "subs pile up on the scope PC and its disk may fill"),
    "PhotonScript auto-arm skipped": (
        "fix it and it arms on the next tick",
        "no auto-arm tonight (a manual Arm still works)"),
    "PhotonScript auto-arm warning": (
        "fix the failed check before dark",
        "the night runs with that check failed"),
    "PhotonScript autofocus": (
        "check the AF filter and per-filter offsets",
        "focus drifts and HFR rises"),
    "PhotonScript autofocus not finishing": (
        "check the focuser and the AF star field",
        "every AF restarts and subs go soft"),
    "PhotonScript focuser at 0": (
        "check the focuser in NINA",
        "AF and the focus model start from a wrong position"),
    "PhotonScript NINA validation": (
        "fix the sequence and re-dispatch",
        "NINA may not run tonight's sequence"),
    "PhotonScript off target": (
        "Pause or Restart tonight from now to re-center",
        "subs on the wrong field are wasted"),
    "PhotonScript PHD2 audit": (
        "fix the PHD2 settings before dark",
        "guiding may fail or degrade tonight"),
    "PhotonScript PHD2 calibration": (
        "recalibrate PHD2 near Dec 0 at the meridian",
        "guiding runs on a bad calibration"),
    "PhotonScript PHD2 not correcting": (
        "check PHD2's guide output and the mount link",
        "subs are unguided in all but name"),
    "PhotonScript PHD2 mount refuses pulses": (
        "check the mount's pulse guide link in PHD2",
        "guiding cannot correct"),
    "PhotonScript guide guard": (
        "pick a real guide star in PHD2",
        "guiding stays on a hot pixel or noise"),
    "PhotonScript pulse self-test": (
        "fix PHD2's Dec output setting or recalibrate on this pier side",
        "Dec corrections push the star the wrong way"),
    "PhotonScript Piggy guard": (
        "stop NINA #2 or get the RC16 mount tracking",
        "Piggy lights on a mount that does not track are wasted"),
    "PhotonScript NINA #2 mount": (
        "remove the slew / park items from NINA #2's sequence",
        "NINA #2 can move the shared mount under the RC16"),
    "PhotonScript NINA watch": (
        "check NINA on the scope PC and restart the sequence if needed",
        "that rig images nothing until it is back"),
    "PhotonScript NINA offline": (
        "restart NINA on the scope PC and Restart tonight from now",
        "that rig images nothing for the rest of the night"),
    "PhotonScript night pilot": (
        "do the step the pilot names",
        "the problem runs on for the rest of the night"),
    "PhotonScript stuck loop": (
        "check NINA #1's sequence and Restart tonight from now",
        "the loop runs on without imaging"),
    "PhotonScript unguided guard": (
        "stop PHD2 / disconnect NINA #1's guider by hand",
        "unguided subs get guide pulses and trail"),
    "PhotonScript sub doctor": (
        "apply the fix above",
        "the rejects continue"),
    "PhotonScript NANNY": (
        "check the rig (clouds, dew, focus, tracking)",
        "the night keeps shooting rejects"),
    "PhotonScript settings": (
        "apply the fixes from System > Settings ledger",
        "the wrong setting stays for tonight"),
    "PhotonScript calibration": (
        "re-run the capture",
        "those darks stay owed"),
    "PhotonScript PixInsight": (
        "check the desktop PixInsight run",
        "the queued finish does not complete"),
    "PhotonScript is down": (
        "start it on the scope PC (photonscript start)",
        "no alerts, no dispatch and no dawn shutdown"),
    "PhotonScript update refused": (
        "fix the reason and deploy again",
        "the scope keeps running the previous code"),
    "PhotonScript rolled back": (
        "fix the commit and push a newer one",
        "the scope keeps running the previous code"),
}


def consequence(title: str) -> tuple | None:
    """(upside, downside) for an act title: exact, then longest prefix."""
    t = str(title or "")
    if t in ACT_CONSEQUENCES:
        return ACT_CONSEQUENCES[t]
    best = max((k for k in ACT_CONSEQUENCES if t.startswith(k)), key=len, default=None)
    return ACT_CONSEQUENCES[best] if best else None


def with_consequence(title: str, message: str) -> str:
    """An act message with "If you act: ... If not: ..." appended when it
    does not say so already and the title has a line in ACT_CONSEQUENCES."""
    msg = str(message or "")
    if "If you act:" in msg:
        return msg
    c = consequence(title)
    if not c:
        return msg
    return msg.rstrip() + f" If you act: {c[0]}. If not: {c[1]}."


def norm_kind(kind) -> str:
    """'act' | 'story' | 'info'; anything else (None, typos) is info."""
    k = str(kind or "").strip().lower()
    return k if k in KINDS else INFO


def enabled_kinds(config) -> set:
    """The kinds whose pushes are on (pushover_kinds, comma list). info is
    never pushed, whatever the setting says."""
    raw = _cfg(config, "pushover_kinds")
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = str(raw or "").replace(";", ",").split(",")
    return {norm_kind(x) for x in items if str(x).strip()} - {INFO}


def _hhmm(text, default: str) -> int:
    """'HH:MM' -> minutes after local midnight (bad input -> default)."""
    for v in (text, default):
        try:
            h, m = str(v).strip().split(":")
            h, m = int(h), int(m)
            if 0 <= h < 24 and 0 <= m < 60:
                return h * 60 + m
        except (ValueError, AttributeError):
            continue
    return 0


def _utc_naive(now_utc: datetime | None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    return now_utc


def _local_naive(config, now_utc: datetime | None) -> datetime:
    """The observatory's local wall clock for a UTC instant (naive)."""
    now_utc = _utc_naive(now_utc)
    try:
        from photonscript.shared.localtime import to_local
        return to_local(config, now_utc)
    except Exception:  # noqa: BLE001 - no tz info: treat UTC as local
        return now_utc


def act_held_now(config, now_utc: datetime | None = None) -> bool:
    """True from pushover_act_cutoff_local until pushover_act_resume_local
    (local time; the window may wrap past midnight): a non-safety act is
    held then. Equal cutoff and resume = never held."""
    cut = _hhmm(_cfg(config, "pushover_act_cutoff_local"), "00:00")
    res = _hhmm(_cfg(config, "pushover_act_resume_local"), "08:00")
    if cut == res:
        return False
    loc = _local_naive(config, now_utc)
    m = loc.hour * 60 + loc.minute
    if cut < res:
        return cut <= m < res
    return m >= cut or m < res


def fact_key(kind: str, title: str, message: str, fact: str | None = None) -> str:
    """The fact a push states: fact= when given; else for an act its title
    (one buzz per problem area per night: the next "Settings wrong" or
    "NANNY" of the night goes to the storyline), for a story the title and
    the message with every digit masked ("RC16: 3 kept" == "RC16: 4 kept")."""
    import re
    if fact:
        return f"{kind}|{fact}"
    if norm_kind(kind) == ACT:
        return f"act|{str(title or '').lower()}"
    body = re.sub(r"\d+", "#", f"{title}|{message}".lower())
    return f"{kind}|{body[:300]}"


def mark_facts(config, keys, now_utc: datetime | None = None) -> None:
    """Mark facts told tonight (the plan heads-up folds held acts into one
    push and marks them, so they do not buzz again). Never raises."""
    try:
        path = _state_path(config)
        state = _load_state(path)
        night = night_of(config, now_utc)
        for k in keys or []:
            if k:
                _fact_mark(state, night, k, time.time())
        _save_state(path, state)
    except Exception as e:  # noqa: BLE001
        logger.debug("mark facts failed: %s", e)


def plan_window_open(config, now_utc: datetime | None = None) -> bool:
    """True while tonight is armed and its plan heads-up has not gone out
    yet (before astro dark): a non-safety act then waits for the heads-up,
    which lists it under "Needs you before pre-config"."""
    try:
        from photonscript.scheduler.night_story import plan_window_open as _open
        return bool(_open(config, now_utc))
    except Exception:  # noqa: BLE001
        return False


def decide(config, kind: str, safety: bool = False,
           now_utc: datetime | None = None) -> tuple:
    """The kind policy, before the limiter: (push, reason)."""
    kind = norm_kind(kind)
    if kind == INFO:
        return False, "info"
    if kind == ACT and safety:
        return True, "ok"
    if kind not in enabled_kinds(config):
        return False, f"{kind}-off"
    if kind == ACT and act_held_now(config, now_utc):
        return False, "act-after-cutoff"
    return True, "ok"


def night_of(config, now_utc: datetime | None = None) -> str:
    """The local evening date (noon to noon) of a UTC instant."""
    now_utc = _utc_naive(now_utc)
    try:
        from photonscript.shared.phd2_store import night_of as _n
        return _n(config, now_utc)
    except Exception:  # noqa: BLE001
        from datetime import timedelta
        return (now_utc - timedelta(hours=12)).strftime("%Y-%m-%d")


def story_path(config, night: str) -> Path:
    data_dir = Path(getattr(config, "data_dir", Path.home() / ".photonscript"))
    return data_dir / "night_story" / f"{night}.jsonl"


def story_append(config, kind: str, title: str, message: str, *, sent: bool,
                 reason: str, priority: int = 0, safety: bool = False,
                 fact: str | None = None, thumb: str | None = None,
                 now_utc: datetime | None = None) -> dict | None:
    """Append one storyline entry (every kind, pushed or not). Never raises."""
    try:
        now = _utc_naive(now_utc)
        rec = {"t": now.replace(microsecond=0).isoformat() + "Z",
               "kind": norm_kind(kind), "title": title,
               "text": (message or "")[:AUDIT_MESSAGE_CHARS],
               "sent": bool(sent), "reason": reason, "priority": int(priority)}
        if safety:
            rec["safety"] = True
        if fact:
            rec["fact"] = fact
        if thumb:
            rec["thumb"] = thumb
        p = story_path(config, night_of(config, now))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    except Exception as e:  # noqa: BLE001 - the story never breaks an alert
        logger.debug("night story write failed: %s", e)
        return None


def _fact_seen(state: dict, night: str, key: str) -> bool:
    facts = state.setdefault("facts", {})
    for k in sorted(facts)[:-2]:   # keep the two newest nights
        facts.pop(k, None)
    return key in facts.get(night, {})


def _fact_mark(state: dict, night: str, key: str, now: float) -> None:
    state.setdefault("facts", {}).setdefault(night, {})[key] = now


def _payload(config, message: str, title: str, priority: int,
             sound: str) -> dict:
    """POST body. Pushover REJECTS priority 2 (emergency) without retry and
    expire: both 2026-09-26 "STILL not guiding" escalations came back
    send-failed for that reason (PS-66)."""
    data = {
        "token": config.pushover_api_token,
        "user": config.pushover_user_key,
        "title": title,
        "message": message,
        "priority": priority,
        "sound": sound,
    }
    if int(priority) >= 2:
        retry = int(_cfg(config, "pushover_emergency_retry_s"))
        expire = int(_cfg(config, "pushover_emergency_expire_s"))
        data["retry"] = max(30, retry)              # Pushover minimum 30 s
        data["expire"] = max(data["retry"], min(10800, expire))  # max 3 h
    return data


def _attachment_file(path) -> tuple | None:
    """PS-161: (name, bytes, mime) for a Pushover image attachment, None
    when unreadable or over Pushover's 5 MB limit."""
    try:
        p = Path(path)
        data = p.read_bytes()
    except (OSError, TypeError, ValueError):
        return None
    if len(data) > 5 * 1024 * 1024:
        logger.warning("Pushover attachment %s over 5 MB: sent without it", path)
        return None
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return (p.name, data, mime)


async def _send_raw(config, message: str, title: str, priority: int,
                    sound: str, attachment=None) -> bool:
    """The actual POST. No-op (logged) if keys unset. attachment (PS-161):
    an image path sent as Pushover's attachment (multipart)."""
    if not config.pushover_user_key or not config.pushover_api_token:
        logger.info("[pushover disabled] %s: %s", title, message)
        return False
    try:
        files = None
        if attachment:
            f = _attachment_file(attachment)
            files = {"attachment": f} if f else None
        async with httpx.AsyncClient(timeout=30 if files else 10) as client:
            r = await client.post(API, data=_payload(config, message, title,
                                                     priority, sound),
                                  files=files)
        if r.status_code != 200:
            logger.error("Pushover rejected (%s): %s", r.status_code,
                         (r.text or "")[:200])
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.error("Pushover send failed: %s", e)
        return False


def sun_altitude_deg(lat_deg: float, lon_deg: float, when: datetime) -> float:
    """Approximate solar altitude (NOAA low-precision formulas, ~0.1 deg).
    Pure math so the Pushover path never pays for an astropy import."""
    import math
    t = when.astimezone(timezone.utc)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqtime + 4 * lon_deg          # true solar time, minutes
    ha = math.radians(tst / 4 - 180)                 # hour angle
    lat = math.radians(lat_deg)
    cos_zen = (math.sin(lat) * math.sin(decl)
               + math.cos(lat) * math.cos(decl) * math.cos(ha))
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


def _is_daytime(config, when: datetime | None = None) -> bool:
    try:
        lat = float(getattr(config, "observatory_lat"))
        lon = float(getattr(config, "observatory_lon"))
    except (AttributeError, TypeError, ValueError):
        return False  # unknown site: never quiet (fail loud)
    alt = sun_altitude_deg(lat, lon, when or datetime.now(timezone.utc))
    return alt > float(_cfg(config, "pushover_daytime_sun_alt_deg"))


def _daytime_gate(state: dict, now: float, title: str, priority: int,
                  window_s: float) -> tuple[bool, str]:
    """Daytime quieting (PS-50). Only called while the sun is up.

    priority <= -1 (heartbeats): suppressed.
    priority 0..1: at most one per TITLE per window (a repeating
      "DISCONNECTED for 32/62/92 min" sends once, not every 30 min).
    priority >= 2 (emergency): always passes.
    Mutates `state`; returns (allow, reason)."""
    if priority >= 2:
        return True, "ok"
    if priority <= -1:
        return False, "quiet-daytime"
    by_title = state.setdefault("day_titles", {})
    last = by_title.get(title)
    if last is not None and (now - last) < window_s:
        return False, "quiet-daytime"
    by_title[title] = now
    for k in [k for k, ts in by_title.items() if now - ts > window_s * 2]:
        by_title.pop(k, None)
    return True, "ok"


def _gate(state: dict, now: float, month: str, key: str, priority: int,
          dedup_window_s: int, max_per_hour: int, monthly_cap: int):
    """Pure decision function. Mutates `state`, returns (allow, reason, note).

    `note` is text to append to an allowed message (e.g. suppression summary).
    Kept side-effect-free besides `state` so it is unit-testable.
    """
    # --- monthly rollover / hard cap ---
    if state.get("month") != month:
        state["month"] = month
        state["month_count"] = 0
        state["month_capped"] = False
    if state["month_count"] >= monthly_cap:
        if not state.get("month_capped"):
            state["month_capped"] = True
            # Let this one final notice through (as an emergency), then clamp.
            state["month_count"] += 1
            return (True, "monthly-cap-final",
                    f"\n[PhotonScript: monthly Pushover cap of {monthly_cap} "
                    f"reached: further alerts suppressed until next month.]")
        return (False, "monthly-capped", "")

    # --- identical-message dedup (collapses flaps) ---
    recent = state.setdefault("recent", {})  # key -> last_ts
    last = recent.get(key)
    if last is not None and (now - last) < dedup_window_s and priority < 2:
        state["suppressed"] = state.get("suppressed", 0) + 1
        return (False, "dedup", "")

    # --- rolling hourly burst cap (priority>=2 bypasses) ---
    window = [t for t in state.get("hour_ts", []) if now - t < 3600]
    if priority < 2 and len(window) >= max_per_hour:
        state["hour_ts"] = window
        state["suppressed"] = state.get("suppressed", 0) + 1
        return (False, "hourly-cap", "")

    # --- allowed: record it ---
    window.append(now)
    state["hour_ts"] = window
    recent[key] = now
    # prune dedup map to keep the file small
    for k in [k for k, ts in recent.items() if now - ts > dedup_window_s * 4]:
        recent.pop(k, None)
    state["month_count"] = state.get("month_count", 0) + 1
    note = ""
    supp = state.get("suppressed", 0)
    if supp:
        note = f"\n[+{supp} alert(s) suppressed]"
        state["suppressed"] = 0
    return (True, "ok", note)


async def notify(config, message: str, title: str = "PhotonScript",
                 priority: int = 0, sound: str = "none", attachment=None,
                 kind: str | None = None, safety: bool = False,
                 fact: str | None = None, thumb: str | None = None) -> bool:
    """Send a Pushover notification, rate-limited. No-op (logged) if keys unset.
    attachment (PS-161): an image path to attach (desktop review JPG).
    Title and message go out as plain ASCII (ascii_text).

    PS-212: kind 'act' | 'story' | 'info' (None = info: logged to the night
    storyline, never pushed). safety=True: an act that is sent at any hour
    and is never folded by the once-per-fact rule. fact: what the push
    states, for the once-per-night rule (default: the title and the
    message with digits masked). thumb: a dashboard URL of the image for
    the storyline (the push itself carries attachment)."""
    message, title = ascii_text(message), ascii_text(title)
    kind = norm_kind(kind)
    safety = bool(safety) and kind == ACT
    att = {"attachment": attachment} if attachment else {}

    def _story(sent: bool, reason: str, prio: int) -> None:
        story_append(config, kind, title, message, sent=sent, reason=reason,
                     priority=prio, safety=safety, fact=fact, thumb=thumb)

    push, why = decide(config, kind, safety)
    if not push:
        logger.info("[pushover %s:%s] %s: %s", kind, why, title, message)
        _audit(config, title, message, priority, False, why, kind)
        _story(False, why, priority)
        return False
    if kind == ACT and not safety and plan_window_open(config):
        # PS-212: armed, plan heads-up still to come: fold it in there
        logger.info("[pushover act:for-plan] %s: %s", title, message)
        _audit(config, title, message, priority, False, "for-plan", kind)
        _story(False, "for-plan", priority)
        return False
    if kind == ACT:
        priority = max(int(priority), 1)
        message = with_consequence(title, message)
    elif kind == STORY:
        priority = min(int(priority), 0)
    fkey = None if safety else fact_key(kind, title, message, fact)
    night = night_of(config)

    if not _cfg(config, "pushover_ratelimit_enabled"):
        sent = await _send_raw(config, message, title, priority, sound, **att)
        _audit(config, title, message, priority, sent,
               "sent" if sent else "not-sent", kind)
        _story(sent, "sent" if sent else "not-sent", priority)
        return sent

    try:
        now = time.time()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        key = f"{title}\x1f{message}"
        path = _state_path(config)
        async with _lock:
            state = _load_state(path)
            if fkey and _fact_seen(state, night, fkey):
                _save_state(path, state)
                logger.info("[pushover suppressed:dup-fact] %s: %s", title, message)
                _audit(config, title, message, priority, False, "dup-fact", kind)
                _story(False, "dup-fact", priority)
                return False
            if _cfg(config, "pushover_quiet_daytime") and _is_daytime(config):
                ok, why = _daytime_gate(
                    state, now, title, priority,
                    float(_cfg(config, "pushover_daytime_title_window_h")) * 3600)
                if not ok:
                    _save_state(path, state)
                    logger.info("[pushover suppressed:%s] %s: %s", why, title, message)
                    _audit(config, title, message, priority, False, why, kind)
                    _story(False, why, priority)
                    return False
            allow, reason, note = _gate(
                state, now, month, key, priority,
                int(_cfg(config, "pushover_dedup_window_s")),
                int(_cfg(config, "pushover_max_per_hour")),
                int(_cfg(config, "pushover_monthly_cap")),
            )
            if allow and fkey:
                _fact_mark(state, night, fkey, now)
            _save_state(path, state)
    except Exception as e:  # noqa: BLE001 - never let the limiter drop a real alert
        logger.warning("Pushover limiter error (%s), sending unthrottled", e)
        sent = await _send_raw(config, message, title, priority, sound, **att)
        _audit(config, title, message, priority, sent, "limiter-error", kind)
        _story(sent, "limiter-error", priority)
        return sent

    if not allow:
        logger.info("[pushover suppressed:%s] %s: %s", reason, title, message)
        _audit(config, title, message, priority, False, reason, kind)
        _story(False, reason, priority)
        return False
    if reason == "monthly-cap-final":
        priority = max(priority, 1)
    sent = await _send_raw(config, message + note, title, priority, sound, **att)
    reason_out = ("sent" if reason == "ok" else reason) if sent else "send-failed"
    _audit(config, title, message, priority, sent, reason_out, kind)
    _story(sent, reason_out, priority)
    return sent
