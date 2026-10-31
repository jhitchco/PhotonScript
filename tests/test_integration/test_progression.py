"""Progression sheets (integration/progression.py): add-run from a ledger,
the changed-note diff, the guarded hook, the pending-publish marker, the
built HTML and the seed import. Synthetic run folders only."""

import json
import re
from pathlib import Path

import pytest
from PIL import Image

from photonscript.integration import progression as pg


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("PS_PROGRESSION_SHEETS", "on")


def _jpg(p: Path, color=(120, 40, 60), size=(1600, 1070)) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(p, "JPEG")
    return p


def _steps(bxt=0.25, sat=0.74):
    return [{"file": "x_final_steps.json", "steps": {"finish": {"steps": [
        {"step": "gradient", "tool": "GradientCorrection", "status": "ran", "settings": {}},
        {"step": "color", "tool": "SPCC", "status": "ran", "settings": {}},
        {"step": "deconvolution", "tool": "BlurXTerminator", "status": "ran",
         "settings": {"stars": bxt, "nonstellar": 0.5}},
        {"step": "noise_reduction", "tool": "NoiseXTerminator", "status": "ran", "settings": {"denoise": 0.8}},
        {"step": "hoo", "tool": "PixelMath", "status": "skipped", "settings": {}},
        {"step": "stretch", "tool": "HistogramTransformation", "status": "ran", "settings": {}},
        {"step": "vivid", "tool": "CurvesTransformation", "status": "ran", "settings": {"sat_mid": sat}},
    ]}}}]


def make_run(staging: Path, name: str, *, target="NGC 604", rig="piggyback", subs=None, flats=False,
             bxt=0.25, created="2026-10-24T18:08:57Z", jpgs=("X_OSC_final.jpg", "X_OSC_vivid.jpg"),
             kind="integration", machine_extra=None) -> Path:
    run = staging / name
    subs = subs if subs is not None else [("2026-10-22", 60.0, "OSC")] * 10
    machine = {
        "subs": [{"file": f"s{i}.fits", "night": n, "exp_s": e, "filter": f, "used": True}
                 for i, (n, e, f) in enumerate(subs)]
        + [{"file": "rej.fits", "night": "2026-10-22", "exp_s": 60.0, "filter": "OSC", "used": False}],
        "calibration": {"bias": 50,
                        "darks": [{"light_exp": 60.0, "dark_exp": 60.0, "n": 10, "scaled": False},
                                  {"light_exp": 450.0, "dark_exp": 120.0, "n": 30, "scaled": True}],
                        "flats": [{"filter": "OSC", "n": 30 if flats else 0}]},
        "finish": {"ok": True, "steps": _steps(bxt)},
        "options": {"qa": "apply", "palette": "off", "vivid": True},
    }
    machine.update(machine_extra or {})
    run.mkdir(parents=True)
    (run / "ledger.json").write_text(json.dumps({"campaign": target, "rig": rig, "kind": kind, "run": name,
                                                 "created_at": created, "machine": machine}), encoding="utf-8")
    for j in jpgs:
        _jpg(run / "out" / "final" / j)
    return run


# ----------------------------------------------------------------- add-run

def test_add_run_from_ledger(tmp_path):
    staging = tmp_path / "Staging"
    run = make_run(staging, "NGC_604_piggyback_20261024-1341",
                   subs=[("2026-10-07", 300.0, "OSC")] * 2 + [("2026-10-14", 450.0, "OSC")] * 6
                   + [("2026-10-22", 60.0, "OSC")] * 99 + [("2026-10-23", 60.0, "OSC")] * 136)
    root = pg.store_root(staging)
    assert root == tmp_path / "progression"
    store, e = pg.add_run(root, run)
    assert store.slug == "m33"                       # NGC 604 lives on the M33 sheet
    assert e["id"] == "01" and e["lineage"] == "Piggy wide" and e["rig"] == "Piggy-600 OSC"
    d = e["data"]
    assert d["subs"] == 243 and d["hours"] == 4.83
    assert d["nights"] == ["2026-10-07", "2026-10-14", "2026-10-22", "2026-10-23"]
    assert d["lengths"] == "60 s (235), 300 s (2), 450 s (6)"
    assert e["calibration"] == "bias 50 + matched 60 s darks; 450 s lights use scaled 120 s darks; no flats"
    assert "BlurX" in e["processing"] and "vivid" in e["processing"] and "HOO" not in e["processing"]
    assert e["date_made"] == "2026-10-24" and e["source"] == str(run)
    thumb = Image.open(store.dir / e["file"])
    assert thumb.width == pg.THUMB_PX                # 1000 px thumbnail of the vivid JPG
    assert store.meta["artifact_url"] == "https://claude.ai/artifact/3YtZccfhp2spWjz7HrQ2rc"
    # re-adding the same run keeps its id
    store2, e2 = pg.add_run(root, run)
    assert len(store2.versions) == 1 and e2["id"] == "01"


def test_final_jpg_order(tmp_path):
    r = tmp_path / "r"
    _jpg(r / "out" / "final" / "T_OSC_final.jpg")
    assert pg.final_jpg(r)[0].name == "T_OSC_final.jpg"
    _jpg(r / "out" / "final" / "T_OSC_vivid.jpg")
    assert pg.final_jpg(r)[0].name == "T_OSC_vivid.jpg"
    b = tmp_path / "b"
    _jpg(b / "out" / "final" / "T_blend.jpg")
    _jpg(b / "out" / "final" / "T_core.jpg")
    main, core = pg.final_jpg(b)
    assert main.name == "T_blend.jpg" and core.name == "T_core.jpg"
    assert pg.final_jpg(tmp_path / "none") == (None, None)


def test_mono_lengths_per_filter(tmp_path):
    run = make_run(tmp_path / "Staging", "M31_rc16_x", target="M31", rig="rc16",
                   subs=[("2026-10-14", 120.0, "L")] * 4 + [("2026-10-14", 120.0, "R")] * 2,
                   jpgs=("M31_L_final.jpg",))
    store, e = pg.add_run(pg.store_root(tmp_path / "Staging"), run)
    assert store.slug == "m31" and e["lineage"] == "RC16 core"
    assert e["data"]["lengths"] == "120 s (L 4, R 2)"


# ----------------------------------------------------------------- changed note

def test_changed_note_more_data_flats_settings(tmp_path):
    staging = tmp_path / "Staging"
    root = pg.store_root(staging)
    a = make_run(staging, "A", subs=[("2026-10-22", 60.0, "OSC")] * 60)
    s, e1 = pg.add_run(root, a)
    assert e1["changed"] == "first M33 / NGC 604 image"
    b = make_run(staging, "B", subs=[("2026-10-22", 60.0, "OSC")] * 60 + [("2026-10-23", 60.0, "OSC")] * 120,
                 flats=True, bxt=0.35)
    s, e2 = pg.add_run(root, b)
    assert "3.0x the data (1 h to 3 h)" in e2["changed"]
    assert "new nights 10-23" in e2["changed"]
    assert "flats added" in e2["changed"]
    assert "new processing settings: deconvolution.stars 0.25 to 0.35" in e2["changed"]
    assert e2["title"] == "Piggy second stack"
    c = make_run(staging, "C", subs=[("2026-10-22", 60.0, "OSC")] * 60 + [("2026-10-23", 60.0, "OSC")] * 120,
                 flats=True, bxt=0.35)
    s, e3 = pg.add_run(root, c)
    assert e3["changed"] == "same data and settings, run again"


def test_changed_note_blend_vs_single(tmp_path):
    staging = tmp_path / "Staging"
    root = pg.store_root(staging)
    osc = make_run(staging, "NGC_604_piggyback_1", subs=[("2026-10-22", 60.0, "OSC")] * 60)
    rc = make_run(staging, "NGC_604_rc16_1", rig="rc16", subs=[("2026-10-14", 120.0, "L")] * 30,
                  jpgs=("NGC_604_L_final.jpg",))
    pg.add_run(root, osc)
    pg.add_run(root, rc)
    blend = make_run(staging / "Blend", "NGC_604_blend_1", kind="blend", jpgs=("NGC_604_blend.jpg", "NGC_604_core.jpg"),
                     machine_extra={"inputs": [{"role": "osc", "run": str(osc)},
                                               {"role": "rc16", "run": str(rc)}],
                                    "mode": "l", "options": {"weight": 0.7}, "subs": []})
    s, e = pg.add_run(root, blend)
    assert e["lineage"] == "Blend" and e["title"] == "Blend 1" and e.get("core_file")
    assert e["changed"].startswith("first two-rig blend")
    assert e["data"]["subs"] == 90 and e["data"]["hours"] == 2.0
    assert e["data"]["lengths"] == "Piggy 60 s, RC16 120 s"
    # the next single-rig Piggy stack is diffed against the Piggy one, not the blend
    osc2 = make_run(staging, "NGC_604_piggyback_2", subs=[("2026-10-22", 60.0, "OSC")] * 120)
    s, e2 = pg.add_run(root, osc2)
    assert "2.0x the data" in e2["changed"]
    blend2 = make_run(staging / "Blend", "NGC_604_blend_2", kind="blend", jpgs=("NGC_604_blend.jpg",),
                      machine_extra={"inputs": [{"role": "osc", "run": str(osc2)},
                                                {"role": "rc16", "run": str(rc)}],
                                     "mode": "l", "options": {"weight": 0.7}, "subs": []})
    s, e3 = pg.add_run(root, blend2)
    assert "two-rig blend again" in e3["changed"] and "data" in e3["changed"]


def test_edited_note_survives_readd(tmp_path):
    staging = tmp_path / "Staging"
    root = pg.store_root(staging)
    run = make_run(staging, "A")
    s, e = pg.add_run(root, run)
    pg.edit_entry(s, e["id"], changed="flats needed", title="Piggy test")
    s, e = pg.add_run(root, run)
    assert e["changed"] == "flats needed" and e["title"] == "Piggy test"
    s, e = pg.add_run(root, run, changed="three dust donuts")
    assert e["changed"] == "three dust donuts"


# ----------------------------------------------------------------- build + pending

def test_html_has_every_frame_and_no_em_dash(tmp_path):
    staging = tmp_path / "Staging"
    root = pg.store_root(staging)
    for i in range(3):
        s, _ = pg.add_run(root, make_run(staging, f"R{i}", subs=[("2026-10-22", 60.0, "OSC")] * (10 + 10 * i)))
    pg.edit_entry(s, "02", changed="dust \u2014 donuts")
    s.meta["notes"].append("a note \u2014 with a dash")
    path, _ = pg.build(s)
    text = path.read_text(encoding="utf-8")
    assert path == root / "m33" / "m33_progression.html"
    assert text.count('<article class="frame"') == 3
    for i in ("01", "02", "03"):
        assert f'<span class="num">{i}</span>' in text
    assert "\u2014" not in text and "&mdash;" not in text
    assert "Frames <b>3</b>" in text
    assert "M33 rendition 01" in text                  # not the old hard-coded "M31 rendition"
    assert len(re.findall(r'src="data:image/jpeg;base64,', text)) == 3
    assert 'data-f="blend"' in text and "prefers-color-scheme: dark" in text and '<dialog id="lb">' in text


def test_pending_marker(tmp_path):
    staging = tmp_path / "Staging"
    root = pg.store_root(staging)
    s, _ = pg.add_run(root, make_run(staging, "A", target="Heart Nebula"))
    path, changed = pg.build(s)
    assert changed
    pend = pg.pending_list(root)
    assert [p["slug"] for p in pend] == ["heart"]
    assert pend[0]["html"] == str(path)
    assert pend[0]["artifact_url"] == "https://claude.ai/artifact/JBqqooj2QLSh45Ys6HJQbe"
    assert json.loads((root / pg.PENDING).read_text(encoding="utf-8"))["targets"]["heart"]["frames"] == 1
    pg.mark_published(root, "heart")
    assert pg.pending_list(root) == []
    s = pg.Store.open(root, "heart")
    _, changed = pg.build(s)                           # same content: not pending
    assert not changed and pg.pending_list(root) == []
    s, _ = pg.add_run(root, make_run(staging, "B", target="Heart Nebula", subs=[("2026-10-23", 60.0, "OSC")] * 30))
    _, changed = pg.build(s)
    assert changed and [p["slug"] for p in pg.pending_list(root)] == ["heart"]


# ----------------------------------------------------------------- hook

def test_hook_adds_and_builds(tmp_path):
    staging = tmp_path / "Staging"
    run = make_run(staging, "A")
    out = pg.after_run({"ledger": str(run / "ledger.json"), "integration": {"ok": True},
                        "finish": {"ok": True}}, staging, echo=lambda s: None)
    assert out and out["slug"] == "m33" and out["pending"]
    assert Path(out["html"]).is_file()


def test_hook_skips_and_never_raises(tmp_path, monkeypatch):
    staging = tmp_path / "Staging"
    run = make_run(staging, "A")
    res = {"ledger": str(run / "ledger.json"), "integration": {"ok": True}}
    lines = []
    assert pg.after_run({**res, "finish": {"ok": False}}, staging, lines.append) is None
    assert pg.after_run({**res, "dry_run": True}, staging, lines.append) is None
    assert pg.after_run(None, staging, lines.append) is None
    (run / "ledger.json").unlink()
    assert pg.after_run(res, staging, lines.append) is None
    assert any("not updated" in s for s in lines)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    run2 = make_run(staging, "B")
    monkeypatch.setattr(pg, "add_run", boom)
    assert pg.after_run({"ledger": str(run2 / "ledger.json")}, staging, lines.append) is None
    assert "progression sheet not updated: disk full" in lines[-1]
    monkeypatch.setenv("PS_PROGRESSION_SHEETS", "off")
    monkeypatch.setattr(pg, "add_run", lambda *a, **k: pytest.fail("hook ran while off"))
    assert pg.after_run({"ledger": str(run2 / "ledger.json")}, staging, lines.append) is None
    assert not (tmp_path / "progression" / "m33" / "versions.json").exists()


def test_pipeline_run_hook_is_guarded(tmp_path, monkeypatch):
    """pipeline.run hands its result to the hook; a hook failure never
    reaches the caller."""
    from photonscript.integration import pipeline as pl
    staging = tmp_path / "Staging"
    run = make_run(staging, "A")
    res = {"ledger": str(run / "ledger.json"), "integration": {"ok": True}}
    monkeypatch.setattr(pl, "_run_ws", lambda o, echo: res)
    seen = []
    monkeypatch.setattr(pg, "add_run", lambda *a, **k: (seen.append(a), (_ for _ in ()).throw(OSError("I: gone")))[1])
    o = pl.Options(target="NGC 604", staging_root=staging, run_pixinsight=False)
    assert pl.run(o, echo=lambda s: None) is res
    assert seen and Path(seen[0][0]) == tmp_path / "progression"


def test_blend_and_finish_entry_points_call_hook(monkeypatch, tmp_path):
    from photonscript.integration import blend as bl
    from photonscript.integration import finish as fn
    calls = []
    monkeypatch.setattr(pg, "after_run", lambda res, root, echo=print: calls.append((res, root)))
    monkeypatch.setattr(bl, "_run", lambda o, echo: {"kind": "blend"})
    monkeypatch.setattr(fn, "_run", lambda o, echo: {"kind": "finish"})
    monkeypatch.setattr(fn, "_run_mono_color", lambda o, echo: {"kind": "color"})
    bl.run(bl.Options(target="M31", staging_root=tmp_path, run_pixinsight=False))
    fn.run(fn.Options(master=tmp_path / "m.xisf", name="n", staging_root=tmp_path, run_pixinsight=False))
    fn.run_mono_color(fn.Options(master=None, name="", mono_run=tmp_path, staging_root=tmp_path, run_pixinsight=False))
    assert [c[0]["kind"] for c in calls] == ["blend", "finish", "color"]
    assert all(c[1] == tmp_path for c in calls)


# ----------------------------------------------------------------- seed import

def test_import_seed_from_published_html(tmp_path):
    staging = tmp_path / "Staging"
    root = pg.store_root(staging)
    s, _ = pg.add_run(root, make_run(staging, "A", target="M31"))
    s, _ = pg.add_run(root, make_run(staging, "B", target="M31", subs=[("2026-09-20", 120.0, "OSC")] * 39))
    s.meta["notes"] = ["Jeremy's call"]
    text = pg.render(s)
    seed = tmp_path / "seed" / "m31"
    seed.mkdir(parents=True)
    (seed / "published.html").write_text("<!doctype html><html><body>\n" + text + "\n</body></html>", encoding="utf-8")
    other = tmp_path / "other"
    imp = pg.import_seed(other, seed)
    assert imp.slug == "m31" and len(imp.versions) == 2
    e = imp.versions[1]
    assert e["data"] == {"subs": 39, "hours": 1.3, "lengths": "120 s", "nights": ["2026-09-20"]}
    assert e["lineage"] == "Piggy wide" and e["changed_edited"]
    assert imp.meta["notes"] == ["Jeremy's call"]
    assert imp.meta["artifact_url"] == "https://claude.ai/artifact/P5vqZKUQW2eQxyRhhaNRcq"
    assert (other / "m31" / e["file"]).read_bytes() == (root / "m31" / s.versions[1]["file"]).read_bytes()
    assert imp.html_path.read_text(encoding="utf-8") == text   # the same sheet again
    assert pg.pending_list(other) == []                        # counts as published


def test_cli_pending_lists_paths(tmp_path):
    from typer.testing import CliRunner
    from photonscript.cli import app
    staging = tmp_path / "Staging"
    root = pg.store_root(staging)
    run = make_run(staging, "A", target="M33")
    r = CliRunner().invoke(app, ["progression-sheet", "--root", str(root), "--add-run", str(run)],
                           env={"COLUMNS": "400"})
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(app, ["progression-sheet", "--root", str(root), "--pending", "--json"],
                           env={"COLUMNS": "400"})
    assert r.exit_code == 0, r.output
    data = json.loads(r.output[r.output.index("{"):])
    assert data["pending"][0]["slug"] == "m33"
    assert data["pending"][0]["html"] == str(root / "m33" / "m33_progression.html")
