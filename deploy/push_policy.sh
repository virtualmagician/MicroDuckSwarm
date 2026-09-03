#!/usr/bin/env bash
# deploy/push_policy.sh -- install a custom .onnx policy on one duck and
# verify it actually took, via robot.health.
#
# UNTESTED AGAINST REAL HARDWARE. See docs/provisioning.md. Verified with
# `bash -n`, a --dry-run run against a fake host, and `--selftest` (a
# from-fixtures check of the robotd.toml edit itself, no network at all).
#
# Usage:
#   deploy/push_policy.sh <host> --file <local.onnx> --slot <slot> (--yes|--dry-run)
#   deploy/push_policy.sh <host> --rollback (--yes|--dry-run)
#   deploy/push_policy.sh --selftest
#
# Options:
#   --file <path>   Local .onnx to install. Its sha256 is printed at the
#                    end for pasting into a show's requires.policies[].sha256
#                    -- this script only makes the file present and
#                    healthy on the duck, it does not touch any
#                    .duckshow.json (see docs/provisioning.md).
#   --slot <name>    Fixed robotd.toml [policy] slot, e.g. "walk". Checked
#                    against the CONFIRMED base slots (walk, stand,
#                    sitstand, ground_pick, kick_left, kick_right,
#                    roulade); a roller-family name is accepted with a
#                    warning, never invented or hard-validated (upstream
#                    never enumerated those exact names).
#   --rollback       Restore the most recent robotd.toml backup on <host>,
#                    restart robotd, and verify. Does not touch the .onnx.
#   --yes            Required to actually write anything and restart
#                    robotd -- the explicit flag this destructive action
#                    needs. Mutually exclusive with --dry-run; the script
#                    refuses to run with neither.
#   --dry-run        Preview only -- fetches nothing real (see
#                    lib/common.sh's remote_read), shows the edit against
#                    a placeholder, never restarts robotd.
#   --no-rollback-on-failure  If the post-restart health poll doesn't come
#                    back healthy, this script's default is to restore the
#                    robotd.toml backup and restart again automatically --
#                    a duck an hour before doors should never be left
#                    holding a known-bad config. This flag turns that off,
#                    for debugging a failed install in place.
#
# The [policy] slot -> file relationship is CONFIRMED upstream
# (docs/robotd-api.md); the exact shape of a robot.health reply is not --
# only that an unhealthy one contains the text "policy unavailable:
# <reason>" somewhere. This script does not invent a field name for that:
# it substring-matches the confirmed text against the whole reply.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

HOST=""
ONNX_FILE=""
SLOT=""
DO_ROLLBACK=0
DO_YES=0
ROLLBACK_ON_FAILURE=1
DO_SELFTEST=0

usage() { sed -n '2,42p' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --file) ONNX_FILE="$2"; shift 2 ;;
    --slot) SLOT="$2"; shift 2 ;;
    --rollback) DO_ROLLBACK=1; shift ;;
    --yes) DO_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-rollback-on-failure) ROLLBACK_ON_FAILURE=0; shift ;;
    --selftest) DO_SELFTEST=1; shift ;;
    --user) DUCKSWARM_SSH_USER="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *)
      [ -z "$HOST" ] || die "unexpected extra argument: $1"
      HOST="$1"; shift ;;
  esac
done

# -- --selftest: no host, no network, just the awk transform ---------------

run_selftest() {
  local failures=0

  check() {
    local name="$1" got="$2" want="$3"
    if [ "$got" = "$want" ]; then
      log "  PASS $name"
    else
      warn "  FAIL $name"
      warn "    --- got ---"
      printf '%s\n' "$got" | sed 's/^/    /' >&2
      warn "    --- want ---"
      printf '%s\n' "$want" | sed 's/^/    /' >&2
      failures=$((failures + 1))
    fi
  }

  # A: existing [policy] section, slot already present with a different
  # value -> replaced in place, other keys and other sections untouched.
  local toml_a got_a want_a
  toml_a=$(cat <<'EOF'
deadman_ms = 500

[policy]
walk = "/old/walk.onnx"
stand = "/vendor/stand.onnx"

[other]
x = 1
EOF
)
  got_a="$(printf '%s\n' "$toml_a" | toml_set_policy_slot walk /var/lib/duckswarm/shows/policies/new.onnx)"
  want_a=$(cat <<'EOF'
deadman_ms = 500

[policy]
walk = "/var/lib/duckswarm/shows/policies/new.onnx"
stand = "/vendor/stand.onnx"

[other]
x = 1
EOF
)
  check "replace existing key, preserve rest" "$got_a" "$want_a"

  # B: [policy] section present, but not this slot -> appended within the
  # section, before the next one.
  local toml_b got_b want_b
  toml_b=$(cat <<'EOF'
[policy]
stand = "/vendor/stand.onnx"

[other]
x = 1
EOF
)
  got_b="$(printf '%s\n' "$toml_b" | toml_set_policy_slot walk /var/lib/duckswarm/shows/policies/new.onnx)"
  want_b=$(cat <<'EOF'
[policy]
stand = "/vendor/stand.onnx"
walk = "/var/lib/duckswarm/shows/policies/new.onnx"

[other]
x = 1
EOF
)
  check "add key to existing section" "$got_b" "$want_b"

  # C: no [policy] section at all -> appended at end of file.
  local toml_c got_c want_c
  toml_c=$(cat <<'EOF'
deadman_ms = 500
EOF
)
  got_c="$(printf '%s\n' "$toml_c" | toml_set_policy_slot walk /var/lib/duckswarm/shows/policies/new.onnx)"
  want_c=$(cat <<'EOF'
deadman_ms = 500
[policy]
walk = "/var/lib/duckswarm/shows/policies/new.onnx"
EOF
)
  check "append missing section at EOF" "$got_c" "$want_c"

  # D: idempotent -- transforming already-transformed content with the
  # same slot/value again is a no-op (a re-run of push_policy.sh with the
  # same --file must not perturb robotd.toml further).
  local got_d
  got_d="$(printf '%s\n' "$want_a" | toml_set_policy_slot walk /var/lib/duckswarm/shows/policies/new.onnx)"
  check "idempotent re-apply" "$got_d" "$want_a"

  if [ "$failures" = 0 ]; then
    log "selftest: all checks passed"
    return 0
  fi
  warn "selftest: $failures check(s) failed"
  return 1
}

if [ "$DO_SELFTEST" = 1 ]; then
  run_selftest
  exit $?
fi

# -- argument validation ---------------------------------------------------

require_cmd ssh
require_cmd rsync
require_cmd awk
require_cmd shasum

[ -n "$HOST" ] || { usage; die "host is required (or --selftest)"; }
if [ "$DRY_RUN" = 1 ] && [ "$DO_YES" = 1 ]; then
  die "--dry-run and --yes are mutually exclusive"
fi
if [ "$DRY_RUN" = 0 ] && [ "$DO_YES" = 0 ]; then
  die "refusing to run with neither --dry-run nor --yes -- pick a preview or commit to the change"
fi

if [ "$DO_ROLLBACK" = 1 ]; then
  [ -z "$ONNX_FILE" ] && [ -z "$SLOT" ] || die "--rollback does not take --file/--slot"
else
  [ -n "$ONNX_FILE" ] || die "--file <local.onnx> is required (or --rollback)"
  [ -n "$SLOT" ] || die "--slot <name> is required (or --rollback)"
  [ -f "$ONNX_FILE" ] || die "not found: $ONNX_FILE"
  case "$ONNX_FILE" in
    *.onnx) : ;;
    *) die "refusing '$ONNX_FILE': expected a .onnx file" ;;
  esac
  case " $ROBOTD_CONFIRMED_POLICY_SLOTS " in
    *" $SLOT "*) : ;;
    *) warn "'$SLOT' is not one of the CONFIRMED base slots ($ROBOTD_CONFIRMED_POLICY_SLOTS)." \
            "Proceeding -- this may be a roller-family slot upstream never enumerated a name for" \
            "(docs/robotd-api.md); double-check the spelling against your board's own robotd.toml." ;;
  esac
fi

# -- remote helpers specific to this script --------------------------------

# remote_write_file <host> <remote-path> <content> -- installs <content>
# as root at <remote-path> via `sudo tee`. Respects --dry-run like every
# other remote-touching call in these scripts.
remote_write_file() {
  local host="$1" path="$2" content="$3"
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] would write $(printf '%s' "$content" | wc -c | tr -d ' ') bytes to $host:$path (sudo tee)"
    return 0
  fi
  printf '%s' "$content" | ssh "${DUCKSWARM_SSH_OPTS[@]}" "$(ssh_target "$host")" -- sudo tee "$path" >/dev/null
}

# poll_robot_health <host> -- one ssh call; the loop (every
# ROBOTD_HEALTH_POLL_INTERVAL_S, up to ROBOTD_HEALTH_POLL_TIMEOUT_S) runs
# on the duck itself. Prints "HEALTHY <blob>", "UNHEALTHY <blob>",
# "ERROR <blob>", or "TIMEOUT <last-status> <blob>" on stdout; caller
# reads the first word. Runs as root (sudo) so it isn't gated on the ssh
# login user's own robot-group membership, which is unconfirmed
# (docs/provisioning.md). Under --dry-run this never touches the network.
poll_robot_health() {
  local host="$1"
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] would poll robot.health on $host every ${ROBOTD_HEALTH_POLL_INTERVAL_S}s for up to ${ROBOTD_HEALTH_POLL_TIMEOUT_S}s"
    printf 'SKIPPED dry-run\n'
    return 0
  fi
  local py
  py=$(cat <<PYEOF
import json, socket, sys, time

SOCK = "$ROBOTD_SOCK"
INTERVAL = $ROBOTD_HEALTH_POLL_INTERVAL_S
DEADLINE = time.time() + $ROBOTD_HEALTH_POLL_TIMEOUT_S

def once():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(SOCK)
        f = s.makefile("rb")
        s.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hello",
                                "params": {"api_version": 17}}) + "\\n").encode())
        f.readline()
        s.sendall((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "robot.health",
                                "params": {}}) + "\\n").encode())
        line = f.readline()
        reply = json.loads(line.decode())
        if reply.get("error") is not None:
            return "ERROR", json.dumps(reply["error"])
        blob = json.dumps(reply.get("result"))
        if "policy unavailable" in blob:
            return "UNHEALTHY", blob
        return "HEALTHY", blob
    except Exception as exc:
        return "UNREACHABLE", str(exc)
    finally:
        try:
            s.close()
        except Exception:
            pass

last = ("UNREACHABLE", "no attempt made")
while time.time() < DEADLINE:
    status, detail = once()
    last = (status, detail)
    if status == "HEALTHY":
        print("HEALTHY " + detail)
        sys.exit(0)
    time.sleep(INTERVAL)

print("TIMEOUT " + last[0] + " " + last[1])
sys.exit(1)
PYEOF
)
  printf '%s' "$py" | ssh "${DUCKSWARM_SSH_OPTS[@]}" "$(ssh_target "$host")" -- sudo python3 - || true
}

# -- rollback path -----------------------------------------------------

if [ "$DO_ROLLBACK" = 1 ]; then
  log "== push_policy.sh --rollback: $HOST (dry-run=$DRY_RUN) =="
  latest_backup="$(remote_read "$HOST" "ls -1t '${ROBOTD_TOML}.bak-'* 2>/dev/null | head -1 || true")"
  if [ -z "$latest_backup" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      log "[dry-run] no real read performed; would find and restore the newest ${ROBOTD_TOML}.bak-* on $HOST"
    else
      die "no ${ROBOTD_TOML}.bak-* backup found on $HOST -- nothing to roll back to"
    fi
  else
    log "restoring: $latest_backup"
  fi
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] would: sudo cp <latest backup> '$ROBOTD_TOML' && sudo systemctl restart $ROBOTD_SERVICE, then poll robot.health"
    exit 0
  fi
  remote_run "$HOST" "sudo cp '$latest_backup' '$ROBOTD_TOML' && sudo systemctl restart '$ROBOTD_SERVICE'"
  result="$(poll_robot_health "$HOST")"
  log "post-rollback health: $result"
  case "$result" in
    HEALTHY*) log "== rollback verified healthy =="; exit 0 ;;
    *) die "rollback restarted robotd but it did not come back healthy: $result" ;;
  esac
fi

# -- install path -----------------------------------------------------

log "== push_policy.sh: $HOST slot=$SLOT file=$ONNX_FILE (dry-run=$DRY_RUN) =="

BASENAME="$(sanitize_basename "$ONNX_FILE")"
REMOTE_ONNX_PATH="$DUCKSWARM_POLICIES_DIR/$BASENAME"
LOCAL_SHA256="$(shasum -a 256 "$ONNX_FILE" | awk '{print $1}')"
log "local sha256: $LOCAL_SHA256"
log "remote destination: $REMOTE_ONNX_PATH"

OLD_TOML="$(remote_read "$HOST" "sudo cat '$ROBOTD_TOML' 2>/dev/null || true")"
if [ "$DRY_RUN" = 1 ] && [ -z "$OLD_TOML" ]; then
  log "[dry-run] previewing the edit against an empty placeholder (no real robotd.toml read on $HOST):"
fi
NEW_TOML="$(printf '%s' "$OLD_TOML" | toml_set_policy_slot "$SLOT" "$REMOTE_ONNX_PATH")"

if [ "$OLD_TOML" = "$NEW_TOML" ] && [ "$DRY_RUN" = 0 ]; then
  log "robotd.toml already has [policy] $SLOT = \"$REMOTE_ONNX_PATH\" -- skipping robotd restart"
  rsync_push "$HOST" "$ONNX_FILE" "$REMOTE_ONNX_PATH"
  remote_run "$HOST" "sudo chown $DUCKSWARM_SERVICE_USER:$DUCKSWARM_SERVICE_GROUP '$REMOTE_ONNX_PATH'"
  log "sha256 for requires.policies[].sha256: $LOCAL_SHA256"
  log "== push_policy.sh done: no robotd.toml change needed =="
  exit 0
fi

log "-- robotd.toml diff --"
diff <(printf '%s\n' "$OLD_TOML") <(printf '%s\n' "$NEW_TOML") || true

if [ "$DRY_RUN" = 1 ]; then
  log "[dry-run] would: push $ONNX_FILE -> $HOST:$REMOTE_ONNX_PATH; back up and install the robotd.toml above;" \
      "sudo systemctl restart $ROBOTD_SERVICE; poll robot.health for up to ${ROBOTD_HEALTH_POLL_TIMEOUT_S}s"
  exit 0
fi

rsync_push "$HOST" "$ONNX_FILE" "$REMOTE_ONNX_PATH"
remote_run "$HOST" "sudo chown $DUCKSWARM_SERVICE_USER:$DUCKSWARM_SERVICE_GROUP '$REMOTE_ONNX_PATH'"

BACKUP_PATH="${ROBOTD_TOML}.bak-$(date -u +%Y%m%dT%H%M%SZ)"
remote_run "$HOST" "sudo cp '$ROBOTD_TOML' '$BACKUP_PATH'"
log "backed up robotd.toml to $BACKUP_PATH"

remote_write_file "$HOST" "$ROBOTD_TOML" "$NEW_TOML"
remote_run "$HOST" "sudo chown root:root '$ROBOTD_TOML' && sudo chmod 0644 '$ROBOTD_TOML'"

log "restarting robotd and polling robot.health (every ${ROBOTD_HEALTH_POLL_INTERVAL_S}s, up to ${ROBOTD_HEALTH_POLL_TIMEOUT_S}s)..."
remote_run "$HOST" "sudo systemctl restart '$ROBOTD_SERVICE'"
result="$(poll_robot_health "$HOST")"
log "health result: $result"

case "$result" in
  HEALTHY*)
    log "== policy installed and verified healthy =="
    log "sha256 for requires.policies[].sha256: $LOCAL_SHA256"
    exit 0
    ;;
  *)
    warn "robotd did not come back healthy after the policy change: $result"
    if [ "$ROLLBACK_ON_FAILURE" = 1 ]; then
      warn "restoring $BACKUP_PATH and restarting robotd (pass --no-rollback-on-failure to leave it as-is)"
      remote_run "$HOST" "sudo cp '$BACKUP_PATH' '$ROBOTD_TOML' && sudo systemctl restart '$ROBOTD_SERVICE'"
      rollback_result="$(poll_robot_health "$HOST")"
      log "post-auto-rollback health: $rollback_result"
      case "$rollback_result" in
        HEALTHY*) die "policy install failed and was rolled back cleanly; robotd is healthy again on the old config" ;;
        *) die "policy install failed AND the automatic rollback did not come back healthy either ($rollback_result) -- this needs a human on $HOST now" ;;
      esac
    else
      die "policy install failed; robotd.toml backup left at $BACKUP_PATH, robotd NOT rolled back (--no-rollback-on-failure)"
    fi
    ;;
esac
