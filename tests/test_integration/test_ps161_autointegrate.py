"""PS-161: desktop auto-integrate. No PixInsight: integrate / blend / Pushover
are fakes; the Library mirror and the staging root are tmp folders."""

import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.integration import autointegrate as ai
from photonscript.integration import pipeline as pl
from photonscript.integration import pjsr
from photonscript.integration import watch
from tests.test_integration.test_ps31_33_watch import NOW, URL, Fakes, _cand

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _lib(tmp_path, n=3, temp=False):
    lib = tmp_path / "Library"
    d = lib / "Andromeda Galaxy" / "OSC"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"sub{i}.fits").write_bytes(b"x" * (100 + i))
    if temp:
        (d / "~syncthing~sub9.fits.tmp").write_bytes(b"partial")
    return lib


def _opts(tmp_path, lib, **kw):
    wkw = {k: kw.pop(k) for k in ("dry_run",) if k in kw}
    wo = watch.WatchOptions(base_url=URL, staging_root=tmp_path / "Staging", **wkw)
    return ai.AutoOptions(watch=wo, library=lib, **kw)


def _jpg(path: Path, size=(300, 200)):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (40, 20, 10)).save(path, "JPEG")
    return path


class Integrate(Fakes):
    """watch Fakes plus a run that leaves a finished JPG behind."""

    def run_integrate(self, root):
        base = super().run_integrate(root)

        def _run(target, rig, trigger):
            res = base(target, rig, trigger)
            fin = Path(res["run_dir"]) / "out" / "final"
            _jpg(fin / "M31_OSC_final.jpg")
            _jpg(fin / "M31_OSC_hoo.jpg")
            res.update(finish={"ok": True}, ledger_version=1, hours_integrated=2.5)
            return res
        return _run


def _no_blend(target):
    raise RuntimeError("no RC16 master yet")


# --- Syncthing settle ------------------------------------------------------------

def test_sync_temp_names():
    assert ai.is_sync_temp("~syncthing~a.fits.tmp")
    assert ai.is_sync_temp(".syncthing.a.fits.tmp")
    assert not ai.is_sync_temp("a.fits") and not ai.is_sync_temp("~syncthing~a.fits")


def test_snapshot_counts_rig_lights_and_temp_files(tmp_path):
    lib = _lib(tmp_path, n=3, temp=True)
    s = ai.folder_snapshot(lib, "M31", "piggyback")
    assert s["folders"] == ["Andromeda Galaxy"] and s["count"] == 3
    assert s["bytes"] == 303 and len(s["temp"]) == 1
    assert ai.folder_snapshot(lib, "M31", "rc16")["count"] == 0


def test_settled_needs_a_stable_count_for_the_wait(tmp_path):
    lib = _lib(tmp_path)
    state = {}
    snap = ai.folder_snapshot(lib, "M31", "piggyback")
    ok, why = ai.settled(snap, "k", state, 15, NOW)
    assert not ok and "changed" in why
    ok, why = ai.settled(snap, "k", state, 15, NOW + timedelta(minutes=10))
    assert not ok and "stable 10 of 15" in why
    ok, _ = ai.settled(snap, "k", state, 15, NOW + timedelta(minutes=16))
    assert ok
    more = dict(snap, count=4, bytes=999)
    assert not ai.settled(more, "k", state, 15, NOW + timedelta(minutes=17))[0]
    tmp = dict(snap, temp=["OSC/~syncthing~x.tmp"])
    ok, why = ai.settled(tmp, "k", state, 15, NOW + timedelta(minutes=40))
    assert not ok and "Syncthing still copying" in why
    assert not ai.settled({"folders": [], "count": 0, "bytes": 0, "temp": []},
                          "z", state, 15, NOW)[0]
    assert ai.settled(snap, "q", {}, 0, NOW)[0]       # no wait configured


# --- one cycle -------------------------------------------------------------------

def _cycle(o, fk, *, pi=(), sends=None, blends=None, discover=_no_blend, now=NOW):
    sends = [] if sends is None else sends
    blends = [] if blends is None else blends

    def run_blend(t):
        blends.append(t)
        rd = o.watch.staging_root / "Blend" / "M31_blend_x"
        _jpg(rd / "out" / "final" / "M31_blend.jpg")
        return {"run_dir": str(rd), "blend": {"ok": True}}
    return ai.cycle(o, run_integrate=fk.run_integrate(o.watch.staging_root),
                    run_blend=run_blend, discover_blend=discover,
                    send=lambda msg, img: sends.append((msg, img)) or True,
                    get=fk.get, post=fk.post, pi_running=lambda: list(pi),
                    echo=lambda s: None, now=now)


def test_waits_for_syncthing_then_integrates_and_sends_the_review(tmp_path):
    o = _opts(tmp_path, _lib(tmp_path), settle_min=15)
    fk = Integrate([_cand()])
    out = _cycle(o, fk)
    assert fk.runs == [] and not out["decisions"][0]["run"]
    assert "waiting 15 min" in out["decisions"][0]["reason"]
    sends = []
    out = _cycle(o, fk, sends=sends, now=NOW + timedelta(minutes=20))
    assert len(fk.runs) == 1 and "Syncthing settled" in fk.runs[0][2]
    assert out["ran"]["posted"] and fk.posts == ["new_run"]
    review = Path(out["ran"]["review"])
    assert review.name == "review.jpg" and review.is_file()
    from PIL import Image
    assert Image.open(review).width == 600            # natural + HOO side by side
    assert len(sends) == 1 and sends[0][1] == review
    assert sends[0][0].startswith("Integrated Andromeda Galaxy [piggyback] v1: 2.5 h")
    assert not (o.watch.staging_root / watch.LOCK_NAME).exists()


def test_temp_files_hold_the_run(tmp_path):
    o = _opts(tmp_path, _lib(tmp_path, temp=True), settle_min=0)
    fk = Integrate([_cand()])
    out = _cycle(o, fk)
    assert fk.runs == [] and "Syncthing still copying" in out["decisions"][0]["reason"]


def test_nothing_while_pixinsight_is_open(tmp_path):
    o = _opts(tmp_path, _lib(tmp_path), settle_min=0)
    fk = Integrate([_cand()])
    out = _cycle(o, fk, pi=[4242])
    assert out["skipped"] == "pixinsight running" and fk.runs == []


def test_no_reintegration_without_new_data(tmp_path):
    o = _opts(tmp_path, _lib(tmp_path), settle_min=0)
    fk = Integrate([_cand(last={"version": 1}, new_h=0.2)])
    out = _cycle(o, fk)
    assert fk.runs == [] and "< 1 h" in out["decisions"][0]["reason"]


def test_dry_run_writes_and_runs_nothing(tmp_path):
    o = _opts(tmp_path, _lib(tmp_path), settle_min=0, dry_run=True)
    fk = Integrate([_cand()])
    out = _cycle(o, fk)
    assert out["would_run"] == {"target": "Andromeda Galaxy", "rig": "piggyback"}
    assert fk.runs == [] and fk.posts == []
    assert not (o.watch.staging_root / ai.STATE_NAME).exists()


def test_notify_off_sends_nothing(tmp_path):
    o = _opts(tmp_path, _lib(tmp_path), settle_min=0, notify=False)
    sends = []
    _cycle(o, Integrate([_cand()]), sends=sends)
    assert sends == []


# --- blend -----------------------------------------------------------------------

def _inputs(tmp_path):
    osc = tmp_path / "Staging" / "M31_piggyback" / "out" / "master" / "master_OSC.xisf"
    rc = tmp_path / "Staging" / "M31_rc16" / "out" / "master" / "master_L.xisf"
    return SimpleNamespace(osc=osc, rc16={"L": rc})


def test_two_rig_goal_blends_once_per_input_set(tmp_path):
    import json
    o = _opts(tmp_path, _lib(tmp_path), settle_min=0)
    inp = _inputs(tmp_path)
    cands = [_cand(last={"version": 1}), {**_cand(rig="rc16", last={"version": 1})}]
    blends, sends = [], []
    out = _cycle(o, Integrate(cands), blends=blends, sends=sends,
                 discover=lambda t: inp)
    assert blends == ["Andromeda Galaxy"] and out["blend"]["ok"]
    assert sends and sends[-1][0].startswith("Blend Andromeda Galaxy: done")
    # the blend folder records its inputs: the same inputs never blend again
    man = {"target": "Andromeda Galaxy", "pixinsight": {"ok": True},
           "inputs": [{"path": str(inp.osc)}, {"path": str(inp.rc16["L"])}]}
    d = o.watch.staging_root / "Blend" / "M31_blend_x"
    (d / "manifest.json").write_text(json.dumps(man), encoding="ascii")
    out = _cycle(o, Integrate(cands), blends=blends, discover=lambda t: inp)
    assert blends == ["Andromeda Galaxy"] and out["blend"] is None


def test_blend_skipped_when_a_rig_has_no_master(tmp_path):
    o = _opts(tmp_path, _lib(tmp_path), settle_min=0)
    blends = []
    out = _cycle(o, Integrate([_cand()]), blends=blends)
    assert blends == [] and out["blend"] is None


# --- library mirror path ---------------------------------------------------------

def test_default_library_prefers_d_then_desktop_dir(tmp_path, monkeypatch):
    d = tmp_path / "D" / "ninashare" / "Library"
    c = tmp_path / "C" / "ninashare" / "Library"
    c.mkdir(parents=True)
    monkeypatch.setattr(pl, "MIRROR_D", d)
    cfg = SimpleNamespace(integration_library_dir="", desktop_library_dir=str(c))
    assert pl.default_library(cfg) == c
    d.mkdir(parents=True)
    assert pl.default_library(cfg) == d
    cfg.integration_library_dir = str(tmp_path / "X")
    assert pl.default_library(cfg) == tmp_path / "X"
    assert pl.config_options(SimpleNamespace(
        integration_library_dir=str(tmp_path / "X"), desktop_library_dir="",
        observatory_bortle=2, observatory_tz="America/Denver", observatory_name="AARO",
        observatory_lat=31.9, observatory_lon=-109.0, observatory_elev=1300,
        camera_readout_mode="HCG", piggyback_readout_mode="LCG"),
        "piggyback")["library"] == tmp_path / "X"


# --- OSC HOO in the finish (PJSR rules) ------------------------------------------

def test_render_finish_hoo_switch():
    base = {"out": "D:/X/out", "ra_deg": None, "dec_deg": None,
            "masters": [{"name": "M31_OSC", "path": "D:/X/out/master/master_OSC.xisf"}]}
    assert 'var HOO = "off";' in pjsr.render_finish(base)
    on = pjsr.render_finish({**base, "hoo": "on"})
    assert 'var HOO = "on";' in on and pjsr.check(on) == []


def _js():
    return (DEPLOY / "finish_osc.js").read_bytes().decode("ascii")


def test_hoo_is_guarded_and_ordered():
    s = _js()
    main = s[s.index("function main()"):]
    assert main.index("colorCalibrate(v, solved)") < main.index("hooSource(v)") \
        < main.index("removeGreen(v)")
    assert main.index('"_final.jpg", "jpg")') < main.index("hooFinish(hooWin)")
    fin = s[s.index("function hooFinish(win)"):s.index("function frameCrop(")]
    assert fin.index("try {") < fin.index("new PixelMath")
    assert "win.forceClose()" in fin and 'step("hoo", "PixelMath", "failed"' in fin
    assert '"$T[0]"' in fin and "($T[1] + $T[2]) / 2" in fin
    src = s[s.index("function hooSource(view)"):s.index("function hooFinish(win)")]
    assert 'HOO !== "on"' in src and "!view.image.isColor" in src
    for n, ln in enumerate(s.splitlines(), 1):
        if "//" in ln:
            assert "/*" not in ln.split("//", 1)[1], n


def test_ps1_hoo_param_default_off():
    ps1 = (DEPLOY / "run-finish-osc.ps1").read_bytes().decode("ascii")
    assert "[ValidateSet('on','off')][string]$Hoo = 'off'" in ps1
    assert "Replace('__HOO__', $Hoo)" in ps1
    assert pjsr.FINISH_DEFAULTS["hoo"] == "off"


# --- install script + Pushover attachment + config --------------------------------

def test_install_script_is_ascii_and_one_instance():
    s = (DEPLOY / "install-autointegrate-task.ps1").read_bytes().decode("ascii")
    assert "autointegrate --once" in s and "-MultipleInstances IgnoreNew" in s
    assert "-LogonType Interactive" in s and "-Uninstall" in s and "-DryRun" in s
    assert not re.search(r"\$args\b", s)          # PowerShell automatic variable
    assert chr(0x2014) not in s


def test_pushover_attachment_file(tmp_path):
    from photonscript.shared import pushover as po
    j = _jpg(tmp_path / "r.jpg")
    name, data, mime = po._attachment_file(j)
    assert name == "r.jpg" and mime == "image/jpeg" and data
    assert po._attachment_file(tmp_path / "missing.jpg") is None
    big = tmp_path / "big.jpg"
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    assert po._attachment_file(big) is None


@pytest.mark.asyncio
async def test_notify_passes_the_attachment_through(tmp_path, monkeypatch):
    from photonscript.shared import pushover as po
    seen = []

    async def fake(config, message, title, priority, sound, attachment=None):
        seen.append(attachment)
        return True
    monkeypatch.setattr(po, "_send_raw", fake)
    cfg = SimpleNamespace(pushover_ratelimit_enabled=False, data_dir=tmp_path)
    await po.notify(cfg, "m", "t", attachment="x.jpg")
    await po.notify(cfg, "m2", "t")
    assert seen == ["x.jpg", None]


def test_config_keys_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    names = {f[0] for f in _CONFIG_FIELDS}
    for k, v in (("integration_library_dir", ""), ("autointegrate_settle_min", 15.0),
                 ("autointegrate_blend", True), ("autointegrate_notify", True),
                 ("autointegrate_hoo", True)):
        assert getattr(c, k) == v and k in names


def test_cli_registers_autointegrate():
    from typer.testing import CliRunner
    from photonscript.cli import app
    r = CliRunner().invoke(app, ["autointegrate", "--help"])
    assert r.exit_code == 0 and "--dry-run" in r.output and "--settle-min" in r.output
