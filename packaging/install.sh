#!/usr/bin/env bash
# Install Izy for the current user: venv, systemd unit, shell extension.
# Idempotent — safe to re-run after pulling changes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/izy/venv"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "==> venv at $VENV"
mkdir -p "$(dirname "$VENV")"
if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.11 "$VENV" 2>/dev/null || uv venv "$VENV"
    uv pip install --python "$VENV/bin/python" -e "$REPO"
else
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -e "$REPO"
fi

echo "==> systemd user unit"
mkdir -p "$UNIT_DIR"
install -m 0644 "$REPO/packaging/izy.service" "$UNIT_DIR/izy.service"
systemctl --user daemon-reload
systemctl --user enable izy.service

echo "==> GNOME shell extension"
"$VENV/bin/izy" install-extension || true

cat <<'EOF'

Done. One more step, once:

  Log out and back in.

GNOME only scans for new shell extensions at startup and a Wayland session
cannot restart the shell in place, so the window-title source stays dark until
you do. After logging back in:

  izy doctor      # confirms titles are readable
  izy status      # what Izy is tracking
  izy day -v      # the day's log

The service starts automatically with the graphical session.
EOF
