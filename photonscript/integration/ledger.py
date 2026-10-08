"""PS-33: the machine half of the integrator ledger, written by
`photonscript integrate` into <run>/ledger.json (schema in shared/ledger.py).

pipeline.run() calls write_for_run() as its last stage, so every run, with
or without PixInsight, leaves a ledger beside its manifest. The numbers come
from the pipeline's own data (selection, star QA, calibration plan, the
PixInsight funnel's DROPPED lines, the finish's <name>_final_steps.json);
nothing parses the logs for counts.

Version: 1 + the highest version among the ledgers of the same target and
rig under the staging root (the scheduler may renumber on POST when another
desktop run already took it; report.py writes the stored version back).
An existing ledger.json in the run folder is never overwritten wholesale:
its review block is kept.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from photonscript.shared import ledger as L

REPO = Path(__file__).resolve().parents[2]


def repo_commit(repo: Path = REPO) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - no git on the PATH
        return ""


def same_campaign(a: str, b: str) -> bool:
    from photonscript.integration.select import target_keys
    return bool(target_keys(a) & target_keys(b))


def local_ledgers(staging_root: Path) -> list[tuple[Path, L.Ledger]]:
    """Every readable <staging_root>/*/ledger.json."""
    out = []
    root = Path(staging_root)
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*/ledger.json")):
        try:
            out.append((f, L.load(f)))
        except Exception:  # noqa: BLE001 - hand-edited or half-written file
            continue
    return out


def next_version(staging_root: Path, campaign: str, rig: str, exclude: Path | None = None,
                 kind: str = "integration") -> int:
    vs = [led.version for f, led in local_ledgers(staging_root)
          if led.rig == rig and led.kind == kind and (exclude is None or f.parent != Path(exclude))
          and same_campaign(led.campaign, campaign)]
    return max(vs + [0]) + 1


def calibration_status(bias: int, darks: list[dict], flats: list[dict]) -> str:
    have_d = any(d.get("n") for d in darks)
    have_f = any(f.get("n") for f in flats)
    if bias and have_d and have_f:
        return "calibrated"
    if bias or have_d or have_f:
        return "partial"
    return "uncalibrated"


def finish_steps(out_dir: Path) -> list[dict]:
    """The finish's per-master steps files (out/final/*_final_steps.json)."""
    out = []
    for f in sorted((Path(out_dir) / "final").glob("*_final_steps.json")):
        try:
            out.append({"file": str(f), "steps": json.loads(f.read_text(encoding="utf-8"))})
        except Exception:  # noqa: BLE001
            out.append({"file": str(f), "steps": None})
    return out


def outputs(out_dir: Path) -> list[str]:
    out_dir = Path(out_dir)
    found = []
    for pat in ("master/*.xisf", "master/*.jpg", "final/*_final.*", "final/*_linear.xisf",
                "final/*_starless.xisf"):
        found += [str(p) for p in sorted(out_dir.glob(pat))]
    return found


def build(*, target: str, rig: str, run_dir: Path, options: dict, selected: list,
          staged: list, integrated: list, dropped: set[str], qa: dict, calibration: dict,
          integration: dict | None, finish: dict | None, timing: list, packet: str,
          csv: str, final: str, trigger: dict | None = None,
          version: int = 0) -> L.Ledger:
    """Assemble the ledger from the pipeline's plain data. `selected`,
    `staged`, `integrated` are integration.frames.Frame lists."""
    run_dir = Path(run_dir)
    out_dir = run_dir / "out"
    staged_names = {f.name for f in staged}
    integ_names = {f.name for f in integrated}
    by_filter: dict[str, dict] = {}
    for f in selected:
        b = by_filter.setdefault(f.filter or "?", {"selected": 0, "staged": 0, "integrated": 0,
                                                  "integrated_s": 0.0})
        b["selected"] += 1
        b["staged"] += f.name in staged_names
        if f.name in integ_names:
            b["integrated"] += 1
            b["integrated_s"] += f.exp
    nights = sorted({f.night for f in integrated if f.night})
    hours = sum(f.exp for f in integrated) / 3600.0
    darks = calibration.get("darks") or []
    flats = calibration.get("flats") or []
    subs = [{"file": f.name, "night": f.night, "filter": f.filter, "exp_s": round(f.exp, 3),
             "date_obs": f.date_obs, "used": f.name in integ_names,
             "staged": f.name in staged_names} for f in selected]
    t_start = timing[0][1] if timing else ""
    t_end = timing[-1][2] if timing else ""
    machine = {
        "run_dir": str(run_dir), "repo_commit": repo_commit(),
        "options": options,
        "acquisition": {
            "nights": nights, "first_night": nights[0] if nights else "",
            "last_night": nights[-1] if nights else "",
            "hours": round(hours, 3),
            "library_s": round(sum(f.exp for f in selected), 1),
            "subs_by_filter": {k: {**v, "integrated_s": round(v["integrated_s"], 1)}
                               for k, v in sorted(by_filter.items())},
            "dropped_in_pixinsight": sorted(dropped),
        },
        "subs": subs,
        "calibration": {"status": calibration_status(calibration.get("bias") or 0, darks, flats),
                        **{k: calibration.get(k) for k in ("epoch", "bias", "darks", "flats",
                                                           "notes")}},
        "qa": {k: qa.get(k) for k in ("mode", "kept", "rejected", "reasons", "reference")},
        "integration": {"script": "integrate_run.js (deploy/integrate_stack.js)",
                        "ok": (integration or {}).get("ok"),
                        "minutes": (integration or {}).get("minutes"),
                        "last_line": (integration or {}).get("last_line", ""),
                        "log": str(out_dir / "pipeline.log")},
        "finish": {"script": "finish_run.js (deploy/finish_osc.js)",
                   "ok": (finish or {}).get("ok"),
                   "minutes": (finish or {}).get("minutes"),
                   "steps": finish_steps(out_dir),
                   "log": str(out_dir / "finish.log")},
        "outputs": outputs(out_dir),
        "final": final,
        "astrobin": {"packet": packet, "csv": csv, "uploaded": False},
        "timing": {"started_utc": t_start, "finished_utc": t_end,
                   "minutes": round(sum(float(r[3]) for r in timing), 2) if timing else 0,
                   "stages": timing},
    }
    if trigger:
        machine["trigger"] = trigger
    return L.Ledger(campaign=target, rig=rig, run=run_dir.name, version=version,
                    machine=machine)


def write_for_run(led: L.Ledger, run_dir: Path, staging_root: Path) -> Path:
    """Write <run>/ledger.json. Keeps the review block (and the version) of
    a ledger already there; a new one gets the next local version."""
    path = Path(run_dir) / "ledger.json"
    if path.exists():
        try:
            old = L.load(path)
            led.review = old.review
            led.version = led.version or old.version
            led.created_at = old.created_at
        except Exception:  # noqa: BLE001 - unreadable: replace it
            pass
    if not led.version:
        led.version = next_version(staging_root, led.campaign, led.rig, exclude=run_dir, kind=led.kind)
    return L.save(path, led)
