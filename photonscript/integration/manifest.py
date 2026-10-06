"""PS-22: the run manifest (what was stacked, from where, with what).

manifest.json is the full record (feeds the AstroBin packet and PS-31's
versioned runs); manifest.csv is the flat file list in the M31_OSC4 shape
(group, file, source, bytes) plus role / night / exposure / QA columns.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from photonscript.integration.frames import Frame

MANIFEST_VERSION = 1
CSV_COLS = ["group", "role", "file", "source", "staged", "bytes", "night", "filter", "exp_s",
            "gain", "offset", "temp_c", "readout", "qa_action", "qa_reason", "hfd_px", "stars"]


def frame_entry(f: Frame, role: str, group: str, staged: str = "", qa: dict | None = None) -> dict:
    qa = qa or {}
    return {
        "group": group, "role": role, "file": f.name, "source": str(f.path), "staged": staged,
        "bytes": f.size, "night": f.night, "filter": f.filter, "exp_s": round(f.exp, 3),
        "gain": f.gain, "offset": f.offset, "temp_c": f.temp, "readout": f.readout or "",
        "date_obs": f.date_obs, "target_dir": f.target_dir, "session": f.session,
        "qa_action": qa.get("action", ""), "qa_reason": qa.get("reason", ""),
        "hfd_px": qa.get("hfd_px"), "stars": qa.get("stars"),
    }


def build(run: dict, entries: list[dict], calibration: dict, qa: dict, timing: list | None = None) -> dict:
    """run: target, rig, run_name, run_dir, built, options; entries:
    frame_entry dicts; calibration / qa: summaries (plain data)."""
    lights = [e for e in entries if e["role"] == "light"]
    used = [e for e in lights if e["qa_action"] in ("", "keep")]
    return {
        "version": MANIFEST_VERSION, **run,
        "counts": {"lights_selected": len(lights), "lights_staged": len(used),
                   "lights_rejected_by_qa": len(lights) - len(used),
                   "bias": sum(1 for e in entries if e["role"] == "bias"),
                   "darks": sum(1 for e in entries if e["role"] == "dark"),
                   "flats": sum(1 for e in entries if e["role"] == "flat")},
        "integration_s": round(sum(e["exp_s"] for e in used), 1),
        "calibration": calibration, "qa": qa, "timing": timing or [],
        "frames": entries,
    }


def write(run_dir: Path, manifest: dict) -> tuple[Path, Path]:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    jp = run_dir / "manifest.json"
    jp.write_text(json.dumps(manifest, indent=1, ensure_ascii=True, default=str), encoding="ascii")
    cp = run_dir / "manifest.csv"
    with open(cp, "w", newline="", encoding="ascii", errors="replace") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for e in manifest["frames"]:
            w.writerow(["" if e.get(k) is None else e.get(k) for k in CSV_COLS])
    return jp, cp


def read(run_dir: Path) -> dict:
    return json.loads((Path(run_dir) / "manifest.json").read_text(encoding="ascii"))
