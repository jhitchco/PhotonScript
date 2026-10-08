"""PS-180 + PS-176 together (wave-2026-10-15).

PS-176 builds smart-policy blocks ("<target> filter block (focus by
offset)") and runs a goal's HDR shorts once per visit ("<target> HDR shorts
(once per visit)") before the repeating loop. PS-180 ends every target but
the last at its planned window (handoff TimeCondition on the DSO container
and the imaging loop) and gives leftover time back in a final "<target>
fill (rest of the night)" container. Pinned: the combined sequence lints
clean (focus-offset, af-runner-filter, loop-spin included), the shorts run
once in the main visit only (not again in the fill), and the PS-77
simulator images all three targets plus the fill with no busy loop.
"""

import json

import pytest

from photonscript.scheduler import af_policy
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.models import ExposurePlan, FilterType as F
from tests.test_scheduler.test_ps176_af_policy import LIVE_MODEL
from tests.test_scheduler.test_ps180_target_windows import (
    _Sim, _by_target, _sim_targets, _utc_at_local)
from tests.test_scheduler.test_ps77_safety_stop import _short, _walk

FILL = "Andromeda Galaxy" + nsj.TARGET_FILL_SUFFIX
HEART_OFF = 7 * 3600 + 42 * 60      # 04:00 local, from the sim's 20:18 dusk


@pytest.fixture
def smart_night(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: {
        "available": True, "down_at_dusk": False, "illum_pct": 5})
    monkeypatch.setattr(af_policy, "_model", lambda cfg: LIVE_MODEL)
    monkeypatch.setenv("PS_RC16_AF_POLICY", "smart")
    ts = _sim_targets(handoffs=True)
    m31 = ts[0]
    # the 10-07 M31 goal owed 12 x 30 s HDR shorts per R/G/B
    m31.exposures = [
        ExposurePlan(filter_type=e.filter_type, exposure_seconds=300,
                     count=e.count, gain=200, offset=256,
                     **({"hdr_short_seconds": 30.0, "hdr_short_count": 12}
                        if e.filter_type != F.LUMINANCE else {}))
        for e in m31.exposures]
    ts[2].handoff_utc = _utc_at_local(4, 0)
    m31.fill_from_utc = ts[2].handoff_utc
    m31.fill_end_utc = _utc_at_local(5, 50)
    return json.loads(nsj.generate_nina_json(
        build_sequence_for_night("PS180_PS176", ts)))


def _dso(seq):
    return [d for d in _walk(seq)
            if _short(d.get("$type", "")) == "DeepSkyObjectContainer"]


def test_smart_blocks_hdr_shorts_and_windows_lint_clean(smart_night):
    r = lint(smart_night, guided=True)
    assert r.ok, [f.detail for f in r.findings if f.level == "ERROR"]
    assert not [f for f in r.findings if f.rule in (
        "loop-spin", "focus-offset", "af-runner-filter")]
    names = [d["Name"] for d in _dso(smart_night)]
    assert names == ["Andromeda Galaxy", "NGC 604", "Heart Nebula", FILL]
    smart = [d for d in _walk(smart_night) if str(d.get("Name", "")).endswith(
        nsj.TARGET_SMART_BLOCK_SUFFIX)]
    assert smart                                   # PS-176 smart policy on
    by = {d["Name"]: d for d in _dso(smart_night)}

    def has(node, suffix):
        return any(str(x.get("Name", "")).endswith(suffix) for x in _walk(node))

    # HDR shorts once, in the main M31 visit only
    assert has(by["Andromeda Galaxy"], nsj.TARGET_HDR_SHORTS_SUFFIX)
    assert not has(by[FILL], nsj.TARGET_HDR_SHORTS_SUFFIX)
    assert has(by[FILL], nsj.TARGET_SMART_BLOCK_SUFFIX)

    def handoffs(d):
        return [(c["Hours"], c["Minutes"]) for c in d["Conditions"]["$values"]
                if _short(c["$type"]) == "TimeCondition"
                and ".DateTimeProvider.TimeProvider," in
                c["SelectedProvider"]["$type"]]

    assert handoffs(by["Andromeda Galaxy"]) == [(23, 18)]
    assert handoffs(by["NGC 604"]) == [(2, 18)]
    assert handoffs(by["Heart Nebula"]) == [(4, 0)]
    assert handoffs(by[FILL]) == []


def test_sim_images_all_three_targets_and_the_fill(smart_night):
    sim = _Sim(smart_night, tick=0.002, max_steps=3_000_000).run()
    assert sim.spins() == {}, sim.spins()
    got = _by_target(sim)
    assert set(got) == {"Andromeda Galaxy", "NGC 604", "Heart Nebula", FILL}
    shorts = [x for x in got["Andromeda Galaxy"]
              if x["end"] - x["start"] < 60]
    assert len(shorts) == 36                       # 12 x R/G/B, once
    assert not [x for x in got[FILL] if x["end"] - x["start"] < 60]
    assert max(x["end"] for x in got["Andromeda Galaxy"]) <= 3 * 3600
    assert max(x["end"] for x in got["NGC 604"]) <= 6 * 3600
    assert max(x["end"] for x in got["Heart Nebula"]) <= HEART_OFF
    assert min(x["start"] for x in got[FILL]) >= HEART_OFF - 900
    assert len(got["NGC 604"]) >= 12 and len(got["Heart Nebula"]) >= 6
