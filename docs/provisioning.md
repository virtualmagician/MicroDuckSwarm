# Provisioning — version 1

How a duck goes from a stock MicroDuck image to a member of the flock, and how
its show and policy set change on show night. This document and `deploy/` are
the contract for that; the scripts implement exactly what's written here and
nothing more.

**Status: written against confirmed upstream facts, executed against nothing
real.** Every filesystem path, systemd behavior and JSON-RPC exchange below
that is stated as fact is cited to a specific file in
[pollen-robotics/microduck](https://github.com/pollen-robotics/microduck)
(their `docs/design/architecture.md`, `docs/design/updater-design.md`,
`docs/design/restart-order.md`, `robotd/systemd/robotd.service`,
`deploy/robotd.toml`, `docs/robot/cheatsheet.md`) or to `docs/robotd-api.md`
in this repo, which itself mirrors `duck-ipc-proto`. Everything else —
directory names under our own `/opt`, `/etc`, `/var/lib`; the shape of the
systemd unit; the exact poll cadence for our own service — is a considered
design choice, not a verified one, and is labeled as such. See "What's
untested" at the end before trusting this at a venue.

## Filesystem layout

The vendor tree is fixed and we never write into it except the one
surgical edit `push_policy.sh` makes to `robotd.toml`'s `[policy]` table:

| Path | Owner | What it is |
|---|---|---|
| `/run/robotd.sock` | root:robot, mode 0660 | robotd's JSON-RPC socket. robotd itself runs as root. **There is no per-uid/gid allowlist** the way `configd`/`updaterd` have — group membership is the entire access gate (docs/robotd-api.md). |
| `/etc/robot/robotd.toml` | root | Per-board config, written once by the installer, never overwritten by an update. |
| `/var/lib/robot/` | root | Durable vendor state. |
| `/var/log` | — | **zram — ephemeral, cleared on reboot.** |
| `/opt/robot/daemon/releases/<ver>/`, `/opt/robot/daemon/current` | root | The vendor's own updater pattern: versioned directories, atomic symlink swap, health-gated. No A/B partitions; the OS/kernel itself is never OTA'd. |
| systemd units `robotd`, `updaterd`, `configd`, `btd`, `padd`, `mediad`, `tofd` | — | Vendor-shipped. |

The `robot` group is created by the vendor image
(`updater/systemd/sysusers.d/robot.conf`); `robotd.service` itself refuses to
start without it. Their own architecture doc's stated reason for a
group-owned socket instead of a localhost port: "if third-party or user code
ever runs on the board, a localhost port is open to it; a group-owned socket
is not." `duck-agent` is exactly that third-party code, so it joins `robot`
as a supplementary group rather than asking for anything else.

Ours sits entirely outside that tree, namespaced `duckswarm` throughout so a
directory listing never leaves anyone guessing which half is whose, and
deliberately mirrors the vendor's own versioned-release-plus-symlink pattern
for the same reason they chose it — no A/B partitioning available, and a
release must be revertible in seconds without an OS-level rollback:

| Path | Owner | What it is |
|---|---|---|
| `/opt/duckswarm/releases/<UTC-timestamp>/` | duckswarm | One `duck-agent` deploy: `python/duck_agent/`, `python/duckshow/` (its one hard runtime dependency — `import duckshow` in `python/duck_agent/agent.py`), `bin/duck-agent-launch.sh`. `mock_duck`, `tools/`, `tests/` are dev-only and never shipped. |
| `/opt/duckswarm/current` | duckswarm | Symlink to the live release. Flipped atomically (`ln -sfn`); `provision_duck.sh` keeps `current` plus the one release named in `.previous` and prunes the rest — an SBC's eMMC/SD is not the place to accumulate every deploy forever. |
| `/opt/duckswarm/releases/.previous` | duckswarm | One line: the release name `current` pointed at before the last flip. What `--rollback` reads. |
| `/etc/duckswarm/agent.env` | duckswarm, 0640 | Per-board config: `DUCK_ID`, `ROBOTD_TARGET`, `SHOWS_DIR`, `LISTEN_PORT`, `MASTER_PORT`, `MASTER_HOST`, `VERBOSE`. Written once by `provision_duck.sh`, left alone on every later run unless `--force-config` — same "written once, never overwritten by an update" discipline as the vendor's own `robotd.toml`. |
| `/etc/systemd/system/duckswarm-agent.service` | root, 0644 | The unit below. Identical byte-for-byte across every duck — all per-board variation lives in `agent.env`, not the unit. |
| `/var/lib/duckswarm/shows/` | duckswarm | Where `deploy_shows.sh` rsyncs `.duckshow.json` files. This is duck-agent's `--shows-dir`. |
| `/var/lib/duckswarm/shows/policies/` | duckswarm | Where `push_policy.sh` installs `.onnx` files — see "Installing a custom policy" for why this exact path, inside `shows/`, is load-bearing. |

`duck-agent` itself takes no config file (`python/duck_agent/__main__.py`
is pure argparse: `--duck-id`, `--robotd`, `--shows-dir`, `--listen-port`,
`--master-host`, `--master-port`, `-v`) — there was nothing to add a loader
for. `agent.env` plus `deploy/duck-agent-launch.sh` (a small wrapper that
turns the env file into that argv, deployed with each release) stand in for
one, and exist so "is `--master-host` set" can be plain bash rather than
depended on `ExecStart=`'s own `$VAR` splitting rules inside the systemd
unit — see the comment in `duckswarm-agent.service` for the reasoning.

## The systemd unit

`deploy/duckswarm-agent.service`, installed verbatim (no templating) at
`/etc/systemd/system/duckswarm-agent.service`:

```ini
[Unit]
After=network-online.target robotd.service
Wants=network-online.target

[Service]
Type=simple
User=duckswarm
Group=duckswarm
SupplementaryGroups=robot
EnvironmentFile=/etc/duckswarm/agent.env
WorkingDirectory=/opt/duckswarm/current/python
ExecStart=/opt/duckswarm/current/bin/duck-agent-launch.sh
Restart=always
RestartSec=5
StartLimitIntervalSec=0
TimeoutStopSec=35
KillSignal=SIGTERM
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/duckswarm
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

(Full file, with the reasoning inline as comments, at
`deploy/duckswarm-agent.service`.)

Three decisions worth being explicit about:

- **`SupplementaryGroups=robot`.** Confirmed the entire access gate to
  `/run/robotd.sock` (see the layout table above) — no capability, no ACL,
  just group membership. `WorkingDirectory=/opt/duckswarm/current/python`
  matters too: `duck_agent` is run as `python3 -m duck_agent`, an implicit
  `sys.path[0]`-relative import exactly like `scripts/e2e_demo.sh` already
  does locally (`cd "$PY" && python3 -m duck_agent ...`) — there is no
  installed package, so the working directory *is* the import mechanism.
- **`Restart=always` / `RestartSec=5` / `StartLimitIntervalSec=0` mirrors
  `padd`'s own reconnect discipline** — confirmed: "a client like ours,
  exits and retries every 5 s when the socket is absent." `duck-agent`'s own
  `RobotdClient` (`python/duck_agent/robotd_client.py`) already reconnects
  with backoff *without the process dying* if robotd's socket merely isn't
  there yet — that's the steady-state case padd's pattern describes. This
  `Restart=` is the belt-and-suspenders case padd's pattern doesn't cover:
  the agent process itself crashing outright. `StartLimitIntervalSec=0`
  means systemd never gives up restarting it — a show-critical daemon must
  not go quiet for the rest of the night because it crash-looped five times
  early in the evening, the same reason padd's own retry has no stated
  giving-up point either.
- **`TimeoutStopSec=35`, comfortably past robotd's own worst-case restart
  health gate (confirmed: polls every 500 ms for up to 30 s, typically
  landing healthy in about 8-9 s).** `duck_agent.__main__` routes SIGTERM through `DuckAgent.stop()`,
  which sends a final zero-velocity `robot.move` + `robot.stop` before
  exiting — robotd is last-value-wins with no local watchdog beyond its own
  500 ms deadman (docs/robotd-api.md), so a `systemctl stop` that lands
  while robotd itself is mid-restart still needs room to finish that
  sequence instead of being SIGKILLed through it.

## Provisioning one duck

```
deploy/provision_duck.sh <host> --duck-id <id> [--master-host <ip>] [--dry-run]
```

What it does, in order (full detail in the script's own header comment and
inline comments — this is the shape, not a transcript):

1. **Refuses to run** unless the remote `robot` group already exists — if it
   doesn't, this isn't a stock MicroDuck image (confirmed: `robotd.service`
   itself won't start without it either), and creating the group ourselves
   would be papering over something worse.
2. Creates the `duckswarm` system user if missing (idempotent), adds it to
   `robot` (idempotent — `usermod -aG`), creates
   `/etc/duckswarm`, `/var/lib/duckswarm/shows/policies`.
3. Writes `/etc/duckswarm/agent.env` **only if it doesn't already exist**
   (or `--force-config`, which backs up the old one first).
4. Uploads a new timestamped release under `/opt/duckswarm/releases/`,
   flips `current`, prunes anything older than `current` + `.previous`.
5. Installs/updates the systemd unit, `daemon-reload`s, `enable`s it.
6. Starts the service if it isn't running. **If it's already active, leaves
   it running and says so** — the new release is in place and `current`
   points at it, but nothing currently performing gets interrupted — unless
   `--restart` was passed.

Re-running this script is always safe: every step above is idempotent, and
the one step that could interrupt a duck mid-show (bouncing an already-active
service) is refused by default and needs the explicit `--restart` flag —
the same "explicit flag for anything destructive" rule the show-night
invariants in `CLAUDE.md` ask for generally.

`--rollback` (`deploy/provision_duck.sh <host> --rollback`) points `current`
back at whatever `.previous` names and restarts — no re-upload, no config
touch. Nothing to roll back to is a hard error, not a silent no-op.

## Deploying a show

```
deploy/deploy_shows.sh <roster.json>          # every duck in the roster
deploy/deploy_shows.sh <host>                 # one duck directly
deploy/deploy_shows.sh <roster.json> --show demo   # just one show
```

docs/swarmlink-protocol.md is explicit that this is out-of-band: "Show files
are distributed out-of-band before the show (rsync/scp in v1; the load hash
check is what makes that safe)." This script *is* that step — it does not
send a `load` command, and shouldn't; loading (and the sha256 check that
makes it safe) is SwarmLink's job at load-in, kept deliberately separate
from "the bytes are on disk."

It rsyncs the local `shows/` tree into `/var/lib/duckswarm/shows/` on each
target, excluding `shows/fixtures/` (deliberately-invalid validator test
data, never something a duck should have) and `shows/policies/` — **always**
excluded, in both directions, regardless of whether a local `shows/policies/`
exists, so that `--delete` (opt-in, rsync's own `--delete` passed straight
through — the explicit flag this script's one destructive option needs) can
never remove a `.onnx` `push_policy.sh` installed. That's not this script's
data to touch.

After every push it re-fetches each file's sha256 over ssh and compares
against the local hash, failing loudly on any mismatch — belt-and-suspenders
on top of the `load`-time check, cheap, and it means a deploy that reports
success actually put the right bytes on the right duck.

## Installing a custom `.onnx` policy

```
deploy/push_policy.sh <host> --file mine.onnx --slot walk --yes
deploy/push_policy.sh <host> --rollback --yes
deploy/push_policy.sh --selftest
```

docs/duckshow-format.md draws a hard line between two mechanisms, and this
script only ever does the first:

1. **Which gait plays at runtime** is a `mode` event, `"walk"` or `"roller"`
   — that's the *show's* concern, sent over the wire by SwarmLink, nothing
   to provision.
2. **What a mode actually does** is pre-show configuration: a fixed
   `robotd.toml` `[policy]` slot pointed at an `.onnx` file, live only after
   `sudo systemctl restart robotd` (confirmed: never mid-show).
   `push_policy.sh` is the manual, ops-level version of exactly that step.

One file, one push, used by two independent consumers — deliberately the
same copy, not two:

- **robotd** reads whatever absolute path `[policy].<slot>` in
  `/etc/robot/robotd.toml` names.
- **duck-agent** verifies a show's `requires.policies[].file` (a path
  *relative to `--shows-dir`*, e.g. `"policies/moonwalk.onnx"`,
  per docs/duckshow-format.md) against a declared sha256 at `load` time.

Pointing robotd's slot straight at `/var/lib/duckswarm/shows/policies/<file>`
— the same file duck-agent resolves `policies/<file>` against, since that's
inside its own `--shows-dir` — satisfies both from one push. The script
prints the sha256 it computed so it can be pasted into a show's
`requires.policies[].sha256`; it does not touch any `.duckshow.json` itself.

The edit to `robotd.toml` is a **targeted key-set**, never a file rewrite:
it fetches the file, runs it through an `awk` transform
(`toml_set_policy_slot` in `deploy/lib/common.sh`) that adds or updates only
the named key inside `[policy]` — creating that section if it's missing —
and leaves every other line byte-for-byte untouched, because that file is
per-board config nobody else gets to clobber. `deploy/push_policy.sh
--selftest` exercises that exact transform against fixtures with zero
network access (existing key replaced, key appended to an existing section,
section created from scratch, and idempotent re-application) — run it any
time; it's the fastest thing this repository can verify.

After installing, the script backs up the pre-edit `robotd.toml`
(`<path>.bak-<UTC-timestamp>`), restarts robotd, and polls `robot.health`
— every 0.5 s for up to 30 s, robotd's own confirmed restart-to-healthy
transcript, reused here because it's the only real number that exists for
this. **The exact shape of a `robot.health` reply is not confirmed
upstream** — only the text of an unhealthy one:
`policy unavailable: <reason>`. Rather than invent a field name, the script
substring-matches that exact confirmed text against the whole reply and
says so in its own comments; this is a real, named gap, not an oversight.

If the poll doesn't come back healthy, the default is to **restore the
backup and restart again automatically** — `--no-rollback-on-failure`
disables that, for debugging a failure in place instead. A `--rollback`
mode does the same restore-and-verify on demand later, from whatever
backup is newest. Every write happens through `--yes` or not at all —
bare invocation with neither `--yes` nor `--dry-run` refuses to run.

## Ordering: a policy and a show change together

Load-in with both a new `.duckshow.json` and a new policy behind one of its
`requires.policies` entries has exactly one safe order, and it's the reverse
of the naive "push everything, then load":

1. **`push_policy.sh` first — and confirmed healthy before anything else
   moves.** If this step fails, nothing downstream has changed yet.
2. **`deploy_shows.sh` after**, updating the show that declares the new
   policy's hash.

Reversed, there's a real gap: a show's `requires.policies` check
(`duck-agent`'s `_check_policies`) only proves a matching `.onnx` sits on
disk with the right hash — it has **no way to ask robotd "is your `walk`
slot actually backed by this file right now."** Push the show first and a
`load` between steps would report `policies_ok: true` while robotd is still
running the *old* gait — preflight would look green for a duck about to do
the wrong thing. Installing and verifying the policy first, before the show
that depends on it is even reachable, is what keeps that window from ever
opening. This is also, plainly, a gap that stays open in general: nothing
in this system continuously reconfirms "robotd's loaded policy still
matches the hash duck-agent last checked" between a successful `load` and
the moment the number actually plays — only that it was true at load time.

## Rollback — an hour before doors

Three different things can be the thing that's wrong, and each has its own
fast, specific undo — none of them require going back to a laptop that
isn't already open:

- **A bad policy.** `deploy/push_policy.sh <host> --rollback --yes` restores
  the newest `robotd.toml.bak-*` on that duck, restarts robotd, and verifies
  health before reporting success. This is also what a failed *install*
  already does automatically, by default.
- **A bad show.** Show files are plain data under version control in this
  repo — `git checkout <last-good-commit> -- shows/<id>` locally, then
  `deploy_shows.sh <roster.json> --show <id>` again. No agent restart, no
  robotd restart: duck-agent reads show files fresh on every `load`, it
  never caches at startup.
- **A bad `duck-agent` release itself** (rare — this is our own code, not
  something pushed under time pressure most nights). `deploy/provision_duck.sh
  <host> --rollback --restart` repoints `current` at the previous release
  and restarts the service.

None of these three interact with each other's undo — rolling back a policy
never touches a show file, rolling back a show never restarts robotd, and
rolling back the agent code never touches `robotd.toml`. That separation is
deliberate, mirroring the ordering rule above: the fewer things one rollback
has to reason about, the more likely it is to work correctly under pressure.

## What you need before you run any of this

- SSH key auth to every duck as the same login user (default `radxa`,
  override with `--user` or `$DUCKSWARM_SSH_USER`) — **none of these
  scripts allocate a tty or support a password prompt**, for `ssh` or for
  `sudo`.
- **Passwordless (`NOPASSWD`) sudo for that user.** Every remote write in
  every script goes through `sudo` — creating the service account, writing
  under `/opt` and `/etc/duckswarm`, editing `/etc/robot/robotd.toml`,
  every `systemctl`. This is a hard prerequisite, not a nice-to-have; it is
  unconfirmed whether a stock image ships this by default.
- `ssh`, `rsync`, `awk`, `shasum`, `python3` on the Mac running these
  scripts (Python only to parse `roster.json` — stdlib `json`, nothing
  installed). Each script checks for what it needs and fails with a clear
  message rather than a mid-script surprise.
- On the duck itself: `python3`, `rsync` and `sha256sum`. `python3` is for
  `push_policy.sh`'s `robot.health` poll and `duck-agent` already requires
  it to run at all, so that one is not an extra ask. The other two are:
  every push runs `rsync` on the remote end (`--rsync-path='sudo rsync'`
  in `deploy/lib/common.sh`), and `deploy_shows.sh` verifies each pushed
  file by running `sha256sum` over ssh. Neither was listed here before,
  and both are hard dependencies: without them a deploy fails on the duck
  rather than on the Mac. Like the sudo requirement above, all three are
  unconfirmed against a real image.
- Also assumed present and unconfirmed: `readlink`, `tee`, `systemctl`, and
  `ln -sfn` behaving as GNU coreutils do. `provision_duck.sh` already uses all
  four, and the bundle flip designed in `docs/fleet.md` would lean on
  `ln -sfn` harder.

## What's untested

Everything in `deploy/` has been checked two ways and no further: `bash -n`
on every script, and a `--dry-run` run against a fake, unreachable hostname
(which must — and does — return immediately, never block on a connection
attempt; that's `lib/common.sh`'s `remote_read`/`remote_script`/`rsync_push`
all checking `$DRY_RUN` before anything network-shaped happens). The
`robotd.toml` edit itself is additionally checked by `push_policy.sh
--selftest` against fixtures, with zero network access. **None of it has
run against a real robotd, a real `/etc/robot/robotd.toml`, or a real
`sysusers.d`-created `robot` group.** Specifically open until a duck exists:

- Whether the default login user on a stock image has (or can be given)
  passwordless sudo, and what that user's name actually is — `radxa` is a
  guess from the one file path upstream's own docs showed
  (`/home/radxa/my_walking.onnx`), not a confirmed default account.
  Provision one real duck by hand first and adjust `--user` before trusting
  the default.
- The real shape of a `robot.health` reply — confirmed only that an
  unhealthy one contains `policy unavailable: <reason>` somewhere; the
  substring match in `poll_robot_health` (`deploy/push_policy.sh`) is a
  deliberate choice made *because* the rest of the shape isn't confirmed,
  not a guess dressed up as one. The 0.5 s / 30 s poll cadence it runs on
  is robotd's own confirmed restart transcript, borrowed for lack of any
  measured number of our own.
- Whether `journald` on a stock image persists across reboots. `/var/log`
  is confirmed zram (ephemeral); whether the systemd journal that captures
  `duck-agent`'s own stdout/stderr (no separate logging is configured — see
  `deploy/duckswarm-agent.service`) survives a reboot depends on
  `journald.conf`'s `Storage=` setting on that image, which is unconfirmed
  either way. If post-mortems need duck-agent's own logs to survive a
  reboot, this needs checking at M1, not assumed.
- Real numbers for everything this doc had to pick without evidence:
  `duck-agent`'s own startup time (there is no external health check for it
  at all yet — `provision_duck.sh` only confirms the *process* didn't
  immediately die, never that it reached a good internal state), SSH
  round-trip time to a duck on the show WiFi, and whether `usermod -aG` and
  a fresh `SupplementaryGroups=` actually take effect without a reboot on
  this specific OS (POSIX group membership is normally read once at login/
  session start, and systemd services read it at unit start — restarting
  `duckswarm-agent.service` after the first `usermod` should be enough, but
  "should be" is exactly the phrase this whole document is trying not to
  lean on without hardware to check it against).
