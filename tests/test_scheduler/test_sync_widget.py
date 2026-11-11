"""2026-11-11 dashboard transfer widget (/api/sync/widget)."""
from photonscript.scheduler import app as appmod
from photonscript.scheduler import runs


SUBS = [
    {"rig": "rc16", "passed_qa": True, "reviewed": True, "file": "a.fits"},
    {"rig": "rc16", "passed_qa": True, "reviewed": True, "file": "b.fits"},
    {"rig": "rc16", "passed_qa": True, "reviewed": False, "file": "c.fits"},
    {"rig": "rc16", "passed_qa": False, "file": "d.fits"},
    {"rig": "piggyback", "passed_qa": True, "reviewed": True, "file": "p.fits"},
]


def test_widget_counts(monkeypatch):
    monkeypatch.setattr(runs, "_load_subs", lambda cfg, night: [dict(s) for s in SUBS])
    monkeypatch.setattr(appmod, "_syncthing_pending_names", lambda: {"b.fits"})
    monkeypatch.setattr(appmod, "_remoteneed_cache", {
        "entries": [("Library/NGC 604/L/b.fits", 60_000_000),
                    ("Library/NGC 604/L/x.xisf", 40_000_000),
                    ("Library/notes.txt", 10)],
        "need_items": 3, "capped": False, "t": 1.0})
    d = appmod.api_sync_widget()
    assert d["known"] and d["pending_subs"] == 2 and d["pending_other"] == 1
    assert d["pending_sub_bytes"] == 100_000_000
    assert d["folders"] == [{"folder": "Library/NGC 604", "subs": 2,
                             "bytes": 100_000_000}]
    rc = d["by_rig"]["rc16"]
    assert (rc["shot"], rc["kept"], rc["filed"], rc["on_desktop"], rc["pending"]) == (4, 3, 2, 1, 1)
    assert d["by_rig"]["piggyback"]["on_desktop"] == 1


def test_widget_unknown_before_first_walk(monkeypatch):
    monkeypatch.setattr(runs, "_load_subs", lambda cfg, night: [dict(s) for s in SUBS])
    monkeypatch.setattr(appmod, "_syncthing_pending_names", lambda: None)
    monkeypatch.setattr(appmod, "_remoteneed_cache", {})
    d = appmod.api_sync_widget()
    assert not d["known"] and d["pending_subs"] == 0
    assert d["by_rig"]["rc16"]["unknown"] == 2
