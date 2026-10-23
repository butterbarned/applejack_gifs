#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu/Debian server (run as root). Safe to run again: it updates the units
# and leaves your code, cache and Caddy configuration alone.
#
#   sudo ./install.sh gif.example.com https://github.com/YOU/asset-tools.git [branch]
#   sudo CORS_ORIGIN=https://YOU.github.io ./install.sh api.example.com https://github.com/YOU/asset-tools.git   (page on GitHub Pages)
#
# It creates the unprivileged user, clones the code, makes a Python environment (with Pillow), installs the
# page as a systemd service and the 5-minute update timer. Caddy (HTTPS) and the game files are separate
# steps: see DEPLOY.md.
set -euo pipefail

DOMAIN=${1:?usage: install.sh YOUR.DOMAIN [REPO_URL] [BRANCH]}
REPO=${2:-}
BRANCH=${3:-main}
BASE=/srv/asset-tools
APP=$BASE/app
CACHE=$BASE/cache
USER_=assettools
PORT=8765
CORS_ORIGIN=${CORS_ORIGIN:-}   # e.g. https://YOU.github.io: the site whose page may call this server from the browser

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }
case "$CORS_ORIGIN" in ""|https://[a-zA-Z0-9.-]*|http://localhost:[0-9]*|http://127.0.0.1:[0-9]*) ;; *) echo "CORS_ORIGIN must look like https://YOU.github.io (no path, no trailing slash)" >&2; exit 1;; esac
case "$DOMAIN" in *[!a-zA-Z0-9.-]*|"") echo "that does not look like a domain name: $DOMAIN" >&2; exit 1;; esac

echo "== packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv curl >/dev/null

echo "== user and folders"
id "$USER_" >/dev/null 2>&1 || useradd --system --home-dir "$BASE" --create-home --shell /usr/sbin/nologin "$USER_"
mkdir -p "$CACHE"
chown -R "$USER_:$USER_" "$BASE"

echo "== code"
if [ ! -d "$APP/.git" ]; then
  [ -n "$REPO" ] || { echo "first run: give the repository address as the second argument" >&2; exit 1; }
  runuser -u "$USER_" -- git clone --branch "$BRANCH" "$REPO" "$APP"
fi

echo "== python environment (Pillow gives better GIFs; the page works without it)"
runuser -u "$USER_" -- python3 -m venv "$BASE/venv"
runuser -u "$USER_" -- "$BASE/venv/bin/pip" install -q --upgrade pip pillow \
  || echo "   (Pillow could not be installed; the built-in GIF encoder will be used)"

echo "== services"
CORS_FLAG=""; [ -n "$CORS_ORIGIN" ] && CORS_FLAG="--cors-origin $CORS_ORIGIN"
sed -e "s/gif\.example\.com/$DOMAIN/g" -e "s/__PORT__/$PORT/g" -e "s#__CORS__#$CORS_FLAG#g" "$APP/deploy/asset-gui.service" > /etc/systemd/system/asset-gui.service
cp "$APP/deploy/asset-gui-update.service" "$APP/deploy/asset-gui-update.timer" /etc/systemd/system/
chmod +x "$APP/deploy/update.sh"
systemctl daemon-reload
systemctl enable asset-gui.service asset-gui-update.timer
systemctl restart asset-gui.service      # also when it was already running, so a changed domain or setting takes effect
systemctl start asset-gui-update.timer

cat <<MSG

Installed. Next:
  1. game files:   sudo -u $USER_ $BASE/venv/bin/python $APP/mirror_assets.py --types defs,avatars --avatar 8 --out $CACHE
                   (a small start; see DEPLOY.md for everything else), then: sudo systemctl restart asset-gui
  2. Caddy (HTTPS): use $APP/deploy/Caddyfile.example with your domain
  3. check:        curl -fsS http://127.0.0.1:$PORT/healthz   and   journalctl -u asset-gui -n 20
MSG
