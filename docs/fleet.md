# The fleet: enrolment, naming, identification and deployment

**Status: designed, not built (2026-09-05). Nothing in this document exists as
code yet.** It is the plan for five questions that `deploy/` and
`docs/provisioning.md` answer only partly:

1. how a `.duckshow` and a `.duckset` reach the robots
2. how a robot joins the fleet and gets a name
3. whether one action deploys to every connected robot
4. whether the deployed unit is a bundle
5. how a human tells which physical duck is which

What exists today is in `docs/provisioning.md`: `provision_duck.sh`,
`deploy_shows.sh`, `push_policy.sh`, a systemd unit, and a hand-written roster.
That is a working single-duck story. This document is what turns it into a
fleet story, and it starts by recording what is broken in the current one.

## What is wrong today

Each of these was verified against the code, not inferred.

**One sleeping duck stops the deploy for every duck after it.**
`deploy/deploy_shows.sh` runs `set -euo pipefail` (line 37) and calls
`rsync_push` in the per-host loop with no failure capture (lines 143 to 153),
unlike the hash-check loop below it which sets `host_ok=0` and continues. Hosts
are processed strictly sequentially. So the eighth duck being asleep is
discovered after seven pushes, and ducks nine and ten never get the show.

**The roster's identity fields are discarded at deploy time.**
`deploy_shows.sh` resolves a roster down to its unique `host` values and throws
away `id`, `role` and `port`. A stale id-to-host mapping therefore deploys to
the wrong duck and reports success.

**Setlists are already being shipped to ducks that cannot read them.**
`RSYNC_ARGS` excludes only `fixtures/`, `policies/` and `.DS_Store` (line 118),
so `shows/setlists/example.duckset.json` goes to every duck. Nothing there can
read it: `provision_duck.sh` uploads the `duck_agent` and `duckshow` packages
and not `duckset`.

**The master discards the name the duck reports.**
`SwarmMaster.ingest` builds its `DuckTelemetry` with `duck: duckID`, the
identity of the connection it dialled, and never compares it against
`message.duck`. `showmaster.py` does the opposite and keys off the wire field
with no address check. So a board provisioned as `duck-03` sitting at the
address the roster calls `duck-02` reports healthy as `duck-02`, the two
masters disagree about the same fleet, and nothing detects it until a show
looks wrong.

**`--master-host` does not pin anything.** `_configured_master_addr` is read
exactly once, to seed `master_addr` (`agent.py:188`), and
`_set_master_addr` then overwrites it unconditionally from any inbound
packet's source. Combined with the agent binding `0.0.0.0` and commands
carrying no authentication, any host on the venue network can become the
master. This is the one item here that should be fixed before anything new
sends commands to a duck.

**The master's belief that a duck is loaded never expires.**
`lastLoadOutcomes` is written only by `load()` and read only by
`ducksWithFailedLoads`. Nothing invalidates it, so a duck that has since
panicked (which clears its show entirely) still counts as loaded, and the play
gate that exists to prevent a cast split waves it through.

**A roster accepts two entries pointing at the same duck.** `RosterError` has
`duplicateDuckID` and no equivalent for a duplicate `(host, port)`.

**Two ducks can hold the same cast role, or a role can be uncovered.**
`SwarmMaster.load` checks only that each entry's role is in the show's cast. It
checks neither uniqueness nor coverage, and `octet`'s eight role names are not
positional, so nothing catches it.

**Two docs describe a component that does not exist.**
`docs/duckshow-format.md:140` says SwarmLink pushes the `.onnx` to each duck,
patches `robotd.toml` and restarts `robotd`. `deploy/push_policy.sh` does that,
one host at a time, over ssh. SwarmLink contains no ssh at all.

## 1. The fleet file (questions 2, 3 and 5)

One file, and it **is** the SwarmLink roster, extended with optional fields
only. Swift's synthesised `Codable` ignores unknown keys, so today's
`[RosterEntry].load`, `deploy_shows.sh` and `roster_hosts` read it unchanged.
The top level stays a bare JSON array.

```json
[
  {
    "id": "duck-03",
    "host": "duck-03.local",
    "port": 47801,
    "role": "lead",
    "cast": { "octet": "wing-l", "showcase": null },
    "label": "orange band",
    "notes": "chipped beak",
    "hw": { "fp": "9f2c1a4e77d3", "src": "device-tree-serial" },
    "active": true
  }
]
```

`id` is required and uses the same charset `provision_duck.sh --duck-id`
already enforces. It is the wire name, the deploy key and the sticker text, and
it is never typed twice, because enrolment writes the board and the fleet entry
in one action. `cast` maps a show id to a role, so one fleet file serves a
setlist whose shows have different casts. An explicit `null` means uncast.
Resolution order for a load of show S: `cast[S]` if the key is present, else
`role`, else uncast.

**Where it lives is forced by CLAUDE.md rule 6.** A fleet file is a list of
addresses on a venue network, so it lives outside the repo:
`--fleet`, then `$DUCKSWARM_FLEET`, then `~/.duckswarm/fleet.json`. The repo
gains `.gitignore` entries for `fleet.json`, `*.fleet.json`, `roster.json` and
`*.roster.json`, a committed `fleet.example.json` using `192.0.2.x`
documentation addresses, and a test asserting `git ls-files` matches no fleet
or roster file. Rule 6 then holds because the test gate enforces it, not
because someone remembered.

`python/tools/fleet.py` (stdlib) gets `add`, `list`, `set`, `remove`, `verify`,
`roster` and `doctor`. `add` refuses a duplicate id, label or host. `verify`
ssh's to each active entry and reports three columns: reachable, `DUCK_ID` in
`agent.env` matches, hardware fingerprint matches the pin. `doctor --unpin`
clears a stale pin, because a pin that cannot be cleared becomes the thing that
stops a show.

## 2. Telling which duck is which (question 5)

Five layers, deliberately separate. Today all five collapse into one string.

**Hardware fingerprint.** Probed on the duck, first hit wins, source recorded:
`/proc/device-tree/serial-number`, then the first permanent MAC under
`/sys/class/net`, then `/etc/machine-id`. **Which of these exists and is stable
on a stock Armbian image for the Radxa Zero 3W is unverified**, including
whether that WiFi part randomises its MAC. Fix the order on duck number one and
write the answer here. `robotd` offers nothing: `docs/robotd-api.md` has no
identity method in the `robot.*` namespace.

**Duck id.** Unchanged: written once into `/etc/duckswarm/agent.env`, never
rewritten without `--force-config`.

**The wire-id gate.** The correctness fix from the list above, and nearly free.
On ingest, compare `message.duck` against the connection's `DuckID`. On
mismatch, mark the duck, surface it in `swarmctl status`, and fail that duck's
load so the play gate stops the show. `showmaster.py` gains the reciprocal
source-address check so the two masters stop disagreeing. Testable today
against `TestDuck`.

**The sticker and the band.** A sticker under the base carrying the duck id in
exactly the characters of `DUCK_ID`, applied at enrolment while the duck is
chirping its own number, plus a coloured band recorded in the entry's `label`.
This is the primary mechanism and no software replaces it: at load-in the ducks
are in a case, powered off, and the network does not exist yet. Cast role goes
on floor tape, never on the duck, because the same body plays a different part
next week.

**The `identify` command.** One new wire verb, and the only one this plan adds.

```
{"v":1, "type":"cmd", "cmd_id":"...", "cmd":"identify", "count":3, "tag":"chirp"}
```

`count` is sent by the master, defaulting to the duck's position in the fleet's
sorted ids, so the duck never parses its own name. The `SoundTag` enum has
seven members for eight to ten ducks, so tags cannot uniquely name a duck;
counting can. Allowed in idle, loaded and fault, refused while armed, playing
or paused, exactly as `relax` is. The sequence is `robot.sound {tag}` `count`
times, then one `greet` as a terminator so a half-heard count is unambiguous.
When not relaxed it adds a head yaw waggle ending neutral. **Never
`robot.move`, in any state**, because the ducks are standing on marks. When
relaxed it is sound only and the ACK says so, because silently re-torquing a
duck someone is holding is not acceptable.

Two constraints found by review that shape the implementation:

- It must be sent as a **direct unicast, not through the command machinery**.
  Every command path calls `issueCommand()` and `fanOut` opens with
  `supersedeInFlightCommands()`, so an identify would cancel an in-flight load.
  The `puppet(duck:frame:)` path is the model.
- The standalone enrolment tool must **not** let the agent adopt its address as
  the master. `_handle_cmd_message` calls `_set_master_addr(addr)` on its first
  line. Identify has to be handled before that call, acked to the sender, and
  leave `master_addr` alone, the way the puppet path already deliberately does.

Every method and tag used is confirmed in `docs/robotd-api.md` at API v17.
There is no LED or display method anywhere in that file, so nothing here
assumes one.

## 3. The bundle (questions 1 and 4)

`duckbundle/1`. A bundle is a **directory with a manifest, not an archive**:
rsync delta transfer matters more on venue WiFi than a single-file transfer,
there is no unpacker to guarantee on a stock image, no SD-card temp space is
consumed, there is no half-unpacked state, and it can be inspected with `ls` and
`cat` at 6pm.

The bundle root **doubles as the duck's `--shows-dir`**, which is the load-
bearing decision: today's unmodified agent reads a bundle with no code change,
because it already resolves an id to `<shows-dir>/<id>.duckshow.json` or
`<shows-dir>/<id>/<id>.duckshow.json` and resolves `requires.policies[].file`
relative to the same directory.

```
opening-set-20260905T142211Z/
  bundle.json
  octet/octet.duckshow.json
  demo/demo.duckshow.json
  policies -> ../../policies
```

The manifest carries a `digest` over the sorted file hashes, so a rebuild of
identical content anywhere produces the same digest, and it carries no
hostname, user, venue or absolute path. Contents are an **allowlist**, which is
what stops the accident where `deploy_shows.sh` ships `.duckset` files to ducks
that cannot read them. Never in a bundle: the fleet file, audio (nothing on a
duck resolves `meta.music.file`), setlists, fixtures, bakes.

`tools/bundle.py` refuses to pack unless every show parses, passes
`duckshow.validate` with zero errors, has a positive `meta.duration` mirroring
the agent's own load gate, declares only confirmed policy slots, and has an id
equal to its filename stem. Each invariant moves a failure from load-in in
front of an audience to a build on a laptop.

**Policies are declared, not carried.** The bundle names the `.onnx` it needs
with a hash and a slot; the bytes still travel by `push_policy.sh`. A show
deploy must never restart `robotd`: that is the single most dangerous operation
in the path, it has never run against a real `robotd`, and no show in the repo
has a non-empty `requires.policies`. What the bundle buys instead is that the
ordering rule, currently a paragraph a human has to remember, becomes a gate:
the deploy refuses to flip a duck whose declared policies are not already
installed, and prints the exact `push_policy.sh` line.

**One path move, cheap only because nothing has ever been provisioned.**
`DUCKSWARM_POLICIES_DIR` is `/var/lib/duckswarm/shows/policies` today, which
after a symlink flip would sit inside a version-swapped tree while
`robotd.toml` holds an absolute path into it. It moves to
`/var/lib/duckswarm/policies`, outside the versioned tree, and the in-bundle
relative symlink satisfies the agent's relative resolution.

**Honest note on proportionality.** The bundle does **not** improve per-show
integrity; the existing per-show sha256 already proves every ACKing duck holds
byte-identical bytes. It earns its keep on four other things: whole-set
verification before doors, policy ordering enforced instead of remembered, an
inventory answer to "what does duck-05 hold", and a rollback that is a symlink
flip rather than a git checkout plus a full re-rsync. If setlists and custom
policies are never used, the bundle is overhead and the fleet file, the
identity gate and `identify` still stand on their own.

## 4. One-click deploy (question 3)

Yes, with a barrier. One command, or one button, running the same code:

```bash
python3 tools/deploy_bundle.py --fleet ~/.duckswarm/fleet.json --bundle dist/bundles/opening-set-20260905T142211Z
```

Stdlib Python rather than bash, because the fan-out needs bounded parallelism,
per-host isolation and a barrier, and macOS ships bash 3.2 with no `wait -n`.
`swarmctl` never learns ssh, and the deploy path never requires a Swift build,
because a bundle deploy is often what you are doing when the Swift build is
what broke.

0. **Pack.** Local, no network, all invariants.
1. **Preflight**, per duck, in parallel. Reachable, sudo works, disk space,
   `DUCK_ID` matches the fleet id, fingerprint matches the pin, every declared
   policy present with the right hash, telemetry state not armed or playing. A
   duck already on this digest is reported "already current" and skipped, so a
   redeploy is a no-op. **A failure marks that duck and does not stop the run.**
2. **Stage** into `.staging-<name>-<version>` with `--link-dest` against the
   live bundle, so a one-show edit transfers one show. Nothing running is
   touched.
3. **Verify on the duck**, against the manifest, on its own disk.
4. **Publish**: rename, then read-only.
5. **The barrier.** Print the table. Nothing has changed on any duck yet, and
   nothing flips unless every target reached publish. `--allow-partial` exists
   for a genuinely dead duck and names, loudly, which ducks stay behind.
6. **Flip**: record `.previous`, swap the `shows` symlink atomically, confirm
   it resolves to a directory containing `bundle.json`, restore on failure.
7. **Report**, per duck, by id **and** by label, so the operator knows which
   physical bird to go and look at.

**Deploying is still not loading.** That separation is correct and stays. The
proof remains `swarmctl load`, which runs the full local admission test on every
duck and refuses to play over a failed load. The last line of a successful
deploy says to re-run it, because a flip does not change what a duck holds in
memory.

**Rollback** is `deploy_bundle.py --rollback`: the previous bundle is already on
disk, so recovery at 19:50 is one symlink rename per duck in parallel, with no
repo and no build. Today the same recovery is a git checkout plus a full
re-rsync over venue WiFi.

## Phasing

Everything below is buildable against the mock duck unless marked otherwise.

| # | Step | Why it stands alone |
|---|---|---|
| 0 | Doc corrections, `.gitignore` and the repo-hygiene test. Make `--master-host` actually pin. | Stops two docs lying, makes rule 6 enforced by the gate, closes the "any host is the master" hole. |
| 1 | `docs/fleet.md` schema and `python/tools/fleet.py` | The roster stops being an undocumented hand-edited file with no home, no validation and no tool. |
| 2 | SwarmLink roster: optional `port` and `role`, the per-show `cast` map, `label` and `active` carried through | `demo` casts 2 roles, `octet` 8, `showcase` 1, so a setlist mixing them cannot run today at all. |
| 3 | `identify` end to end, doc first | You can find out which bird is which, which nothing in the stack can do today. |
| 4 | The wire-id gate, the fingerprint, roster duplicate-address and cast-coverage checks | A board in the wrong place currently reports healthy under the wrong name. |
| 5 | `docs/bundle-format.md`, `python/duckbundle/`, `tools/bundle.py` | Validates the whole night on a laptop before anything ships. |
| 6 | Bundle visible in telemetry, `bundle` on the load command, `swarmctl status` columns | First time anything can answer what a duck holds without re-running a deploy. |
| 7 | Move the policies directory out of the versioned tree | Cheap now, expensive once real ducks are provisioned. |
| 8 | `tools/deploy_bundle.py` | The one-click deploy and the instant rollback. |
| 9 | `enroll_duck.sh` and `fleet_run.sh` | Adding a duck stops being two unlinked hand-typed strings. |
| 10 | `scripts/e2e_deploy.sh` | First thing in `deploy/` ever tested by anything but `bash -n`. |
| 11 | `editor/fleet.html`, a Deploy button on the setlist page, a printable call sheet | The operator surface. |
| 13 | **Hardware.** Duck number one, by hand, before any of this is trusted. | See below. |

## What cannot be answered before hardware

None of these are stylistic. Each one would change the design if it comes back
the wrong way.

- The ssh login user. `radxa` is a guess from one upstream file path, and every
  remote write in every script depends on it.
- Whether a stock image gives that user passwordless sudo. This is a hard
  prerequisite of the entire deployment mechanism, and if it is wrong the whole
  mechanism is wrong at once.
- Whether `rsync` and `sha256sum` are present. Both are already depended on and
  neither is in the documented prerequisite list.
- How a fresh duck joins the show WiFi at all. Nothing in `deploy/` touches
  networking.
- Whether hostname plus mDNS gives a stable address, or whether DHCP
  reservations on the show router are required. That decides whether `host` is
  stable, and the reservation table is exactly the kind of venue data that
  cannot live in this repo.
- Which hardware identifier exists and is stable, which decides the whole
  board-swap detection story.
- Whether `usermod -aG robot` takes effect without a reboot. Group membership
  is the entire access gate to `/run/robotd.sock`.
- ssh round-trip time on venue WiFi, which decides whether one click feels like
  ten seconds or three minutes.
- Whether a chirp is audible over a keynote PA at ten metres and whether a head
  waggle reads in a lit ballroom. If the gesture does not read, the fallback is
  a `robot.do` skill, and that choice should be made in the room.
- Whether `robot.sound` works at all with torque released. The physical effect
  of `robot.relax` is itself inferred rather than documented.
- The cost of a load on an RK3566, which decides whether the setlist `continue`
  seam is a problem.
