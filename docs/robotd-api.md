# MicroDuck robotd JSON-RPC surface (verified)

Verified against [`duck-ipc-proto/src/lib.rs`](https://github.com/pollen-robotics/microduck/blob/main/duck-ipc-proto/src/lib.rs) on 2026-09-01. **API_VERSION = 16.** This file is the authority for every component in this repo (duck-agent client, mock duck server). When upstream bumps the version, re-verify and update here first.

## Wire format

JSON-RPC 2.0, **NDJSON** (one JSON object per line, `\n`-framed), over a Unix domain socket on the robot. The mock duck also serves the identical protocol over TCP for development convenience.

Handshake: client sends `hello` request `{"api_version": 16}` → reply `{"api_version": ..., "daemon_version": ..., "revision": ...}`. Version mismatch is reported, not refused.

All field names are **snake_case**. Angles are radians, velocities m/s and rad/s (SI assumed; confirm ranges on hardware).

## Continuous intents — send as notifications (no `id`, no reply)

| Method | Params | Notes |
|---|---|---|
| `robot.move` | `{vx: f64, vy: f64, vyaw: f64}` | Trunk-frame velocity command |
| `robot.head` | `{neck_pitch, head_pitch, head_yaw, head_roll}` (all f64) | Head pose |
| `robot.pose` | `{z: f64, roll: f64, pitch: f64, active: bool}` | Body lean/crouch; glided while `active`, snaps back when `false` |
| `robot.mouth` | `{open: f64}` | Beak |

These are last-value-wins at the 50 Hz control loop. A watchdog halts the robot if a **remote** command stream stalls; the local Unix socket path used by duck-agent is not subject to network jitter.

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

1. **Policy installation** (pre-show provisioning): `robotd.toml` policy slots point at `.onnx` files, e.g. `[policy] walk = "/home/radxa/my_walking.onnx"`, applied by restarting `robotd` (`sudo systemctl restart robotd`). Never done mid-show. The `.duckshow` manifest declares required policies by hash; duck-agent verifies at LOAD (see duckshow-format.md).
2. **Mode switching** (runtime): `robot.setMode {mode}` switches between configured policy modes (e.g. legs ↔ roller). **Open hardware question (M1): switch latency, allowed mid-motion states, and whether custom modes can be registered.** Until measured, treat a mode switch as requiring the duck to be standing still.

## Error codes

JSON-RPC reserved (−32700…−32603) plus application codes: `1` BUSY, `14` PERMISSION_DENIED.

## Other daemons (not used by duck-agent v1)

`update.*` (updaterd), `net.*` / `system.*` (configd), `pad.*` (padd), `tof.stream` (tofd), `chorale.*` (btd↔robotd). `system.info`, `net.status`, and battery/health fields are candidates for telemetry enrichment later.
