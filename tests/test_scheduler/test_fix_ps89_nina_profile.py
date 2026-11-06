"""Fix 2026-10-13: NinaClient.get_profile() asked /profile/show without
?active=true and got ninaAPI's profile LIST (Id, Name, IsActive), so the
PS-89 NINA rows (settle, timeout, dither floor) always read unknown. Also
the PS-89 row for PHD2's AutoFind minimum SNR (StarMinSNR, 50 on 10-12),
and the push latches once those rows start evaluating."""
import asyncio

import httpx

from photonscript.scheduler import phd2_audit as pa
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent.nina_client import NinaClient

# the scope PC's answers on 2026-10-13 (trimmed)
PROFILE_LIST = [
    {"Id": "3864b0f8-3118-4a80-a2bd-8b38388e1d41", "Name": "RC16", "IsActive": True},
    {"Id": "bca6fe74-e04b-484d-ac91-30a2dd7c4619", "Name": "piggyback600",
     "IsActive": False}]
PROFILE_ACTIVE = {
    "Name": "RC16", "Id": "3864b0f8-3118-4a80-a2bd-8b38388e1d41",
    "CameraSettings": {"Gain": 0},
    "GuiderSettings": {"GuiderName": "PHD2_Single", "DitherPixels": 20,
                       "DitherRAOnly": False, "SettleTime": 5, "SettlePixels": 5,
                       "SettleTimeout": 60, "PHD2ProfileId": 2}}
# PHD2 as the 10-13 audit read it (GP678C at bin 3)
LIVE_API = {"camera": "GP678C", "binning": 3, "pixel_scale": 0.381031,
            "focal_length_mm": 3248}


def _ok(payload):
    return httpx.Response(200, json={"Response": payload, "Error": "",
                                     "StatusCode": 200, "Success": True})


def _nina(seen):
    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/profile/show"):
            active = request.url.params.get("active") == "true"
            return _ok(PROFILE_ACTIVE if active else PROFILE_LIST)
        if request.url.path.endswith("/equipment/mount/info"):
            return _ok({"Connected": True, "GuideRateRightAscensionArcsecPerSec": 7.5,
                        "GuideRateDeclinationArcsecPerSec": 7.5})
        return httpx.Response(404)
    c = NinaClient("http://nina/v2/api")
    c._client = httpx.AsyncClient(base_url=c.base_url,
                                  transport=httpx.MockTransport(handler))
    return c


def _cfg(tmp_path, **kw):
    kw.setdefault("data_dir", tmp_path / "data")
    return PhotonScriptConfig(_env_file=None, **kw)


def test_get_profile_asks_for_the_active_profile():
    seen = []
    p = asyncio.run(_nina(seen).get_profile())
    assert seen and "active=true" in seen[0]
    assert p["GuiderSettings"]["SettlePixels"] == 5


def test_collect_nina_reads_settle_timeout_and_dither(tmp_path):
    o, note = asyncio.run(pa._collect_nina(_cfg(tmp_path), _nina([])))
    assert note["ok"], note
    assert o["settle_px"] == 5 and o["settle_timeout_s"] == 60 and o["dither_px"] == 20
    assert o["nina_settle_px"] == 5 and o["nina_settle_timeout_s"] == 60


def _eval(tmp_path, observed):
    cfg = _cfg(tmp_path)
    obs = {s: {} for s in pa.SOURCES}
    obs.update(observed)
    res = pa.evaluate(pa.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}


def test_nina_rows_now_evaluate_on_the_live_shape(tmp_path):
    o, _ = asyncio.run(pa._collect_nina(_cfg(tmp_path), _nina([])))
    rows = _eval(tmp_path, {"nina": o, "api": {**LIVE_API, "search_region_px": 20}})
    # 5 px x 0.381"/px = 1.9": over the 0.5 to 1.0" band (warn, never a push)
    assert rows["nina_settle_px"]["status"] == pa.WARN
    assert rows["nina_settle_timeout_s"]["status"] == pa.PASS
    # the search region now sees NINA's 20 px dither: 20 covers it, the range miss warns
    sr = rows["search_region_px"]
    assert sr["status"] == pa.WARN and "20.0 px" in sr["desired"]
    rows = _eval(tmp_path, {"nina": o, "api": {**LIVE_API, "search_region_px": 15}})
    assert rows["search_region_px"]["status"] == pa.FAIL


def test_min_snr_row(tmp_path):
    d = pa.load_desired(_cfg(tmp_path))
    assert not d.get("error") and not d.get("lint"), d.get("lint")
    row = {r["id"]: r for r in d["check"]}["min_snr"]
    assert row["sources"] == ["profile"] and row["min"] == 8 and row["max"] == 20
    for val, want in (("50", pa.FAIL), ("10", pa.PASS), ("12", pa.PASS), ("5", pa.FAIL)):
        assert _eval(tmp_path, {"profile": {"min_snr": val}})["min_snr"]["status"] == want, val
    assert _eval(tmp_path, {})["min_snr"]["status"] == pa.UNKNOWN


def test_new_rows_push_once_per_night(tmp_path, monkeypatch):
    """The NINA rows and min_snr evaluating: the arm push is one per night
    and a ConfigurationChange re-audit (PHD2 sends bursts) re-pushes a FAIL
    only the first time it is seen that night. (Ledger off: with its pushes
    on, min_snr pages from the settings ledger only, see
    test_ps89_rows_the_ledger_pages_are_report_only.)"""
    from photonscript.shared import pushover
    cfg = _cfg(tmp_path, settings_ledger_enabled=False)
    pushes = []

    async def _push(c, msg, **k):
        pushes.append(msg)
    monkeypatch.setattr(pushover, "notify", _push)
    night = {"v": "2026-10-13"}

    async def _run_audit(config, reason="manual", **kw):
        rows = _eval(tmp_path, {"profile": {"min_snr": "50"},
                                "nina": {"settle_px": 5, "nina_settle_px": 5,
                                         "nina_settle_timeout_s": 30, "dither_px": 20},
                                "api": {**LIVE_API, "search_region_px": 15}})
        rows = list(rows.values())
        return {"night": night["v"], "rows": rows,
                "fail_ids": [r["id"] for r in rows if r["status"] == pa.FAIL]}
    monkeypatch.setattr(pa, "run_audit", _run_audit)

    async def go():
        await pa.at_arm(cfg)
        await pa.at_arm(cfg)                    # a re-arm the same night
        for _ in range(5):                      # a burst of configuration changes
            await pa.on_config_change(cfg)
    asyncio.run(go())
    assert len(pushes) == 1
    assert "AutoFind minimum star SNR 50" in pushes[0]
    night["v"] = "2026-10-14"                   # the next night pushes again, once
    asyncio.run(go())
    assert len(pushes) == 2


def test_ps89_rows_the_ledger_pages_are_report_only(tmp_path, monkeypatch):
    """One owner per page (2026-10-13): the settings-ledger rule
    phd2_min_snr (audit_row min_snr) pages the AutoFind minimum SNR, so the
    PS-89 min_snr row stays FAIL in the report, marked paged_by, and never
    pages; the other PS-89 FAILs still do. Ledger off: PS-89 pages it."""
    from photonscript.scheduler import settings_ledger as sl
    from photonscript.shared import pushover
    pushes = []

    async def _push(c, msg, **k):
        pushes.append(msg)
    monkeypatch.setattr(pushover, "notify", _push)

    def _audit(snr="50", focal=600):
        rows = list(_eval(tmp_path, {"profile": {"min_snr": snr},
                                     "api": {**LIVE_API, "focal_length_mm": focal}}
                          ).values())
        return {"night": "2026-10-13", "rows": rows}

    cfg = _cfg(tmp_path, settings_ledger_enabled=True)   # conftest turns it off
    assert sl.audit_rows_paged(cfg) == {"min_snr": "phd2_min_snr",
                                        "guide_scale": "phd2_guide_scale"}
    audit = _audit()
    bad = pa.page_fails(cfg, audit)
    snr = next(r for r in audit["rows"] if r["id"] == "min_snr")
    assert snr["status"] == pa.FAIL                       # still in the report
    assert snr["paged_by"] == "settings ledger (phd2_min_snr)"
    assert "min_snr" not in [r["id"] for r in bad]
    assert "focal_length_mm" in [r["id"] for r in bad]

    async def _only_snr(config, reason="manual", **kw):
        return _audit(focal=3248)
    monkeypatch.setattr(pa, "run_audit", _only_snr)
    asyncio.run(pa.at_arm(cfg))
    asyncio.run(pa.on_config_change(cfg))
    assert pushes == []                                   # the ledger pages it

    for off in ({"settings_ledger_push": False}, {"settings_ledger_enabled": False},
                {"settings_ledger_apps": "nina1,thesky"}):
        c = _cfg(tmp_path / str(len(pushes)), **{"settings_ledger_enabled": True, **off})
        assert sl.audit_rows_paged(c) == {}
        asyncio.run(pa.at_arm(c))
    assert len(pushes) == 3 and all("AutoFind minimum star SNR 50" in p for p in pushes)


def test_every_ledger_audit_row_names_a_ps89_row(tmp_path):
    from photonscript.scheduler import settings_ledger as sl
    cfg = _cfg(tmp_path)
    ids = {r["id"] for r in pa.load_desired(cfg).get("check") or []}
    rules = sl.load_rules(cfg)
    assert not rules.get("lint")
    rows = [r["audit_row"] for r in rules["rule"] if r.get("audit_row")]
    assert "min_snr" in rows and set(rows) <= ids
