"""PS-22: generate the PixInsight scripts for a run and check the PJSR rules.

The templates live in deploy/ (integrate_stack.js, finish_stack.js). A run
gets its own copy with the settings filled in as one `var CONFIG = {...};`
line (JSON, ASCII-escaped), so the template never needs per-run edits.

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
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
INTEGRATE_TEMPLATE = "integrate_stack.js"
FINISH_TEMPLATE = "finish_stack.js"
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
