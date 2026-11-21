"""Piggy HDR set (2026-10-28): a deliberate OSC short set (M42 15 s beside
120 s) is never stacked into master_OSC. osc_hdr.plan_hdr_set marks it
exclude, integrate_stack.js integrates the main master from the other
groups and the shorts on their own into osc_hdr/master_OSC_short.xisf,
even with integrate_osc_hdr_core off. The M31 rule (<= 180 s also in the
full master) is unchanged when there is no such set."""
from pathlib import Path

from photonscript.integration import osc_hdr, pjsr
from photonscript.integration import pipeline as pl


def _st(exps, name="OSC"):
    return {"name": name, "filter": "OSC", "mixed_exposures": len(exps) > 1,
            "groups": [{"name": f"{e:g}s", "exp": float(e), "tag": f"_{e:.2f}s_"} for e in exps]}


OUT = Path("D:/S/run/out")


def test_hdr_set_is_excluded_from_the_main_master():
    st = _st([15, 120])
    notes = osc_hdr.attach([st], {"OSC": {15.0: 40, 120.0: 200}}, OUT, max_s=180,
                           min_frames=3, set_max_s=30)
    sub = st["short_subset"]
    assert sub["exclude"] and sub["groups"] == ["15s"] and sub["frames"] == 40
    assert sub["output"] == "D:/S/run/out/master/osc_hdr/master_OSC_short.xisf"
    assert st["mixed_exposures"] is False            # a pure 120 s master
    assert "kept out of master_OSC" in notes[0]
    it = osc_hdr.item_for(st, {15.0: 40, 120.0: 200}, OUT, {"x": 1.0, "y": 1.0}, 40.0)
    assert [x["exp"] for x in it["inputs"]] == [120.0, 15.0] and it["ratio"] == 8.0


def test_hdr_set_even_without_the_core_combine():
    st = _st([15, 120])
    osc_hdr.attach([st], {"OSC": {15.0: 40, 120.0: 200}}, OUT, max_s=180, min_frames=3,
                   set_max_s=30, core=False)
    assert st["short_subset"]["exclude"]
    # the M31 mix gets no subset at all without the core combine
    m31 = _st([120, 300])
    osc_hdr.attach([m31], {"OSC": {120.0: 98, 300.0: 162}}, OUT, max_s=180, min_frames=3,
                   set_max_s=30, core=False)
    assert "short_subset" not in m31


def test_m31_mix_unchanged_and_rules():
    m31 = _st([120, 300])
    osc_hdr.attach([m31], {"OSC": {120.0: 98, 300.0: 162}}, OUT, max_s=180, min_frames=3,
                   set_max_s=30)
    assert m31["short_subset"]["groups"] == ["120s"] and not m31["short_subset"].get("exclude")
    assert m31["mixed_exposures"] is True
    # 30 s beside 60 s is not 4x: no HDR set
    assert osc_hdr.plan_hdr_set(_st([30, 60]), {30.0: 9, 60.0: 9}, set_max_s=30) == (None, "")
    assert osc_hdr.plan_hdr_set(_st([120]), {120.0: 9}, set_max_s=30) == (None, "")
    # set_max_s 0 turns the rule off (15 s and 120 s are both <= 180 s:
    # one mixed master as before)
    st = _st([15, 120])
    osc_hdr.attach([st], {"OSC": {15.0: 40, 120.0: 9}}, OUT, max_s=180, min_frames=3,
                   set_max_s=0)
    assert "short_subset" not in st and st["mixed_exposures"] is True


def test_too_few_shorts_still_stay_out():
    st = _st([15, 120])
    notes = osc_hdr.attach([st], {"OSC": {15.0: 2, 120.0: 50}}, OUT, max_s=180,
                           min_frames=3, set_max_s=30)
    assert "short_subset" not in st and st["exclude_groups"] == ["15s"]
    assert "only 2 short sub(s)" in notes[0] and st["mixed_exposures"] is False


def test_integrate_template_excludes_the_set():
    t = pjsr.template(pjsr.INTEGRATE_TEMPLATE)
    assert t.isascii()
    body = t[t.index("function runStack("):]
    assert "stack.short_subset.exclude" in body and "stack.exclude_groups" in body
    assert "integrateShort(stack, regAll, R.selfRef);" in body
    # the exclusion happens before LocalNormalization and the integration
    assert body.index("HDR short set kept out of the master") < body.index("new LocalNormalization")


def test_config_key():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    assert c.integrate_osc_hdr_set_max_s == 30.0
    assert c.piggyback_hdr_fraction == 0.10 and c.piggyback_hdr_block_min == 20.0
    f = {x[0]: x for x in _CONFIG_FIELDS}
    assert f["integrate_osc_hdr_set_max_s"][1] == "PS_INTEGRATE_OSC_HDR_SET_MAX_S"
    assert f["piggyback_hdr_fraction"][1] == "PS_PIGGYBACK_HDR_FRACTION"
    o = pl.config_options(PhotonScriptConfig(_env_file=None, integrate_osc_hdr_set_max_s=20),
                          "piggyback")
    assert o["osc_hdr_set_max_s"] == 20.0
