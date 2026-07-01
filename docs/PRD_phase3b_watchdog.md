# PRD: Phase 3b — Health Watchdog

## Overview

A lightweight monitoring agent that runs on the OptiPlex and reports daemon health to Discord. It uses the `last_alive` heartbeat already written to SQLite by the Phase 2 daemon every 30 seconds, plus event counts from the `events` table.

This is a separate concern from the systemd service (Phase 3a) — systemd handles restarts, the watchdog handles visibility.

---

## Goals

1. Alert to Discord within 10 minutes if the daemon stops updating its heartbeat
2. Daily summary at 09:00 local time: events detected in last 24h, service uptime, last heartbeat age
3. Runs as a systemd timer (not a cron job) — consistent with Phase 3a, visible in `systemctl list-timers`
4. No external dependencies beyond SQLite and an HTTP POST to a Discord webhook

## Non-Goals

- SMS/email/ntfy alerting (Discord webhook is sufficient)
- Per-event notifications (too noisy; daily summary is enough)
- Prometheus/Grafana metrics export
- Alerting on detection rate anomalies (deferred — needs baseline history)

---

## Architecture

```
systemd timer (every 5 min)
  └─ scripts/watchdog.py
       ├─ open /mnt/hdd/meteor_radar.db
       ├─ read last_alive from baseline_state
       ├─ if stale > ALERT_THRESHOLD_MIN → POST Discord alert
       ├─ if daily_summary_due (09:00 window) → POST Discord summary
       └─ exit 0
```

No long-running process. The timer fires a short-lived Python script every 5 minutes.

---

## Heartbeat Staleness Check

```python
ALERT_THRESHOLD_MINUTES = 10

last_alive = read_last_alive_from_db()  # UTC ISO8601 string
age_minutes = (datetime.now(UTC) - datetime.fromisoformat(last_alive)).total_seconds() / 60

if age_minutes > ALERT_THRESHOLD_MINUTES:
    post_discord_alert(age_minutes)
```

The 10-minute threshold allows for:
- One full systemd restart cycle (30s cooldown + ~15s startup + 900s warmup skipped on resume)
- Wait — on resume, the daemon loads saved baseline state and skips warmup. Startup to first heartbeat should be < 60s.
- 10 minutes therefore catches: daemon crashed AND systemd failed to restart (e.g. device not found loop)

---

## Alert Message Format

**Staleness alert:**
```
⚠️ meteor-radar heartbeat stale — last seen 14 min ago
Service may be down. Check: journalctl -u meteor-radar -n 50
```

**Recovery notice** (sent once when heartbeat resumes after an alert):
```
✅ meteor-radar recovered — heartbeat resumed
```

**Daily summary (09:00):**
```
📡 Meteor radar daily summary — 2026-07-01
Events (last 24h): 12 (3 suspected RFI filtered)
Total events all-time: 847
Last heartbeat: 2 min ago
Service uptime: 23h 47m
```

---

## Discord Webhook

Discord supports incoming webhooks without a bot token — just an HTTP POST to a webhook URL.

**Setup (one-time, manual):**
1. In Discord: channel settings → Integrations → Webhooks → New Webhook
2. Copy the webhook URL
3. Store in `/etc/meteor-radar-watchdog.env` (chmod 600, owned by jurgis):
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```

The watchdog script reads this env file at startup. The URL is never stored in the repo.

---

## Deduplication / Alert Suppression

The watchdog fires every 5 minutes. Without suppression, a stuck daemon would flood Discord with alerts.

**Implementation:** State file at `/tmp/meteor-watchdog-state.json`
```json
{
  "alert_sent_at": "2026-07-01T08:30:00Z",
  "recovered": false
}
```

- Send alert only if: no alert sent in last 60 minutes, OR this is a new outage (heartbeat was fresh in the previous check)
- Send recovery notice only once per outage

---

## systemd Timer Unit Files

**`/etc/systemd/system/meteor-watchdog.service`:**
```ini
[Unit]
Description=Meteor radar health watchdog
After=meteor-radar.service

[Service]
Type=oneshot
User=jurgis
EnvironmentFile=/etc/meteor-radar-watchdog.env
ExecStart=/usr/bin/python3 /home/jurgis/meteor_radar/scripts/watchdog.py
StandardOutput=journal
StandardError=journal
SyslogIdentifier=meteor-watchdog
```

**`/etc/systemd/system/meteor-watchdog.timer`:**
```ini
[Unit]
Description=Run meteor radar watchdog every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=meteor-watchdog.service

[Install]
WantedBy=timers.target
```

`OnBootSec=2min` — delay first run by 2 minutes on boot to give the daemon time to start and write its first heartbeat.

---

## Daily Summary Timing

The watchdog checks at each 5-minute fire whether a daily summary is due:

```python
now = datetime.now(local_tz)
summary_due = (
    now.hour == 9 and
    now.minute < 5 and  # within the first 5-min window of 09:00
    last_summary_date != now.date()
)
```

State file tracks `last_summary_date` to prevent duplicate summaries.

---

## Implementation Checklist

- [ ] `scripts/watchdog.py` — main script
- [ ] `/etc/systemd/system/meteor-watchdog.service`
- [ ] `/etc/systemd/system/meteor-watchdog.timer`
- [ ] `deploy.sh` additions:
  - Prompt for Discord webhook URL if `/etc/meteor-radar-watchdog.env` is missing
  - Install + enable timer
- [ ] Manual step (documented in deploy output): create Discord webhook in the target channel

---

## File Layout

```
~/meteor_radar/
└── scripts/
    └── watchdog.py          # NEW — watchdog script

/etc/systemd/system/
├── meteor-watchdog.service  # NEW
└── meteor-watchdog.timer    # NEW

/etc/
└── meteor-radar-watchdog.env  # NEW — webhook URL (manual, not in repo)
```
