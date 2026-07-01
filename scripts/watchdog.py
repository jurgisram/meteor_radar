#!/usr/bin/env python3
"""Meteor radar health watchdog — runs every 5 minutes via systemd timer."""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

DB_PATH = '/mnt/hdd/meteor_radar/meteor_radar.db'
STATE_PATH = '/mnt/hdd/meteor_radar/meteor-watchdog-state.json'
STALE_THRESHOLD_MINUTES = 10
ALERT_SUPPRESS_MINUTES = 60
VILNIUS_TZ = ZoneInfo("Europe/Vilnius")


def _load_state(state_path: str) -> dict:
    if os.path.exists(state_path):
        with open(state_path) as f:
            return json.load(f)
    return {
        "alert_sent_at": None,
        "recovered": True,
        "last_summary_date": None,
    }


def _save_state(state_path: str, state: dict):
    with open(state_path, 'w') as f:
        json.dump(state, f)


def _send_discord(webhook_url: str, message: str):
    requests.post(webhook_url, json={"content": message})


def _get_last_alive(db_path: str):
    """Returns (last_alive_dt_utc_or_None, event_count_24h, event_count_total)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT last_alive FROM baseline_state ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        last_alive = None
        if row and row[0]:
            last_alive = datetime.fromisoformat(row[0])
            if last_alive.tzinfo is None:
                last_alive = last_alive.replace(tzinfo=timezone.utc)

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        count_24h = conn.execute(
            "SELECT COUNT(*) FROM events WHERE timestamp > ?", (cutoff,)
        ).fetchone()[0]

        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()

    return last_alive, count_24h, total


def main():
    webhook_url = os.environ.get('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    now_vilnius = now_utc.astimezone(VILNIUS_TZ)

    last_alive, count_24h, total = _get_last_alive(DB_PATH)
    state = _load_state(STATE_PATH)

    # NULL last_alive → daemon hasn't run yet, not an alert condition
    if last_alive is None:
        sys.exit(0)

    age_minutes = (now_utc - last_alive).total_seconds() / 60.0
    is_stale = age_minutes > STALE_THRESHOLD_MINUTES

    # --- Heartbeat staleness alert ---
    if is_stale:
        # Check if we should suppress (already alerted within 60 min)
        suppress = False
        if state.get("alert_sent_at"):
            last_alert = datetime.fromisoformat(state["alert_sent_at"])
            if last_alert.tzinfo is None:
                last_alert = last_alert.replace(tzinfo=timezone.utc)
            minutes_since_alert = (now_utc - last_alert).total_seconds() / 60.0
            if minutes_since_alert < ALERT_SUPPRESS_MINUTES:
                suppress = True

        if not suppress:
            msg = f"⚠️ meteor-radar heartbeat stale — last seen {int(age_minutes)} min ago"
            _send_discord(webhook_url, msg)
            state["alert_sent_at"] = now_utc.isoformat()
            state["recovered"] = False
    else:
        # Heartbeat is fresh
        if not state.get("recovered", True):
            # Previously alerted but not yet recovered
            _send_discord(webhook_url, "✅ meteor-radar recovered — heartbeat resumed")
            state["recovered"] = True
            state["alert_sent_at"] = None

    # --- Daily summary (09:xx Vilnius) ---
    today_str = now_vilnius.strftime("%Y-%m-%d")
    if now_vilnius.hour == 9 and state.get("last_summary_date") != today_str:
        age_int = int(age_minutes)
        msg = (
            f"\U0001f4ca Daily summary: {count_24h} events in 24h, "
            f"{total} all-time, heartbeat {age_int} min ago"
        )
        _send_discord(webhook_url, msg)
        state["last_summary_date"] = today_str

    _save_state(STATE_PATH, state)


if __name__ == '__main__':
    main()
