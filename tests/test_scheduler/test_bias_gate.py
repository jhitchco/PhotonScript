"""Bias-library age gate: the roof-closed 50-bias block should only appear
when the bias library is missing or older than config.bias_refresh_days,
so a run of cloudy nights doesn't bank 50 bias every single night."""

import json
from datetime import datetime, timedelta

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (
    ExposurePlan, FilterType, NinaSequenceTarget,
)
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence_json import generate_nina_json
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.calibration import days_since_last_bias


def _make_bias(root, days_ago):
    d = root / (datetime.now().date() - timedelta(days=days_ago)).strftime("%Y-%m-%d") / "BIAS"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bias_0001.fits").write_bytes(b"\x00")


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _has_bias_block(data):
    return any(isinstance(d, dict) and d.get("Name") == "BIAS_IF_STILL_UNSAFE"
               for d in _walk(data))


@pytest.fixture
def gen(tmp_path):
    """Return a builder that installs a config into the module cache and
    generates the night JSON; restores the cache afterward."""
    saved = nsj._gen_cfg_cache

    def _build(refresh_days=60, bias_age_days=None):
        watch = tmp_path / "nina"
        watch.mkdir(exist_ok=True)
        if bias_age_days is not None:
            _make_bias(watch, bias_age_days)
        nsj._gen_cfg_cache = PhotonScriptConfig(
            image_watch_dir=str(watch), bias_refresh_days=refresh_days)
        target = NinaSequenceTarget(
            name="M 42", ra_hours=5.588, dec_degrees=-5.39,
            exposures=[ExposurePlan(filter_type=FilterType.HA,
                                    exposure_seconds=300, count=10, gain=200)])
        seq = build_sequence_for_night("BiasGateTest", [target])
        seq.wait_until_local = "21:00:00"
        return json.loads(generate_nina_json(seq)), watch

    yield _build
    nsj._gen_cfg_cache = saved


def test_days_since_last_bias_none_when_empty(tmp_path):
    cfg = PhotonScriptConfig(image_watch_dir=str(tmp_path))
    assert days_since_last_bias(cfg) is None


def test_days_since_last_bias_counts_newest(tmp_path):
    watch = tmp_path / "nina"; watch.mkdir()
    _make_bias(watch, 40)
    _make_bias(watch, 5)
    cfg = PhotonScriptConfig(image_watch_dir=str(watch))
    assert days_since_last_bias(cfg) == 5


def test_fresh_bias_skips_block(gen):
    data, _ = gen(refresh_days=60, bias_age_days=5)
    assert not _has_bias_block(data)


def test_old_bias_includes_block(gen):
    data, _ = gen(refresh_days=60, bias_age_days=120)
    assert _has_bias_block(data)


def test_empty_library_includes_block(gen):
    data, _ = gen(refresh_days=60, bias_age_days=None)
    assert _has_bias_block(data)


def test_zero_refresh_days_always_includes(gen):
    data, _ = gen(refresh_days=0, bias_age_days=1)
    assert _has_bias_block(data)
