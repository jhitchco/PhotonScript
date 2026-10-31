"""2026-10-15 autointegrate end to end: the night's data becomes finished
images with no manual step. No PixInsight: integrate / finish / blend /
Pushover are fakes; staging roots, ledgers and finals are tmp fixtures
shaped like the 2026-10-14 desktop (D:/Astrophotography/Staging, the fast
scratch C:/Astro/Scratch, the legacy C:/Astro/Staging)."""

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.integration import autointegrate as ai
from photonscript.integration import blend as bl
from photonscript.integration import finish as fn
from photonscript.integration import finish_retry as fr
from photonscript.integration import night_summary as ns
from photonscript.integration import pjsr
from photonscript.integration import watch
from photonscript.shared import ledger as L
from tests.test_integration.test_ps31_33_watch import URL, _cand

NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
M31 = "Andromeda Galaxy"


# ----------------------------------------------------------------- fixtures

def _touch(p, n=16):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


def _jpg(p):
    from PIL import Image
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 40), (40, 20, 10)).save(p, "JPEG")
    return p


def _ago(h):
    return (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(root, name, *, rig="piggyback", version=1, campaign=M31, created=None, integ=True,
         fin=True, trigger="integrate-watch", masters=("OSC",), hdr=None, extra=None):
    """An integrate run folder: masters, ledger (+ machine blocks)."""
    d = Path(root) / name
    for m in masters:
        _touch(d / "out" / "master" / f"master_{m}.xisf")
    machine = {"integration": {"ok": integ}, "finish": {"ok": fin} if fin is not None else None,
               "trigger": {"by": trigger, "reason": "test"}, "run_dir": str(d)}
    if hdr:
        machine["hdr"] = hdr
    machine.update(extra or {})
    led = L.Ledger(campaign=campaign, rig=rig, run=name, version=version,
                   created_at=created or _ago(5), reported=True, machine=machine)
    L.save(d / "ledger.json", led)
    return d


def _finish_dir(root, name, run_dir, version, files=(), campaign="M 31", ok=True):
    """A Staging/Finish/<name> folder made from run_dir (kind finish ledger)."""
    d = Path(root) / "Finish" / name
    for f in files:
        (_jpg if f.endswith(".jpg") else _touch)(d / "out" / "final" / f)
    led = L.Ledger(campaign=campaign, rig="piggyback", kind="finish", run=name,
                   machine={"kind": "finish", "finish": {"ok": ok},
                            "input": {"master": str(Path(run_dir) / "out" / "master" / "master_OSC_hdr.xisf"),
                                      "run": str(run_dir), "run_version": version}})
    L.save(d / "ledger.json", led)
    return d


def _osc_final(d, name="Andromeda_Galaxy_OSC"):
    _touch(d / f"{name}_linear.xisf")
    _jpg(d / f"{name}_final.jpg")


# ----------------------------------------------------------------- 1. RC16 watched

def test_scheduler_thresholds_watch_rc16_with_its_own_hours():
    from photonscript.scheduler.integrations import thresholds
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    t = thresholds(c)
    assert t["rigs"] == ["piggyback", "rc16"]
    assert t["per_rig"] == {"rc16": {"new_data_h": 1.0, "first_h": 1.0}}
    c2 = SimpleNamespace(integrate_watch_rigs="piggyback", integrate_watch_rc16_new_data_h=-1,
                         integrate_watch_rc16_first_h=2.0)
    assert thresholds(c2)["per_rig"] == {"rc16": {"first_h": 2.0}}


def test_merged_thresholds_fill_per_rig_for_an_older_scope():
    o = watch.WatchOptions(base_url=URL, staging_root=Path("x"))
    thr = watch.merged_thresholds({"rigs": ["piggyback", "rc16"], "new_data_h": 1.0}, o)
    assert thr["per_rig"]["rc16"] == {"new_data_h": 1.0, "first_h": 1.0}
    assert watch.rig_threshold(thr, "rc16", "first_h") == 1.0
    assert watch.rig_threshold(thr, "piggyback", "first_h") == 0.0
    # a command line --first-h overrides every rig
    o2 = watch.WatchOptions(base_url=URL, staging_root=Path("x"), first_h=3.0)
    thr2 = watch.merged_thresholds({"rigs": ["rc16"]}, o2)
    assert watch.rig_threshold(thr2, "rc16", "first_h") == 3.0


def test_decide_rc16_first_run_at_its_first_hour_piggyback_waits_for_goal():
    o = watch.WatchOptions(base_url=URL, staging_root=Path("x"))
    thr = watch.merged_thresholds({"rigs": ["piggyback", "rc16"]}, o)
    local = {"newest": "", "queued": [], "count": 0}
    heart_rc = _cand(pct=15, approved=1.2, rig="rc16")
    d = watch.decide(heart_rc, thr, local, NOW)
    assert d.run and "first integration" in d.reason
    assert not watch.decide(_cand(pct=15, approved=0.08, rig="rc16"), thr, local, NOW).run
    assert not watch.decide(_cand(pct=15, approved=1.2), thr, local, NOW).run   # piggyback: goal
    # 12 h per goal + rig still holds for the RC16
    recent = {"newest": _ago(3), "queued": [], "count": 1}
    d = watch.decide(_cand(last={"version": 2}, new_h=1.5, rig="rc16"), thr, recent, NOW)
    assert not d.run and "< 12 h" in d.reason
    old = {"newest": _ago(13), "queued": [], "count": 1}
    assert watch.decide(_cand(last={"version": 2}, new_h=1.5, rig="rc16"), thr, old, NOW).run
    assert not watch.decide(_cand(last={"version": 2}, new_h=0.65, rig="rc16"), thr, old, NOW).run


def test_scope_status_chip_uses_the_rc16_first_hour():
    from photonscript.scheduler.integrations import ready_to_process, thresholds
    from photonscript.shared.config import PhotonScriptConfig
    thr = thresholds(PhotonScriptConfig(_env_file=None))
    ok, why = ready_to_process(_cand(pct=15, approved=1.2, rig="rc16"), thr)
    assert ok and "first run at 1 h" in why
    assert not ready_to_process(_cand(pct=15, approved=1.2), thr)[0]


def test_new_config_keys_defaults_and_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    names = {f[0] for f in _CONFIG_FIELDS}
    for k, v in (("integrate_watch_rigs", "piggyback,rc16"), ("integrate_watch_rc16_new_data_h", 1.0),
                 ("integrate_watch_rc16_first_h", 1.0), ("autointegrate_finish_retries", 2),
                 ("autointegrate_idle_min", 10.0), ("autointegrate_max_cpu_pct", 80.0),
                 ("autointegrate_summary_hour", 8.0), ("integration_extra_roots", "C:/Astro/Staging"),
                 ("blend_vivid", True), ("blend_vivid_kinds", "galaxy")):
        assert getattr(c, k) == v, k
        assert k in names, k


# ----------------------------------------------------------------- 2. finish-only retry

def _v6(st, **kw):
    """The 2026-10-14 M31 Piggy v6: integrated (200 min), finish hung."""
    kw.setdefault("masters", ("OSC", "OSC_hdr"))
    return _run(st, "Andromeda_Galaxy_piggyback_20261014-0856", version=6, fin=False, **kw)


def _need(st, **kw):
    f = st / "Andromeda_Galaxy_piggyback_20261014-0856" / "ledger.json"
    return fr.need_for(f, L.load(f), roots=[st], now=NOW, **kw)


def test_v6_without_finals_needs_a_finish_retry(tmp_path):
    st = tmp_path / "Staging"
    d = _v6(st)
    n = _need(st)
    assert n and not n.exhausted and n.kind == "finish" and n.attempt == 1 and n.solve
    assert n.master == d / "out" / "master" / "master_OSC_hdr.xisf"
    assert n.why == "finish failed"


def test_a_hand_refinish_counts_as_finished(tmp_path):
    st = tmp_path / "Staging"
    d = _v6(st)
    _osc_final(d / "out" / "final_v6" / "out" / "final")            # hand --out inside the run
    assert _need(st) is None
    shutil.rmtree(d / "out" / "final_v6")
    _finish_dir(st, "Andromeda_Galaxy_OSC_vivid_v7", d, 6,
                files=("Andromeda_Galaxy_OSC_final.jpg", "Andromeda_Galaxy_OSC_linear.xisf"))
    assert _need(st) is None


def test_retry_bounds_second_without_solve_then_gives_up(tmp_path):
    st = tmp_path / "Staging"
    _v6(st, extra={"finish_retry": {"attempts": [{"attempt": 1, "ok": False}]}})
    n = _need(st)
    assert n.attempt == 2 and not n.solve and not n.exhausted
    _v6(st, extra={"finish_retry": {"attempts": [{"ok": False}, {"ok": False}]}})
    n = _need(st)
    assert n.exhausted and "2 finish-only retries used" in n.why
    assert _need(st, max_retries=0) is None


def test_no_retry_for_old_failed_or_hand_runs(tmp_path):
    st = tmp_path / "Staging"
    _v6(st, created=_ago(80))
    assert _need(st) is None                                    # older than MAX_AGE_H
    _v6(st, integ=False)
    assert _need(st) is None                                    # the integration failed
    _run(st, "Andromeda_Galaxy_piggyback_20261014-0856", version=6, fin=None, trigger="hand")
    assert _need(st) is None                                    # integrate --no-finish by hand
    _run(st, "Andromeda_Galaxy_piggyback_20261014-0856", version=6, fin=None)
    assert _need(st).why == "no finished image"                 # the watcher's run, no finish record


def test_mono_run_wants_a_color_image(tmp_path):
    st = tmp_path / "Staging"
    d = _run(st, "Heart_Nebula_rc16_x", rig="rc16", campaign="Heart Nebula", fin=True,
             masters=("L", "L_30s", "L_hdr", "R", "G", "B"))
    _jpg(d / "out" / "final" / "Heart_Nebula_L_final.jpg")
    led = L.load(d / "ledger.json")
    n = fr.need_for(d / "ledger.json", led, roots=[st], now=NOW)
    assert n.kind == "color" and "Heart_Nebula_LRGB" in n.why and n.master is None
    _jpg(d / "out" / "final" / "Heart_Nebula_LRGB.jpg")
    assert fr.need_for(d / "ledger.json", led, roots=[st], now=NOW) is None


def test_core_combine_redo_only_after_a_check_error(tmp_path):
    st = tmp_path / "Staging"
    hdr = {"kind": "osc_core", "status": "skipped",
           "long_check": {"error": "unbound prefix: line 4, column 3"}}
    d = _v6(st, masters=("OSC",), hdr=hdr)
    _touch(d / "out" / "master" / "osc_hdr" / "master_OSC_short.xisf")
    n = _need(st)
    assert n.core and n.master.name == "master_OSC.xisf"
    _touch(d / "out" / "master" / "master_OSC_hdr.xisf")         # made by hand since
    assert not _need(st).core


def test_core_combine_rebuilds_the_stack_from_the_run(tmp_path, monkeypatch):
    st = tmp_path / "Staging"
    d = _v6(st, masters=("OSC",))
    stack = {"name": "OSC", "filter": "OSC", "groups": [{"exp": 120.0}, {"exp": 300.0}],
             "short_subset": {"name": "OSC_short", "exps": [120.0], "max_s": 180.0,
                              "output": str(d / "out" / "master" / "osc_hdr" / "master_OSC_short.xisf")}}
    (d / "integrate_run.js").write_text("// x\nvar CONFIG = " + json.dumps({"stacks": [stack]}) + ";\n",
                                        encoding="ascii")
    frames = [{"role": "light", "filter": "OSC", "exp_s": e, "staged": "x"} for e in (120, 120, 300)]
    (d / "manifest.json").write_text(json.dumps({"frames": frames}), encoding="ascii")
    assert fr.integrate_stacks(d)[0]["short_subset"]["name"] == "OSC_short"
    assert fr.stack_counts(d, stack) == {120.0: 2, 300.0: 1}
    seen = {}

    def fake_run(run_dir, st_, counts, **k):
        seen.update(stack=st_["name"], counts=counts, r=k["r_core_px"])
        return {"status": "ok", "made": ["OSC_hdr"]}
    from photonscript.integration import osc_hdr
    monkeypatch.setattr(osc_hdr, "run", fake_run)
    n = _need(st)
    r = fr.core_combine(n, exe="pi.exe")
    assert r["made"] == ["OSC_hdr"] and seen["stack"] == "OSC" and seen["r"] > 0


def _fake_finish(st, ok=True):
    calls = []

    def run_finish(master, n):
        calls.append((master.name, n.solve))
        d = st / "Finish" / f"retry_{len(calls)}"
        if ok:
            _osc_final(d / "out" / "final")
            _jpg(d / "out" / "final" / "Andromeda_Galaxy_OSC_vivid.jpg")
        led = L.Ledger(campaign=n.target, rig=n.rig, kind="finish", run=d.name,
                       machine={"input": {"run": str(n.run_dir), "run_version": n.version},
                                "finish": {"ok": ok}})
        L.save(d / "ledger.json", led)
        return {"run_dir": str(d), "finish": {"ok": ok, "minutes": 12.0}}
    return calls, run_finish


def test_retry_run_records_and_stamps_the_finals(tmp_path):
    st = tmp_path / "Staging"
    d = _v6(st)
    calls, run_finish = _fake_finish(st)
    res = fr.run(_need(st), run_finish=run_finish, run_color=None, roots=[st], echo=lambda s: None)
    assert calls == [("master_OSC_hdr.xisf", True)] and res["entry"]["ok"]
    led = L.load(d / "ledger.json")
    blk = led.machine["finish_retry"]
    assert blk["ok"] and blk["max"] == 2 and len(blk["attempts"]) == 1
    assert led.machine["final"].endswith("Andromeda_Galaxy_OSC_final.jpg")
    assert [Path(p).name for p in led.machine["finals"]] == ["Andromeda_Galaxy_OSC_final.jpg",
                                                             "Andromeda_Galaxy_OSC_vivid.jpg"]
    assert not led.reported                                    # posted again with the JPG list
    assert _need(st) is None                                   # finished now


def test_retry_failure_is_recorded_and_bounded(tmp_path):
    st = tmp_path / "Staging"
    d = _v6(st)
    calls, run_finish = _fake_finish(st, ok=False)
    for _ in range(2):
        fr.run(_need(st), run_finish=run_finish, run_color=None, roots=[st], echo=lambda s: None)
    assert [c[1] for c in calls] == [True, False]              # 2nd without the plate solve
    att = L.load(d / "ledger.json").machine["finish_retry"]["attempts"]
    assert [a["ok"] for a in att] == [False, False] and L.load(d / "ledger.json").reported
    assert _need(st).exhausted


def test_newest_runs_one_per_goal_and_rig(tmp_path):
    st = tmp_path / "Staging"
    _run(st, "M31_piggyback_no0921", version=2, campaign="M31", created=_ago(90))
    _v6(st)
    _run(st, "M31_rc16_x", rig="rc16", version=1, campaign="M31", masters=("L",))
    names = sorted(f.parent.name for f, _ in fr.newest_runs(st))
    assert names == ["Andromeda_Galaxy_piggyback_20261014-0856", "M31_rc16_x"]


# ----------------------------------------------------------------- 3 + 4. blend discovery + staleness

def _desktop(tmp_path):
    """The 2026-10-14 layout: D: staging with v6 (finish failed, re-finished
    twice) and the RC16 v1 + a manifest-only v2 stub; the legacy C: root
    with the real RC16 v2 (L HDR, finish linears); the hand blend of 17:14
    (ledger only) and the auto blend of 10-13 (v2 + v1)."""
    st, legacy, sc = tmp_path / "D" / "Staging", tmp_path / "C" / "Staging", tmp_path / "C" / "Scratch"
    sc.mkdir(parents=True)
    old = _run(st, "M31_piggyback_no0921", version=2, campaign="M31", created=_ago(90))
    _osc_final(old / "out" / "final", "M31_OSC")
    v6 = _v6(st)
    _osc_final(v6 / "out" / "final_v6" / "out" / "final")
    fin7 = _finish_dir(st, "Andromeda_Galaxy_OSC_vivid_v7", v6, 6,
                       files=("Andromeda_Galaxy_OSC_final.jpg", "Andromeda_Galaxy_OSC_linear.xisf",
                              "Andromeda_Galaxy_OSC_vivid.jpg"))
    os.utime(fin7 / "out" / "final" / "Andromeda_Galaxy_OSC_linear.xisf", (2e9, 2e9))
    v1 = _run(st, "M31_rc16_core_v1", rig="rc16", version=1, campaign="M31", created=_ago(50),
              masters=("L", "B"))
    stub = st / "M31_rc16_core_v2"
    _touch(stub / "BIAS" / "b.fits")
    (stub / "manifest.json").write_text(json.dumps({"rig": "rc16", "target": "M31", "built": "x"}),
                                        encoding="ascii")
    v2 = _run(legacy, "M31_rc16_core_v2", rig="rc16", version=2, campaign="M31", created=_ago(30),
              masters=("L", "L_30s", "L_hdr", "R", "G", "B"))
    for f in "LRGB":
        _touch(v2 / "out" / "final" / f"M31_{f}_linear.xisf")
    # the hand blend of 2026-10-14 17:14 (no manifest, ledger only)
    hb = st / "Blend" / "M31_blend_20261014-1714"
    L.save(hb / "ledger.json", L.Ledger(
        campaign="M31", rig="piggyback", kind="blend", run=hb.name, created_at=_ago(8),
        machine={"mode": "l", "blend": {"ok": True}, "inputs": [
            {"role": "osc", "path": str(v6 / "out" / "final_v6" / "out" / "final" / "Andromeda_Galaxy_OSC_linear.xisf"),
             "mtime": "2026-10-14 15:57:37", "run_version": None},
            {"role": "rc16", "path": str(v2 / "out" / "master" / "master_L_hdr.xisf"),
             "mtime": "2026-10-13 00:30:39", "run_version": None}]}))
    os.utime(hb, (1.9e9, 1.9e9))
    # the autointegrate blend of 2026-10-13 (v2 OSC + RC16 v1)
    ab = st / "Blend" / "Andromeda_Galaxy_blend_20261013-0054"
    _touch(ab / "x")
    (ab / "manifest.json").write_text(json.dumps({
        "target": M31, "mode": "l", "pixinsight": {"ok": True}, "built": "2026-10-13 00:54",
        "inputs": [{"role": "osc", "path": str(old / "out" / "final" / "M31_OSC_linear.xisf"),
                    "mtime": "2026-10-11 03:43:19", "run_version": 2},
                   {"role": "rc16", "path": str(v1 / "out" / "master" / "master_L.xisf"),
                    "mtime": "2026-10-12 01:25:12", "run_version": 1}]}), encoding="ascii")
    os.utime(ab, (1.8e9, 1.8e9))
    return SimpleNamespace(st=st, legacy=legacy, sc=sc, v6=v6, v1=v1, v2=v2, stub=stub, fin7=fin7,
                           hb=hb, ab=ab, old=old)


def _bopts(x, extra=True):
    return bl.Options(target=M31, staging_root=x.st, scratch_root=x.sc,
                      extra_roots=[x.legacy] if extra else [], dry_run=True)


def test_discovery_reads_every_root_and_the_stub_never_hides_v2(tmp_path):
    x = _desktop(tmp_path)
    inp = bl.discover(_bopts(x, extra=False))
    assert inp.rc16_run.run_dir == x.v1                        # 10-14: v1 only without C:
    inp = bl.discover(_bopts(x))
    assert inp.rc16_run.run_dir == x.v2 and inp.rc16_run.version == 2
    # 2026-10-16: M31 is a galaxy, so the raw HDR integration master goes in
    assert inp.rc16["L"] == x.v2 / "out" / "master" / "master_L_hdr.xisf"
    assert any("L from the HDR master" in n for n in inp.notes)
    fin = bl.discover(bl.Options(**{**_bopts(x).__dict__, "rc16_input": "finish"}))
    assert fin.rc16["L"] == x.v2 / "out" / "final" / "M31_L_linear.xisf"
    # the newest OSC finish linear, here the Finish/<name>_vivid_v7 re-finish
    assert inp.osc_run.run_dir == x.v6
    assert inp.osc == x.fin7 / "out" / "final" / "Andromeda_Galaxy_OSC_linear.xisf"
    assert any("(re-finish)" in n for n in inp.notes)


def test_discovery_takes_the_nested_refinish(tmp_path):
    x = _desktop(tmp_path)
    shutil.rmtree(x.fin7)
    inp = bl.discover(_bopts(x))
    assert inp.osc == x.v6 / "out" / "final_v6" / "out" / "final" / "Andromeda_Galaxy_OSC_linear.xisf"


def test_find_runs_prefers_hdr_l_on_a_tie(tmp_path):
    st = tmp_path / "S"
    _run(st, "a_rc16", rig="rc16", version=3, campaign="M31", created=_ago(1), masters=("L",))
    _run(st, "b_rc16", rig="rc16", version=3, campaign="M31", created=_ago(1), masters=("L", "L_hdr"))
    assert [r.run_dir.name for r in bl.find_runs(st, "M31", "rc16")] == ["b_rc16", "a_rc16"]


def _aopts(x):
    wo = watch.WatchOptions(base_url=URL, staging_root=x.st, scratch_root=x.sc)
    return ai.AutoOptions(watch=wo, library=x.st, extra_roots=[x.legacy])


def test_the_hand_blend_covers_a_refinish_of_the_same_masters(tmp_path):
    x = _desktop(tmp_path)
    inp = bl.discover(_bopts(x))
    done, why = ai.blend_status(_aopts(x), M31, inp)
    assert done == "M31_blend_20261014-1714" and "OSC v6" in why and "RC16 v2" in why


def test_a_newer_piggy_master_reblends(tmp_path):
    """10-14 01:00: the auto blend of 10-13 (OSC v2 + RC16 v1) must not
    cover the v6 finish + RC16 v2."""
    x = _desktop(tmp_path)
    shutil.rmtree(x.hb)
    inp = bl.discover(_bopts(x))
    done, why = ai.blend_status(_aopts(x), M31, inp)
    assert done is None and "OSC v6 > v2" in why and "RC16 v2 > v1" in why
    inp2, why2 = ai.blend_candidate(_aopts(x), M31, lambda t: inp)
    assert inp2 is inp and why2.startswith("both rigs have masters, newer input")


def test_exact_inputs_and_unknown_versions(tmp_path):
    st = tmp_path / "S"
    o = ai.AutoOptions(watch=watch.WatchOptions(base_url=URL, staging_root=st), library=st)
    osc, rc = _touch(tmp_path / "in" / "osc.xisf"), _touch(tmp_path / "in" / "L.xisf")
    inp = SimpleNamespace(osc=osc, rc16={"L": rc}, mode="l")
    assert ai.blend_status(o, "M31", inp) == (None, "no earlier blend")
    d = st / "Blend" / "M31_blend_1"
    man = {"target": "M31", "mode": "l", "pixinsight": {"ok": True},
           "inputs": [{"role": "osc", "path": str(osc), "mtime": "2026-10-01 00:00:00"},
                      {"role": "rc16", "path": str(rc), "mtime": "2026-10-01 00:00:00"}]}
    _touch(d / "manifest.json").write_text(json.dumps(man), encoding="ascii")
    assert ai.blend_status(o, "M31", inp)[0] == "M31_blend_1"           # exactly these files
    osc2 = _touch(tmp_path / "in" / "osc2.xisf")                       # no ledger: file times
    assert ai.blend_status(o, "M31", SimpleNamespace(osc=osc2, rc16={"L": rc}, mode="l"))[0] is None
    man["inputs"][0]["mtime"] = man["inputs"][1]["mtime"] = "2099-01-01 00:00:00"
    (d / "manifest.json").write_text(json.dumps(man), encoding="ascii")
    assert ai.blend_status(o, "M31", SimpleNamespace(osc=osc2, rc16={"L": rc}, mode="l"))[0] == "M31_blend_1"
    # an nb blend never covers an l blend
    assert ai.blend_status(o, "M31", SimpleNamespace(osc=osc2, rc16={"L": rc}, mode="nb"))[0] is None


def test_source_version_of_runs_refinishes_and_unknown(tmp_path):
    x = _desktop(tmp_path)
    assert ai.source_version(x.fin7 / "out" / "final" / "Andromeda_Galaxy_OSC_linear.xisf") == 6
    assert ai.source_version(x.v2 / "out" / "master" / "master_L_hdr.xisf") == 2
    assert ai.source_version(tmp_path / "nowhere" / "a.xisf") is None


# ----------------------------------------------------------------- cycles

class _Fk:
    def __init__(self, cands):
        self.cands, self.posts, self.runs = cands, [], []

    def get(self, url):
        return {"candidates": self.cands,
                "thresholds": {"rigs": ["piggyback", "rc16"], "new_data_h": 1.0, "first_h": 0.0,
                               "min_interval_h": 12.0, "require_calibration": False}}

    def post(self, url, body, timeout):
        if url.endswith("/processing"):
            return 200, {"ok": True}
        self.posts.append(body["run"])
        return 200, {"ok": True, "version": body["version"]}

    def run_integrate(self, root):
        def _r(target, rig, trigger):
            self.runs.append((target, rig))
            d = _run(root, f"new_{rig}", rig=rig, version=7, campaign=target, created=_ago(0))
            _jpg(d / "out" / "final" / "Andromeda_Galaxy_OSC_final.jpg")
            return {"run_dir": str(d), "ledger": str(d / "ledger.json"), "integration": {"ok": True},
                    "finish": {"ok": True}, "ledger_version": 7, "hours_integrated": 1.0}
        return _r


def _cyc(o, fk, *, retry=None, blends=None, discover=None, guard=None, sends=None, local_now=None,
         pi=()):
    blends = [] if blends is None else blends
    sends = [] if sends is None else sends

    def run_blend(t):
        blends.append(t)
        return {"run_dir": str(o.watch.staging_root / "Blend" / "b"), "blend": {"ok": True}}
    return ai.cycle(o, run_integrate=fk.run_integrate(o.watch.staging_root), run_blend=run_blend,
                    discover_blend=discover or (lambda t: (_ for _ in ()).throw(RuntimeError("no rc16"))),
                    send=lambda m, i: sends.append(m) or True, get=fk.get, post=fk.post,
                    pi_running=lambda: list(pi), echo=lambda s: None, now=NOW, run_retry=retry,
                    load_guard=guard, local_now=local_now)


def _wait_cands():
    return [_cand(last={"version": 6}, new_h=0.2), _cand(rig="rc16", last={"version": 2}, new_h=0.65)]


def test_cycle_retries_the_finish_once_then_blends(tmp_path):
    st = tmp_path / "Staging"
    _v6(st)
    o = ai.AutoOptions(watch=watch.WatchOptions(base_url=URL, staging_root=st), library=st, settle_min=0)
    calls, run_finish = _fake_finish(st)
    retry = lambda n: fr.run(n, run_finish=run_finish, run_color=None, roots=[st],  # noqa: E731
                             echo=lambda s: None)
    fk, blends, sends = _Fk(_wait_cands()), [], []
    inp = SimpleNamespace(osc=st / "x.xisf", rc16={"L": st / "L.xisf"}, mode="l")
    out = _cyc(o, fk, retry=retry, blends=blends, sends=sends, discover=lambda t: inp)
    assert fk.runs == [] and len(calls) == 1 and out["retry"]["ok"] and out["retry"]["posted"]
    assert fk.posts == ["Andromeda_Galaxy_piggyback_20261014-0856"]   # re-posted with its finals
    assert blends == [M31]                                             # blended right after
    assert any(m.startswith("Finish retry 1 of Andromeda Galaxy [piggyback]: done") for m in sends)
    out = _cyc(o, fk, retry=retry, blends=blends)
    assert len(calls) == 1 and out["retry"] is None


def test_cycle_gives_up_after_the_retries_and_holds_the_blend_meanwhile(tmp_path):
    st = tmp_path / "Staging"
    _v6(st)
    o = ai.AutoOptions(watch=watch.WatchOptions(base_url=URL, staging_root=st), library=st, settle_min=0)
    calls, run_finish = _fake_finish(st, ok=False)
    retry = lambda n: fr.run(n, run_finish=run_finish, run_color=None, roots=[st],  # noqa: E731
                             echo=lambda s: None)
    inp = SimpleNamespace(osc=st / "x.xisf", rc16={"L": st / "L.xisf"}, mode="l")
    blends, counts = [], []
    for _ in range(4):
        _cyc(o, _Fk(_wait_cands()), retry=retry, blends=blends, discover=lambda t: inp)
        counts.append((len(calls), len(blends)))
    # retry 1 fails: the blend waits; retry 2 fails: given up, the blend goes
    # ahead with what exists; no 3rd retry
    assert counts[:2] == [(1, 0), (2, 1)] and counts[3][0] == 2


def test_cycle_integrate_first_and_stamps_finals(tmp_path):
    st = tmp_path / "Staging"
    _v6(st, created=_ago(13))
    o = ai.AutoOptions(watch=watch.WatchOptions(base_url=URL, staging_root=st), library=tmp_path / "Lib",
                       settle_min=0)
    d = tmp_path / "Lib" / "Andromeda Galaxy" / "OSC"
    _touch(d / "a.fits")
    calls, run_finish = _fake_finish(st)
    retry = lambda n: fr.run(n, run_finish=run_finish, run_color=None, roots=[st],  # noqa: E731
                             echo=lambda s: None)
    fk = _Fk([_cand(last={"version": 6}, new_h=2.0)])
    out = _cyc(o, fk, retry=retry)
    assert fk.runs == [(M31, "piggyback")] and calls == []           # one PixInsight job per cycle
    led = L.load(st / "new_piggyback" / "ledger.json")
    assert [Path(p).name for p in led.machine["finals"]] == ["Andromeda_Galaxy_OSC_final.jpg"]
    assert out["ran"]["finals"]


def test_desktop_guard(tmp_path):
    st = tmp_path / "Staging"
    o = ai.AutoOptions(watch=watch.WatchOptions(base_url=URL, staging_root=st), library=st,
                       idle_min=10, max_cpu_pct=80)
    assert "desktop in use" in ai.desktop_guard(o, idle=lambda: 2.0, cpu=lambda: 5.0)
    assert "CPU 95%" in ai.desktop_guard(o, idle=lambda: 30.0, cpu=lambda: 95.0)
    assert ai.desktop_guard(o, idle=lambda: 30.0, cpu=lambda: 5.0) is None
    assert ai.desktop_guard(o, idle=lambda: None, cpu=lambda: None) is None   # unreadable: no block
    off = ai.AutoOptions(watch=o.watch, library=st)
    assert ai.desktop_guard(off, idle=lambda: 0.0, cpu=lambda: 100.0) is None
    fk = _Fk([_cand()])
    out = _cyc(o, fk, guard=lambda: "desktop in use (test)")
    assert out["skipped"] == "desktop in use" and fk.runs == []
    o.watch.dry_run = True
    out = _cyc(o, fk, guard=lambda: "desktop in use (test)")
    assert out.get("skipped") is None and out["decisions"]


def test_idle_minutes_reads_or_returns_none():
    v = ai.idle_minutes()
    assert v is None or v >= 0


# ----------------------------------------------------------------- 5. blend vivid

def test_vivid_wanted_for_galaxy_l_blends_only():
    inp_l = bl.Inputs("linear", Path("o"), {}, mode="l")
    assert bl.vivid_wanted(bl.Options(target="M31"), inp_l) == (True, "")
    assert not bl.vivid_wanted(bl.Options(target="Heart Nebula"), inp_l)[0]
    assert bl.vivid_wanted(bl.Options(target="Heart Nebula", vivid_kinds=""), inp_l)[0]
    assert not bl.vivid_wanted(bl.Options(target="M31"), bl.Inputs("linear", Path("o"), {}, mode="nb"))[0]
    assert not bl.vivid_wanted(bl.Options(target="M31", vivid=False), inp_l)[0]


def test_render_vivid_is_clean_pjsr(tmp_path):
    cfg = bl.vivid_config(bl.Options(target="M31"), tmp_path / "Blend" / "M31_blend_1")
    assert cfg["name"] == "M31_blend" and cfg["input"].endswith("out/final/M31_blend.xisf")
    assert cfg["sat_mid"] == 0.5 and cfg["vivid_sat_mid"] == 0.74
    t = bl.render_vivid(cfg)
    assert pjsr.check(t) == []
    import re
    assert not re.findall(r"__[A-Z_]+__", t)
    assert t.count("function vividFinish(") == 1 and t.count("function dupWindow(") == 1
    assert "//__FINISH_LIB__" not in t and "//__VIVID_LIB__" not in t
    for n, ln in enumerate(t.splitlines(), 1):
        if "//" in ln:
            assert "/*" not in ln.split("//", 1)[1], n
    node = shutil.which("node")
    if node:
        js = tmp_path / "v.js"
        js.write_text("\n".join(ln for ln in t.splitlines() if not ln.startswith("#include")),
                      encoding="ascii")
        r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_vivid_lib_markers_wrap_the_vivid_section():
    s = (Path(bl.__file__).resolve().parents[2] / "deploy" / "finish_osc.js").read_bytes().decode("ascii")
    a, b = s.index(bl.VIVID_LIB_BEGIN), s.index(bl.VIVID_LIB_END)
    sec = s[a:b]
    assert "function vividFinish(" in sec and "var VIVID_SRC" in sec and "PS_VIVID_MATH_BEGIN" in sec
    assert "function main()" not in sec and "__" not in sec.replace("PS_VIVID", "")


def test_blend_run_makes_the_vivid_after_an_ok_blend(tmp_path, monkeypatch):
    st = tmp_path / "Staging"
    _run(st, "M31_piggyback_1", version=1, campaign="M31")
    _osc_final(st / "M31_piggyback_1" / "out" / "final", "M31_OSC")
    _run(st, "M31_rc16_1", rig="rc16", version=1, campaign="M31", masters=("L",))
    seen = []

    def fake_run(script, log_path, **k):
        seen.append(Path(script).name)
        fin = Path(log_path).parent / "final"
        if Path(script).name == "blend_run.js":
            _touch(fin / "M31_blend.xisf")
            _jpg(fin / "M31_blend.jpg")
        else:
            _jpg(fin / "M31_blend_vivid.jpg")
        Path(log_path).write_text("EXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 1.0, "last_line": "EXIT OK", "timed_out": False}
    monkeypatch.setattr(bl.runner, "run_script", fake_run)
    res = bl.run(bl.Options(target="M31", staging_root=st, lock_wait_min=0), echo=lambda s: None)
    assert seen == ["blend_run.js", "blend_vivid_run.js"]
    assert res["vivid"]["ok"]
    rd = Path(res["run_dir"])
    assert json.loads((rd / "manifest.json").read_text())["vivid"]["ok"]
    assert L.load(rd / "ledger.json").machine["vivid"]["ok"]
    assert [p.name for p in ai.finals(rd, blend=True)][:2] == ["M31_blend.jpg", "M31_blend_vivid.jpg"]
    assert ai.notice_text("blend", "M31", "x", res, "why").startswith("Blend M31: done, vivid")


def test_a_failed_vivid_never_fails_the_blend(tmp_path, monkeypatch):
    st = tmp_path / "Staging"
    rd = st / "Blend" / "b"
    _touch(rd / "out" / "final" / "M31_blend.xisf")

    def boom(*a, **k):
        raise RuntimeError("PixInsight busy")
    monkeypatch.setattr(bl.runner, "run_script", boom)
    r = bl.run_vivid(bl.Options(target="M31", staging_root=st), rd, echo=lambda s: None)
    assert r["ok"] is False and "busy" in r["why"]
    r = bl.run_vivid(bl.Options(target="M31", staging_root=st), st / "Blend" / "none", echo=lambda s: None)
    assert r["ok"] is False and "no stretched blend" in r["why"]


# ----------------------------------------------------------------- finish --no-solve

def test_finish_without_solve_passes_no_coordinates(tmp_path):
    m = _touch(tmp_path / "run" / "out" / "master" / "master_OSC.xisf")
    res = fn.resolve(fn.Options(master=m, name="M31_OSC", target="M31", solve=False))
    assert res["ra_deg"] is None and fn.NO_SOLVE_NOTE in res["notes"]
    cfg = fn.finish_config(fn.Options(master=m, name="M31_OSC"), res, tmp_path / "f")
    assert "var RA_DEG    = NaN;" in pjsr.render_finish(cfg)
    assert fn.resolve(fn.Options(master=m, name="M31_OSC", target="M31"))["ra_deg"] is not None


# ----------------------------------------------------------------- 6. morning summary

def test_summary_lines_per_goal_with_links(tmp_path):
    x = _desktop(tmp_path)
    goals = ns.collect([x.st, x.legacy], NOW - timedelta(hours=24))
    by = {g["goal"]: g for g in goals}
    assert set(by) == {M31}                                # the integrate ledger's goal name
    g = by[M31]
    text = " ".join(g["events"])
    assert "stacked piggyback v6" in text and "finish FAILED, re-finished" in text
    assert "finished piggyback v6 from Andromeda_Galaxy_piggyback_20261014-0856" in text
    assert "blended (M31_blend_20261014-1714)" in text
    out = ns.render(goals, "2026-10-15")
    assert out.count("\n") == 1 and "file:///" in out
    sh = ns.short(goals, "2026-10-15", Path("D:/S/Summaries/2026-10-15.txt"))
    assert "file:///" not in sh and sh.endswith("2026-10-15.txt") and len(sh) <= ns.PUSH_MAX
    assert ns.render([], "d").endswith("nothing stacked, finished or blended.")


def test_summary_once_a_day_after_the_hour(tmp_path):
    x = _desktop(tmp_path)
    o = _aopts(x)
    o.summary_hour = 8.0
    sends = []
    early = datetime(2026, 10, 15, 7, 30)
    out = _cyc(o, _Fk([]), sends=sends, local_now=early)
    assert out["summary"] is None and sends == []
    late = datetime(2026, 10, 15, 8, 5)
    out = _cyc(o, _Fk([]), sends=sends, local_now=late)
    assert out["summary"]["day"] == "2026-10-15" and Path(out["summary"]["file"]).is_file()
    assert len(sends) == 1 and sends[0].startswith("Night 2026-10-15:")
    out = _cyc(o, _Fk([]), sends=sends, local_now=late + timedelta(hours=2))
    assert out["summary"] is None and len(sends) == 1
    out = _cyc(o, _Fk([]), sends=sends, local_now=late + timedelta(days=1), pi=[42])
    assert out["summary"]["day"] == "2026-10-16"          # sent even while PixInsight is open


def test_ledger_brief_lists_the_finals():
    from photonscript.scheduler.integrations import ledger_brief
    led = L.Ledger(campaign="M31", rig="piggyback", run="r", version=6,
                   machine={"final": "a.jpg", "finals": ["a.jpg", "b.jpg"]})
    assert ledger_brief(led)["finals"] == ["a.jpg", "b.jpg"]


# ----------------------------------------------------------------- CLI

@pytest.mark.parametrize("cmd,flag", [("autointegrate", "--no-guard"), ("blend", "--no-vivid"),
                                      ("finish", "--no-solve"), ("autointegrate-summary", "--hours")])
def test_cli_flags(cmd, flag):
    from typer.testing import CliRunner
    from photonscript.cli import app
    r = CliRunner().invoke(app, [cmd, "--help"], env={"COLUMNS": "200"})
    assert r.exit_code == 0 and flag in r.output
