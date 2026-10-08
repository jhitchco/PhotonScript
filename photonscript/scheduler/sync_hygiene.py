"""PS-43: what is the Syncthing backlog made of, and why is it stuck?

Observe only. Nothing here changes Syncthing's config, ignore patterns or
files: it reads three Syncthing REST endpoints on the scope PC and reports.

- Census: pages through /rest/db/remoteneed for the WHOLE backlog (no 20,000
  cap, unlike the PS-75 name walk) but keeps only per-folder counts and bytes,
  never the file list, so a 45k-file walk costs a few KB of memory. It runs in
  a background thread at most every _CENSUS_PERIOD_S, sleeps between pages
  (rate limit), never while the armer is active, and never on a request.
- Classification: each top-level folder of the share is "astro" (Library,
  NINA, PHD2, calibration, sequences...) or "other" (OneDrive, Documents,
  AppData, anything unknown), by the rules in classify_folder().
- Stall diagnosis: /rest/folder/errors (scan + pull errors, e.g. OneDrive
  cloud placeholders Syncthing cannot read) and /rest/db/status (folder state
  and error), refreshed every _ERRORS_PERIOD_S.
- Ignore suggestion: the exact Syncthing ignore patterns that would stop the
  non-astronomy folders from syncing, as TEXT for Jeremy to review and paste
  himself. Nothing applies them.

Only names and sizes Syncthing's API already exposes are used; file contents
are never read (the backlog includes personal documents).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_CENSUS_PERPAGE = 1000
_CENSUS_MAX_PAGES = 500          # 500k files: a safety stop, not a cap in practice
_CENSUS_PAGE_PAUSE_S = 0.25      # rate limit between pages
_CENSUS_PAGE_TIMEOUT_S = 60
_CENSUS_PERIOD_S = 3600          # a full census at most hourly
_CENSUS_FAIL_BACKOFF_S = 900
_ERRORS_PERIOD_S = 300           # folder errors + status: cheap, every 5 min
_ERRORS_PERPAGE = 200
_ERROR_SAMPLES = 25
_SKIP_WHILE_ARMED = True

_cache: dict = {
    "census": None,        # last complete-or-stopped census (see _walk_census)
    "census_t": 0.0,
    "census_fail_t": 0.0,
    "census_error": "",
    "errors": None,        # summarize_errors() output
    "status": None,        # trimmed /rest/db/status
    "errors_t": 0.0,
    "errors_error": "",
}
_lock = threading.Lock()

# --- classification ----------------------------------------------------------

# Top-level folder names (lower case) that hold astronomy data.
ASTRO_NAMES = {
    "library", "nina", "ninashare", "phd2", "phd", "calibration", "cal",
    "calib", "darks", "dark", "flats", "flat", "bias", "biases", "lights",
    "light", "sequences", "sequence", "sequencer", "targets", "profiles",
    "images", "capture", "captures", "fits", "subs", "piggyback", "mosaics",
    "mosaic", "_analysis", "analysis", "logs", "photonscript", "astro",
    "astrophotography", "pixinsight", "masters", "stacks", "integration",
    "_rejected", "skyflats", "plate solves", "platesolve", "astap",
}
# Substrings that mark an astronomy folder even with an unusual name.
ASTRO_HINTS = ("nina", "phd2", "calib", "sequence", "flat", "dark", "bias",
               "light", "fits", "astro", "library", "piggy", "mosaic")
# Known personal / Windows profile folders: never astronomy data.
PERSONAL_NAMES = {
    "onedrive", "documents", "my documents", "desktop", "appdata",
    "downloads", "pictures", "my pictures", "music", "videos", "favorites",
    "contacts", "links", "saved games", "searches", "3d objects",
    "application data", "local settings", "temp", "dropbox", "google drive",
    "icloud drive", "recent",
}
ASTRO_EXTS = (".fits", ".fit", ".fts", ".xisf", ".json", ".csv", ".log",
              ".txt", ".ser", ".cr2", ".nef", ".tif", ".tiff")
ROOT_FILES = "(root files)"


def classify_folder(name: str) -> tuple[str, str]:
    """("astro" | "other", reason) for one top-level folder of the share."""
    n = (name or "").strip()
    low = n.lower()
    if n == ROOT_FILES:
        return "astro", "files at the share root"
    if low in PERSONAL_NAMES or low.startswith("onedrive"):
        why = ("OneDrive folder (personal files, likely cloud placeholders)"
               if low.startswith("onedrive") else "Windows profile folder "
               "(personal files)")
        return "other", why
    if low in ASTRO_NAMES:
        return "astro", "known astronomy folder"
    if any(h in low for h in ASTRO_HINTS):
        return "astro", "name looks like astronomy data"
    return "other", "unknown folder (not a known astronomy folder)"


def classify_root_file(name: str) -> str:
    return "astro" if name.lower().endswith(ASTRO_EXTS) else "other"


def split_path(name: str) -> tuple[str, str]:
    """(top-level folder, two-level folder) for a remoteneed entry name."""
    parts = [p for p in (name or "").replace("\\", "/").split("/") if p]
    if len(parts) <= 1:
        return ROOT_FILES, ROOT_FILES
    top = parts[0]
    sub = "/".join(parts[:2]) if len(parts) > 2 else top
    return top, sub


def add_entry(agg: dict, name: str, size: int) -> None:
    """Fold one remoteneed entry into the census aggregate."""
    top, sub = split_path(name)
    t = agg.setdefault(top, {"files": 0, "bytes": 0, "subs": {}})
    t["files"] += 1
    t["bytes"] += size
    if sub != top:
        s = t["subs"].setdefault(sub, [0, 0])
        s[0] += 1
        s[1] += size


# --- ignore patterns ---------------------------------------------------------

_GLOB_CHARS = set("*?[]{}\\|!#")


def ignore_pattern(folder: str) -> str:
    """Syncthing ignore pattern for one top-level folder: "/<name>" matches
    that folder at the share root only (never a same-named folder deeper)."""
    return "/" + folder


def ignore_suggestion(folders: list[dict], folder_id: str = "",
                      folder_path: str = "") -> dict:
    """Generate (never apply) ignore patterns for the "other" folders.

    folders: census rows ({"folder", "class", "files", "bytes"}). Astro
    folders and the root-files bucket are never suggested."""
    rows = [f for f in folders
            if f.get("class") == "other" and f.get("folder") != ROOT_FILES]
    patterns, review = [], []
    for f in rows:
        p = ignore_pattern(f["folder"])
        patterns.append(p)
        if any(c in _GLOB_CHARS for c in f["folder"]) or f["folder"].startswith("("):
            review.append(f"{p}: the name has characters Syncthing treats as "
                          "pattern syntax; check it matches only this folder")
    header = [
        "// PhotonScript PS-43 suggestion: non-astronomy folders in the share.",
        "// Review, then paste into Syncthing on the SCOPE PC:",
        "//   Folders > " + (folder_id or "NINAShare") + " > Edit > "
        "Ignore Patterns, one pattern per line, Save.",
        "// Ignoring does not delete anything (scope or desktop); the files",
        "// just stop being offered to the desktop.",
    ]
    text = "\n".join(header + patterns) + "\n" if patterns else ""
    return {
        "applied": False,
        "patterns": patterns,
        "text": text,
        "files": sum(int(f.get("files", 0)) for f in rows),
        "bytes": sum(int(f.get("bytes", 0)) for f in rows),
        "review": review,
        "where": ("Syncthing web UI on the scope PC (http://localhost:8384 on "
                  "that machine): Folders > " + (folder_id or "NINAShare") +
                  " > Edit > Ignore Patterns"),
        "folder_path": folder_path,
        "note": ("Ignoring does not delete anything. Files already on the "
                 "desktop stay there; the scope keeps its copies. Syncthing "
                 "simply stops offering the ignored paths, so they drop out "
                 "of the backlog. PhotonScript never applies this itself."),
    }


# --- folder errors -----------------------------------------------------------

_PLACEHOLDER_RE = re.compile(
    r"cloud (file )?(provider|operation|sync)|0x8007016a|0x80070185|"
    r"0x8007017c|cloud_file|files[- ]on[- ]demand|reparse point|"
    r"onedrive|offline|not available locally|the tag present in the reparse",
    re.I)
_PERMISSION_RE = re.compile(r"access is denied|permission denied|"
                            r"being used by another process|locked", re.I)
_SPACE_RE = re.compile(r"no space|disk full|insufficient (disk )?space", re.I)
_PATH_RE = re.compile(r"file name too long|path too long|invalid (file )?name",
                      re.I)


def error_kind(msg: str, path: str = "") -> str:
    m = msg or ""
    if _PLACEHOLDER_RE.search(m):
        return "cloud_placeholder"
    if _PERMISSION_RE.search(m):
        return "permission"
    if _SPACE_RE.search(m):
        return "disk_space"
    if _PATH_RE.search(m):
        return "path"
    return "other"


ERROR_KIND_TEXT = {
    "cloud_placeholder": ("OneDrive cloud placeholder (files-on-demand): the "
                          "file is not on the disk, so Syncthing cannot read "
                          "it"),
    "permission": "access denied or file locked",
    "disk_space": "out of disk space",
    "path": "path or file name problem",
    "other": "other error",
}


def summarize_errors(errors: list) -> dict:
    """Group /rest/folder/errors entries by kind and top-level folder."""
    by_kind: dict[str, int] = {}
    by_folder: dict[str, int] = {}
    samples = []
    for e in errors or []:
        if not isinstance(e, dict):
            continue
        path = str(e.get("path", ""))
        msg = str(e.get("error", ""))
        kind = error_kind(msg, path)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        top, _ = split_path(path)
        by_folder[top] = by_folder.get(top, 0) + 1
        if len(samples) < _ERROR_SAMPLES:
            samples.append({"path": path, "error": msg[:300], "kind": kind})
    return {
        "count": sum(by_kind.values()),
        "by_kind": [{"kind": k, "count": v, "text": ERROR_KIND_TEXT[k]}
                    for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])],
        "by_folder": [{"folder": k, "count": v,
                       "class": classify_folder(k)[0]}
                      for k, v in sorted(by_folder.items(),
                                         key=lambda kv: -kv[1])],
        "samples": samples,
    }


# --- Syncthing calls (run in the background thread only) -------------------

def _walk_census(settings, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Page through remoteneed until it runs dry (or _CENSUS_MAX_PAGES).
    Returns the aggregate; raises on an HTTP / parse failure."""
    import httpx
    url, key, folder, device = settings
    t0 = time.monotonic()
    agg: dict = {}
    listed = 0
    pages = 0
    complete = False
    with httpx.Client(timeout=_CENSUS_PAGE_TIMEOUT_S,
                      headers={"X-API-Key": key}) as cl:
        for page in range(1, _CENSUS_MAX_PAGES + 1):
            pages = page
            r = cl.get(url.rstrip("/") + "/rest/db/remoteneed",
                       params={"folder": folder, "device": device,
                               "page": page, "perpage": _CENSUS_PERPAGE})
            d = r.json()
            batch = d.get("files") if isinstance(d, dict) else None
            if not isinstance(batch, list):
                batch = []
            for f in batch:
                if isinstance(f, dict):
                    add_entry(agg, str(f.get("name", "")),
                              int(f.get("size", 0) or 0))
                else:
                    add_entry(agg, str(f), 0)
            listed += len(batch)
            n = len(batch)
            del d, batch
            if n < _CENSUS_PERPAGE:
                complete = True
                break
            sleep(_CENSUS_PAGE_PAUSE_S)
    return {"folders": agg, "listed_files": listed, "pages": pages,
            "complete": complete, "took_s": round(time.monotonic() - t0, 1),
            "t": time.time()}


def _fetch_errors(settings) -> tuple[dict, dict]:
    """(summarize_errors(...), trimmed db status). Raises on failure."""
    import httpx
    url, key, folder, _device = settings
    base = url.rstrip("/")
    with httpx.Client(timeout=15, headers={"X-API-Key": key}) as cl:
        r = cl.get(base + "/rest/folder/errors",
                   params={"folder": folder, "page": 1,
                           "perpage": _ERRORS_PERPAGE})
        d = r.json() if r is not None else {}
        errs = (d or {}).get("errors") or []
        status = {}
        try:
            s = cl.get(base + "/rest/db/status", params={"folder": folder}).json()
            keep = ("state", "error", "watchError", "pullErrors", "errors",
                    "needFiles", "needBytes", "localFiles", "globalFiles",
                    "stateChanged")
            status = {k: s.get(k) for k in keep if k in s}
        except Exception:  # noqa: BLE001
            status = {}
        try:
            fc = cl.get(base + "/rest/config/folders/" + folder).json()
            status["path"] = str(fc.get("path", "") or "")
            status["label"] = str(fc.get("label", "") or "")
        except Exception:  # noqa: BLE001
            pass
    summary = summarize_errors(errs)
    # The errors endpoint is paged; the status count is the true total.
    total = status.get("pullErrors")
    if isinstance(status.get("errors"), int):
        total = status["errors"]
    if isinstance(total, int) and total > summary["count"]:
        summary["total"] = total
    else:
        summary["total"] = summary["count"]
    return summary, status


# --- cadence + background refresh -----------------------------------------

def census_due(cache: dict, now: float, armed: bool) -> bool:
    if armed and _SKIP_WHILE_ARMED:
        return False
    if now - cache.get("census_fail_t", 0.0) < _CENSUS_FAIL_BACKOFF_S:
        return False
    if cache.get("census") is None:
        return True
    return now - cache.get("census_t", 0.0) >= _CENSUS_PERIOD_S


def errors_due(cache: dict, now: float, armed: bool) -> bool:
    if armed and _SKIP_WHILE_ARMED:
        return False
    return now - cache.get("errors_t", 0.0) >= _ERRORS_PERIOD_S


def refresh(settings, armed: bool = False, now: Optional[float] = None,
            sleep: Callable[[float], None] = time.sleep) -> None:
    """Refresh-thread body: errors/status when due, then the census when
    due. Each part records its own failure and never raises."""
    now = time.time() if now is None else now
    c = _cache
    if errors_due(c, now, armed):
        try:
            c["errors"], c["status"] = _fetch_errors(settings)
            c["errors_error"] = ""
        except Exception as e:  # noqa: BLE001
            c["errors_error"] = f"{type(e).__name__}: {e}"
        c["errors_t"] = now
    if census_due(c, now, armed):
        try:
            res = _walk_census(settings, sleep=sleep)
            c.update(census=res, census_t=now, census_error="",
                     census_fail_t=0.0)
            logger.info("Syncthing backlog census: %d files in %d folder(s), "
                        "%d page(s), %.1f s%s", res["listed_files"],
                        len(res["folders"]), res["pages"], res["took_s"],
                        "" if res["complete"] else " (stopped at page limit)")
        except Exception as e:  # noqa: BLE001
            c.update(census_fail_t=now,
                     census_error=f"{type(e).__name__}: {e}")
            logger.warning("Syncthing backlog census failed: %s: %s",
                           type(e).__name__, e)


def refresh_bg(settings, armed: bool) -> bool:
    """Kick refresh() in a daemon thread when anything is due; at most one
    in flight. True when a thread was started."""
    now = time.time()
    if not (errors_due(_cache, now, armed) or census_due(_cache, now, armed)):
        return False
    if not _lock.acquire(blocking=False):
        return False

    def _run():
        try:
            refresh(settings, armed=armed)
        finally:
            _lock.release()

    threading.Thread(target=_run, name="sync-hygiene", daemon=True).start()
    return True


def refreshing() -> bool:
    return _lock.locked()


# --- report ------------------------------------------------------------------

def folder_rows(census: Optional[dict], top_n: int = 25) -> list[dict]:
    """Census aggregate -> rows sorted by pending bytes (then files)."""
    if not census:
        return []
    rows = []
    for name, t in (census.get("folders") or {}).items():
        cls, why = classify_folder(name)
        subs = sorted(((k, v[0], v[1]) for k, v in t.get("subs", {}).items()),
                      key=lambda s: (-s[2], -s[1]))[:5]
        rows.append({"folder": name, "class": cls, "reason": why,
                     "files": t["files"], "bytes": t["bytes"],
                     "pattern": (ignore_pattern(name)
                                 if cls == "other" and name != ROOT_FILES
                                 else None),
                     "top_subfolders": [{"folder": k, "files": f, "bytes": b}
                                        for k, f, b in subs]})
    rows.sort(key=lambda r: (-r["bytes"], -r["files"]))
    return rows[:top_n] if top_n else rows


def queue_groups(census: Optional[dict]) -> list[dict]:
    """Census -> /api/sync/queue style groups (two-level folders), complete
    rather than "from the first 20,000 listed"."""
    out = []
    for top, t in ((census or {}).get("folders") or {}).items():
        cls = classify_folder(top)[0]
        rest_f, rest_b = t["files"], t["bytes"]
        for k, (f, b) in (t.get("subs") or {}).items():
            out.append({"folder": k, "files": f, "bytes": b, "class": cls})
            rest_f -= f
            rest_b -= b
        if rest_f > 0:
            out.append({"folder": top, "files": rest_f, "bytes": rest_b,
                        "class": cls})
    out.sort(key=lambda g: (-g["bytes"], -g["files"]))
    return out


def build_report(cache: dict, folder_id: str = "", folder_path: str = "",
                 need: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """The /api/sync/hygiene payload, from cached data only."""
    now = time.time() if now is None else now
    census = cache.get("census")
    rows_all = folder_rows(census, top_n=0)
    other = [r for r in rows_all if r["class"] == "other"]
    astro = [r for r in rows_all if r["class"] == "astro"]
    total_files = sum(r["files"] for r in rows_all)
    total_bytes = sum(r["bytes"] for r in rows_all)
    other_files = sum(r["files"] for r in other)
    other_bytes = sum(r["bytes"] for r in other)
    errs = cache.get("errors") or None
    status = cache.get("status") or {}
    sugg = ignore_suggestion(rows_all, folder_id, folder_path)

    warning = None
    if other:
        warning = {
            "text": "Folders: "
                    + ", ".join(r["folder"] for r in other[:6])
                    + (" and more" if len(other) > 6 else "")
                    + f" ({other_files} files, {_gb(other_bytes)} of the "
                      "backlog).",
            "patterns": sugg["patterns"],
            "where": sugg["where"],
            "note": sugg["note"],
        }

    diagnosis = []
    if census and other_files:
        pct = round(100.0 * other_files / total_files, 1) if total_files else 0
        diagnosis.append(f"{pct}% of the pending files ({other_files} of "
                         f"{total_files}, {_gb(other_bytes)}) are in "
                         "non-astronomy folders.")
    if errs and errs.get("total"):
        ph = next((k["count"] for k in errs.get("by_kind", [])
                   if k["kind"] == "cloud_placeholder"), 0)
        line = f"Syncthing reports {errs['total']} failed item(s) on this folder"
        if ph:
            line += (f"; {ph} of the {errs.get('count', 0)} listed look like "
                     "OneDrive cloud placeholders that Syncthing cannot read "
                     "(probable stall cause)")
        diagnosis.append(line + ".")
    if status.get("error"):
        diagnosis.append(f"Folder error: {status['error']}")
    if status.get("watchError"):
        diagnosis.append(f"Watcher error: {status['watchError']}")
    if (census and need and isinstance(need.get("items"), int)
            and census.get("complete")
            and abs(need["items"] - total_files) > max(50, need["items"] // 20)):
        diagnosis.append(f"Census counted {total_files} files but completion "
                         f"says {need['items']}; the backlog changed during "
                         "the walk or Syncthing is still scanning.")

    return {
        "configured": True,
        "census": {
            "available": census is not None,
            "complete": bool(census and census.get("complete")),
            "listed_files": census.get("listed_files") if census else 0,
            "pages": census.get("pages") if census else 0,
            "took_s": census.get("took_s") if census else None,
            "walked_at": census.get("t") if census else None,
            "age_s": round(now - census["t"]) if census else None,
            "error": cache.get("census_error") or "",
            "refreshing": refreshing(),
            "period_s": _CENSUS_PERIOD_S,
        },
        "total_files": total_files,
        "total_bytes": total_bytes,
        "astro": {"files": total_files - other_files,
                  "bytes": total_bytes - other_bytes, "folders": len(astro)},
        "other": {"files": other_files, "bytes": other_bytes,
                  "folders": len(other)},
        "folders": rows_all[:25],
        "more_folders": max(0, len(rows_all) - 25),
        "warning": warning,
        "ignore_suggestion": sugg,
        "errors": errs,
        "errors_error": cache.get("errors_error") or "",
        "folder_status": status,
        "diagnosis": diagnosis,
    }


def _gb(b: int) -> str:
    return f"{(b or 0) / 1e9:.1f} GB"
