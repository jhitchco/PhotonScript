"""PS-80 part C: the runs page as a review screen.

Summary / Review tabs (opens on Review when subs await review), a state chip
row (needs review / accepted / rejected / all; default needs review), a grid
sort (time, worst first, HFR, ecc, stars, background), the viewer docked
beside the grid (F toggles full screen), the sub on show outlined in the
grid, and no more burned-in annotate=true circles (viewer.js draws them).
The sort and state buckets live in static/js/runs_grid.js and run under node
when it is installed.
"""
import json
import re
from collections import Counter

from tests.test_scheduler.test_ps115_review_panel import _func, _page, _script
from tests.test_scheduler.test_runs_grid_js import JS, SUBS, _node


# ---------------------------------------------------------------- runs_grid.js

def test_bucket_maps_states_to_the_three_chips():
    r = _node("""
console.log(JSON.stringify(['review', 'accepted', 'syncing', 'rejected', 'offtarget']
  .map(G.bucket)));""")
    assert r == ["review", "accepted", "accepted", "rejected", "rejected"]


SORT_SUBS = [
    {"file": "a.fits", "target": "T", "filter": "L", "time": "2026-10-24T01:00:00Z",
     "hfr": 2.1, "ecc": 0.40, "stars": 900, "background": 300, "score": 85},
    {"file": "b.fits", "target": "T", "filter": "L", "time": "2026-10-24T02:00:00Z",
     "hfr": 3.4, "ecc": 0.62, "stars": 400, "background": 500, "score": 41},
    {"file": "c.fits", "target": "T", "filter": "L", "time": "2026-10-24T03:00:00Z",
     "hfr": None, "ecc": 0.55, "stars": 650, "background": None, "score": None},
    {"file": "d.fits", "target": "T", "filter": "L", "time": "2026-10-24T00:30:00Z",
     "hfr": 2.8, "ecc": 0.62, "stars": 120, "background": 410, "score": 70},
]


def test_sort_inside_groups_worst_first_missing_last():
    r = _node(f"""
const subs = {json.dumps(SORT_SUBS)};
const order = k => G.flat(G.group(subs, {{cmp: G.sortCmp(k, s => s.score)}})).map(s => s.file);
console.log(JSON.stringify({{time: order('time'), hfr: order('hfr'), ecc: order('ecc'),
  stars: order('stars'), bkg: order('bkg'), worst: order('worst'), bogus: order('nope'),
  dflt: G.flat(G.group(subs)).map(s => s.file)}}));""")
    assert r["time"] == r["dflt"] == r["bogus"] == ["d.fits", "a.fits", "b.fits", "c.fits"]
    assert r["hfr"] == ["b.fits", "d.fits", "a.fits", "c.fits"]      # highest first, null last
    assert r["ecc"] == ["d.fits", "b.fits", "c.fits", "a.fits"]      # tie in time order
    assert r["stars"] == ["d.fits", "b.fits", "c.fits", "a.fits"]    # fewest first
    assert r["bkg"] == ["b.fits", "d.fits", "a.fits", "c.fits"]
    assert r["worst"] == ["b.fits", "d.fits", "a.fits", "c.fits"]    # lowest score first


def test_sort_keeps_rig_and_group_order():
    r = _node(f"""
const secs = G.group({json.dumps(SUBS)}, {{cmp: G.sortCmp('hfr')}});
console.log(JSON.stringify(secs.map(s => s.groups.map(g => g.target + '|' + g.filter))));""")
    assert r == [["Andromeda Galaxy|L", "NGC 604|L", "NGC 604|Ha", "?|L"], ["NGC 604|OSC"]]


def test_runs_grid_js_has_no_em_dash():
    assert chr(0x2014) not in JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------- page

def test_tabs_and_new_controls_exist_once(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    static = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    ids = re.findall(r'\bid="([^"]+)"', static)
    assert not [k for k, n in Counter(ids).items() if n > 1]
    for k in ("nightTabs", "tabSummary", "tabReview", "tabReviewN", "stateChips",
              "subSort", "subSortBox", "backfillBox", "thumbWarmBox"):
        assert k in ids, k
    # Summary holds the night report, Review holds the subs
    summ = static[static.index('id="tabSummary"'):static.index("/tabSummary")]
    rev = static[static.index('id="tabReview"'):static.index("/tabReview")]
    for k in ("scoreTotal", "pvaTable", "calibration", "phases", "bundleBtn", "regradeBtn"):
        assert f'id="{k}"' in summ and f'id="{k}"' not in rev, k
    for k in ("thumbStrip", "subTable", "reviewCounts", "approveBtn", "filterChips"):
        assert f'id="{k}"' in rev, k
    # progress boxes stay visible on either tab
    assert static.index('id="backfillBox"') < static.index('id="nightTabs"')
    for v in ("time", "worst", "hfr", "ecc", "stars", "bkg"):
        assert f'<option value="{v}">' in static, v


def test_annotate_overlay_retired_from_grid_and_preview(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    assert "annotateToggle" not in html and "annotate=true" not in html
    js = _script(html)
    assert "&w=1400';" in _func(js, "previewUrl")


def test_tab_state_chips_sort_and_dock_wiring(tmp_path, monkeypatch):
    js = _script(_page(tmp_path, monkeypatch))
    # opens on Review when a sub awaits review; a reload with keepView keeps the tab
    assert "nightTab = d.subs.some(s => subState(s) === 'review') ? 'review' : 'summary';" in js
    assert "subStateF = null; nightTab = null;" in _func(js, "loadDetail")
    show = _func(js, "showTab")
    assert "'tabSummary').hidden = t !== 'summary'" in show
    # state chips: default needs review while any sub awaits it, else all
    rs = _func(js, "renderSubs")
    assert "subStateF = d.subs.some(s => subState(s) === 'review') ? 'review' : 'ALL';" in rs
    assert "RunsGrid.bucket(subState(s)) === subStateF" in rs
    assert "#stateChips .fchip" in rs
    assert "cmp: RunsGrid.sortCmp(subSortKey, subScore)" in rs
    assert "const shown = RunsGrid.flat(sections);" in rs
    # the state chip alone is not a scope for "Approve shown"
    assert "const scoped = shown.length !== rev;" in _func(js, "renderReviewBar")
    # docked viewer: F toggles, remembered, narrow screens full screen
    assert "if (k === 'f') toggleDock();" in js
    assert "localStorage.setItem('runsLbDocked'" in _func(js, "toggleDock")
    assert "window.innerWidth >= 1200" in _func(js, "applyDock")
    ob = _func(js, "openLb")
    assert "applyDock();" in ob and "markCur(true);" in ob
    assert "applyDock(); markCur();" in _func(js, "closeLb")
    assert "showTab('review'); openLb(j);" in js          # Enter from Summary
