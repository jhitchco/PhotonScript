"""Focus table: per-filter RC16 positions from the PS-76 model and
same-night AF pairs, the frozen-thermometer fallback, focuser counter-shift
re-anchoring, drive-mode decisions and the AF-minutes payoff.

Fixture: RC16-shaped AF points (slope -8.25 steps/C, L intercept 5900,
Ha +121, R -34, OIII +80, SII +90 from L, 20-step noise, 110-step AF step
= CFZ), nine nights 2026-10-01..09 with an L AF every ~40 min while the
temperature falls 24 -> 17 C, a few R / Ha AFs, and a dusk calibration
(L, Ha, L, OIII, L, SII, L, 8 min apart) on three nights."""
import asyncio
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import focus_model as fm
from photonscript.scheduler import focus_table as ft
from photonscript.shared.config import PhotonScriptConfig

SLOPE = -8.25
L0 = 5900.0
OFF = {"L": 0, "R": -34, "Ha": 121, "OIII": 80, "SII": 90}
CAL_NIGHTS = (3, 5, 8)


def _cfg(tmp_path, **kw):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    kw.setdefault("focus_counter_epoch", "")
    return PhotonScriptConfig(_env_file=None, data_dir=str(data), **kw)


def _pos(f, t, noise=0.0):
    return round(L0 + OFF[f] + SLOPE * t + noise)


def _pt(f, when: datetime, t, noise=0.0, r2=0.98, **kw):
    return dict({"filter": f, "temp": round(t, 2), "rig": "rc16",
                 "position": _pos(f, t, noise), "r2": r2, "step": 110,
                 "duration_s": 180.0,
                 "time": when.strftime("%Y-%m-%dT%H:%M:%S") + ".0-06:00"},
                **kw)


def _nights(n=9, seed=3, first=date(2026, 10, 1)):
    random.seed(seed)
    pts = []
    for k in range(n):
        d = first + timedelta(days=k)
        start = datetime(d.year, d.month, d.day, 20, 0)
        if k + 1 in CAL_NIGHTS:
            t = 24.0 - k * 0.3
            for i, f in enumerate(("L", "Ha", "L", "OIII", "L", "SII", "L")):
                pts.append(_pt(f, start + timedelta(minutes=8 * i), t,
                               random.gauss(0, 12)))
            start += timedelta(minutes=60)
        for i in range(12):
            t = 24.0 - 7.0 * i / 11 - k * 0.2
            when = start + timedelta(minutes=40 * i)
            f = "R" if i == 5 else ("Ha" if i == 9 else "L")
            pts.append(_pt(f, when, t, random.gauss(0, 20)))
    return pts


def _store(cfg, pts):
    (Path(cfg.data_dir) / fm.POINTS_FILE).write_text(json.dumps(pts))


# ------------------------------------------------------------ the table

class TestTable:
    def test_positions_follow_the_formula(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _nights())
        tab = ft.build_table(cfg, temp=20.0)
        assert tab["slope_steps_per_c"] == pytest.approx(SLOPE, abs=1.5)
        assert tab["cfz_steps"] == 110 and tab["trusted"] is True
        assert list(tab["filters"]) == ["L", "R", "G", "B", "Ha", "OIII",
                                        "SII"]
        rows = tab["filters"]
        for f in ("L", "R", "Ha", "OIII", "SII"):
            assert rows[f]["position"] == pytest.approx(_pos(f, 20.0),
                                                        abs=30), f
            assert rows[f]["position"] == round(
                tab["intercept_ref"] + tab["slope_steps_per_c"] * 20.0
                + rows[f]["offset"])
        # G / B: never focused, no configured offset -> L position, no trust
        assert rows["G"]["offset_source"] == "none"
        assert rows["G"]["position"] == rows["L"]["position"]
        assert rows["G"]["confidence"] == "none"
        assert rows["L"]["confidence"] == "high"

    def test_drive_ready_filters(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _nights())
        tab = ft.build_table(cfg, temp=20.0)
        # L: plenty; R and Ha: 9 / 12 AFs of their own, recent; OIII / SII:
        # three pairs (high) but only three AFs of their own
        assert set(tab["drive_ready"]) == {"L", "R", "Ha"}
        why = " ".join(tab["filters"]["OIII"]["drive_reasons"])
        assert "3 AF(s) of its own, needs 5" in why

    def test_stale_filter_is_not_ready(self, tmp_path):
        cfg = _cfg(tmp_path)
        pts = [p for p in _nights() if p["filter"] != "R"]
        old = datetime(2026, 9, 10, 22, 0)
        pts += [_pt("R", old + timedelta(minutes=30 * i), 22.0 - i)
                for i in range(6)]
        _store(cfg, pts)
        row = ft.build_table(cfg, temp=20.0)["filters"]["R"]
        assert row["drive_ready"] is False
        assert any("days older" in r for r in row["drive_reasons"])

    def test_extrapolated_temperature_is_med(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _nights())
        tab = ft.build_table(cfg, temp=2.0)
        assert tab["filters"]["L"]["confidence"] == "med"
        assert tab["drive_ready"] == []

    def test_untrusted_model_drives_nothing(self, tmp_path):
        cfg = _cfg(tmp_path, focus_cfz_steps=20)   # scatter limit 10
        _store(cfg, _nights())
        tab = ft.build_table(cfg, temp=20.0)
        assert tab["trusted"] is False and tab["drive_ready"] == []

    def test_empty_store(self, tmp_path):
        cfg = _cfg(tmp_path)
        tab = ft.build_table(cfg)
        assert tab["drive_ready"] == [] and tab["filters"]["L"]["position"] \
            is None

    def test_text_render(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _nights())
        txt = ft.format_table(ft.build_table(cfg, temp=20.0))
        assert "Drive-ready today: L, R, Ha" in txt
        assert "PS_FOCUS_FILTER_OFFSETS=" in txt


# ------------------------------------------------------------ AF pairs

class TestPairOffsets:
    def test_pairs_measure_the_offsets(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _nights())
        tab = ft.build_table(cfg, temp=20.0)
        for f in ("Ha", "OIII", "SII"):
            row = tab["filters"][f]
            assert row["offset_source"] == "pairs" and row["n_pairs"] == 3
            assert row["offset"] == pytest.approx(OFF[f], abs=25), f
        # R has no same-night pair (no L within 30 min before it at 1 C):
        # the model offset stands in
        assert tab["filters"]["R"]["offset_source"] == "model"
        assert tab["filters"]["R"]["offset"] == pytest.approx(-34, abs=25)
        assert tab["proposed_offsets"]["Ha"] == tab["filters"]["Ha"]["offset"]
        assert tab["proposed_env"].startswith("PS_FOCUS_FILTER_OFFSETS=Ha:")

    def test_pair_window_and_temperature(self):
        t0 = datetime(2026, 10, 5, 21, 0)
        pts = [_pt("L", t0, 20.0),
               _pt("Ha", t0 + timedelta(minutes=20), 20.0),     # pair
               _pt("L", t0 + timedelta(hours=2), 18.0),
               _pt("Ha", t0 + timedelta(hours=2, minutes=40), 18.0),  # 40 min
               _pt("L", t0 + timedelta(hours=4), 16.0),
               _pt("Ha", t0 + timedelta(hours=4, minutes=5), 14.5),   # 1.5 C
               _pt("Ha", t0 + timedelta(hours=5), 14.0),
               _pt("L", t0 + timedelta(hours=5, minutes=5), 14.0)]    # L after
        out = ft.pair_offsets(pts, "L", SLOPE)
        assert out["Ha"]["n"] == 1 and out["Ha"]["steps"] == 121

    def test_temperature_difference_is_corrected(self):
        t0 = datetime(2026, 10, 5, 21, 0)
        pts = [_pt("L", t0, 20.0), _pt("OIII", t0 + timedelta(minutes=10),
                                       19.2)]
        assert ft.pair_offsets(pts, "L", SLOPE)["OIII"]["steps"] == 80
        # without the slope the 0.8 C would read as +6.6 steps of offset
        assert ft.pair_offsets(pts, "L", None)["OIII"]["steps"] == 87

    def test_offset_source_order(self, tmp_path):
        model = {"offsets": {"R": {"steps": -30, "n": 9, "n_ref": 50,
                                   "se": 8.0},
                             "OIII": {"steps": 128, "n": 1, "n_ref": 50}}}
        pairs = {"Ha": {"steps": 120, "n": 3, "se": 4.0},
                 "OIII": {"steps": 79, "n": 1, "se": None},
                 "R": {"steps": 200, "n": 1, "se": None}}
        conf = {"Ha": 120, "OIII": 120, "SII": 120}
        o = lambda f: ft._offset_for(f, "L", pairs, model, conf, 110)  # noqa
        assert o("Ha")["source"] == "pairs"
        assert o("R")["source"] == "model" and "pair" in o("R")["note"]
        assert o("OIII")["source"] == "pairs (few)" and o("OIII")["steps"] == 79
        assert o("SII")["source"] == "config" and o("SII")["steps"] == 120
        assert o("G")["source"] == "none" and o("G")["steps"] == 0
        assert o("L")["source"] == "reference"

    def test_config_offsets_proposal_keeps_unmeasured(self, tmp_path):
        cfg = _cfg(tmp_path, focus_filter_offsets="Ha:-187,OIII:120,SII:120")
        pts = [p for p in _nights() if p["filter"] not in ("OIII", "SII")]
        _store(cfg, pts)
        tab = ft.build_table(cfg, temp=20.0)
        prop = tab["proposed_offsets"]
        assert prop["OIII"] == 120 and prop["SII"] == 120   # not measured
        assert prop["Ha"] == pytest.approx(121, abs=25)    # PS-144 fix
        assert tab["filters"]["SII"]["offset_source"] == "config"
        assert tab["filters"]["SII"]["confidence"] == "low"


# ------------------------------------------------------------ thermometer

class TestFrozenTemperature:
    def test_live_focuser_is_used(self, tmp_path):
        cfg = _cfg(tmp_path)
        t0 = datetime(2026, 10, 12, 21, 0)
        out = ft.choose_temperature(cfg, 18.29, 17.6, now=t0)
        assert out["source"] == "focuser" and out["temp"] == 18.29

    def test_known_frozen_value_uses_ambient(self, tmp_path):
        cfg = _cfg(tmp_path)     # focus_frozen_temps default "21.29"
        out = ft.choose_temperature(cfg, 21.29, 15.4,
                                    now=datetime(2026, 10, 12, 23, 0))
        assert out["source"] == "ambient" and out["temp"] == 15.4
        assert "frozen" in out["reason"]

    def test_known_frozen_value_without_ambient(self, tmp_path):
        cfg = _cfg(tmp_path)
        out = ft.choose_temperature(cfg, 21.29, None,
                                    now=datetime(2026, 10, 12, 23, 0))
        assert out["source"] == "focuser" and "no ambient" in out["reason"]

    def test_stuck_reading_while_ambient_moves(self, tmp_path):
        cfg = _cfg(tmp_path, focus_frozen_temps="")
        t0 = datetime(2026, 10, 12, 21, 0)
        assert ft.choose_temperature(cfg, 19.29, 19.0, now=t0)["source"] \
            == "focuser"
        # 40 min later, same reading, ambient down 1.5 C: not long enough
        o = ft.choose_temperature(cfg, 19.29, 17.5,
                                  now=t0 + timedelta(minutes=40))
        assert o["source"] == "focuser"
        # 75 min: stuck
        o = ft.choose_temperature(cfg, 19.29, 17.5,
                                  now=t0 + timedelta(minutes=75))
        assert o["source"] == "ambient" and o["focuser_unchanged_min"] == 75
        assert "stuck" in o["reason"]
        # ambient steady for the hour: the focuser is trusted
        cfg2 = _cfg(tmp_path / "b", focus_frozen_temps="")
        ft.choose_temperature(cfg2, 19.29, 19.0, now=t0)
        o = ft.choose_temperature(cfg2, 19.29, 19.4,
                                  now=t0 + timedelta(minutes=90))
        assert o["source"] == "focuser"

    def test_bias_learned_from_live_readings(self, tmp_path):
        cfg = _cfg(tmp_path)
        t0 = datetime(2026, 10, 12, 20, 0)
        for i, (f, a) in enumerate(((20.29, 19.29), (19.29, 18.29),
                                    (18.29, 17.29))):
            ft.choose_temperature(cfg, f, a, now=t0 + timedelta(minutes=20 * i))
        o = ft.choose_temperature(cfg, 21.29, 16.0,
                                  now=t0 + timedelta(minutes=80))
        assert o["source"] == "ambient" and o["bias"] == pytest.approx(1.0,
                                                                       abs=.01)
        assert o["temp"] == pytest.approx(17.0, abs=0.01)
        track = json.loads((Path(cfg.data_dir) / ft.TEMP_TRACK_FILE)
                           .read_text())
        assert track["focuser"] == 21.29

    def test_no_focuser_temperature(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert ft.choose_temperature(cfg, None, 12.0)["source"] == "ambient"
        assert ft.choose_temperature(cfg, "NaN", None)["source"] == "none"

    def test_move_endpoint_logs_the_source(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        from photonscript.scheduler.routers import focus
        cfg = _cfg(tmp_path, focus_model_drive="drive",
                   nina_base_url="http://x:1888/v2/api")
        _store(cfg, _nights())
        monkeypatch.setattr(app, "_config", cfg)
        moves = []

        async def info(base):
            return {"Temperature": 21.29, "Position": 5000}

        async def wx(base):
            return {"Temperature": 18.0}

        async def move(base, pos):
            moves.append(pos)
            return {"Success": True}

        monkeypatch.setattr(focus, "_nina_focuser", info)
        monkeypatch.setattr(focus, "_nina_weather", wx)
        monkeypatch.setattr(focus, "_nina_move", move)
        out = asyncio.run(focus.api_focus_model_move(filter="L"))
        assert out["verdict"] == "MOVED" and out["temp_source"] == "ambient"
        assert out["temp"] == 18.0 and out["focuser_temp"] == 21.29
        assert moves[0] == pytest.approx(_pos("L", 18.0), abs=30)
        assert fm.recent_moves(cfg)[-1]["temp_source"] == "ambient"


# ------------------------------------------------------------ counter shift

EPOCH = "2026-10-10T12:00:00"


def _after(cfg, rows):
    """Add AFs on the night of 2026-10-10 (after EPOCH) to the fixture."""
    t0 = datetime(2026, 10, 10, 21, 0)
    pts = _nights() + [_pt(f, t0 + timedelta(minutes=30 * i), t, j, r2=r2)
                       for i, (f, t, j, r2) in enumerate(rows)]
    _store(cfg, pts)
    return pts


class TestCounterShift:
    def test_no_shift_is_identity(self, tmp_path):
        cfg = _cfg(tmp_path, focus_counter_epoch=EPOCH)
        _after(cfg, [("L", 20.0, 15, 0.98), ("L", 19.0, -20, 0.99)])
        st = fm.counter_status(cfg)
        assert st["events"] == [] and st["shift"] == 0 and st["flag"] is None
        assert fm._rc16_points(cfg) == fm._rc16_raw_points(cfg)

    def test_jump_is_flagged_and_reanchored(self, tmp_path):
        cfg = _cfg(tmp_path, focus_counter_epoch=EPOCH)
        _after(cfg, [("L", 20.0, 0, 0.98), ("L", 19.0, 600, 0.99)])
        st = fm.counter_status(cfg)
        assert len(st["events"]) == 1 and st["pending"]["status"] == \
            "provisional"
        assert st["shift"] == pytest.approx(600, abs=30)
        assert "focuser counter shifted?" in st["flag"]
        tab = ft.build_table(cfg, temp=19.0)
        # re-anchored on the new AF, not averaged in (one AF of ~120 would
        # move a plain fit by about 5 steps)
        assert tab["filters"]["L"]["position"] == pytest.approx(
            _pos("L", 19.0) + 600, abs=30)
        assert tab["filters"]["Ha"]["position"] == pytest.approx(
            _pos("Ha", 19.0) + 600, abs=40)
        # nothing drives while the shift waits for a confirming AF
        assert tab["drive_ready"] == []
        assert "counter shift" in " ".join(tab["filters"]["L"]
                                           ["drive_reasons"])

    def test_next_af_confirms(self, tmp_path):
        cfg = _cfg(tmp_path, focus_counter_epoch=EPOCH)
        _after(cfg, [("L", 20.0, 600, 0.99), ("L", 19.0, 610, 0.98)])
        st = fm.counter_status(cfg)
        assert st["events"][0]["status"] == "confirmed"
        assert st["pending"] is None and st["flag"] is None
        tab = ft.build_table(cfg, temp=19.0)
        assert "L" in tab["drive_ready"]
        assert tab["filters"]["L"]["position"] == pytest.approx(
            _pos("L", 19.0) + 600, abs=30)

    def test_one_off_is_reverted(self, tmp_path):
        cfg = _cfg(tmp_path, focus_counter_epoch=EPOCH)
        _after(cfg, [("L", 20.0, 600, 0.99), ("L", 19.0, 10, 0.98)])
        st = fm.counter_status(cfg)
        assert st["events"][0]["status"] == "reverted" and st["shift"] == 0
        tab = ft.build_table(cfg, temp=19.0)
        assert tab["filters"]["L"]["position"] == pytest.approx(
            _pos("L", 19.0), abs=30)

    def test_poor_curve_and_old_points_never_shift(self, tmp_path):
        cfg = _cfg(tmp_path, focus_counter_epoch=EPOCH)
        _after(cfg, [("L", 20.0, 600, 0.6)])          # R^2 0.6: not trusted
        assert fm.counter_status(cfg)["events"] == []
        # a big jump BEFORE the epoch is history, taken as it is
        cfg2 = _cfg(tmp_path / "b", focus_counter_epoch=EPOCH)
        pts = _nights()
        pts.append(_pt("L", datetime(2026, 10, 9, 23, 0), 18.0, 600))
        _store(cfg2, pts)
        assert fm.counter_status(cfg2)["events"] == []

    def test_epoch_empty_is_off(self, tmp_path):
        cfg = _cfg(tmp_path, focus_counter_epoch="")
        _after(cfg, [("L", 20.0, 600, 0.99), ("L", 19.0, 610, 0.99)])
        assert fm.counter_status(cfg)["events"] == []

    def test_ingest_model_is_in_the_current_frame(self, tmp_path):
        cfg = _cfg(tmp_path, focus_counter_epoch=EPOCH,
                   nina_autofocus_reports_dir=str(tmp_path / "none"))
        (tmp_path / "none").mkdir()
        _after(cfg, [("L", 20.0, 600, 0.99), ("L", 19.0, 600, 0.99)])
        fm.ingest_af_reports(cfg)
        m = fm.load_model(cfg)
        assert m["filters"]["L"]["intercept"] == pytest.approx(L0 + 600,
                                                               abs=40)
        # the seed (AF start) follows the shift too
        from photonscript.scheduler.focus_seeds import seed_for
        assert seed_for("L", 19.0, cfg) == pytest.approx(
            _pos("L", 19.0) + 600, abs=40)


# ------------------------------------------------------------ drive mode

class TestDriveMode:
    def test_default_is_seed(self, tmp_path, monkeypatch):
        c = PhotonScriptConfig(_env_file=None)
        assert c.focus_model_drive == "seed" and fm.drive_mode(c) == "seed"
        assert c.focus_counter_epoch == "2026-10-12T20:20:00"
        assert c.focus_frozen_temps == "21.29"
        for v, want in ((True, "drive"), (False, "seed"), ("drive", "drive"),
                        ("true", "drive"), ("off", "seed"), ("", "seed"),
                        ("bogus", "seed")):
            assert PhotonScriptConfig(_env_file=None,
                                      focus_model_drive=v) \
                .focus_model_drive == want, v
        monkeypatch.setenv("PS_FOCUS_MODEL_DRIVE", "false")
        assert PhotonScriptConfig(_env_file=None).focus_model_drive == "seed"

    def test_seed_mode_never_moves(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _nights())
        t = fm.model_move_target(cfg, "L", 20.0)
        assert t["move"] is False and "seed" in t["reason"]
        assert fm.trust(cfg)["mode"] == "advisory"

    def test_seed_unchanged_by_the_table(self, tmp_path):
        """The default (seed) AF starting points are what the PS-76 model
        gave before the table: no shift -> same points, same seed."""
        from photonscript.scheduler.focus_seeds import seed_for
        cfg = _cfg(tmp_path, focus_counter_epoch="2026-10-12T20:20:00",
                   nina_autofocus_reports_dir=str(tmp_path / "none"))
        (tmp_path / "none").mkdir()
        _store(cfg, _nights())
        fm.ingest_af_reports(cfg)
        raw = fm.fit(fm._rc16_raw_points(cfg), ref_filter="L")
        for f in ("L", "Ha", "R"):
            assert seed_for(f, 20.0, cfg) == fm.predict(raw, f, 20.0)[
                "position"]

    def test_drive_moves_ready_filters_only(self, tmp_path):
        cfg = _cfg(tmp_path, focus_model_drive="drive")
        _store(cfg, _nights())
        t = fm.model_move_target(cfg, "H", 20.0)       # NINA name -> Ha
        assert t["move"] is True and t["filter"] == "Ha"
        assert t["position"] == pytest.approx(_pos("Ha", 20.0), abs=30)
        assert t["offset_source"] == "pairs"
        o = fm.model_move_target(cfg, "OIII", 20.0)
        assert o["move"] is False and "not drive-ready" in o["reason"]

    def test_sequence_spec(self, tmp_path, monkeypatch):
        from photonscript.scheduler import nina_sequence_json as nsj
        script = tmp_path / "focus-model-move.cmd"
        script.write_text("@echo off\r\nexit /b 0\r\n")
        cfg = _cfg(tmp_path, focus_model_move_script=str(script))
        _store(cfg, _nights())
        assert nsj._focus_drive_spec(cfg) is None          # seed default
        cfg = _cfg(tmp_path, focus_model_drive="drive",
                   focus_model_move_script=str(script))
        spec = nsj._focus_drive_spec(cfg)
        assert spec["filters"] == {"L", "R", "Ha"}
        assert spec["verify_min"] == 120.0
        # half a 110-step CFZ at 8.25 steps/C is 6.7 C: capped at 5 C
        assert spec["temp_c"] == 5.0

    def test_move_temp_step(self):
        assert ft.move_temp_step_c(-8.25, 110) == 5.0
        assert ft.move_temp_step_c(-30.0, 110) == pytest.approx(1.8)
        assert ft.move_temp_step_c(45.0, 90) == 1.0
        assert ft.move_temp_step_c(None, 110) == 1.0

    def test_decisions(self):
        ready = {"drive_ready": True, "drive_reasons": []}
        no = {"drive_ready": False, "drive_reasons": ["2 AF(s) of its own"]}
        d = ft.drive_decision
        assert d(ready, mode="seed")["action"] == "af"
        assert d(ready, night_start=True)["reason"] == "night / target start"
        assert d(ready, after_flip=True)["reason"] == "after a meridian flip"
        assert d(ready, hfr_soft=True)["action"] == "af"
        assert d(no, filter_changed=True)["reason"].startswith(
            "not drive-ready")
        assert d(ready, in_range=False)["action"] == "af"
        assert d(ready, minutes_since_af=125)["reason"].startswith(
            "verify AF due")
        assert d(ready, minutes_since_af=30, filter_changed=True)["action"] \
            == "move"
        assert d(ready, temp_change_c=-5.2, temp_step_c=5.0)["action"] \
            == "move"
        assert d(ready, temp_change_c=-2.0, temp_step_c=5.0)["action"] \
            == "hold"


# ------------------------------------------------------------ payoff

class TestPayoff:
    def test_last_week(self, tmp_path):
        cfg = _cfg(tmp_path)
        _store(cfg, _nights())
        pay = ft.payoff(cfg, nights=7, today=date(2026, 10, 10),
                        af_s=200.0, sub_s=180.0, per_sub_s=11.0)
        assert pay["n_nights"] == 7          # nights of 10-03 .. 10-09
        n = pay["nights"][-1]
        assert n["night"] == "2026-10-09" and n["afs"] == 12
        # 12 AFs over 7.3 h at 40-min spacing: the first, then a verify AF
        # every 120 min (3 more); the rest become table moves
        assert n["kept"] == 4 and n["saved"] == 8
        assert n["af_min_saved"] == pytest.approx(8 * (180 - ft.MOVE_S) / 60,
                                                  abs=0.1)
        assert n["shutter_h_gained"] == pytest.approx(
            8 * (180 - ft.MOVE_S) * 180 / 191 / 3600, abs=0.01)
        cal = next(x for x in pay["nights"] if x["night"] == "2026-10-05")
        # the calibration's OIII / SII AFs stay (not drive-ready)
        assert cal["kept_reasons"].get("not drive-ready") == 2
        assert pay["af_min_saved"] == pytest.approx(
            sum(x["af_min_saved"] for x in pay["nights"]), abs=0.2)

    def test_table_miss_keeps_the_af(self, tmp_path):
        cfg = _cfg(tmp_path)
        pts = _nights()
        bad = _pt("L", datetime(2026, 10, 9, 22, 10), 21.0, 300)
        _store(cfg, pts + [bad])
        pay = ft.payoff(cfg, nights=1, today=date(2026, 10, 10), af_s=200.0,
                        sub_s=180.0, per_sub_s=11.0)
        assert pay["nights"][0]["kept_reasons"].get(
            "HFR trend says focus is soft") == 1


# ------------------------------------------------------------ CLI + API

def test_cli_focus_table_local(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    _store(cfg, _nights())
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    res = CliRunner().invoke(cli.app, ["focus-table", "--local", "--temp",
                                       "20", "--json"])
    assert res.exit_code == 0, res.output
    tab = json.loads(res.output)
    assert tab["temp"] == 20.0 and "L" in tab["drive_ready"]
    res = CliRunner().invoke(cli.app, ["focus-table", "--local", "--temp",
                                       "20", "--payoff"])
    assert res.exit_code == 0 and "RC16 focus table at 20.00 C" in res.output
    assert "Payoff" in res.output
    res = CliRunner().invoke(cli.app, ["focus-table", "--rig", "piggyback"])
    assert res.exit_code == 2


def test_api_focus_table(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import focus
    cfg = _cfg(tmp_path)
    _store(cfg, _nights())
    monkeypatch.setattr(app, "_config", cfg)
    out = focus.api_focus_table(temp=20.0, payoff=True)
    assert out["filters"]["Ha"]["offset_source"] == "pairs"
    assert "nights" in out["payoff"]
    assert "error" in focus.api_focus_table(rig="piggyback")
    s = fm.summary(cfg)
    assert s["table"]["drive_ready"] == out["drive_ready"]
