"""PS-137: name each Piggy-600 sub after the goal the Piggy actually framed.

A Piggy-600 sub takes its name from the RC16 (its OBJECT, the live target or
the PS-51 time correlation). When the RC16 shoots a target of another name
(a mosaic panel, a tracking-test field, an RC16 core plan) the Piggy subs land
under that name in the runs table and the Library, and the Piggy goal never
sees them. 2026-09-21: the M31 Piggy files carry OBJECT "Crescent Nebula"
(29) or none (59).

Rule (attribute_records):

1. Position of the Piggy frame centre per sub: its own plate solve (the PS-96
   / PS-67 solve store, else the pointing sidecar), else the nearest solved
   Piggy sub within NEAR_SOLVE_MIN when nothing moved the mount in between
   (slew_gate.NightWindows: the mount log, else the RC16 frames), else the
   mount position from the pointing sidecar (mount log / RC16-correlated:
   the RC16 boresight, rotation unknown).
2. Candidates are the goals (projects.json, read only). A goal fits when its
   target lies inside the Piggy frame around that centre: the full
   piggyback FOV rectangle (refimage.rig_fov, about 134' x 90') turned by the
   solve rotation; with no rotation (a mount position) only the circle of
   half the short side counts, which is inside the frame at any rotation.
3. Preference among goals that fit: Piggy-driven goals (driving_rig
   piggyback), then goals with a Piggy-600 plan (passenger), then any other
   goal; nearest the frame centre within a tier. No goal in the frame: the
   sub keeps its name. PS-111 mosaic panels are never candidates (RC16-only
   goals).
4. PS-111: a Piggy sub named after a mosaic panel credits the mosaic's
   companion goal (ProjectStore.credit_sub), so it keeps the panel name
   unless its frame holds a different Piggy-driven goal (then that goal
   wins). Its evidence names the companion (tier "mosaic companion").
5. PS-135: the sub's current name is matched to the goals through the alias
   index (target_names.known_target_index: names, catalog ids and catalog
   aliases), so "NGC 224" already counts as the "M 31" goal.

Records: `target` is the attributed name (what the Library, goal progress
and the runs page use), `target_raw` keeps what the sub was called before
(the raw OBJECT / RC16 name, "?" for none), `target_src` = "piggy-frame",
and `target_attr` holds the evidence {name, src, off_arcmin, tier, from,
applied}. A sub assigned by hand (`target_src` "manual") is never touched.

Modes (`piggyback_frame_attribution`): off | report (default: only
`target_attr` is written, nothing is renamed or refiled) | on (rename; the
Library build that follows moves the links). The CLI `piggy-attribution`
re-attributes past nights (dry run unless --apply) and moves the Library
links itself. Library moves only ever happen under library_root on the
scope; the desktop ninashare mirror is receive-only and never written.
Nothing here writes a FITS file.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

PIGGY = "piggyback"
NEAR_SOLVE_MIN = 10.0     # borrow a neighbour's solve this close in time
TIERS = ("piggy-driven", "passenger", "goal")
MODES = ("off", "report", "on")


# ------------------------------------------------------------------ geometry

def _sep_arcmin(ra1, dec1, ra2, dec2) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 60.0


def frame_offsets(c_ra, c_dec, ra, dec) -> tuple[float, float] | None:
    """Gnomonic (xi east, eta north) offsets in arcmin of (ra, dec) from the
    frame centre; None behind the tangent plane."""
    a0, d0, a, d = map(math.radians, (c_ra, c_dec, ra, dec))
    cosc = (math.sin(d0) * math.sin(d)
            + math.cos(d0) * math.cos(d) * math.cos(a - a0))
    if cosc <= 0:
        return None
    xi = math.cos(d) * math.sin(a - a0) / cosc
    eta = (math.cos(d0) * math.sin(d)
           - math.sin(d0) * math.cos(d) * math.cos(a - a0)) / cosc
    return math.degrees(xi) * 60.0, math.degrees(eta) * 60.0


def frame_contains(c_ra, c_dec, ra, dec, w_arcmin, h_arcmin,
                   pa_deg=None) -> bool:
    """True when (ra, dec) lies inside a w x h frame centred on (c_ra, c_dec).
    pa_deg = the solve rotation (image up, degrees east of north); the
    rectangle is symmetric so the image parity does not matter. Without a
    rotation only the circle of half the short side counts (inside the
    frame whatever the rotation)."""
    off = frame_offsets(c_ra, c_dec, ra, dec)
    if off is None:
        return False
    xi, eta = off
    if pa_deg is None:
        return math.hypot(xi, eta) <= min(w_arcmin, h_arcmin) / 2.0
    p = math.radians(float(pa_deg))
    x = xi * math.cos(p) - eta * math.sin(p)
    y = xi * math.sin(p) + eta * math.cos(p)
    return abs(x) <= w_arcmin / 2.0 and abs(y) <= h_arcmin / 2.0


def piggy_fov(config) -> tuple[float, float]:
    """(width, height) arcmin of the Piggy-600 frame (about 134 x 90)."""
    try:
        from photonscript.scheduler.refimage import rig_fov
        f = rig_fov(config, PIGGY)
        return float(f["w_arcmin"]), float(f["h_arcmin"])
    except Exception as e:  # noqa: BLE001
        logger.debug("piggy fov fallback: %s", e)
        return 133.8, 89.6


# ------------------------------------------------------------------ goals

def _tier(proj: dict) -> int:
    if str(proj.get("driving_rig") or "rc16") == PIGGY:
        return 0
    plans = proj.get("exposure_plans") or []
    if any(str((p or {}).get("rig") or "rc16") == PIGGY for p in plans):
        return 1
    return 2


def _raw_goals(config, projects=None) -> list[dict]:
    """The goals as dicts. `projects` = ImagingProject objects or dicts;
    default: projects.json read only (a second process must not save the
    store)."""
    raw = projects
    if raw is None:
        p = Path(config.data_dir) / "projects.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            raw = list(data.values()) if isinstance(data, dict) else list(data)
        except (OSError, ValueError) as e:
            logger.info("piggy attribution: no goals (%s)", e)
            return []
    return [p.model_dump(mode="json") if hasattr(p, "model_dump") else p
            for p in raw if p]


def _is_panel(proj: dict) -> bool:
    m = proj.get("mosaic")
    return isinstance(m, dict) and bool(m.get("id"))


def goal_candidates(config, projects=None) -> list[dict]:
    """[{name, ra, dec (deg), tier (0 piggy-driven, 1 passenger, 2 goal)}]
    from the goals (projects as in _raw_goals). PS-111 mosaic panels are
    left out: they are RC16 goals and their Piggy subs credit the companion."""
    return _candidates(_raw_goals(config, projects))


def _candidates(raw: list[dict]) -> list[dict]:
    out, seen = [], set()
    for proj in raw:
        if _is_panel(proj):
            continue
        t = (proj or {}).get("target") or {}
        name = str(t.get("name") or "").strip()
        try:
            ra = float(t["ra_hours"]) * 15.0
            dec = float(t["dec_degrees"])
        except (KeyError, TypeError, ValueError):
            continue
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "ra": ra, "dec": dec, "tier": _tier(proj)})
    return out


def _goal_index(raw: list[dict]):
    """PS-135 alias index {target_key(alias): goal name} over every goal's
    name and catalog id (catalog aliases added by known_target_index)."""
    from photonscript.shared.target_names import known_target_index
    pairs = {}
    for proj in raw:
        t = proj.get("target") or {}
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        pairs.setdefault(name, name)
        cid = str(t.get("catalog_id") or "").strip()
        if cid:
            pairs.setdefault(cid, name)
    return known_target_index(pairs)


def _panel_companions(raw: list[dict]) -> dict[str, str]:
    """PS-111: {panel goal name (casefolded): companion goal name} for the
    mosaic panels whose companion is a known goal. The companion is stored
    as a project id (older specs: a name or catalog id)."""
    from photonscript.shared.target_names import target_key
    by_id, by_key = {}, {}
    for proj in raw:
        t = proj.get("target") or {}
        name = str(t.get("name") or "").strip()
        if not name or _is_panel(proj):
            continue
        if proj.get("id"):
            by_id[str(proj["id"])] = name
        for k in (target_key(name), target_key(t.get("catalog_id"))):
            if k:
                by_key.setdefault(k, name)
    out = {}
    for proj in raw:
        if not _is_panel(proj):
            continue
        ref = str(proj["mosaic"].get("companion") or "")
        comp = by_id.get(ref) or by_key.get(target_key(ref))
        name = str((proj.get("target") or {}).get("name") or "").strip()
        if comp and name:
            out[name.casefold()] = comp
    return out


def choose_goal(c_ra, c_dec, pa, cands, w, h) -> dict | None:
    """The goal whose target the frame contains, Piggy-driven first, then
    passenger, then any goal; nearest the centre within a tier. Adds
    off_arcmin (target to frame centre)."""
    fit = []
    for c in cands:
        if frame_contains(c_ra, c_dec, c["ra"], c["dec"], w, h, pa):
            fit.append({**c, "off_arcmin": round(
                _sep_arcmin(c_ra, c_dec, c["ra"], c["dec"]), 1)})
    if not fit:
        return None
    return min(fit, key=lambda c: (c["tier"], c["off_arcmin"]))


# ------------------------------------------------------------------ positions

def _is_piggy(rec: dict) -> bool:
    return (rec.get("rig") or "rc16") not in ("rc16", "")


def _solve_pos(s: dict | None) -> dict | None:
    if not s or s.get("solved") is False or s.get("ra") is None:
        return None
    return {"ra": float(s["ra"]), "dec": float(s["dec"]),
            "pa": s.get("pa"), "src": "solve"}


def positions(config, date: str, subs: list[dict], solve: bool = False,
              runner=None, max_solves: int = 30) -> dict[str, dict]:
    """file -> {ra, dec, pa, src} for the night's Piggy subs (see the module
    doc, step 1). solve=True plate-solves (through the solve store, so the
    pointing pass reuses them) the subs nothing else places, up to
    max_solves, earliest first; each new solve also places its neighbours."""
    from photonscript.scheduler import solve_store
    from photonscript.scheduler.phd2_analysis import sub_start_utc
    from photonscript.shared import pointing

    piggy = [s for s in subs if _is_piggy(s) and s.get("file")]
    if not piggy:
        return {}
    stored = solve_store.lookup(config, date, PIGGY)
    side = pointing.load(config, date)
    frames = []
    for s in piggy:
        st = sub_start_utc(s, config)
        if st is not None:
            frames.append((st, st + timedelta(seconds=float(s.get("exp_s") or 0)), s))
    frames.sort(key=lambda f: f[0])

    own: dict[str, dict] = {}
    mount: dict[str, dict] = {}
    for _st, _en, s in frames:
        f = s["file"]
        pos = _solve_pos(stored.get(f))
        rec = side.get((s.get("rig") or PIGGY, f)) or {}
        if pos is None and rec.get("solved_ra") is not None:
            pos = {"ra": float(rec["solved_ra"]), "dec": float(rec["solved_dec"]),
                   "pa": rec.get("rotation"), "src": "solve"}
        if pos:
            own[f] = pos
        elif rec.get("mount_ra") is not None and rec.get("mount_dec") is not None:
            mount[f] = {"ra": float(rec["mount_ra"]), "dec": float(rec["mount_dec"]),
                        "pa": None, "src": rec.get("mount_src") or rec.get("src")
                        or "mount"}

    nw = None
    try:
        from photonscript.scheduler.slew_gate import NightWindows
        from photonscript.shared import mount_log
        nw = NightWindows(config, lines=mount_log.load(config, date),
                          records=subs)
    except Exception as e:  # noqa: BLE001
        logger.debug("piggy attribution: no move windows (%s)", e)

    def near(i):
        """The nearest own-solved frame within NEAR_SOLVE_MIN with no mount
        move in between (unknown = no borrowing)."""
        if nw is None:
            return None
        st, en, _s = frames[i]
        best = None
        for step in (-1, 1):        # frames are time-ordered: walk outwards
            j = i + step
            while 0 <= j < len(frames):
                st2, en2, s2 = frames[j]
                gap = abs((st2 - st).total_seconds()) / 60.0
                if gap > NEAR_SOLVE_MIN:
                    break
                p = own.get(s2["file"])
                if p is not None:
                    a = nw.assess(min(st, st2), max(en, en2))
                    if a.get("overlap_s") == 0 and (best is None
                                                    or gap < best[0]):
                        best = (gap, p)
                    break           # the nearest solve on this side decides
                j += step
        return best[1] if best else None

    def place_all():
        out = {}
        for i, (_st, _en, s) in enumerate(frames):
            f = s["file"]
            if f in own:
                out[f] = own[f]
                continue
            p = near(i)
            if p:
                out[f] = {**p, "src": "solve (neighbour)"}
            elif f in mount:
                out[f] = mount[f]
        return out

    out = place_all()
    if solve:
        n = 0
        for i, (st, _en, s) in enumerate(frames):
            f = s["file"]
            if (out.get(f) or {}).get("src", "").startswith("solve"):
                continue
            if n >= max_solves:
                break
            if f in stored or not s.get("abs_path"):
                continue      # already tried (failed) or no file
            m = mount.get(f)
            hint = (m["ra"], m["dec"]) if m else None
            res = solve_store.solve(
                config, s["abs_path"], rig=PIGGY, night=date, hint=hint,
                rel_file=f, start_utc=st.isoformat() + "Z", runner=runner,
                radius_deg=5.0 if hint else 30.0)
            n += 1
            pos = _solve_pos(res)
            if pos:
                own[f] = pos
                out = place_all()
    return out


# ------------------------------------------------------------------ attribute

def attribute_records(config, date: str, subs: list[dict], apply: bool,
                      solve: bool = False, runner=None, projects=None,
                      max_solves: int = 30) -> dict:
    """Run the rule over a night's records IN PLACE. apply=False writes only
    `target_attr` (applied False); apply=True also renames. Returns
    {piggy, placed, changed: [{file, filter, from, to, src, off_arcmin,
    tier}], kept, no_goal, no_position, attr_written}."""
    from photonscript.shared.target_names import UNATTRIBUTED, canonical_target

    out = {"date": date, "piggy": 0, "placed": 0, "changed": [], "kept": 0,
           "no_goal": 0, "no_position": 0, "manual": 0, "attr_written": 0,
           "applied": bool(apply)}
    piggy = [s for s in subs if _is_piggy(s)]
    out["piggy"] = len(piggy)
    if not piggy:
        return out
    raw = _raw_goals(config, projects)
    cands = _candidates(raw)
    known = _goal_index(raw)              # PS-135 aliases
    panels = _panel_companions(raw)       # PS-111 panel -> companion goal
    driven = {c["name"].casefold() for c in cands if c["tier"] == 0}
    if not cands:
        out["no_goal"] = len(piggy)
        return out
    w, h = piggy_fov(config)
    pos = positions(config, date, subs, solve=solve, runner=runner,
                    max_solves=max_solves)
    for s in piggy:
        if s.get("target_src") == "manual":
            out["manual"] += 1
            continue
        p = pos.get(s.get("file"))
        if p is None:
            out["no_position"] += 1
            continue
        out["placed"] += 1
        g = choose_goal(p["ra"], p["dec"], p.get("pa"), cands, w, h)
        if g is None:
            out["no_goal"] += 1
            continue
        cur = s.get("target")
        cur_canon = canonical_target(cur, known)
        prev_attr = s.get("target_attr") or {}
        comp = panels.get((cur_canon or "").casefold())
        if comp and not (g["name"].casefold() in driven
                         and g["name"].casefold() != comp.casefold()):
            # PS-111: a panel's Piggy sub credits the companion goal; only a
            # different Piggy-driven goal in the frame takes it (below)
            attr = {"name": comp, "src": p["src"], "off_arcmin": None,
                    "tier": "mosaic companion", "via": cur_canon,
                    "from": prev_attr.get("from") or cur, "applied": True}
            out["kept"] += 1
            if prev_attr != attr:
                s["target_attr"] = attr
                out["attr_written"] += 1
            continue
        same = cur_canon is not None and cur_canon.casefold() == g["name"].casefold()
        attr = {"name": g["name"], "src": p["src"],
                "off_arcmin": g["off_arcmin"], "tier": TIERS[g["tier"]],
                "from": (prev_attr.get("from") if same and prev_attr.get("applied")
                         else (cur if cur not in (None, "") else UNATTRIBUTED)),
                "applied": bool(apply and not same) or bool(
                    same and prev_attr.get("applied"))}
        if same:
            out["kept"] += 1
        else:
            out["changed"].append({
                "file": s.get("file"), "filter": s.get("filter"),
                "from": cur if cur not in (None, "") else UNATTRIBUTED,
                "to": g["name"], "src": p["src"], "off_arcmin": g["off_arcmin"],
                "tier": TIERS[g["tier"]], "abs_path": s.get("abs_path"),
                "passed_qa": bool(s.get("passed_qa"))})
            if apply:
                if not s.get("target_raw"):
                    s["target_raw"] = (cur if cur not in (None, "")
                                       else UNATTRIBUTED)
                s["target"] = g["name"]
                s["target_src"] = "piggy-frame"
        if prev_attr != attr:
            s["target_attr"] = attr
            out["attr_written"] += 1
    return out


def mode(config) -> str:
    m = str(getattr(config, "piggyback_frame_attribution", "report")
            or "report").strip().lower()
    return m if m in MODES else "report"


def attribute_piggy_night(config, date: str, solve: bool = False,
                          apply: bool | None = None, projects=None,
                          runner=None) -> dict:
    """The night pass used by runs.attribute_night and the dawn backfill:
    apply follows the mode unless given (off = nothing at all). Rewrites
    the subs file only when a record changed. Returns attribute_records'
    result plus `renamed` (subs whose target changed)."""
    from photonscript.scheduler.runs import edit_subs
    m = mode(config)
    if m == "off" and apply is None:
        return {"date": date, "mode": m, "renamed": 0}
    if apply is None:
        apply = m == "on"
    # PS-140: solves (solve=True) run without the night lock; only the
    # fields this pass changes are merged in under it on exit. Report mode
    # writes the evidence (target_attr) too, as before.
    with edit_subs(config, date, hold_lock=False) as subs:
        res = attribute_records(config, date, subs, apply=apply,
                                solve=solve, runner=runner,
                                projects=projects)
    res["mode"] = m
    res["renamed"] = len(res["changed"]) if apply else 0
    if res["changed"]:
        logger.info("Piggy attribution %s (%s): %d of %d Piggy subs %s %s",
                    date, "applied" if apply else "report",
                    len(res["changed"]), res["piggy"],
                    "renamed" if apply else "would move",
                    sorted({(c["from"], c["to"]) for c in res["changed"]}))
    return res


# ------------------------------------------------------------------ library

def library_moves(config, changed: list[dict], apply: bool) -> dict:
    """Move the Library links of re-attributed subs from the old target
    folder to the new one (Library root and Library/_rejected), under
    library_root on the scope only. A destination that exists is a
    collision: reported, left in place, nothing overwritten or deleted.
    Subs not yet linked are left to the next Library build."""
    from photonscript.scheduler.runs import _safe_name, library_root
    lib = library_root(config)
    res = {"library": str(lib), "moves": [], "collisions": [], "moved": 0}
    for c in changed:
        name = Path(str(c.get("abs_path") or c.get("file") or "")
                    .replace("\\", "/")).name
        if not name:
            continue
        old = _safe_name(c["from"] if c["from"] not in ("?", "") else "_")
        new = _safe_name(c["to"])
        fdir = _safe_name(c.get("filter") or "?")
        for root in (lib, lib / "_rejected"):
            src = root / old / fdir / name
            if not src.exists():
                continue
            dest = root / new / fdir / name
            rel = (str(src.relative_to(lib)), str(dest.relative_to(lib)))
            if dest.exists():
                res["collisions"].append({"from": rel[0], "to": rel[1]})
                continue
            if apply:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dest)
                except OSError as e:
                    res["collisions"].append({"from": rel[0], "to": rel[1],
                                              "error": str(e)})
                    continue
                res["moved"] += 1
            res["moves"].append({"from": rel[0], "to": rel[1]})
    return res


def reattribute(config, dates: list[str], apply: bool = False,
                solve: bool = False, runner=None, max_solves: int = 30) -> dict:
    """CLI core: re-attribute past nights' Piggy subs (whatever the mode).
    Dry run unless apply: then the subs files are rewritten and the Library
    links moved. Goal progress is not synced here (the store belongs to the
    server): POST /api/projects2/recount afterwards."""
    from photonscript.scheduler.runs import edit_subs, runs_dir
    all_dates = sorted({f.name.split("_")[0]
                        for f in runs_dir(config).glob("*_subs.jsonl")})
    if dates:
        want = set(dates)
        all_dates = [d for d in all_dates if d in want]
    nights, all_changed = [], []
    for d in all_dates:
        # PS-140: solves run unlocked, changed fields merge in on exit
        with edit_subs(config, d, hold_lock=False, write=apply) as subs:
            r = attribute_records(config, d, subs, apply=apply, solve=solve,
                                  runner=runner, max_solves=max_solves)
        if not r["piggy"]:
            continue
        by = {}
        for c in r["changed"]:
            k = (c["from"], c["to"], c["src"].split(" ")[0])
            by[k] = by.get(k, 0) + 1
        nights.append({"date": d, "piggy": r["piggy"], "placed": r["placed"],
                       "kept": r["kept"], "no_goal": r["no_goal"],
                       "no_position": r["no_position"], "manual": r["manual"],
                       "changes": [{"from": k[0], "to": k[1], "src": k[2],
                                    "subs": n} for k, n in sorted(by.items())]})
        all_changed.extend(r["changed"])
    lib = library_moves(config, all_changed, apply)
    return {"applied": bool(apply), "nights_scanned": len(all_dates),
            "nights": nights, "subs_changed": len(all_changed),
            "library": lib["library"], "library_moves": lib["moves"],
            "library_collisions": lib["collisions"],
            "next": ("POST /api/projects2/recount to resync goal progress"
                     if apply else "dry run: nothing written; add --apply")}
