"""PS-22: AstroBin acquisition CSV + packet MD draft for a stacked run.

Follows the astrobin-publish skill conventions: AstroBin's CSV import
header (date,number,duration,binning,gain,sensorCooling,fNumber,darks,flats,
flatDarks,bias,bortle,temperature), one row per night and filter and sub
length; `date` is the local date of the night's first integrated sub (the
M31 packets: 09-21 for a night that began after midnight, 10-03 for one
that began at 20:01); `number` counts frames actually integrated;
`temperature` is the median FOCTEMP per night, a focuser-probe proxy, and
the packet says so. The filter column needs AstroBin's numeric filter id,
so it is left out; the packet lists the filter names for the upload step.

The packet is a DRAFT for Jeremy: nothing here uploads or publishes. All
text is ASCII with no em or en dashes (no_em_dash() guards it).
"""

from __future__ import annotations

import csv
import io
from collections import OrderedDict
from statistics import median

from photonscript.integration.frames import Frame

CSV_HEADER = ["date", "number", "duration", "binning", "gain", "sensorCooling", "fNumber",
              "darks", "flats", "flatDarks", "bias", "bortle", "temperature"]

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)


def no_em_dash(text: str) -> str:
    """Raise if the text has an em or en dash (or any non-ASCII); return it."""
    if EM_DASH in text or EN_DASH in text:
        raise ValueError("em / en dash in packet text")
    bad = sorted({c for c in text if ord(c) > 127})
    if bad:
        raise ValueError("non-ASCII in packet text: " + " ".join(f"U+{ord(c):04X}" for c in bad))
    return text


def fmt_duration(seconds: float) -> str:
    """33840 -> '9 h 24 m'; under an hour -> '42 m'."""
    m = int(round(seconds / 60.0))
    h, m = divmod(m, 60)
    return f"{h} h {m} m" if h else f"{m} m"


def _num(v, nd=1):
    if v is None:
        return ""
    r = round(float(v), nd)
    return str(int(r)) if r == int(r) else str(r)


def acquisition_rows(frames: list[Frame], *, darks_for: dict, flats_for: dict, bias: int,
                     bortle: int = 2) -> list[dict]:
    """One row per (night, filter, sub length) of the INTEGRATED frames.
    darks_for: light exposure -> dark frames used for it; flats_for: filter
    -> flat frames used; bias: bias frames used."""
    groups: "OrderedDict[tuple, list[Frame]]" = OrderedDict()
    night_first: dict[str, str] = {}
    for f in sorted(frames, key=lambda x: (x.date_obs, x.name)):
        night_first.setdefault(f.night, (f.date_loc or f.date_obs)[:10])
        groups.setdefault((f.night, f.filter, round(f.exp, 2)), []).append(f)
    night_temp: dict[str, float | None] = {}
    for n in night_first:
        t = [f.foctemp for f in frames if f.night == n and f.foctemp is not None]
        night_temp[n] = round(median(t), 1) if t else None
    rows = []
    for (night, filt, exp), fs in groups.items():
        g = fs[0]
        cool = [f.set_temp if f.set_temp is not None else f.ccd_temp for f in fs]
        cool = [c for c in cool if c is not None]
        rows.append({
            "date": night_first[night] or night,
            "number": len(fs),
            "duration": _num(exp, 2),
            "binning": g.xbin or 1,
            "gain": "" if g.gain is None else g.gain,
            "sensorCooling": "" if not cool else int(round(median(cool))),
            "fNumber": _num(g.focratio, 1),
            "darks": darks_for.get(exp, 0),
            "flats": flats_for.get(filt, 0),
            "flatDarks": 0,
            "bias": bias,
            "bortle": bortle,
            "temperature": "" if night_temp[night] is None else _num(night_temp[night], 1),
            "_filter": filt, "_night": night,
        })
    return rows


def csv_text(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for r in rows:
        w.writerow([r[k] for k in CSV_HEADER])
    return no_em_dash(buf.getvalue())


def _table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def _deg(v, pos: str, neg: str) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"{abs(x):.3f} {pos if x >= 0 else neg}"


def packet_md(info: dict) -> str:
    """The packet draft. `info` keys (plain data, built by pipeline.run):
    target, run_name, run_dir, built, rows (acquisition_rows), integrated,
    total_s, staged, qa {mode, kept, rejected, reasons [(reason, n)]},
    calibration {bias, darks [(light exp, n, dark exp, scaled)], flats
    [(filter, n, note)], notes}, equipment {camera, optics, mount,
    software}, location {name, lat, lon, elev, bortle}, timing [(stage,
    minutes)], integrated_by_pi, known_issues [str], final, processing."""
    t = info["target"]
    rows = info["rows"]
    L: list[str] = []
    L.append(f"# {t} AstroBin packet draft ({info['run_name']})")
    L.append("")
    L.append(f"Built {info['built']} by `photonscript integrate` (PS-22) from `{info['run_dir']}`. "
             "Draft for Jeremy: nothing is uploaded or published by the pipeline.")
    L.append("")
    L.append("## Status")
    L.append("")
    L += _table(["Item", "State"], [
        ["Acquisition CSV", "Done (this run)"],
        ["Final image", info.get("final") or "Not made (finish not run)"],
        ["Integration", "PixInsight run finished" if info.get("integrated_by_pi")
         else "PLANNED only: the counts below are the subs selected for stacking, not yet integrated"],
        ["AstroBin upload", "Not uploaded. Staging area only; Jeremy presses Publish"]])
    L.append("")
    L.append("## Title")
    L.append("")
    L.append(f"**{t} (WIP)**")
    L.append("")
    L.append("Keep (WIP) until the stack has matched darks and flats and Jeremy calls it final.")
    L.append("")
    nights = sorted({r["date"] for r in rows})
    subs = ", ".join(f"{r['number']} x {r['duration']} s ({r['_filter']}, {r['date']})" for r in rows)
    cal = info["calibration"]
    cal_bits = []
    if cal["bias"]:
        cal_bits.append(f"{cal['bias']} bias frames")
    for exp, n, dexp, scaled in cal["darks"]:
        if n:
            cal_bits.append(f"{n} x {dexp:g} s darks for the {exp:g} s subs"
                            + (" (scaled with dark optimization)" if scaled else ""))
    flat_n = sum(n for _, n, _ in cal["flats"])
    cal_bits.append(f"{flat_n} flats" if flat_n else "no flats yet")
    L.append("## Description (draft, paste)")
    L.append("")
    L.append(f"{info['integrated']} subs, {fmt_duration(info['total_s'])} in total over "
             f"{len(nights)} night{'s' if len(nights) != 1 else ''}: {subs}.")
    L.append("")
    qa = info["qa"]
    if qa.get("mode") != "off":
        L.append("Every sub was checked star by star before stacking (sharpness, elongation, star count, "
                 "doubled stars, and registration to a reference, including a check for a second set of "
                 f"star images). {qa['rejected']} of {qa['kept'] + qa['rejected']} were left out.")
        L.append("")
    L.append("Calibrated with " + ", ".join(cal_bits) + ".")
    L.append("")
    L.append("PixInsight processing: " + info.get("processing", ""))
    L.append("")
    L.append("## Acquisition (what the CSV contains)")
    L.append("")
    L.append("```")
    L.append(csv_text(rows).rstrip("\n"))
    L.append("```")
    L.append("")

    def _set(key, suffix=""):
        return " / ".join(sorted({str(r[key]) for r in rows})) + suffix

    L += _table(["Field", "Value", "Source"], [
        ["date", ", ".join(nights), "DATE-LOC of the first integrated sub of each night"],
        ["number", " / ".join(str(r["number"]) for r in rows) + f" = {info['integrated']}",
         "integrated frames (pipeline.log funnel)" if info.get("integrated_by_pi")
         else "selected frames (not yet integrated)"],
        ["Total integration", f"{int(info['total_s'])} s = {fmt_duration(info['total_s'])}", "sum of the rows"],
        ["duration", _set("duration", " s"), "FITS EXPTIME"],
        ["gain", _set("gain"), "FITS GAIN (offset and readout are not CSV fields)"],
        ["sensorCooling", _set("sensorCooling", " C"), "FITS SET-TEMP"],
        ["fNumber", _set("fNumber"), "FITS FOCRATIO"],
        ["darks / flats / bias", "per row: frames that went into the masters", "manifest.json"],
        ["bortle", _set("bortle"), "AARO site"],
        ["temperature", _set("temperature", " C"),
         "median FOCTEMP per night: a focuser probe, a proxy for ambient"],
        ["filter", _set("_filter"), "not in the CSV (AstroBin wants its numeric filter id); set by hand"],
    ])
    L.append("")
    L.append("## Sub selection")
    L.append("")
    if qa.get("mode") == "off":
        L.append("Star QA was off for this run (--qa off): every approved Library sub was staged.")
    else:
        L.append(f"Library subs approved by PhotonScript: {info['staged']}. Star QA ({qa.get('mode')}): "
                 f"{qa['kept']} kept, {qa['rejected']} rejected. Details: qa/star_qa.csv.")
        if qa.get("reasons"):
            L.append("")
            L += _table(["Reason", "Subs"], [[r, n] for r, n in qa["reasons"]])
    L.append("")
    L.append("## Calibration")
    L.append("")
    for n in cal["notes"]:
        L.append(f"- {n}")
    L.append("")
    L.append("## Equipment (from FITS headers; select in AstroBin)")
    L.append("")
    eq = info["equipment"]
    L += _table(["Role", "Value", "Source / confidence"], [
        ["Imaging camera", eq.get("camera", "?"), "FITS INSTRUME (high)"],
        ["Focal length / ratio", eq.get("optics", "?"), "FITS FOCALLEN / FOCRATIO (high)"],
        ["Mount driver", eq.get("mount", "?"), "FITS TELESCOP (medium: driver name, not the model)"],
        ["Capture software", eq.get("software", "?"), "FITS SWCREATE (high)"],
        ["Processing", "PixInsight, PhotonScript", "this pipeline"],
        ["Filter / guiding", "not recorded", "ask Jeremy; never invent"],
    ])
    L.append("")
    loc = info["location"]
    L.append("## Location")
    L.append("")
    L.append(f"{loc.get('name', 'AARO')}: {_deg(loc.get('lat'), 'N', 'S')}, {_deg(loc.get('lon'), 'E', 'W')}, "
             f"{loc.get('elev', '?')} m, Bortle {loc.get('bortle', '?')}. Elevation from the PhotonScript "
             "config, not from FITS SITEELEV (often 0 or driver-reported).")
    L.append("")
    if info.get("timing"):
        L.append("## Timing (this run)")
        L.append("")
        L += _table(["Stage", "Minutes"], [[s, m] for s, m in info["timing"]])
        L.append("")
    L.append("## Upload steps (only when Jeremy says go)")
    L.append("")
    L.append("1. New image or a revision of an existing one: ask Jeremy (default for more data on the same target: revision).")
    L.append("2. Upload the final image to the staging area; fill title, description, equipment.")
    L.append("3. Acquisition tab: Import > From CSV with the CSV beside this file; check the rows against the table above.")
    L.append("4. Stop in staging and report back. Never press Publish without Jeremy's yes.")
    L.append("")
    L.append("## Known issues")
    L.append("")
    for k in info.get("known_issues") or ["None recorded."]:
        L.append(f"- {k}")
    L.append("")
    return no_em_dash("\n".join(L))
