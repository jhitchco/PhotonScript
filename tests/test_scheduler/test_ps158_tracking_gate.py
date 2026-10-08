"""PS-158: NINA #2 holds each OSC light until the shared mount is tracking.

2026-10-06: 22 Piggy-600 lights were shot on a parked, non-tracking mount.
A parked mount is "still", so the PS-27 settle gate passed. The same gate
now also holds (bounded, re-checking every poll) while NINA #1 reports the
mount parked or not tracking, and still fails open on anything unknown.
"""
import json

import pytest

from photonscript.scheduler import split_guard as sg
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.mount_motion import MotionTracker

from tests.test_scheduler.test_ps27_split_guard import (  # noqa: F401
    _Clock, _agent_polls, _cfg, _companion, _events, _fake_urlopen, _gate,
    _mount, gate_on)


@pytest.fixture(autouse=True)
def _fresh():
    sg.LAST.clear()
    yield
    sg.LAST.clear()


def _parked(**kw):
    return {**_mount(parked=True), "TrackingEnabled": False, **kw}


def _still_tracker(clock, parked=False):
    tr = MotionTracker()
    if not parked:
        _agent_polls(tr, clock())
        return tr
    for k in range(12, 0, -1):    # the RC16 agent's 5 s poll, parked
        tr.observe({"ra": 37.5, "dec": 61.5, "slewing": False, "parked": True,
                    "pier": "East"}, clock() - 5 * k)
    return tr


def test_parked_mount_holds_until_it_tracks(tmp_path):
    clock = _Clock()
    cfg = _cfg(tmp_path)
    # parked for 10 min, unpark + slew, then tracking on target
    seq = ([_parked()] * 300 + [_mount(slewing=True)] * 5
           + [_mount(ra_h=0.71, dec=41.3)])
    res, reads = _gate(cfg, seq, tracker=_still_tracker(clock), clock=clock)
    assert res["verdict"] == "PASS"
    assert "parked" in res["held"] and "slewing" in res["held"]
    assert res["waited_s"] > 600          # past the 90 s settle timeout
    ev = _events(cfg)[-1]
    assert ev["kind"] == "settle_gate" and "parked" in ev["held"]


def test_not_tracking_mount_holds_then_passes(tmp_path):
    clock = _Clock()
    seq = [{**_mount(), "TrackingEnabled": False}] * 20 + [_mount()]
    res, _ = _gate(_cfg(tmp_path), seq, tracker=_still_tracker(clock),
                   clock=clock)
    assert res["verdict"] == "PASS" and res["held"] == ["not tracking"]
    assert 38 <= res["waited_s"] <= 42


def test_tracking_hold_is_bounded_then_shoots(tmp_path):
    """Never an unbounded wait: a mount that stays parked costs each light
    at most piggyback_tracking_hold_s, then the light is taken (exit 0)."""
    clock = _Clock()
    cfg = _cfg(tmp_path, piggyback_tracking_hold_s=300)
    res, reads = _gate(cfg, [_parked()],
                       tracker=_still_tracker(clock, parked=True), clock=clock)
    assert res["verdict"] == "TIMEOUT" and res["waited_s"] == 300
    assert res["held"] == ["parked"]
    assert "mount parked" in res["reason"] and "shooting anyway" in res["reason"]
    assert len(reads) <= 300 / 2 + 2      # polls every piggyback_settle_poll_s
    assert _events(cfg)[-1]["value"] == "TIMEOUT"


def test_hold_never_shorter_than_the_settle_timeout(tmp_path):
    cfg = _cfg(tmp_path, piggyback_tracking_hold_s=10)
    assert sg.tracking_hold_s(cfg) == 90.0
    assert sg.gate_bound_s(cfg) == 180.0
    cfg = _cfg(tmp_path, piggyback_tracking_hold_s=900)
    assert sg.gate_bound_s(cfg) == 990.0          # hold, then settle
    assert sg.gate_bound_s(_cfg(tmp_path, piggyback_tracking_gate=False)) == 90.0


def test_settle_after_unpark_is_bounded_too(tmp_path):
    """Parked, then a mount that never settles: hold, then at most one
    settle timeout after the unpark, then shoot. Never unbounded."""
    clock = _Clock()
    seq = [_parked()] * 100 + [_mount(slewing=True)]
    res, _ = _gate(_cfg(tmp_path), seq, tracker=_still_tracker(clock),
                   clock=clock)
    assert res["verdict"] == "TIMEOUT" and "still moving" in res["reason"]
    assert 198 + 90 - 2 <= res["waited_s"] <= 198 + 90 + 2


@pytest.mark.parametrize("raw", [
    {"TrackingEnabled": True},                       # tracking
    {},                                              # driver says nothing
    {"AtPark": False},                               # tracking flag missing
    {"AtPark": True, "Connected": False},            # NINA #1 lost the mount
    {"TrackingEnabled": False, "Connected": False},
    None,
])
def test_only_explicit_flags_hold(raw):
    assert sg.mount_not_tracking(raw) is None


def test_explicit_flags():
    assert sg.mount_not_tracking({"AtPark": True}) == "parked"
    assert sg.mount_not_tracking({"AtPark": False,
                                  "TrackingEnabled": False}) == "not tracking"
    assert sg.mount_not_tracking({"Tracking": False}) == "not tracking"  # v1


def test_unknown_tracking_fails_open(tmp_path):
    clock = _Clock()
    m = _mount()
    m.pop("TrackingEnabled")
    res, _ = _gate(_cfg(tmp_path), [m], tracker=_still_tracker(clock),
                   clock=clock)
    assert res["verdict"] == "PASS" and res["waited_s"] == 0


def test_tracking_gate_off_is_the_old_gate(tmp_path):
    clock = _Clock()
    res, _ = _gate(_cfg(tmp_path, piggyback_tracking_gate=False), [_parked()],
                   tracker=_still_tracker(clock, parked=True), clock=clock)
    assert res["verdict"] == "PASS" and res["waited_s"] == 0   # PS-27 as was


def test_unreadable_nina1_still_fails_open(tmp_path):
    res, reads = _gate(_cfg(tmp_path), [None])
    assert res["verdict"] == "UNKNOWN" and len(reads) == 3


def test_split_summary_counts_tracking_holds(tmp_path):
    clock = _Clock()
    cfg = _cfg(tmp_path, piggyback_tracking_hold_s=120)
    _gate(cfg, [_parked()], tracker=_still_tracker(clock, parked=True),
          clock=clock)
    ev = _events(cfg)
    night = sg.night_split_summary(cfg, _night(cfg, ev), subs=[])
    assert night["tracking_holds"] == 1 and night["timeouts"] == 1
    assert night["saves"] == 0            # a parked hold is not a split save


def _night(cfg, ev):
    from photonscript.shared.phd2_store import night_of, parse_z
    return night_of(cfg, parse_z(ev[-1]["t"]))


def test_cli_waits_for_the_tracking_hold(monkeypatch):
    """The CLI's HTTP timeout covers the longest hold, so NINA #2's
    ExternalScript is never cut off mid-hold (and still exits 0)."""
    import io
    import urllib.request

    from typer.testing import CliRunner

    from photonscript import cli
    seen = {}

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        seen["timeout"] = timeout
        return _R(json.dumps({"verdict": "TIMEOUT", "waited_s": 900,
                              "reason": "mount parked"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    r = CliRunner().invoke(cli.app, ["settle-gate", "--from-nina"])
    assert r.exit_code == 0 and "TIMEOUT" in r.output
    assert seen["timeout"] >= 900 + 90 + 30


def test_companion_unchanged_and_lint_clean(gate_on):
    """The hold lives in the existing settle-gate ExternalScript: the OSC
    light loop is item-for-item the PS-27 one, so the PS-149 loop-spin and
    PS-154 cooling rules stay clean."""
    seq = _companion()
    rules = {f.rule for f in lint(seq, setpoint=-5.0).findings
             if f.level == "ERROR"}
    assert not rules & {"settle-gate", "loop-spin", "cooling", "parent-links"}


def test_config_defaults_and_system_fields(monkeypatch):
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.piggyback_tracking_gate is True
    assert c.piggyback_tracking_hold_s == 900.0
    by_attr = {f[0]: f for f in _CONFIG_FIELDS}
    for k in ("piggyback_tracking_gate", "piggyback_tracking_hold_s"):
        assert by_attr[k][1] == "PS_" + k.upper() and by_attr[k][3] == "Piggyback"


def test_status_endpoint_reports_the_tracking_gate(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import split_guard as r
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    s = r.api_split_guard_status()
    assert s["tracking_gate"] is True and s["tracking_hold_s"] == 900.0
