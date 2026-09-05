#!/bin/bash
# DuckSwarm Editor: runs scripts/edit.sh in this Terminal window.
# Close the window or press Ctrl+C to stop the server.
#
# Built by scripts/make_launcher_app.sh, which bakes the repository path in.
# Two optional one-line files under ~/.duckswarm override it without a
# rebuild:
#   editor-repo   the repository path, if it moved
#   editor-args   what to open, e.g. "shows/octet shows/setlists/example.duckset.json"
REPO="@REPO@"
[ -f "$HOME/.duckswarm/editor-repo" ] && REPO="$(head -1 "$HOME/.duckswarm/editor-repo")"
ARGS="shows/octet"
[ -f "$HOME/.duckswarm/editor-args" ] && ARGS="$(head -1 "$HOME/.duckswarm/editor-args")"

if [ ! -f "$REPO/scripts/edit.sh" ]; then
  osascript -e "display dialog \"DuckSwarm Editor cannot find the repository at:\" & return & \"$REPO\" & return & return & \"Rebuild the launcher with scripts/make_launcher_app.sh, or write the path into ~/.duckswarm/editor-repo.\" buttons {\"OK\"} default button 1 with title \"DuckSwarm Editor\" with icon stop" >/dev/null 2>&1
  echo "DuckSwarm Editor: repository not found at $REPO" >&2
  exit 1
fi

printf '\033]0;DuckSwarm Editor\007'
cd "$REPO" || exit 1
# shellcheck disable=SC2086 -- ARGS is a space-separated argument list by design
exec ./scripts/edit.sh $ARGS
