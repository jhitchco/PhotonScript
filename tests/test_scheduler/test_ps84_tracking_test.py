"""PS-84: unguided tracking test (TPoint + ProTrack): the NINA sequence
generator, the field picker, and the per-filter / per-exposure report."""
import json
from datetime import datetime

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import tracking_test as tt
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path, **kw):
    kw.setdefault("quality_eccentricity_max", 0.60)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


@pytest.fixture(autouse=True)
def _cfg_for_generator(tmp_path, monkeypatch):
    """The generator reads config for gain / setpoint / offsets; pin it."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    return cfg


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


def _exec(node):
    for it in (node.get("Items") or {}).get("$values", []) or []:
        yield it
        yield from _exec(it)


def _gen(**kw):
    return json.loads(nsj.generate_tracking_test_json(**kw))


def _dso(seq):
    return [d for d in _walk(seq)
            if "DeepSkyObjectContainer" in d.get("$type", "")][0]


def _ladder(seq):
    return [d for d in _walk(seq) if str(d.get("Name", "")).endswith(
        nsj.TARGET_TRACKING_LADDER_SUFFIX)][0]


# ------------------------------------------------------------ sequence

class TestSequence:
    def test_lints_clean_with_tracking_test_warning(self):
        res = lint(_gen(), guided=False)
        assert res.ok, [f.detail for f in res.findings]
        assert any(f.rule == "tracking-test" and f.level == "WARN"
                   for f in res.findings)

    def test_no_guiding_no_dither(self):
        seq = _gen()
        types = [_short(d) for d in _walk(seq) if "$type" in d]
        assert "StartGuiding" not in types
        dithers = [d for d in _walk(seq)
                   if "$type" in d and _short(d) == "DitherAfterExposures"]
        assert dithers and all(d["AfterExposures"] == 0 for d in dithers)

    def test_acquisition_order(self):
        dso = _dso(_gen())
        order = [_short(d) for d in dso["Items"]["$values"]]
        # StopGuiding and the cool check come before anything moves
        assert order.index("StopGuiding") < order.index("SlewScopeToRaDec")
        assert order.index("CoolCamera") < order.index("SlewScopeToRaDec")
        seq_part = [t for t in order if t in (
            "SlewScopeToRaDec", "SwitchFilter", "MoveFocuserAbsolute",
            "RunAutofocus", "Center")]
        assert seq_part == ["SlewScopeToRaDec", "SwitchFilter",
                            "MoveFocuserAbsolute", "RunAutofocus", "Center"]
        sw = [d for d in dso["Items"]["$values"] if _short(d) == "SwitchFilter"]
        assert sw[0]["Filter"]["_name"] == "L"
        cool = [d for d in dso["Items"]["$values"] if _short(d) == "CoolCamera"]
        assert cool[0]["Temperature"] <= 0.0

    def test_ladder_order_offset_and_recenter(self):
        lad = _ladder(_gen())
        steps = []
        for it in lad["Items"]["$values"]:
            t = _short(it)
            if t == "SmartExposure":
                items = it["Items"]["$values"]
                steps.append((items[0]["Filter"]["_name"],
                              items[1]["ExposureTime"],
                              it["Conditions"]["$values"][0]["Iterations"]))
            elif t in ("Center", "RunAutofocus"):
                steps.append(t)
            elif t == "SwitchFilter":
                steps.append(("switch", it["Filter"]["_name"]))
            elif t == "MoveFocuserRelative":
                steps.append(("offset", it["RelativePosition"]))
        off = PhotonScriptConfig(_env_file=None).focus_offset_map()
        delta = int(off.get("Ha", 0)) - int(off.get("L", 0))
        want = [("L", 60.0, 2), ("L", 120.0, 2), ("L", 180.0, 2),
                ("L", 300.0, 2), "Center", ("switch", "L"), "RunAutofocus"]
        if delta:
            want.append(("offset", delta))
        want += [("H", 60.0, 2), ("H", 120.0, 2), ("H", 180.0, 2),
                 ("H", 300.0, 2)]
        assert steps == want

    def test_light_loops_guarded_and_ladder_runs_once(self):
        seq = _gen()
        lad = _ladder(seq)
        conds = [_short(c) for c in lad["Conditions"]["$values"]]
        for want in ("SafetyMonitorCondition", "AltitudeCondition",
                     "TimeCondition", "LoopCondition"):
            assert want in conds
        loop = [c for c in lad["Conditions"]["$values"]
                if _short(c) == "LoopCondition"][0]
        assert loop["Iterations"] == 1
        smarts = [d for d in _walk(lad) if "$type" in d
                  and _short(d) == "SmartExposure"]
        assert len(smarts) == 8
        for s in smarts:
            c = [_short(x) for x in s["Conditions"]["$values"]]
            assert c[0] == "LoopCondition"
            assert "SafetyMonitorCondition" in c and "TimeCondition" in c
            trig = [_short(x) for x in s["Triggers"]["$values"]]
            assert trig[0] == "DitherAfterExposures"
            # no HFR refocus mid-ladder: trailing would trigger it
            assert "AutofocusAfterHFRIncreaseTrigger" not in trig

    def test_names_and_parent_links(self):
        seq = _gen(name="Soul Nebula", ra_hours=2.852, dec_degrees=60.43)
        dso = _dso(seq)
        assert dso["Target"]["TargetName"] == "Tracking test Soul Nebula"
        assert dso["Name"] == "Tracking test Soul Nebula"
        assert list(dso)[0] == "$id" and "$ref" in dso["Parent"]
        # already-prefixed names are not doubled
        assert nsj.tracking_test_name("Tracking test X") == "Tracking test X"

    def test_custom_filters_exposures_repeats(self):
        seq = _gen(filters=["Ha"], exposures=[300, 60, 60, "x", -5],
                   repeats=3)
        lad = _ladder(seq)
        smarts = [it for it in lad["Items"]["$values"]
                  if _short(it) == "SmartExposure"]
        assert [(s["Items"]["$values"][0]["Filter"]["_name"],
                 s["Items"]["$values"][1]["ExposureTime"],
                 s["Conditions"]["$values"][0]["Iterations"])
                for s in smarts] == [("H", 60.0, 3), ("H", 300.0, 3)]
        # a single non-L filter still gets the L -> Ha offset after the AF
        types = [_short(it) for it in lad["Items"]["$values"]]
        assert "Center" not in types

    def test_lint_rejects_guiding_inside_a_tracking_test(self):
        seq = _gen()
        lad = _ladder(seq)
        lad["Items"]["$values"].insert(0, {
            "$id": "9999",
            "$type": "NINA.Sequencer.SequenceItem.Guider.StartGuiding, "
                     "NINA.Sequencer",
            "Parent": {"$ref": lad["$id"]}, "ForceCalibration": False})
        res = lint(seq, guided=False)
        assert not res.ok
        assert any(f.rule == "tracking-test" and f.level == "ERROR"
                   for f in res.findings)

    def test_container_names_canonicalize(self):
        from photonscript.shared.target_names import canonical_target
        name = "Tracking test Heart Nebula"
        assert canonical_target(
            f"{name}{nsj.TARGET_TRACKING_LADDER_SUFFIX}_Container") == name
        # never merges into the real project
        assert canonical_target(name, ["Heart Nebula"]) == name
        assert tt.is_tracking_test(name)
        assert not tt.is_tracking_test("Heart Nebula")

    def test_duration_estimate(self):
        s = nsj.tracking_test_duration_s(2, [60, 120, 180, 300], 2)
        assert 2 * 2 * 660 < s < 2 * 2 * 660 + 1800


# ------------------------------------------------------------ target pick

class TestPick:
    def test_ephemeris_heart_transit(self):
        # Heart Nebula transits ~60 deg up from Rodeo
        cfg = PhotonScriptConfig(_env_file=None)
        best = max(tt.altitude(2.555, 61.47, datetime(2026, 9, 28, h, m),
                               cfg.observatory_lat, cfg.observatory_lon)
                   for h in range(6, 12) for m in (0, 30))
        assert 59.0 < best < 61.5

    @pytest.mark.parametrize("when", ["2026-09-28T04:00:00",
                                      "2026-09-28T06:00:00",
                                      "2026-09-28T08:30:00",
                                      "2026-09-28T10:30:00"])
    def test_pick_is_high_and_does_not_cross(self, when):
        cfg = PhotonScriptConfig(_env_file=None)
        t0 = datetime.fromisoformat(when)
        p = tt.pick_target(cfg, t0, 3600)
        assert p["source"] == "auto"
        assert tt.ALT_MIN <= p["alt_deg"] <= tt.ALT_MAX
        ha0 = p["ha_hours"]
        assert ha0 >= 0.05 or ha0 + 1.0 <= -0.1  # never crosses mid-test

    def test_fallback_heart(self, monkeypatch):
        cfg = PhotonScriptConfig(_env_file=None)
        monkeypatch.setattr(tt, "_candidates", lambda projects=None: [
            {"name": "Low", "ra_hours": 12.0, "dec_degrees": -80.0,
             "type": "", "source": "catalog"}])
        p = tt.pick_target(cfg, datetime(2026, 9, 28, 6, 0), 3600)
        assert p["source"] == "fallback" and p["name"] == "Heart Nebula"

    def test_projects_are_candidates(self):
        class T:
            name, ra_hours, dec_degrees, object_type = "Mine", 1.0, 2.0, "x"

        class P:
            target = T()
        c = tt._candidates([P()])
        assert c[0]["name"] == "Mine" and c[0]["source"] == "project"


# ------------------------------------------------------------ report

def _recs(ladder, flt="L", rig="rc16", target="Tracking test Heart Nebula"):
    out = []
    for exp, eccs in ladder:
        for i, e in enumerate(eccs):
            out.append({"rig": rig, "target": target, "filter": flt,
                        "exp_s": float(exp), "ecc": e, "hfr": 3.0 + exp / 300,
                        "fwhm_arcsec": 1.5, "stars": 180, "passed_qa": e <= 0.6,
                        "file": f"LIGHT\\{flt}_{exp}_{i}.fits",
                        "time": f"2026-09-28T04:{i:02d}:00Z"})
    return out


GOOD = [(60, [.40, .42]), (120, [.44, .46]), (180, [.48, .50]),
        (300, [.52, .54])]


class TestReport:
    def test_grouping_and_capped_verdict(self, tmp_path):
        cfg = _cfg(tmp_path)
        recs = (_recs(GOOD, "L")
                + _recs([(60, [.40, .42]), (120, [.46, .50]),
                         (180, [.58, .66]), (300, [.70, .75])], "Ha")
                + [{"rig": "rc16", "target": "Heart Nebula", "filter": "Ha",
                    "exp_s": 300.0, "ecc": 0.9}])
        rep = tt.build_report(cfg, "2026-09-27", records=recs,
                              read_headers=False)
        assert rep["n_subs"] == 16
        keys = [(g["filter"], g["exp_s"]) for g in rep["groups"]]
        assert keys[0] == ("L", 60.0) and keys[4] == ("Ha", 60.0)
        ha180 = [g for g in rep["groups"]
                 if g["filter"] == "Ha" and g["exp_s"] == 180.0][0]
        assert ha180["n"] == 2 and ha180["tracking_pass"] == 1
        assert ha180["ecc_median"] == pytest.approx(0.62)
        assert ha180["ecc_max"] == pytest.approx(0.66)
        assert ha180["status"] == "fail"
        v = {x["filter"]: x for x in rep["verdicts"]}
        assert v["L"]["longest_pass_s"] == 300.0
        assert v["Ha"]["longest_pass_s"] == 120.0
        assert v["Ha"]["ecc_per_100s"] > 0
        r = rep["recommendation"]
        assert r["action"] == "run-unguided-capped" and r["unguided_s"] == 120.0
        assert "120 s" in r["headline"]

    def test_all_pass(self, tmp_path):
        rep = tt.build_report(_cfg(tmp_path), "2026-09-27",
                              records=_recs(GOOD, "L") + _recs(GOOD, "Ha"),
                              read_headers=False)
        r = rep["recommendation"]
        assert r["action"] == "run-unguided" and r["unguided_s"] == 300.0

    def test_nothing_passes_rebuild(self, tmp_path):
        bad = [(60, [.55, .60]), (120, [.7, .8]), (180, [.8, .8]),
               (300, [.9, .9])]
        rep = tt.build_report(_cfg(tmp_path), "2026-09-27",
                              records=_recs(bad, "L") + _recs(bad, "Ha"),
                              read_headers=False)
        r = rep["recommendation"]
        assert r["unguided_s"] is None
        # 60 s median 0.575 > 0.55: already elongated at the shortest length
        assert r["action"] == "keep-guiding-fix-optics"
        assert any("optics" in d for d in r["detail"])

    def test_tracking_failure_rebuild_model(self, tmp_path):
        # round stars at the shortest length (not optics) but every length
        # fails: the 60 s subs show a doubled-star tracking jump
        recs = _recs([(60, [.30, .32]), (120, [.7, .8])], "L")
        for r in recs[:2]:
            r["scorecard"] = {"rows": [["tracking_jump", 0.4, 0.25, "fail"]]}
        rep = tt.build_report(_cfg(tmp_path), "2026-09-27", records=recs,
                              read_headers=False)
        assert rep["recommendation"]["action"] == "rebuild-model"
        assert "ProTrack" in rep["recommendation"]["headline"]

    def test_non_monotonic_flag(self, tmp_path):
        lad = [(60, [.40, .42]), (120, [.7, .75]), (180, [.45, .47])]
        rep = tt.build_report(_cfg(tmp_path), "2026-09-27",
                              records=_recs(lad, "L"), read_headers=False)
        v = rep["verdicts"][0]
        assert v["longest_pass_s"] == 60.0 and v["non_monotonic"]

    def test_no_subs(self, tmp_path):
        rep = tt.build_report(_cfg(tmp_path), "2026-09-27", records=[],
                              read_headers=False)
        assert rep["n_subs"] == 0
        assert "No subs" in rep["recommendation"]["detail"][0]

    def test_direction_from_star_sidecar(self, tmp_path):
        from photonscript.shared import star_table
        cfg = _cfg(tmp_path)
        recs = _recs([(300, [.5, .5])], "L")
        for r in recs:
            n = 40
            tbl = {"v": 1, "w": 1000, "h": 800, "n": n,
                   "x": [100 + 20 * i for i in range(n)],
                   "y": [400 + (i % 5) for i in range(n)],
                   "hfr": [3.0] * n, "ecc": [0.5] * n,
                   "theta": [1.5708] * n}  # every star stretched along y
            star_table.write(cfg, "2026-09-27", r["file"], tbl, rig="rc16")
        rep = tt.build_report(cfg, "2026-09-27", records=recs,
                              read_headers=False, pa_override=0.0)
        el = rep["groups"][0]["elongation"]
        assert el["R_median"] == pytest.approx(1.0)
        assert el["direction"].startswith("one common direction")
        assert el["image_axis"] == "image y (vertical)"
        assert el["sky_axis"] == "Dec"
        # camera turned 90 deg: the same stretch is RA
        rep90 = tt.build_report(cfg, "2026-09-27", records=recs,
                                read_headers=False, pa_override=90.0)
        assert rep90["groups"][0]["elongation"]["sky_axis"] == "RA"
        # diagonal camera: no RA/Dec split, the hint says so
        rep45 = tt.build_report(cfg, "2026-09-27", records=recs,
                                read_headers=False, pa_override=45.0)
        assert rep45["groups"][0]["elongation"]["sky_axis"] is None
        assert any("Camera angle unknown" in d
                   for d in rep45["recommendation"]["detail"])

    def test_headers_altitude_and_pier(self, tmp_path):
        from astropy.io import fits
        import numpy as np
        cfg = _cfg(tmp_path)
        recs = _recs([(60, [.4, .4])], "L")
        for i, r in enumerate(recs):
            p = tmp_path / f"sub{i}.fits"
            h = fits.Header()
            h["CENTALT"] = 58.0 + i
            h["PIERSIDE"] = "West"
            fits.PrimaryHDU(np.zeros((2, 2), dtype=np.uint16),
                            header=h).writeto(p)
            r["abs_path"] = str(p)
        g = tt.build_report(cfg, "2026-09-27", records=recs)["groups"][0]
        assert g["alt_median"] == pytest.approx(58.5)
        assert g["pier_side"] == ["West"]

    def test_alt_from_mount_coords(self):
        cfg = PhotonScriptConfig(_env_file=None)
        alt, ha = tt._alt_ha_from_header(cfg, {
            "OBJCTRA": "02 33 18", "OBJCTDEC": "+61 28 12",
            "DATE-OBS": "2026-09-28T08:40:00"})
        assert 55 < alt < 61 and -1 < ha < 1

    def test_format_and_default_night(self, tmp_path):
        cfg = _cfg(tmp_path)
        rep = tt.build_report(cfg, "2026-09-27", records=_recs(GOOD, "L"),
                              read_headers=False)
        txt = tt.format_report(rep)
        assert "Verdict:" in txt and "longest unguided pass 300 s" in txt
        assert chr(0x2014) not in txt  # no em dashes in output
        assert tt.default_night(cfg, datetime(2026, 9, 28, 9, 0)) \
            == "2026-09-27"


# ------------------------------------------------------------ API + CLI

class TestApi:
    def test_sequence_download_with_coords(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        monkeypatch.setattr(app, "_config", _cfg(tmp_path))
        r = app.api_tracking_test_sequence(name="Soul Nebula", ra=2.852,
                                           dec=60.43, filters="L,Ha",
                                           exposures="60,120", repeats=2,
                                           at="")
        assert "tracking_test_Soul_Nebula.json" in r.headers[
            "content-disposition"]
        seq = json.loads(r.body)
        assert _dso(seq)["Target"]["TargetName"] == "Tracking test Soul Nebula"

    def test_sequence_autopicks(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        monkeypatch.setattr(app, "_config", _cfg(tmp_path))
        f = app.api_tracking_test_target(name="", ra=None, dec=None,
                                         filters="L,Ha",
                                         exposures="60,120,180,300",
                                         repeats=2, at="2026-09-28T06:00:00Z")
        assert f["source"] in ("auto", "fallback") and f["est_minutes"] > 40
        r = app.api_tracking_test_sequence(name="", ra=None, dec=None,
                                           filters="L,Ha",
                                           exposures="60,120,180,300",
                                           repeats=2,
                                           at="2026-09-28T06:00:00Z")
        assert f["name"].replace(" ", "_") in r.headers[
            "content-disposition"]

    def test_named_field_without_coords(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        monkeypatch.setattr(app, "_config", _cfg(tmp_path))
        f = app.api_tracking_test_target(name="heart nebula", ra=None,
                                         dec=None, filters="L,Ha",
                                         exposures="60", repeats=2, at="")
        assert f["name"] == "Heart Nebula" and f["source"] == "named"

    def test_report_endpoint_reads_runs_log(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        from photonscript.scheduler.runs import runs_dir
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(app, "_config", cfg)
        p = runs_dir(cfg) / "2026-09-27_subs.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in _recs(GOOD, "L")),
                     encoding="utf-8")
        rep = app.api_tracking_test_report(date="2026-09-27", pa=None,
                                           headers=False)
        assert rep["n_subs"] == 8
        assert rep["verdicts"][0]["longest_pass_s"] == 300.0

    def test_cli(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from photonscript import cli
        from photonscript.scheduler.runs import runs_dir
        cfg = _cfg(tmp_path)
        (runs_dir(cfg) / "2026-09-27_subs.jsonl").write_text(
            "\n".join(json.dumps(r) for r in _recs(GOOD, "Ha")),
            encoding="utf-8")
        monkeypatch.setattr("photonscript.shared.config.PhotonScriptConfig",
                            lambda *a, **k: cfg)
        r = CliRunner().invoke(cli.app, ["tracking-test-report", "--date",
                                         "2026-09-27"])
        assert r.exit_code == 0, r.output
        assert "Verdict: Run unguided at 300 s" in r.output
