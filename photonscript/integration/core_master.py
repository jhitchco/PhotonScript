"""After the integration: is the target's core still clipped in the master
it will be finished from (2026-10-13, M31)?

Per filter the HDR master (master_<F>_hdr, integration/hdr.py) when it was
made, else the filter's longest-exposure master. The core is the brightest
block median near the frame center (a master has no WCS before the finish
solves it) inside a disk of the target's core radius (shared.core_sat);
"clipped" = pixels within 0.5% of that disk's maximum (a clipped core stays
a flat-topped plateau through calibration and integration, an unclipped
nucleus is peaked). Over qa_core_sat_warn_px (25) of them = still clipped.

    read_xisf(path) -> ndarray | None      (C, H, W) float, memmapped when
                                           the block is not compressed
    check_master(path, r_px, warn_px)      one master
    check_run(out_dir, stacks, made, ...)  every filter of a run; the lines
                                           go to the run log, out/pipeline.log
                                           and the ledger (machine.core_check)

Never raises from check_run: an unreadable master is reported as not
checked.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import numpy as np

from photonscript.shared.core_sat import master_core

_FMT = {"Float32": "f4", "Float64": "f8", "UInt16": "u2", "UInt32": "u4",
        "UInt8": "u1"}
_NORM = {"UInt16": 65535.0, "UInt32": 4294967295.0, "UInt8": 255.0}


def read_xisf(path) -> np.ndarray | None:
    """The first image of a monolithic XISF file as (channels, rows, cols),
    normalized to 0..1 for integer samples. Uncompressed blocks are
    memmapped (a 26 MP Float32 master is never copied whole); zlib blocks
    are inflated; any other codec gives None."""
    p = Path(path)
    with p.open("rb") as fh:
        if fh.read(8) != b"XISF0100":
            return None
        hlen = struct.unpack("<I", fh.read(4))[0]
        fh.read(4)
        xml = fh.read(hlen).decode("utf-8", errors="replace")
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml, count=0)
    # PixInsight writes xsi:schemaLocation; with its xmlns:xsi stripped the
    # prefix is unbound ("unbound prefix" on every real master), so drop
    # prefixed attributes too
    xml = re.sub(r'\s[A-Za-z_][\w.-]*:[\w.-]+="[^"]*"', "", xml)
    root = ET.fromstring(xml)
    img = next((e for e in root.iter() if e.tag.split("}")[-1] == "Image"), None)
    if img is None:
        return None
    geo = [int(x) for x in img.get("geometry", "").split(":") if x]
    if len(geo) < 2:
        return None
    w, h = geo[0], geo[1]
    c = geo[2] if len(geo) > 2 else 1
    fmt = img.get("sampleFormat", "Float32")
    if fmt not in _FMT:
        return None
    loc = img.get("location", "").split(":")
    if len(loc) != 3 or loc[0] != "attachment":
        return None
    pos, size = int(loc[1]), int(loc[2])
    dt = np.dtype(_FMT[fmt]).newbyteorder(">" if img.get("byteOrder") == "big" else "<")
    comp = img.get("compression")
    if comp:
        codec = comp.split(":")[0].split("+")[0]
        if codec != "zlib":
            return None
        with p.open("rb") as fh:
            fh.seek(pos)
            raw = zlib.decompress(fh.read(size))
        if "+sh" in comp.split(":")[0]:       # byte shuffling
            n = len(raw) // dt.itemsize
            raw = np.frombuffer(raw, np.uint8).reshape(dt.itemsize, n).T.tobytes()
        a = np.frombuffer(raw, dtype=dt)
    else:
        a = np.memmap(p, dtype=dt, mode="r", offset=pos, shape=(c * h * w,))
    if img.get("pixelStorage", "Planar") == "Normal":
        a = a.reshape(h, w, c).transpose(2, 0, 1)
    else:
        a = a.reshape(c, h, w)
    if fmt in _NORM:
        a = a.astype(np.float32) / _NORM[fmt]
    return a


def check_master(path, r_px: float, warn_px: int = 25) -> dict:
    """{master, path, clipped_px, peak, still_clipped, x, y, r_px, line}, or
    {master, path, error, line} when it cannot be read."""
    p = Path(path)
    name = p.stem
    try:
        data = read_xisf(p)
        if data is None:
            raise ValueError("unsupported XISF (compression or format)")
        res = master_core(data, r_px)
        if not res:
            raise ValueError("no core region")
    except Exception as e:  # noqa: BLE001
        return {"master": name, "path": str(p), "error": str(e),
                "line": f"core clipped in {name}: not checked ({e})"}
    n = int(res["clipped_px"])
    still = n > int(warn_px)
    return {"master": name, "path": str(p), **res, "still_clipped": still,
            "line": f"core clipped in {name}: {n} px"
            + (" still clipped" if still else "")}


def masters_to_check(out_dir: Path, stacks: list[dict], made) -> list[Path]:
    """Per filter: master_<F>_hdr when made, else the longest-exposure
    master of that filter that exists."""
    from photonscript.integration import hdr
    made = set(made or [])
    by: dict[str, list[dict]] = {}
    for s in stacks:
        by.setdefault(s["filter"], []).append(s)
    out = []
    mdir = Path(out_dir) / "master"
    for filt in sorted(by):
        h = mdir / f"master_{filt}{hdr.SUFFIX}.xisf"
        if f"{filt}{hdr.SUFFIX}" in made and h.is_file():
            out.append(h)
            continue
        for s in sorted(by[filt], key=lambda s: -hdr.stack_exp(s)):
            m = mdir / f"master_{s['name']}.xisf"
            if m.is_file():
                out.append(m)
                break
    return out


def check_run(out_dir: Path, stacks: list[dict], made, *, r_px: float,
              warn_px: int = 25, echo=print) -> list[dict]:
    """Check every filter's master, echo the lines and append them to
    out/pipeline.log. Returns the per-master results (the ledger block)."""
    out_dir = Path(out_dir)
    res = []
    try:
        for m in masters_to_check(out_dir, stacks, made):
            r = check_master(m, r_px, warn_px)
            res.append(r)
            echo("  " + r["line"])
    except Exception as e:  # noqa: BLE001 - never costs a run
        echo(f"  core check skipped: {e}")
    if res:
        try:
            with (out_dir / "pipeline.log").open("a", encoding="ascii",
                                                 errors="replace") as fh:
                for r in res:
                    fh.write(r["line"] + "\n")
        except OSError:
            pass
    return res
