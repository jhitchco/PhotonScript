"""PS-138: the TheSky audit reads the TPoint model / ProTrack / mount state
live from TheSky instead of passing them from the manual record, the new
"First slew vs model index terms" row (TPoint Recalibrate, not a rebuild),
"ProTrack OFF" on the Guiding tab and the unguided arm warning.

The 2026-10-05 night is the main case: the manual record said model on, 250
points, ProTrack on (all PASS), while TheSky's ProTrack tab was unticked and
greyed and TheSky's mount read not connected; the first slew was 1 deg 14'
off with IH -3484.63", ID -2250.28" in the model."""
import asyncio
from datetime import datetime

import pytest

from photonscript.scheduler import guiding_attention as ga
from photonscript.scheduler import thesky_audit as ta
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import thesky_client as tc
from tests.test_scheduler.test_ps104_thesky_audit import _violations

RECORD = {"model_date": "2026-10-04", "entered_at": "2026-10-05T12:04:00Z",
          "points": 250, "rms_arcsec": 15.77, "model_active": True, "protrack_on": True,
          "ih_arcsec": -3484.63, "id_arcsec": -2250.28}


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
        kw.setdefault("nina_logs_dir", str(tmp_path / "ninalogs"))
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    return PhotonScriptConfig(_env_file=None, **kw)


def _eval(observed, cfg=None, record=RECORD):
    cfg = cfg or _cfg()
    obs = {s: {} for s in ta.SOURCES}
    if record is not None:
        man, _src = ta.manual_observed(dict(record, entered_at=datetime.utcnow().strftime(
            "%Y-%m-%dT%H:%M:%SZ")), cfg)
        obs.update({"manual": man, "manual_record": dict(record)})
    obs.update(observed)
    res = ta.evaluate(ta.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}, res


# --------------------------------------------------------------------------
# 1. the read-only TPoint / ProTrack script
# --------------------------------------------------------------------------

def test_tpoint_flags_script_is_read_only_and_in_the_onsite_check():
    js = tc.READ_ONLY_JS["tpoint_flags"]
    assert js.isascii() and not _violations(js)
    assert "TPoint.ProTrackActive" in js and "sky6RASCOMTele.ProTrack" in js
    on = tc.onsite_script()
    for k, _expr in tc.READ_PAIRS["tpoint_flags"][0]:
        assert f"'tpoint_flags.{k} = '" in on


def test_parse_kv_drops_method_text_and_objects():
    d = tc.parse_kv("a=function ProTrack() { [native code] };b=[object Object];c=null;d=1")
    assert d == {"a": None, "b": None, "c": None, "d": "1"}


class _Fake(tc.TheSkyClient):
    def __init__(self, replies):
        super().__init__("x", 3040)
        self.replies = replies

    def run_script(self, js):
        for name, reply in self.replies.items():
            if js == tc.READ_ONLY_JS[name]:
                return reply
        return "?=?"


def test_collect_maps_the_live_tpoint_reads():
    o, src = ta.collect_thesky(_cfg(), _Fake({
        "ping": "ok",
        "mount_flags": "connected=1;parked=0;tracking=1;ra_h=1;dec_d=2;last_slew_error=0",
        "tpoint_flags": "apply_corrections=1;points=250;rms_arcsec=15.77;ih_arcsec=-3484.63;"
                        "id_arcsec=-2250.28;protrack_active=?ERR;protrack_active_tele=0;"
                        "protrack_adjustments=?ERR"}))
    assert src["ok"]
    assert o["tpoint_model_active"] is True and o["tpoint_points"] == 250
    assert o["tpoint_ih_arcsec"] == pytest.approx(-3484.63)
    assert o["protrack_on"] is False                 # the second candidate answered
    assert "protrack_adjustments" not in o
    # a build without any of the names: nothing invented
    o, _ = ta.collect_thesky(_cfg(), _Fake({"ping": "ok", "tpoint_flags":
                                            "apply_corrections=?ERR;points=?ERR"}))
    assert not any(k.startswith(("tpoint_", "protrack")) for k in o)


# --------------------------------------------------------------------------
# 2. ProTrack and the model rows: live first, never a pass from the record
# --------------------------------------------------------------------------

def test_the_2026_10_05_audit_no_longer_passes_from_the_record():
    # TheSky reachable, mount not connected in TheSky, no ProTrack property
    live = {"thesky-script": {"thesky_scripting": True, "mount_connected": False}}
    rows, _ = _eval(dict(live, armer_state="DISARMED"))
    for rid in ("tpoint_model_active", "tpoint_points", "protrack_on"):
        assert rows[rid]["status"] != "pass", rid
    p = rows["protrack_on"]
    assert p["status"] == "warn" and p["current"] == "OFF (greyed)"
    assert "not connected" in p["note"] and "manual (2026-10-05) says on" in p["note"]
    assert rows["tpoint_points"]["status"] == "info"
    assert rows["tpoint_points"]["current"] == "manual (2026-10-05): 250"
    rows, _ = _eval(dict(live, armer_state="ARMED"))
    assert rows["protrack_on"]["status"] == "fail"


def test_protrack_live_reads():
    base = {"mount_connected": True, "mount_tracking": True}
    rows, _ = _eval({"thesky-script": dict(base, protrack_on=False)})
    assert rows["protrack_on"]["status"] == "fail" and rows["protrack_on"]["current"] == "OFF"
    assert rows["protrack_on"]["protrack"] == "off"
    rows, _ = _eval({"thesky-script": dict(base, protrack_on=True, protrack_adjustments=False)})
    assert rows["protrack_on"]["status"] == "fail"
    assert "Enable tracking adjustments" in rows["protrack_on"]["note"]
    rows, _ = _eval({"thesky-script": dict(base, protrack_on=True, protrack_adjustments=True)})
    assert rows["protrack_on"]["status"] == "pass"
    rows, _ = _eval({"thesky-script": dict(base, protrack_on=True)})
    assert rows["protrack_on"]["status"] == "unknown"
    # unticked by day (parked): a warn, it cannot be ticked until tracking
    rows, _ = _eval({"thesky-script": {"mount_connected": True, "mount_tracking": False,
                                       "protrack_on": False}, "armer_state": "DISARMED"})
    assert rows["protrack_on"]["status"] == "warn"
    # connected and tracking but nothing readable: the record is a hint only
    rows, _ = _eval({"thesky-script": base})
    assert rows["protrack_on"]["status"] == "info"
    assert rows["protrack_on"]["current"].startswith("manual (")
    rows, _ = _eval({"thesky-script": base}, record=None)
    assert rows["protrack_on"]["status"] == "unknown"
    assert "verify by eye" in rows["protrack_on"]["note"]
    # TheSky not reachable at all: never a pass
    rows, _ = _eval({})
    assert rows["protrack_on"]["status"] == "info"
    assert rows["tpoint_model_active"]["status"] == "info"


def test_model_rows_judge_the_live_value():
    rows, _ = _eval({"thesky-script": {"tpoint_model_active": False, "tpoint_points": 40,
                                       "tpoint_rms_arcsec": 12.0}})
    assert rows["tpoint_model_active"]["status"] == "fail"
    assert rows["tpoint_model_active"]["source"] == "thesky-script"
    assert rows["tpoint_points"]["status"] == "warn"            # 40 < 50, read live
    assert rows["tpoint_rms_arcsec"]["status"] == "pass"
    rows, _ = _eval({"thesky-script": {"tpoint_model_active": True}})
    assert rows["tpoint_model_active"]["status"] == "pass"


def test_protrack_status_helper():
    rows, _ = _eval({"thesky-script": {"mount_connected": False}, "armer_state": "ARMED"})
    p = ta.protrack_status({"rows": list(rows.values())})
    assert p["state"] == "off" and p["status"] == "fail" and "Activate ProTrack" in p["fix"]
    assert ta.protrack_status(None)["state"] == "unknown"


# --------------------------------------------------------------------------
# 3. first slew vs the model's index terms
# --------------------------------------------------------------------------

def _pointing(sep, east, north, side_med=None):
    st = {"n": 3, "median_arcmin": sep, "median_east_arcmin": east,
          "median_north_arcmin": north}
    return {"pointing-log": {"per_night": [dict(st, night="2026-10-05")],
                             "overall": st, "runs": 3, "nights": 14,
                             "by_side": {"E": {"n": 3, "median_arcmin": side_med or sep}}}}


def test_index_shift_recommends_recalibrate_not_rebuild():
    # 74' off, about 62' east / 40' north: the shape of IH -58' / ID -37.5'
    rows, res = _eval(_pointing(74.0, -62.0, -40.0))
    r = rows["first_slew_index_terms"]
    assert r["status"] == "warn" and "MATCH" in r["current"]
    assert "Recalibrate" in r["note"] and "not a rebuild" in r["note"]
    assert "manual (2026-10-05)" in r["current"]
    reb = rows["tpoint_rebuild"]
    assert reb["current"] == "watch"                       # not REBUILD
    assert any("Recalibrate" in x for x in res["rebuild"]["reasons"])
    assert rows["first_slew_error"]["status"] == "fail"    # still shown as large


def test_index_shift_no_match_small_and_unknown():
    rows, _ = _eval(_pointing(20.0, 12.0, 16.0))         # 20' vs 69': no
    assert rows["first_slew_index_terms"]["status"] == "info"
    assert rows["tpoint_rebuild"]["current"] == "REBUILD"
    rows, _ = _eval(_pointing(70.0, 5.0, 69.0))          # size right, direction wrong
    assert rows["first_slew_index_terms"]["status"] == "info"
    assert "direction differs" in rows["first_slew_index_terms"]["note"]
    rows, _ = _eval(_pointing(1.2, 0.5, 1.0))
    assert rows["first_slew_index_terms"]["status"] == "pass"
    rows, _ = _eval(_pointing(74.0, -62.0, -40.0),
                    record={k: v for k, v in RECORD.items() if k not in ("ih_arcsec", "id_arcsec")})
    assert rows["first_slew_index_terms"]["status"] == "unknown"
    assert "IH / ID" in rows["first_slew_index_terms"]["note"]
    # live IH / ID from TheSky win over the record
    rows, _ = _eval(dict(_pointing(10.0, 6.0, 8.0),
                         **{"thesky-script": {"tpoint_ih_arcsec": 360.0, "tpoint_id_arcsec": 480.0}}))
    r = rows["first_slew_index_terms"]
    assert r["status"] == "warn" and "(TheSky)" in r["current"]


def test_manual_record_takes_ih_id():
    rec, bad = ta.validate_manual({"ih_arcsec": "-3484.63", "id_arcsec": -2250.28})
    assert not bad and rec == {"ih_arcsec": -3484.63, "id_arcsec": -2250.28}
    _rec, bad = ta.validate_manual({"ih_arcsec": 99999})
    assert bad


# --------------------------------------------------------------------------
# 4. Guiding tab and the unguided arm warning
# --------------------------------------------------------------------------

def _off_audit():
    rows, _ = _eval({"thesky-script": {"thesky_scripting": True, "mount_connected": True,
                                       "mount_tracking": True, "protrack_on": False}})
    return {"t_utc": "2026-10-06T04:00:00Z", "reason": "arm", "rows": list(rows.values()),
            "imagelink": None, "manual": RECORD, "pointing": {"nights": 14}}


def test_guiding_tab_shows_protrack_off_as_a_fail(tmp_path):
    s = ga.build(_cfg(tmp_path), datetime(2026, 10, 6, 4, 10), phd2={"rows": []},
                 thesky=_off_audit())
    it = next(i for i in s["items"] if i["id"] == "protrack_on")
    assert it["severity"] == "fail" and it["setting"] == "ProTrack OFF"
    assert it["fix"] == ta.PROTRACK_FIX and it["priority"] == 0
    # unknown "verify by eye" rows get their own not-checked group
    rows, _ = _eval({"thesky-script": {"mount_connected": True, "mount_tracking": True}},
                    record=None)
    nc = ga.not_checked(None, {"rows": list(rows.values())})
    assert any(g["key"] == "by_eye" and "ProTrack on" in g["rows"] for g in nc)


def _armer(monkeypatch, audit, guided=False):
    from photonscript.scheduler import armer as armer_mod
    from photonscript.scheduler.armer import Armer
    a = Armer(PhotonScriptConfig(_env_file=None, guided_default=guided))
    sent = []

    async def _notify(cfg, msg, **kw):
        sent.append((msg, kw))

    async def _at_arm(cfg, armer_state=None):
        return audit
    monkeypatch.setattr(armer_mod, "notify", _notify)
    monkeypatch.setattr(ta, "at_arm", _at_arm)
    return a, sent


def test_unguided_arm_warns_when_protrack_is_off(monkeypatch):
    a, sent = _armer(monkeypatch, _off_audit())
    asyncio.run(a._thesky_audit_at_arm())
    assert len(sent) == 1
    msg, kw = sent[0]
    assert "UNGUIDED" in msg and "ProTrack is OFF" in msg and "Activate ProTrack" in msg
    assert kw.get("title") == "PhotonScript ProTrack"


def test_no_warning_guided_or_when_protrack_is_on(monkeypatch):
    a, sent = _armer(monkeypatch, _off_audit(), guided=True)
    asyncio.run(a._thesky_audit_at_arm())
    assert sent == []
    rows, _ = _eval({"thesky-script": {"mount_connected": True, "mount_tracking": True,
                                       "protrack_on": True, "protrack_adjustments": True}})
    a, sent = _armer(monkeypatch, {"rows": list(rows.values())})
    asyncio.run(a._thesky_audit_at_arm())
    assert sent == []
    # unguided and not confirmed: warned as "could not be confirmed"
    rows, _ = _eval({"thesky-script": {"mount_connected": True, "mount_tracking": True}})
    a, sent = _armer(monkeypatch, {"rows": list(rows.values())})
    asyncio.run(a._thesky_audit_at_arm())
    assert len(sent) == 1 and "could not be confirmed" in sent[0][0]
