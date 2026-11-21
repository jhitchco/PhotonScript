"""Gaia preflight for finishes that plate-solve (`photonscript pixinsight-gaia-check`).

2026-10-14: the M31 v6 finish hung 3.5 h. Only Gaia DR3/SP was configured;
ImageSolver logged "No database files have been selected", fell back to a
VizieR download and froze. Gaia DR3 (astrometric) now lives in D:/Gaia/DR3
(gdr3-1.0.0-01..16.xpsd) and DR3/SP in D:/Gaia/DR3SP, set in PixInsight's
settings.

PixInsight keeps settings per instance slot: %APPDATA%/Pleiades/
core-00N-pxi.settings (XSDT XML). The Gaia process database paths are
<xsdt>/<i k="ModuleData">/<i k="Gaia"> values
<v k="DR3DatabaseFilePathNN" t="s">path</v> and DR3SPDatabaseFilePathNN.
Only core-001 has them. An automation instance (`-n`, started only while
no other PixInsight runs: runner.run_script refuses otherwise) takes the
lowest free slot, so slot 1; while N instances run, a new one gets slot
N + 1.

check() reads the settings files (read-only) and reports: the slot the
next automation instance would use, the DR3 and DR3/SP paths of that slot
and whether every file exists. dr3_ok False (no DR3 paths in the slot, or
one missing on disk) = a finish must not plate-solve: the online fallback
is what hung. Settings not found = unknown: nothing is changed.

fix() (--fix) copies the Gaia keys of the slot that has them into every
other slot file that lacks them, only while no PixInsight runs, after a
backup (<file>.bak-<YYYYMMDD-HHMMSS>-gaia), as a text splice that leaves
the rest of the file byte for byte (PixInsight rewrites it on exit).
"""

from __future__ import annotations

import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ENV_DIR = "PS_PIXINSIGHT_SETTINGS_DIR"
NS = "{http://www.pixinsight.com/xsdt}"
SLOT_RE = re.compile(r"^core-(\d{3})-pxi\.settings$")
KEY_RE = re.compile(r"^(DR3|DR3SP)DatabaseFilePath(\d+)$")
_V_KEY = re.compile(r'<v k="(?:DR3|DR3SP)DatabaseFilePath\d+"[^>]*?(?:/>|>[^<]*</v>)')
_TAG = re.compile(r'<i k="([^"]*)"\s*(/?)>|</i>')


class GaiaFixError(RuntimeError):
    pass


def settings_dir() -> Path:
    env = os.environ.get(ENV_DIR, "").strip()
    if env:
        return Path(env)
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Pleiades"


def slot_files(d: Path | None = None) -> dict[int, Path]:
    """slot number -> core-00N-pxi.settings (backups and other files ignored)."""
    d = Path(d) if d else settings_dir()
    out: dict[int, Path] = {}
    try:
        entries = list(d.iterdir())
    except OSError:
        return out
    for p in entries:
        m = SLOT_RE.match(p.name)
        if m and p.is_file():
            out[int(m.group(1))] = p
    return dict(sorted(out.items()))


def gaia_paths(path: Path) -> dict[str, list[str]]:
    """{"DR3": [...], "DR3SP": [...]} from one settings file's
    ModuleData/Gaia block (in key order); empty lists when absent.
    Raises ValueError when the file is not parseable XML."""
    try:
        root = ET.parse(str(path)).getroot()
    except (ET.ParseError, OSError) as e:
        raise ValueError(f"{Path(path).name}: {e}") from None
    out: dict[str, list[tuple[int, str]]] = {"DR3": [], "DR3SP": []}
    for md in root.findall(f"{NS}i[@k='ModuleData']"):
        for g in md.findall(f"{NS}i[@k='Gaia']"):
            for v in g.findall(f"{NS}v"):
                m = KEY_RE.match(v.get("k") or "")
                if m:
                    out[m.group(1)].append((int(m.group(2)), (v.text or "").strip()))
    return {k: [p for _n, p in sorted(v) if p] for k, v in out.items()}


def automation_slot(running_count: int) -> int:
    """The slot a new `-n` PixInsight takes: the lowest free one, assuming
    the running instances hold slots 1..N."""
    return max(0, int(running_count)) + 1


@dataclass
class GaiaStatus:
    settings_dir: Path
    slots: dict = field(default_factory=dict)          # slot -> {"file", "dr3", "dr3sp", "error"}
    slot: int = 1                                      # the next automation instance's slot
    dr3: list = field(default_factory=list)            # that slot's paths
    dr3sp: list = field(default_factory=list)
    dr3_missing: list = field(default_factory=list)    # listed but not on disk
    dr3sp_missing: list = field(default_factory=list)
    found: bool = False                                # that slot's settings file was read
    warnings: list = field(default_factory=list)

    @property
    def dr3_ok(self) -> bool:
        return self.found and bool(self.dr3) and not self.dr3_missing

    @property
    def dr3sp_ok(self) -> bool:
        return self.found and bool(self.dr3sp) and not self.dr3sp_missing

    @property
    def unknown(self) -> bool:
        return not self.found

    def source_slot(self) -> int | None:
        """The slot whose DR3 paths all exist (lowest first), else None."""
        for n, s in self.slots.items():
            if s.get("dr3") and all(Path(p).is_file() for p in s["dr3"]):
                return n
        return None

    def lacking(self) -> list[int]:
        """Readable slots with no Gaia DR3 (astrometric) paths."""
        return [n for n, s in self.slots.items() if not s.get("error") and not s.get("dr3")]

    def solve_block_reason(self) -> str:
        """Why a finish must not plate-solve ('' = it may, or unknown)."""
        if self.unknown:
            return ""
        if not self.dr3:
            return (f"Gaia DR3 (astrometric) not set in PixInsight slot {self.slot} "
                    f"(core-{self.slot:03d}-pxi.settings)")
        if self.dr3_missing:
            return (f"Gaia DR3 files missing on disk ({len(self.dr3_missing)} of {len(self.dr3)}, "
                    f"e.g. {self.dr3_missing[0]})")
        return ""


def check(d: Path | None = None, *, running_count: int | None = None, exists=None) -> GaiaStatus:
    """Read every slot file (read-only) and judge the automation slot."""
    exists = exists or (lambda p: Path(p).is_file())
    d = Path(d) if d else settings_dir()
    files = slot_files(d)
    if files and running_count is None:
        from photonscript.integration.runner import pixinsight_running
        running_count = len(pixinsight_running())
    st = GaiaStatus(settings_dir=d, slot=automation_slot(running_count or 0))
    for n, f in files.items():
        try:
            g = gaia_paths(f)
            st.slots[n] = {"file": str(f), "dr3": g["DR3"], "dr3sp": g["DR3SP"], "error": ""}
        except ValueError as e:
            st.slots[n] = {"file": str(f), "dr3": [], "dr3sp": [], "error": str(e)}
    if not st.slots:
        st.warnings.append(f"no PixInsight settings (core-00N-pxi.settings) in {d}: Gaia not checked")
        return st
    me = st.slots.get(st.slot)
    if me is None or me.get("error"):
        st.warnings.append(f"PixInsight slot {st.slot} settings "
                           + ("not found" if me is None else f"unreadable ({me['error']})")
                           + ": Gaia not checked for the automation instance")
        return st
    st.found = True
    st.dr3, st.dr3sp = list(me["dr3"]), list(me["dr3sp"])
    st.dr3_missing = [p for p in st.dr3 if not exists(p)]
    st.dr3sp_missing = [p for p in st.dr3sp if not exists(p)]
    if st.slot != 1:
        st.warnings.append(f"the automation PixInsight would use slot {st.slot} "
                           f"({running_count} instance(s) running); the Gaia paths are set in "
                           + (", ".join(f"core-{n:03d}" for n, s in st.slots.items() if s["dr3"] or s["dr3sp"])
                              or "no slot"))
    why = st.solve_block_reason()
    if why:
        st.warnings.append(why + ": a plate solve would fall back to an online VizieR download "
                                 "(the 2026-10-14 hang)")
    if st.found and (not st.dr3sp or st.dr3sp_missing):
        st.warnings.append("Gaia DR3/SP " + ("not set" if not st.dr3sp else
                                             f"files missing ({len(st.dr3sp_missing)})")
                           + f" in slot {st.slot}: SPCC falls back to BN + ColorCalibration")
    lack = [n for n in st.lacking() if n != st.slot]
    if lack and st.source_slot() is not None:
        st.warnings.append("no Gaia DR3 paths in slot(s) " + ", ".join(str(n) for n in lack)
                           + f" (only core-{st.source_slot():03d}): an instance started there "
                           "would solve online; `photonscript pixinsight-gaia-check --fix` copies them")
    return st


def preflight(echo=print, *, d: Path | None = None, running_count: int | None = None) -> dict:
    """For a finish about to plate-solve: {"solve": bool, "why": str,
    "warnings": [...]}. solve False only when the settings were read and
    DR3 is not usable; unknown keeps the solve. Never raises."""
    try:
        st = check(d, running_count=running_count)
    except Exception as e:  # noqa: BLE001 - a preflight must not stop a finish
        echo(f"  Gaia check skipped ({e})")
        return {"solve": True, "why": "", "warnings": [f"Gaia check skipped ({e})"]}
    for w in st.warnings:
        echo("  Gaia: " + w)
    why = st.solve_block_reason()
    return {"solve": not why, "why": why, "warnings": list(st.warnings), "slot": st.slot,
            "dr3": len(st.dr3), "dr3sp": len(st.dr3sp)}


# ----------------------------------------------------------------- --fix

def _span(text: str, start: int, end: int, key: str) -> tuple[int, int, int] | None:
    """(open_start, content_start, close_end) of the direct child <i k=key>
    between text[start:end] (one nesting level down)."""
    depth = 0
    pos = start
    while True:
        m = _TAG.search(text, pos, end)
        if not m:
            return None
        if m.group(0) == "</i>":
            depth -= 1
            if depth < 0:
                return None
        elif m.group(2) == "/":
            if depth == 0 and m.group(1) == key:
                return (m.start(), m.end(), m.end())   # self-closing: empty
        else:
            if depth == 0 and m.group(1) == key:
                cs = m.end()
                d2, p2 = 0, cs
                while True:
                    m2 = _TAG.search(text, p2, end)
                    if not m2:
                        return None
                    if m2.group(0) == "</i>":
                        if d2 == 0:
                            return (m.start(), cs, m2.end())
                        d2 -= 1
                    elif m2.group(2) != "/":
                        d2 += 1
                    p2 = m2.end()
            depth += 1
        pos = m.end()


def _children(text: str, start: int, end: int) -> list[tuple[str, int]]:
    """(key, open_start) of the direct <i> children in text[start:end]."""
    out, depth, pos = [], 0, start
    while True:
        m = _TAG.search(text, pos, end)
        if not m:
            return out
        if m.group(0) == "</i>":
            depth -= 1
        else:
            if depth == 0:
                out.append((m.group(1), m.start()))
            if m.group(2) != "/":
                depth += 1
        pos = m.end()


def gaia_values_xml(text: str) -> str:
    """The <v k="DR3...DatabaseFilePathNN"> elements of ModuleData/Gaia, verbatim."""
    root = _root_span(text)
    md = _span(text, root[0], root[1], "ModuleData")
    if not md:
        return ""
    g = _span(text, md[1], md[2] - len("</i>"), "Gaia")
    if not g or g[1] == g[2]:
        return ""
    body_end = g[2] - len("</i>")
    # only the direct <v> children (not those of <i k="Interfaces">)
    vals, depth, pos = [], 0, g[1]
    for m in re.finditer(r'<i k="[^"]*"\s*/>|<i k="[^"]*"\s*>|</i>|' + _V_KEY.pattern, text[g[1]:body_end]):
        t = m.group(0)
        if t.startswith("<v"):
            if depth == 0:
                vals.append(t)
        elif t == "</i>":
            depth -= 1
        elif not t.endswith("/>"):
            depth += 1
    return "".join(vals)


def _root_span(text: str) -> tuple[int, int]:
    m = re.search(r"<xsdt\b[^>]*>", text)
    e = text.rfind("</xsdt>")
    if not m or e < 0:
        raise GaiaFixError("not an XSDT settings file (no <xsdt> root)")
    return m.end(), e


def splice_gaia(target: str, values: str) -> str:
    """target text with ModuleData/Gaia's DR3 / DR3SP path values replaced
    by `values` (a Gaia block is created, in key order, when missing)."""
    if not values:
        raise GaiaFixError("no Gaia values to copy")
    r0, r1 = _root_span(target)
    md = _span(target, r0, r1, "ModuleData")
    if not md:
        raise GaiaFixError("no ModuleData block in the target settings")
    if md[1] == md[2]:
        raise GaiaFixError("empty (self-closing) ModuleData block in the target settings")
    md_body_end = md[2] - len("</i>")
    g = _span(target, md[1], md_body_end, "Gaia")
    if g and g[1] != g[2]:
        g_end = g[2] - len("</i>")
        body = target[g[1]:g_end]
        # drop the old path values at the Gaia level only, then put the new ones first
        kept, depth, last = [], 0, 0
        for m in re.finditer(r'<i k="[^"]*"\s*/>|<i k="[^"]*"\s*>|</i>|' + _V_KEY.pattern, body):
            t = m.group(0)
            if t.startswith("<v"):
                if depth == 0:
                    kept.append(body[last:m.start()])
                    last = m.end()
            elif t == "</i>":
                depth -= 1
            elif not t.endswith("/>"):
                depth += 1
        kept.append(body[last:])
        return target[:g[1]] + values + "".join(kept) + target[g_end:]
    if g:   # self-closing <i k="Gaia"/>
        return target[:g[0]] + '<i k="Gaia">' + values + "</i>" + target[g[2]:]
    at = md_body_end
    for k, s in _children(target, md[1], md_body_end):
        if k > "Gaia":
            at = s
            break
    return target[:at] + '<i k="Gaia">' + values + "</i>" + target[at:]


def fix(d: Path | None = None, *, running=None, now: datetime | None = None, echo=print) -> list[dict]:
    """Copy the Gaia keys of the slot that has them into every other slot
    file lacking them. Refused while any PixInsight runs. Each changed file
    is backed up first. Returns [{"slot", "file", "backup"}]."""
    if running is None:
        from photonscript.integration.runner import pixinsight_running as running
    busy = running()
    if busy:
        raise GaiaFixError(f"PixInsight is running (pid {', '.join(map(str, busy))}): close it first "
                           "(it rewrites its settings on exit)")
    st = check(d, running_count=0)
    src = st.source_slot()
    if src is None:
        raise GaiaFixError("no slot has a complete Gaia DR3 set on disk to copy from")
    src_text = Path(st.slots[src]["file"]).read_bytes().decode("utf-8")
    values = gaia_values_xml(src_text)
    if not values:
        raise GaiaFixError(f"slot {src}: no Gaia values found to copy")
    want = gaia_paths(Path(st.slots[src]["file"]))
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    done = []
    for n in st.lacking():
        f = Path(st.slots[n]["file"])
        raw = f.read_bytes()
        new = splice_gaia(raw.decode("utf-8"), values)
        tmp = f.with_name(f.name + ".tmp-gaia")
        tmp.write_bytes(new.encode("utf-8"))
        try:
            if gaia_paths(tmp) != want:
                raise GaiaFixError(f"{f.name}: the spliced file does not read back the Gaia paths")
            bak = f.with_name(f"{f.name}.bak-{stamp}-gaia")
            shutil.copy2(f, bak)
            os.replace(tmp, f)
        finally:
            if tmp.exists():
                tmp.unlink()
        echo(f"  slot {n}: Gaia paths copied from slot {src} into {f.name} (backup {bak.name})")
        done.append({"slot": n, "file": str(f), "backup": str(bak)})
    return done


def report_lines(st: GaiaStatus) -> list[str]:
    out = [f"PixInsight settings: {st.settings_dir}"]
    for n, s in st.slots.items():
        mark = " <- next automation instance" if n == st.slot else ""
        if s.get("error"):
            out.append(f"  slot {n}: unreadable ({s['error']}){mark}")
        else:
            out.append(f"  slot {n}: DR3 {len(s['dr3'])} file(s), DR3/SP {len(s['dr3sp'])} file(s){mark}")
    if st.found:
        out.append(f"Gaia DR3 (astrometric, plate solve): "
                   + ("OK" if st.dr3_ok else "NOT USABLE") + f", {len(st.dr3)} file(s)"
                   + (f", {len(st.dr3_missing)} missing" if st.dr3_missing else "")
                   + (f" in {Path(st.dr3[0]).parent}" if st.dr3 else ""))
        out.append(f"Gaia DR3/SP (SPCC): " + ("OK" if st.dr3sp_ok else "NOT USABLE")
                   + f", {len(st.dr3sp)} file(s)"
                   + (f", {len(st.dr3sp_missing)} missing" if st.dr3sp_missing else "")
                   + (f" in {Path(st.dr3sp[0]).parent}" if st.dr3sp else ""))
    for w in st.warnings:
        out.append("WARNING: " + w)
    return out
