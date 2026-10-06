"""PS-148: through-focus optics test (astigmatism / collimation check).

On 2026-10-05 the RC16 Ha subs showed a fixed elongation axis (~33 deg in
image coordinates) in every zone, magnified by ~310 steps of defocus, and
weakly in in-focus L too; the PS-95 optics report has the upper-left corner
~28% softer than the lower-right. Star shapes at one focus position cannot
tell astigmatism from a mechanical stretch. Through focus they can:

* astigmatism (collimation, a tilted / decentered secondary, a pinched
  mirror) stretches defocused stars one way inside focus and the
  perpendicular way outside focus: the axis flips by ~90 deg;
* tracking, wind, flexure or a loose mechanical part stretch every star the
  same way at every focus position: the axis stays put;
* stars round at best focus that only grow when defocused: defocus only,
  the optics are fine;
* sensor tilt shows up as a soft side that swaps across focus (the focal
  plane crosses the sensor at an angle).

Two halves, like the PS-84 tracking test:

* the sequence: ``generate_sequence`` (nina_sequence_json
  .generate_optics_test_json with the config's offsets / filters / lengths)
  on a field from ``pick_field`` (tracking_test.pick_target: 50 to 70 deg up
  near the meridian). Every step's subs carry OBJECT
  "Optics test <field> <filter> <offset>".
* ``build_report``: the night's optics-test subs grouped by (filter,
  offset) with the median eccentricity and HFR of the bright stars, the
  elongation axis (overall and per 3x3 zone, from the PS-80 sidecars), and
  a verdict per filter.

Orientation: zones and axes are in the runs-page thumbnail view (row 0 =
top, angles from +x, y down), as in optics_report.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)

BRIGHT_N = 100          # sidecar stars are flux-sorted: the first N are bright
AXIS_FLOOR = 0.30       # sqrt-form ecc a star needs to vote on the axis
AXIS_MIN_STARS = 10
AXIS_R_MIN = 0.35       # axial resultant: a common stretch direction
FLIP_MIN_DEG = 60.0     # inside vs outside axis this far apart = a flip
SAME_MAX_DEG = 25.0     # every axis within this of the mean = constant
ROUND_ECC = 0.45        # median bright-star ecc under this = round (b/a ~0.9)
TILT_GRAD_MIN = 0.08    # soft-side gradient (fraction of center FWHM)

ASTIGMATISM = "astigmatism"
CONSTANT = "constant-axis"
DEFOCUS_ONLY = "defocus-only"
UNCLEAR = "unclear"
NO_DATA = "no-data"

_NAME_RE = re.compile(r"^optics test (.+) (\S+) ([+-]?\d+)$", re.IGNORECASE)
_LEFT, _RIGHT = ("TL", "ML", "BL"), ("TR", "MR", "BR")
_TOP, _BOTTOM = ("TL", "TC", "TR"), ("BL", "BC", "BR")
_CORNERS = ("TL", "TR", "BL", "BR")


# ------------------------------------------------------------- helpers

def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _axial_diff(a, b) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _ints(text: Any) -> list[int]:
    out = []
    for tok in str(text or "").replace(";", ",").split(","):
        try:
            out.append(int(round(float(tok))))
        except ValueError:
            continue
    return out


# ------------------------------------------------------------- sequence

def params(config) -> dict:
    """The test's settings from the config (PS_OPTICS_TEST_*)."""
    from photonscript.scheduler.nina_sequence_json import (
        OPTICS_TEST_EXPOSURE_S, OPTICS_TEST_NB_EXPOSURE_S,
        _optics_test_offsets)
    offs = _optics_test_offsets(_ints(getattr(config, "optics_test_offsets",
                                              "")) or None)
    filters = [f.strip() for f in str(getattr(config, "optics_test_filters",
                                              "L") or "L").split(",")
               if f.strip()] or ["L"]
    return {
        "offsets": offs, "filters": filters,
        "exposure_s": float(getattr(config, "optics_test_exposure_s",
                                    OPTICS_TEST_EXPOSURE_S)
                            or OPTICS_TEST_EXPOSURE_S),
        "nb_exposure_s": float(getattr(config, "optics_test_nb_exposure_s",
                                       OPTICS_TEST_NB_EXPOSURE_S)
                               or OPTICS_TEST_NB_EXPOSURE_S),
        "repeats": max(1, min(int(getattr(config, "optics_test_repeats", 2)
                                  or 1), 10)),
    }


def duration_s(config) -> float:
    from photonscript.scheduler.nina_sequence_json import optics_test_duration_s
    p = params(config)
    return optics_test_duration_s(p["filters"], p["offsets"], p["exposure_s"],
                                  p["nb_exposure_s"], p["repeats"])


def parse_at(at: str) -> datetime | None:
    if not at:
        return None
    try:
        return datetime.fromisoformat(at.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


def pick_field(config, when_utc: datetime | None = None,
               projects: Iterable[Any] | None = None) -> dict:
    """The PS-84 field picker (50 to 70 deg up, near the meridian and not
    crossing it during the test) for this test's length."""
    from photonscript.scheduler.tracking_test import pick_target
    return pick_target(config, when_utc, duration_s(config), projects)


def generate_sequence(config, field: dict) -> str:
    """The standalone night sequence for `field` with the config's offsets,
    filters, sub lengths and repeats."""
    from photonscript.scheduler.nina_sequence_json import (
        generate_optics_test_json)
    p = params(config)
    return generate_optics_test_json(
        field["name"], field["ra_hours"], field["dec_degrees"],
        filters=p["filters"], offsets=p["offsets"],
        exposure_s=p["exposure_s"], nb_exposure_s=p["nb_exposure_s"],
        repeats=p["repeats"])


# ------------------------------------------------------------- names

def parse_name(name: Any) -> dict | None:
    """'Optics test M 2 L -300' -> {"field": "M 2", "filter": "L",
    "offset": -300}; a container name with its suffix works too. None for
    anything else."""
    from photonscript.shared.target_names import strip_container_name
    m = _NAME_RE.match(strip_container_name(name))
    if not m:
        return None
    return {"field": m.group(1), "filter": m.group(2),
            "offset": int(m.group(3))}


def is_optics_test(name: Any) -> bool:
    from photonscript.shared.target_names import target_key
    return target_key(name).startswith("opticstest")


# ------------------------------------------------------------- per sub

def _gradient(zones: dict) -> tuple | None:
    """(gx, gy): right minus left and bottom minus top zone FWHM, as a
    fraction of the center's. Positive gx = right side softer."""
    c = (zones.get("C") or {}).get("fwhm")
    if not c:
        return None

    def side(names):
        v = [(zones.get(n) or {}).get("fwhm") for n in names]
        v = [x for x in v if x is not None]
        return sum(v) / len(v) if v else None
    lft, rgt, top, bot = side(_LEFT), side(_RIGHT), side(_TOP), side(_BOTTOM)
    if None in (lft, rgt, top, bot):
        return None
    return (rgt - lft) / c, (bot - top) / c


def measure_sub(config, date: str, rec: dict, scale: float,
                min_zone: int = 8) -> dict:
    """One sub: median ecc and HFR of the bright stars, their stretch axis,
    and the 3x3 zone map (optics_report.sub_field_map). Falls back to the
    record's own ecc / HFR without a sidecar."""
    from photonscript.scheduler.optics_report import axial_stats, sub_field_map
    from photonscript.scheduler.tracking_test import _sidecar_ecc_sqrt
    from photonscript.shared import star_table
    tbl = None
    try:
        tbl = star_table.read(config, date, rec.get("file") or "",
                              rec.get("rig") or "rc16")
    except Exception:  # noqa: BLE001
        tbl = None
    out = {"file": rec.get("file"), "time": rec.get("time"),
           "source": "stars" if tbl else "record"}
    if not tbl:
        out.update({"ecc": _num(rec.get("ecc")), "hfr": _num(rec.get("hfr")),
                    "axis_deg": None, "R": None, "zones": None,
                    "gradient": None, "n_bright": 0})
        return out
    ecc = _sidecar_ecc_sqrt(tbl, rec)[:BRIGHT_N]
    hfr = [_num(h) for h in (tbl.get("hfr") or [])][:BRIGHT_N]
    theta = list(tbl.get("theta") or [])[:BRIGHT_N]
    ax = axial_stats(theta, ecc, floor=AXIS_FLOOR, min_n=AXIS_MIN_STARS)
    fm = sub_field_map(tbl, scale, min_zone)
    zones = fm.get("zones") if fm else None
    out.update({
        "ecc": _r(_median(ecc), 3), "hfr": _r(_median(hfr), 2),
        "axis_deg": ax.get("axis_deg"), "R": ax.get("R"),
        "n_bright": len([e for e in ecc if e is not None]),
        "zones": zones,
        "gradient": _gradient(zones) if zones else None,
        "corner_fwhm": {k: (zones.get(k) or {}).get("fwhm") for k in _CORNERS}
        if zones else None,
    })
    return out


# ------------------------------------------------------------- per step

def _step(flt: str, offset: int, subs: list[dict]) -> dict:
    from photonscript.scheduler.optics_report import ZONES, axial_mean
    zones = {}
    for row in ZONES:
        for z in row:
            zz = [(s.get("zones") or {}).get(z) or {} for s in subs]
            zones[z] = {
                "axis_deg": axial_mean([q.get("axis_deg") for q in zz]),
                "R": _r(_median([q.get("R") for q in zz]), 2),
                "ecc": _r(_median([q.get("ecc") for q in zz]), 3),
                "fwhm": _r(_median([q.get("fwhm") for q in zz]), 2),
            }
    grads = [s["gradient"] for s in subs if s.get("gradient")]
    grad = ((_median([g[0] for g in grads]), _median([g[1] for g in grads]))
            if grads else None)
    cf = {k: zones[k]["fwhm"] for k in _CORNERS
          if zones[k]["fwhm"] is not None}
    ratio = soft = sharp = None
    if len(cf) >= 2:
        soft = max(cf, key=cf.get)
        sharp = min(cf, key=cf.get)
        ratio = round(cf[soft] / cf[sharp], 3) if cf[sharp] else None
    return {
        "filter": flt, "offset": offset, "n": len(subs),
        "ecc_median": _r(_median([s.get("ecc") for s in subs]), 3),
        "hfr_median": _r(_median([s.get("hfr") for s in subs]), 2),
        "axis_deg": axial_mean([s.get("axis_deg") for s in subs]),
        "R": _r(_median([s.get("R") for s in subs]), 2),
        "zones": zones,
        "gradient": None if grad is None else [round(grad[0], 3),
                                               round(grad[1], 3)],
        "corner_ratio": ratio, "soft_corner": soft, "sharp_corner": sharp,
        "files": [s.get("file") for s in subs],
    }


def _coherent(s: dict) -> bool:
    return s.get("axis_deg") is not None and (s.get("R") or 0) >= AXIS_R_MIN


def _zone_flips(inside: dict, outside: dict) -> list[str]:
    out = []
    for z, qi in (inside.get("zones") or {}).items():
        qo = (outside.get("zones") or {}).get(z) or {}
        if (qi.get("axis_deg") is not None and qo.get("axis_deg") is not None
                and (qi.get("R") or 0) >= AXIS_R_MIN
                and (qo.get("R") or 0) >= AXIS_R_MIN
                and _axial_diff(qi["axis_deg"], qo["axis_deg"]) >= FLIP_MIN_DEG):
            out.append(z)
    return out


def _tilt(steps: list[dict], best: dict | None, tilt_warn: float) -> dict:
    """Tilt from the soft side across focus: a tilted focal plane makes one
    side soft inside focus and the opposite side soft outside focus."""
    from photonscript.scheduler.optics_report import CORNER_WORDS
    ins = [s for s in steps if s["offset"] < 0 and s.get("gradient")]
    outs = [s for s in steps if s["offset"] > 0 and s.get("gradient")]
    res = {"verdict": "unknown", "detail": None,
           "best_corner_ratio": best.get("corner_ratio") if best else None,
           "best_soft_corner": best.get("soft_corner") if best else None,
           "best_sharp_corner": best.get("sharp_corner") if best else None}
    soft_at_best = None
    if best and best.get("corner_ratio"):
        soft_at_best = (f"at best focus the {CORNER_WORDS[best['soft_corner']]} "
                        f"corner is {(best['corner_ratio'] - 1) * 100:.0f}% "
                        f"softer than the {CORNER_WORDS[best['sharp_corner']]}")
    if not ins or not outs:
        res["detail"] = soft_at_best
        return res
    gi = min(ins, key=lambda s: s["offset"])["gradient"]
    go = max(outs, key=lambda s: s["offset"])["gradient"]
    mi, mo = math.hypot(*gi), math.hypot(*go)
    dot = gi[0] * go[0] + gi[1] * go[1]
    if mi >= TILT_GRAD_MIN and mo >= TILT_GRAD_MIN and dot < 0 \
            and dot / (mi * mo) <= -0.5:
        from photonscript.scheduler.optics_report import sector
        res["verdict"] = "tilt"
        res["detail"] = ("the soft side swaps across focus (inside: "
                         f"{sector(math.degrees(math.atan2(gi[1], gi[0])))}, "
                         "outside: "
                         f"{sector(math.degrees(math.atan2(go[1], go[0])))}): "
                         "the focal plane is tilted against the sensor. Check "
                         "the camera tilt plate / spacer"
                         + (f"; {soft_at_best}" if soft_at_best else ""))
    elif best and (best.get("corner_ratio") or 0) >= tilt_warn:
        res["verdict"] = "soft-corner"
        res["detail"] = (soft_at_best + ", but the soft side does not swap "
                         "across focus: not a plain sensor tilt (collimation "
                         "or an off-axis aberration)")
    else:
        res["verdict"] = "none"
        res["detail"] = ("no soft side that swaps across focus"
                         + (f"; {soft_at_best}" if soft_at_best else ""))
    return res


def filter_verdict(steps: list[dict], tilt_warn: float = 1.20) -> dict:
    """Verdict for one filter's steps (sorted by offset)."""
    if not steps:
        return {"verdict": NO_DATA, "headline": "no optics-test subs",
                "detail": []}
    best = min(steps, key=lambda s: abs(s["offset"]))   # 0 when shot
    ins = [s for s in steps if s["offset"] < 0 and _coherent(s)]
    outs = [s for s in steps if s["offset"] > 0 and _coherent(s)]
    coh = [s for s in steps if _coherent(s)]
    far_in = min(ins, key=lambda s: s["offset"]) if ins else None
    far_out = max(outs, key=lambda s: s["offset"]) if outs else None
    flip = (_axial_diff(far_in["axis_deg"], far_out["axis_deg"])
            if far_in and far_out else None)
    best_ecc = best.get("ecc_median")
    best_round = best_ecc is not None and best_ecc < ROUND_ECC
    detail = []
    from photonscript.scheduler.optics_report import axial_mean
    mean_ax = axial_mean([s["axis_deg"] for s in coh])
    spread = (max(_axial_diff(s["axis_deg"], mean_ax) for s in coh)
              if coh and mean_ax is not None else None)
    defoc = [s for s in steps if s["offset"] != 0
             and s.get("ecc_median") is not None]
    if flip is not None and flip >= FLIP_MIN_DEG:
        verdict = ASTIGMATISM
        flips = _zone_flips(far_in, far_out)
        headline = (f"Astigmatism: the stretch axis flips {flip:.0f} deg "
                    f"across focus ({far_in['axis_deg']:g} deg at "
                    f"{far_in['offset']:+d} vs {far_out['axis_deg']:g} deg at "
                    f"{far_out['offset']:+d} steps)")
        if "C" in flips:
            detail.append("The center flips too (" + ", ".join(flips)
                          + "): on-axis astigmatism, so collimation (secondary "
                          "tilted or decentered) or a pinched / stressed "
                          "mirror, not field astigmatism. Check the "
                          "collimation (defocused-star or Aberration "
                          "Inspector) before touching the camera tilt.")
        elif flips:
            detail.append("Only off-axis zones flip (" + ", ".join(flips)
                          + "), the center does not: field astigmatism, "
                          "normal for an RC without a flattener / corrector. "
                          "Collimation looks fine on axis.")
        else:
            detail.append("Zone axes are too noisy to say whether the center "
                          "flips; the whole-frame axis does.")
    elif len(coh) >= 2 and spread is not None and spread <= SAME_MAX_DEG \
            and (far_in and far_out or (_coherent(best)
                                        and (far_in or far_out))):
        verdict = CONSTANT
        headline = (f"Constant axis: stars stretched along {mean_ax:g} deg "
                    "at every focus position (within "
                    f"{spread:.0f} deg): not astigmatism. Tracking, wind, "
                    "flexure or a mechanical stretch (a loose or pinched "
                    "component)")
        far = max(defoc, key=lambda s: abs(s["offset"])) if defoc else None
        if far is not None and best_ecc is not None:
            if far["ecc_median"] < best_ecc:
                detail.append("The stretch shrinks relative to the bigger "
                              "defocused stars: a fixed-length smear "
                              "(tracking, wind, flexure). See the PS-84 "
                              "tracking test and the Guiding page.")
            else:
                detail.append("The stretch grows with defocus: something in "
                              "the light path deforms the beam the same way "
                              "at every focus (pinched mirror, a loose or "
                              "tilted part), not a smear.")
        if best_round:
            detail.append(f"At best focus the stars are round (ecc "
                          f"{best_ecc:g}): it only shows out of focus.")
    elif best_round and defoc:
        verdict = DEFOCUS_ONLY
        headline = (f"Defocus only: round at best focus (ecc {best_ecc:g}) "
                    "and no stretch direction that survives defocus. The "
                    "optics look fine")
    elif best_ecc is not None and len(steps) > 1:
        verdict = UNCLEAR
        headline = (f"Unclear: elongated at best focus (ecc {best_ecc:g}) "
                    "with no common direction that flips or holds across "
                    "focus. Rerun on a steadier night, or check collimation "
                    "with a defocused star")
    else:
        verdict = UNCLEAR if best_ecc is not None else NO_DATA
        headline = ("Need the defocused steps to judge" if best_ecc is not None
                    else "No measurable stars in the optics-test subs")
    tilt = _tilt(steps, best if best["offset"] == 0 else None, tilt_warn)
    if tilt.get("detail"):
        detail.append("Tilt: " + tilt["detail"] + ".")
    return {"verdict": verdict, "headline": headline, "detail": detail,
            "flip_deg": None if flip is None else round(flip, 1),
            "axis_inside": far_in and far_in["axis_deg"],
            "axis_outside": far_out and far_out["axis_deg"],
            "axis_spread_deg": None if spread is None else round(spread, 1),
            "best_ecc": best_ecc, "best_offset": best["offset"], "tilt": tilt}


# ------------------------------------------------------------- per night

def default_night(config) -> str:
    from photonscript.scheduler.tracking_test import default_night as dn
    return dn(config)


def build_report(config, date: str | None = None,
                 records: list | None = None) -> dict:
    """The optics-test report for one night (runs-page date)."""
    from photonscript.shared.rigs import rig_config
    date = date or default_night(config)
    if records is None:
        from photonscript.scheduler.runs import _load_subs
        records = _load_subs(config, date)
    subs = [r for r in records if (r.get("rig") or "rc16") == "rc16"
            and is_optics_test(r.get("target"))]
    scale = float(getattr(rig_config(config, "rc16"), "pixel_scale_arcsec",
                          0.236) or 0.236)
    min_zone = int(getattr(config, "optics_min_stars_zone", 8) or 8)
    tilt_warn = float(getattr(config, "optics_tilt_warn", 1.20) or 1.20)
    groups: dict[tuple, list] = {}
    unnamed = 0
    fields = set()
    for r in subs:
        p = parse_name(r.get("target"))
        if p is None:
            unnamed += 1
            continue
        fields.add(p["field"])
        flt = str(r.get("filter") or p["filter"])
        groups.setdefault((flt, p["offset"]), []).append(
            measure_sub(config, date, r, scale, min_zone))
    order = {f: i for i, f in enumerate(("L", "R", "G", "B", "Ha", "OIII",
                                         "SII"))}
    by_f: dict[str, list] = {}
    for (flt, off), ms in sorted(groups.items(),
                                 key=lambda kv: (order.get(kv[0][0], 99),
                                                 kv[0][0], kv[0][1])):
        by_f.setdefault(flt, []).append(_step(flt, off, ms))
    filters = []
    for flt, steps in by_f.items():
        v = filter_verdict(steps, tilt_warn)
        filters.append({"filter": flt, **v, "steps": steps})
    if filters:
        main = filters[0]
        headline = f"{main['filter']}: {main['headline']}."
        others = [f"{f['filter']}: {f['verdict']}" for f in filters[1:]]
        if others:
            headline += " Also " + "; ".join(others) + "."
    else:
        headline = (f"No subs named 'Optics test ...' in the {date} runs log. "
                    "Sideload the optics_through_focus recipe (or download "
                    "/api/optics-test/sequence), Start it in NINA, then pass "
                    "?date= for the night it ran.")
    return {
        "date": date, "n_subs": len(subs), "n_unparsed": unnamed,
        "fields": sorted(fields), "headline": headline, "filters": filters,
        "rules": {
            "flip_min_deg": FLIP_MIN_DEG, "same_max_deg": SAME_MAX_DEG,
            "round_ecc": ROUND_ECC, "axis_r_min": AXIS_R_MIN,
            "bright_stars": BRIGHT_N, "tilt_warn": tilt_warn,
            "text": ("axis flips ~90 deg across focus = astigmatism "
                     "(collimation / tilted secondary if the center flips, "
                     "field astigmatism if only the corners do); the same "
                     "axis at every offset = tracking / wind / flexure / "
                     "mechanical; round at best focus with no surviving "
                     "direction = defocus only; a soft side that swaps "
                     "across focus = sensor tilt")},
        "orientation": "angles from +x in the runs-page thumbnail view "
                       "(row 0 is the top of the image, y down)",
    }


def format_report(rep: dict) -> str:
    """Plain-text rendering for the CLI."""
    lines = [f"Optics test {rep['date']}: {rep['n_subs']} subs"
             + (f" ({', '.join(rep['fields'])})" if rep["fields"] else "")]
    for f in rep["filters"]:
        lines.append(f"{f['filter']}: {f['verdict']}")
        lines.append(f"  {'offset':>6s} {'n':>2s} {'ecc':>5s} {'HFR':>5s} "
                     f"{'axis':>5s} {'R':>4s} {'TL/BR':>6s}  zone axes "
                     "(TL TC TR / ML C MR / BL BC BR)")
        for s in f["steps"]:
            def g(v, fmt):
                return format(v, fmt) if v is not None else "-"
            z = s.get("zones") or {}
            za = " ".join(g((z.get(n) or {}).get("axis_deg"), ".0f")
                          for n in ("TL", "TC", "TR", "ML", "C", "MR",
                                    "BL", "BC", "BR"))
            tl, br = ((z.get("TL") or {}).get("fwhm"),
                      (z.get("BR") or {}).get("fwhm"))
            lines.append(f"  {s['offset']:+6d} {s['n']:2d} "
                         f"{g(s['ecc_median'], '5.3f')} "
                         f"{g(s['hfr_median'], '5.2f')} "
                         f"{g(s['axis_deg'], '5.1f')} {g(s['R'], '4.2f')} "
                         f"{g(tl / br if tl and br else None, '6.2f')}  {za}")
        lines.append("  " + f["headline"])
        for d in f.get("detail", []):
            lines.append("  - " + d)
    lines.append("Verdict: " + rep["headline"])
    return "\n".join(lines)
