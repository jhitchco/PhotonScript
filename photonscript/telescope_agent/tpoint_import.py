"""TPoint import file: pointing samples in TPOINT's input-data format, for
TPoint Module Settings > Import Pointing Data (pure; the live loop that
fills it is tpoint_autocal_live, docs/TPOINT_AUTOCAL.md).

Why: on 2026-10-20 (the first hardware run of tpoint-autocal) the slews,
TheSky's camera and the saved frames all worked, but
sky6RASCOMTheSky.AutoMap() added nothing (ImageLinkResults "Process
aborted. Error _ 212.", AutoMap "requires ... Professional Edition and it
must be running. Error _ 224.", TPoint stayed at 561 samples). So the loop
now solves each frame itself (TheSky ImageLink, or ASTAP), records the
mount's own position at the exposure, and writes the pair here. Jeremy (or
a later scripted step) imports the file into TPoint and refits.

Format source: the real TheSky64 files in tests/test_scheduler/fixtures/
tpoint/ (TheSky Version 10.5.0 Build 14139, Paramount MX), matched byte for
byte against FORMAT_FIXTURE (a test regenerates that whole file from its
parsed values):

    !TheSky Version 10.5.0 Build 14139 (64 bit)        a "!" comment line
    Paramount MX                                        the caption
    :NODA                                               option lines
    :EQUAT
    +31 54 25 2026 10 15 30.81 870.00 1300.00  0.50 0.5500 0.0065
        latitude d m s, date y m d, then temperature, pressure, height,
        humidity, wavelength, lapse rate (copied from the newest real file)
    21 42 47.7967 +30 47 28.879 09 38 53.4448 +149 58 37.056  21 10.118
        one line per sample: true RA h m s.ssss, true Dec d m s.sss,
        observed RA, observed Dec, two spaces, sidereal time h m.mmm
        (minutes right-aligned in 6), one trailing space

CRLF line ends, no END line (TheSky64 writes none).

* true = the solved image centre (J2000 from ImageLink / ASTAP) converted
  to the apparent place of the date (erfa: ICRS -> CIRS, minus the
  equation of the origins), the place tpoint_coverage documents for
  TheSky's own files.
* observed = the mount's position as TheSky reports it
  (sky6RASCOMTele.GetRaDec, JNow). On the east-looking side (HA < 0: the
  side before the meridian flip) TPOINT's "through the pole" notation is
  used, as in every real file: RA + 12 h and Dec 180 - Dec (or -180 - Dec
  for a negative Dec). The west-looking side is written as read.
  CAVEAT (HANDBOOK "Hand import"): TheSky reports the mount AFTER any
  TPoint pointing corrections. With "Apply pointing corrections" on, the
  rows measure the current model's residuals, not raw pointing errors.

The writer never writes into TheSky's own folders (check_import_path).
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path, PureWindowsPath

DEFAULT_IMPORT_DIR = r"C:\astro\tpoint_import"
FORMAT_FIXTURE = "tests/test_scheduler/fixtures/tpoint/2026-10-15/TPoint base run in.dat"
DEFAULT_VERSION_LINE = "!TheSky Version 10.5.0 Build 14139 (64 bit)"
DEFAULT_CAPTION = "Paramount MX"
DEFAULT_OPTIONS = ("NODA", "EQUAT")
# temperature, pressure, height, humidity, wavelength, lapse rate as the
# 2026-10-16 file has them (used only when no real file is found)
DEFAULT_SITE_REST = "30.81 870.00 1300.00  0.50 0.5500 0.0065"
EOL = "\r\n"
_FORBIDDEN_PARTS = ("ninashare",)


# ------------------------------------------------------------- numbers

def fmt_ra(h: float) -> str:
    """Hours -> 'HH MM SS.ssss' (0 <= h < 24)."""
    u = int(round((float(h) % 24.0) * 36_000_000.0)) % (24 * 36_000_000)
    hh, r = divmod(u, 36_000_000)
    mm, r = divmod(r, 600_000)
    return f"{hh:02d} {mm:02d} {r // 10_000:02d}.{r % 10_000:04d}"


def fmt_dec(d: float, neg: bool | None = None) -> str:
    """Degrees -> '+DD MM SS.sss' (three degree digits past 99: the
    through-the-pole form). neg forces the sign (a '-00' Dec)."""
    d = float(d)
    sign = "-" if (neg if neg is not None else d < 0) else "+"
    u = int(round(abs(d) * 3_600_000.0))
    dd, r = divmod(u, 3_600_000)
    mm, r = divmod(r, 60_000)
    return f"{sign}{dd:02d} {mm:02d} {r // 1000:02d}.{r % 1000:03d}"


def fmt_lst(h: float) -> str:
    """Sidereal time hours -> 'HH MM.mmm' with the minutes right-aligned in
    6 ('21  1.182')."""
    u = int(round((float(h) % 24.0) * 60_000.0)) % (24 * 60_000)
    hh, r = divmod(u, 60_000)
    return f"{hh:02d} {r / 1000.0:6.3f}"


def fmt_lat(lat: float) -> str:
    """Latitude -> '+DD MM SS' (whole seconds, as TheSky writes it)."""
    sign = "-" if lat < 0 else "+"
    u = int(round(abs(float(lat)) * 3600.0))
    dd, r = divmod(u, 3600)
    mm, ss = divmod(r, 60)
    return f"{sign}{dd:02d} {mm:02d} {ss:02d}"


def wrap_ha(h: float) -> float:
    return ((float(h) + 12.0) % 24.0) - 12.0


# ------------------------------------------------------------- the sample

def j2000_to_apparent(ra_h: float, dec_d: float, utc: datetime) -> tuple[float, float]:
    """J2000 (ICRS) -> apparent place of the date (equinox based, geocentric:
    precession, nutation, aberration, light deflection), hours / degrees.
    erfa only (ships with astropy): no IERS table, works offline."""
    import erfa
    utc = utc.replace(tzinfo=None)
    d1 = 2440587.5 + (utc - datetime(1970, 1, 1)).total_seconds() / 86400.0
    tai1, tai2 = erfa.utctai(d1, 0.0)
    tt1, tt2 = erfa.taitt(tai1, tai2)
    ri, di, eo = erfa.atci13(math.radians(float(ra_h) * 15.0),
                             math.radians(float(dec_d)), 0.0, 0.0, 0.0, 0.0,
                             tt1, tt2)
    return math.degrees(erfa.anp(ri - eo)) / 15.0, math.degrees(di)


def beyond_pole(ha_h: float) -> bool:
    """TPOINT's through-the-pole notation is used on the east-looking side
    (HA < 0), as in every real TheSky64 file (tpoint_coverage)."""
    return wrap_ha(ha_h) < 0


def encode_observed(ra_h: float, dec_d: float, through_pole: bool) -> tuple[float, float]:
    """The mount reading as TPOINT wants it: as read, or (through the pole)
    RA + 12 h and Dec 180 - Dec (-180 - Dec for a negative Dec)."""
    ra_h, dec_d = float(ra_h) % 24.0, float(dec_d)
    if not through_pole:
        return ra_h, dec_d
    return (ra_h + 12.0) % 24.0, (180.0 - dec_d) if dec_d >= 0 else (-180.0 - dec_d)


def obs_line(true_ra_h: float, true_dec_d: float, obs_ra_h: float,
             obs_dec_d: float, lst_h: float, true_dec_neg: bool | None = None,
             obs_dec_neg: bool | None = None) -> str:
    """One observation line (no line end), TheSky64's spacing."""
    return (f"{fmt_ra(true_ra_h)} {fmt_dec(true_dec_d, true_dec_neg)} "
            f"{fmt_ra(obs_ra_h)} {fmt_dec(obs_dec_d, obs_dec_neg)}  {fmt_lst(lst_h)} ")


def sample_values(solved_ra_j2000_h: float, solved_dec_j2000_d: float,
                  mount_ra_h: float, mount_dec_d: float, lst_h: float,
                  utc: datetime) -> dict:
    """Everything one import line needs, from the solve and the mount
    snapshot: {"true_ra_h", "true_dec_d" (apparent), "obs_ra_h", "obs_dec_d"
    (TPOINT notation), "lst_h", "ha_h", "through_pole", "line"}."""
    tra, tdec = j2000_to_apparent(solved_ra_j2000_h, solved_dec_j2000_d, utc)
    ha = wrap_ha(float(lst_h) - float(mount_ra_h))
    tp = beyond_pole(ha)
    ora, odec = encode_observed(mount_ra_h, mount_dec_d, tp)
    return {"true_ra_h": tra, "true_dec_d": tdec, "obs_ra_h": ora,
            "obs_dec_d": odec, "lst_h": float(lst_h) % 24.0, "ha_h": round(ha, 4),
            "through_pole": tp,
            "line": obs_line(tra, tdec, ora, odec, lst_h)}


# ------------------------------------------------------------- the header

_SITE_RE = re.compile(r"^\s*([+-]?\d+\s+\d+\s+\d+)\s+(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(.*?)\s*$")


def reference_header(text: str | None) -> dict:
    """The header of a real TPoint input file: {"version_line", "caption",
    "options", "lat", "site_rest"}; missing pieces fall back to the
    defaults (DEFAULT_SITE_REST: the 2026-10-16 file's values)."""
    out = {"version_line": None, "caption": None, "options": [], "lat": None,
           "site_rest": None}
    for ln in (text or "").splitlines():
        s = ln.rstrip()
        if not s.strip():
            continue
        if s.startswith("!"):
            out["version_line"] = out["version_line"] or s
            continue
        if s.startswith(":"):
            out["options"].append(s[1:].strip())
            continue
        m = _SITE_RE.match(s)
        if m and out["lat"] is None and len(s.split()) >= 9:
            out["lat"], out["site_rest"] = m.group(1), m.group(5)
            break
        if out["caption"] is None:
            out["caption"] = s.strip()
    return out


def header_lines(date: datetime, lat_deg: float | None = None,
                 ref: dict | None = None, caption: str | None = None) -> list[str]:
    """The five header lines: comment, caption, :NODA / :EQUAT (or the
    reference's options), the site line for `date`."""
    ref = ref or {}
    lat = fmt_lat(lat_deg) if lat_deg is not None else (ref.get("lat") or fmt_lat(31.9069))
    lat = " ".join(lat.split())
    rest = ref.get("site_rest") or DEFAULT_SITE_REST
    opts = ref.get("options") or list(DEFAULT_OPTIONS)
    return ([ref.get("version_line") or DEFAULT_VERSION_LINE,
             caption or ref.get("caption") or DEFAULT_CAPTION]
            + [f":{o}" for o in opts]
            + [f"{lat} {date.year} {date.month} {date.day} {rest}"])


# ------------------------------------------------------------- the file

def import_path(dir_: str, stamp: str, side: str) -> str:
    return str(PureWindowsPath(dir_) / f"{stamp}_{side}.dat")


def check_import_path(path: str, forbidden_roots=()) -> None:
    """Refuses a target inside TheSky's own folders (TPoint's files: the
    import is Jeremy's step) or the ninashare mirror. ValueError."""
    p = str(PureWindowsPath(path)).lower().replace("/", "\\")
    if any(part in p for part in _FORBIDDEN_PARTS):
        raise ValueError(f"refusing to write the TPoint import file under ninashare: {path}")
    if "software bisque" in p or "\\tpoint\\" in p:
        raise ValueError(f"refusing to write into TheSky's folders: {path}")
    for r in forbidden_roots or ():
        root = str(PureWindowsPath(str(r))).lower().replace("/", "\\").rstrip("\\")
        if root and (p == root or p.startswith(root + "\\")):
            raise ValueError(f"refusing to write into TheSky's folder {r}: {path}")


class ImportFile:
    """Append-only import file: the header is written with the first
    sample, each line is flushed at once (a stopped run keeps what it
    solved)."""

    def __init__(self, path: str, header: list[str], forbidden_roots=()):
        check_import_path(path, forbidden_roots)
        self.path = path
        self.header = list(header)
        self.lines = 0

    def append(self, line: str) -> None:
        p = Path(self.path)
        new = not p.exists() or p.stat().st_size == 0
        if new:
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="ascii", newline="") as fh:
            if new:
                fh.write("".join(h + EOL for h in self.header))
            fh.write(line + EOL)
        self.lines += 1


def render(header: list[str], lines: list[str]) -> str:
    """The whole file as text (tests, dry runs)."""
    return "".join(x + EOL for x in [*header, *lines])


HEADER_LINES = 5   # "!" version, caption, ":NODA", ":EQUAT", site line


def merge_files(paths: list[str]) -> tuple[str, int]:
    """One import file from several (TPoint's Import Pointing Data REPLACES
    the data on each import, seen 2026-10-23, so the samples must arrive in
    one file). Header from the first file, then every sample line of every
    file in order. Returns (text, sample count)."""
    if not paths:
        raise ValueError("no files to merge")
    header: list[str] = []
    samples: list[str] = []
    for i, p in enumerate(paths):
        lines = Path(p).read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) < HEADER_LINES:
            raise ValueError(f"{p}: shorter than the {HEADER_LINES}-line header")
        if i == 0:
            header = lines[:HEADER_LINES]
        samples += [ln for ln in lines[HEADER_LINES:] if ln.strip()]
    return render(header, samples), len(samples)


def merge_dir(dir_: str = DEFAULT_IMPORT_DIR, stamp: str | None = None) -> dict:
    """Merge every *_west.dat / *_east.dat in dir_ (oldest first, never an
    earlier *_merged.dat) into <stamp>_merged.dat in the same folder."""
    d = Path(dir_)
    files = sorted(str(f) for f in d.glob("*.dat")
                   if not f.name.endswith("_merged.dat"))
    stamp = stamp or datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out = d / f"{stamp}_merged.dat"
    check_import_path(str(out))
    text, n = merge_files(files)
    out.write_text(text, encoding="utf-8", newline="")
    return {"out": str(out), "files": files, "samples": n}


DEFAULT_BACKUP_ROOT = r"C:\astro\tpoint_backup"
OLD_DATA_NAMES = ("Super Model Indat.dat", "Super Model Outdat.dat")


def count_samples(path) -> int:
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return sum(1 for ln in lines[HEADER_LINES:] if ln.strip())


def old_data_file(backup_root: str = DEFAULT_BACKUP_ROOT) -> str | None:
    """The pre-autocal pointing data (TheSky's own calibration runs, raw
    positions) from the OLDEST tpoint-autocal backup: its Super Model
    input data, else the *in.dat / *Indat.dat with the most samples."""
    root = Path(backup_root)
    dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    for d in dirs:
        named = [f for n in OLD_DATA_NAMES for f in d.rglob(n)]
        if named:
            return str(named[0])
        cands = [f for f in d.rglob("*.dat")
                 if f.name.lower().endswith(("in.dat", "indat.dat"))]
        if cands:
            return str(max(cands, key=count_samples))
    return None
