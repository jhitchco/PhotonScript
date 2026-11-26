"""RC16 minimum filter block (rc16_min_block_subs, 2026-11-25).

On 2026-11-25 the RC16 spent 125 min autofocusing vs 83 min exposing. A
short Elephant Trunk window scaled the owed Ha 22 / OIII 20 / SII 31 (300 s)
uniformly with a floor of 1, so every pass was a handful of one- and
two-sub filter blocks, each with its own AF + filter switch (every_block).
With rc16_min_block_subs N > 1 a filter scaled under N subs drops out for
tonight (smallest share first) and the rest rescale into the whole budget.

Pinned: the default (1) is today's uniform scale-down, the Elephant Trunk
case at N=6 (every block >= 6, inside the budget), a single-filter group is
unchanged, which filter drops (smallest share, ties by goal done then the
later filter), the window fit through _put_windows, the config field and
env var, and the night plan / shutter estimate with a zero-count filter.
"""

from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import night_plan, shutter_efficiency
from photonscript.scheduler.target_planner import (
    _fit_by_moon, _put_windows, _scale_group, min_block_rule)
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject,
                                        NinaSequenceTarget)

F = FilterType
ET_WINDOW_S = 72 * 60 * 0.85     # _put_windows fits a pass into 85 % of it


def _plan(f, exp, count, acquired=0, rig="rc16"):
    return ExposurePlan(filter_type=f, exposure_seconds=exp, count=count,
                        acquired=acquired, gain=200, offset=50, rig=rig)


def _et():
    """Elephant Trunk's owed RC16 subs on 2026-11-25 (300 s)."""
    return [_plan(F.HA, 300, 22), _plan(F.OIII, 300, 20),
            _plan(F.SII, 300, 31)]


def _counts(exps):
    return {e.filter_type.value: e.count for e in exps}


def _legacy_scale(counts, exp, budget_s):
    """The pre-2026-11-25 _scale_group, for the N=1 comparison."""
    total = sum(c * exp for c in counts)
    if total <= budget_s or total == 0:
        return list(counts)
    scale = budget_s / total
    return [max(1, int(c * scale)) if c > 0 else 0 for c in counts]


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


# --- the rule ---------------------------------------------------------------------

def test_elephant_trunk_n6_every_block_at_least_six_inside_the_budget():
    et = _et()
    used = _scale_group(et, ET_WINDOW_S, 6)
    blocks = [e.count for e in et if e.count > 0]
    assert blocks and all(n >= 6 for n in blocks)
    assert used <= ET_WINDOW_S
    assert used == sum(e.count * e.exposure_seconds for e in et)
    # Ha and OIII drop out tonight, SII takes the whole window
    assert _counts(et) == {"Ha": 0, "OIII": 0, "SII": 12}


def test_elephant_trunk_n1_is_todays_uniform_scale_down():
    et = _et()
    _scale_group(et, ET_WINDOW_S, 1)
    assert _counts(et) == {"Ha": 3, "OIII": 3, "SII": 5}


@pytest.mark.parametrize("budget_s", [600, 1650, 3672, 9000, 15000, 30000])
@pytest.mark.parametrize("counts", [(22, 20, 31), (40, 3, 30), (5, 0, 9),
                                    (1, 1, 1), (100, 60, 2)])
def test_n1_matches_the_legacy_scale_down(budget_s, counts):
    exps = [_plan(f, 300, c) for f, c in zip((F.HA, F.OIII, F.SII), counts)]
    _scale_group(exps, budget_s)            # default: rule off
    assert [e.count for e in exps] == _legacy_scale(counts, 300, budget_s)
    exps = [_plan(f, 300, c) for f, c in zip((F.HA, F.OIII, F.SII), counts)]
    _scale_group(exps, budget_s, 1)
    assert [e.count for e in exps] == _legacy_scale(counts, 300, budget_s)


@pytest.mark.parametrize("tag", ["NB", "BB", "NB+OIII", None])
def test_fit_by_moon_without_the_rule_is_unchanged(tag):
    mixed = lambda: _et() + [_plan(F.LUMINANCE, 180, 40),   # noqa: E731
                             _plan(F.RED, 180, 15)]
    a = _counts(_fit_by_moon(mixed(), ET_WINDOW_S, tag))
    b = _counts(_fit_by_moon(mixed(), ET_WINDOW_S, tag, None))
    c = _counts(_fit_by_moon(mixed(), ET_WINDOW_S, tag, (1, {})))
    assert a == b == c


@pytest.mark.parametrize("budget_s", [300, 900, 1650, 3672])
def test_single_filter_group_unchanged(budget_s):
    one = [_plan(F.HA, 300, 22)]
    _scale_group(one, budget_s, 6)
    assert one[0].count == _legacy_scale([22], 300, budget_s)[0]
    assert one[0].count >= 1                # floor 1, never dropped


def test_dropped_filter_is_the_smallest_share():
    exps = [_plan(F.HA, 300, 40), _plan(F.OIII, 300, 10),
            _plan(F.SII, 300, 30)]
    _scale_group(exps, 6000, 6)              # uniform: Ha 10, OIII 2, SII 7
    assert _counts(exps) == {"Ha": 11, "OIII": 0, "SII": 8}
    assert sum(e.count * 300 for e in exps) <= 6000


def test_smallest_share_by_seconds_not_subs():
    # OIII 600 s x 3 holds more time than SII 300 s x 4: SII drops first
    exps = [_plan(F.HA, 300, 40), _plan(F.OIII, 600, 15),
            _plan(F.SII, 300, 20)]
    _scale_group(exps, 6000, 6)
    assert _counts(exps)["SII"] == 0
    assert all(e.count >= 6 for e in exps if e.count > 0)


def test_tie_drops_the_filter_with_the_most_of_its_goal_done():
    # Ha 3 / OIII 3 tie on share: the one with more of its goal done drops
    first = [_plan(F.HA, 300, 20), _plan(F.OIII, 300, 20)]
    _scale_group(first, 1800, 6, {"Ha": 0.8, "OIII": 0.1})
    assert _counts(first) == {"Ha": 0, "OIII": 6}
    first = [_plan(F.HA, 300, 20), _plan(F.OIII, 300, 20)]
    _scale_group(first, 1800, 6, {"Ha": 0.1, "OIII": 0.8})
    assert _counts(first) == {"Ha": 6, "OIII": 0}


def test_tie_without_done_drops_the_later_filter():
    pair = [_plan(F.HA, 300, 20), _plan(F.OIII, 300, 20)]
    _scale_group(pair, 1800, 6)              # 3 / 3, nothing done
    assert _counts(pair) == {"Ha": 6, "OIII": 0}


def test_last_filter_left_gets_what_fits_floor_one():
    exps = _et()
    _scale_group(exps, 1650, 6)              # SII alone: 5 fits, under 6
    assert _counts(exps) == {"Ha": 0, "OIII": 0, "SII": 5}
    exps = _et()
    _scale_group(exps, 100, 6)               # nothing fits: floor 1 on one
    assert [e.count for e in exps if e.count > 0] == [1]


def test_a_filter_that_finishes_tonight_is_never_short():
    # Ha 19 / OIII 2 / SII 2: SII drops (tie, later), then Ha 20 + OIII 3
    # fit whole; OIII gets all it owes, so it stays (no budget left unused)
    exps = [_plan(F.HA, 300, 20), _plan(F.OIII, 300, 3),
            _plan(F.SII, 300, 3)]
    _scale_group(exps, 300 * 25, 6)
    assert _counts(exps) == {"Ha": 20, "OIII": 3, "SII": 0}


def test_hdr_short_only_plan_stays_zero_and_keeps_its_shorts():
    # PS-112: a plan whose long set is done (count 0) stays at 0, shorts kept
    done = ExposurePlan(filter_type=F.OIII, exposure_seconds=600, count=0,
                        hdr_short_seconds=60, hdr_short_count=7)
    exps = [_plan(F.HA, 300, 22), done, _plan(F.SII, 300, 31)]
    _scale_group(exps, ET_WINDOW_S, 6)
    assert done.count == 0 and done.short_remaining() == 7
    assert all(e.count >= 6 for e in exps if e.count > 0)


def test_only_rc16_filters_are_dropped():
    exps = [_plan(F.HA, 300, 22), _plan(F.OSC, 300, 2, rig="piggyback"),
            _plan(F.SII, 300, 31)]
    _scale_group(exps, ET_WINDOW_S, 6)
    assert _counts(exps)["OSC"] >= 1


# --- through the planner --------------------------------------------------------

def test_window_fit_applies_the_rule(tmp_path):
    t0 = datetime(2026, 11, 26, 2, 0)
    t = NinaSequenceTarget(name="Elephant's Trunk", ra_hours=21.6,
                           dec_degrees=57.5, exposures=_et())
    alloc = {"windows": {t.name: (t0, t0 + timedelta(minutes=72))},
             "handoff": {}, "fill": None}
    kept = _put_windows([t], alloc, {}, {t.name: (6, {})})
    assert kept == [t]
    assert _counts(t.exposures) == {"Ha": 0, "OIII": 0, "SII": 12}
    # without the rule: today's per-pass counts
    t2 = NinaSequenceTarget(name="ET", ra_hours=21.6, dec_degrees=57.5,
                            exposures=_et())
    _put_windows([t2], {"windows": {"ET": alloc["windows"][t.name]},
                        "handoff": {}, "fill": None})
    assert _counts(t2.exposures) == {"Ha": 3, "OIII": 3, "SII": 5}


def test_config_default_off_and_env(tmp_path, monkeypatch):
    proj = ImagingProject(
        id="et", target=CelestialTarget(name="ET", ra_hours=21.6,
                                        dec_degrees=57.5),
        exposure_plans=[_plan(F.HA, 300, 40, acquired=18),
                        _plan(F.OIII, 300, 20),
                        _plan(F.OSC, 120, 100, rig="piggyback")])
    assert _cfg(tmp_path).rc16_min_block_subs == 1
    assert min_block_rule(proj, _cfg(tmp_path)) is None
    monkeypatch.setenv("PS_RC16_MIN_BLOCK_SUBS", "6")
    cfg = _cfg(tmp_path)
    assert cfg.rc16_min_block_subs == 6
    n, done = min_block_rule(proj, cfg)
    assert n == 6 and done == {"Ha": pytest.approx(0.45), "OIII": 0.0}


def test_setting_is_in_the_registry():
    from photonscript.scheduler.app import _CONFIG_FIELDS as reg
    row = next(r for r in reg if r[0] == "rc16_min_block_subs")
    assert row[1] == "PS_RC16_MIN_BLOCK_SUBS" and row[4] == "int"


def test_night_plan_and_shutter_estimate_skip_a_dropped_filter():
    t = NinaSequenceTarget(name="ET", ra_hours=21.6, dec_degrees=57.5,
                           exposures=_et())
    _scale_group(t.exposures, ET_WINDOW_S, 6)
    sets = night_plan._owed_sets(t)
    assert sets == [{"f": "SII", "exp": 300, "n": 12}]
    once, loop = shutter_efficiency._blocks(t)
    assert once == [] and loop == [("SII", [(12, 300.0)])]
    oh = {"af_s": 180.0, "per_sub_s": 10.0, "filter_change_s": 15.0}
    ruled = shutter_efficiency.target_estimate(t, 72 * 60, "every_block", oh)
    legacy = NinaSequenceTarget(name="ET", ra_hours=21.6, dec_degrees=57.5,
                                exposures=_et())
    _scale_group(legacy.exposures, ET_WINDOW_S)
    old = shutter_efficiency.target_estimate(legacy, 72 * 60, "every_block",
                                             oh)
    assert ruled["af_count"] < old["af_count"]
