"""Fix 2026-10-13: the PS-144 focus-offset alert fired on ONE 2026-07-06 Ha
pair (-106, an L AF up to 90 min away) against the configured +120 while
the focus model said +121. offset_check() now measures with the focus
table's pooled pairs (focus_table.pair_offsets: the L AF at most 30 min
before, within 1 C) over the last OFFSET_ALERT_RECENT_DAYS of AFs, and
alerts only with at least OFFSET_ALERT_MIN_PAIRS of them."""
from photonscript.scheduler import focus_model as fm
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    kw.setdefault("focus_cfz_steps", 110)
    return PhotonScriptConfig(_env_file=None, **kw)


def _pt(f, pos, time, temp=10.0):
    return {"filter": f, "position": pos, "temp": temp, "time": time,
            "step": 110, "rig": "rc16"}


# the 07-06 shape: the Ha AF 80 min after the L AF (a 90-min pair, not a
# 30-min one), then three months of L / Ha AFs that never pair
JULY = [_pt("L", 5900, "2026-07-06T22:00:00-06:00"),
        _pt("Ha", 5794, "2026-07-06T23:20:00-06:00")]
LATER = [_pt("L", 5700, f"2026-10-{d:02d}T20:00:00-06:00") for d in range(1, 13)] + \
        [_pt("Ha", 5821, f"2026-10-{d:02d}T23:30:00-06:00") for d in range(1, 13)]


def _pairs(night, delta, temp_ha=10.0, gap_min=10):
    return [_pt("L", 5700, f"{night}T20:00:00-06:00"),
            _pt("Ha", 5700 + delta, f"{night}T20:{gap_min:02d}:00-06:00", temp=temp_ha)]


def test_the_single_july_pair_no_longer_alerts():
    cfg = _cfg(focus_filter_offsets="Ha:120,OIII:120,SII:120")
    # the old measurement: the newest night with a 90-min pair, n=1, -106
    old = fm.pair_offsets(JULY + LATER, "L")
    assert old["Ha"]["steps"] == -106 and old["Ha"]["n"] == 1
    chk = fm.offset_check(cfg, JULY + LATER)
    assert chk["alert"] is False and chk["alerts"] == []
    assert chk["filters"]["Ha"]["measured"] is None       # no 30-min pair recently
    assert chk["min_pairs"] == fm.OFFSET_ALERT_MIN_PAIRS == 3


def test_two_recent_pairs_show_but_do_not_alert():
    cfg = _cfg(focus_filter_offsets="Ha:120")
    pts = LATER + _pairs("2026-10-10", -106) + _pairs("2026-10-11", -100)
    chk = fm.offset_check(cfg, pts)
    ha = chk["filters"]["Ha"]
    assert ha["measured"]["n"] == 2 and ha["enough_pairs"] is False
    assert abs(ha["diff"]) > 110 and ha["alert"] is False and chk["alert"] is False


def test_three_recent_pairs_alert(tmp_path):
    cfg = _cfg(tmp_path, focus_filter_offsets="Ha:120")
    pts = LATER + _pairs("2026-10-09", -106) + _pairs("2026-10-10", -100) + \
        _pairs("2026-10-11", -110)
    chk = fm.offset_check(cfg, pts)
    ha = chk["filters"]["Ha"]
    assert ha["measured"]["n"] == 3 and ha["measured"]["steps"] == -106
    assert ha["measured"]["night"] == "2026-10-11" and chk["night"] == "2026-10-11"
    assert chk["alerts"] == ["Ha"]
    assert "measured -106 (3 pair(s), 2026-10-11)" in fm.offset_alert_text(chk)


def test_pairs_older_than_the_recent_window_do_not_count():
    cfg = _cfg(focus_filter_offsets="Ha:120")
    old = _pairs("2026-07-06", -106) + _pairs("2026-07-07", -106) + _pairs("2026-07-08", -106)
    chk = fm.offset_check(cfg, old + LATER)
    assert chk["filters"]["Ha"]["measured"] is None and chk["alert"] is False
    # the same three pairs alone are the newest AFs: they count
    assert fm.offset_check(cfg, old)["alerts"] == ["Ha"]


def test_the_30_min_and_1_c_rules():
    cfg = _cfg(focus_filter_offsets="Ha:120")
    far = _pairs("2026-10-09", -106, gap_min=45) + _pairs("2026-10-10", -106, gap_min=45) \
        + _pairs("2026-10-11", -106, gap_min=45)
    assert fm.offset_check(cfg, far)["filters"]["Ha"]["measured"] is None
    warm = _pairs("2026-10-09", -106, temp_ha=12.0) + _pairs("2026-10-10", -106, temp_ha=12.0) \
        + _pairs("2026-10-11", -106, temp_ha=12.0)
    assert fm.offset_check(cfg, warm)["filters"]["Ha"]["measured"] is None
