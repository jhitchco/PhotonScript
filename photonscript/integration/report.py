"""PS-33: post integrator ledgers from the desktop to the scheduler.

The run folder's ledger.json is the queue: `reported: false` = not yet taken
by the scheduler. post_ledger() posts one (POST /api/integrations, short
timeout); on success it writes back reported / reported_at and the version
the scheduler stored. When the scope is unreachable the file stays
`reported: false` and the next sweep (every integrate run, every
integrate-watch cycle, or `photonscript integrate-report`) retries it.

A failed post is a warning, never an error: the ledger file is the record.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from photonscript.integration import ledger as writer
from photonscript.shared import ledger as L

DEFAULT_TIMEOUT_S = 10.0


def http_post_json(url: str, body: dict, timeout: float = DEFAULT_TIMEOUT_S) -> tuple[int, dict]:
    """POST JSON; (status, body). Network errors raise OSError."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - tailnet URL from config
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        return e.code, payload


def http_get_json(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8") or "{}")


def post_ledger(path: Path, base_url: str, *, post=http_post_json,
                timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Post one ledger file. Returns {ok, status, detail, version}. Never
    raises for network trouble."""
    path = Path(path)
    if not base_url:
        return {"ok": False, "status": 0, "detail": "no scheduler URL (integration_report_url)"}
    led = L.load(path)
    url = base_url.rstrip("/") + "/api/integrations"
    try:
        status, body = post(url, led.dump(), timeout)
    except (OSError, ValueError) as e:
        return {"ok": False, "status": 0, "detail": f"scheduler unreachable ({e})"}
    if status != 200 or not body.get("ok"):
        return {"ok": False, "status": status,
                "detail": str(body.get("detail") or f"HTTP {status}")}
    led.reported = True
    led.reported_at = L.now_iso()
    if body.get("version"):
        led.version = int(body["version"])
    L.save(path, led)
    return {"ok": True, "status": status, "detail": body.get("headline", ""),
            "version": led.version, "project_id": body.get("project_id")}


def pending(staging_root: Path) -> list[Path]:
    """Ledger files the scheduler has not taken yet."""
    return [f for f, led in writer.local_ledgers(staging_root) if not led.reported]


def sweep(staging_root: Path, base_url: str, *, post=http_post_json, echo=print,
          timeout: float = DEFAULT_TIMEOUT_S, dry_run: bool = False) -> dict:
    """Post every unreported ledger under the staging root. Stops at the
    first unreachable error (the scope is down: try again next time)."""
    todo = pending(staging_root)
    done, failed = [], []
    for f in todo:
        if dry_run:
            echo(f"  would post {f}")
            continue
        r = post_ledger(f, base_url, post=post, timeout=timeout)
        if r["ok"]:
            done.append(str(f))
            echo(f"  posted {f.parent.name} (v{r.get('version')})")
            continue
        failed.append({"file": str(f), **r})
        echo(f"  WARNING: {f.parent.name} not posted: {r['detail']} (queued, retried next time)")
        if r["status"] == 0:
            break
    return {"pending": len(todo), "posted": done, "failed": failed}
