"""TPoint autocal (docs/TPOINT_AUTOCAL.md): the preflight, the backup plan,
the NINA hand-off description, the TheSky script builders, the dry-run CLI
and the safety fences (nothing in the module can send; --go --mode window
is not implemented, loop mode lives in tpoint_autocal_live and is tested in
test_tpoint_autocal_loop; the write scripts never join the read-only TheSky
set)."""
import re
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import thesky_client as tc
from photonscript.telescope_agent import tpoint_autocal as ta
from tests.test_scheduler.test_ps104_thesky_audit import _violations

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 12, 4, 0)


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("thesky_user_dir", str(tmp_path / "nosky"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _apps(**kw):
    a = {"sequence_running": {ta.NINA1_LABEL: [], ta.NINA2_LABEL: []},
         "stop_blockers": [], "thesky": {"answering": True, "mount_connected": True},
         "sun_alt_deg": -25.0}
    a.update(kw)
    return a


_BACKUP = {"dest": r"C:\astro\tpoint_backup\x", "copies": [{"src": "a", "dst": "b"}]}


def _status(pre, name):
    return next(c["status"] for c in pre["checks"] if c["name"] == name)


# ------------------------------------------------------------- preflight

def test_preflight_go_when_everything_passes():
    pre = ta.preflight({"state": "DISARMED"}, _apps(), True, _BACKUP)
    assert pre["go"], pre["blockers"]
    assert all(c["status"] == ta.PASS for c in pre["checks"])


@pytest.mark.parametrize("state", ["ARMED", "RUNNING", "PAUSED_UNSAFE",
                                   "PAUSED_OPERATOR", "WATCHING", "COMPLETE",
                                   "ERROR"])
def test_preflight_refuses_any_armer_state_but_disarmed(state):
    pre = ta.preflight({"state": state}, _apps(), True, _BACKUP)
    assert not pre["go"]
    assert _status(pre, "armer idle") == ta.FAIL


def test_preflight_unknowns_block():
    pre = ta.preflight(None, None, None, None)
    assert not pre["go"]
    assert {c["status"] for c in pre["checks"]} <= {ta.UNKNOWN, ta.PASS}
    assert _status(pre, "armer idle") == ta.UNKNOWN
    assert _status(pre, "roof open") == ta.UNKNOWN
    assert _status(pre, "TPoint backup") == ta.UNKNOWN


def test_preflight_nina_running_or_unreadable_blocks():
    run = _apps(sequence_running={ta.NINA1_LABEL: ["Smart Exposure"],
                                  ta.NINA2_LABEL: []})
    pre = ta.preflight({"state": "DISARMED"}, run, True, _BACKUP)
    assert _status(pre, f"{ta.NINA1_LABEL} idle") == ta.FAIL
    assert "Smart Exposure" in " ".join(pre["blockers"])
    unread = _apps(sequence_running={ta.NINA1_LABEL: [], ta.NINA2_LABEL: None})
    pre = ta.preflight({"state": "DISARMED"}, unread, True, _BACKUP)
    assert _status(pre, f"{ta.NINA2_LABEL} idle") == ta.UNKNOWN
    assert not pre["go"]


def test_preflight_roof_sun_thesky_capture_backup():
    d = {"state": "DISARMED"}
    assert _status(ta.preflight(d, _apps(), False, _BACKUP), "roof open") == ta.FAIL
    assert _status(ta.preflight(d, _apps(sun_alt_deg=-5.0), True, _BACKUP),
                   "dark") == ta.FAIL
    sky = _apps(thesky={"answering": False})
    assert _status(ta.preflight(d, sky, True, _BACKUP), "TheSky answering") == ta.FAIL
    nomount = _apps(thesky={"answering": True, "mount_connected": False})
    assert _status(ta.preflight(d, nomount, True, _BACKUP),
                   "TheSky answering") == ta.FAIL
    cap = _apps(stop_blockers=["calibration capture job running on rc16"])
    assert _status(ta.preflight(d, cap, True, _BACKUP), "no capture job") == ta.FAIL
    empty = {"dest": "x", "copies": []}
    assert _status(ta.preflight(d, _apps(), True, empty), "TPoint backup") == ta.FAIL


def test_preflight_automation_switches():
    d = {"state": "DISARMED"}
    pre = ta.preflight(d, _apps(), True, _BACKUP, auto_arm_enabled=True)
    assert _status(pre, "auto-arm off") == ta.FAIL and not pre["go"]
    pre = ta.preflight(d, _apps(), True, _BACKUP, auto_dusk_flats=True)
    assert _status(pre, "dusk flats") == ta.WARN and pre["go"]


# ------------------------------------------------------------- backup / handoff

def test_backup_plan_maps_files_under_a_stamped_folder():
    listing = {"files": [
        {"rel": r"TPoint\TPoint base run in.dat", "path": r"D:\sky\TPoint\TPoint base run in.dat",
         "size": 10},
        {"rel": r"Imaging System Profiles\x.ini", "path": r"D:\sky\x.ini", "size": 1}]}
    bp = ta.backup_plan(listing, NOW)
    assert bp["dest"] == r"C:\astro\tpoint_backup\20261012T040000Z"
    assert [c["dst"] for c in bp["copies"]] == [
        r"C:\astro\tpoint_backup\20261012T040000Z\TPoint\TPoint base run in.dat"]
    assert ta.backup_plan(None, NOW)["copies"] == []


def test_nina_handoff_is_a_description():
    h = ta.nina_handoff("http://localhost:1888/v2/api/")
    urls = [r["url"] for r in h["lend"]]
    assert "http://localhost:1888/v2/api/equipment/camera/disconnect" in urls
    assert urls.index("http://localhost:1888/v2/api/sequence/state") == 0
    assert h["return"][0]["url"].endswith("/equipment/camera/connect")


# ------------------------------------------------------------- scripts

def _all():
    out = {k: f() for k, f in ta.SCRIPT_BUILDERS.items()}
    out["camera_setup"] = ta.camera_setup_script(5, 2)
    out["ails_setup"] = ta.ails_setup_script(0.472, 5)
    out["slew"] = ta.slew_altaz_script(45.0, 120.5, "tpoint autocal 1")
    return out


def test_scripts_are_ascii_es5_and_set_out():
    for name, js in _all().items():
        assert js.isascii(), name
        assert "Out = " in js, name
        assert "=>" not in js and "let " not in js and "const " not in js, name


def test_read_only_builders_pass_the_ps104_denylist():
    for name in ta.READ_ONLY_SCRIPTS:
        js = ta.SCRIPT_BUILDERS[name]()
        assert not _violations(js), (name, _violations(js))


def test_write_builders_do_what_they_say():
    js = _all()
    assert "ccdsoftCamera.Connect();" in js["camera_connect"]
    assert "ccdsoftCamera.Disconnect();" in js["camera_disconnect"]
    assert "ccdsoftCamera.TakeImage();" in js["expose"]
    assert "sky6RASCOMTheSky.AutoMap();" in js["automap"]
    assert "SlewToAzAlt(120.5000, 45.0000, 'tpoint autocal 1')" in js["slew"]
    assert "ccdsoftCamera.Abort()" in js["abort"] and "sky6RASCOMTele.Abort()" in js["abort"]
    assert "TheSkyXAction.execute(TheSkyXAction.AutoPointingCalibration)" in \
        js["open_autocal_window"]
    assert "ccdsoftCamera.BinX = 2;" in js["camera_setup"]
    assert "AutomatedImageLinkSettings.imageScale = 0.47200;" in js["ails_setup"]
    # never a sync, a closed-loop slew, a park or a connect of the mount
    for name, s in js.items():
        assert not re.search(r"Sync\w*\s*\(|ClosedLoopSlew|Park|Unpark|"
                             r"sky6RASCOMTele\.Connect|SetTracking", s), name


def test_script_inputs_are_validated():
    with pytest.raises(ValueError):
        ta.slew_altaz_script(10.0, 100, "x")          # below min alt
    with pytest.raises(ValueError):
        ta.slew_altaz_script(89.0, 100, "x")          # zenith
    with pytest.raises(ValueError):
        ta.slew_altaz_script(45, 100, "x'); sky6RASCOMTele.Park(); ('")
    with pytest.raises(ValueError):
        ta.camera_setup_script(500, 2)
    with pytest.raises(ValueError):
        ta.camera_setup_script(5, 9)
    with pytest.raises(ValueError):
        ta.ails_setup_script("nan", 5)


def test_write_scripts_never_join_the_read_only_client():
    ro = "\n".join(tc.READ_ONLY_JS.values())
    for bad in ("TakeImage", "AutoMap", "ccdsoftCamera.Connect", "SlewToAzAlt",
                "AutoPointingCalibration)"):
        assert bad not in ro, bad


def test_module_cannot_send():
    src = (ROOT / "photonscript" / "telescope_agent" / "tpoint_autocal.py").read_text(
        encoding="utf-8")
    assert src.isascii()
    for bad in ("import socket", "httpx", "import requests", "run_script", "TheSkyClient",
                "urlopen", "subprocess", "shutil"):
        assert bad not in src, bad
    assert "\u2014" not in src


def test_go_is_not_implemented():
    with pytest.raises(NotImplementedError, match="not implemented"):
        ta.run_go()


# ------------------------------------------------------------- plan / CLI

_SKY = [{"alt": 40.0, "az": 100.0, "ha_h": -3.0, "dec_deg": 10.0, "side": "east"},
        {"alt": 50.0, "az": 250.0, "ha_h": 2.0, "dec_deg": 20.0, "side": "west"}]


def test_build_plan_loop_and_window(tmp_path):
    cfg = _cfg(tmp_path)
    p = ta.build_plan(cfg, points=2, sky_points=_SKY)
    assert p["requested_points"] == ta.MIN_POINTS and p["points"] == 2
    assert p["params"]["image_scale"] == pytest.approx(0.472)
    phases = [s["phase"] for s in p["steps"]]
    assert phases.index("preflight") < phases.index("backup") < phases.index("handoff") \
        < phases.index("run") < phases.index("finish")
    # default add (2026-10-20): solve + import file, no AutoMap, no AILS write
    assert p["add"] == "import" and p["solver"] == "imagelink"
    assert not any("AutoMap" in (s["script"] or "") for s in p["steps"])
    assert not any("AutomatedImageLinkSettings.imageScale =" in (s["script"] or "")
                   for s in p["steps"])
    assert any("ImageLink.execute()" in (s["script"] or "") for s in p["steps"])
    assert any("ComputeLocalSiderealTime" in (s["script"] or "") for s in p["steps"])
    assert any("Import Pointing Data" in s["what"] for s in p["steps"])
    am = ta.build_plan(cfg, points=2, sky_points=_SKY, add="automap")
    assert any("AutoMap" in (s["script"] or "") for s in am["steps"])
    assert not any("ImageLink.execute()" in (s["script"] or "") for s in am["steps"])
    fb = ta.build_plan(cfg, points=2, sky_points=_SKY, solver="fallback")
    assert any("ASTAP" in s["what"] for s in fb["steps"])
    with pytest.raises(ValueError):
        ta.build_plan(cfg, points=2, sky_points=_SKY, solver="pinpoint")
    w = ta.build_plan(cfg, points=900, mode="window")
    assert w["points"] == ta.MAX_POINTS
    assert any("AutoPointingCalibration" in (s["script"] or "") for s in w["steps"])
    assert not any("AutoMap" in (s["script"] or "") for s in w["steps"])
    with pytest.raises(ValueError):
        ta.build_plan(cfg, mode="auto")
    txt = ta.format_plan(p, ta.preflight(None, None, None, None))
    assert "DRY RUN" in txt and "NO GO" in txt and "--test" in txt
    assert "not implemented" in ta.format_plan(w)
    assert txt.isascii()


def test_cli_dry_run_sends_nothing(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    import socket
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)

    def boom(*a, **k):
        raise AssertionError("dry run opened a connection")
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--points", "8", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "DRY RUN" in r.output and "NO GO" in r.output
    assert "equipment/camera/disconnect" in r.output
    assert "Points for --go" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--go", "--mode", "window"])
    assert r.exit_code == 2 and "not implemented" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--go", "--dry-run"])
    assert r.exit_code == 2
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--mode", "nope"])
    assert r.exit_code == 2
