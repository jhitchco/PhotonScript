"""Desktop-transfer batch counter (high-water mark that drains to 100%)."""

import json

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler import sync_batch


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path))


def test_mark_reset_zeroes_baseline_and_timestamps(tmp_path):
    cfg = _cfg(tmp_path)
    st = sync_batch.mark_reset(cfg)
    assert st["baseline_items"] == 0 and st["baseline_bytes"] == 0
    assert st["reset_at"]
    on_disk = json.loads((tmp_path / "sync_batch.json").read_text())
    assert on_disk["baseline_items"] == 0


def test_batch_grows_to_peak_then_drains(tmp_path):
    cfg = _cfg(tmp_path)
    sync_batch.mark_reset(cfg)
    # 150 files / 150 MB freshly queued -> baseline grows to the peak, 0% done
    b = sync_batch.annotate(cfg, 150, 150_000_000)
    assert b["baseline_items"] == 150 and b["transferred_items"] == 0
    assert b["pct"] == 0.0 and b["active"] is True
    # partway: 19 files / 19 MB still pending
    b = sync_batch.annotate(cfg, 19, 19_000_000)
    assert b["transferred_items"] == 131
    assert 87 <= b["pct"] <= 88
    assert b["active"] is True
    # done: nothing pending -> 100%, batch no longer active
    b = sync_batch.annotate(cfg, 0, 0)
    assert b["transferred_items"] == 150 and b["pct"] == 100.0
    assert b["active"] is False
    # baseline is remembered so the widget can say "batch of 150 done"
    assert b["baseline_items"] == 150


def test_baseline_does_not_shrink_on_transient_dip(tmp_path):
    cfg = _cfg(tmp_path)
    sync_batch.mark_reset(cfg)
    sync_batch.annotate(cfg, 100, 100)
    # a later, larger wave extends the same batch
    b = sync_batch.annotate(cfg, 140, 140)
    assert b["baseline_items"] == 140


def test_idle_when_nothing_ever_queued(tmp_path):
    cfg = _cfg(tmp_path)
    b = sync_batch.annotate(cfg, 0, 0)
    assert b["pct"] == 100.0 and b["active"] is False


def test_reset_endpoint_resets_state(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    # seed a non-empty batch, then hit the endpoint
    sync_batch.mark_reset(cfg)
    sync_batch.annotate(cfg, 50, 50)
    out = app.api_sync_reset()
    assert out["ok"] is True
    assert out["batch"]["baseline_items"] == 0
    assert json.loads((tmp_path / "sync_batch.json").read_text())["baseline_items"] == 0
