"""TPoint / ProTrack preflight at arm (2026-10-16).

Why: on 2026-10-16 a TheSky "Full" calibration run was started and cleared
the pointing data: `TPoint\\TPoint base run in.dat` went to 137 bytes
(header only, no sample) and `outmod.dat` to 98 bytes ("T 0", IH / ID
+0.0000), and the active TPoint settings file was an empty one. The good
model (east-only samples, 22-term super model, sky RMS about 12") was
only saved as a .tptx under C:\\astro. A night armed UNGUIDED on that would
track with ProTrack on no model (or ProTrack off) and every sub trails.

evaluate() reads TPoint's own files (tpoint_files.stats, read only; the
ProTrack flag comes from the same listing, e.g. ImagingSystem.ini
`ProTrackOn=1`) and the coverage check (tpoint_coverage), and returns one
verdict:

  block  the active model has no samples or no fitted model: its own run's
         data file is header only (0 samples) or the model file has "T 0"
         / no non-zero term
  warn   coverage east-only / west-only; sky RMS over
         tpoint_preflight_rms_max_arcsec (40"); the model is older than a
         recorded polar-alignment change (tpoint_polar_changed_on, or the
         manual TPoint record's equipment_changed_on); ProTrack off;
         TPoint's files not readable
  ok     none of those

What the verdict does (`tpoint_preflight`, block | warn | off; default
block): only the no-model case on an UNGUIDED night is enforced. That case
is never a judgement call (every unguided sub trails), the warn cases are
(an east-only model still tracks well before the flip). Enforced = the
armer arms, pushes the fix at once (priority 1), and every NINA #1
dispatch re-reads the files: while the model is still empty the dispatch
is refused (NINA #1 not loaded, "Dispatch FAILED" page) instead of loading
a night that trails. Not enforced: a guided arm (info only, no push), the
TPoint mapping recipe (the operator is sampling the sky tonight), an arm
with `tpoint_override` (the model was loaded by hand and TheSky has not
rewritten the files), `tpoint_preflight=warn` (the push only).

Fix text for the empty model names the newest saved .tptx model saved
BEFORE the run cleared the data (the empty settings file the new run
created is newer) from `tpoint_saved_models_dir` (C:\\astro) and the TPoint
folders in the listing, else "your latest saved model".

Records: Armer.tpoint_pf (GET /api/arm `tpoint_preflight`, the Arm card
line), run events kind "tpoint_preflight", <data_dir>/tpoint/
preflight_latest.json and the morning report line (morning_line).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

KIND = "tpoint_preflight"
MODES = ("block", "warn", "off")
LEVELS = ("ok", "warn", "block")
DEFAULT_RMS_MAX = 40.0
PUSH_TITLE = "PhotonScript TPoint preflight"
TPOINT_RECIPES = ("tpoint_mapping_then_tonight",)


def mode(cfg) -> str:
    m = str(getattr(cfg, "tpoint_preflight", "block") or "block").strip().lower()
    return m if m in MODES else "block"


def rms_max(cfg) -> float:
    try:
        v = float(getattr(cfg, "tpoint_preflight_rms_max_arcsec", DEFAULT_RMS_MAX))
    except (TypeError, ValueError):
        return DEFAULT_RMS_MAX
    return v if v > 0 else DEFAULT_RMS_MAX


def _iso(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v, nd: int = 1) -> str:
    return "?" if v is None else f"{float(v):.{nd}f}"


# ------------------------------------------------------------- saved models

def saved_models(cfg, listing: dict | None = None, before_iso: str | None = None,
                 limit: int = 3) -> list[dict]:
    """Saved TPoint settings (.tptx) newest first: the files directly in
    tpoint_saved_models_dir (C:\\astro; "" = none) plus the .tptx files in
    the TPoint listing. before_iso: only files saved before it (the empty
    settings file a new run created is newer than the clear). Directory
    listing and stat only; nothing is opened."""
    out, seen = [], set()
    d = str(getattr(cfg, "tpoint_saved_models_dir", r"C:\astro") or "").strip()
    if d:
        try:
            for e in os.scandir(d):
                if e.is_file() and e.name.lower().endswith(".tptx"):
                    st = e.stat()
                    out.append({"path": e.path, "mtime": _iso(st.st_mtime),
                                "size": st.st_size, "_ts": st.st_mtime})
        except OSError:
            pass
    for f in (listing or {}).get("files") or []:
        if str(f.get("rel") or "").lower().endswith(".tptx"):
            out.append({"path": f.get("path") or f.get("rel"), "mtime": f.get("mtime"),
                        "size": f.get("size"), "_ts": f.get("_ts") or 0})
    res = []
    for m in sorted(out, key=lambda x: x["_ts"], reverse=True):
        key = str(m["path"]).lower()
        if key in seen:
            continue
        seen.add(key)
        if before_iso and str(m.get("mtime") or "") >= before_iso:
            continue
        res.append({k: m[k] for k in ("path", "mtime", "size")})
    return res[:limit]


def fix_text(models: list[dict]) -> str:
    what = models[0]["path"] if models else "your latest saved model"
    return (f"TPoint has no model: open {what} (TPoint Module Settings > Open) "
            "and turn ProTrack on, or run a calibration")


# ------------------------------------------------------------- the checks

def _polar_change(cfg) -> tuple[str | None, str | None]:
    """(date, source) of the newest recorded polar-alignment / equipment
    change: config tpoint_polar_changed_on, the manual TPoint record's
    equipment_changed_on."""
    found = []
    v = str(getattr(cfg, "tpoint_polar_changed_on", "") or "").strip()[:10]
    if v:
        found.append((v, "tpoint_polar_changed_on"))
    try:
        from photonscript.scheduler.thesky_audit import load_manual
        rec = load_manual(cfg) or {}
        e = str(rec.get("equipment_changed_on") or "").strip()[:10]
        if e:
            found.append((e, "manual TPoint record (equipment changed)"))
    except Exception:  # noqa: BLE001
        pass
    if not found:
        return None, None
    return max(found)


def evaluate(cfg, *, guided: bool, st: dict | None = None, listing: dict | None = None,
             recipe: dict | None = None, override: bool = False,
             now: datetime | None = None) -> dict:
    """The preflight record (see the module doc): {"level", "action",
    "enforced", "mode", "guided", "checks": [{id, level, text}], "line",
    "fix", "protrack", "points", "fitted_terms", "sky_rms_arcsec",
    "model_date", "coverage", "saved_models", "at"}. Never raises."""
    now = now or datetime.utcnow()
    md = mode(cfg)
    rec: dict = {"mode": md, "guided": bool(guided), "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "override": bool(override), "checks": [], "fix": None}
    checks = rec["checks"]

    def add(cid: str, level: str, text: str) -> None:
        checks.append({"id": cid, "level": level, "text": text})

    try:
        from photonscript.scheduler import tpoint_files as tf
        if st is None:
            listing = listing if listing is not None else tf.list_files(cfg)
            st = tf.stats(cfg, listing)
        prot = st.get("protrack")
        rec["protrack"] = prot
        rec["protrack_file"] = st.get("protrack_file")
        run_pts = st.get("active_run_points")
        t_obs = st.get("active_t_obs")
        nonzero = st.get("active_nonzero_terms")
        fitted = st.get("active_fitted_terms", st.get("fitted_terms"))
        rms = st.get("active_sky_rms_arcsec", st.get("sky_rms_arcsec"))
        mt = st.get("active_model_mtime")
        rec.update(model_file=st.get("active_model_file"), model_date=(mt or "")[:10] or
                   st.get("model_date"), points=(run_pts if run_pts is not None
                                                  else st.get("points")),
                   fitted_terms=fitted, sky_rms_arcsec=rms,
                   data_file=st.get("active_run_data_file") or st.get("data_file"))
        if not st.get("ok"):
            add("files", "warn", "TPoint's files not readable ("
                + str(st.get("note") or "no data or model file") + "): model unknown")
        else:
            empty_why = []
            if run_pts == 0:
                empty_why.append(f"{st.get('active_run_data_file') or 'the run data'} "
                                 "is header only (0 samples)")
            if t_obs == 0 or (nonzero == 0 and st.get("active_model_file")):
                empty_why.append(f"{st.get('active_model_file') or 'the model'} has "
                                 f"{'T 0' if t_obs == 0 else 'no fitted term'}"
                                 f" ({fitted or 0} terms, all 0)")
            if not st.get("active_model_file"):
                empty_why.append("no model file (data only, never fitted)")
            if empty_why:
                rec["points"] = 0 if run_pts == 0 else rec["points"]
                cleared = None
                for f in (listing or {}).get("files") or []:
                    if f.get("rel") == st.get("active_run_data_file"):
                        cleared = f.get("mtime")
                        break
                cleared = cleared or mt
                rec["cleared_utc"] = cleared
                rec["saved_models"] = saved_models(cfg, listing, before_iso=cleared)
                rec["fix"] = fix_text(rec["saved_models"])
                add("model", "block", "no TPoint model: " + "; ".join(empty_why))
            else:
                if rms is not None and float(rms) > rms_max(cfg):
                    add("rms", "warn", f"sky RMS {_num(rms, 1)}\" over "
                        f"{_num(rms_max(cfg), 0)}\"")
                ch, src = _polar_change(cfg)
                if ch and rec["model_date"] and str(rec["model_date"]) < ch:
                    add("polar_change", "warn",
                        f"model {rec['model_date']} is older than the polar / "
                        f"equipment change of {ch} ({src}): rebuild it")
                try:
                    from photonscript.scheduler import tpoint_coverage as tc
                    cov = tc.compact(tc.from_stats(cfg, st, save_it=False))
                except Exception as e:  # noqa: BLE001
                    logger.debug("preflight coverage not read: %s", e)
                    cov = None
                if cov:
                    rec["coverage"] = {k: cov.get(k) for k in
                                       ("overall", "points", "by_side", "line",
                                        "topup_points", "topup_minutes")}
                    if cov.get("overall") in ("east-only", "west-only"):
                        other = "west" if cov["overall"] == "east-only" else "east"
                        add("coverage", "warn",
                            f"coverage {cov['overall']}: no samples {other} of the "
                            f"meridian ({'after' if other == 'west' else 'before'} the flip)"
                            + (f", top-up {cov['topup_points']} points"
                               if cov.get("topup_points") else ""))
        if prot is False:
            add("protrack", "warn", f"ProTrack is OFF ({st.get('protrack_file') or 'TPoint files'})")
        elif prot is None:
            add("protrack", "info", "ProTrack state not in TPoint's files")
    except Exception as e:  # noqa: BLE001
        logger.warning("TPoint preflight failed: %s", e)
        add("files", "warn", f"TPoint preflight could not read the files: {e}")
    lv = ("block" if any(c["level"] == "block" for c in checks)
          else "warn" if any(c["level"] == "warn" for c in checks) else "ok")
    rec["level"] = lv
    is_recipe = bool(recipe and recipe.get("id") in TPOINT_RECIPES)
    if md == "off":
        action, why = "off", "tpoint_preflight=off"
    elif guided:
        action, why = "info", "guided arm"
    elif lv == "block" and md == "block" and not override and not is_recipe:
        action, why = "block", None
    elif lv == "block":
        action = "warn"
        why = ("override: model loaded by hand" if override
               else "TPoint mapping recipe tonight" if is_recipe else "tpoint_preflight=warn")
    else:
        action, why = lv, None
    rec["action"], rec["why_not_enforced"] = action, why
    rec["enforced"] = action == "block"
    rec["line"] = line(rec)
    return rec


def _summary(rec: dict) -> str:
    bits = [("ProTrack on" if rec.get("protrack") else
             "ProTrack OFF" if rec.get("protrack") is False else "ProTrack ?")]
    if rec.get("level") != "block":
        bits.append(f"model {rec.get('model_date') or '?'}, {rec.get('points', '?')} samples, "
                    f"{rec.get('fitted_terms', '?')} terms, sky RMS "
                    f"{_num(rec.get('sky_rms_arcsec'), 1)}\"")
        cov = (rec.get("coverage") or {}).get("overall")
        if cov:
            bits.append(f"coverage {cov}")
    return ", ".join(bits)


def line(rec: dict) -> str:
    """The Arm card line."""
    lv = rec.get("level") or "ok"
    mode_s = "guided" if rec.get("guided") else "unguided"
    head = {"block": "BLOCK", "warn": "WARN", "ok": "OK"}[lv]
    if rec.get("action") == "info" and lv != "ok":
        head += ", info only"
    elif lv == "block" and not rec.get("enforced"):
        head += f", not enforced: {rec.get('why_not_enforced')}"
    probs = [c["text"] for c in rec.get("checks") or [] if c["level"] in ("block", "warn")]
    s = f"{head} ({mode_s}): " + "; ".join(probs + [_summary(rec)])
    if rec.get("enforced"):
        s += ". NINA #1 is not loaded until the model is back"
    return s


def push_text(rec: dict) -> str | None:
    """The arm-time push for an unguided night (None: nothing to say)."""
    if rec.get("guided") or rec.get("action") in ("off", "ok", "info"):
        return None
    if rec.get("level") == "block":
        why = "; ".join(c["text"].replace("no TPoint model: ", "")
                        for c in rec["checks"] if c["level"] == "block")
        s = f"Armed UNGUIDED and {rec.get('fix') or fix_text([])}. ({why})."
        if rec.get("enforced"):
            s += (" NINA #1 will not be loaded at pre-config while the model is empty. "
                  "Loaded it by hand already? Arm again with Arm anyway (TPoint "
                  "loaded) on the Arm card.")
        else:
            s += f" Not enforced ({rec.get('why_not_enforced')}): subs will trail."
        return s
    return "Armed UNGUIDED, TPoint preflight: " + "; ".join(
        c["text"] for c in rec["checks"] if c["level"] == "warn") + "."


def compact(rec: dict | None) -> dict | None:
    if not rec:
        return None
    return {k: rec.get(k) for k in ("level", "action", "enforced", "mode", "guided",
                                    "override", "line", "fix", "protrack", "points",
                                    "fitted_terms", "sky_rms_arcsec", "model_date",
                                    "coverage", "saved_models", "checks", "at",
                                    "night", "why_not_enforced")}


# ------------------------------------------------------------- records

def path(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "tpoint" / "preflight_latest.json"


def save(cfg, rec: dict) -> None:
    try:
        p = path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(compact(rec), indent=1, default=str), encoding="utf-8")
    except OSError as e:
        logger.warning("TPoint preflight not saved: %s", e)


def saved(cfg) -> dict | None:
    try:
        return json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def event(cfg, rec: dict, phase: str, now: datetime | None = None) -> None:
    """One run event kind "tpoint_preflight" (value = level). Never raises."""
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        now = now or datetime.utcnow()
        append_jsonl(events_path(cfg, rec.get("night") or night_of(cfg, now)),
                     {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                      "kind": KIND, "value": rec.get("level"), "phase": phase,
                      "action": rec.get("action"), "detail": rec.get("line"),
                      "fix": rec.get("fix"), "guided": rec.get("guided")})
    except Exception as e:  # noqa: BLE001
        logger.warning("TPoint preflight event not logged: %s", e)


def morning_line(cfg, date: str | None) -> str | None:
    """"TPoint preflight: ..." for the morning report: the night's last
    non-ok preflight event, else the saved record when its night is after
    `date` (a blocked night has no subs log of its own)."""
    evs = []
    if date:
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import read_jsonl
            evs = [e for e in read_jsonl(events_path(cfg, date)) if e.get("kind") == KIND]
        except Exception:  # noqa: BLE001
            evs = []
    if evs:
        e = evs[-1]
        if e.get("value") in ("ok", None):
            return None
        return ("TPoint preflight: " + str(e.get("detail") or e.get("value"))
                + (f". Fix: {e['fix']}" if e.get("fix") else ""))
    rec = saved(cfg)
    if rec and rec.get("level") not in ("ok", None) and (
            not date or str(rec.get("night") or "") > date):
        return ("TPoint preflight: " + str(rec.get("line"))
                + (f". Fix: {rec['fix']}" if rec.get("fix") else ""))
    return None
