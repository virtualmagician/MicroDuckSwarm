#!/usr/bin/env bash
# deploy/deploy_shows.sh -- push .duckshow.json files to every duck in a
# roster (or to one host directly), then verify by hash.
#
# UNTESTED AGAINST REAL HARDWARE. See docs/provisioning.md. Verified with
# `bash -n` and a --dry-run run against a fake, unreachable roster/host.
#
# Usage:
#   deploy/deploy_shows.sh <roster.json> [options]
#   deploy/deploy_shows.sh <host>         [options]
#
# The first form fans out to every unique `host` in a SwarmLink roster
# file ([{"id","host","port","role"}, ...]); the second targets one duck
# directly. Distinguished automatically: a roster is a JSON array file, a
# bare host/IP never parses as one.
#
# Options:
#   --show <id>    Push only shows/<id>.duckshow.json (or shows/<id>/), not
#                  the whole local shows/ tree.
#   --delete       Also remove remote show files that no longer exist
#                  locally (plain rsync --delete semantics). Without it,
#                  this script only ever adds/updates -- the explicit flag
#                  this destructive option needs. The remote shows/
#                  policies/ subdirectory is never touched either way (see
#                  the rsync_args comment below) -- that's push_policy.sh's
#                  territory, not this script's, --delete included.
#   --user <ssh-user>  Default: radxa (or $DUCKSWARM_SSH_USER).
#   --repo <path>       Local repo root. Default: this script's ../..
#   --dry-run           Print every remote action instead of taking it.
#
# docs/swarmlink-protocol.md: "Show files are distributed out-of-band
# before the show (rsync/scp in v1; the load hash check is what makes
# that safe)." This script *is* that out-of-band step; the actual `load`
# command (and its sha256 check) is SwarmLink's job at load-in, not this
# script's -- deploying and loading are deliberately separate actions.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET=""
SHOW_ID=""
DO_DELETE=0

usage() { sed -n '2,35p' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --show) SHOW_ID="$2"; shift 2 ;;
    --delete) DO_DELETE=1; shift ;;
    --user) DUCKSWARM_SSH_USER="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *)
      [ -z "$TARGET" ] || die "unexpected extra argument: $1"
      TARGET="$1"; shift ;;
  esac
done

[ -n "$TARGET" ] || { usage; die "roster.json or host is required"; }

require_cmd ssh
require_cmd rsync
require_cmd python3   # roster.json parsing (looks_like_roster/roster_hosts)
require_cmd shasum    # hash verification

SHOWS_SRC="$REPO/shows"
[ -d "$SHOWS_SRC" ] || die "not found: $SHOWS_SRC (wrong --repo?)"

SHOW_FLAT=""
SHOW_NESTED=""
if [ -n "$SHOW_ID" ]; then
  case "$SHOW_ID" in
    # See provision_duck.sh's DUCK_ID check for why this is
    # *[!...]* and not [...]* -- the latter only constrains the first
    # character.
    ''|*[!A-Za-z0-9_-]*) die "invalid --show '$SHOW_ID': letters, digits, '_', '-' only" ;;
  esac
  SHOW_FLAT="$SHOWS_SRC/$SHOW_ID.duckshow.json"
  SHOW_NESTED="$SHOWS_SRC/$SHOW_ID"
  # Checked here, in the main shell, rather than inside a function called
  # via `< <(...)` process substitution below -- a `die` in a subshell
  # only kills that subshell, not this script, so a bad --show id must be
  # caught before anything runs in one.
  if [ ! -f "$SHOW_FLAT" ] && [ ! -f "$SHOW_NESTED/$SHOW_ID.duckshow.json" ]; then
    die "show '$SHOW_ID' not found as $SHOW_FLAT or $SHOW_NESTED/$SHOW_ID.duckshow.json"
  fi
fi

# -- resolve targets ------------------------------------------------------

HOSTS=()
if looks_like_roster "$TARGET"; then
  log "roster: $TARGET"
  while IFS= read -r h; do
    [ -n "$h" ] && HOSTS+=("$h")
  done < <(roster_hosts "$TARGET")
  [ "${#HOSTS[@]}" -gt 0 ] || die "roster $TARGET has no entries with a 'host' field"
else
  HOSTS=("$TARGET")
fi
log "targets: ${HOSTS[*]}"

# -- what to push -----------------------------------------------------
#
# fixtures/ is validator test data (deliberately-invalid .duckshow.json
# fixtures among them, see python/tests/test_loader_malformed_docs.py) --
# never something a duck should have locally. policies/ is push_policy.sh's
# territory exclusively: excluding it here (rather than just not adding
# it) means --delete can never remove a policy .onnx this script never
# pushed in the first place, regardless of whether shows/policies/ exists
# locally on this Mac.
RSYNC_ARGS=(-a --exclude=fixtures/ --exclude=policies/ --exclude=.DS_Store)
if [ "$DO_DELETE" = 1 ]; then
  RSYNC_ARGS+=(--delete)
fi

# One local file list to push and, after each host, to verify by hash --
# either the whole tree (minus the exclusions above) or one show's files.
# --show's existence was already checked above in the main shell (not
# here: this runs under `< <(...)` process substitution below, where a
# `die` would only kill the subshell, not this script).
list_local_show_files() {
  if [ -n "$SHOW_ID" ]; then
    [ -f "$SHOW_FLAT" ] && printf '%s\n' "$SHOW_FLAT"
    [ -f "$SHOW_NESTED/$SHOW_ID.duckshow.json" ] && find "$SHOW_NESTED" -type f
  else
    find "$SHOWS_SRC" -type f \
      -not -path "$SHOWS_SRC/fixtures/*" \
      -not -path "$SHOWS_SRC/policies/*" \
      -not -name '.DS_Store'
  fi
}

# -- push + verify, per host ---------------------------------------------

FAILED_HOSTS=()
for host in "${HOSTS[@]}"; do
  log "-- $host --"
  if [ -n "$SHOW_ID" ]; then
    if [ -f "$SHOW_FLAT" ]; then
      rsync_push "$host" "$SHOW_FLAT" "$DUCKSWARM_SHOWS_DIR/$SHOW_ID.duckshow.json"
    else
      rsync_push "$host" "$SHOW_NESTED/" "$DUCKSWARM_SHOWS_DIR/$SHOW_ID/" "${RSYNC_ARGS[@]}"
    fi
  else
    rsync_push "$host" "$SHOWS_SRC/" "$DUCKSWARM_SHOWS_DIR/" "${RSYNC_ARGS[@]}"
  fi

  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] skipping post-push hash verification against $host"
    continue
  fi

  host_ok=1
  while IFS= read -r local_file; do
    [ -n "$local_file" ] || continue
    rel="${local_file#"$SHOWS_SRC"/}"
    local_sha="$(shasum -a 256 "$local_file" | awk '{print $1}')"
    remote_sha="$(remote_read "$host" "sha256sum '$DUCKSWARM_SHOWS_DIR/$rel' 2>/dev/null | awk '{print \$1}'" || true)"
    if [ "$local_sha" = "$remote_sha" ]; then
      log "  OK   $rel"
    else
      warn "  MISMATCH $rel (local=$local_sha remote=${remote_sha:-<missing>})"
      host_ok=0
    fi
  done < <(list_local_show_files)
  [ "$host_ok" = 1 ] || FAILED_HOSTS+=("$host")
done

if [ "${#FAILED_HOSTS[@]}" -gt 0 ]; then
  die "hash verification failed on: ${FAILED_HOSTS[*]}"
fi

log "== deploy_shows.sh done (or previewed) =="
