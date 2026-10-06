"""PS-70: PHD2 guide RMS must be honest arcsec (or labelled pixels).

PHD2's JSON-RPC replies arrive on the same socket as its event stream. Before
PS-70 refresh_pixel_scale() read back its own REQUEST, so the scale stayed 1.0
and guide-camera pixels were reported as arcsec ("Guide RMS 98\\"")."""
import asyncio
import json
import logging

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import GuidingMetrics, GuidingState
from photonscript.telescope_agent import phd2_client as pc
from photonscript.telescope_agent.phd2_client import (
    PHD2Client,
    guide_focal_length_mm,
    guide_rms_text,
    guide_scale_from_config,
)


def _cfg(**kw):
    return PhotonScriptConfig(_env_file=None, **kw)


class FakePHD2:
    """A canned PHD2 event server: sends a Version + AppState event on connect,
    answers RPCs by id (with an unrelated event squeezed in before the reply,
    as the real server does), and can push GuideStep events."""

    def __init__(self, replies):
        self.replies = replies           # method -> result, or ("error", msg)
        self.requests = []
        self.writers = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _send(self, w, obj):
        w.write((json.dumps(obj) + "\r\n").encode())
        await w.drain()

    async def _handle(self, reader, writer):
        self.writers.append(writer)
        await self._send(writer, {"Event": "Version", "PHDVersion": "2.6.13",
                                  "MsgVersion": 1})
        await self._send(writer, {"Event": "AppState", "State": "Stopped"})
        while True:
            line = await reader.readline()
            if not line:
                break
            req = json.loads(line)
            self.requests.append(req)
            await self._send(writer, {"Event": "LoopingExposures", "Frame": 1})
            r = self.replies.get(req["method"])
            if isinstance(r, tuple) and r[0] == "error":
                await self._send(writer, {"jsonrpc": "2.0", "id": req["id"],
                                          "error": {"code": 1, "message": r[1]}})
            else:
                await self._send(writer, {"jsonrpc": "2.0", "id": req["id"],
                                          "result": r})

    async def push(self, obj):
        for w in self.writers:
            await self._send(w, obj)

    async def close(self):
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()


def _step(ra_px, dec_px):
    return {"Event": "GuideStep", "Frame": 1, "RADistanceRaw": ra_px,
            "DECDistanceRaw": dec_px, "SNR": 30.0, "StarMass": 5000}


async def _connected(replies, config=None):
    srv = FakePHD2(replies)
    port = await srv.start()
    cl = PHD2Client("127.0.0.1", port, config=config)
    assert await cl.connect()
    return srv, cl


# --- config fallback math ----------------------------------------------------

def test_focal_length_derived_from_imaging_scale():
    fl = guide_focal_length_mm(_cfg())
    assert fl == pytest.approx(206.265 * 3.76 / 0.236)      # ~3286 mm
    assert 3270 < fl < 3300   # 1% over the nominal 3248
    assert guide_focal_length_mm(_cfg(guide_focal_length_mm=3248)) == 3248


def test_guide_scale_from_config_gp678c_and_binning():
    c = _cfg()
    assert guide_scale_from_config(c, 1) == pytest.approx(2.0 * 0.236 / 3.76)  # 0.126
    assert guide_scale_from_config(c, 2) == pytest.approx(2 * 2.0 * 0.236 / 3.76)
    assert guide_scale_from_config(_cfg(phd2_pixel_scale_arcsec=0.5), 2) == 0.5
    assert guide_scale_from_config(_cfg(guide_camera_pixel_um=0), 1) is None
    assert guide_scale_from_config(None, 1) is None


# --- reading PHD2's reply ------------------------------------------------------

async def test_pixel_scale_read_from_phd2_reply_not_the_request():
    srv, cl = await _connected({"get_pixel_scale": 0.13, "get_camera_binning": 1},
                               _cfg())
    try:
        await cl.refresh_pixel_scale()
        assert cl.pixel_scale == pytest.approx(0.13)
        m = cl.metrics
        assert m.scale_source == "phd2" and m.units == "arcsec"
        assert [r["method"] for r in srv.requests] == ["get_pixel_scale",
                                                       "get_camera_binning"]
        await cl._handle_event(_step(3.0, 4.0))            # 5 px total
        assert m.rms_total_px == pytest.approx(5.0)
        assert m.rms_total_arcsec == pytest.approx(0.65)   # 5 px x 0.13
        assert m.rms_ra_arcsec == pytest.approx(0.39)
        assert m.peak_dec_arcsec == pytest.approx(0.52)
    finally:
        await cl.disconnect()
        await srv.close()


async def test_null_scale_falls_back_to_config_with_phd2_binning():
    srv, cl = await _connected({"get_pixel_scale": None, "get_camera_binning": 2},
                               _cfg())
    try:
        await cl.refresh_pixel_scale()
        assert cl.metrics.scale_source == "config"
        assert cl.metrics.guide_binning == 2
        assert cl.pixel_scale == pytest.approx(2 * 2.0 * 0.236 / 3.76)
    finally:
        await cl.disconnect()
        await srv.close()


async def test_scale_of_one_means_unknown_and_binning_error_means_bin1():
    srv, cl = await _connected({"get_pixel_scale": 1.0,
                                "get_camera_binning": ("error", "camera not connected")},
                               _cfg())
    try:
        await cl.refresh_pixel_scale()
        assert cl.metrics.scale_source == "config"
        assert cl.pixel_scale == pytest.approx(2.0 * 0.236 / 3.76)
    finally:
        await cl.disconnect()
        await srv.close()


async def test_unknown_scale_reports_pixels_honestly():
    srv, cl = await _connected({"get_pixel_scale": None, "get_camera_binning": 1},
                               _cfg(guide_camera_pixel_um=0))
    try:
        await cl.refresh_pixel_scale()
        await cl._handle_event(_step(60.0, 80.0))
        m = cl.metrics
        assert m.units == "px" and m.scale_source is None
        assert m.rms_total_arcsec is None and m.rms_total_px == pytest.approx(100.0)
        txt = guide_rms_text(m)
        assert "guide px" in txt and "not arcsec" in txt and '"' not in txt
    finally:
        await cl.disconnect()
        await srv.close()


async def test_600mm_profile_on_the_oag_is_flagged():
    # PHD2 profile left on the piggyback's 600 mm: 206.265 * 2.0 / 600
    srv, cl = await _connected({"get_pixel_scale": 0.69, "get_camera_binning": 1},
                               _cfg())
    try:
        await cl.refresh_pixel_scale()
        assert cl.metrics.scale_source == "phd2"
        await cl._handle_event(_step(1.0, 0.0))
        assert "focal length" in (cl.metrics.scale_warning or "")
    finally:
        await cl.disconnect()
        await srv.close()


async def test_rpc_reply_while_event_loop_owns_the_socket():
    srv, cl = await _connected({"get_pixel_scale": 0.13, "get_camera_binning": 1},
                               _cfg())
    task = asyncio.create_task(cl.run_event_loop())
    try:
        await asyncio.sleep(0.05)
        assert await cl.call("get_pixel_scale") == pytest.approx(0.13)
        # a PHD2 profile change re-learns the scale from inside the loop
        srv.replies["get_pixel_scale"] = 0.26
        await srv.push({"Event": "ConfigurationChange"})
        for _ in range(50):
            await asyncio.sleep(0.02)
            if cl.pixel_scale and abs(cl.pixel_scale - 0.26) < 1e-9:
                break
        assert cl.pixel_scale == pytest.approx(0.26)
        await srv.push({"Event": "StartGuiding"})
        await srv.push(_step(2.0, 0.0))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if cl.metrics.samples:
                break
        assert cl.metrics.rms_ra_arcsec == pytest.approx(0.52)
    finally:
        await cl.disconnect()
        task.cancel()
        await srv.close()


async def test_connect_failure_warns_once_not_every_retry(caplog):
    cl = PHD2Client("127.0.0.1", 1)   # nothing listens on port 1
    with caplog.at_level(logging.WARNING, logger=pc.__name__):
        for _ in range(4):
            assert not await cl.connect()
    assert sum("Cannot connect to PHD2" in r.message for r in caplog.records) == 1


# --- the agent's threshold + log -------------------------------------------------

def _agent(monkeypatch, rig="rc16", **cfg):
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = _cfg(quality_tracking_rms_max=1.0, **cfg)
    a.rig = rig
    a.state = agent_mod.TelescopeState()
    a._alerted = set()
    a._rms_logged_at = None
    sent = []

    async def fake_notify(config, msg, **kw):
        sent.append(msg)
    monkeypatch.setattr(agent_mod, "notify", fake_notify)
    return a, sent


def _metrics(tot_arcsec=None, px=10.0, n=60, state=GuidingState.GUIDING,
             scale=0.128):
    m = GuidingMetrics(state=state, samples=n, rms_total_px=px,
                       rms_ra_px=px / 1.414, rms_dec_px=px / 1.414)
    if scale:
        m.units, m.pixel_scale_arcsec, m.scale_source = "arcsec", scale, "phd2"
        m.rms_total_arcsec = px * scale if tot_arcsec is None else tot_arcsec
        m.rms_ra_arcsec = m.rms_dec_arcsec = m.rms_total_arcsec / 1.414
    else:
        m.units = "px"
        m.rms_total_arcsec = m.rms_ra_arcsec = m.rms_dec_arcsec = None
    return m


async def test_rms_alert_in_arcsec_with_pixels_shown(monkeypatch, caplog):
    a, sent = _agent(monkeypatch)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            await a._on_guiding_update(_metrics(px=98.0))   # 12.5"
    assert len(sent) == 1
    assert '12.54"' in sent[0] and "98.0 guide px" in sent[0] and "0.128" in sent[0]
    # one log line per 5 min, not one per guide step
    assert sum("exceeds threshold" in r.message for r in caplog.records) == 1


async def test_rms_below_threshold_in_arcsec_is_quiet(monkeypatch):
    a, sent = _agent(monkeypatch)
    await a._on_guiding_update(_metrics(px=5.0))            # 0.64" < 1.0"
    assert sent == []


async def test_pixels_never_compared_with_arcsec_threshold(monkeypatch):
    a, sent = _agent(monkeypatch)
    await a._on_guiding_update(_metrics(px=98.0, scale=None))
    assert sent == []


async def test_rms_not_judged_when_not_guiding_or_short_window(monkeypatch):
    a, sent = _agent(monkeypatch)
    await a._on_guiding_update(_metrics(px=98.0, state=GuidingState.STOPPED))
    await a._on_guiding_update(_metrics(px=98.0, n=5))
    assert sent == []


async def test_piggyback_agent_does_not_double_alert(monkeypatch):
    a, sent = _agent(monkeypatch, rig="piggyback")
    await a._on_guiding_update(_metrics(px=98.0))
    assert sent == [] and a.state.guiding.rms_total_px == 98.0
