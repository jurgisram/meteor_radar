import math
from datetime import datetime, timezone, timedelta

import numpy as np

from src.acquisition import N_SAMPLES, SAMPLE_RATE
from src.detector import Event

_FFT_BIN_WIDTH_HZ = 10.0
_ROW_PERIOD_MS = float(N_SAMPLES) / float(SAMPLE_RATE) * 1000.0  # 100.0 ms
_CENTER_BIN = 20  # bin index corresponding to 143.050 MHz
_CLUSTER_GAP_S = 60.0


class EventWriter:
    def __init__(self, db_conn):
        self._conn = db_conn
        self._last_cluster_id = 0
        self._last_event_end_time = None

    def write(self, event: Event, baseline) -> int:
        cluster_id = self._assign_cluster(event)
        self._last_event_end_time = event.end_time

        spectrogram = np.vstack(event.frames).astype(np.float32)
        n_rows = spectrogram.shape[0]
        spectrogram_shape = f"{n_rows},40"
        spectrogram_blob = spectrogram.tobytes()

        duration_ms = (event.signal_end_time - event.start_time).total_seconds() * 1000
        peak_power_db = float(spectrogram.max())
        snr_db = float(peak_power_db - baseline.mean)
        integrated_power = float(np.sum(np.maximum(0.0, spectrogram - baseline.mean)))
        frequency_centroid_hz = self._compute_centroid(spectrogram)
        bandwidth_hz = self._compute_bandwidth(spectrogram, frequency_centroid_hz)

        cur = self._conn.execute(
            """
            INSERT INTO events (
                timestamp, duration_ms, peak_power_db, snr_db, integrated_power,
                frequency_centroid_hz, bandwidth_hz, suspected_rfi, cluster_id,
                baseline_mean_db, baseline_std_db, spectrogram, spectrogram_shape,
                fft_bin_width_hz, row_period_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.start_time.isoformat(),
                duration_ms,
                peak_power_db,
                snr_db,
                integrated_power,
                frequency_centroid_hz,
                bandwidth_hz,
                1 if event.suspected_rfi else 0,
                cluster_id,
                float(baseline.mean),
                float(baseline.std),
                spectrogram_blob,
                spectrogram_shape,
                _FFT_BIN_WIDTH_HZ,
                _ROW_PERIOD_MS,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def _assign_cluster(self, event: Event) -> int:
        if self._last_event_end_time is not None:
            gap_s = (event.start_time - self._last_event_end_time).total_seconds()
            if gap_s <= _CLUSTER_GAP_S:
                return self._last_cluster_id
        self._last_cluster_id += 1
        return self._last_cluster_id

    def _compute_centroid(self, spectrogram: np.ndarray) -> float:
        # Mean power across all rows, then power-weighted bin centroid
        mean_row = spectrogram.mean(axis=0).astype(np.float64)
        total = mean_row.sum()
        if total == 0:
            return 0.0
        bins = np.arange(40, dtype=np.float64)
        centroid_bin = (mean_row * bins).sum() / total
        return (centroid_bin - _CENTER_BIN) * _FFT_BIN_WIDTH_HZ

    def _compute_bandwidth(self, spectrogram: np.ndarray, centroid_hz: float) -> float:
        centroid_bin = centroid_hz / _FFT_BIN_WIDTH_HZ + _CENTER_BIN
        mean_row = spectrogram.mean(axis=0).astype(np.float64)
        total = mean_row.sum()
        if total == 0:
            return 0.0
        bins = np.arange(40, dtype=np.float64)
        variance_bins = (mean_row * (bins - centroid_bin) ** 2).sum() / total
        return math.sqrt(variance_bins) * _FFT_BIN_WIDTH_HZ
