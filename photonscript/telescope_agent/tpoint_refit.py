"""TPoint refit: the step after tpoint-autocal (docs/TPOINT_REFIT.md).

After ``photonscript tpoint-autocal --go`` adds pointing samples, the model
still has to be refit (Super Model), saved, judged, and "Apply pointing
corrections" / ProTrack turned on. This module is ``photonscript
tpoint-refit``: back up TPoint's files, run the fit, read the new model
back from TPoint's own files (tpoint_files.stats), keep it only when it is
good enough, else put the backed-up files back.

What is evidenced (2026-10-08 probe, TheSky64 10.5 build 14139, read only;
HANDBOOK "TPoint Command Line from a script"):

* There is NO TPoint scripting object (TPoint, sky6TPoint, TPointAddOn2 are
  undefined globals) and no known sky6* method that takes a TPoint command
  string, so the TPoint Command Line (FIT, SUPERMODEL style commands) is
  NOT reachable from a script as far as anything shows.
* TheSkyXAction is an object of numeric action ids (AddPointingSample =
  197, AutoPointingCalibration = 105, TPointModule = 146, TPointAddOn2 =
  147) and TheSkyXAction.execute is a function: it runs toolbar commands
  ("a subset of commands listed under Preferences, Toolbars, Customize").
  The TPoint ids seen open TPoint's windows. No id for a fit, a Super
  Model, a save, Apply pointing corrections or ProTrack has been seen; the
  probe's member list was not captured anywhere in the repo.
* ProTrack / Apply pointing corrections: no write API. The only earlier
  attempt (an undocumented sky6RASCOMTele string command for ProTrack,
  never called) was removed in PS-104 (commit 80a0586); thesky_client.tpoint_flags reads CANDIDATE
  property names only. So this module never touches either: they stay a
  console step.
* TPoint's files: a Super Model fit writes "Super Model Outmod.dat" (and
  Indat / Outdat), which tpoint_files.stats parses (sky RMS, observations
  in the fit, fitted terms); the TPoint watcher already treats a model
  file with more than the 6 basic terms as "the Super Model fit".

So the live fit has two forms, both SUPERVISED:

* ``--fit console`` (works today, at the console only): PhotonScript backs
  up, Jeremy runs Super Model and saves in TPoint, PhotonScript reads the
  result back, judges it and restores the backup when it is worse.
* ``--fit action --action <Name>`` (a CANDIDATE, first test only):
  TheSkyXAction.execute(TheSkyXAction.<Name>) for a TheSkyXAction member
  found with ``--discover`` (read only: lists the members, calls nothing).
  Refused without ``--test`` until ``tpoint_refit_action`` pins a name that
  a supervised test confirmed.

Restoring files does NOT revert the model TheSky holds in memory (and
ProTrack uses that one): after a restore TPoint must reopen the restored
file before the next save or slew that relies on it. Whether TPoint
rereads its files at all is unknown (the console test answers it).

Never arms, never dispatches, never moves the mount, never syncs, never
turns pointing corrections or ProTrack on or off. Nothing here is deleted:
the rejected fit's files are moved into the backup folder's ``rejected``
subfolder before the backup is copied back.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Callable

from photonscript.telescope_agent import tpoint_autocal as ta

FITS = ("console", "action")
DEFAULT_FIT = "console"
DEFAULT_WAIT_S = 180.0
POLL_S = 5.0
DEFAULT_MAX_WORSE_PCT = 20.0
DEFAULT_MIN_PER_TERM = 3.0
PASS, FAIL, UNKNOWN = ta.PASS, ta.FAIL, ta.UNKNOWN

# a TheSkyXAction member name: a plain identifier, nothing else gets into
# the script text
_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# never a refit: these add / clear samples, start runs or move the mount
_ACTION_DENY = re.compile(r"add|delete|clear|remove|sync|slew|park|calibrat|"
                          r"home|abort|connect|image|camera|focus", re.IGNORECASE)
# the TheSkyXAction members --discover lists
DISCOVER_RE = "tpoint|model|fit|super|protrack|correct|pointing|mapping"

MANUAL_AFTER = (
    "In TheSky's TPoint window: check the fit, turn on Apply pointing "
    "corrections and ProTrack (Activate ProTrack + Enable tracking "
    "adjustments) as usual. No script can do this on build 14139 "
    "(docs/TPOINT_REFIT.md).")


def action_ok(name: str) -> str | None:
    """None when name is a usable TheSkyXAction member name, else why not."""
    n = str(name or "")
    if not _ACTION_RE.match(n):
        return f"action name {n!r} is not a plain identifier"
    if _ACTION_DENY.search(n):
        return (f"action {n!r} looks like it adds / clears samples, starts a "
                "run or moves the mount: never used for a refit")
    return None


# --------------------------------------------------------------- scripts
#
# ES5 for TheSky's TCP server, the tpoint_autocal helper style ("k=v;k=v"
# in Out). discover_script is READ ONLY (for..in and typeof, nothing is
# called). fit_action_script is the one write and runs only from
# RefitRun.fit() in action mode.

def discover_script() -> str:
    """READ ONLY: TheSkyXAction members whose names mention TPoint / model /
    fit / super / ProTrack / correction / pointing, as name:value pairs,
    and execute's typeof. Nothing is executed."""
    return (ta._HELPERS
            + "var acts = '?ERR', ex = '?ERR', n = 0;"
            "try { var r = [];"
            " for (var k in TheSkyXAction) { n++;"
            f"  if (/{DISCOVER_RE}/i.test(k)) {{"
            "   var v = '?'; try { v = s(TheSkyXAction[k]); } catch (e1) {}"
            "   r.push(s(k) + ':' + v); } }"
            " acts = r.join('~'); } catch (e) { acts = '?ERR:' + s(e.message || e); }"
            "try { ex = typeof TheSkyXAction.execute; } catch (e2) {}"
            "Out = 'execute=' + ex + ';members_seen=' + n + ';actions=' + acts;")


def fit_action_script(name: str) -> str:
    """CANDIDATE write: TheSkyXAction.execute(TheSkyXAction.<name>) in the
    numeric-id form, only when that member is a number (else nothing is
    called and found=0). Which argument form execute() takes is not
    confirmed; the id form is the one the add candidates lead with."""
    why = action_ok(name)
    if why:
        raise ValueError(why)
    m = f"TheSkyXAction.{name}"
    return (ta._HELPERS
            + "var err = '', found = 0, called = 0, ret = '';"
            f"try {{ found = (typeof {m} == 'number') ? 1 : 0; }} catch (e0) {{}}"
            "if (found) { try { ret = s(TheSkyXAction.execute("
            f"{m})); called = 1; }} catch (e) {{ err = s(e.message || e); }} }}"
            f"Out = 'found=' + found + ';called=' + called + ';id=' + g(function(){{return {m};}})"
            " + ';ret=' + ret + ';err=' + err;")


def parse_discover(kv: dict) -> dict:
    """{"execute", "members_seen", "actions": [{"name", "value"}]}."""
    acts = []
    raw = str(kv.get("actions") or "")
    if raw and not raw.startswith("?ERR"):
        for item in raw.split("~"):
            name, _, val = item.partition(":")
            if name:
                acts.append({"name": name, "value": val,
                             "refusal": action_ok(name)})
    return {"execute": kv.get("execute"), "members_seen": kv.get("members_seen"),
            "error": raw if raw.startswith("?ERR") else None, "actions": acts}


# --------------------------------------------------------------- judging

def model_key(st: dict | None) -> tuple:
    """What identifies a fitted model in tpoint_files.stats output: a new
    fit changes at least one of these."""
    s = st or {}
    return (s.get("active_model_file"), s.get("active_model_mtime"),
            s.get("model_points"), s.get("sky_rms_arcsec"), s.get("fitted_terms"))


def thresholds(config, min_samples=None, max_rms=None, max_worse_pct=None,
               min_per_term=None) -> dict:
    def pick(v, key, default):
        if v is not None:
            return float(v)
        c = getattr(config, key, None)
        return float(default if c is None else c)
    return {"min_samples": int(pick(min_samples, "tpoint_min_points", 50)),
            "max_rms_arcsec": pick(max_rms, "tpoint_rms_max_arcsec", 30.0),
            "max_worse_pct": pick(max_worse_pct, "tpoint_refit_max_worse_pct",
                                  DEFAULT_MAX_WORSE_PCT),
            "min_per_term": pick(min_per_term, "tpoint_refit_min_samples_per_term",
                                 DEFAULT_MIN_PER_TERM)}


def brief(st: dict | None) -> dict:
    s = st or {}
    return {"points": s.get("model_points"), "rms": s.get("sky_rms_arcsec"),
            "terms": s.get("fitted_terms"), "file": s.get("active_model_file"),
            "mtime": s.get("active_model_mtime")}


def judge(before: dict | None, after: dict | None, th: dict) -> dict:
    """Keep the new fit? Pure. {"verdict": keep | reject | no_change,
    "checks": [{name, status, detail}], "reasons": [str], "before",
    "after"} with before / after = {points, rms, terms, file}. Unknown
    counts as reject."""
    b, a = before or {}, after or {}
    out = {"before": brief(b), "after": brief(a), "checks": []}
    if not a.get("ok"):
        out.update(verdict="reject", reasons=[
            "TPoint's files could not be read after the fit: "
            + str(a.get("note") or "no model")])
        return out
    if model_key(a) == model_key(b):
        out.update(verdict="no_change", reasons=[
            "no new model in TPoint's files (same file, time, points, RMS, "
            "terms): the fit was not saved, or did not run"])
        return out
    checks = out["checks"]
    n = a.get("model_points")
    terms = a.get("fitted_terms")
    rms = a.get("sky_rms_arcsec")
    if a.get("empty_model"):
        checks.append(ta._check("model has samples", FAIL, a.get("note") or "empty model"))
    if n is None:
        checks.append(ta._check("samples", UNKNOWN, "points in the fit not read"))
    else:
        checks.append(ta._check(
            "samples", PASS if n >= th["min_samples"] else FAIL,
            f"{n} in the fit (need >= {th['min_samples']})"))
    if n is not None and terms:
        per = n / float(terms)
        checks.append(ta._check(
            "samples per term", PASS if per >= th["min_per_term"] else FAIL,
            f"{n} samples / {terms} fitted terms = {per:.1f} "
            f"(need >= {th['min_per_term']:g}; fewer = over-fitted)"))
    elif terms is None:
        checks.append(ta._check("samples per term", UNKNOWN, "fitted terms not read"))
    if rms is None:
        checks.append(ta._check("sky RMS", UNKNOWN, "sky RMS not read"))
    else:
        checks.append(ta._check(
            "sky RMS", PASS if rms <= th["max_rms_arcsec"] else FAIL,
            f"{rms:.2f}\" (limit {th['max_rms_arcsec']:g}\")"))
        brms = b.get("sky_rms_arcsec")
        if brms:
            lim = brms * (1.0 + th["max_worse_pct"] / 100.0)
            checks.append(ta._check(
                "not worse than before", PASS if rms <= lim else FAIL,
                f"{rms:.2f}\" vs {brms:.2f}\" before (at most "
                f"+{th['max_worse_pct']:g}% = {lim:.2f}\")"))
    bad = [f"{c['name']}: {c['detail']}" for c in checks
           if c["status"] in (FAIL, UNKNOWN)]
    out.update(verdict="reject" if bad else "keep", reasons=bad)
    return out


# --------------------------------------------------------------- restore

def _rel_of(copy: dict, dest: str) -> str:
    try:
        return str(PureWindowsPath(copy["dst"]).relative_to(PureWindowsPath(dest)))
    except ValueError:
        return PureWindowsPath(copy["dst"]).name


def restore_plan(backup: dict, listing_after: dict | None,
                 changed: Callable[[str, str], bool]) -> dict:
    """What a rejected fit undoes. Pure apart from ``changed(src, backup)``
    (True when the current file differs from its backup copy).
    {"restore": [{src, backup, aside}], "set_aside": [{src, aside}]}:
    changed files are first copied to <dest>\\rejected\\<rel> and then the
    backup is copied back over them; files the fit created (no backup) are
    moved to <dest>\\rejected\\<rel>. Nothing is deleted."""
    dest = str(backup.get("dest") or "")
    rej = PureWindowsPath(dest) / "rejected"
    by_src = {c["src"]: c for c in backup.get("copies") or []}
    restore, aside = [], []
    for c in backup.get("copies") or []:
        if changed(c["src"], c["dst"]):
            restore.append({"src": c["src"], "backup": c["dst"],
                            "aside": str(rej / _rel_of(c, dest))})
    for f in (listing_after or {}).get("files") or []:
        p = str(f.get("path") or "")
        rel = str(f.get("rel") or "")
        if not p or p in by_src or not ta._BACKUP_RE.search(rel):
            continue
        aside.append({"src": p, "aside": str(rej / PureWindowsPath(rel))})
    return {"restore": restore, "set_aside": aside}


# --------------------------------------------------------------- the run

class StopRefit(Exception):
    pass


@dataclass
class Deps:
    sky: object | None                 # .run(js) -> dict (TheSkyLink)
    sched: object | None               # .arm() -> dict | None
    list_files: Callable[[], dict]
    stats: Callable[[dict], dict]      # tpoint_files.stats(config, listing)
    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print
    sleep: Callable[[float], None] = time.sleep
    mono: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = datetime.utcnow
    copy: Callable[[str, str], None] | None = None
    move: Callable[[str, str], None] | None = None
    changed: Callable[[str, str], bool] | None = None
    save_record: Callable[[dict], None] | None = None


@dataclass
class Opts:
    fit: str = DEFAULT_FIT
    action: str = ""
    action_pinned: bool = False        # from tpoint_refit_action (confirmed)
    test: bool = False
    wait_s: float = DEFAULT_WAIT_S
    backup_root: str = ta.DEFAULT_BACKUP_ROOT
    th: dict = field(default_factory=dict)
    verbose: bool = False


def _copy_file(src: str, dst: str) -> None:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _move_file(src: str, dst: str) -> None:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)


def _changed(src: str, bak: str) -> bool:
    import filecmp
    try:
        return not filecmp.cmp(src, bak, shallow=False)
    except OSError:
        return True


def _fmt(s: dict) -> str:
    def r(v):
        return "?" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
    return (f"{r(s.get('points'))} samples in the fit, sky RMS {r(s.get('rms'))}\", "
            f"{r(s.get('terms'))} fitted terms ({s.get('file') or 'no model file'})")


class RefitRun:
    def __init__(self, deps: Deps, opts: Opts):
        self.d = deps
        self.o = opts
        self.backup: dict | None = None
        self.before: dict = {}
        self.after: dict = {}
        self.fit_reply: dict | None = None
        self.verdict: dict | None = None
        self.restored: dict | None = None
        self.listing: dict | None = None

    def say(self, m: str):
        self.d.say(m)

    def yes(self, prompt: str) -> bool:
        try:
            a = self.d.ask(prompt + " Type yes to go on: ")
        except EOFError:
            return False
        return str(a or "").strip().lower() == "yes"

    def armer_check(self) -> dict:
        """GET /api/arm reads DISARMED (unknown = no go)."""
        arm = None
        try:
            arm = self.d.sched.arm() if self.d.sched is not None else None
        except Exception as e:  # noqa: BLE001
            self.say(f"  could not read the armer ({e})")
        state = str((arm or {}).get("state") or "")
        if not state:
            return ta._check("armer idle", UNKNOWN, "GET /api/arm unreadable")
        if state in ta.ARMER_OK_STATES:
            return ta._check("armer idle", PASS, f"armer {state}")
        return ta._check("armer idle", FAIL, f"armer {state}: a refit "
                         "changes pointing mid-night; disarm first")

    # -- 1. preflight
    def preflight(self, fit_check: bool = True) -> dict:
        checks = []
        if not fit_check:
            pass
        elif self.o.fit not in FITS:
            checks.append(ta._check("fit form", FAIL, f"--fit must be one of {FITS}"))
        elif self.o.fit == "action":
            why = action_ok(self.o.action)
            if why:
                checks.append(ta._check("action", FAIL, why))
            elif not (self.o.test or self.o.action_pinned):
                checks.append(ta._check(
                    "action", FAIL, f"{self.o.action} is not confirmed: run it "
                    "once with --test at the console, then pin it in "
                    "tpoint_refit_action"))
            elif self.d.sky is None:
                checks.append(ta._check("action", FAIL, "no TheSky link"))
            else:
                checks.append(ta._check("action", PASS, f"TheSkyXAction.{self.o.action}"
                                        + (" (pinned)" if self.o.action_pinned else
                                           " (supervised test)")))
        else:
            checks.append(ta._check("fit form", PASS, "console: Jeremy runs Super "
                                    "Model and saves in TPoint"))
        checks.append(self.armer_check())
        lst = None
        try:
            lst = self.d.list_files()
            self.listing = lst
            self.backup = ta.backup_plan(lst, self.d.now(), self.o.backup_root)
        except Exception as e:  # noqa: BLE001
            self.say(f"  could not list TPoint's files ({e})")
        if not self.backup or not self.backup.get("copies"):
            checks.append(ta._check("TPoint backup", FAIL if self.backup else UNKNOWN,
                                    "no TPoint files found to back up"))
        else:
            checks.append(ta._check("TPoint backup", PASS,
                                    f"{len(self.backup['copies'])} file(s) to "
                                    f"{self.backup['dest']}"))
        if lst is not None:
            try:
                self.before = self.d.stats(lst)
            except Exception as e:  # noqa: BLE001
                self.before = {"ok": False, "note": str(e)}
        if not self.before.get("ok"):
            checks.append(ta._check("model before", UNKNOWN,
                                    str(self.before.get("note") or "not read")))
        else:
            checks.append(ta._check("model before", PASS, _fmt(brief(self.before))))
        bad = [f"{c['name']}: {c['detail']}" for c in checks
               if c["status"] in (FAIL, UNKNOWN)]
        for c in checks:
            self.say(f"  [{c['status'].upper():7}] {c['name']}: {c['detail']}")
        return {"go": not bad, "checks": checks, "blockers": bad}

    # -- 2. backup
    def do_backup(self) -> None:
        cp = self.d.copy or _copy_file
        self.say(f"Step 2: backing up {len(self.backup['copies'])} TPoint file(s) "
                 f"to {self.backup['dest']}.")
        for c in self.backup["copies"]:
            try:
                cp(c["src"], c["dst"])
            except Exception as e:  # noqa: BLE001
                raise StopRefit(f"backup of {c['src']} failed ({e}); nothing "
                                "was sent to TheSky") from None

    # -- 3. fit
    def fit(self) -> None:
        if self.o.fit == "console":
            self.say("Step 3 (console): in TheSky's TPoint window run the fit "
                     "(Super Model), wait for it to finish, then save the model "
                     "(File > Save) so TPoint writes its files. Do NOT turn on "
                     "pointing corrections / ProTrack yet.")
            if not self.yes("  Super Model run and saved?"):
                raise StopRefit("operator did not confirm the fit")
            return
        js = fit_action_script(self.o.action)
        self.say(f"Step 3 (action): TheSkyXAction.execute(TheSkyXAction."
                 f"{self.o.action}).")
        if self.o.test:
            self.say("  SUPERVISED TEST: watch TheSky's TPoint window. Expected: a "
                     "fit runs (or a window opens). Anything else (a dialog, "
                     "samples cleared): note it, cancel in TheSky, and answer "
                     "no at the next question.")
            if not self.yes("  Send it now?"):
                raise StopRefit("operator did not send the action")
        if self.o.verbose or self.o.test:
            self.say(f"    TheSky <- {js}")
        r = self.d.sky.run(js)
        self.fit_reply = r
        if self.o.verbose or self.o.test:
            self.say(f"    TheSky -> {r}")
        if str(r.get("found")) != "1":
            raise StopRefit(f"TheSkyXAction.{self.o.action} is not a number on "
                            "this build: nothing was called")
        if str(r.get("called")) != "1" or r.get("err"):
            raise StopRefit(f"execute failed: {r.get('err') or r}")
        if self.o.test:
            self.say("  If TPoint shows a new fit but has not saved it, save it "
                     "now (File > Save).")
            if not self.yes("  Did TPoint run a fit (and is it saved)?"):
                self.say("  Noted: this action is not a fit. Do not pin it.")

    # -- 4. read back
    def wait_for_model(self) -> dict:
        self.say(f"Step 4: reading TPoint's files back (up to {self.o.wait_s:g} s "
                 "for a new model file).")
        t0 = self.d.mono()
        last = {}
        while True:
            try:
                last = self.d.stats(self.d.list_files())
            except Exception as e:  # noqa: BLE001
                last = {"ok": False, "note": str(e)}
            if last.get("ok") and model_key(last) != model_key(self.before):
                return last
            if self.d.mono() - t0 >= self.o.wait_s:
                return last
            self.d.sleep(POLL_S)

    # -- 5. restore
    def restore(self) -> dict:
        plan = restore_plan(self.backup, self.d.list_files(),
                            self.d.changed or _changed)
        cp = self.d.copy or _copy_file
        mv = self.d.move or _move_file
        done, errors = [], []
        for r in plan["restore"]:
            try:
                cp(r["src"], r["aside"])      # keep the rejected version
                cp(r["backup"], r["src"])
                done.append(r["src"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{r['src']}: {e}")
        for a in plan["set_aside"]:
            try:
                mv(a["src"], a["aside"])
                done.append(a["src"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{a['src']}: {e}")
        return {**plan, "done": done, "errors": errors}

    def run(self) -> dict:
        self.say("Step 1: preflight (armer, TPoint files, the model now).")
        pre = self.preflight()
        if not pre["go"]:
            return self.summary("refused", "; ".join(pre["blockers"]), pre)
        try:
            self.do_backup()
            self.fit()
        except StopRefit as e:
            return self.summary("stopped", str(e), pre)
        self.after = self.wait_for_model()
        self.verdict = judge(self.before, self.after, self.o.th)
        for c in self.verdict["checks"]:
            self.say(f"  [{c['status'].upper():7}] {c['name']}: {c['detail']}")
        v = self.verdict["verdict"]
        if v == "keep":
            return self.summary("kept", "the new fit passed every check", pre)
        if v == "no_change":
            return self.summary("no_change", "; ".join(self.verdict["reasons"]), pre)
        self.say("Step 5: the fit is REJECTED: " + "; ".join(self.verdict["reasons"]))
        self.say(f"  restoring the backup from {self.backup['dest']} (the rejected "
                 "files go to its rejected folder).")
        self.restored = self.restore()
        for e in self.restored["errors"]:
            self.say(f"  restore FAILED: {e}")
        return self.summary("rejected", "; ".join(self.verdict["reasons"]), pre)

    def summary(self, status: str, reason: str, pre: dict | None = None) -> dict:
        s = {"status": status, "reason": reason, "fit": self.o.fit,
             "action": self.o.action or None, "test": self.o.test,
             "backup": (self.backup or {}).get("dest"),
             "verdict": self.verdict, "fit_reply": self.fit_reply,
             "restored": self.restored, "preflight": pre,
             "utc": self.d.now().strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.say(f"TPoint refit: {status.upper()}: {reason}")
        if self.verdict:
            self.say("  before: " + _fmt(self.verdict["before"]))
            self.say("  after:  " + _fmt(self.verdict["after"]))
        if status == "kept":
            self.say("  Next: " + MANUAL_AFTER)
        elif status == "rejected":
            self.say("  TheSky STILL HOLDS THE REJECTED MODEL IN MEMORY. In TPoint: "
                     "reopen the restored model (File > Open) or undo the fit, and do "
                     "not save before that (a save would overwrite the restore). "
                     "Leave pointing corrections / ProTrack as they were.")
        if self.backup and status in ("kept", "rejected", "no_change",
                                      "bad_not_restored", "backed_up"):
            self.say(f"  Backup: {self.backup['dest']}")
        if self.d.save_record:
            try:
                self.d.save_record(s)
            except Exception as e:  # noqa: BLE001
                self.say(f"  could not save the refit record ({e})")
        return s


def run_refit(deps: Deps, opts: Opts) -> dict:
    return RefitRun(deps, opts).run()


# --------------------------------------------------------------- remote steps
#
# 2026-10-29: the refit in three non-interactive steps for the remote CLI
# (no stdin there): ``--backup`` (preflight, backup, the model before in
# refit_before.json), then the Super Model fit and save by hand in TPoint
# (Remote Desktop), then ``--judge [--restore-if-bad]`` (the model now
# against refit_before.json, judge(), and the same restore as --go).

BEFORE_NAME = "refit_before.json"


def write_before(dest: str, rec: dict) -> Path:
    p = Path(dest) / BEFORE_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return p


def latest_backup(root: str) -> Path | None:
    """The newest <root>/<UTC stamp> folder holding refit_before.json (the
    stamps sort by time), else None."""
    r = Path(root)
    if not r.is_dir():
        return None
    dirs = sorted((d for d in r.iterdir() if (d / BEFORE_NAME).is_file()),
                  key=lambda d: d.name)
    return dirs[-1] if dirs else None


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def backup_only(deps: Deps, opts: Opts) -> dict:
    """``tpoint-refit --backup``: the --go preflight (without the fit form),
    the --go backup, and the model before in <backup>/refit_before.json.
    Asks nothing."""
    run = RefitRun(deps, opts)
    run.say("Step 1: preflight (armer, TPoint files, the model now).")
    pre = run.preflight(fit_check=False)
    if not pre["go"]:
        return run.summary("refused", "; ".join(pre["blockers"]), pre)
    try:
        run.do_backup()
    except StopRefit as e:
        return run.summary("stopped", str(e), pre)
    files = {f.get("path"): f for f in (run.listing or {}).get("files") or []}
    dest = run.backup["dest"]
    copies = []
    for c in run.backup["copies"]:
        f = files.get(c["src"]) or {}
        copies.append({"src": c["src"], "rel": _rel_of(c, dest),
                       "size": f.get("size", c.get("size")), "mtime": f.get("mtime")})
    rec = {"utc": deps.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "backup_dir": dest,
           "files": copies, "model": brief(run.before),
           "samples": run.before.get("model_points"),
           "sky_rms_arcsec": run.before.get("sky_rms_arcsec"),
           "terms": run.before.get("fitted_terms"), "before": run.before}
    try:
        p = write_before(dest, rec)
    except Exception as e:  # noqa: BLE001
        return run.summary("stopped", f"could not write {BEFORE_NAME} ({e})", pre)
    run.say("  model before: " + _fmt(brief(run.before)))
    run.say(f"  wrote {p}")
    run.say("Next: in TPoint (Remote Desktop) run Super Model and File > Save, "
            "then photonscript tpoint-refit --judge --restore-if-bad")
    s = run.summary("backed_up", f"{len(copies)} file(s) backed up", pre)
    s["before_file"] = str(p)
    return s


def judge_only(deps: Deps, opts: Opts, backup_dir: str | None = None,
               restore_if_bad: bool = False) -> dict:
    """``tpoint-refit --judge``: the model now (TPoint's files, one read)
    against <backup>/refit_before.json with judge(). Reports only, unless
    restore_if_bad and the verdict is reject: then the --go restore (armer
    DISARMED first). Asks nothing."""
    run = RefitRun(deps, opts)
    root = Path(opts.backup_root)
    if backup_dir:
        bdir = Path(backup_dir)
        if not bdir.is_absolute():
            bdir = root / bdir
        if not _under(bdir, root):
            return run.summary("refused", f"{bdir} is not under {root}")
    else:
        bdir = latest_backup(opts.backup_root)
        if bdir is None:
            return run.summary("refused", f"no backup with {BEFORE_NAME} under "
                               f"{root}: run tpoint-refit --backup first")
    try:
        rec = json.loads((bdir / BEFORE_NAME).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return run.summary("refused", f"could not read {bdir / BEFORE_NAME} ({e})")
    run.backup = {"dest": str(bdir), "copies": [
        {"src": f["src"], "dst": str(bdir / PureWindowsPath(f["rel"])),
         "size": f.get("size")} for f in rec.get("files") or []]}
    run.before = rec.get("before") or {}
    run.say(f"Backup: {bdir} (taken {rec.get('utc')})")
    pre = None
    if restore_if_bad:
        c = run.armer_check()
        run.say(f"  [{c['status'].upper():7}] {c['name']}: {c['detail']}")
        pre = {"go": c["status"] == PASS, "checks": [c],
               "blockers": [] if c["status"] == PASS else
               [f"{c['name']}: {c['detail']}"]}
        if not pre["go"]:
            return run.summary("refused", "; ".join(pre["blockers"]), pre)
    try:
        run.after = deps.stats(deps.list_files())
    except Exception as e:  # noqa: BLE001
        run.after = {"ok": False, "note": str(e)}
    run.verdict = judge(run.before, run.after, opts.th)
    for c in run.verdict["checks"]:
        run.say(f"  [{c['status'].upper():7}] {c['name']}: {c['detail']}")
    v = run.verdict["verdict"]
    if v == "keep":
        return run.summary("kept", "the new fit passed every check", pre)
    if v == "no_change":
        return run.summary("no_change", "; ".join(run.verdict["reasons"]), pre)
    if not restore_if_bad:
        run.say("The fit is BAD; nothing restored (report only). To restore: "
                "photonscript tpoint-refit --judge --restore-if-bad")
        return run.summary("bad_not_restored", "; ".join(run.verdict["reasons"]), pre)
    run.say("The fit is REJECTED: " + "; ".join(run.verdict["reasons"]))
    run.say(f"  restoring the backup from {bdir} (the rejected files go to its "
            "rejected folder).")
    run.restored = run.restore()
    for e in run.restored["errors"]:
        run.say(f"  restore FAILED: {e}")
    return run.summary("rejected", "; ".join(run.verdict["reasons"]), pre)


# --------------------------------------------------------------- dry run

def build_plan(config, opts: Opts, listing: dict | None, before: dict | None,
               now: datetime) -> dict:
    """The dry run: what --go would do, with the scripts. Pure (listing /
    before come from tpoint_files)."""
    backup = ta.backup_plan(listing, now, opts.backup_root)
    steps = [
        "preflight: GET /api/arm DISARMED (unknown = no go), TPoint files "
        "found, the model before read from TPoint's files",
        f"backup: copy {len(backup['copies'])} TPoint file(s) to {backup['dest']}",
    ]
    script = None
    if opts.fit == "action":
        why = action_ok(opts.action)
        if why:
            steps.append(f"fit (action): REFUSED: {why}")
        else:
            script = fit_action_script(opts.action)
            steps.append(f"fit (action, CANDIDATE): TheSkyXAction.execute("
                         f"TheSkyXAction.{opts.action})"
                         + ("" if opts.action_pinned else
                            "; not pinned: --go needs --test"))
    else:
        steps.append("fit (console): Jeremy runs Super Model in TPoint and saves; "
                     "PhotonScript waits for the yes")
    steps += [
        f"read back: poll TPoint's files up to {opts.wait_s:g} s for a new model "
        "(tpoint_files.stats: points in the fit, sky RMS, fitted terms)",
        f"judge: keep only with >= {opts.th.get('min_samples')} samples, >= "
        f"{opts.th.get('min_per_term'):g} samples per fitted term, sky RMS <= "
        f"{opts.th.get('max_rms_arcsec'):g}\" and at most "
        f"+{opts.th.get('max_worse_pct'):g}% over the RMS before",
        "reject: copy the rejected files to <backup>\\rejected, copy the backup "
        "back (TheSky's in-memory model is NOT reverted: reopen in TPoint)",
        "after: " + MANUAL_AFTER,
    ]
    return {"backup": backup, "before": before, "steps": steps,
            "fit_script": script, "discover_script": discover_script(),
            "opts": {"fit": opts.fit, "action": opts.action or None,
                     "pinned": opts.action_pinned, "th": opts.th,
                     "wait_s": opts.wait_s}}


def format_plan(plan: dict) -> str:
    b = plan.get("before") or {}
    out = ["TPoint refit DRY RUN (nothing sent, nothing copied)"]
    if b.get("ok"):
        out.append(f"Model now: {b.get('model_points')} samples in the fit, sky RMS "
                   f"{b.get('sky_rms_arcsec')}\", {b.get('fitted_terms')} fitted "
                   f"terms, {b.get('active_model_file')} ({b.get('active_model_mtime')})")
    else:
        out.append("Model now: not read (" + str(b.get("note") or "no files") + ")")
    out.append("Steps:")
    out += [f"  {i}. {s}" for i, s in enumerate(plan["steps"], 1)]
    if plan.get("fit_script"):
        out.append("Fit script (JS): " + plan["fit_script"])
    out.append("Discover script (READ ONLY, --discover): " + plan["discover_script"])
    return "\n".join(out)


def format_discover(d: dict) -> str:
    out = [f"TheSkyXAction.execute: {d.get('execute')}; members seen: "
           f"{d.get('members_seen')}"]
    if d.get("error"):
        out.append("member listing failed: " + d["error"])
    if not d["actions"]:
        out.append("No TheSkyXAction member mentions TPoint / model / fit / super / "
                   "ProTrack / correction / pointing.")
    for a in d["actions"]:
        out.append(f"  {a['name']} = {a['value']}"
                   + (f"   (never used: {a['refusal']})" if a["refusal"] else ""))
    out.append("Nothing was executed. A name that looks like a fit can be tried "
               "ONCE at the console: photonscript tpoint-refit --go --test --fit "
               "action --action <Name>")
    return "\n".join(out)


# --------------------------------------------------------------- live wiring

def record_path(config, now: datetime) -> Path:
    return (Path(getattr(config, "data_dir", ".")) / "tpoint"
            / f"refit_{now.strftime('%Y%m%dT%H%M%SZ')}.json")


def live_deps(config, scheduler_url: str, need_sky: bool, ask=input,
              say=print) -> Deps:
    """The real adapters: TheSky TCP 3040 (only when the fit form needs it),
    the scheduler's GET /api/arm, TPoint's files, a JSON record."""
    from photonscript.scheduler import tpoint_files as tf
    from photonscript.telescope_agent import tpoint_autocal_live as tal
    sky = None
    if need_sky:
        from photonscript.telescope_agent.thesky_client import client_from_config
        cl = client_from_config(config)
        cl.timeout = max(float(cl.timeout), 60.0)
        sky = tal.TheSkyLink(cl)

    def save_record(rec: dict) -> None:
        p = record_path(config, datetime.utcnow())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
        if rec.get("status") == "kept":
            tf.save(config, tf.stats(config))

    return Deps(sky=sky, sched=tal.SchedulerLink(scheduler_url),
                list_files=lambda: tf.list_files(config),
                stats=lambda lst: tf.stats(config, lst), ask=ask, say=say,
                save_record=save_record)
