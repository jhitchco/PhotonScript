"""PS-31: goal to finished image, the desktop watcher (`photonscript
integrate-watch`).

One cycle (run it every 30 min from a Windows Scheduled Task with --once,
or let it loop):

  1. post any queued ledger (report.sweep: ledger.json files still
     `reported: false`, e.g. the scope was down after the last run);
  2. stop if PixInsight is running (Jeremy is working, or a run is going);
  3. GET /api/integrations/candidates from the scheduler: per active goal +
     rig its goal progress, approved hours, last ledger, new data since it,
     readiness (calibration missing) and the rig's calibration owed list,
     plus the thresholds set on the System page;
  4. decide() per candidate (pure, tested): a goal whose rig is watched
     integrates when its goal is met and it has no ledger yet (or, with
     first_h > 0, once that many approved hours exist), or when new data
     since its last ledger reaches new_data_h; never sooner than
     min_interval_h after its last local run, never while one of its
     ledgers is still queued, and (require_calibration) not while
     calibration is missing;
  5. run at most ONE `photonscript integrate` (pipeline.run) into a NEW
     staging folder, then post its ledger (queued when the scope is down).
     PS-142: around the run it posts a processing notice (POST
     /api/integrations/processing, state start / end; best effort) so the
     goal's campaign status reads "Processing" while PixInsight works.

The AstroBin side is the packet draft the pipeline writes; nothing uploads.
--dry-run prints the decisions and runs nothing (no integrate, no post).
A lock file in the staging root keeps two watchers from overlapping.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from photonscript.integration import ledger as writer
from photonscript.integration import report, runner

LOCK_NAME = ".integrate-watch.lock"
FAILURES_NAME = ".integrate-watch-failures.json"   # "target|rig" -> ISO of a failed run


@dataclass
class WatchOptions:
    base_url: str
    staging_root: Path
    rigs: list[str] | None = None            # None = the scope's integrate_watch_rigs
    new_data_h: float | None = None          # None = the scope's thresholds
    first_h: float | None = None
    min_interval_h: float | None = None
    require_calibration: bool | None = None
    qa: str = "report"
    dry_run: bool = False
    interval_min: float = 30.0


@dataclass
class Decision:
    run: bool
    reason: str
    target: str = ""
    rig: str = ""
    candidate: dict = field(default_factory=dict)


def merged_thresholds(server: dict, o: WatchOptions) -> dict:
    """The scope's thresholds with the command line's overrides on top."""
    t = {"rigs": ["piggyback"], "new_data_h": 1.0, "first_h": 0.0, "min_interval_h": 12.0,
         "require_calibration": False}
    t.update({k: v for k, v in (server or {}).items() if v is not None})
    for k in ("rigs", "new_data_h", "first_h", "min_interval_h", "require_calibration"):
        v = getattr(o, k)
        if v is not None:
            t[k] = v
    return t


def _hours_since(iso: str, now: datetime) -> float | None:
    try:
        t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - t).total_seconds() / 3600.0


def _failures(staging_root: Path) -> dict:
    try:
        return json.loads((Path(staging_root) / FAILURES_NAME).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return {}


def record_failure(staging_root: Path, target: str, rig: str, when: str | None = None) -> None:
    """A run that raised before writing its ledger still counts as a run for
    min_interval_h (no retry storm of empty run folders)."""
    f = _failures(staging_root)
    f[f"{target}|{rig}"] = when or writer.L.now_iso()
    p = Path(staging_root) / FAILURES_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(f, indent=1), encoding="ascii")


def local_state(staging_root: Path, target: str, rig: str) -> dict:
    """This goal + rig's ledgers in the staging root: the newest one's age
    stamp (a recorded failed run counts) and whether any is still queued
    (not reported)."""
    mine = [led for _f, led in writer.local_ledgers(staging_root)
            if led.rig == rig and writer.same_campaign(led.campaign, target)]
    newest = max([led.created_at for led in mine]
                 + [_failures(staging_root).get(f"{target}|{rig}", "")])
    queued = [led.run for led in mine if not led.reported]
    return {"newest": newest, "queued": queued, "count": len(mine)}


def decide(c: dict, thr: dict, local: dict, now: datetime | None = None) -> Decision:
    """Integrate this candidate now? Pure: c = one /candidates row, thr =
    merged_thresholds(), local = local_state()."""
    now = now or datetime.now(timezone.utc)
    t, rig = c.get("target", "?"), c.get("rig", "")
    d = lambda run, why: Decision(run, why, t, rig, c)  # noqa: E731
    if rig not in (thr.get("rigs") or []):
        return d(False, f"rig {rig} not watched")
    if not c.get("approved_subs"):
        return d(False, "no approved subs")
    if local.get("queued"):
        return d(False, f"ledger of {local['queued'][0]} still queued (scope unreachable?)")
    age = _hours_since(local.get("newest", ""), now)
    if age is not None and age < float(thr.get("min_interval_h") or 0):
        return d(False, f"last run {age:.1f} h ago (< {thr['min_interval_h']:g} h)")
    rd = c.get("readiness") or {}
    missing = rd.get("calibration_missing") or []
    if thr.get("require_calibration") and missing:
        return d(False, "waiting for calibration: " + ", ".join(missing))
    goal = c.get("goal") or {}
    pct = float(goal.get("pct") or 0)
    last = c.get("last")
    new_h = float(c.get("new_data_h") or 0)
    if last is None:
        if pct >= 100:
            return d(True, f"goal met ({goal.get('hours_done')} of {goal.get('hours_goal')} h)")
        first = float(thr.get("first_h") or 0)
        if first > 0 and float(c.get("approved_h") or 0) >= first:
            return d(True, f"first integration: {c.get('approved_h')} h approved (>= {first:g} h)")
        return d(False, f"goal {pct:.0f}% and no integration yet (first at goal"
                        + (f" or {first:g} h" if first > 0 else "") + ")")
    need = float(thr.get("new_data_h") or 0)
    if need > 0 and new_h >= need:
        return d(True, f"{new_h:.2f} h new since v{last.get('version')} (>= {need:g} h)")
    return d(False, f"{new_h:.2f} h new since v{last.get('version')} (< {need:g} h)")


def pick(decisions: list[Decision]) -> Decision | None:
    """At most one run per cycle: a met goal first, then the most new data."""
    go = [x for x in decisions if x.run]
    if not go:
        return None
    return sorted(go, key=lambda x: (x.candidate.get("last") is not None,
                                     -float(x.candidate.get("new_data_h") or 0)))[0]


def trigger_of(dec: Decision, thr: dict) -> dict:
    c = dec.candidate
    return {"by": "integrate-watch", "reason": dec.reason, "at": writer.L.now_iso(),
            "goal": c.get("goal"), "approved_h": c.get("approved_h"),
            "new_data_h": c.get("new_data_h"),
            "previous_version": (c.get("last") or {}).get("version"),
            "calibration_missing": (c.get("readiness") or {}).get("calibration_missing") or [],
            "calibration_owed": c.get("calibration_owed") or [],
            "thresholds": thr}


# --- lock ------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return False


def acquire_lock(staging_root: Path, alive=_pid_alive) -> Path | None:
    """Lock file with our pid; None when another live watcher holds it."""
    root = Path(staging_root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / LOCK_NAME
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="ascii").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and alive(pid):
            return None
    lock.write_text(str(os.getpid()), encoding="ascii")
    return lock


def release_lock(lock: Path | None) -> None:
    if lock is not None:
        try:
            lock.unlink()
        except OSError:
            pass


# --- one cycle -------------------------------------------------------------------

def post_processing(o: WatchOptions, dec: Decision, state: str, *, post=report.http_post_json,
                    echo=print) -> bool:
    """PS-142: tell the scheduler a run of this goal + rig started / ended.
    Best effort: a down scope or an older scheduler (404) only skips it."""
    url = o.base_url.rstrip("/") + "/api/integrations/processing"
    body = {"campaign": dec.target, "rig": dec.rig, "state": state,
            "reason": dec.reason if state == "start" else ""}
    try:
        status, _b = post(url, body, report.DEFAULT_TIMEOUT_S)
    except (OSError, ValueError) as e:
        echo(f"  (processing notice not sent: {e})")
        return False
    return status == 200


def cycle(o: WatchOptions, *, run_integrate, get=report.http_get_json,
          post=report.http_post_json, pi_running=runner.pixinsight_running,
          echo=print, now: datetime | None = None, alive=_pid_alive) -> dict:
    """One watcher pass (see module doc). run_integrate(target, rig,
    trigger) -> pipeline result dict (with "ledger"). Returns a summary."""
    out: dict = {"ran": None, "decisions": [], "sweep": None}
    lock = None if o.dry_run else acquire_lock(o.staging_root, alive=alive)
    if not o.dry_run and lock is None:
        echo("another integrate-watch is running; nothing to do")
        out["skipped"] = "locked"
        return out
    try:
        echo("== queued ledgers")
        out["sweep"] = report.sweep(o.staging_root, o.base_url, post=post, echo=echo,
                                    dry_run=o.dry_run)
        busy = pi_running()
        if busy:
            echo(f"PixInsight is running (pid {', '.join(map(str, busy))}): no integration now")
            out["skipped"] = "pixinsight running"
            return out
        url = o.base_url.rstrip("/") + "/api/integrations/candidates"
        try:
            data = get(url)
        except (OSError, ValueError) as e:
            echo(f"scheduler unreachable ({e}): no decisions this time")
            out["skipped"] = "scheduler unreachable"
            return out
        thr = merged_thresholds(data.get("thresholds") or {}, o)
        out["thresholds"] = thr
        decs = []
        for c in data.get("candidates") or []:
            dec = decide(c, thr, local_state(o.staging_root, c.get("target", ""), c.get("rig", "")),
                         now=now)
            decs.append(dec)
            echo(f"  {'RUN ' if dec.run else 'wait'} {dec.target} [{dec.rig}]: {dec.reason}")
        out["decisions"] = [{"target": x.target, "rig": x.rig, "run": x.run, "reason": x.reason}
                            for x in decs]
        chosen = pick(decs)
        if chosen is None:
            echo("nothing to integrate")
            return out
        if o.dry_run:
            echo(f"dry run: would integrate {chosen.target} [{chosen.rig}] ({chosen.reason})")
            out["would_run"] = {"target": chosen.target, "rig": chosen.rig}
            return out
        echo(f"== integrate {chosen.target} [{chosen.rig}]: {chosen.reason}")
        post_processing(o, chosen, "start", post=post, echo=echo)
        try:
            try:
                res = run_integrate(chosen.target, chosen.rig, trigger_of(chosen, thr))
            except Exception:
                record_failure(o.staging_root, chosen.target, chosen.rig)
                raise
            out["ran"] = {"target": chosen.target, "rig": chosen.rig,
                          "run_dir": res.get("run_dir"), "ledger": res.get("ledger"),
                          "ok": res.get("integration", {}).get("ok")}
            if res.get("ledger"):
                r = report.post_ledger(Path(res["ledger"]), o.base_url, post=post)
                out["ran"]["posted"] = r["ok"]
                echo(("  ledger posted: " + r.get("detail", "")) if r["ok"]
                     else f"  WARNING: ledger queued, not posted ({r['detail']})")
        finally:
            post_processing(o, chosen, "end", post=post, echo=echo)
        return out
    finally:
        release_lock(lock)


def loop(o: WatchOptions, *, once: bool, run_integrate, sleep=time.sleep, echo=print,
         **kw) -> list[dict]:
    """cycle() once, or forever every interval_min (errors are logged and the
    loop goes on: a Scheduled Task with --once is the recommended setup)."""
    results = []
    while True:
        try:
            results.append(cycle(o, run_integrate=run_integrate, echo=echo, **kw))
        except Exception as e:  # noqa: BLE001 - keep watching
            echo(f"integrate-watch cycle failed: {e}")
            results.append({"error": str(e)})
            if once:
                raise
        if once:
            return results
        sleep(max(60.0, o.interval_min * 60.0))


def dump(result: dict) -> str:
    return json.dumps(result, indent=1, default=str)
