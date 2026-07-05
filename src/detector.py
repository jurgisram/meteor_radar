from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import numpy as np


@dataclass
class Event:
    frames: list
    start_time: datetime
    end_time: datetime
    signal_end_time: datetime   # true signal end (when debounce expired); same as end_time for RFI
    suspected_rfi: bool


_PRE_TRIGGER_CAPACITY = 1000   # rows in rolling pre-trigger buffer
_PRE_TRIGGER_FLUSH = 100       # rows of pre-event context prepended on trigger
_DEBOUNCE_ROWS = 50            # consecutive below-threshold rows before debounce expires
_POST_TRIGGER_ROWS = 100       # rows collected after debounce expires
_MIN_DURATION_ROWS = 2         # consecutive above-threshold rows to declare a real event (200 ms)
_MIN_INTER_EVENT_ROWS = 50     # minimum POST rows before a new event can start (5 s)
_MAX_ACTIVE_ROWS = 600         # 60 s at 10 Hz — force-close runaway events

_IDLE = 'idle'
_PENDING = 'pending'
_ACTIVE = 'active'
_POST = 'post'


class Detector:
    def __init__(self):
        self._pre_buf: deque = deque(maxlen=_PRE_TRIGGER_CAPACITY)
        self._state = _IDLE
        self._pending_row = None
        self._pending_ts = None
        self._pending_rows: list = []   # rows accumulated while waiting for MIN_DURATION_ROWS
        self._pending_count: int = 0    # above-threshold row count while PENDING
        self._frames: list = []
        self._start_time: Optional[datetime] = None
        self._below_count: int = 0   # consecutive below-threshold rows while ACTIVE
        self._active_row_count: int = 0  # total rows in ACTIVE state; triggers force-close at cap
        self._post_count: int = 0    # rows collected in post-trigger window
        self._signal_end_time: Optional[datetime] = None  # recorded when ACTIVE→POST

    @property
    def in_event(self) -> bool:
        return self._state in (_PENDING, _ACTIVE, _POST)

    def feed(self, row: np.ndarray, baseline, timestamp: Optional[datetime] = None) -> Optional['Event']:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        above = bool(row.max() > baseline.threshold_db)

        if self._state == _IDLE:
            if above:
                self._pending_row = row
                self._pending_ts = timestamp
                self._pending_rows = [row]
                self._pending_count = 1
                self._state = _PENDING
            else:
                self._pre_buf.append(row)
            return None

        if self._state == _PENDING:
            if above:
                self._pending_rows.append(row)
                self._pending_count += 1
                if self._pending_count >= _MIN_DURATION_ROWS:
                    # Signal sustained long enough — promote to ACTIVE
                    pre = list(self._pre_buf)[-_PRE_TRIGGER_FLUSH:]
                    self._frames = pre + list(self._pending_rows)
                    self._start_time = self._pending_ts
                    self._below_count = 0
                    self._active_row_count = 0
                    self._pending_row = None
                    self._pending_ts = None
                    self._pending_rows = []
                    self._pending_count = 0
                    self._state = _ACTIVE
            else:
                # Too short — emit all accumulated pending rows as suspected RFI
                rfi_rows = list(self._pending_rows)
                rfi_ts = self._pending_ts
                self._pending_row = None
                self._pending_ts = None
                self._pending_rows = []
                self._pending_count = 0
                for r in rfi_rows:
                    self._pre_buf.append(r)
                self._pre_buf.append(row)
                self._state = _IDLE
                return Event(frames=rfi_rows, start_time=rfi_ts, end_time=timestamp,
                             signal_end_time=timestamp, suspected_rfi=True)
            return None

        if self._state == _ACTIVE:
            self._frames.append(row)
            self._active_row_count += 1
            if self._active_row_count >= _MAX_ACTIVE_ROWS:
                # Force-close: runaway event — transition to POST to collect final context
                self._signal_end_time = timestamp
                self._state = _POST
                self._post_count = 1
                self._active_row_count = 0
                return None
            if above:
                self._below_count = 0
            else:
                self._below_count += 1
                if self._below_count > _DEBOUNCE_ROWS:
                    # Debounce expired — record true signal end and begin post-trigger window
                    self._signal_end_time = timestamp
                    self._state = _POST
                    self._post_count = 1   # this row is the first post-trigger row
            return None

        if self._state == _POST:
            self._frames.append(row)
            self._post_count += 1
            if above and self._post_count >= _MIN_INTER_EVENT_ROWS:
                # Sufficient quiet time has passed — close current event and start new pending
                event = self._close_event(timestamp)
                self._pending_row = row
                self._pending_ts = timestamp
                self._pending_rows = [row]
                self._pending_count = 1
                self._state = _PENDING
                return event
            if self._post_count >= _POST_TRIGGER_ROWS:
                return self._close_event(timestamp)
            return None

        return None  # unreachable

    def _close_event(self, end_time: datetime) -> Event:
        signal_end = self._signal_end_time if self._signal_end_time is not None else end_time
        event = Event(
            frames=list(self._frames),
            start_time=self._start_time,
            end_time=end_time,
            signal_end_time=signal_end,
            suspected_rfi=False,
        )
        self._frames = []
        self._start_time = None
        self._below_count = 0
        self._active_row_count = 0
        self._post_count = 0
        self._signal_end_time = None
        self._state = _IDLE
        return event
