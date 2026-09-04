"""Per-role physics loop: steps one duck through its whole show and
returns the pose-cache arrays plus this role's bake-log entries.

Ducks never interact physically (docs/viewer.md: "they share a floor,
never each other") -- each role gets its own MjData against a single
shared, already-compiled MjModel (loading the ~26 MB of STL meshes is the
expensive part of a fresh load, ~0.3 s measured on this machine; MjData
construction is microseconds, so sharing the compiled model across roles
in one process is a real, safe speedup, not a fidelity shortcut).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import mujoco
import numpy as np

from . import bam_actuator, duckmodel, marks, observation, policyset

CONTROL_HZ = duckmodel.CONTROL_HZ
CONTROL_DECIMATION = duckmodel.CONTROL_DECIMATION

# docs/bake-parts.md §1b: "The observation normalizer is baked into the
# ONNX graph ... so a consumer does not reimplement normalization" -- but
# nothing in that research pass pins down the action *de*-normalization
# (whether the network's raw output is already a radians offset, or needs
# scaling first). policies/manifest.json gives an explicit action_scale
# for the two roller-family policies (0.8) but not for alpha_walking or
# alpha_stand, which this baker reads as "1.0 by omission" -- the
# manifest's own schema treats the field as optional per-policy, and the
# roller entries look like the exception being called out, not the rule.
# NOT independently confirmed from microduck_rl's training config. If the
# baked gait looks like it's barely moving from the STAND pose (actions
# too small) or thrashing wildly (actions too large), this is the first
# constant to revisit.
LOCOMOTION_ACTION_SCALE = 1.0

# editor/duckshow-viewer.js's own constants (duplicated here, not derived
# from any physics fact -- see marks.py's module docstring for why this
# split across languages exists at all). Reused verbatim so a baked
# walkPhase and a kinematic walkPhase mean the same animation-cycle
# distance, even though the *distance* each is computed from now differs
# (dead-reckoned commanded distance for the kinematic path; real
# physics-simulated distance for this bake -- exactly the kind of gap
# docs/viewer.md's "the payoff: the diff" wants visible).
PHASE_PER_METRE = (2.0 * math.pi) / 0.10
STOP_SPEED_EPS = 0.005

# Fall detection (this baker's own heuristic, not a number from any
# source): a trunk height under a third of nominal standing, or a
# roll/pitch tilt past ~60 degrees, is not a duck that recovers.
FALL_HEIGHT_FRACTION = 0.35
FALL_TILT_RAD = 1.0


def _quat_to_yaw_pitch_roll(quat_wxyz: np.ndarray) -> tuple[float, float, float]:
    """Extrinsic Z-Y-X (yaw, then pitch, then roll) Euler decomposition.
    This baker's own choice of convention for turning the trunk's physics
    orientation into the pose-cache's heading/bodyRoll/bodyPitch fields --
    the .duckshow format and docs/robotd-api.md define roll/pitch only as
    small deltas-from-upright (never a full 3D decomposition, since no real
    command needs one), so there is no upstream convention to match here.
    Standard robotics 3-2-1 sequence; see docs/bake-format.md.
    """
    w, x, y, z = quat_wxyz
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    sinp = 2.0 * (w * y - z * x)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    return yaw, pitch, roll


@dataclass
class RoleBakeLogEntry:
    role: str
    t: float
    kind: str  # "skill_unsimulated" | "mode_unsimulated" | "fell"
    detail: str

    def to_json(self) -> dict:
        return {"role": self.role, "t": round(self.t, 3), "kind": self.kind, "detail": self.detail}


@dataclass
class RoleBakeResult:
    role: str
    frame_count: int
    x: np.ndarray
    y: np.ndarray
    heading: np.ndarray
    head_yaw: np.ndarray
    head_pitch: np.ndarray
    head_roll: np.ndarray
    neck_pitch: np.ndarray
    body_z: np.ndarray
    body_roll: np.ndarray
    body_pitch: np.ndarray
    mouth_open: np.ndarray
    walk_phase: np.ndarray
    log: list[RoleBakeLogEntry] = field(default_factory=list)
    simulated: bool = True  # False for a role this baker could not drive at all (e.g. roller mode)


_SKILL_POLICY_HINT = {
    "kick_left": "ball_kick_left.onnx (episodic; needs the ball.xml prop, not loaded by this baker)",
    "kick_right": "ball_kick_right.onnx (episodic; needs the ball.xml prop, not loaded by this baker)",
    "sit_toggle": "alpha_sitstand.onnx (scripted, posture_flag command on twist.vx per manifest.json)",
    "roulade": "roulade.onnx (episodic, chained)",
    "ground_pick": "alpha_ground_pick.onnx (episodic, phase-encoded command per manifest.json)",
}


def simulate_role(
    duck: duckmodel.DuckModel,
    policies: policyset.PolicySet,
    show_doc: dict,
    sampler,  # duckshow.Sampler
    role: str,
    role_index: int,
    role_total: int,
    duration: float,
    progress=None,  # optional callable(role, k, frame_count)
) -> RoleBakeResult:
    frame_count = max(1, round(duration * CONTROL_HZ))

    mark = marks.resolve_mark(show_doc, role, role_index, role_total)

    tracks = sampler.tracks
    mode_events = [e.mode for e in tracks.events if e.mode is not None]
    if any(m != "walk" for m in mode_events):
        # docs/duckshow-format.md: "the only two values real robotd
        # accepts" are "walk"/"roller". This baker only loads the legged
        # (walk-mode) MJCF and alpha_walking.onnx -- a roller-mode show
        # would need robot_allcollisions_rollers.xml and roller.onnx,
        # neither wired up here. Rather than silently simulate the wrong
        # physical machine for that stretch of the show, log it plainly
        # and still emit a full-length array (idle standing throughout)
        # so the cache stays well-formed.
        log = [RoleBakeLogEntry(
            role=role, t=0.0, kind="mode_unsimulated",
            detail="show uses a 'roller' mode event; this baker only drives the legged "
                   "(walk-mode) model. Role held static at its mark instead of simulated.",
        )]
        return _static_role_result(duck, role, frame_count, mark, sampler, log)

    data = mujoco.MjData(duck.model)
    # Build this role's BAM controller before reset_to_mark's mj_forward --
    # mirrors bam.mujoco.Simulator.reset()'s own order (construct the
    # controller against a freshly-zeroed MjData, *then* set qpos/qvel and
    # call mj_forward), and also resets the shared model's
    # dof_frictionloss/dof_damping back to zero first (see
    # bam_actuator.new_controller's docstring for why that matters when one
    # compiled MjModel is reused across every role).
    controller = bam_actuator.new_controller(duck.bam_rig, duck.model, data)
    duckmodel.reset_to_mark(duck, data, mark.x, mark.y, mark.heading)

    x = np.zeros(frame_count)
    y = np.zeros(frame_count)
    heading = np.zeros(frame_count)
    head_yaw = np.zeros(frame_count)
    head_pitch = np.zeros(frame_count)
    head_roll = np.zeros(frame_count)
    neck_pitch = np.zeros(frame_count)
    body_z = np.zeros(frame_count)
    body_roll = np.zeros(frame_count)
    body_pitch = np.zeros(frame_count)
    mouth_open = np.zeros(frame_count)

    log: list[RoleBakeLogEntry] = []
    fallen_logged = False
    prev_action = np.zeros(policyset.ACTION_LEN, dtype=np.float32)
    prev_event_t = -1e-9

    hi = {name: 7 + idx for name, idx in duckmodel.HEAD_JOINT_INDICES.items()}

    for k in range(frame_count):
        t = k / CONTROL_HZ
        sample = sampler.at(t)

        for e in sampler.events_between(prev_event_t, t):
            if e.do is not None:
                hint = _SKILL_POLICY_HINT.get(e.do, "no policy entry found in manifest.json")
                log.append(RoleBakeLogEntry(
                    role=role, t=e.t, kind="skill_unsimulated", detail=(
                        f"'{e.do}' event not driven by physics in this v1 baker -- the base "
                        f"locomotion policy keeps running through it unchanged. Would need {hint}. "
                        f"See docs/bake-format.md \"What isn't simulated\"."
                    ),
                ))
        prev_event_t = t

        quat = np.array(data.sensordata[
            duck.imu_sensor_adr["orientation"][0]:duck.imu_sensor_adr["orientation"][0] + 4
        ])
        yaw, pitch, roll = _quat_to_yaw_pitch_roll(quat)

        x[k] = data.qpos[0]
        y[k] = data.qpos[1]
        heading[k] = yaw
        body_z[k] = data.qpos[2] - duck.nominal_height
        body_roll[k] = roll
        body_pitch[k] = pitch
        neck_pitch[k] = data.qpos[hi["neck_pitch"]]
        head_pitch[k] = data.qpos[hi["head_pitch"]]
        head_yaw[k] = data.qpos[hi["head_yaw"]]
        head_roll[k] = data.qpos[hi["head_roll"]]
        # mouthOpen is never touched by physics -- the trained action space
        # is 14 joints and excludes the mouth (docs/bake-parts.md §3.1,
        # docs/viewer.md "What it cannot do"). Pass the show's own mouth
        # track straight through, exactly like the kinematic path.
        mouth_open[k] = sample.mouth.open if sample.mouth is not None else 0.0

        if not fallen_logged:
            fell = (data.qpos[2] < duck.nominal_height * FALL_HEIGHT_FRACTION) or \
                   (abs(roll) > FALL_TILT_RAD) or (abs(pitch) > FALL_TILT_RAD)
            if fell:
                fallen_logged = True
                log.append(RoleBakeLogEntry(
                    role=role, t=t, kind="fell", detail=(
                        f"trunk height {data.qpos[2]:.3f} m (nominal {duck.nominal_height:.3f} m), "
                        f"roll {roll:.2f} rad, pitch {pitch:.2f} rad."
                    ),
                ))

        if progress is not None:
            progress(role, k, frame_count)

        if k == frame_count - 1:
            break

        obs = observation.build_observation(duck, data, prev_action, sample.locomotion, sample.head, sample.pose)
        action = policyset.run_locomotion_policy(policies, obs)
        q_target = duck.stand_joint_qpos + action.astype(np.float64) * LOCOMOTION_ACTION_SCALE
        # The policy's target *angle* updates once per control tick (50 Hz)
        # -- same as before BAM. What changes is what turns that angle into
        # torque: controller.update() now recomputes BAM's firmware
        # voltage-control law + load-dependent friction every *physics*
        # substep (200 Hz), not just once per control tick, exactly mirroring
        # bam.mujoco.Simulator.step()'s own one-update()-per-mj_step() loop
        # (docs/bake-parts.md §3.5 step 4) -- a real servo's internal PID
        # loop runs far faster than the 50 Hz policy that sets its target.
        for name, val in zip(duckmodel.JOINT_NAMES, q_target):
            controller.set_q_target(name, val)
        prev_action = action

        for _ in range(CONTROL_DECIMATION):
            controller.update()
            mujoco.mj_step(duck.model, data)

    walk_phase = _walk_phase_from_xy(x, y)

    return RoleBakeResult(
        role=role, frame_count=frame_count,
        x=x, y=y, heading=heading,
        head_yaw=head_yaw, head_pitch=head_pitch, head_roll=head_roll, neck_pitch=neck_pitch,
        body_z=body_z, body_roll=body_roll, body_pitch=body_pitch,
        mouth_open=mouth_open, walk_phase=walk_phase, log=log, simulated=True,
    )


def _walk_phase_from_xy(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Cumulative stride phase from the *actual* physics trajectory, same
    formula as editor/duckshow-viewer.js's precomputeRolePath (distance
    since the last sample, scaled by PHASE_PER_METRE, held while the duck
    is below STOP_SPEED_EPS). This bake's control tick spacing is exactly
    1/50 s = 0.02 s, i.e. the same dt the kinematic path's DEFAULT_DT uses,
    so per-frame distances are directly comparable between the two paths.
    """
    n = len(x)
    phase = np.zeros(n)
    acc = 0.0
    dt = 1.0 / CONTROL_HZ
    for i in range(1, n):
        dist = math.hypot(x[i] - x[i - 1], y[i] - y[i - 1])
        speed = dist / dt if dt > 0 else 0.0
        if speed > STOP_SPEED_EPS:
            acc += dist * PHASE_PER_METRE
        phase[i] = acc
    return phase


def _static_role_result(duck, role, frame_count, mark, sampler, log) -> RoleBakeResult:
    """A role this baker declines to simulate at all (roller mode). Holds
    at its mark, standing, for the whole show -- mouthOpen still passes
    through (it never depended on physics anyway).
    """
    ones = np.ones(frame_count)
    mouth = np.zeros(frame_count)
    for k in range(frame_count):
        s = sampler.at(k / CONTROL_HZ)
        mouth[k] = s.mouth.open if s.mouth is not None else 0.0
    return RoleBakeResult(
        role=role, frame_count=frame_count,
        x=mark.x * ones, y=mark.y * ones, heading=mark.heading * ones,
        head_yaw=np.zeros(frame_count), head_pitch=np.zeros(frame_count),
        head_roll=np.zeros(frame_count), neck_pitch=np.zeros(frame_count),
        body_z=np.zeros(frame_count), body_roll=np.zeros(frame_count), body_pitch=np.zeros(frame_count),
        mouth_open=mouth, walk_phase=np.zeros(frame_count),
        log=log, simulated=False,
    )
