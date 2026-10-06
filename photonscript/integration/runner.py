"""PS-22: run one PixInsight script, unattended, one instance at a time.

Rules from the M31_OSC4 runs:
  * never start PixInsight while another instance is running (it shares
    settings and swap; a second instance either refuses or corrupts state);
  * launch with -n --automation-mode --run=<script> --force-exit so the
    process exits when the script ends and the next stage can start;
  * follow the script's log by polling: open, read what is new, close. A
    reader that keeps the log open (tail -F) broke a run on 2026-10-05.
Success = the process exited and the log contains "EXIT OK".
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

DEFAULT_EXE = r"C:\Program Files\PixInsight\bin\PixInsight.exe"


class PixInsightBusy(RuntimeError):
    pass


def pixinsight_running() -> list[int]:
    """PIDs of running PixInsight processes."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a dependency
        return []
    out = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower().startswith("pixinsight"):
                out.append(p.pid)
        except Exception:  # noqa: BLE001 - process vanished
            continue
    return out


def read_new(path: Path, pos: int) -> tuple[str, int]:
    """Text appended to `path` since byte `pos`; the file is opened and
    closed inside this call (never held open). A shorter file (rewritten
    from the start, as the PJSR log writer does) is read from 0."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            if end < pos:
                pos = 0
            f.seek(pos)
            data = f.read()
    except OSError:
        return "", pos
    return data.decode("ascii", errors="replace"), pos + len(data)


def launch_args(exe: str, script: Path) -> list[str]:
    return [str(exe), "-n", "--automation-mode", f"--run={script}", "--force-exit"]


def run_script(script: Path, log_path: Path, *, exe: str = DEFAULT_EXE, poll_s: float = 15.0,
               timeout_s: float = 6 * 3600, echo=print, popen=subprocess.Popen,
               running=pixinsight_running, sleep=time.sleep, ok_marker: str = "EXIT OK") -> dict:
    """Run `script` in a fresh PixInsight and wait for it. Returns
    {ok, exit_code, minutes, last_line, timed_out}. Raises PixInsightBusy if
    PixInsight is already running (nothing is started then)."""
    busy = running()
    if busy:
        raise PixInsightBusy(f"PixInsight is already running (pid {', '.join(map(str, busy))}); "
                             "close it and run again")
    if not Path(exe).exists():
        raise FileNotFoundError(f"PixInsight not found at {exe}")
    t0 = time.time()
    proc = popen(launch_args(exe, Path(script)))
    pos, last, timed_out = 0, "", False
    while True:
        rc = proc.poll()
        txt, pos = read_new(Path(log_path), pos)
        for line in txt.splitlines():
            if line.strip():
                last = line.strip()
                if "STAGE" in line or "ERROR" in line or "DROPPED" in line or "EXIT" in line:
                    echo("  PI | " + last)
        if rc is not None:
            break
        if time.time() - t0 > timeout_s:
            timed_out = True
            echo(f"  PixInsight still running after {timeout_s / 3600:.1f} h; leaving it, not killing it")
            break
        sleep(poll_s)
    full, _ = read_new(Path(log_path), 0)
    return {"ok": (ok_marker in full) and not timed_out, "exit_code": proc.poll(),
            "minutes": round((time.time() - t0) / 60.0, 2), "last_line": last,
            "timed_out": timed_out}
