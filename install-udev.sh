#!/usr/bin/env bash
# Installs udev rules to give access to the Stream Deck.
# Note: uses GROUP="users" (not uaccess) so it works without a logind seat.
# Usage:  ./install-udev.sh   (requires sudo password)
set -euo pipefail

RULE="/etc/udev/rules.d/40-streamdeck.rules"
GROUP="users"   # device access group (pier is a member)

if [ "$(id -u)" -ne 0 ]; then
    echo "Re-running with sudo..."
    exec sudo bash "$0"
fi

cat > "$RULE" <<EOF
# Elgato Stream Deck (all models, vid 0fd9) — access via group $GROUP
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", GROUP="$GROUP", MODE="0660"
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", GROUP="$GROUP", MODE="0660"
EOF

# CPU power (RAPL): make the energy counter readable by the user
# (read-only access to a counter; no extra write privileges)
RAPL="/sys/class/powercap/intel-rapl:0/energy_uj"
if [ -f "$RAPL" ]; then
    cat > /etc/udev/rules.d/99-powercap-read.rules <<EOF
# RAPL energy counter readable by all (read-only)
KERNEL=="intel-rapl:0", SUBSYSTEM=="powercap", RUN+="/bin/chmod 444 /sys/class/powercap/intel-rapl:0/energy_uj"
EOF
    chmod 444 "$RAPL" 2>/dev/null || true
fi

udevadm control --reload-rules
udevadm trigger

echo "Rules installed in $RULE (group: $GROUP)"
echo "Check with:  ls -l /dev/bus/usb/0*/00* | grep 0fd9"
echo "If the deck is still not accessible, unplug and replug it."
