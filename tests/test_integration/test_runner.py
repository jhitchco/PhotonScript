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
