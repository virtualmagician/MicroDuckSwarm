# SwarmLink protocol — version 1

The show-night wire protocol between the **master** (Mac running StageWizard/`swarmctl`/`showmaster.py`) and each duck's **agent**. Design rules, learned from drone shows and Falcon Player rigs:

1. The network never carries the performance — only clock, triggers, telemetry.
2. **No multicast/broadcast for anything that must arrive** (802.11 doesn't ACK it). Commands are unicast per duck, repeated, idempotent.
3. Every message is one JSON object in one UDP datagram (< 1200 bytes). Unknown fields are ignored; unknown `type` is dropped silently.
4. All protocol times are **nanosecond integers on the sender's monotonic clock** — wall clocks are never trusted.

Ports: master listens on **UDP 47800**; each agent listens on **UDP 47801**. Every message carries `"v": 1` and `"duck": "<duck-id>"` (agent messages) so one master can serve many agents from one socket.

## 0 · Who the agent will listen to

An agent binds `0.0.0.0` and, by default, adopts as its master the source of
the first `time_resp`, `state` or `cmd` it receives. **There is no
authentication on this protocol.** Any host that can reach the agent's port can
drive the duck: send a `cmd` and it is executed, send a `time_resp` and the
show clock moves, send a `puppet` frame and the duck moves without the sender
ever becoming its master. On a venue network shared with anything else, that is
the whole attack surface.

`--master-host` (`MASTER_HOST` in `/etc/duckswarm/agent.env`) narrows it. When
set, the agent resolves it once at startup and **drops every inbound datagram
that did not come from one of those addresses**, before parsing and before
dispatch, so a foreign `time_resp` cannot poison the clock and a foreign `cmd`
cannot drive the duck. Unset, the agent behaves as before and learns its master
from whoever speaks first.

Three properties of the pin, each chosen deliberately:

* **It pins the host, not the port.** `--master-port` is only where the agent
  sends *before* it has heard from a master; after that it replies to the
  master's own source port. Both reference masters do send from a fixed port
  (`SwarmMaster` pins every connection's local endpoint to `masterPort`), so
  matching it would usually work, and is still not worth doing: the agent's
  `--master-port` is a separate config value that can disagree with the port
  the master was actually started on, so matching would strand a duck over a
  mismatched number. It buys nothing either way, because anything that can
  occupy the host can send from any port on it.
* **Anything that does not resolve to an IPv4 address disables the pin rather
  than the duck.** The agent's socket is `AF_INET`, so every source it can ever
  see is a dotted quad; resolution is constrained to that family and the result
  is filtered to it again, so a v6 `MASTER_HOST` cannot produce a pin no
  datagram can match. A resolver failure, a malformed hostname label (which
  raises `UnicodeError` from the IDNA codec, not `gaierror`) and a v6-only host
  all land on the same path: log an error, run unpinned. A duck that refuses
  every master is worse on a stage than one that accepts any, because "panic
  always works from any state" is rule 5 and a pin that can brick a cast is not
  a safety feature.
* **Resolution happens once, at startup.** A master that changes address
  mid-show stops being heard. That is the cost of not doing a DNS round trip in
  front of every command on a network whose DNS may not answer at all. The
  recovery is to restart the agent, or to unset `MASTER_HOST`.
* **It is a filter, not an identity check.** Source addresses are forgeable on
  a network an attacker already sits on, and a pin can still name the wrong
  host if the configured address is simply wrong. This raises the bar from
  "anyone who can reach the port" to "anyone who can reach the port and forge
  the master's address", and it stops there. A shared secret on `cmd` and
  `puppet` is the real answer. It is named as a gap in `docs/fleet.md` and has
  not been designed.

## 1 · Time sync (agent-initiated, NTP-style)

Every 2 s (500 ms while ARMED), each agent runs an exchange:

```
agent → master  {"v":1, "type":"time_req",  "duck":"duck-01", "t0": <agent_ns>}
master → agent  {"v":1, "type":"time_resp", "t0": <echoed>, "t1": <master_ns_rx>, "t2": <master_ns_tx>}
```

Agent stamps arrival `t3`, computes `offset = ((t1−t0)+(t2−t3))/2`, `rtt = (t3−t0)−(t2−t1)`. It keeps a sliding window of the last 8 samples and uses the offset from the **minimum-RTT** sample; the estimate slews (≤ 5 ms/s) rather than steps while PLAYING. Report `clock_offset_ms` and `clock_rtt_ms` in telemetry. Target accuracy on a dedicated AP: well under 10 ms; agents flag `degraded` sync when the best-window RTT exceeds 50 ms or no sample succeeded for 10 s.

## 2 · Transport state (master → each agent, unicast, 5 Hz)

```
{"v":1, "type":"state", "seq": 421, "show": "<show-id>", "transport": "stopped"|"armed"|"playing"|"paused",
 "show_time": 12.48, "master_time": <master_ns>}
```

Loss-tolerant by design (the next one comes in 200 ms); `seq` lets agents ignore reordering. While PLAYING, agents compare their local show clock against `show_time` + offset and slew out drift; a missing state stream does **not** stop local playback.

## 3 · Commands (master → agent, unicast, repeated, ACKed)

```
{"v":1, "type":"cmd", "cmd_id":"<uuid>", "cmd":"load"|"play"|"stop"|"seek"|"pause"|"resume"|"relax"|"panic", ...}
agent → master: {"v":1, "type":"ack", "duck":"duck-01", "cmd_id":"<uuid>", "ok": true, "error": null}
```

Master sends each command up to 5× at 100 ms intervals until ACKed; agents deduplicate by `cmd_id` (re-ACK, don't re-execute).

| cmd | extra fields | agent behavior |
|---|---|---|
| `load` | `"show": id, "sha256": …, "role": "lead"` | Verify the named show file exists locally with matching hash, verify `requires.policies`, sample-check, → LOADED. NACK with `error` on any failure. |
| `play` | `"show": id, "at_master_time": <ns>, "from_show_time": 0.0` | Schedule start at local time `at_master_time − offset` → ARMED, then PLAYING. If the start is already > 0.25 s past on arrival: join in progress only if < 2 s late (seek to the correct point); otherwise stay put and report `missed_start`. |
| `seek` | `"show_time": 45.0, "at_master_time": <ns>` | Jump the local show clock (re-applying the latest `mode` event ≤ target). |
| `stop` | — | End playback gracefully: zero locomotion, `robot.stop`, → LOADED. |
| `pause` | `"at_master_time": <ns>` | Freeze the show clock at the instant given, holding show-time where it was. Keeps ticking at 50 Hz with locomotion commanded to **zero** (never silence — see below), so head/pose/mouth hold their frozen values. → PAUSED. |
| `resume` | `"at_master_time": <ns>` | Un-freeze at the instant given: re-anchor the show clock so show-time continues from exactly where it stopped. → PLAYING. |
| `relax` | `"on": true` (default) | `on: true` makes the duck safe to pick up: neutral head/pose/mouth, then `robot.relax`. `on: false` re-torques it (`robot.enable`). Refused while armed, playing or paused. Reports `relaxed` in telemetry. |
| `panic` | — | Highest priority, any state: `robot.stop`, neutral head/pose, → IDLE. Never NACKed. |

### Relax: a state that is safe to pick up

Repositioning the cast by hand is an ordinary part of running a show with
chapters, and until now there was no state in which doing so was safe.
`stop` sends `robot.move {0,0,0}` and `robot.stop`, and `docs/robotd-api.md`
is explicit that this leaves the duck **standing**: "it does not go limp or
collapse". So the operator would be lifting a powered, actively balancing
robot, frozen in whatever head and pose the last frame left it, with its bill
open if the show happened to end mid-`mouthOpen`.

`relax` neutralises head, pose and mouth, then calls `robot.relax`. The duck
reports `relaxed: true` in telemetry until something re-torques it. Three
things do: `relax` with `on: false`, a `play` (which sends
`robot.enable {on: true}` before it arms), and a robotd reconnect, since the
agent cannot know what a fresh daemon did to the torque state and says so
rather than claiming a torque state it did not set.

Relax is deliberately **not** part of `stop`: stopping is how a number ends
and the cast should still be standing afterwards; going limp is a separate
decision an operator makes when they are about to touch a duck. It is refused
while armed, playing or paused for the same reason in reverse: there is no
reading of "go limp mid-number" that an operator can have meant, and the
failure mode is a duck on the floor.

**A relaxed duck ignores the puppet stream.** Puppet packets are the setup-mode
live drive (§5); forwarding them to a duck whose torque is off would command
motion that cannot happen and would leave the editor's ghost showing a duck
somewhere the real one is not. They are dropped, logged, and not buffered.
Re-torque first (`relax on: false`), then drive.

**The physical effect of `robot.relax` is inferred, not documented.**
`docs/robotd-api.md` gives its signature (`{}` → ack) and nothing more. The
name and the deadman note together read as "release torque", which is what
this assumes, and `robot.enable {on: true, toggle: false}` is assumed to be its
inverse. `python/mock_duck/server.py` models both that way (`robot.relax`
clears `enabled` and zeroes the last move; `robot.enable` sets it) so the
behaviour is testable, but it is one of the first things to check against a
real duck,
because a duck that goes limp while standing falls over rather than becoming
handleable. Until then, treat `relax` as "ask robotd to make this duck
handleable" and confirm before trusting it with hardware.

### Pause and resume

An operator pause is the transport half of the timeline control track
(`docs/control-track.md`); authored hold points will trigger the same
mechanism locally rather than a second one. Three rules make it survivable on
a stage:

**Both commands are parked, not validated on arrival.** `pause` and `resume`
carry `at_master_time` and are applied by the tick loop at that instant, the
way `seek` already is. A command that arrives before the duck is in the state
it names is *not* NACKed — NACKing would be fatal, because both reference
masters break their retry loop on any ACK, NACK included, so one early NACK
would strand one duck forever.

**Resume is idempotent by state, not just by `cmd_id`.** A resume for a duck
that is already playing is ACKed and does nothing. `cmd_id` dedup only covers
retries of one command; two distinct GO presses are two `cmd_id`s, and without
the state check the second would re-anchor the clock to a different epoch and
split the cast. The same holds for a second `pause`.

**A pause commands zero, it never goes silent.** Emitting no `robot.move`
would leave the duck coasting on its last velocity until `robotd`'s 500 ms
deadman caught it. The deadman is a safety net for a lost link, not the
mechanism choreography stops a duck with.

`seek` while paused re-targets the frozen position and stays paused, so
scrubbing during a hold behaves the way an operator expects. `stop`, `panic`
and `load` all clear the pause outright.

Show files are distributed out-of-band before the show (rsync/scp in v1; the `load` hash check is what makes that safe).

### The master must not play over a failed load

`load` NACKs are per-duck and they are the whole point of the hash check, so a
master that fans out `load`, collects the outcomes and then plays regardless has
thrown away the only safety this protocol has against a duck performing the
wrong show. A duck that NACKed keeps whatever show it had loaded before, and it
will happily accept a `play` naming that older show id. That is a cast split
produced by the master, not by the network.

**`play` refuses when any duck's most recent `load` did not succeed**, and the
error names them. The refusal covers every non-OK outcome, not just an explicit
NACK: a timeout, a connection failure and a superseded command all mean the same
thing, which is that the master does not know what that duck is holding.

An operator can override deliberately — a duck that is genuinely off the roster
tonight should not be able to block the show — but it has to be said out loud.
In SwarmLink that is `play(allowingFailedLoads: true)`, and on the CLI
`swarmctl play --allow-failed-loads`.

**The override is deliberately not on the OSC surface.** Bypassing this gate
means a duck performs a different show, and that should not be one fat-fingered
cue away on a lighting desk. `/duckswarm/play` reports the refusal and names the
ducks; releasing it is a decision taken at the console, not in a cue stack.

The outcomes are cleared by the next `load`, so the gate always reflects the
show that is actually about to play, never a stale verdict.

## 4 · Telemetry (agent → master, unicast, 1 Hz; 5 Hz while PLAYING)

```
{"v":1, "type":"telemetry", "duck":"duck-01", "seq": 88, "state":"idle"|"loaded"|"armed"|"playing"|"paused"|"degraded"|"fault",
 "show": "<id or null>", "show_time": 12.5, "clock_offset_ms": 1.8, "clock_rtt_ms": 4.2,
 "policies_ok": true, "relaxed": false, "battery_pct": null, "rssi_dbm": null, "last_error": null}
```

`relaxed` says whether this duck's torque is released — which ducks are safe to pick up right now, per duck, rather than something the operator has to remember. `battery_pct`/`rssi_dbm` are null until wired to `robot.health` / OS sources on real hardware. `clock_offset_ms`/`clock_rtt_ms` are **null until the first successful time-sync exchange** — masters must decode them as optional and treat null as "not yet synced", never as 0. Master marks a duck **lost** after 5 s without telemetry (preflight red; if PLAYING, the duck is presumed still performing from its local copy — that's the architecture working, not an emergency).

## 5 · Agent state machine

```
IDLE ──load──▶ LOADED ──play──▶ ARMED ──(start time)──▶ PLAYING ──(end/stop)──▶ LOADED
                                                         │  ▲
                                                    pause│  │resume
                                                         ▼  │
                                                        PAUSED
  ▲                                                        │
  └───────────────────────── panic (from any state) ◀──────┘
PLAYING with sync lost ▶ keep playing local copy, state="degraded", resync when beacons return.
Any robotd error ▶ FAULT: robot.stop, report last_error, accept load/panic.
```

The invariant that matters on stage: **a duck never improvises to catch up.** It performs from its local copy on its disciplined clock, joins late only within the 2 s grace, and otherwise sits the number out looking composed.

## 6 · Puppet stream (master → one agent, unicast, ≤ 50 Hz, unacknowledged)

```
{"v":1, "type":"puppet", "seq": 1042, "master_time": <master_ns>,
 "move": {"vx": 0.1, "vy": 0.0, "vyaw": 0.0}, "head": {"neck_pitch": 0, "head_pitch": -0.2, "head_yaw": 0, "head_roll": 0},
 "pose": {"z": 0, "roll": 0, "pitch": 0, "active": false}, "mouth": {"open": 0.0},
 "do": "kick_left", "sound": "chirp"}
```

Every field except `seq` is optional; a packet carries only what the sender wants to assert this tick. Loss-tolerant by design (the next packet comes in 20 ms), so no ACK and no retry; agents drop packets with a `seq` ≤ the last one seen, and forget the last `seq` after 2 s of silence so a restarted sender is never locked out (senders seed `seq` from a millisecond clock anyway). A packet is **fresh for 250 ms** (the deadman), tracked per channel: while fresh, `move` is forwarded directly in IDLE/LOADED and *added* to the timeline's locomotion while PLAYING (clamped to the validation limits), and `head`/`pose`/`mouth` override the timeline; a channel the sender stops asserting is released 250 ms later, and when the whole stream goes stale, puppet influence is removed — locomotion zeroed at once when not playing. `do`/`sound` fire once per `seq` that carries them. Values are validated against the same limits as show files; out-of-range packets are clamped, malformed ones dropped. Telemetry adds `"puppet": true|false`. Panic, stop, and load are unaffected by puppet traffic and always win: after a panic or stop the agent ignores the stream until it has been quiet for one deadman period, so a sender must stop streaming (not merely zero its sticks) to regain control. See `docs/authoring.md`.
