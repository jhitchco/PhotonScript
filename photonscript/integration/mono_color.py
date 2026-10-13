"""Color images from the per-filter masters of a mono (RC16) run.

`photonscript integrate --rig rc16` makes co-registered linear masters per
filter (master_L / R / G / B, master_Ha / OIII / SII; master_<F>_hdr when a
filter has two lengths) and a finished mono image per filter, but no color
image: a two-rig target gets its color from the Piggy-600 (PS-153 blend),
an RC16-only target (M27, NGC 6535, NGC 2408, the RC16 Heart) got none.

This module plans and runs deploy/finish_mono_color.js (written into the run
folder as color_run.js) after the integration, the HDR combine and the
per-filter finish:

    R + G + B (+ L)   -> <Target>_LRGB (or <Target>_RGB without L)
    SII + Ha + OIII   -> <Target>_SHO (R = SII, G = Ha, B = OIII)
    Ha + OIII         -> <Target>_HOO (R = Ha, G = B = OIII)

each as out/final/<Name>.{xisf,tif,jpg} + <Name>_linear.xisf +
<Name>_steps.json, out/final/mono_color_steps.json for the whole combine,
out/color.log, out/timing_color.csv. The template reuses the finish_osc.js
functions between its PS_FINISH_LIB markers (crop, gradient, plate solve,
SPCC / BN + ColorCalibration, RC Astro BXT / NXT / SXT with their
fallbacks, the stretch), so the color image is finished exactly like an OSC
master.

Auto (integrate, `--color auto`): LRGB (RGB without L) when R, G and B
exist; SHO and HOO when Ha, OIII and SII exist, HOO alone when SII is
missing. A run with too few filters for any of them makes no color image
(a note says what is missing). Config integrate_mono_color (default on)
switches it in integrate; `photonscript finish --mono-run <run folder>
--color lrgb|rgb|sho|hoo|auto` runs it on an existing run, into a NEW
folder under <staging_root>/Finish.
"""

from __future__ import annotations

import json
from pathlib import Path

from photonscript.integration import pjsr, runner

TEMPLATE = "finish_mono_color.js"
SCRIPT = "color_run.js"
LOG = "color.log"
STEPS = "mono_color_steps.json"
LIB_MARK = "//__FINISH_LIB__"
LIB_BEGIN = "// ==== PS_FINISH_LIB_BEGIN"
LIB_END = "// ==== PS_FINISH_LIB_END"

COLORS = ("lrgb", "rgb", "sho", "hoo")
RGB_KINDS = ("lrgb", "rgb")
NB_KINDS = ("sho", "hoo")
LRGB_LIGHTNESS = 0.5      # LRGBCombination mL (0.5 = lightness as is)
LRGB_SATURATION = 0.45    # LRGBCombination mc (below 0.5 = a little more saturation)
SHO_SCNR = 0.8            # SCNR green on the stretched SHO, before the gold / blue hue curve
# SPCC curves for the RC16 mono filters: blank = the SPCC defaults (the
# brand of the RC16 L R G B filters is not recorded yet; the OSC finish's
# Sony color-sensor curves would be wrong here). QE: the IMX571 family.
MONO_SPCC = {"spcc_qe": "Sony IMX411/455/461/533/571", "spcc_red": "", "spcc_green": "", "spcc_blue": ""}

# filter names as they reach the masters (NINA single letters, long names)
ALIASES = {"L": "L", "LUM": "L", "LUMINANCE": "L", "R": "R", "RED": "R", "G": "G", "GREEN": "G",
           "B": "B", "BLUE": "B", "HA": "Ha", "H": "Ha", "HALPHA": "Ha", "OIII": "OIII", "O": "OIII",
           "O3": "OIII", "SII": "SII", "S": "SII", "S2": "SII"}


class MonoColorError(ValueError):
    pass


def canonical(filt: str) -> str | None:
    """L R G B Ha OIII SII for a master's filter name, else None."""
    return ALIASES.get(str(filt or "").strip().upper().replace("-", "").replace("_", ""))


def parse_colors(spec) -> list[str] | None:
    """--color: '' / auto = None (auto); else a comma list of lrgb rgb sho
    hoo (all = every one). Raises MonoColorError on anything else."""
    if spec is None:
        return None
    if isinstance(spec, (list, tuple)):
        parts = [str(x) for x in spec]
    else:
        parts = str(spec).split(",")
    parts = [p.strip().lower() for p in parts if p.strip()]
    if not parts or parts == ["auto"]:
        return None
    if parts == ["all"]:
        return list(COLORS)
    bad = [p for p in parts if p not in COLORS]
    if bad:
        raise MonoColorError(f"--color must be auto, all or a comma list of {', '.join(COLORS)} "
                             f"(got {', '.join(bad)})")
    return list(dict.fromkeys(parts))


def canonical_masters(masters: dict) -> dict[str, str]:
    """{filter: path} with canonical filter keys (unknown filters dropped,
    the first path of a filter kept)."""
    out: dict[str, str] = {}
    for f, p in masters.items():
        c = canonical(f)
        if c and c not in out:
            out[c] = pjsr.fwd(p)
    return out


def plan(masters: dict, prefix: str, colors: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """The color items of a run from its masters ({filter: path}, any
    filter spelling), outputs named <prefix>_<KIND>. colors None = auto.
    Returns (items, notes); a requested color whose filters are missing is
    left out with a note."""
    m = canonical_masters(masters)
    have = set(m)
    notes: list[str] = []
    rgb_ok = {"R", "G", "B"} <= have
    want = list(colors) if colors else []
    if not colors:
        if rgb_ok:
            want.append("lrgb" if "L" in have else "rgb")
        elif have & {"R", "G", "B"}:
            want.append("rgb")                        # left out below, with what is missing
        if {"Ha", "OIII"} <= have:
            want += ["sho", "hoo"] if "SII" in have else ["hoo"]
        elif have & {"Ha", "OIII"}:
            want.append("hoo")
    kinds: list[str] = []
    for k in want:
        if k in RGB_KINDS:
            if not rgb_ok:
                notes.append(f"{k.upper()} left out: needs R, G and B (missing "
                             + ", ".join(f for f in ("R", "G", "B") if f not in have) + ")")
                continue
            if k == "lrgb" and "L" not in have:
                notes.append("LRGB: no L master, making RGB instead")
                k = "rgb"
        elif k == "sho" and not {"Ha", "OIII", "SII"} <= have:
            notes.append("SHO left out: needs SII, Ha and OIII (missing "
                         + ", ".join(f for f in ("SII", "Ha", "OIII") if f not in have) + ")")
            continue
        elif k == "hoo" and not {"Ha", "OIII"} <= have:
            notes.append("HOO left out: needs Ha and OIII (missing "
                         + ", ".join(f for f in ("Ha", "OIII") if f not in have) + ")")
            continue
        if k not in kinds:
            kinds.append(k)
    kinds.sort(key=COLORS.index)
    if not kinds and not colors:
        notes.append("no color image: the run has " + (", ".join(sorted(have)) or "no filter masters")
                     + " (LRGB needs R, G, B; HOO needs Ha, OIII)")
    has_rgb = any(k in RGB_KINDS for k in kinds)
    has_nb = any(k in NB_KINDS for k in kinds)
    items, kept_stars = [], False
    for k in kinds:
        it: dict = {"kind": k, "name": f"{prefix}_{k.upper()}"}
        if k in RGB_KINDS:
            it["channels"] = {"R": m["R"], "G": m["G"], "B": m["B"]}
            it["L"] = m["L"] if k == "lrgb" else None
            it["keep_stars"] = has_nb and not kept_stars
            kept_stars = kept_stars or it["keep_stars"]
        elif k == "sho":
            it["channels"] = {"R": m["SII"], "G": m["Ha"], "B": m["OIII"]}
            it["ha_channel"] = 1
        else:
            it["channels"] = {"R": m["Ha"], "G": m["OIII"], "B": m["OIII"]}
            it["ha_channel"] = 0
        if k in NB_KINDS:
            it["stars_from"] = "rgb" if has_rgb else "ha"
        items.append(it)
    return items, notes


def expected_masters(stacks: list[dict], out_dir: Path, hdr_items: list[dict] | None = None,
                     hdr_made=None) -> dict[str, str]:
    """{filter: master path} a mono run will have before PixInsight ran:
    master_<F> of each filter's primary stack (named after the filter),
    master_<F>_hdr instead when that filter has an HDR item (made, when
    hdr_made is given)."""
    out_dir = Path(out_dir)
    hdr_by = {it["filter"]: it for it in (hdr_items or [])
              if hdr_made is None or it["name"] in hdr_made}
    out: dict[str, str] = {}
    for s in stacks:
        if s.get("mixed_exposures") or s["name"] != s["filter"]:
            continue
        it = hdr_by.get(s["filter"])
        out[s["filter"]] = it["output"] if it else pjsr.fwd(out_dir / "master" / f"master_{s['name']}.xisf")
    return out


def run_masters(run_dir: Path) -> dict[str, Path]:
    """{filter: master} on disk for a run folder (blend.masters: the HDR
    master preferred, exposure variants dropped)."""
    from photonscript.integration.blend import masters
    return masters(Path(run_dir))


# ----------------------------------------------------------------- render

def finish_library(deploy_dir: Path | None = None) -> str:
    """The finish_osc.js functions between its PS_FINISH_LIB markers."""
    t = pjsr.template(pjsr.FINISH_TEMPLATE, deploy_dir)
    a, b = t.find(LIB_BEGIN), t.find(LIB_END)
    if a < 0 or b < a:
        raise pjsr.PjsrError(f"{pjsr.FINISH_TEMPLATE}: no PS_FINISH_LIB_BEGIN / END markers")
    return t[a:b]


def config(out_dir: Path, items: list[dict], finish_cfg: dict, target: str) -> dict:
    """The CONFIG of color_run.js: the finish settings of the run
    (pjsr.FINISH_DEFAULTS updated with finish_cfg: coordinates, optics,
    gradient, RC Astro, GraXpert, pi_library) with the mono SPCC curves,
    plus the items."""
    c = dict(pjsr.FINISH_DEFAULTS)
    c.update({k: v for k, v in (finish_cfg or {}).items() if v is not None or k in ("ra_deg", "dec_deg")})
    c.update(MONO_SPCC)
    out_dir = Path(out_dir)
    rc = pjsr.rc_mode(c["rc_mode"]) if c.get("use_rc", True) else "off"
    keys = ("gradient", "bxt_stars", "bxt_nonstellar", "nxt_denoise", "denoise", "deconv",
            "deconv_strength", "graxpert", "graxpert_version", "graxpert_ai", "graxpert_gpu",
            "graxpert_timeout_min", "stars", "star_strength", "color", "spcc_qe", "spcc_red",
            "spcc_green", "spcc_blue", "spcc_white", "bg_target", "shadow_sigma", "scnr", "sat_mid",
            "hdr_layers", "focal_mm", "pixel_um", "pi_library")
    cfg = {k: c.get(k) for k in keys}
    for k in ("bxt_stars", "bxt_nonstellar", "nxt_denoise"):
        cfg[k] = pjsr.unit_value(k, cfg[k])
    ra, dec = c.get("ra_deg"), c.get("dec_deg")
    cfg.update(rc_mode=rc, ra_deg=None if ra is None or ra != ra else float(ra),
               dec_deg=None if dec is None or dec != dec else float(dec),
               focal_mm=float(cfg["focal_mm"]), pixel_um=float(cfg["pixel_um"]),
               pi_library=pjsr.fwd(cfg.get("pi_library") or ""), graxpert=pjsr.fwd(cfg.get("graxpert") or ""),
               out=pjsr.fwd(out_dir), final=pjsr.fwd(out_dir / "final"),
               staging=pjsr.fwd(c.get("staging") or out_dir.parent),
               steps_file=pjsr.fwd(out_dir / "final" / STEPS), target=target,
               lrgb_lightness=LRGB_LIGHTNESS, lrgb_saturation=LRGB_SATURATION, sho_scnr=SHO_SCNR,
               items=items)
    return cfg


def render(cfg: dict, solver_block: str = "", deploy_dir: Path | None = None) -> str:
    t = pjsr.template(TEMPLATE, deploy_dir)
    if LIB_MARK not in t:
        raise pjsr.PjsrError(f"{TEMPLATE} has no {LIB_MARK}")
    if not cfg.get("items"):
        raise pjsr.PjsrError("mono color: no items")
    t = t.replace(LIB_MARK, finish_library(deploy_dir))
    return pjsr.render(t, cfg, solver_block)


def write_script(run_dir: Path, cfg: dict, solver_block: str = "") -> Path:
    return pjsr.write(Path(run_dir) / SCRIPT, render(cfg, solver_block))


def read_steps(out_dir: Path) -> dict | None:
    p = Path(out_dir) / "final" / STEPS
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - not written (PixInsight failed early) or half-written
        return None


def outputs(out_dir: Path, items: list[dict]) -> list[str]:
    """The color files the items made (<Name>.{xisf,tif,jpg}, _linear, _steps)."""
    fin = Path(out_dir) / "final"
    found = []
    for it in items:
        for sfx in (".xisf", ".tif", ".jpg", "_linear.xisf", "_steps.json"):
            p = fin / f"{it['name']}{sfx}"
            if p.is_file():
                found.append(str(p))
    st = fin / STEPS
    if st.is_file():
        found.append(str(st))
    return found


def made(out_dir: Path, items: list[dict]) -> list[str]:
    fin = Path(out_dir) / "final"
    return [it["name"] for it in items if (fin / f"{it['name']}.jpg").is_file()
            or (fin / f"{it['name']}.xisf").is_file()]


def run(run_dir: Path, cfg: dict, *, solver_block: str = "", exe: str, echo=print) -> dict:
    """Write color_run.js and run it in PixInsight (one instance, the
    integrate runner). Returns {ok, made, items, outputs, steps, notes...}.
    An item that failed simply has no output; nothing here raises for it."""
    run_dir = Path(run_dir)
    out_dir = Path(cfg["out"])
    js = write_script(run_dir, cfg, solver_block)
    r = runner.run_script(js, out_dir / LOG, exe=exe, echo=echo)
    items = cfg["items"]
    got = made(out_dir, items)
    for it in items:
        echo(f"  color {it['kind'].upper()}: " + (f"{it['name']}.jpg" if it["name"] in got
                                                    else "not made (see color.log)"))
    return dict(r, made=got, items=items, steps=read_steps(out_dir), script=str(js),
                log=str(out_dir / LOG), outputs=outputs(out_dir, items))


# the color kinds in the order a run's "final" image prefers them
FINAL_ORDER = ("LRGB", "RGB", "SHO", "HOO")


def color_jpgs(out_dir: Path) -> list[Path]:
    """The color JPGs of a mono run, out/final/<prefix>_LRGB / _RGB / _SHO /
    _HOO.jpg, in FINAL_ORDER. The suffix is matched case-sensitively, so
    an OSC finish's <name>_hoo.jpg is not one of them."""
    fin = Path(out_dir) / "final"
    try:
        names = sorted(p.name for p in fin.iterdir() if p.is_file())
    except OSError:
        return []
    out: list[Path] = []
    for kind in FINAL_ORDER:
        out += [fin / n for n in names if n.endswith(f"_{kind}.jpg")]
    return out


def preferred_final(out_dir: Path, fallback: str = "") -> str:
    """The run's "final" image: the first color JPG (LRGB, RGB, SHO, HOO)
    when the color combine made one, else `fallback` (the per-filter
    finish's *_final.jpg)."""
    c = color_jpgs(out_dir)
    return str(c[0]) if c else fallback


def processing_text(items: list[dict], made_names: list[str] | None = None) -> str:
    """AstroBin packet sentence for the color images actually made (every
    item when made_names is None); '' when none."""
    made_set = None if made_names is None else set(made_names)
    parts = []
    for it in items or []:
        if made_set is not None and it.get("name") not in made_set:
            continue
        k = it.get("kind")
        if k == "lrgb":
            parts.append(f"{it['name']}: R, G and B combined with ChannelCombination, "
                         "L applied with LRGBCombination")
        elif k == "rgb":
            parts.append(f"{it['name']}: R, G and B combined with ChannelCombination")
        elif k == "sho":
            parts.append(f"{it['name']}: SHO mapping (R = SII, G = Ha, B = OIII) with "
                         "ChannelCombination, SCNR green and a gold / blue hue curve")
        elif k == "hoo":
            parts.append(f"{it['name']}: HOO mapping (R = Ha, G = B = OIII) with ChannelCombination")
    if not parts:
        return ""
    return ("Color from the co-registered mono masters, each finished like the masters "
            "(gradient, color calibration, stretch): " + "; ".join(parts) + ".")


def ledger_block(res: dict | None, notes: list[str] | None = None) -> dict | None:
    """The ledger's color block (None when no combine was planned)."""
    if not res:
        return None
    return {"script": f"{SCRIPT} (deploy/{TEMPLATE})", "ok": res.get("ok"),
            "minutes": res.get("minutes"), "made": res.get("made") or [],
            "items": [{k: it.get(k) for k in ("kind", "name", "channels", "L", "stars_from")}
                      for it in res.get("items") or []],
            "outputs": res.get("outputs") or [], "steps": res.get("steps"),
            "notes": list(notes or res.get("notes") or []), "log": res.get("log", "")}
