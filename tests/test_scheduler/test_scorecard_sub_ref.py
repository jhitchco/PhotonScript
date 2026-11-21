"""GET /api/runs/<night>/scorecard?file= takes every form GET /api/subs
hands out for a sub (2026-10-14: its key and abs_path answered "sub not
found"): the file, the key "date|rig|file", the abs_path, a backslash file
and a bare name; rig= (or the key's rig) picks between the two rigs."""
import pytest

from photonscript.shared.sub_file import find_sub, sub_ref
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed

ABS = r"C:\Users\jeremy\Documents\NINA-Piggyback" + "\\" + NIGHT + r"\LIGHT"


def test_sub_ref_forms():
    assert sub_ref(f"{NIGHT}|piggyback|LIGHT/a.fits") == (NIGHT, "piggyback", "LIGHT/a.fits")
    assert sub_ref(f"{NIGHT}|piggyback|LIGHT/a.fits", "rc16")[1] == "rc16"
    assert sub_ref("LIGHT\\a.fits") == (None, None, "LIGHT/a.fits")


def test_find_sub_prefers_exact_then_tail_then_unique_name():
    subs = [{"rig": "rc16", "file": "LIGHT/a.fits"},
            {"rig": "piggyback", "file": "LIGHT/a.fits", "abs_path": ABS + r"\a.fits"},
            {"rig": "piggyback", "file": "LIGHT/b.fits"}]
    assert find_sub(subs, "LIGHT/a.fits", "piggyback") is subs[1]
    assert find_sub(subs, f"{NIGHT}|rc16|LIGHT/a.fits") is subs[0]
    assert find_sub(subs, ABS + r"\b.fits") is subs[2]
    assert find_sub(subs, "b.fits") is subs[2]
    assert find_sub(subs, "a.fits") is None              # two rigs: ambiguous
    assert find_sub(subs, "a.fits", "rc16") is subs[0]
    assert find_sub(subs, "LIGHT/c.fits") is None and find_sub(subs, "") is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    monkeypatch.setattr(app, "_config", cfg)
    _seed(cfg, [_rec("rc_Ha_300s_0001"),
                _rec("2026-09-27_03-14-00__0.00_300.00s_0084", rig="piggyback",
                     filter="L", abs_path=ABS + r"\2026-09-27_03-14-00__0.00_300.00s_0084.fits"),
                _rec("old", file="LIGHT\\old_Ha_300s_0002.fits")])   # pre-PS-147
    return TestClient(app.app)


def test_every_form_api_subs_returns_finds_the_scorecard(client):
    rows = client.get("/api/subs", params={"night": NIGHT, "limit": 50}).json()["rows"]
    assert len(rows) == 3
    for row in rows:
        forms = [row["file"], row["key"], row["file"].replace("/", "\\"),
                 row["file"].rsplit("/", 1)[-1]]
        if row.get("abs_path"):
            forms.append(row["abs_path"])
        for f in forms:
            r = client.get(f"/api/runs/{row['date']}/scorecard", params={"file": f})
            assert r.status_code == 200, (f, r.text)
            out = r.json()
            assert out["file"] == row["file"].replace("\\", "/")
            assert out["rig"] == row["rig"]


def test_the_key_night_wins_and_unknown_is_still_404(client):
    rows = client.get("/api/subs", params={"night": NIGHT}).json()["rows"]
    r = client.get("/api/runs/2026-09-27/scorecard", params={"file": rows[0]["key"]})
    assert r.status_code == 200
    r = client.get(f"/api/runs/{NIGHT}/scorecard", params={"file": "LIGHT/none.fits"})
    assert r.status_code == 404 and r.json()["detail"] == "sub not found"


def test_api_subs_takes_date_as_night(client):
    """?date= was ignored (every night listed); it is an alias of night=."""
    from photonscript.scheduler.runs import append_sub_record
    import photonscript.scheduler.app as app
    append_sub_record(app._config, "2026-09-25", _rec("other_night"))
    every = client.get("/api/subs").json()["total"]
    assert every == 4
    assert client.get("/api/subs", params={"date": NIGHT}).json()["total"] == 3
    assert client.get("/api/subs", params={"night": NIGHT}).json()["total"] == 3
