"""Phase 2 meteor detection daemon — synchronous main loop."""

import logging
import signal
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from src.acquisition import Acquisition, AcquisitionError
from src.baseline import BaselineTracker
from src.db import init_db
from src.detector import Detector
from src.writer import EventWriter

LOG_PATH = '/mnt/hdd/meteor_daemon.log'
DB_PATH = '/mnt/hdd/meteor_radar.db'

log = logging.getLogger(__name__)


def _setup_logging():
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fh = RotatingFileHandler(LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def _update_alive(db_conn):
    now = datetime.now(timezone.utc).isoformat()
    db_conn.execute("UPDATE baseline_state SET last_alive = ?", (now,))
    db_conn.commit()


def run_loop(*, acq, baseline, detector, writer, db_conn, max_iterations=None):
    """
    Core processing loop, extracted for testability.

    Runs forever when max_iterations is None; otherwise stops after that many
    iterations. Returns None normally; raises SystemExit(1) on unrecoverable
    acquisition failure.
    """
    sample_count = 0
    i = 0

    while max_iterations is None or i < max_iterations:
        # --- acquire ---
        try:
            row = acq.read_row()
        except AcquisitionError as exc:
            log.error("AcquisitionError: %s — attempting device reconnect", exc)
            try:
                acq.close()
                acq.open_device()
                try:
                    row = acq.read_row()
                except AcquisitionError as exc2:
                    log.error("Reconnect failed: %s — exiting", exc2)
                    sys.exit(1)
            except AcquisitionError as exc2:
                log.error("Reconnect open failed: %s — exiting", exc2)
                sys.exit(1)

        # --- baseline update (gated by detector state) ---
        baseline.update(row.max(), in_event=detector.in_event)

        # --- detection ---
        event = detector.feed(row, baseline)
        if event and baseline.is_warmed_up() and not baseline.is_drifting():
            writer.write(event, baseline)
            log.info(
                "Event: timestamp=%s snr_db=%.1f duration_ms=%d suspected_rfi=%s",
                event.start_time.isoformat(),
                getattr(baseline, 'mean', 0),
                int((event.end_time - event.start_time).total_seconds() * 1000),
                event.suspected_rfi,
            )

        sample_count += 1
        i += 1

        if sample_count % 100 == 0:
            baseline.recompute_std()

        if sample_count % 300 == 0:
            baseline.save(db_conn)
            _update_alive(db_conn)
            log.info("Baseline saved (sample %d)", sample_count)

    return None


def main():
    _setup_logging()
    log.info("meteor_radar daemon starting")

    db_conn = init_db(DB_PATH)
    baseline = BaselineTracker()
    if not baseline.load(db_conn):
        log.info("Cold start — will warm up over 900 s")
    else:
        log.info("Restored baseline state from DB, warmup skipped")

    acq = Acquisition()
    acq.open_device()
    log.info("RTL-SDR opened")

    detector = Detector()
    writer = EventWriter(db_conn)

    def _shutdown(signum, frame):
        log.info("Signal %d received — shutting down", signum)
        baseline.save(db_conn)
        acq.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        run_loop(acq=acq, baseline=baseline, detector=detector,
                 writer=writer, db_conn=db_conn)
    except (KeyboardInterrupt, SystemExit):
        baseline.save(db_conn)
        acq.close()
        raise


if __name__ == '__main__':
    main()
