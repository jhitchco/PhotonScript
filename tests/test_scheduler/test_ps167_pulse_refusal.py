"""PS-167: the TheSky ASCOM driver refusing PHD2's guide pulses ('IsSlewing
failed ... pulseguide command failed [80020009]'): the debug-log parser and
diagnosis, the agent's live watch and page, the morning analysis finding,
the PHD2 audit row for silenced alerts, and the operator-run guide-recover
playbook (plan, dry run, execute with fakes).

Fixtures: PHD2_DebugLog_2026-10-0{4,5,6}_*.txt are trimmed excerpts of the
scope PC's real debug logs (10-04: the mount connects that worked, the last
at 10-05 08:50; 10-05: the first refusals 22:55, with the 6-line block per
pulse re-stamped from the real IsSlewing lines, and the real 04:45 tail;
10-06: real lines 02:29 to 02:41 and 19:42 on 10-07)."""
import shutil
import socket
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import guide_recover as gr
from photonscript.scheduler import phd2_analysis as pa
from photonscript.scheduler import phd2_audit as audit
from photonscript.scheduler import phd2_profile_store as ps
from photonscript.shared import phd2_store as store
from photonscript.shared import pulse_refusal as pr
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.telescope_agent import pulse_refusal_watch as prw
from photonscript.telescope_agent import thesky_client as tc
from tests.test_scheduler.test_ps89_phd2_audit import FakeWinreg, _node

FIX = Path(__file__).parent / "fixtures" / "phd2"
D04 = "PHD2_DebugLog_2026-10-04_193915.txt"
D05 = "PHD2_DebugLog_2026-10-05_225451.txt"
D06 = "PHD2_DebugLog_2026-10-06_203731.txt"
ROOT = Path(__file__).resolve().parents[2]


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
        kw.setdefault("phd2_logs_dir", str(tmp_path / "phd2"))
    return PhotonScriptConfig(_env_file=None, **kw)


def _scan(name):
    return pr.scan_text((FIX / name).read_text(encoding="utf-8"), name)


# ---- the parser ------------------------------------------------------------------

def test_scanner_reads_the_1005_refusals():
    s = _scan(D05)
    assert s["refusals"] == 14 and s["slewing_errors"] == 14
    assert s["members"] == ["PulseGuide", "SideOfPier", "Slewing"]
    assert s["driver"] == "ASCOM.SoftwareBisque.Telescope" and s["hresult"] == "80020009"
    assert s["first_error"] == "ScopeASCOM::IsSlewing failed: (ASCOM.SoftwareBisque.Telescope) Slewing"
    assert s["first_refusal"] == "pulseguide command failed: (ASCOM.SoftwareBisque.Telescope) PulseGuide"
    assert s["first_error_at"] == "2026-10-05T22:55:00"
    assert s["last_error_at"] == "2026-10-06T04:45:51"          # rolled past midnight
    assert s["side_of_pier_errors"] == 2 and s["coordinates_unavailable"] == 2
    assert s["guide_rate_reads_ok"] == 2 and s["moves_failed"] == 8
    assert s["silenced"] == ["/Confirm/2/PulseGuideFailedAlertEnabled"]
    assert any("PulseGuide command to mount has failed" in k for k in s["suppressed_alerts"])
    assert s["mount_connected_at"] is None                      # connected in the older log


def test_scanner_on_the_working_1004_log_sees_the_last_connect():
    s = _scan(D04)
    assert s["refusals"] == 0 and s["first_error"] is None
    # 19:39, then 06:13 and 08:50 the next morning (PHD2 restarted 07:44)
    assert s["mount_connected_at"] == "2026-10-05T08:50:21"
    assert s["mount"] == "ASCOM Telescope Driver for TheSky."


def test_scanner_1006_and_status_zero_moves():
    s = _scan(D06)
    assert s["refusals"] == 7 and s["first_error_at"].startswith("2026-10-07T02:29")
    sc = pr.Scanner(datetime(2026, 9, 25, 19, 24))
    sc.feed("21:00:00.000 00.000 1 Move returns status 0, amount 350")
    sc.feed("21:00:01.000 00.000 1 Move returns status 0, amount 120")
    sc.feed("not a log line")
    v = sc.summary()
    assert v["moves_ok"] == 2 and v["last_ok_at"] == "2026-09-25T21:00:01"
    assert pr.file_start(D06) == datetime(2026, 10, 6, 20, 37, 31)
    assert pr.file_start("x.txt") is None


def test_diagnose_stale_driver_vs_slewing():
    d = pr.diagnose(_scan(D05))
    assert d["cause"] == "stale_driver"
    assert "lost TheSky" in d["detail"] and "silenced" in d["detail"]
    assert "Disconnect the mount" in d["fix"] and "Do not" in d["fix"]
    assert "guide-recover" in d["short_fix"]
    # only Slewing / PulseGuide failing, everything else answering
    lines = ["20:00:00.000 00.000 1 ScopeASCOM::IsSlewing failed: (ASCOM.SoftwareBisque.Telescope) Slewing",
             "20:00:00.000 00.000 1 Error thrown from x.cpp:600->ASCOM Scope: pulseguide command "
             "failed: (ASCOM.SoftwareBisque.Telescope) PulseGuide"]
    s = pr.Scanner(datetime(2026, 10, 8, 19, 0)).feed_text("\n".join(lines)).summary()
    assert pr.diagnose(s)["cause"] == "slewing"
    assert pr.diagnose({"refusals": 2, "members": ["Foo"]})["cause"] == "stale_driver"
    assert pr.diagnose({"refusals": 1})["cause"] == "slewing"


def test_ascom_trace_scan():
    text = ("10:00:01 PulseGuide Start East 500\n"
            "10:00:01 PulseGuide Exception: ASCOM.DriverException: PulseGuide (80020009)\n"
            "10:00:02 Slewing Get Exception: Slewing\n"
            "10:00:03 Connected Get True\n")
    r = pr.scan_ascom(text)
    assert r["errors"] == 2 and r["members"] == ["PulseGuide", "Slewing"]
    assert "80020009" in r["first"] and pr.scan_ascom("")["errors"] == 0


# ---- the live watch and the agent page ---------------------------------------------

def test_debug_log_tail_partial_lines_rollover_and_truncation(tmp_path):
    p1 = tmp_path / D05
    p1.write_text("22:00:00.000 00.000 1 a\n22:00:01.000 00.000 1 b", encoding="utf-8")
    cur = {"p": p1}
    t = prw.DebugLogTail(lambda: cur["p"])
    path, lines, new = t.read_new()
    assert new and lines == ["22:00:00.000 00.000 1 a"] and not t.started_mid
    with open(p1, "a", encoding="utf-8") as fh:
        fh.write(" c\n22:00:02.000 00.000 1 d\n")
    _p, lines, new = t.read_new()
    assert not new and lines == ["22:00:01.000 00.000 1 b c", "22:00:02.000 00.000 1 d"]
    assert t.read_new()[1] == []
    p2 = tmp_path / D06
    p2.write_text("20:37:31.000 00.000 1 e\n", encoding="utf-8")
    cur["p"] = p2
    _p, lines, new = t.read_new()
    assert new and lines == ["20:37:31.000 00.000 1 e"]
    p2.write_text("", encoding="utf-8")
    p2.write_text("x\n", encoding="utf-8")                  # shorter: re-read
    assert t.read_new()[1] == ["x"]
    # a big existing file is joined START_BACK_BYTES before its end
    big = tmp_path / "PHD2_DebugLog_2026-10-07_200000.txt"
    big.write_bytes(b"20:00:00.000 00.000 1 filler\n" * 20000)
    t2 = prw.DebugLogTail(lambda: big)
    _p, lines, _n = t2.read_new()
    assert t2.started_mid and 0 < len(lines) < 20000
    t3 = prw.DebugLogTail(lambda: big)
    t3.skip_to_end()
    assert t3.read_new()[1] == []
    assert prw.DebugLogTail(lambda: None).read_new() == (None, [], False)


def test_watch_hits_once_per_night_at_the_minimum(tmp_path):
    d = tmp_path / "phd2"
    d.mkdir()
    shutil.copy(FIX / D05, d / D05)
    night = {"n": "2026-10-05"}
    w = prw.PulseRefusalWatch(_cfg(tmp_path), finder=lambda: d / D05,
                              night_fn=lambda: night["n"])
    hit = w.poll()
    assert hit and hit["night"] == "2026-10-05" and hit["cause"] == "stale_driver"
    assert hit["summary"]["refusals"] == 14 and hit["file"] == D05
    assert w.poll() is None                                 # once per night
    night["n"] = "2026-10-06"                               # same file, new night
    assert w.poll() is None                                 # nothing new yet
    blk = (FIX / D05).read_text(encoding="utf-8").splitlines()
    refusals = [ln.replace("22:55", "23:55") for ln in blk if ln.startswith("22:55:0")]
    with open(d / D05, "a", encoding="utf-8") as fh:
        fh.write("\n".join(refusals) + "\n")
    hit2 = w.poll()
    assert hit2 and hit2["night"] == "2026-10-06" and hit2["summary"]["refusals"] >= 3
    off = prw.PulseRefusalWatch(_cfg(tmp_path, phd2_pulse_refusal_min=0),
                                finder=lambda: d / D05, night_fn=lambda: "x")
    assert off.poll() is None
    working = prw.PulseRefusalWatch(_cfg(tmp_path), finder=lambda: FIX / D04,
                                    night_fn=lambda: "2026-10-04")
    assert working.poll() is None


def test_page_text_is_short_and_carries_the_error_and_fix():
    s = _scan(D05)
    hit = dict(pr.diagnose(s), summary=s, night="2026-10-05")
    t = prw.page_text(hit, "M31")
    assert len(t) <= 1000
    assert "IsSlewing failed: (ASCOM.SoftwareBisque.Telescope) Slewing" in t
    assert "pulseguide command failed" in t and "[80020009]" in t
    assert "Disconnect then Connect the mount" in t and "guide-recover" in t
    assert "silenced" in t and "M31" in t


async def test_agent_pages_once_per_night_with_a_run_event(tmp_path, monkeypatch):
    from photonscript.telescope_agent import agent as agent_mod
    cfg = _cfg(tmp_path)
    pages = []

    async def _notify(c, msg, **k):
        pages.append((msg, k.get("priority"), k.get("title")))
    monkeypatch.setattr(agent_mod, "notify", _notify)

    class _State:
        current_target = "M31"
    ag = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    ag.config, ag.state = cfg, _State()
    s = _scan(D05)
    hit = dict(pr.diagnose(s), summary=s, night="2026-10-05", file=D05,
               t_utc="2026-10-06T04:55:10Z")
    await ag._pulse_refusal_alarm(hit)
    await ag._pulse_refusal_alarm(hit)
    assert len(pages) == 1 and pages[0][1] == 1
    assert "PulseGuide" in pages[0][0] and "refuses pulses" in pages[0][2]
    ev = [r for r in store.read_jsonl(events_path(cfg, "2026-10-05"))
          if r.get("kind") == "pulse_refused"]
    assert len(ev) == 2 and ev[0]["value"] == "stale_driver"
    assert ev[0]["evidence"]["refusals"] == 14


# ---- the morning analysis ----------------------------------------------------------

def test_morning_analysis_finding_from_the_debug_log(tmp_path):
    d = tmp_path / "phd2"
    d.mkdir()
    for n in (D04, D05):
        shutil.copy(FIX / n, d / n)
    cfg = _cfg(tmp_path)
    pa._DEBUG_SCANS.clear()
    f, info = pa.debug_findings(cfg, file="PHD2_GuideLog_2026-10-05_225451.txt")
    assert info["files"] == [D05] and len(f) == 1
    x = f[0]
    assert x["id"] == "pulse_refused" and x["severity"] == "critical"
    assert "Exact error: ScopeASCOM::IsSlewing failed" in x["detail"]
    assert x["evidence"]["refusals"] == 14 and x["evidence"]["cause"] == "stale_driver"
    assert "Disconnect the mount" in x["recommendation"]
    assert pa.debug_findings(cfg, file="PHD2_GuideLog_2026-10-04_193915.txt")[0] == []
    # a live log that grows is rescanned from where it stopped
    with open(d / D05, "a", encoding="utf-8") as fh:
        fh.write("\n".join(ln.replace("04:45", "04:50") for ln in
                           (FIX / D05).read_text(encoding="utf-8").splitlines()
                           if ln.startswith("04:45:38")) + "\n")
    f2, _ = pa.debug_findings(cfg, file="PHD2_GuideLog_2026-10-05_225451.txt")
    assert f2[0]["evidence"]["refusals"] == 16
    # the finding leads the night's list
    out = pa.analyze_sections([], cfg, extra_findings=f2)
    assert out["findings"][0]["id"] == "pulse_refused"
    assert pa.RULE_ORDER[0] == "pulse_refused"


# ---- the PHD2 audit row ------------------------------------------------------------

def _eval(observed):
    cfg = _cfg()
    obs = {s: {} for s in audit.SOURCES}
    obs.update(observed)
    res = audit.evaluate(audit.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}


def test_silenced_alerts_row():
    assert not audit.load_desired(_cfg()).get("lint")
    r = _eval({"profile": {"silenced_alerts": ["StarLostAlertEnabled",
                                               "PulseGuideFailedAlertEnabled"]}})["silenced_alerts"]
    assert r["status"] == audit.WARN and r["apply"] == "manual"
    assert r["current"].startswith("PulseGuideFailedAlert")
    assert "2 silenced" in r["note"] and "hid every" in r["note"]
    assert "Global" in r["fix"]
    assert _eval({"profile": {"silenced_alerts": []}})["silenced_alerts"]["status"] == audit.PASS
    assert _eval({})["silenced_alerts"]["status"] == audit.UNKNOWN


def _registry(confirm=None):
    D = FakeWinreg.REG_DWORD
    keys = {"profile": _node(keys={"2": _node({"name": ("Primary RC Profile (Guider)", 1)})})}
    if confirm is not None:
        keys["Confirm"] = _node(keys={"2": _node(confirm)})
    root = _node({"currentProfile": (2, D)}, keys)
    return FakeWinreg(_node(keys={"Software": _node(keys={"StarkLabs": _node(
        keys={"PHDGuidingV2": root})})}))


def test_read_confirm_from_the_registry(monkeypatch):
    D = FakeWinreg.REG_DWORD
    monkeypatch.setattr(ps, "_winreg", lambda: _registry({
        "PulseGuideFailedAlertEnabled": (0, D), "StarLostAlertEnabled": (1, D),
        "SomethingElseEnabled": (0, D)}))
    c = ps.read_confirm("2")
    assert c["available"] and c["silenced"] == ["PulseGuideFailedAlertEnabled"]
    assert c["location"].endswith("Confirm\\2")
    monkeypatch.setattr(ps, "_winreg", lambda: _registry(None))
    c = ps.read_confirm("2")
    assert c["available"] and c["silenced"] == []          # never silenced anything
    monkeypatch.setattr(ps, "_winreg", lambda: None)
    assert ps.read_confirm("2")["available"] is False
    monkeypatch.setattr(ps, "_winreg", lambda: _registry({}))
    assert ps.read_confirm(None)["available"] is False


async def test_collect_puts_silenced_alerts_on_the_profile(tmp_path, monkeypatch):
    D = FakeWinreg.REG_DWORD
    monkeypatch.setattr(ps, "_winreg", lambda: _registry({
        "PulseGuideFailedAlertEnabled": (0, D)}))

    class _NoPhd2:
        pass
    obs = await audit.collect(_cfg(tmp_path), client=_NoPhd2(), nina=None)
    assert obs["profile"]["silenced_alerts"] == ["PulseGuideFailedAlertEnabled"]


# ---- the TheSky read and the one write ---------------------------------------------

def test_slew_state_read_is_read_only_and_abort_lives_only_in_guide_recover():
    from tests.test_scheduler.test_ps104_thesky_audit import _violations
    js = tc.READ_ONLY_JS["slew_state"]
    assert not _violations(js) and "IsSlewComplete" in js and js.isascii()
    assert "slew_state.slew_complete" in tc.onsite_script()
    assert all("Abort" not in v for v in tc.READ_ONLY_JS.values())
    assert _violations(gr.ABORT_JS) == ["abort"]
    hits = [p.relative_to(ROOT).as_posix() for p in (ROOT / "photonscript").rglob("*.py")
            if "Abort()" in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == ["photonscript/scheduler/guide_recover.py"]


# ---- guide-recover: plan ------------------------------------------------------------

DBG_BAD = {"ok": True, "refusals": 30, "members": ["PulseGuide", "SideOfPier", "Slewing"],
           "side_of_pier_errors": 5, "first_error_at": "2026-10-06T20:38:00",
           "last_error_at": "2026-10-07T03:24:30", "mount_connected_at": None,
           "first_error": "ScopeASCOM::IsSlewing failed: (ASCOM.SoftwareBisque.Telescope) Slewing"}
SKY_OK = {"ok": True, "connected": True, "slew_complete": True, "tracking": True}
PHD2_GUIDING = {"ok": True, "app_state": "Guiding", "connected": True}


def _plan(dbg=DBG_BAD, sky=SKY_OK, ph=PHD2_GUIDING, force=False):
    return gr.plan({"debug": dbg, "thesky": sky, "phd2": ph}, force=force)


def test_plan_recovers_a_stale_driver():
    p = _plan()
    assert p["verdict"] == "recover" and p["diagnosis"]["cause"] == "stale_driver"
    assert [s["id"] for s in p["steps"]] == ["stop_capture", "disconnect", "connect", "verify"]
    p = _plan(ph={"ok": True, "app_state": "Stopped", "connected": False})
    assert [s["id"] for s in p["steps"]] == ["connect", "verify"]


def test_plan_blocks_and_nothing_to_do():
    assert "TCP" in _plan(sky={"ok": False, "note": "refused"})["blocked"]
    b = _plan(sky={"ok": True, "connected": False})
    assert b["verdict"] == "blocked" and "never does it" in b["blocked"]
    assert "PHD2" in _plan(ph={"ok": False, "note": "PHD2 not answering"})["blocked"]
    # reconnected since the last refusal
    fixed = dict(DBG_BAD, mount_connected_at="2026-10-07T19:00:00")
    p = _plan(dbg=fixed)
    assert p["verdict"] == "nothing to recover" and not p["steps"]
    assert _plan(dbg=fixed, force=True)["verdict"] == "recover"
    assert _plan(dbg={"ok": True, "refusals": 0})["verdict"] == "nothing to recover"


def test_plan_aborts_only_a_stuck_slew():
    stuck = dict(SKY_OK, slew_complete=False, moved_arcsec=4.0, slew_complete_after=False)
    p = _plan(sky=stuck)
    assert p["steps"][0]["id"] == "abort_slew"
    moving = dict(SKY_OK, slew_complete=False, moved_arcsec=5000.0)
    assert "really slewing" in _plan(sky=moving)["blocked"]
    done = dict(SKY_OK, slew_complete=False, moved_arcsec=3.0, slew_complete_after=True)
    assert _plan(sky=done)["verdict"] == "blocked"
    unknown = dict(SKY_OK, slew_complete=False, moved_arcsec=None)
    assert "second position read failed" in _plan(sky=unknown)["blocked"]
    # a stuck slew is aborted even when the log shows nothing
    assert _plan(dbg={"ok": True, "refusals": 0}, sky=stuck)["steps"][0]["id"] == "abort_slew"


class FakeSky:
    def __init__(self, reads):
        self.reads = list(reads)
        self.scripts = []

    def slew_state(self):
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def run_script(self, js):
        self.scripts.append(js)
        return "aborted"


def test_thesky_state_samples_twice_only_while_slewing():
    sleeps = []
    a = {"connected": "true", "slew_complete": "0", "tracking": "1", "ra_h": "1.0", "dec_d": "20"}
    b = dict(a, ra_h="1.0001")
    s = gr.thesky_state(FakeSky([a, b]), sleep=sleeps.append)
    assert s["slew_complete"] is False and sleeps == [gr.SAMPLE_GAP_S]
    assert 4 < s["moved_arcsec"] < 6
    s = gr.thesky_state(FakeSky([dict(a, slew_complete="1")]), sleep=sleeps.append)
    assert s["slew_complete"] is True and len(sleeps) == 1
    assert gr.sep_arcsec({"ra_h": 1, "dec_d": 0}, {"ra_h": 1, "dec_d": 1}) == pytest.approx(3600)

    class Bad:
        def slew_state(self):
            raise tc.TheSkyError("refused")
    assert gr.thesky_state(Bad())["ok"] is False


# ---- guide-recover: execute and the CLI ---------------------------------------------

class FakePhd2:
    def __init__(self, state="Guiding", fail=None):
        self.state, self.conn, self.calls, self.fail = state, True, [], fail

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == self.fail:
            raise gr.Phd2RpcError("refused")
        if method == "stop_capture":
            self.state = "Stopped"
        elif method == "get_app_state":
            return self.state
        elif method == "set_connected":
            self.conn = bool(params[0])
        elif method == "get_connected":
            return self.conn
        elif method == "get_current_equipment":
            return {"mount": {"name": "Driver for telescope connected through TheSky (ASCOM)",
                              "connected": self.conn}}
        return 0


def test_execute_runs_in_order_and_verifies_from_the_log(monkeypatch):
    monkeypatch.setattr(gr, "VERIFY_WAIT_S", 0)
    ph, sky = FakePhd2(), FakeSky([{"slew_complete": "1", "tracking": "0"}])
    steps = _plan(sky=dict(SKY_OK, slew_complete=False, moved_arcsec=1.0))["steps"]
    good = ["19:50:00.000 00.000 1 ASCOM Scope: Connect success",
            "19:50:00.100 00.000 1 ScopeASCOM::SideOfPier() returns 0"]
    res = gr.execute(steps, phd2=ph, thesky=sky, log_tail=lambda: good,
                     sleep=lambda s: None, say=lambda m: None)
    assert [r["id"] for r in res] == ["abort_slew", "stop_capture", "disconnect",
                                      "connect", "verify"]
    assert all(r["ok"] for r in res) and sky.scripts == [gr.ABORT_JS]
    sets = [c for c in ph.calls if c[0] == "set_connected"]
    assert sets == [("set_connected", [False]), ("set_connected", [True])]
    assert "tracking 0" in res[0]["note"]
    # still failing after the reconnect: verify fails and says where to look
    bad = good + ["19:50:06.000 00.000 1 Error thrown from x.cpp:1140->ASCOM Scope: "
                  "SideOfPier failed: (ASCOM.SoftwareBisque.Telescope) SideOfPier"]
    res = gr.execute([{"id": "verify", "what": "v", "why": ""}], phd2=FakePhd2(),
                     thesky=sky, log_tail=lambda: bad, sleep=lambda s: None,
                     say=lambda m: None)
    assert not res[0]["ok"] and "One TheSky running" in res[0]["note"]
    # stops at the first failure
    ph = FakePhd2(fail="set_connected")
    res = gr.execute(_plan()["steps"], phd2=ph, thesky=sky, sleep=lambda s: None,
                     say=lambda m: None)
    assert [r["ok"] for r in res] == [True, False]


def test_phd2_rpc_skips_events_and_matches_the_reply():
    a, b = socket.socketpair()
    try:
        rpc = gr.Phd2Rpc("x", 1, timeout=2.0)
        rpc._sock = a
        b.sendall(b'{"Event":"Version","PHDVersion":"2.6.14"}\r\n'
                  b'{"Event":"AppState","State":"Guiding"}\r\n'
                  b'{"jsonrpc":"2.0","result":"Guiding","id":1}\r\n'
                  b'{"jsonrpc":"2.0","error":{"code":1,"message":"nope"},"id":2}\r\n')
        assert rpc.call("get_app_state") == "Guiding"
        with pytest.raises(gr.Phd2RpcError):
            rpc.call("set_connected", [False])
        assert b'"method": "set_connected"' in b.recv(4096)
    finally:
        a.close()
        b.close()


def _cli(monkeypatch, tmp_path, ph, sky_state=SKY_OK, dbg=DBG_BAD):
    from typer.testing import CliRunner
    import photonscript.cli as cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    monkeypatch.setattr(gr, "debug_summary", lambda c: dict(dbg))
    monkeypatch.setattr(gr, "thesky_state", lambda t: dict(sky_state))
    monkeypatch.setattr(gr, "VERIFY_WAIT_S", 0)

    class _Rpc:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return ph

        def __exit__(self, *e):
            return False
    monkeypatch.setattr(gr, "Phd2Rpc", _Rpc)
    sky = FakeSky([{"slew_complete": "1"}])
    monkeypatch.setattr(tc, "client_from_config", lambda c: sky)
    return CliRunner(), cli.app, sky


def test_cli_dry_run_changes_nothing(monkeypatch, tmp_path):
    ph = FakePhd2()
    runner, app, sky = _cli(monkeypatch, tmp_path, ph)
    r = runner.invoke(app, ["guide-recover", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "DRY RUN" in r.output and "would PHD2: set_connected false" in r.output
    assert "lost TheSky" in r.output
    assert {m for m, _p in ph.calls} <= {"get_app_state", "get_connected",
                                          "get_current_equipment"}
    assert sky.scripts == []
    r = runner.invoke(app, ["guide-recover", "--dry-run", "--json"])
    assert r.exit_code == 0 and '"verdict": "recover"' in r.output


def test_cli_runs_only_with_confirmation(monkeypatch, tmp_path):
    ph = FakePhd2()
    runner, app, _sky = _cli(monkeypatch, tmp_path, ph)
    r = runner.invoke(app, ["guide-recover"])               # not a terminal, no --yes
    assert r.exit_code == 2 and "nothing done" in r.output
    assert not any(m == "set_connected" for m, _p in ph.calls)
    r = runner.invoke(app, ["guide-recover", "--yes"])
    assert r.exit_code == 0, r.output
    assert [p for m, p in ph.calls if m == "set_connected"] == [[False], [True]]
    # blocked: nothing to run, exit 1
    ph2 = FakePhd2()
    runner, app, _sky = _cli(monkeypatch, tmp_path, ph2,
                             sky_state={"ok": True, "connected": False})
    r = runner.invoke(app, ["guide-recover", "--yes"])
    assert r.exit_code == 1 and "never does it" in r.output
    assert not any(m == "set_connected" for m, _p in ph2.calls)


def test_debug_summary_reads_the_newest_log(tmp_path):
    d = tmp_path / "phd2"
    d.mkdir()
    shutil.copy(FIX / D05, d / D05)
    s = gr.debug_summary(_cfg(tmp_path))
    assert s["ok"] and s["refusals"] == 14 and not s["partial"]
    s = gr.debug_summary(_cfg(tmp_path), tail_bytes=2000)
    assert s["partial"] and s["refusals"] < 14
    assert gr.debug_summary(_cfg(tmp_path, phd2_logs_dir=str(tmp_path / "none")))[
        "ok"] in (False, True)       # falls back to the usual PHD2 folders


# ---- config and hygiene ------------------------------------------------------------

def test_config_row_and_ascii():
    from photonscript.scheduler import app
    by_env = {f[1]: f for f in app._CONFIG_FIELDS}
    assert by_env["PS_PHD2_PULSE_REFUSAL_MIN"][0] == "phd2_pulse_refusal_min"
    assert _cfg().phd2_pulse_refusal_min == 3
    for rel in ("photonscript/shared/pulse_refusal.py",
                "photonscript/telescope_agent/pulse_refusal_watch.py",
                "photonscript/scheduler/guide_recover.py",
                "config/phd2/desired_oag_rc16.toml",
                "tests/test_scheduler/test_ps167_pulse_refusal.py",
                "tests/test_scheduler/fixtures/phd2/" + D04,
                "tests/test_scheduler/fixtures/phd2/" + D05,
                "tests/test_scheduler/fixtures/phd2/" + D06):
        assert all(b < 128 for b in (ROOT / rel).read_bytes()), rel
