"""PS-22: the `photonscript integrate` pipeline (desktop).

    select -> star QA -> calibration match -> stage (copy) -> manifest
    -> PJSR -> PixInsight integration -> PixInsight finish -> AstroBin packet

Every stage is timed into <run>/out/timing.csv (PixInsight's own per-stage
times go to <run>/out/timing_pi.csv).

Safety:
  * the Library mirror is READ-ONLY: frames are COPIED into a new staging
    run folder; copy_into() refuses any destination inside the Library;
  * every run gets a NEW folder (never overwrites an earlier run);
  * PixInsight is started only when no instance is running, one script at
    a time, its logs polled with short reads (runner.py).
"""

from __future__ import annotations

import csv
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from photonscript.integration import astrobin, calib, manifest, pjsr, runner, star_qa
from photonscript.integration.frames import Frame
from photonscript.integration.select import Selection, select_lights

PEDESTAL_DN = 1000
QA_MODES = ("apply", "report", "off")


class PipelineError(RuntimeError):
    pass


@dataclass
class Options:
    target: str
    rig: str = "piggyback"
    library: Path = Path(r"C:\Users\sleep\ninashare\Library")
    staging_root: Path = Path(r"D:\Astrophotography\Staging")
    out: Path | None = None
    since: str = ""
    until: str = ""
    filters: list[str] | None = None
    qa: str = "report"                # report (default, Jeremy 2026-10-06) | apply | off
    flats: bool = True
    min_darks: int = 10
    max_cal: int = 50
    limit: int = 0                    # >0: at most this many subs per group (smoke runs)
    run_pixinsight: bool = True
    finish: bool = True
    dry_run: bool = False
    pixinsight: str = runner.DEFAULT_EXE
    workers: int | None = None
    default_readout: str | None = None
    gradient: str = "auto"
    use_rc: bool = True
    bortle: int = 2
    site: dict = field(default_factory=dict)
    tz: str = "America/Denver"
    rig_label: str = ""
    thresholds: star_qa.Thresholds = field(default_factory=star_qa.Thresholds)
    trigger: dict = field(default_factory=dict)   # PS-31: why the watcher started this run
    hoo: bool = False                 # PS-161: OSC finish also writes <name>_hoo.{xisf,jpg}


def default_staging_root(cfg=None) -> Path:
    """integration_staging_root, else D:/Astrophotography/Staging when it
    exists, else ~/Astrophotography/Staging."""
    root = str(getattr(cfg, "integration_staging_root", "") or "") if cfg is not None else ""
    if root:
        return Path(root)
    d = Path(r"D:\Astrophotography\Staging")
    return d if d.is_dir() else Path.home() / "Astrophotography" / "Staging"


MIRROR_D = Path(r"D:\ninashare\Library")


def default_library(cfg=None) -> Path:
    """PS-161: the Library mirror the desktop reads. integration_library_dir
    when set; else the first that exists of D:/ninashare/Library (the mirror
    since 2026-10; C:/Users/sleep/ninashare becomes a junction to it),
    desktop_library_dir and ~/ninashare/Library; else desktop_library_dir
    (or D:/ninashare/Library) so the error names a real candidate."""
    explicit = str(getattr(cfg, "integration_library_dir", "") or "") if cfg is not None else ""
    if explicit:
        return Path(explicit)
    desk = str(getattr(cfg, "desktop_library_dir", "") or "") if cfg is not None else ""
    cands = [MIRROR_D] + ([Path(desk)] if desk else []) + [Path.home() / "ninashare" / "Library"]
    for c in cands:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return Path(desk) if desk else MIRROR_D


def config_options(cfg, rig: str) -> dict:
    """Options fields that come from the PhotonScript config (site, readout,
    labels): shared by `photonscript integrate` and integrate-watch."""
    from photonscript.shared.rigs import rig_label, rig_readout
    return dict(
        library=default_library(cfg),
        default_readout=rig_readout(cfg, rig),
        bortle=int(getattr(cfg, "observatory_bortle", 2)), tz=cfg.observatory_tz,
        rig_label=rig_label(cfg, rig),
        site={"name": cfg.observatory_name, "lat": round(cfg.observatory_lat, 3),
              "lon": round(cfg.observatory_lon, 3), "elev": int(cfg.observatory_elev),
              "bortle": cfg.observatory_bortle})


def safe_name(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", s).strip("_") or "target"


def default_run_dir(o: Options, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return Path(o.staging_root) / f"{safe_name(o.target)}_{o.rig}_{now:%Y%m%d-%H%M}"


def is_inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def copy_into(src: Path, dest_dir: Path, library: Path) -> Path:
    """Copy one frame into the staging run (never into the Library)."""
    dest_dir = Path(dest_dir)
    if is_inside(dest_dir, library):
        raise PipelineError(f"refusing to write inside the Library mirror: {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(src).name
    if dest.exists() and dest.stat().st_size == Path(src).stat().st_size:
        return dest
    shutil.copy2(src, dest)
    return dest


class Timer:
    """Stage wall times, rewritten to timing.csv after every stage."""

    def __init__(self, path: Path | None, echo=print):
        self.path, self.rows, self.echo = path, [], echo

    def stage(self, name: str):
        timer = self

        class _S:
            def __enter__(self):
                self.t0 = time.time()
                self.start = datetime.now(timezone.utc)
                timer.echo(f"== {name}")
                return self

            def __exit__(self, *exc):
                mins = round((time.time() - self.t0) / 60.0, 2)
                end = datetime.now(timezone.utc)
                timer.rows.append([name, self.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   end.strftime("%Y-%m-%dT%H:%M:%SZ"), mins,
                                   "error" if exc[0] else "ok"])
                timer.flush()
                return False
        return _S()

    def flush(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", newline="", encoding="ascii") as f:
            w = csv.writer(f)
            w.writerow(["stage", "start_utc", "end_utc", "minutes", "status"])
            w.writerows(self.rows)


# ----------------------------------------------------------------- helpers

def group_dir(f: Frame) -> str:
    return f"LIGHTS/{safe_name(f.filter or 'NOFILTER')}/{f.exp_key}"


def nina_tag(exp: float) -> str:
    """The exposure part of a NINA file name: _120.00s_ (group counts)."""
    return f"_{exp:.2f}s_"


def reference_per_filter(rows: list[dict], kept: set[str]) -> dict[str, str]:
    """Per filter: the kept sub with the most stars among the sharper half
    (LocalNormalization reference for that stack)."""
    out = {}
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r["file"] in kept:
            by.setdefault(r["filter"], []).append(r)
    for filt, rs in by.items():
        h = sorted(x["hfd_px"] for x in rs if x.get("hfd_px") == x.get("hfd_px") and x.get("hfd_px"))
        med = h[len(h) // 2] if h else None
        cand = [x for x in rs if med is None or (x.get("hfd_px") or 0) <= med] or rs
        out[filt] = Path(max(cand, key=lambda x: x["stars"])["file"]).stem
    return out


def limit_per_group(lights: list[Frame], n: int) -> list[Frame]:
    """At most n subs per (filter, exposure) group, evenly spaced in time."""
    by: dict[tuple, list[Frame]] = {}
    for f in sorted(lights, key=lambda x: (x.date_obs, x.name)):
        by.setdefault((f.filter, round(f.exp, 2)), []).append(f)
    out = []
    for fs in by.values():
        if len(fs) <= n:
            out += fs
        else:
            out += [fs[int(i * len(fs) / n)] for i in range(n)]
    return sorted(out, key=lambda x: (x.date_obs, x.name))


def dropped_from_log(text: str) -> set[str]:
    """Base names the PJSR log reports as DROPPED at some stage."""
    out = set()
    for m in re.finditer(r"DROPPED at [^:]+: (\S+)", text):
        n = Path(m.group(1)).stem
        for sfx in ("_r", "_d", "_cc", "_c"):      # same order as baseOf() in the PJSR
            if n.endswith(sfx):
                n = n[: -len(sfx)]
        out.add(n)
    return out


def build_integrate_config(o: Options, run_dir: Path, stacks: list[dict], plan: calib.CalibrationPlan,
                           cfa: str, reference: str) -> dict:
    darks = [{"exp": e, "label": f"{e:g}s", "dir": f"DARKS/{e:g}s"} for e in plan.dark_masters()]
    flats = [{"filter": safe_name(f), "dir": f"FLATS/{safe_name(f)}"}
             for f, fc in plan.flats.items() if fc.frames]
    return {"staging": pjsr.fwd(run_dir), "out": pjsr.fwd(run_dir / "out"),
            "target": o.target, "rig": o.rig, "cfa": cfa, "pedestal": PEDESTAL_DN,
            "bias": {"dir": "BIAS"} if plan.bias else None,
            "darks": darks, "flats": flats, "stacks": stacks, "reference": reference}


def catalog_coords(target: str) -> tuple[float | None, float | None]:
    try:
        from photonscript.shared.astronomy import find_catalog_entry
        e = find_catalog_entry(target)
        if e:
            return float(e["ra"]) * 15.0, float(e["dec"])
    except Exception:  # noqa: BLE001
        pass
    return None, None


# ----------------------------------------------------------------- run

def run(o: Options, echo=print) -> dict:
    if o.qa not in QA_MODES:
        raise PipelineError(f"--qa must be one of {', '.join(QA_MODES)}")
    run_dir = Path(o.out) if o.out else default_run_dir(o)
    if is_inside(run_dir, o.library):
        raise PipelineError(f"the run folder must be outside the Library mirror: {run_dir}")
    if not o.dry_run:
        if run_dir.exists() and any(run_dir.iterdir()):
            raise PipelineError(f"{run_dir} exists and is not empty: every run gets a NEW folder "
                                "(pass another --out or leave it out)")
        run_dir.mkdir(parents=True, exist_ok=True)
    out_dir = run_dir / "out"
    timer = Timer(None if o.dry_run else out_dir / "timing.csv", echo)
    result: dict = {"run_dir": str(run_dir), "dry_run": o.dry_run}

    with timer.stage("select lights from the Library"):
        sel: Selection = select_lights(o.library, o.target, o.rig, since=o.since, until=o.until,
                                       filters=o.filters, tz=o.tz)
        groups = sel.groups()
        echo(f"  folders: {', '.join(sel.folders) or 'none'}; {len(sel.lights)} lights "
             + "(" + ", ".join(f"{k[0]} {k[1]} x{len(v)}" for k, v in groups.items()) + ")")
        for p, why in sel.skipped[:10]:
            echo(f"  skipped {Path(p).name}: {why}")
    if not sel.lights:
        raise PipelineError(f"no approved {o.rig} lights for {o.target!r} in {o.library}")

    rows, ref_i = [], None
    qa_by_file: dict[str, dict] = {}
    if o.qa != "off":
        with timer.stage(f"star QA ({len(sel.lights)} subs)"):
            rows, ref_i = star_qa.run_qa(sel.lights, workers=o.workers, thr=o.thresholds, echo=echo)
            qa_by_file = {r["file"]: r for r in rows}
            if not o.dry_run:
                star_qa.write_csv(run_dir / "qa" / "star_qa.csv", rows)
            nrej = sum(r["action"] == "reject" for r in rows)
            echo(f"  reference {rows[ref_i]['file']}; {len(rows) - nrej} keep, {nrej} reject"
                 + (" (report only: all staged)" if o.qa == "report" else ""))
    if o.qa == "apply":
        lights = [f for f in sel.lights if qa_by_file.get(f.name, {}).get("action", "keep") == "keep"]
    else:
        lights = list(sel.lights)
    if o.limit > 0:
        lights = limit_per_group(lights, o.limit)
        echo(f"  --limit {o.limit}: {len(lights)} subs kept for a small run")

    with timer.stage("match calibration"):
        instrument = Counter(f.instrument for f in lights).most_common(1)[0][0]
        cals, cal_skipped = calib.scan(o.library, instrument=instrument)
        plan = calib.plan(lights, cals, default_readout=o.default_readout, use_flats=o.flats,
                          min_darks=o.min_darks, max_frames=o.max_cal)
        lights, off = calib.lights_in_epoch(lights, plan.epoch, o.default_readout)
        for f, why in off:
            plan.notes.append(f"light {f.name} left out: {why} (not the stack's epoch)")
        for n in plan.notes:
            echo("  " + n)
    if not lights:
        raise PipelineError("no lights left after QA and the epoch check")

    kept_names = {f.name for f in lights}
    reference = ""
    if rows and ref_i is not None and rows[ref_i]["file"] in kept_names:
        reference = Path(rows[ref_i]["file"]).stem
    per_filter_ref = reference_per_filter(rows, kept_names) if rows else {}
    if not reference and per_filter_ref:
        # the QA reference was left out (--limit, epoch): best kept sub of
        # the filter that holds the longest subs
        reference = per_filter_ref.get(max(lights, key=lambda f: f.exp).filter, "")
    cfa = (lights[0].bayer or "RGGB").upper() if lights[0].is_osc else ""

    # stacks: OSC = one stack of every exposure; mono = one per filter
    stacks = []
    by_filter: dict[str, list[Frame]] = {}
    for f in lights:
        by_filter.setdefault(f.filter, []).append(f)
    for filt in sorted(by_filter):
        exps = sorted({round(f.exp, 2) for f in by_filter[filt]})
        grp = []
        for e in exps:
            dc = plan.darks.get(e)
            sample = next(f for f in by_filter[filt] if round(f.exp, 2) == e)
            grp.append({"name": sample.exp_key, "exp": e, "dir": group_dir(sample), "tag": nina_tag(e),
                        "dark": (dc.dark_exp if dc and dc.frames else None),
                        "optimize": bool(dc and dc.scaled)})
        stacks.append({"name": safe_name(filt), "filter": safe_name(filt), "groups": grp,
                       "reference": per_filter_ref.get(filt, "")})
    if reference:
        ref_filter = next((f.filter for f in lights if f.base == reference), "")
        stacks.sort(key=lambda s: s["filter"] != safe_name(ref_filter))

    entries = []
    staged_files: dict[str, str] = {}
    if not o.dry_run:
        with timer.stage("stage (copy into the run folder)"):
            n = 0
            for f in sel.lights:
                qa = qa_by_file.get(f.name)
                if f.name in kept_names:
                    d = copy_into(f.path, run_dir / group_dir(f), o.library)
                    staged_files[f.name] = str(d)
                    n += 1
                entries.append(manifest.frame_entry(f, "light", f"{f.filter} {f.exp_key}",
                                                    staged_files.get(f.name, ""), qa))
            for b in plan.bias:
                d = copy_into(b.path, run_dir / "BIAS", o.library)
                entries.append(manifest.frame_entry(b, "bias", "BIAS", str(d)))
            for e in plan.dark_masters():
                for df in plan.dark_frames(e):
                    d = copy_into(df.path, run_dir / "DARKS" / f"{e:g}s", o.library)
                    entries.append(manifest.frame_entry(df, "dark", f"DARK {e:g}s", str(d)))
            for filt, fc in plan.flats.items():
                for ff in fc.frames:
                    d = copy_into(ff.path, run_dir / "FLATS" / safe_name(filt), o.library)
                    entries.append(manifest.frame_entry(ff, "flat", f"FLAT {filt}", str(d)))
            if reference:
                (run_dir / "reference.txt").write_text(reference + "\n", encoding="ascii")
            echo(f"  copied {n} lights, {len(plan.bias)} bias, "
                 f"{sum(len(plan.dark_frames(e)) for e in plan.dark_masters())} darks, "
                 f"{sum(len(fc.frames) for fc in plan.flats.values())} flats into {run_dir}")

    calib_summary = {
        "epoch": plan.epoch.label(), "notes": plan.notes,
        "bias": len(plan.bias),
        "darks": [{"light_exp": d.light_exp, "dark_exp": d.dark_exp, "n": len(d.frames),
                   "scaled": d.scaled, "note": d.note} for d in plan.darks.values()],
        "flats": [{"filter": k, "session": v.session, "n": len(v.frames), "note": v.note}
                  for k, v in plan.flats.items()],
        "skipped_sessions": cal_skipped[:50],
    }
    reasons = Counter()
    for r in rows:
        if r["action"] == "reject":
            for part in r["reason"].split(";"):
                reasons[part.split("(")[0]] += 1
    qa_summary = {"mode": o.qa, "kept": sum(1 for r in rows if r["action"] == "keep"),
                  "rejected": sum(1 for r in rows if r["action"] == "reject"),
                  "reasons": reasons.most_common(), "reference": reference,
                  "thresholds": star_qa.thresholds_dict(o.thresholds)}
    run_info = {"target": o.target, "rig": o.rig, "run_name": run_dir.name, "run_dir": str(run_dir),
                "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "options": {"since": o.since, "until": o.until, "qa": o.qa, "flats": o.flats,
                            "library": str(o.library), "filters": o.filters}}
    result.update(lights_selected=len(sel.lights), lights_staged=len(lights), qa=qa_summary,
                  calibration=calib_summary, stacks=stacks)
    if o.dry_run:
        echo("dry run: nothing written, PixInsight not started")
        return result

    with timer.stage("manifest"):
        man = manifest.build(run_info, entries, calib_summary, qa_summary)
        manifest.write(run_dir, man)

    with timer.stage("generate PixInsight scripts"):
        icfg = build_integrate_config(o, run_dir, stacks, plan, cfa, reference)
        integ_js = pjsr.write(run_dir / "integrate_run.js",
                              pjsr.render(pjsr.template(pjsr.INTEGRATE_TEMPLATE), icfg))
        block, missing = pjsr.solver_include(o.pixinsight)
        if missing:
            echo(f"  ImageSolver files missing ({', '.join(missing)}): the finish skips the plate solve")
        ra, dec = catalog_coords(o.target)
        g0 = lights[0]
        # PS-22/PS-46: the finish is deploy/finish_osc.js, the same script
        # run-finish-osc.ps1 runs (SPCC with Gaia DR3/SP, NR + deconvolution,
        # StarNet2 star reduction, a steps json per master), at its defaults.
        gx = pjsr.find_graxpert()
        fcfg = {"out": pjsr.fwd(out_dir), "staging": pjsr.fwd(run_dir), "ra_deg": ra, "dec_deg": dec,
                "focal_mm": g0.focallen or 600.0, "pixel_um": g0.pixel_um or 3.76,
                "gradient": o.gradient, "use_rc": o.use_rc,
                "pi_library": pjsr.fwd(Path(o.pixinsight).parent.parent / "library"),
                "graxpert": gx, "graxpert_version": "unknown" if gx else "",
                "hoo": "on" if (o.hoo and cfa) else "off",   # PS-161: OSC only
                "masters": [{"name": f"{safe_name(o.target)}_{s['name']}",
                             "path": pjsr.fwd(out_dir / "master" / f"master_{s['name']}.xisf")}
                            for s in stacks]}
        finish_js = pjsr.write(run_dir / "finish_run.js", pjsr.render_finish(fcfg, block))
        echo(f"  {integ_js.name}, {finish_js.name}")
    result["scripts"] = [str(integ_js), str(finish_js)]

    pi_ok = False
    final_img = ""
    dropped: set[str] = set()
    if o.run_pixinsight:
        with timer.stage("PixInsight integration (launch to exit)"):
            r = runner.run_script(integ_js, out_dir / "pipeline.log", exe=o.pixinsight, echo=echo)
            echo(f"  integration: {'EXIT OK' if r['ok'] else 'FAILED'} in {r['minutes']} min ({r['last_line']})")
            result["integration"] = r
            pi_ok = r["ok"]
            log_txt, _ = runner.read_new(out_dir / "pipeline.log", 0)
            dropped = dropped_from_log(log_txt)
        if pi_ok and o.finish:
            with timer.stage("PixInsight finish (launch to exit)"):
                r2 = runner.run_script(finish_js, out_dir / "finish.log", exe=o.pixinsight, echo=echo)
                echo(f"  finish: {'EXIT OK' if r2['ok'] else 'FAILED'} in {r2['minutes']} min")
                result["finish"] = r2
                jpgs = sorted((out_dir / "final").glob("*_final.jpg"))
                final_img = str(jpgs[0]) if jpgs else ""
    else:
        echo("  PixInsight not started (--no-pixinsight). Run it later with:")
        echo(f'    "{o.pixinsight}" -n --automation-mode --run={integ_js} --force-exit')

    with timer.stage("AstroBin packet draft"):
        integrated = [f for f in lights if not (pi_ok and f.base in dropped)]
        darks_for = {d.light_exp: len(d.frames) for d in plan.darks.values()}
        flats_for = {k: len(v.frames) for k, v in plan.flats.items()}
        rows_csv = astrobin.acquisition_rows(integrated, darks_for=darks_for, flats_for=flats_for,
                                             bias=len(plan.bias), bortle=o.bortle)
        stamp = datetime.now().strftime("%Y-%m-%d")
        ab = run_dir / "astrobin"
        ab.mkdir(exist_ok=True)
        tname = safe_name(o.target)
        csv_path = ab / f"{tname}_{stamp}_astrobin_acquisition.csv"
        csv_path.write_bytes(astrobin.csv_text(rows_csv).encode("ascii"))
        issues = []
        if not any(fc.frames for fc in plan.flats.values()):
            issues.append("No flats: vignetting and dust shadows remain.")
        for d in plan.darks.values():
            if d.scaled:
                issues.append(f"{d.light_exp:g} s subs calibrated with {d.dark_exp:g} s darks scaled by "
                              f"dark optimization; matched darks would remove this.")
            elif not d.frames:
                issues.append(f"{d.light_exp:g} s subs have no dark ({d.note}).")
        if not plan.bias:
            issues.append("No bias frames matched.")
        if len({f.night for f in integrated}) > 1:
            issues.append("Several nights: framing can differ between nights; the stack keeps the overlap.")
        if dropped:
            issues.append(f"{len(dropped)} frame(s) dropped inside PixInsight (see out/pipeline.log).")
        if o.qa == "report":
            issues.append("Star QA ran in report mode: rejected subs were stacked anyway.")
        g0 = integrated[0] if integrated else lights[0]
        info = {
            "target": o.target, "rig_label": o.rig_label or o.rig, "run_name": run_dir.name,
            "run_dir": str(run_dir), "built": run_info["built"], "rows": rows_csv,
            "integrated": len(integrated), "total_s": sum(f.exp for f in integrated),
            "staged": len(sel.lights), "qa": qa_summary,
            "calibration": {"bias": len(plan.bias),
                            "darks": [(d.light_exp, len(d.frames), d.dark_exp or 0, d.scaled)
                                      for d in plan.darks.values()],
                            "flats": [(k, len(v.frames), v.note) for k, v in plan.flats.items()],
                            "notes": plan.notes},
            "equipment": {"camera": g0.instrument or "?",
                          "optics": (f"{g0.focallen:g} mm f/{g0.focratio:g}"
                                     if g0.focallen and g0.focratio else "?"),
                          "mount": g0.telescope or "not in the header",
                          "software": g0.software or "?"},
            "location": o.site, "integrated_by_pi": pi_ok, "final": final_img,
            "processing": ("calibration (bias, darks" + (", flats" if flats_for and any(flats_for.values()) else "")
                           + "), CosmeticCorrection, " + ("VNG debayer, " if cfa else "")
                           + "StarAlignment with distortion correction, LocalNormalization, "
                           "ImageIntegration with PSF Signal Weight and "
                           + ("Winsorized sigma clipping, " if len(integrated) >= 15 else "sigma clipping, ") +
                           "GradientCorrection, plate solve, color calibration, linked stretch."),
            "timing": [(r[0], r[3]) for r in timer.rows],
            "known_issues": issues,
        }
        md_path = ab / f"{tname}_{stamp}_astrobin_packet.md"
        md_path.write_bytes(astrobin.packet_md(info).encode("ascii"))
    timer.flush()
    man = manifest.read(run_dir)
    man["timing"] = timer.rows
    man["integrated_by_pixinsight"] = pi_ok
    man["dropped_in_pixinsight"] = sorted(dropped)
    manifest.write(run_dir, man)
    result["timing"] = timer.rows

    # PS-33: the machine half of the integrator ledger (report.py posts it)
    from photonscript.integration import ledger as ledger_writer
    led = ledger_writer.build(
        target=o.target, rig=o.rig, run_dir=run_dir, options=run_info["options"],
        selected=sel.lights, staged=lights, integrated=integrated, dropped=dropped,
        qa=qa_summary, calibration=calib_summary, integration=result.get("integration"),
        finish=result.get("finish"), timing=timer.rows, packet=str(md_path), csv=str(csv_path),
        final=final_img, trigger=o.trigger or None)
    result["ledger"] = str(ledger_writer.write_for_run(led, run_dir, o.staging_root))
    result["ledger_version"] = led.version
    result["hours_integrated"] = round(led.hours, 3)
    echo(f"done: {run_dir}")
    return result
