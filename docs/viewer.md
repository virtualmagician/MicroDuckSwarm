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

**Colour.** The floor and background are light neutral grey; the ducks are dark mechanism plus one accent, so they read as dark shapes on a light ground — the opposite of before, and much easier to see. Role hues must now be chosen for contrast against *light* grey: mid-to-deep saturated tones, not pastels. Bill and feet stay the signature orange on every duck, since that is the product's identity and it separates them from the role colour.

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

Real-policy preview stays a later option: MuJoCo compiled to WASM plus `onnxruntime-web` driving one hero duck, feeding this same pose interface. Nothing here should make that harder.

## Tests

`node --test editor/tests` — pure logic only, no GL context: pose derivation from a show (dead reckoning matches `duckshow-core.js`'s existing `integrate`, head/pose/mouth pass through, `walkPhase` advances with speed and holds when stopped), the role-colour palette (ten distinguishable hues, deterministic per role, stable across reorderings), camera preset interpolation, and the matrix maths (multiply, invert, perspective, lookAt) against hand-computed values. Rendering itself is verified by eye against the art direction above.
