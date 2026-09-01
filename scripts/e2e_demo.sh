#!/usr/bin/env bash
# End-to-end demo: two mock ducks + two duck-agents + the reference show master
# playing shows/demo/demo.duckshow.json, then timing verification from the
# mock ducks' intent logs. No hardware required.
#
# Ports default to the repo-documented 7010/7011 (mock ducks, TCP) and
# 47800-47802 (master/agents, UDP); override with E2E_PORT_BASE /
# E2E_UDP_BASE to run a second instance in parallel (e.g. another agent
# working in this repo at the same time) without colliding.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/python"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/duckswarm-e2e.XXXXXX")"
echo "e2e run dir: $RUN_DIR"

PORT_BASE="${E2E_PORT_BASE:-7010}"
UDP_BASE="${E2E_UDP_BASE:-47800}"
MOCK1_PORT=$((PORT_BASE))
MOCK2_PORT=$((PORT_BASE + 1))
MASTER_PORT=$((UDP_BASE))
AGENT1_PORT=$((UDP_BASE + 1))
AGENT2_PORT=$((UDP_BASE + 2))

READY_TIMEOUT_S=15

PIDS=()
FAILED=0
cleanup() {
  status=$?
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  if [ "$status" -ne 0 ] || [ "$FAILED" -ne 0 ]; then
    echo "── e2e failed (exit $status); dumping run logs from $RUN_DIR"
    for f in "$RUN_DIR"/*.log; do
      [ -e "$f" ] || continue
      echo "---- $f ----"
      tail -n 40 "$f"
    done
  fi
}
trap cleanup EXIT

# -- port preflight: fail fast (with a clear message) instead of letting a
# background python3 process die silently at bind() and produce an opaque
# "head stream missing" failure 20s later with no clue why.
#
# For TCP this checks for an actual listener (connect-refused == free)
# rather than bind()ability: a just-closed prior run's socket can sit in
# TIME_WAIT for a minute afterward, which fails a strict bind() check even
# though nothing is listening and a real client would connect fine (or a
# fresh server would bind fine, since most stacks bind past TIME_WAIT).
check_tcp_free() {
  python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.2)
try:
    s.connect(('127.0.0.1', int(sys.argv[1])))
except OSError:
    sys.exit(0)  # refused/unreachable: nothing listening, port is free
else:
    print('a server is already listening on this port'); sys.exit(1)
finally:
    s.close()
" "$1"
}
check_udp_free() {
  python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(('0.0.0.0', int(sys.argv[1])))
except OSError as e:
    print(e); sys.exit(1)
finally:
    s.close()
" "$1"
}

echo "── checking ports are free (tcp $MOCK1_PORT/$MOCK2_PORT, udp $MASTER_PORT/$AGENT1_PORT/$AGENT2_PORT)"
port_check_failed=0
for p in "$MOCK1_PORT" "$MOCK2_PORT"; do
  check_tcp_free "$p" || { echo "tcp port $p is busy (set E2E_PORT_BASE to use a different pair)"; port_check_failed=1; }
done
for p in "$MASTER_PORT" "$AGENT1_PORT" "$AGENT2_PORT"; do
  check_udp_free "$p" || { echo "udp port $p is busy (set E2E_UDP_BASE to use a different range)"; port_check_failed=1; }
done
if [ "$port_check_failed" -ne 0 ]; then
  FAILED=1
  exit 1
fi

# -- stale __pycache__ under a Dropbox-synced checkout can retain bytecode
# compiled against an *older* version of a source file if Dropbox's sync
# touches the file's mtime without invalidating Python's mtime-based pyc
# cache; empirically reproduced (see repo history around this line) as a
# spurious "TypeError: missing positional argument" deep in mock_duck that
# has nothing to do with the actual code. Cheap to avoid: pycache dirs are
# gitignored build output, never source.
find "$PY" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cd "$PY"

wait_for_tcp() {
  local port="$1" name="$2" deadline
  deadline=$((SECONDS + READY_TIMEOUT_S))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.2)
try:
    s.connect(('127.0.0.1', int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
" "$port"; then
      return 0
    fi
    sleep 0.1
  done
  echo "$name did not start listening on port $port within ${READY_TIMEOUT_S}s"
  return 1
}

wait_for_log_line() {
  local logfile="$1" pattern="$2" name="$3" deadline
  deadline=$((SECONDS + READY_TIMEOUT_S))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if grep -q "$pattern" "$logfile" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  echo "$name never logged '$pattern' within ${READY_TIMEOUT_S}s"
  return 1
}

require_alive() {
  local pid="$1" name="$2"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name (pid $pid) is not running -- see its log in $RUN_DIR"
    FAILED=1
    exit 1
  fi
}

echo "── starting mock ducks"
python3 -m mock_duck --name duck-01 --tcp "127.0.0.1:$MOCK1_PORT" --log "$RUN_DIR/duck-01.intents.jsonl" >"$RUN_DIR/mock-01.log" 2>&1 &
PIDS+=($!)
python3 -m mock_duck --name duck-02 --tcp "127.0.0.1:$MOCK2_PORT" --log "$RUN_DIR/duck-02.intents.jsonl" >"$RUN_DIR/mock-02.log" 2>&1 &
PIDS+=($!)
require_alive "${PIDS[0]}" "mock duck-01"
require_alive "${PIDS[1]}" "mock duck-02"
wait_for_tcp "$MOCK1_PORT" "mock duck-01" || { FAILED=1; exit 1; }
wait_for_tcp "$MOCK2_PORT" "mock duck-02" || { FAILED=1; exit 1; }

echo "── starting duck-agents"
python3 -m duck_agent --duck-id duck-01 --robotd "127.0.0.1:$MOCK1_PORT" --shows-dir "$REPO/shows" \
  --listen-port "$AGENT1_PORT" --master-port "$MASTER_PORT" --master-host 127.0.0.1 >"$RUN_DIR/agent-01.log" 2>&1 &
PIDS+=($!)
python3 -m duck_agent --duck-id duck-02 --robotd "127.0.0.1:$MOCK2_PORT" --shows-dir "$REPO/shows" \
  --listen-port "$AGENT2_PORT" --master-port "$MASTER_PORT" --master-host 127.0.0.1 >"$RUN_DIR/agent-02.log" 2>&1 &
PIDS+=($!)
require_alive "${PIDS[2]}" "duck-agent duck-01"
require_alive "${PIDS[3]}" "duck-agent duck-02"
wait_for_log_line "$RUN_DIR/agent-01.log" "listening on UDP" "duck-agent duck-01" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-02.log" "listening on UDP" "duck-agent duck-02" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-01.log" "robotd connected" "duck-agent duck-01 (robotd link)" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-02.log" "robotd connected" "duck-agent duck-02 (robotd link)" || { FAILED=1; exit 1; }

echo "── all processes up; PIDs alive, sockets bound, robotd links connected"
for pid in "${PIDS[@]}"; do
  require_alive "$pid" "process $pid"
done

echo "── show master: load + play (20 s show)"
python3 tools/showmaster.py \
  --duck "duck-01=127.0.0.1:$AGENT1_PORT" --duck "duck-02=127.0.0.1:$AGENT2_PORT" \
  --port "$MASTER_PORT" \
  --role lead=duck-01 --role wing=duck-02 \
  run "$REPO/shows/demo/demo.duckshow.json"

echo "── verifying intent logs"
# `run` returns once its wait_s timer elapses, which races the agents'
# own end-of-show robot.stop by roughly a tick period; wait for both
# intent logs to actually contain it (IntentLog flushes synchronously,
# so this is a tight poll, not a guess) instead of a blind sleep.
wait_for_log_line "$RUN_DIR/duck-01.intents.jsonl" '"method": "robot.stop"' "duck-01 intent log (robot.stop)" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/duck-02.intents.jsonl" '"method": "robot.stop"' "duck-02 intent log (robot.stop)" || { FAILED=1; exit 1; }

if ! python3 "$REPO/scripts/verify_e2e.py" "$RUN_DIR/duck-01.intents.jsonl" "$RUN_DIR/duck-02.intents.jsonl"; then
  FAILED=1
  exit 1
fi
