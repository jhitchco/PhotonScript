"""PS-121: what the dashboard Mount row shows about where the mount points.

Input is the ninaAPI v2 ``/equipment/mount/info`` payload that /api/rigs
already reads (no extra NINA call). Units, as ninaAPI reports them:

  RightAscension   HOURS (0..24), in the mount's own epoch
  Declination      degrees
  Altitude, Azimuth  degrees
  SiderealTime     hours (local apparent sidereal time from the driver)
  EquatorialSystem ASCOM EquatorialCoordinateType: 1 / equTopocentric =
                   JNow, 2 / equJ2000, 3 / equJ2050, 4 / equB1950
  Coordinates.Epoch  NINA Epoch enum: 0 / JNOW, 1 / B1950, 2 / J2000,
                   3 / J2050 (fallback when EquatorialSystem is missing)
  SideOfPier       pierEast / pierWest / 0 / 1
  AtPark, Slewing, TrackingEnabled (v1: Tracking)

mount_view() turns that into display strings plus the numbers behind them.
Every value is "-" when the mount is not connected in NINA #1 (for example
while TheSky owns it for a TPoint run) or NINA #1 is unreachable.
"""

from __future__ import annotations

import math
from datetime import datetime

DASH = "-"
HINT_NOT_CONNECTED = "mount not connected in NINA #1"
HINT_UNREACHABLE = "NINA #1 not reachable"

_ASCOM_SYSTEM = {"1": "JNow", "equtopocentric": "JNow", "topocentric": "JNow",
                 "2": "J2000", "equj2000": "J2000", "j2000": "J2000",
                 "3": "J2050", "equj2050": "J2050", "j2050": "J2050",
                 "4": "B1950", "equb1950": "B1950", "b1950": "B1950"}
_NINA_EPOCH = {"0": "JNow", "jnow": "JNow", "1": "B1950", "b1950": "B1950",
               "2": "J2000", "j2000": "J2000", "3": "J2050", "j2050": "J2050"}


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _sexa(value: float, units: int) -> tuple[int, int, int]:
    """abs(value) -> (whole, minutes, seconds), rounded to the second with
    the carry done once (so 59.9996 never prints as 60s)."""
    total = int(round(abs(value) * 3600.0))
    whole, rest = divmod(total, 3600)
    return whole % units if units else whole, rest // 60, rest % 60


def fmt_ra(ra_hours) -> str:
    """RA in hours -> '12h 03m 05s' ('-' when unknown)."""
    v = _num(ra_hours)
    if v is None:
        return DASH
    h, m, s = _sexa(v % 24.0, 24)
    return f"{h:02d}h {m:02d}m {s:02d}s"


def fmt_dec(dec_deg) -> str:
    """Dec in degrees -> '+41d 16m 09s' ('-' when unknown)."""
    v = _num(dec_deg)
    if v is None:
        return DASH
    d, m, s = _sexa(v, 0)
    sign = "-" if v < 0 and (d or m or s) else "+"
    return f"{sign}{d:02d}d {m:02d}m {s:02d}s"


def fmt_deg1(v) -> str:
    """Alt / Az: degrees to 1 decimal ('-' when unknown)."""
    x = _num(v)
    return DASH if x is None else f"{x:.1f}"


def fmt_ha(ha_hours) -> str:
    """Hour angle -> '+1h 23m W' (west of the meridian, setting) or
    '-0h 40m E' (east, rising); '-' when unknown."""
    v = _num(ha_hours)
    if v is None:
        return DASH
    h, m, _s = _sexa(round(v * 60.0) / 60.0, 0)
    if h == 0 and m == 0:
        return "0h 00m"
    return f"{'-' if v < 0 else '+'}{h}h {m:02d}m {'E' if v < 0 else 'W'}"


def epoch_label(mount: dict) -> str | None:
    """'JNow' | 'J2000' | 'J2050' | 'B1950' from what NINA reports, else
    None (shown as 'epoch ?')."""
    sys_ = str(mount.get("EquatorialSystem", "")).strip().lower()
    if sys_ in _ASCOM_SYSTEM:
        return _ASCOM_SYSTEM[sys_]
    coords = mount.get("Coordinates")
    if isinstance(coords, dict):
        ep = str(coords.get("Epoch", "")).strip().lower()
        if ep in _NINA_EPOCH:
            return _NINA_EPOCH[ep]
    return None


def lst_hours(when_utc: datetime, lon_deg: float) -> float:
    """Local mean sidereal time (h) from the GMST polynomial (about 1 s);
    the same formula as scheduler.tracking_test.lst_hours."""
    d = (when_utc.replace(tzinfo=None)
         - datetime(2000, 1, 1, 12, 0, 0)).total_seconds() / 86400.0
    return (18.697374558 + 24.06570982441908 * d + lon_deg / 15.0) % 24.0


def mount_state(mount: dict) -> str:
    """parked | slewing | tracking | stopped."""
    if mount.get("AtPark"):
        return "parked"
    if mount.get("Slewing"):
        return "slewing"
    if mount.get("TrackingEnabled", mount.get("Tracking")):
        return "tracking"
    return "stopped"


def _pier(v) -> str | None:
    from photonscript.shared.pointing import pier_name
    return pier_name(v)


def mount_view(mount: dict | None, connected: bool, lon_deg: float | None = None,
               now: datetime | None = None, error: str = "") -> dict:
    """Display block for the dashboard Mount row (see module doc).

    Numbers: ra_hours, ra_deg (= hours x 15), dec_deg, alt_deg, az_deg,
    ha_hours, lst_hours; strings: ra, dec, alt, az, ha, pier, epoch, state.
    HA uses the driver's SiderealTime when present, else the LST from the
    configured longitude."""
    mount = mount if isinstance(mount, dict) else {}
    ra_h, dec = _num(mount.get("RightAscension")), _num(mount.get("Declination"))
    if not connected or ra_h is None or dec is None:
        return {"connected": False,
                "hint": HINT_UNREACHABLE if error else HINT_NOT_CONNECTED,
                "state": DASH, "ra": DASH, "dec": DASH, "epoch": None,
                "alt": DASH, "az": DASH, "pier": DASH, "ha": DASH,
                "ra_hours": None, "ra_deg": None, "dec_deg": None,
                "alt_deg": None, "az_deg": None, "ha_hours": None,
                "lst_hours": None}
    ra_h %= 24.0
    lst = _num(mount.get("SiderealTime"))
    if lst is None and lon_deg is not None:
        lst = lst_hours(now or datetime.utcnow(), float(lon_deg))
    ha = None if lst is None else round((lst - ra_h + 12.0) % 24.0 - 12.0, 4)
    alt, az = _num(mount.get("Altitude")), _num(mount.get("Azimuth"))
    pier = _pier(mount.get("SideOfPier"))
    return {"connected": True, "hint": "",
            "state": mount_state(mount),
            "ra": fmt_ra(ra_h), "dec": fmt_dec(dec),
            "epoch": epoch_label(mount),
            "alt": fmt_deg1(alt), "az": fmt_deg1(az),
            "pier": pier or DASH, "ha": fmt_ha(ha),
            "ra_hours": round(ra_h, 6), "ra_deg": round(ra_h * 15.0, 5),
            "dec_deg": round(dec, 5),
            "alt_deg": None if alt is None else round(alt, 3),
            "az_deg": None if az is None else round(az, 3),
            "ha_hours": ha,
            "lst_hours": None if lst is None else round(lst, 5)}
