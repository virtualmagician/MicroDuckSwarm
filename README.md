# MicroDuckSwarm

[![CI](https://github.com/virtualmagician/MicroDuckSwarm/actions/workflows/ci.yml/badge.svg)](https://github.com/virtualmagician/MicroDuckSwarm/actions/workflows/ci.yml)

Choreography authoring and playback for a flock of [Pollen Robotics MicroDucks](https://pollen-robotics.com/microduck/) — synchronized to music and video, built for live keynote stages.

A duck show is authored once, compiled to a `.duckshow` file, and pre-loaded onto every duck. On show night the WiFi carries only a shared clock, start/stop triggers, and telemetry — each duck performs its part locally at 50 Hz against MicroDuck's `robotd` daemon, so a network dropout mid-number costs nothing. The pattern is borrowed from drone light shows and Disney's BDX droids: **pre-load the show, sync only clocks, never stream the performance.**

## Status

Pre-hardware development (M0). MicroDuck units ship late 2026; everything here runs today against a protocol-faithful mock duck and is verified against the [microduck](https://github.com/pollen-robotics/microduck) source (API version 16).

## Layout

| Path | What it is |
|---|---|
| `docs/` | Specs — the source of truth for every component |
| `docs/duckshow-format.md` | The `.duckshow` choreography file format |
| `docs/swarmlink-protocol.md` | Clock sync, triggers, telemetry over UDP |
| `docs/robotd-api.md` | Verified MicroDuck `robotd` JSON-RPC surface |
| `docs/osc-facade.md` | OSC 1.0 control surface of `swarmctl serve` for external rigs |
| `python/duckshow/` | Format library: parse, validate, sample at 50 Hz |
| `python/duck_agent/` | On-duck show agent: clock discipline, local playback, telemetry |
| `python/mock_duck/` | Protocol-faithful mock `robotd` for development without hardware |
| `python/tools/` | Reference show master CLI, stdlib OSC send/listen tool |
| `SwarmLink/` | Swift package: show-master engine, `swarmctl` CLI, OSC facade (`swarmctl serve`) |
| `shows/` | Example choreographies |
| `scripts/` | End-to-end demos (`e2e_demo.sh` Python master, `e2e_osc.sh` Swift master over OSC) |

## Quick start (no hardware needed)

```bash
./scripts/e2e_demo.sh
```

Boots two mock ducks, attaches a duck-agent to each, and plays the demo show — then verifies both ducks received their intents in sync.

## Design in one paragraph

Choreography on MicroDuck is **intent curves, not joint keyframes**: `robotd` accepts only high-level intents (`robot.move`, `robot.head`, `robot.pose`, `robot.mouth`, one-shot skills and sounds) and its RL policies handle balance underneath. A `.duckshow` is therefore a set of low-dimensional timed tracks per cast role — locomotion, head, pose, mouth, events — snapped to a beat grid. The duck-agent disciplines its clock to the show master (NTP-style over UDP, well under 10 ms on a dedicated AP), then plays its track into `robotd`'s local Unix socket. Triggers are unicast, repeated, and idempotent; WiFi multicast is never used.

## Python

Python 3.10+, standard library only — no third-party dependencies, on the duck or off it.

```bash
cd python && python -m pytest 2>/dev/null || python -m unittest discover -s tests -v
```

## SwarmLink (Swift)

```bash
cd SwarmLink && swift build && swift test
```

Zero third-party dependencies (Network.framework for UDP). `swarmctl` is the standalone show-master CLI. For show night with any rig that speaks OSC (QLab, TouchDesigner, a lighting desk, StageWizard network cues):

```bash
swarmctl serve --roster roster.json --shows-dir shows/        # OSC on UDP 53300, Bonjour _duckswarm._udp
python3 python/tools/osc_send.py 127.0.0.1:53300 /duckswarm/load s:demo
python3 python/tools/osc_send.py 127.0.0.1:53300 /duckswarm/go
```

Commands: `/duckswarm/{load,play,go,seek,stop,panic,ping,status}`; status feedback is pushed to any address that pinged in the last 5 s — the same contract as StageWizard ↔ StageWand. Full table in `docs/osc-facade.md`. Embedding into [StageWizard](https://github.com/virtualmagician/StageWizard) as a `robotShow` cue type is planned once hardware is in hand.

## License

MIT — see [LICENSE](LICENSE). MicroDuck is a product of Pollen Robotics / Hugging Face; this project is an independent fan-built tool and is not affiliated with them.
