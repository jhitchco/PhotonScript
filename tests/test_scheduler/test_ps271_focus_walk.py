"""PS-271: a failed mid-block autofocus must not pile the filter offset up.

2026-11-22: inside the RC16 Ha blocks the HFR trigger's refocus recipe was
SwitchFilter(L), RunAutofocus, MoveFocuserRelative(+120). The focuser was
already at L focus + 120; each AF failed (no stars), NINA restored the start
position (L + 120) and the recipe added +120 again, before every sub
(5239 to 6679 by 08:39 UTC). The recipe now moves -offset first.
"""

from photonscript.shared.models import FilterType as F
from photonscript.scheduler.nina_sequence_json import _block_af_runner


def _short(t: str) -> str:
    return t.split(",")[0].rsplit(".", 1)[-1]


def _simulate(runner: list, start: int, af_ok: bool, af_best: int) -> int:
    """Focuser position after the recipe. NINA's RunAutofocus moves to the
    fitted best focus on success and back to where it started on failure."""
    pos = start
    for it in runner:
        k = _short(it["$type"])
        if k == "MoveFocuserRelative":
            pos += int(it["RelativePosition"])
        elif k == "RunAutofocus":
            pos = af_best if af_ok else pos
    return pos


def test_runner_undoes_the_offset_before_the_af():
    r = _block_af_runner(F.LUMINANCE, 120, F.HA)
    kinds = [_short(x["$type"]) for x in r]
    assert kinds == ["MoveFocuserRelative", "SwitchFilter", "RunAutofocus",
                     "MoveFocuserRelative", "SwitchFilter"]
    assert [x["RelativePosition"] for x in r
            if _short(x["$type"]) == "MoveFocuserRelative"] == [-120, 120]


def test_failed_afs_do_not_walk_the_focuser():
    r = _block_af_runner(F.LUMINANCE, 120, F.HA)
    pos = 5119 + 120                      # L focus + Ha offset, in the block
    for _ in range(13):                   # 13 failed refocuses, as on 11-22
        pos = _simulate(r, pos, af_ok=False, af_best=0)
    assert pos == 5239


def test_good_af_lands_on_new_focus_plus_offset():
    r = _block_af_runner(F.LUMINANCE, 120, F.HA)
    assert _simulate(r, 5239, af_ok=True, af_best=5150) == 5270


def test_no_offset_no_moves():
    r = _block_af_runner(F.LUMINANCE, 0, F.LUMINANCE)
    assert [_short(x["$type"]) for x in r] == ["SwitchFilter", "RunAutofocus"]
