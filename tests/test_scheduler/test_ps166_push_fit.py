"""PS-166 follow-up (2026-10-12): the dawn "PhotonScript complete" push must
fit Pushover's 1024-character message limit. The 2026-10-10 push (stored
text cut at 500 by the audit, card from GET /api/morning/report) was about
1.2k characters. Lines carry priorities: card details go first, then the
calibration / integration notes, then the per-rig lines; the headline and
a shutdown failure stay; a trimmed push ends with the dashboard link."""
import pytest

from photonscript.scheduler import morning_report as mr

HEAD = ("Night ended while paused: dawn shutdown stopped the night loop and shut "
        "down: stop ok - guider stop ok - rc16 warm ok/dew ok - piggyback stop ok - "
        "piggyback warm ok/dew ok - park ok - prewarm 2026-10-10,2026-10-11")
SPLIT = "Piggy split 2.5% (1 of 40; 1 saved by gate)"
INTEG = ("Integrations: Heart Nebula v2 integrated: 3.0 h, packet ready; Andromeda "
         "Galaxy v5 integration FAILED; M31 v1 integration FAILED; M31 v3 staged: "
         "0.0 h (not integrated)")
OWED = ("Calibration owed: RC16: flats B 100 d, flats G 100 d, flats L 100 d, "
        "flats R 100 d; Piggy-600: darks 450 s 8/30, flats OSC 21 d")
CARD = "\n".join([
    "RC16: 0 kept / 14 rejected; rejects: fwhm 13, hfr 9, ecc 8; guiding: unguided",
    "Piggy-600: 14 kept / 0 rejected; hours: Heart Nebula 1.1 h (30/30 h), NGC 604 0.6 h",
    "Library: 14 subs filed for sync (Heart Nebula 9, NGC 604 5); 14 auto-approved at dawn",
    "Calibration: missing RC16 Andromeda Galaxy (flats B stale, flats G stale, flats L "
    "stale +1); RC16 tonight (flats B stale, flats G stale, flats L stale +1); Piggy-600 "
    "Andromeda Galaxy (flats OSC stale); Piggy-600 Heart Nebula (flats OSC stale, darks "
    "450 s 8/30); Piggy-600 tonight (darks 450 s 8/30, flats OSC stale)"])


def _live_parts():
    return ([(mr.PRI_KEEP, HEAD), (mr.PRI_RIG, SPLIT), (mr.PRI_NOTE, INTEG),
             (mr.PRI_NOTE, OWED)] + mr.card_parts(CARD))


def test_rig_lines_are_recognised():
    r = {"name": "RC16", "lights": {"kept": 0, "rejected": 14, "review": 0,
                                    "top_reasons": [], "hours": []}}
    assert mr.card_line_priority(mr._rig_line(r)) == mr.PRI_RIG
    pri = [p for p, _t in mr.card_parts(CARD)]
    assert pri == [mr.PRI_RIG, mr.PRI_RIG, mr.PRI_DETAIL, mr.PRI_DETAIL]


def test_short_push_is_unchanged():
    parts = [(mr.PRI_KEEP, "Night complete."), (mr.PRI_NOTE, "Calibration owed: x")]
    assert mr.fit_push(parts) == "Night complete.\nCalibration owed: x"


def test_live_1010_push_fits_and_keeps_the_rig_lines():
    full = "\n".join(t for _p, t in _live_parts())
    assert len(full) > 1024                      # the 10-10 push did not fit
    out = mr.fit_push(_live_parts())
    assert len(out) <= mr.PUSH_MAX_CHARS - mr.PUSH_NOTE_MARGIN
    assert out.startswith(HEAD + "\n" + SPLIT + "\n")
    for ln in CARD.splitlines()[:2]:             # both per-rig lines whole
        assert ln in out
    assert OWED in out and INTEG in out          # notes kept: details went first
    assert "Calibration: missing" not in out or "..." in out
    assert out.endswith(f"(trimmed: full report {mr.DASHBOARD_URL})")


def test_trim_order_details_then_notes_then_rigs():
    out = mr.fit_push(_live_parts(), limit=520)
    assert len(out) <= 520
    assert HEAD in out and CARD.splitlines()[0] in out and CARD.splitlines()[1] in out
    assert "Library:" not in out and "Calibration: missing" not in out
    # tighter: the notes go (or are cut) before the per-rig lines
    out = mr.fit_push(_live_parts(), limit=420)
    assert len(out) <= 420 and HEAD in out
    assert "Integrations" not in out and CARD.splitlines()[0] in out


def test_a_line_is_cut_with_dots_before_it_is_dropped():
    parts = [(mr.PRI_KEEP, "head"), (mr.PRI_DETAIL, "x" * 200)]
    out = mr.fit_push(parts, limit=150, link=None)
    lines = out.splitlines()
    assert lines[0] == "head" and lines[1].endswith("...") and len(out) <= 150
    assert lines[-1] == "(trimmed to fit Pushover)"


def test_keep_lines_are_cut_only_as_a_last_resort():
    parts = [(mr.PRI_KEEP, "h" * 300), (mr.PRI_RIG, "RC16: 1 kept / 0 rejected"),
             (mr.PRI_KEEP, "Shutdown step FAILED: park")]
    out = mr.fit_push(parts, limit=200)
    assert len(out) <= 200 and "Shutdown step FAILED: park" in out
    assert "RC16" not in out


@pytest.mark.asyncio
async def test_night_complete_push_is_fitted(tmp_path, monkeypatch):
    from photonscript.scheduler import armer as armer_mod
    from photonscript.scheduler import calibration_owed, integrations
    from photonscript.scheduler.armer import Armer
    from photonscript.shared.config import PhotonScriptConfig
    import photonscript.scheduler.split_guard as sg
    sent = []

    async def _notify(cfg, msg, title="", priority=0, **kw):
        sent.append((msg, priority))
    monkeypatch.setattr(armer_mod, "notify", _notify)
    monkeypatch.setattr(sg, "morning_split_note", lambda cfg, n: (SPLIT, False))
    monkeypatch.setattr(integrations, "morning_note", lambda cfg: INTEG)
    monkeypatch.setattr(calibration_owed, "morning_note", lambda cfg: OWED)
    monkeypatch.setattr(mr, "push_text", lambda cfg, night: CARD)
    a = Armer(PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data"))
    a.plan = {"night_of": "2026-10-10"}
    a.shutdown = {"failed": ["park FAILED"]}
    await a._notify_complete(HEAD)
    msg, prio = sent[-1]
    assert len(msg) <= 1024 and prio == 1
    assert msg.startswith(HEAD + "\n" + SPLIT)
    assert "Shutdown step FAILED: park FAILED" in msg
    assert CARD.splitlines()[1] in msg and mr.DASHBOARD_URL in msg


def test_audit_keeps_a_whole_push(tmp_path):
    import json
    from photonscript.shared import pushover
    from photonscript.shared.config import PhotonScriptConfig
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data")
    pushover._audit(cfg, "PhotonScript complete", "y" * 1020, 0, True, "sent")
    rec = json.loads(pushover._audit_path(cfg).read_text(encoding="utf-8").splitlines()[-1])
    assert len(rec["message"]) == 1020
