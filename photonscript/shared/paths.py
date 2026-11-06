"""Path settings: "~" / env-var expansion, the image save-root guard and
Windows-safe folder names (PS-237).

Between 2026-09-26 and 10-09 the Librarian's legacy per-sub copy wrote 438
FITS to C:\\astro\\PhotonScript\\~\\Astrophotography: the scope's .env had
PS_LOCAL_IMAGE_DIR="~\\Astrophotography" (as config/example.env shows) and
Path("~\\...") never expands, so it resolved against the service's working
folder, the repo. Every path setting is now expanded once at config load
(expand_path), and a root that is still relative, still holds a "~"
component or sits inside the checkout is refused (save_root_problem).

No imports from photonscript.shared.config: config imports this module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# The checkout this package runs from (photonscript/shared/paths.py -> repo).
REPO_ROOT = Path(__file__).resolve().parents[2]

# Settings whose name ends like this hold a filesystem path (data_dir,
# library_dir, local_image_dir, stacking_output_dir, transfer_key_path,
# integration_staging_root, qa_score_weights_file, ...).
PATH_SUFFIXES = ("_dir", "_path", "_root", "_file")

_PCT_VAR = re.compile(r"%([^%\s]+)%")


def is_path_setting(name: str) -> bool:
    """True for a config field that holds a filesystem path."""
    return str(name or "").endswith(PATH_SUFFIXES)


def expand_path(value) -> str:
    """A path setting with "~" (either separator, any OS) and %VARS% /
    $VARS expanded. Blank stays blank (callers pick their own default);
    an unknown variable is left as written (save_root_problem catches what
    stays relative)."""
    text = str(value if value is not None else "").strip()
    if not text:
        return text
    if text == "~" or text[:2] in ("~/", "~\\"):
        text = str(Path.home()) + text[1:]
    # %VAR% on every OS (os.path.expandvars only knows it on Windows)
    text = _PCT_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), text)
    return os.path.expandvars(os.path.expanduser(text))


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def save_root_problem(value, repo_root: Path | None = None) -> str | None:
    """Why `value` must not be used as an image save root, or None when it
    is fine. Refused: a relative path (it lands in whatever folder the
    process runs from), a "~" component that was never expanded, and any
    folder inside the PhotonScript checkout. Blank is not judged here."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    parts = re.split(r"[\\/]+", text)
    if "~" in parts:
        return f'"{text}" holds an unexpanded "~"'
    p = Path(text)
    # C:\x is absolute on Windows; on POSIX a drive path counts as absolute
    # too, so a scope .env read on the desktop is not refused for its drive
    if not (p.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", text)):
        return f'"{text}" is a relative path'
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    if p.is_absolute() and _inside(p, root):
        return f'"{text}" is inside the PhotonScript checkout ({root})'
    return None


# Windows refuses these in a path component (WinError 123), plus control
# characters; a trailing dot or space is silently dropped by Win32.
_ILLEGAL = set('<>:"/\\|?*')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
UNKNOWN_DIR = "_unknown"


def safe_component(name) -> str:
    """One Windows-safe path component. Illegal characters ("/" and "\\"
    included, so "TPoint point 01/45 ..." stays one folder) become "_",
    trailing dots / spaces go, reserved device names get a "_" prefix, and
    a name with nothing left (or the "?" an unattributed sub carries)
    becomes UNKNOWN_DIR. "." and ".." can never come out of it."""
    raw = str(name or "").strip()
    if not raw or set(raw) <= {"?"}:
        return UNKNOWN_DIR
    out = "".join("_" if (c in _ILLEGAL or ord(c) < 32) else c for c in raw)
    out = out.rstrip(" .")
    if not out.strip("_ ."):
        return UNKNOWN_DIR
    if out.split(".")[0].upper() in _RESERVED:
        out = "_" + out
    return out


def image_save_roots(config) -> dict[str, str]:
    """{label: path} of every folder PhotonScript (or NINA, for the watch
    dirs) saves image files under, as configured. Blank settings that fall
    back to a default under data_dir are given as that default."""
    data = str(getattr(config, "data_dir", "") or "")
    lib = str(getattr(config, "library_dir", "") or "") or (
        str(Path(data) / "Library") if data else "")
    out = {
        "PS_LOCAL_IMAGE_DIR": getattr(config, "local_image_dir", ""),
        "PS_STACKING_OUTPUT_DIR": getattr(config, "stacking_output_dir", ""),
        "PS_DATA_DIR": data,
        "PS_LIBRARY_DIR": lib,
        "PS_PIGGYBACK_LIBRARY_DIR": getattr(config, "piggyback_library_dir", ""),
        "PS_LIBRARY_ARCHIVE_DIR": getattr(config, "library_archive_dir", ""),
        "PS_IMAGE_WATCH_DIR": getattr(config, "image_watch_dir", ""),
        "PS_PIGGYBACK_IMAGE_WATCH_DIR": getattr(config,
                                                "piggyback_image_watch_dir", ""),
    }
    return {k: str(v or "") for k, v in out.items() if str(v or "").strip()}


def image_root_problems(config, repo_root: Path | None = None) -> dict[str, str]:
    """{setting: reason} for every image save root that save_root_problem
    refuses (empty when all are fine)."""
    out = {}
    for label, value in image_save_roots(config).items():
        why = save_root_problem(value, repo_root)
        if why:
            out[label] = why
    return out


async def refuse_save_root(config, setting: str, why: str, who: str) -> None:
    """Log and alert (Pushover act) that `who` will not save under the
    root in `setting`. Never raises: a failed push must not stop the
    caller from starting without that root."""
    import logging
    msg = (f"{who} will not save images: {setting} {why}. Set it to an "
           "absolute folder outside the PhotonScript checkout in .env and "
           "restart.")
    logging.getLogger(__name__).error("Image save root refused: %s", msg)
    try:
        from photonscript.shared.pushover import notify
        await notify(config, msg, title="Image save path refused", kind="act",
                     fact=f"save-root-refused:{setting}")
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("save-root alert not sent: %s", e)
