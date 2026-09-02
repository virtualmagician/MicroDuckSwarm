#!/usr/bin/env bash
# End-to-end demo of docs/authoring.md's recorder: two mock ducks + two
# duck-agents, `swarmctl record` puppeteering duck-01 (role "lead") from a
# scripted (reproducible) gamepad-frame file over the puppet channel
# (docs/swarmlink-protocol.md §6), producing a .duckshow file -- then that
# file is validated with the python duckshow package and played back for
# real with tools/showmaster.py to confirm the recorded choreography
# actually drives the mock duck the way it was puppeteered.
#
# Mirrors scripts/e2e_osc.sh's structure (port preflight, readiness waits,
# a hand-rolled timeout since macOS has no `timeout(1)`, cleanup-with-
# log-dump-on-failure, E2E_* overrides so this can run alongside the other
# e2e scripts without colliding). Two phases, each with its own mock
# ducks/duck-agents and intent logs, so the recording phase's *live*
# puppet-driven intents (recording drives the real duck too, per
# docs/authoring.md's puppet-mode semantics) never get confused with the
# playback phase's intents sampled straight from the recorded file.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/python"
SWARMLINK="$REPO/SwarmLink"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/duckswarm-e2e-record.XXXXXX")"
echo "e2e-record run dir: $RUN_DIR"

PORT_BASE="${E2E_PORT_BASE:-7210}"
UDP_BASE="${E2E_UDP_BASE:-47950}"
MOCK1_PORT=$((PORT_BASE))
MOCK2_PORT=$((PORT_BASE + 1))
MASTER_PORT=$((UDP_BASE))
AGENT1_PORT=$((UDP_BASE + 1))
AGENT2_PORT=$((UDP_BASE + 2))

READY_TIMEOUT_S=15
# The scripted recording is a 0.5s lead-in (--lead) + a 6s script; give
# swarmctl record generous slack for countdown/process startup on a slow
# CI runner before we conclude it hung and kill it.
RECORD_TIMEOUT_S=30
# meta.duration >= 6s once loaded; give playback the same generosity for
# scheduling lead + the show itself.
PLAYBACK_TIMEOUT_S=20

PIDS=()
FAILED=0
cleanup() {
  status=$?
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  if [ "$status" -ne 0 ] || [ "$FAILED" -ne 0 ]; then
    echo "── e2e-record failed (exit $status); dumping run logs from $RUN_DIR"
    for f in "$RUN_DIR"/*.log; do
      [ -e "$f" ] || continue
      echo "---- $f ----"
      tail -n 60 "$f"
    done
  fi
}
trap cleanup EXIT

# -- port preflight: same rationale as e2e_demo.sh/e2e_osc.sh -- fail fast
# with a clear message instead of a background process dying silently at
# bind() and producing an opaque failure well downstream with no clue why.
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

require_alive() {
  local pid="$1" name="$2"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name (pid $pid) is not running -- see its log in $RUN_DIR"
    FAILED=1
    exit 1
  fi
}

# macOS has no timeout(1) (that's GNU coreutils); poll-and-kill instead.
# Mirrors coreutils' exit code convention: 124 on a forced kill.
run_with_timeout() {
  local timeout_s="$1" logfile="$2"; shift 2
  "$@" >"$logfile" 2>&1 &
  local pid=$!
  local deadline=$((SECONDS + timeout_s))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"
      return $?
    fi
    sleep 0.2
  done
  echo "process (pid $pid) did not exit within ${timeout_s}s -- killing" >>"$logfile"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  return 124
}

echo "── starting mock ducks + duck-agents (record phase)"
python3 -m mock_duck --name duck-01 --tcp "127.0.0.1:$MOCK1_PORT" --log "$RUN_DIR/duck-01.record.jsonl" >"$RUN_DIR/mock-01-record.log" 2>&1 &
PIDS+=($!)
python3 -m mock_duck --name duck-02 --tcp "127.0.0.1:$MOCK2_PORT" --log "$RUN_DIR/duck-02.record.jsonl" >"$RUN_DIR/mock-02-record.log" 2>&1 &
PIDS+=($!)
require_alive "${PIDS[0]}" "mock duck-01 (record)"
require_alive "${PIDS[1]}" "mock duck-02 (record)"
wait_for_tcp "$MOCK1_PORT" "mock duck-01 (record)" || { FAILED=1; exit 1; }
wait_for_tcp "$MOCK2_PORT" "mock duck-02 (record)" || { FAILED=1; exit 1; }

python3 -m duck_agent --duck-id duck-01 --robotd "127.0.0.1:$MOCK1_PORT" --shows-dir "$RUN_DIR/rec" \
  --listen-port "$AGENT1_PORT" --master-port "$MASTER_PORT" --master-host 127.0.0.1 >"$RUN_DIR/agent-01-record.log" 2>&1 &
PIDS+=($!)
python3 -m duck_agent --duck-id duck-02 --robotd "127.0.0.1:$MOCK2_PORT" --shows-dir "$RUN_DIR/rec" \
  --listen-port "$AGENT2_PORT" --master-port "$MASTER_PORT" --master-host 127.0.0.1 >"$RUN_DIR/agent-02-record.log" 2>&1 &
PIDS+=($!)
require_alive "${PIDS[2]}" "duck-agent duck-01 (record)"
require_alive "${PIDS[3]}" "duck-agent duck-02 (record)"
wait_for_log_line "$RUN_DIR/agent-01-record.log" "listening on UDP" "duck-agent duck-01 (record)" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-02-record.log" "listening on UDP" "duck-agent duck-02 (record)" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-01-record.log" "robotd connected" "duck-agent duck-01 (record, robotd link)" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-02-record.log" "robotd connected" "duck-agent duck-02 (record, robotd link)" || { FAILED=1; exit 1; }
echo "── record-phase mock ducks + duck-agents up"

mkdir -p "$RUN_DIR/rec"
cat >"$RUN_DIR/roster.json" <<EOF
[
  {"id": "duck-01", "host": "127.0.0.1", "port": $AGENT1_PORT, "role": "lead"},
  {"id": "duck-02", "host": "127.0.0.1", "port": $AGENT2_PORT, "role": "wing"}
]
EOF

# -- scripted input file (docs/authoring.md §2 "Input: ... script:<file>
# replays a JSON list of timed input frames"): a reproducible 6s
# recording -- forward walk 0..2s, a head nod ~2.2..2.9s, A (chirp) at
# 3.0s, left shoulder (kick_left) at 4.5s, options (stop recording) at
# 6.0s. Every frame is a full stick/trigger/button snapshot (matching the
# doc's own worked example, which lists every axis on every frame) held
# until the next frame. Stick magnitudes are well past the documented
# 0.08 dead-zone so they survive scaling to the validation limits
# (max_abs_vx=0.25 m/s -- python/duckshow/limits.py -- so ly=0.7 scales
# to vx~0.175, comfortably over the 0.05 threshold this script checks
# for). Button names (lowercase a/b/x/y, snake_case left_shoulder/
# right_shoulder/menu/options) match SwarmLink/Sources/SwarmLink/
# Recorder.swift's GamepadButton enum.
cat >"$RUN_DIR/record-input.json" <<'EOF'
[
  {"t": 0.0, "lx": 0.0, "ly": 0.7, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": []},
  {"t": 2.0, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": []},
  {"t": 2.2, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": -0.6, "lt": 0.0, "rt": 0.0, "buttons": []},
  {"t": 2.5, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.6, "lt": 0.0, "rt": 0.0, "buttons": []},
  {"t": 2.9, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": []},
  {"t": 3.0, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": ["a"]},
  {"t": 3.1, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": []},
  {"t": 4.5, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": ["left_shoulder"]},
  {"t": 4.6, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": []},
  {"t": 6.0, "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "buttons": ["options"]}
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

echo "── swarmctl record: puppeteering duck-01 (role lead) from the scripted input"
# docs/authoring.md §2: without --show, recording starts at t=0 on a
# countdown (--lead seconds) and meta.duration becomes the recorded
# length (rounded up to the next beat when --bpm is given). No --show
# here, so this is a fresh single-role ("lead") recording, not a layering
# pass over an existing show.
record_status=0
run_with_timeout "$RECORD_TIMEOUT_S" "$RUN_DIR/swarmctl-record.log" \
  "$SWARMCTL_BIN" record \
  --roster "$RUN_DIR/roster.json" \
  --duck duck-01 --role lead \
  --out "$RUN_DIR/rec/rec.duckshow.json" \
  --shows-dir "$RUN_DIR/rec" \
  --input "script:$RUN_DIR/record-input.json" \
  --bpm 120 --lead 0.5 \
  --master-port "$MASTER_PORT" \
  || record_status=$?
if [ "$record_status" -ne 0 ]; then
  echo "swarmctl record exited $record_status -- see $RUN_DIR/swarmctl-record.log"
  FAILED=1
  exit 1
fi
if [ ! -f "$RUN_DIR/rec/rec.duckshow.json" ]; then
  echo "swarmctl record reported success but did not write $RUN_DIR/rec/rec.duckshow.json"
  FAILED=1
  exit 1
fi
echo "── swarmctl record finished -- see $RUN_DIR/swarmctl-record.log"

echo "── stopping record-phase mock ducks + duck-agents"
for pid in "${PIDS[@]}"; do
  kill "$pid" 2>/dev/null || true
done
wait 2>/dev/null || true
PIDS=()

echo "── verifying the live puppet stream drove duck-01 during the take (record-phase intent log)"
# This is the Swift-sender -> Python-agent wire check: docs/authoring.md
# section 1 says a fresh puppet packet in IDLE/LOADED is forwarded as the
# robot.* notifications at the 50 Hz tick and do/sound fire once per seq.
# Without this block a field-name/casing or seq mismatch would go unseen:
# the agent drops malformed or stale packets silently, the recorder
# captures its *own* stream regardless, and the playback checks below
# would still pass.
python3 - "$RUN_DIR/duck-01.record.jsonl" "$RUN_DIR/duck-02.record.jsonl" <<'PYEOF'
import json
import sys


def load(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


failures = []


def check(cond, ok_msg, fail_msg):
    print(("  ok    " if cond else "  FAIL  ") + (ok_msg if cond else fail_msg))
    if not cond:
        failures.append(fail_msg)


puppeteered = load(sys.argv[1])
bystander = load(sys.argv[2])

moves = [e for e in puppeteered if e.get("msg", {}).get("method") == "robot.move"]
heads = [e for e in puppeteered if e.get("msg", {}).get("method") == "robot.head"]
walking = [e for e in moves if e["msg"]["params"].get("vx", 0.0) > 0.05]
check(len(walking) >= 15,
      f"{len(walking)} live robot.move notifications with vx > 0.05 (>= 15): puppet packets were accepted and forwarded",
      f"only {len(walking)} live robot.move notifications with vx > 0.05 -- the agent is not forwarding the recorder's puppet stream")
if walking:
    span = walking[-1]["rx_wall"] - walking[0]["rx_wall"]
    check(1.5 <= span <= 2.6,
          f"walk lasted {span:.2f} s (scripted 2.0 s; held frames re-sent every tick, released on the next frame)",
          f"walk lasted {span:.2f} s, expected ~2.0 s")
nodding = [e for e in heads if abs(e["msg"]["params"].get("head_pitch", 0.0)) > 0.3]
check(len(nodding) >= 5,
      f"{len(nodding)} live robot.head notifications with |head_pitch| > 0.3 (the right-stick nod)",
      f"only {len(nodding)} live robot.head notifications with |head_pitch| > 0.3 -- head channel not forwarded")

fired = {}
for e in puppeteered:
    msg = e.get("msg", {})
    params = msg.get("params", {}) or {}
    key = None
    if msg.get("method") == "robot.do":
        key = ("do", params.get("skill"))
    elif msg.get("method") == "robot.sound":
        key = ("sound", params.get("tag"))
    if key is not None:
        fired.setdefault(key, []).append(e["rx_wall"])
check(fired.get(("sound", "chirp")) is not None, "live chirp fired from the A press", "no live robot.sound chirp during the take")
check(fired.get(("do", "kick_left")) is not None, "live kick_left fired from the left-shoulder press", "no live robot.do kick_left during the take")
check(all(len(v) == 1 for v in fired.values()),
      "each do/sound fired exactly once (once per seq, never re-fired from held packets)",
      f"a do/sound fired more than once: { {k: len(v) for k, v in fired.items()} }")
if fired.get(("sound", "chirp")) and fired.get(("do", "kick_left")):
    check(fired[("sound", "chirp")][0] < fired[("do", "kick_left")][0],
          "chirp fired before kick_left, as scripted", "chirp did not fire before kick_left")

if moves:
    last = moves[-1]["msg"]["params"]
    check(all(abs(last.get(k, 0.0)) < 1e-9 for k in ("vx", "vy", "vyaw")),
          "last live robot.move is zero (closing neutral frame / deadman release)",
          f"last live robot.move is not zero: {last}")

stray = [e["msg"].get("method") for e in bystander
         if str(e.get("msg", {}).get("method", "")).startswith("robot.")]
check(not stray,
      "duck-02 (not puppeteered, no show loaded) received no robot.* intents",
      f"duck-02 received robot.* intents it should not have: {sorted(set(stray))}")

if failures:
    print(f"\nlive puppet verification FAILED: {len(failures)} check(s) failed")
    sys.exit(1)
print("\nlive puppet verification PASSED")
PYEOF

echo "── validating the recorded show file with the python duckshow package"
python3 - "$RUN_DIR/rec/rec.duckshow.json" <<'PYEOF'
import sys

sys.path.insert(0, ".")
from duckshow import DEFAULT_LIMITS, load_show, validate

path = sys.argv[1]
show = load_show(path)
issues = validate(show, DEFAULT_LIMITS)
errors = [i for i in issues if i.severity == "error"]
for i in issues:
    print(f"  {i.severity}: role={i.role} track={i.track} t={i.t} {i.message}")

failures = []


def check(cond, ok_msg, fail_msg):
    print(("  ok    " if cond else "  FAIL  ") + (ok_msg if cond else fail_msg))
    if not cond:
        failures.append(fail_msg)


check(not errors, "zero validation errors", f"{len(errors)} validation error(s)")

duration = show.meta.duration
check(duration is not None and duration >= 6.0,
      f"meta.duration = {duration} (>= 6.0)",
      f"meta.duration = {duration}, expected >= 6.0")

tracks = show.tracks_for("lead")
vx_keyframes = [kf for kf in tracks.locomotion if kf.vx > 0.05]
check(len(vx_keyframes) >= 5,
      f"{len(vx_keyframes)} locomotion keyframe(s) with vx > 0.05 (>= 5)",
      f"only {len(vx_keyframes)} locomotion keyframe(s) with vx > 0.05, expected >= 5")

chirp = next((e for e in tracks.events if e.sound == "chirp"), None)
kick = next((e for e in tracks.events if e.do == "kick_left"), None)
check(chirp is not None, f"chirp event present at t={chirp.t if chirp else None}", "no chirp event recorded")
check(kick is not None, f"kick_left event present at t={kick.t if kick else None}", "no kick_left event recorded")
if chirp is not None and kick is not None:
    check(chirp.t < kick.t,
          f"chirp (t={chirp.t}) fired before kick_left (t={kick.t})",
          f"chirp (t={chirp.t}) did not fire before kick_left (t={kick.t})")

if failures:
    print(f"\nshow-file validation FAILED: {len(failures)} check(s) failed")
    sys.exit(1)
print("\nshow-file validation PASSED")
PYEOF

echo "── starting mock ducks + duck-agents (playback phase, fresh intent logs)"
python3 -m mock_duck --name duck-01 --tcp "127.0.0.1:$MOCK1_PORT" --log "$RUN_DIR/duck-01.playback.jsonl" >"$RUN_DIR/mock-01-playback.log" 2>&1 &
PIDS+=($!)
python3 -m mock_duck --name duck-02 --tcp "127.0.0.1:$MOCK2_PORT" --log "$RUN_DIR/duck-02.playback.jsonl" >"$RUN_DIR/mock-02-playback.log" 2>&1 &
PIDS+=($!)
require_alive "${PIDS[0]}" "mock duck-01 (playback)"
require_alive "${PIDS[1]}" "mock duck-02 (playback)"
wait_for_tcp "$MOCK1_PORT" "mock duck-01 (playback)" || { FAILED=1; exit 1; }
wait_for_tcp "$MOCK2_PORT" "mock duck-02 (playback)" || { FAILED=1; exit 1; }

python3 -m duck_agent --duck-id duck-01 --robotd "127.0.0.1:$MOCK1_PORT" --shows-dir "$RUN_DIR/rec" \
  --listen-port "$AGENT1_PORT" --master-port "$MASTER_PORT" --master-host 127.0.0.1 >"$RUN_DIR/agent-01-playback.log" 2>&1 &
PIDS+=($!)
python3 -m duck_agent --duck-id duck-02 --robotd "127.0.0.1:$MOCK2_PORT" --shows-dir "$RUN_DIR/rec" \
  --listen-port "$AGENT2_PORT" --master-port "$MASTER_PORT" --master-host 127.0.0.1 >"$RUN_DIR/agent-02-playback.log" 2>&1 &
PIDS+=($!)
require_alive "${PIDS[2]}" "duck-agent duck-01 (playback)"
require_alive "${PIDS[3]}" "duck-agent duck-02 (playback)"
wait_for_log_line "$RUN_DIR/agent-01-playback.log" "listening on UDP" "duck-agent duck-01 (playback)" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-02-playback.log" "listening on UDP" "duck-agent duck-02 (playback)" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-01-playback.log" "robotd connected" "duck-agent duck-01 (playback, robotd link)" || { FAILED=1; exit 1; }
wait_for_log_line "$RUN_DIR/agent-02-playback.log" "robotd connected" "duck-agent duck-02 (playback, robotd link)" || { FAILED=1; exit 1; }
echo "── playback-phase mock ducks + duck-agents up"

echo "── playing the recorded show back through tools/showmaster.py run"
# rec.duckshow.json has a single-role ("lead") cast (docs/authoring.md
# §2: "the file is created with a one-role cast if absent"). Unlike
# Show.tracks_for's graceful idle-RoleTracks fallback, the LOAD handshake
# itself NACKs a duck whose assigned role is not in the cast ("role
# 'wing' not in cast") -- so only duck-01/lead is addressed here.
# duck-02's mock+agent stay up alongside it (started above) as the
# second duck a real two-role show would have, they are just not part
# of this particular one-role recording.
playback_status=0
run_with_timeout "$PLAYBACK_TIMEOUT_S" "$RUN_DIR/showmaster-run.log" \
  python3 tools/showmaster.py \
  --duck "duck-01=127.0.0.1:$AGENT1_PORT" \
  --port "$MASTER_PORT" \
  --role lead=duck-01 \
  run "$RUN_DIR/rec/rec.duckshow.json" \
  || playback_status=$?
cat "$RUN_DIR/showmaster-run.log"
if [ "$playback_status" -ne 0 ]; then
  echo "tools/showmaster.py run exited $playback_status -- see $RUN_DIR/showmaster-run.log"
  FAILED=1
  exit 1
fi

wait_for_log_line "$RUN_DIR/duck-01.playback.jsonl" '"method": "robot.stop"' "duck-01 playback intent log (robot.stop)" || { FAILED=1; exit 1; }

echo "── verifying duck-01's playback intent log matches the recorded choreography"
python3 - "$RUN_DIR/duck-01.playback.jsonl" <<'PYEOF'
import json
import sys

path = sys.argv[1]
entries = []
with open(path, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            entries.append(json.loads(line))

failures = []


def check(cond, ok_msg, fail_msg):
    print(("  ok    " if cond else "  FAIL  ") + (ok_msg if cond else fail_msg))
    if not cond:
        failures.append(fail_msg)


t0 = next((e["rx_wall"] for e in entries if e.get("msg", {}).get("method") in
           ("robot.move", "robot.head", "robot.pose", "robot.mouth")), None)
check(t0 is not None, "playback stream present", "no continuous-track notification received at all")

# The walk was recorded for show_time in [0, 2.0)s; a generous window
# tolerates scheduling lead + localhost jitter without being so wide it
# would also catch the end-of-show zeroing move.
walk_vx = []
if t0 is not None:
    for e in entries:
        msg = e.get("msg", {})
        if msg.get("method") != "robot.move":
            continue
        show_time = e["rx_wall"] - t0
        if 0.0 <= show_time <= 2.3:
            vx = msg.get("params", {}).get("vx")
            if vx is not None:
                walk_vx.append(vx)
check(any(v > 0.05 for v in walk_vx),
      f"robot.move vx > 0.05 seen during the walk window ({sum(1 for v in walk_vx if v > 0.05)} of {len(walk_vx)} samples)",
      f"no robot.move with vx > 0.05 during the walk window (samples: {walk_vx[:10]})")

events = {}
for e in entries:
    msg = e.get("msg", {})
    method = msg.get("method")
    params = msg.get("params", {}) or {}
    key = None
    if method == "robot.do":
        key = ("do", params.get("skill"))
    elif method == "robot.sound":
        key = ("sound", params.get("tag"))
    if key is not None and key not in events:
        events[key] = e["rx_wall"]

check(("sound", "chirp") in events, "chirp event replayed", "chirp event missing from playback")
check(("do", "kick_left") in events, "kick_left event replayed", "kick_left event missing from playback")

check(any(e.get("msg", {}).get("method") == "robot.stop" for e in entries),
      "robot.stop at show end", "no robot.stop at show end")

if failures:
    print(f"\nplayback verification FAILED: {len(failures)} check(s) failed")
    sys.exit(1)
print("\nplayback verification PASSED")
PYEOF

echo "── e2e-record PASSED"
