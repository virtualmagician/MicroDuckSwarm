# Stage viewer — version 1

A 3D stage view inside the timeline editor: scrub the playhead and watch the whole cast perform. It answers the question the timeline cannot — *does this bit read from the house?* — months before any hardware exists.

Three modules, rendered into a canvas the editor already owns: `editor/duckshow-viewer.js` (pure, no DOM/GL — pose derivation, dead reckoning, palette, camera easing, and the `StageViewer` controller that binds them to a canvas), `editor/viewer-gl.js` (the raw-WebGL2 renderer — matrix maths, primitive meshes, `StageRenderer`), and `editor/viewer-duck.js` (the duck built from those primitives). It consumes the same sampler output as everything else, so what you see is what the ducks will be told to do.

## Non-negotiables

1. **No dependencies, no CDN, no build step.** Raw WebGL2 and our own maths. The editor must open at a venue with no internet — a viewer that needs a CDN is a viewer that dies backstage. This rules out three.js; write the small amount of matrix/quaternion code we need.
2. **No Pollen assets.** `microduck_rl`'s meshes and hardware design files are **CC BY-SA-NC (non-commercial)**, this repo is public and MIT, and the shows are paid work. Model our own stylized duck from primitives. Never vendor their mesh, and don't fetch it at runtime either.
3. **Pose in, pixels out.** The renderer takes a plain array of per-duck poses (`{role, x, y, heading, headYaw, headPitch, headRoll, neckPitch, bodyZ, bodyRoll, bodyPitch, mouthOpen, walkPhase, resting}`) and draws them. `resting` (optional bool: is this duck at rest right now) exists because `walkPhase` is an unbounded accumulating angle — a renderer can't safely tell "moving" from "scrubbed elsewhere and back" by diffing it alone. It never reads a `.duckshow` file, never touches the sampler, never knows about time. This is what lets a MuJoCo-driven pose stream replace the kinematic one later without touching the renderer — one that has no notion of `resting` simply omits it.
4. **60 fps with ten ducks** on a MacBook, and it must not spin the GPU when the playhead is parked (render on change, not on a permanent rAF loop).

## Layout

The editor is laid out like a video editor or game engine, not a document: **the 3D view sits centre-top and takes the majority of the window**, with the timeline in a panel below it and the inspector/validation to the side. The viewer is the primary surface — it is what you look at while working — and the timeline is the instrument you drive it with. The 3D panel resizes with the window and the split between viewer and timeline is draggable, with the position remembered per browser (localStorage, never in the show file).

## Art direction

A clean, well-lit workspace — a game-engine viewport or a photographer's cyclorama, not a darkened theatre. The earlier dark-stage treatment was honest about what an audience sees but made the ducks nearly invisible while working; authoring needs to *see*. Judging house readability is what the House camera and the real stage are for.

**Ground.** A light neutral grey floor, evenly lit, extending far enough to feel like a room rather than a platter, fading gently at the far edge so it does not end in a hard line. It is a measuring surface, so the grid is a real instrument, not texture:

- **1 metre major lines** — clearly visible, the reference you count in.
- **10 centimetre minor lines** — lighter, for reading spacing and duck-scale distances at a glance.
- Minor lines fade out as you dolly away so the floor never turns into moiré; major lines persist.
- The two stage axes through the origin are tinted (one warm, one cool) so orientation is unambiguous from any camera, the way a 3D tool marks X and Z.
- A duck is 25 cm tall and 14 cm wide, so it should read as roughly two and a half minor squares tall — that relationship is the whole point of the grid.

**Light.** Neutral studio lighting on a light ground: a soft key from high front-left, a fill from the right, and enough ambient that shells and shadowed sides stay readable. Contact shadows now matter more, not less — on a light floor a soft grounded shadow is what stops the ducks looking pasted on. Keep them soft and neutral grey, never black.

**The duck — model the real robot.** MicroDuck is a BDX-style walking robot, not a bathtub duck. Reference: Pollen's product photography and the MuJoCo model in their simulator (both in the scratchpad, for looking at only — never vendored; their meshes are CC BY-SA-NC).

- **Head:** the dominant feature and the thing that makes it recognisable. A **rounded rectangular shell** — a capsule or loaf lying on its side, wider and deeper than it is tall, in white/cream (or the role's accent colour). Not a sphere and not a dome.
- **Eye:** one **large circular camera lens on the front of the head**, nearly the height of the shell — a dark glossy disc with a visible concentric ring, set slightly proud. This single feature does most of the recognition work; make it big and get it crisp.
- **Bill:** a flat **yellow** band wrapping the underside and front edge of the head shell, projecting forward as a broad spatula. Yellow, not orange. `mouthOpen` hinges it down and reveals a lighter interior.
- **Neck:** a short dark articulated stack of small blocks joining head to body — mechanism, never a smooth organic neck.
- **Body:** small relative to the legs. A **light grey/silver servo block** as the main torso mass, with darker mechanical parts and small rounded light-grey panels on the hips. Reads as machined parts, not a soft mass.
- **Legs:** long, dark, visibly articulated — thigh and shin segments with a chunky silver joint block at the knee and hip. The legs carry most of the height.
- **Feet:** large flat **yellow** feet, oversized and slightly upturned at the front, with a darker sole. Along with the lens and bill, they carry the silhouette.
- **Colour split:** white/cream head shell, yellow bill and feet on every duck (that pairing is the product's identity — never recolour it per role), light grey body panels, dark mechanism. The **role hue tints the head shell and the hip panels only**, so the flock stays identifiable without losing the product's look.
- **Posture:** a forward-leaning crouch, knees bent, head carried out in front of the feet. It should look ready to move even when standing.

**Colour.** The floor and background are light neutral grey; the ducks are dark mechanism plus one accent, so they read as dark shapes on a light ground — the opposite of before, and much easier to see. Role hues must now be chosen for contrast against *light* grey: mid-to-deep saturated tones, not pastels. Bill and feet stay the signature **yellow** on every duck, since that pairing is the product's identity and it separates them from the role colour.

**Motion.** A walk cycle driven by `walkPhase`, which advances with speed — legs alternate, the body bobs slightly and rocks, the head counter-rotates a touch to stay level. A waddle, not a march. At rest the duck settles into its standing crouch rather than freezing mid-stride. Everything else (head, crouch, bill) comes straight from the pose.

**Trails.** Each duck's dead-reckoned path on the floor in its role colour, brightest near the current position and fading toward the start, so the eye reads direction without an arrow. Start marks are thin rings in the same colour — restrained; the ducks are the subject.

## Camera

Orbit with drag, dolly with scroll, always aimed at the stage centre. Three presets on keys, because the whole point is comparing what different eyes see:

- **House** (`1`) — the audience view. Eye height, front and centre, slight downward tilt. This is the default and the one that matters.
- **Three-quarter** (`2`) — raised and off to one side, for judging depth and spacing.
- **Top** (`3`) — straight down, for formations and marks. Effectively the old 2D view with better lighting.

Smoothly interpolate between presets rather than cutting; a half-second ease reads as intent and helps you keep your bearings.

## Editor integration

- Replaces the top-down canvas. Keep the draggable start marks: dragging on the floor plane in Top view moves a mark, exactly as before, still persisted under the file's `editor.marks`.
- Bound to the existing playhead — scrub, and the cast poses live. When the editor is playing (space), the viewer animates in step with the audio.
- Kinematic pose comes from the sampler: position by dead reckoning from locomotion, head and body angles direct, `mouthOpen` direct, `walkPhase` integrated from speed. A role with no locomotion track stands on its mark.
- Selecting a role in the timeline highlights that duck — a subtle lift in its rim light and a brighter trail, never a selection box.
- Skills that have no pose representation (`kick_left`, `sit_toggle`, `roulade`) show as a small floating label above the duck at the moment they fire, so events are visible without pretending we simulate them. Sounds show the same way, dimmer.

## What it is not

It does not simulate physics or run the RL policies, and it must never imply it does. A duck that walks cleanly here can still stumble on a raked stage. It is a staging and timing tool: spacing, facing, silhouette, whether a gesture is large enough to read from row fifteen. The label in the corner says **kinematic preview** so nobody mistakes it for a guarantee.

That is what **Create Preview** is for — see below.

## Create Preview (baked physics)

Authoring stays kinematic and instant. When a piece is ready, one button runs it through the real thing and **bakes the result to a pose cache** the normal viewer plays back. Physics is a render step, not a live mode.

```
.duckshow ──kinematic sampler──▶ intents ──▶ [MuJoCo + the shipped ONNX policies, one process per duck] ──▶ baked pose cache ──▶ the same renderer
```

- **Why baking, not streaming.** A physics sim cannot seek: to see 0:42 you must integrate from zero. Baking runs the simulation once, offline, and writes per-duck poses per frame; after that the timeline scrubs recorded data exactly as it scrubs kinematic poses. It also removes the 1x speed limit: a 15-DoF robot steps far faster than real time, and the ducks are independent (they share a floor, never each other), so the bake is embarrassingly parallel, one process or worker per duck. Estimated at low single-digit seconds of physics for the whole eight-duck, 64-second octet; see `docs/bake-parts.md` §3.2 for the working.
- **What it consumes.** The same intent stream the ducks get — `robot.move`, `robot.head`, `robot.pose`, `robot.mouth`, skills — so the preview is driven by exactly what will be sent to hardware, not a parallel description of it.
- **What it produces.** A cache keyed by show hash and policy versions, invalidated when either changes, plus a bake log of anything the physics refused to do.
- **The payoff: the diff.** The most valuable output is not the pretty render, it is the divergence between *intended* (kinematic) and *actual* (physics). Draw both paths on the floor and mark where they part: "lead is 38 cm left of its mark by 0:41", "wing fell during the roulade at 0:52". That directly measures the dead-reckoning drift this whole project is designed around, which nothing else we have can quantify before hardware exists.
- **What it cannot do.** The trained action space is 14 joints and excludes the mouth, so no bake will ever move the beak. `mouthOpen` keeps passing straight from the show's mouth track to the renderer exactly as the kinematic path does it.
- **Honesty.** A bake is evidence, not proof. Two gaps sit in front of the ordinary sim-to-real one. The policies were trained against the **BAM actuator model**, a voltage-control-law model of the XL330 with back-EMF, friction and randomised battery voltage; re-stepping the exported MJCF under MuJoCo's stock actuators drives the policy against a plant it was not quite trained for. And the exported MJCF carries no `<option>` element, so the bake driver must inject the timestep (0.005 s) and decimation (4) that produce the 50 Hz control rate, rather than inheriting them. Beyond that, a raked or carpeted stage is not the sim's flat plane. A bake raises confidence; it does not replace a rehearsal.

**Native first, not in-browser.** The research (`docs/bake-parts.md` §3.3) recommends a native helper — Python or Rust driving real MuJoCo and `onnxruntime` — over the in-browser WASM path this section originally assumed. It is faster, it avoids a ~31 MB first-run download and a per-worker WASM instantiate cost, and it makes the actuator-model gap above materially cheaper to close, since `microduck_rl`'s BAM implementation is Python that can be ported rather than reimplemented in JavaScript. The editor would invoke it and load the resulting pose cache.

**Dependencies.** Either path is heavy. A native baker needs pip packages, which is a real exception to this project's stdlib-only rule (`CLAUDE.md` #1) and not merely to the editor's no-CDN rule; an in-browser baker needs MuJoCo-WASM and `onnxruntime-web`. Either way the preview is a **separate, optional component**, never required on show night, so the editor and the duck-agent stay dependency-free and venue-proof while the preview is allowed to be expensive.

**Assets are supplied, never vendored.** Physics needs Pollen's MJCF model and meshes, which are **CC BY-SA-NC**; this repo is public MIT, and MIT permits commercial reuse downstream, so we cannot relabel their assets — that would hand every cloner an NC restriction without telling them. That is a repo-licensing fact, independent of how any particular person uses the tool.

So the assets are **user-supplied at runtime**, the way an emulator does not ship a BIOS:

- The preview looks for a local `assets/microduck/` directory (gitignored, never committed) holding the MJCF and meshes fetched from Pollen's own repositories by whoever is running the tool, under whatever licence applies to them.
- Present → the preview runs with the real model, and the editor may optionally render the real meshes too.
- Absent → the button explains where to get them in one line, and the kinematic viewer with our own primitive duck carries on exactly as before.

This keeps the repo cleanly MIT with no third-party assets in it, keeps the default experience dependency-free, and puts the licence question where it belongs: with the person who downloads the assets, for their own use. A note in the README should say plainly that MicroDuck's meshes and hardware design files are CC BY-SA-NC and are not redistributed here.

Worth doing anyway: ask Pollen whether they will grant explicit permission for an open-source authoring tool for their robot. It costs an email and would let us simplify all of the above.

See `docs/bake-parts.md` for the full parts list, exact asset-setup commands, and a browser-vs-native feasibility verdict with real numbers.

### The button, wired up

`scripts/edit.sh` runs `scripts/editor_server.py` in place of plain `python3 -m http.server` — a small **stdlib-only** local server (same discipline as everywhere else, `CLAUDE.md` #1) that serves the repo root exactly as before (nothing about loading a show or an asset changes) and adds three routes:

- `GET /api/capabilities` — whether baking is possible here at all: does `tools/bake/.venv/bin/python3` exist, does `assets/microduck/` carry both `mjcf/` and `policies/`, and which shows under `shows/` have a `.duckshow.json` to offer (`shows/fixtures/` excluded — those are `python/duckshow` validator test fixtures, several deliberately invalid, not authored shows). The editor fetches this once at boot and uses it to decide whether to offer the button at all.
- `POST /api/bake` — body `{"show": "/shows/octet/octet.duckshow.json"}`, a repo-root-relative path in the same form the editor's own `?show=` parameter already uses. Starts a bake in a background thread and returns a job id immediately.
- `GET /api/bake/<job id>` — progress (which role, what percent — read live off the baker's own per-role progress lines, not a fake spinner) and, once finished, the URL of the written cache plus a summary read back off the cache itself: role and frame counts, `unsimulated_roles`, `fallen_roles`.

**Security posture.** This server executes a subprocess on request, so it is scoped deliberately narrowly. It binds **127.0.0.1 only** — never `0.0.0.0` — and that is not a command-line flag; there is nothing for a caller to get wrong. The only thing an HTTP request can influence is *which show already in this repository* gets baked: the supplied path is resolved against the repo root and refused unless it stays inside the tree (no `..` escape, no symlink escape), ends in `.duckshow.json`, and exists as a file. The baker subprocess is always an explicit argv list — `tools/bake/.venv/bin/python3 bake_show.py <show> <out>` — never `shell=True`, never string-built; the interpreter, the script, and the output path are all fixed by the server, and a caller cannot choose any of them, nor any other flag. Caches are written to a gitignored `bakes/` directory at the repo root, one file per job — never into `shows/`, which is authored content, not build output.

**In the editor.** A **Create Preview** button sits in the stage header next to **Play Baked…**. It is disabled with a one-line reason — driven by `/api/capabilities` plus the loaded show's own state — whenever baking isn't possible right now: opened from `file://`, served by a plain `http.server`, no venv, no `assets/microduck/`, or the loaded show has no known path on the server (picked via **Open…** or dragged in — the browser's File API never exposes a filesystem path a request could use; **Load demo**, `?show=`, and `scripts/edit.sh`'s own show argument all do carry one). It is never a dead button with no explanation. Press it and the loaded show bakes in place: the button's own label and the status line track real per-role progress (`baking lead (3/8) 40%…`, not a spinner), and on success the fresh cache loads automatically and the viewer switches to **BAKED PHYSICS** — no file picker involved. The bake log (`docs/bake-format.md`'s `log[]`: `skill_unsimulated`, `mode_unsimulated`, `fell`) is the most useful part of a bake and is not left for the one-line, truncating status bar to bury: whenever a loaded cache carries any log entries — whether it arrived via **Create Preview** or via opening a `.duckbake.json` directly through **Play Baked…** — a small panel over the stage lists them.

**Unsaved edits bake fine, because the editor sends the show it has.** This used to refuse outright ("save your edits first"), on the reasoning that a bake reads the show from disk and an edited-but-unsaved document would produce a cache whose `show.sha256` could never be honestly checked against what is on screen. That reasoning was right about the hash and wrong about the fix. The browser cannot write the file back (**Save** downloads a copy), so the loop never closed: every edit made the button unusable until the author manually moved a download over the original. Setup mode made that constant, since placing the cast is an ordinary edit.

**Create Preview** now POSTs the current document as `show_text` and the server bakes exactly those bytes, writing them to a scratch file under `bakes/` at a path it chooses itself (so no request can steer where anything is written). The editor sets `state.showText` to the same bytes, so the returned cache hash-checks against what was actually baked, which is the honest version of the original guarantee rather than a weaker one. `state.dirty` is untouched: the show is still unsaved, and still says so.

The path form (`{"show": "/shows/…"}`) still works and is what a script would use. Baking a document the browser only holds in memory is why the size cap on the request body is 4 MB rather than the 1 MB a path needs.

**Everything else still works.** The terminal route — `cd tools/bake && .venv/bin/python3 bake_show.py <show> <out>` — is unchanged and always available; the button is a convenience over it, not a replacement. Serve the repo any other way — plain `python3 -m http.server`, a different static server, or open the HTML from `file://` — and the editor is the plain static page it always was: the kinematic preview and **Play Baked…** work precisely as before, `/api/capabilities` simply isn't there to answer, and **Create Preview** explains that plainly on the button itself instead of failing silently. **One caveat that is not cosmetic:** every other static server allows caching, and the editor is separately-fetched ES modules with no bundler and no content hashes in their URLs, so a browser can pair a fresh `duckshow-editor.html` with a stale cached `duckshow-core.js`. That has happened, and it presents as "the whole editor is broken" rather than as one stale file. `editor_server.py` sends `no-store` precisely to prevent it; `duckshow-core.js` exports `MODULE_API`, asserted at boot, to catch it when some other server does not.

## Tests

`node --test editor/tests` — pure logic only, no GL context: pose derivation from a show (dead reckoning matches `duckshow-core.js`'s existing `integrate`, head/pose/mouth pass through, `walkPhase` advances with speed and holds when stopped), the role-colour palette (ten distinguishable hues, deterministic per role, stable across reorderings), camera preset interpolation, and the matrix maths (multiply, invert, perspective, lookAt) against hand-computed values. Rendering itself is verified by eye against the art direction above. `editor/create-preview.js`'s own suite covers the Create Preview button's decision logic the same way `bake-cache.js`/`asset-probe.js` are covered — show-path shape checking, the enabled/disabled-with-reason logic against fake capabilities, and the job-status state machine's progress/summary/error text — none of it touching `scripts/editor_server.py` or a real subprocess; that server-side half is exercised by hand (`curl`, a browser) rather than an automated Python suite, since the project has no Python test runner wired up for one-off dev tooling outside `python/`'s own `unittest` gate.
