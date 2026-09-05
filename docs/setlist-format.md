# `.duckset`: the setlist format

**Status: format and editor built (2026-09-05), setlist playback not.** The
editor authors and saves a setlist; no master runs one yet. What is missing is
listed under "What has to exist first".

A setlist is an ordered list of shows with an end behaviour on each. It is the
answer to "play this number, let me pick the ducks up and move them, play the
next one", which is how a demonstration inside a talk actually runs.

## Why a list of shows rather than chapters inside one show

The first design put chapter markers inside a single `.duckshow`, each with its
own starting positions. It does not work, for a reason that is structural
rather than fixable: **every duck ends the show locally**, at `meta.duration`
(`python/duck_agent/agent.py`, `_end_of_show`). The master fans out nothing at
the end: `publishStateTick` halts and sends no command, because every agent's
clock reaches duration on its own. A chapter boundary in the
middle of a show is therefore a *hold*, and holds are blocked on two open
problems in `docs/control-track.md` (blockers 4 and 8).

A boundary *between* shows has neither problem. It is the state the system is
already designed to sit in: every duck LOADED, transport `stopped`, waiting for
an operator cue. Nothing new happens on the wire.

Separate shows also give each chapter its starting positions for free. A show
already declares its own cast and its own `editor.marks`, so the editor's setup
mode already shows where the ducks have to be before that entry plays. Chapters
inside one file would have needed a second, parallel way to say the same thing.

## The document

```json
{
  "format": "duckset/1",
  "meta": { "name": "opening set", "notes": "8 ducks, house left" },
  "entries": [
    { "id": "e1", "show": "/shows/octet/octet.duckshow.json", "end": "hold" },
    { "id": "e2", "show": "/shows/demo/demo.duckshow.json", "end": "loop", "label": "vamp" },
    { "id": "e3", "show": "/shows/octet/octet.duckshow.json", "end": "continue" }
  ]
}
```

| field | required | meaning |
|---|---|---|
| `format` | yes | `duckset/1`. Loaders test the major for equality; a breaking change bumps it and gets a migration (CLAUDE.md rule 4). |
| `meta.name` | yes | What this set is called. |
| `meta.notes` | no | Free text for the operator. |
| `entries` | yes | Ordered. May be empty (a new, unfilled setlist is a valid document). |
| `entries[].id` | yes | Unique within the setlist, stable across reordering. |
| `entries[].show` | yes | Repo-root-relative, starts `/shows/`, ends `.duckshow.json`. |
| `entries[].end` | no | `hold` (default), `loop`, or `continue`. |
| `entries[].label` | no | Display name for the block. Defaults to the show's `meta.name`. |

Unknown fields are ignored, everywhere, like `.duckshow`.

**`show` is a file path, not a show id.** A setlist is an authoring artifact
that names files on the machine that edits it. The master turns a path into a
`load` by reading the id out of the file, which is what `swarmctl load <path>`
already does. Ids travel on the wire, paths are what the editor opens.

**`id` is not the show.** The same show can appear twice in a set (a reprise),
so entries need an identity of their own: one for the editor's drag ordering,
and one for an operator cue that names an entry rather than a position.

## End behaviours

| `end` | on reaching `meta.duration` |
|---|---|
| `hold` | Stop. Wait for an operator cue. This is what already happens today: the ducks end locally → LOADED, the master's transport → `stopped`. |
| `loop` | `play` the same show again from 0. The show is still LOADED on every duck, so this is one fan-out. Runs until the operator advances. |
| `continue` | `load` the next entry's show, then `play` it. Two fan-outs. |

`hold` is the default because the gap where the cast gets picked up and
repositioned is what this feature is for. `docs/swarmlink-protocol.md`
("Relax") is the other half of that gap: `swarmctl relax on`, move the ducks,
`swarmctl relax off`, cue the next entry.

### No end behaviour is gapless, and `continue` is the slowest

A `play` is scheduled at `at_master_time` with a lead so that a late datagram
still arrives before the start; the default lead is **1500 ms**
(`DEFAULT_LEAD_MS` in `python/tools/showmaster.py`). `continue` adds a `load`
fan-out in front of that, during which every duck re-hashes the show file,
parses it, runs the validator and builds a sampler.

So the seam between two entries is at least the play lead, and in practice
more. Two numbers that have to flow together musically belong in one
`.duckshow` with the transition authored in, rather than in two setlist
entries. The setlist is for the boundaries where a gap is wanted.

### A failed load stops the set

`SwarmMaster.play` refuses when any duck's most recent `load` did not succeed
(`docs/swarmlink-protocol.md`, "The master must not play over a failed load").
A `continue` whose load fails on one duck therefore stops the setlist rather
than playing the next entry to a short cast. A duck that NACKed a load still
holds the *previous* show and would accept a `play` naming it, so playing on
would split the cast.

## Validation

`python/duckset/` is the canonical loader and validator, same shape as
`python/duckshow/`. Errors (refuse to run):

- `format` missing or not `duckset/1`
- `entries` not a list, or an entry that is not an object
- an entry with no `id`, or a duplicate `id`
- `show` missing, not a string, not ending in `.duckshow.json`, or escaping the
  repo root
- `end` present and not one of `hold` / `loop` / `continue`

Warnings (run anyway, say so):

- a `show` path that does not exist on this machine. A setlist authored
  elsewhere is still a valid document, it just cannot be run here
- a referenced show that fails `duckshow.validate` with errors of its own
- the last entry ending in `continue`, which has nothing to continue to and
  behaves as `hold`
- an entry whose cast differs from the previous entry's, since the operator is
  about to be surprised by which ducks are needed

## Switching between the two editors

The show editor and the setlist editor are two static sibling pages under
`editor/`. A button in each topbar opens the other, and the contract between
them is three URL parameters. Anything that changes one of them changes both
pages together.

* `duckshow-editor.html?show=<repo path>` opens that show. It is also what
  makes `POST /api/save` write back in place instead of downloading a copy.
* `setlist.html?set=<repo path>` opens that setlist.
* `duckshow-editor.html?from=<setlist repo path>` names the setlist that opened
  the tab. The show editor uses it for the button's label and destination and
  for nothing else. It is refused unless it is under `/shows/setlists/` and
  ends `.duckset.json`, since that is the only directory `GET /api/setlists`
  lists and the only one `POST /api/save-setlist` accepts. A set outside it
  would open and then fail Save with a 400.

**Switching always opens a tab. It never navigates the tab you clicked in, and
it never navigates a tab that is already showing a page.** Neither page has a
document model: nothing outside its `state` object is serialised, there is no
autosave, and navigating away would take the undo stack, the decoded audio, an
open bake cache and the poll loop for any in-flight bake with it. So the
switcher opens a new tab, or brings forward the one it opened earlier if that
tab is still around. Each page keeps the handle its own `window.open` returned,
which is why this works only within one server origin. `scripts/edit.sh` picks
a free port per run, so two editors launched from two terminals are on two
origins and cannot see each other. That is the floor, and the status line says
which of the two things happened on every click.

**A switch never lands you on a document you did not ask for.** The setlist's
button refuses when the set has no entries, rather than opening a show editor
with no `?show=`: that path loads the demo show *with a writable repo path*
(`loadDemo` passes `DEMO_SHOW_PATH`), so one Save would rewrite
`shows/demo/demo.duckshow.json` for a user who thought they had a blank
document.

`⌥L` switches views from the keyboard in both pages. It is inert while a
timeline drag is live, because `endDrag` is the only thing that pushes the
pre-drag document onto the undo stack.

Both buttons ship `disabled` in the markup and are enabled by the script that
wires them. A page whose modules failed to load, which is what happens on
`file://` and on a `MODULE_API` mismatch, therefore shows a visibly disabled
button instead of a live-looking one that does nothing.

## Why this is painful in a browser

A browser cannot reliably write a file back. Chrome's File System Access API
works, but the handle does not survive a reload, and a show opened via `?show=`
never had one to begin with. That is why `POST /api/save` exists in
`scripts/editor_server.py`: writing through the local server is the only route
that works in every browser and survives a reload.

A setlist references many files, so moving between a set and the shows in it
is a normal part of the work. Two tabs handle that without either page being
unloaded, so an unsaved reorder and an unsaved cue both survive a switch. What
neither page gets from a browser is a document model: no undo across a reload,
no autosave, no file handle that survives relaunch, and no way for one tab to
know that the other is holding unsaved edits to a show it is drawing a block
for. The setlist refetches `GET /api/shows` whenever its tab is brought
forward, so a saved change to a show's duration or cast reaches the block that
draws it, but an unsaved one cannot.

**The native app is parked.** What follows records what a document model would
buy and is still true. It is not being built. The two-tab switcher above is the
answer being maintained.

**What a native macOS app buys.** A real document model: open / save / save-as
through the system panel, security-scoped bookmarks that survive relaunch,
undo through `NSUndoManager`, autosave and versions, a window per document,
and drag-and-drop of `.duckshow` files from Finder into a setlist. The master
also moves in-process: SwarmLink is already a Swift 6 package with no
third-party dependencies, so an app can `import SwarmLink` and drive the ducks
directly instead of shelling out to `swarmctl`.

**What is already native-ready.** SwarmLink itself, and the `.duckshow` parser
and validator in Swift, which are held to byte-parity with the Python canonical
implementation by `DuckShowParityTests`. The gap is the editor UI, which is JS.

**Three routes, cheapest first.**

1. **WKWebView shell around today's editor.** Replace `fetch('/api/save')` with
   a `WKScriptMessageHandler` that writes through the app's `NSDocument`.
   Keeps one UI and one renderer, and buys the entire document model. It is
   reversible, because the page still runs unchanged under `edit.sh`.
2. **SwiftUI chrome, WKWebView for the 3D view only.** Native setlist and
   timeline, web canvas for the viewer. Middle cost. The seam is a message
   protocol between the two halves, which has to be designed and then kept.
3. **Fully native (SwiftUI + SceneKit/Metal).** Best result, highest cost, and
   it makes the duck renderer exist a third time, after `editor/viewer-gl.js`
   and `tools/bake`'s MuJoCo scene. Three implementations of the *format* are
   checked against each other by the parity tests. Three implementations of a
   *renderer* would only be three things to keep in sync by hand.

Route 1 removes the save-state problem without a rewrite, which is why it goes
first.

**What a native app does not solve.** Baking needs MuJoCo, which is a pip
environment (`tools/bake/`, the documented exception to the stdlib-only rule).
That cannot go inside a sandboxed App Store app, so the bake stays a local
helper process, which is what `editor_server.py` already is.

## What has to exist first

The format and the editor are built. Running a setlist is not, and needs:

1. **A setlist runner in a master.** `swarmctl setlist <file>` and a
   `showmaster.py` equivalent: hold at each `hold`, re-`play` at each `loop`,
   `load`+`play` at each `continue`, and stop the set on a failed load.
2. **An operator cue to advance.** `/duckswarm/go` already means play, so a
   setlist needs "advance to the next entry" as a distinct verb, or `go` has to
   become setlist-aware. This is the same argument that made `resume` a
   separate verb from `go` (`docs/control-track.md`, blocker 1).
3. **A Swift loader**, if the runner lives in SwarmLink, held to parity with
   `python/duckset/` the way `DuckShow.swift` is.
