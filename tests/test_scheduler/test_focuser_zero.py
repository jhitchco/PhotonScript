"""Focuser reporting position 0 (2026-10-08: the RC16 EAF dropped off USB,
NINA kept it Connected at Position 0, every later light had FOCPOS 0 at about
5" FWHM). Per-sub reject (focuser_zero), the live once-per-night page, and
the qa-focuser-zero pass over the subs logs and the Library."""
import asyncio
import json
from datetime import datetime

import numpy as np
import pytest

from photonscript.scheduler import focuser_zero as fz
from photonscript.shared import qa_rules as q
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-10-08"
GOOD = dict(hfr=3.0, ecc=0.4, stars=500, background=400.0, exp_s=300.0,
            ccd_temp=0.2, exposure="ok")


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                stamp_fits_object=False, observatory_tz="UTC",
                library_attribute=False)
    base.update(kw)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return PhotonScriptConfig(**base)


def _card(cfg, image_type="LIGHT", **m):
    return q.evaluate({**GOOD, **m}, q.context(cfg, "rc16", image_type=image_type))


def _row(card):
    return next(c for c in card.checks if c.id == "focuser_zero")


# ----------------------------------------------------------- per-sub check

def test_light_with_focpos_zero_is_rejected_with_a_reason_code(tmp_path):
    from photonscript.scheduler.sub_index import reason_codes, reason_label
    card = _card(_cfg(tmp_path), focpos=0)
    assert not card.passed and card.verdict == q.REJECTED
    assert card.drivers == ["focuser_zero"]
    assert "focuser reported position 0 (disconnected or frozen); focus unknown" \
        in card.reason
    rec = card.record_fields()
    assert reason_codes(rec, "rejected") == ["focuser_zero"]
    assert reason_label("focuser_zero") == "Focuser position"
    # every card still lists the whole big list, in order
    assert [r[0] for r in card.compact()["rows"]] == list(q.CHECKS)


def test_calibration_frames_with_focpos_zero_are_kept(tmp_path):
    cfg = _cfg(tmp_path)
    for kind in ("DARK", "BIAS", "FLAT", "DARKFLAT", "FLATDARK", "Dark Frame"):
        card = _card(cfg, image_type=kind, focpos=0)
        row = _row(card)
        assert row.status == q.SKIP and "calibration" in row.reason, kind
        assert "focuser_zero" not in card.drivers


def test_missing_focpos_is_unknown_not_a_reject(tmp_path):
    card = _card(_cfg(tmp_path))
    row = _row(card)
    assert row.status == q.SKIP and "unknown" in row.reason
    assert card.passed and card.verdict == q.APPROVED


def test_a_real_position_passes(tmp_path):
    card = _card(_cfg(tmp_path), focpos=5845, focuser_live_pos=5845)
    assert _row(card).status == q.PASS and _row(card).value == 5845
    assert card.passed


def test_live_read_of_zero_rejects_even_with_a_header_position(tmp_path):
    card = _card(_cfg(tmp_path), focpos=5845, focuser_live_pos=0)
    assert card.drivers == ["focuser_zero"]
    assert "live focuser read 0" in card.reason


def test_mode_softens_or_turns_off(tmp_path):
    warn = _card(_cfg(tmp_path, qa_focuser_zero_mode="warn"), focpos=0)
    assert warn.passed and warn.verdict == q.NEEDS_LOOK
    off = _card(_cfg(tmp_path, qa_focuser_zero_mode="off"), focpos=0)
    assert off.passed and _row(off).status == q.SKIP


def test_score_on_still_rejects(tmp_path):
    card = _card(_cfg(tmp_path, qa_score_mode="on"), focpos=0)
    assert card.score.value <= 20 and card.score.decision == "reject"
    assert not card.passed


def test_rescore_reads_the_stored_focpos(tmp_path):
    m = q.metrics_from_record({"focpos": 0, "focuser_live_pos": None})
    assert m["focpos"] == 0
    card = q.evaluate({**GOOD, **m}, q.context(_cfg(tmp_path), "rc16"))
    assert card.drivers == ["focuser_zero"]


def test_defaults_and_system_page_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    cfg = PhotonScriptConfig(_env_file=None)
    assert cfg.qa_focuser_zero_mode == "fail" and cfg.focuser_zero_alert is True
    by = {f[0]: f for f in _CONFIG_FIELDS}
    assert by["qa_focuser_zero_mode"][1] == "PS_QA_FOCUSER_ZERO_MODE"
    assert by["focuser_zero_alert"][1] == "PS_FOCUSER_ZERO_ALERT"


# ----------------------------------------------------------- live alert

class _State:
    def __init__(self, imaging=True):
        from photonscript.shared.models import SessionState
        self.session_state = SessionState.IMAGING if imaging else SessionState.IDLE
        self.current_target = "Andromeda Galaxy"


ZERO = {"Connected": True, "Position": 0, "StepSize": 600000,
        "Temperature": 21.29, "DisplayName": "EAF(MainRC16)"}


def _agent(monkeypatch, cfg, rig="rc16", imaging=True):
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = cfg
    a.rig = rig
    a.state = _State(imaging)
    sent, held = [], []

    async def fake_notify(config, msg, **kw):
        sent.append((msg, kw))
    monkeypatch.setattr(agent_mod, "notify", fake_notify)
    monkeypatch.setattr("photonscript.shared.pushover.record",
                        lambda c, m, **kw: held.append(m))
    return a, sent, held


def _tick(a, focuser, *, expire=True):
    asyncio.run(a._focuser_zero_watch(focuser))
    if expire and getattr(a, "_focuser_zero_since", None) is not None:
        a._focuser_zero_since -= a.FOCUSER_ZERO_GRACE_S + 1


def test_alert_fires_once_per_night_per_rig(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    a, sent, held = _agent(monkeypatch, cfg)
    _tick(a, ZERO)                       # first zero read arms the grace timer
    assert sent == []
    for _ in range(5):
        _tick(a, ZERO)
    assert len(sent) == 1
    msg, kw = sent[0]
    assert msg.startswith("RC16: focuser EAF(MainRC16) reports position 0")
    assert "USB" in msg and "power" in msg and kw["priority"] == 1
    # a restarted agent the same night does not push again (latched)
    b, sent_b, held_b = _agent(monkeypatch, cfg)
    for _ in range(3):
        _tick(b, ZERO)
    assert sent_b == [] and len(held_b) == 1
    # the other rig has its own latch
    c, sent_c, _ = _agent(monkeypatch, cfg, rig="piggyback")
    for _ in range(3):
        _tick(c, ZERO)
    assert len(sent_c) == 1 and sent_c[0][0].startswith("Piggy-600: focuser")
    # a run event for the morning debrief
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import night_of
    ev = [json.loads(x) for x in events_path(cfg, night_of(cfg)).read_text()
          .splitlines() if x.strip()]
    assert [e["kind"] for e in ev].count("focuser_zero") == 3


def test_no_alert_when_not_imaging_or_brief_or_real_position(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    idle, sent, _ = _agent(monkeypatch, cfg, imaging=False)
    for _ in range(3):
        _tick(idle, ZERO)
    assert sent == []
    a, sent, _ = _agent(monkeypatch, cfg)
    _tick(a, ZERO, expire=False)
    _tick(a, ZERO, expire=False)                       # inside the grace
    _tick(a, {**ZERO, "Position": 5845})               # recovered: resets
    _tick(a, ZERO, expire=False)
    assert sent == []
    _tick(a, {**ZERO, "Connected": False})             # disconnected: not this alert
    assert sent == [] and a._focuser_zero_since is None


def test_alert_config_key_turns_it_off(tmp_path, monkeypatch):
    a, sent, _ = _agent(monkeypatch, _cfg(tmp_path, focuser_zero_alert=False))
    for _ in range(3):
        _tick(a, ZERO)
    assert sent == []


def test_live_pos_only_from_a_connected_focuser(tmp_path, monkeypatch):
    a, _, _ = _agent(monkeypatch, _cfg(tmp_path))
    assert a._focuser_live_pos() is None
    a._focuser_info = ZERO
    assert a._focuser_live_pos() == 0
    a._focuser_info = {**ZERO, "Connected": False}
    assert a._focuser_live_pos() is None


# ------------------------------------------------- subs log verdict pass

def _approved(cfg, name, rig="rc16", **kw):
    card = q.evaluate(GOOD, q.context(cfg, rig))
    return {"rig": rig, "file": name, "target": "Andromeda Galaxy",
            "filter": "L", **GOOD, **card.record_fields(), **kw}


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("photonscript.scheduler.runs.sync_goal_progress",
                        lambda c: [])


def test_night_pass_rejects_zero_lights_and_keeps_human_verdicts(tmp_path, quiet):
    from photonscript.scheduler.runs import _load_subs, append_sub_record
    cfg = _cfg(tmp_path)
    append_sub_record(cfg, NIGHT, _approved(cfg, "ok.fits", focpos=5845))
    append_sub_record(cfg, NIGHT, _approved(cfg, "z1.fits", focpos=0))
    append_sub_record(cfg, NIGHT, _approved(cfg, "z2.fits", focpos=0,
                                            review_source="manual",
                                            reviewed=True))
    append_sub_record(cfg, NIGHT, _approved(cfg, "nofp.fits"))
    append_sub_record(cfg, NIGHT, _approved(cfg, "pz.fits", rig="piggyback",
                                            focpos=0))
    dry = fz.night_pass(cfg, NIGHT, rig="rc16")
    assert dry["mode"] == "dry-run" and dry["subs"] == 4 and dry["zero"] == 2
    assert dry["newly_rejected"] == 1 and dry["human_kept"] == 1
    assert dry["unknown_focpos"] == 1 and dry["records_updated"] == 0
    assert all(r["passed_qa"] for r in _load_subs(cfg, NIGHT))   # untouched
    out = fz.night_pass(cfg, NIGHT, apply=True, rig="rc16")
    assert out["records_updated"] == 1
    by = {r["file"]: r for r in _load_subs(cfg, NIGHT)}
    assert by["z1.fits"]["passed_qa"] is False
    assert by["z1.fits"]["drivers"] == ["focuser_zero"]
    assert not by["z1.fits"].get("reviewed")          # the auto-approve undone
    assert by["z2.fits"]["passed_qa"] is True          # a person decided
    assert by["ok.fits"]["passed_qa"] and by["nofp.fits"]["passed_qa"]
    assert by["pz.fits"]["passed_qa"] is True          # --rig rc16 only
    again = fz.night_pass(cfg, NIGHT, apply=True, rig="rc16")
    assert again["records_updated"] == 0 and again["already_rejected"] == 1


def test_night_pass_without_a_log(tmp_path):
    out = fz.night_pass(_cfg(tmp_path), "2026-10-09")
    assert out.get("missing_log") is True and out["zero"] == 0


# ----------------------------------------------------------- Library scan

def _fits(path, kind="LIGHT", focpos=0, bayer=False, minute=0):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = kind
    h["EXPTIME"] = 300.0
    h["INSTRUME"] = "AP26CC" if bayer else "AP26MC"
    if bayer:
        h["BAYERPAT"] = "RGGB"
    else:
        h["FILTER"] = "L"
    if focpos is not None:
        h["FOCPOS"] = focpos
    h["DATE-LOC"] = f"2026-10-08T21:{minute:02d}:00"
    h["DATE-OBS"] = f"2026-10-09T03:{minute:02d}:00"
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((4, 4), np.uint16), header=h).writeto(path)


@pytest.fixture
def lib(tmp_path):
    L = tmp_path / "Library"
    _fits(L / "Andromeda Galaxy" / "L" / "a0.fits", minute=1)
    _fits(L / "Andromeda Galaxy" / "L" / "a1.fits", minute=2)
    _fits(L / "Andromeda Galaxy" / "L" / "a2.fits", focpos=5845, minute=3)
    _fits(L / "Andromeda Galaxy" / "L" / "a3.fits", focpos=None, minute=4)
    _fits(L / "Andromeda Galaxy" / "OSC" / "p0.fits", bayer=True, minute=5)
    _fits(L / "_rejected" / "NGC 604" / "L" / "r0.fits", minute=6)
    _fits(L / "Calibration" / "DARK" / "2026-10-08" / "d0.fits", kind="DARK")
    return L


def test_scan_library_lists_only_zero_lights(lib):
    hits = fz.scan_library(lib, tz="UTC")
    assert sorted(h["file"] for h in hits) == ["a0.fits", "a1.fits",
                                                "p0.fits", "r0.fits"]
    g = fz.summarize(hits)
    by = {(x["rig"], x["target"]): x for x in g}
    assert by[("rc16", "Andromeda Galaxy")]["n"] == 2
    assert by[("piggyback", "Andromeda Galaxy")]["n"] == 1
    assert by[("rc16", "NGC 604")]["in_rejected"] == 1
    assert all(x["night"] == "2026-10-08" for x in g)
    assert [h["file"] for h in fz.scan_library(lib, rig="piggyback",
                                               tz="UTC")] == ["p0.fits"]
    assert fz.scan_library(lib, since="2026-10-09", tz="UTC") == []


def test_cli_dry_run_lists_and_writes_nothing(lib, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda p: cfg)
    before = sorted(p.stat().st_mtime_ns for p in lib.rglob("*") if p.is_file())
    res = CliRunner().invoke(cli.app, ["qa-focuser-zero", "--since", "2026-10-08",
                                       "--rig", "rc16", "--library", str(lib)])
    assert res.exit_code == 0, res.output
    assert "3 light(s) with FOCPOS 0" in res.output
    assert "Andromeda Galaxy: 2" in res.output
    assert "No subs logs here" in res.output
    after = sorted(p.stat().st_mtime_ns for p in lib.rglob("*") if p.is_file())
    assert before == after
    res = CliRunner().invoke(cli.app, ["qa-focuser-zero", "--library", str(lib),
                                       "--json"])
    doc = json.loads(res.output)
    assert doc["mode"] == "dry-run" and doc["library_lights"] == 4


def test_alert_text_names_the_focuser():
    cfg = PhotonScriptConfig(_env_file=None)
    t = fz.alert_text(cfg, "rc16", ZERO, "M 31")
    assert "EAF(MainRC16)" in t and "(M 31)" in t and t.isascii()
    assert fz.is_zero_reading(ZERO) and not fz.is_zero_reading({"Position": 5845})
    assert not fz.is_zero_reading({}) and not fz.is_zero_reading(None)
