"""PS-63: goal cards show HDR short subs, their progress, and what the
library count includes.

The Cat's Eye card read "600s x 44, 0 accepted" per filter while the plan
also carried 12 x 60 s shorts on Ha and OIII that the sequence shoots and
the goal hours count, and its "50 accepted lights in library" mixed 47
Piggy-600 OSC frames with 3 RC16 frames. The card now has a short row per
HDR plan, an HDR chip with an edit/clear control, and the library count and
night strip split by rig.
"""
import json
from pathlib import Path

from photonscript.scheduler import readiness, runs, sub_index
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget

CATS_EYE = CelestialTarget(name="Cat's Eye Nebula", catalog_id="NGC 6543",
                           ra_hours=17.976, dec_degrees=66.63,
                           object_type="planetary nebula")


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _cats_eye(cfg):
    store = ProjectStore(cfg)
    proj = store.add_from_target(CATS_EYE, budget_hours=15.0)
    store.update(proj.id, filter_mix={"Ha": 50, "OIII": 50},
                 hdr={"Ha": 60, "OIII": 60})
    p = store.projects[proj.id]
    for e in p.exposure_plans:
        if e.filter_type.value == "OIII":
            e.acquired = 2
            e.acquired_s = 1200.0
            e.hdr_short_acquired = 5
    return store, p


def _touch(d: Path, n: int):
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"f{i}.fits").write_bytes(b"")


def _library(cfg, n_osc=47, n_oiii=2, n_sii=1):
    lib = runs.library_root(cfg) / "Cat's Eye Nebula"
    _touch(lib / "OSC", n_osc)
    _touch(lib / "OIII", n_oiii)
    _touch(lib / "SII", n_sii)
    return lib


def _write(cfg, date, rows):
    (runs.runs_dir(cfg) / f"{date}_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(rig, filt, t, ok=True):
    return {"rig": rig, "file": f"LIGHT/{rig}_{t}.fits",
            "time": f"2026-09-26T0{t}:00:00", "target": "Cat's Eye Nebula",
            "filter": filt, "exp_s": 600 if rig == "rc16" else 120,
            "passed_qa": ok, "reviewed": ok, "reason": ""}


# ---- library count by rig ---------------------------------------------------

def test_library_fits_by_rig_osc_folder_is_the_piggyback(tmp_path):
    readiness._lib_count_cache.clear()
    lib = _library(_cfg(tmp_path))
    assert readiness.library_fits_by_rig(lib) == {"piggyback": 47, "rc16": 3}
    assert readiness.library_fits_count(lib) == 50
    assert readiness.library_fits_by_rig(tmp_path / "nope") == {}


def test_api_projects2_splits_library_and_nights_by_rig(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    readiness._lib_count_cache.clear()
    cfg = _cfg(tmp_path)
    store, proj = _cats_eye(cfg)
    _library(cfg)
    _write(cfg, "2026-09-25", [_row("rc16", "OIII", 1), _row("rc16", "OIII", 2, ok=False),
                               _row("piggyback", "OSC", 3), _row("piggyback", "OSC", 4)])
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", store)
    d = next(x for x in app.api_projects2() if x["id"] == proj.id)
    assert d["library_files"] == 50
    assert d["library_by_rig"] == [
        {"rig": "rc16", "label": "RC16", "n": 3},
        {"rig": "piggyback", "label": "Piggy-600", "n": 47}]
    n = d["nights"][0]
    assert (n["date"], n["accepted"], n["attempted"]) == ("2026-09-25", 3, 4)
    assert n["by_rig"] == {"rc16": {"accepted": 1, "attempted": 2},
                           "piggyback": {"accepted": 2, "attempted": 2}}
    # the card's HDR inputs travel in the same payload
    assert d["hdr"] == {"Ha": 60.0, "OIII": 60.0}
    shorts = {e["filter_type"]: (e["hdr_short_seconds"], e["hdr_short_count"],
                                 e["hdr_short_acquired"])
              for e in d["exposure_plans"]}
    assert shorts["Ha"] == (60.0, 12, 0) and shorts["OIII"] == (60.0, 12, 5)
    # goal hours include the short time (long + short), as allocate does
    assert d["hours_done"] == round((1200 + 5 * 60) / 3600, 1)


def test_api_projects2_without_library_keeps_old_shape(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    readiness._lib_count_cache.clear()
    cfg = _cfg(tmp_path)
    store, proj = _cats_eye(cfg)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", store)
    d = next(x for x in app.api_projects2() if x["id"] == proj.id)
    assert (d["library_files"], d["library_by_rig"]) == (0, [])


# ---- target page goal ---------------------------------------------------------

def test_target_goal_gives_the_short_set_its_own_hours_and_bar(tmp_path):
    cfg = _cfg(tmp_path)
    _, proj = _cats_eye(cfg)
    g = sub_index._goal(proj)
    oiii = next(p for p in g["plans"] if p["filter"] == "OIII")
    assert oiii["rig"] == "rc16"
    hs = oiii["hdr_short"]
    assert (hs["exposure_s"], hs["count"], hs["acquired"]) == (60.0, 12, 5)
    assert hs["hours_goal"] == round(12 * 60 / 3600, 2)
    assert hs["hours_done"] == round(5 * 60 / 3600, 2)
    assert hs["pct"] == round(5 / 12 * 100)
    # the header total is long + short goal seconds
    long_goal = sum(e.count * e.exposure_seconds for e in proj.exposure_plans)
    assert g["hours_goal"] == round((long_goal + 2 * 12 * 60) / 3600, 1)


# ---- card renderer (static) ---------------------------------------------------

def _tpl(name) -> str:
    p = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
         / "templates" / name)
    return p.read_text(encoding="utf-8")


def test_dashboard_card_renders_short_rows_chip_and_rig_split():
    s = _tpl("dashboard.html")
    i = s.index("PS-63: HDR short sets on the goal cards")
    helpers = s[i:s.index("function altChartSVG", i)]
    helpers.encode("ascii")                            # the PS-63 helpers are ASCII
    for fn in ("function hdrShortRow(e, col)", "function hdrChip(p)",
               "async function editHdr(id)", "function libraryText(p)",
               "function nightCounts(n)"):
        assert fn in helpers
    assert "await patchProject(id, {hdr: hdr});" in helpers
    assert "e.hdr_short_seconds" in helpers and "e.hdr_short_acquired" in helpers
    k = s.index("async function loadProjects()")
    card = s[k:s.index("// goal input: commit on Enter/blur", k)]
    # a short row under every active RC16 filter row and every OSC row
    assert "(active ? hdrShortRow(e, col) : '')" in card
    assert "'</div>' + hdrShortRow(e, col);" in card
    assert "p.priority + hdrChip(p)" in card
    assert "onclick=\"editHdr(" in card and "data-hdr-for=" in card
    assert "libraryText(p) +" in card and "nightCounts(n)" in card
    # PS-129's checks still hold: the mix buttons sit before the library count
    tail = card[card.index("filterRows + oscRows +"):card.index("accepted lights in library")]
    assert "(showMix" in tail and "editHdr(" in tail


def test_target_page_short_bar():
    s = _tpl("target.html")
    i = s.index("PS-63: the HDR short set gets its own line and bar")
    block = s[i:s.index("}).join('');", i)]
    block.encode("ascii")
    assert "hs.hours_done" in block and "hsPct" in block
    assert "p.rig !== 'rc16'" in block
