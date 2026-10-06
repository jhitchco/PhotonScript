"""PS-1: safety-monitor analytics from synthetic NINA logs, the
/api/safety/night endpoint, and the armer's observe-only cross-check.

The log lines mirror NINA 3.2's SafetyMonitorVM output on the scope PC
(local clock), e.g. 2026-10-05T21:40:12.4004|INFO|SafetyMonitorVM.cs|
UpdateMonitorValues|95|SafetyMonitorInfo state changed to Safe."""
import json
import os
import time
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import safety_analytics as sa
from photonscript.shared.config import PhotonScriptConfig

OFF = -6.0                                   # MDT: local = UTC - 6
W0 = datetime(2026, 10, 5, 18, 0, 0)         # 2026-10-05 12:00 local in UTC
W1 = W0 + timedelta(days=1)


def _l(ts, kind):
    """One NINA SafetyMonitorVM line at local time ts ('HH:MM:SS' on the
    night of 10-05, '+' prefix = next morning)."""
    day = "2026-10-06" if ts.startswith("+") else "2026-10-05"
    t = f"{day}T{ts.lstrip('+')}.1000"
    if kind in ("Safe", "Unsafe"):
        return (f"{t}|INFO|SafetyMonitorVM.cs|UpdateMonitorValues|95|"
                f"SafetyMonitorInfo state changed to {kind}")
    if kind == "C":
        return (f"{t}|INFO|SafetyMonitorVM.cs|Connect|175|Successfully connected "
                "Safety Monitor. Id: ASCOM.AlpacaDynamic3.SafetyMonitor Name: "
                "AARO Safety Obs 2")
    return f"{t}|INFO|SafetyMonitorVM.cs|Disconnect|221|Disconnected Safety Monitor"


def _night(rc16, pig=None, **kw):
    raw = {"rc16": sa.parse_lines(rc16)}
    if pig is not None:
        raw["piggyback"] = sa.parse_lines(pig)
    return sa.analyze(raw, W0, W1, OFF, **kw)


# -- parsing / classification ---------------------------------------------

def test_parse_ignores_other_lines_and_reads_kinds():
    lines = [_l("20:00:00", "C"), "2026-10-05T20:00:01|INFO|Other.cs|x|1|state changed to Safe",
             _l("20:05:00", "Safe"), _l("20:06:00", "Unsafe"), _l("20:07:00", "D")]
    assert [k for _t, k in sa.parse_lines(lines)] == [
        "connect", "safe", "unsafe", "disconnect"]


def test_unsafe_then_disconnect_is_a_connection_loss_not_weather():
    """NINA logs 'changed to Unsafe' a few ms before 'Disconnected' when a
    driver read fails (the 2026-09-25 storm: 500+ of these)."""
    ev = sa.classify(sa.parse_lines([
        _l("21:00:00", "Safe"),
        "2026-10-05T23:30:52.0927|INFO|SafetyMonitorVM.cs|UpdateMonitorValues|95|"
        "SafetyMonitorInfo state changed to Unsafe",
        "2026-10-05T23:30:52.1000|INFO|SafetyMonitorVM.cs|Disconnect|221|Disconnected Safety Monitor",
        "2026-10-05T23:30:53.1051|INFO|SafetyMonitorVM.cs|Disconnect|221|Disconnected Safety Monitor",
        _l("23:30:54", "C"),
        _l("23:45:00", "Unsafe")]))
    assert [k for _t, k in ev] == ["safe", "lost", "connect", "unsafe"]


def test_bare_disconnect_is_offline():
    ev = sa.classify(sa.parse_lines([_l("21:00:00", "Safe"), _l("22:00:00", "D"),
                                     _l("22:00:01", "D")]))
    assert [k for _t, k in ev] == ["safe", "offline"]


# -- one night -------------------------------------------------------------

AGREE_RC16 = [_l("20:30:00", "Safe"), _l("+03:38:11", "Unsafe"),
              _l("+05:10:32", "Safe"), _l("+05:39:48", "Unsafe")]
AGREE_PIG = [_l("20:30:01", "Safe"), _l("+03:38:11", "Unsafe"),
             _l("+05:10:33", "Safe"), _l("+05:39:48", "Unsafe")]


def test_both_ninas_agree_is_aaro_state():
    r = _night(AGREE_RC16, AGREE_PIG)
    xc = r["cross_check"]
    assert len(xc["agreed"]) == 4 and not xc["suspects"] and not xc["losses"]
    assert "seen by both NINAs" in r["verdict"]
    rc = r["rigs"]["rc16"]
    assert rc["to_unsafe"] == 2 and rc["connection_losses"] == 0
    # 20:30 local = 02:30Z; 03:38 local = 09:38Z
    states = [(s["start"][11:16], s["state"]) for s in rc["segments"]]
    assert ("02:30", "safe") in states and ("09:38", "unsafe") in states
    assert r["unsafe_onsets_local"] == ["03:38", "05:39"]


def test_one_sided_flap_is_a_suspect_and_a_flap():
    """NINA #1 reads unsafe for 90 s while NINA #2 stays connected and safe:
    a driver / connection fault on NINA #1, not weather."""
    rc16 = [_l("20:30:00", "Safe"), _l("23:00:00", "Unsafe"), _l("23:01:30", "Safe")]
    pig = [_l("20:30:00", "Safe")]
    r = _night(rc16, pig)
    sus = r["cross_check"]["suspects"]
    # the return to safe matches NINA #2, so only the unsafe read is suspect
    assert [(s["rig"], s["state"]) for s in sus] == [("rc16", "unsafe")]
    assert sus[0]["other"] == "piggyback" and sus[0]["other_state"] == "safe"
    fl = r["rigs"]["rc16"]["flaps"]
    assert len(fl) == 1 and fl[0]["state"] == "unsafe" and fl[0]["seconds"] == 90
    assert "SUSPECT" in r["verdict"]


def test_other_nina_offline_is_unverified_not_suspect():
    rc16 = [_l("20:30:00", "Safe"), _l("23:00:00", "Unsafe")]
    pig = [_l("20:30:00", "Safe"), _l("22:00:00", "D")]
    xc = _night(rc16, pig)["cross_check"]
    assert not xc["suspects"]
    assert xc["unverified"][0]["why"] == "other NINA offline"


def test_simultaneous_loss_on_both_ninas_points_upstream():
    """2026-10-02 10:42Z: both NINAs lost the monitor within 7 s (AARO's
    Alpaca server, not one driver)."""
    rc16 = [_l("20:30:00", "Safe"), _l("+04:42:57", "Unsafe"), _l("+04:42:57", "D")]
    pig = [_l("20:30:00", "Safe"), _l("+04:42:50", "Unsafe"), _l("+04:42:50", "D")]
    r = _night(rc16, pig)
    assert [x["both"] for x in r["cross_check"]["losses"]] == [True, True]
    assert r["rigs"]["rc16"]["connection_losses"] == 1
    assert r["rigs"]["rc16"]["segments"][-1]["state"] == "offline"


def test_state_after_connect_is_inferred_from_the_next_change():
    """NINA does not log the state it reads on connect: the next logged
    change to unsafe means it was safe in between."""
    rc16 = [_l("19:00:00", "C"), _l("+05:30:00", "Unsafe")]
    segs = _night(rc16)["rigs"]["rc16"]["segments"]
    first = [s for s in segs if s["start"].startswith("2026-10-06T01:00")][0]
    assert first["state"] == "safe" and first["inferred"] is True


def test_single_nina_night_says_no_cross_check():
    r = _night(AGREE_RC16)
    assert "no cross-check" in r["verdict"]
    assert all(u["why"] == "no log for the other NINA"
               for u in r["cross_check"]["unverified"])


def test_debounce_table_counts_held_off_resumes():
    """Three resumes: a 76 s safe blip, a 3.6 min one and a long one."""
    rc16 = [_l("19:00:00", "Unsafe"), _l("19:26:00", "Safe"), _l("19:29:36", "Unsafe"),
            _l("21:00:00", "Safe"), _l("+03:00:00", "Unsafe"),
            _l("+06:44:22", "Safe"), _l("+06:45:38", "Unsafe")]
    r = _night(rc16, confirm_s=120)
    rows = {d["hold_s"]: d for d in r["debounce"]}
    assert rows[120]["current"] and rows[120]["resumes"] == 3
    assert rows[60]["swallowed"] == 0
    assert rows[120]["swallowed"] == 1       # the 76 s blip
    assert rows[300]["swallowed"] == 2       # and the 3.6 min one
    assert rows[300]["safe_min_spent"] == pytest.approx((76 + 216 + 300) / 60, abs=0.1)


def test_armer_history_row(tmp_path):
    raw = {"rc16": sa.parse_lines(AGREE_RC16)}
    armer = [(datetime(2026, 10, 6, 2, 30, 20), "safe"),
             (datetime(2026, 10, 6, 9, 38, 30), "unsafe")]
    r = sa.analyze(raw, W0, W1, OFF, armer=armer)
    assert [s["state"] for s in r["armer"]["segments"]] == ["unknown", "safe", "unsafe"]


def test_summary_row_counts_reference_rig_flaps_once():
    rc16 = [_l("19:00:00", "Unsafe"), _l("19:26:00", "Safe"), _l("19:29:36", "Unsafe")]
    pig = [_l("19:00:00", "Unsafe"), _l("19:26:00", "Safe"), _l("19:29:36", "Unsafe")]
    r = _night(rc16, pig)
    r["date"] = "2026-10-05"
    row = sa.summary_row(r)
    assert row["flaps"] == 1 and row["agreed"] == 3 and row["suspects"] == 0


# -- endpoint ----------------------------------------------------------------

def _log(dir_, name, port, lines):
    p = dir_ / name
    p.write_text(f"2026-10-05T12:00:00.0000|INFO|API.cs|Start|134|starting web server, "
                 f"listening at 0.0.0.0:{port}\n" + "\n".join(lines) + "\n",
                 encoding="utf-8")
    t = time.time()
    os.utime(p, (t, t))
    return p


def test_endpoint_reads_both_ninas_logs(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from photonscript.scheduler.routers.safety import api_safety_night
    _log(tmp_path, "20261005-120000-3.2.0.9001.1-202610.log", 1888, AGREE_RC16)
    _log(tmp_path, "20261005-120100-3.2.0.9001.2-202610.log", 1889, AGREE_PIG)
    monkeypatch.setattr(app, "_config", PhotonScriptConfig(
        _env_file=None, nina_logs_dir=str(tmp_path), data_dir=str(tmp_path),
        nina_base_url="http://localhost:1888/v2/api",
        piggyback_nina_base_url="http://localhost:1889/v2/api"))
    r = api_safety_night(date="2026-10-05")
    assert r["ok"] and set(r["rigs"]) == {"rc16", "piggyback"}
    assert len(r["cross_check"]["agreed"]) == 4 and not r["notes"]
    assert api_safety_night(date="10/05")["ok"] is False


def test_endpoint_is_routed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import photonscript.scheduler.app as app
    monkeypatch.setattr(app, "_config", PhotonScriptConfig(
        _env_file=None, nina_logs_dir=str(tmp_path), data_dir=str(tmp_path)))
    client = TestClient(app.app)
    r = client.get("/api/safety/night", params={"date": "2026-10-05"}).json()
    assert r["ok"] and r["rigs"] == {} and len(r["notes"]) == 2
    s = client.get("/api/safety/summary", params={"days": 2}).json()
    assert s["nights"] == 0 and s["totals"]["suspects"] == 0


# -- armer cross-check (observe only) ----------------------------------------

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _fake_client(payload, seen):
    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            seen.append(url)
            if isinstance(payload, Exception):
                raise payload
            return _Resp(payload)
    return _C


def _armer(tmp_path, monkeypatch, payload, mode):
    import photonscript.scheduler.armer as armer_mod
    from photonscript.scheduler.armer import Armer
    a = Armer(PhotonScriptConfig(data_dir=str(tmp_path), safety_crosscheck=mode,
                                 piggyback_nina_base_url="http://pc:1889/v2/api"))
    seen, notes = [], []

    async def fake_notify(cfg_, msg, **kw):
        notes.append((kw.get("title"), msg))

    monkeypatch.setattr(armer_mod.httpx, "AsyncClient", _fake_client(payload, seen))
    monkeypatch.setattr(armer_mod, "notify", fake_notify)
    return a, seen, notes


def _events(tmp_path):
    out = []
    for p in (tmp_path / "runs").glob("*_events.jsonl"):
        out += [json.loads(x) for x in p.read_text().splitlines()]
    return out


@pytest.mark.asyncio
async def test_crosscheck_suspect_logs_and_alerts_once_a_night(tmp_path, monkeypatch):
    a, seen, notes = _armer(tmp_path, monkeypatch,
                            {"Response": {"Connected": True, "IsSafe": True}}, "alert")
    t = datetime(2026, 10, 6, 5, 0, 0)
    state0 = a.state
    assert await a._crosscheck_unsafe(t) == "suspect"
    assert seen == ["http://pc:1889/v2/api/equipment/safetymonitor/info"]
    assert await a._crosscheck_unsafe(t + timedelta(minutes=30)) == "suspect"
    assert len(notes) == 1 and "nothing was changed" in notes[0][1]
    ev = _events(tmp_path)
    assert [e["value"] for e in ev] == ["suspect", "suspect"]
    assert ev[0]["src"] == "safety" and ev[0]["kind"] == "crosscheck"
    assert ev[0]["piggyback_safe"] is True
    assert a.state == state0                 # observe only: nothing acted on


@pytest.mark.asyncio
async def test_crosscheck_log_mode_never_pushes(tmp_path, monkeypatch):
    a, _seen, notes = _armer(tmp_path, monkeypatch,
                             {"Response": {"Connected": True, "IsSafe": True}}, "log")
    assert await a._crosscheck_unsafe(datetime(2026, 10, 6, 5, 0, 0)) == "suspect"
    assert not notes and _events(tmp_path)


@pytest.mark.asyncio
async def test_crosscheck_agree_and_unverified(tmp_path, monkeypatch):
    a, _s, notes = _armer(tmp_path, monkeypatch,
                          {"Response": {"Connected": True, "IsSafe": False}}, "alert")
    assert await a._crosscheck_unsafe(datetime(2026, 10, 6, 5, 0, 0)) == "agree"
    b, _s2, notes2 = _armer(tmp_path / "b", monkeypatch, OSError("refused"), "alert")
    assert await b._crosscheck_unsafe(datetime(2026, 10, 6, 5, 0, 0)) == "unverified"
    c, _s3, _n3 = _armer(tmp_path / "c", monkeypatch,
                         {"Response": {"Connected": False, "IsSafe": True}}, "alert")
    assert await c._crosscheck_unsafe(datetime(2026, 10, 6, 5, 0, 0)) == "unverified"
    assert not notes and not notes2


@pytest.mark.asyncio
async def test_crosscheck_off_does_nothing(tmp_path, monkeypatch):
    a, seen, notes = _armer(tmp_path, monkeypatch,
                            {"Response": {"Connected": True, "IsSafe": True}}, "off")
    assert await a._crosscheck_unsafe(datetime(2026, 10, 6, 5, 0, 0)) is None
    assert not seen and not notes and not _events(tmp_path)


def test_crosscheck_config_default_and_system_field(monkeypatch):
    from photonscript.scheduler.app import _CONFIG_FIELDS
    monkeypatch.delenv("PS_SAFETY_CROSSCHECK", raising=False)
    assert PhotonScriptConfig(_env_file=None).safety_crosscheck == "log"
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_SAFETY_CROSSCHECK"][0] == "safety_crosscheck"
    assert by_env["PS_SAFETY_CROSSCHECK"][4] == "str"
