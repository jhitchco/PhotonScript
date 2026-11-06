"""PS-98: reach rejected subs from the desktop without syncing them.

The synced Library holds accepted subs only, and rejected FITS on the scope
PC get deleted to keep its disk lean. What stays is small: each sub's
record in the subs log and its PS-80 star sidecar
(<data_dir>/stars/<night>/<rig>/<stem>.json, about 20 KB). This module
serves those, read only:

    night_stars(config, date, rig, state)   every sub's record summary and
                                            star sidecar for one night (the
                                            trailing / flexure diagnosis
                                            works from these alone)
    reject_fits(config, date, file)         one rejected FITS, while it
                                            still exists on the scope PC

Nothing here writes, moves or deletes a file, and nothing measures a FITS
(a sub without a sidecar is listed with "sidecar": null).
"""
from __future__ import annotations

from pathlib import Path

STATES = ("rejected", "accepted", "all")

# the record fields worth carrying to the desktop (scalars only)
KEEP = ("file", "rig", "target", "filter", "exp_s", "time", "start_utc",
        "end_utc", "hfr", "fwhm_arcsec", "ecc", "stars", "background",
        "ccd_temp", "passed_qa", "reason", "drivers", "qa_flag",
        "auto_verdict", "reviewed", "guiding_rms", "pier_side")


def _is_rejected(rec: dict) -> bool:
    return not rec.get("passed_qa")


def night_stars(config, date: str, rig: str = "piggyback",
                state: str = "rejected") -> dict:
    """{"night", "rig", "state", "count", "with_sidecar", "subs": [{record
    summary..., "fits_on_scope": bool, "sidecar": {...} | None}]} for one
    night and rig. state: rejected (default) | accepted | all. Sidecars are
    read only (never measured on request); ecc in sqrt form."""
    from photonscript.scheduler.runs import _load_subs, _sub_source
    from photonscript.scheduler.sub_viewer import _normalized
    from photonscript.shared import star_table
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    out: list[dict] = []
    for rec in _load_subs(config, date):
        if (rec.get("rig") or "rc16") != rig or not rec.get("file"):
            continue
        rej = _is_rejected(rec)
        if (state == "rejected" and not rej) or (state == "accepted" and rej):
            continue
        row = {k: rec.get(k) for k in KEEP if k in rec}
        row["state"] = "rejected" if rej else "accepted"
        src, _ = _sub_source(config, date, rec["file"])
        row["fits_on_scope"] = src is not None
        t = star_table.read(config, date, rec["file"], rig)
        row["sidecar"] = _normalized(t, rec) if t is not None else None
        out.append(row)
    return {"night": date, "rig": rig, "state": state, "count": len(out),
            "with_sidecar": sum(1 for r in out if r["sidecar"] is not None),
            "subs": out}


def reject_fits(config, date: str, file: str) -> tuple[int, Path | str]:
    """(200, path) for a REJECTED sub's FITS that is still on the scope PC,
    else (status, why): 400 a bad name, 404 no such sub / the FITS is gone,
    403 an accepted sub (it is in the synced Library already)."""
    from photonscript.scheduler.runs import _load_subs, _sub_source
    from photonscript.shared.sub_file import norm_file
    f = norm_file(file)
    if not f or ".." in f or f.startswith("/") or ":" in f:
        return 400, "bad file name"
    rec = next((r for r in _load_subs(config, date)
                if norm_file(r.get("file")) == f), None)
    if rec is None:
        return 404, "no such sub in that night's log"
    if not _is_rejected(rec):
        return 403, "accepted sub: it is in the synced Library"
    src, _ = _sub_source(config, date, f)
    if src is None or src.suffix.lower() not in (".fits", ".fit", ".fts"):
        return 404, "the FITS is no longer on the scope PC (deleted)"
    return 200, src
