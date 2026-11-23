"""2026-11-23: dashboard equipment status as one tab per rig + status chips.

The old rows were long text lines that ran off a phone screen."""
from pathlib import Path

HTML = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
        / "templates" / "dashboard.html").read_text(encoding="utf-8")
BODY = HTML.split("async function refreshEquip()", 1)[1].split(
    "async function _toggleCooler(", 1)[0]


def test_tabs_and_chips():
    assert 'role="tablist"' in BODY and 'data-equip-tab="' in BODY
    assert 'data-equip-pane="' in BODY
    for label in ("'Camera'", "'Cooler'", "'Dew'", "'Focuser'", "'Mount'",
                  "'Roof'", "'Cover'"):
        assert label in BODY, label
    assert "_equipRow('Camera" not in BODY   # the old text rows are gone


def test_toggles_keep_their_ids_and_the_tab_is_remembered():
    assert "'cool-' + r.rig" in BODY and "'dew-' + r.rig" in BODY
    assert "getElementById('cool-' + r.rig)" in BODY   # wiring unchanged
    assert "localStorage.setItem('equipTab'" in BODY
    assert "localStorage.getItem('equipTab')" in HTML


def test_recipe_select_cannot_widen_the_page():
    tag = HTML.split('<select id="sideloadRecipe"', 1)[1].split(">", 1)[0]
    assert "max-width:100%" in tag
