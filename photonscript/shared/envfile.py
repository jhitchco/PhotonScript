"""Read and update the .env file while preserving comments and ordering."""

from __future__ import annotations

import re
from pathlib import Path

_LINE = re.compile(r'^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$')


def env_path(root: Path | None = None) -> Path:
    """Locate the .env file (cwd, then repo root)."""
    candidates = [Path.cwd() / ".env"]
    if root:
        candidates.append(Path(root) / ".env")
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]  # default location for creation


def read_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs; strips surrounding quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            v = m.group("value").strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            values[m.group("key")] = v
    return values


def _format_value(value: str) -> str:
    if value == "" or any(ch in value for ch in " #\"'\\"):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Update keys in place, preserving comments/order; append new keys."""
    updates = dict(updates)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    for line in lines:
        m = None if line.lstrip().startswith("#") else _LINE.match(line)
        if m and m.group("key") in updates:
            key = m.group("key")
            out.append(f"{key}={_format_value(updates.pop(key))}")
        else:
            out.append(line)

    if updates:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- Added via web config editor ---")
        for key, value in updates.items():
            out.append(f"{key}={_format_value(value)}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
