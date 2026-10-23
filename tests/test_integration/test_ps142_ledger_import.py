"""PS-142: `photonscript ledger-import` (one-time import of the pre-PS-33
ledger history) and integrate-watch's processing notice. The import is
read-only on the source folder, posts only with --apply, and never on its
own; the watcher tells the scheduler when a run starts and ends."""

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import ledger_import as li
from photonscript.integration import watch
from photonscript.shared import ledger as L

FIX = Path(__file__).parents[1] / "test_scheduler" / "fixtures" / "ledger" / "M31_OSC3_v01.json"
URL = "http://scope:8100"


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(root)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _osc4(tmp_path) -> Path:
    """A small hand-run staging folder shaped like D:\\...\\M31_OSC4."""
    run = tmp_path / "M31_OSC4"
    lights = ["2026-09-21_03-00-19__0.00_120.00s_0193.fits",
              "2026-09-21_03-02-19__0.00_120.00s_0194.fits",
              "2026-10-04_03-15-34__0.00_400.00s_0058.fits",
              "2026-10-03_22-15-34__0.00_300.00s_0001.fits"]
    m = ['\ufeff"group","file","source","bytes"']
    m += [f'"BIAS","b{i}.fits","x","1"' for i in range(3)]
    m += [f'"DARKS_120s","d{i}.fits","x","1"' for i in range(2)]
    m += ['"FLATS_candidates_warm_0921","f0.fits","x","1"']
    for n in lights:
        grp = "LIGHTS_" + n.split("__0.00_")[1].split(".00s")[0] + "s"
        m.append(f'"{grp}","{n}","C:\\lib\\{n}","1"')
    run.mkdir(parents=True)
    (run / "manifest.csv").write_text("\n".join(m) + "\n", encoding="utf-8")
    (run / "reference.txt").write_text(lights[2] + "\n", encoding="ascii")
    (run / "v4b_120s_selection.csv").write_text(
        "file,v4a_action,v4b_action,v4b_reason\n"
        f"{lights[0]},keep,keep,stars_ok\n"
        f"{lights[1]},keep,reject,doubled(0.30 of stars);not_registered(failed)\n",
        encoding="ascii")
    for v, used in (("v4a_noflat", lights[:1]), ("v4b_noflat", [lights[0]] + lights[2:])):
        d = run / "out" / v
        (d / "final").mkdir(parents=True)
        (d / "weights.csv").write_text(
            "file,wR,wG,wB\n" + "".join(f"{Path(n).stem}_c_cc_d_r,0.4,0.5,0.6\n" for n in used),
            encoding="ascii")
        (d / "final" / f"M31_OSC4_{v}_final.jpg").write_bytes(b"jpg")
    (run / "out" / "pipeline_v4b.log").write_text(
        "2026-10-05T14:07:43.664Z  [+0.0m]  staging\n"
        "2026-10-05T15:21:27.563Z  [+73.7m]  variant v4b_noflat done in 73.7 min; "
        "funnel 3 staged -> 3 integrated\n"
        "2026-10-05T15:21:27.576Z  [+73.7m]  EXIT OK\n", encoding="ascii")
    (run / "out" / "finish_v4b.log").write_text(
        "2026-10-05T15:22:18.452Z  DONE\n2026-10-05T15:22:18.457Z  EXIT OK\n", encoding="ascii")
    ab = run / "astrobin"
    ab.mkdir()
    (ab / "M31_2026-10-04_astrobin_packet.md").write_text(
        "Target: revision of https://app.astrobin.com/i/stnh5q (WIP)\n", encoding="ascii")
    (ab / "M31_2026-10-04_astrobin_acquisition.csv").write_text("date,number\n", encoding="ascii")
    (ab / "M31_OSC4_v4b_crop.jpg").write_bytes(b"jpg")
    return run


def test_names_and_nights():
    assert li.sub_name("2026-09-21_03-00-19__0.00_120.00s_0193_c_cc_d_r") == \
        "2026-09-21_03-00-19__0.00_120.00s_0193.fits"
    assert li.night_of("2026-09-21_03-00-19__0.00_120.00s_0193.fits") == "2026-09-20"
    assert li.night_of("2026-10-03_22-15-34__0.00_300.00s_0001.fits") == "2026-10-03"
    assert li.exp_of("2026-10-04_03-15-34__0.00_400.00s_0058.fits") == 400.0


def test_synthesized_ledger_for_a_hand_run_folder(tmp_path):
    run = _osc4(tmp_path)
    before = _tree_hash(run)
    led, how = li.load_any(run, variant="v4b")
    assert "variant v4b_noflat" in how
    assert (led.campaign, led.rig, led.run, led.version) == ("M31", "piggyback",
                                                              "M31_OSC4_v4b", 4)
    m = led.machine
    assert led.hours == round((120 + 400 + 300) / 3600, 3)
    assert m["acquisition"]["nights"] == ["2026-09-20", "2026-10-03"]
    used = {s["file"]: s["used"] for s in m["subs"]}
    assert used["2026-09-21_03-02-19__0.00_120.00s_0194.fits"] is False and sum(used.values()) == 3
    assert m["calibration"]["status"] == "partial" and m["calibration"]["flats"] == []
    assert m["calibration"]["darks"] == [{"exposure_s": 120.0, "n": 2}]
    assert m["qa"]["kept"] == 1 and m["qa"]["reasons"] == {"doubled": 1, "not_registered": 1}
    assert m["integration"]["ok"] is True and m["integration"]["minutes"] == 73.7
    assert m["finish"]["ok"] is True and m["final"].endswith("M31_OSC4_v4b_crop.jpg")
    assert m["astrobin"]["packet"].endswith("_astrobin_packet.md")
    assert led.publish == {"astrobin": {"status": "packet_ready",
                                        "revision_of": "https://app.astrobin.com/i/stnh5q"}}
    assert m["imported"]["synthesized"] and led.created_at == "2026-10-05T15:22:18Z"
    assert L.headline(led).startswith("Integrated 0.2 h on 2026-10-05 (v4)")
    assert L.parse(led.dump()).dump() == led.dump()                 # valid 0.2
    assert _tree_hash(run) == before                                # read-only
    # newest variant by default; an unknown one is refused
    led2, _ = li.load_any(run)
    assert led2.machine["variant"] in ("v4a_noflat", "v4b_noflat")
    try:
        li.load_any(run, variant="v9")
        raise AssertionError("expected LedgerImportError")
    except li.LedgerImportError as e:
        assert "v4b_noflat" in str(e)


def test_hand_written_v01_ledger_is_upgraded_and_filled(tmp_path):
    d = tmp_path / "M31_OSC3"
    d.mkdir()
    (d / "ledger.json").write_text(FIX.read_text(), encoding="utf-8")
    before = _tree_hash(d)
    led, how = li.load_any(d)
    assert "0.1, upgraded" in how and led.version == 3 and led.run == "M31_OSC3"
    assert led.hours == 1.3 and led.machine["integration"]["ok"] is True
    assert led.machine["astrobin"]["packet"] == str(
        d / "astrobin" / "M31_2026-09-21_astrobin_packet_v3.md")
    assert "acquisition.hours" in led.machine["imported"]["filled"]
    assert led.publish["astrobin"]["url"] == "https://app.astrobin.com/i/stnh5q"
    again, _ = li.load_any(d / "ledger.json")
    assert [a.id for a in again.review.asks] == [a.id for a in led.review.asks]  # stable ids
    assert _tree_hash(d) == before


def test_post_never_writes_back_and_reports_errors(tmp_path):
    led, _ = li.load_any(_osc4(tmp_path), variant="v4b")
    calls = []

    def ok(url, body, timeout):
        calls.append((url, body["run"], body["machine"]["imported"]["by"]))
        return 200, {"ok": True, "version": 4, "headline": "Integrated", "project_id": "p"}
    r = li.post(led, URL, post=ok)
    assert r["ok"] and r["version"] == 4
    assert calls == [(URL + "/api/integrations", "M31_OSC4_v4b",
                      "photonscript ledger-import (PS-142)")]
    assert li.post(led, URL, post=lambda u, b, t: (404, {"detail": "no goal"}))["detail"] == "no goal"

    def down(url, body, timeout):
        raise OSError("down")
    assert li.post(led, URL, post=down)["status"] == 0
    assert li.post(led, "", post=ok)["ok"] is False


def test_cli_dry_run_by_default_and_apply_posts(tmp_path, monkeypatch):
    run = _osc4(tmp_path)
    posted = []
    monkeypatch.setattr(li, "post", lambda led, base, **k: (posted.append(base) or
                                                            {"ok": True, "version": 4,
                                                             "detail": "ok"}))
    runner = CliRunner()
    r = runner.invoke(cli.app, ["ledger-import", str(run), "--variant", "v4b"])
    assert r.exit_code == 0, r.output
    assert "dry run: nothing posted" in r.output and posted == []
    r = runner.invoke(cli.app, ["ledger-import", str(run), "--variant", "v4b", "--apply",
                                "--dry-run"])
    assert r.exit_code == 0 and posted == []                       # --dry-run wins
    r = runner.invoke(cli.app, ["ledger-import", str(run), "--variant", "v4b",
                                "--out", str(run / "copy.json")])
    assert r.exit_code == 2 and not (run / "copy.json").exists()   # read-only source
    out = tmp_path / "elsewhere" / "M31_OSC4_v4b.json"
    r = runner.invoke(cli.app, ["ledger-import", str(run), "--variant", "v4b",
                                "--out", str(out), "--apply", "--url", URL])
    assert r.exit_code == 0, r.output
    assert posted == [URL] and json.loads(out.read_text())["run"] == "M31_OSC4_v4b"
    r = runner.invoke(cli.app, ["ledger-import", str(tmp_path / "missing")])
    assert r.exit_code == 1


# --- integrate-watch processing notice ------------------------------------------------

def _cand():
    return {"project_id": "p1", "target": "Andromeda Galaxy", "catalog_id": "M 31",
            "rig": "piggyback", "goal": {"hours_goal": 6.0, "hours_done": 6.0, "pct": 100},
            "approved_h": 6.0, "approved_subs": 180, "last": None, "new_data_h": 6.0,
            "readiness": {}, "calibration_owed": []}


def test_watch_posts_processing_start_and_end_around_a_run(tmp_path):
    o = watch.WatchOptions(base_url=URL, staging_root=tmp_path / "Staging")
    seen = []

    def post(url, body, timeout):
        seen.append((url.rsplit("/", 1)[-1], body.get("state") or body.get("run")))
        return 200, {"ok": True, "version": 1}

    def run_integrate(target, rig, trigger):
        seen.append(("run", target))
        f = L.save(o.staging_root / "r1" / "ledger.json",
                   L.Ledger(campaign=target, rig=rig, run="r1", version=1))
        return {"run_dir": str(f.parent), "ledger": str(f), "integration": {"ok": True}}
    watch.cycle(o, run_integrate=run_integrate,
                get=lambda u: {"candidates": [_cand()], "thresholds": {}},
                post=post, pi_running=lambda: [], echo=lambda s: None)
    assert seen == [("processing", "start"), ("run", "Andromeda Galaxy"),
                    ("integrations", "r1"), ("processing", "end")]


def test_watch_ends_the_notice_when_the_run_fails_and_survives_a_down_notice(tmp_path):
    o = watch.WatchOptions(base_url=URL, staging_root=tmp_path / "Staging")
    seen = []

    def post(url, body, timeout):
        if url.endswith("/processing"):
            seen.append(body["state"])
            raise OSError("older scheduler or down")
        return 200, {"ok": True}

    def boom(target, rig, trigger):
        raise RuntimeError("PixInsight not found")
    try:
        watch.cycle(o, run_integrate=boom,
                    get=lambda u: {"candidates": [_cand()], "thresholds": {}},
                    post=post, pi_running=lambda: [], echo=lambda s: None)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "PixInsight" in str(e)
    assert seen == ["start", "end"]
