"""Suite-wide fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _cooler_gate_off(monkeypatch):
    """PS-61: the cooler gate is emitted only when its script exists on the
    machine, so a generated sequence would differ between the desktop and
    the scope PC (where deploy\\cooler-gate.cmd exists). Pin it off for the
    suite; tests/test_scheduler/test_ps61_cooler_gate.py turns it on."""
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "off")


@pytest.fixture(autouse=True)
def _load_validation_no_settle(monkeypatch):
    """PS-132: the post-load NINA validation check waits a few seconds for
    NINA to validate; no test should sleep for it."""
    monkeypatch.setenv("PS_NINA_LOAD_VALIDATION_SETTLE_S", "0")


@pytest.fixture(autouse=True)
def _settle_gate_off(monkeypatch):
    """PS-27: like the cooler gate, pin the Piggy-600 settle gate off
    (deploy\\settle-gate.cmd);
    tests/test_scheduler/test_ps27_split_guard.py turns it on."""
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "false")


@pytest.fixture(autouse=True)
def _safety_crosscheck_off(monkeypatch):
    """PS-1: the armer's safety cross-check reads NINA #2 over HTTP on an
    unsafe onset; no test should touch the network for it.
    tests/test_scheduler/test_ps1_safety_analytics.py turns it on."""
    monkeypatch.setenv("PS_SAFETY_CROSSCHECK", "off")


@pytest.fixture(autouse=True)
def _app_lifecycle_alert_off(monkeypatch):
    """PS-170: the armer pages at arm when NINA / PHD2 do not answer on their
    ports, which they never do on a test machine; no test should count that
    push. tests/test_scheduler/test_ps170_app_lifecycle.py turns it on."""
    monkeypatch.setenv("PS_APP_LIFECYCLE_ALERT", "false")


@pytest.fixture(autouse=True)
def _ttl_cache_clear():
    """PS-174: dashboard status endpoints share a few-second cache; no test
    should see another test's cached NINA reads."""
    from photonscript.shared import ttl_cache
    ttl_cache.invalidate()
    yield
    ttl_cache.invalidate()


@pytest.fixture(autouse=True)
def _settings_ledger_off(monkeypatch):
    """Settings ledger: the armer would snapshot NINA, PHD2 and TheSky (over
    the network) on every state change; no test should. The ledger's own
    tests (tests/test_scheduler/test_settings_ledger.py) turn it on."""
    monkeypatch.setenv("PS_SETTINGS_LEDGER_ENABLED", "false")


@pytest.fixture(autouse=True)
def _pixinsight_lock_isolated(monkeypatch, tmp_path):
    """The PixInsight queue lock (integration/pilock.py) lives in
    %LOCALAPPDATA%; every test gets its own lock file instead, so the suite
    never sees (or leaves) the desktop's real lock."""
    monkeypatch.setenv("PS_PIXINSIGHT_LOCK", str(tmp_path / "pixinsight.lock"))
    from photonscript.integration import pilock
    pilock._held.clear()
    yield
    pilock._held.clear()


@pytest.fixture(autouse=True)
def _pixinsight_settings_isolated(monkeypatch, tmp_path_factory):
    """Gaia preflight (integration/pigaia.py): no test reads the desktop's
    real %APPDATA%/Pleiades settings; an empty folder = Gaia unknown, the
    finish keeps its plate solve. tests/test_integration/test_pi_watchdog.py
    points it at fabricated settings."""
    monkeypatch.setenv("PS_PIXINSIGHT_SETTINGS_DIR", str(tmp_path_factory.mktemp("pleiades-none")))


@pytest.fixture(autouse=True)
def _tpoint_preflight_off(monkeypatch):
    """2026-10-16: the arm / dispatch TPoint preflight reads TheSky's TPoint
    files from the user's Documents, so a dispatch would differ between the
    desktop and the scope PC. Pin it off for the suite;
    tests/test_scheduler/test_tpoint_preflight.py turns it on."""
    monkeypatch.setenv("PS_TPOINT_PREFLIGHT", "off")


@pytest.fixture(autouse=True)
def _generator_config_fresh(monkeypatch):
    """The sequence generator caches its config (nina_sequence_json
    ._gen_cfg_cache) the first time it builds one, from the environment of
    whatever test ran first. A test that set PS_COOLER_GATE_MODE=skip
    (test_ps61_cooler_gate.py) left a gate-on config behind, and later
    tests saw ExternalScript cooler gates with the suite's gate pinned off.
    Every test starts with no cached config; monkeypatch restores the
    module value afterwards."""
    from photonscript.scheduler import nina_sequence_json as nsj
    monkeypatch.setattr(nsj, "_gen_cfg_cache", None)


@pytest.fixture(autouse=True)
def _pushover_v2_isolated(monkeypatch, tmp_path_factory):
    """PS-212: the act cutoff follows the observatory's wall clock, so pin
    it off for the suite (cutoff == resume: never held); the cutoff tests
    pass their own times. A config left on the default data_dir (the real
    ~/.photonscript) keeps its fact state, storyline and story state in a
    per-test folder, so no run sees another run's "once per night"."""
    from pathlib import Path

    from photonscript.scheduler import night_story
    from photonscript.shared import pushover
    monkeypatch.setenv("PS_PUSHOVER_ACT_RESUME_LOCAL", "00:00")
    monkeypatch.setitem(pushover._DEFAULTS, "pushover_act_resume_local", "00:00")
    home = Path.home() / ".photonscript"
    d = tmp_path_factory.mktemp("po-home")

    def _default(c) -> bool:
        try:
            return Path(getattr(c, "data_dir", home)) == home
        except TypeError:
            return True
    st, sp, ns = pushover._state_path, pushover.story_path, night_story._state_file
    monkeypatch.setattr(pushover, "_state_path",
                        lambda c: d / "pushover_state.json" if _default(c) else st(c))
    monkeypatch.setattr(pushover, "story_path",
                        lambda c, n: d / "night_story" / f"{n}.jsonl" if _default(c) else sp(c, n))
    monkeypatch.setattr(night_story, "_state_file",
                        lambda c: d / "night_story" / "state.json" if _default(c) else ns(c))
