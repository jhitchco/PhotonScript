"""2026-11-23: the runs page night title folds test steps and long lists.

A TPoint night put all 100 "TPoint point 001/100 alt .. az .." names in the
title (4,792 characters on a phone)."""
from pathlib import Path

HTML = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
        / "templates" / "runs.html").read_text(encoding="utf-8")


def test_title_uses_the_folding_helper():
    assert "function titleTargets(names)" in HTML
    assert "titleTargets(names)" in HTML.split("nightTitle').innerHTML =", 1)[1][:200]
    assert "names.join(', ')" not in HTML


def test_helper_folds_tpoint_optics_and_tracking_steps():
    body = HTML.split("function titleTargets(names)", 1)[1].split("\n    }\n", 1)[0]
    assert "TPoint point (\d+)" in body and "'TPoint mapping'" in body
    assert "Optics test" in body and "Tracking test" in body
    assert "<details" in body and "more</summary>" in body
    assert "escT(k)" in body   # names are escaped
