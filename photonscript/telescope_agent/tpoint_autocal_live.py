"""TPoint autocal, loop mode, LIVE (docs/TPOINT_AUTOCAL.md).

``photonscript tpoint-autocal --go --mode loop``: Jeremy starts it at the
console and stays there. Per point: open-loop slew to an alt / az
(SlewToAzAlt, async, polled), one exposure with TheSky's own camera
(TakeImage, async, polled), sky6RASCOMTheSky.AutoMap() (Image Link of that
frame + a TPoint sample), then the TPoint input-data row count is read back
from TPoint's files (tpoint_files) to confirm a sample was added, and a CSV
row is written.

Hard rules (the doc's): operator-started only; never arms, never
dispatches, never starts / loads a NINA sequence, never syncs, never parks,
never changes tracking; refuses unless every preflight check passes
(unknown = no go); the camera is lent, never taken: NINA #1 disconnects it,
TheSky connects it, and at the end (always, also after Ctrl+C or an error)
TheSky disconnects it and NINA #1 must read Connected again.

Every outside call goes through four small adapters (TheSky TCP 3040, NINA
#1's ninaAPI, the scheduler's GET endpoints, the TPoint file count) so the
tests drive the whole loop with fakes. The TheSky scripts are the reviewed
builders in tpoint_autocal (this module writes no JavaScript of its own).

Status: implemented, UNTESTED ON HARDWARE. AutoMap's effect on build 14139
is the open question; the first run is the supervised 3-point test
(``--test``).
"""

from __future__ import annotations

import csv
import math
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from photonscript.scheduler import tpoint_mapping as tm
from photonscript.telescope_agent import tpoint_autocal as ta

SIDES = ("east", "west", "both")
LOOP_MAX_POINTS = ta.MAX_POINTS
TEST_POINTS = 3
TEST_SIDE = "west"
SLEW_TIMEOUT_S = 180.0
EXPOSE_EXTRA_S = 120.0       # download + save on top of the exposure
POLL_S = 1.0
SETTLE_S = 2.0
FLIP_S = 90.0                # a pier flip between the east and west halves
MAX_AUTOMAP_FAILS = 2        # in a row
# --side east / west: at least this far from the meridian, so the mount's
# pier-side choice cannot put a "west" sample on the east side (TPoint's
# meridian band in tpoint_mapping is 0.25 h)
SIDE_MARGIN_H = 0.5
NINA_WAIT_S = 20.0           # camera Connected to change after (dis)connect

CSV_FIELDS = ["utc", "point", "of", "side", "alt", "az", "ha_h", "dec_deg",
              "slew_s", "image", "automap_err", "solved", "error_text",
              "solution_rms", "image_scale", "samples_before",
              "samples_after", "added", "note"]


# ------------------------------------------------------------- helpers

def truthy(v) -> bool:
    return str(v if v is not None else "").strip().lower() in (
        "1", "true", "yes", "on", "-1")


def _fmt_min(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s} s" if s < 90 else f"{round(s / 60)} min"


def _alt_of(ha_h: float, dec_deg: float, lat: float) -> float:
    h, d, p = math.radians(ha_h * 15.0), math.radians(dec_deg), math.radians(lat)
    s = math.sin(d) * math.sin(p) + math.cos(d) * math.cos(p) * math.cos(h)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def pace_s(exposure_s: float) -> float:
    """Seconds per point: the exposure plus slew / settle / download /
    Image Link (ta.OVERHEAD_S, ~35 s)."""
    return float(exposure_s) + ta.OVERHEAD_S


# ------------------------------------------------------------- the points

def _thin(pts: list[dict], n: int) -> list[dict]:
    if len(pts) <= n:
        return list(pts)
    step = len(pts) / n
    return [pts[int(i * step)] for i in range(n)]


def _coverage_points(config, n: int, side: str):
    """Points from the TPoint coverage module when it exists (a later
    branch); None (with why) when it does not, so the caller falls back to
    the grid + side filter. Never raises."""
    try:
        from photonscript.scheduler import tpoint_coverage as cov  # type: ignore
    except Exception:  # noqa: BLE001
        return None, "no coverage module in this build: grid + side filter"
    fn = getattr(cov, "suggest_points", None)
    if not callable(fn):
        return None, "coverage module has no suggest_points(): grid + side filter"
    try:
        pts = fn(config, n=n, side=side)
        out = [p for p in (pts or []) if "alt" in p and "az" in p]
        if not out:
            return None, "coverage module suggested nothing: grid + side filter"
        return out, "coverage module (the emptiest sky first)"
    except Exception as e:  # noqa: BLE001
        return None, f"coverage module failed ({e}): grid + side filter"


def select_points(config, n: int, side: str = "both",
                  now: datetime | None = None, exposure_s: float = 5.0,
                  dark_end: datetime | None = None,
                  sky_points: list | None = None,
                  moon_altaz: list | None = None,
                  from_coverage: bool = False) -> dict:
    """The run's points: tpoint_mapping's equal-area grid (meridian band,
    under-pole and moon skipped), filtered to `side`, thinned evenly to n
    and walked serpentine (east then west, hour-angle strips, Dec up /
    down: short slews). Then the during-run filter, each point at its own
    slot time (now + i x pace):

    * the slew goes to a fixed alt / az, then the mount TRACKS for the
      point's dwell (one pace): a point whose tracked position would drop
      below min alt, or (east side) drift into the meridian band, or
      (west side) past the under-pole limit, before its dwell ends is
      dropped;
    * a point whose slot ends after dark_end (astronomical dawn) is
      dropped;
    * with side east / west, points within SIDE_MARGIN_H of the meridian
      are dropped too (the pier side there is the mount's choice).

    {"points": [{alt, az, ha_h, dec_deg, side, eta_utc}], "dropped": {...},
    "source", "pace_s", "est_s", "note"}. Pure apart from the grid and the
    optional moon / coverage lookups."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, got {side!r}")
    n = max(1, min(int(n), LOOP_MAX_POINTS))
    now = (now or datetime.utcnow()).replace(tzinfo=None)
    p = tm.params(config)
    min_alt, max_ha = p["min_alt"], p["max_ha_h"]
    lat = float(getattr(config, "observatory_lat", 31.9))
    pace = pace_s(exposure_s)
    dwell_h = pace / 3600.0
    source, note = "grid", ""
    pts = None
    if sky_points is None and from_coverage:
        pts, note = _coverage_points(config, n, side)
        if pts is not None:
            source = "coverage"
            pts = [dict(q) for q in pts]
            for q in pts:
                if "ha_h" not in q or "dec_deg" not in q:
                    ha, dec = tm.altaz_to_hadec(q["alt"], q["az"], lat)
                    q.update(ha_h=round(ha, 3), dec_deg=round(dec, 3))
                q.setdefault("side", tm.side_of(q["ha_h"]))
    if pts is None and sky_points is not None:
        pts = [dict(q) for q in sky_points]
        source = "given"
    if pts is None:
        want = n if side == "both" else 3 * n
        want = max(tm.MIN_POINTS, min(int(want * 1.3) + 2, tm.MAX_POINTS))
        if moon_altaz is None:
            moon_altaz = tm.moon_track(config, now, n * pace / 3600.0)
        pts = tm.plan_points(want, lat, min_alt, moon_altaz, p["moon_deg"],
                             max_ha)["points"]
    dropped = {"side": 0, "sets_or_meridian": 0, "dawn": 0}
    kept = []
    for q in pts:
        if side != "both" and (q.get("side") != side
                               or abs(float(q["ha_h"])) < SIDE_MARGIN_H):
            dropped["side"] += 1
            continue
        ha_end = float(q["ha_h"]) + dwell_h
        alt_end = _alt_of(ha_end, float(q["dec_deg"]), lat)
        if (float(q["alt"]) < min_alt or alt_end < min_alt
                or (q["side"] == tm.EAST and ha_end > -tm.MERIDIAN_BAND_H)
                or (q["side"] == tm.WEST and ha_end > max_ha)):
            dropped["sets_or_meridian"] += 1
            continue
        kept.append(q)
    kept = tm.order_points(_thin(kept, n)) if source != "coverage" else kept[:n]
    out, t = [], 0.0
    prev_side = None
    for q in kept:
        if prev_side is not None and q["side"] != prev_side:
            t += FLIP_S
        end = now + timedelta(seconds=t + pace)
        if dark_end is not None and end > dark_end.replace(tzinfo=None):
            dropped["dawn"] += 1
            continue
        out.append({**q, "eta_utc": (now + timedelta(seconds=t)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")})
        t += pace
        prev_side = q["side"]
    return {"points": out, "dropped": dropped, "source": source,
            "pace_s": pace, "est_s": t, "note": note, "side": side,
            "min_alt": min_alt}


def format_points(sel: dict) -> str:
    pts = sel["points"]
    d = sel["dropped"]
    lines = [f"{len(pts)} point(s), side {sel['side']}, from {sel['source']}"
             + (f" ({sel['note']})" if sel.get("note") else "")
             + f"; about {_fmt_min(sel['est_s'])} at ~{sel['pace_s']:.0f} s a point."
             f" Left out: {d['side']} on the other side or near the meridian, "
             f"{d['sets_or_meridian']} that would set or reach the meridian "
             f"while being measured, {d['dawn']} after dawn."]
    for i, q in enumerate(pts, 1):
        lines.append(f"  {i:3}. {q['side']:4} alt {q['alt']:5.1f} az "
                     f"{q['az']:5.1f}  HA {q['ha_h']:+5.2f} h  Dec "
                     f"{q['dec_deg']:+6.1f}  at {q['eta_utc'][11:16]} UTC")
    return "\n".join(lines)


# ------------------------------------------------------------- adapters

class TheSkyLink:
    """TheSky TCP 3040: run one builder script, return its k=v reply."""

    def __init__(self, client):
        self.client = client

    def run(self, js: str) -> dict:
        from photonscript.telescope_agent.thesky_client import parse_kv
        return parse_kv(self.client.run_script(js))


class NinaLink:
    """NINA #1's ninaAPI through nina_client (one short event loop per
    call, so the loop itself stays synchronous)."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def _call(self, name: str, *args):
        import asyncio
        from photonscript.telescope_agent.nina_client import NinaClient

        async def go():
            c = NinaClient(self.base_url)
            try:
                return await getattr(c, name)(*args)
            finally:
                await c.close()
        return asyncio.run(go())

    def sequence_state(self) -> dict:
        return self._call("get_sequence_status")

    def camera_info(self) -> dict:
        return self._call("get_camera_info")

    def disconnect_camera(self):
        return self._call("disconnect_camera")

    def connect_camera(self):
        return self._call("connect_camera")

    def cool_camera(self, temperature: float):
        return self._call("cool_camera", temperature)

    def safety(self) -> dict:
        return self._call("get_safety_info")


class SchedulerLink:
    """The scheduler's read-only GET endpoints."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def _get(self, path: str, timeout: float):
        import httpx
        return httpx.get(self.url + path, timeout=timeout).json()

    def arm(self) -> dict | None:
        return self._get("/api/arm", 10)

    def apps(self) -> dict | None:
        return self._get("/api/apps/status", 30)


def sample_count(config, listing: dict | None = None) -> dict:
    """TPoint's raw input data rows ("<run> in.dat", "Super Model
    Indat.dat"): {"total", "files": {rel: points}, "newest"}. total None =
    no such file (no signal). Read only."""
    from photonscript.scheduler import tpoint_files as tf
    lst = listing if listing is not None else tf.list_files(config)
    files: dict = {}
    newest = None
    for f in lst.get("files") or []:          # newest first
        if tf._data_rank(f) != 0:
            continue
        text = tf._read(f["path"])
        d = tf.parse_data(text) if text is not None else None
        if d:
            files[f["rel"]] = d["points"]
            newest = newest or f["rel"]
    return {"total": sum(files.values()) if files else None,
            "files": files, "newest": newest}


# ------------------------------------------------------------- the run

class StopRun(Exception):
    """A stop rule fired (the reason is the message)."""


@dataclass
class Deps:
    sky: object                       # .run(js) -> dict
    nina: object                      # NinaLink-like
    sched: object                     # .arm(), .apps()
    count: Callable[[], dict]         # sample_count-like
    list_files: Callable[[], dict]    # tpoint_files.list_files-like
    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print
    sleep: Callable[[float], None] = time.sleep
    mono: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = datetime.utcnow
    copy: Callable[[str, str], None] | None = None
    notify: Callable[[str], None] | None = None


@dataclass
class Opts:
    exposure_s: float | None = None
    binning: int = 2
    image_scale_per_bin1: float = 0.236
    test: bool = False
    verbose: bool = False
    slew_timeout_s: float = SLEW_TIMEOUT_S
    backup_root: str = ta.DEFAULT_BACKUP_ROOT
    csv_path: str | None = None
    auto_arm_enabled: bool = False
    auto_dusk_flats: bool = False
    min_alt: float = 30.0
    extra: dict = field(default_factory=dict)


def _local(config, utc: datetime) -> str:
    try:
        from photonscript.shared.localtime import to_local
        return to_local(config, utc).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return utc.strftime("%H:%M UTC")


class LoopRun:
    def __init__(self, config, points: list[dict], deps: Deps, opts: Opts):
        self.cfg = config
        self.points = points
        self.d = deps
        self.o = opts
        self.rows: list[dict] = []
        self.lent = False             # NINA #1 was told to let go
        self.sky_cam = False          # TheSky's camera connect was sent
        self.cam_before: dict = {}
        self.reason = ""
        self.signal_live: bool | None = None   # the file count moves per add
        self.before: dict = {}
        self.after: dict = {}
        self.t0 = 0.0
        self.exposure_s = 5.0
        self.returned: bool | None = None
        self.last_total = None
        self.confirmed = False
        self.busy = False             # a slew or exposure may be in flight

    # -- small helpers
    def say(self, msg: str):
        self.d.say(msg)

    def v(self, msg: str):
        if self.o.verbose or self.o.test:
            self.d.say("    " + msg)

    def sky(self, name: str, js: str) -> dict:
        self.v(f"TheSky <- {name}")
        r = self.d.sky.run(js)
        self.v(f"TheSky -> {r}")
        return r

    def yes(self, prompt: str) -> bool:
        try:
            a = self.d.ask(prompt + " Type yes to go on: ")
        except EOFError:
            return False
        return str(a or "").strip().lower() == "yes"

    # -- 1. preflight
    def preflight(self) -> dict:
        self.say("Step 1: checking it is safe to borrow the scope "
                 "(armer, both NINAs, roof, dark, TheSky, auto-arm).")
        arm = apps = roof = None
        try:
            arm = self.d.sched.arm()
        except Exception as e:  # noqa: BLE001
            self.say(f"  could not read the armer ({e})")
        try:
            apps = self.d.sched.apps()
        except Exception as e:  # noqa: BLE001
            self.say(f"  could not read the apps status ({e})")
        try:
            s = self.d.nina.safety() or {}
            roof = (bool(s.get("IsSafe")) if s.get("Connected") is not False
                    and s.get("IsSafe") is not None else None)
        except Exception as e:  # noqa: BLE001
            self.say(f"  could not read NINA #1's safety monitor ({e})")
        backup = None
        try:
            backup = ta.backup_plan(self.d.list_files(), self.d.now(),
                                    self.o.backup_root)
        except Exception as e:  # noqa: BLE001
            self.say(f"  could not list TPoint's files ({e})")
        pre = ta.preflight(arm, apps, roof, backup,
                           auto_arm_enabled=self.o.auto_arm_enabled,
                           auto_dusk_flats=self.o.auto_dusk_flats)
        pre["backup"] = backup
        for c in pre["checks"]:
            self.say(f"  [{c['status'].upper():7}] {c['name']}: {c['detail']}")
        return pre

    # -- 2. backup
    def backup(self, plan: dict) -> bool:
        self.say(f"Step 2: backing up {len(plan['copies'])} TPoint file(s) "
                 f"to {plan['dest']}.")
        cp = self.d.copy or _copy_file
        for c in plan["copies"]:
            try:
                cp(c["src"], c["dst"])
            except Exception as e:  # noqa: BLE001
                self.say(f"  backup of {c['src']} failed ({e}): stopping, "
                         "nothing was sent to TheSky or NINA")
                return False
        self.before = self.d.count()
        self.last_total = self.before.get("total")
        self.say(f"  TPoint input data now: {self._cnt(self.before)} sample(s) "
                 "in the files.")
        self.say("  In TheSky: open the TPoint window, write down its point "
                 "count, and if it has unsaved points save them first "
                 "(File > Save). Check TheSky's Camera Setup selects the AP26MC "
                 "and PHD2 is not guiding.")
        return self.yes("  Backup done, TPoint count noted, camera setup checked?")

    @staticmethod
    def _cnt(c: dict) -> str:
        return "unknown (no in.dat file found)" if c.get("total") is None \
            else str(c["total"])

    # -- 3. lend the camera
    def lend(self) -> None:
        self.say("Step 3: lending the camera: NINA #1 lets go, TheSky connects it.")
        st = self.d.nina.sequence_state() or {}
        if str(st.get("State") or "").upper() != "IDLE":
            raise StopRun(f"NINA #1 is not idle ({st.get('State')}): not lending "
                          "the camera")
        self.cam_before = self.d.nina.camera_info() or {}
        self.v(f"NINA #1 camera before: Connected {self.cam_before.get('Connected')}, "
               f"cooler {self.cam_before.get('CoolerOn')}, setpoint "
               f"{self.cam_before.get('TemperatureSetPoint')}")
        self.lent = True
        self.d.nina.disconnect_camera()
        if not self._wait_nina(False):
            raise StopRun("NINA #1 still shows the camera Connected after "
                          "disconnect")
        self.say("  NINA #1 camera disconnected.")
        cam = self.sky("camera_read", ta.camera_read_script())
        self.say(f"  TheSky's selected camera: {cam.get('camera_model')}")
        self.sky_cam = True
        r = self.sky("camera_connect", ta.camera_connect_script())
        if r.get("err"):
            raise StopRun(f"TheSky could not connect its camera: {r['err']}")
        self.say(f"  TheSky camera connected (status {r.get('status')}).")

    def _wait_nina(self, want: bool) -> bool:
        end = self.d.mono() + NINA_WAIT_S
        while True:
            try:
                c = bool((self.d.nina.camera_info() or {}).get("Connected"))
                if c == want:
                    return True
            except Exception as e:  # noqa: BLE001
                self.v(f"NINA camera info failed ({e})")
            if self.d.mono() >= end:
                return False
            self.d.sleep(POLL_S)

    # -- 4. setup
    def setup(self) -> None:
        ails = {}
        try:
            from photonscript.telescope_agent.thesky_client import READ_ONLY_JS
            ails = self.sky("ails (read)", READ_ONLY_JS["ails"])
        except Exception as e:  # noqa: BLE001
            self.v(f"AILS read failed ({e})")
        t = self.o.exposure_s
        if t is None:
            try:
                t = float(ails.get("exposure_s"))
            except (TypeError, ValueError):
                t = None
            if t is None or not 0.5 <= t <= 60:
                t = float(getattr(self.cfg, "tpoint_mapping_exposure_s", 5.0) or 5.0)
        self.exposure_s = t
        scale = self.o.image_scale_per_bin1 * self.o.binning
        self.say(f"Step 4: TheSky camera {t:g} s, bin {self.o.binning}; "
                 f"Image Link scale {scale:.3f}\"/px (TheSky's Automated "
                 f"Image Link had {ails.get('exposure_s')} s, "
                 f"{ails.get('image_scale')}\"/px).")
        self.sky("camera_setup", ta.camera_setup_script(t, self.o.binning))
        self.sky("ails_setup", ta.ails_setup_script(scale, t))

    # -- safety re-check before each point
    def recheck(self) -> None:
        try:
            arm = self.d.sched.arm() or {}
        except Exception as e:  # noqa: BLE001
            raise StopRun(f"armer unreadable ({e})") from None
        if str(arm.get("state") or "") not in ta.ARMER_OK_STATES:
            raise StopRun(f"the armer left DISARMED ({arm.get('state')})")
        try:
            s = self.d.nina.safety() or {}
        except Exception as e:  # noqa: BLE001
            raise StopRun(f"safety monitor unreadable ({e})") from None
        if s.get("IsSafe") is not True:
            raise StopRun("the roof reads UNSAFE (or unknown)")
        try:
            st = self.d.nina.sequence_state() or {}
        except Exception as e:  # noqa: BLE001
            raise StopRun(f"NINA #1 sequence state unreadable ({e})") from None
        if str(st.get("State") or "").upper() != "IDLE":
            raise StopRun("NINA #1 shows a RUNNING sequence item")

    # -- one point
    def _poll(self, name: str, js: str, key: str, timeout: float) -> dict | None:
        end = self.d.mono() + timeout
        while True:
            r = self.d.sky.run(js)
            if truthy(r.get(key)):
                self.v(f"{name}: {r}")
                return r
            if self.d.mono() >= end:
                return None
            self.d.sleep(POLL_S)

    def _fail(self, row: dict, note: str, why: str):
        row["note"] = note
        self.rows.append(row)
        self._write_csv()
        raise StopRun(why)

    def point(self, i: int, q: dict) -> dict:
        n = len(self.points)
        row = {"utc": self.d.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "point": i,
               "of": n, "side": q["side"], "alt": q["alt"], "az": q["az"],
               "ha_h": q.get("ha_h"), "dec_deg": q.get("dec_deg")}
        self.say(f"Point {i}/{n} ({q['side']} side, alt {q['alt']:.1f} az "
                 f"{q['az']:.1f}): slewing.")
        t = self.d.mono()
        self.busy = True
        r = self.sky("slew", ta.slew_altaz_script(q["alt"], q["az"],
                                                  f"tpoint autocal {i}",
                                                  self.o.min_alt))
        if r.get("err"):
            raise StopRun(f"TheSky refused the slew to point {i}: {r['err']}")
        if self._poll("slew_poll", ta.slew_poll_script(), "slew_complete",
                      self.o.slew_timeout_s) is None:
            self._fail(row, "slew timeout", f"slew to point {i} did not "
                       f"finish in {self.o.slew_timeout_s:.0f} s")
        row["slew_s"] = round(self.d.mono() - t, 1)
        self.d.sleep(SETTLE_S)
        self.say(f"  slewed in {row['slew_s']:.0f} s; exposing {self.exposure_s:g} s.")
        r = self.sky("expose", ta.expose_script())
        if r.get("err"):
            self._fail(row, "expose error",
                       f"TheSky could not start the exposure: {r['err']}")
        r = self._poll("expose_poll", ta.expose_poll_script(), "complete",
                       self.exposure_s + EXPOSE_EXTRA_S)
        if r is None:
            self._fail(row, "exposure timeout",
                       f"the exposure at point {i} did not finish")
        row["image"] = r.get("last_image")
        self.say("  frame saved; AutoMap (Image Link + TPoint sample).")
        prev = self.last_total
        a = self.sky("automap", ta.automap_script())
        self.busy = False
        solved = not a.get("err") and truthy(a.get("succeeded"))
        row.update(automap_err=a.get("err") or "", solved=solved,
                   error_text=a.get("error_text") or "",
                   solution_rms=a.get("solution_rms"),
                   image_scale=a.get("image_scale"))
        try:
            self.after = self.d.count()
        except Exception as e:  # noqa: BLE001
            self.v(f"TPoint file count failed ({e})")
            self.after = {"total": None}
        now_n = self.after.get("total")
        if now_n is not None:
            self.last_total = now_n
        row["samples_before"], row["samples_after"] = prev, now_n
        added = (prev is not None and now_n is not None and now_n > prev)
        row["added"] = added if now_n is not None and prev is not None else ""
        self.say(f"  AutoMap: {'solved' if solved else 'NOT solved'}"
                 + (f", RMS {a.get('solution_rms')}\"" if solved else
                    f" ({a.get('err') or a.get('error_text') or 'no reason given'})")
                 + f"; TPoint samples in the files {prev} -> {now_n}.")
        return row

    # -- the loop
    def loop(self) -> None:
        fails = 0
        n = len(self.points)
        for i, q in enumerate(self.points, 1):
            self.recheck()
            row = self.point(i, q)
            self.rows.append(row)
            solved = bool(row.get("solved"))
            try:
                if not self.confirmed and solved:
                    self._confirm_first(row)      # raises when no sample
                    self.confirmed = True
                elif self.o.test and i == 1:
                    self.say("  Point 1 did not solve, so there is nothing to "
                             "look for in TPoint yet.")
                    if not self.yes("  Try the next point?"):
                        raise StopRun("operator stopped after point 1")
            finally:
                self._write_csv()
            ok = solved and not (self.signal_live and row.get("added") is False)
            if solved and not ok:
                row["note"] = "solved but no sample added"
            fails = 0 if ok else fails + 1
            if fails >= MAX_AUTOMAP_FAILS:
                raise StopRun(f"{MAX_AUTOMAP_FAILS} AutoMap failures in a row "
                              "(clouds, focus, or AutoMap not adding)")
            left = (n - i) * pace_s(self.exposure_s)
            if i < n:
                fin = self.d.now() + timedelta(seconds=left)
                self.say(f"  done {i}/{n}; about {_fmt_min(left)} left "
                         f"(finish ~{_local(self.cfg, fin)} local).")

    def _confirm_first(self, row: dict) -> bool:
        """After the first solved point (point 1 unless clouds spoiled
        it): did a TPoint sample appear? The file count says so when it
        moved (--test asks the operator anyway); when it did not, or there
        is no file, the operator looks at TPoint's pointing data. No =
        stop: "AutoMap did not add a sample"."""
        added = row.get("added")
        if added is True and not self.o.test:
            self.signal_live = True
            self.say("  TPoint's input file gained a sample: AutoMap adds points.")
            return True
        if added is True:
            self.signal_live = True
            self.say("  TPoint's input file gained a sample.")
        else:
            self.say("  TPoint's input file did NOT change (TPoint may only "
                     "write it on save)." if added is False else
                     "  No TPoint input file to count.")
        self.say("  Look at TPoint's pointing data window: is there one new "
                 "sample at the end (the point count went up by one)?")
        if self.yes("  New sample there?"):
            if added is not True:
                self.signal_live = False
                row["note"] = "added (operator saw it; file not live)"
            return True
        row["note"] = "operator: no sample added"
        raise StopRun("AutoMap did not add a sample")

    def abort(self) -> None:
        """Abort TheSky's camera and mount, only while a slew or exposure
        may be running (between points nothing moves: no Abort sent)."""
        if not self.busy:
            return
        try:
            r = self.d.sky.run(ta.abort_script())
            self.say(f"  sent Abort to TheSky's camera and mount ({r}).")
        except Exception as e:  # noqa: BLE001
            self.say(f"  Abort to TheSky FAILED ({e}): stop the mount by hand "
                     "in TheSky if it is still moving.")

    # -- 5. hand-back (always)
    def handback(self) -> bool:
        self.say("Giving the camera back: TheSky disconnects, NINA #1 connects.")
        if self.sky_cam:
            try:
                r = self.sky("camera_disconnect", ta.camera_disconnect_script())
                if r.get("err"):
                    self.say(f"  TheSky camera disconnect said: {r['err']}")
            except Exception as e:  # noqa: BLE001
                self.say(f"  TheSky camera disconnect failed ({e})")
        ok = False
        try:
            self.d.nina.connect_camera()
            ok = self._wait_nina(True)
        except Exception as e:  # noqa: BLE001
            self.say(f"  NINA #1 camera connect failed ({e})")
        if ok:
            self.say("  NINA #1 camera Connected again.")
            sp = self.cam_before.get("TemperatureSetPoint")
            if self.cam_before.get("CoolerOn") and sp is not None:
                try:
                    self.d.nina.cool_camera(float(sp))
                    self.say(f"  cooler re-applied at {float(sp):g} C.")
                except Exception as e:  # noqa: BLE001
                    self.say(f"  cooler re-apply failed ({e}): set it in NINA #1")
        else:
            msg = ("TPoint autocal: NINA #1 did NOT get the camera back. In "
                   "TheSky: Camera > Disconnect; in NINA #1: Equipment > "
                   "Camera > Connect, then check cooling.")
            self.say("!" * 70)
            self.say(msg)
            self.say("!" * 70)
            if self.d.notify:
                try:
                    self.d.notify(msg)
                except Exception:  # noqa: BLE001
                    pass
        return ok

    def _write_csv(self) -> None:
        if not self.o.csv_path:
            return
        p = Path(self.o.csv_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.exists()
            with open(p, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(self.rows[-1])
        except OSError as e:
            self.v(f"CSV write failed ({e})")

    # -- everything
    def run(self) -> dict:
        self.t0 = self.d.mono()
        pre = self.preflight()
        if not pre["go"]:
            self.say("NO GO: " + "; ".join(pre["blockers"]))
            return self.summary("refused", "preflight: " + "; ".join(pre["blockers"]))
        if not self.backup(pre["backup"]):
            return self.summary("refused", "operator did not confirm the backup")
        status = "stopped"
        try:
            self.lend()
            self.setup()
            self.say(f"Step 5: {len(self.points)} point(s), about "
                     f"{_fmt_min(len(self.points) * pace_s(self.exposure_s))}. "
                     "Ctrl+C stops safely (camera and mount abort, camera "
                     "goes back to NINA #1).")
            self.loop()
            status, self.reason = "done", "all points done"
        except StopRun as e:
            self.reason = str(e)
            self.say(f"STOP: {self.reason}")
            self.abort()
        except KeyboardInterrupt:
            self.reason = "operator Ctrl+C"
            self.say("Ctrl+C: stopping.")
            self.abort()
        except Exception as e:  # noqa: BLE001
            self.reason = f"error {type(e).__name__}: {e}"
            self.say(f"STOP on an error: {self.reason}")
            self.abort()
        finally:
            if self.lent:
                try:
                    self.returned = self.handback()
                except KeyboardInterrupt:
                    self.returned = False
                    self.say("Interrupted during the hand-back: give NINA #1 "
                             "its camera back by hand.")
        try:
            self.after = self.d.count()
        except Exception:  # noqa: BLE001
            pass
        return self.summary(status, self.reason)

    def summary(self, status: str, reason: str) -> dict:
        tried = len(self.rows)
        added = sum(1 for r in self.rows if r.get("added") is True)
        solved = sum(1 for r in self.rows if r.get("solved"))
        eye = sum(1 for r in self.rows if "operator saw" in str(r.get("note") or ""))
        s = {"status": status, "reason": reason, "attempted": tried,
             "solved": solved, "added_by_file": added, "added_by_eye": eye,
             "failed": tried - solved, "seconds": round(self.d.mono() - self.t0),
             "samples_before": self.before.get("total"),
             "samples_after": (self.after or {}).get("total"),
             "camera_returned": self.returned, "csv": self.o.csv_path,
             "rows": self.rows}
        self.say("")
        self.say("Summary")
        self.say(f"  result: {status} ({reason})")
        self.say(f"  points attempted {tried}, solved {solved}, failed "
                 f"{tried - solved}; samples confirmed in the files {added}"
                 + (f", seen by eye {eye}" if eye else ""))
        self.say(f"  time {_fmt_min(s['seconds'])}; TPoint input data "
                 f"{s['samples_before']} -> {s['samples_after']} sample(s)")
        if self.returned is not None:
            self.say("  camera back on NINA #1: " + ("yes" if self.returned else
                                                     "NO, fix by hand"))
        if self.o.csv_path and self.rows:
            self.say(f"  log: {self.o.csv_path}")
        if solved:
            self.say("  Next: in TPoint, refit the Super Model by hand, check "
                     "the RMS, save, and Apply pointing corrections / ProTrack "
                     "as usual; then photonscript tpoint-stats. Nothing was "
                     "armed: arming tonight is a separate click.")
        return s


def _copy_file(src: str, dst: str) -> None:
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def run_loop(config, points: list[dict], deps: Deps, opts: Opts) -> dict:
    return LoopRun(config, points, deps, opts).run()


def live_deps(config, scheduler_url: str, ask=input, say=print) -> Deps:
    """The real adapters (TheSky TCP, NINA #1, the scheduler, TPoint's
    files, Pushover)."""
    from photonscript.scheduler import tpoint_files as tf
    from photonscript.telescope_agent.thesky_client import client_from_config
    sky = client_from_config(config)
    sky.timeout = max(float(sky.timeout), 60.0)   # AutoMap waits on Image Link
    nina = NinaLink(str(getattr(config, "nina_base_url", "")
                        or "http://localhost:1888/v2/api"))

    def notify(msg: str) -> None:
        from photonscript.shared.supervisor import pushover_notifier
        pushover_notifier(config)("TPoint autocal", msg, 1)

    return Deps(sky=TheSkyLink(sky), nina=nina, sched=SchedulerLink(scheduler_url),
                count=lambda: sample_count(config),
                list_files=lambda: tf.list_files(config), ask=ask, say=say,
                notify=notify)


def csv_path(config, now: datetime | None = None) -> str:
    try:
        from photonscript.shared.localtime import night_base_date
        night = night_base_date(config, now).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        night = (now or datetime.utcnow()).strftime("%Y-%m-%d")
    return str(Path(getattr(config, "data_dir", ".")) / "runs"
               / f"{night}_tpoint_autocal.csv")


def dark_end(config, now: datetime | None = None) -> datetime | None:
    """Astronomical dawn of the night in progress (None if unknown)."""
    try:
        from photonscript.shared.localtime import night_window
        _b, tw = night_window(config, now)
        e = tw.get("astro_dark_end")
        return e.replace(tzinfo=None) if e else None
    except Exception:  # noqa: BLE001
        return None
