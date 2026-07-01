"""Tests for scripts/watchdog.py."""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

# Make scripts/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

VILNIUS_TZ = ZoneInfo("Europe/Vilnius")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """In-memory-style temp SQLite DB with the baseline_state and events tables."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE baseline_state (
            saved_at TEXT, mean_db REAL, std_db REAL, sample_count INTEGER, last_alive TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, timestamp TEXT, duration_ms INTEGER,
            peak_power_db REAL, snr_db REAL, integrated_power REAL,
            frequency_centroid_hz REAL, bandwidth_hz REAL, suspected_rfi INTEGER,
            cluster_id INTEGER, baseline_mean_db REAL, baseline_std_db REAL,
            spectrogram BLOB, spectrogram_shape TEXT, fft_bin_width_hz REAL
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def tmp_state(tmp_path):
    """Temp state file path (does not exist initially)."""
    return str(tmp_path / "state.json")


def _insert_last_alive(db_path, dt_utc=None):
    conn = sqlite3.connect(db_path)
    val = dt_utc.isoformat() if dt_utc else None
    conn.execute("INSERT INTO baseline_state (last_alive) VALUES (?)", (val,))
    conn.commit()
    conn.close()


def _insert_event(db_path, ts_utc):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO events (timestamp) VALUES (?)", (ts_utc.isoformat(),))
    conn.commit()
    conn.close()


def _run_watchdog(db_path, state_path, now_utc, env=None):
    """Run watchdog.main() with mocked DB path, state path, and time."""
    import watchdog as wd

    if env is None:
        env = {"DISCORD_WEBHOOK_URL": "https://discord.example.com/webhook"}

    with patch.dict(os.environ, env, clear=False), \
         patch.object(wd, 'DB_PATH', db_path), \
         patch.object(wd, 'STATE_PATH', state_path), \
         patch('watchdog.datetime') as mock_dt, \
         patch('watchdog.requests') as mock_requests:

        # Mock datetime.now to return our controlled time
        mock_dt.now.return_value = now_utc
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        try:
            wd.main()
        except SystemExit as e:
            return e.code, mock_requests

        return None, mock_requests


# ---------------------------------------------------------------------------
# Test 1: NULL last_alive → exit 0, no POST
# ---------------------------------------------------------------------------

class TestNullLastAlive:
    def test_null_last_alive_exits_zero_no_post(self, tmp_db, tmp_state):
        _insert_last_alive(tmp_db, None)
        now = datetime.now(timezone.utc)

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now)
        assert exit_code == 0
        mock_requests.post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Stale heartbeat → alert; second fire within 60 min → suppressed
# ---------------------------------------------------------------------------

class TestStaleHeartbeat:
    def test_stale_sends_alert(self, tmp_db, tmp_state):
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(minutes=15)
        _insert_last_alive(tmp_db, stale_time)

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now)
        assert exit_code is None
        mock_requests.post.assert_called_once()
        call_kwargs = mock_requests.post.call_args
        content = call_kwargs[1]['json']['content']
        assert "stale" in content
        assert "15 min ago" in content

    def test_second_alert_within_60_min_suppressed(self, tmp_db, tmp_state):
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(minutes=15)
        _insert_last_alive(tmp_db, stale_time)

        # Write state as if we just sent an alert 5 min ago
        alert_time = now - timedelta(minutes=5)
        state = {
            "alert_sent_at": alert_time.isoformat(),
            "recovered": False,
            "last_summary_date": None,
        }
        with open(tmp_state, 'w') as f:
            json.dump(state, f)

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now)
        assert exit_code is None
        mock_requests.post.assert_not_called()

    def test_alert_after_60_min_suppression_expires(self, tmp_db, tmp_state):
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(minutes=20)
        _insert_last_alive(tmp_db, stale_time)

        # Last alert was 65 min ago → suppression expired
        alert_time = now - timedelta(minutes=65)
        state = {
            "alert_sent_at": alert_time.isoformat(),
            "recovered": False,
            "last_summary_date": None,
        }
        with open(tmp_state, 'w') as f:
            json.dump(state, f)

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now)
        assert exit_code is None
        mock_requests.post.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: Fresh heartbeat after alert → recovery notice sent once
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_recovery_notice_sent_once(self, tmp_db, tmp_state):
        now = datetime.now(timezone.utc)
        fresh_time = now - timedelta(minutes=2)
        _insert_last_alive(tmp_db, fresh_time)

        # State: alerted but not yet recovered
        state = {
            "alert_sent_at": (now - timedelta(hours=1)).isoformat(),
            "recovered": False,
            "last_summary_date": None,
        }
        with open(tmp_state, 'w') as f:
            json.dump(state, f)

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now)
        assert exit_code is None
        mock_requests.post.assert_called_once()
        content = mock_requests.post.call_args[1]['json']['content']
        assert "recovered" in content.lower()

        # Saved state should mark recovered=True
        with open(tmp_state) as f:
            saved = json.load(f)
        assert saved["recovered"] is True

    def test_no_recovery_notice_if_already_recovered(self, tmp_db, tmp_state):
        now = datetime.now(timezone.utc)
        fresh_time = now - timedelta(minutes=2)
        _insert_last_alive(tmp_db, fresh_time)

        # Already recovered — no alert pending
        state = {
            "alert_sent_at": None,
            "recovered": True,
            "last_summary_date": None,
        }
        with open(tmp_state, 'w') as f:
            json.dump(state, f)

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now)
        assert exit_code is None
        mock_requests.post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Daily summary fires once in 09:xx Vilnius hour
# ---------------------------------------------------------------------------

class TestDailySummary:
    def test_daily_summary_fires_at_09_vilnius(self, tmp_db, tmp_state):
        # Construct a datetime that is 09:30 in Vilnius
        vilnius = ZoneInfo("Europe/Vilnius")
        now_vilnius = datetime(2026, 7, 1, 9, 30, 0, tzinfo=vilnius)
        now_utc = now_vilnius.astimezone(timezone.utc)

        fresh_time = now_utc - timedelta(minutes=2)
        _insert_last_alive(tmp_db, fresh_time)
        _insert_event(tmp_db, now_utc - timedelta(hours=5))
        _insert_event(tmp_db, now_utc - timedelta(hours=10))

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now_utc)
        assert exit_code is None
        mock_requests.post.assert_called_once()
        content = mock_requests.post.call_args[1]['json']['content']
        assert "Daily summary" in content
        assert "2 events in 24h" in content

        # Second run same hour should not send again
        with open(tmp_state) as f:
            saved = json.load(f)
        assert saved["last_summary_date"] == "2026-07-01"

    def test_daily_summary_not_sent_outside_09_hour(self, tmp_db, tmp_state):
        vilnius = ZoneInfo("Europe/Vilnius")
        now_vilnius = datetime(2026, 7, 1, 10, 0, 0, tzinfo=vilnius)
        now_utc = now_vilnius.astimezone(timezone.utc)

        fresh_time = now_utc - timedelta(minutes=2)
        _insert_last_alive(tmp_db, fresh_time)

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now_utc)
        assert exit_code is None
        mock_requests.post.assert_not_called()

    def test_daily_summary_not_sent_twice_same_day(self, tmp_db, tmp_state):
        vilnius = ZoneInfo("Europe/Vilnius")
        now_vilnius = datetime(2026, 7, 1, 9, 45, 0, tzinfo=vilnius)
        now_utc = now_vilnius.astimezone(timezone.utc)

        fresh_time = now_utc - timedelta(minutes=2)
        _insert_last_alive(tmp_db, fresh_time)

        # Already sent today
        state = {
            "alert_sent_at": None,
            "recovered": True,
            "last_summary_date": "2026-07-01",
        }
        with open(tmp_state, 'w') as f:
            json.dump(state, f)

        exit_code, mock_requests = _run_watchdog(tmp_db, tmp_state, now_utc)
        assert exit_code is None
        mock_requests.post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: DISCORD_WEBHOOK_URL missing → exit 1
# ---------------------------------------------------------------------------

class TestMissingWebhook:
    def test_missing_webhook_exits_1(self, tmp_db, tmp_state):
        now = datetime.now(timezone.utc)
        _insert_last_alive(tmp_db, now - timedelta(minutes=2))

        # Remove webhook URL from env
        env_without_webhook = {k: v for k, v in os.environ.items()
                                if k != 'DISCORD_WEBHOOK_URL'}

        import watchdog as wd

        with patch.dict(os.environ, env_without_webhook, clear=True), \
             patch.object(wd, 'DB_PATH', tmp_db), \
             patch.object(wd, 'STATE_PATH', tmp_state):
            with pytest.raises(SystemExit) as exc_info:
                wd.main()
            assert exc_info.value.code == 1
