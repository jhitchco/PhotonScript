"""PS-22: match bias, darks and flats to the lights being stacked.

Calibration frames come from the Library mirror's two trees,
Library/Calibration/<TYPE>/<date>/ (RC16) and
Library/piggyback/Calibration/<TYPE>/<date>/ (Piggy-600, PS-36), never from
_quarantine (PS-113 failed frames). The camera is told apart by INSTRUME.

The light epoch a dark or bias must share: instrument, gain, offset,
binning, sensor temperature (setpoint, else CCD-TEMP, within temp_tol) and
the readout mode (PS-128; a frame with no READOUTM is assumed to be at the
rig's readout, like calibration.count_matching_darks). Then:

* bias: newest sessions first, up to max_frames.
* darks per light exposure: the same length when at least min_darks exist
  (used as-is); else the dark length with the most frames (ties: the
  longest) used with dark scaling (PixInsight optimizeDarks, the M31_OSC4
  v4 recipe for 300 / 400 s lights on 120 s darks). Scaling needs a master
  bias (calibrateDark subtracts it first); without one there is no dark.
* PS-178: a light length with no usable dark gets `alternatives`, the
  nearest ways out in order (same-length darks that miss the epoch and why,
  too few same-length darks and the --min-darks that would take them, other
  lengths that could be scaled once a matching bias exists, the bias
  sessions that miss the epoch and why). The pipeline refuses to stack such
  a group unless --allow-uncalibrated.
* flats per filter: same instrument, filter and binning; sensor within
  flat_temp_tol of the lights (the 2026-09-21 Piggy-600 flats were shot
  uncooled at about 38 C, which is why v4b ran without flats); the session
  nearest in date to the lights' median night.

Read-only: frames are only listed and their headers read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import median

from photonscript.integration.frames import Frame, read_frame
from photonscript.shared.rigs import normalize_readout

_TYPE_DIRS = {"BIAS": "BIAS", "BIA": "BIAS", "DARK": "DARK", "DARKS": "DARK",
              "FLAT": "FLAT", "FLATS": "FLAT"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CAL_EXT = (".fits", ".fit", ".fts")


def calibration_roots(lib: Path) -> list[Path]:
    lib = Path(lib)
    return [p for p in (lib / "Calibration", lib / "piggyback" / "Calibration") if p.is_dir()]


def scan(lib: Path, read=read_frame, instrument: str = "") -> tuple[list[Frame], list[tuple[str, str]]]:
    """Every BIAS / DARK / FLAT frame under the calibration roots (not
    _quarantine). With `instrument`, a session whose first frame is another
    camera is skipped whole (one header read per session). Returns (frames,
    skipped)."""
    frames: list[Frame] = []
    skipped: list[tuple[str, str]] = []
    for root in calibration_roots(lib):
        for tdir in sorted(p for p in root.iterdir() if p.is_dir()):
            typ = _TYPE_DIRS.get(tdir.name.upper())
            if typ is None:
                continue        # _quarantine and anything else
            for sdir in sorted(p for p in tdir.iterdir() if p.is_dir() and _DATE_RE.match(p.name)):
                files = sorted(p for p in sdir.rglob("*") if p.suffix.lower() in CAL_EXT)
                if not files:
                    continue
                if instrument:
                    try:
                        first = read(files[0], kind=typ)
                    except Exception as e:  # noqa: BLE001
                        skipped.append((str(sdir), f"unreadable ({e})"))
                        continue
                    if first.instrument.upper() != instrument.upper():
                        skipped.append((str(sdir), f"camera {first.instrument or '?'}"))
                        continue
                for f in files:
                    try:
                        fr = read(f, kind=typ)
                    except Exception as e:  # noqa: BLE001
                        skipped.append((str(f), f"unreadable ({e})"))
                        continue
                    fr.kind = typ
                    fr.session = sdir.name
                    frames.append(fr)
    return frames, skipped


@dataclass
class Epoch:
    """What a dark / bias must share with the lights."""
    instrument: str
    gain: int | None
    offset: int | None
    xbin: int
    temp: float | None
    readout: str | None     # normalized; None = not matched

    @classmethod
    def of(cls, light: Frame, default_readout: str | None = None) -> "Epoch":
        return cls(light.instrument, light.gain, light.offset, light.xbin, light.temp,
                   light.readout or normalize_readout(default_readout))

    def label(self) -> str:
        t = "?" if self.temp is None else f"{self.temp:g} C"
        return (f"{self.instrument or '?'} gain {self.gain} offset {self.offset} bin {self.xbin} "
                f"{t} {self.readout or 'any readout'}")


def epoch_reason(cal: Frame, ep: Epoch, *, temp_tol: float = 1.5,
                 default_readout: str | None = None) -> str:
    """'' when `cal` matches the epoch, else why not."""
    if ep.instrument and cal.instrument.upper() != ep.instrument.upper():
        return f"camera {cal.instrument or '?'}"
    if cal.gain != ep.gain:
        return f"gain {cal.gain}"
    if cal.offset != ep.offset:
        return f"offset {cal.offset}"
    if (cal.xbin or 1) != (ep.xbin or 1):
        return f"bin {cal.xbin}"
    if ep.temp is not None:
        if cal.temp is None:
            return "no temperature"
        if abs(cal.temp - ep.temp) > temp_tol:
            return f"temperature {cal.temp:g} C"
    if ep.readout:
        ro = cal.readout or normalize_readout(default_readout) or ep.readout
        if ro != ep.readout:
            return f"readout {ro}"
    return ""


@dataclass
class DarkChoice:
    light_exp: float
    dark_exp: float | None = None
    frames: list[Frame] = field(default_factory=list)
    scaled: bool = False
    note: str = ""
    alternatives: list[str] = field(default_factory=list)   # PS-178


@dataclass
class FlatChoice:
    filter: str
    session: str = ""
    frames: list[Frame] = field(default_factory=list)
    note: str = ""


@dataclass
class CalibrationPlan:
    epoch: Epoch
    bias: list[Frame] = field(default_factory=list)
    darks: dict[float, DarkChoice] = field(default_factory=dict)
    flats: dict[str, FlatChoice] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    bias_misses: list[str] = field(default_factory=list)   # PS-178

    def uncalibrated(self) -> list[float]:
        """PS-178: light lengths with no dark at all."""
        return sorted(e for e, d in self.darks.items() if not d.frames)

    def dark_masters(self) -> list[float]:
        """Dark lengths that need a master (each once)."""
        return sorted({d.dark_exp for d in self.darks.values() if d.frames})

    def dark_frames(self, exp: float) -> list[Frame]:
        for d in self.darks.values():
            if d.dark_exp == exp and d.frames:
                return d.frames
        return []


def _newest_first(frames: list[Frame], max_frames: int) -> list[Frame]:
    by_session: dict[str, list[Frame]] = {}
    for f in frames:
        by_session.setdefault(f.session, []).append(f)
    out: list[Frame] = []
    for s in sorted(by_session, reverse=True):
        for f in sorted(by_session[s], key=lambda x: x.name):
            if len(out) >= max_frames:
                return out
            out.append(f)
    return out


def epoch_misses(cals: list[Frame], ep: Epoch, kind: str, *, exp: float | None = None,
                 temp_tol: float = 1.5, default_readout: str | None = None,
                 limit: int = 6) -> list[str]:
    """PS-178: sessions of `kind` (and length `exp`) that miss the epoch, as
    'YYYY-MM-DD (n x 300 s): readout LCG', newest first."""
    by: dict[tuple, list[str]] = {}
    for c in cals:
        if c.kind != kind:
            continue
        if exp is not None and abs(round(c.exp, 2) - exp) >= 0.5:
            continue
        why = epoch_reason(c, ep, temp_tol=temp_tol, default_readout=default_readout)
        if why:
            by.setdefault((c.session, round(c.exp, 2) if kind == "DARK" else None), []).append(why)
    out = []
    for (sess, e), whys in sorted(by.items(), key=lambda kv: kv[0][0], reverse=True)[:limit]:
        what = f"{len(whys)} x {e:g} s" if e is not None else f"{len(whys)} frames"
        common = max(set(whys), key=whys.count)
        out.append(f"{sess or '?'} ({what}): {common}")
    return out


def match_bias(cals: list[Frame], ep: Epoch, *, max_frames: int = 50, temp_tol: float = 1.5,
               default_readout: str | None = None) -> list[Frame]:
    ok = [c for c in cals if c.kind == "BIAS"
          and not epoch_reason(c, ep, temp_tol=temp_tol, default_readout=default_readout)]
    return _newest_first(ok, max_frames)


def match_darks(cals: list[Frame], ep: Epoch, exposures: list[float], *, have_bias: bool,
                min_darks: int = 10, max_frames: int = 50, temp_tol: float = 1.5,
                default_readout: str | None = None,
                bias_misses: list[str] | None = None) -> dict[float, DarkChoice]:
    ok = [c for c in cals if c.kind == "DARK"
          and not epoch_reason(c, ep, temp_tol=temp_tol, default_readout=default_readout)]
    by_exp: dict[float, list[Frame]] = {}
    for c in ok:
        by_exp.setdefault(round(c.exp, 2), []).append(c)
    usable = {e: v for e, v in by_exp.items() if len(v) >= min_darks}
    out: dict[float, DarkChoice] = {}
    for le in sorted({round(e, 2) for e in exposures}):
        ch = DarkChoice(light_exp=le)
        exact = [e for e in usable if abs(e - le) < 0.5]
        if exact:
            ch.dark_exp = exact[0]
            ch.frames = _newest_first(usable[exact[0]], max_frames)
            ch.note = f"{len(ch.frames)} x {exact[0]:g} s darks (matched length)"
        elif usable and have_bias:
            best = max(usable, key=lambda e: (len(usable[e]), e))
            ch.dark_exp = best
            ch.frames = _newest_first(usable[best], max_frames)
            ch.scaled = True
            ch.note = (f"no {le:g} s darks at this epoch: {len(ch.frames)} x {best:g} s darks "
                       f"scaled (optimizeDarks)")
        elif usable:
            ch.note = (f"no {le:g} s darks and no bias to scale "
                       f"{', '.join(f'{e:g} s' for e in sorted(usable))} darks: no dark")
        else:
            few = {e: len(v) for e, v in by_exp.items()}
            ch.note = ("no darks at this epoch" if not few else
                       "too few darks (" + ", ".join(f"{n} x {e:g} s" for e, n in sorted(few.items()))
                       + f"; need {min_darks})")
        if not ch.frames:
            ch.alternatives = _dark_alternatives(cals, ep, le, by_exp, usable, have_bias,
                                                 min_darks, temp_tol, default_readout,
                                                 bias_misses or [])
        out[le] = ch
    return out


def _dark_alternatives(cals, ep, le, by_exp, usable, have_bias, min_darks, temp_tol,
                       default_readout, bias_misses) -> list[str]:
    """PS-178: the nearest usable alternatives for light length `le`, most
    direct first."""
    alt: list[str] = []
    same = [e for e in by_exp if abs(e - le) < 0.5]
    if same:
        n = len(by_exp[same[0]])
        alt.append(f"{n} x {le:g} s darks at this epoch: --min-darks {n} would use them "
                   f"(fewer frames, noisier master)")
    for m in epoch_misses(cals, ep, "DARK", exp=le, temp_tol=temp_tol,
                          default_readout=default_readout, limit=3):
        alt.append(f"{le:g} s darks off this epoch: {m}")
    if usable and not have_bias:
        lens = ", ".join(f"{e:g} s x {len(usable[e])}" for e in sorted(usable))
        alt.append(f"scale {lens} darks (optimizeDarks) once a bias at this epoch is in the "
                   f"Library" + (f"; bias sessions off this epoch: {'; '.join(bias_misses[:3])}"
                                 if bias_misses else "; no bias of this camera at all"))
    if not alt:
        alt.append(f"capture {le:g} s darks at {ep.label()} "
                   "(photonscript calibration-capture on the scope)")
    return alt


def _days(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except ValueError:
        return 10 ** 6


def match_flats(cals: list[Frame], lights: list[Frame], *, flat_temp_tol: float = 5.0,
                min_flats: int = 5, max_frames: int = 50) -> dict[str, FlatChoice]:
    """Per filter of `lights`: the nearest-in-date session of matching flats."""
    out: dict[str, FlatChoice] = {}
    filters = sorted({f.filter for f in lights})
    for filt in filters:
        lf = [f for f in lights if f.filter == filt]
        ch = FlatChoice(filter=filt)
        ref = lf[0]
        temps = [f.temp for f in lf if f.temp is not None]
        t_light = median(temps) if temps else None
        nights = sorted(f.night for f in lf if f.night)
        mid = nights[len(nights) // 2] if nights else ""
        cand, why = [], {}
        for c in cals:
            if c.kind != "FLAT":
                continue
            r = ""
            if ref.instrument and c.instrument.upper() != ref.instrument.upper():
                r = f"camera {c.instrument or '?'}"
            elif (c.filter or "").upper() != (filt or "").upper():
                r = f"filter {c.filter or 'none'}"
            elif (c.xbin or 1) != (ref.xbin or 1):
                r = f"bin {c.xbin}"
            elif t_light is not None and c.temp is not None and abs(c.temp - t_light) > flat_temp_tol:
                r = f"sensor {c.temp:g} C vs lights {t_light:g} C (uncooled flats)"
            if r:
                why.setdefault(c.session, r)
            else:
                cand.append(c)
        sessions: dict[str, list[Frame]] = {}
        for c in cand:
            sessions.setdefault(c.session, []).append(c)
        sessions = {s: v for s, v in sessions.items() if len(v) >= min_flats}
        if sessions:
            best = min(sessions, key=lambda s: (_days(s, mid), s))
            ch.session = best
            ch.frames = sorted(sessions[best], key=lambda x: x.name)[:max_frames]
            ch.note = f"{len(ch.frames)} flats from {best} ({_days(best, mid)} days from the median night {mid})"
        else:
            ch.note = "no matching flats" + (
                "; excluded: " + "; ".join(f"{s}: {r}" for s, r in sorted(why.items())) if why else "")
        out[filt] = ch
    return out


def plan(lights: list[Frame], cals: list[Frame], *, default_readout: str | None = None,
         use_flats: bool = True, min_darks: int = 10, max_frames: int = 50,
         temp_tol: float = 1.5, flat_temp_tol: float = 5.0) -> CalibrationPlan:
    """The whole calibration choice for a set of lights. Lights of several
    epochs (gain / offset / temp) use the most common one; the others are
    reported (the caller should not stack them, see pipeline)."""
    if not lights:
        raise ValueError("no lights")
    eps: dict[tuple, int] = {}
    for f in lights:
        e = Epoch.of(f, default_readout)
        eps[(e.instrument, e.gain, e.offset, e.xbin, e.temp, e.readout)] = \
            eps.get((e.instrument, e.gain, e.offset, e.xbin, e.temp, e.readout), 0) + 1
    key = max(eps, key=eps.get)
    ep = Epoch(*key)
    p = CalibrationPlan(epoch=ep)
    if len(eps) > 1:
        p.notes.append("lights span several epochs: " + "; ".join(
            f"{n} at {Epoch(*k).label()}" for k, n in sorted(eps.items(), key=lambda kv: -kv[1])))
    p.bias = match_bias(cals, ep, max_frames=max_frames, temp_tol=temp_tol,
                        default_readout=default_readout)
    if not p.bias:
        p.bias_misses = epoch_misses(cals, ep, "BIAS", temp_tol=temp_tol,
                                     default_readout=default_readout)
    p.notes.append(f"bias: {len(p.bias)} frames" + (
        f" ({', '.join(sorted({b.session for b in p.bias}))})" if p.bias else " (none match: darks unscaled only)"))
    for m in p.bias_misses:
        p.notes.append(f"  bias off this epoch: {m}")
    p.darks = match_darks(cals, ep, [f.exp for f in lights], have_bias=bool(p.bias),
                          min_darks=min_darks, max_frames=max_frames, temp_tol=temp_tol,
                          default_readout=default_readout, bias_misses=p.bias_misses)
    for d in p.darks.values():
        p.notes.append(f"darks for {d.light_exp:g} s lights: {d.note}")
        for a in d.alternatives:
            p.notes.append(f"  nearest alternative: {a}")
    if use_flats:
        p.flats = match_flats(cals, lights, flat_temp_tol=flat_temp_tol, max_frames=max_frames)
        for fc in p.flats.values():
            p.notes.append(f"flats {fc.filter}: {fc.note}")
    else:
        p.notes.append("flats: off (--no-flats)")
    return p


def lights_in_epoch(lights: list[Frame], ep: Epoch, default_readout: str | None = None,
                    temp_tol: float = 1.5) -> tuple[list[Frame], list[tuple[Frame, str]]]:
    """Split lights into those at the plan's epoch and the rest (+ why)."""
    keep, off = [], []
    for f in lights:
        r = epoch_reason(f, ep, temp_tol=temp_tol, default_readout=default_readout)
        (off.append((f, r)) if r else keep.append(f))
    return keep, off
