"""PS-132: Piggy-600 OSC flats NINA #2 can validate, a lint rule for flats on
a rig without a filter wheel, and NINA's own validation read after a load.

2026-10-05: the companion's DAWN_SKY_FLATS_OSC SkyFlat had no SwitchFilter
child. NINA's SkyFlat.GetSwitchFilterItem() is Items.First(x is SwitchFilter),
so SkyFlat.Validate threw "Sequence contains no matching element" every 5 s
in NINA #2's log and Start did nothing.
"""
import asyncio
import json
from datetime import datetime

import httpx
import pytest

from photonscript.scheduler import nina_validation as nv
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.calibration import (generate_dusk_flats_json,
                                                generate_piggyback_companion_json)
from photonscript.scheduler.sequence_lint import LintResult, _check_flat_filters
from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig

_SKYFLAT = "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat"
_SWITCH = "NINA.Sequencer.SequenceItem.FilterWheel.SwitchFilter, NINA.Sequencer"


def _run(coro):
    return asyncio.run(coro)


def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from _walk(v)


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              piggyback_enabled=True, **kw)


def _companion(tmp_path, has_safety=True, with_lights=True):
    return json.loads(generate_piggyback_companion_json(
        rigs.rig_config(_cfg(tmp_path), rigs.PIGGYBACK),
        has_safety=has_safety, with_lights=with_lights))


def _skyflats(root):
    return [n for n in _walk(root) if n.get("$type", "").startswith(_SKYFLAT)]


def _strip_switch(root):
    """The pre-PS-132 shape: SkyFlat with only its flats loop."""
    for sf in _skyflats(root):
        sf["Items"]["$values"] = [i for i in sf["Items"]["$values"]
                                  if "SwitchFilter" not in i.get("$type", "")]
    return root


def _errors(res):
    return {f.rule for f in res.findings if f.level == "ERROR"}


# ------------------------------------------------------------- generator

@pytest.mark.parametrize("has_safety,with_lights",
                         [(True, True), (True, False), (False, False)])
def test_companion_osc_skyflat_has_filterless_switch(tmp_path, has_safety,
                                                     with_lights):
    root = _companion(tmp_path, has_safety, with_lights)
    sfs = _skyflats(root)
    assert len(sfs) == 1
    first, loop = sfs[0]["Items"]["$values"]
    assert first["$type"] == _SWITCH and first["Filter"] is None
    assert "Parent" in first and first["Parent"]["$ref"] == sfs[0]["$id"]
    take = loop["Items"]["$values"][0]
    assert take["ImageType"] == "FLAT"
    # NINA's own auto exposure, inside the PS-113 flat QA band (20..80 %)
    assert 0.2 < sfs[0]["HistogramTargetPercentage"] < 0.8
    assert not sfs[0]["ShouldDither"]   # no mount on NINA #2
    res = sd.lint_companion(root)
    assert res.ok, [f.detail for f in res.findings]


def test_dusk_osc_flats_have_filterless_switch(tmp_path):
    pc = rigs.rig_config(_cfg(tmp_path), rigs.PIGGYBACK)
    txt, _ = generate_dusk_flats_json(pc, osc=True, owns_mount=False)
    root = json.loads(txt)
    sw = [n for n in _walk(root) if "SwitchFilter" in n.get("$type", "")]
    assert len(sw) == 1 and sw[0]["Filter"] is None
    assert not _errors(sd.lint_companion(root)) & {"flat-filter",
                                                   "no-filter-wheel"}


def test_rc16_sky_flats_keep_their_filters(tmp_path):
    txt, _ = generate_dusk_flats_json(PhotonScriptConfig(_env_file=None),
                                      osc=False, owns_mount=True)
    root = json.loads(txt)
    sfs = _skyflats(root)
    assert len(sfs) == 7
    for sf in sfs:
        first = sf["Items"]["$values"][0]
        assert first["$type"] == _SWITCH and first["Filter"]["_name"]
    r = LintResult()
    _check_flat_filters(root, r, filter_wheel=True)
    assert r.ok


# ------------------------------------------------------------------ lint

def test_rig_filter_wheel_registry():
    assert rigs.rig_has_filter_wheel(rigs.RC16)
    assert not rigs.rig_has_filter_wheel(rigs.PIGGYBACK)


def test_lint_catches_the_2026_10_05_companion(tmp_path):
    root = _strip_switch(_companion(tmp_path))
    res = sd.lint_companion(root)
    assert not res.ok and "flat-filter" in _errors(res)
    assert any("Sequence contains no matching element" in f.detail
               for f in res.findings)


def test_lint_flat_filter_applies_to_any_rig(tmp_path):
    txt, _ = generate_dusk_flats_json(PhotonScriptConfig(_env_file=None),
                                      osc=False, owns_mount=True)
    root = _strip_switch(json.loads(txt))
    r = LintResult()
    _check_flat_filters(root, r, filter_wheel=True)
    assert "flat-filter" in _errors(r)
    assert len([f for f in r.findings if f.rule == "flat-filter"]) == 7


def test_lint_no_filter_wheel_rejects_a_selected_filter(tmp_path):
    from photonscript.scheduler.nina_sequence_json import _filter_info
    from photonscript.shared.models import FilterType
    root = _companion(tmp_path)
    for n in _walk(root):
        if "SwitchFilter" in n.get("$type", ""):
            n["Filter"] = _filter_info(FilterType.LUMINANCE)
    res = sd.lint_companion(root)
    assert "no-filter-wheel" in _errors(res)
    # the same sequence is fine for a rig with a wheel
    assert "no-filter-wheel" not in _errors(sd.lint_companion(root,
                                                              filter_wheel=True))


def test_rc16_night_lint_runs_the_flat_rule(tmp_path):
    from photonscript.scheduler import nina_sequence_json as nsj
    from photonscript.scheduler.sequence_lint import lint
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.shared.models import (ExposurePlan, FilterType,
                                            NinaSequenceTarget)
    s = build_sequence_for_night("PhotonScript_20261005", [NinaSequenceTarget(
        name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])])
    root = json.loads(nsj.generate_nina_json(s))
    if not _skyflats(root):
        pytest.skip("tonight's sequence has no dawn flats in this config")
    assert "flat-filter" not in _errors(lint(root))
    assert "flat-filter" in _errors(lint(_strip_switch(root)))


# --------------------------------------------------- validation: parsing

# NINA #2's log on 2026-10-05 (shape of the real lines; paths shortened)
LOG = """2026-10-05T20:59:58.1000|INFO|SequenceNavigationVM.cs|LoadSequence|88|Loaded sequence
2026-10-05T21:02:14.5168|ERROR|SequenceRootContainer.cs|Validate|212|System.InvalidOperationException: Sequence contains no matching element
   at System.Linq.ThrowHelper.ThrowNoMatchException()
   at NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat.GetSwitchFilterItem() in C:\\nina\\SkyFlat.cs:line 186
   at NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat.Validate() in C:\\nina\\SkyFlat.cs:line 504
2026-10-05T21:02:19.5170|ERROR|SequenceRootContainer.cs|Validate|212|System.InvalidOperationException: Sequence contains no matching element
   at NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat.GetSwitchFilterItem() in C:\\nina\\SkyFlat.cs:line 186
   at NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat.Validate() in C:\\nina\\SkyFlat.cs:line 504
2026-10-05T21:02:20.0000|ERROR|CameraVM.cs|Capture|50|Camera timeout
2026-10-05T21:02:21.0000|INFO|Foo.cs|Bar|1|Validate() mentioned at INFO only
"""


def test_validation_errors_from_the_real_log_shape():
    errs = nv.validation_errors(LOG)
    assert errs == ["System.InvalidOperationException: Sequence contains no "
                    "matching element (in SkyFlat.GetSwitchFilterItem)"]


def test_validation_errors_since_the_load():
    assert nv.validation_errors(LOG, since=datetime(2026, 10, 5, 21, 3)) == []
    assert len(nv.validation_errors(LOG, since=datetime(2026, 10, 5, 21, 2, 15))) == 1


def test_collect_issues_from_ninaapi_state():
    state = [{"Name": "Start_Container", "Status": "CREATED", "Issues": [],
              "Items": [{"Name": "Cool Camera", "Status": "CREATED",
                         "Issues": ["Camera not connected"]}]},
             {"Name": "Targets_Container", "Status": "CREATED", "Items": [
                 {"Name": "Sky flats OSC_Container", "Status": "CREATED",
                  "Issues": ["Filter wheel not connected"],
                  "Conditions": [{"Name": "Loop", "Issues": None}]},
                 {"Name": "Cool Camera", "Issues": ["Camera not connected"]}]}]
    assert nv.collect_issues(state) == ["Cool Camera: Camera not connected",
                                        "Sky flats OSC: Filter wheel not connected"]
    assert nv.collect_issues([]) == [] and nv.collect_issues(None) == []


def test_mode_and_settle_defaults(tmp_path):
    c = _cfg(tmp_path)
    assert nv.mode(c) == "alert"
    assert nv.mode(_cfg(tmp_path, nina_load_validation="REFUSE")) == "refuse"
    assert nv.mode(_cfg(tmp_path, nina_load_validation="bogus")) == "alert"
    assert PhotonScriptConfig.model_fields["nina_load_validation_settle_s"].default == 8.0


def test_new_keys_on_the_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    keys = {f[0] for f in _CONFIG_FIELDS}
    assert {"nina_load_validation", "nina_load_validation_settle_s"} <= keys


# ------------------------------------------- validation: fake ninaAPI

class FakeNina:
    def __init__(self, issues=None):
        self.calls = []
        self.issues = issues or []

    def handler(self, request):
        path = request.url.path
        self.calls.append(path.rsplit("/api", 1)[-1])
        if path.endswith("/sequence/state"):
            return httpx.Response(200, json={"Success": True, "Response": [
                {"Name": "Sky flats OSC_Container", "Status": "CREATED",
                 "Issues": self.issues}]})
        return httpx.Response(200, json={"Success": True, "Response": "ok"})

    def install(self, monkeypatch, *modules):
        real = httpx.AsyncClient

        def factory(*a, **kw):
            kw["transport"] = httpx.MockTransport(self.handler)
            return real(*a, **kw)
        for m in modules:
            monkeypatch.setattr(m.httpx, "AsyncClient", factory)
        return self


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def fake_notify(config, message, title="PhotonScript", priority=0,
                          sound="none"):
        sent.append({"msg": message, "title": title, "priority": priority})
        return True
    from photonscript.shared import pushover
    monkeypatch.setattr(pushover, "notify", fake_notify)
    return sent


def _log_reader(errors):
    seen = {}

    def reader(config, rig, since):
        seen.update(rig=rig, since=since)
        return list(errors), ""
    reader.seen = seen
    return reader


def test_check_loaded_reads_state_and_log(tmp_path):
    fake = FakeNina(issues=["Filter wheel not connected"])
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    slept = []

    async def sleep(s):
        slept.append(s)
    reader = _log_reader(["boom (in SkyFlat.GetSwitchFilterItem)"])
    t0 = datetime(2026, 10, 5, 21, 2)
    res = _run(nv.check_loaded("http://n2/v2/api", _cfg(tmp_path), "piggyback",
                               since=t0, client=client, settle=3.0,
                               sleep=sleep, log_reader=reader))
    assert slept == [3.0] and fake.calls == ["/sequence/state"]
    assert reader.seen == {"rig": "piggyback", "since": t0}
    assert not res["ok"]
    assert res["issues"] == ["Sky flats OSC: Filter wheel not connected"]
    assert res["errors"] == ["boom (in SkyFlat.GetSwitchFilterItem)"]
    assert "1 validation error(s)" in res["detail"]


def test_read_log_errors_uses_the_rig_log(tmp_path, monkeypatch):
    from photonscript.scheduler.routers import triage
    p = tmp_path / "nina2.log"
    p.write_text(LOG, encoding="utf-8")
    monkeypatch.setattr(triage, "_rig_log", lambda cfg, rig: (str(p), ""))
    errs, note = nv.read_log_errors(_cfg(tmp_path), "piggyback",
                                    datetime(2026, 10, 5, 21, 0))
    assert note == "" and len(errs) == 1
    monkeypatch.setattr(triage, "_rig_log", lambda cfg, rig: (None, "no log"))
    assert nv.read_log_errors(_cfg(tmp_path), "piggyback", None) == ([], "no log")


@pytest.mark.parametrize("vmode,errors,started,ok", [
    ("alert", ["boom"], True, True),
    ("refuse", ["boom"], False, False),
    ("refuse", [], True, True),          # Issues alone never block a dispatch
    ("off", ["boom"], True, True),
])
def test_dispatch_validates_between_load_and_start(tmp_path, monkeypatch, pushes,
                                                   vmode, errors, started, ok):
    fake = FakeNina(issues=["Camera not connected"]).install(monkeypatch, rigs, sd)
    monkeypatch.setattr(nv, "read_log_errors", _log_reader(errors))
    cfg = _cfg(tmp_path, nina_load_validation=vmode)
    res = _run(rigs.nina_dispatch("http://n2/v2/api", {"$type": "x"},
                                  config=cfg, rig="piggyback"))
    assert res["ok"] is ok
    want = ["/sequence/stop", "/sequence/load"]
    if vmode != "off":
        want.append("/sequence/state")
    if started:
        want.append("/sequence/start")
    assert fake.calls == want
    if vmode == "off":
        assert pushes == [] and "validation" not in res
    else:
        assert len(pushes) == 1 and pushes[0]["title"] == "PhotonScript NINA validation"
        assert "Camera not connected" in res["detail"]
        assert pushes[0]["priority"] == (1 if errors else 0)
    if not started:
        assert "NOT started" in res["detail"]


def test_dispatch_without_config_is_unchanged(monkeypatch, pushes):
    fake = FakeNina().install(monkeypatch, rigs, sd)
    res = _run(rigs.nina_dispatch("http://n2/v2/api", {"$type": "x"}))
    assert res == {"ok": True, "detail": "loaded + started"}
    assert fake.calls == ["/sequence/stop", "/sequence/load", "/sequence/start"]


def test_dispatch_clean_validation_says_nothing(tmp_path, monkeypatch, pushes):
    FakeNina().install(monkeypatch, rigs, sd)
    monkeypatch.setattr(nv, "read_log_errors", _log_reader([]))
    res = _run(rigs.nina_dispatch("http://n2/v2/api", {"$type": "x"},
                                  config=_cfg(tmp_path), rig="piggyback"))
    assert res == {"ok": True, "detail": "loaded + started"} and pushes == []


# ---------------------------------------------------------- sideload

@pytest.fixture
def wired(tmp_path, monkeypatch, pushes):
    from photonscript.scheduler import app
    from photonscript.scheduler import nina_sequence_json as nsj
    cfg = _cfg(tmp_path, nina_base_url="http://nina1:1888/v2/api",
               piggyback_nina_base_url="http://nina2:1889/v2/api")
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.chdir(tmp_path)

    class _Armer:
        state = "DISARMED"
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "get_armer", lambda: _Armer())
    fake = FakeNina()

    def handler(request):
        if request.url.path.endswith("/equipment/safetymonitor/info"):
            return httpx.Response(200, json={"Success": True,
                                             "Response": {"Connected": True}})
        if request.url.path.endswith("/sequence/state") and \
                not any(c.endswith("/sequence/load") for c in fake.calls):
            fake.calls.append("/sequence/state")
            return httpx.Response(200, json={"Success": True, "Response": []})
        return fake.handler(request)
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)
    monkeypatch.setattr(sd.httpx, "AsyncClient", factory)
    return {"cfg": cfg, "nina": fake, "tmp": tmp_path, "pushes": pushes}


def _post_piggyback():
    from photonscript.scheduler.routers import sideload as rt
    r = _run(rt.api_sideload(rig="piggyback", recipe="tracking_test_then_tonight",
                             exclude=[], at="", payload=None))
    if isinstance(r, dict):
        return 200, r
    return r.status_code, json.loads(r.body)


def test_sideload_validate_error_is_422_and_pushed(wired, monkeypatch):
    monkeypatch.setattr(nv, "read_log_errors", _log_reader(
        ["System.InvalidOperationException: Sequence contains no matching "
         "element (in SkyFlat.GetSwitchFilterItem)"]))
    code, b = _post_piggyback()
    assert code == 422 and b["ok"] is False and b["started"] is False
    assert "REJECTS" in b["detail"] and "will do nothing" in b["detail"]
    assert b["validation"]["errors"]
    assert wired["nina"].calls[-1] == "/sequence/state"
    assert len(wired["pushes"]) == 1 and wired["pushes"][0]["priority"] == 1
    ev = next((wired["tmp"] / "runs").glob("*_events.jsonl")).read_text()
    assert '"ok": false' in ev


def test_sideload_issues_only_loads_ok_with_a_note(wired, monkeypatch):
    wired["nina"].issues = ["Camera not connected"]
    monkeypatch.setattr(nv, "read_log_errors", _log_reader([]))
    code, b = _post_piggyback()
    assert code == 200 and b["ok"] is True
    assert "NINA flags" in b["detail"] and "Camera not connected" in b["detail"]
    assert len(wired["pushes"]) == 1 and wired["pushes"][0]["priority"] == 0


def test_sideload_validation_off(wired, monkeypatch):
    wired["cfg"].nina_load_validation = "off"
    monkeypatch.setattr(nv, "read_log_errors", _log_reader(["boom"]))
    code, b = _post_piggyback()
    assert code == 200 and b["validation"] is None
    assert wired["nina"].calls == ["/sequence/state", "/sequence/load"]


# ------------------------------------------------------------- armer

@pytest.fixture
def armer_env(tmp_path, monkeypatch):
    from photonscript.scheduler import armer as armer_mod
    from photonscript.scheduler import calibration, preflight
    cfg = _cfg(tmp_path)
    a = armer_mod.Armer(cfg)
    a.sequence_path = tmp_path / "sequences" / "tonight.json"
    notes, dispatched = [], []

    async def fake_notify(config, message, title="PhotonScript", priority=0,
                          sound="none"):
        notes.append(message)
        return True

    async def fake_connected(c, dev, *a, **k):
        return True, {}, ""

    async def fake_dispatch(base, seq, config=None, rig=None):
        dispatched.append({"base": base, "rig": rig, "config": config})
        return {"ok": True, "detail": "loaded + started"}
    monkeypatch.setattr(armer_mod, "notify", fake_notify)
    monkeypatch.setattr(preflight, "_ensure_connected", fake_connected)
    monkeypatch.setattr(rigs, "nina_dispatch", fake_dispatch)
    real = calibration.generate_piggyback_companion_json
    return {"armer": a, "notes": notes, "dispatched": dispatched,
            "cal": calibration, "real": real, "cfg": cfg}


def test_armer_dispatches_a_lint_clean_companion_with_validation(armer_env):
    _run(armer_env["armer"]._dispatch_piggyback_companion())
    d = armer_env["dispatched"]
    assert len(d) == 1 and d[0]["rig"] == "piggyback"
    assert d[0]["config"] is armer_env["cfg"]


def test_armer_refuses_a_companion_that_fails_lint(armer_env, monkeypatch):
    real = armer_env["real"]

    def broken(cfg, has_safety=False, with_lights=False):
        return json.dumps(_strip_switch(json.loads(
            real(cfg, has_safety=has_safety, with_lights=with_lights))))
    monkeypatch.setattr(armer_env["cal"], "generate_piggyback_companion_json",
                        broken)
    _run(armer_env["armer"]._dispatch_piggyback_companion())
    assert armer_env["dispatched"] == []
    assert any("NOT started" in n and "flat-filter" in n
               for n in armer_env["notes"])


def test_ascii_sources():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for p in ("photonscript/scheduler/nina_validation.py",
              "photonscript/scheduler/routers/sideload.py",
              "photonscript/scheduler/sideload.py",
              "tests/test_scheduler/test_ps132_osc_flats.py"):
        assert (root / p).read_bytes().isascii(), p
