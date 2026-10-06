"""Per-block guiding decisions (PS-85): guided + ProTrack, never on noise.

The RC16's off-axis guider sits behind the filter wheel. On 3 nm narrowband
the guide camera may see no real star at all: on 2026-10-05 (Heart Ha /
OIII / SII, 600 s) PHD2 locked on noise (SNR 21.9 to 30.9, a jagged
profile), Dec RMS read 8.5 px and NINA's StartGuiding failed, which skips
the whole target. With ProTrack on, such a block is better shot unguided at
the sub length the tracking test proved than guided on noise or not at all.

A "block" is one filter of one target (one ExposurePlan). For each guided
block this module decides guided or unguided:

    decide(...)                  the decision table (pure)
    fallback_exposure_s(...)     the unguided sub length for a filter: the
                                 tracking-test report's longest pass after
                                 guide_fallback_test_since_utc (ProTrack on),
                                 else guide_fallback_exposure_s, never over
                                 unguided_max_exposure_s
    dispatch_decisions(...)      tonight's live decisions plus history (the
                                 same target and filter not viable on a
                                 recent night at the same guide bin / gain)
    apply_to_targets(...)        marks the blocks unguided on the planner's
                                 per-night target copies and caps their subs

Records (readers never raise):

    <data_dir>/phd2/blocks/<night>.jsonl
        {"event": "check", ...}     one viability check (the RC16 agent)
        {"event": "decision", ...}  a block decision (mode, acted or not)

guide_block_mode: off (today's behavior), observe (decide, record, show on
the Guiding tab, one push per target per night; nothing is switched), auto
(the armer stops guiding and re-dispatches the rest with the block unguided).
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

MODES = ("off", "observe", "auto")
NB_FILTERS = ("Ha", "OIII", "SII")
GUIDED, UNGUIDED, PENDING = "guided", "unguided", "pending"
REPORT_TTL_S = 3600.0
_report_cache: dict = {}


def block_mode(config) -> str:
    m = str(getattr(config, "guide_block_mode", "observe") or "observe").strip().lower()
    return m if m in MODES else "observe"


def is_nb(filt) -> bool:
    return str(filt or "") in NB_FILTERS


def blocks_path(config, night: str) -> Path:
    return store.phd2_dir(config) / "blocks" / f"{night}.jsonl"


def records(config, night: str) -> list[dict]:
    return store.read_jsonl(blocks_path(config, night))


def append(config, night: str, rec: dict) -> dict:
    rec = dict(rec)
    rec.setdefault("t_utc", store.iso_z(datetime.utcnow()))
    rec.setdefault("night", night)
    store.append_jsonl(blocks_path(config, night), rec)
    return rec


# ------------------------------------------------------- sub length per filter

def parse_lengths(text) -> dict[str, float]:
    """'L:60,Ha:300' -> {'L': 60.0, 'Ha': 300.0}; junk entries are skipped."""
    out: dict[str, float] = {}
    for part in str(text or "").split(","):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        try:
            s = float(v)
        except ValueError:
            continue
        if k.strip() and s > 0:
            out[k.strip()] = s
    return out


def proven_lengths(config, report: dict | None = None) -> dict[str, float]:
    """{filter: longest unguided pass (s)} from the tracking-test report of
    guide_fallback_test_date, counting only groups shot after
    guide_fallback_test_since_utc (ProTrack on). {} when unset or unreadable.
    The report is cached for an hour (it reads the night's runs log)."""
    date = str(getattr(config, "guide_fallback_test_date", "") or "").strip()
    if not date and report is None:
        return {}
    since = store.parse_z(getattr(config, "guide_fallback_test_since_utc", "") or "")
    if report is None:
        key = (str(getattr(config, "data_dir", "")), date)
        hit = _report_cache.get(key)
        if hit and time.monotonic() - hit[0] < REPORT_TTL_S:
            report = hit[1]
        else:
            try:
                from photonscript.scheduler.tracking_test import build_report
                report = build_report(config, date, read_headers=False)
            except Exception as e:  # noqa: BLE001
                logger.warning("PS-85: tracking-test report %s unreadable: %s", date, e)
                report = {}
            _report_cache[key] = (time.monotonic(), report)
    from photonscript.scheduler.tracking_test import filter_verdicts
    groups = [g for g in (report or {}).get("groups") or []
              if g.get("rig", "rc16") == "rc16"
              and (since is None or (store.parse_z(g.get("time_first")) or since) >= since)]
    return {v["filter"]: float(v["longest_pass_s"])
            for v in filter_verdicts(groups) if v.get("longest_pass_s")}


def fallback_exposure_s(config, filt, proven: dict | None = None) -> tuple:
    """(seconds, source) for an unguided block of this filter: the proven
    length, else the configured one, else the PS-66 cap; never over
    unguided_max_exposure_s. (None, "none") when nothing applies."""
    proven = proven_lengths(config) if proven is None else proven
    f = str(filt or "")
    s, src = None, "none"
    if proven.get(f):
        s, src = float(proven[f]), ("tracking test "
                                    + str(getattr(config, "guide_fallback_test_date", "")))
    else:
        cfg = parse_lengths(getattr(config, "guide_fallback_exposure_s", ""))
        if cfg.get(f):
            s, src = cfg[f], "config"
    try:
        cap = float(getattr(config, "unguided_max_exposure_s", 300) or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if s is None and cap > 0:
        s, src = cap, "unguided cap"
    elif s is not None and cap > 0 and s > cap:
        s, src = cap, src + ", capped"
    return s, src


# ------------------------------------------------------------ decision table

def decide(*, mode: str, night_guided: bool, verdict: dict | None = None,
           history: dict | None = None, headroom: bool = False) -> dict:
    """One block's decision. verdict: tonight's viability check ({"viable",
    "reason"}) or None; history: a recent non-viable check at the same setup
    or None; headroom: the tuner can still lengthen the guide exposure on
    this filter (mode exposure, NB band not exhausted).

    {"decision": guided | unguided | pending, "act": bool (switch it now),
     "source": night | off | live | history | default, "reason"}"""
    if not night_guided:
        return {"decision": UNGUIDED, "act": False, "source": "night",
                "reason": "night armed unguided"}
    if mode == "off":
        return {"decision": GUIDED, "act": False, "source": "off",
                "reason": "per-block guiding off (guide_block_mode=off)"}
    act = mode == "auto"
    if verdict is not None:
        if verdict.get("viable"):
            return {"decision": GUIDED, "act": False, "source": "live",
                    "reason": "real guide star: " + str(verdict.get("reason") or "")}
        if headroom:
            return {"decision": PENDING, "act": False, "source": "live",
                    "reason": "no real guide star yet; the tuner can still "
                              "lengthen the guide exposure: " + str(verdict.get("reason") or "")}
        return {"decision": UNGUIDED, "act": act, "source": "live",
                "reason": "no real guide star: " + str(verdict.get("reason") or "")}
    if history is not None:
        return {"decision": UNGUIDED, "act": act, "source": "history",
                "reason": f"not viable on {history.get('night')} at the same "
                          f"guide setup: {history.get('reason')}"}
    return {"decision": GUIDED, "act": False, "source": "default",
            "reason": "no check yet"}


# --------------------------------------------------------- history and setup

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def current_setup(config) -> dict:
    """Guide binning and gain as the newest PS-89 audit read them."""
    try:
        from photonscript.scheduler import phd2_audit
        a = phd2_audit.load_latest(config) or {}
    except Exception:  # noqa: BLE001
        a = {}
    out = {"binning": None, "gain": None}
    for r in a.get("rows") or []:
        if r.get("id") in out:
            out[r["id"]] = _num(r.get("current"))
    return out


def _same_setup(rec: dict, setup: dict) -> bool:
    for k in ("binning", "gain"):
        a, b = _num(rec.get(k)), _num((setup or {}).get(k))
        if a is not None and b is not None and a != b:
            return False
    return True


def history_unguided(config, target, filt, night: str, setup: dict | None = None,
                     days: int | None = None) -> dict | None:
    """The newest final non-viable check of this target and filter within
    guide_block_history_days before `night` (tonight excluded) at the same
    guide binning and gain, unless a newer check there was viable. None
    otherwise."""
    if days is None:
        days = int(getattr(config, "guide_block_history_days", 7) or 0)
    if days <= 0:
        return None
    try:
        n0 = datetime.strptime(night, "%Y-%m-%d")
    except ValueError:
        return None
    setup = current_setup(config) if setup is None else setup
    for back in range(1, days + 1):
        d = (n0 - timedelta(days=back)).strftime("%Y-%m-%d")
        checks = [r for r in records(config, d) if r.get("event") == "check"
                  and r.get("final", True) and r.get("target") == target
                  and r.get("filter") == filt and _same_setup(r, setup)]
        if checks:
            last = checks[-1]
            return None if last.get("viable") else dict(last, night=d)
    return None


def tonight_decisions(config, night: str) -> dict:
    """{(target, filter): newest decision record} for tonight."""
    out: dict = {}
    for r in records(config, night):
        if r.get("event") == "decision":
            out[(r.get("target"), r.get("filter"))] = r
    return out


def dispatch_decisions(config, night: str, targets, live: dict | None = None,
                       setup: dict | None = None) -> dict:
    """{(target, filter): {"source", "reason"}} of the blocks to run
    unguided in this dispatch: the armer's live decisions (mode auto) plus,
    for guided targets with no live decision on that block, history. Only
    filters the targets actually carry."""
    out: dict = {}
    live = live or {}
    for key, d in live.items():
        out[tuple(key)] = {"source": d.get("source", "live"), "reason": d.get("reason")}
    days = int(getattr(config, "guide_block_history_days", 7) or 0)
    if days <= 0:
        return out
    setup = current_setup(config) if setup is None else setup
    for t in targets or []:
        if not getattr(t, "start_guiding", False):
            continue
        for e in t.exposures:
            key = (t.name, e.filter_type.value)
            if key in out:
                continue
            h = history_unguided(config, t.name, key[1], night, setup, days)
            if h is not None:
                out[key] = {"source": "history",
                            "reason": decide(mode="auto", night_guided=True,
                                             history=h)["reason"]}
    return out


def planned_blocks(config, night: str) -> set | None:
    """{(target, filter)} of the guided blocks in the night's last dispatch
    (runs/<night>_plan.json, written by the armer), None when there is no
    snapshot. The AF filter's guide frames (L between narrowband blocks)
    are checked too, but only a planned guided block can be switched."""
    p = Path(getattr(config, "data_dir", ".")) / "runs" / f"{night}_plan.json"
    snap = store.read_json(p)
    if not snap:
        return None
    out = set()
    for t in snap.get("targets") or []:
        if not t.get("guided"):
            continue
        ung = set(t.get("unguided_filters") or [])
        for e in t.get("exposures") or []:
            if e.get("filter") and e["filter"] not in ung:
                out.add((t.get("name"), e["filter"]))
    return out


# ------------------------------------------------------------- apply to plan

def _cap_plan(e, cap: float):
    """An ExposurePlan with its long set (and an HDR short set over it) at
    `cap` seconds, same integration (PS-66 cap_unguided math)."""
    upd = {}
    owed = e.count - e.acquired
    if cap and e.exposure_seconds > cap and owed > 0:
        upd.update(exposure_seconds=cap, acquired=0, acquired_s=0.0,
                   count=math.ceil(owed * e.exposure_seconds / cap - 1e-9))
    short_owed = e.short_remaining()
    if cap and e.hdr_short_seconds and e.hdr_short_seconds > cap and short_owed > 0:
        upd.update(hdr_short_seconds=cap, hdr_short_acquired=0,
                   hdr_short_count=math.ceil(short_owed * e.hdr_short_seconds / cap - 1e-9))
    return e.model_copy(update=upd) if upd else e


def apply_to_targets(config, targets, decisions: dict,
                     proven: dict | None = None) -> list[str]:
    """Mark the decided blocks unguided on the planner's per-night target
    copies (NinaSequenceTarget.unguided_filters) and cap their subs at the
    filter's fallback length. A target whose every block with owed subs is
    unguided becomes an unguided target (no StartGuiding at all). Returns
    one note per block."""
    notes: list[str] = []
    if not decisions:
        return notes
    proven = proven_lengths(config) if proven is None else proven
    for t in targets or []:
        if not getattr(t, "start_guiding", False) or getattr(t, "tracking_test", False) \
                or getattr(t, "focus_calibration", False):
            continue
        ung = []
        new = []
        for e in t.exposures:
            f = e.filter_type.value
            d = decisions.get((t.name, f))
            if d is None:
                new.append(e)
                continue
            s, src = fallback_exposure_s(config, f, proven)
            ung.append(f)
            new.append(_cap_plan(e, s) if s else e)
            notes.append(f"{t.name} {f}: unguided at {s:g} s ({src}; "
                         f"{d.get('source')}: {d.get('reason')})" if s else
                         f"{t.name} {f}: unguided ({d.get('source')})")
        if not ung:
            continue
        t.exposures = new
        owed = [e.filter_type.value for e in t.exposures
                if e.count - e.acquired > 0 or e.short_remaining() > 0]
        if owed and all(f in ung for f in owed):
            t.start_guiding = False
            t.unguided_filters = []
        else:
            t.unguided_filters = sorted(set(ung))
    if notes:
        logger.info("PS-85 per-block guiding: %s", "; ".join(notes))
    return notes


# ------------------------------------------------------------------- alerts

async def alert_once(config, night: str, target: str, text: str) -> bool:
    """One Pushover per target per night (key block-<night>-<target>); the
    rest are audited only. True when it was sent."""
    from photonscript.shared.pushover import notify, record
    if store.alert_once(config, f"block-{night}-{target}"):
        await notify(config, text, title="PhotonScript guiding per block",
                     priority=0)
        return True
    record(config, text, title="PhotonScript guiding per block", priority=0,
           reason="block-once-per-target")
    return False


# ------------------------------------------------------------------- summary

def summary(config, night: str) -> dict:
    """The night's per-block guiding for the Guiding tab: mode, the
    fallback lengths per filter, every check and the latest decision per
    block."""
    recs = records(config, night)
    checks = [r for r in recs if r.get("event") == "check"]
    dec = tonight_decisions(config, night)
    try:
        proven = proven_lengths(config)
    except Exception:  # noqa: BLE001
        proven = {}
    lengths = {}
    for f in ("L", "R", "G", "B", "Ha", "OIII", "SII"):
        s, src = fallback_exposure_s(config, f, proven)
        lengths[f] = {"exposure_s": s, "source": src}
    blocks = []
    keys = sorted({(c.get("target"), c.get("filter")) for c in checks} | set(dec))
    for k in keys:
        cs = [c for c in checks if (c.get("target"), c.get("filter")) == k]
        last = cs[-1] if cs else {}
        d = dec.get(k) or {}
        blocks.append({"target": k[0], "filter": k[1],
                       "decision": d.get("decision") or (
                           GUIDED if last.get("viable") else
                           PENDING if last and not last.get("final", True) else
                           UNGUIDED if last else None),
                       "source": d.get("source") or ("live" if last else None),
                       "acted": d.get("acted"), "reason": d.get("reason") or last.get("reason"),
                       "exposure_s": d.get("exposure_s"),
                       "checks": len(cs), "snr": last.get("snr"),
                       "hfd_px": last.get("hfd_px"), "profile": last.get("profile"),
                       "exposure_ms": last.get("exposure_ms"),
                       "t_utc": d.get("t_utc") or last.get("t_utc")})
    return {"date": night, "mode": block_mode(config),
            "snr_min": float(getattr(config, "guide_viable_snr_min", 30.0) or 30.0),
            "lowsnr_frames": int(getattr(config, "guide_lowsnr_frames", 10) or 0),
            "fallback": lengths, "proven": proven,
            "test_date": getattr(config, "guide_fallback_test_date", ""),
            "blocks": blocks, "checks": len(checks),
            "redispatches": sum(1 for d in dec.values() if d.get("acted"))}
