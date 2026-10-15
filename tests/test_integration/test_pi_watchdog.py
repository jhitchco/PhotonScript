"""PixInsight hang watchdog (runner.py) + Gaia preflight (pigaia.py).

2026-10-14: the M31 v6 finish hung 3.5 h in a VizieR download after
ImageSolver found no Gaia DR3 database. No PixInsight here: popen, the
process tree, the clock and Pushover are fakes; the PixInsight settings are
a fabricated minimal XSDT file with the real shape (no real content)."""

import os
from pathlib import Path

import pytest

from photonscript.integration import finish as fn
from photonscript.integration import finish_retry as fr
from photonscript.integration import pigaia, runner
from photonscript.shared import ledger as L


# ----------------------------------------------------------------- fakes

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(float(s), 1.0)


class StallingProc:
    """Writes `lines` on its first poll, then never logs again; exits only
    when killed (or after exit_after polls)."""

    def __init__(self, log, lines, pid=4321, exit_after=None):
        self.log, self.lines, self.pid, self.exit_after = log, lines, pid, exit_after
        self.calls, self.killed = 0, False

    def poll(self):
        self.calls += 1
        if self.calls == 1:
            self.log.write_text("\n".join(self.lines) + "\n")
        if self.killed:
            return 1
        if self.exit_after and self.calls >= self.exit_after:
            return 0
        return None


def _exe(tmp_path):
    exe = tmp_path / "PixInsight.exe"
    exe.write_text("x")
    return exe


def _run(tmp_path, proc, clock, **kw):
    notes, kills = [], []

    def kill(pid):
        kills.append(pid)
        proc.killed = True
        return [pid, pid + 1]
    kw.setdefault("stall_min", 60)
    kw.setdefault("solve_stall_min", 20)
    kw.setdefault("hang_action", "close")
    kw.setdefault("stall_cpu_pct", 10)
    kw.setdefault("cpu", lambda pid: None)      # never a real process's CPU
    r = runner.run_script(tmp_path / "finish_run.js", proc.log, exe=str(_exe(tmp_path)), running=lambda: [],
                          popen=lambda a: proc, sleep=clock.sleep, clock=clock, poll_s=15,
                          echo=lambda s: None, notify=notes.append, kill=kill, **kw)
    return r, notes, kills


SOLVE_LINES = ["2026-10-14T05:00:00Z  STAGE START Andromeda_Galaxy_OSC / finish",
               "2026-10-14T05:00:01Z  plate solve: seeding at RA 10.685",
               "2026-10-14T05:00:02Z  STAGE START Andromeda_Galaxy_OSC / plate solve"]


# ----------------------------------------------------------------- watchdog

def test_solve_stage_hang_closes_own_pixinsight_after_20_min(tmp_path):
    clock = Clock()
    proc = StallingProc(tmp_path / "finish.log", SOLVE_LINES)
    r, notes, kills = _run(tmp_path, proc, clock)
    assert not r["ok"]
    assert r["hung"] == "hung: no log progress 20 min in Andromeda_Galaxy_OSC / plate solve"
    assert r["hang_stage"] == "Andromeda_Galaxy_OSC / plate solve"
    assert kills == [4321]                      # only the pid this runner started
    assert r["killed"] == [4321, 4322]
    assert len(notes) == 1 and "closed it (pid 4321)" in notes[0] and "plate solve" in notes[0]
    assert 20 * 60 <= clock.t - 1000.0 < 22 * 60


def test_other_stage_gets_60_min(tmp_path):
    clock = Clock()
    lines = SOLVE_LINES + ["2026-10-14T05:03:00Z  plate solve: OK on try 1",
                           "2026-10-14T05:03:00Z  STAGE END   Andromeda_Galaxy_OSC / plate solve",
                           "2026-10-14T05:03:01Z  deconvolution: BlurXTerminator"]
    proc = StallingProc(tmp_path / "finish.log", lines)
    r, notes, kills = _run(tmp_path, proc, clock)
    assert r["hung"] == "hung: no log progress 60 min in Andromeda_Galaxy_OSC / finish"
    assert 60 * 60 <= clock.t - 1000.0 < 62 * 60 and kills == [4321]


def test_progress_resets_the_stall_clock(tmp_path):
    clock = Clock()
    log = tmp_path / "finish.log"

    class Growing(StallingProc):
        def poll(self):
            self.calls += 1
            # a line every 10 min for 3 h, then EXIT OK
            if self.calls % 40 == 1:
                n = self.calls // 40
                self.log.write_text("\n".join(SOLVE_LINES + [f"line {i}" for i in range(n)]) + "\n")
            if self.calls >= 720:
                self.log.write_text("\n".join(SOLVE_LINES + ["EXIT OK"]) + "\n")
                return 0
            return None
    proc = Growing(log, [])
    r, notes, kills = _run(tmp_path, proc, clock)
    assert r["ok"] and not r["hung"] and notes == [] and kills == []


def test_busy_cpu_without_log_lines_is_not_a_hang(tmp_path):
    """The v6 integration: 145 min of debayer without a log line, at full CPU."""
    clock = Clock()
    lines = ["x  STAGE START OSC / debayer"]
    proc = StallingProc(tmp_path / "pipeline.log", lines + ["EXIT OK"], exit_after=700)   # ~175 min
    used = {"s": 0.0}

    def cpu(pid):
        assert pid == 4321
        used["s"] += 15 * 6.0                 # six cores busy per 15 s poll
        return used["s"]
    r, notes, kills = _run(tmp_path, proc, clock, cpu=cpu)
    assert r["ok"] and not r["hung"] and notes == [] and kills == []


def test_idle_cpu_without_log_lines_is_a_hang(tmp_path):
    clock = Clock()
    proc = StallingProc(tmp_path / "finish.log", SOLVE_LINES)
    used = {"s": 50.0}

    def cpu(pid):
        used["s"] += 0.2                      # ~1 % of a core: waiting on a dead download
        return used["s"]
    r, notes, kills = _run(tmp_path, proc, clock, cpu=cpu)
    assert r["hung"].startswith("hung: no log progress 20 min") and kills == [4321]


def test_tree_cpu_seconds_sums_the_tree():
    class T:
        def __init__(self, u, s):
            self.user, self.system = u, s
    a = FakeP(10, [FakeP(11)])
    a.cpu_times = lambda: T(3.0, 1.0)
    a.kids[0].cpu_times = lambda: T(2.0, 0.5)
    ps = FakePsutil([a, a.kids[0]])
    assert runner.tree_cpu_seconds(10, psutil_mod=ps) == 6.5
    assert runner.tree_cpu_seconds(None, psutil_mod=ps) is None
    assert runner.tree_cpu_seconds(77, psutil_mod=ps) is None


def test_alert_action_only_alerts_and_keeps_waiting(tmp_path):
    clock = Clock()
    proc = StallingProc(tmp_path / "finish.log", SOLVE_LINES + ["EXIT OK"], exit_after=400)
    r, notes, kills = _run(tmp_path, proc, clock, hang_action="alert")
    assert kills == [] and r["hung"] == "" and r["killed"] == []
    assert len(notes) == 1 and "left running" in notes[0]
    assert r["ok"] and r["exit_code"] == 0


def test_no_pid_means_never_kill(tmp_path):
    clock = Clock()
    proc = StallingProc(tmp_path / "finish.log", SOLVE_LINES, pid=None, exit_after=200)
    r, notes, kills = _run(tmp_path, proc, clock)
    assert kills == [] and r["hung"] == "" and len(notes) == 1


def test_stall_limits_zero_turn_the_watchdog_off(tmp_path):
    clock = Clock()
    proc = StallingProc(tmp_path / "finish.log", SOLVE_LINES, exit_after=500)
    r, notes, kills = _run(tmp_path, proc, clock, stall_min=0, solve_stall_min=0)
    assert notes == [] and kills == [] and not r["hung"]


def test_stage_tracker_nesting_and_solve_detection():
    t = runner.StageTracker()
    t.feed("x  STAGE START M31 / finish")
    assert t.current == "M31 / finish" and not t.solving
    t.feed("x  STAGE START M31 / SPCC")
    assert t.solving
    t.feed("x  STAGE END   M31 / SPCC")
    assert t.current == "M31 / finish" and not t.solving
    t.feed("x  STAGE START VizieR download")
    assert t.solving
    t.feed("x  STAGE END   M31 / finish (12.30 min)")      # closes the inner one too
    assert t.current == "" and not t.solving


def test_watchdog_settings_defaults_and_bad_action():
    w = runner.watchdog_settings(60, 20, "explode", 10)
    assert w == {"stall_min": 60.0, "solve_stall_min": 20.0, "hang_action": "close", "cpu_pct": 10.0}
    assert runner.watchdog_settings(5, 2, "ALERT", 0)["hang_action"] == "alert"


def test_config_defaults():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    names = {f[0] for f in _CONFIG_FIELDS}
    for k, v in (("pixinsight_stall_min", 60.0), ("pixinsight_solve_stall_min", 20.0),
                 ("pixinsight_hang_action", "close"), ("pixinsight_gaia_check", True),
                 ("pixinsight_stall_cpu_pct", 10.0)):
        assert getattr(c, k) == v and k in names, k


class FakeP:
    def __init__(self, pid, kids=(), dies=True):
        self.pid, self.kids, self.dies = pid, list(kids), dies
        self.terminated = self.killed = False

    def children(self, recursive=False):
        out = []
        for k in self.kids:
            out.append(k)
            if recursive:
                out += k.children(True)
        return out

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class FakePsutil:
    def __init__(self, procs):
        self.procs = {p.pid: p for p in procs}

    def Process(self, pid):
        if pid not in self.procs:
            raise LookupError(pid)
        return self.procs[pid]

    def wait_procs(self, procs, timeout=None):
        return [p for p in procs if p.dies], [p for p in procs if not p.dies]


def test_kill_tree_only_the_started_tree():
    grandkid = FakeP(12, dies=False)
    kid = FakeP(11, [grandkid])
    ours = FakeP(10, [kid])
    jeremys = FakeP(99)                               # another PixInsight: never touched
    ps = FakePsutil([ours, kid, grandkid, jeremys])
    assert sorted(runner.kill_tree(10, psutil_mod=ps, wait_s=0)) == [10, 11, 12]
    assert ours.terminated and kid.terminated and grandkid.terminated and grandkid.killed
    assert not jeremys.terminated and not jeremys.killed
    assert runner.kill_tree(None, psutil_mod=ps) == []
    assert runner.kill_tree(55, psutil_mod=ps) == []   # gone already


# ----------------------------------------------------------------- finish retry after a hang

def _hung_run(root):
    d = Path(root) / "Andromeda_Galaxy_piggyback_20261014-0856"
    for m in ("OSC", "OSC_hdr"):
        p = d / "out" / "master" / f"master_{m}.xisf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    from datetime import datetime, timezone
    led = L.Ledger(campaign="Andromeda Galaxy", rig="piggyback", run=d.name, version=6,
                   created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), reported=True,
                   machine={"integration": {"ok": True},
                            "finish": {"ok": False,
                                       "hung": "hung: no log progress 20 min in M31 / plate solve"},
                            "trigger": {"by": "autointegrate"}})
    L.save(d / "ledger.json", led)
    return d / "ledger.json"


def test_hung_finish_retries_first_without_the_solve(tmp_path):
    f = _hung_run(tmp_path)
    n = fr.need_for(f, L.load(f), roots=[tmp_path])
    assert n and n.attempt == 1 and not n.solve
    assert "hung: no log progress 20 min" in n.why


# ----------------------------------------------------------------- Gaia settings fixture

HEAD = ('<?xml version="1.0" encoding="UTF-8"?><!--\nfabricated test settings\n-->'
        '<xsdt version="1.0" xmlns="http://www.pixinsight.com/xsdt" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')


def _v(k, path):
    return f'<v k="{k}" t="s">{path}</v>'


def gaia_block(dr3, dr3sp, extra=""):
    vals = "".join(_v(f"DR3DatabaseFilePath{i:02d}", p) for i, p in enumerate(dr3))
    vals += "".join(_v(f"DR3SPDatabaseFilePath{i:02d}", p) for i, p in enumerate(dr3sp))
    return f'<i k="Gaia">{vals}{extra}</i>'


def settings_xml(module_children: str) -> str:
    return (HEAD + '<i k="Global"><i k="Statistics"><i k="Process"><i k="Gaia">'
            '<v k="ExecutionCount" t="u" v="1"/></i></i></i></i>'
            f'<i k="ModuleData">{module_children}</i>'
            '<i k="Modules"><v k="Count" t="u" v="2"/></i></xsdt>\n')


@pytest.fixture
def gaia_disk(tmp_path):
    dr3 = [tmp_path / "Gaia" / "DR3" / f"gdr3-1.0.0-{i:02d}.xpsd" for i in range(1, 17)]
    sp = [tmp_path / "Gaia" / "DR3SP" / f"gdr3sp-1.0.0-s-{i:02d}.xpsd" for i in range(1, 5)]
    for p in dr3 + sp:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return [p.as_posix() for p in dr3], [p.as_posix() for p in sp]


def _pleiades(tmp_path, gaia_disk, slot2=None):
    d = tmp_path / "Pleiades"
    d.mkdir()
    dr3, sp = gaia_disk
    s1 = settings_xml('<i k="FastIntegration"><v k="X" t="i" v="1"/></i>'
                      + gaia_block(dr3, sp, '<i k="Interfaces"><i k="Gaia"><i k="Geometry">'
                                            '<v k="Height" t="i" v="489"/></i></i></i>')
                      + '<i k="ImageSolver"><v k="Y" t="i" v="2"/></i>')
    (d / "core-001-pxi.settings").write_text(s1, encoding="utf-8")
    (d / "core-002-pxi.settings").write_text(
        slot2 if slot2 is not None else
        settings_xml('<i k="FastIntegration"><v k="X" t="i" v="1"/></i>'
                     '<i k="ImageSolver"><v k="Y" t="i" v="2"/></i>'), encoding="utf-8")
    (d / "core-001-pxi.settings.bak-20261014-gaia-dr3").write_text("not a slot", encoding="utf-8")
    (d / "PixInsightLicense").write_text("not read", encoding="utf-8")
    return d


def test_parse_gaia_paths_from_module_data_only(tmp_path, gaia_disk):
    d = _pleiades(tmp_path, gaia_disk)
    assert sorted(pigaia.slot_files(d)) == [1, 2]
    g = pigaia.gaia_paths(d / "core-001-pxi.settings")
    assert g["DR3"] == gaia_disk[0] and g["DR3SP"] == gaia_disk[1]
    assert pigaia.gaia_paths(d / "core-002-pxi.settings") == {"DR3": [], "DR3SP": []}


def test_check_ok_on_slot_1_and_warns_about_the_others(tmp_path, gaia_disk):
    d = _pleiades(tmp_path, gaia_disk)
    st = pigaia.check(d, running_count=0)
    assert st.slot == 1 and st.dr3_ok and st.dr3sp_ok and len(st.dr3) == 16
    assert st.solve_block_reason() == ""
    assert any("slot(s) 2" in w and "--fix" in w for w in st.warnings)


def test_check_slot_2_when_an_instance_runs(tmp_path, gaia_disk):
    d = _pleiades(tmp_path, gaia_disk)
    st = pigaia.check(d, running_count=1)
    assert st.slot == 2 and not st.dr3_ok
    assert "not set in PixInsight slot 2" in st.solve_block_reason()
    assert any("would use slot 2" in w for w in st.warnings)


def test_missing_dr3_files_block_the_solve(tmp_path, gaia_disk):
    d = _pleiades(tmp_path, gaia_disk)
    os.remove(gaia_disk[0][3])
    st = pigaia.check(d, running_count=0)
    assert not st.dr3_ok and st.dr3_missing == [gaia_disk[0][3]]
    pf = pigaia.preflight(lambda s: None, d=d, running_count=0)
    assert pf["solve"] is False and "missing on disk" in pf["why"]


def test_unknown_settings_keep_the_solve(tmp_path):
    st = pigaia.check(tmp_path / "nowhere", running_count=0)
    assert st.unknown and st.solve_block_reason() == ""
    assert pigaia.preflight(lambda s: None, d=tmp_path / "nowhere", running_count=0)["solve"] is True


def test_finish_gaia_gate_drops_the_solve(tmp_path):
    o = fn.Options(master=tmp_path / "m.xisf", name="M31")
    res = {"ra_deg": 10.68, "dec_deg": 41.27, "notes": []}
    g = fn.gaia_gate(o, res, lambda s: None,
                     preflight=lambda echo: {"solve": False, "why": "Gaia DR3 files missing on disk"})
    assert g["solve"] is False and res["ra_deg"] is None and res["dec_deg"] is None
    assert "plate solve off: Gaia DR3 files missing" in res["notes"][0]
    res2 = {"ra_deg": 10.68, "dec_deg": 41.27, "notes": []}
    fn.gaia_gate(o, res2, lambda s: None, preflight=lambda echo: {"solve": True, "why": ""})
    assert res2["ra_deg"] == 10.68 and res2["notes"] == []
    called = []
    fn.gaia_gate(fn.Options(master=None, name="x", solve=False), dict(res2), lambda s: None,
                 preflight=lambda echo: called.append(1))
    fn.gaia_gate(fn.Options(master=None, name="x", gaia_check=False), dict(res2), lambda s: None,
                 preflight=lambda echo: called.append(1))
    assert called == []


# ----------------------------------------------------------------- --fix

def test_fix_refused_while_pixinsight_runs(tmp_path, gaia_disk):
    d = _pleiades(tmp_path, gaia_disk)
    before = (d / "core-002-pxi.settings").read_bytes()
    with pytest.raises(pigaia.GaiaFixError, match="running"):
        pigaia.fix(d, running=lambda: [777])
    assert (d / "core-002-pxi.settings").read_bytes() == before
    assert not list(d.glob("core-002-pxi.settings.bak-*"))


def test_fix_inserts_gaia_block_with_backup(tmp_path, gaia_disk):
    from datetime import datetime
    d = _pleiades(tmp_path, gaia_disk)
    f2 = d / "core-002-pxi.settings"
    before = f2.read_bytes()
    src_before = (d / "core-001-pxi.settings").read_bytes()
    done = pigaia.fix(d, running=lambda: [], now=datetime(2026, 10, 15, 9, 30, 0), echo=lambda s: None)
    assert [x["slot"] for x in done] == [2]
    bak = d / "core-002-pxi.settings.bak-20261015-093000-gaia"
    assert bak.read_bytes() == before
    assert (d / "core-001-pxi.settings").read_bytes() == src_before      # source untouched
    g = pigaia.gaia_paths(f2)
    assert g["DR3"] == gaia_disk[0] and g["DR3SP"] == gaia_disk[1]
    after = f2.read_bytes().decode("utf-8")
    # placed in key order inside ModuleData, the rest of the file unchanged
    assert after.index('<i k="FastIntegration">') < after.index('<i k="Gaia"><v k="DR3') \
        < after.index('<i k="ImageSolver">')
    assert after.replace(after[after.index('<i k="Gaia"><v k="DR3'):after.index('<i k="ImageSolver">')], "") \
        == before.decode("utf-8")
    assert pigaia.check(d, running_count=1).dr3_ok                       # slot 2 now usable
    assert pigaia.fix(d, running=lambda: [], echo=lambda s: None) == []  # idempotent


def test_fix_replaces_old_values_in_an_existing_gaia_block(tmp_path, gaia_disk):
    old = settings_xml('<i k="FastIntegration"/>'
                       + gaia_block([], ["C:/old/gdr3sp-1.0.0-s-01.xpsd"],
                                    '<v k="SomeOtherKey" t="b" v="true"/>'
                                    '<i k="Interfaces"><i k="Gaia"><v k="Top" t="i" v="3"/></i></i>'))
    d = _pleiades(tmp_path, gaia_disk, slot2=old)
    pigaia.fix(d, running=lambda: [], echo=lambda s: None)
    f2 = d / "core-002-pxi.settings"
    g = pigaia.gaia_paths(f2)
    assert g["DR3"] == gaia_disk[0] and g["DR3SP"] == gaia_disk[1]
    txt = f2.read_text(encoding="utf-8")
    assert "C:/old/" not in txt and 'k="SomeOtherKey"' in txt and '<v k="Top" t="i" v="3"/>' in txt
    assert len(list(d.glob("core-002-pxi.settings.bak-*-gaia"))) == 1


def test_fix_refuses_without_a_complete_source(tmp_path, gaia_disk):
    d = _pleiades(tmp_path, gaia_disk)
    os.remove(gaia_disk[0][0])
    with pytest.raises(pigaia.GaiaFixError, match="no slot has a complete"):
        pigaia.fix(d, running=lambda: [])
