#!/bin/sh
# gc5000-rumble-linux installer
# Installs the FF proxy daemon, systemd unit and udev rule.
# Run as root:  sudo ./install.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root: sudo ./install.sh" >&2
    exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing daemon to /usr/local/bin/gc5000_ffd.py ..."
install -m 755 "$DIR/gc5000_ffd.py" /usr/local/bin/gc5000_ffd.py

echo "Installing systemd unit to /etc/systemd/system/gc5000-ffd.service ..."
install -m 644 "$DIR/gc5000-ffd.service" /etc/systemd/system/gc5000-ffd.service

echo "Installing udev rule to /etc/udev/rules.d/90-gc5000.rules ..."
install -m 644 "$DIR/90-gc5000.rules" /etc/udev/rules.d/90-gc5000.rules

systemctl daemon-reload
udevadm control --reload

echo
echo "Done. Connect (or power-cycle) your Nokia GC 5000 -- the service"
echo "starts automatically. Check with:"
echo "  systemctl status gc5000-ffd"
