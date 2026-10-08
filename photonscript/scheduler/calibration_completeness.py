"""PS-181: calibration completeness, one model per rig.

    completeness(config, rig=None, pending=None, pending_capped=False,
                 names=False) -> dict
    apply_mirror(rep, mirror)        desktop: what D:/ninashare/Library holds
    morning_line(rep) -> str         "Calibration: complete for ... / missing ..."
    format_report(rep) -> str

Jeremy 2026-10-08: "let's make sure that we're capturing all of the biases
and calibration and copying them over please? the darks are the only ones
that have been tough". This joins what the other calibration views each
know into one answer per light set:

  light sets   every light configuration (target, filter, exposure, gain,
               offset, SET-TEMP, readout, binning) of the rig's subs logs in
               the last calibration_completeness_nights nights (every goal,
               rejected subs left out) plus tonight's plan
  requirements per light set: bias (gain, offset, SET-TEMP, readout), darks
               (that exposure at that epoch), flats (that filter; OSC on the
               Piggy-600), each followed through the stages
                 captured     frames of the epoch exist on the scope
                 QA-passed    calibration QA passed, at least the quota
                              (dark_target_count, 50 bias, flat count)
                 in Library   linked in Library/Calibration (what Syncthing
                              ships)
                 on desktop   present in the desktop mirror: from the scope
                              the Syncthing remoteneed cache (a file the
                              desktop still needs is not there yet); on the
                              desktop `photonscript calibration-status`
                              checks D:/ninashare/Library itself
               with the reason at the first stage that is short.

"Usable" is calibration_qa.usable_misses, the same rule the owed view, the
night quota and the library report count with (PS-181: one source of
truth; a frame with no readout recorded older than rig_readout_since does
not count). Frames come from calibration_library.library_report (watch dir,
Library, quarantine, with QA verdicts); flat staleness (age, optics change,
focus / rotator) from calibration_owed. Each rig also carries the darks
windows (calibration_window, cooler history), its deferred dawn plan, the
bias plan (BIAS_AT_SETPOINT when none is usable) and a light-tightness
check of its recent darks (the Piggy-600 has no filter wheel: no dark
slide in front of the sensor). Read only.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

STAGES = ("not_captured", "qa_failed", "short", "stale", "not_in_library",
          "syncing", "complete")
PASS_VERDICTS = ("pass", "warn")
LIGHT_CODES = ("leak", "stars", "level", "daytime_leak")
EXP_TOL_S = 0.5


def _fmt(v) -> str:
    return f"{float(v):g}"


def _canon_filter(config, rig: str, raw) -> str:
    if rig != "rc16":
        return "OSC"
    try:
        rev = config.reverse_filter_map()
    except Exception:  # noqa: BLE001
        rev = {}
    return rev.get(raw or "?", raw or "?")


def light_sets(config, rig: str, *, projects=None, nights: int | None = None,
               now: datetime | None = None) -> list[dict]:
    """The rig's light configurations of the last `nights` nights (every
    goal) and tonight's plan."""
    from photonscript.scheduler.calibration_owed import collect_lights, planned_lights
    from photonscript.scheduler.calibration_plan import load_projects
    now = now or datetime.now()
    nights = int(nights or getattr(config, "calibration_completeness_nights", 14) or 14)
    if projects is None:
        projects = load_projects(config)
    lights = collect_lights(config, rig, list(projects), days=nights, now=now)
    sets: dict[tuple, dict] = {}
    for li in lights + [dict(p, target="tonight's plan", night=None)
                        for p in planned_lights(config, rig)]:
        key = (li["target"], li["filter"], round(float(li["exp_s"]), 1), li["gain"],
               li["offset"], round(float(li["settemp"]) * 2) / 2, li.get("readout"),
               int(li.get("xbin") or 1))
        s = sets.setdefault(key, {
            "target": li["target"], "filter": li["filter"], "exp_s": key[2],
            "gain": li["gain"], "offset": li["offset"], "settemp": key[5],
            "readout": li.get("readout"), "xbin": key[7], "lights": 0,
            "nights": set(), "planned": False})
        if li.get("night"):
            s["lights"] += 1
            s["nights"].add(li["night"])
        else:
            s["planned"] = True
    out = []
    for s in sets.values():
        s["nights"] = sorted(s["nights"])
        out.append(s)
    out.sort(key=lambda s: (s["target"], s["filter"], s["exp_s"]))
    return out


def _req_key(kind: str, ls: dict) -> tuple:
    if kind == "DARK":
        return ("DARK", ls["exp_s"], ls["gain"], ls["offset"], ls["settemp"],
                ls.get("readout"), ls["xbin"])
    if kind == "BIAS":
        return ("BIAS", ls["gain"], ls["offset"], ls["settemp"], ls.get("readout"),
                ls["xbin"])
    return ("FLAT", ls["filter"])


def _req_label(key: tuple) -> str:
    if key[0] == "DARK":
        return (f"darks {_fmt(key[1])} s (gain {key[2]}, offset {key[3]}, "
                f"{_fmt(key[4])} C" + (f", {key[5]}" if key[5] else "") + ")")
    if key[0] == "BIAS":
        return (f"bias (gain {key[1]}, offset {key[2]}, {_fmt(key[3])} C"
                + (f", {key[4]}" if key[4] else "") + ")")
    return f"flats {key[1]}"


def _short_label(r: dict) -> str:
    k = r["key"]
    if k[0] == "DARK":
        txt = f"darks {_fmt(k[1])} s"
    elif k[0] == "BIAS":
        txt = "bias" + (f" {k[4]}" if k[4] else "") + f" {_fmt(k[3])} C"
    else:
        txt = f"flats {k[1]}"
    if r["stage"] in ("short", "qa_failed", "not_captured") and k[0] != "FLAT":
        txt += f" {r['qa_passed']}/{r['need']}"
    elif r["stage"] == "stale":
        txt += " stale"
    elif r["stage"] == "not_captured":
        txt += " none"
    elif r["stage"] == "not_in_library":
        txt += " not filed"
    elif r["stage"] == "syncing":
        txt += " syncing"
    return txt


def _frame_usable(fr: dict, key: tuple, default_ro, since) -> bool:
    from photonscript.scheduler.calibration_qa import usable_misses
    if key[0] == "DARK":
        ep = {"gain": key[2], "offset": key[3], "setpoint": key[4], "readout": key[5]}
    else:
        ep = {"gain": key[1], "offset": key[2], "setpoint": key[3], "readout": key[4]}
    rec = {"gain": fr.get("gain"), "offset": fr.get("offset"),
           "settemp": fr.get("settemp"), "date": fr.get("date"),
           "readout": None if fr.get("readout_assumed") else fr.get("readout")}
    return not usable_misses(rec, ep, default_ro=default_ro, since=since)


def _pool(config, rig: str, key: tuple, frames: list, default_ro, since,
          cal_cutoff: str) -> list:
    out = []
    for fr in frames:
        if fr["type"] != key[0]:
            continue
        if key[0] == "FLAT":
            if _canon_filter(config, rig, fr.get("filter")) == key[1]:
                out.append(fr)
            continue
        if key[0] == "DARK":
            if fr.get("exptime") is None or abs(fr["exptime"] - key[1]) >= EXP_TOL_S:
                continue
            if fr["date"] < cal_cutoff:
                continue
        if _frame_usable(fr, key, default_ro, since):
            out.append(fr)
    return out


def _top(reasons: list, n: int = 2) -> str:
    c = Counter(r for r in reasons if r)
    return "; ".join(f"{r} (x{k})" if k > 1 else r for r, k in c.most_common(n))


def _evaluate(config, rig: str, key: tuple, frames: list, *, default_ro, since,
              cal_cutoff: str, need: int, flat_item: dict | None,
              dark_item: dict | None, unusable_bias: list, pending, pending_capped,
              names: bool) -> dict:
    pool = _pool(config, rig, key, frames, default_ro, since, cal_cutoff)
    passed = [f for f in pool if f.get("verdict") in PASS_VERDICTS]
    session = None
    if key[0] in ("BIAS", "FLAT"):
        session = (flat_item or {}).get("last") if key[0] == "FLAT" else None
        dates = sorted({f["date"] for f in passed})
        if session is None or session not in dates:
            session = dates[-1] if dates else None
        counted = [f for f in passed if f["date"] == session] if session else []
    else:
        counted = passed
    lib = [f for f in counted if f.get("status") == "library"]
    out = {"key": list(key), "kind": key[0], "label": _req_label(key), "need": need,
           "captured": len(pool), "qa_passed": len(counted), "in_library": len(lib),
           "session": session, "on_desktop": None, "desktop_checked": None}
    if pending is not None and lib:
        waiting = sum(1 for f in lib if f.get("name") in pending)
        if waiting or not pending_capped:
            out["on_desktop"] = len(lib) - waiting
            out["desktop_checked"] = "syncthing"
    if names:
        out["library_frames"] = [[f["type"], f["date"], f["name"]] for f in lib]
    assumed = sum(1 for f in counted if f.get("readout_assumed"))
    out["readout_assumed"] = assumed
    out["note"] = (f"{assumed} of the {len(counted)} counted frame(s) have no READOUTM "
                   f"recorded (assumed {default_ro}): the dawn sweep / calibration-qa "
                   "--backfill --dry-run records it" if assumed and default_ro
                   and key[0] != "FLAT" else "")
    out["stage"], out["reason"] = _stage(out, key, pool, frames, flat_item, dark_item,
                                         unusable_bias, counted)
    out["complete"] = out["stage"] == "complete"
    return out


def _stage(r: dict, key: tuple, pool: list, frames: list, flat_item, dark_item,
           unusable_bias: list, counted: list) -> tuple[str, str]:
    need = r["need"]
    if not pool:
        if key[0] == "BIAS":
            why = "no usable bias captured"
            if unusable_bias:
                why += ": " + "; ".join(unusable_bias[:3])
            return "not_captured", why
        if key[0] == "DARK":
            why = "no darks of this epoch captured"
            if dark_item and dark_item.get("fix"):
                why += f" ({dark_item['fix']})"
            elif dark_item and dark_item.get("auto_fill"):
                why += (" (the night quota fills them while the roof is closed and"
                        " the sensor is at the setpoint)")
            return "not_captured", why
        return "not_captured", "no flats of this filter captured"
    if not counted:
        fails = [f.get("reason") or f.get("verdict") for f in pool]
        return "qa_failed", (f"{len(pool)} captured, none passed calibration QA: "
                             f"{_top(fails)}")
    if key[0] == "FLAT" and flat_item and flat_item.get("owed"):
        return "stale", "; ".join(flat_item.get("reasons") or ["owed"])
    if r["qa_passed"] < need:
        bad = [f for f in pool if f.get("verdict") not in PASS_VERDICTS]
        txt = f"{r['qa_passed']} of {need} QA-passed"
        if bad:
            txt += f" ({len(bad)} failed QA: {_top([f.get('reason') for f in bad], 1)})"
        if key[0] == "DARK" and dark_item and dark_item.get("fix"):
            txt += f"; {dark_item['fix']}"
        return "short", txt
    if r["in_library"] < r["qa_passed"]:
        nf = [f.get("reason") for f in counted if f.get("status") != "library"]
        return "not_in_library", (f"{r['qa_passed'] - r['in_library']} QA-passed "
                                  f"frame(s) not in the Library: {_top(nf, 1)}")
    if r["on_desktop"] is not None and r["on_desktop"] < r["in_library"]:
        return "syncing", (f"{r['in_library'] - r['on_desktop']} Library frame(s) "
                           "still on their way to the desktop (Syncthing)")
    return "complete", ""


def light_tightness(config, rig: str, *, days: int = 45,
                    now: datetime | None = None) -> dict:
    """Recent darks of the rig that failed a light check (leak, stars,
    level). The Piggy-600 has no filter wheel, so nothing but the roof (and
    a lens cap) keeps light off its sensor during darks."""
    from photonscript.scheduler import calibration_qa as cq
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    recs = [r for r in cq.load_store(config, rig)["frames"].values()
            if r.get("type") == "DARK" and str(r.get("date") or "") >= cutoff
            and r.get("verdict")]
    lit = [r for r in recs if any(c in LIGHT_CODES for c in r.get("codes") or [])
           and "temp" not in (r.get("codes") or [])]
    dates = sorted({r["date"] for r in lit})
    if not recs:
        verdict, text = "unknown", "no recent QA'd darks to judge"
    elif lit:
        verdict = "warn"
        text = (f"{len(lit)} of {len(recs)} darks since {cutoff} show light "
                f"(leak / stars / level) on {', '.join(dates[-4:])}")
        if rig != "rc16":
            text += (": the Piggy-600 has no filter wheel or dark slide, so cap the "
                     "lens or shoot its darks only with the roof closed at night "
                     "(companion OSC_DARKS_IF_UNSAFE needs the safety monitor in the "
                     "NINA #2 profile)")
    else:
        verdict, text = "ok", f"{len(recs)} darks since {cutoff}, none shows light"
    return {"verdict": verdict, "checked": len(recs), "light_failures": len(lit),
            "dates": dates, "text": text}


def _rig(config, rig: str, *, projects, now, pending, pending_capped, names,
         nights) -> dict:
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.scheduler.calibration import dark_epoch
    from photonscript.scheduler.calibration_library import library_report
    from photonscript.scheduler.calibration_owed import owed_report
    from photonscript.scheduler.calibration_plan import BIAS_COUNT, estimate_minutes
    from photonscript.shared.rigs import rig_label, rig_readout, rig_readout_since
    ep = dark_epoch(config, rig)
    default_ro, since = rig_readout(config, rig), rig_readout_since(config, rig)
    sets = light_sets(config, rig, projects=projects, nights=nights, now=now)
    lib = library_report(config, rig, now=now, frames_limit=10 ** 7)
    frames = lib.get("frames") or []
    active = [p for p in projects if getattr(p, "active", True)]
    owed = owed_report(config, rig, projects=active, now=now)["rigs"][0]
    flat_items = {f["filter"]: f for f in owed["flats"]}
    cal_days = int(getattr(config, "library_cal_days", 120))
    cal_cutoff = (now - timedelta(days=cal_days)).strftime("%Y-%m-%d")
    dark_need = int(getattr(config, "dark_target_count", 30))
    flat_need = int(getattr(config, "flat_count", 15) if rig == "rc16"
                    else getattr(config, "piggyback_flat_count", 25))

    def dark_item(key):
        for d in owed["darks"]:
            if (abs(d["exp_s"] - key[1]) < EXP_TOL_S and d["gain"] == key[2]
                    and d["offset"] == key[3] and abs(d["settemp"] - key[4]) < 1.5
                    and d.get("readout") == key[5]):
                return d
        return None

    unusable_bias = [
        f"{s['date']} x{s['n']}: {'; '.join(s['epoch_misses'])}"
        for s in sorted((s for s in lib.get("sets") or []
                         if s["type"] == "BIAS" and not s["usable"]),
                        key=lambda s: s["date"], reverse=True)]
    reqs: dict[tuple, dict] = {}
    for ls in sets:
        ls["requires"] = []
        for kind in ("BIAS", "DARK", "FLAT"):
            key = _req_key(kind, ls)
            if key not in reqs:
                need = (dark_need if kind == "DARK" else BIAS_COUNT if kind == "BIAS"
                        else int((flat_items.get(key[1]) or {}).get("need") or flat_need))
                reqs[key] = _evaluate(
                    config, rig, key, frames, default_ro=default_ro, since=since,
                    cal_cutoff=cal_cutoff, need=need,
                    flat_item=flat_items.get(key[1]) if kind == "FLAT" else None,
                    dark_item=dark_item(key) if kind == "DARK" else None,
                    unusable_bias=unusable_bias, pending=pending,
                    pending_capped=pending_capped, names=names)
            ls["requires"].append(_req_label(key))
    by_label = {r["label"]: r for r in reqs.values()}
    for ls in sets:
        mine = [by_label[lb] for lb in ls["requires"]]
        ls["complete"] = all(r["complete"] for r in mine)
        ls["missing"] = [_short_label(r) for r in mine if not r["complete"]]
        ls["usable_now"] = all(r["in_library"] > 0 for r in mine)
    targets: dict[str, dict] = {}
    for ls in sets:
        t = targets.setdefault(ls["target"], {"target": ls["target"], "complete": True,
                                              "missing": []})
        t["complete"] = t["complete"] and ls["complete"]
        for m in ls["missing"]:
            if m not in t["missing"]:
                t["missing"].append(m)
    owed_darks = [[r["key"][1], max(0, r["need"] - r["qa_passed"])]
                  for r in reqs.values() if r["kind"] == "DARK" and not r["complete"]
                  and r["qa_passed"] < r["need"]]
    bias_missing = any(r["kind"] == "BIAS" and r["qa_passed"] == 0 for r in reqs.values())
    minutes = estimate_minutes([(e, n) for e, n in owed_darks],
                               BIAS_COUNT if bias_missing else 0) if (owed_darks or bias_missing) else 0
    win = None
    if owed_darks:
        try:
            from photonscript.scheduler.calibration_window import windows
            win = windows(config, rig, minutes=minutes, now=datetime.utcnow())
        except Exception as e:  # noqa: BLE001 - the report stands without it
            logger.warning("darks windows failed for %s: %s", rig, e)
    try:
        from photonscript.scheduler.calibration_window import deferred
        dplan = deferred(config, rig)
    except Exception:  # noqa: BLE001
        dplan = None
    bias_plan = None
    if bias_missing:
        bias_plan = ("BIAS_AT_SETPOINT: tonight's sequence shoots 50 bias at the "
                     "setpoint first thing (cooler gate, any roof)"
                     if getattr(config, "calibration_bias_when_missing", True) else
                     "no usable bias and calibration_bias_when_missing is off: only "
                     "the unsafe-night top-up or a capture job shoots it")
    try:
        tight = light_tightness(cq.rig_view(config, rig), rig, now=now)
    except Exception as e:  # noqa: BLE001
        tight = {"verdict": "unknown", "text": f"unavailable ({e})"}
    reqs_out = sorted(reqs.values(), key=lambda r: (r["kind"], str(r["key"])))
    return {"rig": rig, "name": rig_label(config, rig),
            "epoch": {**ep, "readout_since": since}, "nights": nights,
            "light_sets": sets, "requirements": reqs_out,
            "targets": sorted(targets.values(), key=lambda t: t["target"]),
            "complete": all(r["complete"] for r in reqs_out) if reqs_out else True,
            "darks_owed": owed_darks, "capture_minutes": round(minutes),
            "windows": win, "deferred": dplan, "bias_plan": bias_plan,
            "light_tight": tight, "readout_note": owed.get("readout_note"),
            "desktop": ("syncthing remoteneed cache" if pending is not None
                        else "not checked (run photonscript calibration-status on "
                             "the desktop)")}


def completeness(config, rig: str | None = None, *, projects=None,
                 now: datetime | None = None, pending: set | None = None,
                 pending_capped: bool = False, names: bool = False,
                 nights: int | None = None) -> dict:
    from photonscript.scheduler.calibration_plan import load_projects
    from photonscript.shared.rigs import rig_ids
    now = now or datetime.now()
    if projects is None:
        projects = load_projects(config)
    nights = int(nights or getattr(config, "calibration_completeness_nights", 14) or 14)
    out = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "nights": nights, "rigs": []}
    for rg in ([rig] if rig else rig_ids(config)):
        try:
            out["rigs"].append(_rig(config, rg, projects=list(projects), now=now,
                                    pending=pending, pending_capped=pending_capped,
                                    names=names, nights=nights))
        except Exception as e:  # noqa: BLE001 - one rig must not hide the other
            logger.warning("completeness failed for %s", rg, exc_info=True)
            out["rigs"].append({"rig": rg, "error": f"{type(e).__name__}: {e}"})
    out["line"] = morning_line(out)
    return out


_line_cache: dict = {}
LINE_TTL_S = 600.0


def cached_line(config, *, projects=None, pending=None, pending_capped=False,
                ttl_s: float = LINE_TTL_S) -> str:
    """morning_line of a fresh completeness() at most every ttl_s (the
    dashboard's morning card polls; the library walk is not free)."""
    import time
    key = str(getattr(config, "data_dir", ""))
    hit = _line_cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    line = completeness(config, projects=projects, pending=pending,
                        pending_capped=pending_capped)["line"]
    _line_cache[key] = (time.monotonic(), line)
    return line


def apply_mirror(rep: dict, mirror: Path) -> dict:
    """Desktop side: count each requirement's Library frames that the mirror
    (D:/ninashare/Library; the Piggy-600 under piggyback/) holds, and re-judge
    the stage. Needs the report built with names=True. Read only."""
    base = Path(mirror)
    for r in rep.get("rigs") or []:
        if r.get("error"):
            continue
        root = (base / "piggyback" if r["rig"] != "rc16" else base) / "Calibration"
        for q in r["requirements"]:
            fr = q.get("library_frames")
            if fr is None:
                continue
            q["on_desktop"] = sum(1 for t, d, n in fr if (root / t / d / n).exists())
            q["desktop_checked"] = str(base)
            if q["stage"] in ("complete", "syncing"):
                if q["on_desktop"] < q["in_library"]:
                    q["stage"] = "syncing"
                    q["reason"] = (f"{q['in_library'] - q['on_desktop']} of "
                                   f"{q['in_library']} Library frame(s) not in "
                                   f"{base} yet (Syncthing)")
                else:
                    q["stage"], q["reason"] = "complete", ""
                q["complete"] = q["stage"] == "complete"
        by_label = {q["label"]: q for q in r["requirements"]}
        for ls in r["light_sets"]:
            mine = [by_label[lb] for lb in ls["requires"] if lb in by_label]
            ls["complete"] = all(q["complete"] for q in mine)
            ls["missing"] = [_short_label(q) for q in mine if not q["complete"]]
        tg: dict = {}
        for ls in r["light_sets"]:
            t = tg.setdefault(ls["target"], {"target": ls["target"], "complete": True,
                                             "missing": []})
            t["complete"] = t["complete"] and ls["complete"]
            t["missing"] += [m for m in ls["missing"] if m not in t["missing"]]
        r["targets"] = sorted(tg.values(), key=lambda t: t["target"])
        r["complete"] = all(q["complete"] for q in r["requirements"])
        r["desktop"] = str(base)
    rep["line"] = morning_line(rep)
    return rep


def morning_line(rep: dict) -> str:
    """One line: "Calibration: complete for RC16 M 31; Piggy-600 M 31 /
    missing RC16 Heart Nebula (darks 600 s 4/30, bias HCG 0 C 0/50)"."""
    done, miss = [], []
    for r in rep.get("rigs") or []:
        if r.get("error"):
            miss.append(f"{r.get('rig')} (report failed)")
            continue
        for t in r.get("targets") or []:
            if t["target"] == "tonight's plan":
                name = f"{r['name']} tonight"
            else:
                name = f"{r['name']} {t['target']}"
            if t["complete"]:
                done.append(name)
            else:
                more = f" +{len(t['missing']) - 3}" if len(t["missing"]) > 3 else ""
                miss.append(f"{name} ({', '.join(t['missing'][:3])}{more})")
    if not done and not miss:
        return "Calibration: no lights in the window"
    parts = []
    if done:
        parts.append("complete for " + ", ".join(done))
    if miss:
        parts.append("missing " + "; ".join(miss))
    return "Calibration: " + " / ".join(parts)


def format_report(rep: dict) -> str:
    lines = [f"Calibration completeness ({rep.get('generated')}, light sets of the "
             f"last {rep.get('nights')} nights + tonight's plan)", rep.get("line", "")]
    for r in rep.get("rigs") or []:
        lines.append("")
        if r.get("error"):
            lines.append(f"{r['rig']}: report failed: {r['error']}")
            continue
        ep = r["epoch"]
        lines.append(f"{r['name']} ({r['rig']}): gain {ep['gain']} offset {ep['offset']} "
                     f"{_fmt(ep['setpoint'])} C {ep.get('readout') or ''}; desktop: "
                     f"{r['desktop']}")
        for q in r["requirements"]:
            desk = ("-" if q["on_desktop"] is None else str(q["on_desktop"]))
            lines.append(f"  {q['label']:<46} captured {q['captured']:>3}  QA {q['qa_passed']:>3}"
                         f"/{q['need']:<3} Library {q['in_library']:>3}  desktop {desk:>3}  "
                         f"{q['stage']}" + (f": {q['reason']}" if q["reason"] else ""))
            if q.get("note"):
                lines.append(f"    note: {q['note']}")
        for t in r["targets"]:
            lines.append(f"  target {t['target']}: "
                         + ("complete" if t["complete"] else "missing " + ", ".join(t["missing"])))
        if r.get("bias_plan"):
            lines.append(f"  bias plan: {r['bias_plan']}")
        w = r.get("windows")
        if w:
            lines.append(f"  darks: {r['capture_minutes']} min owed; recommended window: "
                         f"{w.get('recommended') or 'none fits'}")
            for x in w["windows"]:
                reach = {True: "reachable", False: "NOT reachable",
                         None: "unknown"}[x["reachable"]]
                lines.append(f"    {x['name']:<22} {x['start']} .. {x['end']} "
                             f"({x['minutes']} min) {reach}"
                             + (f": {x['why']}" if x.get("why") else ""))
        if r.get("deferred"):
            d = r["deferred"]
            lines.append(f"  deferred plan: {', '.join(f'{_fmt(e)} s x{n}' for e, n in d['darks'])}"
                         + (f" + {d['bias']} bias" if d.get("bias") else "")
                         + f" ({d.get('reason')})")
        lt = r.get("light_tight") or {}
        lines.append(f"  light-tight darks: {lt.get('verdict')}: {lt.get('text')}")
        if r.get("readout_note"):
            lines.append(f"  note: {r['readout_note']}")
    return "\n".join(lines)
