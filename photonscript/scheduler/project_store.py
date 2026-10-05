"""Persistent imaging-project store + time-budget filter allocation.

Projects live in <data_dir>/projects.json and survive restarts. Each project
carries a priority (0-100) and an hour budget; PhotonScript allocates the
budget across filters automatically based on the target type:

  narrowband (nebulae/remnants):  Ha 35% / OIII 30% / SII 35%  @ 300s
  broadband (galaxies/clusters):  L 50% / R 16.7% / G 16.7% / B 16.7% @ 180s

Community best practice (Cloudy Nights / Starizona consensus): L carries
detail so ~50% when time-limited; SII is the faintest narrowband line and
deserves MORE time, not less — and the right SHO split is target-dependent,
which is why every project can carry its own filter_mix percentages.

Changing the budget or the mix re-allocates counts, preserving acquired subs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

logger = logging.getLogger(__name__)

# Exposure seconds come from config (nb_exposure_s / bb_exposure_s); the
# numbers here are fallbacks only.
NARROWBAND_MIX = [(FilterType.HA, 0.35, 600), (FilterType.OIII, 0.30, 600),
                  (FilterType.SII, 0.35, 600)]
BROADBAND_MIX = [(FilterType.LUMINANCE, 0.50, 180), (FilterType.RED, 1 / 6, 180),
                 (FilterType.GREEN, 1 / 6, 180), (FilterType.BLUE, 1 / 6, 180)]


def target_kind(target: CelestialTarget) -> str:
    obj = (target.object_type or "").lower()
    if any(kw in obj for kw in ("nebula", "remnant", "emission", "planetary")):
        return "narrowband"
    return "broadband"


def default_mix(kind: str) -> dict[str, float]:
    """Type-default split as {filter: percent}."""
    mix = NARROWBAND_MIX if kind == "narrowband" else BROADBAND_MIX
    return {f.value: round(frac * 100, 1) for f, frac, _ in mix}


# HDR short companion set: a small FIXED number of short subs ADDED as a light
# time overhead. Their exposure time is subtracted from the filter's budget
# first and the remainder becomes the long set, so total integration stays
# ~= budget and the deep long set is preserved (NOT gutted by carving sub count).
# Fixed-count so the short set never grows with the budget.
HDR_SHORT_COUNT = 12


def allocate_exposures(kind: str, budget_hours: float, config,
                       acquired: dict | None = None,
                       custom_mix: dict | None = None,
                       hdr: dict | None = None,
                       overrides: dict | None = None) -> list[ExposurePlan]:
    """Split an hour budget across filters. Preserves acquired counts.

    custom_mix: {filter_value: percent} — normalized; overrides type default.
    hdr: {filter_value: short_exposure_seconds} — for each listed filter, add a
        fixed HDR_SHORT_COUNT short subs; their time is subtracted from that
        filter's budget first and the remainder becomes the long set, so total
        integration stays ~= budget and the deep long set is preserved.
    overrides: {filter_value: long_exposure_seconds} — override the default
        long-sub length for specific filters (else config nb/bb defaults).
    """
    base = NARROWBAND_MIX if kind == "narrowband" else BROADBAND_MIX
    nb = {"Ha", "OIII", "SII"}

    def _exp(fv):
        if overrides and fv in overrides and overrides[fv]:
            return overrides[fv]
        return (getattr(config, "nb_exposure_s", 600) if fv in nb
                else getattr(config, "bb_exposure_s", 180))

    if custom_mix:
        total = sum(v for v in custom_mix.values() if v and v > 0) or 1
        mix = [(FilterType(fv), pct / total, _exp(fv))
               for fv, pct in custom_mix.items() if pct and pct > 0]
    elif overrides:
        # No custom mix but per-filter overrides -> honor overrides on the
        # type-default mix.
        mix = [(ftype, frac, _exp(ftype.value)) for ftype, frac, _ in base]
    else:
        mix = base
    acquired = acquired or {}
    hdr = hdr or {}
    plans = []
    for ftype, frac, exp_s in mix:
        filter_seconds = budget_hours * 3600 * frac
        short_seconds = hdr.get(ftype.value)
        short_count = 0
        if short_seconds:
            # Add the fixed short set as a light time overhead: subtract its time
            # from the budget, then size the long set from what remains, so total
            # integration stays ~= budget and the deep long set is preserved.
            short_count = HDR_SHORT_COUNT
            remaining = filter_seconds - short_count * short_seconds
            if remaining >= exp_s:
                long_count = max(1, round(remaining / exp_s))
            else:
                # Budget too small to justify HDR (no room for a real long set) —
                # go all long rather than let the short set dominate the budget.
                short_count = 0
                long_count = max(1, round(filter_seconds / exp_s))
        else:
            long_count = max(1, round(filter_seconds / exp_s))
        plans.append(ExposurePlan(
            filter_type=ftype, exposure_seconds=exp_s, count=long_count,
            gain=config.default_gain, offset=config.default_offset,
            acquired=min(acquired.get(ftype.value, 0), long_count),
            hdr_short_seconds=(short_seconds if short_count else None),
            hdr_short_count=short_count,
        ))
    return plans


RC16_RIG = "rc16"
PIGGYBACK_RIG = "piggyback"


def osc_plan(hours: float, config, acquired: int = 0) -> ExposurePlan:
    """PS-30: a Piggy-600 one-shot-color goal of `hours`, at the piggyback's
    own sub length and gain/offset (its light epoch, so its darks match)."""
    exp_s = float(getattr(config, "piggyback_exposure_s", 120.0) or 120.0)
    count = max(1, round(hours * 3600 / exp_s))
    return ExposurePlan(
        filter_type=FilterType.OSC, exposure_seconds=exp_s, count=count,
        gain=int(getattr(config, "piggyback_default_gain", 100)),
        offset=int(getattr(config, "piggyback_default_offset", 256)),
        acquired=min(acquired, count), rig=PIGGYBACK_RIG)


def _carry_seconds(new: ExposurePlan, old_len: float, old_s: float) -> None:
    """Carry accepted long-set seconds onto a rebuilt plan. Same sub length:
    keep partial seconds (PS-66, a lone capped sub). PS-118: a new sub length
    keeps the SECONDS, not the sub count (the goal is seconds), so 10 x 600 s
    accepted is 20 x 300 s on a 300 s plan, not 10."""
    if old_len == new.exposure_seconds:
        new.acquired_s = max(new.acquired_s, old_s)
        return
    new.acquired_s = float(old_s)
    new.acquired = min(new.subs_from_seconds(new.acquired_s), new.count)


def _norm_cid(cid: str) -> str:
    return (cid or "").replace(" ", "").lower()


class ProjectStore:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.data_dir) / "projects.json"
        self.projects: dict[str, ImagingProject] = {}
        self.load()

    def load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.projects = {pid: ImagingProject(**p)
                                 for pid, p in raw.items()}
                logger.info("Loaded %d projects from %s",
                            len(self.projects), self.path)
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to load projects.json: %s", e)
        self._seed_special_projects()
        self._migrate_m31_to_piggyback()

    # --- one-time goal decisions (PS-30) ------------------------------------
    MIGRATIONS_FILE = "project_migrations.json"
    M31_OSC_HOURS = 6.0

    def _migrations_path(self) -> Path:
        return Path(self.config.data_dir) / self.MIGRATIONS_FILE

    def _migrate_m31_to_piggyback(self):
        """Jeremy, 2026-09-26 (PS-30): M31 is ~3 deg, far larger than the RC16
        field, so it becomes a 6 h Piggy-600 OSC goal with the piggyback
        driving, and the 10 h RC16 LRGB plan is dropped (the RC16 works
        narrowband). Applied ONCE (marker in project_migrations.json), so a
        later hand edit of M31 is never undone. RC16 plans that already hold
        accepted subs are kept rather than discarded."""
        path = self._migrations_path()
        try:
            done = json.loads(path.read_text(encoding="utf-8")) \
                if path.exists() else {}
        except Exception:  # noqa: BLE001
            done = {}
        if done.get("ps30_m31_osc"):
            return
        m31 = next((p for p in self.projects.values()
                    if _norm_cid(p.target.catalog_id) == "m31"), None)
        if m31 is None:
            return  # nothing to convert; try again on the next load
        if not any(e.rig == PIGGYBACK_RIG for e in m31.exposure_plans):
            keep = [e for e in m31.exposure_plans
                    if e.rig != RC16_RIG or e.acquired or e.acquired_s
                    or e.hdr_short_acquired]
            m31.exposure_plans = keep + [osc_plan(self.M31_OSC_HOURS,
                                                  self.config)]
            if not any(e.rig == RC16_RIG for e in keep):
                m31.budget_hours = self.M31_OSC_HOURS
                m31.filter_mix = None
            m31.driving_rig = PIGGYBACK_RIG
            m31.total_integration_hours = m31.budget_hours
            m31.compute_completion()
            logger.warning("PS-30: M31 is now a %.0f h Piggy-600 OSC goal "
                           "(piggyback driving); RC16 plans kept: %s",
                           self.M31_OSC_HOURS,
                           [e.filter_type.value for e in keep] or "none")
            self.save()
        done["ps30_m31_osc"] = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(done, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("could not record project migration: %s", e)

    # Committed seed of "special plan per target" entries. Any target here whose
    # catalog_id is not already among the loaded projects is ADDED (never
    # overwritten). Future special targets = one more entry in this file — no
    # code change. Path: <repo>/config/seed_projects.json.
    SEED_PATH = Path(__file__).resolve().parents[2] / "config" / "seed_projects.json"

    def _seed_special_projects(self):
        """Non-destructively ADD any committed seed target whose catalog_id is
        missing from the loaded projects. Malformed/absent seed is logged and
        skipped — never crashes startup. Persists added targets via save()."""
        seed_path = self.SEED_PATH
        if not seed_path.exists():
            return
        try:
            raw = json.loads(seed_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("seed_projects.json unreadable (%s) — skipping seed", e)
            return
        if not isinstance(raw, dict):
            logger.warning("seed_projects.json is not a dict — skipping seed")
            return

        def _norm(cid: str) -> str:
            return (cid or "").replace(" ", "").lower()

        existing = {_norm(p.target.catalog_id) for p in self.projects.values()}
        added = []
        for pid, entry in raw.items():
            try:
                cid = _norm((entry.get("target") or {}).get("catalog_id", ""))
                if cid and cid in existing:
                    continue  # user already has this target — never overwrite
                if pid in self.projects:
                    continue  # id collision with an existing project — leave it
                proj = ImagingProject(**entry)
                proj.id = proj.id or pid
                # A seed entry may declare only the high-level intent (budget +
                # filter_mix + hdr) and leave exposure_plans empty; allocate them
                # here so authoring a new special target needs no hand-computed
                # counts. If the seed already carries plans, keep them as-is.
                if not proj.exposure_plans:
                    proj.exposure_plans = allocate_exposures(
                        target_kind(proj.target), proj.budget_hours, self.config,
                        custom_mix=proj.filter_mix, hdr=proj.hdr,
                        overrides=proj.exposure_overrides)
                    proj.total_integration_hours = proj.budget_hours
                self.projects[proj.id] = proj
                existing.add(cid)
                added.append(proj.target.name)
            except Exception as e:  # noqa: BLE001
                logger.warning("skipping malformed seed entry %r: %s", pid, e)
        if added:
            logger.info("Seeded %d special target(s): %s",
                        len(added), ", ".join(added))
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {pid: p.model_dump(mode="json") for pid, p in self.projects.items()},
            indent=2), encoding="utf-8")

    def add_from_target(self, target: CelestialTarget,
                        budget_hours: float = 8.0) -> ImagingProject:
        from uuid import uuid4
        kind = target_kind(target)
        proj = ImagingProject(
            id=str(uuid4()), target=target, priority=50,
            budget_hours=budget_hours,
            exposure_plans=allocate_exposures(kind, budget_hours, self.config),
            total_integration_hours=budget_hours,
        )
        self.projects[proj.id] = proj
        self.save()
        return proj

    def update(self, project_id: str, priority: int | None = None,
               budget_hours: float | None = None,
               active: bool | None = None,
               filter_mix: dict | None = None,
               hdr: dict | None = None,
               exposure_overrides: dict | None = None,
               osc_hours: float | None = None,
               driving_rig: str | None = None,
               drop_rc16: bool = False) -> ImagingProject | None:
        """hdr / exposure_overrides: pass {} to clear, a dict to set, None to
        leave unchanged. Either change re-allocates the plans.

        PS-30: only the RC16 plans are re-allocated from the budget and mix;
        piggyback (OSC) plans are kept as they are, HDR fields included.
        osc_hours: None = leave, 0 = remove the OSC plan, > 0 = set or resize
        it (acquired kept). On a piggyback-only goal (no RC16 plans) the
        budget sizes the OSC plan instead. drop_rc16 removes the RC16 plans
        (the M31 decision). driving_rig: "rc16" | "piggyback"."""
        proj = self.projects.get(project_id)
        if proj is None:
            return None
        plan_change = False
        if hdr is not None:
            proj.hdr = {k: float(v) for k, v in hdr.items() if v} or None
            plan_change = True
        if exposure_overrides is not None:
            proj.exposure_overrides = {k: float(v) for k, v
                                       in exposure_overrides.items() if v} or None
            plan_change = True
        if priority is not None:
            proj.priority = max(0, min(100, priority))
        if active is not None:
            proj.active = active
        if driving_rig in (RC16_RIG, PIGGYBACK_RIG):
            proj.driving_rig = driving_rig
        if filter_mix is not None:
            total = sum(v for v in filter_mix.values() if v and v > 0)
            if total > 0:  # normalize to 100
                proj.filter_mix = {k: round(v / total * 100)
                                   for k, v in filter_mix.items()
                                   if v and v > 0}
        if budget_hours is not None and budget_hours > 0:
            proj.budget_hours = round(budget_hours, 1)
        rc16 = [p for p in proj.exposure_plans if p.rig == RC16_RIG]
        other = [p for p in proj.exposure_plans if p.rig != RC16_RIG]
        if drop_rc16:
            rc16 = []
        osc_only = not rc16 and bool(other)
        if osc_only and budget_hours is not None and budget_hours > 0                 and osc_hours is None:
            osc_hours = proj.budget_hours
        if osc_hours is not None:
            old = next((p for p in other if p.filter_type == FilterType.OSC),
                       None)
            other = [p for p in other if p is not old]
            if osc_hours > 0:
                new = osc_plan(osc_hours, self.config,
                               old.acquired if old else 0)
                if old:
                    _carry_seconds(new, old.exposure_seconds,
                                   old.long_seconds_done())
                other.append(new)
        if not osc_only and not drop_rc16 and (
                (budget_hours is not None and budget_hours > 0)
                or filter_mix is not None or plan_change):
            acquired = {p.filter_type.value: p.acquired for p in rc16}
            short_acq = {p.filter_type.value: p.hdr_short_acquired
                         for p in rc16}
            # PS-66/PS-118: carry accepted seconds onto the rebuilt plans
            # (_carry_seconds)
            secs = {p.filter_type.value: (p.exposure_seconds,
                                          p.long_seconds_done())
                    for p in rc16}
            rc16 = allocate_exposures(
                target_kind(proj.target), proj.budget_hours, self.config,
                acquired, custom_mix=proj.filter_mix,
                hdr=proj.hdr, overrides=proj.exposure_overrides)
            for p in rc16:  # keep short-set progress too
                p.hdr_short_acquired = min(short_acq.get(p.filter_type.value, 0),
                                           p.hdr_short_count)
                old_len, old_s = secs.get(p.filter_type.value, (None, 0.0))
                if old_len:
                    _carry_seconds(p, old_len, old_s)
            proj.total_integration_hours = proj.budget_hours
        proj.exposure_plans = rc16 + other
        if not rc16 and other:  # piggyback-only: the budget is the OSC goal
            proj.budget_hours = round(sum(p.count * p.exposure_seconds
                                          for p in other) / 3600, 1)
            proj.total_integration_hours = proj.budget_hours
        proj.compute_completion() if hasattr(proj, "compute_completion") else None
        self.save()
        return proj

    def record_accepted_sub(self, target_name: str, filter_class: str,
                            exposure_seconds: float | None = None,
                            rig: str | None = None) -> bool:
        """Credit a QA-passed sub. Returns True if matched.
        With HDR, a sub whose length is closer to the short set's counts toward
        hdr_short_acquired instead of the long set. PS-30: only a plan of the
        sub's rig counts it (None = the RC16). PS-118: a long sub is credited
        by its own length (ExposurePlan.credit_long) on both rigs, guided or
        not, so a 400 s piggyback sub on a 120 s plan is 400 s and a capped
        300 s sub is half of a 600 s plan sub (PS-66)."""
        # PS-78: a container name ("<target> imaging (...)_Container") counts
        # for its target; an OSC loop container names none
        from photonscript.shared.target_names import canonical_target
        tn = (canonical_target(target_name) or "").strip().lower()
        if not tn:
            return False
        rig = rig or RC16_RIG
        for proj in self.projects.values():
            names = {proj.target.name.lower(), proj.target.catalog_id.lower(),
                     proj.target.catalog_id.replace(" ", "").lower()}
            if tn in names or any(tn and tn in n for n in names if n):
                for plan in proj.exposure_plans:
                    if plan.filter_type.value == filter_class                             and plan.rig == rig:
                        if plan.is_short_exposure(exposure_seconds):
                            plan.hdr_short_acquired += 1
                        else:
                            plan.credit_long(exposure_seconds)
                        self.save()
                        return True
        return False

    def delete(self, project_id: str) -> bool:
        if project_id in self.projects:
            del self.projects[project_id]
            self.save()
            return True
        return False
