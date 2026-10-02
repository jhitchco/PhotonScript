"""PS-96 step 0: is the Piggy-600 elongation real or the measure?

Measures each one-shot-color FITS twice with the live grader's own code:
  raw  = the old path (sep on the raw RGGB mosaic after a 3x3 median)
  sp   = the PS-96 path (2x2 superpixel, median only for big stars)
and prints median HFR (native px), median eccentricity (sqrt form), star
count, and the elongation direction statistics:
  dir_R    resultant length of the star angles (0 = no common direction,
           1 = every star points the same way). Real trailing gives a high
           R at ONE angle, the same in raw and sp.
  dir_deg  that common direction (deg, sep theta folded to 0..180).
  grid%    share of stars within 7.5 deg of 0/45/90/135. Uniform angles
           give ~33%; much more WITH a low dir_R means the pixel grid
           (Bayer / median) is shaping the stars: a measurement artifact.
           (Real trailing along a row or column also scores high grid%,
           but then dir_R is high too.)

Read-only on the FITS. Usage (run from the repo root):
  python scripts/ps96_osc_measure_check.py <file|dir|glob> [...] [--limit N]
         [--after HH:MM --before HH:MM] [--json out.json]
--after / --before filter on the time in NINA's file name (local).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photonscript.telescope_agent.image_validator import (  # noqa: E402
    _detect_stars, _detect_stars_osc, _estimate_background_and_noise)

_NAME_T = re.compile(r"_(\d{2})-(\d{2})-(\d{2})__")


def _files(args) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out += sorted(q for q in p.iterdir()
                          if q.suffix.lower() in (".fits", ".fit", ".fts"))
        elif any(c in a for c in "*?["):
            out += sorted(Path(x) for x in glob.glob(a))
        elif p.exists():
            out.append(p)
    return out


def direction_stats(thetas) -> dict:
    """Axial statistics of sep theta (radians, an axis not a vector)."""
    t = np.asarray([x for x in thetas if x is not None and math.isfinite(x)])
    if len(t) == 0:
        return {"dir_R": None, "dir_deg": None, "grid_pct": None}
    c, s = np.cos(2 * t).mean(), np.sin(2 * t).mean()
    deg = np.degrees(t) % 180.0
    off = np.minimum(deg % 45.0, 45.0 - deg % 45.0)
    return {"dir_R": round(float(math.hypot(c, s)), 3),
            "dir_deg": round(float(math.degrees(math.atan2(s, c)) / 2) % 180, 1),
            "grid_pct": round(float((off <= 7.5).mean() * 100), 1)}


def summarize(stars) -> dict:
    if not stars:
        return {"n": 0, "hfr": None, "ecc": None, "dir_R": None,
                "dir_deg": None, "grid_pct": None}
    return {"n": len(stars),
            "hfr": round(float(np.median([s["hfr"] for s in stars])), 2),
            "ecc": round(float(np.median([s["eccentricity"] for s in stars])), 3),
            **direction_stats([s.get("theta") for s in stars])}


def measure(path: Path) -> dict:
    from astropy.io import fits
    with fits.open(str(path)) as hdul:
        data = hdul[0].data.astype(np.float32)
        hdr = hdul[0].header
    bkg, noise = _estimate_background_and_noise(data[::4, ::4])
    return {"file": path.name, "date_obs": hdr.get("DATE-OBS"),
            "bayer": hdr.get("BAYERPAT"),
            "raw": summarize(_detect_stars(data, bkg, noise)),
            "sp": summarize(_detect_stars_osc(data))}


def _in_window(name: str, after: str | None, before: str | None) -> bool:
    m = _NAME_T.search(name)
    if not m or not (after or before):
        return True
    hm = f"{m.group(1)}:{m.group(2)}"
    return (not after or hm >= after) and (not before or hm <= before)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--after")
    ap.add_argument("--before")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    files = [f for f in _files(a.paths) if _in_window(f.name, a.after, a.before)]
    if a.limit:
        files = files[:a.limit]
    if not files:
        print("no FITS found")
        return 1
    rows = []
    hdr = (f"{'file':44s} {'raw n':>5s} {'hfr':>5s} {'ecc':>5s} {'R':>5s} "
           f"{'dir':>5s} {'grid%':>5s} | {'sp n':>5s} {'hfr':>5s} {'ecc':>5s} "
           f"{'R':>5s} {'dir':>5s} {'grid%':>5s}")
    print(hdr)
    for f in files:
        try:
            r = measure(f)
        except Exception as e:  # noqa: BLE001
            print(f"{f.name}: {type(e).__name__}: {e}")
            continue
        rows.append(r)

        def cols(d):
            return " ".join(f"{'-' if d[k] is None else d[k]:>5}" for k in
                            ("n", "hfr", "ecc", "dir_R", "dir_deg", "grid_pct"))
        print(f"{f.name[:44]:44s} {cols(r['raw'])} | {cols(r['sp'])}")
    if rows:
        def med(k, side):
            v = [r[side][k] for r in rows if r[side][k] is not None]
            return round(float(np.median(v)), 3) if v else None
        summ = {side: {k: med(k, side) for k in
                       ("n", "hfr", "ecc", "dir_R", "dir_deg", "grid_pct")}
                for side in ("raw", "sp")}
        print(f"\nmedian over {len(rows)} subs: raw {summ['raw']}\n"
              f"{'':24s}sp  {summ['sp']}")
        if a.json:
            Path(a.json).write_text(json.dumps({"subs": rows, "median": summ},
                                               indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
