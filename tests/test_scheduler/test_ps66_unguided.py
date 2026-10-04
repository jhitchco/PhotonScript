"""PS-66 part 1: the unguided (TPoint + ProTrack) mode.

1. "unguided" name with the old "encoders" accepted as an alias.
2. Unguided sub-length cap and time-weighted crediting.
3. Nanny quiet in unguided mode.
4. Unguided dither through NINA's Direct Guider.
"""
import json

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler.armer import Armer, norm_guiding_mode
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    return PhotonScriptConfig(_env_file=None, **kw)


async def _noop(*a, **k):
    return None


# ---- 1. mode name and alias --------------------------------------------------

def test_norm_guiding_mode_aliases():
    assert norm_guiding_mode("encoders") == "unguided"
    assert norm_guiding_mode("Unguided") == "unguided"
    assert norm_guiding_mode(" guided ") == "guided"
    for junk in (None, "", "phd2", "default", 1, True):
        assert norm_guiding_mode(junk) is None


def test_encoders_override_is_unguided_and_status_says_unguided(tmp_path):
    a = Armer(_cfg(tmp_path, guided_default=True, noon_arm_guided=False))
    a.guiding_override = "encoders"
    assert a._use_guiding() is False
    st = a.status()
    assert st["guiding"] == "unguided"
    assert st["noon_arm"]["guiding"] == "unguided"


def test_persisted_encoders_restores_as_unguided(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, connect_all_on_arm=False)
    p = tmp_path / "data" / "armer_state.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"state": "ARMED", "plan": {"night_of": "2026-10-03"},
                             "guiding_override": "encoders"}))
    a = Armer(cfg)
    created = []
    monkeypatch.setattr(armer_mod.asyncio, "create_task",
                        lambda coro: (created.append(coro), coro.close()))
    assert a.restore() is True
    assert a.guiding_override == "unguided" and a._use_guiding() is False


async def test_arm_accepts_encoders_alias(tmp_path, monkeypatch):
    a = Armer(_cfg(tmp_path, connect_all_on_arm=False,
                   cooler_off_until_precool=False))
    monkeypatch.setattr(armer_mod, "notify", _noop)
    monkeypatch.setattr("photonscript.scheduler.night_plan.build_night_plan",
                        lambda cfg: {"night_of": "2026-10-03", "targets": ["Heart"],
                                     "preconfig_utc": "2026-10-04T00:30:00Z",
                                     "dark_hours": 8.0})
    monkeypatch.setattr(a, "_run", _noop)

    async def _nina_down(key, *x, **k):          # NINA unreachable at arm
        a.detail = f"{key}: All connection attempts failed"
        return None
    monkeypatch.setattr(a, "_nina", _nina_down)
    st = await a.arm("encoders")
    assert a.guiding_override == "unguided"
    assert st["guiding"] == "unguided"
    assert "unguided (TPoint + ProTrack)" in st["detail"]
    assert a._audit_task is None                      # no PS-89 audit unguided


def test_api_arm_normalizes_and_rejects_junk(monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app

    seen = []

    class _FakeArmer:
        async def arm(self, guiding=None):
            seen.append(guiding)
            return {"state": "ARMED", "guiding": guiding}

        async def disarm(self):
            return {"state": "DISARMED"}

    monkeypatch.setattr(app, "_armer", _FakeArmer())
    client = TestClient(app.app)
    r = client.post("/api/arm", json={"armed": True, "guiding": "warp drive"})
    assert r.status_code == 400 and "unknown guiding mode" in r.json()["detail"]
    assert seen == []
    assert client.post("/api/arm", json={"armed": True,
                                         "guiding": "encoders"}).status_code == 200
    assert client.post("/api/arm", json={"armed": True}).status_code == 200
    assert client.post("/api/arm", json={"armed": True,
                                         "guiding": "guided"}).status_code == 200
    assert seen == ["unguided", None, "guided"]


# ---- 2. unguided exposure cap and time-weighted crediting ---------------------

from photonscript.scheduler.target_planner import cap_unguided  # noqa: E402
from photonscript.shared.models import (  # noqa: E402
    CelestialTarget, ExposurePlan, FilterType, NinaSequenceTarget)


def _plan(f=FilterType.HA, exp=600, count=30, acquired=0, **kw):
    return ExposurePlan(filter_type=f, exposure_seconds=exp, count=count,
                        acquired=acquired, gain=200, offset=256, **kw)


def _tgt(name="Heart Nebula", guided=False, exposures=None):
    return NinaSequenceTarget(name=name, ra_hours=2.55, dec_degrees=61.5,
                              start_guiding=guided,
                              exposures=exposures or [_plan()])


def test_cap_unguided_keeps_integration():
    t = _tgt(exposures=[_plan(count=30), _plan(FilterType.OIII, exp=600, count=30,
                                               acquired=10),
                        _plan(FilterType.LUMINANCE, exp=180, count=20)])
    notes = cap_unguided([t], 300)
    ha, oiii, lum = t.exposures
    assert (ha.exposure_seconds, ha.count - ha.acquired) == (300, 60)
    assert (oiii.exposure_seconds, oiii.count - oiii.acquired) == (300, 40)
    assert (lum.exposure_seconds, lum.count) == (180, 20)     # under the cap
    assert len(notes) == 2


def test_cap_unguided_leaves_guided_hdr_shorts_and_off_alone():
    g = _tgt(guided=True)
    cap_unguided([g], 300)
    assert g.exposures[0].exposure_seconds == 600
    h = _tgt(exposures=[_plan(count=20, hdr_short_seconds=60, hdr_short_count=12)])
    cap_unguided([h], 300)
    e = h.exposures[0]
    assert (e.exposure_seconds, e.count) == (300, 40)
    assert (e.hdr_short_seconds, e.short_remaining()) == (60, 12)
    off = _tgt()
    cap_unguided([off], 0)
    assert off.exposures[0].exposure_seconds == 600
    odd = _tgt(exposures=[_plan(exp=500, count=3)])          # 1500 s owed
    cap_unguided([odd], 300)
    assert odd.exposures[0].count == 5


def _armer_for_dispatch(tmp_path, monkeypatch, targets, **cfg):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import target_planner

    class _Store:
        projects: dict = {}
    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: targets)
    monkeypatch.chdir(tmp_path)
    a = Armer(_cfg(tmp_path, **cfg))
    a.plan = {"night_of": "2026-10-03", "dusk_utc": "2026-10-04T01:30:00Z",
              "dawn_utc": "2026-10-04T11:30:00Z"}
    return a


def _exposure_lengths(seq_path):
    from photonscript.scheduler.sequence_lint import _exec_items
    seq = json.loads(seq_path.read_text(encoding="utf-8"))
    return sorted({it["ExposureTime"] for it in _exec_items(seq)
                   if "TakeExposure" in it.get("$type", "")
                   and it.get("ImageType") == "LIGHT"})


def test_dispatch_caps_unguided_and_snapshots_guided_flag(tmp_path, monkeypatch):
    a = _armer_for_dispatch(tmp_path, monkeypatch, [_tgt(exposures=[_plan(count=10)])],
                            guided_default=True)
    a.guiding_override = "unguided"
    assert a._dispatch() is True
    assert _exposure_lengths(a.sequence_path) == [300.0]
    snap = json.loads((tmp_path / "data" / "runs" / "2026-10-03_plan.json").read_text())
    assert snap["targets"][0]["guided"] is False
    assert snap["targets"][0]["exposures"][0] == {"filter": "Ha", "exp_s": 300.0,
                                                  "planned": 20}


def test_dispatch_guided_is_not_capped(tmp_path, monkeypatch):
    a = _armer_for_dispatch(tmp_path, monkeypatch, [_tgt(exposures=[_plan(count=10)])],
                            guided_default=True)
    monkeypatch.setattr(a, "_calibration_slot", lambda targets, now: None)
    assert a._dispatch() is True
    assert _exposure_lengths(a.sequence_path) == [600.0]
    snap = json.loads((tmp_path / "data" / "runs" / "2026-10-03_plan.json").read_text())
    assert snap["targets"][0]["guided"] is True


async def test_fallback_unguided_redispatch_is_capped(tmp_path, monkeypatch):
    a = _armer_for_dispatch(tmp_path, monkeypatch, [_tgt(exposures=[_plan(count=10)])],
                            guided_default=True)
    a.state = "RUNNING"

    async def _nina(key, *x, **k):
        return {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(armer_mod, "notify", _noop)
    assert await a.fallback_unguided("selftest FAIL") is True
    assert _exposure_lengths(a.sequence_path) == [300.0]


def test_exposure_plan_seeds_acquired_s_from_old_records():
    old = ExposurePlan(**{"filter_type": "Ha", "exposure_seconds": 600,
                          "count": 30, "acquired": 7})
    assert old.acquired_s == 4200
    keep = ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600,
                        count=30, acquired=1, acquired_s=900)
    assert keep.acquired_s == 900                         # half a sub kept


def _heart(store):
    return store.add_from_target(CelestialTarget(
        name="Heart Nebula", catalog_id="IC 1805", ra_hours=2.55,
        dec_degrees=61.5, object_type="emission nebula"), budget_hours=5.0)


def test_two_capped_subs_make_one_plan_sub_guided_subs_count_one(tmp_path):
    from photonscript.scheduler.project_store import ProjectStore
    store = ProjectStore(_cfg(tmp_path))
    proj = _heart(store)
    ha = next(e for e in proj.exposure_plans if e.filter_type.value == "Ha")
    assert ha.exposure_seconds == 600 and ha.acquired == 0
    assert store.record_accepted_sub("Heart Nebula", "Ha", 300, by_seconds=True)
    assert (ha.acquired, ha.acquired_s) == (0, 300)
    store.record_accepted_sub("Heart Nebula", "Ha", 300, by_seconds=True)
    assert (ha.acquired, ha.acquired_s) == (1, 600)
    # guided (default): one sub is one sub whatever its length, as before
    store.record_accepted_sub("Heart Nebula", "Ha", 600)
    store.record_accepted_sub("Heart Nebula", "Ha", 300)
    store.record_accepted_sub("Heart Nebula", "Ha", None, by_seconds=True)
    assert ha.acquired == 4
    # round trip: acquired_s survives save/load
    again = ProjectStore(_cfg(tmp_path))
    ha2 = next(e for e in again.projects[proj.id].exposure_plans
               if e.filter_type.value == "Ha")
    assert (ha2.acquired, ha2.acquired_s) == (4, 2400)


def test_old_projects_json_without_acquired_s_loads(tmp_path):
    from photonscript.scheduler.project_store import ProjectStore
    store = ProjectStore(_cfg(tmp_path))
    proj = _heart(store)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    for e in raw[proj.id]["exposure_plans"]:
        e.pop("acquired_s")
        e["acquired"] = 3
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    again = ProjectStore(_cfg(tmp_path))
    p = again.projects[proj.id]
    assert all(e.acquired == 3 and e.acquired_s == 3 * e.exposure_seconds
               for e in p.exposure_plans)
    p.compute_completion()                     # same count-based completion
    assert p.completion_pct == round(3 * len(p.exposure_plans)
                                     / sum(e.count for e in p.exposure_plans) * 100, 1)


def test_capped_sub_is_not_mistaken_for_an_hdr_short():
    e = _plan(count=20, hdr_short_seconds=120, hdr_short_count=12)
    assert e.is_short_exposure(120) and e.is_short_exposure(100)
    assert not e.is_short_exposure(300)       # nearer 120 than 600, still long
    assert not e.is_short_exposure(600)


def test_resync_credits_seconds_only_for_unguided_nights(tmp_path, monkeypatch):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import runs
    from photonscript.scheduler.project_store import ProjectStore
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    proj = _heart(store)
    monkeypatch.setattr(app_mod, "_store", store)
    rd = runs.runs_dir(cfg)
    rd.mkdir(parents=True, exist_ok=True)

    def _night(date, guided, n, exp):
        if guided is not None:
            (rd / f"{date}_plan.json").write_text(json.dumps({"targets": [
                {"name": "Heart Nebula", "guided": guided, "exposures": []}]}))
        with open(rd / f"{date}_subs.jsonl", "w", encoding="utf-8") as fh:
            for i in range(n):
                fh.write(json.dumps({
                    "rig": "rc16", "file": f"LIGHT/{date}_{i}.fits",
                    "time": f"{date}T03:{i:02d}:00", "target": "Heart Nebula",
                    "filter": "Ha", "exp_s": exp, "passed_qa": True,
                    "reviewed": True, "reason": ""}) + "\n")
    _night("2026-09-20", None, 3, 300)    # pre-PS-66 history: one each
    _night("2026-09-21", True, 2, 300)    # guided, off-length: still one each
    _night("2026-10-03", False, 4, 300)   # unguided capped: 4 x 300 = 2 subs
    runs.sync_goal_progress(cfg)
    ha = next(e for e in store.projects[proj.id].exposure_plans
              if e.filter_type.value == "Ha")
    assert ha.acquired == 3 + 2 + 2
    assert ha.acquired_s == (3 + 2) * 600 + 4 * 300


def test_dark_library_adds_the_unguided_cap():
    from photonscript.scheduler.nina_sequence_json import dark_library_exposures
    assert dark_library_exposures(_cfg(dark_exposures="600,180")) == [600, 180, 300]
    assert dark_library_exposures(_cfg(dark_exposures="600,300,180")) == [600, 300, 180]
    assert dark_library_exposures(_cfg(dark_exposures="600,180",
                                       unguided_max_exposure_s=0)) == [600, 180]


# ---- 3. nanny quiet in unguided mode -----------------------------------------

def _running(tmp_path, override, **cfg):
    a = Armer(_cfg(tmp_path, **cfg))
    a.state, a.plan = "RUNNING", {"night_of": "2026-10-03",
                                  "dawn_utc": "2099-01-01T11:00:00Z"}
    a.guiding_override = override
    return a


async def test_recalibrate_is_a_no_op_unguided(tmp_path, monkeypatch):
    a = _running(tmp_path, "encoders")
    calls = []

    async def _nina(key, *x, **k):
        calls.append(key)
        return {"Success": True}

    async def _dispatch(companion=True, fail_state="ERROR"):
        calls.append("dispatch")
        return True
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(a, "_dispatch_and_start", _dispatch)
    monkeypatch.setattr(armer_mod, "notify", _noop)
    assert await a.recalibrate("PHD2 has no calibration") is False
    assert calls == [] and a._recal_night is None
    a.guiding_override = "guided"                      # a guided night still does
    assert await a.recalibrate("PHD2 has no calibration") is True
    assert calls == ["sequence_stop", "guider_stop", "dispatch"]


async def test_fallback_unguided_no_op_when_already_unguided(tmp_path, monkeypatch):
    a = _running(tmp_path, "unguided")
    calls = []

    async def _nina(key, *x, **k):
        calls.append(key)
        return {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(armer_mod, "notify", _noop)
    assert await a.fallback_unguided("guard") is False
    assert calls == [] and a._fallback_night is None


def test_armer_guided_now_in_process_and_from_the_state_file(tmp_path, monkeypatch):
    import sys
    from photonscript.scheduler.armer import armer_guided_now
    cfg = _cfg(tmp_path, guided_default=True)
    if "photonscript.scheduler.app" in sys.modules:
        monkeypatch.setattr(sys.modules["photonscript.scheduler.app"], "_armer", None)
    assert armer_guided_now(cfg) is False                  # no state file
    st = tmp_path / "data" / "armer_state.json"
    st.parent.mkdir(parents=True)
    for state, override, want in (("RUNNING", "encoders", False),
                                  ("RUNNING", "unguided", False),
                                  ("ARMED", "guided", True),
                                  ("PAUSED_UNSAFE", None, True),   # config default
                                  ("DISARMED", "guided", False)):
        st.write_text(json.dumps({"state": state, "guiding_override": override}))
        assert armer_guided_now(cfg) is want, (state, override)
    from photonscript.scheduler import app as app_mod
    monkeypatch.setattr(app_mod, "_armer", _running(tmp_path / "x", "unguided"))
    assert armer_guided_now(cfg) is False                  # in-process wins
    app_mod._armer.guiding_override = "guided"
    assert armer_guided_now(cfg) is True


async def test_reauditor_quiet_when_armed_unguided(tmp_path, monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace
    from photonscript.scheduler import phd2_audit as pa
    from photonscript.telescope_agent.agent import TelescopeAgent
    if "photonscript.scheduler.app" in sys.modules:
        monkeypatch.setattr(sys.modules["photonscript.scheduler.app"], "_armer", None)
    pushes = []

    async def _occ(config, push=True, **kw):
        pushes.append(push)
        return {}
    monkeypatch.setattr(pa, "on_config_change", _occ)
    monkeypatch.setattr(pa, "DEBOUNCE_S", 0.01)
    cfg = _cfg(tmp_path, guided_default=True)
    handlers = []
    ag = SimpleNamespace(rig="rc16", config=cfg,
                         phd2=SimpleNamespace(on_event=handlers.append))
    TelescopeAgent._audit_setup(ag)
    ag.reauditor.debounce_s = 0.01
    st = tmp_path / "data" / "armer_state.json"
    st.parent.mkdir(parents=True)
    for override in ("unguided", "guided"):
        st.write_text(json.dumps({"state": "RUNNING", "guiding_override": override}))
        await handlers[0]({"Event": "ConfigurationChange"})
        await asyncio.sleep(0.1)
    assert pushes == [False, True]          # recorded both times, pushed guided only


# ---- 4. unguided dither through Direct Guider --------------------------------

def _dither_afters(seq):
    from photonscript.scheduler.sequence_lint import _find_type
    return [d["AfterExposures"] for d in _find_type(seq, "DitherAfterExposures")]


def test_smart_exposure_dithers_unguided_only_with_the_flag():
    from photonscript.scheduler.nina_sequence_json import _smart_exposure
    e = _plan(count=10)

    def after(n=5, **kw):
        return _dither_afters(_smart_exposure(e, dither_every_n=n, **kw))
    assert after(guided=True) == [5]
    assert after(guided=False) == [0]
    assert after(guided=False, unguided_dither=True) == [5]
    assert after(0, guided=False, unguided_dither=True) == [0]


def test_lint_allows_unguided_dithers_only_with_the_flag():
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.nina_sequence_json import generate_nina_json
    from photonscript.scheduler.sequence_lint import lint
    seq = build_sequence_for_night("PS66", [_tgt(exposures=[_plan(count=10)])])
    data = json.loads(generate_nina_json(seq, unguided_dither=True))
    assert set(_dither_afters(data)) == {5}
    assert not lint(data, guided=False).ok
    assert lint(data, guided=False, unguided_dither=True).ok
    plain = json.loads(generate_nina_json(seq))
    assert set(_dither_afters(plain)) == {0}
    assert lint(plain, guided=False).ok
    g = build_sequence_for_night("PS66", [_tgt(guided=True,
                                               exposures=[_plan(count=10)])])
    guided = json.loads(generate_nina_json(g))
    res = lint(guided, guided=False, unguided_dither=True)  # StartGuiding still bad
    assert not res.ok and any("StartGuiding" in f.detail for f in res.findings)


def _guider_armer(tmp_path, monkeypatch, override, name, **cfg):
    a = Armer(_cfg(tmp_path, **cfg))
    a.guiding_override = override
    notes = []

    async def _notify(cfg_, msg, **k):
        notes.append((msg, k.get("priority", 0)))
    monkeypatch.setattr(armer_mod, "notify", _notify)

    async def _nina(key, *x, **k):
        assert key == "guider"
        return None if name is None else {"Success": True, "Response": {
            "Connected": True, "Name": name, "State": "Idle"}}
    monkeypatch.setattr(a, "_nina", _nina)
    return a, notes


async def test_guider_check_at_arm(tmp_path, monkeypatch):
    a, notes = _guider_armer(tmp_path, monkeypatch, "unguided", "Direct Guider",
                             unguided_dither=True)
    await a._check_guider_at_arm()
    assert a._unguided_dither() is True and notes == []
    a, notes = _guider_armer(tmp_path / "b", monkeypatch, "unguided", "PHD2",
                             unguided_dither=True)
    await a._check_guider_at_arm()
    assert a._unguided_dither() is False
    assert len(notes) == 1 and "Unguided dithers are OFF" in notes[0][0]
    a, notes = _guider_armer(tmp_path / "c", monkeypatch, "unguided", None,
                             unguided_dither=True)            # NINA unreachable
    await a._check_guider_at_arm()
    assert a._unguided_dither() is False and "unknown" in notes[0][0]
    a, notes = _guider_armer(tmp_path / "d", monkeypatch, "unguided", "PHD2")
    await a._check_guider_at_arm()                            # flag off: silent
    assert a._unguided_dither() is False and notes == []
    a, notes = _guider_armer(tmp_path / "e", monkeypatch, "guided", "Direct Guider",
                             unguided_dither=True)
    await a._check_guider_at_arm()
    assert a._unguided_dither() is False                      # guided night
    assert len(notes) == 1 and notes[0][1] == 1 and "Direct Guider" in notes[0][0]
    a, notes = _guider_armer(tmp_path / "f", monkeypatch, "guided", "PHD2")
    await a._check_guider_at_arm()
    assert notes == []
    saved = json.loads((tmp_path / "f" / "data" / "armer_state.json").read_text())
    assert saved["guider_name"] == "PHD2"


def test_dispatch_dithers_unguided_only_with_direct_guider(tmp_path, monkeypatch):
    a = _armer_for_dispatch(tmp_path, monkeypatch, [_tgt(exposures=[_plan(count=10)])],
                            unguided_dither=True)
    a.guiding_override = "unguided"
    a.guider_name = "Direct Guider"
    assert a._dispatch() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert set(_dither_afters(seq)) == {5}
    a.guider_name = "PHD2"
    assert a._dispatch() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert set(_dither_afters(seq)) == {0}
