"""PS-91 live non-star lock guard: the five detectors.

D2 and D4 replay the PS-88 guide-log fixtures through the guard exactly as
the live ring buffer would feed it (same frame shape): the 2026-09-25 22:15
'star' that sat still must trip D2, the 2026-09-26 Heart session (max pulses,
no motion) must trip D4 as a pulse-path case, and the working sessions must
stay silent. D1, D3 and D5 are synthetic."""
import base64
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import phd2_logs as pl
from photonscript.shared import guide_motion as gm
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import guide_guard as gg
from photonscript.telescope_agent.guide_guard import (
    GuardContext, NonStarLockGuard)

FIX = Path(__file__).parents[1] / "test_scheduler" / "fixtures" / "phd2"
N25 = "PHD2_GuideLog_2026-09-25_192414.txt"
N26 = "PHD2_GuideLog_2026-09-26_120453.txt"


def _cfg(**kw):
    return PhotonScriptConfig(_env_file=None, **kw)


def _replay(name, start):
    """Every verdict code:kind seen while replaying one guiding session."""
    cfg = _cfg()
    secs = pl.parse_guide_log((FIX / name).read_text(encoding="utf-8"), name)
    s = next(x for x in secs if x["kind"] == "guiding" and x["start"].endswith(start))
    h = s["header"]
    scale, _ = pl._scale_for(h, cfg)
    rates = gm.pulse_rates(h, scale)
    g = NonStarLockGuard(cfg)
    seen = set()
    fr = s["frames"]
    for i in range(1, len(fr) + 1):
        now = fr[i - 1]["t"]
        win = [f for f in fr[:i] if f["t"] >= now - 1800]
        for v in g.verdicts(GuardContext(frames=win, app_state="Guiding",
                                         scale=scale, binning=h.get("binning"),
                                         rates_px_s=rates, now=now)):
            seen.add(f"{v.code}:{v.kind}")
    return seen


def test_static_star_0925_trips_d2():
    assert _replay(N25, "22:15:42") == {"D2:non_star"}


def test_heart_0926_trips_d4_as_a_pulse_path_case():
    assert _replay(N26, "21:54:09") == {"D4:pulses_not_moving"}


@pytest.mark.parametrize("name,start", [(N25, "20:53:31"), (N25, "01:36:51"),
                                        (N26, "20:18:08")])
def test_working_sessions_stay_silent(name, start):
    assert _replay(name, start) == set()


def _frames(n, hfd, t0=0.0, dt=2.0, epoch=1, scatter=0.5):
    rng = np.random.default_rng(1)
    return [{"t": t0 + i * dt, "ra": float(rng.normal(0, scatter)),
             "dec": float(rng.normal(0, scatter)), "ra_ms": 0.0, "ra_dir": "",
             "dec_ms": 0.0, "dec_dir": "", "hfd": hfd, "drop": False,
             "settling": False, "epoch": epoch, "output": True}
            for i in range(n)]


def test_d1_needs_low_hfd_and_a_one_pixel_profile():
    g = NonStarLockGuard(_cfg())
    ctx = GuardContext(frames=_frames(12, 0.6), app_state="Guiding", scale=0.25,
                       binning=2, now=24.0)
    assert g.wants_star_image(ctx)
    assert g.verdicts(ctx) == []                       # unconfirmed: no verdict
    ctx.star_peak_frac = 0.9
    v = g.verdicts(ctx)
    assert [x.code for x in v] == ["D1"] and v[0].kind == gg.NON_STAR
    ctx.star_peak_frac = 0.15                          # a real star's spread
    assert g.verdicts(ctx) == []
    real = GuardContext(frames=_frames(12, 4.5), app_state="Guiding", scale=0.25,
                        binning=2, now=24.0)
    assert not g.wants_star_image(real)
    # threshold is given for bin 2 and scales with binning
    assert gg.hfd_threshold(_cfg(), 1) == pytest.approx(3.0)
    assert gg.hfd_threshold(_cfg(), 2) == pytest.approx(1.5)


def _star_image(crop, pos):
    return {"width": crop.shape[1], "height": crop.shape[0], "star_pos": list(pos),
            "pixels": base64.b64encode(crop.astype("<u2").tobytes()).decode()}


def test_peak_fraction_tells_a_hot_pixel_from_a_star():
    yy, xx = np.mgrid[0:15, 0:15]
    star = 500 + 8000 * np.exp(-((xx - 7) ** 2 + (yy - 7) ** 2) / (2 * 1.6 ** 2))
    hot = np.full((15, 15), 500.0)
    hot[7, 7] = 30000
    assert gg.peak_fraction(_star_image(star, (7, 7))) < 0.2
    assert gg.peak_fraction(_star_image(hot, (7, 7))) > 0.9
    assert gg.peak_fraction({"width": 3}) is None


def test_d5_lock_on_a_mapped_hot_pixel_with_binning():
    hp = {"binning": 1, "pixels": [[200, 100, 900.0]]}
    g = NonStarLockGuard(_cfg(), hotpix=hp)
    g.binning = 2
    v = g.feed({"Event": "LockPositionSet", "X": 100.6, "Y": 50.4})
    assert [x.code for x in v] == ["D5"] and v[0].evidence["distance_px"] < 2
    assert g.feed({"Event": "LockPositionSet", "X": 140.0, "Y": 70.0}) == []
    # D5 reported on ticks while guiding on that lock
    g.feed({"Event": "LockPositionSet", "X": 100.0, "Y": 50.0})
    out = g.verdicts(GuardContext(app_state="Guiding", binning=2, now=1.0))
    assert [x.code for x in out] == ["D5"]
    assert g.verdicts(GuardContext(app_state="Looping", binning=2, now=1.0)) == []


def test_d3_debounced_and_never_while_photonscript_holds_phd2():
    g = NonStarLockGuard(_cfg())

    def ctx(now, **kw):
        base = dict(app_state="Guiding", at_park=True, tracking=False, safe=False,
                    armer_active=True, now=now)
        base.update(kw)
        return GuardContext(**base)
    assert g.verdicts(ctx(0.0)) == []
    assert g.verdicts(ctx(60.0)) == []
    v = g.verdicts(ctx(130.0))
    assert [x.code for x in v] == ["D3"] and "mount parked" in v[0].detail
    assert g.verdicts(ctx(200.0, ops_busy=True)) == []   # our own hot-pixel loop
    # looping with no night armed is fine; guiding with none is not
    g2 = NonStarLockGuard(_cfg())
    for t in (0.0, 200.0):
        assert g2.verdicts(GuardContext(app_state="Looping", armer_active=False,
                                        now=t)) == []
    g3 = NonStarLockGuard(_cfg())
    g3.verdicts(GuardContext(app_state="Guiding", armer_active=False, now=0.0))
    v = g3.verdicts(GuardContext(app_state="Guiding", armer_active=False, now=121.0))
    assert v and "no night armed" in v[0].detail
    # healthy: nothing
    g4 = NonStarLockGuard(_cfg())
    for t in (0.0, 300.0):
        assert g4.verdicts(GuardContext(app_state="Guiding", at_park=False,
                                        tracking=True, safe=True,
                                        armer_active=True, now=t)) == []


def test_d4_with_a_hot_pixel_lock_is_a_non_star_case():
    cfg = _cfg()
    secs = pl.parse_guide_log((FIX / N26).read_text(encoding="utf-8"), N26)
    s = next(x for x in secs if x["kind"] == "guiding" and x["start"].endswith("21:54:09"))
    scale, _ = pl._scale_for(s["header"], cfg)
    rates = gm.pulse_rates(s["header"], scale)
    fr = s["frames"]
    lock = (50.0, 60.0)
    g = NonStarLockGuard(cfg, hotpix={"binning": 2, "pixels": [[50, 60, 500.0]]})
    g.binning = 2
    g.feed({"Event": "LockPositionSet", "X": lock[0], "Y": lock[1]})
    kinds = set()
    for i in range(1, len(fr) + 1):
        now = fr[i - 1]["t"]
        for v in g.verdicts(GuardContext(frames=fr[:i], app_state="Guiding",
                                         scale=scale, binning=2, rates_px_s=rates,
                                         now=now)):
            kinds.add(f"{v.code}:{v.kind}")
    assert "D4:non_star" in kinds and "D5:non_star" in kinds
    assert "D4:pulses_not_moving" not in kinds


def test_detect_and_vet_stars_skip_hot_pixels():
    from tests.fakes.fake_phd2 import SimField
    f = SimField.default(n_stars=6)
    img = f.render()
    hp = {"binning": 2, "pixels": [[int(x), int(y), v] for x, y, v in f.hot]}
    mask = gg.hotpix_mask(img.shape, hp, 2)
    stars = gg.detect_stars(img, mask)
    vetted = gg.vet_stars(stars, img.shape, hotpix=hp, binning=2)
    assert vetted, stars
    for s in vetted:
        assert all(abs(s["x"] - hx) > 3 or abs(s["y"] - hy) > 3 for hx, hy, _ in f.hot)
        assert 1.5 <= s["hfd"] <= 10
