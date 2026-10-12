"""Push text is plain ASCII (2026-10-12): the dawn "PhotonScript complete"
push showed a garbled A-circumflex + middle dot where U+00B7 separated the
shutdown steps. PhotonScript posted correct UTF-8 (notifications.jsonl held
U+00B7), so the bytes were misread after the form POST; push text is now
ASCII and the shutdown steps are joined with " | "."""
import json
from pathlib import Path

import pytest

from photonscript.shared import pushover
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]
DOT = chr(0xB7)


def test_ascii_text_maps_typography():
    steps = f"stop ok {DOT} guider stop ok {DOT} park ok"
    assert pushover.ascii_text(steps) == "stop ok | guider stop ok | park ok"
    assert pushover.ascii_text(f"Disarmed {chr(0x2014)} cooler {chr(0x2192)} -10{chr(0xB0)}C") \
        == "Disarmed - cooler -> -10 degC"
    assert pushover.ascii_text(f"{chr(0x201C)}M31{chr(0x201D)} caf{chr(0xE9)} "
                               f"{chr(0x2026)} {chr(0x2603)}") == '"M31" cafe ... ?'
    assert pushover.ascii_text(None) == "" and pushover.ascii_text("plain") == "plain"
    # the mojibake itself is never produced
    assert chr(0xC2) not in pushover.ascii_text(steps)


@pytest.mark.asyncio
async def test_notify_and_record_send_and_store_ascii(tmp_path, monkeypatch):
    sent = []

    async def _fake_send(config, message, title, priority, sound, **kw):
        sent.append((title, message))
        return True
    monkeypatch.setattr(pushover, "_send_raw", _fake_send)
    monkeypatch.setattr(pushover, "_is_daytime", lambda config, when=None: False)
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path))
    msg = (f"Night complete: dawn shutdown ran (dawn; stop ok {DOT} guider stop ok "
           f"{DOT} park ok)")
    await pushover.notify(cfg, msg, title="PhotonScript complete")
    pushover.record(cfg, f"held {DOT} x", title="PhotonScript NINA watch")
    assert sent == [("PhotonScript complete",
                     "Night complete: dawn shutdown ran (dawn; stop ok | guider stop "
                     "ok | park ok)")]
    raw = (tmp_path / "notifications.jsonl").read_bytes()
    assert raw.isascii()
    recs = [json.loads(x) for x in raw.decode().splitlines()]
    assert all(r["message"].isascii() for r in recs)
    assert recs[1]["message"] == "held | x"
    # the form body that goes to Pushover is plain ASCII too
    import httpx
    body = httpx.Request("POST", pushover.API, data=pushover._payload(
        PhotonScriptConfig(_env_file=None, pushover_api_token="t", pushover_user_key="u"),
        sent[0][1], sent[0][0], 0, "none")).read()
    assert b"%C2" not in body and b"+%7C+" in body


def test_shutdown_steps_are_joined_with_an_ascii_separator():
    src = (ROOT / "photonscript/scheduler/armer.py").read_text(encoding="utf-8")
    assert f'" {DOT} ".join(steps)' not in src and f'" {DOT} safety' not in src
    assert src.count('" | ".join(steps)') == 3
