"""TheSky audit: the refraction air (2026-10-28). TheSky's refraction
temperature / pressure (and whether it refracts) through a pinned
sky6StarChart.DocumentProperty index (CANDIDATE until
`thesky-audit --discover-docprops` pins it), else the site line TheSky wrote
into TPoint's input file; judged against ~870 hPa (or NINA #1's station
pressure) and NINA #1's outside air. Read only; fakes only."""
import pytest

from photonscript.scheduler import thesky_audit as ta
from photonscript.scheduler import tpoint_files as tf
from photonscript.telescope_agent import thesky_client as tc

from tests.test_scheduler.test_ps104_thesky_audit import _cfg, _violations
from tests.test_scheduler.test_ps171_tpoint_files import _real_tree
from tests.test_scheduler.test_ps171_tpoint_files import _cfg as _tf_cfg


# ------------------------------------------------------------- the scripts

def test_docprops_scripts_are_read_only():
    js = tc.READ_ONLY_JS["docprops"]
    assert js.isascii() and not _violations(js)
    assert "SetDocumentProperty" not in js
    for i in (0, 9, 57, tc.DOCPROP_SCAN_MAX):
        assert f"'p{i}=' + g(function(){{return (sky6StarChart.DocumentProperty({i}), " \
               "sky6StarChart.DocPropOut);})" in js
    assert f"p{tc.DOCPROP_SCAN_MAX + 1}=" not in js
    one = tc.docprops_script([13, 12, 13])
    assert not _violations(one) and one.count("DocumentProperty(") == 2
    for bad in ([], [-1], [999]):
        with pytest.raises(ValueError):
            tc.docprops_script(bad)


def test_client_docprops_parses_the_reply():
    sent = []

    class C(tc.TheSkyClient):
        def run_script(self, js):
            sent.append(js)
            return "p0=31.9;p12=870.00;p13=?ERR;x=1"
    d = C("x", 1).docprops([0, 12, 13])
    assert d == {0: "31.9", 12: "870.00", 13: None}
    assert "DocumentProperty(12)" in sent[0] and "DocumentProperty(5)" not in sent[0]
    C("x", 1).docprops()
    assert sent[1] == tc.READ_ONLY_JS["docprops"]


def test_onsite_script_and_client_method_count_unchanged():
    # the scan is a separate script, not part of the on-site check list
    assert "DocumentProperty(57)" not in tc.onsite_script()


# ------------------------------------------------------------- collection

class _Sky(tc.TheSkyClient):
    def __init__(self, props=None):
        super().__init__("x", 3040)
        self.props = props or {}
        self.sent = []

    def run_script(self, js):
        self.sent.append(js)
        if js == tc.READ_ONLY_JS["ping"]:
            return "ok"
        if js == tc.READ_ONLY_JS["site"]:
            return "latitude=31.9069;longitude=109.0211;elevation_m=1300;use_computer_clock=1"
        if "DocumentProperty(" in js:
            return ";".join(f"p{i}={v}" for i, v in self.props.items())
        return "a=1"


def test_no_pin_no_extra_read():
    sky = _Sky()
    o, src = ta.collect_thesky(_cfg(), sky)
    assert "site_pressure_hpa" not in o
    assert not any("DocumentProperty(12)" in js for js in sky.sent)
    assert ta.air_pins(_cfg()) == {}


def test_pinned_indices_are_read_and_mapped():
    cfg = _cfg(thesky_docprop_temperature=11, thesky_docprop_pressure=12,
               thesky_docprop_refraction=14)
    assert ta.air_pins(cfg) == {"temperature": 11, "pressure": 12, "refraction": 14}
    sky = _Sky({11: "38.31", 12: "1013.25", 14: "1"})
    o, src = ta.collect_thesky(cfg, sky)
    assert o["site_temperature_c"] == 38.31 and o["site_pressure_hpa"] == 1013.25
    assert o["refraction_on"] is True
    assert o["site_air_indices"] == "temperature 11, pressure 12, refraction 14"
    js = [j for j in sky.sent if "DocumentProperty(12)" in j][0]
    assert not _violations(js)
    # an index that reads nothing leaves its key out
    o, _ = ta.collect_thesky(cfg, _Sky({12: "870"}))
    assert o["site_pressure_hpa"] == 870 and "site_temperature_c" not in o


def test_nina_weather_parsing():
    w = ta.nina_weather(_cfg(), lambda: {"Response": {
        "Connected": True, "Temperature": 8.4, "Pressure": 868.2, "Humidity": "NaN"}})
    assert w == {"connected": True, "temp_c": 8.4, "pressure_hpa": 868.2, "humidity": None}

    def boom():
        raise OSError("refused")
    assert ta.nina_weather(_cfg(), boom) == {}
    assert ta.nina_weather(_cfg(), lambda: "x") == {}


def test_weather_is_read_only_when_thesky_answers(tmp_path):
    seen = []

    def wx():
        seen.append(1)
        return {"Connected": True, "Temperature": 9.0, "Pressure": 866.0}
    a = ta.run_audit(_cfg(tmp_path), weather=wx, persist=False)   # TheSky refused
    assert seen == [] and a["nina_weather"] is None
    a = ta.run_audit(_cfg(tmp_path, thesky_docprop_pressure=12), client=_Sky({12: "870"}),
                     weather=wx, persist=False)
    assert seen == [1] and a["nina_weather"]["pressure_hpa"] == 866.0
    r = {x["id"]: x for x in a["rows"]}["site_pressure"]
    assert r["status"] == "pass" and r["source"] == "thesky-script"
    assert "NINA #1 station 866" in r["desired"]


# ------------------------------------------------------------- the rows

def _rows(ts=None, tpf=None, wx=None, cfg=None):
    cfg = cfg or _cfg(thesky_docprop_pressure=12, thesky_docprop_temperature=11)
    obs = {s: {} for s in ta.SOURCES}
    obs["thesky-script"] = ts or {}
    obs["tpoint-files"] = tpf or {}
    if wx is not None:
        obs["nina-weather"] = wx
    res = ta.evaluate(ta.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}


def test_desired_file_has_the_rows_and_lints_clean():
    d = ta.load_desired(_cfg())
    assert not d.get("lint"), d.get("lint")
    ids = {r["id"] for r in d["check"]}
    assert {"site_pressure", "site_temperature", "refraction"} <= ids


def test_pressure_870_passes_sea_level_warns():
    r = _rows({"site_pressure_hpa": 870.0})["site_pressure"]
    assert r["status"] == "pass" and r["current"] == "870.0 hPa"
    assert "870 +/- 30 hPa (AARO ~1300 m)" == r["desired"]
    assert "DocumentProperty(12)" in r["note"]
    for p in (1013.25, 1000.0):
        r = _rows({"site_pressure_hpa": p})["site_pressure"]
        assert r["status"] == "warn" and "sea level" in r["note"], p
        assert "over-corrected by +1" in r["note"] and "at 30 deg alt" in r["note"]
    r = _rows({"site_pressure_hpa": 940.0})["site_pressure"]
    assert r["status"] == "warn" and "+70 hPa from 870" in r["note"]


def test_pressure_target_is_the_station_when_it_reads_a_station_value():
    r = _rows({"site_pressure_hpa": 870.0}, wx={"pressure_hpa": 845.0})["site_pressure"]
    assert r["status"] == "pass" and "NINA #1 station 845" in r["desired"]
    r = _rows({"site_pressure_hpa": 900.0}, wx={"pressure_hpa": 845.0})["site_pressure"]
    assert r["status"] == "warn"
    # a sea level station reading is not the target
    r = _rows({"site_pressure_hpa": 870.0}, wx={"pressure_hpa": 1016.0})["site_pressure"]
    assert r["status"] == "pass" and "AARO" in r["desired"]
    assert "a sea level value?" in r["note"]


def test_pressure_from_the_tpoint_file_when_not_pinned():
    cfg = _cfg()
    tpf = {"site_pressure_hpa": 870.0, "site_temperature_c": 30.81,
           "site_air_file": r"TPoint\TPoint base run in.dat (2026-10-15)"}
    r = _rows({}, tpf, cfg=cfg)["site_pressure"]
    assert r["status"] == "pass" and r["source"] == "tpoint-files"
    assert r["current"] == "870.0 hPa (TPoint input file)"
    assert "TPoint base run in.dat" in r["note"] and "--discover-docprops" in r["note"]
    r = _rows({}, dict(tpf, site_pressure_hpa=1000.0), cfg=cfg)["site_pressure"]
    assert r["status"] == "warn" and "sea level" in r["note"]
    r = _rows({}, {}, cfg=cfg)["site_pressure"]
    assert r["status"] == "unknown" and "--discover-docprops" in r["note"]


def test_temperature_against_the_station():
    r = _rows({"site_temperature_c": 38.31}, wx={"temp_c": 8.0})["site_temperature"]
    assert r["status"] == "warn" and "TheSky 38.3 C vs NINA #1 station 8.0 C: +30 C" in r["note"]
    r = _rows({"site_temperature_c": 12.0}, wx={"temp_c": 8.0})["site_temperature"]
    assert r["status"] == "pass"
    r = _rows({"site_temperature_c": 12.0})["site_temperature"]
    assert r["status"] == "info" and "no live sensor reading" in r["desired"]
    # the TPoint file's value is a past night's: info only
    r = _rows({}, {"site_temperature_c": 30.81, "site_air_file": "x"}, wx={"temp_c": 8.0},
              cfg=_cfg())["site_temperature"]
    assert r["status"] == "info" and "(TPoint input file)" in r["current"]
    assert _rows({}, {}, cfg=_cfg())["site_temperature"]["status"] == "unknown"


def test_refraction_row_is_report_only():
    r = _rows({}, cfg=_cfg())["refraction"]
    assert r["status"] == "info" and r["current"] == "not read"
    assert "thesky_docprop_refraction" in r["note"]
    cfg = _cfg(thesky_docprop_refraction=14)
    r = _rows({"refraction_on": True}, cfg=cfg)["refraction"]
    assert r["status"] == "info" and r["current"] == "on"


def test_tpoint_files_expose_the_site_line(tmp_path, monkeypatch):
    root = _real_tree(tmp_path, {"TPoint base run outmod.dat": 60,
                                 "TPoint base run in.dat": 60})
    cfg = _tf_cfg(tmp_path, thesky_user_dir=str(root))
    st = tf.stats(cfg)
    assert st["data_site"] == {"temp_c": 32.56, "pressure_mb": 1000.0, "height_m": 1300.0}
    o, src = tf.audit_observed(cfg)
    assert o["site_pressure_hpa"] == 1000.0 and o["site_temperature_c"] == 32.56
    assert "TPoint base run in.dat" in o["site_air_file"]


# ------------------------------------------------------------- discovery

def test_discover_docprops_flags_candidates():
    sky = _Sky({0: "31.9069", 3: "1300", 9: "2461342.7", 11: "38.31", 12: "870.00",
                13: "870.00", 20: "2026", 40: "8.0"})
    d = ta.discover_docprops(_cfg(), client=sky, weather=lambda: {
        "Connected": True, "Temperature": 8.0, "Pressure": 866.0},
        tpoint_site={"temp_c": 38.31, "pressure_mb": 870.0, "height_m": 1300.0,
                     "file": "in.dat", "date": "2026-10-16"})
    assert d["ok"] and d["pressure_candidates"] == [12, 13]
    assert d["temperature_candidates"] == [11, 40]
    tags = {r["index"]: r["tags"] for r in d["props"]}
    assert tags[3] == ["elevation m"] and "pressure?" not in tags[3]
    assert any("same as 12" in t for t in tags[13])
    text = ta.format_discovery(d)
    assert "READ ONLY" in text and "PS_THESKY_DOCPROP_PRESSURE" in text
    assert " 12: 870.00" in text and "pressure candidates: [12, 13]" in text
    assert text.isascii()
    js = [j for j in sky.sent if "DocumentProperty(" in j]
    assert js == [tc.READ_ONLY_JS["docprops"]] and not _violations(js[0])


def test_discover_unreachable(tmp_path):
    d = ta.discover_docprops(_cfg(tmp_path))
    assert not d["ok"] and "not reachable" in d["note"]
    assert "not reachable" in ta.format_discovery(d)


def test_cli_discover_flag(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: _cfg(tmp_path))
    monkeypatch.setattr(ta, "discover_docprops", lambda c: {
        "ok": True, "props": [{"index": 12, "value": "870", "tags": ["pressure?"]}],
        "pressure_candidates": [12], "temperature_candidates": [], "weather": {},
        "tpoint_site": {}, "pins": {}})
    monkeypatch.setattr(ta, "run_audit", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no audit with --discover-docprops")))
    r = CliRunner().invoke(cli.app, ["thesky-audit", "--discover-docprops"])
    assert r.exit_code == 0, r.output
    assert "pressure candidates: [12]" in r.output


def test_new_sources_are_ascii_without_em_dashes():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for rel in ("photonscript/scheduler/thesky_audit.py",
                "photonscript/telescope_agent/thesky_client.py",
                "config/thesky/desired_rc16.toml",
                "tests/test_scheduler/test_thesky_refraction_air.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert chr(0x2014) not in src and chr(0x2013) not in src, rel
