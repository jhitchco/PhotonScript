"""One PhotonScript actor commands PHD2 at a time (PS-91).

The live guard's recovery, the pulse self-test (PS-92), the hot-pixel map
capture and later the calibration manager / settings audit / auto-tune all
send PHD2 commands (loop, find_star, guide_pulse, stop_capture, ...). Two of
them interleaving would wreck both (a guide pulse in the middle of a
re-selection, a stop_capture under a running self-test), so every command
sequence runs inside ``hold(owner)``:

    async with phd2_ops.hold("selftest", wait_s=30):
        ...

The lock is process wide (the scheduler app and the telescope agents share
one process in `photonscript start --mode full`). It is not a lock on PHD2
itself: NINA still drives PHD2 directly, which is why each actor also gates
on PHD2's own state. Loop agnostic on purpose (a plain flag polled, not an
asyncio.Lock bound to one event loop), so tests and helpers can use it from
any loop.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

_state: dict = {"owner": None, "since": None}


class PHD2Busy(RuntimeError):
    """Another PhotonScript actor holds PHD2."""

    def __init__(self, owner: str | None, wanted: str):
        super().__init__(f"PHD2 is busy ({owner}); {wanted} not started")
        self.owner = owner
        self.wanted = wanted


def owner() -> str | None:
    """Who holds PHD2 right now (None = free)."""
    return _state["owner"]


def busy() -> bool:
    return _state["owner"] is not None


def status() -> dict:
    since = _state["since"]
    return {"owner": _state["owner"],
            "held_s": round(time.monotonic() - since, 1) if since else None}


def _try(owner_tag: str) -> bool:
    if _state["owner"] is None:
        _state["owner"] = owner_tag
        _state["since"] = time.monotonic()
        return True
    return False


def release(owner_tag: str) -> None:
    if _state["owner"] == owner_tag:
        _state["owner"] = None
        _state["since"] = None


@asynccontextmanager
async def hold(owner_tag: str, wait_s: float = 0.0, poll_s: float = 0.2):
    """Hold PHD2 for owner_tag. Waits up to wait_s for the current holder,
    then raises PHD2Busy. Always released on exit (error or cancel)."""
    deadline = time.monotonic() + max(0.0, wait_s)
    while not _try(owner_tag):
        if time.monotonic() >= deadline:
            raise PHD2Busy(_state["owner"], owner_tag)
        await asyncio.sleep(poll_s)
    try:
        yield owner_tag
    finally:
        release(owner_tag)


def _reset_for_tests() -> None:
    _state["owner"] = None
    _state["since"] = None
