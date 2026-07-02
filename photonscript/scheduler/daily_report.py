"""Daily report: sky utilization + photon efficiency.

  sky utilization   = shutter-open hours / is-safe hours    (did we use the sky?)
  photon efficiency = integrating hours / shutter-open hours (overhead cost:
                      slews, autofocus, flips, plate solves)

Safe hours come from SafetyMonitor transitions in the NINA log; integrating
time from FITS EXPTIME sums; the shutter window from first/last sub timestamps.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SAFE_RE = re.compile(
    r"^(?P<ts>[\d\-:.T ]+?)\|.*SafetyMonitor.*?(?P<state>Safe|Unsafe)",
    re.IGNORECASE,
)


@dataclass
class DailyReport:
    date: str
    safe_hours: float = 0.0
    shutter_hours: float = 0.0
    integrating_hours: float = 0.0
    sub_count: int = 0
    per_target: dict = field(default_factory=dict)

    @property
    def sky_utilization_pct(self) -> float:
        return self.shutter_hours / self.safe_hours * 100 if self.safe_hours else 0.0

    @property
    def photon_efficiency_pct(self) -> float:
        return (self.integrating_hours / self.shutter_hours * 100
                if self.shutter_hours else 0.0)

    def to_text(self) -> str:
        lines = [
            f"PhotonScript daily report — {self.date}",
            "",
            f"Safe (usable) hours : {self.safe_hours:5.1f}",
            f"Shutter-open hours  : {self.shutter_hours:5.1f}",
            f"Integrating hours   : {self.integrating_hours:5.1f}",
            f"Sky utilization     : {self.sky_utilization_pct:5.0f}%  (shutter / safe)",
            f"Photon efficiency   : {self.photon_efficiency_pct:5.0f}%  (integrating / shutter)",
            "",
            "Subs by target:",
        ]
        for obj, filts in self.per_target.items():
            detail = ", ".join(f"{k}x{v}" for k, v in sorted(filts.items()))
            lines.append(f"  {obj}: {detail}")
        if not self.sub_count:
            lines.append("  (no light frames captured)")
        return "\n".join(lines)


def _latest_log(logs_dir: str, date_str: str) -> str | None:
    logs = sorted(glob.glob(os.path.join(logs_dir, f"{date_str.replace('-', '')}*.log")))
    if not logs:  # the session usually starts the previous evening
        logs = sorted(glob.glob(os.path.join(logs_dir, "*.log")))
    return logs[-1] if logs else None


def _safe_hours(log_path: str) -> float:
    """Sum hours spent in the 'Safe' state from SafetyMonitor transitions."""
    events: list[tuple[datetime, bool]] = []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = SAFE_RE.search(line)
            if m:
                try:
                    ts = datetime.fromisoformat(m.group("ts").strip()[:19])
                    events.append((ts, m.group("state").lower() == "safe"))
                except ValueError:
                    continue
    total = timedelta()
    safe_since: datetime | None = None
    for ts, is_safe in events:
        if is_safe and safe_since is None:
            safe_since = ts
        elif not is_safe and safe_since is not None:
            total += ts - safe_since
            safe_since = None
    if safe_since is not None and events:
        total += events[-1][0] - safe_since
    return total.total_seconds() / 3600


def build_daily_report(config, date_str: str) -> DailyReport:
    """Build the report for a date (YYYY-MM-DD, the night that ENDED that morning)."""
    from astropy.io import fits

    report = DailyReport(date=date_str)

    fits_root = os.path.join(config.image_watch_dir, date_str)
    lights = sorted(glob.glob(os.path.join(fits_root, "**", "LIGHT", "*.fits"),
                              recursive=True))
    integ_s, first, last = 0.0, None, None
    for f in lights:
        try:
            with fits.open(f) as hdul:
                hdr = hdul[0].header
        except Exception:  # noqa: BLE001
            continue
        integ_s += float(hdr.get("EXPTIME", 0))
        obj = hdr.get("OBJECT", "?")
        filt = hdr.get("FILTER", "?")
        report.per_target.setdefault(obj, {}).setdefault(filt, 0)
        report.per_target[obj][filt] += 1
        report.sub_count += 1
        ts = hdr.get("DATE-OBS")
        if ts:
            try:
                dt = datetime.fromisoformat(ts[:19])
                first = min(first or dt, dt)
                last = max(last or dt, dt)
            except ValueError:
                pass

    report.integrating_hours = integ_s / 3600
    if first and last and report.sub_count:
        avg_exp_h = integ_s / 3600 / report.sub_count
        report.shutter_hours = (last - first).total_seconds() / 3600 + avg_exp_h

    log = _latest_log(config.nina_logs_dir, date_str)
    if log:
        report.safe_hours = _safe_hours(log)

    return report
