"""PS-189: the Piggy-600's Wanderer Cover V4-EC (dust cover + EL flat panel).

Pinned: with piggyback_cover_enabled off nothing changes; on, the companion
connects the Flat Panel, closes the cover for the darks, opens + gates it
before every OSC image pass, closes it while the roof is unsafe, shoots
panel flats at astro dawn instead of the PS-163 sky flats and ends closed
with the panel off. The lint rules (cover-open, cover-flats, cover-end), the
NINA-faithful PS-77 simulator, the cover gate, the ninaAPI helpers, the armer
close steps and the daytime capture on a closed cover.
"""

import asyncio
import copy
import json

import pytest

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import calibration_capture as cc
from photonscript.scheduler import flat_cover as fc
from photonscript.scheduler.sequence_lint import LintResult, _check_cover
from photonscript.scheduler.sideload import lint_companion
from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.rigs import PIGGYBACK, rig_config
from tests.test_scheduler.test_ps77_safety_stop import (TIMES, NinaSim, _short,
                                                        _vals, _walk)
from tests.test_scheduler.test_ps149_busy_loop import SCENARIOS

ND = TIMES["NauticalDawnProvider"]
AD = TIMES["DawnProvider"]


def _cfg(**kw):
    return rig_config(PhotonScriptConfig(_env_file=None, **kw), PIGGYBACK)


def _companion(cover=True, has_safety=True, with_lights=True, **kw):
    cfg = _cfg(piggyback_cover_enabled=cover, **kw)
    return json.loads(cal.generate_piggyback_companion_json(
        cfg, has_safety=has_safety, with_lights=with_lights))


def _named(seq, name):
    return next(d for d in _walk(seq) if d.get("Name") == name)


def _area(seq, kind):
    return next(d for d in _vals(seq, "Items") if kind in d["$type"])


def _types(node):
    return [_short(i["$type"]) for i in _vals(node, "Items")]


@pytest.fixture
def gate_script(tmp_path):
    p = tmp_path / "cover-gate.cmd"
    p.write_text("@echo off\n", encoding="ascii")
    return str(p)


# --- config + rigs ------------------------------------------------------------------

def test_defaults_keep_todays_behavior():
    c = PhotonScriptConfig(_env_file=None)
    assert c.piggyback_cover_enabled is False
    assert c.piggyback_panel_flat_mode == "auto"
    assert c.piggyback_flat_count == 25
    assert rigs.rig_connect_devices(c, PIGGYBACK) == ("camera", "focuser")
    on = PhotonScriptConfig(_env_file=None, piggyback_cover_enabled=True)
    assert rigs.rig_connect_devices(on, PIGGYBACK)[-1] == "flatdevice"
    assert "flatdevice" not in rigs.rig_connect_devices(on, "rc16")
    from photonscript.scheduler.app import _CONFIG_FIELDS
    keys = {f[0] for f in _CONFIG_FIELDS}
    for k in ("piggyback_cover_enabled", "piggyback_cover_gate_timeout_s",
              "piggyback_cover_script", "piggyback_panel_flat_mode",
              "piggyback_panel_brightness", "piggyback_panel_flat_exposure_s",
              "piggyback_panel_flat_max_s"):
        assert k in keys, k


@pytest.mark.parametrize("info,want", [
    ({"CoverState": "Open"}, "open"), ({"CoverState": "Closed"}, "closed"),
    ({"CoverState": "NeitherOpenNorClosed"}, "moving"), ({"CoverState": 3}, "open"),
    ({"CoverState": 2}, "closed"), ({"CoverState": 0}, "unknown"),
    ({"LocalizedCoverState": "Closed"}, "closed"), ({}, "unknown"), (None, None),
])
def test_cover_state_parsing(info, want):
    assert rigs.cover_state(info) == want


def test_flatdevice_endpoints(monkeypatch):
    seen = []

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"Success": True, "Response": {"CoverState": "Open"}}

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            seen.append((url, params))
            return _R()

    monkeypatch.setattr(rigs.httpx, "AsyncClient", _C)
    base = "http://x:1889/v2/api"
    for cmd in ("connect", "open", "close", "light_on", "light_off"):
        assert asyncio.run(rigs.nina_flatdevice(base, cmd))["ok"]
    assert asyncio.run(rigs.nina_flatdevice(base, "brightness", 80))["ok"]
    assert asyncio.run(rigs.nina_flatdevice_info(base)) == {"CoverState": "Open"}
    assert asyncio.run(rigs.nina_cover_close(base))["ok"]
    paths = [(u.replace(base, ""), p) for u, p in seen]
    assert ("/equipment/flatdevice/connect", {}) in paths
    assert ("/equipment/flatdevice/set-cover", {"closed": "false"}) in paths
    assert ("/equipment/flatdevice/set-cover", {"closed": "true"}) in paths
    assert ("/equipment/flatdevice/set-light", {"on": "true"}) in paths
    assert ("/equipment/flatdevice/set-brightness", {"brightness": 80}) in paths
    assert paths[-2:] == [("/equipment/flatdevice/set-light", {"on": "false"}),
                          ("/equipment/flatdevice/set-cover", {"closed": "true"})]
    assert not asyncio.run(rigs.nina_flatdevice(base, "explode"))["ok"]


# --- the companion -------------------------------------------------------------------

def test_cover_off_companion_has_no_cover_items():
    seq = _companion(cover=False)
    text = json.dumps(seq)
    assert "FlatDevice.OpenCover" not in text and "FlatDevice.CloseCover" not in text
    assert "Flat Panel" not in text and fc.PANEL_FLATS_NAME not in text
    assert "FlatDevice.SkyFlat" in text        # PS-163 dawn sky flats unchanged
    assert lint_companion(seq, cover=False).ok


def test_cover_on_companion_shape(gate_script):
    seq = _companion(piggyback_cover_script=gate_script)
    start = _area(seq, "StartAreaContainer")
    devs = [i.get("SelectedDevice") for i in _vals(start, "Items")
            if "ConnectEquipment" in i["$type"]]
    assert "Flat Panel" in devs
    t = _types(start)
    assert t.index("CloseCover") < t.index("SequentialContainer")   # before cooling
    darks = _named(seq, "OSC_DARKS_COVER_CLOSED")
    assert "LoopWhileUnsafe" not in json.dumps(darks["Conditions"])
    assert "LoopWhileUnsafe" not in json.dumps(_vals(darks, "Items"))
    # each OSC dark block ends at astro dusk (the light loop opens the cover)
    assert all("DuskProvider" in json.dumps(b["Conditions"])
               for b in _vals(darks, "Items") if "DARKS_" in b.get("Name", ""))
    loop = _named(seq, cal.OSC_LIGHTS_UNTIL_DAWN_NAME)
    assert _vals(loop, "Items")[0]["Name"] == fc.COVER_CLOSE_IF_UNSAFE_NAME
    ip = _named(seq, cal.OSC_IMAGE_PASS_NAME)
    first, gate = _vals(ip, "Items")[:2]
    assert _short(first["$type"]) == "OpenCover" and first["ErrorBehavior"] == 1
    assert "cover-gate" in gate["Script"] and gate["ErrorBehavior"] == 1
    assert 'piggyback --label="OSC 120s"' in gate["Script"]
    tgt = _area(seq, "TargetAreaContainer")
    names = [i.get("Name") for i in _vals(tgt, "Items")]
    assert names[-1] == fc.PANEL_FLATS_NAME
    assert "SkyFlat" not in json.dumps(seq)
    assert not any("WAIT_SAFE_FOR_OSC_FLATS" in str(n) for n in names)
    pf = _named(seq, fc.PANEL_FLATS_NAME)
    assert "SafetyMonitorCondition" not in json.dumps(pf["Conditions"])
    af = next(d for d in _walk(pf) if "AutoExposureFlat" in d.get("$type", ""))
    assert _types(af) == ["CloseCover", "ToggleLight", "SwitchFilter",
                          "SetBrightness", "SequentialContainer", "ToggleLight",
                          "OpenCover"]
    assert af["KeepPanelClosed"] is True and af["Brightness"] == 50
    assert _vals(af, "Items")[1]["OnOff"] is True and _vals(af, "Items")[5]["OnOff"] is False
    assert _vals(af, "Items")[2]["Filter"] is None
    loop25 = _vals(af, "Items")[4]
    assert _vals(loop25, "Conditions")[0]["Iterations"] == 25
    end = _area(seq, "EndAreaContainer")
    assert _types(end)[:2] == ["ToggleLight", "CloseCover"]
    assert _vals(end, "Items")[0]["OnOff"] is False
    res = lint_companion(seq, cover=True)
    assert res.ok and not [f for f in res.findings if f.rule.startswith("cover")], \
        [(f.rule, f.detail) for f in res.findings]


def test_fixed_panel_flats_and_no_lights_variants(gate_script):
    seq = _companion(piggyback_cover_script=gate_script, piggyback_panel_flat_mode="fixed",
                     piggyback_panel_flat_exposure_s=2.5, piggyback_panel_brightness=120)
    pf = _named(seq, fc.PANEL_FLATS_NAME)
    t = _types(pf)
    assert t[1:-1] == ["CloseCover", "ToggleLight", "SetBrightness",
                       "SequentialContainer", "ToggleLight"]
    shots = [d for d in _walk(pf) if d.get("ImageType") == "FLAT"]
    assert shots[0]["ExposureTime"] == 2.5
    assert next(d for d in _walk(pf) if "SetBrightness" in d.get("$type", ""))["Brightness"] == 120
    assert lint_companion(seq, cover=True).ok
    for hs in (True, False):   # no lights: darks capped at astro dawn, still clean
        s2 = _companion(has_safety=hs, with_lights=False)
        darks = _named(s2, "OSC_DARKS_COVER_CLOSED")
        assert "DawnProvider" in json.dumps(darks)
        r = lint_companion(s2, cover=True)
        assert r.ok, [(f.rule, f.detail) for f in r.findings]


def test_gate_missing_is_annotated_and_warned(tmp_path):
    seq = _companion(piggyback_cover_script=str(tmp_path / "nope.cmd"))
    assert "cover gate OFF tonight" in json.dumps(seq)
    ip = _named(seq, cal.OSC_IMAGE_PASS_NAME)
    assert _short(_vals(ip, "Items")[0]["$type"]) == "OpenCover"
    res = lint_companion(seq, cover=True)
    assert res.ok
    assert any(f.rule == "cover-open" and f.level == "WARN" for f in res.findings)


# --- lint -----------------------------------------------------------------------------

def _strip(seq, pred):
    out = copy.deepcopy(seq)
    for d in _walk(out):
        items = d.get("Items")
        if isinstance(items, dict):
            items["$values"] = [i for i in items["$values"] if not pred(i)]
    return out


def _errors(seq, **kw):
    r = LintResult()
    _check_cover(seq, r, **kw)
    return {f.rule for f in r.findings if f.level == "ERROR"}


def test_lint_cover_rules(gate_script):
    seq = _companion(piggyback_cover_script=gate_script)
    assert _errors(seq) == set()
    no_open = _strip(seq, lambda i: "OpenCover" in i.get("$type", "")
                     and i.get("ErrorBehavior") == 1)
    assert "cover-open" in _errors(no_open)
    end = _area(seq, "EndAreaContainer")
    no_end = copy.deepcopy(seq)
    _area(no_end, "EndAreaContainer")["Items"]["$values"] = [
        i for i in _vals(end, "Items") if "FlatDevice" not in i["$type"]]
    assert "cover-end" in _errors(no_end)
    # panel flats without the close: the cover is still open from the lights
    open_flats = _strip(seq, lambda i: "CloseCover" in i.get("$type", "")
                        and "AutoExposureFlat" not in i.get("$type", ""))
    pf = _named(open_flats, fc.PANEL_FLATS_NAME)
    af = next(d for d in _walk(pf) if "AutoExposureFlat" in d.get("$type", ""))
    af["Items"]["$values"] = [i for i in af["Items"]["$values"]
                              if "CloseCover" not in i["$type"]]
    assert "cover-flats" in _errors(open_flats)
    # a light with the panel lit
    lit = copy.deepcopy(seq)
    ip = _named(lit, cal.OSC_IMAGE_PASS_NAME)
    ip["Items"]["$values"].insert(1, fc.toggle_light(True))
    assert "cover-open" in _errors(lit)
    # the cover enabled, an old companion without any cover item
    old = _companion(cover=False)
    assert {"cover-open", "cover-end"} <= _errors(old, expected=True)
    assert _errors(old, expected=False) == set()
    # sky flats with the cover closed
    sky = _companion(cover=False)
    start = _area(sky, "StartAreaContainer")
    start["Items"]["$values"].append(fc.close_cover())
    _area(sky, "EndAreaContainer")["Items"]["$values"] += fc.end_items()
    ip = _named(sky, cal.OSC_IMAGE_PASS_NAME)
    ip["Items"]["$values"].insert(0, fc.open_cover())
    assert "cover-flats" not in _errors(sky)       # opened by the lights
    ip["Items"]["$values"].pop(0)
    errs = _errors(sky)
    assert "cover-flats" in errs and "cover-open" in errs


# --- the simulator -----------------------------------------------------------------------

def _sim_cover(seq, unsafe):
    sim = NinaSim(seq, unsafe=unsafe, tick=0.002, max_steps=3_000_000)
    st = {"cover": "closed", "light": False}
    bad, closes, flats, darks = [], [], [], []
    orig = sim.run_instruction

    def spy(n):
        ty = n.type
        if ty == "OpenCover":
            if not (n.parent is not None and n.parent.d.get("KeepPanelClosed")):
                st["cover"] = "open"
        elif ty == "CloseCover":
            st["cover"] = "closed"
            closes.append(sim.t)
        elif ty == "ToggleLight":
            st["light"] = bool(n.d.get("OnOff"))
        elif ty == "TakeExposure":
            kind = n.d.get("ImageType")
            if kind == "LIGHT" and (st["cover"] != "open" or st["light"]):
                bad.append(("light", sim.t, dict(st)))
            if kind == "FLAT":
                flats.append(sim.t)
                if st["cover"] != "closed" or not st["light"]:
                    bad.append(("flat", sim.t, dict(st)))
            if kind in ("DARK", "BIAS"):
                darks.append(sim.t)
                if st["cover"] != "closed" or st["light"]:
                    bad.append((kind, sim.t, dict(st)))
        return orig(n)

    sim.run_instruction = spy
    sim.run()
    return sim, st, bad, closes, flats, darks


@pytest.mark.parametrize("weather", sorted(SCENARIOS))
@pytest.mark.parametrize("mode", ["auto", "fixed"])
def test_sim_cover_never_shoots_through_a_closed_cover(gate_script, weather, mode):
    seq = _companion(piggyback_cover_script=gate_script, piggyback_panel_flat_mode=mode)
    sim, st, bad, closes, flats, darks = _sim_cover(seq, SCENARIOS[weather])
    assert bad == [], bad[:5]
    assert sim.spins() == {}, sim.spins()
    assert st == {"cover": "closed", "light": False}     # closed by day
    assert len(flats) == 25 and min(flats) >= AD          # panel flats at astro dawn
    assert darks                                          # cover-closed darks ran
    if weather == "clear":
        assert sim.lights and all(x["start"] < AD for x in sim.lights)
    # the cover closes during every unsafe spell inside the light window
    for a, b in SCENARIOS[weather]:
        lo, hi = max(a, 0), min(b, AD)
        if hi - lo > 1200:
            assert any(lo <= t <= hi for t in closes), (a, b)


def test_sim_old_shape_without_cover_items_is_caught_by_the_simulator():
    """The pre-PS-189 companion on a rig whose cover is closed by day: every
    light is shot through the closed cover (what the cover-open rule stops)."""
    sim, st, bad, *_ = _sim_cover(_companion(cover=False), [])
    assert [b for b in bad if b[0] == "light"]


# --- the gate ------------------------------------------------------------------------------

class _Dev:
    def __init__(self, states, light=False):
        self.states = list(states)
        self.light = light
        self.cmds = []

    async def read(self, cfg, rig):
        s = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if s is None:
            return None
        return {"Connected": True, "CoverState": s, "LightOn": self.light}

    async def command(self, cfg, rig, name):
        self.cmds.append(name)
        if name == "light_off":
            self.light = False
        return {"ok": True}


def _gate(dev, cfg=None, notes=None, clock=None, **kw):
    cfg = cfg or _cfg(piggyback_cover_enabled=True, piggyback_enabled=True,
                      piggyback_cover_gate_timeout_s=30, piggyback_cover_gate_poll_s=5)
    notes = notes if notes is not None else []
    t = {"now": 1_000_000.0}

    async def sleep(s):
        t["now"] += s

    async def notify(cfg, msg, **k):
        notes.append(msg)
    return asyncio.run(fc.run_gate(
        cfg, "piggyback", "OSC 120s", read=dev.read, command=dev.command,
        sleep=sleep, clock=clock or (lambda: t["now"]), notify_fn=notify, **kw))


@pytest.fixture(autouse=True)
def _fresh_gate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(fc, "LIVE", {})
    monkeypatch.setattr(fc, "_PAGED", {})


def test_gate_off_when_disabled():
    dev = _Dev(["Closed"])
    res = _gate(dev, cfg=_cfg(piggyback_cover_enabled=False))
    assert res["verdict"] == "OFF" and dev.cmds == []


def test_gate_passes_open_and_opens_a_closed_cover(tmp_path):
    assert _gate(_Dev(["Open"]))["verdict"] == "PASS"
    dev = _Dev(["Closed", "NeitherOpenNorClosed", "Open"])
    res = _gate(dev, cfg=_cfg(piggyback_cover_enabled=True, data_dir=tmp_path))
    assert res["verdict"] == "PASS" and dev.cmds == ["open"]
    assert fc.LIVE["piggyback"]["state"] == "open"


def test_gate_turns_the_panel_off_first():
    dev = _Dev(["Open"], light=True)
    assert _gate(dev)["verdict"] == "PASS" and dev.cmds == ["light_off"]


def test_gate_skips_after_timeout_and_pages_once_a_night(tmp_path):
    notes = []
    cfg = _cfg(piggyback_cover_enabled=True, data_dir=tmp_path,
               piggyback_cover_gate_timeout_s=30)
    dev = _Dev(["Closed"])
    r1 = _gate(dev, cfg=cfg, notes=notes)
    r2 = _gate(_Dev(["Closed"]), cfg=cfg, notes=notes)
    assert r1["verdict"] == r2["verdict"] == "SKIP"
    assert dev.cmds == ["open"]
    assert len(notes) == 1 and "not imaging" in notes[0]
    assert fc.row_note("piggyback", now=fc.LIVE["piggyback"]["updated"]).startswith(
        "not imaging")
    assert len(fc.recent(cfg)) == 2
    # opening later sends one recovery line
    _gate(_Dev(["Open"]), cfg=cfg, notes=notes)
    assert len(notes) == 2 and "open again" in notes[1]


def test_gate_unreadable_cover_never_passes():
    dev = _Dev([None])
    res = _gate(dev)
    assert res["verdict"] == "SKIP" and res["cover"] == "unreadable"
    assert dev.cmds == ["connect"]


def test_gate_aborts_when_nina_cancels():
    async def gone():
        return True
    res = _gate(_Dev(["Closed"]), disconnected=gone)
    assert res["verdict"] == "ABORTED"


def test_cli_fallback_reads_nina_directly(monkeypatch):
    from photonscript import cli
    cfg = PhotonScriptConfig(_env_file=None, piggyback_enabled=True,
                             piggyback_cover_enabled=True)

    def info(state, light=False):
        async def f(base):
            return None if state is None else {"CoverState": state, "LightOn": light}
        return f
    monkeypatch.setattr(rigs, "nina_flatdevice_info", info("Open"))
    assert cli._cover_fallback(cfg, "piggyback")["verdict"] == "PASS"
    monkeypatch.setattr(rigs, "nina_flatdevice_info", info("Closed"))
    assert cli._cover_fallback(cfg, "piggyback")["verdict"] == "SKIP"
    monkeypatch.setattr(rigs, "nina_flatdevice_info", info(None))
    assert cli._cover_fallback(cfg, "piggyback")["verdict"] == "SKIP"
    off = PhotonScriptConfig(_env_file=None)
    assert cli._cover_fallback(off, "piggyback")["verdict"] == "OFF"


def test_cover_gate_cmd_is_ascii_and_skips_only_on_exit_3():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "deploy" / "cover-gate.cmd").read_bytes()
    assert text.isascii()
    s = text.decode()
    assert "cover-gate %* --from-nina" in s and "EQU 3 exit /b 1" in s
    assert "C:\\astro\\venv\\Scripts\\photonscript.exe" in s


# --- armer --------------------------------------------------------------------------------

def test_armer_closes_the_cover_only_when_enabled(tmp_path, monkeypatch):
    from photonscript.scheduler.armer import Armer
    calls = []

    async def close(base):
        calls.append(base)
        return {"ok": True, "detail": "ok"}
    monkeypatch.setattr(rigs, "nina_cover_close", close)

    def armer(**kw):
        a = Armer.__new__(Armer)
        a.config = PhotonScriptConfig(_env_file=None, data_dir=tmp_path, **kw)
        return a
    assert asyncio.run(armer(piggyback_enabled=True)._close_piggy_cover()) is None
    assert asyncio.run(armer(piggyback_cover_enabled=True)._close_piggy_cover()) is None
    step = asyncio.run(armer(piggyback_enabled=True,
                             piggyback_cover_enabled=True)._close_piggy_cover())
    assert step == "piggyback cover close ok" and calls == ["http://localhost:1889/v2/api"]


def test_dawn_shutdown_and_unsafe_stop_call_the_cover_close():
    import inspect
    from photonscript.scheduler.armer import Armer
    for fn in (Armer.dawn_shutdown, Armer._maybe_stop_stuck_imaging, Armer._paused_tick):
        assert "_close_piggy_cover" in inspect.getsource(fn), fn.__name__


# --- daytime darks on a closed cover ---------------------------------------------------

def test_generate_darks_json_cover_closed():
    cfg = _cfg(piggyback_cover_enabled=True)
    seq = json.loads(cal.generate_darks_json(cfg, [(120.0, 2)], 0, cover_closed=True)[0])
    text = json.dumps(seq)
    assert "LoopWhileUnsafe" not in text and "Flat Panel" in text
    start = _area(seq, "StartAreaContainer")
    assert "CloseCover" in json.dumps(start) and "OpenCover" not in text
    assert "CloseCover" in json.dumps(_area(seq, "EndAreaContainer"))
    plain = json.loads(cal.generate_darks_json(cfg, [(120.0, 2)], 0)[0])
    assert "FlatDevice" not in json.dumps(plain)


class _CoverIO:
    def __init__(self, cover="closed", light=False, roof=False):
        self.cover_st, self.light, self.roof_closed = cover, light, roof
        self.closes = 0

    async def cover(self, close=False):
        if close and self.cover_st != "closed":
            self.closes += 1
            self.cover_st, self.light = "closed", False
        return self.cover_st, self.light

    async def roof(self, connect=True):
        return self.roof_closed, "fake monitor"


@pytest.mark.parametrize("enabled,cover,connect,want", [
    (True, "closed", True, (True, "cover")),
    (True, "open", True, (True, "cover")),       # a job start closes it first
    (True, "open", False, (False, "roof")),      # a running job only reads
    (False, "closed", True, (False, "roof")),    # cover disabled: roof as before
])
def test_light_tight(enabled, cover, connect, want):
    cfg = PhotonScriptConfig(_env_file=None, piggyback_enabled=True,
                             piggyback_cover_enabled=enabled)
    io = _CoverIO(cover=cover, roof=False)
    closed, _src, via = asyncio.run(cc.light_tight(cfg, "piggyback", io, connect=connect))
    assert (closed, via) == want
    rc = asyncio.run(cc.light_tight(cfg, "rc16", io))
    assert rc[2] == "roof"


def test_daytime_capture_runs_on_a_closed_cover_with_the_roof_open(tmp_path, monkeypatch):
    from tests.test_scheduler.test_ps113_calibration import (FakeIO, _pb_cfg,
                                                             _start_and_wait, _blocks)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc, "_jobs", {})

    async def _quiet(*a, **k):
        return True
    import photonscript.shared.pushover as po
    monkeypatch.setattr(po, "notify", _quiet)

    class CoverIO(FakeIO):
        cover_st = "open"

        async def cover(self, close=False):
            if close and self.cover_st != "closed":
                self.calls.append("close_cover")
                self.cover_st = "closed"
            return self.cover_st, False

    cfg = _pb_cfg(tmp_path, piggyback_cover_enabled=True)
    io = CoverIO(cfg, roof=False)                 # roof OPEN (daytime)
    ok, body, job = asyncio.run(_start_and_wait(cfg, io, state="COMPLETE",
                                                exposures=[300.0], count=2, bias=0))
    assert ok, body
    assert job.state == "complete", job.events
    assert job.light_via == "cover" and "close_cover" in io.calls
    text = json.dumps(io.seq)
    assert "LoopWhileUnsafe" not in text and "FlatDevice.CloseCover" in text
    assert _blocks(io.seq) == [("DARK", 300.0, 2)]
    # without the cover the open roof still refuses, and the refusal says why
    cfg2 = _pb_cfg(tmp_path / "b", piggyback_cover_enabled=False)
    ok2, body2, _ = asyncio.run(_start_and_wait(cfg2, FakeIO(cfg2, roof=False),
                                                exposures=[300.0], count=2))
    assert not ok2 and any("roof reads OPEN" in r for r in body2["refusals"])


def test_cover_job_stops_when_the_cover_opens(tmp_path, monkeypatch):
    from tests.test_scheduler.test_ps113_calibration import (FakeIO, _pb_cfg,
                                                             _start_and_wait)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc, "_jobs", {})

    async def _quiet(*a, **k):
        return True
    import photonscript.shared.pushover as po
    monkeypatch.setattr(po, "notify", _quiet)

    class CoverIO(FakeIO):
        cover_st = "closed"

        async def cover(self, close=False):
            return self.cover_st, False

    cfg = _pb_cfg(tmp_path, piggyback_cover_enabled=True)
    io = CoverIO(cfg, roof=False)

    def opener(fio):
        if fio.seq is not None and len(fio.to_land) < 4:
            fio.cover_st = "open"
    io.on_camera = opener
    ok, _b, job = asyncio.run(_start_and_wait(cfg, io, exposures=[300.0], count=6))
    assert ok and job.state == "aborted" and "roof opened" in job.detail


# --- PS-188 dark capture window (merge of wave-2026-10-17) --------------------------

@pytest.mark.parametrize("window", ["manual", "unsafe-night", "dawn-roof-closed"])
def test_cover_darks_run_whatever_the_ps188_window(window):
    """With the cover the darks are light-tight, so the PS-188 window
    (manual = no companion darks) does not apply; with the cover off it does."""
    seq = _companion(piggyback_dark_capture_window=window)
    text = json.dumps(seq)
    assert "OSC_DARKS_COVER_CLOSED" in text
    assert "OSC darks OFF tonight" not in text
    assert lint_companion(seq, cover=True).ok
    off = json.dumps(_companion(cover=False, piggyback_dark_capture_window=window))
    if window == "manual":
        assert "OSC darks OFF tonight" in off and "OSC_DARKS_" not in off
    else:
        assert "OSC_DARKS_IF_UNSAFE" in off


def test_piggy_dark_action_names_the_cover():
    from photonscript.scheduler import calibration_filing as cf
    on = PhotonScriptConfig(_env_file=None, piggyback_cover_enabled=True,
                            piggyback_dark_capture_window="manual")
    assert "cover" in cf.piggy_dark_action(on) and "COVER_CLOSED" in cf.piggy_dark_action(on)
    off = PhotonScriptConfig(_env_file=None, piggyback_dark_capture_window="manual")
    assert "manual" in cf.piggy_dark_action(off)
