"""PS-132: check NINA's own validation right after a ninaAPI /sequence/load.

NINA refuses a manual Start silently when a sequence fails validation: on
2026-10-05 the Piggy-600 companion's SkyFlat (no SwitchFilter child) made
SkyFlat.Validate throw "Sequence contains no matching element" every 5 s in
NINA #2's log, Start did nothing twice, and nothing in PhotonScript noticed.
A start with skipValidation=true (rigs.nina_dispatch) runs anyway, but the
broken item fails when it is reached, so the flats never happened either.

Two signals, read after a short settle so NINA's periodic validation has run:

  issues    every "Issues" list in GET /sequence/state (ninaAPI reflects each
            item's public properties, so a validatable item's Issues are in
            the tree), e.g. "Filter wheel not connected"
  errors    ERROR blocks in the rig's NINA log, written since the load, whose
            text names a Validate method (a validator that THROWS never sets
            Issues, so the log is the only place it shows)

check_loaded() returns both; alert() pushes one Pushover. Mode (config
nina_load_validation): "alert" (default) reports and never blocks; "refuse"
also makes nina_dispatch skip Start when a validator threw; "off" skips the
check. Issues alone never block a dispatch: some are expected at load time
(e.g. a camera the sequence connects itself), which is why the dispatch
starts with skipValidation.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("alert", "refuse", "off")
LOG_TAIL_BYTES = 2_000_000

# <local time>|LEVEL|Source.cs|Member|line|message (NINA 3 log line)
_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)\|(\w+)\|"
                   r"([^|]*)\|([^|]*)\|([^|]*)\|(.*)$")
_FRAME = re.compile(r"\bat\s+([\w.`<>]+)\(")


def mode(config) -> str:
    m = str(getattr(config, "nina_load_validation", "alert") or "alert")
    m = m.strip().lower()
    return m if m in MODES else "alert"


def settle_s(config) -> float:
    try:
        return max(0.0, float(getattr(config, "nina_load_validation_settle_s", 8.0)))
    except (TypeError, ValueError):
        return 8.0


# ------------------------------------------------------------------ parsing

def collect_issues(tree) -> list[str]:
    """"<item>: <issue>" for every non-empty Issues list in a ninaAPI
    sequence tree (Items / Conditions / Triggers as lists or {"$values"}),
    deduplicated, document order."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        iss = n.get("Issues")
        if isinstance(iss, dict):
            iss = iss.get("$values")
        if isinstance(iss, list):
            name = str(n.get("Name") or "?")
            if name.endswith("_Container"):
                name = name[: -len("_Container")]
            for i in iss:
                s = f"{name}: {i}"
                if i and s not in out:
                    out.append(s)
        for key in ("Items", "Conditions", "Triggers"):
            v = n.get(key)
            walk(v.get("$values") if isinstance(v, dict) else v)

    walk(tree)
    return out


def _ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s[:26])
    except ValueError:
        return None


def validation_errors(text: str, since: datetime | None = None) -> list[str]:
    """Distinct validation failures in NINA log text: ERROR blocks (the
    header line plus its stack-trace continuation lines) at or after `since`
    (local time, 2 s slack) that name a Validate method. Each comes back as
    "<message> (in <Class.Method>)", the first non-System stack frame."""
    out: list[str] = []
    floor = since - timedelta(seconds=2) if since else None
    block: list[str] | None = None
    head = None

    def flush():
        if block is None or head is None:
            return
        level, member, msg = head
        blob = "\n".join(block)
        if level.upper() != "ERROR":
            return
        if "validate" not in member.lower() and "Validate(" not in blob:
            return
        frames = [f for f in _FRAME.findall(blob)
                  if not f.startswith(("System.", "Microsoft."))]
        where = ".".join(frames[0].split(".")[-2:]) if frames else member
        s = f"{msg.strip()} (in {where})"
        if s not in out:
            out.append(s)

    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if m:
            flush()
            t = _ts(m.group(1))
            if floor is not None and (t is None or t < floor):
                block, head = None, None
                continue
            block = [line]
            head = (m.group(2), m.group(4), m.group(6))
        elif block is not None:
            block.append(line)
    flush()
    return out


def _tail(path: Path, nbytes: int = LOG_TAIL_BYTES) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - nbytes))
        return fh.read().decode("utf-8", errors="replace")


def read_log_errors(config, rig: str, since: datetime | None):
    """(errors, note) from the rig's newest NINA log (the same pick as
    /api/nina/log). note says why the log could not be read; errors is
    then []. Never raises."""
    try:
        from photonscript.scheduler.routers.triage import _rig_log
        path, note = _rig_log(config, rig)
        if path is None:
            return [], note or "no NINA log"
        return validation_errors(_tail(Path(path)), since), ""
    except Exception as e:  # noqa: BLE001
        return [], f"NINA log not read ({type(e).__name__}: {e})"


# -------------------------------------------------------------------- check

def summarize(res: dict) -> str:
    parts = []
    if res.get("errors"):
        parts.append(f"{len(res['errors'])} validation error(s): "
                     + "; ".join(res["errors"][:3]))
    if res.get("issues"):
        parts.append(f"{len(res['issues'])} issue(s): "
                     + "; ".join(res["issues"][:5]))
    if not parts:
        parts.append("no validation issues")
    if res.get("state_error"):
        parts.append(f"sequence state not read ({res['state_error']})")
    if res.get("log_note"):
        parts.append(res["log_note"])
    return ". ".join(parts)


async def check_loaded(base_url: str, config, rig: str,
                       since: datetime | None = None, client=None,
                       settle: float | None = None, sleep=asyncio.sleep,
                       log_reader=None) -> dict:
    """Read NINA's verdict on the sequence just loaded. Waits `settle` s
    (config nina_load_validation_settle_s), then GET /sequence/state for
    Issues and the rig's NINA log for Validate errors since `since`.
    {ok, issues, errors, state_error, log_note, detail}; ok is False when
    either list is non-empty. Never raises."""
    from photonscript.scheduler.sideload import read_sequence_state
    s = settle_s(config) if settle is None else settle
    if s > 0:
        await sleep(s)
    try:
        tree, err = await read_sequence_state(base_url, client=client)
    except Exception as e:  # noqa: BLE001
        tree, err = None, f"{type(e).__name__}: {e}"
    issues = collect_issues(tree) if tree is not None else []
    reader = log_reader or read_log_errors
    try:
        errors, note = reader(config, rig, since)
    except Exception as e:  # noqa: BLE001
        errors, note = [], f"NINA log not read ({type(e).__name__}: {e})"
    res = {"ok": not issues and not errors, "issues": issues,
           "errors": errors, "state_error": err, "log_note": note}
    res["detail"] = summarize(res)
    return res


async def alert(config, rig: str, res: dict, what: str) -> None:
    """One Pushover for a failed check (never raises)."""
    try:
        from photonscript.shared.pushover import notify
        from photonscript.shared.rigs import rig_label
        label = rig_label(config, rig)
        fatal = bool(res.get("errors"))
        msg = (f"{label}: NINA {'REJECTS' if fatal else 'flags'} the sequence "
               f"just loaded ({what}): {res.get('detail')}"
               + (". A manual Start in NINA will do nothing." if fatal else ""))
        await notify(config, msg, title="PhotonScript NINA validation",
                     priority=1 if fatal else 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("NINA validation alert failed: %s", e)
