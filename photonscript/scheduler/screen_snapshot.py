"""Scope PC desktop screenshot (READ ONLY: remote eyes for Jeremy).

Captures the interactive desktop of the logged-in user (the scheduler runs
as the "PhotonScript" task, logon type Interactive, in jeremy's session) and
encodes it as a JPEG. Used by:

- GET /api/screen/snapshot (routers/screen.py): returns the JPEG; needs the
  remote CLI token (X-PhotonScript-Token);
- `photonscript screen-snapshot` (on the scope PC, also on the remote CLI
  allowlist): writes the JPEG under <data_dir>/screen and prints its path;
  GET /api/screen/files/{name} fetches it;
- `photonscript remote-screen --out file.jpg` (the desktop): calls the
  endpoint with the token from .env.

It never interacts with the desktop: one PIL.ImageGrab.grab call (a GDI
BitBlt copy of the screen), no input, no clicks, no window focus. Guards:
a rate limit shared by every caller (one capture per MIN_INTERVAL_S, a
stamp file under <data_dir>/screen so the CLI child and the server agree),
a size cap (quality, then scale, stepped down until the JPEG fits), and a
refusal in session 0 (a service session has no desktop to capture).
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

MIN_INTERVAL_S = 5.0
MAX_BYTES = 4 * 1024 * 1024
SCALE_MIN, SCALE_MAX, SCALE_DEFAULT = 0.1, 1.0, 0.5
QUALITY_MIN, QUALITY_MAX, QUALITY_DEFAULT = 20, 90, 70
MONITORS = ("all", "primary")
NAME_RE = re.compile(r"^screen_\d{8}T\d{6}(_\d+)?Z\.jpg$")


class SnapshotRefused(Exception):
    """A capture that will not run: HTTP status + message (+ retry_after)."""

    def __init__(self, status: int, detail: str, retry_after: Optional[float] = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


@dataclass
class Shot:
    jpeg: bytes
    width: int
    height: int
    scale: float
    quality: int
    monitor: str
    session_id: Optional[int]
    taken: str


def screen_dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "screen"


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(scale=None, monitor=None, quality=None) -> tuple[float, str, int]:
    """Validated (scale, monitor, quality); raises SnapshotRefused(400)."""
    try:
        s = float(SCALE_DEFAULT if scale in (None, "") else scale)
    except (TypeError, ValueError):
        raise SnapshotRefused(400, "scale must be a number") from None
    if not SCALE_MIN <= s <= SCALE_MAX:
        raise SnapshotRefused(400, f"scale must be {SCALE_MIN}..{SCALE_MAX}")
    m = str(monitor or "all").strip().lower()
    if m not in MONITORS:
        raise SnapshotRefused(400, f"monitor must be one of {', '.join(MONITORS)}")
    try:
        q = int(QUALITY_DEFAULT if quality in (None, "") else quality)
    except (TypeError, ValueError):
        raise SnapshotRefused(400, "quality must be an integer") from None
    if not QUALITY_MIN <= q <= QUALITY_MAX:
        raise SnapshotRefused(400, f"quality must be {QUALITY_MIN}..{QUALITY_MAX}")
    return s, m, q


def session_check(sid: Optional[int]) -> None:
    """Refuse when this process has no interactive desktop."""
    if sid == 0:
        raise SnapshotRefused(409, "this PhotonScript process runs in session 0 "
                                   "(no desktop): a screen snapshot needs the "
                                   "\"PhotonScript\" task with logon type Interactive "
                                   "in jeremy's session (photonscript autostart-check)")


class RateLimit:
    """One capture per interval across processes: an in-process lock plus a
    stamp file (wall time of the last capture) under <data_dir>/screen."""

    def __init__(self, interval_s: float = MIN_INTERVAL_S,
                 clock: Callable[[], float] = time.time):
        self.interval_s = float(interval_s)
        self.clock = clock
        self._lock = threading.Lock()

    def _stamp(self, config) -> Path:
        return screen_dir(config) / "last_capture.json"

    def acquire(self, config) -> None:
        """Take the slot or raise SnapshotRefused(429)."""
        with self._lock:
            now = self.clock()
            p = self._stamp(config)
            last = None
            try:
                last = float(json.loads(p.read_text(encoding="utf-8"))["t"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if last is not None and 0 <= now - last < self.interval_s:
                wait = round(self.interval_s - (now - last), 1)
                raise SnapshotRefused(429, f"one screen snapshot per {self.interval_s:g} s: "
                                           f"try again in {wait:g} s", retry_after=wait)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"t": now}), encoding="utf-8")
            except OSError:
                pass


LIMIT = RateLimit()


def grab_pil(monitor: str):
    """The real capture (Windows: GDI). Read only: nothing is sent to the
    desktop. all_screens spans every monitor."""
    from PIL import ImageGrab
    return ImageGrab.grab(all_screens=(monitor == "all"))


def encode(img, scale: float, quality: int, max_bytes: int = MAX_BYTES) -> tuple[bytes, int, int, float, int]:
    """JPEG bytes under max_bytes: the asked scale / quality first, then
    quality down to QUALITY_MIN, then the scale by 0.7 per step.
    Returns (jpeg, width, height, scale, quality)."""
    from PIL import Image
    if img.mode != "RGB":
        img = img.convert("RGB")
    w0, h0 = img.size
    s, q = scale, quality
    for _ in range(12):
        w, h = max(1, int(w0 * s)), max(1, int(h0 * s))
        im = img if (w, h) == (w0, h0) else img.resize((w, h), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, optimize=True)
        b = buf.getvalue()
        if len(b) <= max_bytes:
            return b, w, h, s, q
        if q > QUALITY_MIN:
            q = max(QUALITY_MIN, q - 20)
        else:
            s = max(0.05, s * 0.7)
    raise SnapshotRefused(507, f"the JPEG stays over {max_bytes} bytes; ask a smaller scale")


def take(config, scale=None, monitor=None, quality=None, *,
         sid_fn: Optional[Callable[[], Optional[int]]] = None,
         grab_fn: Optional[Callable] = None, limit: Optional[RateLimit] = None,
         max_bytes: int = MAX_BYTES) -> Shot:
    """Validate, check the session, take the rate-limit slot, capture, encode."""
    s, m, q = parse_args(scale, monitor, quality)
    if sid_fn is None:
        from photonscript.app_start import session_id as sid_fn
    sid = sid_fn()
    session_check(sid)
    (limit or LIMIT).acquire(config)
    try:
        img = (grab_fn or grab_pil)(m)
    except Exception as e:  # noqa: BLE001
        raise SnapshotRefused(503, f"screen capture failed ({type(e).__name__}: {e}); "
                                   "is the desktop locked or the session disconnected?")
    jpeg, w, h, s2, q2 = encode(img, s, q, max_bytes)
    return Shot(jpeg=jpeg, width=w, height=h, scale=s2, quality=q2, monitor=m,
                session_id=sid, taken=_now_z())


def save(config, shot: Shot) -> Path:
    """Write the JPEG under <data_dir>/screen (a new name each time; nothing
    is overwritten or deleted)."""
    d = screen_dir(config)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    p = d / f"screen_{stamp}Z.jpg"
    n = 1
    while p.exists():
        p = d / f"screen_{stamp}_{n}Z.jpg"
        n += 1
    p.write_bytes(shot.jpeg)
    return p


def saved_file(config, name: str) -> Optional[Path]:
    """A saved snapshot by its bare file name (no paths), or None."""
    if not NAME_RE.match(str(name or "")):
        return None
    p = screen_dir(config) / name
    return p if p.is_file() else None


def meta(shot: Shot, path: Optional[Path] = None) -> dict:
    out = {"width": shot.width, "height": shot.height, "scale": shot.scale,
           "quality": shot.quality, "monitor": shot.monitor, "bytes": len(shot.jpeg),
           "session_id": shot.session_id, "taken": shot.taken}
    if path is not None:
        out["path"] = str(path)
        out["name"] = path.name
        out["fetch"] = f"GET /api/screen/files/{path.name}"
    return out
