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

Read-only: this module never writes, moves or renames anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from photonscript.integration.frames import OSC_FILTER, Frame, read_frame
from photonscript.shared.target_names import canonical_target, target_key

RIGS = ("piggyback", "rc16")
# top-level Library folders that are never targets
NOT_TARGETS = {"calibration", "piggyback", "bad", "_rejected", "_", "_analysis"}
LIGHT_EXT = (".fits", ".fit", ".fts", ".xisf")


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
                  tz: str = "America/Denver") -> Selection:
    """Approved lights of `target` shot with `rig`, nights in [since, until]
    (evening dates, inclusive; '' = open). `filters` limits the filter set
    (mono rig). Duplicate file names across folders count once."""
    if rig not in RIGS:
        raise ValueError(f"unknown rig {rig!r} (use one of {', '.join(RIGS)})")
    sel = Selection(target=target, rig=rig)
    want_filters = {f.upper() for f in filters} if filters else None
    seen: set[str] = set()
    for tdir in target_folders(Path(lib), target):
        sel.folders.append(tdir.name)
        for fdir in _filter_dirs(tdir, rig):
            if want_filters and fdir.name.upper() not in want_filters:
                continue
            for p in sorted(fdir.iterdir()):
                if not p.is_file() or p.suffix.lower() not in LIGHT_EXT:
                    continue
                if p.name in seen:
                    sel.skipped.append((str(p), "duplicate file name"))
                    continue
                try:
                    fr = read(p, kind="LIGHT", tz=tz)
                except Exception as e:  # noqa: BLE001 - unreadable header
                    sel.skipped.append((str(p), f"unreadable header ({e})"))
                    continue
                fr.target_dir = tdir.name
                if rig_of(fr) != rig:
                    sel.skipped.append((str(p), f"rig {rig_of(fr)} frame in a {rig} folder"))
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
    sel.lights.sort(key=lambda f: (f.date_obs, f.name))
    return sel
