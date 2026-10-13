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
    only the first time it is seen that night."""
    from photonscript.shared import pushover
    cfg = _cfg(tmp_path)
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
