"""Shared fixtures for scheduler tests."""

import pytest


@pytest.fixture(autouse=True)
def _dark_evening_moon(monkeypatch):
    """Pin the sequence generator's live moon window to a dark evening.

    Without this, generator tests depend on tonight's real moon: on a bright
    night broadband is deferred and (since PS-27) a broadband-only target is
    skipped entirely, so a test's sequence would change with the calendar.
    Tests that exercise the moon rule override this with their own patch."""
    from photonscript.scheduler import nina_sequence_json as nsj
    monkeypatch.setattr(nsj, "_moon_window", lambda: {
        "available": True, "down_at_dusk": True, "illum_pct": 0,
        "rise_local_hh": None, "rise_local_mm": None})
