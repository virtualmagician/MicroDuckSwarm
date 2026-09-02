# duckshow editor

A single-file timeline editor for `.duckshow.json` choreographies (spec: `docs/authoring.md` §3, format: `docs/duckshow-format.md`). No build step, no CDN, no npm packages.

| File | What |
|---|---|
| `duckshow-core.js` | Pure ES module, no DOM: parse/serialize (unknown fields preserved), sampler and validator with the **same semantics, limits, issue order and message text** as `python/duckshow` (the canonical implementation), beat-grid helpers, dead reckoning, and pure edit operations. |
| `duckshow-viewer.js` | Pure ES module, no DOM/GL: turns a show + a moment in show time into pose/trail/event-label data for the stage viewer (spec: `docs/viewer.md`) — dead-reckoned pose derivation (reuses `duckshow-core.js`'s `integrate`, never reimplements it), the per-role colour palette, and start-mark resolution. |
| `viewer-gl.js` / `viewer-duck.js` | The stage viewer's renderer: raw WebGL2, no dependencies. `viewer-gl.js` is `StageRenderer` (camera, ground, trails, shadows, marks — `setCast`/`setMarks`/`setTrails`/`setSelected`/`setCamera`/`draw`/`pick`) plus the from-scratch matrix maths and primitive mesh generators; `viewer-duck.js` builds the duck itself from those primitives (never Pollen's meshes — see `docs/viewer.md` item 2). |
| `duckshow-editor.html` | The editor UI. Imports the four modules above with `<script type="module">`. |
| `tests/*.test.mjs` | Node's built-in test runner. Sampler parity with the numbers `python/tests/test_sampler.py` asserts, validator parity with `shows/fixtures/*.duckshow.json` + `expected.json` (the same gate `DuckShowFixtureTests.swift` runs), round-trip preservation, dead reckoning, every edit op, stage-viewer pose/palette/camera-easing logic, and the WebGL2 matrix/mesh maths. |

## Opening the editor

**Served (recommended — this is what makes "Load demo" work):**

```sh
cd <repo root>
python3 -m http.server 8000
# then open http://localhost:8000/editor/duckshow-editor.html
```

The page fetches `../shows/demo/demo.duckshow.json` on startup and via the **Load demo** button.

**Double-click the HTML (file://):** works in Firefox. Chrome and Safari refuse ES-module imports from `file://` (the page then shows a banner explaining this) — serve it as above. When opened from `file://` the demo cannot be fetched either; use **Open…** or drag a `.duckshow.json` onto the window. A built-in starter show is loaded so the UI is never empty.

**Saving:** browsers cannot write files in place. **Save** downloads a copy to your downloads folder (Chromium-based browsers offer a native save dialog that writes in place; if you cancel it, nothing is written). Move the file into `shows/` yourself.

## Tests

```sh
node --test editor/tests                  # from the repo root (Node ≥ 20) — what CI runs
node --test 'editor/tests/*.test.mjs'     # equivalent glob form
cd editor && node --test                  # or: default discovery from inside editor/
```

Node 20 treats the directory argument as a place to search for `*.test.mjs`; Node 21+ treats it as a glob and runs whatever it matches as a single test file, which is why `tests/index.js` (with `tests/package.json` marking the directory as ES modules) exists: it imports every suite, so the same command works on every Node version. Add new suites to that import list. Tests read the fixtures and demo from `../shows/` by relative path — run them from a checkout.

## The UI

- **Header fields** — name, author, duration, bpm, beat_offset, music file (`meta.*`). Duration is required and drives the end-of-show shading and the beat grid.
- **Lanes** — one group per role (collapse with ▾, remove with ✕, add with **+ role**), with sub-lanes `locomotion` (vx/vy/vyaw), `head` (neck_pitch/head_pitch/head_yaw/head_roll), `pose` (z/roll/pitch + `active` band), `mouth` (open), and `events`. Each curve lane spans exactly the validation limits of its fields, so anything touching the lane edge is at the limit. Curves are drawn by sampling the same sampler the duck uses (step / linear / smooth), so what you see is what plays. The small glyph under a keyframe shows its interp (`/` linear, `~` smooth, `⌐` step).
- **Beat grid** — drawn from bpm / beat_offset (bars emphasised, assuming 4 beats per bar for display only); **grid** picks beat, ½ or ¼; **snap** snaps drags, clicks and arrow nudges to it.
- **Validation** — re-runs on every change with the rules `python/duckshow` applies at LOAD; click an issue to jump to it (playhead, lane, selection). Keyframes and events with issues get a red (error) or yellow (warning) ring.
- **Stage** — a real-time 3D kinematic preview (raw WebGL2, no dependencies; spec: `docs/viewer.md`), bound live to the playhead: scrub and the whole cast poses in step, press play and it animates in sync. It renders on change only — parked, it burns no GPU. Camera: `1` house (audience, default) / `2` three-quarter / `3` top, each a half-second eased transition; drag to orbit, scroll to dolly, always aimed at stage centre. Start marks are only draggable in **top** view (floor-plane pick, exactly the old top-down behaviour) and stay persisted under the top-level `"editor"` field (see below). Click a role's name in the lane header (or its swatch in the legend below the stage) to highlight that duck — a lifted rim light and a brighter trail, no selection box. Skills and sounds with no pose (`kick_left`, `sit_toggle`, …) surface as a small floating label above the duck when they fire. The **kinematic preview** label in the corner is a standing reminder: this stages spacing, facing and timing — it does not simulate physics or run the RL policies, and a duck that walks cleanly here can still stumble on a raked stage.
- **Audio** — ♪ audio… loads any browser-decodable file and plays it in sync with the playhead (Web Audio). No beat detection in v1: type the bpm. **use as music file** copies the file name into `meta.music.file`.

### Mouse

| Action | Effect |
|---|---|
| click | select keyframe (per field) / event; click empty space to scrub |
| drag keyframe | move in time and value; **⇧** time only, **⌥** value only |
| drag event | move in time |
| double-click / ⌘-click empty lane | add a keyframe (values sampled from the curve at that time) or an event (`sound: chirp`) |
| double-click keyframe | cycle interp (on the `active` dot: toggle it) |
| drag in the ruler | scrub |
| ⌘/Ctrl + wheel | zoom around the cursor |
| ⇧ + wheel, horizontal wheel | scroll time |
| drag on the stage | orbit the camera |
| scroll on the stage | dolly the camera |
| drag a start mark (top view only) | move it; ⌥/⇧-drag to turn it |

### Keyboard

| Key | Effect |
|---|---|
| `Space` | play / pause |
| `Home` / `End` | playhead to start / end |
| `←` `→` | nudge the selection by one frame (0.02 s), or one grid step when snap is on; `⇧` ×10. With nothing selected: move the playhead (`⇧`: 1 s) |
| `↑` `↓` | nudge the selected field's value by 1 % of its range (`⇧`: 10 %); toggles `active` |
| `,` `.` | step the playhead one frame back / forward |
| `Delete` / `Backspace` | delete the selection |
| `I` | cycle interp: linear → smooth → step |
| `S` | toggle snap |
| `+` `−` `0` | zoom in / out / fit |
| `1` `2` `3` | stage camera: house / three-quarter / top |
| `⌘Z` / `⇧⌘Z` (`Ctrl` on other platforms), `⌘Y` | undo / redo |
| `⌘S`, `⌘O` | save, open |
| `Esc` | clear selection (or leave a text field) |

## The `"editor"` field

The editor keeps its own state in a top-level `"editor"` object in the show file — today only the stage start marks:

```json
"editor": { "marks": { "lead": { "x": 0.0, "y": 0.4, "heading": 0.0 }, "wing": { "x": 0.0, "y": -0.4, "heading": 0.0 } } }
```

Every loader (`python/duckshow`, SwarmLink, the duck-agent) ignores `"editor"` like any other unknown field, and the editor preserves any unknown fields it finds anywhere in a file on round-trip (tested in `tests/roundtrip.test.mjs`). Marks are in metres, heading in radians counter-clockwise, in the stage frame the editor draws — they do not move a duck; they only place the dead-reckoned preview.

## Using the core module elsewhere

```js
import { parseShow, validate, createSampler, integrate, serializeShow } from './duckshow-core.js';
const show = parseShow(text);              // the JSON document itself, shape-checked
validate(show);                            // [{severity, role, track, t, message}] — identical to python/duckshow
createSampler(show, 'lead').at(3.25);      // {t, locomotion, head, pose, mouth}
integrate(show, 'lead');                   // [{t, x, y, heading}] every 0.02 s
serializeShow(show);                       // JSON text, unknown fields intact, trailing newline
```

In the browser, `window.duckshowEditor.state.show` is the live document (handy in devtools), and `window.duckshowEditor.core` is the module.

All edit operations (`addKeyframe`, `moveKeyframe`, `deleteKeyframe`, `setInterp`, `addEvent`, `updateEvent`, `setEventAction`, `deleteEvent`, `setMeta`, `addRole`, `removeRole`, `setMark`) are pure and return a new show object; the input is never mutated, which is what makes undo/redo in the editor a plain stack of documents.
