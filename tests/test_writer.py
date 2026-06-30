import math
import sqlite3
import tempfile
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.db import init_db
from src.detector import Event
from src.writer import EventWriter


def make_frames(n=5, value=-25.0):
    return [np.full(40, value, dtype=np.float32) for _ in range(n)]


def make_event(start_offset_s=0, duration_s=1.0, suspected_rfi=False, frames=None):
    base = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    start = base + timedelta(seconds=start_offset_s)
    end = start + timedelta(seconds=duration_s)
    return Event(
        frames=frames if frames is not None else make_frames(),
        start_time=start,
        end_time=end,
        suspected_rfi=suspected_rfi,
    )


class FakeBaseline:
    def __init__(self, mean=-35.0, std=1.0):
        self.mean = mean
        self.std = std


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = init_db(path)
    yield conn
    conn.close()
    os.unlink(path)


class TestClusterAssignment:
    def test_first_event_gets_cluster_1(self, db):
        writer = EventWriter(db)
        writer.write(make_event(0), FakeBaseline())
        row = db.execute("SELECT cluster_id FROM events").fetchone()
        assert row[0] == 1

    def test_events_within_60s_share_cluster(self, db):
        writer = EventWriter(db)
        writer.write(make_event(0, duration_s=1), FakeBaseline())
        writer.write(make_event(55, duration_s=1), FakeBaseline())   # 55s after end of first
        writer.write(make_event(110, duration_s=1), FakeBaseline())  # 55s after end of second
        rows = db.execute("SELECT cluster_id FROM events ORDER BY rowid").fetchall()
        ids = [r[0] for r in rows]
        assert ids[0] == ids[1] == ids[2]

    def test_event_beyond_60s_gets_new_cluster(self, db):
        writer = EventWriter(db)
        writer.write(make_event(0, duration_s=1), FakeBaseline())
        writer.write(make_event(55, duration_s=1), FakeBaseline())
        writer.write(make_event(110, duration_s=1), FakeBaseline())
        writer.write(make_event(172, duration_s=1), FakeBaseline())  # 61s after end of third
        rows = db.execute("SELECT cluster_id FROM events ORDER BY rowid").fetchall()
        ids = [r[0] for r in rows]
        assert ids[3] != ids[0]
        assert ids[3] == ids[0] + 1

    def test_isolated_events_get_incrementing_clusters(self, db):
        writer = EventWriter(db)
        writer.write(make_event(0, duration_s=1), FakeBaseline())
        writer.write(make_event(200, duration_s=1), FakeBaseline())
        writer.write(make_event(400, duration_s=1), FakeBaseline())
        rows = db.execute("SELECT cluster_id FROM events ORDER BY rowid").fetchall()
        ids = [r[0] for r in rows]
        assert ids == [1, 2, 3]


class TestSpectrogramSerialisation:
    def test_round_trip(self, db):
        frames = [np.arange(40, dtype=np.float32) * i for i in range(1, 6)]
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), FakeBaseline())
        row = db.execute("SELECT spectrogram, spectrogram_shape FROM events").fetchone()
        blob, shape_str = row
        n, bins = map(int, shape_str.split(","))
        recovered = np.frombuffer(blob, dtype=np.float32).reshape((n, bins))
        expected = np.vstack(frames).astype(np.float32)
        np.testing.assert_array_equal(recovered, expected)

    def test_shape_string(self, db):
        frames = make_frames(n=7)
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), FakeBaseline())
        shape_str = db.execute("SELECT spectrogram_shape FROM events").fetchone()[0]
        assert shape_str == "7,40"

    def test_fft_bin_width(self, db):
        writer = EventWriter(db)
        writer.write(make_event(), FakeBaseline())
        val = db.execute("SELECT fft_bin_width_hz FROM events").fetchone()[0]
        assert val == 10.0


class TestDerivedMetrics:
    def test_duration_ms(self, db):
        writer = EventWriter(db)
        writer.write(make_event(0, duration_s=2.5), FakeBaseline())
        val = db.execute("SELECT duration_ms FROM events").fetchone()[0]
        assert val == pytest.approx(2500.0)

    def test_peak_power_db(self, db):
        frames = [np.full(40, -28.0, dtype=np.float32), np.full(40, -22.0, dtype=np.float32)]
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), FakeBaseline(mean=-35.0))
        val = db.execute("SELECT peak_power_db FROM events").fetchone()[0]
        assert val == pytest.approx(-22.0)

    def test_snr_db(self, db):
        frames = [np.full(40, -22.0, dtype=np.float32)]
        baseline = FakeBaseline(mean=-35.0)
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), baseline)
        val = db.execute("SELECT snr_db FROM events").fetchone()[0]
        assert val == pytest.approx(-22.0 - (-35.0))  # 13.0

    def test_suspected_rfi_flag(self, db):
        writer = EventWriter(db)
        writer.write(make_event(suspected_rfi=True), FakeBaseline())
        val = db.execute("SELECT suspected_rfi FROM events").fetchone()[0]
        assert val == 1

    def test_suspected_rfi_false(self, db):
        writer = EventWriter(db)
        writer.write(make_event(suspected_rfi=False), FakeBaseline())
        val = db.execute("SELECT suspected_rfi FROM events").fetchone()[0]
        assert val == 0

    def test_baseline_columns_written(self, db):
        baseline = FakeBaseline(mean=-36.5, std=1.2)
        writer = EventWriter(db)
        writer.write(make_event(), baseline)
        row = db.execute("SELECT baseline_mean_db, baseline_std_db FROM events").fetchone()
        assert row[0] == pytest.approx(-36.5)
        assert row[1] == pytest.approx(1.2)

    def test_timestamp_written(self, db):
        event = make_event(start_offset_s=0)
        writer = EventWriter(db)
        writer.write(event, FakeBaseline())
        ts = db.execute("SELECT timestamp FROM events").fetchone()[0]
        assert ts == event.start_time.isoformat()

    def test_integrated_power(self, db):
        baseline = FakeBaseline(mean=-35.0)
        # Each frame: all bins at -30.0, so each bin contributes max(0, -30 - (-35)) = 5.0
        # 40 bins * 5.0 * 3 frames = 600.0
        frames = [np.full(40, -30.0, dtype=np.float32) for _ in range(3)]
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), baseline)
        val = db.execute("SELECT integrated_power FROM events").fetchone()[0]
        assert val == pytest.approx(600.0)


class TestFrequencyMetrics:
    def test_centroid_center_bin(self, db):
        # All power in bin 20 → centroid at bin 20 → 0 Hz relative to 143.050 MHz
        frames = [np.zeros(40, dtype=np.float32)]
        frames[0][20] = -10.0
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), FakeBaseline())
        val = db.execute("SELECT frequency_centroid_hz FROM events").fetchone()[0]
        assert val == pytest.approx(0.0, abs=0.01)

    def test_centroid_offset_bin(self, db):
        # All power in bin 25 → centroid at bin 25 → (25-20)*10 = 50 Hz
        frames = [np.zeros(40, dtype=np.float32)]
        frames[0][25] = -10.0
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), FakeBaseline())
        val = db.execute("SELECT frequency_centroid_hz FROM events").fetchone()[0]
        assert val == pytest.approx(50.0, abs=0.01)

    def test_bandwidth_uniform(self, db):
        # Uniform power across all bins → bandwidth should be positive
        frames = [np.full(40, -10.0, dtype=np.float32)]
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), FakeBaseline())
        val = db.execute("SELECT bandwidth_hz FROM events").fetchone()[0]
        assert val > 0

    def test_bandwidth_single_bin_zero(self, db):
        # All power in one bin → zero spread
        frames = [np.zeros(40, dtype=np.float32)]
        frames[0][20] = -10.0
        writer = EventWriter(db)
        writer.write(make_event(frames=frames), FakeBaseline())
        val = db.execute("SELECT bandwidth_hz FROM events").fetchone()[0]
        assert val == pytest.approx(0.0, abs=0.01)

    def test_returns_row_id(self, db):
        writer = EventWriter(db)
        row_id = writer.write(make_event(), FakeBaseline())
        assert isinstance(row_id, int)
        assert row_id >= 1
