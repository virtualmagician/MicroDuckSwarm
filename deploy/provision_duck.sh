#!/usr/bin/env bash
# deploy/provision_duck.sh -- get one duck ready to run duck-agent, or
# upgrade/roll back the release already on it.
#
# UNTESTED AGAINST REAL HARDWARE. See docs/provisioning.md for what this
# assumes about a stock MicroDuck image and why; verified here with
# `bash -n` and a --dry-run run against a fake, unreachable host (which
# must return instantly -- see lib/common.sh's remote_read comment).
#
# Usage:
#   deploy/provision_duck.sh <host> --duck-id <id> [options]
#   deploy/provision_duck.sh <host> --rollback [--restart]
#
# Options:
#   --duck-id <id>        Required unless --rollback. [a-zA-Z0-9_-]+.
#   --master-host <host>  Baked into agent.env; omit to let the agent learn
#                          the master's address from its first packet.
#   --master-port <n>     Default 47800.
#   --listen-port <n>     Default 47801.
#   --user <ssh-user>     Default: radxa (or $DUCKSWARM_SSH_USER).
#   --repo <path>         Local repo root. Default: this script's ../..
#   --force-config        Rewrite /etc/duckswarm/agent.env even if present
#                          (the existing one is backed up first). Without
#                          this flag an existing agent.env is left alone --
#                          same "written once, not overwritten by an
#                          update" discipline as the vendor's own
#                          /etc/robot/robotd.toml.
#   --restart              Required to bounce a service that is already
#                          active. Without it, a new release is uploaded
#                          and `current` is repointed at it, but the
#                          running process (if any) is left alone -- so
#                          this is always safe to run against a duck that
#                          might be mid-show; the new release takes effect
#                          on the next restart, whenever that is.
#   --rollback             Point `current` back at the previous release
#                          and restart. Implies restart (that's the whole
#                          point); does not touch agent.env or upload
#                          anything.
#   --dry-run              Print every remote action instead of taking it.
#
# Refuses to run at all without --duck-id (unless --rollback), and
# refuses to restart an active service without --restart -- the two
# "explicit flag for anything destructive" gates this script has.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST=""
DUCK_ID=""
MASTER_HOST=""
MASTER_PORT="47800"
LISTEN_PORT="47801"
FORCE_CONFIG=0
DO_RESTART=0
DO_ROLLBACK=0

usage() { sed -n '2,43p' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --duck-id) DUCK_ID="$2"; shift 2 ;;
    --master-host) MASTER_HOST="$2"; shift 2 ;;
    --master-port) MASTER_PORT="$2"; shift 2 ;;
    --listen-port) LISTEN_PORT="$2"; shift 2 ;;
    --user) DUCKSWARM_SSH_USER="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --force-config) FORCE_CONFIG=1; shift ;;
    --restart) DO_RESTART=1; shift ;;
    --rollback) DO_ROLLBACK=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *)
      [ -z "$HOST" ] || die "unexpected extra argument: $1"
      HOST="$1"; shift ;;
  esac
done

[ -n "$HOST" ] || { usage; die "host is required"; }
if [ "$DO_ROLLBACK" = 0 ]; then
  [ -n "$DUCK_ID" ] || die "--duck-id is required (unless --rollback)"
  case "$DUCK_ID" in
    # NOT "[A-Za-z0-9_-]*)" -- that glob only constrains the *first*
    # character; '*' then matches the rest of the string unconditionally.
    # Rejecting "any disallowed character anywhere" needs the negated
    # class below (empty is caught by the -n check above already, listed
    # again here for a self-contained pattern).
    ''|*[!A-Za-z0-9_-]*) die "invalid --duck-id '$DUCK_ID': letters, digits, '_', '-' only" ;;
  esac
fi
# MASTER_HOST ends up as a bare value in a written env file (see
# agent.env below); not a code-injection risk either way (heredoc
# substitution never re-parses its output as a script), but whitespace
# or quotes would produce a malformed line, so reject them up front.
case "$MASTER_HOST" in
  *[[:space:]\'\"]*) die "invalid --master-host '$MASTER_HOST': no whitespace or quotes" ;;
esac

require_cmd ssh
require_cmd rsync

log "== provision_duck.sh: $HOST (dry-run=$DRY_RUN, rollback=$DO_ROLLBACK) =="

# -- rollback path -----------------------------------------------------
#
# Independent of everything below: just flip the symlink back and
# restart. No upload, no config touch.
if [ "$DO_ROLLBACK" = 1 ]; then
  prev="$(remote_read "$HOST" "cat '$DUCKSWARM_PREVIOUS_MARKER' 2>/dev/null || true")"
  if [ -z "$prev" ] && [ "$DRY_RUN" = 0 ]; then
    die "no previous release recorded at $DUCKSWARM_PREVIOUS_MARKER on $HOST -- nothing to roll back to"
  fi
  [ -n "$prev" ] || prev="<unknown: dry-run, no real read>"
  log "rolling back $HOST: current -> $prev"
  script=$(cat <<EOF
set -euo pipefail
prev="\$(cat '$DUCKSWARM_PREVIOUS_MARKER')"
[ -d "$DUCKSWARM_RELEASES_DIR/\$prev" ] || { echo "previous release dir missing: \$prev" >&2; exit 1; }
sudo ln -sfn "$DUCKSWARM_RELEASES_DIR/\$prev" "$DUCKSWARM_CURRENT_LINK"
sudo systemctl restart "$DUCKSWARM_UNIT_NAME"
echo "rolled back to \$prev and restarted"
EOF
)
  remote_script "$HOST" "$script"
  log "== rollback complete (or previewed) =="
  exit 0
fi

# -- normal provisioning path -------------------------------------------

PY_SRC="$REPO/python"
[ -d "$PY_SRC/duck_agent" ] || die "not found: $PY_SRC/duck_agent (wrong --repo?)"
[ -d "$PY_SRC/duckshow" ] || die "not found: $PY_SRC/duckshow (wrong --repo?)"
[ -f "$REPO/deploy/duck-agent-launch.sh" ] || die "not found: $REPO/deploy/duck-agent-launch.sh"
[ -f "$REPO/deploy/duckswarm-agent.service" ] || die "not found: $REPO/deploy/duckswarm-agent.service"

RELEASE="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE_DIR="$DUCKSWARM_RELEASES_DIR/$RELEASE"
log "release: $RELEASE"

# Step 1: base layout, service account, robot-group membership. Idempotent
# -- every check either no-ops or is itself idempotent (useradd is
# guarded, usermod -aG is naturally idempotent, mkdir -p is idempotent).
bootstrap_script=$(cat <<EOF
set -euo pipefail
getent group '$ROBOT_GROUP' >/dev/null || {
  echo "FATAL: group '$ROBOT_GROUP' does not exist on this host." >&2
  echo "That group is created by the vendor image (sysusers.d/robot.conf)" >&2
  echo "and robotd.service itself refuses to start without it -- this" >&2
  echo "does not look like a stock MicroDuck image. Stopping." >&2
  exit 1
}
if ! id -u '$DUCKSWARM_SERVICE_USER' >/dev/null 2>&1; then
  sudo useradd --system --no-create-home --home-dir '$DUCKSWARM_VAR_DIR' \\
    --shell /usr/sbin/nologin '$DUCKSWARM_SERVICE_USER'
  echo "created system user $DUCKSWARM_SERVICE_USER"
fi
sudo usermod -aG '$ROBOT_GROUP' '$DUCKSWARM_SERVICE_USER'
sudo mkdir -p '$DUCKSWARM_ETC_DIR' '$DUCKSWARM_POLICIES_DIR' '$RELEASE_DIR/python' '$RELEASE_DIR/bin'
sudo chown -R '$DUCKSWARM_SERVICE_USER:$DUCKSWARM_SERVICE_GROUP' '$DUCKSWARM_ETC_DIR' '$DUCKSWARM_VAR_DIR'
if [ -e '$DUCKSWARM_ENV_FILE' ] && [ '$FORCE_CONFIG' = 0 ]; then
  echo "agent.env already present, leaving it alone (pass --force-config to rewrite)"
else
  if [ -e '$DUCKSWARM_ENV_FILE' ]; then
    sudo cp '$DUCKSWARM_ENV_FILE' "$DUCKSWARM_ENV_FILE.bak-\$(date -u +%Y%m%dT%H%M%SZ)"
    echo "backed up existing agent.env before rewriting"
  fi
  sudo tee '$DUCKSWARM_ENV_FILE' >/dev/null <<'AGENT_ENV'
DUCK_ID=$DUCK_ID
ROBOTD_TARGET=$ROBOTD_SOCK
SHOWS_DIR=$DUCKSWARM_SHOWS_DIR
LISTEN_PORT=$LISTEN_PORT
MASTER_PORT=$MASTER_PORT
MASTER_HOST=$MASTER_HOST
VERBOSE=0
AGENT_ENV
  sudo chown '$DUCKSWARM_SERVICE_USER:$DUCKSWARM_SERVICE_GROUP' '$DUCKSWARM_ENV_FILE'
  sudo chmod 0640 '$DUCKSWARM_ENV_FILE'
  echo "wrote agent.env"
fi
EOF
)
# bootstrap_script's heredoc above is unquoted (<<EOF, not <<'EOF'), so
# every $VAR in it -- DUCK_ID, ROBOTD_SOCK, DUCKSWARM_SHOWS_DIR, etc. --
# was already substituted with this script's own local values the moment
# it was captured, including the values written inside the nested
# <<'AGENT_ENV'...AGENT_ENV block. That inner block's quoted delimiter
# only protects against the *remote* shell re-expanding whatever literal
# text ends up there (e.g. if MASTER_HOST is empty, or, in principle,
# contained a literal '$'); it plays no part in getting our own values in
# -- that already happened here, the same reason duck-agent-launch.sh
# exists rather than leaning on systemd's own ExecStart variable rules.
# A remote-side value that must be evaluated at execution time instead
# (there are none in this block) would need `\$` to survive this capture.
remote_script "$HOST" "$bootstrap_script"

# Step 2: upload the release (duck_agent + duckshow packages, the launch
# wrapper) and the systemd unit. duckshow is a hard runtime dependency of
# duck_agent (`import duckshow`, python/duck_agent/agent.py); mock_duck,
# tools/ and tests/ are dev-only and deliberately not shipped to a duck.
rsync_push "$HOST" "$PY_SRC/duck_agent/" "$RELEASE_DIR/python/duck_agent/" -a --delete
rsync_push "$HOST" "$PY_SRC/duckshow/" "$RELEASE_DIR/python/duckshow/" -a --delete
rsync_push "$HOST" "$REPO/deploy/duck-agent-launch.sh" "$RELEASE_DIR/bin/duck-agent-launch.sh"
rsync_push "$HOST" "$REPO/deploy/duckswarm-agent.service" "$DUCKSWARM_UNIT_PATH"

finish_script=$(cat <<EOF
set -euo pipefail
sudo chmod +x '$RELEASE_DIR/bin/duck-agent-launch.sh'
sudo chown -R '$DUCKSWARM_SERVICE_USER:$DUCKSWARM_SERVICE_GROUP' '$RELEASE_DIR'
sudo chown root:root '$DUCKSWARM_UNIT_PATH'
sudo chmod 0644 '$DUCKSWARM_UNIT_PATH'

prev_target="\$(readlink -f '$DUCKSWARM_CURRENT_LINK' 2>/dev/null || true)"
if [ -n "\$prev_target" ]; then
  basename "\$prev_target" | sudo tee '$DUCKSWARM_PREVIOUS_MARKER' >/dev/null
fi
sudo ln -sfn '$RELEASE_DIR' '$DUCKSWARM_CURRENT_LINK'
echo "current -> $RELEASE_DIR"

# Disk hygiene: an SBC's eMMC/SD is not the place to accumulate every
# release forever. Keep only current + the one .previous points at.
keep_prev="\$(cat '$DUCKSWARM_PREVIOUS_MARKER' 2>/dev/null || true)"
for d in '$DUCKSWARM_RELEASES_DIR'/*/; do
  d="\${d%/}"
  name="\$(basename "\$d")"
  [ "\$name" = "$RELEASE" ] && continue
  [ -n "\$keep_prev" ] && [ "\$name" = "\$keep_prev" ] && continue
  sudo rm -rf "\$d"
  echo "pruned old release \$name"
done

sudo systemctl daemon-reload
sudo systemctl enable '$DUCKSWARM_UNIT_NAME' >/dev/null

if systemctl is-active --quiet '$DUCKSWARM_UNIT_NAME'; then
  if [ '$DO_RESTART' = 1 ]; then
    sudo systemctl restart '$DUCKSWARM_UNIT_NAME'
    echo "restarted $DUCKSWARM_UNIT_NAME (new release active)"
  else
    echo "$DUCKSWARM_UNIT_NAME is already active -- new release uploaded and"
    echo "current/ repointed, but the running process was left alone."
    echo "Re-run with --restart to activate it now (this will interrupt any"
    echo "duck currently mid-show)."
  fi
else
  sudo systemctl start '$DUCKSWARM_UNIT_NAME'
  echo "started $DUCKSWARM_UNIT_NAME"
fi
EOF
)
remote_script "$HOST" "$finish_script"

log "== provision_duck.sh: $HOST done (or previewed) =="
