"""Preflight — automated daytime system test.

Runs the full pre-session checklist so problems surface in the afternoon,
not at 10 PM: config, directories (created if missing), NINA API, camera,
PHD2, safety monitor, sequence generation + lint round-trip, star-extraction
library, Pushover, and disk space.

Each check returns: name, status (pass | warn | fail), detail.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
from datetime import datetime
from pathlib import Path

import httpx


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


async def _nina_get(config, endpoint: str, timeout=5) -> dict:
    url = config.nina_base_url.rstrip("/") + endpoint
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


async def _nina_connect(config, device: str, timeout=20) -> str:
    """Actively tell NINA to connect a device. Returns '' on success, else an
    error string. (NINA's connect endpoints are GET.)"""
    try:
        await _nina_get(config, f"/equipment/{device}/connect", timeout=timeout)
        return ""
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


async def _connected(config, device: str, timeout=8):
    """Return (connected: bool, payload: dict, err: str) for a device."""
    try:
        info = await _nina_get(config, f"/equipment/{device}/info", timeout=timeout)
        payload = info.get("Response", info)
        return bool(payload.get("Connected", False)), payload, ""
    except Exception as e:  # noqa: BLE001
        return False, {}, f"{type(e).__name__}: {e}"


async def _ensure_connected(config, device: str, attempts: int = 3):
    """Read state; if disconnected, actively connect and re-read. Returns
    (connected, payload, detail) where detail carries the last error seen."""
    connected, payload, err = await _connected(config, device)
    if connected:
        return True, payload, ""
    for _ in range(attempts):
        err = await _nina_connect(config, device) or err
        await asyncio.sleep(3)
        connected, payload, read_err = await _connected(config, device)
        if connected:
            return True, payload, ""
        err = read_err or err
    return False, payload, err


async def run_preflight(config) -> dict:
    """Run all checks. Returns {'ran_at', 'summary', 'checks': [...]}."""
    checks: list[dict] = []

    # 1. Config file -------------------------------------------------------
    env = Path.cwd() / ".env"
    if env.exists():
        checks.append(_check("Config (.env)", "pass", f"Loaded from {env}"))
    else:
        checks.append(_check("Config (.env)", "warn",
                             "No .env found — running on built-in defaults"))

    # 2. Directories — create the ones we own, verify the ones NINA owns ---
    created, missing = [], []
    ours = {
        "data dir": Path(config.data_dir),
        "local images": Path(config.local_image_dir),
        "stacking output": Path(config.stacking_output_dir),
        "sequences": Path.cwd() / "sequences",
        "sequences/archive": Path.cwd() / "sequences" / "archive",
    }
    for label, p in ours.items():
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
                created.append(label)
            except OSError as e:
                missing.append(f"{label} ({e})")
    detail = "All present"
    if created:
        detail = f"Created: {', '.join(created)}"
    if missing:
        checks.append(_check("Working directories", "fail",
                             f"Could not create: {'; '.join(missing)}"))
    else:
        checks.append(_check("Working directories", "pass", detail))

    watch = Path(config.image_watch_dir)
    if watch.exists():
        checks.append(_check("NINA image directory", "pass", str(watch)))
    else:
        checks.append(_check("NINA image directory", "warn",
                             f"{watch} does not exist yet — NINA creates it on "
                             "first capture, or fix PS_IMAGE_WATCH_DIR"))

    logs = Path(config.nina_logs_dir) if hasattr(config, "nina_logs_dir") else None
    if logs and logs.exists():
        checks.append(_check("NINA logs directory", "pass", str(logs)))
    elif logs:
        checks.append(_check("NINA logs directory", "warn",
                             f"{logs} not found — daily report needs it"))

    # 3. NINA Advanced API + equipment -------------------------------------
    # Preflight is active, not passive: if a device is disconnected it TRIES to
    # connect it (rather than just reporting "not connected"), then reports the
    # real result. This is also the "connect everything" step for a restart.
    api_ok = False
    try:
        connected, payload, _ = await _ensure_connected(config, "camera")
        api_ok = True
        temp = payload.get("Temperature")
        if connected:
            checks.append(_check("NINA API + camera", "pass",
                                 f"Camera connected, sensor {temp}°C"))
        else:
            checks.append(_check("NINA API + camera", "warn",
                                 "API reachable but camera would not connect "
                                 "(tried auto-connect)"))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("NINA API + camera", "fail",
                             f"{config.nina_base_url} unreachable ({type(e).__name__}) "
                             "— is NINA running with the Advanced API plugin enabled?"))

    if api_ok:
        connected, payload, err = await _ensure_connected(config, "mount")
        if connected:
            checks.append(_check("Mount", "pass",
                                 f"Connected, tracking={payload.get('TrackingEnabled', payload.get('Tracking', '?'))}"))
        else:
            checks.append(_check("Mount", "warn",
                                 f"Would not connect (tried auto-connect)"
                                 + (f" — {err}" if err else "")))

        # Safety monitor — the one that bit us. Actively connect + retry, and
        # on failure surface the REAL error (e.g. the AARO Alpaca device at
        # 192.168.1.8:12345 timing out), not a generic "not connected".
        connected, payload, err = await _ensure_connected(config, "safetymonitor")
        if connected:
            state = "SAFE" if payload.get("IsSafe", False) else "UNSAFE"
            checks.append(_check("Safety monitor", "pass",
                                 f"Connected, currently {state} "
                                 "(UNSAFE in daytime is correct)"))
        else:
            checks.append(_check(
                "Safety monitor", "fail",
                "Could not connect after auto-connect retries — "
                f"{err or 'unknown error'}. Sequences would image through "
                "weather. If this is an Alpaca timeout, the AARO safety device "
                "may be slow/down or NINA's Alpaca client timeout is too short."))

    # 4. PHD2 (optional — unguided is the default) --------------------------
    try:
        with socket.create_connection((config.phd2_host, config.phd2_port), timeout=3):
            checks.append(_check("PHD2 event server", "pass",
                                 f"Listening on {config.phd2_host}:{config.phd2_port}"))
    except OSError:
        checks.append(_check("PHD2 event server", "warn",
                             "Not reachable — fine for unguided runs"))

    # 5. Sequence generation + lint round-trip ------------------------------
    try:
        from photonscript.shared.models import (ExposurePlan, FilterType,
                                                NinaSequenceTarget)
        from photonscript.scheduler.nina_sequence import build_sequence_for_night
        from photonscript.scheduler.nina_sequence_json import generate_nina_json
        from photonscript.scheduler.sequence_lint import lint

        target = NinaSequenceTarget(
            name="PreflightTest", ra_hours=13.5, dec_degrees=47.2,
            exposures=[ExposurePlan(filter_type=FilterType.LUMINANCE,
                                    exposure_seconds=180, count=5,
                                    gain=config.default_gain,
                                    offset=config.default_offset)],
        )
        seq = build_sequence_for_night("PreflightTest", [target])
        result = lint(json.loads(generate_nina_json(seq)))
        if result.ok:
            checks.append(_check("Sequence generate + lint", "pass",
                                 "Round-trip clean — generator output passes "
                                 "all AARO rules"))
        else:
            errs = "; ".join(f.detail for f in result.findings if f.level == "ERROR")
            checks.append(_check("Sequence generate + lint", "fail", errs))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("Sequence generate + lint", "fail", repr(e)))

    # 6. Star extraction library --------------------------------------------
    try:
        try:
            import sep  # noqa: F401
        except ImportError:
            import sep_pjw  # noqa: F401
        checks.append(_check("Star extraction (sep)", "pass",
                             "Full QA available (eccentricity + collimation watch)"))
    except ImportError:
        checks.append(_check("Star extraction (sep)", "warn",
                             "sep not installed — scipy fallback active; "
                             "eccentricity and corner-spread QA disabled"))

    # 7. Pushover ------------------------------------------------------------
    if getattr(config, "pushover_user_key", "") and getattr(config, "pushover_api_token", ""):
        try:
            from photonscript.shared.pushover import notify
            ok = await notify(config, "Preflight test notification — all good if "
                              "you can read this.", title="PhotonScript preflight")
            checks.append(_check("Pushover", "pass" if ok else "fail",
                                 "Test notification sent — check your phone"
                                 if ok else "Send failed — check keys"))
        except Exception as e:  # noqa: BLE001
            checks.append(_check("Pushover", "fail", repr(e)))
    else:
        checks.append(_check("Pushover", "warn",
                             "Keys not set — nanny alerts will only go to the log"))

    # 8. Disk space -----------------------------------------------------------
    try:
        anchor = watch if watch.exists() else Path.cwd()
        free_gb = shutil.disk_usage(anchor).free / 1e9
        status = "pass" if free_gb > 50 else ("warn" if free_gb > 15 else "fail")
        checks.append(_check("Disk space", status,
                             f"{free_gb:.0f} GB free on image drive"))
    except OSError as e:
        checks.append(_check("Disk space", "warn", repr(e)))

    counts = {s: sum(1 for c in checks if c["status"] == s)
              for s in ("pass", "warn", "fail")}
    return {
        "ran_at": datetime.utcnow().isoformat() + "Z",
        "summary": counts,
        "go": counts["fail"] == 0,
        "checks": checks,
    }
