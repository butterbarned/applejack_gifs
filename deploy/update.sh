#!/usr/bin/env bash
# Pull the newest code from GitHub, check it, restart the page, and roll back if anything is wrong.
# Run by asset-gui-update.timer (as root), or by hand:  sudo /srv/asset-tools/app/deploy/update.sh
set -euo pipefail

BASE=${BASE:-/srv/asset-tools}
APP=$BASE/app
BRANCH=${BRANCH:-main}
RUN_AS=${RUN_AS:-assettools}
SERVICE=${SERVICE:-asset-gui}
PORT=${PORT:-8765}
SYSTEMCTL=${SYSTEMCTL:-systemctl}          # (overridden only by the tests)
LOCK=${LOCK:-/run/asset-gui-update.lock}

exec 9>"$LOCK"
flock -n 9 || { echo "another update is running"; exit 0; }

if [ "$(id -u)" -eq 0 ] && [ -n "$RUN_AS" ]; then
  git_() { runuser -u "$RUN_AS" -- git -C "$APP" "$@"; }   # the clone belongs to the service user, not root
else
  git_() { git -C "$APP" "$@"; }
fi

git_ fetch --quiet origin "$BRANCH"
OLD=$(git_ rev-parse HEAD)
NEW=$(git_ rev-parse "origin/$BRANCH")
if [ "$OLD" = "$NEW" ]; then
  exit 0   # nothing new
fi
BAD=$BASE/rejected-commit
if [ -f "$BAD" ] && [ "$(cat "$BAD")" = "$NEW" ]; then
  exit 0   # this commit already failed here; wait for a newer one instead of retrying it every few minutes
fi
echo "updating ${OLD:0:8} -> ${NEW:0:8}"

RESTARTED=0
rollback() {
  echo "ROLLING BACK to ${OLD:0:8}: $1" >&2
  echo "$NEW" > "$BAD"
  git_ reset --hard --quiet "$OLD"
  if [ "$RESTARTED" = 1 ]; then $SYSTEMCTL restart "$SERVICE" || true; fi   # only if we had restarted it onto the bad code
  exit 1
}

git_ reset --hard --quiet "$NEW"

# 1. every script must at least parse
(cd "$APP" && python3 - <<'PY'
import ast, glob
for f in glob.glob("*.py"):
    ast.parse(open(f, encoding="utf-8").read(), f)
PY
) || rollback "a script has a syntax error"

# 2. restart, then wait for the page to answer
$SYSTEMCTL restart "$SERVICE"
RESTARTED=1
for _ in $(seq 1 20); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "ok: now running ${NEW:0:8}"
    rm -f "$BAD"
    exit 0
  fi
  sleep 1
done
rollback "the page did not come back"
