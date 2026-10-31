"""Guided-night readiness: can tonight be a guided night? (2026-10-26)

Jeremy: "We need a guider to do Ha, and longer exposures." The RC16 images
unguided on TPoint + ProTrack (300 s cap, narrowband rejected unguided); the
fastest way to guided 300 to 900 s narrowband is to know, every evening,
exactly what stands between tonight and a guided night, in the order the
fixes have to happen (a binning change invalidates the dark library and the
calibration, so it comes first).

    build(config, ...)   pure over the stored records (never runs an audit,
                         never talks to PHD2, NINA or TheSky):
        PHD2 audit       <data_dir>/phd2_audit/latest.json (PS-89)
        calibration      phd2_calibration.summary() (PS-93)
        self-test        phd2_store.selftest_results() (PS-92)
        dark library     phd2_audit.dark_inventory() (the PHD2 darks file)
        camera           the audit's detected guide camera vs
                         guide_camera_expected (PS-190 swap) and the camera
                         seen before (camera_state: the time it changed)
        commissioning    guided_commission.status() (the first guided night's
                         guiding test)
      -> {"status": ready | auto | blocked, "ready", "line", "items": [ordered
          {order, id, level: blocker | auto | advice | info, what, current,
          fix, where, auto}], "fast_path", "camera", "commission", ...}
      blocker  must be fixed before a guided night (a trial night,
               guided_trial_night, goes guided anyway)
      auto     PhotonScript fixes it itself on the guided night (the PS-93
               calibration slot at the next guided dispatch)
      advice   should be fixed; does not stop a guided night
      info     context (guiding mode, commissioning, swap pending)

    headsup_line(config, guided)   one line for the evening plan heads-up
    prep_tick(config, armer)       guided_prep_mode act: the unattended
                                   fix-ups that are safe and already
                                   supported (the PHD2 guide exposure to a
                                   length the dark library has, through the
                                   PS-89 audit apply: PHD2 Stopped / Looping
                                   only, under phd2_ops.hold("audit")); never
                                   while a night runs or a capture job holds
                                   the camera. Building PHD2's dark library or
                                   defect map is GUI-only (no PHD2 RPC for it):
                                   always a human step.

GET /api/guiding/readiness (routers/phd2.py); the Guiding tab's first panel;
the dashboard's Tonight's Run card; the plan heads-up.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

BLOCKER, AUTO, ADVICE, INFO = "blocker", "auto", "advice", "info"
LEVEL_RANK = {BLOCKER: 0, AUTO: 1, ADVICE: 2, INFO: 3}
PREP_MODES = ("off", "advise", "act")
# PHD2 settings that change the guide frame itself: each change invalidates
# the dark library and the calibration, so they are fixed first
GEOMETRY_IDS = ("camera", "binning", "pixel_size_um", "guide_scale", "focal_length",
                "bit_depth", "saturation_adu", "saturation_by_adu")
DARK_USE_IDS = ("darks_or_defects", "have_dark")
AUDIT_STALE_H = 36.0
SELFTEST_NIGHTS = 30
NB_LADDER_DEFAULT = "300,450,600,900"
CAMERA_FILE = "camera.json"
PREP_FILE = "prep.json"


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "guided"


# --------------------------------------------------------------------------
# the guide camera seen (PS-190 swap): when did it change?
# --------------------------------------------------------------------------

def camera_state(config) -> dict:
    return store.read_json(_dir(config) / CAMERA_FILE) or {}


def note_camera(config, cam_id, name=None, now: datetime | None = None) -> dict:
    """Remember the detected guide camera; on a change keep the previous one
    and the time of the change (the dark library and the calibration from
    before it belong to the old sensor). Never raises."""
    st = camera_state(config)
    if not cam_id:
        return st
    if st.get("id") == cam_id:
        return st
    now = now or datetime.utcnow()
    new = {"id": cam_id, "name": name, "since": store.iso_z(now),
           "previous": ({"id": st.get("id"), "name": st.get("name")}
                        if st.get("id") else None),
           # the first camera ever seen is not a change
           "changed": bool(st.get("id"))}
    try:
        store.write_json(_dir(config) / CAMERA_FILE, new)
    except OSError as e:
        logger.debug("guide camera state not saved: %s", e)
    return new


def camera_changed_since(config) -> datetime | None:
    """When the guide camera last changed (None: never seen a change)."""
    st = camera_state(config)
    return store.parse_z(st.get("since")) if st.get("changed") else None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _item(order, rid, level, what, current, fix, where, *, auto=None,
          detail=None, apply_id=None) -> dict:
    return {"order": order, "id": rid, "level": level, "what": what,
            "current": current, "fix": fix, "where": where, "auto": auto,
            "detail": detail, "apply_id": apply_id}


def _rows(audit: dict | None) -> dict:
    return {r.get("id"): r for r in (audit or {}).get("rows") or []}


def _cur_desired(r: dict) -> str:
    return f"{r.get('current')} (want {r.get('desired')})"


def _trial(config, night: str | None) -> bool:
    t = str(getattr(config, "guided_trial_night", "") or "").strip()
    return bool(t and night and t == night)


def prep_mode(config) -> str:
    m = str(getattr(config, "guided_prep_mode", "advise") or "advise").strip().lower()
    return m if m in PREP_MODES else "advise"


def _lib_exposures(config, audit) -> list[int] | None:
    try:
        from photonscript.scheduler.phd2_audit import dark_inventory
        return dark_inventory(config, (audit or {}).get("profile_id"))
    except Exception as e:  # noqa: BLE001
        logger.debug("dark inventory unavailable: %s", e)
        return None


def exposure_fast_path(rows: dict, lib_ms: list[int] | None) -> dict | None:
    """The dark library FAIL is only "no dark at the current exposure" and
    the audit's exposure fix (row exposure_ms, target ms) is a length the
    library holds: applying the exposure row clears both rows without a
    rebuild. {"target_ms", "lib"} or None."""
    d, e = rows.get("dark_library") or {}, rows.get("exposure_ms") or {}
    if d.get("status") != "fail" or not lib_ms:
        return None
    note = str(d.get("note") or "")
    if "current" not in note or "span" in note:
        return None
    t = _num(e.get("target"))
    if t is None or e.get("apply") != "api":
        return None
    if int(t) not in [int(x) for x in lib_ms]:
        return None
    return {"target_ms": int(t), "lib_s": [x / 1000.0 for x in lib_ms]}


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def build(config, now: datetime | None = None, *, audit: dict | None = "load",
          cal: dict | None = "load", selftests: list | None = "load",
          lib_ms="load", commission: dict | None = "load",
          night: str | None = None, guided: bool | None = None,
          darks: bool = False) -> dict:
    """The readiness list (see the module doc). Every source can be passed
    in (tests); "load" reads the stored record. guided: tonight's arm
    (None = config guided_default / trial). darks=True adds the guided
    narrowband ladder's dark status (slower: a library scan). Never
    raises."""
    now = now or datetime.utcnow()
    problems: list[str] = []

    def guarded(name, fn, default=None):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            problems.append(f"{name}: {type(e).__name__}: {e}")
            return default

    if night is None:
        night = guarded("night", lambda: store.night_of(config, now)) or now.strftime("%Y-%m-%d")
    if audit == "load":
        from photonscript.scheduler import phd2_audit as pa
        audit = guarded("PHD2 audit", lambda: pa.load_latest(config))
    if cal == "load":
        from photonscript.scheduler import phd2_calibration as pc
        cal = guarded("calibration", lambda: pc.summary(config), {}) or {}
    if selftests == "load":
        selftests = guarded("self-test", lambda: store.selftest_results(
            config, days=SELFTEST_NIGHTS), []) or []
    if lib_ms == "load":
        lib_ms = _lib_exposures(config, audit)
    rows = _rows(audit)
    items: list[dict] = []

    # ---- 10 the guide camera ------------------------------------------------
    cam = (audit or {}).get("camera") or {}
    cam_id = cam.get("id")
    cstate = guarded("camera state", lambda: note_camera(config, cam_id, cam.get("name"), now)
                     if cam.get("detected") else camera_state(config), {}) or {}
    crow = rows.get("camera") or {}
    if not audit:
        items.append(_item(5, "audit", BLOCKER, "PHD2 settings audit",
                           "none on record",
                           "Run the audit: Guiding tab > Settings audit > Refresh "
                           "(PHD2 open, guide camera connected)", "PhotonScript"))
    elif crow.get("status") == "fail" or (audit and not cam.get("detected")):
        items.append(_item(10, "camera", BLOCKER, "Guide camera",
                           crow.get("current") or cam.get("reported") or "not detected",
                           crow.get("fix") or "PHD2 Connect Equipment: the OAG guide "
                           "camera (GP678C, or the ASI174MM Mini after PS-190)", "PHD2"))
    expected = str(getattr(config, "guide_camera_expected", "") or "").strip().lower()
    swap = bool(expected and cam_id and str(cam_id).lower() != expected)
    if swap:
        items.append(_item(12, "camera_swap", INFO, "Guide camera swap pending (PS-190)",
                           f"{cam.get('name') or cam_id} connected, {expected} expected",
                           "Readiness is judged for the connected camera. After the "
                           "install: HARDWARE.md 'ASI174MM Mini swap checklist' (OAG focus, "
                           "PHD2 profile, darks, defect map, calibration, Guiding Assistant)",
                           "AARO / PHD2"))
    changed = store.parse_z(cstate.get("since")) if cstate.get("changed") else None

    # ---- 20 frame geometry (before darks and calibration) -------------------
    for rid in GEOMETRY_IDS[1:]:
        r = rows.get(rid)
        if not r or r.get("status") not in ("fail", "warn"):
            continue
        lvl = BLOCKER if r["status"] == "fail" else ADVICE
        tail = (" Change it BEFORE building the dark library and calibrating: it "
                "invalidates both." if lvl == BLOCKER else
                " Optional for a first guided night; if you change it, do it BEFORE "
                "building the dark library and calibrating (it invalidates both).")
        items.append(_item(20, rid, lvl, r.get("label") or rid, _cur_desired(r),
                           (r.get("fix") or "") + tail,
                           "PHD2", apply_id=rid if r.get("applicable") else None))

    # ---- 30 / 40 guide exposure and the dark library -------------------------
    fast = exposure_fast_path(rows, lib_ms)
    er = rows.get("exposure_ms") or {}
    if fast:
        items.append(_item(30, "exposure_ms", BLOCKER, "Guide exposure vs the dark library",
                           f"{er.get('current')} ms; the dark library has no dark at it",
                           f"Set PHD2's exposure to {fast['target_ms'] / 1000:g} s: Guiding "
                           "tab > Settings audit > Apply on 'Guide exposure' (PHD2 Stopped or "
                           f"Looping). The library holds {fast['target_ms'] / 1000:g} s, so "
                           "this clears the dark-library FAIL without a rebuild.",
                           "PHD2 (Apply)", apply_id="exposure_ms",
                           auto={"by": "guided_prep_mode act", "mode": prep_mode(config)}))
    elif er.get("status") in ("fail", "warn"):
        items.append(_item(30, "exposure_ms", BLOCKER if er["status"] == "fail" else ADVICE,
                           "Guide exposure", _cur_desired(er),
                           (er.get("fix") or "") + " Set it before building the dark "
                           "library (darks are taken per exposure).", "PHD2",
                           apply_id="exposure_ms" if er.get("applicable") else None))
    dr = rows.get("dark_library") or {}
    lib_age = _num(((audit or {}).get("source_times") or {}).get("file_age_days"))
    lib_t = None
    if lib_age is not None and audit and store.parse_z(audit.get("t_utc")):
        lib_t = store.parse_z(audit.get("t_utc")) - timedelta(days=lib_age)
    build_fix = ("Roof closed: PHD2 Darks > Dark Library, 1 s to at least the guide "
                 "exposure (and 1 to 4 s), at the current gain and binning; then Darks > "
                 "Use Dark Library. GUI only (PHD2 has no API for it): Jeremy or AARO.")
    if changed and lib_t and lib_t < changed:
        items.append(_item(40, "dark_library", BLOCKER, "PHD2 dark library",
                           f"built {lib_t:%Y-%m-%d}, before the guide camera changed "
                           f"{changed:%Y-%m-%d}", build_fix, "PHD2 (human)"))
    elif dr.get("status") == "fail" and not fast:
        items.append(_item(40, "dark_library", BLOCKER, "PHD2 dark library",
                           f"{dr.get('current')}: {dr.get('note') or 'FAIL'}", build_fix,
                           "PHD2 (human)"))
    elif dr.get("status") == "fail" and fast:
        items.append(_item(41, "dark_library_age", ADVICE, "PHD2 dark library rebuild",
                           str(dr.get("current")),
                           "Fixed by the exposure above for now; rebuild before it is "
                           f"{getattr(config, 'phd2_dark_max_age_days', 30):g} days old "
                           "or after any binning / gain change. " + build_fix,
                           "PHD2 (human)"))
    elif dr.get("status") == "warn":
        items.append(_item(41, "dark_library", ADVICE, "PHD2 dark library",
                           f"{dr.get('current')}: {dr.get('note') or 'WARN'}", build_fix,
                           "PHD2 (human)"))
    for rid in DARK_USE_IDS:
        r = rows.get(rid)
        if r and r.get("status") in ("fail", "warn"):
            items.append(_item(45, rid, ADVICE, r.get("label") or rid, _cur_desired(r),
                               (r.get("fix") or "") + " Without it the library is not "
                               "used: hot pixels can pass for guide stars (PS-91).",
                               "PHD2"))

    # ---- 50 the pulse path ----------------------------------------------------
    active = [r for r in selftests or [] if r.get("kind", "active") == "active"
              and r.get("verdict") not in ("SKIPPED",)]
    if active and str(active[-1].get("verdict") or "").upper() == "FAIL":
        last = active[-1]
        items.append(_item(50, "selftest", BLOCKER, "Pulse self-test (PS-92)",
                           f"FAIL {str(last.get('t_utc') or '')[:10]}",
                           "Check the guide cable / ASCOM pulse path and TheSky's "
                           "autoguide rate, then Guiding tab > Run self-test now.",
                           "mount driver",
                           detail="; ".join(last.get("reasons") or []) or None))
    elif not active:
        items.append(_item(52, "selftest", ADVICE, "Pulse self-test (PS-92)",
                           f"none in {SELFTEST_NIGHTS} nights",
                           "Guiding tab > Run self-test now with the scope tracking on a "
                           "star field and PHD2 not guiding (proves the pulses move the "
                           "mount: 2026-10-06 every pulse was refused).", "PhotonScript"))
    sr = rows.get("silenced_alerts") or {}
    if sr.get("status") in ("fail", "warn"):
        items.append(_item(55, "silenced_alerts", ADVICE, sr.get("label") or "PHD2 alerts",
                           str(sr.get("current")), sr.get("fix") or
                           "PHD2: re-enable the silenced alert", "PHD2"))

    # ---- 60 / 70 calibration --------------------------------------------------
    stp = rows.get("calibration_step_ms") or {}
    if stp.get("status") in ("fail", "warn"):
        items.append(_item(60, "calibration_step_ms",
                           BLOCKER if stp["status"] == "fail" else ADVICE,
                           stp.get("label") or "Calibration step", _cur_desired(stp),
                           (stp.get("fix") or "") + " Set it before the calibration "
                           "(about 12 steps per axis).", "PHD2",
                           apply_id="calibration_step_ms" if stp.get("applicable") else None))
    items += _calibration_items(config, cal or {}, changed, now)

    # ---- 80 the other audit fails / warns -------------------------------------
    done = {i["id"] for i in items} | set(GEOMETRY_IDS) | set(DARK_USE_IDS) | {
        "exposure_ms", "dark_library", "calibration_record", "calibration_step_ms",
        "silenced_alerts", "flip"}
    warns = []
    for rid, r in rows.items():
        if rid in done:
            continue
        if r.get("status") == "fail":
            items.append(_item(80, rid, BLOCKER, r.get("label") or rid, _cur_desired(r),
                               r.get("fix") or "", "PHD2",
                               apply_id=rid if r.get("applicable") else None))
        elif r.get("status") == "warn":
            warns.append(r)
    if warns:
        items.append(_item(85, "audit_warns", ADVICE,
                           f"{len(warns)} other PHD2 audit warning(s)",
                           ", ".join(str(r.get("label") or r.get("id")) for r in warns[:6])
                           + (" ..." if len(warns) > 6 else ""),
                           "Guiding tab > What to change (Apply where offered).",
                           "PHD2", detail="; ".join(f"{r.get('id')}: {_cur_desired(r)}"
                                                    for r in warns)))
    if audit:
        at = store.parse_z(audit.get("t_utc"))
        if at and (now - at).total_seconds() > AUDIT_STALE_H * 3600:
            items.append(_item(90, "audit_age", ADVICE, "PHD2 audit age",
                               f"{(now - at).total_seconds() / 3600:.0f} h old",
                               "Guiding tab > Settings audit > Refresh (a fixed row stays "
                               "listed until the audit re-runs).", "PhotonScript"))

    # ---- 95 commissioning, mode -------------------------------------------------
    if commission == "load":
        from photonscript.scheduler import guided_commission as gc
        commission = guarded("commissioning", lambda: gc.status(config, night, cam_id), {})
    commission = commission or {}
    if commission.get("needed"):
        items.append(_item(95, "commission", INFO, "Guided commissioning test",
                           commission.get("text") or "not commissioned with this camera",
                           commission.get("fix") or "", "PhotonScript"))
    trial = _trial(config, night)
    gdef = bool(getattr(config, "guided_default", True))
    if guided is None:
        guided = gdef or trial
    if not guided:
        items.append(_item(99, "mode", INFO, "Tonight's guiding mode", "unguided",
                           "Arm (Guiding) on the dashboard, or set guided_trial_night "
                           f"= {night} (System page) for a one-night guided trial.",
                           "dashboard"))
    if darks:
        nb = guarded("NB ladder darks", lambda: nb_ladder_darks(config), None)
        if nb and nb.get("missing"):
            items.append(_item(97, "nb_ladder_darks", ADVICE,
                               "Darks for the guided narrowband ladder",
                               nb["text"], nb["fix"], "PhotonScript"))

    items.sort(key=lambda i: (i["order"], LEVEL_RANK.get(i["level"], 9)))
    blockers = [i for i in items if i["level"] == BLOCKER]
    autos = [i for i in items if i["level"] == AUTO]
    status = "blocked" if blockers else ("auto" if autos else "ready")
    return {"t_utc": store.iso_z(now), "night": night, "status": status,
            "ready": status != "blocked", "trial": trial, "guided": bool(guided),
            "line": _line(status, blockers, autos, trial),
            "items": items, "counts": {lv: sum(1 for i in items if i["level"] == lv)
                                       for lv in (BLOCKER, AUTO, ADVICE, INFO)},
            "fast_path": fast,
            "camera": {"id": cam_id, "name": cam.get("name"), "detected": cam.get("detected"),
                       "expected": expected or None, "swap_pending": swap,
                       "changed_utc": store.iso_z(changed) if changed else None,
                       "previous": cstate.get("previous")},
            "commission": commission,
            "sources": {"phd2_audit": (audit or {}).get("t_utc"),
                        "calibration": ((cal or {}).get("record") or {}).get("t_utc")},
            "problems": problems}


def _calibration_items(config, cal: dict, changed: datetime | None,
                       now: datetime) -> list[dict]:
    """The PS-93 record: none / FAIL / stale / far from Dec 0 / older than the
    camera change. Auto when phd2_cal_mode is auto or always (the slot runs
    at the next guided dispatch), else a blocker."""
    from photonscript.scheduler import phd2_calibration as pc
    mode = pc.cfg_mode(config)
    rec = cal.get("record") or {}
    why = None
    if not cal.get("grade"):
        why = "none on record"
    elif cal.get("poor"):
        why = "FAIL: " + ("; ".join(rec.get("reasons") or []) or "poor")
    elif cal.get("stale"):
        why = f"{cal.get('grade')}, {cal.get('age_days')} d old"
    else:
        far = pc.far_dec_reason(config, rec)
        t = store.parse_z(rec.get("t_utc"))
        if far:
            why = far
        elif changed and t and t < changed:
            why = f"taken {t:%Y-%m-%d}, before the guide camera changed"
    if why is None:
        return []
    where = (f"Dec {rec.get('dec_deg')}, HA {rec.get('ha_hr')} h, pier "
             f"{rec.get('pier_side')}" if rec.get("dec_deg") is not None else None)
    cur = why + (f" ({where})" if where else "")
    if mode == "never":
        return [_item(70, "calibration", BLOCKER, "PHD2 calibration", cur,
                      "phd2_cal_mode is never: calibrate near Dec 0 by the meridian by "
                      "hand (GET /api/phd2/calibration-sequence), or set phd2_cal_mode "
                      "auto.", "PHD2")]
    plan = cal.get("plan") or {}
    field = (plan.get("field") or {}).get("name")
    return [_item(70, "calibration", AUTO, "PHD2 calibration", cur,
                  "PS-93 calibrates at the start of the next guided night on a field near "
                  "Dec +5, 0.25 to 1 h from the meridian (about 5 min), grades it and "
                  "retries once. Nothing to do; 'Calibrate at the next dispatch' on the "
                  "Guiding tab forces it.", "PhotonScript",
                  auto={"by": "PS-93 calibration slot", "mode": mode,
                        "field": field, "planned": bool(plan)})]


def _line(status: str, blockers: list, autos: list, trial: bool) -> str:
    if status == "ready":
        return "Guided night: READY"
    if status == "auto":
        return ("Guided night: READY (auto: " + "; ".join(a["what"] for a in autos)
                + " on the guided night)")
    head = f"Guided night: BLOCKED ({len(blockers)})"
    if trial:
        head += ", trial night set: arms guided anyway"
    return head + ": " + "; ".join(f"{b['what']}" for b in blockers[:3]) + (
        " ..." if len(blockers) > 3 else "")


def safe_build(config, now: datetime | None = None, **kw) -> dict:
    try:
        return build(config, now, **kw)
    except Exception as e:  # noqa: BLE001
        logger.warning("guided readiness failed: %s", e)
        return {"t_utc": store.iso_z(now or datetime.utcnow()), "status": "unknown",
                "ready": False, "line": "Guided night: readiness unavailable",
                "items": [], "counts": {}, "problems": [f"{type(e).__name__}: {e}"]}


# --------------------------------------------------------------------------
# the guided narrowband ladder's darks
# --------------------------------------------------------------------------

def nb_ladder_rungs(config) -> list[float]:
    raw = str(getattr(config, "night_pilot_ladder_rc16_guided_nb", NB_LADDER_DEFAULT)
              or NB_LADDER_DEFAULT)
    out = set()
    for x in raw.replace(";", ",").split(","):
        v = _num(x.strip())
        if v and v > 0:
            out.add(v)
    return sorted(out) or [float(x) for x in NB_LADDER_DEFAULT.split(",")]


def nb_ladder_darks(config, status=None) -> dict:
    """Which guided narrowband rungs have no matched darks (night quota or
    library, night_pilot.dark_match). status: test hook (seconds -> dict)."""
    if status is None:
        from photonscript.scheduler.night_pilot import dark_match

        def status(s):
            return dark_match(config, "rc16", s)
    rows = {s: status(s) for s in nb_ladder_rungs(config)}
    missing = [s for s, d in rows.items() if (d or {}).get("match") != "matched"]
    txt = ", ".join(f"{s:g} s {(rows[s] or {}).get('match')}" for s in rows)
    return {"rungs": {f"{s:g}": rows[s] for s in rows}, "missing": missing,
            "text": txt,
            "fix": ("The guided ladder skips a rung without matched darks. Turn on "
                    "rc16_guided_nb_ladder_darks (System page) or add "
                    + "/".join(f"{s:g}" for s in missing)
                    + " to rc16_always_dark_exposures so closed-roof time fills them.")
            if missing else ""}


# --------------------------------------------------------------------------
# the evening plan heads-up line
# --------------------------------------------------------------------------

def headsup_line(config, guided: bool, night: str | None = None,
                 r: dict | None = None) -> str | None:
    """'Guided night: BLOCKED (1): ... Fix: ...; auto: PS-93 calibration'.
    None when there is nothing to say (ready, nothing auto, and guided)."""
    try:
        r = r or build(config, night=night, guided=guided)
        if r.get("status") == "ready" and guided and not (r.get("commission") or {}).get("needed"):
            return None
        bits = [r.get("line") or "Guided night: ?"]
        blk = [i for i in r.get("items") or [] if i["level"] == BLOCKER]
        if blk:
            b = blk[0]
            bits.append(f"First fix: {b['what']} ({b['current']}): {str(b['fix'])[:160]}")
        com = r.get("commission") or {}
        if guided and com.get("needed"):
            bits.append(str(com.get("text") or ""))
        return ". ".join(x.rstrip(". ") for x in bits if x) + "."
    except Exception as e:  # noqa: BLE001
        logger.debug("guided readiness heads-up line skipped: %s", e)
        return None


# --------------------------------------------------------------------------
# the unattended fix-ups (guided_prep_mode)
# --------------------------------------------------------------------------

IDLE_STATES = ("DISARMED", "COMPLETE", "ARMED", "")


def _prep_state(config) -> dict:
    return store.read_json(_dir(config) / PREP_FILE) or {}


def _save_prep(config, st: dict) -> None:
    try:
        store.write_json(_dir(config) / PREP_FILE, st)
    except OSError as e:
        logger.debug("guided prep state not saved: %s", e)


def prep_guard(config, armer, now: datetime) -> str | None:
    """Why the fix-ups must not run now, else None: never while a night
    runs (RUNNING, PAUSED_*, WATCHING), an armed night past pre-config, a
    calibration capture job, or another actor holding PHD2."""
    state = str(getattr(armer, "state", "") or "")
    if state not in IDLE_STATES:
        return f"armer is {state}: never during a night"
    if state == "ARMED":
        pc = store.parse_z(((getattr(armer, "plan", None) or {}).get("preconfig_utc")))
        if pc is None or now >= pc - timedelta(minutes=15):
            return "armed and within 15 min of pre-config"
    try:
        from photonscript.scheduler.night_pilot import _calibration_busy
        cal = _calibration_busy()
        if cal:
            return f"a calibration capture job runs on the {cal}"
    except Exception:  # noqa: BLE001
        pass
    try:
        from photonscript.telescope_agent import phd2_ops
        if phd2_ops.busy():
            return f"PHD2 is held by {phd2_ops.owner()}"
    except Exception:  # noqa: BLE001
        pass
    return None


async def prep_tick(config, armer, now: datetime | None = None, *,
                    readiness: dict | None = None, apply_fn=None,
                    audit_fn=None) -> dict:
    """guided_prep_mode: advise records what it would do (once per night);
    act applies the PHD2 guide exposure fast path (exposure_fast_path) once
    per night through the PS-89 audit apply (PHD2 Stopped / Looping only)
    and re-runs the audit. apply_fn / audit_fn: test hooks. Never raises."""
    now = now or datetime.utcnow()
    m = prep_mode(config)
    out = {"mode": m, "did": None}
    if m == "off":
        return out
    try:
        r = readiness or build(config, now)
        fast = r.get("fast_path")
        night = r.get("night")
        st = _prep_state(config)
        if not fast or st.get("exposure_night") == night:
            return out
        why = prep_guard(config, armer, now)
        if why:
            out["held"] = why
            return out
        if m == "advise":
            st["exposure_night"] = night
            st["exposure"] = {"t": store.iso_z(now), "advised": True,
                              "target_ms": fast["target_ms"]}
            _save_prep(config, st)
            out["did"] = "advised"
            return out
        from photonscript.scheduler import phd2_audit as pa
        apply_fn = apply_fn or (lambda: pa.apply(config, ["exposure_ms"], dry_run=False,
                                                 armer_state=str(getattr(armer, "state", ""))))
        res = await apply_fn()
        ok = bool((res or {}).get("ok"))
        st["exposure_night"] = night
        st["exposure"] = {"t": store.iso_z(now), "ok": ok, "target_ms": fast["target_ms"],
                          "result": (res or {}).get("results")}
        _save_prep(config, st)
        out["did"] = "applied" if ok else "failed"
        if ok:
            audit_fn = audit_fn or (lambda: pa.run_audit(config, reason="guided prep"))
            try:
                await audit_fn()
            except Exception as e:  # noqa: BLE001
                logger.debug("guided prep re-audit failed: %s", e)
        try:
            from photonscript.shared import pushover
            await pushover.notify(
                config,
                (f"Guided prep: PHD2 guide exposure set to {fast['target_ms'] / 1000:g} s "
                 "(the dark library has it; clears the dark-library FAIL)." if ok else
                 f"Guided prep: setting the PHD2 guide exposure to "
                 f"{fast['target_ms'] / 1000:g} s failed: "
                 + "; ".join(str(x.get("note") or "") for x in (res or {}).get("results") or [])),
                title="PhotonScript guided prep", kind="info")
        except Exception:  # noqa: BLE001
            pass
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("guided prep failed: %s", e)
        out["error"] = str(e)
        return out
