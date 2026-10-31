"""PS-22: PixInsight runner (no PixInsight needed: popen is faked)."""
import pytest

from photonscript.integration import runner


class FakeProc:
    def __init__(self, log, lines, polls_until_exit=3, rc=0):
        self.log, self.lines, self.n, self.rc = log, lines, polls_until_exit, rc
        self.calls = 0

    def poll(self):
        self.calls += 1
        # the PJSR log writer rewrites the whole file on every line
        k = min(len(self.lines), self.calls)
        self.log.write_text("\n".join(self.lines[:k]) + "\n")
        return self.rc if self.calls >= self.n else None


def _exe(tmp_path):
    exe = tmp_path / "PixInsight.exe"
    exe.write_text("x")
    return exe


def test_refuses_when_pixinsight_is_running(tmp_path):
    started = []
    with pytest.raises(runner.PixInsightBusy):
        runner.run_script(tmp_path / "s.js", tmp_path / "l.log", exe=str(_exe(tmp_path)),
                          running=lambda: [4242], popen=lambda a: started.append(a))
    assert started == []          # nothing launched


def test_success_needs_exit_ok_and_uses_force_exit(tmp_path):
    log = tmp_path / "pipeline.log"
    seen, args = [], []
    lines = ["STAGE START a", "STAGE END a", "EXIT OK"]

    def popen(a):
        args.append(a)
        return FakeProc(log, lines)
    r = runner.run_script(tmp_path / "s.js", log, exe=str(_exe(tmp_path)), running=lambda: [],
                          popen=popen, sleep=lambda s: None, echo=seen.append, poll_s=0)
    assert r["ok"] and r["exit_code"] == 0 and r["last_line"] == "EXIT OK"
    assert "--force-exit" in args[0] and "--automation-mode" in args[0]
    assert any("STAGE START a" in s for s in seen)


def test_failure_without_exit_ok(tmp_path):
    log = tmp_path / "pipeline.log"
    r = runner.run_script(tmp_path / "s.js", log, exe=str(_exe(tmp_path)), running=lambda: [],
                          popen=lambda a: FakeProc(log, ["STAGE START a", "ERROR: boom"]),
                          sleep=lambda s: None, echo=lambda s: None, poll_s=0)
    assert not r["ok"] and r["last_line"] == "ERROR: boom"


def test_read_new_reads_increments_and_survives_rewrite(tmp_path):
    p = tmp_path / "x.log"
    assert runner.read_new(p, 0) == ("", 0)               # missing file
    p.write_bytes(b"a\n")
    t, pos = runner.read_new(p, 0)
    assert t == "a\n"
    p.write_bytes(b"a\nb\n")
    t, pos = runner.read_new(p, pos)
    assert t == "b\n"
    p.write_bytes(b"z\n")                                  # shorter: start over
    t, pos = runner.read_new(p, pos)
    assert t == "z\n"


def test_missing_exe(tmp_path):
    with pytest.raises(FileNotFoundError):
        runner.run_script(tmp_path / "s.js", tmp_path / "l", exe=str(tmp_path / "nope.exe"),
                          running=lambda: [])


def test_launch_kwargs_minimized_below_normal():
    import os
    import subprocess
    from photonscript.integration import runner
    kw = runner.launch_kwargs("minimized", "below_normal")
    if os.name != "nt":
        assert kw == {}
        return
    assert kw["startupinfo"].wShowWindow == 7
    assert kw["creationflags"] == subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert runner.launch_kwargs("normal", "normal") == {}


# PS-31: PixInsight crashing at startup (Qt6WebEngineCore.dll, 0x80000003)
# while the desktop display is off: no log line, FAILED in 0.25 min "()".

class CrashProc:
    """Exits at once with the Windows crash code, writes no log."""
    def __init__(self, rc=0x80000003):
        self.rc = rc

    def poll(self):
        return self.rc


def test_every_launch_disables_the_webengine_gpu(tmp_path):
    log = tmp_path / "pipeline.log"
    envs = []

    def popen(a, env=None):
        envs.append(env)
        return FakeProc(log, ["EXIT OK"])
    r = runner.run_script(tmp_path / "s.js", log, exe=str(_exe(tmp_path)), running=lambda: [],
                          popen=popen, sleep=lambda s: None, echo=lambda s: None, poll_s=0)
    assert r["ok"] and r["attempts"] == 1 and not r["startup_crash"]
    flags = envs[0]["QTWEBENGINE_CHROMIUM_FLAGS"].split()
    assert "--disable-gpu" in flags and "--disable-gpu-compositing" in flags
    assert envs[0].get("QT_OPENGL") == runner.os.environ.get("QT_OPENGL")   # untouched


def test_pixinsight_env_keeps_flags_and_adds_software():
    env = runner.pixinsight_env({"QTWEBENGINE_CHROMIUM_FLAGS": "--foo --disable-gpu", "X": "1"})
    assert env["QTWEBENGINE_CHROMIUM_FLAGS"] == "--foo --disable-gpu --disable-gpu-compositing"
    assert env["X"] == "1" and "QT_OPENGL" not in env
    sw = runner.pixinsight_env({}, software=True)
    assert sw["QT_OPENGL"] == "software" and sw["QT_QUICK_BACKEND"] == "software"
    assert runner.exit_code_text(0x80000003) == "0x80000003"
    assert runner.exit_code_text(-2147483645) == "0x80000003"
    assert runner.exit_code_text(1) == "1"


def test_startup_crash_is_named_and_retried_once_with_software_rendering(tmp_path):
    log = tmp_path / "pipeline.log"
    envs, seen = [], []

    def popen(a, env=None):
        envs.append(env)
        return CrashProc() if len(envs) == 1 else FakeProc(log, ["STAGE START a", "EXIT OK"])
    pushed = []
    r = runner.run_script(tmp_path / "s.js", log, exe=str(_exe(tmp_path)), running=lambda: [],
                          popen=popen, sleep=lambda s: None, echo=seen.append, poll_s=0,
                          notify=pushed.append)
    assert r["ok"] and r["attempts"] == 2 and r["software_retry"]
    assert pushed == []                      # PS-212: a retry that worked is info
    assert envs[1]["QT_OPENGL"] == "software"
    assert any("code 0x80000003 before the script wrote its log" in s and "retrying" in s
               for s in seen)


def test_startup_crash_twice_fails_with_a_reason(tmp_path):
    log = tmp_path / "pipeline.log"
    n, pushed = [], []
    r = runner.run_script(tmp_path / "s.js", log, exe=str(_exe(tmp_path)), running=lambda: [],
                          popen=lambda a: n.append(1) or CrashProc(),
                          sleep=lambda s: None, echo=lambda s: None, poll_s=0,
                          notify=pushed.append)
    assert not r["ok"] and r["startup_crash"] and len(n) == 2
    # PS-212: both attempts crashed: one act push (the retry itself is info)
    assert len(pushed) == 1 and "crashed at startup twice" in pushed[0]
    assert r["exit_code"] == 0x80000003
    assert r["last_line"].startswith("PixInsight exited with code 0x80000003 before the script")


def test_script_error_is_not_a_startup_crash(tmp_path):
    """A script that logged and failed is not retried."""
    log = tmp_path / "pipeline.log"
    n = []
    r = runner.run_script(tmp_path / "s.js", log, exe=str(_exe(tmp_path)), running=lambda: [],
                          popen=lambda a: n.append(1) or FakeProc(log, ["ERROR: boom"], rc=1),
                          sleep=lambda s: None, echo=lambda s: None, poll_s=0)
    assert not r["ok"] and not r["startup_crash"] and len(n) == 1
    assert r["last_line"] == "ERROR: boom"
    # a clean exit 0 with no log is not a crash either (nothing to retry)
    n.clear()
    r = runner.run_script(tmp_path / "s2.js", tmp_path / "none.log", exe=str(_exe(tmp_path)),
                          running=lambda: [], popen=lambda a: n.append(1) or CrashProc(rc=0),
                          sleep=lambda s: None, echo=lambda s: None, poll_s=0)
    assert not r["ok"] and not r["startup_crash"] and len(n) == 1
