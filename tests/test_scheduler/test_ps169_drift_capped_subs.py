"""PS-169: the unguided sub length capped from the measured tracking drift
(max sub = smear budget x rig scale / drift rate), never above the existing
caps, never below the floor, rounded to a dark length the night fills; the
PS-156 fallback uses it in tracking_drift_cap_mode auto, observe only says."""
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import guide_fallback as gf
from photonscript.scheduler import tracking_drift as td
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "phd2"
N07 = "PHD2_GuideLog_2026-10-07_201941.txt"


@pytest.fixture(autouse=True)
def _clear_cache():
    td._CACHE.clear()
    td._darks_cache.clear()
    yield
    td._CACHE.clear()
    td._darks_cache.clear()


def _cfg(tmp_path=None, logs=(), **kw):
    if tmp_path is not None:
        d = tmp_path / "phd2logs"
        d.mkdir(exist_ok=True)
        for n in logs:
            shutil.copy(FIX / n, d / n)
        kw.setdefault("data_dir", tmp_path / "data")
        kw.setdefault("phd2_logs_dir", str(d))
    kw.setdefault("piggyback_enabled", True)
    kw.setdefault("guide_fallback_test_date", "")
    return PhotonScriptConfig(_env_file=None, **kw)


# ---- the formula ------------------------------------------------------------------

def test_ticket_examples():
    cfg = _cfg()
    rc = td.recommend(cfg, "rc16", 0.5, darks=[])
    assert rc["drift_max_s"] == pytest.approx(1.5 * 0.236 / 0.5 * 60, abs=0.1)   # 42.5
    assert rc["exposure_s"] == 40 and rc["dark_match"] is False
    assert rc["why"] == "drift budget"
    pg = td.recommend(cfg, "piggyback", 0.5, darks=[], cap_s=300)
    assert pg["drift_max_s"] == pytest.approx(232.2, abs=0.1)
    assert pg["exposure_s"] == 230


def test_rounds_down_to_a_dark_length_in_range():
    cfg = _cfg()
    r = td.recommend(cfg, "rc16", 0.2, darks=[30, 60, 90, 180, 300, 600])   # 106 s
    assert r["exposure_s"] == 90 and r["dark_match"] is True
    assert r["smear_px"] <= 1.5


def test_never_above_the_cap_never_below_the_floor():
    cfg = _cfg()
    slow = td.recommend(cfg, "rc16", 0.01, darks=[180, 300, 600])
    assert slow["exposure_s"] == 300 and slow["why"] == "existing cap"
    pg = td.recommend(cfg, "piggyback", 0.5, darks=[120])        # cap: piggyback_exposure_s
    assert pg["cap_s"] == 120 and pg["exposure_s"] == 120 and pg["dark_match"] is True
    fast = td.recommend(cfg, "rc16", 5.0, darks=[60, 180])       # 4 s raw
    assert fast["exposure_s"] == 30 and fast["why"] == "floor"
    floor = td.recommend(_cfg(tracking_sub_floor_s=45), "rc16", 5.0, darks=[])
    assert floor["exposure_s"] == 45
    none = td.recommend(cfg, "rc16", None)
    assert none["exposure_s"] is None and none["why"] == "no drift measurement"


def test_budget_from_config():
    r = td.recommend(_cfg(tracking_smear_budget_px=3.0), "rc16", 0.5, darks=[])
    assert r["drift_max_s"] == pytest.approx(85, abs=0.1)


def test_dark_lengths_come_from_the_night_quota(tmp_path):
    cfg = _cfg(tmp_path, dark_exposures="600,180,35")
    assert td.dark_lengths(cfg, "rc16") == [35.0, 180.0, 300.0, 600.0]
    r = td.recommend(cfg, "rc16", 0.5)
    assert r["exposure_s"] == 35 and r["dark_match"] is True   # 42.5 s max: 35 s dark


# ---- which drift: tonight's last hour, else last night ----------------------------

def test_live_drift_tonight_then_last_night(tmp_path):
    cfg = _cfg(tmp_path, logs=(N07,))
    # 10-08 01:30 local: the 01:03:36 session ended 01:17 -> tonight's
    d = td.live_drift(cfg, datetime(2026, 10, 8, 1, 30))
    assert d["night"] == "2026-10-07" and d["source"].startswith("tonight, 1 session")
    assert d["total_arcsec_min"] == pytest.approx(0.77, abs=0.02)
    # 03:00: nothing in the last 60 min (04:30 is in the future) -> last night: none
    assert td.live_drift(cfg, datetime(2026, 10, 8, 3, 0)) is None
    # the next evening: last night's summary
    d = td.live_drift(cfg, datetime(2026, 10, 8, 21, 0))
    assert d["night"] == "2026-10-07" and d["source"].startswith("last night")
    # a shorter window drops tonight's session
    cfg2 = _cfg(tmp_path, logs=(N07,), tracking_drift_recent_min=5)
    assert td.live_drift(cfg2, datetime(2026, 10, 8, 1, 30)) is None


# ---- the PS-156 fallback ----------------------------------------------------------

def _rec(s=40.0, rate=0.5):
    return {"exposure_s": s, "rate_arcsec_min": rate}


def test_cap_length_modes():
    assert td.cap_length(_cfg(tracking_drift_cap_mode="auto"), 60, _rec()) == (
        40.0, 'drift 0.5"/min caps at 40 s')
    s, note = td.cap_length(_cfg(), 60, _rec())                  # default observe
    assert s == 60 and note.startswith("observe: drift")
    assert td.cap_length(_cfg(tracking_drift_cap_mode="off"), 60, _rec()) == (60, None)
    assert td.cap_length(_cfg(tracking_drift_cap_mode="auto"), 30, _rec()) == (30, None)
    assert td.cap_length(_cfg(tracking_drift_cap_mode="auto"), 60, None) == (60, None)


def test_fallback_lengths_capped_in_auto_only():
    auto = _cfg(tracking_drift_cap_mode="auto")
    lens = gf.lengths(auto, ["L", "Ha"], rec=_rec())
    assert lens["L"][0] == 40.0 and "drift" in lens["L"][1]
    assert lens["Ha"][0] == 40.0
    obs = gf.lengths(_cfg(), ["L", "Ha"], rec=_rec())
    assert obs["L"][0] == 60 and obs["Ha"][0] == 300
    assert "observe: drift" in obs["L"][1]
    assert "observe" in gf.plan_text(obs)
    # never longer than the existing cap
    long = gf.lengths(auto, ["L"], rec=_rec(s=200))
    assert long["L"][0] == 60


def test_fallback_without_drift_is_unchanged(tmp_path):
    cfg = _cfg(tmp_path, tracking_drift_cap_mode="auto")         # no guide logs
    assert gf.drift_rec(cfg) is None
    assert gf.lengths(cfg, ["L"]) == {"L": (60.0, "config")}


def test_drift_rec_off_and_failure(monkeypatch, tmp_path):
    assert gf.drift_rec(_cfg(tracking_drift_cap_mode="off")) is None

    def boom(*a, **k):
        raise RuntimeError("log locked")
    monkeypatch.setattr(td, "live_drift", boom)
    assert gf.drift_rec(_cfg(tmp_path)) is None


def test_cap_targets_uses_the_drift_cap(tmp_path, monkeypatch):
    from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
    cfg = _cfg(tmp_path, tracking_drift_cap_mode="auto")
    monkeypatch.setattr(gf, "drift_rec", lambda c: _rec(s=45.0))
    t = NinaSequenceTarget(name="M31", ra_hours=0.7, dec_degrees=41.3, exposures=[
        ExposurePlan(filter_type=FilterType("L"), exposure_seconds=180, count=10)])
    notes = gf.cap_targets(cfg, [t])
    assert t.exposures[0].exposure_seconds == 45 and t.exposures[0].count == 40
    assert "drift" in notes[0]


# ---- API + System page + config ---------------------------------------------------

def test_api_and_system_page(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    cfg = _cfg(tmp_path, logs=(N07,))
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(td, "live_drift", lambda c, now=None: {
        "total_arcsec_min": 0.5, "source": "last night (test)"})
    d = TestClient(app_mod.app).get("/api/tracking/drift?date=2026-10-07&nights=1").json()
    rec = d["recommended"]
    assert rec["mode"] == "observe" and set(rec["rigs"]) == {"rc16", "piggyback"}
    assert rec["rigs"]["rc16"]["drift_max_s"] == pytest.approx(42.5, abs=0.1)
    html = (Path(app_mod.__file__).parent / "templates" / "system.html").read_text(
        encoding="utf-8")
    assert "driftSubPanel" in html and "/api/tracking/drift" in html


def test_config_defaults_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.tracking_drift_cap_mode == "observe"
    assert (c.tracking_smear_budget_px, c.tracking_sub_floor_s,
            c.tracking_drift_recent_min) == (1.5, 30.0, 60.0)
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    for env in ("PS_TRACKING_DRIFT_CAP_MODE", "PS_TRACKING_SMEAR_BUDGET_PX",
                "PS_TRACKING_SUB_FLOOR_S", "PS_TRACKING_DRIFT_RECENT_MIN"):
        assert hasattr(c, by_env[env][0])
    assert td.cap_mode(PhotonScriptConfig(_env_file=None, tracking_drift_cap_mode="bogus")) == "observe"
