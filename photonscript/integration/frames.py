"""PS-22: one FITS frame (light or calibration) described by its header.

Only the header is read (astropy getheader on the primary HDU), never the
pixels, so selecting and matching a few hundred frames on the Syncthing
mirror stays cheap. Nothing here writes anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared.rigs import header_readout

OSC_FILTER = "OSC"


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _i(v):
    x = _f(v)
    return None if x is None else int(round(x))


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def parse_time(s: str) -> datetime | None:
    """FITS date strings: 2026-10-03T20:01:24.3268722 (NINA writes 7 digit
    fractions, more than datetime accepts)."""
    s = _s(s)
    if not s:
        return None
    if "." in s:
        head, frac = s.split(".", 1)
        s = head + "." + frac[:6]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def night_of(date_loc: str, date_obs: str = "", tz: str = "America/Denver") -> str:
    """Evening date of the night a frame belongs to (local time minus 12 h),
    so a 02:00 sub files under the previous evening. DATE-LOC first; else
    DATE-OBS (UTC) converted to the site zone; '' when neither parses."""
    t = parse_time(date_loc)
    if t is None:
        u = parse_time(date_obs)
        if u is None:
            return ""
        try:
            from datetime import timezone
            from zoneinfo import ZoneInfo
            t = u.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)
        except Exception:  # noqa: BLE001 - no tz database: UTC - 7 (AARO MST)
            t = u - timedelta(hours=7)
    return (t - timedelta(hours=12)).strftime("%Y-%m-%d")


@dataclass
class Frame:
    path: Path
    kind: str = "LIGHT"            # LIGHT / DARK / BIAS / FLAT
    exp: float = 0.0
    gain: int | None = None
    offset: int | None = None
    set_temp: float | None = None
    ccd_temp: float | None = None
    readout: str | None = None     # normalized HCG / LCG (PS-128)
    readout_raw: str = ""
    instrument: str = ""
    filter: str = ""               # "OSC" for a Bayer frame with no filter
    xbin: int = 1
    bayer: str = ""
    date_obs: str = ""
    date_loc: str = ""
    night: str = ""
    session: str = ""              # calibration session folder (YYYY-MM-DD)
    target_dir: str = ""           # Library folder the light came from
    object: str = ""
    foctemp: float | None = None
    focallen: float | None = None
    focratio: float | None = None
    pixel_um: float | None = None
    telescope: str = ""
    software: str = ""
    site_lat: float | None = None
    site_lon: float | None = None
    site_elev: float | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    width: int | None = None
    height: int | None = None
    size: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def base(self) -> str:
        return self.path.stem

    @property
    def temp(self) -> float | None:
        """Sensor temperature to match on: setpoint, else the measured one."""
        return self.set_temp if self.set_temp is not None else self.ccd_temp

    @property
    def is_osc(self) -> bool:
        return bool(self.bayer)

    @property
    def exp_key(self) -> str:
        """Exposure label used for folders and group names: 120s, 0.14s."""
        e = round(self.exp, 2)
        return (f"{int(e)}s" if e == int(e) else f"{e:g}s")


def kind_of(imagetyp: str, default: str = "LIGHT") -> str:
    t = _s(imagetyp).upper()
    if "DARK" in t and "FLAT" in t:
        return "FLATDARK"
    for k in ("BIAS", "DARK", "FLAT", "LIGHT"):
        if k in t:
            return k
    return default


def from_header(path, hdr, *, kind: str | None = None, tz: str = "America/Denver",
                size: int = 0) -> Frame:
    """A Frame from a header-like mapping (astropy Header or a dict)."""
    get = hdr.get
    path = Path(path)
    bayer = _s(get("BAYERPAT"))
    filt = _s(get("FILTER"))
    if filt.lower() in ("none", "null"):
        filt = ""
    if not filt and bayer:
        filt = OSC_FILTER
    ro, ro_raw = header_readout(hdr)
    date_loc, date_obs = _s(get("DATE-LOC")), _s(get("DATE-OBS"))
    ra, dec = _f(get("RA")), _f(get("DEC"))
    return Frame(
        path=path,
        kind=kind or kind_of(get("IMAGETYP")),
        exp=_f(get("EXPTIME")) or _f(get("EXPOSURE")) or 0.0,
        gain=_i(get("GAIN")), offset=_i(get("OFFSET")),
        set_temp=_f(get("SET-TEMP")), ccd_temp=_f(get("CCD-TEMP")),
        readout=ro, readout_raw=ro_raw or "",
        instrument=_s(get("INSTRUME")), filter=filt,
        xbin=_i(get("XBINNING")) or 1, bayer=bayer,
        date_obs=date_obs, date_loc=date_loc,
        night=night_of(date_loc, date_obs, tz),
        object=_s(get("OBJECT")),
        foctemp=_f(get("FOCTEMP")), focallen=_f(get("FOCALLEN")),
        focratio=_f(get("FOCRATIO")), pixel_um=_f(get("XPIXSZ")),
        telescope=_s(get("TELESCOP")), software=_s(get("SWCREATE")),
        site_lat=_f(get("SITELAT")), site_lon=_f(get("SITELONG")),
        site_elev=_f(get("SITEELEV")),
        ra_deg=ra, dec_deg=dec,
        width=_i(get("NAXIS1")), height=_i(get("NAXIS2")),
        size=size,
    )


def read_frame(path, *, kind: str | None = None, tz: str = "America/Denver") -> Frame:
    """Header-only read of one FITS file (read-only open)."""
    from astropy.io import fits
    p = Path(path)
    hdr = fits.getheader(p)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return from_header(p, hdr, kind=kind, tz=tz, size=size)
