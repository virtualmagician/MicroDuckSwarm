# OSC facade — version 1

A long-lived `swarmctl serve` process exposes the SwarmLink master over **OSC 1.0 / UDP**, so any rig — QLab, TouchDesigner, a lighting desk, StageWizard's OSC network cues — can load, arm, and fire the flock without linking the Swift package. Conventions deliberately mirror the StageWizard ↔ StageWand contract (address prefix, ping-renewed subscriptions, pushed status feedback, Bonjour) so the two feel like one family of devices.

Security model, same as StageWizard's: **LAN-only, unauthenticated by design**, and only alive while `swarmctl serve` runs on the show Mac behind the dedicated show router.

## Process

```
swarmctl serve --roster roster.json --shows-dir shows/ [--osc-port 53300] [--master-port 47800] [--no-bonjour] [--quiet]
```

- Ports: OSC listens on **UDP 53300** (StageWizard uses 53100/53200; keep the family adjacent). The SwarmLink master side stays on 47800.
- Bonjour: advertises `_duckswarm._udp` on the OSC port with TXT `v=1`, `master=47800`, unless `--no-bonjour`.
- Show ids resolve exactly like the duck-agent: `<shows-dir>/<id>.duckshow.json`, then `<shows-dir>/<id>/<id>.duckshow.json`. A rig only ever names a show by id.
- Exit codes: 0 on clean shutdown (SIGINT/SIGTERM), 2 on bad arguments, 3 if the OSC or master port cannot be bound.

## Inbound commands (any sender)

| Address | Args | Effect |
|---|---|---|
| `/duckswarm/load` | `s` show-id | Resolve id, `SwarmMaster.load` to the whole roster. Replies `/duckswarm/ack` per duck. If a show is armed/playing, it is stopped first. |
| `/duckswarm/play` | optional `f` lead seconds (default 1.5) | Arm at now + lead. Requires a loaded show; otherwise `/duckswarm/error "no show loaded"`. |
| `/duckswarm/go` | — | StageWizard-style single GO: `play` with the default lead. |
| `/duckswarm/seek` | `f` show-time seconds | Seek (allowed while loaded, armed, or playing). |
| `/duckswarm/stop` | — | Graceful stop. |
| `/duckswarm/panic` | — | Panic fan-out. Always executed, never refused, from any state. |
| `/duckswarm/ping` | — | Subscribe the sender to status feedback (see below) for 5 s; re-ping to renew — identical contract to `/stagewand/ping`. |
| `/duckswarm/status` | — | One immediate full status push to the sender (does not subscribe). |

Argument leniency: an `i` where an `f` is expected is accepted; `T`/`F` are accepted for flags; extra args are ignored; a wrong-typed required arg yields `/duckswarm/error` to the sender and no action. Unknown addresses are ignored silently (logged unless `--quiet`).

Command semantics are the SwarmLink master's — repeated, ACKed, idempotent unicast to each duck. The facade adds no queueing: a second `/duckswarm/play` while armed re-arms with the new lead (a fresh `cmd_id`).

## Outbound feedback (to subscribers; also to the sender of any command)

Sent to every address that pinged within the last 5 s, at **2 Hz** while armed/playing, **0.5 Hz** otherwise, plus immediately on any transport change or ACK/NACK:

| Address | Args |
|---|---|
| `/duckswarm/status/transport` | `s` `stopped` \| `armed` \| `playing` |
| `/duckswarm/status/show` | `s` show-id or `""` |
| `/duckswarm/status/show_time` | `f` seconds (0.0 when stopped) |
| `/duckswarm/status/summary` | `i` roster size, `i` ducks reporting, `i` ducks lost |
| `/duckswarm/status/duck` | `s` duck-id, `s` role or `""`, `s` state (`idle`/`loaded`/`armed`/`playing`/`degraded`/`fault`/`lost`), `f` show_time, `f` clock_offset_ms (−1.0 when not yet synced), `i` policies_ok |
| `/duckswarm/ack` | `s` command (`load`/`play`/`seek`/`stop`/`panic`), `s` duck-id, `i` ok (1/0), `s` error or `""` |
| `/duckswarm/error` | `s` message — sent only to the offending sender |

One `/duckswarm/status/duck` message per roster duck per push. Everything fits in single datagrams; bundles are not used.

The transport returns to `stopped` on its own when the master's show clock reaches the show's `meta.duration`: the agents end playback there themselves (→ LOADED, docs/swarmlink-protocol.md §5), the master mirrors it within one 5 Hz state tick, and the change is pushed like any other — a rig sees `playing` → `stopped` (show_time `0.0`) without sending `/duckswarm/stop`.

## OSC codec

OSC 1.0 only: address pattern, `,` typetag string, 32-bit big-endian `i`, `f`, null-padded `s`, plus `T`/`F` (no payload). `b` blobs are parsed and ignored. Zero-dependency Swift, hand-rolled — port the pure encoder/decoder from StageWizard's `OSCServer.swift` (same author, MIT) rather than inventing a third one; keep the functions `nonisolated static` and unit-test them against hand-assembled byte arrays. A stdlib-only Python encoder/decoder (`python/tools/osc_send.py`) exists for tests, e2e, and quick operator checks:

```
python3 tools/osc_send.py 127.0.0.1:53300 /duckswarm/load s:demo
python3 tools/osc_send.py 127.0.0.1:53300 /duckswarm/play f:1.5
python3 tools/osc_send.py --listen 0.0.0.0:53301 --seconds 3   # print feedback
```

## End-to-end gate

`scripts/e2e_osc.sh`: two mock ducks + two duck-agents + `swarmctl serve`; `osc_send.py` pings, loads, plays; the verifier from `e2e_demo.sh` checks the intent logs, and the listener must have seen `/duckswarm/status/transport playing` and per-duck `playing` states. Runs in CI's macOS job (Swift + python3 are both present there).
