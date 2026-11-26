"""RC16 smart AF: configurable measured-offset threshold (2026-11-26).

With rc16_af_policy smart a 3 nm filter keeps an AF at every block until
the focus model has rc16_af_min_offset_afs AF runs (model offsets n) in the
filter and in the AF filter (n_ref). Default 3 (af_policy.MIN_OFFSET_AFS).
The live table on 2026-11-25: Ha 62 AFs (+90), OIII 2 (+13), SII 1 (+85).

Pinned: the default is unchanged, 2 measures OIII (no AF block, the model
offset as the focuser move) but not SII, n_ref counts too, the floor, the
setting row, and the sequence annotation line.
"""

from types import SimpleNamespace

import pytest

from photonscript.scheduler import af_policy
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import FilterType as F
from tests.test_scheduler.test_ps176_af_policy import (  # noqa: F401
    NAME, _afs, _blocks_by_filter, _dso, _exec, _gen, _imaging, _m31, _plan,
    _short, _vals, no_moon)

MODEL_1125 = {"ref_filter": "L", "offsets": {
    "Ha": {"steps": 90, "se": 5.0, "n": 62, "n_ref": 200},
    "OIII": {"steps": 13, "se": 65.0, "n": 2, "n_ref": 200},
    "SII": {"steps": 85, "se": None, "n": 1, "n_ref": 200}}}


@pytest.fixture
def model_1125(monkeypatch):
    monkeypatch.setattr(af_policy, "_model", lambda cfg: MODEL_1125)


def _cfg(monkeypatch, n=None):
    monkeypatch.setenv("PS_RC16_AF_POLICY", "smart")
    if n is not None:
        monkeypatch.setenv("PS_RC16_AF_MIN_OFFSET_AFS", str(n))
    return PhotonScriptConfig(
        _env_file=None, focus_filter_offsets="Ha:120,OIII:120,SII:120")


def test_default_three_is_unchanged(monkeypatch):
    cfg = _cfg(monkeypatch)
    assert cfg.rc16_af_min_offset_afs == af_policy.MIN_OFFSET_AFS == 3
    assert af_policy.min_offset_afs(cfg) == 3
    spec = af_policy.smart_spec(cfg, F.LUMINANCE, MODEL_1125)
    assert spec["measured"] >= {"L", "Ha"}
    assert spec["af_blocks"] == {"OIII", "SII"}
    tab = spec["table"]
    assert (tab["OIII"]["steps"], tab["OIII"]["source"]) == (120, "config")
    assert (tab["Ha"]["steps"], tab["Ha"]["source"]) == (90, "model")


def test_two_measures_a_filter_with_two_afs(monkeypatch):
    cfg = _cfg(monkeypatch, 2)
    spec = af_policy.smart_spec(cfg, F.LUMINANCE, MODEL_1125)
    assert spec["min_afs"] == 2
    assert "OIII" in spec["measured"] and "SII" not in spec["measured"]
    assert spec["af_blocks"] == {"SII"}
    assert spec["offsets"]["OIII"] == 13
    assert spec["table"]["OIII"]["source"] == "model"
    line = af_policy.describe(spec)
    assert "OIII +13 (model, 2 AFs)" in line
    assert "SII AF each block, then +120 (config, not measured: 1 of 2 AFs)" \
        in line


def test_n_ref_must_reach_the_threshold_too(monkeypatch):
    cfg = _cfg(monkeypatch, 2)
    m = {"ref_filter": "L", "offsets": {
        "OIII": {"steps": 13, "n": 2, "n_ref": 1}}}
    assert af_policy.offset_table(cfg, "L", m)["OIII"]["measured"] is False
    m["offsets"]["OIII"]["n_ref"] = 2
    assert af_policy.offset_table(cfg, "L", m)["OIII"]["measured"] is True


@pytest.mark.parametrize("raw,want", [(0, 1), (-4, 1), (1, 1), (5, 5),
                                      ("x", 3), (None, 3)])
def test_threshold_floor_and_bad_values(raw, want):
    assert af_policy.min_offset_afs(
        SimpleNamespace(rc16_af_min_offset_afs=raw)) == want


def test_setting_row():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    row = next(r for r in _CONFIG_FIELDS if r[0] == "rc16_af_min_offset_afs")
    assert row[1] == "PS_RC16_AF_MIN_OFFSET_AFS" and row[3] == "Imaging" \
        and row[4] == "int"


def _moves(b):
    return [d["RelativePosition"] for d in _exec(b)
            if _short(d["$type"]) == "MoveFocuserRelative"]


@pytest.mark.parametrize("n_min", [None, 2])
def test_sequence_oiii_block(monkeypatch, no_moon, model_1125, n_min):
    env = {"PS_FOCUS_FILTER_OFFSETS": "Ha:120,OIII:120,SII:120"}
    if n_min is not None:
        env["PS_RC16_AF_MIN_OFFSET_AFS"] = n_min
    t = _m31(extra=(_plan(F.HA, 600, 2), _plan(F.OIII, 600, 2),
                    _plan(F.SII, 600, 2)))
    seq = _gen([t], monkeypatch, "smart", **env)
    blocks = _blocks_by_filter(seq)
    oiii, sii = blocks[NAME[F.OIII]], blocks[NAME[F.SII]]
    note = [i["Text"] for i in _vals(_dso(seq), "Items")
            if _short(i["$type"]) == "Annotation"]
    line = next(x for x in note if "AF policy smart" in x)
    # SII (1 AF) keeps its AF block either way
    assert _afs(sii) and _moves(sii) == [120, -120]
    if n_min is None:
        assert _afs(oiii) and _moves(oiii) == [120, -120]
        assert "OIII AF each block, then +120 (config, not measured: 2 of 3" \
            in line
        assert len(_afs(_imaging(seq))) == 2
    else:
        assert not _afs(oiii) and _moves(oiii) == [13, -13]
        assert "OIII +13 (model, 2 AFs)" in line
        assert len(_afs(_imaging(seq))) == 1
