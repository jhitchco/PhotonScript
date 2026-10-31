"""PS-183: a re-dispatch before the armed recipe started must not drop it.

2026-10-09: armed with tpoint_mapping_then_tonight (10 points); a PHD2
recalibration re-dispatch during startup ("stop ok; guider stop FAILED;
re-dispatch ok") left the TPoint container out because PS-181 treated
"dispatched" as done. Now the re-dispatch reads NINA's state first: the
recipe is left out only when its container is RUNNING / FINISHED (or
anything in it ran, or a TPoint point container FINISHED); CREATED or
absent puts it back to "armed" and this re-dispatch splices it again.
"""
from pathlib import Path

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import tpoint_mapping as tm
from tests.test_scheduler.test_ps181_arm_recipe import (  # noqa: F401 - fixture
    TPM, TT, _arm_recipe, _dispatched, _dsos, _events, armer)

ROOT = Path(__file__).resolve().parents[2]
TPM_NAME = "TPoint mapping 10 points"


@pytest.fixture
def tpm(armer, monkeypatch):
    monkeypatch.setattr(tm, "moon_track", lambda *a, **k: None)
    return armer


def _points_tree(container_status, point_status="CREATED", n=10,
                 container=TPM_NAME):
    pts = [{"Name": f"TPoint point {i:02d}/{n:02d} alt 45.0 az 120.0 (east)"
                    "_Container",
            "Status": point_status if i == 1 else "CREATED"}
           for i in range(1, n + 1)]
    items = []
    if container:
        items.append({"Name": container + "_Container",
                      "Status": container_status,
                      "Items": {"$values": [
                          {"Name": "Slew_Container", "Status": "CREATED"},
                          *pts]}})
    items.append({"Name": "Heart Nebula_Container", "Status": "CREATED"})
    return {"Response": [{"Name": "Targets_Container", "Status": "RUNNING",
                          "Items": items}]}


async def _first(a, rid=TPM, opts=None):
    _arm_recipe(a, rid, opts if opts is not None else
                ({"points": 10} if rid == TPM else None))
    assert await a._dispatch_and_start() is True
    assert a.recipe["status"] == "dispatched"
    return a.recipe["container"]


async def test_10_09_recal_redispatch_before_the_mapping_started(tpm, tmp_path):
    """The 10-09 shape: dispatched, PHD2 recal re-dispatch while the TPoint
    container (and every point) is still CREATED: the re-dispatched
    sequence carries the TPoint container again."""
    a = tpm
    cont = await _first(a)
    assert cont == TPM_NAME
    assert _dsos(_dispatched(a))[0] == TPM_NAME
    a.tree = _points_tree("CREATED")
    a.calls.clear()
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert a.calls[0] == "state"                  # read before the rebuild
    seq = _dispatched(a)
    assert _dsos(seq)[0] == TPM_NAME and _dsos(seq)[-1] == "Heart Nebula"
    assert a.recipe["status"] == "dispatched"
    assert a.recipe["redispatches"] == 1
    assert "never started" in a.recipe["detail"]
    assert [e["value"] for e in _events(tmp_path)] == [
        "dispatched", "re-included", "dispatched"]


async def test_absent_after_a_nina_restart_is_re_included(tpm):
    a = tpm
    await _first(a)
    a.tree = []                                   # NINA: no sequence loaded
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a))[0] == TPM_NAME
    a.tree = _points_tree("CREATED", container="Some other run")
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a))[0] == TPM_NAME
    assert a.recipe["redispatches"] == 2


async def test_a_finished_point_means_started_and_is_not_repeated(tpm, tmp_path):
    """A stopped NINA run puts the interrupted container back to CREATED;
    the finished first point still shows it ran."""
    a = tpm
    await _first(a)
    a.tree = _points_tree("CREATED", point_status="FINISHED")
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert a.recipe["status"] == "started" and a.recipe["started_utc"]
    # the next re-dispatch: the container is gone, still not repeated
    a.tree = []
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert a.recipe["status"] == "started"
    assert [e["value"] for e in _events(tmp_path)] == ["dispatched", "started"]


@pytest.mark.parametrize("status", ["RUNNING", "FINISHED"])
async def test_running_or_finished_container_is_left_out(tpm, status):
    a = tpm
    await _first(a)
    a.tree = _points_tree(status)
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert a.recipe["status"] == ("started" if status == "RUNNING" else "done")


async def test_an_item_inside_the_container_that_ran_means_started(armer):
    """Any recipe (here the tracking test): a FINISHED instruction inside the
    container counts as started."""
    a = armer
    cont = await _first(a, TT)
    a.tree = {"Response": [{"Name": cont + "_Container", "Status": "CREATED",
                            "Items": [{"Name": "Slew", "Status": "FINISHED"}]}]}
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert a.recipe["status"] == "started"


async def test_tracking_test_never_started_is_re_included(armer):
    a = armer
    cont = await _first(a, TT)
    a.tree = {"Response": [{"Name": cont + "_Container", "Status": "CREATED"}]}
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a))[0] == cont


async def test_unreadable_nina_leaves_it_out(tpm, monkeypatch):
    a = tpm
    await _first(a)

    async def _down():
        a.calls.append("state")
        return None, "ConnectError: refused"
    monkeypatch.setattr(a, "_watch_read_state", _down)
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert a.recipe["status"] == "dispatched"


async def test_first_dispatch_does_not_read_state(tpm):
    a = tpm
    _arm_recipe(a, TPM, {"points": 10})
    a.calls.clear()
    assert await a._dispatch_and_start() is True
    assert "state" not in a.calls


def test_helper_on_dollar_values_and_prefix():
    tree = {"Response": {"Items": {"$values": [
        {"Name": "X_Container", "Status": "CREATED", "Items": []},
        {"Name": "TPoint point 03/10 alt 30.0 az 10.0 (west)_Container",
         "Status": "FINISHED"}]}}}
    assert armer_mod._nina_recipe_ran(tree, "x", "tpoint point")
    assert not armer_mod._nina_recipe_ran(tree, "x", "")
    assert not armer_mod._nina_recipe_ran([], "x", "tpoint point")


def test_dashboard_shows_started():
    html = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(
        encoding="utf-8")
    assert "started: 'started, interrupted by a re-dispatch" in html


@pytest.mark.parametrize("rel", [
    "tests/test_scheduler/test_ps183_recipe_redispatch.py", "docs/HANDBOOK.md"])
def test_ascii_no_em_dash(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines()
             if rel != "docs/HANDBOOK.md" or "PS-183" in ln or "re-include" in ln]
    assert not [ln for ln in lines if any(ord(c) > 127 for c in ln)]
