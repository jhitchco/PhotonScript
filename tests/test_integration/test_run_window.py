"""2026-10-25: the desktop run window + locked-session check for the
automatic PixInsight jobs (autointegrate / integrate-watch). PixInsight
1.9.5 crashed in Qt6WebEngineCore on every overnight automatic run."""

from datetime import datetime, timedelta

import pytest

from photonscript.integration import autointegrate as ai
from photonscript.integration import run_window as rw
from photonscript.integration import watch
from tests.test_integration.test_ps161_autointegrate import Integrate, _lib, _no_blend
from tests.test_integration.test_ps31_33_watch import NOW, URL, Fakes, _cand

DAY = datetime(2026, 10, 25, 14, 0)
NIGHT = datetime(2026, 10, 25, 1, 5)


def test_parse_window():
    assert rw.parse_window("") is None and rw.parse_window("off") is None
    s, e = rw.parse_window("08:00-23:00")
    assert (s.hour, e.hour) == (8, 23)
    with pytest.raises(ValueError):
        rw.parse_window("8am to 11pm")


def test_in_window_plain_and_wrapping():
    w = rw.parse_window("08:00-23:00")
    assert rw.in_window(w, DAY) and not rw.in_window(w, NIGHT)
    assert rw.in_window(w, datetime(2026, 10, 25, 8, 0))
    assert not rw.in_window(w, datetime(2026, 10, 25, 23, 0))
    wrap = rw.parse_window("22:00-06:00")
    assert rw.in_window(wrap, NIGHT) and not rw.in_window(wrap, DAY)
    assert rw.in_window(None, NIGHT)
    assert rw.in_window(rw.parse_window("00:00-00:00"), NIGHT)


def test_gate_reasons():
    assert rw.gate("08:00-23:00", True, DAY, locked=lambda: None) is None
    why = rw.gate("08:00-23:00", False, NIGHT, locked=lambda: "x")
    assert why.startswith("deferred: outside the run window (08:00-23:00, now 01:05")
    why = rw.gate("08:00-23:00", True, DAY, locked=lambda: "LogonUI.exe running")
    assert why.startswith("deferred: Windows session locked (LogonUI.exe")
    assert rw.gate("08:00-23:00", False, DAY, locked=lambda: "x") is None   # check off
    assert rw.gate("garbage", False, NIGHT) is None                         # never blocks
    assert rw.gate("", True, NIGHT, locked=lambda: None) is None


def test_session_locked_reads_never_raise(monkeypatch):
    r = rw.session_locked()
    assert r is None or isinstance(r, str)
    monkeypatch.setattr(rw.os, "name", "nt")
    monkeypatch.setattr(rw, "_input_desktop_locked", lambda: True)
    assert "input desktop" in rw.session_locked()
    monkeypatch.setattr(rw, "_input_desktop_locked", lambda: None)
    monkeypatch.setattr(rw, "_logonui_in_session", lambda: True)
    assert "LogonUI" in rw.session_locked()
    monkeypatch.setattr(rw, "_logonui_in_session", lambda: None)
    assert rw.session_locked() is None


def _auto(tmp_path, **kw):
    wo = watch.WatchOptions(base_url=URL, staging_root=tmp_path / "Staging",
                            run_window="08:00-23:00", lock_check=True, **kw)
    return ai.AutoOptions(watch=wo, library=_lib(tmp_path), settle_min=0)


def _ai_cycle(o, fk, local_now, locked=lambda: None):
    lines = []
    out = ai.cycle(o, run_integrate=fk.run_integrate(o.watch.staging_root),
                   run_blend=lambda t: {}, discover_blend=_no_blend, send=None,
                   get=fk.get, post=fk.post, pi_running=lambda: [], echo=lines.append,
                   now=NOW, local_now=local_now, locked=locked, load_guard=lambda: None)
    return out, lines


def test_autointegrate_defers_outside_the_window_then_runs(tmp_path):
    o = _auto(tmp_path)
    fk = Integrate([_cand()])
    out, lines = _ai_cycle(o, fk, NIGHT)
    assert out["skipped"] == "deferred" and fk.runs == []
    assert any(s.startswith("deferred: outside the run window") for s in lines)
    assert not (o.watch.staging_root / watch.LOCK_NAME).exists()
    out, _ = _ai_cycle(o, fk, DAY)                       # next allowed tick
    assert len(fk.runs) == 1 and out["ran"]["posted"]


def test_autointegrate_defers_on_a_locked_session(tmp_path):
    o = _auto(tmp_path)
    fk = Integrate([_cand()])
    out, lines = _ai_cycle(o, fk, DAY, locked=lambda: "LogonUI.exe running (lock screen)")
    assert out["skipped"] == "deferred" and fk.runs == []
    assert any("deferred: Windows session locked" in s for s in lines)


def test_autointegrate_dry_run_only_says_it(tmp_path):
    o = _auto(tmp_path, dry_run=True)
    fk = Integrate([_cand()])
    out, lines = _ai_cycle(o, fk, NIGHT)
    assert out.get("skipped") is None and out["would_run"]["target"] == "Andromeda Galaxy"
    assert any("a real cycle would wait" in s for s in lines)


def test_integrate_watch_defers_outside_the_window(tmp_path):
    o = watch.WatchOptions(base_url=URL, staging_root=tmp_path / "Staging",
                           run_window="08:00-23:00", lock_check=False)
    fk = Fakes([_cand()])
    kw = dict(run_integrate=fk.run_integrate(o.staging_root), get=fk.get, post=fk.post,
              pi_running=lambda: [], echo=lambda s: None, now=NOW)
    out = watch.cycle(o, local_now=NIGHT, **kw)
    assert out["skipped"] == "deferred" and fk.runs == []
    out = watch.cycle(o, local_now=DAY + timedelta(hours=1), **kw)
    assert len(fk.runs) == 1


def test_bare_options_have_no_window():
    o = watch.WatchOptions(base_url=URL, staging_root=None)
    assert o.run_window == "" and o.lock_check is False


def test_config_defaults_and_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    assert c.autointegrate_window_local == "08:00-23:00" and c.autointegrate_lock_check is True
    names = {f[0] for f in _CONFIG_FIELDS}
    assert {"autointegrate_window_local", "autointegrate_lock_check"} <= names


def test_cli_window_opts():
    from types import SimpleNamespace
    from photonscript.cli import _run_window_opts
    cfg = SimpleNamespace(autointegrate_window_local="08:00-23:00", autointegrate_lock_check=True)
    assert _run_window_opts(cfg, False, print) == {"run_window": "08:00-23:00", "lock_check": True}
    assert _run_window_opts(cfg, True, print) == {"run_window": "", "lock_check": False}
    said = []
    bad = SimpleNamespace(autointegrate_window_local="nope", autointegrate_lock_check=False)
    assert _run_window_opts(bad, False, said.append)["run_window"] == ""
    assert said and "WARNING" in said[0]


def test_cli_has_anytime():
    from typer.testing import CliRunner
    from photonscript.cli import app
    for cmd in ("autointegrate", "integrate-watch"):
        r = CliRunner().invoke(app, [cmd, "--help"])
        assert r.exit_code == 0 and "--anytime" in r.output
