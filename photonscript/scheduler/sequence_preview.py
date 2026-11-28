"""Tonight's sequence for the current arm, checked (PS-205). Read only.

Why: before a restart Claude verified the rebuilt plan by fetching
/api/tonight/sequence.json and grepping it for StartGuiding. This makes
that one answer: GET /api/tonight/sequence-check (scheduler/app.py) builds
tonight's JSON exactly as the download does (the ARMED guiding mode and
dither), runs sequence_lint and the PS-197 dispatch gate on it, and lists
the targets; `photonscript sequence-preview [--check]` prints it (remote
CLI read command).

    build(content, targets, guided=, unguided_dither=)   the JSON answer
    format_text(d, check=)                               the CLI text

Nothing is loaded into NINA, saved or sent.
"""
from __future__ import annotations

import json
from datetime import datetime

from photonscript.scheduler import dispatch_gate as dg


def _iso(v) -> str | None:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(v) if v else None


def _filter(e) -> str:
    f = getattr(e, "filter_type", None)
    return str(getattr(f, "value", f) or "?")


def target_rows(targets, seq: dict) -> list[dict]:
    """One row per planned target: window, exposures, and the StartGuiding
    items its container in the generated JSON holds."""
    sg = {}
    for c in dg.target_containers(seq):
        sg[dg._target_name(c)] = sg.get(dg._target_name(c), 0) + len(
            dg.start_guiding_items(c))
    out = []
    for t in targets or []:
        exps = []
        for e in getattr(t, "exposures", None) or []:
            x = {"filter": _filter(e), "seconds": getattr(e, "exposure_seconds", None),
                 "count": getattr(e, "count", None), "binning": getattr(e, "binning", None)}
            if getattr(e, "hdr_short_count", 0):
                x["hdr_short"] = [e.hdr_short_seconds, e.hdr_short_count]
            exps.append(x)
        kind = next((k for k in ("tracking_test", "optics_test", "tpoint_mapping",
                                 "focus_calibration", "carrier")
                     if getattr(t, k, False)), None)
        out.append({"name": t.name, "rig": getattr(t, "driving_rig", None),
                    "kind": kind,
                    "window_start_utc": _iso(getattr(t, "window_start_utc", None)),
                    "window_end_utc": _iso(getattr(t, "window_end_utc", None)),
                    "handoff_utc": _iso(getattr(t, "handoff_utc", None)),
                    "fill_from_utc": _iso(getattr(t, "fill_from_utc", None)),
                    "fill_end_utc": _iso(getattr(t, "fill_end_utc", None)),
                    "start_guiding": bool(getattr(t, "start_guiding", False)),
                    "unguided_filters": list(getattr(t, "unguided_filters", None) or []),
                    "start_guiding_items": sg.get(t.name, 0),
                    "exposures": exps})
    return out


def dither_mode(guided: bool, unguided_dither: bool) -> str:
    if guided:
        return "PHD2 dithers (guided)"
    if unguided_dither:
        return "NINA Direct Guider dithers (unguided)"
    return "no dithers (unguided)"


def build(content: str, targets, *, guided: bool, unguided_dither: bool = False,
          name: str | None = None, armer_state: str = "", now_mode: bool = False,
          ) -> dict:
    """The check for one generated night (the JSON text and its planned
    targets). ok = the dispatch gate passes (sequence_lint ERRORs count,
    as at the armer's load). Never raises on a bad sequence."""
    from photonscript.scheduler.sequence_lint import lint as _lint
    try:
        seq = json.loads(content)
    except (TypeError, ValueError) as e:
        seq, lint_findings, lint_errors = {}, [], [f"not JSON: {e}"]
    else:
        try:
            res = _lint(seq, guided=guided, unguided_dither=unguided_dither)
            lint_findings = [{"level": f.level, "rule": f.rule, "detail": f.detail}
                             for f in res.findings]
        except Exception as e:  # noqa: BLE001 - a broken lint is an error
            lint_findings = [{"level": "ERROR", "rule": "lint", "detail": str(e)}]
        lint_errors = [f"[{f['rule']}] {f['detail']}" for f in lint_findings
                       if f["level"] == "ERROR"]
    rows = guided_targets = None
    try:
        rows = target_rows(targets, seq)
        guided_targets = [r["name"] for r in rows if r["start_guiding"]]
    except Exception:  # noqa: BLE001
        rows = rows or []
    gate = dg.check(seq, guided=guided, unguided_dither=unguided_dither,
                    guided_targets=guided_targets if guided else None,
                    lint_errors=lint_errors)
    return {"ok": gate["ok"], "name": name, "armer_state": armer_state,
            "guided": bool(guided), "unguided_dither": bool(unguided_dither),
            "dither": dither_mode(guided, unguided_dither), "now_mode": bool(now_mode),
            "lint": {"ok": not lint_errors, "errors": len(lint_errors),
                     "warnings": sum(1 for f in lint_findings if f["level"] == "WARN"),
                     "findings": lint_findings},
            "gate": gate, "targets": rows,
            "start_guiding": gate.get("start_guiding"),
            "guider_connect": gate.get("guider_connect")}


def _hm(v) -> str:
    return str(v)[11:16] if v and len(str(v)) >= 16 else "?"


def _sec(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return "?"


def format_text(d: dict, check: bool = False) -> str:
    """The CLI text: header, targets, guiding items, lint, and with check
    the dispatch gate verdict."""
    lines = [f"Tonight's sequence {d.get('name') or '?'} for the current arm "
             f"(armer {d.get('armer_state') or '?'}): "
             f"{'GUIDED' if d.get('guided') else 'UNGUIDED'}, {d.get('dither')}"
             + (" [now mode: no dusk gate]" if d.get("now_mode") else "")]
    rows = d.get("targets") or []
    lines.append(f"Targets ({len(rows)}):")
    for r in rows:
        win = f"{_hm(r.get('window_start_utc'))}-{_hm(r.get('window_end_utc'))}Z"
        if r.get("handoff_utc"):
            win += f" (handoff {_hm(r['handoff_utc'])}Z)"
        if r.get("fill_from_utc"):
            win += f" + fill {_hm(r['fill_from_utc'])}-{_hm(r.get('fill_end_utc'))}Z"
        exps = ", ".join(f"{x['filter']} {x.get('count')}x{_sec(x.get('seconds'))}s"
                         + (f" bin{x['binning']}" if x.get("binning") not in (None, 1) else "")
                         for x in r.get("exposures") or []) or "no exposures"
        extra = []
        if r.get("kind"):
            extra.append(r["kind"])
        if r.get("rig") and r["rig"] != "rc16":
            extra.append(f"centers {r['rig']}")
        guide = (f"guided, {r.get('start_guiding_items')} StartGuiding"
                 if r.get("start_guiding") else
                 f"unguided, {r.get('start_guiding_items')} StartGuiding")
        if r.get("unguided_filters"):
            guide += f", unguided blocks {', '.join(r['unguided_filters'])}"
        lines.append(f"  {r.get('name')}: {win}; {exps}; {guide}"
                     + (f" [{', '.join(extra)}]" if extra else ""))
    lines.append(f"Guiding items: {d.get('start_guiding')} StartGuiding, guider connect "
                 f"{'yes' if d.get('guider_connect') else 'no'}")
    lint = d.get("lint") or {}
    lines.append(f"Lint: {'PASS' if lint.get('ok') else 'FAIL'} "
                 f"({lint.get('errors', 0)} error(s), {lint.get('warnings', 0)} warning(s))")
    for f in lint.get("findings") or []:
        if f.get("level") == "ERROR" or check:
            lines.append(f"  {f.get('level'):5s} [{f.get('rule')}] {f.get('detail')}")
    if check:
        g = d.get("gate") or {}
        lines.append(f"Dispatch gate: {g.get('line')}")
        for e in g.get("errors") or []:
            lines.append(f"  ERROR {e}")
        for n in g.get("notes") or []:
            lines.append(f"  note  {n}")
    return "\n".join(lines)
