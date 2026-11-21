"""Meridian guard — don't open a run on a target that would flip immediately."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from photonscript.scheduler.target_planner import meridian_safe_order


def _t(name):
    return SimpleNamespace(name=name)


def test_meridian_target_is_not_the_opener():
    dark = datetime(2026, 9, 25, 2, 0, 0)
    pairs = [
        (dark + timedelta(minutes=5), _t("meridian")),   # crosses right at start
        (dark - timedelta(hours=2), _t("west")),         # already setting (west)
        (dark + timedelta(hours=3), _t("east")),         # rises later
    ]
    ordered, deferred = meridian_safe_order(pairs, dark, guard_min=20)
    names = [t.name for t in ordered]
    assert names[0] == "west"          # the safe western target opens
    assert names[0] != "meridian"
    assert "meridian" in deferred


def test_no_defer_when_nothing_near_meridian():
    dark = datetime(2026, 9, 25, 2, 0, 0)
    pairs = [(dark - timedelta(hours=1), _t("a")),
             (dark + timedelta(hours=2), _t("b"))]
    ordered, deferred = meridian_safe_order(pairs, dark, 20)
    assert deferred == []
    assert [t.name for t in ordered] == ["a", "b"]


def test_deferred_target_reorders_after_a_clear_one():
    dark = datetime(2026, 9, 25, 2, 0, 0)
    pairs = [(dark, _t("mer")),                          # on meridian -> deferred
             (dark + timedelta(minutes=25), _t("east"))]  # clear of the guard
    ordered, deferred = meridian_safe_order(pairs, dark, 20)
    assert [t.name for t in ordered] == ["east", "mer"]
    assert "mer" in deferred


def test_untimed_target_sorts_last():
    dark = datetime(2026, 9, 25, 2, 0, 0)
    pairs = [(datetime.max, _t("unknown")),
             (dark + timedelta(hours=1), _t("real"))]
    ordered, _ = meridian_safe_order(pairs, dark, 20)
    assert [t.name for t in ordered] == ["real", "unknown"]
