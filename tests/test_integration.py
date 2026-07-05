"""End-to-end integration test: baseline + detector on realistic noise."""

import numpy as np
import pytest
from src.baseline import BaselineTracker
from src.detector import Detector


def _warmup(baseline, detector, n_rows, rng, floor=-30.0, sigma=1.5):
    """Feed n_rows of synthetic noise through baseline + detector, simulating daemon loop."""
    events = []
    for i in range(n_rows):
        row = rng.normal(floor, sigma, 40).astype(np.float32)
        baseline.update(float(row.max()), in_event=detector.in_event)
        if baseline.is_warmed_up():
            ev = detector.feed(row, baseline)
            if ev:
                events.append(ev)
        if (i + 1) % 100 == 0:
            baseline.recompute_std()
    return events


class TestEndToEnd:
    def test_pure_noise_produces_no_events(self):
        rng = np.random.default_rng(42)
        baseline = BaselineTracker()
        detector = Detector()

        # Warmup: 9001 rows to satisfy WARMUP_SAMPLES=9000
        warmup_events = _warmup(baseline, detector, n_rows=9001, rng=rng)
        assert baseline.is_warmed_up()
        # No events should fire on pure noise
        assert len(warmup_events) == 0, f"Got {len(warmup_events)} events on pure noise"

    def test_burst_produces_one_event(self):
        rng = np.random.default_rng(99)
        baseline = BaselineTracker()
        detector = Detector()

        # Warmup
        _warmup(baseline, detector, n_rows=9001, rng=rng)
        assert baseline.is_warmed_up()
        assert baseline.std > 0

        # Inject a 10-row burst well above threshold
        burst_level = baseline.threshold_db + 10.0
        burst_row = np.full(40, burst_level, dtype=np.float32)

        events = []
        for _ in range(10):
            baseline.update(float(burst_row.max()), in_event=detector.in_event)
            ev = detector.feed(burst_row, baseline)
            if ev:
                events.append(ev)

        # Drain: 160 below-threshold rows to close the event
        noise_row = np.full(40, baseline.mean - 5.0, dtype=np.float32)
        for _ in range(160):
            baseline.update(float(noise_row.max()), in_event=detector.in_event)
            ev = detector.feed(noise_row, baseline)
            if ev:
                events.append(ev)

        assert len(events) == 1, f"Expected 1 event, got {len(events)}"
        assert events[0].suspected_rfi is False

    def test_short_burst_is_rfi(self):
        """A burst of exactly 1 row (< _MIN_DURATION_ROWS) is tagged suspected_rfi."""
        rng = np.random.default_rng(7)
        baseline = BaselineTracker()
        detector = Detector()

        _warmup(baseline, detector, n_rows=9001, rng=rng)

        burst_level = baseline.threshold_db + 10.0
        burst_row = np.full(40, burst_level, dtype=np.float32)
        noise_row = np.full(40, baseline.mean - 5.0, dtype=np.float32)

        events = []
        # Feed exactly 1 above-threshold row (< _MIN_DURATION_ROWS regardless of value)
        baseline.update(float(burst_row.max()), in_event=detector.in_event)
        ev = detector.feed(burst_row, baseline)
        if ev:
            events.append(ev)

        # Drain
        for _ in range(160):
            baseline.update(float(noise_row.max()), in_event=detector.in_event)
            ev = detector.feed(noise_row, baseline)
            if ev:
                events.append(ev)

        assert len(events) == 1
        assert events[0].suspected_rfi is True
