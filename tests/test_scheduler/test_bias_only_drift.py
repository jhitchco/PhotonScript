"""Bias-only capture jobs, bias-first ordering, bias drift detection and the
light vs bias-drift verdict for a dark that fails the level check.

The 2026-10-15 Piggy-600 night: 450 s darks at 294 to 298 ADU failed
"level" against the 2026-09-12 bias (256): 36 to 42 ADU over, 19 allowed.
Either light (no shutter) or the bias level drifted since 09-12. A bias set
shot now decides: a new bias near 290 explains the darks (bias drift), one
near 257 does not (light). Synthetic records / FITS in tmp dirs, fake NINA."""

import asyncio
import json

import pytest

from photonscript.scheduler import calibration_capture as cc
from photonscript.scheduler import calibration_qa as cq
from photonscript.scheduler import runs
from tests.test_scheduler.test_ps113_calibration import (  # noqa: F401 (_fast: autouse)
    D1, FakeIO, _blocks, _fast, _pb_cfg, _run, lib_set)


# --- the 10-15 numbers, judged from records -------------------------------------------

def _r(typ, date, median, i, exp=None, **kw):
    r = {"type": typ, "imagetyp": typ, "date": date, "name": f"{typ}_{date}_{i}.fits",
         "exptime": exp if exp is not None else (0.001 if typ == "BIAS" else 450.0),
         "gain": 100, "offset": 256, "xbin": 1, "instrume": "AP26CC",
         "readout": "LCG", "settemp": 0.0, "ccdtemp": 0.0, "median": median,
         "mad": 3.0, "center": median, "corners": [median] * 4, "stars": 0}
    r.update(kw)
    return r


def _store(new_bias=None, darks=(294.0, 296.0, 298.0), dark_date="2026-10-15"):
    frames = {}
    for i in range(5):
        frames[f"BIAS/2026-09-12/{i}"] = _r("BIAS", "2026-09-12", 256.0, i)
    if new_bias is not None:
        for i in range(5):
            frames[f"BIAS/2026-10-16/{i}"] = _r("BIAS", "2026-10-16", new_bias, i)
    for i, m in enumerate(darks):
        frames[f"DARK/{dark_date}/{i}"] = _r("DARK", dark_date, m, i)
    return {"frames": frames}


def _judge(store, drift_adu=8.0):
    cq.judge_all(store, rig="piggyback", tol=1.0, setpoint=0.0, drift_adu=drift_adu)
    return [r for r in store["frames"].values() if r["type"] == "DARK"]


def test_1015_darks_alone_fail_level_naming_the_bias():
    darks = _judge(_store())
    for d in darks:
        assert d["verdict"] == "fail" and d["codes"] == ["level"]
        assert d["level_verdict"] == "light"
    msg = darks[-1]["reasons"][0]
    assert msg == ("level: median 298 is 42.0 ADU over bias 256 (bias 2026-09-12, "
                   "median 256 of 5 frames); allowed 19.0 for 450 s")


def test_1015_new_bias_290_is_bias_drift_not_light():
    store = _store(new_bias=290.0)
    darks = _judge(store)
    for d in darks:
        assert d["verdict"] == "warn", d["reasons"]
        assert d["level_verdict"] == "bias drift"
        assert not any(c in cq.LIGHT_CODES for c in d["codes"])
    w = darks[-1]["warnings"][0]
    assert w.startswith("bias_drift: median 298 is 42.0 ADU over bias 256 "
                        "(bias 2026-09-12, median 256 of 5 frames)")
    assert "8.0 ADU over the newer bias 290 (bias 2026-10-16" in w
    assert "drift +34.0 ADU): bias drift, not light" in w
    row = cq.drift_for(None, "piggyback", gain=100, offset=256, readout="LCG",
                       setpoint=0.0, store=store)
    assert row["date"] == "2026-10-16" and row["prev_date"] == "2026-09-12"
    assert row["drift"] == 34.0 and row["median"] == 290.0 and row["n"] == 5


def test_1015_new_bias_257_is_light():
    store = _store(new_bias=257.0)
    darks = _judge(store)
    for d in darks:
        assert d["verdict"] == "fail" and "level" in d["codes"]
        assert d["level_verdict"] == "light"
    assert "bias 2026-09-12, median 256 of 5 frames" in darks[-1]["reasons"][0]
    row = cq.drift_for(None, "piggyback", gain=100, offset=256, store=store)
    assert row["drift"] == 1.0   # within 8 ADU: no drift, the excess is light


def test_drift_threshold_from_config():
    # a 34 ADU drift under a 40 ADU threshold is not drift: still light
    darks = _judge(_store(new_bias=290.0), drift_adu=40.0)
    assert all(d["level_verdict"] == "light" for d in darks)
    assert cq.bias_drift_adu(type("C", (), {"calibration_bias_drift_adu": 5.0})()) == 5.0
    assert cq.bias_drift_adu(object()) == cq.BIAS_DRIFT_ADU


def test_darks_after_the_new_bias_use_it_and_old_darks_keep_theirs():
    store = _store(new_bias=290.0, darks=(296.0,), dark_date="2026-10-16")
    store["frames"]["DARK/2026-09-20/0"] = _r("DARK", "2026-09-20", 266.0, 9)
    _judge(store)
    new = store["frames"]["DARK/2026-10-16/0"]
    old = store["frames"]["DARK/2026-09-20/0"]
    # tonight's dark against tonight's bias (290): 6 ADU over, clean pass
    assert new["verdict"] == "pass" and new["level_verdict"] is None
    # a September dark is judged against the September bias, not quarantined
    # as "below bias" by the drifted new one
    assert old["verdict"] == "pass" and old["level_verdict"] is None


def test_bias_drift_rows_and_settemp_match():
    store = _store(new_bias=290.0)
    # a 20 C bias session never stands in for the 0 C epoch
    for i in range(5):
        store["frames"][f"BIAS/2026-10-17/{i}"] = _r("BIAS", "2026-10-17", 400.0, i,
                                                     settemp=20.0, ccdtemp=20.0)
    darks = _judge(store)
    assert all(d["level_verdict"] == "bias drift" for d in darks)
    rows = {r["settemp"]: r for r in store["bias_drift"]}
    assert rows[0]["drift"] == 34.0 and rows[20]["drift"] is None


# --- jobs -------------------------------------------------------------------------------

class LevelIO(FakeIO):
    """FakeIO whose bias and dark levels are set per frame type."""

    def __init__(self, cfg, levels, **kw):
        super().__init__(cfg, **kw)
        self.levels = levels
        self.landed = []

    async def camera(self):
        if self.seq is not None and self.to_land:
            typ = self.to_land[0][0]
            self.frame_kw = {"level": self.levels[typ]}
            self.landed.append(typ)
        return await super().camera()


async def _go(cfg, io, *, daytime=False, state="DISARMED", **kw):
    ok, body = await cc.start_job(cfg, io.rig, armer_state_fn=lambda: state, io=io,
                                  poll_s=0.01, daytime=daytime, **kw)
    if not ok:
        return ok, body, None
    job = cc._jobs[io.rig]
    await asyncio.wait_for(job.task, 20)
    return ok, body, job


def test_bias_only_job_any_roof_by_day_no_owed_darks(tmp_path):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    lib_set(lib, "DARK", D1, 5, prefix="n", level=261.0)   # would give a day probe
    cq.backfill(cfg, "piggyback")
    cq.set_daytime_state(cfg, "piggyback", "disabled")   # day darks refused...
    io = FakeIO(cfg, roof=False)                         # ...and the roof is OPEN
    ok, body, job = _run(_go(cfg, io, daytime=True, bias=4, bias_only=True))
    assert ok, body
    assert body["darks"] == [] and body["bias"] == 4 and body["bias_only"] is True
    assert body["probe"] is None and body["light_via"] == "none"
    assert _blocks(io.seq) == [("BIAS", 0.001, 4)]
    assert "LoopWhileUnsafe" not in json.dumps(io.seq)
    assert job.state == "complete", job.events
    assert job.to_dict()["verdicts"] == {"pass": 4}


def test_bias_only_still_refused_while_armed(tmp_path):
    cfg = _pb_cfg(tmp_path)
    for st in ("ARMED", "RUNNING"):
        ok, body, _ = _run(_go(cfg, FakeIO(cfg, roof=False), state=st, bias=4,
                               bias_only=True))
        assert not ok and any(f"armer is {st}" in r for r in body["refusals"])
        assert not any("roof" in r for r in body["refusals"])
    ok, body, _ = _run(_go(cfg, FakeIO(cfg, running=True), bias=4, bias_only=True))
    assert not ok and any("running a sequence" in r for r in body["refusals"])
    ok, body, _ = _run(_go(cfg, FakeIO(cfg), bias=0, bias_only=True))
    assert not ok and "bias > 0" in body["refusals"][0]


def test_endpoint_bias_only_bodies(monkeypatch):
    from photonscript.scheduler.routers import calibration as rc
    seen = []

    async def spy(cfg, rig, **kw):
        seen.append(kw)
        return True, {"id": "x"}
    monkeypatch.setattr(cc, "start_job", spy)
    monkeypatch.setattr(rc, "_cfg", lambda: None)
    monkeypatch.setattr(rc, "_armer_state", lambda: "DISARMED")
    for body in ({"rig": "piggyback", "bias": 30, "darks": False},
                 {"rig": "piggyback", "bias": 30, "bias_only": True},
                 {"rig": "piggyback", "exposures": [], "bias": 30, "darks": False}):
        _run(rc.api_capture_job_start(body))
        assert seen[-1]["bias_only"] is True and seen[-1]["exposures"] is None
        assert seen[-1]["bias"] == 30
    # an empty exposures list alone still means the owed darks
    _run(rc.api_capture_job_start({"rig": "piggyback", "exposures": [], "bias": 30}))
    assert seen[-1]["bias_only"] is False
    res = _run(rc.api_capture_job_start({"rig": "piggyback", "exposures": [450],
                                         "bias_only": True}))
    assert res.status_code == 400


def test_bias_first_then_darks_judged_against_tonights_bias_and_drift_pushed(
        tmp_path, monkeypatch):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    lib_set(lib, "BIAS", D1, 5, prefix="b", exp=0.001, level=256.0)
    cq.backfill(cfg, "piggyback")
    pushed = []

    async def note(_cfg, msg, **k):
        pushed.append(msg)
        return True
    import photonscript.shared.pushover as po
    monkeypatch.setattr(po, "notify", note)
    # tonight the bias sits at 290 and the 300 s darks at 300: 44 ADU over
    # the old bias (light, abort) but 10 over tonight's (16 allowed)
    io = LevelIO(cfg, {"BIAS": 290.0, "DARK": 300.0})
    ok, body, job = _run(_go(cfg, io, exposures=[300.0], count=3, bias=5))
    assert ok, body
    assert _blocks(io.seq)[0] == ("BIAS", 0.001, 5)
    assert io.landed == ["BIAS"] * 5 + ["DARK"] * 3
    assert job.state == "complete", job.events
    assert job.to_dict()["verdicts"] == {"pass": 8}
    d = job.bias_drift
    assert d["exceeded"] and d["prev_date"] == D1 and 30 <= d["drift"] <= 38
    assert any("bias drift" in m and "drift +" in m for m in pushed)
    assert any("bias" in e and "drift +" in e for e in job.events)


def test_small_drift_not_pushed(tmp_path, monkeypatch):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    lib_set(lib, "BIAS", D1, 5, prefix="b", exp=0.001, level=256.0)
    cq.backfill(cfg, "piggyback")
    pushed = []

    async def note(_cfg, msg, **k):
        pushed.append(msg)
        return True
    import photonscript.shared.pushover as po
    monkeypatch.setattr(po, "notify", note)
    io = LevelIO(cfg, {"BIAS": 257.0, "DARK": 262.0}, roof=True)
    ok, body, job = _run(_go(cfg, io, bias=5, bias_only=True))
    assert ok and job.state == "complete", job.events
    assert job.bias_drift["exceeded"] is False and abs(job.bias_drift["drift"]) <= 3
    assert not any("bias drift" in m for m in pushed)


@pytest.mark.parametrize("darks", [[(120.0, 2)], []])
def test_generate_darks_json_bias_first(darks):
    from photonscript.scheduler.calibration import generate_darks_json
    from photonscript.shared.config import PhotonScriptConfig
    cfg = PhotonScriptConfig(_env_file=None)
    seq = json.loads(generate_darks_json(cfg, darks, 5)[0])
    assert _blocks(seq) == [("BIAS", 0.001, 5)] + [("DARK", e, n) for e, n in darks]


def test_cli_bias_only_body(monkeypatch):
    from typer.testing import CliRunner
    import httpx
    from photonscript.cli import app
    sent = {}

    class R:
        status_code = 200

        def json(self):
            return {"id": "j1", "darks": [], "bias": 30, "bias_only": True,
                    "estimated_minutes": 1.0, "budget_min": 240}

    def post(url, json=None, timeout=None):
        sent.update(url=url, body=json)
        return R()
    monkeypatch.setattr(httpx, "post", post)
    res = CliRunner().invoke(app, ["calibration-capture", "--rig", "piggyback",
                                   "--bias-only", "30", "--no-follow"])
    assert res.exit_code == 0, res.output
    assert sent["body"] == {"rig": "piggyback", "source": "cli", "bias": 30,
                            "darks": False, "bias_only": True}
    assert "30 bias (bias only)" in res.output
    # not on the remote CLI allowlist (hardware)
    from pathlib import Path
    toml = (Path(__file__).resolve().parents[2] / "config" / "remote_cli.toml").read_text()
    assert "calibration-capture" not in toml
