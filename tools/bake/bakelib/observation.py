"""Builds the 61-dim observation: 48 proprioception + 13 command
(docs/bake-parts.md §3.6, and this repo's own MicroDuckSwarm task
instructions). See docs/bake-format.md "Observation layout -- confidence
by field" for exactly which of the pieces below are a confirmed fact from
source, and which are this baker's own inference (labeled here too, at
the point each is built, not just in the doc).
"""

from __future__ import annotations

import numpy as np

from .duckmodel import DuckModel, HEAD_JOINT_INDICES, NUM_JOINTS

# --- proprioception (48) ----------------------------------------------
#
# docs/bake-parts.md §4 "Simulation fidelity": "The exact 48-dim
# proprioception breakdown (3 gyro + 3 gravity + 42 joint values) is well
# supported by policies/README.md's description of the legacy 51-D format,
# but the internal 3-way split of the 42 joint values per servo
# (position/velocity/previous-action, the standard convention, versus
# something else) is Strand 1's inference from standard practice, not a
# literal quote." This baker follows that same inference -- pos, then vel,
# then previous raw action, each 14 long -- because it is the standard
# IsaacLab/mjlab observation-term ordering and nothing more specific was
# found. NOT independently confirmed against microduck_rl's actual mdp.py
# observation-term registration order. If a real duck's gait looks wrong
# in a way that isn't explained by the actuator-model gap
# (docs/viewer.md "Honesty"), this ordering is the first thing to
# re-derive from source.
PROPRIO_LEN = 48
COMMAND_LEN = 13
OBS_LEN = PROPRIO_LEN + COMMAND_LEN  # 61, matches policies/manifest.json


def _quat_rotate_inverse(quat_wxyz: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotate world-frame vector `v` into the frame of `quat_wxyz`
    (standard inverse quaternion rotation via the conjugate, since the
    quaternion is unit-norm). Used for the projected-gravity term, the
    conventional way legged-robot proprioception expresses "which way is
    down" in the body frame.
    """
    w, x, y, z = quat_wxyz
    qv = np.array([-x, -y, -z])  # conjugate's vector part
    t = 2.0 * np.cross(qv, v)
    return v + w * t + np.cross(qv, t)


def read_proprioception(duck: DuckModel, data, prev_action: np.ndarray) -> np.ndarray:
    """gyro(3) + projected_gravity(3) + joint_pos_delta(14) + joint_vel(14) + prev_action(14)."""
    orient_adr, orient_dim = duck.imu_sensor_adr["orientation"]
    gyro_adr, gyro_dim = duck.imu_sensor_adr["imu_ang_vel"]
    assert orient_dim == 4 and gyro_dim == 3

    quat = np.array(data.sensordata[orient_adr:orient_adr + 4], dtype=np.float64)
    # gyro: read from the noise-free "imu_ang_vel" sensor, not the
    # noisy "angular-velocity" sensor the same sensors.xml also defines.
    # A bake wants a deterministic replay of the *policy*, not a replay of
    # whatever pseudo-random noise a training-time sensor model injected --
    # see docs/bake-format.md "Sensor noise" for the reasoning.
    gyro = np.array(data.sensordata[gyro_adr:gyro_adr + 3], dtype=np.float64)

    gravity_world = np.array([0.0, 0.0, -1.0])
    projected_gravity = _quat_rotate_inverse(quat, gravity_world)

    joint_qpos = np.array(data.qpos[7:7 + NUM_JOINTS], dtype=np.float64)
    joint_qvel = np.array(data.qvel[6:6 + NUM_JOINTS], dtype=np.float64)
    joint_pos_delta = joint_qpos - duck.stand_joint_qpos

    proprio = np.concatenate([gyro, projected_gravity, joint_pos_delta, joint_qvel, prev_action])
    assert proprio.shape == (PROPRIO_LEN,)
    return proprio.astype(np.float32)


# --- command (13) = [twist(3), head_pose(4), body_pose(6)] -------------
# Order confirmed by direct quote from microduck_rl's task source
# (docs/bake-parts.md §3.6).

def build_command(
    duck: DuckModel,
    locomotion,  # duckshow.LocomotionSample | None
    head,  # duckshow.HeadSample | None
    pose,  # duckshow.PoseSample | None
) -> np.ndarray:
    # twist(3) = [vx, vy, vyaw], directly off the locomotion track -- a role
    # with no locomotion track (idle stand, docs/duckshow-format.md) gets
    # the zero vector, matching "the duck's defaults rule" for an omitted
    # curve track.
    if locomotion is not None:
        twist = np.array([locomotion.vx, locomotion.vy, locomotion.vyaw], dtype=np.float64)
    else:
        twist = np.zeros(3)

    # head_pose(4) = [neck_pitch, head_pitch, head_yaw, head_roll] deltas
    # from default joint positions (mdp.py docstring, quoted verbatim in
    # docs/bake-parts.md §3.6). This baker's .duckshow `head` track carries
    # *absolute* joint-angle targets (docs/robotd-api.md: robot.head takes
    # the same four field names as absolute servo targets) -- so the
    # command sent to the policy is that absolute target minus the STAND
    # keyframe's own head angles, taken as "default". This subtraction is
    # this baker's own inference, not a confirmed fact: the training
    # config's actual "default_joint_pos" was not read from source, only
    # assumed to match the exported scene's STAND keyframe (a reasonable
    # assumption -- the keyframe looks built for exactly this purpose --
    # but not verified against microduck_rl's env config). See
    # docs/bake-format.md.
    if head is not None:
        default = duck.stand_joint_qpos
        head_pose = np.array([
            head.neck_pitch - default[HEAD_JOINT_INDICES["neck_pitch"]],
            head.head_pitch - default[HEAD_JOINT_INDICES["head_pitch"]],
            head.head_yaw - default[HEAD_JOINT_INDICES["head_yaw"]],
            head.head_roll - default[HEAD_JOINT_INDICES["head_roll"]],
        ], dtype=np.float64)
    else:
        head_pose = np.zeros(4)

    # body_pose(6) = [x, y, z, roll, pitch, yaw], deltas from nominal
    # standing (docs/bake-parts.md §3.6, quoted from mdp.py). Per this
    # project's own instructions and that section's resolution:
    # body_pose = [0, 0, pose.z, pose.roll, pose.pitch, 0] -- x/y/yaw have
    # no robotd wire equivalent and are always zero for a real duck.
    #
    # Refinement over the literal formula: robotd-api.md says robot.pose is
    # "glided while active, snaps back when false" -- i.e. on real hardware
    # the z/roll/pitch target is *ignored* (treated as 0) whenever
    # pose.active is false, regardless of what the track's z/roll/pitch
    # values happen to hold at that instant (a held-from-last-keyframe
    # value that was never meant to still apply). The kinematic preview
    # (editor/duckshow-viewer.js) does not gate on `active` at all -- a
    # known, intentional simplification there. This baker honors `active`
    # because its whole purpose is fidelity to what robotd will actually
    # do, and this is a real, documented piece of robotd's behavior, not
    # an inference.
    if pose is not None and pose.active:
        body_pose = np.array([0.0, 0.0, pose.z, pose.roll, pose.pitch, 0.0], dtype=np.float64)
    else:
        body_pose = np.zeros(6)

    command = np.concatenate([twist, head_pose, body_pose])
    assert command.shape == (COMMAND_LEN,)
    return command.astype(np.float32)


def build_observation(duck: DuckModel, data, prev_action: np.ndarray, locomotion, head, pose) -> np.ndarray:
    proprio = read_proprioception(duck, data, prev_action)
    command = build_command(duck, locomotion, head, pose)
    obs = np.concatenate([proprio, command])
    assert obs.shape == (OBS_LEN,)
    return obs
