"""Tests for librarian transfer window logic."""

from photonscript.shared.models import TransferWindow


class TestTransferWindow:
    def test_default_window(self):
        window = TransferWindow()
        assert window.start_hour_local == 8
        assert window.end_hour_local == 18
        assert window.max_concurrent == 1

    def test_bandwidth_limit(self):
        window = TransferWindow(bandwidth_limit_mbps=25.0)
        assert window.bandwidth_limit_mbps == 25.0
