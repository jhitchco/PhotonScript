"""Pointing sanity at Library filing (dawn filing, Approve).

The Library files a light under the target the sequence named, which is not
always where the mount was (2026-10-13: an "NGC 2408" run held frames whose
RA / DEC header was M27's; the integrate step caught it, the Library had
already filed them). Before build_library or an Approve links a light into
<Target>/<Filter>/, the integrate step's header checks run on it
(integration.pointing_check, reused as is):

  off_target     header RA / DEC more than library_pointing_max_deg (1 deg)
                 from the target's catalog position (shared.pointing
                 target_coords: the project first, then the catalog);
  mount_stopped  two lights of one night whose altitude (and azimuth) stayed
                 pinned while RA ran at the sidereal rate.

The altitude check is not run here (a low sub is not a misfiled one).
Frames without RA / DEC (the Piggy-600) cannot be judged and file as before.

library_pointing_check:
  warn (default)  the sub stays out of the target folder and goes back to
                  review (passed, not reviewed, review_source "pointing")
                  with the reason in reason / library_pointing; dawn filing
                  never re-approves it. A person's Approve of a sub that
                  carries the reason files it (override recorded).
  refuse          the sub is rejected automatically with the reason.
  off             no check.

Only subs about to get a NEW link are checked (dawn filing files a whole
night at once, an Approve one sub; a full rebuild does not re-read the
headers of every filed sub). Nothing here writes a FITS file.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("warn", "refuse", "off")
SOURCE = "pointing"          # review_source of a sub this check held back


def mode(config) -> str:
    m = str(getattr(config, "library_pointing_check", "warn") or "warn").strip().lower()
    return m if m in MODES else "warn"


def max_deg(config) -> float:
    try:
        return max(0.0, float(getattr(config, "library_pointing_max_deg", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def held(rec: dict) -> bool:
    """True when this check sent the sub back to review and no person has
    filed it since (dawn auto-approve skips it)."""
    lp = rec.get("library_pointing") or {}
    return bool(lp.get("issues")) and not lp.get("override") \
        and rec.get("review_source") == SOURCE


def overridden(rec: dict) -> bool:
    """A person approved the sub knowing the reason (it was held before)."""
    lp = rec.get("library_pointing") or {}
    return bool(lp.get("issues")) and bool(rec.get("manual_qa")) \
        and rec.get("review_source") == "manual"


def _read(path):
    from photonscript.integration.frames import read_frame
    return read_frame(path)


def screen(config, recs: list[dict], target_of, reader=None) -> dict[str, list[str]]:
    """{file: [issue, ...]} for the subs in `recs` that fail the checks.
    target_of(rec) -> the target name the Library would file it under.
    A sub a person already filed over the reason (overridden) and a sub
    whose FITS cannot be read are never flagged. Never raises."""
    if mode(config) == "off" or not recs:
        return {}
    from photonscript.integration import pointing_check as pcheck
    from photonscript.shared.pointing import target_coords
    reader = reader or _read
    groups: dict[tuple, list] = {}
    for r in recs:
        if overridden(r) or not r.get("abs_path"):
            continue
        try:
            f = reader(r["abs_path"])
        except Exception as e:  # noqa: BLE001 - unreadable: file as before
            logger.debug("library pointing: header of %s not read: %s",
                         r.get("abs_path"), e)
            continue
        name = target_of(r)
        groups.setdefault((r.get("rig") or "rc16", name), []).append((r, f))
    out: dict[str, list[str]] = {}
    for (_rig, name), pairs in groups.items():
        hit = None
        try:
            hit = target_coords(config, name)
        except Exception as e:  # noqa: BLE001
            logger.debug("library pointing: no coordinates for %s: %s", name, e)
        ra, dec = (hit[1], hit[2]) if hit else (None, None)
        try:
            rep = pcheck.check([f for _r, f in pairs], ra, dec, mode="warn",
                               max_deg=max_deg(config), min_alt_deg=0.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("library pointing check failed for %s: %s", name, e)
            continue
        by_name = {f.name: r for r, f in pairs}
        for fname, issues in rep.flagged.items():
            r = by_name.get(fname)
            if r is not None:
                out[r["file"]] = [f"{i} (filed as {name})" if i.startswith("off_target")
                                  else i for i in issues]
    return out


def reason_text(issues: list[str]) -> str:
    return "pointing: " + "; ".join(issues)


def apply_to(rec: dict, issues: list[str], how: str, now: str | None = None) -> None:
    """Hold one record back (in place): warn -> review, refuse -> rejected."""
    now = now or datetime.utcnow().isoformat(timespec="seconds") + "Z"
    lp = {"issues": list(issues), "t": now, "mode": how}
    if how == "refuse":
        rec.update(passed_qa=False, reviewed=False, manual_qa=False,
                   auto_verdict="rejected", reason=reason_text(issues),
                   review_source=SOURCE, library_pointing=lp)
    else:
        rec.update(passed_qa=True, reviewed=False, manual_qa=False,
                   reason=reason_text(issues), review_source=SOURCE,
                   library_pointing=lp)
    rec.pop("approved_by", None)


def hold(config, date: str, flagged: dict[str, list[str]]) -> list[dict]:
    """Write the hold-backs into the night's subs log (under its lock) and
    drop any Library link they already have. Returns the changed records."""
    if not flagged:
        return []
    from photonscript.scheduler import runs
    from photonscript.scheduler.qa_backfill import _library_links
    from photonscript.shared.sub_file import norm_file
    how = mode(config)
    want = {norm_file(f): v for f, v in flagged.items()}
    hits = []
    with runs.edit_subs(config, date) as subs:
        for s in subs:
            issues = want.get(norm_file(s.get("file")))
            if issues:
                apply_to(s, issues, how)
                hits.append(s)
    for s in hits:
        for link in _library_links(config, s):
            try:
                Path(link).unlink(missing_ok=True)
            except OSError as e:
                logger.warning("library pointing: link %s not removed: %s", link, e)
        logger.warning("Library pointing (%s): %s %s", how, s.get("file"),
                       reason_text(s["library_pointing"]["issues"]))
    return hits
