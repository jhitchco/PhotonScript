"""2026-10-16: "Skipping Andromeda Galaxy tonight: broadband-only and the
moon defers broadband (moon 37% lit, up at dusk (alt 24 deg), sets 22:54
local ...)" for all three goals, then "Night plan: 0 targets". Dusk 02:01Z,
moonset 04:54Z (about 3 h later): the plan is made at pre-config, before
moonset, so the grace (1.5 h) and the already-set rule never applied.

Fix: a moon up at dusk that sets before dawn does not defer the night.
Broadband-only targets get windows from moonset, and the generator waits
for moonset (WaitForTime at the local moonset time) before broadband. A
moon up all night still defers; a moon down at dusk is unchanged.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import campaign  # noqa: F401  (import check)
from photonscript.scheduler import dispatch_gate as dg
from photonscript.scheduler import moon
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import target_planner as tp
from photonscript.scheduler.deep_night import moon_fit
from photonscript.scheduler.moon import (broadband_deferred, broadband_from,
                                         broadband_from_local, describe_moon)
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.shared.astronomy import get_twilight_times
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (ExposurePlan, FilterType,
                                        ImagingProject, NinaSequenceTarget)

F = FilterType
FIX = Path(__file__).parent / "fixtures" / "deep_night" / "projects2_2026-10-14.json"
PRECONFIG = datetime(2026, 10, 16, 20, 0)      # 14:00 MDT, before dusk
MOONSET = datetime(2026, 10, 17, 4, 54)        # 22:54 MDT
GOALS = ["Andromeda Galaxy", "Heart Nebula", "NGC 604"]


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _goals():
    return [ImagingProject(**p) for p in json.loads(FIX.read_text())
            if p["target"]["name"] in GOALS]


def _tonight(cfg, now=PRECONFIG, **kw):
    tw = get_twilight_times(cfg.get_observatory(), datetime(2026, 10, 16))
    mw = {"available": True, "down_at_dusk": False, "illum_pct": 37,
          "alt_at_dusk_deg": 24.0, "dusk_utc": tw["astro_dark_start"],
          "dawn_utc": tw["astro_dark_end"], "now_utc": now,
          "rise_local_hh": None, "rise_local_mm": None, "rise_utc": None,
          "set_utc": MOONSET, "set_local": "22:54"}
    mw.update(kw)
    return mw


@pytest.fixture
def tonight(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    mw = _tonight(cfg)
    monkeypatch.setattr(moon, "moon_window_tonight",
                        lambda config, now=None: dict(mw))
    monkeypatch.setattr(nsj, "_moon_window", lambda: dict(mw))
    moon._cache.clear()
    yield cfg, mw
    moon._cache.clear()


# --- the rule ------------------------------------------------------------------

def test_moon_setting_before_dawn_opens_broadband_at_moonset(tmp_path):
    mw = _tonight(_cfg(tmp_path))
    assert broadband_deferred(mw) is False
    assert broadband_from(mw) == MOONSET
    assert broadband_from_local(mw) == (22, 54)
    assert "broadband after moonset (22:54 local)" in describe_moon(mw)


def test_moon_up_all_night_still_defers(tmp_path):
    allnight = _tonight(_cfg(tmp_path), set_utc=None, set_local=None)
    assert broadband_deferred(allnight) is True
    assert broadband_from(allnight) is None
    # a faint moon up all night is still fine any time
    assert broadband_deferred({**allnight, "illum_pct": 10}) is False


def test_no_wait_when_faint_soon_set_or_already_set(tmp_path):
    mw = _tonight(_cfg(tmp_path))
    assert broadband_from({**mw, "illum_pct": 12}) is None
    assert broadband_from({**mw, "now_utc": MOONSET + timedelta(minutes=5)}) is None
    soon = {**mw, "set_utc": mw["dusk_utc"] + timedelta(hours=1)}
    assert broadband_from(soon) is None and not broadband_deferred(soon)


def test_down_at_dusk_unchanged():
    down = {"available": True, "down_at_dusk": True, "illum_pct": 60,
            "rise_utc": MOONSET, "set_utc": None}
    assert broadband_deferred(down) is False and broadband_from(down) is None


def test_deep_moon_fit_counts_broadband_after_moonset_only():
    lo = datetime(2026, 10, 17, 2, 0)
    owed = {"NB": 0.0, "BB": 100.0, "OSC": 0.0}
    assert moon_fit(owed, lo, lo + timedelta(hours=8), True, None,
                    lo + timedelta(hours=2)) == pytest.approx(0.75)


# --- the planner -----------------------------------------------------------------

def test_tonight_plans_all_three_goals_from_moonset(tonight, caplog):
    cfg, _mw = tonight
    import logging
    with caplog.at_level(logging.INFO, "photonscript.scheduler.target_planner"):
        ts = tp.plan_night_sequence(_goals(), cfg, PRECONFIG)
    assert "Skipping" not in caplog.text
    assert sorted(t.name for t in ts) == sorted(GOALS), caplog.text
    first = min(t.window_start_utc for t in ts)
    assert first == MOONSET
    assert all(t.window_start_utc >= MOONSET for t in ts)


def test_narrowband_target_takes_the_moonlit_start(tonight):
    cfg, mw = tonight
    t0 = mw["dusk_utc"]
    ts = [NinaSequenceTarget(name="M31", ra_hours=0.7, dec_degrees=41,
                             exposures=[ExposurePlan(
                                 filter_type=F.LUMINANCE, exposure_seconds=300,
                                 count=200, gain=200, offset=256)]),
          NinaSequenceTarget(name="Heart", ra_hours=2.5, dec_degrees=61,
                             exposures=[ExposurePlan(
                                 filter_type=F.HA, exposure_seconds=600,
                                 count=200, gain=200, offset=256)])]
    kept = tp._apply_windows(ts, {}, {"M31": 1e6, "Heart": 1e6},
                             {"M31": 90, "Heart": 10}, cfg, t0,
                             mw["dawn_utc"], t0, mw)
    assert [t.name for t in kept] == ["Heart", "M31"]
    assert kept[0].window_start_utc == t0
    assert kept[1].window_start_utc >= MOONSET


# --- the generator -----------------------------------------------------------------

def _types(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            _types(v, out)
    elif isinstance(node, list):
        for v in node:
            _types(v, out)
    return out


def _short(t):
    return t.split(",")[0].rsplit(".", 1)[-1]


def _waits(node):
    return [d for d in _types(node) if _short(d.get("$type", "")) == "WaitForTime"
            and "TimeProvider" in d.get("SelectedProvider", {}).get("$type", "")]


def test_broadband_only_target_waits_for_moonset_before_it_slews(tonight):
    t = NinaSequenceTarget(name="Andromeda Galaxy", ra_hours=0.7,
                           dec_degrees=41, exposures=[ExposurePlan(
                               filter_type=F.LUMINANCE, exposure_seconds=300,
                               count=20, gain=200, offset=256)])
    c = nsj._build_target_container(t, 30.0)
    items = c["Items"]["$values"]
    kinds = [_short(i["$type"]) for i in items]
    w = kinds.index("WaitForTime")
    assert (items[w]["Hours"], items[w]["Minutes"]) == (22, 54)
    slew = next(k for k, i in enumerate(items)
                if "Slew" in _short(i["$type"]) or _types(i) and any(
                    "SlewScopeToRaDec" in d.get("$type", "") for d in _types(i)))
    assert w < slew


def test_mixed_target_shoots_narrowband_first_then_waits(tonight):
    t = NinaSequenceTarget(name="Crescent", ra_hours=20.2, dec_degrees=38,
                           exposures=[ExposurePlan(
                               filter_type=F.RED, exposure_seconds=300,
                               count=5, gain=200, offset=256),
                               ExposurePlan(filter_type=F.HA,
                                            exposure_seconds=600, count=5,
                                            gain=200, offset=256)])
    c = nsj._build_target_container(t, 30.0)
    names = [d.get("Name") or "" for d in _types(c)]
    after = [n for n in names if n.endswith(nsj.FILTER_AFTER_MOONSET_SUFFIX)]
    assert after == ["R" + nsj.FILTER_AFTER_MOONSET_SUFFIX]
    w = _waits(c)
    assert [(x["Hours"], x["Minutes"]) for x in w] == [(22, 54)]
    # the wait is inside the R block, not before the slew
    top = [_short(i["$type"]) for i in c["Items"]["$values"]]
    assert "WaitForTime" not in top


def test_tonight_sequence_passes_the_dispatch_gate(tonight):
    cfg, _mw = tonight
    ts = tp.plan_night_sequence(_goals(), cfg, PRECONFIG, guided=False)
    seq = json.loads(nsj.generate_nina_json(build_sequence_for_night("t", ts)))
    v = dg.check(seq, guided=False)
    assert v["ok"], v
    assert len(_waits(seq)) >= 1
