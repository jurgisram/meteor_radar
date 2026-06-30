#!/usr/bin/env python3
"""
Meteor detection validator for rtl_power CSV output.
Run directly on the server: python3 validate_meteors.py /mnt/hdd/long_test.csv

What it does:
1. Reads the CSV in chunks (memory-safe for large files)
2. Skips the first 15 min (thermal warmup drift)
3. Computes rolling noise floor baseline
4. Flags samples where power at 143.050 MHz exceeds baseline + threshold
5. Groups nearby flags into events
6. Prints a summary + event list
7. Saves a small PNG of the power timeline with events marked
"""

import csv
import sys
import os
from datetime import datetime, timedelta
from collections import deque

INPUT_FILE = sys.argv[1] if len(sys.argv) > 1 else "/mnt/hdd/long_test.csv"
OUTPUT_DIR = os.path.dirname(INPUT_FILE)

# --- Config ---
WARMUP_SECONDS = 900          # skip first 15 min
BASELINE_WINDOW = 300         # rolling baseline over 5 min (seconds)
THRESHOLD_DB = 3.0            # dB above rolling baseline to flag
EVENT_GAP_SECONDS = 5         # merge flags within this gap into one event
TARGET_FREQ_MHZ = 143.050
# ---------------

print(f"Reading: {INPUT_FILE}")
print(f"Config: skip {WARMUP_SECONDS}s warmup, {BASELINE_WINDOW}s baseline window, {THRESHOLD_DB} dB threshold\n")

# Pass 1: stream through, extract power at target bin, detect events
times = []
powers = []
first_ts = None
target_bin = None
rows_read = 0
rows_used = 0

with open(INPUT_FILE, 'r') as f:
    reader = csv.reader(f)
    for row in reader:
        rows_read += 1
        ts = datetime.strptime(f"{row[0].strip()} {row[1].strip()}", "%Y-%m-%d %H:%M:%S")

        if first_ts is None:
            first_ts = ts
            # figure out which bin is 143.050 MHz
            freq_start = float(row[2])
            freq_end = float(row[3])
            n_bins = len(row) - 6
            bin_width = (freq_end - freq_start) / n_bins
            target_bin = int((TARGET_FREQ_MHZ * 1e6 - freq_start) / bin_width)
            target_bin = max(0, min(target_bin, n_bins - 1))
            actual_freq = (freq_start + target_bin * bin_width) / 1e6
            print(f"Bins per row: {n_bins}")
            print(f"Target bin: {target_bin} ({actual_freq:.4f} MHz)")

        elapsed = (ts - first_ts).total_seconds()
        if elapsed < WARMUP_SECONDS:
            continue

        power = float(row[6 + target_bin])
        times.append(elapsed)
        powers.append(power)
        rows_used += 1

print(f"\nRows read: {rows_read}")
print(f"Rows after warmup: {rows_used}")
print(f"Duration after warmup: {(times[-1] - times[0]) / 3600:.1f} hours")

# Compute rolling baseline and detect threshold crossings
baseline = deque(maxlen=BASELINE_WINDOW)
flags = []  # (time, power, baseline_val)

for i, (t, p) in enumerate(zip(times, powers)):
    if len(baseline) >= 30:  # need minimum samples
        bl_mean = sum(baseline) / len(baseline)
        if p > bl_mean + THRESHOLD_DB:
            flags.append((t, p, bl_mean))
    baseline.append(p)

print(f"\nThreshold crossings: {len(flags)}")

# Group into events
events = []
if flags:
    event_start = flags[0]
    event_peak = flags[0]
    event_end = flags[0]

    for flag in flags[1:]:
        if flag[0] - event_end[0] <= EVENT_GAP_SECONDS:
            # same event
            if flag[1] > event_peak[1]:
                event_peak = flag
            event_end = flag
        else:
            # new event
            events.append({
                'start': event_start[0],
                'end': event_end[0],
                'duration': event_end[0] - event_start[0],
                'peak_power': event_peak[1],
                'peak_time': event_peak[0],
                'baseline': event_peak[2],
                'snr': event_peak[1] - event_peak[2],
            })
            event_start = flag
            event_peak = flag
            event_end = flag

    # last event
    events.append({
        'start': event_start[0],
        'end': event_end[0],
        'duration': event_end[0] - event_start[0],
        'peak_power': event_peak[1],
        'peak_time': event_peak[0],
        'baseline': event_peak[2],
        'snr': event_peak[1] - event_peak[2],
    })

hours = (times[-1] - times[0]) / 3600
print(f"\nEvents detected: {len(events)}")
print(f"Rate: {len(events) / hours:.1f} per hour")
print(f"{'':=<70}")

for i, e in enumerate(events):
    t_abs = first_ts + timedelta(seconds=e['peak_time'])
    print(f"  Event {i+1:3d} | {t_abs.strftime('%H:%M:%S')} | "
          f"duration: {e['duration']:.1f}s | "
          f"peak: {e['peak_power']:.1f} dB | "
          f"baseline: {e['baseline']:.1f} dB | "
          f"SNR: {e['snr']:.1f} dB")

# Save minimal plot
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(16, 5))
    # downsample for plotting if too many points
    step = max(1, len(times) // 10000)
    ax.plot([times[i] / 3600 for i in range(0, len(times), step)],
            [powers[i] for i in range(0, len(powers), step)],
            linewidth=0.3, color='steelblue')

    for e in events:
        ax.axvline(x=e['peak_time'] / 3600, color='red', alpha=0.5, linewidth=0.8)

    ax.set_xlabel('Hours since start')
    ax.set_ylabel('Power (dB) at 143.050 MHz')
    ax.set_title(f'Meteor validation — {len(events)} events in {hours:.1f}h ({len(events)/hours:.1f}/hr)')
    plt.tight_layout()

    plot_path = os.path.join(OUTPUT_DIR, 'validation_result.png')
    plt.savefig(plot_path, dpi=150)
    print(f"\nPlot saved: {plot_path}")
except ImportError:
    print("\nmatplotlib not installed, skipping plot. Install with: pip3 install matplotlib")

# Save event log
log_path = os.path.join(OUTPUT_DIR, 'events.csv')
with open(log_path, 'w') as f:
    f.write("event,time_utc,start_s,end_s,duration_s,peak_power_db,baseline_db,snr_db\n")
    for i, e in enumerate(events):
        t_abs = first_ts + timedelta(seconds=e['peak_time'])
        f.write(f"{i+1},{t_abs.isoformat()},{e['start']:.1f},{e['end']:.1f},"
                f"{e['duration']:.1f},{e['peak_power']:.1f},{e['baseline']:.1f},{e['snr']:.1f}\n")
print(f"Event log saved: {log_path}")
