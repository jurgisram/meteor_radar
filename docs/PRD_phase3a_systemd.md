# PRD: Phase 3a — systemd Service

## Overview

Convert the Phase 2 daemon from a manually-started tmux session into a proper systemd service. This makes the detector a long-term collection system that starts on boot, restarts automatically on crash, and integrates with standard system logging.

---

## Goals

1. Daemon starts automatically on OptiPlex boot, no manual intervention
2. Automatic restart on crash or USB hang (with cooldown to give USB time to recover)
3. Logs go to journald — persisted across restarts, queryable by time range, no manual rotation needed
4. Deployment is idempotent: re-running `deploy.sh` installs or upgrades the service in place

## Non-Goals

- Health alerting (covered in Phase 3b)
- USB hang detection / adaptive recovery (Phase 3c, deferred)
- Multiple instance support

---

## Unit File Design

**File:** `/etc/systemd/system/meteor-radar.service`

```ini
[Unit]
Description=Meteor forward-scatter detection daemon
After=local-fs.target
RequiresMountsFor=/mnt/hdd

[Service]
Type=simple
User=jurgis
WorkingDirectory=/home/jurgis/meteor_radar
ExecStart=/usr/bin/python3 -m src.daemon
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=meteor-radar
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

**Key decisions:**

- `RequiresMountsFor=/mnt/hdd` — systemd will not start the service until the HDD is mounted. Prevents the daemon from crashing immediately on boot if the HDD hasn't come up yet.
- `RestartSec=30` — 30-second cooldown between restarts. Gives the RTL-SDR USB subsystem time to recover after a disconnect or SIGKILL. Too short (< 10s) risks a restart loop that hammers the USB device.
- `User=jurgis` — runs as the normal user, not root. The RTL-SDR Blog udev rules install group permissions allowing non-root USB access.
- `PYTHONUNBUFFERED=1` — ensures Python stdout/stderr flush immediately to journald rather than buffering.
- `StandardOutput=journal` — replaces the rotating log file in `daemon.py`. The `_setup_logging()` function currently writes to `/mnt/hdd/meteor_daemon.log` via `RotatingFileHandler`; this should be retained as a secondary sink (useful for `tail -f` without journalctl), but journald becomes the primary.

---

## Log Access

```bash
# Follow live
journalctl -u meteor-radar -f

# Last 100 lines
journalctl -u meteor-radar -n 100

# Since a specific time
journalctl -u meteor-radar --since "2026-07-01 08:00"

# Just errors
journalctl -u meteor-radar -p err
```

---

## Deployment Steps (added to `deploy.sh`)

1. Write unit file to `/etc/systemd/system/meteor-radar.service`
2. `systemctl daemon-reload`
3. `systemctl enable meteor-radar` — enable on boot
4. `systemctl restart meteor-radar` — start/restart immediately
5. `systemctl is-active meteor-radar` — verify it's running

Re-running deploy.sh should be safe: `systemctl restart` handles the in-place upgrade.

---

## HDD Mount Dependency

The HDD at `/mnt/hdd` is currently mounted manually or via `/etc/fstab`. For the `RequiresMountsFor` directive to work, `/mnt/hdd` must be in `/etc/fstab` with a proper mount entry. The deploy script should verify this and warn if the entry is missing.

If `/mnt/hdd` is not in fstab, `RequiresMountsFor` is a no-op and the service may start before the HDD is available. **`daemon.main()` must call `db.check_writable()` before `init_db()`** so an unmounted HDD raises immediately and lets systemd restart cleanly after 30s. Without this call, `init_db()` silently creates the SQLite DB on the root filesystem and the daemon appears healthy while writing to the wrong location.

This requires a small code change to `src/daemon.py`: add `db.check_writable()` (or inline the path check) as the first statement in `main()`, before `init_db()` is called.

---

## Testing

After deployment:

```bash
# Verify it's running
systemctl status meteor-radar

# Force a restart and watch it come back
sudo systemctl restart meteor-radar
journalctl -u meteor-radar -f

# Simulate a crash and watch auto-restart
sudo kill -9 $(systemctl show -p MainPID --value meteor-radar)
# Should see "Started meteor-radar" in journalctl within 30s

# Verify on-boot start (safe test — systemctl won't reboot the machine)
sudo systemctl disable meteor-radar && sudo systemctl enable meteor-radar
systemctl is-enabled meteor-radar  # should print "enabled"
```

---

## File Layout Changes

```
/etc/systemd/system/
└── meteor-radar.service     # NEW — unit file

~/meteor_radar/
└── scripts/
    └── install-service.sh   # NEW — extracted from deploy.sh for clarity
```

No changes to `src/` — the daemon code is unchanged.
