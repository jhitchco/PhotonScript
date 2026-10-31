"""TPoint refit (docs/TPOINT_REFIT.md): the action-name fence, the read-only
discover script, the judge, the restore plan, the supervised run with fakes
(console and action forms) and the dry-run CLI. Nothing here talks to
TheSky: the fakes record every script."""
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import thesky_client as tc
from photonscript.telescope_agent import tpoint_refit as tr
from tests.test_scheduler.test_ps104_thesky_audit import _violations

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 18, 4, 0)
TH = {"min_samples": 50, "max_rms_arcsec": 30.0, "max_worse_pct": 20.0,
      "min_per_term": 3.0}


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("thesky_user_dir", str(tmp_path / "nosky"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


def _st(points=233, rms=15.8, terms=22, file="TPoint\\Super Model Outmod.dat",
        mtime="2026-10-17T03:00:00Z", **kw):
    return {"ok": True, "model_points": points, "sky_rms_arcsec": rms,
            "fitted_terms": terms, "active_model_file": file,
            "active_model_mtime": mtime, **kw}


# ------------------------------------------------------------- fences

@pytest.mark.parametrize("name", ["AddPointingSample", "AutoPointingCalibration",
                                  "ClearPointingData", "SlewToHome", "Park",
                                  "Sync", "TPoint;x", "a b", "", "1abc",
                                  "TPointModule)"])
def test_action_names_that_are_never_used(name):
    assert tr.action_ok(name)
    with pytest.raises(ValueError):
        tr.fit_action_script(name)


def test_fit_action_script_checks_the_member_before_calling():
    js = tr.fit_action_script("TPointSuperModel")
    assert "typeof TheSkyXAction.TPointSuperModel == 'number'" in js
    assert "TheSkyXAction.execute(TheSkyXAction.TPointSuperModel)" in js
    assert js.index("typeof") < js.index("execute(")
    assert js.isascii()


def test_discover_script_is_read_only():
    js = tr.discover_script()
    assert "execute(" not in js
    assert not _violations(js), _violations(js)
    assert "for (var k in TheSkyXAction)" in js


def test_write_script_never_joins_the_read_only_set():
    ro = "\n".join(tc.READ_ONLY_JS.values())
    assert "TheSkyXAction.execute(" not in ro
    assert tr.discover_script() not in ro


def test_module_text_is_ascii_without_em_dashes():
    for p in (ROOT / "photonscript" / "telescope_agent" / "tpoint_refit.py",
              ROOT / "docs" / "TPOINT_REFIT.md"):
        txt = p.read_text(encoding="utf-8")
        assert txt.isascii(), p
        assert chr(0x2014) not in txt and chr(0x2013) not in txt


def test_never_turns_on_corrections_or_protrack():
    src = (ROOT / "photonscript" / "telescope_agent" / "tpoint_refit.py").read_text(
        encoding="utf-8")
    for bad in ("DoCommandStr", "ApplyPointingCorrections =", "ProTrack =",
                "DoCommand(", "SetTracking", "Sync(", "SlewTo"):
        assert bad not in src, bad


def test_parse_discover():
    d = tr.parse_discover({"execute": "function", "members_seen": "210",
                           "actions": "TPointModule:146~TPointAddOn2:147~"
                                      "AddPointingSample:197"})
    names = {a["name"]: a for a in d["actions"]}
    assert names["TPointModule"]["value"] == "146"
    assert names["TPointModule"]["refusal"] is None
    assert names["AddPointingSample"]["refusal"]
    assert "never used" in tr.format_discover(d)
    assert tr.parse_discover({"actions": "?ERR:boom"})["error"] == "?ERR:boom"


# ------------------------------------------------------------- judge

def test_judge_keeps_a_good_fit():
    v = tr.judge(_st(rms=15.8), _st(points=260, rms=14.9, mtime="2026-10-18T04:00:00Z"), TH)
    assert v["verdict"] == "keep", v["reasons"]


def test_judge_no_change_when_the_model_did_not_move():
    v = tr.judge(_st(), _st(), TH)
    assert v["verdict"] == "no_change"


@pytest.mark.parametrize("after,why", [
    (_st(points=40, terms=10, mtime="n"), "samples"),
    (_st(points=60, terms=22, rms=10.0, mtime="n"), "samples per term"),
    (_st(rms=31.0, mtime="n"), "sky RMS"),
    (_st(rms=19.5, mtime="n"), "not worse than before"),
    (_st(rms=None, mtime="n"), "sky RMS"),
    (_st(terms=None, mtime="n"), "samples per term"),
])
def test_judge_rejects(after, why):
    v = tr.judge(_st(rms=15.8), after, TH)
    assert v["verdict"] == "reject"
    assert any(r.startswith(why) for r in v["reasons"]), v["reasons"]


def test_judge_rejects_unreadable_files():
    v = tr.judge(_st(), {"ok": False, "note": "no files"}, TH)
    assert v["verdict"] == "reject" and "no files" in v["reasons"][0]


def test_thresholds_from_config(tmp_path):
    cfg = _cfg(tmp_path, tpoint_min_points=80, tpoint_rms_max_arcsec=25.0)
    th = tr.thresholds(cfg)
    assert th["min_samples"] == 80 and th["max_rms_arcsec"] == 25.0
    assert th["max_worse_pct"] == 20.0 and th["min_per_term"] == 3.0
    assert tr.thresholds(cfg, min_samples=10)["min_samples"] == 10


# ------------------------------------------------------------- live run (fakes)

def _tree(tmp_path):
    root = tmp_path / "sky" / "TPoint"
    root.mkdir(parents=True)
    (root / "Super Model Outmod.dat").write_text("OLD MODEL\n", encoding="ascii")
    (root / "Super Model Indat.dat").write_text("OLD DATA\n", encoding="ascii")
    return root


def _listing(root):
    files = []
    for p in sorted(root.iterdir()):
        files.append({"rel": "TPoint\\" + p.name, "path": str(p),
                      "size": p.stat().st_size, "mtime": "x"})
    return {"roots": [], "files": files}


class FakeSky:
    def __init__(self, reply=None):
        self.sent = []
        self.reply = reply or {"found": "1", "called": "1", "id": "999", "err": ""}

    def run(self, js):
        self.sent.append(js)
        return dict(self.reply)


class FakeSched:
    def __init__(self, state="DISARMED"):
        self.state = state

    def arm(self):
        return {"state": self.state} if self.state else None


def _deps(tmp_path, root, stats_seq, sky=None, state="DISARMED", answers=None,
          on_fit=None):
    said, asked, records = [], [], []
    seq = list(stats_seq)

    def stats(_lst):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def ask(prompt):
        asked.append(prompt)
        if on_fit and "Super Model" in prompt:
            on_fit()
        return (answers or {}).get(len(asked), "yes")
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
    d = tr.Deps(sky=sky, sched=FakeSched(state), list_files=lambda: _listing(root),
                stats=stats, ask=ask, say=said.append, sleep=sleep,
                mono=lambda: clock["t"], now=lambda: NOW,
                save_record=records.append)
    return d, said, asked, records


def _opts(tmp_path, **kw):
    kw.setdefault("backup_root", str(tmp_path / "backup"))
    kw.setdefault("th", dict(TH))
    kw.setdefault("wait_s", 30.0)
    return tr.Opts(**kw)


def test_console_fit_kept(tmp_path):
    root = _tree(tmp_path)
    d, said, asked, rec = _deps(tmp_path, root, [_st(rms=15.8), _st(rms=15.8),
                                                 _st(points=260, rms=14.0, mtime="new")])
    res = tr.run_refit(d, _opts(tmp_path))
    assert res["status"] == "kept", res["reason"]
    bak = Path(res["backup"])
    assert (bak / "TPoint" / "Super Model Outmod.dat").read_text() == "OLD MODEL\n"
    assert any("Apply pointing corrections" in s for s in said)
    assert rec and rec[0]["status"] == "kept"


def test_console_fit_rejected_restores_and_keeps_the_rejected_files(tmp_path):
    root = _tree(tmp_path)

    def fit():   # what TPoint would write on a bad Super Model save
        (root / "Super Model Outmod.dat").write_text("BAD MODEL\n", encoding="ascii")
        (root / "TPoint new run outmod.dat").write_text("NEW FILE\n", encoding="ascii")
    d, said, _a, _r = _deps(tmp_path, root,
                            [_st(rms=15.8), _st(rms=40.0, mtime="new")], on_fit=fit)
    res = tr.run_refit(d, _opts(tmp_path))
    assert res["status"] == "rejected"
    assert (root / "Super Model Outmod.dat").read_text() == "OLD MODEL\n"
    assert (root / "Super Model Indat.dat").read_text() == "OLD DATA\n"
    assert not (root / "TPoint new run outmod.dat").exists()
    rej = Path(res["backup"]) / "rejected" / "TPoint"
    assert (rej / "Super Model Outmod.dat").read_text() == "BAD MODEL\n"
    assert (rej / "TPoint new run outmod.dat").read_text() == "NEW FILE\n"
    assert not (rej / "Super Model Indat.dat").exists()     # unchanged: not touched
    assert res["restored"]["errors"] == []
    assert any("STILL HOLDS THE REJECTED MODEL" in s for s in said)


def test_no_change_restores_nothing(tmp_path):
    root = _tree(tmp_path)
    d, _s, _a, _r = _deps(tmp_path, root, [_st()])
    res = tr.run_refit(d, _opts(tmp_path, wait_s=10.0))
    assert res["status"] == "no_change" and res["restored"] is None


def test_operator_says_no_stops_after_the_backup(tmp_path):
    root = _tree(tmp_path)
    d, _s, _a, _r = _deps(tmp_path, root, [_st()], answers={1: "no"})
    res = tr.run_refit(d, _opts(tmp_path))
    assert res["status"] == "stopped"
    assert Path(res["backup"]).is_dir()


@pytest.mark.parametrize("state", ["ARMED", "RUNNING", "WATCHING", None])
def test_refuses_unless_disarmed_and_copies_nothing(tmp_path, state):
    root = _tree(tmp_path)
    copies = []
    d, _s, _a, _r = _deps(tmp_path, root, [_st()], state=state)
    d.copy = lambda a, b: copies.append((a, b))
    res = tr.run_refit(d, _opts(tmp_path))
    assert res["status"] == "refused" and "armer idle" in res["reason"]
    assert not copies


def test_refuses_without_tpoint_files(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    d, _s, _a, _r = _deps(tmp_path, root, [{"ok": False, "note": "none"}])
    res = tr.run_refit(d, _opts(tmp_path))
    assert res["status"] == "refused" and "TPoint backup" in res["reason"]


def test_action_fit_refused_without_test_or_pin(tmp_path):
    root = _tree(tmp_path)
    sky = FakeSky()
    d, _s, _a, _r = _deps(tmp_path, root, [_st()], sky=sky)
    res = tr.run_refit(d, _opts(tmp_path, fit="action", action="TPointSuperModel"))
    assert res["status"] == "refused" and "not confirmed" in res["reason"]
    assert sky.sent == []


def test_action_fit_refuses_a_denied_name_even_in_test(tmp_path):
    root = _tree(tmp_path)
    sky = FakeSky()
    d, _s, _a, _r = _deps(tmp_path, root, [_st()], sky=sky)
    res = tr.run_refit(d, _opts(tmp_path, fit="action", action="AddPointingSample",
                                test=True))
    assert res["status"] == "refused" and sky.sent == []


def test_action_fit_test_missing_member_calls_nothing(tmp_path):
    root = _tree(tmp_path)
    sky = FakeSky({"found": "0", "called": "0", "id": "undefined", "err": ""})
    d, _s, asked, _r = _deps(tmp_path, root, [_st()], sky=sky)
    res = tr.run_refit(d, _opts(tmp_path, fit="action", action="TPointSuperModel",
                                test=True))
    assert res["status"] == "stopped" and "nothing was called" in res["reason"]
    assert len(sky.sent) == 1 and any("Send it now" in a for a in asked)


def test_action_fit_test_asks_before_sending(tmp_path):
    root = _tree(tmp_path)
    sky = FakeSky()
    d, _s, _a, _r = _deps(tmp_path, root, [_st()], sky=sky, answers={1: "no"})
    res = tr.run_refit(d, _opts(tmp_path, fit="action", action="TPointSuperModel",
                                test=True))
    assert res["status"] == "stopped" and sky.sent == []


def test_action_fit_pinned_runs_and_is_judged(tmp_path):
    root = _tree(tmp_path)
    sky = FakeSky()
    d, _s, asked, _r = _deps(tmp_path, root,
                             [_st(), _st(), _st(points=300, rms=12.0, mtime="new")],
                             sky=sky)
    res = tr.run_refit(d, _opts(tmp_path, fit="action", action="TPointSuperModel",
                                action_pinned=True))
    assert res["status"] == "kept", res["reason"]
    assert len(sky.sent) == 1 and "execute(TheSkyXAction.TPointSuperModel)" in sky.sent[0]
    assert asked == []          # pinned, not a test: no questions


# ------------------------------------------------------------- dry run / CLI

def test_build_plan_lists_backup_and_thresholds(tmp_path):
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path)
    p = tr.build_plan(cfg, _opts(tmp_path), _listing(root), _st(), NOW)
    txt = tr.format_plan(p)
    assert "DRY RUN" in txt and "2 TPoint file(s)" in txt
    assert ">= 50 samples" in txt and "30\"" in txt
    assert p["fit_script"] is None
    p = tr.build_plan(cfg, _opts(tmp_path, fit="action", action="TPointSuperModel"),
                      _listing(root), _st(), NOW)
    assert "needs --test" in tr.format_plan(p) and p["fit_script"]


def test_cli_dry_run_sends_and_copies_nothing(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    import shutil
    import socket
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)

    def boom(*a, **k):
        raise AssertionError("dry run opened a connection or copied a file")
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(shutil, "copy2", boom)
    r = CliRunner().invoke(cli.app, ["tpoint-refit"])
    assert r.exit_code == 0, r.output
    assert "DRY RUN" in r.output and "Discover script" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-refit", "--fit", "nope"])
    assert r.exit_code == 2
    r = CliRunner().invoke(cli.app, ["tpoint-refit", "--discover", "--go"])
    assert r.exit_code == 2


def test_remote_allowlist_read_modes_and_the_remote_steps():
    """2026-10-18: dry run and --discover. 2026-10-29 (Jeremy approved):
    --backup / --judge / --restore-if-bad. --go, --fit, --action, --test
    (console input) never appear."""
    from photonscript.scheduler import remote_cli as rc
    allow = rc.load_allowlist(ROOT / "config" / "remote_cli.toml")
    r = allow.commands["tpoint-refit"]
    assert r.cls == "read" and not r.night_safe
    assert set(r.flags) == {"--discover", "--backup", "--judge", "--backup-dir",
                            "--restore-if-bad", "--min-samples", "--max-rms",
                            "--max-worse-pct", "--verbose", "--json"}
    assert set(r.write_flags) == {"--backup", "--restore-if-bad"}
    for bad in ("--go", "--fit", "--action", "--test", "--url", "--wait-s"):
        assert bad not in r.flags
    assert not [b for b in rc.check_allowlist(allow) if b.startswith("tpoint-refit")]

    def cls(*a):
        return rc.validate(["tpoint-refit", *a], allow).cls
    assert cls("--discover") == "read" and cls("--json") == "read"
    assert cls("--judge", "--json") == "read"
    assert cls("--judge", "--max-rms", "20", "--min-samples", "40") == "read"
    assert cls("--backup") == "write"
    assert cls("--judge", "--restore-if-bad", "--backup-dir",
               "20261029T040000Z") == "write"
    for argv in (["--go"], ["--go", "--fit", "action"], ["--test"],
                 ["--action", "X"]):
        with pytest.raises(rc.Refused):
            rc.validate(["tpoint-refit", *argv], allow)


# ------------------------------------------------------------- remote steps

def _no_ask(prompt):
    raise AssertionError("asked " + prompt)


def _backup(tmp_path, root, before=None, state="DISARMED"):
    d, said, _a, _r = _deps(tmp_path, root, [before or _st(rms=15.8)], state=state)
    d.ask = _no_ask
    return tr.backup_only(d, _opts(tmp_path)), said


def test_backup_writes_the_folder_and_refit_before(tmp_path):
    import json
    root = _tree(tmp_path)
    res, said = _backup(tmp_path, root)
    assert res["status"] == "backed_up", res["reason"]
    bak = Path(res["backup"])
    assert bak.parent == tmp_path / "backup" and bak.name == "20261018T040000Z"
    assert (bak / "TPoint" / "Super Model Outmod.dat").read_text() == "OLD MODEL\n"
    assert (bak / "TPoint" / "Super Model Indat.dat").read_text() == "OLD DATA\n"
    rec = json.loads((bak / tr.BEFORE_NAME).read_text(encoding="utf-8"))
    assert rec["samples"] == 233 and rec["sky_rms_arcsec"] == 15.8 and rec["terms"] == 22
    assert {f["rel"] for f in rec["files"]} == {"TPoint\\Super Model Outmod.dat",
                                               "TPoint\\Super Model Indat.dat"}
    assert all(f["size"] and f["mtime"] for f in rec["files"])
    assert any(str(bak) in s for s in said)


def test_backup_refuses_unless_disarmed(tmp_path):
    root = _tree(tmp_path)
    res, _s = _backup(tmp_path, root, state="RUNNING")
    assert res["status"] == "refused" and "armer idle" in res["reason"]
    assert not (tmp_path / "backup").exists()


def _judge(tmp_path, root, after, restore=False, state="DISARMED", backup_dir=None):
    d, said, _a, rec = _deps(tmp_path, root, [after], state=state)
    d.ask = _no_ask
    return tr.judge_only(d, _opts(tmp_path), backup_dir, restore), said, rec


def test_judge_keeps_a_good_fit_after_backup(tmp_path):
    root = _tree(tmp_path)
    b, _s = _backup(tmp_path, root)
    (root / "Super Model Outmod.dat").write_text("NEW MODEL\n", encoding="ascii")
    res, said, _r = _judge(tmp_path, root, _st(points=260, rms=14.0, mtime="new"),
                           restore=True)
    assert res["status"] == "kept", res["reason"]
    assert res["backup"] == b["backup"] and res["restored"] is None
    assert (root / "Super Model Outmod.dat").read_text() == "NEW MODEL\n"
    assert any("before:" in s and "15.80" in s for s in said)
    assert any("after:" in s and "14.00" in s for s in said)


def test_judge_restore_if_bad_restores_and_deletes_nothing(tmp_path):
    root = _tree(tmp_path)
    b, _s = _backup(tmp_path, root)
    (root / "Super Model Outmod.dat").write_text("BAD MODEL\n", encoding="ascii")
    (root / "TPoint new run outmod.dat").write_text("NEW FILE\n", encoding="ascii")
    res, _said, _r = _judge(tmp_path, root, _st(rms=40.0, mtime="new"), restore=True)
    assert res["status"] == "rejected"
    assert (root / "Super Model Outmod.dat").read_text() == "OLD MODEL\n"
    assert (root / "Super Model Indat.dat").read_text() == "OLD DATA\n"
    rej = Path(b["backup"]) / "rejected" / "TPoint"
    assert (rej / "Super Model Outmod.dat").read_text() == "BAD MODEL\n"
    assert (rej / "TPoint new run outmod.dat").read_text() == "NEW FILE\n"
    bak = Path(b["backup"]) / "TPoint"
    assert (bak / "Super Model Outmod.dat").read_text() == "OLD MODEL\n"
    assert (bak / "Super Model Indat.dat").read_text() == "OLD DATA\n"
    assert (Path(b["backup"]) / tr.BEFORE_NAME).is_file()
    assert res["restored"]["errors"] == []


def test_judge_without_restore_only_reports(tmp_path):
    root = _tree(tmp_path)
    _backup(tmp_path, root)
    (root / "Super Model Outmod.dat").write_text("BAD MODEL\n", encoding="ascii")
    res, _s, _r = _judge(tmp_path, root, _st(rms=40.0, mtime="new"), state=None)
    assert res["status"] == "bad_not_restored" and res["restored"] is None
    assert (root / "Super Model Outmod.dat").read_text() == "BAD MODEL\n"


def test_judge_restore_refused_unless_disarmed(tmp_path):
    root = _tree(tmp_path)
    _backup(tmp_path, root)
    (root / "Super Model Outmod.dat").write_text("BAD MODEL\n", encoding="ascii")
    res, _s, _r = _judge(tmp_path, root, _st(rms=40.0, mtime="new"), restore=True,
                         state="ARMED")
    assert res["status"] == "refused" and "armer idle" in res["reason"]
    assert (root / "Super Model Outmod.dat").read_text() == "BAD MODEL\n"


def test_judge_no_change_restores_nothing(tmp_path):
    root = _tree(tmp_path)
    _backup(tmp_path, root)
    res, _s, _r = _judge(tmp_path, root, _st(rms=15.8), restore=True)
    assert res["status"] == "no_change" and res["restored"] is None
    assert not (Path(res["backup"]) / "rejected").exists()


def test_judge_picks_the_newest_backup_and_fences_backup_dir(tmp_path):
    import json
    root = _tree(tmp_path)
    b, _s = _backup(tmp_path, root)
    old = tmp_path / "backup" / "20261001T000000Z"
    old.mkdir()
    (old / tr.BEFORE_NAME).write_text(json.dumps({"files": [], "before": _st(rms=99.0)}))
    (tmp_path / "backup" / "20991231T000000Z").mkdir()    # no refit_before.json
    assert tr.latest_backup(str(tmp_path / "backup")) == Path(b["backup"])
    res, _s, _r = _judge(tmp_path, root, _st(rms=15.8))
    assert res["status"] == "no_change" and res["backup"] == b["backup"]
    res, _s, _r = _judge(tmp_path, root, _st(rms=15.8), backup_dir="20261001T000000Z")
    assert res["backup"] == str(old)
    res, _s, _r = _judge(tmp_path, root, _st(), backup_dir=str(tmp_path))
    assert res["status"] == "refused" and "not under" in res["reason"]
    res, _s, _r = _judge(tmp_path, root, _st(), backup_dir="..")
    assert res["status"] == "refused"


def test_judge_refused_without_a_backup(tmp_path):
    root = _tree(tmp_path)
    res, _s, _r = _judge(tmp_path, root, _st())
    assert res["status"] == "refused" and "--backup first" in res["reason"]


def test_cli_remote_step_flag_checks(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: _cfg(tmp_path))
    for argv in (["--backup", "--judge"], ["--backup", "--go"],
                 ["--restore-if-bad"], ["--backup", "--backup-dir", "x"]):
        r = CliRunner().invoke(cli.app, ["tpoint-refit", *argv])
        assert r.exit_code == 2, (argv, r.output)
