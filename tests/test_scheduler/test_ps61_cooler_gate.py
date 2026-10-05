"""PS-61: no lights until the sensor is within 1 C of its setpoint.

Covers the gate logic (bounded wait, fail-open, one alert per episode), the
RC16 sequence (every light block is its own container starting with the
cooler-gate ExternalScript, ErrorBehavior 1), the Piggy-600 companion (the
OSC image pass starts with it), the lint rule, the PS-77 sequence simulator
(a dead cooler skips lights without wedging the night), the CLI / cmd exit
codes and the dashboard camera-row note."""
import asyncio
import json
from pathlib import Path

import pytest

from photonscript.scheduler import cooler_gate as cg
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import _exec_items, lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

from tests.test_scheduler.test_ps77_safety_stop import (
    LOOP_END, NinaSim, _Interrupt, _as_deployed, _short, _vals, _walk)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def gate_on(tmp_path, monkeypatch):
    script = tmp_path / "cooler-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    monkeypatch.setenv("PS_COOLER_GATE_SCRIPT", str(script))
    return script


@pytest.fixture(autouse=True)
def _fresh_live():
    cg.LIVE.clear()
    cg._EPISODE.clear()
    yield
    cg.LIVE.clear()
    cg._EPISODE.clear()


def _heart(oiii=40, ha=20, exp=300, hdr=False):
    plans = [ExposurePlan(filter_type=FilterType.OIII, exposure_seconds=exp,
                          count=oiii, gain=200, offset=256),
             ExposurePlan(filter_type=FilterType.HA, exposure_seconds=exp,
                          count=ha, gain=200, offset=256)]
    if hdr:
        plans[1] = plans[1].model_copy(update={"hdr_short_seconds": 30,
                                               "hdr_short_count": 10})
    t = NinaSequenceTarget(name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                           exposures=plans, camera_temp_c=0.0)
    t.start_guiding = True
    return t


def _gen(targets=None, **kw):
    seq = build_sequence_for_night("PS61", targets or [_heart(**kw)])
    return json.loads(nsj.generate_nina_json(seq))


def _gates(seq):
    return [it for it in _exec_items(seq) if "ExternalScript" in it.get("$type", "")
            and "cooler-gate" in it.get("Script", "")]


def _blocks(seq):
    return [d for d in _walk(seq)
            if str(d.get("Name", "")).endswith(nsj.TARGET_BLOCK_SUFFIX)]


def _companion(**cfg_kw):
    from photonscript.scheduler.calibration import generate_piggyback_companion_json
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    cfg = PhotonScriptConfig(piggyback_setpoint_c=-5.0, **cfg_kw)
    return json.loads(generate_piggyback_companion_json(
        rig_config(cfg, PIGGYBACK), has_safety=True, with_lights=True))


# --- the RC16 sequence --------------------------------------------------------

def test_every_rc16_light_block_starts_with_the_gate(gate_on):
    seq = _gen()
    blocks = _blocks(seq)
    assert len(blocks) == 2                      # OIII, Ha
    for b, filt in zip(blocks, ("OIII", "Ha")):
        items = _vals(b, "Items")
        gate = items[0]
        assert "ExternalScript" in gate["$type"]
        assert gate["Script"].startswith(f'"{gate_on}" rc16 --setpoint=0 ')
        assert f'--label="Heart Nebula {filt}"' in gate["Script"]
        assert gate["ErrorBehavior"] == 1 and gate["Attempts"] == 1
        kinds = [_short(i["$type"]) for i in items]
        assert kinds.index("RunAutofocus") > 0            # gate before the AF
        assert kinds[-1] == "SmartExposure"
        assert gate["Parent"] == {"$ref": b["$id"]}       # SKIP interrupts the block
    r = lint(seq, guided=True)
    assert r.ok, [f"{f.rule}: {f.detail}" for f in r.findings]
    assert not [f for f in r.findings if f.rule in ("cooler-gate", "parent-links")]


def test_gate_uses_the_temperature_the_start_area_cooled_to(gate_on):
    t = _heart()
    t.camera_temp_c = -10.0
    seq = _gen([t])
    cool = [d for d in _walk(seq) if _short(d.get("$type", "")) == "CoolCamera"]
    assert cool[0]["Temperature"] == -10.0
    assert all("--setpoint=-10 " in g["Script"] for g in _gates(seq))


def test_hdr_and_moonrise_blocks_are_gated(gate_on, monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: {
        "available": True, "down_at_dusk": True, "illum_pct": 60,
        "rise_local_hh": 1, "rise_local_mm": 30})
    t = _heart(hdr=True)
    t.exposures.append(ExposurePlan(filter_type=FilterType.RED,
                                    exposure_seconds=180, count=10,
                                    gain=200, offset=256))
    seq = _gen([t])
    assert len(_blocks(seq)) == 3
    moon = [d for d in _walk(seq) if str(d.get("Name", "")).endswith(
        nsj.FILTER_UNTIL_MOONRISE_SUFFIX)]
    assert moon and _vals(moon[0], "Items")[0]["Name"].endswith(nsj.TARGET_BLOCK_SUFFIX)
    ha = [b for b in _blocks(seq) if "Heart Nebula Ha" in _vals(b, "Items")[0]["Script"]][0]
    assert [_short(i["$type"]) for i in _vals(ha, "Items")].count("SmartExposure") == 2
    assert lint(seq, guided=True).ok


def test_tracking_test_ladder_is_gated(gate_on):
    seq = json.loads(nsj.generate_tracking_test_json())
    ladder = [d for d in _walk(seq) if str(d.get("Name", "")).endswith(
        nsj.TARGET_TRACKING_LADDER_SUFFIX)][0]
    first = _vals(ladder, "Items")[0]
    assert "cooler-gate" in first["Script"] and first["ErrorBehavior"] == 1
    assert not [f for f in lint(seq).findings if f.rule == "cooler-gate"]


def test_off_means_the_old_flat_shape(monkeypatch, gate_on):
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "off")
    seq = _gen()
    assert not _gates(seq) and not _blocks(seq)
    assert not [d for d in _walk(seq) if "cooler gate" in str(d.get("Text", ""))]
    assert lint(seq, guided=True).ok


def test_warn_mode_gate_never_skips(monkeypatch, gate_on):
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "warn")
    seq = _gen()
    assert _gates(seq) and all(g["ErrorBehavior"] == 0 for g in _gates(seq))
    assert not [f for f in lint(seq, guided=True).findings if f.rule == "cooler-gate"]


def test_missing_script_emits_no_gate_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    monkeypatch.setenv("PS_COOLER_GATE_SCRIPT", str(tmp_path / "gone.cmd"))
    seq = _gen()
    assert not _gates(seq) and not _blocks(seq)
    notes = [d for d in _walk(seq) if "cooler gate OFF tonight" in
             str(d.get("Text", "")) + str(d.get("Message", ""))]
    assert len(notes) == 2                               # annotation + Pushover
    r = lint(seq, guided=True)
    assert r.ok and any(f.rule == "cooler-gate" and f.level == "WARN"
                        for f in r.findings)


# --- the Piggy-600 companion ----------------------------------------------------

def test_companion_image_pass_starts_with_the_gate(gate_on):
    from photonscript.scheduler.calibration import OSC_IMAGE_PASS_NAME
    seq = _companion()
    ip = [d for d in _walk(seq) if d.get("Name") == OSC_IMAGE_PASS_NAME][0]
    gate = _vals(ip, "Items")[0]
    assert "cooler-gate" in gate["Script"]
    assert gate["Script"].split('" ', 1)[1].startswith("piggyback --setpoint=-5 ")
    assert gate["ErrorBehavior"] == 1 and gate["Parent"] == {"$ref": ip["$id"]}
    r = lint(seq)
    assert not [f for f in r.findings if f.rule in ("cooler-gate", "parent-links")]
    assert len(_gates(seq)) == 1


def test_companion_without_script_warns_in_its_start_area(monkeypatch, tmp_path):
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    monkeypatch.setenv("PS_COOLER_GATE_SCRIPT", str(tmp_path / "gone.cmd"))
    seq = _companion()
    assert not _gates(seq)
    start = [d for d in _walk(seq) if "StartAreaContainer" in d.get("$type", "")][0]
    assert any("cooler gate OFF tonight" in str(i.get("Message", ""))
               for i in _vals(start, "Items"))


# --- the lint rule ------------------------------------------------------------------

def _drop_first_gate(seq):
    for b in _blocks(seq):
        vals = b["Items"]["$values"]
        if vals and "cooler-gate" in vals[0].get("Script", ""):
            vals.pop(0)
            return seq
    raise AssertionError("no gate")


def test_lint_flags_an_ungated_light_block(gate_on):
    r = lint(_drop_first_gate(_gen()), guided=True)
    errs = [f for f in r.findings if f.rule == "cooler-gate" and f.level == "ERROR"]
    assert errs and "1 light loop" in errs[0].detail and not r.ok


def test_lint_flags_a_gate_that_would_not_skip(gate_on):
    seq = _gen()
    _gates(seq)[0]["ErrorBehavior"] = 0
    r = lint(seq, guided=True)
    assert any(f.rule == "cooler-gate" and "ErrorBehavior 1" in f.detail
               for f in r.findings)


def test_lint_flags_ungated_companion_lights(gate_on):
    seq = _companion()
    for d in _walk(seq):
        vals = (d.get("Items") or {}).get("$values") if isinstance(d.get("Items"), dict) else None
        if vals:
            d["Items"]["$values"] = [v for v in vals if "cooler-gate" not in
                                     str(v.get("Script", ""))]
    assert any(f.rule == "cooler-gate" and f.level == "ERROR" for f in lint(seq).findings)


def test_lint_rule_can_be_forced_without_config(monkeypatch):
    seq = _gen()                                         # conftest: mode off, no gate
    assert not [f for f in lint(seq, guided=True).findings if f.rule == "cooler-gate"]
    r = lint(seq, guided=True, cooler_gate=True)
    assert any(f.rule == "cooler-gate" and f.level == "ERROR" for f in r.findings)


# --- the PS-77 simulator: what NINA does with the gate ----------------------------

class GateSim(NinaSim):
    """NinaSim plus the gate: ExternalScript(cooler-gate) holds until the
    sensor reaches the setpoint (`cold_at`, seconds from astro dusk) or the
    timeout; on timeout ErrorBehavior 1 interrupts the gate's Parent."""

    def __init__(self, seq, cold_at, timeout_s=1200, **kw):
        super().__init__(seq, **kw)
        self.cold_at, self.timeout_s = cold_at, timeout_s
        self.skips = 0

    def run_instruction(self, n):
        if n.type == "ExternalScript" and "cooler-gate" in n.d.get("Script", ""):
            if self.t >= self.cold_at:
                return
            wait = min(self.cold_at - self.t, self.timeout_s)
            self.advance(wait)
            if self.t >= self.cold_at:
                return
            self.skips += 1
            if n.d.get("ErrorBehavior") == 1 and n.parent is not None:
                raise _Interrupt(n.parent)
            return
        return super().run_instruction(n)


def test_sim_cold_sensor_images_exactly_as_before(gate_on, monkeypatch):
    seq = _gen(oiii=200, ha=200)
    a = GateSim(seq, cold_at=-10 ** 6).run()
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "off")
    b = NinaSim(_gen(oiii=200, ha=200)).run()
    assert a.skips == 0 and a.lights
    assert [(x["start"], x["end"]) for x in a.lights] == \
        [(x["start"], x["end"]) for x in b.lights]


def test_sim_dead_cooler_takes_no_lights_and_never_wedges(gate_on):
    sim = GateSim(_gen(oiii=200, ha=200), cold_at=10 ** 9).run()
    assert sim.lights == []
    assert sim.skips >= 10                     # each block retried every pass
    assert sim.t >= LOOP_END                   # held to the loop end, no wedge


def test_sim_cooler_recovers_mid_night_and_imaging_resumes(gate_on):
    cold = 3 * 3600
    sim = GateSim(_gen(oiii=200, ha=200), cold_at=cold).run()
    assert sim.skips >= 1
    assert sim.lights and min(x["start"] for x in sim.lights) >= cold
    assert max(x["end"] for x in sim.lights) <= LOOP_END


def test_sim_skip_needs_the_parent_link(gate_on):
    """SkipInstructionSetOnError interrupts the gate's Parent, which NINA
    sets only from the JSON $ref (PS-77). Without links the SKIP would be a
    no-op and the block would shoot warm; link_parents keeps it working."""
    sim = GateSim(_as_deployed(_gen(oiii=200, ha=200)), cold_at=10 ** 9).run()
    assert sim.lights                           # proves the links matter


# --- the gate itself ----------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def _run(cfg, temps, rig="rc16", **kw):
    clock = _Clock()
    seq = list(temps)
    notes = []

    async def read(_cfg, _rig):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def notify(_cfg, msg, title="", priority=0):
        notes.append((title, msg, priority))

    res = asyncio.run(cg.run_gate(cfg, rig, kw.pop("setpoint", 0.0),
                                  kw.pop("label", "M 31 Ha"), read=read,
                                  sleep=clock.sleep, clock=clock,
                                  notify_fn=notify, **kw))
    return res, notes


def _cfg(tmp_path, **kw):
    base = dict(data_dir=tmp_path, cooler_gate_mode="skip",
                cooler_gate_timeout_min=20, cooler_gate_poll_s=15)
    base.update(kw)
    return PhotonScriptConfig(_env_file=None, **base)


def test_gate_passes_at_once_when_cold(tmp_path):
    res, notes = _run(_cfg(tmp_path), [0.6])
    assert res["verdict"] == "PASS" and res["waited_s"] == 0 and not notes
    assert cg.recent(_cfg(tmp_path)) == []          # nothing logged for a no-wait pass


def test_gate_waits_then_passes(tmp_path):
    res, notes = _run(_cfg(tmp_path), [8.0, 4.0, 1.4, 0.9])
    assert res["verdict"] == "PASS" and res["waited_s"] == 45 and not notes
    assert cg.recent(_cfg(tmp_path))[-1]["verdict"] == "PASS"


def test_gate_times_out_skips_and_alerts_once_per_episode(tmp_path):
    cfg = _cfg(tmp_path)
    res, notes = _run(cfg, [12.4])
    assert res["verdict"] == "SKIP" and res["waited_s"] == 1200
    assert notes == [("PhotonScript cooler gate",
                      "RC16: not imaging M 31 Ha, sensor at 12.4 C vs setpoint "
                      "0 C after 20 min; check the cooler. The block is skipped "
                      "and retried on the next pass.", 1)]
    assert cg.LIVE["rc16"]["state"] == "skipped"
    res2, notes2 = _run(cfg, [12.6], label="M 31 OIII")
    assert res2["verdict"] == "SKIP" and notes2 == []       # same episode
    res3, notes3 = _run(cfg, [0.2])
    assert res3["verdict"] == "PASS"
    assert notes3 and "back at 0.2 C" in notes3[0][1]       # recovery notice
    assert [r["verdict"] for r in cg.recent(cfg)] == ["SKIP", "SKIP"]


def test_gate_is_symmetric_too_cold_also_waits(tmp_path):
    res, _ = _run(_cfg(tmp_path), [-3.0])
    assert res["verdict"] == "SKIP"


def test_gate_fails_open_when_the_sensor_is_unreadable(tmp_path):
    res, notes = _run(_cfg(tmp_path), [None])
    assert res["verdict"] == "UNKNOWN" and res["waited_s"] == 30 and not notes


def test_warn_mode_alerts_but_does_not_skip(tmp_path):
    res, notes = _run(_cfg(tmp_path, cooler_gate_mode="warn"), [9.0])
    assert res["verdict"] == "WARN" and "imaging M 31 Ha WARM" in notes[0][1]


def test_off_mode_does_not_read(tmp_path):
    res, notes = _run(_cfg(tmp_path, cooler_gate_mode="off"), [30.0])
    assert res["verdict"] == "OFF"


def test_gate_stops_quietly_when_nina_cancels(tmp_path):
    async def gone():
        return True
    res, notes = _run(_cfg(tmp_path), [12.0], disconnected=gone)
    assert res["verdict"] == "ABORTED" and not notes and "rc16" not in cg.LIVE


def test_piggyback_message_names_the_rig(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=True, piggyback_name="Piggy-600")
    res, notes = _run(cfg, [7.0], rig="piggyback", label="OSC lights")
    assert notes[0][1].startswith("Piggy-600: not imaging OSC lights, sensor at 7.0 C")


def test_labels_are_command_line_safe():
    assert cg.safe_label('Cat\'s Eye "N" & <x>') == "Cat s Eye N x"
    assert cg.script_args("rc16", 0.0, "M 31 Ha") == 'rc16 --setpoint=0 --label="M 31 Ha"'


# --- dashboard camera row ---------------------------------------------------------

def test_camera_row_note(tmp_path):
    cfg = _cfg(tmp_path)
    assert cg.camera_row_note(cfg, "rc16", 0.4, True, "RUNNING") is None
    assert cg.camera_row_note(cfg, "rc16", 6.0, True, "RUNNING") == \
        "waiting for cooler: 6.0 C -> 0 C"
    assert cg.camera_row_note(cfg, "rc16", 18.0, False, "RUNNING") is None  # pre-cool
    assert cg.camera_row_note(cfg, "rc16", 6.0, True, "DISARMED") is None
    now = 5000.0
    cg.LIVE["rc16"] = {"state": "waiting", "temp_c": 4.2, "setpoint_c": 0.0,
                       "label": "M 31 Ha", "since": now - 300, "updated": now - 5}
    assert cg.camera_row_note(cfg, "rc16", 4.0, True, "RUNNING", now=now) == \
        "waiting for cooler: 4.2 C -> 0 C (5 min, lights held: M 31 Ha)"
    cg.LIVE["rc16"] = {"state": "skipped", "temp_c": 12.4, "setpoint_c": 0.0,
                       "label": "M 31 Ha", "since": now, "updated": now,
                       "waited_s": 1200}
    note = cg.camera_row_note(cfg, "rc16", 12.4, True, "RUNNING", now=now + 60)
    assert note.startswith("not imaging: sensor 12.4 C vs setpoint 0 C after 20 min")
    assert cg.camera_row_note(cfg, "rc16", 0.1, True, "RUNNING",
                              now=now + cg.SKIP_NOTE_S + 1) is None


# --- CLI, cmd wrapper, API, config, names -------------------------------------------

def _fake_urlopen(monkeypatch, body=None, exc=None):
    import io
    import urllib.request

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        if exc:
            raise exc
        assert "/api/cooler/gate?" in req.full_url
        return _R(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)


@pytest.mark.parametrize("verdict,code", [("SKIP", 3), ("PASS", 0), ("WARN", 0),
                                          ("UNKNOWN", 0)])
def test_cli_exit_codes(monkeypatch, verdict, code):
    from typer.testing import CliRunner

    from photonscript import cli
    _fake_urlopen(monkeypatch, {"verdict": verdict, "reason": "x"})
    r = CliRunner().invoke(cli.app, ["cooler-gate", "rc16", "--setpoint=-10",
                                     '--label=M 31 Ha', "--from-nina"])
    assert r.exit_code == code, r.output


def test_cli_fails_open_when_the_service_is_down(monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    _fake_urlopen(monkeypatch, exc=OSError("refused"))
    r = CliRunner().invoke(cli.app, ["cooler-gate", "piggyback", "--from-nina"])
    assert r.exit_code == 0 and "imaging anyway" in r.output


def test_cmd_wrapper_maps_only_skip_to_failure():
    text = (REPO / "deploy" / "cooler-gate.cmd").read_bytes()
    assert all(b < 128 for b in text)
    s = text.decode("ascii")
    assert "cooler-gate %* --from-nina" in s
    assert "if %ERRORLEVEL% EQU 3 exit /b 1" in s and s.rstrip().endswith("exit /b 0")
    assert PhotonScriptConfig(_env_file=None).cooler_gate_script.endswith(
        "deploy\\cooler-gate.cmd")


def test_config_defaults_and_system_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None, cooler_gate_mode="skip")
    assert (c.cooler_gate_tolerance_c, c.cooler_gate_timeout_min) == (1.0, 20.0)
    by_attr = {f[0]: f for f in _CONFIG_FIELDS}
    for k in ("cooler_gate_mode", "cooler_gate_tolerance_c",
              "cooler_gate_timeout_min", "cooler_gate_script"):
        assert by_attr[k][1] == "PS_" + k.upper()
    assert cg.gate_mode(PhotonScriptConfig(_env_file=None, cooler_gate_mode="bogus")) == "skip"


def test_api_status_and_unknown_rig(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import cooler as r
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    s = r.api_cooler_gate_status()
    assert s["mode"] == "skip" and s["tolerance_c"] == 1.0 and s["live"] == {}

    class _Req:
        async def is_disconnected(self):
            return False
    res = asyncio.run(r.api_cooler_gate(_Req(), rig="piggyback"))   # not enabled
    assert res["verdict"] == "UNKNOWN"


def test_block_container_name_maps_to_the_target():
    from photonscript.shared.target_names import canonical_target
    assert canonical_target("Heart Nebula filter block (cooler-gated)_Container") \
        == "Heart Nebula"
