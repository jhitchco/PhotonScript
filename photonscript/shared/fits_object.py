"""Stamp the target name into a FITS OBJECT header when it is missing.

NINA on the AARO rig does not write the OBJECT keyword even though the
sequence's DeepSkyObjectContainer knows the target, so every light lands with
a blank OBJECT and the runs page / downstream tools see target '?'. PhotonScript
resolves the name (from NINA's live target, the plan, or a plate-solved
position) and writes it back here — never overwriting a name that is already
present, and never touching anything but OBJECT.

Best-effort by design: any failure is logged and swallowed so a header write
can never break grading, transfer, or identify.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def header_object(path) -> str:
    """Return the OBJECT value already in the FITS header ('' if none)."""
    try:
        from astropy.io import fits
        return str(fits.getheader(str(path)).get("OBJECT") or "").strip()
    except Exception:  # noqa: BLE001 - unreadable / not a FITS
        return ""


def stamp_object(path, name: str, *, overwrite: bool = False) -> bool:
    """Write OBJECT=name into the FITS header. Returns True if modified.

    By default this only fills a *blank* OBJECT — an existing target name is
    left untouched. Pass overwrite=True to replace it. A missing file, a blank
    or '?' name, or any astropy error is a no-op returning False.
    """
    name = str(name or "").strip()
    if not name or name == "?":
        return False
    p = Path(path)
    if not p.is_file():
        return False
    try:
        from astropy.io import fits
        with fits.open(str(p), mode="update", memmap=False) as hdul:
            hdr = hdul[0].header
            if not overwrite and str(hdr.get("OBJECT") or "").strip():
                return False
            hdr["OBJECT"] = (name[:68], "Target (stamped by PhotonScript)")
            hdul.flush()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("OBJECT stamp failed for %s: %s", p.name, e)
        return False
