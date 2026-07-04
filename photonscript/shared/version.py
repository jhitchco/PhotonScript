"""Running-checkout version (git short hash + commit date)."""

from __future__ import annotations

from pathlib import Path


def repo_version() -> str:
    import subprocess
    try:
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h (%cd)",
             "--date=format:%b %d %H:%M"],
            cwd=root, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "version unknown"
    except Exception:  # noqa: BLE001
        return "version unknown"
