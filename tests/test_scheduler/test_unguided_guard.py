"""2026-10-13 unguided guard: nothing may guide on an UNGUIDED night.

The night of 2026-10-13 was armed unguided, but NINA #1 had PHD2 connected
as its guider; NINA's meridian flip started guiding (02:28) and every
autofocus after it restarted it (02:57, 03:40, 04:18). The fixture
2026-10-13_unguided_flip_guider_start.json is NINA #1's event history of
that night (ninaAPI GET /event-history, points / image saves left out).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import unguided_guard as ug
from photonscript.scheduler.armer import Armer
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (
    ExposurePlan, FilterType, NinaSequenceTarget,
)

FIX = (Path(__file__).parent / "fixtures" / "nina_events"
       / "2026-10-13_unguided_flip_guider_start.json")


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    return PhotonScriptConfig(_env_file=None, **kw)


def _events():
    return json.loads(FIX.read_text(encoding="utf-8"))


def _local(s):
    """'2026-10-14 02:28:03' scope local (UTC-6 in October) -> naive UTC."""
    return datetime.fromisoformat(s) + timedelta(hours=6)


# ---- what started it ---------------------------------------------------------

def test_attribute_starts_reproduces_the_1013_night():
    starts = ug.attribute_starts(_events())
    by_local = [((s["t"] - timedelta(hours=6)).strftime("%H:%M"), s["by"])
                for s in starts]
    # 20:14 is the end of the tracking test (sequence finishing), then the
    # flip and the three autofocus resumes of the night
    assert by_local == [("20:14", "nina"), ("02:28", "meridian_flip"),
                        ("02:57", "autofocus"), ("03:40", "autofocus"),
                        ("04:18", "autofocus")]
    s = ug.latest_start(_events(), until=_local("2026-10-14 02:40:00"))
    assert s["by"] == "meridian_flip"
    assert ug.cause_text("meridian_flip") == "NINA started PHD2 after the meridian flip"


def test_latest_start_none_before_any_start():
    assert ug.latest_start(_events(), until=_local("2026-10-13 19:00:00")) is None
    assert ug.latest_start(None) is None


# ---- sequences ---------------------------------------------------------------

def _gen(guided: bool, unguided_dither: bool = False):
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.nina_sequence_json import generate_nina_json
    t = NinaSequenceTarget(name="Heart", ra_hours=2.55, dec_degrees=61.45,
                           exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                   exposure_seconds=300,
                                                   count=10, gain=200)],
                           start_guiding=guided)
    seq = build_sequence_for_night("T", [t])
    seq.wait_until_local = "21:00:00"
    return json.loads(generate_nina_json(seq, unguided_dither=unguided_dither))


def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from _walk(v)


def _connected_devices(seq):
    return [d.get("SelectedDevice") for d in _walk(seq)
            if "ConnectEquipment" in str(d.get("$type", ""))]


def test_unguided_sequence_never_connects_the_guider():
    seq = _gen(guided=False)
    assert "Guider" not in _connected_devices(seq)
    assert "Mount" in _connected_devices(seq)
    notes = [d.get("Text") for d in _walk(seq)
             if "Annotation" in str(d.get("$type", ""))]
    assert any("Guider NOT connected" in (n or "") for n in notes)
    assert not any("StartGuiding" in str(d.get("$type", "")) for d in _walk(seq))


def test_guided_and_direct_guider_sequences_connect_it():
    assert "Guider" in _connected_devices(_gen(guided=True))
    assert "Guider" in _connected_devices(_gen(guided=False, unguided_dither=True))


def test_lint_warns_on_an_unguided_run_that_connects_the_guider():
    from photonscript.scheduler.sequence_lint import lint
    seq = _gen(guided=False)
    assert not [f for f in lint(seq, guided=False).findings
                if f.rule == "unguided-guider"]
    guided_seq = _gen(guided=True)
    # the same Start area on an unguided run (StartGuiding removed by hand)
    for d in _walk(guided_seq):
        if "StartGuiding" in str(d.get("$type", "")):
            d["$type"] = "NINA.Sequencer.SequenceItem.Utility.Annotation, NINA.Sequencer"
    found = [f for f in lint(guided_seq, guided=False).findings
             if f.rule == "unguided-guider"]
    assert found and found[0].level == "WARN"
    assert not [f for f in lint(guided_seq, guided=False, unguided_dither=True).findings
                if f.rule == "unguided-guider"]


# ---- the check, with a fake scope --------------------------------------------

class FakeIO:
    def __init__(self, connected=True, state="Stopped", name="PHD2",
                 phd2="Stopped", events=None, disconnect_ok=True,
                 phd2_stop_ok=True):
        self.g = {"Connected": connected, "State": state,
                  **({"Name": name} if connected and name else {})}
        self.phd2 = phd2
        self.events = events
        self.disconnect_ok = disconnect_ok
        self.phd2_stop_ok = phd2_stop_ok
        self.calls, self.pushes, self.logged = [], [], []

    async def nina_guider(self):
        return None if self.g is None else {"Success": True, "Response": dict(self.g)}

    async def nina_stop(self):
        self.calls.append("nina_stop")
        if self.g and self.g.get("Connected"):
            self.g["State"] = "Stopped"
            self.phd2 = "Stopped"      # NINA's stop goes to PHD2
        return True

    async def nina_disconnect(self):
        self.calls.append("nina_disconnect")
        if self.disconnect_ok and self.g:
            self.g = {"Connected": False}
        return self.disconnect_ok

    async def nina_events(self):
        self.calls.append("nina_events")
        return self.events

    async def phd2_state(self):
        return self.phd2

    async def phd2_stop(self):
        self.calls.append("phd2_stop")
        if self.phd2_stop_ok:
            self.phd2 = "Stopped"
        return self.phd2_stop_ok, f"PHD2 now {self.phd2}"

    async def notify(self, message, priority=1):
        self.pushes.append((message, priority))

    async def event(self, value, detail, **extra):
        self.logged.append({"value": value, "detail": detail, **extra})

    def forbidden(self):
        """Nothing that could start guiding or touch the mount / camera."""
        return [c for c in self.calls if c not in
                ("nina_stop", "nina_disconnect", "nina_events", "phd2_stop")]


async def test_arm_disconnects_a_connected_phd2_guider(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO(connected=True, state="Stopped")
    st = ug.new_state("2026-10-13")
    r = await ug.check(cfg, io, st, phase="arm")
    assert r["status"] == "connected" and r["action"] == "disconnected"
    assert io.calls == ["nina_disconnect"] and io.forbidden() == []
    assert len(io.pushes) == 1 and io.pushes[0][1] == 0
    assert "meridian flip" in io.pushes[0][0]
    assert io.logged[-1]["value"] == "disconnected"
    r = await ug.check(cfg, io, st, phase="dispatch")
    assert r["status"] == "clear" and len(io.pushes) == 1


async def test_alert_policy_sends_nothing_and_pages_the_manual_step(tmp_path):
    cfg = _cfg(tmp_path, unguided_guider_policy="alert")
    io = FakeIO(connected=True)
    st = ug.new_state("n")
    r = await ug.check(cfg, io, st, phase="arm")
    assert r["action"] is None and io.calls == []
    assert len(io.pushes) == 1 and io.pushes[0][1] == 1
    assert "Equipment > Guider > Disconnect" in io.pushes[0][0]
    await ug.check(cfg, io, st, phase="watch")
    assert len(io.pushes) == 1          # once a night


async def test_off_policy_does_nothing(tmp_path):
    cfg = _cfg(tmp_path, unguided_guider_policy="off")
    io = FakeIO(connected=True, state="Guiding", phd2="Guiding")
    assert (await ug.check(cfg, io, ug.new_state("n"), phase="watch")) == {"status": "off"}
    assert io.calls == [] and io.pushes == []
    assert ug.policy(_cfg(tmp_path, unguided_guider_policy="bogus")) == "disconnect"


async def test_direct_guider_is_left_alone(tmp_path):
    io = FakeIO(connected=True, name="Direct Guider", state="Guiding")
    r = await ug.check(_cfg(tmp_path), io, ug.new_state("n"), phase="watch")
    assert r["status"] == "direct" and io.calls == [] and io.pushes == []


async def test_watch_stops_flip_started_guiding_once_per_episode(tmp_path):
    """The 10-13 replay: 02:28 the flip starts PHD2; the guard stops and
    disconnects it, names the flip, pushes once; a new episode pushes again."""
    cfg = _cfg(tmp_path)
    io = FakeIO(connected=True, state="Guiding", phd2="Guiding", events=_events())
    st = ug.new_state("2026-10-13")
    now = _local("2026-10-14 02:29:00")
    r = await ug.check(cfg, io, st, phase="watch", now=now)
    assert r["status"] == "guiding" and r["by"] == "meridian_flip"
    assert io.calls[:3] == ["nina_events", "nina_stop", "nina_disconnect"]
    assert "phd2_stop" not in io.calls        # NINA's stop already stopped PHD2
    assert io.forbidden() == []
    det = [e for e in io.logged if e["value"] == "detect"]
    assert det and det[0]["by"] == "meridian_flip"
    assert len(io.pushes) == 1
    msg, prio = io.pushes[0]
    assert prio == 1 and msg.startswith("NINA started PHD2 after the meridian flip")
    assert "Stopped and disconnected" in msg
    # guiding gone: the episode closes
    io.phd2 = "Stopped"
    r = await ug.check(cfg, io, st, phase="watch", now=now + timedelta(seconds=30))
    assert r["status"] == "clear" and io.logged[-1]["value"] == "clear"
    # someone reconnects and the 02:57 autofocus resume starts it again
    io.g = {"Connected": True, "Name": "PHD2", "State": "Guiding"}
    io.phd2 = "Guiding"
    r = await ug.check(cfg, io, st, phase="watch", now=_local("2026-10-14 02:58:00"))
    assert r["by"] == "autofocus" and len(io.pushes) == 2
    assert io.pushes[1][0].startswith("NINA restarted PHD2 after an autofocus")


async def test_watch_stops_phd2_guiding_outside_nina(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO(connected=False, phd2="Guiding")
    st = ug.new_state("n")
    r = await ug.check(cfg, io, st, phase="watch")
    assert r["by"] == "phd2" and io.calls == ["phd2_stop"]
    assert "NINA never started it" in io.pushes[0][0]


async def test_watch_failed_stop_pages_priority_with_manual_step(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO(connected=False, phd2="LostLock", phd2_stop_ok=False)
    st = ug.new_state("n")
    now = datetime(2026, 10, 14, 9, 0)
    await ug.check(cfg, io, st, phase="watch", now=now)
    assert len(io.pushes) == 1 and io.pushes[0][1] == 2
    assert "could not stop it" in io.pushes[0][0]
    assert "Equipment > Guider > Disconnect" in io.pushes[0][0]
    # within the retry window: no new command, no new push
    await ug.check(cfg, io, st, phase="watch", now=now + timedelta(seconds=30))
    assert io.calls.count("phd2_stop") == 1 and len(io.pushes) == 1
    # after it: tried again, still one push for the episode
    await ug.check(cfg, io, st, phase="watch", now=now + timedelta(seconds=200))
    assert io.calls.count("phd2_stop") == 2 and len(io.pushes) == 1


async def test_alert_policy_while_guiding_names_the_cause(tmp_path):
    cfg = _cfg(tmp_path, unguided_guider_policy="alert")
    io = FakeIO(connected=True, state="Guiding", phd2="Guiding", events=_events())
    await ug.check(cfg, io, ug.new_state("n"), phase="watch",
                   now=_local("2026-10-14 03:41:00"))
    assert [c for c in io.calls if c != "nina_events"] == []
    assert io.pushes[0][0].startswith("NINA restarted PHD2 after an autofocus")
    assert "nothing sent" in io.pushes[0][0]


async def test_disconnect_failure_pages_once(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO(connected=True, disconnect_ok=False)
    st = ug.new_state("n")
    r = await ug.check(cfg, io, st, phase="arm")
    assert r["action"] == "failed" and io.pushes[0][1] == 1
    assert "could not disconnect" in io.pushes[0][0]


async def test_unreadable_scope_is_unknown_and_quiet(tmp_path):
    io = FakeIO()
    io.g = None
    io.phd2 = None
    r = await ug.check(_cfg(tmp_path), io, ug.new_state("n"), phase="watch")
    assert r["status"] == "unknown" and io.calls == [] and io.pushes == []


# ---- armer wiring ------------------------------------------------------------

def _armer(tmp_path, mode="unguided", state="RUNNING", **cfg):
    a = Armer(_cfg(tmp_path, **cfg))
    a.guiding_override = mode
    a.state = state
    a.plan = {"night_of": "2026-10-13"}
    return a


def test_guider_unwanted_only_on_an_armed_unguided_night(tmp_path):
    assert _armer(tmp_path)._guider_unwanted() is True
    assert _armer(tmp_path, mode="guided")._guider_unwanted() is False
    assert _armer(tmp_path, state="DISARMED")._guider_unwanted() is False
    assert _armer(tmp_path, state="WATCHING")._guider_unwanted() is False
    assert _armer(tmp_path, unguided_dither=True)._guider_unwanted() is False
    assert _armer(tmp_path, unguided_guider_policy="off")._guider_unwanted() is False


async def test_connect_all_skips_the_guider_on_an_unguided_night(tmp_path, monkeypatch):
    from photonscript.scheduler import preflight
    seen = []

    async def _ensure(cfg, dev, attempts=2):
        seen.append(dev)
        return True, {}, None
    monkeypatch.setattr(preflight, "_ensure_connected", _ensure)
    res = await _armer(tmp_path).connect_all("rc16")
    assert "guider" not in seen and res["guider"] == "skipped (unguided night)"
    assert "mount" in seen and "camera" in seen
    seen.clear()
    await _armer(tmp_path, mode="guided").connect_all("rc16")
    assert "guider" in seen


async def test_armer_guard_runs_only_unguided_and_uses_guider_paths(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    keys = []
    g = {"Connected": True, "Name": "PHD2", "State": "Stopped"}

    async def _nina(key, *x, **k):
        keys.append(key)
        if key == "guider":
            return {"Success": True, "Response": dict(g)}
        if key == "guider_disconnect":
            g.clear()
            g["Connected"] = False
        return {"Success": True, "Response": "ok"}
    monkeypatch.setattr(a, "_nina", _nina)

    async def _none(self, *x, **k):
        return None
    from photonscript.scheduler.night_pilot import PilotIO
    monkeypatch.setattr(PilotIO, "phd2_state", _none)
    pushes = []

    async def _notify(cfg, msg, title="", priority=0, **k):
        pushes.append((title, msg, priority))
        return True
    monkeypatch.setattr("photonscript.shared.pushover.notify", _notify)
    r = await a._unguided_guard("arm")
    assert r["action"] == "disconnected"
    assert set(keys) <= {"guider", "guider_disconnect"}
    assert pushes and pushes[0][0] == ug.ALERT_TITLE
    assert armer_mod.NINA_PATHS["guider_disconnect"] == ["/equipment/guider/disconnect"]
    ev = (tmp_path / "data" / "runs").glob("*_events.jsonl")
    lines = [json.loads(x) for p in ev for x in p.read_text().splitlines()]
    assert any(e["kind"] == ug.KIND and e["value"] == "disconnected" for e in lines)
    keys.clear()
    g.update({"Connected": True, "Name": "PHD2", "State": "Guiding"})
    a.guiding_override = "guided"
    assert await a._unguided_guard("watch") is None and keys == []


async def test_dispatch_checks_the_guard_before_loading(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    order = []
    monkeypatch.setattr(a, "_dispatch", lambda: (order.append("build"), False)[1])

    async def _noop(*x, **k):
        return None

    async def _guard(phase):
        order.append(f"guard:{phase}")
    monkeypatch.setattr(a, "_check_recipe_before_redispatch", _noop)
    monkeypatch.setattr(a, "_send_recipe_alert", _noop)
    monkeypatch.setattr(armer_mod, "notify", _noop)
    monkeypatch.setattr(a, "_unguided_guard", _guard)
    assert await a._dispatch_and_start(fail_state=None) is False
    assert order == ["build"]                  # a failed build loads nothing
    sp = tmp_path / "seq.json"
    sp.write_text("{}")
    a.sequence_path = sp

    def _ok():
        order.append("build")
        return True
    monkeypatch.setattr(a, "_dispatch", _ok)
    monkeypatch.setattr(a, "_check_focus_cal_before_redispatch", _noop)
    monkeypatch.setattr(a, "_wait_nina1_stopped", _noop)

    async def _nina(key, *x, **k):
        order.append(key)
        return None if key == "sequence_load" else {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)
    order.clear()
    await a._dispatch_and_start(fail_state=None)
    assert order.index("guard:dispatch") < order.index("sequence_load")


# ---- sub doctor names the cause ----------------------------------------------

def test_sub_doctor_names_nina_flip_as_the_cause(tmp_path):
    from photonscript.scheduler import sub_doctor as sd
    gs = sd.guider_start_at(_events(), [], _local("2026-10-14 02:40:00"))
    assert gs["by"] == "meridian_flip"
    run_events = [{"t": "2026-10-14T09:00:00Z", "kind": ug.KIND, "value": "detect",
                   "by": "autofocus", "start_t": "2026-10-14T08:57:20Z"}]
    gs2 = sd.guider_start_at(_events(), run_events, _local("2026-10-14 03:00:00"))
    assert gs2["by"] == "autofocus"
    assert ug.start_from_run_events(run_events)["by"] == "autofocus"


def test_sub_doctor_guider_conflict_headline(tmp_path, monkeypatch):
    from photonscript.scheduler import sub_doctor as sd
    from photonscript.scheduler import sub_pairs as sp
    cfg = _cfg(tmp_path)
    t0 = datetime(2026, 10, 14, 8, 30)
    win = [{"rig": "rc16", "file": f"f{i}.fits", "time": (t0 + timedelta(minutes=5 * i)
            ).isoformat() + "Z"} for i in range(3)]

    class _Sub(dict):
        def __missing__(self, k):     # every other sub field: not set
            return None

    def _pair(rig, r, subs, bases):
        return {"verdict": "both_trailing", "sub": _Sub({
            "excluded": False, "bad": True, "trailing": True, "soft": False,
            "stars_low": False, "focuser_zero": False, "ecc": 0.8, "hfr": 9.0,
            "fwhm": 5.0, "stars": 300, "exp_s": 300, "fails": ["ecc"],
            "guide_state": "guiding"})}
    monkeypatch.setattr(sp, "pair_for", _pair)
    monkeypatch.setattr(sp, "baselines", lambda subs: {})
    ctx = {"armed": "unguided", "phd2": {"state": "guiding", "rms_arcsec": 37.0},
           "calibration": {"grade": "FAIL", "reasons": ["axes 78 deg from perpendicular"]},
           "guider_start": {"t": _local("2026-10-14 02:28:03"), "by": "meridian_flip"}}
    d = sd.diagnose(cfg, "rc16", win, subs=win, ctx=ctx)
    assert d["cause"] == "guider_conflict"
    assert d["headline"].startswith(
        "NINA started PHD2 after the meridian flip (08:28Z) in an unguided night")
    assert "Disconnect the guider in NINA #1" in d["fix"]
    assert d["action"]["id"] == "stop_phd2"
    assert d["evidence"]["guider_start"]["by"] == "meridian_flip"
    ctx.pop("guider_start")
    d = sd.diagnose(cfg, "rc16", win, subs=win, ctx=ctx)
    assert d["headline"].startswith("PHD2 is guiding on an UNGUIDED night")


# ---- 20:38:46 GUIDER-CONNECTED, 20:38:48 SEQUENCE-STARTING: no PhotonScript
# path connects the guider on an unguided night ---------------------------------

def _record_connects(monkeypatch):
    from photonscript.scheduler import preflight
    seen = []

    async def _ensure(cfg, dev, attempts=2):
        seen.append(dev)
        return True, {}, None
    monkeypatch.setattr(preflight, "_ensure_connected", _ensure)
    return seen


async def test_arm_unguided_never_connects_the_guider(tmp_path, monkeypatch):
    seen = _record_connects(monkeypatch)
    for mode, want in (("unguided", False), ("guided", True)):
        a = Armer(_cfg(tmp_path / mode, cooler_off_until_precool=False,
                       phd2_audit_enabled=False, thesky_audit_enabled=False))
        monkeypatch.setattr(armer_mod, "notify", _anoop)
        monkeypatch.setattr("photonscript.scheduler.night_plan.build_night_plan",
                            lambda cfg: {"night_of": "2026-10-13", "targets": ["Heart"],
                                         "preconfig_utc": "2026-10-14T00:30:00Z",
                                         "dark_hours": 8.0})
        monkeypatch.setattr(a, "_run", _anoop)
        monkeypatch.setattr(a, "_apps_alert", _anoop)
        monkeypatch.setattr(a, "_unguided_guard", _anoop)
        monkeypatch.setattr(a, "_start_nina2_mount_check", lambda *x: None)
        monkeypatch.setattr(a, "_nina", _anoop)
        seen.clear()
        await a.arm(mode)
        assert "mount" in seen and ("guider" in seen) is want, (mode, seen)


async def test_restore_unguided_never_connects_the_guider(tmp_path, monkeypatch):
    """A PhotonScript restart mid-night (deploy, crash) reattaches and
    connects everything again: not the guider on an unguided night."""
    seen = _record_connects(monkeypatch)
    cfg = _cfg(tmp_path)
    p = tmp_path / "data" / "armer_state.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"state": "RUNNING", "plan": {"night_of": "2026-10-13"},
                             "guiding_override": "unguided"}))
    a = Armer(cfg)
    created = []
    monkeypatch.setattr(armer_mod.asyncio, "create_task",
                        lambda coro: (created.append(coro), None)[1])
    assert a.restore() is True
    names = [c.cr_code.co_name for c in created]
    assert "connect_all_rigs" in names and "_unguided_guard" in names
    for c in created:
        if c.cr_code.co_name == "connect_all_rigs":
            await c
        else:
            c.close()
    assert "mount" in seen and "guider" not in seen


def test_redispatched_unguided_sequence_never_connects_the_guider(tmp_path, monkeypatch):
    """The armer's own (re-)dispatch writes the sequence NINA starts: on an
    unguided night it has no ConnectEquipment(Guider) anywhere."""
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import target_planner

    class _Store:
        projects: dict = {}
    t = NinaSequenceTarget(name="Heart", ra_hours=2.55, dec_degrees=61.45,
                           exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                   exposure_seconds=300,
                                                   count=10, gain=200)])
    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: [t])
    monkeypatch.chdir(tmp_path)
    a = Armer(_cfg(tmp_path, guided_default=True))
    a.plan = {"night_of": "2026-10-13", "dusk_utc": "2026-10-14T01:30:00Z",
              "dawn_utc": "2026-10-14T11:30:00Z"}
    a.guiding_override = "unguided"
    assert a._dispatch() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert "Guider" not in _connected_devices(seq)
    assert "Mount" in _connected_devices(seq)


async def _anoop(*a, **k):
    return None
