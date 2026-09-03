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
| `SwarmLink` | Swift 6 (zero deps) | Production master engine: roster, clock, command fan-out, telemetry/preflight. `swarmctl` CLI; `swarmctl serve` exposes the OSC facade (`docs/osc-facade.md`) for external rigs; later embeds in StageWizard as the `robotShow` cue player. |

Both masters implement `docs/swarmlink-protocol.md`; both robot ends implement `docs/robotd-api.md`. The docs are the contracts — change docs first, then code.

## Sync budget

Master↔agent clock sync well under 10 ms on a dedicated AP (min-RTT NTP-style filter) + 50 Hz quantization (≤ 20 ms) keeps duck-to-duck and duck-to-music error inside the ~20–50 ms human perception window. Music/video play in-process in StageWizard, so they share the master clock by construction.

## Custom policies

`.duckshow` files declare required `.onnx` policies by hash (`requires.policies`); SwarmLink provisions them at load-in (push file, patch `robotd.toml`, restart `robotd` — never mid-show), agents verify hashes at LOAD, and timeline `mode` events switch gaits at runtime via `robot.setMode`.

## The app

Decided 2026-09-02: the destination is a **separate DuckSwarm.app**, not a module inside StageWizard.

- **Why separate.** Breaking the duck app can never break the show-control tool. Duck work keeps its own release cadence. And the seam already exists and is tested — StageWizard (or QLab, or a lighting desk) fires OSC at `swarmctl serve`; the app is that server with a face.
- **What it contains.** SwarmLink as a library (the actor API is already shaped for this: `load`, `play`, `seek`, `stop`, `panic`, a telemetry `AsyncStream`); the OSC facade on the same port it uses today; the recorder driven by a real gamepad; and the editor hosted in a `WKWebView` — the page is dependency-free and self-contained, so it moves in with no rewrite and keeps working standalone in a browser.
- **The one part that must wait.** The preflight dashboard — per-duck battery, signal, clock offset, heartbeat age, policy hashes, go/no-go before the segment — is the piece worth building natively, and it is exactly the piece that cannot be designed honestly before real telemetry exists. Building it against invented numbers guarantees rebuilding it at M1.
- **Boundary to hold.** The app is a shell. Anything it can do, `swarmctl` can still do headless, and the editor still opens as a plain file — a venue with a dead laptop should be recoverable from a terminal and a browser.

## Milestones

- **M0 (now, pre-hardware):** everything in this repo running against `mock_duck` + the browser simulator. Format, agent, mock, SwarmLink skeleton, e2e demo.
- **M1 (first duck):** hardware bring-up — latency measurements, `robot.setMode` semantics, battery/boot timing, camera access for servo cues, watchdog behavior on the local socket, and the duck-agent's achieved tick rate on the RK3566 (Python reaches ~38 Hz on a dev Mac and ~16 Hz on a loaded CI VM; if the duck cannot hold ≥ 40 Hz, port the tick loop to Rust).
- **M2:** show-grade core — agent as systemd unit, provisioning scripts, DuckSwarm.app shell (SwarmLink + WKWebView editor + recorder + preflight dashboard, designed against real telemetry), 2–3 ducks to music from a GO. (OSC facade: done 2026-09-02, pre-hardware.)
- **M3:** full flock — preflight dashboard, timeline editor with beat grid, rehearsal tools (seek/loop/solo), servo cues (laser/color homing, marker follow), and the 3D stage viewer (`docs/viewer.md`) — kinematic, dependency-free, house/three-quarter/top cameras.
- **M4+:** NPU person following, overhead tag tracking for true formations, Blender import, and **Create Preview** — baked real-policy physics (MuJoCo-WASM + onnxruntime-web) rendered through the viewer's pose interface, whose real prize is the intended-vs-actual drift diff (`docs/viewer.md`). Pollen's MJCF/meshes are CC BY-SA-NC and are never vendored into this MIT repo — the preview loads them from a gitignored, user-supplied `assets/microduck/`, falling back to the primitive duck when absent.
- **DuckSwarm.app** (M2, once telemetry is real) — the show-night face of all this: a small SwiftUI app wrapping SwarmLink, hosting the existing editor in a WKWebView unchanged, running the recorder against a gamepad, and showing the preflight dashboard. See "The app" below.
- **StageWizard integration** (a `robotShow` cue type embedding SwarmLink) is **no longer the plan**, superseded by the OSC facade: StageWizard fires `/duckswarm/*` at DuckSwarm.app like any other cued device. Revisit only if a genuinely single-app show workflow proves necessary — the cost of that path is coupling duck experiments to the release cycle of the tool Marco actually performs with.

## Decisions log

- 2026-09-02 — The product is a **separate DuckSwarm.app**, not a StageWizard module; the OSC facade is the integration seam, so the StageWizard `robotShow` cue type is dropped rather than deferred. App shell lands at M2 because its most valuable surface, the preflight dashboard, needs real telemetry to design.

- 2026-09-01 — Engine as a Swift package used both ways (StageWizard cue type + OSC facade). Authoring = record infra + minimal timeline UI in parallel. V1 spatial scope: in-place + loose walks + servo cues; no overhead tracking. Duck-agent in Python first. Custom `.onnx` policy triggering in scope via `requires.policies` + `mode` events.
- 2026-09-02 — OSC facade shipped (`swarmctl serve`, docs/osc-facade.md): StageWizard/StageWand-style contract, own MIT codec ported; reviewed by a second adversarial workflow (31 findings → 26 fixed, 4 refuted-as-spec-conformant). Swift↔Python interop is now gated by `scripts/e2e_osc.sh` in CI's macOS job.
- 2026-09-01 (later) — StageWizard integration parked until hardware arrives and the rest is ready. M0 hardened by an adversarial review workflow (88 findings → 72 fixed); CI added; e2e verifier now checks curve values, rates, and end-of-show ordering.
