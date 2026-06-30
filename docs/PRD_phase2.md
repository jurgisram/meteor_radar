# PRD: Phase 2 — Automated Detection Daemon

## Overview

Replace the post-hoc `rtl_power` + `validate_meteors.py` workflow with a continuous real-time Python daemon using `pyrtlsdr`. The daemon runs 24/7, detects meteor forward-scatter events as they happen, and stores per-event 2D spectrograms (time × frequency) in SQLite — the primary input for the Phase 4 visualization pipeline.

---

## Goals

1. Continuous unattended operation on the OptiPlex 3070 (headless Ubuntu, `/mnt/hdd/` storage)
2. Capture full spectrogram snapshots (pre-event + event + post-event tail) per detection
3. Storage under 1 GB/year (vs 240 GB/year with `rtl_power`)
4. Clean SQLite schema that feeds Phase 4 SVG generation directly

## Non-Goals

- Real-time alerting (ntfy deferred — not a current priority)
- Web dashboard or WebSocket API
- Antenna or hardware changes

---

## Pre-Implementation Checklist

Before writing code, verify on the OptiPlex:
- `python3 -c "import rtlsdr"` — confirm pyrtlsdr is installed (`pip install pyrtlsdr` if not; requires `librtlsdr` which is already installed)
- `/mnt/hdd/` is mounted and writable

---

## Signal Acquisition

**Library:** `pyrtlsdr` — use synchronous `read_samples()` in a tight loop for initial implementation. Simpler to debug than async callbacks; adequate latency at 100ms accumulation window.

**Center frequency:** 143.3 MHz (PLL-friendly offset; 143.050 MHz extracted digitally from FFT — avoids the R820T2 PLL false-lock warning entirely)

**IQ sample rate:** 1.024 MSPS

**Accumulation window:** 100ms = 102,400 IQ samples
- Read 20 × 5ms chunks (or one 100ms read), then compute one FFT
- Yields 10 spectrogram rows/second (10 Hz)
- FFT bin width: 1,024,000 / 102,400 = **10 Hz/bin**
- ±200 Hz band = 400 Hz = **40 bins** — sufficient to resolve Doppler chirp structure

**Why 100ms accumulation, not per-chunk FFT:**
- 5ms FFT gives 200 Hz/bin = only 2 bins across ±200 Hz band — not enough Doppler resolution
- 100ms FFT gives 10 Hz/bin = 40 bins — enough to see frequency spread and chirp signatures
- Processing rate drops from 200/s to 10/s — lower CPU overhead, smaller storage

---

## Detection Algorithm

### Baseline (noise floor)

- **Window:** 300 seconds = 3,000 samples at 10 Hz
- **Implementation:** Ring buffer tracking rolling mean. Recompute std dev every 10 seconds (every 100 samples) — costs ~3,000 multiplies (~0.05ms on modern hardware). Simpler and more correct than a sliding-window Welford's implementation.
- **Gated update:** Baseline is only updated when NOT in an active event. RFI bursts and meteor tails must not corrupt the noise floor estimate.
- **Long-RFI fallback:** If continuously in-event for >60 seconds, resume baseline updates — a genuine meteor event cannot last 60s, so this indicates persistent RFI.
- **Warmup:** Skip first 900 seconds (15 min) on cold start. Persist last known baseline to SQLite (`baseline_state` table); resume from saved state if saved within the last 2 hours — avoids repeated warmup on restarts.
- **Drift detection:** If baseline mean moves monotonically for >5 consecutive minutes, suppress event logging until it stabilizes.

### Threshold

- **Trigger:** Peak bin power (across the 40-bin ±200 Hz band) > `baseline_mean + 3.0 dB`
- **Minimum duration filter:** Must exceed threshold for ≥2 consecutive spectrogram rows (≥200ms) before logging as event. Single-row spikes are flagged as `suspected_rfi=1` and stored but marked separately.
- **Debounce gap:** Spectrogram rows within 5 seconds of each other are merged into one event.
- **Cluster detection — chain-link rule:** An event joins an existing cluster if its start time is within 60 seconds of the *previous event in that cluster*. This allows fragmentation sequences (A→B→C each 55s apart) to form one cluster, while two isolated events 61s apart remain separate.

### Event capture window

- **Pre-trigger buffer:** Rolling ring buffer of the last 100 seconds of spectrogram rows (1,000 rows at 10 Hz). On trigger, flush last 100 rows (~10 seconds) as pre-event context — captures the rise.
- **Post-trigger window:** Continue recording for 10 seconds after the last threshold crossing — captures the exponential decay tail.
- **Total stored window:** ~20 seconds of 2D spectrogram data per event (10s pre + up to ~0s–several seconds of event + 10s post).

---

## SQLite Schema

**File:** `/mnt/hdd/meteor_radar.db`

### Table: `events`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `timestamp` | TEXT | UTC ISO8601 of trigger moment |
| `duration_ms` | INTEGER | Time from first to last threshold crossing |
| `peak_power_db` | REAL | Peak power at strongest bin |
| `snr_db` | REAL | Peak power minus baseline mean at trigger |
| `integrated_power` | REAL | Sum of (power − baseline) across event duration and band |
| `frequency_centroid_hz` | REAL | Power-weighted center frequency within ±200 Hz band (relative to 143.050 MHz) |
| `bandwidth_hz` | REAL | Frequency spread (power-weighted std dev across band) |
| `suspected_rfi` | INTEGER | 1 if duration < 200ms (single/double spectrogram row) |
| `cluster_id` | INTEGER | Shared ID for chain-linked events within 60s (NULL if isolated) |
| `baseline_mean_db` | REAL | Baseline mean at time of detection |
| `baseline_std_db` | REAL | Baseline std dev at time of detection |
| `spectrogram` | BLOB | `numpy.ndarray.tobytes()`, float32, shape encoded in `spectrogram_shape` |
| `spectrogram_shape` | TEXT | e.g. `"200,40"` — rows × freq bins; required to deserialize BLOB in Phase 4 |
| `fft_bin_width_hz` | REAL | FFT bin width in Hz (e.g. 10.0) — calibration parameter for frequency_centroid_hz |

### Table: `baseline_state`

| Column | Type | Description |
|--------|------|-------------|
| `saved_at` | TEXT | UTC ISO8601 timestamp |
| `mean_db` | REAL | Rolling baseline mean |
| `std_db` | REAL | Rolling baseline std dev |
| `sample_count` | INTEGER | Number of samples in current window |
| `last_alive` | TEXT | UTC ISO8601 updated every 30s by daemon — external watchdog checks this |

One row, overwritten every 30 minutes (and every 30s for `last_alive`).

---

## Process Architecture

```
main loop (synchronous)
  └─ read_samples(102400)          # 100ms of IQ data
       ├─ FFT → 40-bin power array (±200 Hz around 143.050 MHz)
       ├─ append to pre-trigger ring buffer (1,000 rows)
       ├─ compute peak bin power
       ├─ check threshold vs baseline
       │    ├─ [below threshold]
       │    │    ├─ if not in long-RFI fallback: update baseline
       │    │    └─ if was in event: end event, write to SQLite
       │    └─ [above threshold]
       │         ├─ start event (flush pre-trigger buffer)
       │         └─ accumulate spectrogram frames
       ├─ every 100 samples (10s): recompute baseline std dev
       └─ every 300 samples (30s): write baseline_state to SQLite
```

Single-threaded synchronous loop. SQLite writes happen at event end (rare) and every 30s for state — no write contention.

**Error handling:** Catch `libusb`/`rtlsdr` exceptions on `read_samples()`. Log the error, attempt to re-open the device handle once, then exit (let systemd restart). Do not silently continue with no data.

---

## Data Volume Estimate

| Parameter | Value |
|-----------|-------|
| Spectrogram rows per event | ~200 (20s × 10 Hz) |
| Freq bins per row | 40 |
| Bytes per element | 4 (float32) |
| **Size per event** | **200 × 40 × 4 = 32 KB** |
| Events/year at 2/hour (indoor) | ~17,500 |
| **Storage/year** | **~560 MB** |
| Events/year at 10/hour (outdoor) | ~87,500 |
| Storage/year (outdoor) | ~2.8 GB |

Indoor rate comfortably under 1 GB/year. Store uncompressed — Phase 4 batch SVG generation benefits from fast random access.

---

## File Layout

```
/mnt/hdd/
├── meteor_radar.db       # SQLite database (WAL mode)
├── meteor_daemon.log     # Rotating log
└── validate_meteors.py   # Phase 1 script (retained for reference)

~/projects/meteor_radar/
├── CONTEXT.md
├── validate_meteors.py   # Phase 1 script (in repo)
├── docs/
│   └── PRD_phase2.md     # This document
└── src/
    └── daemon.py         # Phase 2 daemon (to be written)
```
