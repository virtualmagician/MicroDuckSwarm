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

# Which scene file this baker drives. docs/bake-parts.md's own parts table
# recommends the full-collision model ("needed for stand/sit/ground-pick/
# kick/roulade, i.e. every `do` skill event and the pose.z crouch") over the
# walk-only model, even though this v1 baker does not yet drive the skill
# policies themselves (see docs/bake-format.md "What isn't simulated") --
# self-collision and ground contact still matter for an ordinary walk/crouch
# bake, and for detecting a fall honestly rather than clipping through the
# floor.
#
# `scene.xml` (which <include>s `robot_groundcontact.xml`), not
# `scene_allcollisions.xml` -- this is the important, live-verified part:
# upstream renamed the curated full-collision file that doc's table
# originally called `robot_allcollisions.xml` to `robot_groundcontact.xml`
# sometime before 2026-09-03, and reassigned the old name to a *different*
# file (every geom gets a matching collision copy, not the curated subset
# training actually used -- confirmed by direct read of
# config_mjcf_groundcontact.json's `ignore` block vs. the new
# robot_allcollisions.xml's own export config). docs/bake-parts.md itself
# was re-verified against upstream the same day this baker was written and
# now says plainly that the newer robot_allcollisions.xml "is not on the
# needed list" for a bake driver. `scene.xml`'s STAND keyframe, actuator
# order, and sensor set are byte-identical to `scene_allcollisions.xml`'s
# (checked directly), so this is a same-day correction, not a rewrite.
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
    model = mujoco.MjModel.from_xml_path(str(scene_path))
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

    return DuckModel(
        model=model,
        stand_qpos=stand_qpos,
        stand_joint_qpos=stand_joint_qpos,
        nominal_height=nominal_height,
        trunk_body_id=trunk_body_id,
        imu_sensor_adr=imu_sensor_adr,
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
