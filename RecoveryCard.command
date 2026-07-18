#!/bin/bash
# Recovery Card — one launcher for the whole thing.
#
# Double-click this file in Finder, or run it from a terminal:
#   ./RecoveryCard.command          start everything
#   ./RecoveryCard.command stop     stop everything
#   ./RecoveryCard.command status   see what is running
#
# Starting is safe to repeat: it clears anything stale first.

cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"
PORT="${PORT:-5001}"
IDLE="${IDLE_THRESHOLD:-60}"
LOGDIR="logs"
ACTION="${1:-start}"

say()  { printf "  %s\n" "$1"; }
bold() { printf "\n\033[1m%s\033[0m\n" "$1"; }

stop_all() {
  pkill -f "menubar.py"  2>/dev/null
  pkill -f "overlay.py"  2>/dev/null
  pkill -f "window.py"   2>/dev/null
  pkill -f "capture.py"  2>/dev/null
  pkill -f "app.py"      2>/dev/null
  sleep 1
}

status() {
  bold "Recovery Card — status"
  for p in menubar.py app.py capture.py; do
    if pgrep -f "$p" >/dev/null; then say "running    $p"; else say "stopped    $p"; fi
  done
  if curl -s -o /dev/null --max-time 2 "http://localhost:$PORT/"; then
    say "reachable  http://localhost:$PORT"
  else
    say "not up     http://localhost:$PORT"
  fi
}

case "$ACTION" in
  stop)
    bold "Stopping Recovery Card"
    stop_all
    say "all stopped."
    echo
    exit 0
    ;;
  status)
    status
    echo
    exit 0
    ;;
esac

bold "Starting Recovery Card"

if [ ! -x "$PY" ]; then
  say "The project's Python environment is missing."
  say "Run this once, then try again:"
  say "    python3 -m venv .venv && .venv/bin/pip install flask rumps pywebview"
  echo
  exit 1
fi

if ! curl -s -o /dev/null --max-time 3 http://localhost:11434/api/tags; then
  say "Ollama is not responding, so no cards can be generated."
  say "Open the Ollama app, or run this in another terminal:"
  say "    ollama serve"
  say "Starting anyway — the app will tell you when Ollama comes back."
  echo
fi

stop_all
mkdir -p "$LOGDIR"
echo "${PLITE_VIA:-terminal}" > "$LOGDIR/launch_path"

# Detached, so closing this window does not kill the app.
PORT="$PORT" IDLE_THRESHOLD="$IDLE" nohup "$PY" menubar.py \
  > "$LOGDIR/menubar.log" 2>&1 &

for _ in $(seq 1 25); do
  sleep 0.4
  curl -s -o /dev/null --max-time 2 "http://localhost:$PORT/" && break
done

if curl -s -o /dev/null --max-time 2 "http://localhost:$PORT/"; then
  say "Running. Opening the app window."
  say "Menu bar: look for the bone."
  say "Idle threshold: ${IDLE}s"

  # The native window IS the interface. Browser is emergency-only:
  # http://localhost:$PORT/engine
  nohup "$PY" window.py "http://localhost:$PORT" \
    > "$LOGDIR/window.log" 2>&1 &
  echo
  say "To stop:   ./RecoveryCard.command stop"
else
  say "It did not come up. Last few log lines:"
  echo
  tail -15 "$LOGDIR/menubar.log" 2>/dev/null | sed 's/^/    /'
fi

echo
