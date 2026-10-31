"""PS-174: a short-lived, single-flight cache for dashboard status reads.

Every open dashboard tab polled NINA #1 about 10,000 times an hour
(/api/scope every 5 s and again every 10 s, /api/night/where every 10 s,
/api/rigs every 15 s, each fanning out to 5-7 ninaAPI GETs), with no
caching, so two tabs doubled it. Endpoints wrap their body in cached():
concurrent callers inside `ttl` seconds share one computation, so the NINA
load no longer scales with the number of tabs or with duplicate polls.

    await cached("scope", 4.0, compute)    # compute: async () -> value
    invalidate("rigs")                      # after a change the user made
"""
from __future__ import annotations

import asyncio
import time

_store: dict[str, tuple[float, object]] = {}
_locks: dict[tuple[int, str], asyncio.Lock] = {}


async def cached(key: str, ttl: float, compute, fresh: bool = False):
    """The value computed for `key` within the last `ttl` seconds, else a new
    one (one computation at a time per key; waiters reuse its result).
    Exceptions are not cached. fresh=True forces a new computation."""
    now = time.monotonic()
    hit = _store.get(key)
    if not fresh and hit is not None and now - hit[0] < ttl:
        return hit[1]
    lk = (id(asyncio.get_running_loop()), key)   # a Lock binds to one loop
    lock = _locks.get(lk)
    if lock is None:
        lock = _locks[lk] = asyncio.Lock()
    t_req = now
    async with lock:
        hit = _store.get(key)
        # someone computed while we waited: theirs started after our request
        if hit is not None and (hit[0] > t_req
                                or (not fresh and time.monotonic() - hit[0] < ttl)):
            return hit[1]
        t0 = time.monotonic()
        value = await compute()
        _store[key] = (t0, value)
        return value


def invalidate(key: str | None = None) -> None:
    if key is None:
        _store.clear()
    else:
        _store.pop(key, None)
