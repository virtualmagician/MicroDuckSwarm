#!/usr/bin/env bash
# End-to-end demo: two mock ducks + two duck-agents + the reference show master
# playing shows/demo/demo.duckshow.json, then timing verification from the
# mock ducks' intent logs. No hardware required.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/python"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/duckswarm-e2e.XXXXXX")"
echo "e2e run dir: $RUN_DIR"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT

cd "$PY"

echo "── starting mock ducks"
python3 -m mock_duck --name duck-01 --tcp 127.0.0.1:7010 --log "$RUN_DIR/duck-01.intents.jsonl" >"$RUN_DIR/mock-01.log" 2>&1 &
PIDS+=($!)
python3 -m mock_duck --name duck-02 --tcp 127.0.0.1:7011 --log "$RUN_DIR/duck-02.intents.jsonl" >"$RUN_DIR/mock-02.log" 2>&1 &
PIDS+=($!)
sleep 1

echo "── starting duck-agents"
python3 -m duck_agent --duck-id duck-01 --robotd 127.0.0.1:7010 --shows-dir "$REPO/shows" \
  --listen-port 47801 --master-port 47800 --master-host 127.0.0.1 >"$RUN_DIR/agent-01.log" 2>&1 &
PIDS+=($!)
python3 -m duck_agent --duck-id duck-02 --robotd 127.0.0.1:7011 --shows-dir "$REPO/shows" \
  --listen-port 47802 --master-port 47800 --master-host 127.0.0.1 >"$RUN_DIR/agent-02.log" 2>&1 &
PIDS+=($!)
sleep 1

echo "── show master: load + play (20 s show)"
python3 tools/showmaster.py \
  --duck duck-01=127.0.0.1:47801 --duck duck-02=127.0.0.1:47802 \
  --role lead=duck-01 --role wing=duck-02 \
  run "$REPO/shows/demo/demo.duckshow.json"

echo "── verifying intent logs"
sleep 1
python3 "$REPO/scripts/verify_e2e.py" "$RUN_DIR/duck-01.intents.jsonl" "$RUN_DIR/duck-02.intents.jsonl"
