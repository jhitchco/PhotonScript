"""PS-153: blend the RC16 luminance core into the Piggy-600 color image
(two-rig goals such as M31, PS-134).

`photonscript blend --target M31` finds the newest Piggy-600 OSC run and the
newest RC16 run of the target among the `photonscript integrate` run folders
(their ledger.json, else manifest.json), or takes the masters by path, then
generates deploy/blend_rc16_osc.js into a NEW blend run folder and runs it in
PixInsight (one instance at a time, log polled with short reads):

    <staging_root>/Blend/<target>_blend_<YYYYMMDD-HHMM>/
        blend_run.js  manifest.json  ledger.json (kind "blend")
        out/blend.log  out/timing.csv  out/timing_pi.csv
        out/final/<name>_blend_linear.xisf  <name>_blend.{xisf,tif,jpg}
                 <name>_osc_ab.jpg  <name>_blend_mask.xisf
                 <name>_core_linear.xisf  <name>_core.{xisf,tif,jpg}
                 <name>_blend_steps.json

Inputs per stage:
  linear (default): OSC out/final/*_linear.xisf (gradient removed, color
                    calibrated by the finish), else out/master/master_OSC.xisf;
                    RC16 out/final/*_<F>_linear.xisf, else master_<F>.xisf
  final:            out/final/*_final.xisf of both runs (already stretched)
RC16 luminance = the L master, else the mean of the R/G/B (or any) masters.
PS-186: at the linear stage the RC16 luminance first gets a BlurXTerminator
pass (correct-only, then sharpen) when the process is installed and rc16_bxt
is not off (config blend_rc16_bxt + finish_use_rcastro). The finish saves
*_linear.xisf before its own deconvolution, so this is the only BXT the RC16
data sees in the blend. Guarded: missing or failing = the blend runs on the
unsharpened luminance.

The Blend folder sits under the staging root on purpose: local_ledgers()
reads <staging_root>/*/ledger.json only, so blend ledgers never count as
integration versions, never trip integrate-watch and are never posted.

Fast scratch (integration_scratch_root): the blend is built in
<scratch>/Blend/<name> and moved to <staging_root>/Blend/<name> when
PixInsight made its outputs (out/work, the registration work, is dropped
unless --keep-work); integrate runs are found in both roots.

Narrowband (`--mode nb`, or auto when the RC16 run has an Ha master; the
Heart Nebula): deploy/blend_nb_osc.js (with the finish_osc.js,
finish_mono_color.js and blend_rc16_osc.js libraries copied in) registers
the RC16 Ha / OIII onto the OSC frame the same way, then inside the
feathered footprint fits each OSC channel as a + b NB + c min(R, G, B)
(Ha for R, OIII for G and B: the line from the RC16, the continuum from the
OSC) and blends it in, puts the luminance source (the RC16 L when the run
has one, else Ha at a modest weight) into CIE L, finishes the result like an
OSC master (natural HaRGB + the gold palette) and makes RC16-scale SHO / HOO
core crops with the OSC stars, in <staging_root>/Blend/<target>_blend_nb_<time>:

        out/final/<name>_nb_linear.xisf  <name>_nb_mask.xisf  <name>_nb_blend.jpg
                 <name>_osc_ab.jpg  <name>_haRGB.{xisf,tif,jpg}
                 <name>_haRGB_gold.{xisf,jpg}  <name>_haRGB_final_steps.json
                 <name>_core_SHO.{xisf,tif,jpg}  <name>_core_HOO.{xisf,tif,jpg}
                 <name>_core_color_steps.json  <name>_blend_steps.json
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from photonscript.integration import pilock, pjsr, runner, scratch
from photonscript.integration.pipeline import PipelineError, Timer, catalog_coords, is_inside, safe_name

TEMPLATE = "blend_rc16_osc.js"
TEMPLATE_NB = "blend_nb_osc.js"
MODES = ("auto", "l", "nb")
NB_FILTERS = ("Ha", "OIII", "SII")
FINISH_LIB_MARK = "//__FINISH_LIB__"
MONO_LIB_MARK = "//__MONO_LIB__"
BLEND_LIB_MARK = "//__BLEND_LIB__"
BLEND_LIB_BEGIN = "// ==== PS_BLEND_LIB_BEGIN"
BLEND_LIB_END = "// ==== PS_BLEND_LIB_END"
MONO_LIB_BEGIN = "// ==== PS_MONO_LIB_BEGIN"
MONO_LIB_END = "// ==== PS_MONO_LIB_END"
CORE_STEPS = "core_color_steps.json"   # <name>_core_color_steps.json (the SHO / HOO summary)
# narrowband defaults (--ha-weight etc.): the RC16 model's share of the OSC
# channel inside the mask, the extra line on top, the Ha share of CIE L
NB_DEFAULTS = {"ha_weight": 0.8, "oiii_weight": 0.6, "ha_boost": 0.15, "oiii_boost": 0.3,
               "nb_lum_weight": 0.3}
BLEND_DIR = "Blend"
STAGES = ("linear", "final")
LUM_ORDER = ("L", "R", "G", "B")
OSC_SCALE = 1.29      # piggyback_pixel_scale_arcsec (600 mm + IMX571 3.76 um)
RC16_SCALE = 0.236    # pixel_scale_arcsec (RC16 3248 mm + AP26MC, plate solves)
PIXEL_UM = 3.76
_SKIP_MASTER = re.compile(r"^master(Bias|Dark|Flat|OSC)", re.I)
_HDR = "_hdr"                                     # integration/hdr.py SUFFIX
_EXP_VARIANT = re.compile(r"^(.+)_\d+(?:\.\d+)?s$")   # PS-177 L_30s -> L


class BlendError(PipelineError):
    pass


@dataclass
class RunInfo:
    run_dir: Path
    rig: str
    campaign: str
    version: int = 0
    created_at: str = ""
    finish_ok: bool | None = None


@dataclass
class Inputs:
    stage: str
    osc: Path
    rc16: dict[str, Path]                 # filter -> master
    osc_run: RunInfo | None = None
    rc16_run: RunInfo | None = None
    notes: list[str] = field(default_factory=list)
    mode: str = "l"                       # l | nb (select_mode)
    mode_why: str = ""

    def nb(self) -> dict[str, Path]:
        return nb_masters(self.rc16)

    def lum_filters(self) -> list[str]:
        if self.mode == "nb":
            return ["L"] if "L" in self.rc16 else ["Ha"]
        return ["L"] if "L" in self.rc16 else sorted(self.rc16, key=_filter_rank)


@dataclass
class Options:
    target: str
    staging_root: Path = Path(r"D:\Astrophotography\Staging")
    out: Path | None = None
    osc: Path | None = None
    rc16: list[Path] | None = None
    stage: str = "linear"
    weight: float = 0.7
    inset_frac: float = 0.02
    feather_frac: float = 0.08
    lum_mask: bool = False
    core: bool = True
    core_margin_frac: float = 0.05
    osc_scale: float = OSC_SCALE
    rc16_scale: float = RC16_SCALE
    solve: bool = True
    resolve: bool = False
    keep_work: bool = False
    bg_target: float = pjsr.FINISH_DEFAULTS["bg_target"]
    shadow_sigma: float = pjsr.FINISH_DEFAULTS["shadow_sigma"]
    rc16_bxt: str = "auto"            # PS-186: auto | on | off (rc16_bxt_mode(cfg))
    mode: str = "auto"                # l | nb | auto (nb when the RC16 run has an Ha master)
    ha_weight: float = NB_DEFAULTS["ha_weight"]
    oiii_weight: float = NB_DEFAULTS["oiii_weight"]
    ha_boost: float = NB_DEFAULTS["ha_boost"]
    oiii_boost: float = NB_DEFAULTS["oiii_boost"]
    nb_lum_weight: float = NB_DEFAULTS["nb_lum_weight"]   # Ha in CIE L (an RC16 L uses weight)
    palette: str = "gold"             # nb: also <name>_haRGB_gold (off = natural HaRGB only)
    rc_mode: str = "auto"             # nb finish chain RC Astro (finish_use_rcastro)
    nxt_denoise: float = pjsr.FINISH_DEFAULTS["nxt_denoise"]
    bxt_stars: float = pjsr.FINISH_DEFAULTS["bxt_stars"]
    bxt_nonstellar: float = pjsr.FINISH_DEFAULTS["bxt_nonstellar"]
    run_pixinsight: bool = True
    dry_run: bool = False
    pixinsight: str = runner.DEFAULT_EXE
    lock_wait_min: float = 60.0       # pixinsight_lock_wait_min: wait this long for the PixInsight queue lock
    scratch_root: Path | None = None  # fast scratch: build here, archive to staging_root/Blend
    extra_roots: list[Path] = field(default_factory=list)   # integration_extra_roots (C:/Astro/Staging)
    vivid: bool = True                # blend_vivid: <name>_vivid from the stretched L blend
    vivid_kinds: str = "galaxy"       # blend_vivid_kinds ("" = every target)
    vivid_sat_mid: float = pjsr.FINISH_DEFAULTS["vivid_sat_mid"]
    vivid_hue_boost: float = pjsr.FINISH_DEFAULTS["vivid_hue_boost"]
    vivid_lhe: float = pjsr.FINISH_DEFAULTS["vivid_lhe"]


def extra_roots(cfg) -> list[Path]:
    """integration_extra_roots (comma list) as Paths; blank = none."""
    raw = str(getattr(cfg, "integration_extra_roots", "") or "") if cfg is not None else ""
    return [Path(x.strip()) for x in raw.split(",") if x.strip()]


def config_vivid(cfg) -> dict:
    """Options fields for the blend vivid from the config: blend_vivid,
    blend_vivid_kinds and the finish_vivid_* look."""
    v = pjsr.vivid_settings(cfg)
    kinds = getattr(cfg, "blend_vivid_kinds", None) if cfg is not None else None
    return {"vivid": bool(getattr(cfg, "blend_vivid", True)) if cfg is not None else True,
            "vivid_kinds": "galaxy" if kinds is None else str(kinds),
            "vivid_sat_mid": v["vivid_sat_mid"], "vivid_hue_boost": v["vivid_hue_boost"],
            "vivid_lhe": v["vivid_lhe"]}


def rc16_bxt_mode(cfg) -> str:
    """PS-186: the blend's RC16 BlurXTerminator mode from the config: off when
    blend_rc16_bxt is off, else finish_use_rcastro (auto | on | off)."""
    if cfg is not None and not bool(getattr(cfg, "blend_rc16_bxt", True)):
        return "off"
    return pjsr.rc_mode(getattr(cfg, "finish_use_rcastro", "auto") if cfg is not None else "auto")


def config_bxt(cfg) -> dict:
    """Options fields for the blend's RC16 BXT pass from the config."""
    rc = pjsr.rc_settings(cfg)
    return {"rc16_bxt": rc16_bxt_mode(cfg), "bxt_stars": rc["bxt_stars"],
            "bxt_nonstellar": rc["bxt_nonstellar"], "rc_mode": rc["rc_mode"],
            "nxt_denoise": rc["nxt_denoise"]}


def parse_mode(v) -> str:
    """--mode: l | nb | auto (blank = auto). Raises BlendError otherwise."""
    m = str(v if v is not None else "").strip().lower() or "auto"
    m = {"lum": "l", "luminance": "l", "narrowband": "nb"}.get(m, m)
    if m not in MODES:
        raise BlendError(f"--mode must be one of {', '.join(MODES)} (got {v!r})")
    return m


def nb_masters(rc16: dict) -> dict[str, Path]:
    """{Ha, OIII, SII: path} of the RC16 masters (any filter spelling: H,
    Halpha, O3, S2 ...); the first path of a filter is kept."""
    from photonscript.integration.mono_color import canonical
    out: dict[str, Path] = {}
    for f, p in rc16.items():
        c = canonical(f)
        if c in NB_FILTERS and c not in out:
            out[c] = Path(p)
    return out


def select_mode(rc16: dict, mode: str = "auto", stage: str = "linear") -> tuple[str, str]:
    """(l | nb, why) for these RC16 masters. auto = nb when there is an Ha
    master and the stage is linear, else l. nb needs Ha and the linear
    stage (raises BlendError)."""
    mode = parse_mode(mode)
    nb = nb_masters(rc16)
    have = ", ".join(f for f in NB_FILTERS if f in nb)
    if mode == "l":
        return "l", "--mode l"
    if mode == "nb":
        if "Ha" not in nb:
            raise BlendError("--mode nb needs an RC16 Ha master (have "
                             + (", ".join(sorted(rc16, key=_filter_rank)) or "none") + ")")
        if stage != "linear":
            raise BlendError("--mode nb needs --stage linear (the channel fits model linear data)")
        return "nb", f"--mode nb (RC16 {have})"
    if "Ha" not in nb:
        return "l", "auto: no RC16 Ha master"
    if stage != "linear":
        return "l", f"auto: RC16 {have}, but stage {stage} (narrowband needs linear)"
    return "nb", f"auto: RC16 {have}"


def _filter_rank(f: str) -> tuple:
    return (LUM_ORDER.index(f) if f in LUM_ORDER else len(LUM_ORDER), f)


# ----------------------------------------------------------------- discovery

def run_info(run_dir: Path) -> RunInfo | None:
    """Rig / campaign / version of an integrate run folder: ledger.json,
    else manifest.json. None when neither is readable or it is a blend."""
    run_dir = Path(run_dir)
    lp = run_dir / "ledger.json"
    if lp.is_file():
        try:
            from photonscript.shared import ledger as L
            led = L.load(lp)
            if led.kind != "integration":
                return None
            return RunInfo(run_dir, led.rig, led.campaign, led.version, led.created_at,
                           (led.machine.get("finish") or {}).get("ok"))
        except Exception:  # noqa: BLE001 - half-written or hand-edited: try the manifest
            pass
    mp = run_dir / "manifest.json"
    if mp.is_file():
        try:
            m = json.loads(mp.read_text(encoding="ascii"))
            if m.get("rig") and m.get("target"):
                return RunInfo(run_dir, str(m["rig"]), str(m["target"]), 0, str(m.get("built", "")))
        except Exception:  # noqa: BLE001
            pass
    return None


def masters(run_dir: Path) -> dict[str, Path]:
    """filter -> out/master/master_<F>.xisf (calibration masters and the
    masterOSC copy left out). A filter with an HDR master (master_<F>_hdr,
    integration/hdr.py) gets that one, and its single-exposure masters
    (master_<F>_<exp>) are left out: they are inside the HDR master."""
    out = {}
    for p in sorted((Path(run_dir) / "out" / "master").glob("master_*.xisf")):
        stem = p.stem
        if _SKIP_MASTER.match(stem) or stem.endswith("_review"):
            continue
        out[stem[len("master_"):]] = p
    for key in [k for k in out if k.endswith(_HDR)]:
        f = key[: -len(_HDR)]
        out[f] = out.pop(key)
        for k in [k for k in out if (m := _EXP_VARIANT.match(k)) and m.group(1) == f]:
            del out[k]
    return out


FINISH_DIR = "Finish"                  # integration/finish.py FINISH_DIR
_NOT_RUNS = {BLEND_DIR.lower(), FINISH_DIR.lower()}


def discovery_roots(staging_root: Path, scratch_root: Path | None = None,
                    extra: list[Path] | tuple = ()) -> list[Path]:
    """Every folder that holds integrate runs: the staging (archive) root,
    the fast scratch root, then integration_extra_roots (the pre-scratch
    C:/Astro/Staging); each once."""
    out: list[Path] = []
    seen: set[str] = set()
    for r in [*scratch.run_roots(staging_root, scratch_root), *[Path(x) for x in extra or ()]]:
        k = _norm(r)
        if k not in seen:
            seen.add(k)
            out.append(Path(r))
    return out


def _norm(p) -> str:
    return str(scratch._resolve(Path(p))).lower()


def _ledger_json(d: Path) -> dict | None:
    try:
        return json.loads((Path(d) / "ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def finish_source(d: Path) -> str:
    """The integrate run a finish folder was made from (its kind finish
    ledger's input.run / input.mono_run, else its manifest), else ""."""
    led = _ledger_json(d)
    if led and led.get("kind") == "finish":
        inp = (led.get("machine") or {}).get("input") or {}
        return str(inp.get("run") or inp.get("mono_run") or "")
    try:
        m = json.loads((Path(d) / "manifest.json").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return ""
    if m.get("kind") == "finish":
        return str(m.get("source_run") or m.get("mono_run") or "")
    return ""


def same_run(ref: str, run_dir: Path) -> bool:
    """Does a recorded run path name this run folder? The same path, or
    (a scratch path archived since) the same folder name when the recorded
    path is gone."""
    if not ref:
        return False
    r = Path(ref)
    if _norm(r) == _norm(run_dir):
        return True
    try:
        gone = not r.is_dir()
    except OSError:
        gone = True
    return gone and r.name.lower() == Path(run_dir).name.lower()


def final_dirs(run_dir: Path, roots: list[Path] | tuple = ()) -> list[Path]:
    """The out/final folders holding finish products of this run: its own,
    a re-finish nested in it (<run>/out/<x>/out/final, a hand --out such as
    out/final_v6), and every <root>/Finish/<name> whose ledger names this
    run as its input (`photonscript finish`, the finish-only retry)."""
    run_dir = Path(run_dir)
    out = []
    own = run_dir / "out" / "final"
    if own.is_dir():
        out.append(own)
    try:
        nested = sorted(p for p in (run_dir / "out").iterdir()
                        if p.is_dir() and p.name.lower() not in ("final", "master", "work"))
    except OSError:
        nested = []
    for d in nested:
        if (d / "out" / "final").is_dir():
            out.append(d / "out" / "final")
    for root in roots or ():
        try:
            subs = sorted(p for p in (Path(root) / FINISH_DIR).iterdir() if p.is_dir())
        except OSError:
            continue
        for d in subs:
            if (d / "out" / "final").is_dir() and same_run(finish_source(d), run_dir):
                out.append(d / "out" / "final")
    return out


def _mtime(p: Path) -> float:
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return 0.0


def finished(run_dir: Path, kind: str, roots: list[Path] | tuple = ()) -> dict[str, Path]:
    """filter -> <name>_<F>_<kind>.xisf (kind: linear | final), matched to
    the run's masters by the filter suffix: the newest such file among the
    run's own out/final and its re-finishes (final_dirs)."""
    out: dict[str, Path] = {}
    finals = [p for d in final_dirs(run_dir, roots) for p in sorted(d.glob(f"*_{kind}.xisf"))]
    for f in masters(run_dir):
        hit = [p for p in finals if p.stem.endswith(f"_{f}_{kind}")]
        if hit:
            out[f] = max(hit, key=_mtime)
    return out


def files_for(run_dir: Path, stage: str, roots: list[Path] | tuple = ()) -> tuple[dict[str, Path], str]:
    """(filter -> image, what) for a run at a stage. linear: the finish's
    *_linear.xisf per filter (the newest re-finish counts), else the raw
    master; final: *_final.xisf."""
    if stage == "final":
        return finished(run_dir, "final", roots), "finish *_final.xisf"
    lin = finished(run_dir, "linear", roots)
    raw = masters(run_dir)
    got = {f: lin.get(f) or raw[f] for f in raw}
    what = ("finish *_linear.xisf" if lin and len(lin) == len(raw)
            else "raw master_*.xisf" if not lin else "mixed linear / raw")
    if any(_norm(p.parent) != _norm(Path(run_dir) / "out" / "final") for p in lin.values()):
        what += " (re-finish)"
    return got, what


def _has_hdr_l(run_dir: Path) -> bool:
    return (Path(run_dir) / "out" / "master" / f"master_L{_HDR}.xisf").is_file()


def find_runs(staging_root: Path, target: str, rig: str,
              scratch_root: Path | None = None, extra_roots: list[Path] | tuple = ()) -> list[RunInfo]:
    """Integrate runs of `target` + `rig` under every discovery root (the
    staging root, the fast scratch root, integration_extra_roots), newest
    first: version, then created time, then an HDR L master, then the
    folder name. A folder name found in two roots counts once: the copy
    with the higher (version, created time) wins, the earlier root on a
    tie (so a manifest-only stub never hides the real run and its ledger)."""
    from photonscript.integration.ledger import same_campaign
    best: dict[str, RunInfo] = {}
    for root in discovery_roots(staging_root, scratch_root, extra_roots):
        try:
            dirs = sorted(p for p in Path(root).iterdir()
                          if p.is_dir() and p.name.lower() not in _NOT_RUNS)
        except OSError:
            continue
        for d in dirs:
            info = run_info(d)
            if not (info and info.rig == rig and same_campaign(info.campaign, target)):
                continue
            k = d.name.lower()
            cur = best.get(k)
            if cur is None or (info.version, info.created_at) > (cur.version, cur.created_at):
                best[k] = info
    return sorted(best.values(), key=lambda r: (r.version, r.created_at, _has_hdr_l(r.run_dir),
                                                r.run_dir.name), reverse=True)


def _pick(runs: list[RunInfo], stage: str, want,
          roots: list[Path] | tuple = ()) -> tuple[RunInfo | None, dict[str, Path], str]:
    """Newest run with what `want` needs. Linear: a run whose finish made
    the *_linear files (gradient removed, color calibrated; its own or a
    re-finish, final_dirs) beats a newer run that only has raw masters;
    raw masters are the fallback."""
    passes = [True, False] if stage == "linear" else [False]
    for finished_only in passes:
        for r in runs:
            got, what = files_for(r.run_dir, stage, roots)
            if finished_only:
                got = {f: p for f, p in got.items() if p.stem.endswith("_linear")}
                what = "finish *_linear.xisf" + (" (re-finish)" if "re-finish" in what else "")
            if want(got):
                return r, got, what
    return None, {}, ""


def filter_of(path: Path) -> str:
    """Filter of a master given by path: master_<F> (or master_<F>_hdr),
    <name>_<F>_linear / _final, else L."""
    stem = Path(path).stem
    m = re.match(r"^master_([A-Za-z0-9]+)(?:_hdr)?$", stem)
    if m:
        return m.group(1)
    m = re.search(r"_([A-Za-z0-9]+)_(linear|final)$", stem)
    if m:
        return m.group(1)
    return "L"


def discover(o: Options) -> Inputs:
    """The OSC + RC16 inputs for a blend (given paths win over discovery)."""
    if o.stage not in STAGES:
        raise BlendError(f"--stage must be one of {', '.join(STAGES)}")
    notes: list[str] = []
    osc_run = rc_run = None
    roots = discovery_roots(o.staging_root, o.scratch_root, o.extra_roots)
    if o.osc:
        osc = Path(o.osc)
    else:
        runs = find_runs(o.staging_root, o.target, "piggyback", o.scratch_root, o.extra_roots)
        osc_run, got, what = _pick(runs, o.stage, lambda g: "OSC" in g, roots)
        if not osc_run:
            raise BlendError(f"no Piggy-600 OSC {o.stage} master for {o.target!r} under {o.staging_root} "
                             f"({len(runs)} piggyback run(s) found); run `photonscript integrate "
                             f"--target \"{o.target}\" --rig piggyback` or pass --osc")
        osc = got["OSC"]
        notes.append(f"OSC from {osc_run.run_dir.name} (v{osc_run.version}): {what}")
    mode = parse_mode(o.mode)
    if o.rc16:
        rc16 = {}
        for p in o.rc16:
            rc16.setdefault(filter_of(p), Path(p))
    else:
        runs = find_runs(o.staging_root, o.target, "rc16", o.scratch_root, o.extra_roots)
        want = (lambda g: "Ha" in nb_masters(g)) if mode == "nb" else bool
        rc_run, rc16, what = _pick(runs, o.stage, want, roots)
        if not rc_run:
            raise BlendError(f"no RC16 {o.stage} master for {o.target!r} under {o.staging_root} "
                             f"({len(runs)} rc16 run(s) found); run `photonscript integrate "
                             f"--target \"{o.target}\" --rig rc16` or pass --rc16")
        notes.append(f"RC16 from {rc_run.run_dir.name} (v{rc_run.version}): {what}, "
                     f"filters {', '.join(sorted(rc16, key=_filter_rank))}"
                     + (" (L from the HDR master)" if "L" in rc16 and _has_hdr_l(rc_run.run_dir) else ""))
    missing = [str(p) for p in [osc, *rc16.values()] if not Path(p).is_file()]
    if missing:
        raise BlendError("missing input file(s): " + ", ".join(missing))
    sel, why = select_mode(rc16, mode, o.stage)
    inp = Inputs(o.stage, osc, rc16, osc_run, rc_run, notes, sel, why)
    notes.append(f"mode {sel}: {why}")
    if sel == "nb":
        nb = inp.nb()
        notes.append("narrowband: Ha -> R"
                     + (", OIII -> G and B" if "OIII" in nb else " (no OIII: G and B as they are)")
                     + f", luminance from {inp.lum_filters()[0]}"
                     + ("" if {"Ha", "OIII"} <= set(nb) else "; no SHO / HOO core (needs OIII)"))
    elif "L" not in rc16:
        notes.append("no RC16 L master: luminance = mean of " + "+".join(inp.lum_filters()))
    if o.stage == "linear" and osc.name.startswith("master_"):
        notes.append("OSC is the raw master (no finish yet): not gradient-removed or color calibrated")
    return inp


# ----------------------------------------------------------------- script

def default_run_dir(o: Options, now: datetime | None = None, mode: str = "l") -> Path:
    now = now or datetime.now()
    tag = "blend_nb" if mode == "nb" else "blend"
    return Path(o.staging_root) / BLEND_DIR / f"{safe_name(o.target)}_{tag}_{now:%Y%m%d-%H%M}"


def blend_config(o: Options, inp: Inputs, run_dir: Path) -> dict:
    ra, dec = catalog_coords(o.target)
    lum = inp.lum_filters()
    return {
        "out": pjsr.fwd(Path(run_dir) / "out"), "name": safe_name(o.target), "stage": inp.stage, "mode": "l",
        "weight": float(o.weight), "inset_frac": float(o.inset_frac),
        "feather_frac": float(o.feather_frac), "lum_mask": bool(o.lum_mask),
        "core": bool(o.core), "core_margin_frac": float(o.core_margin_frac),
        "osc": {"path": pjsr.fwd(inp.osc), "scale": float(o.osc_scale), "pixel_um": PIXEL_UM,
                "step_deg": 0.6, "rings": 2},
        "rc16": [{"filter": f, "path": pjsr.fwd(inp.rc16.get(f) or inp.nb()[f])} for f in lum],
        "rc16_scale": float(o.rc16_scale), "rc16_pixel_um": PIXEL_UM, "rc16_step_deg": 0.15,
        "ra_deg": ra, "dec_deg": dec, "solve": bool(o.solve), "resolve": bool(o.resolve),
        "keep_work": bool(o.keep_work),
        "bg_target": float(o.bg_target), "shadow_sigma": float(o.shadow_sigma),
        "rc16_bxt": {"mode": pjsr.rc_mode(o.rc16_bxt) if inp.stage == "linear" else "off",
                     "why_off": "" if inp.stage == "linear" else "stage final (already stretched)",
                     "stars": pjsr.unit_value("bxt_stars", o.bxt_stars),
                     "nonstellar": pjsr.unit_value("bxt_nonstellar", o.bxt_nonstellar)},
    }


def _unit(name: str, v) -> float:
    try:
        return pjsr.unit_value(name, v)
    except (pjsr.PjsrError, TypeError, ValueError) as e:
        raise BlendError(f"--{name.replace('_', '-')} must be within 0..1 (got {v!r})") from e


def core_items(o: Options, inp: Inputs) -> tuple[list[dict], list[str]]:
    """The RC16-scale SHO / HOO items (mono_color.plan on the RC16
    narrowband masters, named <name>_core_SHO / _HOO), each with the OSC
    stars (stars_from "rgb": the blend saves the OSC star layer as
    finish_mono_color.js's STARS_FILE). [] when --no-core."""
    from photonscript.integration import mono_color
    if not o.core:
        return [], ["core off (--no-core)"]
    nb = {f: p for f, p in inp.nb().items()}
    items, notes = mono_color.plan(nb, f"{safe_name(o.target)}_core")
    items = [dict(it, stars_from="rgb") for it in items if it["kind"] in mono_color.NB_KINDS]
    return items, notes


def finish_settings(o: Options, run_dir: Path, items: list[dict]) -> dict:
    """CONFIG.finish of the narrowband script: the finish_osc.js /
    finish_mono_color.js globals (mono_color.config with the run's RC
    Astro settings, the OSC optics and the gold mix)."""
    from photonscript.integration import mono_color
    ra, dec = catalog_coords(o.target)
    out_dir = Path(run_dir) / "out"
    gx = pjsr.find_graxpert()
    fcfg = {"ra_deg": ra, "dec_deg": dec, "focal_mm": 600.0, "pixel_um": PIXEL_UM,
            "staging": pjsr.fwd(run_dir), "rc_mode": pjsr.rc_mode(o.rc_mode),
            "bxt_stars": o.bxt_stars, "bxt_nonstellar": o.bxt_nonstellar, "nxt_denoise": o.nxt_denoise,
            "bg_target": o.bg_target, "shadow_sigma": o.shadow_sigma,
            "pi_library": pjsr.fwd(Path(o.pixinsight).parent.parent / "library"),
            "graxpert": gx, "graxpert_version": "unknown" if gx else ""}
    c = mono_color.config(out_dir, items, fcfg, o.target)
    c.pop("items", None)
    c["steps_file"] = pjsr.fwd(out_dir / "final" / f"{safe_name(o.target)}_{CORE_STEPS}")
    c["gold_mix"] = pjsr.FINISH_DEFAULTS["gold_mix"]
    return c


def nb_config(o: Options, inp: Inputs, run_dir: Path) -> dict:
    """CONFIG of blend_nb_osc.js: blend_config's registration settings plus
    the narrowband masters, weights, luminance source, palette, the core
    items and the finish settings."""
    cfg = blend_config(o, inp, run_dir)
    nb = inp.nb()
    lum = inp.lum_filters()[0]
    items, _ = core_items(o, inp)
    cfg.update(
        mode="nb", nb={f: (pjsr.fwd(nb[f]) if f in nb else None) for f in NB_FILTERS},
        lum={"filter": lum, "path": pjsr.fwd(inp.rc16[lum] if lum == "L" else nb["Ha"]),
             "weight": _unit("weight", o.weight) if lum == "L" else _unit("nb_lum_weight", o.nb_lum_weight)},
        ha_weight=_unit("ha_weight", o.ha_weight), oiii_weight=_unit("oiii_weight", o.oiii_weight),
        ha_boost=_unit("ha_boost", o.ha_boost), oiii_boost=_unit("oiii_boost", o.oiii_boost),
        palette=pjsr.palette_mode(o.palette), items=items,
        finish=finish_settings(o, run_dir, items))
    return cfg


def rc16_depth(inp: Inputs) -> dict[str, dict] | None:
    """{Ha, OIII, SII: {subs, hours}} the chosen RC16 run integrated, from
    its ledger (acquisition.subs_by_filter; PS-177 exposure groups such as
    Ha_30s count toward Ha). None when unknown: given --rc16 paths, or the
    run has no readable ledger."""
    from photonscript.integration.mono_color import canonical
    run = getattr(inp, "rc16_run", None)
    if run is None:
        return None
    try:
        led = json.loads((Path(run.run_dir) / "ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sbf = ((led.get("machine") or {}).get("acquisition") or {}).get("subs_by_filter")
    if not isinstance(sbf, dict):
        return None
    out: dict[str, dict] = {}
    for k, v in sbf.items():
        m = _EXP_VARIANT.match(str(k))
        c = canonical(m.group(1) if m else k)
        if c not in NB_FILTERS or not isinstance(v, dict):
            continue
        b = out.setdefault(c, {"subs": 0, "hours": 0.0})
        b["subs"] += int(v.get("integrated") or 0)
        b["hours"] += float(v.get("integrated_s") or 0.0) / 3600.0
    for b in out.values():
        b["hours"] = round(b["hours"], 2)
    return out


def channel_plan(inp: Inputs, items: list[dict] | None = None) -> dict:
    """Which RC16 master is meant to go where (the steps json says what
    PixInsight actually applied): {R, G, B, L: [filters], core: [kinds]}."""
    if inp.mode != "nb":
        return {"L": inp.lum_filters()}
    nb = inp.nb()
    o3 = ["OIII"] if "OIII" in nb else []
    return {"R": ["Ha"], "G": o3, "B": o3, "L": inp.lum_filters(),
            "core": [it["kind"].upper() for it in (items or [])]}


def library(text: str, begin: str, end: str, name: str) -> str:
    a, b = text.find(begin), text.find(end)
    if a < 0 or b < a:
        raise pjsr.PjsrError(f"{name}: no {begin[8:]} / {end[8:]} markers")
    return text[a:b]


def render_nb(cfg: dict, solver_block: str = "", deploy_dir: Path | None = None) -> str:
    """deploy/blend_nb_osc.js with the three libraries copied in (finish_osc.js
    PS_FINISH_LIB, finish_mono_color.js PS_MONO_LIB, blend_rc16_osc.js
    PS_BLEND_LIB) and CONFIG / the solver include filled."""
    from photonscript.integration import mono_color
    t = pjsr.template(TEMPLATE_NB, deploy_dir)
    for mark in (FINISH_LIB_MARK, MONO_LIB_MARK, BLEND_LIB_MARK):
        if mark not in t:
            raise pjsr.PjsrError(f"{TEMPLATE_NB} has no {mark}")
    libs = {
        FINISH_LIB_MARK: mono_color.finish_library(deploy_dir),
        MONO_LIB_MARK: library(pjsr.template(mono_color.TEMPLATE, deploy_dir), MONO_LIB_BEGIN, MONO_LIB_END,
                               mono_color.TEMPLATE),
        BLEND_LIB_MARK: library(pjsr.template(TEMPLATE, deploy_dir), BLEND_LIB_BEGIN, BLEND_LIB_END, TEMPLATE),
    }
    for mark, text in libs.items():
        t = t.replace(mark, text)
    return pjsr.render(t, cfg, solver_block)


def render(cfg: dict, solver_block: str = "", deploy_dir: Path | None = None) -> str:
    if cfg.get("mode") == "nb":
        return render_nb(cfg, solver_block, deploy_dir)
    return pjsr.render(pjsr.template(TEMPLATE, deploy_dir), cfg, solver_block)


def config_for(o: Options, inp: Inputs, run_dir: Path) -> dict:
    return nb_config(o, inp, run_dir) if inp.mode == "nb" else blend_config(o, inp, run_dir)


# ----------------------------------------------------------------- vivid

VIVID_TEMPLATE = "blend_vivid.js"
VIVID_SCRIPT = "blend_vivid_run.js"
VIVID_LOG = "blend_vivid.log"
VIVID_LIB_MARK = "//__VIVID_LIB__"
VIVID_LIB_BEGIN = "// ==== PS_VIVID_LIB_BEGIN"
VIVID_LIB_END = "// ==== PS_VIVID_LIB_END"
BLEND_SAT_MID = 0.5        # the blend's stretch has no saturation curve


def vivid_wanted(o: Options, inp: Inputs) -> tuple[bool, str]:
    """(make the vivid blend?, why not). L blends only (a narrowband blend
    has its own HaRGB / gold / SHO products), blend_vivid on, and the
    target's catalog type one of blend_vivid_kinds (an unknown type keeps
    it, like finish_palette_kinds)."""
    if not o.vivid:
        return False, "blend_vivid off"
    if inp.mode != "l":
        return False, "narrowband blend (HaRGB / gold / SHO products instead)"
    ks = [k.strip().lower() for k in str(o.vivid_kinds or "").split(",") if k.strip()]
    kind = pjsr.target_kind(o.target) if ks else ""
    if ks and kind and not any(k in kind for k in ks):
        return False, f"{o.target} is catalogued as {kind}, not one of blend_vivid_kinds ({', '.join(ks)})"
    return True, ""


def vivid_config(o: Options, run_dir: Path) -> dict:
    out_dir = Path(run_dir) / "out"
    name = f"{safe_name(o.target)}_blend"
    return {"name": name, "input": pjsr.fwd(out_dir / "final" / f"{name}.xisf"),
            "final": pjsr.fwd(out_dir / "final"), "out": pjsr.fwd(out_dir),
            "sat_mid": BLEND_SAT_MID, "bg_target": float(o.bg_target),
            "vivid_sat_mid": pjsr.vivid_sat_mid(o.vivid_sat_mid),
            "vivid_hue_boost": pjsr.unit_value("vivid_hue_boost", o.vivid_hue_boost),
            "vivid_lhe": pjsr.unit_value("vivid_lhe", o.vivid_lhe)}


def render_vivid(cfg: dict, deploy_dir: Path | None = None) -> str:
    """deploy/blend_vivid.js with the finish_osc.js libraries (PS_FINISH_LIB,
    PS_VIVID_LIB) copied in and CONFIG filled."""
    from photonscript.integration import mono_color
    t = pjsr.template(VIVID_TEMPLATE, deploy_dir)
    for mark in (FINISH_LIB_MARK, VIVID_LIB_MARK):
        if mark not in t:
            raise pjsr.PjsrError(f"{VIVID_TEMPLATE} has no {mark}")
    t = t.replace(FINISH_LIB_MARK, mono_color.finish_library(deploy_dir))
    t = t.replace(VIVID_LIB_MARK, library(pjsr.template(pjsr.FINISH_TEMPLATE, deploy_dir),
                                          VIVID_LIB_BEGIN, VIVID_LIB_END, pjsr.FINISH_TEMPLATE))
    return pjsr.render(t, cfg)


def run_vivid(o: Options, run_dir: Path, echo=print) -> dict:
    """The vivid blend in PixInsight after a successful L blend. Never
    raises: {ok, why, minutes, outputs, script}."""
    cfg = vivid_config(o, run_dir)
    src = Path(cfg["input"])
    res: dict = {"ok": False, "why": "", "minutes": 0.0, "outputs": []}
    if not src.is_file():
        res["why"] = f"no stretched blend {src.name}"
        echo(f"  vivid: not made ({res['why']})")
        return res
    try:
        js = pjsr.write(Path(run_dir) / VIVID_SCRIPT, render_vivid(cfg))
        res["script"] = str(js)
        r = runner.run_script(js, Path(run_dir) / "out" / VIVID_LOG, exe=o.pixinsight, echo=echo)
        jpg = Path(run_dir) / "out" / "final" / f"{cfg['name']}_vivid.jpg"
        res.update(ok=bool(r.get("ok")) and jpg.is_file(), minutes=r.get("minutes", 0.0),
                   last_line=r.get("last_line", ""))
        res["outputs"] = [str(p) for p in sorted(jpg.parent.glob(f"{cfg['name']}_vivid.*"))]
        if not res["ok"]:
            res["why"] = r.get("last_line") or "no vivid JPG"
    except Exception as e:  # noqa: BLE001 - the natural blend stands without it
        res["why"] = str(e)
    echo(f"  vivid: {'made ' + cfg['name'] + '_vivid.jpg' if res['ok'] else 'FAILED (' + res['why'] + ')'}")
    return res


# ----------------------------------------------------------------- records

def _file_entry(p: Path, role: str, filt: str, run: RunInfo | None) -> dict:
    p = Path(p)
    st = p.stat() if p.exists() else None
    return {"role": role, "filter": filt, "path": str(p), "bytes": st.st_size if st else None,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S") if st else "",
            "run": str(run.run_dir) if run else "", "run_version": run.version if run else None}


def outputs(out_dir: Path) -> list[str]:
    return [str(p) for p in sorted((Path(out_dir) / "final").glob("*"))
            if p.suffix.lower() in (".xisf", ".tif", ".jpg", ".json")]


def steps(out_dir: Path) -> dict | None:
    for f in sorted((Path(out_dir) / "final").glob("*_blend_steps.json")):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def build_manifest(o: Options, inp: Inputs, run_dir: Path, cfg: dict, pi: dict | None,
                   timing: list) -> dict:
    files = [_file_entry(inp.osc, "osc", "OSC", inp.osc_run)]
    files += [_file_entry(inp.rc16[f], "rc16", f, inp.rc16_run) for f in sorted(inp.rc16, key=_filter_rank)]
    return {"version": 1, "kind": "blend", "target": o.target, "run_name": Path(run_dir).name,
            "run_dir": str(run_dir), "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "stage": inp.stage, "mode": inp.mode, "mode_why": inp.mode_why, "inputs": files,
            "luminance_from": inp.lum_filters(), "channels": channel_plan(inp, cfg.get("items")),
            "notes": inp.notes, "config": cfg, "pixinsight": pi, "timing": timing,
            "outputs": outputs(Path(run_dir) / "out")}


OPTION_KEYS = ("stage", "mode", "weight", "inset_frac", "feather_frac", "lum_mask", "core",
               "core_margin_frac", "rc16_bxt", "ha_weight", "oiii_weight", "ha_boost", "oiii_boost",
               "lum", "palette")


def build_ledger(o: Options, inp: Inputs, run_dir: Path, man: dict, pi: dict | None, timing: list):
    from photonscript.integration.ledger import repo_commit
    from photonscript.shared import ledger as L
    out_dir = Path(run_dir) / "out"
    st = steps(out_dir)
    machine = {
        "run_dir": str(run_dir), "repo_commit": repo_commit(), "kind": "blend",
        "inputs": man["inputs"], "luminance_from": man["luminance_from"], "notes": inp.notes,
        "mode": inp.mode, "mode_why": inp.mode_why, "channels": man["channels"],
        "options": {k: man["config"].get(k) for k in OPTION_KEYS if k in man["config"]},
        "blend": {"script": f"blend_run.js (deploy/{TEMPLATE_NB if inp.mode == 'nb' else TEMPLATE})",
                  "mode": inp.mode,
                  "ok": (pi or {}).get("ok"), "minutes": (pi or {}).get("minutes"),
                  "last_line": (pi or {}).get("last_line", ""),
                  "products": (st or {}).get("products"), "steps": st,
                  "channels_applied": (st or {}).get("channels"),
                  "log": str(out_dir / "blend.log")},
        "vivid": man.get("vivid"),
        "outputs": man["outputs"],
        "timing": {"started_utc": timing[0][1] if timing else "",
                   "finished_utc": timing[-1][2] if timing else "",
                   "minutes": round(sum(float(r[3]) for r in timing), 2) if timing else 0,
                   "stages": timing},
    }
    return L.Ledger(campaign=o.target, rig="piggyback", kind="blend", run=Path(run_dir).name,
                    machine=machine)


def write_ledger(led, run_dir: Path, blend_root: Path, scratch_blend_root: Path | None = None) -> Path:
    from photonscript.integration import ledger as writer
    return writer.write_for_run(led, run_dir, blend_root, scratch_root=scratch_blend_root)


# ----------------------------------------------------------------- run

def workspace(o: Options, inp: Inputs, run_dir: Path, echo=print) -> scratch.Workspace | None:
    """Fast scratch for a blend: <scratch>/Blend/<name>, archived to
    <staging_root>/Blend/<name>. Off for --out, --dry-run, --no-pixinsight."""
    if o.out or o.dry_run or not o.run_pixinsight or not o.scratch_root:
        return None
    if not scratch.usable(o.scratch_root, o.staging_root):
        echo(f"  scratch root {o.scratch_root} overlaps the staging root: not used")
        return None
    try:
        ws = scratch.Workspace.create(run_dir, o.scratch_root, o.staging_root, kind="blend",
                                      keep_staged=o.keep_work)
    except ValueError as e:
        raise BlendError(str(e)) from None
    for src in [inp.osc, *inp.rc16.values()]:
        if is_inside(src, ws.work_dir):
            raise BlendError(f"an input sits inside the blend folder {ws.work_dir}")
    return ws


def run(o: Options, echo=print) -> dict:
    """The blend, holding the PixInsight queue lock (pilock.py) when it
    runs PixInsight."""
    with pilock.job(f"blend {o.target}", enabled=o.run_pixinsight and not o.dry_run,
                    wait_min=o.lock_wait_min, echo=echo):
        return _run(o, echo)


def _run(o: Options, echo=print) -> dict:
    if not 0.0 <= o.weight <= 1.0:
        raise BlendError("--weight must be within 0..1")
    parse_mode(o.mode)
    timer = Timer(None, echo)                 # the file is set once the run folder exists
    with timer.stage("find the OSC and RC16 masters"):
        inp = discover(o)
        if inp.mode == "nb":
            inp.notes += [f"core: {n}" for n in core_items(o, inp)[1]]
        for n in inp.notes:
            echo("  " + n)
        echo(f"  OSC:  {inp.osc}")
        for f in sorted(inp.rc16, key=_filter_rank):
            echo(f"  RC16 {f}: {inp.rc16[f]}")
    run_dir = Path(o.out) if o.out else default_run_dir(o, mode=inp.mode)
    for src in [inp.osc, *inp.rc16.values()]:
        if is_inside(src, run_dir):
            raise BlendError(f"an input sits inside the blend folder {run_dir}: pick a NEW --out")
    result: dict = {"run_dir": str(run_dir), "dry_run": o.dry_run, "stage": inp.stage, "mode": inp.mode,
                    "channels": channel_plan(inp, core_items(o, inp)[0] if inp.mode == "nb" else None),
                    "osc": str(inp.osc), "rc16": {k: str(v) for k, v in inp.rc16.items()},
                    "luminance_from": inp.lum_filters(), "notes": inp.notes}
    if o.dry_run:
        cfg = config_for(o, inp, run_dir)
        problems = pjsr.check(render(cfg))
        result["config"] = cfg
        result["script_problems"] = problems
        echo(f"  would write {run_dir} (script check: {'ok' if not problems else '; '.join(problems)})")
        echo("dry run: nothing written, PixInsight not started")
        return result
    if run_dir.exists() and any(run_dir.iterdir()):
        raise BlendError(f"{run_dir} exists and is not empty: every blend gets a NEW folder "
                         "(pass another --out or leave it out)")
    ws = workspace(o, inp, run_dir, echo)
    if ws is None:
        return _run_in(o, inp, run_dir, None, timer, result, echo)
    ws.work_dir.mkdir(parents=True, exist_ok=True)
    ws.preflight(sum(Path(p).stat().st_size for p in [inp.osc, *inp.rc16.values()]), False, echo)
    try:
        res = _run_in(o, inp, ws.run_dir, ws, timer, result, echo)
    except Exception:
        ws.after_error(echo)
        raise
    return ws.finish(res, echo, ok=bool((res.get("blend") or {}).get("ok")))


def _run_in(o: Options, inp: Inputs, run_dir: Path, ws: scratch.Workspace | None, timer: Timer,
            result: dict, echo) -> dict:
    result["run_dir"] = str(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir = run_dir / "out"
    timer.path = out_dir / "timing.csv"

    with timer.stage("generate the PixInsight script"):
        cfg = config_for(o, inp, run_dir)
        block, missing = pjsr.solver_include(o.pixinsight)
        if missing:
            echo(f"  ImageSolver files missing ({', '.join(missing)}): no plate solve, "
                 "StarAlignment only (no WCS fallback)")
        js = pjsr.write(run_dir / "blend_run.js", render(cfg, block))
        echo(f"  {js.name}")
    result["script"] = str(js)

    pi = None
    if o.run_pixinsight:
        with timer.stage("PixInsight blend (launch to exit)"):
            pi = runner.run_script(js, out_dir / "blend.log", exe=o.pixinsight, echo=echo)
            echo(f"  blend: {'EXIT OK' if pi['ok'] else 'FAILED'} in {pi['minutes']} min ({pi['last_line']})")
        result["blend"] = pi
        want, why = vivid_wanted(o, inp)
        if pi.get("ok") and want:
            with timer.stage("PixInsight blend vivid (launch to exit)"):
                result["vivid"] = run_vivid(o, run_dir, echo)
        else:
            result["vivid"] = {"ok": None, "why": why if not want else "blend failed"}
            echo(f"  vivid: not made ({result['vivid']['why']})")
    else:
        echo("  PixInsight not started (--no-pixinsight). Run it later with:")
        echo(f'    "{o.pixinsight}" -n --automation-mode --run={js} --force-exit')

    with timer.stage("manifest + ledger"):
        man = build_manifest(o, inp, run_dir, cfg, pi, timer.rows)
        if result.get("vivid") is not None:
            man["vivid"] = result["vivid"]
        (run_dir / "manifest.json").write_text(json.dumps(man, indent=1, ensure_ascii=True, default=str),
                                               encoding="ascii")
        led = build_ledger(o, inp, run_dir, man, pi, timer.rows)
        blend_root = (ws.archive_dir if ws else run_dir).parent
        result["ledger"] = str(write_ledger(led, run_dir, blend_root,
                                            ws.work_dir.parent if ws and not ws.fell_back else None))
        result["ledger_version"] = led.version
    timer.flush()
    result["outputs"] = man["outputs"]
    result["timing"] = timer.rows
    echo(f"done: {run_dir}")
    return result
