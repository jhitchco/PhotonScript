"""TheSky64 TCP client: read-only status reads (PS-104).

Why this exists: PhotonScript reaches the Paramount MX through NINA's ASCOM
pass-through (TheSky's ASCOM connector), which does NOT expose TPoint, Image
Link or the camera add-on. TheSky64 also runs a "TheSky TCP Server" (default
port 3040) that executes JavaScript sent over the socket and returns the value
of the script's `Out` variable, followed by ``|`` and an error string.

STATUS: report only. ``ping`` is used by the PS-92 fail alert (behind
`thesky_enabled`); the PS-104 TheSky / TPoint audit (scheduler.thesky_audit)
uses the read methods below. Every script lives in READ_ONLY_JS (or comes
from imagelink_script) and a test greps all of them for a denylist: nothing
here connects, unparks, parks, slews, syncs, jogs, changes tracking, takes
an image or writes a TheSky setting.

PS-104 removed two latent traps (neither had a caller):
- get_mount_status used sky6RASCOMTele.Connect(), which may unpark the mount
  (Bisque documents a separate ConnectAndDoNotUnpark). It now reads
  IsConnected and reads the position only when already connected.
- set_protrack used DoCommandStr, which is not in Bisque's sky6RASCOMTele
  reference. Removed.

PS-138: the TPoint / ProTrack state is read live (tpoint_flags) instead of
trusted from the manual record: on 2026-10-05 the audit passed "ProTrack on"
from the record while TheSky's ProTrack tab was unticked and greyed. Bisque's
published scripting reference has no TPoint / ProTrack property that we
could confirm offline, so tpoint_flags tries CANDIDATE property names, one
key each, every read in its own try: a name this build lacks reads "?ERR"
(None) and the audit row reads unknown ("verify by eye"), never a pass. The
on-site check script prints every candidate, so the real names can be
confirmed (or fixed here) on the site's build.
"""

from __future__ import annotations

import logging
import re
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
            return self.run_script(READ_ONLY_JS["ping"]) == "ok"
        except TheSkyError as e:
            logger.debug("TheSky ping failed: %s", e)
            return False

    # --- documented reads (never connect) ------------------------------------
    def get_mount_status(self) -> dict:
        """RA (hours), Dec (deg) and tracking via sky6RASCOMTele, without
        connecting: the script reads IsConnected first and reads the position
        only when the mount is already connected. {"connected": False, ...}
        otherwise."""
        raw = self.run_script(READ_ONLY_JS["mount_status"])
        try:
            conn, ra_h, dec_d, trk = raw.split(",")
        except ValueError as e:
            raise TheSkyError(f"unexpected status reply: {raw!r}") from e
        if not _truthy(conn):
            return {"connected": False, "ra_hours": None, "dec_deg": None,
                    "tracking": None}
        try:
            return {"connected": True, "ra_hours": float(ra_h),
                    "dec_deg": float(dec_d), "tracking": _truthy(trk)}
        except ValueError as e:
            raise TheSkyError(f"unexpected status reply: {raw!r}") from e

    def _kv(self, name: str) -> dict:
        return parse_kv(self.run_script(READ_ONLY_JS[name]))

    def selected_hardware(self) -> dict:
        """SelectedHardware: camera / mount / filter wheel / focuser /
        autoguider model names as TheSky shows them."""
        return self._kv("selected_hardware")

    def mount_flags(self) -> dict:
        """IsConnected; only when connected IsParked, IsTracking, the
        position and LastSlewError. Never Connect()."""
        return self._kv("mount_flags")

    def site(self) -> dict:
        """sky6StarChart.DocumentProperty 0 to 5 and 9 (latitude, longitude,
        time zone, elevation m, DST index, use computer clock, JD now) and
        sky6Utils' local sidereal time (PS-120)."""
        return self._kv("site")

    def ails(self) -> dict:
        """AutomatedImageLinkSettings (the Automated Pointing Calibration
        Run's Image Link fields; binning is not among them)."""
        return self._kv("ails")

    def camera_flags(self) -> dict:
        """ccdsoftCamera Status, BinX, BinY, AutoSavePath, ImageReduction.
        Never ccdsoftCamera.Connect(): that would try to take the AP26MC
        from NINA #1."""
        return self._kv("camera_flags")

    def tpoint_flags(self) -> dict:
        """PS-138: TPoint "Apply pointing corrections", model points / RMS,
        the IH / ID index terms and ProTrack "Activate ProTrack" / "Enable
        tracking adjustments", from candidate property names (see the module
        doc). Property reads only."""
        return self._kv("tpoint_flags")

    def slew_state(self) -> dict:
        """PS-167: IsConnected; only when connected IsSlewComplete,
        IsTracking, IsParked, the position and LastSlewError."""
        return self._kv("slew_state")

    def version(self) -> dict:
        return self._kv("version")

    def allsky_flags(self) -> dict:
        """All Sky Image Link flags through DoCommand 12 / 13 in their read
        form (empty argument). Called only with thesky_audit_allsky_read,
        after the on-site check confirmed the read form changes nothing."""
        return self._kv("allsky_flags")

    def imagelink_file(self, path: str, scale: float) -> dict:
        """TheSky's own Image Link on a FITS file (a copy PhotonScript made):
        sets the scripted ImageLink object's inputs (file, scale), executes,
        reads ImageLinkResults. No camera, no mount, no sync."""
        return parse_kv(self.run_script(imagelink_script(path, scale)))


# --------------------------------------------------------------------------
# Every script the client sends. Each one only READS TheSky state (the Image
# Link self-test sets the solver's own inputs, see IMAGELINK_INPUTS).
# Replies are "k=v;k=v": s() strips | ; = and newlines from values (a | would
# end TheSky's reply early) and g() wraps each read in a try, so a property a
# build lacks reads "?ERR" instead of failing the whole script.
# --------------------------------------------------------------------------

_JS_HELPERS = ("var Out;"
               "function s(v){return String(v).replace(/[|;=\\r\\n]/g,'_');}"
               "function g(f){try{return s(f());}catch(e){return '?ERR';}}")


def _js_kv(pairs: list[tuple[str, str]], pre: str = "") -> str:
    body = " + ';' + ".join(f"'{k}=' + g(function(){{return {expr};}})"
                            for k, expr in pairs)
    return _JS_HELPERS + pre + "Out = " + body + ";"


def _doc(n: int) -> str:
    return f"(sky6StarChart.DocumentProperty({n}), sky6StarChart.DocPropOut)"


_MOUNT_PRE = "var c = false; try { c = sky6RASCOMTele.IsConnected; } catch (e) {}"

# name -> ([(key, JS expression)], prelude). The TCP reads and the on-site
# check script (onsite_script) are both built from this one list.
READ_PAIRS: dict[str, tuple[list[tuple[str, str]], str]] = {
    "version": ([("version", "Application.version"),
                 ("build", "Application.build")], ""),
    "selected_hardware": ([
        ("camera", "SelectedHardware.cameraModel"),
        ("mount", "SelectedHardware.mountModel"),
        ("filter_wheel", "SelectedHardware.filterWheelModel"),
        ("focuser", "SelectedHardware.focuserModel"),
        ("autoguider", "SelectedHardware.autoguiderCameraModel")], ""),
    "mount_flags": ([
        ("connected", "c"),
        ("parked", "c ? sky6RASCOMTele.IsParked() : ''"),
        ("tracking", "c ? sky6RASCOMTele.IsTracking : ''"),
        ("ra_h", "c ? (sky6RASCOMTele.GetRaDec(), sky6RASCOMTele.dRa) : ''"),
        ("dec_d", "c ? sky6RASCOMTele.dDec : ''"),
        ("last_slew_error", "c ? sky6RASCOMTele.LastSlewError : ''")], _MOUNT_PRE),
    "site": ([
        ("latitude", _doc(0)), ("longitude", _doc(1)), ("time_zone", _doc(2)),
        ("elevation_m", _doc(3)), ("dst_index", _doc(4)),
        ("use_computer_clock", _doc(5)), ("jd_now", _doc(9)),
        # PS-120: TheSky's own local sidereal time (hours), read right after
        # its Julian date: the E / W verdict for the longitude (the
        # DocumentProperty(1) sign reads the same for 109 E and 109 W)
        ("lst_h", "(sky6Utils.ComputeLocalSiderealTime(), sky6Utils.dOut0)")], ""),
    "ails": ([
        ("image_scale", "AutomatedImageLinkSettings.imageScale"),
        ("position_angle", "AutomatedImageLinkSettings.positionAngle"),
        ("exposure_s", "AutomatedImageLinkSettings.exposureTimeAILS"),
        ("fovs", "AutomatedImageLinkSettings.fovsToSearch"),
        ("retries", "AutomatedImageLinkSettings.retries"),
        ("filter", "AutomatedImageLinkSettings.filterNameAILS")], ""),
    "camera_flags": ([
        ("status", "ccdsoftCamera.Status"),
        ("bin_x", "ccdsoftCamera.BinX"),
        ("bin_y", "ccdsoftCamera.BinY"),
        ("autosave_path", "ccdsoftCamera.AutoSavePath"),
        ("autosave_on", "ccdsoftCamera.AutoSaveOn"),
        ("image_reduction", "ccdsoftCamera.ImageReduction")], ""),
    # PS-138: CANDIDATE names (not confirmed on build 14139; the on-site
    # check prints each). Property reads only, no method call: a name this
    # build lacks reads ?ERR, a method reads as its source text (dropped by
    # parse_kv), and the audit then says unknown / verify by eye.
    "tpoint_flags": ([
        ("apply_corrections", "TPoint.ApplyPointingCorrections"),
        ("points", "TPoint.NumberOfPoints"),
        ("rms_arcsec", "TPoint.SkyRMS"),
        ("ih_arcsec", "TPoint.IH"),
        ("id_arcsec", "TPoint.ID"),
        ("protrack_active", "TPoint.ProTrackActive"),
        ("protrack_active_tele", "sky6RASCOMTele.ProTrack"),
        ("protrack_adjustments", "TPoint.EnableTrackingAdjustments")], ""),
    # PS-167: is TheSky slewing (or stuck in a slew)? guide-recover reads it
    # twice a few seconds apart. Property reads only, never Connect().
    "slew_state": ([
        ("connected", "c"),
        ("slew_complete", "c ? sky6RASCOMTele.IsSlewComplete : ''"),
        ("tracking", "c ? sky6RASCOMTele.IsTracking : ''"),
        ("parked", "c ? sky6RASCOMTele.IsParked() : ''"),
        ("ra_h", "c ? (sky6RASCOMTele.GetRaDec(), sky6RASCOMTele.dRa) : ''"),
        ("dec_d", "c ? sky6RASCOMTele.dDec : ''"),
        ("last_slew_error", "c ? sky6RASCOMTele.LastSlewError : ''")], _MOUNT_PRE),
    # read form only (empty argument); behind thesky_audit_allsky_read
    "allsky_flags": ([
        ("allsky_scripted",
         "(sky6RASCOMTele.DoCommand(12, ''), sky6RASCOMTele.DoCommandOutput)"),
        ("allsky_automated",
         "(sky6RASCOMTele.DoCommand(13, ''), sky6RASCOMTele.DoCommandOutput)")],
        _MOUNT_PRE),
}

READ_ONLY_JS: dict[str, str] = {
    "ping": "var Out; Out = 'ok';",
    "mount_status": (
        "var Out; var c = sky6RASCOMTele.IsConnected;"
        "if (c) { sky6RASCOMTele.GetRaDec();"
        "Out = '1,' + sky6RASCOMTele.dRa + ',' + sky6RASCOMTele.dDec + ',' + "
        "sky6RASCOMTele.IsTracking; } else { Out = '0,,,'; }"),
    **{name: _js_kv(pairs, pre) for name, (pairs, pre) in READ_PAIRS.items()},
}


def onsite_script() -> str:
    """The read-only script for TheSky's Tools > Run Java Script window (the
    2-minute on-site check, MAINTENANCE.md): every read the audit makes, one
    line each, so the property names can be confirmed on the site's build.
    The All Sky DoCommand reads are left out on purpose: they are tried by
    hand (see MAINTENANCE.md) before thesky_audit_allsky_read is turned on."""
    lines = ["var lines = [];",
             "function g(f){try{return String(f());}catch(e){return '?ERR ' + e;}}"]
    for name, (pairs, pre) in READ_PAIRS.items():
        if name == "allsky_flags":
            continue
        if pre:
            lines.append(pre)
        for k, expr in pairs:
            lines.append(f"lines.push('{name}.{k} = ' + g(function(){{return {expr};}}));")
    lines += ["var Out = lines.join('\\n');",
              "try { RunJavaScriptOutput.writeLine(Out); } catch (e) {}"]
    return "\n".join(lines) + "\n"

# The only TheSky properties any script may assign: the scripted Image
# Link's own inputs (which file, which scale), for the self-test.
IMAGELINK_INPUTS = ("ImageLink.pathToFITS", "ImageLink.scale",
                    "ImageLink.unknownScale")

_SAFE_PATH = re.compile(r"^[A-Za-z0-9 _.:\\/()-]+$")


def imagelink_pre(path: str, scale: float) -> str:
    """The Image Link prelude for one file: set the scripted ImageLink's
    inputs (IMAGELINK_INPUTS only) and execute, catching the error into
    `err`. Refuses a path with characters that could break out of the JS
    string, and an implausible scale. Shared with PS-171 tpoint_sample."""
    p = str(path)
    if not p.isascii() or not _SAFE_PATH.match(p):
        raise TheSkyError(f"refusing an unusual path for Image Link: {p!r}")
    p = p.replace("\\", "/")
    sc = float(scale)
    if not 0.01 < sc < 100:
        raise TheSkyError(f"implausible Image Link scale {sc}")
    return (f"ImageLink.pathToFITS = '{p}';"
            f"ImageLink.scale = {sc:.5f};"
            "ImageLink.unknownScale = 0;"
            "var err = '';"
            "try { ImageLink.execute(); } catch (e) { err = String(e.message || e); }")


def imagelink_script(path: str, scale: float) -> str:
    """The Image Link self-test script for one file (imagelink_pre, then
    the ImageLinkResults reads)."""
    pre = imagelink_pre(path, scale)
    return _js_kv([
        ("exec_error", "err"),
        ("succeeded", "ImageLinkResults.succeeded"),
        ("error_code", "ImageLinkResults.errorCode"),
        ("error_text", "ImageLinkResults.errorText"),
        ("image_scale", "ImageLinkResults.imageScale"),
        ("position_angle", "ImageLinkResults.imagePositionAngle"),
        ("mirrored", "ImageLinkResults.imageIsMirrored"),
        ("image_stars", "ImageLinkResults.imageStarCount"),
        ("solution_rms", "ImageLinkResults.solutionRMS"),
        ("solution_stars", "ImageLinkResults.solutionStarCount"),
        ("catalog_stars", "ImageLinkResults.catalogStarCount")], pre=pre)


def parse_kv(raw: str) -> dict:
    """'a=1;b=x' -> {'a': '1', 'b': 'x'}. '?ERR' (property missing on this
    build), 'undefined', '' and (PS-138) a method's source text or an
    "[object ...]" (a candidate name that is not a value) read as None."""
    out: dict = {}
    for part in (raw or "").split(";"):
        k, sep, v = part.partition("=")
        if not sep or not k.strip():
            continue
        v = v.strip()
        if v in ("", "?ERR", "undefined", "null") or v.startswith(("function", "[object")):
            v = None
        out[k.strip()] = v
    return out


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "-1")


def client_from_config(config) -> "TheSkyClient":
    return TheSkyClient(getattr(config, "thesky_tcp_host", "localhost"),
                        int(getattr(config, "thesky_tcp_port", 3040)))
