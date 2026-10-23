"""Fix 2026-10-13: the last "tonight" callers that took the night from the
UTC date (from 18:00 MDT the UTC date is already tomorrow): /api/tonight,
tpoint_mapping.start_time() and `photonscript plan`. They now use
localtime.night_window() (night_base_date, noon to noon local, rolled at
dawn). Replayed at 17:57, 18:02 and 00:04 local on the 10-12/13 night."""
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import tpoint_mapping as tm
from photonscript.shared.astronomy import get_twilight_times
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.localtime import local_evening_utc, night_window

AT_1757 = datetime(2026, 10, 12, 23, 57)   # 17:57 MDT 10-12
AT_1802 = datetime(2026, 10, 13, 0, 2)     # 18:02 MDT 10-12 (UTC 10-13)
AT_0004 = datetime(2026, 10, 13, 6, 4)     # 00:04 MDT 10-13
TIMES = [AT_1757, AT_1802, AT_0004]


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data")


def _night(cfg, day):
    return get_twilight_times(cfg.get_observatory(), datetime(2026, 10, day))


@pytest.mark.parametrize("now", TIMES)
def test_night_window_is_the_10_12_night(tmp_path, now):
    cfg = _cfg(tmp_path)
    base, tw = night_window(cfg, now)
    assert base == datetime(2026, 10, 12)
    assert tw == _night(cfg, 12)
    assert tw["astro_dark_start"] < tw["astro_dark_end"]


def test_night_window_rolls_after_dawn(tmp_path):
    cfg = _cfg(tmp_path)
    dawn = _night(cfg, 12)["astro_dark_end"]
    base, tw = night_window(cfg, dawn + timedelta(minutes=5))   # before local noon
    assert base == datetime(2026, 10, 13) and tw == _night(cfg, 13)


def test_local_evening_utc(tmp_path):
    cfg = _cfg(tmp_path)
    assert local_evening_utc(cfg, datetime(2026, 10, 12)) == datetime(2026, 10, 13, 2, 0)
    assert local_evening_utc(cfg, datetime(2026, 12, 12)) == datetime(2026, 12, 13, 3, 0)


# --- tpoint_mapping.start_time ----------------------------------------------------

def test_tpoint_start_time(tmp_path):
    cfg = _cfg(tmp_path)
    dusk = _night(cfg, 12)["astro_dark_start"]
    assert tm.start_time(cfg, now=AT_1757) == dusk
    # was: the UTC date's (10-13) dusk, a day late
    assert tm.start_time(cfg, now=AT_1802) == dusk
    assert tm.start_time(cfg, now=AT_0004) == AT_0004          # dark already: now


# --- /api/tonight ------------------------------------------------------------------

@pytest.mark.parametrize("now", TIMES)
def test_api_tonight(tmp_path, monkeypatch, now):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_stored_projects", lambda: {})
    seen = {}

    def _rank(targets, obs, date_utc, *a, **k):
        seen["rank"] = date_utc
        return []

    def _plan(projects, config, date_utc=None):
        seen["plan"] = date_utc
        return []
    monkeypatch.setattr(app, "rank_targets_for_night", _rank)
    monkeypatch.setattr(app, "plan_night_sequence", _plan)

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return now
    monkeypatch.setattr(app, "datetime", _DT)
    r = TestClient(app.app).get("/api/tonight")
    assert r.status_code == 200, r.text
    body = r.json()
    tw = _night(cfg, 12)
    assert body["date"] == "2026-10-12"
    assert body["twilight"]["astro_dark_start"] == tw["astro_dark_start"].isoformat()
    assert body["twilight"]["astro_dark_end"] == tw["astro_dark_end"].isoformat()
    assert seen["rank"] == datetime(2026, 10, 12) and seen["plan"] == now


# --- photonscript plan -------------------------------------------------------------

@pytest.mark.parametrize("now", TIMES)
def test_cli_plan(tmp_path, monkeypatch, now):
    import photonscript.cli as cli
    from photonscript.scheduler import target_planner
    from photonscript.shared import config as config_mod
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(config_mod, "PhotonScriptConfig", lambda *a, **k: cfg)
    seen = {}

    def _plan(projects, config, date_utc=None):
        seen["plan"] = date_utc
        return []
    monkeypatch.setattr(target_planner, "plan_night_sequence", _plan)
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a))

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return now
    monkeypatch.setattr(cli, "datetime", _DT)
    cli.plan(month=0, date="")
    assert seen["plan"] == now
    head = printed[0][0].renderable
    assert "October 12, 2026" in head
    assert str(_night(cfg, 12)["astro_dark_start"]) in head


def test_cli_plan_with_a_date(tmp_path, monkeypatch):
    import photonscript.cli as cli
    from photonscript.scheduler import target_planner
    from photonscript.shared import config as config_mod
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(config_mod, "PhotonScriptConfig", lambda *a, **k: cfg)
    seen = {}

    def _plan(projects, config, date_utc=None):
        seen["plan"] = date_utc
        return []
    monkeypatch.setattr(target_planner, "plan_night_sequence", _plan)
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: None)
    cli.plan(month=0, date="2026-10-12")
    # 20:00 MDT 10-12: inside the 10-12 night (night_base_date gives 10-12)
    assert seen["plan"] == datetime(2026, 10, 13, 2, 0)
