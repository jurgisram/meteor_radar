import os
import sqlite3
import sys


def _check_rtlsdr():
    try:
        import rtlsdr  # noqa: F401
    except ImportError:
        print(
            "ERROR: pyrtlsdr is not installed.\n"
            "Install it with: pip install pyrtlsdr\n"
            "(requires librtlsdr: sudo apt install librtlsdr-dev)",
            file=sys.stderr,
        )
        sys.exit(1)


def _check_writable(path: str):
    if not os.path.isdir(path) or not os.access(path, os.W_OK):
        print(
            f"ERROR: '{path}' is not a writable directory.\n"
            "Mount the HDD and ensure the path exists before running.",
            file=sys.stderr,
        )
        sys.exit(1)


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id                   INTEGER PRIMARY KEY,
            timestamp            TEXT,
            duration_ms          INTEGER,
            peak_power_db        REAL,
            snr_db               REAL,
            integrated_power     REAL,
            frequency_centroid_hz REAL,
            bandwidth_hz         REAL,
            suspected_rfi        INTEGER,
            cluster_id           INTEGER,
            baseline_mean_db     REAL,
            baseline_std_db      REAL,
            spectrogram          BLOB,
            spectrogram_shape    TEXT,
            fft_bin_width_hz     REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baseline_state (
            saved_at      TEXT,
            mean_db       REAL,
            std_db        REAL,
            sample_count  INTEGER,
            last_alive    TEXT
        )
    """)
    conn.commit()
    return conn


if __name__ == "__main__":
    _check_rtlsdr()
    _check_writable("/mnt/hdd/meteor_radar")
    conn = init_db("/mnt/hdd/meteor_radar/meteor_radar.db")
    conn.close()
    print("Database initialised at /mnt/hdd/meteor_radar/meteor_radar.db")
