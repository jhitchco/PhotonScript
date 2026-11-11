"""2026-11-11 drift ladder: the tracking-test recipe with custom exposures and
several sky positions (fields), each its own container and gate."""
import json
from datetime import datetime

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import sideload as sd
from photonscript.scheduler import tracking_test as tt
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

LADDER = [60, 120, 240, 300, 450, 600, 750, 900]
SPECS = [{"label": "East", "ha": -3.5, "dec": 20},
         {"label": "Overhead", "ha": -0.7, "dec": 45},
         {"label": "West", "ha": 3.0, "dec": 20}]
DUSK = datetime(2026, 11, 12, 1, 40)


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    c = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                           quality_eccentricity_max=0.65)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", c)
    monkeypatch.setattr(nsj, "_filter_names_cache", c.filter_name_map())
    monkeypatch.chdir(tmp_path)
    return c


def _dur():
    return nsj.tracking_test_duration_s(1, LADDER, 1)


def test_opts_validate_and_label():
    rec, err = sd.arm_recipe(sd.RECIPE_TT_THEN_TONIGHT, {
        "exposures": "900,60,120,240,300,450,600,750", "filters": ["L"],
        "repeats": 1, "fields": SPECS})
    assert err is None
    assert rec["opts"]["exposures"] == [float(x) for x in LADDER]
    assert "60 to 900 s" in rec["label"] and "3 fields" in rec["label"]
    assert sd.arm_recipe(sd.RECIPE_TT_THEN_TONIGHT, {"bogus": 1})[1]
    assert sd.arm_recipe(sd.RECIPE_TT_THEN_TONIGHT,
                         {"fields": [{"label": "x"}]})[1]
    assert sd.arm_recipe(sd.RECIPE_TT_THEN_TONIGHT, {"exposures": "0"})[1]
    # no options: the old fixed recipe, unchanged
    assert sd.arm_recipe(sd.RECIPE_TT_THEN_TONIGHT, {})[0]["opts"] == {}


def test_place_fields_hits_the_asked_sky(cfg):
    spots = tt.place_fields(cfg, SPECS, DUSK, _dur())
    assert [s["label"] for s in spots] == ["East", "Overhead", "West"]
    lon = cfg.observatory_lon
    for s, spec in zip(spots, SPECS):
        assert min(s["alt_start_deg"], s["alt_end_deg"]) >= tt.FIELD_ALT_MIN
        mid_ha = (s["ha_start_h"] + s["ha_end_h"]) / 2
        assert abs(mid_ha - spec["ha"]) <= tt.FIELD_HA_TOL_H + 0.05
        # never across the meridian during its own ladder
        assert (s["ha_start_h"] < 0) == (s["ha_end_h"] < 0)
    assert spots[0]["start_utc"] < spots[1]["start_utc"] < spots[2]["start_utc"]
    assert len({s["name"] for s in spots}) == 3
    assert lon  # placed with the site longitude


def test_fixed_coordinates_kept(cfg):
    s = tt.place_fields(cfg, [{"label": "Here", "ra": 5.5, "dec": 22.0}],
                        DUSK, _dur())[0]
    assert (s["ra_hours"], s["dec_degrees"], s["source"]) == (5.5, 22.0, "request")


def _night():
    t = NinaSequenceTarget(
        name="NGC 604", ra_hours=1.5758, dec_degrees=30.78, start_guiding=False,
        exposures=[ExposurePlan(filter_type=FilterType.LUMINANCE,
                                exposure_seconds=120, count=10, gain=200,
                                offset=256)])
    s = build_sequence_for_night("PhotonScript_20261111", [t])
    s.wait_until_local = "00:00:00"
    return json.loads(nsj.generate_nina_json(s))


@pytest.mark.parametrize("gate", [None, "C:/astro/reentry-gate.cmd"])
def test_three_field_splice_lints(cfg, gate):
    spots = tt.place_fields(cfg, SPECS, DUSK, _dur())
    test = json.loads(nsj.generate_tracking_test_json(
        filters=["L"], exposures=LADDER, repeats=1, fields=spots))
    out = sd.splice_tracking_test(_night(), test, gate_script=gate)
    names = sd.target_names(out)
    assert names[:3] == [nsj.tracking_test_name(s["name"]) for s in spots]
    assert names[3] == "NGC 604"
    res = lint(out, guided=False)
    assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]
    text = json.dumps(out)
    for e in LADDER:
        assert f'"ExposureTime": {float(e)}' in text
    if gate:
        assert text.count("reentry-gate.cmd") >= 3   # one gate per field
