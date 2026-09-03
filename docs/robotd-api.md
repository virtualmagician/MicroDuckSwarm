# MicroDuck robotd JSON-RPC surface (verified)

Verified against [`duck-ipc-proto/src/lib.rs`](https://github.com/pollen-robotics/microduck/blob/main/duck-ipc-proto/src/lib.rs) on 2026-09-03. **API_VERSION = 17.** This file is the authority for every component in this repo (duck-agent client, mock duck server). When upstream bumps the version, re-verify and update here first.

Both v16 (`update.show`) and v17 (`system.services`'s `UnitState` gaining `Restarting`/`Failed`) landed since the previous verification, and both landed **outside** the `robot.*` namespace duck-agent uses — nothing in the method tables below changed as a result of either bump, only this file's stated version number and verified date.

## Wire format

JSON-RPC 2.0, **NDJSON** (one JSON object per line, `\n`-framed), over a Unix domain socket on the robot. The mock duck also serves the identical protocol over TCP for development convenience.

Handshake: client sends `hello` request `{"api_version": 17}` → reply `{"api_version": ..., "daemon_version": ..., "revision": ...}`. Version mismatch is reported, not refused.

All field names are **snake_case**. Angles are radians, velocities m/s and rad/s (SI assumed; confirm ranges on hardware).

## Continuous intents — send as notifications (no `id`, no reply)

| Method | Params | Notes |
|---|---|---|
| `robot.move` | `{vx: f64, vy: f64, vyaw: f64}` | Trunk-frame velocity command |
| `robot.head` | `{neck_pitch, head_pitch, head_yaw, head_roll}` (all f64) | Head pose |
| `robot.pose` | `{z: f64, roll: f64, pitch: f64, active: bool}` | Body lean/crouch; glided while `active`, snaps back when `false` |
| `robot.mouth` | `{open: f64}` | Beak |

These are last-value-wins at the 50 Hz control loop, guarded by **one deadman that applies to every client alike** — local Unix socket or remote — not a remote-only watchdog with the local path exempt. `deploy/robotd.toml` sets `deadman_ms = 500` by default: once intents stop arriving for that long, robotd zeroes the commanded velocity and the duck stays standing (it does not go limp or collapse). The direct consequence for us: duck-agent's own 50 Hz tick loop must not stall past roughly 500 ms, which is exactly what the M1 tick-rate measurement item in docs/architecture.md is tracking (Python has reached only ~16 Hz on a loaded CI VM there).

## Discrete calls — send as requests (expect replies)

| Method | Params | Reply |
|---|---|---|
| `robot.look` | `{x, y, z, neck_pitch}` | `{head: HeadParams, clamped: bool}` — gaze at trunk-frame point |
| `robot.do` | `{skill: Skill}` | ack |
| `robot.sound` | `{tag: SoundTag, hold?: bool}` | ack |
| `robot.stop` | `{}` | ack |
| `robot.enable` | `{on: bool, toggle: bool}` | ack |
| `robot.init` | `{}` | ack |
| `robot.relax` | `{}` | ack |
| `robot.mode` | `{}` | current mode |
| `robot.setMode` | `{mode: String}` | ack — runtime policy-mode switch (see below) |
| `robot.health` | `{}` | health report |
| `robot.safeToRestart` | `{}` | bool-ish |
| `robot.modelApi` | `{}` | `ModelApiResult` — `{model_api: u32, ...}`, the ONNX observation/action contract version (distinct from `API_VERSION` above; the published policy manifest carries the same number). Not used by duck-agent v1. |
| `robot.remoteSessionActive` | `{}` | bool — is a telepresence session live. Not used by duck-agent v1. |
| `robot.subscribe` | `{hz?: u32}` | `{accepted, ...}` then streams `robot.state` notifications |
| `robot.theremin` / `robot.chorale` | `{active: bool}` | `{accepted, reason?}` |
| `robot.shutdown` | `{}` | ack |

### Skill enum (snake_case in JSON)

`ground_pick` · `kick_left` · `kick_right` · `sit_toggle` · `roulade`

### SoundTag enum (snake_case in JSON)

`alarm` · `greet` · `inquire` · `peck` · `chirp` · `coo` · `wheee` (held sounds: start → loop → end via `hold`)

## State stream

After `robot.subscribe`, the daemon emits `robot.state` notifications at the requested rate: `{joints: [...], targets: [...], ...}`. Canonical joint order (`JOINT_NAMES`, 15): left leg ×5 · neck/head/mouth ×5 · right leg ×5.

## Custom .onnx policies & modes

Two distinct mechanisms — do not conflate them:

1. **Policy installation** (pre-show provisioning): the absolute config path is `/etc/robot/robotd.toml`. It has a *fixed* set of policy slots — `walk`, `stand`, `sitstand`, `ground_pick`, `kick_left`, `kick_right`, `roulade`, and their roller-family equivalents — each pointing at a `.onnx` file, e.g. `[policy] walk = "/home/radxa/my_walking.onnx"`. There is no way to add a slot or register a custom-named mode; a custom gait replaces the file behind an existing slot. Applied by restarting `robotd` (`sudo systemctl restart robotd`), never mid-show: the restart's health gate polls `robot.health` for up to 30 s, typically landing healthy in about 8-9 s. A policy that fails to load leaves `robotd` running and holding the last pose rather than crashing — it reports unhealthy via `robot.health`, with a message of the form `policy unavailable: <reason>`. Policies are validated at load time against a fixed contract, `obs[1,61] -> actions[1,14]` — the observation/action shape implied by the `robot.modelApi` contract version above. The `.duckshow` manifest declares required policies by hash; duck-agent verifies at LOAD (see duckshow-format.md) — `requires.policies[].name` there is a human label for logs only and is never sent to robotd; `slot` is the field that actually matters.
2. **Mode switching** (runtime): `robot.setMode {mode}` switches between the two drive modes and only those two, `"walk"` and `"roller"` — there is no third value and no way to name a custom mode over the wire. A custom-trained gait is reached by installing it into one of the fixed slots above, then switching to whichever of `"walk"`/`"roller"` that slot belongs to. **Open hardware question (M1): switch latency and which mid-motion states allow it.** Until measured, treat a mode switch as requiring the duck to be standing still.

## Error codes

JSON-RPC reserved (−32700…−32603) plus application codes: `1` BUSY, `14` PERMISSION_DENIED.

## Other daemons (not used by duck-agent v1)

`update.*` (updaterd), `net.*` / `system.*` (configd), `pad.*` (padd), `tof.stream` (tofd), `chorale.*` (btd↔robotd). `system.info`, `net.status`, and battery/health fields are candidates for telemetry enrichment later.
