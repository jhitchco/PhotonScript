"""PS-22: generate the PixInsight scripts for a run and check the PJSR rules.

The templates live in deploy/. integrate_stack.js gets its settings as one
`var CONFIG = {...};` line (JSON, ASCII-escaped), so the template never needs
per-run edits. The finish is deploy/finish_osc.js, the ONE finish
implementation (PS-46 SPCC with Gaia, PS-41 noise reduction + deconvolution,
PS-40 StarNet2 star reduction, a steps json per master): render_finish()
fills the same __PLACEHOLDERS__ run-finish-osc.ps1 fills, with MASTERS set to
every master of the run.

check() enforces the rules that each cost a debugging session (HANDBOOK
sec 6 plus the 2026-09-26 / 10-04 runs):
  * pure ASCII, no BOM (PowerShell's UTF8 BOM breaks the parser);
  * no slash-star inside a line comment (the preprocessor opens a block);
  * pjsr/ headers before any quoted include (the AdP solver files need
    DataType_Double at load time);
  * the astrometric solution is cleared before every Crop (else a Yes/No
    dialog stalls an unattended run);
  * no placeholder left unfilled.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
INTEGRATE_TEMPLATE = "integrate_stack.js"
FINISH_TEMPLATE = "finish_osc.js"
CONFIG_MARK = "//__CONFIG__"
SOLVER_MARK = "//__SOLVER_INCLUDE__"
SOLVER_DEPS = ("WCSmetadata.jsh", "AstronomicalCatalogs.jsh", "SearchCoordinatesDialog.js",
               "CatalogDownloader.js", "ImageSolver.js")

_PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")


class PjsrError(ValueError):
    pass


def template(name: str, deploy_dir: Path | None = None) -> str:
    return (Path(deploy_dir or DEPLOY) / name).read_bytes().decode("ascii")


def _strip_strings(line: str) -> str:
    """The line with string literal contents blanked (keeps the quotes)."""
    out, q, esc = [], None, False
    for ch in line:
        if q:
            if esc:
                esc = False
                out.append(" ")
            elif ch == "\\":
                esc = True
                out.append(" ")
            elif ch == q:
                q = None
                out.append(ch)
            else:
                out.append(" ")
        else:
            if ch in ("'", '"'):
                q = ch
            out.append(ch)
    return "".join(out)


def _line_comment(line: str) -> str | None:
    code = _strip_strings(line)
    i = code.find("//")
    return None if i < 0 else line[i + 2:]


def check(text: str) -> list[str]:
    """Problems with a PJSR script (empty list = fine)."""
    problems = []
    if text.startswith(chr(0xFEFF)):
        problems.append("starts with a BOM")
    bad = sorted({c for c in text if ord(c) > 127})
    if bad:
        problems.append("non-ASCII characters: " + " ".join(f"U+{ord(c):04X}" for c in bad))
    lines = text.splitlines()
    last_pjsr, first_quoted = -1, None
    for n, line in enumerate(lines, 1):
        c = _line_comment(line)
        if c is not None and "/*" in c:
            problems.append(f"line {n}: slash-star inside a line comment")
        s = line.strip()
        if s.startswith("#include <pjsr/"):
            last_pjsr = n
        elif s.startswith('#include "') and first_quoted is None:
            first_quoted = n
    if first_quoted is not None and last_pjsr > first_quoted:
        problems.append(f"pjsr header on line {last_pjsr} after the quoted include on line {first_quoted}")
    for m in re.finditer(r"new\s+Crop\b", text):
        fn = text.rfind("function ", 0, m.start())
        body = text[fn if fn >= 0 else 0:m.start()]
        if "clearWcs(" not in body and "clearAstrometricSolution" not in body:
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"line {line_no}: Crop without clearing the astrometric solution first")
    code = "\n".join(l for l in lines if not l.lstrip().startswith("//"))
    left = sorted(set(_PLACEHOLDER.findall(code)))
    if left:
        problems.append("unfilled placeholders: " + ", ".join(left))
    if CONFIG_MARK in text:
        problems.append("CONFIG not filled")
    return problems


def config_line(cfg: dict) -> str:
    return "var CONFIG = " + json.dumps(cfg, ensure_ascii=True, sort_keys=True) + ";"


def solver_include(pixinsight_exe: str | Path) -> tuple[str, list[str]]:
    """The #define / #include block for PixInsight's ImageSolver in library
    mode (mirrors run-finish-osc.ps1). Returns (block, missing files); the
    block is '' when any file is missing (the finish then skips the solve)."""
    root = Path(pixinsight_exe).parent.parent
    adp = root / "src" / "scripts" / "AdP"
    missing = [d for d in SOLVER_DEPS if not (adp / d).exists()]
    if missing:
        return "", missing
    fwd = str(adp).replace("\\", "/")
    lines = ['#define USE_SOLVER_LIBRARY true',
             '#define TITLE "PhotonScript Finish"',
             '#define SETTINGS_MODULE "SOLVER"',
             '#define STAR_CSV_FILE (File.systemTempDirectory + format( "/stars-%03d.csv", '
             'CoreApplication.instance ))']
    lines += [f'#include "{fwd}/{d}"' for d in SOLVER_DEPS]
    return "\n".join(lines), []


def render(template_text: str, cfg: dict, solver_block: str = "") -> str:
    if CONFIG_MARK not in template_text:
        raise PjsrError("template has no " + CONFIG_MARK)
    out = template_text.replace(CONFIG_MARK, config_line(cfg))
    out = out.replace(SOLVER_MARK, solver_block)
    return out


def write(path: Path, text: str) -> Path:
    """Check, then write ASCII with no BOM. Raises PjsrError on a problem."""
    problems = check(text)
    if problems:
        raise PjsrError(f"{Path(path).name}: " + "; ".join(problems))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(text.encode("ascii"))
    return Path(path)


def fwd(p) -> str:
    """Forward-slash path for PJSR."""
    return str(p).replace("\\", "/")


# ------------------------------------------------------------------ finish

# The run-finish-osc.ps1 parameter defaults (the M31_OSC4 v4b look, PS-46 /
# PS-41 / PS-40). tests/test_integration/test_pjsr.py checks they agree.
FINISH_DEFAULTS = {
    "gradient": "auto", "use_rc": True, "color": "auto",
    "spcc_qe": "Sony IMX411/455/461/533/571",
    "spcc_red": "Sony Color Sensor R-UVIRcut",
    "spcc_green": "Sony Color Sensor G-UVIRcut",
    "spcc_blue": "Sony Color Sensor B-UVIRcut",
    "spcc_white": "Average Spiral Galaxy",
    "bg_target": 0.12, "shadow_sigma": 2.0, "scnr": 0.60, "sat_mid": 0.64, "hdr_layers": 7,
    "frame": None, "denoise": 0.5, "deconv": "on", "deconv_strength": 0.5,
    "graxpert": "", "graxpert_version": "", "graxpert_ai": "", "graxpert_gpu": "",
    "graxpert_timeout_min": 30, "stars": "on", "star_strength": 0.7,
    "hoo": "off",                  # PS-161: on = also <name>_hoo.{xisf,jpg} (OSC only)
    "focal_mm": 600.0, "pixel_um": 3.76,
}


def find_graxpert() -> str:
    """GraXpert executable the way run-finish-osc.ps1 looks for it:
    PS_GRAXPERT, then the usual install paths. "" = not found."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    la = os.environ.get("LOCALAPPDATA", "")
    home = os.environ.get("USERPROFILE", "")
    cands = [os.environ.get("PS_GRAXPERT", ""),
             os.path.join(pf, "GraXpert", "GraXpert-win64.exe"), os.path.join(pf, "GraXpert", "GraXpert.exe")]
    if la:
        cands += [os.path.join(la, "Programs", "GraXpert", "GraXpert.exe"),
                  os.path.join(la, "Programs", "GraXpert", "GraXpert-win64.exe")]
    if home:
        cands += [os.path.join(home, "GraXpert", "GraXpert-win64.exe"),
                  os.path.join(home, "GraXpert", "GraXpert.exe")]
    for c in cands:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    return ""


def _js_str(v) -> str:
    """Contents for a "..." JS literal: forward slashes, ASCII-escaped."""
    return json.dumps(str(v if v is not None else "").replace("\\", "/"), ensure_ascii=True)[1:-1]


def _js_num(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "NaN"
    return repr(float(v)) if isinstance(v, float) else str(int(v))


def render_finish(cfg: dict, solver_block: str = "", deploy_dir: Path | None = None) -> str:
    """deploy/finish_osc.js with every placeholder filled for a run.
    cfg: out (run out dir; finals go to out/final, finish.log to out),
    masters [{name, path}], ra_deg / dec_deg (None = unknown), pi_library,
    plus any FINISH_DEFAULTS key to override."""
    c = dict(FINISH_DEFAULTS)
    c.update({k: v for k, v in cfg.items() if v is not None or k in ("ra_deg", "dec_deg")})
    masters = list(c.get("masters") or [])
    if not masters:
        raise PjsrError("render_finish: no masters")
    out = str(c["out"]).replace("\\", "/")
    frame = c.get("frame")
    vals = {
        "STAGING": _js_str(c.get("staging", out)), "NAME": _js_str(masters[0]["name"]),
        "MASTER": _js_str(masters[0]["path"]), "FINAL": _js_str(out + "/final"),
        "LOGDIR": _js_str(out), "PI_LIBRARY": _js_str(c.get("pi_library", "")),
        "RA": _js_num(c.get("ra_deg")), "DEC": _js_num(c.get("dec_deg")),
        "FOCAL": _js_num(float(c["focal_mm"])), "PIXEL": _js_num(float(c["pixel_um"])),
        "GRADIENT": _js_str(c["gradient"]), "USE_RC": "true" if c["use_rc"] else "false",
        "COLOR": _js_str(c["color"]),
        "SPCC_QE": _js_str(c["spcc_qe"]), "SPCC_RED": _js_str(c["spcc_red"]),
        "SPCC_GREEN": _js_str(c["spcc_green"]), "SPCC_BLUE": _js_str(c["spcc_blue"]),
        "SPCC_WHITE": _js_str(c["spcc_white"]),
        "BG_TARGET": _js_num(float(c["bg_target"])), "SHADOW_SIGMA": _js_num(float(c["shadow_sigma"])),
        "SCNR": _js_num(float(c["scnr"])), "SAT_MID": _js_num(float(c["sat_mid"])),
        "HDR_LAYERS": str(int(c["hdr_layers"])),
        "FRAME": "null" if not frame else json.dumps(
            {k: float(frame[k]) for k in ("left", "top", "right", "bottom")}),
        "DENOISE": _js_num(float(c["denoise"])), "DECONV": _js_str(c["deconv"]),
        "DECONV_STRENGTH": _js_num(float(c["deconv_strength"])),
        "GRAXPERT": _js_str(c["graxpert"]), "GRAXPERT_VERSION": _js_str(c["graxpert_version"]),
        "GRAXPERT_AI": _js_str(c["graxpert_ai"]), "GRAXPERT_GPU": _js_str(c["graxpert_gpu"]),
        "GRAXPERT_TIMEOUT_MIN": str(int(c["graxpert_timeout_min"])),
        "STARS": _js_str(c["stars"]), "STAR_STRENGTH": _js_num(float(c["star_strength"])),
        "HOO": "on" if str(c.get("hoo") or "off").lower() in ("on", "true", "1") else "off",
        "MASTERS": json.dumps([{"name": m["name"], "path": str(m["path"]).replace("\\", "/")}
                               for m in masters], ensure_ascii=True),
    }
    text = template(FINISH_TEMPLATE, deploy_dir).replace(SOLVER_MARK, solver_block)
    lines = []
    for line in text.split("\n"):
        if not line.lstrip().startswith("//"):
            line = _PLACEHOLDER.sub(lambda m: vals.get(m.group(0)[2:-2], m.group(0)), line)
        lines.append(line)
    return "\n".join(lines)
