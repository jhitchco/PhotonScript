"""PS-152: test subs and calibration targets.

1. Tracking-test (PS-84) subs, like optics-test (PS-148) subs, neither count
   toward nor reset the telescope agent's consecutive-reject streak.
2. The watch guiding fallback (no sideloaded file) skips every test container.
3. Test subs never feed night medians, QA baselines or the night score.
4. The PS-144 dusk focus calibration runs once per night.
5. focus_seeds.harvest_night skips filters with a configured focus offset.
"""
import asyncio
import json
from pathlib import Path

import pytest

from photonscript.shared.target_names import is_test_target

ROOT = Path(__file__).resolve().parents[2]


# ---- shared helper -------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Tracking test Heart Nebula",
    "Tracking test Heart Nebula unguided ladder_Container",
    "Optics test M 2",
    "Optics test M 2 L -300",
    "Optics test M 2 through-focus sweep_Container",
    "Focus calibration M52",
    "Focus calibration NGC 7789_Container",
    "NGC 7789 focus calibration AFs_Container",
    "tracking TEST m 31",
])
def test_test_targets(name):
    assert is_test_target(name) is True


@pytest.mark.parametrize("name", [
    "Heart Nebula", "M 31_Container", "", None, "?",
    "Heart Nebula imaging (repeats while safe and up)_Container",
    "Testudo Nebula", "Optical Ring",
])
def test_imaging_targets(name):
    assert is_test_target(name) is False


# ---- 1. reject streak ----------------------------------------------------------

def test_agent_streak_skips_every_test_target():
    src = (ROOT / "photonscript/telescope_agent/agent.py").read_text(
        encoding="utf-8")
    i = src.index("if is_test_target(target_name):")
    j = src.index("self._consecutive_rejects = 0", i)
    k = src.index("self._consecutive_rejects += 1", i)
    # the test branch comes first and does nothing: no reset, no count
    branch = src[i:min(j, k)]
    assert "pass" in branch and "_consecutive_rejects" not in branch
    assert "is_optics_test(target_name)" not in src


# ---- 2. watch guiding fallback -------------------------------------------------

def _cfg(tmp_path, **kw):
    from photonscript.shared.config import PhotonScriptConfig
    kw.setdefault("connect_all_on_arm", False)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


@pytest.mark.parametrize("running", [
    ["Tracking test M 2_Container"],
    ["Optics test M 2_Container", "Optics test M 2 L -300 through-focus step_Container"],
    ["Focus calibration M52_Container"],
    ["Targets_Container", "Focus calibration M52 focus calibration AFs_Container"],
    ["NGC 7789 focus calibration AFs_Container"],
])
def test_watch_fallback_skips_test_containers(tmp_path, running):
    from photonscript.scheduler.armer import Armer
    a = Armer(_cfg(tmp_path, guided_default=True))
    a.watch = {"guided_targets": None}
    assert a._watch_guiding_active(running) is False
    assert a._watch_guiding_active(["Targets_Container", "M 31_Container"]) is True


def test_watch_with_file_is_unchanged(tmp_path):
    from photonscript.scheduler.armer import Armer
    a = Armer(_cfg(tmp_path))
    a.watch = {"guided_targets": ["M 31"]}
    assert a._watch_guiding_active(["M 31_Container"]) is True
    assert a._watch_guiding_active(["Optics test M 2_Container"]) is False


# ---- 3. night medians, baselines, night score ----------------------------------

def _sub(target, hfr, passed=True, flt="L", exp=300.0, **kw):
    return {"rig": "rc16", "target": target, "filter": flt, "hfr": hfr,
            "background": 1000.0, "passed_qa": passed, "exp_s": exp,
            "fwhm_arcsec": hfr * 0.236, "stars": 500, "ecc": 0.4,
            "file": "2026-10-06/x.fits", **kw}


def test_night_context_ignores_test_subs():
    from photonscript.shared import qa_rules
    recs = [_sub("M 31", 4.0) for _ in range(5)]
    recs += [_sub("Tracking test M 31", 9.0) for _ in range(6)]
    recs += [_sub("Whatever", 9.0, test=True)]       # flag alone is enough
    ctx = qa_rules.night_context(recs)
    assert set(ctx) == {("rc16", "M 31", "L")}
    assert ctx[("rc16", "M 31", "L")]["hfr_median"] == 4.0
    assert qa_rules.is_test_record({"target": "Optics test M 2 L -300"})
    assert not qa_rules.is_test_record({"target": "M 2"})


def test_baselines_ignore_test_subs(tmp_path):
    from photonscript.scheduler import qa_baselines
    cfg = _cfg(tmp_path)
    real = [_sub("M 31", 4.0, _night="2026-10-06") for _ in range(20)]
    test = [_sub("Optics test M 2 L -300", 12.0, _night="2026-10-06")
            for _ in range(20)]
    a = qa_baselines.baselines(cfg, records=real)
    b = qa_baselines.baselines(cfg, records=real + test)
    assert a["rigs"] == b["rigs"]
    hfr = b["rigs"][0]["filters"][0]["metrics"]["hfr"]
    assert hfr["n"] == 20 and hfr["median"] == 4.0


class _Report:
    safe_hours = shutter_hours = integrating_hours = 1.0
    sky_utilization_pct = photon_efficiency_pct = 50.0


def test_night_score_and_hours_skip_test_subs(tmp_path, monkeypatch):
    from photonscript.scheduler import runs
    monkeypatch.setattr(runs, "calibration_inventory", lambda c, d: {})
    monkeypatch.setattr(runs, "_phase_stats", lambda c, d: {})
    real = [_sub("M 31", 4.0, passed=True, exp=360.0) for _ in range(10)]
    test = [_sub("Tracking test M 31", 9.0, passed=False, exp=360.0)
            for _ in range(10)]
    subs = real + test
    runs._flag_test_subs(subs)
    assert [s.get("test", False) for s in subs] == [False] * 10 + [True] * 10
    out = runs._night_detail_rest(_cfg(tmp_path), "2026-10-06", None, {},
                                  subs, {}, [], _Report(), 10, 0, 8.0)
    rep = out["report"]
    assert rep["light_hours"] == 1.0 and rep["accepted_hours"] == 1.0
    assert rep["test_hours"] == 1.0
    assert out["score"]["breakdown"]["keep_rate"]["value"] == 100
    assert len(out["subs"]) == 20          # recorded and listed, flagged


def test_plan_vs_actual_flags_test_rows():
    from photonscript.scheduler import runs
    _p, table = runs._plan_vs_actual(None, [_sub("M 31", 4.0),
                                            _sub("Optics test M 2", 9.0)])
    by = {r["target"]: r for r in table}
    assert by["Optics test M 2"].get("test") is True
    assert "test" not in by["M 31"]


def test_records_are_flagged_at_write():
    agent = (ROOT / "photonscript/telescope_agent/agent.py").read_text(
        encoding="utf-8")
    runs_src = (ROOT / "photonscript/scheduler/runs.py").read_text(
        encoding="utf-8")
    assert '{"test": True} if qa_rules.is_test_record(' in agent
    assert '{"test": True} if qa_rules.is_test_record({"target": target})' \
        in runs_src


# ---- 4. dusk focus calibration runs once per night -----------------------------

from tests.test_scheduler.test_ps144_focus_offsets import (  # noqa: E402
    _armer as _cal_armer, _target_containers)


def _cal_state(status):
    """ninaAPI /sequence/state with the calibration container in `status`."""
    return {"Response": [{"Name": "Targets_Container", "Status": "RUNNING",
                          "Items": [{"Name": "Focus calibration M52_Container",
                                     "Status": status},
                                    {"Name": "Heart Nebula_Container",
                                     "Status": "RUNNING"}]}]}


def _fake_nina(a, monkeypatch, tree, order):
    from photonscript.scheduler import armer as armer_mod
    from photonscript.shared import envfile

    async def _nina(key, **kw):
        order.append(key)
        return {"Success": True}

    async def _read():
        order.append("state")
        return tree, None

    async def _notify(*a_, **k):
        return True

    async def _none():
        return None
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(a, "_watch_read_state", _read)
    monkeypatch.setattr(a, "_send_block_alerts", _none)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    monkeypatch.setattr(envfile, "env_path",
                        lambda root=None: Path(a.config.data_dir) / ".env")


def _events(tmp_path):
    out = []
    for p in (tmp_path / "data" / "runs").glob("*_events.jsonl"):
        out += [json.loads(x) for x in p.read_text().splitlines()]
    return out


async def test_calibration_is_recorded_and_never_repeated(tmp_path, monkeypatch):
    a = _cal_armer(tmp_path, monkeypatch, focus_calibration_tonight=True)
    order = []
    _fake_nina(a, monkeypatch, _cal_state("FINISHED"), order)
    assert await a._dispatch_and_start(companion=False) is True
    assert "state" not in order                  # nothing dispatched before
    fc = a.focus_cal
    assert fc["night"] == "2026-10-06" and fc["status"] == "dispatched"
    assert fc["container"] == "Focus calibration M52"
    saved = json.loads((tmp_path / "data" / "armer_state.json").read_text())
    assert saved["focus_cal"]["status"] == "dispatched"
    # the flag comes back on (failed .env reset + restart, or re-ticked):
    # a re-dispatch still leaves the calibration out
    a.config.focus_calibration_tonight = True
    order.clear()
    assert await a._dispatch_and_start(companion=False) is True
    assert order[:2] == ["state", "sequence_stop"]   # read before the stop
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert _target_containers(seq) == ["Heart Nebula"]
    assert a.focus_cal["status"] == "done" and a.focus_cal["done_utc"]
    ev = [e for e in _events(tmp_path) if e["kind"] == "focus_calibration"]
    assert [e["value"] for e in ev] == ["dispatched", "done"]


@pytest.mark.parametrize("status", ["RUNNING", "CREATED", None])
def test_not_done_until_its_container_finished(tmp_path, monkeypatch, status):
    a = _cal_armer(tmp_path, monkeypatch)
    a.focus_cal = {"night": "2026-10-06", "container": "Focus calibration M52",
                   "status": "dispatched"}
    tree = _cal_state(status) if status else {"Response": []}
    assert a._note_focus_cal_progress(tree) is False
    assert a.focus_cal["status"] == "dispatched"
    assert a._note_focus_cal_progress(_cal_state("FINISHED")) is True
    assert a.focus_cal["status"] == "done"
    assert a._note_focus_cal_progress(_cal_state("FINISHED")) is False  # once


def test_a_new_night_calibrates_again(tmp_path, monkeypatch):
    a = _cal_armer(tmp_path, monkeypatch, focus_calibration_tonight=True)
    a.focus_cal = {"night": "2026-10-05", "container": "Focus calibration M52",
                   "status": "done"}
    assert a._focus_cal_tonight() is None
    assert a._dispatch() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert _target_containers(seq)[0].startswith("Focus calibration ")


def test_focus_cal_survives_a_restart(tmp_path, monkeypatch):
    from photonscript.scheduler.armer import Armer
    a = _cal_armer(tmp_path, monkeypatch)
    a.state = "RUNNING"
    a.focus_cal = {"night": "2026-10-06", "container": "Focus calibration M52",
                   "status": "dispatched"}
    a.plan["dawn_utc"] = "2099-01-01T11:30:00Z"
    a._persist()
    b = Armer(a.config)
    monkeypatch.setattr(b, "_run", lambda: asyncio.sleep(0))
    import photonscript.scheduler.armer as armer_mod
    monkeypatch.setattr(armer_mod.asyncio, "create_task",
                        lambda c: (c.close(), None)[1])
    assert b.restore() is True
    assert b.focus_cal == a.focus_cal
