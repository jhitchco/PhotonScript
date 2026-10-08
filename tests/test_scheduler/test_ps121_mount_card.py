"""PS-121: the dashboard Mount row shows where the mount points.

shared/mount_view formats the ninaAPI mount info (RA in HOURS, x15 for
degrees) into RA hh mm ss / Dec +dd mm ss with the epoch NINA reports,
Alt / Az to 1 decimal, pier side, hour angle and state; every value is "-"
with a hint when NINA #1's mount is not connected. /api/rigs carries that
block as devices.mount.view (the dashboard's existing 15 s poll), and
/api/status gains optional mount_alt / mount_az / mount_connected.
"""
import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.shared import mount_view as mv
from tests.test_scheduler.test_ps67_pointing import _FakeNina, _cfg, _mount

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
DASH = ROOT / "templates" / "dashboard.html"


def _info(**kw):
    m = {"Connected": True, "RightAscension": 12.05, "Declination": 0.4,
         "Altitude": 55.04, "Azimuth": 180.06, "SideOfPier": "pierWest",
         "Slewing": False, "TrackingEnabled": True, "AtPark": False,
         "SiderealTime": 13.05, "EquatorialSystem": 1}
    m.update(kw)
    return m


# ------------------------------------------------------------- formatting

@pytest.mark.parametrize("ra_h,text", [
    (12.05, "12h 03m 00s"), (5.5, "05h 30m 00s"), (0.0, "00h 00m 00s"),
    (23.999999, "00h 00m 00s"),            # rounds up, carries, wraps
    (17.97594, "17h 58m 33s"), (None, "-"), ("x", "-")])
def test_fmt_ra_hours(ra_h, text):
    assert mv.fmt_ra(ra_h) == text


@pytest.mark.parametrize("dec,text", [
    (41.269, "+41d 16m 08s"), (-0.4, "-00d 24m 00s"), (0.0, "+00d 00m 00s"),
    (66.63319, "+66d 37m 59s"), (-89.99999, "-90d 00m 00s"),
    (-0.0000001, "+00d 00m 00s"), (None, "-")])
def test_fmt_dec(dec, text):
    assert mv.fmt_dec(dec) == text


def test_fmt_alt_az_and_ha():
    assert mv.fmt_deg1(55.04) == "55.0" and mv.fmt_deg1(None) == "-"
    assert mv.fmt_ha(1.0) == "+1h 00m W"
    assert mv.fmt_ha(-0.6667) == "-0h 40m E"
    assert mv.fmt_ha(0.001) == "0h 00m"
    assert mv.fmt_ha(None) == "-"


@pytest.mark.parametrize("info,label", [
    ({"EquatorialSystem": 1}, "JNow"), ({"EquatorialSystem": "equTopocentric"}, "JNow"),
    ({"EquatorialSystem": 2}, "J2000"), ({"EquatorialSystem": "equJ2000"}, "J2000"),
    ({"Coordinates": {"Epoch": "JNOW"}}, "JNow"), ({"Coordinates": {"Epoch": 2}}, "J2000"),
    ({"EquatorialSystem": 0}, None), ({}, None)])
def test_epoch_label_from_what_nina_reports(info, label):
    assert mv.epoch_label(info) == label


@pytest.mark.parametrize("info,state", [
    ({"AtPark": True, "TrackingEnabled": False}, "parked"),
    ({"Slewing": True, "TrackingEnabled": True}, "slewing"),
    ({"TrackingEnabled": True}, "tracking"),
    ({"Tracking": True}, "tracking"),          # ninaAPI v1 name
    ({"TrackingEnabled": False}, "stopped")])
def test_mount_state(info, state):
    assert mv.mount_state(info) == state


# ----------------------------------------------------------- mount_view

def test_view_ra_is_hours_times_15_not_degrees():
    v = mv.mount_view(_info(), True)
    assert v["ra_hours"] == pytest.approx(12.05)
    assert v["ra_deg"] == pytest.approx(180.75)       # x15, never 12.05 deg
    assert v["ra"] == "12h 03m 00s" and v["dec"] == "+00d 24m 00s"
    assert v["epoch"] == "JNow" and v["state"] == "tracking"
    assert (v["alt"], v["az"], v["pier"]) == ("55.0", "180.1", "West")
    assert v["ha_hours"] == pytest.approx(1.0) and v["ha"] == "+1h 00m W"
    assert v["connected"] is True and v["hint"] == ""


def test_view_ha_from_config_longitude_when_driver_has_no_lst():
    from photonscript.scheduler.tracking_test import hour_angle
    now = datetime(2026, 10, 5, 4, 0, 0)
    info = _info()
    info.pop("SiderealTime")
    v = mv.mount_view(info, True, lon_deg=-109.021367, now=now)
    assert v["ha_hours"] == pytest.approx(hour_angle(12.05, now, -109.021367), abs=1e-3)
    # no LST source at all: HA unknown, everything else still shown
    v2 = mv.mount_view(info, True)
    assert v2["ha"] == "-" and v2["ra"] == "12h 03m 00s"


def test_view_parked_keeps_coordinates():
    v = mv.mount_view(_info(AtPark=True, TrackingEnabled=False), True)
    assert v["state"] == "parked" and v["ra"] != "-" and v["alt"] == "55.0"


@pytest.mark.parametrize("payload,conn,err,hint", [
    (None, False, "", mv.HINT_NOT_CONNECTED),
    (_info(Connected=False), False, "", mv.HINT_NOT_CONNECTED),
    ({}, False, "ConnectError: refused", mv.HINT_UNREACHABLE),
    (_info(RightAscension=None), True, "", mv.HINT_NOT_CONNECTED)])
def test_view_disconnected_is_all_dashes(payload, conn, err, hint):
    v = mv.mount_view(payload, conn, lon_deg=-109.0, error=err)
    assert v["connected"] is False and v["hint"] == hint
    for k in ("state", "ra", "dec", "alt", "az", "pier", "ha"):
        assert v[k] == "-"
    assert v["ra_deg"] is None and v["epoch"] is None


# ------------------------------------------------------------- /api/rigs

def _rigs(monkeypatch, tmp_path, mount_payload, conn=True, err=""):
    import photonscript.scheduler.app as app
    from photonscript.scheduler import preflight
    from photonscript.shared.models import TelescopeState
    monkeypatch.setattr(app, "_config", _cfg(tmp_path, piggyback_enabled=False))
    monkeypatch.setattr(app, "_telescope_state",
                        TelescopeState(current_target="M 31"))

    async def _fake(rc, dev, timeout=8):
        if dev == "mount":
            return conn, mount_payload, err
        return False, {}, ""
    monkeypatch.setattr(preflight, "_connected", _fake)
    # PS-174: fresh=True, each call here reads a new fake NINA
    rigs = asyncio.run(app.api_rigs(fresh=True))["rigs"]
    assert [r["rig"] for r in rigs] == ["rc16"]
    return rigs[0]


def test_api_rigs_mount_view_connected(tmp_path, monkeypatch):
    r = _rigs(monkeypatch, tmp_path, _info())
    m = r["devices"]["mount"]
    assert m["ra"] == 12.05 and m["parked"] is False      # old keys unchanged
    v = m["view"]
    assert v["ra"] == "12h 03m 00s" and v["ra_deg"] == pytest.approx(180.75)
    assert v["epoch"] == "JNow" and v["pier"] == "West" and v["ha"] == "+1h 00m W"
    assert r["target"] == "M 31"


def test_api_rigs_mount_view_parked(tmp_path, monkeypatch):
    v = _rigs(monkeypatch, tmp_path,
              _info(AtPark=True, TrackingEnabled=False))["devices"]["mount"]["view"]
    assert v["state"] == "parked" and v["connected"] is True


def test_api_rigs_mount_view_disconnected(tmp_path, monkeypatch):
    m = _rigs(monkeypatch, tmp_path, {"Connected": False}, conn=False)["devices"]["mount"]
    assert m["connected"] is False and "ra" not in m
    assert m["view"]["ra"] == "-" and m["view"]["hint"] == mv.HINT_NOT_CONNECTED
    m2 = _rigs(monkeypatch, tmp_path, {}, conn=False, err="Timeout")["devices"]["mount"]
    assert m2["view"]["hint"] == mv.HINT_UNREACHABLE


# ----------------------------------------------------------- /api/status

def test_status_payload_carries_alt_az_connected(monkeypatch):
    import photonscript.scheduler.app as app
    from photonscript.shared.models import AgentMessage, AgentRole, TelescopeState
    monkeypatch.setattr(app, "_telescope_state", TelescopeState())
    monkeypatch.setattr(app, "_rig_states", {})
    monkeypatch.setattr(app, "_ws_clients", [])

    async def run(payload):
        await app.on_agent_message(AgentMessage(
            sender=AgentRole.TELESCOPE, recipient=AgentRole.SCHEDULER,
            msg_type="telescope_state_update", payload=payload))
        return await app.api_status()
    st = asyncio.run(run({"rig": "rc16", "mount_ra": 12.05, "mount_dec": 0.4,
                          "mount_alt": 55.0, "mount_az": 180.0,
                          "mount_connected": True, "mount_side_of_pier": "West",
                          "mount_tracking": True}))
    t = st["telescope"]
    assert t["mount_ra"] == 12.05          # still HOURS in the status payload
    assert (t["mount_alt"], t["mount_az"], t["mount_connected"]) == (55.0, 180.0, True)
    assert st["rigs"]["rc16"]["mount_az"] == 180.0
    # an older agent's payload (no new keys) still parses: fields are optional
    old = asyncio.run(run({"rig": "rc16", "mount_ra": 1.0, "mount_dec": 2.0}))
    assert old["telescope"]["mount_alt"] is None
    assert old["telescope"]["mount_connected"] is None


class _Nina(_FakeNina):
    def __init__(self, connected=True):
        super().__init__()
        self.connected = connected

    async def get_mount_info(self):
        self.calls.append("mount")
        return {**_mount(), "Connected": self.connected}


@pytest.mark.parametrize("connected", [True, False])
def test_agent_poll_fills_alt_az_connected(tmp_path, monkeypatch, connected):
    from photonscript.telescope_agent import agent as agent_mod
    ag = agent_mod.TelescopeAgent(_cfg(tmp_path, observatory_tz="UTC"), rig="rc16")
    ag.nina = _Nina(connected)
    for name in ("_cooling_watchdog", "_dew_heater_watchdog"):
        async def _noop(*a, **k):
            return None
        monkeypatch.setattr(ag, name, _noop)

    async def _stop(_s):
        ag._running = False
    monkeypatch.setattr(agent_mod.asyncio, "sleep", _stop)
    ag._running = True
    asyncio.run(ag._nina_poll_loop())
    s = ag.state
    assert (s.mount_alt, s.mount_az, s.mount_connected) == (55.0, 180.0, connected)
    assert s.mount_ra == 12.05            # hours, unchanged (PS-67)


# ------------------------------------------------------------- template

def _mount_block():
    html = DASH.read_text(encoding="utf-8")
    a = html.index("// PS-121: where the mount points")
    b = html.index("// Roof (piggyback only)")
    return html, html[a:b]


def test_dashboard_mount_row_reads_the_view():
    html, block = _mount_block()
    assert "mnt.view" in block and "_mountRows(v)" in block
    assert "Parked &#8212; home" in block           # the old parked line stays
    for label in ("'RA'", "'Dec'", "'Alt'", "'Az'", "'Pier'", "'HA'", "epoch ?", "v.hint"):
        assert label in block
    assert "_fmtRa" not in html and "_fmtDec" not in html
    # refreshed by the existing /api/rigs poll, no new loop
    assert html.count("setInterval(refreshEquip") == 1


def test_mount_code_is_ascii():
    html = DASH.read_text(encoding="utf-8")
    helpers = html[html.index("// PS-121: where the mount points"):
                   html.index("function _toggleBtn")]
    row = html[html.index("// Mount (RC16 owns it"):html.index("// Roof (piggyback only)")]
    assert helpers.isascii() and row.isascii()
    src = (ROOT.parent / "shared" / "mount_view.py").read_text(encoding="utf-8")
    assert src.isascii()
