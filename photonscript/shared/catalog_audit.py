"""PS-135: sanity checks for the target catalog (SEASONAL_TARGETS, mostly an
OpenNGC import, plus the user catalog).

audit() returns one finding per suspicious row; scripts/check_catalog.py
prints them and the PS-135 test pins the list, so a new import or hand edit
that adds a suspicious row shows up in the suite. Checks:

* range        RA outside [0, 24) h, Dec outside [-90, 90], size <= 0
* dup_name     two rows with the same name (target_key)
* dup_id       two rows with the same catalog id
* dec_sign     |Dec| < 3 deg (where an import drops the "-" of "-00 26")
               and the row is not in NEAR_EQUATOR_SIGN, or disagrees with it
* size         a Messier row's size off by more than 2x from MESSIER_SIZE
* months       the row's months miss the RA's months_for_ra() window
* faint_large  a galaxy over 20' fainter than mag 10 (a B or surface mag)
* odd_name     a name starting with "the ", a lowercase word "nebula", or an
               abbreviated Bayer star ("omi Vel Cluster")

The reference values are ones checked by hand (SEDS / NED / SIMBAD); they
are not a full catalog, only enough to catch import slips.
"""

from __future__ import annotations

import re

from photonscript.shared.target_names import target_key

# Dec sign of every catalog row within 3 deg of the equator, checked by hand
# (J2000): +1 north, -1 south.
NEAR_EQUATOR_SIGN: dict[str, int] = {
    "B 33": -1,       # Horsehead, -02 27
    "IC 434": -1,     # -02 27
    "NGC 2301": 1,    # +00 27
    "PGC088608": -1,  # Sextans dwarf, -01 37
    "NGC 6741": -1,   # -00 27
    "M 5": 1,         # +02 05
    "M 12": -1,       # -01 57
    "M 2": -1,        # -00 49
    "M 78": 1,        # +00 05
    "M 77": -1,       # -00 01
    "IC 1613": 1,     # +02 07
}

# Messier major-axis sizes in arcmin (SEDS), for the ones known reliably.
MESSIER_SIZE: dict[str, float] = {
    "M 1": 6.0, "M 13": 20.0, "M 27": 8.0, "M 31": 178.0, "M 32": 8.7,
    "M 33": 73.0, "M 42": 85.0, "M 44": 95.0, "M 45": 110.0, "M 51": 11.2,
    "M 57": 1.4, "M 63": 12.6, "M 64": 10.7, "M 74": 10.5, "M 76": 2.7,
    "M 77": 7.1, "M 81": 26.9, "M 82": 11.2, "M 83": 12.9, "M 87": 8.3,
    "M 94": 11.2, "M 97": 3.4, "M 101": 28.8, "M 104": 8.7, "M 106": 18.6,
    "M 110": 21.9,
}

_BAYER = re.compile(r"^(alf|bet|gam|del|eps|zet|eta|tet|iot|kap|lam|mu|nu|"
                    r"xi|omi|pi|rho|sig|tau|ups|phi|chi|psi|ome) [A-Z][a-z]{2} ")


def _finding(e: dict, check: str, detail: str) -> dict:
    return {"catalog_id": e.get("catalog_id", ""), "name": e.get("name", ""),
            "check": check, "detail": detail}


def audit(entries: list[dict] | None = None) -> list[dict]:
    """Findings for the given rows (default: SEASONAL_TARGETS plus the user
    catalog), in catalog order."""
    from photonscript.shared import astronomy
    if entries is None:
        entries = [*astronomy.SEASONAL_TARGETS, *astronomy._USER_TARGETS]
    out: list[dict] = []
    seen_name: dict[str, str] = {}
    seen_id: dict[str, str] = {}
    for e in entries:
        cid = str(e.get("catalog_id") or "")
        name = str(e.get("name") or "")
        ra, dec, size = e.get("ra"), e.get("dec"), e.get("size")
        if ra is None or not 0 <= ra < 24 or dec is None \
                or not -90 <= dec <= 90 or (size is not None and size <= 0):
            out.append(_finding(e, "range", f"ra={ra} dec={dec} size={size}"))
            continue
        nk, ik = target_key(name), target_key(cid)
        if nk in seen_name:
            out.append(_finding(e, "dup_name",
                                f"same name as {seen_name[nk]}"))
        seen_name.setdefault(nk, cid)
        if ik and ik in seen_id:
            out.append(_finding(e, "dup_id", f"same id as {seen_id[ik]!r}"))
        if ik:
            seen_id.setdefault(ik, name)
        if abs(dec) < 3:
            want = NEAR_EQUATOR_SIGN.get(cid)
            if want is None:
                out.append(_finding(e, "dec_sign",
                                    f"dec {dec:+.3f} near the equator, "
                                    "sign not checked"))
            elif (dec >= 0) != (want > 0):
                out.append(_finding(e, "dec_sign",
                                    f"dec {dec:+.3f}, expected "
                                    f"{'+' if want > 0 else '-'}"))
        ref = MESSIER_SIZE.get(cid)
        if ref and size and not 0.5 <= size / ref <= 2.0:
            out.append(_finding(e, "size", f"{size}' vs {ref}' (SEDS)"))
        months = set(e.get("months") or [])
        if months and not months & set(astronomy.months_for_ra(ra)):
            out.append(_finding(e, "months",
                                f"{sorted(months)} vs RA window "
                                f"{astronomy.months_for_ra(ra)}"))
        mag = e.get("mag")
        if "galaxy" in str(e.get("type") or "") and size and size >= 20 \
                and mag is not None and mag > 10:
            out.append(_finding(e, "faint_large",
                                f"mag {mag} for a {size}' galaxy"))
        if name.lower().startswith("the ") or re.search(r" nebula\b", name) \
                or _BAYER.match(name):
            out.append(_finding(e, "odd_name", repr(name)))
    return out


def format_findings(findings: list[dict]) -> str:
    if not findings:
        return "catalog audit: no findings"
    lines = [f"catalog audit: {len(findings)} finding(s)"]
    for f in findings:
        lines.append(f"  {f['check']:<12} {f['catalog_id']:<14} "
                     f"{f['name']}: {f['detail']}")
    return "\n".join(lines)
