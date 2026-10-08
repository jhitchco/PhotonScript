"""PS-81: integration readiness lives in scheduler/readiness.py; the
/api/integration/readiness output is unchanged by the move."""

import asyncio

import pytest

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import readiness
from photonscript.scheduler import runs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

HEART_C = "Heart Nebula imaging (repeats while safe and up)_Container"

# What the endpoint returned before the move (computed by the app.py loop at
# 765064c on this same fixture).
EXPECTED = {
    "targets": [
        {"target": "Heart Nebula",
         "filters": {
             "Ha": {"accepted_in_library": 3, "planned": 20,
                    "exposure_s": 600.0, "flats": 12},
             "OIII": {"accepted_in_library": 1, "planned": 20,
                      "exposure_s": 600.0, "flats": 0},
             "R": {"accepted_in_library": 0, "planned": 10,
                   "exposure_s": 180.0, "flats": 5}},
         "darks_by_exposure": {"600s": 32, "180s": 0},
         "bias": 50, "lights_in_library": 4, "ready": False,
         "command": '.\\deploy\\prepare-integration.ps1 -Target "Heart Nebula"'},
        {"target": "Crescent Nebula",
         "filters": {"Ha": {"accepted_in_library": 2, "planned": 20,
                            "exposure_s": 600.0, "flats": 12}},
         "darks_by_exposure": {"600s": 32},
         "bias": 50, "lights_in_library": 2, "ready": True,
         "command": '.\\deploy\\prepare-integration.ps1 -Target "Crescent Nebula"'},
    ],
}

HEALTH = {"FLAT": {"detail": {"H": 12, "R": 5}},
          "BIAS": {"count_latest": 50}, "DARK": {"detail": {}}}


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _proj(pid, name, plans, active=True):
    return ImagingProject(
        id=pid, active=active,
        target=CelestialTarget(name=name, ra_hours=2.5, dec_degrees=61.0),
        exposure_plans=[ExposurePlan(filter_type=FilterType(f),
                                     exposure_seconds=e, count=n)
                        for f, e, n in plans])


def _fixture(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    lib = runs.library_root(cfg)
    for folder, flt, files in (
            ("Heart Nebula", "Ha", ["a", "b"]),
            (HEART_C, "Ha", ["c"]),
            (HEART_C, "OIII", ["d"]),
            ("Crescent Nebula", "Ha", ["e", "f"])):
        (lib / folder / flt).mkdir(parents=True, exist_ok=True)
        for f in files:
            (lib / folder / flt / f"{f}.fits").write_bytes(b"x")
    projects = [
        _proj("p1", "Heart Nebula", [("Ha", 600, 20), ("OIII", 600, 20),
                                     ("R", 180, 10)]),
        _proj("p2", "Crescent Nebula", [("Ha", 600, 20)]),
        _proj("p3", "Paused Galaxy", [("L", 180, 10)], active=False),
    ]
    calls = []

    def darks(c, e, **kw):
        calls.append((e, kw))
        return 32 if e == 600 else 0

    monkeypatch.setattr(cal, "calibration_health", lambda c: HEALTH)
    monkeypatch.setattr(cal, "count_matching_darks", darks)
    return cfg, lib, projects, calls


def test_endpoint_output_unchanged(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg, lib, projects, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_projects", {p.id: p for p in projects})
    out = app.api_integration_readiness()
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    assert out == {**EXPECTED, "library_dir": str(lib)}


def test_module_api_and_dark_memo(tmp_path, monkeypatch):
    cfg, lib, projects, calls = _fixture(tmp_path, monkeypatch)
    ctx = readiness.calibration_context(cfg)
    assert ctx.lib == lib and ctx.bias == 50
    assert ctx.flats == {"Ha": 12, "R": 5}
    rep = readiness.readiness_report(cfg, projects, ctx)
    assert rep == {**EXPECTED, "library_dir": str(lib)}
    # one header scan per exposure, shared across targets
    assert sorted(e for e, _ in calls) == [180.0, 600.0]
    one = readiness.target_readiness(cfg, projects[1], ctx)
    assert one == EXPECTED["targets"][1]
    assert readiness.library_lights(lib, "Heart Nebula", "Ha") == 3


def test_context_cache_and_unknown_rig(tmp_path, monkeypatch):
    cfg, *_ = _fixture(tmp_path, monkeypatch)
    a = readiness.calibration_context(cfg, max_age_s=300)
    assert readiness.calibration_context(cfg, max_age_s=300) is a
    assert readiness.calibration_context(cfg) is not a  # 0 = always rebuild
    with pytest.raises(ValueError):
        readiness.calibration_context(cfg, rig="nope")
