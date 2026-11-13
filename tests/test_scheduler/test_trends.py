"""Cross-night trend / systematic-drift detector."""
import json

import pytest

import photonscript.scheduler.trends as trends
from photonscript.shared.config import PhotonScriptConfig


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """PS-95: analyze_trends also reads <data_dir>/optics; never the real one."""
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path / "default-data"))


def _subs(n, *, ecc, pa, rad, hfr=2.5, nights=4):
    return [{"rig": "rc16", "ecc": ecc, "ecc_pa_R": pa,
             "ecc_radial_frac": rad, "hfr": hfr,
             "_night": f"2026-09-{10 + (i % nights):02d}"} for i in range(n)]


def _patch(monkeypatch, subs):
    monkeypatch.setattr(trends, "_recent_light_subs", lambda c, n: subs)


def test_polar_drift_detected(monkeypatch):
    # elongated + one direction + not radial = polar/tracking drift
    _patch(monkeypatch, _subs(40, ecc=0.72, pa=0.82, rad=0.2))
    res = trends.analyze_trends(PhotonScriptConfig())
    kinds = {f["kind"] for f in res["findings"]}
    assert "polar_drift" in kinds


def test_random_seeing_not_flagged(monkeypatch):
    # elongated but NO coherent direction (low pa_R) = seeing, not a fault
    _patch(monkeypatch, _subs(40, ecc=0.72, pa=0.2, rad=0.3))
    res = trends.analyze_trends(PhotonScriptConfig())
    assert not any(f["kind"] == "polar_drift" for f in res["findings"])


def test_radial_frac_alone_no_longer_flags_tilt(monkeypatch):
    # PS-95: optical tilt comes from the optics report trend, not the
    # mixed-formula ecc_radial_frac (PS-94)
    _patch(monkeypatch, _subs(40, ecc=0.7, pa=0.4, rad=0.75))
    kinds = {f["kind"] for f in trends.analyze_trends(PhotonScriptConfig())["findings"]}
    assert "optical_tilt" not in kinds and "polar_drift" not in kinds


def _optics_night(data_dir, date, verdict="tilt", direction="lower-right",
                  ratio=1.32):
    d = data_dir / "optics"
    d.mkdir(parents=True, exist_ok=True)
    soft = {"lower-right": "BR", "upper-left": "TL"}.get(direction, "BR")
    tilt = {"direction": direction, "ratio_median": ratio, "soft_corner": soft,
            "sharp_corner": "TL" if soft == "BR" else "BR", "agree": 9,
            "n": 10, "stable": True} if verdict == "tilt" else None
    (d / f"{date}.json").write_text(json.dumps({
        "date": date, "rig": "rc16", "headline": f"{verdict} {direction}",
        "overall": {"verdict": verdict, "n_measured": 10, "tilt": tilt,
                    "corner_ratio": ratio, "center_fwhm": 2.6,
                    "center_ecc": 0.4}}), encoding="utf-8")


def test_optics_trend_persistent_tilt_alerts_once(monkeypatch, tmp_path):
    _patch(monkeypatch, _subs(40, ecc=0.35, pa=0.3, rad=0.3, hfr=2.4))
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    for i in range(4):
        _optics_night(tmp_path, f"2026-09-{20 + i:02d}")
    res = trends.analyze_trends(cfg)
    (f,) = [f for f in res["findings"] if f["kind"] == "optical_tilt"]
    assert f["key"] == "optical_tilt:lower-right" and "32.0%" in f["detail"]
    sent = []

    async def _fake_notify(config, msg, **kw):
        sent.append(msg)

    monkeypatch.setattr("photonscript.shared.pushover.notify", _fake_notify)
    trends.check_and_alert(cfg)
    trends.check_and_alert(cfg)          # same finding: no repeat alert
    assert len(sent) == 1 and "tilt" in sent[0]


def test_optics_trend_detects_a_direction_change(monkeypatch, tmp_path):
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    for i in range(4):
        _optics_night(tmp_path, f"2026-09-{10 + i:02d}")
    for i in range(4):
        _optics_night(tmp_path, f"2026-09-{20 + i:02d}",
                      direction="upper-left", ratio=1.25)
    tr = trends.optics_trend(cfg)
    assert [p["date"] for p in tr["nights"]][0] == "2026-09-10"
    ch = tr["change"]
    assert ch["after"] == "2026-09-13" and ch["first_new"] == "2026-09-20"
    assert ch["from"] == "tilt:lower-right" and ch["to"] == "tilt:upper-left"
    assert tr["persistent"]["key"] == "tilt:upper-left"
    # a tilt that changed direction is a new finding: it alerts again
    _patch(monkeypatch, [])
    sent = []

    async def _fake_notify(config, msg, **kw):
        sent.append(msg)

    monkeypatch.setattr("photonscript.shared.pushover.notify", _fake_notify)
    (tmp_path / "trend_alerts.json").write_text(
        json.dumps(["optical_tilt:lower-right"]), encoding="utf-8")
    trends.check_and_alert(cfg)
    assert len(sent) == 1 and "upper-left" in sent[0]


def test_optics_fixed_tilt_is_not_persistent(tmp_path):
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    for i in range(3):
        _optics_night(tmp_path, f"2026-09-{10 + i:02d}")
    for i in range(3):
        _optics_night(tmp_path, f"2026-09-{20 + i:02d}", verdict="fine")
    tr = trends.optics_trend(cfg)
    assert tr["persistent"] is None
    assert tr["change"]["to"] == "fine"


def test_focus_drift_detected(monkeypatch):
    _patch(monkeypatch, _subs(40, ecc=0.35, pa=0.2, rad=0.3, hfr=6.5))
    kinds = {f["kind"] for f in trends.analyze_trends(PhotonScriptConfig())["findings"]}
    assert "focus_drift" in kinds


def test_clean_nights_no_findings(monkeypatch):
    _patch(monkeypatch, _subs(40, ecc=0.35, pa=0.3, rad=0.3, hfr=2.4))
    assert trends.analyze_trends(PhotonScriptConfig())["findings"] == []


def test_too_few_subs_no_findings(monkeypatch):
    _patch(monkeypatch, _subs(10, ecc=0.9, pa=0.9, rad=0.1, hfr=9.0))
    res = trends.analyze_trends(PhotonScriptConfig())
    assert res["findings"] == [] and res["n_subs"] == 10


def test_check_and_alert_dedups(monkeypatch, tmp_path):
    _patch(monkeypatch, _subs(40, ecc=0.72, pa=0.82, rad=0.2))
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    sent = []

    async def _fake_notify(config, msg, **kw):
        sent.append(msg)

    monkeypatch.setattr("photonscript.shared.pushover.notify", _fake_notify)
    trends.check_and_alert(cfg)          # first time → alerts
    trends.check_and_alert(cfg)          # same finding → deduped, silent
    assert len(sent) == 1 and "TREND" in sent[0]
