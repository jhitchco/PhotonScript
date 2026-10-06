"""PS-85 guide-star viability (RC16 agent side): profile_sanity on synthetic
crops (a star, a noise lock, a lumpy noise cluster, a hot pixel), judge
(SNR, HFD scaled by binning, lost frames, profile), the ViabilityMonitor over
the shared fake PHD2 (a faint 3 nm star asks the armer for an unguided
block, a real star does not, pending while the tuner can still lengthen the
NB exposure, quiet on unguided nights), guard D6 (low SNR plus a non-star
profile; a weak real star is left alone) and the agent wiring."""
import asyncio
import base64

import numpy as np
import pytest

from photonscript.scheduler import guide_blocks as gb
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import guide_viability as gv
from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent.guide_guard import (
    LOW_SNR, GuardContext, NonStarLockGuard)
from photonscript.telescope_agent.phd2_client import PHD2Client
from tests.fakes.fake_phd2 import FakePHD2, SimField


def _crop(peak, sigma=2.5, size=21, noise=8.0, hot=False, lumps=False, seed=1):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    c = size // 2
    img = rng.normal(500, noise, (size, size))
    if hot:
        img[c, c] += peak
    elif lumps:   # the 2026-10-05 kind of "star": a jagged cluster of noise
        for x, y, a in ((c, c, 1.0), (c + 3, c - 2, 0.9), (c - 3, c + 3, 0.8), (c + 1, c + 4, 0.85)):
            img += peak * a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 0.8 ** 2))
    else:
        img += peak * np.exp(-((xx - c) ** 2 + (yy - c) ** 2) / (2 * sigma * sigma))
    px = np.clip(img, 0, 65535).astype("<u2")
    return {"width": size, "height": size, "star_pos": [c, c],
            "pixels": base64.b64encode(px.tobytes()).decode()}


# ------------------------------------------------------------------ pure

def test_profile_sanity_star_noise_lumps_hot_pixel():
    for seed in (1, 2, 3):
        assert gv.profile_sanity(_crop(4000, seed=seed))["ok"]
        assert gv.profile_sanity(_crop(200, seed=seed))["ok"]       # faint but round
    noise = gv.profile_sanity(_crop(0))
    assert not noise["ok"] and "sigma" in noise["why"]
    lumps = gv.profile_sanity(_crop(120, lumps=True))
    assert not lumps["ok"] and "jagged" in lumps["why"] and lumps["asymmetry"] > 0.35
    hot = gv.profile_sanity(_crop(30000, hot=True))
    assert not hot["ok"] and "one pixel" in hot["why"]
    assert gv.profile_sanity({"width": 3}) is None


def _frames(snr, hfd=6.5, n=5, lost=0):
    return [{"snr": snr, "hfd": hfd, "drop": i < lost} for i in range(n)]


def test_judge_rows():
    cfg = gv.ViableCfg()
    ok = gv.judge(_frames(45.0), {"ok": True}, cfg, binning=2)
    assert ok["viable"] and ok["snr"] == 45.0 and ok["profile"] == "ok"
    # 2026-10-05: SNR 21.9 to 30.9, HFD 5.6 to 6.2, jagged
    noise = gv.judge([{"snr": s, "hfd": h} for s, h in
                      ((21.9, 5.6), (24.0, 5.9), (26.2, 6.0), (28.8, 6.2), (30.9, 6.1))],
                     {"ok": False, "why": "jagged profile"}, cfg, binning=2)
    assert not noise["viable"] and "SNR 26.2 under 30" in noise["reason"]
    assert "jagged" in noise["reason"]
    lost = gv.judge(_frames(45.0, lost=3), None, cfg, binning=2)
    assert not lost["viable"] and "lost in 3 of 5" in lost["reason"]
    # HFD band is given at bin 2: 1.5 to 10 px; at bin 1 it is 3 to 20
    assert not gv.judge(_frames(45.0, hfd=1.0), None, cfg, binning=2)["viable"]
    assert gv.judge(_frames(45.0, hfd=15.0), None, cfg, binning=1)["viable"]
    assert not gv.judge(_frames(45.0, hfd=2.5), None, cfg, binning=1)["viable"]
    assert gv.judge([], None, cfg) is None
    c = gv.viable_cfg(PhotonScriptConfig(_env_file=None, guide_viable_snr_min=25,
                                         guide_viable_hfd_px="2,8", guide_viable_frames=3))
    assert (c.snr_min, c.hfd_lo, c.hfd_hi, c.frames) == (25.0, 2.0, 8.0, 3)


# ------------------------------------------------------------------ guard D6

def _gframes(snr, n=12, epoch=1, settling=False):
    return [{"t": 1000.0 + i, "snr": snr, "hfd": 6.0, "drop": False, "epoch": epoch,
             "settling": settling, "ra": 0.1, "dec": 0.1, "ra_ms": 0, "dec_ms": 0}
            for i in range(n)]


def _ctx(frames, profile_ok=None, state="Guiding"):
    return GuardContext(frames=frames, app_state=state, binning=2, now=2000.0,
                        star_profile_ok=profile_ok)


def test_guard_d6_needs_low_snr_and_a_non_star_profile():
    cfg = PhotonScriptConfig(_env_file=None)
    g = NonStarLockGuard(cfg)
    low = _gframes(24.0)
    assert g.wants_star_image(_ctx(low))                      # asks for the image
    assert not [v for v in g.verdicts(_ctx(low)) if v.code == "D6"]        # unknown
    assert not [v for v in g.verdicts(_ctx(low, True)) if v.code == "D6"]  # weak real star
    d6 = [v for v in g.verdicts(_ctx(low, False)) if v.code == "D6"]
    assert d6 and d6[0].kind == LOW_SNR and d6[0].evidence["snr_median"] == 24.0
    assert not [v for v in g.verdicts(_ctx(_gframes(24.0, n=9), False)) if v.code == "D6"]
    assert not [v for v in g.verdicts(_ctx(_gframes(31.0), False)) if v.code == "D6"]
    assert not [v for v in g.verdicts(_ctx(_gframes(24.0, settling=True), False))
                if v.code == "D6"]
    assert not [v for v in g.verdicts(_ctx(low, False, state="Looping")) if v.code == "D6"]
    off = NonStarLockGuard(PhotonScriptConfig(_env_file=None, guide_lowsnr_frames=0))
    assert not [v for v in off.verdicts(_ctx(low, False)) if v.code == "D6"]


def test_low_snr_episodes_mark_subs_like_non_star(tmp_path):
    from datetime import datetime
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path)
    p = store.guard_path(cfg, "2026-10-05")
    store.append_jsonl(p, {"event": "open", "id": "e1", "t_utc": "2026-10-06T05:00:00Z",
                           "kind": "low_snr", "codes": ["D6"]})
    store.append_jsonl(p, {"event": "close", "id": "e1", "t_utc": "2026-10-06T05:10:00Z"})
    w = store.nonstar_windows(cfg, "2026-10-05")
    assert w == [(datetime(2026, 10, 6, 5, 0), datetime(2026, 10, 6, 5, 10))]


# ------------------------------------------------------------------ live

@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    phd2_ops._reset_for_tests()
    monkeypatch.setattr(gv, "FRAME_WAIT_S", 5.0)
    yield
    phd2_ops._reset_for_tests()


async def _setup(tmp_path, monkeypatch, flux_scale=1.0, filt="Ha", guided=True,
                 tuner=None, exp_ms=2000, **cfg_kw):
    field = SimField(width=120, height=100, stars=[(60.0, 50.0, 10000.0, 1.6)], hot=[])
    fake = FakePHD2(tmp_path / "phd2tmp", field=field, flux_ref_ms=2000,
                    flux_scale=flux_scale, guide_exposure_ms=exp_ms)
    port = await fake.start()
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                             phd2_host="127.0.0.1", phd2_port=port, **cfg_kw)
    client = PHD2Client("127.0.0.1", port, config=cfg)
    assert await client.connect()
    task = await client.start_event_loop()
    await client.refresh_pixel_scale()
    ctx = {"target": "Heart Nebula", "filter": filt}
    asked = []

    async def _req(config, target, f, reason, source="live", evidence=None):
        asked.append((target, f, reason, source))
        return False
    monkeypatch.setattr("photonscript.scheduler.armer.request_block_unguided", _req)
    mon = gv.ViabilityMonitor(cfg, client, context_fn=lambda: ctx, tuner=tuner,
                              guided_fn=lambda: guided)
    await client.call("guide", [{"pixels": 1.5, "time": 1, "timeout": 10}, False])
    for _ in range(200):
        if client.app_state == "Guiding" and not client.settling:
            break
        await asyncio.sleep(0.02)
    return fake, client, task, mon, cfg, ctx, asked


async def _close(fake, client, task):
    await client.disconnect()
    task.cancel()
    await fake.close()


async def test_faint_nb_star_asks_for_an_unguided_block(tmp_path, monkeypatch):
    fake, client, task, mon, cfg, ctx, asked = await _setup(tmp_path, monkeypatch,
                                                            flux_scale=0.003)
    try:
        rec = await mon.check("filter Ha")
        assert rec is not None and rec["viable"] is False and rec["final"] is True
        assert rec["snr"] < 30 and rec["binning"] == 2
        assert asked and asked[0][:2] == ("Heart Nebula", "Ha")
        assert asked[0][2].startswith("no real guide star")
        recs = gb.records(cfg, store.night_of(cfg))
        assert recs[-1]["event"] == "check" and recs[-1]["target"] == "Heart Nebula"
        assert await mon.check("settled") is None              # decided: no re-check
        assert "set_exposure" not in fake.methods() and "loop" not in fake.methods()
    finally:
        await _close(fake, client, task)


async def test_real_star_is_viable_and_nothing_is_asked(tmp_path, monkeypatch):
    fake, client, task, mon, cfg, ctx, asked = await _setup(tmp_path, monkeypatch)
    try:
        rec = await mon.check("filter Ha")
        assert rec["viable"] is True and rec["profile"] == "ok"
        assert asked == []
    finally:
        await _close(fake, client, task)


async def test_pending_while_the_tuner_has_nb_headroom(tmp_path, monkeypatch):
    class _Tuner:
        mode = "exposure"
    fake, client, task, mon, cfg, ctx, asked = await _setup(
        tmp_path, monkeypatch, flux_scale=0.003, tuner=_Tuner())
    try:
        for i in range(gv.MAX_PENDING):
            rec = await mon.check("settled")
            assert rec["final"] is False, i                     # 2000 ms < 8000 ms
        assert asked == []
        rec = await mon.check("settled")                        # out of re-checks
        assert rec["final"] is True and len(asked) == 1
    finally:
        await _close(fake, client, task)


async def test_no_headroom_on_broadband_or_observe_tuner(tmp_path, monkeypatch):
    class _Tuner:
        mode = "exposure"
    fake, client, task, mon, cfg, ctx, asked = await _setup(
        tmp_path, monkeypatch, flux_scale=0.003, filt="L", tuner=_Tuner())
    try:
        rec = await mon.check("settled")
        assert rec["final"] is True and asked                  # L: 4 s band, no NB room
    finally:
        await _close(fake, client, task)


async def test_quiet_when_not_guided_held_or_off(tmp_path, monkeypatch):
    fake, client, task, mon, cfg, ctx, asked = await _setup(
        tmp_path, monkeypatch, flux_scale=0.003, guided=False)
    try:
        assert mon.blocked() == "no guided night armed"
        assert await mon.check("settled") is None
        mon.guided_fn = lambda: True
        async with phd2_ops.hold("tuner"):
            assert "held by tuner" in mon.blocked()
        ctx["slewing"] = True
        assert "slewing" in mon.blocked()
        ctx["slewing"] = False
        mon.config = PhotonScriptConfig(_env_file=None, guide_block_mode="off",
                                        data_dir=cfg.data_dir)
        assert mon.blocked() == "guide_block_mode off"
        assert asked == []
    finally:
        await _close(fake, client, task)


def test_frame_sample_from_guiding_and_looping():
    class C:
        app_state = "Guiding"
        loop_star = {"SNR": 12.0, "HFD": 4.0, "t": 5.0}

        def recent_frames(self):
            return [{"snr": 33.0, "hfd": 6.0, "drop": False, "t": 4.0}]
    c = C()
    assert gv.frame_sample(c)["snr"] == 33.0
    c.app_state = "Looping"
    assert gv.frame_sample(c) == {"snr": 12.0, "hfd": 4.0, "drop": False, "t": 5.0}
    c.app_state = "Stopped"
    assert gv.frame_sample(c) is None


# ------------------------------------------------------------------ agent

async def test_agent_wiring_and_low_snr_switch(tmp_path, monkeypatch):
    from photonscript.telescope_agent.agent import TelescopeAgent
    from photonscript.telescope_agent.guide_guard import Verdict
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                             nina_filter_names="Ha:H,OIII:O")
    rc = TelescopeAgent(cfg, rig="rc16")
    rc._viability_setup()
    assert isinstance(rc.viability, gv.ViabilityMonitor)
    pig = TelescopeAgent(cfg, rig="piggyback")
    pig._viability_setup()
    assert pig.viability is None
    off = TelescopeAgent(PhotonScriptConfig(_env_file=None, guide_block_mode="off"), rig="rc16")
    off._viability_setup()
    assert off.viability is None
    seen = []
    rc.viability.filter_changed = seen.append

    class _Nina:
        async def get_filter_wheel_info(self):
            return {"SelectedFilter": {"Name": "H"}}
    rc.nina = _Nina()
    await rc._poll_filter()
    assert seen == ["Ha"]

    asked, stops = [], []

    async def _req(config, target, f, reason, source="live", evidence=None):
        asked.append((target, f, source))
        return False

    async def _stop():
        stops.append(1)
    monkeypatch.setattr("photonscript.scheduler.armer.request_block_unguided", _req)
    monkeypatch.setattr(rc.phd2, "stop_capture", _stop)
    rc.state.current_target = "Heart Nebula"
    v = Verdict("D6", LOW_SNR, "guide star SNR 24", {"snr_median": 24.0})
    await rc._lowsnr_switch(v)                       # observe: no PHD2 command
    assert asked == [("Heart Nebula", "Ha", "guard D6")] and stops == []
    rc.config = PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                                   guide_block_mode="auto")
    await rc._lowsnr_switch(v)                       # auto: stop guiding on noise
    assert stops == [1] and len(asked) == 2
    night = store.night_of(cfg)
    assert (night, "Heart Nebula", "Ha") in rc.viability._final


async def test_guard_act_routes_d6_to_the_block_switch(tmp_path, monkeypatch):
    from photonscript.telescope_agent.agent import TelescopeAgent
    from photonscript.telescope_agent.guide_guard import Verdict
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path, guard_auto_recover=True)
    rc = TelescopeAgent(cfg, rig="rc16")
    rc._guard_setup()
    switched, recovered, alerts = [], [], []

    async def _switch(v):
        switched.append(v.code)

    async def _recover(*a, **k):
        recovered.append(1)

    async def _alert(night, msg):
        alerts.append(msg)
    monkeypatch.setattr(rc, "_lowsnr_switch", _switch)
    monkeypatch.setattr(rc, "_guard_recover", _recover)
    monkeypatch.setattr(rc, "_guard_alert", _alert)
    v = Verdict("D6", LOW_SNR, "guide star SNR 24", {"snr_median": 24.0})
    await rc._guard_act([v], tick=True)
    await rc._guard_act([v], tick=True)              # same episode: once
    assert switched == ["D6"] and recovered == [] and alerts == []
    eps = store.guard_episodes(cfg, store.night_of(cfg))
    assert [e["kind"] for e in eps] == ["low_snr"]
