"""TPoint run watcher, fitted-model history and the unguided limits epoch.

2026-10-14: after a SharpCap polar alignment (15") Jeremy ran a 228-point
TheSky calibration run. TheSky autosaves "TPoint\\<run> in.dat" and
"<run> outmod.dat" after every point (6 basic terms: IH ID ME MA CH NP),
so the run can be followed from TPoint's own files without touching TheSky
or the mount. Everything here reads files (tpoint_files.list_files / _read)
and writes only under <data_dir>/tpoint.

Watcher (run_watch, every tpoint_watch_poll_s while a run is in progress,
5x that while idle):

  run        the newest raw-input data file ("<run> in.dat", "Super Model
             Indat.dat"). Identity = its folder / run name + its first
             observation line (TheSky starts the file over for a new run).
             It is read only when its size or mtime changed.
  running    its point count grew (or it was written within
             tpoint_watch_quiet_min when first seen). Pace = the mean gap
             between consecutive points from the LST column of each
             observation (gaps over PAUSE_S left out), in solar seconds;
             ETA to tpoint_run_target_points when set (0 = unknown: pace
             only).
  stopped    no new point for tpoint_watch_quiet_min and no fitted model
             yet: waiting for the model fit.
  finished   a model file written at or after the last new point with more
             than the 6 basic terms fitted (TPoint's Super Model fit), or
             stopped for tpoint_watch_fit_wait_min more. Then, once per
             run: the model goes into the history, the epoch check runs, a
             "tpoint_watch" run event (the morning report line) and one
             Pushover (tpoint_watch_mode alert) with the final stats against
             the previous model (RMS, popn SD, terms fitted, MA / ME and the
             polar error before / after). A run that grows again after
             finishing goes back to running; it is never pushed twice.

History (<data_dir>/tpoint/models.jsonl): one line per fitted model
(date, local night, points, points in the fit, sky RMS, popn SD, fitted /
total terms, IH ID MA ME CH NP of the base model, the active recal, the
polar error), appended when the model in TPoint's files changes while no
run is in progress (a later refit or recal included). An empty history is
seeded with the previous model (the files without the newest model's run)
and the current one. GET /api/thesky/tpoint/history, `photonscript
tpoint-stats --history`.

Unguided limits epoch (<data_dir>/tpoint/unguided_epoch.json): when a new
BASE model's MA or ME differs from the previous base model's by more than
tpoint_epoch_polar_shift_arcsec (30"), the polar axis moved: the learned
unguided limits (unguided_limits.since_date) take evidence only from the
local night of that model on (the later of this and the TPoint model in
force; unguided_limit_since still overrides both), the table is refreshed
and the morning report says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import tpoint_files as tf

logger = logging.getLogger(__name__)

MODES = ("alert", "observe", "off")
KIND = "tpoint_watch"
BASIC_TERMS = 6           # IH ID ME MA CH NP: what the run autosaves
PAUSE_S = 600.0           # a gap longer than this is a pause, not the pace
SIDEREAL = 1.00273790935  # sidereal seconds per solar second
MAIN_TERMS = ("IH", "ID", "MA", "ME", "CH", "NP")
_IN_RE = re.compile(r"(?:^|\s)in(?:dat)?$", re.IGNORECASE)


# ------------------------------------------------------------------ config

def mode(cfg) -> str:
    m = str(getattr(cfg, "tpoint_watch_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def _f(cfg, key: str, default: float, lo: float = 0.0) -> float:
    try:
        v = float(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return default
    return max(lo, v) if v == v else default


def poll_s(cfg) -> float:
    return _f(cfg, "tpoint_watch_poll_s", 60.0, 10.0)


def quiet_min(cfg) -> float:
    return _f(cfg, "tpoint_watch_quiet_min", 15.0, 1.0)


def fit_wait_min(cfg) -> float:
    return _f(cfg, "tpoint_watch_fit_wait_min", 45.0, 0.0)


def target_points(cfg) -> int:
    return int(_f(cfg, "tpoint_run_target_points", 0.0, 0.0))


def shift_limit(cfg) -> float:
    return _f(cfg, "tpoint_epoch_polar_shift_arcsec", 30.0, 0.0)


# ------------------------------------------------------------------ paths

def _dir(cfg) -> Path:
    return tf.stats_dir(cfg)


def state_path(cfg) -> Path:
    return _dir(cfg) / "watch_state.json"


def history_path(cfg) -> Path:
    return _dir(cfg) / "models.jsonl"


def epoch_path(cfg) -> Path:
    return _dir(cfg) / "unguided_epoch.json"


def _read_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(p: Path, d: dict) -> None:
    from photonscript.shared.phd2_store import write_json
    try:
        write_json(p, d)          # tmp + fileops.replace_retry
    except OSError as e:
        logger.warning("TPoint watch: %s not saved: %s", p.name, e)


def load_state(cfg) -> dict:
    return _read_json(state_path(cfg)) or {}


def history(cfg) -> list[dict]:
    out = []
    try:
        for ln in history_path(cfg).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    except OSError:
        pass
    return out


def _append_history(cfg, rec: dict) -> None:
    try:
        p = history_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError as e:
        logger.warning("TPoint model history not saved: %s", e)


def load_epoch(cfg) -> dict | None:
    return _read_json(epoch_path(cfg))


def epoch_since(cfg) -> str | None:
    """The unguided limits epoch (YYYY-MM-DD), None when never set."""
    e = load_epoch(cfg) or {}
    s = str(e.get("since") or "").strip()
    return s[:10] if s else None


# ------------------------------------------------------------------ helpers

def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _parse(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).strip().rstrip("Z"))
    except (TypeError, ValueError):
        return None


def _night(cfg, when: datetime) -> str:
    from photonscript.shared.phd2_store import night_of
    try:
        return night_of(cfg, when)
    except Exception:  # noqa: BLE001
        return when.strftime("%Y-%m-%d")


def _local_hm(cfg, when: datetime | None) -> str:
    if when is None:
        return "?"
    try:
        from photonscript.shared.localtime import to_local
        return to_local(cfg, when).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return when.strftime("%H:%MZ")


def observations(text: str) -> tuple[list[float], str | None]:
    """(LST hours of each observation, the first observation line) from a
    TPOINT input data file (parse_data's rules: the first numeric line is
    the site line, an observation has at least 10 numbers and ends with
    the LST as "h m.mmm")."""
    lst, first, site = [], None, False
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or s.startswith((":", "!", "#")):
            continue
        if s.upper() == "END":
            break
        if s[0] not in "+-0123456789":
            continue
        n = tf._nums(s.replace("+", " +").replace("-", " -"))
        if n is None:
            continue
        if not site:
            site = True
            continue
        if len(n) >= 10:
            if first is None:
                first = s
            lst.append(n[-2] + n[-1] / 60.0)
    return lst, first


def pace_s(lst: list[float]) -> float | None:
    """Mean solar seconds per point from the LST column, pauses left out."""
    gaps = []
    for a, b in zip(lst, lst[1:]):
        d = ((b - a) % 24.0) * 3600.0 / SIDEREAL
        if 0 < d <= PAUSE_S:
            gaps.append(d)
    return round(sum(gaps) / len(gaps), 1) if gaps else None


def run_file(listing: dict) -> dict | None:
    """The newest raw-input data file in the listing (newest first)."""
    for f in listing.get("files") or []:
        if _IN_RE.search(Path(str(f.get("rel") or "")).stem):
            return f
    return None


def _fitted_after(listing: dict, since_ts: float, cache: dict) -> dict | None:
    """A model file written at or after since_ts with more than the basic
    terms fitted: {"file", "fitted_terms"} or None."""
    for f in listing.get("files") or []:
        if f.get("_ts", 0) < since_ts - 1.0:
            break          # newest first
        if _IN_RE.search(Path(str(f.get("rel") or "")).stem):
            continue
        key = (f["path"], f.get("size"), f.get("_ts"))
        if key not in cache:
            text = tf._read(f["path"])
            m = tf.parse_model(text) if text and tf.parse_data(text) is None else None
            cache[key] = (m or {}).get("fitted_terms") or 0
        if cache[key] > BASIC_TERMS:
            return {"file": f["rel"], "fitted_terms": cache[key]}
    return None


# ------------------------------------------------------------------ models

def summarize(cfg, st: dict, listing: dict | None = None) -> dict | None:
    """One history record from tpoint_files.stats (the base model's
    numbers, the active recal, the polar error)."""
    if not st or not st.get("ok") or not st.get("model_file"):
        return None
    terms = st.get("terms") or {}
    mtime = None
    for f in (listing or {}).get("files") or []:
        if f.get("rel") == st["model_file"]:
            mtime = f.get("mtime")
            break
    pol = st.get("polar") or {}
    rc = st.get("recal") or {}
    rec = {"model_date": st.get("model_date"), "model_mtime": mtime,
           "night": _night(cfg, _parse(mtime)) if mtime else st.get("model_date"),
           "model_file": st.get("model_file"), "kind": st.get("model_kind"),
           "active_model": st.get("active_model"),
           "points": st.get("points"), "model_points": st.get("model_points"),
           "sky_rms_arcsec": st.get("sky_rms_arcsec"),
           "popn_sd_arcsec": st.get("popn_sd_arcsec"),
           "fitted_terms": st.get("fitted_terms"), "n_terms": len(terms),
           "terms": {k: round(terms[k]["value"], 1) for k in MAIN_TERMS
                     if (terms.get(k) or {}).get("value") is not None},
           "polar_arcmin": pol.get("total_arcmin"),
           "polar_true_arcmin": pol.get("true_pole_arcmin"),
           "polar_ma_arcsec": pol.get("ma_arcsec"),
           "polar_me_arcsec": pol.get("me_arcsec")}
    if rc and rc.get("has_data") and rc.get("after_base"):
        rt = rc.get("terms") or {}
        rec["recal"] = {"model_date": rc.get("model_date"), "points": rc.get("points"),
                        "sky_rms_arcsec": rc.get("sky_rms_arcsec"),
                        "fitted": rc.get("fitted"),
                        "terms": {k: round(rt[k]["value"], 1) for k in MAIN_TERMS
                                  if (rt.get(k) or {}).get("value") is not None}}
    return rec


def _sig(rec: dict | None) -> tuple:
    if not rec:
        return ()
    rc = rec.get("recal") or {}
    return (rec.get("model_file"), rec.get("model_mtime"), rec.get("model_points"),
            rec.get("sky_rms_arcsec"), rec.get("fitted_terms"),
            rc.get("model_date"), rc.get("sky_rms_arcsec"))


def _base_sig(rec: dict | None) -> tuple:
    if not rec:
        return ()
    return (rec.get("model_file"), rec.get("model_mtime"), rec.get("model_points"),
            rec.get("sky_rms_arcsec"))


def previous_from_files(cfg, listing: dict, exclude_run) -> dict | None:
    """The model in TPoint's files without one run's files (the run being
    written now, or the newest model's run): what was in force before."""
    rest = {"roots": listing.get("roots") or [],
            "files": [f for f in listing.get("files") or [] if tf.run_key(f) != exclude_run]}
    rec = summarize(cfg, tf.stats(cfg, rest), rest)
    if rec:
        rec["source"] = "files (before this run)"
    return rec


def _num(v, nd=1) -> str:
    return f"{round(float(v), nd):g}" if isinstance(v, (int, float)) else "?"


def _arc(v) -> str:
    return f"{float(v):+.1f}\"" if isinstance(v, (int, float)) else "?"


def compare(new: dict, prev: dict | None) -> dict:
    """{"d_ma", "d_me" (base MA / ME change, arcsec), "lines"} new vs prev."""
    nt, pt = new.get("terms") or {}, (prev or {}).get("terms") or {}
    d = {k: (round(nt[k] - pt[k], 1) if isinstance(nt.get(k), (int, float))
             and isinstance(pt.get(k), (int, float)) else None) for k in ("MA", "ME")}
    was = (lambda k: f" (was {_num(prev.get(k), 2)}\")"
           if prev and prev.get(k) is not None else "")
    lines = [f"sky RMS {_num(new.get('sky_rms_arcsec'), 2)}\"{was('sky_rms_arcsec')}, "
             f"popn SD {_num(new.get('popn_sd_arcsec'), 2)}\"{was('popn_sd_arcsec')}",
             f"terms fitted {new.get('fitted_terms', '?')}"
             + (f" (was {prev.get('fitted_terms')})" if prev and prev.get('fitted_terms') is not None else "")
             + f", points {new.get('points', '?')}"
             + (f" ({new.get('model_points')} in the fit)"
                if new.get("model_points") not in (None, new.get("points")) else "")
             + (f" (was {prev.get('points')})" if prev and prev.get("points") is not None else "")]
    pol = (f"polar {_num(new.get('polar_arcmin'), 2)}'"
           + (f" (was {_num(prev.get('polar_arcmin'), 2)}')" if prev and prev.get("polar_arcmin") is not None else "")
           + f": MA {_arc(nt.get('MA'))}" + (f" (was {_arc(pt.get('MA'))})" if 'MA' in pt else "")
           + f", ME {_arc(nt.get('ME'))}" + (f" (was {_arc(pt.get('ME'))})" if 'ME' in pt else ""))
    lines.append(pol)
    return {"d_ma": d["MA"], "d_me": d["ME"], "lines": lines}


def check_epoch(cfg, new: dict, prev: dict | None, now: datetime) -> dict | None:
    """A new BASE model whose MA or ME moved more than the limit from the
    previous base model: the unguided limits epoch moves to its night.
    Returns the epoch record when it was set."""
    lim = shift_limit(cfg)
    if lim <= 0 or not prev or new.get("kind") != "base" or prev.get("kind") not in ("base", None):
        return None
    if _base_sig(new) == _base_sig(prev):
        return None
    c = compare(new, prev)
    moved = [(k, v) for k, v in (("MA", c["d_ma"]), ("ME", c["d_me"]))
             if v is not None and abs(v) > lim]
    if not moved:
        return None
    since = str(new.get("night") or new.get("model_date") or "")[:10]
    if not since:
        return None
    cur = load_epoch(cfg) or {}
    if str(cur.get("since") or "") >= since:
        return None
    rec = {"since": since, "set_utc": _iso(now), "model_file": new.get("model_file"),
           "model_date": new.get("model_date"), "d_ma_arcsec": c["d_ma"],
           "d_me_arcsec": c["d_me"], "limit_arcsec": lim,
           "previous": {k: prev.get(k) for k in ("model_date", "model_file", "terms")},
           "note": "polar alignment moved: " + ", ".join(f"{k} {v:+.0f}\"" for k, v in moved)}
    _write_json(epoch_path(cfg), rec)
    try:
        from photonscript.scheduler import unguided_limits as ul
        if ul.mode(cfg) != "off":
            ul.refresh(cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("unguided limits not refreshed after the TPoint epoch: %s", e)
    return rec


def epoch_line(ep: dict) -> str:
    return (f"Unguided limits: evidence restarts {ep['since']} ({ep.get('note')}; "
            "limits learned on the old alignment are not used)")


def record_model(cfg, listing: dict, now: datetime, st: dict | None = None) -> dict:
    """Append the current model to the history when it changed (seeding an
    empty history with the model before it), then the epoch check.
    {"added": rec | None, "previous": rec | None, "epoch": rec | None}."""
    st = st if st is not None else tf.stats(cfg, listing)
    cur = summarize(cfg, st, listing)
    out = {"added": None, "previous": None, "epoch": None}
    if not cur:
        return out
    hist = history(cfg)
    if hist and _sig(hist[-1]) == _sig(cur):
        return out
    prev = hist[-1] if hist else None
    if prev is None:
        mf = next((f for f in listing.get("files") or [] if f.get("rel") == cur["model_file"]), None)
        prev = previous_from_files(cfg, listing, tf.run_key(mf)) if mf else None
        if prev and _sig(prev) != _sig(cur):
            _append_history(cfg, {"t_utc": _iso(now), **prev})
        else:
            prev = None
    cur = {"t_utc": _iso(now), **cur, "source": cur.get("source") or "files"}
    _append_history(cfg, cur)
    base_prev = prev
    if prev and _base_sig(prev) == _base_sig(cur):
        base_prev = None          # only a recal changed
    elif hist and prev is hist[-1]:
        # the newest earlier entry with another base model
        base_prev = next((h for h in reversed(hist) if _base_sig(h) != _base_sig(cur)), None)
    out.update(added=cur, previous=prev,
               epoch=check_epoch(cfg, cur, base_prev, now) if base_prev else None)
    return out


# ------------------------------------------------------------------ text

def status_text(cfg, state: dict, now: datetime | None = None) -> str:
    """The one-line TPoint status for the dashboard."""
    now = now or datetime.utcnow()
    run = state.get("run") or {}
    st = state.get("status") or "idle"
    if st in ("running", "stopped") and run:
        bits = [f"{run.get('points')} points"
                + (f" of {run['target_points']}" if run.get("target_points") else "")]
        if run.get("pace_s"):
            bits.append(f"{run['pace_s']:.0f} s/point")
        eta = _parse(run.get("eta_utc"))
        if st == "running" and eta:
            mins = max(0, round((eta - now).total_seconds() / 60))
            bits.append(f"ETA {_local_hm(cfg, eta)} local ({mins // 60} h {mins % 60:02d} min)"
                        if mins >= 60 else f"ETA {_local_hm(cfg, eta)} local ({mins} min)")
        last = _parse(run.get("last_point_utc"))
        if st == "stopped":
            bits.append(f"no new point since {_local_hm(cfg, last)} local, waiting for the model fit")
        elif last:
            bits.append(f"last point {_local_hm(cfg, last)} local")
        return f"TPoint run in progress ({run.get('name') or 'run'}): " + ", ".join(bits)
    m = state.get("last_model") or (history(cfg) or [None])[-1]
    if not m:
        return "TPoint: no model seen yet"
    return (f"TPoint model {m.get('model_date') or '?'}: {m.get('points', '?')} points, "
            f"sky RMS {_num(m.get('sky_rms_arcsec'), 2)}\", "
            f"{m.get('fitted_terms', '?')} terms, polar {_num(m.get('polar_arcmin'), 2)}' "
            f"(MA {_arc((m.get('terms') or {}).get('MA'))}, ME {_arc((m.get('terms') or {}).get('ME'))})"
            + (f"; last run finished {_local_hm(cfg, _parse(run.get('finished_utc')))} local"
               if run.get("finished_utc") and st == "finished" else ""))


def short_line(cfg, run: dict, new: dict | None, prev: dict | None,
               epoch: dict | None) -> str:
    """The morning report's one line for a finished run."""
    p = prev or {}
    pt = p.get("terms") or {}

    def was(v, nd=2, unit='"'):
        return f" (was {_num(v, nd)}{unit})" if v is not None else ""
    s = (f"{run.get('points')}-point run finished "
         f"{_local_hm(cfg, _parse(run.get('finished_utc')))} local"
         + (" (model fitted)" if run.get("finish_reason") == "fitted"
            else " (basic terms only)"))
    if new:
        nt = new.get("terms") or {}
        s += (f": sky RMS {_num(new.get('sky_rms_arcsec'), 2)}\"{was(p.get('sky_rms_arcsec'))}, "
              f"{new.get('fitted_terms', '?')} terms{was(p.get('fitted_terms'), 0, '')}, "
              f"polar {_num(new.get('polar_arcmin'), 2)}'{was(p.get('polar_arcmin'), 2, chr(39))}, "
              f"MA {_arc(nt.get('MA'))}" + (f" (was {_arc(pt['MA'])})" if "MA" in pt else "")
              + f", ME {_arc(nt.get('ME'))}" + (f" (was {_arc(pt['ME'])})" if "ME" in pt else ""))
    if epoch:
        s += f". {epoch_line(epoch)}"
    return s


def finish_text(cfg, run: dict, new: dict | None, prev: dict | None,
                epoch: dict | None) -> str:
    head = (f"TPoint run finished: {run.get('points')} points"
            + (f" (target {run['target_points']})" if run.get("target_points") else "")
            + (f", {run['pace_s']:.0f} s/point" if run.get("pace_s") else "")
            + (" (model fitted)" if run.get("finish_reason") == "fitted"
               else " (no fitted model after the wait: basic terms only)"))
    if not new:
        return head + ". No model parsed from TPoint's files."
    c = compare(new, prev)
    lines = [head, *c["lines"]]
    if prev:
        lines.append(f"previous: {prev.get('model_date') or '?'} {prev.get('model_file') or ''}".rstrip())
    if epoch:
        lines.append(epoch_line(epoch))
    return "\n".join(lines)


# ------------------------------------------------------------------ tick

def _event(cfg, night: str, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl
        append_jsonl(events_path(cfg, night),
                     {"t": _iso(now), "rig": "rc16", "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("TPoint watch event not logged: %s", e)


_CACHE: dict = {}


def step(cfg, now: datetime | None = None, listing: dict | None = None,
         state: dict | None = None) -> dict:
    """One synchronous check (file reads only). Returns {"state", "push":
    text | None}; the state is saved."""
    now = now or datetime.utcnow()
    state = dict(state if state is not None else load_state(cfg))
    state["checked_utc"] = _iso(now)
    push = None
    listing = listing if listing is not None else tf.list_files(cfg)
    f = run_file(listing)
    run = dict(state.get("run") or {})
    status = state.get("status") or "idle"
    quiet = timedelta(minutes=quiet_min(cfg))
    if f is not None:
        sig = [f["path"], f.get("size"), f.get("mtime")]
        if sig != state.get("file_sig"):
            text = tf._read(f["path"]) or ""
            d = tf.parse_data(text) or {}
            lst, first = observations(text)
            pts = int(d.get("points") or 0)
            rk = tf.run_key(f)
            rid = hashlib.sha1(f"{rk}|{first}".encode("utf-8", "replace")).hexdigest()[:12]
            mt = _parse(f.get("mtime"))
            fresh = bool(mt and now - mt <= quiet)
            if rid != run.get("id") or pts < int(run.get("points") or 0):
                run = {"id": rid, "name": rk[1], "data_file": f["rel"], "date": d.get("date"),
                       "points": pts, "first_seen_utc": _iso(now),
                       "last_point_utc": f.get("mtime"), "seen_running": fresh,
                       "notified": False}
                status = "running" if fresh and pts else "idle"
                if status == "running":
                    state["baseline"] = previous_from_files(cfg, listing, rk)
            elif pts > int(run.get("points") or 0):
                run.update(points=pts, last_point_utc=f.get("mtime"), seen_running=True)
                if status != "running":
                    if not state.get("baseline"):
                        state["baseline"] = previous_from_files(cfg, listing, rk)
                    status = "running"
            run["pace_s"] = pace_s(lst) or run.get("pace_s")
            state["file_sig"] = sig
        run["target_points"] = target_points(cfg) or None
        if run.get("pace_s") and run.get("target_points") and status == "running":
            left = max(0, run["target_points"] - int(run.get("points") or 0))
            base_t = _parse(run.get("last_point_utc")) or now
            run["eta_utc"] = _iso(base_t + timedelta(seconds=left * run["pace_s"]))
        else:
            run["eta_utc"] = None
    if status in ("running", "stopped") and run:
        last = _parse(run.get("last_point_utc")) or now
        fit = _fitted_after(listing, _ts_of(last), _CACHE)
        if fit:
            run.update(finish_reason="fitted", fit_file=fit["file"])
            status = "finished"
        elif now - last > quiet + timedelta(minutes=fit_wait_min(cfg)):
            run["finish_reason"] = "quiet"
            status = "finished"
        elif now - last > quiet:
            status = "stopped"
        if status == "finished":
            run["finished_utc"] = _iso(now)
            st = tf.stats(cfg, listing)
            new = summarize(cfg, st, listing)
            prev = state.get("baseline")
            hist = history(cfg)
            # the newest recorded model written before this run began (the
            # run's own autosaves never count as the previous model)
            start = _parse(run.get("first_seen_utc")) or last
            if run.get("pace_s"):
                start = min(start, last - timedelta(
                    seconds=run["pace_s"] * max(0, int(run.get("points") or 0) - 1)))
            before = [h for h in hist if (_parse(h.get("model_mtime")) or now) < start]
            if before:
                prev = before[-1]
            elif prev and all(_sig(h) != _sig(prev) for h in hist):
                _append_history(cfg, {"t_utc": _iso(now), **prev})
            epoch = None
            if new and (not prev or _sig(prev) != _sig(new)):
                new = {"t_utc": _iso(now), **new, "source": "watch",
                       "run_points": run.get("points")}
                _append_history(cfg, new)
                epoch = check_epoch(cfg, new, prev, now)
            text = finish_text(cfg, run, new, prev, epoch)
            night = _night(cfg, last)
            run["night"] = night
            if run.get("seen_running") and not run.get("notified"):
                run["notified"] = True
                _event(cfg, night, "finished", short_line(cfg, run, new, prev, epoch), now,
                       points=run.get("points"), model=new, previous=prev)
                if epoch:
                    _event(cfg, night, "epoch", epoch_line(epoch), now, epoch=epoch)
                push = text
            state["last_model"] = new or state.get("last_model")
            state["models_ts"] = max((x.get("_ts", 0) for x in listing.get("files") or []),
                                     default=0)
    elif status in ("idle", "finished"):
        # a refit / recal while no run is in progress
        newest = max((x.get("_ts", 0) for x in listing.get("files") or []), default=0)
        if newest and newest != state.get("models_ts"):
            res = record_model(cfg, listing, now)
            state["models_ts"] = newest
            if res["added"]:
                state["last_model"] = res["added"]
            if res["epoch"]:
                night = res["epoch"]["since"]
                _event(cfg, night, "epoch", epoch_line(res["epoch"]), now, epoch=res["epoch"])
    state["status"] = status
    state["run"] = run or None
    state["text"] = status_text(cfg, state, now)
    _write_json(state_path(cfg), state)
    return {"state": state, "push": push}


def _ts_of(dt: datetime) -> float:
    """A naive UTC datetime as a POSIX timestamp (listing _ts values)."""
    return (dt - datetime(1970, 1, 1)).total_seconds()


async def tick(cfg, now: datetime | None = None, listing: dict | None = None,
               notify=None) -> dict:
    if mode(cfg) == "off":
        return {"state": {"status": "off"}, "push": None}
    res = await asyncio.to_thread(step, cfg, now, listing)
    if res.get("push") and mode(cfg) == "alert":
        if notify is None:
            from photonscript.shared.pushover import notify as _notify
            notify = _notify
        try:
            await notify(cfg, res["push"], title="PhotonScript TPoint run finished")
        except Exception as e:  # noqa: BLE001
            logger.warning("TPoint finish push not sent: %s", e)
    return res


async def run_watch(get_config) -> None:
    """Background loop (app startup). File reads only: never talks to
    TheSky or the mount. Fast while a run is in progress, 5x slower idle."""
    while True:
        cfg = get_config()
        status = "idle"
        try:
            status = ((await tick(cfg)).get("state") or {}).get("status") or "idle"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("TPoint watch failed: %s", e)
        p = poll_s(cfg)
        await asyncio.sleep(p if status in ("running", "stopped") else p * 5)


# ------------------------------------------------------------------ reports

def morning_line(cfg, date: str | None) -> str | None:
    """"TPoint: ..." for the morning report: the run that finished on
    `date` (run events), else the newest finished run when its night is
    after `date` (a TPoint night has no subs log of its own)."""
    evs = []
    if date:
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import read_jsonl
            evs = [e for e in read_jsonl(events_path(cfg, date)) if e.get("kind") == KIND]
        except Exception:  # noqa: BLE001
            evs = []
    if not evs:
        st = load_state(cfg)
        run = st.get("run") or {}
        if run.get("notified") and run.get("night") and (not date or run["night"] > date):
            try:
                from photonscript.shared.night_events import events_path
                from photonscript.shared.phd2_store import read_jsonl
                evs = [e for e in read_jsonl(events_path(cfg, run["night"]))
                       if e.get("kind") == KIND]
            except Exception:  # noqa: BLE001
                evs = []
    if not evs:
        return None
    parts = [str(e.get("detail") or "") for e in evs
             if e.get("value") in ("finished", "epoch") and e.get("detail")]
    # the finish detail already carries its epoch line
    out = []
    for p in parts:
        if not any(p in q for q in out):
            out.append(p)
    return ("TPoint: " + "; ".join(out)) if out else None


def format_history(cfg, recs: list[dict] | None = None, limit: int = 20) -> str:
    recs = history(cfg) if recs is None else recs
    lines = [f"TPoint model history ({len(recs)} model(s), {history_path(cfg)}):"]
    if not recs:
        lines.append("  none yet (the watcher records each fitted model; "
                     "tpoint-stats shows the current one)")
    for r in recs[-limit:]:
        t = r.get("terms") or {}
        rc = r.get("recal")
        lines.append(
            f"  {r.get('model_date') or '?'} {str(r.get('kind') or '?'):5} "
            f"{r.get('points', '?'):>4} pts  RMS {_num(r.get('sky_rms_arcsec'), 2):>6}\"  "
            f"PSD {_num(r.get('popn_sd_arcsec'), 2):>6}\"  {r.get('fitted_terms', '?'):>2} terms  "
            + "  ".join(f"{k} {_arc(t.get(k))}" for k in MAIN_TERMS)
            + f"  polar {_num(r.get('polar_arcmin'), 2)}'"
            + (f"  [recal {rc.get('model_date')} {rc.get('points')} pts RMS "
               f"{_num(rc.get('sky_rms_arcsec'), 2)}\"]" if rc else ""))
    ep = load_epoch(cfg)
    if ep:
        lines.append(f"  unguided limits epoch: since {ep.get('since')} ({ep.get('note')}, "
                     f"set {ep.get('set_utc')})")
    return "\n".join(lines)
