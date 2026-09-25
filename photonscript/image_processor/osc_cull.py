"""Pre-integration cull for piggyback (Piggy-600) OSC subs.

Why this exists (2026-09-25, M31_OSC2): Piggy-600 exposes blindly while the
RC16 drives the mount. When the mount moves mid-exposure (target switch,
mosaic panel, recenter, dither) the 120 s sub records the sky at TWO
pointings. Registration locks onto the dominant copy and sigma rejection
removes the faint second copy of each star, but diffuse light (a galaxy) from
the minority pointing survives and prints a ghost galaxy into the master.
Of 62 M31 subs that night, 30 were split this way.

Detection is reference-free. A sub that saw two pointings contains its star
field twice, offset by the slew vector d, so the autocorrelation of its
high-passed star map has an extra peak at d. Autocorrelation is translation
invariant, so every clean sub of the same field has the SAME sidelobe pattern;
the per-lag 20th percentile across the session is that baseline, and a split
shows up as excess over it (clean M31_OSC2 subs <= 0.026 of the central peak,
splits >= 0.04). Small-minority splits are caught at a lower bar when their
excess sits on a slew vector already confirmed in another sub. Validated
against ground truth (M31 flux measured at both pointings) on the 62
M31_OSC2 subs: 0 misclassified. Needs >= 6 subs, and at least ~20% of them
clean, for the baseline to hold.

Also:
  * duplicate subs (same DATE-OBS and identical bytes, e.g. NINA/Syncthing
    "_1" copies) are culled, keeping the shorter name;
  * sky-background outliers (twilight, moon, cloud glow) are FLAGGED in the
    report but not moved unless --reject-bright is given;
  * reference.txt names the sharpest kept sub, which integrate_osc.js uses as
    the StarAlignment reference.

Nothing is deleted: rejects are moved to <stage>/REJECTED/<reason>/. Every
decision lands in <stage>/cull_report.csv.

Pure numpy + stdlib (astropy optional) so it runs from the desktop with any
Python that has numpy:
    python photonscript/image_processor/osc_cull.py <stage_dir> [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

BIN = 2                 # CFA superpixel (2x2), then a 3x3 median kills hot pixels,
BIN2 = 2                # then 2x2 mean: net 4x binning, Bayer pattern averaged out
HIGHPASS_BOX = 9        # binned px; removes galaxy / gradient structure
CENTER_MASK = 6         # binned px (~24 native px): ignore the central peak + drift
SPLIT_CONFIRM = 0.040   # excess autocorr (fraction of the central peak) that alone
                        # marks a split sub; M31_OSC2: clean subs <= 0.026, splits
                        # down to ~9% minority time >= 0.041
SPLIT_RECUR = 0.022     # lower bar at a lag (+-RECUR_RADIUS) already seen in a
                        # confirmed split sub: catches small-minority splits
RECUR_RADIUS = 8        # binned px
BASELINE_PCTL = 20      # per-lag percentile across subs = the static star-field
                        # sidelobes (autocorrelation is translation invariant)
MIN_SUBS_FOR_SPLIT = 6
BRIGHT_SIGMA = 8.0      # background outlier: > median + 8 * 1.4826 * MAD


# --------------------------------------------------------------------------- FITS I/O

def _parse_header(raw: bytes) -> tuple[dict[str, str], int]:
    cards: dict[str, str] = {}
    pos = 0
    while pos + 2880 <= len(raw):
        block = raw[pos:pos + 2880].decode("ascii", "replace")
        pos += 2880
        for i in range(0, 2880, 80):
            card = block[i:i + 80]
            key = card[:8].strip()
            if key == "END":
                return cards, pos
            if card[8:10] == "= " and key not in cards:
                val = card[10:].split("/")[0].strip() if "'" not in card[10:] else \
                    card[10:].split("'")[1].strip()
                cards[key] = val
    raise ValueError("no END card in FITS header")


def read_fits(path: Path) -> tuple[dict[str, str], np.ndarray]:
    """Primary HDU header (str values) + 2-D float32 data."""
    try:  # astropy if present (handles every FITS corner case)
        from astropy.io import fits  # type: ignore
        with fits.open(path, memmap=False) as hdul:
            hdr = {k: str(v) for k, v in hdul[0].header.items()}
            return hdr, np.asarray(hdul[0].data, dtype=np.float32)
    except ImportError:
        pass
    raw = Path(path).read_bytes()
    hdr, off = _parse_header(raw)
    bitpix = int(hdr["BITPIX"])
    w, h = int(hdr["NAXIS1"]), int(hdr["NAXIS2"])
    dtype = {8: ">u1", 16: ">i2", 32: ">i4", -32: ">f4", -64: ">f8"}[bitpix]
    n = w * h * abs(bitpix) // 8
    a = np.frombuffer(raw[off:off + n], dtype=dtype).reshape(h, w).astype(np.float32)
    bscale = float(hdr.get("BSCALE", 1) or 1)
    bzero = float(hdr.get("BZERO", 0) or 0)
    return hdr, a * bscale + bzero


# --------------------------------------------------------------------------- metrics

def superpixel(a: np.ndarray, k: int = BIN) -> np.ndarray:
    h, w = (a.shape[0] // k) * k, (a.shape[1] // k) * k
    return a[:h, :w].reshape(h // k, k, w // k, k).mean(axis=(1, 3))


def median3(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    h, w = a.shape
    return np.median(np.stack([p[i:i + h, j:j + w] for i in range(3) for j in range(3)]), axis=0)


def bin_frame(a: np.ndarray) -> np.ndarray:
    """CFA frame -> hot-pixel-free, 4x-binned luminance."""
    return superpixel(median3(superpixel(a, BIN)), BIN2)


def box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """(2r+1) box mean via cumulative sums, edge-padded."""
    p = np.pad(a, r, mode="edge")
    c = np.cumsum(np.cumsum(p, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    k = 2 * r + 1
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / (k * k)


def star_image(a: np.ndarray) -> np.ndarray:
    """High-passed, clipped star map from a binned frame."""
    hp = a - box_blur(a, HIGHPASS_BOX)
    med = np.median(hp)
    mad = np.median(np.abs(hp - med)) * 1.4826 + 1e-6
    hp = np.clip(hp - med, 0, None)
    hp[hp < 3 * mad] = 0                       # keep only real sources
    top = np.percentile(hp[hp > 0], 99.5) if np.any(hp > 0) else 1.0
    return np.minimum(hp, top)                 # no single saturated star dominates


def _tukey(n: int, alpha: float = 0.2) -> np.ndarray:
    w = np.ones(n)
    k = max(1, int(alpha * n / 2))
    r = 0.5 * (1 - np.cos(np.pi * np.arange(k) / k))
    w[:k] = r
    w[-k:] = r[::-1]
    return w


def autocorr(binned: np.ndarray) -> np.ndarray:
    """Centered autocorrelation of the star map, normalized to the zero lag."""
    s = star_image(binned)
    s = s - s.mean()
    win = _tukey(s.shape[0])[:, None] * _tukey(s.shape[1])[None, :]
    f = np.fft.rfft2(s * win)
    ac = np.fft.fftshift(np.fft.irfft2(f * np.conj(f), s=s.shape))
    p0 = ac[s.shape[0] // 2, s.shape[1] // 2]
    return (ac / p0 if p0 > 0 else ac * 0).astype(np.float32)


def softness(ac: np.ndarray) -> float:
    """Mean autocorr at 1 binned px: larger = fatter stars. Lower is sharper."""
    cy, cx = ac.shape[0] // 2, ac.shape[1] // 2
    return float((ac[cy, cx + 1] + ac[cy, cx - 1] + ac[cy + 1, cx] + ac[cy - 1, cx]) / 4)


@dataclass
class SplitResult:
    excess: float     # autocorr excess over the session baseline at the worst lag
    dx: int           # that lag in native px (the slew vector for a split sub)
    dy: int
    softness: float
    recur: float = 0.0  # excess near a lag confirmed split in another sub


def split_scan(acs: np.ndarray) -> list[SplitResult]:
    """Two-pass split detection over a session's autocorrelations.

    Pass 1: excess = ac - per-lag percentile across subs (removes the star
    field's own sidelobes, identical in every clean sub). A sub whose worst
    excess >= SPLIT_CONFIRM is a confirmed split and votes its lag.
    Pass 2: any other sub with excess >= SPLIT_RECUR within RECUR_RADIUS of a
    voted lag is also split (small-minority splits along the same slew).
    """
    n, h, w = acs.shape
    cy, cx = h // 2, w // 2
    base = np.percentile(acs, BASELINE_PCTL, axis=0) if n >= MIN_SUBS_FOR_SPLIT \
        else np.zeros((h, w), np.float32)
    res: list[SplitResult] = []
    lags: list[tuple[int, int]] = []
    for ac in acs:
        e = ac - base
        e[cy - CENTER_MASK:cy + CENTER_MASK + 1, cx - CENTER_MASK:cx + CENTER_MASK + 1] = -np.inf
        iy, ix = np.unravel_index(np.argmax(e), e.shape)
        r = SplitResult(float(e[iy, ix]), int(ix - cx) * BIN * BIN2,
                        int(iy - cy) * BIN * BIN2, softness(ac))
        res.append(r)
        if r.excess >= SPLIT_CONFIRM:
            lags.append((int(iy), int(ix)))
    for ac, r in zip(acs, res):
        if r.excess >= SPLIT_CONFIRM or not lags:
            continue
        e = ac - base
        r.recur = max(float(e[max(0, y - RECUR_RADIUS):y + RECUR_RADIUS + 1,
                              max(0, x - RECUR_RADIUS):x + RECUR_RADIUS + 1].max())
                      for y, x in lags)
    return res


def is_split(r: SplitResult) -> bool:
    return r.excess >= SPLIT_CONFIRM or r.recur >= SPLIT_RECUR


# --------------------------------------------------------------------------- cull

@dataclass
class Sub:
    path: Path
    date_obs: str = ""
    background: float = 0.0
    split: SplitResult | None = None
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def find_lights(stage: Path) -> Path:
    for d in (stage / "LIGHTS" / "OSC", stage / "LIGHTS", stage):
        if d.is_dir() and any(d.glob("*.fit*")):
            return d
    raise FileNotFoundError(f"no OSC lights under {stage}")


def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def analyze(files: list[Path], log=print) -> list[Sub]:
    subs: list[Sub] = []
    acs = []
    for i, p in enumerate(files, 1):
        hdr, a = read_fits(p)
        b = bin_frame(a)
        subs.append(Sub(p, hdr.get("DATE-OBS", ""), float(np.median(b))))
        acs.append(autocorr(b))
        if i % 10 == 0 or i == len(files):
            log(f"  measured {i}/{len(files)}")
    if len(subs) >= MIN_SUBS_FOR_SPLIT:
        for s, r in zip(subs, split_scan(np.stack(acs))):
            s.split = r
            if is_split(r):
                s.reasons.append("split_pointing")
    else:
        log(f"  only {len(subs)} subs: split-pointing check needs >= {MIN_SUBS_FOR_SPLIT}, skipped")
        for s, ac in zip(subs, acs):
            s.split = SplitResult(0.0, 0, 0, softness(ac))
    for s in subs:
        r = s.split
        log(f"  {s.path.name}: excess={r.excess:.3f} recur={r.recur:.3f} "
            f"lag=({r.dx},{r.dy})px bg={s.background:.1f}"
            f"{'  SPLIT' if 'split_pointing' in s.reasons else ''}")

    # duplicates: same DATE-OBS and identical bytes -> keep the shortest name
    by_time: dict[str, list[Sub]] = {}
    for s in subs:
        if s.date_obs:
            by_time.setdefault(s.date_obs, []).append(s)
    for group in by_time.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda s: (len(s.path.name), s.path.name))
        keep_hash = _sha1(group[0].path)
        for s in group[1:]:
            if _sha1(s.path) == keep_hash:
                s.reasons.append("duplicate")

    # background outliers (twilight / moon / cloud glow): flag only
    bgs = np.array([s.background for s in subs])
    if len(bgs) >= 5:
        med = float(np.median(bgs))
        mad = float(np.median(np.abs(bgs - med))) * 1.4826 + 1e-6
        for s in subs:
            if s.background > med + BRIGHT_SIGMA * mad:
                s.flags.append(f"bright_sky(+{(s.background - med) / mad:.0f} sigma)")
    return subs


def cull(stage: Path, dry_run: bool = False, reject_bright: bool = False, log=print) -> list[Sub]:
    stage = Path(stage)
    lights_dir = find_lights(stage)
    files = sorted(list(lights_dir.glob("*.fits")) + list(lights_dir.glob("*.fit")))
    log(f"osc_cull: {len(files)} subs in {lights_dir}")
    subs = analyze(files, log)
    if reject_bright:
        for s in subs:
            if any(f.startswith("bright_sky") for f in s.flags):
                s.reasons.append("bright_sky")

    kept = [s for s in subs if not s.reasons]
    if kept:
        ref = min(kept, key=lambda s: s.split.softness)
        (stage / "reference.txt").write_text(ref.path.name + "\n")
        log(f"reference (sharpest kept sub): {ref.path.name}")

    with open(stage / "cull_report.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "date_obs", "action", "reasons", "flags", "split_excess",
                    "split_recur", "split_dx", "split_dy", "softness", "background"])
        for s in subs:
            w.writerow([s.path.name, s.date_obs, "reject" if s.reasons else "keep",
                        ";".join(s.reasons), ";".join(s.flags), f"{s.split.excess:.4f}", f"{s.split.recur:.4f}",
                        s.split.dx, s.split.dy, f"{s.split.softness:.4f}",
                        f"{s.background:.2f}"])

    for s in subs:
        if not s.reasons:
            continue
        dest = stage / "REJECTED" / s.reasons[0]
        if dry_run:
            log(f"  would move {s.path.name} -> REJECTED/{s.reasons[0]}/")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / s.path.name
        if target.exists():
            log(f"  SKIP move (exists): {target}")
            continue
        shutil.move(str(s.path), str(target))
        log(f"  moved {s.path.name} -> REJECTED/{s.reasons[0]}/")

    n_rej = sum(1 for s in subs if s.reasons)
    by = {}
    for s in subs:
        for r in s.reasons[:1]:
            by[r] = by.get(r, 0) + 1
    flagged = sum(1 for s in subs if s.flags and not s.reasons)
    log(f"osc_cull: kept {len(subs) - n_rej}/{len(subs)}; rejected {by or 0}; "
        f"flagged-but-kept {flagged}{' (DRY RUN, nothing moved)' if dry_run else ''}")
    return subs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("stage", type=Path, help="staging dir (the integrate_osc.js STAGING)")
    ap.add_argument("--dry-run", action="store_true", help="report only, move nothing")
    ap.add_argument("--reject-bright", action="store_true",
                    help="also move sky-background outliers (default: flag only)")
    a = ap.parse_args(argv)
    cull(a.stage, a.dry_run, a.reject_bright)
    return 0


if __name__ == "__main__":
    sys.exit(main())
