#!/usr/bin/env bash
# Open the choreography editor in a browser. One command, no arguments needed.
#
#   ./scripts/edit.sh                     # opens with the demo show
#   ./scripts/edit.sh shows/octet         # a directory: finds the .duckshow.json inside
#   ./scripts/edit.sh shows/octet/octet.duckshow.json
#   ./scripts/edit.sh --setlist           # same page, setlist view in front (docs/setlist-format.md)
#   ./scripts/edit.sh shows/setlists/example.duckset.json
#   PORT=9000 ./scripts/edit.sh           # pick the port yourself
#
# Every form opens editor/index.html, a shell holding the show editor and the
# setlist editor as two frames with a tab bar; switching between them keeps
# both documents in memory. The two pages are still reachable on their own at
# editor/duckshow-editor.html and editor/setlist.html.
#
# Serves the repo root (Chrome and Safari refuse ES-module imports from
# file://, so a server is required) and shuts it down again on Ctrl+C.
#
# Serving is scripts/editor_server.py (docs/viewer.md "Create Preview") --
# a stdlib-only drop-in for `python3 -m http.server` that additionally
# lets the editor's Create Preview button run tools/bake itself, bound to
# 127.0.0.1 only. If it fails to start for any reason, this script falls
# straight back to plain `python3 -m http.server`, so the editor still
# works — just without the Create Preview button, which the page itself
# will explain (GET /api/capabilities is unreachable, same as file://).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# -- classify the arguments. Every form opens editor/index.html: a show path
# fills the show frame, a .duckset.json fills the setlist frame, --setlist
# brings the setlist view forward. A second value of the same kind, or
# anything else, is an error rather than something silently dropped.
PAGE="editor/index.html"
VIEW=""
SHOW_PARAM=""
SET_PARAM=""

# Strip the checkout prefix so the browser asks for a repo-root-relative path,
# then refuse anything still absolute or climbing out: the page fetches this
# verbatim, and a path outside the checkout would turn into a URL for another
# host.
relativize() {
  local t="$1"
  case "$t" in
    "$REPO"/*) t="${t#"$REPO"/}" ;;
    ./*)       t="${t#./}" ;;
  esac
  case "$t" in
    /*|*..*) echo "must be inside the checkout: $1" >&2; return 1 ;;
  esac
  printf '%s' "$t"
}

for arg in "$@"; do
  case "$arg" in
    --setlist)
      VIEW="setlist" ;;
    *.duckset.json)
      [ -z "$SET_PARAM" ] || { echo "only one setlist, got a second: $arg"; exit 1; }
      [ -f "$arg" ] || { echo "no such setlist: $arg"; exit 1; }
      rel="$(relativize "$arg")" || exit 1
      SET_PARAM="/${rel}"
      # A setlist file brings its view forward even without --setlist, so
      # tab-completing a .duckset.json does the obvious thing.
      VIEW="setlist" ;;
    *)
      [ -z "$SHOW_PARAM" ] || { echo "only one show, got a second: $arg"; exit 1; }
      target="$arg"
      if [ -d "$target" ]; then
        found="$(find "$target" -maxdepth 1 -name '*.duckshow.json' | sort | head -1)"
        [ -n "$found" ] || { echo "no .duckshow.json inside $target"; exit 1; }
        target="$found"
      fi
      [ -f "$target" ] || { echo "no such show: $target"; exit 1; }
      rel="$(relativize "$target")" || exit 1
      SHOW_PARAM="/${rel}" ;;
  esac
done

# Percent-encode the two paths (stdlib python, which the port probe below
# already needs). A space, +, & or # in a path would otherwise be decoded into
# something else by the browser's URL parser, and the page would fetch that.
enc() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="/"))' "$1"; }

# Assemble the shell's query: ?view=setlist when asked, ?show= for the show
# frame, ?set= for the setlist frame. Reloading the page restores all three.
# A launch with no show passes show= EMPTY on purpose whenever a view or a
# setlist was named: to the show editor a present-but-empty show= means
# "start blank", where an absent one means "load the demo", and the demo
# comes wired for in-place Save.
QUERY=""
[ -n "$VIEW" ] && QUERY="${QUERY}&view=${VIEW}"
if [ -n "$SHOW_PARAM" ]; then
  QUERY="${QUERY}&show=$(enc "$SHOW_PARAM")"
elif [ -n "$SET_PARAM" ] || [ -n "$VIEW" ]; then
  QUERY="${QUERY}&show="
fi
[ -n "$SET_PARAM" ] && QUERY="${QUERY}&set=$(enc "$SET_PARAM")"
[ -n "$QUERY" ] && QUERY="?${QUERY#&}"

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

URL="http://localhost:${PORT}/${PAGE}${QUERY}"

SERVER_PID=""
cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  echo ""
  echo "stopped."
}
trap cleanup EXIT INT TERM

# Blocks until the port at 127.0.0.1:$1 accepts a connection, or until
# $2 (a still-running background PID, checked so a server that crashed on
# startup doesn't make us wait out the whole timeout) exits first. Used
# for both the primary server and the http.server fallback below.
wait_for_port() {
  local port="$1" pid="$2"
  for _ in $(seq 1 50); do
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      return 1  # the server process already died — no point waiting further
    fi
    if python3 -c "
import socket, sys
s = socket.socket(); s.settimeout(0.2)
try:
    s.connect(('127.0.0.1', int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
" "$port" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

mkdir -p "$REPO/tmp"
SERVER_LOG="$REPO/tmp/editor-server.log"
: > "$SERVER_LOG"

python3 "$REPO/scripts/editor_server.py" "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

if wait_for_port "$PORT" "$SERVER_PID"; then
  BAKE_SERVER=1
else
  # editor_server.py never came up (crashed, import error, permission
  # issue — see $SERVER_LOG). Fall back to the plain static server so the
  # editor still works; Create Preview will report itself unavailable.
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  # The fallback is NOT merely "the same server minus Create Preview".
  # `python3 -m http.server` sends Last-Modified and no Cache-Control, which
  # lets a browser apply heuristic freshness (RFC 9111 4.2.2) to each ES
  # module independently. The editor has no bundler and no content hashes in
  # its module URLs, so one fallback session can leave a stale
  # duckshow-core.js cached at this exact origin and path -- and a LATER,
  # perfectly good editor_server.py session cannot undo it, because a cache
  # entry that is never revalidated never sees the new no-store header. That
  # is a fresh HTML paired with an old module: the editor breaks with no
  # visible cause. Say so plainly rather than naming Create Preview as the
  # only casualty, and keep the fallback's own output instead of discarding
  # it, so a second failure is not silent too.
  echo ""
  echo "WARNING: editor_server.py did not start (see tmp/editor-server.log)."
  echo "         Falling back to plain http.server, which means:"
  echo "           - Create Preview is unavailable (no bake API), and"
  echo "           - assets are served WITHOUT Cache-Control, so your browser may"
  echo "             cache editor scripts and later pair them with a newer page."
  echo "         If the editor misbehaves, hard-reload (Shift-Cmd-R / Ctrl-Shift-R)."
  echo "         Prefer fixing the cause above and re-running this script."
  echo ""
  BAKE_SERVER=0
  python3 -m http.server "$PORT" --bind 127.0.0.1 >>"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  if ! wait_for_port "$PORT" "$SERVER_PID"; then
    echo "the http.server fallback could not bind port $PORT either (see $SERVER_LOG)."
    echo "Something else is already listening there — opening the browser now would"
    echo "load whatever that is, not this checkout. Re-run with a different PORT."
    exit 1
  fi
fi

echo "duckswarm editor  $URL"
[ -n "$SHOW_PARAM" ] && echo "show              ${SHOW_PARAM#/}"
[ -n "$SET_PARAM" ] && echo "setlist           ${SET_PARAM#/}"
[ "$BAKE_SERVER" = "1" ] && echo "baking            available via Create Preview (see docs/viewer.md)"
echo "Ctrl+C to stop."

if command -v open >/dev/null 2>&1; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1
else
  echo "(open that URL in a browser)"
fi

wait "$SERVER_PID"
