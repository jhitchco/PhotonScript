"""PS-178: calibration frames on the scope vs in the Library, and why.

2026-10-08: `photonscript integrate --target M31 --rig rc16 --dry-run` on the
desktop found no 300 s darks and no bias, while /api/calibration/health on
the scope listed 300 s darks from 2026-10-01 and a 2026-09-30 bias. The
health view counts the NINA watch dir AND the Library, so it cannot say
which frames the desktop will ever see. This report walks the same frames
per rig and gives each one a status and the reason:

  library      linked in Library/Calibration (Syncthing ships it)
  quarantine   failed calibration QA (PS-113); the reasons from the store
  not_filed    still only in the NINA watch dir, with why: QA verdict fail
               (it will be quarantined when filed), older than
               library_cal_days, archived, a calibration-only folder with no
               run record (build_library(date) never filed those before the
               PS-178 dawn sweep), or simply waiting for the dawn filing

and a light-epoch check for darks and bias (gain, offset, SET-TEMP vs the
rig setpoint, readout mode, PS-128): "usable" says whether integrate could
ever match the frame to tonight's lights. Example: the 2026-09-30 RC16 bias
was shot at SET-TEMP 20 C (sensor 25 to 30 C), so it fails QA and can
never calibrate 0 C lights.

compare_desktop() adds what the desktop mirror holds (the CLI on the
desktop fetches the scope report over GET and compares it with
D:/ninashare/Library): a frame linked on the scope but missing on the
desktop is "Syncthing pending".

Read only: listings, the QA store and (only for frames the store lacks) a
header read. Nothing is moved, linked or measured.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STATUSES = ("library", "quarantine", "not_filed")


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _header_fields(path: Path) -> dict:
    """Header-only read for a frame the QA store does not know."""
    try:
        from astropy.io import fits

        from photonscript.scheduler.calibration_qa import header_fields
        return header_fields(fits.getheader(path))
    except Exception as e:  # noqa: BLE001 - one unreadable frame
        return {"error": f"{type(e).__name__}: {e}"}


def epoch_misses(rec: dict, epoch: dict, *, tol: float = 1.0) -> list[str]:
    """Why a DARK / BIAS frame cannot match the rig's lights ([] = usable).
    epoch = calibration.dark_epoch(config, rig). A frame with no readout
    keyword is assumed to be at the rig's readout (PS-128)."""
    out = []
    g, o = _num(rec.get("gain")), _num(rec.get("offset"))
    if g is not None and epoch.get("gain") is not None and g != float(epoch["gain"]):
        out.append(f"gain {g:g} vs {epoch['gain']}")
    if o is not None and epoch.get("offset") is not None and o != float(epoch["offset"]):
        out.append(f"offset {o:g} vs {epoch['offset']}")
    st = _num(rec.get("settemp"))
    sp = epoch.get("setpoint")
    if st is not None and sp is not None and abs(st - float(sp)) > tol:
        out.append(f"SET-TEMP {st:g} C vs setpoint {float(sp):g} C")
    want = epoch.get("readout")
    ro = rec.get("readout")
    if want and ro and ro != want:
        out.append(f"readout {ro} vs {want}")
    return out


def _subs_nights(config) -> set[str]:
    try:
        from photonscript.scheduler.runs import runs_dir
        return {f.name.split("_")[0] for f in runs_dir(config).glob("*_subs.jsonl")}
    except Exception:  # noqa: BLE001
        return set()


def _quarantine_frames(lib: Path):
    from photonscript.scheduler.calibration_qa import QUARANTINE_DIR
    q = lib / "Calibration" / QUARANTINE_DIR
    if not q.is_dir():
        return
    for tdir in sorted(p for p in q.iterdir() if p.is_dir()):
        for ddir in sorted(p for p in tdir.iterdir() if p.is_dir()):
            for f in ddir.glob("*.fits"):
                yield tdir.name.upper(), ddir.name, f


def library_report(config, rig: str = "rc16", *, now: datetime | None = None,
                   frames_limit: int = 0) -> dict:
    """Every calibration frame of `rig` in its watch dir, Library and
    quarantine with a status and a reason, grouped into sets
    (type, folder date, exposure or filter, readout, SET-TEMP, status,
    reason). frames_limit > 0 also lists that many single frames
    (not-filed and quarantined first)."""
    from photonscript.scheduler import calibration as cal
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.scheduler.library_archive import archived_kinds
    from photonscript.scheduler.runs import library_root
    from photonscript.shared.rigs import rig_readout

    now = now or datetime.now()
    view = cq.rig_view(config, rig)
    watch = Path(view.image_watch_dir)
    lib = library_root(view)
    cal_days = int(getattr(config, "library_cal_days", 120))
    epoch = cal.dark_epoch(config, rig)
    default_ro = rig_readout(config, rig)
    tol = cq.temp_tol(config)
    store = cq.load_store(config, rig)["frames"]
    subs_nights = _subs_nights(config) if rig == "rc16" else None

    seen: dict[tuple, dict] = {}

    def slot(typ, d, f):
        k = (typ, d, f.name)
        e = seen.get(k)
        if e is None:
            e = seen[k] = {"type": typ, "date": d, "name": f.name, "path": str(f),
                           "watch": False, "library": False, "quarantine": False}
        return e

    if watch.is_dir() and watch.resolve() != lib.resolve():
        for typ, d, f in cal._iter_watch_frames(watch):
            if typ in ("BIAS", "DARK", "FLAT"):
                slot(typ, d, f)["watch"] = True
    for typ, d, f in cal._iter_library_frames(lib):
        slot(typ, d, f)["library"] = True
    for typ, d, f in _quarantine_frames(lib):
        if typ in ("BIAS", "DARK", "FLAT"):
            slot(typ, d, f)["quarantine"] = True

    frames = []
    for (typ, d, name), e in sorted(seen.items()):
        rec = store.get(cq.frame_key(typ, d, name))
        info = dict(rec) if rec else _header_fields(Path(e["path"]))
        if rec is None or not info.get("readout"):
            info.setdefault("readout", None)
        verdict = (rec or {}).get("verdict") or "unchecked"
        reasons = list((rec or {}).get("reasons") or [])
        if e["quarantine"] and not e["library"]:
            status, why = "quarantine", "; ".join(reasons[:2]) or "QA fail (reasons.json)"
        elif e["library"]:
            status, why = "library", ""
        else:
            status = "not_filed"
            try:
                age = (now - datetime.strptime(d, "%Y-%m-%d")).days
            except ValueError:
                age = 0
            if verdict == "fail":
                why = "QA fail, will be quarantined when filed: " + "; ".join(reasons[:2])
            elif age > cal_days:
                why = f"older than library_cal_days ({age} > {cal_days} days)"
            elif typ in archived_kinds(config, d):
                why = "night archived (library_archive): not re-linked"
            elif subs_nights is not None and d not in subs_nights:
                why = ("calibration-only folder (no run record for this date), so "
                       "build_library(date) never filed it; the PS-178 dawn sweep files it")
            else:
                why = "not filed yet (dawn filing / Approve night)"
        exp = _num(info.get("exptime"))
        ro = info.get("readout")
        ro_assumed = not ro
        misses = epoch_misses({**info, "readout": ro or default_ro}, epoch, tol=tol) \
            if typ in ("DARK", "BIAS") else []
        frames.append({
            "type": typ, "date": d, "name": name, "status": status, "reason": why,
            "verdict": verdict, "exptime": exp, "filter": info.get("filter"),
            "settemp": _num(info.get("settemp")), "ccdtemp": _num(info.get("ccdtemp")),
            "gain": _num(info.get("gain")), "offset": _num(info.get("offset")),
            "readout": ro or default_ro, "readout_assumed": ro_assumed,
            "usable": not misses, "epoch_misses": misses})

    sets: dict[tuple, dict] = {}
    for fr in frames:
        what = fr["filter"] if fr["type"] == "FLAT" else (
            None if fr["exptime"] is None else round(fr["exptime"], 3))
        k = (fr["type"], fr["date"], what, fr["readout"], fr["settemp"], fr["status"],
             fr["reason"], "; ".join(fr["epoch_misses"]))
        s = sets.get(k)
        if s is None:
            s = sets[k] = {"type": fr["type"], "date": fr["date"],
                           "exptime": None if fr["type"] == "FLAT" else what,
                           "filter": what if fr["type"] == "FLAT" else None,
                           "readout": fr["readout"], "settemp": fr["settemp"],
                           "status": fr["status"], "reason": fr["reason"],
                           "usable": fr["usable"], "epoch_misses": fr["epoch_misses"],
                           "n": 0, "names": []}
        s["n"] += 1
        s["names"].append(fr["name"])
    totals = {st: sum(1 for f in frames if f["status"] == st) for st in STATUSES}
    totals["usable_in_library"] = sum(1 for f in frames if f["status"] == "library" and f["usable"])
    out = {"rig": rig, "generated": now.isoformat(timespec="seconds"),
           "watch_dir": str(watch), "library": str(lib), "epoch": epoch,
           "library_cal_days": cal_days, "qa_mode": cq.mode(config),
           "totals": totals,
           "sets": sorted(sets.values(), key=lambda s: (s["type"], s["date"],
                                                         str(s["exptime"] or s["filter"]),
                                                         s["status"]),
                          reverse=False)}
    if frames_limit > 0:
        order = {"not_filed": 0, "quarantine": 1, "library": 2}
        out["frames"] = sorted(frames, key=lambda f: (order[f["status"]], f["date"]),
                               )[:frames_limit]
    return out


def compare_desktop(report: dict, desktop_library: Path) -> dict:
    """Add the desktop mirror's view to a (scope) report: per set how many
    frames the mirror holds under Calibration/<TYPE>/<date>/ (or, for a
    quarantined set, under _quarantine) and how many are still on their
    way. Piggy-600 sets live under <mirror>/piggyback/Calibration. Read
    only."""
    from photonscript.scheduler.calibration_qa import QUARANTINE_DIR
    base = Path(desktop_library)
    if report.get("rig") == "piggyback":
        base = base / "piggyback"
    base = base / "Calibration"
    n_on = n_pending = 0
    for s in report.get("sets") or []:
        if s["status"] == "not_filed":
            s["desktop"] = {"present": 0, "pending": 0}
            continue
        folder = (base / QUARANTINE_DIR if s["status"] == "quarantine" else base) / s["type"] / s["date"]
        have = sum(1 for n in s.get("names") or [] if (folder / n).exists())
        s["desktop"] = {"present": have, "pending": len(s.get("names") or []) - have}
        if s["status"] == "library":
            n_on += have
            n_pending += s["desktop"]["pending"]
    report["desktop"] = {"library": str(desktop_library), "library_frames_present": n_on,
                         "library_frames_pending": n_pending}
    return report


def format_report(rep: dict) -> str:
    """Plain-text table, one line per set."""
    t = rep.get("totals") or {}
    ep = rep.get("epoch") or {}
    lines = [f"{rep.get('rig')}: calibration frames, watch dir {rep.get('watch_dir')} vs Library "
             f"{rep.get('library')} (QA mode {rep.get('qa_mode')})",
             f"  lights epoch: gain {ep.get('gain')} offset {ep.get('offset')} setpoint "
             f"{ep.get('setpoint')} C readout {ep.get('readout') or 'any'}",
             f"  totals: {t.get('library', 0)} in the Library ({t.get('usable_in_library', 0)} "
             f"usable for these lights), {t.get('quarantine', 0)} quarantined, "
             f"{t.get('not_filed', 0)} not filed"]
    d = rep.get("desktop")
    if d:
        lines.append(f"  desktop {d['library']}: {d['library_frames_present']} of the Library frames "
                     f"present, {d['library_frames_pending']} still on their way (Syncthing)")
    lines.append("")
    for s in rep.get("sets") or []:
        what = s["filter"] if s["type"] == "FLAT" else (
            "?" if s["exptime"] is None else f"{s['exptime']:g} s")
        st = "?" if s["settemp"] is None else f"{s['settemp']:g} C"
        line = (f"  {s['type']:<5} {s['date']} {what:>8} x{s['n']:<3} {s['readout'] or '-':<4} "
                f"set {st:<6} {s['status']}")
        if s.get("desktop") is not None and s["status"] != "not_filed":
            line += f" (desktop {s['desktop']['present']}/{s['n']})"
        if s["reason"]:
            line += f": {s['reason']}"
        if s["type"] in ("DARK", "BIAS") and not s["usable"]:
            line += f" [not usable: {'; '.join(s['epoch_misses'])}]"
        lines.append(line)
    return "\n".join(lines)
