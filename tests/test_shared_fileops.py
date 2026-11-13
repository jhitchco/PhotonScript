"""replace_retry: os.replace retried on a brief Windows PermissionError
(WinError 5 from the virus scanner / indexer holding a fresh file)."""

import pytest

from photonscript.shared import fileops
from photonscript.shared import ledger as L


def _flaky(n_fail):
    calls = []

    def replace(src, dst):
        calls.append((src, dst))
        if len(calls) <= n_fail:
            raise PermissionError(5, "Access is denied")
        import os
        os.replace(src, dst)
    return replace, calls


def test_replace_retry_rides_out_a_short_hold(tmp_path):
    src, dst = tmp_path / "a.json.tmp", tmp_path / "a.json"
    src.write_text("new", encoding="ascii")
    dst.write_text("old", encoding="ascii")
    replace, calls = _flaky(3)
    sleeps = []
    assert fileops.replace_retry(src, dst, replace=replace, sleep=sleeps.append) == dst
    assert dst.read_text(encoding="ascii") == "new" and not src.exists()
    assert len(calls) == 4 and sleeps == pytest.approx([0.05, 0.10, 0.15])


def test_replace_retry_gives_up_and_raises(tmp_path):
    src, dst = tmp_path / "a.tmp", tmp_path / "a"
    src.write_text("x", encoding="ascii")
    replace, calls = _flaky(99)
    with pytest.raises(PermissionError):
        fileops.replace_retry(src, dst, attempts=3, replace=replace, sleep=lambda s: None)
    assert len(calls) == 3 and src.exists()


def test_other_errors_are_not_retried(tmp_path):
    calls = []

    def replace(src, dst):
        calls.append(1)
        raise FileNotFoundError(src)
    with pytest.raises(FileNotFoundError):
        fileops.replace_retry(tmp_path / "x", tmp_path / "y", replace=replace, sleep=lambda s: None)
    assert calls == [1]


def test_ledger_save_retries_a_held_replace(tmp_path, monkeypatch):
    replace, calls = _flaky(2)
    orig = fileops.replace_retry
    monkeypatch.setattr(fileops, "replace_retry",
                        lambda src, dst, **kw: orig(src, dst, replace=replace, sleep=lambda s: None))
    p = tmp_path / "run" / "ledger.json"
    L.save(p, L.Ledger(campaign="M 27", rig="rc16", run="run", version=1, machine={}))
    assert p.exists() and len(calls) == 3 and not (p.parent / "ledger.json.tmp").exists()
