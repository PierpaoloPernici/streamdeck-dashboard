#!/usr/bin/env bash
# Installs udev rules that give the current user access to the Stream Deck
# and make the CPU RAPL energy counter readable (used for CPU power).
#
# Note: uses GROUP="users" (not uaccess) so it works without a logind seat.
# Usage:  ./install-udev.sh   (requires sudo password)
set -euo pipefail

RULE="/etc/udev/rules.d/40-streamdeck.rules"
RAPL_RULE="/etc/udev/rules.d/99-powercap-read.rules"
GROUP="users"   # device access group (add your user to it if different)

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
# (read-only access to a counter; no extra write privileges).
# Domains are discovered, not hardcoded: the package id differs per CPU.
: > "$RAPL_RULE"
for dir in /sys/class/powercap/intel-rapl:*; do
    [ -e "$dir/energy_uj" ] || continue
    name="$(cat "$dir/name" 2>/dev/null || true)"
    case "$name" in
        package*) ;;
        *) continue ;;            # skip sub-domains (core, uncore, dram...)
    esac
    kernel="$(basename "$dir")"
    cat >> "$RAPL_RULE" <<EOF
# $name — readable by all (read-only)
KERNEL=="$kernel", SUBSYSTEM=="powercap", RUN+="/bin/chmod 444 $dir/energy_uj"
EOF
    chmod 444 "$dir/energy_uj" 2>/dev/null || true
    echo "RAPL: $dir/energy_uj readable"
done

udevadm control --reload-rules
udevadm trigger

echo "Rules installed: $RULE, $RAPL_RULE (group: $GROUP)"
echo "Check with:  ls -l /dev/bus/usb/0*/00* | grep 0fd9"
echo "If the deck is still not accessible, unplug and replug it."
