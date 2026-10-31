"""TheSky refraction temperature sync (2026-10-28): the focuser probe's
temperature (NINA #1, else NINA #2) written into TheSky's pinned
DocumentProperty while a night runs; skipped when unpinned, under 2 C,
while slewing or without a plausible reading; the audit row against the live
sensor. Fakes only: no TheSky, no NINA."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import thesky_audit as ta
from photonscript.scheduler import thesky_temp_sync as ts
from photonscript.shared.night_events import events_path
from photonscript.telescope_agent import thesky_client as tc

from tests.test_scheduler.test_ps104_thesky_audit import _cfg, _violations

NOW = datetime(2026, 10, 29, 4, 0, 0)
N1 = "http://localhost:1888/v2/api"
N2 = "http://localhost:1889/v2/api"


@pytest.fixture(autouse=True)
def _reset():
    ts.reset()
    yield
    ts.reset()


def _getter(foc1=None, foc2=None, mount=None, boom=()):
    """getter(url) for the ninaAPI reads: focuser/info per NINA, mount/info."""
    seen = []

    def get(url):
        seen.append(url)
        for base in boom:
            if url.startswith(base):
                raise OSError("refused")
        if url == N1 + "/equipment/focuser/info":
            return {"Response": foc1 or {"Connected": False}}
        if url == N2 + "/equipment/focuser/info":
            return {"Response": foc2 or {"Connected": False}}
        if url == N1 + "/equipment/mount/info":
            return {"Response": mount or {"Connected": True, "Slewing": False}}
        raise OSError(url)
    get.seen = seen
    return get


class _Sky(tc.TheSkyClient):
    """TheSky with one temperature property; SetDocumentProperty updates it."""
    def __init__(self, props=None, slew=None, fail_write=False):
        super().__init__("x", 3040)
        self.props = dict(props or {})
        self.slew = slew if slew is not None else {"connected": "1", "slew_complete": "1"}
        self.fail_write = fail_write
        self.sent = []

    def docprops(self, indices=None):
        return {i: self.props.get(i) for i in (indices or [])}

    def slew_state(self):
        if self.slew == "boom":
            raise tc.TheSkyError("refused")
        return self.slew

    def run_script(self, js):
        self.sent.append(js)
        import re
        m = re.search(r"SetDocumentProperty\((\d+), ([-\d.]+)\)", js)
        assert m, js
        i, v = int(m.group(1)), m.group(2)
        before = self.props.get(i)
        if self.fail_write:
            return f"before={before};after={before};err=Error 5"
        self.props[i] = v
        return f"before={before};after={v};err="


def _events(cfg):
    p = events_path(cfg, "2026-10-28")
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


FOC = {"Connected": True, "Temperature": 7.84, "Position": 12000}


# ------------------------------------------------------------- the sensor

def test_focuser_reading_skips_nan_disconnected_and_implausible():
    assert ts.focuser_reading({"Response": FOC}) == 7.84
    assert ts.focuser_reading({"Connected": True, "Temperature": "NaN"}) is None
    assert ts.focuser_reading({"Connected": True, "Temperature": float("nan")}) is None
    assert ts.focuser_reading({"Connected": False, "Temperature": 8.0}) is None
    assert ts.focuser_reading({"Connected": True}) is None
    for t in (-31.0, 45.5, 120.0, -273.0):
        assert ts.focuser_reading({"Connected": True, "Temperature": t}) is None
    assert ts.focuser_reading({"Connected": True, "Temperature": -30.0}) == -30.0
    assert ts.focuser_reading("x") is None


def test_live_temperature_falls_back_to_nina2(tmp_path):
    cfg = _cfg(tmp_path)
    assert ts.live_temperature(cfg, _getter(foc1=FOC)) == {"temp_c": 7.84,
                                                          "source": "NINA #1 focuser"}
    lt = ts.live_temperature(cfg, _getter(foc1={"Connected": True, "Temperature": "NaN"},
                                          foc2={"Connected": True, "Temperature": 6.5}))
    assert lt == {"temp_c": 6.5, "source": "NINA #2 focuser"}
    lt = ts.live_temperature(cfg, _getter(foc2={"Connected": True, "Temperature": 6.5},
                                          boom=(N1,)))
    assert lt["source"] == "NINA #2 focuser"
    lt = ts.live_temperature(cfg, _getter(boom=(N1, N2)))
    assert lt["temp_c"] is None and len(lt["tried"]) == 2


# ------------------------------------------------------------- the script

def test_write_script_writes_only_the_one_property():
    js = ts.write_script(57, 7.84)
    assert js.isascii()
    assert js.count("SetDocumentProperty(") == 1
    assert "sky6StarChart.SetDocumentProperty(57, 7.84)" in js
    # the PS-104 denylist flags it only as the document property write
    assert _violations(js) == ["document property write"]
    assert "DocumentProperty(57)" in js
    for bad_i in (-1, 0, 1, 3, 9, 501):
        with pytest.raises(ValueError):
            ts.write_script(bad_i, 8.0)
    for bad_v in (float("nan"), 46.0, -31.0, "x", None):
        with pytest.raises(ValueError):
            ts.write_script(57, bad_v)


def test_read_only_client_scripts_still_never_write():
    # the write lives in thesky_temp_sync, not in the read-only client
    for name, js in tc.READ_ONLY_JS.items():
        assert "SetDocumentProperty" not in js, name


# ------------------------------------------------------------- one sync

def test_unpinned_logs_once_and_writes_nothing(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    sky = _Sky()
    caplog.set_level("INFO")
    for _ in range(3):
        r = ts.sync_once(cfg, NOW, client=sky, getter=_getter(foc1=FOC))
        assert r == {"action": "skipped", "why": "unpinned"}
    assert sky.sent == []
    ev = _events(cfg)
    assert len(ev) == 1 and ev[0]["kind"] == "thesky_temp_sync"
    assert ev[0]["value"] == "unpinned"
    assert ev[0]["detail"] == "temperature sync skipped: docprop unpinned"
    assert sum("docprop unpinned" in m for m in caplog.messages) == 1


def test_writes_when_off_by_2c_or_more(tmp_path):
    cfg = _cfg(tmp_path, thesky_docprop_temperature=57)
    sky = _Sky({57: "20.30"})
    r = ts.sync_once(cfg, NOW, client=sky, getter=_getter(foc1=FOC))
    assert r["action"] == "written", r
    assert r["thesky_c"] == 20.3 and r["after_c"] == 7.84 and r["index"] == 57
    assert len(sky.sent) == 1 and "SetDocumentProperty(57, 7.84)" in sky.sent[0]
    ev = _events(cfg)[-1]
    assert ev["value"] == "written" and ev["before_c"] == 20.3 and ev["after_c"] == 7.84
    assert ev["source"] == "NINA #1 focuser"
    assert "20.3 -> 7.8 C" in ev["detail"]


def test_small_change_is_skipped(tmp_path):
    cfg = _cfg(tmp_path, thesky_docprop_temperature=57)
    sky = _Sky({57: "9.5"})
    r = ts.sync_once(cfg, NOW, client=sky, getter=_getter(foc1=FOC))
    assert r["action"] == "skipped" and "under 2 C" in r["why"]
    assert sky.sent == [] and _events(cfg) == []


@pytest.mark.parametrize("slew,mount,word", [
    ({"connected": "1", "slew_complete": "0"}, None, "slew in progress"),
    ({"connected": "1", "slew_complete": None}, None, "IsSlewComplete"),
    ("boom", None, "not read"),
    ({"connected": "1", "slew_complete": "1"}, {"Slewing": True}, "NINA #1"),
])
def test_never_writes_while_slewing_or_unknown(tmp_path, slew, mount, word):
    cfg = _cfg(tmp_path, thesky_docprop_temperature=57)
    sky = _Sky({57: "20.3"}, slew=slew)
    r = ts.sync_once(cfg, NOW, client=sky, getter=_getter(foc1=FOC, mount=mount))
    assert r["action"] == "skipped" and word in r["why"], r
    assert sky.sent == []


def test_no_sensor_no_write(tmp_path):
    cfg = _cfg(tmp_path, thesky_docprop_temperature=57)
    sky = _Sky({57: "20.3"})
    r = ts.sync_once(cfg, NOW, client=sky,
                     getter=_getter(foc1={"Connected": True, "Temperature": 60.0}))
    assert r["action"] == "skipped" and "no live temperature" in r["why"]
    assert sky.sent == []


def test_a_wrong_pin_is_refused(tmp_path):
    # a known site property (latitude) is never written
    cfg = _cfg(tmp_path, thesky_docprop_temperature=0)
    r = ts.sync_once(cfg, NOW, client=_Sky({0: "31.9"}), getter=_getter(foc1=FOC))
    assert r["action"] == "refused"
    # the pressure index
    cfg = _cfg(tmp_path, thesky_docprop_temperature=12, thesky_docprop_pressure=12)
    r = ts.sync_once(cfg, NOW, client=_Sky({12: "870"}), getter=_getter(foc1=FOC))
    assert r["action"] == "refused" and "pressure" in r["why"]
    # an index that does not read like a temperature now
    cfg = _cfg(tmp_path, thesky_docprop_temperature=40)
    sky = _Sky({40: "873.0"})
    r = ts.sync_once(cfg, NOW, client=sky, getter=_getter(foc1=FOC))
    assert r["action"] == "refused" and "not a temperature" in r["why"]
    assert sky.sent == []
    assert {e["value"] for e in _events(cfg)} == {"refused"}


def test_failed_write_is_logged(tmp_path):
    cfg = _cfg(tmp_path, thesky_docprop_temperature=57)
    r = ts.sync_once(cfg, NOW, client=_Sky({57: "20.3"}, fail_write=True),
                     getter=_getter(foc1=FOC))
    assert r["action"] == "failed" and "Error 5" in r["why"]
    assert _events(cfg)[-1]["value"] == "failed"


def test_kill_switch(tmp_path):
    cfg = _cfg(tmp_path, thesky_docprop_temperature=57, thesky_temp_sync=False)
    sky = _Sky({57: "20.3"})
    r = ts.sync_once(cfg, NOW, client=sky, getter=_getter(foc1=FOC))
    assert r["action"] == "skipped" and sky.sent == []


def test_config_defaults():
    cfg = _cfg()
    assert cfg.thesky_temp_sync is True and cfg.thesky_temp_sync_min == 45.0
    assert cfg.thesky_docprop_temperature == -1


# ------------------------------------------------------------- the armer hook

class _Armer:
    def __init__(self, cfg, state="RUNNING"):
        self.config = cfg
        self.state = state
        self.plan = {"night_of": "2026-10-28",
                     "preconfig_utc": "2026-10-28T23:30:00Z",
                     "dusk_utc": "2026-10-29T00:40:00Z",
                     "dawn_utc": "2026-10-29T11:50:00Z"}

    def _shutdown_due_at(self):
        return datetime(2026, 10, 29, 12, 20)


def test_window_states_and_times(tmp_path):
    a = _Armer(_cfg(tmp_path))
    assert ts.in_window(a, NOW)
    assert not ts.in_window(a, datetime(2026, 10, 28, 23, 0))     # before pre-config
    assert not ts.in_window(a, datetime(2026, 10, 29, 12, 21))    # after the shutdown
    for st in ("ARMED", "WATCHING", "PAUSED_OPERATOR", "COMPLETE", "DISARMED"):
        a.state = st
        assert not ts.in_window(a, NOW), st
    a.state = "PAUSED_UNSAFE"
    assert ts.in_window(a, NOW)


def test_armer_tick_rate_limit(tmp_path):
    cfg = _cfg(tmp_path, thesky_docprop_temperature=57, thesky_temp_sync_min=45)
    a = _Armer(cfg)
    sky = _Sky({57: "20.3"})
    get = _getter(foc1=FOC)

    async def go():
        t = ts.armer_tick(a, NOW, client=sky, getter=get)
        assert t is not None
        r = await t
        assert r["action"] == "written"
        # within the interval: nothing
        assert ts.armer_tick(a, NOW + timedelta(minutes=44), client=sky, getter=get) is None
        sky.props[57] = "20.3"
        t = ts.armer_tick(a, NOW + timedelta(minutes=45), client=sky, getter=get)
        assert (await t)["action"] == "written"
        # out of the window: nothing
        a.state = "ARMED"
        ts.reset()
        assert ts.armer_tick(a, NOW, client=sky, getter=get) is None
    asyncio.run(go())
    assert len(sky.sent) == 2


def test_armer_calls_the_hook_while_running():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
           / "armer.py").read_text(encoding="utf-8")
    i = src.index("async def _tick(self):")
    body = src[i:i + 1500]
    assert 'if self.state in ("RUNNING", "PAUSED_UNSAFE"):' in body
    assert "thesky_temp_sync.armer_tick(self, now)" in body


# ------------------------------------------------------------- the audit row

def _rows(ts_obs=None, tpf=None, wx=None, live=None, cfg=None):
    cfg = cfg or _cfg(thesky_docprop_temperature=11)
    obs = {s: {} for s in ta.SOURCES}
    obs["thesky-script"] = ts_obs or {}
    obs["tpoint-files"] = tpf or {}
    if wx is not None:
        obs["nina-weather"] = wx
    if live is not None:
        obs["live-temp"] = live
    res = ta.evaluate(ta.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}


def test_row_warns_over_5c_against_the_focuser_both_values_shown():
    live = {"temp_c": 7.8, "source": "NINA #1 focuser"}
    r = _rows({"site_temperature_c": 20.3}, live=live, wx={"temp_c": 3.0})["site_temperature"]
    assert r["status"] == "warn"
    assert "TheSky 20.3 C vs NINA #1 focuser 7.8 C" in r["note"]
    assert "NINA #1 focuser 7.8 C" in r["desired"] and "within 5 C" in r["desired"]
    r = _rows({"site_temperature_c": 12.0}, live=live)["site_temperature"]
    assert r["status"] == "pass"
    r = _rows({"site_temperature_c": 13.0}, live=live)["site_temperature"]
    assert r["status"] == "warn"
    # no focuser: the station
    r = _rows({"site_temperature_c": 12.0}, wx={"temp_c": 8.0},
              live={"temp_c": None})["site_temperature"]
    assert r["status"] == "pass" and "NINA #1 station 8.0 C" in r["desired"]


def test_row_tpoint_file_stays_info_with_both_values():
    r = _rows({}, {"site_temperature_c": 20.3, "site_air_file": "x"},
              live={"temp_c": 7.8, "source": "NINA #1 focuser"},
              cfg=_cfg())["site_temperature"]
    assert r["status"] == "info" and "(TPoint input file)" in r["current"]
    assert "TheSky 20.3 C vs NINA #1 focuser 7.8 C" in r["note"]


def test_audit_reads_the_focuser_only_when_thesky_answers(tmp_path):
    from tests.test_scheduler.test_thesky_refraction_air import _Sky as _AuditSky
    get = _getter(foc1=FOC)
    a = ta.run_audit(_cfg(tmp_path), weather=lambda: {}, live_temp=get, persist=False)
    assert get.seen == [] and a["live_temp"] is None
    a = ta.run_audit(_cfg(tmp_path, thesky_docprop_temperature=11),
                     client=_AuditSky({11: "20.3"}), weather=lambda: {},
                     live_temp=get, persist=False)
    assert a["live_temp"]["temp_c"] == 7.84
    r = {x["id"]: x for x in a["rows"]}["site_temperature"]
    assert r["status"] == "warn" and "NINA #1 focuser 7.8 C" in r["note"]


def test_discovery_uses_the_focuser_as_a_temperature_reference():
    from tests.test_scheduler.test_thesky_refraction_air import _Sky as _AuditSky
    sky = _AuditSky({0: "31.9", 41: "7.84", 12: "873.0"})
    d = ta.discover_docprops(_cfg(), client=sky, weather=lambda: {},
                             tpoint_site={}, live_temp=_getter(foc1=FOC))
    assert d["ok"] and 41 in d["temperature_candidates"]
    assert "live focuser: 7.84 C (NINA #1 focuser)" in ta.format_discovery(d)
    # nothing but reads went to TheSky
    assert all("SetDocumentProperty" not in js for js in sky.sent)


def test_sources_are_ascii_without_em_dashes():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for rel in ("photonscript/scheduler/thesky_temp_sync.py",
                "tests/test_scheduler/test_thesky_temp_sync.py",
                "config/remote_cli.toml"):
        src = (root / rel).read_text(encoding="utf-8")
        assert chr(0x2014) not in src and chr(0x2013) not in src, rel
