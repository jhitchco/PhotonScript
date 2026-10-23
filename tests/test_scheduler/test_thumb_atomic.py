"""PS-59 (thumbnail half): thumbnails and contact sheets are written
atomically, so a kill mid-write never leaves a truncated cached PNG."""

from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image

from photonscript.scheduler import runs


def _frame():
    rng = np.random.default_rng(1)
    return rng.normal(1000, 20, (60, 80)).astype(np.float32)


def test_thumbnail_written_whole_without_temp_files(tmp_path):
    out = tmp_path / "thumbs" / "d" / "a_264.png"
    runs._stretch_and_save(_frame(), out, 40)
    with Image.open(out) as im:
        im.verify()
        assert im.format == "PNG"
    assert [p.name for p in out.parent.iterdir()] == ["a_264.png"]


def test_crash_mid_write_leaves_no_cached_file(tmp_path, monkeypatch):
    out = tmp_path / "t.png"
    real_save = Image.Image.save

    def dying_save(self, fp, *a, **k):
        with open(fp, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n trunc")      # half a file...
        raise OSError("killed")                       # ...then the kill

    monkeypatch.setattr(Image.Image, "save", dying_save)
    with pytest.raises(OSError):
        runs._stretch_and_save(_frame(), out, 40)
    assert not out.exists()                           # cache stays empty
    assert list(tmp_path.iterdir()) == []             # temp cleaned up
    monkeypatch.setattr(Image.Image, "save", real_save)
    runs._stretch_and_save(_frame(), out, 40)         # next view rebuilds it
    with Image.open(out) as im:
        im.verify()


def test_lost_replace_race_keeps_existing(tmp_path, monkeypatch):
    out = tmp_path / "t.png"
    runs._stretch_and_save(_frame(), out, 40)
    before = out.read_bytes()

    def locked(src, dst):
        raise PermissionError("in use by a reader")   # Windows open handle

    monkeypatch.setattr(os, "replace", locked)
    runs._stretch_and_save(_frame(), out, 40)
    assert out.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["t.png"]
