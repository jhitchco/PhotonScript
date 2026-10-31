"""PS-161: desktop auto-integrate (`photonscript autointegrate`).

integrate-watch (PS-31) decides WHEN a goal is worth a run (goal met, or
new approved hours since its last ledger). This job adds what an unattended
desktop needs around that decision, one cycle per Scheduled Task tick
(deploy/install-autointegrate-task.ps1, every 30 min, --once):

  1. the integrate-watch lock (the two never overlap), the queued-ledger
     sweep, the morning summary once a day after autointegrate_summary_hour
     (one line per goal: stacked / finished / blended in the last night,
     the final JPGs; <staging>/Summaries/<date>.txt + Pushover), and
     nothing at all while PixInsight is open (Jeremy working);
  2. the run window (run_window.py, autointegrate_window_local, default
     08:00-23:00) and the locked-session check (autointegrate_lock_check):
     outside it, or with the Windows session locked, the cycle logs
     "deferred: ..." and starts nothing (PixInsight crashed in
     Qt6WebEngineCore on every overnight run); the next tick inside the
     window picks the work up; then the desktop-load guard: no PixInsight
     start while the desktop had keyboard / mouse input in the last
     autointegrate_idle_min minutes or its CPU is above
     autointegrate_max_cpu_pct (next cycle);
  3. the candidates from the scheduler and integrate-watch's decide()
     (both rigs watched by default; the RC16 with its own first-run and
     new-data hours, watch.rig_threshold); no re-integration unless new
     approved data arrived (idempotent);
  4. Syncthing settled: a goal that wants a run only runs when its Library
     folders (this rig's filter folders) hold no Syncthing temp file
     (~syncthing~*.tmp / .syncthing.*.tmp) and their file count + total size
     have not changed for autointegrate_settle_min minutes (state kept in
     <staging>/.autointegrate-state.json; Syncthing keeps the source
     modification times, so mtimes cannot tell);
  5. at most one `photonscript integrate` run (OSC: natural color, plus the
     HOO-mapped image with autointegrate_hoo; RC16: one master per filter,
     the HDR combine of the short L, the finish and the LRGB / SHO color),
     the final JPG list stamped into its ledger (machine.finals), the
     ledger posted (queued when the scope is down);
  6. else at most one finish-only retry (finish_retry.py): a newest run with
     a master but no finished image gets its finish (or mono color) again,
     at most autointegrate_finish_retries times, recorded in its ledger;
  7. two-rig goals: `photonscript blend` (PS-153) once both rigs have
     masters and an input is newer than the last blend's (blend_status:
     the source integrate version, a re-finish counts as its run's; runs
     and re-finishes found under every root, blend.discovery_roots); a
     blend waits while a finish retry of the goal is pending; a narrowband
     blend also waits until the RC16 run integrated blend_nb_min_ha_h of Ha
     (nb_gate: a weak Ha master makes a weak blend);
  8. a review JPG (<run>/review.jpg, the finals side by side, at most
     2048 px) sent with Pushover (autointegrate_notify).

PixInsight runs one instance at a time (runner.run_script refuses to start a
second one); every run gets a NEW folder; the Library mirror is only read.
--dry-run prints the decisions (settle verdicts included), runs nothing,
posts nothing and writes no state.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from photonscript.integration import finish_retry as fr
from photonscript.integration import report, runner
from photonscript.integration import run_window as rw
from photonscript.integration import watch as w

STATE_NAME = ".autointegrate-state.json"
TEMP_PREFIXES = ("~syncthing~", ".syncthing.")
REVIEW_NAME = "review.jpg"
REVIEW_MAX_PX = 2048
SUMMARY_DIR = "Summaries"


@dataclass
class AutoOptions:
    watch: w.WatchOptions
    library: Path
    settle_min: float = 15.0
    blend: bool = True
    notify: bool = True
    hoo: bool = True
    interval_min: float = 30.0
    extra: dict = field(default_factory=dict)
    # narrowband blend gate (config blend_nb_gate / blend_nb_min_*_h): an NB
    # blend whose RC16 Ha master is thinner than nb_min_ha_h is skipped
    nb_gate: bool = True
    nb_min_ha_h: float = 2.0
    nb_min_oiii_h: float = 1.5
    nb_min_sii_h: float = 1.5
    # the CLI fills these from the config (autointegrate_finish_retries 2,
    # autointegrate_idle_min 10, autointegrate_max_cpu_pct 80,
    # autointegrate_summary_hour 8); the bare defaults keep a test cycle
    # free of the real desktop's idle time, CPU and clock
    finish_retries: int = 2           # finish-only retries per run (0 = off)
    idle_min: float = 0.0             # desktop-load guard: minutes without input (0 = off)
    max_cpu_pct: float = 0.0          # desktop-load guard: CPU percent (0 = off)
    summary_hour: float = -1.0        # morning summary after this local hour (< 0 = off)
    extra_roots: list[Path] = field(default_factory=list)   # integration_extra_roots


# --- Syncthing settle ------------------------------------------------------------

def is_sync_temp(name: str) -> bool:
    low = name.lower()
    return any(low.startswith(p) for p in TEMP_PREFIXES) and low.endswith(".tmp")


def folder_snapshot(library: Path, target: str, rig: str) -> dict:
    """Files of this target + rig in the Library mirror (read-only walk):
    {"folders": [...], "count", "bytes", "temp": [names]}."""
    from photonscript.integration.select import LIGHT_EXT, _filter_dirs, target_folders
    out = {"folders": [], "count": 0, "bytes": 0, "temp": []}
    for tdir in target_folders(Path(library), target):
        out["folders"].append(tdir.name)
        try:
            dirs = _filter_dirs(tdir, rig)
        except OSError:
            continue
        for d in [tdir] + dirs:
            try:
                entries = list(d.iterdir())
            except OSError:
                continue
            for p in entries:
                if not p.is_file():
                    continue
                if is_sync_temp(p.name):
                    out["temp"].append(f"{d.name}/{p.name}")
                elif d is not tdir and p.suffix.lower() in LIGHT_EXT:
                    out["count"] += 1
                    try:
                        out["bytes"] += p.stat().st_size
                    except OSError:
                        pass
    return out


def load_state(staging_root: Path) -> dict:
    try:
        return json.loads((Path(staging_root) / STATE_NAME).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return {}


def save_state(staging_root: Path, state: dict) -> None:
    p = Path(staging_root) / STATE_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="ascii")


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def settled(snap: dict, key: str, state: dict, settle_min: float,
            now: datetime) -> tuple[bool, str]:
    """Is this target + rig's Library folder done syncing? Updates
    state["settle"][key] = {"sig": [count, bytes], "since": first seen}."""
    st = state.setdefault("settle", {})
    if not snap["folders"]:
        return False, "no Library folder for this target on the desktop mirror"
    if snap["temp"]:
        st.pop(key, None)
        return False, (f"Syncthing still copying ({len(snap['temp'])} temp file(s), e.g. "
                       f"{snap['temp'][0]})")
    sig = [int(snap["count"]), int(snap["bytes"])]
    prev = st.get(key) or {}
    if prev.get("sig") != sig:
        st[key] = {"sig": sig, "since": _iso(now)}
        if settle_min <= 0:
            return True, f"{sig[0]} files (no settle wait)"
        return False, f"file count changed to {sig[0]}: waiting {settle_min:g} min for it to hold"
    age = w._hours_since(prev.get("since", ""), now)
    age_min = (age or 0.0) * 60.0
    if age_min < settle_min:
        return False, f"{sig[0]} files, stable {age_min:.0f} of {settle_min:g} min"
    return True, f"{sig[0]} files, stable {age_min:.0f} min"


# --- desktop-load guard ----------------------------------------------------------

def idle_minutes() -> float | None:
    """Minutes since the last keyboard / mouse input of this desktop session
    (Windows GetLastInputInfo); None when it cannot be read."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class _LastInput(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        li = _LastInput()
        li.cbSize = ctypes.sizeof(li)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return None
        ms = (ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF
        return ms / 60000.0
    except Exception:  # noqa: BLE001 - no user32 (service session): no idle guard
        return None


def cpu_percent() -> float | None:
    try:
        import psutil
        return float(psutil.cpu_percent(interval=2.0))
    except Exception:  # noqa: BLE001
        return None


def desktop_guard(o: AutoOptions, idle=idle_minutes, cpu=cpu_percent) -> str | None:
    """Why PixInsight must not start now (Jeremy at the desk, or the
    desktop busy), else None. A reading that fails never blocks."""
    if o.idle_min and o.idle_min > 0:
        m = idle()
        if m is not None and m < o.idle_min:
            return (f"desktop in use (input {m:.0f} min ago, < {o.idle_min:g} min, "
                    "autointegrate_idle_min)")
    if o.max_cpu_pct and o.max_cpu_pct > 0:
        c = cpu()
        if c is not None and c >= o.max_cpu_pct:
            return f"desktop busy (CPU {c:.0f}% >= {o.max_cpu_pct:g}%, autointegrate_max_cpu_pct)"
    return None


# --- blend -----------------------------------------------------------------------

def blend_inputs_key(inp) -> list[str]:
    return sorted([str(inp.osc)] + [str(p) for p in inp.rc16.values()])


def _roots(o: AutoOptions) -> list[Path]:
    from photonscript.integration.blend import discovery_roots
    return discovery_roots(o.watch.staging_root, o.watch.scratch_root, o.extra_roots)


def _mtime_of(p) -> float:
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return 0.0


def blend_records(roots, target: str) -> list[dict]:
    """The blends of `target` under <root>/Blend for every root, newest
    first: {name, dir, target, mode, ok, built, inputs: [{role, path, mtime,
    run_version}]} from the blend's manifest.json, else its ledger.json (a
    blend whose manifest is missing, e.g. a hand run)."""
    from photonscript.integration.blend import BLEND_DIR
    from photonscript.integration.ledger import same_campaign
    out = []
    for root in roots:
        try:
            dirs = [d for d in (Path(root) / BLEND_DIR).iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in dirs:
            rec = None
            try:
                m = json.loads((d / "manifest.json").read_text(encoding="ascii"))
                rec = {"target": m.get("target", ""), "mode": m.get("mode") or "l",
                       "ok": (m.get("pixinsight") or {}).get("ok"), "built": str(m.get("built", "")),
                       "inputs": m.get("inputs") or []}
            except (OSError, ValueError):
                try:
                    led = json.loads((d / "ledger.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    led = None
                if led and led.get("kind") == "blend":
                    mm = led.get("machine") or {}
                    rec = {"target": led.get("campaign", ""), "mode": mm.get("mode") or "l",
                           "ok": (mm.get("blend") or {}).get("ok"),
                           "built": str(led.get("created_at", "")), "inputs": mm.get("inputs") or []}
            if rec and same_campaign(str(rec["target"]), target):
                rec.update(name=d.name, dir=d)
                out.append(rec)
    return sorted(out, key=lambda r: (_mtime_of(r["dir"]), r["name"]), reverse=True)


def already_blended(staging_root: Path, target: str, key: list[str],
                    scratch_root: Path | None = None, extra_roots=()) -> str | None:
    """Name of a blend folder under <root>/Blend (staging, fast scratch,
    integration_extra_roots) that used exactly these inputs, else None."""
    from photonscript.integration.blend import discovery_roots
    for r in blend_records(discovery_roots(staging_root, scratch_root, extra_roots), target):
        used = sorted(str(f.get("path", "")) for f in r["inputs"])
        if used == key and r["ok"] is not False:
            return r["name"]
    return None


def source_version(path) -> int | None:
    """The integrate version an input image comes from: the ledger of the
    folder it sits in (an integrate run: its version; a finish: the version
    of the run it was made from), searched up to six folders up; None when
    no ledger says."""
    from photonscript.integration.blend import run_info
    for d in list(Path(path).parents)[:6]:
        try:
            led = json.loads((d / "ledger.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        kind = led.get("kind")
        if kind == "integration":
            return int(led.get("version") or 0) or None
        if kind == "finish":
            inp = (led.get("machine") or {}).get("input") or {}
            v = inp.get("run_version")
            src = inp.get("run") or inp.get("mono_run")
            if v is None and src:
                info = run_info(Path(src))
                v = info.version if info else None
            return int(v) if v else None
        return None
    return None


def _stamp(entries: list[dict]) -> tuple[int | None, float]:
    """(newest source version or None, newest file time) of input records."""
    vs, ts = [], []
    for e in entries:
        v = e.get("run_version")
        if v is None:
            v = source_version(e.get("path", ""))
        if v is not None:
            vs.append(int(v))
        try:
            ts.append(datetime.strptime(str(e.get("mtime", ""))[:19], "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            ts.append(_mtime_of(e.get("path", "")))
    return (max(vs) if vs else None), (max(ts) if ts else 0.0)


def _now_entries(inp) -> dict[str, list[dict]]:
    def ent(p, run):
        t = _mtime_of(p)
        return {"path": str(p), "mtime": datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "",
                "run_version": (getattr(run, "version", None) or None) if run is not None else None}
    return {"osc": [ent(inp.osc, getattr(inp, "osc_run", None))],
            "rc16": [ent(p, getattr(inp, "rc16_run", None)) for p in inp.rc16.values()]}


def blend_status(o: AutoOptions, target: str, inp) -> tuple[str | None, str]:
    """(blend folder that already covers these inputs, why), or (None, why
    a new blend is due). Covered: a blend used exactly these files, or the
    newest successful blend of this mode used inputs no older than these,
    per role (OSC, RC16): by the source integrate version when both sides
    know it (a re-finish counts as its run's version, so re-finishing the
    same master does not re-blend), else by the file time."""
    key = blend_inputs_key(inp)
    mode = getattr(inp, "mode", "l") or "l"
    recs = [r for r in blend_records(_roots(o), target) if r["ok"] is not False and r["mode"] == mode]
    for r in recs:
        if sorted(str(f.get("path", "")) for f in r["inputs"]) == key:
            return r["name"], f"already blended in {r['name']}"
    good = [r for r in recs if r["ok"]]
    if not good:
        return None, "no earlier blend"
    last = good[0]
    cur = _now_entries(inp)
    newer, same = [], []
    for role, label in (("osc", "OSC"), ("rc16", "RC16")):
        cv, ct = _stamp(cur[role])
        lv, lt = _stamp([e for e in last["inputs"] if e.get("role") == role])
        if cv is not None and lv is not None:
            if cv > lv:
                newer.append(f"{label} v{cv} > v{lv}")
            else:
                same.append(f"{label} v{cv}")
        elif ct > lt + 60.0:
            newer.append(f"{label} input newer than {last['name']}'s")
        else:
            same.append(label)
    if newer:
        return None, f"newer input than {last['name']}: " + ", ".join(newer)
    return last["name"], f"already blended in {last['name']} (inputs not newer: {', '.join(same)})"


def blend_candidate(o: AutoOptions, target: str, discover) -> tuple[object | None, str]:
    """(inputs, why) for a blend of `target` now, or (None, why not)."""
    try:
        inp = discover(target)
    except Exception as e:  # noqa: BLE001 - BlendError: a rig has no master yet
        return None, f"no blend: {e}"
    held, depth_note = nb_gate(o, inp)
    if held:
        return None, held
    done, why = blend_status(o, target, inp)
    if done:
        return None, why
    return inp, (f"both rigs have masters, {why}" + (f" ({depth_note})" if depth_note else ""))


def nb_gate(o: AutoOptions, inp) -> tuple[str | None, str]:
    """(skip reason or None, depth note) for a narrowband blend. The RC16
    run's ledger says how much Ha / OIII / SII it integrated; Ha under
    nb_min_ha_h skips the blend (a weak Ha master makes a weak blend).
    OIII / SII under their thresholds only go in the note: the blend uses
    the masters it has. Unknown depth (no ledger, given paths) never
    skips. An L blend is never gated."""
    if getattr(inp, "mode", "l") != "nb":
        return None, ""
    from photonscript.integration.blend import rc16_depth
    depth = rc16_depth(inp)
    if depth is None:
        return None, "RC16 depth unknown (no run ledger): not gated"
    need = {"Ha": o.nb_min_ha_h, "OIII": o.nb_min_oiii_h, "SII": o.nb_min_sii_h}
    bits = []
    for f in ("Ha", "OIII", "SII"):
        h = (depth.get(f) or {}).get("hours", 0.0)
        if f in depth or f == "Ha":
            bits.append(f"{f} {h:.1f}/{need[f]:g} h")
    note = "RC16 " + ", ".join(bits)
    ha_h = (depth.get("Ha") or {}).get("hours", 0.0)
    if o.nb_gate and ha_h < float(o.nb_min_ha_h or 0.0):
        return (f"no blend: narrowband blend waits for RC16 Ha "
                f"{ha_h:.1f} h < {o.nb_min_ha_h:g} h (blend_nb_min_ha_h); "
                + note), note
    return None, note


# --- review image + notice -------------------------------------------------------

def finals(run_dir: Path, blend: bool = False) -> list[Path]:
    """The finished JPGs of a run: a mono run's color images first
    (<target>_LRGB / _RGB / _SHO / _HOO.jpg), then integrate *_final.jpg
    (+ *_vivid.jpg, *_hoo.jpg, *_gold.jpg); or a blend's *_blend.jpg (+ its
    *_blend_vivid.jpg, *_core.jpg), a narrowband blend's *_haRGB.jpg,
    *_haRGB_gold.jpg, *_core_SHO.jpg, *_core_HOO.jpg first.
    make_review shows the first four."""
    from photonscript.integration.mono_color import color_jpgs
    fin = Path(run_dir) / "out" / "final"
    pats = ("*_haRGB.jpg", "*_haRGB_gold.jpg", "*_core_SHO.jpg", "*_core_HOO.jpg",
            "*_blend.jpg", "*_vivid.jpg", "*_core.jpg") if blend \
        else ("*_final.jpg", "*_vivid.jpg", "*_hoo.jpg", "*_gold.jpg")
    out: list[Path] = [] if blend else color_jpgs(Path(run_dir) / "out")
    seen = {p.name.lower() for p in out}
    for pat in pats:
        for p in sorted(fin.glob(pat)):
            if p.name.lower() not in seen:     # Windows glob is case-blind: *_hoo matches _HOO
                seen.add(p.name.lower())
                out.append(p)
    return out


def make_review(images: list[Path], dest: Path, max_px: int = REVIEW_MAX_PX) -> Path | None:
    """Side-by-side review JPG of up to 4 images, at most max_px wide."""
    from PIL import Image
    ims = []
    for p in images[:4]:
        try:
            im = Image.open(p)
            im.load()
            ims.append(im.convert("RGB"))
        except Exception:  # noqa: BLE001 - one unreadable JPG
            continue
    if not ims:
        return None
    cell = max(1, max_px // len(ims))
    for im in ims:
        im.thumbnail((cell, cell))
    wid = sum(im.width for im in ims)
    hgt = max(im.height for im in ims)
    sheet = Image.new("RGB", (wid, hgt), (0, 0, 0))
    x = 0
    for im in ims:
        sheet.paste(im, (x, (hgt - im.height) // 2))
        x += im.width
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest, "JPEG", quality=85)
    return dest


def notice_text(kind: str, target: str, rig: str, res: dict, why: str) -> str:
    if kind == "blend":
        ok = (res.get("blend") or {}).get("ok")
        vivid = (res.get("vivid") or {}).get("ok")
        return (f"Blend {target}: {'done' if ok else 'FAILED'}"
                + (", vivid" if vivid else "")
                + f" ({Path(res.get('run_dir', '')).name}). {why}")
    if kind == "retry":
        e = res.get("entry") or {}
        return (f"Finish retry {e.get('attempt', '?')} of {target} [{rig}]: "
                f"{'done' if e.get('ok') else 'FAILED'}"
                + ("" if e.get("solve", True) else " (no plate solve)")
                + f" ({Path(str(e.get('run_dir') or '')).name}). {why}")
    integ = (res.get("integration") or {})
    fin = (res.get("finish") or {})
    h = res.get("hours_integrated")
    made = (res.get("mono_color") or {}).get("made") or []   # mono: <target>_LRGB, _SHO ...
    return (f"Integrated {target} [{rig}] v{res.get('ledger_version', '?')}: "
            + (f"{h:.1f} h, " if isinstance(h, (int, float)) else "")
            + f"integration {'ok' if integ.get('ok') else 'FAILED'}"
            + (f", finish {'ok' if fin.get('ok') else 'FAILED'}" if fin else "")
            + (", color " + ", ".join(made) if made else "")
            + f" ({Path(res.get('run_dir', '')).name}). {why}")


def pushover_sender(cfg):
    """send(message, image) via the shared rate-limited Pushover client."""
    def send(message: str, image: Path | None) -> bool:
        import asyncio
        from photonscript.shared.pushover import notify
        return bool(asyncio.run(notify(cfg, message, title="PhotonScript integrate",
                                       attachment=str(image) if image else None, kind="story")))
    return send


def stamp_finals(ledger_path, jpgs: list[Path]) -> bool:
    """Put the run's final JPG list into its ledger (machine.finals, and
    machine.final when empty) before it is posted: the scope ledger lists
    every final image. False when there is nothing to stamp."""
    if not ledger_path or not jpgs:
        return False
    from photonscript.shared import ledger as L
    try:
        led = L.load(Path(ledger_path))
    except Exception:  # noqa: BLE001
        return False
    led.machine["finals"] = [str(p) for p in jpgs]
    if not led.machine.get("final"):
        led.machine["final"] = str(jpgs[0])
    L.save(Path(ledger_path), led)
    return True


# --- morning summary -------------------------------------------------------------

def _summary_due(o: AutoOptions, state: dict, local_now: datetime) -> str | None:
    """Today's date (local, ISO) when the morning summary is due, else None."""
    if o.summary_hour is None or o.summary_hour < 0:
        return None
    if local_now.hour + local_now.minute / 60.0 < float(o.summary_hour):
        return None
    day = local_now.date().isoformat()
    return None if (state.get("summary") or {}).get("sent") == day else day


def _maybe_summary(o: AutoOptions, send, echo, local_now: datetime | None) -> dict | None:
    from photonscript.integration import night_summary as ns
    local_now = local_now or datetime.now()
    state = load_state(o.watch.staging_root)
    day = _summary_due(o, state, local_now)
    if day is None:
        return None
    since = datetime.combine(local_now.date() - timedelta(days=1), datetime.min.time()) + timedelta(hours=12)
    goals = ns.collect(_roots(o), since.astimezone(timezone.utc))
    text = ns.render(goals, day)
    echo("== morning summary\n" + text)
    if o.watch.dry_run:
        return {"day": day, "text": text, "dry_run": True}
    path = Path(o.watch.staging_root) / SUMMARY_DIR / f"{day}.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as e:
        echo(f"  (summary not written: {e})")
    sent = False
    if goals and o.notify and send is not None:
        try:
            sent = bool(send(ns.short(goals, day, path), None))
        except Exception as e:  # noqa: BLE001
            echo(f"  (summary Pushover not sent: {e})")
    state.setdefault("summary", {})["sent"] = day
    save_state(o.watch.staging_root, state)
    return {"day": day, "text": text, "file": str(path), "sent": sent, "goals": len(goals)}


# --- one cycle -------------------------------------------------------------------

def cycle(o: AutoOptions, *, run_integrate, run_blend, discover_blend, send=None,
          get=report.http_get_json, post=report.http_post_json,
          pi_running=runner.pixinsight_running, echo=print,
          now: datetime | None = None, alive=w._pid_alive, run_retry=None,
          load_guard=None, local_now: datetime | None = None,
          locked=rw.session_locked) -> dict:
    """One auto-integrate pass (module doc). run_integrate(target, rig,
    trigger) -> pipeline result; run_blend(target) -> blend result;
    discover_blend(target) -> blend inputs (raises when a rig is missing);
    run_retry(need) -> finish_retry.run result (None = no retries);
    load_guard() -> why PixInsight must wait, or None (default
    desktop_guard); send(message, image) -> bool; locked() -> why the
    Windows session is locked, or None (run_window.gate)."""
    wo = o.watch
    now = now or datetime.now(timezone.utc)
    out: dict = {"ran": None, "blend": None, "retry": None, "decisions": [], "sweep": None}
    lock = None if wo.dry_run else w.acquire_lock(wo.staging_root, alive=alive)
    if not wo.dry_run and lock is None:
        echo("another integrate-watch / autointegrate is running; nothing to do")
        out["skipped"] = "locked"
        return out
    qlock = None
    try:
        echo("== queued ledgers")
        out["sweep"] = report.sweep(wo.staging_root, wo.base_url, post=post, echo=echo,
                                    dry_run=wo.dry_run, scratch_root=wo.scratch_root)
        try:
            out["summary"] = _maybe_summary(o, send, echo, local_now)
        except Exception as e:  # noqa: BLE001 - a summary never stops a cycle
            echo(f"  (morning summary failed: {e})")
        busy = pi_running()
        if busy:
            echo(f"PixInsight is running (pid {', '.join(map(str, busy))}): nothing now")
            out["skipped"] = "pixinsight running"
            return out
        # the run window + locked-session check (run_window.py): PixInsight
        # crashed in Qt6WebEngineCore on every overnight run; defer, the next
        # tick inside the window picks the work up
        deferred = rw.gate(wo.run_window, wo.lock_check, local_now, locked=locked)
        if deferred:
            if not wo.dry_run:
                echo(f"{deferred}: no PixInsight now (next tick)")
                out["skipped"] = "deferred"
                out["deferred"] = deferred
                return out
            echo(f"  ({deferred}: a real cycle would wait)")
        guard = (load_guard or (lambda: desktop_guard(o)))()
        if guard:
            if not wo.dry_run:
                echo(f"{guard}: no PixInsight now")
                out["skipped"] = "desktop in use"
                return out
            echo(f"  ({guard}: a real cycle would wait)")
        # the PixInsight queue lock for the whole cycle: the integrate and
        # the blend nest inside it, so no other job runs between them
        qlock, why = w.queue_lock("autointegrate", wo.dry_run, echo)
        if why:
            echo(f"{why}: nothing now")
            out["skipped"] = "pixinsight queue busy"
            return out
        try:
            data = get(wo.base_url.rstrip("/") + "/api/integrations/candidates")
        except (OSError, ValueError) as e:
            echo(f"scheduler unreachable ({e}): no decisions this time")
            out["skipped"] = "scheduler unreachable"
            return out
        thr = w.merged_thresholds(data.get("thresholds") or {}, wo)
        out["thresholds"] = thr
        state = load_state(wo.staging_root)
        cands = data.get("candidates") or []
        decs = []
        for c in cands:
            t, rig = c.get("target", ""), c.get("rig", "")
            dec = w.decide(c, thr, w.local_state(wo.staging_root, t, rig, wo.scratch_root), now=now)
            if dec.run:
                snap = folder_snapshot(o.library, t, rig)
                ok, why = settled(snap, f"{t}|{rig}", state, o.settle_min, now)
                if not ok:
                    dec = w.Decision(False, f"{dec.reason}; but {why}", t, rig, c)
                else:
                    dec.reason = f"{dec.reason}; Syncthing settled ({why})"
            decs.append(dec)
            echo(f"  {'RUN ' if dec.run else 'wait'} {dec.target} [{dec.rig}]: {dec.reason}")
        out["decisions"] = [{"target": x.target, "rig": x.rig, "run": x.run, "reason": x.reason}
                            for x in decs]
        if not wo.dry_run:
            save_state(wo.staging_root, state)
        chosen = w.pick(decs)
        if chosen is not None:
            if wo.dry_run:
                echo(f"dry run: would integrate {chosen.target} [{chosen.rig}] ({chosen.reason})")
                out["would_run"] = {"target": chosen.target, "rig": chosen.rig}
            else:
                out["ran"] = _integrate(o, chosen, thr, run_integrate, post, send, echo)
        else:
            echo("nothing to integrate")
        needs = _retry_needs(o, now, echo)
        if chosen is None:
            out["retry"] = _maybe_retry(o, needs, run_retry, post, send, pi_running, echo)
            if out["retry"] and not wo.dry_run:
                needs = _retry_needs(o, now, lambda s: None)
        if o.blend:
            out["blend"] = _maybe_blend(o, cands, chosen, run_blend, discover_blend, send,
                                        pi_running, echo, needs)
        return out
    finally:
        if qlock is not None:
            qlock.release()
        w.release_lock(lock)


def _integrate(o: AutoOptions, chosen, thr: dict, run_integrate, post, send, echo) -> dict:
    wo = o.watch
    echo(f"== integrate {chosen.target} [{chosen.rig}]: {chosen.reason}")
    w.post_processing(wo, chosen, "start", post=post, echo=echo)
    try:
        try:
            res = run_integrate(chosen.target, chosen.rig, w.trigger_of(chosen, thr))
        except Exception:
            w.record_failure(wo.staging_root, chosen.target, chosen.rig)
            raise
        ran = {"target": chosen.target, "rig": chosen.rig, "run_dir": res.get("run_dir"),
               "ledger": res.get("ledger"), "ok": (res.get("integration") or {}).get("ok")}
        if res.get("ledger") and res.get("run_dir"):
            jpgs = fr.run_jpgs(Path(res["run_dir"]))
            if stamp_finals(res["ledger"], jpgs):
                ran["finals"] = [str(p) for p in jpgs]
        if res.get("ledger"):
            r = report.post_ledger(Path(res["ledger"]), wo.base_url, post=post)
            ran["posted"] = r["ok"]
            echo(("  ledger posted: " + r.get("detail", "")) if r["ok"]
                 else f"  WARNING: ledger queued, not posted ({r['detail']})")
    finally:
        w.post_processing(wo, chosen, "end", post=post, echo=echo)
    ran["review"] = _review_and_send(o, "integrate", chosen.target, chosen.rig, res,
                                     chosen.reason, send, echo)
    return ran


def _retry_needs(o: AutoOptions, now: datetime, echo) -> list:
    if o.finish_retries <= 0:
        return []
    try:
        needs = fr.needs(o.watch.staging_root, o.watch.scratch_root, roots=_roots(o),
                         max_retries=o.finish_retries, now=now)
    except Exception as e:  # noqa: BLE001 - a broken ledger never stops a cycle
        echo(f"  (finish retry check failed: {e})")
        return []
    for n in needs:
        echo(f"  {'gave up' if n.exhausted else 'finish retry due'}: {n.label}: {n.why}")
    return needs


def _maybe_retry(o: AutoOptions, needs: list, run_retry, post, send, pi_running, echo) -> dict | None:
    """At most one finish-only retry per cycle (the oldest pending run)."""
    todo = [n for n in needs if not n.exhausted]
    if not todo or run_retry is None:
        return None
    n = todo[0]
    what = ("color" if n.kind == "color" else "finish") + ("" if n.solve else " without the plate solve") \
        + (" (after the OSC core combine)" if n.core else "")
    if o.watch.dry_run:
        echo(f"dry run: would retry the {what} of {n.label} (attempt {n.attempt}/{n.max_retries})")
        return {"target": n.target, "rig": n.rig, "would_run": True, "attempt": n.attempt}
    busy = pi_running()
    if busy:
        echo("  PixInsight is running: finish retry next time")
        return {"target": n.target, "skipped": "pixinsight running"}
    echo(f"== finish retry {n.attempt}/{n.max_retries}: {what} of {n.label} ({n.why})")
    res = run_retry(n)
    e = res.get("entry") or {}
    done = {"target": n.target, "rig": n.rig, "run_dir": str(n.run_dir), "attempt": n.attempt,
            "ok": bool(e.get("ok")), "finish_dir": e.get("run_dir", ""),
            "finals": e.get("finals") or []}
    if e.get("ok"):
        r = report.post_ledger(n.ledger, o.watch.base_url, post=post)
        done["posted"] = r["ok"]
        echo(("  ledger re-posted with the finals: " + r.get("detail", "")) if r["ok"]
             else f"  WARNING: ledger queued, not posted ({r['detail']})")
    review = None
    jpgs = [Path(p) for p in done["finals"]]
    if jpgs and e.get("run_dir"):
        try:
            review = make_review(jpgs, Path(e["run_dir"]) / REVIEW_NAME)
        except Exception as ex:  # noqa: BLE001
            echo(f"  review JPG not made ({ex})")
    done["review"] = str(review) if review else None
    if o.notify and send is not None:
        try:
            send(notice_text("retry", n.target, n.rig, res, n.why), review)
        except Exception as ex:  # noqa: BLE001
            echo(f"  (Pushover not sent: {ex})")
    return done


def _review_and_send(o: AutoOptions, kind: str, target: str, rig: str, res: dict,
                     why: str, send, echo) -> str | None:
    run_dir = res.get("run_dir")
    review = None
    if run_dir:
        try:
            review = make_review(finals(Path(run_dir), blend=(kind == "blend")),
                                 Path(run_dir) / REVIEW_NAME)
        except Exception as e:  # noqa: BLE001 - a run without a picture still reports
            echo(f"  review JPG not made ({e})")
    if review:
        echo(f"  review: {review}")
    if o.notify and send is not None:
        try:
            send(notice_text(kind, target, rig, res, why), review)
        except Exception as e:  # noqa: BLE001
            echo(f"  (Pushover not sent: {e})")
    return str(review) if review else None


def _maybe_blend(o: AutoOptions, cands: list[dict], chosen, run_blend, discover_blend, send,
                 pi_running, echo, needs: list | None = None) -> dict | None:
    """At most one blend per cycle: the target just integrated first, then
    any goal the candidates list for both rigs. A goal with a finish retry
    still pending waits (its blend would take the older finish)."""
    rigs_by: dict[str, set] = {}
    for c in cands:
        rigs_by.setdefault(c.get("target", ""), set()).add(c.get("rig", ""))
    order = []
    if chosen is not None:
        order.append(chosen.target)
    order += [t for t, rs in sorted(rigs_by.items())
              if {"piggyback", "rc16"} <= rs and t not in order]
    for t in order:
        pend = fr.pending_for(needs or [], t)
        if pend is not None:
            echo(f"  blend {t}: waits for the finish retry of {pend.label}")
            continue
        inp, why = blend_candidate(o, t, discover_blend)
        echo(f"  blend {t}: {why}")
        if inp is None:
            continue
        if o.watch.dry_run:
            echo(f"dry run: would blend {t}")
            return {"target": t, "would_run": True}
        busy = pi_running()
        if busy:
            echo("  PixInsight is running: blend next time")
            return {"target": t, "skipped": "pixinsight running"}
        res = run_blend(t)
        done = {"target": t, "run_dir": res.get("run_dir"),
                "ok": (res.get("blend") or {}).get("ok"),
                "vivid": (res.get("vivid") or {}).get("ok")}
        done["review"] = _review_and_send(o, "blend", t, "piggyback+rc16", res, why, send, echo)
        return done
    return None


def loop(o: AutoOptions, *, once: bool, sleep=time.sleep, echo=print, **kw) -> list[dict]:
    results = []
    while True:
        try:
            results.append(cycle(o, echo=echo, **kw))
        except Exception as e:  # noqa: BLE001 - keep watching
            echo(f"autointegrate cycle failed: {e}")
            results.append({"error": str(e)})
            if once:
                raise
        if once:
            return results
        sleep(max(60.0, o.interval_min * 60.0))
