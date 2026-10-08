"""PS-161: desktop auto-integrate (`photonscript autointegrate`).

integrate-watch (PS-31) decides WHEN a goal is worth a run (goal met, or
new approved hours since its last ledger). This job adds what an unattended
desktop needs around that decision, one cycle per Scheduled Task tick
(deploy/install-autointegrate-task.ps1, every 30 min, --once):

  1. the integrate-watch lock (the two never overlap), the queued-ledger
     sweep, and nothing at all while PixInsight is open (Jeremy working);
  2. the candidates from the scheduler and integrate-watch's decide();
     no re-integration unless new approved data arrived (idempotent);
  3. Syncthing settled: a goal that wants a run only runs when its Library
     folders (this rig's filter folders) hold no Syncthing temp file
     (~syncthing~*.tmp / .syncthing.*.tmp) and their file count + total size
     have not changed for autointegrate_settle_min minutes (state kept in
     <staging>/.autointegrate-state.json; Syncthing keeps the source
     modification times, so mtimes cannot tell);
  4. at most one `photonscript integrate` run (OSC: natural color, plus the
     HOO-mapped image with autointegrate_hoo; RC16: one master per filter),
     its ledger posted (queued when the scope is down);
  5. two-rig goals: `photonscript blend` (PS-153) once both rigs have
     masters and the newest inputs were not blended yet;
  6. a review JPG (<run>/review.jpg, the finals side by side, at most
     2048 px) sent with Pushover (autointegrate_notify).

PixInsight runs one instance at a time (runner.run_script refuses to start a
second one); every run gets a NEW folder; the Library mirror is only read.
--dry-run prints the decisions (settle verdicts included), runs nothing,
posts nothing and writes no state.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from photonscript.integration import report, runner
from photonscript.integration import watch as w

STATE_NAME = ".autointegrate-state.json"
TEMP_PREFIXES = ("~syncthing~", ".syncthing.")
REVIEW_NAME = "review.jpg"
REVIEW_MAX_PX = 2048


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


# --- blend -----------------------------------------------------------------------

def blend_inputs_key(inp) -> list[str]:
    return sorted([str(inp.osc)] + [str(p) for p in inp.rc16.values()])


def already_blended(staging_root: Path, target: str, key: list[str]) -> str | None:
    """Name of a blend folder under <staging>/Blend that used exactly these
    inputs, else None (blend idempotence)."""
    from photonscript.integration.blend import BLEND_DIR
    from photonscript.integration.ledger import same_campaign
    root = Path(staging_root) / BLEND_DIR
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir(), reverse=True):
        try:
            m = json.loads((d / "manifest.json").read_text(encoding="ascii"))
        except (OSError, ValueError):
            continue
        if not same_campaign(str(m.get("target", "")), target):
            continue
        used = sorted(str(f.get("path", "")) for f in m.get("inputs") or [])
        if used == key and (m.get("pixinsight") or {}).get("ok") is not False:
            return d.name
    return None


def blend_candidate(o: AutoOptions, target: str, discover) -> tuple[object | None, str]:
    """(inputs, why) for a blend of `target` now, or (None, why not)."""
    try:
        inp = discover(target)
    except Exception as e:  # noqa: BLE001 - BlendError: a rig has no master yet
        return None, f"no blend: {e}"
    done = already_blended(o.watch.staging_root, target, blend_inputs_key(inp))
    if done:
        return None, f"already blended in {done}"
    return inp, "both rigs have masters and these inputs are not blended yet"


# --- review image + notice -------------------------------------------------------

def finals(run_dir: Path, blend: bool = False) -> list[Path]:
    """The finished JPGs of a run: integrate *_final.jpg (+ *_hoo.jpg), or a
    blend's *_blend.jpg (+ *_core.jpg)."""
    fin = Path(run_dir) / "out" / "final"
    pats = ("*_blend.jpg", "*_core.jpg") if blend else ("*_final.jpg", "*_hoo.jpg")
    out: list[Path] = []
    for pat in pats:
        out += sorted(fin.glob(pat))
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
        return (f"Blend {target}: {'done' if ok else 'FAILED'} "
                f"({Path(res.get('run_dir', '')).name}). {why}")
    integ = (res.get("integration") or {})
    fin = (res.get("finish") or {})
    h = res.get("hours_integrated")
    return (f"Integrated {target} [{rig}] v{res.get('ledger_version', '?')}: "
            + (f"{h:.1f} h, " if isinstance(h, (int, float)) else "")
            + f"integration {'ok' if integ.get('ok') else 'FAILED'}"
            + (f", finish {'ok' if fin.get('ok') else 'FAILED'}" if fin else "")
            + f" ({Path(res.get('run_dir', '')).name}). {why}")


def pushover_sender(cfg):
    """send(message, image) via the shared rate-limited Pushover client."""
    def send(message: str, image: Path | None) -> bool:
        import asyncio
        from photonscript.shared.pushover import notify
        return bool(asyncio.run(notify(cfg, message, title="PhotonScript integrate",
                                       attachment=str(image) if image else None)))
    return send


# --- one cycle -------------------------------------------------------------------

def cycle(o: AutoOptions, *, run_integrate, run_blend, discover_blend, send=None,
          get=report.http_get_json, post=report.http_post_json,
          pi_running=runner.pixinsight_running, echo=print,
          now: datetime | None = None, alive=w._pid_alive) -> dict:
    """One auto-integrate pass (module doc). run_integrate(target, rig,
    trigger) -> pipeline result; run_blend(target) -> blend result;
    discover_blend(target) -> blend inputs (raises when a rig is missing);
    send(message, image) -> bool."""
    wo = o.watch
    now = now or datetime.now(timezone.utc)
    out: dict = {"ran": None, "blend": None, "decisions": [], "sweep": None}
    lock = None if wo.dry_run else w.acquire_lock(wo.staging_root, alive=alive)
    if not wo.dry_run and lock is None:
        echo("another integrate-watch / autointegrate is running; nothing to do")
        out["skipped"] = "locked"
        return out
    try:
        echo("== queued ledgers")
        out["sweep"] = report.sweep(wo.staging_root, wo.base_url, post=post, echo=echo,
                                    dry_run=wo.dry_run)
        busy = pi_running()
        if busy:
            echo(f"PixInsight is running (pid {', '.join(map(str, busy))}): nothing now")
            out["skipped"] = "pixinsight running"
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
            dec = w.decide(c, thr, w.local_state(wo.staging_root, t, rig), now=now)
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
        if o.blend:
            out["blend"] = _maybe_blend(o, cands, chosen, run_blend, discover_blend, send,
                                        pi_running, echo)
        return out
    finally:
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
                 pi_running, echo) -> dict | None:
    """At most one blend per cycle: the target just integrated first, then
    any goal the candidates list for both rigs."""
    rigs_by: dict[str, set] = {}
    for c in cands:
        rigs_by.setdefault(c.get("target", ""), set()).add(c.get("rig", ""))
    order = []
    if chosen is not None:
        order.append(chosen.target)
    order += [t for t, rs in sorted(rigs_by.items())
              if {"piggyback", "rc16"} <= rs and t not in order]
    for t in order:
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
                "ok": (res.get("blend") or {}).get("ok")}
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
