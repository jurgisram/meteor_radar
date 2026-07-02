#!/usr/bin/env python3
import sqlite3, sys

DB = "/mnt/hdd/meteor_radar/meteor_radar.db"
conn = sqlite3.connect(DB)

print("=== RFI vs Real ===")
for row in conn.execute("SELECT suspected_rfi, COUNT(*) FROM events GROUP BY suspected_rfi"):
    label = "suspected_rfi" if row[0] else "real"
    print(f"  {label}: {row[1]}")

print("\n=== Duration (real events only) ===")
for row in conn.execute("""
    SELECT CASE
        WHEN duration_ms < 200 THEN '<200ms'
        WHEN duration_ms < 1000 THEN '200ms-1s'
        WHEN duration_ms < 5000 THEN '1s-5s'
        WHEN duration_ms < 30000 THEN '5s-30s'
        ELSE '>30s' END AS b,
    COUNT(*) FROM events WHERE suspected_rfi=0
    GROUP BY b ORDER BY MIN(duration_ms)
"""):
    print(f"  {row[0]}: {row[1]}")

print("\n=== SNR distribution (real events, dB) ===")
for row in conn.execute("""
    SELECT CAST(snr_db AS INT) AS s, COUNT(*)
    FROM events WHERE suspected_rfi=0
    GROUP BY s ORDER BY s
"""):
    print(f"  {row[0]} dB: {row[1]}")

print("\n=== Hourly count (all events) ===")
for row in conn.execute("""
    SELECT strftime('%H', timestamp) AS h, COUNT(*)
    FROM events GROUP BY h ORDER BY h
"""):
    print(f"  {row[0]}h: {row[1]}")

print("\n=== Freq centroid (real events) ===")
for row in conn.execute("""
    SELECT CAST(frequency_centroid_hz/10)*10 AS c, COUNT(*)
    FROM events WHERE suspected_rfi=0
    GROUP BY c ORDER BY c
"""):
    print(f"  {row[0]:+.0f} Hz: {row[1]}")

print("\n=== Baseline at time of events ===")
for row in conn.execute("""
    SELECT MIN(baseline_mean_db), MAX(baseline_mean_db), AVG(baseline_mean_db)
    FROM events
"""):
    print(f"  mean_db min={row[0]:.1f} max={row[1]:.1f} avg={row[2]:.1f}")
