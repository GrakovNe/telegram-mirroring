#!/usr/bin/env bash
#
# uninstall.sh — remove a Telegram web mirror service installed by install.sh.
#
# Stops and disables the systemd service and removes its unit. Prompts before
# deleting the code + mirrored data directory.
#
set -euo pipefail

die() { echo "Error: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "please run as root (sudo ./uninstall.sh)."

# Which channel's mirror to remove (matches install.sh's SERVICE naming).
CHANNEL="${1:-}"
if [ -z "$CHANNEL" ]; then
  read -rp "Telegram channel to uninstall (e.g. @durov): " CHANNEL
fi
CHANNEL="${CHANNEL#@}"
[ -n "$CHANNEL" ] || die "channel is required."

SERVICE="tgmirror-${CHANNEL}"
INSTALL_DIR="/opt/${SERVICE}"
UNIT="/etc/systemd/system/${SERVICE}.service"

if [ ! -e "$UNIT" ] && [ ! -d "$INSTALL_DIR" ]; then
  die "nothing installed for @${CHANNEL} (no ${UNIT} or ${INSTALL_DIR})."
fi

echo "-> stopping and disabling ${SERVICE}…"
systemctl disable --now "${SERVICE}" 2>/dev/null || true

if [ -e "$UNIT" ]; then
  echo "-> removing unit ${UNIT}…"
  rm -f "${UNIT}"
  systemctl daemon-reload
  systemctl reset-failed "${SERVICE}" 2>/dev/null || true
fi

if [ -d "$INSTALL_DIR" ]; then
  read -rp "Delete code + mirrored data at ${INSTALL_DIR}? [y/N]: " DEL
  if [[ "${DEL:-N}" =~ ^[Yy]$ ]]; then
    rm -rf "${INSTALL_DIR}"
    echo "-> removed ${INSTALL_DIR}"
  else
    echo "-> kept ${INSTALL_DIR} (data preserved)"
  fi
fi

echo "✅ Uninstalled @${CHANNEL}."
