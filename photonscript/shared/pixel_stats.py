"""PS-108 pixel statistics both graders record, plus the PS-5 histogram.

    saturation_level(header, config) -> float   camera max ADU in force
    frame_stats(data, sat_adu, bzero, bscale)   saturated / zero pixel counts,
                                                max ADU, background median+MAD
    histogram(data, ...)                        full-resolution 16-bit counts,
                                                mono or per Bayer channel

All of them walk the frame in row chunks, so a memmapped raw FITS (the
backfill grader's BZERO-scaled int16, do_not_scale_image_data) is never
copied whole, and work the same on the live grader's float32 frame
(bzero 0, bscale 1).
"""

from __future__ import annotations

import numpy as np

ROWS = 256           # rows per chunk (6224 px wide: about 1.6 M px per chunk)
SAMPLE_STEP = 4      # background median / MAD on every 4th row and column


def saturation_level(header, config) -> float:
    """The ADU at and above which a pixel counts as saturated: the FITS
    SATURATE keyword when the camera driver writes one, else
    qa_saturation_adu (65000, the level the PS-21 exposure gates and the
    preview stretch already use; the 16-bit ADC tops out at 65535 and turns
    nonlinear before that)."""
    try:
        v = float((header or {}).get("SATURATE") or 0)
        if v > 0:
            return v
    except (TypeError, ValueError, AttributeError):
        pass
    return float(getattr(config, "qa_saturation_adu", 65000.0) or 65000.0)


def frame_stats(data, sat_adu: float = 65000.0, bzero: float = 0.0,
                bscale: float = 1.0) -> dict:
    """Counts on the full-resolution frame. `data` is raw (apply bzero /
    bscale) or physical ADU (defaults). Returns sat_px, sat_px_pct, zero_px,
    zero_px_pct, max_adu, n_px, bg_median, bg_mad (MAD x 1.4826 = sigma
    equivalent is NOT applied: the raw median absolute deviation)."""
    bscale = float(bscale or 1.0)
    bzero = float(bzero or 0.0)
    h = int(data.shape[0])
    thr_sat = (float(sat_adu) - bzero) / bscale
    thr_zero = (0.0 - bzero) / bscale
    n_sat = n_zero = 0
    mx = None
    for i in range(0, h, ROWS):
        c = np.asarray(data[i:i + ROWS])
        n_sat += int(np.count_nonzero(c >= thr_sat))
        n_zero += int(np.count_nonzero(c <= thr_zero))
        cm = float(c.max()) if c.size else None
        if cm is not None and (mx is None or cm > mx):
            mx = cm
    n = int(data.shape[0]) * int(data.shape[1]) if data.ndim >= 2 else int(data.size)
    sample = np.asarray(data[::SAMPLE_STEP, ::SAMPLE_STEP], dtype=np.float32)
    if bscale != 1.0:
        sample = sample * bscale
    if bzero:
        sample = sample + bzero
    med = float(np.median(sample)) if sample.size else None
    mad = float(np.median(np.abs(sample - med))) if sample.size else None
    return {"sat_px": n_sat,
            "sat_px_pct": round(100.0 * n_sat / n, 4) if n else None,
            "zero_px": n_zero,
            "zero_px_pct": round(100.0 * n_zero / n, 4) if n else None,
            "max_adu": None if mx is None else round(mx * bscale + bzero, 1),
            "sat_adu": float(sat_adu),
            "bg_median": None if med is None else round(med, 1),
            "bg_mad": None if mad is None else round(mad, 2),
            "n_px": n}


_BAYER_SITES = {  # pattern -> {(row parity, col parity): channel}
    "RGGB": {(0, 0): "R", (0, 1): "G", (1, 0): "G", (1, 1): "B"},
    "BGGR": {(0, 0): "B", (0, 1): "G", (1, 0): "G", (1, 1): "R"},
    "GRBG": {(0, 0): "G", (0, 1): "R", (1, 0): "B", (1, 1): "G"},
    "GBRG": {(0, 0): "G", (0, 1): "B", (1, 0): "R", (1, 1): "G"},
}


def _to_u16(c, bzero, bscale):
    """Raw chunk -> physical ADU as uint16 (clipped to 0..65535)."""
    if c.dtype.kind == "i" and c.dtype.itemsize == 2:
        c = c.astype(np.int16, copy=False)      # FITS is big-endian
    if bzero == 32768.0 and bscale == 1.0 and c.dtype == np.int16:
        return (c.view(np.uint16) ^ np.uint16(0x8000))
    x = np.asarray(c, dtype=np.float64)
    if bscale != 1.0:
        x = x * bscale
    if bzero:
        x = x + bzero
    return np.clip(np.rint(x), 0, 65535).astype(np.uint16)


def histogram(data, bzero: float = 0.0, bscale: float = 1.0,
              bayer: str | None = None, bins: int = 256,
              sat_adu: float = 65000.0, offset: float | None = None) -> dict:
    """PS-5: exact 16-bit histogram of the full-resolution frame (chunked
    bincount), reduced to `bins` bins over the populated range (0.1 to 99.9
    percentile, padded). Mono -> channel "L"; with a Bayer pattern the four
    sites are counted separately into "R", "G" (both G sites), "B". Also:
    median per channel, % of pixels at 0 (black clip), % at or above
    sat_adu (white clip) and, when `offset` is given, % at or below the bias
    floor (offset + 6, the PS-71 margin)."""
    bscale = float(bscale or 1.0)
    bzero = float(bzero or 0.0)
    pat = _BAYER_SITES.get(str(bayer or "").strip().upper()) if bayer else None
    names = sorted(set(pat.values()), key="RGB".index) if pat else ["L"]
    full = {n: np.zeros(65536, dtype=np.int64) for n in names}
    h = int(data.shape[0])
    for i in range(0, h, ROWS):
        c = _to_u16(np.asarray(data[i:i + ROWS]), bzero, bscale)
        if pat is None:
            full["L"] += np.bincount(c.ravel(), minlength=65536)
            continue
        for (pr, pc), ch in pat.items():
            r0 = (pr - i) % 2
            full[ch] += np.bincount(c[r0::2, pc::2].ravel(), minlength=65536)
    total = sum(int(v.sum()) for v in full.values())
    allc = sum(full.values())
    cum = np.cumsum(allc)
    lo = int(np.searchsorted(cum, 0.001 * total)) if total else 0
    hi = int(np.searchsorted(cum, 0.999 * total)) if total else 65535
    pad = max(8, (hi - lo) // 10)
    lo, hi = max(0, lo - pad), min(65535, hi + pad)
    edges = np.linspace(lo, hi + 1, bins + 1).astype(np.int64)
    chans = {}
    for n, v in full.items():
        cs = np.concatenate([[0], np.cumsum(v)])
        counts = (cs[edges[1:]] - cs[edges[:-1]]).tolist()
        tot = int(v.sum())
        med = int(np.searchsorted(np.cumsum(v), tot / 2.0)) if tot else None
        chans[n] = {"counts": [int(x) for x in counts], "median": med,
                    "n": tot}
    sat = int(min(65535, max(0, round(sat_adu))))
    out = {"bins": bins, "lo": int(lo), "hi": int(hi),
           "channels": chans, "sat_adu": sat,
           "black_clip_pct": round(100.0 * int(allc[0]) / total, 4) if total else None,
           "white_clip_pct": round(100.0 * int(allc[sat:].sum()) / total, 4)
           if total else None,
           "median": int(np.searchsorted(cum, total / 2.0)) if total else None,
           "bayer": (str(bayer).upper() if pat else None)}
    if offset is not None:
        floor = int(min(65535, max(0, round(float(offset) + 6))))
        out["bias_floor"] = floor
        out["at_bias_floor_pct"] = (round(100.0 * int(allc[:floor + 1].sum())
                                          / total, 3) if total else None)
    return out
