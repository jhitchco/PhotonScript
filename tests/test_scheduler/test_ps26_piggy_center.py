"""PS-26: center a Piggy-600-driven target in the 600 mm frame.

Synthetic simultaneous solve pairs (an RC16 solve and a Piggy-600 solve a
fixed boresight offset away, per pier side) measure the offset; the
generator shifts the RC16 Center for a piggyback-driven target, keeps
RC16-driven targets as they are, and falls back to no shift (with an
annotation saying so) when no offset is measured yet."""

import json
import math
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import piggy_offset as po
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

M31 = (0.7123, 41.269)              # ra_hours, dec_degrees
WEST_OFF = (21.0, -13.0)            # Piggy E / N of the RC16, pier West (arcmin)
EAST_OFF = (-20.6, 12.7)            # about the negation, plus a little flexure


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data", **kw)


def _rec(rig, t, ra, dec, pier, target="M 31", rot=None, file=None):
    return {"rig": rig, "file": file or f"{rig}_{t:%H%M%S}.fits",
            "t": t.replace(microsecond=0).isoformat() + "Z",
            "solved_ra": ra, "solved_dec": dec, "rotation": rot,
            "pier": pier, "target": target}


def _night_records(start, n, pier, off, rot_rc=90.0, rot_pg=92.5, noise=0.3,
                   seed=1, ra_h=M31[0], dec=M31[1]):
    """n RC16 subs (600 s) each covered by two Piggy subs (mid +-150 s), the
    RC16 dithering a few arcsec around the target, the Piggy the boresight
    offset away (+ noise arcmin)."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        t = start + timedelta(minutes=10 * i)
        ra0, dec0 = po.tangent_inverse(ra_h * 15.0, dec, rnd.uniform(-0.2, 0.2),
                                       rnd.uniform(-0.2, 0.2))
        out.append(_rec("rc16", t, ra0, dec0, pier, rot=rot_rc))
        for dt in (-150, 150):
            e = off[0] + rnd.gauss(0, noise)
            nn = off[1] + rnd.gauss(0, noise)
            ra, de = po.tangent_inverse(ra0, dec0, e, nn)
            out.append(_rec("piggyback", t + timedelta(seconds=dt), ra, de,
                            None, rot=rot_pg))
    return out


def _write_night(cfg, night, recs):
    p = cfg.data_dir / "runs" / f"{night}_pointing.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _store(west=WEST_OFF, east=EAST_OFF, n=12):
    def side(o, inferred=False):
        return {"east": o[0], "north": o[1], "total": round(math.hypot(*o), 2),
                "sigma_east": 0.3, "sigma_north": 0.3, "rot": 2.5,
                "n_pairs": 0 if inferred else n, "n_nights": 0 if inferred else 2,
                "nights": [], "inferred": inferred}
    return {"n_pairs": 2 * n, "piers": {
        "West": side(west) if west else None,
        "East": side(east) if east else None}}


# --- geometry -----------------------------------------------------------------

@pytest.mark.parametrize("dec", [0.0, 41.3, 75.0, -30.0])
def test_tangent_roundtrip(dec):
    ra0 = 10.68
    for e, n in ((0.0, 0.0), (21.0, -13.0), (-45.0, 30.0)):
        ra, d = po.tangent_inverse(ra0, dec, e, n)
        e2, n2 = po.tangent_offset(ra0, dec, ra, d)
        assert abs(e2 - e) < 1e-6 and abs(n2 - n) < 1e-6


# --- pairs and stats ------------------------------------------------------------

def test_pairs_recover_offset_and_rotation():
    t0 = datetime(2026, 10, 4, 3, 0, 0)
    pairs = po.pairs_from_records(_night_records(t0, 10, "West", WEST_OFF), "2026-10-03")
    assert len(pairs) == 20
    assert all(p["pier"] == "West" for p in pairs)
    st = po.side_stats(pairs)
    assert abs(st["east"] - WEST_OFF[0]) < 0.3
    assert abs(st["north"] - WEST_OFF[1]) < 0.3
    assert abs(st["rot"] - 2.5) < 1e-6
    assert st["n_pairs"] == 20 and st["n_nights"] == 1


def test_pairing_rules_time_target_and_unsolved():
    t0 = datetime(2026, 10, 4, 3, 0, 0)
    rc = _rec("rc16", t0, 10.68, 41.27, "West")
    far = _rec("piggyback", t0 + timedelta(seconds=po.PAIR_MAX_DT_S + 60),
               10.9, 41.0, None)
    other = _rec("piggyback", t0 + timedelta(seconds=30), 10.9, 41.0, None,
                 target="Crescent Nebula")
    unsolved = {**_rec("piggyback", t0, 10.9, 41.0, None), "solved_ra": None}
    ok = _rec("piggyback", t0 + timedelta(seconds=60), 10.9, 41.0, None)
    pairs = po.pairs_from_records([rc, far, other, unsolved, ok], "n")
    assert [p["piggy_file"] for p in pairs] == [ok["file"]]


def test_outlier_pairs_are_clipped():
    t0 = datetime(2026, 10, 4, 3, 0, 0)
    recs = _night_records(t0, 10, "West", WEST_OFF)
    # a Piggy sub through a slew: 40' off
    ra, dec = po.tangent_inverse(recs[0]["solved_ra"], recs[0]["solved_dec"], 60.0, 20.0)
    recs.append(_rec("piggyback", t0 + timedelta(seconds=20), ra, dec, None))
    st = po.side_stats(po.pairs_from_records(recs, "n"))
    assert st["n_rejected"] == 1
    assert abs(st["east"] - WEST_OFF[0]) < 0.3


def test_missing_pier_side_is_inferred_by_negation():
    t0 = datetime(2026, 10, 4, 3, 0, 0)
    pairs = po.pairs_from_records(_night_records(t0, 5, "West", WEST_OFF), "n")
    piers = po.summarize(pairs, min_pairs=6)
    assert piers["East"]["inferred"] is True
    assert abs(piers["East"]["east"] + piers["West"]["east"]) < 1e-6
    assert abs(piers["East"]["north"] + piers["West"]["north"]) < 1e-6
    few = po.summarize(pairs[:3], min_pairs=6)
    assert few["East"] is None and few["West"]["n_pairs"] == 3


def test_measure_over_nights_save_and_load(tmp_path):
    cfg = _cfg(tmp_path)
    _write_night(cfg, "2026-10-02", _night_records(datetime(2026, 10, 3, 3), 6, "West",
                                                   WEST_OFF, seed=2))
    _write_night(cfg, "2026-10-03", _night_records(datetime(2026, 10, 4, 3), 6, "West",
                                                   (WEST_OFF[0] + 0.4, WEST_OFF[1]), seed=3)
                 + _night_records(datetime(2026, 10, 4, 8), 6, "East", EAST_OFF, seed=4))
    res = po.refresh(cfg)
    w, e = res["piers"]["West"], res["piers"]["East"]
    assert w["n_nights"] == 2 and w["n_pairs"] + w["n_rejected"] == 24
    assert abs(w["east"] - (WEST_OFF[0] + 0.2)) < 0.5
    assert w["night_sigma_east"] is not None
    assert not e["inferred"] and abs(e["north"] - EAST_OFF[1]) < 0.3
    assert po.load(cfg)["n_pairs"] == res["n_pairs"] == 36
    assert "pier West" in po.format_report(res)


def test_current_remeasures_a_missing_store(tmp_path):
    cfg = _cfg(tmp_path)
    assert po.current(cfg)["n_pairs"] == 0      # nothing measured, still a dict
    assert po.store_path(cfg).exists()


# --- center plan ------------------------------------------------------------------

def _landed(plan, pier, off):
    """Where the Piggy-600 center lands when the RC16 centers per the plan."""
    c = plan["by_pier"][pier]
    return po.tangent_inverse(c["ra_hours"] * 15.0, c["dec_degrees"], *off)


@pytest.mark.parametrize("pier,off", [("West", WEST_OFF), ("East", EAST_OFF)])
def test_center_plan_puts_target_mid_piggy_frame(tmp_path, pier, off):
    cfg = _cfg(tmp_path, piggy_center_mode="on")
    plan = po.center_plan(cfg, *M31, _store())
    assert plan["applied"]
    ra, dec = _landed(plan, pier, off)
    e, n = po.tangent_offset(M31[0] * 15.0, M31[1], ra, dec)
    assert math.hypot(e, n) < 0.01          # arcmin
    assert "RC16 frame sits off-center" in plan["note"]


def test_center_plan_frame_center_option(tmp_path):
    cfg = _cfg(tmp_path, piggy_center_mode="on")
    point = (0.70, 41.5)                    # between M31 and M110
    plan = po.center_plan(cfg, *M31, _store(), frame_center=point)
    ra, dec = _landed(plan, "West", WEST_OFF)
    e, n = po.tangent_offset(point[0] * 15.0, point[1], ra, dec)
    assert math.hypot(e, n) < 0.01
    assert "frame center" in plan["note"]


def test_center_plan_preview_off_missing_and_cap(tmp_path):
    prev = po.center_plan(_cfg(tmp_path), *M31, _store())
    assert not prev["applied"] and "PREVIEW" in prev["note"] and prev["by_pier"]
    off = po.center_plan(_cfg(tmp_path, piggy_center_mode="off"), *M31, _store())
    assert not off["applied"] and off["note"] == ""
    none = po.center_plan(_cfg(tmp_path, piggy_center_mode="on"), *M31, None)
    assert not none["applied"] and "no RC16-to-Piggy-600 offset measured yet" in none["note"]
    few = _store(n=3)
    assert not po.center_plan(_cfg(tmp_path, piggy_center_mode="on"), *M31, few)["applied"]
    big = po.center_plan(_cfg(tmp_path, piggy_center_mode="on",
                              piggy_center_max_shift_arcmin=10.0), *M31, _store())
    assert not big["applied"] and "max_shift" in big["note"]


# --- generator ----------------------------------------------------------------------

def _target(driving="piggyback", transit=datetime(2026, 10, 5, 6, 30)):
    return NinaSequenceTarget(
        name="M 31", ra_hours=M31[0], dec_degrees=M31[1], driving_rig=driving,
        transit_utc=transit,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600,
                                count=6, gain=200, offset=256)])


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _dsos(seq):
    return [d for d in _walk(seq) if "DeepSkyObjectContainer" in d.get("$type", "")]


def _radec(coords):
    ra = coords["RAHours"] + coords["RAMinutes"] / 60 + coords["RASeconds"] / 3600
    dec = (abs(coords["DecDegrees"]) + coords["DecMinutes"] / 60
           + coords["DecSeconds"] / 3600) * (-1 if coords["NegativeDec"] else 1)
    return ra, dec


def _gen(monkeypatch, targets, mode="on", store="default"):
    monkeypatch.setenv("PS_PIGGY_CENTER_MODE", mode)
    monkeypatch.setattr(nsj, "_piggy_store",
                        lambda cfg: _store() if store == "default" else store)
    seq = json.loads(nsj.generate_nina_json(build_sequence_for_night("T", targets)))
    return seq


def _centers(node):
    return [d for d in _walk(node) if "Platesolving.Center" in d.get("$type", "")]


def test_generator_shifts_center_for_piggy_driven_target(monkeypatch):
    seq = _gen(monkeypatch, [_target()])
    outer, inner = _dsos(seq)
    plan = po.center_plan(SimpleNamespace(piggy_center_mode="on", piggy_center_min_pairs=6,
                                          piggy_center_max_shift_arcmin=90.0),
                          *M31, _store())
    east, west = plan["by_pier"]["East"], plan["by_pier"]["West"]
    ora, odec = _radec(outer["Target"]["InputCoordinates"])
    assert abs(ora - east["ra_hours"]) < 1e-4 and abs(odec - east["dec_degrees"]) < 1e-3
    # the acquisition center (outer) and the pier-West center (nested)
    oc = [c for c in _centers(outer) if c not in _centers(inner)]
    assert len(oc) == 1 and abs(_radec(oc[0]["Coordinates"])[0] - east["ra_hours"]) < 1e-4
    assert inner["Name"].endswith(nsj.PIGGY_WEST_CENTER_SUFFIX)
    ira, idec = _radec(inner["Target"]["InputCoordinates"])
    assert abs(ira - west["ra_hours"]) < 1e-4 and abs(idec - west["dec_degrees"]) < 1e-3
    (ic,) = _centers(inner)
    assert abs(_radec(ic["Coordinates"])[1] - west["dec_degrees"]) < 1e-3
    conds = json.dumps(inner["Conditions"])
    assert "LoopCondition" in conds and "TimeCondition" in conds
    tc = next(c for c in inner["Conditions"]["$values"] if "TimeCondition" in c["$type"])
    from photonscript.shared.localtime import to_local
    tl = to_local(PhotonScriptConfig(), datetime(2026, 10, 5, 6, 30))
    assert (tc["Hours"], tc["Minutes"]) == (tl.hour, tl.minute)
    # the inner center comes right after the outer acquisition center
    items = outer["Items"]["$values"]
    i_center = next(i for i, it in enumerate(items) if "Platesolving.Center" in it["$type"])
    assert items[i_center + 1] is inner or items[i_center + 1]["Name"] == inner["Name"]
    # altitude still judged on the target
    alt = next(c for c in outer["Conditions"]["$values"] if "AltitudeCondition" in c["$type"])
    assert abs(_radec(alt["Data"]["Coordinates"])[0] - M31[0]) < 1e-3
    res = lint(seq)
    assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]
    pc = [f.detail for f in res.findings if f.rule == "piggy-center"]
    assert len(pc) == 1 and "E +21.0' N -13.0'" in pc[0]


def test_rc16_driven_target_unchanged(monkeypatch):
    seq = _gen(monkeypatch, [_target("rc16")])
    (dso,) = _dsos(seq)
    ra, dec = _radec(dso["Target"]["InputCoordinates"])
    assert abs(ra - M31[0]) < 1e-4 and abs(dec - M31[1]) < 1e-3
    assert "PS-26" not in json.dumps(seq)
    assert not [f for f in lint(seq).findings if f.rule == "piggy-center"]


def test_no_offset_measured_yet_falls_back_to_target(monkeypatch):
    seq = _gen(monkeypatch, [_target()], store=None)
    (dso,) = _dsos(seq)
    ra, dec = _radec(dso["Target"]["InputCoordinates"])
    assert abs(ra - M31[0]) < 1e-4 and abs(dec - M31[1]) < 1e-3
    (c,) = _centers(dso)
    assert abs(_radec(c["Coordinates"])[0] - M31[0]) < 1e-4
    res = lint(seq)
    assert res.ok
    (pc,) = [f.detail for f in res.findings if f.rule == "piggy-center"]
    assert "no RC16-to-Piggy-600 offset measured yet" in pc and "no shift" in pc


def test_preview_mode_annotates_without_moving(monkeypatch):
    seq = _gen(monkeypatch, [_target()], mode="preview")
    (dso,) = _dsos(seq)
    assert abs(_radec(dso["Target"]["InputCoordinates"])[0] - M31[0]) < 1e-4
    (pc,) = [f.detail for f in lint(seq).findings if f.rule == "piggy-center"]
    assert "PREVIEW" in pc


def test_no_transit_sets_only_the_flip_side(monkeypatch):
    seq = _gen(monkeypatch, [_target(transit=None)])
    (dso,) = _dsos(seq)
    assert "only the pier-East center" in json.dumps(dso)
    assert lint(seq).ok


def test_lint_rejects_a_looping_west_center(monkeypatch):
    seq = _gen(monkeypatch, [_target()])
    inner = _dsos(seq)[1]
    inner["Conditions"]["$values"] = [c for c in inner["Conditions"]["$values"]
                                      if "TimeCondition" not in c["$type"]]
    assert any(f.rule == "piggy-center" and f.level == "ERROR"
               for f in lint(seq).findings)


def test_container_suffix_maps_back_to_target():
    from photonscript.shared.target_names import strip_container_name
    assert strip_container_name("M 31" + nsj.PIGGY_WEST_CENTER_SUFFIX) == "M 31"


# --- planner, pointing check, API -------------------------------------------------------

def test_planner_passes_driving_rig_and_frame_center():
    from photonscript.scheduler.target_planner import plan_night_sequence
    from photonscript.shared.models import CelestialTarget, ImagingProject
    proj = ImagingProject(
        id="m31", target=CelestialTarget(name="M 31", catalog_id="M31",
                                         ra_hours=M31[0], dec_degrees=M31[1]),
        exposure_plans=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600,
                                     count=6, gain=200, offset=256)],
        driving_rig="piggyback", frame_center_ra_hours=0.70,
        frame_center_dec_degrees=41.5)
    cfg = PhotonScriptConfig(_env_file=None, moon_aware_planning=False)
    out = plan_night_sequence([proj], cfg, datetime(2026, 10, 5, 3, 0))
    assert out, "M 31 should be visible in October"
    (t,) = out
    assert t.driving_rig == "piggyback"
    assert (t.frame_center_ra_hours, t.frame_center_dec_degrees) == (0.70, 41.5)
    assert t.transit_utc is not None


def test_rc16_on_target_check_uses_shifted_center(tmp_path, monkeypatch):
    from photonscript.shared import pointing
    cfg = _cfg(tmp_path, piggy_center_mode="on")
    plan = po.center_plan(cfg, *M31, _store())
    w = plan["by_pier"]["West"]
    monkeypatch.setattr(pointing, "target_coords",
                        lambda c, n: ("M 31", M31[0] * 15.0, M31[1]))
    monkeypatch.setattr(po, "expected_rc16_center",
                        lambda c, n, pier: (w["ra_hours"] * 15.0, w["dec_degrees"])
                        if pier == "West" else None)
    rec = {"rig": "rc16", "solved_ra": w["ra_hours"] * 15.0,
           "solved_dec": w["dec_degrees"], "pier": "West"}
    out = pointing.assess(cfg, "rc16", rec, "M 31")
    assert out["off_target_arcmin"] < 0.1 and out["flag"] == ""
    assert "shifted" in out["note"]
    # preview (the default): judged against the target, the shift shows
    monkeypatch.setattr(po, "expected_rc16_center", lambda c, n, pier: None)
    out2 = pointing.assess(cfg, "rc16", rec, "M 31")
    assert out2["off_target_arcmin"] > 20


def test_expected_rc16_center_only_in_mode_on(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.shared.models import CelestialTarget, ImagingProject
    proj = ImagingProject(id="m31", target=CelestialTarget(
        name="M 31", catalog_id="M31", ra_hours=M31[0], dec_degrees=M31[1]),
        driving_rig="piggyback")
    monkeypatch.setattr(app, "_store", SimpleNamespace(projects={"m31": proj}))
    monkeypatch.setattr(po, "load", lambda cfg: _store())
    on = _cfg(tmp_path, piggy_center_mode="on")
    got = po.expected_rc16_center(on, "M 31", "West")
    plan = po.center_plan(on, *M31, _store())
    assert got == pytest.approx((plan["by_pier"]["West"]["ra_hours"] * 15.0,
                                 plan["by_pier"]["West"]["dec_degrees"]))
    assert po.expected_rc16_center(on, "M 31", None) is None
    assert po.expected_rc16_center(on, "Crescent Nebula", "West") is None
    assert po.expected_rc16_center(_cfg(tmp_path), "M 31", "West") is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    from photonscript.shared.models import CelestialTarget, ImagingProject
    cfg = _cfg(tmp_path, piggy_center_mode="on")
    monkeypatch.setattr(app, "_config", cfg)
    proj = ImagingProject(id="m31", target=CelestialTarget(
        name="M 31", catalog_id="M31", ra_hours=M31[0], dec_degrees=M31[1]),
        driving_rig="piggyback")
    saved = []
    monkeypatch.setattr(app, "_store", SimpleNamespace(
        projects={"m31": proj}, save=lambda: saved.append(1)))
    _write_night(cfg, "2026-10-03", _night_records(datetime(2026, 10, 4, 3), 8, "West",
                                                   WEST_OFF))
    c = TestClient(app.app)
    c.saved, c.proj = saved, proj
    return c


def test_api_measure_and_project_plan(client):
    d = client.get("/api/piggy-offset").json()
    assert d["ok"] and d["store"] is None and d["mode"] == "on"
    assert "no RC16-to-Piggy-600 offset" in d["projects"][0]["plan"]["note"]
    d = client.get("/api/piggy-offset?refresh=true").json()
    assert d["store"]["piers"]["West"]["n_pairs"] == 16
    assert d["store"]["piers"]["East"]["inferred"]
    (p,) = d["projects"]
    assert p["plan"]["applied"] and set(p["plan"]["by_pier"]) == {"East", "West"}
    assert client.get("/api/piggy-offset").json()["store"]["n_pairs"] == 16


def test_api_frame_center_set_clear_and_validate(client):
    r = client.put("/api/piggy-offset/frame-center/m31",
                   json={"ra_hours": 0.70, "dec_degrees": 41.5}).json()
    assert r["ok"] and client.proj.frame_center_ra_hours == 0.70 and client.saved
    assert client.put("/api/piggy-offset/frame-center/m31",
                      json={"ra_hours": 25, "dec_degrees": 0}).status_code == 400
    assert client.put("/api/piggy-offset/frame-center/nope", json={}).status_code == 404
    assert client.put("/api/piggy-offset/frame-center/m31", json={}).json()["ok"]
    assert client.proj.frame_center_ra_hours is None


def test_guiding_page_has_the_panel():
    from pathlib import Path
    root = Path(nsj.__file__).resolve().parent
    html = (root / "templates" / "guiding.html").read_text(encoding="utf-8")
    js = (root / "static" / "js" / "piggy_offset_panel.js").read_text(encoding="utf-8")
    for el in ("poInfo", "poRefresh", "poStatus"):
        assert f'id="{el}"' in html and f"'{el}'" in js
    assert "piggy_offset_panel.js" in html and "PIGGYOFFSET.init()" in html
    assert all(ord(ch) < 128 for ch in js)
