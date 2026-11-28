"""Disk tab: hardlink-aware disk usage, rejected / reclaimable analysis and
the read-only /api/disk endpoints."""
import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler import disk_usage as du
from photonscript.scheduler import runs

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
NOW = datetime(2026, 11, 2, 12, 0)
NIGHT = "2026-08-01"


def _put(p: Path, n: int, mtime: float | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _link(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, dst)
    return dst


@pytest.fixture
def world(tmp_path):
    cap1, cap2 = tmp_path / "NINA", tmp_path / "NINA-Piggyback"
    share = tmp_path / "NINAShare"
    lib = share / "Library"
    sky = tmp_path / "astro" / "TheSkyAutoSave"
    ps = tmp_path / "astro" / "PhotonScript"
    cfg = PhotonScriptConfig(
        _env_file=None, data_dir=tmp_path / "data", image_watch_dir=str(cap1),
        piggyback_image_watch_dir=str(cap2), library_dir=str(lib),
        disk_thesky_autosave_dir=str(sky), disk_photonscript_dir=str(ps),
        disk_thesky_keep_days=30, disk_night_keep_days=60,
        disk_library_backup_dir=r"I:\Astrophotography\RawArchive\Library")
    a = _put(cap1 / NIGHT / "M31" / "LIGHT" / "a.fits", 1000)      # approved
    b = _put(cap1 / NIGHT / "M31" / "LIGHT" / "b.fits", 2000)      # rejected
    f = _put(cap1 / NIGHT / "M31" / "FLAT" / "f.fits", 300)        # calibration
    c = _put(cap2 / NIGHT / "M31" / "c.fits", 4000)                # pending
    bad = _put(cap1 / "2026-10-30" / "BAD" / "d.fits", 500)        # BAD folder
    _link(a, lib / "M31" / "L" / "a.fits")
    _link(b, lib / "_rejected" / "M31" / "L" / "b.fits")
    _put(share / "OneDrive" / "doc.bin", 7000)
    old = time.mktime(NOW.timetuple()) - 90 * 86400
    _put(sky / "old.bin", 600, mtime=old)
    _put(sky / "new.bin", 50)
    _put(ps / "code.py", 80)
    recs = [
        {"file": "M31/LIGHT/a.fits", "abs_path": str(a), "passed_qa": True,
         "reviewed": True, "target": "M31", "filter": "L"},
        {"file": "M31/LIGHT/b.fits", "abs_path": str(b), "passed_qa": False,
         "reason": "elongated stars", "target": "M31", "filter": "L"},
        {"file": "M31/c.fits", "abs_path": str(c), "passed_qa": True,
         "rig": "piggyback", "target": "M31", "filter": "OSC"},
    ]
    (runs.runs_dir(cfg) / f"{NIGHT}_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    du._mem.clear()
    yield cfg, dict(a=a, b=b, f=f, c=c, bad=bad, lib=lib, sky=sky)
    du._mem.clear()


def _area(s, key):
    return next(a for a in s["areas"] if a["key"] == key)


def _night(rows, date, rig):
    return next(r for r in rows if r["date"] == date and r["rig"] == rig)


def test_areas_come_from_config(world):
    cfg, w = world
    keys = [a["key"] for a in du.areas(cfg)]
    assert keys == ["captures_rc16", "captures_piggyback", "library",
                    "library_archive", "share_other", "thesky_autosave",
                    "photonscript", "data"]
    share = next(a for a in du.areas(cfg) if a["key"] == "share_other")
    assert Path(share["path"]) == w["lib"].parent


def test_hardlinks_counted_once(world):
    cfg, _ = world
    s, _f = du.scan(cfg, now=time.mktime(NOW.timetuple()))
    rc = _area(s, "captures_rc16")
    assert rc["bytes"] == 1000 + 2000 + 300 + 500
    lib = _area(s, "library")
    assert lib["bytes"] == 0                       # both are links to captures
    assert lib["shared_bytes"] == 3000 and lib["apparent_bytes"] == 3000
    so = _area(s, "share_other")
    assert so["bytes"] == 7000                     # Library pruned from the share
    assert so["top"][0]["name"] == "OneDrive"
    assert _area(s, "captures_piggyback")["bytes"] == 4000
    assert _area(s, "library_archive")["exists"] is False
    other = _area(s, "other")
    assert other["bytes"] >= 0 and s["drive"]["total"] > 0


def test_rejected_nights_and_thesky(world, monkeypatch):
    cfg, _ = world
    monkeypatch.setattr(du, "_ledger_files", lambda c: {"a.fits"})
    s, files = du.scan(cfg, now=time.mktime(NOW.timetuple()))
    r = s["rejected"]
    assert r["bytes"] == 2000 + 500 and r["files"] == 2
    assert r["links"] == 3                          # b twice, d once
    assert r["verdict_bytes"] == 2000 and r["folder_bytes"] == 2000 + 500
    assert r["links_elsewhere_bytes"] == 0
    n = _night(s["nights"], NIGHT, "rc16")
    assert (n["bytes"], n["kept_bytes"], n["rejected_bytes"],
            n["calibration_bytes"], n["unjudged_bytes"]) == (3300, 1000, 2000, 300, 0)
    assert n["library_bytes"] == 3000 and n["integrated_pct"] == 100.0
    pb = _night(s["nights"], NIGHT, "piggyback")
    assert pb["unjudged_bytes"] == 4000 and pb["library_bytes"] == 0
    assert _night(s["nights"], "2026-10-30", "rc16")["rejected_bytes"] == 500
    assert s["thesky_old"] == {"days": 30, "bytes": 600, "files": 1}


def test_report_sync_candidates_and_paths(world):
    cfg, w = world
    du.save(cfg, *du.scan(cfg, now=time.mktime(NOW.timetuple())))
    du._mem.clear()                                 # read back from disk
    rep = du.report(cfg, pending=set(), now=NOW)
    n = _night(rep["nights"], NIGHT, "rc16")
    assert n["synced"] is True and n["old"] and n["candidate"]
    assert "lib_names" not in n
    assert _night(rep["nights"], NIGHT, "piggyback")["synced"] is None
    rc = rep["reclaimable"]
    assert rc["old_synced_nights"]["bytes"] == 3300
    assert rc["rejected"]["freeable_bytes"] == 2500
    assert rc["thesky_old"]["bytes"] == 600
    assert rc["total_bytes"] == 3300 + 2500 + 600
    assert "-WhatIf" in rc["rejected"]["command"]
    assert "receive-only" in rep["warning"] and "RawArchive" in rep["warning"]
    # a pending Library file means the desktop does not have the night yet
    rep2 = du.report(cfg, pending={"a.fits"}, now=NOW)
    n2 = _night(rep2["nights"], NIGHT, "rc16")
    assert n2["synced"] is False and n2["pending"] == 1 and not n2["candidate"]
    # unknown sync state (Syncthing not configured) is never a candidate
    assert not _night(du.report(cfg, pending=None, now=NOW)["nights"],
                      NIGHT, "rc16")["candidate"]
    # every link of a rejected sub, so deleting them frees the bytes
    rej = {Path(p).as_posix() for p in du.paths(cfg, "rejected")}
    assert {Path(w["b"]).as_posix(), Path(w["bad"]).as_posix(),
            (w["lib"] / "_rejected" / "M31" / "L" / "b.fits").as_posix()} == rej
    assert [Path(p).name for p in du.paths(cfg, "thesky_old")] == ["old.bin"]
    night = du.paths(cfg, "old_synced_nights", pending=set(), now=NOW)
    assert len(night) == 5                          # a x2, b x2, f
    assert du.paths(cfg, "night", date=NIGHT, rig="piggyback") == [str(w["c"])]
    with pytest.raises(ValueError):
        du.paths(cfg, "everything")


def test_archived_nights_are_not_candidates(world):
    cfg, _ = world
    cfg.library_archive_before = "2026-09-01"
    du.save(cfg, *du.scan(cfg, now=time.mktime(NOW.timetuple())))
    n = _night(du.report(cfg, pending=set(), now=NOW)["nights"], NIGHT, "rc16")
    assert n["archived"] is True and n["candidate"] is False


def test_scan_never_deletes(world):
    cfg, w = world
    before = sorted(p for p in Path(cfg.image_watch_dir).parent.rglob("*")
                    if p.is_file() and "data" not in p.parts)
    du.save(cfg, *du.scan(cfg))
    du.report(cfg, pending=set())
    du.paths(cfg, "rejected")
    after = sorted(p for p in Path(cfg.image_watch_dir).parent.rglob("*")
                   if p.is_file() and "data" not in p.parts)
    assert before == after


# --- endpoint ------------------------------------------------------------------

@pytest.fixture
def client(world, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg, w = world
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_syncthing_pending_names", lambda: set())
    monkeypatch.setattr(app, "_remoteneed_cache", {"capped": False})
    calls = []

    def _sync_refresh(c):                          # run the scan inline
        calls.append(1)
        du.refresh(c)
        return True
    monkeypatch.setattr(du, "refresh_bg", _sync_refresh)
    tc = TestClient(app.app)
    tc.calls = calls
    tc.world = w
    return tc


def test_paths_before_any_scan_is_409(client):
    r = client.get("/api/disk/paths", params={"kind": "rejected"})
    assert r.status_code == 409


def test_api_disk_scans_once_then_serves_cache(client):
    r = client.get("/api/disk")
    assert r.status_code == 200 and client.calls == [1]
    d = r.json()
    assert d["ok"] is True and d["scan_started"] is True
    for k in ("drive", "areas", "rejected", "nights", "reclaimable", "warning",
              "scanned_at", "sync_known"):
        assert k in d, k
    assert {"total", "used", "free"} <= set(d["drive"])
    d2 = client.get("/api/disk").json()
    assert client.calls == [1] and d2["scan_started"] is False
    client.get("/api/disk", params={"refresh": 1})
    assert client.calls == [1, 1]
    n = _night(d2["nights"], NIGHT, "rc16")
    assert n["synced"] is True and n["rejected_bytes"] == 2000


def test_api_disk_paths(client):
    client.get("/api/disk")
    r = client.get("/api/disk/paths", params={"kind": "rejected"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    lines = r.text.strip().splitlines()
    assert len(lines) == 3 and all(Path(x).name in ("b.fits", "d.fits") for x in lines)
    assert client.get("/api/disk/paths", params={"kind": "nope"}).status_code == 400
    assert client.get("/api/disk/paths", params={"kind": "night"}).status_code == 400


def test_disk_page_and_pill(client):
    r = client.get("/disk")
    assert r.status_code == 200
    assert 'id="nightTable"' in r.text and "/api/disk" in r.text
    assert 'href="/disk"' not in r.text                      # its own pill hidden
    for path in ("/runs", "/calibration", "/mosaic"):
        assert 'href="/disk" class="nav-pill"' in client.get(path).text, path


def test_new_sources_are_ascii_without_em_dashes():
    for p in (ROOT / "disk_usage.py", ROOT / "routers" / "disk.py",
              ROOT / "templates" / "disk.html", Path(__file__)):
        data = p.read_bytes()
        assert all(b < 128 for b in data), p
