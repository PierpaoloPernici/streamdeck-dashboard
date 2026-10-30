#!/usr/bin/env bash
# Installs deck-stats as a systemd *user* service and enables lingering for
# the user, so the service survives SSH logout (see note below).
#
# The repository can live anywhere: the unit is generated from
# deck-stats.service with the current checkout path substituted in, so no
# hardcoded path ends up in the repo.
#
# Usage:
#   ./install-service.sh            install / refresh the unit and enable it
#   ./install-service.sh --remove   stop and remove the unit
set -euo pipefail

UNIT_NAME="deck-stats.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$UNIT_NAME"
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$REPO/.venv/bin/python"

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Removed $UNIT (linger left enabled; disable with: sudo loginctl disable-linger $USER)"
    exit 0
fi

if [ ! -x "$PYTHON" ]; then
    echo "error: $PYTHON not found." >&2
    echo "Create the virtualenv first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"
sed -e "s|@REPO@|$REPO|g" -e "s|@PYTHON@|$PYTHON|g" \
    "$REPO/deck-stats.service" > "$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"

# A *user* service is torn down by logind as soon as the last login session
# of the user ends (SSH logout), because logind then stops user@UID.service.
# Lingering keeps the user manager — and therefore the dashboard — alive with
# no session open, and restarts it at boot on a headless machine.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo "Enabling linger for $USER (needs sudo)..."
    sudo loginctl enable-linger "$USER"
fi
echo "Linger: $(loginctl show-user "$USER" -p Linger --value)"

echo "Installed $UNIT"
systemctl --user --no-pager status "$UNIT_NAME" | head -5
