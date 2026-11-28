"""2026-11-21 Targets page refactor (scheduler/target_board.py).

Jeremy: "fonts a little bit smaller ... see what the campaign [calls for],
and then underneath, show where it is in the sky over the night ... active
projects ... projects which are done ... a whole set of test ones ...
expandable, collapsible, or hideable."

Pinned: the section of every card (active / paused / done / test), the
tonight line (planned window and sets from the /api/tonight payload, or why
not), the campaign line, the dusk-to-dawn altitude series, the two
endpoints and the page's sections + collapsed-by-default Tests.
"""
from datetime import datetime

import pytest

from photonscript.scheduler import target_board as tb
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

# 2026-11-21 at AARO (MST): astro dark 01:36Z to 12:26Z
DUSK, DAWN = "2026-11-22T01:36:10", "2026-11-22T12:26:13"


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _card(name, *, pid="p", active=True, pct=40, unattr=False, key=None):
    return {"name": name, "key": key or name.lower().replace(" ", ""),
            "unattributed": unattr, "project_id": pid, "active": active,
            "goal": {"pct": pct} if pid else None}


def _proj(pid, name, ra, dec, active=True, min_alt=None):
    return ImagingProject(
        id=pid, active=active, priority=50, min_alt_deg=min_alt,
        target=CelestialTarget(name=name, ra_hours=ra, dec_degrees=dec),
        exposure_plans=[ExposurePlan(filter_type=FilterType.HA,
                                     exposure_seconds=300, count=20)])


# --- sections -------------------------------------------------------------------

@pytest.mark.parametrize("card,section", [
    (_card("?", pid=None, unattr=True), "test"),
    (_card("Tracking test East (Owl Cluster)", pid=None), "test"),
    (_card("Optics test M 31 tilt", pid=None), "test"),
    (_card("Focus calibration NGC 7000", pid=None), "test"),
    (_card("TPoint mapping 60 points", pid=None), "test"),
    (_card("Guider commission Cave", pid=None), "test"),
    (_card("M 27", pid=None), "done"),               # imaged, no goal
    (_card("Heart Nebula", active=False, pct=100), "done"),
    (_card("NGC 604", active=True, pct=100), "done"),
    (_card("Pacman Nebula", active=True, pct=36), "active"),
    (_card("Crescent Nebula", active=False, pct=19), "paused"),
    (_card("Triangulum P1", active=False, pct=0), "paused"),
])
def test_section_of(card, section):
    assert tb.section_of(card)[0] == section


def test_annotate_and_counts():
    cards = tb.annotate([_card("Pacman Nebula"), _card("M 27", pid=None),
                         _card("Tracking test Pleiades", pid=None),
                         _card("Crab", active=False, pct=0)])
    assert [c["section"] for c in cards] == ["active", "done", "test",
                                             "paused"]
    assert all(c["section_why"] for c in cards)
    assert tb.section_counts(cards) == {"active": 1, "paused": 1, "done": 1,
                                        "test": 1}


# --- the tonight and campaign lines -------------------------------------------------

def _exp(f, n, s=300.0, rig="rc16"):
    return {"filter_type": f, "count": n, "exposure_seconds": s, "rig": rig}


def test_fmt_sets_shared_and_mixed_lengths():
    assert tb._fmt_sets([_exp("Ha", 7), _exp("OIII", 11), _exp("SII", 15)]) \
        == "Ha 7 / OIII 11 / SII 15 x 300 s"
    assert tb._fmt_sets([_exp("L", 10, 180), _exp("R", 6, 120),
                         _exp("G", 0, 120)]) == "L 10x180 s / R 6x120 s"
    assert tb._fmt_sets([]) == ""


def test_tonight_line_planned_window_in_local_time(tmp_path):
    cfg = _cfg(tmp_path)
    plan = {"name": "Pacman Nebula",
            "window_start_utc": "2026-11-22T04:56:00Z",
            "window_end_utc": "2026-11-22T08:17:00Z",
            "exposures": [_exp("Ha", 7), _exp("OIII", 11), _exp("SII", 15),
                          _exp("OSC", 40, 120, rig="piggyback")]}
    ln = tb.tonight_line(cfg, _card("Pacman Nebula"), plan, None)
    assert ln["status"] == "planned"
    assert ln["text"] == ("Tonight: RC16 21:56-01:17 MST, Ha 7 / OIII 11 / "
                          "SII 15 x 300 s; Piggy-600 OSC 40 x 120 s")


def _camp(**kw):
    c = {"status": "active", "returns": None, "priority": 50,
         "remaining_h": 5.0, "nb_remaining_h": 5.0, "bb_remaining_h": 0.0,
         "eta": "2026-11-23", "tonight_h": 0.0, "tonight_rigs": [],
         "horizon_h": 5.0, "nights": 2, "horizon_days": 14}
    c.update(kw)
    return c


@pytest.mark.parametrize("card,camp,kw,want", [
    (_card("X", active=False), None, {}, "goal paused"),
    (_card("X", pct=100), None, {}, "goal met"),
    (_card("X"), _camp(), {"paused_tonight": True}, "paused for tonight"),
    (_card("X"), _camp(status="parked", returns="2027-01-05"), {},
     "out of season, returns Jan 5"),
    (_card("X"), _camp(status="lights_done_needs_calibration"), {},
     "lights done"),
    (_card("X"), _camp(), {"unguided": {"filters": ["Ha", "OIII"]}},
     "Ha/OIII needs guiding; tonight is unguided"),
    (_card("X"), _camp(), {"up_h": 0.0}, "below 30 deg all night"),
    (_card("X"), _camp(tonight_h=0.4), {"up_h": 3.0},
     "campaign has 0.4 h, the planner filled the night with others"),
    (_card("X"), _camp(nb_remaining_h=0.0, bb_remaining_h=1.4),
     {"up_h": 7.0, "night": {"moon": {"illum_pct": 92, "moon_free_h": 0.7}}},
     "moon 92%, broadband left (0.7 h moon-free went elsewhere)"),
    (_card("X"), _camp(), {"up_h": 7.0},
     "higher-priority goals fill its hours"),
])
def test_tonight_line_reasons(tmp_path, card, camp, kw, want):
    card["section"] = tb.section_of(card)[0]
    ln = tb.tonight_line(_cfg(tmp_path), card, None, camp, **kw)
    assert ln["text"] == "Not tonight: " + want and ln["reason"] == want


def test_campaign_summary_and_text():
    goal = {"id": "g", "name": "Soul Nebula", "status": "active",
            "priority": 45, "goal_hours": 8.1, "hours_done": 0.0,
            "nb_remaining_h": 8.1, "bb_remaining_h": 0.0, "eta": "2026-11-23",
            "driving_rig": "rc16", "min_alt_deg": 30.0}
    camp = {"nights": [
        {"date": "2026-11-21", "assigned": [
            {"goal": "Soul Nebula", "rig": "rc16", "hours": 1.69},
            {"goal": "Pacman Nebula", "rig": "rc16", "hours": 2.81}]},
        {"date": "2026-11-22", "assigned": [
            {"goal": "Soul Nebula", "rig": "rc16", "hours": 2.06}]},
        {"date": "2026-11-23", "assigned": []}]}
    c = tb.campaign_summary(goal, camp, "2026-11-21")
    assert (c["tonight_h"], c["horizon_h"], c["nights"], c["remaining_h"],
            c["horizon_days"]) == (1.7, 3.8, 2, 8.1, 3)
    assert tb.campaign_text(c) == ("Campaign: 8.1 h left, 3.8 h planned on 2 "
                                   "of the next 3 nights, ETA Nov 23, p45")
    assert tb.campaign_text({**c, "status": "complete"}) == \
        "Campaign: lights done"
    assert tb.campaign_text({**c, "eta": None, "horizon_h": 0.0}) == (
        "Campaign: 8.1 h left, no slot in the next 3 nights, ETA past 3 "
        "nights, p45")
    assert tb.campaign_summary(None, camp, "2026-11-21") is None


# --- the board -------------------------------------------------------------------------

def _tonight(**kw):
    t = {"date": "2026-11-21",
         "twilight": {"astro_dark_start": DUSK, "astro_dark_end": DAWN},
         "targets": [{"name": "Pacman Nebula",
                      "window_start_utc": "2026-11-22T05:46:40Z",
                      "window_end_utc": "2026-11-22T08:45:02Z",
                      "exposures": [_exp("Ha", 6), _exp("OIII", 9)]}],
         "rc16_nb_unguided": []}
    t.update(kw)
    return t


def test_build_board(tmp_path):
    cfg = _cfg(tmp_path)
    projects = [_proj("p1", "Pacman Nebula", 0.878, 56.63),
                _proj("p2", "Soul Nebula", 2.85, 60.4, min_alt=40),
                _proj("p3", "Crescent Nebula", 20.2, 38.35, active=False),
                _proj("p4", "Southern Thing", 12.0, -80.0)]
    camp = {"nights": [{"date": "2026-11-21", "moon": {"illum_pct": 10},
                        "assigned": [{"goal": "Soul Nebula", "rig": "rc16",
                                      "hours": 1.7,
                                      "start": "2026-11-22T09:38Z",
                                      "end": "2026-11-22T11:38Z"}]}],
            "goals": [{"id": "p1", "name": "Pacman Nebula", "status": "active",
                       "priority": 50, "nb_remaining_h": 5.1,
                       "bb_remaining_h": 0.0, "eta": "2026-11-22"},
                      {"id": "p2", "name": "Soul Nebula", "status": "active",
                       "priority": 45, "nb_remaining_h": 8.1,
                       "bb_remaining_h": 0.0, "eta": "2026-11-23"}]}
    b = tb.build(cfg, projects, _tonight(), camp,
                 now=datetime(2026, 11, 22, 3, 0))
    assert b["tz"] == "MST" and (b["dusk_local"], b["dawn_local"]) == \
        ("18:36", "05:26")
    n = len(b["moon"])
    assert n == 45                    # 10.8 h at 15 min, dusk and dawn included
    assert 0.1 < b["now_frac"] < 0.15
    assert [t["label"] for t in b["ticks"]] == ["20", "22", "00", "02", "04"]
    pac = b["targets"]["pacmannebula"]
    assert len(pac["alts"]) == n and pac["floor"] == 30.0
    assert pac["tonight"]["text"] == ("Tonight: RC16 22:46-01:45 MST, Ha 6 / "
                                      "OIII 9 x 300 s")
    assert pac["window"] == pytest.approx([0.385, 0.66], abs=0.002)
    assert pac["campaign_text"].startswith("Campaign: 5.1 h left")
    soul = b["targets"]["soulnebula"]
    assert soul["floor"] == 40.0 and soul["window"] is None
    assert soul["campaign_window"] == pytest.approx([0.741, 0.926], abs=0.002)
    assert soul["tonight"]["status"] == "not_planned"
    assert b["targets"]["crescentnebula"]["tonight"]["status"] == "paused"
    south = b["targets"]["southernthing"]
    assert south["up_h"] == 0.0 and max(south["alts"]) < 0
    assert south["tonight"]["reason"] == "below 30 deg all night"


def test_build_skips_tests_and_survives_no_campaign(tmp_path):
    cfg = _cfg(tmp_path)
    projects = [_proj("p1", "Pacman Nebula", 0.878, 56.63)]
    cards = tb.annotate([_card("Pacman Nebula", pid="p1", key="pacmannebula")])
    b = tb.build(cfg, projects, _tonight(), None, cards=cards)
    assert b["campaign_ok"] is False
    t = b["targets"]["pacmannebula"]
    assert t["campaign"] is None and t["campaign_text"] == ""
    assert t["tonight"]["status"] == "planned"
    assert tb.build(cfg, projects, {"date": "x"}, None)["targets"] == {}


# --- endpoints and page ----------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app
    from photonscript.scheduler.routers import targets as rt
    cfg = _cfg(tmp_path)
    projects = [_proj("p1", "Pacman Nebula", 0.878, 56.63),
                _proj("p3", "Crescent Nebula", 20.2, 38.35, active=False)]
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", type("S", (), {
        "projects": {p.id: p for p in projects}})())
    monkeypatch.setattr(rt, "_board_cache", {})
    calls = []

    async def fake_tonight():
        calls.append("tonight")
        return _tonight()
    monkeypatch.setattr(app, "api_tonight_plan", fake_tonight)

    def fake_campaign(config, store, forecast=None, **kw):
        calls.append(("campaign", kw.get("with_calibration")))
        return {"nights": [], "goals": []}
    monkeypatch.setattr("photonscript.scheduler.campaign.build_campaign",
                        fake_campaign)

    async def no_fc(_cfg):
        raise OSError("offline")
    monkeypatch.setattr("photonscript.scheduler.forecast.get_forecast", no_fc)
    return TestClient(app.app), calls


def test_api_targets_has_sections(client):
    c, _calls = client
    d = c.get("/api/targets").json()
    by = {t["name"]: t["section"] for t in d["targets"]}
    assert by == {"Pacman Nebula": "active", "Crescent Nebula": "paused"}
    assert d["sections"] == {"active": 1, "paused": 1, "done": 0, "test": 0}


def test_api_targets_tonight_reuses_plan_and_caches(client):
    c, calls = client
    d = c.get("/api/targets/tonight").json()
    assert d["targets"]["pacmannebula"]["tonight"]["status"] == "planned"
    assert d["targets"]["crescentnebula"]["tonight"]["status"] == "paused"
    assert calls == ["tonight", ("campaign", False)]   # no calibration scan
    c.get("/api/targets/tonight")
    assert len(calls) == 2                             # served from the cache
    c.get("/api/targets/tonight?refresh=1")
    assert len(calls) == 4


def test_api_targets_tonight_survives_a_planner_error(client, monkeypatch):
    from photonscript.scheduler import app
    c, _calls = client

    async def boom():
        raise RuntimeError("planner down")
    monkeypatch.setattr(app, "api_tonight_plan", boom)
    r = c.get("/api/targets/tonight?refresh=1")
    assert r.status_code == 200 and r.json()["targets"] == {}


def test_targets_page_sections_and_no_empty_thumbnail(client):
    c, _calls = client
    html = c.get("/targets").text
    assert "/api/targets/tonight" in html
    assert "['test', 'Tests'" in html and "LS_KEY" in html
    # Tests and Done collapsed by default (UI tidy 2026-11-23: Done starts folded)
    assert "'tracking / optics tests, TPoint, calibration, unattributed', false" \
        in html
    assert "['done', 'Done', 'goal met, or imaged with no goal', false]" in html
    # the thumbnail block only shows once an image loaded
    assert ".tcard .pic { display:none;" in html and ".tcard .pic.has" in html
    assert "localStorage" in html and "catch (e)" in html
    assert "\u2014" not in html                        # no em dashes
