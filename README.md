# MicroDuckSwarm

[![CI](https://github.com/virtualmagician/MicroDuckSwarm/actions/workflows/ci.yml/badge.svg)](https://github.com/virtualmagician/MicroDuckSwarm/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](python/)
[![Swift 6](https://img.shields.io/badge/swift-6-orange.svg)](SwarmLink/)

**A choreography system for a flock of [Pollen Robotics MicroDucks](https://pollen-robotics.com/microduck/)** — author a piece on a timeline, preview the whole cast in 3D, and play it back on stage in sync with music and video.

![The duckshow editor: a 3D stage viewer above a beat-gridded timeline, with eight MicroDucks in formation](docs/images/editor.png)

Eight ducks dancing together is not a networking problem, it is a *timing* problem. This repository solves it the way drone shows and theme-park animatronics do — and has the whole thing working, tested and watchable **before the hardware ships**.

## Quick start

```bash
./scripts/edit.sh shows/octet
```

Serves the checkout, opens the editor with the eight-duck piece loaded, and stops again on Ctrl+C. Space plays; `1` `2` `3` switch between the house, three-quarter and top cameras; drag to orbit. No build step, no dependencies, no CDN.

To watch the entire stack run without a single robot — two mock ducks, a duck-agent on each, the show master driving them, and a verifier checking they stayed in sync:

```bash
./scripts/e2e_demo.sh
```

## The idea

A show is authored once, compiled to a `.duckshow` file, and **pre-loaded onto every duck**. On show night the network carries only a shared clock, start/stop triggers, and telemetry. Each duck performs its own part locally at 50 Hz against MicroDuck's `robotd` daemon, so a WiFi dropout mid-number costs nothing:

```
 .duckshow ──▶ SwarmLink master ──UDP──▶ duck-agent ──▶ robotd ──▶ ONNX policies ──▶ 15 servos
                (clock + triggers)        (50 Hz local playback)
```

**Pre-load the show, synchronise only clocks, never stream the performance.** Almost everything else follows from that one decision.

Choreography here is **intent curves, not joint keyframes**. `robotd` accepts only high-level intents — `robot.move`, `robot.head`, `robot.pose`, `robot.mouth`, plus one-shot skills and sounds — and the robot's reinforcement-learned policies handle balance underneath. A `.duckshow` is therefore a handful of low-dimensional timed tracks per cast role, snapped to a beat grid. That makes authoring look like a sequencer rather than a character-animation rig.

## What it looks like

The stage viewer is a **kinematic preview**: it answers questions about staging, spacing, facing and silhouette. It does not simulate physics, and it never pretends to.

![Eight ducks in three-quarter view on the measured floor](docs/images/viewer-threequarter.png)

The floor is an instrument, not decoration — 1 m major lines, 10 cm minor divisions, tinted axes through the origin. A MicroDuck is 25 cm tall, so it stands about two and a half minor squares high and blocking distances read at a glance.

![Close view showing the head shell, camera lens, bill and articulated legs](docs/images/viewer-closeup.png)

![Top-down view showing the eight-duck formation and start marks](docs/images/viewer-top.png)

Top view is for formations and marks; drag a duck's start mark and it persists into the show file. `shows/octet` — *Eight to the Bar* — is 64 seconds at 120 bpm: a unison opening, eight solo turns while the rest hold still, and a finale back in unison.

## Prior art and influences

The design borrows deliberately, and it is worth being explicit about from where:

- **Boston Dynamics' Spot Choreography SDK** — the [Choreographer](https://dev.bostondynamics.com/docs/concepts/choreography/choreographer.html) data model of beats, slices and per-actuator tracks, uploaded ahead of time and executed from a shared start timestamp, is the closest documented ancestor of the `.duckshow` format.
- **Disney Research's BDX droids** — *[Design and Control of a Bipedal Robotic Character](https://arxiv.org/abs/2501.05204)* establishes the pattern this project's authoring loop imitates: artist-authored motion becomes an imitation-learning reference, a command-conditioned policy executes it, and a human operator drives the result live. MicroDuck descends from the same lineage via [Open Duck Mini](https://github.com/apirrone/Open_Duck_Mini).
- **Drone light shows** — [Verge Aero on how a show actually runs](https://docs.verge.aero/drone-show-technology/how-drone-shows-work-an-overview) (upload the entire show to every aircraft, then trigger against a scheduled future timestamp), and [Verity Studios](https://www.veritystudios.com/technology) treating performing robots as ordinary timecode-cued show devices.
- **[Falcon Player](https://github.com/FalconChristmas/fpp) / xLights** — the unglamorous but proven precedent for many small WiFi nodes performing in sync to music: a master emitting lightweight sync packets while each node free-runs its own local copy.
- **[RFC 9119](https://www.rfc-editor.org/rfc/rfc9119.html)**, on multicast over IEEE 802.11 — why must-arrive messages here are unicast, repeated and idempotent. 802.11 neither acknowledges nor retransmits multicast frames, so a "start" packet can silently reach seven of eight ducks.
- **Audiovisual synchrony thresholds** — misalignment below roughly 20 ms is imperceptible, and detection is asymmetric. That sets the sync budget the whole system is engineered against.
- **[QLab](https://qlab.app/docs/v5/networking/)** and the OSC show-control conventions it popularised, which the `swarmctl serve` facade mirrors so the flock behaves like any other cued device in an existing rig.

## Status

Pre-hardware. MicroDuck units ship late 2026; everything here runs today against a protocol-faithful mock duck, with the wire protocol verified against the [microduck](https://github.com/pollen-robotics/microduck) source (`duck-ipc-proto`, API version 16).

| | |
|---|---|
| Cross-duck event sync (localhost, mock ducks) | **1.3 ms** measured, against a ~20 ms perceptual budget |
| Clock discipline | NTP-style over UDP, min-RTT filtered, slew-limited |
| Tests | 326 Python · 175 Swift · 146 editor |
| End-to-end gates | 3 — Python master, Swift master over OSC, recorder round-trip |
| Third-party runtime dependencies | **0** |

That zero is deliberate and load-bearing. Python is standard-library only so the agent runs on a stock Armbian image; SwarmLink uses only system frameworks; the editor has no build step and no CDN, so it opens at a venue with no internet.

## Repository layout

| Path | What it is |
|---|---|
| `docs/` | Specs — the contracts between components, written before the code |
| `docs/duckshow-format.md` | The `.duckshow` choreography file format |
| `docs/swarmlink-protocol.md` | Clock sync, triggers, telemetry and the puppet channel over UDP |
| `docs/robotd-api.md` | Verified MicroDuck `robotd` JSON-RPC surface (API v16) |
| `docs/osc-facade.md` | OSC 1.0 control surface for external rigs |
| `docs/authoring.md` | Puppeteering, `swarmctl record`, the timeline editor |
| `docs/viewer.md` | The 3D stage viewer, and the planned baked-physics preview |
| `python/duckshow/` | Format library: parse, validate, sample at 50 Hz |
| `python/duck_agent/` | On-duck agent: clock discipline, local playback, telemetry, puppet channel |
| `python/mock_duck/` | Protocol-faithful mock `robotd`, with a timestamped intent log |
| `python/tools/` | Reference show master, stdlib OSC send/listen, puppet streamer |
| `SwarmLink/` | Swift 6 package: master engine, `swarmctl` CLI, OSC facade, recorder |
| `editor/` | Zero-dependency timeline editor and WebGL2 stage viewer |
| `shows/` | Example choreographies, including the eight-duck `octet` |
| `scripts/` | Launcher and three end-to-end gates |

## Components

### Python — the duck side

Python 3.10+, standard library only, on the duck and off it.

```bash
cd python && python3 -m unittest discover -s tests
```

### SwarmLink — the show side

Swift 6, strict concurrency, zero third-party dependencies (Network.framework for UDP).

```bash
cd SwarmLink && swift build && swift test
```

`swarmctl` is the show-master CLI. For show night with any rig that speaks OSC — QLab, TouchDesigner, a lighting desk:

```bash
swarmctl serve --roster roster.json --shows-dir shows/    # OSC on UDP 53300, Bonjour _duckswarm._udp
python3 python/tools/osc_send.py 127.0.0.1:53300 /duckswarm/load s:octet
python3 python/tools/osc_send.py 127.0.0.1:53300 /duckswarm/go
```

Commands: `/duckswarm/{load,play,go,seek,stop,panic,ping,status}`, with status pushed to any address that pinged in the last five seconds. Full table in [`docs/osc-facade.md`](docs/osc-facade.md).

### Authoring

Three ways into a `.duckshow` file, detailed in [`docs/authoring.md`](docs/authoring.md):

- **Puppet with a gamepad.** `swarmctl record` streams a controller to one duck over the live puppet channel and captures the intent stream as that role's tracks — one role at a time, layering onto a show that plays back around you.
- **Scripted recordings.** `--input script:<file.json>` replays timed input frames instead of a live controller. Reproducible, which is how CI exercises the recorder.
- **The timeline editor.** Keyframes and events on a beat grid, with the 3D stage above showing the whole cast.

The puppet channel doubles as a show-night nudge layer: puppeteering a duck mid-playback *adds* to its locomotion and *overrides* its head and pose while packets stay fresh (250 ms deadman), then hands control back to the timeline. Panic and stop mute it outright — the safety invariants always win.

## Assets and licensing

This repository contains **no Pollen Robotics files**. MicroDuck's meshes, MJCF model and hardware design files are licensed CC BY-SA-NC and are not redistributed here; the duck in the viewer is our own geometry, built from primitives. The planned physics preview ([`docs/viewer.md`](docs/viewer.md)) loads Pollen's model from a gitignored `assets/microduck/` that you supply yourself — the way an emulator does not ship a BIOS.

MIT — see [LICENSE](LICENSE). MicroDuck is a product of [Pollen Robotics](https://pollen-robotics.com/) / [Hugging Face](https://huggingface.co/pollen-robotics). This is an independent, unaffiliated project, built by an enthusiast with admiration for the original.
