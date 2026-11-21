"""2026-11-20: queue an arm recipe for a future night; the noon auto-arm
(or any arm without its own recipe) of that night takes it, once."""
import json
from datetime import datetime

import pytest

from photonscript.scheduler import recipe_queue as rq
from photonscript.scheduler import sideload as sd
from photonscript.shared.config import PhotonScriptConfig

TT = sd.RECIPE_TT_THEN_TONIGHT
TPM = sd.RECIPE_TPOINT_MAPPING
NOW = datetime(2026, 11, 21, 3, 0)


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data")


def test_queue_validates_and_lists(tmp_path):
    c = _cfg(tmp_path)
    e, err = rq.queue(c, "2026-11-22", TPM, {"points": 100, "add": "off"}, now=NOW)
    assert err is None and e["recipe"] == TPM and e["opts"] == {"points": 100, "add": "off"}
    assert e["label"] == "TPoint mapping (100)"
    assert [x["night"] for x in rq.listing(c)] == ["2026-11-22"]
    assert rq.queue(c, "tomorrow", TPM, None, now=NOW)[1].startswith("night must be")
    assert "unknown recipe" in rq.queue(c, "2026-11-22", "nope", None, now=NOW)[1]
    assert "not allowed" in rq.queue(c, "2026-11-22", TPM, {"zz": 1}, now=NOW)[1]
    assert "is over" in rq.queue(c, "2026-11-01", TPM, None, now=NOW)[1]
    assert "recipe required" in rq.queue(c, "2026-11-23", "", None, now=NOW)[1]


def test_tracking_test_options_survive_the_queue(tmp_path):
    c = _cfg(tmp_path)
    opts = {"exposures": "30,60,120,300", "filters": "L", "repeats": 3,
            "fields": [{"label": "East", "ha": -2.5, "dec": 60},
                       {"label": "West", "ha": 2.5, "dec": 45}]}
    e, err = rq.queue(c, "2026-11-21", TT, opts, now=NOW)
    assert err is None
    rec = rq.take(c, "2026-11-21")
    assert rec["id"] == TT and rec["opts"]["exposures"] == [30.0, 60.0, 120.0, 300.0]
    assert len(rec["opts"]["fields"]) == 2
    # taken once: a later arm of the same night gets nothing
    assert rq.take(c, "2026-11-21") is None and rq.listing(c) == []


def test_take_other_night_and_remove(tmp_path):
    c = _cfg(tmp_path)
    rq.queue(c, "2026-11-22", TPM, None, now=NOW)
    assert rq.take(c, "2026-11-21") is None
    assert rq.take(c, None) is None
    assert rq.remove(c, "2026-11-22") is True and rq.remove(c, "2026-11-22") is False
    assert rq.listing(c) == []


def test_past_nights_are_pruned_and_bad_file_is_empty(tmp_path):
    c = _cfg(tmp_path)
    p = tmp_path / "data" / "recipe_queue.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"nights": {"2026-10-01": {"recipe": TPM}}}))
    rq.queue(c, "2026-11-22", TPM, None, now=NOW)
    assert [x["night"] for x in rq.listing(c)] == ["2026-11-22"]
    p.write_text("{not json")
    assert rq.listing(c) == [] and rq.take(c, "2026-11-22") is None


async def test_an_arm_without_a_recipe_takes_the_queued_one(tmp_path, monkeypatch):
    from photonscript.scheduler import armer as armer_mod
    from photonscript.scheduler import night_plan
    c = _cfg(tmp_path)
    c.connect_all_on_arm = False
    c.cooler_off_until_precool = False
    c.phd2_audit_enabled = False
    c.thesky_audit_enabled = False
    c.app_lifecycle_alert = False
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(night_plan, "build_night_plan", lambda cfg: {
        "night_of": "2026-11-22", "preconfig_utc": "2026-11-23T00:30:00Z",
        "dusk_utc": "2026-11-23T01:30:00Z", "dawn_utc": "2099-01-01T11:30:00Z",
        "targets": ["Pacman Nebula"], "dark_hours": 10.9})
    a = armer_mod.Armer(c)

    async def _quiet(*a_, **k):
        return None
    monkeypatch.setattr(a, "_check_guider_at_arm", _quiet)
    monkeypatch.setattr(a, "_run", _quiet)
    monkeypatch.setattr(a, "_start_nina2_mount_check", lambda reason: None)
    rq.queue(c, "2026-11-22", TPM, {"points": 100, "add": "off"},
             now=datetime(2026, 11, 21, 3, 0))
    out = await a.arm(guiding="unguided", armed_by="auto-noon")
    assert out["state"] == "ARMED"
    assert out["recipe"]["id"] == TPM and out["recipe"]["opts"] == {"points": 100, "add": "off"}
    assert "with TPoint mapping (100) first" in a.detail
    assert rq.listing(c) == []
    # an explicit recipe wins over the queue, which stays untouched
    rq.queue(c, "2026-11-22", TT, None, now=datetime(2026, 11, 21, 3, 0))
    rec, _ = sd.arm_recipe(TPM, {"points": 10})
    out = await a.arm(guiding="unguided", recipe=rec)
    assert out["recipe"]["opts"] == {"points": 10}
    assert [x["recipe"] for x in rq.listing(c)] == [TT]
