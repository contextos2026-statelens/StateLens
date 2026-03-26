#!/bin/zsh
set -u

CHAT_DB="$HOME/Library/Messages/chat.db"
LOG_FILE="$HOME/Library/Logs/imessage_relay.log"

echo "=== StateLens Relay Access Diagnose ==="
echo "Time: $(date '+%Y-%m-%d %H:%M:%S %z')"
echo

echo "[1] Relay / Viewer process"
/bin/ps -ax -o pid,ppid,user,command | /usr/bin/grep -E 'imessage_relay.py|viewer_server.py|Python\.app/Contents/MacOS/Python' | /usr/bin/grep -v grep || echo "No relay/viewer process found"
echo

echo "[2] chat.db existence"
if [[ -e "$CHAT_DB" ]]; then
  /bin/ls -l "$CHAT_DB"
else
  echo "chat.db not found: $CHAT_DB"
fi
echo

echo "[3] python read test for chat.db"
/usr/bin/python3 - <<'PY'
import os
import sqlite3
db = os.path.expanduser("~/Library/Messages/chat.db")
print("DB:", db)
try:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM message")
    n = cur.fetchone()[0]
    conn.close()
    print("OK: message count =", n)
except Exception as e:
    print("ERROR:", repr(e))
PY
echo

echo "[4] relay log tail"
if [[ -f "$LOG_FILE" ]]; then
  /usr/bin/tail -n 40 "$LOG_FILE"
else
  echo "relay log not found: $LOG_FILE"
fi
echo

echo "[5] tccd recent entries (last 5m, filtered)"
/usr/bin/log show --style compact --last 5m --predicate 'process == "tccd"' 2>/dev/null | /usr/bin/grep -E 'Messages|chat\.db|SystemPolicyAllFiles|Python|Terminal|osascript|applet' | /usr/bin/tail -n 80 || true

