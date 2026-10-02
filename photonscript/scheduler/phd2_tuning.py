"""Guide-star tuning memory, next-night recommendation and the night table
(PS-90). The live half is telescope_agent.guide_tuner.

    <data_dir>/phd2/tune.json             one entry per (profile, binning,
                                          gain, target, filter): the last
                                          measurement and the last exposure
                                          that landed in the peak band
    <data_dir>/phd2/tune/<night>.jsonl    every measurement and change

    record() / record_change()   the tuner's writes (changes also go to the
                         PS-89 audit's changes.jsonl, one change log for PHD2)
    recall()             the exposure for a target + filter: remembered, or
                         predicted from another filter of the same target and
                         the learned per-filter flux ratio against L (the OAG
                         is assumed to sit behind the filter wheel), or the
                         typical rate of that filter over other targets
    recommend()          next night's gain / binning from recent nights: a
                         filter still faint at the longest allowed exposure
                         wants more gain (then bin 3), one clipped at the
                         shortest wants less gain. Binning stays at the
                         current value (approved: bin 2 for now); the "bin3"
                         block says, from the measured HFD, whether bin 3
                         would hit the HFD target.
    summary()            the night block (API, runs page, System page box)
    apply_predusk()      the armer's pre-dusk hook (ARMED, before the
                         dispatch): with phd2_audit_autofix only, write the
                         recommended gain / binning through the PS-89 profile
                         writer (PHD2 closed, backup first, verified registry
                         names), then re-run the PS-89 audit.

PHD2 gain is PHD2's own camera gain setting as its guide log reports it
(0 to 100 for most camera drivers; GAIN_MAX). The mapping to the sensor's
real gain is assumed linear and is unverified.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

GAIN_MAX = 100          # PHD2's camera gain scale
GAIN_MIN = 0
GAIN_STEP = 5
RECENT_NIGHTS = 7
REF_FILTER = "L"


def tune_path(config) -> Path:
    return store.phd2_dir(config) / "tune.json"


def night_log_path(config, night: str) -> Path:
    return store.phd2_dir(config) / "tune" / f"{night}.jsonl"


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def entry_key(profile, binning, gain, target, filt) -> str:
    g = _num(gain)
    return "|".join([str(profile or "?"), f"b{binning or '?'}",
                     f"g{g:g}" if g is not None else "g?",
                     str(target or "?").strip(), str(filt or "?")])


def load(config) -> dict:
    d = store.read_json(tune_path(config)) or {}
    d.setdefault("entries", {})
    return d


def current_gain(config):
    """The guide camera gain as the newest PS-89 audit saw it (API, the
    profile or the guide log), else None."""
    try:
        from photonscript.scheduler import phd2_audit
        a = phd2_audit.load_latest(config) or {}
    except Exception:  # noqa: BLE001
        return None
    for r in a.get("rows") or []:
        if r.get("id") == "gain":
            return _num(r.get("current"))
    return None


# ------------------------------------------------------------------ writes

def record(config, rec: dict) -> dict:
    """One tuner measurement: appended to tonight's log, and the
    (profile, binning, gain, target, filter) entry updated."""
    now = datetime.utcnow()
    rec = dict(rec, t_utc=store.iso_z(now), night=store.night_of(config, now),
               kind="measure")
    store.append_jsonl(night_log_path(config, rec["night"]), rec)
    d = load(config)
    k = entry_key(rec.get("profile"), rec.get("binning"), rec.get("gain"),
                  rec.get("target"), rec.get("filter"))
    e = d["entries"].get(k) or {"n": 0}
    e.update({kk: rec.get(kk) for kk in ("profile", "binning", "gain", "target",
                                          "filter", "exposure_ms", "peak_frac",
                                          "amp_frac", "snr", "hfd_px", "clipped",
                                          "in_band", "t_utc", "night")})
    e["n"] = int(e.get("n") or 0) + 1
    if rec.get("in_band") and rec.get("exposure_ms"):
        e["good_exposure_ms"] = rec["exposure_ms"]
    d["entries"][k] = e
    try:
        store.write_json(tune_path(config), d)
    except OSError as ex:
        logger.warning("tuning memory not saved: %s", ex)
    return rec


def record_change(config, change: dict) -> dict:
    """A tuner change (live exposure or a pre-dusk gain / binning write):
    tonight's log plus the PS-89 changes.jsonl."""
    now = datetime.utcnow()
    rec = dict(change, t_utc=store.iso_z(now), night=store.night_of(config, now),
               kind="change", source="PS-90 tuner")
    store.append_jsonl(night_log_path(config, rec["night"]), rec)
    try:
        from photonscript.scheduler import phd2_audit
        phd2_audit._record_change(config, {"id": f"tune_{change.get('kind')}",
                                           "ok": change.get("ok"), "kind": "tune",
                                           "from": change.get("from"),
                                           "to": change.get("to"),
                                           "note": change.get("reason")})
    except Exception as e:  # noqa: BLE001
        logger.debug("audit change log skipped: %s", e)
    return rec


def night_records(config, night: str) -> list[dict]:
    return store.read_jsonl(night_log_path(config, night))


# ------------------------------------------------------------------ recall

def _rate(e: dict) -> float | None:
    """Star amplitude (fraction of full scale) per ms of exposure; None for
    a clipped or 8-bit reading (its amplitude is not linear)."""
    a, ms = _num(e.get("amp_frac")), _num(e.get("exposure_ms"))
    if not a or not ms or e.get("clipped"):
        return None
    return a / ms


def _same_setup(e: dict, profile, binning, gain) -> bool:
    return (str(e.get("profile")) == str(profile)
            and str(e.get("binning")) == str(binning)
            and _num(e.get("gain")) == _num(gain))


def filter_ratios(entries: list[dict]) -> dict:
    """Median flux rate of each filter relative to L over targets that have
    both (the OAG behind the wheel sees narrowband stars fade)."""
    by_t: dict = {}
    for e in entries:
        r = _rate(e)
        if r:
            by_t.setdefault(e.get("target"), {})[e.get("filter")] = r
    out: dict = {REF_FILTER: 1.0}
    acc: dict = {}
    for rates in by_t.values():
        ref = rates.get(REF_FILTER)
        if not ref:
            continue
        for f, r in rates.items():
            if f != REF_FILTER:
                acc.setdefault(f, []).append(r / ref)
    for f, xs in acc.items():
        out[f] = round(statistics.median(xs), 4)
    return out


def recall(config, profile, binning, gain, target, filt, cfg=None) -> dict | None:
    """{"exposure_ms", "source"} for a target + filter, or None."""
    from photonscript.telescope_agent.guide_tuner import tune_cfg
    cfg = cfg or tune_cfg(config)
    ents = [e for e in load(config)["entries"].values()
            if _same_setup(e, profile, binning, gain)]
    for e in ents:
        if e.get("target") == target and e.get("filter") == filt and e.get("good_exposure_ms"):
            return {"exposure_ms": int(e["good_exposure_ms"]), "source": "remembered"}
    ratios = filter_ratios(ents)

    def want(rate, bkg=0.0):
        return int(round(max(0.05, cfg.target - bkg) / rate)) if rate else None
    if filt in ratios:
        mine = [e for e in ents if e.get("target") == target and _rate(e)
                and e.get("filter") in ratios]
        mine.sort(key=lambda e: (e.get("filter") != REF_FILTER, str(e.get("t_utc"))))
        if mine:
            e = mine[0]
            rate = _rate(e) * ratios[filt] / ratios[e["filter"]]
            return {"exposure_ms": want(rate), "source":
                    f"predicted from {e['filter']} x {ratios[filt] / ratios[e['filter']]:.2f}"}
    rates = [_rate(e) for e in ents if e.get("filter") == filt and _rate(e)]
    if rates:
        return {"exposure_ms": want(statistics.median(rates)),
                "source": f"typical {filt} star over {len(rates)} targets"}
    return None


# ------------------------------------------------------------------ recommend

def _recent(config, nights: int) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(days=nights + 1)).strftime("%Y-%m-%d")
    return [e for e in load(config)["entries"].values()
            if str(e.get("night") or "") >= cutoff]


def bin3_report(hfd_px, binning, cfg) -> dict:
    """Would bin 3 put the guide star's HFD in the target band? HFD scales
    with 1 / binning."""
    if not hfd_px or not binning:
        return {"hfd_px": None, "note": "no HFD measured yet"}
    h3 = float(hfd_px) * int(binning) / 3.0
    hits_now = cfg.hfd_lo <= float(hfd_px) <= cfg.hfd_hi
    hits3 = cfg.hfd_lo <= h3 <= cfg.hfd_hi
    return {"binning": int(binning), "hfd_px": round(float(hfd_px), 2),
            "hfd_px_at_bin3": round(h3, 2), "target": [cfg.hfd_lo, cfg.hfd_hi],
            "hits_target_now": hits_now, "bin3_hits_target": hits3,
            "note": (f"HFD {float(hfd_px):.1f} px at bin {binning}; bin 3 would give "
                     f"about {h3:.1f} px, {'inside' if hits3 else 'outside'} the "
                     f"{cfg.hfd_lo:g} to {cfg.hfd_hi:g} px target"
                     + (" (and about 2.25x the signal per pixel)" if int(binning) == 2 else ""))}


def recommend(config, nights: int = RECENT_NIGHTS) -> dict:
    """Next night's gain and binning from the recent measurements of the
    newest setup. change=False when nothing needs to move or there is no
    data."""
    from photonscript.telescope_agent.guide_tuner import tune_cfg
    cfg = tune_cfg(config)
    ents = sorted(_recent(config, nights), key=lambda e: str(e.get("t_utc")))
    if not ents:
        return {"ok": False, "change": False, "note": "no tuning measurements yet"}
    last = ents[-1]
    setup = {k: last.get(k) for k in ("profile", "binning", "gain")}
    ents = [e for e in ents if _same_setup(e, setup["profile"], setup["binning"],
                                           setup["gain"])]
    gain = _num(setup["gain"])
    by_f: dict = {}
    for e in ents:
        by_f.setdefault(e.get("filter") or "?", []).append(e)
    rows = {}
    need_up, need_down = [], []
    for f, es in sorted(by_f.items()):
        rates = [r for r in (_rate(e) for e in es) if r]
        clipped = sum(1 for e in es if e.get("clipped"))
        bkg = statistics.median([max(0.0, (_num(e.get("peak_frac")) or 0)
                                     - (_num(e.get("amp_frac")) or 0)) for e in es])
        target_amp = max(0.05, cfg.target - bkg)
        row = {"measurements": len(es), "clipped": clipped,
               "snr": _med([e.get("snr") for e in es]),
               "hfd_px": _med([e.get("hfd_px") for e in es]),
               "peak_frac": _med([e.get("peak_frac") for e in es])}
        if rates:
            r = statistics.median(rates)
            at_max, at_min = r * cfg.exp_hi, r * cfg.exp_lo
            row["peak_at_max_exp"] = round(at_max + bkg, 3)
            row["peak_at_min_exp"] = round(at_min + bkg, 3)
            if at_max + bkg < cfg.peak_lo:
                need_up.append(target_amp / at_max)
                row["verdict"] = "faint at the longest exposure"
            elif at_min + bkg > cfg.peak_hi:
                need_down.append(target_amp / at_min)
                row["verdict"] = "too bright at the shortest exposure"
            else:
                row["verdict"] = "exposure alone reaches the band"
        elif clipped:
            need_down.append(0.5)
            row["verdict"] = "clipped (no linear reading)"
        else:
            row["verdict"] = "no usable reading"
        rows[f] = row
    hfd = _med([e.get("hfd_px") for e in ents])
    out = {"ok": True, "current": setup, "by_filter": rows,
           "binning": setup["binning"], "gain": gain, "change": False,
           "bin3": bin3_report(hfd, setup["binning"], cfg),
           "nights": nights, "measurements": len(ents)}
    if gain is None:
        out["note"] = "guide gain unknown (no audit has read it): no gain advice"
        return out
    if need_up:
        k = max(need_up)
        g = min(GAIN_MAX, math.ceil(gain * k / GAIN_STEP) * GAIN_STEP)
        out["note"] = (f"faintest filter needs {k:.1f}x the signal at "
                       f"{cfg.exp_hi} ms: gain {gain:g} -> {g:g}")
        if gain * k > GAIN_MAX:
            out["note"] += (f"; even gain {GAIN_MAX} is short: bin 3 is the next "
                            "step (approved: stay at bin 2 until decided from data)")
        if need_down:
            out["note"] += "; some filters are too bright at the shortest exposure (kept: a lost star is worse)"
        out["gain"] = g
    elif need_down:
        k = min(need_down)
        g = max(GAIN_MIN, math.floor(gain * k / GAIN_STEP) * GAIN_STEP)
        out["note"] = (f"brightest filter clips at {cfg.exp_lo} ms: gain "
                       f"{gain:g} -> {g:g}")
        out["gain"] = g
    else:
        out["note"] = "exposure alone covers every filter: keep gain and binning"
    out["change"] = out["gain"] != gain
    return out


def _med(xs):
    xs = [float(x) for x in xs if _num(x) is not None]
    return round(statistics.median(xs), 3) if xs else None


# ------------------------------------------------------------------ summary

def summary(config, date: str) -> dict:
    """The night's tuning: per-filter peak / SNR / HFD / exposure, the
    changes, the last measurement and the recommendation."""
    from photonscript.telescope_agent.guide_tuner import tune_mode
    recs = night_records(config, date)
    meas = [r for r in recs if r.get("kind") == "measure"]
    changes = [r for r in recs if r.get("kind") == "change"]
    by_f: dict = {}
    for r in meas:
        by_f.setdefault(r.get("filter") or "?", []).append(r)
    table = {f: {"measurements": len(rs),
                 "exposure_ms": _med([r.get("exposure_ms") for r in rs]),
                 "peak_pct": (round(100 * _med([r.get("peak_frac") for r in rs]), 1)
                              if _med([r.get("peak_frac") for r in rs]) is not None else None),
                 "snr": _med([r.get("snr") for r in rs]),
                 "hfd_px": _med([r.get("hfd_px") for r in rs]),
                 "clipped": sum(1 for r in rs if r.get("clipped")),
                 "in_band_pct": round(100.0 * sum(1 for r in rs if r.get("in_band")) / len(rs), 0)}
             for f, rs in sorted(by_f.items())}
    last = meas[-1] if meas else None
    try:
        rec = recommend(config)
    except Exception as e:  # noqa: BLE001
        rec = {"ok": False, "note": f"recommend failed: {e}"}
    return {"date": date, "mode": tune_mode(config),
            "measurements": len(meas), "by_filter": table,
            "changes": [{k: c.get(k) for k in ("t_utc", "kind", "from", "to", "ok",
                                                "reason", "target", "filter")}
                        for c in changes],
            "last": {k: (last or {}).get(k) for k in (
                "t_utc", "target", "filter", "exposure_ms", "peak_frac", "snr",
                "hfd_px", "clipped", "bit8", "in_band", "would", "decision",
                "binning", "gain")} if last else None,
            "last_change": changes[-1].get("t_utc") if changes else None,
            "recommend": rec}


# ------------------------------------------------------------------ pre-dusk

def apply_predusk(config, armer_state: str = "ARMED") -> dict:
    """Write the recommended gain / binning into PHD2's stored profile before
    dusk (Option B), through the PS-89 writer. Only with phd2_audit_autofix,
    PHD2 closed, a resolvable profile, a backup and verified registry names
    (the writer refuses an unverified one). Never raises."""
    from photonscript.scheduler import phd2_profile_store as ps
    try:
        rec = recommend(config)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "note": f"recommend failed: {e}"}
    out = {"recommend": rec, "written": {}, "ok": False}
    if not rec.get("change"):
        out.update(ok=True, note=rec.get("note") or "no change recommended")
        return out
    changes = {}
    cur = rec.get("current") or {}
    if rec.get("gain") is not None and _num(rec["gain"]) != _num(cur.get("gain")):
        changes["gain"] = int(rec["gain"])
    if rec.get("binning") and str(rec["binning"]) != str(cur.get("binning")):
        changes["binning"] = int(rec["binning"])
    if not getattr(config, "phd2_audit_autofix", False):
        out["note"] = (f"recommended {changes} not written: phd2_audit_autofix is "
                       "off (set it by hand, or turn autofix on)")
        return out
    if ps.phd2_running():
        out["note"] = "PHD2 is running: the profile is written only while it is closed"
        return out
    pid = ps.resolve_id(None, cur.get("profile"))
    if pid is None:
        out["note"] = "PHD2 profile id unknown"
        return out
    b = ps.backup(config, pid)
    if not b.get("ok"):
        out["note"] = f"no backup, not written ({b.get('note')})"
        return out
    res = ps.write(config, pid, changes, backup_path=b["reg"])
    out.update(written=res.get("written") or {}, refused=res.get("refused") or {},
               backup=b["reg"], ok=bool(res.get("ok")),
               note=res.get("note") or "; ".join(f"{k}: {v}" for k, v in
                                                 (res.get("refused") or {}).items()) or None)
    for k, v in (res.get("written") or {}).items():
        record_change(config, {"kind": k, "from": cur.get(k), "to": v, "ok": True,
                               "reason": f"pre-dusk ({armer_state}): {rec.get('note')}",
                               "profile": cur.get("profile")})
    return out
