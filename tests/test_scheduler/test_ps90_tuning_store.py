"""PS-90 tuning memory and next-night advice: per-filter recall (remembered,
predicted from the L flux ratio, typical), recommend() (more gain when a
filter is faint at 4 s, less when one clips at 1 s, the bin 3 HFD check), the
PS-89 audit's gain row fed by it, the pre-dusk writer's gates, the per-filter
guide table from the 2026-09-26 fixture, and the API / config wiring."""
from pathlib import Path

import pytest

from photonscript.scheduler import phd2_analysis as pa
from photonscript.scheduler import phd2_audit
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler import phd2_profile_store as ps
from photonscript.scheduler import phd2_tuning as tn
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "phd2"
N26 = "PHD2_GuideLog_2026-09-26_120453.txt"
SETUP = {"profile": "RC16 OAG", "binning": 2, "gain": 100}


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path, **kw)


def _m(cfg, target, filt, exp_ms, amp, hfd=6.8, clipped=False, in_band=None, **kw):
    peak = amp + 0.008
    rec = dict(SETUP, target=target, filter=filt, exposure_ms=exp_ms,
               peak_frac=peak, amp_frac=amp, snr=40.0, hfd_px=hfd, clipped=clipped,
               in_band=(0.6 <= peak <= 0.8 and not clipped) if in_band is None else in_band)
    rec.update(kw)
    return tn.record(cfg, rec)


def test_record_and_recall_per_filter(tmp_path):
    cfg = _cfg(tmp_path)
    _m(cfg, "M27", "L", 1000, 0.69)                 # in band: remembered
    _m(cfg, "M27", "Ha", 4000, 0.10)                # Ha 0.025/ms vs L 0.69/ms
    _m(cfg, "M57", "L", 2000, 0.40)
    hit = tn.recall(cfg, "RC16 OAG", 2, 100, "M27", "L")
    assert hit == {"exposure_ms": 1000, "source": "remembered"}
    # M57 Ha: predicted from M57 L and the M27 Ha/L ratio
    r = tn.filter_ratios(list(tn.load(cfg)["entries"].values()))
    assert r["Ha"] == pytest.approx((0.10 / 4000) / (0.69 / 1000), rel=1e-3)
    hit = tn.recall(cfg, "RC16 OAG", 2, 100, "M57", "Ha")
    assert hit["source"].startswith("predicted from L")
    want = (0.70 - 0.008) / ((0.40 / 2000) * r["Ha"])
    assert hit["exposure_ms"] == pytest.approx(want, rel=0.02)
    # a new target: the typical Ha star
    assert tn.recall(cfg, "RC16 OAG", 2, 100, "NGC 7000", "Ha")["source"].startswith("typical Ha")
    # another gain is another setup
    assert tn.recall(cfg, "RC16 OAG", 2, 50, "M27", "L") is None
    e = tn.load(cfg)["entries"][tn.entry_key("RC16 OAG", 2, 100, "M27", "L")]
    assert e["n"] == 1 and e["good_exposure_ms"] == 1000
    night = store.night_of(cfg)
    assert len(tn.night_records(cfg, night)) == 3


def test_recommend_more_gain_when_faint_at_the_longest_exposure(tmp_path):
    # the PS-90 band (4 s) on every filter; PS-85's narrowband band is tested
    # in test_ps85_guide_blocks.py
    cfg = _cfg(tmp_path, phd2_tune_exp_ms_nb="")
    _m(cfg, "M27", "L", 1000, 0.50)
    _m(cfg, "M27", "OIII", 4000, 0.20)     # 0.20 at 4000: faint, wants 3.5x
    rec = tn.recommend(cfg)
    assert rec["by_filter"]["OIII"]["verdict"].startswith("faint")
    assert rec["by_filter"]["L"]["verdict"].startswith("exposure alone")
    # already at gain 100 (PHD2's max): stays, and says bin 3 is next
    assert rec["gain"] == 100 and not rec["change"] and "bin 3" in rec["note"]
    tn.record(cfg, dict(SETUP, gain=40, target="M27", filter="OIII", exposure_ms=4000,
                        peak_frac=0.208, amp_frac=0.20, hfd_px=6.8))
    rec = tn.recommend(cfg)
    assert rec["current"]["gain"] == 40 and rec["change"] and rec["gain"] == 100


def test_recommend_less_gain_when_clipped_at_the_shortest(tmp_path):
    cfg = _cfg(tmp_path)
    _m(cfg, "Vega field", "L", 1000, 0.95)
    rec = tn.recommend(cfg)
    assert rec["change"] and rec["gain"] < 100
    assert "clips" in rec["note"]


def test_recommend_without_data_and_the_bin3_check(tmp_path):
    cfg = _cfg(tmp_path)
    assert tn.recommend(cfg) == {"ok": False, "change": False,
                                 "note": "no tuning measurements yet"}
    _m(cfg, "M27", "L", 2000, 0.69, hfd=6.8)
    b = tn.recommend(cfg)["bin3"]
    # 09-26: HFD 6.8 px at bin 2 -> about 4.5 px at bin 3, inside 2..5
    assert b["hits_target_now"] is False and b["bin3_hits_target"] is True
    assert b["hfd_px_at_bin3"] == pytest.approx(4.53, abs=0.01)
    assert tn.recommend(cfg)["binning"] == 2      # approved: stay at bin 2


def test_audit_gain_row_uses_the_recommendation(tmp_path):
    cfg = _cfg(tmp_path)
    desired = phd2_audit.load_desired(cfg)
    assert not desired.get("lint")
    row = next(r for r in desired["check"] if r["id"] == "gain")
    assert row["computed"] == "guide_gain" and row["apply"] == "profile"
    obs = {"api": {}, "log": {"gain": 100}, "profile": {}}
    out = phd2_audit.evaluate_row(row, dict(obs, tuning={"ok": False, "note": "none"}),
                                  cfg, None)
    assert out["status"] == "info"
    out = phd2_audit.evaluate_row(row, dict(obs, tuning={"ok": True, "gain": 100,
                                                          "note": "keep"}), cfg, None)
    assert out["status"] == "pass"
    out = phd2_audit.evaluate_row(row, dict(obs, tuning={"ok": True, "gain": 60,
                                                          "note": "clips"}), cfg, None)
    assert out["status"] == "warn" and out["target"] == 60 and out["applicable"]


def test_predusk_writer_gates(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _m(cfg, "Vega field", "L", 1000, 0.95)          # recommends less gain
    res = tn.apply_predusk(cfg)
    assert not res["ok"] and "phd2_audit_autofix is off" in res["note"]
    cfg = _cfg(tmp_path, phd2_audit_autofix=True)
    monkeypatch.setattr(ps, "phd2_running", lambda: True)
    assert "PHD2 is running" in tn.apply_predusk(cfg)["note"]
    monkeypatch.setattr(ps, "phd2_running", lambda: False)
    monkeypatch.setattr(ps, "resolve_id", lambda pid=None, name=None: "1")
    monkeypatch.setattr(ps, "backup", lambda c, pid: {"ok": True, "reg": str(tmp_path / "b.reg")})
    calls = {}

    def _write(c, pid, changes, backup_path=None):
        calls.update(changes)
        return {"ok": False, "written": {}, "refused": {"gain": "registry name unverified"}}
    monkeypatch.setattr(ps, "write", _write)
    res = tn.apply_predusk(cfg)
    assert calls == {"gain": res["recommend"]["gain"]} and not res["ok"]
    assert "unverified" in res["note"]
    # no change wanted: nothing written
    tn.record(cfg, dict(SETUP, target="M27", filter="L", exposure_ms=2000,
                        peak_frac=0.7, amp_frac=0.69, hfd_px=6.8))
    monkeypatch.setattr(tn, "recommend", lambda c, nights=7: {"ok": True, "change": False,
                                                              "note": "keep"})
    assert tn.apply_predusk(cfg) == {"recommend": {"ok": True, "change": False,
                                                   "note": "keep"},
                                     "written": {}, "ok": True, "note": "keep"}


def test_summary_table_and_changes(tmp_path):
    cfg = _cfg(tmp_path)
    _m(cfg, "M27", "L", 2000, 0.69)
    _m(cfg, "M27", "L", 2000, 0.99, clipped=True)
    tn.record_change(cfg, {"kind": "exposure", "from": 2000, "to": 1500, "ok": True,
                           "reason": "clipped"})
    s = tn.summary(cfg, store.night_of(cfg))
    assert s["mode"] == "observe" and s["measurements"] == 2
    assert s["by_filter"]["L"]["clipped"] == 1
    assert s["changes"][0]["to"] == 1500 and s["last_change"]
    # the change also lands in the PS-89 change log
    ch = store.read_jsonl(phd2_audit.changes_path(cfg))
    assert ch[-1]["kind"] == "tune" and ch[-1]["to"] == 1500


def test_per_filter_guide_table_from_the_0926_fixture():
    secs = pl.parse_guide_log((FIX / N26).read_text(encoding="utf-8"), N26)
    subs = [{"rig": "rc16", "file": "LIGHT\\2026-09-26_22-05-22__O_300.00s_0001.fits",
             "exp_s": 300.0, "passed_qa": False, "filter": "OIII"},
            {"rig": "rc16", "file": "LIGHT\\2026-09-26_20-18-28__H_60.00s_0002.fits",
             "exp_s": 60.0, "passed_qa": True, "filter": "Ha"}]
    a = pa.analyze_sections(secs, PhotonScriptConfig(_env_file=None),
                            date="2026-09-26", subs=subs)
    t = a["per_sub"]["summary"]["rc16_by_filter"]
    assert t["Ha"]["guide_exposure_ms"] == 2000 and t["OIII"]["guide_exposure_ms"] == 5000
    assert t["Ha"]["saturated_pct"] == 100.0           # every frame ErrorCode 1
    assert 3.0 < t["Ha"]["star_hfd_px"] < 8.0
    assert t["Ha"]["median_snr"] is not None
    assert "saturated" in pa.format_report(a)


def test_router_and_config_fields(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as appmod
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(appmod, "get_config", lambda: cfg)
    _m(cfg, "M27", "L", 2000, 0.69)
    r = TestClient(appmod.app).get("/api/phd2/tuning")
    assert r.status_code == 200
    d = r.json()
    assert d["by_filter"]["L"]["measurements"] == 1 and d["recommend"]["ok"]
    by_env = {f[1]: f for f in appmod._CONFIG_FIELDS}
    for env, attr, default in (("PS_PHD2_TUNE_MODE", "phd2_tune_mode", "observe"),
                               ("PS_PHD2_TUNE_PEAK_LO", "phd2_tune_peak_lo", 0.60),
                               ("PS_PHD2_TUNE_PEAK_HI", "phd2_tune_peak_hi", 0.80),
                               ("PS_PHD2_TUNE_SNR_MIN", "phd2_tune_snr_min", 20.0),
                               ("PS_PHD2_TUNE_HFD_PX", "phd2_tune_hfd_px", "2,5"),
                               ("PS_PHD2_TUNE_EXP_MS", "phd2_tune_exp_ms", "1000,4000"),
                               ("PS_PHD2_GUIDE_FULL_SCALE_ADU", "phd2_guide_full_scale_adu",
                                65535)):
        assert by_env[env][0] == attr
        assert getattr(PhotonScriptConfig(_env_file=None), attr) == default


async def test_armer_predusk_hook_once_per_night(tmp_path, monkeypatch):
    from photonscript.scheduler.armer import Armer
    a = Armer.__new__(Armer)
    a.config = _cfg(tmp_path)
    a.plan = {"night_of": "2026-10-02"}
    a.state = "ARMED"
    a._use_guiding = lambda: True
    seen = []

    def _apply(cfg, state):
        seen.append(state)
        return {"ok": False, "note": "PHD2 is running: ..." if len(seen) == 1 else "off",
                "written": {}}
    monkeypatch.setattr(tn, "apply_predusk", _apply)
    await a._maybe_predusk_tune()        # PHD2 running: tried again next tick
    await a._maybe_predusk_tune()
    await a._maybe_predusk_tune()        # done for the night
    assert seen == ["ARMED", "ARMED"]
