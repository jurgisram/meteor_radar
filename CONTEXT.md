# Meteor Radar — Project Context

## What This Is

Forward scatter meteor detection using an RTL-SDR, monitoring the GRAVES space surveillance radar (143.050 MHz, Dijon, France) from Vilnius, Lithuania (~1800 km). Meteors create brief ionized trails that reflect the GRAVES signal toward the receiver. The end goal is a collection of printable, generative SVG visualizations — one unique artifact per detected event, derived from each event's raw signal shape.

## Hardware (Fixed — No Planned Changes)

- **Dongle**: RTL-SDR Blog V3 (R820T2 tuner)
- **Antenna**: Stock kit dipole, both arms extended to **52cm** (half-wave for 143 MHz), mounted **horizontally** on a window
- **Server**: Dell OptiPlex (model TBD — verify with `sudo dmidecode -s system-product-name`), headless Ubuntu
- **Data storage**: `/mnt/hdd/`
- **Software**: `rtl-sdr`, `librtlsdr-dev` installed; `dvb_usb_rtl28xxu` kernel module blacklisted via `/etc/modprobe.d/blacklist-rtlsdr.conf`

Antenna is indoors with limited sky view — outdoor placement is not feasible. This is the final hardware configuration.

## Current State: Phase 1 Complete

Detection confirmed working. Best validation run: **25 events over 11 hours (2.3/hour)**, including strong events (8.9 dB SNR), multi-second duration events (2s, 7s — characteristic meteor trail decay), and temporal clustering consistent with fragmentation. Rate is below the 5–10/hour sporadic background, attributed to the indoor antenna.

## Validated Detection Parameters

| Parameter | Value |
|-----------|-------|
| Target frequency | 143.050 MHz (GRAVES) |
| RTL-SDR gain | 40.20 dB |
| Warmup skip | 900 s |
| Rolling baseline window | 300 s |
| Detection threshold | 3.0 dB above baseline |
| Event debounce (gap) | 5 s |

**Why gain 40, not max (49.6 dB):** Higher gain raises the noise floor to -21 dB, drowning weak signals. Gain 40 gives a clean -28 to -35 dB floor. Tested overnight: 1 event at max gain vs 4 events at gain 40.

## Known Gotchas

**PLL warning is a false alarm.** Every `rtl_power` run at 143 MHz prints `[R82XX] PLL not locked!`. This is a known quirk of R820T2 chips — the status register lies. Data is valid regardless. Ignore it.

**Thermal warmup drift.** The dongle drifts for the first 10–15 minutes. Skip the first 900 seconds of any capture before computing baseline.

**SSH sessions.** Always run captures under `tmux`, `nohup`, or `screen`. Session disconnect kills the process.

**Center frequency offset.** For the Phase 2 `pyrtlsdr` pipeline, tune to **143.3 MHz** (PLL-friendly) and digitally extract the 143.050 MHz bin. This sidesteps any PLL lock concerns entirely.

## Existing Scripts

`/mnt/hdd/validate_meteors.py` — post-hoc validation of `rtl_power` CSV output. Streams line by line, skips warmup, computes rolling baseline, flags threshold crossings, groups into events, saves `events.csv` and `validation_result.png`.

```
python3 /mnt/hdd/validate_meteors.py /path/to/output.csv
```

Reference capture command (Phase 1 only, not for Phase 2):
```
nohup rtl_power -f 143.00M:143.10M:100 -g 40 -i 1 -e 12h /mnt/hdd/output.csv > /dev/null 2>&1 &
```

## Phase Plan

### Phase 2 — Automated Detection Pipeline (NEXT)

Replace `rtl_power` with a custom Python daemon using `pyrtlsdr`. The key motivation: `rtl_power` generates ~670 MB/day of continuous spectral data. The custom pipeline logs only compact summaries continuously, and saves full-resolution raw samples only around detected events (<1 GB/year).

**Architecture:**
```
RTL-SDR V3
  → pyrtlsdr @ 143.3 MHz center (extract 143.050 MHz bin digitally)
  → signal strength sampling (10–100ms intervals)
  → rolling baseline + threshold detection (3.0 dB)
  → SQLite event logging
```

**SQLite schema** (per event):
- `timestamp` — UTC detection time
- `duration_ms` — event length
- `peak_power_db` — peak signal strength
- `snr` — SNR above baseline at peak
- `integrated_power` — area under the event curve
- `raw_samples` — numpy array of power values around the event (pre + post window)

Raw samples are the key output — they feed the visualization pipeline.

### Phase 3 — Robustness

- `systemd` service with auto-restart
- USB watchdog (dongle can hang)
- Adaptive noise floor threshold (accounts for RFI changes over time)
- False positive filtering by event profile (meteors: fast rise, exponential decay)
- Log rotation

### Phase 4 — Visualization Pipeline

Each detected event generates a **unique static SVG** derived from its raw signal shape (the `raw_samples` array). These are collected and printed — not displayed live.

- No WebSocket or real-time interface needed
- Input: raw power sample array per event from SQLite
- Output: SVG file, one per event
- The signal's rise, peak, and exponential decay curve are the generative seed — each meteor is physically unique

Design of the SVG generation algorithm is TBD and is the creative core of the project.

## Out of Scope

- Phase 4 from original doc (DIY outdoor antenna) — dropped, placement not feasible
- Portable Raspberry Pi station — future idea, not planned
- ntfy alerting — set up on server but not a current priority
- Real-time web dashboard or WebSocket server
