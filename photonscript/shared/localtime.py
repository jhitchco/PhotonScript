"""DST-proof local time handling via the observatory's IANA timezone.

The telescope doesn't move, but the UTC offset does (MDT -6 / MST -7).
All local-time conversions go through here; config.utc_offset_hours is
only a fallback if the zoneinfo database is unavailable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def utc_offset_hours(config, when_utc: datetime | None = None) -> float:
    """UTC offset in hours for the observatory at a given UTC instant."""
    when_utc = when_utc or datetime.utcnow()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(config.observatory_tz)
        offset = tz.utcoffset(when_utc.replace(tzinfo=None)
                              .replace(tzinfo=__import__("datetime").timezone.utc))
        return offset.total_seconds() / 3600
    except Exception as e:  # noqa: BLE001 — no tzdata on this box, use fallback
        logger.debug("zoneinfo unavailable (%s); using configured offset", e)
        return getattr(config, "utc_offset_hours", -7.0)


def to_local(config, when_utc: datetime) -> datetime:
    return when_utc + timedelta(hours=utc_offset_hours(config, when_utc))


def night_base_date(config, now_utc: datetime | None = None) -> datetime:
    """Midnight of the LOCAL evening date the night in progress (or the
    coming night) started on, for get_twilight_times(). Noon to noon: until
    local noon the night that began yesterday evening is still "tonight"
    (2026-10-13: from local midnight the planner anchored to the new local
    date and planned the 10-13/14 night at 00:04 local). Callers still roll
    to base + 1 day once that night's dawn has passed."""
    now_utc = now_utc or datetime.utcnow()
    local = to_local(config, now_utc)
    if local.hour < 12:
        local -= timedelta(days=1)
    return datetime(local.year, local.month, local.day)
