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

if [ -e "$DIR/91-gc5000-ffproxy.rules" ]; then
    echo "Installing udev rule to /etc/udev/rules.d/91-gc5000-ffproxy.rules ..."
    install -m 644 "$DIR/91-gc5000-ffproxy.rules" /etc/udev/rules.d/91-gc5000-ffproxy.rules
fi

if [ -e "$DIR/94-gc5000-realmode.rules" ]; then
    echo "Installing udev rule to /etc/udev/rules.d/94-gc5000-realmode.rules ..."
    install -m 644 "$DIR/94-gc5000-realmode.rules" /etc/udev/rules.d/94-gc5000-realmode.rules
fi

if [ -e "$DIR/68-gc5000-hide-realpad.rules" ]; then
    echo "Installing udev rule to /etc/udev/rules.d/68-gc5000-hide-realpad.rules ..."
    install -m 644 "$DIR/68-gc5000-hide-realpad.rules" /etc/udev/rules.d/68-gc5000-hide-realpad.rules
fi

systemctl daemon-reload
udevadm control --reload

echo
echo "Done. Connect (or power-cycle) your Nokia GC 5000 -- the service"
echo "starts automatically. Check with:"
echo "  systemctl status gc5000-ffd"
