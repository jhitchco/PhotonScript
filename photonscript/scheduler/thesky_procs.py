"""Which TheSky is running, and which one NINA's mount driver uses (PS-138).
READ ONLY.

Why: on 2026-10-05 the scope PC ran two Bisque sky apps at once, an older
TheSkyX and TheSky64 10.5 ("the older thesky keeps running": it comes back
after being closed). NINA's mount is "Driver for telescope connected through
TheSky" (ASCOM.SoftwareBisque.Telescope). The first slew was 1 deg 14' off
with the TPoint model applied in TheSky64, and the audit over TheSky TCP
3040 read "mount not connected": NINA's mount path most likely runs through
the older TheSkyX, so TheSky64's model and ProTrack are bypassed.

    scan(config)      never raises; every part is optional:
        apps          every running Bisque sky app (psutil, else tasklist):
                      name, pid, exe path, file version (win32api, else the
                      Windows version API through ctypes, else none)
        others        other processes under a Software Bisque folder (info)
        tcp           which process listens on TheSky's TCP port (psutil
                      net_connections, else netstat -ano)
        driver        which TheSky the Bisque ASCOM telescope driver targets
                      (driver_info): its ASCOM profile values (HKCU / HKLM,
                      both registry views), the Software Bisque keys, and
                      the COM servers registered for TheSky's scripting
                      ProgIDs (the exe COM starts when the driver connects).
                      The setting's name is not known yet: when nothing
                      names a TheSky, the keys that exist are reported.

Nothing here kills, starts or connects anything. The registry is opened
with KEY_READ only; the only commands run are tasklist and netstat (both
read only, and only when psutil cannot answer).
"""
from __future__ import annotations

import csv
import io
import logging
import re
import subprocess
import sys

logger = logging.getLogger(__name__)

TCP_PORT = 3040
SKY64, SKYX, SKY = "TheSky64", "TheSkyX", "TheSky"
DRIVER_PROGID = "ASCOM.SoftwareBisque.Telescope"
# the driver's ASCOM profile (Profile store) and Software Bisque's own keys,
# under HKCU and HKLM; Software\WOW6432Node is the 32-bit view ASCOM uses
DRIVER_PATHS = (r"Software\ASCOM\Telescope Drivers\ASCOM.SoftwareBisque.Telescope",
                r"Software\WOW6432Node\ASCOM\Telescope Drivers\ASCOM.SoftwareBisque.Telescope")
BISQUE_PATHS = (r"Software\Software Bisque", r"Software\WOW6432Node\Software Bisque")
# HKCR ProgIDs whose COM server is a TheSky exe (TheSky's scripting objects;
# the driver talks to TheSky through them)
PROGID_PREFIXES = ("thesky", "sky6", "ccdsoft", "ascom.softwarebisque")
MAX_DEPTH = 3
MAX_VALUES = 300
MAX_KEYS = 200


# --------------------------------------------------------------------------
# helpers (tests monkeypatch _psutil, _winreg and _run)
# --------------------------------------------------------------------------

def _psutil():
    try:
        import psutil
        return psutil
    except Exception:  # noqa: BLE001
        return None


def _winreg():
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def _run(args: list[str], timeout: float = 15.0) -> str:
    """Output of a read-only command (tasklist / netstat), "" on failure."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("%s failed: %s", args[0], e)
        return ""


def app_kind(name, path=None) -> str | None:
    """TheSky64 / TheSkyX / TheSky from an exe name or path; None when it is
    not a Bisque sky app. TheSky64 is checked first (its folder can hold an
    exe with an older name)."""
    s = f"{name or ''} {path or ''}".lower()
    if "thesky64" in s:
        return SKY64
    if "theskyx" in s:
        return SKYX
    if str(name or "").lower().startswith("thesky") or \
            re.search(r"[\\/]thesky[^\\/]*\.exe$", str(path or "").lower()):
        return SKY
    return None


def is_sky_app(name, path=None) -> bool:
    n = str(name or "").lower()
    leaf = re.split(r"[\\/]", str(path or "").lower())[-1]
    return n.startswith("thesky") or leaf.startswith("thesky")


def _bisque_path(path) -> bool:
    return "software bisque" in str(path or "").lower()


# --------------------------------------------------------------------------
# file version
# --------------------------------------------------------------------------

def file_version(path) -> str | None:
    """The exe's file version ("10.5.0.14139"), None when not readable."""
    if not path:
        return None
    try:
        import win32api  # type: ignore
        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001
        pass
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(n)) or not n.value:
            return None

        class _Fixed(ctypes.Structure):
            _fields_ = [(k, wintypes.DWORD) for k in (
                "sig", "struc", "ms", "ls", "pms", "pls", "mask", "flags", "os",
                "type", "sub", "dms", "dls")]
        f = ctypes.cast(ptr, ctypes.POINTER(_Fixed)).contents
        return f"{f.ms >> 16}.{f.ms & 0xFFFF}.{f.ls >> 16}.{f.ls & 0xFFFF}"
    except Exception as e:  # noqa: BLE001
        logger.debug("file version of %s: %s", path, e)
        return None


# --------------------------------------------------------------------------
# processes and the TCP listener
# --------------------------------------------------------------------------

def list_processes() -> tuple[list[dict], str]:
    """([{pid, name, exe}], how). psutil, else tasklist (no exe path)."""
    ps = _psutil()
    if ps is not None:
        try:
            out = []
            for p in ps.process_iter(["pid", "name", "exe"]):
                i = p.info
                out.append({"pid": i.get("pid"), "name": i.get("name") or "",
                            "exe": i.get("exe") or None})
            return out, "psutil"
        except Exception as e:  # noqa: BLE001
            logger.debug("psutil process list failed: %s", e)
    txt = _run(["tasklist", "/FO", "CSV", "/NH"])
    out = []
    for row in csv.reader(io.StringIO(txt)):
        if len(row) >= 2 and row[1].strip().isdigit():
            out.append({"pid": int(row[1]), "name": row[0].strip(), "exe": None})
    return out, ("tasklist" if out else "none")


_NETSTAT = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE)


def tcp_listener(port: int = TCP_PORT) -> tuple[int | None, str]:
    """(pid listening on TCP <port>, how). psutil, else netstat -ano."""
    ps = _psutil()
    if ps is not None:
        try:
            for c in ps.net_connections(kind="tcp"):
                if (str(c.status).upper() == "LISTEN" and c.laddr
                        and int(c.laddr.port) == int(port)):
                    return c.pid, "psutil"
            return None, "psutil"
        except Exception as e:  # noqa: BLE001
            logger.debug("psutil net_connections failed: %s", e)
    txt = _run(["netstat", "-ano", "-p", "TCP"])
    for line in txt.splitlines():
        m = _NETSTAT.match(line)
        if m and int(m.group(1)) == int(port):
            return int(m.group(2)), "netstat"
    return None, ("netstat" if txt else "none")


# --------------------------------------------------------------------------
# the registry (read only)
# --------------------------------------------------------------------------

def _open(reg, root, path):
    return reg.OpenKey(root, path, 0, reg.KEY_READ)


def _walk(reg, key, prefix: str, values: dict, keys: list, depth: int) -> None:
    i = 0
    while len(values) < MAX_VALUES:
        try:
            name, value, _typ = reg.EnumValue(key, i)
        except OSError:
            break
        values[f"{prefix}\\{name or '(default)'}"] = value
        i += 1
    if depth >= MAX_DEPTH:
        return
    i = 0
    while len(keys) < MAX_KEYS:
        try:
            sub = reg.EnumKey(key, i)
        except OSError:
            break
        keys.append(f"{prefix}\\{sub}")
        try:
            with reg.OpenKey(key, sub, 0, reg.KEY_READ) as h:
                _walk(reg, h, f"{prefix}\\{sub}", values, keys, depth + 1)
        except OSError:
            pass
        i += 1


def _default(reg, root, path) -> str | None:
    try:
        with _open(reg, root, path) as h:
            v = reg.QueryValueEx(h, "")[0]
            return str(v) if v not in (None, "") else None
    except OSError:
        return None


def _com_server(reg, progid: str) -> dict | None:
    """{progid, clsid, server} for a ProgID: HKCR\\<progid>\\CLSID, then
    LocalServer32 / InprocServer32 in both registry views."""
    hkcr = reg.HKEY_CLASSES_ROOT
    clsid = _default(reg, hkcr, f"{progid}\\CLSID")
    if not clsid:
        cur = _default(reg, hkcr, f"{progid}\\CurVer")
        clsid = _default(reg, hkcr, f"{cur}\\CLSID") if cur else None
    if not clsid:
        return None
    for view in ("CLSID", r"WOW6432Node\CLSID"):
        for kind in ("LocalServer32", "InprocServer32"):
            srv = _default(reg, hkcr, f"{view}\\{clsid}\\{kind}")
            if srv:
                return {"progid": progid, "clsid": clsid, "server": srv.strip().strip('"'),
                        "server_type": kind, "view": view}
    return {"progid": progid, "clsid": clsid, "server": None}


def _progids(reg) -> list[str]:
    hkcr, out, i = reg.HKEY_CLASSES_ROOT, [], 0
    while len(out) < 60:
        try:
            n = reg.EnumKey(hkcr, i)
        except OSError:
            break
        if n.lower().startswith(PROGID_PREFIXES):
            out.append(n)
        i += 1
    return out


def driver_info() -> dict:
    """Which TheSky the Bisque ASCOM telescope driver targets. {available,
    target (TheSky64 / TheSkyX / None), via, candidates [{kind, where}],
    keys (that exist), values (TheSky-ish ones), com [...], note}. Never
    raises."""
    reg = _winreg()
    if reg is None:
        return {"available": False, "target": None, "note": "not Windows (no registry)",
                "candidates": [], "keys": [], "values": {}, "com": []}
    keys, values, cands = [], {}, []
    roots = (("HKCU", reg.HKEY_CURRENT_USER), ("HKLM", reg.HKEY_LOCAL_MACHINE))
    for rname, root in roots:
        for path in DRIVER_PATHS + BISQUE_PATHS:
            try:
                with _open(reg, root, path) as h:
                    base = f"{rname}\\{path}"
                    keys.append(base)
                    vals: dict = {}
                    sub: list = []
                    depth = 0 if path in DRIVER_PATHS else 1
                    _walk(reg, h, base, vals, sub, depth)
                    keys += sub
                    for k, v in vals.items():
                        driver_key = path in DRIVER_PATHS
                        leaf = k.rsplit("\\", 1)[-1]
                        kind = app_kind(leaf, v if isinstance(v, str) else None)
                        if driver_key or kind:
                            values[k] = v if isinstance(v, (str, int, float)) else repr(v)
                        if driver_key and kind and kind != SKY:
                            cands.append({"kind": kind, "where": f"{k} = {v}"})
            except OSError:
                continue
    com = []
    try:
        for pid in _progids(reg):
            c = _com_server(reg, pid)
            if c:
                c["kind"] = app_kind(None, c.get("server"))
                com.append(c)
                if c["kind"] in (SKY64, SKYX) and not pid.lower().startswith("ascom."):
                    cands.append({"kind": c["kind"],
                                  "where": f"COM {pid} -> {c['server']}"})
    except Exception as e:  # noqa: BLE001
        logger.debug("COM ProgID scan failed: %s", e)
    kinds = sorted({c["kind"] for c in cands})
    out = {"available": True, "keys": keys[:MAX_KEYS], "values": values, "com": com,
           "candidates": cands}
    if len(kinds) == 1:
        via = "ASCOM profile" if any(not c["where"].startswith("COM ") for c in cands) \
            else "COM registration"
        out.update(target=kinds[0], via=via, note=f"{kinds[0]} ({via})")
    elif kinds:
        out.update(target=None, via=None,
                   note="both named: " + "; ".join(c["where"] for c in cands[:4]))
    else:
        found = ", ".join(keys[:6]) if keys else "no driver or Software Bisque keys"
        out.update(target=None, via=None,
                   note=f"no setting names a TheSky (keys: {found})")
    return out


# --------------------------------------------------------------------------
# the scan
# --------------------------------------------------------------------------

def _app_text(a: dict) -> str:
    v = a.get("version") or (a.get("exe") or "path unknown")
    return f"{a.get('kind') or a.get('name')} {v} (pid {a.get('pid')})"


def scan(config=None, *, procs=None, listener=None, driver=None) -> dict:
    """Every running Bisque sky app, the TCP port's owner and the driver's
    target. procs / listener / driver are passed in by tests. Never raises."""
    port = int(getattr(config, "thesky_tcp_port", TCP_PORT) or TCP_PORT)
    out: dict = {"ok": False, "port": port, "apps": [], "others": [], "tcp_pid": None,
                 "tcp_owner": None, "tcp_kind": None, "driver": None, "how": {}}
    try:
        if procs is None:
            procs, how = list_processes()
        else:
            how = "given"
        out["how"]["processes"] = how
        out["ok"] = how != "none"
        for p in procs or []:
            name, exe = p.get("name") or "", p.get("exe")
            if is_sky_app(name, exe):
                a = {"pid": p.get("pid"), "name": name, "exe": exe,
                     "kind": app_kind(name, exe),
                     "version": p.get("version") if "version" in p else file_version(exe)}
                out["apps"].append(a)
            elif _bisque_path(exe):
                out["others"].append({"pid": p.get("pid"), "name": name, "exe": exe})
    except Exception as e:  # noqa: BLE001
        out["note"] = f"process list: {type(e).__name__}: {e}"
    try:
        if listener is None:
            pid, how = tcp_listener(port)
        else:
            pid, how = listener, "given"
        out["how"]["tcp"] = how
        out["tcp_pid"] = pid
        if pid is not None:
            hit = next((a for a in out["apps"] if a.get("pid") == pid), None)
            out["tcp_owner"] = _app_text(hit) if hit else f"pid {pid} (not a TheSky)"
            out["tcp_kind"] = hit.get("kind") if hit else None
    except Exception as e:  # noqa: BLE001
        out["how"]["tcp"] = f"failed: {e}"
    try:
        out["driver"] = driver if driver is not None else driver_info()
    except Exception as e:  # noqa: BLE001
        out["driver"] = {"available": False, "target": None, "note": str(e)}
    out["count"] = len(out["apps"])
    out["text"] = ", ".join(_app_text(a) for a in out["apps"]) or "none"
    return out
