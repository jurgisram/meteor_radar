import numpy as np
import pytest
from datetime import datetime, timezone
from src.detector import Detector, Event

THRESHOLD = -30.0
ABOVE = np.full(40, -25.0, dtype=np.float32)
BELOW = np.full(40, -35.0, dtype=np.float32)
TS = datetime(2024, 1, 1, tzinfo=timezone.utc)

# Debounce = 50 rows, post-trigger = 100 rows.
# After last above-threshold row, need 51 rows to exit debounce + 99 more = 150 total.
# Use 160 as safe drain margin.
DRAIN = 160


class MockBaseline:
    threshold_db = THRESHOLD


BL = MockBaseline()


def drain(det, n=DRAIN):
    """Feed n below-threshold rows; collect any completed events."""
    events = []
    for _ in range(n):
        r = det.feed(BELOW, BL, timestamp=TS)
        if r:
            events.append(r)
    return events


class TestSingleRowSpike:
    def test_suspected_rfi_true(self):
        det = Detector()
        det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        assert len(events) == 1
        assert events[0].suspected_rfi is True

    def test_single_spike_has_one_event_frame(self):
        det = Detector()
        det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        spike_rows = [f for f in events[0].frames if f.max() > THRESHOLD]
        assert len(spike_rows) == 1


class TestTwoRowSpike:
    # With _MIN_DURATION_ROWS=5, two rows are not enough to promote to ACTIVE;
    # they exit PENDING as a suspected_rfi event instead.
    def test_two_rows_still_suspected_rfi(self):
        det = Detector()
        det.feed(ABOVE, BL, timestamp=TS)
        det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        assert len(events) == 1
        assert events[0].suspected_rfi is True

    def test_two_row_rfi_has_two_above_threshold_frames(self):
        det = Detector()
        det.feed(ABOVE, BL, timestamp=TS)
        det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        above_frames = [f for f in events[0].frames if f.max() > THRESHOLD]
        assert len(above_frames) == 2


class TestFiveRowEvent:
    # _MIN_DURATION_ROWS=5: exactly 5 consecutive above rows produces a real event.
    def test_five_rows_not_rfi(self):
        det = Detector()
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        assert len(events) == 1
        assert events[0].suspected_rfi is False

    def test_four_rows_still_rfi(self):
        det = Detector()
        for _ in range(4):
            det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        assert len(events) == 1
        assert events[0].suspected_rfi is True


class TestDebounce:
    def _run_with_gap(self, gap_rows):
        det = Detector()
        events = []

        def tick(row):
            r = det.feed(row, BL, timestamp=TS)
            if r:
                events.append(r)

        # Need 5 ABOVE rows to satisfy MIN_DURATION_ROWS and enter ACTIVE
        for _ in range(5):
            tick(ABOVE)
        for _ in range(gap_rows):
            tick(BELOW)
        for _ in range(5):
            tick(ABOVE)
        for _ in range(DRAIN):
            tick(BELOW)
        return events

    def test_gap_within_debounce_merges(self):
        # gap < 51 rows: second burst arrives while still in ACTIVE → one merged event
        events = self._run_with_gap(49)
        assert len(events) == 1

    def test_gap_exceeds_debounce_and_inter_event_gives_two_events(self):
        # gap >= 100: 51 rows exit debounce into POST, 49 more satisfy _MIN_INTER_EVENT_ROWS(50)
        events = self._run_with_gap(105)
        assert len(events) == 2


class TestPreTriggerBuffer:
    def test_pre_trigger_prepended_to_event(self):
        det = Detector()
        for _ in range(50):
            det.feed(BELOW, BL, timestamp=TS)
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        assert len(events) == 1
        below_frames = [f for f in events[0].frames if f.max() <= THRESHOLD]
        assert len(below_frames) > 0

    def test_pre_trigger_max_100_rows(self):
        det = Detector()
        for _ in range(300):
            det.feed(BELOW, BL, timestamp=TS)
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        assert len(events) == 1
        # Count only pre-event context (frames before the first above-threshold row)
        first_above = next(i for i, f in enumerate(events[0].frames) if f.max() > THRESHOLD)
        assert first_above <= 100


class TestPostTriggerWindow:
    def test_event_not_closed_before_100_post_trigger_rows(self):
        det = Detector()
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        # 50 debounce rows + 49 post-trigger rows = 99 total; event must still be open
        for _ in range(99):
            r = det.feed(BELOW, BL, timestamp=TS)
            assert r is None, "Event closed too early"

    def test_event_closes_after_100_post_trigger_rows(self):
        det = Detector()
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        result = None
        for _ in range(DRAIN):
            r = det.feed(BELOW, BL, timestamp=TS)
            if r:
                result = r
                break
        assert result is not None


class TestMaxActiveDuration:
    def test_force_close_at_max_active_rows(self):
        from src.detector import _MAX_ACTIVE_ROWS
        det = Detector()
        # Enter ACTIVE (needs MIN_DURATION_ROWS=5 above rows)
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        # Feed _MAX_ACTIVE_ROWS above rows; force-close transitions to POST, returns None
        result_during = None
        for _ in range(_MAX_ACTIVE_ROWS):
            r = det.feed(ABOVE, BL, timestamp=TS)
            if r is not None:
                result_during = r
        assert result_during is None
        assert det.in_event is True  # still in POST collecting context
        events = drain(det)
        assert len(events) == 1
        assert events[0].suspected_rfi is False

    def test_detector_recovers_after_force_close(self):
        from src.detector import _MAX_ACTIVE_ROWS
        det = Detector()
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        for _ in range(_MAX_ACTIVE_ROWS):
            det.feed(ABOVE, BL, timestamp=TS)
        drain(det)
        assert det.in_event is False
        # Normal event after recovery
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        events = drain(det)
        assert len(events) == 1


class TestInEvent:
    def test_in_event_false_initially(self):
        assert Detector().in_event is False

    def test_in_event_true_while_pending(self):
        # Even with only 2 rows (not yet ACTIVE), PENDING counts as in_event
        det = Detector()
        det.feed(ABOVE, BL, timestamp=TS)
        det.feed(ABOVE, BL, timestamp=TS)
        assert det.in_event is True

    def test_in_event_true_during_active(self):
        det = Detector()
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        assert det.in_event is True

    def test_in_event_true_during_debounce(self):
        det = Detector()
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        det.feed(BELOW, BL, timestamp=TS)
        assert det.in_event is True

    def test_in_event_true_during_post_trigger(self):
        det = Detector()
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        # Exhaust debounce window to enter post-trigger
        for _ in range(52):
            det.feed(BELOW, BL, timestamp=TS)
        assert det.in_event is True

    def test_in_event_false_after_event_closes(self):
        det = Detector()
        for _ in range(5):
            det.feed(ABOVE, BL, timestamp=TS)
        drain(det, DRAIN)
        assert det.in_event is False
