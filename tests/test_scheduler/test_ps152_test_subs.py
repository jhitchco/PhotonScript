"""PS-152: test subs and calibration targets.

1. Tracking-test (PS-84) subs, like optics-test (PS-148) subs, neither count
   toward nor reset the telescope agent's consecutive-reject streak.
2. The watch guiding fallback (no sideloaded file) skips every test container.
3. Test subs never feed night medians, QA baselines or the night score.
4. The PS-144 dusk focus calibration runs once per night.
5. focus_seeds.harvest_night skips filters with a configured focus offset.
"""
from pathlib import Path

import pytest

from photonscript.shared.target_names import is_test_target

ROOT = Path(__file__).resolve().parents[2]


# ---- shared helper -------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Tracking test Heart Nebula",
    "Tracking test Heart Nebula unguided ladder_Container",
    "Optics test M 2",
    "Optics test M 2 L -300",
    "Optics test M 2 through-focus sweep_Container",
    "Focus calibration M52",
    "Focus calibration NGC 7789_Container",
    "NGC 7789 focus calibration AFs_Container",
    "tracking TEST m 31",
])
def test_test_targets(name):
    assert is_test_target(name) is True


@pytest.mark.parametrize("name", [
    "Heart Nebula", "M 31_Container", "", None, "?",
    "Heart Nebula imaging (repeats while safe and up)_Container",
    "Testudo Nebula", "Optical Ring",
])
def test_imaging_targets(name):
    assert is_test_target(name) is False


# ---- 1. reject streak ----------------------------------------------------------

def test_agent_streak_skips_every_test_target():
    src = (ROOT / "photonscript/telescope_agent/agent.py").read_text(
        encoding="utf-8")
    i = src.index("if is_test_target(target_name):")
    j = src.index("self._consecutive_rejects = 0", i)
    k = src.index("self._consecutive_rejects += 1", i)
    # the test branch comes first and does nothing: no reset, no count
    branch = src[i:min(j, k)]
    assert "pass" in branch and "_consecutive_rejects" not in branch
    assert "is_optics_test(target_name)" not in src
