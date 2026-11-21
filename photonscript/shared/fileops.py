"""Small file helpers shared by the scheduler and the integrator.

replace_retry: os.replace with a short retry on PermissionError. On Windows
a rename onto a file is refused (WinError 5, access denied) while another
process holds the target or the temp file open for a moment: the virus
scanner and the search indexer open every new file right after it is
written. The hold is brief, so a few short retries ride it out; a lasting
refusal still raises.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

REPLACE_ATTEMPTS = 6
REPLACE_DELAY_S = 0.05


def replace_retry(src, dst, *, attempts: int = REPLACE_ATTEMPTS, delay_s: float = REPLACE_DELAY_S,
                  sleep=None, replace=None) -> Path:
    """os.replace(src, dst), retried on PermissionError up to `attempts`
    times with a growing short sleep (0.05, 0.10, ... s). Returns dst; the
    last PermissionError propagates. `replace` / `sleep` default to
    os.replace / time.sleep looked up at call time, so a test that patches
    os.replace still reaches every writer that goes through here."""
    replace = replace or os.replace
    sleep = sleep or time.sleep
    n = max(1, int(attempts))
    for i in range(n):
        try:
            replace(src, dst)
            return Path(dst)
        except PermissionError:
            if i == n - 1:
                raise
            sleep(delay_s * (i + 1))
    return Path(dst)  # pragma: no cover - the loop returns or raises
