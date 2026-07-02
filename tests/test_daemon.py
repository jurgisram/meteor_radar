"""Unit tests for src/daemon.py — hardware mocked throughout."""

import sys
import sqlite3
import numpy as np
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(peak=-35.0):
    row = np.full(40, peak, dtype=np.float32)
    return row


def _make_baseline(warmed_up=True, drifting=False, threshold=-30.0, mean=-33.0, std=1.0):
    b = MagicMock()
    b.is_warmed_up.return_value = warmed_up
    b.is_drifting.return_value = drifting
    b.threshold_db = threshold
    b.mean = mean
    b.std = std
    return b


def _make_event():
    from src.detector import Event
    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 1, 0, 0, 1)
    return Event(
        frames=[np.full(40, -25.0, dtype=np.float32)],
        start_time=start,
        end_time=end,
        signal_end_time=end,
        suspected_rfi=False,
    )


# ---------------------------------------------------------------------------
# Import guard — daemon imports are side-effect free
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_rtlsdr(monkeypatch):
    """Prevent any attempt to actually import rtlsdr."""
    rtlsdr_mock = MagicMock()
    monkeypatch.setitem(sys.modules, 'rtlsdr', rtlsdr_mock)
    yield


# ---------------------------------------------------------------------------
# Tests for run_loop() (the extracted, testable loop body)
# ---------------------------------------------------------------------------

class TestEventSuppression:
    """run_loop should gate writes on warmup and drift state."""

    def _run_one(self, baseline, detector_event=None):
        """Run one iteration of the loop body with mocked components."""
        from src.daemon import run_loop

        acq = MagicMock()
        acq.read_row.return_value = _make_row()

        detector = MagicMock()
        detector.in_event = False
        detector.feed.return_value = detector_event

        writer = MagicMock()
        db_conn = MagicMock()

        # Run exactly 1 iteration then stop
        run_loop(acq=acq, baseline=baseline, detector=detector,
                 writer=writer, db_conn=db_conn, max_iterations=1)
        return writer

    def test_event_logged_after_warmup(self):
        baseline = _make_baseline(warmed_up=True, drifting=False)
        event = _make_event()
        writer = self._run_one(baseline, detector_event=event)
        writer.write.assert_called_once_with(event, baseline)

    def test_event_suppressed_during_warmup(self):
        baseline = _make_baseline(warmed_up=False, drifting=False)
        event = _make_event()
        writer = self._run_one(baseline, detector_event=event)
        writer.write.assert_not_called()

    def test_event_suppressed_during_drift(self):
        baseline = _make_baseline(warmed_up=True, drifting=True)
        event = _make_event()
        writer = self._run_one(baseline, detector_event=event)
        writer.write.assert_not_called()

    def test_no_write_when_no_event(self):
        baseline = _make_baseline(warmed_up=True, drifting=False)
        writer = self._run_one(baseline, detector_event=None)
        writer.write.assert_not_called()

    def test_rfi_event_suppressed_even_after_warmup(self):
        from src.detector import Event
        baseline = _make_baseline(warmed_up=True, drifting=False)
        ts = datetime(2026, 1, 1, 0, 0, 1)
        rfi_event = Event(
            frames=[np.full(40, -25.0, dtype=np.float32)],
            start_time=datetime(2026, 1, 1, 0, 0, 0),
            end_time=ts,
            signal_end_time=ts,
            suspected_rfi=True,
        )
        writer = self._run_one(baseline, detector_event=rfi_event)
        writer.write.assert_not_called()


class TestPeriodicOps:
    """Baseline recompute and save triggered at correct sample counts."""

    def _run_n(self, n, baseline=None):
        from src.daemon import run_loop

        if baseline is None:
            baseline = _make_baseline()

        acq = MagicMock()
        acq.read_row.return_value = _make_row()

        detector = MagicMock()
        detector.in_event = False
        detector.feed.return_value = None

        writer = MagicMock()
        db_conn = MagicMock()

        run_loop(acq=acq, baseline=baseline, detector=detector,
                 writer=writer, db_conn=db_conn, max_iterations=n)
        return baseline, db_conn

    def test_baseline_recompute_every_100(self):
        baseline, _ = self._run_n(200)
        assert baseline.recompute_std.call_count == 2

    def test_baseline_recompute_not_called_at_99(self):
        baseline, _ = self._run_n(99)
        baseline.recompute_std.assert_not_called()

    def test_baseline_save_every_300(self):
        baseline, db_conn = self._run_n(600)
        assert baseline.save.call_count == 2

    def test_baseline_save_not_called_at_299(self):
        baseline, _ = self._run_n(299)
        baseline.save.assert_not_called()


class TestWritableGuard:
    """daemon.main() should call _check_writable before anything else."""

    def test_daemon_main_exits_when_hdd_missing(self):
        """If _check_writable calls sys.exit(1), main() raises SystemExit(1)."""
        with patch('src.daemon._check_writable', side_effect=lambda p: sys.exit(1)):
            with pytest.raises(SystemExit) as exc_info:
                from src.daemon import main
                main()
            assert exc_info.value.code == 1

    def test_daemon_main_calls_writable_check_before_init_db(self):
        """_check_writable must be called before init_db."""
        call_order = []

        def fake_check_writable(path):
            call_order.append('_check_writable')

        def fake_init_db(path):
            call_order.append('init_db')
            return MagicMock()

        mock_acq = MagicMock()
        mock_acq.open_device.side_effect = SystemExit(0)

        mock_baseline = MagicMock()
        mock_baseline.load.return_value = False

        with patch('src.daemon._check_writable', side_effect=fake_check_writable), \
             patch('src.daemon.init_db', side_effect=fake_init_db), \
             patch('src.daemon._setup_logging'), \
             patch('src.daemon.BaselineTracker', return_value=mock_baseline), \
             patch('src.daemon.Acquisition', return_value=mock_acq):
            with pytest.raises(SystemExit):
                from src.daemon import main
                main()

        assert call_order.index('_check_writable') < call_order.index('init_db')


class TestAcquisitionErrorHandling:
    """USB error handling: retry once, then exit."""

    def _run_with_errors(self, side_effects, baseline=None):
        from src.daemon import run_loop
        from src.acquisition import AcquisitionError

        if baseline is None:
            baseline = _make_baseline()

        acq = MagicMock()
        acq.read_row.side_effect = side_effects

        detector = MagicMock()
        detector.in_event = False
        detector.feed.return_value = None

        writer = MagicMock()
        db_conn = MagicMock()

        return acq, run_loop(acq=acq, baseline=baseline, detector=detector,
                             writer=writer, db_conn=db_conn, max_iterations=3)

    def test_acquisition_error_retries_once(self):
        from src.acquisition import AcquisitionError

        row = _make_row()
        # Error on first call; retry call + 2 normal iterations = 4 total read_row calls
        acq, _ = self._run_with_errors(
            [AcquisitionError("usb error"), row, row, row]
        )
        # Should have called open_device() once to retry
        acq.open_device.assert_called_once()

    def test_acquisition_error_exits_on_second_failure(self):
        from src.acquisition import AcquisitionError

        with pytest.raises(SystemExit) as exc_info:
            self._run_with_errors(
                [AcquisitionError("usb error 1"), AcquisitionError("usb error 2")]
            )
        assert exc_info.value.code == 1
