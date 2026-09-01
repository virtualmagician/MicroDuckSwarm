# SwarmLink protocol — version 1

The show-night wire protocol between the **master** (Mac running StageWizard/`swarmctl`/`showmaster.py`) and each duck's **agent**. Design rules, learned from drone shows and Falcon Player rigs:

1. The network never carries the performance — only clock, triggers, telemetry.
2. **No multicast/broadcast for anything that must arrive** (802.11 doesn't ACK it). Commands are unicast per duck, repeated, idempotent.
3. Every message is one JSON object in one UDP datagram (< 1200 bytes). Unknown fields are ignored; unknown `type` is dropped silently.
4. All protocol times are **nanosecond integers on the sender's monotonic clock** — wall clocks are never trusted.

Ports: master listens on **UDP 47800**; each agent listens on **UDP 47801**. Every message carries `"v": 1` and `"duck": "<duck-id>"` (agent messages) so one master can serve many agents from one socket.

## 1 · Time sync (agent-initiated, NTP-style)

Every 2 s (500 ms while ARMED), each agent runs an exchange:

```
agent → master  {"v":1, "type":"time_req",  "duck":"duck-01", "t0": <agent_ns>}
master → agent  {"v":1, "type":"time_resp", "t0": <echoed>, "t1": <master_ns_rx>, "t2": <master_ns_tx>}
```

Agent stamps arrival `t3`, computes `offset = ((t1−t0)+(t2−t3))/2`, `rtt = (t3−t0)−(t2−t1)`. It keeps a sliding window of the last 8 samples and uses the offset from the **minimum-RTT** sample; the estimate slews (≤ 5 ms/s) rather than steps while PLAYING. Report `clock_offset_ms` and `clock_rtt_ms` in telemetry. Target accuracy on a dedicated AP: well under 10 ms; agents flag `degraded` sync when the best-window RTT exceeds 50 ms or no sample succeeded for 10 s.

## 2 · Transport state (master → each agent, unicast, 5 Hz)

```
{"v":1, "type":"state", "seq": 421, "show": "<show-id>", "transport": "stopped"|"armed"|"playing",
 "show_time": 12.48, "master_time": <master_ns>}
```

Loss-tolerant by design (the next one comes in 200 ms); `seq` lets agents ignore reordering. While PLAYING, agents compare their local show clock against `show_time` + offset and slew out drift; a missing state stream does **not** stop local playback.

## 3 · Commands (master → agent, unicast, repeated, ACKed)

```
{"v":1, "type":"cmd", "cmd_id":"<uuid>", "cmd":"load"|"play"|"stop"|"seek"|"panic", ...}
agent → master: {"v":1, "type":"ack", "duck":"duck-01", "cmd_id":"<uuid>", "ok": true, "error": null}
```

Master sends each command up to 5× at 100 ms intervals until ACKed; agents deduplicate by `cmd_id` (re-ACK, don't re-execute).

| cmd | extra fields | agent behavior |
|---|---|---|
| `load` | `"show": id, "sha256": …, "role": "lead"` | Verify the named show file exists locally with matching hash, verify `requires.policies`, sample-check, → LOADED. NACK with `error` on any failure. |
| `play` | `"show": id, "at_master_time": <ns>, "from_show_time": 0.0` | Schedule start at local time `at_master_time − offset` → ARMED, then PLAYING. If the start is already > 0.25 s past on arrival: join in progress only if < 2 s late (seek to the correct point); otherwise stay put and report `missed_start`. |
| `seek` | `"show_time": 45.0, "at_master_time": <ns>` | Jump the local show clock (re-applying the latest `mode` event ≤ target). |
| `stop` | — | End playback gracefully: zero locomotion, `robot.stop`, → LOADED. |
| `panic` | — | Highest priority, any state: `robot.stop`, neutral head/pose, → IDLE. Never NACKed. |

Show files are distributed out-of-band before the show (rsync/scp in v1; the `load` hash check is what makes that safe).

## 4 · Telemetry (agent → master, unicast, 1 Hz; 5 Hz while PLAYING)

```
{"v":1, "type":"telemetry", "duck":"duck-01", "seq": 88, "state":"idle"|"loaded"|"armed"|"playing"|"degraded"|"fault",
 "show": "<id or null>", "show_time": 12.5, "clock_offset_ms": 1.8, "clock_rtt_ms": 4.2,
 "policies_ok": true, "battery_pct": null, "rssi_dbm": null, "last_error": null}
```

`battery_pct`/`rssi_dbm` are null until wired to `robot.health` / OS sources on real hardware. `clock_offset_ms`/`clock_rtt_ms` are **null until the first successful time-sync exchange** — masters must decode them as optional and treat null as "not yet synced", never as 0. Master marks a duck **lost** after 5 s without telemetry (preflight red; if PLAYING, the duck is presumed still performing from its local copy — that's the architecture working, not an emergency).

## 5 · Agent state machine

```
IDLE ──load──▶ LOADED ──play──▶ ARMED ──(start time)──▶ PLAYING ──(end/stop)──▶ LOADED
  ▲                                                        │
  └───────────────────────── panic (from any state) ◀──────┘
PLAYING with sync lost ▶ keep playing local copy, state="degraded", resync when beacons return.
Any robotd error ▶ FAULT: robot.stop, report last_error, accept load/panic.
```

The invariant that matters on stage: **a duck never improvises to catch up.** It performs from its local copy on its disciplined clock, joins late only within the 2 s grace, and otherwise sits the number out looking composed.
