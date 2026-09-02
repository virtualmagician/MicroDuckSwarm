# Stage viewer — version 1

A 3D stage view inside the timeline editor: scrub the playhead and watch the whole cast perform. It answers the question the timeline cannot — *does this bit read from the house?* — months before any hardware exists.

Three modules, rendered into a canvas the editor already owns: `editor/duckshow-viewer.js` (pure, no DOM/GL — pose derivation, dead reckoning, palette, camera easing, and the `StageViewer` controller that binds them to a canvas), `editor/viewer-gl.js` (the raw-WebGL2 renderer — matrix maths, primitive meshes, `StageRenderer`), and `editor/viewer-duck.js` (the duck built from those primitives). It consumes the same sampler output as everything else, so what you see is what the ducks will be told to do.

## Non-negotiables

1. **No dependencies, no CDN, no build step.** Raw WebGL2 and our own maths. The editor must open at a venue with no internet — a viewer that needs a CDN is a viewer that dies backstage. This rules out three.js; write the small amount of matrix/quaternion code we need.
2. **No Pollen assets.** `microduck_rl`'s meshes and hardware design files are **CC BY-SA-NC (non-commercial)**, this repo is public and MIT, and the shows are paid work. Model our own stylized duck from primitives. Never vendor their mesh, and don't fetch it at runtime either.
3. **Pose in, pixels out.** The renderer takes a plain array of per-duck poses (`{role, x, y, heading, headYaw, headPitch, headRoll, neckPitch, bodyZ, bodyRoll, bodyPitch, mouthOpen, walkPhase, resting}`) and draws them. `resting` (optional bool: is this duck at rest right now) exists because `walkPhase` is an unbounded accumulating angle — a renderer can't safely tell "moving" from "scrubbed elsewhere and back" by diffing it alone. It never reads a `.duckshow` file, never touches the sampler, never knows about time. This is what lets a MuJoCo-driven pose stream replace the kinematic one later without touching the renderer — one that has no notion of `resting` simply omits it.
4. **60 fps with ten ducks** on a MacBook, and it must not spin the GPU when the playhead is parked (render on change, not on a permanent rAF loop).

## Art direction

The reference is a real stage with the house dark, not a CAD viewport or a game engine demo. Restraint over spectacle: the ducks are the subject, everything else recedes.

**Ground.** A dark stage floor that fades to black at the edges via a soft radial falloff, so the stage feels like a lit pool rather than a floating rectangle. A faint grid, one line per half metre, dim enough to read as texture rather than as a chart — it exists to give scale and parallax, not to be looked at. Start marks sit on the floor as thin rings in each role's colour.

**Light.** One warm key from front-left and high, a cooler dimmer fill from the right, and a subtle rim from behind to separate the ducks from the dark. Lambert plus a low-exponent specular is plenty; skip PBR. Every duck gets a soft elliptical blob shadow on the floor, darkest directly under the body and falling off quickly — nothing sells "standing on a stage" more cheaply, and it makes vertical motion (crouch, sit) legible.

**The duck.** Charming, unmistakably a duck, never cartoonish or cute-ugly. Built from smooth primitives: an egg-shaped body a touch taller than wide; a short neck; a rounded head noticeably smaller than the body; a flat wide beak that opens on `mouthOpen`; two thin legs with a visible knee, feet as small flat ellipses; a stub tail. Proportions from the real thing — 25 cm tall, 14 cm wide — so blocking distances are honest. Give it a slight forward lean at rest, the way a real biped stands. Smooth-shaded, with a soft matte body and a slightly glossier beak and eyes so the face catches light and the audience's eye goes where it should.

**Colour.** The floor and background are near-black with a faint warm bias, never pure `#000`. Each role gets one saturated hue used sparingly — the body stays a warm off-white/cream (MicroDuck ships in Cream, Graphite, Lavender, Sky, so cream is honest) and the role colour appears as a band, the start-mark ring, and the motion trail. Ten roles need ten distinguishable hues that survive a dark ground: walk the hue circle at even spacing, keep saturation and lightness constant, and skip the muddy yellow-greens.

**Motion.** A walk cycle driven by `walkPhase`, which advances with speed — legs alternate, the body bobs slightly and rocks, the head counter-rotates a touch to stay level. It should read as a waddle, not a march. When velocity is zero the duck settles to a neutral stand rather than freezing mid-stride. Everything else (head, crouch, beak) comes straight from the pose.

**Trails.** Each duck's dead-reckoned path drawn as a line in its role colour on the floor, brightest near the current position and fading toward the start, so the eye reads direction without an arrow.

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
