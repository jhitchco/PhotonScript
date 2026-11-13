"""Learned unguided exposure limits (scheduler/unguided_limits.py).

Evidence 2026-10-13 (unguided, TPoint + ProTrack, RC16 at 0.24"/px): on
M31 (Dec +41, east, alt about 46 deg) 300 s L / R trailed (ecc 0.79 /
0.71) while 180 s L R G B were round (ecc 0.43 to 0.55). 2026-10-12 (Heart,
Dec +61) trailed at 180 s and 300 s while PHD2 was guiding on a FAIL
calibration: none of it may count against the mount. The fixtures are the
two nights' subs and plans from a read-only GET /api/runs/<night>,
trimmed.

  1. evidence: the guider-conflict night is excluded, M31 counts, every
     exclusion reason (synthetic);
  2. the table: measured limit, the drift model, a shorter failing length,
     confidence / probe, the lookup fallbacks;
  3. the planner: advise changes nothing, apply cuts with the same
     integration and darks matched, overrides win, the probe, the Piggy;
  4. calibration: 180 s and 240 s darks join the night quota and the owed
     view;
  5. refresh / store, the dawn step, the morning line, CLI, API, page,
     config fields.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import unguided_limits as ul
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

FIX = Path(__file__).resolve().parent / "fixtures" / "unguided_limits"
N12, N13 = "2026-10-12", "2026-10-13"
M31_RA_H, M31_DEC = 0.7123, 41.27
HEART_RA_H, HEART_DEC = 2.555, 61.45


def _cfg(tmp_path, **kw):
    kw.setdefault("quality_eccentricity_max", 0.6)     # the scope PC's env
    kw.setdefault("piggyback_exposure_s", 450.0)
    kw.setdefault("unguided_limit_since", "2026-10-09")  # TPoint model night
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _night(name):
    d = json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))
    return {"subs": d["subs"], "plan": d["plan"]}


def _real(cfg, nights=(N12, N13)):
    return ul.refresh(cfg, N13, save=False, nights_data={n: _night(n) for n in nights})


def _sub(t0, exp, ecc, *, rig="rc16", target="M31", hfr=8.0, stars=400,
         guide_state="stopped", focpos=5000, dec=41.27, ra_deg=10.68, alt=48.0,
         **kw):
    st = t0
    en = st + timedelta(seconds=exp)
    r = {"rig": rig, "file": f"LIGHT/{st:%H-%M-%S}_{exp:g}.fits", "target": target,
         "filter": "L", "exp_s": float(exp), "ecc": ecc, "hfr": hfr,
         "fwhm_arcsec": 3.8, "stars": stars, "focpos": focpos,
         "guide_state": guide_state,
         "start_utc": st.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "end_utc": en.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "pointing": {"mount_ra": ra_deg, "mount_dec": dec, "alt": alt}}
    r.update(kw)
    return r


T0 = datetime(2026, 10, 14, 3, 0, 0)
PLAN_U = {"targets": [{"name": "M31", "guided": False}]}


# --------------------------------------------------------------------------
# 1. evidence
# --------------------------------------------------------------------------

def test_1013_m31_counts_and_1012_guider_conflict_is_excluded(tmp_path):
    cfg = _cfg(tmp_path)
    rows13, ex13 = ul.evidence(cfg, "rc16", N13, **_night(N13))
    assert len(rows13) == 7 and not ex13
    by = {}
    for r in rows13:
        by.setdefault(r["exp_s"], []).append(r["pass"])
    assert by[300.0] == [False, False] and all(by[180.0]) and len(by[180.0]) == 5
    assert all(r["ha_h"] < 0 for r in rows13)                     # east
    assert all(40 < r["dec"] < 42 for r in rows13)
    rows12, ex12 = ul.evidence(cfg, "rc16", N12, **_night(N12))
    assert rows12 == []                       # PHD2 interfered all night
    assert ex12["guider_conflict"] >= 90


def test_exclusion_reasons(tmp_path):
    cfg = _cfg(tmp_path)
    subs = [
        _sub(T0, 180, 0.40),                                          # kept
        _sub(T0 + timedelta(minutes=4), 180, 0.42, focpos=0),         # focuser
        _sub(T0 + timedelta(minutes=8), 180, 0.42, stars=20),         # clouds
        _sub(T0 + timedelta(minutes=12), 180, 0.30, hfr=14.0),        # soft, round
        _sub(T0 + timedelta(minutes=16), 180, 0.75, hfr=14.0),        # soft, trailed: kept
        _sub(T0 + timedelta(minutes=20), 180, None),                  # unmeasured
        _sub(T0 + timedelta(minutes=24), 5, 0.4, target="TPoint point 01/45 alt 38.1 az 72.6 (east)"),
        _sub(T0 + timedelta(minutes=28), 180, 0.4, target="Tracking test M15",
             dec=12.2, ra_deg=322.5),                                 # kept
    ]
    rows, ex = ul.evidence(cfg, "rc16", N13, subs, plan=PLAN_U)
    assert len(rows) == 3
    assert ex == {"focuser": 1, "clouds": 1, "soft_round": 1, "unmeasured": 1, "test": 1}


def test_guided_unknown_and_phd2_neighbours(tmp_path):
    cfg = _cfg(tmp_path)
    guided = {"targets": [{"name": "M31", "guided": True, "unguided_filters": ["Ha"]}]}
    ha = _sub(T0, 300, 0.4, filter="Ha")
    rows, ex = ul.evidence(cfg, "rc16", N13, [_sub(T0, 300, 0.4), ha], plan=guided)
    assert [r["filter"] for r in rows] == ["Ha"] and ex == {"guided": 1}
    # no plan, no PHD2 state: unknown; no plan, PHD2 stopped: unguided
    rows, ex = ul.evidence(cfg, "rc16", N13, [_sub(T0, 300, 0.4, guide_state=None)])
    assert rows == [] and ex == {"unknown": 1}
    rows, _ = ul.evidence(cfg, "rc16", N13, [_sub(T0, 300, 0.4)])
    assert len(rows) == 1
    # a stopped sub right after a guiding one (either rig) is suspect
    subs = [_sub(T0, 180, 0.4, guide_state="lost_star"),
            _sub(T0 + timedelta(minutes=4), 180, 0.5),
            _sub(T0 + timedelta(minutes=60), 180, 0.5)]
    rows, ex = ul.evidence(cfg, "rc16", N13, subs, plan=PLAN_U)
    assert len(rows) == 1 and ex == {"guider_conflict": 2}


def test_sub_doctor_guider_conflict_events_exclude_their_span(tmp_path):
    cfg = _cfg(tmp_path)
    subs = [_sub(T0 + timedelta(minutes=10 * i), 180, 0.45) for i in range(6)]
    ev = [{"kind": "sub_doctor", "rig": "rc16", "cause": "guider_conflict",
           "t": (T0 + timedelta(minutes=25)).isoformat() + "Z"},
          {"kind": "sub_doctor", "rig": "rc16", "cause": "none", "value": "none",
           "t": (T0 + timedelta(minutes=41)).isoformat() + "Z"}]
    rows, ex = ul.evidence(cfg, "rc16", N13, subs, plan=PLAN_U, events=ev)
    # span 23:55 lead .. 00:41: subs at 0, 10, 20, 30, 40 min overlap it
    assert ex == {"guider_conflict": 5} and len(rows) == 1


def test_position_falls_back_to_the_target_coordinates(tmp_path, monkeypatch):
    from photonscript.shared import pointing
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(pointing, "target_coords",
                        lambda c, n: ("M31", 10.68, 41.27) if n == "M31" else None)
    s = _sub(T0, 180, 0.4)
    s.pop("pointing")
    rows, _ = ul.evidence(cfg, "rc16", N13, [s, {**s, "target": "Nowhere"}],
                          plan={"targets": [{"name": "M31", "guided": False},
                                            {"name": "Nowhere", "guided": False}]})
    assert len(rows) == 1 and rows[0]["dec"] == 41.27


# --------------------------------------------------------------------------
# 2. the table
# --------------------------------------------------------------------------

def test_real_table_m31_region_180_measured_probe_240(tmp_path):
    cfg = _cfg(tmp_path)
    doc = _real(cfg)
    rc = doc["rigs"]["rc16"]
    assert rc["n_used"] == 7 and rc["excluded"]["guider_conflict"] >= 90
    assert len(rc["regions"]) == 1
    reg = rc["regions"][0]
    assert reg["dec_band"] == [30, 50] and reg["ha_band"][1] <= -1.0   # east
    assert reg["lengths"]["180"] == {"exp_s": 180.0, "n": 5, "pass": 5, "rate": 1.0,
                                     "ecc_med": 0.486}
    assert reg["lengths"]["300"]["pass"] == 0
    assert reg["limit_s"] == 180.0 and reg["source"] == "measured"
    assert reg["confidence"] == "medium" and reg["uncertain"]
    assert reg["probe_s"] == 240.0
    assert reg["verdicts"]["300"]["ok"] is False
    assert 0.5 < reg["drift_arcsec_min"] < 2.0
    # the Piggy-600: 450 s rode along and stayed round at its own gate
    pg = doc["rigs"]["piggyback"]
    assert pg["overall"]["limit_s"] == 450.0


def test_gate_0_7_lets_the_drift_model_allow_240(tmp_path):
    reg = _real(_cfg(tmp_path, quality_eccentricity_max=0.7))["rigs"]["rc16"]["regions"][0]
    assert reg["limit_s"] == 240.0 and reg["source"] == "drift model"
    assert reg["confidence"] == "low" and reg["probe_s"] is None   # 300 measured


def _rows(cfg, spec, dec=41.27, ra_deg=10.68):
    subs, t = [], T0
    for exp, eccs in spec:
        for e in eccs:
            subs.append(_sub(t, exp, e, dec=dec, ra_deg=ra_deg))
            t += timedelta(seconds=exp + 30)
    return ul.evidence(cfg, "rc16", N13, subs, plan=PLAN_U)[0]


def test_drift_model_extrapolates_from_short_subs(tmp_path):
    cfg = _cfg(tmp_path)
    # k = e/sqrt(1-e^2) grows with the sub length: 120 s at ecc 0.30 is
    # k/s = 0.00262 -> 240 s predicts ecc 0.53 (passes), 300 s 0.62 (fails)
    st = ul.stats(_rows(cfg, [(120, [0.30, 0.30, 0.30])]), cfg, "rc16")
    assert st["verdicts"]["240"]["source"] == "drift model"
    assert st["verdicts"]["240"]["predicted_ecc"] == pytest.approx(0.532, abs=0.01)
    assert st["verdicts"]["300"]["ok"] is False
    assert st["limit_s"] == 240.0 and st["probe_s"] == 300.0 and st["uncertain"]


def test_a_measured_failure_caps_the_longer_lengths(tmp_path):
    cfg = _cfg(tmp_path)
    # 180 s fails on measurement, 240 s had a lucky pair: the limit stays 120
    st = ul.stats(_rows(cfg, [(120, [0.3] * 8), (180, [0.7, 0.7, 0.4]),
                              (240, [0.4, 0.4])]), cfg, "rc16")
    assert st["verdicts"]["180"] == {"exp_s": 180.0, "ok": False, "source": "measured",
                                     "predicted_ecc": st["verdicts"]["180"]["predicted_ecc"]}
    assert st["limit_s"] == 120.0 and st["confidence"] == "high"
    # every step trails
    st = ul.stats(_rows(cfg, [(120, [0.8, 0.8])]), cfg, "rc16")
    assert st["limit_s"] is None and st["fails_all"]


def test_lookup_falls_back_region_dec_band_whole_sky(tmp_path):
    cfg = _cfg(tmp_path)
    tbl = _real(cfg)["rigs"]["rc16"]
    reg = tbl["regions"][0]
    ha_mid = (reg["ha_band"][0] + reg["ha_band"][1]) / 2
    assert ul.lookup(tbl, 41.0, ha_mid)["scope"] == "region"
    assert ul.lookup(tbl, 35.0, 2.0)["scope"] == "dec band"
    hit = ul.lookup(tbl, 61.0, 0.0)
    assert hit["scope"] == "whole sky" and hit["limit_s"] == 180.0
    assert ul.lookup(None, 0, 0) is None
    assert ul.lookup({"regions": [], "dec_bands": [], "overall": {"n": 0}}, 0, 0) is None


def test_ladder_includes_240_and_the_rigs_own_length(tmp_path):
    cfg = _cfg(tmp_path)
    assert ul.ladder(cfg) == [300.0, 240.0, 180.0, 120.0]
    assert ul.ladder(cfg, "piggyback")[0] == 450.0
    assert ul.ladder(_cfg(tmp_path, unguided_limit_steps="x, ,")) == [300.0, 240.0, 180.0, 120.0]


# --------------------------------------------------------------------------
# 3. the planner
# --------------------------------------------------------------------------

def _target(name="Andromeda Galaxy", ra=M31_RA_H, dec=M31_DEC, exps=(("L", 300.0, 10),),
            guided=False, ub=()):
    t = NinaSequenceTarget(
        name=name, ra_hours=ra, dec_degrees=dec, start_guiding=guided,
        unguided_filters=list(ub),
        exposures=[ExposurePlan(filter_type=FilterType(f), exposure_seconds=s, count=n)
                   for f, s, n in exps])
    t.window_start_utc = datetime(2026, 10, 14, 2, 55)
    t.window_end_utc = datetime(2026, 10, 14, 5, 15)
    return t


def _status(matched=(180.0, 300.0), scaled=(240.0, 120.0)):
    def f(s):
        if s in matched:
            return {"match": "matched", "text": f"darks match {s:g}"}
        if s in scaled:
            return {"match": "scaled", "text": f"scaled {s:g}"}
        return {"match": "none", "text": ""}
    return f


def test_advise_records_and_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    t = _target(exps=(("L", 300.0, 10), ("R", 120.0, 5)))
    rec = ul.apply_targets(cfg, [t], tbl=_real(cfg), status=_status())
    assert rec["mode"] == "advise"
    assert [e.exposure_seconds for e in t.exposures] == [300.0, 120.0]
    (row,) = rec["targets"]
    assert row["filter"] == "L" and row["from_s"] == 300.0 and row["to_s"] == 180.0
    assert not row["applied"] and row["dark"] == "matched"
    assert row["why"].startswith("unguided limit learned: 180 s at Dec +41 east")


def test_apply_cuts_with_the_same_integration(tmp_path):
    cfg = _cfg(tmp_path, unguided_auto_length="apply")
    t = _target(exps=(("L", 300.0, 10), ("Ha", 600.0, 4), ("R", 120.0, 5)))
    rec = ul.apply_targets(cfg, [t], tbl=_real(cfg), status=_status())
    got = [(e.filter_type.value, e.exposure_seconds, e.count) for e in t.exposures]
    assert got == [("L", 180.0, 17), ("Ha", 180.0, 14), ("R", 120.0, 5)]
    assert all(r["applied"] for r in rec["targets"])


def test_dark_snap_prefers_matched_then_scaled(tmp_path):
    cfg = _cfg(tmp_path)
    assert ul.snap_length(cfg, "rc16", 240, 300, status=_status())[0] == 180.0
    assert ul.snap_length(cfg, "rc16", 240, 300, status=_status(matched=()))[0] == 240.0
    assert ul.snap_length(cfg, "rc16", 240, 300,
                          status=_status(matched=(), scaled=()))[0] == 240.0
    assert ul.snap_length(cfg, "rc16", 240, 200, status=_status())[0] == 180.0


def test_overrides_guided_and_off(tmp_path):
    from types import SimpleNamespace as NS
    cfg = _cfg(tmp_path, unguided_auto_length="apply")
    t = _target()
    proj = NS(target=NS(name="Andromeda Galaxy"), exposure_overrides={"L": 300})
    rec = ul.apply_targets(cfg, [t], [proj], tbl=_real(cfg), status=_status())
    assert t.exposures[0].exposure_seconds == 300.0
    assert rec["targets"][0]["note"] == "hand-set exposure override wins"
    g = _target(guided=True)
    assert ul.apply_targets(cfg, [g], tbl=_real(cfg), status=_status())["targets"] == []
    gb = _target(guided=True, exps=(("L", 300.0, 4), ("Ha", 300.0, 4)), ub=("Ha",))
    ul.apply_targets(cfg, [gb], tbl=_real(cfg), status=_status())
    assert [e.exposure_seconds for e in gb.exposures] == [300.0, 180.0]
    off = _cfg(tmp_path, unguided_auto_length="off")
    t2 = _target()
    assert ul.apply_targets(off, [t2], tbl=_real(cfg))["targets"] == []
    # no table: nothing happens
    assert ul.apply_targets(cfg, [_target()], tbl={"rigs": {}})["targets"] == []


def test_probe_advise_and_apply(tmp_path):
    cfg = _cfg(tmp_path, unguided_limit_probe="advise")
    t = _target()
    rec = ul.apply_targets(cfg, [t], tbl=_real(cfg), status=_status())
    (p,) = rec["probes"]
    assert p["probe_s"] == 240.0 and p["subs"] == 2 and not p["applied"]
    assert "kept only if they pass" in p["why"]
    cfg = _cfg(tmp_path, unguided_limit_probe="apply", unguided_auto_length="apply")
    t = _target()
    rec = ul.apply_targets(cfg, [t], tbl=_real(cfg), status=_status())
    got = [(e.exposure_seconds, e.count) for e in t.exposures]
    assert got == [(240.0, 2), (180.0, 14)]        # 17 - round(2*240/180)
    assert rec["probes"][0]["applied"]
    # a goal that never runs 240 s gets no probe
    t = _target(exps=(("L", 180.0, 10),))
    assert ul.apply_targets(cfg, [t], tbl=_real(cfg), status=_status())["probes"] == []


def test_piggy_companion_length(tmp_path):
    cfg = _cfg(tmp_path, unguided_auto_length="apply")
    tbl = _real(cfg)
    rec = ul.apply_targets(cfg, [_target()], tbl=tbl, status=_status())
    assert rec["piggyback"]["limit_s"] == 450.0 and not rec["piggyback"]["applied"]
    # a Piggy table that trails at 450 s: the companion goes to 300 s
    tbl["rigs"]["piggyback"]["overall"].update(limit_s=300.0)
    tbl["rigs"]["piggyback"]["regions"] = []
    tbl["rigs"]["piggyback"]["dec_bands"] = []
    rec = ul.apply_targets(cfg, [_target()], tbl=tbl, status=_status(matched=(300.0,)))
    assert rec["piggyback"]["to_s"] == 300.0 and rec["piggyback"]["applied"]

    from photonscript.scheduler.armer import Armer
    a = Armer.__new__(Armer)
    a.plan = {"unguided_limits": rec}
    assert a._piggy_unguided_cfg(cfg).piggyback_exposure_s == 300.0
    a.plan = {"unguided_limits": {**rec, "piggyback": {**rec["piggyback"], "applied": False}}}
    assert a._piggy_unguided_cfg(cfg).piggyback_exposure_s == 450.0


def test_armer_dispatch_records_the_plan(tmp_path, monkeypatch):
    from photonscript.scheduler.armer import Armer
    cfg = _cfg(tmp_path)
    a = Armer.__new__(Armer)
    a.config, a.plan = cfg, {"night_of": N13}
    ul.refresh(cfg, N13, nights_data={n: _night(n) for n in (N12, N13)})
    monkeypatch.setattr("photonscript.scheduler.night_pilot.dark_match",
                        lambda c, r, s: _status()(s))
    t = _target()
    a._unguided_limits([t], [], datetime(2026, 10, 14, 3, 0))
    assert a.plan["unguided_limits"]["targets"][0]["to_s"] == 180.0
    assert t.exposures[0].exposure_seconds == 300.0             # advise
    monkeypatch.setattr(ul, "apply_targets", lambda *a, **k: 1 / 0)
    a._unguided_limits([t], [], datetime(2026, 10, 14, 3, 0))   # never raises
    assert a.plan["unguided_limits"] is None
    src = (Path(ul.__file__).parent / "armer.py").read_text(encoding="utf-8")
    assert "self._unguided_limits(targets, projects, now)" in src
    assert "pcfg = self._piggy_unguided_cfg(pcfg)" in src


def test_plan_snapshot_carries_the_record(tmp_path):
    from photonscript.scheduler import runs
    cfg = _cfg(tmp_path)
    runs.save_plan_snapshot(cfg, N13, {"unguided_limits": {"mode": "advise"}}, [])
    snap = json.loads((runs.runs_dir(cfg) / f"{N13}_plan.json").read_text())
    assert snap["unguided_limits"] == {"mode": "advise"}


# --------------------------------------------------------------------------
# 4. calibration
# --------------------------------------------------------------------------

def test_learned_dark_lengths_180_and_240(tmp_path):
    cfg = _cfg(tmp_path)
    doc = _real(cfg)
    assert ul.learned_dark_lengths(cfg, "rc16", doc) == [180.0, 240.0]
    assert ul.learned_dark_lengths(cfg, "piggyback", doc) == [450.0]
    assert ul.learned_dark_lengths(_cfg(tmp_path, unguided_limit_darks_max=1),
                                   "rc16", doc) == [180.0]
    # gate 0.7: 240 s from the drift model, so 180 s (the step under) too
    c7 = _cfg(tmp_path, quality_eccentricity_max=0.7)
    assert ul.learned_dark_lengths(c7, "rc16", _real(c7)) == [240.0, 180.0]
    assert ul.learned_dark_lengths(_cfg(tmp_path, unguided_auto_length="off"),
                                   "rc16", doc) == []
    assert ul.learned_dark_lengths(cfg, "rc16") == []          # no store yet


def test_night_quota_and_owed_view_get_the_learned_lengths(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration
    from photonscript.scheduler import calibration_owed as co
    from photonscript.scheduler import calibration_plan as cp
    cfg = _cfg(tmp_path, dark_exposures="600,180")
    base = calibration.night_dark_exposures(cfg, "rc16")
    ul.refresh(cfg, N13, nights_data={n: _night(n) for n in (N12, N13)})
    got = calibration.night_dark_exposures(cfg, "rc16")
    assert got[:len(base)] == base and 240.0 in got
    assert got.count(180.0) == 1
    monkeypatch.setattr(cp, "load_projects", lambda c: [])
    monkeypatch.setattr(cp, "tonight_plan_rows", lambda c: [])
    r = co.owed_report(cfg, "rc16", projects=[])["rigs"][0]
    d = {it["exp_s"]: it for it in r["darks"]}
    assert d[240.0]["from_unguided_limits"] and d[240.0]["auto_fill"]
    assert d[240.0]["owed"] > 0 and "unguided limit learned" in d[240.0]["planned"]
    assert not d[180.0]["from_unguided_limits"] or d[180.0]["in_quota_list"]


# --------------------------------------------------------------------------
# 5. refresh, dawn, morning, CLI, API, page, config
# --------------------------------------------------------------------------

def test_refresh_reads_local_nights_and_stores(tmp_path):
    from photonscript.scheduler import runs
    cfg = _cfg(tmp_path)
    for n in (N12, N13):
        d = _night(n)
        (runs.runs_dir(cfg) / f"{n}_subs.jsonl").write_text(
            "".join(json.dumps(s) + "\n" for s in d["subs"]), encoding="utf-8")
        (runs.runs_dir(cfg) / f"{n}_plan.json").write_text(json.dumps(d["plan"]))
    (runs.runs_dir(cfg) / "2026-10-01_subs.jsonl").write_text("", encoding="utf-8")
    assert ul.local_nights(cfg, N13, "2026-10-09") == [N12, N13]
    doc = ul.refresh(cfg, N13)
    assert ul.load(cfg)["rigs"]["rc16"]["regions"][0]["limit_s"] == 180.0
    assert doc["since"] == "2026-10-09"
    txt = ul.format_table(doc, "rc16")
    assert "limit 180 s (measured, medium) probe 240 s" in txt


def test_dawn_filing_refreshes(tmp_path, monkeypatch):
    from photonscript.scheduler import dawn_autofile as da
    cfg = _cfg(tmp_path)
    seen = []
    monkeypatch.setattr(ul, "refresh", lambda c, d: seen.append(d) or {"rigs": {}})
    rec = da.file_night(cfg, N13, push=False)
    assert seen == [N13] and "unguided limits" not in " ".join(rec["errors"])
    off = _cfg(tmp_path, unguided_auto_length="off")
    rec = da.file_night(off, N13, push=False)
    assert rec["unguided_limits"] == {"skipped": "unguided_auto_length off"}


def test_morning_line_tonight_and_plan(tmp_path):
    cfg = _cfg(tmp_path)
    nd = _night(N13)
    t = _target()
    nd["plan"]["unguided_limits"] = ul.apply_targets(
        _cfg(tmp_path, unguided_limit_probe="advise"), [t], tbl=_real(cfg), status=_status())
    line = ul.morning_line(cfg, N13, night_data=nd)
    assert line.startswith("Unguided limits: tonight Andromeda Galaxy (Dec +41 east) "
                           "180 s 5/5, 300 s 0/2 round")
    assert "Andromeda Galaxy L 300->180 s suggested" in line
    assert "probe Andromeda Galaxy 2 x 240 s suggested" in line
    assert "unguided_auto_length=apply" in line
    line12 = ul.morning_line(cfg, N12, night_data=_night(N12))
    assert line12.startswith("Unguided limits: no clean unguided subs tonight (")
    assert "excluded: PHD2 active)" in line12
    assert ul.morning_line(_cfg(tmp_path, unguided_auto_length="off"), N13,
                           night_data=nd) is None


def test_morning_report_card_has_the_line(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration_owed
    from photonscript.scheduler import morning_report as mr
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(calibration_owed, "morning_note", lambda c: None)
    monkeypatch.setattr(ul, "morning_line", lambda c, d: "Unguided limits: x")
    from photonscript.scheduler import runs
    (runs.runs_dir(cfg) / f"{N13}_subs.jsonl").write_text(
        json.dumps({"rig": "rc16", "file": "a.fits", "target": "M31", "filter": "L",
                    "exp_s": 180, "passed_qa": True}) + "\n", encoding="utf-8")
    card = mr.report_card(cfg, N13, projects=[])
    assert "Unguided limits: x" in card["lines"]


def test_api_and_guiding_page(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    c = TestClient(app_mod.app)
    d = c.get("/api/tracking/unguided-limits").json()
    assert d["mode"] == "advise" and d["probe"] == "off"
    ul.refresh(cfg, N13, nights_data={n: _night(n) for n in (N12, N13)})
    d = c.get("/api/tracking/unguided-limits?rig=rc16").json()
    assert list(d["rigs"]) == ["rc16"]
    rows = d["rigs"]["rc16"]["rows"]
    assert rows[0]["limit_s"] == 180.0 and rows[0]["lengths"]["180"] == "5/5"
    assert d["rigs"]["rc16"]["dark_lengths"] == [180.0, 240.0]
    root = Path(app_mod.__file__).parent
    html = (root / "templates" / "guiding.html").read_text(encoding="utf-8")
    assert 'id="ulSec"' in html and "UNGUIDEDLIMITS.init()" in html
    js = (root / "static" / "js" / "unguided_limits_panel.js").read_text(encoding="utf-8")
    assert "/api/tracking/unguided-limits" in js and js.isascii()


def test_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    ul.refresh(cfg, N13, nights_data={n: _night(n) for n in (N12, N13)})
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    r = CliRunner().invoke(cli.app, ["unguided-limits", "--rig", "rc16"])
    assert r.exit_code == 0, r.output
    assert "limit 180 s" in r.output and "darks owed for learned lengths" in r.output
    r = CliRunner().invoke(cli.app, ["unguided-limits", "--json"])
    assert json.loads(r.output)["rigs"]["rc16"]["regions"][0]["probe_s"] == 240.0


def test_config_defaults_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.unguided_auto_length == "advise" and c.unguided_limit_probe == "off"
    assert c.unguided_limit_steps == "300,240,180,120"
    assert c.unguided_limit_pass_rate == 0.8
    keys = {f[0] for f in _CONFIG_FIELDS}
    for k in ("unguided_auto_length", "unguided_limit_steps", "unguided_limit_pass_rate",
              "unguided_limit_min_subs", "unguided_limit_model_margin",
              "unguided_limit_lookback_days", "unguided_limit_since",
              "unguided_limit_darks_max", "unguided_limit_probe",
              "unguided_limit_probe_subs"):
        assert k in keys
    assert ul.mode(PhotonScriptConfig(_env_file=None, unguided_auto_length="bogus")) == "advise"


def test_source_is_ascii_without_em_dashes():
    for p in (Path(ul.__file__),
              Path(ul.__file__).parent / "static" / "js" / "unguided_limits_panel.js"):
        txt = p.read_text(encoding="utf-8")
        assert txt.isascii()
