"""PS-138 follow-up: "One TheSky running". On 2026-10-05 the scope PC ran an
older TheSkyX next to TheSky64 10.5; NINA's mount ("Driver for telescope
connected through TheSky") most likely went through TheSkyX, bypassing the
TheSky64 TPoint model (first slew 1 deg 14' off, the audit over TCP 3040
read the mount not connected). The check lists the Bisque sky apps, the TCP
3040 listener and the driver's target. All process lists and registry
contents here are fakes; nothing is ever killed or started."""
from collections import namedtuple
from datetime import datetime
from pathlib import Path

from photonscript.scheduler import guiding_attention as ga
from photonscript.scheduler import thesky_audit as ta
from photonscript.scheduler import thesky_procs as tp
from photonscript.shared.config import PhotonScriptConfig

X_EXE = r"C:\Program Files (x86)\Software Bisque\TheSkyX Professional Edition\TheSkyX.exe"
S64_EXE = r"C:\Program Files (x86)\Software Bisque\TheSky64\TheSky64.exe"
PROCS = [{"pid": 4, "name": "System", "exe": None},
         {"pid": 1200, "name": "TheSkyX.exe", "exe": X_EXE, "version": "10.5.0.12000"},
         {"pid": 3300, "name": "TheSky64.exe", "exe": S64_EXE, "version": "10.5.0.14139"},
         {"pid": 77, "name": "NINA.exe", "exe": r"C:\Program Files\NINA\NINA.exe"},
         {"pid": 88, "name": "BisqueHelper.exe",
          "exe": r"C:\Program Files (x86)\Software Bisque\Common\BisqueHelper.exe"}]
DRV_X = {"available": True, "target": "TheSkyX", "via": "COM registration",
         "note": "TheSkyX (COM registration)", "candidates": [], "keys": []}


def _cfg(**kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    return PhotonScriptConfig(_env_file=None, **kw)


def _row(sc, cfg=None):
    cfg = cfg or _cfg()
    obs = {s: {} for s in ta.SOURCES}
    obs["thesky_procs"] = sc
    if sc.get("ok"):
        obs["processes"] = {"one_thesky": sc.get("count")}
    res = ta.evaluate(ta.load_desired(cfg), obs, cfg)
    return next(r for r in res["rows"] if r["id"] == "one_thesky")


# --------------------------------------------------------------------------
# thesky_procs
# --------------------------------------------------------------------------

def test_app_kind_and_sky_app_filter():
    assert tp.app_kind("TheSkyX.exe", X_EXE) == "TheSkyX"
    assert tp.app_kind("TheSky64.exe", S64_EXE) == "TheSky64"
    # an older exe name inside the TheSky64 folder is still TheSky64
    assert tp.app_kind("TheSkyX.exe", S64_EXE.replace("TheSky64.exe", "TheSkyX.exe")) == "TheSky64"
    assert tp.app_kind("TheSky.exe") == "TheSky"
    assert tp.app_kind("NINA.exe", r"C:\NINA\NINA.exe") is None
    assert tp.is_sky_app("TheSkyX.exe") and not tp.is_sky_app("BisqueHelper.exe", PROCS[4]["exe"])


def test_scan_two_apps_tcp_owner_and_others():
    sc = tp.scan(_cfg(), procs=PROCS, listener=3300, driver=DRV_X)
    assert sc["ok"] and sc["count"] == 2
    assert [a["kind"] for a in sc["apps"]] == ["TheSkyX", "TheSky64"]
    assert sc["tcp_kind"] == "TheSky64" and "pid 3300" in sc["tcp_owner"]
    assert [o["name"] for o in sc["others"]] == ["BisqueHelper.exe"]
    assert "TheSkyX 10.5.0.12000 (pid 1200)" in sc["text"]


def test_scan_listener_not_a_thesky_and_none():
    sc = tp.scan(_cfg(), procs=PROCS[:1], listener=4, driver=DRV_X)
    assert sc["count"] == 0 and sc["tcp_owner"] == "pid 4 (not a TheSky)"
    sc = tp.scan(_cfg(), procs=[], listener=None, driver=DRV_X)
    assert sc["tcp_owner"] is None and sc["text"] == "none"


class _PsutilFake:
    CONN = namedtuple("CONN", "status laddr pid")
    ADDR = namedtuple("ADDR", "ip port")

    class _P:
        def __init__(self, info):
            self.info = info

    def process_iter(self, attrs):
        return [self._P({"pid": 1200, "name": "TheSkyX.exe", "exe": X_EXE}),
                self._P({"pid": 9, "name": "svchost.exe", "exe": None})]

    def net_connections(self, kind):
        return [self.CONN("ESTABLISHED", self.ADDR("127.0.0.1", 3040), 5),
                self.CONN("LISTEN", self.ADDR("0.0.0.0", 3040), 1200)]


def test_psutil_paths(monkeypatch):
    monkeypatch.setattr(tp, "_psutil", lambda: _PsutilFake())
    procs, how = tp.list_processes()
    assert how == "psutil" and procs[0]["exe"] == X_EXE
    assert tp.tcp_listener(3040) == (1200, "psutil")
    assert tp.tcp_listener(3041) == (None, "psutil")


TASKLIST = ('"System","4","Services","0","144 K"\r\n'
            '"TheSkyX.exe","1200","Console","1","450,000 K"\r\n'
            '"TheSky64.exe","3300","Console","1","900,000 K"\r\n')
NETSTAT = ("\r\nActive Connections\r\n\r\n  Proto  Local Address  Foreign Address  State  PID\r\n"
           "  TCP    0.0.0.0:135     0.0.0.0:0     LISTENING     1000\r\n"
           "  TCP    0.0.0.0:3040    0.0.0.0:0     LISTENING     3300\r\n"
           "  TCP    127.0.0.1:3040  127.0.0.1:50000  ESTABLISHED  77\r\n")


def test_tasklist_and_netstat_fallbacks(monkeypatch):
    calls = []

    def run(args, timeout=15.0):
        calls.append(args[0])
        return TASKLIST if args[0] == "tasklist" else NETSTAT
    monkeypatch.setattr(tp, "_psutil", lambda: None)
    monkeypatch.setattr(tp, "_run", run)
    procs, how = tp.list_processes()
    assert how == "tasklist" and [p["pid"] for p in procs] == [4, 1200, 3300]
    assert tp.tcp_listener(3040) == (3300, "netstat")
    sc = tp.scan(_cfg(), driver=DRV_X)
    assert sc["count"] == 2 and sc["tcp_kind"] == "TheSky64"
    # read-only commands only
    assert set(calls) <= {"tasklist", "netstat"}


class _Key:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RegFake:
    """A read-only fake winreg over {(root, path): {"values": {}, "keys": []}}."""
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, HKEY_CLASSES_ROOT = "HKCU", "HKLM", "HKCR"
    KEY_READ = 0x20019

    def __init__(self, tree, hkcr_names=()):
        self.tree = {(r, p.lower()): v for (r, p), v in tree.items()}
        self.hkcr_names = list(hkcr_names)
        self.access = []

    def OpenKey(self, root, path, reserved=0, access=KEY_READ):
        self.access.append(access)
        if isinstance(root, _Key):
            rt, base = root.path
            full = f"{base}\\{path}"
        else:
            rt, full = root, path
        if (rt, full.lower()) not in self.tree:
            raise OSError("no key")
        return _Key((rt, full))

    def _node(self, key):
        if key == "HKCR":
            return {"values": {}, "keys": self.hkcr_names}
        return self.tree[(key.path[0], key.path[1].lower())]

    def EnumValue(self, key, i):
        vals = list(self._node(key).get("values", {}).items())
        if i >= len(vals):
            raise OSError("end")
        return vals[i][0], vals[i][1], 1

    def EnumKey(self, key, i):
        ks = self._node(key).get("keys", [])
        if i >= len(ks):
            raise OSError("end")
        return ks[i]

    def QueryValueEx(self, key, name):
        vals = self._node(key).get("values", {})
        if name not in vals:
            raise OSError("no value")
        return vals[name], 1


DRV = r"Software\WOW6432Node\ASCOM\Telescope Drivers\ASCOM.SoftwareBisque.Telescope"


def test_driver_info_from_com_registration(monkeypatch):
    clsid = "{11111111-2222-3333-4444-555555555555}"
    tree = {("HKLM", DRV): {"values": {"": "Driver for telescope connected through TheSky"},
                            "keys": []},
            ("HKCR", r"TheSkyX.Application\CLSID"): {"values": {"": clsid}},
            ("HKCR", rf"WOW6432Node\CLSID\{clsid}\LocalServer32"): {"values": {"": f'"{X_EXE}"'}}}
    reg = _RegFake(tree, hkcr_names=["AcroPDF", "TheSkyX.Application", "zzz"])
    monkeypatch.setattr(tp, "_winreg", lambda: reg)
    d = tp.driver_info()
    assert d["target"] == "TheSkyX" and d["via"] == "COM registration"
    assert d["com"][0]["server"] == X_EXE
    assert any("ASCOM.SoftwareBisque.Telescope" in k for k in d["keys"])
    assert set(reg.access) == {reg.KEY_READ}


def test_driver_info_from_profile_value_and_unknown(monkeypatch):
    tree = {("HKCU", DRV): {"values": {"TheSkyPath": S64_EXE, "Trace": "false"}, "keys": []}}
    monkeypatch.setattr(tp, "_winreg", lambda: _RegFake(tree))
    d = tp.driver_info()
    assert d["target"] == "TheSky64" and d["via"] == "ASCOM profile"
    tree = {("HKLM", DRV): {"values": {"Trace": "false"}, "keys": ["Settings"]},
            ("HKLM", DRV + r"\Settings"): {"values": {"Port": "COM3"}}}
    monkeypatch.setattr(tp, "_winreg", lambda: _RegFake(tree))
    d = tp.driver_info()
    assert d["target"] is None and "no setting names a TheSky" in d["note"]
    assert d["values"]["HKLM\\" + DRV + r"\Settings\Port"] == "COM3"
    monkeypatch.setattr(tp, "_winreg", lambda: None)
    assert tp.driver_info()["available"] is False


def test_module_never_kills_or_starts():
    src = Path(tp.__file__).read_text(encoding="utf-8")
    assert src.isascii() and "\u2014" not in src
    for bad in (".kill(", ".terminate(", "taskkill", "Popen(", "os.startfile", "SetValue",
                "CreateKey", "DeleteKey", "KEY_SET_VALUE", "KEY_WRITE"):
        assert bad not in src, bad


# --------------------------------------------------------------------------
# the audit row
# --------------------------------------------------------------------------

def test_row_fails_on_two_apps_and_names_tcp_owner_and_driver():
    r = _row(tp.scan(_cfg(), procs=PROCS, listener=3300, driver=DRV_X))
    assert r["status"] == "fail" and r["source"] == "processes"
    assert r["current"].startswith("2: TheSkyX 10.5.0.12000 (pid 1200), TheSky64")
    assert "TCP 3040: TheSky64 10.5.0.14139 (pid 3300)" in r["note"]
    assert "driver targets TheSkyX (COM registration)" in r["note"]
    assert "point it at TheSky64" in r["fix"] and "keeps relaunching" in r["fix"]
    assert r["fix"] == ta.TWO_THESKY_FIX


def test_row_one_app_pass_and_driver_mismatch_warn():
    one = [PROCS[2]]
    drv64 = dict(DRV_X, target="TheSky64", via="ASCOM profile")
    r = _row(tp.scan(_cfg(), procs=one, listener=3300, driver=drv64))
    assert r["status"] == "pass" and "driver targets TheSky64" in r["note"]
    r = _row(tp.scan(_cfg(), procs=one, listener=3300, driver=DRV_X))
    assert r["status"] == "warn" and "starts TheSkyX next to it" in r["note"]
    unk = {"available": True, "target": None, "note": "no setting names a TheSky (keys: x)"}
    r = _row(tp.scan(_cfg(), procs=one, listener=3300, driver=unk))
    assert r["status"] == "pass" and "driver targets unknown (no setting names" in r["note"]


def test_row_none_running_unreadable_and_remote():
    r = _row(tp.scan(_cfg(), procs=[], listener=None, driver=DRV_X))
    assert r["status"] == "info" and r["current"] == "none running"
    r = _row({"ok": False, "note": "boom"})
    assert r["status"] == "unknown" and "verify by eye" in r["note"]
    r = _row({"ok": False}, _cfg(thesky_tcp_host="10.0.0.5"))
    assert r["status"] == "info" and "10.0.0.5" in r["note"]


def test_collect_skips_the_scan_for_a_remote_thesky(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(tp, "scan", lambda config: called.append(1) or {"ok": True})
    cfg = _cfg(thesky_tcp_host="10.0.0.5", thesky_tcp_port=1, data_dir=tmp_path,
               nina_logs_dir=str(tmp_path / "n"))
    obs = ta.collect(cfg)
    assert not called and obs["_sources"]["processes"]["ok"] is False
    monkeypatch.setattr(tp, "scan", lambda config: {"ok": True, "count": 2, "text": "a, b",
                                                    "apps": [{}, {}]})
    obs = ta.collect(_cfg(thesky_tcp_port=1, data_dir=tmp_path, nina_logs_dir=str(tmp_path / "n")))
    assert obs["processes"] == {"one_thesky": 2} and obs["thesky_procs"]["count"] == 2


def test_guiding_tab_item_first_with_fix(tmp_path):
    r = _row(tp.scan(_cfg(), procs=PROCS, listener=3300, driver=DRV_X))
    s = ga.build(_cfg(data_dir=tmp_path), datetime(2026, 10, 6, 4, 10), phd2={"rows": []},
                 thesky={"t_utc": "2026-10-06T04:00:00Z", "rows": [r], "pointing": {}})
    it = s["items"][0]
    assert it["id"] == "one_thesky" and it["severity"] == "fail" and it["priority"] == -1
    assert it["setting"] == "Two TheSky apps running" and it["fix"] == ta.TWO_THESKY_FIX
    assert "TCP 3040" in it["detail"] and it["source_text"].startswith("process list")
