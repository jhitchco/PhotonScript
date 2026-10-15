"""Finish-only retry for autointegrate (2026-10-14: the M31 Piggy v6
integrate, 200 min, succeeded; its finish hung on the plate solve; the run
was archived as "Integrated 23.4 h (v6)" with no final image, and the next
cycle would not redo it: the last run was under 12 h old and nothing new
had arrived).

A run needs a finish-only retry when, newest of its goal + rig and at most
MAX_AGE_H old:
  * its ledger says the integration made it (machine.integration.ok) and
    the run has masters (out/master/master_<F>.xisf);
  * nothing finished it: no final JPG in its own out/final, in a re-finish
    nested in it (out/<x>/out/final) or in a Staging/Finish folder whose
    ledger names it (blend.final_dirs). A mono run with color items wants
    a color JPG (<target>_LRGB / _RGB / _SHO / _HOO), else a *_final.jpg;
  * the finish failed (machine.finish.ok False), or the run was started by
    the watcher / autointegrate and has no finish record (a hand
    `integrate --no-finish` is left alone);
  * fewer than `max_retries` retries are recorded in its ledger
    (machine.finish_retry.attempts).

The retry runs the finish only, into a NEW Staging/Finish folder
(`photonscript finish` for an OSC master, `finish --mono-run` color for a
mono run with color items): the 1st attempt as integrate would, the 2nd
without the plate solve (finish.Options.solve False: a solve that hung
once is not given a second hour). When the run's own finish (or color
combine) was closed by the runner's hang watchdog (machine.finish.hung /
machine.color.hung, "hung: no log progress N min in <stage>"), the 1st
retry already goes without the solve. An OSC run whose core check errored
(machine.hdr.long_check.error) and has its short master but no
master_<S>_hdr gets the OSC core combine again first (osc_hdr.run).
Every attempt goes into the run's ledger (machine.finish_retry), and a
successful one sets machine.final / machine.finals (the JPG list the scope
shows); the ledger is then marked unreported so it is posted again (a
re-post of the same run replaces it on the scope, same version, no ping).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAX_AGE_H = 72.0
RETRY_KEY = "finish_retry"
AUTO_TRIGGERS = ("integrate-watch", "autointegrate")


@dataclass
class Need:
    run_dir: Path
    ledger: Path
    target: str
    rig: str
    version: int
    kind: str                          # finish (one master) | color (mono color items)
    attempt: int                       # the attempt this retry would be (1-based)
    max_retries: int
    master: Path | None = None         # kind finish: the master the finish takes
    core: bool = False                 # OSC: re-run the core combine first
    solve: bool = True
    why: str = ""
    exhausted: bool = False
    history: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.target} [{self.rig}] v{self.version} ({self.run_dir.name})"


# ----------------------------------------------------------------- finished?

def run_jpgs(run_dir: Path, roots=()) -> list[Path]:
    """Every final JPG of a run across its finish folders (blend.final_dirs):
    color images first (LRGB, RGB, SHO, HOO), then *_final, *_vivid, *_hoo,
    *_gold; within a kind the newest folder first."""
    from photonscript.integration import blend as bl
    from photonscript.integration.mono_color import FINAL_ORDER
    dirs = sorted(bl.final_dirs(run_dir, roots), key=lambda d: bl._mtime(d), reverse=True)
    out: list[Path] = []
    seen: set[str] = set()
    pats = [f"*_{k}.jpg" for k in FINAL_ORDER] + ["*_final.jpg", "*_vivid.jpg", "*_hoo.jpg", "*_gold.jpg"]
    for pat in pats:
        for d in dirs:
            for p in sorted(d.glob(pat)):
                if pat.startswith("*_") and pat[2:-4] in FINAL_ORDER and not p.name.endswith(pat[1:]):
                    continue            # Windows glob is case-blind: _hoo matched _HOO
                k = str(p).lower()
                if k not in seen:
                    seen.add(k)
                    out.append(p)
    return out


def color_jpgs(jpgs: list[Path]) -> list[Path]:
    from photonscript.integration.mono_color import FINAL_ORDER
    return [p for p in jpgs if any(p.name.endswith(f"_{k}.jpg") for k in FINAL_ORDER)]


def _color_items(run_dir: Path, target: str) -> list[dict]:
    from photonscript.integration import mono_color
    from photonscript.integration.pipeline import safe_name
    try:
        items, _ = mono_color.plan(mono_color.run_masters(run_dir), safe_name(target))
    except Exception:  # noqa: BLE001 - no plan = no color wanted
        return []
    return items


def _age_h(iso: str, now: datetime) -> float | None:
    try:
        t = datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - t).total_seconds() / 3600.0


def _core_redo(run_dir: Path, machine: dict) -> bool:
    """An OSC run whose core check errored, with the short master on disk
    and no master_<S>_hdr yet."""
    h = machine.get("hdr") or {}
    if h.get("kind") != "osc_core":
        return False
    err = (h.get("long_check") or {}).get("error")
    if not err:
        return False
    mdir = Path(run_dir) / "out" / "master"
    shorts = list((mdir / "osc_hdr").glob("master_*_short.xisf")) if (mdir / "osc_hdr").is_dir() else []
    if not shorts:
        return False
    for s in shorts:
        stack = s.stem[len("master_"):-len("_short")]
        if not (mdir / f"master_{stack}_hdr.xisf").is_file():
            return True
    return False


def osc_master(run_dir: Path) -> Path | None:
    """The master an OSC finish takes: master_OSC_hdr (the core combine)
    when it exists, else master_OSC."""
    mdir = Path(run_dir) / "out" / "master"
    for n in ("master_OSC_hdr.xisf", "master_OSC.xisf"):
        if (mdir / n).is_file():
            return mdir / n
    return None


def need_for(ledger_path: Path, led, *, roots=(), max_retries: int = 2,
             now: datetime | None = None) -> Need | None:
    """A Need when this run wants a finish-only retry (module doc), one with
    exhausted=True when it would but the retries are used up, else None."""
    from photonscript.integration import blend as bl
    now = now or datetime.now(timezone.utc)
    if max_retries <= 0 or led.kind != "integration":
        return None
    m = led.machine or {}
    run_dir = Path(ledger_path).parent
    if (m.get("integration") or {}).get("ok") is not True:
        return None
    age = _age_h(led.created_at, now)
    if age is None or age > MAX_AGE_H:
        return None
    masters = bl.masters(run_dir)
    if not masters:
        return None
    fin_ok = (m.get("finish") or {}).get("ok")
    by = (m.get("trigger") or {}).get("by", "")
    jpgs = run_jpgs(run_dir, roots)
    mono = "OSC" not in masters
    items = _color_items(run_dir, led.campaign) if mono else []
    if items:
        if color_jpgs(jpgs):
            return None
        kind, master = "color", None
        why = "no color image (" + ", ".join(it["name"] for it in items) + ")"
        if fin_ok is None and not (m.get("color") or {}) and by not in AUTO_TRIGGERS:
            return None
    else:
        if any(p.name.endswith("_final.jpg") for p in jpgs):
            return None
        if fin_ok is None and by not in AUTO_TRIGGERS:
            return None
        kind = "finish"
        master = osc_master(run_dir) if not mono else masters.get("L") or next(iter(masters.values()))
        why = "finish failed" if fin_ok is False else "no finished image"
    hist = list(((m.get(RETRY_KEY) or {}).get("attempts")) or [])
    n = len(hist)
    hung = (m.get("finish") or {}).get("hung") or (m.get("color") or {}).get("hung") or ""
    if hung:
        why += f" ({hung})"
    need = Need(run_dir=run_dir, ledger=Path(ledger_path), target=led.campaign, rig=led.rig,
                version=led.version, kind=kind, attempt=n + 1, max_retries=max_retries,
                master=master, core=(not mono and _core_redo(run_dir, m)),
                solve=(n == 0 and not hung), why=why, history=hist)
    if n >= max_retries:
        need.exhausted = True
        need.why = f"{why}; {n} finish-only retr{'y' if n == 1 else 'ies'} used (autointegrate_finish_retries)"
    return need


def newest_runs(staging_root: Path, scratch_root: Path | None = None) -> list[tuple[Path, object]]:
    """(ledger path, Ledger) of the newest integration ledger per goal + rig
    in the staging (and scratch) root."""
    from photonscript.integration import ledger as writer
    leds = [(f, x) for f, x in writer.local_ledgers(staging_root, scratch_root) if x.kind == "integration"]
    leds.sort(key=lambda fx: (fx[1].created_at, fx[1].version), reverse=True)
    out: list[tuple[Path, object]] = []
    for f, x in leds:
        if any(y.rig == x.rig and writer.same_campaign(y.campaign, x.campaign) for _g, y in out):
            continue
        out.append((f, x))
    return out


def needs(staging_root: Path, scratch_root: Path | None = None, *, roots=(), max_retries: int = 2,
          now: datetime | None = None) -> list[Need]:
    """Every newest-run Need (exhausted ones included), oldest run first."""
    out = []
    for f, led in newest_runs(staging_root, scratch_root):
        n = need_for(f, led, roots=roots, max_retries=max_retries, now=now)
        if n is not None:
            out.append(n)
    return sorted(out, key=lambda n: n.version)


def pending_for(needs_list: list[Need], target: str, rig: str | None = None) -> Need | None:
    """The not-exhausted Need of this target (and rig), else None."""
    from photonscript.integration.ledger import same_campaign
    for n in needs_list:
        if not n.exhausted and same_campaign(n.target, target) and (rig is None or n.rig == rig):
            return n
    return None


# ----------------------------------------------------------------- run + record

def record(need: Need, entry: dict, jpgs: list[Path]) -> None:
    """Append one attempt to the run's ledger; a successful one also sets
    final (when empty) and finals, and marks the ledger unreported so it is
    posted again."""
    from photonscript.shared import ledger as L
    led = L.load(need.ledger)
    m = led.machine
    blk = m.setdefault(RETRY_KEY, {})
    blk.setdefault("attempts", []).append(entry)
    blk["max"] = need.max_retries
    if entry.get("ok"):
        blk["ok"] = True
        if jpgs:
            if not m.get("final"):
                m["final"] = str(jpgs[0])
            m["finals"] = [str(p) for p in jpgs]
        led.reported = False
    L.save(need.ledger, led)


def run(need: Need, *, run_finish, run_color, run_core=None, roots=(), echo=print) -> dict:
    """One finish-only retry. run_finish(master, need) -> finish.run result,
    run_color(need) -> finish.run_mono_color result, run_core(need) -> the
    osc_hdr.run block. Never raises for a PixInsight failure: the attempt is
    recorded either way (an exception is recorded, then re-raised)."""
    entry = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "attempt": need.attempt,
             "kind": need.kind, "solve": need.solve, "why": need.why, "ok": False}
    jpgs: list[Path] = []
    try:
        master = need.master
        if need.core and run_core is not None:
            rh = run_core(need) or {}
            entry["core"] = {k: rh.get(k) for k in ("status", "why", "made", "ok")}
            if rh.get("made"):
                master = osc_master(need.run_dir) or master
        if need.kind == "color":
            res = run_color(need) or {}
            ok = bool((res.get("color") or {}).get("ok") or (res.get("color") or {}).get("made"))
        else:
            res = run_finish(master, need) or {}
            ok = bool((res.get("finish") or {}).get("ok"))
            entry["master"] = str(master) if master else ""
        entry["run_dir"] = res.get("run_dir", "")
        entry["minutes"] = (res.get("finish") or res.get("color") or {}).get("minutes")
        if (res.get("finish") or res.get("color") or {}).get("hung"):
            entry["hung"] = (res.get("finish") or res.get("color") or {}).get("hung")
        jpgs = run_jpgs(need.run_dir, roots)
        entry["ok"] = ok and bool(jpgs)
        entry["finals"] = [str(p) for p in jpgs]
    except Exception as e:  # noqa: BLE001 - record it, then let the cycle report it
        entry["error"] = str(e)
        record(need, entry, [])
        raise
    record(need, entry, jpgs)
    echo(f"  finish retry {need.attempt}/{need.max_retries} of {need.label}: "
         + ("ok, " + ", ".join(p.name for p in jpgs[:4]) if entry["ok"]
            else "FAILED" + (f" ({entry.get('run_dir')})" if entry.get("run_dir") else "")))
    return {"need": need, "entry": entry, "jpgs": jpgs, "result": res}


# ----------------------------------------------------------------- OSC core again

_CONFIG_LINE = re.compile(r"^var CONFIG = (\{.*\});\s*$", re.M)


def integrate_stacks(run_dir: Path) -> list[dict]:
    """The stacks of a run's integrate_run.js CONFIG (with short_subset)."""
    try:
        text = (Path(run_dir) / "integrate_run.js").read_text(encoding="ascii", errors="replace")
    except OSError:
        return []
    m = _CONFIG_LINE.search(text)
    if not m:
        return []
    try:
        return list(json.loads(m.group(1)).get("stacks") or [])
    except ValueError:
        return []


def stack_counts(run_dir: Path, stack: dict) -> dict[float, int]:
    """exposure -> staged lights of this stack's filter (manifest frames)."""
    try:
        man = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return {}
    exps = {round(float(g.get("exp") or 0), 2) for g in stack.get("groups") or []}
    out: dict[float, int] = {}
    for f in man.get("frames") or []:
        if f.get("role") != "light" or not f.get("staged"):
            continue
        if str(f.get("filter", "")) != str(stack.get("filter", "")):
            continue
        e = round(float(f.get("exp_s") or 0), 2)
        if e in exps:
            out[e] = out.get(e, 0) + 1
    return out


def core_combine(need: Need, *, exe: str, echo=print, core_radius_arcmin: float = 1.0,
                 warn_px: int = 25, check=None) -> dict:
    """The OSC core combine of a run again (osc_hdr.run): the stack with a
    short subset from its integrate_run.js, the light counts from its
    manifest, the core radius from the run's optics. Never raises."""
    from types import SimpleNamespace

    from photonscript.integration import osc_hdr
    from photonscript.integration.finish import RIG_OPTICS, source_finish_values
    try:
        stacks = [s for s in integrate_stacks(need.run_dir) if s.get("short_subset")]
        if not stacks:
            return {"status": "skipped", "why": "no short subset in integrate_run.js", "made": []}
        st = stacks[0]
        vals = source_finish_values(need.run_dir)
        focal = vals.get("FOCAL_MM") or RIG_OPTICS.get(need.rig, RIG_OPTICS["piggyback"])[0]
        pixel = vals.get("PIXEL_UM") or RIG_OPTICS.get(need.rig, RIG_OPTICS["piggyback"])[1]
        from photonscript.shared.core_sat import radius_arcmin
        r_am = radius_arcmin(SimpleNamespace(qa_core_radius_arcmin=core_radius_arcmin), need.target)
        r_px = r_am * 60.0 / (206.265 * float(pixel) / float(focal))
        return osc_hdr.run(need.run_dir, st, stack_counts(need.run_dir, st), r_core_px=r_px,
                           warn_px=warn_px, exe=exe, echo=echo, check=check,
                           target=need.target)
    except Exception as e:  # noqa: BLE001 - the finish runs on master_OSC without it
        return {"status": "failed", "why": str(e), "made": []}
