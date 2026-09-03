#!/usr/bin/env bash
# Open the choreography editor in a browser. One command, no arguments needed.
#
#   ./scripts/edit.sh                     # opens with the demo show
#   ./scripts/edit.sh shows/octet         # a directory: finds the .duckshow.json inside
#   ./scripts/edit.sh shows/octet/octet.duckshow.json
#   PORT=9000 ./scripts/edit.sh           # pick the port yourself
#
# Serves the repo root (Chrome and Safari refuse ES-module imports from
# file://, so a server is required) and shuts it down again on Ctrl+C.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# -- resolve an optional show argument to a path relative to the served root.
SHOW_PARAM=""
if [ $# -gt 0 ]; then
  target="$1"
  if [ -d "$target" ]; then
    found="$(find "$target" -maxdepth 1 -name '*.duckshow.json' | sort | head -1)"
    [ -n "$found" ] || { echo "no .duckshow.json inside $target"; exit 1; }
    target="$found"
  fi
  [ -f "$target" ] || { echo "no such show: $target"; exit 1; }
  # make it relative to the repo root, which is what the browser will request
  case "$target" in
    "$REPO"/*) target="${target#"$REPO"/}" ;;
    ./*)       target="${target#./}" ;;
  esac
  SHOW_PARAM="?show=/${target}"
fi

# -- find a free port rather than failing if 8000 is taken.
PORT="${PORT:-}"
if [ -z "$PORT" ]; then
  for candidate in 8000 8001 8002 8003 8080 8137; do
    if python3 -c "
import socket, sys
s = socket.socket()
try:
    s.bind(('127.0.0.1', int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
" "$candidate" 2>/dev/null; then
      PORT="$candidate"
      break
    fi
  done
fi
[ -n "$PORT" ] || { echo "no free port found in 8000-8003, 8080, 8137 — set PORT=…"; exit 1; }

URL="http://localhost:${PORT}/editor/duckshow-editor.html${SHOW_PARAM}"

SERVER_PID=""
cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  echo ""
  echo "stopped."
}
trap cleanup EXIT INT TERM

python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
SERVER_PID=$!

# wait for it to actually accept connections before opening a browser at it
for _ in $(seq 1 50); do
  if python3 -c "
import socket, sys
s = socket.socket(); s.settimeout(0.2)
try:
    s.connect(('127.0.0.1', int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
" "$PORT" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

echo "duckshow editor → $URL"
[ -n "$SHOW_PARAM" ] && echo "loading           ${SHOW_PARAM#?show=/}"
echo "Ctrl+C to stop."

if command -v open >/dev/null 2>&1; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1
else
  echo "(open that URL in a browser)"
fi

wait "$SERVER_PID"
