"""Pointing sanity of the lights an integrate run selects (header only).

The Library files a light under the target the sequence named, which is not
always where the mount was: on 2026-10-13 a run of "NGC 2408" (Camelopardalis,
+71 deg) stacked frames whose RA / DEC header was M27's. Three checks per
light, from its FITS header:

  off_target     the mount RA / DEC (NINA writes J2000 degrees) is more than
                 integrate_pointing_max_deg (1 deg) from the target's catalog
                 position;
  low_altitude   CENTALT (else OBJCTALT, else computed from RA / DEC, the
                 site and DATE-OBS) below integrate_min_alt_deg (20 deg);
  mount_stopped  two consecutive lights of one night whose altitude (and
                 azimuth when written) stayed pinned while RA advanced at
                 the sidereal rate: the mount was not tracking (a stopped
                 mount keeps its hour angle, so RA = LST - HA runs at
                 15.04 deg / h while alt / az stand still).

integrate_pointing_check: warn (default) = report them (run log, star QA
summary, ledger, AstroBin known issues) and stack everything; refuse =
leave those lights out; off = no check. Frames without RA / DEC (the
Piggy-600) are only counted. Pure functions; nothing reads pixels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from photonscript.integration.frames import Frame, parse_time

MODES = ("warn", "refuse", "off")
SIDEREAL_DEG_PER_H = 15.041
STOP_ALT_TOL_DEG = 0.05      # alt / az pinned between two subs
STOP_RATE_TOL = 0.25         # RA advance within 25% of the sidereal rate
STOP_MIN_GAP_S = 60.0
STOP_MAX_GAP_S = 3600.0


@dataclass
class Report:
    mode: str = "warn"
    target_ra: float | None = None
    target_dec: float | None = None
    max_deg: float = 1.0
    min_alt_deg: float = 20.0
    checked: int = 0
    no_position: int = 0
    flagged: dict = field(default_factory=dict)    # file name -> [issue text]
    notes: list = field(default_factory=list)

    def files(self) -> set[str]:
        return set(self.flagged)

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for issues in self.flagged.values():
            for k in {i.split(" ", 1)[0] for i in issues}:
                out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items()))

    def summary(self) -> dict:
        return {"mode": self.mode, "target_ra": self.target_ra, "target_dec": self.target_dec,
                "max_deg": self.max_deg, "min_alt_deg": self.min_alt_deg, "checked": self.checked,
                "no_position": self.no_position, "counts": self.counts(),
                "flagged": {k: v for k, v in sorted(self.flagged.items())}, "notes": list(self.notes)}

    def lines(self) -> list[str]:
        """Run-log / known-issue lines (empty when nothing was flagged)."""
        c = self.counts()
        if not c:
            return []
        what = {"off_target": f"more than {self.max_deg:g} deg from the target's catalog position",
                "low_altitude": f"shot below {self.min_alt_deg:g} deg altitude",
                "mount_stopped": "shot with the mount not tracking (alt pinned while RA ran at the "
                                 "sidereal rate)"}
        verb = "left out" if self.mode == "refuse" else "stacked anyway (integrate_pointing_check warn)"
        return [f"{n} light(s) {what.get(k, k)}: {verb}" for k, n in c.items()]


def sep_deg(ra1, dec1, ra2, dec2) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def altitude(f: Frame, lat: float | None, lon: float | None) -> float | None:
    """The light's altitude: CENTALT / OBJCTALT, else computed from its RA /
    DEC, the site (header SITELAT / SITELONG first) and DATE-OBS."""
    if f.alt_deg is not None:
        return f.alt_deg
    t = parse_time(f.date_obs)
    la = f.site_lat if f.site_lat is not None else lat
    lo = f.site_lon if f.site_lon is not None else lon
    if None in (f.ra_deg, f.dec_deg, t, la, lo):
        return None
    from photonscript.shared.pointing import alt_ha
    alt, _ = alt_ha(f.ra_deg, f.dec_deg, t + _exp_half(f), la, lo)
    return alt


def _exp_half(f: Frame) -> timedelta:
    return timedelta(seconds=(f.exp or 0) / 2.0)


def _ra_advance(ra1: float, ra2: float) -> float:
    return ((ra2 - ra1 + 180.0) % 360.0) - 180.0


def stopped_pairs(lights: list[Frame]) -> list[tuple[Frame, Frame, str]]:
    """Consecutive lights of one night with the mount not tracking."""
    by: dict[str, list[tuple[datetime, Frame]]] = {}
    for f in lights:
        t = parse_time(f.date_obs)
        if t is None or f.ra_deg is None or f.alt_deg is None:
            continue
        by.setdefault(f.night or "", []).append((t, f))
    out = []
    for rows in by.values():
        rows.sort(key=lambda x: (x[0], x[1].name))
        for (t1, a), (t2, b) in zip(rows, rows[1:]):
            dt = (t2 - t1).total_seconds()
            if not STOP_MIN_GAP_S <= dt <= STOP_MAX_GAP_S:
                continue
            if abs(b.alt_deg - a.alt_deg) > STOP_ALT_TOL_DEG:
                continue
            if a.az_deg is not None and b.az_deg is not None \
                    and abs(_ra_advance(a.az_deg, b.az_deg)) > STOP_ALT_TOL_DEG:
                continue
            want = SIDEREAL_DEG_PER_H * dt / 3600.0
            got = _ra_advance(a.ra_deg, b.ra_deg)
            if abs(got - want) <= STOP_RATE_TOL * want:
                out.append((a, b, f"alt {a.alt_deg:.2f} -> {b.alt_deg:.2f} deg while RA ran "
                                  f"{got * 4:.1f} min of time in {dt / 60:.1f} min"))
    return out


def check(lights: list[Frame], target_ra: float | None, target_dec: float | None, *,
          mode: str = "warn", max_deg: float = 1.0, min_alt_deg: float = 20.0,
          lat: float | None = None, lon: float | None = None) -> Report:
    """The pointing report of the selected lights (module doc)."""
    if mode not in MODES:
        raise ValueError(f"integrate_pointing_check must be one of {', '.join(MODES)} (got {mode!r})")
    rep = Report(mode=mode, target_ra=target_ra, target_dec=target_dec, max_deg=float(max_deg),
                 min_alt_deg=float(min_alt_deg))
    if mode == "off":
        return rep

    def flag(f: Frame, text: str) -> None:
        rep.flagged.setdefault(f.name, []).append(text)
    if target_ra is None or target_dec is None:
        rep.notes.append("target position not in the catalog: the off-target check was skipped")
    for f in lights:
        if f.ra_deg is None or f.dec_deg is None:
            rep.no_position += 1
        else:
            rep.checked += 1
            if target_ra is not None and target_dec is not None and max_deg > 0:
                d = sep_deg(f.ra_deg, f.dec_deg, target_ra, target_dec)
                if d > max_deg:
                    flag(f, f"off_target ({d:.1f} deg from the target: header RA {f.ra_deg:.3f} "
                            f"Dec {f.dec_deg:+.3f})")
        if min_alt_deg > 0:
            alt = altitude(f, lat, lon)
            if alt is not None and alt < min_alt_deg:
                flag(f, f"low_altitude ({alt:.1f} deg)")
    for a, b, why in stopped_pairs(lights):
        for f in (a, b):
            if not any(i.startswith("mount_stopped") for i in rep.flagged.get(f.name, [])):
                flag(f, f"mount_stopped ({why})")
    if rep.no_position and lights and rep.no_position == len(lights):
        rep.notes.append("no light has an RA / DEC header (Piggy-600): only the altitude check ran")
    return rep


def config_settings(cfg) -> dict:
    """Options fields from the config (integrate_pointing_check / _max_deg,
    integrate_min_alt_deg)."""
    mode = str(getattr(cfg, "integrate_pointing_check", "warn") or "warn").strip().lower()
    if mode not in MODES:
        raise ValueError(f"integrate_pointing_check must be one of {', '.join(MODES)} (got {mode!r})")
    return {"pointing_check": mode,
            "pointing_max_deg": float(getattr(cfg, "integrate_pointing_max_deg", 1.0)),
            "pointing_min_alt_deg": float(getattr(cfg, "integrate_min_alt_deg", 20.0))}
