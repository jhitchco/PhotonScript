"""PS-50: quiet Pushover while the sun is up at the observatory."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from photonscript.shared import pushover as po

LAT, LON = 31.906944, -109.021367  # AARO


def test_sun_altitude_noon_and_midnight():
    noon = datetime(2026, 9, 26, 19, 0, tzinfo=timezone.utc)      # ~12:00 MST solar
    midnight = datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc)
    assert 50 < po.sun_altitude_deg(LAT, LON, noon) < 65
    assert po.sun_altitude_deg(LAT, LON, midnight) < -30


def test_daytime_gate_rules():
    st, now = {}, 1_000_000.0
    w = 4 * 3600
    assert po._daytime_gate(st, now, "PhotonScript heartbeat", -1, w) == (False, "quiet-daytime")
    assert po._daytime_gate(st, now, "PhotonScript NANNY", 1, w) == (True, "ok")
    # same title 30 min later: suppressed (the repeating DISCONNECTED alert)
    assert po._daytime_gate(st, now + 1800, "PhotonScript NANNY", 1, w)[0] is False
    # a different title still goes
    assert po._daytime_gate(st, now + 1800, "PhotonScript armed", 0, w)[0] is True
    # after the window it may repeat
    assert po._daytime_gate(st, now + w + 1, "PhotonScript NANNY", 1, w)[0] is True
    # emergencies always pass
    assert po._daytime_gate(st, now + 2, "PhotonScript NANNY", 2, w) == (True, "ok")


def test_is_daytime_uses_site_and_threshold():
    cfg = SimpleNamespace(observatory_lat=LAT, observatory_lon=LON,
                          pushover_daytime_sun_alt_deg=-3.0)
    assert po._is_daytime(cfg, datetime(2026, 9, 26, 19, 0, tzinfo=timezone.utc))
    assert not po._is_daytime(cfg, datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc))
    assert not po._is_daytime(SimpleNamespace(), datetime(2026, 9, 26, 19, 0,
                                                          tzinfo=timezone.utc))


def test_notify_suppresses_daytime_heartbeat(tmp_path, monkeypatch):
    sent = []

    async def fake_send(config, message, title, priority, sound):
        sent.append((title, message))
        return True

    monkeypatch.setattr(po, "_send_raw", fake_send)
    monkeypatch.setattr(po, "_is_daytime", lambda config, when=None: True)
    cfg = SimpleNamespace(data_dir=str(tmp_path), observatory_lat=LAT,
                          observatory_lon=LON)
    asyncio.run(po.notify(cfg, "Nanny alive.", title="PhotonScript heartbeat",
                          priority=-1))
    asyncio.run(po.notify(cfg, "Safety monitor DISCONNECTED for 32 min",
                          title="PhotonScript NANNY", priority=1))
    asyncio.run(po.notify(cfg, "Safety monitor DISCONNECTED for 62 min",
                          title="PhotonScript NANNY", priority=1))
    assert sent == [("PhotonScript NANNY", "Safety monitor DISCONNECTED for 32 min")]
    # audit trail keeps the suppressed ones
    text = po._audit_path(cfg).read_text()
    assert text.count("quiet-daytime") == 2


def test_notify_night_unchanged(tmp_path, monkeypatch):
    sent = []

    async def fake_send(config, message, title, priority, sound):
        sent.append(title)
        return True

    monkeypatch.setattr(po, "_send_raw", fake_send)
    monkeypatch.setattr(po, "_is_daytime", lambda config, when=None: False)
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    asyncio.run(po.notify(cfg, "Nanny alive.", title="PhotonScript heartbeat",
                          priority=-1))
    assert sent == ["PhotonScript heartbeat"]
