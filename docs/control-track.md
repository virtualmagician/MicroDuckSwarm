# Timeline control track (hold / resume cues)

**Status: transport half built (2026-09-05), authored hold points not.** The design survived review at
the format level and failed it at the protocol level. This records what is
settled, what is blocked, and what has to exist first, so the next attempt does
not restart from zero.

The ask: a show can contain a point where playback stops and every duck waits,
resuming on either an operator cue or a sensor reading from a duck.

## What is settled

**The hold lives in the show file, not on the wire.** Every duck is pre-loaded
with the show, so it reaches the hold and stops locally with no network message
at all. Only *leaving* needs a message. That is the only shape where a dropped
packet cannot leave one duck playing while the rest stand still, and it follows
directly from the architecture's existing "pre-load the show, synchronise only
clocks" decision.

**It is show-level, never per-role.** A per-role hold *is* the cast splitting,
written into the format. The block is a sibling of `requires`:

```json
"control": {
  "holds": [
    { "id": "applause", "t": 42.0, "label": "hold for applause",
      "pose": "freeze",
      "auto_resume": { "timeout_s": 45.0, "on_timeout": "resume" } }
  ]
}
```

**A hold occupies zero show-time.** The clock freezes, so curve interpolation,
event windows and skill occupancy are untouched downstream of the sampler.
`python/duckshow/sampler.py` needs no functional change.

**Resume is a re-anchor of the play epoch, not a `total_held` accumulator.**
`_start_playing_now(show_time, epoch_local_ns)` in `python/duck_agent/agent.py`
already does exactly this. Keeping the clock a pure affine function of one
epoch means `_current_show_time`, the drift slew and telemetry need no new
term.

**The operator cue is unconditional and never declared in the file.** Any hold,
in any state, releases on an operator resume — the same class of rule as "panic
always works from any state". It removes the whole failure family where a
sensor never fires and the file says the operator may not intervene.

## What blocks it

Three independent designs were each attacked through three lenses. 57 flaws
were raised; one design was rated unsound outright. They were almost all about
components that did not exist rather than about the format, which is why the
first two prerequisites below were buildable on their own.

**Six of the eight are now closed** by the operator pause and the load gate
(marked below). Blockers 4 and 8 are specific to *authored* hold points and
remain open.

### 1. There is no operator resume path, and the obvious button is destructive — CLOSED

`docs/osc-facade.md`'s inbound verbs are load/play/go/seek/stop/panic/ping/status.
There is no resume. `/duckswarm/go` maps to `play`, and `SwarmMaster.play` starts
from `cueShowTime`, which `haltTransport` resets to 0 — so the one cue an
operator has to hand **restarts the number from the top**. The feature as
designed would ship with no way to release a hold and one very available way to
destroy the show. `architecture.md` makes the OSC facade *the* integration seam,
so this is the surface that matters, not an afterthought.

### 2. Resume is not idempotent across two presses, and the master cancels its own retries — CLOSED

A second GO produces a second `cmd_id` with a different `at_master_time`, which
re-anchors to a *different* epoch. Worse, `SwarmMaster.fanOut` opens with
`supersedeInFlightCommands()`, and `sendWithRetry` bails `.superseded` as soon
as a newer command is issued — so a second resume cancels the first one's
remaining retries **to exactly the duck that had not yet received it**.

### 3. A NACK ends the retry ladder in both masters — CLOSED (both commands park)

`showmaster.py` breaks out of its retry loop on any ack, and
`SwarmMaster.swift` returns `.nacked` identically. A NACK is an ACK as far as
retry is concerned. So a resume validated at arrival (`only accepted when the
duck is currently held at this id`) strands any duck whose clock has not yet
crossed `hold.t` — one early NACK, held forever. `_handle_seek` already avoids
this by parking the command and letting the tick loop apply it; resume must do
the same.

### 4. Entry is edge-triggered on the duck and level-triggered on the master — OPEN

The agent would enter a hold by crossing `(_last_processed_show_time, show_time]`.
The master enters on `showTime >= hold.t`. But `_begin_playback` jumps the clock
forward whenever a join is more than 0.25 s late, and `_start_playing_now` then
seeds `_last_processed_show_time` past the gap — so a late-joining duck **skips
the hold entirely and performs alone**. A cast split produced by normal,
documented late-join behaviour rather than by operator error.

### 5. Adding a `held` transport silently kills the state stream — CLOSED

`SwarmMaster.publishStateTick()` opens with
`guard transport == .armed || transport == .playing else { return false }`, and
returning false tears the 5 Hz loop down. Adding `.held` therefore stops the
very telemetry a hold needs to be visible. It is a `==` comparison, so the
compiler will not flag it.

### 6. Seek during a hold strands the duck, and seek is the recommended recovery — CLOSED

Nothing in the proposed design clears the hold state on the seek-apply path, so
a duck seeked out of a stuck hold stays frozen at `hold.t` forever while the
cast plays on. `MasterClock`'s frozen show-time has the mirror problem: it
survives `stop()`, `panic()` and the next `play()`.

### 7. `requires.features` cannot gate the population it exists to gate — CLOSED by the load gate instead

The proposal was to stay at `duckshow/1` and add
`requires.features: ["control.holds"]`. But an agent that predates the field
ignores it *by construction* — `_parse_requires` reads only `policies` — so the
gate is a no-op on exactly the ducks it is meant to catch. It was also claimed
to "already block preflight": `grep -rn preflight SwarmLink/Sources python/`
returns four comments and no code, and `architecture.md` defers the preflight
dashboard to M2 because it cannot be designed honestly before real telemetry
exists.

Bumping to `duckshow/2` is not the alternative it looks like: all three loaders
test the major for **equality**, not "≤ supported", so flipping the constant
does not add hold support, it drops every show that exists. CLAUDE.md rule 4
requires a loader migration, which no design provided.

### 8. The master ends the show without telling anyone — OPEN

`publishStateTick` halts at `showTime >= duration` and deliberately fans out
nothing, because every agent's clock reaches duration on its own. A held
agent's never does. So a duck stranded in a hold is stranded permanently, and
the operator's recovery path closes the moment the master's clock passes the
end of the show.

## Sensor-triggered resume is blocked on hardware, not on design

**Asked upstream 2026-09-05.** Pollen have been emailed about this and about `roulade`'s chain partner; until they answer, the two remain open for the reason below rather than for want of asking.

`docs/robotd-api.md` gives duck-agent no verified sensor surface.
`robot.subscribe` → `robot.state` (`joints`, `targets`) and `robot.health` are
the only real per-duck readings, and ToF lives in a **different daemon**
(`tof.stream`/tofd), listed there as not used by duck-agent v1. Hard rule 3
forbids inventing the method.

The recommendation was to ship the *shape* now (`auto_resume.sensor`) with a
closed enum that is empty in v1, and a validator warning in the same words the
`servo` track already uses: "not honored in v1; this hold resumes only on an
operator cue". Whatever the mechanism, evaluation belongs to the **master**,
from telemetry — a duck deciding locally when to resume is the cast splitting
again.

## What has to exist first

1. ~~**`/duckswarm/resume` in the OSC facade and the master**, with a real
   transport state and a resume that parks rather than validating at
   arrival.~~ **Done, 2026-09-05.** Shipped as an operator pause, which is
   useful on its own and is the exact mechanism an authored hold will trigger
   locally rather than a second one. `pause`/`resume` wire commands, a
   `paused` transport, `/duckswarm/pause`, `/duckswarm/resume`,
   `swarmctl pause|resume`, `showmaster.py pause|resume`. Blockers 1, 2, 3, 5
   and 6 are closed by it: both commands park rather than validating on
   arrival, resume is idempotent by state so a second GO cannot re-anchor, the
   5 Hz state stream survives a pause, seek while paused moves the frozen
   point instead of stranding the duck, and stop/panic/load all clear a
   freeze.
2. ~~**A load-outcome gate on play.**~~ **Done, 2026-09-05.** `play()` refuses
   when any duck's most recent `load` did not succeed, overridable only
   deliberately and not from the OSC surface. Blocker 7's real content: it is
   the honest version of what `requires.features` was being asked to do, and
   it works on old agents because it is enforced entirely at the master.
3. **Still open: the format block, authored hold entry, and the editor UI.**
   Blocker 4 (edge-triggered on the duck, level-triggered on the master) and
   blocker 8 (the master ends the show without telling anyone, so a held duck
   is stranded) are both specific to *authored* holds and are untouched by the
   operator pause. They must be answered before `control.holds` is
   implemented.

## One real bug already fixed from this review

The first draft proposed freezing the cast by reusing the agent's existing
`servo {"mode": "hold"}` path. That path did not freeze anything: it skipped
the locomotion block entirely, emitting **no `robot.move` at all**, so a duck
coasted at its last commanded velocity until robotd's 500 ms deadman caught
it. Fixed, with tests that fail without the fix — see
`python/tests/test_agent_servo_hold.py`. Had the control track shipped on that
path, it would have inherited the bug at exactly the moment eight ducks are
meant to stand still together.

## Naming

`hold` already means two other things in this format (`sound.hold` seconds,
`servo.mode == "hold"`) and roughly forty in `agent.py`. `control.holds` is
unambiguous in context and is the word the feature was asked for in, but every
*code* symbol should be named distinctly (`HoldPoint`, `_hold_point`,
`_enter_hold_point`) so grep still works. `control.waits` is the alternative if
the collision proves annoying in practice.
