#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"
PID_FILE="$LOG_DIR/server.pid"
export OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
mkdir -p "$LOG_DIR"

find_running_pid() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && ps -p "$pid" -o args= 2>/dev/null | grep -Fq "$ROOT/server.py"; then
      echo "$pid"
      return 0
    fi
    rm -f "$PID_FILE"
  fi

  local pid
  pid="$(ps -eo pid=,args= | awk -v target="python3 $ROOT/server.py" '$0 ~ target {print $1; exit}')"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    echo "$pid"
    return 0
  fi
  return 1
}

if running_pid="$(find_running_pid)"; then
  echo "agent-monitor already running pid=$running_pid"
  exit 0
fi

nohup python3 "$ROOT/server.py" > "$LOG_DIR/server.out" 2>&1 &
echo $! > "$PID_FILE"
echo "started agent-monitor pid=$(cat "$PID_FILE")"
