"""Fix 2026-10-13: a sub's guide_rms was the PHD2 client's rolling RMS at
grading time, so RC16 subs on 10-12 carried a constant 34.84" (the last
guiding sample) while PHD2 was stopped or LostLock. Now only the guide
frames inside the sub's own exposure count (phd2_client.window_rms), else
None with the guide_state that says why."""
import asyncio
from datetime import datetime, timezone

from photonscript.shared.models import GuidingState, ImageQualityMetrics
from photonscript.telescope_agent.phd2_client import WINDOW_RMS_MIN_FRAMES, window_rms
from tests.test_scheduler.test_ps21_grading import (NIGHT, _agent, _cfg, _records,
                                                    _write_light)

T0 = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc).timestamp()   # DATE-OBS


def _f(t, ra=0.3, dec=0.4, drop=False):
    return {"t": t, "ra": None if drop else ra, "dec": None if drop else dec,
            "drop": drop}


def test_window_rms_counts_only_guided_frames_inside_the_exposure():
    frames = ([_f(T0 - 60 + i, ra=30.0, dec=30.0) for i in range(10)]   # before
              + [_f(T0 + 5 * i) for i in range(10)]                     # inside
              + [_f(T0 + 20, drop=True)]
              + [_f(T0 + 400 + i, ra=30.0) for i in range(5)])          # after
    w = window_rms(frames, T0, T0 + 300, 0.25)
    assert w["frames"] == 10 and w["drops"] == 1
    assert abs(w["rms_arcsec"] - 0.5 * 0.25) < 1e-9          # hypot(0.3, 0.4) px


def test_window_rms_is_none_without_enough_frames_or_a_scale():
    few = [_f(T0 + i) for i in range(WINDOW_RMS_MIN_FRAMES - 1)]
    assert window_rms(few, T0, T0 + 300, 0.25)["rms_arcsec"] is None
    many = [_f(T0 + i) for i in range(10)]
    assert window_rms(many, T0, T0 + 300, None)["rms_arcsec"] is None
    assert window_rms([], T0, T0 + 300, 0.25) == {"rms_arcsec": None, "frames": 0,
                                                  "drops": 0}


def _process(tmp_path, monkeypatch, frames, state, snapshot=34.84):
    from photonscript.telescope_agent import agent as agent_mod
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(agent_mod, "validate_image",
                        lambda *a, **k: ImageQualityMetrics(
                            hfr_pixels=2.0, star_count=100, eccentricity=0.3,
                            background_adu=400.0, exposure_flag="ok"))
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "s_Ha_300s_0001.fits",
                     size=64, n=2)
    a = _agent(cfg, rms=snapshot, gstate=state)
    a.phd2.frames = frames
    a.phd2.pixel_scale = 0.381
    asyncio.run(a._process_new_image(f))
    (rec,) = _records(cfg)
    return rec


def test_10_12_stopped_phd2_records_null_not_the_last_sample(tmp_path, monkeypatch):
    # the last guiding came minutes before the sub; PHD2 then stopped
    old = [_f(T0 - 600 + 3 * i, ra=60.0, dec=60.0) for i in range(30)]
    rec = _process(tmp_path, monkeypatch, old, GuidingState.STOPPED)
    assert rec["guide_rms"] is None and rec["guide_rms_frames"] == 0
    assert rec["guide_state"] == "stopped"


def test_lost_lock_inside_the_sub_records_null(tmp_path, monkeypatch):
    frames = [_f(T0 + 2, ra=60.0, dec=60.0)] + [_f(T0 + 3 + 3 * i, drop=True)
                                               for i in range(80)]
    rec = _process(tmp_path, monkeypatch, frames, GuidingState.LOST_STAR)
    assert rec["guide_rms"] is None and rec["guide_state"] == "lost_star"


def test_guided_sub_records_its_own_rms(tmp_path, monkeypatch):
    frames = ([_f(T0 - 100 + i, ra=60.0, dec=60.0) for i in range(20)]
              + [_f(T0 + 3 * i, ra=2.0, dec=0.0) for i in range(100)])
    rec = _process(tmp_path, monkeypatch, frames, GuidingState.GUIDING)
    assert rec["guide_rms"] == round(2.0 * 0.381, 3) and rec["guide_rms_frames"] == 100
