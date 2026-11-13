"""Librarian legacy transfer path (2026-10-10 scope night).

Every Piggy sub logged image_captured WinError 123 on a path like
"~\\Astrophotography\\?\\L": the "?" target of an unattributed sub went
straight into a mkdir, "~" was never expanded, and the mkdir ran on the
event loop inside the image_captured handler. Now the path is expanded and
Windows-safe, the folder is made off the loop, nothing raises into the
handler, and the whole legacy copy is off by default."""
import asyncio
import threading
from pathlib import Path

from photonscript.librarian import agent as lib
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import AgentMessage, AgentRole, CapturedImage


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                local_image_dir=str(tmp_path / "local"))
    base.update(kw)
    return PhotonScriptConfig(**base)


def _img(target="?", flt="L", name="sub_0001.fits", status="validated"):
    return CapturedImage(project_id="", filename=name,
                         file_path=f"C:\\NINA\\{name}", target_name=target,
                         filter_type=flt, exposure_seconds=300,
                         file_size_bytes=1024, status=status)


# ------------------------------------------------------------- path helpers

def test_unknown_target_maps_to_a_fixed_folder():
    assert lib.safe_component("?") == lib.UNKNOWN_DIR == "_unknown"
    assert lib.safe_component("???") == "_unknown"
    assert lib.safe_component("") == "_unknown"
    assert lib.safe_component(None) == "_unknown"


def test_windows_illegal_characters_are_replaced():
    assert lib.safe_component('M 31: core?') == "M 31_ core_"
    assert lib.safe_component('a<b>c"d|e*f/g\\h') == "a_b_c_d_e_f_g_h"
    assert lib.safe_component("NGC 7000. ") == "NGC 7000"     # trailing dot/space
    assert lib.safe_component("CON") == "_CON"
    assert lib.safe_component("nul.fits") == "_nul.fits"
    assert lib.safe_component("Heart Nebula") == "Heart Nebula"
    assert lib.safe_component("a\tb") == "a_b"


def test_tilde_is_expanded():
    home = Path.home()
    assert lib.local_base_dir("~\\Astrophotography") == home / "Astrophotography"
    assert lib.local_base_dir("~/Astrophotography") == home / "Astrophotography"
    assert lib.local_base_dir("~") == home
    assert lib.local_base_dir("") == home / "Astrophotography"
    assert "~" not in str(lib.local_base_dir("~\\Astrophotography"))


def test_transfer_dest_for_a_question_mark_target(tmp_path):
    d = lib.transfer_dest(tmp_path, _img())
    assert d == tmp_path / "_unknown" / "L" / "sub_0001.fits"
    assert "?" not in str(d)


def test_librarian_expands_the_configured_dir(tmp_path):
    l = lib.Librarian(_cfg(tmp_path, local_image_dir="~\\Astrophotography"))
    assert l._local_base == Path.home() / "Astrophotography"


# ------------------------------------------------------------- the gate

def test_config_default_off_and_exposed():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    assert PhotonScriptConfig(_env_file=None).librarian_legacy_transfer is False
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    f = by_env["PS_LIBRARIAN_LEGACY_TRANSFER"]
    assert f[0] == "librarian_legacy_transfer" and f[4] == "bool"


def test_off_by_default_queues_nothing_and_makes_no_folder(tmp_path):
    l = lib.Librarian(_cfg(tmp_path))
    asyncio.run(l._queue_transfer(_img()))
    assert l._transfer_queue.qsize() == 0
    assert not (tmp_path / "local").exists()


def test_on_queues_into_a_sanitized_folder(tmp_path):
    l = lib.Librarian(_cfg(tmp_path, librarian_legacy_transfer=True))
    asyncio.run(l._queue_transfer(_img()))
    job = l._transfer_queue.get_nowait()
    assert Path(job.dest_path) == tmp_path / "local" / "_unknown" / "L" / "sub_0001.fits"
    assert (tmp_path / "local" / "_unknown" / "L").is_dir()


# ------------------------------------------------------------- never raises, off the loop

def test_mkdir_error_does_not_propagate(tmp_path, monkeypatch, caplog):
    def boom(self, *a, **k):
        raise OSError(123, "The filename, directory name, or volume label "
                           "syntax is incorrect")
    monkeypatch.setattr(Path, "mkdir", boom)
    l = lib.Librarian(_cfg(tmp_path, librarian_legacy_transfer=True))
    asyncio.run(l._queue_transfer(_img()))          # no exception
    assert l._transfer_queue.qsize() == 0
    assert "transfer not queued" in caplog.text
    # nor through the image_captured handler
    msg = AgentMessage(sender=AgentRole.TELESCOPE, recipient=AgentRole.SCHEDULER,
                       msg_type="image_captured",
                       payload=_img().model_dump(mode="json"))
    asyncio.run(l._on_image_captured(msg))


def test_mkdir_runs_off_the_event_loop(tmp_path, monkeypatch):
    seen = []
    real = Path.mkdir

    def spy(self, *a, **k):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", spy)
    l = lib.Librarian(_cfg(tmp_path, librarian_legacy_transfer=True))
    asyncio.run(l._queue_transfer(_img(target="M31")))
    assert seen and not any(seen)        # every mkdir in a worker thread
    assert l._transfer_queue.qsize() == 1


def test_slow_mkdir_does_not_stall_the_loop(tmp_path, monkeypatch):
    """A 0.5 s mkdir: a 50 ms ticker on the same loop keeps ticking."""
    import time
    real = Path.mkdir

    def slow(self, *a, **k):
        time.sleep(0.5)
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", slow)
    l = lib.Librarian(_cfg(tmp_path, librarian_legacy_transfer=True))
    ticks = []

    async def main():
        async def ticker():
            for _ in range(8):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)
        t = asyncio.create_task(ticker())
        await l._queue_transfer(_img())
        await t
    asyncio.run(main())
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.3
