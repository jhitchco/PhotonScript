"""PS-116: the runs page subs view toggles between the thumbnail Grid and a
Table (small lazy thumbnail, score, verdict and the core stats per sub).

The toggle is remembered per browser (localStorage in try / catch), the
table has its columns with score first, uses the shared SubTiles lazy loader
(2 in flight), opens the same lightbox in table order, needs no per-row API
call, replaces the old second table, and every chip works in both views.
"""
import re
from collections import Counter

from tests.test_scheduler.test_ps115_review_panel import _func, _page, _script


def test_view_ids_exist_once(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    static = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    ids = re.findall(r'\bid="([^"]+)"', static)
    assert not [k for k, n in Counter(ids).items() if n > 1]
    for k in ("viewToggle", "viewGridBtn", "viewTableBtn", "gridView",
              "tableView", "thumbStrip", "subTable", "scoreHint", "thumbCount"):
        assert k in ids, k
    assert "/static/js/sub_tiles.js" in html


def test_grid_table_toggle_and_columns(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    js = _script(html)
    assert 'data-v="grid"' in html and 'data-v="table"' in html
    assert re.search(r"try \{ const v = localStorage\.getItem\('runsSubView'\)", js)
    assert re.search(r"try \{ localStorage\.setItem\('runsSubView', v\); \}", js)
    assert "let subSort = {key: 'score', dir: 1};" in js        # score first
    assert "SubTiles.lazy(tbl, 2)" in js and "data-src=" in js
    keys = re.findall(r"\['(\w+)', '[^']*', '[^']*', ", js[js.index("const SUB_COLS"):])
    for k in ("thumb", "time", "target", "filter", "exp", "score", "verdict", "hfr",
              "fwhm", "ecc", "ecc_bin", "stars", "bkg", "sat", "expo", "rms", "alt",
              "pier", "off"):
        assert k in keys, k
    assert keys.index("score") < keys.index("hfr")
    table = _func(js, "renderSubTable")
    assert "window._shownSubs = rows" in table and "openLb(" in table
    assert "qa-rescore --date" in table and "fetch(" not in table  # no per-row API
    # the old second table is folded in: one #subTable, rendered only here
    assert html.count('id="subTable"') == 1
    assert js.count("getElementById('subTable').innerHTML =") == 1
    # chip clicks only touch their own chips (rig / target chips used to
    # also reset the filter to undefined and empty the view)
    assert "document.querySelectorAll('.fchip')" not in js
