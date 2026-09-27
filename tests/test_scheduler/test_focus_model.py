"""PS-76: RC16 focus model (AF-report points, pooled temperature fit,
measured filter offsets), its seed hook, /api/focus read-out and the
focus-offset calibration sequence."""
import json
import random

import pytest

from photonscript.scheduler import focus_model as fm
from photonscript.shared.config import PhotonScriptConfig


def _report(filt="L", pos=5736, temp=18.29, r2=0.97, npts=9, ts="2026-09-27T02:12:00",
            initial=6040, duration="00:03:12.5000000"):
    return {"Filter": filt, "Timestamp": ts, "Temperature": temp,
            "CalculatedFocusPoint": {"Position": pos, "Value": 3.1},
            "InitialFocusPoint": {"Position": initial, "Value": 9.0},
            "RSquares": {"Quadratic": r2 - 0.05, "Hyperbolic": r2},
            "MeasurePoints": [{"Position": pos + 40 * i, "Value": 4.0}
                              for i in range(npts)],
            "Duration": duration}


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _synthetic(n=60, slope=45.0, noise=8.0, seed=1):
    random.seed(seed)
    off = {"L": 0, "R": -10, "G": -5, "B": 15, "Ha": -190, "OIII": -170,
           "SII": -200}
    pts = []
    for i in range(n):
        f = list(off)[i % len(off)]
        t = random.uniform(5, 28)
        pts.append({"filter": f, "temp": t, "time": f"t{i:03d}",
                    "position": round(5000 + off[f] + slope * t
                                      + random.gauss(0, noise))})
    return pts, off


# ---------------------------------------------------------------- reports

class TestPointFromReport:
    def test_good_report(self, tmp_path):
        pt, why = fm.point_from_report(_report(filt="H"), _cfg(tmp_path))
        assert why == "ok"
        assert pt["filter"] == "Ha"          # NINA name H -> class Ha
        assert pt["position"] == 5736 and pt["temp"] == 18.29
        assert pt["initial"] == 6040 and pt["points"] == 9
        assert pt["duration_s"] == pytest.approx(192.5)
        assert pt["r2"] == 0.97

    def test_rejects_low_r2_few_points_and_no_position(self):
        assert fm.point_from_report(_report(r2=0.4))[0] is None
        assert fm.point_from_report(_report(npts=3))[0] is None
        rep = _report()
        rep["CalculatedFocusPoint"] = {}
        assert fm.point_from_report(rep)[0] is None

    def test_nan_temperature_kept_as_none(self):
        pt, _ = fm.point_from_report(_report(temp="NaN"))
        assert pt is not None and pt["temp"] is None

    def test_duration_formats(self):
        assert fm._duration_s("00:02:00") == 120
        assert fm._duration_s("1.00:00:01") == 86401
        assert fm._duration_s(95.5) == 95.5
        assert fm._duration_s("garbage") is None


# -------------------------------------------------------------------- fit

class TestFit:
    def test_recovers_common_slope_and_offsets(self):
        pts, off = _synthetic()
        m = fm.fit(pts, "L")
        assert m["slope_steps_per_c"] == pytest.approx(45.0, abs=2.0)
        for f, true in off.items():
            if f == "L":
                continue
            assert m["offsets"][f]["steps"] == pytest.approx(true, abs=20)
        assert m["n_rejected"] == 0

    def test_single_wild_af_is_rejected_not_averaged(self):
        pts, _off = _synthetic()
        pts.append({"filter": "Ha", "position": 6400, "temp": 15.0,
                    "time": "zzz"})
        m = fm.fit(pts, "L")
        assert m["n_rejected"] == 1
        assert m["filters"]["Ha"]["n_rejected"] == 1
        assert m["offsets"]["Ha"]["steps"] == pytest.approx(-190, abs=20)

    def test_no_temperature_spread_means_no_slope(self):
        pts = [{"filter": "L", "position": 5700 + i, "temp": 20.0,
                "time": str(i)} for i in range(4)]
        pts += [{"filter": "Ha", "position": 5500, "temp": 20.3, "time": "h"}]
        m = fm.fit(pts, "L")
        assert m["slope_steps_per_c"] is None
        assert m["filters"]["L"]["intercept"] == pytest.approx(5701.5)
        assert m["offsets"]["Ha"]["steps"] == -202

    def test_points_without_temperature_are_ignored(self):
        m = fm.fit([{"filter": "L", "position": 5700, "temp": None}], "L")
        assert m["n_points"] == 0 and m["filters"] == {}

    def test_empty(self):
        m = fm.fit([], "L")
        assert m["filters"] == {} and m["slope_steps_per_c"] is None
        assert fm.predict(m, "L", 10) is None


class TestPredict:
    def test_confidence_levels(self):
        pts, _ = _synthetic()
        m = fm.fit(pts, "L")
        inside = fm.predict(m, "L", 15.0)
        assert inside["confidence"] == "high"
        assert inside["position"] == pytest.approx(5000 + 45 * 15, abs=25)
        assert fm.predict(m, "L", 60.0)["confidence"] == "low"
        assert fm.predict(m, "L", None)["confidence"] == "med"
        assert fm.predict(m, "Dark", 10.0) is None

    def test_few_points_low_confidence(self):
        m = fm.fit([{"filter": "L", "position": 5700, "temp": 20.0}], "L")
        assert fm.predict(m, "L", 5.0)["confidence"] == "low"

    def test_lookup_table(self):
        pts, _ = _synthetic()
        m = fm.fit(pts, "L")
        lut = m["lookup"]
        assert set(lut) == {"L", "R", "G", "B", "Ha", "OIII", "SII"}
        assert lut["L"]["20"] - lut["L"]["10"] == pytest.approx(450, abs=25)

    def test_af_skip_readiness(self):
        assert fm.af_skip_readiness({})["ready"] is False
        pts, _ = _synthetic(n=140)
        assert fm.af_skip_readiness(fm.fit(pts, "L"))["ready"] is True


# ----------------------------------------------------------------- ingest

class TestIngest:
    def _write_reports(self, d, reps):
        d.mkdir(exist_ok=True)
        for i, r in enumerate(reps):
            (d / f"af_{i}.json").write_text(json.dumps(r))

    def test_disabled_without_reports_dir(self, tmp_path):
        out = fm.ingest_af_reports(_cfg(tmp_path))
        assert out["enabled"] is False and out["added"] == 0
        assert not (tmp_path / fm.POINTS_FILE).exists()

    def test_ingest_dedup_and_model_written(self, tmp_path):
        rdir = tmp_path / "AutoFocus"
        self._write_reports(rdir, [
            _report("L", 5736, 24.3, ts="2026-09-27T02:12:00"),
            _report("L", 5650, 20.3, ts="2026-09-27T05:12:00"),
            _report("H", 5550, 24.0, ts="2026-09-27T02:20:00"),
            _report("L", 5999, 20.0, r2=0.2, ts="2026-09-27T06:00:00"),
        ])
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(rdir))
        out = fm.ingest_af_reports(cfg)
        assert {k: out[k] for k in ("enabled", "read", "added", "rejected",
                                    "total")} == {"enabled": True, "read": 4,
                                                  "added": 3, "rejected": 1,
                                                  "total": 3}
        again = fm.ingest_af_reports(cfg)
        assert again["added"] == 0 and again["total"] == 3
        model = json.loads((tmp_path / fm.MODEL_FILE).read_text())
        assert set(model["filters"]) == {"L", "Ha"}
        assert model["offsets"]["Ha"]["steps"] < 0


# --------------------------------------------- rig filter (shared AF folder)

def _osc(pos=11045, temp=2.0, ts="2026-09-27T03:00:00", filt=None, **kw):
    r = _report("L", pos, temp, ts=ts, initial=pos + 150, **kw)
    r["Filter"] = filt           # the OSC has no filter wheel
    return r


class TestRigFilter:
    def test_default_rule(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert fm.classify_report(_report("L", 5736), cfg) == "rc16"
        assert fm.classify_report(_report("H", 5550), cfg) == "rc16"
        assert fm.classify_report(_osc(), cfg) == "piggyback"
        assert fm.classify_report(_osc(filt=""), cfg) == "piggyback"
        assert fm.classify_report(_osc(filt="L-Pro"), cfg) == "piggyback"
        # an RC16-looking filter name far outside the RC16 EAF range, near
        # the Piggy-600 seed, is the Piggy-600 (e.g. a stub wheel named L)
        assert fm.classify_report(_osc(filt="L", pos=10900), cfg) == "piggyback"
        # an RC16 filter at a wild position nearer the RC16: unknown, left out
        assert fm.classify_report(_report("L", 2500), cfg) is None

    def test_configured_matchers(self, tmp_path):
        rc = _report("L", 5736)
        rc["CameraName"] = "OGMA AP26MC"
        pb = _osc(filt="L", pos=5800)          # would pass the default rule
        pb["CameraName"] = "OGMA AP26CC"
        cfg = _cfg(tmp_path, focus_model_rc16_match="any~AP26MC")
        assert fm.classify_report(rc, cfg) == "rc16"
        assert fm.classify_report(pb, cfg) == "piggyback"
        cfg = _cfg(tmp_path,
                   focus_model_rc16_match="Filter=L|R|G|B|H|O|S;position:4000-7000",
                   focus_model_piggyback_match="CameraName~AP26CC")
        assert fm.classify_report(rc, cfg) == "rc16"
        assert fm.classify_report(pb, cfg) == "rc16"     # rc16 rule checked first
        pb["CalculatedFocusPoint"]["Position"] = 11000
        assert fm.classify_report(pb, cfg) == "piggyback"
        odd = _report("SII", 9000)
        assert fm.classify_report(odd, cfg) is None       # neither matches

    def test_match_report_clauses(self):
        r = _report("H", 5550, 18.3)
        r["_file"] = "2026-09-27--02-20-00.json"
        assert fm.match_report(r, "filter=h|o|s")
        assert fm.match_report(r, "position:5000-6000; temp:10-")
        assert not fm.match_report(r, "position:-5000")
        assert fm.match_report(r, "file~2026-09-27")
        assert fm.match_report(r, "CalculatedFocusPoint.Value:3-4")
        assert not fm.match_report(r, "nosuchfield=x")
        assert not fm.match_report(r, "garbage clause")
        assert not fm.match_report(r, "")

    def test_mixed_folder_only_rc16_reaches_the_rc16_model(self, tmp_path):
        rdir = tmp_path / "AutoFocus"
        rdir.mkdir()
        reps = [
            _report("L", 5736, 24.3, ts="2026-09-27T02:12:00"),
            _report("L", 5650, 20.3, ts="2026-09-27T05:12:00"),
            _report("H", 5550, 24.0, ts="2026-09-27T02:20:00"),
            _osc(11045, 1.0, ts="2026-09-27T02:13:00"),
            _osc(11020, 0.0, ts="2026-09-27T04:13:00"),
            _osc(11060, 3.5, ts="2026-09-27T05:13:00", filt=""),
            _report("L", 2500, 20.0, ts="2026-09-27T06:00:00"),   # unknown
        ]
        for i, r in enumerate(reps):
            (rdir / f"af_{i}.json").write_text(json.dumps(r))
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(rdir))
        out = fm.ingest_af_reports(cfg)
        assert out["added"] == 3 and out["total"] == 3
        assert out["other_rig"] == 3 and out["unclassified"] == 1
        assert out["piggyback"] == {"added": 3, "total": 3}
        rc = json.loads((tmp_path / fm.MODEL_FILE).read_text())
        assert set(rc["filters"]) == {"L", "Ha"}
        assert rc["filters"]["L"]["intercept"] < 7000     # no 11000s mixed in
        pb = json.loads((tmp_path / fm.PB_MODEL_FILE).read_text())
        assert set(pb["filters"]) == {"OSC"} and pb["filters"]["OSC"]["n"] == 3
        again = fm.ingest_af_reports(cfg)
        assert again["added"] == 0 and again["piggyback"]["added"] == 0

    def test_points_stored_before_the_filter_are_resorted(self, tmp_path):
        mixed = [{"filter": "L", "position": 5736, "temp": 24.3, "time": "a"},
                 {"filter": "", "position": 11045, "temp": 1.0, "time": "b"},
                 {"filter": "L", "position": 11020, "temp": 0.0, "time": "c"}]
        (tmp_path / fm.POINTS_FILE).write_text(json.dumps(mixed))
        cfg = _cfg(tmp_path)
        assert fm.summary(cfg)["model"]["n_points"] == 1        # read-only view
        rdir = tmp_path / "AutoFocus"
        rdir.mkdir()
        out = fm.ingest_af_reports(cfg, str(rdir))
        assert out["total"] == 1 and out["piggyback"]["total"] == 2
        kept = json.loads((tmp_path / fm.POINTS_FILE).read_text())
        assert [p["position"] for p in kept] == [5736] and kept[0]["rig"] == "rc16"

    def test_piggyback_model_can_be_switched_off(self, tmp_path):
        rdir = tmp_path / "AutoFocus"
        rdir.mkdir()
        (rdir / "a.json").write_text(json.dumps(_osc()))
        cfg = _cfg(tmp_path, nina_autofocus_reports_dir=str(rdir),
                   focus_model_piggyback=False)
        out = fm.ingest_af_reports(cfg)
        assert out["other_rig"] == 1 and out["piggyback"]["added"] == 0

    def test_api_focus_shows_piggyback_model(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        pts = [{"filter": "OSC", "position": 11045 + i, "temp": float(i),
                "time": f"t{i}", "rig": "piggyback"} for i in range(4)]
        (tmp_path / fm.PB_POINTS_FILE).write_text(json.dumps(pts))
        monkeypatch.setattr(app, "_config", _cfg(tmp_path))
        out = app.api_focus()
        assert out["piggyback"]["model"]["af_runs"]["stored"] == 4
        assert out["piggyback"]["model"]["model"]["filters"]["OSC"]["n"] == 4
        assert out["rc16"]["model"]["af_runs"]["stored"] == 0


# ------------------------------------------------------------ seed hook

class TestSeedHook:
    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        from photonscript.scheduler import focus_seeds
        monkeypatch.setattr(focus_seeds, "_MODULE_TABLE",
                            tmp_path / "no_module.json")
        (tmp_path / "focus_seeds.json").write_text(json.dumps([
            {"filter": "L", "focpos": 6040, "foctemp": 28.8}]))
        return focus_seeds

    def _write_model(self, tmp_path, pts):
        (tmp_path / fm.MODEL_FILE).write_text(json.dumps(fm.fit(pts, "L")))

    def test_confident_model_wins(self, tmp_path, isolated):
        pts, _ = _synthetic()
        self._write_model(tmp_path, pts)
        seed = isolated.seed_for("L", 15.0, _cfg(tmp_path))
        assert seed == pytest.approx(5675, abs=25)

    def test_low_confidence_falls_back_to_table(self, tmp_path, isolated):
        self._write_model(tmp_path, [{"filter": "L", "position": 5500,
                                      "temp": 20.0}])
        assert isolated.seed_for("L", 5.0, _cfg(tmp_path)) == 6040

    def test_switch_off(self, tmp_path, isolated):
        pts, _ = _synthetic()
        self._write_model(tmp_path, pts)
        cfg = _cfg(tmp_path, focus_model_seed=False)
        assert isolated.seed_for("L", 15.0, cfg) == 6040

    def test_no_model_file_is_old_behavior(self, tmp_path, isolated):
        assert isolated.seed_for("L", 15.0, _cfg(tmp_path)) == 6040


# ------------------------------------------------------------------- API

def test_api_focus_carries_model(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    out = app.api_focus()
    assert set(out) == {"rc16", "piggyback"}
    m = out["rc16"]["model"]
    assert m["enabled"] is False and m["af_runs"]["stored"] == 0
    assert m["af_skip_readiness"]["ready"] is False


def test_summary_stats(tmp_path):
    pts = [dict(p, initial=p["position"] + 100, duration_s=180.0)
           for p in _synthetic()[0]]
    (tmp_path / fm.POINTS_FILE).write_text(json.dumps(pts))
    s = fm.summary(_cfg(tmp_path))
    assert s["af_runs"]["stored"] == 60
    assert s["af_runs"]["median_duration_s"] == 180.0
    assert s["af_runs"]["median_seed_error_steps"] == 100
    assert s["model"]["slope_steps_per_c"] == pytest.approx(45, abs=2)


# ------------------------------------------------ calibration sequence

def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from _walk(v)


def _short(d):
    return d["$type"].split(",")[0].split(".")[-1]


class TestCalibrationSequence:
    def _gen(self, **kw):
        from photonscript.scheduler.nina_sequence_json import generate_focus_calibration_json
        return json.loads(generate_focus_calibration_json(**kw))

    def _cal_items(self, seq):
        c = [d for d in _walk(seq)
             if str(d.get("Name", "")).endswith("focus calibration AFs")][0]
        return c["Items"]["$values"]

    def test_lints_clean_and_takes_no_lights(self):
        from photonscript.scheduler.sequence_lint import lint
        seq = self._gen()
        res = lint(seq)
        assert res.ok, [f.detail for f in res.findings]
        assert any(f.rule == "focus-calibration" for f in res.findings)
        types = [_short(d) for d in _walk(seq) if "$type" in d]
        assert "SmartExposure" not in types

    def test_bracketed_order_with_offset_moves(self):
        items = self._cal_items(self._gen())
        seq = []
        for it in items:
            t = _short(it)
            if t == "SwitchFilter":
                seq.append(it["Filter"]["_name"])
            elif t == "MoveFocuserRelative":
                seq.append(it["RelativePosition"])
            elif t == "RunAutofocus":
                seq.append("AF")
        # acquisition AF already did L, so the series starts at R
        assert seq == ["R", "AF", "G", "AF", "B", "AF", "L", "AF",
                       "H", -187, "AF", "O", "AF", "S", "AF", "L", 187, "AF"]

    def test_rounds_repeat_and_end_on_reference(self):
        items = self._cal_items(self._gen(rounds=2, filters=["L", "Ha"]))
        names = [it["Filter"]["_name"] for it in items
                 if _short(it) == "SwitchFilter"]
        assert names == ["H", "L", "H", "L"]

    def test_api_download(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        monkeypatch.setattr(app, "_config", _cfg(tmp_path))
        r = app.api_focus_calibration_sequence(rounds=9)
        assert "focus_calibration_NGC_7789.json" in r.headers[
            "content-disposition"]
        assert json.loads(r.body)["$type"]
