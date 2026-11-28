"""2026-10-24 Imaging Runs grid: static/js/runs_grid.js (grouping, counts,
the in-card badge, the loading line) run under node when it is installed,
plus template checks for the loading / error state and the grouped views."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
JS = ROOT / "static" / "js" / "runs_grid.js"
TILES = ROOT / "static" / "js" / "sub_tiles.js"
RUNS = ROOT / "templates" / "runs.html"

NODE = shutil.which("node")


def _node(script: str):
    if not NODE:
        pytest.skip("node not installed")
    prog = f"const G = require({json.dumps(str(JS))});\n{script}"
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, encoding="utf-8",
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


SUBS = [
    {"file": "LIGHT/p2.fits", "rig": "piggyback", "target": "NGC 604", "filter": "OSC",
     "time": "2026-10-24T03:29:00Z", "exp_s": 60, "passed_qa": True, "reviewed": True},
    {"file": "LIGHT/r2.fits", "target": "NGC 604", "filter": "Ha",
     "time": "2026-10-24T03:23:00Z", "exp_s": 60, "passed_qa": False},
    {"file": "LIGHT/p1.fits", "rig": "piggyback", "target": "NGC 604", "filter": "OSC",
     "time": "2026-10-24T03:27:00Z", "exp_s": 60, "passed_qa": True, "reviewed": False},
    {"file": "LIGHT/r1.fits", "rig": "rc16", "target": "NGC 604", "filter": "Ha",
     "time": "2026-10-24T03:22:00Z", "exp_s": 60, "passed_qa": False,
     "pointing": {"flag": "flag", "off_target_arcmin": 64.77, "off_target_dir": "SW"}},
    {"file": "LIGHT/r3.fits", "rig": "rc16", "target": "Andromeda Galaxy", "filter": "L",
     "time": "2026-10-24T01:00:00Z", "exp_s": 180, "passed_qa": True, "reviewed": True},
    {"file": "LIGHT/r4.fits", "rig": "rc16", "target": "NGC 604", "filter": "L",
     "time": "2026-10-24T02:00:00Z", "exp_s": 180, "passed_qa": True, "reviewed": True},
    {"file": "LIGHT/q.fits", "rig": "rc16", "target": "?", "filter": "L",
     "time": "2026-10-24T00:00:00Z", "exp_s": 180, "passed_qa": True},
]


def test_group_by_rig_then_target_filter_time():
    r = _node(f"""
const secs = G.group({json.dumps(SUBS)});
console.log(JSON.stringify({{
  rigs: secs.map(s => [s.rig, s.label, s.counts.total, s.counts.rejected,
                      s.counts.accepted, s.counts.review]),
  groups: secs.map(s => s.groups.map(g => g.target + '|' + g.filter + '|' + g.subs.length)),
  flat: G.flat(secs).map(s => s.file),
  line: G.countsLine(secs[0].counts)}}));""")
    assert r["rigs"] == [["rc16", "RC16", 5, 2, 2, 1],
                         ["piggyback", "Piggy-600", 2, 0, 1, 1]]
    # targets by name ('?' last), filters in FILTER_ORDER (L before Ha)
    assert r["groups"] == [["Andromeda Galaxy|L|1", "NGC 604|L|1", "NGC 604|Ha|2", "?|L|1"],
                           ["NGC 604|OSC|2"]]
    assert r["flat"] == ["LIGHT/r3.fits", "LIGHT/r4.fits", "LIGHT/r1.fits",
                         "LIGHT/r2.fits", "LIGHT/q.fits", "LIGHT/p1.fits",
                         "LIGHT/p2.fits"]
    assert r["line"].startswith("5 subs · 0.2 h · 2 accepted · 1 to review")


def test_state_function_and_off_target_count():
    r = _node(f"""
const st = s => s.file === 'LIGHT/r2.fits' ? 'offtarget' : (!s.passed_qa ? 'rejected' : 'accepted');
const secs = G.group({json.dumps(SUBS)}, {{stateOf: st}});
console.log(JSON.stringify([secs[0].counts.offtarget, G.countsLine(secs[0].counts)]));""")
    assert r[0] == 1 and "(1 off target)" in r[1]


def test_badge_is_short_and_inside_the_card():
    r = _node("""
console.log(JSON.stringify([
  G.badge({pointing: {flag: 'flag', off_target_arcmin: 64.77, off_target_dir: 'SW'}}),
  G.badge({pointing: {flag: 'off-target', off_target_arcmin: 400, solved_ra: 1}}),
  G.badge({pointing: {flag: ''}}),
  G.badge({qa_flag: 'roof-closed'}),
  G.badge({guide_nocorr: true})]));""")
    flag, off, none, roof, ung = r
    assert flag["cls"] == "flag" and flag["text"] == "off? 65' SW"
    assert "unconfirmed" in flag["title"]
    assert off["cls"] == "off" and off["text"].startswith("off 7")
    assert none is None and roof["text"] == "roof" and ung["text"] == "unguided"
    assert all(len(b["text"]) <= 14 for b in (flag, off, roof, ung))


def test_loading_lines():
    r = _node("""
console.log(JSON.stringify([
  G.loadingText('fetch', {date: '2026-10-23', ms: 700}),
  G.loadingText('body', {date: '2026-10-23', bytes: 1500000, ms: 3200}),
  G.loadingText('draw', {subs: 673}),
  G.loadingText('error', {date: '2026-10-23', error: 'HTTP 500'})]));""")
    assert r[0] == "Loading night 2026-10-23: waiting for the scope PC (0.7 s)"
    assert r[1] == "Loading night 2026-10-23: 1.4 MB received (3.2 s)"
    assert r[2] == "Drawing 673 subs"
    assert r[3] == "Could not load night 2026-10-23: HTTP 500"


def test_template_wires_loading_error_and_groups():
    t = RUNS.read_text(encoding="utf-8")
    assert "runs_grid.js" in t
    for needle in ('id="nightLoading"', 'id="nightLoadingRetry"', "fetchNight(date)",
                   "setNightLoading('error'", "RunsGrid.group(subs",
                   "class=\"rigrow\"", "SubTiles.lazy(strip, 6",
                   "nightLoadAbort.abort()", "Subs not loaded: "):
        assert needle in t, needle
    # the old long caption tags are no longer in the grid card
    card = t[t.index("function subCard("):t.index("// PS-24: a sub's outline")]
    assert "pointTag(" not in card and "RunsGrid.badge(s)" in card
    tiles = TILES.read_text(encoding="utf-8")
    assert "onProgress" in tiles and "visible" in tiles
