from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional
import sqlite3
import math


class BaselineTracker:
    RING_SIZE = 3000       # 300s at 10 Hz
    WARMUP_SAMPLES = 9000  # 900s at 10 Hz
    RFI_FALLBACK_COUNT = 600  # 60s at 10 Hz

    def __init__(self):
        self._ring: deque = deque(maxlen=self.RING_SIZE)
        self._mean: float = 0.0
        self._std: float = 0.0
        self._total_samples: int = 0
        self._consecutive_event: int = 0
        self._warmed_up: bool = False
        # Drift tracking: store mean snapshots at each update to check monotonicity
        self._mean_history: deque = deque(maxlen=self.RING_SIZE)

    def update(self, power: float, in_event: bool) -> None:
        self._total_samples += 1
        if not self._warmed_up and self._total_samples >= self.WARMUP_SAMPLES:
            self._warmed_up = True

        if in_event:
            self._consecutive_event += 1
            if self._consecutive_event <= self.RFI_FALLBACK_COUNT:
                return  # gated — don't update baseline
            # Long-RFI fallback: update anyway
        else:
            self._consecutive_event = 0

        self._add_sample(power)

    def _add_sample(self, power: float) -> None:
        old_len = len(self._ring)
        if old_len == self.RING_SIZE:
            evicted = self._ring[0]
        else:
            evicted = None

        self._ring.append(power)
        new_len = len(self._ring)

        # Incremental mean update
        if evicted is not None:
            # Ring was full: remove evicted, add new
            self._mean += (power - evicted) / self.RING_SIZE
        elif new_len == 1:
            self._mean = power
        else:
            self._mean += (power - self._mean) / new_len

        self._mean_history.append(self._mean)

    def recompute_std(self) -> None:
        if len(self._ring) < 2:
            self._std = 0.0
            return
        n = len(self._ring)
        mean = self._mean
        variance = sum((x - mean) ** 2 for x in self._ring) / n
        self._std = math.sqrt(variance)

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def std(self) -> float:
        return self._std

    @property
    def threshold_db(self) -> float:
        return self._mean + 3.0

    def is_warmed_up(self) -> bool:
        return self._warmed_up

    def is_drifting(self) -> bool:
        history = list(self._mean_history)
        if len(history) < 2:
            return False
        if history[0] == history[-1]:
            return False
        increasing = all(history[i] <= history[i + 1] for i in range(len(history) - 1))
        decreasing = all(history[i] >= history[i + 1] for i in range(len(history) - 1))
        return increasing or decreasing

    def save(self, db_conn: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc).isoformat()
        db_conn.execute("DELETE FROM baseline_state")
        db_conn.execute(
            "INSERT INTO baseline_state (saved_at, mean_db, std_db, sample_count, last_alive) VALUES (?,?,?,?,?)",
            # Cast to Python float: sqlite3 doesn't recognise numpy scalars and
            # would store them as 4-byte BLOBs, corrupting the restored state.
            (now, float(self._mean), float(self._std), len(self._ring), now),
        )
        db_conn.commit()

    def load(self, db_conn: sqlite3.Connection) -> bool:
        row = db_conn.execute(
            "SELECT saved_at, mean_db, std_db, sample_count FROM baseline_state ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return False

        try:
            saved_at_str, mean_db, std_db, sample_count = row
            saved_at = datetime.fromisoformat(saved_at_str)
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - saved_at
            if age > timedelta(hours=2):
                return False

            self._mean = float(mean_db)
            self._std = float(std_db)
            # Restore ring with sample_count copies of mean (approximation for std recompute)
            n = min(sample_count, self.RING_SIZE)
            self._ring = deque([float(mean_db)] * n, maxlen=self.RING_SIZE)
            self._warmed_up = True
            self._total_samples = self.WARMUP_SAMPLES  # mark as past warmup
            return True
        except (ValueError, TypeError):
            # Corrupt or unexpected DB value (e.g. old BLOB-stored numpy scalar) — cold start
            return False
