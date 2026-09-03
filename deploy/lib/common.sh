#!/usr/bin/env bash
# deploy/lib/common.sh -- shared helpers for the deploy/*.sh scripts.
# Source this, don't execute it: `. "$(dirname "$0")/lib/common.sh"`.
#
# UNTESTED AGAINST REAL HARDWARE. Verified so far: `bash -n` on every
# script that sources this, and --dry-run runs against a fake host (see
# docs/provisioning.md "What's untested"). The remote paths and the exact
# shape of a stock MicroDuck image are otherwise best-effort from
# docs/robotd-api.md and docs/architecture.md's CONFIRMED FACTS, not from
# having touched a duck.

# Guard against being executed directly instead of sourced.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  echo "common.sh is a library; source it from one of deploy/*.sh, don't run it directly." >&2
  exit 1
fi

set -euo pipefail

# -- our namespace on the duck ------------------------------------------
#
# Chosen to sit entirely outside the vendor tree (/run/robotd.sock,
# /etc/robot/, /var/lib/robot/, /opt/robot/, and the vendor's own systemd
# units robotd/updaterd/configd/btd/padd/mediad/tofd -- see
# docs/provisioning.md "Filesystem layout" for the citations). Everything
# below is "duckswarm"-prefixed so a directory listing never leaves a
# human guessing which tree is ours.

DUCKSWARM_RELEASES_DIR="/opt/duckswarm/releases"
DUCKSWARM_CURRENT_LINK="/opt/duckswarm/current"
DUCKSWARM_PREVIOUS_MARKER="${DUCKSWARM_RELEASES_DIR}/.previous"
DUCKSWARM_ETC_DIR="/etc/duckswarm"
DUCKSWARM_ENV_FILE="${DUCKSWARM_ETC_DIR}/agent.env"
DUCKSWARM_VAR_DIR="/var/lib/duckswarm"
DUCKSWARM_SHOWS_DIR="${DUCKSWARM_VAR_DIR}/shows"
DUCKSWARM_POLICIES_DIR="${DUCKSWARM_SHOWS_DIR}/policies"
DUCKSWARM_SERVICE_USER="duckswarm"
DUCKSWARM_SERVICE_GROUP="duckswarm"
DUCKSWARM_UNIT_NAME="duckswarm-agent.service"
DUCKSWARM_UNIT_PATH="/etc/systemd/system/${DUCKSWARM_UNIT_NAME}"

# -- vendor paths we depend on but never own -----------------------------
ROBOT_GROUP="robot"
ROBOTD_SOCK="/run/robotd.sock"
ROBOTD_TOML="/etc/robot/robotd.toml"
ROBOTD_SERVICE="robotd.service"

# The base slots CONFIRMED in docs/robotd-api.md. "and their roller-family
# equivalents" is named but never enumerated upstream -- we do not invent
# names for those (CLAUDE.md rule 3), we just don't hard-validate them.
ROBOTD_CONFIRMED_POLICY_SLOTS="walk stand sitstand ground_pick kick_left kick_right roulade"

# robotd's own restart-to-healthy transcript (docs/robotd-api.md, CONFIRMED
# 2026-09-03): "polls the socket every 500 ms for up to 30 s; a real
# transcript shows about 8 to 9 s typical." We have no equivalent number of
# our own, so we reuse theirs for our external poll after `systemctl
# restart robotd` -- it is the only real evidence we have.
ROBOTD_HEALTH_POLL_INTERVAL_S="0.5"
ROBOTD_HEALTH_POLL_TIMEOUT_S="30"

DUCKSWARM_SSH_USER="${DUCKSWARM_SSH_USER:-radxa}"
DUCKSWARM_SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o BatchMode=yes)

DRY_RUN=0

# -- logging ---------------------------------------------------------------

log()  { printf '%s\n' "$*" >&2; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# -- ssh / rsync wrappers ---------------------------------------------------
#
# Every remote-touching call in the deploy/*.sh scripts goes through one
# of these three, and every one of them checks $DRY_RUN first -- that is
# what makes --dry-run a real guarantee of "touched nothing on the
# network" rather than a script that merely prints extra lines on the way
# to doing the thing anyway.

ssh_target() { printf '%s@%s' "$DUCKSWARM_SSH_USER" "$1"; }

# remote_run <host> <shell-command-string>
# Runs one command line on <host> via ssh. Prints it either way; actually
# executes only when DRY_RUN=0.
remote_run() {
  local host="$1" cmd="$2"
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] ssh $(ssh_target "$host") -- $cmd"
    return 0
  fi
  ssh "${DUCKSWARM_SSH_OPTS[@]}" "$(ssh_target "$host")" -- "$cmd"
}

# remote_script <host> <script-text>
# Pipes a multi-line bash script to <host> over ssh (bash -s). Used for
# the provisioning steps, which are too long to be a single command line.
remote_script() {
  local host="$1" script="$2"
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] ssh $(ssh_target "$host") -- bash -s <<'DUCKSWARM_SCRIPT'"
    log "$script"
    log "DUCKSWARM_SCRIPT"
    return 0
  fi
  printf '%s\n' "$script" | ssh "${DUCKSWARM_SSH_OPTS[@]}" "$(ssh_target "$host")" -- bash -s
}

# remote_read <host> <shell-command-string>
# Fetches output from <host>. Used to read robotd.toml before editing it
# locally, poll robot.health, and find the current release for rollback.
# Unlike remote_run/remote_script, this one is used to make *decisions*
# (what to edit, whether a poll succeeded) -- so under --dry-run it does
# NOT touch the network either (a fake, unreachable host must return
# instantly, never block on a connect timeout): it prints what it would
# have read and returns empty. Every caller is written to treat an empty
# dry-run read the same as "nothing there yet" -- always a valid real
# state (a first-ever provision, a robotd.toml with no [policy] section)
# -- so a preview run degrades to "here is the shape of what I'd do,
# without the real current values" instead of failing or hanging.
remote_read() {
  local host="$1" cmd="$2"
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] would read via: ssh $(ssh_target "$host") -- $cmd"
    printf ''
    return 0
  fi
  ssh "${DUCKSWARM_SSH_OPTS[@]}" "$(ssh_target "$host")" -- "$cmd"
}

# rsync_push <host> <local-src> <remote-dest> [extra rsync args...]
#
# Every destination path these scripts write to is either root-owned
# (the systemd unit, /etc/robot/robotd.toml) or duckswarm-owned with the
# ssh login user (default "radxa", see DUCKSWARM_SSH_USER) a third,
# unprivileged account -- so every push goes over as root via the
# "sudo rsync on the remote end" idiom, uniformly, rather than special-
# casing some destinations. Callers chown to duckswarm:duckswarm
# afterwards wherever the service (not root) needs to own the result.
# Requires passwordless sudo for the ssh user -- see docs/provisioning.md
# "What you need before you run any of this".
rsync_push() {
  local host="$1" src="$2" dest="$3"
  shift 3
  local -a extra=("$@")
  local rsh="ssh ${DUCKSWARM_SSH_OPTS[*]}"
  # "${extra[@]+"${extra[@]}"}" (not bare "${extra[@]}") because bash 3.2
  # (macOS's /bin/bash, still what `#!/usr/bin/env bash` resolves to on a
  # Mac with no Homebrew bash ahead of it in PATH) treats *any* expansion
  # of a zero-element array as an unbound variable under `set -u` --
  # verified against this exact bash. extra is legitimately empty on
  # calls with no trailing rsync args (e.g. pushing one file).
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] rsync -e \"$rsh\" --rsync-path='sudo rsync' ${extra[*]+"${extra[*]}"} $src $(ssh_target "$host"):$dest"
    return 0
  fi
  rsync -e "$rsh" --rsync-path='sudo rsync' "${extra[@]+"${extra[@]}"}" "$src" "$(ssh_target "$host"):$dest"
}

# roster_hosts <roster.json>
# Prints each unique `host` field from a SwarmLink roster file
# (SwarmLink/Sources/SwarmLink/Roster.swift: a JSON array of
# {"id","host","port","role"}), one per line, first-seen order. Runs
# locally against a local file -- safe under --dry-run, no network.
# Deliberately python3's stdlib json rather than jq: CLAUDE.md's
# stdlib-only discipline is about python/, but there's no reason for our
# own tooling to reach for a dependency (jq) that isn't guaranteed present
# on "any Mac" when the stdlib already does the job.
roster_hosts() {
  local roster="$1"
  # NOTE: today's only caller (deploy_shows.sh) checks looks_like_roster
  # first, so this guard is never actually exercised there. If you call
  # this from a `< <(roster_hosts ...)` process substitution directly,
  # know that die()'s exit only kills that subshell -- the reading loop
  # never sees it. Check the file yourself first, same as deploy_shows.sh.
  [ -f "$roster" ] || die "roster file not found: $roster"
  python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    entries = json.load(f)
seen = []
for e in entries:
    h = e.get("host")
    if h and h not in seen:
        seen.append(h)
for h in seen:
    print(h)
' "$roster"
}

# looks_like_roster <path> -- a roster is a JSON *file*; a bare host/IP
# never parses as one, so this is an unambiguous dispatch: try to parse,
# and require the result to be a JSON array (not e.g. a .duckshow.json,
# which is a JSON object) before calling it a roster.
looks_like_roster() {
  local path="$1"
  [ -f "$path" ] || return 1
  python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(data, list) else 1)
' "$path"
}

# toml_set_policy_slot <slot> <value>
# Reads a robotd.toml on stdin, writes it on stdout with [policy]
# <slot> = "<value>" added (creating the [policy] section if it isn't
# there) or updated in place -- every other line preserved byte-for-byte.
# /etc/robot/robotd.toml is per-board config "written once by the
# installer and never overwritten by an update" (docs/provisioning.md);
# this is why the edit is a targeted key-set, never a file replacement.
# A pure stdin/stdout transform (rather than editing in place over ssh)
# is what lets push_policy.sh's --selftest exercise this exact function
# with zero network access, and lets the real path preview the diff
# before installing anything.
toml_set_policy_slot() {
  local slot="$1" val="$2"
  # Blank lines inside [policy] are buffered (not printed immediately) so
  # a section that ends "last-key, blank line(s), [next-section]" gets an
  # appended key placed right after the last real key -- not after the
  # trailing blank, which would visually (though not semantically) look
  # like it landed in the wrong section. See push_policy.sh --selftest.
  awk -v slot="$slot" -v val="$val" '
    function flush_blanks() { for (i = 0; i < blanks; i++) print ""; blanks = 0 }
    BEGIN { in_policy = 0; found = 0; saw_section = 0; blanks = 0 }
    /^\[policy\][ \t]*$/ { saw_section = 1; in_policy = 1; print; next }
    /^\[/ {
      if (in_policy) {
        if (!found) { printf "%s = \"%s\"\n", slot, val; found = 1 }
        flush_blanks()
      }
      in_policy = 0
      print
      next
    }
    {
      if (in_policy) {
        if ($0 ~ /^[ \t]*$/) { blanks++; next }
        key = $0
        sub(/[ \t]*=.*/, "", key)
        gsub(/^[ \t]+|[ \t]+$/, "", key)
        if (key == slot) {
          flush_blanks()
          printf "%s = \"%s\"\n", slot, val
          found = 1
          next
        }
        flush_blanks()
        print
        next
      }
      print
    }
    END {
      if (in_policy) {
        if (!found) { printf "%s = \"%s\"\n", slot, val; found = 1 }
        flush_blanks()
      }
      if (!saw_section) { print "[policy]"; printf "%s = \"%s\"\n", slot, val; found = 1 }
    }
  '
}

# -- misc --------------------------------------------------------------

# require_cmd <name> -- fail early with a clear message instead of a
# cryptic "command not found" three steps into a remote script.
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required local command not found: $1"
}

# sanitize_basename <path> -- the only untrusted string that ends up
# inside a TOML value or a remote path in these scripts is the .onnx
# filename an operator passes to push_policy.sh; keep it to a safe
# charset so it can never break the TOML edit or the remote path it's
# placed at.
sanitize_basename() {
  local base
  base="$(basename -- "$1")"
  case "$base" in
    *[!A-Za-z0-9._-]*|"")
      die "refusing unsafe filename: '$base' (letters, digits, '.', '_', '-' only)"
      ;;
  esac
  printf '%s' "$base"
}
