"""Quiet carriers (2026-10-25): an unguided moon-fill carrier is not judged.

On a moon-fill night (scheduler/moon_fill.py) the RC16 shoots Ha as a
pointing carrier for the Piggy-600. Unguided, its subs are expected to be
rejected (the 2026-10-24 Ha night: 126 rejects in a row). Nothing that
reads RC16 rejects as a problem should alarm or act on them:

  * the night pilot: no RC16 shorten / limit / refocus / AF / skip / sub
    ladder decisions and no "needs a human" or optics push for the RC16
    on a carrier (the PHD2-on-an-unguided-night stop and alert stay: PHD2
    moves the shared mount, so the Piggy-600 trails too)
  * the sub doctor: still diagnoses and records (state, run events), no
    push for the RC16 on a carrier (guider_conflict still pushes)
  * the telescope agent's reject streak: carrier subs neither count toward
    nor reset it (no NANNY severe, no auto abort that would stop the
    Piggy-600's pointing)
  * autofocus failures (the af_stall alarm, the AF report grader, the sub
    doctor's focuser verdict): info only for the RC16 on a carrier night,
    unless the Piggy-600's autofocus is failing too
  * the focuser guard: an RC16 carrier sub does not decide "soft" (a
    trailed carrier sub says nothing about focus); the decision waits for a
    sub that is judged
  * the night story and the morning report: "RC16 carrier for the
    Piggy-600, N subs, not judged" instead of the kept / rejected tally

The RC16 subs are still graded, culled and recorded as always. The Piggy-600
and every safety alert are unchanged. A guided carrier (moon_fill guided,
the armer guiding) is bonus Ha and behaves as any target.

plan_moon_fill_carrier_quiet (default on) turns all of this off.

Where a carrier is known from: the dispatch's plan["moon_fill"] carriers
(name and window), the live armer first (its plan and guiding mode), else
<data_dir>/armer_state.json, else the night's plan snapshot
runs/<night>_plan.json (moon_fill saved there since 2026-10-25). Unguided
means the live armer's guiding mode while a night is armed, else the
carrier's planned expect_rejected.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

KEY = "plan_moon_fill_carrier_quiet"
RC16 = "rc16"
NOT_JUDGED = "not judged"


def enabled(cfg) -> bool:
    return bool(getattr(cfg, KEY, True))


def _key(name) -> str:
    try:
        from photonscript.shared.target_names import canonical_target, target_key
        return target_key(canonical_target(name) or name)
    except Exception:  # noqa: BLE001
        return str(name or "").strip().lower()


def _parse(v) -> datetime | None:
    if not isinstance(v, datetime):
        try:
            v = datetime.fromisoformat(str(v).strip().rstrip("Z"))
        except (TypeError, ValueError):
            return None
    if v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _app_armer():
    mod = sys.modules.get("photonscript.scheduler.app")
    return getattr(mod, "_armer", None) if mod else None


def _armer_guided(armer) -> bool | None:
    """The armer's guiding mode while a night is armed, else None."""
    try:
        from photonscript.scheduler.armer import ACTIVE_STATES
        if str(getattr(armer, "state", "")) not in ACTIVE_STATES:
            return None
        return bool(armer._use_guiding())
    except Exception:  # noqa: BLE001
        return None


def _saved_state(cfg) -> dict:
    p = Path(getattr(cfg, "data_dir", ".")) / "armer_state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _saved_guided(cfg, saved: dict) -> bool | None:
    try:
        from photonscript.scheduler.armer import ACTIVE_STATES, norm_guiding_mode
    except Exception:  # noqa: BLE001
        return None
    if saved.get("state") not in ACTIVE_STATES:
        return None
    m = norm_guiding_mode(saved.get("guiding_override"))
    if m is not None:
        return m == "guided"
    return bool(getattr(cfg, "guided_default", True))


def _snapshot(cfg, night: str) -> dict | None:
    p = Path(getattr(cfg, "data_dir", ".")) / "runs" / f"{night}_plan.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _source(cfg, night: str | None = None, plan: dict | None = None,
            armer=None) -> tuple[dict | None, bool | None]:
    """(plan, live guided or None): the plan that names tonight's carriers.
    An explicit plan wins (its guiding mode from the armer when given)."""
    if plan is not None:
        return plan, (_armer_guided(armer) if armer is not None else None)
    a = armer if armer is not None else _app_armer()
    if a is not None:
        p = getattr(a, "plan", None) or {}
        if p.get("moon_fill") is not None and (night is None or p.get("night_of") == night):
            return p, _armer_guided(a)
    saved = _saved_state(cfg)
    p = saved.get("plan") or {}
    if p.get("moon_fill") is not None and (night is None or p.get("night_of") == night):
        return p, _saved_guided(cfg, saved)
    if night:
        snap = _snapshot(cfg, night)
        if snap and snap.get("moon_fill") is not None:
            return snap, None
    return None, None


def carriers(cfg, night: str | None = None, plan: dict | None = None,
             armer=None) -> list[dict]:
    """Tonight's moon-fill carriers: the plan's rows, each with "unguided"
    (bool) resolved and "start" / "end" (naive UTC). [] when none."""
    p, live = _source(cfg, night, plan, armer)
    mf = (p or {}).get("moon_fill") or {}
    if not mf.get("active"):
        return []
    out = []
    for r in mf.get("carriers") or []:
        if not isinstance(r, dict) or not r.get("target"):
            continue
        if live is not None:
            ung = not live
        elif "expect_rejected" in r:
            ung = bool(r.get("expect_rejected"))
        else:
            ung = mf.get("guided") is not True
        out.append({**r, "unguided": ung, "start": _parse(r.get("window_start_utc")),
                    "end": _parse(r.get("window_end_utc"))})
    return out


def carrier_for(cfg, *, rig: str = RC16, target=None, when=None,
                night: str | None = None, plan: dict | None = None,
                armer=None) -> dict | None:
    """The unguided carrier row the RC16 is on (by target name, else by the
    carrier window holding `when`, default now), or None: quiet off, not the
    RC16, no carrier, or a guided carrier (bonus Ha, normal behavior)."""
    if rig != RC16 or not enabled(cfg):
        return None
    try:
        rows = carriers(cfg, night, plan, armer)
    except Exception as e:  # noqa: BLE001 - never cost a decision
        logger.debug("carrier lookup failed: %s", e)
        return None
    if not rows:
        return None
    row = None
    if target:
        k = _key(target)
        row = next((r for r in rows if _key(r["target"]) == k), None)
    else:
        t = _parse(when) if when is not None else datetime.utcnow()
        if t is not None:
            row = next((r for r in rows if r["start"] and r["end"]
                        and r["start"] <= t < r["end"]), None)
    return row if row and row.get("unguided") else None


def quiet_targets(cfg, night: str | None = None, plan: dict | None = None,
                  armer=None) -> dict[str, dict]:
    """{target key: row} of the night's unguided carriers (quiet on)."""
    if not enabled(cfg):
        return {}
    try:
        return {_key(r["target"]): r for r in carriers(cfg, night, plan, armer)
                if r.get("unguided")}
    except Exception:  # noqa: BLE001
        return {}


def is_carrier_sub(rec: dict, quiet: dict) -> bool:
    """An RC16 light on one of `quiet` (quiet_targets)."""
    return bool(quiet) and (rec.get("rig") or RC16) == RC16 and \
        _key(rec.get("target")) in quiet


def piggy_af_failing(cfg, night: str | None) -> bool:
    """The Piggy-600's autofocus failed tonight too (an af_stall run event
    of the piggyback rig)."""
    if not night:
        return False
    try:
        from photonscript.shared import phd2_store as store
        from photonscript.shared.night_events import events_path
        return any(e.get("kind") == "af_stall" and (e.get("rig") or RC16) != RC16
                   for e in store.read_jsonl(events_path(cfg, night)))
    except Exception:  # noqa: BLE001
        return False


def af_kind(cfg, night: str | None, kind: str = "act", *, when=None,
            target=None) -> str:
    """The push kind for an RC16 autofocus failure: info on an unguided
    carrier unless the Piggy-600 is failing too, else `kind`."""
    if carrier_for(cfg, target=target, when=when, night=night) is None:
        return kind
    return kind if piggy_af_failing(cfg, night) else "info"


def summary_text(n: int, targets=None, kept: int = 0) -> str:
    """The report line: "RC16 carrier for the Piggy-600, N subs, not judged"."""
    s = f"RC16 carrier for the Piggy-600, {n} sub{'s' if n != 1 else ''}, {NOT_JUDGED}"
    names = [t for t in targets or [] if t]
    if names:
        s += f" ({', '.join(names)})"
    if kept:
        s += f"; {kept} passed QA anyway"
    return s
