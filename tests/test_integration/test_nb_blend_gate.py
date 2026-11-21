"""autointegrate: a narrowband blend waits for enough RC16 Ha (2026-10-14).

blend.select_mode picks nb as soon as the RC16 run has any Ha master, and
autointegrate blended any new input set, so a 20 min Ha master would make
a weak Heart blend. Now autointegrate reads the RC16 run's ledger
(acquisition.subs_by_filter) and skips the NB blend while Ha is under
blend_nb_min_ha_h, with the reason. Unknown depth never skips; an L blend
is never gated; nb_gate off turns it off.
"""

import json
from types import SimpleNamespace

from photonscript.integration import autointegrate as ai
from photonscript.integration import blend as bl
from photonscript.integration import watch


def _run(tmp_path, sbf=None):
    d = tmp_path / "Staging" / "Heart_Nebula_rc16_x"
    d.mkdir(parents=True)
    if sbf is not None:
        (d / "ledger.json").write_text(json.dumps({
            "campaign": "Heart Nebula", "rig": "rc16", "kind": "integration",
            "machine": {"acquisition": {"subs_by_filter": sbf}}}), encoding="utf-8")
    return bl.RunInfo(d, "rc16", "Heart Nebula")


def _inp(tmp_path, sbf=None, mode="nb"):
    return SimpleNamespace(osc=tmp_path / "osc.xisf",
                           rc16={"Ha": tmp_path / "ha.xisf"},
                           rc16_run=_run(tmp_path, sbf), mode=mode)


def _opts(tmp_path, **kw):
    wo = watch.WatchOptions(base_url="http://x", staging_root=tmp_path / "Staging")
    return ai.AutoOptions(watch=wo, library=tmp_path / "lib", **kw)


def _sbf(ha_s, oiii_s=0.0):
    out = {"Ha": {"integrated": int(ha_s // 180), "integrated_s": ha_s}}
    if oiii_s:
        out["OIII"] = {"integrated": int(oiii_s // 180), "integrated_s": oiii_s}
    return out


def test_rc16_depth_from_the_run_ledger(tmp_path):
    inp = _inp(tmp_path, {"Ha": {"integrated": 40, "integrated_s": 7200},
                          "Ha_30s": {"integrated": 10, "integrated_s": 300},
                          "L": {"integrated": 5, "integrated_s": 900}})
    assert bl.rc16_depth(inp) == {"Ha": {"subs": 50, "hours": 2.08}}
    assert bl.rc16_depth(SimpleNamespace(rc16_run=None)) is None


def test_thin_ha_skips_the_nb_blend_with_the_reason(tmp_path):
    o = _opts(tmp_path)
    inp = _inp(tmp_path, _sbf(1200, oiii_s=1200))
    got, why = ai.blend_candidate(o, "Heart Nebula", lambda t: inp)
    assert got is None
    assert why.startswith("no blend: narrowband blend waits for RC16 Ha 0.3 h < 2 h")
    assert "OIII 0.3/1.5 h" in why


def test_enough_ha_blends_and_notes_the_depth(tmp_path):
    o = _opts(tmp_path)
    inp = _inp(tmp_path, _sbf(7400))
    got, why = ai.blend_candidate(o, "Heart Nebula", lambda t: inp)
    assert got is inp and "RC16 Ha 2.1/2 h" in why


def test_unknown_depth_and_l_blends_are_not_gated(tmp_path):
    o = _opts(tmp_path)
    got, why = ai.blend_candidate(o, "Heart Nebula", lambda t: _inp(tmp_path))
    assert got is not None and "depth unknown" in why
    o2 = _opts(tmp_path / "b")
    l_inp = _inp(tmp_path / "b", _sbf(60), mode="l")
    assert ai.blend_candidate(o2, "M31", lambda t: l_inp)[0] is l_inp


def test_gate_off_and_threshold_from_options(tmp_path):
    inp = _inp(tmp_path, _sbf(1200))
    assert ai.blend_candidate(_opts(tmp_path, nb_gate=False), "Heart Nebula",
                              lambda t: inp)[0] is inp
    assert ai.blend_candidate(_opts(tmp_path, nb_min_ha_h=0.25), "Heart Nebula",
                              lambda t: inp)[0] is inp

