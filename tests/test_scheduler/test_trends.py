"""Cross-night trend / systematic-drift detector."""
import photonscript.scheduler.trends as trends
from photonscript.shared.config import PhotonScriptConfig


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


def test_optical_tilt_detected(monkeypatch):
    # elongated + radial from center = tilt/curvature, not polar
    _patch(monkeypatch, _subs(40, ecc=0.7, pa=0.4, rad=0.75))
    kinds = {f["kind"] for f in trends.analyze_trends(PhotonScriptConfig())["findings"]}
    assert "optical_tilt" in kinds and "polar_drift" not in kinds


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
