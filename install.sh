#!/usr/bin/env bash
#
# install.sh — install the Telegram web mirror as a systemd service.
#
# Prompts for: port, @channel, fetch politeness, and how many posts to mirror.
# Defaults are VERY polite and mirror ALL posts. Installs dependencies, fetches
# the code from git, and enables a systemd service that runs on boot.
#
set -euo pipefail

# ---- the project's git repository (assumed to already exist) ----------------
REPO_URL="${REPO_URL:-https://github.com/GrakovNe/telegram-mirroring.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

# ---------------------------------------------------------------------------
die() { echo "Error: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "please run as root (sudo ./install.sh) — needed for systemd."

# The service should run as the invoking user, not root, when possible.
RUN_USER="${SUDO_USER:-root}"

echo "=== Telegram web mirror — installer ==="
echo

# ---- prompts ---------------------------------------------------------------
read -rp "HTTP port [8080]: " PORT
PORT="${PORT:-8080}"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "port must be a number."

while :; do
  read -rp "Telegram channel (e.g. @durov): " CHANNEL
  CHANNEL="${CHANNEL#@}"
  [ -n "$CHANNEL" ] && break
  echo "  channel is required."
done

echo
echo "Fetch politeness (how gently to pull from Telegram, to avoid a ban):"
echo "  1) very polite  — default, safest"
echo "  2) polite       — a bit faster"
echo "  3) fast         — least gentle, use with care"
read -rp "Choose [1]: " POLITE
case "${POLITE:-1}" in
  1|"") POLL=90; PGMIN=8;   PGMAX=15; MDMIN=1.0; MDMAX=2.5; BFPC=1 ;;
  2)    POLL=60; PGMIN=4;   PGMAX=8;  MDMIN=0.5; MDMAX=1.5; BFPC=2 ;;
  3)    POLL=45; PGMIN=1.5; PGMAX=3;  MDMIN=0.2; MDMAX=0.6; BFPC=3 ;;
  *)    die "invalid politeness choice." ;;
esac

echo
read -rp "How many posts to mirror? number or 'all' [all]: " POSTS
POSTS="${POSTS:-all}"
if [[ "$POSTS" == "all" ]]; then
  MAX_POSTS=0
elif [[ "$POSTS" =~ ^[0-9]+$ ]]; then
  MAX_POSTS="$POSTS"
else
  die "posts must be a number or 'all'."
fi

echo
echo "The mirror re-checks recently published posts for edits (Telegram lets"
echo "authors edit a post for about a day). Checks start 15 min after publishing"
echo "and double each time, so the request volume stays small."
read -rp "Enable edit re-checking? [Y/n]: " RECHECK
case "${RECHECK:-Y}" in
  [Yy]|"") RECHECK_ENABLED=1 ;;
  [Nn])    RECHECK_ENABLED=0 ;;
  *)       die "answer y or n." ;;
esac

echo
echo "Absolute public URL of this mirror (https://mirror.example.com)."
echo "Required for search engines: canonical links, Open Graph, sitemap.xml, RSS."
echo "Leave empty to serve pages without absolute links (no sitemap/RSS)."
read -rp "Public URL [none]: " PUBLIC_URL
PUBLIC_URL="${PUBLIC_URL%/}"
if [ -n "$PUBLIC_URL" ] && [[ "$PUBLIC_URL" != http* ]]; then
  die "public URL must start with http:// or https://."
fi

SERVICE="tgmirror-${CHANNEL}"
INSTALL_DIR="/opt/${SERVICE}"
UNIT="/etc/systemd/system/${SERVICE}.service"

echo
echo "About to install:"
echo "  channel      @${CHANNEL}"
echo "  port         ${PORT}"
echo "  politeness   poll=${POLL}s page=${PGMIN}-${PGMAX}s media=${MDMIN}-${MDMAX}s pages/cycle=${BFPC}"
echo "  posts        $([ "$MAX_POSTS" -eq 0 ] && echo all || echo "$MAX_POSTS")"
echo "  re-check     $([ "$RECHECK_ENABLED" -eq 1 ] && echo on || echo off)"
echo "  public URL   ${PUBLIC_URL:-none}"
echo "  directory    ${INSTALL_DIR}"
echo "  service      ${SERVICE} (runs as ${RUN_USER})"
read -rp "Proceed? [Y/n]: " OK
[[ "${OK:-Y}" =~ ^[Yy]$|^$ ]] || die "aborted."

# ---- dependencies ----------------------------------------------------------
echo "-> installing dependencies (python3, git)…"
if   command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3 git
elif command -v dnf     >/dev/null; then dnf install -y -q python3 git
elif command -v yum     >/dev/null; then yum install -y -q python3 git
elif command -v pacman  >/dev/null; then pacman -Sy --noconfirm python git
elif command -v zypper  >/dev/null; then zypper -q install -y python3 git
else die "no supported package manager found; install python3 and git manually."
fi
command -v python3 >/dev/null || die "python3 not available after install."

# ---- fetch code ------------------------------------------------------------
echo "-> fetching code from ${REPO_URL} (${REPO_BRANCH})…"
if [ -d "${INSTALL_DIR}/.git" ]; then
  git -C "${INSTALL_DIR}" fetch --depth 1 origin "${REPO_BRANCH}"
  git -C "${INSTALL_DIR}" reset --hard "origin/${REPO_BRANCH}"
else
  rm -rf "${INSTALL_DIR}"
  git clone --depth 1 --branch "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi
chown -R "${RUN_USER}:${RUN_USER}" "${INSTALL_DIR}"

# ---- systemd unit ----------------------------------------------------------
echo "-> writing systemd unit ${UNIT}…"
cat > "${UNIT}" <<EOF
[Unit]
Description=Telegram web mirror (@${CHANNEL})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=TG_CHANNEL=${CHANNEL}
Environment=PORT=${PORT}
Environment=POLL_INTERVAL=${POLL}
Environment=PAGE_DELAY_MIN=${PGMIN}
Environment=PAGE_DELAY_MAX=${PGMAX}
Environment=MEDIA_DELAY_MIN=${MDMIN}
Environment=MEDIA_DELAY_MAX=${MDMAX}
Environment=BACKFILL_PAGES_PER_CYCLE=${BFPC}
Environment=MAX_POSTS=${MAX_POSTS}
Environment=RECHECK_ENABLED=${RECHECK_ENABLED}
Environment=PUBLIC_URL=${PUBLIC_URL}
ExecStart=/usr/bin/env python3 ${INSTALL_DIR}/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "-> enabling and starting service…"
systemctl daemon-reload
systemctl enable --now "${SERVICE}" >/dev/null

sleep 2
echo
if systemctl is-active --quiet "${SERVICE}"; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "✅ Installed and running."
  echo "   URL:      http://${IP:-localhost}:${PORT}"
  echo "   Service:  systemctl status ${SERVICE}"
  echo "   Logs:     journalctl -u ${SERVICE} -f"
  echo "   Remove:   ./uninstall.sh   (channel: ${CHANNEL})"
else
  echo "⚠️  Service failed to start. Check: journalctl -u ${SERVICE} -e"
  exit 1
fi
