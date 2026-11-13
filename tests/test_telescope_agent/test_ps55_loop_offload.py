"""PS-55: blocking work seen in stalls.log runs off the event loop.

The scope PC's photonscript.log (2026-10-28 to 11-01, task-Interactive)
showed 5 to 6 s "Event loop STALLED" stacks in two places: the image file
watch rglob over the NINA output tree, and the evening dusk-flats calibration
health scan (FITS getheader). Both now run in a worker thread."""
import asyncio
import threading

from photonscript.telescope_agent.agent import TelescopeAgent


class _WatchDir:
    """Stands in for the NINA output folder; records the scanning thread."""

    def __init__(self, files):
        self.files = files
        self.threads = []

    def exists(self):
        return True

    def rglob(self, pattern):
        self.threads.append(threading.get_ident())
        ext = pattern.lstrip("*")
        return [f for f in self.files if str(f).endswith(ext)]


def test_file_watch_scan_runs_off_the_loop_thread(tmp_path, monkeypatch):
    sub = tmp_path / "Heart_L_0001.fits"
    sub.write_bytes(b"x" * 10)
    agent = TelescopeAgent.__new__(TelescopeAgent)
    agent._running = True
    watch = _WatchDir([])
    agent._watch_dir = watch
    seen = []

    async def process(path):
        seen.append(path)
        agent._running = False
    agent._process_new_image = process

    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        watch.files = [sub]          # a new sub lands after the first scan
        await real_sleep(0)
    monkeypatch.setattr("photonscript.telescope_agent.agent.asyncio.sleep",
                        fast_sleep)

    async def main():
        loop_thread = threading.get_ident()
        await asyncio.wait_for(agent._file_watch_loop(), 5)
        return loop_thread

    loop_thread = asyncio.new_event_loop().run_until_complete(main())
    assert seen == [sub]
    assert watch.threads and loop_thread not in watch.threads


def test_file_watch_waits_while_the_folder_is_missing(monkeypatch):
    agent = TelescopeAgent.__new__(TelescopeAgent)
    agent._running = True

    class _Missing:
        def exists(self):
            return False

        def rglob(self, pattern):
            raise AssertionError("no walk of a missing folder")
    agent._watch_dir = _Missing()
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        agent._running = False
    monkeypatch.setattr("photonscript.telescope_agent.agent.asyncio.sleep", sleep)
    asyncio.new_event_loop().run_until_complete(agent._file_watch_loop())
    assert sleeps == [10]
