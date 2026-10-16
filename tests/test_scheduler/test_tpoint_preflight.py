"""TPoint / ProTrack preflight at arm and the watcher's no-sample alert
(2026-10-16).

fixtures/tpoint/2026-10-16/ are the two files a TheSky "Full" calibration
run left on the scope PC on 2026-10-16, read with GET
/api/thesky/tpoint/file?rel= (read only): `TPoint base run in.dat` 137
bytes (header only, no sample) and `TPoint base run outmod.dat` 98 bytes
("T 0", IH / ID +0.0000). The good model is the super model of 2026-10-15
(fixtures/tpoint/2026-10-15/Super Model Outmod.dat: 91 samples in the fit,
20 terms fitted, sky RMS 12.76") with the 99-sample east-only base run
data (2026-10-15/TPoint base run in.dat).
"""
import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import tpoint_files as tf
from photonscript.scheduler import tpoint_preflight as tpp
from photonscript.scheduler import tpoint_watch as tw
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "tpoint"
EMPTY_IN = FIX / "2026-10-16" / "TPoint base run in.dat"
EMPTY_OUT = FIX / "2026-10-16" / "TPoint base run outmod.dat"
GOOD_IN = FIX / "2026-10-15" / "TPoint base run in.dat"
GOOD_OUT = FIX / "2026-10-15" / "Super Model Outmod.dat"
GOOD_TPTX = "TPoint Settings-2026oct-12RMS.tptx"
EMPTY_TPTX = "TPoint Settings-2026oct-number6.tptx"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.delenv("PS_TPOINT_PREFLIGHT", raising=False)   # suite pins it off


def _put(path: Path, src, age_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(src, Path):
        shutil.copyfile(src, path)
    else:
        path.write_bytes(src)
    t = time.time() - age_s
    os.utime(path, (t, t))


def _tree(tmp_path, *, empty: bool, protrack: str | None = "1") -> Path:
    """TheSky's user folder: the 10-15 super model (a day old), and either
    the 10-16 cleared base run (an hour old) or the 10-15 base run data."""
    root = tmp_path / "TheSky Professional Edition 64"
    tp = root / "TPoint"
    _put(tp / "Super Model Outmod.dat", GOOD_OUT, 86400)
    if empty:
        _put(tp / "Super Model Indat.dat", GOOD_IN, 86400)
        _put(tp / "TPoint base run in.dat", EMPTY_IN, 3600)
        _put(tp / "TPoint base run outmod.dat", EMPTY_OUT, 3600)
    else:
        _put(tp / "Super Model Indat.dat", GOOD_IN, 86400)
    if protrack is not None:
        _put(root / "Imaging System Profiles" / "ImagingSystem.ini",
             f"[MKSDRIVER0]\r\nProTrackOn={protrack}\r\n".encode(), 7200)
    return root


def _astro(tmp_path) -> Path:
    """C:\\astro: the good model saved on 10-15, the empty settings file the
    new run created after the clear (newer)."""
    d = tmp_path / "astro"
    _put(d / GOOD_TPTX, b"\x00" * 64, 80000)
    _put(d / EMPTY_TPTX, b"\x00" * 16, 600)
    _put(d / "notes.txt", b"x", 60)
    return d


def _cfg(tmp_path, root, **kw):
    kw.setdefault("tpoint_saved_models_dir", "")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              thesky_user_dir=str(root), nina_logs_dir=str(tmp_path / "logs"),
                              image_watch_dir=str(tmp_path / "fits"), **kw)


# ------------------------------------------------------------- the files

def test_1016_files_parse_as_an_empty_model():
    assert EMPTY_IN.stat().st_size == 137 and EMPTY_OUT.stat().st_size == 98
    text = EMPTY_IN.read_bytes().decode("latin-1")
    assert tf.parse_data(text) is None and tf.header_only(text)
    assert not tf.header_only(GOOD_IN.read_bytes().decode("latin-1"))
    m = tf.parse_model(EMPTY_OUT.read_bytes().decode("latin-1"))
    assert m["t_obs"] == 0 and m["nonzero_terms"] == 0 and m["sky_rms_arcsec"] is None
    g = tf.parse_model(GOOD_OUT.read_bytes().decode("latin-1"))
    assert g["t_obs"] == 91 and g["fitted_terms"] == 20 and g["nonzero_terms"] == 20
    assert g["sky_rms_arcsec"] == pytest.approx(12.7641)


def test_stats_marks_the_active_run_empty_although_points_fall_back(tmp_path):
    cfg = _cfg(tmp_path, _tree(tmp_path, empty=True))
    st = tf.stats(cfg)
    # the old behaviour hid it: points come from another run's data file
    assert st["active_model_file"].endswith("TPoint base run outmod.dat")
    assert st["points"] == 99 and st["data_file"].endswith("Super Model Indat.dat")
    assert st["active_run_points"] == 0 and st["active_t_obs"] == 0
    assert st["active_nonzero_terms"] == 0 and st["protrack"] is True


# ------------------------------------------------------------- verdicts

def test_empty_model_unguided_blocks_and_names_the_saved_model(tmp_path):
    astro = _astro(tmp_path)
    cfg = _cfg(tmp_path, _tree(tmp_path, empty=True), tpoint_saved_models_dir=str(astro))
    rec = tpp.evaluate(cfg, guided=False)
    assert rec["level"] == "block" and rec["action"] == "block" and rec["enforced"]
    assert rec["points"] == 0
    # the empty settings file is newer than the clear: never recommended
    assert [Path(m["path"]).name for m in rec["saved_models"]] == [GOOD_TPTX]
    assert rec["fix"] == (f"TPoint has no model: open {astro / GOOD_TPTX} (TPoint Module "
                          "Settings > Open) and turn ProTrack on, or run a calibration")
    assert rec["line"].startswith("BLOCK (unguided): no TPoint model: ")
    assert "header only (0 samples)" in rec["line"] and "T 0" in rec["line"]
    assert rec["line"].endswith("NINA #1 is not loaded until the model is back")
    msg = tpp.push_text(rec)
    assert msg.startswith("Armed UNGUIDED and TPoint has no model: open ")
    assert GOOD_TPTX in msg and "Arm anyway (TPoint loaded)" in msg


def test_empty_model_without_a_saved_models_folder_says_latest_saved(tmp_path):
    cfg = _cfg(tmp_path, _tree(tmp_path, empty=True),
               tpoint_saved_models_dir=str(tmp_path / "nope"))
    rec = tpp.evaluate(cfg, guided=False)
    assert rec["fix"] == ("TPoint has no model: open your latest saved model (TPoint "
                          "Module Settings > Open) and turn ProTrack on, or run a calibration")


def test_good_12rms_model_east_only_warns_never_blocks(tmp_path):
    cfg = _cfg(tmp_path, _tree(tmp_path, empty=False))
    rec = tpp.evaluate(cfg, guided=False)
    assert rec["level"] == "warn" and rec["action"] == "warn" and not rec["enforced"]
    assert [c["id"] for c in rec["checks"] if c["level"] == "warn"] == ["coverage"]
    assert rec["coverage"]["overall"] == "east-only"
    assert rec["points"] == 99 and rec["fitted_terms"] == 20
    assert "sky RMS 12.8\"" in rec["line"] and "ProTrack on" in rec["line"]
    assert rec["fix"] is None
    assert tpp.push_text(rec).startswith("Armed UNGUIDED, TPoint preflight: coverage east-only")


def test_protrack_off_warns(tmp_path):
    cfg = _cfg(tmp_path, _tree(tmp_path, empty=False, protrack="0"))
    rec = tpp.evaluate(cfg, guided=False)
    assert rec["level"] == "warn" and rec["protrack"] is False
    assert any(c["id"] == "protrack" and c["level"] == "warn" for c in rec["checks"])
    assert "ProTrack is OFF" in rec["line"] and "ProTrack OFF" in rec["line"]


def test_rms_and_polar_change_warn(tmp_path):
    st = {"ok": True, "protrack": True, "active_model_file": "TPoint\\x outmod.dat",
          "active_run_points": 120, "active_t_obs": 120, "active_nonzero_terms": 12,
          "active_fitted_terms": 12, "active_sky_rms_arcsec": 55.0,
          "active_model_mtime": "2026-10-10T03:00:00Z"}
    cfg = _cfg(tmp_path, tmp_path / "none", tpoint_polar_changed_on="2026-10-14")
    rec = tpp.evaluate(cfg, guided=False, st=st)
    ids = {c["id"] for c in rec["checks"] if c["level"] == "warn"}
    assert ids == {"rms", "polar_change"} and rec["level"] == "warn"
    assert "sky RMS 55.0\" over 40\"" in rec["line"]
    assert "older than the polar / equipment change of 2026-10-14" in rec["line"]
    ok = tpp.evaluate(_cfg(tmp_path, tmp_path / "none"), guided=False,
                      st={**st, "active_sky_rms_arcsec": 12.4})
    assert ok["level"] == "ok" and ok["line"].startswith("OK (unguided): ProTrack on")


def test_guided_arm_is_info_only(tmp_path):
    cfg = _cfg(tmp_path, _tree(tmp_path, empty=True))
    rec = tpp.evaluate(cfg, guided=True)
    assert rec["level"] == "block" and rec["action"] == "info" and not rec["enforced"]
    assert rec["line"].startswith("BLOCK, info only (guided)")
    assert tpp.push_text(rec) is None


@pytest.mark.parametrize("kw,why", [
    ({"override": True}, "override: model loaded by hand"),
    ({"recipe": {"id": "tpoint_mapping_then_tonight"}}, "TPoint mapping recipe tonight"),
    ({"mode": "warn"}, "tpoint_preflight=warn"),
])
def test_block_not_enforced(tmp_path, kw, why):
    md = kw.pop("mode", None)
    cfg = _cfg(tmp_path, _tree(tmp_path, empty=True),
               **({"tpoint_preflight": md} if md else {}))
    rec = tpp.evaluate(cfg, guided=False, **kw)
    assert rec["level"] == "block" and rec["action"] == "warn" and not rec["enforced"]
    assert rec["why_not_enforced"] == why
    assert "Not enforced" in tpp.push_text(rec)


def test_off(tmp_path):
    cfg = _cfg(tmp_path, _tree(tmp_path, empty=True), tpoint_preflight="off")
    rec = tpp.evaluate(cfg, guided=False)
    assert rec["action"] == "off" and not rec["enforced"] and tpp.push_text(rec) is None


# ------------------------------------------------------------- the armer

NIGHT = "2026-10-16"


def _armer(tmp_path, monkeypatch, guiding, empty=True, **cfg):
    monkeypatch.chdir(tmp_path)
    a = armer_mod.Armer(_cfg(tmp_path, _tree(tmp_path, empty=empty), **cfg))
    a.plan = {"night_of": NIGHT, "dusk_utc": "2026-10-17T01:30:00Z",
              "dawn_utc": "2026-10-17T11:30:00Z"}
    a.guiding_override = guiding
    a.state, a.detail = "ARMED", "armed"
    return a


def _events(tmp_path):
    p = tmp_path / "data" / "runs" / f"{NIGHT}_events.jsonl"
    return ([json.loads(x) for x in p.read_text().splitlines() if '"tpoint_preflight"' in x]
            if p.exists() else [])


def test_armer_unguided_empty_model_pushes_and_refuses_the_dispatch(tmp_path, monkeypatch):
    sent = []

    async def _n(cfg, msg, **kw):
        sent.append((msg, kw.get("title"), kw.get("priority")))
    monkeypatch.setattr(armer_mod, "notify", _n)
    a = _armer(tmp_path, monkeypatch, "unguided")
    asyncio.run(a._tpoint_preflight_at_arm())
    assert len(sent) == 1 and sent[0][1] == tpp.PUSH_TITLE and sent[0][2] == 1
    assert "TPoint has no model: open your latest saved model" in sent[0][0]
    st = a.status()["tpoint_preflight"]          # GET /api/arm
    assert st["level"] == "block" and st["enforced"] and st["phase"] == "arm"
    assert "TPoint preflight BLOCK (unguided)" in a.detail
    built = []
    monkeypatch.setattr(a, "_build_night", lambda *x, **k: built.append(1))
    assert a._dispatch() is False and not built
    assert a.detail.startswith("TPoint preflight blocked the night: TPoint has no model")
    assert [e["value"] for e in _events(tmp_path)] == ["block", "block"]
    assert [e["phase"] for e in _events(tmp_path)] == ["arm", "dispatch"]
    # persisted and restored with the night
    saved = json.loads(a._state_path.read_text(encoding="utf-8"))
    assert saved["tpoint_preflight"]["level"] == "block"
    # the morning report line (a blocked night has no subs log)
    line = tpp.morning_line(a.config, NIGHT)
    assert line.startswith("TPoint preflight: BLOCK (unguided)") and "Fix: TPoint has no" in line
    from photonscript.scheduler.morning_report import card_lines
    assert any(x.startswith("TPoint preflight: BLOCK")
               for x in card_lines({"date": None, "tpoint_preflight": line}))


def test_armer_override_lets_the_dispatch_through(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, "unguided")
    a.tpoint_override = True
    built = []
    monkeypatch.setattr(a, "_build_night", lambda *x, **k: built.append(1))
    assert a._dispatch() is False and built == [1]      # past the preflight
    assert a.tpoint_pf["action"] == "warn" and not a.tpoint_pf["enforced"]


def test_armer_guided_arm_not_blocked_and_no_push(tmp_path, monkeypatch):
    sent = []

    async def _n(cfg, msg, **kw):
        sent.append(msg)
    monkeypatch.setattr(armer_mod, "notify", _n)
    a = _armer(tmp_path, monkeypatch, "guided")
    asyncio.run(a._tpoint_preflight_at_arm())
    assert sent == [] and a.status()["tpoint_preflight"]["action"] == "info"
    built = []
    monkeypatch.setattr(a, "_build_night", lambda *x, **k: built.append(1))
    a._dispatch()
    assert built == [1]


def test_armer_good_model_warns_once_and_dispatches(tmp_path, monkeypatch):
    sent = []

    async def _n(cfg, msg, **kw):
        sent.append((msg, kw.get("priority")))
    monkeypatch.setattr(armer_mod, "notify", _n)
    a = _armer(tmp_path, monkeypatch, "unguided", empty=False)
    asyncio.run(a._tpoint_preflight_at_arm())
    assert len(sent) == 1 and sent[0][1] == 0 and "east-only" in sent[0][0]
    built = []
    monkeypatch.setattr(a, "_build_night", lambda *x, **k: built.append(1))
    a._dispatch()
    assert built == [1]
    assert [e["phase"] for e in _events(tmp_path)] == ["arm"]   # same level: no repeat


# ------------------------------------------------------------- the watcher

DARK = datetime(2026, 10, 16, 4, 0)       # 22:00 MDT
DAY = datetime(2026, 10, 16, 19, 0)       # 13:00 MDT


def _watch_tree(tmp_path, n_good=True):
    root = tmp_path / "TheSky Professional Edition 64"
    tp = root / "TPoint"
    _put(tp / "Super Model Outmod.dat", GOOD_OUT, 2 * 86400)
    _put(tp / "TPoint base run in.dat", GOOD_IN, 86400)
    _put(tp / "TPoint base run outmod.dat", GOOD_OUT, 86400)
    return root


def _clear(root, at: datetime):
    tp = root / "TPoint"
    for f, src in (("TPoint base run in.dat", EMPTY_IN), ("TPoint base run outmod.dat", EMPTY_OUT)):
        shutil.copyfile(src, tp / f)
        t = (at - datetime(1970, 1, 1)).total_seconds()
        os.utime(tp / f, (t, t))


def _wcfg(tmp_path, root):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              thesky_user_dir=str(root), nina_logs_dir=str(tmp_path / "logs"))


def _watch_events(tmp_path, night):
    p = tmp_path / "data" / "runs" / f"{night}_events.jsonl"
    return ([json.loads(x) for x in p.read_text().splitlines() if '"tpoint_watch"' in x]
            if p.exists() else [])


def test_watcher_alerts_once_when_a_cleared_run_gets_no_sample(tmp_path):
    root = _watch_tree(tmp_path)
    cfg = _wcfg(tmp_path, root)
    t0 = DARK
    tw.step(cfg, now=t0 - timedelta(minutes=30))            # the old run, idle
    _clear(root, t0)
    r = tw.step(cfg, now=t0 + timedelta(minutes=1))
    run = r["state"]["run"]
    assert run["points"] == 0 and run["cleared_utc"] and r["alert"] is None
    assert "data cleared, header only), no samples yet" in r["state"]["text"]
    r = tw.step(cfg, now=t0 + timedelta(minutes=10))
    assert r["alert"] is None                                # under 15 min
    r = tw.step(cfg, now=t0 + timedelta(minutes=16))
    assert r["alert"].startswith("TPoint run started but no samples in 15 min "
                                 "(roof closed? camera? Image Link?)")
    assert "roof closed? camera? Image Link?" in r["state"]["text"]
    r = tw.step(cfg, now=t0 + timedelta(minutes=30))
    assert r["alert"] is None                                # once
    evs = [e for e in _watch_events(tmp_path, "2026-10-15") if e["value"] == "no_samples"]
    assert len(evs) == 1
    assert "no samples in 15 min" in tw.morning_line(cfg, "2026-10-15")

    # the push goes out in alert mode with its own title (observe: none)
    for md, want in (("alert", 1), ("observe", 0)):
        sub = tmp_path / md
        r2 = _watch_tree(sub)
        c2 = _wcfg(sub, r2).model_copy(update={"tpoint_watch_mode": md})
        tw.step(c2, now=t0 - timedelta(minutes=30))
        _clear(r2, t0)
        tw.step(c2, now=t0 + timedelta(minutes=1))
        sent = []

        async def _n(cfg, msg, **kw):
            sent.append((msg, kw.get("title")))
        asyncio.run(tw.tick(c2, now=t0 + timedelta(minutes=20), notify=_n))
        assert len(sent) == want
        if want:
            assert sent[0][1] == "PhotonScript TPoint run"


def test_watcher_no_alert_in_daylight_or_once_samples_arrive(tmp_path):
    root = _watch_tree(tmp_path)
    cfg = _wcfg(tmp_path, root)
    tw.step(cfg, now=DAY - timedelta(minutes=30))
    _clear(root, DAY)
    tw.step(cfg, now=DAY + timedelta(minutes=1))
    assert tw.step(cfg, now=DAY + timedelta(minutes=20))["alert"] is None   # daytime test run
    # a dark run whose first sample arrives in time
    root2 = _watch_tree(tmp_path / "b")
    cfg2 = _wcfg(tmp_path / "b", root2)
    tw.step(cfg2, now=DARK - timedelta(minutes=30))
    _clear(root2, DARK)
    tw.step(cfg2, now=DARK + timedelta(minutes=1))
    lines = EMPTY_IN.read_bytes().decode("latin-1").rstrip("\r\n").split("\r\n")
    one = GOOD_IN.read_bytes().decode("latin-1").split("\r\n")[5]
    p = root2 / "TPoint" / "TPoint base run in.dat"
    p.write_bytes(("\r\n".join(lines + [one, ""])).encode("latin-1"))
    t = (DARK + timedelta(minutes=5) - datetime(1970, 1, 1)).total_seconds()
    os.utime(p, (t, t))
    r = tw.step(cfg2, now=DARK + timedelta(minutes=6))
    assert r["state"]["run"]["points"] == 1 and r["state"]["status"] == "running"
    assert tw.step(cfg2, now=DARK + timedelta(minutes=20))["alert"] is None


def test_new_sources_are_ascii():
    root = Path(__file__).resolve().parents[2]
    for f in ("photonscript/scheduler/tpoint_preflight.py",
              "photonscript/scheduler/tpoint_watch.py", __file__):
        txt = (root / f).read_text(encoding="utf-8")
        assert txt.isascii(), f
        assert "\u2014" not in txt
