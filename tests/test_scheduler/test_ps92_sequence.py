"""PS-92 sequence side: the self-test ExternalScript slots, the lint rule,
the CLI wrapper NINA runs, and the self-test API / night summary."""
import json
from pathlib import Path

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import _exec_items, lint
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def selftest_on(tmp_path, monkeypatch):
    script = tmp_path / "phd2-selftest.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_PHD2_SELFTEST_ENABLED", "true")
    monkeypatch.setenv("PS_PHD2_SELFTEST_SCRIPT", str(script))
    return script


def _seq(guided=True, n=2, dusk=True):
    targets = [NinaSequenceTarget(
        name=name, ra_hours=ra, dec_degrees=dec, start_guiding=guided,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600,
                                count=10, gain=200, offset=256)])
        for name, ra, dec in (("Heart Nebula", 2.55, 61.5),
                              ("Cat's Eye Nebula", 17.98, 66.6))[:n]]
    s = build_sequence_for_night("PS92", targets)
    if dusk:
        s.wait_until_local = "19:30:00"
    return json.loads(nsj.generate_nina_json(s))


def _types(seq):
    return [it.get("$type", "").split(",")[0].rsplit(".", 1)[-1] for it in _exec_items(seq)]


def _scripts(seq):
    return [it for it in _exec_items(seq) if "ExternalScript" in it.get("$type", "")]


def test_slots_after_twilight_af_and_before_each_start_guiding(selftest_on):
    seq = _seq()
    sc = _scripts(seq)
    assert [s["Script"].rsplit(" ", 1)[-1] for s in sc] == ["twilight", "target", "target"]
    assert all(str(selftest_on) in s["Script"] and s["ErrorBehavior"] == 0
               and s["Attempts"] == 1 for s in sc)
    items = list(_exec_items(seq))
    t = _types(seq)
    tw = items.index(sc[0])
    assert "RunAutofocus" in t[:tw]                       # after the twilight AF
    nxt = items[tw + 1]
    assert "WaitForTime" in nxt["$type"]                  # then the imaging gate
    for s in sc[1:]:
        i = items.index(s)
        assert "SetTracking" in items[i - 1]["$type"]
        assert "StartGuiding" in items[i + 1]["$type"]
    assert t.count("StartGuiding") == 2
    r = lint(seq, guided=True)
    assert r.ok, [f"{f.rule}: {f.detail}" for f in r.findings]
    assert not [f for f in r.findings if f.rule in ("phd2-selftest", "parent-links")]


def test_absent_when_unguided_disabled_or_a_tracking_test(selftest_on, monkeypatch):
    assert _scripts(_seq(guided=False)) == []
    tt = json.loads(nsj.generate_tracking_test_json())
    assert _scripts(tt) == []
    monkeypatch.setenv("PS_PHD2_SELFTEST_ENABLED", "false")
    assert _scripts(_seq()) == []


def test_lint_rule_requires_the_script_before_start_guiding(selftest_on, monkeypatch):
    seq = _seq()

    def strip(node):
        items = (node.get("Items") or {}).get("$values")
        if isinstance(items, list):
            node["Items"]["$values"] = [it for it in items if not (
                isinstance(it, dict) and "ExternalScript" in it.get("$type", "")
                and it.get("Script", "").endswith("target"))]
            for it in node["Items"]["$values"]:
                if isinstance(it, dict):
                    strip(it)
    strip(seq)
    r = lint(seq, guided=True)
    assert not r.ok
    assert any(f.rule == "phd2-selftest" and f.level == "ERROR" and "2 StartGuiding"
               in f.detail for f in r.findings)
    # a missing script file is only a warning (NINA skips it harmlessly)
    monkeypatch.setenv("PS_PHD2_SELFTEST_SCRIPT", str(selftest_on.parent / "gone.cmd"))
    r = lint(_seq(), guided=True)
    assert r.ok and any(f.rule == "phd2-selftest" and f.level == "WARN"
                        for f in r.findings)
    # disabled: no rule at all
    monkeypatch.setenv("PS_PHD2_SELFTEST_ENABLED", "false")
    assert not [f for f in lint(seq, guided=True).findings if f.rule == "phd2-selftest"]


def test_cmd_wrapper_always_exits_zero_and_is_ascii():
    text = (REPO / "deploy" / "phd2-selftest.cmd").read_bytes()
    assert all(b < 128 for b in text)
    s = text.decode("ascii")
    assert "phd2-selftest %1 --from-nina" in s and "exit /b 0" in s
    assert PhotonScriptConfig(_env_file=None).phd2_selftest_script.endswith(
        "deploy\\phd2-selftest.cmd")


def test_cli_from_nina_never_fails(monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    r = CliRunner().invoke(cli.app, ["phd2-selftest", "twilight", "--from-nina",
                                     "--url", "http://127.0.0.1:1"])
    assert r.exit_code == 0 and "not run" in r.output
    r = CliRunner().invoke(cli.app, ["phd2-selftest", "--url", "http://127.0.0.1:1"])
    assert r.exit_code == 1


def test_selftest_api_summary_and_trend(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import phd2 as r
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "d")
    monkeypatch.setattr(app, "_config", cfg)
    p = store.selftest_path(cfg)
    for rec in (
            {"kind": "active", "night": "2026-10-01", "verdict": "PASS",
             "pier_side": "West", "directions": {"W": {"ratio": 1.02}}},
            {"kind": "active", "night": "2026-10-01", "verdict": "WARN",
             "pier_side": "East", "directions": {"W": {"ratio": 1.6}}},
            {"kind": "active", "night": "2026-10-01", "verdict": "SKIPPED"},
            {"kind": "passive", "night": "2026-10-01", "source": "post-flip",
             "verdict": "FAIL", "reasons": ["RA pulses did not move the star"]},
            {"kind": "active", "night": "2026-09-30", "verdict": "FAIL",
             "directions": {"W": {"ratio": 0.02}}}):
        store.append_jsonl(p, rec)
    s = r.api_phd2_selftest(date="2026-10-01")
    assert s["runs"] == 2 and s["verdict"] == "WARN"
    assert s["by_pier"] == {"West": "PASS", "East": "WARN"}
    assert s["passive"][0]["verdict"] == "FAIL"
    t = r.api_phd2_selftest(days=4000)
    assert [(n["night"], n["verdict"]) for n in t["nights"]] == [
        ("2026-10-01", "WARN"), ("2026-09-30", "FAIL")]
    assert t["nights"][0]["ratios"] == {"W": 1.6}
