"""PS-165: subs exposed during a PHD2 no-corrections episode (PS-155).

PHD2 said Guiding but no correction reached the mount: such a sub is
unguided in practice. It is flagged qa_flag 'unguided-in-name', judged on
unguided limits (the rig's ecc gate + a margin, HFR gate x a factor) and
held for review instead of auto-rejected on the guided gates; the runs page
tags it and the guard line counts it.
"""
import json
from datetime import datetime
from pathlib import Path

from photonscript.scheduler import runs
from photonscript.shared import phd2_store as store
from photonscript.shared import qa_rules
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path

NIGHT = "2026-10-06"


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    return PhotonScriptConfig(_env_file=None, **kw)


def _card(cfg, rig="rc16", **m):
    base = dict(hfr=3.0, ecc=0.3, stars=200, background=900, exp_s=300,
                ccd_temp=0.0, guide_rms=0.05, guide_state="guiding")
    base.update(m)
    return qa_rules.evaluate(qa_rules.record_metrics(**base),
                             qa_rules.context(cfg, rig, "Heart", "Ha"))


def _check(card, cid):
    return next((c for c in card.checks if c.id == cid), None)


def _episode(cfg, start, end=None):
    store.append_jsonl(store.guard_path(cfg, NIGHT), {
        "event": "open", "id": "n1", "t_utc": start,
        "kind": "no_corrections", "codes": ["D7"]})
    if end:
        store.append_jsonl(store.guard_path(cfg, NIGHT), {
            "event": "close", "id": "n1", "t_utc": end, "codes": ["D7"]})


# ---- thresholds and the card ------------------------------------------------

def test_unguided_limits_derived_from_the_rig_gates():
    t = qa_rules.thresholds(_cfg(quality_eccentricity_max=0.6,
                                 quality_hfr_abs_max=8.0))
    assert t["unguided_ecc_max"] == 0.7
    assert t["unguided_hfr_max"] == 10.0
    t = qa_rules.thresholds(_cfg(quality_eccentricity_max=0.9))
    assert t["unguided_ecc_max"] == 0.95          # capped
    t = qa_rules.thresholds(_cfg(qa_unguided_eccentricity_max=0.8,
                                 qa_unguided_hfr_max=12.0))
    assert t["unguided_ecc_max"] == 0.8 and t["unguided_hfr_max"] == 12.0


def test_nocorr_sub_held_for_review_not_rejected():
    cfg = _cfg(quality_eccentricity_max=0.6)
    guided = _card(cfg, ecc=0.65)
    assert not guided.passed and "ecc" in guided.drivers
    card = _card(cfg, ecc=0.65, guide_nocorr=True)
    assert card.passed and card.verdict == qa_rules.NEEDS_LOOK
    assert not card.auto_approved
    assert card.qa_flag == qa_rules.UNGUIDED_IN_NAME
    assert _check(card, "guide_nocorr").status == qa_rules.WARN
    assert _check(card, "ecc").limit == 0.7
    assert _check(card, "guide_rms").status == qa_rules.SKIP
    fields = card.record_fields()
    assert fields["qa_flag"] == "unguided-in-name" and fields["passed_qa"]
    assert "reviewed" not in fields


def test_nocorr_sub_still_rejected_past_the_unguided_limits():
    card = _card(_cfg(quality_eccentricity_max=0.6), ecc=0.8, guide_nocorr=True)
    assert not card.passed and "ecc" in card.drivers
    assert card.qa_flag == qa_rules.UNGUIDED_IN_NAME
    card = _card(_cfg(quality_hfr_abs_max=8.0), hfr=11.0, guide_nocorr=True)
    assert not card.passed and "hfr" in card.drivers


def test_roof_flag_wins_and_metrics_round_trip():
    assert qa_rules.metrics_from_record({"guide_nocorr": True})["guide_nocorr"]
    card = _card(_cfg(), background=0.0, stars=0, hfr=None, ecc=None,
                 guide_nocorr=True)
    assert card.qa_flag in ("roof-closed", qa_rules.UNGUIDED_IN_NAME)
    rows = qa_rules.expand(_card(_cfg(), guide_nocorr=True).compact())
    assert any(r["id"] == "guide_nocorr" for r in rows)


# ---- windows ------------------------------------------------------------------

def test_nocorr_windows_from_episodes_and_events(tmp_path):
    cfg = _cfg(tmp_path)
    _episode(cfg, "2026-10-07T04:00:00Z", "2026-10-07T05:00:00Z")
    # a non-star episode is not a no-corrections window
    store.append_jsonl(store.guard_path(cfg, NIGHT), {
        "event": "open", "id": "x", "t_utc": "2026-10-07T02:00:00Z",
        "kind": "non_star"})
    # a run event inside the episode adds nothing; one outside adds 30 min
    p = events_path(cfg, NIGHT)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "2026-10-07T04:00:05Z",
                             "kind": "no_corrections"}) + "\n")
        fh.write(json.dumps({"t": "2026-10-07T08:00:00Z",
                             "kind": "no_corrections"}) + "\n")
    w = store.nocorr_windows(cfg, NIGHT)
    assert w == [(datetime(2026, 10, 7, 4), datetime(2026, 10, 7, 5)),
                 (datetime(2026, 10, 7, 8), datetime(2026, 10, 7, 8, 30))]
    assert store.in_windows(w, datetime(2026, 10, 7, 3, 58), 300)
    assert not store.in_windows(w, datetime(2026, 10, 7, 5, 0), 300)
    assert not store.in_windows(w, None, 300)


def test_open_episode_runs_to_now(tmp_path):
    cfg = _cfg(tmp_path)
    _episode(cfg, "2026-10-07T04:00:00Z")
    now = datetime(2026, 10, 7, 6, 0)
    assert store.nocorr_windows(cfg, NIGHT, now=now) == [
        (datetime(2026, 10, 7, 4), now)]


# ---- rescore: un-reject into review --------------------------------------------

def _sub(**kw):
    rec = {"rig": "rc16", "target": "Heart", "filter": "Ha", "exp_s": 300.0,
           "hfr": 3.0, "ecc": 0.65, "stars": 200, "background": 900,
           "ccd_temp": 0.0, "guide_state": "guiding",
           "passed_qa": False, "drivers": ["ecc"], "reason": "ecc",
           "auto_verdict": "rejected"}
    rec.update(kw)
    return rec


def test_rescore_moves_shape_rejects_in_the_episode_to_review(tmp_path):
    cfg = _cfg(tmp_path, quality_eccentricity_max=0.6)
    _episode(cfg, "2026-10-07T04:00:00Z", "2026-10-07T05:00:00Z")
    subs = [_sub(file="a.fits", time="2026-10-07T04:10:00"),       # inside
            _sub(file="b.fits", time="2026-10-07T06:10:00"),       # outside
            _sub(file="c.fits", time="2026-10-07T04:20:00",        # inside,
                 drivers=["ecc", "stars"], stars=1),               # not shape
            _sub(file="d.fits", time="2026-10-07T04:30:00",        # human
                 manual_qa=True)]
    p = runs.runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    p.write_text("".join(json.dumps(s) + "\n" for s in subs), encoding="utf-8")
    res = runs.rescore_night(cfg, NIGHT, apply=True)
    assert res["counts"].get("unrejected_nocorr") == 1
    got = {r["file"]: r for r in runs._load_subs(cfg, NIGHT)}
    a = got["a.fits"]
    assert a["passed_qa"] and not a.get("reviewed")
    assert a["qa_flag"] == "unguided-in-name" and a["guide_nocorr"]
    assert not got["b.fits"]["passed_qa"]
    assert got["b.fits"].get("qa_flag") != "unguided-in-name"
    assert not got["c.fits"]["passed_qa"]
    assert not got["d.fits"]["passed_qa"]


def test_guard_summary_counts_nocorr_subs(tmp_path):
    from photonscript.scheduler.routers.phd2 import guard_summary
    cfg = _cfg(tmp_path)
    _episode(cfg, "2026-10-07T04:00:00Z", "2026-10-07T05:00:00Z")
    p = runs.runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    p.write_text(json.dumps(_sub(file="a.fits", qa_flag="unguided-in-name"))
                 + "\n" + json.dumps(_sub(file="b.fits")) + "\n",
                 encoding="utf-8")
    g = guard_summary(cfg, NIGHT)
    assert g["no_corrections"] == 1 and g["nocorr_subs"] == 1


def test_runs_page_tags_the_sub():
    html = (Path(runs.__file__).parent / "templates" / "runs.html").read_text(
        encoding="utf-8")
    assert "function nocorrTag(s)" in html
    assert html.count("roofTag(s) + nocorrTag(s)") == 2
    assert "gd.nocorr_subs" in html


def test_config_fields_on_the_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    keys = {f[0] for f in _CONFIG_FIELDS}
    assert {"qa_unguided_ecc_margin", "qa_unguided_hfr_factor",
            "qa_unguided_eccentricity_max", "qa_unguided_hfr_max"} <= keys
