"""PS-205: `photonscript sequence-preview [--check]`, tonight's sequence for
the current arm checked before a restart (GET /api/tonight/sequence-check).
Read only: the lint and the PS-197 dispatch gate on a guided and an
unguided fixture night, an empty night, the route and the CLI exit codes."""
import asyncio
import json
from datetime import datetime

import pytest
from typer.testing import CliRunner

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import sequence_preview as sp
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

NAMES = (("Heart Nebula", 2.55, 61.5), ("Pacman Nebula", 0.88, 56.6))


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


@pytest.fixture(autouse=True)
def _pinned(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.chdir(tmp_path)
    return cfg


def _night(guided=False, names=NAMES):
    targets = [NinaSequenceTarget(
        name=name, ra_hours=ra, dec_degrees=dec, start_guiding=guided,
        window_start_utc=datetime(2026, 11, 3, 2, 0),
        window_end_utc=datetime(2026, 11, 3, 6, 30),
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256),
                   ExposurePlan(filter_type=FilterType.OIII, exposure_seconds=300,
                                count=8, gain=200, offset=256)])
        for name, ra, dec in names]
    s = build_sequence_for_night("PhotonScript_20261102", targets)
    s.wait_until_local = "00:00:00"
    return targets, nsj.generate_nina_json(s)


def test_unguided_night_passes_and_lists_targets():
    targets, text = _night(guided=False)
    d = sp.build(text, targets, guided=False, name="n", armer_state="ARMED")
    assert d["ok"] and d["gate"]["level"] == "OK", d["gate"]
    assert d["start_guiding"] == 0 and not d["guided"]
    rows = {r["name"]: r for r in d["targets"]}
    assert set(rows) == {n for n, _, _ in NAMES}
    r = rows["Heart Nebula"]
    assert r["window_start_utc"] == "2026-11-03T02:00:00Z"
    assert [(x["filter"], x["count"], x["seconds"]) for x in r["exposures"]] == [
        ("Ha", 10, 300.0), ("OIII", 8, 300.0)]
    assert r["start_guiding_items"] == 0
    txt = sp.format_text(d, check=True)
    assert "UNGUIDED" in txt and "Heart Nebula: 02:00-06:30Z; Ha 10x300s" in txt
    assert "Dispatch gate: OK" in txt
    assert txt.isascii()


def test_guided_night_passes_with_start_guiding_in_every_target():
    targets, text = _night(guided=True)
    d = sp.build(text, targets, guided=True)
    assert d["ok"], d["gate"]
    assert d["start_guiding"] >= len(NAMES)
    assert all(r["start_guiding_items"] >= 1 for r in d["targets"])
    assert "PHD2 dithers" in d["dither"]


def test_start_guiding_on_an_unguided_arm_is_a_gate_error():
    targets, text = _night(guided=True)
    for t in targets:
        t.start_guiding = False          # the arm says unguided
    d = sp.build(text, targets, guided=False)
    assert not d["ok"] and d["gate"]["level"] == "ERROR"
    assert any("StartGuiding" in e for e in d["gate"]["errors"])
    txt = sp.format_text(d, check=True)
    assert "Dispatch gate: ERROR" in txt and "ERROR armed UNGUIDED" in txt
    # without --check the gate is not printed
    assert "Dispatch gate" not in sp.format_text(d)


def test_empty_night_and_bad_json_are_errors():
    _t, text = _night(names=())
    d = sp.build(text, [], guided=False)
    assert not d["ok"] and any("empty night" in e or "no target" in e
                               for e in d["gate"]["errors"]), d["gate"]
    d = sp.build("{nope", [], guided=False)
    assert not d["ok"] and not d["lint"]["ok"]


def test_route_builds_from_the_armed_night(monkeypatch):
    from photonscript.scheduler import app
    targets, text = _night(guided=False)
    monkeypatch.setattr(app, "_tonight_sequence_build", lambda now_mode=False: {
        "name": "PhotonScript_20261102", "content": text, "guided": False,
        "unguided_dither": True, "targets": targets})

    class _Armer:
        state = "ARMED"
    monkeypatch.setattr(app, "get_armer", lambda: _Armer())
    d = app.api_tonight_sequence_check(False)
    assert d["ok"] and d["armer_state"] == "ARMED" and d["unguided_dither"]
    assert "Direct Guider" in d["dither"]
    json.dumps(d)                        # JSON-serializable as served

    def boom(now_mode=False):
        raise RuntimeError("no store")
    monkeypatch.setattr(app, "_tonight_sequence_build", boom)
    res = app.api_tonight_sequence_check(False)
    assert res.status_code == 500 and b"no store" in res.body


def test_tonight_sequence_tuple_still_matches(monkeypatch, tmp_path):
    from photonscript.scheduler import app
    targets, text = _night()
    monkeypatch.setattr(app, "_tonight_sequence_build", lambda now_mode=False: {
        "name": "x", "content": text, "guided": False, "unguided_dither": False,
        "targets": targets})
    assert app._tonight_sequence(False) == ("x", text, False, False)


class _Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("guided_arm,check,code", [
    (True, True, 0), (False, True, 1), (False, False, 0)])
def test_cli_exit_codes(monkeypatch, guided_arm, check, code):
    from photonscript import cli
    targets, text = _night(guided=True)
    if not guided_arm:
        for t in targets:
            t.start_guiding = False
    d = sp.build(text, targets, guided=guided_arm, name="n", armer_state="ARMED")
    seen = []

    def fake_open(u, timeout=None):
        seen.append(u)
        return _Resp(d)
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    args = ["sequence-preview", "--now"] + (["--check"] if check else [])
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == code, res.output
    assert seen == ["http://127.0.0.1:8100/api/tonight/sequence-check?now_mode=true"]
    assert ("Dispatch gate" in res.output) == check


def test_cli_unreachable_exits_2(monkeypatch):
    from photonscript import cli

    def fake_open(u, timeout=None):
        raise OSError("refused")
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    res = CliRunner().invoke(cli.app, ["sequence-preview", "--check"])
    assert res.exit_code == 2 and "refused" in res.output


def test_remote_allowlist_lists_it_as_read():
    import tomllib
    from pathlib import Path
    t = tomllib.loads((Path(__file__).resolve().parents[2] / "config"
                       / "remote_cli.toml").read_text(encoding="utf-8"))
    c = t["commands"]["sequence-preview"]
    assert c["class"] == "read" and set(c["flags"]) == {"--check", "--json", "--now"}
    assert "--url" not in c["flags"] and not c.get("write_flags")
