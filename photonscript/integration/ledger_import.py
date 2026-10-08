"""PS-142: one-time import of the ledger history from before PS-33
(`photonscript ledger-import`).

Two sources:

  * a hand-written ledger file (schema 0.1, e.g.
    Staging\\M31_OSC3\\ledger.json, or a folder holding one): loaded with
    shared/ledger.load, which upgrades it to 0.2 without losing anything;
  * a hand-run staging folder WITHOUT a ledger (D:\\Astrophotography\\
    Staging\\M31_OSC4): a ledger is synthesized from what the folder holds
    for one processing variant (out\\<variant>\\weights.csv = the integrated
    subs, manifest.csv = what was staged, <tag>_*selection.csv = the star
    QA, out\\pipeline_<tag>.log / finish_<tag>.log = the PixInsight results,
    astrobin\\ = the packet and CSV).

Both are READ-ONLY on the source folder: nothing is written there (not even
`reported`), unlike report.post_ledger. The ledger is marked
machine.imported = {by, at, source}, so the scheduler sends no "new version"
ping for it and the morning note skips it. Posting happens only with
--apply (POST /api/integrations, idempotent per run); the default is a dry
run that prints what would be posted.

    load_any(path, variant=, campaign=, version=) -> (Ledger, note)
    synthesize_run(run_dir, ...)                    -> Ledger
    post(led, base_url, post=)                      -> {ok, status, detail}
"""

from __future__ import annotations

import csv
import hashlib
import re
import statistics
from pathlib import Path

from photonscript.integration import report
from photonscript.integration.ledger import calibration_status
from photonscript.shared import ledger as L

NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")
EXP_RE = re.compile(r"_(\d+(?:\.\d+)?)s_")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
ASTROBIN_RE = re.compile(r"https://(?:app\.)?astrobin\.com/(?:i/)?\w+")
_SUFFIXES = ("_c", "_cc", "_d", "_r", "_ln")


class LedgerImportError(ValueError):
    """The path holds nothing importable."""


def _imported(source: Path, synthesized: bool) -> dict:
    return {"by": "photonscript ledger-import (PS-142)", "at": L.now_iso(),
            "source": str(source), "synthesized": synthesized}


def sub_name(stem: str) -> str:
    """'..._0193_c_cc_d_r' (a PixInsight intermediate) -> '..._0193.fits'."""
    s = Path(stem).stem if stem.lower().endswith((".xisf", ".fits", ".fit")) else stem
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if s.endswith(suf):
                s = s[: -len(suf)]
                changed = True
    return s + ".fits"


def night_of(name: str) -> str:
    """Evening date of a NINA file name (local time; before noon = the
    previous evening)."""
    from datetime import date, timedelta
    m = NAME_RE.search(name)
    if not m:
        return ""
    d = date.fromisoformat(m.group(1))
    if int(m.group(2)) < 12:
        d -= timedelta(days=1)
    return d.isoformat()


def exp_of(name: str) -> float:
    m = EXP_RE.search(name)
    return float(m.group(1)) if m else 0.0


def _rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _log_tail(path: Path) -> dict:
    """ok (EXIT OK / ERROR), last timestamp, the funnel and minutes lines."""
    if not path.is_file():
        return {"ok": None, "log": ""}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: dict = {"ok": None, "log": str(path), "last_line": lines[-1].strip() if lines else ""}
    if any("EXIT OK" in ln for ln in lines):
        out["ok"] = True
    elif any("ERROR" in ln for ln in lines[-5:]):
        out["ok"] = False
    for ln in lines:
        if "funnel" in ln:
            out["funnel"] = ln.split("funnel", 1)[1].strip()
        m = re.search(r"done in ([\d.]+) min", ln)
        if m:
            out["minutes"] = float(m.group(1))
    for ln in reversed(lines):
        m = TS_RE.match(ln.strip())
        if m:
            out["last_utc"] = m.group(1) + "Z"
            break
    return out


def variants(run_dir: Path) -> list[Path]:
    """out\\<variant> folders with a weights.csv, newest first."""
    out = [p.parent for p in Path(run_dir).glob("out/*/weights.csv")]
    return sorted(out, key=lambda p: (p / "weights.csv").stat().st_mtime, reverse=True)


def _pick_variant(run_dir: Path, variant: str | None) -> Path:
    vs = variants(run_dir)
    if not vs:
        raise LedgerImportError(f"{run_dir}: no ledger.json and no out\\<variant>\\weights.csv")
    if not variant:
        return vs[0]
    v = variant.lower()
    hit = [p for p in vs if p.name.lower() == v] or \
          [p for p in vs if p.name.lower().split("_")[0] == v]
    if not hit:
        raise LedgerImportError(f"no variant {variant!r} (have {', '.join(p.name for p in vs)})")
    return hit[0]


def _newest(paths) -> str:
    paths = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    return str(paths[0]) if paths else ""


def synthesize_run(run_dir: Path, *, variant: str | None = None, campaign: str = "",
                   version: int = 0, rig: str = "piggyback") -> L.Ledger:
    """A 0.2 ledger for one processing variant of a hand-run staging folder
    (see module doc). Reads only."""
    run_dir = Path(run_dir)
    vdir = _pick_variant(run_dir, variant)
    tag = vdir.name.split("_")[0]                       # v4b_noflat -> v4b
    out_dir = run_dir / "out"
    weights = _rows(vdir / "weights.csv")
    used = {sub_name(r.get("file") or ""): r for r in weights if r.get("file")}
    manifest = _rows(run_dir / "manifest.csv") if (run_dir / "manifest.csv").is_file() else []
    groups: dict[str, list[dict]] = {}
    for r in manifest:
        groups.setdefault(str(r.get("group") or ""), []).append(r)
    light_names: dict[str, dict] = {}
    for g, rows in groups.items():
        if g.upper().startswith("LIGHTS"):
            for r in rows:
                light_names.setdefault(r.get("file") or "", r)
    for n in used:                                  # integrated but not in the manifest
        light_names.setdefault(n, {"file": n, "group": "", "source": ""})
    subs = []
    for n in sorted(light_names):
        subs.append({"file": n, "night": night_of(n), "filter": "OSC",
                     "exp_s": exp_of(n), "used": n in used, "staged": True,
                     "source": light_names[n].get("source", "")})
    integ = [s for s in subs if s["used"]]
    nights = sorted({s["night"] for s in integ if s["night"]})
    hours = sum(s["exp_s"] for s in integ) / 3600.0
    by_exp: dict[str, dict] = {}
    for s in subs:
        b = by_exp.setdefault(f"{s['exp_s']:g}s", {"selected": 0, "integrated": 0,
                                                     "integrated_s": 0.0})
        b["selected"] += 1
        if s["used"]:
            b["integrated"] += 1
            b["integrated_s"] += s["exp_s"]
    # star QA: <tag>_*selection.csv, column <tag>_action / <tag>_reason
    qa: dict = {"mode": "hand-run star QA"}
    sel = sorted(run_dir.glob(f"{tag}_*selection.csv"))
    if sel:
        rows = _rows(sel[0])
        act, why = f"{tag}_action", f"{tag}_reason"
        reasons: dict[str, int] = {}
        kept = rej = 0
        for r in rows:
            if (r.get(act) or "").strip() == "reject":
                rej += 1
                for part in str(r.get(why) or "").split(";"):
                    k = part.split("(")[0].strip()
                    if k:
                        reasons[k] = reasons.get(k, 0) + 1
            elif (r.get(act) or "").strip() == "keep":
                kept += 1
        qa.update(file=str(sel[0]), kept=kept, rejected=rej, reasons=reasons)
    ref = run_dir / "reference.txt"
    if ref.is_file():
        qa["reference"] = ref.read_text(encoding="utf-8", errors="replace").strip()
    # calibration from the manifest groups; a "noflat" variant used no flat
    bias = len(groups.get("BIAS", []))
    darks = [{"exposure_s": exp_of("_" + g.split("_", 1)[1] + "_") or None, "n": len(r)}
             for g, r in groups.items() if g.upper().startswith("DARK") and "_" in g]
    flats = [] if "noflat" in vdir.name.lower() else \
        [{"group": g, "n": len(r)} for g, r in groups.items() if g.upper().startswith("FLAT")]
    pipe = _log_tail(out_dir / f"pipeline_{tag}.log")
    fin = _log_tail(out_dir / f"finish_{tag}.log")
    ab_dir = run_dir / "astrobin"
    packet = _newest(ab_dir.glob("*_astrobin_packet*.md")) if ab_dir.is_dir() else ""
    csvp = _newest(ab_dir.glob("*_astrobin_acquisition*.csv")) if ab_dir.is_dir() else ""
    final = _newest(ab_dir.glob(f"*{tag}*crop.jpg")) if ab_dir.is_dir() else ""
    final = final or _newest((vdir / "final").glob("*_final.jpg"))
    revision_of = ""
    if packet:
        m = ASTROBIN_RE.search(Path(packet).read_text(encoding="utf-8", errors="replace"))
        revision_of = m.group(0) if m else ""
    wg: dict[str, list[float]] = {}
    for n, r in used.items():
        try:
            wg.setdefault(f"{exp_of(n):g}s", []).append(float(r.get("wG") or 0))
        except ValueError:
            pass
    outputs = [str(p) for pat in ("master/*.xisf", "master/*.jpg", "final/*")
               for p in sorted(vdir.glob(pat))]
    machine = {
        "run_dir": str(run_dir), "variant": vdir.name,
        "imported": _imported(run_dir, True),
        "acquisition": {"nights": nights, "first_night": nights[0] if nights else "",
                        "last_night": nights[-1] if nights else "",
                        "hours": round(hours, 3),
                        "library_s": round(sum(s["exp_s"] for s in subs), 1),
                        "subs_by_filter": {"OSC": {
                            "selected": len(subs), "staged": len(subs),
                            "integrated": len(integ),
                            "integrated_s": round(hours * 3600, 1)}},
                        "subs_by_exposure": by_exp},
        "subs": subs,
        "calibration": {"status": calibration_status(bias, darks, flats), "bias": bias,
                        "darks": darks, "flats": flats,
                        "notes": ("variant without flats" if "noflat" in vdir.name.lower()
                                  else "")},
        "qa": qa,
        "weights": {"file": str(vdir / "weights.csv"), "rows": len(weights),
                    "median_wG_by_exposure": {k: round(statistics.median(v), 4)
                                              for k, v in sorted(wg.items()) if v}},
        "integration": {"script": f"hand-run ({tag})", "ok": pipe.get("ok"),
                        "minutes": pipe.get("minutes"), "funnel": pipe.get("funnel", ""),
                        "last_line": pipe.get("last_line", ""), "log": pipe.get("log", "")},
        "finish": {"script": f"hand-run ({tag})", "ok": fin.get("ok"),
                   "log": fin.get("log", "")},
        "outputs": outputs,
        "final": final,
        "astrobin": {"packet": packet, "csv": csvp, "uploaded": False},
    }
    timing = out_dir / f"timing_{tag}.csv"
    if timing.is_file():
        machine["timing"] = {"file": str(timing)}
    if not version:
        digits = "".join(c for c in tag if c.isdigit())
        version = int(digits) if digits else 0
    publish = {"astrobin": {"status": "packet_ready", "revision_of": revision_of}} \
        if revision_of else {}
    created = fin.get("last_utc") or pipe.get("last_utc") or L.now_iso()
    return L.Ledger(campaign=campaign or run_dir.name.split("_")[0], rig=rig,
                    run=f"{run_dir.name}_{tag}", version=version, created_at=created,
                    machine=machine, publish=publish)


def fill_hand_written(led: L.Ledger, folder: Path) -> list[str]:
    """A hand-written ledger keeps its own field names (0.1: hours per
    filter, a master path instead of an ok flag, the packet under publish).
    Fill the 0.2 reads the summaries use, only where missing, and say which
    were filled (machine.imported.filled)."""
    m = led.machine
    filled = []
    acq = m.setdefault("acquisition", {})
    if not acq.get("hours"):
        sbf = acq.get("subs_by_filter") or {}
        h = sum(float(v.get("hours") or 0) for v in sbf.values() if isinstance(v, dict))
        if not h:
            h = sum(float(v.get("integrated") or 0) * float(v.get("exposure_s") or 0)
                    for v in sbf.values() if isinstance(v, dict)) / 3600.0
        if h:
            acq["hours"] = round(h, 3)
            filled.append("acquisition.hours")
    integ = m.get("integration")
    if isinstance(integ, dict) and integ.get("ok") is None and integ.get("master"):
        integ["ok"] = True
        filled.append("integration.ok (master recorded)")
    fin = m.get("finish")
    if isinstance(fin, dict) and fin.get("ok") is None and fin.get("outputs"):
        fin["ok"] = True
        filled.append("finish.ok (outputs recorded)")
    ab = m.setdefault("astrobin", {})
    pub = (led.publish or {}).get("astrobin") or {}
    if not ab.get("packet") and pub.get("packet"):
        pk = Path(str(pub["packet"]))
        ab["packet"] = str(pk if pk.is_absolute() else Path(folder) / pk)
        filled.append("astrobin.packet")
    if not m.get("run_dir"):
        m["run_dir"] = str(folder)
        filled.append("run_dir")
    return filled


def load_any(path: Path, *, variant: str | None = None, campaign: str = "",
             version: int = 0) -> tuple[L.Ledger, str]:
    """A ledger file, a folder with ledger.json, or a staging folder to
    synthesize from. Returns (ledger, how)."""
    path = Path(path)
    if not path.exists():
        raise LedgerImportError(f"{path} does not exist")
    f = path if path.is_file() else path / "ledger.json"
    if f.is_file() and not variant:
        import json
        raw = json.loads(f.read_text(encoding="utf-8"))
        led = L.parse(raw)
        led.machine["imported"] = _imported(f, False)
        led.machine["imported"]["filled"] = fill_hand_written(led, f.parent)
        # stable ask ids, so a repeated import keeps Jeremy's decisions (the
        # scheduler matches decided asks by id on a re-post)
        raw_asks = (raw.get("review") or {}).get("asks") or []
        for i, ask in enumerate(led.review.asks):
            had = raw_asks[i].get("id") if i < len(raw_asks) and isinstance(raw_asks[i], dict)                 else None
            if not had:
                ask.id = hashlib.sha1(f"{led.run}|{i}|{ask.type}".encode()).hexdigest()[:10]
        if campaign:
            led.campaign = campaign
        if version:
            led.version = version
        led.reported, led.reported_at = False, ""
        was = "0.1, upgraded to 0.2" if raw.get("schema") == L.SCHEMA_V01 else "0.2"
        return led, f"loaded {f} ({was})"
    if path.is_file():
        raise LedgerImportError(f"{path} is a file but not a readable ledger")
    led = synthesize_run(path, variant=variant, campaign=campaign, version=version)
    return led, f"synthesized from {path} variant {led.machine['variant']}"


def summary_lines(led: L.Ledger) -> list[str]:
    m = led.machine
    a = m.get("acquisition") or {}
    subs = m.get("subs") or []
    asks = led.review.asks
    lines = [f"campaign {led.campaign}  rig {led.rig}  run {led.run}  version v{led.version}",
             f"created {led.created_at}  headline: {L.headline(led)}",
             f"hours {led.hours:.2f}  nights {', '.join(a.get('nights') or []) or '-'}"
             + (f"  subs used {sum(1 for s in subs if s.get('used'))} of {len(subs)}"
                if subs else ""),
             f"calibration {(m.get('calibration') or {}).get('status', '-')}  "
             f"integration ok {(m.get('integration') or {}).get('ok')}  "
             f"finish ok {(m.get('finish') or {}).get('ok')}",
             f"packet {(m.get('astrobin') or {}).get('packet') or '-'}"]
    url = ((led.publish or {}).get("astrobin") or {}).get("url") or ""
    rev = ((led.publish or {}).get("astrobin") or {}).get("revision_of") or ""
    if url:
        lines.append(f"AstroBin {url} (campaign status: Published)")
    elif rev:
        lines.append(f"AstroBin revision of {rev} (not uploaded)")
    if led.review.verdict or asks:
        lines.append(f"review: verdict {led.review.verdict or '-'}, {len(asks)} asks "
                     f"({', '.join(a.type for a in asks)})")
    return lines


def post(led: L.Ledger, base_url: str, *, post=report.http_post_json,
         timeout: float = report.DEFAULT_TIMEOUT_S) -> dict:
    """POST the ledger; never writes anything back to the source folder."""
    if not base_url:
        return {"ok": False, "status": 0, "detail": "no scheduler URL (integration_report_url)"}
    url = base_url.rstrip("/") + "/api/integrations"
    try:
        status, body = post(url, led.dump(), timeout)
    except (OSError, ValueError) as e:
        return {"ok": False, "status": 0, "detail": f"scheduler unreachable ({e})"}
    if status != 200 or not body.get("ok"):
        return {"ok": False, "status": status, "detail": str(body.get("detail") or f"HTTP {status}")}
    return {"ok": True, "status": status, "detail": body.get("headline", ""),
            "version": body.get("version"), "project_id": body.get("project_id")}
