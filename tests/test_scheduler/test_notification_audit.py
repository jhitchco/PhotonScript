"""Notification audit trail — every Pushover decision is logged + tallyable."""
import json
from datetime import datetime, timezone

import pytest

from photonscript.shared import pushover
from photonscript.shared.config import PhotonScriptConfig


@pytest.mark.asyncio
async def test_notify_writes_sent_and_suppressed_records(tmp_path, monkeypatch):
    async def _fake_send(config, message, title, priority, sound):
        return True   # pretend the POST succeeded — no network
    monkeypatch.setattr(pushover, "_send_raw", _fake_send)
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path))

    await pushover.notify(cfg, "hello", title="PhotonScript guiding", priority=1)
    # identical within the dedup window -> suppressed, but still audited
    await pushover.notify(cfg, "hello", title="PhotonScript guiding", priority=1)

    recs = [json.loads(x) for x in
            (tmp_path / "notifications.jsonl").read_text().splitlines()]
    assert len(recs) == 2
    assert recs[0]["sent"] is True and recs[0]["reason"] == "sent"
    assert recs[0]["title"] == "PhotonScript guiding" and recs[0]["priority"] == 1
    assert recs[1]["sent"] is False and recs[1]["reason"] == "dedup"


def test_api_notifications_tallies_by_title(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"ts": now, "title": "PhotonScript cooler", "sent": True,
         "reason": "sent", "priority": 1, "message": "x"},
        {"ts": now, "title": "PhotonScript cooler", "sent": False,
         "reason": "dedup", "priority": 1, "message": "x"},
        {"ts": now, "title": "PhotonScript guiding", "sent": True,
         "reason": "sent", "priority": 1, "message": "y"},
        {"ts": "2000-01-01T00:00:00+00:00", "title": "PhotonScript old",
         "sent": True, "reason": "sent", "priority": 0, "message": "old"},
    ]
    (tmp_path / "notifications.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(app, "_config",
                        PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path)))
    out = app.api_notifications(since_hours=24)
    assert out["count"] == 3   # the year-2000 row is outside the 24h window
    assert out["summary"]["PhotonScript cooler"] == {
        "total": 2, "sent": 1, "suppressed": 1}
    assert out["sent_total"] == 2 and out["suppressed_total"] == 1
    # title filter narrows it
    out2 = app.api_notifications(since_hours=24, title="guiding")
    assert out2["count"] == 1 and "PhotonScript guiding" in out2["summary"]
