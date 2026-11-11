"""PS-22: pick the lights to stack from the Library mirror.

The Library is PhotonScript's verdict on the desktop: runs.build_library
links a light into Library/<Target>/<Filter>/ only when it passed QA and was
reviewed (review_gate); rejects go to Library/_rejected (and the old BAD
folder), never into a target folder. So "accepted subs" = the target folders.

A target can be split over several folders ("Andromeda Galaxy" and "M 31",
PS-78 / PS-109): a folder belongs to the target when both names resolve to
the same catalog row (name, catalog id or alias) or to the same canonical
name. Rig: the Piggy-600 shoots Bayer frames (BAYERPAT, filter folder OSC),
the RC16 mono frames (no BAYERPAT).

Wide-field overlap (Piggy-600): the Piggy rides along while the RC16 shoots
mosaic panels, so its subs land under the panel name ("M33 Core P1") although
the wide frame is the same field as the target ("NGC 604"). For the piggyback
rig, lights in other folders whose pointing (FITS RA/DEC) lies within
`overlap_deg` of the target's pointing join the selection. The RC16 field is
small enough that panels really are different frames, so it matches by name.

Read-only: this module never writes, moves or renames anything.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from photonscript.integration.frames import OSC_FILTER, Frame, read_frame
from photonscript.shared.target_names import canonical_target, target_key

RIGS = ("piggyback", "rc16")
# top-level Library folders that are never targets
NOT_TARGETS = {"calibration", "piggyback", "bad", "_rejected", "_", "_analysis"}
LIGHT_EXT = (".fits", ".fit", ".fts", ".xisf")
# Piggy-600 field is about 3.4 x 2.3 deg; a 0.25 deg offset keeps > 85 % overlap
PIGGY_OVERLAP_DEG = 0.25


def sep_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation in degrees (inputs in degrees)."""
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def folder_pointing(tdir: Path, read=read_frame, sample: int = 6,
                    tz: str = "America/Denver") -> tuple[float, float] | None:
    """Median RA/Dec (deg) of up to `sample` lights in a Library target
    folder that carry RA/DEC headers (any filter). The Piggy (NINA #2, no
    mount) writes none, but the RC16 subs in the same folder do."""
    pts = []
    for fdir in sorted(p for p in Path(tdir).iterdir() if p.is_dir()):
        for p in sorted(fdir.iterdir(), reverse=True):
            if len(pts) >= sample:
                break
            if not p.is_file() or p.suffix.lower() not in LIGHT_EXT:
                continue
            try:
                fr = read(p, kind="LIGHT", tz=tz)
            except Exception:  # noqa: BLE001
                continue
            if fr.ra_deg is not None and fr.dec_deg is not None:
                pts.append((fr.ra_deg, fr.dec_deg))
        if len(pts) >= sample:
            break
    if not pts:
        return None
    return (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))


def _catalog_radec(name: str) -> tuple[float, float] | None:
    try:
        from photonscript.shared.astronomy import find_catalog_entry
        e = find_catalog_entry(name) or {}
    except Exception:  # noqa: BLE001
        return None
    ra, dec = e.get("ra"), e.get("dec")
    if ra is None or dec is None:
        return None
    return (float(ra) * 15.0, float(dec))   # catalog RA is in hours


def _catalog_keys(name: str) -> set[str]:
    try:
        from photonscript.shared.astronomy import _entry_keys, find_catalog_entry
        e = find_catalog_entry(name)
        if e:
            return _entry_keys(e)
    except Exception:  # noqa: BLE001 - catalog unavailable: names only
        pass
    return set()


def target_keys(name: str) -> set[str]:
    """Every key a folder name may match for `name`: its own key, its
    canonical (container-stripped) key, and the catalog row's keys."""
    keys = {target_key(name)}
    canon = canonical_target(name)
    if canon:
        keys.add(target_key(canon))
    keys |= _catalog_keys(canon or name)
    return keys - {""}


def same_target(folder: str, target: str) -> bool:
    canon = canonical_target(folder)
    if not canon:
        return False
    fk = {target_key(canon)} | _catalog_keys(canon)
    return bool(fk & target_keys(target))


def target_folders(lib: Path, target: str) -> list[Path]:
    """Top-level Library folders holding lights of `target`, sorted."""
    lib = Path(lib)
    if not lib.is_dir():
        return []
    out = []
    for d in sorted(p for p in lib.iterdir() if p.is_dir()):
        low = d.name.lower()
        if low in NOT_TARGETS or low.startswith("_"):
            continue
        if same_target(d.name, target):
            out.append(d)
    return out


def rig_of(frame: Frame) -> str:
    return "piggyback" if frame.is_osc else "rc16"


def _filter_dirs(tdir: Path, rig: str) -> list[Path]:
    dirs = sorted(p for p in tdir.iterdir() if p.is_dir())
    if rig == "piggyback":
        return [d for d in dirs if d.name.upper() == OSC_FILTER]
    return [d for d in dirs if d.name.upper() != OSC_FILTER]


@dataclass
class Selection:
    target: str
    rig: str
    folders: list[str] = field(default_factory=list)
    overlap_folders: list[str] = field(default_factory=list)   # joined by sky overlap
    overlap_count: int = 0
    lights: list[Frame] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (path, reason)

    def groups(self) -> dict[tuple[str, str], list[Frame]]:
        """(filter, exposure label) -> frames, time ordered."""
        g: dict[tuple[str, str], list[Frame]] = {}
        for f in self.lights:
            g.setdefault((f.filter, f.exp_key), []).append(f)
        return {k: sorted(v, key=lambda f: (f.date_obs, f.name))
                for k, v in sorted(g.items(), key=lambda kv: (kv[0][0], float(kv[0][1][:-1])))}


def select_lights(lib: Path, target: str, rig: str, *, since: str = "", until: str = "",
                  filters: list[str] | None = None, read=read_frame,
                  tz: str = "America/Denver",
                  overlap_deg: float | None = None) -> Selection:
    """Approved lights of `target` shot with `rig`, nights in [since, until]
    (evening dates, inclusive; '' = open). `filters` limits the filter set
    (mono rig). Duplicate file names across folders count once.

    piggyback: lights from other target folders pointing within
    `overlap_deg` (default PIGGY_OVERLAP_DEG; 0 turns it off) of the
    target join too (mosaic panels shot under another name)."""
    if rig not in RIGS:
        raise ValueError(f"unknown rig {rig!r} (use one of {', '.join(RIGS)})")
    sel = Selection(target=target, rig=rig)
    want_filters = {f.upper() for f in filters} if filters else None
    seen: set[str] = set()
    lib = Path(lib)
    named = target_folders(lib, target)

    def scan(tdir: Path, accept=None) -> int:
        n = 0
        for fdir in _filter_dirs(tdir, rig):
            if want_filters and fdir.name.upper() not in want_filters:
                continue
            for p in sorted(fdir.iterdir()):
                if not p.is_file() or p.suffix.lower() not in LIGHT_EXT:
                    continue
                if p.name in seen:
                    if accept is None:
                        sel.skipped.append((str(p), "duplicate file name"))
                    continue
                try:
                    fr = read(p, kind="LIGHT", tz=tz)
                except Exception as e:  # noqa: BLE001 - unreadable header
                    if accept is None:
                        sel.skipped.append((str(p), f"unreadable header ({e})"))
                    continue
                fr.target_dir = tdir.name
                if rig_of(fr) != rig:
                    if accept is None:
                        sel.skipped.append((str(p), f"rig {rig_of(fr)} frame in a {rig} folder"))
                    continue
                if accept is not None and not accept(fr):
                    continue
                if since and fr.night and fr.night < since:
                    sel.skipped.append((str(p), f"night {fr.night} before --since {since}"))
                    continue
                if until and fr.night and fr.night > until:
                    sel.skipped.append((str(p), f"night {fr.night} after --until {until}"))
                    continue
                if fr.exp <= 0:
                    sel.skipped.append((str(p), "no EXPTIME"))
                    continue
                seen.add(p.name)
                sel.lights.append(fr)
                n += 1
        return n

    for tdir in named:
        sel.folders.append(tdir.name)
        scan(tdir)

    radius = PIGGY_OVERLAP_DEG if overlap_deg is None else overlap_deg
    if rig == "piggyback" and radius > 0 and lib.is_dir():
        pts = [(f.ra_deg, f.dec_deg) for f in sel.lights
               if f.ra_deg is not None and f.dec_deg is not None]
        ref = None
        if pts:
            ref = (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))
        for d in named:
            if ref is None:
                ref = folder_pointing(d, read, tz=tz)
        if ref is None:
            ref = _catalog_radec(canonical_target(target) or target)
        if ref is not None:
            named_set = {d.name for d in named}

            for d in sorted(p for p in lib.iterdir() if p.is_dir()):
                low = d.name.lower()
                if d.name in named_set or low in NOT_TARGETS or low.startswith("_"):
                    continue
                if not any(x.is_dir() and x.name.upper() == OSC_FILTER for x in d.iterdir()):
                    continue
                fp = folder_pointing(d, read, tz=tz)

                def near(fr: Frame, fp=fp) -> bool:
                    pos = ((fr.ra_deg, fr.dec_deg)
                           if fr.ra_deg is not None and fr.dec_deg is not None else fp)
                    return pos is not None and sep_deg(pos[0], pos[1], ref[0], ref[1]) <= radius

                n = scan(d, accept=near)
                if n:
                    sel.overlap_folders.append(d.name)
                    sel.overlap_count += n
    sel.lights.sort(key=lambda f: (f.date_obs, f.name))
    return sel
