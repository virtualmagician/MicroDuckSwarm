# Authoring — puppeteering, recording, and the timeline editor (v1)

Decision (2026-09-01): build the record path and a minimal timeline UI in parallel. Puppeteer a duck with a gamepad, capture the intent stream as a role's tracks, layer role by role, then polish on a beat-gridded timeline. This is the Disney BDX workflow applied to intent curves.

Three pieces, each independently useful:

| Piece | Where | What |
|---|---|---|
| Puppet channel | `docs/swarmlink-protocol.md` §6, `python/duck_agent` | Live intents to one duck, and the nudge layer during playback |
| Recorder | `swarmctl record` (SwarmLink) | Gamepad → puppet stream → `.duckshow` tracks for one role, while the rest of the cast plays back |
| Editor | `editor/duckshow-editor.html` + `editor/duckshow-core.js` | Single-file, zero-dependency timeline editor with beat grid, validation, and a top-down preview |

## 1 · Puppet channel

See `docs/swarmlink-protocol.md` §6 for the wire message. Agent semantics:

- **Puppet mode** (IDLE/LOADED): a fresh puppet packet drives the duck directly — `move`/`head`/`pose`/`mouth` are forwarded as the corresponding `robot.*` notifications at the 50 Hz tick; `do`/`sound` fire once per packet `seq`.
- **Nudge layer** (PLAYING): puppet `move` is *added* to the timeline's locomotion (vector sum, clamped to the validation limits); puppet `head`/`pose`/`mouth` *override* the timeline's values while packets are fresh; `do`/`sound` fire immediately.
- **Deadman:** a packet is fresh for 250 ms. When the stream goes stale, puppet influence drops to zero — locomotion is zeroed at once in puppet mode; while PLAYING the timeline simply resumes ownership. Panic wins over everything, always.
- Puppet packets are never ACKed and never retried; stale `seq` is dropped. Telemetry reports `"puppet": true` while packets are fresh.
- **Freshness is per channel.** A packet asserts only the channels it carries, so a channel is fresh for 250 ms after the last packet carrying *it*. An operator releases a head override by simply no longer asserting `head`, while still streaming `move`.
- **Panic and stop mute the puppet.** After either, incoming puppet packets are dropped until the stream has been quiet for one deadman period. Without this a sender still streaming at 50 Hz re-drives the duck 20 ms after a panic, which would break the invariant that panic always wins. A consequence: `stop` in IDLE/LOADED now also zeroes locomotion and sends `robot.stop` when the puppet was driving. A recorder must therefore stop streaming on panic or stop.
- **`seq` tracking resets after 2 s of silence** so a restarted sender counting from 1 is not locked out; the deadman covers any packet that late anyway.
- **ARMED ignores puppet input** — a duck waiting for its start time holds still. A puppet-driven duck that receives `play` gets one zero move as it arms.

## 2 · Recorder — `swarmctl record`

```
swarmctl record --roster roster.json --duck duck-01 --role lead --out shows/mine/mine.duckshow.json
                [--show shows/mine/mine.duckshow.json] [--shows-dir shows/] [--bpm 120 --beat-offset 0 --duration 30]
                [--input gamepad|script:<file.json>] [--map default] [--lead 3.0]
```

- **Input:** `gamepad` reads the first connected controller via GameController.framework (macOS, zero deps). `script:<file>` replays a JSON list of timed input frames (`[{"t": 0.0, "lx": 0.0, "ly": 0.5, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "buttons": ["a"]}, …]`; a frame holds until the next one, and the take ends at the `t` of the frame that presses `options` / `"stop": true` or, if the script runs out first, of its last frame) — this is what tests and CI use, and it makes recordings reproducible.
- **Default map** (`--map default`): left stick → `vx` (forward = up) / `vy`; right stick X → `vyaw`; right stick Y → `head_pitch`; D-pad left/right → `head_yaw` steps; left trigger → `pose.z` crouch (active while > 0); right trigger → `mouth.open`; A `chirp`, B `greet`, X `coo`, Y `wheee`; left shoulder `kick_left`, right shoulder `kick_right`; menu `sit_toggle`; options = stop recording. Stick values are scaled to the validation limits and dead-zoned at 0.08.
- **Layering:** with `--show`, the recorder writes a temporary copy of the show with the target role's tracks emptied into `--shows-dir` under a temp id, loads it to the whole roster, plays it with `--lead` seconds of countdown (printed 3‑2‑1), streams puppet to `--duck` from the play epoch, and records against show time. Without `--show`, recording starts at t=0 on a countdown and `meta.duration` becomes the recorded length (rounded up to the next beat when `--bpm` is given).
- **Capture → tracks:** the recorder samples its own puppet stream at 50 Hz and decimates: a keyframe is written when a value moves more than an epsilon (0.01 m/s, 0.01 rad, 0.02 mouth) or at least every 100 ms; `interp: "linear"`. Button presses become `events` (skills/sounds); presses closer than the 0.25 s event-spacing limit are dropped with a warning. `pose.active` is true while the crouch trigger is held.
- **Output:** the role's tracks in `--out` are replaced (other roles untouched; the file is created with a one-role cast if absent). The result is validated with the package validator and issues are printed; a show with errors is still written, marked in the log, so the editor can fix it.
- Temp show ids are removed from `--shows-dir` on exit, including on SIGINT.

## 3 · Editor — `editor/`

- `editor/duckshow-core.js`: pure ES module, no DOM — parse/serialize (unknown fields preserved on round-trip), the sampler (identical semantics to `python/duckshow/sampler.py`: step/linear/smooth, hold before/after, locomotion zeroed at duration), the validator (identical rules and limits to `python/duckshow/validator.py` — checked against `shows/fixtures/*.duckshow.json` + `expected.json`, the same parity gate the Swift validator uses), beat-grid helpers (`bpm`, `beat_offset` → beat times, snap), dead-reckoning (`integrate(role, dt=0.02)` → `[{t,x,y,heading}]`), and keyframe edit operations (add/move/delete/set-interp, event add/edit/delete) as pure functions returning new show objects.
- `editor/duckshow-editor.html`: one file, no build step, no CDN — imports the core module; Open/Save via `<input type=file>` and a download link; header fields (name, bpm, beat_offset, duration, music file name); one lane group per role with sub-lanes locomotion (vx/vy/vyaw), head (4), pose (z/roll/pitch + active), mouth, events; beat grid drawn from bpm/beat_offset with a snap toggle; zoom + horizontal scroll; playhead scrub with live value readout; keyframe drag in time and value, click-to-add, delete, interp cycling; event editing with dropdowns limited to the enums; a validation panel (errors/warnings with role/track/t, click to jump); a top-down stage canvas showing every role's dead-reckoned path with per-role start marks the user can drag (persisted under a top-level `"editor": {"marks": {role: {x, y, heading}}}` field, which every loader ignores). Optional music: load an audio file to play along with the playhead (Web Audio); no beat detection in v1 — type the BPM.
- Tests: `node --test editor/tests` (Node's built-in runner, no npm packages) covering sampler parity with hand-computed values, validator parity with the fixtures, round-trip preservation, dead-reckoning of a straight walk and a turn, and edit operations. The editor HTML is smoke-tested by loading the demo show in a headless check only where a browser is available (not required in CI).

## Decisions log

- 2026-09-02 — Puppet channel added to SwarmLink as the single live-intent path (works against mock and real ducks; doubles as the show-night nudge layer). Recorder input is abstracted so scripted recordings are reproducible in CI. Editor logic lives in a DOM-free module so the same validator/sampler rules are tested in a third implementation against the shared fixtures.
