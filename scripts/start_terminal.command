#!/bin/zsh
set -e

REPO_DIR="/Users/nishiokamahiro/Desktop/Antigravity/iMessage soujushin"
RUNTIME_DIR="$HOME/StateLens_Chat_runtime"
ENV_FILE="$REPO_DIR/.env"

# ==== Load .env ====
# .env があれば読み込む（APIキーなどの秘密情報は .env へ）
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

# ==== Defaults (if not set in .env) ====
export OPENAI_MODEL="${OPENAI_MODEL:-gpt-5-mini}"
export OPENAI_MODEL_LIGHT="${OPENAI_MODEL_LIGHT:-gpt-5-mini}"
export OPENAI_MODEL_HEAVY="${OPENAI_MODEL_HEAVY:-gpt-5.2}"
export OPENAI_TIMEOUT="${OPENAI_TIMEOUT:-45}"
export OPENAI_MAX_RETRIES="${OPENAI_MAX_RETRIES:-3}"
export SELF_DISPLAY_NAME="${SELF_DISPLAY_NAME:-西岡まひろ}"

mkdir -p "$RUNTIME_DIR/viewer"
cp "$REPO_DIR/imessage_relay.py" "$RUNTIME_DIR/imessage_relay.py"
cp "$REPO_DIR/viewer_server.py" "$RUNTIME_DIR/viewer_server.py"
cp "$REPO_DIR/Address_list.md" "$RUNTIME_DIR/Address_list.md"
cp "$REPO_DIR/viewer/index.html" "$RUNTIME_DIR/viewer/index.html"
cp "$REPO_DIR/viewer/app.js" "$RUNTIME_DIR/viewer/app.js"
cp "$REPO_DIR/viewer/style.css" "$RUNTIME_DIR/viewer/style.css"

echo "=== StateLens_Chat Relay ==="
echo "Starting viewer backend..."
/usr/bin/nohup /usr/bin/python3 "$HOME/StateLens_Chat_runtime/viewer_server.py" >> "$HOME/Library/Logs/imessage_relay_launcher.log" 2>&1 &
echo "Starting relay monitor..."
exec /usr/bin/python3 "$HOME/StateLens_Chat_runtime/imessage_relay.py"
