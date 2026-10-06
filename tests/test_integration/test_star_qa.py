"""PS-22: star QA on synthetic star tables (no FITS, no sep)."""
import math

import numpy as np
import pytest

from photonscript.integration import star_qa as q

W, H = 3000, 2000


def _field(n=600, seed=0):
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.uniform(40, W - 40, n), rng.uniform(40, H - 40, n)])


def _move(pts, dx, dy, rot_deg=0.0):
    c = np.array([W / 2.0, H / 2.0])
    a = math.radians(rot_deg)
    r = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    return (pts - c) @ r.T + c + np.array([dx, dy])


# ----------------------------------------------------------- doubled stars

def test_doubled_fraction_finds_the_common_offset():
    a = _field(200)
    b = a[:80] + [13.0, 1.0]            # 80 stars have a twin 13 px away
    xy = np.vstack([a, b])
    fr, vec = q.doubled_fraction(xy[:, 0], xy[:, 1])
    assert fr > 0.25
    assert abs(vec[0] - 13) <= 2 and abs(vec[1] - 1) <= 2


def test_doubled_fraction_clean_field_is_low():
    a = _field(300, seed=3)
    fr, _ = q.doubled_fraction(a[:, 0], a[:, 1])
    assert fr < 0.05


def test_doubled_fraction_too_few_stars():
    assert q.doubled_fraction([1, 2], [3, 4]) == (0.0, (0.0, 0.0))


# ----------------------------------------------------------- registration

def test_align_recovers_large_shift_and_rotation():
    ref = _field()
    sub = _move(ref, -412.0, 233.0, rot_deg=0.3)       # sub = ref moved
    al = q.align(sub, ref, (W, H))
    assert al is not None and not al["flipped"]
    back = q.apply_align(al, sub)
    assert np.median(np.hypot(*(back - ref).T)) < 0.2
    assert abs(al["rotation_deg"] + 0.3) < 0.02


def test_align_handles_a_pier_flip():
    ref = _field(seed=5)
    flipped = np.column_stack([(W - 1) - ref[:, 0], (H - 1) - ref[:, 1]]) + [5.0, -3.0]
    al = q.align(flipped, ref, (W, H))
    assert al is not None and al["flipped"]
    assert np.median(np.hypot(*(q.apply_align(al, flipped) - ref).T)) < 0.2


def test_align_returns_none_for_unrelated_fields():
    assert q.align(_field(seed=1), _field(seed=2), (W, H)) is None


def test_match_stats_counts_extras_inside_the_footprint():
    ref = _field()
    ghosts = ref[:150] + [60.0, 40.0]
    sub = np.vstack([ref, ghosts])
    al = q.align(sub, ref, (W, H))
    m = q.match_stats(sub, ref, al, (W, H))
    assert m["coverage"] > 0.95
    assert 0.15 < 1 - m["match_frac"] < 0.25        # ~150 of 750
    assert len(m["extra"]) == pytest.approx(150, abs=10)


# ----------------------------------------------------------- coherence / ghosts

def test_coherence_separates_shared_extras_from_ghosts():
    real_faint = _field(60, seed=9)                  # every sub sees these
    rng = np.random.default_rng(4)
    extras = [real_faint + rng.normal(0, 0.3, real_faint.shape) for _ in range(8)]
    extras.append(_field(60, seed=99))                # one ghosted sub
    c = q.coherence(extras, radius=4.0, min_votes=5)
    assert all(x > 0.9 for x in c[:8])
    assert c[8] < 0.1


def test_coherence_empty_sub_is_nan():
    c = q.coherence([np.zeros((0, 2)), _field(10)], min_votes=1)
    assert math.isnan(c[0])


def test_ghost_offset_reports_the_second_copy_shift():
    ref = _field()
    ghosts = ref[:200] + [-310.0, 95.0]
    g = q.ghost_offset(ghosts, ref)
    assert abs(g["dx"] + 310) < 2 and abs(g["dy"] - 95) < 2
    assert g["explained"] > 0.8


# ----------------------------------------------------------- classification

def _row(i, **kw):
    r = {"file": f"s{i:03d}.fits", "filter": "OSC", "exp": 120.0, "time": f"T{i:03d}",
         "stars": 3000, "hfd_px": 3.5, "ecc": 0.45, "sky_adu": 1000.0, "double_frac": 0.0,
         "double_vec": "", "aligned": True, "coverage": 1.0, "extra_frac": 0.08,
         "coherent_frac": 0.9}
    r.update(kw)
    return r


def test_classify_v4b_rules():
    rows = [_row(i) for i in range(12)]
    rows[1].update(stars=1000)                         # low stars
    rows[2].update(sky_adu=2000.0)                     # bright sky
    rows[3].update(hfd_px=5.5)                         # soft
    rows[4].update(ecc=0.78)                           # trailed
    rows[5].update(double_frac=0.3, double_vec="dx 13 dy 1")
    rows[6].update(aligned=False)                      # not registered
    rows[7].update(extra_frac=0.5, coherent_frac=0.1)  # second star set
    rows[8].update(extra_frac=0.2, coherent_frac=0.95) # deep but coherent: keep
    q.classify(rows)
    act = {r["file"]: (r["action"], r["reason"]) for r in rows}
    assert act["s000.fits"] == ("keep", "stars_ok")
    assert act["s001.fits"][1].startswith("low_stars")
    assert act["s002.fits"][1].startswith("bright_sky")
    assert act["s003.fits"][1].startswith("soft")
    assert act["s004.fits"][1].startswith("trailed")
    assert act["s005.fits"][1].startswith("doubled")
    assert act["s006.fits"][1].startswith("not_registered")
    assert act["s007.fits"][1].startswith("second_star_set")
    assert act["s008.fits"][0] == "keep"


def test_classify_falls_back_to_plain_extra_rule_without_vote():
    rows = [_row(0, extra_frac=0.3, coherent_frac=0.95), _row(1, extra_frac=0.1)]
    q.classify(rows, coherence_ok=False)
    assert rows[0]["action"] == "reject" and "second_star_set" in rows[0]["reason"]
    assert rows[1]["action"] == "keep"


def test_neighbors_stay_inside_the_exposure_group():
    rows = [_row(i, exp=120.0, stars=2000) for i in range(5)]
    rows += [_row(10 + i, exp=400.0, stars=4000) for i in range(5)]
    q.classify(rows)
    assert all(r["action"] == "keep" for r in rows)    # 2000 is not "low" vs 4000


def test_low_coverage_counts_as_not_registered():
    rows = [_row(0), _row(1, coverage=0.1)]
    q.classify(rows)
    assert "not_registered" in rows[1]["reason"]


# ----------------------------------------------------------- assess (end to end on tables)

def _sub_row(i, xy, exp=120.0, stars=None):
    return {"file": f"s{i:03d}.fits", "filter": "OSC", "exp": exp, "time": f"T{i:03d}",
            "stars": stars or len(xy), "hfd_px": 3.5, "ecc": 0.45, "sky_adu": 1000.0,
            "double_frac": 0.0, "double_vec": "", "theta_R": 0.1,
            "_xy": np.asarray(xy, np.float32), "_shape": (W, H)}


def test_assess_flags_a_split_pointing_sub_and_keeps_clean_ones():
    base = _field(500, seed=11)
    faint = _field(40, seed=12)                         # real stars not in the reference
    rng = np.random.default_rng(0)
    rows = []
    rows.append(_sub_row(0, base, exp=400.0, stars=900))        # deep reference
    for i in range(1, 9):
        jit = rng.normal(0, 0.2, base.shape)
        sub = _move(np.vstack([base + jit, faint]), 20.0 * i, -7.0 * i)
        rows.append(_sub_row(i, sub))
    ghost = _move(np.vstack([base, base[:250] + [400.0, 150.0]]), 15.0, 3.0)
    rows.append(_sub_row(9, ghost))
    ref_i = q.assess(rows, q.Thresholds(coherence_min_subs=5, coherence_votes=4))
    assert ref_i == 0
    assert rows[9]["action"] == "reject" and "second_star_set" in rows[9]["reason"]
    assert all(rows[i]["action"] == "keep" for i in range(1, 9)), [r["reason"] for r in rows]


def test_pick_reference_prefers_the_longest_exposure():
    a = _field(300)
    rows = [_sub_row(0, a, exp=120.0, stars=5000), _sub_row(1, a, exp=400.0, stars=3000)]
    assert q.pick_reference(rows, q.Thresholds()) == 1


def test_write_csv_formats(tmp_path):
    rows = [_row(0)]
    q.classify(rows)
    p = tmp_path / "qa.csv"
    q.write_csv(p, rows)
    head, line = p.read_text().splitlines()[:2]
    assert head.split(",")[:6] == ["file", "night", "filter", "exp", "time", "action"]
    assert "keep" in line
