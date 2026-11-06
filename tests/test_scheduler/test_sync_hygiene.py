"""PS-43: Syncthing backlog census (no 20,000 cap), astro vs other folder
classification, the "non-astronomy folders are being synced" warning with the
ignore patterns, folder-error diagnosis, and the ignore-suggestion helper
(generated, never applied). Syncthing is mocked throughout."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import photonscript.scheduler.app as app
from photonscript.scheduler import sync_hygiene as sh

S = ("http://st", "k", "ninashare-ddpnx-urgun", "DESK")


def _backlog(n_onedrive=30_000, n_library=1_200, n_docs=4_000):
    """Fake remoteneed listing shaped like the live NINAShare backlog."""
    files = []
    for i in range(n_onedrive):
        sub = "Documents" if i % 3 else "Desktop"
        files.append({"name": f"OneDrive/{sub}/f{i}.docx", "size": 500_000})
    for i in range(n_library):
        files.append({"name": f"Library/M31/Ha/m31_{i}.fits", "size": 50_000_000})
    for i in range(n_docs):
        files.append({"name": f"AppData/Local/x{i}.dat", "size": 1_000})
    files.append({"name": "notes.txt", "size": 10})
    return files


class FakeSyncthing:
    """httpx.Client stand-in for remoteneed, folder errors, db status and
    folder config. Records every URL so tests can prove nothing is written."""
    files: list = []
    errors: list = []
    status: dict = {}
    calls: list = []
    fail: bool = False

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("sync hygiene must never write to Syncthing")

    put = patch = delete = post

    def get(self, url, params=None):
        cls = type(self)
        cls.calls.append((url, dict(params or {})))
        if cls.fail:
            raise RuntimeError("connection refused")

        class R:
            def json(self_inner):
                if url.endswith("/rest/db/remoteneed"):
                    page, per = params["page"], params["perpage"]
                    s = (page - 1) * per
                    return {"files": cls.files[s:s + per], "page": page,
                            "perpage": per}
                if url.endswith("/rest/folder/errors"):
                    return {"folder": params["folder"], "errors": cls.errors,
                            "page": 1, "perpage": params["perpage"]}
                if url.endswith("/rest/db/status"):
                    return cls.status
                if "/rest/config/folders/" in url:
                    return {"id": S[2], "label": "NINAShare",
                            "path": "C:\\Users\\jeremy\\NINAShare"}
                raise AssertionError(url)
        return R()


@pytest.fixture
def fake(monkeypatch):
    import httpx
    FakeSyncthing.files = _backlog()
    FakeSyncthing.errors = []
    FakeSyncthing.status = {"state": "idle", "error": "", "errors": 0}
    FakeSyncthing.calls = []
    FakeSyncthing.fail = False
    monkeypatch.setattr(httpx, "Client", FakeSyncthing)
    cache = {"census": None, "census_t": 0.0, "census_fail_t": 0.0,
             "census_error": "", "errors": None, "status": None,
             "errors_t": 0.0, "errors_error": ""}
    monkeypatch.setattr(sh, "_cache", cache)
    return FakeSyncthing


# --- classification ------------------------------------------------------------

@pytest.mark.parametrize("name,cls", [
    ("Library", "astro"), ("NINA", "astro"), ("PHD2", "astro"),
    ("Calibration", "astro"), ("Sequences", "astro"), ("_analysis", "astro"),
    ("NINA Flats 2025", "astro"), ("(root files)", "astro"),
    ("OneDrive", "other"), ("OneDrive - Personal", "other"),
    ("Documents", "other"), ("AppData", "other"), ("Desktop", "other"),
    ("RandomStuff", "other"),
])
def test_classify_folder(name, cls):
    assert sh.classify_folder(name)[0] == cls


def test_ignore_suggestion_never_includes_astro_folders():
    rows = [{"folder": "OneDrive", "class": "other", "files": 3, "bytes": 30},
            {"folder": "Library", "class": "astro", "files": 9, "bytes": 90},
            {"folder": sh.ROOT_FILES, "class": "other", "files": 1, "bytes": 1},
            {"folder": "AppData", "class": "other", "files": 2, "bytes": 20}]
    s = sh.ignore_suggestion(rows, "ninashare-ddpnx-urgun")
    assert s["patterns"] == ["/OneDrive", "/AppData"]
    assert s["applied"] is False
    assert "/Library" not in s["text"] and "/OneDrive\n" in s["text"]
    assert "Ignore Patterns" in s["where"] and "does not delete" in s["note"]
    assert s["files"] == 5 and s["bytes"] == 50
    assert all(ord(ch) < 128 for ch in s["text"] + s["note"] + s["where"])
    odd = sh.ignore_suggestion([{"folder": "Back[up]", "class": "other",
                                 "files": 1, "bytes": 1}])
    assert odd["review"]                                   # flagged for review


# --- census: complete, cheap, observe only --------------------------------------

def test_census_pages_past_the_old_20000_cap(fake):
    pauses = []
    res = sh._walk_census(S, sleep=pauses.append)
    total = len(fake.files)
    assert total > app._REMOTENEED_CAP
    assert res["complete"] is True and res["listed_files"] == total
    assert res["pages"] == total // sh._CENSUS_PERPAGE + 1
    assert len(pauses) == res["pages"] - 1                  # rate limited
    f = res["folders"]
    assert f["OneDrive"]["files"] == 30_000
    assert f["OneDrive"]["subs"]["OneDrive/Documents"][0] == 20_000
    assert f["Library"]["bytes"] == 1_200 * 50_000_000
    assert f[sh.ROOT_FILES]["files"] == 1
    # only counts are kept, never the file list
    assert "names" not in res and "entries" not in res
    assert all(u.endswith("/rest/db/remoteneed") for u, _ in fake.calls)


def test_census_stops_at_page_limit(fake, monkeypatch):
    monkeypatch.setattr(sh, "_CENSUS_MAX_PAGES", 3)
    res = sh._walk_census(S, sleep=lambda s: None)
    assert res["complete"] is False and res["pages"] == 3
    assert res["listed_files"] == 3 * sh._CENSUS_PERPAGE


def test_cadence_and_backoff(fake):
    now = 5_000_000.0
    c = sh._cache
    assert sh.census_due(c, now, armed=False)
    assert not sh.census_due(c, now, armed=True)            # night loop owns it
    assert not sh.errors_due(c, now, armed=True)
    sh.refresh(S, armed=False, now=now, sleep=lambda s: None)
    assert c["census"]["complete"] and c["errors"] is not None
    assert not sh.census_due(c, now + 600, armed=False)     # hourly
    assert sh.errors_due(c, now + 301, armed=False)         # every 5 min
    assert sh.census_due(c, now + 3601, armed=False)
    fake.fail = True
    c["census"] = None
    sh.refresh(S, armed=False, now=now + 4000, sleep=lambda s: None)
    assert c["census_error"].startswith("RuntimeError")
    assert c["errors_error"].startswith("RuntimeError")
    assert not sh.census_due(c, now + 4100, armed=False)    # failure backoff
    assert sh.census_due(c, now + 4000 + sh._CENSUS_FAIL_BACKOFF_S, armed=False)


def test_refresh_bg_runs_once_in_background(fake, monkeypatch):
    monkeypatch.setattr(sh, "_CENSUS_PAGE_PAUSE_S", 0)
    assert sh.refresh_bg(S, armed=False) is True
    for _ in range(200):
        if not sh.refreshing():
            break
        time.sleep(0.02)
    assert sh._cache["census"]["complete"]
    assert sh.refresh_bg(S, armed=False) is False           # nothing due now
    assert sh.refresh_bg(S, armed=True) is False


# --- errors + report ----------------------------------------------------------------

def test_folder_errors_cloud_placeholders(fake):
    fake.errors = [
        {"path": "OneDrive/Documents/a.docx",
         "error": "opening file: The cloud file provider is not running."},
        {"path": "OneDrive/Desktop/b.pdf",
         "error": "hashing: read C:\\x: The cloud operation was unsuccessful."},
        {"path": "Library/M31/Ha/x.fits",
         "error": "open: Access is denied."},
    ]
    fake.status = {"state": "idle", "errors": 812, "error": ""}
    errs, status = sh._fetch_errors(S)
    assert errs["count"] == 3 and errs["total"] == 812
    kinds = {k["kind"]: k["count"] for k in errs["by_kind"]}
    assert kinds == {"cloud_placeholder": 2, "permission": 1}
    assert errs["by_folder"][0] == {"folder": "OneDrive", "count": 2,
                                    "class": "other"}
    assert status["path"].endswith("NINAShare")


def test_report_warning_patterns_and_diagnosis(fake):
    fake.errors = [{"path": "OneDrive/Documents/a.docx",
                    "error": "The cloud file provider is not running."}]
    sh.refresh(S, armed=False, sleep=lambda s: None)
    d = sh.build_report(sh._cache, S[2], "C:\\Users\\jeremy\\NINAShare",
                        need={"items": len(fake.files), "bytes": 0})
    assert d["census"]["complete"] is True
    assert d["folders"][0]["folder"] == "Library"           # sorted by bytes
    assert d["folders"][1]["folder"] == "OneDrive"
    assert d["folders"][1]["class"] == "other"
    assert d["folders"][1]["pattern"] == "/OneDrive"
    w = d["warning"]
    assert w["text"].startswith("Folders: OneDrive, AppData")
    assert w["patterns"] == ["/OneDrive", "/AppData"]
    assert "Ignore Patterns" in w["where"]
    assert "does not delete" in w["note"]
    assert d["other"]["files"] == 34_000
    assert d["astro"]["files"] == 1_201
    assert any("cloud placeholders" in x for x in d["diagnosis"])
    assert any("non-astronomy" in x for x in d["diagnosis"])


def test_report_clean_backlog_has_no_warning(fake):
    fake.files = [{"name": f"Library/M31/Ha/{i}.fits", "size": 1}
                  for i in range(50)]
    sh.refresh(S, armed=False, sleep=lambda s: None)
    d = sh.build_report(sh._cache, S[2])
    assert d["warning"] is None and d["ignore_suggestion"]["patterns"] == []
    assert d["diagnosis"] == []


def test_queue_groups_complete():
    agg: dict = {}
    for n, sz in [("OneDrive/Documents/a", 5), ("OneDrive/Documents/b", 5),
                  ("OneDrive/top.txt", 1), ("Library/M31/Ha/x.fits", 100),
                  ("root.fits", 2)]:
        sh.add_entry(agg, n, sz)
    g = sh.queue_groups({"folders": agg})
    by = {x["folder"]: x for x in g}
    assert by["Library/M31"]["files"] == 1 and by["Library/M31"]["class"] == "astro"
    assert by["OneDrive/Documents"]["files"] == 2
    assert by["OneDrive"]["files"] == 1 and by["OneDrive"]["class"] == "other"
    assert sum(x["files"] for x in g) == 5


# --- endpoints ------------------------------------------------------------------------

@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr(app, "_syncthing_settings", lambda: S)
    monkeypatch.setattr(app, "_armer_active", lambda: False)
    monkeypatch.setattr(sh, "refresh_bg", lambda s, armed: False)
    return TestClient(app.app)


def test_hygiene_endpoint_serves_cache_without_calling_syncthing(client, fake):
    r = client.get("/api/sync/hygiene")
    assert r.status_code == 200
    d = r.json()
    assert d["configured"] is True and d["census"]["available"] is False
    assert fake.calls == []                                 # never on a request
    sh.refresh(S, armed=False, sleep=lambda s: None)
    fake.calls.clear()
    d = client.get("/api/sync/hygiene").json()
    assert d["census"]["complete"] and d["warning"]["patterns"]
    assert fake.calls == []


def test_ignore_suggestion_endpoint(client, fake):
    sh.refresh(S, armed=False, sleep=lambda s: None)
    d = client.get("/api/sync/ignore-suggestion").json()
    assert d["applied"] is False and d["census_complete"] is True
    assert d["patterns"] == ["/OneDrive", "/AppData"]
    assert S[2] in d["where"]
    t = client.get("/api/sync/ignore-suggestion?format=text")
    assert t.status_code == 200 and "/OneDrive" in t.text
    assert t.headers["content-type"].startswith("text/plain")


def test_endpoints_when_not_configured(fake, monkeypatch):
    monkeypatch.setattr(app, "_syncthing_settings", lambda: None)
    c = TestClient(app.app)
    assert c.get("/api/sync/hygiene").json() == {"configured": False}
    assert c.get("/api/sync/ignore-suggestion").json()["patterns"] == []


def test_sync_queue_uses_complete_census(fake, monkeypatch):
    monkeypatch.setattr(app, "_syncthing_settings", lambda: S)
    monkeypatch.setattr(app, "_refresh_remoteneed_bg", lambda s: None)
    rc = {"t": time.time(), "checked_t": time.time(), "names": {"f0.fits"},
          "entries": [("Library/M31/Ha/f0.fits", 10)], "capped": True,
          "need_items": 35_201, "need_bytes": 9_000_000}
    monkeypatch.setattr(app, "_remoteneed_cache", rc)
    d = app.api_sync_queue()
    assert d["partial"] is True and d["folders_from"] == "walk"
    sh.refresh(S, armed=False, sleep=lambda s: None)
    d = app.api_sync_queue()
    assert d["folders_from"] == "census" and d["partial"] is False
    assert d["listed_files"] == len(fake.files)
    assert d["total_files"] == 35_201 and d["total_bytes"] == 9_000_000
    folders = {g["folder"]: g for g in d["groups"]}
    assert folders["OneDrive/Documents"]["files"] == 20_000
    assert folders["OneDrive/Documents"]["class"] == "other"
