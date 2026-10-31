"""PS-107: header-only (or mount-log / rc16-correlated) pointing offsets
reject only gross misses; a plate solve keeps the PS-67 bands. Re-verdict
through the dawn pass (dry run first), the rescore and the CLI."""
from pathlib import Path

import pytest

from photonscript.shared import pointing
from photonscript.shared import qa_rules as q
from photonscript.shared.config import PhotonScriptConfig
from tests.test_scheduler.test_ps67_pointing import (CAL_HDR, CATS, CATS_HDR,
                                                     NIGHT, _approved_record,
                                                     _cfg, _fits)
from tests.test_scheduler.test_ps67_pointing import _targets  # noqa: F401
from tests.test_scheduler.test_ps67_pointing import appcfg  # noqa: F401

# the mount model error: the header sits about 1 deg north of the target
# while NINA's offset re-slew has the frame on it (09-19 / 09-20 / 09-24)
MODEL_HDR = {**CATS_HDR, "DEC": CATS[2] + 1.0}


def _legacy_header_reject(cfg, rec, off, note):
    """A record as PS-67 left it: rejected by a header-only offset under
    the solve bands (no pointing_src stored)."""
    f = q.regrade_pointing(rec, off, q.thresholds(cfg, rec["rig"]), note,
                           "solve")
    rec.update(f)
    rec.pop("pointing_src", None)
    rec.pop("reviewed", None)
    rec.pop("review_source", None)
    assert rec["passed_qa"] is False and rec["drivers"] == ["pointing"]
    return rec


# ------------------------------------------------------------- the check

def test_header_60_arcmin_warns_unconfirmed(tmp_path):
    cfg = _cfg(tmp_path)
    a = pointing.assess(cfg, "rc16", pointing.from_header(MODEL_HDR), CATS[0])
    assert 55 < a["off_target_arcmin"] < 65
    assert a["flag"] == "flag"            # not "off-target": jumps the solve queue
    card = q.evaluate({"pointing_offset_arcmin": a["off_target_arcmin"],
                       "pointing_note": a["note"], "pointing_src": "header"},
                      q.context(cfg, "rc16"))
    row = next(c for c in card.checks if c.id == "pointing")
    assert row.status == q.WARN and "pointing" not in card.drivers
    assert "header only, unconfirmed (mount model error up to about 1 deg)" \
        in row.reason
    assert row.limit == pytest.approx(300.0)


def test_header_67_deg_still_fails(tmp_path):
    cfg = _cfg(tmp_path)
    a = pointing.assess(cfg, "rc16", pointing.from_header(CAL_HDR), CATS[0])
    assert a["flag"] == "off-target"
    card = q.evaluate({"pointing_offset_arcmin": a["off_target_arcmin"],
                       "pointing_src": "header"}, q.context(cfg, "rc16"))
    row = next(c for c in card.checks if c.id == "pointing")
    assert row.status == q.FAIL and card.drivers == ["pointing"]
    assert "> 5 deg, header only" in row.reason


def test_solve_20_arcmin_fails(tmp_path):
    cfg = _cfg(tmp_path)
    t = q.thresholds(cfg, "rc16")
    assert q.pointing_check(20.0, t, src="solve").status == q.FAIL
    assert q.pointing_check(20.0, t, src="header").status == q.WARN
    assert q.pointing_check(20.0, t).status == q.WARN   # unknown = unconfirmed
    rec = {"src": "solve", "mount_src": "header", "mount_ra": CATS[1],
           "mount_dec": CATS[2] + 1.0, "solved_ra": CATS[1],
           "solved_dec": CATS[2] + 20 / 60.0}
    a = pointing.assess(cfg, "rc16", rec, CATS[0])
    assert pointing.judged_src(rec) == "solve" and a["flag"] == "off-target"


def test_piggy_rc16_correlated_90_arcmin_warns(tmp_path):
    cfg = _cfg(tmp_path)
    pb = q.thresholds(cfg, "piggyback")
    rec = {"src": "rc16-correlated", "mount_ra": CATS[1],
           "mount_dec": CATS[2] + 1.5}
    a = pointing.assess(cfg, "piggyback", rec, CATS[0])
    assert a["off_target_arcmin"] == pytest.approx(90, abs=1)
    assert a["flag"] == "flag"
    c = q.pointing_check(a["off_target_arcmin"], pb, a["note"],
                         "rc16-correlated")
    assert c.status == q.WARN and "RC16 header only, unconfirmed" in c.reason
    assert q.pointing_check(90.0, pb, src="solve").status == q.FAIL
    assert q.pointing_check(400.0, pb, src="mount-log").status == q.FAIL


def test_header_reject_limit_is_configurable(tmp_path):
    t = q.thresholds(_cfg(tmp_path, pointing_header_reject_deg=0.5), "rc16")
    assert q.pointing_check(40.0, t, src="header").status == q.FAIL
    assert q.pointing_check(20.0, t, src="header").status == q.WARN


def test_config_field_present():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    f = by_env["PS_POINTING_HEADER_REJECT_DEG"]
    assert f[0] == "pointing_header_reject_deg" and f[3] == "Quality"
    assert f[4] == "float"
    assert PhotonScriptConfig(_env_file=None).pointing_header_reject_deg == 5.0
    names = [x[0] for x in _CONFIG_FIELDS]
    i = names.index("pointing_header_reject_deg")
    assert "pointing_off_target_reject_arcmin" in names[i - 4:i + 2]


# -------------------------------------------------------- re-verdict pass

def _night(cfg):
    """good-but-header-60' subs rejected by PS-67, a cal-spot sub (67 deg),
    a human-rejected header-60' sub and a human-accepted cal-spot sub."""
    from photonscript.scheduler.runs import append_sub_record
    fits_dir = Path(cfg.image_watch_dir) / NIGHT
    note = "from Cat's Eye Nebula"
    recs = []
    for i in range(2):
        f = _fits(fits_dir / f"M_{i}.fits", MODEL_HDR)
        recs.append(_legacy_header_reject(cfg, _approved_record(
            cfg, f"M_{i}.fits", CATS[0], abs_path=str(f),
            time=f"2026-09-27T04:{i * 6:02d}:00"), 61.0, note))
    f = _fits(fits_dir / "C_0.fits", CAL_HDR)
    recs.append(_legacy_header_reject(cfg, _approved_record(
        cfg, "C_0.fits", CATS[0], abs_path=str(f),
        time="2026-09-27T04:20:00"), 3960.0, note))
    f = _fits(fits_dir / "H_0.fits", MODEL_HDR)
    h = _legacy_header_reject(cfg, _approved_record(
        cfg, "H_0.fits", CATS[0], abs_path=str(f),
        time="2026-09-27T04:26:00"), 61.0, note)
    h.update(manual_qa=True, review_source="manual", reviewed=True)
    recs.append(h)
    f = _fits(fits_dir / "H_1.fits", CAL_HDR)
    recs.append(_approved_record(cfg, "H_1.fits", CATS[0], abs_path=str(f),
                                 time="2026-09-27T04:32:00", manual_qa=True,
                                 review_source="manual"))
    for r in recs:
        append_sub_record(cfg, NIGHT, r)


def test_night_pass_dry_run_then_real_run_flips_header_rejects(
        tmp_path, appcfg, monkeypatch):  # noqa: F811
    from photonscript.scheduler import pointing_record
    from photonscript.scheduler.runs import _load_subs, runs_dir
    cfg = appcfg
    monkeypatch.setattr("photonscript.scheduler.runs.sync_goal_progress",
                        lambda c: [])
    built = []
    monkeypatch.setattr("photonscript.scheduler.runs.build_library",
                        lambda c, d=None: built.append(d) or {})
    _night(cfg)
    subs_file = runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    before = subs_file.read_text(encoding="utf-8")

    dry = pointing_record.night_pass(cfg, NIGHT, solve=False, apply=False)
    assert dry["dry_run"] is True
    assert dry["un_rejected"] == 2 and dry["newly_rejected"] == 0
    assert dry["verdicts_changed"] == 2 and dry["written"] == 5
    assert subs_file.read_text(encoding="utf-8") == before     # nothing written
    assert not pointing.sidecar_path(cfg, NIGHT).exists()
    assert built == []

    out = pointing_record.night_pass(cfg, NIGHT, solve=False)
    assert out["un_rejected"] == 2 and out["newly_rejected"] == 0
    assert built == [NIGHT]
    by = {r["file"]: r for r in _load_subs(cfg, NIGHT)}
    for k in ("M_0.fits", "M_1.fits"):
        assert by[k]["passed_qa"] is True
        assert by[k]["auto_verdict"] == q.NEEDS_LOOK          # header WARN
        assert by[k]["pointing_src"] == "header"
        assert not by[k].get("reviewed")
        row = next(r for r in q.expand(by[k]["scorecard"])
                   if r["id"] == "pointing")
        assert row["status"] == q.WARN
        assert "header only, unconfirmed" in row["reason"]
    assert by["C_0.fits"]["passed_qa"] is False               # 67 deg stays
    assert by["C_0.fits"]["drivers"] == ["pointing"]
    assert by["H_0.fits"]["passed_qa"] is False               # human kept
    assert by["H_1.fits"]["passed_qa"] is True                # human kept
    pts = pointing.load(cfg, NIGHT)
    assert pts[("rc16", "M_0.fits")]["flag"] == "flag"
    assert pts[("rc16", "C_0.fits")]["flag"] == "off-target"
    again = pointing_record.night_pass(cfg, NIGHT, solve=False)
    assert again["records_updated"] == 0 and again["un_rejected"] == 0


def test_header_flag_sub_is_solved_first_and_a_solve_confirms(
        tmp_path, appcfg, monkeypatch):  # noqa: F811
    from photonscript.scheduler import pointing_record
    from photonscript.scheduler.runs import _load_subs, append_sub_record
    cfg = appcfg
    cfg.pointing_solve_every = 100
    monkeypatch.setattr("photonscript.scheduler.runs.sync_goal_progress",
                        lambda c: [])
    fits_dir = Path(cfg.image_watch_dir) / NIGHT
    for i in range(4):
        f = _fits(fits_dir / f"L_{i}.fits", MODEL_HDR if i == 2 else CATS_HDR)
        append_sub_record(cfg, NIGHT, _approved_record(
            cfg, f"L_{i}.fits", CATS[0], abs_path=str(f),
            time=f"2026-09-27T04:{i * 6:02d}:00"))
    calls = []

    def runner(path, fov, hint, radius, timeout):
        calls.append(Path(path).name)
        # the frame really is 20' north of the target: a confirmed miss
        return {"CRVAL1": str(CATS[1]), "CRVAL2": str(CATS[2] + 20 / 60.0),
                "CD1_1": "-6.6e-5", "CD1_2": "0", "CD2_1": "0",
                "CD2_2": "6.6e-5"}
    out = pointing_record.night_pass(cfg, NIGHT, solve=True, runner=runner)
    assert calls[0] == "L_2.fits"                # the header flag leads
    by = {r["file"]: r for r in _load_subs(cfg, NIGHT)}
    assert by["L_2.fits"]["passed_qa"] is False
    assert by["L_2.fits"]["pointing_src"] == "solve"
    assert out["newly_rejected"] >= 1


def test_rescore_unrejects_a_header_only_pointing_reject(
        tmp_path, appcfg, monkeypatch):  # noqa: F811
    from photonscript.scheduler.runs import (_load_subs, append_sub_record,
                                             rescore_night)
    cfg = appcfg
    monkeypatch.setattr("photonscript.scheduler.runs.sync_goal_progress",
                        lambda c: [])
    monkeypatch.setattr("photonscript.scheduler.runs.build_library",
                        lambda c, d=None: {})
    note = "from Cat's Eye Nebula"
    append_sub_record(cfg, NIGHT, _legacy_header_reject(cfg, _approved_record(
        cfg, "a.fits", CATS[0], time="2026-09-27T04:00:00",
        pointing_offset_arcmin=61.0, pointing_note=note), 61.0, note))
    append_sub_record(cfg, NIGHT, _legacy_header_reject(cfg, _approved_record(
        cfg, "b.fits", CATS[0], time="2026-09-27T04:06:00",
        pointing_offset_arcmin=3960.0, pointing_note=note), 3960.0, note))
    hum = _legacy_header_reject(cfg, _approved_record(
        cfg, "h.fits", CATS[0], time="2026-09-27T04:12:00",
        pointing_offset_arcmin=61.0, pointing_note=note), 61.0, note)
    hum.update(manual_qa=True, review_source="manual", reviewed=True)
    append_sub_record(cfg, NIGHT, hum)
    for rec in _load_subs(cfg, NIGHT):   # metrics carried on the record
        assert rec["pointing_offset_arcmin"] in (61.0, 3960.0)
    res = rescore_night(cfg, NIGHT)
    assert res["counts"].get("unrejected_pointing") == 1
    assert res["counts"].get("kept_rejected", 0) == 0
    rescore_night(cfg, NIGHT, apply=True)
    by = {r["file"]: r for r in _load_subs(cfg, NIGHT)}
    assert by["a.fits"]["passed_qa"] is True
    assert by["b.fits"]["passed_qa"] is False
    assert by["h.fits"]["passed_qa"] is False


def test_cli_pointing_backfill_dry_run_reports_flips(tmp_path, appcfg,
                                                     monkeypatch):  # noqa: F811
    from typer.testing import CliRunner
    from photonscript import cli
    from photonscript.scheduler.runs import _load_subs
    cfg = appcfg
    monkeypatch.setattr("photonscript.scheduler.runs.sync_goal_progress",
                        lambda c: [])
    monkeypatch.setattr("photonscript.scheduler.runs.build_library",
                        lambda c, d=None: {})
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    _night(cfg)
    res = CliRunner().invoke(cli.app, ["pointing-backfill", "--since",
                                       "2026-09-18", "--dry-run"])
    assert res.exit_code == 0, res.output
    text = " ".join(res.output.split())     # the console wraps lines
    assert "(dry run)" in text and "2 back from a header reject" in text
    assert "2 would flip back from a header-only reject" in text
    assert not _load_subs(cfg, NIGHT)[0]["passed_qa"]          # unchanged
    res = CliRunner().invoke(cli.app, ["pointing-backfill", "--since",
                                       "2026-09-18"])
    assert res.exit_code == 0, res.output
    assert "2 flipped back from a header-only reject" in " ".join(
        res.output.split())
    by = {r["file"]: r for r in _load_subs(cfg, NIGHT)}
    assert by["M_0.fits"]["passed_qa"] and not by["C_0.fits"]["passed_qa"]
    assert not by["H_0.fits"]["passed_qa"]
