"""Progression contact sheets: one self-contained HTML page per target that
shows every rendition in the order it was made, rebuilt after every
integrate / finish / blend run on the desktop.

Store (desktop; <integration_staging_root parent>/progression):

    progression/
      pending_publish.json      targets whose HTML changed since the last
                                publish (Claude republishes them to the
                                artifact URL in their meta.json, then runs
                                `progression-sheet --published`)
      <slug>/
        meta.json               title, sub, facts, filters, notes, aliases,
                                artifact_url, published_sha256
        versions.json           one entry per frame (id, file, title,
                                date_made, rig, lineage, data {subs, hours,
                                lengths, nights}, calibration, processing,
                                changed, source, run, cal, settings)
        <id>_<run>.jpg          the frame thumbnails (1000 px wide)
        <slug>_progression.html the built sheet (thumbnails embedded)

add_run() makes an entry from a run folder (ledger.json, else manifest.json,
plus its final JPG: *_vivid.jpg first, else the mono color image,
*_blend.jpg, *_final.jpg) and derives the "changed" note by diffing it
against the previous entry of the same lineage (more data, new calibration,
new processing settings, a blend vs a single rig). Notes edited by hand
(edit_entry) are never overwritten by a later re-add of the same run.

after_run() is the hook the integrate / finish / blend entry points call:
guarded, it never raises, so a sheet can never fail an integration.
`PS_PROGRESSION_SHEETS=off` turns it off (the test suite does).

The page design is the darkroom contact sheet of the hand-built sheets
(2026-10): light and dark tokens, lineage filters wide / core / blend, a
dialog for the large view.
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

DIR_NAME = "progression"
PENDING = "pending_publish.json"
THUMB_PX = 1000

LINEAGES = {"Piggy wide": "wide", "RC16 core": "core", "Blend": "blend"}
RIG_OF_LINEAGE = {"Piggy wide": "Piggy-600 OSC", "RC16 core": "RC16 mono", "Blend": "Piggy-600 + RC16"}
SHORT_RIG = {"Piggy wide": "Piggy", "RC16 core": "RC16"}
DEFAULT_FILTERS = [["wide", "Piggy wide field"], ["core", "RC16 core"], ["blend", "Two-rig blends"]]
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]

# The sheets Claude published by hand before this module (target page links)
KNOWN_TARGETS = {
    "m31": {"title": "M31 Progression", "aliases": ["M31", "Andromeda Galaxy"],
            "artifact_url": "https://claude.ai/artifact/P5vqZKUQW2eQxyRhhaNRcq"},
    "heart": {"title": "Heart Nebula Progression", "aliases": ["Heart Nebula", "IC 1805"],
              "artifact_url": "https://claude.ai/artifact/JBqqooj2QLSh45Ys6HJQbe"},
    "m33": {"title": "M33 / NGC 604 Progression", "aliases": ["M33", "Triangulum Galaxy", "NGC 604"],
            "artifact_url": "https://claude.ai/artifact/3YtZccfhp2spWjz7HrQ2rc"},
}

# finish step tools -> the short names the sheets use
TOOL_SHORT = {"GradientCorrection": "GradientCorrection", "AutomaticBackgroundExtractor": "ABE",
              "SPCC": "SPCC", "ColorCalibration": "ColorCalibration", "BlurXTerminator": "BlurX",
              "NoiseXTerminator": "NoiseX", "StarXTerminator": "StarX",
              "HDRMultiscaleTransform": "core HDR"}
STEP_SHORT = {"stretch": "stretch", "vivid": "vivid", "palette": "palette", "hoo": "HOO"}


class ProgressionError(Exception):
    pass


# ----------------------------------------------------------------- paths

def store_root(staging_root) -> Path:
    """<integration_staging_root parent>/progression (I:/Astrophotography/progression
    for I:/Astrophotography/Staging)."""
    return Path(staging_root).parent / DIR_NAME


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return s or "target"


def _keys(name: str) -> set[str]:
    try:
        from photonscript.integration.select import target_keys
        return set(target_keys(name))
    except Exception:  # noqa: BLE001 - catalog not importable: plain key
        return {re.sub(r"[^a-z0-9]", "", str(name).lower())} - {""}


def target_dirs(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "meta.json").is_file())


def resolve_slug(root: Path, target: str) -> str:
    """The store of `target`: a store whose slug, title or aliases share a
    catalog key with it (NGC 604 -> m33), else a known sheet, else
    slugify(target)."""
    want = _keys(target) | {slugify(target)}
    for d in target_dirs(root):
        try:
            meta = _read_json(d / "meta.json")
        except Exception:  # noqa: BLE001
            continue
        names = [d.name] + list(meta.get("aliases") or [])
        if any(_keys(n) & want for n in names) or d.name in want:
            return d.name
    for slug, k in KNOWN_TARGETS.items():
        if any(_keys(n) & want for n in k["aliases"]):
            return slug
    return slugify(target)


# ----------------------------------------------------------------- io

def _read_json(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _write_json(p: Path, obj) -> Path:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    text = json.dumps(obj, indent=1, ensure_ascii=False)
    tmp.write_text(text, encoding="utf-8")
    for i in range(20):
        try:
            os.replace(tmp, p)
            return p
        except PermissionError:
            # Windows: a virus scanner or the indexer holds the file just written
            time.sleep(0.05 * (i + 1))
    p.write_text(text, encoding="utf-8")
    try:
        tmp.unlink()
    except OSError:
        pass
    return p


def default_meta(slug: str, target: str) -> dict:
    k = KNOWN_TARGETS.get(slug, {})
    return {
        "title": k.get("title") or f"{target} Progression",
        "sub": f"Every rendition of {target}, in the order it was made. The orange line on each "
               "frame is what changed. Click any frame to see it larger.",
        "facts": [["Data", "auto"], ["Site", "AARO, Bortle 2"]],
        "filters": [list(f) for f in DEFAULT_FILTERS],
        "notes": ["Sub counts are what PixInsight integrated (ledger counts).",
                  "The bar under each frame scales to the longest stack on the sheet."],
        "aliases": list(k.get("aliases") or [target]),
        "artifact_url": k.get("artifact_url", ""),
        "published_sha256": "",
    }


class Store:
    def __init__(self, root: Path, slug: str):
        self.root = Path(root)
        self.slug = slug
        self.dir = self.root / slug
        self.meta: dict = {}
        self.versions: list[dict] = []

    @classmethod
    def open(cls, root: Path, slug: str, target: str = "", create: bool = False) -> "Store":
        s = cls(root, slug)
        mp, vp = s.dir / "meta.json", s.dir / "versions.json"
        if mp.is_file():
            s.meta = _read_json(mp)
        elif create:
            s.meta = default_meta(slug, target or slug)
        else:
            raise ProgressionError(f"no progression store {s.dir} (meta.json missing)")
        s.versions = _read_json(vp) if vp.is_file() else []
        return s

    def save(self) -> None:
        _write_json(self.dir / "meta.json", self.meta)
        _write_json(self.dir / "versions.json", self.versions)

    @property
    def html_path(self) -> Path:
        return self.dir / f"{self.slug}_progression.html"

    def next_id(self) -> str:
        n = max([int(e["id"]) for e in self.versions if str(e.get("id", "")).isdigit()] + [0]) + 1
        return f"{n:02d}"

    def entry(self, ident: str) -> dict:
        for e in self.versions:
            if str(e.get("id")) == str(ident):
                return e
        raise ProgressionError(f"{self.slug}: no frame {ident}")


# ----------------------------------------------------------------- run -> entry

def _ledger(run_dir: Path) -> dict:
    for name in ("ledger.json", "manifest.json"):
        p = Path(run_dir) / name
        if p.is_file():
            try:
                d = _read_json(p)
            except Exception:  # noqa: BLE001
                continue
            if name == "manifest.json":
                d = {"campaign": d.get("target", ""), "rig": d.get("rig", "piggyback"),
                     "kind": d.get("kind", "integration"), "run": d.get("run_name", Path(run_dir).name),
                     "created_at": "", "machine": d}
            return d
    raise ProgressionError(f"{run_dir}: no ledger.json or manifest.json")


def final_jpg(run_dir: Path) -> tuple[Path | None, Path | None]:
    """(main image, core inset) of a run: *_vivid.jpg first, then a mono
    run's color image (LRGB / RGB / SHO / HOO), *_blend.jpg, *_haRGB.jpg,
    *_L_final.jpg, *_final.jpg; the inset is a blend's *_core.jpg."""
    fin = Path(run_dir) / "out" / "final"
    if not fin.is_dir():
        return None, None
    names = sorted(p.name for p in fin.iterdir() if p.is_file() and p.suffix.lower() == ".jpg")

    def first(pred):
        for n in names:
            if pred(n):
                return fin / n
        return None

    color = None
    for kind in ("LRGB", "RGB", "SHO", "HOO"):
        color = color or first(lambda n, k=kind: n.endswith(f"_{k}.jpg"))
    main = (first(lambda n: n.endswith("_blend_vivid.jpg")) or first(lambda n: n.endswith("_vivid.jpg"))
            or color or first(lambda n: n.endswith("_blend.jpg")) or first(lambda n: n.endswith("_haRGB.jpg"))
            or first(lambda n: n.endswith("_L_final.jpg")) or first(lambda n: n.endswith("_final.jpg"))
            or first(lambda n: "_blend" in n))
    core = first(lambda n: n.endswith("_core.jpg")) if main is not None else None
    return main, core


def _fmt_s(x: float) -> str:
    return f"{x:g} s"


def acquisition(machine: dict) -> dict:
    """subs, hours, nights, lengths and the per-length counts of an
    integration ledger's used subs."""
    subs = [s for s in machine.get("subs") or [] if s.get("used")]
    acq = machine.get("acquisition") or {}
    if not subs:
        n = sum(int(v.get("integrated") or 0) for v in (acq.get("subs_by_filter") or {}).values())
        return {"subs": n or None, "hours": round(float(acq["hours"]), 2) if acq.get("hours") else None,
                "nights": list(acq.get("nights") or []), "lengths": "unknown", "by_exp": {}, "by_filter": {}}
    by_exp: dict[float, int] = {}
    by_ef: dict[float, dict[str, int]] = {}
    for s in subs:
        e = float(s.get("exp_s") or 0)
        by_exp[e] = by_exp.get(e, 0) + 1
        f = s.get("filter") or "?"
        by_ef.setdefault(e, {})[f] = by_ef.setdefault(e, {}).get(f, 0) + 1
    mono = any(f not in ("OSC", "?") for d in by_ef.values() for f in d)
    order = {"L": 0, "R": 1, "G": 2, "B": 3, "Ha": 4, "OIII": 5, "SII": 6}
    parts = []
    for e in sorted(by_exp):
        if mono:
            fl = ", ".join(f"{f} {n}" for f, n in sorted(by_ef[e].items(), key=lambda kv: (order.get(kv[0], 9), kv[0])))
            parts.append(f"{_fmt_s(e)} ({fl})")
        elif len(by_exp) > 1:
            parts.append(f"{_fmt_s(e)} ({by_exp[e]})")
        else:
            parts.append(_fmt_s(e))
    hours = sum(float(s.get("exp_s") or 0) for s in subs) / 3600.0
    return {"subs": len(subs), "hours": round(hours, 2), "nights": sorted({s["night"] for s in subs if s.get("night")}),
            "lengths": ", ".join(parts), "by_exp": {f"{k:g}": v for k, v in sorted(by_exp.items())},
            "by_filter": {f: sum(d.get(f, 0) for d in by_ef.values()) for f in sorted({f for d in by_ef.values() for f in d})}}


def calibration(machine: dict) -> tuple[str, dict]:
    """(sheet text, structured {bias, darks: {light_s: dark_s|scaled}, flats: [filters]})."""
    c = machine.get("calibration") or {}
    bias = int(c.get("bias") or 0)
    darks = [d for d in c.get("darks") or [] if d.get("n")]
    flats = [f for f in c.get("flats") or [] if f.get("n")]
    matched = sorted({float(d["light_exp"]) for d in darks if not d.get("scaled")})
    scaled = sorted({(float(d["light_exp"]), float(d.get("dark_exp") or 0)) for d in darks if d.get("scaled")})
    parts = []
    head = []
    if bias:
        head.append(f"bias {bias}")
    if matched:
        head.append("matched " + "/".join(f"{x:g}" for x in matched) + " s darks")
    if head:
        parts.append(" + ".join(head))
    for le, de in scaled:
        parts.append(f"{le:g} s lights use scaled {de:g} s darks")
    if flats:
        parts.append("flats " + ", ".join(f"{f.get('filter') or '?'} {f['n']}" for f in flats))
    else:
        parts.append("no flats")
    if not bias and not darks and not flats:
        parts = ["none (uncalibrated: no bias, darks or flats)"]
    struct = {"bias": bool(bias), "darks": {f"{d['light_exp']:g}": ("scaled" if d.get("scaled") else "matched")
                                            for d in darks},
              "flats": sorted({f.get("filter") or "?" for f in flats})}
    return "; ".join(parts), struct


def _steps_of(machine: dict) -> list[dict]:
    out = []
    for fs in (machine.get("finish") or {}).get("steps") or []:
        st = (fs or {}).get("steps") or {}
        out += ((st.get("finish") or {}).get("steps")) or []
    return out


def processing(machine: dict, kind: str) -> tuple[str, dict]:
    """(sheet text, the settings dict a later entry is diffed against)."""
    if kind == "blend":
        b = machine.get("blend") or {}
        opts = machine.get("options") or {}
        lum = machine.get("luminance_from") or []
        txt = (f"{'narrowband' if machine.get('mode') == 'nb' else 'luminance'} blend: RC16 "
               f"{'+'.join(lum) if lum else 'L'} into the Piggy color"
               + (f", weight {opts['weight']:g}" if isinstance(opts.get("weight"), (int, float)) else "")
               + (", vivid" if (machine.get("vivid") or {}).get("ok") else ""))
        sett = {k: v for k, v in opts.items() if isinstance(v, (int, float, str, bool)) or v is None}
        sett["mode"] = machine.get("mode") or b.get("mode")
        return txt, sett
    names: list[str] = []
    sett: dict = {}
    for s in _steps_of(machine):
        if s.get("status") != "ran":
            continue
        tool, step = s.get("tool", ""), s.get("step", "")
        short = TOOL_SHORT.get(tool) or STEP_SHORT.get(step)
        if short and short not in names:
            names.append(short)
        for k, v in (s.get("settings") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and step in (
                    "deconvolution", "noise_reduction", "vivid", "saturation", "core_hdr", "star_reduction",
                    "scnr", "palette"):
                sett.setdefault(f"{step}.{k}", v)
    opts = machine.get("options") or {}
    for k in ("qa", "palette", "vivid", "gradient", "hoo"):
        if k in opts and not isinstance(opts[k], (dict, list)):
            sett[k] = opts[k]
    rc = opts.get("rcastro")
    if isinstance(rc, dict):
        for k, v in rc.items():
            sett[f"rcastro.{k}"] = v
    if machine.get("color"):
        names.append("mono color combine")
    if opts.get("qa") == "apply":
        names.insert(0, "star QA applied")
    return (", ".join(names) if names else "unknown"), sett


def _run_ledger(ref: str, run_dir: Path) -> dict | None:
    """The ledger of an input run named by a finish / blend ledger. Runs
    move between disks (D: to I:), so a missing path is also looked up by
    its folder name beside this run and under its staging root."""
    if not ref:
        return None
    p = Path(ref)
    if p.name == "ledger.json":
        p = p.parent
    run_dir = Path(run_dir)
    for c in (p, run_dir.parent / p.name, run_dir.parent.parent / p.name):
        try:
            return _read_json(c / "ledger.json")
        except Exception:  # noqa: BLE001
            continue
    return None


def _source_ledger(machine: dict, run_dir: Path) -> dict | None:
    """A finish's input integrate run ledger."""
    ref = (machine.get("input") or {}).get("run") or (machine.get("input") or {}).get("mono_run") \
        or machine.get("source_run") or ""
    return _run_ledger(ref, run_dir)


def entry_from_run(run_dir: Path) -> dict:
    """A versions.json entry (without id / file / title / changed) from a run folder."""
    run_dir = Path(run_dir)
    led = _ledger(run_dir)
    m = led.get("machine") or {}
    kind = led.get("kind") or m.get("kind") or "integration"
    rig = led.get("rig") or "piggyback"
    lineage = "Blend" if kind == "blend" else ("RC16 core" if rig == "rc16" else "Piggy wide")
    acq_src = m
    if kind == "finish":
        src = _source_ledger(m, run_dir)
        acq_src = (src or {}).get("machine") or {}
        if "calibration" not in m:
            m = {**m, "calibration": acq_src.get("calibration") or {}}
    if kind == "blend":
        runs: dict[str, dict] = {}
        for i in m.get("inputs") or []:
            if i.get("run") and i["run"] not in runs:
                runs[i["run"]] = _run_ledger(i["run"], run_dir) or {
                    "rig": "rc16" if i.get("role") == "rc16" else "piggyback"}
        subs, hours, nights, lengths = 0, 0.0, set(), []
        cal_parts = []
        for r, ld in runs.items():
            a = acquisition((ld or {}).get("machine") or {})
            subs += a["subs"] or 0
            hours += a["hours"] or 0
            nights |= set(a["nights"])
            who = "RC16" if (ld or {}).get("rig") == "rc16" else "Piggy"
            if a["by_exp"]:
                lengths.append(f"{who} " + "/".join(a["by_exp"]) + " s")
            if (ld or {}).get("machine"):
                cal_parts.append(f"{who}: {calibration(ld['machine'])[0]}")
        acq = {"subs": subs or None, "hours": round(hours, 2) if hours else None, "nights": sorted(nights),
               "lengths": ", ".join(lengths) or "unknown"}
        cal_txt, cal = ("; ".join(cal_parts) or "as the input runs"), {"inputs": sorted(Path(r).name for r in runs)}
    else:
        a = acquisition(acq_src)
        acq = {k: a[k] for k in ("subs", "hours", "nights", "lengths")}
        cal_txt, cal = calibration(m)
    proc_txt, sett = processing(m, kind)
    made = str(led.get("created_at") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
    return {"date_made": made, "rig": RIG_OF_LINEAGE[lineage], "lineage": lineage, "kind": kind,
            "data": acq, "calibration": cal_txt, "processing": proc_txt, "source": str(run_dir),
            "run": run_dir.name, "campaign": led.get("campaign") or "", "cal": cal, "settings": sett}


def auto_title(store: Store, e: dict, existing: dict | None = None) -> str:
    if existing and existing.get("title_edited"):
        return existing["title"]
    before = store.versions if existing is None else store.versions[:store.versions.index(existing)]
    n = len([v for v in before if v.get("lineage") == e["lineage"]]) + 1
    if e["kind"] == "blend":
        return f"Blend {n}"
    who = SHORT_RIG[e["lineage"]]
    what = "finish" if e["kind"] == "finish" else "stack"
    word = ORDINALS[n - 1] if n <= len(ORDINALS) else f"#{n}"
    return f"{who} {word} {what}"


def changed_note(prev_same: dict | None, prev_any: dict | None, e: dict, target: str = "") -> str:
    """What changed from the previous entry of the same lineage (else the
    previous entry at all): more data (x factor), new nights, calibration
    (flats added, darks now matched), new processing settings, blend vs
    single rig."""
    if prev_any is None:
        return f"first {target + ' ' if target else ''}image"
    if prev_same is None:
        if e["lineage"] == "Blend":
            return "first two-rig blend: RC16 detail with Piggy color"
        if e["lineage"] == "RC16 core":
            return "first RC16 data"
        return "first Piggy wide-field stack"
    out: list[str] = []
    if e["lineage"] == "Blend" and prev_any.get("lineage") != "Blend":
        out.append(f"two-rig blend again (after a {SHORT_RIG.get(prev_any.get('lineage'), 'single')} rig frame)")
    d, p = e.get("data") or {}, prev_same.get("data") or {}
    h, ph = d.get("hours"), p.get("hours")
    if isinstance(h, (int, float)) and isinstance(ph, (int, float)) and ph > 0:
        r = h / ph
        if r >= 1.15:
            out.append(f"{r:.1f}x the data ({ph:g} h to {h:g} h)")
        elif r <= 0.87:
            out.append(f"less data ({ph:g} h to {h:g} h)")
    new_n = sorted(set(d.get("nights") or []) - set(p.get("nights") or []))
    if new_n:
        out.append("new nights " + ", ".join(n[5:] for n in new_n))
    c, pc = e.get("cal") or {}, prev_same.get("cal") or {}
    if "flats" in c and "flats" in pc:
        if c["flats"] and not pc["flats"]:
            out.append("flats added")
        elif pc["flats"] and not c["flats"]:
            out.append("no flats this time")
        if c.get("bias") and not pc.get("bias"):
            out.append("bias added")
        now_matched = [k for k, v in (c.get("darks") or {}).items() if v == "matched"
                       and (pc.get("darks") or {}).get(k) == "scaled"]
        if now_matched:
            out.append("matched " + "/".join(now_matched) + " s darks replace scaled ones")
    s, ps = e.get("settings") or {}, prev_same.get("settings") or {}
    if s and ps:
        diffs = [f"{k} {ps.get(k)} to {s.get(k)}" for k in sorted(set(s) | set(ps))
                 if s.get(k) != ps.get(k) and k in s and k in ps]
        added = [k for k in sorted(set(s) - set(ps))]
        if diffs or added:
            txt = "new processing settings: " + ", ".join(diffs[:4] + [f"{k} added" for k in added[:2]])
            if len(diffs) > 4:
                txt += f" (+{len(diffs) - 4} more)"
            out.append(txt)
    if e.get("kind") == "finish" and prev_same.get("kind") != "finish":
        out.append("re-finish of the stack")
    return "; ".join(out) or "same data and settings, run again"


def _thumb(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > THUMB_PX:
                im = im.resize((THUMB_PX, max(1, round(im.height * THUMB_PX / im.width))), Image.LANCZOS)
            im.save(dest, "JPEG", quality=85, optimize=True)
    except ImportError:
        shutil.copyfile(src, dest)
    return dest


def add_run(root: Path, run_dir: Path, target: str = "", changed: str = "", title: str = "") -> tuple[Store, dict]:
    """Add (or refresh) the entry of `run_dir` in its target's store. A run
    already on the sheet keeps its id and any hand-edited title / note."""
    run_dir = Path(run_dir)
    e = entry_from_run(run_dir)
    main, core = final_jpg(run_dir)
    if main is None:
        raise ProgressionError(f"{run_dir}: no final JPG (out/final/*_vivid.jpg, *_final.jpg, *_blend*.jpg)")
    tname = target or e["campaign"] or run_dir.name
    slug = resolve_slug(root, tname)
    store = Store.open(root, slug, tname, create=True)
    existing = next((v for v in store.versions if v.get("run") == e["run"]), None)
    ident = existing["id"] if existing else store.next_id()
    base = f"{ident}_{slugify(e['run'])}"
    e["id"] = ident
    e["file"] = _thumb(main, store.dir / f"{base}.jpg").name
    if core is not None:
        e["core_file"] = _thumb(core, store.dir / f"{base}_core.jpg").name
    upto = store.versions if existing is None else store.versions[:store.versions.index(existing)]
    prev_same = next((v for v in reversed(upto) if v.get("lineage") == e["lineage"]), None)
    prev_any = upto[-1] if upto else None
    e["title"] = title or auto_title(store, e, existing)
    e["title_edited"] = bool(title) or bool(existing and existing.get("title_edited"))
    if changed:
        e["changed"], e["changed_edited"] = changed, True
    elif existing and existing.get("changed_edited"):
        e["changed"], e["changed_edited"] = existing["changed"], True
    else:
        e["changed"] = changed_note(prev_same, prev_any, e, store.meta.get("title", "").replace(" Progression", ""))
        e["changed_edited"] = False
    if existing is not None:
        store.versions[store.versions.index(existing)] = e
    else:
        store.versions.append(e)
        # a hand-written Data fact described the frames before this one
        store.meta["facts"] = [[k, "auto" if k == "Data" else v] for k, v in store.meta.get("facts") or []]
    store.save()
    return store, e


def edit_entry(store: Store, ident: str, changed: str | None = None, title: str | None = None) -> dict:
    e = store.entry(ident)
    if changed is not None:
        e["changed"], e["changed_edited"] = changed, True
    if title is not None:
        e["title"], e["title_edited"] = title, True
    store.save()
    return e


# ----------------------------------------------------------------- build

def _clean(s) -> str:
    """No em dashes on the sheets."""
    return str(s).replace("\u2014", ", ").replace(" ,", ",")


def esc(s) -> str:
    return _html.escape(_clean(s if s is not None else "unknown"))


def _b64(p: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(Path(p).read_bytes()).decode()


def data_fact(versions: list[dict]) -> str:
    """'1.3 h to 23.4 h Piggy, 2.2 h RC16': the hours range per rig lineage."""
    parts = []
    for lin, who in SHORT_RIG.items():
        hs = [float(e["data"]["hours"]) for e in versions if e.get("lineage") == lin
              and isinstance((e.get("data") or {}).get("hours"), (int, float))]
        if hs:
            lo, hi = min(hs), max(hs)
            parts.append(f"{hi:g} h {who}" if lo == hi else f"{lo:g} h to {hi:g} h {who}")
    return ", ".join(parts) or "unknown"


CSS = """
/* Layout: a darkroom contact sheet. Frames run left to right in the order they were made,
   each with a film-edge number, the "what changed" note in grease-pencil orange, and the data underneath. */
:root{
  --bg:#eceae6; --sheet:#f8f7f4; --ink:#1d1f24; --muted:#5d606a; --line:#d4d1ca;
  --film:#22252b; --film-hole:#eceae6; --pencil:#c2521b; --wide:#3b6ea8; --core:#7a4fa3; --blend:#2f8a6b;
  --display:"Saira Condensed","Arial Narrow",sans-serif; --body:"IBM Plex Sans",system-ui,sans-serif; --mono:"IBM Plex Mono",ui-monospace,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0f1115; --sheet:#171a20; --ink:#e7e5e0; --muted:#9a9ca5; --line:#2b2f37;
  --film:#05060a; --film-hole:#171a20; --pencil:#f08a4b; --wide:#79a8e0; --core:#b48be0; --blend:#5fc7a1; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f1115; --sheet:#171a20; --ink:#e7e5e0; --muted:#9a9ca5; --line:#2b2f37;
  --film:#05060a; --film-hole:#171a20; --pencil:#f08a4b; --wide:#79a8e0; --core:#b48be0; --blend:#5fc7a1; color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:var(--body);font-size:14px;line-height:1.5}
.wrap{max-width:1280px;margin:0 auto;padding-inline:16px;padding-block:28px 48px}
header{display:grid;gap:10px;margin-bottom:22px}
h1{font-family:var(--display);font-weight:700;font-size:clamp(34px,5vw,54px);letter-spacing:.02em;line-height:1;margin:0;text-wrap:balance}
.sub{color:var(--muted);max-width:68ch;margin:0}
.facts{display:flex;flex-wrap:wrap;gap:8px 22px;font-family:var(--mono);font-size:12.5px;color:var(--muted)}
.facts b{color:var(--ink);font-weight:500}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 18px}
.filters button{font:500 12.5px var(--mono);letter-spacing:.04em;text-transform:uppercase;padding:6px 12px;border-radius:999px;
  border:1px solid var(--line);background:var(--sheet);color:var(--ink);cursor:pointer}
.filters button[aria-pressed="true"]{background:var(--ink);color:var(--sheet);border-color:var(--ink)}
.filters button:focus-visible,.open:focus-visible,.inset:focus-visible{outline:2px solid var(--pencil);outline-offset:2px}
.sheet{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),1fr));gap:18px}
.frame{background:var(--sheet);border:1px solid var(--line);border-radius:4px;overflow:hidden;display:grid;grid-template-rows:auto auto 1fr;min-width:0}
.sprockets{height:14px;background:var(--film);
  background-image:radial-gradient(circle at 50% 50%,var(--film-hole) 0 3px,transparent 3.5px);background-size:22px 14px}
.shot{position:relative;background:var(--film);padding:0 8px 8px}
.open{display:block;width:100%;padding:0;border:0;background:#000;cursor:zoom-in;aspect-ratio:3/2;overflow:hidden}
.open img{width:100%;height:100%;object-fit:cover;display:block}
.num{position:absolute;left:14px;bottom:14px;font:700 15px var(--display);letter-spacing:.08em;color:#f4c46a;text-shadow:0 1px 2px #000}
.inset{position:absolute;right:14px;bottom:14px;width:30%;max-width:130px;aspect-ratio:3/2;padding:0;border:1px solid #f4c46a;background:#000;cursor:zoom-in;overflow:hidden}
.inset img{width:100%;height:100%;object-fit:cover;display:block}
.inset span{position:absolute;left:4px;top:2px;font:500 10px var(--mono);color:#f4c46a;text-transform:uppercase;letter-spacing:.06em}
.meta{padding:12px 14px 14px;display:grid;gap:8px;align-content:start;min-width:0}
.row1{display:flex;justify-content:space-between;align-items:center;gap:8px}
time{font:400 12px var(--mono);color:var(--muted);font-variant-numeric:tabular-nums}
.chip{font:500 11px var(--mono);text-transform:uppercase;letter-spacing:.06em;padding:2px 8px;border-radius:999px;border:1px solid currentColor}
.chip.wide{color:var(--wide)} .chip.core{color:var(--core)} .chip.blend{color:var(--blend)}
h2{font:600 16px/1.3 var(--body);margin:0;text-wrap:balance}
.changed{margin:0;color:var(--pencil);font-weight:500;display:grid;gap:2px}
.changed span{font:500 10.5px var(--mono);text-transform:uppercase;letter-spacing:.08em;opacity:.8}
.data{display:flex;flex-wrap:wrap;gap:4px 14px;font-family:var(--mono);font-size:12.5px;font-variant-numeric:tabular-nums;color:var(--muted)}
.data b{color:var(--ink);font-weight:500}
.hbar{height:4px;background:var(--line);border-radius:2px;overflow:hidden}
.hbar i{display:block;height:100%;background:var(--muted)}
dl{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;margin:2px 0 0;font-size:12.5px}
dt{font:500 10.5px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding-top:2px}
dd{margin:0;color:var(--ink);min-width:0;overflow-wrap:anywhere}
.notes{margin-top:26px;color:var(--muted);font-size:12.5px;max-width:80ch;display:grid;gap:4px}
.notes h3{font:600 13px var(--body);color:var(--ink);margin:0}
.notes ul{margin:0;padding-left:18px}
dialog{border:0;padding:0;background:transparent;max-width:min(96vw,1400px)}
dialog::backdrop{background:rgba(0,0,0,.85)}
dialog img{max-width:100%;max-height:86vh;display:block;margin:0 auto}
dialog p{color:#eee;font:500 13px var(--mono);text-align:center;margin:8px 0 0}
dialog button{position:absolute;top:8px;right:8px;font:600 13px var(--mono);background:#000;color:#fff;border:1px solid #666;border-radius:4px;padding:4px 10px;cursor:pointer}
@media (prefers-reduced-motion:no-preference){.open img{transition:transform .25s}.open:hover img{transform:scale(1.03)}}
"""

SCRIPT = """
(function(){
  var lb=document.getElementById('lb'),img=document.getElementById('lbi'),cap=document.getElementById('lbc');
  document.querySelectorAll('[data-full]').forEach(function(b){b.addEventListener('click',function(){
    img.src=b.querySelector('img').src;img.alt=b.getAttribute('data-cap');cap.textContent=b.getAttribute('data-cap');
    if(lb.showModal){lb.showModal();}});});
  document.getElementById('lbx').addEventListener('click',function(){lb.close();});
  lb.addEventListener('click',function(e){if(e.target===lb)lb.close();});
  var btns=document.querySelectorAll('.filters button');
  btns.forEach(function(b){b.addEventListener('click',function(){
    var f=b.getAttribute('data-f');
    btns.forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false');});
    document.querySelectorAll('.frame').forEach(function(c){c.hidden=!(f==='all'||c.getAttribute('data-lin')===f);});
  });});
})();
"""


def _card(store: Store, e: dict, max_h: float, subject: str) -> str:
    d = e.get("data") or {}
    hrs = d.get("hours")
    hrs_txt = f"{hrs:g} h" if isinstance(hrs, (int, float)) else "unknown"
    bar = (float(hrs) / max_h * 100) if isinstance(hrs, (int, float)) and max_h else 0
    nights = d.get("nights") or []
    nights_txt = ", ".join(esc(n[5:]) for n in nights) if isinstance(nights, list) else esc(nights)
    lin = LINEAGES.get(e.get("lineage"), "wide")
    core = ""
    if e.get("core_file") and (store.dir / e["core_file"]).is_file():
        core = (f'<button class="inset" type="button" data-full="1" '
                f'data-cap="{esc(e["title"])}: core crop" aria-label="Open core crop">'
                f'<img src="{_b64(store.dir / e["core_file"])}" alt="Core crop of {esc(e["title"])}" loading="lazy">'
                f'<span>core</span></button>')
    return f"""
<article class="frame" data-lin="{lin}">
  <div class="sprockets" aria-hidden="true"></div>
  <div class="shot">
    <button class="open" type="button" data-full="1" data-cap="{esc(e['id'])} {esc(e['title'])}" aria-label="Open {esc(e['title'])} larger">
      <img src="{_b64(store.dir / e['file'])}" alt="{esc(subject)} rendition {esc(e['id'])}: {esc(e['title'])}" loading="lazy">
    </button>
    {core}
    <span class="num">{esc(e['id'])}</span>
  </div>
  <div class="meta">
    <div class="row1"><span class="chip {lin}">{esc(e.get('lineage'))}</span><time>{esc(e.get('date_made'))}</time></div>
    <h2>{esc(e['title'])}</h2>
    <p class="changed"><span>Changed</span>{esc(e.get('changed'))}</p>
    <div class="data">
      <span><b>{esc(d.get('subs'))}</b> subs</span>
      <span><b>{hrs_txt}</b></span>
      <span>{esc(d.get('lengths'))}</span>
    </div>
    <div class="hbar" aria-hidden="true"><i style="width:{bar:.1f}%"></i></div>
    <dl>
      <dt>Nights</dt><dd>{nights_txt or 'unknown'}</dd>
      <dt>Calibration</dt><dd>{esc(e.get('calibration'))}</dd>
      <dt>Processing</dt><dd>{esc(e.get('processing'))}</dd>
    </dl>
  </div>
</article>"""


def render(store: Store) -> str:
    M, V = store.meta, store.versions
    hs = [float(e["data"]["hours"]) for e in V if isinstance((e.get("data") or {}).get("hours"), (int, float))]
    max_h = max(hs) if hs else 0.0
    subject = (M.get("aliases") or [M.get("title", "").replace(" Progression", "")])[0]
    cards = "\n".join(_card(store, e, max_h, subject) for e in V)
    facts_l = [["Frames", len(V)]] + [[k, data_fact(V) if k == "Data" and v == "auto" else v]
                                      for k, v in M.get("facts") or []]
    facts = "\n".join(f"    <span>{esc(k)} <b>{esc(v)}</b></span>" for k, v in facts_l)
    filters = "\n".join(f'  <button type="button" data-f="{esc(k)}" aria-pressed="{str(k == "all").lower()}">{esc(lbl)}</button>'
                        for k, lbl in [["all", "All"]] + list(M.get("filters") or DEFAULT_FILTERS))
    notes = "\n".join(f"    <li>{esc(n)}</li>" for n in M.get("notes") or [])
    title = esc(M.get("title") or f"{store.slug} Progression")
    return f"""<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@500;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header>
  <h1>{title}</h1>
  <p class="sub">{esc(M.get('sub', ''))}</p>
  <div class="facts">
{facts}
  </div>
</header>
<div class="filters" role="group" aria-label="Filter by lineage">
{filters}
</div>
<main class="sheet">
{cards}
</main>
<section class="notes">
  <h3>Notes on the record</h3>
  <ul>
{notes}
  </ul>
</section>
</div>
<dialog id="lb"><button type="button" id="lbx">Close</button><img id="lbi" alt=""><p id="lbc"></p></dialog>
<script>{SCRIPT}</script>
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(store: Store) -> tuple[Path, bool]:
    """Write <slug>_progression.html; (path, changed since the last publish).
    Updates pending_publish.json."""
    text = render(store)
    p = store.html_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    h = sha(text)
    store.meta["built_sha256"] = h
    store.save()
    changed = h != (store.meta.get("published_sha256") or "")
    update_pending(store.root, store, changed)
    return p, changed


# ----------------------------------------------------------------- pending marker

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_pending(root: Path) -> dict:
    p = Path(root) / PENDING
    try:
        d = _read_json(p)
        if isinstance(d.get("targets"), dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"targets": {}}


def update_pending(root: Path, store: Store, changed: bool) -> dict:
    d = read_pending(root)
    if changed:
        d["targets"][store.slug] = {
            "title": store.meta.get("title", ""), "artifact_url": store.meta.get("artifact_url", ""),
            "html": str(store.html_path), "frames": len(store.versions),
            "sha256": store.meta.get("built_sha256", ""), "changed_at": _now()}
    else:
        d["targets"].pop(store.slug, None)
    d["updated"] = _now()
    d["note"] = ("Claude republishes each html to its artifact_url (same URL), then runs "
                 "`photonscript progression-sheet --target <slug> --published`.")
    _write_json(Path(root) / PENDING, d)
    return d


def mark_published(root: Path, slug: str) -> Store:
    s = Store.open(root, slug)
    s.meta["published_sha256"] = s.meta.get("built_sha256") or sha(s.html_path.read_text(encoding="utf-8"))
    s.meta["published_at"] = _now()
    s.save()
    update_pending(root, s, False)
    return s


def pending_list(root: Path) -> list[dict]:
    return [{"slug": k, **v} for k, v in sorted(read_pending(root)["targets"].items())]


# ----------------------------------------------------------------- hook

def enabled() -> bool:
    return os.environ.get("PS_PROGRESSION_SHEETS", "on").strip().lower() not in ("off", "0", "false", "no")


def after_run(result: dict | None, staging_root, echo=print) -> dict | None:
    """The integrate / finish / blend hook: add the run to its target's
    sheet and rebuild it. Never raises (a sheet never fails a run); skips
    dry runs, failed runs and runs without a final JPG."""
    try:
        if not enabled() or not isinstance(result, dict) or result.get("dry_run"):
            return None
        for k in ("integration", "finish", "blend", "color"):
            if isinstance(result.get(k), dict) and result[k].get("ok") is False:
                return None
        led = result.get("ledger")
        if not led or not staging_root:
            return None
        run_dir = Path(led).parent
        if final_jpg(run_dir)[0] is None:
            return None
        root = store_root(staging_root)
        store, e = add_run(root, run_dir)
        path, changed = build(store)
        echo(f"  progression sheet {store.slug}: frame {e['id']} ({e['title']}), {path}"
             + (" (to republish)" if changed else ""))
        return {"slug": store.slug, "id": e["id"], "html": str(path), "pending": changed}
    except Exception as ex:  # noqa: BLE001 - never fail the run because of the sheet
        try:
            echo(f"  progression sheet not updated: {ex}")
        except Exception:  # noqa: BLE001
            pass
        return None


# ----------------------------------------------------------------- seed import

_ART = re.compile(r'<article class="frame" data-lin="(?P<lin>[^"]*)">(?P<body>.*?)</article>', re.S)


def _txt(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _num(s: str):
    s = s.strip()
    if s in ("", "unknown", "None"):
        return None
    try:
        v = float(s)
        return int(v) if v.is_integer() and "." not in s else v
    except ValueError:
        return s


def parse_published(text: str, year: str = "2026") -> tuple[dict, list[dict], list[tuple[str, bytes, bytes | None]]]:
    """(meta, versions, [(id, main jpg bytes, core jpg bytes)]) from a
    published sheet."""
    def one(rx, s=text, default=""):
        m = re.search(rx, s, re.S)
        return _txt(m.group(1)) if m else default
    meta = {"title": one(r"<h1>(.*?)</h1>"), "sub": one(r'<p class="sub">(.*?)</p>')}
    facts_html = re.search(r'<div class="facts">(.*?)</div>', text, re.S)
    facts = []
    for k, v in re.findall(r"<span>(.*?) <b>(.*?)</b></span>", facts_html.group(1) if facts_html else ""):
        if _txt(k) != "Frames":
            facts.append([_txt(k), _txt(v)])
    meta["facts"] = facts
    meta["filters"] = [[k, _txt(lbl)] for k, lbl in
                       re.findall(r'<button type="button" data-f="([^"]+)" aria-pressed="[^"]*">(.*?)</button>', text)
                       if k != "all"]
    notes_html = re.search(r'<section class="notes">(.*?)</section>', text, re.S)
    meta["notes"] = [_txt(n) for n in re.findall(r"<li>(.*?)</li>", notes_html.group(1) if notes_html else "", re.S)]
    versions, images = [], []
    for m in _ART.finditer(text):
        b = m.group("body")
        imgs = re.findall(r'<img src="data:image/jpeg;base64,([^"]+)"', b)
        main_b = base64.b64decode(imgs[0]) if imgs else b""
        core_b = base64.b64decode(imgs[1]) if 'class="inset"' in b and len(imgs) > 1 else None
        ident = one(r'<span class="num">(.*?)</span>', b)
        data_spans = re.findall(r"<span>(.*?)</span>", re.search(r'<div class="data">(.*?)</div>', b, re.S).group(1), re.S)
        subs = _num(_txt(re.sub(r"\s*subs$", "", _txt(data_spans[0])))) if data_spans else None
        hrs_t = _txt(data_spans[1]) if len(data_spans) > 1 else "unknown"
        hours = _num(hrs_t[:-2]) if hrs_t.endswith(" h") else None
        lengths = _txt(data_spans[2]) if len(data_spans) > 2 else "unknown"
        dds = dict(zip([_txt(x) for x in re.findall(r"<dt>(.*?)</dt>", b, re.S)],
                       [_txt(x) for x in re.findall(r"<dd>(.*?)</dd>", b, re.S)]))
        nights_t = dds.get("Nights", "unknown")
        nights = [] if nights_t == "unknown" else [f"{year}-{n.strip()}" for n in nights_t.split(",") if n.strip()]
        lineage = one(r'<span class="chip [^"]*">(.*?)</span>', b)
        changed = one(r'<p class="changed"><span>Changed</span>(.*?)</p>', b)
        e = {"id": ident, "file": "", "title": one(r"<h2>(.*?)</h2>", b), "date_made": one(r"<time>(.*?)</time>", b),
             "rig": RIG_OF_LINEAGE.get(lineage, ""), "lineage": lineage,
             "kind": "blend" if lineage == "Blend" else "integration",
             "data": {"subs": subs, "hours": hours, "lengths": lengths, "nights": nights},
             "calibration": dds.get("Calibration", "unknown"), "processing": dds.get("Processing", "unknown"),
             "changed": changed, "changed_edited": True, "title_edited": True, "source": "", "run": ""}
        versions.append(e)
        images.append((ident, main_b, core_b))
    return meta, versions, images


def import_seed(root: Path, seed_dir: Path, slug: str = "") -> Store:
    """A hand-built sheet into the store: versions.json + meta.json +
    thumbnails when the folder has them (M33), else parsed from its
    published.html (M31, Heart; meta.json there wins over the parsed meta).
    The rebuilt HTML counts as the published one (it is the same sheet)."""
    seed_dir = Path(seed_dir)
    slug = slug or seed_dir.name.lower()
    k = KNOWN_TARGETS.get(slug, {})
    s = Store(root, slug)
    s.dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {}
    if (seed_dir / "versions.json").is_file():
        s.versions = _read_json(seed_dir / "versions.json")
        for e in s.versions:
            for key in ("file", "core_file"):
                if e.get(key):
                    shutil.copyfile(seed_dir / e[key], s.dir / e[key])
            e.setdefault("kind", "blend" if e.get("lineage") == "Blend" else "integration")
            e.setdefault("run", Path(str(e.get("source") or "")).name)
            e["changed_edited"] = e["title_edited"] = True
    else:
        pub = seed_dir / "published.html"
        if not pub.is_file():
            raise ProgressionError(f"{seed_dir}: neither versions.json nor published.html")
        meta, s.versions, images = parse_published(pub.read_text(encoding="utf-8"))
        for e, (ident, main_b, core_b) in zip(s.versions, images):
            e["file"] = f"{ident}_{slug}.jpg"
            (s.dir / e["file"]).write_bytes(main_b)
            if core_b:
                e["core_file"] = f"{ident}_{slug}_core.jpg"
                (s.dir / e["core_file"]).write_bytes(core_b)
    if (seed_dir / "meta.json").is_file():
        meta = _read_json(seed_dir / "meta.json")
    meta.pop("out", None)
    base = default_meta(slug, (k.get("aliases") or [slug])[0])
    base.update({kk: v for kk, v in meta.items() if v not in (None, "", [])})
    base["aliases"] = list(k.get("aliases") or base.get("aliases") or [slug])
    base["artifact_url"] = k.get("artifact_url", base.get("artifact_url", ""))
    s.meta = base
    s.save()
    text = render(s)
    s.html_path.write_text(text, encoding="utf-8")
    s.meta["built_sha256"] = s.meta["published_sha256"] = sha(text)
    s.meta["published_at"] = "seed import " + _now()
    s.save()
    update_pending(root, s, False)
    return s
