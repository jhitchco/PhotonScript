"""Tests for filter mapping, DST handling, armer persistence, progress wiring."""

import json
from datetime import datetime
from pathlib import Path

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.localtime import utc_offset_hours
from photonscript.shared.models import CelestialTarget
from photonscript.scheduler.project_store import ProjectStore


def test_filter_name_map_roundtrip():
    config = PhotonScriptConfig()
    fwd = config.filter_name_map()
    assert fwd["Ha"] == "H"
    assert fwd["OIII"] == "O"
    assert fwd["SII"] == "S"
    rev = config.reverse_filter_map()
    assert rev["H"] == "Ha"
    assert rev["L"] == "L"


def test_generated_sequence_uses_nina_filter_names():
    from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.nina_sequence_json import generate_nina_json
    t = NinaSequenceTarget(name="M97", ra_hours=11.2, dec_degrees=55.0,
                           exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                   exposure_seconds=300, count=5)])
    content = generate_nina_json(build_sequence_for_night("T", [t]))
    # NINA profile names the Ha filter 'H' — the JSON must say 'H', not 'Ha'
    assert '"_name": "H"' in content
    assert '"_name": "Ha"' not in content


def test_dst_offset_changes_with_season():
    config = PhotonScriptConfig()  # America/Denver
    winter = utc_offset_hours(config, datetime(2026, 1, 15, 12, 0))
    summer = utc_offset_hours(config, datetime(2026, 7, 15, 12, 0))
    assert winter == -7.0  # MST
    assert summer == -6.0  # MDT


def test_armer_persists_and_ignores_stale_night(tmp_path):
    import asyncio
    from photonscript.scheduler.armer import Armer

    config = PhotonScriptConfig(data_dir=tmp_path)
    a = Armer(config)
    a.plan = {"night_of": "2020-01-01", "dawn_utc": "2020-01-02T12:00:00"}
    a._set_state("RUNNING", "test")
    saved = json.loads((Path(tmp_path) / "armer_state.json").read_text())
    assert saved["state"] == "RUNNING"

    async def _restore():
        b = Armer(config)
        return b.restore()
    # dawn long past -> restore refuses to reattach
    assert asyncio.run(_restore()) is False


def test_record_accepted_sub_matches_and_saves(tmp_path):
    config = PhotonScriptConfig(data_dir=tmp_path)
    store = ProjectStore(config)
    t = CelestialTarget(name="M 97", catalog_id="M 97", ra_hours=11.2,
                        dec_degrees=55.0, object_type="planetary nebula")
    proj = store.add_from_target(t, budget_hours=5.0)
    assert store.record_accepted_sub("M 97", "Ha") is True
    assert store.record_accepted_sub("M97", "OIII") is True     # no-space form
    assert store.record_accepted_sub("Unknown Thing", "Ha") is False
    reloaded = ProjectStore(config)
    ha = next(p for p in reloaded.projects[proj.id].exposure_plans
              if p.filter_type.value == "Ha")
    assert ha.acquired == 1
