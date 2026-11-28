"""PS-101: the PHD2 audit's exposure row follows the PS-90 tuner. A tuned
exposure reads PASS, Apply leaves the exposure to the tuner in exposure
mode, and the tuner's band comes from the guide camera's exposure row
unless phd2_tune_exp_ms is set."""
from photonscript.scheduler import phd2_audit as pa
from photonscript.scheduler import phd2_tuning as tn
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import guide_tuner as gt

ASI = "ZWO ASI174MM Mini"
GP = "OGMA GP678C"


def _cfg(tmp_path, **kw):
    gt._row_cache.clear()
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path, **kw)


def _tuned(filt, good, target="M27", t="2026-11-01T05:00:00Z"):
    return {"filter": filt, "target": target, "good_exposure_ms": good, "t_utc": t}


def _row(cfg, exp_ms, camera=GP, tuned=None):
    obs = {s: {} for s in pa.SOURCES}
    obs["api"] = {"exposure_ms": exp_ms, "camera": camera}
    if tuned is not None:
        obs["tuned"] = tuned
    res = pa.evaluate(pa.load_desired(cfg), obs, cfg)
    return next(r for r in res["rows"] if r["id"] == "exposure_ms")


def test_desired_file_still_lints_clean(tmp_path):
    d = pa.load_desired(_cfg(tmp_path))
    assert not d.get("lint"), d.get("lint")


def test_tuned_1500_ms_reads_pass_and_untuned_stays_warn(tmp_path):
    cfg = _cfg(tmp_path, phd2_tune_exp_ms="1000,4000")
    r = _row(cfg, 1500, tuned=_tuned("L", 1500))
    assert r["status"] == pa.PASS and "PS-90 tuner" in r["note"]
    assert "1000 to 4000" in r["desired"]
    # not what the tuner accepted: judged on the GP678C row (2 to 4 s)
    assert _row(cfg, 1500, tuned=_tuned("L", 2500))["status"] == pa.WARN
    assert _row(cfg, 1500)["status"] == pa.WARN
    assert _row(cfg, 3000)["status"] == pa.PASS


def test_tuned_value_outside_the_tuner_band_is_not_a_pass(tmp_path):
    # no explicit band: the tuner band is the camera row (ASI174MM 1.5 to 4 s)
    cfg = _cfg(tmp_path)
    r = _row(cfg, 1000, camera=ASI, tuned=_tuned("L", 1000))
    assert r["status"] == pa.WARN
    assert _row(cfg, 1500, camera=ASI, tuned=_tuned("L", 1500))["status"] == pa.PASS


def test_narrowband_6_s_on_ha_passes_with_the_nb_band(tmp_path):
    cfg = _cfg(tmp_path)          # phd2_tune_exp_ms_nb default 1000,8000
    r = _row(cfg, 6000, camera=ASI, tuned=_tuned("Ha", 6000))
    assert r["status"] == pa.PASS and "Ha" in r["note"]
    # the same 6 s on L is outside the broadband band
    assert _row(cfg, 6000, camera=ASI, tuned=_tuned("L", 6000))["status"] == pa.WARN


def test_tuner_mode_makes_the_row_not_applicable(tmp_path):
    cfg = _cfg(tmp_path, phd2_tune_mode="exposure")
    r = _row(cfg, 1000)
    assert r["status"] == pa.WARN and not r["applicable"]
    assert "left to the PS-90 tuner" in r["note"]
    cfg2 = _cfg(tmp_path / "b")
    assert _row(cfg2, 1000)["applicable"]


async def test_apply_does_not_touch_exposure_in_tuner_mode(tmp_path):
    cfg = _cfg(tmp_path, phd2_tune_mode="exposure")
    audit = {"rows": [{"id": "exposure_ms", "status": pa.WARN, "apply": "api",
                       "target": 3000, "current": "1500"}],
             "camera": {"id": "gp678c"}}

    class NoClient:     # any PHD2 call would fail the test
        def __getattr__(self, name):
            raise AssertionError(f"PHD2 touched: {name}")
    res = await pa.apply(cfg, ["exposure_ms"], dry_run=False, client=NoClient(),
                         audit=audit)
    x = res["results"][0]
    assert res["ok"] and x["skipped"] == "tuner" and "left to the PS-90 tuner" in x["note"]
    assert not store.read_jsonl(pa.changes_path(cfg))


def test_tuner_band_follows_the_camera_row(tmp_path):
    cfg = _cfg(tmp_path)
    assert gt.exposure_band(cfg) == (1000.0, 4000.0)          # no audit yet
    pa.save(cfg, {"t_utc": "2026-11-01T05:00:00Z", "night": "2026-10-31",
                  "rows": [], "camera": {"id": "asi174mm_mini"}})
    gt._row_cache.clear()
    assert gt.exposure_band(cfg) == (1500.0, 4000.0)
    c = gt.tune_cfg(cfg)
    assert (c.exp_lo, c.exp_hi) == (1500, 4000)
    c = gt.tune_cfg(cfg, "Ha")
    assert (c.exp_lo, c.exp_hi) == (1000, 8000)               # NB band kept
    # an explicit phd2_tune_exp_ms still wins
    cfg2 = PhotonScriptConfig(_env_file=None, data_dir=tmp_path, phd2_tune_exp_ms="1000,3000")
    assert gt.exposure_band(cfg2) == (1000.0, 3000.0)


def test_last_accepted_is_the_newest_entry(tmp_path):
    cfg = _cfg(tmp_path)
    assert tn.last_accepted(cfg) is None
    store.write_json(tn.tune_path(cfg), {"entries": {
        "a": {"target": "M27", "filter": "L", "good_exposure_ms": 2000,
              "t_utc": "2026-11-01T04:00:00Z"},
        "b": {"target": "M27", "filter": "Ha", "good_exposure_ms": 6000,
              "t_utc": "2026-11-01T05:00:00Z"},
        "c": {"target": "M1", "filter": "L", "t_utc": "2026-10-30T05:00:00Z"}}})
    got = tn.last_accepted(cfg)
    assert got["filter"] == "Ha" and got["good_exposure_ms"] == 6000
