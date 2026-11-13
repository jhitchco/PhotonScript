"""ProTrack re-judged once the RC16 mount tracks (2026-10-12): the arm-time
TheSky audit runs with the mount parked (ProTrack row INFO "not judged while
parked"); the nina watch loop runs just that row again once NINA reports the
RC16 mount unparked and tracking (or at its first light), stores it in the
night's audit and pushes once when ProTrack is OFF on an unguided night."""
import asyncio
from datetime import datetime

from photonscript.scheduler import protrack_recheck as pr
from photonscript.scheduler import thesky_audit as ta
from photonscript.scheduler.nina_watchdog import NinaWatch
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import thesky_client as tc

NOW = datetime(2026, 10, 13, 2, 30)      # 22:30 EDT on the night of 2026-10-12
TRACKING = "connected=1;parked=0;tracking=1;ra_h=1;dec_d=2;last_slew_error=0"
PARKED = "connected=1;parked=1;tracking=0;ra_h=1;dec_d=2;last_slew_error=0"
PT_OFF = "apply_corrections=1;points=250;protrack_active=0;protrack_adjustments=0"
PT_ON = "apply_corrections=1;points=250;protrack_active=1;protrack_adjustments=1"


def _cfg(tmp_path, **kw):
    kw.setdefault("data_dir", tmp_path / "data")
    kw.setdefault("nina_logs_dir", str(tmp_path / "ninalogs"))
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    return PhotonScriptConfig(_env_file=None, **kw)


class _Fake(tc.TheSkyClient):
    """The TheSky audit fake: canned replies per read-only script."""
    def __init__(self, replies):
        super().__init__("x", 3040)
        self.replies = replies
        self.ran = []

    def run_script(self, js):
        for name, reply in self.replies.items():
            if js == tc.READ_ONLY_JS[name]:
                self.ran.append(name)
                return reply
        self.ran.append("?")
        return "?=?"


def _fake(mount=TRACKING, pt=PT_OFF):
    return _Fake({"ping": "ok", "mount_flags": mount, "tpoint_flags": pt})


def _parked_audit(cfg):
    """The arm-time audit by day: ProTrack not judged (parked)."""
    fake = _fake(mount=PARKED)
    obs = {s: {} for s in ta.SOURCES}
    obs["thesky-script"], _ = ta.collect_thesky(cfg, fake)
    obs["armer_state"] = "ARMED"
    res = ta.evaluate(ta.load_desired(cfg), obs, cfg)
    audit = {"t_utc": "2026-10-12T20:00:00Z", "night": "2026-10-12", "reason": "arm", **res,
             "fail_ids": [r["id"] for r in res["rows"] if r["status"] == "fail"]}
    ta.save(cfg, audit)
    p = next(r for r in audit["rows"] if r["id"] == "protrack_on")
    assert p["status"] == "info" and p["current"] == "not judged (parked)"
    return audit


# --------------------------------------------------------------------------
# recheck(): the one row, read only, stored in the night's audit
# --------------------------------------------------------------------------

def test_recheck_reads_only_the_protrack_scripts(tmp_path):
    cfg = _cfg(tmp_path)
    fake = _fake()
    res = pr.recheck(cfg, "tracking", client=fake, armer_state="RUNNING", persist=False, now=NOW)
    assert set(fake.ran) == {"ping", "mount_flags", "tpoint_flags"}
    assert res["night"] == "2026-10-12" and res["trigger"] == "tracking"
    row = res["row"]
    assert row["id"] == "protrack_on" and row["status"] == "fail"
    assert row["current"] == "OFF" and row["protrack"] == "off"
    assert row["rechecked"] == {"t_utc": "2026-10-13T02:30:00Z", "trigger": "tracking"}
    for js in (tc.READ_ONLY_JS["mount_flags"], tc.READ_ONLY_JS["tpoint_flags"]):
        assert "Connect(" not in js


def test_recheck_replaces_the_parked_row_and_keeps_history(tmp_path):
    cfg = _cfg(tmp_path)
    audit = _parked_audit(cfg)
    n_rows = len(audit["rows"])
    pr.recheck(cfg, "tracking", client=_fake(), armer_state="RUNNING", now=NOW)
    a = ta.load_night(cfg, "2026-10-12")
    assert len(a["rows"]) == n_rows and a["reason"] == "arm"
    p = next(r for r in a["rows"] if r["id"] == "protrack_on")
    assert p["status"] == "fail" and p["current"] == "OFF"
    assert "protrack_on" in a["fail_ids"] and a["counts"]["fail"] >= 1
    assert a["rows"][0]["status"] == "fail"           # still sorted worst first
    assert len(a["rechecks"]) == 1 and a["rechecks"][0]["trigger"] == "tracking"
    assert a["rechecks"][0]["protrack"] == "off"
    # latest.json (the Guiding tab) follows; protrack_status reads it off
    latest = ta.load_latest(cfg)
    assert latest["rechecks"] == a["rechecks"]
    assert ta.protrack_status(latest)["state"] == "off"
    assert pr.done_tonight(cfg, "2026-10-12")


def test_recheck_without_an_arm_audit_writes_a_one_row_audit(tmp_path):
    cfg = _cfg(tmp_path)
    pr.recheck(cfg, "first light", client=_fake(pt=PT_ON), armer_state="RUNNING", now=NOW)
    a = ta.load_night(cfg, "2026-10-12")
    assert a["reason"] == "protrack-recheck" and [r["id"] for r in a["rows"]] == ["protrack_on"]
    assert a["rows"][0]["status"] == "pass" and a["counts"]["pass"] == 1
    assert a["fail_ids"] == []


def test_recheck_does_not_overwrite_another_nights_latest(tmp_path):
    cfg = _cfg(tmp_path)
    store.write_json(ta.audit_dir(cfg) / "latest.json", {"night": "2026-10-13", "rows": []})
    pr.recheck(cfg, "tracking", client=_fake(), armer_state="RUNNING", now=NOW)
    assert ta.load_latest(cfg)["night"] == "2026-10-13"
    assert ta.load_night(cfg, "2026-10-12")["rechecks"]


def test_recheck_thesky_unreachable_is_never_a_pass(tmp_path):
    cfg = _cfg(tmp_path)
    res = pr.recheck(cfg, "tracking", client=_Fake({"ping": "no"}), armer_state="RUNNING",
                     persist=False, now=NOW)
    assert res["row"]["status"] != "pass"
    assert res["source"]["ok"] is False


# --------------------------------------------------------------------------
# tick(): when, once, and the one push
# --------------------------------------------------------------------------

class _Armer:
    def __init__(self, state="RUNNING", guided=False):
        self.state = state
        self.guided = guided

    def _use_guiding(self):
        return self.guided


def _monitor(tracked=False, leaf=None, exp=None, night="2026-10-12"):
    m = NinaWatch()
    m.night = night
    s = m._rig("rc16")
    s["tracked"] = tracked
    s["reads"] = {"seq_leaf": leaf, "seq_exposure_s": exp}
    return m


def _run_tick(cfg, armer, monitor, client, state=None):
    sent, runs = [], []

    async def _notify(c, msg, **kw):
        sent.append((msg, kw))

    def _run(c, trig, armer_state=None):
        runs.append(trig)
        return pr.recheck(c, trig, client=client, armer_state=armer_state, now=NOW)
    state = state or pr.Recheck()
    res = asyncio.run(pr.tick(cfg, armer, monitor, now=NOW, run=_run, notify=_notify,
                              state=state))
    return res, sent, runs, state


def test_tick_unguided_tracking_protrack_off_pushes_once(tmp_path):
    cfg = _cfg(tmp_path)
    _parked_audit(cfg)
    res, sent, runs, st = _run_tick(cfg, _Armer(), _monitor(tracked=True), _fake())
    assert runs == ["tracking"] and res["row"]["protrack"] == "off"
    assert len(sent) == 1
    msg, kw = sent[0]
    assert msg.startswith("ProTrack is off and the night is unguided: subs will trail")
    assert "Activate ProTrack" in msg and msg.isascii()
    assert kw["title"] == "PhotonScript ProTrack"
    # next tick: nothing (memory latch); a restart: nothing (disk record)
    _r, sent2, runs2, _ = _run_tick(cfg, _Armer(), _monitor(tracked=True), _fake(), state=st)
    assert runs2 == [] and sent2 == []
    _r, sent3, runs3, _ = _run_tick(cfg, _Armer(), _monitor(tracked=True), _fake())
    assert runs3 == [] and sent3 == []


def test_tick_no_push_when_on_guided_or_unknown(tmp_path):
    cfg = _cfg(tmp_path)
    _r, sent, runs, _ = _run_tick(cfg, _Armer(), _monitor(tracked=True), _fake(pt=PT_ON))
    assert runs == ["tracking"] and sent == []
    cfg = _cfg(tmp_path / "g")
    _r, sent, runs, _ = _run_tick(cfg, _Armer(guided=True), _monitor(tracked=True), _fake())
    assert runs == ["tracking"] and sent == []          # judged and stored, no push
    assert ta.load_night(cfg, "2026-10-12")["rechecks"][0]["protrack"] == "off"
    cfg = _cfg(tmp_path / "u")
    _r, sent, runs, _ = _run_tick(cfg, _Armer(), _monitor(tracked=True),
                                  _fake(pt="apply_corrections=1"))
    assert runs == ["tracking"] and sent == []          # not readable: no push


def test_tick_waits_for_tracking_or_first_light(tmp_path):
    cfg = _cfg(tmp_path)
    for mon, armer in ((_monitor(), _Armer()),                              # parked
                       (_monitor(leaf="Take Exposure", exp=300), _Armer("ARMED")),
                       (_monitor(leaf="Take Exposure", exp=None), _Armer()),
                       (_monitor(leaf="Flat Wizard Exposure", exp=2), _Armer()),
                       (_monitor(tracked=True), _Armer("DISARMED")),
                       (_monitor(tracked=True, night="2026-10-11"), _Armer())):
        _r, sent, runs, _ = _run_tick(cfg, armer, mon, _fake())
        assert runs == [] and sent == [], (mon.rigs, armer.state)
    _r, sent, runs, _ = _run_tick(cfg, _Armer(), _monitor(leaf="Smart Exposure", exp=300),
                                  _fake())
    assert runs == ["first light"] and len(sent) == 1


def test_tick_off_with_the_audit(tmp_path):
    cfg = _cfg(tmp_path, thesky_audit_enabled=False)
    _r, sent, runs, _ = _run_tick(cfg, _Armer(), _monitor(tracked=True), _fake())
    assert runs == [] and sent == []
