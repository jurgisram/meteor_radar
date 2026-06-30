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

## Signal Acquisition

**Library:** `pyrtlsdr`

**Center frequency:** 143.3 MHz (PLL-friendly offset; 143.050 MHz extracted digitally from the FFT output — avoids the R820T2 PLL false-lock warning entirely)

**IQ sample rate:** 1.024 MSPS

**Chunk size:** 5ms (5,120 IQ samples per chunk at 1.024 MSPS)
- Yields 200 power measurements/second
- Resolves sub-100ms underdense meteor events that appear as a single blip at 1s resolution

**FFT per chunk:** Compute FFT over the chunk, extract power across ±200 Hz around 143.050 MHz
- Results in a frequency array of ~4 bins at 1.024 MSPS / 5120-point FFT (bin width ≈ 200 Hz); use zero-padding or adjust FFT size to get ~1 Hz resolution over the ±200 Hz band
- Detection triggered on the peak bin; full band stored per frame

---

## Detection Algorithm

### Baseline (noise floor)

- **Window:** 300 seconds = 60,000 samples at 200 Hz
- **Implementation:** Ring buffer with Welford's online algorithm for incremental mean and variance — O(1) per update, no recompute
- **Gated update:** Baseline is only updated when NOT in an active event. RFI bursts and meteor tails must not corrupt the noise floor estimate
- **Warmup:** Skip first 900 seconds (15 min) on cold start to let the dongle thermally stabilize. Persist last known baseline to SQLite so daemon restarts skip warmup if baseline was saved within the last 2 hours
- **Drift detection:** If baseline mean moves monotonically for >5 minutes, suppress event logging until it stabilizes (handles slow RFI environment changes)

### Threshold

- **Trigger:** Peak bin power > baseline_mean + 3.0 dB
- **Minimum duration filter:** Must exceed threshold for ≥2 consecutive samples (≥10ms) before logging as an event. Single-sample spikes are flagged separately as `suspected_rfi`
- **Debounce gap:** Events within 5 seconds of each other are merged into one event
- **Cluster detection:** Events within 60 seconds of each other are assigned a shared `cluster_id` (fragmentation sequences / multi-path reflections)

### Event capture window

- **Pre-trigger buffer:** Rolling ring buffer of the last 10 seconds of spectrogram frames (2,000 frames). On trigger, flush this as the event's pre-event context — captures the rise
- **Post-trigger window:** Continue recording for 10 seconds after the last threshold crossing — captures the exponential decay tail
- **Total window per event:** up to 20+ seconds of 2D spectrogram data

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
| `integrated_power` | REAL | Sum of (power - baseline) across event duration |
| `frequency_centroid_hz` | REAL | Power-weighted center frequency within ±200 Hz band |
| `bandwidth_hz` | REAL | Frequency spread (power-weighted std dev) |
| `suspected_rfi` | INTEGER | 1 if duration < 10ms (single/double sample spike) |
| `cluster_id` | INTEGER | Shared ID for events within 60s of each other (NULL if isolated) |
| `baseline_mean_db` | REAL | Baseline mean at time of detection |
| `baseline_std_db` | REAL | Baseline std dev at time of detection |
| `spectrogram` | BLOB | Serialized numpy float32 array, shape (N_frames, N_freq_bins) — full pre+event+post window |

### Table: `baseline_state`

| Column | Type | Description |
|--------|------|-------------|
| `saved_at` | TEXT | UTC timestamp |
| `mean_db` | REAL | Baseline mean to resume from |
| `variance_db` | REAL | Baseline variance to resume from |
| `sample_count` | INTEGER | Welford's n |

Saved every 30 minutes. On startup, loaded if saved within 2 hours.

---

## Process Architecture

```
main loop
  └─ pyrtlsdr async callback (5ms chunks)
       ├─ FFT → extract ±200 Hz power array
       ├─ update pre-trigger ring buffer (always)
       ├─ compute peak bin power
       ├─ check threshold against baseline
       │    ├─ [no event] → update baseline (Welford's)
       │    └─ [event] → accumulate spectrogram frames
       │         └─ [event ends] → write to SQLite
       └─ periodic: save baseline_state every 30 min
```

Single-threaded async loop. SQLite writes happen only at event end — no write contention.

---

## Data Volume Estimate

| Item | Rate | Size |
|------|------|------|
| Summary fields per event | ~2–5/hour indoor | negligible |
| Spectrogram per event | 20s window × 200 Hz × N_freq_bins × float32 | ~160 KB/event at 4 freq bins |
| Events/year (indoor, conservative) | ~35,000 | ~5.5 GB |
| Events/year (realistic ~2/hour) | ~17,500 | ~2.8 GB |

Still dramatically better than 240 GB/year from `rtl_power`. Adjust FFT resolution to control spectrogram size.

---

## File Layout

```
/mnt/hdd/
├── meteor_radar.db       # SQLite database
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

---

## Open Questions

1. **FFT size / frequency resolution:** What bin width do we want across the ±200 Hz band? 1 Hz resolution requires a 1,024,000-point FFT per chunk — likely too expensive. More practical: 5,120-point FFT at 1.024 MSPS = 200 Hz bin width, giving ~2 bins in ±200 Hz. Need to decide: coarser frequency resolution (fewer bins, cheaper) vs finer (more Doppler detail for visualization). Recommend testing with 200 Hz bins first.

2. **Spectrogram storage format:** Store as raw numpy bytes (compact, fast) or compressed (smaller, slower)? At ~160 KB/event uncompressed, yearly storage is manageable — recommend uncompressed for simplicity.

3. **pyrtlsdr async vs sync:** The library supports both callback-based async and synchronous read. Async is preferred for low-latency 5ms chunks but adds complexity. Decide at implementation time.
