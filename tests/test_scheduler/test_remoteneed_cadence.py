"""PS-75: the Syncthing remoteneed walk runs on a slow cadence, backs off
while a capped backlog is not draining, skips while armed, and a capped
(partial) list never claims a file has transferred."""

from __future__ import annotations

import time

import pytest

import photonscript.scheduler.app as app

S = ("http://st", "k", "lib", "DESK")


@pytest.fixture
def cache(monkeypatch):
    c = {"t": 0.0, "names": None}
    monkeypatch.setattr(app, "_remoteneed_cache", c)
    monkeypatch.setattr(app, "_armer_active", lambda: False)
    return c


# --- cadence rule --------------------------------------------------------------

def test_walk_due_rules(cache):
    now = 1_000_000.0
    due = app._remoteneed_walk_due
    assert due(cache, now, armed=False)                       # never walked
    cache.update(names=set(), t=now - 300, checked_t=now - 300)
    assert not due(cache, now, armed=False)                   # 5 min old
    cache.update(t=now - 660, checked_t=now - 660)
    assert due(cache, now, armed=False)                       # 11 min old
    cache["capped"] = True
    assert not due(cache, now, armed=False)                   # capped: 15 min
    cache.update(t=now - 960, checked_t=now - 960)
    assert due(cache, now, armed=False)
    assert not due(cache, now, armed=True)                    # night loop owns it
    cache["fail_t"] = now - 60
    assert not due(cache, now, armed=False)                   # failure backoff


def test_dashboard_polling_no_longer_walks_back_to_back(cache, monkeypatch):
    """The dashboard polls /api/sync/queue every 30 s. Over an hour that used
    to mean ~30 walks (each ~65 s, back to back); now at most one per 10 min."""
    monkeypatch.setattr(app, "_syncthing_settings", lambda: S)
    clock = [2_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    kicks = []

    def fake_bg(settings):
        kicks.append(clock[0])
        cache.update(names={"a.fits"}, entries=[("x/a.fits", 1)],
                     t=clock[0], checked_t=clock[0], capped=False)

    monkeypatch.setattr(app, "_refresh_remoteneed_bg", fake_bg)
    for _ in range(120):                                       # one hour
        app._syncthing_pending_names()
        clock[0] += 30
    assert 5 <= len(kicks) <= 7, len(kicks)
    gaps = [b - a for a, b in zip(kicks, kicks[1:])]
    assert min(gaps) >= app._REMOTENEED_FRESH_S


def test_no_walk_while_armed(cache, monkeypatch):
    monkeypatch.setattr(app, "_syncthing_settings", lambda: S)
    monkeypatch.setattr(app, "_armer_active", lambda: True)
    kicked = []
    monkeypatch.setattr(app, "_refresh_remoteneed_bg", lambda s: kicked.append(1))
    assert app._syncthing_pending_names() is None
    assert kicked == []


# --- the walk itself -------------------------------------------------------------

class FakeClient:
    """httpx.Client stand-in: `total` files in remoteneed, 500 per page."""
    total = 45_000
    need_calls = 0
    walk_pages = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        cls = type(self)

        class R:
            def json(self_inner):
                if url.endswith("/rest/db/completion"):
                    cls.need_calls += 1
                    return {"needItems": cls.total, "needBytes": cls.total * 1000}
                cls.walk_pages += 1
                page, per = params["page"], params["perpage"]
                start = (page - 1) * per
                n = max(0, min(per, cls.total - start))
                return {"files": [{"name": f"L/_/Ha/f{start + i}.fits",
                                   "size": 1000} for i in range(n)]}
        return R()


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx
    FakeClient.need_calls = FakeClient.walk_pages = 0
    FakeClient.total = 45_000
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return FakeClient


def test_capped_walk_is_flagged(cache, fake_httpx):
    names = app._refresh_remoteneed(S)
    assert len(names) == app._REMOTENEED_CAP == 20_000
    assert cache["capped"] is True
    assert fake_httpx.walk_pages == 40


def test_short_walk_not_capped(cache, fake_httpx):
    fake_httpx.total = 1_120
    names = app._refresh_remoteneed(S)
    assert len(names) == 1_120 and cache["capped"] is False
    assert fake_httpx.walk_pages == 3


def test_capped_backlog_not_draining_skips_walk(cache, fake_httpx):
    app._refresh_remoteneed_maybe(S)                 # first: walks, capped
    assert fake_httpx.walk_pages == 40 and cache["need_at_walk"] == 45_000
    fake_httpx.walk_pages = 0
    cache.update(t=0.0, checked_t=0.0)
    app._refresh_remoteneed_maybe(S)                 # still 45k: skip
    assert fake_httpx.walk_pages == 0
    assert cache["checked_t"] > 0 and cache["t"] > 0  # partial answer renewed
    fake_httpx.total = 44_000                        # draining: walk again
    app._refresh_remoteneed_maybe(S)
    assert fake_httpx.walk_pages == 40
    fake_httpx.walk_pages = 0
    fake_httpx.total = 900                           # under the cap: full walk
    app._refresh_remoteneed_maybe(S)
    assert fake_httpx.walk_pages == 2 and cache["capped"] is False
    assert cache["need_items"] == 900


# --- partial answers ---------------------------------------------------------------

def test_transfer_state_with_capped_list(cache):
    pending = {"a.fits"}
    cache["capped"] = False
    assert app._transfer_state("a.fits", pending) == "pending"
    assert app._transfer_state("b.fits", pending) == "done"
    cache["capped"] = True
    assert app._transfer_state("a.fits", pending) == "pending"
    assert app._transfer_state("b.fits", pending) is None    # unknown, not done
    assert app._transfer_state("a.fits", None) is None


def test_sync_queue_reports_true_total_when_capped(cache, monkeypatch):
    monkeypatch.setattr(app, "_syncthing_settings", lambda: S)
    monkeypatch.setattr(app, "_refresh_remoteneed_bg", lambda s: None)
    now = time.time()
    cache.update(names={"f0.fits", "f1.fits"}, t=now, checked_t=now,
                 entries=[("L/_/Ha/f0.fits", 10), ("L/_/Ha/f1.fits", 10)],
                 capped=True, need_items=45_000, need_bytes=9_000_000)
    d = app.api_sync_queue()
    assert d["total_files"] == 45_000 and d["listed_files"] == 2
    assert d["partial"] is True and d["total_bytes"] == 9_000_000
    cache.update(capped=False, need_items=2)
    d = app.api_sync_queue()
    assert d["total_files"] == 2 and d["partial"] is False
    assert d["total_bytes"] == 20
