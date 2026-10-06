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
