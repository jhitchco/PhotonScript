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
