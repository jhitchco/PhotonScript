"""PS-123 sideload endpoints: load (never start) a hand-built night into NINA.

GET  /api/sequence/sideload/preview?recipe=&exclude=&exclude=&at=
     lint result + container tree for each rig (RC16 night, Piggy companion)
POST /api/sequence/sideload?rig=rc16|piggyback&recipe=&exclude=&at=
     or with a JSON body (the sequence itself, or {"sequence": {...}}):
     lint gate (422), refuse while the armer is ARMED / RUNNING /
     PAUSED_UNSAFE (409), when NINA cannot be read (502) or runs anything
     (409), and (Piggy recipe) when NINA #2 does not see the safety monitor
     (409). Then save a copy under sequences/, POST /sequence/load only, and
     log a "sideload" event. Start stays in NINA. PS-132: then read NINA's
     own validation (nina_validation.check_loaded): a Validate error is
     ok False / 422 (NINA would refuse Start), Issues are reported with
     ok True; both push once ("validation" in the body).

Pure helpers live in scheduler/sideload.py. Kept out of app.py (PS-8).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

router = APIRouter()

_load_lock = asyncio.Lock()

NOTE = ("Load only: nothing is started. Start the sequence in NINA (or arm "
        "the night as usual).")


def _app():
    from photonscript.scheduler import app
    return app


def _cfg():
    return _app().get_config()


def _armer_state() -> str:
    return str(_app().get_armer().state)


def _seq_dir() -> Path:
    """Same folder the armer writes tonight's dispatched sequences to."""
    return Path.cwd() / "sequences"


def _excludes(exclude) -> list[str]:
    return [str(x).strip() for x in (exclude or []) if str(x or "").strip()]


# ------------------------------------------------------------------ builders

def build_rc16(recipe: str, exclude=(), at: str = "") -> dict:
    """The RC16 sequence for `recipe`: {"name", "seq", "lint" (LintResult),
    "field", "tonight_targets", "targets", "excluded"}."""
    from photonscript.scheduler import sideload as sd
    from photonscript.scheduler.nina_sequence_json import generate_tracking_test_json
    from photonscript.scheduler.sequence_lint import lint
    if recipe != sd.RECIPE_TT_THEN_TONIGHT:
        raise ValueError(f"unknown recipe {recipe!r}")
    app = _app()
    night_name, night_text, guided, dither = app._tonight_sequence(False)
    night = json.loads(night_text)
    fl, ex, rep = app._tracking_test_params("L,Ha", "60,120,180,300", 2)
    field = app._tracking_test_field("", None, None, fl, ex, rep, at)
    tt = json.loads(generate_tracking_test_json(
        field["name"], field["ra_hours"], field["dec_degrees"],
        filters=fl, exposures=ex, repeats=rep))
    tonight = sd.target_names(night)
    drop = {" ".join(x.split()).lower() for x in exclude}
    kept = [n for n in tonight if " ".join(n.split()).lower() not in drop]
    date = night_name.rsplit("_", 1)[-1]
    seq = sd.splice_tracking_test(night, tt, exclude,
                                  name=sd.splice_name(date, field["name"], kept))
    return {"name": seq["Name"], "seq": seq,
            "lint": lint(seq, guided=guided, unguided_dither=dither),
            "field": field, "guided": guided, "tonight_targets": tonight,
            "targets": sd.target_names(seq),
            "excluded": [n for n in tonight if n not in kept]}


def build_piggyback(recipe: str) -> dict:
    """The Piggy-600 companion for `recipe`, generated here on the scope so
    the dark quotas come from its real library. has_safety=True: the lights
    and darks are roof-gated by NINA #2's safety monitor (the load refuses
    when NINA #2 does not report it connected)."""
    from photonscript.scheduler import sideload as sd
    from photonscript.scheduler.calibration import generate_piggyback_companion_json
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    if recipe != sd.RECIPE_TT_THEN_TONIGHT:
        raise ValueError(f"unknown recipe {recipe!r}")
    cfg = _cfg()
    with_lights = bool(getattr(cfg, "piggyback_image_lights", True))
    seq = json.loads(generate_piggyback_companion_json(
        rig_config(cfg, PIGGYBACK), has_safety=True, with_lights=with_lights))
    return {"name": seq.get("Name") or "PiggybackCompanion", "seq": seq,
            "lint": sd.lint_companion(seq), "has_safety": True,
            "with_lights": with_lights}


def _build(rig: str, recipe: str, exclude, at: str) -> dict:
    from photonscript.shared.rigs import PIGGYBACK
    if rig == PIGGYBACK:
        return build_piggyback(recipe)
    return build_rc16(recipe, exclude, at)


def _view(rig: str, built: dict) -> dict:
    """What the dashboard shows for one rig (no sequence body)."""
    from photonscript.scheduler import sideload as sd
    out = {k: v for k, v in built.items() if k not in ("seq", "lint")}
    out["rig"] = rig
    out["lint"] = sd.lint_dict(built["lint"])
    out["tree"] = sd.container_tree(built["seq"])
    return out


# ----------------------------------------------------------------- endpoints

@router.get("/api/sequence/sideload/preview")
async def api_sideload_preview(recipe: str = "tracking_test_then_tonight",
                               exclude: list[str] = Query(default=[]),
                               at: str = ""):
    """Read only: generates and lints, sends nothing to NINA."""
    from photonscript.scheduler import sideload as sd
    from photonscript.shared.rigs import rig_ids, rig_label
    if recipe not in sd.RECIPES:
        return JSONResponse(status_code=400, content={
            "detail": f"unknown recipe {recipe!r}", "recipes": sd.RECIPES})
    cfg = _cfg()
    ex = _excludes(exclude)
    rigs = {}
    for rig in rig_ids(cfg):
        try:
            built = await asyncio.to_thread(_build, rig, recipe, ex, at)
            rigs[rig] = _view(rig, built)
        except Exception as e:  # noqa: BLE001
            rigs[rig] = {"rig": rig, "error": f"{type(e).__name__}: {e}"}
        rigs[rig]["label"] = rig_label(cfg, rig)
    return {"recipe": recipe, "recipes": sd.RECIPES, "exclude": ex,
            "armer": _armer_state(), "rigs": rigs, "note": NOTE}


@router.post("/api/sequence/sideload")
async def api_sideload(rig: str = "rc16", recipe: str = "",
                       exclude: list[str] = Query(default=[]), at: str = "",
                       payload: dict | None = Body(default=None)):
    from photonscript.scheduler import sideload as sd
    from photonscript.scheduler.armer import ACTIVE_STATES
    from photonscript.scheduler.sequence_lint import lint
    from photonscript.shared.rigs import PIGGYBACK, rig_config, rig_ids
    cfg = _cfg()
    if rig not in rig_ids(cfg):
        return JSONResponse(status_code=404, content={
            "detail": f"unknown or disabled rig {rig!r}"})
    ex = _excludes(exclude)
    if payload:
        seq = payload.get("sequence") if isinstance(payload.get("sequence"), dict) \
            else payload
        if "$type" not in seq:
            return JSONResponse(status_code=400, content={
                "detail": "body is not a NINA sequence (no $type)"})
        built = {"name": str(seq.get("Name") or "custom"), "seq": seq,
                 "lint": sd.lint_companion(seq) if rig == PIGGYBACK
                 else lint(seq, guided=None)}
        recipe = None
    elif recipe in sd.RECIPES:
        try:
            built = await asyncio.to_thread(_build, rig, recipe, ex, at)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=500, content={
                "detail": f"could not build {recipe}: {type(e).__name__}: {e}"})
    else:
        return JSONResponse(status_code=400, content={
            "detail": "give ?recipe= or a JSON sequence body",
            "recipes": sd.RECIPES})

    view = _view(rig, built)
    if not built["lint"].ok:
        return JSONResponse(status_code=422, content={
            "detail": "lint FAILED: refusing to load", **view})
    state = _armer_state()
    if state in ACTIVE_STATES:
        return JSONResponse(status_code=409, content={
            "detail": f"armer is {state}: disarm (or wait for COMPLETE) before "
                      "sideloading", **view})
    async with _load_lock:
        base = rig_config(cfg, rig).nina_base_url
        tree, err = await sd.read_sequence_state(base)
        if err:
            return JSONResponse(status_code=502, content={
                "detail": f"cannot read NINA's sequence state ({err}): refusing "
                          "to load blind", **view})
        running = sd.nina_running(tree)
        if running:
            return JSONResponse(status_code=409, content={
                "detail": "NINA is running a sequence (" + ", ".join(running[:5])
                          + "): stop it in NINA first", **view})
        if rig == PIGGYBACK and recipe:
            sm = await sd.safety_connected(base)
            if sm is not True:
                return JSONResponse(status_code=409, content={
                    "detail": "NINA #2 does not report the safety monitor "
                              "connected, so the companion's lights and darks "
                              "would not be roof-gated. Connect it in NINA #2 "
                              "and retry.", **view})
        path = sd.sideload_path(_seq_dir(), rig, built["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sd.sequence_text(built["seq"]), encoding="utf-8")
        t0 = datetime.now()
        res = await sd.load_only(base, built["seq"])
        validation = None
        if res["ok"]:
            validation = await _validate_load(cfg, rig, base, t0)
            if validation is not None and not validation["ok"]:
                fatal = bool(validation["errors"])
                res = {"ok": not fatal, "detail": res["detail"] + (
                    ". NINA REJECTS it: " if fatal else ". NINA flags: ")
                    + validation["detail"] + (
                    ". Start in NINA will do nothing: fix and reload."
                    if fatal else "")}
        sd.record_event(cfg, rig, built["name"], str(path), recipe,
                        res["ok"], res["detail"])
    body = {"ok": res["ok"], "detail": res["detail"], "file": str(path),
            "loaded_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "started": False, "note": NOTE, "validation": validation, **view}
    if res["ok"]:
        return body
    # PS-132: loaded, but NINA's validation failed (422); else the load failed
    return JSONResponse(status_code=422 if validation else 502, content=body)


async def _validate_load(cfg, rig: str, base: str, since):
    """PS-132: NINA's validation of the sequence just loaded (None when
    nina_load_validation is off); pushes once when it finds problems."""
    from photonscript.scheduler import nina_validation as nv
    if nv.mode(cfg) == "off":
        return None
    v = await nv.check_loaded(base, cfg, rig, since=since)
    if not v["ok"]:
        await nv.alert(cfg, rig, v, "sideload")
    return v
