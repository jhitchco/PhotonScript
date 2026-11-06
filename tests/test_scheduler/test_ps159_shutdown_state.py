"""PS-159: the dawn shutdown reads state before it acts.

2026-10-07 13:07Z: NINA #1's own End area had parked (mount parked since the
09:24Z unsafe) and DisconnectAllEquipment had run, so the armer's guider
stop and park had nothing to talk to and the report said "guider stop
FAILED ... park FAILED". Already parked / guider not running are ok; only a
real failure pages.
"""
from datetime import datetime

import pytest

import photonscript.scheduler.armer as armer_mod
from photonscript.scheduler.armer import Armer
from photonscript.shared.config import PhotonScriptConfig


def _armer(tmp_path):
    return Armer(PhotonScriptConfig(data_dir=tmp_path))


def _patch_rigs(monkeypatch):
    import photonscript.shared.rigs as rigs_mod
    import photonscript.scheduler.runs as runs_mod
    from types import SimpleNamespace

    async def _ok(base, *args, **kw):
        return {"ok": True}

    monkeypatch.setattr(rigs_mod, "rig_ids", lambda cfg: ["rc16", "piggyback"])
    monkeypatch.setattr(rigs_mod, "rig_config", lambda cfg, r:
                        SimpleNamespace(nina_base_url=f"http://{r}"))
    monkeypatch.setattr(rigs_mod, "nina_warm", _ok)
    monkeypatch.setattr(rigs_mod, "nina_dew_heater", _ok)
    monkeypatch.setattr(rigs_mod, "nina_sequence_stop", _ok)
    monkeypatch.setattr(runs_mod, "post_night_warm", lambda cfg: [])


def _fake_nina(a, monkeypatch, replies: dict, calls: list):
    async def _nina(key, *args, **kw):
        calls.append(key)
        r = replies.get(key, {"Success": True})
        if r is None:
            a.detail = f"{key}: Mount not connected"
        return r
    monkeypatch.setattr(a, "_nina", _nina)

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(a, "_verify_shutdown", _noop)


def _no_thesky(a, monkeypatch, value=None):
    async def _ts():
        return value
    monkeypatch.setattr(a, "_thesky_parked", _ts)


@pytest.mark.asyncio
async def test_2026_10_07_already_parked_and_disconnected_is_ok(tmp_path,
                                                                monkeypatch):
    """The evidence night: every NINA #1 device disconnected, the mount log
    last line says parked. No FAILED, no park command, nothing pages."""
    from photonscript.shared import mount_log
    from photonscript.shared.phd2_store import append_jsonl, night_of
    a = _armer(tmp_path)
    _patch_rigs(monkeypatch)
    _no_thesky(a, monkeypatch, None)
    now = datetime.utcnow()
    append_jsonl(mount_log.log_path(a.config, night_of(a.config, now)),
                 {"t": now.replace(microsecond=0).isoformat() + "Z",
                  "rig": "rc16", "ra": 10.0, "dec": 20.0, "parked": True,
                  "tracking": False, "slewing": False, "why": "park"})
    calls: list = []
    _fake_nina(a, monkeypatch, {
        "guider": {"Response": {"Connected": False}},
        "guider_stop": None,
        "mount_info": {"Response": {"Connected": False}},
        "mount_park": None}, calls)
    report = await a.dawn_shutdown(reason="dawn")
    assert "FAILED" not in report
    assert "guider stop ok (not connected)" in report
    assert "park ok (already parked, mount log)" in report
    assert "guider_stop" not in calls and "mount_park" not in calls
    assert a.shutdown["failed"] == []


@pytest.mark.asyncio
async def test_nina_reports_parked_skips_the_park(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    _patch_rigs(monkeypatch)
    calls: list = []
    _fake_nina(a, monkeypatch, {
        "guider": {"Response": {"Connected": True, "State": "Stopped"}},
        "mount_info": {"Response": {"Connected": True, "AtPark": True}}},
        calls)
    report = await a.dawn_shutdown()
    assert "park ok (already parked)" in report
    assert "guider stop ok (stopped)" in report
    assert "mount_park" not in calls and "guider_stop" not in calls


@pytest.mark.asyncio
async def test_thesky_says_parked_when_nina_mount_disconnected(tmp_path,
                                                              monkeypatch):
    a = _armer(tmp_path)
    _patch_rigs(monkeypatch)
    _no_thesky(a, monkeypatch, True)
    calls: list = []
    _fake_nina(a, monkeypatch, {
        "mount_info": {"Response": {"Connected": False}}}, calls)
    report = await a.dawn_shutdown()
    assert "park ok (already parked, TheSky)" in report
    assert "mount_park" not in calls


@pytest.mark.asyncio
async def test_unparked_mount_still_parks(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    _patch_rigs(monkeypatch)
    calls: list = []
    _fake_nina(a, monkeypatch, {
        "guider": {"Response": {"Connected": True, "State": "Guiding"}},
        "mount_info": {"Response": {"Connected": True, "AtPark": False}}},
        calls)
    report = await a.dawn_shutdown()
    assert "mount_park" in calls and "guider_stop" in calls
    assert "park ok" in report and "already" not in report


@pytest.mark.asyncio
async def test_real_park_failure_is_failed_and_pages(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    _patch_rigs(monkeypatch)
    _no_thesky(a, monkeypatch, None)
    calls: list = []
    _fake_nina(a, monkeypatch, {
        "mount_info": {"Response": {"Connected": True, "AtPark": False}},
        "mount_park": None}, calls)
    report = await a.dawn_shutdown()
    assert "park FAILED" in report
    assert a.shutdown["failed"] and "park FAILED" in a.shutdown["failed"][0]
    sent = []

    async def _notify(cfg, msg, title="", priority=0, **kw):
        sent.append((msg, priority))
    monkeypatch.setattr(armer_mod, "notify", _notify)
    import photonscript.scheduler.split_guard as sg
    monkeypatch.setattr(sg, "morning_split_note", lambda cfg, n: ("", False))
    await a._notify_complete("Night complete")
    assert sent and sent[0][1] == 1 and "Shutdown step FAILED" in sent[0][0]


@pytest.mark.asyncio
async def test_park_fails_then_reads_parked_is_ok(tmp_path, monkeypatch):
    """The park call races the End area's own park + disconnect."""
    a = _armer(tmp_path)
    _patch_rigs(monkeypatch)
    _no_thesky(a, monkeypatch, True)
    seq = iter([{"Response": {"Connected": True, "AtPark": False}},
                {"Response": {"Connected": False}}])
    calls: list = []

    async def _nina(key, *args, **kw):
        calls.append(key)
        if key == "mount_info":
            return next(seq)
        if key == "mount_park":
            a.detail = "mount_park: not connected"
            return None
        return {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(a, "_verify_shutdown", _noop)
    report = await a.dawn_shutdown()
    assert "park ok (already parked, TheSky)" in report
    assert a.shutdown["failed"] == []


@pytest.mark.asyncio
async def test_clean_shutdown_does_not_page(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    a.shutdown = {"steps": ["park ok (already parked)"], "failed": []}
    sent = []

    async def _notify(cfg, msg, title="", priority=0, **kw):
        sent.append(priority)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    import photonscript.scheduler.split_guard as sg
    monkeypatch.setattr(sg, "morning_split_note", lambda cfg, n: ("", False))
    await a._notify_complete("Night complete")
    assert sent == [0]


@pytest.mark.asyncio
async def test_make_safe_trusts_only_nina_parked(tmp_path, monkeypatch):
    """Make-safe is an abort: a disconnected mount is connected and parked
    even when the mount log remembers a park."""
    a = _armer(tmp_path)
    _no_thesky(a, monkeypatch, True)
    calls: list = []
    _fake_nina(a, monkeypatch, {
        "mount_info": {"Response": {"Connected": False}}}, calls)
    report = await a.make_safe()
    assert "mount_park" in calls and "park:ok" in report
    calls.clear()
    _fake_nina(a, monkeypatch, {
        "mount_info": {"Response": {"Connected": True, "AtPark": True}}},
        calls)
    report = await a.make_safe()
    assert "mount_park" not in calls and "park:ok (already parked)" in report


def test_mount_log_parked_reads_last_line(tmp_path):
    from photonscript.shared import mount_log
    from photonscript.shared.phd2_store import append_jsonl, night_of
    a = _armer(tmp_path)
    assert a._mount_log_parked() is None
    now = datetime.utcnow()
    p = mount_log.log_path(a.config, night_of(a.config, now))
    t = now.replace(microsecond=0).isoformat() + "Z"
    append_jsonl(p, {"t": t, "parked": False, "why": "start"})
    assert a._mount_log_parked() is False
    append_jsonl(p, {"t": t, "parked": True, "why": "park"})
    assert a._mount_log_parked() is True


def test_generated_end_area_parks_before_disconnecting():
    """The sequence end area order the armer relies on: StopGuiding, park,
    then DisconnectAllEquipment (the park must never follow the
    disconnect)."""
    import json
    from photonscript.scheduler import nina_sequence_json as nsj
    src = open(nsj.__file__, encoding="utf-8").read()
    i_stop = src.index("end_items.append(_stop_guiding())")
    i_park = src.index("end_items.append(_park())")
    i_disc = src.index("end_items.append(_disconnect_all())")
    assert i_stop < i_park < i_disc
    assert json.dumps(nsj._disconnect_all())
