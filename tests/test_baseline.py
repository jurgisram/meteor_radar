import sqlite3
import tempfile
import os
from datetime import datetime, timezone, timedelta

import pytest

from src.db import init_db


class TestGatedUpdate:
    def setup_method(self):
        from src.baseline import BaselineTracker
        self.tracker = BaselineTracker()

    def test_normal_updates_change_mean(self):
        for i in range(100):
            self.tracker.update(-30.0, in_event=False)
        assert self.tracker.mean == pytest.approx(-30.0, abs=0.01)

    def test_in_event_samples_do_not_change_mean(self):
        for _ in range(100):
            self.tracker.update(-30.0, in_event=False)
        mean_before = self.tracker.mean
        for _ in range(100):
            self.tracker.update(-10.0, in_event=True)
        assert self.tracker.mean == pytest.approx(mean_before, abs=0.001)

    def test_long_rfi_fallback_resumes_after_600(self):
        for _ in range(200):
            self.tracker.update(-30.0, in_event=False)
        mean_before = self.tracker.mean
        # Feed 601 in_event=True samples with very different power
        for _ in range(601):
            self.tracker.update(-5.0, in_event=True)
        # After 601 consecutive in-event, baseline should resume updating
        assert self.tracker.mean != pytest.approx(mean_before, abs=0.1)

    def test_long_rfi_fallback_not_triggered_at_600(self):
        for _ in range(200):
            self.tracker.update(-30.0, in_event=False)
        mean_before = self.tracker.mean
        # Exactly 600: should NOT resume
        for _ in range(600):
            self.tracker.update(-5.0, in_event=True)
        assert self.tracker.mean == pytest.approx(mean_before, abs=0.001)

    def test_consecutive_count_resets_on_normal_sample(self):
        for _ in range(100):
            self.tracker.update(-30.0, in_event=False)
        for _ in range(300):
            self.tracker.update(-5.0, in_event=True)
        # Break the streak
        self.tracker.update(-30.0, in_event=False)
        mean_mid = self.tracker.mean
        # Now another 601 in_event should be needed to trigger fallback
        for _ in range(300):
            self.tracker.update(-5.0, in_event=True)
        assert self.tracker.mean == pytest.approx(mean_mid, abs=0.001)


class TestWarmup:
    def setup_method(self):
        from src.baseline import BaselineTracker
        self.tracker = BaselineTracker()

    def test_not_warmed_up_initially(self):
        assert not self.tracker.is_warmed_up()

    def test_not_warmed_up_at_8999(self):
        for _ in range(8999):
            self.tracker.update(-30.0, in_event=False)
        assert not self.tracker.is_warmed_up()

    def test_warmed_up_at_9000(self):
        for _ in range(9000):
            self.tracker.update(-30.0, in_event=False)
        assert self.tracker.is_warmed_up()

    def test_warmup_counts_in_event_samples(self):
        # Even in_event=True samples count toward total_sample_count for warmup
        for _ in range(9000):
            self.tracker.update(-30.0, in_event=True)
        assert self.tracker.is_warmed_up()


class TestThreshold:
    def setup_method(self):
        from src.baseline import BaselineTracker
        self.tracker = BaselineTracker()

    def test_threshold_is_mean_plus_3(self):
        for _ in range(100):
            self.tracker.update(-30.0, in_event=False)
        assert self.tracker.threshold_db == pytest.approx(self.tracker.mean + 3.0, abs=0.001)


class TestRecomputeStd:
    def setup_method(self):
        from src.baseline import BaselineTracker
        self.tracker = BaselineTracker()

    def test_recompute_std_matches_actual(self):
        import numpy as np
        values = [-30.0, -29.0, -31.0, -28.0, -32.0] * 20  # 100 samples
        for v in values:
            self.tracker.update(v, in_event=False)
        self.tracker.recompute_std()
        expected_std = float(np.std(values))
        assert self.tracker.std == pytest.approx(expected_std, abs=0.01)

    def test_std_zero_for_constant_signal(self):
        for _ in range(100):
            self.tracker.update(-30.0, in_event=False)
        self.tracker.recompute_std()
        assert self.tracker.std == pytest.approx(0.0, abs=1e-6)


class TestDriftDetection:
    def setup_method(self):
        from src.baseline import BaselineTracker
        self.tracker = BaselineTracker()

    def test_no_drift_initially(self):
        assert not self.tracker.is_drifting()

    def test_monotonically_increasing_triggers_drift(self):
        for i in range(3000):
            self.tracker.update(-30.0 + i * 0.01, in_event=False)
        assert self.tracker.is_drifting()

    def test_monotonically_decreasing_triggers_drift(self):
        for i in range(3000):
            self.tracker.update(-30.0 - i * 0.01, in_event=False)
        assert self.tracker.is_drifting()

    def test_flat_signal_no_drift(self):
        for _ in range(3000):
            self.tracker.update(-30.0, in_event=False)
        assert not self.tracker.is_drifting()

    def test_noisy_signal_no_drift(self):
        import math
        for i in range(3000):
            # oscillating signal — not monotonic
            v = -30.0 + math.sin(i * 0.1) * 2.0
            self.tracker.update(v, in_event=False)
        assert not self.tracker.is_drifting()


class TestPersistence:
    def setup_method(self):
        from src.baseline import BaselineTracker
        self.tmp = tempfile.mktemp(suffix=".db")
        self.conn = init_db(self.tmp)
        self.tracker = BaselineTracker()

    def teardown_method(self):
        self.conn.close()
        if os.path.exists(self.tmp):
            os.unlink(self.tmp)

    def test_save_and_load_roundtrip(self):
        from src.baseline import BaselineTracker
        for _ in range(9000):
            self.tracker.update(-30.0, in_event=False)
        self.tracker.recompute_std()
        self.tracker.save(self.conn)

        new_tracker = BaselineTracker()
        restored = new_tracker.load(self.conn)

        assert restored is True
        assert new_tracker.mean == pytest.approx(self.tracker.mean, abs=0.001)
        assert new_tracker.std == pytest.approx(self.tracker.std, abs=0.001)
        assert new_tracker.is_warmed_up() is True

    def test_load_skips_warmup(self):
        from src.baseline import BaselineTracker
        for _ in range(9000):
            self.tracker.update(-30.0, in_event=False)
        self.tracker.save(self.conn)

        new_tracker = BaselineTracker()
        assert not new_tracker.is_warmed_up()
        new_tracker.load(self.conn)
        assert new_tracker.is_warmed_up()

    def test_load_stale_does_not_restore(self):
        from src.baseline import BaselineTracker
        for _ in range(100):
            self.tracker.update(-30.0, in_event=False)
        # Save with a timestamp > 2 hours ago
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.conn.execute(
            "INSERT INTO baseline_state (saved_at, mean_db, std_db, sample_count, last_alive) VALUES (?,?,?,?,?)",
            (stale_time, -30.0, 0.5, 100, stale_time),
        )
        self.conn.commit()

        new_tracker = BaselineTracker()
        restored = new_tracker.load(self.conn)
        assert restored is False
        assert not new_tracker.is_warmed_up()

    def test_load_empty_db_returns_false(self):
        from src.baseline import BaselineTracker
        new_tracker = BaselineTracker()
        result = new_tracker.load(self.conn)
        assert result is False

    def test_save_overwrites_existing_row(self):
        for _ in range(100):
            self.tracker.update(-30.0, in_event=False)
        self.tracker.save(self.conn)
        self.tracker.save(self.conn)
        count = self.conn.execute("SELECT COUNT(*) FROM baseline_state").fetchone()[0]
        assert count == 1

    def test_save_numpy_float32_loads_as_python_float(self):
        """numpy float32 must be cast to Python float before sqlite3 INSERT.
        sqlite3 doesn't recognise numpy scalars and stores them as 4-byte BLOBs;
        loading a BLOB back and using it in float arithmetic causes a UFuncNoLoopError."""
        import numpy as np
        from src.baseline import BaselineTracker

        tracker = BaselineTracker()
        # Feed numpy float32 values — after the first sample _mean becomes np.float32
        row = np.full(40, -62.5, dtype=np.float32)
        for _ in range(500):
            tracker.update(row.max(), in_event=False)
        tracker.save(self.conn)

        # Confirm sqlite3 stored REAL (Python float), not BLOB
        raw = self.conn.execute("SELECT mean_db FROM baseline_state").fetchone()[0]
        assert isinstance(raw, float), f"expected float from DB, got {type(raw)}"

        # Full roundtrip: restored tracker must survive arithmetic with np.float32 input
        restored = BaselineTracker()
        assert restored.load(self.conn) is True
        restored.update(row.max(), in_event=False)  # must not raise

    def test_load_corrupt_blob_returns_false(self):
        """If mean_db is a BLOB (old corrupt row), load() must return False, not crash."""
        from src.baseline import BaselineTracker
        import datetime as dt

        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO baseline_state (saved_at, mean_db, std_db, sample_count, last_alive) VALUES (?,?,?,?,?)",
            (now, b'\xc2\x80\xc2\x80', 0.5, 100, now),
        )
        self.conn.commit()

        tracker = BaselineTracker()
        assert tracker.load(self.conn) is False
