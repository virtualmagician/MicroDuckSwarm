# MicroDuckSwarm architecture

**Goal:** 8–10 MicroDucks performing choreographed numbers in live keynotes, tight to music and video, run from StageWizard with one GO.

**Core pattern** (from drone shows / BDX droids / Falcon Player): pre-load the full show onto every duck, discipline clocks over WiFi, trigger with repeated idempotent unicast commands, and let each duck perform locally. The network is allowed to fail mid-number.

```
StageWizard (Mac) ── robotShow cue ──▶ SwarmLink (Swift package)
                                          │  UDP 47800/47801 · dedicated 5 GHz AP
                          ┌───────────────┼────────────────┐
                          ▼               ▼                ▼
                     duck-agent      duck-agent   ···  duck-agent      (Python, on each duck)
                          │ robotd Unix socket (JSON-RPC NDJSON, api v16)
                          ▼
                       robotd ──▶ ONNX policies ──▶ 15 servos @ 50 Hz
```

## Components

| Component | Language | Role |
|---|---|---|
| `python/duckshow` | Python (stdlib) | Parse / validate / sample `.duckshow` files. Shared by agent, tools, tests. |
| `python/duck_agent` | Python (stdlib) | On-duck daemon: SwarmLink agent side (time sync, commands, telemetry), 50 Hz playback into `robotd`. |
| `python/mock_duck` | Python (stdlib) | Protocol-faithful fake `robotd` (Unix socket + TCP). Records every intent with timestamps for assertions. Dev without hardware. |
| `python/tools/showmaster.py` | Python (stdlib) | Reference master CLI (protocol conformance + e2e tests). |
| `SwarmLink` | Swift 6 (zero deps) | Production master engine: roster, clock, command fan-out, telemetry/preflight. Embeds in StageWizard as the `robotShow` cue player; `swarmctl` CLI + OSC facade for external rigs. |

Both masters implement `docs/swarmlink-protocol.md`; both robot ends implement `docs/robotd-api.md`. The docs are the contracts — change docs first, then code.

## Sync budget

Master↔agent clock sync well under 10 ms on a dedicated AP (min-RTT NTP-style filter) + 50 Hz quantization (≤ 20 ms) keeps duck-to-duck and duck-to-music error inside the ~20–50 ms human perception window. Music/video play in-process in StageWizard, so they share the master clock by construction.

## Custom policies

`.duckshow` files declare required `.onnx` policies by hash (`requires.policies`); SwarmLink provisions them at load-in (push file, patch `robotd.toml`, restart `robotd` — never mid-show), agents verify hashes at LOAD, and timeline `mode` events switch gaits at runtime via `robot.setMode`.

## Milestones

- **M0 (now, pre-hardware):** everything in this repo running against `mock_duck` + the browser simulator. Format, agent, mock, SwarmLink skeleton, e2e demo.
- **M1 (first duck):** hardware bring-up — latency measurements, `robot.setMode` semantics, battery/boot timing, camera access for servo cues, watchdog behavior on the local socket.
- **M2:** show-grade core — agent as systemd unit, provisioning scripts, 2–3 ducks to music from a StageWizard GO.
- **M3:** full flock — preflight dashboard, timeline editor with beat grid, rehearsal tools (seek/loop/solo), servo cues (laser/color homing, marker follow).
- **M4+:** NPU person following, overhead tag tracking for true formations, Blender import.

## Decisions log

- 2026-09-01 — Engine as a Swift package used both ways (StageWizard cue type + OSC facade). Authoring = record infra + minimal timeline UI in parallel. V1 spatial scope: in-place + loose walks + servo cues; no overhead tracking. Duck-agent in Python first. Custom `.onnx` policy triggering in scope via `requires.policies` + `mode` events.
