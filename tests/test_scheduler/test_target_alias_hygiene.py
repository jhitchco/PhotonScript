"""2026-10-14 alias hygiene: '?', "*_Container" and generator loop names never
become a target name or an "also recorded as" alias.

Fixtures reproduce the live pages: Heart Nebula read "also recorded as: ? .
Heart Nebula imaging (repeats while safe and up)_Container .
OSC_LIGHTS_UNTIL_DAWN_Container . OSC_LIGHT_LOOP_Container", Andromeda
Galaxy "? . M 31"."""

import json

import pytest

from photonscript.scheduler import runs, sub_index, target_alias_report
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)
from photonscript.shared.target_names import (canonical_target,
                                              is_placeholder_name,
                                              recorded_alias)

NIGHT = "2026-10-13"
HEART_C = "Heart Nebula imaging (repeats while safe and up)_Container"
OSC_C = "OSC_LIGHT_LOOP_Container"
DAWN_C = "OSC_LIGHTS_UNTIL_DAWN_Container"


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _proj(pid, name, cid, ra, dec):
    return ImagingProject(
        id=pid, active=True, priority=50,
        target=CelestialTarget(name=name, catalog_id=cid, ra_hours=ra,
                               dec_degrees=dec, angular_size_arcmin=60.0,
                               object_type="emission nebula"),
        exposure_plans=[ExposurePlan(filter_type=FilterType("Ha"),
                                     exposure_seconds=300, count=20)])


PROJECTS = [_proj("p1", "Heart Nebula", "IC 1805", 2.55, 61.45),
            _proj("p2", "Andromeda Galaxy", "M 31", 0.712, 41.27)]


def _row(target, time, *, rig="rc16", raw=None, exp=300, name=None, ok=True,
         **kw):
    name = name or f"{rig}_{time[11:19].replace(':', '-')}.fits"
    r = {"rig": rig, "file": f"LIGHT/{name}",
         "abs_path": f"C:/NINA/{NIGHT}/LIGHT/{name}", "time": time,
         "target": target, "filter": "OSC" if rig == "piggyback" else "Ha",
         "exp_s": exp, "hfr": 2.0, "ecc": 0.4, "stars": 300,
         "passed_qa": ok, "reviewed": ok, "reason": ""}
    if raw is not None:
        r["target_raw"] = raw
    r.update(kw)
    return r


def _write(cfg, rows, date=NIGHT):
    p = runs.runs_dir(cfg) / f"{date}_subs.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def _live_like(cfg):
    """The stored shapes behind the two live pages."""
    _write(cfg, [
        _row("Heart Nebula", f"{NIGHT}T02:00:00", name="h1.fits"),
        _row("Heart Nebula", f"{NIGHT}T02:10:00", raw=HEART_C, name="h2.fits"),
        _row("Heart Nebula", f"{NIGHT}T02:01:00", rig="piggyback", raw="?",
             name="p1.fits"),
        _row("Heart Nebula", f"{NIGHT}T02:06:00", rig="piggyback", raw=OSC_C,
             name="p2.fits"),
        _row("Heart Nebula", f"{NIGHT}T02:11:00", rig="piggyback", raw=DAWN_C,
             name="p3.fits"),
        _row("Andromeda Galaxy", f"{NIGHT}T05:00:00", name="m1.fits"),
        _row("Andromeda Galaxy", f"{NIGHT}T05:01:00", rig="piggyback",
             raw="?", name="m2.fits"),
        _row("Andromeda Galaxy", f"{NIGHT}T05:06:00", rig="piggyback",
             raw="M 31", name="m3.fits"),
    ])


# --- the naming rule ----------------------------------------------------------

@pytest.mark.parametrize("name", [
    "", "?", None, "  ", HEART_C, OSC_C, DAWN_C, "Targets_Container",
    "SAFE_LOOP", "Ha until moonrise",
    "Some hand-built loop_Container",       # any *_Container, known or not
    "OSC_NEW_LOOP", "OSC_NEW_LOOP_Container",   # an unlisted UPPER_SNAKE loop
])
def test_placeholders(name):
    assert is_placeholder_name(name)


@pytest.mark.parametrize("name", ["Heart Nebula", "M 31", "IC 1805",
                                  "NGC_7000", "SH2_101", "Tracking test NGC 6934"])
def test_real_names_are_not_placeholders(name):
    assert not is_placeholder_name(name)
    assert canonical_target(name) == name


def test_unlisted_loop_never_becomes_a_target():
    assert canonical_target("OSC_NEW_LOOP_Container") is None
    assert canonical_target("LIGHTS_UNTIL_SUNRISE") is None
    # a known target spelled in that shape still resolves
    assert canonical_target("IC_1805", PROJECTS) == "Heart Nebula"


def test_recorded_alias():
    assert recorded_alias("M 31", "Andromeda Galaxy") == "M 31"
    assert recorded_alias("?", "Andromeda Galaxy") is None
    assert recorded_alias(OSC_C, "Heart Nebula") is None
    assert recorded_alias(HEART_C, "Heart Nebula") is None
    assert recorded_alias("heart nebula", "Heart Nebula") is None
    assert recorded_alias(None, "Heart Nebula") is None


# --- the target pages ---------------------------------------------------------

def test_heart_page_lists_no_container_aliases(tmp_path):
    cfg = _cfg(tmp_path)
    _live_like(cfg)
    d = sub_index.target_detail(cfg, "Heart Nebula", PROJECTS)
    assert d["totals"]["total"] == 5          # every sub still credited
    assert d["raw_names"] == []
    rows = {r["file"]: r for r in sub_index.all_rows(cfg, PROJECTS)}
    p2 = rows["LIGHT/p2.fits"]
    assert p2["target"] == "Heart Nebula" and p2["target_raw"] == "Heart Nebula"
    assert p2["target_recorded"] == OSC_C     # provenance kept


def test_m31_page_keeps_the_real_alias_only(tmp_path):
    cfg = _cfg(tmp_path)
    _live_like(cfg)
    d = sub_index.target_detail(cfg, "M 31", PROJECTS)
    assert d["target"] == "Andromeda Galaxy" and d["totals"]["total"] == 3
    assert d["raw_names"] == ["M 31"]


def test_runs_page_alias_field(tmp_path):
    cfg = _cfg(tmp_path)
    _live_like(cfg)
    subs = {s["file"]: s for s in runs.night_detail(cfg, NIGHT,
                                                    backfill=False)["subs"]}
    assert subs["LIGHT/p1.fits"]["target_alias"] is None
    assert subs["LIGHT/h2.fits"]["target_alias"] is None
    assert subs["LIGHT/p3.fits"]["target_alias"] is None
    assert subs["LIGHT/m3.fits"]["target_alias"] == "M 31"
    # read side only: the log still holds what was recorded
    assert subs["LIGHT/p2.fits"]["target_raw"] == OSC_C


# --- report / backfill --------------------------------------------------------

def _stale(cfg):
    """A night still stored under container names, plus an orphan Piggy sub."""
    return _write(cfg, [
        _row(HEART_C, f"{NIGHT}T02:00:00", name="h1.fits"),
        _row(OSC_C, f"{NIGHT}T02:02:00", rig="piggyback", name="p1.fits"),
        _row("?", f"{NIGHT}T02:03:00", rig="piggyback", name="p2.fits",
             ok=False),
        # an hour after the RC16 stopped: nothing names it
        _row(DAWN_C, f"{NIGHT}T04:00:00", rig="piggyback", name="p3.fits"),
        _row("Andromeda Galaxy", f"{NIGHT}T05:00:00", raw="M 31",
             name="m1.fits"),
        _row("Andromeda Galaxy", f"{NIGHT}T05:01:00", rig="piggyback",
             raw="?", name="m2.fits"),
    ])


def test_plan_night_reattributes_by_rc16_and_counts(tmp_path):
    cfg = _cfg(tmp_path)
    p = _stale(cfg)
    subs = runs._load_subs(cfg, NIGHT)
    r = target_alias_report.plan_night(subs, PROJECTS)
    assert r["renamed"][("rc16", HEART_C, "Heart Nebula")] == 1
    assert r["renamed"][("piggyback", OSC_C, "?")] == 1
    assert r["renamed"][("piggyback", DAWN_C, "?")] == 1
    assert r["correlated"] == {"Heart Nebula": 2}
    by = {s["file"]: s for s in subs}
    assert by["LIGHT/p1.fits"]["target"] == "Heart Nebula"
    assert by["LIGHT/p1.fits"]["target_raw"] == OSC_C
    assert by["LIGHT/p2.fits"]["target"] == "Heart Nebula"
    assert by["LIGHT/p2.fits"]["target_src"] == "rc16-correlate"
    assert by["LIGHT/p3.fits"]["target"] == "?"       # unattributed, not fake
    assert r["unattributed"] == {"piggyback": 1}
    assert r["hidden_aliases"] == {("Heart Nebula", HEART_C): 1,
                                   ("Heart Nebula", OSC_C): 1,
                                   ("Andromeda Galaxy", "?"): 1}
    assert r["in_library"] == 2      # h1 and p1 accepted, p2 rejected
    assert "OSC_LIGHT_LOOP_Container" in p.read_text(encoding="utf-8")


def test_run_dry_run_writes_nothing_and_apply_only_the_log(tmp_path):
    cfg = _cfg(tmp_path)
    p = _stale(cfg)
    before = p.read_bytes()
    lib = runs.library_root(cfg)
    r = target_alias_report.run(cfg)
    assert not r["applied"] and r["nights_scanned"] == 1
    assert r["reattributed_by_target"] == {"Heart Nebula": 3}
    assert r["hidden_aliases_by_target"] == {
        "Andromeda Galaxy": {"?": 1},
        "Heart Nebula": {HEART_C: 1, OSC_C: 1}}
    assert r["still_unattributed"] == {"piggyback": 1}
    assert p.read_bytes() == before
    lib_before = sorted(lib.rglob("*")) if lib.exists() else []

    r = target_alias_report.run(cfg, apply=True)
    assert r["applied"] and r["library_touched"] is False
    recs = {s["file"]: s for s in runs._load_subs(cfg, NIGHT)}
    assert recs["LIGHT/h1.fits"]["target"] == "Heart Nebula"
    assert recs["LIGHT/h1.fits"]["target_raw"] == HEART_C
    assert recs["LIGHT/p3.fits"]["target"] == "?"
    assert (sorted(lib.rglob("*")) if lib.exists() else []) == lib_before
    # idempotent
    assert target_alias_report.run(cfg)["reattributed"] == 0


def test_cli_target_aliases_dry_run(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    cfg = _cfg(tmp_path)
    _stale(cfg)
    monkeypatch.setattr("photonscript.shared.config.PhotonScriptConfig",
                        lambda *a, **k: cfg)
    res = CliRunner().invoke(cli.app, ["target-aliases", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["applied"] is False and out["reattributed"] == 3
