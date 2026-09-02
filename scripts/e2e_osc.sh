#!/usr/bin/env bash
# End-to-end demo of docs/osc-facade.md: two mock ducks + two duck-agents +
# `swarmctl serve` (SwarmLink's OSC facade), driven entirely over OSC/UDP
# with python/tools/osc_send.py standing in for a lighting desk / QLab /
# StageWizard's OSC network cues. No hardware, no Swift API calls from the
# driver side -- this is the same contract an external rig gets.
#
# Mirrors scripts/e2e_demo.sh's structure (port preflight, readiness waits,
# cleanup-with-log-dump-on-failure, E2E_PORT_BASE/E2E_UDP_BASE overrides for
# running a second instance in parallel) but drives the show over OSC
# instead of calling tools/showmaster.py or the SwarmLink API directly.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/python"
SWARMLINK="$REPO/SwarmLink"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/duckswarm-e2e-osc.XXXXXX")"
echo "e2e-osc run dir: $RUN_DIR"

PORT_BASE="${E2E_PORT_BASE:-7010}"
UDP_BASE="${E2E_UDP_BASE:-47800}"
OSC_PORT="${E2E_OSC_PORT:-53300}"
MOCK1_PORT=$((PORT_BASE))
MOCK2_PORT=$((PORT_BASE + 1))
MASTER_PORT=$((UDP_BASE))
AGENT1_PORT=$((UDP_BASE + 1))
AGENT2_PORT=$((UDP_BASE + 2))
# The osc_send.py subscriber binds its own local port rather than an
# ephemeral one so a run's ports are reproducible/greppable in logs; kept
# distinct from OSC_PORT (the facade's own port) purely for readability.
LISTENER_LOCAL_PORT=$((OSC_PORT + 1))

READY_TIMEOUT_S=15
# The demo show is 20s (shows/demo/demo.duckshow.json meta.duration); with
# a 1.0s play lead plus scheduling/network slop, end-of-show should land
# well inside this.
SHOW_TIMEOUT_S=35

PIDS=()
FAILED=0
cleanup() {
  status=$?
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  if [ "$status" -ne 0 ] || [ "$FAILED" -ne 0 ]; then
    echo "── e2e-osc failed (exit $status); dumping run logs from $RUN_DIR"
    for f in "$RUN_DIR"/*.log; do
      [ -e "$f" ] || continue
      echo "---- $f ----"
      tail -n 60 "$f"
    done
  fi
}
trap cleanup EXIT

# -- port preflight: same rationale as e2e_demo.sh -- fail fast with a
# clear message instead of a background process dying silently at bind()
# and producing an opaque failure well downstream with no clue why.
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

echo "── checking ports are free (tcp $MOCK1_PORT/$MOCK2_PORT, udp $MASTER_PORT/$AGENT1_PORT/$AGENT2_PORT/$OSC_PORT/$LISTENER_LOCAL_PORT)"
port_check_failed=0
for p in "$MOCK1_PORT" "$MOCK2_PORT"; do
  check_tcp_free "$p" || { echo "tcp port $p is busy (set E2E_PORT_BASE to use a different pair)"; port_check_failed=1; }
done
for p in "$MASTER_PORT" "$AGENT1_PORT" "$AGENT2_PORT"; do
  check_udp_free "$p" || { echo "udp port $p is busy (set E2E_UDP_BASE to use a different range)"; port_check_failed=1; }
done
for p in "$OSC_PORT" "$LISTENER_LOCAL_PORT"; do
  check_udp_free "$p" || { echo "udp port $p is busy (set E2E_OSC_PORT to use a different OSC port)"; port_check_failed=1; }
done
if [ "$port_check_failed" -ne 0 ]; then
  FAILED=1
  exit 1
fi

# -- stale __pycache__ under a Dropbox-synced checkout can retain bytecode
# compiled against an *older* version of a source file (see e2e_demo.sh's
# identical note); pycache dirs are gitignored build output, never source.
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
  local logfile="$1" pattern="$2" name="$3" timeout="${4:-$READY_TIMEOUT_S}" deadline
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if grep -q "$pattern" "$logfile" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  echo "$name never logged '$pattern' within ${timeout}s"
  return 1
}

# Polls `/duckswarm/status` against the facade and waits for any OSC reply
# at all -- a functional readiness probe rather than a log-line grep, since
# swarmctl serve's exact log wording isn't part of the contract (only its
# OSC behavior, docs/osc-facade.md, is). Also checks `pid` is still alive on
# every poll: the require_alive right after backgrounding swarmctl races an
# early exit (fork succeeds, then exec/startup fails moments later), so
# without this an early crash burns the full timeout and reports the
# misleading "never answered" message instead of the real cause (see the
# tail-dumped swarmctl-serve.log either way).
wait_for_osc_facade() {
  local host="$1" port="$2" pid="$3" deadline
  deadline=$((SECONDS + READY_TIMEOUT_S))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "swarmctl serve (pid $pid) exited before its OSC facade came up -- see $RUN_DIR/swarmctl-serve.log"
      return 1
    fi
    if python3 -c "
import socket, sys
sys.path.insert(0, '$PY')
sys.path.insert(0, '$PY/tools')
from tools import osc_send
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.3)
try:
    sock.sendto(osc_send.encode('/duckswarm/status', []), ('$host', $port))
    data, _ = sock.recvfrom(65536)
    osc_send.decode(data)  # just confirm it is a well-formed OSC reply
except Exception:
    sys.exit(1)
finally:
    sock.close()
"; then
      return 0
    fi
    sleep 0.2
  done
  echo "swarmctl serve's OSC facade never answered /duckswarm/status on $host:$port within ${READY_TIMEOUT_S}s"
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

# Sends an OSC command and resends it (a fresh cmd_id each time, per
# docs/osc-facade.md: "a second /duckswarm/play while armed re-arms ...")
# until the listener's log shows a successful (ok=1) /duckswarm/ack for
# `ack_cmd` from both roster ducks, or `ACK_RETRY_TIMEOUT_S` elapses.
# UDP is unreliable-by-design here (show-night invariant: "no multicast
# for must-arrive messages; commands idempotent by cmd_id") -- a rig is
# expected to retry on a dropped/NACKed command, not give up on the first
# reply whatever it says, so this is the operator behavior the facade
# assumes, not a workaround for it.
ACK_RETRY_TIMEOUT_S=15
ACK_RETRY_INTERVAL_S=1
send_until_acked() {
  local ack_cmd="$1" listener_log="$2"; shift 2
  local deadline=$((SECONDS + ACK_RETRY_TIMEOUT_S)) wait_deadline sends=0
  while [ "$SECONDS" -lt "$deadline" ]; do
    python3 tools/osc_send.py "$@"
    sends=$((sends + 1))
    wait_deadline=$((SECONDS + ACK_RETRY_INTERVAL_S))
    while [ "$SECONDS" -lt "$wait_deadline" ]; do
      if grep -q "^/duckswarm/ack $ack_cmd duck-01 1" "$listener_log" 2>/dev/null \
        && grep -q "^/duckswarm/ack $ack_cmd duck-02 1" "$listener_log" 2>/dev/null; then
        if [ "$sends" -gt 1 ]; then
          # Retrying is the right operator behavior, but on loopback a
          # first attempt that is NACKed or times out is a facade/master
          # regression, not packet loss (this is how the load-after-connect
          # re-dial bug hid behind this loop once) -- say so loudly.
          echo "note: /duckswarm/$ack_cmd needed $sends sends before both ducks ack'd ok=1 -- check $RUN_DIR/swarmctl-serve.log and $listener_log"
        fi
        return 0
      fi
      sleep 0.1
    done
  done
  echo "/duckswarm/$ack_cmd was never ack'd ok=1 by both ducks within ${ACK_RETRY_TIMEOUT_S}s -- see $listener_log"
  return 1
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

echo "── mock ducks + duck-agents up; PIDs alive, sockets bound, robotd links connected"
for pid in "${PIDS[@]}"; do
  require_alive "$pid" "process $pid"
done

echo "── writing roster for swarmctl serve"
cat >"$RUN_DIR/roster.json" <<EOF
[
  {"id": "duck-01", "host": "127.0.0.1", "port": $AGENT1_PORT, "role": "lead"},
  {"id": "duck-02", "host": "127.0.0.1", "port": $AGENT2_PORT, "role": "wing"}
]
EOF

echo "── building swarmctl"
if ! swift build --package-path "$SWARMLINK" >"$RUN_DIR/swift-build.log" 2>&1; then
  echo "swarmctl build failed -- see $RUN_DIR/swift-build.log"
  FAILED=1
  exit 1
fi
SWARMCTL_BIN="$(swift build --package-path "$SWARMLINK" --show-bin-path 2>>"$RUN_DIR/swift-build.log")/swarmctl"
if [ ! -x "$SWARMCTL_BIN" ]; then
  echo "swarmctl binary not found at $SWARMCTL_BIN after a successful build"
  FAILED=1
  exit 1
fi

echo "── starting swarmctl serve (OSC facade on udp $OSC_PORT, master on udp $MASTER_PORT)"
"$SWARMCTL_BIN" serve --roster "$RUN_DIR/roster.json" --shows-dir "$REPO/shows" \
  --osc-port "$OSC_PORT" --master-port "$MASTER_PORT" --no-bonjour >"$RUN_DIR/swarmctl-serve.log" 2>&1 &
SWARMCTL_PID=$!
PIDS+=($SWARMCTL_PID)
require_alive "$SWARMCTL_PID" "swarmctl serve"
wait_for_osc_facade 127.0.0.1 "$OSC_PORT" "$SWARMCTL_PID" || { FAILED=1; exit 1; }
require_alive "$SWARMCTL_PID" "swarmctl serve"

echo "── all processes up; subscribing an OSC listener and driving the show over OSC"
# The listener must outlive every budget downstream of its own start: the
# subscription-confirm wait below, up to ACK_RETRY_TIMEOUT_S of retries for
# /load, up to ACK_RETRY_TIMEOUT_S more for /play, the full SHOW_TIMEOUT_S
# wait for end-of-show robot.stop, plus slack for the settle sleep and
# verification below. A fixed --seconds shorter than this worst case would
# let the listener die before the end-of-show status push and misreport it
# as a facade bug when the real cause was slow ack retries (see O24).
LISTENER_SECONDS=$((READY_TIMEOUT_S + 2 * ACK_RETRY_TIMEOUT_S + SHOW_TIMEOUT_S + 10))
python3 -u tools/osc_send.py --ping-then-listen "127.0.0.1:$OSC_PORT" --seconds "$LISTENER_SECONDS" \
  --from "$LISTENER_LOCAL_PORT" \
  --expect /duckswarm/status/transport --expect /duckswarm/status/duck \
  >"$RUN_DIR/osc-listener.log" 2>&1 &
LISTENER_PID=$!
PIDS+=($LISTENER_PID)
require_alive "$LISTENER_PID" "osc_send.py --ping-then-listen"
# Wait for the listener's own log to show it has actually subscribed --
# the facade pushes a full status batch, including
# /duckswarm/status/transport, to every new subscriber immediately (see
# docs/osc-facade.md) -- rather than gambling on a blind sleep. A slow
# runner whose first ping lands late would otherwise send /load before the
# listener is subscribed, missing that ack (pushed only to live subscribers
# plus the one-shot sender) and triggering a benign retry with a misleading
# "needed N sends ... regression" note (see O28).
wait_for_log_line "$RUN_DIR/osc-listener.log" '^/duckswarm/status/transport' "osc listener subscription" || { FAILED=1; exit 1; }
require_alive "$LISTENER_PID" "osc_send.py --ping-then-listen"

echo "── /duckswarm/load demo (resent until both ducks ack ok)"
send_until_acked load "$RUN_DIR/osc-listener.log" "127.0.0.1:$OSC_PORT" /duckswarm/load s:demo || { FAILED=1; exit 1; }

echo "── /duckswarm/play f:1.0 (resent until both ducks ack ok)"
send_until_acked play "$RUN_DIR/osc-listener.log" "127.0.0.1:$OSC_PORT" /duckswarm/play f:1.0 || { FAILED=1; exit 1; }

echo "── waiting for the show to finish (up to ${SHOW_TIMEOUT_S}s)"
# `play` returns immediately; the agents end the show themselves at
# meta.duration. Poll the intent logs for the terminal robot.stop rather
# than a blind sleep -- exactly what e2e_demo.sh does for the same reason.
wait_for_log_line "$RUN_DIR/duck-01.intents.jsonl" '"method": "robot.stop"' "duck-01 intent log (robot.stop)" "$SHOW_TIMEOUT_S" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/duck-02.intents.jsonl" '"method": "robot.stop"' "duck-02 intent log (robot.stop)" "$SHOW_TIMEOUT_S" || { FAILED=1; exit 1; }
# The transport/duck status pushes lag slightly behind the agents' own
# end-of-show robot.stop (they come from swarmctl's telemetry, a separate
# path); give the listener a brief moment to catch up before asserting.
sleep 1
# Confirm the listener is still the one that wrote osc-listener.log and
# didn't hit LISTENER_SECONDS early -- otherwise the OSC verification below
# would blame a missing end-of-show status push on the facade instead of a
# dead listener (see O24).
require_alive "$LISTENER_PID" "osc_send.py --ping-then-listen"

echo "── verifying intent logs"
if ! python3 "$REPO/scripts/verify_e2e.py" "$RUN_DIR/duck-01.intents.jsonl" "$RUN_DIR/duck-02.intents.jsonl"; then
  FAILED=1
  exit 1
fi

echo "── verifying OSC status feedback"
python3 -c "
import re, sys

path = sys.argv[1]
lines = open(path, encoding='utf-8').read().splitlines()

transport_playing = any(line == '/duckswarm/status/transport playing' for line in lines)
duck_playing_ids = {
    line.split()[1]
    for line in lines
    if re.match(r'^/duckswarm/status/duck \S+ \S+ playing ', line)
}
# docs/osc-facade.md: transport is stopped|armed|playing. The agents end
# the show themselves at meta.duration; the master mirrors that and the
# facade pushes it, so the newest transport line must be back to stopped
# without anyone having sent /duckswarm/stop.
transport_lines = [line for line in lines if line.startswith('/duckswarm/status/transport ')]
ended_stopped = bool(transport_lines) and transport_lines[-1] == '/duckswarm/status/transport stopped'

ok = transport_playing and len(duck_playing_ids) >= 2 and ended_stopped
print(f'transport playing seen: {transport_playing}')
print(f'ducks seen in playing state: {sorted(duck_playing_ids)}')
print(f'transport back to stopped after the show ended: {ended_stopped}')
if not ok:
    print('OSC status assertions FAILED')
    sys.exit(1)
print('OSC status assertions passed')
" "$RUN_DIR/osc-listener.log" || { FAILED=1; exit 1; }

echo "── e2e-osc PASSED"
