#!/bin/bash
# The .app's executable. It hands the real work to Terminal, so the server
# gets a window with its log and a Ctrl+C, then exits at once so the Dock icon
# does not sit bouncing for a process that is not this one.
#
# Built into DuckSwarm Editor.app/Contents/MacOS/launch by
# scripts/make_launcher_app.sh. Terminal runs a .command file it is handed.
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
exec open -a Terminal "$CONTENTS/Resources/run.command"
