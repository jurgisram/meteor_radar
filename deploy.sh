#!/usr/bin/env bash
# Meteor Radar — OptiPlex deployment script
# Run via SSH: bash <(curl -s https://raw.githubusercontent.com/jurgisram/meteor_radar/main/deploy.sh)
# Or after cloning: bash deploy.sh
set -euo pipefail

REPO_URL="https://github.com/jurgisram/meteor_radar"
REPO_DIR="/mnt/hdd/meteor_radar"
DATA_DIR="/mnt/hdd/meteor_radar"

echo "=== Meteor Radar Deployment ==="
echo ""

# --- 1. Clone or update repo ---
if [ -d "$REPO_DIR/.git" ]; then
    echo "[1/7] Updating repo..."
    git -C "$REPO_DIR" pull --ff-only
else
    echo "[1/7] Cloning repo..."
    git clone "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR"

# --- 2. System packages ---
echo ""
echo "[2/7] Checking system packages..."
MISSING=()
for pkg in python3 python3-pip cmake build-essential libusb-1.0-0-dev pkg-config; do
    dpkg -s "$pkg" &>/dev/null || MISSING+=("$pkg")
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  Installing: ${MISSING[*]}"
    sudo apt-get install -y "${MISSING[@]}"
else
    echo "  All system packages present."
fi

# Install RTL-SDR Blog V3 fork of librtlsdr (has rtlsdr_set_dithering; Ubuntu package lacks it)
if nm -D /lib/x86_64-linux-gnu/librtlsdr.so 2>/dev/null | grep -q rtlsdr_set_dithering; then
    echo "  librtlsdr (RTL-SDR Blog fork): already installed."
else
    echo "  Building librtlsdr from RTL-SDR Blog fork (Ubuntu package is missing rtlsdr_set_dithering)..."
    BUILD_DIR=$(mktemp -d)
    git clone --depth=1 https://github.com/rtlsdrblog/rtl-sdr-blog.git "$BUILD_DIR/rtl-sdr-blog"
    mkdir -p "$BUILD_DIR/rtl-sdr-blog/build"
    cmake -S "$BUILD_DIR/rtl-sdr-blog" -B "$BUILD_DIR/rtl-sdr-blog/build" \
        -DINSTALL_UDEV_RULES=ON -DCMAKE_BUILD_TYPE=Release -DDETACH_KERNEL_DRIVER=ON
    make -C "$BUILD_DIR/rtl-sdr-blog/build" -j"$(nproc)"
    sudo make -C "$BUILD_DIR/rtl-sdr-blog/build" install
    sudo ldconfig
    rm -rf "$BUILD_DIR"
    echo "  librtlsdr (RTL-SDR Blog fork): installed."
fi

# --- 3. Python dependencies ---
echo ""
echo "[3/7] Installing Python dependencies..."
# pyrtlsdr >= 0.3.0 calls rtlsdr_set_dithering at import time; that symbol
# doesn't exist in any released librtlsdr build. Pin to 0.2.93 which works.
pip3 install --quiet --break-system-packages 'pyrtlsdr==0.2.93' numpy

# --- 4. RTL-SDR device check ---
echo ""
echo "[4/7] Checking RTL-SDR device..."
if rtl_test -t 2>&1 | grep -q "Found 1 device"; then
    echo "  RTL-SDR found."
elif lsusb | grep -qi "realtek\|rtl28"; then
    echo "  USB device present but rtl_test failed — check udev rules or kernel module."
    echo "  Run: sudo rtl_test -t"
else
    echo "  WARNING: No RTL-SDR detected via USB. Plug in the dongle and re-run."
fi

# Verify kernel module is blacklisted
if grep -q "blacklist dvb_usb_rtl28xxu" /etc/modprobe.d/blacklist-rtlsdr.conf 2>/dev/null; then
    echo "  DVB kernel module blacklisted: OK"
else
    echo "  Setting up kernel module blacklist..."
    echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
    sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
fi

# --- 5. Data directory ---
echo ""
echo "[5/7] Checking data directory ($DATA_DIR)..."
if [ -d "$DATA_DIR" ] && [ -w "$DATA_DIR" ]; then
    echo "  $DATA_DIR is writable: OK"
    df -h "$DATA_DIR" | tail -1 | awk '{print "  Disk: "$4" free of "$2}'
else
    echo "  WARNING: $DATA_DIR is not writable or doesn't exist."
    echo "  Mount the HDD and ensure /mnt/hdd is writable before running daemon."
fi

# --- 6. Run env/DB check ---
echo ""
echo "[6/7] Running environment check..."
python3 - <<'PYEOF'
import sys, os
sys.path.insert(0, os.getcwd())
try:
    from src.db import init_db
    conn = init_db('/tmp/meteor_test.db')
    conn.close()
    os.remove('/tmp/meteor_test.db')
    print("  DB init: OK")
except SystemExit as e:
    print(f"  DB init FAILED (exit {e.code}) — check errors above")
    sys.exit(1)

try:
    import rtlsdr
    print("  pyrtlsdr import: OK")
except ImportError:
    print("  pyrtlsdr import: FAILED")
    sys.exit(1)

try:
    import numpy as np
    print(f"  numpy {np.__version__}: OK")
except ImportError:
    print("  numpy import: FAILED")
    sys.exit(1)
PYEOF

# --- 7. systemd service ---
echo ""
echo "[7/7] Installing meteor-radar systemd service..."
if ! grep -q '/mnt/hdd' /etc/fstab; then
    echo "  WARNING: /mnt/hdd not found in /etc/fstab — RequiresMountsFor= will have no effect."
    echo "  Add an fstab entry for the HDD to ensure the service waits for mount on boot."
fi
sudo cp "$REPO_DIR/scripts/meteor-radar.service" /etc/systemd/system/meteor-radar.service
sudo systemctl daemon-reload
sudo systemctl enable meteor-radar
sudo systemctl restart meteor-radar
if systemctl is-active --quiet meteor-radar; then
    echo "  meteor-radar.service: active"
else
    echo "  WARNING: meteor-radar.service is not active — check: journalctl -u meteor-radar -n 20"
fi

echo ""
echo "=== Deployment complete ==="
echo ""
echo "HITL validation steps:"
echo "  1. Quick acquisition test (10 rows of 40 floats):"
echo "       cd /mnt/hdd/meteor_radar && python3 -c \"from src.acquisition import Acquisition; a = Acquisition(); a.open_device(); [print(a.read_row()) for _ in range(10)]; a.close()\""
echo ""
echo "  2. Check systemd service:"
echo "       systemctl status meteor-radar"
echo "       journalctl -u meteor-radar -n 20"
echo ""
echo "  3. Check events after a run:"
echo "       sqlite3 /mnt/hdd/meteor_radar/meteor_radar.db 'SELECT timestamp, duration_ms, snr_db FROM events ORDER BY id DESC LIMIT 20;'"
echo ""
echo "  4. Tail the log:"
echo "       tail -f /mnt/hdd/meteor_radar/meteor_daemon.log"
