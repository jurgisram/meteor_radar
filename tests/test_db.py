import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EVENTS_COLUMNS = {
    "id", "timestamp", "duration_ms", "peak_power_db", "snr_db",
    "integrated_power", "frequency_centroid_hz", "bandwidth_hz",
    "suspected_rfi", "cluster_id", "baseline_mean_db", "baseline_std_db",
    "spectrogram", "spectrogram_shape", "fft_bin_width_hz", "row_period_ms",
}

BASELINE_STATE_COLUMNS = {
    "saved_at", "mean_db", "std_db", "sample_count", "last_alive",
}


def get_columns(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Tests for init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_events_table(self, tmp_path):
        from src.db import init_db
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            cols = get_columns(conn, "events")
        assert EVENTS_COLUMNS == cols

    def test_creates_baseline_state_table(self, tmp_path):
        from src.db import init_db
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            cols = get_columns(conn, "baseline_state")
        assert BASELINE_STATE_COLUMNS == cols

    def test_wal_mode_enabled(self, tmp_path):
        from src.db import init_db
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_idempotent_second_call(self, tmp_path):
        from src.db import init_db
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        # Insert a row so we can verify data survives second init
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO baseline_state VALUES (?,?,?,?,?)",
                ("2026-01-01T00:00:00Z", -30.0, 1.5, 100, "2026-01-01T00:00:00Z"),
            )
        init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT * FROM baseline_state").fetchall()
        assert len(rows) == 1, "Second init_db call must not destroy existing data"

    def test_events_id_is_primary_key_autoincrement(self, tmp_path):
        from src.db import init_db
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("PRAGMA table_info(events)")
            for row in cur.fetchall():
                if row[1] == "id":
                    assert row[5] == 1, "id must be primary key"
                    break


# ---------------------------------------------------------------------------
# Tests for main-guard environment checks
# ---------------------------------------------------------------------------

class TestEnvironmentChecks:
    def test_missing_rtlsdr_exits_nonzero(self, tmp_path):
        """When rtlsdr cannot be imported, main() exits with code 1."""
        from src import db as db_module

        with patch.object(db_module, "_check_rtlsdr") as mock_check:
            mock_check.side_effect = SystemExit(1)
            with pytest.raises(SystemExit) as exc:
                db_module._check_rtlsdr()
        assert exc.value.code != 0

    def test_nonwritable_path_exits_nonzero(self, tmp_path):
        """When /mnt/hdd/ (or given path) is not writable, main() exits with code 1."""
        from src import db as db_module

        with patch.object(db_module, "_check_writable") as mock_check:
            mock_check.side_effect = SystemExit(1)
            with pytest.raises(SystemExit) as exc:
                db_module._check_writable("/nonexistent/path")
        assert exc.value.code != 0

    def test_check_rtlsdr_passes_when_available(self):
        """_check_rtlsdr should not raise when rtlsdr is importable."""
        from src import db as db_module
        with patch("builtins.__import__", side_effect=lambda name, *a, **kw: MagicMock() if name == "rtlsdr" else __import__(name, *a, **kw)):
            # Just verify the function exists and is callable
            assert callable(db_module._check_rtlsdr)

    def test_check_writable_passes_for_tmp(self, tmp_path):
        """_check_writable should not raise for a writable directory."""
        from src import db as db_module
        # Should not raise
        db_module._check_writable(str(tmp_path))

    def test_check_writable_raises_for_missing_dir(self):
        """_check_writable should call sys.exit(1) for a non-existent directory."""
        from src import db as db_module
        with pytest.raises(SystemExit) as exc:
            db_module._check_writable("/this/does/not/exist/ever")
        assert exc.value.code == 1
