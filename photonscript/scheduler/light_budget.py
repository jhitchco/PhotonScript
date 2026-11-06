"""PS-117 part (b): one target's light budget, from its sub records.

GET /api/target/light-budget?name=&rig=&filter= (routers/targets.py) and
the Targets page "Light budget" section. Per rig + filter the target was
shot with:

  sky          p10 / median / p90 sky e-/s per pixel (sky_e_s, the G channel
               on the Piggy-600) and per 2x2 pixel, from the subs' records
  camera       the rig's constants (exposure_analysis.CameraModel)
  overhead     per-sub gap from consecutive subs of one length (DATE-OBS)
  acceptance   accepted share per sub length
  table        per candidate length: RN penalty, sky / RN^2, and with a
               feature signal SNR per sub / per hour, hours to the goal
  recommend    the advisory length with plain-language reasons
  progress     SNR at the feature from the accepted subs, and the share of
               the light the goal needs ((SNR now / goal)^2)

Advisory only (Choice D): nothing here changes a plan or a sequence.
"""

from __future__ import annotations

from statistics import median

from photonscript.shared import exposure_analysis as ea

ADVISORY = ("advisory: the goal stays in seconds; the SNR share is ideal "
            "shot noise (gradients and flats limit the real stack first)")


def _project(projects, name: str):
    from photonscript.scheduler.sub_index import _match_target
    from photonscript.shared.target_names import target_key
    k = _match_target(name, projects)
    return next((p for p in projects if target_key(p.target.name) == k), None)


def _readout(rows) -> str | None:
    vals = [r.get("readout") for r in rows if r.get("readout")]
    return max(set(vals), key=vals.count) if vals else None


def _sky_sp(r) -> float | None:
    m = r["metrics"]
    return ea.superpixel_sky(m.get("sky_e_s"), m.get("sky_e_s_ch"))


def group_budget(config, rig: str, flt: str, rows: list[dict], *,
                 signal: float | None, goal_snr: float,
                 lengths=ea.CANDIDATE_LENGTHS) -> dict:
    """The light budget of one rig + filter from its sub rows
    (sub_index.rows)."""
    from photonscript.shared.rigs import rig_label
    cam = ea.CameraModel.from_config(config, rig, readout=_readout(rows))
    sky_rows = [r for r in rows if r["metrics"].get("sky_e_s") is not None]
    sky = ea.percentiles([r["metrics"]["sky_e_s"] for r in sky_rows])
    sky_sp = ea.percentiles([_sky_sp(r) for r in sky_rows])
    over = ea.overhead_from_subs([(r["date"], r["time"], r["exp_s"])
                                  for r in rows])
    acc = ea.acceptance_by_length([(r["exp_s"], r["verdict"] != "rejected")
                                   for r in rows])
    sat: dict = {}
    for r in rows:
        v = r["metrics"].get("sat_stars_pct")
        if v is not None and r["exp_s"]:
            sat.setdefault(int(round(r["exp_s"])), []).append(v)
    sat_med = {t: round(median(v), 2) for t, v in sat.items()}
    out = {"rig": rig, "rig_label": rig_label(config, rig), "filter": flt,
           "subs": len(rows), "subs_with_sky": len(sky_rows),
           "camera": cam.as_dict(), "sky_e_s": sky, "sky_sp_e_s": sky_sp,
           "overhead": over, "acceptance": acc,
           "acceptance_pooled": None,
           "signal_e_s": signal, "goal_snr": goal_snr,
           "table": [], "recommendation": None, "headline": "",
           "progress": None, "sky_limited_s": None}
    if not sky:
        out["headline"] = ("no sky measurement yet: subs graded before "
                           "PS-117 get one from `qa-rescore --apply` "
                           "(approximate) or a re-grade")
        return out
    s_med = sky["p50"]
    sp_med = sky_sp["p50"] if sky_sp else 4.0 * s_med
    out["sky_limited_s"] = {
        str(int(p)): ea._r(ea.sky_limited_length(s_med, cam, p), 0)
        for p in (5.0, 10.0)}
    used_acc = {t: d["share"] for t, d in acc.items() if d["used"]}
    table = ea.length_table(cam, s_med, sp_med, signal=signal,
                            goal_snr=goal_snr, lengths=lengths,
                            overhead_s=over["used_s"], acceptance=used_acc,
                            sat_stars=sat_med,
                            measured={t: d["n"] for t, d in acc.items()})
    n_all = sum(d["n"] for d in acc.values())
    pooled = (sum(d["accepted"] for d in acc.values()) / n_all
              if n_all >= ea.MIN_ACCEPT_N else None)
    rec = ea.recommend_length(table, cam, default_acceptance=pooled)
    out.update(table=table, recommendation=rec, headline=ea.headline(rec),
               acceptance_pooled=None if pooled is None else round(pooled, 3))
    if signal:
        accepted = [(r["exp_s"], _sky_sp(r)) for r in rows
                    if r["verdict"] != "rejected"]
        out["progress"] = ea.progress(accepted, signal, goal_snr, cam,
                                      default_sky_sp=sp_med)
    return out


def target_light_budget(config, name: str, projects=(), rig: str = "",
                        filter: str = "") -> dict:  # noqa: A002
    """Every rig + filter group of one target (or the one asked for)."""
    from photonscript.scheduler import sub_index
    projects = list(projects or ())
    rows = sub_index.rows(config, projects, target=name, rig=rig or None,
                          filter=filter or None)
    proj = _project(projects, name)
    goal_snr = float((proj.goal_snr if proj is not None else None)
                     or getattr(config, "light_budget_goal_snr",
                                ea.DEFAULT_GOAL_SNR) or ea.DEFAULT_GOAL_SNR)
    signal = proj.feature_signal_e_s if proj is not None else None
    feature_rig = None
    if proj is not None:
        feature_rig = proj.feature_rig or proj.driving_rig
    groups: dict = {}
    for r in rows:
        if r["hdr_short"]:
            continue    # an HDR short set is not the light budget's sub
        groups.setdefault((r["rig"], r["filter"]), []).append(r)
    out = []
    for (rg, flt), rs in sorted(groups.items()):
        sig = signal if (signal and rg == feature_rig) else None
        out.append(group_budget(config, rg, flt, rs, signal=sig,
                                goal_snr=goal_snr))
    return {"target": rows[0]["target"] if rows else name,
            "project": proj is not None,
            "goal_snr": goal_snr,
            "feature": {"signal_e_s": signal, "rig": feature_rig,
                        "note": proj.feature_note if proj is not None else ""},
            "groups": out, "note": ADVISORY}
