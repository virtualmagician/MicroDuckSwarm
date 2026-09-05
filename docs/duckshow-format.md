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

Event `t` must be finite and `>= 0`, and a `hold` (sound events) must be finite when present. Same rule and same reason as the curve tracks: a negative or non-finite time is not a point on any timeline.

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

#### Skill durations and occupancy

Each `do` skill is a fixed `.onnx` policy running against a policy slot (docs/robotd-api.md's "Custom .onnx policies & modes"); `assets/microduck/policies/manifest.json` (schema_version 2, `control_hz` 50) says how it behaves once it starts, and this is the full authoring mapping:

| Authored (`do` / implicit) | Policy | Kind | Duration |
|---|---|---|---|
| `ground_pick` | `alpha_ground_pick.onnx` (walk mode) / `roller_crouch.onnx` (roller mode) | episodic | 2.8 s (walk) / 3.5 s (roller) |
| `roulade` | `roulade.onnx` | episodic, chains | 1.0 s |
| `kick_left` | `ball_kick_left.onnx` | episodic | 0.5 s |
| `kick_right` | `ball_kick_right.onnx` | episodic | 0.5 s |
| `sit_toggle` | `alpha_sitstand.onnx` | scripted | no fixed duration — `ramp_s` 2.0 s / `unwind_s` 1.0 s posture transition; the hand-off timing for a second `sit_toggle` mid-ramp is not confirmed |
| implicit in `locomotion` | `alpha_walking.onnx` (walk mode) / `roller.onnx` (roller mode) | perpetual | runs continuously |
| implicit in `pose` | `alpha_stand.onnx` | perpetual | runs continuously |

`roller_crouch.onnx` is never authored directly — it is simply what the robot runs *instead of* `alpha_ground_pick.onnx` when a `ground_pick` event fires while the duck is in roller mode. Which mode is "in effect" for a given `ground_pick` is resolved from the `mode` events preceding it, the same rule late-join/seek uses for gait (above). The other three episodic skills' durations do not depend on drive mode.

An *episodic* skill (everything above except `sit_toggle`) runs its whole clip once started — the robot cannot interrupt it to honor a second command any sooner. Scheduling a second skill event inside that window is legal (the robot will accept the command and something will happen) but is very likely not what the author meant, so the validator raises it as a **warning**, naming both skills, the overlap in seconds, and the occupying skill's duration. This is a different concern from the event-density limit below, which is about command flooding (any two discrete events, regardless of type or duration) — the two rules run independently and can both fire on the same pair of events.

`roulade` is the one documented exception: `manifest.json` marks it `"chain": true`, meaning a `roulade` immediately following a `roulade` is the intended way to keep rolling, not two skills contending for one window — that specific pairing never warns. `sit_toggle` has no confirmed duration at all (see the table above), so it never *occupies*: it opens no window, and nothing scheduled after a `sit_toggle` is ever warned about. It can still be the *interrupting* skill. A `sit_toggle` 0.5 s into a `ground_pick` is a second command to a duck that cannot have finished the first, and warns exactly as any other skill scheduled there would.

Durations live in `python/duckshow/limits.py`'s `SKILL_DURATIONS_S` / `GROUND_PICK_ROLLER_DURATION_S` / `CHAINING_SKILLS`, mirrored in SwarmLink and the editor.

### Servo track (`servo`) — reserved in v1

Declared in the spec so files can carry it, but v1 agents only honor `{"mode": "hold"}` (freeze locomotion, keep pose/head). Future modes: `laser_homing`, `color_homing` (`"target": "<beacon-id>"`), `follow_marker`. During a servo window, the servo controller owns `locomotion`/`head`; curve tracks resume when the window ends.

**Validated even though it is reserved**, because a file can carry the track today and the diagnostics are cheap: `t` must be finite and `>= 0`, and a `duration`, when present, must be finite and `> 0` (a zero or negative window is silently never entered). Any `mode` other than `"hold"` is a **warning**, not an error, in the same words the format uses here: the file is legal, but the mode has no effect on a v1 agent, and finding that out on show night is the failure this prevents. A `servo` entry with no `duration` extends until the next entry, or forever.

## Custom .onnx policies (`requires.policies`)

Two distinct mechanisms here, and they must not be conflated (docs/robotd-api.md's "Custom .onnx policies & modes" has the full picture):

1. **Which gait plays at runtime** is a `mode` event (above), sent over the wire as exactly `"walk"` or `"roller"` — the only two values real robotd accepts. There is no wire mechanism to name a custom mode.
2. **What a given mode actually does** is a *pre-show configuration* question: a fixed **policy slot** (`walk`, `stand`, `sitstand`, `ground_pick`, `kick_left`, `kick_right`, `roulade`, and the roller-family equivalents) is pointed at a custom `.onnx` file, applied by restarting `robotd` during load-in — never mid-show.

Mode isn't only cosmetic: it also picks which policy a `ground_pick` event runs and for how long — see "Skill durations and occupancy" above.

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
| \|vx\| | ≤ 0.40 m/s |
| \|vy\| | ≤ 0.40 m/s |
| \|vyaw\| | ≤ 1.5 rad/s |
| head angles | ≤ 1.2 rad each |
| pose z / roll / pitch | ≤ 0.05 m / 0.5 rad / 0.5 rad |
| mouth open | 0.0 – 1.0 |
| event density | ≥ 0.25 s between discrete events per duck |
| skill occupancy | a `do` skill event starting before the previous skill's duration has elapsed → **warning** (see "Skill durations and occupancy" above); `roulade` chaining into itself is exempt |

### Why the translation limits are 0.40 (raised 2026-09-04)

They were 0.25 / 0.20, picked as cautious stage speeds before anything was measured. Measuring `alpha_walking.onnx` showed those caps were not conservative, they were **unusable**: the policy has a sharp stand/walk gate and emits no gait at all below a threshold per axis (`docs/bake-format.md`, "The low-speed problem"). Against the old caps:

| axis | gait threshold | old cap | old usable band |
|---|---|---|---|
| `vx` forward | 0.238 m/s | 0.25 | 0.238 to 0.25 |
| `vx` backward | -0.326 m/s | -0.25 | **empty** |
| `vy` lateral | 0.312 m/s | 0.20 | **empty** |
| `vyaw` | 1.047 rad/s | 1.5 | 1.047 to 1.5 |

A cap below the gate does not make the duck move slowly and safely; it makes the duck **not move at all**, while the show still validates clean. Every reverse and every sidestep in every show in this repo was silently a no-op. Forward motion survived only in a 0.012 m/s sliver at the very top of the legal range.

0.40 is not an arbitrary loosening. It is the edge of the policy's own training distribution: `microduck_rl` sampled `lin_vel_x` uniformly from **(-0.4, 0.4)**, so commanding beyond it asks for behaviour the policy was never trained to produce. `vy` is raised to match rather than to a separately-derived number, since no per-axis training range for lateral velocity has been found; if one turns up, tighten it.

`vyaw` stays 1.5: it was already above its gate and already usable.

**These thresholds are measured in the baker's simulated plant, not on hardware.** That plant also under-tracks (a 0.40 command yields about 0.154 m/s achieved), and a plant that under-tracks gates later than the real machine, so a real duck may well gait below these numbers. The day-one hardware measurement is specific and cheap: ramp `vx`, `vy` and `vyaw` from zero and record where stepping begins. Retune both these caps and `docs/bake-format.md`'s table from those three numbers.

Limits live in `python/duckshow/limits.py` as data, not scattered constants; the validator reports every violation with role, track, and `t`.

## Editor and tool fields

Tools may keep their own state in a top-level `"editor"` object (for example the timeline editor's per-role stage start marks under `editor.marks`). Loaders ignore it like any unknown field; editors preserve unknown fields on round-trip. Recorder-generated curve tracks are decimated to keyframes on value change (or at least every 100 ms) with `interp: "linear"` — see `docs/authoring.md`.

## Compiled form

None in v1 — agents parse the JSON directly and sample on the fly (a 5-minute, 10-duck show is well under a megabyte). If parsing ever matters on the RK3566, add a compiled form *behind the same sampler API*.
