"""Moon-aware nightly allocation: broadband is protected on dark (BB) nights,
deferred on bright (NB) nights, and fills leftover time on intermediate nights."""

from photonscript.shared.models import ExposurePlan, FilterType
from photonscript.scheduler.target_planner import _fit_by_moon, _NB_FILTERS


def _plan(f, s, c):
    return ExposurePlan(filter_type=f, exposure_seconds=s, count=c,
                        gain=200, offset=256)


def _fresh():
    # Narrowband-heavy target with a real broadband set (like Crescent + RGB)
    return [
        _plan(FilterType.HA, 600, 50),
        _plan(FilterType.OIII, 600, 50),
        _plan(FilterType.RED, 180, 20),
        _plan(FilterType.GREEN, 180, 20),
        _plan(FilterType.BLUE, 180, 20),
    ]


def _counts(exps):
    return {e.filter_type.value: e.count for e in exps}


AVAIL = 2 * 3600  # 2 usable hours


def test_dark_night_protects_broadband():
    out = _counts(_fit_by_moon(_fresh(), AVAIL, "BB"))
    # broadband survives with a real set, not crushed to 1
    assert out.get("R", 0) > 3 and out.get("G", 0) > 3 and out.get("B", 0) > 3
    # narrowband still present as fill
    assert out.get("Ha", 0) >= 1 and out.get("OIII", 0) >= 1


def test_bright_night_defers_broadband():
    out = _fit_by_moon(_fresh(), AVAIL, "NB")
    fv = {e.filter_type.value for e in out}
    assert fv and fv.issubset(_NB_FILTERS)  # narrowband only


def test_bright_night_skips_broadband_only_target():
    bb_only = [_plan(FilterType.LUMINANCE, 180, 60),
               _plan(FilterType.RED, 180, 20)]
    assert _fit_by_moon(bb_only, AVAIL, "NB") == []


def test_intermediate_prioritizes_narrowband():
    out = _counts(_fit_by_moon(_fresh(), AVAIL, "NB+OIII"))
    nb_secs = out.get("Ha", 0) * 600 + out.get("OIII", 0) * 600
    bb_secs = (out.get("R", 0) + out.get("G", 0) + out.get("B", 0)) * 180
    assert nb_secs > bb_secs  # narrowband claims the bulk of the night


def test_moon_off_is_uniform_legacy():
    # None tag -> uniform scale; broadband is NOT protected (small share)
    dark = _counts(_fit_by_moon(_fresh(), AVAIL, "BB"))
    legacy = _counts(_fit_by_moon(_fresh(), AVAIL, None))
    assert legacy.get("R", 0) < dark.get("R", 0)
