# The .duckshow format — version 1

A `.duckshow` file is one JSON document describing a complete choreography for a cast of ducks. It is authored once, validated, distributed to every duck before the show, and played back locally by each duck-agent. Design goals, in order: **safe** (validation catches anything that could hurt a duck or the show), **diff-friendly** (named fields, stable ordering), **portable** (shows reference cast *roles*, never physical ducks).

File extension: `.duckshow.json`. Encoding: UTF-8.

## Top level

```json
{
  "format": "duckshow/1",
  "meta": {
    "name": "Demo Waddle",
    "author": "Marco Tempest",
    "created": "2026-09-01",
    "duration": 30.0,
    "music": { "file": "demo.wav", "bpm": 120.0, "beat_offset": 0.0 }
  },
  "requires": {
    "policies": []
  },
  "cast": [
    { "role": "lead",  "notes": "front center mark" },
    { "role": "left",  "notes": "stage left mark" }
  ],
  "tracks": {
    "lead": { ... },
    "left": { ... }
  }
}
```

- `format` — exactly `"duckshow/1"`. Parsers reject unknown major versions; unknown *fields* anywhere are ignored (forward compatibility, same discipline as StageWizard show files).
- `meta.duration` — **required**, finite, > 0. Seconds; playback ends here regardless of track contents (locomotion is zeroed and `robot.stop` is sent). Validators reject a missing, null, non-finite, or non-positive duration.
- `meta.music` — optional; `bpm` + `beat_offset` (seconds to first downbeat) define the beat grid editors snap to. The music itself plays from the show master (StageWizard), never from this file.
- `cast` — ordered list of roles. Physical assignment (role → duck hostname) lives in the SwarmLink roster, not in the show.
- `tracks` — one entry per role; every role in `cast` must have a track entry (may be empty `{}` = duck stands idle).

## Per-role tracks

Five curve tracks and one event track, all optional:

```json
{
  "locomotion": [ { "t": 0.0, "vx": 0.0, "vy": 0.0, "vyaw": 0.0, "interp": "linear" } ],
  "head":       [ { "t": 0.0, "neck_pitch": 0.0, "head_pitch": 0.0, "head_yaw": 0.0, "head_roll": 0.0, "interp": "smooth" } ],
  "pose":       [ { "t": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "active": false } ],
  "mouth":      [ { "t": 0.0, "open": 0.0 } ],
  "events":     [ { "t": 8.0, "do": "kick_left" },
                  { "t": 3.0, "sound": "chirp" },
                  { "t": 15.0, "mode": "roller" } ],
  "servo":      [ { "t": 20.0, "mode": "hold", "duration": 5.0 } ]
}
```

### Curve tracks (`locomotion`, `head`, `pose`, `mouth`)

- Keyframes sorted by `t` (seconds from show start, float, ≥ 0). Validation rejects unsorted or duplicate `t` within a track.
- `interp` on a keyframe describes interpolation **from it to the next keyframe**: `"step"` | `"linear"` (default) | `"smooth"` (smoothstep). Booleans (`pose.active`) always step.
- Before the first keyframe: hold the first keyframe's values. After the last: hold the last (except locomotion at `meta.duration`, which is zeroed).
- The duck-agent samples curves at 50 Hz and emits the corresponding `robot.*` notifications (see robotd-api.md). Omitted curve tracks emit nothing — the duck's defaults rule.

### Event track (`events`)

Point events, exactly one action key per entry:

| Key | Value | Maps to |
|---|---|---|
| `do` | skill name — exactly one of `ground_pick`, `kick_left`, `kick_right`, `sit_toggle`, `roulade`; anything else is a validation **error** | `robot.do` |
| `sound` | sound tag — exactly one of `alarm`, `greet`, `inquire`, `peck`, `chirp`, `coo`, `wheee` (else validation **error**); optional `"hold": <seconds>` | `robot.sound` — see "Held sounds" below |
| `mode` | drive mode — exactly `"walk"` or `"roller"`; anything else is a validation **error** | `robot.setMode` — see "Custom .onnx policies" below |

Events fire once, at the first 50 Hz tick ≥ `t`. If playback starts *after* an event's `t` (late join, seek), the event is **skipped**, never replayed — except `mode` events, where the latest one ≤ the seek point is applied so the duck is in the right gait. If no `mode` event precedes that point, the current mode is left unchanged — shows that switch modes should place an explicit `mode` event at `t: 0.0` so every start and seek lands in a defined gait.

#### Held sounds

A `sound` event's `hold` is not a single start-then-stop pair. Per
`duck-ipc-proto`, `hold: true` must keep arriving once per tick (a
notification, the same cadence as `robot.mouth`) or robotd's hold state
decays and the sound ends on its own — a client that fires `hold: true`
once and then waits is a client that stops sustaining it almost
immediately. duck-agent re-issues `robot.sound {tag, hold: true}` from its
50 Hz tick loop for the event's `hold` seconds, then sends
`robot.sound {tag, hold: false}` exactly once to release it deliberately —
also triggered early, exactly once, by `stop`, `panic`, a fresh `load`, or
end-of-show. A `sound` event with no `hold` is a single one-shot trigger:
no re-sending, nothing to release.

### Servo track (`servo`) — reserved in v1

Declared in the spec so files can carry it, but v1 agents only honor `{"mode": "hold"}` (freeze locomotion, keep pose/head). Future modes: `laser_homing`, `color_homing` (`"target": "<beacon-id>"`), `follow_marker`. During a servo window, the servo controller owns `locomotion`/`head`; curve tracks resume when the window ends.

## Custom .onnx policies (`requires.policies`)

Two distinct mechanisms here, and they must not be conflated (docs/robotd-api.md's "Custom .onnx policies & modes" has the full picture):

1. **Which gait plays at runtime** is a `mode` event (above), sent over the wire as exactly `"walk"` or `"roller"` — the only two values real robotd accepts. There is no wire mechanism to name a custom mode.
2. **What a given mode actually does** is a *pre-show configuration* question: a fixed **policy slot** (`walk`, `stand`, `sitstand`, `ground_pick`, `kick_left`, `kick_right`, `roulade`, and the roller-family equivalents) is pointed at a custom `.onnx` file, applied by restarting `robotd` during load-in — never mid-show.

Shows that need a non-stock policy declare it so SwarmLink can provision it and duck-agent can verify it landed:

```json
"requires": {
  "policies": [
    { "name": "moonwalk", "file": "policies/moonwalk.onnx",
      "sha256": "…", "slot": "walk" }
  ]
}
```

- `name` is a **human label only**, for logs and error messages — it is never sent to robotd. `slot` is the field that matters: it is the fixed policy slot this `.onnx` occupies once installed.
- Provisioning is a **pre-show step**, never mid-show: SwarmLink pushes the `.onnx` to each cast duck, points `slot` at it in `robotd.toml`, and restarts `robotd` during load-in. The duck-agent verifies each required policy's `sha256` at LOAD and reports `policies_ok` in telemetry; preflight blocks the show otherwise.
- Once installed, the custom gait plays through the *ordinary* `mode` event mechanism above — a `{"mode": "walk"}` (or `"roller"`) event, exactly like a show with no custom policies at all. A policy's `name` never appears on the wire; there is no "reference the declared mode" step, because there is no declared mode to reference.
- Mode-switch constraints (standing still, switch latency) are a hardware question tracked for M1; the validator warns if a `mode` event overlaps nonzero locomotion within ±0.5 s.

## Validation limits (conservative defaults, tune on hardware)

| Quantity | Limit |
|---|---|
| \|vx\| | ≤ 0.25 m/s |
| \|vy\| | ≤ 0.20 m/s |
| \|vyaw\| | ≤ 1.5 rad/s |
| head angles | ≤ 1.2 rad each |
| pose z / roll / pitch | ≤ 0.05 m / 0.5 rad / 0.5 rad |
| mouth open | 0.0 – 1.0 |
| event density | ≥ 0.25 s between discrete events per duck |

Limits live in `python/duckshow/limits.py` as data, not scattered constants; the validator reports every violation with role, track, and `t`.

## Editor and tool fields

Tools may keep their own state in a top-level `"editor"` object (for example the timeline editor's per-role stage start marks under `editor.marks`). Loaders ignore it like any unknown field; editors preserve unknown fields on round-trip. Recorder-generated curve tracks are decimated to keyframes on value change (or at least every 100 ms) with `interp: "linear"` — see `docs/authoring.md`.

## Compiled form

None in v1 — agents parse the JSON directly and sample on the fly (a 5-minute, 10-duck show is well under a megabyte). If parsing ever matters on the RK3566, add a compiled form *behind the same sampler API*.
