"""PS-123: sideload a hand-built night into NINA #1 / NINA #2 (load only).

The splice (tracking test first, then tonight's targets minus the excluded,
fresh $id / Parent links), the companion lint, the running check over the
ninaAPI sequence tree, and the endpoints' refusals (lint, armer, NINA
running or unreadable, NINA #2 without its safety monitor) and the load-only
path (never /sequence/stop, never /sequence/start).
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.routers import sideload as rt
from photonscript.scheduler.sequence_lint import _check_parent_links, LintResult, lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

ROOT = Path(__file__).resolve().parents[2]
NAMES = (("Heart Nebula", 2.55, 61.5), ("Cat's Eye Nebula", 17.98, 66.6),
         ("Pacman Nebula", 0.88, 56.6))


def _cfg(tmp_path, **kw):
    kw.setdefault("quality_eccentricity_max", 0.60)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


@pytest.fixture(autouse=True)
def _pinned(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.chdir(tmp_path)
    return cfg


def _night(guided=False, n=3):
    targets = [NinaSequenceTarget(
        name=name, ra_hours=ra, dec_degrees=dec, start_guiding=guided,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])
        for name, ra, dec in NAMES[:n]]
    s = build_sequence_for_night("PhotonScript_20261005", targets)
    s.wait_until_local = "00:00:00"
    return s, json.loads(nsj.generate_nina_json(s))


def _tt():
    return json.loads(nsj.generate_tracking_test_json("M 2", 21.56, -0.82))


def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from _walk(v)


def _run(coro):
    return asyncio.run(coro)


def _body(res):
    if isinstance(res, dict):
        return 200, res
    return res.status_code, json.loads(res.body)


# ------------------------------------------------------------------ splice

class TestSplice:
    def test_order_excluded_removed_and_names(self):
        _, night = _night()
        out = sd.splice_tracking_test(night, _tt(), ["  cat's eye   nebula "],
                                      name="PS_TT")
        assert out["Name"] == "PS_TT"
        assert sd.target_names(out) == ["Tracking test M 2", "Heart Nebula",
                                        "Pacman Nebula"]

    def test_ids_unique_and_parents_link(self):
        _, night = _night()
        out = sd.splice_tracking_test(night, _tt(), ["Cat's Eye Nebula"])
        ids = [d["$id"] for d in _walk(out) if "$id" in d]
        assert len(ids) == len(set(ids)) and next(iter(out)) == "$id"
        r = LintResult()
        _check_parent_links(out, r)
        assert r.ok, r.findings
        tc = sd.find_container(out, sd.TARGETS_CONTAINER, "SequentialContainer")
        for ch in tc["Items"]["$values"]:
            assert ch["Parent"] == {"$ref": tc["$id"]}

    def test_inputs_untouched_and_rest_is_tonights(self):
        _, night = _night()
        tt = _tt()
        before = json.dumps(night), json.dumps(tt)
        out = sd.splice_tracking_test(night, tt, [])
        assert (json.dumps(night), json.dumps(tt)) == before
        # tonight's start / loop / shutdown; the test's park-and-hold is not taken
        assert [c["name"] for c in sd.container_tree(out, 1)] == \
            [c["name"] for c in sd.container_tree(night, 1)]
        sl = sd.find_container(out, "SAFE_LOOP", "SequentialContainer")
        tsl = sd.find_container(tt, "SAFE_LOOP", "SequentialContainer")
        types = lambda c: [sd._short(i["$type"]) for i in c["Items"]["$values"]]  # noqa: E731
        assert "WaitForTime" in types(tsl) and types(sl) == types(
            sd.find_container(night, "SAFE_LOOP", "SequentialContainer"))

    @pytest.mark.parametrize("guided", [False, True])
    def test_lint_passes_with_only_the_tracking_test_note(self, guided):
        _, night = _night(guided=guided)
        out = sd.splice_tracking_test(night, _tt(), ["Cat's Eye Nebula"])
        res = lint(out, guided=guided)
        assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]
        assert {f.rule for f in res.findings} <= {"tracking-test", "autofocus",
                                                   "altitude", "reacquire",
                                                   "night-loop", "guiding"}

    def test_refuses_bad_shapes(self):
        _, night = _night()
        with pytest.raises(sd.SpliceError):
            sd.splice_tracking_test({"Name": "x", "$type": "y"}, _tt())
        with pytest.raises(sd.SpliceError, match="exactly 1"):
            sd.splice_tracking_test(night, night)   # 3 targets in "the test"
        bad = json.loads(json.dumps(night))
        bad["Extra"] = {"$ref": "5"}
        with pytest.raises(sd.SpliceError, match="outside Parent"):
            sd.splice_tracking_test(bad, _tt())

    def test_splice_name(self):
        assert sd.splice_name("20261005", "M 2", ["Heart Nebula"]) == \
            "PhotonScript_20261005_TT_M_2_then_Heart_Nebula"
        assert sd.splice_name("2026-10-05", "M 2", ["A", "B"]).endswith("_then_tonight")


# --------------------------------------------------------- companion lint

def _companion(tmp_path, has_safety=True, with_lights=True):
    from photonscript.scheduler.calibration import generate_piggyback_companion_json
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    return json.loads(generate_piggyback_companion_json(
        rig_config(cfg, PIGGYBACK), has_safety=has_safety, with_lights=with_lights))


class TestCompanionLint:
    def test_real_companion_passes(self, tmp_path):
        res = sd.lint_companion(_companion(tmp_path))
        assert res.ok, [f.detail for f in res.findings]

    def test_mount_moves_and_warm_setpoint_fail(self, tmp_path):
        seq = _companion(tmp_path)
        seq["Items"]["$values"][0]["Items"]["$values"].append(
            {"$type": "NINA.Sequencer.SequenceItem.Telescope.ParkScope, NINA.Sequencer"})
        for d in _walk(seq):
            if "CoolCamera" in d.get("$type", ""):
                d["Temperature"] = 5.0
        rules = {f.rule for f in sd.lint_companion(seq).findings if f.level == "ERROR"}
        assert {"companion-mount", "cooling"} <= rules

    def test_night_lint_would_wrongly_fail_it(self, tmp_path):
        # why the companion has its own lint
        assert not lint(_companion(tmp_path)).ok


# ---------------------------------------------------------- running check

@pytest.mark.parametrize("state,want", [
    ({"Success": True, "Response": [{"Name": "Start", "Status": "FINISHED",
                                     "Items": [{"Name": "Cool", "Status": "FINISHED"}]},
                                    {"Name": "Targets", "Status": "CREATED"}]}, []),
    ({"Response": [{"Name": "Targets", "Status": "RUNNING", "Items": [
        {"Name": "SAFE_LOOP_Container", "Status": "running"}]}]},
     ["Targets", "SAFE_LOOP_Container"]),
    ({"Items": {"$values": [{"Name": "x", "Status": "RUNNING"}]}}, ["x"]),
    ([], []), (None, []),
])
def test_nina_running(state, want):
    assert sd.nina_running(state) == want


# ------------------------------------------------------------ fake ninaAPI

class FakeNina:
    def __init__(self, state=None, state_code=200, safety=True, load_ok=True):
        self.calls = []
        self.state = state if state is not None else {"Success": True, "Response": []}
        self.state_code = state_code
        self.safety = safety
        self.load_ok = load_ok
        self.loaded = None

    def handler(self, request: httpx.Request):
        path = request.url.path
        self.calls.append((request.method, request.url.host, request.url.port, path))
        if path.endswith("/sequence/state"):
            return httpx.Response(self.state_code, json=self.state)
        if path.endswith("/sequence/load"):
            self.loaded = json.loads(request.content)
            return httpx.Response(200, json={"Success": self.load_ok, "Response": "ok",
                                             "Error": "" if self.load_ok else "bad"})
        if path.endswith("/equipment/safetymonitor/info"):
            return httpx.Response(200, json={"Success": True,
                                             "Response": {"Connected": self.safety}})
        return httpx.Response(200, json={"Success": True, "Response": "ok"})

    def install(self, monkeypatch):
        real = httpx.AsyncClient

        def factory(*a, **kw):
            kw["transport"] = httpx.MockTransport(self.handler)
            return real(*a, **kw)
        monkeypatch.setattr(sd.httpx, "AsyncClient", factory)
        return self


class _Armer:
    def __init__(self, state="DISARMED"):
        self.state = state

    def _use_guiding(self):
        return False

    def _unguided_dither(self):
        return False


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The app pinned to a two-rig config, tonight = 3 fixed targets and a
    fixed tracking-test field, armer DISARMED, fake ninaAPI on both rigs."""
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path, piggyback_enabled=True,
               nina_base_url="http://nina1:1888/v2/api",
               piggyback_nina_base_url="http://nina2:1889/v2/api")
    s, night = _night()
    armer = _Armer()
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "get_armer", lambda: armer)
    monkeypatch.setattr(app, "_tonight_sequence", lambda now_mode=False: (
        "PhotonScript_20261005", json.dumps(night), False, False))
    monkeypatch.setattr(app, "_tracking_test_field", lambda *a, **k: {
        "name": "M 2", "ra_hours": 21.56, "dec_degrees": -0.82, "source": "test"})
    nina = FakeNina().install(monkeypatch)
    return {"cfg": cfg, "armer": armer, "nina": nina, "tmp": tmp_path}


def _post(**kw):
    kw.setdefault("rig", "rc16")
    kw.setdefault("recipe", "tracking_test_then_tonight")
    kw.setdefault("exclude", ["Cat's Eye Nebula"])
    kw.setdefault("at", "")
    kw.setdefault("payload", None)
    return _body(_run(rt.api_sideload(**kw)))


# -------------------------------------------------------------- endpoints

class TestPreview:
    def test_both_rigs_lint_and_tree(self, wired):
        code, b = _body(_run(rt.api_sideload_preview(
            recipe="tracking_test_then_tonight", exclude=["Cat's Eye Nebula"], at="")))
        assert code == 200 and set(b["rigs"]) == {"rc16", "piggyback"}
        rc = b["rigs"]["rc16"]
        assert rc["lint"]["ok"] and rc["targets"] == [
            "Tracking test M 2", "Heart Nebula", "Pacman Nebula"]
        assert rc["excluded"] == ["Cat's Eye Nebula"]
        assert rc["name"] == "PhotonScript_20261005_TT_M_2_then_tonight"
        assert any(n["name"] == sd.TARGETS_CONTAINER for n in rc["tree"])
        pb = b["rigs"]["piggyback"]
        assert pb["lint"]["ok"] and pb["has_safety"] and pb["with_lights"]
        assert wired["nina"].calls == []          # preview never talks to NINA

    def test_unknown_recipe(self, wired):
        code, _ = _body(_run(rt.api_sideload_preview(recipe="x", exclude=[], at="")))
        assert code == 400


class TestLoad:
    def test_rc16_loads_only_saves_copy_and_logs(self, wired):
        code, b = _post()
        assert code == 200 and b["ok"] and b["started"] is False
        calls = [c[3] for c in wired["nina"].calls]
        # PS-132: the state is read again after the load (NINA's validation)
        assert calls == ["/v2/api/sequence/state", "/v2/api/sequence/load",
                         "/v2/api/sequence/state"]
        assert b["validation"]["ok"]
        assert all(c[1] == "nina1" for c in wired["nina"].calls)
        assert not any("start" in c or "stop" in c for c in calls)
        assert sd.target_names(wired["nina"].loaded) == [
            "Tracking test M 2", "Heart Nebula", "Pacman Nebula"]
        saved = Path(b["file"])
        assert saved.parent == wired["tmp"] / "sequences"
        assert saved.name.startswith("Sideload_rc16_PhotonScript_20261005_TT_M_2")
        assert json.loads(saved.read_text(encoding="utf-8")) == wired["nina"].loaded
        ev = [json.loads(x) for x in
              next((wired["tmp"] / "runs").glob("*_events.jsonl")).read_text().splitlines()]
        assert ev[-1]["kind"] == "sideload" and ev[-1]["rig"] == "rc16" and ev[-1]["ok"]
        audit = (wired["tmp"] / "notifications.jsonl")
        if audit.exists():
            assert "sideload" in audit.read_text(encoding="utf-8")

    def test_piggyback_companion_with_safety_and_lights(self, wired, monkeypatch):
        from photonscript.scheduler import calibration
        seen = {}
        real = calibration.generate_piggyback_companion_json

        def spy(cfg, has_safety=False, with_lights=False):
            seen.update(has_safety=has_safety, with_lights=with_lights,
                        nina=cfg.nina_base_url)
            return real(cfg, has_safety=has_safety, with_lights=with_lights)
        monkeypatch.setattr(calibration, "generate_piggyback_companion_json", spy)
        code, b = _post(rig="piggyback")
        assert code == 200 and b["ok"]
        assert seen == {"has_safety": True, "with_lights": True,
                        "nina": "http://nina2:1889/v2/api"}
        calls = [c[3] for c in wired["nina"].calls]
        assert calls == ["/v2/api/sequence/state",
                         "/v2/api/equipment/safetymonitor/info",
                         "/v2/api/sequence/load", "/v2/api/sequence/state"]
        assert all(c[1] == "nina2" and c[2] == 1889 for c in wired["nina"].calls)

    def test_piggyback_refused_without_safety_monitor(self, wired):
        wired["nina"].safety = False
        code, b = _post(rig="piggyback")
        assert code == 409 and "safety monitor" in b["detail"]
        assert wired["nina"].loaded is None

    @pytest.mark.parametrize("state", ["ARMED", "RUNNING", "PAUSED_UNSAFE"])
    def test_refused_while_armed(self, wired, state):
        wired["armer"].state = state
        code, b = _post()
        assert code == 409 and state in b["detail"]
        assert wired["nina"].calls == []

    def test_refused_while_nina_runs(self, wired):
        wired["nina"].state = {"Success": True, "Response": [
            {"Name": "Targets", "Status": "RUNNING", "Items": []}]}
        code, b = _post()
        assert code == 409 and "Targets" in b["detail"]
        assert wired["nina"].loaded is None

    def test_refused_when_nina_unreadable(self, wired):
        wired["nina"].state_code = 500
        code, b = _post()
        assert code == 502 and wired["nina"].loaded is None

    def test_lint_failure_refused(self, wired):
        bad = {"$type": "NINA.Sequencer.Container.SequenceRootContainer, NINA.Sequencer",
               "Name": "bad", "Items": {"$values": []}}
        code, b = _post(recipe="", payload=bad)
        assert code == 422 and not b["lint"]["ok"]
        assert wired["nina"].calls == []

    def test_json_body_is_linted_and_loaded(self, wired):
        _, night = _night()
        code, b = _post(recipe="", payload={"sequence": night})
        assert code == 200 and wired["nina"].loaded == night

    def test_load_failure_is_502_and_logged(self, wired):
        wired["nina"].load_ok = False
        code, b = _post()
        assert code == 502 and not b["ok"] and "bad" in b["detail"]
        ev = next((wired["tmp"] / "runs").glob("*_events.jsonl")).read_text()
        assert '"ok": false' in ev

    def test_unknown_rig_and_missing_recipe(self, wired):
        assert _post(rig="nope")[0] == 404
        assert _post(recipe="")[0] == 400


def test_state_falls_back_to_sequence_json(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/sequence/state"):
            return httpx.Response(404)
        return httpx.Response(200, json={"Success": True, "Response": [
            {"Name": "x", "Status": "RUNNING"}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tree, err = _run(sd.read_sequence_state("http://n/v2/api", client=client))
    assert err is None and sd.nina_running(tree) == ["x"]


def test_tonight_sequence_helper_matches_download(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    s, _ = _night()
    monkeypatch.setattr(app, "get_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(app, "get_armer", lambda: _Armer())
    monkeypatch.setattr(app, "_projects", {})
    monkeypatch.setattr(app, "_store", None)   # PS-126: readers load the store
    monkeypatch.setattr(app, "rank_targets_for_night", lambda *a, **k: [])
    monkeypatch.setattr(app, "plan_night_sequence", lambda *a, **k: s.targets)
    name, text, guided, dither = app._tonight_sequence(False)
    assert name.startswith("PhotonScript_") and guided is False and dither is False
    res = _run(app.api_tonight_sequence_json(False))
    assert res.status_code == 200 and json.loads(res.body)["Name"] == name


def test_dashboard_panel_and_ascii():
    dash = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'id="sideloadBox"' in dash and "sideload_panel.js" in dash
    js = (ROOT / "photonscript/scheduler/static/js/sideload_panel.js").read_bytes()
    assert js.isascii() and b"/api/sequence/sideload" in js and b"confirm(" in js
    for p in ("photonscript/scheduler/sideload.py",
              "photonscript/scheduler/routers/sideload.py"):
        assert (ROOT / p).read_bytes().isascii()
