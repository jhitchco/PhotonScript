"""Experimental TheSky64 TCP client — pointing status and a ProTrack toggle.

Why this exists: PhotonScript reaches the Paramount MX through NINA's ASCOM
pass-through (TheSky's ASCOM connector), which does NOT expose TPoint or
ProTrack. TheSky64 also runs a "TheSky TCP Server" (default port 3040) that
executes JavaScript sent over the socket and returns the value of the script's
`Out` variable, followed by ``|`` and an error string. Talking to that server
lets us read pointing state and — once the exact call is confirmed on-site —
flip ProTrack, the Bisque-native fix for the ~7.8"/min Dec drift.

STATUS: experimental. NOTHING in the nightly flow calls this; it is opt-in
(`thesky_enabled`, off by default). The status reads use documented TheSkyX
script objects and should work as-is. ``set_protrack`` is best-effort and
MARKED so — confirm the ProTrack scripting call against the site's TheSky build
(see the NOTE in that method) before wiring it into automation. Use
``run_script`` as an escape hatch to try the exact API your build exposes.
"""

from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)

# TheSky's TCP server expects the JavaScript wrapped in these markers and
# returns "<Out value>|<error text>" (e.g. "...|No error. Error = 0.").
_HEADER = "/* Java Script */\n/* Socket Start Packet */\n"
_FOOTER = "\n/* Socket End Packet */\n"


class TheSkyError(RuntimeError):
    """A TheSky connection failure or a script error reported by TheSky."""


class TheSkyClient:
    def __init__(self, host: str = "localhost", port: int = 3040,
                 timeout: float = 10.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    # --- framing / parsing (pure, unit-tested) -------------------------------
    @staticmethod
    def _frame(js: str) -> str:
        return _HEADER + js + _FOOTER

    @staticmethod
    def _parse_reply(raw: str) -> str:
        """Return the script's Out value; raise TheSkyError on a reported error.

        TheSky replies "<out>|<error>"; the error text reads "No error. Error =
        0." on success. Anything else in the error half is treated as a failure.
        """
        out, sep, err = raw.partition("|")
        if sep and err.strip():
            low = err.lower()
            if "error = 0" not in low and "no error" not in low:
                raise TheSkyError(f"TheSky script error: {err.strip()} "
                                  f"(out={out.strip()!r})")
        return out.strip()

    # --- transport -----------------------------------------------------------
    def run_script(self, js: str) -> str:
        """Send a JS snippet (which must assign its result to `Out`) and return
        the parsed Out value. Raises TheSkyError on connection or script error."""
        payload = self._frame(js).encode("ascii", "replace")
        try:
            with socket.create_connection((self.host, self.port),
                                          self.timeout) as s:
                s.settimeout(self.timeout)
                s.sendall(payload)
                chunks = []
                while True:
                    b = s.recv(4096)
                    if not b:
                        break
                    chunks.append(b)
                    if b"|" in b:  # reply terminates with <out>|<error>
                        break
            raw = b"".join(chunks).decode("ascii", "replace")
        except OSError as e:
            raise TheSkyError(
                f"TheSky TCP {self.host}:{self.port}: {e}") from e
        return self._parse_reply(raw)

    def ping(self) -> bool:
        """True if the TCP server answers and runs a trivial script."""
        try:
            return self.run_script("var Out; Out = 'ok';") == "ok"
        except TheSkyError as e:
            logger.debug("TheSky ping failed: %s", e)
            return False

    # --- documented reads ----------------------------------------------------
    def get_mount_status(self) -> dict:
        """RA (hours), Dec (deg), tracking flag via sky6RASCOMTele."""
        js = ("var Out;"
              "sky6RASCOMTele.Connect();"
              "sky6RASCOMTele.GetRaDec();"
              "Out = sky6RASCOMTele.dRa + ',' + sky6RASCOMTele.dDec + ',' + "
              "sky6RASCOMTele.IsTracking;")
        raw = self.run_script(js)
        try:
            ra_h, dec_d, trk = raw.split(",")
            return {"ra_hours": float(ra_h), "dec_deg": float(dec_d),
                    "tracking": bool(int(float(trk)))}
        except ValueError as e:
            raise TheSkyError(f"unexpected status reply: {raw!r}") from e

    # --- experimental write --------------------------------------------------
    def set_protrack(self, on: bool) -> str:
        """EXPERIMENTAL: enable/disable ProTrack.

        NOTE: TPoint/ProTrack is not exposed through one stable, documented
        TheSkyX script property across builds. The call below is a best-effort
        CANDIDATE. Confirm it against the site's TheSky version first — open
        TheSkyX's Script window, run a one-liner, and check the result — before
        wiring this into automation. If it errors, use run_script() to try the
        exact API your build exposes and update this method. It intentionally
        raises TheSkyError (via run_script) so a wrong call fails loudly rather
        than silently pretending ProTrack changed.
        """
        val = "true" if on else "false"
        js = (f"var Out; sky6RASCOMTele.Connect();"
              f"sky6RASCOMTele.DoCommandStr(\"ProTrack\", \"{val}\");"
              f"Out = \"protrack={val}\";")
        return self.run_script(js)


def client_from_config(config) -> "TheSkyClient":
    return TheSkyClient(getattr(config, "thesky_tcp_host", "localhost"),
                        int(getattr(config, "thesky_tcp_port", 3040)))
