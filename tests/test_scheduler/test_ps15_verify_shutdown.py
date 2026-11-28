"""PS-15: the dawn-shutdown cooler verify (safety-critical, was untested) and
the Piggy-600 companion dispatch status.

_verify_shutdown: every cooler off = ok, no push; a cooler still on gets one
warm + dew-off retry on that rig and one priority-1 push; an unreachable NINA
is recorded as "unreachable" (not as a cooler that is on).

_dispatch_piggyback_companion: the outcome (dispatched | lint_refused |
failed | error | skipped) is kept on the armer, persisted, shown in status()
and written as one kind "companion_dispatch" run event.
"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import photonscript.scheduler.armer as armer_mod
from photonscript.scheduler.armer import Armer
from photonscript.shared.config import PhotonScriptConfig


def _armer(tmp_path, **kw):
    return Armer(PhotonScriptConfig(data_dir=tmp_path, **kw))


def _capture_notify(monkeypatch) -> list:
    sent: list = []

    async def _notify(cfg, msg, title="", priority=0, **kw):
        sent.append({"msg": msg, "priority": priority, **kw})
    monkeypatch.setattr(armer_mod, "notify", _notify)
    return sent


# -- _verify_shutdown --------------------------------------------------------

def _patch_verify_rigs(monkeypatch, cams: dict) -> list:
    """cams: rig -> camera info dict (None = NINA unreachable). Returns the
    list of (call, base, arg) the warm / dew retry made."""
    import photonscript.shared.rigs as rigs_mod
    calls: list = []

    async def _cam(base):
        return cams[base.split("//")[1]]

    async def _warm(base, minutes=0.0):
        calls.append(("warm", base, minutes))
        return {"ok": True}

    async def _dew(base, on):
        calls.append(("dew", base, on))
        return {"ok": True}

    monkeypatch.setattr(rigs_mod, "rig_ids", lambda cfg: list(cams))
    monkeypatch.setattr(rigs_mod, "rig_config", lambda cfg, r:
                        SimpleNamespace(nina_base_url=f"http://{r}"))
    monkeypatch.setattr(rigs_mod, "nina_camera_info", _cam)
    monkeypatch.setattr(rigs_mod, "nina_warm", _warm)
    monkeypatch.setattr(rigs_mod, "nina_dew_heater", _dew)
    return calls


@pytest.mark.asyncio
async def test_verify_all_coolers_off_is_ok_and_silent(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    a.shutdown = {"at": datetime.utcnow().isoformat() + "Z"}
    sent = _capture_notify(monkeypatch)
    calls = _patch_verify_rigs(monkeypatch, {
        "rc16": {"CoolerOn": False, "Temperature": 12.0, "DewHeaterOn": False},
        "piggyback": {"CoolerOn": False, "Temperature": 14.0}})
    await a._verify_shutdown(delay_s=0)
    v = a.shutdown["verify"]
    assert v["ok"] is True
    assert v["rigs"]["rc16"] == {"cooler_on": False, "temp_c": 12.0,
                                 "dew_on": False}
    assert v["rigs"]["piggyback"]["cooler_on"] is False
    assert calls == [] and sent == []
    saved = json.loads((Path(tmp_path) / "armer_state.json").read_text())
    assert saved["shutdown"]["verify"]["ok"] is True


@pytest.mark.asyncio
async def test_verify_one_cooler_on_retries_that_rig_and_pushes(tmp_path,
                                                                monkeypatch):
    a = _armer(tmp_path)
    a.shutdown = {"at": datetime.utcnow().isoformat() + "Z"}
    sent = _capture_notify(monkeypatch)
    calls = _patch_verify_rigs(monkeypatch, {
        "rc16": {"CoolerOn": False, "Temperature": 12.0},
        "piggyback": {"CoolerOn": True, "Temperature": -10.0,
                      "DewHeaterOn": True}})
    await a._verify_shutdown(delay_s=0)
    v = a.shutdown["verify"]
    assert v["ok"] is False
    assert v["rigs"]["piggyback"]["cooler_on"] is True
    # one warm + one dew-off, on the rig whose cooler is on only
    assert [c[:2] for c in calls] == [("warm", "http://piggyback"),
                                      ("dew", "http://piggyback")]
    assert calls[1][2] is False
    assert len(sent) == 1
    assert sent[0]["priority"] == 1 and sent[0].get("safety") is True
    assert "piggyback" in sent[0]["msg"] and "STILL ON" in sent[0]["msg"]


@pytest.mark.asyncio
async def test_verify_unreachable_nina_is_recorded_not_on(tmp_path,
                                                          monkeypatch):
    a = _armer(tmp_path)
    a.shutdown = {"at": datetime.utcnow().isoformat() + "Z"}
    sent = _capture_notify(monkeypatch)
    calls = _patch_verify_rigs(monkeypatch, {
        "rc16": {"CoolerOn": False, "Temperature": 12.0},
        "piggyback": None})
    await a._verify_shutdown(delay_s=0)
    v = a.shutdown["verify"]
    assert v["rigs"]["piggyback"] == "unreachable"
    # unreachable is not a cooler that is on: no warm retry, no push
    assert v["ok"] is True
    assert calls == [] and sent == []


@pytest.mark.asyncio
async def test_verify_without_shutdown_record_still_pushes(tmp_path,
                                                           monkeypatch):
    a = _armer(tmp_path)
    a.shutdown = None
    sent = _capture_notify(monkeypatch)
    _patch_verify_rigs(monkeypatch, {"rc16": {"CoolerOn": True}})
    await a._verify_shutdown(delay_s=0)
    assert a.shutdown is None
    assert len(sent) == 1 and sent[0]["priority"] == 1


# -- companion dispatch status ------------------------------------------------

NIGHT = "2026-11-01"


def _companion_armer(tmp_path, monkeypatch, *, lint_ok=True, dispatch=None,
                     gen_raises=False, **cfg):
    """An armer with the companion's collaborators faked. dispatch: the
    nina_dispatch reply."""
    import photonscript.shared.rigs as rigs_mod
    import photonscript.scheduler.preflight as pre_mod
    import photonscript.scheduler.calibration as cal_mod
    import photonscript.scheduler.sideload as side_mod
    import photonscript.scheduler.sequence_lint as lint_mod
    a = _armer(tmp_path, **{"piggyback_enabled": True, **cfg})
    a.plan = {"night_of": NIGHT}
    a.sequence_path = Path(tmp_path) / "seq" / "night.json"
    a.sequence_path.parent.mkdir(parents=True, exist_ok=True)
    sent = _capture_notify(monkeypatch)

    async def _ensure(pcfg, dev, attempts=1):
        return True, {}, None

    def _gen(pcfg, **kw):
        if gen_raises:
            raise RuntimeError("generator broke")
        return json.dumps({"Name": "companion"})

    async def _dispatch(base, seq, config=None, rig=None):
        return dispatch if dispatch is not None else {"ok": True}

    finding = SimpleNamespace(level="ERROR", rule="no-mount",
                              detail="a slew in the companion")
    monkeypatch.setattr(rigs_mod, "rig_config", lambda c, r:
                        SimpleNamespace(nina_base_url="http://nina2"))
    monkeypatch.setattr(rigs_mod, "cover_enabled", lambda c: False)
    monkeypatch.setattr(rigs_mod, "nina_dispatch", _dispatch)
    monkeypatch.setattr(pre_mod, "_ensure_connected", _ensure)
    monkeypatch.setattr(cal_mod, "generate_piggyback_companion_json", _gen)
    monkeypatch.setattr(cal_mod, "osc_dawn_wanted", lambda c: True)
    monkeypatch.setattr(side_mod, "lint_companion", lambda root:
                        SimpleNamespace(ok=lint_ok,
                                        findings=[] if lint_ok else [finding]))
    monkeypatch.setattr(lint_mod, "format_result", lambda r: "lint")
    monkeypatch.setattr(a, "_piggy_unguided_cfg", lambda p: p)
    monkeypatch.setattr(a, "_piggy_length_kwargs", lambda p: (p, {}))
    return a, sent


def _events(a) -> list:
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import night_of
    p = events_path(a.config, night_of(a.config, datetime.utcnow()))
    if not Path(p).exists():
        return []
    return [json.loads(ln) for ln in Path(p).read_text().splitlines()
            if '"companion_dispatch"' in ln]


@pytest.mark.asyncio
async def test_companion_dispatched(tmp_path, monkeypatch):
    a, sent = _companion_armer(tmp_path, monkeypatch)
    rec = await a._dispatch_piggyback_companion()
    assert rec["state"] == "dispatched"
    assert rec["detail"] == "lights+flats+darks/bias"
    assert rec["sequence"].startswith("piggyback_companion_")
    assert rec["reason"] == "arm" and rec["night"] == NIGHT
    st = a.status()["companion"]
    assert st["state"] == "dispatched" and st["at"] == rec["at"]
    saved = json.loads((Path(tmp_path) / "armer_state.json").read_text())
    assert saved["companion"]["state"] == "dispatched"
    ev = _events(a)
    assert len(ev) == 1 and ev[0]["value"] == "dispatched"
    assert ev[0]["rig"] == "piggyback"


@pytest.mark.asyncio
async def test_companion_lint_refused(tmp_path, monkeypatch):
    a, sent = _companion_armer(tmp_path, monkeypatch, lint_ok=False)
    rec = await a._dispatch_piggyback_companion()
    assert rec["state"] == "lint_refused"
    assert "no-mount" in rec["detail"]
    assert any("lint" in s["msg"] for s in sent)
    assert _events(a)[0]["value"] == "lint_refused"


@pytest.mark.asyncio
async def test_companion_failed(tmp_path, monkeypatch):
    a, sent = _companion_armer(tmp_path, monkeypatch,
                               dispatch={"ok": False, "detail": "NINA #2 down"})
    rec = await a._dispatch_piggyback_companion(reason="resume")
    assert rec["state"] == "failed" and rec["detail"] == "NINA #2 down"
    assert rec["reason"] == "resume"
    assert sent and sent[-1]["priority"] == 1
    assert a.status()["companion"]["state"] == "failed"


@pytest.mark.asyncio
async def test_companion_error_never_raises(tmp_path, monkeypatch):
    a, _ = _companion_armer(tmp_path, monkeypatch, gen_raises=True)
    rec = await a._dispatch_piggyback_companion()
    assert rec["state"] == "error" and "generator broke" in rec["detail"]
    assert rec["sequence"] is None


@pytest.mark.asyncio
async def test_companion_skipped_when_piggyback_off(tmp_path, monkeypatch):
    a = _armer(tmp_path, piggyback_enabled=False)
    a.plan = {"night_of": NIGHT}
    rec = await a._dispatch_piggyback_companion()
    assert rec["state"] == "skipped" and rec["detail"] == "piggyback off"
    assert _events(a)[0]["value"] == "skipped"


@pytest.mark.asyncio
async def test_companion_status_is_this_night_only_and_restored(tmp_path,
                                                                monkeypatch):
    a = _armer(tmp_path)
    a.plan = {"night_of": NIGHT}
    a.companion = {"night": "2026-10-31", "state": "dispatched", "at": "x"}
    assert a.status()["companion"] is None
    a.companion = {"night": NIGHT, "state": "failed", "at": "x",
                   "detail": "d", "sequence": "s", "reason": "arm"}
    a.state = "RUNNING"
    a._persist()
    saved = json.loads((Path(tmp_path) / "armer_state.json").read_text())
    assert saved["companion"]["state"] == "failed"
    b = _armer(tmp_path)

    async def _idle():
        return None
    monkeypatch.setattr(b, "_run", _idle)
    _capture_notify(monkeypatch)
    assert b.restore() is True
    for t in (b._task, getattr(b, "_pause_task", None)):
        if t is not None:
            t.cancel()
    assert b.companion["state"] == "failed"
    assert b.status()["companion"]["state"] == "failed"


def test_companion_redispatch_text():
    f = Armer._companion_redispatch_text
    assert f({"state": "dispatched"}) == "Piggy-600 companion re-dispatched"
    assert f(None) == "Piggy-600 companion re-dispatched"
    assert f({"state": "failed", "detail": "NINA #2 down"}) == (
        "Piggy-600 companion NOT re-dispatched (failed: NINA #2 down)")
