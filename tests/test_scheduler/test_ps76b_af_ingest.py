"""PS-76 part 2: the RC16 / Piggy-600 focus lookup table actually fills and
is used. Real-shaped NINA AF report fixtures (tests/.../fixtures/nina_af):
the 2026-10-05 22:22 RC16 L run (seed ~6045, best 5760 at 23.29 C, 7-point
sweep 5640..6200), a Piggy-600 run (Filter null, EAF ~11030), a starved Ha
run (R^2 0.4) and a half-written file."""
import asyncio
import json
import math
import random
import shutil
from datetime import date
from pathlib import Path

import pytest

from photonscript.scheduler import focus_model as fm
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "nina_af"


def _cfg(tmp_path, **kw):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(data), **kw)


def _reports_dir(tmp_path, bom=False):
    d = tmp_path / "AutoFocus"
    d.mkdir(exist_ok=True)
    for f in FIX.glob("*.json"):
        shutil.copy(f, d / f.name)
    if bom:   # a UTF-8 BOM copy of the L run, one hour later
        rep = json.loads((FIX / "2026-10-05--22-22-59.json").read_text())
        rep["Timestamp"] = "2026-10-05T23:22:59.0000000-06:00"
        rep["Temperature"] = 22.29
        rep["CalculatedFocusPoint"]["Position"] = 5701
        (d / "bom.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(rep).encode())
    return d


def _synthetic(n=60, slope=45.0, noise=8.0, seed=1, step=90):
    random.seed(seed)
    off = {"L": 0, "R": -10, "G": -5, "B": 15, "Ha": -190, "OIII": -170,
           "SII": -200}
    pts = []
    for i in range(n):
        f = list(off)[i % len(off)]
        t = random.uniform(5, 28)
        pts.append({"filter": f, "temp": round(t, 2), "rig": "rc16",
                    "time": f"2026-09-{1 + i // 7:02d}T2{i % 4}:00:00",
                    "step": step,
                    "position": round(5000 + off[f] + slope * t
                                      + random.gauss(0, noise))})
    return pts


def _store(cfg, pts):
    (Path(cfg.data_dir) / fm.POINTS_FILE).write_text(json.dumps(pts))


# ------------------------------------------------------------ ingest

class TestIngestRealReports:
    def test_fixture_folder(self, tmp_path):
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(
            _reports_dir(tmp_path, bom=True)))
        res = fm.ingest_af_reports(cfg, trigger="cli")
        assert res["files"] == 5 and res["parse_errors"] == 1
        assert "2026-10-06--01-12-44.json" in res["bad_files"][0]
        assert res["read"] == 4
        assert res["added"] == 2             # L 22:22 + the BOM L 23:22
        assert res["rejected"] == 1          # the starved Ha run
        assert res["reject_reasons"] == {"rc16: R^2 below af_min_r2": 1}
        assert res["piggyback"]["added"] == 1
        pts = fm.load_points(cfg)
        first = next(p for p in pts if p["time"].startswith("2026-10-05T22:22"))
        assert first["filter"] == "L" and first["position"] == 5760
        assert first["temp"] == 23.29 and first["initial"] == 6045
        assert first["step"] == 93 and first["points"] == 7
        assert first["duration_s"] == pytest.approx(221.22, abs=0.01)
        pb = fm.load_points(cfg, "piggyback")
        assert pb[0]["position"] == 11032 and pb[0]["filter"] == "OSC"
        # status saved for /api/focus, with the trigger
        st = fm.load_ingest_status(cfg)
        assert st["trigger"] == "cli" and st["added"] == 2 and st["at"]

    def test_reingest_is_idempotent(self, tmp_path):
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(
            _reports_dir(tmp_path)))
        fm.ingest_af_reports(cfg)
        again = fm.ingest_af_reports(cfg)
        assert again["added"] == 0 and again["total"] == 1

    def test_missing_folder_is_explained(self, tmp_path):
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(tmp_path / "nope"))
        res = fm.ingest_af_reports(cfg)
        assert res["enabled"] and res["dir_exists"] is False
        assert res["files"] == 0

    def test_unset_dir_is_a_noop_and_writes_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert fm.ingest_af_reports(cfg)["enabled"] is False
        assert fm.load_ingest_status(cfg) is None


class TestCameraNameHints:
    def _rep(self, **kw):
        rep = json.loads((FIX / "2026-10-05--22-22-59.json").read_text())
        rep.update(kw)
        return rep

    def test_hint_beats_the_range_rule(self, tmp_path):
        cfg = _cfg(tmp_path)
        # an L-named report in the RC16 EAF range, but the Piggy camera named
        assert fm.classify_report(self._rep(Camera="OGMA AP26CC"), cfg) \
            == "piggyback"
        assert fm.classify_report(self._rep(Camera="AP26MC"), cfg) == "rc16"

    def test_no_hint_falls_back_to_the_rule(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert fm.classify_report(self._rep(), cfg) == "rc16"
        assert fm.classify_report(self._rep(Camera="AP26MC AP26CC"), cfg) \
            == "rc16"   # both named: undecided, the rule decides

    def test_configured_matcher_still_wins(self, tmp_path):
        cfg = _cfg(tmp_path, focus_model_rc16_match="filter=R")
        assert fm.classify_report(self._rep(Camera="AP26MC"), cfg) \
            == "piggyback"


class TestIngestLoop:
    def test_startup_then_only_on_change(self, tmp_path):
        d = _reports_dir(tmp_path)
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(d),
                   focus_model_ingest_poll_s=30)
        calls = []
        real = fm.ingest_af_reports

        def spy(c, rdir=None, trigger="x"):
            calls.append(trigger)
            return real(c, rdir, trigger)

        ticks = {"n": 0}

        async def fake_sleep(s):
            ticks["n"] += 1
            if ticks["n"] == 2:   # a new NINA AF report lands
                rep = json.loads((FIX / "2026-10-05--22-22-59.json").read_text())
                rep["Timestamp"] = "2026-10-06T00:40:00.0-06:00"
                rep["Temperature"] = 21.29
                (d / "new.json").write_text(json.dumps(rep))
            return ticks["n"] < 4

        orig = fm.ingest_af_reports
        fm.ingest_af_reports = spy
        try:
            asyncio.run(fm.ingest_loop(lambda: cfg, sleep=fake_sleep))
        finally:
            fm.ingest_af_reports = orig
        assert calls == ["startup", "poll"]
        assert len(fm.load_points(cfg)) == 2

    def test_off_when_poll_zero(self, tmp_path):
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(
            _reports_dir(tmp_path)), focus_model_ingest_poll_s=0)

        async def once(s):
            return False

        asyncio.run(fm.ingest_loop(lambda: cfg, sleep=once))
        assert fm.load_points(cfg) == []


# ------------------------------------------------------------- seeds

class TestSeedFallback:
    @pytest.fixture
    def fs(self, monkeypatch, tmp_path):
        """The real July table (repo focus_seeds.json) only."""
        from photonscript.scheduler import focus_seeds
        return focus_seeds

    def test_no_temperature_is_no_longer_the_warm_median(self, tmp_path, fs):
        cfg = _cfg(tmp_path)
        # L 5200 @ 10 C and 6040 @ 28.8 C: the line at its mean temp, not 6040
        assert fs.seed_for("L", None, cfg) == 5620

    def test_tonight_from_the_line(self, tmp_path, fs):
        cfg = _cfg(tmp_path)
        assert fs.seed_for("L", 23.29, cfg) == pytest.approx(5794, abs=2)

    def test_narrowband_is_l_line_plus_offset(self, tmp_path, fs):
        cfg = _cfg(tmp_path)
        assert fs.seed_for("Ha", 23.29, cfg) == fs.seed_for("L", 23.29, cfg) - 187
        assert fs.seed_for("SII", 23.29, cfg) == fs.seed_for("L", 23.29, cfg) - 187
        assert fs.seed_for("R", 23.29, cfg) == fs.seed_for("L", 23.29, cfg)

    def test_empty_offsets_keep_own_records(self, tmp_path, fs):
        cfg = _cfg(tmp_path, focus_filter_offsets="")
        # Ha's own two July points (5785 @ 20.3, 5853 @ 28.3)
        assert fs.seed_for("Ha", 24.3, cfg) == pytest.approx(5819, abs=2)

    def test_ingested_af_and_expected_temp(self, tmp_path, fs):
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(
            _reports_dir(tmp_path)))
        fm.ingest_af_reports(cfg)
        et = fm.expected_temp(cfg, today=date(2026, 10, 6))
        assert et["temp"] == 23.29 and et["night"] == "2026-10-05"
        # sequence built with no live temperature: expected temp + the AF
        # point in the fit put the seed within ~40 steps of tonight's 5760
        # (the old median seed was 6040, 280 high)
        seed = fs.seed_for("L", None, cfg)
        assert abs(seed - 5760) <= 40

    def test_nan_temperature_is_ignored(self, tmp_path, fs):
        cfg = _cfg(tmp_path)
        assert fs.seed_for("L", float("nan"), cfg) == 5620

    def test_expected_temp_too_old(self, tmp_path):
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(
            _reports_dir(tmp_path)))
        fm.ingest_af_reports(cfg)
        assert fm.expected_temp(cfg, today=date(2026, 12, 1)) is None


def test_build_time_focuser_temp_uses_the_info_endpoint(monkeypatch):
    import httpx
    from photonscript.scheduler import nina_sequence_json as nsj
    seen = []

    class R:
        def __init__(self, t):
            self.t = t

        def json(self):
            return {"Response": {"Temperature": self.t}, "Success": True}

    for t, want in ((23.29, 23.29), ("NaN", None)):
        nsj._FOCTEMP_CACHE.update(t=0.0, v=None)
        monkeypatch.setattr(httpx, "get",
                            lambda url, timeout=0, _t=t: (seen.append(url),
                                                          R(_t))[1])
        cfg = PhotonScriptConfig(_env_file=None,
                                 nina_base_url="http://x:1888/v2/api")
        assert nsj._current_focuser_temp(cfg) == want
    assert seen[0] == "http://x:1888/v2/api/equipment/focuser/info"
    nsj._FOCTEMP_CACHE.update(t=0.0, v=None)


# ------------------------------------------------------- trust / drive

class TestTrust:
    def test_cfz_from_af_step(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _synthetic())
        tr = fm.trust(cfg)
        assert tr["cfz_steps"] == 90 and tr["sigma_limit"] == 45
        assert tr["cfz_source"] == "median AF step size"
        assert tr["trusted"] is True and tr["mode"] == "advisory"
        assert set(tr["filters"]) == {"L", "R", "G", "B", "Ha", "OIII", "SII"}

    def test_drive_mode_needs_switch_and_trust(self, tmp_path):
        cfg = _cfg(tmp_path, focus_model_drive=True)
        _store(cfg, _synthetic())
        assert fm.trust(cfg)["mode"] == "drive"
        _store(cfg, _synthetic(n=7))   # one AF per filter
        tr = fm.trust(cfg)
        assert tr["trusted"] is False and tr["mode"] == "advisory"
        assert any("needs 8" in r for r in tr["reasons"])

    def test_tight_cfz_config_blocks_trust(self, tmp_path):
        cfg = _cfg(tmp_path, focus_cfz_steps=10)
        _store(cfg, _synthetic())
        tr = fm.trust(cfg)
        assert tr["sigma_limit"] == 5 and tr["trusted"] is False

    def test_no_cfz_uses_fixed_bar(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, [dict(p, step=None) for p in _synthetic()])
        assert fm.trust(cfg)["sigma_limit"] == 25.0


class TestModelMove:
    def test_refuses_when_off(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _synthetic())
        assert fm.model_move_target(cfg, "L", 15.0)["move"] is False

    def test_moves_to_the_table(self, tmp_path):
        cfg = _cfg(tmp_path, focus_model_drive=True)
        _store(cfg, _synthetic())
        t = fm.model_move_target(cfg, "H", 15.0)    # NINA name -> Ha
        assert t["move"] is True and t["filter"] == "Ha"
        assert t["position"] == pytest.approx(5000 - 190 + 45 * 15, abs=25)

    def test_refuses_without_temperature_or_points(self, tmp_path):
        cfg = _cfg(tmp_path, focus_model_drive=True)
        _store(cfg, _synthetic())
        assert fm.model_move_target(cfg, "L", "NaN")["move"] is False
        assert fm.model_move_target(cfg, "Foo", 15.0)["move"] is False
        assert fm.model_move_target(cfg, "L", 80.0)["move"] is False  # far out


class TestFocusRouter:
    @pytest.fixture
    def router(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        from photonscript.scheduler.routers import focus
        cfg = _cfg(tmp_path, focus_model_drive=True,
                   nina_base_url="http://x:1888/v2/api",
                   nina_autofocus_reports_dir=str(_reports_dir(tmp_path)))
        monkeypatch.setattr(app, "_config", cfg)
        moves = []

        async def info(base):
            return {"Temperature": 15.0, "Position": 5000, "Connected": True}

        async def move(base, pos):
            moves.append(pos)
            return {"Success": True}

        monkeypatch.setattr(focus, "_nina_focuser", info)
        monkeypatch.setattr(focus, "_nina_move", move)
        return focus, cfg, moves

    def test_model_move_moves_and_logs(self, router):
        focus, cfg, moves = router
        _store(cfg, _synthetic())
        out = asyncio.run(focus.api_focus_model_move(filter="L"))
        assert out["verdict"] == "MOVED" and moves == [out["position"]]
        assert fm.recent_moves(cfg)[-1]["verdict"] == "MOVED"

    def test_dry_run_and_deadband(self, router, monkeypatch):
        focus, cfg, moves = router
        _store(cfg, _synthetic())
        out = asyncio.run(focus.api_focus_model_move(filter="L", dry_run=True))
        assert out["verdict"] == "WOULD_MOVE" and moves == []

        async def info(base):
            return {"Temperature": 15.0, "Position": out["position"] + 3}

        monkeypatch.setattr(focus, "_nina_focuser", info)
        out2 = asyncio.run(focus.api_focus_model_move(filter="L"))
        assert out2["verdict"] == "IN_PLACE" and moves == []

    def test_untrusted_never_moves(self, router):
        focus, cfg, moves = router
        out = asyncio.run(focus.api_focus_model_move(filter="L"))
        assert out["verdict"] == "NO_MOVE" and moves == []

    def test_nina_error_is_contained(self, router, monkeypatch):
        focus, cfg, moves = router

        async def boom(base):
            raise RuntimeError("NINA down")

        monkeypatch.setattr(focus, "_nina_focuser", boom)
        out = asyncio.run(focus.api_focus_model_move(filter="L"))
        assert out["verdict"] == "ERROR" and "NINA down" in out["reason"]

    def test_ingest_and_predict(self, router):
        focus, cfg, moves = router
        res = asyncio.run(focus.api_focus_ingest())
        assert res["added"] == 1 and fm.load_ingest_status(cfg)["trigger"] == "api"
        pr = focus.api_focus_predict(filter="L", temp=23.29)
        assert pr["filter"] == "L" and pr["temp"] == 23.29
        assert pr["model"]["confidence"] == "low"     # one AF only
        assert abs(pr["seed"] - 5760) <= 40


# ----------------------------------------------------------- sequence

def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from _walk(v)


def _gen_multi():
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.nina_sequence_json import generate_nina_json
    from photonscript.shared.models import (ExposurePlan, FilterType,
                                            NinaSequenceTarget)
    target = NinaSequenceTarget(
        name="IC 1805", ra_hours=2.55, dec_degrees=61.5, start_guiding=True,
        exposures=[ExposurePlan(filter_type=f, exposure_seconds=300, count=20,
                                gain=200)
                   for f in (FilterType.LUMINANCE, FilterType.HA)])
    seq = build_sequence_for_night("PS76b", [target])
    seq.wait_until_local = "21:00:00"
    return json.loads(generate_nina_json(seq))


def _loop(data):
    return next(d for d in _walk(data) if isinstance(d, dict)
                and "imaging (repeats" in str(d.get("Name", "")))


class TestDriveSequence:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        data = tmp_path / "data"
        data.mkdir()
        script = tmp_path / "focus-model-move.cmd"
        script.write_text("@echo off\r\nexit /b 0\r\n")
        monkeypatch.setenv("PS_DATA_DIR", str(data))
        monkeypatch.setenv("PS_FOCUS_MODEL_MOVE_SCRIPT", str(script))
        return data, script

    def test_default_off_is_todays_sequence(self, env):
        data, script = env
        (data / fm.POINTS_FILE).write_text(json.dumps(_synthetic()))
        txt = json.dumps(_gen_multi())
        assert "focus-model-move" not in txt and str(script.name) not in txt

    def test_drive_on_but_untrusted_keeps_af(self, env, monkeypatch):
        data, script = env
        monkeypatch.setenv("PS_FOCUS_MODEL_DRIVE", "true")
        (data / fm.POINTS_FILE).write_text(json.dumps(_synthetic(n=7)))
        assert script.name not in json.dumps(_gen_multi())

    def test_drive_on_and_trusted(self, env, monkeypatch):
        from photonscript.scheduler.sequence_lint import lint
        data, script = env
        monkeypatch.setenv("PS_FOCUS_MODEL_DRIVE", "true")
        monkeypatch.setenv("PS_FOCUS_MODEL_VERIFY_AF_MIN", "90")
        pts = [p for p in _synthetic() if p["filter"] != "Ha"]
        (data / fm.POINTS_FILE).write_text(json.dumps(pts))
        seq = _gen_multi()
        assert lint(seq, guided=True).ok
        steps = []
        for it in _loop(seq)["Items"]["$values"]:
            t = it["$type"]
            if "SwitchFilter" in t:
                steps.append(("switch", it["Filter"]["_name"]))
            elif "ExternalScript" in t:
                steps.append(("script", it["Script"].split()[-1]))
            elif "RunAutofocus" in t:
                steps.append(("af", None))
            elif "MoveFocuserRelative" in t:
                steps.append(("rel", it["RelativePosition"]))
            elif "SmartExposure" in t:
                steps.append(("expose", None))
        # L is driven (table move, no AF); Ha has no AFs of its own -> AF + offset
        assert steps == [("switch", "L"), ("script", "L"), ("expose", None),
                         ("switch", "L"), ("af", None), ("rel", -187),
                         ("expose", None)]
        smart = [d for d in _loop(seq)["Items"]["$values"]
                 if "SmartExposure" in d["$type"]][0]
        trig = {d["$type"].split(",")[0].split(".")[-1]: d
                for d in smart["Triggers"]["$values"]}
        temp = trig["AutofocusAfterTemperatureChangeTrigger"]
        assert temp["Amount"] == 1.0
        runner = [d["$type"] for d in temp["TriggerRunner"]["Items"]["$values"]]
        assert len(runner) == 1 and "ExternalScript" in runner[0]
        verify = trig["AutofocusAfterTimeTrigger"]
        assert verify["Amount"] == 90.0
        assert any("RunAutofocus" in d["$type"]
                   for d in verify["TriggerRunner"]["Items"]["$values"])
        assert any("RunAutofocus" in d.get("$type", "") for d in _walk(
            trig["AutofocusAfterHFRIncreaseTrigger"]["TriggerRunner"]))

    def test_missing_script_keeps_af(self, env, monkeypatch, tmp_path):
        data, script = env
        monkeypatch.setenv("PS_FOCUS_MODEL_DRIVE", "true")
        monkeypatch.setenv("PS_FOCUS_MODEL_MOVE_SCRIPT", str(tmp_path / "x.cmd"))
        (data / fm.POINTS_FILE).write_text(json.dumps(_synthetic()))
        assert "ExternalScript" not in json.dumps(_loop(_gen_multi()))


# ------------------------------------------------------------ read-out

def test_api_focus_readout(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(
        _reports_dir(tmp_path)))
    monkeypatch.setattr(app, "_config", cfg)
    fm.ingest_af_reports(cfg)
    out = app.api_focus()
    m = out["rc16"]["model"]
    assert m["ingest"]["added"] == 1 and m["af_runs"]["stored"] == 1
    assert m["points"][0] == {"f": "L", "t": 23.29, "p": 5760,
                              "time": "2026-10-05T22:22:59.4513981-06:00"}
    r = m["residuals"][0]
    assert r["predicted"] == 5760 and r["residual"] == 0 and r["initial"] == 6045
    assert m["trust"]["mode"] == "advisory" and m["trust"]["cfz_steps"] == 93
    assert m["af_skip_readiness"]["ready"] is False
    assert out["piggyback"]["model"]["af_runs"]["stored"] == 1
    assert out["piggyback"]["model"]["points"][0]["p"] == 11032


def test_config_fields_exposed():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    c = PhotonScriptConfig(_env_file=None)
    for env, typ in (("PS_FOCUS_MODEL_INGEST_POLL_S", "int"),
                     ("PS_FOCUS_MODEL_DRIVE", "bool"),
                     ("PS_FOCUS_MODEL_VERIFY_AF_MIN", "float"),
                     ("PS_FOCUS_CFZ_STEPS", "int"),
                     ("PS_FOCUS_MODEL_MOVE_SCRIPT", "str")):
        assert by_env[env][4] == typ and hasattr(c, by_env[env][0])
    assert c.focus_model_drive is False and c.focus_model_ingest_poll_s == 120


def test_cli_focus_ingest_local(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    res = CliRunner().invoke(cli.app, ["focus-ingest", "--local", "--dir",
                                       str(_reports_dir(tmp_path))])
    assert res.exit_code == 0, res.output
    assert "RC16 +1" in res.output and "1 unparseable" in res.output
    assert "rejected 1x: rc16: R^2 below af_min_r2" in res.output
    assert math.isclose(fm.load_points(cfg)[0]["temp"], 23.29)
