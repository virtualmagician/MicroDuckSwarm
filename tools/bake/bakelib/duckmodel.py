"""Loads the legged MicroDuck MJCF and pins down the constants a bake needs
that the exported files do not state explicitly. See docs/bake-format.md
"Physics model and constants" for the reasoning behind every value here --
each one is confirmed against a source (the MJCF itself, docs/bake-parts.md's
research, or a direct read of docs/robotd-api.md), not guessed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import mujoco
import numpy as np

from . import bam_actuator

# The 14-DOF action space, in the exact order the exported MJCF declares its
# <actuator> block (confirmed identical in both robot_walk.xml and
# robot_allcollisions.xml) and the order docs/bake-parts.md §3.1 confirms
# the trained action space uses: "0-4 left leg ..., 5-8 neck/head ...,
# 9-13 right leg". qpos/qvel for the 7-DOF free joint precede these, so a
# duck's qpos[7:21] and qvel[6:20] are these 14 joints in this order too.
JOINT_NAMES: tuple[str, ...] = (
    "left_hip_yaw", "left_hip_roll", "left_hip_pitch", "left_knee", "left_ankle",
    "neck_pitch", "head_pitch", "head_yaw", "head_roll",
    "right_hip_yaw", "right_hip_roll", "right_hip_pitch", "right_knee", "right_ankle",
)
NUM_JOINTS = len(JOINT_NAMES)  # 14

# Indices of the four head joints within the 14-vector above -- used to pull
# the physics-actual head angles out for the pose-cache output, and to place
# the head_pose command's 4 values into the observation's command slice.
HEAD_JOINT_INDICES = {
    "neck_pitch": JOINT_NAMES.index("neck_pitch"),
    "head_pitch": JOINT_NAMES.index("head_pitch"),
    "head_yaw": JOINT_NAMES.index("head_yaw"),
    "head_roll": JOINT_NAMES.index("head_roll"),
}

# The complement: every actuated joint the pose cache does NOT already carry
# as a flat headYaw/headPitch/headRoll/neckPitch field -- i.e. the ten leg
# joints. These are what docs/bake-format.md's optional `poses[role].joints`
# block records. Derived from JOINT_NAMES rather than retyped, so the two can
# never drift apart. Name -> index into the 14-vector (add 7 for the qpos
# index, the same offset the head joints use).
LEG_JOINT_INDICES = {
    name: i for i, name in enumerate(JOINT_NAMES) if name not in HEAD_JOINT_INDICES
}
LEG_JOINT_NAMES: tuple[str, ...] = tuple(LEG_JOINT_INDICES)
assert len(LEG_JOINT_NAMES) == 10, LEG_JOINT_NAMES

# docs/bake-parts.md §3.1, confirmed independently two ways (microduck_rl's
# README + the reference Space's constants.js, and mjlab's own
# SimulationCfg default that microduck_rl's task configs never override):
# 200 Hz physics, decimated by 4 to a 50 Hz control rate. Neither exported
# MJCF carries a MuJoCo <option> element, so this baker injects it after
# load rather than editing the XML (mutating model.opt post-compile has the
# same effect on mj_step as declaring it in the file, and it means we never
# touch the fetched assets on disk).
PHYSICS_TIMESTEP = 0.005
CONTROL_DECIMATION = 4
CONTROL_HZ = 50.0
assert abs(1.0 / CONTROL_HZ - PHYSICS_TIMESTEP * CONTROL_DECIMATION) < 1e-12

# Which scene file this baker drives.
#
# 2026-09-04, corrected: this used to be `scene.xml` (-> `robot_groundcontact.xml`)
# for every policy this baker runs, including ordinary locomotion. That was
# wrong for the one policy this v1 baker actually drives:
# `microduck_rl`'s `microduck_velocity_env_cfg.py` sets
# `cfg.scene.entities = {"robot": MICRODUCK_WALK_ROBOT_CFG}`, which
# `microduck_constants.py` resolves via `spec_fn=get_walk_spec` to
# `MICRODUCK_WALK_XML = _ROBOT_DIR / "robot_walk.xml"` -- `alpha_walking.onnx`
# was trained against `robot_walk.xml`, not `robot_groundcontact.xml`. This
# baker now loads `scene_walk.xml` (-> `robot_walk.xml`) to match, for
# exactly the same reason it picked BAM's XL330 parameters over MuJoCo's
# stock actuator: fidelity to the trained plant, not just "a plausible MJCF".
#
# What `robot_walk.xml` actually drops relative to `robot_groundcontact.xml`
# (direct diff, 2026-09-04): 6 fewer geoms (`ngeom` 82 -> 76) -- internal
# self-collision-only meshes (`hip_l`, `leg` x2, `top_head_shell`, `jaw`,
# `bottom_head_shell`, the `np_f970` sensor bracket) either dropped entirely
# or downgraded from `class="collision"` to `class="self_collision_only"`.
# The foot-ground contact geoms that actually drive locomotion dynamics
# (`left_foot_collision`/`right_foot_collision`, mesh `sole_left`/
# `sole_right`) are byte-identical in both files -- same pos, quat, mesh.
# STAND keyframe, actuator block (`chosen_actuator`, `kp=0.55`), and sensor
# set are also byte-identical between the two (checked directly).
#
# Measured consequence of the swap (docs/bake-format.md "MJCF variant
# swap" has the full sweep): because only self-collision geometry differs,
# and the low/mid-speed walk-initiation regime never engages self-collision
# (an upright or gently-stepping duck's legs and head never touch its own
# torso), switching to `scene_walk.xml` produced bit-for-bit identical net
# travel at every tested commanded speed (0.05-0.40 m/s) and left the
# walk-initiation threshold exactly where it was under `scene.xml`. This is
# still the correct model to drive `alpha_walking.onnx` against -- it just
# turned out not to be the axis the reported low-speed bug lives on.
#
# The skill family (`alpha_sitstand`, `alpha_ground_pick`, `ball_kick_*`,
# `roulade`) and roller mode need `robot_groundcontact.xml` /
# `robot_groundcontact_rollers.xml` respectively, per the same upstream
# config (`get_standup_spec` / rollers spec_fn) -- moot for this v1 baker,
# which never drives those policies at all (base locomotion keeps running
# unmodified through a skill window, logged `skill_unsimulated`; roller mode
# is logged `mode_unsimulated` and held static -- see "What isn't
# simulated"). A future version that actually drives a skill policy for its
# window would need to swap the compiled `MjModel` for that stretch (or load
# both up front) rather than picking one scene for the whole bake, since a
# single MJCF is not simultaneously correct for both families.
#
# Not `scene_allcollisions.xml`'s underlying `robot_allcollisions.xml`
# either, for the reason already established before this correction:
# upstream renamed the curated full-collision file this repo's research
# originally catalogued as `robot_allcollisions.xml` to `robot_groundcontact.xml`,
# and reassigned the old name to a *different*, denser file (every geom
# gets a matching collision copy, not the curated self-collision subset
# training used) that `docs/bake-parts.md` confirms directly "is not on the
# needed list" for a bake driver.
# scene.xml (-> robot_groundcontact.xml), the full ground-contact legged
# model. Changed back from scene_walk.xml on 2026-09-04, with evidence that
# the earlier switch lacked.
#
# The switch TO scene_walk.xml was made because it is the model
# alpha_walking.onnx trained against, and was justified by measuring that it
# made no difference: net travel was bit-identical at every tested speed.
# That measurement was correct and the conclusion drawn from it was too
# broad. Walking only ever uses foot-ground contact, so of course the two
# agree; the variants were then treated as interchangeable in general.
#
# They are not. Counted directly off the compiled models, scene.xml has 12
# collidable geoms and scene_walk.xml has 6 -- and the six missing ones
# include hip_l, hip_l_2 and jaw_soft. A duck standing on its feet never
# touches them. A duck SITTING rests on its hips. Driving
# alpha_sitstand.onnx against scene_walk.xml therefore let the hips pass
# straight through the floor: measured, the trunk sank to z = -0.036 m
# (below the floor plane) and the duck rolled 178 deg onto its back, every
# time, for a command in ANY twist slot -- which is what ruled out a
# slot-identification mistake and pointed here instead. roulade.onnx failed
# the same way for the same reason.
#
# scene.xml is also what the skill policies trained against
# (docs/bake-format.md), and what editor/duck-mesh.js already builds its
# render skeleton from, so this makes the baker, the renderer and the
# policies agree on one model.
SCENE_FILENAME = "scene.xml"

# The exported scene's own STAND keyframe (docs/bake-parts.md §3.6 and this
# file's own read of scene_allcollisions.xml's <keyframe> block) is the only
# concrete "nominal standing pose" this repo has read out of the source
# material. It is used three ways, documented in docs/bake-format.md:
#   1. the reset state every bake starts from (a duck starts each show
#      standing, matching how a real duck is placed before GO);
#   2. the joint-angle zero-point for the 14 action outputs (this baker
#      assumes, per standard IsaacLab/mjlab practice but NOT a literally
#      confirmed fact -- see docs/bake-format.md -- that
#      ctrl_target = stand_qpos + action * action_scale);
#   3. the zero-point body_pose[2] ("z") and head_pose deltas are taken
#      from (docs/bake-parts.md §3.6: "z delta from nominal_height"; the
#      head_pose docstring: "deltas from default joint positions").
STAND_KEYFRAME_NAME = "STAND"


@dataclass(frozen=True)
class DuckModel:
    model: "mujoco.MjModel"
    stand_qpos: np.ndarray  # full nq-length qpos at the STAND keyframe
    stand_joint_qpos: np.ndarray  # (14,) the 14 actuated joints only, STAND values
    nominal_height: float  # STAND qpos[2] -- trunk z at nominal standing pose
    trunk_body_id: int
    imu_sensor_adr: dict[str, tuple[int, int]]  # name -> (start, dim) into sensordata
    bam_rig: bam_actuator.BamRig  # the ported XL330 actuator model -- see bakelib/bam_actuator.py


def load_duck_model(mjcf_dir: Path) -> DuckModel:
    """Load the legged scene MJCF from `mjcf_dir` (assets/microduck/mjcf),
    inject the confirmed physics timestep, and resolve the STAND keyframe.

    Raises FileNotFoundError with a clear message if assets/microduck/ is
    absent -- see docs/viewer.md "Assets are supplied, never vendored":
    this tool must fail obviously, not silently fall back to nothing.
    """
    scene_path = mjcf_dir / SCENE_FILENAME
    if not scene_path.exists():
        raise FileNotFoundError(
            f"{scene_path} not found. tools/bake needs assets/microduck/ populated -- "
            f"see docs/bake-parts.md §2 for the (user-run, never automated) fetch commands."
        )
    # Loaded as an editable MjSpec, not directly to MjModel, so the 14
    # stock <position> actuators can be converted to BAM's torque-motor
    # plant before compiling (bakelib/bam_actuator.py, docs/bake-parts.md
    # §3.5) -- mirrors bam.mjlab.BamActuator.edit_spec()'s own approach of
    # editing the MjSpec once at build time, not patching the compiled
    # MjModel after the fact.
    spec = mujoco.MjSpec.from_file(str(scene_path))
    bam_model = bam_actuator.load_bam_model()
    force_limit = bam_actuator.edit_spec_to_bam(spec, JOINT_NAMES, bam_model)
    model = spec.compile()
    model.opt.timestep = PHYSICS_TIMESTEP

    stand_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_KEY, STAND_KEYFRAME_NAME)
    if stand_id < 0:
        raise ValueError(f"{scene_path} has no {STAND_KEYFRAME_NAME!r} keyframe")
    stand_qpos = np.array(model.key_qpos[stand_id], dtype=np.float64).copy()
    stand_joint_qpos = stand_qpos[7:7 + NUM_JOINTS].copy()
    nominal_height = float(stand_qpos[2])

    trunk_body_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "trunk_base")
    if trunk_body_id < 0:
        raise ValueError(f"{scene_path} has no 'trunk_base' body")

    imu_sensor_adr: dict[str, tuple[int, int]] = {}
    for name in ("orientation", "imu_ang_vel"):
        sid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SENSOR, name)
        if sid < 0:
            raise ValueError(f"{scene_path} has no {name!r} sensor")
        adr = int(model.sensor_adr[sid])
        dim = int(model.sensor_dim[sid])
        imu_sensor_adr[name] = (adr, dim)

    bam_rig = bam_actuator.build_rig(model, JOINT_NAMES, bam_model, force_limit)

    return DuckModel(
        model=model,
        stand_qpos=stand_qpos,
        stand_joint_qpos=stand_joint_qpos,
        nominal_height=nominal_height,
        trunk_body_id=trunk_body_id,
        imu_sensor_adr=imu_sensor_adr,
        bam_rig=bam_rig,
    )


def reset_to_mark(duck: DuckModel, data: "mujoco.MjData", x: float, y: float, heading: float) -> None:
    """Reset `data` to the STAND keyframe, then place the trunk at stage
    position (x, y) facing `heading` radians (about world Z) instead of the
    keyframe's own (0, 0, facing +X).

    Why: docs/viewer.md's kinematic path dead-reckons from the role's stage
    *mark* (`editor.marks[role]`, or a spread default -- see
    editor/duckshow-viewer.js resolveMark/defaultMarkFor, mirrored in
    marks.py). The physics bake's whole value is the diff against that path
    (docs/viewer.md "the payoff: the diff"), which only means anything if
    both paths start from the same place. Placing each duck at its own mark
    before stepping means the recorded x/y/heading are already in stage
    (world) coordinates -- no post-hoc frame transform needed anywhere else
    in this file.
    """
    mujoco.mj_resetDataKeyframe(duck.model, data, mujoco.mj_name2id(duck.model, mujoco.mjtObj.mjOBJ_KEY, STAND_KEYFRAME_NAME))
    c, s = math.cos(heading / 2.0), math.sin(heading / 2.0)
    data.qpos[0] = x
    data.qpos[1] = y
    # STAND's own orientation is identity (quat="1 0 0 0" on trunk_base, and
    # key_qpos[3:7] == [1,0,0,0] -- confirmed by direct read), so composing
    # with a pure Z-rotation is just that rotation, not a quaternion product.
    data.qpos[3] = c
    data.qpos[4] = 0.0
    data.qpos[5] = 0.0
    data.qpos[6] = s
    mujoco.mj_forward(duck.model, data)
