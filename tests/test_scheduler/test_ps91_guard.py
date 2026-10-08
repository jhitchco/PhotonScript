"""PS-91 scheduler side: grading (qa_rules guide_lock), backfill lock from the
guide log, the armer's hot-pixel trigger and unguided fallback, the PHD2
router and the System page config fields."""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import phd2_analysis as pa
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler.armer import Armer
from photonscript.shared import phd2_store as store
from photonscript.shared import qa_rules
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "phd2"
N25 = "PHD2_GuideLog_2026-09-25_192414.txt"


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    return PhotonScriptConfig(_env_file=None, **kw)


# ---- qa_rules ---------------------------------------------------------------

def _card(cfg, **m):
    base = dict(hfr=3.0, ecc=0.3, stars=200, background=900, exp_s=300,
                ccd_temp=0.0, guide_rms=0.05, guide_state="guiding")
    base.update(m)
    return qa_rules.evaluate(qa_rules.record_metrics(**base),
                             qa_rules.context(cfg, "rc16", "Heart", "Ha"))


def _check(card, cid):
    return next(c for c in card.checks if c.id == cid)


def test_non_star_sub_warns_and_its_rms_is_not_judged():
    cfg = _cfg(qa_guide_rms_mode="fail", quality_tracking_rms_max=0.01)
    card = _card(cfg, guide_lock="non-star")
    assert _check(card, "guide_lock").status == qa_rules.WARN
    assert _check(card, "guide_rms").status == qa_rules.SKIP
    assert "guide_lock" in card.warnings and card.passed
    assert card.verdict == qa_rules.NEEDS_LOOK


def test_non_star_fail_mode_and_star_pass():
    card = _card(_cfg(qa_guide_lock_mode="fail"), guide_lock="non-star")
    assert _check(card, "guide_lock").status == qa_rules.FAIL and not card.passed
    card = _card(_cfg(), guide_lock="star")
    assert _check(card, "guide_lock").status == qa_rules.PASS
    card = _card(_cfg())
    assert _check(card, "guide_lock").status == qa_rules.SKIP
    rows = qa_rules.expand(card.compact())
    assert any(r["id"] == "guide_lock" and "no guard data" in r["reason"] for r in rows)
    # stored records round-trip the input (rescore)
    assert qa_rules.metrics_from_record({"guide_lock": "non-star"})["guide_lock"] == "non-star"


# ---- backfill: guard episodes, else the guide log ----------------------------

def _timeline():
    secs = pl.parse_guide_log((FIX / N25).read_text(encoding="utf-8"), N25)
    raw = [(s, pl._scale_for(s["header"], None)[0]) for s in secs
           if s["kind"] == "guiding" and s["frames"]]
    return pa.GuideTimeline(raw, _cfg())


def test_backfill_lock_from_the_guide_log_and_the_guard(tmp_path):
    cfg = _cfg(tmp_path)
    tl = _timeline()
    # 2026-09-25 22:15:42 local (MDT) = 04:15:42Z: the static 'star'
    static_start = datetime(2026, 9, 26, 4, 17, 0)
    real_start = datetime(2026, 9, 26, 7, 37, 30)        # 01:36 local, 0.45"
    assert pa.sub_guide_lock(cfg, "2026-09-25", static_start, 240, timeline=tl) == "non-star"
    assert pa.sub_guide_lock(cfg, "2026-09-25", real_start, 240, timeline=tl) == "star"
    assert pa.sub_guide_lock(cfg, "2026-09-25", datetime(2026, 9, 26, 1, 0), 60,
                             timeline=tl) is None          # unguided
    # a guard episode wins over the log
    store.append_jsonl(store.guard_path(cfg, "2026-09-25"), {
        "event": "open", "id": "e1", "t_utc": "2026-09-26T07:30:00Z",
        "kind": "non_star", "codes": ["D5"]})
    store.append_jsonl(store.guard_path(cfg, "2026-09-25"), {
        "event": "close", "id": "e1", "t_utc": "2026-09-26T07:45:00Z"})
    assert pa.sub_guide_lock(cfg, "2026-09-25", real_start, 240, timeline=tl) == "non-star"


def test_store_night_and_alert_latch(tmp_path):
    cfg = _cfg(tmp_path)
    assert store.night_of(cfg, datetime(2026, 10, 3, 4, 0)) == "2026-10-02"   # 22:00 MDT
    assert store.night_of(cfg, datetime(2026, 10, 3, 19, 0)) == "2026-10-03"  # 13:00 MDT next day
    assert store.alert_once(cfg, "guard-2026-10-02") is True
    assert store.alert_once(cfg, "guard-2026-10-02") is False
    assert store.alert_once(cfg, "selftest-2026-10-02") is True


# ---- armer --------------------------------------------------------------------

async def _noop(*a, **k):
    return None


async def test_fallback_unguided_once_per_night_keeps_the_companion(tmp_path, monkeypatch):
    a = Armer(_cfg(tmp_path))
    a.state, a.plan = "RUNNING", {"night_of": "2026-10-02"}
    calls, dispatched = [], []

    async def _nina(key, *x, **k):
        calls.append(key)
        return {"Success": True}

    async def _dispatch(companion=True, fail_state="ERROR"):
        dispatched.append((companion, fail_state))
        return True
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(a, "_dispatch_and_start", _dispatch)
    import photonscript.scheduler.armer as armer_mod
    monkeypatch.setattr(armer_mod, "notify", _noop)
    assert await a.fallback_unguided("selftest FAIL") is True
    assert calls == ["sequence_stop", "guider_stop"]
    assert dispatched == [(False, None)]
    assert a.guiding_override == "unguided" and not a._use_guiding()
    saved = json.loads((tmp_path / "data" / "armer_state.json").read_text())
    assert saved["guiding_override"] == "unguided" and saved["fallback_night"] == "2026-10-02"
    assert await a.fallback_unguided("again") is False      # once per night
    assert len(dispatched) == 1
    b = Armer(_cfg(tmp_path / "b"))
    b.state, b.plan = "ARMED", {"night_of": "2026-10-02"}
    assert await b.fallback_unguided("x") is False          # RUNNING only


async def test_request_fallback_without_an_armer_is_a_no_op(monkeypatch):
    import sys
    from photonscript.scheduler.armer import request_fallback_unguided
    monkeypatch.delitem(sys.modules, "photonscript.scheduler.app", raising=False)
    assert await request_fallback_unguided(_cfg(), "x") is False


async def test_hotpix_map_tried_pre_dusk_only_when_roof_closed(tmp_path, monkeypatch):
    import photonscript.telescope_agent.guide_hotpix as gh
    runs = []

    async def _maybe(cfg, why):
        runs.append(why)
        return {"ok": True}
    monkeypatch.setattr(gh, "maybe_capture", _maybe)
    a = Armer(_cfg(tmp_path))
    now = datetime.utcnow()
    a.state = "ARMED"
    a.plan = {"night_of": "2026-10-02",
              "preconfig_utc": (now + timedelta(minutes=10)).isoformat() + "Z"}
    safe = {"v": True}

    async def _is_safe():
        return safe["v"]
    monkeypatch.setattr(a, "_is_safe", _is_safe)
    await a._tick()
    await asyncio.sleep(0)
    assert runs == []                                     # roof open
    safe["v"] = False
    await a._tick()
    await a._tick()                                       # once per night
    await asyncio.sleep(0.05)
    assert runs == ["pre-dusk, roof closed"]
    a.plan["preconfig_utc"] = (now + timedelta(minutes=50)).isoformat() + "Z"
    a._hotpix_tried = None
    await a._tick()
    await asyncio.sleep(0.05)
    assert len(runs) == 1                                 # outside the 30 min window


# ---- router + config ------------------------------------------------------------

def test_guard_router_and_config_fields(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import phd2 as r
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    store.append_jsonl(store.guard_path(cfg, "2026-10-01"), {
        "event": "open", "id": "e1", "t_utc": "2026-10-02T04:00:00Z",
        "kind": "non_star", "codes": ["D2"], "observe_only": True})
    store.append_jsonl(store.guard_path(cfg, "2026-10-01"), {
        "event": "close", "id": "e1", "t_utc": "2026-10-02T04:30:00Z"})
    g = r.api_phd2_guard(date="2026-10-01")
    assert g["episodes"] == 1 and g["non_star"] == 1 and g["closed_minutes"] == 30.0
    assert g["auto_recover"] is False and g["phd2_ops"]["owner"] is None
    h = r.api_phd2_hotpix()
    assert h["exists"] is False and h["stale"] == "no hot-pixel map yet"
    from fastapi.testclient import TestClient
    client = TestClient(app.app)                      # mounted on the app
    assert client.get("/api/phd2/guard?date=2026-10-01").json()["episodes"] == 1
    assert client.get("/api/phd2/hotpix").json()["exists"] is False
    by_env = {f[1]: f for f in app._CONFIG_FIELDS}
    for env in ("PS_GUARD_ENABLED", "PS_GUARD_AUTO_RECOVER", "PS_GUARD_ON_FAIL",
                "PS_GUIDE_MIN_STAR_HFD_PX", "PS_PHD2_HOTPIX_MAX_AGE_DAYS",
                "PS_QA_GUIDE_LOCK_MODE"):
        assert hasattr(cfg, by_env[env][0])


async def test_hotpix_capture_refused_while_a_night_runs(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import phd2 as r
    monkeypatch.setattr(r, "_armer_state", lambda: "RUNNING")
    res = await r.api_phd2_hotpix_capture()
    assert res.status_code == 409
