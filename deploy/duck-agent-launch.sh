#!/usr/bin/env bash
# deploy/duck-agent-launch.sh -- ExecStart target for duckswarm-agent.service.
#
# Deployed alongside each release (/opt/duckswarm/releases/<rel>/bin/), so
# it's swapped atomically with the code by the current/ symlink flip. Its
# only job: turn /etc/duckswarm/agent.env (loaded by systemd's
# EnvironmentFile=, already in this process's environment by the time it
# runs) into duck_agent's argv, with MASTER_HOST genuinely optional --
# see the ExecStart= comment in duckswarm-agent.service for why this is a
# script and not inline systemd variable expansion.
#
# UNTESTED AGAINST REAL HARDWARE (docs/provisioning.md). Verified with
# `bash -n` and by sourcing it under a fake environment (see
# deploy/provision_duck.sh's self-check).

set -euo pipefail

: "${DUCK_ID:?agent.env must set DUCK_ID}"
: "${ROBOTD_TARGET:?agent.env must set ROBOTD_TARGET}"
: "${SHOWS_DIR:?agent.env must set SHOWS_DIR}"
: "${LISTEN_PORT:=47801}"
: "${MASTER_PORT:=47800}"

args=(
  --duck-id "$DUCK_ID"
  --robotd "$ROBOTD_TARGET"
  --shows-dir "$SHOWS_DIR"
  --listen-port "$LISTEN_PORT"
  --master-port "$MASTER_PORT"
)

if [ -n "${MASTER_HOST:-}" ]; then
  args+=(--master-host "$MASTER_HOST")
fi
if [ "${VERBOSE:-0}" = "1" ]; then
  args+=(-v)
fi

exec /usr/bin/python3 -m duck_agent "${args[@]}"
