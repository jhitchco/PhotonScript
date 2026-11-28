"""PS-71: QA passed hot-pixel frames shot while parked (2026-09-26).

RC16 Heart OIII 300 s subs 0024-0030 (11:41-12:17Z) passed with background
257 ADU (the bias floor), 12-22 "stars", HFR 1.4-1.7 px, FWHM 0.57". Sub 0023
ran through the roof close. The records below are the live ones from
/api/runs/2026-09-26 (abs_path dropped)."""

import json
from datetime import datetime

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.qa_signatures import (HOT_PIXELS, ROOF_CLOSED,
                                               parked_frame_verdict)
from photonscript.shared import safety_history as sh
from photonscript.shared.rigs import PIGGYBACK, rig_config

T = "Heart Nebula imaging (repeats while safe and up)_Container"


def _rec(n, start, end, hfr, fwhm, stars, bg, exp=300.0, flt="O"):
    return {"rig": "rc16",
            "file": f"LIGHT\\2026-09-27_{start}__{flt}_{exp:.2f}s_{n:04d}.fits",
            "time": f"2026-09-27T{end}Z", "target": T,
            "filter": {"O": "OIII", "H": "Ha", "S": "SII"}[flt], "exp_s": exp,
            "ccd_temp": 0, "hfr": hfr, "fwhm_arcsec": fwhm, "stars": stars,
            "ecc": 0.5, "background": bg, "passed_qa": True, "reason": ""}


GOOD = [_rec(20, "05-20-16", "11:25:25.838860", 7.8, 2.43, 149, 283.0),
        _rec(21, "05-25-17", "11:30:25.688330", 7.13, 2.25, 150, 290.0),
        _rec(22, "05-30-18", "11:35:26.471407", 6.83, 2.24, 93, 318.0)]
THROUGH_CLOSE = _rec(23, "05-35-19", "11:40:27.167229", 6.66, 2.0, 17, 361.0)
PARKED = [_rec(24, "05-41-21", "11:46:32.155661", 1.42, 0.57, 12, 257.0),
          _rec(25, "05-46-22", "11:51:32.973203", 1.73, 0.57, 21, 257.0),
          _rec(26, "05-51-23", "11:56:32.101295", 1.67, 0.58, 16, 257.0),
          _rec(27, "05-56-23", "12:01:32.647040", 1.64, 0.56, 15, 257.0),
          _rec(28, "06-01-24", "12:06:33.448984", 1.55, 0.58, 22, 257.0),
          _rec(29, "06-07-34", "12:12:44.976819", 1.71, 0.56, 22, 257.0),
          _rec(30, "06-12-34", "12:17:45.688483", 1.51, 0.59, 16, 257.0)]
UNSAFE_09_26 = [(datetime(2026, 9, 27, 11, 39, 56), datetime(2026, 9, 27, 12, 18, 19))]


def _v(cfg, r, **kw):
    return parked_frame_verdict(cfg, hfr_px=r["hfr"], fwhm_arcsec=r["fwhm_arcsec"],
                                background=r["background"], exp_s=r["exp_s"],
                                stars=r["stars"], **kw)


def test_the_seven_parked_subs_are_rejected_and_flagged():
    cfg = PhotonScriptConfig()
    for r in PARKED:
        v = _v(cfg, r)
        assert v.reject and v.flag == ROOF_CLOSED, r["file"]
        assert "dark-frame signature" in v.reasons[0]


def test_good_subs_that_night_still_pass():
    cfg = PhotonScriptConfig()
    for r in GOOD + [THROUGH_CLOSE]:
        assert not _v(cfg, r).reject, r["file"]


def test_short_narrowband_at_the_floor_with_real_stars_passes():
    # 09-26 Ha 60 s HDR shorts: background 257 with 156 real stars (HFR 6.4)
    cfg = PhotonScriptConfig()
    v = parked_frame_verdict(cfg, hfr_px=6.43, fwhm_arcsec=1.69, background=257.0,
                             exp_s=60.0, stars=156)
    assert not v.reject


def test_long_narrowband_sky_just_above_the_floor_passes():
    # darkest real long NB subs 09-17..09-25: 269-270 ADU at 900 s
    cfg = PhotonScriptConfig()
    for bg in (269.0, 270.0):
        assert not parked_frame_verdict(cfg, hfr_px=9.24, fwhm_arcsec=2.42,
                                        background=bg, exp_s=900.0,
                                        stars=400).reject


def test_long_light_at_the_floor_rejects_on_background_alone():
    cfg = PhotonScriptConfig()
    v = parked_frame_verdict(cfg, hfr_px=None, fwhm_arcsec=None,
                             background=258.0, exp_s=600.0, stars=0)
    assert v.reject and v.flag == ROOF_CLOSED and "no sky" in v.reasons[0]


def test_sub_physical_stars_alone_reject_as_hot_pixels():
    cfg = PhotonScriptConfig()
    v = parked_frame_verdict(cfg, hfr_px=1.5, fwhm_arcsec=0.6, background=400.0,
                             exp_s=300.0, stars=30)
    assert v.reject and v.flag == HOT_PIXELS


def test_star_size_needs_both_estimates_under_the_floor():
    """A real star with an odd sep FWHM but a normal HFR is not hot pixels."""
    cfg = PhotonScriptConfig()
    assert not parked_frame_verdict(cfg, hfr_px=5.0, fwhm_arcsec=0.8,
                                    background=400.0, exp_s=300.0,
                                    stars=100).reject


def test_piggyback_uses_its_own_scale_and_floor():
    pcfg = rig_config(PhotonScriptConfig(piggyback_enabled=True), PIGGYBACK)
    # a normal 09-26 Piggy-600 sub
    assert not parked_frame_verdict(pcfg, hfr_px=2.27, fwhm_arcsec=5.7,
                                    background=1051.0, exp_s=120.0,
                                    stars=400).reject
    assert pcfg.quality_fwhm_min_arcsec == 2.0


def test_exposure_overlapping_unsafe_is_rejected():
    cfg = PhotonScriptConfig()
    v = _v(cfg, THROUGH_CLOSE, start_utc=datetime(2026, 9, 27, 11, 35, 19),
           unsafe_windows=UNSAFE_09_26)
    assert v.reject and v.flag == ROOF_CLOSED and "UNSAFE" in v.reasons[-1]
    ok = _v(cfg, GOOD[-1], start_utc=datetime(2026, 9, 27, 11, 30, 18),
            unsafe_windows=UNSAFE_09_26)
    assert not ok.reject
    off = PhotonScriptConfig(quality_reject_unsafe_subs=False)
    assert not _v(off, THROUGH_CLOSE, start_utc=datetime(2026, 9, 27, 11, 35, 19),
                  unsafe_windows=UNSAFE_09_26).reject


# --- safety history ---------------------------------------------------------

def test_history_records_only_transitions_and_builds_windows(tmp_path):
    cfg = PhotonScriptConfig(data_dir=str(tmp_path))
    t = datetime(2026, 9, 27, 11, 39, 26)
    assert sh.record(cfg, True, now=t)
    assert not sh.record(cfg, True, now=t.replace(second=56))
    assert sh.record(cfg, False, now=t.replace(minute=39, second=56))
    assert not sh.record(cfg, False, now=t.replace(minute=40))
    assert sh.record(cfg, True, now=t.replace(hour=13, minute=0))
    lines = sh.history_path(cfg).read_text().splitlines()
    assert [json.loads(x)["state"] for x in lines] == ["safe", "unsafe", "safe"]
    wins, src = sh.unsafe_windows(cfg, datetime(2026, 9, 27, 0, 0),
                                  datetime(2026, 9, 27, 14, 0))
    assert src == "history"
    assert wins == [(datetime(2026, 9, 27, 11, 39, 56),
                     datetime(2026, 9, 27, 13, 0, 26))]


def test_windows_fall_back_to_the_pushover_audit(tmp_path):
    cfg = PhotonScriptConfig(data_dir=str(tmp_path))
    rows = [{"ts": "2026-09-27T11:39:56.354007+00:00", "title": "PhotonScript paused"},
            {"ts": "2026-09-27T12:00:01+00:00", "title": "PhotonScript NANNY"},
            {"ts": "2026-09-27T12:18:19.978183+00:00", "title": "PhotonScript complete"}]
    (tmp_path / "notifications.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    wins, src = sh.unsafe_windows(cfg, datetime(2026, 9, 27), datetime(2026, 9, 28))
    assert src == "notifications" and len(wins) == 1
    a, b = wins[0]
    assert a.strftime("%H:%M:%S") == "11:39:56" and b.strftime("%H:%M:%S") == "12:18:19"


# --- backfill ------------------------------------------------------------------

def _night(tmp_path, monkeypatch):
    from photonscript.scheduler import runs
    cfg = PhotonScriptConfig(data_dir=str(tmp_path))
    recs = GOOD + [THROUGH_CLOSE] + PARKED
    p = runs.runs_dir(cfg) / "2026-09-26_subs.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    (tmp_path / "notifications.jsonl").write_text(
        json.dumps({"ts": "2026-09-27T11:39:56.354007+00:00",
                    "title": "PhotonScript paused"}) + "\n"
        + json.dumps({"ts": "2026-09-27T12:18:19.978183+00:00",
                      "title": "PhotonScript complete"}) + "\n")
    link = tmp_path / "Library" / T / "OIII" / PARKED[0]["file"].split("\\")[-1]
    link.parent.mkdir(parents=True)
    link.write_text("x")
    synced = []
    monkeypatch.setattr(runs, "sync_goal_progress", lambda c: synced.append(1))
    return cfg, p, link, synced


def test_backfill_is_a_dry_run_by_default(tmp_path, monkeypatch):
    from photonscript.scheduler.qa_backfill import regrade_parked
    cfg, p, link, synced = _night(tmp_path, monkeypatch)
    before = p.read_text()
    res = regrade_parked(cfg, "2026-09-26")
    assert res["mode"] == "dry-run" and res["unsafe_source"] == "notifications"
    files = {c["file"].split("_")[-1] for c in res["rejects"]}
    assert files == {f"{n:04d}.fits" for n in range(23, 31)}
    assert all(c["flag"] == ROOF_CLOSED for c in res["rejects"])
    assert p.read_text() == before and link.exists() and not synced


def test_backfill_apply_regrades_and_moves_links_out_of_the_stack_set(
        tmp_path, monkeypatch):
    from photonscript.scheduler.qa_backfill import regrade_parked
    from photonscript.scheduler import runs
    cfg, p, link, synced = _night(tmp_path, monkeypatch)
    res = regrade_parked(cfg, "2026-09-26", apply=True)
    assert len(res["rejects"]) == 8 and len(res["library_moves"]) == 1
    assert not link.exists()
    assert (tmp_path / "Library" / "_rejected" / T / "OIII" / link.name).exists()
    subs = {r["file"].split("_")[-1]: r for r in runs._load_subs(cfg, "2026-09-26")}
    for n in range(23, 31):
        r = subs[f"{n:04d}.fits"]
        assert r["passed_qa"] is False and r["qa_flag"] == ROOF_CLOSED
    for n in (20, 21, 22):
        assert subs[f"{n:04d}.fits"]["passed_qa"] is True
    assert synced
    again = regrade_parked(cfg, "2026-09-26", apply=True)   # idempotent
    assert again["rejects"] == []


def test_backfill_never_overrides_a_manual_verdict(tmp_path, monkeypatch):
    from photonscript.scheduler.qa_backfill import regrade_parked
    from photonscript.scheduler import runs
    cfg, p, link, _ = _night(tmp_path, monkeypatch)
    recs = [json.loads(x) for x in p.read_text().splitlines()]
    recs[-1]["manual_qa"] = True
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    runs._invalidate_subs_cache(p)
    res = regrade_parked(cfg, "2026-09-26")
    assert len(res["rejects"]) == 7


def test_backfill_explicit_unsafe_window_without_any_history(tmp_path, monkeypatch):
    from photonscript.scheduler.qa_backfill import regrade_parked
    cfg, p, link, _ = _night(tmp_path, monkeypatch)
    (tmp_path / "notifications.jsonl").unlink()
    res = regrade_parked(cfg, "2026-09-26")
    assert res["unsafe_source"] == "none" and len(res["rejects"]) == 7  # 0023 kept
    res = regrade_parked(cfg, "2026-09-26", extra_unsafe=[
        ("2026-09-27T11:39:44Z", "2026-09-27T13:00:00Z")])
    assert res["unsafe_source"] == "manual" and len(res["rejects"]) == 8


# --- armer writes the history ----------------------------------------------------

@pytest.mark.asyncio
async def test_armer_tick_records_safety_transitions(tmp_path, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    from photonscript.scheduler.armer import Armer
    a = Armer(PhotonScriptConfig(data_dir=str(tmp_path)))
    a.state = "RUNNING"
    a.plan = {"dawn_utc": "2026-09-27T11:48:14Z", "dusk_utc": "2026-09-27T02:26:50Z"}
    env = {"safe": True}

    async def fake_nina(key, **kw):
        if key == "safety":
            return {"Response": {"Connected": True, "IsSafe": env["safe"]}}
        return None

    async def nothing(*a_, **k):
        return None

    a._nina = fake_nina
    a._maybe_warn_not_guiding = nothing
    a._reconcile_cooler = nothing
    monkeypatch.setattr(armer_mod, "notify", nothing)
    clock = {"now": datetime(2026, 9, 27, 11, 39, 26)}

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return clock["now"]

    monkeypatch.setattr(armer_mod, "datetime", _DT)
    await a._tick()
    env["safe"] = False
    clock["now"] = datetime(2026, 9, 27, 11, 39, 56)
    await a._tick()
    states = [json.loads(x)["state"]
              for x in sh.history_path(a.config).read_text().splitlines()]
    assert states == ["safe", "unsafe"]
