"""Suite-wide fixtures."""

import atexit
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

# --- the machine the suite runs on stays out of it ----------------------------
# 2026-11-02: the scope PC ran 112 failures against the desktop's handful,
# all of them the machine leaking in: its .env (PS_PIGGYBACK_ENABLED,
# PS_RC16_AF_POLICY, PS_LIBRARY_DIR, ...), its ~/.photonscript (QA store,
# goals DB) and the deploy\*.cmd scripts and NINA folders the config points
# at by absolute path. This runs at conftest import, before any test module
# imports photonscript, so class-level defaults built from Path.home() land in
# a throwaway home, and no PS_* variable of the shell reaches a test.
_SUITE_HOME = Path(tempfile.mkdtemp(prefix="ps-test-home-"))
atexit.register(shutil.rmtree, _SUITE_HOME, True)
for _k in [k for k in os.environ if k.upper().startswith("PS_")]:
    del os.environ[_k]
os.environ["USERPROFILE"] = os.environ["HOME"] = str(_SUITE_HOME)
# astropy keeps its download cache under the home too: the scope PC's has a
# cached Leap_Second.dat, which astropy re-opens through download_file (seen
# as a "download" by tests/test_health.py). Pin it to the suite home, so the
# per-test home below does not move it.
for _var, _sub in (("ASTROPY_CACHE_DIR", ".cache"), ("ASTROPY_CONFIG_DIR", ".config")):
    (_SUITE_HOME / _sub / "astropy").mkdir(parents=True)   # must exist
    os.environ[_var] = str(_SUITE_HOME / _sub / "astropy")

from photonscript.shared import config as _cfgmod  # noqa: E402
from photonscript.shared import envfile as _envfile  # noqa: E402

# No .env at all (repo root or cwd); a test that wants one passes _env_file.
_cfgmod.PhotonScriptConfig.model_config["env_file"] = None

# Settings whose default is an absolute path on one of the two machines
# (C:\astro\PhotonScript\deploy\*.cmd, C:\Users\jeremy\Documents\N.I.N.A,
# C:\Users\sleep\ninashare\Library, ...) default to the same path under a
# folder that does not exist, so the scope PC and the desktop build the same
# sequences. Only the default moves (an env var, a .env passed as _env_file
# or a keyword still win), and the tail is kept (...\deploy\cooler-gate.cmd,
# ...\NINA.exe) for the tests of the defaults' shape.
_ABSENT = _SUITE_HOME / "absent"
_MACHINE_DEFAULTS = {
    n: f.default for n, f in _cfgmod.PhotonScriptConfig.model_fields.items()
    if isinstance(f.default, str) and re.match(r"^[A-Za-z]:[\\/]", f.default)}
_MACHINE_PATH_FIELDS = sorted(_MACHINE_DEFAULTS)


def _set_defaults(values: dict) -> None:
    for n, v in values.items():
        _cfgmod.PhotonScriptConfig.model_fields[n].default = v
    _cfgmod.PhotonScriptConfig.model_rebuild(force=True)


_set_defaults({n: str(_ABSENT / v[3:]) for n, v in _MACHINE_DEFAULTS.items()})


@pytest.fixture(autouse=True)
def _machine_isolated(monkeypatch, tmp_path_factory):
    """Per test: a fresh home, data_dir and db_path, and the .env writers
    (envfile.env_path) aimed at a scratch file instead of the checkout's
    .env. Its folders sit outside the test's tmp_path, which stays the
    test's own. (Autouse fixtures run in name order, so this one must not
    clear PS_* variables: the gate pins below may already be set.)"""
    tmp_path = tmp_path_factory.mktemp("machine")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PS_DATA_DIR", str(home / ".photonscript"))
    monkeypatch.setenv("PS_DB_PATH", str(home / ".photonscript" / "photonscript.db"))
    real_env_path = _envfile.env_path
    monkeypatch.setattr(_envfile, "env_path", lambda root=None: (
        real_env_path(root) if root is not None else tmp_path / ".env"))
    # The dashboard app keeps its config and goal store in module globals,
    # built by the first test that asks (app.get_config / get_store) and seen
    # by every later one: calibration_plan.load_projects reads app._store
    # first, so a goal of an earlier test (or of the real projects.json) put
    # its 15 s HDR shorts into the owed darks of ps128 / ps188 /
    # closed_roof_first_lights_darks, depending on the test order.
    import sys
    app = sys.modules.get("photonscript.scheduler.app")
    if app is not None:
        monkeypatch.setattr(app, "_config", None)
        monkeypatch.setattr(app, "_store", None)


@pytest.fixture
def machine_defaults():
    """For a test of the code defaults themselves: the real machine-path
    defaults, for this one test."""
    redirected = {n: _cfgmod.PhotonScriptConfig.model_fields[n].default
                  for n in _MACHINE_PATH_FIELDS}
    _set_defaults(_MACHINE_DEFAULTS)
    yield
    _set_defaults(redirected)


@pytest.fixture(autouse=True)
def _no_real_pixinsight(monkeypatch):
    """A test may never start the installed PixInsight (runner.run_script's
    default exe is C:\\Program Files\\PixInsight\\bin\\PixInsight.exe, a module
    constant no config redirect reaches). Fakes (a test popen, or a stand-in
    exe under tmp_path) still run."""
    import subprocess

    from photonscript.integration import runner
    real_start = runner._start
    installed = os.path.normcase(os.path.abspath(runner.DEFAULT_EXE))

    def guarded(popen, args, env):
        if popen is subprocess.Popen and args and os.path.normcase(
                os.path.abspath(str(args[0]))) == installed:
            raise RuntimeError("a test tried to launch the installed PixInsight")
        return real_start(popen, args, env)
    monkeypatch.setattr(runner, "_start", guarded)


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
def _progression_sheets_off(monkeypatch):
    """Progression sheets: integrate / finish / blend add their run to
    <staging_root parent>/progression after every run; no other test should
    write a sheet. tests/test_integration/test_progression.py turns it on."""
    monkeypatch.setenv("PS_PROGRESSION_SHEETS", "off")


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


@pytest.fixture(autouse=True)
def _focuser_guard_off(monkeypatch):
    """Focuser position-loss guard: the armer's pre-config check reads each
    rig's focuser over ninaAPI; no test should touch the network for it.
    tests/test_scheduler/test_focuser_guard.py turns it on."""
    monkeypatch.setenv("PS_FOCUSER_GUARD_MODE", "off")
