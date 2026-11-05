"""PS-241: a failed Piggy-600 autofocus must not re-run after every sub.

2026-11-04/05: every Piggy AF from 20:52 local failed Hocus Focus HFR
validation, and NINA's time / temperature / HFR AF triggers (which measure
from the last SUCCESSFUL AF) fired again after the next sub. The Piggy AF
trigger runners now carry the back-off gate (scheduler/piggy_af_backoff.py)
and RunAutofocus Attempts 1. Fixture: NINA #2's 11-04 log, AF lines only
(GET /api/nina/log?rig=piggyback&date=2026-11-04&grep=utofocus)."""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import piggy_af_backoff as bo
from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "piggy_af_backoff"
LOG_1104 = (FIX / "nina2_2026-11-04_af.log").read_text(
    encoding="utf-8").splitlines()
_RUN_AF = "NINA.Sequencer.SequenceItem.Autofocus.RunAutofocus"
_SCRIPT = "NINA.Sequencer.SequenceItem.Utility.ExternalScript"


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", str(tmp_path))
    return PhotonScriptConfig(**kw)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _outcomes(lines, until: datetime | None = None):
    rows = [ln for ln in lines
            if until is None or (bo._line_time(ln) or until) <= until]
    return bo.parse_outcomes(reversed(rows))


# --------------------------------------------------------------------------
# the NINA #2 log
# --------------------------------------------------------------------------

def test_parse_outcomes_from_the_1104_log():
    out = _outcomes(LOG_1104)
    oks = [o for o in out if o["ok"]]
    fails = [o for o in out if not o["ok"]]
    assert [o["t"].strftime("%H:%M") for o in oks] == [
        "19:08", "20:14", "21:57", "22:48"]
    assert len(fails) == 40
    first = fails[0]
    assert first["t"] == _t("2026-11-04T20:56:40")
    # the reason is the final validation of that attempt (Hocus Focus's
    # own retry), not the first sweep's
    assert first["why"] == ("HFR regression: new focus HFR 3.45 vs 2.32 "
                            "before")


def test_parse_outcomes_stops_at_since_and_skips_cancels():
    lines = [
        "2026-11-04T20:00:00.0|INFO|FocuserMediator.cs|BroadcastSuccessfulAutoFocusRun|46|ok",
        "2026-11-04T21:00:00.0|ERROR|SequenceItem.cs|Run|263|Category: Focuser, Item: RunAutofocus - ",
        "System.OperationCanceledException: The operation was canceled.",
        "2026-11-04T22:00:00.0|ERROR|SequenceItem.cs|Run|263|Category: Focuser, Item: RunAutofocus - ",
        "NINA.Core.Model.SequenceEntityFailedException: Autofocus failed to run",
    ]
    out = bo.parse_outcomes(reversed(lines))
    assert [(o["t"].hour, o["ok"]) for o in out] == [(20, True), (22, False)]
    out = bo.parse_outcomes(reversed(lines), since=_t("2026-11-04T20:30:00"))
    assert [(o["t"].hour, o["ok"]) for o in out] == [(22, False)]


def test_episodes_group_attempts():
    f = [{"t": _t(f"2026-11-04T{hm}:00"), "ok": False}
         for hm in ("20:56", "21:02", "23:52", "23:58")]
    assert [len(e) for e in bo.episodes(f)] == [2, 2]


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def _fail(hm: str, day: str = "2026-11-04"):
    return {"t": _t(f"{day}T{hm}:00"), "ok": False, "why": "HFR regression"}


def _ok(hm: str, day: str = "2026-11-04"):
    return {"t": _t(f"{day}T{hm}:00"), "ok": True, "why": ""}


def test_no_failure_runs():
    d = bo.decide(_cfg(), now_local=_t("2026-11-04T21:00:00"),
                  outcomes=[_ok("19:08")])
    assert d["verdict"] == "RUN" and d["streak"] == 0
    d = bo.decide(_cfg(), now_local=_t("2026-11-04T21:00:00"),
                  outcomes=[_fail("20:00"), _ok("20:30")])
    assert d["verdict"] == "RUN" and d["failed_episodes"] == 1


def test_unreadable_log_runs():
    d = bo.decide(_cfg(), now_local=_t("2026-11-04T21:00:00"), outcomes=None)
    assert d["verdict"] == "RUN" and "unreadable" in d["why"]


def test_backoff_holds_then_runs_and_doubles_to_the_cap():
    cfg = _cfg()
    outs = [_ok("19:08"), _fail("20:56"), _fail("21:02")]   # one episode
    d = bo.decide(cfg, now_local=_t("2026-11-04T21:05:00"), outcomes=outs)
    assert d["verdict"] == "SKIP" and d["retry_at"] == "2026-11-04T22:02"
    assert "next try 22:02" in d["why"]
    d = bo.decide(cfg, now_local=_t("2026-11-04T22:02:00"), outcomes=outs)
    assert d["verdict"] == "RUN" and "over" in d["why"]
    outs2 = outs + [_fail("22:46")]                          # second episode
    d = bo.decide(cfg, now_local=_t("2026-11-04T23:00:00"), outcomes=outs2)
    assert d["verdict"] == "SKIP" and d["retry_at"] == "2026-11-05T00:46"
    outs4 = outs2 + [_fail("00:50", "2026-11-05"), _fail("05:00", "2026-11-05")]
    d = bo.decide(cfg, now_local=_t("2026-11-05T05:05:00"), outcomes=outs4)
    assert d["streak"] == 4 and d["retry_at"] == "2026-11-05T09:00"  # 240 cap


def test_limits_follow_config():
    cfg = _cfg(piggyback_af_backoff_min=30, piggyback_af_backoff_max_min=45)
    outs = [_fail("20:00"), _fail("21:00")]
    d = bo.decide(cfg, now_local=_t("2026-11-04T21:10:00"), outcomes=outs)
    assert d["retry_at"] == "2026-11-04T21:45"


def _lights(start: str, n: int, hfr: float, step_min: int = 3, exp: float = 180.0):
    t0 = _t(start)
    return [(t0 + timedelta(minutes=step_min * i), hfr, exp) for i in range(n)]


def test_hfr_escape_runs_when_the_subs_really_soften():
    outs = [_ok("19:08"), _fail("20:56"), _fail("21:02")]
    soft = _lights("2026-11-04T20:30:00", 6, 2.0) + \
        _lights("2026-11-04T21:05:00", 4, 2.6)
    d = bo.decide(_cfg(), now_local=_t("2026-11-04T21:20:00"), outcomes=outs,
                  trigger="hfr", lights=soft)
    assert d["verdict"] == "RUN" and "rose 30%" in d["why"]
    sharp = _lights("2026-11-04T20:30:00", 6, 2.0) + \
        _lights("2026-11-04T21:05:00", 4, 2.1)
    d = bo.decide(_cfg(), now_local=_t("2026-11-04T21:20:00"), outcomes=outs,
                  trigger="hfr", lights=sharp)
    assert d["verdict"] == "SKIP" and "steady" in d["why"]
    # too few lights since the failure: hold
    d = bo.decide(_cfg(), now_local=_t("2026-11-04T21:20:00"), outcomes=outs,
                  lights=soft[:-1])
    assert d["verdict"] == "SKIP"


def test_piggy_lights_from_sub_records():
    cfg = _cfg()
    subs = [
        {"rig": "piggyback", "file": "LIGHT/a.fits", "hfr": 2.1, "exp_s": 180,
         "end_utc": "2026-11-05T04:00:00Z"},
        {"rig": "rc16", "file": "LIGHT/b.fits", "hfr": 5.0, "exp_s": 600,
         "end_utc": "2026-11-05T04:01:00Z"},
        {"rig": "piggyback", "file": "FLAT/c.fits", "hfr": 1.0,
         "end_utc": "2026-11-05T04:02:00Z"},
        {"rig": "piggyback", "file": "LIGHT/d.fits", "hfr": None,
         "end_utc": "2026-11-05T04:03:00Z"},
    ]
    rows = bo.piggy_lights(cfg, subs)
    assert len(rows) == 1 and rows[0][1] == 2.1
    assert rows[0][0] == _t("2026-11-04T21:00:00")   # MST


# --------------------------------------------------------------------------
# backtest: the 11-04 night replayed through the gate
# --------------------------------------------------------------------------

def _blocks(lines):
    """(pre lines, [(trigger time, trigger name, block lines)]): a block runs
    from a "Starting Trigger: Autofocus..." line to the next one."""
    pre, blocks = [], []
    for ln in lines:
        if "SequenceTrigger.cs|Run|114|Starting Trigger: Autofocus" in ln:
            name = ln.split("Starting Trigger: ")[1].split(",")[0]
            trig = {"AutofocusAfterTimeTrigger": "time",
                    "AutofocusAfterTemperatureChangeTrigger": "temp",
                    "AutofocusAfterHFRIncreaseTrigger": "hfr"}[name]
            blocks.append((bo._line_time(ln), trig, [ln]))
        elif blocks:
            blocks[-1][2].append(ln)
        else:
            pre.append(ln)
    return pre, blocks


def test_backtest_1104_holds_the_loop():
    """Each trigger asks the gate with the history it would have seen; a
    SKIP drops that block's AF (it never ran)."""
    cfg = _cfg()
    pre, blocks = _blocks(LOG_1104)
    assert len(blocks) == 23
    kept = list(pre)
    verdicts = []
    for t, trig, lines in blocks:
        d = bo.decide(cfg, now_local=t, trigger=trig,
                      outcomes=bo.parse_outcomes(reversed(kept)))
        verdicts.append((t.strftime("%H:%M"), trig, d["verdict"]))
        if d["verdict"] == "RUN":
            kept += lines
    runs = [v for v in verdicts if v[2] == "RUN"]
    assert [v[0] for v in runs] == ["20:10", "20:52", "22:42", "23:49", "01:01"]
    assert len(verdicts) - len(runs) == 18
    # the 21:05 HFR re-fire and the 21:17 .. 21:52 time re-fires are held
    assert ("21:05", "hfr", "SKIP") in verdicts
    assert ("21:52", "time", "SKIP") in verdicts
    # after 01:01 (the 2nd failed episode: 120 min hold) every time and
    # temperature re-fire is held
    after = verdicts[[v[0] for v in verdicts].index("01:01") + 1:]
    assert after and all(v[2] == "SKIP" for v in after)
    assert {v[1] for v in after} == {"time", "temp"}
    # the night needed 23 AFs of ~9 min; the gate lets 5 through
    saved_min = (len(verdicts) - len(runs)) * bo.SAVED_S / 60.0
    assert saved_min >= 80


# --------------------------------------------------------------------------
# the gate call, alert, log
# --------------------------------------------------------------------------

def _gate(cfg, now_utc, outcomes, trigger="time", notify=None, lights=()):
    return asyncio.run(bo.run_gate(cfg, trigger, now=now_utc, notify=notify,
                                   outcomes=outcomes, lights=list(lights)))


class _Notify:
    def __init__(self):
        self.sent = []

    async def __call__(self, cfg, msg, **kw):
        self.sent.append((msg, kw))
        return True


def test_gate_modes_and_one_alert_per_night(tmp_path):
    outs = [_ok("19:08"), _fail("20:56"), _fail("21:02"), _fail("22:46")]
    now = _t("2026-11-05T05:50:00")          # 22:50 MST
    n = _Notify()
    out = _gate(_cfg(tmp_path), now, outs, notify=n)
    assert out["verdict"] == "SKIP" and out["mode"] == "on"
    assert len(n.sent) == 1
    msg, kw = n.sent[0]
    assert kw["title"] == bo.TITLE and kw["kind"] == "act"
    assert "failed 2 times tonight" in msg and chr(0x2014) not in msg
    out = _gate(_cfg(tmp_path), now + timedelta(minutes=3), outs, notify=n)
    assert out["verdict"] == "SKIP" and len(n.sent) == 1     # once a night
    adv = _gate(_cfg(tmp_path, piggyback_af_backoff="advise"), now, outs,
                notify=n)
    assert adv["verdict"] == "RUN" and adv["would_skip"]
    assert adv["why"].startswith("advise (would skip)")
    off = _gate(_cfg(tmp_path, piggyback_af_backoff="off"), now, outs, notify=n)
    assert off["verdict"] == "RUN" and "off" in off["why"]
    lines = bo.morning_lines(_cfg(tmp_path), "2026-11-04")
    assert lines == ["Piggy AF back-off: 2 failed AF(s); 2 triggered AF(s) "
                     "skipped (~9 min saved); advise: 1 would have skipped"]


def test_single_failure_does_not_alert(tmp_path):
    n = _Notify()
    out = _gate(_cfg(tmp_path), _t("2026-11-05T04:05:00"),
                [_ok("19:08"), _fail("20:56"), _fail("21:02")], notify=n)
    assert out["verdict"] == "SKIP" and not n.sent


def test_gate_error_runs(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(bo, "evaluate", boom)
    out = _gate(_cfg(tmp_path), _t("2026-11-05T04:05:00"), [])
    assert out["verdict"] == "RUN" and out["why"].startswith("error")


def test_read_outcomes_from_a_log_file(tmp_path):
    p = tmp_path / "nina2.log"
    p.write_text("\n".join(LOG_1104) + "\n", encoding="utf-8")
    out = bo.read_outcomes(_cfg(), _t("2026-11-05T02:40:00"), path=p)
    assert len([o for o in out if o["ok"]]) == 4
    assert bo.read_outcomes(_cfg(), _t("2026-11-05T02:40:00"),
                            path=tmp_path / "missing.log") is None


def test_router_dispatches_piggy_af(monkeypatch):
    from photonscript.scheduler.routers import reentry as router
    seen = {}

    async def fake(cfg, trigger, label):
        seen.update(trigger=trigger, label=label)
        return {"verdict": "SKIP"}
    monkeypatch.setattr(router, "_cfg", lambda: _cfg())
    monkeypatch.setattr(bo, "run_gate", fake)
    out = asyncio.run(router.api_reentry_gate(kind="piggy_af", label="Piggy-600",
                                              trigger="hfr"))
    assert out == {"verdict": "SKIP"}
    assert seen == {"trigger": "hfr", "label": "Piggy-600"}


# --------------------------------------------------------------------------
# the generated Piggy-600 sequence
# --------------------------------------------------------------------------

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _companion(**kw):
    from photonscript.scheduler.calibration import generate_piggyback_companion_json
    cfg = rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_image_lights=True,
        piggyback_focus_seed=0, **kw), "piggyback")
    return json.loads(generate_piggyback_companion_json(
        cfg, has_safety=True, with_lights=True))


def _runners(root):
    loop = next(n for n in _walk(root) if n.get("Name") == "OSC_LIGHT_LOOP")
    return {t["$type"].split(",")[0].split(".")[-1]:
            t["TriggerRunner"]["Items"]["$values"]
            for t in loop["Triggers"]["$values"]}


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "reentry-gate.cmd"
    p.write_text("@echo off\n", encoding="utf-8")
    return str(p)


def test_no_script_runner_is_one_attempt_af():
    for items in _runners(_companion(reentry_gate_script="")).values():
        assert [i["$type"].split(",")[0] for i in items] == [_RUN_AF]
        assert items[0]["Attempts"] == 1 and items[0]["ErrorBehavior"] == 0


def test_gated_runners_and_companion_lint(script):
    from photonscript.scheduler.sideload import lint_companion
    root = _companion(reentry_gate_script=script)
    runners = _runners(root)
    assert set(runners) == {"AutofocusAfterTemperatureChangeTrigger",
                            "AutofocusAfterHFRIncreaseTrigger",
                            "AutofocusAfterTimeTrigger"}
    want = {"AutofocusAfterTemperatureChangeTrigger": "temp",
            "AutofocusAfterHFRIncreaseTrigger": "hfr",
            "AutofocusAfterTimeTrigger": "time"}
    for name, items in runners.items():
        assert len(items) == 1
        box = items[0]
        assert box["Name"] == f"Piggy AF {want[name]} (back-off gate)"
        assert not box["Conditions"]["$values"] and not box["Triggers"]["$values"]
        gate, af = box["Items"]["$values"]
        assert gate["$type"].startswith(_SCRIPT)
        assert gate["Script"] == (f'"{script}" piggy_af --label="Piggy-600" '
                                  f"--trigger={want[name]}")
        assert (gate["ErrorBehavior"], gate["Attempts"]) == (1, 1)
        assert af["$type"].startswith(_RUN_AF) and af["Attempts"] == 1
    res = lint_companion(root)
    bad = [f for f in res.findings if f.level == "ERROR"]
    assert not bad, bad


def test_lint_catches_a_bad_piggy_gate(script):
    from photonscript.scheduler.sideload import lint_companion
    root = _companion(reentry_gate_script=script)
    box = next(n for n in _walk(root)
               if str(n.get("Name", "")).endswith("(back-off gate)"))
    box["Items"]["$values"][0]["ErrorBehavior"] = 0
    res = lint_companion(root)
    assert any(f.rule == "reentry-gate" and f.level == "ERROR"
               for f in res.findings)


def test_mode_off_drops_the_gate(script):
    root = _companion(reentry_gate_script=script, piggyback_af_backoff="off")
    for items in _runners(root).values():
        assert [i["$type"].split(",")[0] for i in items] == [_RUN_AF]


def test_rc16_af_runner_unchanged():
    from photonscript.scheduler.nina_sequence_json import _autofocus
    assert _autofocus()["Attempts"] == 2


def test_cli_passes_the_trigger_and_fails_open():
    from typer.testing import CliRunner
    from photonscript.cli import app
    r = CliRunner().invoke(app, ["reentry-gate", "piggy_af", "--label",
                                 "Piggy-600", "--trigger=hfr", "--url",
                                 "http://127.0.0.1:9", "--from-nina"])
    assert r.exit_code == 0 and "running the step" in r.output
