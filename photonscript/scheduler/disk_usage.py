"""Disk tab: where the scope PC's image drive goes and what could be freed.

Read only. Nothing here deletes, moves or renames a file: the cleanup panel
only shows PowerShell commands Jeremy can run himself.

One scan walks every configured area (all paths come from config):

    captures_rc16       image_watch_dir (NINA #1, per-night YYYY-MM-DD)
    captures_piggyback  piggyback_image_watch_dir (NINA #2)
    library             library_dir (runs.library_root)
    library_archive     library_archive.default_archive_root (NINAArchive)
    share_other         disk_share_dir, else the Library's parent (OneDrive
                        and everything else Syncthing shares)
    thesky_autosave     disk_thesky_autosave_dir
    photonscript        disk_photonscript_dir
    data                data_dir (thumbs, logs, grade records)

and reports "other" as the rest of the drive's used bytes.

Hardlink aware: Library files are hardlinks to the capture originals
(runs.build_library), so a file is counted once per file id (st_dev,
st_ino; on Windows os.stat fills both, DirEntry.stat does not, so the link
areas pay one os.stat per file). A file's bytes belong to the first area
in the order above that holds a link to it; the other areas report it as
shared. Deleting one link frees nothing: a file is freed only when every
link is gone, which is why the cleanup commands list every link. A file
whose st_nlink is higher than the links found has a link outside the
scanned folders (links_elsewhere).

Per night x rig: total, kept (approved), rejected (sub index verdict
REJECTED, or a link under a _rejected / BAD folder), unjudged (pending or
not in any subs log) and calibration bytes; whether the desktop already
has the night's Library files (Syncthing remoteneed, app.
_syncthing_pending_names, at request time); how much of the kept bytes an
integration ledger used (integrations ledgers, sub_files); archived
(library_archive cutoff: those nights' desktop copies were removed by
design, so they are never cleanup candidates).

A scan takes a minute or two on the scope: it runs in a daemon thread
(refresh_bg) and the result is cached in memory and in
<data_dir>/disk_usage.json (summary) + disk_usage_files.json (the paths
behind each cleanup list). Requests never wait for a scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

SUMMARY_FILE = "disk_usage.json"
FILES_FILE = "disk_usage_files.json"
SCHEMA = 1

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINK_AREAS = ("captures_rc16", "captures_piggyback", "library",
              "library_archive")
CAPTURE_AREAS = {"captures_rc16": "rc16", "captures_piggyback": "piggyback"}
LIBRARY_AREAS = ("library", "library_archive")
REJECT_DIRS = {"_rejected", "bad"}
CAL_DIRS = {"FLAT", "FLATS", "DARK", "DARKS", "BIAS", "BIASES", "SNAPSHOT",
            "CALIBRATION"}
# Windows cloud placeholders (OneDrive Files On-Demand): listed size, no
# bytes on disk
_CLOUD_ATTRS = 0x1000 | 0x40000 | 0x400000   # OFFLINE | RECALL_ON_OPEN |
                                              # RECALL_ON_DATA_ACCESS
NO_NIGHT = "(no date)"

KEPT, REJECTED, UNJUDGED, CALIBRATION = ("kept", "rejected", "unjudged",
                                         "calibration")

_lock = threading.Lock()
_state: dict = {"scanning": False, "progress": 0, "started": 0.0,
                "error": ""}
_mem: dict = {}   # data_dir -> (summary, files)


# --- areas -------------------------------------------------------------------

def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")


def _is_anchor(p: Path) -> bool:
    """A drive or filesystem root: never walked as an area."""
    s = str(p).rstrip("\\/")
    return s == "" or _norm(p) == _norm(Path(str(p)).anchor or "/")


def areas(config) -> list[dict]:
    """The configured areas, in attribution order; duplicates and roots
    dropped. Each: key, label, path, links (hardlink-aware), rig."""
    from photonscript.scheduler.runs import library_root
    lib = library_root(config)
    try:
        from photonscript.scheduler.library_archive import default_archive_root
        arch = default_archive_root(config)
    except Exception:  # noqa: BLE001 - no archive location
        arch = None
    share = (getattr(config, "disk_share_dir", "") or "").strip()
    cand = [
        ("captures_rc16", "Captures NINA #1 (RC16)",
         getattr(config, "image_watch_dir", "") or ""),
        ("captures_piggyback", "Captures NINA #2 (Piggy-600)",
         getattr(config, "piggyback_image_watch_dir", "") or ""),
        ("library", "Library (hardlinks to captures)", str(lib)),
        ("library_archive", "Library archive (NINAArchive)",
         str(arch) if arch else ""),
        ("share_other", "Share other (OneDrive etc.)",
         share or str(lib.parent)),
        ("thesky_autosave", "TheSky autosave",
         getattr(config, "disk_thesky_autosave_dir", "") or ""),
        ("photonscript", "PhotonScript install",
         getattr(config, "disk_photonscript_dir", "") or ""),
        ("data", "PhotonScript data", str(getattr(config, "data_dir", "") or "")),
    ]
    out, seen = [], set()
    for key, label, path in cand:
        if not path:
            continue
        p = Path(path)
        n = _norm(p)
        if n in seen or _is_anchor(p):
            continue
        seen.add(n)
        out.append({"key": key, "label": label, "path": str(p),
                    "links": key in LINK_AREAS,
                    "rig": CAPTURE_AREAS.get(key)})
    return out


# --- walking -----------------------------------------------------------------

def _walk(root: Path, prune: set[str], progress=None) -> Iterable:
    """(DirEntry, relative parts) of every regular file under root, never
    following links or junctions and never entering another area's root."""
    stack = [(str(root), ())]
    while stack:
        d, rel = stack.pop()
        try:
            it = os.scandir(d)
        except OSError as e:
            logger.debug("disk usage: cannot list %s: %s", d, e)
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink() or (hasattr(e, "is_junction")
                                          and e.is_junction()):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if _norm(e.path) not in prune:
                            stack.append((e.path, rel + (e.name,)))
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if progress is not None:
                    progress()
                yield e, rel + (e.name,)


def _file_id(entry, links: bool):
    """(file id, size, nlink, cloud_only, mtime). Link areas get a real
    (st_dev, st_ino): os.stat when DirEntry left them zero (Windows)."""
    st = entry.stat(follow_symlinks=False)
    if links and not st.st_ino:
        st = os.stat(entry.path, follow_symlinks=False)
    attrs = getattr(st, "st_file_attributes", 0) or 0
    cloud = bool(attrs & _CLOUD_ATTRS)
    if links and st.st_ino:
        fid = f"{st.st_dev}:{st.st_ino}"
    else:
        fid = "p:" + _norm(entry.path)
    return fid, int(st.st_size), int(st.st_nlink or 1), cloud, st.st_mtime


# --- sub index / ledgers -----------------------------------------------------

def _verdicts(config) -> tuple[dict, dict, dict]:
    """(by abs path, by (rig, basename), by basename) -> (verdict, date,
    rig) from the sub index (every night's subs log)."""
    from photonscript.scheduler import sub_index
    by_path, by_rig_name, by_name = {}, {}, {}
    try:
        rows = sub_index.all_rows(config)
    except Exception as e:  # noqa: BLE001 - the scan still reports bytes
        logger.warning("disk usage: sub index unavailable: %s", e)
        rows = []
    for r in rows:
        v = (r["verdict"], r["date"], r["rig"])
        if r.get("abs_path"):
            by_path[_norm(r["abs_path"])] = v
        name = Path(str(r.get("file") or r.get("abs_path") or "")
                    .replace("\\", "/")).name
        if name:
            by_rig_name[(r["rig"], name)] = v
            by_name.setdefault(name, v)
    return by_path, by_rig_name, by_name


def _ledger_files(config) -> Optional[set]:
    """Basenames any integration ledger used; None when there is none."""
    try:
        from photonscript.scheduler.integrations import ledgers_root
        from photonscript.shared import ledger as L
    except Exception:  # noqa: BLE001
        return None
    root = ledgers_root(config)
    files = sorted(root.glob("*/v*.json")) if root.is_dir() else []
    if not files:
        return None
    out: set = set()
    for f in files:
        try:
            out |= L.load(f).sub_files
        except Exception as e:  # noqa: BLE001 - one bad ledger never hides the rest
            logger.debug("disk usage: ledger %s unreadable: %s", f, e)
    return out


# --- scan --------------------------------------------------------------------

def _drive(config, area_list: list[dict]) -> dict:
    base = getattr(config, "image_watch_dir", "") or ""
    p = Path(base) if base else Path(str(config.data_dir))
    anchor = p.anchor or str(p)
    probe = anchor if os.path.exists(anchor) else str(config.data_dir)
    try:
        du = shutil.disk_usage(probe)
        return {"path": anchor, "total": du.total, "used": du.used,
                "free": du.free}
    except OSError as e:
        return {"path": anchor, "total": None, "used": None, "free": None,
                "error": str(e)}


def scan(config, now: Optional[float] = None, progress=None) -> tuple[dict, dict]:
    """Walk every area once. Returns (summary, files); never raises for a
    missing or unreadable folder."""
    t0 = time.time()
    now = t0 if now is None else now
    alist = areas(config)
    roots = {a["key"]: _norm(a["path"]) for a in alist}
    keep_days = int(getattr(config, "disk_thesky_keep_days", 30) or 0)
    sky_cut = now - keep_days * 86400 if keep_days > 0 else None

    inodes: dict[str, dict] = {}
    area_out = []
    thesky_old: list[str] = []
    thesky_old_bytes = 0
    for a in alist:
        root = Path(a["path"])
        row = {"key": a["key"], "label": a["label"], "path": a["path"],
               "exists": root.is_dir(), "bytes": 0, "apparent_bytes": 0,
               "files": 0, "shared_bytes": 0, "cloud_only_bytes": 0,
               "top": {}}
        area_out.append(row)
        if not row["exists"]:
            continue
        prune = {v for k, v in roots.items() if k != a["key"]}
        for entry, rel in _walk(root, prune, progress):
            try:
                fid, size, nlink, cloud, mtime = _file_id(entry, a["links"])
            except OSError:
                continue
            row["files"] += 1
            row["apparent_bytes"] += size
            rec = inodes.get(fid)
            if rec is None:
                rec = {"s": size, "k": nlink, "a": a["key"], "cl": cloud,
                       "p": [], "m": mtime}
                inodes[fid] = rec
                if cloud:
                    row["cloud_only_bytes"] += size
                else:
                    row["bytes"] += size
                    top = rel[0] if len(rel) > 1 else "(files here)"
                    row["top"][top] = row["top"].get(top, 0) + size
            else:
                row["shared_bytes"] += size
            if a["links"]:
                rec["p"].append((a["key"], entry.path, rel))
            if (a["key"] == "thesky_autosave" and sky_cut is not None
                    and mtime < sky_cut and rec["a"] == a["key"]):
                thesky_old.append(entry.path)
                if not cloud:
                    thesky_old_bytes += size
    for row in area_out:
        tops = sorted(row["top"].items(), key=lambda kv: -kv[1])
        row["top"] = [{"name": k, "bytes": v} for k, v in tops[:8]]

    drive = _drive(config, alist)
    d_anchor = _norm(drive["path"])
    on_drive = 0
    for row in area_out:
        row["other_drive"] = _norm(Path(row["path"]).anchor or row["path"]) != d_anchor
        if not row["other_drive"]:
            on_drive += row["bytes"]
    if drive.get("used") is not None:
        area_out.append({"key": "other", "label": "Everything else on the drive",
                         "path": drive["path"], "exists": True,
                         "bytes": max(0, drive["used"] - on_drive),
                         "apparent_bytes": None, "files": None,
                         "shared_bytes": 0, "cloud_only_bytes": 0,
                         "top": [], "other_drive": False})

    nights, files_out, rejected = _classify(config, inodes)
    summary = {
        "schema": SCHEMA,
        "scanned_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "scanned_ts": now,
        "took_s": round(time.time() - t0, 1),
        "drive": drive,
        "areas": area_out,
        "rejected": rejected,
        "nights": nights,
        "thesky_old": {"days": keep_days, "bytes": thesky_old_bytes,
                       "files": len(thesky_old)},
        "inodes": len(inodes),
    }
    files = {"schema": SCHEMA, "scanned_ts": now, "inodes": files_out,
             "thesky_old": thesky_old}
    return summary, files


def _classify(config, inodes: dict) -> tuple[list, list, dict]:
    """Night x rig rows, the per-file list behind the cleanup commands, and
    the rejected totals, from the link-area files."""
    by_path, by_rig_name, by_name = _verdicts(config)
    used = _ledger_files(config)
    nights: dict[tuple, dict] = {}
    files_out = []
    rej = {"bytes": 0, "files": 0, "links": 0, "verdict_bytes": 0,
           "folder_bytes": 0, "links_elsewhere_bytes": 0,
           "links_elsewhere_files": 0}
    for rec in inodes.values():
        links = rec["p"]
        if not links:
            continue
        name = Path(links[0][1]).name
        cap = next((x for x in links if x[0] in CAPTURE_AREAS), None)
        rig = CAPTURE_AREAS[cap[0]] if cap else None
        night = None
        if cap is not None:
            night = cap[2][0] if (len(cap[2]) > 1
                                  and _DATE_RE.match(cap[2][0])) else NO_NIGHT
        verdict = None
        for _k, path, _rel in links:
            v = by_path.get(_norm(path))
            if v:
                verdict = v
                break
        if verdict is None and rig:
            verdict = by_rig_name.get((rig, name))
        if verdict is None:
            verdict = by_name.get(name)
        if rig is None:
            lib_rel = next((x[2] for x in links if x[0] in LIBRARY_AREAS), ())
            rig = (verdict[2] if verdict else
                   ("piggyback" if lib_rel and lib_rel[0].lower() == "piggyback"
                    else "rc16"))
        if (night is None or night == NO_NIGHT) and verdict is not None:
            night = verdict[1]
        in_reject_dir = any(p.lower() in REJECT_DIRS for x in links
                            for p in x[2][:-1])
        is_cal = any(p.upper() in CAL_DIRS for x in links for p in x[2][:-1])
        if (verdict and verdict[0] == "rejected") or in_reject_dir:
            cls = REJECTED
        elif is_cal and not verdict:
            cls = CALIBRATION
        elif verdict and verdict[0] == "approved":
            cls = KEPT
        else:
            cls = UNJUDGED
        size = 0 if rec["cl"] else rec["s"]
        lib_names = [Path(x[1]).name for x in links if x[0] == "library"]
        if cls == REJECTED:
            rej["bytes"] += size
            rej["files"] += 1
            rej["links"] += len(links)
            if verdict and verdict[0] == "rejected":
                rej["verdict_bytes"] += size
            if in_reject_dir:
                rej["folder_bytes"] += size
            if rec["k"] > len(links):
                rej["links_elsewhere_bytes"] += size
                rej["links_elsewhere_files"] += 1
        files_out.append({"s": size, "n": night or "", "r": rig, "c": cls,
                          "e": rec["k"] > len(links),
                          "p": [x[1] for x in links]})
        if night is None:
            continue           # a Library-only file of no known night
        row = nights.get((night, rig))
        if row is None:
            row = nights[(night, rig)] = {
                "date": night, "rig": rig, "bytes": 0, "files": 0,
                "kept_bytes": 0, "rejected_bytes": 0, "unjudged_bytes": 0,
                "calibration_bytes": 0, "library_bytes": 0,
                "integrated_bytes": 0, "lib_names": []}
        row["bytes"] += size
        row["files"] += 1
        row[cls + "_bytes"] += size
        if lib_names:
            row["library_bytes"] += size
            row["lib_names"].extend(lib_names)
        if cls == KEPT and used is not None and name in used:
            row["integrated_bytes"] += size
    out = sorted(nights.values(), key=lambda r: (r["date"], r["rig"]),
                 reverse=True)
    for r in out:
        r["integrated_pct"] = (None if used is None or not r["kept_bytes"]
                               else round(100.0 * r["integrated_bytes"]
                                          / r["kept_bytes"], 1))
    return out, files_out, rej


# --- cache -------------------------------------------------------------------

def _paths(config) -> tuple[Path, Path]:
    d = Path(str(config.data_dir))
    return d / SUMMARY_FILE, d / FILES_FILE


def _write(p: Path, data: dict) -> None:
    from photonscript.shared.fileops import replace_retry
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=True), encoding="ascii")
    replace_retry(tmp, p)


def save(config, summary: dict, files: dict) -> None:
    sp, fp = _paths(config)
    _write(fp, files)
    _write(sp, summary)
    _mem[_norm(config.data_dir)] = (summary, files)


def cached(config, with_files: bool = False):
    """The last scan (summary, or (summary, files)); None when never run."""
    key = _norm(config.data_dir)
    hit = _mem.get(key)
    if hit is None:
        sp, fp = _paths(config)
        try:
            s = json.loads(sp.read_text(encoding="utf-8"))
            if s.get("schema") != SCHEMA:
                return None
        except (OSError, ValueError):
            return None
        try:
            f = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            f = {"inodes": [], "thesky_old": []}
        hit = _mem[key] = (s, f)
    return hit if with_files else hit[0]


def refresh(config) -> None:
    """Scan-thread body: scan, then cache. Records a failure, never raises."""
    _state.update(scanning=True, progress=0, started=time.time(), error="")

    def _tick():
        _state["progress"] += 1
    try:
        s, f = scan(config, progress=_tick)
        save(config, s, f)
        logger.info("disk usage scan: %d files in %.1f s",
                    _state["progress"], s["took_s"])
    except Exception as e:  # noqa: BLE001
        _state["error"] = f"{type(e).__name__}: {e}"
        logger.warning("disk usage scan failed: %s", _state["error"])
    finally:
        _state["scanning"] = False


def refresh_bg(config) -> bool:
    """Start a scan in a daemon thread unless one runs. True when started."""
    if not _lock.acquire(blocking=False):
        return False
    _state["scanning"] = True

    def _run():
        try:
            refresh(config)
        finally:
            _lock.release()

    threading.Thread(target=_run, name="disk-usage", daemon=True).start()
    return True


def scanning() -> bool:
    return _lock.locked()


# --- report ------------------------------------------------------------------

def _sync_state(names: list[str], pending: Optional[set],
                capped: bool) -> tuple[Optional[bool], Optional[int]]:
    """(synced, pending count) of one night's Library files. None = cannot
    tell (Syncthing unknown, nothing in the Library, or a capped list)."""
    if not names or pending is None:
        return None, None
    n = sum(1 for x in names if x in pending)
    if n:
        return False, n
    return (None if capped else True), 0


def _night_view(config, summary: dict, pending, capped, now: datetime) -> list[dict]:
    try:
        from photonscript.scheduler.library_archive import archive_cutoff
        cut = archive_cutoff(config)
    except Exception:  # noqa: BLE001
        cut = ""
    keep = int(getattr(config, "disk_night_keep_days", 60) or 0)
    old_before = (now - timedelta(days=keep)).strftime("%Y-%m-%d") if keep else ""
    out = []
    for r in summary.get("nights") or []:
        v = {k: x for k, x in r.items() if k != "lib_names"}
        v["synced"], v["pending"] = _sync_state(r.get("lib_names") or [],
                                                pending, capped)
        dated = bool(_DATE_RE.match(r["date"]))
        v["archived"] = bool(cut and dated and r["date"] < cut)
        v["old"] = bool(old_before and dated and r["date"] < old_before)
        v["candidate"] = bool(v["old"] and v["synced"] is True
                              and not v["archived"])
        out.append(v)
    return out


def _base_url(config) -> str:
    return f"http://localhost:{int(getattr(config, 'scheduler_port', 8100) or 8100)}"


def _ps_commands(config, kind: str, extra: str = "") -> str:
    """List the paths to a file, count them, dry-run, then the real
    delete, as separate steps for Jeremy to run on the scope PC."""
    url = f"{_base_url(config)}/api/disk/paths?kind={kind}{extra}"
    lst = f"$env:USERPROFILE\\Desktop\\photonscript_disk_{kind}.txt"
    return "\n".join([
        "# 1. Save the list of files (every hardlink of each file) and review it",
        f'$list = "{lst}"',
        f'Invoke-WebRequest -UseBasicParsing "{url}" -OutFile $list',
        "(Get-Content $list).Count",
        "notepad $list",
        "# 2. Dry run: shows what would be removed, removes nothing",
        "Get-Content $list | ForEach-Object { Remove-Item -LiteralPath $_ -WhatIf }",
        "# 3. Only after reviewing the list and the warning above:",
        "# Get-Content $list | ForEach-Object { Remove-Item -LiteralPath $_ }",
    ])


def report(config, pending: Optional[set] = None, capped: bool = False,
           now: Optional[datetime] = None) -> dict:
    """The /api/disk body from the cached scan (no file system walk)."""
    now = now or datetime.now()
    s = cached(config)
    st = {"scanning": scanning() or bool(_state.get("scanning")),
          "progress": _state.get("progress", 0),
          "scan_error": _state.get("error", "")}
    if s is None:
        return {"ok": False, "scanned_at": None, **st,
                "note": "no scan yet: one is started in the background"}
    nights = _night_view(config, s, pending, capped, now)
    cands = [n for n in nights if n["candidate"]]
    keep = int(getattr(config, "disk_night_keep_days", 60) or 0)
    backup = getattr(config, "disk_library_backup_dir", "") or ""
    rej = dict(s.get("rejected") or {})
    sky = dict(s.get("thesky_old") or {})
    warning = ("Deleting a file from the Library (including Library\\_rejected) "
               "also deletes the desktop's Syncthing mirror copy: the desktop "
               "folder is receive-only and mirrors deletions. Deleting only one "
               "link of a hardlinked file frees nothing; the lists below hold "
               "every link.")
    if backup:
        warning += (f" A backup of the desktop mirror is being made at {backup};"
                    " confirm it is complete before deleting anything.")
    reclaim = {
        "rejected": {
            **rej,
            "freeable_bytes": max(0, rej.get("bytes", 0)
                                  - rej.get("links_elsewhere_bytes", 0)),
            "command": _ps_commands(config, "rejected"),
        },
        "thesky_old": {
            **sky, "path": getattr(config, "disk_thesky_autosave_dir", ""),
            "command": _ps_commands(config, "thesky_old"),
        },
        "old_synced_nights": {
            "days": keep,
            "bytes": sum(n["bytes"] for n in cands),
            "nights": [{"date": n["date"], "rig": n["rig"], "bytes": n["bytes"]}
                       for n in cands],
            "command": _ps_commands(config, "old_synced_nights"),
            "note": ("synced = every Library file of the night is on the "
                     "desktop (Syncthing has nothing pending for it); "
                     "archived nights are left out (their desktop copies "
                     "were already removed by the archive)"),
        },
    }
    reclaim["total_bytes"] = (reclaim["rejected"]["freeable_bytes"]
                              + int(sky.get("bytes") or 0)
                              + reclaim["old_synced_nights"]["bytes"])
    age = time.time() - float(s.get("scanned_ts") or 0)
    return {
        "ok": True, **st,
        "scanned_at": s.get("scanned_at"), "age_s": round(age),
        "took_s": s.get("took_s"),
        "drive": s.get("drive"), "areas": s.get("areas"),
        "rejected": rej, "nights": nights,
        "sync_known": pending is not None, "sync_capped": bool(capped),
        "reclaimable": reclaim, "warning": warning,
        "settings": {
            "thesky_keep_days": sky.get("days"),
            "night_keep_days": keep,
            "library_backup_dir": backup,
        },
    }


def paths(config, kind: str, pending: Optional[set] = None,
          capped: bool = False, date: str = "", rig: str = "",
          now: Optional[datetime] = None) -> Optional[list[str]]:
    """The file paths behind one cleanup list (every link of each file).
    kind: rejected | thesky_old | old_synced_nights | night (date, rig).
    None = no scan yet; ValueError on an unknown kind."""
    hit = cached(config, with_files=True)
    if hit is None:
        return None
    summary, files = hit
    if kind == "thesky_old":
        return list(files.get("thesky_old") or [])
    inodes = files.get("inodes") or []
    if kind == "rejected":
        sel = [f for f in inodes if f["c"] == REJECTED]
    elif kind == "night":
        if not date:
            raise ValueError("night needs date")
        sel = [f for f in inodes if f["n"] == date and (not rig or f["r"] == rig)]
    elif kind == "old_synced_nights":
        keys = {(n["date"], n["rig"])
                for n in _night_view(config, summary, pending, capped,
                                     now or datetime.now())
                if n["candidate"]}
        sel = [f for f in inodes if (f["n"], f["r"]) in keys]
    else:
        raise ValueError(f"unknown kind {kind!r}")
    out: list[str] = []
    for f in sel:
        out.extend(f["p"])
    return out


__all__ = ["areas", "scan", "refresh", "refresh_bg", "scanning", "cached",
           "report", "paths", "save"]
