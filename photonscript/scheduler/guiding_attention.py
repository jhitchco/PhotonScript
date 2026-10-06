"""The Guiding tab's "What to change" list (PS-119).

Why: the Guiding tab (PS-103 / PS-104) shows every row of the PHD2 settings
audit and the TheSky / TPoint audit in full; the few fails and warns that
matter were buried among passes and unknowns. This module gathers every
fail, then every warn, from the stored records only (it never runs an audit
and never talks to PHD2, NINA or TheSky):

    PHD2 audit        <data_dir>/phd2_audit/latest.json (PS-89)
    TheSky audit      <data_dir>/thesky_audit/latest.json (PS-104), with the
                      TPoint rebuild flag and the first-slew rows
    calibration       phd2_calibration.summary() (PS-93): none / FAIL / stale
    self-test         phd2_store.selftest_results() (PS-92): none in
                      SELFTEST_NIGHTS nights / FAIL / WARN
    guard             guide_hotpix.status() and tonight's guard episodes (PS-91)
    tuner             phd2_tuning.summary() (PS-90): a clipped or 8-bit guide
                      star only (the gain advice is the audit's gain row)
    NINA #2 mount     nina2_mount_check.tonight() (PS-139): tonight's NINA #2
                      sequence holds slew / center / park instructions (fail
                      inside a loop, warn outside)

Each item: severity, section + anchor, setting, current -> desired (with the
reading's source and age), a one-sentence fix, where to do it, and the
audit row id when the Guiding tab can Apply it live.

Order: fail before warn; within each, PRIORITY (lower first):
   -1  first                TheSky's site longitude (a wrong one invalidates
                            every slew and the TPoint model), two TheSky
                            apps running (PS-138: NINA's mount may bypass
                            the TheSky64 model)
    0  ProTrack OFF         PS-138: TheSky's ProTrack read off (unguided
                            nights trail without it), with the fix
    0  guiding tonight      PHD2 settings PHD2 guides with (camera, guiding,
                            algorithms, calibration, darks, focal length),
                            the PS-93 calibration, a failed self-test
    1  set up tonight       NINA, the mount driver, TheSky's link / mount /
                            site / camera add-on / Image Link settings, the
                            hot-pixel map, guard episodes, no recent self-test
    2  when convenient      the TPoint model, catalogs, pointing trend, ASTAP
                            image truth, the guide-star tuner
then the section order on the page.

Sources: PHD2 API (live) beats the PHD2 profile in the registry (read live
at the audit) beats the guide log (dated). Stale readings: a PHD2 audit row
whose value came from a cached reading
(the newest guide log or Guiding Assistant, CACHED_SOURCES) is "stale: may
already be fixed" when that reading is older than the last PHD2
ConfigurationChange, older than STALE_HOURS (which also covers "older than
the audit by a day"), or its time is unknown. Stale fails and warns move to
their own group with STALE_HINT (bit depth read from a guide log that
predates the switch to 16-bit was the case that asked for this).

Unknown rows become "Not checked yet" groups by reason, each with a count
and the one action that clears it (NOT_CHECKED).

build() never raises; annotate_phd2() / annotate_thesky() add the source
text, stale flag and live-apply flag to an audit for the Guiding tab tables.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

FAIL, WARN, PASS, UNKNOWN, INFO = "fail", "warn", "pass", "unknown", "info"
SEV_RANK = {FAIL: 0, WARN: 1}
STALE_HOURS = 12.0
SELFTEST_NIGHTS = 30
LIVE_HOURS = 2.0           # a live source read this recently says "live"
CACHED_SOURCES = ("log", "ga")
STALE_HINT = ("clears after the next guiding session writes a new log, "
              "or after the reg export")

# section anchor on /guiding -> nav label (page order)
SECTIONS = (("liveSec", "Live"), ("auditSec", "Settings audit"),
            ("selftestSec", "Pulse self-test"), ("calSec", "Calibration"),
            ("guardSec", "Guide guard"), ("tuneSec", "Guide star"),
            ("glogSec", "Tonight's guide log"), ("tpointSec", "TheSky / TPoint"))
SECTION_ORDER = {a: i for i, (a, _n) in enumerate(SECTIONS)}

PRIORITY_LABEL = {-1: "first: invalidates pointing and TPoint", 0: "guiding tonight",
                  1: "set up tonight", 2: "when convenient"}
# TheSky rows that go above everything (PS-138: two TheSky apps at once,
# NINA's mount may bypass the TheSky64 model)
FIRST_IDS = ("site_longitude", "one_thesky")
TONIGHT_IDS = ("protrack_on",)       # PS-138: TheSky rows that matter tonight
PHD2_TONIGHT_GROUPS = ("Camera", "Guiding", "Algorithms", "Calibration", "Darks", "Mount")
THESKY_LATER_GROUPS = ("Image truth (ASTAP)", "Catalogs", "TPoint and ProTrack", "Pointing")

SOURCE_NAMES = {
    "api": "PHD2 API", "profile": "PHD2 profile", "log": "guide log",
    "ga": "Guiding Assistant", "nina": "NINA", "ascom": "mount driver",
    "thesky": "TheSky", "file": "dark library files", "calibration": "PS-93 record",
    "thesky-script": "TheSky script", "astap": "ASTAP check",
    "pointing-log": "NINA Center log", "manual": "manual record",
    "processes": "process list"}
LIVE_SOURCES = ("api", "profile", "nina", "ascom", "thesky", "thesky-script", "processes")

# where a profile-only PHD2 row is changed by hand when profile writes are off
PHD2_WHERE = {"bit_depth": "Equipment > camera properties (16-bit)"}

# "Not checked yet": key -> (title, the one action that clears it, anchor)
NOT_CHECKED = {
    "reg_export": ("PHD2 registry names unverified",
                   "Run the reg export on the scope PC (docs/MAINTENANCE.md) so the "
                   "profile values can be read.", "auditSec"),
    "manual_tpoint": ("No manual TPoint record",
                      "Enter the TPoint record on this tab after the TPoint session.",
                      "tsManualForm"),
    "driver_flags": ("Mount driver flags not readable",
                     "Check the Bisque ASCOM driver flags by hand (DirectGuide, "
                     "Can Get Pointing State).", "auditSec"),
    "nina": ("NINA guider settings not found",
             "Open NINA with the RC16 profile (Options > Equipment > Guider), "
             "then Refresh the settings audit.", "auditSec"),
    "astap": ("No ASTAP Image Link check yet",
              "Press ASTAP check now in the TheSky / TPoint section.", "tsImagelink"),
    "by_eye": ("TheSky state not readable by script",
               "Check these by eye in TheSky, and run the on-site check script once so "
               "the property names can be confirmed (MAINTENANCE.md).", "tpointSec"),
    "thesky": ("TheSky not reachable",
               "Refresh the TheSky / TPoint audit with TheSky running (TCP server on).",
               "tpointSec"),
    "phd2": ("PHD2 not reachable",
             "Refresh the settings audit with PHD2 running.", "auditSec"),
    "other": ("No source had a value",
              "Refresh both audits with PHD2, NINA and TheSky running.", "auditSec"),
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _now(now) -> datetime:
    return now or datetime.utcnow()


def _date(t: datetime | None) -> str:
    return t.strftime("%Y-%m-%d") if t else "?"


def _age_text(hours: float) -> str:
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} d"


def _log_time(audit: dict, config) -> datetime | None:
    """When the guide-log reading was taken: the audit's source_times, else
    the guide-log file name in the log source note (older audits)."""
    t = store.parse_z((audit.get("source_times") or {}).get("log"))
    if t:
        return t
    note = str(((audit.get("sources") or {}).get("log") or {}).get("note") or "")
    m = re.search(r"(\d{4}-\d\d-\d\d)_(\d\d)(\d\d)(\d\d)", note)
    if not m:
        return None
    try:
        from photonscript.scheduler import phd2_analysis as an
        loc = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}")
        return an.to_utc(config, loc)
    except Exception:  # noqa: BLE001
        return None


def last_config_change(config, phd2: dict | None = None) -> datetime | None:
    """The last PHD2 ConfigurationChange: the RC16 agent's note, else the
    newest audit that a configuration change triggered."""
    best = None
    try:
        from photonscript.scheduler import phd2_audit as pa
        best = pa.last_config_change(config)
    except Exception:  # noqa: BLE001
        pass
    if phd2 and phd2.get("reason") == "PHD2 configuration change":
        t = store.parse_z(phd2.get("t_utc"))
        if t and (best is None or t > best):
            best = t
    return best


# --------------------------------------------------------------------------
# row annotation (the Guiding tab tables use the same fields)
# --------------------------------------------------------------------------

def _live_text(name: str, audit_t: datetime | None, now: datetime) -> str:
    if audit_t and (now - audit_t).total_seconds() <= LIVE_HOURS * 3600:
        return f"{name}, live"
    return f"{name}, read {_date(audit_t)}"


def annotate_phd2(audit: dict | None, config, now: datetime | None = None,
                  last_change: datetime | None = None) -> dict | None:
    """Add source_text, reading_utc, stale, stale_why, apply_live and
    manual_hint to every PHD2 audit row (in place). Never raises."""
    if not audit:
        return audit
    now = _now(now)
    try:
        audit_t = store.parse_z(audit.get("t_utc"))
        st = audit.get("source_times") or {}
        log_t = _log_time(audit, config)
        ga_t = store.parse_z(st.get("ga"))
        if last_change is None:
            last_change = last_config_change(config, audit)
        autofix = bool(audit.get("autofix"))
        from photonscript.scheduler.phd2_audit import profile_row_writable
        for r in audit.get("rows") or []:
            src = r.get("source")
            name = SOURCE_NAMES.get(src, src or "")
            reading = None
            if src in LIVE_SOURCES:
                text, reading = _live_text(name, audit_t, now), audit_t
            elif src == "log":
                reading = log_t
                text = f"guide log {_date(log_t)}"
            elif src == "ga":
                reading = ga_t
                text = f"Guiding Assistant {_date(ga_t)}"
            elif src == "calibration":
                age = st.get("calibration_age_days")
                text = name + (f", {age:g} d old" if isinstance(age, (int, float)) else "")
            elif src == "file":
                age = st.get("file_age_days")
                text = name + (f", {age:g} d old" if isinstance(age, (int, float)) else "")
            else:
                text = name
            stale, why = False, None
            if src in CACHED_SOURCES:
                if reading is None:
                    stale, why = True, "reading time unknown"
                elif last_change and reading < last_change:
                    stale, why = True, (f"PHD2 settings changed {_date(last_change)}, "
                                        f"after this {name}")
                elif (now - reading).total_seconds() > STALE_HOURS * 3600:
                    stale, why = True, (f"{name} reading is "
                                        f"{_age_text((now - reading).total_seconds() / 3600)} old")
            bad = r.get("status") in (FAIL, WARN)
            live = bool(bad and r.get("target") is not None and (
                r.get("apply") == "api" or (r.get("apply") == "profile" and autofix
                                            and profile_row_writable(r))))
            hint = None
            if bad and not live and r.get("apply") == "profile":
                hint = "set in PHD2: " + PHD2_WHERE.get(r.get("id"), r.get("fix") or "")
            r.update({"source_text": text, "reading_utc": store.iso_z(reading),
                      "stale": stale, "stale_why": why, "apply_live": live,
                      "manual_hint": hint})
    except Exception as e:  # noqa: BLE001
        logger.debug("PHD2 audit annotation failed: %s", e)
    return audit


def annotate_thesky(audit: dict | None, now: datetime | None = None) -> dict | None:
    """Source text (with the reading's date) for every TheSky audit row, in
    place. TheSky rows are report only: apply_live is always False."""
    if not audit:
        return audit
    now = _now(now)
    try:
        audit_t = store.parse_z(audit.get("t_utc"))
        il_t = store.parse_z((audit.get("imagelink") or {}).get("t_utc"))
        man_t = store.parse_z((audit.get("manual") or {}).get("entered_at"))
        for r in audit.get("rows") or []:
            src = r.get("source")
            name = SOURCE_NAMES.get(src, src or "")
            if src in LIVE_SOURCES:
                text = _live_text(name, audit_t, now)
            elif src == "astap":
                text = f"{name} {_date(il_t)}"
            elif src == "manual":
                text = f"{name} {_date(man_t)}"
            elif src == "pointing-log":
                text = f"{name}, {(audit.get('pointing') or {}).get('nights') or 14} nights"
            else:
                text = name
            r.update({"source_text": text, "stale": False, "stale_why": None,
                      "apply_live": False, "manual_hint": None})
    except Exception as e:  # noqa: BLE001
        logger.debug("TheSky audit annotation failed: %s", e)
    return audit


# --------------------------------------------------------------------------
# items
# --------------------------------------------------------------------------

def _where_phd2(r: dict) -> str:
    g, rid = r.get("group"), r.get("id")
    if g == "NINA":
        return "NINA"
    if g == "Mount driver":
        return "TheSky" if rid == "thesky_scripting" else "mount driver"
    if rid == "calibration_record":
        return "PhotonScript"
    if rid == "guide_speed":
        return "TheSky"
    return "PHD2"


def _prio_phd2(r: dict) -> int:
    return 0 if r.get("group") in PHD2_TONIGHT_GROUPS else 1


def _prio_thesky(r: dict) -> int:
    if r.get("id") in FIRST_IDS:
        return -1
    if r.get("id") in TONIGHT_IDS:
        return 0
    return 2 if r.get("group") in THESKY_LATER_GROUPS else 1


def _item(severity, section, anchor, setting, current, desired, fix, where,
          priority, **kw) -> dict:
    out = {"severity": severity, "section": section, "anchor": anchor,
           "row_anchor": kw.pop("row_anchor", None) or anchor,
           "id": kw.pop("id", None), "setting": setting,
           "current": current, "desired": desired, "fix": fix, "where": where,
           "priority": priority, "priority_label": PRIORITY_LABEL.get(priority),
           "apply_id": None, "manual_hint": None, "source_text": None,
           "stale": False, "stale_why": None, "detail": None}
    out.update(kw)
    return out


def _audit_item(r: dict, section: str, anchor: str, prefix: str, where: str,
                priority: int) -> dict:
    return _item(r.get("status"), section, anchor, r.get("label"), r.get("current"),
                 r.get("desired"), r.get("fix"), where, priority,
                 id=r.get("id"), row_anchor=f"{prefix}-{r.get('id')}",
                 apply_id=r.get("id") if r.get("apply_live") else None,
                 manual_hint=r.get("manual_hint"), source_text=r.get("source_text"),
                 stale=bool(r.get("stale")), stale_why=r.get("stale_why"),
                 detail=r.get("note"))


def _calibration_items(config) -> tuple[list[dict], dict]:
    from photonscript.scheduler import phd2_calibration as pc
    c = pc.summary(config)
    sec, a = "PHD2 calibration (PS-93)", "calSec"
    fix = ("PS-93 calibrates on the next guided night (mode auto), or use "
           "Calibrate at the next dispatch.")
    want = "graded PASS, under phd2_cal_max_age_days"
    rec = c.get("record") or {}
    if not c.get("grade"):
        it = _item(FAIL, sec, a, "PHD2 calibration", "none on record", want, fix,
                   "PhotonScript", 0, id="calibration")
    elif c.get("poor"):
        it = _item(FAIL, sec, a, "PHD2 calibration", f"FAIL, {c.get('age_days')} d old",
                   want, fix, "PhotonScript", 0, id="calibration",
                   detail="; ".join(rec.get("reasons") or []) or None)
    elif c.get("stale"):
        it = _item(WARN, sec, a, "PHD2 calibration",
                   f"{c.get('grade')}, {c.get('age_days')} d old", want, fix,
                   "PhotonScript", 0, id="calibration")
    else:
        return [], {PASS: 1}
    return [it], {it["severity"]: 1}


def _selftest_items(config) -> tuple[list[dict], dict]:
    rows = store.selftest_results(config, days=SELFTEST_NIGHTS)
    active = [r for r in rows if r.get("kind", "active") == "active"
              and r.get("verdict") not in ("SKIPPED",)]
    sec, a = "Pulse self-test (PS-92)", "selftestSec"
    run = ("Run self-test now with the scope tracking on a star field and PHD2 not "
           "guiding (or turn on PS_PHD2_SELFTEST_ENABLED).")
    if not active:
        it = _item(WARN, sec, a, "Pulse self-test", f"none in {SELFTEST_NIGHTS} nights",
                   "a recent PASS", run, "PhotonScript", 1, id="selftest")
        return [it], {WARN: 1}
    last = active[-1]
    v = str(last.get("verdict") or "").upper()
    if v in ("FAIL", "WARN"):
        sev = FAIL if v == "FAIL" else WARN
        fix = ("Check the guide cable / ASCOM pulse path and TheSky's autoguide rate, "
               "then run the self-test again." if sev == FAIL else
               "Match TheSky's autoguide rate with PHD2's guide speed, then run the "
               "self-test again.")
        it = _item(sev, sec, a, "Pulse self-test",
                   f"{v} {str(last.get('t_utc') or '')[:10]}", "PASS", fix,
                   "TheSky" if sev == WARN else "mount driver", 0 if sev == FAIL else 1,
                   id="selftest", detail="; ".join(last.get("reasons") or []) or None)
        return [it], {sev: 1}
    return [], {PASS: 1}


def _guard_items(config, night: str) -> tuple[list[dict], dict]:
    from photonscript.telescope_agent import guide_hotpix
    out, counts = [], {}
    sec, a = "Guide guard (PS-91)", "guardSec"
    h = guide_hotpix.status(config)
    if not h.get("exists"):
        out.append(_item(WARN, sec, a, "Guide-camera hot-pixel map", "none",
                         "a map under phd2_hotpix_max_age_days",
                         "Capture hot-pixel map now with the roof closed (it is also "
                         "built automatically before dusk).", "PhotonScript", 1,
                         id="hotpix"))
    elif h.get("stale"):
        out.append(_item(WARN, sec, a, "Guide-camera hot-pixel map",
                         f"{h.get('age_days')} d old", "current",
                         "Capture hot-pixel map now with the roof closed.",
                         "PhotonScript", 1, id="hotpix", detail=h.get("stale")))
    eps = store.guard_episodes(config, night)
    if eps:
        failed = sum(1 for e in eps for r in e.get("recoveries") or []
                     if r.get("ok") is False)
        out.append(_item(WARN, sec, a, "Guide guard episodes tonight",
                         f"{len(eps)} episode(s)" + (f", {failed} failed recovery"
                                                     if failed else ""),
                         "none", "Check the guard table: a lock on a hot pixel needs a "
                         "fresh hot-pixel map and a dark library.", "PHD2", 1,
                         id="guard_episodes"))
    for it in out:
        counts[it["severity"]] = counts.get(it["severity"], 0) + 1
    return out, counts


def _tuner_items(config, night: str, skip_bit8: bool) -> tuple[list[dict], dict]:
    from photonscript.scheduler import phd2_tuning as tn
    t = tn.summary(config, night)
    last = t.get("last") or {}
    out = []
    sec, a = "Guide star tuner (PS-90)", "tuneSec"
    if last.get("bit8") and not skip_bit8:
        out.append(_item(WARN, sec, a, "Guide camera bit depth (tuner)", "8-bit star",
                         "16-bit", "PHD2 Connect Equipment > camera properties: 16-bit.",
                         "PHD2", 2, id="tune_bit8"))
    if last.get("clipped"):
        out.append(_item(WARN, sec, a, "Guide star clipped",
                         f"{last.get('filter') or '?'} at {last.get('exposure_ms')} ms",
                         "peak 60 to 80% of full scale",
                         "Lower the PHD2 exposure or gain (or pick a fainter star).",
                         "PHD2", 2, id="tune_clipped"))
    return out, {WARN: len(out)} if out else {}


def _nina2_mount_items(config, now: datetime) -> tuple[list[dict], dict]:
    """PS-139: NINA #2 holds mount instructions tonight (the record the
    arm / watch check wrote; nothing is read from NINA here)."""
    from photonscript.scheduler.nina2_mount_check import attention_item
    a = attention_item(config, now)
    if not a:
        return [], {}
    it = _item(a["severity"], "NINA #2 mount check (PS-139)", "liveSec",
               a["setting"], a["current"], a["desired"], a["fix"], a["where"],
               0, id="nina2_mount")
    return [it], {it["severity"]: 1}


def _classify_unknown(r: dict, which: str) -> str:
    note = str(r.get("note") or "").lower()
    if "registry name" in note:
        return "reg_export"
    if "driver flag" in note:
        return "driver_flags"
    if which == "thesky":
        if "verify by eye" in note:
            return "by_eye"
        if ("manual" in note or r.get("source") == "manual"
                or r.get("group") in ("Catalogs", "TPoint and ProTrack")):
            return "manual_tpoint"
        if "astap" in note or str(r.get("group") or "").startswith("Image truth") \
                or r.get("id") == "imagelink_selftest":
            return "astap"
        if "thesky-script" in note:
            return "thesky"
    if "nina" in note:
        return "nina"
    if which == "phd2" and ("api:" in note or "phd2 not reachable" in note):
        return "phd2"
    return "other"


def not_checked(phd2: dict | None, thesky: dict | None) -> list[dict]:
    groups: dict[str, list[str]] = {}
    for which, audit in (("phd2", phd2), ("thesky", thesky)):
        for r in (audit or {}).get("rows") or []:
            if r.get("status") == UNKNOWN:
                groups.setdefault(_classify_unknown(r, which), []).append(
                    str(r.get("label") or r.get("id")))
    out = []
    for key, (title, action, anchor) in NOT_CHECKED.items():
        if groups.get(key):
            out.append({"key": key, "title": title, "count": len(groups[key]),
                        "action": action, "anchor": anchor, "rows": groups[key]})
    return out


def _sort_key(it: dict, i: int):
    return (SEV_RANK.get(it["severity"], 9), it["priority"],
            SECTION_ORDER.get(it["anchor"], 99), i)


def _counts(rows) -> dict:
    out = {FAIL: 0, WARN: 0, PASS: 0, UNKNOWN: 0, INFO: 0}
    for r in rows or []:
        s = r.get("status")
        if s in out:
            out[s] += 1
    return out


# --------------------------------------------------------------------------
# the summary
# --------------------------------------------------------------------------

def build(config, now: datetime | None = None, *, phd2: dict | None = None,
          thesky: dict | None = None) -> dict:
    """The "What to change" summary from the cached records. Never raises
    and never runs an audit (phd2= / thesky= pass audits in for tests)."""
    now = _now(now)
    items: list[dict] = []
    problems: list[str] = []
    sections: dict[str, dict] = {}
    srcinfo: dict[str, dict] = {}
    try:
        night = store.night_of(config, now)
    except Exception:  # noqa: BLE001
        night = now.strftime("%Y-%m-%d")

    def guarded(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            problems.append(f"{name}: {type(e).__name__}: {e}")
            return None

    if phd2 is None:
        from photonscript.scheduler import phd2_audit as pa
        phd2 = guarded("PHD2 audit", lambda: pa.load_latest(config))
    if thesky is None:
        from photonscript.scheduler import thesky_audit as ta
        thesky = guarded("TheSky audit", lambda: ta.load_latest(config))

    cal = guarded("calibration", lambda: _calibration_items(config))
    if phd2:
        guarded("PHD2 audit", lambda: annotate_phd2(phd2, config, now))
        for r in phd2.get("rows") or []:
            if r.get("status") not in (FAIL, WARN):
                continue
            if r.get("id") == "calibration_record" and cal is not None:
                continue          # the PS-93 item below says it from the live record
            items.append(_audit_item(r, "PHD2 settings audit (PS-89)", "auditSec", "pa",
                                     _where_phd2(r), _prio_phd2(r)))
        sections["auditSec"] = _counts(phd2.get("rows"))
        srcinfo["phd2_audit"] = {"t_utc": phd2.get("t_utc"), "reason": phd2.get("reason")}
    else:
        srcinfo["phd2_audit"] = None
    if thesky:
        guarded("TheSky audit", lambda: annotate_thesky(thesky, now))
        for r in thesky.get("rows") or []:
            if r.get("status") in (FAIL, WARN):
                it = _audit_item(r, "TheSky / TPoint audit (PS-104)", "tpointSec",
                                 "ts", "TheSky", _prio_thesky(r))
                if r.get("id") == "protrack_on" and r.get("protrack") == "off":
                    # PS-138: say it plainly, with the fix
                    from photonscript.scheduler.thesky_audit import PROTRACK_FIX
                    it.update(setting="ProTrack OFF", fix=PROTRACK_FIX)
                if r.get("id") == "one_thesky" and r.get("status") == FAIL:
                    # PS-138: two Bisque sky apps; the detail names the TCP
                    # owner and the driver's target
                    from photonscript.scheduler.thesky_audit import TWO_THESKY_FIX
                    it.update(setting="Two TheSky apps running", fix=TWO_THESKY_FIX)
                items.append(it)
        sections["tpointSec"] = _counts(thesky.get("rows"))
        srcinfo["thesky_audit"] = {"t_utc": thesky.get("t_utc"),
                                   "reason": thesky.get("reason")}
    else:
        srcinfo["thesky_audit"] = None
    for anchor, res in (("calSec", cal),
                        ("selftestSec", guarded("self-test", lambda: _selftest_items(config))),
                        ("guardSec", guarded("guard", lambda: _guard_items(config, night)))):
        if res is not None:
            items += res[0]
            sections[anchor] = res[1]
    bit8_known = any(i.get("id") == "bit_depth" for i in items)
    tune = guarded("tuner", lambda: _tuner_items(config, night, bit8_known))
    if tune is not None:
        items += tune[0]
        sections["tuneSec"] = tune[1]
    n2 = guarded("NINA #2 mount", lambda: _nina2_mount_items(config, now))
    if n2 is not None:
        items += n2[0]

    order = sorted(range(len(items)), key=lambda i: _sort_key(items[i], i))
    items = [items[i] for i in order]
    fresh = [i for i in items if not i["stale"]]
    stale = [i for i in items if i["stale"]]
    nc = not_checked(phd2, thesky)
    n_fail = sum(1 for i in items if i["severity"] == FAIL)
    n_warn = sum(1 for i in items if i["severity"] == WARN)
    n_nc = sum(g["count"] for g in nc)
    for k, v in sections.items():
        sections[k] = {s: int(v.get(s, 0)) for s in (FAIL, WARN, PASS, UNKNOWN, INFO)}
    line = (f"Guiding: {len(items)} to change ({n_fail} fail, {n_warn} warn), "
            f"{n_nc} not checked")
    if stale:
        line += f", {len(stale)} may already be fixed"
    return {"ok": not problems, "t_utc": store.iso_z(now), "night": night,
            "line": line,
            "counts": {"to_change": len(items), FAIL: n_fail, WARN: n_warn,
                       "stale": len(stale), "not_checked": n_nc},
            "items": fresh, "stale_items": stale, "stale_hint": STALE_HINT,
            "not_checked": nc, "sections": sections, "sources": srcinfo,
            "problems": problems}


def safe_build(config, now: datetime | None = None) -> dict:
    """build() with a last-resort guard (the route and the CLI use this)."""
    try:
        return build(config, now)
    except Exception as e:  # noqa: BLE001
        logger.warning("guiding attention summary failed: %s", e)
        return {"ok": False, "t_utc": store.iso_z(_now(now)),
                "line": "Guiding: summary unavailable",
                "counts": {"to_change": 0, FAIL: 0, WARN: 0, "stale": 0, "not_checked": 0},
                "items": [], "stale_items": [], "stale_hint": STALE_HINT,
                "not_checked": [], "sections": {}, "sources": {},
                "problems": [f"{type(e).__name__}: {e}"]}


def _item_line(it: dict) -> str:
    cur = f"{it.get('current')}"
    if it.get("source_text"):
        cur += f" ({it['source_text']})"
    act = (f"Apply on /guiding ({it['apply_id']})" if it.get("apply_id")
           else it.get("manual_hint") or f"in {it.get('where')}")
    return (f"  [{str(it['severity']).upper():4}] {it['setting']}: {cur} -> "
            f"{it.get('desired')}. {it.get('fix') or ''} [{act}; {it.get('section')}]")


def format_text(s: dict) -> str:
    """The CLI / plain-text form of the summary."""
    lines = [s.get("line") or "Guiding: ?", ""]
    if s.get("items"):
        lines.append("What to change:")
        lines += [_item_line(i) for i in s["items"]]
    else:
        lines.append("What to change: nothing")
    if s.get("stale_items"):
        lines += ["", f"May already be fixed (old reading; {s.get('stale_hint')}):"]
        for i in s["stale_items"]:
            lines.append(_item_line(i) + (f" [stale: {i['stale_why']}]"
                                          if i.get("stale_why") else ""))
    if s.get("not_checked"):
        lines += ["", "Not checked yet:"]
        for g in s["not_checked"]:
            lines.append(f"  {g['count']:3} {g['title']}: {g['action']}")
    src = s.get("sources") or {}
    lines += ["", "Cached audits: PHD2 " + ((src.get("phd2_audit") or {}).get("t_utc") or "none")
              + ", TheSky " + ((src.get("thesky_audit") or {}).get("t_utc") or "none")]
    if s.get("problems"):
        lines.append("Problems: " + "; ".join(s["problems"]))
    return "\n".join(lines)

