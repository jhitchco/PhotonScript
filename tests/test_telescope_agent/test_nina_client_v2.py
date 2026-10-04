"""NinaClient against ninaAPI v2 (2.2.15.2 on the scope PC, probed 2026-09-26).

Before this, most reads hit v1-era paths (/equipment/camera, /sequence, ...)
that 404 on v2, so the agent's camera poll, cooling and dew watchdogs never
saw real data, and start/stop were POSTs to GET-only endpoints."""
import asyncio
import json

import httpx

from photonscript.telescope_agent.nina_client import NinaClient, _sequence_status

CAMERA = {"Connected": True, "Temperature": 42.1, "CoolerOn": False,
          "CoolerPower": 0, "TemperatureSetPoint": 10, "DewHeaterOn": False,
          "HasDewHeater": True}
MOUNT = {"Connected": True, "RightAscension": 22.67, "Declination": 89.63,
         "TrackingEnabled": False, "AtPark": False, "Slewing": False}
TREE_IDLE = [{"GlobalTriggers": []},
             {"Name": "Start_Container", "Status": "CREATED", "Items": []},
             {"Name": "Targets_Container", "Status": "CREATED", "Items": []},
             {"Name": "End_Container", "Status": "CREATED", "Items": []}]
TREE_RUNNING = [{"GlobalTriggers": []},
                {"Name": "Start_Container", "Status": "FINISHED", "Items": []},
                {"Name": "Targets_Container", "Status": "RUNNING", "Items": [
                    {"Name": "M31", "Status": "FINISHED", "Items": []},
                    {"Name": "NGC 6543", "Status": "RUNNING", "Items": [
                        {"Name": "Smart Exposure", "Status": "RUNNING"}]}]},
                {"Name": "End_Container", "Status": "CREATED", "Items": []}]


def _ok(payload):
    return httpx.Response(200, json={"Response": payload, "Error": "",
                                     "StatusCode": 200, "Success": True})


def _client(routes, seen):
    def handler(request):
        seen.append((request.method, request.url.path))
        key = request.url.path.replace("/v2/api", "")
        if key in routes:
            r = routes[key]
            return r(request) if callable(r) else r
        return httpx.Response(404)
    c = NinaClient("http://nina/v2/api")
    c._client = httpx.AsyncClient(base_url=c.base_url,
                                  transport=httpx.MockTransport(handler))
    return c


def test_reads_use_v2_info_endpoints_and_unwrap():
    seen = []
    c = _client({"/equipment/camera/info": _ok(CAMERA),
                 "/equipment/mount/info": _ok(MOUNT),
                 "/equipment/focuser/info": _ok({"Connected": True, "Position": 5853}),
                 "/sequence/json": _ok(TREE_IDLE)}, seen)

    async def go():
        return (await c.get_camera_info(), await c.get_mount_info(),
                await c.get_focuser_info(), await c.get_sequence_status())
    cam, mnt, foc, seq = asyncio.run(go())
    assert cam["Temperature"] == 42.1 and cam["CoolerOn"] is False
    assert mnt["Tracking"] is False and mnt["RightAscension"] == 22.67
    assert foc["Position"] == 5853
    assert seq == {"State": "IDLE", "CurrentTarget": None}
    assert ("GET", "/v2/api/equipment/camera/info") in seen


def test_sequence_status_running_and_current_target():
    assert _sequence_status(TREE_RUNNING) == {
        "State": "RUNNING", "CurrentTarget": {"Name": "NGC 6543"},
        "Running": "Smart Exposure"}   # PS-67: the running instruction
    assert _sequence_status([]) == {"State": "IDLE", "CurrentTarget": None}


def test_sequence_not_initialized_is_idle():
    seen = []
    c = _client({"/sequence/json": httpx.Response(409, json={
        "Response": "", "Error": "Sequencer not initialized", "Success": False})}, seen)
    assert asyncio.run(c.get_sequence_status())["State"] == "IDLE"


def test_start_stop_are_get_and_load_posts_json(tmp_path):
    seen = []
    body_seen = {}

    def load(req):
        body_seen.update(json.loads(req.content))
        return _ok("Sequence loaded")
    c = _client({"/sequence/start": _ok("Started"), "/sequence/stop": _ok("Stopped"),
                 "/sequence/load": load}, seen)
    seq = tmp_path / "s.json"
    seq.write_text(json.dumps({"$type": "NINA.Sequencer.Container.SequenceRootContainer"}))

    async def go():
        await c.load_sequence(str(seq))
        await c.start_sequence()
        await c.stop_sequence()
    asyncio.run(go())
    assert ("POST", "/v2/api/sequence/load") in seen
    assert ("GET", "/v2/api/sequence/start") in seen
    assert ("GET", "/v2/api/sequence/stop") in seen
    assert body_seen["$type"].endswith("SequenceRootContainer")


def test_cooling_alert_waits_for_grace(monkeypatch):
    """Normal cool-down must not page: off-setpoint has to persist."""
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared.models import TelescopeState
    from photonscript.telescope_agent import agent as agent_mod

    class Nina:
        def __init__(self):
            self.cam = dict(CAMERA, CoolerOn=True, CoolerPower=80, Temperature=15.0)

        async def get_camera_info(self):
            return self.cam

        async def get_mount_info(self):
            return dict(MOUNT, Tracking=False)

        async def get_focuser_info(self):
            return {"Position": 1}

        async def get_sequence_status(self):
            return {"State": "IDLE", "CurrentTarget": None}

        async def set_dew_heater(self, power):
            pass

    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = PhotonScriptConfig(_env_file=None, camera_setpoint_c=0.0)
    a.nina = Nina()
    a.state = TelescopeState()
    a._alerted = set()
    a._cool_bad_since = None
    a._cool_fix_attempts = 0
    a._dew_last_set = 0.0
    a._dew_api_broken = False
    a._armer_state = lambda: None   # hermetic: ignore any real armer_state.json
    sent = []

    async def fake_notify(config, msg, **kw):
        sent.append(msg)
    monkeypatch.setattr(agent_mod, "notify", fake_notify)
    clock = [1000.0]
    import time as _time
    monkeypatch.setattr(_time, "monotonic", lambda: clock[0])

    async def one_poll():
        a._running = True
        calls = {"n": 0}

        async def stop_after_one(_):
            calls["n"] += 1
            a._running = False
        monkeypatch.setattr(agent_mod.asyncio, "sleep", stop_after_one)
        await a._nina_poll_loop()

    asyncio.run(one_poll())                  # starts the off-setpoint clock
    clock[0] += 600
    asyncio.run(one_poll())                  # 10 min: still cooling, no page
    assert not any("Sensor at" in m for m in sent)
    clock[0] += 700
    asyncio.run(one_poll())                  # >20 min: now it pages
    assert any("Sensor at 15.0C" in m for m in sent)
    assert a.state.camera_temp_c == 15.0 and a.state.mount_tracking is False
