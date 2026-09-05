# The pose cache format, and how `tools/bake` produces one

`tools/bake` is the native **Create Preview** baker (`docs/viewer.md` "Create Preview (baked physics)"; `docs/bake-parts.md` for the research and feasibility case). It reads a `.duckshow`, drives a real MuJoCo simulation of each cast role against the shipped ONNX policies, and writes a **pose cache** — a `duckbake/1` JSON document — that the existing kinematic viewer (`docs/viewer.md`) can play back frame-for-frame, exactly the way it plays back the live kinematic sampler today. Physics is a bake step, not a live mode: nothing in the editor or duck-agent ever imports this tool.

## Why this is exempt from CLAUDE.md #1

`CLAUDE.md`'s hard rule #1 — *"Python is stdlib-only (3.10+). No pip dependencies, ever"* — exists so `python/duck_agent` runs on a stock Armbian image and `python/duckshow`/`python/mock_duck`/`python/tools` run on any Mac with nothing installed. `tools/bake` needs real `mujoco` and `onnxruntime`; there is no stdlib way to do rigid-body physics or run a trained network. `docs/viewer.md` already carves out the equivalent exemption for the *JavaScript* in-browser preview module; `docs/bake-parts.md` §3.3 names the same shape of carve-out for a native Python helper but leaves it unresolved, recommending it live in a directory "structurally separate from `python/`". This directory is that resolution:

- `tools/bake` is never imported by `python/duckshow`, `python/duck_agent`, `python/mock_duck`, or `python/tools/showmaster.py`.
- It is never installed on a duck and never required to author or run a show.
- Its dependencies live in `tools/bake/requirements.txt` and its own venv (`tools/bake/.venv/`, gitignored like every other `.venv/` in this repo), never in the repo's shared environment.
- The whole repo — editor, duck-agent, SwarmLink, tests — works with `tools/bake/` absent entirely.

## Setup

Needs `assets/microduck/` populated (gitignored, user-supplied — `docs/bake-parts.md` §2; never fetched or committed by this tool) and a Python **3.12** venv (narrower than this repo's general 3.10+ floor — `better-actuator-models` (PyPI, import name `bam`) pins `requires-python = ">=3.12,<3.13"`, and this baker depends on it directly now, not just optionally — see "Actuator model: BAM ported" below).

```bash
cd tools/bake
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python3 bake_show.py ../../shows/octet/octet.duckshow.json /path/to/octet.duckbake.json
.venv/bin/python3 bake_show.py ../../shows/octet/octet.duckshow.json /path/to/lead-only.duckbake.json --duck lead
```

`--duck ROLE` is repeatable, for fast single-duck iteration. `--quiet` suppresses per-role progress. The show is loaded and validated with the canonical `python/duckshow` parser/validator (imported via `sys.path`, not duplicated — `python/duckshow` is stdlib-only, so this is a one-directional dependency: `tools/bake` may import `python/duckshow`, nothing in `python/` ever imports `tools/bake`); a show that fails validation is refused, not baked around.

## What actually got fetched, vs. `bake-parts.md`'s proposal

`docs/bake-parts.md` §2's directory layout was explicitly "a **proposal**, not a contract — pick it (or change it) when the bake driver is actually written, and update this doc if it changes." The `assets/microduck/mjcf/` actually populated on this machine (2026-09) carries files that table didn't originally list — including a live rename this document caught while `tools/bake` was being written: upstream renamed the curated full-collision file `docs/bake-parts.md` originally catalogued as `robot_allcollisions.xml` to **`robot_groundcontact.xml`**, and reassigned the old name to a *different* file (every geom gets a matching collision copy, not the curated self-collision subset training actually used — `docs/bake-parts.md` §1a's own re-verification, done the same day, confirms this directly and says plainly the new `robot_allcollisions.xml` "is not on the needed list" for a bake driver). `assets/microduck/policies/manifest.json` also carries more structure than `bake-parts.md`'s original research pass covered (`obs_len`/`action_len`, per-policy `kind`/`command` encodings) — this baker reads and validates against it (`bakelib/policyset.py`), and it is what resolved several of the "how would this skill actually be driven" questions in "What isn't simulated" below.

**Update, 2026-09-04 (second correction): this baker now uses `scene_walk.xml` (→ `robot_walk.xml`), not `scene.xml` (→ `robot_groundcontact.xml`), for the one policy it actually drives.** The first correction above (picking `robot_groundcontact.xml` over the newly-repurposed `robot_allcollisions.xml`) was about avoiding the *wrong* full-collision file; this one is about a different axis entirely — `microduck_rl`'s own training config (`microduck_velocity_env_cfg.py`: `cfg.scene.entities = {"robot": MICRODUCK_WALK_ROBOT_CFG}`, resolved by `microduck_constants.py` via `spec_fn=get_walk_spec` to `MICRODUCK_WALK_XML = _ROBOT_DIR / "robot_walk.xml"`) trained `alpha_walking.onnx` against `robot_walk.xml`, not `robot_groundcontact.xml` at all — the groundcontact variant is what the *standup/ground-pick skill family* trains against (`get_standup_spec`), a different policy this v1 baker never drives. See "MJCF variant: switched to `robot_walk.xml`" below for what this changed (measured directly: nothing, for a specific and now-understood reason) and why the switch was still made.

## Physics model and constants

| Quantity | Value | Source |
|---|---|---|
| Physics timestep | 0.005 s (200 Hz) | `docs/bake-parts.md` §3.1, confirmed two ways. Neither exported MJCF carries a MuJoCo `<option>` element (confirmed by direct read of `robot_walk.xml`/`robot_groundcontact.xml` — grepped, none present) — this baker sets `model.opt.timestep` on the compiled `MjModel` after load rather than editing the XML, which has the same effect on `mj_step` without touching the fetched assets on disk. |
| Control decimation | 4 (→ 50 Hz control) | Same source. |
| Model file | `assets/microduck/mjcf/scene_walk.xml` (→ `robot_walk.xml`) | The MJCF `alpha_walking.onnx` was actually trained against (`microduck_velocity_env_cfg.py` → `get_walk_spec` → `robot_walk.xml`). Was `scene.xml` (→ `robot_groundcontact.xml`) until 2026-09-04 — see "MJCF variant: switched to `robot_walk.xml`" below; the earlier choice was a defensible pick between two *groundcontact*-family files (avoiding the newly-repurposed `robot_allcollisions.xml`), not a check against what training actually used. |
| Actuators | 14 joints driven by the ported BAM XL330 "m6" torque/friction model (`bam.mujoco.MujocoController`, nominal `vin=7.35V`, `kp_fw=200`), not MuJoCo's stock `<position>` actuator the MJCF ships | `bakelib/bam_actuator.py`, `docs/bake-parts.md` §3.5. The MJCF's own `<actuator>` block (`kp=0.55 kv=0.0 forcerange=[-0.96,0.96]`) is only the *pre-edit* starting point — `duckmodel.load_duck_model` converts these 14 `<position>` actuators to `<motor>` (torque) mode via `mujoco.MjSpec` before compiling. See "Actuator model: BAM ported" below for what changed, why, and what it did (and did not) fix. |
| Nominal standing pose | The scene's own `STAND` keyframe (`qpos`/`ctrl`, 14 joint values + trunk `z=0.12`) | Direct read of `scene_walk.xml`'s `<keyframe>` block — byte-identical to `scene.xml`'s (checked directly, both same-day corrections). Used as (1) every role's reset state, (2) the action-output zero-point (`ctrl = stand_qpos + action`), and (3) the zero-point `body_pose`/`head_pose` deltas are computed from. |
| Joint / action order | `left_hip_yaw, left_hip_roll, left_hip_pitch, left_knee, left_ankle, neck_pitch, head_pitch, head_yaw, head_roll, right_hip_yaw, right_hip_roll, right_hip_pitch, right_knee, right_ankle` (14) | The MJCF's own `<actuator>` declaration order, confirmed identical to `docs/bake-parts.md` §3.1's quoted action-space description ("0-4 left leg ..., 5-8 neck/head ..., 9-13 right leg"). `qpos[7:21]`/`qvel[6:20]` are these 14 in this order (freejoint occupies `qpos[0:7]`/`qvel[0:6]`). |

Loading the compiled model costs ~0.3 s (mesh parsing, mostly the ~26 MB of STLs); one `MjModel` is loaded once and shared read-only across every role's own `MjData` in a single process — ducks never interact physically (`docs/viewer.md`), so this is a safe speedup, not a fidelity shortcut, and it means this baker does not need the process-per-duck parallelism `docs/bake-parts.md` §3.2's estimates assumed to hit its numbers (see "Measured" below).

## Observation layout — confidence by field

`obs[61] = proprioception(48) ++ command(13)`, matching `assets/microduck/policies/manifest.json`'s declared `obs_len: 61` (checked at load; a mismatched manifest refuses to bake rather than guess).

**Confirmed exactly, 2026-09-04, two independent ways — this whole section's "not independently confirmed" hedging below is now resolved.** Investigating a report that baked ducks barely translate (see "Actuator model: BAM ported, low-speed bifurcation NOT fixed" below) required checking this baker's observation assembly against *something* other than its own reasoning. Two sources did that, agreeing with each other and with everything below to float32 precision:

1. **`craigm26/microduck-policy-golden-vectors`** (Hugging Face dataset, Apache-2.0, `sha256` of its `alpha_walking.onnx` reference matches `assets/microduck/policies/alpha_walking.onnx` byte-for-byte) — observation → action pairs for the real shipped policy, with an explicit `meta.obs_layout` string: `gyro(3) gravity(3) jointpos-home(14) jointvel(14) lastaction(14) cmd(13:[vx,vy,vyaw,neck_pitch,head_pitch,head_yaw,head_roll,0,0,z,roll,pitch,0])` — field-for-field identical to this baker's own layout, including the exact `[0, 0, z, roll, pitch, 0]` shape of `body_pose`. Feeding its four recorded observation vectors through this baker's own `policyset.run_locomotion_policy` reproduces its recorded actions to **max error 5.96e-08** (float32 rounding noise, not a discrepancy) — this checks the *whole* pipeline (field order, indices, and the ONNX forward call itself), not just the layout on paper. **Scope limit, checked 2026-09-04:** the four *observations* are hand-authored synthetic inputs, not telemetry from a walking robot — every value in them is a round number (`gyro` absmax exactly 0.10, `jointvel` absmax exactly 1.20, `lastaction` absmax exactly 0.35, `cmd[0]` exactly 0.15), and two of the four cases are all-zero or near-all-zero. The *actions* are genuine output of the real shipped policy (its `sha256` matches this repo's copy byte-for-byte), so the pairs do validate the forward call and the field placement exactly as claimed. They validate **nothing about units, scaling, or normalization**, because a pre-built observation never exercises the code that fills one in. Nor is the dataset's `obs_layout` an independent source for field *order*: it is a third party's inference from the same standard-practice convention this baker used. Field order is nevertheless confirmed, independently and authoritatively, by the ONNX's own `observation_names` metadata (`base_ang_vel,projected_gravity,joint_pos,joint_vel,actions,command,head_command,body_command`).
2. **`alpha_walking.onnx`'s own embedded metadata** — `onnxruntime.InferenceSession.get_modelmeta().custom_metadata_map`, read directly off the file already in `assets/microduck/policies/`, no fetch needed: `observation_names = base_ang_vel,projected_gravity,joint_pos,joint_vel,actions,command,head_command,body_command`, `command_names = twist,head_pose,body_pose`, `joint_names` (matches `duckmodel.JOINT_NAMES` exactly, same order), `default_joint_pos = 0.000,-0.087,-0.458,-0.005,0.453,0.349,0.349,0.000,0.000,0.000,0.087,0.458,0.005,-0.453` (matches `STAND.qpos[7:21]` to the metadata's own rounding), and `action_scale = 1.0`. This is the training export's own record of its contract, not a third party's re-derivation of it.

**Proprioception (48) = `gyro(3) ++ projected_gravity(3) ++ joint_pos_delta(14) ++ joint_vel(14) ++ prev_action(14)`.** Was flagged by `docs/bake-parts.md` §4 as *"Strand 1's inference from standard practice, not a literal quote"* — **now confirmed** by both sources above (`observation_names`'s own order; the golden vectors' own layout string and matching actions). Specifics:
- `gyro` — read from the MJCF's `imu_ang_vel` sensor (noise-free), not the also-present `angular-velocity` sensor (which the MJCF's own `sensors.xml` gives `noise="0.005"`). A bake wants a deterministic replay of the *policy*, not of training-time sensor-noise domain randomization. (This specific choice — noise-free sensor, not the noisy one — isn't itself distinguished by the golden vectors, whose test cases carry no angular velocity worth diffing between the two; still this baker's own reasoned choice, not contradicted by anything found.)
- `projected_gravity` — world gravity `[0,0,-1]` rotated into the trunk's body frame via the inverse of the `orientation` framequat sensor's quaternion. Confirmed by direct MJCF read that the `imu` site sits at `quat="1 -0 -0 -0"` (identity) on `trunk_base`, which is itself declared at identity relative to the free joint — so this sensor's frame *is* the trunk's own frame, not an offset one. Cross-checked against the golden vectors' `standing_at_home`/`turn_with_head` cases, whose only nonzero gravity component is index 5 (`z`) at exactly `-1.0` — an upright duck, matching.
- `joint_pos_delta` = `qpos[7:21] - STAND.qpos[7:21]`, `joint_vel` = `qvel[6:20]` raw. The delta-from-default convention for position (but not velocity) is, again, standard practice — **now confirmed**: the ONNX's own `default_joint_pos` field *is* the zero-point this baker already subtracts (`duck.stand_joint_qpos`, read from the same `STAND` keyframe), so there is no longer a gap between "what this baker assumes" and "what the policy declares."
- `prev_action` — the raw (unscaled) 14-vector this baker's own policy returned on the previous control tick; zero at each role's `t=0`. Matches the `actions` term named in `observation_names`.

**Command (13) = `twist(3) ++ head_pose(4) ++ body_pose(6)`.** Order confirmed by direct quote from `microduck_rl`'s task source (`docs/bake-parts.md` §3.6) — **now confirmed a second and third way**, by the golden vectors' explicit layout string and by the ONNX's own `command_names` metadata field, both independent of `microduck_rl`'s source and of each other.
- `twist = [vx, vy, vyaw]`, straight off the `.duckshow` `locomotion` track (`docs/duckshow-format.md`); `[0,0,0]` for a role with no locomotion track. The golden vectors' `walking_forward` case (`cmd[0] = 0.15`, nothing else in `cmd` nonzero) and `turn_with_head` case (`cmd[2] = 0.4`) land at exactly these indices.
- `head_pose = [neck_pitch, head_pitch, head_yaw, head_roll]`, each the `.duckshow` `head` track's value **minus the STAND keyframe's own value for that joint**. The upstream docstring `bake-parts.md` §3.6 quotes says the command is *"deltas from default joint positions"*; this baker's inference that "default" means the STAND keyframe is **now confirmed directly** — the ONNX's own `default_joint_pos` metadata field states it outright, closing the gap the previous version of this doc flagged ("not independently verified against `microduck_rl`'s env config, which might define `default_joint_pos` slightly differently"). `[0,0,0,0]` for a role with no head track.
- `body_pose = [0, 0, pose.z, pose.roll, pose.pitch, 0]` when `pose.active` is true; **`[0,0,0,0,0,0]` when `pose.active` is false or the role has no pose track.** `x`/`y`/`yaw` are always zero — confirmed by `docs/bake-parts.md` §3.6 to have no `robotd` wire equivalent at all, and now also confirmed literally: the golden vectors' own `obs_layout` string spells this exact sub-shape out as `[...,0,0,z,roll,pitch,0]`. The `active`-gating is a refinement over the literal formula: `docs/robotd-api.md` states `robot.pose` is *"glided while active, snaps back when false"* — real robotd ignores `z`/`roll`/`pitch` outright when inactive, not just visually but as the actual command sent to the policy. **This is a deliberate divergence from the kinematic preview**, which does not gate on `active` at all (`editor/duckshow-viewer.js` samples `pose.z`/`roll`/`pitch` unconditionally) — the kinematic path is a known simplification there; this baker's whole purpose is fidelity to what robotd will really do, so it honors the flag.

## Action mapping

`ctrl[14] = STAND.qpos[7:21] + action * action_scale`, `action_scale = 1.0`. **Confirmed directly, 2026-09-04** — `alpha_walking.onnx`'s own embedded metadata (`custom_metadata_map['action_scale'] = '1.0'`, read via `onnxruntime`'s `get_modelmeta()`, no fetch needed) states this outright; it is no longer an inference from `manifest.json`'s silence on the two non-roller policies. A second, independent source agrees for the right reason: `craigm26/duckkit` (a from-scratch Swift port of the real robot's control loop, `pollen-robotics/microduck`'s `robotd/src/control.rs` and `duck-control/src/model.rs`, read 2026-09-04) documents **two different correct values for two different domains** — the real robot's `robotd` de-rates walking to `action_scale = 0.9` in its own control loop, but its source comment is explicit that *"each `.onnx` carries `action_scale = 1.0` in its metadata and all six `microduck_rl` env configs agree, so a replay reproducing what the network did in simulation uses 1.0."* This baker is exactly that second kind of replay (a from-scratch MuJoCo physics run, not a client of real `robotd`), so `1.0` is the right constant for it, confirmed rather than assumed. Do **not** import `robotd`'s `0.9` here — that would replicate the wrong domain's de-rating on top of a simulation that never asked for it.

Also confirmed by the same `custom_metadata_map` read: `joint_damping = 0.000` for all 14 joints in the policy's own training config — i.e. the actuator BAM trained against ran with zero *static* joint damping (BAM's own runtime friction model supplies a dynamic, load-dependent replacement every step instead — `docs/bake-parts.md` §3.5). This baker's MJCF actuator preset (`chosen_actuator`, `docs/bake-format.md` "Physics model and constants" table) carries a small nonzero `damping="0.053" frictionloss="0.0048"` on those same joints — a real, now-confirmed (not just suspected) point of divergence from what the policy trained against, on top of the actuator-*type* substitution below. Tested directly (2026-09-04): zeroing `dof_damping`/`dof_frictionloss` on the 14 actuated joints does **not** close the low-speed walk-initiation gap described below (it neither fixes nor materially worsens it, and makes mid-range commands more erratic) — this confirms the damping term is a real but minor divergence, not the dominant one. **Update, same day, after the BAM port below:** this specific divergence is now closed as a side effect, not a workaround — `bakelib/bam_actuator.py`'s conversion of these 14 joints to BAM's torque-motor plant zeroes their MJCF `damping`/`frictionloss` permanently (BAM supplies its own dynamic, load-dependent friction every physics step instead, exactly matching what the policy trained against), so the static `0.053`/`0.0048` values in the table above no longer apply to a bake at all. See "Actuator model: BAM ported" below for what else that port did (and did not) fix.

## Which policy drives the bake

Every role, for the whole show, is driven by **`alpha_walking.onnx`** alone — the only policy this v1 baker loads. `manifest.json` marks both `alpha_walking` and `alpha_stand` `"kind": "perpetual"` but nothing in this repo's research (`docs/bake-parts.md`, `docs/robotd-api.md`) documents a rule for *when* real robotd switches between them — `robot.setMode` only ever names `"walk"`/`"roller"` (a completely different axis, the roller-family swap). Always using `alpha_walking`, including at moments the locomotion command is exactly zero, is this baker's own simplification.

**Measured consequence, not just a theoretical caveat:** this baker's own verification run found `alpha_walking.onnx` tracks `twist` (walking velocity) convincingly — a sustained `vx=0.3` command produced ~0.56 m of real forward travel over 4.5 s of physics, roughly matching the commanded speed within the actuator-fidelity gap below — but tracks a `body_pose.z` crouch only weakly: a sustained `z=-0.048` command (near the format's own ±0.05 m validation ceiling) settled at an actual trunk-height delta of about **-0.003 to -0.005 m**, not the commanded -0.048 m, even held for 3.6 s. Observation wiring was checked directly (the command lands at the exact expected `obs` index) so this is not an indexing bug; it reads as `alpha_walking.onnx` genuinely prioritizing velocity tracking over static pose-hold, plausible for a policy whose name and `"perpetual"` walking role suggest that emphasis. **A skill/pose-hold-focused policy (`alpha_stand.onnx`, or a per-skill policy) likely tracks `body_pose` far better** — this is exactly the kind of thing the "one policy for everything" simplification above costs, named honestly rather than smoothed over. A show role that leans mainly on `pose.z`/`roll`/`pitch` crouches (e.g. `octet.duckshow.json`'s `sable`, "the crouch solo") will bake with much shallower crouches than the `.duckshow` file specifies.

## Actuator model: BAM ported, low-speed bifurcation NOT fixed (2026-09-04)

**Status: still open.** This section used to be titled "Known bug: show-authored walking speeds don't reach a gait" and end with "not fixed in this pass, get `better-actuator-models[mujoco]` installed and re-run the sweep." That dependency is now installed and the port described in that section's own "Next step" is done — `bakelib/bam_actuator.py` drives the trained policy against the same XL330 voltage-control-law torque/friction plant (`bam.mujoco.MujocoController`, the package's own plain-NumPy CPU integration path) that `alpha_walking.onnx` was actually trained against, in place of MuJoCo's stock `<position>` actuator. **Measured result: the walk-initiation bifurcation is essentially unchanged.** The hypothesis that motivated the port — a torque-controlled actuator would kick the system over into a stepping gait at much lower commanded speeds than a soft position spring — is **not supported** by direct measurement, for a specific, now-understood reason (see "Why BAM didn't move the threshold" below). This is reported plainly rather than papered over by raising the show's commanded speeds, per this repo's own standing instruction: a documented limitation beats a misleading preview.

### What was ported, and how

`bakelib/bam_actuator.py` follows `docs/bake-parts.md` §3.5's five steps against the installed `better-actuator-models==1.0.2` (PyPI; import name `bam`; `tools/bake/requirements.txt`):

1. **Load**: `bam.model.load_model(motor_name="xl330", model="m6")` — the exact bundled parameter file (`bam/params/xl330/m6.json`) `microduck_constants.py`'s `_BAM_ACTUATOR_KWARGS` names (`motor_name="xl330", model="m6"`), fetched from `pollen-robotics/microduck_rl`'s `develop` branch to confirm.
2. **Convert actuators**: `duckmodel.load_duck_model` now loads the scene as an editable `mujoco.MjSpec` (not directly to `MjModel`) and calls `bam_actuator.edit_spec_to_bam`, which converts each of the 14 `<position>` actuators to `<motor>` (direct-torque) mode via MuJoCo's own `MjSpec.set_to_motor()`, sets a voltage-derived `forcerange` ceiling and `gear=[1,0,0,0,0,0]`, and zeroes each joint's `armature`/`damping`/`frictionloss` (BAM supplies its own dynamic replacement every step) — a direct, line-by-line mirror of `bam.mjlab.BamActuator.edit_spec()`'s non-mjlab-specific logic (`bam/mjlab.py` lines ~265-305, read directly), minus the parts that only make sense for the PyTorch/MuJoCo-Warp GPU path this baker does not use.
3. **Drive it**: `bakelib/sim.py`'s per-role loop now calls `controller.set_q_target(name, angle)` once per **control** tick (50 Hz, same as before — the policy's target angle doesn't change faster than that), then `controller.update()` once per **physics** substep (200 Hz) immediately before each `mujoco.mj_step()` — mirroring `bam.mujoco.Simulator.step()`'s own one-`update()`-per-`mj_step()` pattern exactly, since a real servo's internal firmware PID loop runs far faster than the 50 Hz policy that sets its target.
4. **Integration path chosen deliberately**: `bam.mujoco.MujocoController` (plain NumPy, no PyTorch/MuJoCo-Warp), not `bam.mjlab.BamActuator` (the GPU path `microduck_rl`'s own training code uses) — the right choice per §3.5 step 4's own framing ("no reimplementation ... this class already is the CPU torque/friction loop"), and the only one that fits a single-process CPU bake.
5. **De-randomization** (bake-only — training randomizes these per episode; a bake must be byte-identical across runs): fetched `microduck_constants.py`'s actual `_BAM_ACTUATOR_KWARGS` from `pollen-robotics/microduck_rl`'s `develop` branch to get real numbers rather than guess.
   - **`kp_fw = 200.0`** — not randomized in training; used as-is.
   - **Battery voltage** — training samples per-episode from `vin_range=(6.5, 8.2)`. This bake fixes `vin` at the range's **midpoint, 7.35 V**, for every joint, every role, the whole show — the least-arbitrary single number absent a stated non-randomized default. **Checked for sensitivity, not just asserted**: re-ran the threshold sweep below at both ends of the trained range (6.5 V and 8.2 V) as well as the midpoint — the walk-initiation threshold does not move meaningfully (stays between `vx=0.22` and `vx=0.25` at all three voltages). The choice of nominal voltage is not why the bifurcation persists.
   - **Voltage sag under load** — training additionally randomizes a sag gain (`vin_drop_gain_range=(0.0, 0.2)`, floored by `vin_min=6.0`). `MujocoController`'s own sag mechanism (`vin_drop_resistance`, an Ohms value) is a different formula from mjlab's gain-on-summed-torque one, so there is no single equivalent number to carry over — de-randomized to the distribution's own zero endpoint instead (`vin_drop_resistance=None`, i.e. no sag, constant `vin` every step), which is a real member of the trained range, not an extrapolation.
   - **Command delay** — training randomizes an integer per-episode lag (`delay_min_lag=3, delay_max_lag=6` steps). **Not modeled at all**: `bam.mujoco.MujocoController` has no delay parameter of any kind — that feature exists only on the mjlab/torch GPU path, and (confirmed by direct read of this installed 1.0.2's `bam/mjlab.py`) only on a newer version than PyPI ships: `microduck_rl`'s `pyproject.toml` actually sources `better-actuator-models` from `github.com/Rhoban/bam` branch `mjlab_frictionloss`, not this PyPI release, and that branch's `BamActuatorCfg` carries fields (`vin_drop_gain_range`, `delay_min_lag`, `delay_max_lag`) that are documented in 1.0.2's own docstring but not present as real dataclass fields on it — a genuine version drift between what training used and what's installed here. This bake therefore runs at zero command delay, same as the pre-BAM baker; not a new gap this port introduces, just one it doesn't close either.

  Every constant and the full reasoning above lives in `bakelib/bam_actuator.py`'s module docstring and per-constant comments, not just here.

### Why BAM didn't move the threshold

The stock `<position>` actuator's `kp=0.55` was not an arbitrary placeholder — `joints_properties.xml`'s own comments name several past hand-calibration passes against real hardware. Computing BAM's own **small-signal proportional stiffness** at low speed (where back-EMF and the current limiter are not yet binding) gives `torque/error ≈ kt · vin · kp_fw · error_gain / R = 0.366 × 7.35 × 200 × 0.002876 / 2.811 ≈ 0.55 Nm/rad` — **matching the stock actuator's `kp=0.55` almost exactly.** That is very unlikely to be a coincidence: the hand-tuned position-actuator preset appears to already be a good linear approximation of BAM's own behavior in exactly the low-error, low-speed regime where the reported bug lives. The mechanism this section originally proposed — "a torque-controlled actuator can deliver a usable fraction of its torque near-instantly regardless of position error" — turns out not to distinguish BAM from the stock preset at all, because BAM's own firmware control law is *itself* a proportional position controller at its core (`duty_cycle = (target − q) · kp_fw · error_gain`, current-limited but not saturating at the errors seen here); it is not a source of large near-instant torque independent of position error the way the original hypothesis assumed.

**This points to the real bifurcation being a property of `alpha_walking.onnx`'s own trained behavior, not of the actuator plant driving it** — the network's own action output settles to a static, non-periodic correction below roughly `vx≈0.22-0.24` regardless of which (reasonably-calibrated) plant executes it, and breaks into a periodic gait above it. Whether that is deliberate (a trained "stand vs. walk" mode switch keyed to commanded speed) or an artifact of the training distribution is outside what this pass can determine — but it is no longer plausible to attribute it to the actuator-type substitution this section originally blamed.

### Measured: the sweep, before and after

Same methodology as before (sustained, constant `twist.vx` from a cold stand, isolated from the show, 3 s hold — reproduced with the exact `bakelib` code, not a separate script) run again against the ported BAM actuator:

| commanded `vx` (m/s) | net travel, 3 s hold (BAM) | character |
|---|---|---|
| 0.05 – 0.20 | 0.003 – 0.010 m | converges to a **static** near-STAND correction, same as before BAM |
| 0.21 – 0.23 | 0.011 – 0.012 m | still static; the transition is between `vx=0.23` and `vx=0.24` under BAM, essentially the same order as before (was `0.20`–`0.22`) |
| 0.24 | 0.14 m | breaks into a genuine walking limit cycle |
| 0.25 – 0.40 | 0.19 – 0.46 m (3 s) | clean walking, travel scales with commanded speed — same order of magnitude as the pre-BAM numbers in the same regime |

**The exact three speeds this project's success criteria named, matched exactly (both actuators run through the identical `bakelib` sweep code, only the actuator model swapped), 3 s hold:**

| commanded `vx` (m/s) | net travel, stock `<position>` actuator (before) | net travel, ported BAM actuator (after) |
|---|---|---|
| 0.10 | 0.0060 m | 0.0055 m |
| 0.15 | 0.0080 m | 0.0080 m |
| 0.20 | 0.0108 m | 0.0102 m |

No meaningful difference at any of the three — BAM does not produce a stepping gait at 0.15 m/s (or 0.10 or 0.20) any more than the stock actuator did.

A 10 s (not 3 s) hold at `vx = 0.10, 0.15, 0.20` confirms the low-speed regime is a genuine converged fixed point, not a slow transient that more time would resolve (travel stays at the same few-millimeter scale at 10 s as at 3 s).

**Success criterion 1 (a sustained `vx=0.15` should produce a genuine stepping gait) is not met.** `vx=0.15` still converges to a static correction under BAM, exactly as it did under the stock actuator.

**Success criterion 2 (baking `shows/octet/octet.duckshow.json` should give `lead` an x displacement on the order of 15-20 cm) is not met either.** Baked with BAM (2026-09-04): every role's `x` range is **0.0112-0.0114 m** and `y` range **0.0033-0.0043 m** — `lead` specifically: `x` 0.0112 m, `y` 0.0033 m — statistically the same as the pre-BAM baseline (`lead`: `x` 0.0134 m, `y` 0.0034 m) and for the same reason: `octet.duckshow.json` never commands more than `vx=0.2` on any role (unchanged fact, re-verified), which sits on the no-gait side of the threshold under BAM too.

**Update, 2026-09-04, later the same day: still not met after two further fixes.** The MJCF-variant swap ("MJCF variant: switched to `robot_walk.xml`" below) and a direct check of whether the BAM `kp_fw`/`vin` override is actually applied ("BAM parameters: confirmed applied, not the bug" below) were investigated next, since both were plausible enough to be worth ruling out with numbers rather than by reasoning. Neither criterion above changes: `vx=0.15` still converges to the same few-millimetre static correction under `robot_walk.xml` as it did under `robot_groundcontact.xml` (bit-identical to 4 decimal places at every tested speed), and re-baking `octet.duckshow.json` under the corrected MJCF gives `lead` the same `x` 0.0112 m / `y` 0.0033 m as before (see "Measured" below for the full post-fix per-role table). The BAM kp/vin override turns out to already have been applied correctly before this pass even started.

**History, pre-BAM (stock `<position>` actuator, kept for reference — the mechanism explanation above supersedes this table's original "root cause" framing, but the raw numbers are still real measurements):**

| commanded `vx` (m/s) | net travel, 3 s hold (stock actuator) | character |
|---|---|---|
| 0.05 – 0.20 | 0.003 – 0.010 m | converges to a **static** near-STAND correction; legs sway a few degrees and settle, no stepping |
| 0.22 | 0.21 m | breaks into a genuine, self-sustaining walking **limit cycle** — knee/hip actions swing ±0.3-0.6 rad in a real alternating gait |
| 0.25 – 0.40 | 0.28 – 0.53 m (3 s) | clean walking, travel scales roughly with commanded speed |

### Other criteria, measured

- **Determinism (criterion 3): met, exactly.** `shows/octet/octet.duckshow.json` baked twice, back to back: `poses` and `log` are byte-identical between the two runs (compared as parsed JSON, not just diffed as text), and `cache_key` is identical. Only `bake.generated_at` (wall-clock timestamp) and `bake.wall_clock_s` (measured duration) differ, exactly as the cache format already documents as varying per-run metadata, not part of `cache_key`. BAM introduces no randomness anywhere in this path (no `np.random` call in `bam.model`/`bam.actuator`/`bam.mujoco`, confirmed by direct read) — every domain-randomization axis training uses is fixed at a single nominal value, not resampled (see "De-randomization" above), so this was expected, not lucky.
- **`BAKE_LAYOUT_VERSION` bumped 1 → 2** (`bakelib/posecache.py`): the cache key's `physics` field (`timestep`/`decimation`/`control_hz`/`scene`) never named the actuator model, so a pre-BAM and post-BAM bake of the identical show would otherwise have produced the identical `cache_key` despite genuinely different simulated trajectories — exactly the "silently wrong, not just stale" case `BAKE_LAYOUT_VERSION` exists for.
- **Honest fall reporting (criterion 4): the fall-detection code itself is unchanged** (`bakelib/sim.py`, reads `data.qpos`/orientation regardless of what wrote `data.ctrl`) and was already correct pre-BAM. `shows/octet/octet.duckshow.json` produces no falls either before or after this port (`fallen_roles: []`, both runs) — the show's commanded speeds never leave the static-correction regime, so there is nothing to fall from. Checked directly that the path still fires under BAM rather than assuming it: a 40 N, 0.2 s lateral shove applied mid-walk (`vx=0.3`, well into the walking regime) produces `roll=-1.39 rad`, past the `FALL_TILT_RAD=1.0` threshold, correctly detected. Separately, a battery of out-of-distribution commands (`vx` up to 1.0 m/s, `vyaw` up to 3.0 rad/s) produced no NaN and no fall under BAM — the ported plant is not spuriously unstable.
- **Performance**: BAM costs real wall-clock, as expected — `controller.update()` now runs once per physics substep (200 Hz) instead of a single `ctrl` assignment per control tick (50 Hz), a 4x increase in per-step actuator work. See "Measured" below for the updated whole-show timing.

**Not fixed. Root cause is still open**, now narrowed: it is very likely intrinsic to `alpha_walking.onnx`'s own trained behavior rather than a sim-vs-train actuator-plant mismatch, per "Why BAM didn't move the threshold" above, but this pass cannot fully confirm that without access to `microduck_rl`'s actual training curves or a second policy to compare against. No show constant was changed to work around this — `shows/octet/octet.duckshow.json` still commands what it always did, and this remains a documented limitation of what a bake of this show can show at these speeds, not a bug in the bake pipeline's fidelity to the policy.

**Symptom, as originally reported (unchanged by this pass):** baking `shows/octet/octet.duckshow.json` produces ducks that barely translate. `lead`'s `x` ranges 0.0134 m and `y` 0.0034 m over the whole 64 s show, against `locomotion` keyframes that ramp to `vx=0.15` (held 1 s, twice, forward and back) and `vx=0.2` (held 1 s, twice) — call it 15-20 cm of intended travel per segment, not ~1 cm total. All 8 roles show the same ~0.013 m range; none fall (`fallen_roles: []`); `unsimulated_roles: []`. Head motion (`headYaw` range 1.11 rad, `headPitch` 0.55, `walkPhase` reaching 2.49 over the show) is large and clearly not stuck — **and is confirmed genuinely simulated, not passed through**: `bakelib/sim.py`'s `simulate_role` reads `head_yaw[k]`/`head_pitch[k]`/`head_roll[k]`/`neck_pitch[k]` straight from `data.qpos[hi[...]]` — live MuJoCo physics state, driven by the same `ctrl` targets and `mj_step` loop as the legs — never from the show's `head` track directly (that track only ever reaches the policy as the `head_pose` *command*, `bakelib/observation.py`'s `build_command`). Only `mouthOpen` is a literal passthrough, and that is correct by design (see "Physics model and constants" / `docs/bake-parts.md` §3.1 — the trained action space excludes the mouth entirely).

**Ruled out, each checked directly rather than by re-reading the code:**
- **Observation layout** — field order confirmed correct (the ONNX's own `observation_names`; golden-vector actions match to 5.96e-08), see "Observation layout — confidence by field" above (golden-vector actions match to 5.96e-08; the ONNX's own metadata confirms field order, `default_joint_pos`, and command layout). **Normalization/scaling is not confirmed by any of these** — no source found states whether training applied per-term observation scales, and this baker applies none. The golden vectors cannot speak to it (see the scope limit above). Evidence that it is *not* a live problem: the policy walks correctly above its stand/walk gate, which a systematic scale error would break at every speed rather than only below a threshold.
- **Action mapping / `action_scale`** — confirmed `1.0` is exactly right for this domain (a from-scratch physics replay), from the ONNX's own metadata and cross-checked against an independent robot-runtime port. See "Action mapping" above.
- **`twist` command slot, units, sign** — confirmed correct: golden vectors' `walking_forward`/`turn_with_head` cases land `vx`/`vyaw` at exactly the indices and signs this baker uses.
- **Physics steps per control step** — 200 Hz physics, decimation 4 (50 Hz control), independently confirmed twice already (`docs/bake-parts.md` §3.1) and reconfirmed by direct read of `bakelib/sim.py`'s decimation loop (`ctrl` set once, then `mujoco.mj_step` called `CONTROL_DECIMATION` times unchanged) — correct.
- **The duck being held, constrained, or spawning unrecoverable** — checked by direct instrumentation (see below): trunk height stays at ~0.116-0.120 m against a 0.12 m nominal the whole time, never drops, no fall heuristic fires. No constraint bug, no bad spawn.

**What's actually happening, found by instrumenting `bakelib/sim.py`'s loop directly (not by reasoning about it) and reproduced in isolation, outside the show, with the exact same `bakelib` code:** driving `alpha_walking.onnx` with a **sustained, constant** `twist.vx` command from a cold stand (this baker's own reset state) shows a sharp, repeatable bifurcation in the closed-loop dynamics:

| commanded `vx` (m/s) | net travel, 3 s hold | character |
|---|---|---|
| 0.05 – 0.20 | 0.003 – 0.010 m | converges to a **static** near-STAND correction; legs sway a few degrees and settle, no stepping |
| 0.22 | 0.21 m | breaks into a genuine, self-sustaining walking **limit cycle** — knee/hip actions swing ±0.3-0.6 rad in a real alternating gait |
| 0.25 – 0.40 | 0.28 – 0.53 m (3 s) | clean walking, travel scales roughly with commanded speed |

The transition sits between `vx=0.20` and `vx=0.22`, and is sharp, not gradual (see the raw sweep: 0.20→0.0103 m, 0.22→0.2075 m, over an identical 3 s hold). Below it, the policy's own raw action output settles to a near-constant vector after a short transient — no periodicity at all. At and above it, the same network, same code path, produces a clearly periodic action trace and the duck actually steps. This is not an artifact of the show's ramped/pulsed commands either: the same threshold reproduces with a flat, constant `vx` held from `t=0`, isolated from `octet.duckshow.json` entirely.

**`shows/octet/octet.duckshow.json` never commands more than `vx=0.2`/`vy=0` (checked across all 8 roles' `locomotion` tracks: every role's maximum `√(vx²+vy²)` is exactly 0.200, `vyaw` is always 0) — every commanded speed in this show sits at or under the no-gait side of this threshold.** That fully accounts for the reported symptom: it isn't that the show's forward-then-back pulses cancel out to a small *net* while the duck still walks in between (which would still show up as a large `x`/`y` *range*, even with near-zero net displacement) — the duck is measurably not walking at all during those pulses, at any point.

**Root cause, as hypothesized at the time (superseded — kept as history, see "Why BAM didn't move the threshold" above for what direct measurement against the real BAM plant actually found):** this was suspected to be the already-documented, already-named BAM actuator gap (`docs/bake-parts.md` §3.5, and "What isn't simulated" below). `alpha_walking.onnx` was trained against BAM's voltage-control-law torque/friction model; at the time this baker drove it against MuJoCo's stock `<position>` actuator (`kp=0.55`, `forcerange=[-0.96,0.96]` — an intentionally hand-tuned approximation, per `joints_properties.xml`'s own comments naming several past calibration passes, not a placeholder). The hypothesized mechanism was that a real torque-controlled actuator (BAM, or the real servo) could deliver a usable fraction of its torque near-instantly regardless of position error, letting even a small commanded velocity kick the system over into a stepping limit cycle, where this baker's soft position spring supposedly could not. **This has since been tested directly and does not hold** — BAM's own firmware control law turns out to be a proportional position controller too, with a small-signal stiffness that happens to match the stock preset's `kp=0.55` almost exactly (see "Why BAM didn't move the threshold" above), so the actuator-*type* substitution was never the right axis.

**Tested and ruled out as a quick fix (pre-BAM):**
- **Zeroing joint damping/frictionloss** to match the policy's own confirmed `joint_damping=0` metadata (see "Action mapping" above) — did not close the gap; low-speed commands still failed to produce a gait, and the zeroed-friction sweep was *more* erratic at mid-range speeds (`vx=0.18`/`0.20` produced negative net travel — a stumble, not a walk), not cleaner. (This specific divergence is now closed as a side effect of the BAM port, which zeroes these same joints' static damping/frictionloss permanently — see "Action mapping" above — but that closure did not move the bifurcation either, consistent with this bullet's original finding that it was a real but minor divergence.)
- **A naive isotropic actuator-strength boost** (`kp` and `forcerange` both ×5, as a quick test of "is it simply underpowered") did **not** produce clean low-speed walking either — it produced instability (NaN in `qacc` at `vx=0.18`, wrong-direction net travel at `vx=0.10`/`0.15`/`0.20`). At the time this was read as inconclusive (a crude hack on the existing PD law, not a real torque-controller); with the real torque-controller now measured to behave the same way at low speed, this result reads as a correct negative in hindsight too, not just an inconclusive one.

**Conclusion after the BAM port: not fixed, and the root cause has moved.** The principled fix `docs/bake-parts.md` §3.5 scoped — porting `better-actuator-models[mujoco]`'s `bam.mujoco.MujocoController` against the XL330 parameters it ships with — is now done (`bakelib/bam_actuator.py`), and it does not change the walk-initiation threshold in any way that matters (see "Measured: the sweep, before and after" above). No constant in `bakelib/` was tuned to force a different result — `kp_fw`/`vin` are the values `microduck_rl`'s own training config actually used (fetched from source, not guessed), and `action_scale` is untouched (still confirmed `1.0`, unrelated to this finding). The evidence now points at `alpha_walking.onnx`'s own trained behavior as the more likely explanation (see "Why BAM didn't move the threshold" above) rather than at a sim-vs-train plant mismatch. **Update, 2026-09-04, later the same day: two further candidates (the MJCF variant driving locomotion, and whether the BAM kp/vin override was actually applied) were investigated and tested directly — see the next two sections. Neither moves the threshold either; both are reported in full below rather than silently ruled out.**

## MJCF variant: switched to `robot_walk.xml`, low-speed bifurcation NOT fixed (2026-09-04)

**What was wrong, independent of everything above:** this baker loaded `scene.xml` (→ `robot_groundcontact.xml`) for the one policy it drives, `alpha_walking.onnx` — but `microduck_rl`'s own training config trains that policy against a *different* MJCF entirely. `microduck_velocity_env_cfg.py` sets `cfg.scene.entities = {"robot": MICRODUCK_WALK_ROBOT_CFG}`; `microduck_constants.py` resolves that through `spec_fn=get_walk_spec` to `MICRODUCK_WALK_XML = _ROBOT_DIR / "robot_walk.xml"`. The standup/ground-pick skill family uses `get_standup_spec` → `robot_groundcontact.xml` instead — a different policy (`alpha_sitstand.onnx`/`alpha_ground_pick.onnx`), one this v1 baker never drives. So the model this baker was actually stepping `alpha_walking.onnx` against was the *skill* family's collision model, not its own. Fixed: `bakelib/duckmodel.py`'s `SCENE_FILENAME` is now `scene_walk.xml` (→ `robot_walk.xml`); see that constant's own module comment for the full reasoning trail.

**What actually differs between the two files, direct diff (2026-09-04):** `robot_walk.xml` has 6 fewer geoms than `robot_groundcontact.xml` (`ngeom` 76 vs. 82, confirmed by loading both through `duckmodel.load_duck_model` and reading `model.ngeom`). Every dropped or downgraded geom is an *internal self-collision* mesh — `hip_l` (×2, left/right), `leg` (downgraded `class="collision"` → `class="self_collision_only"`, ×2), `top_head_shell`, `jaw`, `bottom_head_shell`, and the `np_f970` sensor-bracket mesh. The geoms that actually generate ground-reaction force — `left_foot_collision`/`right_foot_collision` (mesh `sole_left`/`sole_right`) — are byte-identical in both files: same `pos`, same `quat`, same mesh reference. So is the `<actuator>` block (`chosen_actuator`, `kp="0.55"`), the `STAND` keyframe, and the sensor set (checked directly, diffed both ways).

**Measured: bit-for-bit identical to the post-BAM baseline, at every tested speed.** Same methodology as the actuator sweep above (sustained constant `twist.vx` from a cold `STAND` reset, isolated from any show, driven through the exact `bakelib` code with only `SCENE_FILENAME` changed):

| commanded `vx` (m/s) | net travel, 3 s hold (`robot_groundcontact.xml`, before) | net travel, 3 s hold (`robot_walk.xml`, after) |
|---|---|---|
| 0.10 | 0.0055 m | 0.0055 m |
| 0.15 | 0.0080 m | 0.0080 m |
| 0.20 | 0.0102 m | 0.0102 m |
| 0.21 | 0.0107 m | 0.0107 m |
| 0.22 | 0.0113 m | 0.0113 m |
| 0.23 | 0.0118 m | 0.0118 m |
| 0.24 | 0.1407 m | 0.1407 m |
| 0.25 | 0.1905 m | 0.1905 m |
| 0.30 | 0.2880 m | 0.2880 m |
| 0.40 | 0.4595 m | 0.4595 m |

Every value matches to the 4th decimal digit (the sweep's own rounding). The walk-initiation threshold sits between `vx=0.23` and `vx=0.24` under both files — unchanged. Baking `shows/octet/octet.duckshow.json` end-to-end under `robot_walk.xml` reproduces this: `lead`'s `x` range is `0.0112 m` (was `0.0112 m` under `robot_groundcontact.xml`, post-BAM), `y` range `0.0033 m` (was `0.0033 m`) — see "Measured (this run)" below for the full per-role table.

**Why the swap made no difference: the reason follows directly from the diff above.** The low- and mid-speed regime this bug lives in never engages self-collision — an upright or gently-swaying-but-not-stepping duck's legs never touch its own torso or each other, and neither does its head. The only geoms `robot_walk.xml` drops are self-collision-only geoms; the foot-ground contact geometry that actually generates the reaction forces driving locomotion is identical between the two files. A model that only differs in self-collision detail cannot change dynamics in a regime where self-collision was never firing in the first place. (Whether it fires *above* the threshold, in the clean-walking regime, wasn't separately isolated — but the sweep's `vx=0.24`-`0.40` rows are also bit-identical, so evidently not in a way that matters there either, at least not for straight-ahead walking with no turning.)

**Still the right thing to do, independent of this result.** `alpha_walking.onnx` should be driven against the MJCF it was actually trained on regardless of whether doing so happens to fix this particular symptom — the same fidelity argument that motivated the BAM actuator port. The fix is kept.

**What this means for skills and roller mode, going forward (not implemented — the v1 baker doesn't drive those policies at all):** a future baker that actually drives `alpha_sitstand.onnx`/`alpha_ground_pick.onnx`/`ball_kick_*.onnx`/`roulade.onnx` for their event window would need `robot_groundcontact.xml` for that window specifically (that's the model those policies trained against), and `robot_groundcontact_rollers.xml` for roller mode — a single MJCF is not simultaneously correct for the walk family and the skill/roller families. That would mean either loading multiple compiled `MjModel`s up front (cheap: one `MjModel` load is ~0.3-0.4 s, dominated by mesh parsing, and this baker already tolerates loading one) and switching which one drives a role's `MjData` at skill-event boundaries, or accepting the walk-model's approximation during a skill window as a documented, lesser fidelity gap. Neither is built; this v1 baker still just logs `skill_unsimulated`/`mode_unsimulated` and keeps `alpha_walking.onnx` running unmodified through those stretches, same as before this fix.

## BAM parameters: confirmed applied, not the bug (2026-09-04)

**The suspicion:** `microduck_constants.py` configures BAM's `_BAM_ACTUATOR_KWARGS` with `kp_fw=200.0, vin_range=(6.5, 8.2), vin_drop_gain_range=(0.0, 0.2), vin_min=6.0, delay_min_lag=3, delay_max_lag=6` for every training config. The installed `better-actuator-models==1.0.2` package's own `XL330Actuator` (`bam/dynamixel/actuator.py`) defaults to `kp=400, vin=7.5` — the generic Dynamixel-XL330 defaults, not MicroDuck's trained values, and roughly double `kp_fw`. If `bakelib/bam_actuator.py` silently used those defaults instead of MicroDuck's own trained constants, the resulting actuator would be a different (and, on paper, stiffer) plant than `alpha_walking.onnx` was ever trained against — worth checking directly rather than assuming the earlier port got it right.

**Checked directly, three ways — the override is applied, not defaulted:**

1. **Direct code read.** `bakelib/bam_actuator.py`'s `load_bam_model()` calls `model.actuator.kp = KP_FW` (`KP_FW = 200.0`) and `model.actuator.vin = VIN_NOMINAL` (`7.35`, the midpoint of the trained `vin_range=(6.5, 8.2)` — training randomizes per-episode, a bake needs one fixed value; see "Actuator model: BAM ported" above for that de-randomization reasoning, unchanged by this check) unconditionally, every time the function runs. There is no code path that skips these two assignments.
2. **This is the package's own sanctioned override idiom, not a workaround.** `bam.mujoco.load_config()` (`bam/mujoco.py`, the package's own reference multi-actuator loader) applies a non-default kp/vin with the exact same pattern: `model.actuator.kp = kp; model.actuator.vin = vin`. `bakelib/bam_actuator.py` does the same thing by the same mechanism the package's own author uses elsewhere in the package — not a fragile monkeypatch of an internal.
3. **Numerically self-consistent with the rest of this document.** The "Why BAM didn't move the threshold" small-signal-stiffness calculation above (`torque/error ≈ kt · vin · kp_fw · error_gain / R = 0.366 × 7.35 × 200 × 0.002876 / 2.811 ≈ 0.55 Nm/rad`) only reproduces the stock actuator's `kp=0.55` using the *applied* values (`kp_fw=200, vin=7.35`) — the package's own defaults (`kp=400, vin=7.5`) would put that stiffness at roughly double, around `1.1 Nm/rad`, which would **not** match the stock preset. The earlier finding's own math already depended on the override being live; this is a second, independent confirmation of the same fact.

**Checked the counterfactual too, not just the code path.** A diagnostic sweep (kept out of `bakelib/`, run only for this check) monkeypatched `load_bam_model()` to skip both assignments, leaving the package's own `kp=400, vin=7.5` defaults, and re-ran the identical 3 s constant-`vx` sweep:

| commanded `vx` (m/s) | net travel, applied constants (`kp_fw=200, vin=7.35`) | net travel, package defaults (`kp=400, vin=7.5`) |
|---|---|---|
| 0.10 | 0.0055 m | 0.0062 m |
| 0.15 | 0.0080 m | 0.0083 m |
| 0.20 | 0.0102 m | 0.0101 m |
| 0.24 | 0.1407 m | 0.2799 m |
| 0.30 | 0.2880 m | 0.4167 m |
| 0.40 | 0.4595 m | 0.5712 m |

Doubling the firmware gain measurably changes the *walking* regime (`vx≥0.24`: more travel per hold, a stiffer actuator tracks the gait target harder) — so the override is not inert, the sweep is sensitive to it. But at the three speeds that matter (`0.10`/`0.15`/`0.20`), the package defaults still converge to the same few-millimetre static correction as the correctly-applied constants — even a roughly 2x stiffer firmware gain does not unlock a gait in this regime. **Conclusion: not only is the kp_fw/vin override already correctly applied (confirmed three ways above), but even the hypothetical broken case — silently using the package's defaults — would not have explained the reported symptom either.** This finding is ruled out as the bug, not just cleared on a technicality.

**What's still genuinely not modeled** (documented already, unchanged by this check, not re-litigated here): voltage sag under load (`vin_drop_gain_range`) and command delay (`delay_min_lag`/`delay_max_lag`) are deliberately not applied — `bam.mujoco.MujocoController`'s CPU integration path has no delay parameter at all, and its sag mechanism (`vin_drop_resistance`, an Ohms value) isn't numerically equivalent to mjlab's gain-on-torque formula, so there's no single number to carry over. Both are de-randomized to their distribution's zero/off endpoint for bake determinism — see "Actuator model: BAM ported" → "De-randomization" above for the full per-parameter reasoning. Nothing found in this pass changes that.

## The low-speed problem is a stand/walk gate in the policy (2026-09-04)

**Resolved.** The ducks were not moving because `alpha_walking.onnx` does not
produce a gait at all below a sharp commanded-speed threshold, and every show in
this repo commands below it.

### The measurement that settled it

Earlier passes measured *displacement* and found roughly 1 cm of travel where 15
to 20 cm was commanded. Displacement alone cannot distinguish two very different
failures: legs cycling while the feet slip (a plant/friction problem), or legs
not cycling at all (a policy problem). Measuring left-knee peak-to-peak travel
over a 3.5 s constant-command run separates them:

| commanded `vx` | knee peak-to-peak | net travel | verdict |
|---|---|---|---|
| 0.05 | 0.0021 rad | 0.003 m | frozen |
| 0.15 | 0.0068 rad | 0.008 m | frozen |
| 0.22 | 0.0151 rad | 0.011 m | frozen |
| 0.25 | 0.5133 rad | 0.276 m | walking |
| 0.40 | 0.6342 rad | 0.615 m | walking |

The knee moves by 34x more across a 0.03 m/s change in command, and the policy's
own peak action magnitude jumps from 0.25 to 0.79 across the same step. **The
legs are frozen, not slipping.** The discontinuity is in the policy's output, not
in the contact solver, so no plant change could ever have moved it.

That retroactively explains the four plant axes tested earlier (actuator type,
joint friction, MJCF collision variant, actuator gain/voltage): all four
correctly showed no effect, because none of them was ever a candidate. They are
left documented below as genuinely eliminated, but they were eliminating the
wrong hypothesis.

### The gates, per axis

Bisected to 0.005 resolution, same method (gait = knee peak-to-peak > 0.05 rad
after a 1 s settle):

| axis | gait threshold | `limits.py` cap | usable band |
|---|---|---|---|
| `vx` forward | 0.238 m/s | 0.25 | 0.238 to 0.25 |
| `vx` backward | -0.326 m/s | -0.25 | **empty** |
| `vy` lateral | 0.312 m/s | 0.20 | **empty** |
| `vyaw` | 1.047 rad/s | 1.5 | 1.047 to 1.5 |

**The authoring envelope and the policy are mutually incompatible on three of
four axes.** Backward and lateral walking are unreachable: the validator's speed
cap sits below the speed at which the policy will take a step, so no legal show
can contain a reverse or a sidestep that produces motion. Forward walking is
reachable only in a 0.012 m/s sliver at the very top of the legal range.

Against real shows: `demo` commands 0.10, `octet` commands 0.15/0.20 forward and
-0.15/-0.20 backward, `showcase` commands 0.20 and 0.24 forward and 0.16 lateral.
Only `showcase`'s 0.24 forward is above a gate. Every other locomotion command in
the repo is, correctly, baked as a duck standing still.

### What this does and does not establish

It is a fact about **this baker's plant**, not yet about the hardware. The sim
also under-tracks in absolute terms (a 0.40 command yields about 0.154 m/s
achieved), so the sim's duck is heavier or lossier than the training plant, and a
plant that under-tracks will also gate later than the real machine. The real
duck's thresholds could be lower. What is now settled is the *mechanism*: a
stand/walk gate, not a contact or actuator defect.

Consistent with this repo's standing instruction, **no show constant and no
`limits.py` cap was changed to make the preview look better.** Whether to raise
the caps (making reverse and lateral moves physically reachable) or to keep the
current slow, stage-safe envelope and accept that legged travel is a narrow band
is a staging decision, and it interacts with hardware safety limits that cannot
be verified until a duck is on the bench.

**Day-one hardware measurement, now specific:** command a real duck a slow ramp
of `vx` from 0 to 0.4 and record where stepping begins, then repeat for `vy` and
`vyaw`. Three numbers settle whether the caps or the baker need to move. This
replaces the earlier, vaguer plan of asking upstream about a low-speed regime,
though that question is still worth asking, and `alpha_stand.onnx` remains the
obvious candidate for a policy-selection gap: it is plausible real `robotd` runs
that policy below the gate and switches at the threshold, which this baker does
not model (see "Which policy drives the bake").

### Measured after raising the caps (2026-09-04)

`limits.py`'s translation caps were raised to 0.40 and `shows/octet` rechoreographed onto symmetric magnitudes that clear both gates (0.34 and 0.38, forward and back). Baked result, against the same show before the change:

| | before (0.15/0.20) | after (0.34/0.38) |
|---|---|---|
| trunk x-range | 0.011 m | 0.247 m |
| `left_knee` range | 0.038 rad | 0.650 rad |
| falls | none | none |

That is a real gait rather than a static lean, and it is what the baked preview now replays, joint for joint.

**Open, and needing hardware to settle: travel is asymmetric between directions, and the asymmetry is state-dependent.** An out-and-back pair commanded at equal magnitude does not return the duck to its mark. In the full `octet` bake every role ends about 0.10 m *behind* its mark after two such pairs; an isolated 1 s-out/1 s-back probe from a standing start drifts about 0.10 m *forward* instead, because at that magnitude the backward leg barely moves the duck at all. Same commands, opposite sign of error, depending on what came before.

No magnitude pair was tuned to cancel this. Fitting constants to a plant already known to under-track by 2-3x (0.40 commanded yields about 0.154 m/s achieved) would be fitting the simulator's error, not the robot's, and this repo's standing rule is that a documented limitation beats a preview that lies. Add it to the day-one hardware list alongside the three gate ramps: command a real duck a symmetric out-and-back at several magnitudes and record the net displacement. If the real machine is symmetric, this is a simulation artefact and the caps need no further change; if it is not, the authoring model needs per-direction magnitudes and the editor should compute the return leg rather than making the author match it by hand.

### The four plant axes, eliminated

Each tested directly, in isolation, measured before and after. All four correctly
show no effect, for the reason given above.

| Candidate | Change made | Effect on the low-speed regime |
|---|---|---|
| Actuator *type* (stock `<position>` PD vs. BAM torque/friction plant) | Ported (`bakelib/bam_actuator.py`) | None. Small-signal stiffness converges to the same 0.55 Nm/rad either way |
| Joint damping/frictionloss (static `0.053`/`0.0048` vs. policy's trained `0`) | Zeroed (a side effect of the BAM port) | None |
| MJCF variant (`robot_groundcontact.xml` vs. the trained `robot_walk.xml`) | Switched (`SCENE_FILENAME`) | None. Bit-identical net travel at every tested speed. Kept anyway: it is the correct plant |
| BAM firmware gain/voltage (`kp_fw=200,vin=7.35` trained values vs. the package's own `kp=400,vin=7.5` defaults) | Already correctly applied; counterfactual tested anyway | None |

### Two further axes checked while resolving this

| Checked | Result |
|---|---|
| Joint ordering: does MuJoCo's `qpos[7:21]` really follow the `<actuator>` block order the observation assumes? | **Confirmed identical.** The MJCF's joint tree order and actuator order are the same 14-name sequence, so `qpos`/`qvel` slices land in the policy's `joint_names` order |
| Home pose: is the MJCF `STAND` keyframe the same vector as the ONNX's own `default_joint_pos` (used both for `joint_pos_delta` and as the action offset)? | **Confirmed identical** to 3 decimal places on all 14 joints |

## Skills: three of five now driven (2026-09-04)

The v1 baker drove no `do` skill at all: it kept `alpha_walking.onnx` running through every skill window and logged `skill_unsimulated`. Measured on a real bake, a `kick_left` moved the knee by 0.007 rad and the trunk not at all -- so "some skills do not play back" was really "no skill plays back, ever, in either preview".

Three of the five authorable skills are now driven by their own policy, and the reasons the other two are not were measured rather than assumed. `manifest.json` specifies each one's command encoding, and all nine policies share the same `obs[1,61] -> action[1,14]` contract, so only the ONNX session, the command block and `action_scale` change; the observation builder is unchanged.

| skill | policy | `kind` | duration | command encoding |
|---|---|---|---|---|
| `kick_left` / `kick_right` | `ball_kick_left.onnx` / `ball_kick_right.onnx` | episodic | 0.5 s | none in the manifest |
| `roulade` | `roulade.onnx` | episodic | 1.0 s | **not driven.** It executes correctly -- the duck launches (trunk z 0.117 to 0.188) and rotates a clean 180 deg -- but after its stated 1.0 s it is still inverted, and the manifest marks it `chain: true` without naming what it chains into. Handing an upside-down duck back to `alpha_walking.onnx` corrupts the whole rest of the bake, which is worse than not simulating it. Logged `skill_unsimulated` until the recovery half of the chain is known. |
| `sit_toggle` | `alpha_sitstand.onnx` | scripted | ramp 2.0 s / unwind 1.0 s | `posture_flag`: `twist.vx` = 1.0 sit, 0.0 stand |
| `ground_pick` | `alpha_ground_pick.onnx` | episodic | 2.8 s | `phase` over `twist.vx,twist.vy`, `period_s` 4.0, `end_phase` 0.7 |
| `mode: "roller"` | `roller.onnx` / `roller_crouch.onnx` | — | — | **still not driven** — a structurally different machine (passive wheel joints, no leg policy) whose MJCF this baker does not load. Unchanged: frozen for the window, logged `mode_unsimulated`. |

### The three inferences, named

Everything above is read from `manifest.json` except these, which are this baker's reading and are flagged in the code at the point each is used:

1. **`phase` means a 2-slot cyclic encoding**, i.e. `twist.vx = cos(2*pi*phase)`, `twist.vy = sin(2*pi*phase)`, with `phase` sweeping 0 to `end_phase` across the clip. The manifest names two slots, a period and an end phase but not the function. Supporting evidence rather than assertion: `duration_s` is exactly `period_s * end_phase` (2.8 = 4.0 * 0.7) for `ground_pick`, and again for `roller_crouch` (3.5 = 5.0 * 0.7), which is what a phase sweep at real time would produce and what an arbitrary pair of numbers would not.
2. **Episodic policies with no manifest command get an idle twist** (`[0,0,0]`, the value `alpha_sitstand`'s own entry names as `idle`) while the authored `head` and `body_pose` commands pass through unchanged. Zeroing the head instead would be an equally arbitrary guess, and the observation needs all 13 values either way.
3. **`sit_toggle` holds.** Sitting is a state, not an impulse: after a sit toggle `alpha_sitstand.onnx` keeps driving with the flag at 1.0 until a later toggle sets it to 0.0, after which control returns to `alpha_walking.onnx` once `unwind_s` elapses. The manifest gives `ramp_s`/`unwind_s` but never says what runs between two toggles. `shows/octet`'s `reed` role is exactly the awkward case -- two `sit_toggle` events 2.0 s apart against a 2.0 s ramp -- and under this reading the second toggle simply begins the unwind from wherever the ramp had reached.

### The plant: scene_walk.xml was the wrong model, and skills are how we found out

`SCENE_FILENAME` moved back to `scene.xml` (`robot_groundcontact.xml`). The earlier switch to `scene_walk.xml` was justified by measuring that it changed nothing: net travel was bit-identical at every tested speed. That measurement was right; the conclusion drawn from it was too broad. Walking only ever uses foot-ground contact, so of course the two models agree.

Counted off the compiled models: `scene.xml` has **12** collidable geoms, `scene_walk.xml` has **6**, and the six missing ones include `hip_l`, `hip_l_2` and `jaw_soft`. A duck on its feet never touches them. A duck sitting rests on its hips.

Driving `alpha_sitstand.onnx` against `scene_walk.xml` therefore let the hips pass through the floor: the trunk sank to z = -0.036 m, below the floor plane, and the duck rolled 178 deg onto its back. That happened for a nonzero command in *any* twist slot, including slots the manifest never mentions, which is what ruled out a slot-identification mistake and pointed at the collision model instead. Against `scene.xml` the same policy sits cleanly at z = 0.0595, pitch -0.03.

Walking is unaffected by the change, as the original measurement predicted: re-baking `shows/octet`, the roles with no skill events are byte-identical between the two models.

### Two hand-off rules the manifest does not state

Both were forced by measurement, and both are this baker's own:

**A skill policy gets a clean command block.** Everything outside the skill's own encoded slots is zero, head_pose and body_pose included. The first reading passed the show's authored head/body commands through, on the grounds that the observation needs all 13 values anyway. Measured consequence: `octet`'s `reed` sat at a pitch of -1.09 rad (62 deg reclined) instead of the -0.03 the same policy produces from a zeroed command, and `alpha_sitstand.onnx` cannot stand up from that posture -- it held the recline through the entire unwind and toppled the moment walking resumed. A skill policy's command contract is what `manifest.json` states for it.

**A stand-up hands back when the duck is standing, not when the clock says so.** `unwind_s` is a nominal duration, not a guarantee about physics. Handing back at exactly 1.0 s left `reed` mid-transition at 0.068 m of its 0.120 m nominal, and `alpha_walking.onnx` put it on the floor within 0.3 s. The hand-off now additionally requires the trunk to be back above 90% of nominal height, with a hard cap of 3x `unwind_s` so a stand-up that never completes cannot hold the policy forever.

**Fall detection is posture-aware.** A deliberate sit is low and pitched, which is exactly what the fall heuristic looks for -- it fired 0.96 s into the first sit at a trunk height of 0.041 m against a 0.042 m threshold. Fall detection is a heuristic about *unintended* collapse, so it is suppressed while a scripted posture policy is deliberately putting the duck on the floor.

### Boundaries are discontinuities, and stay honest

Handing control between `alpha_walking.onnx` and a skill policy carries the physics state across untouched (`prev_action` included), which is what a real robot does too. Nothing is blended or eased: a skill that ends with the duck mid-crouch resumes walking from mid-crouch. Where that looks abrupt it is reporting something real about the hand-off, not hiding it.

## What isn't simulated

Per the project brief: *"a skill you cannot drive faithfully should be recorded in the bake log as unsimulated rather than faked."* This v1 baker drives ordinary locomotion + head + body-pose faithfully (to the fidelity gaps named above) and **does not** drive any of the five `do` skills or roller mode. Nothing pretends to; the base locomotion policy keeps running unmodified through a skill event, and every occurrence is logged (`log[].kind == "skill_unsimulated"`, one entry per event, with the specific policy file and command encoding — now resolvable from `manifest.json` — that a future version would need):

| Skill | Policy | Why not v1 |
|---|---|---|
| `kick_left` / `kick_right` | `ball_kick_left.onnx` / `ball_kick_right.onnx` | Episodic; needs `ball.xml`'s 70 mm/15 g prop loaded into the scene, which this baker's model does not include. |
| `sit_toggle` | `alpha_sitstand.onnx` | Scripted: `manifest.json` gives a `posture_flag` command encoding (`twist.vx` slot, `sit=1.0`/`stand=0.0`, `ramp_s=2.0`, `unwind_s=1.0`) and confirms *what* to send, but not the transition semantics between the perpetual walk policy and this one — in particular what happens when a second `sit_toggle` fires before the first's `ramp_s` completes (`shows/octet/octet.duckshow.json`'s `reed` role does exactly this: two `sit_toggle` events 2.0 s apart against a 2.0 s ramp). Driving this on an unverified guess about hand-off timing would be exactly the "faked" outcome the brief warns against; logging it is the honest choice. |
| `roulade` | `roulade.onnx` | Episodic, and `manifest.json` marks it `"chain": true` — chained to something else the manifest doesn't specify. |
| `ground_pick` | `alpha_ground_pick.onnx` | Episodic, phase-encoded command (`manifest.json`: `period_s`, `end_phase`) against `twist.vx,twist.vy` — resolvable in principle, not implemented in v1. |
| any `mode: "roller"` stretch | `roller.onnx` / `roller_crouch.onnx` against `robot_groundcontact_rollers.xml` | A structurally different physical machine (passive wheel joints, no leg policy) that this baker's model does not load at all. **Scoped to the roller window only, as of 2026-09-04.** The role's walk-mode stretches are simulated normally; physics is frozen and the pose held only while `Sampler.mode_at(t)` reports a non-walk mode, with one `kind: "mode_unsimulated"` entry logged per window carrying that window's own start time. Resuming after the window is a genuine discontinuity — the legged model's frozen state is not a valid roller-model state, and nothing pretends otherwise — but it is confined to the stretch the baker cannot drive. The role is still listed in `unsimulated_roles` if any window was held, so the editor's warning is unchanged. |

`sound` events have no physical effect on any pose field and are not logged — they don't move the duck, so there is nothing to report as unsimulated.

**Update, 2026-09-04: the BAM actuator model is now ported.** This paragraph used to say the gap was open; it is not any more, and this baker no longer drives `alpha_walking.onnx` against MuJoCo's stock `<position>` actuator at all — see "Actuator model: BAM ported, low-speed bifurcation NOT fixed" above for what changed (`bakelib/bam_actuator.py`, following `docs/bake-parts.md` §3.5's own five-step plan against the installed `better-actuator-models==1.0.2`) and, importantly, what it did **not** fix: the low-speed walk-initiation bifurcation that motivated the port turns out not to be caused by the actuator-type substitution after all (measured directly, not just theorized) — see that section for the full finding. `docs/viewer.md`'s "Honesty" section named the pre-port gap as expected; that specific gap (training-plant-vs-bake-plant mismatch) is now closed, even though the symptom that prompted closing it remains.

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
  "physics": { "timestep": 0.005, "decimation": 4, "control_hz": 50.0, "scene": "scene_walk.xml" },
  "bake": {
    "generated_at": "2026-09-03T20:27:55Z",
    "baker": "tools/bake",
    "layout_version": 2,
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
      "walkPhase": ["..."],
      "joints": {
        "left_hip_yaw": ["... 3200 floats ..."], "left_hip_roll": ["..."],
        "left_hip_pitch": ["..."], "left_knee": ["..."], "left_ankle": ["..."],
        "right_hip_yaw": ["..."], "right_hip_roll": ["..."],
        "right_hip_pitch": ["..."], "right_knee": ["..."], "right_ankle": ["..."]
      }
    }
  },
  "log": [
    { "role": "lead", "t": 17.0, "kind": "skill_unsimulated", "detail": "'kick_left' event not driven by physics ..." }
  ]
}
```

- **Field vocabulary** — `poses[role]` carries exactly the field names `docs/viewer.md`'s renderer contract uses (`{role, x, y, heading, headYaw, headPitch, headRoll, neckPitch, bodyZ, bodyRoll, bodyPitch, mouthOpen, walkPhase}`, minus `role` itself since it's the dict key, and minus the optional `resting` the kinematic path also doesn't require) — a consumer already written against the kinematic pose shape needs no new field names, only a new source of frames.
- **Numeric arrays, not per-frame objects** — every array under one role is parallel, length `frame_count` (`round(duration * frame_rate)`, e.g. 3200 for a 64 s show at 50 Hz — `roles × frame_count` sums to exactly `docs/bake-parts.md`'s own "about 25,600 frames total" figure for the 8-duck 64 s octet). Values are rounded before serialization (4-5 decimal digits depending on field, see `bakelib/posecache.py`'s `_ROUND` table) — plenty of precision for anything a renderer draws, and it keeps an 8-duck 64 s cache at **~2.4 MB**, comfortably under the "couple of megabytes" target.
- **`joints` (optional, additive)** — the per-frame angle in radians of every actuated joint the flat fields above do not already carry: the ten leg joints, keyed by their MJCF names verbatim (`left_hip_pitch`, snake_case, matching `duckmodel.JOINT_NAMES`, not the camelCase of the surrounding fields — those are camelCase only because they mirror the JS renderer contract). Each array is frame-parallel with `x`.

  **Why it exists.** Without it a baked preview does not show baked legs. `bakelib/sim.py` integrates all 14 joints under MuJoCo + BAM at 200 Hz, but the cache used to persist only the trunk, the four head joints and `walkPhase` — and `walkPhase` is derived from *trunk displacement* (`_walk_phase_from_xy`), never from a leg. `editor/duck-mesh.js` then re-synthesised all ten leg angles from that scalar using the kinematic path's own `legSwing()`. So the legs in a "baked physics" preview were the kinematic waddle, re-keyed off however far the physics trunk happened to slide: physics simulated the legs, the format discarded them, and the renderer reinvented them. Compounded by the policy's stand/walk gate (see "The low-speed problem"), a show commanding below the gate produced a trunk that barely moved, hence a `walkPhase` that barely advanced, hence legs that barely animated — which reads as "the baked ducks aren't walking" even when the question of whether they *should* be is a separate one the bake answers correctly.

  **Compatibility.** Purely additive: this does **not** bump the `duckbake/1` major (CLAUDE.md rule 4 — unknown fields are ignored everywhere). Verified against every existing consumer: `bake-cache.js`'s `validateBakeCache()` iterates a fixed `POSE_FIELDS` list and never enumerates `Object.keys(p)`; `poseAtTime()` copies only `POSE_FIELDS`; `scripts/editor_server.py`'s `_summarize_cache()` touches only `poses[role].x`. An older loader ignores the block silently and correctly. The reverse direction is the one that needs care, and is handled: every cache already on disk lacks the block, so a consumer must keep the procedural path as a **per-joint** fallback rather than an all-or-nothing one.

  **Why "any recorded joint, by name" rather than "the ten leg joints".** A later baker can add the head four, or a roller model's wheel joints, with no further schema change.

  **Precision and size** — 4 decimals, i.e. 1e-4 rad. That is about 15x finer than the XL330's own 4096-tick encoder resolution (1.53e-3 rad), so more digits carry no physical meaning. Measured on the real cache: it adds ~1.95 MB to the 8-role 64 s octet, taking it from 2.43 MB to ~4.38 MB. That is a gitignored local file fetched from 127.0.0.1 and parsed once; a compact encoding would save ~1 MB and cost the format its "plain floats, same field names" property, so it is not worth it. The head four are deliberately **not** repeated here — they already ship as `headYaw`/`headPitch`/`headRoll`/`neckPitch`.

- **`held_roles`** — roles with at least one held window but real physics elsewhere. Distinct from `unsimulated_roles` on purpose: after holds became per-window rather than per-role, a role with a single `roller` stretch was still being reported as "unsimulated" despite 74% of its show being genuinely simulated, which is both misleading in the log and wrong for any consumer that treats the flag as "this duck is not performing" (the editor dims those on stage). A role appears in exactly one of the two lists.

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

Matches `docs/viewer.md`'s framing exactly — *"A cache keyed by show hash and policy versions, invalidated when either changes"* — plus the physics constants and a `BAKE_LAYOUT_VERSION` (currently `2`, in `bakelib/posecache.py` — bumped from `1` on 2026-09-04 when the actuator plant swapped to BAM, since none of the hashed `physics` fields name the actuator model and an old layout-1 cache would otherwise be indistinguishable from a new layout-2 one despite simulating a genuinely different plant) bumped whenever this baker's own observation/action/output conventions change in a way that would make an old cache silently wrong rather than merely stale. A consumer should compare `cache_key` against what it expects and refuse (or re-bake) a mismatch, exactly as it already must for the kinematic sampler's own show-hash checks.

## Bake log kinds

| `kind` | Meaning |
|---|---|
| `skill_unsimulated` | A `do` event fired; the base locomotion policy kept running through it unmodified. |
| `mode_unsimulated` | One `mode: "roller"` window, logged at the window's own start time; physics was frozen and the pose held for that stretch only, and the role's walk-mode stretches were simulated normally (see "What isn't simulated"). One entry per window, so a show that toggles roller three times logs three. |
| `fell` | Trunk height or tilt crossed the fall heuristic above; logged once, first crossing only. |

## Measured (this run, 2026-09-04, post-BAM, post-`robot_walk.xml`)

`bake_show.py ../../shows/octet/octet.duckshow.json <out>` against all 8 roles, sequential in one process, Apple Silicon (`platform.machine() == "arm64"`):

| Quantity | Measured (`robot_walk.xml`, this pass) | Measured (`robot_groundcontact.xml`, post-BAM, 2026-09-04 earlier) | Measured (pre-BAM, 2026-09-03) |
|---|---|---|---|
| Model + policy load (once, shared) | ~0.3-0.4 s | 0.40 s | 0.34 s |
| All 8 roles, physics only | 5.86-5.87 s (two runs) | 5.92 s | 3.70 s |
| Wall clock, cold process start to cache written | ~6.5 s | ~6.6 s | ~4.3 s |
| Output cache size | 2375.7 KiB (byte-identical across two runs) | 2.32 MB | 2.40 MB |
| Falls | none (`fallen_roles: []`) | none | — |
| `do` events logged `skill_unsimulated` | 5/5 (`kick_left`, `roulade`, two `sit_toggle`, `ground_pick`) | same | — |

Switching `robot_groundcontact.xml` → `robot_walk.xml` (6 fewer geoms, all self-collision-only — see "MJCF variant" above) changed wall-clock by noise, not a measurable amount, consistent with the two files differing only in how many geoms MuJoCo's broad-phase collision pass has to consider, not in per-step cost of anything this bug touches.

**Per-role `x`/`y` ranges, this run (`robot_walk.xml`, both fixes applied):**

| role | `x` range (m) | `x` span | `y` range (m) | `y` span |
|---|---|---|---|---|
| lead | 0.2951 – 0.3063 | 0.0112 | −0.6750 – −0.6717 | 0.0033 |
| echo | 0.2951 – 0.3063 | 0.0112 | −0.2250 – −0.2217 | 0.0033 |
| drift | 0.2952 – 0.3064 | 0.0112 | 0.2250 – 0.2285 | 0.0035 |
| spark | 0.2950 – 0.3063 | 0.0113 | 0.6750 – 0.6783 | 0.0033 |
| reed | −0.3049 – −0.2937 | 0.0112 | −0.6750 – −0.6717 | 0.0033 |
| wren | −0.3049 – −0.2937 | 0.0112 | −0.2250 – −0.2216 | 0.0034 |
| sable | −0.3049 – −0.2936 | 0.0113 | 0.2250 – 0.2284 | 0.0034 |
| flare | −0.3050 – −0.2936 | 0.0114 | 0.6750 – 0.6793 | 0.0043 |

Every role's `x` span is still ~0.011-0.011 m and `y` span ~0.003-0.004 m — the same order of magnitude as every earlier measurement in this document (pre-BAM `lead`: `x` 0.0134 m; post-BAM/pre-MJCF-fix `lead`: `x` 0.0112 m), not the 15-20 cm success criterion. **Success criterion 2 remains not met after both fixes.**

**Determinism, re-checked after both fixes:** `octet.duckshow.json` baked twice back-to-back produced identical `cache_key` (`253bbb9c...`), byte-identical `poses` (compared as parsed JSON), and byte-identical `log`. Only `bake.generated_at`/`bake.wall_clock_s` differ, exactly as the format already documents as varying per-run metadata. `physics.scene` in the cache now reads `"scene_walk.xml"` — this alone changes `cache_key` relative to any pre-fix cache without needing a `BAKE_LAYOUT_VERSION` bump (the hashed `physics` dict already names the scene file; `BAKE_LAYOUT_VERSION` stays `2` — that field exists for changes the hashed inputs *don't* capture, and this one already is).

The ~1.6x per-role slowdown from the BAM port (pre-BAM 3.70 s → post-BAM 5.9 s, both against 8 roles) is expected and not a regression to chase: `bakelib/bam_actuator.py`'s `controller.update()` runs once per **physics** substep (200 Hz) instead of a single `ctrl` assignment per **control** tick (50 Hz) — 4x more actuator-model work per control tick, offset somewhat by `MujocoController.update()` being cheap plain-NumPy per call. This still lands well inside `docs/bake-parts.md` §3.2a's own measured spike (single duck, ~290x realtime) once shared model-loading and Python/import overhead are counted.

## Not built yet

- **The diff itself.** `docs/viewer.md`'s stated payoff — drawing the kinematic dead-reckoned path and this baked path together and marking divergence ("lead is 38 cm left of its mark by 0:41") — is not computed or rendered anywhere. This document's `walkPhase` section above is the only place the two paths are related at all. Both paths are now on disk in comparable coordinates (this cache's `x`/`y`/`heading` vs. `editor/duckshow-viewer.js`'s `precomputeRolePath` output for the same show); wiring an actual diff view is future work, not attempted here.
- **The roller family and the five skills**, as detailed above. (BAM actuators are ported as of 2026-09-04 — see "Actuator model: BAM ported" above — though porting them did not close the low-speed gait gap that motivated the port.)
- **Multi-process parallelism.** `docs/viewer.md`/`docs/bake-parts.md` frame the bake as "embarrassingly parallel, one process or worker per duck." This baker runs every role sequentially in one process instead (see "Physics model and constants" above for why that is fast enough in practice) — a future version wanting sub-second wall-clock on a much longer show could still parallelize across roles. One caveat this sequential-only design has never had to face: BAM mutates the shared, compiled `MjModel`'s `dof_frictionloss`/`dof_damping` every physics step (`bakelib/bam_actuator.py`'s `new_controller` explicitly resets both to zero before each role starts, precisely to keep roles independent despite this); a future parallel-across-roles version would need each worker to hold its own compiled `MjModel` (or an equivalent per-worker reset/lock), not just share one read-only copy the way the pre-BAM baker safely could.
