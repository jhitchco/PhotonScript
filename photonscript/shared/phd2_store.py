"""Where PhotonScript keeps its own PHD2 records (PS-91 / PS-92).

    <data_dir>/phd2/hotpix.json            guide-camera hot-pixel map (PS-91)
    <data_dir>/phd2/hotpix_median.fits     the median frame it was built from
    <data_dir>/phd2/guard/<night>.jsonl    non-star lock guard episodes
    <data_dir>/phd2/selftest.jsonl         pulse-path self-test results (PS-92)
    <data_dir>/phd2/alerts.json            once-per-night alert latches

A "night" is the local evening date (the runs page date): local time minus
12 h. Readers never raise: a missing or torn file reads as empty.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def phd2_dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "phd2"


def night_of(config, when_utc: datetime | None = None) -> str:
    """The local evening date a UTC instant belongs to (noon to noon)."""
    from photonscript.shared.localtime import to_local
    when_utc = when_utc or datetime.utcnow()
    return (to_local(config, when_utc) - timedelta(hours=12)).strftime("%Y-%m-%d")


def iso_z(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def parse_z(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).strip().rstrip("Z"))
    except (TypeError, ValueError):
        return None


def append_jsonl(path: Path, rec: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError as e:
        logger.warning("could not append %s: %s", path, e)


def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: dict) -> None:
    """Atomic write (tmp + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------- guard log

def guard_path(config, night: str) -> Path:
    return phd2_dir(config) / "guard" / f"{night}.jsonl"


def guard_records(config, night: str) -> list[dict]:
    return read_jsonl(guard_path(config, night))


def guard_episodes(config, night: str) -> list[dict]:
    """Episodes of a night: an 'open' record joined with its 'close' (end
    None while still open) and any recovery records."""
    eps: dict[str, dict] = {}
    for r in guard_records(config, night):
        eid = r.get("id")
        if not eid:
            continue
        if r.get("event") == "open":
            eps[eid] = {"id": eid, "start_utc": r.get("t_utc"), "end_utc": None,
                        "kind": r.get("kind"), "codes": r.get("codes", []),
                        "detail": r.get("detail"), "lock": r.get("lock"),
                        "recoveries": [], "observe_only": r.get("observe_only")}
        elif eid in eps and r.get("event") == "close":
            eps[eid]["end_utc"] = r.get("t_utc")
            eps[eid]["close_reason"] = r.get("reason")
            eps[eid]["codes"] = sorted(set(eps[eid]["codes"]) | set(r.get("codes", [])))
        elif eid in eps and r.get("event") == "recovery":
            eps[eid]["recoveries"].append({k: r.get(k) for k in
                                           ("t_utc", "ok", "detail", "steps")})
    return list(eps.values())


def nonstar_windows(config, night: str, now: datetime | None = None
                    ) -> list[tuple[datetime, datetime]]:
    """[(start, end)] UTC of the night's non-star lock episodes (an episode
    still open runs to `now`, or 30 min past its start when now is None).
    PS-85: a low-SNR episode (guard D6, guiding on noise) counts too."""
    out = []
    for e in guard_episodes(config, night):
        if e.get("kind") not in ("non_star", "low_snr"):
            continue
        a = parse_z(e.get("start_utc"))
        if a is None:
            continue
        b = parse_z(e.get("end_utc")) or (now or a + timedelta(minutes=30))
        out.append((a, max(a, b)))
    return out


# ------------------------------------------------------------ self-test log

def selftest_path(config) -> Path:
    return phd2_dir(config) / "selftest.jsonl"


def selftest_results(config, night: str | None = None, days: int | None = None
                     ) -> list[dict]:
    rows = read_jsonl(selftest_path(config))
    if night:
        rows = [r for r in rows if r.get("night") == night]
    elif days:
        cutoff = (datetime.utcnow() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        rows = [r for r in rows if str(r.get("night") or "") >= cutoff]
    return rows


# -------------------------------------------------------- hot-pixel map

def hotpix_path(config) -> Path:
    return phd2_dir(config) / "hotpix.json"


def hotpix_fits_path(config) -> Path:
    return phd2_dir(config) / "hotpix_median.fits"


# ------------------------------------------------------- once per night

def alert_once(config, key: str) -> bool:
    """True the first time `key` (e.g. 'guard-2026-10-02') is seen; the
    latch survives restarts. Keeps the last 60 keys."""
    path = phd2_dir(config) / "alerts.json"
    d = read_json(path) or {}
    keys = d.get("keys", [])
    if key in keys:
        return False
    keys = (keys + [key])[-60:]
    try:
        write_json(path, {"keys": keys})
    except OSError as e:
        logger.warning("alert latch not saved (%s): %s", key, e)
    return True
