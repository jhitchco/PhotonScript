"""PS-176: when the RC16 autofocuses inside a target.

2026-10-07 (M31 LRGB, dusk 02:10Z to dawn 11:56Z): the shutter was open
5.4 h of 9.8 h. 28 Run Autofocus items took 118 min, one at every filter
block (PS-65: every block runs AF on the AF filter, then its offset), and
the M31 plan switched L, R, G, B every few minutes.

config.rc16_af_policy:
  every_block  today's sequence (the default until a supervised night).
  smart        a block whose filter has a MEASURED offset from the AF filter
               (the PS-76 focus model: >= MIN_OFFSET_AFS AF runs in that
               filter and in the AF filter) moves the focuser by that offset
               and back at the block end (MoveFocuserRelative, no AF). A full
               AF runs at target start, on the block triggers (temperature
               change, HFR rise, every rc16_af_interval_min) and on every
               block of a 3 nm filter whose offset is not measured yet (it
               keeps the every_block recipe: AF on the AF filter + the
               configured offset). An unmeasured broadband filter moves by
               its configured offset (0 when unlisted). Offsets come from
               the model, else focus_filter_offsets (PS-144).

This module only decides; nina_sequence_json builds the blocks and
shutter_efficiency estimates what each policy costs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

EVERY_BLOCK = "every_block"
SMART = "smart"
POLICIES = (EVERY_BLOCK, SMART)

# AF runs per filter (and in the reference filter) before the model's
# offset counts as measured. The model's per-filter scatter is about 20 to
# 40 steps on the RC16, so 3 runs put the offset's standard error well
# inside one critical focus zone.
MIN_OFFSET_AFS = 3

FILTERS = ("L", "R", "G", "B", "Ha", "OIII", "SII")
NARROWBAND = ("Ha", "OIII", "SII")


def policy(cfg) -> str:
    """The configured policy; anything unknown is every_block (safe)."""
    v = str(getattr(cfg, "rc16_af_policy", EVERY_BLOCK) or EVERY_BLOCK)
    v = v.strip().lower().replace("-", "_")
    return v if v in POLICIES else EVERY_BLOCK


def _model(cfg) -> dict | None:
    try:
        from photonscript.scheduler.focus_model import load_model
        return load_model(cfg)
    except Exception as e:  # noqa: BLE001 - never break a night over this
        logger.warning("af_policy: focus model unavailable: %s", e)
        return None


def offset_table(cfg, ref: str, model: dict | None = None) -> dict:
    """{filter: {steps, measured, source, n, n_ref}} relative to the AF
    filter `ref`. measured = the focus model has >= MIN_OFFSET_AFS AF runs
    in the filter and in ref (source "model", steps = the model offset);
    else the configured focus_filter_offsets value (source "config") or 0
    (source "none"). The reference itself is 0 and measured."""
    model = _model(cfg) if model is None else model
    offs = {}
    if model and str(model.get("ref_filter") or "") == ref:
        offs = model.get("offsets") or {}
    try:
        conf = cfg.focus_offset_map()
    except Exception:  # noqa: BLE001
        conf = {}
    out = {}
    for f in FILTERS:
        if f == ref:
            out[f] = {"steps": 0, "measured": True, "source": "reference",
                      "n": None, "n_ref": None}
            continue
        mo = offs.get(f) or {}
        n, n_ref = int(mo.get("n") or 0), int(mo.get("n_ref") or 0)
        if mo.get("steps") is not None and n >= MIN_OFFSET_AFS \
                and n_ref >= MIN_OFFSET_AFS:
            out[f] = {"steps": int(mo["steps"]), "measured": True,
                      "source": "model", "n": n, "n_ref": n_ref}
        else:
            # configured offsets are relative to the AF filter as well
            steps = int(conf.get(f, 0)) - int(conf.get(ref, 0))
            out[f] = {"steps": steps, "measured": False,
                      "source": "config" if f in conf else "none",
                      "n": n, "n_ref": n_ref}
    return out


def smart_spec(cfg, af_filter, model: dict | None = None,
               force: bool = False) -> dict | None:
    """What the sequence needs for the smart policy, or None for
    every_block (the policy, or no AF filter configured: smart moves are
    relative to the AF filter's best focus, so it needs one). force: build
    it whatever the policy (the efficiency preview's smart column).
    {ref, offsets {filter: steps}, measured {filters}, table, temp_c,
    hfr_pct, interval_min}."""
    if policy(cfg) != SMART and not force:
        return None
    if af_filter is None:
        logger.warning("af_policy: smart needs autofocus_filter; using "
                       "every_block")
        return None
    ref = getattr(af_filter, "value", str(af_filter))
    table = offset_table(cfg, ref, model)

    def _f(key, default):
        try:
            return float(getattr(cfg, key, default))
        except (TypeError, ValueError):
            return float(default)
    measured = {f for f, r in table.items() if r["measured"]}
    return {"ref": ref,
            "offsets": {f: r["steps"] for f, r in table.items()},
            "measured": measured,
            # a 3 nm filter with no measured offset keeps an AF at every
            # block; broadband (an LRGB set is near parfocal: R measured -34
            # steps, a third of the 110-step CFZ) takes its configured
            # offset (0 when unlisted) with the HFR trigger as the guard
            "af_blocks": {f for f in NARROWBAND if f not in measured},
            "table": table,
            "temp_c": max(0.1, _f("rc16_af_temp_change_c", 2.0)),
            "hfr_pct": max(1.0, _f("rc16_af_hfr_increase_pct", 10.0)),
            "interval_min": max(0.0, _f("rc16_af_interval_min", 60.0))}


def describe(spec: dict | None) -> str:
    """One line for the sequence annotation and the preview."""
    if not spec:
        return "AF policy every_block: autofocus at every filter block"
    rows = []
    for f, r in spec["table"].items():
        if f == spec["ref"]:
            continue
        if r["measured"]:
            how = f"{r['steps']:+d} (model, {r['n']} AFs)"
        elif f in spec["af_blocks"]:
            how = (f"AF each block, then {r['steps']:+d} "
                   f"({r['source']}, not measured)")
        else:
            how = f"{r['steps']:+d} ({r['source']}, not measured)"
        rows.append(f"{f} {how}")
    timed = (f", every {spec['interval_min']:.0f} min"
             if spec["interval_min"] > 0 else "")
    return (f"AF policy smart (PS-176): AF on {spec['ref']} at target start, "
            f"after {spec['temp_c']:g} C, an HFR rise of "
            f"{spec['hfr_pct']:g}%{timed}; filter offsets from "
            f"{spec['ref']}: " + "; ".join(rows))
