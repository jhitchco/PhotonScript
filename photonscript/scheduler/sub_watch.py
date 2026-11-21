"""Night pilot sub watch: judge every graded sub as it lands (both rigs,
paired by time) and turn what it shows into a pilot diagnosis.

Jeremy (2026-10-14): "augment the night watch and pilot to watch the subs
as they come in to adjust if something doesn't work." The sub doctor speaks
only after failed subs (2 bad of the last 3); this watch reads every new
graded light and also sees what the QA gates miss:

  focus drift      the rig's HFR has risen over the night's best for that
                   target and filter (the median of its 3 sharpest good
                   subs) by night_pilot_watch_hfr_rise (20%) on each of the
                   last night_pilot_watch_subs (3) round subs. Not focus
                   when the other rig's subs exposed at the same time rose
                   too (the sky: seeing / transparency), when the stars
                   trail (a mount cause inflates the HFR), or when an
                   autofocus finished after the softening began and the
                   subs right after it were as soft (the AF already found
                   the best focus it could). Action: refocus.
  tracking drift   2 of the last 3 RC16 subs trail (ecc gate failed) on an
                   UNGUIDED night with PHD2 idle, the stars not down, and
                   the sub longer than the learned unguided limit of the
                   region it points at (scheduler/unguided_limits.py: Dec
                   band x hour-angle band). The Piggy-600 is the witness:
                   trailing too = the shared mount; round = still the
                   mount when the RC16's trail rate, at the Piggy-600's
                   1.29"/px and sub length, predicts an ecc under its gate
                   (the RC16 at 0.236"/px sees a drift the Piggy-600
                   cannot); round although it should trail = only the RC16
                   (flexure, its optics): a human, no change. Action:
                   limit_subs (the goal's RC16 subs cut to the limit, the
                   longest ladder length at or under it with matched darks:
                   unguided_limits.snap_length).
  guider on an     the newest sub of either rig was exposed with PHD2
  unguided night   guiding / settling / lost star on an UNGUIDED night (or
                   PHD2 reads active now). Immediate: one priority push and
                   action guider_stop (the pilot_hooks hook, then PHD2
                   stop_capture).
  clouds           both rigs' star counts down at the same time: noted only.
  roof             a sub with the roof row failed (closed / parked): noted;
                   the closed-roof darks (closed_roof_plan) own that time.

Pure functions over subs-log records; nothing is read from disk or the
network here and nothing is written. The night pilot (night_pilot.tick)
calls signals() each tick and runs what comes back through its own policy,
guards, rate limits and verification like a sub doctor diagnosis. Each
signal is a dict in night_pilot.norm_diag's shape plus "source": "watch".
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime

SOURCE = "watch"
PHD2_ACTIVE = ("guiding", "settling", "lost_star", "lostlock", "lost lock",
               "calibrating", "looping", "selected")
BEST_N = 3              # the night's best: median of the 3 sharpest good subs
PARTNER_RISE_SHARE = 0.5  # the other rig rose by half the threshold = the sky


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _f(cfg, key: str, default: float) -> float:
    v = _num(getattr(cfg, key, default))
    return float(default) if v is None else v


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def watch_on(cfg) -> bool:
    return bool(getattr(cfg, "night_pilot_watch", True))


def window_n(cfg) -> int:
    return max(2, int(_f(cfg, "night_pilot_watch_subs", 3)))


def hfr_rise(cfg) -> float:
    return max(0.05, _f(cfg, "night_pilot_watch_hfr_rise", 0.20))


# --------------------------------------------------------------------------
# sub helpers
# --------------------------------------------------------------------------

def graded_at(r: dict) -> datetime | None:
    """When the sub was graded (the subs-log "time"), else its end."""
    from photonscript.shared.sub_time import parse_utc, sub_end
    return parse_utc(r.get("time")) or sub_end(r)


def lights(subs: list[dict], rig: str) -> list[dict]:
    """The rig's measured lights in exposure order (no test subs)."""
    from photonscript.scheduler import sub_pairs as sp
    from photonscript.shared.sub_time import sub_window
    out = []
    for r in sp.rig_subs(subs, rig):
        if sp.is_test(r) or sub_window(r) is None:
            continue
        if not _num(r.get("exp_s")):
            continue
        out.append(r)
    return out


def _key(r: dict) -> tuple:
    from photonscript.shared.target_names import target_key
    try:
        t = target_key(str(r.get("target") or "?"))
    except Exception:  # noqa: BLE001
        t = str(r.get("target") or "?").lower()
    return (t, str(r.get("filter") or ""))


def _state(r: dict) -> dict:
    from photonscript.scheduler.sub_pairs import sub_state
    return sub_state(r)


def _good_for_best(r: dict, s: dict) -> bool:
    return (not s.get("excluded") and not s.get("trailing") and not s.get("stars_low")
            and _num(r.get("hfr")) not in (None, 0.0))


def bests(subs: list[dict], rig: str) -> dict:
    """{(target key, filter): best HFR} of the rig: the median of its
    BEST_N sharpest round subs (needs BEST_N of them)."""
    groups: dict[tuple, list] = {}
    for r in lights(subs, rig):
        if _good_for_best(r, _state(r)):
            groups.setdefault(_key(r), []).append(float(r["hfr"]))
    out = {}
    for k, v in groups.items():
        if len(v) >= BEST_N:
            out[k] = round(statistics.median(sorted(v)[:BEST_N]), 2)
    return out


def guiding(r: dict) -> bool:
    return str(r.get("guide_state") or "").strip().lower() in PHD2_ACTIVE


def _ecc_gate(r: dict) -> float | None:
    from photonscript.scheduler.sub_pairs import rows
    v = rows(r).get("ecc")
    return _num(v[1]) if v else None


def _scale(cfg, rig: str) -> float:
    key = "piggyback_pixel_scale_arcsec" if rig == "piggyback" else "pixel_scale_arcsec"
    return _f(cfg, key, 1.29 if rig == "piggyback" else 0.236) or 0.236


def partners(r: dict, subs: list[dict]) -> list[dict]:
    from photonscript.scheduler.sub_pairs import overlapping
    rig = r.get("rig") or "rc16"
    others = [o for o in subs or [] if (o.get("rig") or "rc16") != rig]
    return [o for o, _ov in overlapping(r, others)]


def predicted_partner_ecc(cfg, r: dict, p: dict) -> float | None:
    """The ecc the other rig's sub would show if this rig's trail were the
    mount's: this rig's trail rate (sub_doctor.trail_estimate) over the
    partner's exposure at the partner's scale, against its HFR."""
    from photonscript.scheduler.sub_doctor import trail_estimate
    rig, prig = r.get("rig") or "rc16", p.get("rig") or "rc16"
    tr = trail_estimate(r.get("hfr"), r.get("ecc"), r.get("exp_s"), _scale(cfg, rig))
    rate = _num(tr.get("rate_arcsec_min"))
    hfr_p, exp_p = _num(p.get("hfr")), _num(p.get("exp_s"))
    if not rate or not hfr_p or not exp_p:
        return None
    length_px = rate * exp_p / 60.0 / _scale(cfg, prig)
    k = length_px / (math.sqrt(12.0) * hfr_p / 1.1774)
    return round(k / math.sqrt(1.0 + k * k), 3)


# --------------------------------------------------------------------------
# the signals
# --------------------------------------------------------------------------

def _sig(rig: str, at: datetime, cause: str, family: str, action: str,
         confidence: str, headline: str, **evidence) -> dict:
    return {"t": _iso(at), "dt": at, "rig": rig, "cause": cause, "family": family,
            "confidence": confidence, "action": action, "action_arg": None,
            "evidence": evidence, "headline": headline, "secondary": [],
            "source": SOURCE}


def guider_signal(cfg, rig: str, last: dict, subs: list[dict], ctx: dict,
                  at: datetime) -> dict | None:
    """PHD2 active on an UNGUIDED night (the newest sub, or PHD2 now)."""
    if ctx.get("guided"):
        return None
    live = str(ctx.get("phd2") or "").strip().lower().replace(" ", "")
    live_active = live in [x.replace(" ", "") for x in PHD2_ACTIVE]
    if not (guiding(last) or live_active):
        return None
    both = [x["rig"] if "rig" in x else "rc16" for x in [last, *partners(last, subs)]
            if guiding(x)]
    s = _state(last)
    why = (f"PHD2 {last.get('guide_state') or ctx.get('phd2')} on an UNGUIDED night "
           f"during the {_label(cfg, rig)} sub of {_hm(cfg, _start(last))}"
           + (f" (ecc {last.get('ecc')}, trailing)" if s.get("trailing") else
              f" (ecc {last.get('ecc')})"))
    return _sig(rig, at, "guider_conflict", "guider_conflict", "guider_stop",
                "high" if len(set(both)) > 1 or s.get("trailing") else "medium",
                why, guide_state=last.get("guide_state"), phd2_live=ctx.get("phd2"),
                rigs_guiding=sorted(set(both)), ecc=_num(last.get("ecc")),
                file=last.get("file"))


def _start(r):
    from photonscript.shared.sub_time import sub_start
    return sub_start(r)


def _label(cfg, rig: str) -> str:
    try:
        from photonscript.shared.rigs import rig_label
        return rig_label(cfg, rig)
    except Exception:  # noqa: BLE001
        return rig


def _hm(cfg, t) -> str:
    try:
        from photonscript.scheduler.nina_watchdog import _local_hm
        return _local_hm(cfg, t)
    except Exception:  # noqa: BLE001
        return t.strftime("%H:%MZ") if t else "?"


def _ratio(r: dict, best: dict) -> float | None:
    b = best.get(_key(r))
    h = _num(r.get("hfr"))
    return round(h / b, 3) if b and h else None


def focus_signal(cfg, rig: str, win: list[dict], subs: list[dict], ctx: dict,
                 at: datetime) -> dict | None:
    """HFR risen over the night's best on each of the last n round subs."""
    rise = hfr_rise(cfg)
    best = bests(subs, rig)
    last = win[-1]
    ratios = []
    for r in win:
        s = _state(r)
        if s.get("excluded") or s.get("stars_low") or s.get("trailing"):
            return None
        if _key(r)[0] != _key(last)[0]:
            return None            # one target at a time
        q = _ratio(r, best)
        if q is None or q < 1.0 + rise:
            return None
        ratios.append(q)
    b = best.get(_key(last))
    ev = {"hfr": _num(last.get("hfr")), "best_hfr": b, "filter": last.get("filter"),
          "ratios": ratios, "n": len(win), "target": last.get("target")}
    pct = round((statistics.median(ratios) - 1.0) * 100)
    head = (f"{_label(cfg, rig)} HFR {ev['hfr']} px vs the night's best {b} px "
            f"({last.get('filter')}, +{pct}%) on the last {len(win)} round subs")
    # the other rig at the same time: rose too = the sky
    other = [o for o in ("rc16", "piggyback") if o != rig][0]
    obest = bests(subs, other)
    pr = []
    for r in win:
        for p in partners(r, subs):
            q = _ratio(p, obest)
            if q is not None:
                pr.append(q)
    ev["partner_ratio"] = round(statistics.median(pr), 3) if pr else None
    if pr and ev["partner_ratio"] >= 1.0 + rise * PARTNER_RISE_SHARE:
        return _sig(rig, at, "seeing", "seeing", "none", "medium",
                    head + f"; the {_label(cfg, other)} rose too (x{ev['partner_ratio']}): "
                    "the sky, not focus", **ev)
    # an AF after the softening began that did not bring it back
    if ctx.get("af_finished") is None:
        # the caller reads NINA's AF history only when a signal needs it
        return _sig(rig, at, "focus_drift", "focus", "refocus", "low",
                    head, needs_af=True, **ev)
    afs = sorted(t for t in (ctx.get("af_finished") or {}).get(rig, []) if t <= at)
    first_soft = _start(win[0])
    rig_ls = lights(subs, rig)
    soft_since = first_soft
    for r in reversed(rig_ls[:max(0, len(rig_ls) - len(win))]):
        q = _ratio(r, best)
        if q is None or q < 1.0 + rise:
            break
        soft_since = _start(r)
    last_af = afs[-1] if afs else None
    ev["last_af"] = _iso(last_af)
    if last_af is not None and soft_since is not None and last_af >= soft_since:
        after = [r for r in lights(subs, rig) if _start(r) and _start(r) >= last_af]
        if after and (_ratio(after[0], best) or 0) >= 1.0 + rise:
            return _sig(rig, at, "seeing", "seeing", "none", "medium",
                        head + f"; the autofocus of {_hm(cfg, last_af)} did not bring "
                        "it back (the next sub as soft): not focus drift", **ev)
    conf = "high" if pr else "medium"
    return _sig(rig, at, "focus_drift", "focus", "refocus", conf,
                head + ("; the other rig is sharp" if pr else "; no other-rig sub "
                        "to compare"), other_rig_sharp=bool(pr), **ev)


def tracking_signal(cfg, rig: str, win: list[dict], subs: list[dict], ctx: dict,
                    at: datetime) -> dict | None:
    """RC16 trailing on an unguided night, sub longer than the region's
    learned unguided limit."""
    if ctx.get("guided") or rig != "rc16":
        return None
    states = [_state(r) for r in win]
    last, ls = win[-1], states[-1]
    trailing = [s for s in states if s.get("trailing")]
    if not ls.get("trailing") or len(trailing) < 2:
        return None
    if any(guiding(r) for r in win) or any(s.get("excluded") or s.get("stars_low")
                                           for s in trailing):
        return None
    ev = {"ecc": _num(last.get("ecc")), "exp_s": _num(last.get("exp_s")),
          "n_trailing": len(trailing), "n": len(win), "target": last.get("target")}
    from photonscript.scheduler.sub_doctor import trail_estimate
    tr = trail_estimate(last.get("hfr"), last.get("ecc"), last.get("exp_s"),
                        _scale(cfg, rig))
    ev["rate_arcsec_min"] = tr.get("rate_arcsec_min")
    head = (f"{_label(cfg, rig)} subs trail on an UNGUIDED night ({len(trailing)} of "
            f"the last {len(win)}, ecc {ev['ecc']}"
            + (f", ~{tr['rate_arcsec_min']}\"/min" if tr.get("rate_arcsec_min") else "")
            + ")")
    # the witness
    # the other rig's graded sub at the same time as the newest window sub
    # that has one (its subs are longer and grade later)
    ps = []
    for r in reversed(win):
        ps = partners(r, subs)
        if ps:
            break
    witness, conf = ("no graded sub of the other rig at the same time yet",
                     "medium")
    if ps:
        p = ps[0]
        pst = _state(p)
        ev["partner_ecc"] = _num(p.get("ecc"))
        if pst.get("trailing"):
            witness, conf = "both trailing (shared mount)", "high"
        else:
            preds = [predicted_partner_ecc(cfg, r, p) for r, s in zip(win, states)
                     if s.get("trailing")]
            preds = [x for x in preds if x is not None]
            pred = round(statistics.median(preds), 3) if preds else None
            gate = _ecc_gate(p)
            ev["partner_pred_ecc"], ev["partner_gate"] = pred, gate
            if pred is not None and gate is not None and pred >= gate:
                return _sig(rig, at, "tracking", "tracking", "none", "medium",
                            head + f"; the {_label(cfg, p.get('rig'))} should trail "
                            f"too at this rate (ecc ~{pred} >= {gate}) but is round "
                            f"(ecc {ev['partner_ecc']}): only this rig, flexure or "
                            "its optics", human=True, **ev)
            witness = (f"the {_label(cfg, p.get('rig'))} round as a mount drift "
                       f"predicts at its scale (ecc ~{pred})")
    ev["witness"] = witness
    tbl = ctx.get("limits") or {}
    lim = region_limit(cfg, tbl.get(rig), last)
    if lim is None:
        return _sig(rig, at, "tracking", "tracking", "none", "low",
                    head + "; no learned unguided limit for this sky region yet",
                    **ev)
    ev.update(region=lim.get("label"), limit_s=lim["limit_s"],
              limit_source=lim.get("source"), limit_confidence=lim.get("confidence"))
    exp = ev["exp_s"] or 0.0
    if exp <= lim["limit_s"] + 0.5:
        return _sig(rig, at, "tracking", "tracking", "none", "medium",
                    head + f"; already at the learned limit {lim['limit_s']:g} s for "
                    f"{lim.get('label')}", **ev)
    return _sig(rig, at, "tracking_limit", "tracking", "limit_subs", conf,
                head + f"; {witness}; the learned unguided limit for "
                f"{lim.get('label')} is {lim['limit_s']:g} s", **ev)


def region_limit(cfg, tbl: dict | None, r: dict) -> dict | None:
    """The learned limit of the sky region the sub pointed at (its mount
    position at mid-exposure)."""
    if not tbl:
        return None
    from photonscript.scheduler import unguided_limits as ul
    from photonscript.shared.sub_time import sub_window
    p = r.get("pointing") or {}
    ra, dec = _num(p.get("mount_ra")), _num(p.get("mount_dec"))
    w = sub_window(r)
    if ra is None or dec is None or w is None:
        return None
    mid = w[0] + (w[1] - w[0]) / 2
    _alt, ha = ul.alt_ha(ra, dec, mid, cfg)
    if ha is None:
        return None
    e = ul.lookup(tbl, dec, ha)
    if e is None:
        return None
    steps = list(tbl.get("ladder") or ul.ladder(cfg, "rc16"))
    return {"limit_s": ul._entry_limit(e, steps), "label": e.get("label"),
            "source": e.get("source"), "confidence": e.get("confidence"),
            "scope": e.get("scope"), "ha_h": round(ha, 2), "dec": round(dec, 1)}


def clouds_signal(cfg, rig: str, last: dict, subs: list[dict], at) -> dict | None:
    s = _state(last)
    if s.get("excluded") or not s.get("stars_low"):
        return None
    ps = [_state(p) for p in partners(last, subs)]
    if ps and any(p.get("stars_low") for p in ps):
        return _sig(rig, at, "clouds", "clouds", "none", "medium",
                    "star counts down on both rigs at the same time: clouds, "
                    "no change", stars=_num(last.get("stars")))
    return None


def roof_signal(cfg, rig: str, last: dict, at) -> dict | None:
    from photonscript.scheduler.sub_pairs import rows
    v = rows(last).get("roof")
    if v and v[2] == "fail":
        return _sig(rig, at, "roof", "roof", "none", "high",
                    f"{_label(cfg, rig)} sub with the roof closed / parked: no "
                    "change (closed-roof darks own that time)")
    return None


def signals(cfg, subs: list[dict], ctx: dict, seen: dict | None = None,
            now: datetime | None = None) -> tuple[list[dict], dict]:
    """The watch's signals for the subs graded since `seen` ({rig: graded
    time ISO of the last sub judged}), at most one per rig (the newest sub;
    guider > tracking > focus > clouds > roof), and the new `seen`.
    ctx: guided (bool), phd2 (live PHD2 state or None), limits ({rig: the
    unguided_limits table}), af_finished ({rig: [AUTOFOCUS-FINISHED
    datetimes]}). Subs graded after `now` are ignored (replay)."""
    from photonscript.shared.sub_time import parse_utc
    seen = dict(seen or {})
    out = []
    if now is not None:
        subs = [r for r in subs or [] if (graded_at(r) or now) <= now]
    n = window_n(cfg)
    for rig in ("rc16", "piggyback"):
        ls = lights(subs, rig)
        if not ls:
            continue
        mark = parse_utc(seen.get(rig))
        new = [r for r in ls if graded_at(r) and (mark is None or graded_at(r) > mark)]
        if not new:
            continue
        last = max(new, key=lambda r: graded_at(r))
        at = graded_at(last)
        seen[rig] = _iso(at)
        seen[f"{rig}|guiding"] = guiding(last)
        upto = [r for r in subs if (graded_at(r) or at) <= at]
        ls_upto = lights(upto, rig)
        win = ls_upto[-n:]
        sig = guider_signal(cfg, rig, last, upto, ctx, at)
        if sig is None and len(win) >= n:
            sig = tracking_signal(cfg, rig, win, upto, ctx, at)
            if sig is None:
                sig = focus_signal(cfg, rig, win, upto, ctx, at)
        if sig is None:
            sig = clouds_signal(cfg, rig, last, upto, at) or roof_signal(cfg, rig, last, at)
        if sig is not None:
            out.append(sig)
    return out, seen


# --------------------------------------------------------------------------
# closed-roof darks: what the idle closed-roof time can hold
# --------------------------------------------------------------------------

def closed_roof_plan(owed: dict, minutes_left: float, *, light_lengths: dict | None = None,
                     min_left_min: float = 30.0) -> list[dict]:
    """What the closed-roof time to dawn fills per rig: owed {rig: [{exp_s,
    owed}]} (the dark quota deficit), each rig's camera on its own NINA, in
    parallel; per rig the lengths its lights use tonight first
    (light_lengths {rig: [s]}; the Piggy-600's 450 s), then the rest in
    the order given (the sequence's), until the time runs out. [{rig, exp_s, owed, n}], [] with
    under min_left_min to dawn."""
    if minutes_left is None or minutes_left < min_left_min:
        return []
    out = []
    for rig in ("piggyback", "rc16"):
        rows = [x for x in (owed or {}).get(rig) or [] if (_num(x.get("owed")) or 0) > 0]
        lights_now = [float(s) for s in (light_lengths or {}).get(rig) or []]
        # stable: after the light lengths, the order the sequence shoots
        # them in (night_dark_exposures: the quota list, then the lights'
        # extra lengths, then the learned unguided limits, most used first)
        rows.sort(key=lambda x: 0 if any(abs(float(x["exp_s"]) - s) < 0.5
                                         for s in lights_now) else 1)
        left = float(minutes_left) * 60.0
        for x in rows:
            e = float(x["exp_s"])
            if e <= 0 or left < e:
                continue
            n = int(min(int(x["owed"]), left // (e + 2.0)))   # ~2 s download
            if n <= 0:
                continue
            out.append({"rig": rig, "exp_s": e, "owed": int(x["owed"]), "n": n})
            left -= n * (e + 2.0)
    return out


def plan_text(cfg, plan: list[dict]) -> str:
    if not plan:
        return "nothing owed"
    return "; ".join(f"{_label(cfg, p['rig'])} {p['exp_s']:g} s x{p['n']} "
                     f"(owed {p['owed']})" for p in plan)


def minutes_between(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0

