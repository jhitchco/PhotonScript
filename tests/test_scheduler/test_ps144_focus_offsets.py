"""PS-144: narrowband focus offsets.

1. focus_filter_offsets default (measured Ha +123) and the "+120" form.
2. System page fields (PS_FOCUS_FILTER_OFFSETS, PS_FOCUS_CALIBRATION_TONIGHT).
3. focus_calibration_tonight: the armer prepends a focus-offset calibration
   to tonight's sequence (lint clean), and turns the key off after a
   successful dispatch (.env, event, Pushover).
4. focus_model.offset_check(): configured offset vs same-night L / filter
   AF pairs, CFZ alert, once-per-night push, Guiding attention item.
"""
import json
from datetime import datetime

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import focus_model as fm
from photonscript.scheduler.armer import Armer
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    return PhotonScriptConfig(_env_file=None, **kw)


# ---- 1. config -----------------------------------------------------------------

def test_default_offsets_are_the_measured_plus_120(monkeypatch):
    monkeypatch.delenv("PS_FOCUS_FILTER_OFFSETS", raising=False)
    cfg = _cfg()
    assert cfg.focus_filter_offsets == "Ha:120,OIII:120,SII:120"
    assert cfg.focus_offset_map() == {"Ha": 120, "OIII": 120, "SII": 120}
    assert cfg.focus_calibration_tonight is False


def test_offsets_accept_a_leading_plus_and_negatives():
    cfg = _cfg(focus_filter_offsets="Ha:+123, OIII: +118 ,SII:-5,R:+x")
    assert cfg.focus_offset_map() == {"Ha": 123, "OIII": 118, "SII": -5}


def test_env_value_parses(monkeypatch):
    monkeypatch.setenv("PS_FOCUS_FILTER_OFFSETS", "Ha:+120,OIII:+120")
    monkeypatch.setenv("PS_FOCUS_CALIBRATION_TONIGHT", "true")
    cfg = PhotonScriptConfig(_env_file=None)
    assert cfg.focus_offset_map() == {"Ha": 120, "OIII": 120}
    assert cfg.focus_calibration_tonight is True


# ---- 2. System page fields -----------------------------------------------------

def test_system_page_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    f = by_env["PS_FOCUS_FILTER_OFFSETS"]
    assert f[0] == "focus_filter_offsets" and f[4] == "str"
    assert "Focus offsets from L per filter" in f[2]
    c = by_env["PS_FOCUS_CALIBRATION_TONIGHT"]
    assert c[0] == "focus_calibration_tonight" and c[4] == "bool"
    assert "focus-offset calibration at the start of tonight" in c[2]


async def test_system_page_save_applies_live(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.shared import envfile
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    env = tmp_path / ".env"
    monkeypatch.setattr(envfile, "env_path", lambda root=None: env)

    class _Req:
        async def json(self):
            return {"PS_FOCUS_FILTER_OFFSETS": "Ha:+123,OIII:120,SII:120",
                    "PS_FOCUS_CALIBRATION_TONIGHT": "true"}
    res = await app.api_update_config(_Req())
    assert res["updated"] == 2 and res["restart_recommended"] is False
    assert cfg.focus_offset_map()["Ha"] == 123
    assert cfg.focus_calibration_tonight is True
    text = env.read_text(encoding="utf-8")
    assert "PS_FOCUS_FILTER_OFFSETS=Ha:+123,OIII:120,SII:120" in text


# ---- 3. dusk calibration in the armed night ------------------------------------

def _tgt(name="Heart Nebula", guided=False):
    return NinaSequenceTarget(
        name=name, ra_hours=2.55, dec_degrees=61.5, start_guiding=guided,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])


def _armer(tmp_path, monkeypatch, **cfg):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import target_planner

    class _Store:
        projects: dict = {}
    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: [_tgt()])
    monkeypatch.chdir(tmp_path)
    a = Armer(_cfg(tmp_path, **cfg))
    a.plan = {"night_of": "2026-10-06", "dusk_utc": "2026-10-07T01:30:00Z",
              "dawn_utc": "2026-10-07T11:30:00Z"}
    monkeypatch.setattr(a, "_calibration_slot", lambda targets, now: None)
    return a


def _target_containers(seq: dict) -> list:
    """Names of the DeepSkyObject containers in document order."""
    out = []

    def walk(d):
        if isinstance(d, dict):
            if "DeepSkyObjectContainer" in str(d.get("$type", "")):
                out.append(d.get("Name"))
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)
    walk(seq)
    return out


def _af_filters(seq: dict) -> list:
    """SwitchFilter names inside the calibration AF series."""
    found = []

    def walk(d):
        if isinstance(d, dict):
            if str(d.get("Name", "")).endswith("focus calibration AFs"):
                for it in d["Items"]["$values"]:
                    if "SwitchFilter" in it.get("$type", ""):
                        found.append(it["Filter"]["_name"])
                return
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)
    walk(seq)
    return found


@pytest.mark.parametrize("guided", [False, True])
def test_dispatch_prepends_calibration_and_lints(tmp_path, monkeypatch, guided):
    a = _armer(tmp_path, monkeypatch, focus_calibration_tonight=True,
               guided_default=guided, noon_arm_guided=False)
    assert a._dispatch() is True, a.detail            # lint passed
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    names = _target_containers(seq)
    assert names[0].startswith("Focus calibration ") and names[1] == "Heart Nebula"
    assert a._focus_cal_field["name"] in names[0]
    # L acquisition AF, then Ha, L, OIII, L, SII, L
    assert _af_filters(seq) == ["H", "L", "O", "L", "S", "L"]
    from photonscript.scheduler.sequence_lint import lint
    res = lint(seq, guided=a._use_guiding())
    assert res.ok and any(f.rule == "focus-calibration" for f in res.findings)
    # the plan snapshot is tonight's real targets only
    snap = json.loads((tmp_path / "data" / "runs" / "2026-10-06_plan.json")
                      .read_text())
    assert [t["name"] for t in snap["targets"]] == ["Heart Nebula"]
    assert a.last_dispatch_targets == ["Heart Nebula"]


def test_dispatch_without_the_flag_is_unchanged(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch)
    assert a._dispatch() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert _target_containers(seq) == ["Heart Nebula"]
    assert a._focus_cal_field is None


def test_field_is_40_to_70_deg_up_at_dusk(tmp_path, monkeypatch):
    from photonscript.scheduler.tracking_test import altitude
    a = _armer(tmp_path, monkeypatch)
    t = a._focus_calibration_target(datetime(2026, 10, 6, 20, 0))
    f = a._focus_cal_field
    assert t.focus_calibration and t.focus_calibration_rounds == 1
    assert t.focus_calibration_filters == list(armer_mod.FOCUS_CAL_FILTERS)
    assert 40.0 <= f["alt_deg"] <= 70.0
    dusk = datetime(2026, 10, 7, 1, 30)
    assert f["alt_deg"] == pytest.approx(altitude(
        f["ra_hours"], f["dec_degrees"], dusk, a.config.observatory_lat,
        a.config.observatory_lon), abs=0.1)
    # tonight NGC 7789 is 37 deg up at astro dusk: the next listed field
    assert f["name"] == "M52"
    # later in the autumn NGC 7789 (first choice) is up at dusk
    a.plan["dusk_utc"] = "2026-10-07T03:00:00Z"
    a._focus_calibration_target(datetime(2026, 10, 6, 20, 0))
    assert a._focus_cal_field["name"] == "NGC 7789"
    # spring dusk: NGC 7789 is low, another listed field is picked
    a.plan["dusk_utc"] = "2026-04-08T02:30:00Z"
    a._focus_calibration_target(datetime(2026, 4, 7, 20, 0))
    assert a._focus_cal_field["name"] != "NGC 7789"
    assert 40.0 <= a._focus_cal_field["alt_deg"] <= 70.0


async def test_successful_dispatch_resets_the_key(tmp_path, monkeypatch):
    from photonscript.shared import envfile
    a = _armer(tmp_path, monkeypatch, focus_calibration_tonight=True)
    env = tmp_path / ".env"
    env.write_text("# scope\nPS_FOCUS_CALIBRATION_TONIGHT=true\n", encoding="utf-8")
    monkeypatch.setattr(envfile, "env_path", lambda root=None: env)
    calls, notes = [], []

    async def _nina(key, **kw):
        calls.append(key)
        return {"Success": True}

    async def _notify(cfg, msg, **kw):
        notes.append((kw.get("title"), msg))
        return True
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(armer_mod, "notify", _notify)

    async def _none():
        return None
    monkeypatch.setattr(a, "_send_block_alerts", _none)
    assert await a._dispatch_and_start(companion=False) is True
    assert calls == ["sequence_stop", "sequence_load", "sequence_start"]
    assert a.config.focus_calibration_tonight is False
    assert envfile.read_env(env)["PS_FOCUS_CALIBRATION_TONIGHT"] == "false"
    assert env.read_text(encoding="utf-8").startswith("# scope\n")
    assert [t for t, _ in notes] == ["PhotonScript focus calibration"]
    assert "M52" in notes[0][1] and "now off" in notes[0][1]
    ev = [json.loads(x) for x in (tmp_path / "data" / "runs").glob(
        "*_events.jsonl").__next__().read_text().splitlines()]
    assert ev[-1]["kind"] == "focus_calibration" and ev[-1]["value"] == "dispatched"
    # a re-dispatch (pause / recalibration) no longer carries it
    assert a._dispatch() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert _target_containers(seq) == ["Heart Nebula"]


async def test_failed_start_keeps_the_key(tmp_path, monkeypatch):
    from photonscript.shared import envfile
    a = _armer(tmp_path, monkeypatch, focus_calibration_tonight=True)
    env = tmp_path / ".env"
    monkeypatch.setattr(envfile, "env_path", lambda root=None: env)

    async def _nina(key, **kw):
        return None if key == "sequence_start" else {"Success": True}

    async def _notify(*a_, **k):
        return True
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    assert await a._dispatch_and_start(companion=False, fail_state=None) is False
    assert a.config.focus_calibration_tonight is True
    assert not env.exists()


# ---- 4. configured vs measured offsets ----------------------------------------

def _pt(f, pos, time, temp=10.0, step=110):
    return {"filter": f, "position": pos, "temp": temp, "time": time,
            "step": step, "rig": "rc16"}


CAL_NIGHT = [  # a dusk calibration, 2026-10-06 local
    _pt("L", 5700, "2026-10-06T19:40:00-06:00"),
    _pt("Ha", 5823, "2026-10-06T19:44:00-06:00"),
    _pt("L", 5700, "2026-10-06T19:48:00-06:00"),
    _pt("OIII", 5810, "2026-10-06T19:52:00-06:00"),
    _pt("L", 5700, "2026-10-06T19:56:00-06:00"),
    _pt("SII", 5830, "2026-10-06T20:00:00-06:00"),
    _pt("L", 5700, "2026-10-06T20:04:00-06:00"),
]


def test_pair_offsets_same_night_nearest_l():
    p = fm.pair_offsets(CAL_NIGHT, "L")
    assert p["Ha"]["steps"] == 123 and p["Ha"]["n"] == 1
    assert p["OIII"]["steps"] == 110          # bracketed by two L AFs
    assert p["SII"]["steps"] == 130
    assert p["Ha"]["night"] == "2026-10-06"


def test_pair_offsets_temperature_correction_gap_and_latest_night():
    pts = [_pt("L", 5700, "2026-10-01T21:00:00", temp=12.0),
           _pt("Ha", 5800, "2026-10-01T21:10:00", temp=11.0),   # 1 C colder
           _pt("Ha", 5000, "2026-10-01T23:30:00", temp=11.0)]   # > 90 min: no pair
    p = fm.pair_offsets(pts, "L", slope=-20.0)
    # raw +100; colder by 1 C at -20 steps/C moves focus +20: corrected +80
    assert p["Ha"]["steps"] == 80 and p["Ha"]["n"] == 1
    later = pts + [_pt("L", 5600, "2026-10-03T21:00:00", temp=12.0),
                   _pt("Ha", 5720, "2026-10-03T21:05:00", temp=12.0)]
    q = fm.pair_offsets(later, "L", slope=-20.0)
    assert q["Ha"]["steps"] == 120 and q["Ha"]["night"] == "2026-10-03"
    assert fm.pair_offsets([_pt("Ha", 5800, "2026-10-01T21:00:00")], "L") == {}


def test_offset_check_alerts_beyond_one_cfz():
    old = fm.offset_check(_cfg(focus_filter_offsets="Ha:-187,OIII:-187,SII:-187",
                               focus_cfz_steps=110), CAL_NIGHT)
    assert old["cfz_steps"] == 110 and old["alert"] is True
    assert old["alerts"] == ["Ha", "OIII", "SII"]
    ha = old["filters"]["Ha"]
    assert ha["configured"] == -187 and ha["measured"]["steps"] == 123
    assert ha["diff"] == -310 and old["night"] == "2026-10-06"
    assert "configured -187, measured +123" in fm.offset_alert_text(old)

    new = fm.offset_check(_cfg(focus_cfz_steps=110), CAL_NIGHT)   # +120 default
    assert new["alert"] is False
    assert [new["filters"][f]["diff"] for f in ("Ha", "OIII", "SII")] == [-3, 10, -10]
    assert new["filters"]["Ha"]["measured"]["deltas"] == [123]
    # no CFZ configured: the AF step size (110) stands in
    assert fm.offset_check(_cfg(focus_filter_offsets="Ha:-187"),
                           CAL_NIGHT)["cfz_source"] == "median AF step size"
    # exactly one CFZ off is still inside; one step more alerts
    edge = fm.offset_check(_cfg(focus_filter_offsets="Ha:13,OIII:-1,SII:131",
                                focus_cfz_steps=110), CAL_NIGHT)
    assert edge["filters"]["Ha"]["diff"] == -110      # one CFZ: inside
    assert edge["filters"]["OIII"]["diff"] == -111    # one step more: alert
    assert edge["alerts"] == ["OIII"]


def test_offset_check_unlisted_filter_is_zero_and_no_pairs_no_alert():
    pts = [_pt("L", 5700, "2026-10-06T21:00:00"),
           _pt("R", 5676, "2026-10-06T21:05:00")]
    chk = fm.offset_check(_cfg(focus_cfz_steps=110), pts)
    r = chk["filters"]["R"]
    assert r["listed"] is False and r["configured"] == 0 and r["diff"] == 24
    assert chk["filters"]["Ha"]["measured"] is None and chk["alert"] is False


async def test_offset_alert_pushes_once_per_night(tmp_path):
    cfg = _cfg(tmp_path, focus_filter_offsets="Ha:-187", focus_cfz_steps=110)
    sent = []

    async def _notify(c, msg, **kw):
        sent.append(msg)
        return True
    chk = fm.offset_check(cfg, CAL_NIGHT)
    assert await fm.offset_alert(cfg, chk, notify=_notify) is True
    assert await fm.offset_alert(cfg, chk, notify=_notify) is False
    assert len(sent) == 1 and "PS_FOCUS_FILTER_OFFSETS" in sent[0]
    ok = fm.offset_check(_cfg(tmp_path, focus_cfz_steps=110), CAL_NIGHT)
    assert await fm.offset_alert(cfg, ok, notify=_notify) is False


def test_summary_and_attention_show_the_offset_check(tmp_path, monkeypatch):
    from photonscript.scheduler import guiding_attention as ga
    cfg = _cfg(tmp_path, focus_filter_offsets="Ha:-187,OIII:120,SII:120",
               focus_cfz_steps=110)
    monkeypatch.setattr(fm, "_rc16_points", lambda config: list(CAL_NIGHT))
    s = fm.summary(cfg)
    assert s["offset_check"]["alerts"] == ["Ha"]
    items = ga.safe_build(cfg)["items"]
    fo = [i for i in items if i.get("id") == "focus_offset_Ha"]
    assert len(fo) == 1 and fo[0]["severity"] == "warn"
    assert "-187" in fo[0]["current"] and "+123" in fo[0]["desired"]
