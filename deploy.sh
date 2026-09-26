#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — deploy the MqttChat client to its web host.
#
# The page, its script and layout, and what makes it installable:
#   mqttchat.html           – the launcher (loads the AllSpeak CDN bundle and MQTT.js)
#   mqttchat-main.as        – the client script (fetched by the launcher)
#   mqttchat.json           – Webson screen layout
#   mqttchat.webmanifest,
#   mqttchat-sw.js, icons   – what makes it installable as an app
#
# --infra adds .htaccess (one AddType, so the manifest is served as JSON) and credentials.php.
#
# It only copies/pushes; it never deletes.
#
# Target resolution, in order of precedence:
#   1. a command-line target:
#        ./deploy.sh /path/to/web/root            # local directory (cp)
#        ./deploy.sh user@host:/path/to/root      # remote host (rsync)
#   2. the MQTTCHAT_DEPLOY_DIR env var (local directory)
#   3. deploy.conf (copy deploy.conf.example and fill in):
#        rsync to $DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH
#
# Options:
#   --infra        also copy .htaccess and credentials.php
#   -h | --help    show this help
#
# Environment:
#   DEPLOY_DRY_RUN=1   print the rsync command without running it
#   DEPLOY_PORT=<n>    ssh port (from deploy.conf or environment)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

INFRA=0
case "${1:-}" in
  -h|--help|help)
    sed -n '2,32p' "$0"
    exit 0
    ;;
  --infra)
    INFRA=1
    shift
    ;;
esac

# Optional per-machine connection details (never committed).
if [[ -f deploy.conf ]]; then
  # shellcheck disable=SC1091
  source deploy.conf
fi

TARGET="${1:-}"
if [[ -z "$TARGET" && -n "${MQTTCHAT_DEPLOY_DIR:-}" ]]; then
  TARGET="$MQTTCHAT_DEPLOY_DIR"
fi
if [[ -z "$TARGET" ]]; then
  if [[ -n "${DEPLOY_USER:-}" && -n "${DEPLOY_HOST:-}" && -n "${DEPLOY_PATH:-}" ]]; then
    TARGET="$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH"
  else
    echo "No target given and no connection details configured." >&2
    echo "Usage: $0 [--infra] TARGET_DIR | user@host:/path" >&2
    echo "  or copy deploy.conf.example to deploy.conf and set DEPLOY_USER/DEPLOY_HOST/DEPLOY_PATH." >&2
    exit 2
  fi
fi

CLIENT_FILES=(mqttchat.html mqttchat-main.as mqttchat.json \
  mqttchat.webmanifest mqttchat-sw.js \
  icon-192.png icon-512.png icon-maskable-512.png apple-touch-icon.png favicon.png)
INFRA_FILES=(.htaccess credentials.php)

FILES=("${CLIENT_FILES[@]}")
if (( INFRA )); then
  FILES+=("${INFRA_FILES[@]}")
fi
for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || { echo "error: missing $f in repo" >&2; exit 1; }
done

# --- local target: plain copy -------------------------------------------
if [[ "$TARGET" != *:* ]]; then
  if [[ ! -d "$TARGET" ]]; then
    echo "error: target directory not found: $TARGET" >&2
    exit 2
  fi
  echo "Deploying MqttChat client to $TARGET"
  for f in "${FILES[@]}"; do
    cp -v "$f" "$TARGET/$f"
  done
else
  # --- remote target: rsync over ssh -------------------------------------
  REMOTE="$TARGET"
  [[ "$REMOTE" == */ ]] || REMOTE="$REMOTE/"
  SSH_CMD=(ssh)
  if [[ -n "${DEPLOY_PORT:-}" ]]; then
    SSH_CMD=(ssh -p "$DEPLOY_PORT")
  fi
  RSYNC_CMD=(rsync -az -e "${SSH_CMD[*]}" "${FILES[@]}" "$REMOTE")
  if [[ -n "${DEPLOY_DRY_RUN:-}" ]]; then
    echo "Would run: ${RSYNC_CMD[*]}"
    exit 0
  fi
  echo "Deploying MqttChat client to $REMOTE"
  "${RSYNC_CMD[@]}"
fi

cat <<'EOF'

Done.

Reminders:
- credentials.php reads ../<host>.txt — for chat.example.com that is
  /home/you/chat.example.com.txt, one level ABOVE the web root. It holds the
  page's MQTT login (see credentials-local.example) and nothing else.
- The service picks up a changed mqttchat-server.as / as_chat.py only on restart:
  systemctl --user restart mqttchat   (on the PC)
- Browsers may cache mqttchat.html; hard-refresh (Ctrl+Shift+R) to test immediately.
EOF
