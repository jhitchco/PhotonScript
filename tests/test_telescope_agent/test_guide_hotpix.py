"""PS-91 guide-camera hot-pixel map: build, capture through PHD2's own
pipeline (fake PHD2), staleness."""
import asyncio
from datetime import datetime, timedelta

import numpy as np
import pytest

from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import guide_hotpix, phd2_ops
from tests.fakes.fake_phd2 import FakePHD2, SimField


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              phd2_host="127.0.0.1", **kw)


def test_build_map_flags_peaks_and_small_clusters_not_blobs():
    rng = np.random.default_rng(0)
    frames = []
    for _ in range(8):
        f = rng.normal(500, 8, (60, 80))
        f[10, 12] = 9000                         # isolated hot pixel
        f[30:32, 40:42] = 4000                   # 2x2 cluster
        f[45:50, 60:66] += 3000                  # a blob (not a hot pixel)
        frames.append(f)
    med, pixels, ignored = guide_hotpix.build_map(frames)
    xy = {(p[0], p[1]) for p in pixels}
    assert (12, 10) in xy
    assert {(40, 30), (41, 30), (40, 31), (41, 31)} <= xy
    assert not any(60 <= x < 66 and 45 <= y < 50 for x, y in xy)
    assert ignored >= 1 and med.shape == (60, 80)


@pytest.fixture
async def dark_phd2(tmp_path):
    field = SimField(stars=[], hot=[(20.0, 15.0, 30000.0), (130.0, 100.0, 25000.0)])
    f = FakePHD2(tmp_path / "phd2tmp", field=field)
    port = await f.start()
    phd2_ops._reset_for_tests()
    yield f, port
    await f.close()


async def test_capture_through_phd2_and_status(tmp_path, dark_phd2):
    f, port = dark_phd2
    cfg = _cfg(tmp_path, phd2_port=port)
    res = await guide_hotpix.capture(cfg, "test")
    assert res["ok"], res
    hp = guide_hotpix.load(cfg)
    xy = {(p[0], p[1]) for p in hp["pixels"]}
    assert {(20, 15), (130, 100)} <= xy
    assert (hp["binning"], hp["exposure_ms"], hp["camera"], hp["frames"]) == \
        (2, 20, "GP678C", 8)
    assert store.hotpix_fits_path(cfg).exists()
    # PHD2 was Stopped: we looped, then stopped again; temp frames removed
    m = f.methods()
    assert m.index("loop") < m.index("save_image") and m[-1] == "stop_capture"
    assert not any(__import__("os").path.exists(p) for p in f.saved)
    st = guide_hotpix.status(cfg)
    assert st["exists"] and st["stale"] is None and st["count"] == len(hp["pixels"])
    # current map: maybe_capture skips
    assert (await guide_hotpix.maybe_capture(cfg, "test"))["skipped"]


async def test_capture_refused_while_guiding_or_busy(tmp_path, dark_phd2):
    f, port = dark_phd2
    cfg = _cfg(tmp_path, phd2_port=port)
    f.app_state = "Guiding"
    res = await guide_hotpix.capture(cfg, "test")
    assert not res["ok"] and "Guiding" in res["note"]
    assert "loop" not in f.methods()
    f.app_state = "Stopped"
    async with phd2_ops.hold("selftest"):
        res = await guide_hotpix.capture(cfg, "test")
    assert not res["ok"] and "busy" in res["note"]


async def test_capture_reports_unreachable_phd2(tmp_path):
    phd2_ops._reset_for_tests()
    res = await guide_hotpix.capture(_cfg(tmp_path, phd2_port=1), "test")
    assert res == {"ok": False, "note": "PHD2 not reachable"}


def test_staleness_rules(tmp_path):
    cfg = _cfg(tmp_path, phd2_hotpix_max_age_days=7)
    now = datetime(2026, 10, 2, 12)
    hp = {"created_utc": store.iso_z(now - timedelta(days=2)), "binning": 2,
          "exposure_ms": 2000, "pixels": []}
    assert guide_hotpix.staleness(None, cfg) == "no hot-pixel map yet"
    assert guide_hotpix.staleness(hp, cfg, 2, 2000, now=now) is None
    assert "binning" in guide_hotpix.staleness(hp, cfg, 1, 2000, now=now)
    assert "exposure" in guide_hotpix.staleness(hp, cfg, 2, 3000, now=now)
    assert "older" in guide_hotpix.staleness(hp, cfg, 2, 2000,
                                             now=now + timedelta(days=6))
