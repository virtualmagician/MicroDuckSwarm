# The pose cache format, and how `tools/bake` produces one

`tools/bake` is the native **Create Preview** baker (`docs/viewer.md` "Create Preview (baked physics)"; `docs/bake-parts.md` for the research and feasibility case). It reads a `.duckshow`, drives a real MuJoCo simulation of each cast role against the shipped ONNX policies, and writes a **pose cache** — a `duckbake/1` JSON document — that the existing kinematic viewer (`docs/viewer.md`) can play back frame-for-frame, exactly the way it plays back the live kinematic sampler today. Physics is a bake step, not a live mode: nothing in the editor or duck-agent ever imports this tool.

## Why this is exempt from CLAUDE.md #1

`CLAUDE.md`'s hard rule #1 — *"Python is stdlib-only (3.10+). No pip dependencies, ever"* — exists so `python/duck_agent` runs on a stock Armbian image and `python/duckshow`/`python/mock_duck`/`python/tools` run on any Mac with nothing installed. `tools/bake` needs real `mujoco` and `onnxruntime`; there is no stdlib way to do rigid-body physics or run a trained network. `docs/viewer.md` already carves out the equivalent exemption for the *JavaScript* in-browser preview module; `docs/bake-parts.md` §3.3 names the same shape of carve-out for a native Python helper but leaves it unresolved, recommending it live in a directory "structurally separate from `python/`". This directory is that resolution:

- `tools/bake` is never imported by `python/duckshow`, `python/duck_agent`, `python/mock_duck`, or `python/tools/showmaster.py`.
- It is never installed on a duck and never required to author or run a show.
- Its dependencies live in `tools/bake/requirements.txt` and its own venv (`tools/bake/.venv/`, gitignored like every other `.venv/` in this repo), never in the repo's shared environment.
- The whole repo — editor, duck-agent, SwarmLink, tests — works with `tools/bake/` absent entirely.

## Setup

Needs `assets/microduck/` populated (gitignored, user-supplied — `docs/bake-parts.md` §2; never fetched or committed by this tool) and a Python **3.12** venv (narrower than this repo's general 3.10+ floor — see `requirements.txt`'s header comment for why: it keeps the door open for the optional BAM actuator path below without a venv rebuild).

```bash
cd tools/bake
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python3 bake_show.py ../../shows/octet/octet.duckshow.json /path/to/octet.duckbake.json
.venv/bin/python3 bake_show.py ../../shows/octet/octet.duckshow.json /path/to/lead-only.duckbake.json --duck lead
```

`--duck ROLE` is repeatable, for fast single-duck iteration. `--quiet` suppresses per-role progress. The show is loaded and validated with the canonical `python/duckshow` parser/validator (imported via `sys.path`, not duplicated — `python/duckshow` is stdlib-only, so this is a one-directional dependency: `tools/bake` may import `python/duckshow`, nothing in `python/` ever imports `tools/bake`); a show that fails validation is refused, not baked around.

## What actually got fetched, vs. `bake-parts.md`'s proposal

`docs/bake-parts.md` §2's directory layout was explicitly "a **proposal**, not a contract — pick it (or change it) when the bake driver is actually written, and update this doc if it changes." The `assets/microduck/mjcf/` actually populated on this machine (2026-09) carries files that table didn't originally list — including a live rename this document caught while `tools/bake` was being written: upstream renamed the curated full-collision file `docs/bake-parts.md` originally catalogued as `robot_allcollisions.xml` to **`robot_groundcontact.xml`**, and reassigned the old name to a *different* file (every geom gets a matching collision copy, not the curated self-collision subset training actually used — `docs/bake-parts.md` §1a's own re-verification, done the same day, confirms this directly and says plainly the new `robot_allcollisions.xml` "is not on the needed list" for a bake driver). This baker uses **`scene.xml`** (which `<include>`s `robot_groundcontact.xml`) accordingly — not `scene_allcollisions.xml`, which was this document's first choice before that rename surfaced. `scene.xml`'s STAND keyframe, actuator order, and sensor set are byte-identical to `scene_allcollisions.xml`'s (checked directly), so this was a same-day correction, not a rewrite. `assets/microduck/policies/manifest.json` also carries more structure than `bake-parts.md`'s original research pass covered (`obs_len`/`action_len`, per-policy `kind`/`command` encodings) — this baker reads and validates against it (`bakelib/policyset.py`), and it is what resolved several of the "how would this skill actually be driven" questions in "What isn't simulated" below.

## Physics model and constants

| Quantity | Value | Source |
|---|---|---|
| Physics timestep | 0.005 s (200 Hz) | `docs/bake-parts.md` §3.1, confirmed two ways. Neither exported MJCF carries a MuJoCo `<option>` element (confirmed by direct read of `robot_walk.xml`/`robot_groundcontact.xml` — grepped, none present) — this baker sets `model.opt.timestep` on the compiled `MjModel` after load rather than editing the XML, which has the same effect on `mj_step` without touching the fetched assets on disk. |
| Control decimation | 4 (→ 50 Hz control) | Same source. |
| Model file | `assets/microduck/mjcf/scene.xml` (→ `robot_groundcontact.xml`) | This document's own choice — see above; corrected same-day after an upstream rename surfaced. |
| Actuators | 14 stock MuJoCo `<position>` actuators, `kp=0.55 kv=0.0 forcerange=[-0.96,0.96]` | Direct read of the MJCF's `<actuator>` block. **Not** the BAM voltage-control-law model training used — see "Known fidelity gaps" below. |
| Nominal standing pose | The scene's own `STAND` keyframe (`qpos`/`ctrl`, 14 joint values + trunk `z=0.12`) | Direct read of `scene.xml`'s `<keyframe>` block. Used as (1) every role's reset state, (2) the action-output zero-point (`ctrl = stand_qpos + action`), and (3) the zero-point `body_pose`/`head_pose` deltas are computed from. |
| Joint / action order | `left_hip_yaw, left_hip_roll, left_hip_pitch, left_knee, left_ankle, neck_pitch, head_pitch, head_yaw, head_roll, right_hip_yaw, right_hip_roll, right_hip_pitch, right_knee, right_ankle` (14) | The MJCF's own `<actuator>` declaration order, confirmed identical to `docs/bake-parts.md` §3.1's quoted action-space description ("0-4 left leg ..., 5-8 neck/head ..., 9-13 right leg"). `qpos[7:21]`/`qvel[6:20]` are these 14 in this order (freejoint occupies `qpos[0:7]`/`qvel[0:6]`). |

Loading the compiled model costs ~0.3 s (mesh parsing, mostly the ~26 MB of STLs); one `MjModel` is loaded once and shared read-only across every role's own `MjData` in a single process — ducks never interact physically (`docs/viewer.md`), so this is a safe speedup, not a fidelity shortcut, and it means this baker does not need the process-per-duck parallelism `docs/bake-parts.md` §3.2's estimates assumed to hit its numbers (see "Measured" below).

## Observation layout — confidence by field

`obs[61] = proprioception(48) ++ command(13)`, matching `assets/microduck/policies/manifest.json`'s declared `obs_len: 61` (checked at load; a mismatched manifest refuses to bake rather than guess).

**Proprioception (48) — `gyro(3) ++ projected_gravity(3) ++ joint_pos_delta(14) ++ joint_vel(14) ++ prev_action(14)`.** `docs/bake-parts.md` §4 flags this exact split as *"Strand 1's inference from standard practice, not a literal quote"* — this baker follows that same inference (it is the standard IsaacLab/mjlab observation-term order) but it is **not independently confirmed** against `microduck_rl`'s actual `mdp.py` term registration. Specifics:
- `gyro` — read from the MJCF's `imu_ang_vel` sensor (noise-free), not the also-present `angular-velocity` sensor (which the MJCF's own `sensors.xml` gives `noise="0.005"`). A bake wants a deterministic replay of the *policy*, not of training-time sensor-noise domain randomization.
- `projected_gravity` — world gravity `[0,0,-1]` rotated into the trunk's body frame via the inverse of the `orientation` framequat sensor's quaternion. Confirmed by direct MJCF read that the `imu` site sits at `quat="1 -0 -0 -0"` (identity) on `trunk_base`, which is itself declared at identity relative to the free joint — so this sensor's frame *is* the trunk's own frame, not an offset one.
- `joint_pos_delta` = `qpos[7:21] - STAND.qpos[7:21]`, `joint_vel` = `qvel[6:20]` raw. The delta-from-default convention for position (but not velocity) is, again, standard practice, not a confirmed literal fact.
- `prev_action` — the raw (unscaled) 14-vector this baker's own policy returned on the previous control tick; zero at each role's `t=0`.

**Command (13) = `twist(3) ++ head_pose(4) ++ body_pose(6)`.** Order **confirmed** by direct quote from `microduck_rl`'s task source (`docs/bake-parts.md` §3.6).
- `twist = [vx, vy, vyaw]`, straight off the `.duckshow` `locomotion` track (`docs/duckshow-format.md`); `[0,0,0]` for a role with no locomotion track.
- `head_pose = [neck_pitch, head_pitch, head_yaw, head_roll]`, each the `.duckshow` `head` track's value **minus the STAND keyframe's own value for that joint**. The upstream docstring `bake-parts.md` §3.6 quotes says the command is *"deltas from default joint positions"*; this baker's own inference is that "default" means the STAND keyframe (the only concrete nominal pose this repo has read out of the source material) — **not independently verified** against `microduck_rl`'s env config, which might define `default_joint_pos` slightly differently. `[0,0,0,0]` for a role with no head track.
- `body_pose = [0, 0, pose.z, pose.roll, pose.pitch, 0]` when `pose.active` is true; **`[0,0,0,0,0,0]` when `pose.active` is false or the role has no pose track.** `x`/`y`/`yaw` are always zero — confirmed by `docs/bake-parts.md` §3.6 to have no `robotd` wire equivalent at all. The `active`-gating is a refinement over the literal formula: `docs/robotd-api.md` states `robot.pose` is *"glided while active, snaps back when false"* — real robotd ignores `z`/`roll`/`pitch` outright when inactive, not just visually but as the actual command sent to the policy. **This is a deliberate divergence from the kinematic preview**, which does not gate on `active` at all (`editor/duckshow-viewer.js` samples `pose.z`/`roll`/`pitch` unconditionally) — the kinematic path is a known simplification there; this baker's whole purpose is fidelity to what robotd will really do, so it honors the flag.

## Action mapping

`ctrl[14] = STAND.qpos[7:21] + action * action_scale`, `action_scale = 1.0`. `assets/microduck/policies/manifest.json` gives an explicit `action_scale: 0.8` for the two roller-family policies but no field at all for `alpha_walking`/`alpha_stand` — read here as "1.0 by omission" (the roller entries reading as the stated exception, not the unstated rule). **Not independently confirmed** against training config. If a future re-check finds a different scale, this is the one constant to change; every other number in this baker follows from it only indirectly (through what gait the policy ends up producing).

## Which policy drives the bake

Every role, for the whole show, is driven by **`alpha_walking.onnx`** alone — the only policy this v1 baker loads. `manifest.json` marks both `alpha_walking` and `alpha_stand` `"kind": "perpetual"` but nothing in this repo's research (`docs/bake-parts.md`, `docs/robotd-api.md`) documents a rule for *when* real robotd switches between them — `robot.setMode` only ever names `"walk"`/`"roller"` (a completely different axis, the roller-family swap). Always using `alpha_walking`, including at moments the locomotion command is exactly zero, is this baker's own simplification.

**Measured consequence, not just a theoretical caveat:** this baker's own verification run found `alpha_walking.onnx` tracks `twist` (walking velocity) convincingly — a sustained `vx=0.3` command produced ~0.56 m of real forward travel over 4.5 s of physics, roughly matching the commanded speed within the actuator-fidelity gap below — but tracks a `body_pose.z` crouch only weakly: a sustained `z=-0.048` command (near the format's own ±0.05 m validation ceiling) settled at an actual trunk-height delta of about **-0.003 to -0.005 m**, not the commanded -0.048 m, even held for 3.6 s. Observation wiring was checked directly (the command lands at the exact expected `obs` index) so this is not an indexing bug; it reads as `alpha_walking.onnx` genuinely prioritizing velocity tracking over static pose-hold, plausible for a policy whose name and `"perpetual"` walking role suggest that emphasis. **A skill/pose-hold-focused policy (`alpha_stand.onnx`, or a per-skill policy) likely tracks `body_pose` far better** — this is exactly the kind of thing the "one policy for everything" simplification above costs, named honestly rather than smoothed over. A show role that leans mainly on `pose.z`/`roll`/`pitch` crouches (e.g. `octet.duckshow.json`'s `sable`, "the crouch solo") will bake with much shallower crouches than the `.duckshow` file specifies.

## What isn't simulated

Per the project brief: *"a skill you cannot drive faithfully should be recorded in the bake log as unsimulated rather than faked."* This v1 baker drives ordinary locomotion + head + body-pose faithfully (to the fidelity gaps named above) and **does not** drive any of the five `do` skills or roller mode. Nothing pretends to; the base locomotion policy keeps running unmodified through a skill event, and every occurrence is logged (`log[].kind == "skill_unsimulated"`, one entry per event, with the specific policy file and command encoding — now resolvable from `manifest.json` — that a future version would need):

| Skill | Policy | Why not v1 |
|---|---|---|
| `kick_left` / `kick_right` | `ball_kick_left.onnx` / `ball_kick_right.onnx` | Episodic; needs `ball.xml`'s 70 mm/15 g prop loaded into the scene, which this baker's model does not include. |
| `sit_toggle` | `alpha_sitstand.onnx` | Scripted: `manifest.json` gives a `posture_flag` command encoding (`twist.vx` slot, `sit=1.0`/`stand=0.0`, `ramp_s=2.0`, `unwind_s=1.0`) and confirms *what* to send, but not the transition semantics between the perpetual walk policy and this one — in particular what happens when a second `sit_toggle` fires before the first's `ramp_s` completes (`shows/octet/octet.duckshow.json`'s `reed` role does exactly this: two `sit_toggle` events 2.0 s apart against a 2.0 s ramp). Driving this on an unverified guess about hand-off timing would be exactly the "faked" outcome the brief warns against; logging it is the honest choice. |
| `roulade` | `roulade.onnx` | Episodic, and `manifest.json` marks it `"chain": true` — chained to something else the manifest doesn't specify. |
| `ground_pick` | `alpha_ground_pick.onnx` | Episodic, phase-encoded command (`manifest.json`: `period_s`, `end_phase`) against `twist.vx,twist.vy` — resolvable in principle, not implemented in v1. |
| any `mode: "roller"` event | `roller.onnx` / `roller_crouch.onnx` against `robot_groundcontact_rollers.xml` | A structurally different physical machine (passive wheel joints, no leg policy) that this baker's model does not load at all. A role whose show uses `roller` mode anywhere is logged `kind: "mode_unsimulated"` and its whole pose array is held static at its stage mark rather than run through the wrong model. `shows/octet/octet.duckshow.json` never uses roller mode, so this path is untested against a real show — only sanity-checked not to crash. |

`sound` events have no physical effect on any pose field and are not logged — they don't move the duck, so there is nothing to report as unsimulated.

The **BAM actuator model** training actually used (`docs/bake-parts.md` §3.5 — a voltage-control-law torque/friction model of the XL330 servo, not MuJoCo's stock `<position>` actuator) is **not ported**, for all roles, always. `docs/viewer.md`'s own "Honesty" section already names this exact gap as expected ("re-stepping the exported MJCF under MuJoCo's stock actuators drives the policy against a plant it was not quite trained for"); §3.5's own effort estimate ("one more small, Python-3.12-pinned pip package and a few dozen lines of glue") is why `requirements.txt` pins Python 3.12 now, so adding `better-actuator-models[mujoco]` later is a `pip install`, not a venv rebuild. Not attempted in this pass — the walking gait produced without it is real and measured (see "Measured" below), and reporting that honestly, un-faked, is more valuable at this stage than a partially-ported actuator model with its own unverified assumptions layered on top.

## Fall detection

A role is logged `kind: "fell"` (once, the first crossing) when the trunk height drops below 35% of nominal standing height, or trunk roll/pitch exceeds ~1.0 rad (~57°) — this baker's own heuristic, not sourced from anywhere; tune it if it proves too sensitive or not sensitive enough once more shows are baked.

## Heading, roll, pitch — the extraction convention

`docs/duckshow-format.md`/`docs/robotd-api.md` only ever define `roll`/`pitch` as small deltas-from-upright and never need a full 3D orientation decomposition, because no real command asks for one. A baked duck's *actual* trunk can end up at any orientation (mid-stumble, say), so this baker picks a convention: extrinsic Z-Y-X (yaw, then pitch, then roll) Euler decomposition of the trunk's quaternion — `heading` is the yaw term, `bodyRoll`/`bodyPitch` the other two. Standard robotics 3-2-1 sequence; not itself sourced from anywhere upstream, since nowhere upstream needed one.

## `walkPhase`

Computed the same way `editor/duckshow-viewer.js`'s `precomputeRolePath` computes it for the kinematic path — cumulative distance since the last sample, scaled by `PHASE_PER_METRE = 2π/0.10` (one stride cycle per 10 cm), held flat while instantaneous speed stays below `STOP_SPEED_EPS = 0.005` m/s — duplicated as constants in `bakelib/sim.py` rather than shared, because the source is JS and there is no cross-language build step in this repo to link them (see that file's module docstring). The one substantive difference: the kinematic path's distance is *dead-reckoned from commanded velocity*; this bake's distance is the *actual simulated* trunk displacement. Both use the same 0.02 s sample spacing (this bake's 50 Hz control tick **is** `editor/duckshow-viewer.js`'s `DEFAULT_DT`), so the two `walkPhase` series are directly comparable — which is the point: a mismatch between them is exactly the kind of intended-vs-actual signal `docs/viewer.md`'s "the payoff: the diff" is about, even though this v1 baker does not yet draw that diff itself (see "Not built yet" below).

## Stage marks

Each role is spawned at its resolved stage mark (`editor.marks[role]` if the show has one, else the same spread default the kinematic path uses — `bakelib/marks.py` mirrors `editor/duckshow-viewer.js`'s `resolveMark`/`defaultMarkFor` exactly, ported by direct read since there is no way to share code across the JS/Python boundary) rather than at the MuJoCo world origin. This is deliberate: `docs/viewer.md`'s stated payoff is the *diff* between the kinematic dead-reckoned path and the physics-baked one, which only means anything if both start from the same place. A duck's `x`/`y`/`heading` in the cache are therefore already in stage (world) coordinates, directly comparable to the kinematic path's — no frame transform needed downstream.

## The pose cache (`duckbake/1`)

```json
{
  "format": "duckbake/1",
  "cache_key": "<sha256 hex, see below>",
  "show": { "path": "...", "sha256": "<hex>", "name": "Eight to the Bar", "duration": 64.0 },
  "policies": {
    "dir": "assets/microduck/policies",
    "combined_sha256": "<hex, over every .onnx + manifest.json>",
    "file_sha256": { "alpha_walking.onnx": "<hex>", "...": "..." },
    "locomotion_policy": "alpha_walking.onnx"
  },
  "physics": { "timestep": 0.005, "decimation": 4, "control_hz": 50.0, "scene": "scene.xml" },
  "bake": {
    "generated_at": "2026-09-03T20:27:55Z",
    "baker": "tools/bake",
    "layout_version": 1,
    "wall_clock_s": 3.7,
    "roles_requested": ["lead", "..."],
    "engine": { "python": "3.12.13", "mujoco": "3.3.7", "onnxruntime": "1.20.1", "numpy": "2.2.6", "platform": "...", "machine": "arm64" }
  },
  "frame_rate": 50.0,
  "roles": ["lead", "echo", "..."],
  "unsimulated_roles": [],
  "fallen_roles": [],
  "poses": {
    "lead": {
      "x": [0.30, 0.2996, "... 3200 floats ..."],
      "y": [-0.675, "..."],
      "heading": ["..."],
      "headYaw": ["..."], "headPitch": ["..."], "headRoll": ["..."], "neckPitch": ["..."],
      "bodyZ": ["..."], "bodyRoll": ["..."], "bodyPitch": ["..."],
      "mouthOpen": ["..."],
      "walkPhase": ["..."]
    }
  },
  "log": [
    { "role": "lead", "t": 17.0, "kind": "skill_unsimulated", "detail": "'kick_left' event not driven by physics ..." }
  ]
}
```

- **Field vocabulary** — `poses[role]` carries exactly the field names `docs/viewer.md`'s renderer contract uses (`{role, x, y, heading, headYaw, headPitch, headRoll, neckPitch, bodyZ, bodyRoll, bodyPitch, mouthOpen, walkPhase}`, minus `role` itself since it's the dict key, and minus the optional `resting` the kinematic path also doesn't require) — a consumer already written against the kinematic pose shape needs no new field names, only a new source of frames.
- **Numeric arrays, not per-frame objects** — every array under one role is parallel, length `frame_count` (`round(duration * frame_rate)`, e.g. 3200 for a 64 s show at 50 Hz — `roles × frame_count` sums to exactly `docs/bake-parts.md`'s own "about 25,600 frames total" figure for the 8-duck 64 s octet). Values are rounded before serialization (4-5 decimal digits depending on field, see `bakelib/posecache.py`'s `_ROUND` table) — plenty of precision for anything a renderer draws, and it keeps an 8-duck 64 s cache at **~2.4 MB**, comfortably under the "couple of megabytes" target.
- **`unsimulated_roles`** — roles this baker declined to simulate at all (roller mode only, in v1); their pose arrays are still full-length (held static at the mark) so the cache stays well-formed, but a consumer should treat them as informational only.
- **`fallen_roles`** — any role with at least one `kind: "fell"` log entry.

### Cache key

```
cache_key = sha256(
  "show=" + sha256(show file bytes) + "|" +
  "policies=" + sha256(sorted "filename:sha256" lines over every .onnx + manifest.json) + "|" +
  "physics=" + json({timestep, decimation, control_hz, scene}, sorted) + "|" +
  "layout=" + BAKE_LAYOUT_VERSION
)
```

Matches `docs/viewer.md`'s framing exactly — *"A cache keyed by show hash and policy versions, invalidated when either changes"* — plus the physics constants and a `BAKE_LAYOUT_VERSION` (currently `1`, in `bakelib/posecache.py`) bumped whenever this baker's own observation/action/output conventions change in a way that would make an old cache silently wrong rather than merely stale. A consumer should compare `cache_key` against what it expects and refuse (or re-bake) a mismatch, exactly as it already must for the kinematic sampler's own show-hash checks.

## Bake log kinds

| `kind` | Meaning |
|---|---|
| `skill_unsimulated` | A `do` event fired; the base locomotion policy kept running through it unmodified. |
| `mode_unsimulated` | The role's show uses a `mode: "roller"` event anywhere; the whole role was held static at its mark instead of simulated (see "What isn't simulated"). |
| `fell` | Trunk height or tilt crossed the fall heuristic above; logged once, first crossing only. |

## Measured (this run, 2026-09-03)

`bake_show.py ../../shows/octet/octet.duckshow.json <out>` against all 8 roles, sequential in one process, Apple Silicon (`platform.machine() == "arm64"`):

| Quantity | Measured |
|---|---|
| Model + policy load (once, shared) | 0.34 s |
| Per-role physics (3200 frames @ 50 Hz control, 200 Hz physics) | ~0.46-0.47 s each |
| All 8 roles, physics only | 3.70 s |
| Wall clock, cold process start to cache written | ~4.3 s |
| Output cache size | 2.40 MB |
| No falls; 5/5 `do` events logged `skill_unsimulated` (exactly the 5 in `octet.duckshow.json`: `kick_left`, `roulade`, two `sit_toggle`, `ground_pick`) | |

This lands well inside `docs/bake-parts.md` §3.2a's own measured spike (single duck, ~290x realtime) once shared model-loading and Python/import overhead are counted — consistent, not a new number contradicting that one.

## Not built yet

- **The diff itself.** `docs/viewer.md`'s stated payoff — drawing the kinematic dead-reckoned path and this baked path together and marking divergence ("lead is 38 cm left of its mark by 0:41") — is not computed or rendered anywhere. This document's `walkPhase` section above is the only place the two paths are related at all. Both paths are now on disk in comparable coordinates (this cache's `x`/`y`/`heading` vs. `editor/duckshow-viewer.js`'s `precomputeRolePath` output for the same show); wiring an actual diff view is future work, not attempted here.
- **BAM actuators**, the roller family, and the five skills, as detailed above.
- **Multi-process parallelism.** `docs/viewer.md`/`docs/bake-parts.md` frame the bake as "embarrassingly parallel, one process or worker per duck." This baker runs every role sequentially in one process instead (see "Physics model and constants" above for why that is fast enough in practice) — a future version wanting sub-second wall-clock on a much longer show could still parallelize across roles, since nothing here shares mutable state between them beyond the one read-only compiled `MjModel`.
