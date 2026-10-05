"""One normalized row per sub across every night (PS-81, Targets tab).

Built from runs._load_subs (already mtime-cached per night) and memoized on
the (name, mtime_ns, size) of every ``runs/*_subs.jsonl`` plus a signature of
the project list, so the Targets endpoints rebuild only when a log or a
project changes. Never touches Syncthing or FITS files (PS-24 pitfall).

Row (treat as read-only, rows are shared between callers)::

    key "date|rig|file", date, rig (missing = rc16), file, abs_path, time,
    target_raw, target (canonical, "?" = unattributed), target_key,
    project_id, filter, exp_s, hdr_short, verdict (pending|approved|
    rejected), verdict_by (auto|manual), reasons[], reason_codes[], qa_flag,
    metrics{hfr, fwhm_arcsec, ecc, ecc_bin, stars, background, ccd_temp,
    corner_ecc, doubled_frac}, thumb, pointing (None until PS-67)

Verdict: rejected = not passed_qa; approved = passed and reviewed (by a
person, approve_night, or the PS-21 auto-approve of an all-green sub);
pending = passed, not yet reviewed.

Reason codes are the PS-21 scorecard check ids (qa_rules.CHECKS: ecc, hfr,
hfr_rel, stars, temp, roof, tracking_jump, guide_rms, ...) plus "manual"
(rejected by a person) and "other". Records with a scorecard give their
failing checks directly (``drivers``, else the scorecard's fail rows); only
records graded before PS-21 fall back to the regex map over the reason text
(LEGACY_REASON_PATTERNS).

Functions: rows(config, projects, **filters), targets(config, projects),
target_detail(config, name, projects), best_subs(rows, config).
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from photonscript.shared.target_names import (
    UNATTRIBUTED,
    canonical_target,
    known_target_index,
    target_key,
)

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
VERDICTS = (APPROVED, PENDING, REJECTED)
MANUAL, OTHER = "manual", "other"
THUMB_W = 264

# Reason strings written before the PS-21 scorecard (agent image_validator,
# runs._fast_grade, flag_hfr_outliers, set_manual_qa, PS-71 signatures),
# mapped onto the scorecard check ids. First match wins, per "; " part.
LEGACY_REASON_PATTERNS: list[tuple[str, re.Pattern]] = [(c, re.compile(p, re.I)) for c, p in (
    (MANUAL, r"^rejected manually"),
    ("roof", r"roof closed|parked|safety monitor read unsafe|hot pixels, not stars"),
    ("tracking_jump", r"tracking jump"),
    ("hfr_rel", r"hfr outlier"),
    ("hfr", r"\bhfr\b.*out of focus"),
    ("ecc_bin", r"eccentricity at 0\.48"),
    ("ecc", r"elongated stars|eccentricity"),
    ("stars", r"\bonly \d+ stars|stars detected|stars > \d+|defocus/false"),
    ("fwhm", r"\bfwhm\b"),
    # "...limit; camera was set to 0C" splits into two parts: both are temp
    ("temp", r"^sensor -?\d+(\.\d+)?c\b|^camera was set to"),
    ("guide_rms", r"tracking rms"),
    ("guide_lock", r"non-star"),
    ("pointing", r"off target"),
)]


def reason_label(code: str) -> str:
    from photonscript.shared.qa_rules import CHECKS
    if code == MANUAL:
        return "Rejected by a person"
    if code == OTHER:
        return "Other"
    return CHECKS.get(code, (code,))[0]


def legacy_reason_codes(reason: str) -> list[str]:
    """Reason text of a pre-scorecard record -> check ids (in order, unique);
    an unrecognized part maps to "other"."""
    out: list[str] = []
    for part in (p.strip() for p in str(reason or "").split(";")):
        if not part:
            continue
        code = next((c for c, rx in LEGACY_REASON_PATTERNS if rx.search(part)),
                    OTHER)
        if code not in out:
            out.append(code)
    return out


def _scorecard_fails(rec: dict) -> list[str]:
    d = rec.get("drivers")
    if isinstance(d, list) and d:
        return [str(x) for x in d]
    rows = (rec.get("scorecard") or {}).get("rows") or []
    return [str(r[0]) for r in rows
            if isinstance(r, (list, tuple)) and len(r) >= 4 and r[3] == "fail"]


def verdict_of(rec: dict) -> tuple[str, str]:
    """(verdict, verdict_by) of one stored sub record."""
    from photonscript.scheduler.runs import _human_verdict
    if not rec.get("passed_qa"):
        return REJECTED, (MANUAL if rec.get("manual_qa")
                          or rec.get("review_source") == "manual" else "auto")
    if rec.get("reviewed"):
        return APPROVED, ("auto" if rec.get("review_source") == "auto"
                          else MANUAL)
    return PENDING, (MANUAL if _human_verdict(rec) else "auto")


def reason_codes(rec: dict, verdict: str) -> list[str]:
    """Why a rejected sub was rejected (scorecard check ids first)."""
    from photonscript.shared.qa_rules import CHECKS
    if verdict != REJECTED:
        return []
    if rec.get("manual_qa") or str(rec.get("reason") or "").lower().startswith(
            "rejected manually"):
        why = str(rec.get("manual_reason") or "")
        return [MANUAL] + ([why] if why in CHECKS else [])
    codes = list(dict.fromkeys(_scorecard_fails(rec)))
    if not codes:
        codes = legacy_reason_codes(rec.get("reason") or "")
    return codes or [OTHER]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _project_sig(projects: Iterable) -> tuple:
    out = []
    for p in projects:
        t = p.target
        out.append((p.id, t.name, t.catalog_id, p.active,
                    tuple((e.filter_type.value, e.exposure_seconds,
                           e.hdr_short_seconds, e.hdr_short_count)
                          for e in p.exposure_plans)))
    return tuple(sorted(out, key=lambda x: str(x[0])))


def _logs_sig(config) -> tuple:
    from photonscript.scheduler.runs import runs_dir
    out = []
    for p in sorted(runs_dir(config).glob("*_subs.jsonl")):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_mtime_ns, st.st_size))
    try:
        rev = tuple(sorted(config.reverse_filter_map().items()))
    except Exception:  # noqa: BLE001
        rev = ()
    return tuple(out) + (rev,)


_cache: dict = {}
_cache_lock = threading.Lock()


def _build_row(date: str, rec: dict, known, by_key: dict) -> dict:
    from photonscript.shared.qa_rules import record_ecc
    rig = rec.get("rig") or "rc16"
    file = str(rec.get("file") or "")
    # `target` decides (PS-78 rename / PS-51 piggyback attribution rewrite
    # it); `target_raw` is only what the sub was first called
    raw = rec.get("target_raw") or rec.get("target")
    canon = canonical_target(rec.get("target"), known) or UNATTRIBUTED
    tkey = target_key(canon) if canon != UNATTRIBUTED else UNATTRIBUTED
    proj = by_key.get(tkey)
    flt = rec.get("filter") or "?"
    exp_s = _num(rec.get("exp_s"))
    hdr_short = False
    if proj is not None:
        for e in proj.exposure_plans:
            if e.filter_type.value == flt and e.is_short_exposure(exp_s):
                hdr_short = True
    verdict, by = verdict_of(rec)
    codes = reason_codes(rec, verdict)
    reason = str(rec.get("reason") or "")
    return {
        "key": f"{date}|{rig}|{file}",
        "date": date, "rig": rig, "file": file,
        "abs_path": rec.get("abs_path") or "",
        "time": rec.get("time") or "",
        "target_raw": raw if raw not in (None, "") else UNATTRIBUTED,
        "target": canon, "target_key": tkey,
        "project_id": proj.id if proj is not None else None,
        "filter": flt, "exp_s": exp_s, "hdr_short": hdr_short,
        "verdict": verdict, "verdict_by": by,
        "reasons": [p.strip() for p in reason.split(";") if p.strip()]
        if verdict == REJECTED else [],
        "reason_codes": codes,
        "qa_flag": rec.get("qa_flag") or "",
        "metrics": {
            "hfr": _num(rec.get("hfr")),
            "fwhm_arcsec": _num(rec.get("fwhm_arcsec")),
            "ecc": record_ecc(rec), "ecc_bin": record_ecc(rec, "ecc_bin"),
            "stars": _num(rec.get("stars")),
            "background": _num(rec.get("background")),
            "ccd_temp": _num(rec.get("ccd_temp")),
            "corner_ecc": _num(rec.get("corner_ecc")),
            "doubled_frac": _num(rec.get("doubled_frac")),
        },
        "thumb": f"/api/runs/{date}/thumb?file={quote(file, safe='')}"
                 f"&w={THUMB_W}",
        "pointing": None,  # PS-67
    }


def all_rows(config, projects: Iterable = ()) -> list[dict]:
    """Every sub of every night, oldest night first (memoized)."""
    from photonscript.scheduler.runs import _load_subs
    projects = list(projects or ())
    sig = (_logs_sig(config), _project_sig(projects))
    ck = str(getattr(config, "data_dir", ""))
    with _cache_lock:
        hit = _cache.get(ck)
    if hit is not None and hit[0] == sig:
        return hit[1]
    known = known_target_index(projects) if projects else None
    by_key = {target_key(p.target.name): p for p in projects}
    out: list[dict] = []
    for name, _m, _s in sig[0][:-1]:
        date = name[:10]
        for rec in _load_subs(config, date):
            out.append(_build_row(date, rec, known, by_key))
    with _cache_lock:
        _cache[ck] = (sig, out)
    return out


def _match_target(name: str, projects) -> str:
    """Canonical key for a requested target name ('?' = unattributed)."""
    n = str(name or "").strip()
    if not n or n == UNATTRIBUTED:
        return UNATTRIBUTED
    known = known_target_index(list(projects)) if projects else None
    canon = canonical_target(n, known)
    return target_key(canon) if canon else UNATTRIBUTED


def rows(config, projects: Iterable = (), *, target: str | None = None,
         rig: str | None = None, filter: str | None = None,  # noqa: A002
         verdict: str | None = None, night: str | None = None,
         reason: str | None = None, sort: str = "time") -> list[dict]:
    """Filtered rows. sort: time (newest first) or hfr (sharpest first)."""
    projects = list(projects or ())
    out = all_rows(config, projects)
    if target is not None:
        k = _match_target(target, projects)
        out = [r for r in out if r["target_key"] == k]
    if rig:
        out = [r for r in out if r["rig"] == rig]
    if filter:
        out = [r for r in out if r["filter"] == filter]
    if verdict:
        out = [r for r in out if r["verdict"] == verdict]
    if night:
        out = [r for r in out if r["date"] == night]
    if reason:
        out = [r for r in out if reason in r["reason_codes"]]
    if sort == "hfr":
        out = sorted(out, key=lambda r: (r["metrics"]["hfr"] is None,
                                         r["metrics"]["hfr"] or 0.0))
    else:
        out = sorted(out, key=lambda r: (r["date"], r["time"]), reverse=True)
    return out


def best_subs(rows_: Iterable[dict], config) -> dict[str, str]:
    """{"rig|filter": row key}: per rig + filter, the lowest-HFR passed sub
    (approved or pending) with eccentricity under that rig's
    quality_eccentricity_max."""
    from photonscript.shared.qa_rules import thresholds
    lim: dict[str, float] = {}
    best: dict[str, tuple] = {}
    for r in rows_:
        if r["verdict"] == REJECTED:
            continue
        hfr, ecc = r["metrics"]["hfr"], r["metrics"]["ecc"]
        if hfr is None or hfr <= 0:
            continue
        if r["rig"] not in lim:
            try:
                lim[r["rig"]] = float(thresholds(config, r["rig"])["ecc_max"])
            except Exception:  # noqa: BLE001
                lim[r["rig"]] = 0.70
        if ecc is None or ecc >= lim[r["rig"]]:
            continue
        k = f"{r['rig']}|{r['filter']}"
        if k not in best or hfr < best[k][0]:
            best[k] = (hfr, r["key"])
    return {k: v[1] for k, v in best.items()}


# --- target facts --------------------------------------------------------------

def _catalog_entry(name: str, catalog_id: str = "") -> dict | None:
    from photonscript.shared.astronomy import SEASONAL_TARGETS
    keys = {target_key(name), target_key(catalog_id)} - {""}
    for t in SEASONAL_TARGETS:
        if keys & {target_key(t.get("name")), target_key(t.get("catalog_id"))}:
            return t
    return None


def _sexagesimal(ra_h, dec_d) -> tuple[str, str]:
    if ra_h is None or dec_d is None:
        return "", ""
    ra = float(ra_h) % 24.0
    h = int(ra)
    m = int((ra - h) * 60)
    s = (ra - h - m / 60) * 3600
    sign = "-" if dec_d < 0 else "+"
    d_ = abs(float(dec_d))
    dd = int(d_)
    dm = int((d_ - dd) * 60)
    ds = (d_ - dd - dm / 60) * 3600
    return f"{h:02d}h {m:02d}m {s:04.1f}s", f"{sign}{dd:02d}d {dm:02d}' {ds:02.0f}\""


def target_facts(name: str, project=None, *, constellation: bool = False) -> dict:
    """What the target is: project target first, else the seasonal catalog.
    constellation=True fills a blank constellation from astropy."""
    t = project.target if project is not None else None
    cat = _catalog_entry(name, t.catalog_id if t else "")
    f = {"name": name,
         "catalog_id": (t.catalog_id if t else "") or (cat or {}).get("catalog_id", ""),
         "object_type": (t.object_type if t else "") or (cat or {}).get("type", ""),
         "magnitude": t.magnitude if t is not None and t.magnitude is not None
         else (cat or {}).get("mag"),
         "size_arcmin": t.angular_size_arcmin if t is not None
         and t.angular_size_arcmin else (cat or {}).get("size"),
         "constellation": (t.constellation if t else "") or "",
         "ra_hours": t.ra_hours if t else (cat or {}).get("ra"),
         "dec_degrees": t.dec_degrees if t else (cat or {}).get("dec")}
    if constellation and not f["constellation"] and f["ra_hours"] is not None:
        try:
            from astropy import units as u
            from astropy.coordinates import SkyCoord, get_constellation
            f["constellation"] = str(get_constellation(SkyCoord(
                ra=float(f["ra_hours"]) * u.hourangle,
                dec=float(f["dec_degrees"]) * u.deg)))
        except Exception:  # noqa: BLE001
            pass
    f["ra_text"], f["dec_text"] = _sexagesimal(f["ra_hours"], f["dec_degrees"])
    return f


def _counts(rs: list[dict]) -> dict:
    c = Counter(r["verdict"] for r in rs)
    h = Counter()
    for r in rs:
        h[r["verdict"]] += (r["exp_s"] or 0.0)
    return {**{v: c.get(v, 0) for v in VERDICTS}, "total": len(rs),
            "hours": {v: round(h.get(v, 0.0) / 3600, 2) for v in VERDICTS}}


def _top_reasons(rs: list[dict], n: int = 5) -> list[dict]:
    cnt: Counter = Counter()
    example: dict[str, str] = {}
    fallback: dict[str, str] = {}
    for r in rs:
        if r["verdict"] != REJECTED:
            continue
        for code in r["reason_codes"]:
            cnt[code] += 1
            if example.get(code):
                continue
            # the reason text that says this (a scorecard's driver ids and
            # its joined reason text are not index-aligned: a fail with no
            # text adds an id but no part)
            hit = next((p for p in r["reasons"]
                        if code in legacy_reason_codes(p)), "")
            if hit:
                example[code] = hit
            elif len(r["reasons"]) == 1 and len(r["reason_codes"]) == 1:
                fallback.setdefault(code, r["reasons"][0])
    for code, text in fallback.items():
        if not example.get(code):
            example[code] = text
    return [{"code": c, "label": reason_label(c), "count": k,
             "example": example.get(c, "")} for c, k in cnt.most_common(n)]


def _goal(project) -> dict | None:
    """Goal progress for the Targets pages. PS-118: hours and % come from
    accepted SECONDS (ExposurePlan.long_seconds_done), so subs longer or
    shorter than the plan count for what they hold. `acquired` stays the
    whole plan subs' worth (floor), the number the planner subtracts; % is
    capped at 100 per plan so an over-done plan does not carry another."""
    if project is None:
        return None
    plans = []
    h_goal = h_done = pct_done = 0.0
    for e in project.exposure_plans:
        has_short = bool(e.hdr_short_seconds and e.hdr_short_count)
        long_goal = e.count * e.exposure_seconds
        long_done = e.long_seconds_done()
        short_goal = e.hdr_short_count * e.hdr_short_seconds if has_short else 0
        short_done = (e.hdr_short_acquired * e.hdr_short_seconds
                      if has_short else 0)
        plans.append({
            "filter": e.filter_type.value, "count": e.count,
            "acquired": e.acquired, "exposure_s": e.exposure_seconds,
            "hours_goal": round(long_goal / 3600, 2),
            "hours_done": round(long_done / 3600, 2),
            "pct": round(min(long_done, long_goal) / long_goal * 100)
            if long_goal else 0,
            "hdr_short": {"exposure_s": e.hdr_short_seconds,
                          "count": e.hdr_short_count,
                          "acquired": e.hdr_short_acquired}
            if has_short else None})
        h_goal += long_goal + short_goal
        h_done += long_done + short_done
        pct_done += min(long_done, long_goal) + min(short_done, short_goal)
    return {"pct": round(pct_done / h_goal * 100) if h_goal else 0,
            "hours_goal": round(h_goal / 3600, 1),
            "hours_done": round(h_done / 3600, 1), "plans": plans}


def _project_meta(p) -> dict:
    return {"project_id": p.id if p is not None else None,
            "active": bool(p.active) if p is not None else None,
            "priority": p.priority if p is not None else None}


def targets(config, projects: Iterable = ()) -> list[dict]:
    """One card per target: active projects (priority order), then other
    targets with subs (latest night first), then the unattributed bucket."""
    projects = list(projects or ())
    all_ = all_rows(config, projects)
    groups: dict[str, list[dict]] = {}
    for r in all_:
        groups.setdefault(r["target_key"], []).append(r)
    by_key = {target_key(p.target.name): p for p in projects}
    cards = []
    for k in set(groups) | set(by_key):
        rs = groups.get(k, [])
        p = by_key.get(k)
        if k == UNATTRIBUTED:
            name = UNATTRIBUTED
        elif p is not None:
            name = p.target.name
        else:
            name = Counter(r["target"] for r in rs).most_common(1)[0][0]
        nights = sorted({r["date"] for r in rs})
        top = _top_reasons(rs, 1)
        cards.append({
            "name": name, "key": k, "unattributed": k == UNATTRIBUTED,
            **_project_meta(p),
            "facts": target_facts(name, p) if k != UNATTRIBUTED else None,
            "goal": _goal(p), "counts": _counts(rs),
            "nights": len(nights),
            "first_night": nights[0] if nights else None,
            "last_night": nights[-1] if nights else None,
            "rigs": sorted({r["rig"] for r in rs}),
            "top_reason": top[0] if top else None,
        })

    def order(c):
        if c["unattributed"]:
            return (3, 0, "")
        if c["active"]:
            return (0, -(c["priority"] or 0), c["name"].lower())
        if c["counts"]["total"]:
            return (1, -int((c["last_night"] or "0000-00-00").replace("-", "")),
                    c["name"].lower())
        return (2, 0, c["name"].lower())
    return sorted(cards, key=order)


def target_detail(config, name: str, projects: Iterable = ()) -> dict:
    """Everything the per-target page shows except the readiness block and
    the reference image (separate, slower requests)."""
    from photonscript.scheduler.runs import library_root, library_target_dirs
    from photonscript.shared.rigs import rig_label
    projects = list(projects or ())
    k = _match_target(name, projects)
    rs = [r for r in all_rows(config, projects) if r["target_key"] == k]
    p = next((x for x in projects if target_key(x.target.name) == k), None)
    if k == UNATTRIBUTED:
        canon = UNATTRIBUTED
    elif p is not None:
        canon = p.target.name
    elif rs:
        canon = Counter(r["target"] for r in rs).most_common(1)[0][0]
    else:
        known = known_target_index(projects) if projects else None
        canon = canonical_target(name, known) or str(name)
    split: dict[tuple, list] = {}
    nights: dict[str, list] = {}
    for r in rs:
        split.setdefault((r["filter"], r["rig"]), []).append(r)
        nights.setdefault(r["date"], []).append(r)
    lib = library_root(config)
    tdirs = library_target_dirs(lib, canon, projects or None) \
        if k != UNATTRIBUTED else []
    desk = Path(getattr(config, "desktop_library_dir", "") or "")
    return {
        "target": canon, "key": k, "unattributed": k == UNATTRIBUTED,
        "found": bool(rs or p),
        **_project_meta(p),
        "facts": target_facts(canon, p, constellation=True)
        if k != UNATTRIBUTED else None,
        "goal": _goal(p),
        "totals": _counts(rs),
        "by_filter_rig": [
            {"filter": f, "rig": rg, "rig_label": rig_label(config, rg),
             **_counts(v)}
            for (f, rg), v in sorted(split.items())],
        "nights": [{"date": d, **_counts(v)}
                   for d, v in sorted(nights.items(), reverse=True)],
        "first_night": min(nights) if nights else None,
        "last_night": max(nights) if nights else None,
        "reasons": _top_reasons(rs, 5),
        "best": best_subs(rs, config),
        "raw_names": sorted({r["target_raw"] for r in rs
                             if r["target_raw"] != canon}),
        "rigs": [{"rig": rg, "label": rig_label(config, rg)}
                 for rg in sorted({r["rig"] for r in rs})],
        "filters": sorted({r["filter"] for r in rs}),
        "library_dir": str(tdirs[0]) if tdirs else "",
        "library_dirs": [str(d) for d in tdirs if d.exists()],
        "desktop_path": str(desk / canon) if str(desk) not in ("", ".") and
        k != UNATTRIBUTED else "",
    }


def library_files(config, name: str, projects: Iterable = ()) -> set[str]:
    """Basenames of every light of `name` in the Library (all its folders)."""
    from photonscript.scheduler.runs import library_root, library_target_dirs
    out: set[str] = set()
    for d in library_target_dirs(library_root(config), name,
                                 list(projects or ()) or None):
        if d.exists():
            out |= {f.name for f in d.rglob("*.fits")}
    return out
