"""Mono runs: the color images (<target>_LRGB / _RGB / _SHO / _HOO) are the
run's final (ledger, Pushover review JPG, AstroBin packet), the packet's
processing text names the combine, and the scheduler's ledger brief lists
them for the dashboard / target / review pages."""

import json
from pathlib import Path

from photonscript.integration import autointegrate as ai
from photonscript.integration import finish as fn
from photonscript.integration import mono_color as mc
from photonscript.integration import pipeline as pl
from photonscript.scheduler import integrations as integ
from photonscript.shared import ledger as L
from tests.test_integration.test_mono_color import (_fake_runner, _fopts, _m27_opts,  # noqa: F401
                                                    lrgb_lib, mono_run)


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_color_jpgs_order_and_case(tmp_path):
    fin = tmp_path / "out" / "final"
    for n in ("M_27_HOO.jpg", "M_27_SHO.jpg", "M_27_L_final.jpg", "M_27_LRGB.jpg",
              "M_27_LRGB.xisf", "NGC_7000_hoo.jpg", "M_27_LRGB_steps.json"):
        _touch(fin / n)
    assert [p.name for p in mc.color_jpgs(tmp_path / "out")] == ["M_27_LRGB.jpg", "M_27_SHO.jpg",
                                                                 "M_27_HOO.jpg"]
    assert mc.preferred_final(tmp_path / "out", "x").endswith("M_27_LRGB.jpg")
    assert mc.preferred_final(tmp_path / "nothing", "fallback.jpg") == "fallback.jpg"


def test_processing_text_names_the_combine():
    items = [{"kind": "lrgb", "name": "M_27_LRGB"}, {"kind": "sho", "name": "M_27_SHO"},
             {"kind": "hoo", "name": "M_27_HOO"}]
    t = mc.processing_text(items)
    assert "ChannelCombination" in t and "LRGBCombination" in t
    assert "SHO mapping (R = SII, G = Ha, B = OIII)" in t and "HOO mapping (R = Ha, G = B = OIII)" in t
    only = mc.processing_text(items, ["M_27_HOO"])
    assert "HOO" in only and "LRGB" not in only and "SHO" not in only
    assert mc.processing_text(items, []) == ""
    assert "LRGBCombination" not in mc.processing_text([{"kind": "rgb", "name": "X_RGB"}])
    t.encode("ascii")


def test_pipeline_final_packet_and_ledger_prefer_the_color_image(lrgb_lib, tmp_path, monkeypatch):
    out = tmp_path / "Staging" / "m27c"
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner([]))
    res = pl.run(_m27_opts(lrgb_lib, tmp_path, out=out, run_pixinsight=True), echo=lambda s: None)
    led = json.loads(Path(res["ledger"]).read_text())
    assert led["machine"]["final"].endswith("M_27_LRGB.jpg")
    packet = next((out / "astrobin").glob("*_astrobin_packet.md")).read_text("ascii")
    assert "LRGBCombination" in packet and "ChannelCombination" in packet
    assert "| Final image | " in packet and "M_27_LRGB.jpg" in packet
    assert "| Color images | " in packet
    brief = integ.ledger_brief(L.load(Path(res["ledger"])))
    assert [Path(p).name for p in brief["color"]] == ["M_27_LRGB.jpg"]
    assert brief["final"].endswith("M_27_LRGB.jpg")


def test_pipeline_without_color_keeps_the_mono_final(lrgb_lib, tmp_path, monkeypatch):
    out = tmp_path / "Staging" / "m27d"
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner([], make_color=False))
    res = pl.run(_m27_opts(lrgb_lib, tmp_path, out=out, run_pixinsight=True), echo=lambda s: None)
    led = json.loads(Path(res["ledger"]).read_text())
    assert not led["machine"]["final"].endswith("_LRGB.jpg")
    packet = next((out / "astrobin").glob("*_astrobin_packet.md")).read_text("ascii")
    assert "LRGBCombination" not in packet and "| Color images |" not in packet
    assert integ.ledger_brief(L.load(Path(res["ledger"])))["color"] == []


def test_mono_run_finish_ledger_final_is_the_color_image(mono_run, monkeypatch):
    monkeypatch.setattr(fn.runner, "run_script", _fake_runner([]))
    res = fn.run_mono_color(_fopts(mono_run, colors="hoo,lrgb"), echo=lambda s: None)
    led = L.load(Path(res["ledger"]))
    assert led.machine["final"].endswith("M_27_LRGB.jpg")
    assert [Path(p).name for p in integ.color_images(led.machine)] == ["M_27_LRGB.jpg", "M_27_HOO.jpg"]


def test_review_jpg_inputs_put_color_first(tmp_path):
    fin = tmp_path / "out" / "final"
    for n in ("M_27_B_final.jpg", "M_27_G_final.jpg", "M_27_L_final.jpg", "M_27_R_final.jpg",
              "M_27_SHO.jpg", "M_27_LRGB.jpg", "M_27_HOO.jpg"):
        _touch(fin / n)
    names = [p.name for p in ai.finals(tmp_path)]
    assert names[:3] == ["M_27_LRGB.jpg", "M_27_SHO.jpg", "M_27_HOO.jpg"]
    assert len(names) == len({n.lower() for n in names}) == 7          # no duplicate via *_hoo.jpg
    # OSC runs and blends are unchanged
    osc = tmp_path / "osc"
    _touch(osc / "out" / "final" / "NGC_7000_final.jpg")
    _touch(osc / "out" / "final" / "NGC_7000_hoo.jpg")
    assert [p.name for p in ai.finals(osc)] == ["NGC_7000_final.jpg", "NGC_7000_hoo.jpg"]


def test_notice_names_the_color_images():
    res = {"integration": {"ok": True}, "finish": {"ok": True}, "hours_integrated": 2.0,
           "ledger_version": 3, "run_dir": "D:/S/M_27_rc16_x",
           "mono_color": {"made": ["M_27_LRGB", "M_27_SHO"]}}
    t = ai.notice_text("integrate", "M 27", "rc16", res, "goal met")
    assert "color M_27_LRGB, M_27_SHO" in t
    assert "color" not in ai.notice_text("integrate", "M 27", "rc16", dict(res, mono_color=None), "x")


def test_integrations_js_shows_the_color_images():
    js = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler" / "static" / "js"
          / "integrations.js").read_text("ascii")
    assert "colorNames(L)" in js and "['color', c]" in js
