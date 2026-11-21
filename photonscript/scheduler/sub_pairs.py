"""The two rigs' subs as linked pairs (sub doctor; also read by the night pilot).

Both rigs ride one mount and look at the same sky, so a sub on one rig and
the sub(s) the other rig exposed at the same time tell causes apart that one
rig alone cannot:

    both soft, both round          seeing / transparency (the sky)
    RC16 soft, Piggy-600 sharp     that rig's focus (and vice versa)
    both trailing                  the shared mount's tracking
    only one trailing              that rig's guider or flexure
    both star counts down          clouds

Pure functions over subs-log records (runs/<night>_subs.jsonl rows); nothing
is read from disk here and nothing is written.

    sub_state(rec, baseline=None) -> {ok, bad, soft, trailing, stars_low,
                                      excluded, why, hfr, ecc, fwhm, stars,
                                      background, axis_deg, axis_r}
    overlapping(rec, others, min_overlap_s) -> [(other rec, overlap s)]
    pair_for(rig, sub, subs, baselines=None) -> {rig, file, partners: [...],
                                      verdict, why}
    paired_summary(subs, rig, n=3, end=None) -> the pair verdicts of the
                                      rig's last n subs and the dominant one
    baselines(subs) -> {(rig, filter): {stars, hfr, background}} medians of
                                      the night's good subs (the "night
                                      median" a star count is compared with)

Verdicts (PAIR_*): seeing, focus_<rig>, mount, guider_<rig> (only that rig
trailing), clouds, both_ok, one_bad_<rig> (one rig bad for another reason),
unpaired (no overlapping sub on the other rig), excluded.
"""
from __future__ import annotations

import statistics
from collections import Counter

from photonscript.shared.sub_time import sub_window

# stars below this fraction of the night median (same rig and filter) count
# as "down" even when the star-count gate passes
STARS_DROP = 0.5
# a soft sub: the HFR gate failed, or HFR this far over the night median
HFR_RISE = 1.3
MIN_OVERLAP_S = 30.0

# scorecard rows that say the sub is not a sky problem at all (the roof was
# closed, it straddled a slew): such subs are left out of every diagnosis
EXCLUDE_ROWS = ("roof", "slew_straddle")


def rows(rec: dict) -> dict:
    """{check id: (value, limit, status, text)} of a sub's PS-21 scorecard."""
    out = {}
    for r in ((rec or {}).get("scorecard") or {}).get("rows") or []:
        if isinstance(r, (list, tuple)) and r:
            r = list(r) + [None] * 5
            out[str(r[0])] = (r[1], r[2], r[3], r[4])
    return out


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def is_test(rec: dict) -> bool:
    try:
        from photonscript.shared.qa_rules import is_test_record
        return bool(is_test_record(rec))
    except Exception:  # noqa: BLE001
        return bool(rec.get("test"))


def baselines(subs: list[dict]) -> dict:
    """Per (rig, filter): medians of the night's subs that passed QA (all
    measured subs when none passed yet). Test subs are left out."""
    groups: dict[tuple, list] = {}
    for r in subs or []:
        if is_test(r) or r.get("stars") in (None, 0):
            continue
        groups.setdefault((r.get("rig") or "rc16", r.get("filter")), []).append(r)
    out = {}
    for k, rs in groups.items():
        good = [r for r in rs if r.get("passed_qa")] or rs
        out[k] = {"stars": _med([_num(r.get("stars")) for r in good]),
                  "hfr": _med([_num(r.get("hfr")) for r in good]),
                  "background": _med([_num(r.get("background")) for r in good]),
                  "n": len(good), "from_passed": any(r.get("passed_qa") for r in rs)}
    return out


def sub_state(rec: dict, baseline: dict | None = None) -> dict:
    """What is wrong with one sub, from its scorecard (and the night
    baseline for its rig and filter)."""
    rw = rows(rec)

    def st(k):
        return (rw.get(k) or (None, None, None, None))[2]

    out = {"file": rec.get("file"), "rig": rec.get("rig") or "rc16",
           "filter": rec.get("filter"), "exp_s": _num(rec.get("exp_s")),
           "hfr": _num(rec.get("hfr")), "ecc": _num(rec.get("ecc")),
           "fwhm": _num(rec.get("fwhm_arcsec")), "stars": _num(rec.get("stars")),
           "background": _num(rec.get("background")),
           "axis_deg": _num(rec.get("elong_axis_deg")),
           "axis_r": _num(rec.get("elong_r")),
           "axis_tang": _num(rec.get("elong_tang")),
           "guide_state": rec.get("guide_state"),
           "excluded": None, "fails": [k for k, v in rw.items() if v[2] == "fail"]}
    if is_test(rec):
        out["excluded"] = "test sub"
    for k in EXCLUDE_ROWS:
        if st(k) == "fail":
            out["excluded"] = f"{k} failed"
    if str(rec.get("auto_reason") or rec.get("reason") or "").startswith("dark-frame"):
        out["excluded"] = "dark-frame signature"
    no_stars = not out["stars"] or out["hfr"] in (None, 0.0)
    trailing = st("ecc") == "fail" or (st("ecc") in (None, "skip")
                                       and st("ecc_bin") == "fail")
    soft = st("hfr") == "fail" or st("fwhm") == "fail" or st("hfr_rel") == "fail"
    b = baseline or {}
    if not soft and out["hfr"] and b.get("hfr") and b.get("from_passed"):
        soft = out["hfr"] > HFR_RISE * b["hfr"]
    stars_low = st("stars") == "fail" or no_stars
    ratio = None
    if out["stars"] is not None and b.get("stars"):
        ratio = out["stars"] / b["stars"]
        stars_low = stars_low or (ratio < STARS_DROP and b.get("from_passed"))
    out.update(no_stars=no_stars, trailing=bool(trailing), soft=bool(soft),
               stars_low=bool(stars_low), stars_ratio=None if ratio is None
               else round(ratio, 2),
               pointing=st("pointing") == "fail",
               focuser_zero=(st("focuser_zero") == "fail"
                             or rec.get("focpos") == 0),
               bad=bool(out["fails"]) or bool(trailing or soft or stars_low))
    out["ok"] = not out["bad"] and not out["excluded"]
    return out


def overlapping(rec: dict, others: list[dict],
                min_overlap_s: float = MIN_OVERLAP_S) -> list[tuple[dict, float]]:
    """The other records whose exposure overlaps rec's by at least
    min_overlap_s (or half the shorter sub), largest overlap first."""
    w = sub_window(rec)
    if w is None:
        return []
    out = []
    for o in others or []:
        if o is rec:
            continue
        v = sub_window(o)
        if v is None:
            continue
        ov = (min(w[1], v[1]) - max(w[0], v[0])).total_seconds()
        shorter = min((w[1] - w[0]).total_seconds(), (v[1] - v[0]).total_seconds())
        if ov > 0 and ov >= min(min_overlap_s, 0.5 * shorter):
            out.append((o, ov))
    out.sort(key=lambda t: -t[1])
    return out


def _verdict(rig: str, a: dict, partners: list[dict]) -> tuple[str, str]:
    other = partners[0]["rig"] if partners else None
    if a.get("excluded"):
        return "excluded", a["excluded"]
    live = [p for p in partners if not p.get("excluded")]
    if not live:
        return "unpaired", ("no overlapping sub on the other rig" if not partners
                            else "the other rig's overlapping sub is excluded")
    b_trail = any(p["trailing"] for p in live)
    b_soft = any(p["soft"] for p in live)
    b_stars = any(p["stars_low"] for p in live)
    b_ok = all(p["ok"] for p in live)
    if a["stars_low"] and b_stars:
        return "clouds", "star counts down on both rigs"
    if a["trailing"] and b_trail:
        return "mount", "both rigs trailing at the same time (shared mount)"
    if a["trailing"] and not b_trail:
        return f"guider_{rig}", f"only {rig} trailing; {other} round"
    if b_trail and not a["trailing"]:
        return f"guider_{other}", f"only {other} trailing; {rig} round"
    if a["soft"] and b_soft:
        return "seeing", "both rigs soft and round at the same time"
    if a["soft"] and not b_soft:
        return f"focus_{rig}", f"{rig} soft, {other} sharp"
    if b_soft and not a["soft"]:
        return f"focus_{other}", f"{other} soft, {rig} sharp"
    if a["ok"] and b_ok:
        return "both_ok", "both rigs good"
    return (f"one_bad_{rig if a['bad'] else other}",
            "one rig bad for another reason")


def pair_for(rig: str, sub: dict, subs: list[dict],
             bases: dict | None = None) -> dict:
    """The sub and the other rig's overlapping sub(s), each judged by
    sub_state, with the pair verdict."""
    bases = baselines(subs) if bases is None else bases
    rig = rig or sub.get("rig") or "rc16"
    others = [r for r in subs or [] if (r.get("rig") or "rc16") != rig]
    a = sub_state(sub, bases.get((rig, sub.get("filter"))))
    partners = []
    for o, ov in overlapping(sub, others):
        s = sub_state(o, bases.get((o.get("rig") or "rc16", o.get("filter"))))
        s["overlap_s"] = round(ov, 1)
        partners.append(s)
    v, why = _verdict(rig, a, partners)
    return {"rig": rig, "file": sub.get("file"), "start_utc": sub.get("start_utc"),
            "sub": a, "partners": partners, "verdict": v, "why": why}


def rig_subs(subs: list[dict], rig: str) -> list[dict]:
    """The rig's subs in exposure order."""
    rs = [r for r in subs or [] if (r.get("rig") or "rc16") == rig]

    def key(r):
        w = sub_window(r)
        return w[0].isoformat() if w else str(r.get("time") or "")
    return sorted(rs, key=key)


def paired_summary(subs: list[dict], rig: str, n: int = 3,
                   end: dict | None = None, bases: dict | None = None) -> dict:
    """Pair verdicts of the rig's last n subs (up to and including `end`
    when given), and the most common verdict among the non-excluded ones."""
    bases = baselines(subs) if bases is None else bases
    rs = rig_subs(subs, rig)
    if end is not None:
        try:
            rs = rs[:rs.index(end) + 1]
        except ValueError:
            pass
    pairs = [pair_for(rig, r, subs, bases) for r in rs[-max(1, int(n)):]]
    c = Counter(p["verdict"] for p in pairs if p["verdict"] not in ("excluded",))
    dom = c.most_common(1)[0] if c else (None, 0)
    return {"rig": rig, "n": len(pairs), "pairs": pairs,
            "counts": dict(c), "dominant": dom[0], "dominant_n": dom[1]}
